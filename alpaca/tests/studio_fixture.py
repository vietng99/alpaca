"""The Studio fixture project: a throwaway Alpaca project with a seeded, fictional record (t-474, t-475).

    python alpaca/tests/studio_fixture.py <empty-dir> [--at 2026-03-18T14:20:00Z]

builds, in an empty directory, a project whose record fills every Studio page: four ops (one closed,
one pinned with phases and an open question batch, one with a task plan of groups and prerequisites,
one research run), tasks in every status and phase, sealed proofs, messages, an open owner order,
an unsigned brief, sessions with transcripts and autodrive levels, a mission map with checks, library
documents, and a git history. Every name, path and line of text is invented here; nothing is copied
from another record or transcript.

It runs in its own process: it pins the wall clock (alpaca/tests/studio/fixed_clock.py) so every
stamp the record writes, every transcript time and every file time sits at a chosen instant before
`--at`. Two builds are equal in what the Studio renders. Beside the project it writes
`.studio-fixture/`:

    auth            the fixture's own web login, user:code (fictional; the server reads it as files-auth)
    workspaces.json the host registry the fixture server reads (ALPACA_WORKSPACES): this project and
                    one fictional neighbour on a closed port
    stubs/          system.json (host load), vcs.json (git view) and vitals.json (the connection
                    label and the health bar), the host-derived reads the capture answers from files
                    (the vcs stub is gitview.snapshot of the fixture's own git history, with the
                    fixture path replaced)
    home/, claude/  HOME and CLAUDE_CONFIG_DIR for the fixture server and any CLI run on the fixture
    env.json        the environment a fixture server needs (paths above), for studio_shots.py

`studio_shots.py` builds this, starts `alpaca serve` on it and captures it.
"""
import argparse
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
AT = "2026-03-18T14:20:00Z"
NAME = "Northwind Atlas"
FOLDER = "northwind-atlas"
AUTH = "pilot:atlas-fixture-4471"
FAKE_ROOT = "/work/northwind-atlas"
SESSIONS = {
    # sid: (operator, title, level, goal)
    "5b1e0c2a-7d41-4c8e-9a31-2f6d8e0b4c11": ("claude", "Tile cache and offline packs", "L5", "finish the tile cache work in op-002"),
    "9e4a7f30-1c2b-4d5e-8f60-7a8b9c0d1e22": ("claude", "Survey importer cleanup", "L3", "close the importer follow-ups"),
    "c3d2e1f0-a9b8-4c7d-8e6f-5a4b3c2d1e33": ("claude", "Sync conflict research", "L4", "measure the merge cost of offline edits"),
}


def env_for(root):
    """The environment a fixture server or a CLI run on the fixture uses: nothing of the host's own
    registry, tunnel config, Claude home or live session reaches it."""
    side = os.path.join(root, ".studio-fixture")
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("ALPACA_", "CLAUDE_", "CODEX_")) and k not in ("PYTHONPATH",)}
    env.update({"HOME": os.path.join(side, "home"), "CLAUDE_CONFIG_DIR": os.path.join(side, "claude"),
                "ALPACA_WORKSPACES": os.path.join(side, "workspaces.json"),
                "ALPACA_CLOUDFLARED_CONFIG": os.path.join(side, "no-cloudflared.yml"),
                "ALPACA_SERVE_RELOAD": "0", "ALPACA_NO_AUTOSERVE": "1", "ALPACA_DISK_SWEEP": "0",
                "ALPACA_RECORD_SKIP_ROOT": REPO, "TZ": "UTC", "LANG": "C.UTF-8",
                "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"})
    return env


def _iso(epoch):
    import datetime
    return datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc).isoformat().replace("+00:00", "Z")


