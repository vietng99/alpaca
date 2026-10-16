"""Round 5 of t-695 (op-059): the trust path of the sandbox write guard (check-adversarial-r4.md B-1, B-2, B-3,
MINOR 4 and 5). The owner confirms a path LIST by pasting the exact command line the readback prints; each
entry is resolved once at set time and never again; an op without a confirmed list adds nothing to the
lists of the other ops the session works on. Temp projects only; probe commands are data, never run."""
import json
import os
import time

import pytest

from alpaca import cli, db, ownerword, sandbox
from alpaca.hooks import sandbox_guard
from alpaca.tests import sandbox_fixtures as fx
from alpaca.tests.ownerword_fixtures import EARLY, Project, typed


@pytest.fixture(autouse=True)
def temp_base(tmp_path, monkeypatch):
    return fx.use_temp_base(monkeypatch, tmp_path)


def _project(tmp_path, monkeypatch, *, ask=True, op="op-062"):
    """A project whose session holds t-1 of `op` (answered, no confirmed list); with `ask` the op's batch
    was shown to the owner (the sandbox question asked)."""
    p = Project(tmp_path, monkeypatch)
    studio = os.path.realpath(str(tmp_path)) + "/studio"
    fx.add_op(p.conn, op, answer=fx.OP_062.format(studio=studio), sid=p.sid)
    fx.add_task(p.conn, "t-1", op, sid=p.sid)
    if ask:
        p.ask("batch:%s" % op)
    return p, studio


def _line(verb, op, paths_):
    return "alpaca sandbox %s %s %s" % (verb, op, " ".join("--path %s" % x for x in paths_))


def _set(p, paths_, quote, op="op-062", verb=None):
    return (verb or sandbox.set_paths)(p.conn, op, paths_, by="owner", quote=quote, session=p.sid, root=p.root,
                                       environ=p.env)


REFUSED = (sandbox.SandboxError, ownerword.OwnerWordRefusal)


# ---------------------------------------------------------------- B-1. the owner confirms the exact list
def test_set_takes_the_command_line_the_owner_pasted(tmp_path, monkeypatch):
    p, studio = _project(tmp_path, monkeypatch)
    line = _line("set", "op-062", ["root", studio, "scratchpad"])
    p.write([typed(line)])
    ev = _set(p, ["scratchpad", studio + "/", "root"], line)          # order and a trailing slash do not matter
    data = json.loads(ev["data"])
    assert ev["kind"] == sandbox.SET_KIND and sorted(data["paths"]) == sorted(["root", studio, "scratchpad"])
    assert data["owner_word"]["quote"] == line and data["owner_word"]["path"] == "session"
    assert isinstance(data["owner_word"]["offset"], int)
    sb = sandbox.for_session(p.conn, p.sid, p.root)
    assert sorted(sb["roots"]) == sorted([p.root, studio]) and sb["scratch"] is True


def test_the_line_may_stand_on_its_own_line_of_a_longer_message(tmp_path, monkeypatch):
    p, studio = _project(tmp_path, monkeypatch)
    line = _line("set", "op-062", ["root", studio])
    msg = "yes, that is the list:\n$ %s\nthanks" % line
    p.write([typed(msg)])
    _set(p, ["root", studio], line)
    assert sandbox.sandbox_of(p.conn, "op-062", p.root)["roots"] == [p.root, studio]


@pytest.mark.parametrize("msg", [
    "Writes stay in this project root and {studio}",                       # names the paths, no command line
    "sandbox: root, {studio} and the scratchpad",
    "never run alpaca sandbox set op-062 --path root --path {studio}",     # the line does not start the line
    "alpaca sandbox set op-062 --path root",                               # another list
    "alpaca sandbox set op-062 --path root --path {studio} --path /srv/x",
    "alpaca sandbox set op-061 --path root --path {studio}",               # another op
    "alpaca sandbox widen op-062 --path root --path {studio}",             # another verb
])
def test_set_refuses_a_message_that_is_not_the_exact_command_line(tmp_path, monkeypatch, msg):
    p, studio = _project(tmp_path, monkeypatch)
    msg = msg.format(studio=studio)
    p.write([typed(msg)])
    with pytest.raises(REFUSED):
        _set(p, ["root", studio], msg)
    assert sandbox.sandbox_of(p.conn, "op-062", p.root) is None


