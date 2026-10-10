"""op-008 t-071b (defect D-1): the byte lane honours the term list's `!allow:` rules, judged in the
context of each match, the way the text lane judges its own matches.

Before the fix the barrier's byte lane (`barrier._blob_hits`) never applied an allow rule, so a term
the owner allowed in one URL came back as a byte-lane hit in every text file that carried the URL,
and `release check` refused a candidate whose only matches were allowed. The audit's byte lane was
the opposite hole: once the text lane had suppressed a term anywhere in a file it skipped the term
for the whole file, so an allowed URL hid the same term elsewhere in the raw bytes.

The terms here are synthetic. Nothing reads the project's private term list.
"""
import base64
import os

import pytest

from alpaca import barrier
from alpaca.gates import leak_audit as la
from alpaca.gates import verdict as vc
from alpaca.release import checks
from alpaca.tests.test_outbound_barrier import (  # noqa: F401  (fixtures used by name)
    TERM, _commit, _commits, _fixed_clock, _run, repo)

URL = "https://example.invalid/owner/" + TERM
ALLOW = r"!allow: https://example\.invalid/owner/zqsealedtokenalpha  # the project's own home URL"
README = ("# Demo\n\nClone it from <" + URL + "> and run the installer.\n"
          "Nothing else here.\n")
INSTALL = ("#!/bin/sh\n# installer\nREPO=\"" + URL + "\"\n"
           "git clone \"$REPO\" demo\necho \"done: " + URL + "\"\n")
BARE = "the value is " + TERM + " and must not ship\n"


def _tl(*extra):
    tl, reasons, _detail = la._terms_from_lines([TERM, ALLOW] + list(extra), "test")
    assert tl is not None and not reasons
    return tl


def _chunked(raw, size):
    return [raw[i:i + size] for i in range(0, len(raw), size)] or [b""]


# ------------------------------------------------------------------ the byte lane itself
def test_the_text_lane_suppresses_the_allowed_url():
    hits, suppressed = la.scan_text(_tl(), README, "README.md", shape_on=False)
    assert hits == [] and suppressed


@pytest.mark.parametrize("body", [README, INSTALL], ids=["readme", "install"])
def test_an_allowed_url_in_a_text_file_gives_no_byte_hit(body):
    assert la.byte_hits(_tl(), [body.encode("utf-8")]) == set()


@pytest.mark.parametrize("codec", ["utf-16", "utf-16-le", "utf-16-be"])
def test_an_allowed_url_in_utf16_text_gives_no_byte_hit(codec):
    assert la.byte_hits(_tl(), [README.encode(codec)]) == set()


def test_the_same_term_elsewhere_in_the_file_still_hits():
    raw = (README + BARE).encode("utf-8")
    assert la.byte_hits(_tl(), [raw]) == {TERM}


def test_the_same_term_on_a_line_of_its_own_after_a_long_allowed_line_still_hits():
    raw = (URL + " " + "x" * 3000 + "\n" + TERM + "\n").encode("utf-8")
    assert la.byte_hits(_tl(), [raw]) == {TERM}


def test_a_term_inside_a_binary_still_hits():
    raw = bytes(range(256)) * 4 + TERM.encode() + b"\x00\x01\x02" + bytes(range(256))
    assert la.byte_hits(_tl(), [raw]) == {TERM}


def test_an_allowed_url_in_a_binary_does_not_hide_the_bare_term_further_on():
    raw = (b"\x00\x01" + URL.encode() + b"\x00" + bytes(range(1, 256)) * 8
           + TERM.encode() + b"\x00\x02")
    assert la.byte_hits(_tl(), [raw]) == {TERM}


def test_a_term_inside_a_base64_blob_still_hits():
    pad = base64.b64encode(os.urandom(600)).decode()
    raw = (README + "data = '" + pad[:300] + TERM + pad[300:] + "'\n").encode("utf-8")
    assert la.byte_hits(_tl(), [raw]) == {TERM}


