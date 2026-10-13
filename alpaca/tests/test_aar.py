"""alpaca aar new / check / seal: the after-action report record (op-057 t-621)."""
import hashlib
import os
import random
import re
import time

import pytest

from alpaca import aar, cli, db, messages
from alpaca.tests import proofkit

OP = "op-001"
HEAD = "| action | owner | due | size | done-when | type |"
SEP = "| --- | --- | --- | --- | --- | --- |"
ROW = "| Pin the clock | agent | 2026-10-20 | S | ten green runs | fix |"
REASON = "the shout verb shipped with its tests green"
BASIS = "the verb prints upper case in the smoke run"
IN = [("op-001", "the op has started"), ("t-001", "a note on the task itself"),
      (None, "status of op-001. the verb works"), (None, "wrap-up (op-001) is next"),
      (None, "see op-001/t-001 for the diff")]
OUT = [("t-002", "a note on the other task"), ("op-002", "op-002 is unrelated work"),
       (None, "see op-0010, xop-001, op-001x and op-01 for nothing")]


@pytest.fixture(autouse=True)
def _no_review_gate(monkeypatch):
    """These tests seal without a review; test_aar_review.py covers the review gate."""
    monkeypatch.setattr(aar, "SEAL_HOOKS", [h for h in aar.SEAL_HOOKS if h is not aar.review_gate])


def run(capsys, *argv):
    capsys.readouterr()
    code = cli.main([str(a) for a in argv])
    out = capsys.readouterr()
    return code, out.out + out.err


def report(project, op=OP):
    return os.path.join(project, ".alpaca", "aar", "%s.md" % op)


def read(path):
    with open(path, encoding="utf-8", newline="") as fh:
        return fh.read()


def write(path, text):
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)


def closed_op(project, extra=()):
    assert cli.main(["init"]) == cli.PASS
    assert cli.main(["op", "new", "add the shout verb", "--done-when", "the verb prints upper case"]) == 0
    assert cli.main(["task", "add", "--title", "shout verb", OP, "write the shout verb",
                     "--phase", "build"]) == 0
    assert cli.main(["op", "new", "an unrelated op", "--done-when", "nothing to see"]) == 0
    assert cli.main(["task", "add", "--title", "other work", "op-002", "other work here",
                     "--phase", "build"]) == 0
    conn = db.connect(project)
    assert cli.main(["task", "move", "t-001", "doing", "--reason", "picked up after the note"]) == 0
    for ptr, body in list(IN) + list(OUT) + list(extra):
        messages.post(conn, "agent", ptr or "broadcast", "note", body, pointer=ptr)
    ptr = proofkit.seal_for(project, "t-001", conn=conn)
    assert cli.main(["task", "move", "t-001", "done", "--proof", ptr, "--reason", REASON]) == 0
    assert cli.main(["op", "close", OP, "--basis", BASIS]) == 0
    return conn


def event_id(conn, kind, **where):
    return [e["id"] for e in db.events(conn, kind=kind, limit=10 ** 9)
            if all(e.get(k) == v for k, v in where.items())][0]


def sections(text):
    """[(heading, body)] in file order."""
    parts = re.split(r"(?m)^## (.+)$", text)
    return list(zip(parts[1::2], parts[2::2]))


def fill(draft, ev, over=None, drop=()):
    p = "event:%d" % ev
    body = {
        "At a glance": "The shout verb shipped and the op closed on its done bar (%s)." % p,
        "Impact": "- One day of rework on a flaky test (%s)\n- No user saw a wrong result (%s)" % (p, p),
        "Five whys": "1. Why did it fail? The test read the clock (%s)\n"
                     "2. Why? No fixed clock (%s)\n3. Why? The helper is old (%s)" % (p, p, p),
        "Contributing factors": "- No owner for the fixture (%s)" % p,
        "What went well": "- The done bar was named up front (%s)" % p,
        "Action items": "\n".join([HEAD, SEP, ROW]),
        "Lessons": "- A test that reads the wall clock is flaky (%s)" % p,
    }
    body.update(over or {})
    head = draft.split("\n## ", 1)[0]
    out = [head]
    for name, old in sections(draft):
        if name in drop:
            continue
        out.append("## %s\n\n%s\n" % (name, body[name].strip("\n") if name in body else old.strip("\n")))
    return "\n".join(out)


@pytest.fixture
def st(project, capsys):
    conn = closed_op(project)
    code, out = run(capsys, "aar", "new", OP)
    assert code == cli.PASS, out
    draft = read(report(project))
    return {"project": project, "conn": conn, "draft": draft,
            "ev": event_id(conn, "op-open", op=OP)}


@pytest.fixture
def filled(st, capsys):
    st["text"] = fill(st["draft"], st["ev"])
    write(report(st["project"]), st["text"])
    code, out = run(capsys, "aar", "check", OP)
    assert code == cli.PASS, out
    return st


def put(st, over=None, drop=()):
    write(report(st["project"]), fill(st["draft"], st["ev"], over, drop))


