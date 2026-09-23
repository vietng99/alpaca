"""M1.12 proof - spec fidelity with a data oracle taxonomy.

`dispose(items, rows, taxonomy)` re-derives a TOTAL disposition of every spec item into
exactly one of {CONSUMED, WAIVED, PENDING, CONDITIONAL}. This module asserts the Done-when
on BOTH the positive and the negative path:

  * every spec item lands in exactly one disposition class, and the totals reconcile to the
    item count (no item counted twice) -- the positive, total-partition path.
  * an item CONSUMED by an oracle class not declared in project.yaml is REFUSED (BLOCKED) --
    the oracle-substitution case the earlier harness's instrument was built to catch, ported to a taxonomy
    read from project.yaml rather than a hardcoded DV class list.
  * the taxonomy is read from project.yaml and its ABSENCE BLOCKs (never a vacuous pass) --
    the deleted hardcoded DV oracle classes and the deleted signed floor are gone.
  * the self-refusals kept from the port fire: an empty item population BLOCKs, absent rows
    BLOCK, and a CONSUMED item the derivation dropped BLOCKs rather than being counted clean.
"""
import os

import pytest

from alpaca.checklist import Halt, artifact, fidelity, step_model, synthesis

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MODELS = os.path.join(REPO, "step-models")

#: the oracle taxonomy shipped in the repo's project.yaml (M1.8 landed oracle_classes).
TAX = ["static", "dynamic", "selftest", "human"]

# A spec whose five items exercise all four disposition classes: two CONSUMED (a legal,
# declared oracle class each), one WAIVED (a register exclusion), one PENDING (an open ask),
# one CONDITIONAL (a standing waiver). No marker word leaks into a statement cell, and no
# item-key token appears in prose, so artifact.parse accepts the file whole.
FOUR = """# Sample spec

## Acceptance

| item  | statement                                    | oracle class | proof kind | notes                         |
| ----- | -------------------------------------------- | ------------ | ---------- | ----------------------------- |
| AC-01 | the parser reads a clean acceptance table    | static       | test       |                               |
| AC-02 | reserved rows return to the pool on expiry   | dynamic      | test       |                               |
| AC-03 | the engine ships under its upstream license  | human        | review     | register exclusion this cycle |
| AC-04 | the fuzzer explores malformed tables here    | selftest     | test       | open question awaiting owner  |
| AC-05 | the reader binds only to the loopback host   | static       | run        | standing waiver from upstream |

## Open questions

None.
"""

# A spec whose one CONSUMED item names an oracle class ABSENT from the taxonomy.
SUBSTITUTED = """# Sample spec

## Acceptance

| item  | statement                                  | oracle class | proof kind |
| ----- | ------------------------------------------ | ------------ | ---------- |
| AC-01 | the parser reads a clean acceptance table  | quantum      | test       |

## Open questions

None.
"""


def _parse(project, text, name="spec.md"):
    p = os.path.join(project, name)
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(text)
    return artifact.parse(p, "item")


def _synth(art):
    model = step_model.load(os.path.join(MODELS, "requirement.json"))
    return synthesis.synthesize(model, art)


# ------------------------------------------------------------- total partition (positive)
def test_every_item_lands_in_exactly_one_class(project):
    art = _parse(project, FOUR)
    rows = _synth(art)
    disp = fidelity.dispose(art["items"], rows, TAX)

    # one disposition per item, every value drawn from the four-class codomain.
    assert sorted(disp) == sorted(it["key"] for it in art["items"])
    assert len(disp) == len(art["items"])
    assert set(disp.values()) <= set(fidelity.CLASSES)

    assert disp["AC-01"] == fidelity.CONSUMED
    assert disp["AC-02"] == fidelity.CONSUMED
    assert disp["AC-03"] == fidelity.WAIVED
    assert disp["AC-04"] == fidelity.PENDING
    assert disp["AC-05"] == fidelity.CONDITIONAL