def test_an_allowed_url_at_the_start_of_a_long_line_does_not_hide_the_term_deep_in_it():
    raw = (URL + " " + "QUJD" * 200 + TERM + "QUJD" * 10 + "\n").encode("utf-8")
    assert la.byte_hits(_tl(), [raw]) == {TERM}
    hits, _sup = la.scan_text(_tl(), raw.decode(), "blob.txt", shape_on=False)
    assert {(h["rule"], h["lane"]) for h in hits} == {(TERM, "line"), (TERM, "joined")}


def test_an_allowed_url_deep_in_a_long_line_is_judged_in_its_own_context():
    raw = ("QUJD" * 200 + " " + URL + " end\n").encode("utf-8")
    assert la.byte_hits(_tl(), [raw]) == set()
    hits, sup = la.scan_text(_tl(), raw.decode(), "blob.txt", shape_on=False)
    assert hits == [] and sup


def test_a_term_hidden_from_the_text_lane_beside_an_allowed_url_still_hits():
    # UTF-16 text whose raw bytes also carry the term in ASCII: the text lane decodes the ASCII run
    # as other characters, so only the byte lane sees it; the allowed URL must not hide it.
    raw = ("﻿see " + URL + "\n").encode("utf-16-le") + TERM.encode() + "\n".encode("utf-16-le")
    text, codec = la.decode_explicit(raw)
    assert text is not None and codec.startswith("utf-16")
    hits, sup = la.scan_text(_tl(), text, "notes.txt", shape_on=False)
    assert hits == [] and sup
    assert la.byte_hits(_tl(), [raw]) == {TERM}


@pytest.mark.parametrize("size", [1, 7, 64, 4096])
def test_a_streamed_file_is_judged_in_the_same_contexts(size):
    tl = _tl()
    allowed = (README * 3 + INSTALL).encode("utf-8")
    assert la.byte_hits(tl, _chunked(allowed, size)) == set()
    assert la.byte_hits(tl, _chunked(allowed + BARE.encode("utf-8"), size)) == {TERM}
    u16 = README.encode("utf-16")
    assert la.byte_hits(tl, _chunked(u16, size)) == set()
    assert la.byte_hits(tl, _chunked(u16 + BARE.encode("utf-16-le"), size)) == {TERM}


def test_a_long_streamed_binary_keeps_the_bare_term_after_the_allowed_url():
    tl = _tl()
    raw = URL.encode() + b"\x00" * 50000 + bytes(range(1, 256)) * 400 + TERM.encode() + b"\x00" * 9
    for size in (13, 1000, la.BYTE_CHUNK):
        assert la.byte_hits(tl, _chunked(raw, size)) == {TERM}


def test_an_allow_rule_still_clears_nothing_without_a_match_in_its_context():
    tl = _tl()
    assert la.byte_hits(tl, [BARE.encode("utf-8")]) == {TERM}
    plain, _r, _d = la._terms_from_lines([TERM], "test")
    assert la.byte_hits(plain, [README.encode("utf-8")]) == {TERM}


def test_a_regex_line_is_judged_in_its_own_context_too():
    tl, _r, _d = la._terms_from_lines(
        [TERM, r"re: zqregexsecret\d+", r"!allow: docs\.example\.invalid/zqregexsecret\d+  # a doc link"],
        "test")
    label = "regex at list line 2"
    raw = b"see https://docs.example.invalid/zqregexsecret42 for more\n"
    assert la.byte_hits(tl, [raw]) == set()
    assert la.byte_hits(tl, [raw + b"value zqregexsecret7\n"]) == {label}
    assert la.byte_hits(_tl(r"re: zqregexsecret\d+"), [raw]) == {"regex at list line 3"}


# ------------------------------------------------------------------ an allow rule clears what it covers
def test_an_allowed_url_does_not_clear_the_bare_term_beside_it_on_the_same_line():
    line = "see <" + URL + "> and also " + TERM + " here\n"
    hits, sup = la.scan_text(_tl(), line, "notes.md", shape_on=False)
    assert {(h["rule"], h["lane"]) for h in hits} == {(TERM, "line"), (TERM, "joined")}
    assert [r["lane"] for r in sup] == ["line", "joined"]
    assert la.byte_hits(_tl(), [line.encode()]) == {TERM}


