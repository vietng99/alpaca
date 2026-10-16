"""The verification scope, the done-gate check that proof.done_gate calls, and the verify state
(design t-089, verifier.md section 8).

`check` is the one place a done move waits for the two-agent verdict. It reads the record,
`project.yaml` and the verification's kept review files, nothing else: it launches nothing and
runs no subprocess, because the roadmap projection calls `proof.done_gate` for every done task
on each overview build (alpaca/taskplan.py:203-215). It never raises. `take_tree` (and
`seal_tree`, which wraps it) is the one function here that runs git, and only `proof.seal`
calls it.

Scope, first match wins (section 8.3, step 1): `engine` (the task's `task-add` carries a
`research` object), `seal` (this seal carries a `verify_scope`), `sticky` (an earlier seal
carries one), `request` (a verification was opened for this task), then the project policy
`proof_verify` in `project.yaml` (`policy:all`, `policy:op`, `policy:malformed`). With none of
these a task closes on its seal alone, as before this change.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess

from alpaca import project
from alpaca.research import events as rev_events, verdicts

PROBLEM_PREFIX = "verification "

#: section 3.2: exact done-bar lines about this task's own proof. The done gate checks them, and
#: no verifier can while the verification runs, so they are never claims.
SELF_LINES = (
    "the proof is sealed and verified by both agents",
    "the proof is sealed and verified by both verifiers",
    "the proof is verified by both agents",
    "the proof is verified by both verifiers",
    "this proof is sealed and verified by both agents",
    "this proof is sealed and verified by both verifiers",
    "this proof is verified by both agents",
    "this proof is verified by both verifiers",
)

REVERIFY_MAX = 2            # section 6.10; proof_verify.reverify_max overrides it

DEFAULT_TIMEOUT_S = 1800
DEFAULT_RETRIES = 2
SCOPES = ("engine", "all")

STATES = ("not-in-scope", "not-sealed", "required", "running", "paused", "stopped", "exhausted",
          "verified", "refuted", "unverified", "superseded")
ROUND_CAP = 5
HARD_CAP = 10
#: section 6.7: the driver stops a resume cannot clear; the cause lives in the seal.
SEAL_STOPS = ("VERIFY-STEERING", "VERIFY-NO-COMMAND", "VERIFY-BRIEF-NOT-BLIND", "VERIFY-NO-CLAIMS",
              "VERIFY-TOO-MANY-CLAIMS", "VERIFY-RUNTIME-MISMATCH")
STOP_CODES = verdicts.STOP_CODES
#: the rule of a VERIFY-TREE problem whose cause lives in the seal (section 8.4): a sealed patch
#: that does not apply, or a kept or sealed file that no longer hashes to its sealed value. The
#: driver sets it where it raises the problem; a resume cannot clear it, a new seal can.
SEALED_RULE = "sealed"
#: the tail a stop with a cause in the seal carries (section 8.4, the runtime binary row);
#: next_steps prints the re-seal steps for a stopped problem that ends with it.
RESEAL_TAIL = "; seal the report again with alpaca proof seal %s"
#: VERIFY-TREE details the driver wrote for a cause in the seal before it recorded SEALED_RULE
_SEALED_DETAILS = ("the kept ", "the sealed ", "evidence ")
FAILED_CODES = verdicts.FAILED_CODES

# the record kinds, bound to events.py's constants so a misspelled kind fails at import
KIND_OPEN = rev_events.VERIFY_OPEN
KIND_ASSIGN = rev_events.VERIFY_ASSIGN
KIND_REFUSED = rev_events.VERIFY_REFUSED
KIND_REVIEW = rev_events.VERIFY_REVIEW
KIND_VERDICT = rev_events.PROOF_VERDICT
KIND_ROUND = rev_events.VERIFY_ROUND
KIND_EXTEND = rev_events.PROOF_EXTEND
KIND_RULING = rev_events.PROOF_RULING
KIND_END = rev_events.VERIFY_END
KIND_LAUNCH_END = rev_events.LAUNCH_END
SEAL_KIND = "proof-report"

_POLICY_CACHE = {}
_KEPT_CACHE = {}


# --------------------------------------------------------------------------- where things live
def slug(ident) -> str:
    """A task or row id made safe for a path the way proof._slug does."""
    from alpaca import proof
    return proof._slug(ident)


def verify_root(root, task) -> str:
    """`.alpaca/verify/<task>/`: every verification folder and the kept reviews of one task."""
    return os.path.join(root, ".alpaca", "verify", slug(task))


def verification_dir(root, task, verification) -> str:
    return os.path.join(verify_root(root, task), verification)


def kept_dir(root, task) -> str:
    return os.path.join(verify_root(root, task), "kept")


# --------------------------------------------------------------------------- record reads
def _load(value):
    if isinstance(value, dict):
        return value
    try:
        data = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def events(conn, kind, ref=None, *, verification=None) -> list:
    """Events of one kind, oldest first, with `data` parsed; by `ref` and by
    `data.verification` when given. A read by kind and ref on the events table."""
    q, p = "SELECT id, ts, session, actor, kind, op, ref, data FROM events WHERE kind=?", [kind]
    if ref is not None:
        q += " AND ref=?"
        p.append(ref)
    if verification is not None:
        q += " AND json_extract(data, '$.verification')=?"
        p.append(verification)
    out = []
    for r in conn.execute(q + " ORDER BY id", p):
        out.append({"id": r["id"], "ts": r["ts"], "session": r["session"], "actor": r["actor"],
                    "kind": r["kind"], "op": r["op"], "ref": r["ref"], "data": _load(r["data"])})
    return out


def event_by_id(conn, eid):
    if not isinstance(eid, int) or isinstance(eid, bool):
        return None
    r = conn.execute("SELECT id, ts, session, actor, kind, op, ref, data FROM events WHERE id=?",
                     (eid,)).fetchone()
    if r is None:
        return None
    return {"id": r["id"], "ts": r["ts"], "session": r["session"], "actor": r["actor"],
            "kind": r["kind"], "op": r["op"], "ref": r["ref"], "data": _load(r["data"])}


def seals(conn, ident) -> list:
    return events(conn, SEAL_KIND, ident)


def research_of(conn, ident):
    """The `research` object of the task's `task-add` event, or None (not an engine task)."""
    for ev in events(conn, "task-add", ident):
        obj = ev["data"].get("research")
        if isinstance(obj, dict):
            return obj
        if obj is not None:
            return {"role": None}
    return None


