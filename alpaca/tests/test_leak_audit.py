"""M4.11 proof (half): the ported leak_audit and canary_score gates.

Every control drives a POSITIVE and a NEGATIVE path so none is a tautological pass:

  leak_audit
    * a sealed term inside a COMPOUND identifier is caught (the word-boundary bypass), and a clean
      tree is not flagged;
    * a UTF-16 file carrying the term is FAIL, never laundered to CLEAN by errors="replace";
    * a term hidden in a binary blob is caught by the byte lane (no suffix skip-list);
    * a generic SHAPE rule catches an identity the term list never enumerated (union, not swap);
    * empty term list / absent target / empty population / missing control all BLOCK, never PASS;
    * a mechanical clean is PAUSED without a content-bound blind-read attestation, and PASS with
      one; a recycled attestation for a different tree is refused;
    * the clearance seam PASSes for the exact bytes audited and REFUSES once the tree changes;
    * the sealed-term list is DATA read from project.yaml, never a code literal.

  canary_score
    * both halves of the acceptance rule (a canary reaching PUBLIC, a control refused) FAIL, a
      clean run PASSes, an incomplete or empty classifier BLOCKS, and the summary names indices
      only, never row text (the blind-console property).
"""
import os

import pytest

from alpaca.clock import FixedClock
from alpaca import util
from alpaca.gates import verdict as vc
from alpaca.gates import leak_audit as la
from alpaca.gates import canary_score as cs

TERM = "ZqSealedTokenAlpha"


@pytest.fixture(autouse=True)
def _fixed_clock():
    util.set_clock(FixedClock("2026-01-01T00:00:00+00:00"))
    yield
    util.set_clock(None)