def test_an_allowed_first_match_does_not_end_the_joined_lane():
    # the joined lane sees a term split across a line break; the allowed URL comes first
    raw = README + "value: " + TERM[:8] + "\n    " + TERM[8:] + "\n"
    hits, _sup = la.scan_text(_tl(), raw, "notes.md", shape_on=False)
    assert [(h["rule"], h["lane"]) for h in hits] == [(TERM, "joined")]


def test_an_anchored_allow_rule_keeps_its_line_meaning():
    tl, _r, _d = la._terms_from_lines(
        [TERM, r'!allow: ^REPO="https://example\.invalid/owner/zqsealedtokenalpha"$  # installer'],
        "test")
    raw = '  REPO="' + URL + '"\n'
    assert la.scan_text(tl, raw, "install.sh", shape_on=False)[0] == []
    assert la.byte_hits(tl, [raw.encode()]) == set()
    raw2 = 'X REPO="' + URL + '"\n'
    assert la.scan_text(tl, raw2, "install.sh", shape_on=False)[0]
    assert la.byte_hits(tl, [raw2.encode()]) == {TERM}


def test_a_rule_that_marks_the_line_but_does_not_cover_the_term_clears_nothing():
    tl, _r, _d = la._terms_from_lines([TERM, r"!allow: ^credits:  # a line marker"], "test")
    raw = "credits: " + TERM + "\n"
    assert la.scan_text(tl, raw, "notes.md", shape_on=False)[0]
    assert la.byte_hits(tl, [raw.encode()]) == {TERM}


def test_text_that_lengthens_when_casefolded_keeps_the_match_in_place():
    raw = "Stra\u00dfe \u00df\u00df\u00df <" + URL + ">\n"
    hits, sup = la.scan_text(_tl(), raw, "notes.md", shape_on=False)
    assert hits == [] and sup
    assert la.byte_hits(_tl(), [raw.encode()]) == set()


# ------------------------------------------------------------------ the barrier (release leak scan)
def test_blob_hits_clears_an_allowed_url_in_a_text_file():
    tl = _tl()
    assert barrier._blob_hits(tl, README.encode("utf-8"), "README.md") == []
    assert barrier._blob_hits(tl, INSTALL.encode("utf-8"), "install.sh") == []


def test_blob_hits_gives_exactly_the_unallowed_hits_for_a_file_with_both():
    tl = _tl()
    hits = barrier._blob_hits(tl, (README + BARE).encode("utf-8"), "README.md")
    # the bare line, seen by the line lane and again by the joined projection; no byte-lane hit
    assert [(h["rule"], h["lane"], h["line"]) for h in hits] == [(TERM, "line", 5),
                                                                 (TERM, "joined", 0)]
    assert "thevalueis" + TERM in hits[1]["context"]          # the joined view of line 5


def test_blob_hits_reports_a_binary_term_from_the_byte_lane():
    tl = _tl()
    raw = b"\x00\x81\x02" + README.encode() + b"\xff\xfe\x00" + TERM.encode() + b"\x00\x8d\x81\x90"
    assert len(raw) % 2 and la.decode_explicit(raw) == (None, None)      # no codec reads it
    hits = barrier._blob_hits(tl, raw, "blob.bin")
    assert [(h["rule"], h["lane"]) for h in hits] == [(TERM, "bytes")]


def _allow_in_terms(root):
    with open(os.path.join(root, ".alpaca", "sealed-terms.txt"), "w", encoding="utf-8") as fh:
        fh.write(TERM + "\n" + ALLOW + "\n")


def test_the_barrier_passes_a_push_whose_only_matches_are_allowed(repo):
    root, _bare = repo
    _allow_in_terms(root)
    _commit(root, "README.md", README, "point at " + URL)
    _commit(root, "install.sh", INSTALL, "add installer")
    res = barrier.scan(root, _commits(root))
    assert res.verdict == vc.PASS, barrier.render(res.report)