def role_of(conn, ident):
    obj = research_of(conn, ident)
    return obj.get("role") if isinstance(obj, dict) else None


def _op_of(conn, ident):
    for table in ("tasks", "rows"):
        try:
            row = conn.execute("SELECT op FROM %s WHERE id=?" % table, (ident,)).fetchone()
        except Exception:
            row = None
        if row is not None:
            return row["op"]
    return None


def verification_id(task, seal_event) -> str:
    return "vf-%s-%d" % (task, int(seal_event))


# --------------------------------------------------------------------------- the policy
def policy(root) -> dict:
    """{"scope", "ops", "timeout_s", "retries", "reverify_max", "error"} from `proof_verify` in
    project.yaml, cached by the file's mtime. With the key absent the scope is `engine` and the
    list empty. A malformed value fails closed: scope `all`, and `error` names the key."""
    path = project.path(root)
    try:
        st = os.stat(path)
        stamp = (st.st_mtime_ns, st.st_size)
    except OSError:
        stamp = None
    key = os.path.abspath(root)
    cached = _POLICY_CACHE.get(key)
    if cached is not None and cached[0] == stamp:
        return dict(cached[1])
    out = {"scope": "engine", "ops": [], "timeout_s": DEFAULT_TIMEOUT_S,
           "retries": DEFAULT_RETRIES, "reverify_max": REVERIFY_MAX, "error": None}
    try:
        cfg = project.load(root) if stamp is not None else {}
    except Exception as exc:
        cfg = {}
        out.update(scope="all", error="project.yaml cannot be read: %s" % type(exc).__name__)
    value = cfg.get("proof_verify") if isinstance(cfg, dict) else None
    problems = []
    if value is not None:
        if not isinstance(value, dict):
            problems.append("proof_verify must be a mapping")
        else:
            unknown = sorted(set(value) - {"scope", "ops", "timeout_s", "retries", "reverify_max"})
            if unknown:
                problems.append("proof_verify has unknown keys: %s" % ", ".join(map(str, unknown)))
            if "scope" in value:
                if value["scope"] in SCOPES:
                    out["scope"] = value["scope"]
                else:
                    problems.append("proof_verify.scope must be engine or all")
            if "ops" in value:
                ops = value["ops"]
                if isinstance(ops, list) and all(isinstance(o, str) and o for o in ops):
                    out["ops"] = list(ops)
                else:
                    problems.append("proof_verify.ops must be a list of op ids")
            for name, lo in (("timeout_s", 1), ("retries", 0), ("reverify_max", 0)):
                if name in value:
                    v = value[name]
                    if isinstance(v, int) and not isinstance(v, bool) and v >= lo:
                        out[name] = v
                    else:
                        problems.append("proof_verify.%s must be an integer of at least %d"
                                        % (name, lo))
    if problems:
        out["scope"] = "all"
        out["error"] = "; ".join(problems)
    _POLICY_CACHE[key] = (stamp, dict(out))
    return out


def _policy_scope(conn, root, ident):
    pol = policy(root)
    if pol["error"]:
        return "policy:malformed"
    if pol["scope"] == "all":
        return "policy:all"
    op = _op_of(conn, ident)
    if op and op in pol["ops"]:
        return "policy:op"
    return None


# --------------------------------------------------------------------------- scope
def scope(conn, root, ident, seal_event: dict) -> str | None:
    """The scope this seal is verified under, or None (section 8.3, step 1)."""
    if research_of(conn, ident) is not None:
        return "engine"
    data = (seal_event or {}).get("data") if isinstance(seal_event, dict) else None
    data = _load(data) if data is not None else {}
    if data.get("verify_scope"):
        return "seal"
    sid = (seal_event or {}).get("id")
    for ev in seals(conn, ident):
        if sid is not None and ev["id"] >= sid:
            continue
        if ev["data"].get("verify_scope"):
            return "sticky"
    if events(conn, KIND_OPEN, ident):
        return "request"
    return _policy_scope(conn, root, ident)


def scope_at_seal(conn, root, ident, *, requested: bool = False) -> str | None:
    """The `verify_scope` a new seal records (section 8.1, item 1), first match wins: `engine`,
    `request` (the seal asked with --verify, or a verification was opened on an earlier seal),
    `sticky:<value>` (an earlier seal carries one; any sticky: prefix dropped), the policy, or
    None."""
    if research_of(conn, ident) is not None:
        return "engine"
    if requested or events(conn, KIND_OPEN, ident):
        return "request"
    for ev in reversed(seals(conn, ident)):
        prior = ev["data"].get("verify_scope")
        if prior:
            while str(prior).startswith("sticky:"):
                prior = str(prior)[len("sticky:"):]
            return "sticky:%s" % prior
    return _policy_scope(conn, root, ident)


# --------------------------------------------------------------------------- the sealed tree
def seal_tree(root, kept_dir: str, *, pathspec: tuple = (".", ":(exclude).alpaca")) -> dict | None:
    """{"head", "patch", "patch_sha256", "bytes"}: the tree a seal's claims were made on
    (section 8.1, item 2), or None outside a git work tree. The patch holds the tracked changes
    and the untracked files that are not ignored, restricted to `pathspec`, taken with a copy of
    the checkout's index under the git folder so the checkout's own index is not touched, and
    kept as `tree.patch` in `kept_dir`. Raises ValueError when the patch is over the kept cap
    or git fails, before anything is written. `proof.seal` calls the two halves apart
    (`take_tree`, then `keep_tree` once the evidence copies are in place), so a refused seal
    leaves the latest seal's kept files as they were."""
    taken = take_tree(root, pathspec=pathspec)
    return None if taken is None else keep_tree(root, kept_dir, taken)


