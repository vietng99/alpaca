"""op-008 t-070 round 2, class C-E (`.alpaca/rune2-development/execution/t-070/fix-plan-r2.md` section 4):
no killed store serves withdrawn text, and no row names missing bytes.

Rules under test:

1. The doors honour every applied withdrawal or correction in the record, whatever state the store is
   in: a store that has not processed one answers `unavailable` (`store-behind-record`), never the
   source (`service._removal_acts_pending`, the watermark `DB.REMOVAL_ACTS_KEY`).
2. A document lands in this order: the landing decision under the lock, which refuses a withdrawn
   source; then its vault bytes; then the store commit. A kill leaves at worst a vault file with no
   row, never a row without its file and never new bytes of a withdrawn source.

The scenario holds a withdrawn event note, a withdrawn transcript, an admitted and then retracted
candidate, and an owner correction. The kill points (`alpaca/wiki/faults.py`, armed only from
inside the process by the test-only `kill_arm.py`) SIGKILL a rebuild (`wiki recover` on an empty
store), an import, a capture child (the capture runner's real child, after the session resumed and its
withdrawn transcript grew) and a live withdrawal door at a boundary. A dry run (an arm with a log)
counts the boundaries; the default run
kills each process at up to `DEFAULT_POINTS` boundaries spread evenly, `RUNE2_KILL_ALL=1` at every
one. On each killed store every surface of `servable.SURFACES` is asked; no withdrawn word may appear
on any of them, every answer is clean or `unavailable` with `store-behind-record`, every document
with an active block has its vault file with its `content_sha256`, and the next `wiki recover` gives
the digest of a fresh build. `RUNE2_KILL_LOG_DIR=<dir>` keeps one JSON line per kill.
"""
from __future__ import annotations

import ast
import contextlib
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from native_helpers import (T09, T10, T11, appender, at, capture, edges, events,  # noqa: E402
                            proposal, store)
from convergence_helpers import rebuilt, shadow, state, withdrawn_active  # noqa: E402
import kill_arm  # noqa: E402
import store_readers  # noqa: E402

from alpaca.wiki import service  # noqa: E402

REPO = Path(__file__).resolve().parents[3]
NOTE = "The osprey berth keeps the spare tide gauge behind the ochre shed."
TALK = "The cormorant ledger names the quayside vault combination as amber heron seventeen."
MORE = "Later the cormorant ledger adds the quayside gate code amber heron eighteen."
WORDS = ("ochre shed", "osprey berth", "amber heron", "cormorant ledger", "quayside")
QUESTIONS = ("Where does the osprey berth keep the spare tide gauge?",
             "What does the cormorant ledger name as the quayside vault combination?",
             "What gate code does the cormorant ledger add?")
DEFAULT_POINTS = 40
#: Kill points per process at which every reader runs (`_every`, round 3), spread evenly over the
#: points killed; the serving surfaces (`_ask`) are asked at every one. `RUNE2_KILL_ALL=1`: all.
READER_POINTS = 5
TALK_SID = "s-talk"


# ------------------------------------------------------------------ the scenario
def _talk(root, text, *, fresh=False):
    from alpaca import observability, transcripts
    path = Path(transcripts.local_path(str(root), TALK_SID))
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [{"type": "user", "timestamp": "2026-03-01T09:30:00Z",
              "message": {"content": "What does the ledger say?"}},
             {"type": "assistant", "timestamp": "2026-03-01T09:30:30Z",
              "message": {"content": [{"type": "text", "text": text}]}}]
    with open(path, "w" if fresh else "a", encoding="utf-8") as fh:
        fh.write("".join(json.dumps(r) + "\n" for r in lines))
    if fresh:
        observability.expect_source(str(root), TALK_SID, "claude", locator=str(path))
    return path


DECISION = "Keep the osprey berth tide gauge behind the ochre shed. Supersedes dec-0a1."


