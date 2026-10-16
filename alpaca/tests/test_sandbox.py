"""alpaca/sandbox.py: the signed sandbox answer of an op, as the write-time guard reads it (op-059 t-695)."""
import json
import os

import pytest

from alpaca import claims, db, ownerword, sandbox
from alpaca.tests import sandbox_fixtures as fx
from alpaca.tests.ownerword_fixtures import Project, typed


@pytest.fixture(autouse=True)
def temp_base(tmp_path, monkeypatch):
    return fx.use_temp_base(monkeypatch, tmp_path)


@pytest.fixture
def root(tmp_path):
    r = os.path.realpath(str(tmp_path / "proj"))
    os.makedirs(os.path.join(r, ".alpaca", "worktrees"))
    return r


# ---------------------------------------------------------------- 0. round 4: only an owner-confirmed list binds
# The free-text parser failed three adversarial rounds; it left the trust path (lead's class rule). The
# guard enforces an op's sandbox only from the path list the owner confirmed with `alpaca sandbox set`
# (event `sandbox-set`), plus `widen`. The answer's reading is a proposal printed for the owner.
def test_a_free_text_answer_alone_is_not_enforced(project, tmp_path):
    e = fx.make(project, tmp_path, confirm_paths=False)
    assert sandbox.sandbox_of(e.conn, e.op, e.root, e.sid) is None
    assert sandbox.for_session(e.conn, e.sid, e.root) is None


def test_a_confirmed_list_is_the_sandbox_and_the_answer_only_proposes(project, tmp_path):
    e = fx.make(project, tmp_path, confirm_paths=False)
    ev = fx.confirm(e.conn, e.op, ["root", e.studio])
    sb = sandbox.sandbox_of(e.conn, e.op, e.root, e.sid)
    assert sb["roots"] == [e.root, e.studio] and sb["globs"] == [] and sb["scratch"] is False
    assert sb["event"] == ev["id"] and sb["paths"] == ["root", e.studio] and sb["answer_event"] == e.event
    assert sb["scratch_roots"] == []


@pytest.mark.parametrize("kw", [{"answer": "whatever you need"}, {"state": "open"}, {"source": "derived"},
                                {"batch": False}])
def test_a_confirmed_list_binds_whatever_the_answer_says_or_lacks(project, tmp_path, kw):
    e = fx.make(project, tmp_path, confirm_paths=["root"], **kw)
    sb = sandbox.for_session(e.conn, e.sid, e.root)
    assert sb is not None and sb["roots"] == [e.root]
    assert not sandbox.covers(os.path.join(e.studio, "x"), sb)


def test_a_confirmed_glob_and_a_home_path_are_read(project, tmp_path):
    e = fx.make(project, tmp_path, confirm_paths=["/tmp/op61-*", "~/.cache/op61"])
    sb = sandbox.for_session(e.conn, e.sid, e.root)
    assert sb["globs"] == ["/tmp/op61-*"]
    assert os.path.realpath(os.path.expanduser("~/.cache/op61")) in sb["roots"]
    assert sandbox.covers("/tmp/op61-a/x", sb) and not sandbox.covers("/tmp/op62-a/x", sb)


def test_a_later_set_replaces_the_list_and_a_widen_adds_to_the_current_one(project, tmp_path):
    e = fx.make(project, tmp_path, confirm_paths=["root", "/srv/one"])

    def widen(path):                             # round 5: a proved row (a raw one is ignored, MINOR 5)
        return fx.widen(e.conn, e.op, [path])
    widen("/srv/wide-before")
    assert sandbox.sandbox_of(e.conn, e.op, e.root)["roots"] == [e.root, "/srv/one", "/srv/wide-before"]
    fx.confirm(e.conn, e.op, ["root"])
    assert sandbox.sandbox_of(e.conn, e.op, e.root)["roots"] == [e.root]
    w = widen("/srv/wide-after")
    sb = sandbox.sandbox_of(e.conn, e.op, e.root)
    assert sb["roots"] == [e.root, "/srv/wide-after"] and sb["widened"] == ["/srv/wide-after"]
    assert w["id"] in sb["events"]


def test_the_scratchpad_keyword_allows_every_session_scratchpad_of_this_project(project, tmp_path, monkeypatch):
    """check-adversarial-r3.md MINOR 6: a session told to use another session's scratchpad (a resume or a
    fork) wrote there and was denied. The keyword maps to the project's scratchpad root, the folder that
    holds every session's scratchpad, not only the hook's own session folder."""
    from alpaca import paths
    top = tmp_path / "ctmp"
    monkeypatch.setenv("CLAUDE_CODE_TMPDIR", str(top))
    e = fx.make(project, tmp_path, confirm_paths=["root"])
    base = top / ("claude-%d" % os.getuid()) / paths.escape_cwd(e.root)
    other = base / "b7b36883-other-session" / "scratchpad"
    other.mkdir(parents=True)
    own = base / e.sid / "scratchpad"
    own.mkdir(parents=True)
    elsewhere = top / ("claude-%d" % os.getuid()) / "-another-project" / "s" / "scratchpad"
    elsewhere.mkdir(parents=True)
    sb = sandbox.for_session(e.conn, e.sid, e.root)
    assert sandbox.covers(str(own / "f"), sb), "the session's own scratchpad is always allowed"
    assert not sandbox.covers(str(other / "s1.out"), sb), "without the keyword only the own one"
    fx.confirm(e.conn, e.op, ["root", "scratchpad"])
    sb = sandbox.for_session(e.conn, e.sid, e.root)
    assert sb["scratch"] is True and sb["scratch_roots"] == [os.path.realpath(str(base))]
    assert sandbox.covers(str(other / "s1.out"), sb)
    assert not sandbox.covers(str(elsewhere / "f"), sb)


# the owner's words: `alpaca sandbox set`. Round 5 (check-adversarial-r4.md B-1): the owner pastes the exact
# command line the readback prints, after the op's sandbox question was asked (test_sandbox_confirm.py)
def _set_project(tmp_path, monkeypatch):
    p = Project(tmp_path, monkeypatch)
    studio = os.path.realpath(str(tmp_path)) + "/studio"
    fx.add_op(p.conn, "op-062", answer=fx.OP_062.format(studio=studio), sid=p.sid)
    fx.add_task(p.conn, "t-1", "op-062", sid=p.sid)
    p.ask("batch:op-062")
    return p, studio


