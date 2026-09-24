"""containers.py - open the compressed files a push carries, so the barrier can read inside them.

A term inside a gzip, tar, zip, xz or bzip2 file, or inside a font's compressed tables, is not in
the file's raw bytes: the byte lane cannot see it (barrier review 3, B4). This module recognises a
container by its magic bytes (never by its name), opens it with the standard library, and yields
every member with its name, recursing into members that are containers themselves. A zip is also
recognised by its end record, so a zip behind a prefix (a Python zipapp, a self-extracting file) is
opened too (review 4, N07).

Caps keep a hostile or huge archive from exhausting memory: a nesting depth, a member count, a size
per member and a total size per top-level file. Each cap is charged BEFORE a member is read (its
declared size) and while a stream is read (review 4, N04, N05), and the openers are generators, so
one member at a time is held. A container this module recognises but cannot open within the caps (a
damaged stream, an encrypted zip member, a WOFF2 font without the `brotli` module, a 7z, rar, ar,
lzip, lz4 or compress archive) raises `Unopenable`; the barrier then refuses the push unless an
allow entry names that blob's digest with a reason. With a `skipped` list, a MEMBER that cannot be
opened is recorded there and its siblings are still read (review 4, N12).

Honest limit: formats with no magic bytes at the start (a raw brotli or raw deflate stream, a disk
image), and compressed streams inside documents (PNG image data, PDF streams), are not opened. The
byte lane still reads their raw bytes. docs/shipping.md lists these gaps.
"""
from __future__ import annotations

import bisect
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
_ZLIB_TRIAL = 4096


class Unopenable(Exception):
    """A recognised container that could not be opened within the caps."""


class OverBudget(Unopenable):
    """The member count or the total size cap of one top-level file was reached: nothing more of
    that file is read."""


#: containers recognised by their magic bytes that the standard library cannot open: refused.
_UNOPENABLE = (
    (b"7z\xbc\xaf\x27\x1c", "7z"),
    (b"Rar!\x1a\x07", "rar"),
    (b"!<arch>\n", "ar"),
    (b"LZIP\x01", "lzip"),
    (b"\x04\x22\x4d\x18", "lz4"),
    (b"\x1f\x9d", "compress (.Z)"),
    (b"\x89LZO\x00\r\n\x1a\n", "lzop"),
    (b"\xed\xab\xee\xdb", "rpm"),
    (b"MSCF\x00\x00\x00\x00", "cab"),
    (b"xar!", "xar"),
    (b"hsqs", "squashfs"),
)
_HEX = frozenset(b"0123456789abcdefABCDEF")
_OCT = frozenset(b"01234567")


def _cpio(raw):
    if raw[:6] in (b"070701", b"070702"):
        return len(raw) >= 110 and all(c in _HEX for c in raw[6:110])
    if raw[:6] == b"070707":
        return len(raw) >= 76 and all(c in _OCT for c in raw[6:76])
    return False


def _zlib_head(raw):
    """True when `raw` starts with a zlib header and its first bytes decode as a deflate stream.
    The two header bytes alone also start ordinary text (`x^`, `HK`), so a trial decode decides."""
    if len(raw) < 3:
        return False
    cmf, flg = raw[0], raw[1]
    if cmf & 0x0F != 8 or cmf >> 4 > 7 or flg & 0x20 or (cmf * 256 + flg) % 31:
        return False
    try:
        return bool(zlib.decompressobj().decompress(raw[:_ZLIB_TRIAL], _STEP))
    except zlib.error:
        return False


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
    for magic, kind in _UNOPENABLE:
        if raw[:len(magic)] == magic:
            return kind
    if _cpio(raw):
        return "cpio"
    if len(raw) > 32774 and raw[32769:32774] == b"CD001":
        return "iso9660"
    if _zlib_head(raw):
        return "zlib"
    if len(raw) >= 22 and b"PK\x05\x06" in raw[-65557:]:
        try:
            if zipfile.is_zipfile(io.BytesIO(raw)):
                return "zip"
        except Exception:
            pass
    return None


