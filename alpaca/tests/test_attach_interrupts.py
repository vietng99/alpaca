"""A Ctrl-C during `alpaca attach init` leaves the studio clean or wholly attached (op-062 t-675, round 4).

The contract fail case is "undo() can still stop before the state cleanup". Rounds 1 to 3 closed one window
each; this file tests the class: a change and the ledger entry that tells the rollback about it are apart
across a check point (a call, a function entry, a backward jump), or the rollback's protection starts after
the catch. The run's answer is `repo._SigintHold`: a Ctrl-C is held by default and acts at once only in the
released regions named in the map above `_attach_locked`.

Every T test sends a REAL signal, `os.kill(os.getpid(), SIGINT)`, from a `sys.settrace` or `sys.setprofile`
hook. A tracer that raises KeyboardInterrupt itself stands in for nothing: it bypasses the handler the run
installs. Every call goes through `_run()`, so a KeyboardInterrupt never reaches pytest and stops the session.

  T1  S1  the catch and the first statement of undo()
  T2  S2  the return of the call that claims <repo>/
  T3  S3  between the mkdir/open of a terms folder or file and its ledger entry (safefs)
  T4  S4, S5  the temp files of project.yaml and of the exclude file
  T5  S7  after the COMMIT
  T6  U   between the two os.umask calls of util.write_text
  T7  the class: one signal at every line of the attach path, and a second at every line of the rollback
  G1..G5  guards: the hold's own lines, a host handler, the handler put back, a thread run, the rule in the code
"""
import ast
import dis
import glob
import inspect
import os
import signal
import sys
import tempfile
import types
from pathlib import Path

import pytest
import yaml

from alpaca import db, project, util
from alpaca.attach import repo, safefs
from alpaca.tests.attachkit import studio, upstream  # noqa: F401

ORIGINAL = signal.default_int_handler
REPO_FILE = os.path.realpath(repo.__file__)
SAFEFS_FILE = os.path.realpath(safefs.__file__)


@pytest.fixture(autouse=True)
def _sigint_sane():
    """SIGINT is the default handler and unblocked around every test, whatever a test does to it."""
    signal.signal(signal.SIGINT, ORIGINAL)
    signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGINT})
    yield
    signal.signal(signal.SIGINT, ORIGINAL)
    signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGINT})


def _run(studio, upstream, *, tracer=None, profiler=None, **kw):
    """repo.attach() that never lets a KeyboardInterrupt reach pytest. Returns the name of what came out."""
    if tracer is not None:
        sys.settrace(tracer)
    if profiler is not None:
        sys.setprofile(profiler)
    try:
        try:
            repo.attach(str(studio), upstream.url, **kw)
            return "returned"
        except BaseException as exc:                 # noqa: BLE001 - the test reads what came out
            return type(exc).__name__
    finally:
        sys.settrace(None)
        sys.setprofile(None)


def _strays(studio):
    return sorted(p[len(str(studio)) + 1:] for p in glob.glob(str(studio) + "/**/.tmp-*", recursive=True,
                                                               include_hidden=True))


def _state(studio):
    """(app/, attach state folder, attach: block, attach-init event, seal/): all False is clean, all True is attached."""
    app = (studio / "app").exists()
    state = (studio / ".alpaca" / "attach").exists()
    block = "attach" in (yaml.safe_load((studio / "project.yaml").read_text()) or {})
    event = "attach-init" in [e["kind"] for e in db.events(db.connect(str(studio)))]
    seal = (studio / "seal").exists()
    return app, state, block, event, seal


def _name_terms(studio):
    doc = project.load(str(studio))
    doc["barrier"] = dict(doc.get("barrier") or {}, terms="seal/deep/terms.txt")
    project.save(str(studio), doc)


def _clean_or_kept(studio, *, terms=True):
    state = _state(studio)
    if not terms:                                    # no barrier.terms is named: seal/ never exists
        state = state[:4] + (state[0],)
    return state in ((False,) * 5, (True,) * 5)


def _handler_back():
    return signal.getsignal(signal.SIGINT) is ORIGINAL and signal.SIGINT not in signal.pthread_sigmask(
        signal.SIG_BLOCK, set())