def take_tree(root, *, pathspec: tuple = (".", ":(exclude).alpaca"), base=None, env=None, extra_config=(),
              skip_gitlinks=False, diff_args=(), want_tree=False):
    """(head, patch bytes) of `seal_tree`, held in memory and written nowhere, or None outside a
    git work tree. Raises ValueError when the patch is over the kept cap or git fails. `base` is
    the commit the patch is taken against (`HEAD` when None): an attached repository's work
    branch is committed work, which a diff against its own HEAD would show as nothing
    (op-062 t-650). `env` is the whole environment git runs in (the process environment when None) and
    `extra_config` a tuple of `key=value` settings passed as `-c`: the attached repository's seal passes a
    clean environment, so the git that stages the files reads no config the filter check did not read.
    The copy of the index is made with O_EXCL and O_NOFOLLOW and the seal refuses when that name is taken,
    so a link planted at the guessable name is not written through (t-650 fix round 1, A7).
    `skip_gitlinks` leaves the submodule paths of the index out of `git add -A`: git looks into a tracked
    submodule there with the submodule's own config, which runs its clean filter, and the diff ignores
    submodules anyway (fix round 1, A1).
    `diff_args` are extra `git diff` options placed before the base (the attached repository's seal pins the
    prefixes, colour and relative mode so no `diff.*` key of the clone changes the paths in the patch), and
    `want_tree` returns `(head, patch, tree id)` where the tree id is `git write-tree` of the staged copy of
    the index: the tree the patch is meant to produce when it is applied to `base` (op-062 t-650 fix round
    2, B3 and B2). Both default to the old behaviour, so a plain project's argv and return value are unchanged."""
    from alpaca import proof
    if not os.path.isdir(os.path.join(root, ".git")) and not os.path.isfile(os.path.join(root, ".git")):
        return None
    env = dict(os.environ if env is None else env)
    env.pop("GIT_INDEX_FILE", None)
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_TERMINAL_PROMPT="0")

    def git(*args, extra_env=None, binary=False):
        e = dict(env)
        if extra_env:
            e.update(extra_env)
        return subprocess.run(("git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false")
                              + tuple(x for kv in extra_config for x in ("-c", kv)) + args,
                              cwd=root, capture_output=True, env=e, timeout=300, text=not binary)

    head = git("rev-parse", "--verify", "HEAD")
    if head.returncode != 0:
        return None
    head = head.stdout.strip()
    where = git("rev-parse", "--git-path", "alpaca-seal-index.%d" % os.getpid())
    if where.returncode != 0:
        return None
    index_copy = where.stdout.strip()
    if not os.path.isabs(index_copy):
        index_copy = os.path.join(root, index_copy)
    real_index = git("rev-parse", "--git-path", "index").stdout.strip()
    if real_index and not os.path.isabs(real_index):
        real_index = os.path.join(root, real_index)
    if os.path.lexists(index_copy):
        raise ValueError("the seal's index copy %s already exists; remove it and seal again"
                         % os.path.basename(index_copy))
    fd = None
    if real_index and os.path.isfile(real_index):
        try:                                    # a name taken in the meantime, or a link: never written through
            fd = os.open(index_copy, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
                         | getattr(os, "O_CLOEXEC", 0), 0o600)
        except OSError:
            raise ValueError("the seal's index copy %s could not be created exclusively; remove it and seal again"
                             % os.path.basename(index_copy)) from None
    try:
        if fd is not None:
            with os.fdopen(fd, "wb") as out, open(real_index, "rb") as src:
                shutil.copyfileobj(src, out)
        extra = {"GIT_INDEX_FILE": index_copy}
        add_spec = tuple(pathspec)
        if skip_gitlinks:
            listed = git("ls-files", "-z", "-s", extra_env=extra, binary=True)
            if listed.returncode != 0:
                raise ValueError("git ls-files for the tree patch failed")
            for row in listed.stdout.split(b"\0"):
                meta, _, name = row.partition(b"\t")
                if meta.startswith(b"160000 "):
                    add_spec += (":(exclude,literal)" + name.decode("utf-8", "surrogateescape"),)
        add = git("add", "-A", "--", *add_spec, extra_env=extra)
        # an excluded path that the checkout ignores (a `.alpaca/` line in .gitignore) makes git
        # name it and exit 1 after it has staged every other path; that is not a failure here
        if add.returncode != 0 and "ignored by one of your .gitignore files" not in add.stderr:
            raise ValueError("git add for the tree patch failed: %s" % add.stderr.strip()[:200])
        diff = git("diff", "--cached", "--binary", "--no-ext-diff", "--no-textconv",
                   "--ignore-submodules=all", *diff_args, base or "HEAD", "--", *pathspec, extra_env=extra,
                   binary=True)
        if diff.returncode != 0:
            raise ValueError("git diff for the tree patch failed")
        patch = diff.stdout
        tree_id = None
        if want_tree:
            wrote = git("write-tree", extra_env=extra)
            tree_id = wrote.stdout.strip() if wrote.returncode == 0 else ""
            if not tree_id:
                raise ValueError("git write-tree for the tree patch failed")
    finally:
        try:
            os.remove(index_copy)
        except OSError:
            pass
    if len(patch) > proof.KEPT_FILE_CAP:
        raise ValueError("the tree patch is over the kept cap: commit the work first")
    return (head, patch, tree_id) if want_tree else (head, patch)


def keep_tree(root, kept_dir: str, taken: tuple) -> dict:
    """Write the patch `take_tree` returned as `tree.patch` in `kept_dir` (a part file moved
    into place) and return the seal's `tree` value."""
    from alpaca import proof
    head, patch = taken
    os.makedirs(kept_dir, exist_ok=True)
    dest = os.path.join(kept_dir, "tree.patch")
    part = os.path.join(kept_dir, ".tree.patch.part")
    with open(part, "wb") as fh:
        fh.write(patch)
    os.replace(part, dest)
    return {"head": head, "patch": proof._rel(root, dest),
            "patch_sha256": __import__("hashlib").sha256(patch).hexdigest(), "bytes": len(patch)}


