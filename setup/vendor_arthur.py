#!/usr/bin/env python3
"""vendor_arthur.py - rebuild alpaca/web/vendor/arthur/ from the owner's Arthur web package.

A maintainer tool (t-487). Nothing in it runs at use time. It needs Pillow (the system python3 here
has it; the project .venv does not) and reads the unpacked package as data: it never runs the
package's scripts.

What it writes, under alpaca/web/vendor/arthur/:
  - the runtime scripts and stylesheet, byte for byte: arthur-core.js, arthur-geometry.js,
    arthur-avatar.css (the optional arthur-host-controller.js is not used: the Studio drives Arthur
    from alpaca/web/arthur.js), and arthur-avatar.js with one patch (AVATAR_PATCH, t-595): the avatar
    loads the idle strip before it is ready and every other image the first time a draw needs it,
    keeping the frame on screen until then, where the package loads all 33 images (about 11 MB) first;
  - the original artwork: the 11 assets/strips/<action>.png of package 1, byte for byte, and
    assets/arthur-spritesheet.png (the gaze atlas) with its image data recompressed (zlib level 9,
    memory level 9, in 65536-byte IDAT chunks; every other chunk and the filtered pixel bytes kept).
    The package's own compressed bytes matched a private-term rule of the release leak scan by
    chance (op-008 t-071), and brief 2f allows this re-encode with the pixels unchanged; the script
    checks the pixel bytes read back the same. Then (t-595) slim_atlas cuts it to the rows of the 16
    gaze frames (the avatar draws only those from the atlas; the other rows were 80% of the file and
    decoded to 14 MB) and checks the gaze frames' pixels are unchanged;
  - one horizontal strip per new action, assets/strips/<action>.png, built from the package's own
    normalized 192 x 208 runtime frames assets/frames/<action>/NN.png: the frames are pasted left to
    right on a transparent canvas, pixels unchanged (checked by reading the strip back), so the
    runtime manifest gives a new action the same shape as an original one. The package's full-size
    sheets (assets/new-sheets, about 25 MB) are not shipped (brief section 2e). The frames directory
    already holds each action's chronological runtime slots, so checklist keeps the package's pose
    order [1,1,2,3,4,5] (slots 00 and 01 are the same pose; checked);
  - last (t-595) the atlas becomes a lossless WebP with a .webp ending, a quarter smaller on the wire;
    to_webp checks the decoded RGBA bytes equal the PNG's and deletes the PNG. stripsManifest in
    alpaca/web/arthur.js points the avatar at it, so the manifest below stays as the package wrote it.
    The strips stay PNG: Chrome decodes a lossless WebP strip one level off on edge pixels, which the
    Studio look baseline refuses;
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


# The one change made to the package's runtime (t-595). The package loads the atlas and every action's
# strip before the avatar is ready (about 11 MB), which the hub pays on every first visit. The avatar
# below loads the neutral action's strip before it is ready and every other image the first time a draw
# needs it; until it arrives the canvas keeps the frame it shows. Each pair is (old, new) and must
# match the package's file exactly once, so a new package that moves this code stops the script.
AVATAR_PATCH = (
    ('this._images=new Map();this._pendingLoads=new Set();',
     'this._images=new Map();this._pendingLoads=new Set();this._loads=new Map();'),
    (r"""    async _loadImages(){
      const paths=new Set([this.manifest.atlas.image,...Object.values(this.manifest.actions).map(a=>a.image)]);
      await Promise.all([...paths].map(path=>new Promise((resolve,reject)=>{
""",
     r"""    async _loadImages(){
      const m=this.manifest;
      await this._load(m.actions[this._resolveAction(m.neutralAction)].image);
    }
    _load(path){
      if(this._loads.has(path)) return this._loads.get(path);
      const load=new Promise((resolve,reject)=>{
"""),
    (r"""        image.onload=()=>{
          if(!this._destroyed) this._images.set(path,image);
          finish();
        };
""",
     r"""        image.onload=()=>{
          if(this._destroyed) return finish();
          try{this._check(path,image);}catch(error){return finish(error);}
          this._images.set(path,image);
          finish();
        };
"""),
    (r"""        image.src=new URL(path,this.assetBaseURL).href;
      })));
      if(this._destroyed) return;
      const m=this.manifest;
      const atlas=this._images.get(m.atlas.image);
      if(atlas.naturalWidth!==m.atlas.width||atlas.naturalHeight!==m.atlas.height)
        throw new Error("Arthur atlas dimensions do not match its manifest");
      for(const [name,action] of Object.entries(m.actions)){
        const image=this._images.get(action.image);
        Geometry.validate(action,m.width,m.height);
        const size=Geometry.sourceSize(action,m.width,m.height);
        if(image.naturalWidth!==size.width||image.naturalHeight!==size.height)
          throw new Error("Arthur strip dimensions do not match: "+name);
      }
    }
""",
     r"""        image.src=new URL(path,this.assetBaseURL).href;
      });
      this._loads.set(path,load);
      return load;
    }
    _check(path,image){
      const m=this.manifest;
      if(path===m.atlas.image&&(image.naturalWidth!==m.atlas.width||image.naturalHeight!==m.atlas.height))
        throw new Error("Arthur atlas dimensions do not match its manifest");
      for(const [name,action] of Object.entries(m.actions)){
        if(action.image!==path) continue;
        Geometry.validate(action,m.width,m.height);
        const size=Geometry.sourceSize(action,m.width,m.height);
        if(image.naturalWidth!==size.width||image.naturalHeight!==size.height)
          throw new Error("Arthur strip dimensions do not match: "+name);
      }
    }
    _ready(path){
      if(this._images.has(path)) return true;
      if(!this._loads.has(path)) this._want(path);
      return false;
    }
    _want(path){
      this._load(path).then(()=>this._draw(),()=>{setTimeout(()=>this._loads.delete(path),5000);});
    }