def _die_in_commit(monkeypatch):
    def boom(*a, **k):
        raise KeyboardInterrupt()
    monkeypatch.setattr(repo, "_commit", boom)


# ------------------------------------------------------------------------------------------ T1 to T6

def test_t1_a_real_sigint_on_the_first_event_of_undo_still_runs_every_step(studio, upstream, monkeypatch):
    """S1 (R3-F5): the Ctrl-C lands between the catch and the first statement of undo()."""
    _die_in_commit(monkeypatch)
    fired = []

    def tracer(frame, event, arg):
        if event == "call" and frame.f_code.co_name == "undo" and not fired:
            fired.append(1)
            os.kill(os.getpid(), signal.SIGINT)
        return None

    out = _run(studio, upstream, tracer=tracer)
    assert fired and out == "KeyboardInterrupt"
    assert _state(studio) == (False,) * 5 and "attach" not in project.load(str(studio))
    assert _handler_back()


def test_t2_a_real_sigint_when_the_claim_returns_leaves_no_empty_app_folder(studio, upstream):
    """S2 (R3-F8): the folder exists, `created` has not been set yet."""
    fired = []

    def tracer(frame, event, arg):
        if (event == "return" and frame.f_code.co_name == "make_dir" and not fired
                and frame.f_locals.get("exclusive") and (studio / "app").exists()):
            fired.append(1)
            os.kill(os.getpid(), signal.SIGINT)
        return tracer if frame.f_code.co_name == "make_dir" else None

    out = _run(studio, upstream, tracer=tracer)
    assert fired and out == "KeyboardInterrupt"
    assert _state(studio) == (False,) * 5
    assert _handler_back()


_APPENDS = [i for i, line in enumerate(Path(SAFEFS_FILE).read_text().splitlines(), 1) if "made.append(" in line]


@pytest.mark.parametrize("lineno", _APPENDS[:2])     # _need_folder and _create_only; the third is unused by attach
def test_t3_a_real_sigint_before_a_terms_folder_or_file_is_in_the_ledger_leaves_none_of_them(studio, upstream,
                                                                                            lineno):
    """S3: the folder or file exists, `made.append` has not run yet."""
    _name_terms(studio)
    fired = []

    def tracer(frame, event, arg):
        if frame.f_code.co_filename == SAFEFS_FILE:
            if event == "line" and frame.f_lineno == lineno and not fired:
                fired.append(1)
                os.kill(os.getpid(), signal.SIGINT)
            return tracer
        return tracer if event == "call" else None

    out = _run(studio, upstream, tracer=tracer)
    assert fired and out == "KeyboardInterrupt"
    assert _state(studio) == (False,) * 5, "seal/ or its terms file was left"
    assert not (studio / "seal").exists() and _strays(studio) == []
    assert _handler_back()


def test_t4_a_real_sigint_after_the_project_yaml_temp_file_exists_leaves_no_temp_file(studio, upstream):
    """S4: util.write_text (project.save inside the record transaction) has made its temp file."""
    fired = []

    def tracer(frame, event, arg):
        if (event == "call" and not fired and frame.f_code.co_name == "fdopen"
                and frame.f_back is not None and frame.f_back.f_code.co_name == "write_text"
                and str(frame.f_back.f_locals.get("path", "")).endswith("project.yaml")):
            fired.append(1)
            os.kill(os.getpid(), signal.SIGINT)
        return None

    out = _run(studio, upstream, tracer=tracer)
    assert fired and out == "KeyboardInterrupt"
    assert _strays(studio) == []
    assert _clean_or_kept(studio, terms=False)
    assert _handler_back()


_SRC, _START = inspect.getsourcelines(tempfile._mkstemp_inner)
_RETURN_FD = next(_START + i for i, line in enumerate(_SRC) if line.strip().startswith("return fd"))


