"""containers.py - open the compressed files a push carries, so the barrier can read inside them.

A term inside a gzip, tar, zip, xz or bzip2 file, or inside a font's compressed tables, is not in
the file's raw bytes: the byte lane cannot see it (barrier review 3, B4). This module recognises a
container by its magic bytes (never by its name), opens it with the standard library, and yields
every member with its name, recursing into members that are containers themselves.

Caps keep a hostile or huge archive from exhausting memory: a nesting depth, a member count, a size
per member and a total size per top-level file. A container this module recognises but cannot open
within the caps (a damaged stream, an encrypted zip member, a WOFF2 font without the `brotli`
module, a 7z or rar archive) raises `Unopenable`; the barrier then refuses the push unless an allow
entry names that blob's digest with a reason.

Honest limit: formats with no magic bytes (a raw brotli or raw deflate stream) and compressed
streams inside documents (PNG image data, PDF streams) are not opened. The byte lane still reads
their raw bytes.
"""
from __future__ import annotations

import bz2
import io
import lzma
import struct
import tarfile
import zipfile
import zlib

MAX_DEPTH = 4
MAX_MEMBERS = 20000
MAX_MEMBER_BYTES = 64 * 1024 * 1024
MAX_TOTAL_BYTES = 256 * 1024 * 1024
_STEP = 1 << 20


class Unopenable(Exception):
    """A recognised container that could not be opened within the caps."""


def sniff(raw: bytes):
    """The container kind of `raw` from its magic bytes, or None for anything else."""
    if raw[:3] == b"\x1f\x8b\x08":
        return "gzip"
    if raw[:3] == b"BZh" and raw[3:4] in b"123456789" and len(raw) > 4 and \
            raw[4:10] in (b"\x31\x41\x59\x26\x53\x59", b"\x17\x72\x45\x38\x50\x90"):
        return "bzip2"
    if raw[:6] == b"\xfd7zXZ\x00":
        return "xz"
    if raw[:4] in (b"PK\x03\x04", b"PK\x05\x06"):
        return "zip"
    if len(raw) >= 262 and raw[257:262] == b"ustar":
        return "tar"
    if raw[:4] == b"wOFF":
        return "woff"
    if raw[:4] == b"wOF2":
        return "woff2"
    if raw[:4] == b"\x28\xb5\x2f\xfd":
        return "zstd"
    if raw[:6] == b"7z\xbc\xaf\x27\x1c":
        return "7z"
    if raw[:6] == b"Rar!\x1a\x07":
        return "rar"
    return None


class _Budget(object):
    def __init__(self):
        self.members = 0
        self.total = 0

    def take(self, n):
        self.members += 1
        self.total += n
        if self.members > MAX_MEMBERS:
            raise Unopenable("more than %d members" % MAX_MEMBERS)
        if self.total > MAX_TOTAL_BYTES:
            raise Unopenable("more than %d bytes once opened" % MAX_TOTAL_BYTES)


def _stream(decomp_factory, raw, multi):
    """Decompress `raw` with a capped output, following concatenated streams when `multi`."""
    out, data, size = [], raw, 0
    while True:
        d = decomp_factory()
        chunk = d.decompress(data, _STEP)
        while True:
            size += len(chunk)
            if size > MAX_MEMBER_BYTES:
                raise Unopenable("a member larger than %d bytes once opened" % MAX_MEMBER_BYTES)
            out.append(chunk)
            if d.eof:
                break
            if d.needs_input:
                raise Unopenable("the compressed stream ends early")
            chunk = d.decompress(b"", _STEP)
        rest = d.unused_data
        if multi and rest and rest.strip(b"\x00"):
            data = rest
            continue
        return b"".join(out)


class _Zlib(object):
    """zlib.decompressobj with the bz2/lzma interface `_stream` uses."""

    def __init__(self, wbits):
        self.d = zlib.decompressobj(wbits)
        self.pending = b""
        self.full = False

    def decompress(self, data, max_length):
        chunk = self.d.decompress(self.pending + data, max_length)
        self.pending = self.d.unconsumed_tail
        self.full = len(chunk) >= max_length
        return chunk

    @property
    def eof(self):
        return self.d.eof

    @property
    def needs_input(self):
        return not self.pending and not self.full

    @property
    def unused_data(self):
        return self.d.unused_data


def _gzip(raw):
    return [("", _stream(lambda: _Zlib(31), raw, True))]


def _bzip2(raw):
    return [("", _stream(bz2.BZ2Decompressor, raw, True))]


def _xz(raw):
    return [("", _stream(lzma.LZMADecompressor, raw, True))]


def _zstd(raw):
    try:
        from compression import zstd
    except ImportError:
        raise Unopenable("zstd needs Python 3.14 or later")
    return [("", _stream(zstd.ZstdDecompressor, raw, True))]


def _zip(raw):
    out = []
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            for info in zf.infolist():
                if info.flag_bits & 0x1:
                    raise Unopenable("an encrypted zip member")
                if info.file_size > MAX_MEMBER_BYTES:
                    raise Unopenable("a member larger than %d bytes" % MAX_MEMBER_BYTES)
                data = b"" if info.is_dir() else zf.open(info).read(MAX_MEMBER_BYTES + 1)
                if len(data) > MAX_MEMBER_BYTES:
                    raise Unopenable("a member larger than %d bytes" % MAX_MEMBER_BYTES)
                out.append((info.filename, data))
            if zf.comment:
                out.append(("(zip comment)", zf.comment))
    except (zipfile.BadZipFile, zlib.error, lzma.LZMAError, OSError, EOFError,
            NotImplementedError, RuntimeError) as e:
        raise Unopenable("zip: %s" % type(e).__name__)
    return out