def test_set_records_the_owner_confirmed_list_and_the_session_is_bound(tmp_path, monkeypatch):
    p, studio = _set_project(tmp_path, monkeypatch)
    assert sandbox.for_session(p.conn, p.sid, p.root) is None
    msg = sandbox.command_line("set", "op-062", ["root", studio, "scratchpad"])
    p.write([typed(msg)])
    sandbox._write(p.root, ("cache",), "any.json", "{}")
    ev = sandbox.set_paths(p.conn, "op-062", ["root", studio, "scratchpad"], by="owner", quote=msg,
                           session=p.sid, root=p.root, environ=p.env)
    data = json.loads(ev["data"])
    assert ev["kind"] == "sandbox-set" and data["paths"] == ["root", studio, "scratchpad"]
    assert data["resolved"] == ["root", studio, "scratchpad"]
    assert data["owner_word"]["quote"] == msg
    assert not os.path.exists(sandbox.cache_path(p.root, "any"))
    sb = sandbox.for_session(p.conn, p.sid, p.root)
    assert sb["roots"] == [p.root, studio] and sb["scratch"] is True and sb["events"] == {"op-062": ev["id"]}


def test_set_refuses_a_list_the_pasted_line_does_not_name_and_a_relative_or_empty_list(tmp_path, monkeypatch):
    p, studio = _set_project(tmp_path, monkeypatch)
    msg = sandbox.command_line("set", "op-062", ["root"])
    p.write([typed(msg)])
    for paths_, reason in (([studio], "SANDBOX-NOT-CONFIRMED"), (["scratchpad"], "SANDBOX-NOT-CONFIRMED"),
                           (["root", "scratchpad"], "SANDBOX-NOT-CONFIRMED"),
                           (["src/x"], "SANDBOX-PATH-ABSOLUTE"), ([], "SANDBOX-PATH-ABSOLUTE")):
        with pytest.raises(sandbox.SandboxError) as err:
            sandbox.set_paths(p.conn, "op-062", paths_, by="owner", quote=msg, session=p.sid, root=p.root,
                              environ=p.env)
        assert err.value.reason == reason, paths_
    assert sandbox.sandbox_of(p.conn, "op-062", p.root) is None


def test_set_refuses_a_quote_no_owner_message_holds_and_a_closed_op(tmp_path, monkeypatch):
    p, studio = _set_project(tmp_path, monkeypatch)
    p.write([typed("hello there")])
    with pytest.raises(ownerword.OwnerWordRefusal):
        sandbox.set_paths(p.conn, "op-062", ["root"], by="owner",
                          quote=sandbox.command_line("set", "op-062", ["root"]),
                          session=p.sid, root=p.root, environ=p.env)
    db.patch(p.conn, "ops", "id", "op-062", {"status": "closed"})
    with pytest.raises(sandbox.SandboxError) as err:
        sandbox.set_paths(p.conn, "op-062", ["root"], by="owner", quote="hello there", session=p.sid,
                          root=p.root, environ=p.env)
    assert err.value.reason == "SANDBOX-OP"


def test_the_set_verb_runs_from_the_cli(tmp_path, monkeypatch, capsys):
    from alpaca import cli
    p, studio = _set_project(tmp_path, monkeypatch)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", p.sid)
    monkeypatch.chdir(p.root)
    msg = sandbox.command_line("set", "op-062", ["root", studio])
    p.write([typed(msg)])
    capsys.readouterr()
    code = cli.main(["sandbox", "set", "op-062", "--path", "root", "--path", studio,
                     "--by", "owner", "--quote", msg])
    out = capsys.readouterr().out
    assert code == cli.PASS, out
    assert "op-062 sandbox set" in out and "ENFORCED" in out
    assert sandbox.sandbox_of(p.conn, "op-062", p.root)["roots"] == [p.root, studio]


# ---------------------------------------------------------------- 0b. the proposal and the set command
def test_the_proposal_of_each_recorded_answer(root):
    assert sandbox.proposal(sandbox.parse_answer(fx.OP_062.format(studio=STUDIO), root)) == \
        ["root", STUDIO, "scratchpad"]
    assert sandbox.proposal(sandbox.parse_answer(fx.OP_061, root)) == ["root", "/tmp/op61-*", "scratchpad"]
    assert sandbox.proposal(sandbox.parse_answer(fx.OP_063, root)) == ["root"]
    assert sandbox.proposal(sandbox.parse_answer(fx.OP_045, root)) == [
        "root", os.path.join(root, ".alpaca/worktrees/op045-*")]
    assert sandbox.proposal(sandbox.parse_answer("whatever you need", root)) == []


def test_the_readback_is_a_proposal_with_the_set_command_and_never_enforced(root):
    rb = sandbox.readback(fx.OP_062.format(studio=STUDIO), root, "op-062")
    assert "proposal" in rb and "NOT ENFORCED" in rb
    assert ("alpaca sandbox set op-062 --path root --path %s --path scratchpad --by owner --quote" % STUDIO) in rb
    assert not [line for line in rb.splitlines() if line.strip().startswith("ENFORCED")]
    rb = sandbox.readback("whatever you need", root, "op-062")
    assert "NOT ENFORCED" in rb and "proposed paths: none" in rb and "alpaca sandbox set op-062 --path" in rb


def test_show_of_an_op_without_a_confirmed_list_says_not_enforced_and_prints_the_proposal(project, tmp_path,
                                                                                         capsys):
    import json
    from alpaca import cli
    e = fx.make(project, tmp_path, confirm_paths=False)
    capsys.readouterr()
    assert cli.main(["sandbox", "show", e.op, "--session-id", e.sid]) == cli.PASS
    out = capsys.readouterr().out
    assert "not enforced: no confirmed path list" in out
    assert ("alpaca sandbox set op-062 --path root --path %s --path scratchpad --by owner --quote" % e.studio) in out
    assert cli.main(["sandbox", "show", e.op, "--session-id", e.sid, "--json"]) == cli.PASS
    got = json.loads(capsys.readouterr().out)
    assert got["sandbox"] is None and got["enforced"] is False
    assert got["proposal"] == ["root", e.studio, "scratchpad"]