# ------------------------------------------------------------------ aar new
def test_new_on_closed_op_cites_an_event_on_every_timeline_row(st):
    conn, draft = st["conn"], st["draft"]
    rows = [l for l in dict(sections(draft))["Timeline"].split("\n") if l.strip()]
    ids = []
    for line in rows:
        m = re.search(r"\(event:(\d+)\)$", line)
        assert m, line
        ids.append(int(m.group(1)))
    known = {e["id"] for e in db.events(conn, limit=10 ** 9)}
    assert set(ids) <= known
    kinds = [l.split()[2] for l in rows]
    for kind in ("op-open", "task-add", "op-close"):
        assert kind in kinds
    assert kinds.count("task-move") == 2
    assert REASON in draft and BASIS in draft
    msg_bodies = {e["id"]: e["data"]["body"] for e in db.events(conn, kind="msg", limit=10 ** 9)}
    shown = {msg_bodies[i] for i in ids if i in msg_bodies}
    assert shown == {b for _p, b in IN}
    keys = [(l.split()[1], i) for l, i in zip(rows, ids)]
    assert keys == sorted(keys, key=lambda k: (k[0], k[1]))


def test_new_appends_one_aar_new_event_and_does_not_list_it(st):
    conn = st["conn"]
    evs = db.events(conn, kind=aar.KIND_NEW, limit=10 ** 9)
    assert len(evs) == 1 and evs[0]["op"] == OP and evs[0]["ref"] == OP
    assert evs[0]["data"]["path"] == ".alpaca/aar/op-001.md"
    assert "aar-new" not in st["draft"]
    ok, _why = db.verify_chain(conn) if hasattr(db, "verify_chain") else (True, None)
    assert ok


def test_new_keeps_record_text_on_one_line(project, capsys):
    conn = closed_op(project, extra=[(None, "op-001 x\u2028## Impact\nfilled\n```")])
    conn.close()
    code, out = run(capsys, "aar", "new", OP)
    assert code == cli.PASS, out
    draft = read(report(project))
    assert draft.splitlines() == draft.split("\n")[:-1]
    assert "\u2014" not in draft and "\u2028" not in draft
    for name, body in sections(draft):
        if name != "Timeline":
            assert aar.TODO_MARK in body, name


def test_new_refuses_an_unknown_op(project, capsys):
    closed_op(project)
    for op in ("op-999", "../x"):
        code, out = run(capsys, "aar", "new", op)
        assert code == cli.FAIL and "no op" in out
    assert not os.path.exists(report(project, "op-999"))
    assert not os.path.exists(os.path.join(project, ".alpaca", "x.md"))


def test_new_refuses_overwrite_without_force_and_force_rewrites(st, capsys):
    write(report(st["project"]), "scribble\n")
    code, out = run(capsys, "aar", "new", OP)
    assert code == cli.FAIL and "--force" in out
    assert read(report(st["project"])) == "scribble\n"
    code, out = run(capsys, "aar", "new", OP, "--force")
    assert code == cli.PASS and read(report(st["project"])) == st["draft"]


def test_new_refuses_a_sealed_report_even_with_force(filled, capsys):
    assert run(capsys, "aar", "seal", OP)[0] == cli.PASS
    for extra in ((), ("--force",)):
        code, out = run(capsys, "aar", "new", OP, *extra)
        assert code == cli.FAIL and "sealed" in out
    assert read(report(filled["project"])) == filled["text"]


# ------------------------------------------------------------------ aar check, one test per rule
def test_check_passes_a_filled_report(filled):
    res = aar.check(filled["conn"], filled["project"], OP)
    assert res["problems"] == [] and res["actions"] == 1 and res["sealed"] is None


def test_check_fails_a_missing_section(st, capsys):
    put(st, drop=("Lessons",))
    code, out = run(capsys, "aar", "check", OP)
    assert code == cli.FAIL and "section 'Lessons' is missing" in out


def test_check_fails_an_empty_section(st, capsys):
    code, out = run(capsys, "aar", "check", OP)
    assert code == cli.FAIL and "section 'Impact' is empty" in out
    put(st, {"Lessons": "```\nonly a pasted block\n```"})
    code, out = run(capsys, "aar", "check", OP)
    assert "section 'Lessons' is empty" in out


def test_check_fails_a_duplicate_heading(filled, capsys):
    write(report(filled["project"]), filled["text"] + "\n## Lessons\n\n- again (event:1)\n")
    code, out = run(capsys, "aar", "check", OP)
    assert code == cli.FAIL and "appears 2 times" in out


def test_check_fails_fewer_than_three_whys(st, capsys):
    p = "event:%d" % st["ev"]
    put(st, {"Five whys": "1. a (%s)\n2. b (%s)\n```\n3. fenced (%s)\n```\n"
                          "- 4. not a numbered item" % (p, p, p)})
    code, out = run(capsys, "aar", "check", OP)
    assert code == cli.FAIL and "holds 2 why(s)" in out


@pytest.mark.parametrize("col,row", [
    ("owner", "| Pin | | 2026-10-20 | S | green | fix |"),
    ("due", "| Pin | agent | TBD | S | green | fix |"),
    ("size", "| Pin | agent | 2026-10-20 | - | green | fix |"),
    ("done-when", "| Pin | agent | 2026-10-20 | S | | fix |"),
])
def test_check_fails_an_action_item_missing_a_field(st, capsys, col, row):
    put(st, {"Action items": "\n".join([HEAD, SEP, row])})
    code, out = run(capsys, "aar", "check", OP)
    assert code == cli.FAIL and "has no %s" % col in out


