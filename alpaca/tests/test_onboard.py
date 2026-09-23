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


# t-002: onboarding a fresh copy of the distribution merges the answers into the shipped template
# project.yaml. It used to rebuild a subset from scratch and drop the template's other keys
# (skin, operators, wiki_providers, budget, concurrency, kicker_grace_seconds, retention, barrier).
import shutil
import yaml

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _template():
    with open(os.path.join(REPO, "project.yaml"), encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _raw(project):
    with open(os.path.join(project, "project.yaml"), encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def test_shipped_project_yaml_is_a_template():
    tpl = _template()
    assert tpl.get("template") is True
    # the keys the old onboarding dropped are part of the shipped template
    for key in ("skin", "operators", "wiki_providers", "budget", "concurrency",
                "kicker_grace_seconds", "retention", "barrier"):
        assert key in tpl, key


def test_onboard_keeps_every_template_key(project):
    shutil.copy(os.path.join(REPO, "project.yaml"), os.path.join(project, "project.yaml"))
    tpl = _template()
    cli.main(["init"])
    rc = cli.main(["onboard", "--name", "demo", "--who", "alex:owner,robin:engineer",
                   "--what", "A tiny CLI", "--tier", "on-prem", "--preset", "plain-writing"])
    assert rc == 0
    cfg = _raw(project)
    # every template key survives except the template marker itself
    missing = [k for k in tpl if k != "template" and k not in cfg]
    assert missing == []
    assert "template" not in cfg
    # template key order is kept
    assert [k for k in cfg if k in tpl] == [k for k in tpl if k != "template"]
    # the values the person gave are applied
    assert cfg["name"] == "demo" and cfg["what"] == "A tiny CLI" and cfg["tier"] == "on-prem"
    assert cfg["people"] == [{"name": "alex", "role": "owner"}, {"name": "robin", "role": "engineer"}]
    assert cfg["style"]["presets"] == ["plain-writing"]
    assert cfg["style"]["banned"] == tpl["style"]["banned"]
    assert cfg["project_id"] != tpl["project_id"] and cfg["cwd_history"] == [project]
    # values nobody answered stay as the template shipped them
    for key in ("skin", "operators", "wiki_providers", "budget", "concurrency",
                "kicker_grace_seconds", "retention", "barrier", "non_adoptions", "paths",
                "phases", "boundary_rules", "fingerprint", "oracle_classes", "default_level"):
        assert cfg[key] == tpl[key], key
    # the result reads as onboarded and still passes the schema
    assert proj.validate(project) == (True, [])
    from alpaca import adopt
    assert adopt.detect(project) == adopt.POPULATED


def test_onboard_template_sensed_commands_still_win(project):
    shutil.copy(os.path.join(REPO, "project.yaml"), os.path.join(project, "project.yaml"))
    open(os.path.join(project, "Makefile"), "w", encoding="utf-8").write("test:\n\tpytest\n")
    cli.main(["init"])
    assert cli.main(["onboard", "--name", "d", "--who", "a:owner", "--what", "w"]) == 0
    cfg = _raw(project)
    tpl = _template()
    assert cfg["commands"]["test"] == "make test"
    # commands no file implied keep the template's value
    assert cfg["commands"]["build"] == tpl["commands"]["build"]
    # no --tier and no --preset: the template's values stay
    assert cfg["tier"] == tpl["tier"] and cfg["style"] == tpl["style"]