def test_totals_reconcile_to_the_item_count(project):
    art = _parse(project, FOUR)
    rows = _synth(art)
    disp = fidelity.dispose(art["items"], rows, TAX)
    counts = fidelity.tally(disp)
    # the tally names all four classes and sums to the item count -- no item counted twice.
    assert set(counts) == set(fidelity.CLASSES)
    assert sum(counts.values()) == len(art["items"])
    assert counts[fidelity.CONSUMED] == 2
    assert counts[fidelity.WAIVED] == 1
    assert counts[fidelity.PENDING] == 1
    assert counts[fidelity.CONDITIONAL] == 1


# ------------------------------------------------- oracle substitution (negative, refused)
def test_consumed_item_with_undeclared_oracle_class_is_refused(project):
    art = _parse(project, SUBSTITUTED)
    rows = _synth(art)
    with pytest.raises(Halt) as ex:
        fidelity.dispose(art["items"], rows, TAX)
    assert ex.value.verdict == fidelity.BLOCKED
    assert ex.value.code == fidelity.T_ORACLE_UNDECLARED
    # the twin: the SAME spec with the oracle class declared disposes clean, proving the
    # refusal above is genuine signal and not a blanket rejection.
    tax_plus = TAX + ["quantum"]
    disp = fidelity.dispose(art["items"], rows, tax_plus)
    assert disp["AC-01"] == fidelity.CONSUMED


# --------------------------------------------------- taxonomy read from project.yaml / BLOCK
def test_absent_taxonomy_blocks(project):
    art = _parse(project, FOUR)
    rows = _synth(art)
    for empty in ([], None, {}):
        with pytest.raises(Halt) as ex:
            fidelity.dispose(art["items"], rows, empty)
        assert ex.value.verdict == fidelity.BLOCKED
        assert ex.value.code == fidelity.T_TAXONOMY_ABSENT


def test_load_taxonomy_reads_project_yaml_and_blocks_when_absent(tmp_path):
    # a project.yaml that declares oracle_classes -> the list is returned.
    good = tmp_path / "good"
    good.mkdir()
    (good / "project.yaml").write_text(
        "name: x\noracle_classes:\n  - static\n  - human\n", encoding="utf-8")
    tax = fidelity.load_taxonomy(str(good))
    assert "static" in tax and "human" in tax

    # a project.yaml with no oracle_classes -> BLOCKED, never a silent empty taxonomy.
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "project.yaml").write_text("name: x\n", encoding="utf-8")
    with pytest.raises(Halt) as ex:
        fidelity.load_taxonomy(str(bad))
    assert ex.value.code == fidelity.T_TAXONOMY_ABSENT


def test_shipped_project_yaml_declares_the_taxonomy():
    # the repo's own project.yaml carries the taxonomy this instrument reads (M1.12 data).
    tax = fidelity.load_taxonomy(REPO)
    assert "static" in tax


# ----------------------------------------------------------------- kept self-refusals
def test_empty_population_blocks(project):
    with pytest.raises(Halt) as ex:
        fidelity.dispose([], [], TAX)
    assert ex.value.verdict == fidelity.BLOCKED
    assert ex.value.code == fidelity.T_POPULATION_EMPTY


def test_absent_rows_block(project):
    art = _parse(project, FOUR)
    with pytest.raises(Halt) as ex:
        fidelity.dispose(art["items"], None, TAX)
    assert ex.value.verdict == fidelity.BLOCKED
    assert ex.value.code == fidelity.T_ROWS_ABSENT


def test_consumed_item_with_no_derived_row_is_dropped(project):
    art = _parse(project, FOUR)
    rows = _synth(art)
    # drop every derived row for AC-01 (a CONSUMED item): the obligation is dropped, never a
    # clean CONSUMED count.
    kept = [r for r in rows if r["item"] != "AC-01"]
    with pytest.raises(Halt) as ex:
        fidelity.dispose(art["items"], kept, TAX)
    assert ex.value.verdict == fidelity.BLOCKED
    assert ex.value.code == fidelity.T_ITEM_DROPPED
    # a non-consumed item (AC-03, WAIVED) needs no derived row: dropping its rows is clean.
    kept3 = [r for r in rows if r["item"] != "AC-03"]
    disp = fidelity.dispose(art["items"], kept3, TAX)
    assert disp["AC-03"] == fidelity.WAIVED
