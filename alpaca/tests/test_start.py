"""alpaca start: the one entry point from raw notes to intake (docs/intake.md).

It picks spec-kit for a new thing and OpenSpec for a change to something that has specs (the
person can override), prints the steps to a spec, a runbook and intake, and with --prepare
installs the kit and moves a spec-kit project to OpenSpec without losing a row.
"""
import json
import os
import shutil

from alpaca.tests.conftest import REPO

# format 1 behaviour: the worked example as it was in format 1, kept as a fixture
EXAMPLE = os.path.join(REPO, "alpaca", "tests", "fixtures", "runbook-example-v1")
NOTES = "A small service that shortens links and counts how often each one is followed."


def _cli(argv, capsys):
    from alpaca import cli
    rc = cli.main(argv)
    out = capsys.readouterr().out
    try:
        return rc, json.loads(out)
    except ValueError:
        return rc, out


def _feature(root):
    dst = os.path.join(root, "specs", "001-link-shortener")
    os.makedirs(dst)
    shutil.copy(os.path.join(EXAMPLE, "spec.md"), os.path.join(dst, "spec.md"))
    return os.path.join(dst, "spec.md")


def test_a_new_thing_goes_to_spec_kit(project, capsys):
    rc, out = _cli(["start", NOTES, "--json"], capsys)
    assert rc == 0, out
    assert out["kit"] == "spec-kit" and out["mode"] == "new" and "no spec yet" in out["reason"]
    names = [s["step"] for s in out["steps"]]
    # raw notes and no signed interview: the notes go to the inbox and the interview comes first
    assert names == ["note", "interview", "prepare", "spec", "clarify", "runbook", "op", "intake"]
    assert out["interview"] == "needed"
    commands = " ".join(s["command"] for s in out["steps"])
    assert "/speckit-specify" in commands and ".claude/skills/alpaca-runbook-forge/SKILL.md" in commands
    assert "alpaca intake specs/<NNN-name>/spec.md" in commands
    assert out["prepared"] == []           # without --prepare it only reads


def test_a_change_to_a_project_with_specs_goes_to_openspec(project, capsys):
    _feature(project)
    notes = os.path.join(project, "notes.md")
    with open(notes, "w", encoding="utf-8") as fh:
        fh.write("Redirects must answer within 30 ms at p95.\n")
    rc, out = _cli(["start", notes, "--json"], capsys)
    assert rc == 0, out
    assert out["kit"] == "openspec" and out["mode"] == "change"
    assert "specs/001-link-shortener/spec.md" in out["reason"] and out["notes"] == notes
    commands = " ".join(s["command"] for s in out["steps"])
    assert "/opsx:propose" in commands and "alpaca intake openspec/changes/<id>" in commands
    assert "/opsx:archive" in commands


def test_the_person_can_override_the_pick(project, capsys):
    _feature(project)
    rc, out = _cli(["start", NOTES, "--kit", "spec-kit", "--json"], capsys)
    assert rc == 0 and out["kit"] == "spec-kit" and out["reason"] == "chosen with --kit"


def test_empty_notes_are_refused(project, capsys):
    rc, out = _cli(["start", "   ", "--json"], capsys)
    assert rc == 64


def test_prepare_moves_a_spec_kit_project_to_openspec_and_records_the_choice(project, capsys, monkeypatch):
    from alpaca import db, runbook, spec_kits
    installed = []

    def fake_init(root, kit, force=False, vendor_dir=None):
        installed.append((kit, force))
        spec_kits.record(root, {"kit": kit, "version": "test", "also_present": "spec-kit"})
        return {"verdict": "PASS"}
    monkeypatch.setattr(spec_kits, "init", fake_init)
    spec_kits.record(project, {"kit": "spec-kit", "version": "test"})
    _feature(project)
    rc, out = _cli(["start", NOTES, "--prepare", "--json"], capsys)
    assert rc == 0, out
    assert installed == [("openspec", True)]
    moved = os.path.join(project, "openspec", "specs", "link-shortener", "spec.md")
    assert os.path.isfile(moved)
    assert any("wrote openspec/specs/link-shortener/spec.md" in p for p in out["prepared"])
    items = {i["alias"]: i for i in runbook.parse_spec(os.path.join(project, "openspec", "specs"))["items"]}
    assert sorted(k for k, i in items.items() if i["required"]) == ["SC-001", "SC-002", "SC-003", "SC-004"]
    assert sorted(k for k, i in items.items() if not i["required"])[:1] == ["FR-001"]
    rec = spec_kits.recorded(project)
    assert rec["kit"] == "openspec" and rec["moved_from"]["specs"] == ["specs/001-link-shortener/spec.md"]
    conn = db.connect(project)
    ev = db.events(conn, kind="start")[-1]
    assert ev["data"]["kit"] == "openspec" and ev["data"]["mode"] == "change"
    # a second prepare never writes over the moved spec
    with open(moved, "a", encoding="utf-8") as fh:
        fh.write("\n<!-- edited -->\n")
    rc, out = _cli(["start", NOTES, "--prepare", "--json"], capsys)
    assert rc == 0 and any("already moved" in p for p in out["prepared"])
    assert "<!-- edited -->" in open(moved, encoding="utf-8").read()