def _scenario(root, withdraw=("gone", "dec", "talk")) -> dict:
    """The live project: a withdrawn note, a withdrawn decision, a withdrawn transcript, a retracted
    admission and an owner correction, all through the live doors.

    Round 3 (C-E completeness) adds what the readers outside the round-2 surfaces read: an intent the
    withdrawn note is the result of (`alpaca sort` pairs them by key), a decision whose `choice` is
    the event's free text (the record rules' `decision/choice` object; it also supersedes an earlier
    decision, which `consolidate` turns into a candidate), and a plain note the runners withdraw."""
    at(root, "intent", {"body": "Check where the tide gauge is kept."}, T09, session="s-notes",
       ref="t-1")
    gone = at(root, "result", {"body": NOTE}, T09, session="s-notes", ref="t-1")
    dec = at(root, "decision", {"id": "dec-0e1", "choice": DECISION}, T09, session="s-notes",
             ref="dec-0e1")
    spare = at(root, "result", {"body": "The pier crane was painted on the north quay."}, T09,
               session="s-notes", ref="t-4")
    _talk(root, TALK, fresh=True)
    at(root, "result", {"body": "The talk session opened."}, T09, session=TALK_SID, ref="t-1")
    fact = at(root, "result", {"body": "Task t-1 is blocked on the review."}, T10, session="s-a",
              ref="t-1")
    at(root, "task-move", {"from": "doing", "to": "done", "proof": None, "reason": None}, T10,
       session="s-b", ref="t-2", actor="human")
    service.capture(root, sessions=[TALK_SID, "s-notes", "s-a", "s-b"], session_events=True,
                    deadline_s=None)
    r = service.propose(root, proposal(root, fact, "Task t-1 is blocked", subject="t-1"),
                        session="s-a", record=appender(root, "s-a"))
    assert r.status == "checked", [c for c in r.checks if not c.passed]
    a = service.admit(root, r.candidate_id, session="s-admin", record=appender(root, "s-admin"))
    assert a.status == "applied", a
    w = service.withdraw_candidate(root, r.candidate_id, session="s-a", record=appender(root, "s-a"),
                                   reason="wrong")
    assert w.status == "applied", w
    live = [e for e in edges(root) if e["status"] == "active" and e["subj_node"] == "t-2"
            and e["predicate"] == "status"]
    assert live, edges(root)
    c = service.correct(root, live[0]["edge_id"], "retract", session="s-owner",
                        record=appender(root, "s-owner"), reason="wrong move", by="owner")
    assert c.status == "applied", c
    refs = {"gone": "event:%d" % gone, "dec": "event:%d" % dec, "talk": "transcript:" + TALK_SID}
    for ref in (refs[name] for name in ("gone", "dec", "talk") if name in withdraw):
        out = service.withdraw_source(root, ref, session="s-owner", record=appender(root, "s-owner"),
                                      reason="wrong source")
        assert out.status == "applied", (ref, out)
    return {"gone": gone, "dec": dec, "spare": spare, "fact": fact, "cid": r.candidate_id,
            "edge": live[0]["edge_id"]}


def _resume(root):
    """After the scenario: the talk session resumes (its withdrawn transcript grows with more of
    the withdrawn words) and another session writes."""
    _talk(root, MORE)
    at(root, "result", {"body": "The talk session resumed."}, T11, session=TALK_SID, ref="t-1")
    at(root, "result", {"body": "Task t-3 is open on the pier."}, T11, session="s-c", ref="t-3")


# ------------------------------------------------------------------ the surfaces
def _gated(exc) -> bool:
    return isinstance(exc, service.StoreBehindRecord) and exc.detail["reason"] == "store-behind-record"


def _ask(root, scratch: Path) -> dict:
    """Each surface of `servable.SURFACES` on the store at `root`: {surface: "clean" | "unavailable"},
    asserting that no withdrawn word appears on any of them."""
    from alpaca.wiki import extract, portable, servable
    seen = {}

    def check(name, text, unavailable):
        low = text.lower()
        leaked = [w for w in WORDS if w in low]
        assert not leaked, "surface %s served withdrawn words %s" % (name, leaked)
        seen[name] = "unavailable" if unavailable else seen.get(name, "clean")

    for q in QUESTIONS:
        out = service.query(root, q)
        unavailable = out["outcome"] == "unavailable" and out["reason"] == "store-behind-record"
        check("query", json.dumps({k: v for k, v in out.items() if k != "question"}), unavailable)
        text = service.context(root, q, max_chars=16000)
        # the held answer's own line (a captured note may quote the reason, as a verdict does)
        check("context", text.replace(q, ""), "Outcome: unavailable (store-behind-record)" in text)
    for kind, ident in (("task", "t-1"), ("op", "op-8")):
        brief = service.briefing(root, **{kind: ident}, max_chars=16000)
        check("briefing", brief.text, not brief.items and
              "store-behind-record: the store has not processed" in brief.text)
    status = service.status(root)
    check("status", json.dumps(status, default=str),
          (status.get("serving") or {}).get("state") == "unavailable")
    try:
        check("candidates", json.dumps([v.to_dict() for v in service.candidates(root)]), False)
    except service.StoreBehindRecord as exc:
        assert _gated(exc)
        check("candidates", "", True)
    target = scratch / "export.jsonl"
    try:
        portable.export_store(root, target)
        check("export", target.read_text(encoding="utf-8"), False)
    except service.StoreBehindRecord as exc:
        assert _gated(exc)
        check("export", "", True)
    out_dir = scratch / "extract"
    try:
        extract.emit(Path(root, ".alpaca", "wiki"), out_dir)
        blob = b"".join(p.read_bytes() for p in sorted(out_dir.rglob("*")) if p.is_file())
        check("extract", blob.decode("utf-8", "replace"), False)
    except service.StoreBehindRecord as exc:
        assert _gated(exc)
        check("extract", "", True)
    surfaces = getattr(servable, "SURFACES", seen)       # none at 8f8976b5a (the red run)
    if "record_facts_preview" in surfaces:               # round 3 (C-E completeness)
        try:
            rows = service.record_facts_preview(root, from_event=1,
                                                through_event=store_readers.record_head(root))
            check("record_facts_preview", json.dumps([r.__dict__ for r in rows], default=str), False)
        except service.StoreBehindRecord as exc:
            assert _gated(exc)
            check("record_facts_preview", "", True)
    if "record-facts" in surfaces:                      # on a copy: a verb may append its verdict
        copy = _copy(root, scratch / "record-facts", lambda r: None)
        out = store_readers.cli(copy, ["wiki", "record-facts", "--dry-run", "--from", "1",
                                       "--through", str(store_readers.record_head(copy))])
        shutil.rmtree(Path(copy).parent, ignore_errors=True)
        check("record-facts", out, '"reason": "store-behind-record"' in out)
    if "sort" in surfaces:
        from alpaca import sort
        try:
            check("sort", json.dumps(sort.run(root)), False)
        except service.StoreBehindRecord as exc:
            assert _gated(exc)
            check("sort", "", True)
    assert set(seen) == set(surfaces), (sorted(seen), sorted(surfaces))
    return seen


