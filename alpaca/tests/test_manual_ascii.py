"""M4.16 clause 2 proof: docs/manual.html is pure ASCII.

The shipped page must contain zero bytes above 0x7F, and reading it as latin-1 must equal its
ascii decode. Every control asserts a POSITIVE and a NEGATIVE path, so the ASCII check is never a
tautological pass: a page carrying one non-ASCII byte (an em dash, an arrow) must be caught.
"""
import importlib.util
import os

from alpaca.tests.conftest import REPO

HTML = os.path.join(REPO, "docs", "manual.html")


def _load():
    path = os.path.join(REPO, "setup", "build_manual_html.py")
    spec = importlib.util.spec_from_file_location("alpaca_build_manual_html_ascii", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ------------------------------------------------------------------ the shipped file (positive)
def test_shipped_manual_has_no_byte_above_0x7f():
    with open(HTML, "rb") as fh:
        raw = fh.read()
    assert not any(b > 0x7F for b in raw), "docs/manual.html carries a byte above 0x7F"


def test_shipped_manual_latin1_equals_ascii_decode():
    with open(HTML, "rb") as fh:
        raw = fh.read()
    # the exact clause-2 phrasing: the latin-1 read equals the ascii decode.
    assert raw.decode("latin-1") == raw.decode("ascii")


def test_grep_style_count_is_zero():
    # mirror `grep -oP '[^\x00-\x7F]' docs/manual.html | wc -l` printing 0.
    with open(HTML, "rb") as fh:
        raw = fh.read()
    above = sum(1 for b in raw if b > 0x7F)
    assert above == 0


def test_check_ascii_passes_the_shipped_bytes():
    mod = _load()
    with open(HTML, "rb") as fh:
        raw = fh.read()
    assert mod.check_ascii(raw) == []


# ------------------------------------------------------------------ negatives (the check bites)
def test_check_ascii_catches_a_high_byte():
    mod = _load()
    # a non-ASCII character (U+2014) encoded as UTF-8 is bytes above 0x7F.
    raw = "text".encode("ascii") + chr(0x2014).encode("utf-8")
    findings = mod.check_ascii(raw)
    assert findings, "a byte above 0x7F must be caught"
    assert any(f["clause"] == 2 for f in findings)


def test_check_ascii_catches_a_non_ascii_char_in_a_string():
    mod = _load()
    findings = mod.check_ascii("an arrow " + chr(0x2192) + " here")
    assert findings and findings[0]["clause"] == 2


def test_check_ascii_clean_string_passes():
    mod = _load()
    assert mod.check_ascii("plain ascii only") == []