def test_show_of_a_confirmed_op_names_the_list_and_its_event(project, tmp_path, capsys):
    from alpaca import cli
    e = fx.make(project, tmp_path)
    capsys.readouterr()
    assert cli.main(["sandbox", "show", e.op, "--session-id", e.sid]) == cli.PASS
    out = capsys.readouterr().out
    assert "owner-confirmed path list" in out and str(e.set_event) in out and "ENFORCED" in out
    assert "not enforced: no confirmed path list" not in out


# ---------------------------------------------------------------- 1. the parser, a proposer only
# Round 3 (investigate-r2.md sections 2 to 4): a place is proposed only when a unit of the answer is
# proved positive from a closed vocabulary. Any unknown word, negation, read word or exclusion gives
# nothing. The table below is the investigator's 72 rows plus the probes of its section 3.4. Round 4: these
# are PROPOSAL tests. What the parser reads is printed for the owner and is never enforced by itself; the
# `covers` checks below say what the proposal would allow if the owner confirmed it as it stands.
STUDIO = "/srv/work/QTRM64-studio"
STUDIO2 = "/srv/work/QTRM64-studio-2"
R = "Writes stay in this project root"
HOME = "/".join(("", "home", "dev"))  # a home folder, spelled so no file holds a literal home path
OP_057 = ("Read and write only inside this project root and the session scratchpad; "
          "no new packages")
WT = "{root}/.alpaca/worktrees/op045-*"


def test_op_062_answer_gives_the_root_the_studio_and_the_scratchpad(root):
    got = sandbox.parse_answer(fx.OP_062.format(studio=STUDIO), root)
    assert got["roots"] == [root, STUDIO]
    assert got["globs"] == [] and got["scratch"] is True and got["enforced"] is True
    assert "no sudo" in [r["clause"] for r in got["refused"]]


def test_op_061_answer_gives_a_tmp_glob_and_not_the_github_host(root):
    got = sandbox.parse_answer(fx.OP_061, root)
    assert got["roots"] == [root] and got["globs"] == ["/tmp/op61-*"] and got["scratch"] is True
    assert not any("github" in r for r in got["roots"])
    assert any("github.com" in r["clause"] for r in got["refused"])


def test_op_045_answer_reads_the_worktree_glob_and_not_the_read_clause(root):
    got = sandbox.parse_answer(fx.OP_045, root)
    assert got["roots"] == [root]
    assert got["globs"] == [os.path.join(root, ".alpaca/worktrees/op045-*")]
    assert not any("Drop-in" in r for r in got["roots"] + got["globs"])
    assert any(r["clause"].startswith("read /srv") for r in got["refused"])


def test_the_six_recorded_answers_keep_the_design_roots(root):
    """The six owner answers in the live record (events 149192, 255444, 258122, 267904, 268565,
    269849), with their texts as recorded: (roots, globs, scratchpad word, enforced)."""
    want = [
        (fx.OP_045, [root], [os.path.join(root, ".alpaca/worktrees/op045-*")], False),
        (fx.OP_056, [root], [], True),
        (OP_057, [root], [], True),
        (fx.OP_061, [root], ["/tmp/op61-*"], True),
        (fx.OP_062.format(studio=STUDIO), [root, STUDIO], [], True),
        (fx.OP_063, [root], [], False),
    ]
    for text, roots, globs, scratch in want:
        got = sandbox.parse_answer(text, root)
        assert (got["roots"], got["globs"], got["scratch"], got["enforced"]) == (roots, globs, scratch, True), text