#: The readers that hand out what the store holds (round 3): besides the words, their output may not
#: name a withdrawn document. `servable.SURFACES` must list exactly these.
SERVING = frozenset({"query", "context", "briefing", "status", "candidates", "export", "extract",
                     "record_facts_preview", "record-facts", "sort"})


def _every(root, scratch: Path, ctx: dict, allowed: dict, **kw) -> dict:
    """Every reader of `store_readers.RUNNERS` on the store at `root` (round 3, C-E completeness):
    no withdrawn word in any output, no withdrawn document named by a serving one, and after a
    reader that writes, its copy serves nothing withdrawn and holds no new withdrawn vault bytes."""
    ctx = dict(ctx, ask_restored=_ask)
    return store_readers.run_every_reader(
        root, scratch, ctx, words=WORDS, doc_ids=ctx["doc_ids"], surfaces=SERVING, ask=_ask,
        withdrawn_bytes=lambda r: _withdrawn_bytes(r, allowed), **kw)


def _rows_have_files(root) -> int:
    """Every document with an active block has its vault file, whose bytes hash to the document's
    `content_sha256` (rule 2). Read-only. Returns how many documents were checked."""
    vault = Path(root, ".alpaca", "wiki")
    db = vault / "rune.db"
    if not db.exists():
        return 0
    conn = sqlite3.connect("file:%s?mode=ro" % db, uri=True)
    try:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='docs'").fetchone():
            return 0
        rows = conn.execute("SELECT d.doc_id, d.content_sha256 FROM docs d WHERE EXISTS "
                            "(SELECT 1 FROM blocks b WHERE b.doc_id=d.doc_id AND b.status='active')"
                            ).fetchall()
    finally:
        conn.close()
    for doc_id, sha in rows:
        path = vault / doc_id
        assert path.is_file(), "a stored document has no vault file: %s" % doc_id
        got = hashlib.sha256(path.read_bytes().decode("utf-8").encode("utf-8")).hexdigest()
        assert got == sha, "a stored document's vault file has other bytes: %s" % doc_id
    return len(rows)


def _vault_files(root) -> dict:
    vault = Path(root, ".alpaca", "wiki", "raw")
    return {p.relative_to(vault).as_posix(): p.read_bytes() for p in vault.rglob("*.md")} \
        if vault.exists() else {}


def _withdrawn_bytes(root, allowed: dict) -> list:
    """Vault files holding a withdrawn word that the live store's vault does not hold byte for byte
    (rule 2: no new bytes of a withdrawn source; the withdrawn versions themselves stay, as an
    exclusion is not a purge)."""
    return sorted(name for name, data in _vault_files(root).items()
                  if any(w in data.decode("utf-8", "replace").lower() for w in WORDS)
                  and allowed.get(name) != data)


# ------------------------------------------------------------------ the kills
@contextlib.contextmanager
def _env(**values):
    old = {k: os.environ.get(k) for k in values}
    try:
        for k, v in values.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = str(v)
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _python(root, body: str, at=None, log=None, **env) -> subprocess.CompletedProcess:
    """`body` in a fresh interpreter importing this tree's alpaca, run from the project's src folder,
    armed from inside (`kill_arm.prelude`) when `at` or `log` is given."""
    armed = kill_arm.prelude(at, log) if at is not None or log is not None else ""
    code = "import sys; sys.path.insert(0, %r)\n%s%s" % (str(REPO), armed, body)
    with _env(**env):
        return subprocess.run([sys.executable, "-c", code], cwd=os.path.join(root, "src"),
                              capture_output=True, text=True, timeout=300, env=dict(os.environ))


def _rebuild(root, at=None, log=None):
    return _python(root, "from alpaca.wiki import recovery\n"
                         "recovery.run(%r, session='s-recover')" % root, at, log)


def _import(export):
    def run(root, at=None, log=None):
        return _python(root, "from alpaca.wiki import portable\n"
                             "portable.import_store(%r, %r)" % (root, str(export)), at, log)
    return run


def _capture_child(root, at=None, log=None):
    """The capture runner's real child, armed from inside by its target (`kill_arm.capture`)."""
    from alpaca import capture_runner
    return capture_runner.run_process(
        "alpaca.tests.wiki.kill_arm:capture",
        {"sessions": [TALK_SID, "s-c"], "session_events": True, "deadline_s": None,
         "kill_at": at, "kill_log": None if log is None else str(log)},
        root=root, kill_s=120)


def _door(gone):
    def run(root, at=None, log=None):
        return _python(root, "from alpaca.wiki import service\n"
                             "import sys; sys.path.insert(1, %r)\n"
                             "from native_helpers import appender\n"
                             "service.withdraw_source(%r, 'event:%d', session='s-owner', "
                             "record=appender(%r, 's-owner'), reason='later')"
                       % (str(Path(__file__).parent), root, gone, root), at, log)
    return run


