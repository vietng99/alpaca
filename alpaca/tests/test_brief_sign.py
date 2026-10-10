"""op-019 t-201 (design 3.1): alpaca brief present, sign and show; the signed copy under
.alpaca/briefs/<op>/<sha256>.md; sign --decision."""
import hashlib
import io
import os
from contextlib import redirect_stdout

import pytest

from alpaca import brief, cli, db, decisions, ownerword
from alpaca.tests.ownerword_fixtures import Project, typed

TEXT = "# Brief\n\nSilence is never consent.\n"
SHA = hashlib.sha256(TEXT.encode()).hexdigest()


@pytest.fixture
def p(tmp_path, monkeypatch):
    proj = Project(tmp_path, monkeypatch)
    proj.op()
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", proj.sid)
    with open(os.path.join(proj.root, "brief.md"), "w") as fh:
        fh.write(TEXT)
    return proj


def run(p, monkeypatch, *argv):
    monkeypatch.chdir(p.root)
    out = io.StringIO()
    with redirect_stdout(out):
        code = cli.main(list(argv))
    return code, out.getvalue()


def test_present_copies_the_brief_records_the_ask_and_prints_the_file(p, monkeypatch):
    code, out = run(p, monkeypatch, "brief", "present", "op-1", "brief.md")
    assert code == 0 and "Silence is never consent." in out and "%s signed" % SHA[:12] in out
    copy = os.path.join(p.root, ".alpaca", "briefs", "op-1", SHA + ".md")
    assert open(copy).read() == TEXT
    ask = ownerword.latest_ask(p.conn, "brief:op-1:" + SHA[:12])
    assert ask["session"] == p.sid and ask["op"] == "op-1"


def test_sign_needs_the_owners_message_after_the_present(p, monkeypatch):
    code, out = run(p, monkeypatch, "brief", "sign", "op-1", "brief.md", "--by", "owner",
                    "--quote", "%s signed" % SHA[:12])
    assert code == cli.FAIL and "OWNER-WORD-UNBOUND" in out           # never presented
    run(p, monkeypatch, "brief", "present", "op-1", "brief.md")
    p.write([typed("sounds fine to me")])
    code, out = run(p, monkeypatch, "brief", "sign", "op-1", "brief.md", "--by", "owner",
                    "--quote", "sounds fine to me")
    assert code == cli.FAIL and "OWNER-WORD-UNBOUND" in out           # a reply that moves on
    code, out = run(p, monkeypatch, "brief", "sign", "op-1", "brief.md", "--by", "owner",
                    "--quote", "%s signed" % SHA[:12])
    assert code == cli.FAIL and "OWNER-WORD-NOT-FOUND" in out         # never typed
    assert brief.signed_events(p.conn) == []
    p.write([typed("%s signed" % SHA[:12])])
    code, out = run(p, monkeypatch, "brief", "sign", "op-1", "brief.md", "--by", "owner",
                    "--quote", "%s signed" % SHA[:12])
    assert code == 0, out
    ev = brief.signed_events(p.conn, "op-1")[0]
    assert ev["sha256"] == SHA and ev["file"] == "brief.md" and ev["owner_word"]["line"] == 1
    assert ev["copy"] == os.path.join(".alpaca", "briefs", "op-1", SHA + ".md")


def test_an_agent_cannot_sign_with_a_non_human_identity(p, monkeypatch):
    run(p, monkeypatch, "brief", "present", "op-1", "brief.md")
    p.write([typed("%s signed" % SHA[:12])])
    code, out = run(p, monkeypatch, "brief", "sign", "op-1", "brief.md", "--by", "agent",
                    "--quote", "%s signed" % SHA[:12])
    assert code == cli.FAIL and "OWNER-WORD-NOT-HUMAN" in out


def test_present_and_sign_refuse_a_session_that_differs_from_the_environment(p, monkeypatch):
    code, out = run(p, monkeypatch, "--session", "fresh-x", "brief", "present", "op-1", "brief.md")
    assert code == cli.FAIL and "OWNER-WORD-SESSION" in out
    assert ownerword.latest_ask(p.conn, "brief:op-1:" + SHA[:12]) is None


