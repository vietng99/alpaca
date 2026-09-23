"""M1.11 - the bridge (R1) and the traceability closure (R2).

Proof test for the Done-when:

  * re-running the bridge over the same rows changes nothing (idempotent),
  * a content drift on an existing row id is BLOCKED,
  * closure BLOCKS on an empty register and on an empty population,
  * the register <-> item bijection is re-derived from the artifact bytes.

Both the positive path (a clean bridge and a clean closure PASS) and the negative
path (drift BLOCKED, empties BLOCKED, both bijection directions BLOCKED) are asserted.

Ported from the earlier harness gates/checklist_bridge.py + gates/traceability_closure.py, with the
plan's adaptations: the sign / signature fields are gone, the war-log becomes the events
table, and cite-and-freeze is kept (a frozen row is never edited in place; a content drift
is refused, never a silent PASS).
"""
import os

from alpaca import db
from alpaca.checklist import artifact, bridge, closure, step_model, synthesis
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


def _artifact(tmp_path, text=CLEAN, name="spec.md"):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return artifact.parse(str(p), "item")


def _model():
    return step_model.load(os.path.join(MODELS, "requirement.json"))


def _rows(tmp_path):
    return synthesis.synthesize(_model(), _artifact(tmp_path))


def _count(conn):
    return conn.execute("SELECT COUNT(*) FROM rows").fetchone()[0]


def _events(conn, kind=None):
    if kind is None:
        return conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    return conn.execute("SELECT COUNT(*) FROM events WHERE kind=?", (kind,)).fetchone()[0]


# ============================================================== bridge: positive path
def test_apply_lands_every_derived_row(project, tmp_path):
    conn = db.connect(project)
    rows = _rows(tmp_path)
    result = bridge.apply(conn, rows, session="s")
    assert result["verdict"] == verdict.PASS
    assert result["appended"] == len(rows)
    assert result["committed"] is True
    assert _count(conn) == len(rows)
    stored = {r["id"] for r in db.rows(conn, "rows")}
    assert stored == {r["id"] for r in rows}


def test_apply_writes_the_event_and_the_row_together(project, tmp_path):
    conn = db.connect(project)
    rows = _rows(tmp_path)
    before = _events(conn)
    bridge.apply(conn, rows, session="s")
    # a single bridge event carries the batch, and the rows landed with it.
    assert _events(conn, "bridge") == 1
    assert _events(conn) == before + 1
    assert _count(conn) == len(rows)
    ok, _ = db.verify_chain(conn)
    assert ok is True


def test_apply_stores_no_sign_fields(project, tmp_path):
    conn = db.connect(project)
    bridge.apply(conn, _rows(tmp_path), session="s")
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(rows)")}
    for f in ("sign", "signer", "signature", "countersign", "capture", "roster"):
        assert f not in cols


# ============================================================== bridge: idempotent
def test_apply_is_idempotent(project, tmp_path):
    conn = db.connect(project)
    rows = _rows(tmp_path)
    bridge.apply(conn, rows, session="s")
    n_rows = _count(conn)
    n_events = _events(conn)

    again = bridge.apply(conn, rows, session="s")
    assert again["verdict"] == verdict.PASS
    assert again["appended"] == 0
    # re-running over the same rows changes nothing: no new row, no new event.
    assert _count(conn) == n_rows
    assert _events(conn) == n_events


# ============================================================== bridge: content drift
def test_apply_blocks_on_content_drift(project, tmp_path):
    conn = db.connect(project)
    rows = _rows(tmp_path)
    bridge.apply(conn, rows, session="s")
    n_rows = _count(conn)
    n_events = _events(conn)

    # the same row id with different content: a frozen row is never edited in place.
    drifted = [dict(r) for r in rows]
    drifted[0] = dict(drifted[0])
    drifted[0]["statement"] = "DRIFTED: the obligation text was changed under the same id"

    result = bridge.apply(conn, drifted, session="s")
    assert result["verdict"] == verdict.BLOCKED
    assert any("DRIFT" in f["code"] for f in result["findings"])
    # nothing was written: the store still carries the original content, unedited.
    assert _count(conn) == n_rows
    assert _events(conn) == n_events
    stored = db.rows(conn, "rows", "id=?", (rows[0]["id"],))[0]
    assert stored["statement"] == rows[0]["statement"]


def test_apply_ignores_a_forged_content_hash_field(project, tmp_path):
    conn = db.connect(project)
    rows = _rows(tmp_path)
    bridge.apply(conn, rows, session="s")

    # a re-run whose self-declared content_hash is forged to match must still be judged
    # on the real content: matching content re-runs cleanly, drifted content is refused.
    forged = [dict(r) for r in rows]
    forged[0] = dict(forged[0])
    forged[0]["statement"] = "DRIFTED under a forged content_hash field"
    forged[0]["content_hash"] = rows[0]["content_hash"]  # forged to look unchanged

    result = bridge.apply(conn, forged, session="s")
    assert result["verdict"] == verdict.BLOCKED
    assert any("DRIFT" in f["code"] for f in result["findings"])


