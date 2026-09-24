"""alpaca interview: the slot map a runbook needs, its append-only log, the readback and the
sign-off (docs/interview.md).

The default slots come from templates/interview/slots.yaml; a project adds or replaces slots in
input/interview/slots.yaml. Each `set` appends one line to input/interview/log.jsonl. `status`
passes only when no slot is open. `signoff` writes the readback with the sha256 of the log, and a
later change to the log makes the sign-off stale.
"""
import hashlib
import json
import os
import re

import pytest
import yaml

from alpaca.tests.conftest import REPO

DEFAULT_SLOTS = ["goal", "scope-out", "done-bar", "thresholds", "edge-cases", "failures", "never",
                 "owner-gates", "rollback", "evidence", "knobs", "commands"]


@pytest.fixture
def clock(monkeypatch):
    from alpaca import util
    now = {"t": "2026-09-24T10:00:00+00:00"}
    monkeypatch.setattr(util, "_CLOCK", lambda: now["t"])
    return now


def _cli(argv, capsys):
    from alpaca import cli
    rc = cli.main(argv)
    out = capsys.readouterr().out
    try:
        return rc, json.loads(out)
    except ValueError:
        return rc, out


def _note(capsys, text):
    rc, out = _cli(["note", "add", text, "--json"], capsys)
    assert rc == 0, out
    return out["file"]


def _set(capsys, slot, state, value, source, reason=None):
    argv = ["interview", "set", slot, "--" + state, "--value", value, "--source", source]
    if reason is not None:
        argv += ["--reason", reason]
    return _cli(argv, capsys)


def _log(project):
    path = os.path.join(project, "input", "interview", "log.jsonl")
    if not os.path.isfile(path):
        return []
    return [json.loads(line) for line in open(path, encoding="utf-8").read().splitlines()]


def _settle_all(capsys, note, skip=()):
    for slot in DEFAULT_SLOTS:
        if slot in skip:
            continue
        rc, out = _set(capsys, slot, "answered", "value of %s" % slot, note)
        assert rc == 0, out