def _points(count: int) -> list:
    if os.environ.get("RUNE2_KILL_ALL") == "1" or count <= DEFAULT_POINTS:
        return list(range(1, count + 1))
    step = (count - 1) / (DEFAULT_POINTS - 1)
    return sorted({1 + round(i * step) for i in range(DEFAULT_POINTS)})


def _boundaries(template: str, prepare, run, scratch: Path) -> list:
    """The boundaries one process passes, from a dry run on a copy of `template`."""
    root = _copy(template, scratch / "dry", prepare)
    log = scratch / "boundaries.log"
    log.unlink(missing_ok=True)
    run(root, log=log)
    lines = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    shutil.rmtree(Path(root).parent, ignore_errors=True)
    return [line.split(" ", 1)[1] for line in lines]


def _copy(template: str, where: Path, prepare) -> str:
    if where.exists():
        shutil.rmtree(where)
    root = where / "proj"
    shutil.copytree(template, root)
    prepare(str(root))
    return str(root)


def _kill_all(label: str, template: str, prepare, run, fresh: str, scratch: Path,
              allowed: dict, ctx: dict) -> dict:
    """Kill `run` at each chosen boundary on a fresh copy, then run every reader (`_every`), ask
    every surface and recover. `allowed` are the vault files of the live store the run rebuilds or
    extends."""
    names = _boundaries(template, prepare, run, scratch)
    assert names, "%s passed no kill boundary" % label
    log_dir = os.environ.get("RUNE2_KILL_LOG_DIR")
    out = {"boundaries": len(names), "killed": 0, "unavailable": 0, "points": []}
    chosen = _points(len(names))
    readers = set(chosen) if os.environ.get("RUNE2_KILL_ALL") == "1" or len(chosen) <= READER_POINTS \
        else {chosen[round(i * (len(chosen) - 1) / (READER_POINTS - 1))] for i in range(READER_POINTS)}
    for n in chosen:
        root = _copy(template, scratch / ("kill-%s-%d" % (label, n)), prepare)
        proc = run(root, at=n)
        killed = (getattr(proc, "returncode", None) == -9
                  or "status -9" in str(getattr(proc, "error", "") or ""))
        try:
            if n in readers:
                _every(root, Path(root).parent / "readers", ctx, allowed)
            seen = _ask(root, Path(root).parent)
            files = _rows_have_files(root)
            fresh_bytes = _withdrawn_bytes(root, allowed)
            assert not fresh_bytes, "new vault bytes of a withdrawn source: %s" % fresh_bytes
            from alpaca.wiki import recovery
            report = recovery.run(root, session="s-recover")
            assert report["status"] == "ok", report
            assert state(root)["digest"] == fresh, "recover after the kill differs from a fresh build"
            assert all(v == "clean" for v in _ask(root, Path(root).parent).values())
        except AssertionError as exc:
            raise AssertionError("%s killed at boundary %d (%s): %s"
                                 % (label, n, names[n - 1], exc)) from exc
        out["killed"] += bool(killed)
        out["unavailable"] += any(v == "unavailable" for v in seen.values())
        out["points"].append({"n": n, "at": names[n - 1], "killed": bool(killed), "readers": n in readers,
                              "unavailable": sorted(k for k, v in seen.items() if v == "unavailable"),
                              "documents_checked": files})
        shutil.rmtree(Path(root).parent, ignore_errors=True)
    if log_dir:
        with open(Path(log_dir, "kill-states.jsonl"), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(dict(out, label=label)) + "\n")
    return out


def _context(root, tmp_path, scenario: dict, settled=None) -> dict:
    """What the readers need: the scenario's ids, the withdrawn documents (read from `settled`, a
    store that has processed every withdrawal, by default `root`) and an export of `root` (a current
    store whose last event every later state's record holds, so an import of it pours and replays)."""
    from alpaca.wiki import portable
    export = tmp_path / "export.jsonl"
    portable.export_store(root, export)
    doc_ids = store_readers.withdrawn_doc_ids(settled or root)
    assert len(doc_ids) >= 3, doc_ids                 # the note, the decision, the transcript
    return dict(scenario, export=str(export), doc_ids=doc_ids)