class _Budget(object):
    """The member count and total size of one top-level file, charged before a member is read."""

    def __init__(self):
        self.members = 0
        self.total = 0

    def take(self, n):
        """One more member of `n` bytes (its declared size)."""
        self.members += 1
        if self.members > MAX_MEMBERS:
            raise OverBudget("more than %d members" % MAX_MEMBERS)
        self.grow(n)

    def grow(self, n):
        """`n` more bytes opened."""
        self.total += n
        if self.total > MAX_TOTAL_BYTES:
            raise OverBudget("more than %d bytes once opened" % MAX_TOTAL_BYTES)


def _too_big():
    return Unopenable("a member larger than %d bytes once opened" % MAX_MEMBER_BYTES)


def _stream(decomp_factory, raw, multi, budget):
    """Decompress `raw` with a capped output, following concatenated streams when `multi`."""
    out, data, size = [], raw, 0
    budget.take(0)
    while True:
        d = decomp_factory()
        chunk = d.decompress(data, _STEP)
        while True:
            size += len(chunk)
            if size > MAX_MEMBER_BYTES:
                raise _too_big()
            budget.grow(len(chunk))
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


def _gzip(raw, budget):
    yield "", _stream(lambda: _Zlib(31), raw, True, budget)


def _bzip2(raw, budget):
    yield "", _stream(bz2.BZ2Decompressor, raw, True, budget)


def _xz(raw, budget):
    yield "", _stream(lzma.LZMADecompressor, raw, True, budget)


def _zstd(raw, budget):
    try:
        from compression import zstd
    except ImportError:
        raise Unopenable("zstd needs Python 3.14 or later")
    yield "", _stream(zstd.ZstdDecompressor, raw, True, budget)


def _zlib(raw, budget):
    """A zlib stream, read the way a lenient decoder reads it: what decodes is the member, whether
    or not the stream reaches its end, so what any decoder can get out of it is scanned."""
    budget.take(0)
    d, out, size = zlib.decompressobj(), [], 0
    for i in range(0, len(raw), _ZLIB_TRIAL):
        try:
            chunk = d.decompress(raw[i:i + _ZLIB_TRIAL], _STEP)
            while True:
                size += len(chunk)
                if size > MAX_MEMBER_BYTES:
                    raise _too_big()
                budget.grow(len(chunk))
                out.append(chunk)
                if not d.unconsumed_tail:
                    break
                chunk = d.decompress(d.unconsumed_tail, _STEP)
        except zlib.error:
            break
        if d.eof:
            break
    yield "", b"".join(out)


def _local_ranges(raw, infos):
    """(starts, ranges) of the local entries the central directory lists: each entry's header
    offset and the span of its header and data."""
    starts, ranges = set(), []
    for info in infos:
        at = info.header_offset
        if raw[at:at + 4] != b"PK\x03\x04":
            raise Unopenable("zip: an entry points at no local header")
        n, m = struct.unpack("<HH", raw[at + 26:at + 30])
        starts.add(at)
        ranges.append((at, at + 30 + n + m + info.compress_size))
    ranges.sort()
    return starts, ranges


def _orphans(raw, infos):
    """True when a local entry header sits outside every entry the central directory lists: a
    reader of the central directory (zipfile) would never see that entry (review 4, N10)."""
    starts, ranges = _local_ranges(raw, infos)
    heads = [a for a, _b in ranges]
    pos = raw.find(b"PK\x03\x04")
    while pos >= 0:
        if pos not in starts:
            k = bisect.bisect_right(heads, pos) - 1
            if k < 0 or pos >= ranges[k][1]:
                return True
        pos = raw.find(b"PK\x03\x04", pos + 1)
    return False