def test_the_moved_spec_passes_the_example_runbook(project):
    from alpaca import runbook, start
    text = start.moved_spec_text(os.path.join(EXAMPLE, "spec.md"), "templates/runbook-example/spec.md")
    path = os.path.join(project, "openspec", "specs", "link-shortener", "spec.md")
    os.makedirs(os.path.dirname(path))
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    result = runbook.check(os.path.join(EXAMPLE, "runbook.yaml"), os.path.join(project, "openspec", "specs"))
    assert result["verdict"] == "PASS", result["errors"]
    assert "The system SHALL meet success criterion SC-002." in text


def test_prepare_after_the_move_does_not_move_again(project, capsys, monkeypatch):
    """L7: once moved_from is recorded, --prepare leaves the moved specs and the spec block alone."""
    from alpaca import spec_kits

    def fake_init(root, kit, force=False, vendor_dir=None):
        spec_kits.record(root, {"kit": kit, "version": "test", "also_present": "spec-kit"})
        return {"verdict": "PASS"}
    monkeypatch.setattr(spec_kits, "init", fake_init)
    spec_kits.record(project, {"kit": "spec-kit", "version": "test"})
    _feature(project)
    rc, out = _cli(["start", NOTES, "--prepare", "--json"], capsys)
    assert rc == 0, out
    with open(os.path.join(project, "project.yaml"), encoding="utf-8") as fh:
        before = fh.read()
    rc, out = _cli(["start", NOTES, "--prepare", "--json"], capsys)
    assert rc == 0, out
    assert any("already moved" in p for p in out["prepared"]), out["prepared"]
    assert not any("kept the existing" in p for p in out["prepared"])
    with open(os.path.join(project, "project.yaml"), encoding="utf-8") as fh:
        assert fh.read() == before


def test_two_features_with_one_capability_name_are_refused(project, capsys, monkeypatch):
    """L7: 001-link-shortener and 002-link-shortener both map to link-shortener; the move refuses
    and writes nothing, instead of moving only the first."""
    from alpaca import spec_kits

    def fake_init(root, kit, force=False, vendor_dir=None):
        spec_kits.record(root, {"kit": kit, "version": "test", "also_present": "spec-kit"})
        return {"verdict": "PASS"}
    monkeypatch.setattr(spec_kits, "init", fake_init)
    spec_kits.record(project, {"kit": "spec-kit", "version": "test"})
    _feature(project)
    dst = os.path.join(project, "specs", "002-link-shortener")
    os.makedirs(dst)
    shutil.copy(os.path.join(EXAMPLE, "spec.md"), os.path.join(dst, "spec.md"))
    rc, out = _cli(["start", NOTES, "--prepare", "--json"], capsys)
    assert rc == 2 and "link-shortener" in out["reason"], out
    assert not os.path.exists(os.path.join(project, "openspec", "specs", "link-shortener"))


# ------------------------------------------------------------------ the interview comes first
def _signed_interview(project, capsys):
    rc, note = _cli(["note", "add", NOTES, "--json"], capsys)
    assert rc == 0, note
    from alpaca import interview
    for slot in interview.slot_ids(project):
        rc, out = _cli(["interview", "set", slot, "--answered", "--value", "v " + slot,
                        "--source", note["file"]], capsys)
        assert rc == 0, out
    rc, out = _cli(["interview", "signoff", "--by", "the operator", "--json"], capsys)
    assert rc == 0, out
    return out["file"]