# --------------------------------------------------------------------------- identities
def work_identities(conn, task) -> dict:
    """{"workers": set, "sessions": set}: who did the work under test (section 5.4). The workers
    of the task's `claim` events and of its proposer and implementer launches; the sessions that
    wrote its `proof-report`, `proof-scaffold`, `claim`, `heartbeat` and `task-move` events and
    its proposer and implementer launches. A pure read. Section 5.4 names the loop runner's
    dispatch-spawn events; this reads the `research-launch` events instead, which carry the
    same worker and session for each launch (architecture.md:3181, :2444-2449)."""
    workers, sessions = set(), set()
    for kind in ("claim", "heartbeat", "task-move", SEAL_KIND, "proof-scaffold"):
        for ev in events(conn, kind, task):
            if ev["session"]:
                sessions.add(ev["session"])
            if kind == "claim" and ev["data"].get("worker"):
                workers.add(str(ev["data"]["worker"]))
    for ev in events(conn, rev_events.LAUNCH, task):
        if ev["data"].get("role") in ("proposer", "implementer", "reproducer-writer"):
            for key, bag in (("worker", workers), ("session", sessions)):
                if ev["data"].get(key):
                    bag.add(str(ev["data"][key]))
    return {"workers": workers, "sessions": sessions}


# --------------------------------------------------------------------------- earlier verifications
def _try_key(d):
    return (d.get("slot") or d.get("verifier"), d.get("round"), d.get("try"))


def try_name(item) -> str:
    """`v1/r0/t2`: one try of a lost, failed, dropped or pending item, for printing."""
    return "%s/r%s/t%s" % (item.get("slot"), item.get("round"), item.get("try"))


def verification_facts(conn, task, verification) -> dict:
    """What one verification holds, from the record: its end, its stop, its counted refutations,
    its lost, failed, dropped and pending tries. The lost, failed and dropped items have the
    shape of the end event's (section 7): `{slot, round, try, refused_event}`, plus `code` for a
    failed try and `refuted` for a dropped refutation; a pending item carries its
    `assign_event` in place of the refusal."""
    end = events(conn, KIND_END, task, verification=verification)
    end = end[-1] if end else None
    assigns = events(conn, KIND_ASSIGN, task, verification=verification)
    verdicts = events(conn, KIND_VERDICT, task, verification=verification)
    refused = events(conn, KIND_REFUSED, task, verification=verification)
    refuted, lost, dropped, failed, pending, stop = [], [], [], [], [], None
    outcome_of = {}
    for ev in verdicts:
        d = ev["data"]
        outcome_of[_try_key(d)] = ev
        if d.get("synthetic"):
            continue
        for c in d.get("claims") or []:
            if isinstance(c, dict) and c.get("effective") == "refuted" and c.get("id") not in refuted:
                refuted.append(c["id"])
    for ev in refused:
        d = ev["data"]
        if d.get("slot") is None:
            if d.get("code") in STOP_CODES:
                stop = ev
            continue
        outcome_of[_try_key(d)] = ev
        item = {"slot": d.get("slot"), "round": d.get("round"), "try": d.get("try"),
                "refused_event": ev["id"]}
        if d.get("code") == "VERIFY-LOST":
            lost.append(dict(item))
        elif d.get("code") in FAILED_CODES:
            failed.append(dict(item, code=d.get("code")))
        if d.get("refuted"):
            dropped.append(dict(item, refuted=d.get("refuted")))
    for ev in assigns:
        d = ev["data"]
        if _try_key(d) not in outcome_of:
            pending.append({"slot": d.get("slot"), "round": d.get("round"), "try": d.get("try"),
                            "assign_event": ev["id"]})
    if stop is not None and assigns and stop["id"] < assigns[-1]["id"]:
        stop = None
    return {"end": end, "stop": stop, "refuted": sorted(refuted), "lost": lost,
            "dropped": dropped, "failed": failed, "pending": pending}


def refuted_runs(conn, task, *, before: int) -> list:
    """The task's verifications opened on seals older than `before` that did not end verified and
    hold a counted refutation, a lost, failed or pending try, or a dropped refutation (section
    6.10): [{"verification", "seal_event", "outcome", "refuted", "why", "counted", "tries"}]."""
    out = []
    for op_ev in events(conn, KIND_OPEN, task):
        d = op_ev["data"]
        seal = d.get("seal_event")
        if not isinstance(seal, int) or seal >= before:
            continue
        vf = d.get("verification")
        facts = verification_facts(conn, task, vf)
        end = facts["end"]
        outcome = end["data"].get("outcome") if end else ("stopped" if facts["stop"] else None)
        if outcome == "verified":
            continue
        why = []
        if facts["refuted"]:
            why.append("refuted")
        if facts["lost"]:
            why.append("lost")
        if facts["dropped"]:
            why.append("dropped")
        if facts["failed"]:
            why.append("failed")
        if facts["pending"]:
            why.append("pending")
        if not why:
            continue
        tries = ([("lost try %s" % try_name(t)) for t in facts["lost"]]
                 + [("failed try %s" % try_name(t)) for t in facts["failed"]]
                 + [("dropped refutation %s" % try_name(t)) for t in facts["dropped"]]
                 + [("pending try %s" % try_name(t)) for t in facts["pending"]])
        out.append({"verification": vf, "seal_event": seal, "outcome": outcome,
                    "refuted": facts["refuted"], "why": why, "counted": True, "tries": tries})
    return out


def earlier(conn, task, *, before: int) -> list:
    """Every verification of the task on an older seal, counted or not (the `earlier` key of the
    open and end events)."""
    counted = {r["verification"]: r for r in refuted_runs(conn, task, before=before)}
    out = []
    for op_ev in events(conn, KIND_OPEN, task):
        d = op_ev["data"]
        seal = d.get("seal_event")
        if not isinstance(seal, int) or seal >= before:
            continue
        vf = d.get("verification")
        if vf in counted:
            r = counted[vf]
            out.append({"verification": vf, "seal_event": seal, "outcome": r["outcome"],
                        "refuted": r["refuted"], "counted": True, "why": r["why"]})
            continue
        facts = verification_facts(conn, task, vf)
        end = facts["end"]
        out.append({"verification": vf, "seal_event": seal,
                    "outcome": end["data"].get("outcome") if end else
                    ("stopped" if facts["stop"] else None),
                    "refuted": facts["refuted"], "counted": False, "why": []})
    return out