def test_set_refuses_a_line_typed_before_the_sandbox_question_or_with_no_question(tmp_path, monkeypatch):
    p, studio = _project(tmp_path, monkeypatch)
    line = _line("set", "op-062", ["root", studio])
    p.write([typed(line, ts=EARLY)])
    with pytest.raises(REFUSED):
        _set(p, ["root", studio], line)
    q, studio = _project(tmp_path / "q", monkeypatch, ask=False)
    line = _line("set", "op-062", ["root", studio])
    q.write([typed(line)])
    with pytest.raises(sandbox.SandboxError) as err:
        _set(q, ["root", studio], line)
    assert err.value.reason == "SANDBOX-NOT-ASKED" and "alpaca sandbox ask" in err.value.detail


def test_a_pasted_line_confirms_once(tmp_path, monkeypatch):
    """One owner message confirms once; the owner pastes the line again to confirm it again."""
    p, studio = _project(tmp_path, monkeypatch)
    wide = _line("set", "op-062", ["root", studio])
    p.write([typed(wide)])
    _set(p, ["root", studio], wide)
    with pytest.raises(sandbox.SandboxError) as err:
        _set(p, ["root", studio], wide)
    assert err.value.reason == "SANDBOX-WORD-USED"
    p.write([typed(wide), typed(wide)])
    assert _set(p, ["root", studio], wide)["kind"] == sandbox.SET_KIND


def test_widen_takes_only_its_own_command_line(tmp_path, monkeypatch):
    p, studio = _project(tmp_path, monkeypatch)
    fx.confirm(p.conn, "op-062", ["root"], sid=p.sid)
    extra = studio + "-2"
    good = _line("widen", "op-062", [extra])
    p.write([typed("Writes may also go to %s" % extra), typed(_line("set", "op-062", [extra])), typed(good)])
    for quote in ("Writes may also go to %s" % extra, _line("set", "op-062", [extra])):
        with pytest.raises(REFUSED):
            _set(p, [extra], quote, verb=sandbox.widen)
    ev = _set(p, [extra], good, verb=sandbox.widen)
    assert ev["kind"] == sandbox.WIDEN_KIND
    assert extra in sandbox.sandbox_of(p.conn, "op-062", p.root)["roots"]


def _broad(tmp_path):
    home = os.path.realpath(os.path.expanduser("~"))
    proj_parent = os.path.dirname(os.path.realpath(str(tmp_path / "proj")))
    return ["/", "//", "~", "~/", "/home", "/tmp", "/var", "/etc", "/usr", "/srv", home, os.path.dirname(home),
            proj_parent, os.path.dirname(proj_parent), "/*", "/tm?", "/home/*", home + "/*"]


@pytest.mark.parametrize("k", range(18))
def test_set_refuses_a_path_that_is_too_broad_even_when_the_owner_pasted_it(tmp_path, monkeypatch, k):
    p, studio = _project(tmp_path, monkeypatch)
    path = _broad(tmp_path)[k]
    line = _line("set", "op-062", ["root", path])
    p.write([typed(line)])
    with pytest.raises(sandbox.SandboxError) as err:
        _set(p, ["root", path], line)
    assert err.value.reason == "SANDBOX-PATH-BROAD", path
    with pytest.raises(sandbox.SandboxError):
        sandbox.from_list(["root", path], p.root, strict=True)


def test_a_narrow_glob_and_a_home_subfolder_are_not_broad(tmp_path):
    root = os.path.realpath(str(tmp_path / "proj"))
    for path in ("/tmp/op61-*", "~/.cache/op61", "/srv/data", root + "/../studio"):
        sandbox.from_list([path], root, strict=True)


def test_the_cli_set_and_the_ask_verb(tmp_path, monkeypatch, capsys):
    p, studio = _project(tmp_path, monkeypatch, ask=False)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", p.sid)
    monkeypatch.chdir(p.root)
    capsys.readouterr()
    assert cli.main(["sandbox", "ask", "op-062", "--path", "root", "--path", studio]) == cli.PASS
    out = capsys.readouterr().out
    line = _line("set", "op-062", ["root", studio])
    assert line in out and "paste" in out
    assert ownerword.latest_ask(p.conn, "sandbox:op-062") is not None
    p.write([typed(line)])
    code = cli.main(["sandbox", "set", "op-062", "--path", "root", "--path", studio, "--by", "owner",
                     "--quote", line])
    out = capsys.readouterr().out
    assert code == cli.PASS, out
    assert "ENFORCED" in out
    assert cli.main(["sandbox", "ask", "op-062", "--path", "/"]) == cli.FAIL       # a broad path is not offered
    assert "SANDBOX-PATH-BROAD" in capsys.readouterr().out