#: (answer, extra roots besides the project root, globs, scratchpad word, enforced, denied, allowed)
TABLE = [
    # ---- the six recorded answers (investigate-r2.md 3.1)
    (fx.OP_062.format(studio=STUDIO), [STUDIO], [], True, True, [STUDIO2, "/tmp/x"], [STUDIO + "/a"]),
    (fx.OP_061, [], ["/tmp/op61-*"], True, True, ["/tmp/other"], ["/tmp/op61-a/x"]),
    (fx.OP_045, [], [WT], False, True, ["/tmp/x", "/srv/drop-in/x"], ["{root}/.alpaca/worktrees/op045-a/f"]),
    (fx.OP_056, [], [], True, True, ["/tmp/x"], ["{root}/a"]),
    (OP_057, [], [], True, True, ["/tmp/x"], ["{root}/a"]),
    (fx.OP_063, [], [], False, True, ["/tmp/x"], ["{root}/a"]),
    # ---- round 1 rows (3.2)
    (R + "; never write to /tmp", [], [], False, True, ["/tmp/x"], ["{root}/a"]),
    (R + ", not /tmp", [], [], False, True, ["/tmp/x"], ["{root}/a"]),
    ("Do not write to /tmp; keep writes in this project root", [], [], False, True, ["/tmp/x"], ["{root}/a"]),
    (R + "; nothing goes to " + STUDIO2, [], [], False, True, [STUDIO2 + "/x"], ["{root}/a"]),
    (R + " except " + STUDIO, [], [], False, True, [STUDIO + "/x"], ["{root}/a"]),
    ("No writes to /tmp, use this project root", [], [], False, True, ["/tmp/x"], ["{root}/a"]),
    ("Anywhere you need, but never touch /etc", [], [], False, False, [], []),
    ("Whatever you need; temp files go to the scratchpad", [], [], True, False, [], []),
    ("Do not write to /tmp", [], [], False, False, [], []),
    # ---- round 2 rows P2: a forbidden or read-only path is never writable (3.3)
    (R + "; exclude /tmp", [], [], False, True, ["/tmp/x"], ["{root}/a"]),
    (R + "; /tmp is prohibited", [], [], False, True, ["/tmp/x"], ["{root}/a"]),
    (R + "; /tmp is banned", [], [], False, True, ["/tmp/x"], ["{root}/a"]),
    (R + "; /tmp is disallowed", [], [], False, True, ["/tmp/x"], ["{root}/a"]),
    (R + "; /tmp is out of bounds", [], [], False, True, ["/tmp/x"], ["{root}/a"]),
    (R + "; stay clear of /tmp", [], [], False, True, ["/tmp/x"], ["{root}/a"]),
    (R + "; refrain from writing to /tmp", [], [], False, True, ["/tmp/x"], ["{root}/a"]),
    (R + "; anything but /tmp", [], [], False, True, ["/tmp/x"], ["{root}/a"]),
    (R + ", with the exception of " + STUDIO, [], [], False, True, [STUDIO + "/x"], ["{root}/a"]),
    ("Write only inside this repo and read /srv/drop-in", [], [], False, True, ["/srv/drop-in/x"], ["{root}/a"]),
    (R + "; /srv/drop-in is read-only", [], [], False, True, ["/srv/drop-in/x"], ["{root}/a"]),
    (R + "; only read /srv/drop-in", [], [], False, True, ["/srv/drop-in/x"], ["{root}/a"]),
    (R + "; you may read /srv/drop-in", [], [], False, True, ["/srv/drop-in/x"], ["{root}/a"]),
    (R + "; /srv/drop-in for reading only", [], [], False, True, ["/srv/drop-in/x"], ["{root}/a"]),
    (R + "; leave /srv/drop-in alone", [], [], False, True, ["/srv/drop-in/x"], ["{root}/a"]),
    (R + "; keep /srv/drop-in untouched", [], [], False, True, ["/srv/drop-in/x"], ["{root}/a"]),
    (R + "; never write to /tmp, the home folder " + HOME + " or /etc", [], [], False, True,
     [HOME + "/x", "/etc/x", "/tmp/x"], ["{root}/a"]),
    # ---- round 2 rows P3: a plain restriction still enforces
    (R + " (never /tmp)", [], [], False, True, ["/tmp/x"], ["{root}/a"]),
    (R + " and " + STUDIO + " (not elsewhere)", [STUDIO], [], False, True, ["/tmp/x"], [STUDIO + "/a"]),
    (R + " and anywhere under " + STUDIO, [STUDIO], [], False, True, ["/tmp/x"], [STUDIO + "/a"]),
    # ---- the branch's own table and the new probes (3.4)
    (R + " but not /tmp", [], [], False, True, ["/tmp/x"], ["{root}/a"]),
    ("Don't write to /tmp, /var/tmp or /etc; keep writes in this project root", [], [], False, True,
     ["/tmp/x", "/var/tmp/x", "/etc/x"], ["{root}/a"]),
    (R + ", not /tmp, /var/tmp or /etc", [], [], False, True, ["/tmp/x", "/var/tmp/x", "/etc/x"], ["{root}/a"]),
    ("Any folder is fine, no limits", [], [], False, False, [], []),
    ("Write to " + STUDIO + " without touching /tmp", [STUDIO], [], False, True, ["/tmp/x"], [STUDIO + "/a"]),
    ("Only " + STUDIO + " is allowed", [STUDIO], [], False, True, ["/tmp/x"], [STUDIO + "/a", "{root}/a"]),
    ("Writes stay in /srv/no/work", ["/srv/no/work"], [], False, True, ["/srv/no/x"], ["/srv/no/work/a"]),
    ("/tmp and /etc are off limits; " + R, [], [], False, True, ["/tmp/x", "/etc/x"], ["{root}/a"]),
    ("/tmp (and /etc) are off limits", [], [], False, False, [], []),
    (R + ", /srv/x and /srv/y are read-only", [], [], False, True, ["/srv/x/a", "/srv/y/a"], ["{root}/a"]),
    (R + "; keep /srv/data", [], [], False, True, ["/srv/data/a"], ["{root}/a"]),
    (R + " and files in /tmp stay", [], [], False, True, ["/tmp/x"], ["{root}/a"]),
    ("Writes stay in /srv/studio but not /srv/studio/secrets", ["/srv/studio"], [], False, True,
     ["/srv/studio/secrets/k", "/tmp/x"], ["/srv/studio/a"]),
    (R + " but never touch .alpaca/alpaca.db", [], [], False, True, ["/tmp/x"], ["{root}/a"]),
    ("/srv/a, /srv/b are fine", ["/srv/a", "/srv/b"], [], False, True, ["/srv/c"], ["/srv/a/x", "/srv/b/x"]),
    (R + " and /srv/x, but never for basetemps", [], [], False, True, ["/srv/x/a"], ["{root}/a"]),
    ("this project root and " + STUDIO, [STUDIO], [], False, True, ["/tmp/x"], [STUDIO + "/a", "{root}/a"]),
    ("/srv/a and /srv/b", [], [], False, False, [], []),
    (R + "; /tmp, /etc: off limits", [], [], False, True, ["/tmp/x", "/etc/x"], ["{root}/a"]),
    (R + "; /srv/x is the owner's", [], [], False, True, ["/srv/x/a"], ["{root}/a"]),
    (R + "; writing to /tmp is verboten", [], [], False, True, ["/tmp/x"], ["{root}/a"]),
    (R + "; /tmp only for reading", [], [], False, True, ["/tmp/x"], ["{root}/a"]),
    (R + "; plus /tmp if needed", [], [], False, True, ["/tmp/x"], ["{root}/a"]),
    (R + "; " + HOME + "/x too", [], [], False, True, [HOME + "/x/a"], ["{root}/a"]),
    ("/tmp is ok?", [], [], False, False, [], []),
    (R + " and /srv/x; keep /srv/x/raw read-only", ["/srv/x"], [], False, True, ["/srv/x/raw/f", "/tmp/x"],
     ["/srv/x/a"]),
    (R + ", \u4e0d /tmp", [], [], False, True, ["/tmp/x"], ["{root}/a"]),
    (R + "; khong ghi vao /tmp", [], [], False, True, ["/tmp/x"], ["{root}/a"]),
    ("Chi ghi trong /srv/du-an", [], [], False, False, [], []),
    ("Write anywhere", [], [], False, False, [], []),
    ("Write anywhere except /etc", [], [], False, False, [], []),
    (R + ", nowhere else", [], [], False, True, ["/tmp/x"], ["{root}/a"]),
    (R + "; you may use /srv/drop-in", [], [], False, True, ["/srv/drop-in/x"], ["{root}/a"]),
    ("Use the scratchpad and /srv/x", [], [], True, True, ["/srv/x/a"], []),
    # ---- more wordings of the same two directions
    ("Read-write only inside this repo", [], [], False, True, ["/tmp/x"], ["{root}/a"]),
    ("read/write: /srv/rw", ["/srv/rw"], [], False, True, ["/srv/other"], ["/srv/rw/a"]),
    ("Writes stay in this project root. Never touch /srv/keep", [], [], False, True, ["/srv/keep/a"], ["{root}/a"]),
    (R + ", and put temp files in /srv/tmpz", ["/srv/tmpz"], [], False, True, ["/srv/other"], ["/srv/tmpz/a"]),
    (R + "; /srv/no is forbidden", [], [], False, True, ["/srv/no/a"], ["{root}/a"]),
    (R + "; hands off /srv/no", [], [], False, True, ["/srv/no/a"], ["{root}/a"]),
]


