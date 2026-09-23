import os, stat
from alpaca import util

def test_write_text_respects_umask(tmp_path):
    old = os.umask(0o022)
    try:
        p = tmp_path / "out.txt"
        util.write_text(str(p), "hello")
        assert stat.S_IMODE(os.stat(str(p)).st_mode) == 0o644
    finally:
        os.umask(old)

def test_now_iso_has_offset():
    ts = util.now_iso()
    assert ts[-6] in ("+", "-") or ts.endswith("Z")

def test_canonical_json_sorts_keys():
    assert util.canonical_json({"b": 1, "a": 2}) == '{"a":2,"b":1}'

def test_sha256_hex_of_empty_matches_known_digest():
    assert util.sha256_hex(b"") == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    assert util.sha256_hex("") == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"

def test_read_text_round_trips(tmp_path):
    p = tmp_path / "rt.txt"
    util.write_text(str(p), "round trip \u00e9")
    assert util.read_text(str(p)) == "round trip \u00e9"
