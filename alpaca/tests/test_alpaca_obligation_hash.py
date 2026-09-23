from alpaca import db
from alpaca.checklist import artifact, bridge, gate, synthesis


def test_synthesized_obligation_keeps_hash_after_bridge_and_derived_tag_change(tmp_path):
    spec = tmp_path / "acceptance.md"
    spec.write_text("| item | need |\n|---|---|\n| adder8 | timing closure report |\n")
    parsed = artifact.parse(str(spec), "item")
    model = {"model_id": "build", "steps": [{"key": "timing", "obligation": "Verify timing closure for {item} using {artifact}"}]}
    rows = synthesis.derive(model, parsed, op="adder8-flow", session="codex-one", operator="codex")
    conn = db.connect(str(tmp_path))
    assert bridge.apply(conn, rows)["verdict"] == 0
    stored = db.rows(conn, "rows", "id=?", (rows[0]["id"],))[0]
    assert gate._row_content_hash(stored) == stored["content_hash"]
    db.patch(conn, "rows", "id", stored["id"], {"tag": "Built", "status": "discharged"})
    updated = db.rows(conn, "rows", "id=?", (stored["id"],))[0]
    assert gate._row_content_hash(updated) == stored["content_hash"]
    updated["statement"] = "Changed acceptance requirement without a new obligation"
    assert gate._row_content_hash(updated) != stored["content_hash"]