def test_the_readback_prints_the_line_to_paste_and_leaves_out_a_broad_entry(tmp_path):
    root = os.path.realpath(str(tmp_path / "proj"))
    os.makedirs(root)
    rb = sandbox.readback("Writes stay in this project root and /tmp; no sudo", root, "op-062")
    assert "alpaca sandbox set op-062 --path root\n" in rb + "\n"
    assert "too broad" in rb and "/tmp" in rb
    assert "paste" in rb


# ---------------------------------------------------------------- B-2. resolved once, at set time
def test_set_refuses_a_path_that_is_or_passes_through_a_link(tmp_path, monkeypatch):
    p, studio = _project(tmp_path, monkeypatch)
    real = os.path.realpath(str(tmp_path)) + "/real"
    os.makedirs(real + "/sub")
    os.symlink(real, studio)
    for path in (studio, studio + "/sub", studio + "/new/deeper"):
        line = _line("set", "op-062", ["root", path])
        p.write([typed(line)])
        with pytest.raises(sandbox.SandboxError) as err:
            _set(p, ["root", path], line)
        assert err.value.reason == "SANDBOX-PATH-LINK", path


def test_a_link_made_at_a_confirmed_path_later_does_not_move_the_sandbox(project, tmp_path):
    base = os.path.realpath(str(tmp_path))
    new, elsewhere = base + "/studio-new", base + "/elsewhere"
    os.makedirs(elsewhere)
    e = fx.make(project, tmp_path, confirm_paths=["root", new])           # not made yet
    data = json.loads(e.conn.execute("SELECT data FROM events WHERE id=?", (e.set_event,)).fetchone()["data"])
    assert data["resolved"] == ["root", new]
    os.symlink(elsewhere, new)
    sandbox.clear_cache(e.root)
    assert sandbox.for_session(e.conn, e.sid, e.root)["roots"] == [e.root, new]
    for path in (elsewhere + "/f", new + "/f"):                          # through the link: elsewhere
        out = sandbox_guard.decide({"tool_name": "Write", "tool_input": {"file_path": path, "content": "x"},
                                    "session_id": e.sid, "cwd": e.root}, root=e.root)
        assert out is not None, path


def test_a_session_scratchpad_replaced_by_a_link_is_not_followed(project, tmp_path, monkeypatch):
    from alpaca import paths
    top = tmp_path / "ctmp"
    monkeypatch.setenv("CLAUDE_CODE_TMPDIR", str(top))
    e = fx.make(project, tmp_path, confirm_paths=["root", "scratchpad"])
    base = top / ("claude-%d" % os.getuid()) / paths.escape_cwd(e.root)
    base.mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    os.symlink(str(elsewhere), str(base / e.sid))                        # rm -rf $SCR; ln -s <elsewhere> $SCR
    sb = sandbox.for_session(e.conn, e.sid, e.root)
    assert not sandbox.covers(str(elsewhere / "f"), sb)


def test_an_always_place_that_resolves_to_a_broad_folder_is_dropped(project, tmp_path, monkeypatch):
    cfg = tmp_path / "cfglink"
    os.symlink("/", str(cfg))                                           # ln -s / <config dir>
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
    e = fx.make(project, tmp_path, confirm_paths=["root"])
    sb = sandbox.for_session(e.conn, e.sid, e.root)
    assert "/" not in sb["always"] and not sandbox.covers("/etc/adv-probe", sb)