@pytest.mark.parametrize("answer,roots,globs,scratch,enforced,denied,allowed", TABLE)
def test_the_proposal_table_reads_as_recorded(root, answer, roots, globs, scratch, enforced, denied, allowed):
    got = sandbox.parse_answer(answer, root)
    assert sorted(set(got["roots"]) - {root}) == sorted(roots), got
    assert got["globs"] == [g.format(root=root) for g in globs], got
    assert got["scratch"] is scratch or (not enforced)
    assert got["enforced"] is enforced, got
    if not enforced:
        return
    sb = {"roots": got["roots"], "globs": got["globs"], "excluded": got["excluded"],
          "always": sandbox._always(root, fx.SID), "temp_root": ""}
    for path in denied:
        assert not sandbox.covers(path.format(root=root), sb), path
    for path in allowed:
        assert sandbox.covers(path.format(root=root), sb), path


def test_the_table_has_at_least_72_rows():
    assert len(TABLE) >= 72


def test_the_project_root_is_always_allowed_whatever_the_answer_says(root):
    got = sandbox.parse_answer("Only " + STUDIO + " is allowed", root)
    assert got["enforced"]
    assert root in sandbox._always(root, fx.SID)
    assert sandbox.covers(os.path.join(root, "src", "a.py"),
                          {"roots": got["roots"], "always": sandbox._always(root, fx.SID)})


def test_a_path_named_no_is_not_a_negation(root):
    got = sandbox.parse_answer("Writes stay in /srv/no/work", root)
    assert got["roots"] == ["/srv/no/work"] and got["refused"] == []


# the words that used to drop a clause now refuse it for what they are: each stays a deny
@pytest.mark.parametrize("cue", ["no", "not", "never", "nothing", "don't", "do not", "without", "except",
                                 "outside", "avoid", "cannot", "off limits", "stay out of", "exclude",
                                 "prohibited", "hands off"])
def test_each_negation_word_keeps_its_place_out_of_the_roots(root, cue):
    got = sandbox.parse_answer("Writes stay in this project root; %s /srv/x/y" % cue, root)
    assert "/srv/x/y" not in got["roots"], cue
    assert got["enforced"], cue


def test_a_refused_clause_says_why(root):
    got = sandbox.parse_answer(R + "; /tmp is verboten; never write to /srv/n; /srv/r is read-only; "
                               "/srv/p is prohibited", root)
    reasons = {r["clause"]: r["reason"] for r in got["refused"]}
    assert reasons["/tmp is verboten"] == 'unknown word "verboten"'
    assert reasons["/srv/p is prohibited"] == "negation"
    assert reasons["never write to /srv/n"] == "negation"
    assert reasons["/srv/r is read-only"] == "read-only"
    got = sandbox.parse_answer(R + ", not /tmp, /var/tmp or /etc", root)
    assert [r["reason"] for r in got["refused"]] == ["negation", "continues a refused unit",
                                                       "continues a refused unit"]
    got = sandbox.parse_answer(R + " and /srv/x, but never for basetemps", root)
    assert any(r["reason"].startswith("qualified by") for r in got["refused"])


def test_a_refused_path_inside_a_granted_folder_is_excluded_and_inside_the_root_is_not_enforceable(root):
    got = sandbox.parse_answer("Writes stay in /srv/studio but not /srv/studio/secrets", root)
    assert got["roots"] == ["/srv/studio"] and got["excluded"] == ["/srv/studio/secrets"]
    sb = {"roots": got["roots"], "excluded": got["excluded"], "always": [], "temp_root": ""}
    assert not sandbox.covers("/srv/studio/secrets", sb) and not sandbox.covers("/srv/studio/secrets/k", sb)
    assert sandbox.covers("/srv/studio/secrets2/k", sb) and sandbox.covers("/srv/studio/a", sb)
    got = sandbox.parse_answer(R + " but never touch .alpaca/alpaca.db", root)
    assert got["not_enforceable"] == [os.path.join(root, ".alpaca/alpaca.db")] and got["excluded"] == []
    got = sandbox.parse_answer(R + " and /srv/studio; nothing in /srv/studio/raw", root)
    assert got["excluded"] == ["/srv/studio/raw"]


def test_a_granted_path_the_same_answer_refuses_is_dropped_and_a_narrower_grant_is_kept(root):
    got = sandbox.parse_answer(R + " and /srv/a; never write to /srv/a", root)
    assert "/srv/a" not in got["roots"]
    got = sandbox.parse_answer("Writes stay in /srv/a and /srv/a/inner; never write to /srv/a", root)
    assert "/srv/a" not in got["roots"] and "/srv/a/inner" in got["roots"]