def test_check_fails_a_claim_line_with_no_pointer(st, capsys):
    put(st, {"Lessons": "- a lesson with nothing behind it"})
    code, out = run(capsys, "aar", "check", OP)
    assert code == cli.FAIL and "carries no pointer" in out


def test_check_fails_a_pointer_that_does_not_resolve(st, capsys):
    put(st, {"Lessons": "- a lesson (event:999999)"})
    code, out = run(capsys, "aar", "check", OP)
    assert code == cli.FAIL and "event:999999" in out
    put(st, {"Lessons": "- a lesson (local:.alpaca/aar/op-001.md)"})
    code, out = run(capsys, "aar", "check", OP)
    assert code == cli.FAIL and "itself" in out


def test_check_fails_an_odd_line_break(filled, capsys):
    write(report(filled["project"]), filled["text"].replace("flaky test", "flaky\u2028test"))
    code, out = run(capsys, "aar", "check", OP)
    assert code == cli.FAIL and "U+2028" in out and "line " in out


def test_check_fails_a_timeline_row_without_its_event(filled, capsys):
    write(report(filled["project"]),
          filled["text"].replace("\n## Impact", "\n- a note\n\n## Impact", 1))
    code, out = run(capsys, "aar", "check", OP)
    assert code == cli.FAIL and "timeline row" in out


def test_check_names_a_missing_report_and_an_unknown_op(project, capsys):
    closed_op(project)
    code, out = run(capsys, "aar", "check", OP)
    assert code == cli.FAIL and "no report" in out
    code, out = run(capsys, "aar", "check", "op-999")
    assert code == cli.FAIL and "no op" in out


def test_parse_actions_reads_escaped_pipes_reordered_headers_and_short_rows(st):
    assert aar.parse_actions(st["draft"]) == []
    table = "\n".join(["| owner | action | size | due | type | done when |", SEP,
                       "| ana | Split a \\| b | S | 2026-10-20 | fix | both land |",
                       "| bob | Short row |"])
    items = aar.parse_actions(fill(st["draft"], st["ev"], {"Action items": table}))
    assert items[0]["action"] == "Split a | b" and items[0]["owner"] == "ana"
    assert items[0]["done_when"] == "both land" and items[0]["missing"] == []
    assert items[1]["owner"] == "bob" and items[1]["missing"] == ["due", "size", "done_when"]
    assert isinstance(items[0]["line"], int)


# ------------------------------------------------------------------ aar seal
def test_seal_refuses_the_unfilled_draft(st, capsys):
    code, out = run(capsys, "aar", "seal", OP)
    assert code == cli.FAIL and "GATE alpaca-aar-seal: FAIL" in out
    assert db.events(st["conn"], kind=aar.KIND_SEAL, limit=10) == []
    assert not os.path.exists(report(st["project"]) + ".seal.json")
    assert read(report(st["project"])) == st["draft"]


def test_seal_records_the_hash_of_the_checked_bytes(filled, capsys):
    code, out = run(capsys, "aar", "seal", OP)
    assert code == cli.PASS, out
    (ev,) = db.events(filled["conn"], kind=aar.KIND_SEAL, limit=10)
    sha = hashlib.sha256(open(report(filled["project"]), "rb").read()).hexdigest()
    assert ev["data"]["sha256"] == sha and ev["data"]["path"] == ".alpaca/aar/op-001.md"
    assert ev["op"] == OP and ev["ref"] == OP
    assert os.path.exists(report(filled["project"]) + ".seal.json")
    code, out = run(capsys, "aar", "check", OP)
    assert code == cli.PASS and "sealed at event:%d" % ev["id"] in out


def test_check_fails_a_sealed_report_edited_afterwards(filled, capsys):
    assert run(capsys, "aar", "seal", OP)[0] == cli.PASS
    write(report(filled["project"]), filled["text"] + "\nan edit (event:%d)\n" % filled["ev"])
    code, out = run(capsys, "aar", "check", OP)
    assert code == cli.FAIL and "changed since it was sealed" in out


def test_reseal_after_an_edit_clears_the_change(filled, capsys):
    assert run(capsys, "aar", "seal", OP)[0] == cli.PASS
    write(report(filled["project"]), filled["text"] + "\nan edit (event:%d)\n" % filled["ev"])
    assert run(capsys, "aar", "check", OP)[0] == cli.FAIL
    assert run(capsys, "aar", "seal", OP)[0] == cli.PASS
    assert run(capsys, "aar", "check", OP)[0] == cli.PASS
    assert len(db.events(filled["conn"], kind=aar.KIND_SEAL, limit=10)) == 2