def build(root, at=AT):
    sys.path.insert(0, os.path.join(HERE, "studio"))
    import fixed_clock
    E = fixed_clock.parse(at)
    clock = fixed_clock.install(E - 7 * 86400)
    sys.path.insert(0, REPO)
    root = os.path.abspath(root)
    if os.path.exists(root) and os.listdir(root):
        raise SystemExit("studio_fixture: %s is not empty" % root)
    os.makedirs(root, exist_ok=True)
    env = env_for(root)
    for key in [k for k in os.environ if k.startswith(("ALPACA_", "CLAUDE_", "CODEX_"))]:
        del os.environ[key]
    os.environ.update(env)
    import time
    time.tzset()                          # TZ=UTC: every stamp the record writes reads +00:00
    side = os.path.join(root, ".studio-fixture")
    for d in (side, env["HOME"], env["CLAUDE_CONFIG_DIR"], os.path.join(side, "stubs")):
        os.makedirs(d, exist_ok=True)

    def at_(epoch):
        clock.set(epoch)
        return _iso(epoch)

    H, D = 3600, 86400
    T0 = E - 6 * D                       # 2026-03-12 14:20 UTC
    write = _writer(root)
    # A fixed copy of a real manifest: the fixture's commit hashes, which the Version control page
    # shows, must not move when this tree's own ALPACA-MANIFEST gains a line (op-008 t-072b).
    with open(os.path.join(REPO, "alpaca", "tests", "studio", "fixture-manifest"), encoding="utf-8") as fh:
        write("ALPACA-MANIFEST", fh.read())
    write("project.yaml", "name: %s\nproject_id: 4e7a1c9b2d30\n"
          "what: 'Field app that maps street trees: offline tiles, a survey importer and sync'\n"
          "people:\n- name: Tovin Harrow\n  role: owner\ntier: private\nexisting_repo: false\n"
          "phases:\n  default:\n  - requirement\n  - design\n  - build\n  - verify\n  - release\n"
          "default_level: L3\n" % NAME)
    write("style/banned.txt", "")
    write(".gitignore", ".alpaca/\n.studio-fixture/\nproofwork/\n")
    write(".alpaca/files-auth", AUTH + "\n")
    write(".studio-fixture/auth", AUTH + "\n")
    os.chmod(os.path.join(root, ".alpaca", "files-auth"), 0o600)
    os.makedirs(os.path.join(root, ".alpaca", "config"), exist_ok=True)
    sources = {
        "src/tiles/cache.txt": "Tile cache: keeps the last 400 map tiles per zoom level on the device.\n",
        "src/tiles/packs.txt": "Offline packs: a district's tiles bundled for a day of field work.\n",
        "src/survey/importer.txt": "Survey importer: reads the city's tree census CSV into species and plots.\n",
        "src/survey/species.txt": "Species table: common and botanical names, canopy class.\n",
        "src/sync/merge.txt": "Sync merge: offline edits from two surveyors meet on the same plot.\n",
        "src/app/map.txt": "Map screen: plots, canopy layer and the surveyor's route.\n",
    }
    for rel, text in sources.items():
        write(rel, text)
    write("docs/field-guide.md", "# Field guide\n\nHow a surveyor plans a day: download the district pack the night before, "
          "walk the route on the map screen, and sync at the depot.\n\n## Before you leave\n\nCheck the pack date.\n")
    write("docs/tile-budget.md", "# Tile budget\n\nThe device keeps at most 400 tiles per zoom level. A district pack is "
          "about 38 MB at zoom 17.\n")
    write("runbooks/district-pack.yaml", "# District pack build\nsteps:\n  - fetch tiles for the district bounds\n"
          "  - bundle and sign the pack\n")
    write("README.md", "# %s\n\nA field app for street-tree surveys.\n" % NAME)

    cwd = os.getcwd()
    os.chdir(root)
    try:
        _record(root, at_, E, T0, H, D)
    finally:
        os.chdir(cwd)
    _git(root, E, T0, H, D)
    _stubs(root, E, fixed_clock, clock)
    _registry(root)
    # every file the Library and the mission map read keeps a fixed modification time
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in (".git", ".studio-fixture")]
        for fn in filenames:
            path = os.path.join(dirpath, fn)
            if not os.path.islink(path):
                os.utime(path, (E - 2 * H, E - 2 * H))
    with open(os.path.join(side, "env.json"), "w", encoding="utf-8") as fh:
        json.dump({"at": at, "env": {k: env[k] for k in ("HOME", "CLAUDE_CONFIG_DIR", "ALPACA_WORKSPACES",
                                                         "ALPACA_CLOUDFLARED_CONFIG", "ALPACA_SERVE_RELOAD",
                                                         "ALPACA_NO_AUTOSERVE", "ALPACA_DISK_SWEEP", "TZ",
                                                         "GIT_CONFIG_GLOBAL", "GIT_CONFIG_NOSYSTEM")}},
                  fh, indent=1)
    return root