#: the folders of a try whose files carry what a verifier chose, wrote or was answered
TRACE_FOLDERS = ("receipts", "out", "requests")
#: the one input folder set of a try an out/ copy may repeat without carrying a judgment
_INPUT_FOLDERS = ("packet", "tree", "base")
_EMPTY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def _files(folder):
    """(path, size) of every regular file under folder, links and `.git` entries left out."""
    out = []
    if not os.path.isdir(folder) or os.path.islink(folder):
        return out
    for base, dirs, names in os.walk(folder, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d != ".git")
        for name in sorted(names):
            full = os.path.join(base, name)
            if name == ".git" or os.path.islink(full):
                continue
            try:
                st = os.lstat(full)
            except OSError:
                continue
            if stat.S_ISREG(st.st_mode):
                out.append((full, st.st_size))
    return out


def _sha256_file(path) -> str | None:
    import hashlib
    h = hashlib.sha256()
    try:
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
    except OSError:
        return None
    return h.hexdigest()


INPUT_COPIES = "out-inputs.json"


def try_git_home(try_dir) -> str:
    """`<verification>/git/<slot>/r<round>/t<try>/` of a try folder: beside it, never inside it
    (the verifier's git folders and the notes the driver keeps about the try)."""
    vf_dir = os.path.dirname(os.path.dirname(os.path.dirname(try_dir)))
    return os.path.join(vf_dir, "git", os.path.relpath(try_dir, vf_dir))