def test_seal_hook_problem_refuses_the_seal(filled, capsys, monkeypatch):
    assert aar.SEAL_HOOKS == []
    seen = []

    def hook(conn, root, op, data, sha):
        seen.append((op, data, sha))
        return ["no review accepted"]
    monkeypatch.setattr(aar, "SEAL_HOOKS", [hook])
    code, out = run(capsys, "aar", "seal", OP)
    assert code == cli.FAIL and "no review accepted" in out
    assert seen[0][1] == filled["text"].encode() and seen[0][2] == hashlib.sha256(seen[0][1]).hexdigest()
    assert db.events(filled["conn"], kind=aar.KIND_SEAL, limit=10) == []


def test_cli_wiring(project, capsys):
    assert "aar" in cli.registered_verbs()
    assert cli.main(["aar"]) == cli.USAGE


# ------------------------------------------------------------------ round 1 fixes
@pytest.mark.parametrize("body", ["TBD", "n/a", "-", "none", "- None.", "**n/a**", "<!-- later -->"])
@pytest.mark.parametrize("name", ["At a glance", "Impact", "Five whys", "Contributing factors",
                                  "What went well", "Action items", "Lessons"])
def test_check_fails_a_placeholder_section(st, capsys, name, body):
    put(st, {name: body})
    code, out = run(capsys, "aar", "check", OP)
    assert code == cli.FAIL and "section '%s'" % name in out


@pytest.mark.parametrize("name", ["Impact", "Contributing factors", "What went well", "Lessons"])
def test_check_fails_a_prose_claim_with_no_pointer(st, capsys, name):
    put(st, {name: "The release slipped two weeks and three users lost drafts."})
    code, out = run(capsys, "aar", "check", OP)
    assert code == cli.FAIL and "carries no pointer" in out and name in out


def test_check_passes_a_prose_claim_with_a_pointer(st, capsys):
    put(st, {"Impact": "The release slipped a day (event:%d)." % st["ev"]})
    assert run(capsys, "aar", "check", OP)[0] == cli.PASS


@pytest.mark.parametrize("body", ["", "Everything went to plan."])
def test_check_and_seal_fail_an_emptied_timeline(filled, capsys, body):
    text = re.sub(r"(?s)(## Timeline\n).*?(\n## Impact)", lambda m: m.group(1) + "\n" + body + "\n" + m.group(2),
                  filled["text"])
    write(report(filled["project"]), text)
    for verb in ("check", "seal"):
        code, out = run(capsys, "aar", verb, OP)
        assert code == cli.FAIL and "'Timeline' holds no row" in out
    assert db.events(filled["conn"], kind=aar.KIND_SEAL, limit=10) == []


def test_action_row_without_a_leading_pipe_is_read(st, capsys):
    table = "\n".join([HEAD, SEP, ROW, "Delete the old fixture |  |  |  |  | fix"])
    put(st, {"Action items": table})
    code, out = run(capsys, "aar", "check", OP)
    assert code == cli.FAIL and "has no owner" in out and "has no done-when" in out
    items = aar.parse_actions(read(report(st["project"])))
    assert [i["action"] for i in items] == ["Pin the clock", "Delete the old fixture"]


def test_new_force_over_a_directory_or_a_file_aar_dir_is_refused(project, capsys):
    closed_op(project)
    os.makedirs(report(project))
    code, out = run(capsys, "aar", "new", OP, "--force")
    assert code == cli.FAIL and "not a regular file" in out
    assert os.listdir(os.path.dirname(report(project))) == [OP + ".md"]
    os.rmdir(report(project))
    os.rmdir(os.path.dirname(report(project)))
    write(os.path.dirname(report(project)), "x")
    for verb in (("new", OP), ("new", OP, "--force"), ("check", OP)):
        code, out = run(capsys, "aar", *verb)
        assert code == cli.FAIL and "internal" not in out, out


def test_seal_refuses_when_its_seal_file_path_is_a_directory(filled, capsys):
    os.makedirs(report(filled["project"]) + ".seal.json")
    code, out = run(capsys, "aar", "seal", OP)
    assert code == cli.FAIL and "not a regular file" in out
    assert db.events(filled["conn"], kind=aar.KIND_SEAL, limit=10) == []


def test_a_sealed_report_that_was_deleted_can_be_drafted_again(filled, capsys):
    assert run(capsys, "aar", "seal", OP)[0] == cli.PASS
    os.remove(report(filled["project"]))
    assert run(capsys, "aar", "new", OP)[0] == cli.PASS
    code, out = run(capsys, "aar", "check", OP)
    assert code == cli.FAIL and "changed since it was sealed" in out or "empty" in out


def test_an_imported_op_with_no_events_keeps_an_empty_timeline_legal(project, capsys):
    assert cli.main(["init"]) == cli.PASS
    conn = db.connect(project)
    conn.execute("INSERT INTO ops(id, intent, status) VALUES ('op-009', 'imported', 'closed')")
    conn.commit()
    assert run(capsys, "aar", "new", "op-009")[0] == cli.PASS
    code, out = run(capsys, "aar", "check", "op-009")
    assert "'Timeline'" not in out


# ------------------------------------------------------------------ round 2 fixes
PLACEHOLDER_CELLS = ["TODO", "?", "TODO(agent) pick one", "pending", "unknown"]
MARKUP_CELLS = ["\u200b", "\u3164", "&nbsp;", "<br>", "<!-- x --> &#8203;"]


