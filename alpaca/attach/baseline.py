"""The gate baseline: which of the product's own checks are already red on a base commit (op-062 t-648, R3).

Attach runs every attach gate once on the upstream base commit and keeps the result, so a later
check judges a work branch only for what it breaks (build plan P9, P11). The result lives in
`.alpaca/attach/baseline/<key>.json`; the key is the base sha together with the gate set (names,
commands, timeouts, interpreter and host), so a changed gate or host makes a new baseline instead of
reusing a stale one (pre-mortem t-648.4).

What this module does:

  * `ensure(root, base_sha)` runs the gates once per key. A flock on `baseline/<key>.lock` makes
    parallel callers (four claims at once) wait for the first, which writes the file; the others read
    it and run nothing (t-648.3). The file is written whole through safefs, so a reader never sees a
    half-written JSON. Every gate runs (fail_fast off) and a gate passes on exit status alone: exit 0
    and no timeout (P9). That a gate edited a tracked file in its clone is a note on the result.
  * `make_seed(app, dest, refs)` builds a small scratch repository holding only the named commits,
    fetched from the attached clone by object, never by checking anything out in `app/`. The gate
    clones are made from this seed, so a gate never runs inside `app/` or in a worktree.
  * `run_gate_set(cfg, seed, work)` calls the release check runner (`release.checks.run_gates`, called
    and not edited) with the inherited git variables removed from the process (P4, PM X1).

Every file and folder this module makes comes from `safefs` (the write guard test enforces it); every
process it starts is a git call through `gitops` or a gate command through the release runner.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import platform
import re
from pathlib import Path

from alpaca.attach import AttachError, STATE_DIR, config, gitops, safefs

SCHEMA = 1
#: the ref a baseline seed holds the base commit under
BASE_REF = "refs/preview/base"
#: characters of a gate's output kept on the result file, and on the record event
LOG_TAIL = 2000
EVENT_TAIL = 500

_SHA = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_REF = re.compile(r"refs/[A-Za-z0-9][A-Za-z0-9_./-]*")


# ------------------------------------------------------------------------------------ the key

def key(cfg, base_sha) -> str:
    """16 hex characters naming the baseline of this gate set on this commit (P11). The gate names
    are part of it, so renaming a gate cannot reuse results filed under the old name."""
    material = "\n".join([str(base_sha), cfg.gate_set_digest(), ",".join(g["name"] for g in cfg.gates)])
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def _relative(*parts) -> str:
    return "/".join((STATE_DIR,) + parts)


def path(root, base_sha, cfg=None) -> Path:
    """Where the baseline of `base_sha` is (or would be) kept."""
    cfg = cfg or config.load(root)
    return Path(str(root)) / _relative("baseline", "%s.json" % key(cfg, base_sha))


def _sha(value) -> str:
    text = str(value or "").strip().lower()
    if not _SHA.fullmatch(text):
        raise AttachError("%r is not a full commit sha" % text[:70])
    return text


def load(root, base_sha, cfg=None):
    """The recorded baseline for `base_sha` and the current gate set, or None. A file that does not
    match what it should hold (schema, key, sha, gate set) counts as absent, never as a baseline."""
    cfg = cfg or config.load(root)
    sha = _sha(base_sha)
    found = config._json_dict(root, path(root, sha, cfg))
    if not found or found.get("schema") != SCHEMA:
        return None
    if found.get("key") != key(cfg, sha) or found.get("base_sha") != sha or found.get("gate_set") != cfg.gate_set_digest():
        return None
    gates = found.get("gates")
    if not isinstance(gates, list) or [g.get("name") if isinstance(g, dict) else None for g in gates] \
            != [g["name"] for g in cfg.gates]:
        return None
    if not _recorded(root, found):
        return None
    return found


def _verdicts(gates):
    """[(name, passed, returncode, timed_out)] of a gate list, or None when it is not a list of objects."""
    if not isinstance(gates, list) or not all(isinstance(g, dict) for g in gates):
        return None
    return [(g.get("name"), g.get("passed"), g.get("returncode"), g.get("timed_out")) for g in gates]


def _recorded(root, found) -> bool:
    """True when the record holds an `attach-baseline` event for this very result (same key, sha and
    per-gate verdicts). The file lives in the studio where an agent can write; the event does not, so a
    hand-written file that marks a passing gate red (and so excuses a break) finds no event and counts
    as no baseline: the gates run again under the lock (adversarial round 1, N1)."""
    from alpaca import db
    want = _verdicts(found.get("gates"))
    if want is None:
        return False
    try:
        conn = db.connect(str(root))
    except Exception:
        return False
    try:
        for row in conn.execute("SELECT data FROM events WHERE kind='attach-baseline' ORDER BY id DESC"):
            try:
                data = json.loads(row["data"]) if row["data"] else {}
            except ValueError:
                continue
            if isinstance(data, dict) and data.get("key") == found.get("key") \
                    and data.get("base_sha") == found.get("base_sha") and _verdicts(data.get("gates")) == want:
                return True
        return False
    finally:
        conn.close()


# -------------------------------------------------------------------------------------- the seed

def make_seed(app, dest, refs) -> Path:
    """Fill the empty folder `dest` (made by the caller through `safefs.make_dir`) with a git
    repository holding the commits `refs` names, fetched from the clone `app` and nothing else.

    `refs` maps a ref name in the seed (`refs/preview/base`) to the commit sha it must point at, or to
    a pair `(source ref in app, sha)` to fetch by name. Each fetched ref is verified against its sha;
    the first one is checked out detached. Nothing in `app` changes: git only reads it."""
    dest = str(dest)
    if os.path.islink(dest) or not os.path.isdir(dest):
        raise AttachError("the seed folder %s is not a folder of its own" % os.path.basename(dest))
    if not refs:
        raise AttachError("a seed needs at least one commit")
    gitops.git(dest, "init", "-q")
    first = None
    for dst, spec in refs.items():
        src, sha = spec if isinstance(spec, tuple) else (None, spec)
        sha = _sha(sha)
        src = src or sha
        if not _REF.fullmatch(dst) or ".." in dst or dst.endswith((".lock", "/")):
            raise AttachError("%r is not a ref name for a seed" % str(dst)[:70])
        if src != sha and not _REF.fullmatch(src):
            raise AttachError("%r is not a ref name to fetch" % str(src)[:70])
        gitops.git(dest, "fetch", "-q", "--no-tags", "--no-write-fetch-head", "--no-recurse-submodules",
                   "--", str(app), "+%s:%s" % (src, dst), timeout=600)
        got = gitops.git(dest, "rev-parse", "--verify", "%s^{commit}" % dst)
        if got != sha:
            raise AttachError("the seed ref %s holds %s, not the expected %s" % (dst, got[:12], sha[:12]))
        first = first or sha
    gitops.git(dest, "checkout", "-q", "--detach", first)
    return Path(dest)


# ---------------------------------------------------------------------------------- running gates

def _tail(log, limit=LOG_TAIL) -> str:
    try:
        with open(log, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - limit * 4))
            data = fh.read()
    except OSError:
        return ""
    text = data.decode("utf-8", "replace").translate({0: None})
    return text[-limit:]


def _result(gate, out) -> dict:
    """One gate's result in the baseline shape (P9): pass is exit 0 and no timeout, nothing more."""
    code = out.get("returncode")
    timed_out = bool(out.get("timed_out"))
    passed = code == 0 and not timed_out
    detail = str(out.get("detail") or "")
    if passed and detail:
        detail = "note: %s (not a failure)" % detail
    elif not passed and not detail:
        detail = "timed out" if timed_out else "exit %s" % code
    return {"name": gate["name"], "returncode": code, "timed_out": timed_out, "passed": passed,
            "duration_s": out.get("duration_s"), "detail": detail, "log_tail": _tail(out.get("log")),
            "definition": out.get("definition")}


