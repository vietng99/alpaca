"""op-019 t-201 (design section 2, brief P2): the origin label of every contract line, the
owner-quote check against its source bytes, and the agent session as the actor of a task write."""
import io
import json
import os
from contextlib import redirect_stdout

import pytest

from alpaca import brief, cli, db, decisions, ownerword, profile, taskcontract
from alpaca.tests.ownerword_fixtures import Project, typed

BRIEF = "# Brief\n\nThe owner signs this. Silence is never consent.\n\n- D3. Unanswered means no.\n"


@pytest.fixture
def p(tmp_path, monkeypatch):
    monkeypatch.setattr(profile, "load", lambda root: profile.Profile())
    proj = Project(tmp_path, monkeypatch)
    proj.op()
    db.upsert(proj.conn, "tasks", "id", {"id": "t-001", "op": "op-1", "statement": "s", "title": "T",
                                         "status": "open", "created": "2000-01-01T00:00:00+00:00"})
    return proj


def agent_session(p, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", p.sid)


def sign_brief(p, monkeypatch, name="brief.md"):
    """Present and sign BRIEF through an owner message; returns the file's repo-relative path."""
    agent_session(p, monkeypatch)
    with open(os.path.join(p.root, name), "w") as fh:
        fh.write(BRIEF)
    sha, _ = brief.present(p.conn, p.root, p.sid, "op-1", name)
    p.write([typed("%s signed" % sha[:12])])
    brief.sign_with_quote(p.conn, p.root, p.sid, "op-1", name, "owner", "%s signed" % sha[:12])
    return name


def record(p, data, **kw):
    kw.setdefault("session", "s")
    kw.setdefault("actor", "a")
    kw.setdefault("root", p.root)
    return taskcontract.record(p.conn, "t-001", data, **kw)


def run(p, monkeypatch, *argv, session=None):
    monkeypatch.chdir(p.root)
    if session:
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", session)
    out = io.StringIO()
    with redirect_stdout(out):
        code = cli.main(list(argv))
    return code, out.getvalue()


# ---- storage and show
def test_origins_are_stored_per_line_aligned_with_the_lines(p):
    record(p, {"done_bar": ["a", "b"], "input": [{"text": "c", "origin": "derived",
                                                  "reason": "restates brief D3"}],
               "expected": [{"text": "d", "origin": "agent-proposed"}]})
    cur = taskcontract.latest(p.conn)["t-001"]
    assert cur["done_bar"] == ["a", "b"] and cur["input"] == ["c"]
    assert taskcontract.origins_of(cur)["done_bar"] == [{"kind": "agent-proposed"}] * 2
    assert cur["origins"]["input"] == [{"kind": "derived", "reason": "restates brief D3"}]
    assert cur["origins"]["expected"] == [{"kind": "agent-proposed"}]
    assert cur["origins"]["fail_cases"] == []


def test_a_missing_origin_is_stored_as_agent_proposed(p):
    record(p, {"done_bar": ["x"]})
    cur = taskcontract.latest(p.conn)["t-001"]
    # no origin key is written for an all-default contract (the shape research intake pins); it
    # reads as agent-proposed on every line
    assert "origins" not in cur
    assert taskcontract.origins_of(cur)["done_bar"] == [{"kind": "agent-proposed"}]
    assert "[agent-proposed] x" in taskcontract.show_text(cur)


def test_show_prints_each_line_with_its_origin_and_the_lens_rule(p):
    record(p, {"done_bar": ["x", {"text": "y", "origin": "derived=because the brief says so"}]})
    text = taskcontract.show_text(taskcontract.latest(p.conn)["t-001"])
    assert "done_bar[0] [agent-proposed] x" in text
    assert "done_bar[1] [derived=because the brief says so] y" in text
    assert text.rstrip().endswith(taskcontract.LENS_RULE)
    assert "never owner authority" in taskcontract.show_text(None)


@pytest.mark.parametrize("origin, message", [
    ("owner-quote", "needs a source"), ("decision", "needs a ref"), ("derived", "needs a reason"),
    ("invented", "origin must be one of"), ("derived=" + "x" * 201, "1 to 200"),
])
def test_bad_origins_are_refused(p, origin, message):
    with pytest.raises(ValueError, match=message):
        record(p, {"done_bar": ["x"], "origin": origin})
    assert taskcontract.latest(p.conn) == {}


# ---- owner-quote against its source bytes
def test_owner_quote_of_the_signed_brief_is_stored(p, monkeypatch):
    name = sign_brief(p, monkeypatch)
    record(p, {"done_bar": ["Silence is never consent.", "D3. Unanswered means no."],
               "origin": "owner-quote=" + name})
    origins = taskcontract.latest(p.conn)["t-001"]["origins"]["done_bar"]
    assert origins == [{"kind": "owner-quote", "source": name}] * 2


def test_owner_quote_that_is_not_in_the_signed_copy_is_refused(p, monkeypatch):
    name = sign_brief(p, monkeypatch)
    with pytest.raises(ValueError, match="QUOTE-SOURCE-MISMATCH"):
        record(p, {"done_bar": ["Silence is sometimes consent."], "origin": "owner-quote=" + name})
    assert taskcontract.latest(p.conn) == {}      # the whole contract is refused


def test_owner_quote_reads_the_signed_copy_not_the_live_file(p, monkeypatch):
    name = sign_brief(p, monkeypatch)
    with open(os.path.join(p.root, name), "a") as fh:
        fh.write("\n- D9. A line added after signing.\n")
    with pytest.raises(ValueError, match="QUOTE-SOURCE-MISMATCH"):
        record(p, {"done_bar": ["D9. A line added after signing."], "origin": "owner-quote=" + name})
    record(p, {"done_bar": ["D3. Unanswered means no."], "origin": "owner-quote=" + name})


def test_owner_quote_of_an_unsigned_file_is_refused(p):
    with open(os.path.join(p.root, "draft.md"), "w") as fh:
        fh.write(BRIEF)
    with pytest.raises(ValueError, match="QUOTE-SOURCE-UNSIGNED"):
        record(p, {"done_bar": ["Silence is never consent."], "origin": "owner-quote=draft.md"})


def test_a_tampered_signed_copy_is_not_a_signed_brief(p, monkeypatch):
    name = sign_brief(p, monkeypatch)
    ev = brief.signed_events(p.conn)[-1]
    with open(os.path.join(p.root, ev["copy"]), "a") as fh:
        fh.write("- D9. Added to the copy.\n")
    with pytest.raises(ValueError, match="QUOTE-SOURCE-UNSIGNED"):
        record(p, {"done_bar": ["Silence is never consent."], "origin": "owner-quote=" + name})


def test_owner_quote_of_a_session_message(p):
    p.add_session("chat-9")
    p.write([typed("so it cannot invent new spec and call it a win right")], sid="chat-9")
    record(p, {"done_bar": ["cannot invent new spec"], "origin": "owner-quote=session:chat-9"})
    with pytest.raises(ValueError, match="QUOTE-SOURCE-MISMATCH"):
        record(p, {"done_bar": ["can invent new spec"], "origin": "owner-quote=session:chat-9"})
    with pytest.raises(ValueError, match="QUOTE-SOURCE-UNSIGNED"):
        record(p, {"done_bar": ["x"], "origin": "owner-quote=session:no-such-session"})


def test_owner_quote_of_an_owner_proved_decision(p, monkeypatch):
    agent_session(p, monkeypatch)
    did = decisions.preview_id("go", "c", ["a"], "a", "q", "ptr")
    p.ask(did)
    p.write([typed("%s yes" % did)])
    word = p.write_as(did, "yes", "%s yes" % did, since=ownerword.ask_ts(p.conn, did))
    decisions.record(p.conn, "go", "c", ["a"], "a", "q", "ptr", actor="owner", session=p.sid,
                     root=p.root, owner_word=word)
    record(p, {"done_bar": ["yes"], "origin": "owner-quote=dec:" + did})
    with pytest.raises(ValueError, match="QUOTE-SOURCE-MISMATCH"):
        record(p, {"done_bar": ["no way"], "origin": "owner-quote=dec:" + did})
    token = decisions.record(p.conn, "go", "c2", ["a"], "a", "q", "ptr2", actor="owner",
                             session=p.sid, root=p.root)
    with pytest.raises(ValueError, match="QUOTE-SOURCE-UNSIGNED"):
        record(p, {"done_bar": ["ptr2"], "origin": "owner-quote=dec:" + token["id"]})


# ---- decision origin records the owner flag
def test_a_decision_origin_records_whether_the_decision_is_owner_proved(p, monkeypatch):
    agent_session(p, monkeypatch)
    token = decisions.record(p.conn, "go", "c", ["a"], "a", "q", "ptr", actor="owner",
                             session=p.sid, root=p.root)
    record(p, {"done_bar": ["x"], "origin": "decision=" + token["id"]})
    assert taskcontract.latest(p.conn)["t-001"]["origins"]["done_bar"] == [
        {"kind": "decision", "ref": token["id"], "owner": False}]
    did = decisions.preview_id("go", "c9", ["a"], "a", "q", "ptr")
    p.ask(did)
    p.write([typed("%s ok" % did)])
    word = p.write_as(did, "yes", "%s ok" % did, since=ownerword.ask_ts(p.conn, did))
    decisions.record(p.conn, "go", "c9", ["a"], "a", "q", "ptr", actor="owner", session=p.sid,
                     root=p.root, owner_word=word)
    record(p, {"done_bar": ["x"], "origin": "decision=" + did})
    assert taskcontract.latest(p.conn)["t-001"]["origins"]["done_bar"][0]["owner"] is True
    with pytest.raises(ValueError, match="ORIGIN-DECISION-UNKNOWN"):
        record(p, {"done_bar": ["x"], "origin": "decision=dec-000000000000"})


# ---- the CLI: flags, files, actors
def test_cli_origin_flag_and_file_objects(p, monkeypatch, tmp_path):
    code, _ = run(p, monkeypatch, "task", "contract", "t-001", "--done-bar", "a", "--origin",
                  "derived=restates brief D3")
    assert code == 0
    cur = taskcontract.latest(p.conn)["t-001"]
    assert cur["origins"]["done_bar"] == [{"kind": "derived", "reason": "restates brief D3"}]
    spec = tmp_path / "c.json"
    spec.write_text(json.dumps({"done_bar": ["p", {"text": "q", "origin": "derived",
                                                    "reason": "r"}]}))
    code, _ = run(p, monkeypatch, "task", "contract", "t-001", "--file", str(spec))
    assert code == 0
    assert [o["kind"] for o in taskcontract.origins_of(taskcontract.latest(p.conn)["t-001"])["done_bar"]] == [
        "agent-proposed", "derived"]
    code, out = run(p, monkeypatch, "task", "contract", "t-001", "--show")
    assert code == 0 and "done_bar[1] [derived=r] q" in out and "never owner authority" in out
    code, out = run(p, monkeypatch, "task", "contract", "t-001", "--show", "--json")
    assert json.loads(out)["origins"]["done_bar"][1]["reason"] == "r"


def test_cli_owner_quote_mismatch_refuses_the_task_add_before_the_task_exists(p, monkeypatch):
    sign_brief(p, monkeypatch)
    code, out = run(p, monkeypatch, "task", "add", "op-1", "stmt", "--title", "T2", "--done-bar",
                    "Silence is sometimes consent.", "--origin", "owner-quote=brief.md")
    assert code == cli.FAIL and "QUOTE-SOURCE-MISMATCH" in out
    assert len(db.rows(p.conn, "tasks")) == 1


def test_a_task_added_by_an_agent_session_is_recorded_with_that_session_as_actor(p, monkeypatch):
    code, _ = run(p, monkeypatch, "task", "add", "op-1", "stmt", "--title", "T2", "--done-bar", "x",
                  session="13de2353")
    assert code == 0
    add = db.events(p.conn, kind="task-add")[-1]
    assert add["actor"] == "13de2353" and add["actor"] != "human"
    contract = db.events(p.conn, kind="task-contract")[-1]
    assert contract["actor"] == "13de2353"
    code, _ = run(p, monkeypatch, "task", "contract", "t-001", "--done-bar", "y", session="13de2353")
    assert db.events(p.conn, kind="task-contract")[-1]["actor"] == "13de2353"
    code, _ = run(p, monkeypatch, "task", "title", "t-001", "New", session="13de2353")
    assert db.events(p.conn, kind="task-title")[-1]["actor"] == "13de2353"
    assert taskcontract.origins_of(taskcontract.latest(p.conn)["t-001"])["done_bar"] == [
        {"kind": "agent-proposed"}]


def test_a_person_at_a_plain_terminal_is_still_recorded_as_human(p, monkeypatch):
    monkeypatch.delenv("CLAUDECODE", raising=False)
    code, _ = run(p, monkeypatch, "task", "add", "op-1", "stmt", "--title", "T2")
    assert code == 0 and db.events(p.conn, kind="task-add")[-1]["actor"] == "human"


def test_by_owner_without_owner_words_is_refused(p, monkeypatch):
    code, out = run(p, monkeypatch, "task", "add", "op-1", "stmt", "--title", "T2", "--by", "owner",
                    session=p.sid)
    assert code == cli.FAIL and "OWNER-WORD-NOT-FOUND" in out
    code, out = run(p, monkeypatch, "task", "contract", "t-001", "--done-bar", "x", "--by", "owner",
                    "--quote", "t-001 yes", session=p.sid)
    assert code == cli.FAIL and "OWNER-WORD" in out
    code, out = run(p, monkeypatch, "task", "title", "t-001", "Nope", "--by", "owner", session=p.sid)
    assert code == cli.FAIL
    assert db.events(p.conn, kind="task-contract") == [] and db.events(p.conn, kind="task-title") == []
    assert len(db.rows(p.conn, "tasks")) == 1


def test_by_owner_with_the_owners_message_is_recorded_with_the_owner_word(p, monkeypatch):
    p.write([typed("t-002 yes")])       # the id the new task will get
    code, out = run(p, monkeypatch, "task", "add", "op-1", "stmt", "--title", "T2", "--by", "owner",
                    "--quote", "t-002 yes", session=p.sid)
    assert code == 0, out
    add = db.events(p.conn, kind="task-add")[-1]
    data = add["data"]
    assert add["actor"] == "owner" and data["owner_word"]["quote"] == "t-002 yes"
    p.write([typed("t-001 yes")])
    code, out = run(p, monkeypatch, "task", "contract", "t-001", "--done-bar", "x", "--by", "owner",
                    "--quote", "t-001 yes", session=p.sid)
    assert code == 0, out
    assert db.events(p.conn, kind="task-contract")[-1]["data"]["owner_word"]["line"] == 1