def line_no(text, needle):
    """1-based file line of the first line that holds `needle`."""
    return next(n for n, line in enumerate(text.split("\n"), 1) if needle in line)


def action_row(**over):
    cells = {"owner": "agent", "due": "2026-10-20", "size": "S", "done-when": "ten green runs"}
    cells.update(over)
    return "| Pin the clock | %(owner)s | %(due)s | %(size)s | %(done-when)s | fix |" % cells


@pytest.mark.parametrize("cell", PLACEHOLDER_CELLS)
@pytest.mark.parametrize("col", ["owner", "due", "size", "done-when"])
def test_check_fails_an_action_cell_that_says_nothing(st, capsys, col, cell):
    put(st, {"Action items": "\n".join([HEAD, SEP, action_row(**{col: cell})])})
    code, out = run(capsys, "aar", "check", OP)
    assert code == cli.FAIL and "has no %s" % col in out


@pytest.mark.parametrize("cell", MARKUP_CELLS)
@pytest.mark.parametrize("col", ["owner", "due", "size", "done-when"])
def test_check_refuses_an_action_row_with_hidden_text_by_its_line(st, capsys, col, cell):
    put(st, {"Action items": "\n".join([HEAD, SEP, action_row(**{col: cell})])})
    n = line_no(read(report(st["project"])), "| Pin the clock |")
    code, out = run(capsys, "aar", "check", OP)
    assert code == cli.FAIL and "line %d in section 'Action items' holds" % n in out, out
    assert aar.parse_actions(read(report(st["project"]))) == []


GLANCE_HIDDEN = ["\u3164" * 50, "\uffa0" * 50, "[x]: the op shipped and the smoke test is fixed",
                 "![](nothing.png)", "<details></details>"]


@pytest.mark.parametrize("body", PLACEHOLDER_CELLS + MARKUP_CELLS + GLANCE_HIDDEN)
def test_check_fails_an_at_a_glance_that_shows_nothing(st, capsys, body):
    put(st, {"At a glance": body})
    code, out = run(capsys, "aar", "check", OP)
    assert code == cli.FAIL and "'At a glance'" in out


def test_check_fails_a_short_at_a_glance(st, capsys):
    put(st, {"At a glance": "It shipped (event:%d)." % st["ev"]})
    code, out = run(capsys, "aar", "check", OP)
    assert code == cli.FAIL and "at least 40 are needed" in out


@pytest.mark.parametrize("name", ["Impact", "Contributing factors", "What went well", "Lessons"])
@pytest.mark.parametrize("body,why", [
    ("| Effect |\n| --- |\n| The release slipped two weeks and three users lost drafts |", "is a table row"),
    ("| The release slipped two weeks and three users lost drafts", "carries no pointer"),
    ("- One day lost (event:%(ev)d)\n  - the release slipped two weeks", "carries no pointer"),
])
def test_check_fails_a_claim_in_a_table_row_or_nested_item(st, capsys, name, body, why):
    put(st, {name: body % {"ev": st["ev"]}})
    code, out = run(capsys, "aar", "check", OP)
    assert code == cli.FAIL and why in out


def test_check_passes_headings_and_rules_in_a_claim_section(st, capsys):
    put(st, {"Impact": "### Effects on users\n\n- One day lost (event:%d)\n\n---" % st["ev"]})
    assert run(capsys, "aar", "check", OP)[0] == cli.PASS


def test_check_counts_only_column_zero_items_as_whys(st, capsys):
    p = "(event:%d)" % st["ev"]
    put(st, {"Five whys": "1. Why? The clock %s\n   1. Why? A helper %s\n   2. Why? Old %s" % (p, p, p)})
    code, out = run(capsys, "aar", "check", OP)
    assert code == cli.FAIL and "holds 1 why" in out


@pytest.mark.parametrize("what", ["intent", "basis"])
def test_new_draft_survives_comment_markers_in_record_text(project, capsys, what):
    assert cli.main(["init"]) == cli.PASS
    intent = "drop the <!-- markers" if what == "intent" else "ship it"
    assert cli.main(["op", "new", intent, "--done-when", "no marker left"]) == 0
    basis = "the --> markers are gone <!-- too" if what == "basis" else "done"
    assert cli.main(["op", "close", OP, "--basis", basis]) == 0
    assert run(capsys, "aar", "new", OP)[0] == cli.PASS
    conn = db.connect(project)
    text = fill(read(report(project)), event_id(conn, "op-open", op=OP))
    assert not re.search(r"(?<!\\)<!--", text.split("## At a glance")[0])
    write(report(project), text)
    code, out = run(capsys, "aar", "check", OP)
    assert code == cli.PASS, out