def test_show_reports_changed_after_a_later_edit_and_the_signed_copy_stays(p, monkeypatch):
    run(p, monkeypatch, "brief", "present", "op-1", "brief.md")
    p.write([typed("%s signed" % SHA[:12])])
    run(p, monkeypatch, "brief", "sign", "op-1", "brief.md", "--by", "owner",
        "--quote", "%s signed" % SHA[:12])
    code, out = run(p, monkeypatch, "brief", "show", "op-1")
    assert "unchanged" in out and SHA[:12] in out
    with open(os.path.join(p.root, "brief.md"), "a") as fh:
        fh.write("an edit after signing\n")
    code, out = run(p, monkeypatch, "brief", "show", "op-1")
    assert "changed" in out and "unchanged" not in out
    assert brief.signed_text(p.conn, p.root, "brief.md")[0] == TEXT


def test_unknown_op_is_refused(p, monkeypatch):
    code, out = run(p, monkeypatch, "brief", "present", "op-9", "brief.md")
    assert code == cli.FAIL and "no op op-9" in out


def _owner_decision(p, context):
    did = decisions.preview_id("intent", context, ["sign"], "sign", "go", "ptr")
    p.ask(did)
    p.write([typed("%s yes" % did)])
    word = p.write_as(did, "yes", "%s yes" % did, since=ownerword.ask_ts(p.conn, did))
    return decisions.record(p.conn, "intent", context, ["sign"], "sign", "go", "ptr",
                            actor="owner", session=p.sid, root=p.root, owner_word=word)["id"]


def test_sign_with_a_decision_binds_an_owner_proved_decision_that_names_the_sha(p, monkeypatch):
    dec = _owner_decision(p, "the owner signs brief.md sha256 %s" % SHA)
    code, out = run(p, monkeypatch, "brief", "sign", "op-1", "brief.md", "--decision", dec)
    assert code == 0, out
    ev = brief.signed_events(p.conn, "op-1")[0]
    assert ev["decision"] == dec and "owner_word" not in ev
    assert os.path.isfile(os.path.join(p.root, ev["copy"]))


def test_sign_with_a_decision_refuses_a_token_decision_and_a_wrong_sha(p, monkeypatch):
    token = decisions.record(p.conn, "intent", "signs sha256 %s" % SHA, ["x"], "x", "y", "ptr",
                             actor="owner", session=p.sid, root=p.root)
    code, out = run(p, monkeypatch, "brief", "sign", "op-1", "brief.md", "--decision", token["id"])
    assert code == cli.FAIL and "not an owner-proved decision" in out
    dec = _owner_decision(p, "the owner signs brief.md sha256 %s" % ("0" * 64))
    code, out = run(p, monkeypatch, "brief", "sign", "op-1", "brief.md", "--decision", dec)
    assert code == cli.FAIL and "does not name the sha256" in out
    assert brief.signed_events(p.conn) == []


def test_owner_ask_and_decide_quote_make_an_owner_proved_decision(p, monkeypatch):
    fields = ["--kind", "go", "--context", "ctx", "--option", "a", "--choice", "a",
              "--consequence", "does a", "--pointer", "ptr"]
    did = decisions.preview_id("go", "ctx", ["a"], "a", "does a", "ptr")
    code, out = run(p, monkeypatch, "decide", *fields, "--by", "owner", "--quote", "%s yes" % did)
    assert code != 0 and "OWNER-WORD-NOT-FOUND" in out
    code, out = run(p, monkeypatch, "owner", "ask", "decision", *fields)
    assert code == 0 and did in out
    p.write([typed("%s yes" % did)])
    code, out = run(p, monkeypatch, "decide", *fields, "--by", "agent", "--quote", "%s yes" % did)
    assert code != 0 and "OWNER-WORD-NOT-HUMAN" in out
    code, out = run(p, monkeypatch, "decide", *fields, "--by", "owner", "--quote", "%s yes" % did)
    assert code == 0, out
    assert ownerword.decision_is_owner(p.conn, did, p.root) is True
    # without --quote a decision is written as before and is not owner-proved
    other = ["--kind", "go", "--context", "ctx2", "--option", "a", "--choice", "a",
             "--consequence", "x", "--pointer", "p"]
    code, out = run(p, monkeypatch, "decide", *other, "--by", "owner")
    assert code == 0
    assert ownerword.decision_is_owner(
        p.conn, decisions.preview_id("go", "ctx2", ["a"], "a", "x", "p"), p.root) is False
