"""M4.14 proof - the doctrine set, its registry, and the registration check.

Done-when (checklist M4.14): every leaf named in spec 5.13 exists as a file and is registered in
BOTH directions with its status re-derived from its own bytes, and the registration check BLOCKS
on an empty population. This module drives that on the POSITIVE path (the live shipped tree) and
on the NEGATIVE path (throwaway roots the test builds), so no assertion is a tautological pass:

  * the live tree PASSes: every leaf under doctrine/leaves/ has exactly one INDEX row, every row
    resolves to a leaf, and every Digest cell equals the sha256 prefix re-derived from the leaf's
    own bytes;
  * both directions are asserted explicitly: leaves-on-disk == registry targets;
  * a leaf on disk with no row -> LEAF-NOT-REGISTERED-IN-INDEX (FAIL);
  * a registry row pointing at no leaf -> INDEX-ROW-TARGET-ABSENT (FAIL);
  * a digest cell that contradicts the leaf's bytes -> DIGEST-CELL-MISMATCH (FAIL);
  * an empty leaf population -> EMPTY-LEAF-POPULATION (BLOCKED, the floor);
  * the dropped leaves are ASSERTED ABSENT from doctrine/leaves/;
  * the ported gate's own negative-control selftest passes (every control fires).
"""
import hashlib
import os
import re

from alpaca.tests.conftest import REPO
from alpaca.gates import doctrine_registration_check as reg
from alpaca.gates import verdict as vc

LEAVES = os.path.join(REPO, "doctrine", "leaves")
INDEX = os.path.join(REPO, "doctrine", "INDEX.md")


def _leaf_basenames():
    return sorted(n for n in os.listdir(LEAVES) if n.endswith(".md"))


def _index_targets():
    with open(INDEX, "r", encoding="utf-8") as fh:
        text = fh.read()
    _, body, _, pidx = reg.parse_registry_table(text)
    targets = []
    for row in body:
        cell = row.cells[pidx] if (pidx is not None and pidx < len(row.cells)) else row.raw
        for tok in reg.harvest_md_tokens(cell) or reg.harvest_md_tokens(row.raw):
            targets.append(tok.replace("\\", "/").rsplit("/", 1)[-1])
            break
    return sorted(targets)


# ---------------------------------------------------------------- positive path (live tree)

def test_live_tree_registers_in_both_directions_and_passes():
    assert reg.check(REPO) == vc.PASS
    res = reg.run_check(REPO)
    assert res.verdict == vc.PASS, res.reasons
    # both directions: every leaf on disk is a registry target and every target is a leaf.
    assert _leaf_basenames() == _index_targets()
    # the population is real, not vacuous.
    assert res.info["leaves"] >= 30
    assert res.info["passed"] > 0


def test_the_four_shipped_by_other_tasks_are_registered_when_present():
    # the task scans the dir: leaves shipped by M3.3 and M4.5 are present here and MUST carry a
    # registry row (the M4.6/M4.7 leaves are not in this branch and are correctly not registered).
    on_disk = set(_leaf_basenames())
    targets = set(_index_targets())
    for shipped in ("two-change-classes.md", "serial-with-resolve.md"):
        assert shipped in on_disk, "expected a shipped leaf on disk: %s" % shipped
        assert shipped in targets, "a present leaf must be registered: %s" % shipped


# The exact leaf set named in spec 5.13 (kept leaves renamed where noted, the six Alpaca operator
# leaves, the five new leaves). A missing name here is a Done-when failure, not a silent pass: this
# is what keeps the registry test from resting only on whatever files happen to be on disk.
SPEC_5_13_LEAVES = (
    # kept (25), renamed where the spec text notes it (honest-verification -> verification-tags)
    "file-as-truth", "provenance-or-die", "test-or-UNTESTED", "verification-tags", "halt-on-drift",
    "crash-only-resume", "double-dispatch", "movement-gate-pin-sentinel", "audit-first-primacy",
    "hitl-decision-gate", "harness-navigation", "harness-distillation", "workspace-isolation",
    "plugin-not-welded", "blind-pairs", "budget-survival", "concurrency-tiers", "watchdog-liveness",
    "heartbeat-os-kicker", "quota-reset-recovery", "worker-pool-recovery", "autodrive-levels",
    "lessons-write-gate", "spec-driven-checklist", "context-clearing",
    # Alpaca operator leaves (6)
    "landing-pad-resume", "operate-in-place", "intent-is-the-done-bar", "serial-with-resolve",
    "write-ahead-capture", "historian",
    # new leaves (5)
    "record-over-ceremony", "level-gated-advance", "board-is-truth", "two-change-classes",
    "plain-language",
)