def test_the_default_slot_list_ships_with_the_twelve_slots():
    with open(os.path.join(REPO, "templates", "interview", "slots.yaml"), encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    assert [s["id"] for s in data["slots"]] == DEFAULT_SLOTS
    for s in data["slots"]:
        assert s["fills"].strip(), s


def test_set_appends_one_line_and_never_rewrites_the_log(project, capsys, clock):
    note = _note(capsys, "we want short links for the newsletter")
    rc, out = _set(capsys, "goal", "answered", "short links for the newsletter", note)
    assert rc == 0, out
    first = open(os.path.join(project, "input", "interview", "log.jsonl"), "rb").read()
    log = _log(project)
    assert log == [{"time": clock["t"], "slot": "goal", "state": "answered",
                    "value": "short links for the newsletter", "source": note}]
    clock["t"] = "2026-09-24T10:05:00+00:00"
    rc, out = _set(capsys, "thresholds", "default", "p95 under 50 ms", "round:1/q2")
    assert rc == 0, out
    after = open(os.path.join(project, "input", "interview", "log.jsonl"), "rb").read()
    assert after.startswith(first) and len(_log(project)) == 2


def test_set_refuses_an_unknown_slot(project, capsys, clock):
    rc, out = _set(capsys, "colour", "answered", "blue", "owner")
    assert rc == 1, out
    assert "colour" in out and "goal" in out                  # names the slots it knows
    assert _log(project) == []


def test_set_refuses_a_waive_without_a_reason(project, capsys, clock):
    rc, out = _set(capsys, "rollback", "waived", "", "owner")
    assert rc == 1 and "reason" in out, out
    rc, out = _set(capsys, "rollback", "waived", "", "owner", reason="   ")
    assert rc == 1, out
    assert _log(project) == []
    rc, out = _set(capsys, "rollback", "waived", "", "owner", reason="nothing to undo: read only")
    assert rc == 0, out
    assert _log(project)[-1]["reason"] == "nothing to undo: read only"


def test_set_refuses_a_source_of_the_wrong_shape(project, capsys, clock):
    note = _note(capsys, "a piece")
    for bad in ("somewhere", "round:1", "round:0/q1", "round:1/q0", "round:x/q1", "Owner",
                "input/notes/missing.md", "notes/a.md", "input/notes/../log.md", ""):
        rc, out = _set(capsys, "goal", "answered", "x", bad)
        assert rc != 0, (bad, out)
    assert _log(project) == []
    for good in (note, "round:2/q1", "round:12/q4", "owner"):
        rc, out = _set(capsys, "goal", "answered", "x", good)
        assert rc == 0, (good, out)
    assert [e["source"] for e in _log(project)] == [note, "round:2/q1", "round:12/q4", "owner"]


def test_status_fails_while_a_slot_is_open_and_lists_the_uncited_notes(project, capsys, clock):
    cited = _note(capsys, "short links for the newsletter")
    loose = _note(capsys, "also: never lose a link")
    _set(capsys, "goal", "answered", "short links", cited)
    rc, out = _cli(["interview", "status", "--json"], capsys)
    assert rc == 1, out
    assert out["verdict"] == "FAIL"
    assert out["progress"] == "1/12 slots settled"
    assert out["open"] == DEFAULT_SLOTS[1:]
    assert out["uncited_notes"] == [loose]
    goal = [s for s in out["slots"] if s["slot"] == "goal"][0]
    assert goal["state"] == "answered" and goal["value"] == "short links" and goal["source"] == cited
    assert out["signoff"]["state"] == "needed"
    rc, text = _cli(["interview", "status"], capsys)
    assert rc == 1
    assert "1/12 slots settled" in text and loose in text and "scope-out" in text


def test_status_passes_only_with_no_open_slot(project, capsys, clock):
    note = _note(capsys, "the notes")
    _settle_all(capsys, note, skip=("never",))
    assert _cli(["interview", "status", "--json"], capsys)[0] == 1
    _set(capsys, "never", "default", "never delete a stored link", "round:1/q1")
    rc, out = _cli(["interview", "status", "--json"], capsys)
    assert rc == 0, out
    assert out["progress"] == "12/12 slots settled" and out["open"] == [] and out["uncited_notes"] == []
    _set(capsys, "never", "open", "", "owner")                 # reopened: open again
    rc, out = _cli(["interview", "status", "--json"], capsys)
    assert rc == 1 and out["open"] == ["never"]


def test_readback_gives_the_five_buckets(project, capsys, clock):
    note = _note(capsys, "short links; must be fast")
    _set(capsys, "goal", "answered", "short links for the newsletter", note)
    _set(capsys, "done-bar", "answered", "it is fast", note)
    _set(capsys, "done-bar", "answered", "p95 redirect under 50 ms on the load test", "round:1/q1")
    _set(capsys, "thresholds", "answered", "p95 50 ms at 200 rps", "round:1/q2")
    _set(capsys, "rollback", "default", "redeploy the previous tag", "round:2/q1")
    _set(capsys, "scope-out", "waived", "", "owner", reason="nothing is left out on purpose")
    rc, out = _cli(["interview", "readback", "--json"], capsys)
    assert rc == 0, out
    b = out["buckets"]
    assert [i["slot"] for i in b["clear"]] == ["goal"]
    assert [i["slot"] for i in b["added"]] == ["done-bar", "thresholds"]
    assert [(i["slot"], i["value"], i["source"]) for i in b["default"]] == [
        ("rollback", "redeploy the previous tag", "round:2/q1")]
    assert [(i["slot"], i["reason"]) for i in b["waived"]] == [("scope-out", "nothing is left out on purpose")]
    assert b["drifted"] == [{"slot": "done-bar", "first": "it is fast", "first_source": note,
                             "now": "p95 redirect under 50 ms on the load test", "now_source": "round:1/q1"}]
    assert "edge-cases" in out["open"]
    rc, text = _cli(["interview", "readback"], capsys)
    assert rc == 0
    for heading in ("## Clear from the start", "## Added during the interview", "## Filled by default",
                    "## Waived", "## Drifted"):
        assert heading in text, heading
    assert "it is fast" in text and "nothing is left out on purpose" in text


def test_signoff_is_refused_while_a_slot_is_open(project, capsys, clock):
    note = _note(capsys, "the notes")
    _settle_all(capsys, note, skip=("rollback",))
    rc, out = _cli(["interview", "signoff", "--by", "the operator"], capsys)
    assert rc == 1 and "rollback" in out, out
    assert not [n for n in os.listdir(os.path.join(project, "input", "interview")) if n.startswith("signed-")]
    from alpaca import db
    assert db.events(db.connect(project), kind="interview-signoff") == []


def test_signoff_writes_the_signed_file_and_a_later_change_makes_it_stale(project, capsys, clock):
    from alpaca import db
    note = _note(capsys, "the notes")
    _settle_all(capsys, note)
    log_path = os.path.join(project, "input", "interview", "log.jsonl")
    log_sha = hashlib.sha256(open(log_path, "rb").read()).hexdigest()
    rc, out = _cli(["interview", "signoff", "--by", "the operator", "--json"], capsys)
    assert rc == 0, out
    assert re.match(r"^input/interview/signed-\d{8}T\d{6}Z-[0-9a-f]{12}\.md$", out["file"]), out["file"]
    assert out["log_sha256"] == log_sha
    text = open(os.path.join(project, out["file"]), encoding="utf-8").read()
    assert log_sha in text and "signed_by: the operator" in text
    assert "## Clear from the start" in text and "value of goal" in text
    ev = db.events(db.connect(project), kind="interview-signoff")
    assert len(ev) == 1 and ev[0]["data"]["file"] == out["file"]
    assert ev[0]["data"]["log_sha256"] == log_sha and ev[0]["data"]["by"] == "the operator"
    rc, st = _cli(["interview", "status", "--json"], capsys)
    assert rc == 0 and st["signoff"]["state"] == "signed" and st["signoff"]["file"] == out["file"]
    # signing an unchanged log again writes nothing new
    rc, again = _cli(["interview", "signoff", "--by", "the operator", "--json"], capsys)
    assert rc == 0 and again["file"] == out["file"] and again["new"] is False
    assert len(db.events(db.connect(project), kind="interview-signoff")) == 1
    # a later change to the log makes the sign-off stale
    clock["t"] = "2026-09-24T11:00:00+00:00"
    _set(capsys, "thresholds", "answered", "p95 40 ms", "round:3/q1")
    rc, st = _cli(["interview", "status", "--json"], capsys)
    assert st["signoff"]["state"] == "stale" and st["signoff"]["file"] == out["file"]
    rc, text = _cli(["interview", "status"], capsys)
    assert "stale" in text
    rc, new = _cli(["interview", "signoff", "--by", "the operator", "--json"], capsys)
    assert rc == 0 and new["new"] is True and new["file"] != out["file"]
    assert _cli(["interview", "status", "--json"], capsys)[1]["signoff"]["state"] == "signed"


def test_a_project_adds_and_replaces_slots(project, capsys, clock):
    path = os.path.join(project, "input", "interview", "slots.yaml")
    os.makedirs(os.path.dirname(path))
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("slots:\n"
                 "  - id: goal\n    fills: the one sentence the sponsor signed\n"
                 "  - id: data-retention\n    fills: how long a stored link is kept\n")
    rc, out = _cli(["interview", "status", "--json"], capsys)
    ids = [s["slot"] for s in out["slots"]]
    assert ids == DEFAULT_SLOTS + ["data-retention"]
    goal = [s for s in out["slots"] if s["slot"] == "goal"][0]
    assert goal["fills"] == "the one sentence the sponsor signed"
    assert out["progress"] == "0/13 slots settled"
    rc, out = _set(capsys, "data-retention", "answered", "one year", "owner")
    assert rc == 0, out
    rc, out = _set(capsys, "colour", "answered", "blue", "owner")
    assert rc == 1


def test_a_broken_project_slot_file_is_blocked(project, capsys, clock):
    path = os.path.join(project, "input", "interview", "slots.yaml")
    os.makedirs(os.path.dirname(path))
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("slots:\n  - fills: no id here\n")
    rc, out = _cli(["interview", "status"], capsys)
    assert rc == 2 and "slots.yaml" in out, out


# ------------------------------------------------------------------ review fixes (op-003)
def _signed(project, capsys, note):
    _settle_all(capsys, note)
    rc, out = _cli(["interview", "signoff", "--by", "the operator", "--json"], capsys)
    assert rc == 0, out
    return out["file"]


def test_a_slot_added_after_the_signoff_makes_it_stale(project, capsys, clock):
    """The sign-off records the slot list it settled. A slot the map gains later (a project
    slots.yaml, a product upgrade) is open, so the sign-off no longer holds."""
    note = _note(capsys, "the notes")
    signed = _signed(project, capsys, note)
    text = open(os.path.join(project, signed), encoding="utf-8").read()
    assert "slots: %s" % " ".join(DEFAULT_SLOTS) in text
    path = os.path.join(project, "input", "interview", "slots.yaml")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("slots:\n  - id: data-retention\n    fills: how long a stored link is kept\n")
    rc, st = _cli(["interview", "status", "--json"], capsys)
    assert rc == 1 and st["open"] == ["data-retention"]
    assert st["signoff"]["state"] == "stale" and st["interview"] == "stale", st["signoff"]
    assert "data-retention" in st["signoff"]["why"]
    rc, text = _cli(["interview", "status"], capsys)
    assert "sign-off: stale" in text and "data-retention" in text
    # settling it and signing again gives a sign-off that holds
    _set(capsys, "data-retention", "answered", "one year", "owner")
    clock["t"] = "2026-09-24T11:00:00+00:00"
    rc, new = _cli(["interview", "signoff", "--by", "the operator", "--json"], capsys)
    assert rc == 0 and new["new"] is True
    assert _cli(["interview", "status", "--json"], capsys)[1]["signoff"]["state"] == "signed"


def test_a_note_added_after_the_signoff_makes_it_stale(project, capsys, clock):
    """A note kept after the sign-off is new raw input the interview has not seen. Notes that
    were there at signing and that no answer cites do not change the sign-off."""
    note = _note(capsys, "the notes")
    _note(capsys, "a side remark no answer cites")
    signed = _signed(project, capsys, note)
    text = open(os.path.join(project, signed), encoding="utf-8").read()
    assert re.search(r"^notes: \S+\.md \S+\.md$", text, re.M), text
    rc, st = _cli(["interview", "status", "--json"], capsys)
    assert rc == 0 and st["signoff"]["state"] == "signed"
    later = _note(capsys, "second change: add CSV export, must never leak other users' links")
    rc, st = _cli(["interview", "status", "--json"], capsys)
    assert st["signoff"]["state"] == "stale" and later in st["signoff"]["why"], st["signoff"]
    rc, text = _cli(["interview", "status"], capsys)
    assert "sign-off: stale" in text and later in text