def test_a_non_english_or_deny_only_answer_gives_no_proposal_and_the_readback_says_so(root):
    for text in ("Chi ghi trong /srv/du-an", "Write anywhere except /etc", "Do not write to /tmp"):
        got = sandbox.parse_answer(text, root)
        assert not got["enforced"] and sandbox.proposal(got) == [], text
        rb = sandbox.readback(text, root)
        assert "NOT ENFORCED" in rb and "proposed paths: none" in rb, rb
        assert "English" in rb


def test_a_question_grants_nothing(root):
    got = sandbox.parse_answer("Is /srv/q ok? " + R, root)
    assert "/srv/q" not in got["roots"] and root in got["roots"]


# ---------------------------------------------------------------- 1b. the read back to the owner
def test_the_readback_names_the_proposed_places_and_the_refused_clauses(root):
    rb = sandbox.readback(fx.OP_062.format(studio=STUDIO), root)
    assert STUDIO in rb and "scratchpad" in rb and "root" in rb
    assert "no sudo" in rb and "no system installs" in rb
    assert "NOT ENFORCED" in rb
    assert "/tmp/sbx" in rb and ".alpaca/" in rb
    rb = sandbox.readback(R + " and /srv/studio; nothing in /srv/studio/raw; never touch .alpaca/alpaca.db", root)
    assert "/srv/studio/raw" in rb and "excluded" in rb
    assert ".alpaca/alpaca.db" in rb and "cannot be enforced" in rb


def test_the_readback_gives_one_proposed_paths_line(root):
    # The 'none' line is only for a reading with no proposed path, not for one with no broad entry.
    rb = sandbox.readback(fx.OP_062.format(studio=STUDIO), root)
    lines = [line for line in rb.splitlines() if "proposed paths:" in line]
    assert len(lines) == 1 and "none" not in lines[0], lines
    rb = sandbox.readback("Write wherever you need to; no sudo", root)
    lines = [line for line in rb.splitlines() if "proposed paths:" in line]
    assert len(lines) == 1 and "none" in lines[0], lines


def test_the_readback_is_pure_text_and_makes_no_folder(root, temp_base):
    sandbox.readback(fx.OP_062.format(studio=STUDIO), root)
    assert os.listdir(temp_base) == []


def test_the_batch_text_shows_the_readback_under_the_sandbox_item(project, tmp_path):
    from alpaca import batch
    e = fx.make(project, tmp_path)
    plain = batch.render(e.conn, e.op)
    shown = batch.render(e.conn, e.op, e.root)
    assert "ANSWERED (your answer)" in plain and "ENFORCED" not in plain
    assert shown.startswith(plain.split("\n[sandbox]")[0])
    after = shown.split("[sandbox]")[1]
    assert "NOT ENFORCED" in after and e.studio in after and "no sudo" in after
    assert "alpaca sandbox set op-062 --path root" in after
    assert shown.index("NOT ENFORCED") > shown.index("ANSWERED (your answer)")


def test_show_prints_the_same_reading_as_the_readback(project, tmp_path, capsys):
    from alpaca import cli
    e = fx.make(project, tmp_path, confirm_paths=False)
    capsys.readouterr()
    assert cli.main(["sandbox", "show", e.op, "--session-id", e.sid]) == cli.PASS
    out = capsys.readouterr().out
    for line in sandbox.readback(fx.OP_062.format(studio=e.studio), e.root, e.op).splitlines():
        assert line in out, line


# ---------------------------------------------------------------- 2. which answers count
def test_a_confirmed_list_is_a_sandbox(project, tmp_path):
    e = fx.make(project, tmp_path)
    sb = sandbox.sandbox_of(e.conn, e.op, e.root, e.sid)
    assert sb["roots"] == [e.root, e.studio] and sb["event"] == e.set_event and sb["answer_event"] == e.event


@pytest.mark.parametrize("kw", [
    {"answer": "whatever you need"},                     # no path in it
    {"state": "open"},                                   # still a question
    {"state": "proposed"},
    {"source": "derived"},                               # the agent's own word
    {"batch": False},                                    # an op with no batch (before op-019)
    {"status": "closed"},
])
def test_no_sandbox_without_a_signed_owner_answer(project, tmp_path, kw):
    e = fx.make(project, tmp_path, claim=False, **kw)
    assert sandbox.sandbox_of(e.conn, e.op, e.root, e.sid) is None


# ---------------------------------------------------------------- 3. which sandbox a session is under
def test_a_session_holding_a_task_of_the_op_is_under_its_sandbox(project, tmp_path, temp_base):
    e = fx.make(project, tmp_path)
    sb = sandbox.for_session(e.conn, e.sid, e.root)
    assert sb["ops"] == ["op-062"] and sb["events"] == {"op-062": e.set_event}
    assert sb["roots"] == [e.root, e.studio]
    assert sb["temp_root"] == os.path.join(temp_base, "sbxsess-aaa")


def test_the_session_that_asked_the_batch_is_under_it_between_claims(project, tmp_path):
    e = fx.make(project, tmp_path, claim=False)
    assert sandbox.for_session(e.conn, e.sid, e.root) is None
    fx.ask(e, e.op)
    assert sandbox.for_session(e.conn, e.sid, e.root)["ops"] == ["op-062"]


def test_a_released_or_expired_claim_ends_the_sandbox(project, tmp_path):
    e = fx.make(project, tmp_path)
    claims.release(e.conn, "t-1", "claude-sess", session=e.sid)
    assert sandbox.for_session(e.conn, e.sid, e.root) is None


def test_another_session_is_not_under_it(project, tmp_path):
    e = fx.make(project, tmp_path)
    assert sandbox.for_session(e.conn, "sess-other", e.root) is None
    fx.ask(e, e.op)
    assert sandbox.for_session(e.conn, "sess-other", e.root) is None


def test_a_session_in_a_sandboxed_and_an_unsandboxed_op_is_under_the_confirmed_list(project, tmp_path):
    """Round 5 (check-adversarial-r4.md B-3): an op with no confirmed list adds nothing and no longer switches
    the confirmed list of the other op off (before: under none)."""
    e = fx.make(project, tmp_path)
    fx.add_op(e.conn, "op-070", state="open", sid="s")
    fx.add_task(e.conn, "t-2", "op-070", sid=e.sid, worker="claude-sess")
    sb = sandbox.for_session(e.conn, e.sid, e.root)
    assert sb["ops"] == ["op-062"] and sb["unconfirmed"] == ["op-070"] and sb["roots"] == [e.root, e.studio]