# ---------------------------------------------------------------- B-3. an unconfirmed op adds nothing
def test_claiming_a_task_of_an_op_with_no_list_keeps_the_confirmed_list_enforced(project, tmp_path):
    e = fx.make(project, tmp_path)
    fx.add_op(e.conn, "op-063", answer=fx.OP_063, sid=e.sid)               # answered, never confirmed
    fx.add_task(e.conn, "t-2", "op-063", sid=e.sid)
    fx.add_op(e.conn, "op-099", batch=False, sid=e.sid)                    # op new, task add, claim
    fx.add_task(e.conn, "t-9", "op-099", sid=e.sid)
    sb = sandbox.for_session(e.conn, e.sid, e.root)
    assert sb["ops"] == ["op-062"] and sorted(sb["unconfirmed"]) == ["op-063", "op-099"]
    out = sandbox_guard.decide({"tool_name": "Write", "tool_input": {"file_path": e.other + "/f", "content": "x"},
                                "session_id": e.sid, "cwd": e.root}, root=e.root)
    assert out is not None
    assert "op-063" in sandbox.show_text(sb, e.sid)


def test_two_confirmed_ops_enforce_the_union_and_only_unconfirmed_ops_enforce_nothing(project, tmp_path):
    e = fx.make(project, tmp_path)
    fx.add_op(e.conn, "op-070", answer=fx.OP_063, sid=e.sid)
    fx.confirm(e.conn, "op-070", ["root", "/srv/seventy"], sid=e.sid)
    fx.add_task(e.conn, "t-2", "op-070", sid=e.sid)
    sb = sandbox.for_session(e.conn, e.sid, e.root)
    assert sb["ops"] == ["op-062", "op-070"] and "/srv/seventy" in sb["roots"] and e.studio in sb["roots"]
    fx.add_op(e.conn, "op-090", answer=fx.OP_063, sid="sess-bbbb")         # another session, no list at all
    fx.add_task(e.conn, "t-3", "op-090", sid="sess-bbbb", worker="claude-b")
    assert sandbox.for_session(e.conn, "sess-bbbb", e.root) is None


# ---------------------------------------------------------------- MINOR 5. a raw row is honored only with its proof
def _raw(conn, op, kind, data, sid=fx.SID):
    return db.append_event(conn, session=sid, actor="owner", kind=kind, op=op, ref=sandbox.ITEM, data=data)


def test_a_raw_set_row_without_its_proof_is_ignored(project, tmp_path):
    e = fx.make(project, tmp_path, confirm_paths=["root"])
    _raw(e.conn, e.op, sandbox.SET_KIND, {"op": e.op, "paths": ["root", "/"], "owner_word": {}})
    assert sandbox.sandbox_of(e.conn, e.op, e.root)["roots"] == [e.root]
    line = _line("set", e.op, ["root", "/srv/x"])                         # the quote names another list
    _raw(e.conn, e.op, sandbox.SET_KIND, {"op": e.op, "paths": ["root", "/srv/y"], "resolved": ["root", "/srv/y"],
                                          "owner_word": {"path": "terminal", "quote": line}})
    assert sandbox.sandbox_of(e.conn, e.op, e.root)["roots"] == [e.root]
    line = _line("set", e.op, ["root", "/srv/y"])                         # resolved does not follow the paths
    _raw(e.conn, e.op, sandbox.SET_KIND, {"op": e.op, "paths": ["root", "/srv/y"], "resolved": ["root", "/"],
                                          "owner_word": {"path": "terminal", "quote": line}})
    assert sandbox.sandbox_of(e.conn, e.op, e.root)["roots"] == [e.root]
    _raw(e.conn, e.op, sandbox.WIDEN_KIND, {"op": e.op, "paths": ["/srv/z"], "owner_word": {}})
    assert "/srv/z" not in sandbox.sandbox_of(e.conn, e.op, e.root)["roots"]


def test_a_set_row_whose_transcript_line_does_not_hold_the_quote_is_ignored(tmp_path, monkeypatch):
    p, studio = _project(tmp_path, monkeypatch)
    line = _line("set", "op-062", ["root", studio])
    p.write([typed(line)])
    _set(p, ["root", studio], line)
    assert sandbox.sandbox_of(p.conn, "op-062", p.root) is not None
    p.write([typed("something else entirely")])                          # the line at the offset is gone
    assert sandbox.sandbox_of(p.conn, "op-062", p.root) is None