def _tar(raw):
    out = []
    try:
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as tf:
            for m in tf:
                name = m.name
                if m.issym() or m.islnk():
                    name = "%s\n%s" % (m.name, m.linkname)
                if m.size > MAX_MEMBER_BYTES:
                    raise Unopenable("a member larger than %d bytes" % MAX_MEMBER_BYTES)
                data = b""
                if m.isreg():
                    f = tf.extractfile(m)
                    data = f.read() if f is not None else b""
                for key, value in (m.pax_headers or {}).items():
                    name += "\n%s=%s" % (key, value)
                out.append((name, data))
    except (tarfile.TarError, OSError, EOFError) as e:
        raise Unopenable("tar: %s" % type(e).__name__)
    return out


def _woff(raw):
    """WOFF 1.0: each table is zlib-compressed when its compressed length is below its length."""
    out = []
    try:
        num = struct.unpack(">H", raw[12:14])[0]
        meta_off, meta_len, meta_orig, priv_off, priv_len = struct.unpack(">IIIII", raw[24:44])
        for i in range(num):
            tag, off, comp, orig, _ck = struct.unpack(">4sIIII", raw[44 + 20 * i:64 + 20 * i])
            if orig > MAX_MEMBER_BYTES:
                raise Unopenable("a table larger than %d bytes" % MAX_MEMBER_BYTES)
            body = raw[off:off + comp]
            data = zlib.decompressobj().decompress(body, orig + 1) if comp < orig else body
            out.append((tag.decode("latin-1"), data))
        if meta_len:
            out.append(("(metadata)", zlib.decompressobj().decompress(
                raw[meta_off:meta_off + meta_len], MAX_MEMBER_BYTES)))
        if priv_len:
            out.append(("(private)", raw[priv_off:priv_off + priv_len]))
    except (struct.error, zlib.error) as e:
        raise Unopenable("woff: %s" % type(e).__name__)
    return out


def _base128(raw, pos):
    value = 0
    for i in range(5):
        byte = raw[pos]
        pos += 1
        if i == 0 and byte == 0x80:
            raise Unopenable("woff2: a bad number")
        value = (value << 7) | (byte & 0x7F)
        if not byte & 0x80:
            return value, pos
    raise Unopenable("woff2: a bad number")


def _woff2(raw):
    """WOFF2: one brotli stream holds every table. Needs the `brotli` module."""
    try:
        import brotli
    except ImportError:
        raise Unopenable("a WOFF2 font needs the brotli module to open")
    try:
        flavor = raw[4:8]
        num = struct.unpack(">H", raw[12:14])[0]
        comp_size = struct.unpack(">I", raw[20:24])[0]
        meta_off, meta_len, _meta_orig, priv_off, priv_len = struct.unpack(">IIIII", raw[28:48])
        pos = 48
        for _ in range(num):
            flags = raw[pos]
            pos += 1
            if flags & 0x3F == 0x3F:
                pos += 4
            _orig, pos = _base128(raw, pos)
            tag_index = flags & 0x3F
            transform = (flags >> 6) & 0x3
            transformed = (transform != 0) if tag_index not in (10, 11) else (transform == 0)
            if transformed:
                _tlen, pos = _base128(raw, pos)
        if flavor == b"ttcf":
            raise Unopenable("a WOFF2 font collection")
        data = brotli.decompress(raw[pos:pos + comp_size])
        if len(data) > MAX_MEMBER_BYTES:
            raise Unopenable("a font larger than %d bytes" % MAX_MEMBER_BYTES)
        out = [("(tables)", data)]
        if meta_len:
            out.append(("(metadata)", brotli.decompress(raw[meta_off:meta_off + meta_len])))
        if priv_len:
            out.append(("(private)", raw[priv_off:priv_off + priv_len]))
        return out
    except (struct.error, IndexError, brotli.error) as e:
        raise Unopenable("woff2: %s" % type(e).__name__)


_OPEN = {"gzip": _gzip, "bzip2": _bzip2, "xz": _xz, "zstd": _zstd, "zip": _zip, "tar": _tar,
         "woff": _woff, "woff2": _woff2}


def members(raw: bytes, name="", depth=0, budget=None):
    """Yield (name, bytes) for every member of the container `raw`, at every nesting level. The
    name joins the levels with `!` (`pkg.tgz!package/a.md`). Yields nothing when `raw` is not a
    container. Raises Unopenable for a recognised container that cannot be opened within the caps."""
    kind = sniff(raw)
    if kind is None:
        return
    budget = budget or _Budget()
    if depth >= MAX_DEPTH:
        raise Unopenable("containers nested deeper than %d" % MAX_DEPTH)
    opener = _OPEN.get(kind)
    if opener is None:
        raise Unopenable("a %s archive, which the standard library cannot open" % kind)
    try:
        opened = opener(raw)
    except Unopenable:
        raise
    except (EOFError, OSError, ValueError, zlib.error, lzma.LZMAError, MemoryError) as e:
        raise Unopenable("%s: %s" % (kind, type(e).__name__))
    for mname, data in opened:
        budget.take(len(data))
        full = "%s!%s" % (name, mname)
        yield full, data
        yield from members(data, full, depth + 1, budget)