def test_the_barrier_still_refuses_the_bare_term_beside_an_allowed_url(repo):
    root, _bare = repo
    _allow_in_terms(root)
    _commit(root, "README.md", README + BARE, "add readme")
    res = barrier.scan(root, _commits(root))
    assert res.verdict == vc.FAIL and barrier.R_LEAK in res.reasons


def test_the_barrier_still_refuses_the_term_in_a_binary_beside_an_allowed_url(repo):
    root, _bare = repo
    _allow_in_terms(root)
    _commit(root, "README.md", README, "add readme")
    p = os.path.join(root, "assets", "blob.bin")
    os.makedirs(os.path.dirname(p))
    with open(p, "wb") as fh:
        fh.write(b"\x89PNG\r\n\x1a\n\x00\x00" + URL.encode() + b"\x00\xff" * 40 + TERM.encode() + b"\x00")
    assert _run(root, "add", "-A").returncode == 0
    assert _run(root, "commit", "-m", "add blob").returncode == 0
    res = barrier.scan(root, _commits(root))
    assert res.verdict == vc.FAIL and barrier.R_LEAK in res.reasons


# ------------------------------------------------------------------ the audit
def _write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(data)


def _audit(tmp_path, files):
    target = tmp_path / "tree"
    for rel, data in files.items():
        _write(str(target / rel), data)
    return la.audit(str(target), term_list=_tl(), shape_on=False, require_control=False).report


def test_the_audit_clears_an_allowed_url_in_a_text_file(tmp_path):
    rep = _audit(tmp_path, {"README.md": README.encode(), "install.sh": INSTALL.encode()})
    assert rep["hit_count"] == 0 and rep["suppressed_count"] >= 3
    assert la.R_HITS not in rep["reason_codes"]


def test_the_audit_keeps_a_byte_only_term_beside_an_allowed_url(tmp_path):
    raw = ("﻿see " + URL + "\n").encode("utf-16-le") + TERM.encode() + "\n".encode("utf-16-le")
    rep = _audit(tmp_path, {"notes.txt": raw})
    assert [(h["file"], h["rule"], h["lane"]) for h in rep["hits"]] == [("notes.txt", TERM, "bytes")]


def test_the_audit_gives_exactly_the_unallowed_hits_for_a_file_with_both(tmp_path):
    rep = _audit(tmp_path, {"README.md": (README + BARE).encode()})
    assert [(h["rule"], h["lane"], h["line"]) for h in rep["hits"]] == [(TERM, "line", 5),
                                                                        (TERM, "joined", 0)]
    assert {(r["lane"], r["line"]) for r in rep["suppressed"]} == {("line", 3), ("joined", 0)}


def test_the_audit_no_longer_clears_a_binary_by_its_path(tmp_path):
    # The old byte lane matched allow rules against "<path> (byte lane: ...)", a context that is not
    # the match's own: a rule naming the path cleared every occurrence in the file.
    tl, _r, _d = la._terms_from_lines([TERM, r"!allow: ^assets/blob\.bin  # a path rule"], "test")
    target = tmp_path / "tree"
    raw = b"\x00\x81" + TERM.encode() + b"\x00\x8d\x90"
    assert la.decode_explicit(raw) == (None, None)        # only the byte lane reads it
    _write(str(target / "assets" / "blob.bin"), raw)
    rep = la.audit(str(target), term_list=tl, shape_on=False, require_control=False).report
    assert [(h["rule"], h["lane"]) for h in rep["hits"]] == [(TERM, "bytes")]