def test_t4b_a_real_sigint_after_the_exclude_temp_file_exists_leaves_no_temp_file(studio, upstream):
    """S5: safefs._replace has made its temp file in .git/info."""
    fired = []

    def tracer(frame, event, arg):
        if frame.f_code.co_name != "_mkstemp_inner":
            return None
        if not str(frame.f_locals.get("dir", "")).endswith(os.path.join(".git", "info")):
            return None

        def local(frame, event, arg):
            if event == "line" and frame.f_lineno == _RETURN_FD and not fired:
                fired.append(1)
                os.kill(os.getpid(), signal.SIGINT)
            return local
        return local

    out = _run(studio, upstream, tracer=tracer)
    assert fired and out == "KeyboardInterrupt"
    assert _strays(studio) == []
    assert _clean_or_kept(studio, terms=False)
    assert _handler_back()


_SRC_COMMIT, _START_COMMIT = inspect.getsourcelines(repo._commit)
_CLOSE_LINE = next(_START_COMMIT + i for i, line in enumerate(_SRC_COMMIT) if "conn.close()" in line)


def test_t5_a_real_sigint_after_the_commit_keeps_the_attach_and_the_record_agrees(studio, upstream):
    """S7: the COMMIT is done; a rollback now would leave an attach-init event with no files."""
    fired = []

    def tracer(frame, event, arg):
        if frame.f_code.co_filename == REPO_FILE:
            if event == "line" and frame.f_lineno == _CLOSE_LINE and not fired:
                fired.append(1)
                os.kill(os.getpid(), signal.SIGINT)
            return tracer
        return tracer if event == "call" else None

    out = _run(studio, upstream, tracer=tracer)
    assert fired and out == "KeyboardInterrupt"      # deliberate: the Ctrl-C comes out after the attach is kept
    state = _state(studio)
    assert state[:4] == (True,) * 4                  # the event, the block, the clone and the state folder agree
    assert _handler_back()


def test_t6_a_real_sigint_between_the_two_umask_calls_leaves_the_umask_as_it_was(studio, upstream):
    """U: util.write_text sets the umask to 0 and back with two calls; a Ctrl-C between them left it at 0."""
    was = os.umask(0)
    os.umask(was)
    fired = []

    def profiler(frame, event, arg):
        if (event == "c_return" and not fired and getattr(arg, "__name__", "") == "umask"
                and frame.f_code.co_name == "write_text"
                and str(frame.f_locals.get("path", "")).endswith("project.yaml")):
            fired.append(1)
            os.kill(os.getpid(), signal.SIGINT)

    try:
        out = _run(studio, upstream, profiler=profiler)
        now = os.umask(0)
        os.umask(now)
    finally:
        os.umask(was)
    assert fired and out == "KeyboardInterrupt"
    assert now == was, "the process umask was left at %03o" % now
    assert _clean_or_kept(studio, terms=False) and _strays(studio) == []
    assert _handler_back()


# ------------------------------------------------------------------------------------------------- T7

def _codes(fn):
    out, todo = [], [fn.__code__]
    while todo:
        c = todo.pop()
        out.append(c)
        todo += [k for k in c.co_consts if isinstance(k, types.CodeType)]
    return out


_FUNCS = [repo._attach_locked, repo._commit, repo.configure, repo.ensure_ignored, repo.write_terms,
          repo._remove_clone, repo._undo_made, repo._remove_inside, repo._try_remove, repo._put_bytes,
          safefs.write_bytes, safefs.make_dir, safefs._need_folder, safefs._replace, util.write_text]
CASES = sorted({(c.co_qualname, line) for f in _FUNCS for c in _codes(f) for _, _, line in c.co_lines()
                if line and line != c.co_firstlineno})
CODES = {}
for _f in _FUNCS:
    for _c in _codes(_f):
        CODES.setdefault(_c.co_qualname, _c)
_IDS = ["%s:%d" % c for c in CASES]


def _line_signal(code, line, fired, *, only_when=None):
    def tracer(frame, event, arg):
        if frame.f_code is code and (only_when is None or only_when()):
            def local(frame, event, arg):
                if event == "line" and frame.f_lineno == line and not fired:
                    fired.append(1)
                    os.kill(os.getpid(), signal.SIGINT)
                return local
            return local
        return None
    return tracer