def _wf(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    mode = "wb" if isinstance(data, bytes) else "w"
    with open(path, mode, **({} if isinstance(data, bytes) else {"encoding": "utf-8"})) as fh:
        fh.write(data)
    return path


@pytest.fixture
def terms(tmp_path):
    return _wf(str(tmp_path / "terms" / "t.txt"), (TERM + "\n").encode("utf-8"))


@pytest.fixture
def control(tmp_path):
    d = tmp_path / "control"
    _wf(str(d / "ok.md"), b"ordinary prose with nothing sealed in it\n")
    return str(d)


# ------------------------------------------------------------------ leak_audit
def test_compound_identifier_is_caught_but_clean_is_not(tmp_path, terms, control):
    dirty = tmp_path / "dirty"
    _wf(str(dirty / "m.py"), ("class Prefix" + TERM + "Suffix: pass\n").encode("utf-8"))
    res = la.audit(str(dirty), terms_path=terms, control=control)
    assert res.verdict == vc.FAIL and la.R_HITS in res.reasons
    # positive control: the OLD word-boundary matcher really is blind to this shape.
    import re
    assert re.search(r"\b%s\b" % re.escape(TERM), "class Prefix" + TERM + "Suffix") is None

    clean = tmp_path / "clean"
    _wf(str(clean / "a.md"), b"ordinary prose\n")
    att = _attest(tmp_path, str(clean))
    res = la.audit(str(clean), terms_path=terms, control=control, attestation=att)
    assert res.verdict == vc.PASS


def test_utf16_file_is_fail_not_clean(tmp_path, terms, control):
    d = tmp_path / "u16"
    _wf(str(d / "n.txt"), ("prelude " + TERM + " coda\n").encode("utf-16"))
    res = la.audit(str(d), terms_path=terms, control=control)
    assert res.verdict == vc.FAIL and la.R_HITS in res.reasons


def test_binary_blob_is_caught_by_the_byte_lane(tmp_path, terms, control):
    d = tmp_path / "bin"
    _wf(str(d / "blob.dat"), b"\x81\x8d\x90\x9d" + TERM.encode("utf-8") + b"\x81\x8d\x90\x9d")
    res = la.audit(str(d), terms_path=terms, control=control)
    # undecodable AND carries the term: BLOCKED (a refusal) with the hit still reported.
    assert res.verdict == vc.BLOCKED
    assert la.R_HITS in res.reasons and la.R_FILE_UNDECODABLE in res.reasons


def test_shape_rule_catches_an_unenumerated_identity(tmp_path, terms, control):
    d = tmp_path / "shape"
    _wf(str(d / "a.md"), b"contact: someone.unenumerated@example.invalid\n")
    res = la.audit(str(d), terms_path=terms, control=control)
    assert res.verdict == vc.FAIL and la.R_HITS in res.reasons


def test_empty_and_absent_and_empty_population_all_block(tmp_path, terms, control):
    empty = _wf(str(tmp_path / "terms" / "empty.txt"), b"# only comments\n\n  \n")
    clean = tmp_path / "clean"
    _wf(str(clean / "a.md"), b"nothing sealed\n")
    assert la.audit(str(clean), terms_path=empty, control=control).verdict == vc.BLOCKED
    assert la.audit(str(tmp_path / "nope"), terms_path=terms, control=control).verdict == vc.BLOCKED
    empty_tree = tmp_path / "emptytree"
    empty_tree.mkdir()
    r = la.audit(str(empty_tree), terms_path=terms, control=control)
    assert r.verdict == vc.BLOCKED and la.R_TARGET_EMPTY in r.reasons


def test_missing_control_blocks(tmp_path, terms):
    d = tmp_path / "c"
    _wf(str(d / "a.md"), b"ordinary prose\n")
    r = la.audit(str(d), terms_path=terms, control=None)
    assert r.verdict == vc.BLOCKED and la.R_CONTROL_ABSENT in r.reasons


def _attest(tmp_path, tree):
    digest, _n = la.tree_digest(tree)
    att = str(tmp_path / ("att-%s.json" % os.path.basename(tree)))
    la.write_json_atomic(att, {"tree_digest": digest, "attested_by": "test",
                               "utc": "1970-01-01T00:00:00Z", "statement": "blind read (synthetic)"})
    return att


def test_mechanical_clean_is_paused_without_attestation(tmp_path, terms, control):
    d = tmp_path / "clean"
    _wf(str(d / "a.md"), b"ordinary prose\n")
    r = la.audit(str(d), terms_path=terms, control=control)
    assert r.verdict == vc.PAUSED and la.R_ATTEST_ABSENT in r.reasons
    r2 = la.audit(str(d), terms_path=terms, control=control, attestation=_attest(tmp_path, str(d)))
    assert r2.verdict == vc.PASS


def test_recycled_attestation_is_refused(tmp_path, terms, control):
    d = tmp_path / "clean"
    _wf(str(d / "a.md"), b"ordinary prose\n")
    bad = str(tmp_path / "bad.json")
    la.write_json_atomic(bad, {"tree_digest": "0" * 64, "attested_by": "x", "utc": "x",
                               "statement": "recycled"})
    r = la.audit(str(d), terms_path=terms, control=control, attestation=bad)
    assert r.verdict == vc.BLOCKED and la.R_ATTEST_MISMATCH in r.reasons


def test_clearance_passes_for_audited_bytes_then_refuses_on_drift(tmp_path, terms, control):
    d = tmp_path / "ship"
    _wf(str(d / "a.md"), b"ordinary prose\n")
    receipt = str(tmp_path / "receipt.json")
    la.audit(str(d), terms_path=terms, control=control, attestation=_attest(tmp_path, str(d)),
             receipt=receipt)
    assert la.verify_clearance(str(d), receipt, terms).verdict == vc.PASS
    # the bytes change after the audit -> the clearance refuses, and require_clearance raises.
    _wf(str(d / "leak.md"), (TERM + "\n").encode("utf-8"))
    drift = la.verify_clearance(str(d), receipt, terms)
    assert drift.verdict == vc.BLOCKED and la.R_CLEARANCE_MISMATCH in drift.reasons
    with pytest.raises(la.LeakError):
        la.require_clearance(str(d), receipt, terms)


def test_terms_are_data_from_project_yaml(tmp_path):
    # the sealed-term list is pointed at project.yaml (Step 3), not a code literal.
    root = tmp_path / "proj"
    root.mkdir()
    (root / "project.yaml").write_text(
        "name: p\nbarrier:\n  sealed_terms:\n  - %s\n" % TERM, encoding="utf-8")
    tl, reasons, _d = la.load_terms_from_project(str(root))
    assert tl is not None and any(t == TERM for t, _k in tl.terms)
    # absent config is an advisory: check() PASSes rather than inventing a claim.
    bare = tmp_path / "bare"
    bare.mkdir()
    (bare / "project.yaml").write_text("name: p\n", encoding="utf-8")
    assert la.check(str(bare)) == vc.PASS


def test_leak_audit_selftest_is_green():
    assert la.selftest() == vc.PASS


# ------------------------------------------------------------------ canary_score
SEALED = "| 1 | CANARY |\n| 2 | CONTROL |\n| 3 | CANARY |\n| 4 | CONTROL |\n"


def test_canary_both_halves_and_clean():
    good = "| 1 | SEALED |\n| 2 | PUBLIC |\n| 3 | QUARANTINE |\n| 4 | PUBLIC |\n"
    assert cs.score(SEALED, good)[0] == vc.PASS
    escaped = "| 1 | PUBLIC |\n| 2 | PUBLIC |\n| 3 | SEALED |\n| 4 | PUBLIC |\n"
    assert cs.score(SEALED, escaped)[0] == vc.FAIL
    refused = "| 1 | SEALED |\n| 2 | SEALED |\n| 3 | SEALED |\n| 4 | PUBLIC |\n"
    assert cs.score(SEALED, refused)[0] == vc.FAIL


def test_canary_incomplete_and_empty_block():
    assert cs.score(SEALED, "| 1 | SEALED |\n")[0] == vc.BLOCKED
    assert cs.score("no numbered rows", "| 1 | PUBLIC |\n")[0] == vc.BLOCKED


def test_canary_summary_names_indices_never_text(tmp_path):
    escaped = "| 1 | PUBLIC |\n| 2 | PUBLIC |\n| 3 | SEALED |\n| 4 | PUBLIC |\n"
    detail = str(tmp_path / "detail.md")
    v, s = cs.score(SEALED, escaped, detail_path=detail)
    assert v == vc.FAIL
    assert s["canary_escaped_indices"] == [1]
    assert all(isinstance(x, int) for x in s["canary_escaped_indices"])
    assert os.path.isfile(detail)


def test_canary_score_selftest_is_green():
    assert cs.selftest() == vc.PASS
