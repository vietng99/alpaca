import os
from alpaca import cli, db, project as proj

def test_onboard_writes_project_yaml_and_queue(project):
    cli.main(["init"])
    rc = cli.main(["onboard", "--name", "demo", "--who", "alex:owner,robin:engineer",
                   "--what", "A tiny CLI", "--task", "add hello verb", "--task", "write README",
                   "--tier", "public", "--preset", "plain-writing"])
    assert rc == 0
    cfg = proj.load(project)
    assert cfg["name"] == "demo" and cfg["people"] == [{"name": "alex", "role": "owner"}, {"name": "robin", "role": "engineer"}]
    assert cfg["tier"] == "public" and cfg["style"]["presets"] == ["plain-writing"]
    assert cfg["phases"]["default"] == ["requirement", "design", "build", "verify", "release"]
    q = open(os.path.join(project, "intents", "queue.md"), encoding="utf-8").read()
    assert "- [ ] add hello verb" in q and "- [ ] write README" in q
    assert proj.is_onboarded(project)
    conn = db.connect(project)
    kinds = [e["kind"] for e in db.events(conn)]
    assert "onboard" in kinds and "op-open" in kinds and "op-close" in kinds
    assert db.rows(conn, "ops", "id=?", ("op-0",))[0]["status"] == "closed"

def test_onboard_twice_is_refused(project):
    cli.main(["init"])
    base = ["onboard", "--name", "d", "--who", "a:owner", "--what", "w"]
    assert cli.main(base) == 0
    assert cli.main(base) == 1

def test_commands_detected_from_tree(project):
    open(os.path.join(project, "Makefile"), "w", encoding="utf-8").write("test:\n\tpytest\n")
    cli.main(["init"]); cli.main(["onboard", "--name", "d", "--who", "a:owner", "--what", "w"])
    assert proj.load(project)["commands"]["test"] == "make test"

def test_onboard_refuses_empty_who(project):
    cli.main(["init"])
    assert cli.main(["onboard", "--name", "d", "--who", "", "--what", "w"]) == 1
    assert not os.path.exists(os.path.join(project, "project.yaml"))

def test_onboard_refuses_when_project_yaml_has_a_name(project):
    cli.main(["init"])
    proj.save(project, {"name": "hand-written"})
    assert cli.main(["onboard", "--name", "d", "--who", "a:owner", "--what", "w"]) == 1
    assert proj.load(project)["name"] == "hand-written"

def test_onboard_record_writes_are_one_transaction(project, monkeypatch):
    # P-005: the onboard event, op-0 open/close and the onboarded flag land together
    # or not at all. A fault at the flag write rolls the op-0 rows back with it.
    cli.main(["init"])
    n_before = len(db.events(db.connect(project)))
    real = db.meta_set
    def boom(conn, key, value):
        if key == "onboarded":
            raise RuntimeError("injected")
        return real(conn, key, value)
    monkeypatch.setattr(db, "meta_set", boom)
    assert cli.main(["onboard", "--name", "d", "--who", "a:owner", "--what", "w"]) == cli.INTERNAL
    conn = db.connect(project)
    assert not proj.is_onboarded(project)
    assert db.rows(conn, "ops", "id=?", ("op-0",)) == []
    assert len(db.events(conn)) == n_before
    assert db.verify_chain(conn)[0] is True