def test_every_spec_5_13_leaf_is_present_and_registered():
    # Done-when: every leaf named in spec 5.13 exists as a file AND is registered in both
    # directions. Enumerated by name so a leaf that is silently missing (as operate-in-place was)
    # fails here instead of passing on a filesystem-derived population.
    assert len(SPEC_5_13_LEAVES) == 36
    on_disk = set(_leaf_basenames())
    targets = set(_index_targets())
    missing_file = [n for n in SPEC_5_13_LEAVES if (n + ".md") not in on_disk]
    unregistered = [n for n in SPEC_5_13_LEAVES if (n + ".md") not in targets]
    assert missing_file == [], "spec 5.13 leaves with no file: %s" % missing_file
    assert unregistered == [], "spec 5.13 leaves with no registry row: %s" % unregistered
    assert reg.check(REPO) == vc.PASS


def test_digest_cell_is_re_derived_from_the_leaf_bytes():
    # pick a real leaf, recompute its digest from bytes, and find that exact prefix in INDEX.md.
    name = "file-as-truth.md"
    with open(os.path.join(LEAVES, name), "rb") as fh:
        want = hashlib.sha256(fh.read()).hexdigest()[:reg.DIGEST_LEN].lower()
    with open(INDEX, "r", encoding="utf-8") as fh:
        index_text = fh.read()
    row = [ln for ln in index_text.splitlines() if "file-as-truth.md" in ln and ln.strip().startswith("|")]
    assert row, "no registry row for file-as-truth.md"
    assert want in row[0].lower(), "the INDEX digest cell is not the leaf's own re-derived digest"


# ---------------------------------------------------------------- dropped leaves absent

def test_dropped_leaves_are_absent():
    on_disk = set(_leaf_basenames())
    for dropped in ("signed-checklist-gate.md", "non-interference.md", "external-head-anchor.md"):
        assert dropped not in on_disk, "a dropped leaf must not ship: %s" % dropped
    # the design-only eval and self-improve leaves are parked, not shipped.
    for n in on_disk:
        assert not n.startswith("eval-"), "an eval-* leaf must not ship: %s" % n
        assert not n.startswith("self-improve-"), "a self-improve-* leaf must not ship: %s" % n


# ---------------------------------------------------------------- negative path (throwaway roots)

def _build(tmp_path, mutate):
    files = reg._good_tree("doctrine_registration_check.py")
    mutate(files)
    root = os.path.join(str(tmp_path), "t")
    os.makedirs(root, exist_ok=True)
    reg._mk(root, files)
    return reg.run_check(root, require_map=("beta.md",))


def test_a_leaf_with_no_registry_row_fails(tmp_path):
    def m(f):
        f["doctrine/INDEX.md"] = "\n".join(
            ln for ln in f["doctrine/INDEX.md"].splitlines() if "beta.md" not in ln) + "\n"
    res = _build(tmp_path, m)
    assert res.verdict == vc.FAIL
    assert any(reg.R_NOT_REGISTERED in r for r in res.reasons), res.reasons


def test_a_dangling_registry_row_fails(tmp_path):
    def m(f):
        f["doctrine/INDEX.md"] = f["doctrine/INDEX.md"].replace(
            "[beta](leaves/beta.md)", "[gamma](leaves/gamma.md)")
        f.pop("doctrine/leaves/beta.md")
    res = _build(tmp_path, m)
    assert res.verdict == vc.FAIL
    assert any(reg.R_ROW_TARGET_ABSENT in r for r in res.reasons), res.reasons


def test_a_digest_cell_that_contradicts_the_bytes_fails(tmp_path):
    good_beta = reg.GOOD_LEAF.format(title="Beta")
    real = reg._digest_of(good_beta)

    def m(f):
        f["doctrine/INDEX.md"] = f["doctrine/INDEX.md"].replace(real, "0123456789ab")
    res = _build(tmp_path, m)
    assert res.verdict == vc.FAIL
    assert any(reg.R_DIGEST_MISMATCH in r for r in res.reasons), res.reasons


def test_empty_leaf_population_blocks(tmp_path):
    # the Done-when floor: the registration check BLOCKS on an empty population.
    def m(f):
        f.pop("doctrine/leaves/alpha.md")
        f.pop("doctrine/leaves/beta.md")
        f["doctrine/leaves/README.txt"] = "the dir exists but holds no markdown leaf\n"
    res = _build(tmp_path, m)
    assert res.verdict == vc.BLOCKED
    assert any(reg.R_EMPTY_POP in r for r in res.reasons), res.reasons


def test_a_non_utf8_byte_in_a_leaf_blocks(tmp_path):
    def m(f):
        f["doctrine/leaves/beta.md"] = ("# Beta\n\n".encode("utf-8") + b"\xff\xfe raw\n")
    res = _build(tmp_path, m)
    assert res.verdict == vc.BLOCKED
    assert any(reg.R_DECODE in r for r in res.reasons), res.reasons


# ---------------------------------------------------------------- the gate's own selftest

def test_registration_check_selftest_passes():
    assert reg.selftest() == vc.PASS