def run_gate_set(cfg, seed, work) -> list:
    """Run every attach gate on a fresh clone of `seed`, one by one, none skipped when another fails.
    `work` is an existing folder (from `safefs.make_dir`) for the clones and the logs. Returns one
    result per gate in the configured order."""
    from alpaca.release import checks
    from alpaca.release.config import ReleaseConfig
    gates = [dict(g) for g in cfg.gates]
    release = ReleaseConfig(allowlist="-", repository="o/n", gates=gates, fail_fast=False, parallel=1)
    raw = []
    with gitops.process_env_scrubbed():
        checks.run_gates(Path(work), release, Path(seed), None, raw)
    by_name = {r.get("name"): r for r in raw}
    missing = [g["name"] for g in gates if g["name"] not in by_name]
    if missing:
        raise AttachError("the gate runner returned no result for %s" % ", ".join(missing))
    return [_result(g, by_name[g["name"]]) for g in gates]


# ------------------------------------------------------------------------------------- ensure

def _op_of(conn, task):
    row = conn.execute("SELECT op FROM tasks WHERE id=?", (task,)).fetchone()
    return row["op"] if row is not None else None


def _record(root, payload, op, task, session) -> None:
    """The `attach-baseline` event: the sha, the gate set and each gate's exit status, duration and
    output tail. On op-0 for the onboarding baseline, on the task's op for a claim's."""
    from alpaca import db
    conn = db.connect(str(root))
    try:
        if op is None and task:
            op = _op_of(conn, task)
        gates = [{"name": g["name"], "returncode": g["returncode"], "timed_out": g["timed_out"],
                  "passed": g["passed"], "duration_s": g["duration_s"], "detail": g["detail"],
                  "log_tail": g["log_tail"][-EVENT_TAIL:]} for g in payload["gates"]]
        db.append_event(conn, session=session, actor="alpaca", kind="attach-baseline", op=op, ref=task,
                        data={"key": payload["key"], "base_sha": payload["base_sha"], "gate_set": payload["gate_set"],
                              "task": task, "gates": gates, "red": [g["name"] for g in gates if not g["passed"]]})
    finally:
        conn.close()