def test_no_kill_point_serves_withdrawn_text_or_names_missing_bytes(project, tmp_path):
    root = str(project)
    ctx = _context(root, tmp_path, _scenario(root))
    assert all(v == "clean" for v in _ask(root, tmp_path).values())
    _rows_have_files(root)
    fresh = state(rebuilt(root, "fresh"))["digest"]
    assert state(root)["digest"] == fresh
    scratch = tmp_path / "kills"
    scratch.mkdir()
    allowed = _vault_files(root)
    # the control: every reader on the live store, which has processed every act
    _every(root, tmp_path / "live-readers", ctx, allowed)

    def empty(r):                                     # a rebuild: the record, no store
        shutil.rmtree(Path(r, ".alpaca", "wiki"), ignore_errors=True)

    # no store at all (a fresh clone, or the vault moved aside): the record holds the acts
    nostore = _copy(root, scratch / "no-store", empty)
    _every(nostore, scratch / "no-store" / "readers", ctx, allowed)
    assert all(v == "unavailable" for v in _ask(nostore, scratch / "no-store").values())
    from alpaca.wiki import recovery
    assert recovery.run(nostore, session="s-recover")["status"] == "ok"
    assert state(nostore)["digest"] == fresh

    rebuild = _kill_all("rebuild", root, empty, _rebuild, fresh, scratch, allowed, ctx)
    imported = _kill_all("import", root, empty, _import(Path(ctx["export"])), fresh, scratch, allowed,
                         ctx)

    # the capture child: the session resumed after its transcript was withdrawn
    resumed = str(tmp_path / "resumed" / "proj")
    shutil.copytree(root, resumed)
    _resume(resumed)
    after = state(rebuilt(resumed, "fresh-resumed"))["digest"]
    child = _kill_all("capture-child", resumed, lambda r: None, _capture_child, after, scratch,
                      _vault_files(resumed), ctx)

    # the live door's lost-commit window: the record holds the act, the store does not yet
    door_root = str(tmp_path / "door" / "proj")
    shutil.copytree(root, door_root)
    later = at(door_root, "result", {"body": "The ochre shed note is withdrawn too."}, T11,
               session="s-notes", ref="t-1")
    capture(door_root, {later})
    door = _kill_all_door(door_root, later, scratch, ctx, _vault_files(door_root))

    for label, out in (("rebuild", rebuild), ("import", imported), ("capture-child", child)):
        assert out["killed"] == len(out["points"]), (label, out)
    # the killed rebuilds and imports before their last act left stores behind the record, and the
    # doors said so
    assert rebuild["unavailable"] and imported["unavailable"], (rebuild, imported)
    assert door["unavailable"], door


def _kill_all_door(root: str, later: int, scratch: Path, ctx: dict, allowed: dict) -> dict:
    """The live withdrawal door killed between the record append and the store commit: every
    reader serves nothing withdrawn, every surface answers store-behind-record, and `wiki recover`
    applies the act."""
    names = _boundaries(root, lambda r: None, _door(later), scratch)
    assert "act-recorded" in names, names
    n = names.index("act-recorded") + 1
    killed_root = _copy(root, scratch / "kill-door", lambda r: None)
    proc = _door(later)(killed_root, at=n)
    assert proc.returncode == -9, proc.stderr
    _every(killed_root, Path(killed_root).parent / "readers", ctx, allowed)
    seen = _ask(killed_root, Path(killed_root).parent)
    assert all(v == "unavailable" for v in seen.values()), seen
    from alpaca.wiki import recovery
    fresh = state(rebuilt(killed_root, "fresh-door"))["digest"]        # the record with the act
    assert recovery.run(killed_root, session="s-recover")["status"] == "ok"
    assert state(killed_root)["digest"] == fresh
    assert all(v == "clean" for v in _ask(killed_root, Path(killed_root).parent).values())
    return {"boundaries": len(names), "unavailable": 1}


def test_the_kill_hook_counts_logs_and_does_nothing_unarmed(tmp_path, monkeypatch):
    """`faults.boundary`: with no arm it neither counts nor writes, whatever the environment holds;
    armed with a log (`kill_arm.arm`) it logs each boundary with its count (the dry run)."""
    from alpaca.wiki import faults
    monkeypatch.setattr(faults, "_ARM", None)
    log = tmp_path / "kills.log"
    monkeypatch.setenv("RUNE2_KILL_AT", "1")
    monkeypatch.setenv("RUNE2_KILL_LOG", str(log))
    faults.boundary("unarmed")
    assert not log.exists() and faults._ARM is None
    arm = kill_arm.arm(log=log)
    faults.boundary("first")
    faults.boundary("second")
    kill_arm.disarm()
    faults.boundary("after")
    assert arm.count == 2
    assert log.read_text(encoding="utf-8").splitlines() == ["1 first", "2 second"]


def _names_outside_tests(name: str) -> list:
    """`file:line` of every reference to the name `name` (a variable, an attribute, a string or an
    imported module) in a module of alpaca outside alpaca/tests."""
    package = REPO / "alpaca"
    hits = []
    for path in sorted(package.rglob("*.py")):
        rel = path.relative_to(REPO).as_posix()
        if rel.startswith("alpaca/tests/"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Name):
                names = [node.id]
            elif isinstance(node, ast.Attribute):
                names = [node.attr]
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                names = [node.value] if node.value == name or name in node.value.split(".") else []
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                names = [part for alias in node.names for part in alias.name.split(".")]
                names += (node.module or "").split(".") if isinstance(node, ast.ImportFrom) else []
            if name in names:
                hits.append("%s:%d" % (rel, node.lineno))
    return hits


