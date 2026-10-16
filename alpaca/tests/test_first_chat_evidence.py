"""What doctor and `onboard show` name when the record or the setup files do not add up (op-062 t-656, fix round 3;
design/attach-mode/onboarding-design.md 7.7).

An agent that hides itself or writes the record, the meta table, a transcript or a setup file directly is a declared
residual: no code on one machine stops it. These tests make each forgery the way the cause and premortem probes did
(LOGS/think/redesign-1-cause/test_cause_probes.py, LOGS/think/redesign-1-premortem/test_premortem.py) and check that
the line that names it appears. Nothing here is a refusal.
"""
from __future__ import annotations

import os
import time

import pytest

from alpaca import adopt, cli, db, onboard_flow
from alpaca.tests.attachkit import upstream  # noqa: F401
from alpaca.tests.onboardchat import Chat, chat  # noqa: F401
from alpaca.tests.test_onboard_attacks import confirmed


def _doctor(c):
    rc, out = c.run("doctor")
    return [l for l in out.splitlines() if "first-chat" in l]


def _warns(c):
    return [l for l in _doctor(c) if l.lstrip().startswith("WARN") and "first-chat-evidence" in l]


def _terminal_confirm(c, cid):
    """F1 P1c / cause S2 as a module call: the yes proved at a terminal (no session, a terminal, the id typed)."""
    ok, lines = onboard_flow.confirm_verb(c.conn, c.root, "cli", cid, quote=cid, environ={}, isatty=lambda: True,
                                          read=lambda prompt="": cid)
    return ok, lines


# ---------------------------------------------------------------- 1. a state with no event behind it (cause S1, R1)

def test_a_skip_written_only_into_meta_is_named_and_the_ok_line_is_gone(chat, upstream):
    chat.look(upstream.url)
    assert not _warns(chat)
    db.meta_set(chat.conn, adopt.FIRST_CHAT, "skipped")
    chat.conn.commit()
    lines = _doctor(chat)
    assert any("marked skipped, but the record has no onboard-skipped event" in l for l in lines), lines
    assert not any("skipped by the owner" in l for l in lines), lines


def test_a_real_skip_keeps_the_ok_line_and_names_nothing(chat, upstream):
    chat.look(upstream.url)
    chat.say("skip setup")
    assert chat.run("onboard", "skip", "--quote", "skip setup")[0] == 0
    lines = _doctor(chat)
    assert any("skipped by the owner" in l for l in lines) and not _warns(chat), lines


@pytest.mark.parametrize("state,event", [("confirmed", "onboard-confirmed"), ("done", "onboard-applied")])
def test_a_confirmed_or_done_mark_with_no_event_is_named(chat, upstream, state, event):
    chat.look(upstream.url)
    db.meta_set(chat.conn, adopt.FIRST_CHAT, state)
    chat.conn.commit()
    assert any("marked %s, but the record has no %s event" % (state, event) in l for l in _warns(chat)), _doctor(chat)


# ---------------------------------------------------------------- 2. a terminal yes in a Claude Code chat (S2, R2)

def test_a_yes_typed_at_a_terminal_while_the_chat_ran_in_claude_code_is_named(chat, upstream):
    cid, _card = chat.walk_to_card(upstream.url)
    ok, _lines = _terminal_confirm(chat, cid)
    assert ok
    warns = _warns(chat)
    assert any("was typed at a terminal, while this first chat ran in a Claude Code chat (session chat-1)" in l
               for l in warns), warns
    rc, out = chat.run("onboard", "show")
    assert "check: The owner's yes" in out and "typed at a terminal" in out, out


def test_a_module_written_confirm_row_with_a_terminal_word_is_named(chat, upstream):
    """Cause S2: one `python -c` appends the row through the module, no terminal at all."""
    cid, _card = chat.walk_to_card(upstream.url)
    ev = onboard_flow._card_event(chat.conn, cid)
    word = {"path": "terminal", "transcript": None, "line": None, "timestamp": chat.now(), "quote": cid}
    onboard_flow._append(chat.conn, "cli", "onboard-confirmed", {"card": cid, "sha256": ev["data"]["sha256"],
                                                                 "owner_word": word, "form": "terminal"}, actor="owner",
                         ref=cid)
    db.meta_set(chat.conn, adopt.FIRST_CHAT, "confirmed")
    chat.conn.commit()
    assert any("typed at a terminal" in l for l in _warns(chat)), _doctor(chat)