@pytest.mark.parametrize("qual, line", CASES, ids=_IDS)
def test_t7_one_real_sigint_at_any_line_leaves_the_studio_clean_or_wholly_attached(studio, upstream, qual, line):
    _name_terms(studio)
    fired = []
    out = _run(studio, upstream, tracer=_line_signal(CODES[qual], line, fired))
    if not fired:
        pytest.skip("line not run")
    assert out in ("KeyboardInterrupt", "returned"), out
    assert _clean_or_kept(studio), _state(studio)
    assert _strays(studio) == []
    assert _handler_back()


@pytest.mark.parametrize("qual, line", CASES, ids=_IDS)
def test_t7b_a_second_real_sigint_at_any_line_of_a_running_rollback_leaves_the_studio_clean(studio, upstream,
                                                                                         monkeypatch, qual, line):
    """The first Ctrl-C is a stand-in _commit that writes project.yaml and raises KeyboardInterrupt (the
    rollback then runs); the second is a real SIGINT at the first run of `line` after that."""
    _name_terms(studio)
    started = []

    def boom(*a, **k):
        d = project.load(str(studio))
        d["attach"] = {"repo": "app"}
        project.save(str(studio), d)
        started.append(1)
        raise KeyboardInterrupt()

    monkeypatch.setattr(repo, "_commit", boom)
    fired = []
    out = _run(studio, upstream, tracer=_line_signal(CODES[qual], line, fired, only_when=lambda: bool(started)))
    if not fired:
        pytest.skip("line not run in the rollback")
    assert out == "KeyboardInterrupt", out
    assert _state(studio) == (False,) * 5, _state(studio)
    assert _strays(studio) == []
    assert _handler_back()


# ------------------------------------------------------------------------------------------------- G1

_HOLD = getattr(repo, "_SigintHold", None)           # absent on a tree without the hold: g0 then fails, the rest collect
_HOLD_CASES = [(m, line) for m in ("__enter__", "__exit__", "release", "check", "_handler")
               for line in (sorted({ln for _, _, ln in getattr(_HOLD, m).__code__.co_lines() if ln}) if _HOLD else [])]


def test_g0_the_run_has_a_sigint_hold():
    assert _HOLD is not None


@pytest.mark.parametrize("method, line", _HOLD_CASES)
def test_g1_a_real_sigint_on_any_line_of_the_hold_leaves_no_handler_and_no_half_attach(studio, upstream, method,
                                                                                       line):
    """S9: the hold's own install, put-back, release and replay are not windows."""
    fired = []
    out = _run(studio, upstream, tracer=_line_signal(getattr(_HOLD, method).__code__, line, fired))
    assert out in ("KeyboardInterrupt", "returned")
    assert _handler_back()
    assert _clean_or_kept(studio, terms=False)


# ------------------------------------------------------------------------------------- G2 to G4

def test_g2_a_host_handler_gets_the_held_ctrl_c_and_is_put_back(studio, upstream, monkeypatch):
    """The replay goes through the caller's handler, so a host that does not raise keeps its own policy."""
    calls = []

    def host(signum, frame):
        calls.append(signum)

    real_configure = repo.configure

    def configure(*a, **k):
        os.kill(os.getpid(), signal.SIGINT)          # lands in a held region
        return real_configure(*a, **k)

    monkeypatch.setattr(repo, "configure", configure)
    signal.signal(signal.SIGINT, host)
    repo.attach(str(studio), upstream.url)
    assert signal.getsignal(signal.SIGINT) is host
    assert calls == [signal.SIGINT]
    assert _state(studio)[:4] == (True,) * 4         # the host did not raise: the attach is kept


def test_g3_the_default_handler_is_put_back_after_a_run_and_after_a_refusal(studio, upstream):
    repo.attach(str(studio), upstream.url)
    assert signal.getsignal(signal.SIGINT) is ORIGINAL
    with pytest.raises(Exception):
        repo.attach(str(studio), upstream.url)       # refused: already attached
    assert signal.getsignal(signal.SIGINT) is ORIGINAL
    assert signal.SIGINT not in signal.pthread_sigmask(signal.SIG_BLOCK, set())