def _zip(raw, budget):
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw))
    except (zipfile.BadZipFile, OSError, EOFError, ValueError, NotImplementedError) as e:
        raise Unopenable("zip: %s" % type(e).__name__)
    with zf:
        infos = zf.infolist()
        try:
            if _orphans(raw, infos):
                raise Unopenable("zip: a local entry the central directory does not list")
        except struct.error:
            raise Unopenable("zip: a damaged local header")
        for info in infos:
            if info.flag_bits & 0x1:
                budget.take(0)
                yield info.filename, Unopenable("an encrypted zip member")
                continue
            if info.file_size > MAX_MEMBER_BYTES:
                budget.take(0)
                yield info.filename, _too_big()
                continue
            budget.take(info.file_size)
            if info.is_dir():
                yield info.filename, b""
                continue
            try:
                with zf.open(info) as fh:
                    data = fh.read(MAX_MEMBER_BYTES + 1)
            except (zipfile.BadZipFile, zlib.error, lzma.LZMAError, OSError, EOFError,
                    NotImplementedError, RuntimeError, ValueError) as e:
                yield info.filename, Unopenable("zip: %s" % type(e).__name__)
                continue
            if len(data) > MAX_MEMBER_BYTES:
                data = None
                yield info.filename, _too_big()
                continue
            yield info.filename, data
            data = None
        if zf.comment:
            budget.take(len(zf.comment))
            yield "(zip comment)", zf.comment


def _tar(raw, budget):
    try:
        tf = tarfile.open(fileobj=io.BytesIO(raw), mode="r:")
    except (tarfile.TarError, OSError, EOFError) as e:
        raise Unopenable("tar: %s" % type(e).__name__)
    with tf:
        while True:
            try:
                m = tf.next()
            except (tarfile.TarError, OSError, EOFError) as e:
                raise Unopenable("tar: %s" % type(e).__name__)
            if m is None:
                return
            tf.members = []                  # keep no list of every member read so far
            name = m.name
            if m.issym() or m.islnk():
                name = "%s\n%s" % (m.name, m.linkname)
            for key, value in (m.pax_headers or {}).items():
                name += "\n%s=%s" % (key, value)
            if m.size > MAX_MEMBER_BYTES:
                budget.take(0)
                yield name, _too_big()
                continue
            budget.take(m.size if m.isreg() else 0)
            data = b""
            if m.isreg():
                try:
                    f = tf.extractfile(m)
                    data = f.read(MAX_MEMBER_BYTES + 1) if f is not None else b""
                except (tarfile.TarError, OSError, EOFError) as e:
                    raise Unopenable("tar: %s" % type(e).__name__)
            yield name, data
            data = None


def _inflate_exact(body, orig):
    """A WOFF zlib stream that must open to exactly `orig` bytes: longer or shorter is refused, so
    nothing can sit past the declared length (review 4, N11)."""
    d = zlib.decompressobj()
    data = d.decompress(body, orig + 1)
    if len(data) != orig or d.unconsumed_tail or not d.eof:
        raise Unopenable("woff: a table that does not open to its declared length")
    return data


def _woff(raw, budget):
    """WOFF 1.0: each table is zlib-compressed when its compressed length is below its length."""
    try:
        num = struct.unpack(">H", raw[12:14])[0]
        meta_off, meta_len, meta_orig, priv_off, priv_len = struct.unpack(">IIIII", raw[24:44])
        for i in range(num):
            tag, off, comp, orig, _ck = struct.unpack(">4sIIII", raw[44 + 20 * i:64 + 20 * i])
            if orig > MAX_MEMBER_BYTES or meta_orig > MAX_MEMBER_BYTES:
                raise _too_big()
            budget.take(orig)
            body = raw[off:off + comp]
            yield tag.decode("latin-1"), _inflate_exact(body, orig) if comp < orig else body
        if meta_len:
            budget.take(meta_orig)
            yield "(metadata)", _inflate_exact(raw[meta_off:meta_off + meta_len], meta_orig)
        if priv_len:
            budget.take(priv_len)
            yield "(private)", raw[priv_off:priv_off + priv_len]
    except (struct.error, zlib.error) as e:
        raise Unopenable("woff: %s" % type(e).__name__)


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