def test_a_terminal_skip_with_no_hook_registered_chat_is_quiet(project, monkeypatch):
    """The Codex route: the first chat's steps were written by a session with no recorded transcript."""
    from alpaca.tests.onboardchat import make_due_studio
    root = os.path.realpath(str(project))
    make_due_studio(root)
    conn = db.connect(root)
    lines = onboard_flow._answer_opening(conn, root, "codex-1", "a new project please", "a new project please", {})
    assert lines
    monkeypatch.setattr("builtins.input", lambda prompt="": "onboard-skip")
    onboard_flow.skip_verb(conn, root, "cli", "onboard-skip", environ={}, isatty=lambda: True)
    assert adopt.first_chat_state(conn) == "skipped"
    assert not [t for lvl, t in onboard_flow.evidence_lines(conn, root) if lvl == "warn"]


# ---------------------------------------------------------------- 3. a session-path word that does not read back (S5)

def test_a_confirm_row_that_points_at_a_line_the_owner_never_typed_is_named(chat, upstream):
    cid, _card = chat.walk_to_card(upstream.url)
    ev = onboard_flow._card_event(chat.conn, cid)
    word = {"path": "session", "transcript": chat.transcript, "line": 2, "timestamp": chat.now(), "quote": "yes",
            "answer": "yes"}
    onboard_flow._append(chat.conn, "chat-1", "onboard-confirmed", {"card": cid, "sha256": ev["data"]["sha256"],
                                                                    "owner_word": word, "form": "sole"},
                         actor="owner", ref=cid)
    db.meta_set(chat.conn, adopt.FIRST_CHAT, "confirmed")
    chat.conn.commit()
    assert any("points at transcript line 2, which does not hold that message" in l for l in _warns(chat)), \
        _doctor(chat)


def test_a_real_yes_reads_back_and_a_removed_transcript_is_only_noted(chat, upstream):
    cid, _card = confirmed(chat, upstream.url)
    assert not _warns(chat), _doctor(chat)
    os.remove(chat.transcript)
    lines = _doctor(chat)
    assert not _warns(chat) and any("no longer on disk" in l for l in lines), lines


# ---------------------------------------------------------------- 4. setup files changed (P1a, P1b, P4)

def test_a_hand_edit_after_the_look_is_named_by_doctor_confirm_and_apply(chat, upstream):
    cid, _card = chat.walk_to_card(upstream.url)
    with open(os.path.join(chat.root, "project.yaml"), "a", encoding="utf-8") as fh:
        fh.write("# an edit nobody asked for\n")
    os.makedirs(os.path.join(chat.root, "app"))
    warns = _warns(chat)
    assert any("These files changed after the first chat began: project.yaml, app/." in l for l in warns), warns
    chat.say("yes")
    rc, out = chat.run("onboard", "confirm", cid, "--quote", "yes")
    assert rc == 0 and "these files changed after the look and before this yes: project.yaml, app/" in out, out
    assert chat.events("onboard-confirmed")[-1]["data"]["setup_changed"] == ["project.yaml", "app/"]


def test_an_untouched_studio_names_nothing_and_confirm_stores_an_empty_list(chat, upstream):
    cid, _card = confirmed(chat, upstream.url)
    assert chat.events("onboard-confirmed")[-1]["data"]["setup_changed"] == []
    rc, out = chat.run("onboard", "apply", cid)
    assert rc == 0, out
    first = [e for e in chat.events("onboard-applied")][0]
    assert first["data"]["setup_changed"] == [] and "changed after the look" not in out


def _edit_then_look_again(c, url):
    c.look(url)
    with open(os.path.join(c.root, "project.yaml"), "a", encoding="utf-8") as fh:
        fh.write("# edited between two looks\n")
    c.look(url)                                     # a new epoch: its opening must not become the baseline
    return [l for l in _warns(c) if "changed after the first chat began" in l]


def test_a_second_look_does_not_launder_an_edit(chat, upstream):
    assert _edit_then_look_again(chat, upstream.url)