def test_a_closed_op_never_binds(project, tmp_path):
    e = fx.make(project, tmp_path)
    db.patch(e.conn, "ops", "id", e.op, {"status": "closed"})
    assert sandbox.for_session(e.conn, e.sid, e.root) is None


# ---------------------------------------------------------------- 3b. the session temp root (option A)
def test_the_temp_root_is_the_same_short_folder_for_every_answer(project, tmp_path, temp_base):
    e = fx.make(project, tmp_path, answer=fx.OP_061)             # a /tmp/op61-* glob changes nothing
    sb = sandbox.for_session(e.conn, e.sid, e.root)
    assert sb["temp_root"] == os.path.join(temp_base, "sbxsess-aaa")
    assert sb["temp_ok"] is True and sandbox.covers(os.path.join(sb["temp_root"], "x"), sb)


def test_the_real_temp_root_is_16_bytes_outside_every_project_and_fits_a_socket(monkeypatch):
    monkeypatch.undo()                                   # the real constant, nothing is made
    from alpaca import paths
    sid = "64d8f84e-8831-4d99-a9e0-7a71666a84bb"
    t = sandbox.temp_root("/any/root", sid)
    assert t == "/tmp/sbx64d8f84e" and len(t) <= sandbox.TEMP_MAX
    with pytest.raises(paths.RootNotFound):
        paths._walk(t)
    # the longest socket the suite makes: Chrome's singleton under an xdist worker (investigate-r1.md Item 2)
    sock = t + "/pytest-of-dev/pytest-999/popen-gw99/tempfile/com.google.Chrome.XXXXXX/SingletonSocket"
    assert len(sock) <= 107


def test_the_temp_root_is_made_private_with_a_marker_and_made_again_safely(tmp_path, temp_base):
    t = os.path.join(temp_base, "sbxabcd1234")
    assert sandbox.make_temp_root(t, "abcd1234-full-id")
    assert os.stat(t).st_mode & 0o777 == 0o700
    assert open(os.path.join(t, sandbox.MARKER)).read() == "abcd1234-full-id"
    assert sandbox.make_temp_root(t, "abcd1234-full-id")                      # a second call
    assert not sandbox.make_temp_root(t, "abcd1234-other-id")                 # same 8 characters


def test_a_temp_root_someone_else_made_first_is_never_used(tmp_path, temp_base, monkeypatch):
    sid = "abcd1234-full-id"
    t = os.path.join(temp_base, "sbxabcd1234")
    os.mkdir(t, 0o755)                                                          # open to others
    assert not sandbox.make_temp_root(t, sid)
    os.chmod(t, 0o700)
    open(os.path.join(t, "keep"), "w").close()                                  # no marker, not empty
    assert not sandbox.make_temp_root(t, sid)
    os.unlink(os.path.join(t, "keep"))
    monkeypatch.setattr(sandbox, "_uid", lambda: os.getuid() + 1)               # another owner
    assert not sandbox.make_temp_root(t, sid)
    monkeypatch.undo()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    lnk = os.path.join(temp_base, "sbxlink0000")
    os.symlink(str(elsewhere), lnk)
    assert not sandbox.make_temp_root(lnk, "link0000-id")
    assert os.listdir(str(elsewhere)) == []
    plain = os.path.join(temp_base, "sbxfile0000")
    open(plain, "w").close()
    assert not sandbox.make_temp_root(plain, "file0000-id")


def test_an_empty_folder_of_ours_without_a_marker_is_adopted(temp_base):
    t = os.path.join(temp_base, "sbxabcd1234")
    os.mkdir(t, 0o700)                                                          # a crash before the marker
    assert sandbox.make_temp_root(t, "abcd1234-full-id")
    assert open(os.path.join(t, sandbox.MARKER)).read() == "abcd1234-full-id"


def test_an_unsafe_temp_root_writes_no_env_file_and_is_not_an_allowed_path(project, tmp_path, temp_base):
    e = fx.make(project, tmp_path)
    t = os.path.join(temp_base, "sbx" + e.sid[:8])
    os.mkdir(t, 0o755)                                                          # squatted
    sb = sandbox.resolve(e.root, e.sid)
    assert sb["temp_ok"] is False and not os.path.exists(sandbox.env_path(e.root, e.sid))
    assert not sandbox.covers(os.path.join(t, "x"), sb)
    assert "NOT usable" in sandbox.show_text(sb, e.sid)
    sandbox.mark_wired(e.root, e.sid)
    assert not sandbox.is_wired(e.root, e.sid), "wired needs an env file that exists"


def test_resolve_makes_the_temp_root_and_the_env_file_points_at_it(project, tmp_path, temp_base):
    e = fx.make(project, tmp_path)
    sb = sandbox.resolve(e.root, e.sid)
    t = os.path.join(temp_base, "sbx" + e.sid[:8])
    assert sb["temp_root"] == t and sb["temp_ok"] is True and os.path.isdir(t)
    text = open(sandbox.env_path(e.root, e.sid)).read()
    assert "TMPDIR=%s" % t in text and "PYTEST_DEBUG_TEMPROOT=%s" % t in text
    assert sandbox.covers(os.path.join(t, "pytest-of-x", "a"), sb)


def test_cleanup_removes_a_temp_root_only_by_its_marker_not_by_the_8_character_name(project, tmp_path, temp_base):
    e = fx.make(project, tmp_path)
    t = os.path.join(temp_base, "sbx" + e.sid[:8])
    sandbox.make_temp_root(t, e.sid[:8] + "-another-session")                   # same first 8 characters
    sandbox.cleanup_session(e.root, e.sid)
    assert os.path.isdir(t), "another session's folder with the same prefix must stay"
    sandbox.remove_temp_root(t, e.sid[:8] + "-another-session")
    sandbox.make_temp_root(t, e.sid)
    sandbox.cleanup_session(e.root, e.sid)
    assert not os.path.exists(t)