# ------------------------------------------------------------------ the release leak-scan gate
def test_the_release_leak_scan_passes_a_candidate_whose_only_matches_are_allowed(tmp_path):
    root = tmp_path / "root"
    _write(str(root / ".alpaca" / "terms.txt"), (TERM + "\n" + ALLOW + "\n").encode())
    _write(str(root / "project.yaml"),
           b"name: sample\nbarrier:\n  terms: .alpaca/terms.txt\n  protected_paths: ['.alpaca/']\n")
    cfg, terms, _policy_hash = checks.policy(root)
    tree = tmp_path / "tree"
    files = {"README.md": README.encode(), "install.sh": INSTALL.encode()}
    for rel, data in files.items():
        _write(str(tree / rel), data)
    seed = tmp_path / "seed"
    checks.copy_files({rel: tree / rel for rel in files}, seed)
    commit = checks.init_repository(seed)
    assert checks.left_out(seed) == []
    report = barrier.scan(str(seed), [commit], cfg=cfg, term_list=terms)
    assert report.verdict == vc.PASS, barrier.render(report.report)

    _write(str(tree / "src" / "app.txt"), BARE.encode())
    files["src/app.txt"] = BARE.encode()
    seed2 = tmp_path / "seed2"
    checks.copy_files({rel: tree / rel for rel in files}, seed2)
    commit2 = checks.init_repository(seed2)
    report2 = barrier.scan(str(seed2), [commit2], cfg=cfg, term_list=terms)
    assert report2.verdict == vc.FAIL and barrier.R_LEAK in report2.reasons


# ------------------------------------------------------------------ review of 26cee2fda (t-071b r2)
# B1: a streamed match that waits for its right-hand context must not fall out of the kept tail.
_FILLER = bytes([0x81, 0x8d, 0x8f, 0x90, 0x9d, 0xff]) * 10      # undecodable in utf-8 and cp1252