def test_mutation_control_a_baseline_from_the_newest_opening_launders_the_edit(chat, upstream, monkeypatch):
    def newest(conn):
        rows = [r for r in onboard_flow._rows(conn, "onboard-answer") if r["data"].get("slot") == "opening"]
        return rows[-1] if rows else None

    monkeypatch.setattr(onboard_flow, "_baseline", newest)
    assert not _edit_then_look_again(chat, upstream.url), "the mutation went unseen: the earliest-baseline case " \
                                                          "tests nothing"


def test_a_changed_baseline_file_is_named(chat, upstream):
    chat.look(upstream.url)
    base = chat.events("onboard-answer")[0]["data"]["setup"]
    with open(os.path.join(chat.root, base["file"]), "w", encoding="utf-8") as fh:
        fh.write('{"v": 1, "files": {}, "app": false, "capped": false}')
    assert any("is missing or changed" in l for l in _warns(chat)), _doctor(chat)


def test_the_opening_records_the_baseline_and_the_chain_holds_it(chat, upstream):
    chat.look(upstream.url)
    opening = [e for e in chat.events("onboard-answer") if e["data"].get("slot") == "opening"][0]
    base = opening["data"]["setup"]
    assert base["file"].startswith(os.path.join(".alpaca", "onboard", "setup")) and len(base["sha256"]) == 64
    assert base["files"] > 0 and base["app"] is False
    assert cli.main(["verify"]) == 0


# ---------------------------------------------------------------- the digest never blocks and never follows a link

def test_the_digest_finishes_at_once_on_a_pipe_a_link_to_a_device_and_a_big_file(tmp_path):
    root = tmp_path / "studio"
    root.mkdir()
    os.mkfifo(root / "pipe")
    os.symlink("/dev/zero", root / "zero")
    with open(root / "big.bin", "wb") as fh:
        fh.write(b"x" * (10 << 20))
    (root / "a.txt").write_text("a")
    (root / ".alpaca").mkdir()
    (root / ".alpaca" / "inner").write_text("not read")
    (root / "RESUME.md").write_text("a view")
    start = time.monotonic()
    d = onboard_flow.setup_digest(str(root))
    assert time.monotonic() - start < 5
    assert d["files"]["pipe"] == "fifo" and d["files"]["zero"] == "link:/dev/zero"
    assert len(d["files"]["big.bin"]) == 16 and "a.txt" in d["files"]
    assert not any(k.startswith(".alpaca") for k in d["files"]) and "RESUME.md" not in d["files"]


def test_the_digest_records_by_size_and_time_past_its_cap(tmp_path, monkeypatch):
    root = tmp_path / "studio"
    root.mkdir()
    for i in range(5):
        (root / ("f%d.txt" % i)).write_text("x")
    monkeypatch.setattr(onboard_flow, "DIGEST_MAX_FILES", 2)
    d = onboard_flow.setup_digest(str(root))
    assert d["capped"] and sum(1 for v in d["files"].values() if v.startswith("stat:")) == 3


# ---------------------------------------------------------------- doctor keeps its other rows when the check fails

def test_an_error_in_the_evidence_check_is_one_warning(chat, upstream, monkeypatch):
    chat.look(upstream.url)

    def broken(conn, root):
        raise RuntimeError("boom")

    monkeypatch.setattr(onboard_flow, "evidence_lines", broken)
    rc, out = chat.run("doctor")
    assert "first-chat evidence check failed: RuntimeError: boom" in out
    assert "first chat not done" in out, out


def test_a_linked_onboard_folder_keeps_the_baseline_inside_the_studio_and_doctor_names_it(chat, upstream, tmp_path):
    """Frozen attack B11 (LOGS/attacks/test_adv_r1_flow.py:318): .alpaca/onboard is a link to a folder outside the
    studio. The look goes on, nothing is written outside, and doctor says the baseline could not be kept."""
    outside = tmp_path / "outside"
    outside.mkdir()
    os.symlink(str(outside), os.path.join(chat.root, ".alpaca", "onboard"))
    chat.look(upstream.url)
    assert [f for _d, _s, fs in os.walk(str(outside)) for f in fs] == []
    opening = [e for e in chat.events("onboard-answer") if e["data"].get("slot") == "opening"][0]
    assert "error" in opening["data"]["setup"] and "file" not in opening["data"]["setup"]
    assert any("could not be kept when the first chat began" in l for l in _warns(chat)), _doctor(chat)