def _writer(root):
    def write(rel, text):
        path = os.path.join(root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return path
    return write


def _record(root, at_, E, T0, H, D):
    from alpaca import batch, cli, db, messages, mission, ops, ownerword, taskplan, transcripts
    from alpaca.posture import level
    from alpaca.research import events as rev
    from alpaca.tests import proofkit

    at_(T0)
    assert cli.main(["init"]) == 0
    conn = db.connect(root)
    S1, S2, S3 = list(SESSIONS)

    def ev(kind, t, *, session=S1, actor="agent", ref=None, op=None, data=None):
        at_(t)
        return db.append_event(conn, session=session, actor=actor, kind=kind, ref=ref, op=op, data=data or {})

    # ---- sessions, operators, levels -------------------------------------------------------------
    starts = {S1: E - 9 * H, S2: E - 30 * H, S3: E - 20 * H}
    for sid, (operator, title, lvl, goal) in SESSIONS.items():
        at_(starts[sid])
        db.upsert(conn, "sessions", "sid", {"sid": sid, "started": _iso(starts[sid]),
                                            "transcript": os.path.relpath(transcripts.local_path(root, sid), root)})
        db.meta_set(conn, "operator:" + sid, operator)
        ev("session-start", starts[sid], session=sid, ref=sid, data={"operator": operator, "title": title})
        level.set(conn, sid, lvl, goal, root=root)

    # ---- op-001: closed, the survey importer -----------------------------------------------------
    at_(T0 + H)
    op1 = ops.open_op(conn, "Import the city tree census", "the census CSV loads into species and plots",
                      session=S2, actor="agent", title="Survey importer")
    done1 = []
    for i, (title, statement) in enumerate([
            ("Parse the census CSV", "Read the city's tree census CSV with its 14 columns"),
            ("Map species names", "Map common names to botanical names and canopy class"),
            ("Load plots", "Create one plot per census row with its coordinates")]):
        at_(T0 + H + 600 * (i + 1))
        tid = ops.add_task(conn, op1, statement, title, session=S2, actor="agent")
        done1.append(tid)
    for i, tid in enumerate(done1):
        t = T0 + (i + 1) * 5 * H
        ev("claim", t, session=S2, ref=tid, op=op1, data={"worker": "importer-worker"})
        db.patch(conn, "tasks", "id", tid, {"status": "doing", "claimant": "importer-worker"})
        _seal_done(conn, root, tid, op1, S2, t + 2 * H, at_, ev, proofkit, db)
    ev("op-close", T0 + 26 * H, session=S2, ref=op1, op=op1, data={"basis": "all three tasks done with sealed proofs"})
    db.patch(conn, "ops", "id", op1, {"status": "closed", "closed": _iso(T0 + 26 * H)})

    # ---- op-002: pinned, phases, the question batch: offline tiles -------------------------------
    at_(E - 2 * D - 4 * H)
    op2 = ops.open_op(conn, "Offline map tiles for the field app", "a surveyor works a full day with no signal",
                      phases=["requirement", "design", "build", "verify", "release"], session=S1, actor="agent",
                      title="Offline tiles")
    ev("op-pin", E - 2 * D - 4 * H + 30, session=S1, ref=op2, op=op2, data={"pinned": True, "on_open": True})
    ev("phase-enter", E - 2 * D - 3 * H, session=S1, op=op2, ref=op2, data={"phase": "requirement"})
    ev("phase-advance", E - 2 * D, session=S1, op=op2, ref=op2, data={"boundary": "requirement->design"})
    ev("phase-advance", E - D - 6 * H, session=S1, op=op2, ref=op2, data={"boundary": "design->build"})
    at_(E - 2 * D - 3 * H)
    batch.open_batch(conn, op2, session=S1, questions=["Which zoom levels does a district pack carry?"])
    plan2 = [
        ("requirement", "Write the pack size budget", "State the tile budget per zoom level"),
        ("design", "Design the tile cache", "Choose the eviction rule and the on-device layout"),
        ("design", "Design the pack format", "One signed bundle per district"),
        ("build", "Build the tile cache", "Keep the last 400 tiles per zoom level"),
        ("build", "Build the pack downloader", "Resume a pack download after a lost signal"),
        ("build", "Show pack status on the map", "A badge with the pack date and size"),
        ("verify", "Field test a full day offline", "Walk district 4 with no signal"),
        ("release", "Ship offline tiles", "Release notes and the store build"),
    ]
    tasks2 = []
    for i, (phase, title, statement) in enumerate(plan2):
        at_(E - 2 * D - 2 * H + 300 * i)
        tasks2.append(ops.add_task(conn, op2, statement, title, phase=phase, session=S1, actor="agent"))
    r1, d1, d2, b1, b2, b3, v1, rel1 = tasks2
    at_(E - 2 * D - H)
    taskplan.record(conn, op2, {"version": 1, "revision": 0, "title": "Offline tiles",
        "groups": [{"id": "budget", "title": "Budget and design", "category": "Design"},
                   {"id": "cache", "title": "Tile cache", "category": "Features"},
                   {"id": "packs", "title": "District packs", "category": "Features"},
                   {"id": "ship", "title": "Field test and release", "category": "Release"}],
        "tasks": [_plan(r1, "budget", []), _plan(d1, "budget", [r1]), _plan(d2, "budget", [r1]),
                  _plan(b1, "cache", [d1]), _plan(b2, "packs", [d2]), _plan(b3, "packs", [b2]),
                  _plan(v1, "ship", [b1, b3]),
                  _plan(rel1, "ship", [v1], hold={"kind": "approval", "reason": "the owner signs the store build"})]},
        session=S1, actor="agent")
    for tid, t in ((r1, E - 2 * D - 2 * H), (d1, E - D - 20 * H), (d2, E - D - 12 * H)):
        ev("claim", t, session=S1, ref=tid, op=op2, data={"worker": "tiles-worker"})
        db.patch(conn, "tasks", "id", tid, {"status": "doing", "claimant": "tiles-worker"})
        _seal_done(conn, root, tid, op2, S1, t + 3 * H, at_, ev, proofkit, db)
    # in flight: the cache (with a proof being written) and the downloader
    for tid, worker, sid, t in ((b1, "tiles-worker", S1, E - 3 * H), (b2, "pack-worker", S3, E - 90 * 60)):
        ev("claim", t, session=sid, ref=tid, op=op2, data={"worker": worker})
        db.patch(conn, "tasks", "id", tid, {"status": "doing", "claimant": worker, "lease_until": "2999-01-01T00:00:00Z"})
    ev("proof-scaffold", E - 40 * 60, session=S1, ref=b1, op=op2, data={"path": ".alpaca/proofs/%s/%s.md" % (op2, b1)})
    ev("task-move", E - 5 * H, session=S1, ref=b3, op=op2, data={"from": "open", "to": "blocked", "reason": "waits on the pack format sign-off"})
    db.patch(conn, "tasks", "id", b3, {"status": "blocked"})

    # ---- op-003: sync, a plan with groups and prerequisites, no phases ---------------------------
    at_(E - D - 8 * H)
    op3 = ops.open_op(conn, "Merge offline edits from two surveyors", "two edits to one plot merge with no loss",
                      session=S3, actor="agent", title="Sync merge")
    plan3 = [("Record edit timestamps", "Stamp each plot edit with the device clock and a counter"),
             ("Detect conflicting edits", "Two edits to one plot field since the last sync"),
             ("Merge rule for species edits", "Prefer the edit with a photo"),
             ("Conflict review screen", "Show both edits side by side"),
             ("Sync load test", "Two hundred devices sync at the depot")]
    tasks3 = []
    for i, (title, statement) in enumerate(plan3):
        at_(E - D - 7 * H + 300 * i)
        tasks3.append(ops.add_task(conn, op3, statement, title, session=S3, actor="agent"))
    s1, s2, s3, s4, s5 = tasks3
    at_(E - D - 6 * H)
    taskplan.record(conn, op3, {"version": 1, "revision": 0, "title": "Sync merge",
        "groups": [{"id": "detect", "title": "Detect", "category": "Features"},
                   {"id": "merge", "title": "Merge", "category": "Features"},
                   {"id": "load", "title": "Load", "category": "Verification"}],
        "tasks": [_plan(s1, "detect", []), _plan(s2, "detect", [s1]), _plan(s3, "merge", [s2]),
                  _plan(s4, "merge", [s2]), _plan(s5, "load", [s3, s4])]}, session=S3, actor="agent")
    ev("claim", E - 20 * H, session=S3, ref=s1, op=op3, data={"worker": "sync-worker"})
    db.patch(conn, "tasks", "id", s1, {"status": "doing", "claimant": "sync-worker"})
    _seal_done(conn, root, s1, op3, S3, E - 16 * H, at_, ev, proofkit, db)
    # a lapsed lease: claimed yesterday, never renewed
    ev("claim", E - 14 * H, session=S2, ref=s2, op=op3, data={"worker": "night-worker"})
    db.patch(conn, "tasks", "id", s2, {"status": "doing", "claimant": "night-worker", "lease_until": _iso(E - 13 * H)})

    # ---- op-004: a research run, still running -------------------------------------------------
    at_(E - 20 * H)
    op4 = ops.open_op(conn, "research run rr-001", "merge cost of offline edits under 12 ms",
                      session=S3, actor="research-runner", title="research run rr-001")
    _research(conn, op4, S3, E, H, at_, ops, rev, db)

    # ---- messages, an order, an unsigned brief --------------------------------------------------
    notes = [
        (S1, "tiles-worker", "result", "Tile cache keeps 400 tiles per zoom level; eviction is least recently drawn.", E - 4 * H),
        (S3, "broadcast", "question", "Should a conflict on the species field block the sync, or queue for review?", E - 3 * H),
        (S2, "op-003", "handoff", "Importer follow-ups are done; the night worker's lease on the conflict task lapsed.", E - 13 * H),
        (S1, "broadcast", "decision", "District packs are signed per district, not per tile.", E - D - 10 * H),
        (S3, "pack-worker", "blocker", "The pack status badge waits on the pack format sign-off.", E - 5 * H),
        (S1, "broadcast", "note", "Field test planned for district 4 next Tuesday.", E - 50 * 60),
    ]
    for sid, to, kind, body, t in notes:
        at_(t)
        messages.post(conn, "agent:" + SESSIONS[sid][0], to, kind, body, session=sid, root=root)
    ev("owner-order", E - 6 * H, session=S1, actor="owner", ref="ord-1", op=op2,
       data={"id": "ord-1", "op": op2, "quote": "keep the district packs under 40 MB each", "door": "any",
             "by": "owner", "owner_word": {}})
    at_(E - 2 * H)
    ownerword.record_ask(conn, S1, "brief:%s:a1b2c3d4e5f6" % op2, "Sign the pack format brief", op=op2)

    # ---- mission map ---------------------------------------------------------------------------
    conn.close()
    at_(E - D - 3 * H)
    mission.define(root, _mission(), session="owner")
    for node, tid in (("cache", b1), ("packs", b2), ("importer", done1[0]), ("merge", s3)):
        mission.link(root, node, tid, "work", session="owner")
    at_(E - 6 * H)
    for node in ("importer", "species", "cache"):
        mission.run_check(root, node, "behavior", [sys.executable, "-c", "pass"], session="owner")

    # ---- transcripts for the Sessions page and the burn board ----------------------------------
    _transcripts(root, E, H, transcripts)
    conn = db.connect(root)
    for sid in SESSIONS:
        for k in range(4):
            ev("heartbeat", E - (k + 1) * 47 * 60, session=sid, ref=sid)
    at_(E - 30 * 60)
    conn.close()


def _plan(tid, group, after, hold=None):
    return {"id": tid, "group": group, "after": list(after), "resources": [], "requires_stages": [], "hold": hold}


def _seal_done(conn, root, tid, op, sid, t, at_, ev, proofkit, db):
    at_(t)
    filler = ("I changed the module for %s, wired it into the map screen and ran the field checks over it. "
              "The numbers below are the ones that run printed." % tid)
    ptr = proofkit.seal_for(root, tid, conn=conn, filler=filler, session=sid)
    ev("task-move", t + 60, session=sid, ref=tid, op=op, data={"from": "doing", "to": "done", "proof": ptr})
    db.patch(conn, "tasks", "id", tid, {"status": "done", "proof": ptr, "lease_until": None})


def _research(conn, op, sid, E, H, at_, ops, rev, db):
    run = "rr-001"
    metric = [{"id": "value", "unit": "ms", "direction": "lower", "kind": "deterministic", "repeats": 1, "min_ok": 1, "margin": 0}]
    at_(E - 20 * H)
    rev.emit(conn, rev.RUN_START, run=run, ref=run, op=op, session=sid, data={
        "mode": "improve", "runbook": "runbooks/merge-cost.yaml", "runbook_sha256": "a" * 64, "contract_sha256": "b" * 64,
        "intent": "merge cost", "override_diff": [], "k": 2, "n": 3, "metrics": metric,
        "targets": [{"metric": "value", "value": 12}], "accept": {"rule": "champion"},
        "budget": {"usd": 8.0, "tokens": 200000, "wall": None}, "control": None, "via": "cli", "level": "L4",
        "session": sid, "research_branch": "research/" + run, "base_commit": "c0" * 20, "repair_tasks": []})
    rev.emit(conn, rev.RUNNER, run=run, ref=run, op=op, session=sid, data={"pid": 4242, "host": "atlas-runner"})

    def task(rnd, slot, t):
        at_(t)
        key = "%s:r%d:s%d" % (run, rnd, slot)
        return ops.add_task(conn, op, "attempt", "%s r%d s%d" % (run, rnd, slot), session=sid, actor="research-runner",
                            extra={"research": {"run": run, "role": "attempt", "attempt": key, "key": key,
                                                "baseline": rnd == 0, "round": rnd, "slot": slot, "mode": "improve",
                                                "category": None, "repair_task": None}})

    def record(t, tid, outcome, value=None, forked="c0" * 20, head=None, reason=None):
        at_(t)
        metrics = {"value": {"median": value, "repeats": [value], "n_ok": 1, "spread": 0, "untested": False}} if value is not None else {}
        rev.emit(conn, rev.ATTEMPT, run=run, ref=tid, op=op, session=sid, data={
            "outcome": outcome, "reason": reason, "broken_class": None if value is not None or outcome != "broken" else "build",
            "metrics": metrics, "forked_from": forked, "head": head or forked, "diff": {"sha256": "d" * 64},
            "tokens": 18400, "cost_usd": 0.62, "priced": True, "alpaca_version": "fixture"})

    def round_(t, rnd, winner, commit, value):
        at_(t)
        rev.emit(conn, rev.ROUND, run=run, ref=run, op=op, session=sid, data={
            "round": rnd, "repair_task": None, "winner": winner, "ranking": [], "reason": "ranked",
            "champion_commit": commit, "champion_metrics": {} if value is None else {"value": {"median": value}},
            "accepted": 1 if winner else 0, "targets_met": False})

    def claim(t, tid):
        at_(t)
        db.append_event(conn, session=sid, actor=run, kind="claim", op=op, ref=tid, data={"worker": run})
        db.patch(conn, "tasks", "id", tid, {"status": "doing", "claimant": run})

    a0 = task(0, 0, E - 19 * H)
    record(E - 18 * H, a0, "baseline", 21.4)
    round_(E - 18 * H + 60, 0, a0, "c0" * 20, 21.4)
    a1, a2 = task(1, 0, E - 17 * H), task(1, 1, E - 17 * H + 30)
    record(E - 14 * H, a1, "improvement", 16.8, head="c1" * 20)
    record(E - 14 * H + 120, a2, "broken", reason="gate build failed")
    at_(E - 14 * H + 180)
    rev.emit(conn, rev.MERGE, run=run, ref=run, op=op, session=sid, data={
        "round": 1, "repair_task": None, "attempt": a1, "commit": "c1" * 20, "parent": "c0" * 20,
        "research_branch": "research/" + run})
    round_(E - 14 * H + 240, 1, a1, "c1" * 20, 16.8)
    a3, a4 = task(2, 0, E - 6 * H), task(2, 1, E - 6 * H + 30)
    record(E - 3 * H, a3, "improvement", 14.1, forked="c1" * 20, head="c2" * 20)
    claim(E - 2 * H, a4)
    at_(E - 2 * H + 60)
    rev.emit(conn, rev.GATE, run=run, ref=a4, op=op, session=sid, data={
        "stage": "build", "side": "attempt", "verdict": "PASS", "rc": 0, "offline": True, "input_sha256": "e" * 64})


def _mission():
    groups = [{"id": "field", "title": "Field app"}, {"id": "data", "title": "Survey data"}]
    nodes = [("cache", "field", "Tile cache", "src/tiles/cache.txt"),
             ("packs", "field", "District packs", "src/tiles/packs.txt"),
             ("map", "field", "Map screen", "src/app/map.txt"),
             ("importer", "data", "Survey importer", "src/survey/importer.txt"),
             ("species", "data", "Species table", "src/survey/species.txt"),
             ("merge", "data", "Sync merge", "src/sync/merge.txt")]
    edges = [("importer", "species"), ("species", "map"), ("cache", "map"), ("packs", "cache"), ("merge", "importer")]
    return {"version": 1, "title": "Street-tree field app", "purpose": "Trace the field app's parts to their checks",
            "inventory": ["src"], "groups": groups,
            "nodes": [{"id": i, "group": g, "title": t, "purpose": "Owns the " + t.lower(), "sources": [s],
                       "checks": ["behavior"], "tags": ["field"], "lifecycle": "current"} for i, g, t, s in nodes],
            "edges": [{"source": a, "target": b, "kind": "feeds", "confidence": "confirmed",
                       "reason": b + " reads " + a, "evidence": [dict((n[0], n[3]) for n in nodes)[b]]} for a, b in edges]}


def _transcripts(root, E, H, transcripts):
    """One Claude transcript per session: prompts, replies with usage (priced: the cache writes name
    their 5-minute share) and tool calls with results, ending shortly before the shown instant so
    the burn board's last 24 hours and the in-flight cards have activity."""
    work = {
        # sid: (model, first, last, every, files, prompts)
        list(SESSIONS)[0]: ("claude-opus-5", E - 9 * H, E - 8 * 60, 31 * 60, ["src/tiles/cache.txt", "docs/tile-budget.md"],
                            ["Start the tile cache task and keep the tile budget in mind.",
                             "Check the eviction rule against the budget doc.",
                             "Run the tile tests again after the eviction change.",
                             "Write the proof for the cache work."]),
        list(SESSIONS)[1]: ("claude-sonnet-5", E - 30 * H, E - 14 * H, 47 * 60, ["src/survey/importer.txt", "src/survey/species.txt"],
                            ["Clean up the importer follow-ups from the census load.",
                             "Map the remaining species names to botanical names.",
                             "Hand the conflict task over; my lease ends soon."]),
        list(SESSIONS)[2]: ("claude-opus-5", E - 20 * H, E - 4 * 60, 41 * 60, ["src/sync/merge.txt", "src/tiles/packs.txt"],
                            ["Measure the merge cost of offline edits for the research run.",
                             "Start the pack downloader and resume after a lost signal.",
                             "Test the resume path with a cut connection."]),
    }
    replies = ["Reading the sources first.", "The rule holds for every zoom level in the budget.",
               "Tests pass; 14 checks, none skipped.", "Done; the evidence list names each run."]
    for n, (sid, (model, first, last, every, files, prompts)) in enumerate(work.items()):
        rows, k, t = [], 0, first
        while t <= last:
            ident = "%s-r%03d" % (sid[:8], k)
            rows.append({"type": "user", "timestamp": _iso(t), "message": {"role": "user", "content": prompts[k % len(prompts)]}})
            tool = ("Read", {"file_path": files[k % len(files)]}) if k % 3 == 0 else \
                   ("Edit", {"file_path": files[k % len(files)], "old_string": "400", "new_string": "400"}) if k % 3 == 1 else \
                   ("Bash", {"command": "python3 -m pytest tests -q"})
            cw = 500 + 40 * k
            rows.append({"type": "assistant", "uuid": ident, "timestamp": _iso(t + 35),
                         "message": {"id": ident, "model": model, "role": "assistant",
                                     "content": [{"type": "text", "text": replies[k % len(replies)]},
                                                 {"type": "tool_use", "id": ident + "-t", "name": tool[0], "input": tool[1]}],
                                     "usage": {"input_tokens": 900 + 120 * k, "output_tokens": 180 + 30 * k,
                                               "cache_read_input_tokens": 6000 + 700 * k, "cache_creation_input_tokens": cw,
                                               "cache_creation": {"ephemeral_5m_input_tokens": cw, "ephemeral_1h_input_tokens": 0}}}})
            rows.append({"type": "user", "timestamp": _iso(t + 50),
                         "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": ident + "-t",
                                                                  "is_error": False, "content": "ok: 14 passed" if tool[0] == "Bash" else "done"}]}})
            k, t = k + 1, t + every
        path = transcripts.local_path(root, sid)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("".join(json.dumps(r) + "\n" for r in rows))