# ---------------------------------------------------------------- MINOR 4. the session transcripts
def test_a_tool_call_cannot_write_a_session_transcript_while_a_list_is_enforced(project, tmp_path):
    e = fx.make(project, tmp_path)
    cfg = os.environ["CLAUDE_CONFIG_DIR"]
    tr = os.path.join(cfg, "projects", ownerword.project_slug(e.root), e.sid + ".jsonl")
    sub = os.path.join(cfg, "projects", ownerword.project_slug(e.root), e.sid, "subagents", "agent-a.jsonl")
    memory = os.path.join(cfg, "projects", ownerword.project_slug(e.root), "memory", "notes.md")

    def d(tool, **tin):
        return sandbox_guard.decide({"tool_name": tool, "tool_input": tin, "session_id": e.sid, "cwd": e.root},
                                    root=e.root)
    assert d("Write", file_path=tr, content="x") is not None
    assert d("Edit", file_path=sub, old_string="a", new_string="b") is not None
    assert d("Bash", command="printf '%%s\\n' x >> %s" % tr) is not None
    assert d("Bash", command="cp /dev/null %s" % tr) is not None
    assert d("Write", file_path=memory, content="x") is None
    assert d("Write", file_path=os.path.join(cfg, "settings.json"), content="{}") is None


# ---------------------------------------------------------------- round 6 (check-adversarial-r5.md notes 1 to 3)
# Note 1: only the NEWEST owner message that holds an `alpaca sandbox set|widen <op>` line confirms, so an older,
# wider message can never be replayed once a newer one exists for that op.
def test_an_older_wider_message_never_confirms_once_a_newer_line_exists(tmp_path, monkeypatch):
    p, studio = _project(tmp_path, monkeypatch)
    wide = _line("set", "op-062", ["root", studio])
    narrow = _line("set", "op-062", ["root"])
    p.write([typed(wide), typed(narrow)])
    with pytest.raises(sandbox.SandboxError) as err:                     # the wide line was never recorded
        _set(p, ["root", studio], wide)
    assert err.value.reason == "SANDBOX-NOT-NEWEST"
    assert sandbox.sandbox_of(p.conn, "op-062", p.root) is None
    _set(p, ["root"], narrow)
    with pytest.raises(sandbox.SandboxError) as err:                     # the narrowing set is not undone
        _set(p, ["root", studio], wide)
    assert err.value.reason == "SANDBOX-NOT-NEWEST"
    assert sandbox.sandbox_of(p.conn, "op-062", p.root)["roots"] == [p.root]


def test_a_newer_widen_line_supersedes_an_older_set_line_and_another_op_does_not(tmp_path, monkeypatch):
    p, studio = _project(tmp_path, monkeypatch)
    fx.add_op(p.conn, "op-061", answer=fx.OP_061, sid=p.sid)
    setl = _line("set", "op-062", ["root", studio])
    p.write([typed(setl), typed("then\n" + _line("widen", "op-062", [studio + "-2"]))])
    with pytest.raises(sandbox.SandboxError) as err:
        _set(p, ["root", studio], setl)
    assert err.value.reason == "SANDBOX-NOT-NEWEST"
    p.write([typed(setl), typed(_line("set", "op-061", ["root"])), typed("ok")])
    assert _set(p, ["root", studio], setl)["kind"] == sandbox.SET_KIND     # a line for op-061 is not op-062's


def test_a_message_older_than_the_recorded_confirmation_of_another_session_never_confirms(tmp_path, monkeypatch):
    p, studio = _project(tmp_path, monkeypatch)
    narrow = _line("set", "op-062", ["root"])
    p.write([typed(narrow, ts="2999-01-01T00:00:00.000Z")])
    _set(p, ["root"], narrow)
    other = "sess-2"
    p.add_session(other)
    wide = _line("set", "op-062", ["root", studio])
    p.write([typed(wide, ts="2998-01-01T00:00:00.000Z")], sid=other)     # typed before the recorded one
    with pytest.raises(sandbox.SandboxError) as err:
        sandbox.set_paths(p.conn, "op-062", ["root", studio], by="owner", quote=wide, session=other, root=p.root,
                          environ=dict(p.env, CLAUDE_CODE_SESSION_ID=other))
    assert err.value.reason == "SANDBOX-NOT-NEWEST"
    assert sandbox.sandbox_of(p.conn, "op-062", p.root)["roots"] == [p.root]