def test_cleanup_keeps_a_folder_with_no_marker_or_a_link(project, tmp_path, temp_base):
    e = fx.make(project, tmp_path)
    t = os.path.join(temp_base, "sbx" + e.sid[:8])
    os.mkdir(t, 0o700)
    open(os.path.join(t, "f"), "w").close()
    sandbox.cleanup_session(e.root, e.sid)
    assert os.path.isdir(t)
    os.unlink(os.path.join(t, "f"))
    os.rmdir(t)
    target = tmp_path / "target"
    target.mkdir()
    (target / sandbox.MARKER).write_text(e.sid)
    os.symlink(str(target), t)
    sandbox.cleanup_session(e.root, e.sid)
    assert os.path.isdir(str(target)) and os.path.islink(t)


def test_the_sandbox_question_and_the_self_check_prompt_name_the_harness_places():
    from alpaca import batch, selfcheck
    fills = {i: f for i, f, _b in batch.class_items()}["sandbox"]
    for word in ("project root", "scratchpad", "/tmp/sbx", "private", "removed"):
        assert word in fills, word
    prompt = selfcheck.prompt_text("op-1", "design", "/f", ["brief.md"], "0" * 64, "n", False)
    assert "/tmp/sbx" in prompt and "NOT a breach" in prompt


def test_the_show_verb_prints_the_harness_places_and_the_json_carries_the_temp_root(project, tmp_path, capsys,
                                                                                   temp_base):
    import json
    from alpaca import cli
    e = fx.make(project, tmp_path)
    capsys.readouterr()
    assert cli.main(["sandbox", "show", "op-062", "--session-id", e.sid]) == cli.PASS
    out = capsys.readouterr().out
    assert "always (harness)" in out and os.path.join(temp_base, "sbxsess-aaa") in out
    assert cli.main(["sandbox", "show", "op-062", "--session-id", e.sid, "--json"]) == cli.PASS
    sb = json.loads(capsys.readouterr().out)["sandbox"]
    assert sb["temp_root"] == os.path.join(temp_base, "sbxsess-aaa") and e.root in sb["always"]
    assert not os.path.exists(sb["temp_root"]), "show only reads: it makes no folder"


def test_show_names_the_project_root_and_the_temp_root_as_harness_places(project, tmp_path):
    e = fx.make(project, tmp_path)
    text = sandbox.show_text(sandbox.for_session(e.conn, e.sid, e.root), e.sid)
    assert "always (harness)" in text and e.root in text
    assert "session temp root (harness" in text and "sbxsess-aaa" in text
    assert "no sudo" in text


# ---------------------------------------------------------------- 4. widen
def _owner_project(tmp_path, monkeypatch):
    p = Project(tmp_path, monkeypatch)
    studio = os.path.realpath(str(tmp_path)) + "/studio"
    fx.add_op(p.conn, "op-062", answer=fx.OP_062.format(studio=studio), sid=p.sid)
    fx.confirm(p.conn, "op-062", ["root", studio, "scratchpad"], sid=p.sid)
    fx.add_task(p.conn, "t-1", "op-062", sid=p.sid)
    p.ask("batch:op-062")
    return p, studio


def test_widen_needs_a_confirmed_list(tmp_path, monkeypatch):
    p, studio = _set_project(tmp_path, monkeypatch)                 # an owner answer, no confirmed list
    wide = str(tmp_path / "studio-2")
    msg = "yes, let the build also write to %s" % wide
    p.write([typed(msg)])
    with pytest.raises(sandbox.SandboxError) as err:
        sandbox.widen(p.conn, "op-062", [wide], by="owner", quote=msg, session=p.sid, root=p.root,
                      environ=p.env)
    assert err.value.reason == "SANDBOX-NONE" and "sandbox set" in str(err.value)


def test_widen_adds_a_path_the_owner_quoted_and_clears_the_cache(tmp_path, monkeypatch):
    p, studio = _owner_project(tmp_path, monkeypatch)
    wide = studio + "-2"
    msg = "yes, for the trial:\n" + sandbox.command_line("widen", "op-062", [wide])
    p.write([typed(msg)])
    sandbox._write(p.root, ("cache",), "any.json", "{}")
    ev = sandbox.widen(p.conn, "op-062", [wide], by="owner", quote=msg, session=p.sid, root=p.root,
                       environ=p.env)
    assert ev["kind"] == "sandbox-widen"
    assert not os.path.exists(sandbox.cache_path(p.root, "any"))
    sb = sandbox.for_session(p.conn, p.sid, p.root)
    assert wide in sb["roots"] and ev["id"] in sb["event_ids"]


def test_widen_refuses_a_path_the_quote_does_not_hold(tmp_path, monkeypatch):
    p, studio = _owner_project(tmp_path, monkeypatch)
    msg = "yes, widen it a little to %s-2" % studio
    p.write([typed(msg)])
    with pytest.raises(sandbox.SandboxError) as err:
        sandbox.widen(p.conn, "op-062", [studio + "-2"], by="owner", quote=msg,
                      session=p.sid, root=p.root, environ=p.env)
    assert err.value.reason == "SANDBOX-NOT-CONFIRMED"


def test_widen_refuses_a_quote_no_owner_message_holds(tmp_path, monkeypatch):
    p, studio = _owner_project(tmp_path, monkeypatch)
    wide = studio + "-2"
    p.write([typed("hello")])
    with pytest.raises(ownerword.OwnerWordRefusal):
        sandbox.widen(p.conn, "op-062", [wide], by="owner", quote=sandbox.command_line("widen", "op-062", [wide]),
                      session=p.sid, root=p.root, environ=p.env)


def test_widen_needs_a_sandbox(tmp_path, monkeypatch):
    p = Project(tmp_path, monkeypatch)
    fx.add_op(p.conn, "op-070", state="open", sid=p.sid)
    p.write([typed("allow /x/y")])
    with pytest.raises(sandbox.SandboxError) as err:
        sandbox.widen(p.conn, "op-070", ["/x/y"], by="owner", quote="allow /x/y", session=p.sid,
                      root=p.root, environ=p.env)
    assert err.value.reason == "SANDBOX-NONE"