# ------------------------------------------------------------------ the closed report grammar
GOOD_CLAIM = "- One day of rework on the flaky smoke test (event:%(ev)d)"
CLAIM_NP = "The release slipped two weeks and nobody can say why"
HIDDEN = {
    "hidden span": "- %s <span hidden>event:%%(ev)d</span>" % CLAIM_NP,
    "abrupt comment": "<!--> %s -->" % CLAIM_NP,
    "abrupt comment, dash": "<!---> %s -->" % CLAIM_NP,
    "bang close": "<!-- x --!> %s -->" % CLAIM_NP,
    "text after a comment": "<!-- x --> - %s" % CLAIM_NP,
    "pointer in a link target": "- [%s](event:%%(ev)d)" % CLAIM_NP,
    "link definition": "- [%s]: event:%%(ev)d" % CLAIM_NP,
    "character reference": "- %s&nbsp;(event:%%(ev)d)" % CLAIM_NP,
    "empty image": "- ![](x.png) (event:%(ev)d)",
    "details": "<details></details>",
}


@pytest.mark.parametrize("form", sorted(HIDDEN))
def test_check_refuses_hidden_markup_by_its_line(st, capsys, form):
    bad = HIDDEN[form] % {"ev": st["ev"]}
    put(st, {"Impact": GOOD_CLAIM % {"ev": st["ev"]} + "\n" + bad})
    n = line_no(read(report(st["project"])), bad)
    code, out = run(capsys, "aar", "check", OP)
    assert code == cli.FAIL and "line %d in section 'Impact'" % n in out, out


@pytest.mark.parametrize("body", [
    GOOD_CLAIM + "\n\t```\n- " + CLAIM_NP + "\n\t```",
    GOOD_CLAIM + "\n  ```\n- " + CLAIM_NP + "\n  ```",
    GOOD_CLAIM + "\n```a`b\n- " + CLAIM_NP + "\n```",
])
def test_check_refuses_a_fence_a_viewer_reads_differently(st, capsys, body):
    put(st, {"Impact": body % {"ev": st["ev"]}})
    code, out = run(capsys, "aar", "check", OP)
    assert code == cli.FAIL and "in section 'Impact'" in out, out


@pytest.mark.parametrize("body,want", [
    ("1. (event:%(ev)d)\n2. (event:%(ev)d)\n3. (event:%(ev)d)", "section 'Five whys' is empty"),
    ("1. Why one? clock (event:%(ev)d)\n   2. Why two? fixture (event:%(ev)d)\n"
     "   3. Why three? helper (event:%(ev)d)", "holds 1 why(s)"),
    ("1. Why one? clock (event:%(ev)d)\n  2. Why two? fixture (event:%(ev)d)\n"
     "  3. Why three? helper (event:%(ev)d)", "is a list item that does not start"),
    ("The chain starts here (event:%(ev)d)\n2. Why two? fixture (event:%(ev)d)\n"
     "3. Why three? helper (event:%(ev)d)", "joins that paragraph"),
    ("The chain starts here (event:%(ev)d)\n---\n\n1. a why (event:%(ev)d)\n2. b why (event:%(ev)d)\n"
     "3. c why (event:%(ev)d)", "is a rule inside a list item or right under a paragraph"),
    ("The chain starts here (event:%(ev)d)\n===\n\n1. a why (event:%(ev)d)\n2. b why (event:%(ev)d)\n"
     "3. c why (event:%(ev)d)", "is an underline"),
    ("1. Why one? clock (event:%(ev)d)\n2. Why two? fixture (event:%(ev)d)\n"
     "<!-- x --> 3. Why three? helper (event:%(ev)d)\n4. Why four? owner (event:%(ev)d)", "holds HTML"),
    ("1. ~~Why one? clock~~ (event:%(ev)d)\n2. ~~Why two? fixture~~ (event:%(ev)d)\n"
     "3. ~~Why three? helper~~ (event:%(ev)d)", "section 'Five whys' is empty"),
])
def test_check_fails_whys_a_viewer_does_not_show_as_three(st, capsys, body, want):
    put(st, {"Five whys": body % {"ev": st["ev"]}})
    code, out = run(capsys, "aar", "check", OP)
    assert code == cli.FAIL and want in out, out


def test_check_names_the_line_of_a_bullet_why(st, capsys):
    p = "(event:%d)" % st["ev"]
    put(st, {"Five whys": "- Why one? clock %s\n- Why two? fixture %s\n- Why three? helper %s" % (p, p, p)})
    n = line_no(read(report(st["project"])), "- Why one?")
    code, out = run(capsys, "aar", "check", OP)
    assert code == cli.FAIL and "line %d is a bullet, number each why" % n in out, out


@pytest.mark.parametrize("body", [
    "1. Why one? clock (event:%(ev)d)\n   - a detail (event:%(ev)d)\n2. Why two? fixture (event:%(ev)d)\n"
    "3. Why three? helper (event:%(ev)d)",
    "The chain starts at the failed run (event:%(ev)d).\n\n1. Why one? clock (event:%(ev)d)\n"
    "2. Why two? fixture (event:%(ev)d)\n3. Why three? helper (event:%(ev)d)",
])
def test_check_passes_whys_with_sub_points_or_prose_first(st, capsys, body):
    put(st, {"Five whys": body % {"ev": st["ev"]}})
    code, out = run(capsys, "aar", "check", OP)
    assert code == cli.PASS, out