# ============================================================== bridge: refusals
def test_apply_blocks_empty_input(project):
    conn = db.connect(project)
    result = bridge.apply(conn, [], session="s")
    assert result["verdict"] == verdict.BLOCKED
    assert result["appended"] == 0
    assert any("EMPTY" in f["code"] for f in result["findings"])


def test_apply_fails_on_a_duplicate_row_id(project, tmp_path):
    conn = db.connect(project)
    rows = _rows(tmp_path)
    dup = list(rows) + [dict(rows[0])]
    result = bridge.apply(conn, dup, session="s")
    assert result["verdict"] == verdict.FAIL
    assert any("DUPLICATE" in f["code"] for f in result["findings"])
    assert _count(conn) == 0  # a defective batch lands nothing


def test_apply_fails_on_a_malformed_row(project, tmp_path):
    conn = db.connect(project)
    rows = _rows(tmp_path)
    bad = [dict(r) for r in rows]
    bad[0] = dict(bad[0])
    del bad[0]["statement"]
    result = bridge.apply(conn, bad, session="s")
    assert result["verdict"] == verdict.FAIL
    assert any("MALFORMED" in f["code"] for f in result["findings"])
    assert _count(conn) == 0


# ============================================================== closure: positive path
def test_check_passes_on_exact_bijection(project, tmp_path):
    conn = db.connect(project)
    art = _artifact(tmp_path)
    rows = synthesis.synthesize(_model(), art)
    bridge.apply(conn, rows, session="s")

    register = list(art["keys"])  # the register enumerates exactly the artifact items
    result = closure.check(conn, register, art)
    assert result["verdict"] == verdict.PASS


# ============================================================== closure: empties BLOCK
def test_check_blocks_empty_register(project, tmp_path):
    conn = db.connect(project)
    art = _artifact(tmp_path)
    bridge.apply(conn, synthesis.synthesize(_model(), art), session="s")
    result = closure.check(conn, [], art)
    assert result["verdict"] == verdict.BLOCKED
    assert any("REGISTER-EMPTY" in f["code"] for f in result["findings"])


def test_check_blocks_empty_population(project, tmp_path):
    conn = db.connect(project)
    art = _artifact(tmp_path)
    empty = dict(art)
    empty["items"] = []
    empty["keys"] = []
    result = closure.check(conn, ["AC-01"], empty)
    assert result["verdict"] == verdict.BLOCKED
    assert any("POPULATION" in f["code"] or "ITEMS-EMPTY" in f["code"]
               for f in result["findings"])


# ============================================================== closure: both directions
def test_check_blocks_spec_item_without_row(project, tmp_path):
    conn = db.connect(project)
    art = _artifact(tmp_path)
    bridge.apply(conn, synthesis.synthesize(_model(), art), session="s")
    # a register requirement that the artifact content does not carry.
    register = list(art["keys"]) + ["AC-99"]
    result = closure.check(conn, register, art)
    assert result["verdict"] == verdict.BLOCKED
    assert any("SPEC-ITEM-WITHOUT-ROW" in f["code"] for f in result["findings"])


def test_check_blocks_row_without_spec_item(project, tmp_path):
    conn = db.connect(project)
    art = _artifact(tmp_path)
    bridge.apply(conn, synthesis.synthesize(_model(), art), session="s")
    # an artifact item that no register requirement traces to.
    register = [k for k in art["keys"] if k != "AC-03"]
    result = closure.check(conn, register, art)
    assert result["verdict"] == verdict.BLOCKED
    assert any("ROW-WITHOUT-SPEC-ITEM" in f["code"] for f in result["findings"])


# ============================================================== closure: store presence
def test_check_blocks_when_the_bridge_did_not_run(project, tmp_path):
    conn = db.connect(project)
    art = _artifact(tmp_path)  # nothing landed in the store
    register = list(art["keys"])
    result = closure.check(conn, register, art)
    assert result["verdict"] == verdict.BLOCKED
    assert any("NOT-IN-STORE" in f["code"] for f in result["findings"])


def test_check_rederives_the_population_from_artifact_bytes(project, tmp_path):
    """The covered-set is re-parsed from the artifact bytes, not read from the store: rows
    landed for one artifact do not launder coverage of a different artifact's items."""
    conn = db.connect(project)
    first = _artifact(tmp_path, name="first.md")
    bridge.apply(conn, synthesis.synthesize(_model(), first), session="s")

    # a second artifact with the same item keys but different bytes (different path/sha) has
    # different canonical row ids, so the store rows from `first` do not cover it.
    other = CLEAN.replace("Some prose", "Different prose entirely")
    second = _artifact(tmp_path, text=other, name="second.md")
    result = closure.check(conn, list(second["keys"]), second)
    assert result["verdict"] == verdict.BLOCKED
    assert any("NOT-IN-STORE" in f["code"] for f in result["findings"])