def ensure(root, base_sha, *, op=None, task=None, session="cli") -> dict:
    """The baseline of `base_sha`: the recorded one, or the result of running the gates once.

    Parallel callers for one key wait on the key's lock; the first runs the gates and writes the file,
    the others find it after the lock and run nothing (t-648.3). The run folder is under
    `.alpaca/attach/runs/_baseline/<key>` and is deleted when the run ends."""
    cfg = config.load(root)
    sha = _sha(base_sha)
    have = load(root, sha, cfg)
    if have is not None:
        return have
    app = str(cfg.repo_path(root))
    if not gitops.is_toplevel(app):
        raise AttachError("%s/ is not a git checkout of its own; run alpaca attach status" % cfg.repo)
    the_key = key(cfg, sha)
    fd = safefs.open_lock(root, _relative("baseline", "%s.lock" % the_key))
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        have = load(root, sha, cfg)                    # another caller finished while this one waited
        if have is not None:
            return have
        payload = _run(root, cfg, app, sha, the_key)
        target = path(root, sha, cfg)
        config.write_json_atomic(target, payload, root=root)
        try:
            _record(root, payload, op, task, session)
        except BaseException:
            try:
                safefs.remove(root, str(target))       # no event, no baseline: a retry redoes both
            except AttachError:
                pass                                   # a link in the way: safefs deleted nothing
            raise
        return payload
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _run(root, cfg, app, sha, the_key) -> dict:
    from alpaca import util
    folder = _relative("runs", "_baseline", the_key)
    safefs.make_dir(root, folder)
    safefs.remove_tree(root, folder)                   # a run that was killed; the lock is held
    run_dir = safefs.make_dir(root, folder)
    try:
        seed = safefs.make_dir(root, os.path.join(run_dir, "seed"))
        work = safefs.make_dir(root, os.path.join(run_dir, "work"))
        make_seed(app, seed, {BASE_REF: sha})
        results = run_gate_set(cfg, seed, work)
    finally:
        try:
            safefs.remove_tree(root, folder)           # through no link; the run folder is this call's own
        except AttachError:
            pass
    return {"schema": SCHEMA, "key": the_key, "base_sha": sha, "gate_set": cfg.gate_set_digest(),
            "gates": results, "created_at": util.now_iso(), "host": platform.node()}


# ------------------------------------------------------------------------------------- the verb

def add_parser(verbs):
    p = verbs.add_parser("baseline", help="record (or show) the gate results on the upstream base commit")
    p.add_argument("--sha", default=None, help="base commit (default: the fetched upstream base)")
    p.add_argument("--fetch", action="store_true", help="fetch the upstream base first")
    p.add_argument("--json", action="store_true", help="print one JSON object")


def run(args) -> int:
    from alpaca import cli as alpaca_cli
    from alpaca.attach.cli import finish
    root = alpaca_cli._root()
    try:
        cfg = config.load(root)
        app = str(cfg.repo_path(root))
        if args.fetch:
            from alpaca.attach import worktree
            sha = worktree.fetch_base(root, cfg)
        elif args.sha:
            sha = _sha(args.sha)
        else:
            sha = gitops.git(app, "rev-parse", "--verify", "refs/remotes/%s/%s^{commit}" % (cfg.upstream, cfg.base))
        existed = load(root, sha, cfg) is not None
        result = ensure(root, sha, op="op-0", session=args.session or "cli")
    except AttachError as exc:
        return finish(args, "baseline", "FAIL", str(exc))
    lines = ["alpaca attach baseline: %s on %s (%s)" % (result["key"], sha[:12], "recorded before" if existed else "recorded now")]
    for g in result["gates"]:
        lines.append("  gate %s: %s exit %s %ss" % (g["name"], "PASS" if g["passed"] else "RED", g["returncode"], g["duration_s"]))
    receipt = {"key": result["key"], "base_sha": sha, "gates": {g["name"]: g["passed"] for g in result["gates"]}}
    return finish(args, "baseline", "PASS", lines=lines, receipt=receipt)


SUBVERBS = {"baseline": (add_parser, run)}


# ------------------------------------------------------------- appended by t-649 (plan 6.3: append only)

def run_gate_set_each(cfg, seed, work, progress) -> list:
    """`run_gate_set`, with `progress` handed to the release runner: its `start(name)` and
    `finish(name, status, duration_s)` are called in this thread as each gate starts and ends.
    The check passes an `alpaca.attach.reap.Reaper`, so what one gate leaves running is stopped before
    the next gate's clone is made (op-062 t-649, adversarial round 2 N9b, N9c). The gates run one by
    one (parallel 1), which the reaper needs: a second gate running at the same time would look like
    a leftover of the first."""
    from alpaca.release import checks
    from alpaca.release.config import ReleaseConfig
    gates = [dict(g) for g in cfg.gates]
    release = ReleaseConfig(allowlist="-", repository="o/n", gates=gates, fail_fast=False, parallel=1)
    raw = []
    with gitops.process_env_scrubbed():
        checks.run_gates(Path(work), release, Path(seed), None, raw, progress=progress)
    by_name = {r.get("name"): r for r in raw}
    missing = [g["name"] for g in gates if g["name"] not in by_name]
    if missing:
        raise AttachError("the gate runner returned no result for %s" % ", ".join(missing))
    return [_result(g, by_name[g["name"]]) for g in gates]