def input_copies(try_dir) -> list:
    """The sha256 of each out/ file of a try that is a byte copy of a file of its own inputs
    (packet/, tree/ or base/). While the trees stand they are read; once the driver has removed
    them (the end of a verification) the list it kept then (`INPUT_COPIES`) is read."""
    outs = [(f, sz) for f, sz in _files(os.path.join(try_dir, "out")) if sz]
    if not outs:
        return []
    if os.path.isdir(os.path.join(try_dir, "tree")) or os.path.isdir(os.path.join(try_dir, "base")):
        inputs = [f for d in _INPUT_FOLDERS for f in _files(os.path.join(try_dir, d))]
        got = set()
        for full, size in outs:
            sha = _sha256_file(full)
            if sha and any(sz == size and _sha256_file(f) == sha for f, sz in inputs):
                got.add(sha)
        return sorted(got)
    try:
        with open(os.path.join(try_git_home(try_dir), INPUT_COPIES), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return []
    return sorted(str(h) for h in data if isinstance(h, str)) if isinstance(data, list) else []


def earlier_verifications(conn, task, *, before: int) -> list:
    """The ids of every verification of the task opened on a seal older than `before`."""
    out = []
    for ev in events(conn, KIND_OPEN, task):
        seal, vf = ev["data"].get("seal_event"), ev["data"].get("verification")
        if isinstance(seal, int) and seal < before and vf and vf not in out:
            out.append(vf)
    return out


def earlier_traces(conn, root, task, *, before: int) -> dict:
    """{"ids", "hashes", "sizes"}: what an earlier verification of the task left that no packet,
    brief or tree of a later one may carry (verifier.md section 5.2: its events, kept reviews and
    folders are withheld, and the brief check refuses any trace that is left).

    `ids` are the verification ids opened on a seal older than `before`. `hashes` are the sha256
    of every non-empty file under their `<slot>/r<round>/t<try>/` receipts/, out/ and requests/
    folders: the submitted verdict, the receipts, the answers, the requests and whatever the
    verifier wrote. Left out are the raw outputs of commands (`receipts/*.out`), which are what
    running a command over the sealed tree prints and which a tree file or a sealed log may
    equal byte for byte with no earlier judgment in them, and an out/ file that repeats a file
    of its own try's inputs (packet/, tree/ or base/; input_copies), which is a copy of an
    input, not a verdict. `sizes` are the byte sizes of the hashed files, so a caller can hash only the files
    that could match."""
    ids = earlier_verifications(conn, task, before=before)
    hashes, sizes = set(), set()
    for vf in ids:
        vdir = verification_dir(root, task, vf)
        for slot in verdicts.SLOTS:
            sdir = os.path.join(vdir, slot)
            if not os.path.isdir(sdir):
                continue
            for rnd in sorted(os.listdir(sdir)):
                rdir = os.path.join(sdir, rnd)
                if not rnd.startswith("r") or not os.path.isdir(rdir):
                    continue
                for tr in sorted(os.listdir(rdir)):
                    tdir = os.path.join(rdir, tr)
                    if not tr.startswith("t") or not os.path.isdir(tdir):
                        continue
                    copies = None
                    for name in TRACE_FOLDERS:
                        for full, size in _files(os.path.join(tdir, name)):
                            if size == 0 or (name == "receipts" and full.endswith(".out")):
                                continue
                            sha = _sha256_file(full)
                            if sha is None or sha == _EMPTY_SHA256:
                                continue
                            if name == "out":
                                if copies is None:
                                    copies = set(input_copies(tdir))
                                if sha in copies:
                                    continue
                            hashes.add(sha)
                            sizes.add(size)
    return {"ids": ids, "hashes": sorted(hashes), "sizes": sorted(sizes)}


# --------------------------------------------------------------------------- kept reviews
def kept_intact(root, path, sha256) -> bool:
    """True when the kept review file at `path` (root-relative) still hashes to `sha256`; the
    hash is cached by path, size, mtime, inode and ctime."""
    if not isinstance(path, str) or not path or os.path.isabs(path) or ".." in path.split("/"):
        return False
    full = os.path.join(root, path)
    try:
        st = os.lstat(full)
    except OSError:
        return False
    if not os.path.isfile(full) or os.path.islink(full):
        return False
    # st_ino and st_ctime_ns too: a rewrite that keeps the size and restores the mtime with
    # os.utime still changes the ctime, which no user can set
    key = (full, st.st_size, st.st_mtime_ns, st.st_ino, st.st_ctime_ns)
    got = _KEPT_CACHE.get(key)
    if got is None:
        import hashlib
        with open(full, "rb") as fh:
            got = hashlib.sha256(fh.read()).hexdigest()
        _KEPT_CACHE[key] = got
    return got == sha256


def kept_context(root, path) -> dict:
    try:
        with open(os.path.join(root, path), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


# --------------------------------------------------------------------------- the consistency step
def _recompute(conn, verification):
    return verdicts.outcome(conn, verification)


def consistency(conn, root, ident, seal_event, open_ev, end_ev) -> list:
    """Every way a `verified` end fails to hold together with its verdicts, kept reviews,
    assignments and minted sessions (section 8.3, step 5). Empty when it holds."""
    problems = []
    vf = open_ev["data"].get("verification")
    sdata = _load(seal_event.get("data"))
    end = end_ev["data"]
    if end.get("report_sha256") != sdata.get("sha256"):
        problems.append("its end names another report hash")
    tree = sdata.get("tree") if isinstance(sdata.get("tree"), dict) else None
    want_tree = tree.get("patch_sha256") if tree else None
    if end.get("tree_patch_sha256") != want_tree:
        problems.append("its end names another tree than the seal describes")
    # an attach seal also sealed the attached repo's work-branch diff (op-062 t-650): the end must
    # name the same app patch. A seal without one and an end without the key leave the check as it was.
    app = tree.get("app") if tree and isinstance(tree.get("app"), dict) else None
    if app or "app_patch_sha256" in end:
        if end.get("app_patch_sha256") != (app.get("patch_sha256") if app else None):
            problems.append("its end names another app patch than the seal describes")
    got = _recompute(conn, vf)
    if got["outcome"] != end.get("outcome"):
        problems.append("its verdict events fold to %s" % got["outcome"])
    # section 8.3, item 4: each recorded round folds again from its verdict events
    for round_ev in events(conn, KIND_ROUND, ident, verification=vf):
        rd = round_ev["data"]
        again = verdicts.fold_round(conn, vf, rd.get("round"))
        if (rd.get("settled") or {}) != again["settled"] \
                or sorted(rd.get("disputed") or []) != sorted(again["disputed"]) \
                or sorted(rd.get("scope") or []) != sorted(again["scope"]):
            problems.append("round %s does not fold again from its verdict events"
                            % rd.get("round"))
    ids = list(open_ev["data"].get("claim_ids") or [])
    if len(ids) != open_ev["data"].get("claims") or sorted(got["supported"]) != sorted(ids):
        problems.append("not every claim is settled supported")
    verdict_evs = events(conn, KIND_VERDICT, ident, verification=vf)
    last_round = end.get("rounds")
    finals = {}
    for ev in verdict_evs:
        if ev["data"].get("round") == last_round:
            finals[ev["data"].get("verifier")] = ev
    runtime = open_ev["data"].get("runtime")
    if set(finals) != {"v1", "v2"}:
        problems.append("round %s lacks a verdict of both slots" % last_round)
    elif any(finals[s]["data"].get("synthetic") for s in finals):
        problems.append("a final verdict is synthetic")
    elif finals["v1"]["data"].get("session") == finals["v2"]["data"].get("session"):
        problems.append("both final verdicts come from one session")
    elif any(finals[s]["data"].get("runtime") != runtime for s in finals):
        problems.append("a final verdict ran on another runtime than the open event names")
    assigns = {}
    for ev in events(conn, KIND_ASSIGN, ident, verification=vf):
        assigns[ev["data"].get("session")] = ev
    ids_of = work_identities(conn, ident)
    reviews = {ev["data"].get("review"): ev for ev in events(conn, KIND_REVIEW, ident)}
    cited = {"launch end": {}, "review": {}}
    for ev in verdict_evs:
        d = ev["data"]
        if d.get("synthetic"):
            continue
        rv = d.get("review") if isinstance(d.get("review"), dict) else {}
        for what, value in (("launch end", d.get("launch_end")), ("review", rv.get("id"))):
            if value is not None:
                cited[what].setdefault(value, []).append(ev["id"])
    for what, seen in cited.items():
        for value, evs in sorted(seen.items(), key=lambda kv: str(kv[0])):
            if len(evs) > 1:
                problems.append("verdict events %s cite one %s" % (
                    ", ".join(str(i) for i in evs), what))
    for ev in verdict_evs:
        d = ev["data"]
        if d.get("synthetic"):
            continue
        sess = d.get("session")
        want = "vs-%s-%s-r%s-t%s" % (vf, d.get("verifier"), d.get("round"), d.get("try"))
        key = "%s/%s/r%s/t%s" % (vf, d.get("verifier"), d.get("round"), d.get("try"))
        a = assigns.get(sess)
        if sess != want or a is None or a["id"] > ev["id"]:
            problems.append("verdict event %d was not made by a session the driver minted" % ev["id"])
            continue
        if a["data"].get("binary") != open_ev["data"].get("binary") \
                or a["data"].get("binary_sha256") != open_ev["data"].get("binary_sha256"):
            problems.append("assign event %d names another runtime binary than the open event"
                            % a["id"])
        if sess in ids_of["sessions"] or a["data"].get("worker") in ids_of["workers"]:
            problems.append("verdict event %d came from a session of the work" % ev["id"])
        rv = d.get("review") if isinstance(d.get("review"), dict) else {}
        rev = reviews.get(rv.get("id"))
        if rev is None or rev["data"].get("sha256") != rv.get("sha256") \
                or not kept_intact(root, rev["data"].get("path"), rev["data"].get("sha256")):
            problems.append("verdict event %d names no intact kept review" % ev["id"])
            continue
        le = event_by_id(conn, d.get("launch_end"))
        if le is None or le["kind"] != KIND_LAUNCH_END \
                or le["data"].get("stream_sha256") != d.get("stream_sha256"):
            problems.append("verdict event %d names no launch end with its stream" % ev["id"])
            continue
        if le["data"].get("key") != key or le["data"].get("session") != sess:
            # section 8.3, step 5: a research-launch-end *of its try*
            problems.append("verdict event %d names the launch end of another try" % ev["id"])
            continue
        ctx = kept_context(root, rev["data"].get("path"))
        if ctx.get("verification") != vf or ctx.get("slot") != d.get("verifier") \
                or ctx.get("round") != d.get("round") or ctx.get("try") != d.get("try"):
            # section 6.8: the kept review names this verification, slot, round and try
            problems.append("the kept review of verdict event %d names another try" % ev["id"])
            continue
        if ctx.get("launch_end") != d.get("launch_end") \
                or ctx.get("stream_sha256") != d.get("stream_sha256") \
                or ctx.get("verdict_sha256") != d.get("verdict_sha256"):
            problems.append("the kept review of verdict event %d names another launch" % ev["id"])
    return problems


# --------------------------------------------------------------------------- state and check
def _seal_tree_missing(conn, ident, seal_event, sc) -> bool:
    if sc == "engine" and role_of(conn, ident) in ("attempt", "repair"):
        return False
    data = _load(seal_event.get("data"))
    return not isinstance(data.get("tree"), dict)


def state(conn, root, ident) -> dict:
    """{"state", "scope", "verification", "round", "cap", "disputed", "outcome", "code", "lost",
    "dropped", "failed", "earlier", "seal_event", "claims"}: the verify state of the task's
    latest seal, one of STATES (section 10.2). `claims` is the open event's claim list, None
    before the open."""
    out = {"state": None, "scope": None, "verification": None, "round": None, "cap": ROUND_CAP,
           "disputed": [], "outcome": None, "code": None, "lost": [], "dropped": [],
           "failed": [], "earlier": [], "seal_event": None, "claims": None}
    all_seals = seals(conn, ident)
    if not all_seals:
        sc = scope_at_seal(conn, root, ident)
        out.update(state="not-in-scope" if sc is None else "not-sealed", scope=sc)
        return out
    seal_ev = all_seals[-1]
    out["seal_event"] = seal_ev["id"]
    sc = scope(conn, root, ident, seal_ev)
    out["scope"] = sc
    if sc is None:
        out["state"] = "not-in-scope"
        return out
    vf = verification_id(ident, seal_ev["id"])
    out["verification"] = vf
    bound = policy(root)["reverify_max"]
    runs = refuted_runs(conn, ident, before=seal_ev["id"])
    out["earlier"] = earlier(conn, ident, before=seal_ev["id"])
    opened = events(conn, KIND_OPEN, ident, verification=vf)
    open_ev = opened[0] if opened else None
    if open_ev:
        facts = verification_facts(conn, ident, vf)
        out.update(lost=facts["lost"], dropped=facts["dropped"], failed=facts["failed"],
                   claims=open_ev["data"].get("claims"))
        rounds = events(conn, KIND_ROUND, ident, verification=vf)
        ext = events(conn, KIND_EXTEND, ident, verification=vf)
        out["cap"] = HARD_CAP if ext else ROUND_CAP
        out["round"] = (rounds[-1]["data"].get("round") + 1) if rounds else 0
        out["disputed"] = list(rounds[-1]["data"].get("disputed") or []) if rounds else []
    if len(runs) > bound:
        out.update(state="exhausted", code="VERIFY-EXHAUSTED")
        return out
    ends = events(conn, KIND_END, ident, verification=vf)
    if ends:
        end = ends[-1]
        out.update(state=end["data"].get("outcome"), outcome=end["data"].get("outcome"),
                   round=end["data"].get("rounds"))
        return out
    if open_ev is None:
        out["state"] = "required"
        return out
    facts = verification_facts(conn, ident, vf)
    if facts["stop"] is not None:
        out.update(state="stopped", code=facts["stop"]["data"].get("code"))
        return out
    # the project budget posture never pauses a verification: a verifier launch is never held
    # by the budget check (t-297, in-session-runtime.md section 21)
    out["state"] = "running"
    return out


def _pending_text(st):
    return ("verification pending: %s for seal event %s is in round %s of %s"
            % (st["verification"], st["seal_event"], st["round"] or 0, st["cap"]))


def _in_seal(stop) -> bool:
    """True when a VERIFY-TREE stop's cause lives in the seal: its first problem carries
    SEALED_RULE, or, for a stop recorded before the rule existed, its detail is one the driver
    writes only for a sealed patch or a kept or sealed file."""
    probs = (stop or {}).get("data", {}).get("problems") or []
    first = probs[0] if probs else None
    if isinstance(first, dict):
        if first.get("rule") == SEALED_RULE:
            return True
        return first.get("rule") is None and str(first.get("detail") or "").startswith(
            _SEALED_DETAILS)
    return isinstance(first, str) and first.startswith(_SEALED_DETAILS)


def _stop_detail(conn, ident, st) -> str:
    facts = verification_facts(conn, ident, st["verification"])
    stop = facts["stop"]
    code = st.get("code")
    detail = ""
    if stop is not None:
        probs = stop["data"].get("problems") or []
        first = probs[0] if probs else None
        if isinstance(first, dict):
            detail = first.get("detail") or ""
        elif isinstance(first, str):
            detail = first
    tail = ""
    if code in ("VERIFY-STEERING", "VERIFY-NO-COMMAND", "VERIFY-NO-CLAIMS", "VERIFY-TOO-MANY-CLAIMS"):
        tail = "; seal a report without it"
    elif code == "VERIFY-RUNTIME-MISMATCH":
        detail = "the runtime binary pinned at open is gone or changed"
        tail = RESEAL_TAIL % ident
    elif code == "VERIFY-TREE" and _in_seal(stop):
        tail = RESEAL_TAIL % ident
    return "verification stopped: %s for seal event %s stopped with %s (%s)%s" % (
        st["verification"], st["seal_event"], code, detail or "see alpaca proof verify --status",
        tail)


def check(conn, root, ident, seal_event: dict) -> tuple:
    """(ok, problems): whether the two-agent verdict lets `ident` close on `seal_event` (section
    8.3). Out of scope: (True, []). In scope, only a `verified` end for this seal that the
    record holds together with passes. Never raises: a record it cannot read refuses."""
    try:
        return _check(conn, root, ident, seal_event)
    except Exception as exc:          # a read that fails closes nothing
        return False, ["verification inconsistent: the verification record of %s could not be "
                       "read (%s)" % (ident, type(exc).__name__)]


def _check(conn, root, ident, seal_event):
    seal_event = dict(seal_event or {})
    seal_event["data"] = _load(seal_event.get("data"))
    sc = scope(conn, root, ident, seal_event)
    if sc is None:
        return True, []
    sid = seal_event.get("id")
    vf = verification_id(ident, sid)
    pol = policy(root)
    runs = refuted_runs(conn, ident, before=sid)
    if len(runs) > pol["reverify_max"]:
        listed = "; ".join("%s %s" % (r["verification"],
                                      ("refuted %s" % ", ".join(r["refuted"])) if r["refuted"]
                                      else "%s, %s" % (r["outcome"] or "no end",
                                                       ", ".join(r["tries"][:2]) or ", ".join(r["why"])))
                           for r in runs)
        return False, ["verification exhausted: %s has %d earlier verifications that did not end "
                       "verified and hold a refutation, a lost, failed or pending try, or a dropped "
                       "refutation (%s), over the bound of %d (proof_verify.reverify_max), so seal "
                       "event %s cannot pass; the owner, or an agent at L5 or L6 with its diff "
                       "recorded, can raise the bound" % (ident, len(runs), listed,
                                                          pol["reverify_max"], sid)]
    label = sc
    if sc == "seal":
        label = seal_event["data"].get("verify_scope") or "seal"
    if label == "policy:malformed":
        label = "policy:malformed: %s" % (pol["error"] or "proof_verify is malformed")
    ends = events(conn, KIND_END, ident, verification=vf)
    opened = events(conn, KIND_OPEN, ident, verification=vf)
    if not ends:
        if not opened:
            if _seal_tree_missing(conn, ident, seal_event, sc):
                return False, ["verification required: %s is in the two-agent verification scope "
                               "(%s) and seal event %s records no tree; seal it again with alpaca "
                               "proof seal %s --verify" % (ident, label, sid, ident)]
            return False, ["verification required: %s is in the two-agent verification scope (%s) "
                           "and seal event %s has not been verified" % (ident, label, sid)]
        st = state(conn, root, ident)
        if st["state"] == "stopped":
            return False, [_stop_detail(conn, ident, st)]
        return False, [_pending_text(st)]
    end = ends[-1]
    word = end["data"].get("outcome")
    if word == "refuted":
        return False, ["verification refuted: %s refuted claim(s) %s of seal event %s"
                       % (vf, ", ".join(end["data"].get("refuted") or []) or "(none listed)", sid)]
    if word == "unverified":
        return False, ["verification unverified: %s could not verify claim(s) %s of seal event %s"
                       % (vf, ", ".join(end["data"].get("unverified") or []) or "(none listed)",
                          sid)]
    if word == "superseded":
        return False, ["verification required: %s is in the two-agent verification scope (%s) "
                       "and seal event %s has not been verified (%s was superseded)"
                       % (ident, label, sid, vf)]
    if word != "verified" or not opened:
        return False, ["verification inconsistent: %s ends %s on the record" % (vf, word)]
    problems = consistency(conn, root, ident, seal_event, opened[0], end)
    if problems:
        got = _recompute(conn, vf)["outcome"]
        return False, ["verification inconsistent: %s ends verified on the record, and %s"
                       % (vf, problems[0] if "fold" in problems[0]
                          else "%s (its verdict events fold to %s)" % (problems[0], got))] + \
            ["verification inconsistent: %s" % p for p in problems[1:]]
    return True, []


def next_steps(ident, problem: str) -> list:
    """The three steps a refused done move prints for a verification problem (section 8.4). A
    stop whose cause lives in the seal (a SEAL_STOPS code, or a VERIFY-TREE stop that
    `_stop_detail` gave RESEAL_TAIL from the recorded SEALED_RULE) gets the re-seal steps."""
    p = str(problem or "")
    done = "alpaca task move %s done --proof local:<report> once it ends verified" % ident
    if p.startswith("verification exhausted"):
        return ["alpaca proof verify %s --status (lists the earlier verifications, why each counts "
                "and the claims they refuted; one listed with why pending counts until alpaca "
                "proof verify %s recovers its tries)" % (ident, ident),
                "fix the work or the report, then alpaca proof seal %s" % ident,
                "ask the owner to raise proof_verify.reverify_max in project.yaml, then alpaca "
                "proof verify %s" % ident]
    if p.startswith("verification refuted") or p.startswith("verification unverified"):
        return ["alpaca proof verify %s --status" % ident,
                "fix the work or the report, then alpaca proof seal %s" % ident,
                "alpaca proof verify %s" % ident]
    if "records no tree" in p:
        return ["alpaca proof seal %s --verify" % ident, "alpaca proof verify %s" % ident, done]
    if p.startswith("verification stopped") and (
            any(c in p for c in SEAL_STOPS) or RESEAL_TAIL % ident in p
            or any("VERIFY-TREE (" + d in p for d in _SEALED_DETAILS)):
        return ["fix the cause in the report or the work (nothing to fix for the runtime binary), "
                "then alpaca proof seal %s" % ident, "alpaca proof verify %s" % ident, done]
    return ["alpaca proof verify %s (starts or resumes both verifiers; the loop runner does this "
            "for an engine task; after a stop, fix the cause the problem names first; a held launch "
            "goes on once its cause is cleared)" % ident,
            "alpaca proof verify %s --status" % ident, done]