"""),
    (r"""Core.canUseGaze(this._player.action,m.neutralAction);
      const kind""",
     r"""Core.canUseGaze(this._player.action,m.neutralAction)&&this._ready(m.atlas.image);
      const kind"""),
    (r"""        image=this._images.get(action.image);
        const rect""",
     r"""        image=this._images.get(action.image);
        if(!image){this._ready(action.image);return;}
        const rect"""),
)


def patch_avatar(path):
    text = Path(path).read_bytes().decode("utf-8")
    eol = "\r\n" if "\r\n" in text else "\n"        # the package's file has CRLF line ends
    for old, new in AVATAR_PATCH:
        old, new = old.replace("\n", eol), new.replace("\n", eol)
        if text.count(old) != 1:
            raise SystemExit("%s: the package's avatar code changed; update AVATAR_PATCH:\n%s" % (path, old))
        text = text.replace(old, new)
    Path(path).write_bytes(text.encode("utf-8"))


def slim_atlas(path, frames):
    """Cut the atlas down to the rows that hold the gaze frames, in place (t-595). The web avatar draws
    only the 16 gaze frames from it (its actions come from the strips), and the other rows made the file
    2.4 MB and the decoded image 14 MB: decoding that on first use stalled Chrome's main thread for over
    100 ms at 6x CPU slowdown. The cut keeps the full width, so a frame sits at (x, y - top) with top
    the smallest frame y; alpaca/web/arthur.js (stripsManifest) hands the avatar that geometry. The
    gaze frames are checked to read back with their pixels unchanged."""
    from PIL import Image
    atlas = Image.open(path)
    atlas.load()
    atlas = atlas.convert("RGBA")
    top, bottom = min(f["y"] for f in frames), max(f["y"] + f["height"] for f in frames)
    atlas.crop((0, top, atlas.width, bottom)).save(path, optimize=True)
    back = Image.open(path)
    back.load()
    back = back.convert("RGBA")
    if back.size != (atlas.width, bottom - top):
        raise SystemExit("%s: the cut atlas is %s, not %dx%d" % (path, back.size, atlas.width, bottom - top))
    for f in frames:
        box = (f["x"], f["y"], f["x"] + f["width"], f["y"] + f["height"])
        if back.crop((f["x"], f["y"] - top, f["x"] + f["width"], f["y"] - top + f["height"])).tobytes() != atlas.crop(box).tobytes():
            raise SystemExit("%s: gaze frame %s differs after the cut" % (path, f["index"]))
    return hashlib.sha256(b"".join(atlas.crop((f["x"], f["y"], f["x"] + f["width"], f["y"] + f["height"])).tobytes()
                                   for f in frames)).hexdigest()


def to_webp(path):
    """Replace a PNG with a lossless WebP of the same name and a .webp ending (t-595): about a quarter
    smaller on the wire. exact=True keeps the colour of fully transparent pixels, so the decoded RGBA
    bytes are checked equal to the PNG's. alpaca/web/arthur.js (stripsManifest) points the avatar at it.
    Only the gaze atlas uses it: the cursor look is not in the Studio baseline."""
    from PIL import Image
    path = Path(path)
    png = Image.open(path)
    png.load()
    png = png.convert("RGBA")
    out = path.with_suffix(".webp")
    png.save(out, "WEBP", lossless=True, quality=100, method=6, exact=True)
    back = Image.open(out)
    back.load()
    if back.convert("RGBA").tobytes() != png.tobytes():
        raise SystemExit("%s does not read back to the pixels of %s" % (out, path))
    path.unlink()
    return out.stat().st_size


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
    gaze_pixels = slim_atlas(OUT / "assets" / source["atlas"]["image"], source["gaze"]["frames"])
    shutil.copyfile(pkg / "assets" / "avatar-manifest.js", OUT / "assets" / "avatar-manifest.js")
    patch_avatar(OUT / "arthur-avatar.js")
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
    to_webp(OUT / "assets" / source["atlas"]["image"])
    shipped = (OUT / "assets" / "avatar-manifest.js").read_text(encoding="utf-8")
    for ident in source["actions"]:
        if '"%s": {' % ident not in shipped:
            raise SystemExit("assets/avatar-manifest.js does not list %s, which avatar-manifest.json does" % ident)
    total = sum(p.stat().st_size for p in OUT.rglob("*") if p.is_file())
    print("package version %s%s" % (version, (", archive sha256 %s" % sha(args.archive)) if args.archive else ""))
    atlas_file = (OUT / "assets" / source["atlas"]["image"]).with_suffix(".webp")      # to_webp removed the PNG
    print("atlas recompressed, pixel bytes sha256 %s; as WebP, file sha256 %s" % (atlas_pixels, sha(atlas_file)))
    print("atlas slimmed to its gaze frames, their pixels sha256 %s" % gaze_pixels)
    print("original actions copied byte for byte: %d (%s)" % (len(original), ", ".join(original)))
    print("new actions built from 192x208 frames: %d" % len(converted))
    for ident, count, size, digest, repeats in converted:
        print("  %-16s %d frames %8d bytes sha256 %s%s" % (ident, count, size, digest,
                                                           (" (slot %s repeats the pose before it)" % repeats) if repeats else ""))
    print("vendor/arthur total %d bytes" % total)
    return 0


if __name__ == "__main__":
    sys.exit(main())