def test_g4_a_run_on_a_thread_other_than_the_main_one_installs_nothing(studio, upstream, monkeypatch):
    import threading
    seen = []
    real = repo.configure

    def configure(*a, **k):
        seen.append(signal.getsignal(signal.SIGINT))
        return real(*a, **k)

    monkeypatch.setattr(repo, "configure", configure)
    t = threading.Thread(target=repo.attach, args=(str(studio), upstream.url))
    t.start()
    t.join(120)
    assert seen == [ORIGINAL]
    assert _state(studio)[:4] == (True,) * 4


# ------------------------------------------------------------------------------------------------- G5

def _repo_ast():
    return ast.parse(Path(REPO_FILE).read_text())


def _is_hold_on_true(stmt):
    return (isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Attribute)
            and stmt.targets[0].attr == "on" and isinstance(stmt.targets[0].value, ast.Name)
            and stmt.targets[0].value.id == "hold" and isinstance(stmt.value, ast.Constant)
            and stmt.value.value is True)


def _function(tree, name):
    return next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == name)


def test_g5_the_hold_is_turned_on_by_a_plain_store_as_the_first_statement_of_each_exit_path():
    tree = _repo_ast()
    locked = _function(tree, "_attach_locked")
    handlers = [h for t in ast.walk(locked) if isinstance(t, ast.Try) for h in t.handlers
                if isinstance(h.type, ast.Name) and h.type.id == "BaseException" and not _in_nested_def(locked, h)]
    assert len(handlers) == 1 and _is_hold_on_true(handlers[0].body[0]), \
        "the first statement of the `except BaseException` in _attach_locked must be `hold.on = True`"
    outer = _function(tree, "attach")
    finals = [t for t in ast.walk(outer) if isinstance(t, ast.Try) and t.finalbody]
    assert len(finals) == 1 and _is_hold_on_true(finals[0].finalbody[0]), \
        "the first statement of the `finally` in attach() must be `hold.on = True`"
    commit = _function(tree, "_commit")
    body = commit.body[1:] if isinstance(commit.body[0], ast.Expr) else commit.body    # past the docstring
    assert _is_hold_on_true(body[0]), "the first statement of _commit must be `hold.on = True`"


def _in_nested_def(outer, node):
    for inner in ast.walk(outer):
        if inner is not outer and isinstance(inner, (ast.FunctionDef, ast.Lambda)):
            if any(n is node for n in ast.walk(inner)):
                return True
    return False


_CHECK_POINTS = ("CALL", "RESUME", "JUMP_BACKWARD", "FOR_ITER", "SEND", "YIELD_VALUE")


def test_g5b_no_call_function_entry_or_backward_jump_lies_between_the_catch_and_the_store():
    """The probe behind the comment on _SigintHold (fix-r3-dis1.log): from CHECK_EXC_MATCH of `except
    BaseException` to the STORE_ATTR `on`, no instruction is one where CPython runs a Python signal handler."""
    insts = list(dis.get_instructions(repo._attach_locked))
    catch = [i for i, ins in enumerate(insts) if ins.opname == "CHECK_EXC_MATCH" and insts[i - 1].argval == "BaseException"]
    assert len(catch) == 1
    start = catch[0]
    store = next(i for i in range(start, len(insts)) if insts[i].opname == "STORE_ATTR" and insts[i].argval == "on")
    between = [ins.opname for ins in insts[start:store]]
    bad = [name for name in between if name.startswith(_CHECK_POINTS)]
    assert bad == [], "check points between the catch and `hold.on = True`: %s" % bad


def test_g5c_no_signal_mask_call_is_made_on_the_attach_path():
    """A mask call can raise after it has blocked SIGINT (investigate-r3-probe-mask.log), so none is used."""
    for path in sorted(Path(REPO_FILE).parent.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            name = node.attr if isinstance(node, ast.Attribute) else node.id if isinstance(node, ast.Name) else ""
            assert name != "pthread_sigmask", "%s uses pthread_sigmask" % path.name
