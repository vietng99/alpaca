"""M1.13 - supersession over Alpaca obligation rows (alpaca/checklist/supersession.py).

Port of the earlier harness supersession selftest controls, minus the signature guard, kept as
pytest cases asserting BOTH sides:

  * freeze witness drift HALTs (an in-place edit of a frozen row) - and, the point of the
    port, this HALT survives the removal of the sign row: no signature is involved.
  * a correction is a NEW row that CITES the superseded id (cite-and-freeze); a miscited
    correction is refused, and the frozen original is never edited.
  * supersession is resolved FORWARD: the superseding row is authoritative, the original
    stays as history.
"""
import os

import pytest

from alpaca.checklist import Halt, artifact, step_model, synthesis, supersession

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MODELS = os.path.join(REPO, "step-models")

CLEAN = """# Sample spec

## Acceptance

| item  | statement                          | oracle class | proof kind |
| ----- | ---------------------------------- | ------------ | ---------- |
| AC-01 | the parser reads a clean table     | unit         | test       |
| AC-02 | residue outside the table is BLOCK | unit         | test       |

## Open questions

None.
"""


def _rows(tmp_path):
    p = tmp_path / "spec.md"
    p.write_text(CLEAN, encoding="utf-8")
    art = artifact.parse(str(p), "item")
    model = step_model.load(os.path.join(MODELS, "requirement.json"))
    return synthesis.synthesize(model, art)


def _correction(row, new_id, statement="a corrected obligation statement here"):
    fixed = dict(row)
    fixed["id"] = new_id
    fixed["statement"] = statement
    fixed["supersedes"] = row["id"]
    fixed.pop("content_hash", None)
    fixed.pop("prev_hash", None)
    return fixed


# --------------------------------------------------------------- freeze / drift
def test_synthesized_rows_are_frozen_and_verify(tmp_path):
    rows = _rows(tmp_path)
    for r in rows:
        assert supersession.is_frozen(r)
    assert supersession.verify_chain(rows) == rows


def test_in_place_edit_of_a_frozen_row_halts(tmp_path):
    """The freeze witness drift HALT, with NO sign row anywhere: editing a frozen row's content
    in place (keeping its stale content_hash) is caught as drift on the next verify."""
    rows = _rows(tmp_path)
    tampered = list(rows)
    tampered[0] = dict(rows[0])
    tampered[0]["statement"] = "TAMPERED-IN-PLACE and still long enough"   # keep old content_hash
    with pytest.raises(Halt) as ex:
        supersession.verify_chain(tampered)
    assert ex.value.code == supersession.R_FROZEN_HALT
    # the untampered store still verifies: the HALT is about the edit, not the shape.
    assert supersession.verify_chain(rows) == rows


def test_reorder_breaks_the_chain(tmp_path):
    rows = _rows(tmp_path)
    if len(rows) < 2:
        pytest.skip("need at least two rows to reorder")
    swapped = [rows[1], rows[0]] + list(rows[2:])
    with pytest.raises(Halt) as ex:
        supersession.verify_chain(swapped)
    assert ex.value.code == supersession.R_CHAIN_DRIFT


# --------------------------------------------------------------- cite-and-freeze
def test_correction_is_a_new_row_citing_supersedes(tmp_path):
    rows = _rows(tmp_path)
    old = rows[0]["id"]
    after = supersession.supersede(rows, old, _correction(rows[0], "R-1b"))
    # the superseding row is frozen, cites the original, and is authoritative going forward.
    assert supersession.is_frozen(after[-1])
    assert after[-1]["supersedes"] == old
    assert supersession.resolve(after, old)["id"] == "R-1b"
    # the frozen ORIGINAL is never edited: it is still on the store, byte for byte.
    assert any(r["id"] == old and r["content_hash"] == rows[0]["content_hash"] for r in after)
    assert [r["id"] for r in supersession.history(after, old)] == [old, "R-1b"]


def test_a_miscited_correction_is_refused(tmp_path):
    rows = _rows(tmp_path)
    old = rows[0]["id"]
    bad = _correction(rows[0], "R-1b")
    bad["supersedes"] = "not-the-original"        # cites the wrong id
    with pytest.raises(Halt) as ex:
        supersession.supersede(rows, old, bad)
    assert ex.value.code == supersession.R_MISCITED


def test_superseding_an_unknown_row_is_refused(tmp_path):
    rows = _rows(tmp_path)
    with pytest.raises(Halt) as ex:
        supersession.supersede(rows, "no-such-id", _correction(rows[0], "R-x"))
    assert ex.value.code == supersession.R_NO_SUCH_ROW


def test_the_last_appended_successor_is_authoritative(tmp_path):
    rows = _rows(tmp_path)
    old = rows[0]["id"]
    after = supersession.supersede(rows, old, _correction(rows[0], "R-1b", "first correction here"))
    after = supersession.supersede(after, "R-1b",
                                   _correction(after[-1], "R-1c", "second correction here now"))
    assert supersession.resolve(after, old)["id"] == "R-1c"
    assert [r["id"] for r in supersession.history(after, old)] == [old, "R-1b", "R-1c"]