def _git(root, E, T0, H, D):
    """A small, fixed git history: the fixture's own sources, a merged feature branch and an open one."""
    last = [T0]

    def git(*args, t=None):
        # every command, checkouts included, is stamped: the reflog the git view reads holds no host time
        last[0] = t = last[0] + 60 if t is None else t
        env = dict(os.environ, GIT_AUTHOR_NAME="Tovin Harrow", GIT_AUTHOR_EMAIL="tovin@northwind.example",
                   GIT_COMMITTER_NAME="Tovin Harrow", GIT_COMMITTER_EMAIL="tovin@northwind.example",
                   GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
        env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = "%d +0000" % t
        subprocess.run(["git", "-C", root, *args], check=True, capture_output=True, env=env)
    git("init", "-q", "-b", "main")
    git("add", ".gitignore", "README.md", "src/survey", t=T0)
    git("commit", "-q", "-m", "Survey importer reads the census CSV (op-001 t-001)", t=T0 + 2 * H)
    git("add", "docs", "runbooks", "project.yaml", "style", "ALPACA-MANIFEST")
    git("commit", "-q", "-m", "Field guide and the district pack runbook", t=T0 + 30 * H)
    git("checkout", "-q", "-b", "tiles/cache")
    git("add", "src/tiles/cache.txt")
    git("commit", "-q", "-m", "Tile cache keeps 400 tiles per zoom level (op-002)", t=E - D - 2 * H)
    git("checkout", "-q", "main")
    git("merge", "-q", "--no-ff", "-m", "Merge tiles/cache", "tiles/cache", t=E - D)
    git("checkout", "-q", "-b", "sync/merge")
    git("add", "src/sync")
    git("commit", "-q", "-m", "Sync merge notes (op-003)", t=E - 6 * H)
    git("checkout", "-q", "main")
    git("add", "src/tiles/packs.txt", "src/app")
    git("commit", "-q", "-m", "Pack downloader and the map screen (op-002)", t=E - 4 * H)
    git("tag", "field-test-1", t=E - 3 * H)


def _stubs(root, E, fixed_clock, clock):
    from alpaca import gitview
    clock.set(E)
    snap = gitview.snapshot(root, 7)
    real = os.path.realpath(root)
    text = json.dumps(snap, indent=1, sort_keys=True).replace(real, FAKE_ROOT).replace(root, FAKE_ROOT)
    with open(os.path.join(root, ".studio-fixture", "stubs", "vcs.json"), "w", encoding="utf-8") as fh:
        fh.write(text + "\n")
    # The connection label and the health bar under Arthur read /hub/vitals.json (t-590). The stub
    # reports a fresh page and full capture, so the label is "Live" whichever of the first route and
    # the first vitals read lands last. The score comes from alpaca/healthscore.py on those facts: its
    # hub speed times real requests, host state like the CPU load, so the live answer would differ
    # between two captures.
    from alpaca import healthscore
    capture = {"status": "ok", "collector": {"status": "fresh", "age_seconds": 4, "liveness": "alive"},
               "backlog": {"bytes": 0, "preservation_jobs": 0, "observations": 0},
               "coverage": {"expected": 3, "registered": 3, "readable": 3, "missing_children": 0,
                            "unknown_children": 0, "complete": True}, "issues": {"open": 0}}
    projection = {"state": "fresh", "detail": "analytics fold and status board match current inputs",
                  "checked_at": E, "refreshed_at": E}
    score = healthscore.score({"page": projection, "speed": [], "collector": capture["collector"], "backlog": 0,
                               "issues": [], "issues_open": 0, "failures": {}, "missing_children": 0}, E)
    with open(os.path.join(root, ".studio-fixture", "stubs", "vitals.json"), "w", encoding="utf-8") as fh:
        json.dump({"projection": projection, "observability": capture, "score": score}, fh, indent=1)
    gib = 1024 ** 3
    system = {
        "ts": _iso(E), "host": "atlas-build-01", "uptime_s": 9 * 86400 + 4 * 3600, "sample_s": 0.5,
        "load": [1.42, 1.18, 0.97],
        "cpu": {"count": 8, "pct": 23.5, "per_core": [31.0, 12.5, 40.2, 8.0, 22.1, 18.7, 35.4, 20.1]},
        "mem": {"total_b": 32 * gib, "used_b": 13 * gib, "available_b": 19 * gib, "pct": 40.6,
                "swap_total_b": 4 * gib, "swap_used_b": gib // 4, "swap_pct": 6.3},
        "disks": [{"mount": "/", "path": "/", "paths": ["project"], "fs": "ext4", "total_b": 480 * gib,
                   "used_b": 212 * gib, "pct": 44.2, "inodes_total": 31000000, "inodes_used": 2400000, "inodes_pct": 7.7, "quota": None},
                  {"mount": "/tmp", "path": "/tmp", "paths": ["/tmp"], "fs": "tmpfs", "total_b": 16 * gib,
                   "used_b": 2 * gib, "pct": 12.5, "inodes_total": 0, "inodes_used": 0, "inodes_pct": 0,
                   "quota": {"used_b": gib, "limit_b": 8 * gib, "inode_limit": None}}],
        "net": {"status": "ok", "ms": 18.0, "targets": []},
        "users": [{"user": "surveyor", "cpu_pct": 64.2, "mem_pct": 22.0, "rss_b": 7 * gib, "procs": 41, "top": "node"},
                  {"user": "atlas-ci", "cpu_pct": 18.4, "mem_pct": 9.1, "rss_b": 3 * gib, "procs": 12, "top": "python3"}],
        "procs": [{"pid": 3100 + i, "user": u, "name": n, "cmd": c, "cpu_pct": cpu, "mem_pct": mem,
                   "rss_b": rss, "elapsed_s": el, "state": "S"}
                  for i, (u, n, c, cpu, mem, rss, el) in enumerate([
                      ("surveyor", "node", "node tile-server.js --port 9100", 38.5, 6.2, 2 * gib, 5400),
                      ("surveyor", "python3", "python3 -m alpaca serve --keep", 4.1, 1.1, 380 * 1024 ** 2, 86000),
                      ("atlas-ci", "python3", "python3 -m pytest tests/sync", 17.9, 4.0, 1300 * 1024 ** 2, 620),
                      ("surveyor", "chrome", "chrome --type=renderer", 9.2, 3.5, 1100 * 1024 ** 2, 3600)])],
        "notes": [],
    }
    with open(os.path.join(root, ".studio-fixture", "stubs", "system.json"), "w", encoding="utf-8") as fh:
        json.dump(system, fh, indent=1)


def _registry(root):
    import hashlib
    real = os.path.realpath(root)
    neighbour = "/work/harbor-lights"
    entries = [{"id": hashlib.sha256(real.encode()).hexdigest()[:12], "name": NAME, "root": real, "port": 7461,
                "hostname": None},
               {"id": hashlib.sha256(neighbour.encode()).hexdigest()[:12], "name": "Harbor Lights", "root": neighbour,
                "port": 1, "hostname": None}]
    with open(os.path.join(root, ".studio-fixture", "workspaces.json"), "w", encoding="utf-8") as fh:
        json.dump({"workspaces": entries}, fh, indent=1)


#: the sample profile with a page of its own (alpaca/tests/studio/sample_profile), for the project slot
SAMPLE_PROFILE = os.path.join(HERE, "studio", "sample_profile")
SAMPLE_PACKAGE = "studio_sample"


def add_sample_profile(root):
    """Give the project at `root` the sample profile: its package copied to <root>/studio_sample (outside
    every mechanism path, so `alpaca upgrade` leaves it alone) and named in project.yaml. The standard
    fixture never carries it, so the t-474 baseline and the t-475 checks keep the default Studio."""
    import shutil
    shutil.copytree(SAMPLE_PROFILE, os.path.join(root, SAMPLE_PACKAGE),
                    ignore=shutil.ignore_patterns("__pycache__"))
    path = os.path.join(root, "project.yaml")
    text = open(path, encoding="utf-8").read() if os.path.exists(path) else ""
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text + ("" if not text or text.endswith("\n") else "\n") + "profile: %s.profile\n" % SAMPLE_PACKAGE)
    return SAMPLE_PACKAGE + ".profile"


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("root", help="an empty (or missing) directory; its last name should be %s" % FOLDER)
    p.add_argument("--at", default=AT, help="the instant the Studio is shown at (ISO 8601, UTC)")
    p.add_argument("--sample-profile", action="store_true",
                   help="also give the project the sample profile with a page of its own (project slot)")
    a = p.parse_args(argv)
    build(a.root, a.at)
    if a.sample_profile:
        add_sample_profile(os.path.abspath(a.root))
    print(os.path.abspath(a.root))
    return 0


if __name__ == "__main__":
    sys.exit(main())
