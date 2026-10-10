"""tools/k_replay.py, the replay of the submit judge on a real Claude Code subagent (fix batch 2,
item F12). The tool needs the live record and the probe agent's files, so the suite tests what does
not: how it decides its exit status, that it stops cleanly where the real data is absent, that it
holds no host path and no non-ASCII text, and that it asks the judge's own rule for the attachments.
A run on the real data is a proof check (`ALPACA_K_REPLAY=1` makes this file run it as well).
"""
from __future__ import annotations

import importlib.util
import os
import sys

import pytest

from alpaca.tests.conftest import REPO
from alpaca.tests.devfiles import needs_dev_files

TOOL = os.path.join(REPO, "tools", "k_replay.py")
pytestmark = needs_dev_files("tools/k_replay.py")


@pytest.fixture(scope="module")
def kr():
    spec = importlib.util.spec_from_file_location("k_replay_tool", TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _rows(profile="notes", claim="A", n=19, bad=()):
    return [(profile, claim, "K%d" % (i + 1), "check%d" % (i + 1),
             bad[0][1] if bad and bad[0][0] == i + 1 else "PASS", "") for i in range(n)]


def test_the_tool_is_ascii_and_holds_no_host_path():
    text = open(TOOL, encoding="utf-8").read()
    assert all(ord(ch) < 128 for ch in text)
    assert os.path.join(os.path.expanduser("~"), "") not in text


def test_every_check_passing_for_claims_a_and_b_in_every_profile_is_a_pass(kr):
    results = []
    for prof in ("notes", "implementer", "verifier"):
        results += _rows(prof, "A") + _rows(prof, "B")
    ok, lines = kr.summarize(results)
    assert ok is True
    assert lines[-1].startswith("K-REPLAY RESULT: PASS, all 19 K checks pass for claims A and B")
    assert "ae0f0dca2662b9b37" in lines[-1] and "implementer, notes, verifier" in lines[-1]


def test_one_refused_check_in_claim_a_or_b_fails_the_run_and_is_named(kr):
    for claim in ("A", "B"):
        results = _rows("notes", "A") + _rows("notes", "B")
        results = [r for r in results if r[1] != claim] + _rows("notes", claim, bad=[(8, "JOB-RELAY")])
        ok, lines = kr.summarize(results)
        assert ok is False
        assert lines[-1].startswith("K-REPLAY RESULT: FAIL") and "K8 check8 JOB-RELAY" in lines[-1]


def test_a_missing_claim_or_a_short_run_fails_and_claim_c_is_reported_not_required(kr):
    ok, lines = kr.summarize(_rows("notes", "A"))
    assert ok is False and "claim B was not run" in lines[-1]
    ok, lines = kr.summarize(_rows("notes", "A") + _rows("notes", "B", n=18))
    assert ok is False and "ran 18 checks, not 19" in lines[-1]
    ok, lines = kr.summarize([])
    assert ok is False and "no check ran" in lines[-1]
    both = _rows("notes", "A") + _rows("notes", "B") + _rows("notes", "C", bad=[(11, "JOB-FINAL-MISMATCH")])
    ok, lines = kr.summarize(both)
    assert ok is True and any("claim C" in l and "JOB-FINAL-MISMATCH" in l for l in lines)


def test_the_tool_stops_with_status_2_where_the_real_data_is_absent(kr, monkeypatch, capsys):
    monkeypatch.setattr(kr, "REAL_DATA", (os.path.join(REPO, "no-such-probe-file"),))
    monkeypatch.setattr(sys, "argv", ["k_replay.py"])
    assert kr.main() == 2
    assert "real data is not on this machine" in capsys.readouterr().out


def test_main_exits_by_the_summary_and_writes_the_result_line(kr, tmp_path, monkeypatch):
    # main itself, with the real data stood in for and no profile or corpus run: the exit status is
    # 0 for a passing summary and 1 for a failing one, and the --out file ends in the result line
    import io
    import alpaca.pool as pool
    from alpaca.research import jobs, session_engine as eng
    # main switches these off for a replay; the monkeypatch puts the originals back afterwards
    for mod, name in ((eng, "COMPILE_SITE_PACKAGES"), (eng, "PRELOAD_BLOCK"), (eng, "compile_engine"),
                      (jobs, "STRAGGLER_WAIT_S"), (jobs, "find_processes"), (pool, "pool_dir"),
                      (kr, "ORIG_POOL_DIR")):
        monkeypatch.setattr(mod, name, getattr(mod, name))
    stand_in = tmp_path / "stand-in.jsonl"
    stand_in.write_text("{}\n", encoding="utf-8")
    for name in ("TRANSCRIPT", "PROMPT"):
        monkeypatch.setattr(kr, name, str(stand_in))
    monkeypatch.setattr(kr, "make_base", lambda scratch: str(stand_in))
    passing = _rows("notes", "A") + _rows("notes", "B")
    failing = _rows("notes", "A") + _rows("notes", "B", bad=[(8, "JOB-RELAY")])
    for rows, want, line in ((passing, 0, "K-REPLAY RESULT: PASS"), (failing, 1, "K-REPLAY RESULT: FAIL")):
        monkeypatch.setattr(kr, "RESULTS", list(rows))
        monkeypatch.setattr(kr, "OUT", io.StringIO())
        out = tmp_path / ("k-replay-%d.txt" % want)
        monkeypatch.setattr(sys, "argv", ["k_replay.py", "--corpus-only", "--no-corpus",
                                          "--scratch", str(tmp_path / "scratch"), "--out", str(out)])
        assert kr.main() == want, line
        assert out.read_text(encoding="ascii").rstrip().splitlines()[-1].startswith(line)


def test_the_attachment_rule_of_the_replay_is_the_judges(kr):
    from alpaca.research import jobs
    src = open(TOOL, encoding="utf-8").read()
    assert "jobs.attachment_verdict" in src and "jobs.called_paths" in src
    assert callable(jobs.attachment_verdict) and callable(jobs.called_paths)


def _pool(path, before, spawn_form, after):
    lines = ['{"ts": "2026-10-02T23:59:%02d+07:00", "n": %d}' % (i % 60, i) for i in range(before)]
    spawn = ('{"ts":"%s", "n": "spawn"}' if spawn_form == "compact" else '{"ts": "%s", "n": "spawn"}')
    lines.append(spawn % "2026-10-03T00:25:41+07:00")
    lines += ['{"ts": "2026-10-03T01:00:00+07:00", "n": %d}' % (1000 + i) for i in range(after)]
    path.write_bytes(("\n".join(lines[:before + 1]) + "\n\n" + "\n".join(lines[before + 1:]) + "\n").encode())
    return lines[before:]


@pytest.mark.parametrize("chunk", [5, 37, 4096])
@pytest.mark.parametrize("form", ["spaced", "compact"])
def test_the_pool_tail_starts_at_the_spawn_line_wherever_a_chunk_cuts_it(kr, tmp_path, chunk, form,
                                                                       monkeypatch):
    """t-297 regression s2: the spawn line is found by a scan from the file start in chunks; a chunk
    boundary inside the spawn line or before it never loses it, and blank lines are dropped."""
    pool_file = tmp_path / "pool.jsonl"
    want = _pool(pool_file, 300, form, 40)
    monkeypatch.setattr(kr, "POOL_SCAN_CHUNK", chunk)
    got = kr.real_pool_tail(str(pool_file))
    assert [x.decode() for x in got] == want
    start = kr.spawn_offset(str(pool_file))
    assert pool_file.read_bytes()[start:].split(b"\n", 1)[0].decode() == want[0]


def test_the_pool_size_never_limits_where_the_scan_for_the_spawn_line_starts(kr, tmp_path, monkeypatch):
    """t-297 regression s2: a tail window (the last 80 MB) lost the spawn line once the driver's pool
    grew past it. The scan reads from offset 0, so a pool reported as huge still yields the line."""
    pool_file = tmp_path / "pool.jsonl"
    want = _pool(pool_file, 3, "spaced", 2)
    real_getsize = os.path.getsize
    monkeypatch.setattr(os.path, "getsize", lambda p: 10 ** 12 if str(p) == str(pool_file)
                        else real_getsize(p))
    assert [x.decode() for x in kr.real_pool_tail(str(pool_file))] == want


def test_a_pool_without_the_spawn_line_yields_no_tail(kr, tmp_path):
    pool_file = tmp_path / "pool.jsonl"
    pool_file.write_bytes(b'{"ts": "2026-10-03T00:25:40+07:00"}\n{"ts": "2026-10-03T00:25:42+07:00"}\n')
    assert kr.spawn_offset(str(pool_file)) is None
    assert kr.real_pool_tail(str(pool_file)) == []


@pytest.mark.skipif(os.environ.get("ALPACA_K_REPLAY") != "1", reason="set ALPACA_K_REPLAY=1 to replay "
                    "the real probe agent (reads the live record, takes minutes)")
def test_the_replay_of_the_real_probe_agent_passes(kr, tmp_path, monkeypatch):
    if [p for p in kr.REAL_DATA if not os.path.exists(p)]:
        pytest.skip("the real data is not on this machine")
    out = tmp_path / "k-replay.txt"
    monkeypatch.setattr(sys, "argv", ["k_replay.py", "--no-corpus", "--no-guard", "--out", str(out)])
    assert kr.main() == 0
    assert "K-REPLAY RESULT: PASS" in out.read_text()


# ================================================================ t-443: the replay's import path
# In-session-runtime.md section 30. The replay used to pin every file under the shared main tree
# (the venv's alpaca-root.pth appends it to sys.path), so another session's work there ended a
# replay with JOB-ENGINE-CHANGED. Synthetic trees only: `main`, `other`, `pkg`, `spkg`.

def _load_tool(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _write(path, text="x = 1\n"):
    os.makedirs(os.path.dirname(str(path)), exist_ok=True)
    with open(str(path), "w", encoding="utf-8") as fh:
        fh.write(text)
    return str(path)


def _bump(path):
    with open(str(path), "a", encoding="utf-8") as fh:
        fh.write("y = 2\n")


def _main_tree(base):
    """A stand-in for the shared main tree: its own modules, and another worktree's engine files."""
    main = os.path.join(str(base), "main")
    for rel in ("pkg/__init__.py", "pkg/mod.py",
                ".alpaca/worktrees/other/alpaca/__init__.py",
                ".alpaca/worktrees/other/alpaca/research/jobs.py",
                ".alpaca/worktrees/other/alpaca/research/__pycache__/jobs.cpython-314.pyc",
                "alpaca/tests/__pycache__/t_x.cpython-314.pyc"):
        _write(os.path.join(main, rel), "" if rel.endswith("__init__.py") else "x = 1\n")
    return os.path.realpath(main)


_OTHER = (".alpaca/worktrees/other/alpaca/research/jobs.py",
          ".alpaca/worktrees/other/alpaca/research/__pycache__/jobs.cpython-314.pyc")


@pytest.mark.parametrize("mode", ["replay-path", "walk-only"])
def test_a_parallel_edit_under_another_worktree_passes_k13(kr, project, tmp_path, monkeypatch, mode):
    """Done bar 0: a job judged by a replay-shaped process (the main tree on sys.path behind the
    engine) passes K13 while another worktree under the main tree is edited, with the replay's path
    and with the walk rule alone; a real change of a root the process imports from still ends the
    run (the fail case)."""
    from alpaca.tests import research_session_kit as rk
    from alpaca.tests.test_research_session_sources import _done
    from alpaca.research import jobs
    main = _main_tree(tmp_path)           # holds no alpaca/__init__.py, so the kit keeps it on the path
    site = os.path.realpath(_write(tmp_path / "lib" / "site-packages" / "spkg" / "mod.py"))
    site = os.path.dirname(os.path.dirname(site))
    tools = os.path.join(REPO, "tools")
    monkeypatch.setattr(sys, "path", [tools] + list(sys.path) + [site, main])
    if mode == "replay-path":
        monkeypatch.setattr(sys, "path", kr.replay_path(sys.path, REPO, main, tools, os.getcwd()))
    w = rk.JudgeWorld(monkeypatch, project, pinned=True)
    try:
        pin = jobs.pin_of(jobs.newest_pin(w.conn, w.SCOPE, w.SCOPE))
        roots = [r["root"] for r in pin["import_roots"]]
        assert site in roots
        if mode == "replay-path":
            assert main not in roots and os.path.realpath(tools) not in roots, roots
        else:
            assert main in roots
        for rel in _OTHER:                                   # parallel edit 1, before the hand
            _bump(os.path.join(main, rel))
        jid, n = _done(w)
        for rel in _OTHER:                                   # parallel edit 2, before the judge
            _bump(os.path.join(main, rel))
        _write(os.path.join(main, ".alpaca", "worktrees", "scratch-s1", "alpaca", "x.py"))
        _c, problem = w.judge(jid, n)
        assert problem is None, problem
        # the fail case: a module on a root the replay imports from changes
        jid, n = _done(w, try_n=2)
        changed = (os.path.join(site, "spkg", "mod.py") if mode == "replay-path"
                   else os.path.join(main, "pkg", "mod.py"))
        _bump(changed)
        _c, problem = w.judge(jid, n)
        assert problem is not None and problem.code == "JOB-ENGINE-CHANGED", problem
        assert problem.extra.get("end_run") is True
        assert changed in [c["path"] for c in problem.extra["changed"]]
    finally:
        w.close()


def test_the_replay_path_runs_the_tree_under_test_with_the_real_run_path(kr, tmp_path):
    main = str(tmp_path / "main")
    task = os.path.join(main, ".alpaca", "worktrees", "task")
    stdlib, dyn = str(tmp_path / "stdlib"), str(tmp_path / "stdlib" / "lib-dynload")
    site = os.path.join(main, ".venv", "lib", "python3.14", "site-packages")
    for d in (main, task, os.path.join(task, "tools"), os.path.join(main, "tools"), stdlib, dyn, site,
              str(tmp_path / "elsewhere")):
        os.makedirs(d, exist_ok=True)
    # (a) a task worktree: the shared tree, the tool's folder and the front copy of the tree go
    got = kr.replay_path([os.path.join(task, "tools"), task, stdlib, dyn, site, main], task, main,
                         os.path.join(task, "tools"), main)
    assert got == [stdlib, dyn, site, task]
    # (b) the main checkout: the tool's folder goes and the tree is last, once
    got = kr.replay_path([os.path.join(main, "tools"), stdlib, dyn, site, main], main, main,
                         os.path.join(main, "tools"), main)
    assert got == [stdlib, dyn, site, main]
    # (c) an empty entry is the working folder
    assert kr.replay_path(["", stdlib], task, main, os.path.join(task, "tools"), main) == [stdlib, task]
    assert kr.replay_path(["", stdlib], task, main, os.path.join(task, "tools"),
                          str(tmp_path / "elsewhere")) == ["", stdlib, task]
    # the scope: the path inside, and the very object it was afterwards, also on an error
    before = sys.path
    saved = list(before)
    with kr.replay_path_scope(task, main, os.path.join(task, "tools")) as inside:
        assert sys.path is inside and sys.path[-1] == task and sys.path is not before
    assert sys.path is before and sys.path == saved
    with pytest.raises(RuntimeError):
        with kr.replay_path_scope(task, main, os.path.join(task, "tools")):
            raise RuntimeError("boom")
    assert sys.path is before and sys.path == saved


class _Stop(Exception):
    """Raised by the recorder that stands in for register_driver: the replay goes no further."""


#: the three passes of the replay that take an engine pin (World.build, corpus_pass, k19_soak)
_PASSES = {"probe": ["--profile", "notes"], "corpus": ["--corpus-only"],
           "soak": ["--corpus-only", "--no-corpus", "--soak", "1"]}


def _arm(mod, tmp_path, monkeypatch, *, real_root, front, back):
    """Stub the real data of the loaded tool `mod`, put `front` + sys.path + `back` on the path and
    replace register_driver by a recorder of the path and the import roots at the pin."""
    import io
    import alpaca.pool as pool
    from alpaca import db
    from alpaca.research import jobs, paths, session_engine as eng, session_runtime as rt
    for owner, name in ((eng, "COMPILE_SITE_PACKAGES"), (eng, "PRELOAD_BLOCK"),
                        (eng, "compile_engine"), (jobs, "STRAGGLER_WAIT_S"),
                        (jobs, "find_processes"), (pool, "pool_dir"), (paths, "research_dir")):
        monkeypatch.setattr(owner, name, getattr(owner, name))
    stand_in = tmp_path / "stand-in.json"
    stand_in.write_text('{"toolUseId": "toolu-synthetic-0001"}\n', encoding="utf-8")
    for name in ("TRANSCRIPT", "PROMPT", "META", "LIVE_DB", "LIVE_POOL"):
        monkeypatch.setattr(mod, name, str(stand_in))
    monkeypatch.setattr(mod, "REAL_DATA", (str(stand_in),))
    conn = db.connect(str(tmp_path / "base-proj"))
    conn.close()
    base = os.path.join(str(tmp_path / "base-proj"), ".alpaca", "alpaca.db")
    monkeypatch.setattr(mod, "make_base", lambda scratch: base)
    monkeypatch.setattr(mod, "REAL_ROOT", real_root)
    monkeypatch.setenv("K_REPLAY_ROOT", real_root)
    monkeypatch.setattr(mod, "OUT", io.StringIO())
    monkeypatch.setattr(mod, "RESULTS", [])
    seen = {}

    def recorder(conn, root, *a, **kw):
        import alpaca
        seen["alpaca_tree"] = os.path.dirname(os.path.dirname(os.path.realpath(alpaca.__file__)))
        seen["install"] = os.path.realpath(eng.install_root())
        seen["roots"] = [r["root"] for r in eng.import_roots(eng.install_root(), names=set(),
                                                             digest=False)]
        raise _Stop()

    monkeypatch.setattr(rt, "register_driver", recorder)
    path = list(front) + list(sys.path) + list(back)
    monkeypatch.setattr(sys, "path", path)
    seen["set_path"] = path
    return seen


def _run(mod, monkeypatch, tmp_path, pass_name):
    monkeypatch.setattr(sys, "argv", ["k_replay.py", "--scratch", str(tmp_path / "scratch")]
                        + _PASSES[pass_name])
    try:
        rc = mod.main()
    except _Stop:
        return "stopped", None
    return "returned", rc


@pytest.mark.parametrize("pass_name", sorted(_PASSES))
def test_every_replay_pin_leaves_out_the_main_root_and_the_script_folder(tmp_path, monkeypatch,
                                                                         pass_name):
    """Every pin the replay takes (the probe world, the corpus pass, the K19 soak) is taken with the
    shared tree and the tool's folder off the import roots and the engine under test importable;
    main() hands the caller's path object back (t-443, section 30.3)."""
    main = _main_tree(tmp_path)
    mod = _load_tool(TOOL, "k_replay_t443_" + pass_name)
    seen = _arm(mod, tmp_path, monkeypatch, real_root=main, front=[os.path.dirname(TOOL)], back=[main])
    how, rc = _run(mod, monkeypatch, tmp_path, pass_name)
    assert how == "stopped", "the replay took no pin (%s %s)" % (how, rc)
    assert main not in seen["roots"], seen["roots"]
    assert os.path.realpath(os.path.dirname(TOOL)) not in seen["roots"], seen["roots"]
    assert seen["alpaca_tree"] == seen["install"] == os.path.realpath(REPO)
    assert sys.path is seen["set_path"]


def test_a_replay_whose_engine_is_another_tree_stops_before_any_pin(tmp_path, monkeypatch, capsys):
    """A copy of the tool in a worktree with no engine beside it would replay the engine this process
    imported; it stops with status 2 and names both trees (t-443, section 30.3)."""
    import shutil
    main = _main_tree(tmp_path)
    wt_tools = os.path.join(main, ".alpaca", "worktrees", "wt", "tools")
    os.makedirs(wt_tools)
    copy = os.path.join(wt_tools, "k_replay.py")
    shutil.copyfile(TOOL, copy)
    mod = _load_tool(copy, "k_replay_t443_other_tree")
    assert mod.REAL_ROOT == main and mod.TREE == os.path.dirname(wt_tools)
    seen = _arm(mod, tmp_path, monkeypatch, real_root=main, front=[wt_tools], back=[main])
    how, rc = _run(mod, monkeypatch, tmp_path, "probe")
    assert (how, rc) == ("returned", 2), seen
    out = capsys.readouterr().out
    assert os.path.dirname(wt_tools) in out and os.path.realpath(REPO) in out


def test_two_worktrees_get_two_default_scratch_folders(tmp_path, monkeypatch):
    """Two task worktrees that replay at the same time do not delete each other's record copy."""
    import shutil
    monkeypatch.delenv("K_REPLAY_ROOT", raising=False)
    main = _main_tree(tmp_path)
    mods = []
    for wt in ("op009-t-901", "op009-t-902"):
        tools = os.path.join(main, ".alpaca", "worktrees", wt, "tools")
        os.makedirs(tools)
        shutil.copyfile(TOOL, os.path.join(tools, "k_replay.py"))
        mods.append(_load_tool(os.path.join(tools, "k_replay.py"), "k_replay_t443_" + wt[-3:]))
    a, b = mods
    assert a.REAL_ROOT == b.REAL_ROOT == main
    assert a.SCRATCH_DEFAULT != b.SCRATCH_DEFAULT
    for m in mods:
        assert os.path.dirname(m.SCRATCH_DEFAULT) == os.path.join(main, ".alpaca", "worktrees")


@pytest.mark.skipif(os.environ.get("ALPACA_K_REPLAY") != "1", reason="set ALPACA_K_REPLAY=1 to replay "
                    "the real probe agent under a parallel writer (reads the live record, takes minutes)")
def test_the_gated_replay_ignores_a_parallel_worktree_edit(kr, tmp_path):
    """Done bar 0 in its literal form: the real replay, started as a bare `python tools/k_replay.py`
    from this tree, passes while another worktree folder of the project is written every 0.2 s."""
    import shutil
    import subprocess
    import threading
    import time
    if [p for p in kr.REAL_DATA if not os.path.exists(p)]:
        pytest.skip("the real data is not on this machine")
    folder = os.path.join(kr.REAL_ROOT, ".alpaca", "worktrees", "t443-parallel-%d" % os.getpid(),
                          "alpaca", "research")
    os.makedirs(os.path.join(folder, "__pycache__"), exist_ok=True)
    stop = threading.Event()

    def writer():
        n = 0
        while not stop.is_set():
            n += 1
            _write(os.path.join(folder, "w%d.py" % (n % 7)), "x = %d\n" % n)
            _write(os.path.join(folder, "__pycache__", "w%d.cpython-314.pyc" % (n % 7)), "%d" % n)
            stop.wait(0.2)
    th = threading.Thread(target=writer, daemon=True)
    th.start()
    out = tmp_path / "k.txt"
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "K_REPLAY_ROOT")}
    try:
        got = subprocess.run([sys.executable, os.path.join(REPO, "tools", "k_replay.py"), "--out",
                              str(out)], cwd=REPO, env=env, capture_output=True, text=True,
                             timeout=3600)
    finally:
        stop.set()
        th.join(5)
        shutil.rmtree(os.path.dirname(os.path.dirname(folder)), ignore_errors=True)
    assert got.returncode == 0, got.stdout[-2000:] + got.stderr[-2000:]
    text = out.read_text(encoding="ascii")
    assert text.rstrip().splitlines()[-1].startswith("K-REPLAY RESULT: PASS")
    assert "engine under test: %s" % os.path.join(os.path.realpath(REPO), "alpaca") in text