# Note 2: the terminal path is refused for set and widen; only an owner message pasted in the session counts.
def test_set_and_widen_refuse_the_terminal_path(tmp_path, monkeypatch):
    from alpaca.tests.ownerword_fixtures import owner_at_terminal
    p, studio = _project(tmp_path, monkeypatch)
    owner_at_terminal(monkeypatch, "sandbox:op-062")
    line = _line("set", "op-062", ["root", studio])
    with pytest.raises(sandbox.SandboxError) as err:
        sandbox.set_paths(p.conn, "op-062", ["root", studio], by="owner", quote=line, session="cli", root=p.root,
                          environ={})
    assert err.value.reason == "SANDBOX-NOT-SESSION" and "message" in err.value.detail
    fx.confirm(p.conn, "op-062", ["root"], sid=p.sid)
    with pytest.raises(sandbox.SandboxError) as err:
        sandbox.widen(p.conn, "op-062", [studio], by="owner", quote=_line("widen", "op-062", [studio]),
                      session="cli", root=p.root, environ={})
    assert err.value.reason == "SANDBOX-NOT-SESSION"
    assert sandbox.sandbox_of(p.conn, "op-062", p.root)["roots"] == [p.root]


def test_a_terminal_path_row_is_not_honored(project, tmp_path):
    e = fx.make(project, tmp_path, confirm_paths=["root"])
    studio = e.studio
    line = _line("set", e.op, ["root", studio])
    _raw(e.conn, e.op, sandbox.SET_KIND, {"op": e.op, "paths": ["root", studio], "resolved": ["root", studio],
                                          "owner_word": {"path": "terminal", "transcript": None, "line": None,
                                                         "quote": line, "answer": "yes"}})
    assert sandbox.sandbox_of(e.conn, e.op, e.root)["roots"] == [e.root]
    _raw(e.conn, e.op, sandbox.WIDEN_KIND, {"op": e.op, "paths": [studio], "resolved": [studio],
                                            "owner_word": {"path": "terminal", "quote": _line("widen", e.op,
                                                                                               [studio])}})
    assert studio not in sandbox.sandbox_of(e.conn, e.op, e.root)["roots"]


def test_the_readback_and_the_deny_say_the_line_is_pasted_in_the_session(project, tmp_path):
    rb = sandbox.readback("Writes stay in this project root; no sudo", os.path.realpath(project), "op-062")
    assert "message of this session" in rb and "terminal" in rb
    e = fx.make(project, tmp_path)
    out = sandbox_guard.decide({"tool_name": "Write", "tool_input": {"file_path": "/srv/r6-probe/f", "content": "x"},
                                "session_id": e.sid, "cwd": e.root}, root=e.root)
    reason = out["hookSpecificOutput"]["permissionDecisionReason"]
    assert "message of this session" in reason and "terminal" in reason


# Note 3: a glob whose static prefix is a protected folder (or that stands for one with a wildcard) is too broad.
def _globs():
    home = os.path.realpath(os.path.expanduser("~"))
    root = home + "/Harness/Alpaca"
    parent = home + "/Harness"
    broad = [home + "/*", home + "/[a-z]*", home + "/.*", home + "/[!.]*", parent + "/*", parent + "/[A-Z]*",
             parent + "/Q*", "/var/tmp/*", "/var/tmp/[a-z]*", "/tmp/[o]p61-*", "/tmp/*x",
             "/home/[v]*/x", "/t*/op61", "/v?r/tmp/x", home[:-1] + "?/x", parent[:-1] + "*/QTRM64-studio-3"]
    narrow = ["/tmp/op61-*", "/var/tmp/op61-*", home + "/.cache/op61-*", parent + "/QTRM64-studio-*",
              "/srv/d*", "/srv/data/x*"]
    return root, broad, narrow


@pytest.mark.parametrize("k", range(16))
def test_a_glob_whose_static_prefix_is_a_protected_folder_is_too_broad(k):
    root, broad, _narrow = _globs()
    assert sandbox.broad_reason(sandbox._norm(broad[k]), root), broad[k]


@pytest.mark.parametrize("k", range(6))
def test_a_specific_glob_below_a_protected_folder_stays_allowed(k):
    root, _broad, narrow = _globs()
    assert sandbox.broad_reason(sandbox._norm(narrow[k]), root) is None, narrow[k]