def test_the_kill_hook_is_inert_unless_a_test_arms_it(project, tmp_path):
    """t-070 round 2 C-D (`notes-r2-cbe.md` open issue: a test-only kill hook in product code). The
    hook reads nothing a project, an operator or an agent can write: no environment variable, no
    record event, no project file, no hook payload. Only an arm a test puts in the process
    (`kill_arm`, under alpaca/tests) makes it act, and no module of alpaca outside alpaca/tests
    names the arm.

    The run: a kill order is planted in each of those places at once (the environment of every
    process, which the capture child inherits; a record event; `project.yaml`; the Stop hook's
    payload). A rebuild, the `wiki recover` verb and a Stop hook whose capture child lands notes all
    run to the end, log nothing and land every note. The same rebuild armed from inside passes the
    boundaries (the control) and dies at the first."""
    from alpaca.wiki import faults
    # the static part: faults.py imports nothing, reads no environment, and only tests arm it
    source = Path(faults.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = {alias.name for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))
                for alias in n.names}
    assert imported <= {"annotations"}, imported
    used = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | \
        {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert not used & {"os", "environ", "getenv", "signal", "kill", "open", "sys"}, used
    arms = [n for n in tree.body if isinstance(n, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "_ARM" for t in n.targets)]
    assert len(arms) == 1 and isinstance(arms[0].value, ast.Constant) and arms[0].value.value is None
    outside = [h for h in _names_outside_tests("_ARM") if not h.startswith("alpaca/wiki/faults.py:")]
    assert outside == [], outside
    assert _names_outside_tests("kill_arm") == []

    # the run: a kill order everywhere a project, an operator or an agent could put one
    root = str(project)
    log = tmp_path / "kills.log"
    order = {"RUNE2_KILL_AT": "1", "RUNE2_KILL_LOG": str(log)}
    for i, ts in enumerate((T09, T10)):
        at(root, "result", {"body": "The heron gate opens at dawn, step %d." % i}, ts,
           session="s-inert", ref="t-1")
    at(root, "result", dict(order, body="A kill order in the record.", env=order), T10,
       session="s-inert", ref="t-1")
    Path(root, "project.yaml").write_text(
        "name: kill-inert\nRUNE2_KILL_AT: 1\nenv:\n  RUNE2_KILL_AT: '1'\n  RUNE2_KILL_LOG: %s\n"
        % json.dumps(str(log)), encoding="utf-8")

    def empty():
        shutil.rmtree(Path(root, ".alpaca", "wiki"), ignore_errors=True)

    empty()
    with _env(**order):
        proc = _python(root, "from alpaca.wiki import recovery\n"
                             "recovery.run(%r, session='s-recover')" % root)
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert not log.exists()
    landed = _rows_have_files(root)
    assert landed >= 3
    empty()
    with _env(**order):
        proc = _python(root, "from alpaca import cli\nsys.exit(cli.main(['wiki', 'recover']))")
    assert proc.returncode == 0, (proc.stdout[-2000:], proc.stderr[-2000:])
    assert not log.exists() and _rows_have_files(root) >= landed      # + the first run's own event

    # the Stop hook: its capture child lands the session's new notes
    for i in range(2):
        at(root, "result", {"body": "The osprey ramp closes at dusk, step %d." % i}, T11,
           session="s-hook", ref="t-1")
    env = dict(os.environ, PYTHONPATH=str(REPO), **order)
    for name in ("ALPACA_ROOT", "CLAUDE_PROJECT_DIR", "ALPACA_SESSION_ID"):
        env.pop(name, None)
    payload = dict(order, session_id="s-hook", cwd=root, hook_event_name="Stop", env=order)
    proc = subprocess.run([sys.executable, "-m", "alpaca.hooks.stop"], input=json.dumps(payload),
                          capture_output=True, text=True, cwd=root, env=env, timeout=120)
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert not log.exists()
    failed = [e for e in events(root) if e["kind"] in ("drain-failed", "capture-deferred")]
    assert failed == [], failed
    assert _rows_have_files(root) >= landed + 2

    # the control: the same rebuild armed from inside passes the boundaries and dies at the first
    empty()
    proc = _rebuild(root, log=log)
    assert proc.returncode == 0, proc.stderr[-2000:]
    passed = log.read_text(encoding="utf-8").splitlines()
    assert len(passed) >= 3 and passed[0] == "1 drain-land", passed
    empty()
    assert _rebuild(root, at=1).returncode == -9


# ------------------------------------------------------------------ the static and registry pins
REMOVAL_KINDS = ("wiki-withdrawal", "wiki-candidate-withdrawn", "wiki-correction")


def _applied(node) -> bool:
    """Whether a call's arguments name `result` `applied` (a keyword or a dict entry)."""
    for sub in ast.walk(node):
        if isinstance(sub, ast.keyword) and sub.arg == "result" and isinstance(sub.value, ast.Constant) \
                and sub.value.value == "applied":
            return True
        if isinstance(sub, ast.Dict):
            for k, v in zip(sub.keys, sub.values):
                if isinstance(k, ast.Constant) and k.value == "result" and isinstance(v, ast.Constant) \
                        and v.value == "applied":
                    return True
    return False


def removal_appends(paths) -> list:
    """`file:line` of every call outside `Writer.record_removal_act` that appends a removal act: a
    call whose first argument (or `kind=`) is one of `REMOVAL_KINDS`, a `wiki-correction` only when
    it names `result` `applied`."""
    out = []
    for path in paths:
        tree = ast.parse(Path(path).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr == "record_removal_act":
                continue
            kinds = [a for a in node.args[:1] if isinstance(a, ast.Constant)]
            kinds += [k.value for k in node.keywords if k.arg == "kind" and isinstance(k.value, ast.Constant)]
            for kind in kinds:
                if kind.value in REMOVAL_KINDS and (kind.value != "wiki-correction" or _applied(node)):
                    out.append("%s:%d" % (Path(path).relative_to(REPO), node.lineno))
    return sorted(out)


def test_one_function_appends_a_removal_act():
    """Static: no module but `Writer.record_removal_act` appends a withdrawal, a retraction or an
    applied correction, so every such act moves the store's watermark in the same transaction."""
    paths = [p for p in sorted((REPO / "alpaca").rglob("*.py")) if "tests" not in p.parts]
    assert removal_appends(paths) == []


def test_the_static_check_sees_each_way_of_appending():
    """The check itself: a positional and a keyword kind, a dict and a keyword `result`."""
    src = ("record('wiki-withdrawal', {})\n"
           "db.append_event(conn, kind='wiki-candidate-withdrawn', data={})\n"
           "record('wiki-correction', dict(event, result='applied'))\n"
           "record('wiki-correction', {'result': 'applied'})\n"
           "record('wiki-correction', dict(event, refusal=code))\n"
           "writer.record_removal_act(record, 'wiki-withdrawal', {})\n")
    probe = REPO / "alpaca" / "tests" / "wiki" / "_removal_probe.txt"
    probe.write_text(src, encoding="utf-8")
    try:
        assert [x.rsplit(":", 1)[1] for x in removal_appends([probe])] == ["1", "2", "3", "4"]
    finally:
        probe.unlink()


def _cli_wiki_verbs() -> set:
    tree = ast.parse((REPO / "alpaca" / "cli.py").read_text(encoding="utf-8"))
    out = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "add_parser" and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "wkv" and node.args
                and isinstance(node.args[0], ast.Constant)):
            out.add(node.args[0].value)
    return out


#: `alpaca` verbs outside `wiki` that read the store (round 3, F-2): the historian pass, the backup
#: and the collector (its wiki consumer lands what capture lands).
OTHER_STORE_VERBS = frozenset({"sort", "backup", "collect"})


def test_every_serving_surface_is_registered(project, tmp_path):
    """Every Memory method, `wiki` verb and other store-reading `alpaca` verb is a registered serving
    surface or a named non-serving one, each registered name has a runner in `store_readers.RUNNERS`
    (so the kill test runs it, exempt or not), the serving ones are exactly `SERVING`, and every
    registered surface answers `unavailable` (`store-behind-record`) on a store the record is ahead
    of, and serves again after `wiki recover`."""
    from alpaca.wiki import api, servable
    methods = {n for n, v in vars(api.Memory).items() if callable(v) and not n.startswith("_")}
    verbs = _cli_wiki_verbs()
    assert methods and verbs and "extract" in verbs
    registry = set(servable.SURFACES) | set(servable.NOT_SERVING)
    unknown = sorted((methods | verbs | OTHER_STORE_VERBS) - registry)
    assert unknown == [], unknown
    assert not set(servable.SURFACES) & set(servable.NOT_SERVING)
    runners = store_readers.registered_names()
    assert sorted(registry - runners) == [], "registered names the kill test does not run"
    assert sorted(runners - registry) == [], "runners of names the registry does not hold"
    assert set(servable.SURFACES) == SERVING, sorted(set(servable.SURFACES) ^ SERVING)
    root = str(project)
    eid = at(root, "result", {"body": "The pelican winch was serviced on the pier."}, T10,
             session="s-notes", ref="t-1")
    capture(root, {eid})
    assert all(v == "clean" for v in _ask(root, tmp_path).values())
    # an applied withdrawal the store has not processed: appended as a crashed door left it
    appender(root, "s-owner")("wiki-withdrawal", {"source_ref": "event:%d" % eid, "reason": "x",
                                                  "session": "s-owner", "result": "applied",
                                                  "dependents": []})
    seen = _ask(root, tmp_path)
    assert all(v == "unavailable" for v in seen.values()), seen
    from alpaca.wiki import recovery
    assert recovery.run(root, session="s-recover")["status"] == "ok"
    assert all(v == "clean" for v in _ask(root, tmp_path).values())
    assert service.status(root)["serving"] == {"state": "available", "reason": None}


# ------------------------------------------------------------------ every reader, gated or proven
def _gate_keys(functions: dict) -> set:
    """The functions that consult the serving gate in their own body, and the gate's own functions."""
    gate = {("alpaca/wiki/service.py", q) for q in ("_removal_acts_pending", "_serving_gate",
                                                     "_record_removal_head", "_store_removal_mark")}
    return {k for k, v in functions.items() if v["gated"]} | gate


def _classify(functions: dict, readers: dict, seen: set, unreachable: set) -> dict:
    """How each reader is covered: `gated`, `run-behind` (it ran while the readers worked on a store
    behind the record), `called-from-gated`, `unreachable`, or `UNPROVEN`."""
    gated = _gate_keys(functions)
    out = {}
    for key in sorted(readers):
        if key in gated:
            out[key] = "gated"
        elif key in seen:
            out[key] = "run-behind"
        elif store_readers.module_name(key[0]) in unreachable:
            out[key] = "unreachable"
        elif store_readers.upstream_gated(key, functions, readers, gated):
            out[key] = "called-from-gated"
        else:
            out[key] = "UNPROVEN"
    return out


def test_every_store_reader_is_gated_or_proven(project, tmp_path):
    """Round 3, C-E completeness (check round 2 R2-B1 and F-2): every function outside alpaca/tests
    that reads store rows or the record's fact text (`store_readers.store_readers`: it opens the
    store, connects to its file by hand, or renders record events as notes or record facts) is either
    gated (its own body consults `service._removal_acts_pending` or `_serving_gate`) or proven: it
    ran while every runner of `store_readers.RUNNERS` worked on a store behind the record (a lost
    commit and no store at all) and every output was checked; a function called only from gated
    code, and a module no product entry reaches (import graph), pass too. A new reader that is none
    of these fails here by name. Every runner that writes is also followed by `wiki recover`, after
    which no withdrawn source is active and every surface serves again."""
    from alpaca.wiki import recovery
    root = str(project)
    scenario = _scenario(root, withdraw=("dec", "talk"))
    allowed = _vault_files(root)
    # a lost commit: the live door withdrawing the note killed after its record append, so the
    # record holds the act and the store still serves the note (and pairs it, for `alpaca sort`)
    names = _boundaries(root, lambda r: None, _door(scenario["gone"]), tmp_path / "dry")
    lost = _copy(root, tmp_path / "lost", lambda r: None)
    assert _door(scenario["gone"])(lost, at=names.index("act-recorded") + 1).returncode == -9
    settled = _copy(lost, tmp_path / "settled", lambda r: None)
    assert recovery.run(settled, session="s-recover")["status"] == "ok"
    ctx = _context(root, tmp_path, scenario, settled=settled)
    # no store at all: the same record, the vault moved aside
    nostore = _copy(lost, tmp_path / "nostore", lambda r: shutil.rmtree(
        Path(r, ".alpaca", "wiki"), ignore_errors=True))
    paths = store_readers.product_paths()
    functions = store_readers.scan(paths)
    readers = {k: v for k, v in functions.items() if v["reasons"]}
    assert len(readers) >= 20, sorted(readers)
    targets = {(os.path.realpath(v["path"]), v["line"]): k for k, v in readers.items()}

    def settles(name, copy):
        report = recovery.run(copy, session="s-recover")
        assert report["status"] == "ok", (name, report)
        assert withdrawn_active(copy) == [], (name, withdrawn_active(copy))
        asked = _ask(copy, Path(copy).parent)
        assert all(v == "clean" for v in asked.values()), (name, asked)

    with store_readers.traced(targets) as trace:
        for label, behind in (("lost", lost), ("nostore", nostore)):
            assert all(v == "unavailable" for v in _ask(behind, tmp_path / label).values())
            _every(behind, tmp_path / label / "readers", ctx, allowed, after_write=settles,
                   trace=trace)
    unreachable = store_readers.unreachable_modules(paths)
    how = _classify(functions, readers, set(trace.seen), unreachable)
    if os.environ.get("RUNE2_READERS_LOG"):
        with open(os.environ["RUNE2_READERS_LOG"], "w", encoding="utf-8") as fh:
            for (rel, qual), state_ in how.items():
                fh.write("%s:%d %s %s (%s)\n" % (rel, readers[(rel, qual)]["def"], qual, state_,
                                                 "; ".join(readers[(rel, qual)]["reasons"])))
    missing = ["%s:%d %s (%s)" % (k[0], readers[k]["def"], k[1], "; ".join(readers[k]["reasons"]))
               for k, v in how.items() if v == "UNPROVEN"]
    assert missing == [], "store readers neither gated nor proven:\n" + "\n".join(missing)


def test_the_reader_enumeration_sees_each_way_of_reading(tmp_path):
    """The enumeration itself: each way of reading the store or the record's fact text is seen, a
    gate in the body is seen, a caller that is gated covers the function it calls, and an ungated
    caller does not."""
    pkg = tmp_path / "alpaca"
    pkg.mkdir()
    (pkg / "probe.py").write_text(
        "from .store.db import DB as Store, open_db_readonly\n"
        "def opens(cfg):\n    return open_db_readonly(cfg)\n"
        "def pours(cfg):\n    return Store(cfg)\n"
        "def names(root):\n    return sqlite3.connect(root + '/.alpaca/wiki/rune.db')\n"
        "def spells(root):\n    return root + '/.alpaca/wiki/rune.db'\n"
        "def path(cfg):\n    return sqlite3.connect(cfg.db_path)\n"
        "def stat(cfg):\n    return os.stat(cfg.db_path).st_size if cfg.db_path.exists() else 0\n"
        "def facts(text):\n    return record_extract.extract(text)\n"
        "def notes(ev):\n    return drain._note_doc(ev)\n"
        "def gated(root, cfg):\n    service._serving_gate(root)\n    open_db_readonly(cfg)\n"
        "    return helper(cfg)\n"
        "def helper(cfg):\n    return open_db_readonly(cfg)\n"
        "def loose(cfg):\n    return other(cfg)\n"
        "def other(cfg):\n    return open_db_readonly(cfg)\n"
        "def plain(x):\n    return paths.db_path(x)\n", encoding="utf-8")
    functions = store_readers.scan([pkg / "probe.py"], base=tmp_path)
    readers = {k[1]: v for k, v in functions.items() if v["reasons"]}
    assert sorted(readers) == ["facts", "gated", "helper", "names", "notes", "opens", "other",
                               "path", "pours"], sorted(readers)          # not `spells`, `stat`
    assert [k for k, v in readers.items() if v["gated"]] == ["gated"]
    keyed = {k: v for k, v in functions.items() if v["reasons"]}
    gated = {k for k, v in keyed.items() if v["gated"]}
    key = {k[1]: k for k in functions}
    assert store_readers.upstream_gated(key["helper"], functions, keyed, gated)
    assert not store_readers.upstream_gated(key["other"], functions, keyed, gated)
    assert not store_readers.upstream_gated(key["opens"], functions, keyed, gated)
