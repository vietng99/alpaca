#!/usr/bin/env python3
"""vendor_arthur.py - rebuild alpaca/web/vendor/arthur/ from the owner's Arthur web package.

A maintainer tool (t-487). Nothing in it runs at use time. It needs Pillow (the system python3 here
has it; the project .venv does not) and reads the unpacked package as data: it never runs the
package's scripts.

What it writes, under alpaca/web/vendor/arthur/:
  - the runtime scripts and stylesheet, byte for byte: arthur-core.js, arthur-geometry.js,
    arthur-avatar.js, arthur-avatar.css (the optional arthur-host-controller.js is not used: the
    Studio drives Arthur from alpaca/web/arthur.js);
  - the original artwork: the 11 assets/strips/<action>.png of package 1, byte for byte, and
    assets/arthur-spritesheet.png (the gaze atlas) with its image data recompressed (zlib level 9,
    memory level 9, in 65536-byte IDAT chunks; every other chunk and the filtered pixel bytes kept).
    The package's own compressed bytes matched a private-term rule of the release leak scan by
    chance (op-008 t-071), and brief 2f allows this re-encode with the pixels unchanged; the script
    checks the pixel bytes read back the same;
  - one horizontal strip per new action, assets/strips/<action>.png, built from the package's own
    normalized 192 x 208 runtime frames assets/frames/<action>/NN.png: the frames are pasted left to
    right on a transparent canvas, pixels unchanged (checked by reading the strip back), so the
    runtime manifest gives a new action the same shape as an original one. The package's full-size
    sheets (assets/new-sheets, about 25 MB) are not shipped (brief section 2e). The frames directory
    already holds each action's chronological runtime slots, so checklist keeps the package's pose
    order [1,1,2,3,4,5] (slots 00 and 01 are the same pose; checked);
  - assets/avatar-manifest.js: the package's own file, byte for byte. Its new actions still point at
    the full-size sheets; the Studio reads it through stripsManifest (alpaca/web/arthur.js), which
    points each new action at its strip and keeps frames, durationsMs, loop, label and trigger. The
    script checks the .js and .json manifests list the same actions and that each new one has a strip.

Usage:
    python3 -I setup/vendor_arthur.py <unpacked Arthur-Web-Package-2 directory> [--archive <zip>]

--archive only hashes the zip for the summary that alpaca/web/vendor/README.md records.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import struct
import sys
import zlib
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "alpaca" / "web" / "vendor" / "arthur"
RUNTIME = ("arthur-core.js", "arthur-geometry.js", "arthur-avatar.js", "arthur-avatar.css")
WIDTH, HEIGHT = 192, 208


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _chunks(data):
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise SystemExit("not a PNG")
    i = 8
    while i < len(data):
        size = struct.unpack(">I", data[i:i + 4])[0]
        yield data[i + 4:i + 8], data[i + 8:i + 8 + size]
        i += 12 + size


def _chunk(kind, body):
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)


def recompress_atlas(src, out):
    """Write src to out with its IDAT stream recompressed; the filtered pixel bytes stay the same."""
    data = Path(src).read_bytes()
    parts = list(_chunks(data))
    raw = zlib.decompress(b"".join(body for kind, body in parts if kind == b"IDAT"))
    packer = zlib.compressobj(9, zlib.DEFLATED, 15, 9)
    stream = packer.compress(raw) + packer.flush()
    first = next(i for i, (kind, _) in enumerate(parts) if kind == b"IDAT")
    rest = [p for p in parts if p[0] != b"IDAT"]
    body = b"".join(_chunk(kind, b) for kind, b in rest[:first])
    body += b"".join(_chunk(b"IDAT", stream[i:i + 65536]) for i in range(0, len(stream), 65536))
    body += b"".join(_chunk(kind, b) for kind, b in rest[first:])
    Path(out).write_bytes(data[:8] + body)
    back = zlib.decompress(b"".join(b for kind, b in _chunks(Path(out).read_bytes()) if kind == b"IDAT"))
    if back != raw:
        raise SystemExit("%s: the recompressed atlas does not read back to the same pixels" % out)
    return hashlib.sha256(raw).hexdigest()


def build_strip(frames_dir, count, out):
    from PIL import Image
    tiles = []
    for i in range(count):
        tile = Image.open(frames_dir / ("%02d.png" % i))
        tile.load()
        if tile.size != (WIDTH, HEIGHT) or tile.mode != "RGBA":
            raise SystemExit("%s/%02d.png is %s %s, not %dx%d RGBA" % (frames_dir, i, tile.size, tile.mode, WIDTH, HEIGHT))
        tiles.append(tile)
    strip = Image.new("RGBA", (WIDTH * count, HEIGHT), (0, 0, 0, 0))
    for i, tile in enumerate(tiles):
        strip.paste(tile, (i * WIDTH, 0))
    strip.save(out, optimize=True)
    back = Image.open(out)
    back.load()
    if back.mode != "RGBA" or back.size != (WIDTH * count, HEIGHT):
        raise SystemExit("%s did not read back as RGBA %dx%d" % (out, WIDTH * count, HEIGHT))
    for i, tile in enumerate(tiles):
        if back.crop((i * WIDTH, 0, (i + 1) * WIDTH, HEIGHT)).tobytes() != tile.tobytes():
            raise SystemExit("%s frame %d differs from its source frame" % (out, i))
    return tiles


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("package")
    ap.add_argument("--archive")
    args = ap.parse_args(argv)
    pkg = Path(args.package).resolve()
    source = json.loads((pkg / "assets" / "avatar-manifest.json").read_text(encoding="utf-8"))
    version = json.loads((pkg / "package.json").read_text(encoding="utf-8")).get("version")
    (OUT / "assets" / "strips").mkdir(parents=True, exist_ok=True)
    for name in RUNTIME:
        shutil.copyfile(pkg / "src" / name, OUT / name)
    atlas_pixels = recompress_atlas(pkg / "assets" / source["atlas"]["image"], OUT / "assets" / source["atlas"]["image"])
    shutil.copyfile(pkg / "assets" / "avatar-manifest.js", OUT / "assets" / "avatar-manifest.js")
    original, converted = [], []
    for ident, action in source["actions"].items():
        image = str(action["image"])
        if image.startswith("strips/"):
            shutil.copyfile(pkg / "assets" / image, OUT / "assets" / image)
            original.append(ident)
            continue
        if not image.startswith("new-sheets/"):
            raise SystemExit("unexpected image for %s: %s" % (ident, image))
        count = len(action["frames"])
        if action["frames"] != list(range(count)):
            raise SystemExit("%s: frames are not the slots 0..%d" % (ident, count - 1))
        out = OUT / "assets" / "strips" / ("%s.png" % ident)
        tiles = build_strip(pkg / "assets" / "frames" / ident, count, out)
        rects = action.get("frameRects") or []
        for i in range(1, count):
            # a slot that repeats the previous source pose (checklist's [1,1,...]) must repeat its pixels
            if rects and rects[i] == rects[i - 1] and tiles[i].tobytes() != tiles[i - 1].tobytes():
                raise SystemExit("%s: slots %d and %d share a pose but not their pixels" % (ident, i - 1, i))
        repeats = [i for i in range(1, count) if rects and rects[i] == rects[i - 1]]
        converted.append((ident, count, out.stat().st_size, sha(out), repeats))
    shipped = (OUT / "assets" / "avatar-manifest.js").read_text(encoding="utf-8")
    for ident in source["actions"]:
        if '"%s": {' % ident not in shipped:
            raise SystemExit("assets/avatar-manifest.js does not list %s, which avatar-manifest.json does" % ident)
    total = sum(p.stat().st_size for p in OUT.rglob("*") if p.is_file())
    print("package version %s%s" % (version, (", archive sha256 %s" % sha(args.archive)) if args.archive else ""))
    print("atlas recompressed, pixel bytes sha256 %s, file sha256 %s" % (atlas_pixels, sha(OUT / "assets" / source["atlas"]["image"])))
    print("original actions copied byte for byte: %d (%s)" % (len(original), ", ".join(original)))
    print("new actions built from 192x208 frames: %d" % len(converted))
    for ident, count, size, digest, repeats in converted:
        print("  %-16s %d frames %8d bytes sha256 %s%s" % (ident, count, size, digest,
                                                           (" (slot %s repeats the pose before it)" % repeats) if repeats else ""))
    print("vendor/arthur total %d bytes" % total)
    return 0


if __name__ == "__main__":
    sys.exit(main())