def _spread_near_boundary(gap, tail_room):
    spread = (b"\x00" * gap).join(TERM.encode()[i:i + 1] for i in range(len(TERM)))
    pad = la.BYTE_CHUNK - len(spread) - tail_room
    return (_FILLER * (pad // len(_FILLER) + 1))[:pad] + spread + _FILLER * 2000


@pytest.mark.parametrize("gap", [260, 300, 400])
@pytest.mark.parametrize("tail_room", [2000, 4000])
def test_a_nul_spread_match_near_a_chunk_boundary_is_judged(gap, tail_room):
    raw = _spread_near_boundary(gap, tail_room)
    chunks = _chunked(raw, la.BYTE_CHUNK)
    assert la.byte_hits(_tl(), chunks) == {TERM}
    plain, _r, _d = la._terms_from_lines([TERM], "test")
    assert la.byte_hits(plain, chunks) == {TERM}                 # the control


def test_a_streamed_file_keeps_the_nul_spread_match(tmp_path):
    p = tmp_path / "big.bin"
    p.write_bytes(_spread_near_boundary(300, 4000))
    hits, _sup, err = la.scan_bytes_stream(_tl(), str(p), "big.bin")
    assert not err and [(h["rule"], h["lane"]) for h in hits] == [(TERM, "bytes")]


# B2: a match that runs past its line is not covered by a rule that covers only its first line.
_MULTI = [TERM, r"re: zqsecret\s+\d{6}", r"!allow: example\.invalid/owner/\S+  # the home URL"]
_MULTI_TEXT = b"see https://example.invalid/owner/zqsecret\n123456 is the code\n"


def _multi():
    tl, reasons, _d = la._terms_from_lines(_MULTI, "test")
    assert tl is not None and not reasons
    return tl


@pytest.mark.parametrize("form", ["text", "binary", "utf-16"])
def test_a_match_that_leaves_its_line_is_not_cleared(form):
    raw = {"text": _MULTI_TEXT,
           "binary": b"\x00\x81" + _MULTI_TEXT + b"\x8d\x90\x00",
           "utf-16": _MULTI_TEXT.decode().encode("utf-16")}[form]
    assert la.byte_hits(_multi(), [raw]) == {"regex at list line 2"}
    assert barrier._blob_hits(_multi(), raw, "notes.txt")


def test_a_commit_message_whose_match_leaves_its_line_is_refused():
    msg = b"tree 0\n\nmsg https://example.invalid/owner/zqsecret\n123456\n"
    assert barrier._meta_hits(_multi(), msg)


def test_the_text_lane_does_not_clear_a_match_that_leaves_its_line():
    hits, _sup = la.scan_text(_multi(), _MULTI_TEXT.decode(), "notes.txt", shape_on=False)
    assert [(h["rule"], h["lane"]) for h in hits] == [("regex at list line 2", "line")]


_QUOTED = [TERM, r"re: zqsecret\s+\d{6}", r'!allow: example\.invalid/owner/[^"]*  # the quoted home URL']


def test_a_negated_class_rule_does_not_clear_a_match_that_leaves_its_line():
    tl, _r, _d = la._terms_from_lines(_QUOTED, "test")
    inside = b'see "https://example.invalid/owner/zqsecret 123456" now\n'
    assert la.byte_hits(tl, [inside]) == set()
    assert [h for h in la.scan_text(tl, inside.decode(), "a.txt", shape_on=False)[0]
            if h["lane"] == "line"] == []
    across = b'see "https://example.invalid/owner/zqsecret\n123456" now\n'
    assert la.byte_hits(tl, [across]) == {"regex at list line 2"}
    assert la.scan_text(tl, across.decode(), "a.txt", shape_on=False)[0]


# M1: `^` and `$` in an allow rule match only at a real line start or end, never at a cut edge.
_ANCHORED = [TERM, r"!allow: ^see .*example\.invalid/owner/zqsealedtokenalpha  # a line rule",
             r"!allow: example\.invalid/home/owner/zqsealedtokenalpha$  # an end-of-line rule"]
_URL = b"see https://example.invalid/owner/" + TERM.encode()
_END = b"at https://example.invalid/home/owner/" + TERM.encode()


def _anchored():
    tl, reasons, _d = la._terms_from_lines(_ANCHORED, "test")
    assert tl is not None and not reasons
    return tl


def _line_hits(raw):
    # the joined lane removes whitespace and line breaks, so line anchors mean nothing there
    hits, _sup = la.scan_text(_anchored(), raw.decode(), "a.txt", shape_on=False)
    return [h["rule"] for h in hits if h["lane"] == "line"]


def test_anchored_rules_clear_on_real_line_boundaries():
    for raw in (_URL + b"\n", b"x\n" + _URL + b"\nmore\n", _END + b"\n", b"x\n  " + _END + b"  \nmore\n"):
        assert la.byte_hits(_anchored(), [raw]) == set(), raw
        assert _line_hits(raw) == [], raw
    bin_ = b"\x00\x81\n" + _URL + b"\n\x8d\x90\x00"
    assert la.decode_explicit(bin_) == (None, None)
    assert la.byte_hits(_anchored(), [bin_]) == set()


def test_caret_does_not_match_where_the_byte_lane_cut_the_line():
    raw = _FILLER + b"leak " + "​".encode() * 400 + _URL + _FILLER
    assert la.decode_explicit(raw) == (None, None)
    assert la.byte_hits(_anchored(), [raw]) == {TERM}
    raw2 = _FILLER * 50 + b" leak " + _URL + _FILLER
    assert la.byte_hits(_anchored(), [raw2]) == {TERM}


def test_caret_does_not_match_where_a_long_line_was_cut():
    line = "x" * 300 + " " + _URL.decode() + "\n"
    assert _line_hits(line.encode()) == [TERM]
    assert la.byte_hits(_anchored(), [line.encode()]) == {TERM}


def test_dollar_does_not_match_where_the_context_was_cut():
    raw = _END + b"y" * 3000 + b"\n"
    assert _line_hits(raw) == [TERM]
    assert la.byte_hits(_anchored(), [raw]) == {TERM}
    binary = b"\x00\x81" + _END + _FILLER * 40
    assert la.byte_hits(_anchored(), [binary]) == {TERM}


def test_dollar_does_not_match_at_a_window_edge_while_streaming():
    raw = b"\n" + _END + b"\n"
    for size in (1, 7, 64):
        assert la.byte_hits(_anchored(), _chunked(raw, size)) == set()
    cut = b"\n" + _END + b"z" * 9000 + b"\n"
    for size in (1, 7, 64, 4096):
        assert la.byte_hits(_anchored(), _chunked(cut, size)) == {TERM}