def test_raw_notes_start_with_the_inbox_and_the_interview(project, capsys):
    notes = os.path.join(project, "notes.md")
    with open(notes, "w", encoding="utf-8") as fh:
        fh.write(NOTES + "\n")
    rc, out = _cli(["start", notes, "--json"], capsys)
    assert rc == 0, out
    assert out["interview"] == "needed"
    assert [s["step"] for s in out["steps"]][:2] == ["note", "interview"]
    assert out["steps"][0]["command"] == "alpaca note add --file %s" % notes
    assert "/alpaca-interview" in out["steps"][1]["command"]
    assert "alpaca interview signoff" in out["steps"][1]["command"]
    rc, text = _cli(["start", NOTES], capsys)
    assert rc == 0 and "interview: needed" in text
    assert "alpaca note add" in text and "/alpaca-interview" in text


def test_a_signed_interview_goes_straight_to_the_spec_and_a_later_change_makes_it_stale(project, capsys):
    signed = _signed_interview(project, capsys)
    rc, out = _cli(["start", NOTES, "--json"], capsys)
    assert rc == 0, out
    assert out["interview"] == "signed"
    names = [s["step"] for s in out["steps"]]
    assert names == ["prepare", "spec", "clarify", "runbook", "op", "intake"]
    spec = [s for s in out["steps"] if s["step"] == "spec"][0]
    assert signed in spec["do"]
    rc, _ = _cli(["interview", "set", "thresholds", "--answered", "--value", "p95 40 ms",
                  "--source", "round:1/q1"], capsys)
    assert rc == 0
    rc, out = _cli(["start", NOTES, "--json"], capsys)
    assert rc == 0 and out["interview"] == "stale"
    assert [s["step"] for s in out["steps"]][:2] == ["note", "interview"]
    assert "stale" in out["steps"][1]["do"]


# ------------------------------------------------------------------ review and trial fixes (op-003)
def test_new_notes_after_a_signoff_go_to_the_inbox_and_the_interview(project, capsys):
    """Raw notes that are not in the inbox yet are new input: start keeps them and sends them
    through the interview again, instead of the earlier signed brief."""
    _signed_interview(project, capsys)
    new = "second change: add CSV export, must never leak other users' links"
    rc, out = _cli(["start", new, "--json"], capsys)
    assert rc == 0, out
    assert out["interview"] == "stale", out["interview"]
    assert [s["step"] for s in out["steps"]][:2] == ["note", "interview"]
    assert out["steps"][0]["command"] == "alpaca note add \"<the notes>\""
    assert "new notes" in out["steps"][1]["do"]
    # once kept, the new note alone makes the sign-off stale
    rc, _ = _cli(["note", "add", new], capsys)
    rc, out = _cli(["start", new, "--json"], capsys)
    assert out["interview"] == "stale" and "new notes" in out["steps"][1]["do"]


def test_a_note_from_the_inbox_is_not_added_again(project, capsys):
    rc, note = _cli(["note", "add", NOTES, "--json"], capsys)
    assert rc == 0, note
    rc, out = _cli(["start", os.path.join(project, note["file"]), "--json"], capsys)
    assert rc == 0, out
    step = out["steps"][0]
    assert step["step"] == "note" and "note add" not in step["command"], step
    assert note["file"] in step["do"]


def test_the_signed_brief_as_the_notes_keeps_the_signoff(project, capsys):
    signed = _signed_interview(project, capsys)
    rc, out = _cli(["start", os.path.join(project, signed), "--json"], capsys)
    assert rc == 0 and out["interview"] == "signed", out
    assert [s["step"] for s in out["steps"]][0] == "prepare"


def test_a_rerun_with_the_same_notes_keeps_the_first_pick(project, capsys, monkeypatch):
    """After `start <brief> --prepare` picked spec-kit and the spec was written from the brief, a
    rerun with the same brief keeps the pick instead of calling it a change to move to OpenSpec."""
    from alpaca import spec_kits
    monkeypatch.setattr(spec_kits, "init", lambda root, kit, force=False: None)
    signed = _signed_interview(project, capsys)
    brief = os.path.join(project, signed)
    rc, out = _cli(["start", brief, "--prepare", "--json"], capsys)
    assert rc == 0 and out["kit"] == "spec-kit" and out["mode"] == "new", out
    _feature(project)                                  # the spec written from the brief
    rc, out = _cli(["start", brief, "--json"], capsys)
    assert rc == 0, out
    assert (out["kit"], out["mode"]) == ("spec-kit", "new"), out["reason"]
    assert "started before" in out["reason"] and "specs/001-link-shortener/spec.md" in out["reason"]
    # other notes are still a change
    rc, out = _cli(["start", "a new change: add CSV export", "--json"], capsys)
    assert (out["kit"], out["mode"]) == ("openspec", "change")
