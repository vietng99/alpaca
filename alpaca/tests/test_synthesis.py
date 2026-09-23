"""M1.10 - row synthesis and the Alpaca row schema (alpaca/checklist/synthesis.py, alpaca/db.py).

Proof test for the Done-when: synthesis over a fixture spec produces exactly one row
per (step, item), the partition bijection check PASSes, and a non-PASS leaves the
output staged rather than committed. Both paths are asserted here.

Also: the extended `rows` table carries the Alpaca columns (phase, why, tag, content_hash,
prev_hash, supersedes, kind) and none of the earlier harness sign fields; every synthesized row
carries an empty `why` the design phase must fill.
"""
import os

import pytest

from alpaca import db
from alpaca.checklist import Halt, artifact, step_model, synthesis
from alpaca.gates import verdict

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MODELS = os.path.join(REPO, "step-models")

CLEAN = """# Sample spec

Some prose that names no obligation at all.

## Acceptance

| item  | statement                          | oracle class | proof kind |
| ----- | ---------------------------------- | ------------ | ---------- |
| AC-01 | the parser reads a clean table     | unit         | test       |
| AC-02 | residue outside the table is BLOCK | unit         | test       |
| AC-03 | keys are unique inside the table   | unit         | test       |

## Open questions

None.
"""

#: the earlier harness sign / signature vocabulary that the Alpaca row schema drops entirely.
SIGN_FIELDS = ("sign", "signer", "signature", "countersign", "capture", "roster",
               "preauth", "break_glass", "boundary_signature")


def _artifact(tmp_path, text=CLEAN):
    p = tmp_path / "spec.md"
    p.write_text(text, encoding="utf-8")
    return artifact.parse(str(p), "item")


def _model():
    return step_model.load(os.path.join(MODELS, "requirement.json"))


def _weak_model():
    """A well-formed model whose obligation renders a statement below the row floor."""
    return {
        "model_id": "weak",
        "steps": [
            {"key": "k1", "name": "n1", "ordinal": 1, "witness": "w", "consumes": [],
             "obligation": "{item} ok", "report_back": {
                 "proof": "p for {item}", "where": "w", "how": "h", "when": "t"}},
        ],
    }


# --------------------------------------------------------------- positive path (PASS)
def test_row_per_step_item_and_bijection_passes(tmp_path):
    model = _model()
    art = _artifact(tmp_path)
    rows = synthesis.synthesize(model, art)

    n_steps = len(model["steps"])
    n_items = len(art["items"])
    assert len(rows) == n_steps * n_items

    pairs = [(r["step"], r["item"]) for r in rows]
    assert len(set(pairs)) == len(pairs), "each (step, item) appears exactly once"
    expected = set((s["key"], it["key"]) for s in model["steps"] for it in art["items"])
    assert set(pairs) == expected, "the rows biject with the (step, item) population"


def test_stage_reports_pass_and_committed(tmp_path):
    result = synthesis.stage(_model(), _artifact(tmp_path))
    assert result["verdict"] == verdict.PASS
    assert result["committed"] is True
    assert result["rows"], "a PASS commits its rows"


def test_row_shape_carries_alpaca_columns_and_no_sign_fields(tmp_path):
    rows = synthesis.synthesize(_model(), _artifact(tmp_path))
    for r in rows:
        assert r["kind"] == "item"
        assert r["phase"] == "requirement"
        for col in ("phase", "why", "tag", "content_hash", "prev_hash", "supersedes"):
            assert col in r, "row missing Alpaca column %s" % col
        for f in SIGN_FIELDS:
            assert f not in r, "the earlier harness sign field %r must not exist on an Alpaca row" % f


def test_every_row_carries_an_empty_why(tmp_path):
    rows = synthesis.synthesize(_model(), _artifact(tmp_path))
    for r in rows:
        assert r["why"] == "", "synthesis leaves why empty for the design phase to fill"


def test_proof_names_the_row(tmp_path):
    art = _artifact(tmp_path)
    rows = synthesis.synthesize(_model(), art)
    for r in rows:
        assert r["proof"].startswith("local:")
        assert art["path"] in r["proof"]


def test_rows_are_hash_chained(tmp_path):
    rows = synthesis.synthesize(_model(), _artifact(tmp_path))
    assert rows[0]["prev_hash"] == synthesis.GENESIS
    for a, b in zip(rows, rows[1:]):
        assert b["prev_hash"] == a["content_hash"], "rows chain by content_hash"


def test_row_id_is_deterministic_and_matches(tmp_path):
    art = _artifact(tmp_path)
    model = _model()
    rows = synthesis.synthesize(model, art)
    for r in rows:
        rid = synthesis.row_id(r["step"], art["path"], r["item"], art["sha256_raw"])
        assert r["id"] == rid
    # a second, independent call gives the same id for the same inputs
    s0, i0 = model["steps"][0]["key"], art["items"][0]["key"]
    assert (synthesis.row_id(s0, art["path"], i0, art["sha256_raw"])
            == synthesis.row_id(s0, art["path"], i0, art["sha256_raw"]))
    # a different item yields a different id
    i1 = art["items"][1]["key"]
    assert (synthesis.row_id(s0, art["path"], i0, art["sha256_raw"])
            != synthesis.row_id(s0, art["path"], i1, art["sha256_raw"]))


# --------------------------------------------------------------- negative path (staged, not committed)
def test_bijection_failure_stages_but_does_not_commit(tmp_path):
    art = _artifact(tmp_path)
    # a duplicate item key breaks the partition: two rows for the same (step, item)
    art = dict(art)
    art["items"] = list(art["items"]) + [dict(art["items"][0])]
    result = synthesis.stage(_model(), art)
    assert result["verdict"] != verdict.PASS
    assert result["committed"] is False
    assert result["rows"], "the defective rows are staged, not discarded"
    with pytest.raises(Halt):
        synthesis.synthesize(_model(), art)


def test_statement_below_floor_stages_but_does_not_commit(tmp_path):
    art = _artifact(tmp_path)
    result = synthesis.stage(_weak_model(), art)
    assert result["verdict"] == verdict.FAIL
    assert result["committed"] is False
    assert result["rows"], "rows are derived and staged even when the floor fails"
    with pytest.raises(Halt):
        synthesis.synthesize(_weak_model(), art)


def test_empty_population_is_blocked(tmp_path):
    art = _artifact(tmp_path)
    art = dict(art)
    art["items"] = []
    result = synthesis.stage(_model(), art)
    assert result["verdict"] == verdict.BLOCKED
    assert result["committed"] is False
    with pytest.raises(Halt):
        synthesis.synthesize(_model(), art)


# --------------------------------------------------------------- the extended rows table
def test_rows_table_has_alpaca_columns_and_no_sign_fields(project):
    conn = db.connect(project)
    names = {r["name"] for r in db.rows(conn, "sqlite_master", "type='table'")}
    assert "rows" in names, "M1.10 extends the record with a rows table"
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(rows)")}
    for col in ("id", "kind", "phase", "why", "tag", "content_hash", "prev_hash", "supersedes"):
        assert col in cols, "rows table missing Alpaca column %s" % col
    for f in SIGN_FIELDS:
        assert f not in cols, "the earlier harness sign field %r must not exist in the rows table" % f