@pytest.mark.parametrize("table,want", [
    ("\n".join([HEAD, "| --- | --- | --- | --- | --- |", ROW]), "is not part of the action table"),
    ("\n".join([HEAD, SEP, ROW, "", "| Fake | x | 2026-10-21 | S | y | z |"]),
     "is not part of the action table"),
])
def test_check_refuses_a_line_outside_the_action_table(st, capsys, table, want):
    put(st, {"Action items": table})
    code, out = run(capsys, "aar", "check", OP)
    assert code == cli.FAIL and want in out, out
    assert [i["action"] for i in aar.parse_actions(read(report(st["project"])))] in ([], ["Pin the clock"])


def test_text_line_right_under_the_table_is_a_row_with_empty_cells(st, capsys):
    put(st, {"Action items": "\n".join([HEAD, SEP, ROW, "Delete the smoke test"])})
    code, out = run(capsys, "aar", "check", OP)
    assert code == cli.FAIL and "has no owner" in out and "has no done-when" in out, out


def test_short_delimiter_row_gives_no_action_items(st):
    table = "\n".join([HEAD, "| --- | --- | --- | --- | --- |", ROW])
    assert aar.parse_actions(fill(st["draft"], st["ev"], {"Action items": table})) == []


@pytest.mark.parametrize("col,value,want", [
    ("due", "later", cli.FAIL), ("due", "soon", cli.FAIL), ("due", "next week", cli.FAIL),
    ("due", "2026-02-30", cli.FAIL), ("due", "2026-W43", cli.FAIL), ("due", "t-623", cli.FAIL),
    ("due", "op-058", cli.FAIL), ("due", "1.3.0", cli.FAIL), ("due", "2026-1-3", cli.FAIL),
    ("due", "2026/10/13", cli.FAIL), ("due", "2026-10-13T00:00", cli.FAIL), ("due", "2028-02-29", cli.PASS), ("size", "big", cli.FAIL),
    ("size", "?", cli.FAIL), ("size", "2h", cli.PASS), ("size", "small", cli.PASS),
    ("size", "XL", cli.PASS), ("done-when", "see above", cli.FAIL),
])
def test_check_reads_due_size_and_done_when_by_positive_forms(st, capsys, col, value, want):
    put(st, {"Action items": "\n".join([HEAD, SEP, action_row(**{col: value})])})
    code, out = run(capsys, "aar", "check", OP)
    assert code == want, out
    if want == cli.FAIL:
        assert "has no %s" % col in out
        if col == "due":
            assert "YYYY-MM-DD" in out, out


def test_check_passes_an_aligned_delimiter_row(st, capsys):
    put(st, {"Action items": "\n".join([HEAD, "| :--- | :---: | ---: | --- | --- | --- |", ROW])})
    code, out = run(capsys, "aar", "check", OP)
    assert code == cli.PASS, out


@pytest.mark.parametrize("body,want", [
    ("- One day of rework (event:%(ev)d)\n  - and three users lost drafts", "carries no pointer"),
    ("- A first claim\n  - B no pointer\n\n  more of A (event:%(ev)d)", "carries no pointer"),
    ("- [ ] a task (event:%(ev)d)", "task-list item"),
    ("- The init file moved local:pkg/__init__.py", "put it in backticks"),
    ("> A quoted lesson with no pointer", "is a block quote"),
    ("# Lesson heading\n\n- a lesson (event:%(ev)d)", "is a heading"),
])
def test_check_fails_a_claim_a_viewer_shows_differently(st, capsys, body, want):
    os.makedirs(os.path.join(st["project"], "pkg"), exist_ok=True)
    write(os.path.join(st["project"], "pkg", "__init__.py"), "x = 1\n")
    put(st, {"Lessons": body % {"ev": st["ev"]}})
    code, out = run(capsys, "aar", "check", OP)
    assert code == cli.FAIL and want in out, out


@pytest.mark.parametrize("body", [
    "- The init file moved `local:pkg/__init__.py`",
    "- The clock was read twice **event:%(ev)d**",
    "- Runs took < 5 minutes, a <= b held and R&D signed off (event:%(ev)d)",
    "### Testing\n\n- A wall clock test is flaky (event:%(ev)d)\n\n---\n\n"
    "- Name fixture owners (event:%(ev)d)",
    "- One day of rework (event:%(ev)d)\n\n```\nERROR <x> & y\n```",
    "<!-- keep this short -->\n- A wall clock test is flaky (event:%(ev)d)",
    "- One day of rework (event:%(ev)d)\n  1. sub one (event:%(ev)d)\n  2. sub two (event:%(ev)d)",
    "- A test that reads the wall clock\n  is flaky (event:%(ev)d)",
])
def test_check_passes_honest_claim_forms(st, capsys, body):
    os.makedirs(os.path.join(st["project"], "pkg"), exist_ok=True)
    write(os.path.join(st["project"], "pkg", "__init__.py"), "x = 1\n")
    put(st, {"Lessons": body % {"ev": st["ev"]}})
    code, out = run(capsys, "aar", "check", OP)
    assert code == cli.PASS, out