def _unbrotli(brotli, data, budget):
    """Decompress a brotli stream with a capped output, a step at a time. A `brotli` module too
    old to cap its output (before 1.1) is refused rather than let it open without a cap."""
    d = brotli.Decompressor()
    if not hasattr(d, "can_accept_more_data") or not hasattr(d, "is_finished"):
        raise Unopenable("the brotli module is too old to open a WOFF2 font within the caps")
    budget.take(0)
    out, size = [], 0
    chunk = d.process(data, output_buffer_limit=_STEP)
    while True:
        size += len(chunk)
        if size > MAX_MEMBER_BYTES:
            raise _too_big()
        budget.grow(len(chunk))
        out.append(chunk)
        if d.is_finished():
            return b"".join(out)
        if d.can_accept_more_data():
            raise Unopenable("woff2: the compressed stream ends early")
        chunk = d.process(b"", output_buffer_limit=_STEP)


def _woff2(raw, budget):
    """WOFF2: one brotli stream holds every table. Needs the `brotli` module (1.1 or later)."""
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
        tables = _unbrotli(brotli, raw[pos:pos + comp_size], budget)
        meta = _unbrotli(brotli, raw[meta_off:meta_off + meta_len], budget) if meta_len else None
    except Unopenable:
        raise
    except Exception as e:                   # struct.error, IndexError, brotli.error, TypeError
        raise Unopenable("woff2: %s" % type(e).__name__)
    yield "(tables)", tables
    tables = None
    if meta is not None:
        yield "(metadata)", meta
    if priv_len:
        budget.take(priv_len)
        yield "(private)", raw[priv_off:priv_off + priv_len]


_OPEN = {"gzip": _gzip, "bzip2": _bzip2, "xz": _xz, "zstd": _zstd, "zip": _zip, "tar": _tar,
         "woff": _woff, "woff2": _woff2, "zlib": _zlib}

_ERRORS = (EOFError, OSError, ValueError, zlib.error, lzma.LZMAError, MemoryError, struct.error,
           IndexError, tarfile.TarError, zipfile.BadZipFile)


def _skip(skipped, name, err):
    if skipped is None or isinstance(err, OverBudget):
        raise err
    skipped.append((name, str(err)))


def members(raw: bytes, name="", depth=0, budget=None, skipped=None):
    """Yield (name, bytes) for every member of the container `raw`, at every nesting level. The
    name joins the levels with `!` (`pkg.tgz!package/a.md`). Yields nothing when `raw` is not a
    container. Raises Unopenable for a recognised container that cannot be opened within the caps.

    `skipped`: when a list, a member that cannot be opened (a nested container this module cannot
    open, an encrypted zip member, a member over the size cap) is appended to it as (name, why),
    its name is still yielded (with no bytes), and its siblings are still read; only the file
    itself, or the member count and total caps, raise. When None, the first such member raises."""
    kind = sniff(raw)
    if kind is None:
        return
    budget = budget or _Budget()
    if depth >= MAX_DEPTH:
        raise Unopenable("containers nested deeper than %d" % MAX_DEPTH)
    opener = _OPEN.get(kind)
    if opener is None:
        raise Unopenable("a %s archive, which the standard library cannot open" % kind)
    it = opener(raw, budget)
    while True:
        try:
            item = next(it)
        except StopIteration:
            return
        except Unopenable:
            raise
        except _ERRORS as e:
            raise Unopenable("%s: %s" % (kind, type(e).__name__))
        mname, data = item
        item = None
        full = "%s!%s" % (name, mname)
        if isinstance(data, Unopenable):
            _skip(skipped, full, data)
            yield full, b""
            continue
        yield full, data
        try:
            yield from members(data, full, depth + 1, budget, skipped)
        except Unopenable as e:
            _skip(skipped, full, e)
        data = None
