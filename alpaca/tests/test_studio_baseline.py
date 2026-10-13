"""t-474: the Studio look stays the same. Every page and view, light and dark, 1440 and 390 px wide, is
captured from the fixture project and compared pixel for pixel with the baseline stored in
alpaca/tests/baseline/studio/ (taken on main before the template refactor, t-476).

Recapture the baseline (a decision, recorded in the task's proof): .venv/bin/python
alpaca/tests/studio_shots.py update. The capture and compare are explained in
alpaca/tests/studio_shots.py and alpaca/tests/studio/capture.mjs; the threshold in studio/compare.mjs.
The browser test skips without node and google-chrome, and when the look differs on a Chrome build other
than the one the baseline names (manifest.json capture.chrome): the zero-pixel bar holds per build."""
import json
import os
import re
import shutil
import subprocess
import zlib
import struct

import pytest

from alpaca.tests import studio_shots

HERE = os.path.dirname(os.path.abspath(__file__))
NODE = shutil.which("node")
VIEWS = ["login", "hub", "cockpit", "overview", "roadmap", "roadmap-mission", "roadmap-finished", "roadmap-finished-op",
         "inflight", "research", "work",
         "activity", "messages", "library", "records", "records-decisions", "records-proofs", "records-wiki", "records-vault",
         "sessions", "sessions-range", "vcs"]


def _manifest():
    with open(os.path.join(studio_shots.BASELINE, "manifest.json"), encoding="utf-8") as fh:
        return json.load(fh)


def test_the_baseline_covers_every_page_theme_and_width():
    images = _manifest()["images"]
    want = {"%s-%d-%s" % (v, w, t) for v in VIEWS for w in (1440, 390) for t in ("light", "dark")}
    assert set(images) == want
    for name, meta in images.items():
        path = os.path.join(studio_shots.BASELINE, name + ".png")
        assert os.path.getsize(path) == meta["bytes"], name
        assert meta["ready"] and meta["stable"] and not meta["errors"], name
        with open(path, "rb") as fh:
            head = fh.read(24)
        assert struct.unpack(">II", head[16:24]) == (meta["width"], meta["height"]), name   # viewport only, scale 1


def _png(path, width, height, pixel):
    raw = b"".join(b"\x00" + b"".join(bytes(pixel(x, y)) for x in range(width)) for y in range(height))
    chunk = lambda kind, data: struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    with open(path, "wb") as fh:
        fh.write(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
                 + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def test_the_compare_script_finds_one_changed_pixel_and_skips_masks(tmp_path):
    if not NODE:
        pytest.skip("node is required for the compare script")
    base, cap = tmp_path / "base", tmp_path / "cap"
    base.mkdir()
    cap.mkdir()
    grey = lambda x, y: (200, 200, 200)
    _png(base / "a.png", 20, 10, grey)
    _png(cap / "a.png", 20, 10, lambda x, y: (201, 200, 200) if (x, y) == (3, 4) else grey(x, y))
    _png(base / "b.png", 20, 10, grey)
    _png(cap / "b.png", 20, 10, lambda x, y: (0, 0, 0) if x >= 15 else grey(x, y))
    entry = lambda masks=(): {"width": 20, "height": 10, "masks": [list(m) for m in masks]}
    (base / "manifest.json").write_text(json.dumps({"images": {"a": entry(), "b": entry([(15, 0, 5, 10)])}}))
    (cap / "manifest.json").write_text(json.dumps({"images": {"a": entry(), "b": entry()}}))
    code, text = studio_shots.compare(str(cap), str(tmp_path / "out"), baseline=str(base))
    summary = json.loads((tmp_path / "out" / "summary.json").read_text())
    rows = {r["name"]: r for r in summary["rows"]}
    assert code == 1 and rows["a"]["status"] == "changed" and rows["a"]["differing"] == 1, text
    assert rows["a"]["box"] == [3, 4, 1, 1] and (tmp_path / "out" / "diff" / "a.png").is_file()
    assert rows["b"]["status"] == "same" and rows["b"]["masked"] == 50          # the masked strip is not compared
    code, _ = studio_shots.compare(str(cap), str(tmp_path / "out2"), baseline=str(base), max_pixels=1)
    assert code == 0                                                             # the stated threshold is the bar


def test_the_studio_looks_like_the_baseline(tmp_path):
    node, chrome, why = studio_shots.tools()
    if not node:
        pytest.skip(why)
    shots, out = str(tmp_path / "capture"), str(tmp_path / "compare")
    code, text = studio_shots.capture(shots, work=str(tmp_path / "work"))
    assert code == 0, text[-4000:]
    code, text = studio_shots.compare(shots, out)
    print(text)
    taken, here = _manifest()["capture"].get("chrome"), _chrome_of(shots)
    if code and taken != here:
        # Zero differing pixels holds for the Chrome build the baseline was taken with. Another build
        # rasterizes a few pixels apart (the hosted runner: 51 pixels in 4 of 60 images, op-008
        # t-571), so the bar cannot be checked here; the numbers stay in the reason.
        pytest.skip("the baseline was taken with %s, this host runs %s: %s"
                    % (taken, here, text.splitlines()[0]))
    assert code == 0, "the Studio look changed; diff images in %s\n%s" % (os.path.join(out, "diff"), text[-6000:])


def _chrome_of(capture_dir):
    with open(os.path.join(capture_dir, "manifest.json"), encoding="utf-8") as fh:
        return json.load(fh)["capture"].get("chrome")


def test_the_baseline_names_its_chrome_build():
    """The capture records the browser build (studio/capture.mjs, Browser.getVersion), so a compare
    on another build is told apart from a change of the look."""
    assert re.fullmatch(r"(Headless)?Chrome/\d+\.\d+\.\d+\.\d+", _manifest()["capture"]["chrome"])