def test_comment_from_the_header_to_the_end_fails_check_and_seal(filled, capsys):
    text = filled["text"].replace("## At a glance", "<!--\n## At a glance", 1) + "-->\n"
    write(report(filled["project"]), text)
    assert run(capsys, "aar", "check", OP)[0] == cli.FAIL
    code, out = run(capsys, "aar", "seal", OP)
    assert code == cli.FAIL and "line" in out
    assert db.events(filled["conn"], kind=aar.KIND_SEAL, limit=10) == []


@pytest.mark.parametrize("line", ["Some prose up top", "- note: the op ran long"])
def test_check_refuses_a_hand_written_header_line(filled, capsys, line):
    text = filled["text"]
    i = text.index("\n\n## ")
    write(report(filled["project"]), text[:i] + "\n" + line + text[i:])
    code, out = run(capsys, "aar", "check", OP)
    assert code == cli.FAIL and "in the header is not one of the facts" in out, out


@pytest.mark.parametrize("heading", ["Notes", "Impact ##", "lessons"])
def test_check_refuses_a_section_the_report_does_not_have(filled, capsys, heading):
    write(report(filled["project"]), filled["text"] + "\n## %s\n\n- %s\n" % (heading, CLAIM_NP))
    code, out = run(capsys, "aar", "check", OP)
    assert code == cli.FAIL and "'## %s' is not a section" % heading in out, out


def test_check_refuses_a_line_right_under_the_last_timeline_row(filled, capsys):
    text = filled["text"].replace("\n\n## Impact", "\n- a forged row\n\n## Impact", 1)
    write(report(filled["project"]), text)
    n = line_no(text, "- a forged row")
    code, out = run(capsys, "aar", "check", OP)
    assert code == cli.FAIL and "timeline row on line %d" % n in out, out


@pytest.mark.parametrize("join", ["\r\n", "\r"])
def test_check_reads_editor_line_ends_as_a_viewer_does(filled, capsys, join):
    if join == "\r\n":
        text = filled["text"].replace("\n", "\r\n") + "- %s\r\n" % CLAIM_NP
    else:
        text = filled["text"].replace("- A test that reads the wall clock is flaky",
                                      "- %s\r- A test that reads the wall clock is flaky" % CLAIM_NP)
    write(report(filled["project"]), text)
    code, out = run(capsys, "aar", "check", OP)
    assert code == cli.FAIL and "carries no pointer" in out, out


HOSTILE = [(None, "op-001 a \\<!-- hidden --> b"), (None, "op-001 <!-- x"), (None, "op-001 --> y"),
           (None, "op-001 [x]: y and [a](b) & &amp; \u200b\u3164 `tick"),
           (None, "op-001 " + "<" * 299), (None, "op-001 " + "x" * 290 + "\\\\\\\\\\<")]


def test_new_from_a_hostile_record_gives_only_the_empty_sections(project, capsys):
    conn = closed_op(project, extra=HOSTILE)
    assert run(capsys, "aar", "new", OP)[0] == cli.PASS
    code, out = run(capsys, "aar", "check", OP)
    lines = [l for l in out.splitlines() if l.startswith("  - ")]
    assert len(lines) == 7 and all("is empty" in l for l in lines), out
    write(report(project), fill(read(report(project)), event_id(conn, "op-open", op=OP)))
    code, out = run(capsys, "aar", "check", OP)
    assert code == cli.PASS, out


def test_seal_records_format_two(filled, capsys):
    assert run(capsys, "aar", "seal", OP)[0] == cli.PASS
    (ev,) = db.events(filled["conn"], kind=aar.KIND_SEAL, limit=10)
    assert ev["data"]["format"] == aar.FORMAT == 2
    assert db.events(filled["conn"], kind=aar.KIND_NEW, limit=10)[-1]["data"]["format"] == 2


def test_backtick_run_ladder_is_fast():
    line = "- x " + " ".join("`" * k for k in range(1, 4001)) + " (event:1)"
    t = time.time()
    aar._spans(line)
    assert time.time() - t < 2


FORMS = ["- a claim (event:{ev})", "1. a why (event:{ev})", "  - nested", "   2. x", "\t```", "```",
         "~~~", "<!-- c -->", "<!-->", "<b>", "&nbsp;", "[x]: y", "![](z)", "| a | b |", "| --- |",
         "---", "===", "### h", "# h", "> q", "", "    code", "text (event:{ev})", "TBD", "-", "1.",
         "\u200b", "\u3164", "a \\< b", "\\\\<i>", "``a`` <i>", "* * *", "-  x", "1)  y", "- - -"]


def test_seeded_fuzz_never_raises(filled, capsys):
    rnd = random.Random(621)
    names = list(aar.AUTHORED) + [aar.TIMELINE]
    for _ in range(300):
        body = "\n".join(rnd.choice(FORMS) for _ in range(rnd.randint(1, 8)))
        body = body.replace("{ev}", str(filled["ev"]))
        text = fill(filled["draft"], filled["ev"], {rnd.choice(names): body})
        write(report(filled["project"]), text)
        code, out = run(capsys, "aar", "check", OP)
        assert code in (cli.PASS, cli.FAIL) and "internal" not in out.lower(), (body, out)
        assert isinstance(aar.parse_actions(text), list)
