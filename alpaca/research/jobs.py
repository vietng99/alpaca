"""The job protocol of the session runtime: the job fold, the hand-out, the claim, the judge and
its nineteen checks, expiry and reconciliation (design/research-engine/in-session-runtime.md
sections 3 to 6 and 10).

A job is one agent launch, as a record: the launch spec of today plus a nonce, a model and a
deadline. The agent that does it is a subagent of the driving session. The driver files a CLAIM
that an agent finished a job; a claim decides nothing. The JUDGE (the process that owns the launch
slots of a scope: the runner for a run, the `alpaca proof verify` verb process for a verification
outside a run) checks the claim against the runtime's own witnesses (the SubagentStart and
SubagentStop events, the observability source row, the transcript, the pool lines the PreToolUse
hook wrote) and only then writes the rows that end the launch. No verb and no process that only
calls a verb can write a launch end (section 2.4).

This module imports none of the modules that decide an attempt (accept, gates, fingerprint,
proofs, the verify gate's writers, the round merge): no function reachable from `submit`,
`next-job` or the judge appends the kinds those modules own (section 12.4). The test that proves it
reads this file's imports and the event kinds it emits.
"""
from __future__ import annotations

import datetime
import glob
import hashlib
import json
import os
import re
import secrets
import shutil
import time
import threading

from alpaca import db, util
from alpaca.gates import verdict as vc
from alpaca.research import agent_marker
from alpaca.research import agent_transcript as at
from alpaca.research import errors, events, paths, session_engine as eng

#: the job protocol kinds: nothing else may be written for an attempt while its job is open (K18)
PROTOCOL_KINDS = (events.JOB, events.JOB_HANDED, events.JOB_CLAIM, events.JOB_RESULT,
                  events.JOB_REFUSED, events.JOB_EXPIRED, events.JOB_LATE)
JOB_SCHEMA = "alpaca-research-job/1"
#: the two plain agent types a job may run on (K4): a named type loads a system prompt that never
#: appears in the transcript
PLAIN_TYPES = ("general-purpose", "workflow-subagent")
#: seconds a handed job lives past its timeout (the deadline), and a job may wait unhanded
GRACE_S = 300
HANDOFF_S = 1800
#: how long the judge waits after the stop event for processes of the job to end (K17)
STRAGGLER_WAIT_S = 10.0
#: the precision of a process's start time as K17 reads it (/proc/<pid>/stat counts 10 ms clock
#: ticks and /proc/uptime hundredths of a second, measured error up to 10 ms either way): a process
#: that started this close to the hand cannot be ordered against it, so K17 spares it and lists it
START_SLACK_S = 0.05
#: seconds a transcript with no subagent-stop row must have been quiet before its agent is priced
STOP_IDLE_S = 30.0
#: the most bytes a job's prompt file may hold (the Read cap below is what the limit protects)
MAX_PROMPT_BYTES = 32000
#: the Read tool answers a file past this many tokens with a PARTIAL view (measured 25000, banner
#: `(<n> tokens, cap 25000)`), and the densest text on this host costs 2.22 bytes a token
READ_CAP_TOKENS = 25000
#: the issue keeps the estimate of the prompt's Read cost under this share of the cap
READ_BUDGET_TOKENS = 15000
#: the estimate's bytes a token: below the worst measured (2.22), so the estimate errs high
BYTES_PER_TOKEN = 2.0
#: pool gaps tolerated: max(3, 3 percent of the calls) (K12)
POOL_GAP_FLOOR, POOL_GAP_RATE = 3, 0.03
#: hook bodies longer than this are refused (K8)
HOOK_BODY_MAX = 8 * 1024
#: the most 40-character lines of a sibling's text a transcript may share (K16)
SIBLING_LINE = 40


class Problem(Exception):
    """A failed check: the reason code, what was found, whether the claim may be repeated."""

    def __init__(self, code: str, detail: str, *, retriable: bool = False, extra: dict | None = None):
        super().__init__("%s: %s" % (code, detail))
        self.code, self.detail, self.retriable = code, detail, retriable
        self.extra = dict(extra or {})


# ================================================================ record reads
def read_tokens(text: str) -> int:
    """An upper estimate of what the Read tool counts for the prompt file: its bytes plus the
    line-number prefix of every line (a tab and up to six digits), at BYTES_PER_TOKEN."""
    raw = text.encode("utf-8")
    return int((len(raw) + 7 * (text.count("\n") + 1)) / BYTES_PER_TOKEN) + 1


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _iso(when: datetime.datetime | None = None) -> str:
    return (when or _now()).astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def parse_time(text):
    return at.parse_time(text)


def _parse(row) -> dict:
    try:
        data = json.loads(row["data"] or "{}")
    except (TypeError, ValueError):
        data = {}
    return {"id": row["id"], "ts": row["ts"], "kind": row["kind"], "ref": row["ref"],
            "op": row["op"], "session": row["session"], "data": data}


def events_of(conn, scope: str, kinds=None, *, job: str | None = None, after: int = 0) -> list:
    """The events of a scope, oldest first: the research events whose data names `scope`, of the
    given kinds, optionally of one job."""
    scope_match = "CASE WHEN json_valid(data) THEN json_extract(data, '$.scope') ELSE NULL END=?"
    sql = "SELECT * FROM events WHERE " + scope_match + " AND id>?"
    invalid = "SELECT * FROM events WHERE NOT json_valid(data) AND json_extract(data, '$.scope')=? AND id>?"
    params: list = [scope, int(after)]
    bad_params: list = list(params)
    if kinds:
        marks = ",".join("?" for _ in kinds)
        sql += " AND kind IN (%s)" % marks
        # The kind test sits in a CASE ahead of the JSON read, as +kind: the planner then takes the
        # malformed-JSON partial index (with kind IN it walked every event of the seven job kinds, about
        # 90 ms a call, to find none) and a malformed row of another kind is still never read.
        invalid = ("SELECT * FROM events WHERE NOT json_valid(data) AND CASE WHEN +kind IN (%s)"
                   " THEN json_extract(data, '$.scope') END=? AND id>?" % marks)
        params += list(kinds)
        bad_params = list(kinds) + [scope, int(after)]
    if job is not None:
        sql += " AND json_extract(data, '$.job')=?"
        invalid += " AND json_extract(data, '$.job')=?"
        params.append(job)
        bad_params.append(job)
    # Both branches share one read snapshot. The invalid branch visits only the
    # indexed malformed subset and raises SQLite's original JSON error there.
    return [_parse(r) for r in conn.execute(sql + " UNION ALL " + invalid + " ORDER BY id",
                                          params + bad_params).fetchall()]


def find_job_event(conn, job: str) -> dict | None:
    """The research-job event whose data.job is `job`: the scope of a job is found from it."""
    row = conn.execute("SELECT * FROM events WHERE kind=? AND json_extract(data, '$.job')=? "
                       "ORDER BY id LIMIT 1", (events.JOB, job)).fetchone()
    return _parse(row) if row else None


def scope_dir(root: str, scope: str) -> str:
    """The folder that holds a scope's pin, ledgers and markers: the run's folder, or the folder
    of a verification's jobs."""
    if paths.RUN_ID.fullmatch(str(scope)):
        return paths.run_dir(root, scope)
    return os.path.join(paths.research_dir(root), "jobs", paths.check_name(scope))


def read_job(root: str, scope: str, job: str) -> dict | None:
    return eng.read_json(os.path.join(paths.job_dir(root, scope, job), "job.json"))


#: the fields of job.json that decide what an agent may do and what it was told. The research-job
#: event holds the same values and is append-only; job.json is a file the driver or an agent could
#: rewrite, so the judge and the guard take these from the event (adversarial check round 1, B2)
EVENT_FIELDS = ("role", "prompt_path", "prompt_sha256", "spawn_prompt_sha256", "allowed_tools",
                "read_paths", "write_paths", "scratch", "cwd", "shell_cwd", "scanned_by")


def event_backed(job: dict | None, event: dict | None) -> dict:
    """`job` (the content of job.json) with every deciding field taken from the research-job
    event data. A job.json whose spawn prompt no longer hashes to the event's spawn_prompt_sha256
    gets `spawn_prompt` None and `spawn_prompt_edited` True: the first-turn check refuses it."""
    out = dict(job or {})
    event = event or {}
    for key in EVENT_FIELDS:
        if key in event:
            out[key] = event[key]
    # the workdir is not in the event: it counts only where an event-backed path covers it
    roots = [out.get(k) for k in ("cwd",)] + list(out.get("read_paths") or []) \
        + list(out.get("write_paths") or [])
    wd = out.get("workdir")
    if wd and not any(r and eng.under(os.path.realpath(wd), os.path.realpath(r)) for r in roots):
        out["workdir"] = out.get("cwd")
    want = event.get("spawn_prompt_sha256")
    if want and hashlib.sha256(str(out.get("spawn_prompt") or "").encode("utf-8")).hexdigest() != want:
        out["spawn_prompt"] = None
        out["spawn_prompt_edited"] = True
    return out


def drivers(conn, scope: str) -> list:
    """The research-driver events of a scope (ref the scope), oldest first."""
    rows = conn.execute("SELECT * FROM events WHERE kind=? AND ref=? ORDER BY id",
                        (events.DRIVER, scope)).fetchall()
    return [_parse(r) for r in rows]


def newest_driver(conn, scope: str) -> dict | None:
    got = drivers(conn, scope)
    return got[-1] if got else None


def newest_pin(conn, scope: str, run: str | None = None) -> dict | None:
    """The newest pin-carrying event of a run or verification: a research-driver or a
    research-repin, whichever has the greater id (12.5, mechanism 5)."""
    best = None
    for ev in drivers(conn, scope):
        best = ev
    if run:
        for row in conn.execute("SELECT * FROM events WHERE kind=? AND ref=? ORDER BY id",
                                (events.REPIN, run)).fetchall():
            ev = _parse(row)
            if best is None or ev["id"] > best["id"]:
                best = ev
    return best


def pin_of(event: dict | None) -> dict | None:
    if not event:
        return None
    return eng.read_json((event["data"] or {}).get("pin_file") or "")


# ================================================================ the fold
def fold(conn, scope: str) -> dict:
    """job id -> {"job" (the research-job data), "event" (its id), "handed" (the newest handed data
    or None), "handed_id", "claims" ([data]), "result", "refused", "expired", "late", "state"}.
    The state is one of types.JOB_STATES: issued, handed, claimed while open, and accepted,
    refused or expired once it ended."""
    jobs: dict = {}
    for ev in events_of(conn, scope, PROTOCOL_KINDS):
        d = ev["data"]
        jid = d.get("job")
        if not jid:
            continue
        j = jobs.setdefault(jid, {"job": None, "event": None, "handed": None, "handed_id": None,
                                  "claims": [], "result": None, "refused": None, "expired": None,
                                  "late": [], "state": "issued", "refusals": 0})
        k = ev["kind"]
        if k == events.JOB:
            j["job"], j["event"] = d, ev["id"]
        elif k == events.JOB_HANDED and j["handed"] is None:
            j["handed"], j["handed_id"] = d, ev["id"]
        elif k == events.JOB_CLAIM:
            j["claims"].append(dict(d, _id=ev["id"]))
        elif k == events.JOB_RESULT:
            j["result"] = dict(d, _id=ev["id"])
        elif k == events.JOB_REFUSED:
            j["refused"] = dict(d, _id=ev["id"])
        elif k == events.JOB_EXPIRED:
            j["expired"] = dict(d, _id=ev["id"])
        elif k == events.JOB_LATE:
            j["late"].append(dict(d, _id=ev["id"]))
    for j in jobs.values():
        if j["result"] is not None:
            j["state"] = "accepted"
        elif j["refused"] is not None:
            j["state"] = "refused"
        elif j["expired"] is not None:
            j["state"] = "expired"
        elif j["claims"] and j["handed"] is not None:
            j["state"] = "claimed"
        elif j["handed"] is not None:
            j["state"] = "handed"
    return {k: v for k, v in jobs.items() if v["job"] is not None}


def job_expired(conn, job_id: str, scope: str) -> bool:
    """True when the record holds a research-job-expired event of `job_id` in `scope`. Read
    from the append-only record, never from the agent cache or job.json; the guard denies every
    call of such a job (t-351 F47, in-session-runtime.md section 10)."""
    if not job_id or not scope:
        return False
    return bool(events_of(conn, str(scope), (events.JOB_EXPIRED,), job=str(job_id)))


def job_ended(conn, job_id: str, scope: str) -> str | None:
    """`accepted`, `refused` or `expired` when the record holds a research-job-result, -refused or
    -expired event of `job_id` in `scope`, in the precedence `fold` gives the states; None while
    the job is open. Read from the append-only record, never from the agent cache or job.json;
    the guard denies every call of an ended job (t-357, in-session-runtime.md section 28.5)."""
    if not job_id or not scope:
        return None
    kinds = {ev["kind"] for ev in events_of(conn, str(scope), (
        events.JOB_RESULT, events.JOB_REFUSED, events.JOB_EXPIRED), job=str(job_id))}
    for kind, state in ((events.JOB_RESULT, "accepted"), (events.JOB_REFUSED, "refused"),
                        (events.JOB_EXPIRED, "expired")):
        if kind in kinds:
            return state
    return None


def is_open(j: dict) -> bool:
    return j["state"] in ("issued", "handed", "claimed")


def open_jobs(conn, scope: str) -> dict:
    return {k: v for k, v in fold(conn, scope).items() if is_open(v)}


def counts(conn, scope: str) -> dict:
    """{issued, handed, claimed, waiting_slots, oldest_handed_age_s} of the open jobs."""
    out = {"issued": 0, "handed": 0, "claimed": 0, "waiting_slots": 0, "oldest_handed_age_s": None}
    now = _now()
    oldest = None
    for j in open_jobs(conn, scope).values():
        out[j["state"]] = out.get(j["state"], 0) + 1
        out["waiting_slots"] += 1
        if j["handed"] and j["state"] in ("handed", "claimed"):
            age = (now - (parse_time(j["handed"].get("handed_at")) or now)).total_seconds()
            oldest = age if oldest is None else max(oldest, age)
    out["oldest_handed_age_s"] = None if oldest is None else round(oldest, 1)
    return out


def oldest_handed_at(conn, scope: str) -> datetime.datetime | None:
    """When the oldest handed or claimed open job of `scope` was handed: `counts` ages it by the clock
    (op-012 t-593: the research view keeps this time and ages it again on every read)."""
    times = [parse_time(j["handed"].get("handed_at")) for j in open_jobs(conn, scope).values()
             if j["handed"] and j["state"] in ("handed", "claimed")]
    times = [t for t in times if t is not None]
    return min(times) if times else None


def refusal_counts(conn, scope: str) -> dict:
    out: dict = {}
    for ev in events_of(conn, scope, (events.JOB_REFUSED,)):
        code = ev["data"].get("code")
        if code:
            out[code] = out.get(code, 0) + 1
    return out


def deadline_of(j: dict) -> datetime.datetime | None:
    return parse_time((j.get("handed") or {}).get("deadline"))


# ================================================================ the marker of an engine change
def marker_path(root: str, scope: str) -> str:
    return os.path.join(scope_dir(root, scope), "engine-changed.json")


def write_marker(root: str, scope: str, *, kind: str, changed: list, pin_event, job: str | None = None,
                 ledger_event=None, **more) -> dict:
    """The first sign of a difference makes the checking process write engine-changed.json (paths,
    the time, `kind` engine, context or context-unlisted, and the id of the pin-carrying event it
    was checked against, so the owner's repin makes it stale)."""
    path = marker_path(root, scope)
    existing = eng.read_json(path)
    if isinstance(existing, dict) and existing.get("pin_event") == pin_event:
        return existing
    body = {"schema": "alpaca-engine-changed/1", "scope": scope, "kind": kind,
            "changed": changed[:eng.MAX_CHANGED], "changed_count": len(changed),
            "pin_event": pin_event, "ledger_event": ledger_event, "job": job, "at": _iso()}
    noted = read_metadata_note(root, scope)
    if noted:
        # the metadata-only changes seen in this run so far, which ended nothing (section 24.1)
        body["metadata"] = noted[:eng.MAX_CHANGED]
    body.update(more)
    eng._write_json(path, body)
    return body


# ================================================================ metadata-only changes (t-360)
def metadata_note_path(root: str, scope: str) -> str:
    return os.path.join(scope_dir(root, scope), "metadata-changed.json")


#: the most paths the note keeps; past it the note says `truncated`
MAX_METADATA_NOTED = 500


def note_metadata(root: str, scope: str, entries: list, *, point: str, group: str) -> None:
    """Record the metadata-only changes of member files (a new uid, gid or mode with the same
    bytes, size, inode and mtime; `session_engine.metadata_only`, orchestrator ruling
    dec-464755c60ab4, section 24.1) in `<run dir>/metadata-changed.json`. They end nothing; the
    note names them for the owner, `research status` reads it and `write_marker` copies it into
    `engine-changed.json` when the run ends for another change. No gate reads the note. A path
    is noted once per new uid, gid and mode, so a repeated look does not rewrite the file."""
    if not entries:
        return
    path = metadata_note_path(root, scope)
    body = eng.read_json(path)
    if not isinstance(body, dict) or not isinstance(body.get("entries"), dict):
        body = {"schema": "alpaca-metadata-changed/1", "scope": scope, "entries": {}, "count": 0,
                "truncated": False}
    got, dirty = body["entries"], False
    for e in entries:
        p = e.get("path")
        if not p:
            continue
        have = got.get(p)
        if have is not None and have.get("new") == e.get("new"):
            continue
        if have is None and len(got) >= MAX_METADATA_NOTED:
            if not body.get("truncated"):
                body["truncated"] = True  # more paths than the note keeps
                dirty = True
            continue
        got[p] = dict(e, group=group, point=point, at=_iso(),
                      first_at=(have or {}).get("first_at") or _iso())
        dirty = True
    if dirty:
        body["count"] = len(got)
        eng._write_json(path, body)


def read_metadata_note(root: str, scope: str) -> list:
    """The noted metadata-only changes, oldest first: [{path, moved, old, new, group, point, at,
    first_at, ...}]."""
    body = eng.read_json(metadata_note_path(root, scope))
    if not isinstance(body, dict) or not isinstance(body.get("entries"), dict):
        return []
    return sorted(body["entries"].values(), key=lambda e: (e.get("first_at") or "", e.get("path") or ""))


def read_marker(root: str, scope: str, pin_event=None) -> dict | None:
    """The marker, or None when absent or stale: a marker whose pin_event is not the newest
    pin-carrying event's was written against an older pin (the owner's repin superseded it)."""
    got = eng.read_json(marker_path(root, scope))
    if not isinstance(got, dict):
        return None
    if pin_event is not None and got.get("pin_event") != pin_event:
        return None
    return got


# ================================================================ the claim (the verb side)
def _claim_args(args: dict) -> dict:
    keep = ("nonce", "agent_id", "transcript", "final", "usage", "tool", "tool_use_id")
    out = {k: args.get(k) for k in keep}
    if args.get("diagnose"):
        out["diagnose"] = True          # the judge evaluates every check (run_checks_all)
    return out


def claim(conn, root: str, job_id: str, args: dict, *, caller_session: str | None,
          in_subagent: bool, session: str) -> dict:
    """File a claim: the cheap checks that need no witness (the job exists and is open and
    handed, the caller is the driver, the nonce matches; a subagent's own call never gets here, the
    guard denies it, and `in_subagent` is for a caller that knows), then the research-job-claim
    row and the claim file. Returns {"n", "job", "scope", "new", "reply"} where
    reply is a final reply already on disk for the same arguments. Raises Problem. It writes no
    launch end, no result row and nothing the plan reads (section 12.4)."""
    ev = find_job_event(conn, job_id)
    if ev is None:
        raise Problem("JOB-UNKNOWN", "no research-job names %s" % job_id)
    scope = ev["data"]["scope"]
    drv = newest_driver(conn, scope)
    if drv is None or (caller_session is not None and drv["data"].get("session") != caller_session):
        raise Problem("JOB-NOT-DRIVER", "only the session that drives %s may submit its jobs" % scope)
    if in_subagent:
        raise Problem("JOB-IN-SUBAGENT", "a subagent's shell cannot submit a job")
    j = fold(conn, scope).get(job_id)
    if j is None or not is_open(j):
        if j is not None:
            # the same call repeated after the judge answered (a submit that answered
            # JOB-GROUP-WAIT or JOB-PENDING, then the job ended): the call gets its claim's reply
            # and files nothing (fix batch 3, F18)
            nonce = str(args.get("nonce") or "")
            if hashlib.sha256(nonce.encode("utf-8")).hexdigest() == j["job"].get("nonce_sha256"):
                want = _event_args(_claim_args(args))
                for c in reversed(j["claims"]):
                    if all(c.get(k) == v for k, v in want.items()):
                        reply = _read_reply(root, scope, job_id, c["n"])
                        if reply is not None and not reply.get("retriable"):
                            return {"n": c["n"], "job": job_id, "scope": scope, "new": False,
                                    "reply": reply}
                        break
        raise Problem("JOB-STATE", "job %s is %s" % (job_id, j["state"] if j else "unknown"))
    if j["handed"] is None:
        raise Problem("JOB-STATE", "job %s was never handed out" % job_id)
    job = read_job(root, scope, job_id) or {}
    nonce = str(args.get("nonce") or "")
    if hashlib.sha256(nonce.encode("utf-8")).hexdigest() != j["job"].get("nonce_sha256"):
        raise Problem("JOB-NONCE", "the nonce is not job %s's" % job_id)
    clean = _claim_args(args)
    folder = paths.job_dir(root, scope, job_id)
    cdir, rdir = os.path.join(folder, "claims"), os.path.join(folder, "replies")
    os.makedirs(cdir, exist_ok=True)
    os.makedirs(rdir, exist_ok=True)
    safe = dict(clean)
    safe["nonce_sha256"] = hashlib.sha256(nonce.encode("utf-8")).hexdigest()
    safe.pop("nonce", None)
    same = None
    for c in j["claims"]:
        if all(c.get(k) == v for k, v in _event_args(clean).items()):
            same = c
    if same is not None:
        n = same["n"]
        reply = _read_reply(root, scope, job_id, n)
        if reply is not None and reply.get("retriable"):
            same = None
        else:
            return {"n": n, "job": job_id, "scope": scope, "new": False, "reply": reply}
    n = max([c["n"] for c in j["claims"]] + [0]) + 1
    body = dict(clean, n=n, job=job_id, scope=scope, filed_at=_iso(), nonce=None)
    final_sha = None
    if clean.get("final"):
        try:
            with open(clean["final"], "rb") as fh:
                final_sha = hashlib.sha256(fh.read()).hexdigest()
        except OSError:
            final_sha = None
    body["final_sha256"] = final_sha
    eng._write_json(os.path.join(cdir, "%d.json" % n), body)
    claim_data = {"job": job_id, "scope": scope, "n": n, "agent_id": clean.get("agent_id"),
                  "transcript": clean.get("transcript"), "final": clean.get("final"),
                  "final_sha256": final_sha, "claimed_usage": clean.get("usage"),
                  "usage": clean.get("usage"), "tool": clean.get("tool"),
                  "tool_use_id": clean.get("tool_use_id")}
    if clean.get("diagnose"):
        claim_data["diagnose"] = True
    events.emit(conn, events.JOB_CLAIM, run=ev["data"].get("run"), ref=ev["ref"], op=ev["op"],
                session=session, actor="operator", data=claim_data)
    return {"n": n, "job": job_id, "scope": scope, "new": True, "reply": None}


def unclaimed_siblings(conn, scope: str, job_id: str, *, root: str | None = None) -> list:
    """The other open jobs of a verifier job's group that have no claim filed yet (fix batch 3,
    F18). The judge of a group member waits at the barrier for its sibling's claim (section 6.3,
    item 6), so `submit` answers JOB-GROUP-WAIT while this list is not empty instead of holding
    the driver until the sibling is claimed. With `root`, a sibling whose every claim the judge
    already answered (a retriable answer: the job is still open) counts as having none, since its
    judge waits for the next one. A job with no group, or whose group has no other open job, has
    none."""
    all_jobs = fold(conn, scope)
    j = all_jobs.get(job_id)
    if j is None:
        return []
    group = j["job"].get("group")
    if not group:
        return []
    out = []
    for jid, other in all_jobs.items():
        if jid == job_id or other["job"].get("group") != group or not is_open(other):
            continue
        if other["job"].get("verifier_slot") == j["job"].get("verifier_slot"):
            continue
        live = [c for c in other["claims"]
                if root is None or _read_reply(root, scope, jid, c["n"]) is None]
        if not live:
            out.append(jid)
    return sorted(out)


def _event_args(clean: dict) -> dict:
    return {"agent_id": clean.get("agent_id"), "transcript": clean.get("transcript"),
            "final": clean.get("final"), "usage": clean.get("usage"), "tool": clean.get("tool"),
            "tool_use_id": clean.get("tool_use_id")}


def _read_reply(root: str, scope: str, job: str, n: int) -> dict | None:
    return eng.read_json(os.path.join(paths.job_dir(root, scope, job), "replies", "%d.json" % n))


def read_reply(root: str, scope: str, job: str, n: int) -> dict | None:
    return _read_reply(root, scope, job, n)


def write_reply(root: str, scope: str, job: str, n: int, reply: dict) -> None:
    eng._write_json(os.path.join(paths.job_dir(root, scope, job), "replies", "%d.json" % n), reply)


def unjudged_claims(conn, root: str, scope: str, job: str) -> list:
    """The claims of an open job that have no reply yet, oldest first."""
    out = []
    for c in (fold(conn, scope).get(job) or {}).get("claims", []):
        if _read_reply(root, scope, job, c["n"]) is None:
            out.append(c)
    return out


# ================================================================ the hand
def group_size_file(root: str, scope: str, job: str) -> str:
    return os.path.join(paths.job_dir(root, scope, job), "group-size.json")


def lower_group_size(root: str, scope: str, job: str, size: int) -> None:
    """A sibling whose issue was refused (a budget HOLD between the two threads, a dispatch.spawn
    refusal) lowers the group_size of the job already issued, so that job is handed at once and
    not withheld until handoff_by (section 6.1)."""
    eng._write_json(group_size_file(root, scope, job), {"group_size": int(size)})


def withheld(root: str, scope: str, all_jobs: dict, j: dict) -> bool:
    """A job in a group whose members are not all issued yet is withheld, so both verifiers of a
    pass are always handed out in one response (section 6.1)."""
    d = j["job"]
    group = d.get("group")
    if not group:
        return False
    size = int(d.get("group_size") or 1)
    lowered = eng.read_json(group_size_file(root, scope, d["job"]))
    if isinstance(lowered, dict) and lowered.get("group_size"):
        size = min(size, int(lowered["group_size"]))
    members = [m for m in all_jobs.values()
               if m["job"].get("group") == group and is_open(m) and m["handed"] is None
               and int(m["job"].get("group_size") or 1) == int(d.get("group_size") or 1)]
    return len(members) < size


def pool_offset(root: str, session: str) -> int:
    from alpaca import pool
    return eng.file_size(pool.path(root, session))


def check_pin(conn, root: str, scope: str, *, run: str | None, driver_session: str | None,
              point: str, job: str | None = None) -> None:
    """K13's engine half at one point (issue, hand, judge, plan step): the stat pass over the
    pinned engine set, hook scripts, import roots and text sources against the newest pin-carrying
    event; a difference writes the marker and raises JOB-ENGINE-CHANGED. A scope with no pin (a
    test run) passes."""
    ev = newest_pin(conn, scope, run)
    if ev is None:
        return
    pin = pin_of(ev)
    if not pin:
        return
    got = eng.engine_unchanged(root, pin, driver_session=driver_session)
    note_metadata(root, scope, got.get("metadata") or [], point=point, group="S")
    if got["ok"]:
        return
    write_marker(root, scope, kind="engine", changed=got["changed"], pin_event=ev["id"], job=job,
                 point=point)
    raise Problem("JOB-ENGINE-CHANGED", "%d file(s) changed since the pin of event %d: %s" % (
        got.get("changed_count", len(got["changed"])), ev["id"],
        ", ".join(c["path"] for c in got["changed"][:5])), extra={"end_run": True, "changed":
                                                                  got["changed"]})


def check_config(conn, root: str, scope: str, drv: dict | None) -> str:
    """K13's hash half: the hash of the text sources now against the run's baseline. Returns the
    hash; raises JOB-CONFIG-CHANGED on a difference."""
    if drv is None:
        return ""
    d = drv["data"]
    now = eng.config_hash(root, d.get("session") or "", d.get("config_dir"),
                          scripts=d.get("hook_scripts"))
    base = d.get("config_base_sha256")
    if base and now != base:
        raise Problem("JOB-CONFIG-CHANGED", "the settings, CLAUDE.md, agent or hook data files "
                      "differ from the run's baseline")
    return now


#: the SessionStart sources that keep the driver's CLI process (a compaction, a clear); every other
#: source, none and an unknown word included, is a restart of the process (t-360, section 24.2)
IN_PROCESS_STARTS = ("compact", "clear")


def _start_source(data) -> str | None:
    """The `source` of a session-start event's data (canonical JSON text or a dict), or None."""
    if isinstance(data, str):
        try:
            data = json.loads(data or "{}")
        except ValueError:
            return None
    return data.get("source") if isinstance(data, dict) else None


def driver_restarted(conn, drv: dict | None) -> bool:
    """True when the record holds a restart of the driver's CLI process after the driver event
    `drv`: a session-end of the driver session, or a session-start whose source is not `compact`
    or `clear`, with an id greater than the event's (a later compaction or clear does not undo it:
    it runs inside the new process). `driver_gone` ends the run on it; `context_changes` skips the
    stored snapshot's removal only on it (t-360, section 24.2)."""
    if drv is None:
        return False
    sid = (drv.get("data") or {}).get("session")
    if not sid:
        return False
    for ev in conn.execute("SELECT kind, data FROM events WHERE session=? AND kind IN ('session-start', "
                           "'session-end') AND id>?", (sid, drv["id"])):
        if ev["kind"] == "session-end" or _start_source(ev["data"]) not in IN_PROCESS_STARTS:
            return True
    return False


def snapshot_wait(bad: list, drv: dict, state: dict) -> bool:
    """The watch's one-look grace (t-360, section 24.2): True, and the look ends nothing, when every
    change that would end the run is the stored driver snapshot's removal with no restart on the
    record (`driver_snapshot: True`) and this driver event has not had its grace yet. The CLI
    removes the file in an exit handler, and its SessionEnd row can land after the look; the next
    look then sees the restart and ends the run RUN-DRIVER-GONE. A removal that is still there at
    the next look with no restart ends the run. `state` is the watch's own dict."""
    if not bad or not all(c.get("driver_snapshot") for c in bad):
        return False
    if drv is None or state.get("driver_event") == drv["id"]:
        return False
    state["driver_event"] = drv["id"]
    return True


def context_now(conn, root: str, scope: str, drv: dict, ledger: dict, *, check_session: str | None,
                point: str | None = None, metadata: list | None = None) -> tuple:
    """(fingerprint now, changes against `ledger`) for group M. The removal of the stored driver
    snapshot is skipped only when the record holds a restart of the driver after `drv`
    (`driver_restarted`, t-360 section 24.2). A metadata-only change of a member is no change
    (section 24.1): it is appended to `metadata` when a list is given, and with a `point` it is
    noted in the run's metadata note (`note_metadata`)."""
    d = drv["data"]
    cur = eng.context_fingerprint(root, d.get("config_dir") or eng.config_dir(),
                                  ledger.get("driver_session") or d.get("session") or "",
                                  runtime=ledger.get("runtime") or "claude",
                                  transcript=ledger.get("transcript"),
                                  extra=tuple(ledger.get("extra") or ()), previous=ledger,
                                  group_s=_group_s(drv),
                                  driver_snapshot=ledger.get("driver_snapshot")
                                  or d.get("driver_snapshot"))
    meta = metadata if metadata is not None else []
    diff = eng.context_changes(ledger, cur, check_session=check_session,
                               driver_restarted=driver_restarted(conn, drv), metadata=meta)
    if point is not None:
        note_metadata(root, scope, meta, point=point, group="M")
    return cur, diff


def _group_s(drv: dict) -> set:
    pin = pin_of(drv) or {}
    out = set()
    for group in ("shared", "driver"):
        for p in ((pin.get("text") or {}).get(group) or {}):
            out.add(p)
    return out


def current_ledger_event(conn, scope: str, run: str | None) -> dict | None:
    """The newest event that carries a group M ledger: a research-driver, a research-job-handed or
    a research-repin."""
    best = None
    for kind, ref in ((events.DRIVER, scope), (events.REPIN, run)):
        if ref is None:
            continue
        for row in conn.execute("SELECT * FROM events WHERE kind=? AND ref=? ORDER BY id",
                                (kind, ref)).fetchall():
            ev = _parse(row)
            if (ev["data"].get("context") or {}).get("ledger_file"):
                best = ev
    for ev in events_of(conn, scope, (events.JOB_HANDED,)):
        if (ev["data"].get("context") or {}).get("ledger_file"):
            if best is None or ev["id"] > best["id"]:
                best = ev
    return best


def classify_changes(root: str, conn, scope: str, drv: dict, changes: list, ledger_ev: dict) -> dict:
    """Put each changed path of group M in a writer class from the pool files of the sessions
    that drove the run since the ledger point (12.6, "Who wrote a change")."""
    from alpaca import pool
    sessions = []
    d = drv["data"]
    sessions.append((pool.path(root, d.get("session") or ""), int(d.get("pool_offset") or 0)
                     if ledger_ev["kind"] == events.DRIVER else 0))
    ledger_session = ledger_ev["session"] or d.get("session")
    first = [(pool.path(root, ledger_session), int((ledger_ev["data"] or {}).get("pool_offset") or 0))]
    files = first if ledger_session != d.get("session") else [sessions[0]]
    if ledger_ev["kind"] == events.JOB_HANDED:
        files = [(pool.path(root, d.get("session") or ""),
                  int((ledger_ev["data"] or {}).get("pool_offset") or 0))]
    calls = set()
    return eng.context_writers(root, files, [c["path"] for c in changes], transcript_calls=calls)


def hand_off(conn, root: str, scope: str, session: str, *, limit: int = 8, run: str | None = None) -> dict:
    """The jobs to hand out now: those never handed whose group is whole, whole groups together,
    each with research-job-handed (the session, the deadline, the config hash, the pool offset
    and the context ledger). Raises Problem JOB-ENGINE-CHANGED or JOB-CONFIG-CHANGED before it
    hands a job (K13). Returns {"jobs": [...], "context_changes": [...]}."""
    all_jobs = fold(conn, scope)
    fresh = [j for j in all_jobs.values()
             if j["state"] == "issued" and not withheld(root, scope, all_jobs, j)]
    if not fresh:
        return {"jobs": [], "context_changes": []}
    drv = newest_driver(conn, scope)
    run = run or (fresh[0]["job"].get("run"))
    if drv is not None:
        check_pin(conn, root, scope, run=run, driver_session=session, point="hand")
    cfg_hash = check_config(conn, root, scope, drv) if drv is not None else ""
    ledger_info, changes = None, []
    lev = current_ledger_event(conn, scope, run) if drv is not None else None
    ledger = eng.load_ledger((lev["data"] or {}).get("context")) if lev else None
    if drv is not None and ledger is not None:
        cur, diff = context_now(conn, root, scope, drv, ledger, check_session=session, point="hand")
        if diff:
            classes = classify_changes(root, conn, scope, drv, diff, lev)
            bad = [c for c in diff if classes.get(os.path.normpath(c["path"])) != "driver"]
            for c in diff:
                c["class"] = classes.get(os.path.normpath(c["path"]), "unattributed")
            if bad:
                write_marker(root, scope, kind="context", changed=diff, pin_event=(
                    newest_pin(conn, scope, run) or {}).get("id"), ledger_event=lev["id"],
                    point="hand")
                raise Problem("JOB-ENGINE-CHANGED", "a file the runtime loads into an agent changed "
                              "and its writer is %s: %s" % (
                                  bad[0]["class"], ", ".join(c["path"] for c in bad[:3])),
                              extra={"end_run": True, "changed": diff})
            changes = diff
            ledger_info = eng.take_ledger(scope_dir(root, scope), cur)
        else:
            ledger_info = dict((lev["data"] or {}).get("context") or {})
    out = []
    handed_groups: set = set()
    count = 0
    for j in sorted(fresh, key=lambda m: m["event"]):
        d = j["job"]
        group = d.get("group")
        if count >= limit and not (group and group in handed_groups):
            continue
        job = read_job(root, scope, d["job"]) or {}
        now = _now()
        deadline = now + datetime.timedelta(seconds=int(d.get("timeout_s") or 0) + GRACE_S)
        data = {"job": d["job"], "scope": scope, "session": session, "handed_at": _iso(now),
                "deadline": _iso(deadline), "config_sha256": cfg_hash, "count": 1,
                "pool_offset": pool_offset(root, session)}
        if ledger_info is not None:
            data["context"] = dict(ledger_info, changes=[
                {k: c.get(k) for k in ("path", "class", "old_sha256", "new_sha256")}
                for c in changes])
        events.emit(conn, events.JOB_HANDED, run=d.get("run"), ref=j["job"].get("attempt") or scope,
                    op=None, session=session, actor="operator", data=data)
        out.append({"job": d["job"], "role": d.get("role"), "model": d.get("model"),
                    "description": d.get("description"), "spawn_prompt": job.get("spawn_prompt"),
                    "nonce": job.get("nonce"), "group": group,
                    "spawn_together": bool(group and int(d.get("group_size") or 1) > 1),
                    "deadline": data["deadline"],
                    "prompt_path": job.get("prompt_path") or d.get("prompt_path"),
                    "timeout_s": d.get("timeout_s")})
        count += 1
        if group:
            handed_groups.add(group)
    return {"jobs": out, "context_changes": [
        {k: c.get(k) for k in ("path", "class", "old_sha256", "new_sha256")} for c in changes
        if c.get("class") == "driver"]}


def expired_since(conn, root: str, scope: str) -> list:
    """Every handed job that expired since the driver's previous call: [{job, reason, agent_ids}],
    from a cursor file beside the jobs (the verb writes nothing to the record for it)."""
    cursor_path = os.path.join(paths.jobs_dir(root, scope), ".expired-reported")
    after = 0
    try:
        with open(cursor_path, encoding="utf-8") as fh:
            after = int(fh.read().strip() or 0)
    except (OSError, ValueError):
        after = 0
    out, top = [], after
    for ev in events_of(conn, scope, (events.JOB_EXPIRED,), after=after):
        top = max(top, ev["id"])
        j = fold(conn, scope).get(ev["data"].get("job"))
        if j is None or j["handed"] is None:
            continue
        out.append({"job": ev["data"]["job"], "reason": ev["data"].get("reason"),
                    "agent_ids": agents_for(conn, root, scope, j)})
    if top != after:
        os.makedirs(os.path.dirname(cursor_path), exist_ok=True)
        with open(cursor_path, "w", encoding="utf-8") as fh:
            fh.write(str(top))
    return out


def list_open(conn, scope: str) -> list:
    """What `next-job --list` prints: the id, role, state and agent id of every open job, never a
    spawn prompt."""
    out = []
    for jid, j in sorted(open_jobs(conn, scope).items()):
        agent = None
        if j["claims"]:
            agent = j["claims"][-1].get("agent_id")
        out.append({"job": jid, "role": j["job"].get("role"), "state": j["state"],
                    "agent_id": agent, "group": j["job"].get("group")})
    return out


# ================================================================ the agents of a driver
def stop_events(conn, driver_session: str, after: int = 0) -> list:
    rows = conn.execute("SELECT * FROM events WHERE kind='subagent-stop' AND session=? AND id>? "
                        "ORDER BY id", (driver_session, int(after))).fetchall()
    return [_parse(r) for r in rows]


def start_events(conn, driver_session: str, after: int = 0) -> list:
    rows = conn.execute("SELECT * FROM events WHERE kind='subagent-start' AND session=? AND id>? "
                        "ORDER BY id", (driver_session, int(after))).fetchall()
    return [_parse(r) for r in rows]


def source_row(conn, agent_id: str) -> dict | None:
    try:
        row = conn.execute("SELECT * FROM obs_source WHERE session=? AND kind='transcript' "
                           "ORDER BY updated_at DESC LIMIT 1", (agent_id,)).fetchone()
    except Exception:  # noqa: BLE001 - no observability table: no row
        return None
    return dict(row) if row else None


def locator_for(conn, agent_id: str, stop_ev: dict | None) -> str | None:
    row = source_row(conn, agent_id)
    if row and row.get("locator"):
        return row["locator"]
    if stop_ev and (stop_ev["data"] or {}).get("agent_transcript_path"):
        return stop_ev["data"]["agent_transcript_path"]
    return None


def _head(path: str, n: int = 2048) -> str:
    try:
        with open(path, "rb") as fh:
            return fh.read(n).decode("utf-8", "replace")
    except OSError:
        return ""


def find_transcript(conn, root: str, driver_session: str, agent_id: str) -> str | None:
    """The transcript of a driver's subagent that no stop row names: the file
    <driver transcript folder>/subagents/**/agent-<id>.jsonl beside the driver's registered
    transcript. A stopped agent (TaskStop) and an expired one write no subagent-stop row (probe B,
    gap-research/GAP-REPORT.md), so the file is the only place its work is recorded."""
    from alpaca import transcripts
    reg = transcripts.registered(conn, driver_session) if driver_session else ""
    if not reg:
        return None
    if not os.path.isabs(reg):
        reg = os.path.join(root, reg)
    stem = os.path.splitext(reg)[0]
    for path in glob.glob(os.path.join(glob.escape(stem), "subagents", "**",
                                       "agent-%s.jsonl" % glob.escape(str(agent_id))),
                          recursive=True)[:1]:
        return path
    return None


def agents_for(conn, root: str, scope: str, j: dict) -> list:
    """The agents of the driver registered after a job's hand whose transcript names the job in
    its first 2 KB (the lookup of reconcile): the agents the hooks saw stop, and the agents the
    hooks saw start that have no stop row, which is the whole record of a stopped or expired
    agent."""
    drv = newest_driver(conn, scope)
    if drv is None or j.get("handed_id") is None:
        return []
    out = []
    session = (j["handed"] or {}).get("session") or drv["data"].get("session")
    stopped = set()
    for ev in stop_events(conn, session, j["handed_id"]):
        aid = (ev["data"] or {}).get("agent_id")
        if aid:
            stopped.add(aid)
        loc = locator_for(conn, aid, ev) if aid else None
        if loc and "ALPACA JOB %s " % j["job"]["job"] in _head(loc):
            out.append(aid)
    for ev in start_events(conn, session, j["handed_id"]):
        aid = (ev["data"] or {}).get("agent_id")
        if not aid or aid in stopped or aid in out:
            continue
        loc = find_transcript(conn, root, session, aid)
        if loc and "ALPACA JOB %s " % j["job"]["job"] in _head(loc):
            out.append(aid)
    return out


_JOB_NAMED = re.compile(r"ALPACA JOB (\S+) NONCE")


def _known_agents(fold_: dict) -> set:
    """Every agent id the record already accounts for in a scope: the claimants of every job, the
    agent of an accepted, refused or late job, and the strays a refusal booked."""
    known = set()
    for j in fold_.values():
        for claim in j["claims"]:
            known.add(claim.get("agent_id"))
        for row in (j["result"], j["refused"]):
            if row:
                known.add(row.get("agent_id"))
                for s in row.get("strays") or []:
                    known.add((s or {}).get("agent_id"))
        for late in j["late"]:
            known.add(late.get("agent_id"))
    known.discard(None)
    return known


def extra_agents(conn, scope: str) -> list:
    """The agents of the driver that stopped in the scope's window and are beyond the jobs'
    (in-session-runtime.md section 12.2, R5): [{agent_id, stop_event, locator, job}]. The window
    of a driver session starts at its first research-driver event of the scope. An agent the record
    accounts for is not listed. `job` is the id of a job of this scope whose spawn prompt opens the
    agent's transcript and that was already accepted or refused (a stray: a twin, or an agent the
    driver spawned and did not file); None for an agent whose transcript names no job at all (the
    driver's own spawn, at the owner's cost). An agent of a job of another scope, and an agent of
    a job of this scope that is still open or expired (the judge or `reconcile` takes those), is
    not beyond anything and is left out."""
    folded = fold(conn, scope)
    known = _known_agents(folded)
    first: dict = {}
    for ev in drivers(conn, scope):
        s = (ev["data"] or {}).get("session")
        if s:
            first.setdefault(s, ev["id"])
    out, seen = [], set()
    for session, after in first.items():
        for ev in stop_events(conn, session, after):
            aid = (ev["data"] or {}).get("agent_id")
            if not aid or aid in known or aid in seen:
                continue
            seen.add(aid)
            loc = locator_for(conn, aid, ev)
            named = _JOB_NAMED.search(_head(loc)) if loc else None
            jid = named.group(1) if named else None
            if jid is not None:
                j = folded.get(jid)
                if j is None:
                    other = find_job_event(conn, jid)
                    if other is not None:
                        continue                # a job of another scope: its own judge reads it
                    jid = None
                elif j["state"] not in ("accepted", "refused"):
                    continue                    # still open, or expired: judge or reconcile
            out.append({"agent_id": aid, "stop_event": ev["id"], "locator": loc, "job": jid})
    return out


def price_strays(conn, root: str, scope: str, *, session: str) -> list:
    """Price every stray of the scope (an extra agent that names an accepted or refused job of the
    scope) that has stopped and whose transcript can be read: copy and hash the file, read the
    cost as the judge does, record research-job-late with `stray` true and book it under the job's
    attempt. The K14 test applied after the fact, for a twin that was still running when the
    judge decided (adversarial check round 2, B2)."""
    from alpaca.research import spend
    out = []
    for item in extra_agents(conn, scope):
        jid, loc, aid = item["job"], item["locator"], item["agent_id"]
        if jid is None or not loc or not os.path.isfile(loc):
            continue
        j = fold(conn, scope).get(jid)
        if j is None:
            continue
        d = j["job"]
        jobd = read_job(root, scope, jid) or {}
        evidence = os.path.join(paths.job_dir(root, scope, jid), "late")
        os.makedirs(evidence, exist_ok=True)
        dst = os.path.join(evidence, "agent-%s.jsonl" % aid)
        shutil.copyfile(loc, dst)
        spec_session = jobd.get("launch_session") or session
        usage = measure_usage(conn, root, "%s-stray-%s" % (spec_session, aid[:8]), dst)
        with db.transaction(conn):
            events.emit(conn, events.JOB_LATE, run=d.get("run"), ref=d.get("attempt") or scope,
                        op=d.get("op"), session=session, actor="research-runner",
                        data={"job": jid, "scope": scope, "key": d.get("key"), "agent_id": aid,
                              "stray": True, "transcript": dst,
                              "transcript_sha256": eng.sha256_file(dst),
                              "measured_usage": {k: usage[k] for k in ("tokens", "cost_usd",
                                                                      "priced", "model")},
                              "priced": usage["priced"], "tokens": usage["tokens"],
                              "cost_usd": usage["cost_usd"]}, in_txn=True)
            if usage["priced"]:
                spend.book(conn, d.get("run"), d.get("attempt"), cost_usd=usage["cost_usd"],
                           tokens=usage["tokens"], session=spec_session, in_txn=True)
        out.append({"job": jid, "agent_id": aid, "stray": True})
    return out


# ================================================================ pricing a transcript
def budget_tokens(summary_tokens: dict | None) -> tuple:
    """(tokens, total, cache_read): the tokens a launch counts against the run's token cap, the
    analysis's total and the cache reads in it. The cap counts what the model processed afresh:
    input it did not read from the prompt cache, cache writes and output. A cache read is the
    re-read of the context the provider kept, which every turn of an agent repeats: one rr-002
    auditor's 11 responses read 708394 cached tokens of a context that never passed 87000, and
    its total of 786811 was booked against a cap of 3000000 (fix batch 3, F22, record event
    78459). The dollar cost still prices every kind, cache reads included. With no cache read
    count the total is the figure."""
    t = summary_tokens if isinstance(summary_tokens, dict) else {}
    total = t.get("total_tokens")
    if not isinstance(total, int) or isinstance(total, bool) or total <= 0:
        return None, None, None
    cached = t.get("cached_input_tokens")
    if not isinstance(cached, int) or isinstance(cached, bool) or cached < 0 or cached > total:
        return total, total, None
    return total - cached, total, cached


def measure_usage(conn, root: str, session: str, path: str) -> dict:
    """{tokens, cost_usd, priced, model, sources, tokens_total, cache_read_tokens}: what the kept
    transcript says it spent, read through the analytics (a session row whose transcript is the
    kept copy, then metrics.analyze, the transcript half of the launcher's read). A subagent
    transcript carries no reported cost, so the figure is the estimate from the rate cards, and a
    model with no card leaves it unpriced. `tokens` is the budget's count (budget_tokens: cache
    reads left out); `tokens_total` keeps the analysis's total."""
    tokens, cost, total, cached = None, None, None, None
    try:
        from alpaca.analytics import metrics
        now = util.now_iso()
        if not db.rows(conn, "sessions", "sid=?", (session,)):
            db.upsert(conn, "sessions", "sid", {"sid": session, "started": now, "transcript": path})
        else:
            db.patch(conn, "sessions", "sid", session, {"transcript": path})
        analysis = metrics.analyze(root, session)
        tokens, total, cached = budget_tokens((analysis.get("summary") or {}).get("tokens"))
        if tokens is not None:
            costs = analysis.get("cost") or {}
            for key in ("reported_usd", "estimated_usd"):
                if isinstance(costs.get(key), (int, float)):
                    cost = float(costs[key])
                    break
    except Exception:  # noqa: BLE001 - an unreadable analysis leaves the figure unpriced
        tokens, cost, total, cached = None, None, None, None
    models = at.models(at.read_records(path)) if os.path.isfile(path) else []
    return {"tokens": tokens, "cost_usd": cost, "priced": cost is not None,
            "model": models[0] if models else None, "tokens_total": total,
            "cache_read_tokens": cached, "sources": {"transcript": {
                "tokens": tokens, "cost_usd": cost, "tokens_total": total,
                "cache_read_tokens": cached}}}


def unpriced_models(root: str, models: list) -> list:
    """The ids of `models` that have no rate card, each with its price-check command."""
    from alpaca.analytics import pricing, ratecards
    try:
        cards = ratecards.recorded(root)
    except Exception:  # noqa: BLE001
        cards = {}
    out = []
    for m in sorted(set(models)):
        if pricing.card_for(m, cards)[0] is None:
            out.append({"model": m, "fix": ratecards.fix_command(m, "anthropic")})
    return out


# ================================================================ calls the guard refused
#: the roles that have call-time shell rules (role_rules, in-session-runtime.md section 18.1); the
#: tool step of the guard covers every role (section 26)
GUARDED_ROLES = ("verifier", "auditor")
#: the deny rules that are not a role rule: the guard writes no event for them
NON_ROLE_RULES = ("tool", "deny-cap")
#: the rule words of the `research-guard-deny` event of a plain-string denial (the earlier checks of
#: the guard) of a job whose role is not in GUARDED_ROLES, so the audit can tell a call the guard
#: refused from one that ran (t-351 F48, in-session-runtime.md section 23); the verifier's and
#: the auditor's string denials write no event (section 18.1)
DENY_EVENT_RULES = ("control-plane", "engine", "background", "driver", "expired")
#: the actor of a `research-guard-deny` event, and the most such events one job can hold
GUARD_ACTOR = "research-guard"
GUARD_DENY_CAP = 64


def call_sha256(value) -> str:
    """The sha256 of the canonical JSON of a tool input (rerun.canonical_json: sorted keys, no
    spaces, no ASCII escaping), as the guard writes it in `research-guard-deny` and the judge reads
    it back from the transcript."""
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def text_sha256(text) -> str:
    return hashlib.sha256(str(text).encode("utf-8")).hexdigest()


def deny_events(conn, job_id: str, agent_id: str | None) -> list:
    """The `research-guard-deny` events of one job and agent, written by the guard (actor
    `research-guard`), oldest first: [{id, data}]."""
    out = []
    if not job_id or not agent_id:
        return out
    try:
        rows = conn.execute("SELECT id, actor, data FROM events WHERE kind=? AND ref=? ORDER BY id",
                            (events.GUARD_DENY, job_id)).fetchall()
    except Exception:  # noqa: BLE001 - no record to read
        return out
    for row in rows:
        if row["actor"] != GUARD_ACTOR:
            continue
        try:
            data = json.loads(row["data"] or "{}")
        except (TypeError, ValueError):
            continue
        if isinstance(data, dict) and data.get("job") == job_id and data.get("agent_id") == agent_id:
            out.append({"id": row["id"], "data": data})
    return out


def _denied(call: dict, ev: dict, allowed: set) -> bool:
    """True when `ev` (one `research-guard-deny` event) is the guard's record of this very call:
    its `tool_use_id`, tool and input hash are the call's, the tool is one the job is given, the
    rule is a role rule, and the call's result is an error whose text is exactly what Claude Code
    writes for a hook's denial, `PreToolUse:<tool> hook error: <deny text>`, with the deny text
    hashing to the event's `reason_sha256`. An error result alone is not enough: a call that ran
    and exited non-zero is an error result too, and its text starts `Exit code N`."""
    d = ev["data"]
    tool = call.get("tool")
    if not call.get("id") or d.get("tool_use_id") != call.get("id") or d.get("tool") != tool \
            or tool not in allowed or d.get("rule") in (None, "") \
            or d.get("rule") in NON_ROLE_RULES:
        return False
    if d.get("input_sha256") != call_sha256(call.get("raw_input") if isinstance(
            call.get("raw_input"), dict) else {}):
        return False
    if call.get("error") is not True or not isinstance(call.get("output"), str):
        return False
    head = "PreToolUse:%s hook error: " % tool
    out = call["output"]
    return out.startswith(head) and text_sha256(out[len(head):]) == d.get("reason_sha256")


def ran_calls(conn, job_id: str, agent_id: str | None, calls: list) -> list:
    """`calls` with each call the guard denied replaced, in its place, by a stub {"kind":
    "denied", "tool", "id", "input": "", "paths": [], "output": None}, so the number of every
    later call stays what it is (in-session-runtime.md section 18.1). A call is denied only on the
    guard's own record: see `_denied`. Text in the transcript alone never makes a call denied, and
    a call that has an event but a result that does not match is judged as a call that ran."""
    evs = deny_events(conn, job_id, agent_id)
    if not evs:
        return list(calls)
    jev = find_job_event(conn, job_id)
    allowed = set(((jev or {}).get("data") or {}).get("allowed_tools") or [])
    by_id = {}
    for ev in evs:
        by_id.setdefault(ev["data"].get("tool_use_id"), []).append(ev)
    out = []
    for call in calls:
        hit = by_id.get(call.get("id")) if call.get("id") else None
        if hit and any(_denied(call, ev, allowed) for ev in hit):
            out.append({"kind": "denied", "tool": call.get("tool"), "id": call.get("id"),
                        "input": "", "paths": [], "output": None})
        else:
            out.append(call)
    return out


def source_transcript_of(conn, scope: str | None, job_id: str) -> str | None:
    """The path of the transcript the judge took the job's agent transcript from (the
    research-job-result event's `source_transcript`), or None. With no scope the job's own
    research-job event names it."""
    try:
        if scope is None:
            jev = find_job_event(conn, job_id)
            scope = ((jev or {}).get("data") or {}).get("scope")
            if scope is None:
                return None
        got = fold(conn, scope).get(job_id)
    except Exception:  # noqa: BLE001
        return None
    return ((got or {}).get("result") or {}).get("source_transcript")


def is_stub(call: dict) -> bool:
    return call.get("kind") == "denied"


# ================================================================ the judge
class Ctx:
    """What the checks of one claim read: the record, the job, its hand, the claim and the files
    the witnesses left. The properties read lazily and cache."""

    def __init__(self, conn, root: str, scope: str, job_id: str, claim: dict | None = None, *,
                 replay: bool = False, kept: str | None = None, result: dict | None = None,
                 barrier=None, now=None, stop=None):
        self.conn, self.root, self.scope, self.job_id = conn, root, scope, job_id
        self.claim = claim or {}
        self.replay, self.kept, self.result = replay, kept, result
        self.barrier, self.stop = barrier, stop
        self.now = now or _now()
        self.fold = fold(conn, scope).get(job_id)
        if self.fold is None:
            raise Problem("JOB-UNKNOWN", "no job %s in %s" % (job_id, scope))
        self.event = self.fold["job"]
        self.job = event_backed(read_job(root, scope, job_id), self.event)
        self.handed = self.fold["handed"] or {}
        self.handed_id = self.fold["handed_id"]
        self.drv = newest_driver(conn, scope)
        self.driver_session = (self.handed.get("session")
                               or ((self.drv or {}).get("data") or {}).get("session"))
        self.run = self.event.get("run")
        self.agent_id = self.claim.get("agent_id") or (result or {}).get("agent_id")
        self.store: dict = {}
        self._records = None
        self.copy_path = kept
        self.checks_run: list = []

    # --- witnesses
    @property
    def stop_ev(self):
        if "stop_ev" not in self.store:
            found = None
            if self.agent_id and self.driver_session:
                for ev in stop_events(self.conn, self.driver_session, self.handed_id or 0):
                    if (ev["data"] or {}).get("agent_id") == self.agent_id:
                        found = ev
            self.store["stop_ev"] = found
        return self.store["stop_ev"]

    @property
    def start_ev(self):
        if "start_ev" not in self.store:
            found = None
            if self.agent_id and self.driver_session:
                for ev in start_events(self.conn, self.driver_session, 0):
                    if (ev["data"] or {}).get("agent_id") == self.agent_id:
                        found = ev
            self.store["start_ev"] = found
        return self.store["start_ev"]

    @property
    def records(self) -> list:
        if self._records is None:
            path = self.copy_path or self.store.get("source_path")
            self._records = at.read_records(path) if path and os.path.isfile(path) else []
        return self._records

    @property
    def calls(self) -> list:
        if "calls" not in self.store:
            self.store["calls"] = at.calls(self.records)
        return self.store["calls"]

    @property
    def pin_event(self):
        return newest_pin(self.conn, self.scope, self.run)

    def first_end(self):
        form, end = at.first_turn(self.records, self.job.get("spawn_prompt") or "")
        return form, end


def check_state(c: Ctx) -> None:
    """K1: the job exists, is open and handed, and the claim number is new."""
    if c.replay:
        return
    if not is_open(c.fold):
        raise Problem("JOB-STATE", "job %s is %s" % (c.job_id, c.fold["state"]))
    if c.fold["handed"] is None:
        raise Problem("JOB-STATE", "job %s was never handed out" % c.job_id)
    if not c.claim.get("n"):
        raise Problem("JOB-STATE", "no claim")


def check_nonce(c: Ctx) -> None:
    """K2: sha256(nonce) equals the job event's nonce_sha256. The verb already compared the nonce
    the driver passed; the judge compares the claim file's hash, so a hand-written claim file with
    another job's nonce is refused too."""
    if c.replay:
        return
    want = c.event.get("nonce_sha256")
    got = c.claim.get("nonce_sha256")
    nonce = c.job.get("nonce")
    if hashlib.sha256(str(nonce or "").encode("utf-8")).hexdigest() != want:
        raise Problem("JOB-NONCE", "job.json no longer holds the nonce the job event hashes")
    if got is not None and got != want:
        raise Problem("JOB-NONCE", "the claim names another job's nonce")


def check_deadline(c: Ctx) -> None:
    """K3: now is not after the job's deadline, read from the handed event."""
    if c.replay:
        return
    dl = parse_time(c.handed.get("deadline"))
    if dl is not None and c.now > dl:
        raise Problem("JOB-DEADLINE", "the claim came after the deadline %s" % c.handed.get("deadline"))


def check_witness(c: Ctx) -> None:
    """K4: the hooks wrote a subagent-stop (and a subagent-start) event of the driver's session
    for this agent after the job was handed, an obs_source row names the transcript, the agent
    type is a plain one and the agent id is new to the scope."""
    if c.replay:
        return
    if not c.agent_id:
        raise Problem("JOB-STOP-MISMATCH", "the claim names no agent id")
    if c.driver_session is None:
        raise Problem("JOB-NOT-DRIVER", "the scope has no driver")
    stop = c.stop_ev
    if stop is None:
        # a stop event written before the hand is a stale agent, not a pending one
        early = [ev for ev in stop_events(c.conn, c.driver_session, 0)
                 if (ev["data"] or {}).get("agent_id") == c.agent_id]
        if early:
            raise Problem("JOB-STOP-MISMATCH", "agent %s stopped before job %s was handed" % (
                c.agent_id, c.job_id), extra={"detail_code": "started-before-hand"})
        raise Problem("JOB-NO-STOP", "no subagent-stop event of the driver names agent %s yet"
                      % c.agent_id, retriable=True)
    start = c.start_ev
    if start is not None and start["id"] < (c.handed_id or 0):
        raise Problem("JOB-STOP-MISMATCH", "agent %s started before job %s was handed"
                      % (c.agent_id, c.job_id), extra={"detail_code": "started-before-hand"})
    row = source_row(c.conn, c.agent_id)
    if row is None or not row.get("locator"):
        loc = (stop["data"] or {}).get("agent_transcript_path")
        if not loc:
            raise Problem("JOB-NO-TRANSCRIPT", "no observability source row names a transcript of "
                          "agent %s" % c.agent_id, retriable=True)
    else:
        if row.get("parent_session") != c.driver_session or row.get("native_id") != c.agent_id:
            raise Problem("JOB-STOP-MISMATCH", "the source row of agent %s names another parent "
                          "or native id" % c.agent_id)
        runtime = (c.event.get("runtime") or "claude")
        if row.get("provider") not in (runtime, "claude-code", None):
            raise Problem("JOB-STOP-MISMATCH", "the source row of agent %s is of provider %s, not "
                          "%s" % (c.agent_id, row.get("provider"), runtime))
    kind = (stop["data"] or {}).get("agent_type")
    meta = _meta_of(c)
    if meta.get("agentType"):
        kind = meta["agentType"]
    if kind is not None and kind not in PLAIN_TYPES:
        raise Problem("JOB-AGENT-TYPE", "agent %s is of type %s; a job runs on a plain type"
                      % (c.agent_id, kind))
    for jid, other in fold(c.conn, c.scope).items():
        if jid == c.job_id:
            continue
        for cl in other["claims"]:
            if cl.get("agent_id") == c.agent_id:
                raise Problem("JOB-AGENT-REUSED", "agent %s served job %s" % (c.agent_id, jid))


def _meta_of(c: Ctx) -> dict:
    loc = locator_for(c.conn, c.agent_id, c.stop_ev) if c.agent_id else None
    if not loc:
        return {}
    meta = loc[:-len(".jsonl")] + ".meta.json" if loc.endswith(".jsonl") else None
    got = eng.read_json(meta) if meta else None
    return got if isinstance(got, dict) else {}


def check_transcript(c: Ctx) -> None:
    """K5: the file exists, is a regular file inside the runtime's transcript folder, is within
    the size the audit reads whole, ends on a whole line and equals --transcript when given; it
    is copied to the kept evidence folder and hashed. The hook's snapshot, when it exists, must
    be a part of the file."""
    from alpaca.research import launcher
    if c.replay:
        path = c.kept
        if not path or not os.path.isfile(path):
            raise Problem("JOB-NO-TRANSCRIPT", "the kept transcript is missing", retriable=False)
        want = (c.result or {}).get("transcript_sha256")
        if want and eng.sha256_file(path) != want:
            raise Problem("JOB-TRANSCRIPT-CHANGED", "the kept transcript no longer hashes to the "
                          "recorded value")
        c.store["source_path"] = path
        return
    loc = locator_for(c.conn, c.agent_id, c.stop_ev)
    if not loc:
        raise Problem("JOB-NO-TRANSCRIPT", "no transcript path is registered for agent %s"
                      % c.agent_id, retriable=True)
    if not os.path.isabs(loc):
        loc = os.path.join(c.root, loc)
    if not os.path.isfile(loc) or os.path.islink(loc):
        raise Problem("JOB-NO-TRANSCRIPT", "the transcript %s is not there" % loc, retriable=True)
    real = os.path.realpath(loc)
    cfg = (c.drv["data"].get("config_dir") if c.drv else None) or eng.config_dir()
    folders = {os.path.join(os.path.realpath(cfg), "projects"),
               os.path.join(eng.home(), ".claude", "projects")}
    from alpaca import transcripts
    folders.add(os.path.realpath(transcripts.subagents_dir(c.root, c.driver_session or "")))
    if not any(eng.under(real, f) for f in folders):
        raise Problem("JOB-NO-TRANSCRIPT", "the transcript %s is outside the runtime's transcript "
                      "folder" % real)
    size = os.path.getsize(real)
    if size > launcher._STREAM_MAX:
        raise Problem("JOB-TRANSCRIPT-TOO-LARGE", "the transcript is %d bytes, over the %d the "
                      "audit reads whole" % (size, launcher._STREAM_MAX))
    if not at.ends_on_whole_line(real):
        raise Problem("JOB-NO-TRANSCRIPT", "the transcript does not end on a whole line yet",
                      retriable=True)
    # the kept copy is taken first, so every later check reads the same bytes the hash covers
    evidence = c.job.get("evidence_dir")
    if not evidence:
        raise Problem("JOB-NO-TRANSCRIPT", "job %s has no evidence folder" % c.job_id)
    os.makedirs(evidence, exist_ok=True)
    dst = os.path.join(evidence, "agent-%s.jsonl" % re.sub(r"[^A-Za-z0-9_.-]", "-", c.agent_id))
    shutil.copyfile(real, dst)
    c.copy_path = dst
    c.store["source_path"] = real
    c.store["source_sha256"] = eng.sha256_file(real)
    c.store["kept_sha256"] = eng.sha256_file(dst)
    c.store["evidence"] = evidence
    c._records = None
    given = c.claim.get("transcript")
    if given and os.path.realpath(given) != real:
        raise Problem("JOB-NO-TRANSCRIPT", "--transcript %s is not the file the hook registered (%s)"
                      % (given, real))
    snap = _snapshot_of(c)
    if snap is not None:
        # records, not record ids and not bytes: a record rewritten after the stop keeps its uuid
        # (adversarial check round 1, B1), and the hook's copy is not a byte copy (it drops the
        # private reasoning of each record and re-serializes a record it changed,
        # `alpaca/transcripts.py` `_filtered_source`), so the file, filtered the way the hook
        # filters, must start with the records of the snapshot and any rest must be whole lines
        # (investigate-s1: a byte prefix refuses about 97 percent of real subagent transcripts)
        try:
            with open(snap, "rb") as fh:
                want = fh.read()
            with open(dst, "rb") as fh:
                have = fh.read()
        except OSError:
            want, have = None, b""
        if want is not None and not _snapshot_matches(want, have):
            raise Problem("JOB-TRANSCRIPT-CHANGED", "the transcript differs from the copy the "
                          "hook took at stop")


def _snapshot_matches(snapshot: bytes, live: bytes) -> bool:
    """True when the records of the hook's snapshot are the first records of the live file as the
    hook would have copied it. The snapshot holds whole lines only; a snapshot that ends inside a
    line, or a line that does not parse, does not match."""
    from alpaca import transcripts
    if snapshot and not snapshot.endswith(b"\n"):
        return False
    want = []
    for line in snapshot.split(b"\n")[:-1]:
        try:
            want.append(json.loads(line))
        except (ValueError, UnicodeError, RecursionError):
            return False
    have = []
    for line in live.split(b"\n")[:-1]:
        try:
            record = json.loads(line)
        except (ValueError, UnicodeError, RecursionError):
            have.append({"type": "capture-malformed-record"})
            continue
        shown = transcripts.visible_record(record)
        if shown is not None:
            have.append(shown)
    return have[:len(want)] == want and len(have) >= len(want)


def _snapshot_of(c: Ctx):
    from alpaca import transcripts
    base = transcripts.subagents_dir(c.root, c.driver_session or "")
    if not os.path.isdir(base):
        return None
    name = "agent-%s.jsonl" % c.agent_id
    for folder, _d, names in os.walk(base):
        if name in names:
            return os.path.join(folder, name)
    return None


def check_first_turn(c: Ctx) -> None:
    """K6: every record carries the driver's session id, the agent id and isSidechain true; the
    first user turn equals the job's spawn prompt byte for byte, in one of the two known forms."""
    if c.job.get("spawn_prompt_edited"):
        raise Problem("JOB-TRANSCRIPT-FRAME", "job.json holds a spawn prompt that no longer hashes "
                      "to the spawn_prompt_sha256 of the research-job event for job %s" % c.job_id)
    recs = c.records
    if not recs:
        raise Problem("JOB-TRANSCRIPT-FRAME", "the transcript holds no record")
    for i, r in enumerate(recs):
        if "_unreadable" in r:
            raise Problem("JOB-TRANSCRIPT-FRAME", "record %d does not parse" % i)
        if r.get("type") in ("user", "assistant", "attachment"):
            if c.driver_session and r.get("sessionId") != c.driver_session:
                raise Problem("JOB-SESSION-MISMATCH", "record %d names session %s, not the driver's"
                              % (i, r.get("sessionId")))
            if r.get("agentId") != c.agent_id or r.get("isSidechain") is not True:
                raise Problem("JOB-SESSION-MISMATCH", "record %d is not agent %s's sidechain record"
                              % (i, c.agent_id))
    form, end = c.first_end()
    if form is None:
        raise Problem("JOB-TRANSCRIPT-FRAME", "the first turn is not job %s's spawn prompt in a form "
                      "the reader knows" % c.job_id)
    c.store["first_form"], c.store["first_end"] = form, end
    given = c.claim.get("tool_use_id") if not c.replay else (c.result or {}).get("tool_use_id")
    meta = {} if c.replay else _meta_of(c)
    if given and meta.get("toolUseId") and given != meta["toolUseId"]:
        raise Problem("JOB-TRANSCRIPT-FRAME", "--tool-use-id %s is not the agent's toolUseId" % given)


def check_prompt_read(c: Ctx) -> None:
    """K7: the first tool call is a Read (or cat) of the prompt file and its result, with the
    cat -n prefixes and a trailing system-reminder removed, equals the prompt file, so the agent
    saw exactly the recorded prompt, the nonce and job id included."""
    from alpaca.research import pathscan
    calls = c.calls
    path = c.job.get("prompt_path") or c.event.get("prompt_path")
    if not calls:
        raise Problem("JOB-PROMPT-UNREAD", "the agent made no tool call")
    first = calls[0]
    named = False
    if first.get("tool") == "Read":
        inp = first.get("input") if isinstance(first.get("input"), dict) else {}
        named = os.path.realpath(str(inp.get("file_path") or "")) == os.path.realpath(path)
        output = at.strip_read_output(first.get("output"))
    elif first.get("kind") == "shell":
        text = pathscan.shell_text(first) or ""
        try:
            got = pathscan.unwrap(pathscan.parse(text))
        except Exception:  # noqa: BLE001
            got = {"commands": []}
        for cmd in got["commands"]:
            argv = cmd["argv"]
            if argv and os.path.basename(argv[0]) == "cat" and len(argv) == 2 and \
                    os.path.realpath(argv[1]) == os.path.realpath(path):
                named = True
        output = str(first.get("output") or "")
    else:
        output = ""
    if not named:
        raise Problem("JOB-PROMPT-UNREAD", "the first call is not a read of the prompt file %s" % path)
    cut = at.read_truncation(c.records, first.get("output"), path)
    if cut is not None:
        # a Read past its token cap answers with a PARTIAL view: the agent saw part of the prompt
        # (fix batch 2, F5; k-replay-findings.md G4). The issue keeps a prompt well under the cap.
        raise Problem("JOB-PROMPT-UNREAD", "the Read of the prompt file was truncated (a PARTIAL view%s)"
                      "; the agent did not see the whole prompt, which is over the Read cap" % (
                          ": %s tokens, cap %s" % (cut["tokens"], cut["cap"]) if cut["tokens"] else ""))
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
        want = raw.decode("utf-8", "replace")
    except OSError:
        raise Problem("JOB-PROMPT-UNREAD", "the prompt file is gone")
    recorded = c.event.get("prompt_sha256")
    if recorded and hashlib.sha256(raw).hexdigest() != recorded:
        # the file was edited after the issue: what the agent read is not what the record says
        raise Problem("JOB-PROMPT-UNREAD", "the prompt file no longer hashes to the recorded "
                      "prompt_sha256, so what the agent read is not the recorded prompt")
    if output.rstrip() != want.rstrip():
        raise Problem("JOB-PROMPT-UNREAD", "the text the agent read differs from the recorded prompt")


def _hook_command_ok(cmd, allowed: list) -> bool:
    return isinstance(cmd, str) and cmd in allowed


def check_relay(c: Ctx) -> None:
    """K8: after the first turn a user-side record holds a tool result, a system-reminder text or
    an attachment and nothing else. Pushed text is refused by name; a hook body is at most 8 KiB
    and must have a provenance (a pinned settings command whose script is in the pin); the
    kinds that carry the text of a file are read for what they carry; every other kind passes and
    is counted by kind."""
    recs = c.records
    end = c.store.get("first_end")
    if end is None:
        end = at.first_user_index(recs)         # the first turn is the first user record
    bad = at.relay_problems(recs, end)
    if bad:
        raise Problem("JOB-RELAY", "record %d: %s" % bad[0])
    allowed_cmds = []
    if c.drv is not None:
        allowed_cmds = list((c.drv["data"] or {}).get("hook_commands") or [])
    for body in at.hook_bodies(recs):
        if len(body["content"].encode("utf-8")) > HOOK_BODY_MAX:
            raise Problem("JOB-HOOK-CONTEXT", "a hook body of %d bytes is over %d"
                          % (len(body["content"].encode("utf-8")), HOOK_BODY_MAX))
        if not body["has_success"] or not body["stdout_ok"] or \
                not _hook_command_ok(body["command"], allowed_cmds):
            raise Problem("JOB-HOOK-CONTEXT", "the hook body of record %s has no pinned settings "
                          "command behind it (%s)" % (body["index"], body["command"]))
    pin = pin_of(newest_pin(c.conn, c.scope, c.run)) or {}
    ledger = eng.load_ledger(((c.handed or {}).get("context")))
    own = _own_paths(c)
    attachments = []
    for f in at.instruction_files(recs):
        member = eng.attachment_member(ledger, pin, f["path"]) if (ledger or pin) else True
        if (ledger or pin) and not member:
            raise Problem("JOB-CONTEXT-UNLISTED", "an instructions attachment names %s, which the "
                          "job's set does not hold" % f["path"],
                          extra={"end_run": True, "unlisted": [f["path"]], "kind": "context-unlisted"})
        known = ((ledger or {}).get("files") or {}).get(os.path.realpath(f["path"])) or \
            ((ledger or {}).get("files") or {}).get(f["path"]) or {}
        digest = hashlib.sha256(f["content"].encode("utf-8")).hexdigest()
        attachments.append({"kind": "instructions", "path": f["path"], "sha256": digest,
                            "bytes": len(f["content"].encode("utf-8")),
                            "pinned_sha256": known.get("sha256"),
                            "matches_pin": known.get("sha256") == digest if known else None})
    called = _called_paths(c)
    for f in at.file_attachments(recs):
        path = f["path"] or ""
        member, ok = attachment_verdict(f, ledger, pin, own, called)
        if not ok:
            raise Problem("JOB-RELAY", "a %s attachment names %s, which the agent could not read"
                          % (f["kind"], path))
        attachments.append({"kind": f["kind"], "path": path,
                            "sha256": hashlib.sha256(f["content"].encode("utf-8")).hexdigest(),
                            "bytes": len(f["content"].encode("utf-8")), "pinned_sha256": None,
                            "matches_pin": None})
    c.store["attachments"] = attachments
    c.store["attachment_kinds"] = at.attachment_kinds(recs)


def attachment_verdict(f: dict, ledger, pin, own: list, called: set) -> tuple:
    """(member, ok) for one `nested_memory` or `edited_text_file` attachment of K8: `member` is
    whether the path belongs to the job's set (the pin and the ledger), and `ok` whether the
    attachment passes. It passes when it is a member, or lies under the job's own paths; an
    `edited_text_file` there must also be a file the agent touched (a call names it, as a tool path
    or as a path in a shell command: a `cat`, a `sed -i`, a script argument) or carry no text. The
    runtime notes a file the agent holds in its read state that changed on disk, and the attachment
    carries a path and, in every sample measured, no text, so one that relays nothing relays nothing
    (fix batch 2, F2; k-replay-findings.md G1). Pure, so the replay tool puts the same rule to the
    real transcripts."""
    path = f.get("path") or ""
    real = os.path.realpath(path) if path else ""
    member = bool(path) and bool(eng.attachment_member(ledger, pin, path))
    by_own = bool(path) and any(eng.under(real, o) for o in own)
    if f.get("kind") == "edited_text_file":
        touched = real in called or any(os.path.basename(real) == os.path.basename(p)
                                        for p in called)
        by_own = by_own and (touched or not str(f.get("content") or "").strip())
    return member, bool(member or by_own)


def called_paths(calls: list, shell) -> set:
    """The real paths a list of calls name: the path field of a tool call, and the paths a shell
    call's text names, placed against `shell`, the folder every call of the job starts in (a
    relative path, `cd`, a `sed -i` target). A path the scan cannot place comes back as written;
    the caller also matches a basename."""
    from alpaca.research import pathscan
    out = set()
    for call in calls:
        for p in call.get("paths") or []:
            out.add(os.path.realpath(p))
        if call.get("kind") == "shell" and shell:
            text = pathscan.shell_text(call)
            if text:
                try:
                    for p in pathscan.paths_in(text, shell):
                        out.add(os.path.realpath(p))
                    # a bare file name after a `cd` (`cd src && cat -n notes.md`) is no path to the
                    # scan; its word is kept as a name, which the caller matches by basename
                    for cmd in pathscan.unwrap(pathscan.parse(text))["commands"]:
                        for word in cmd["argv"][1:]:
                            if word and not word.startswith("-") and "/" not in word \
                                    and "=" not in word and len(word) < 256:
                                out.add(os.path.join(os.path.realpath(shell), word))
                except Exception:  # noqa: BLE001 - a text the scan cannot read names nothing
                    pass
    return out


def _called_paths(c: Ctx) -> set:
    calls = c.calls
    if c.event.get("role") in GUARDED_ROLES:
        # a call the guard denied touched no file
        calls = ran_calls(c.conn, c.job_id, c.agent_id, calls)
    return called_paths(calls, c.job.get("shell_cwd") or c.job.get("cwd"))


def session_scratchpads(c: Ctx) -> list:
    """The scratch folder Claude Code advertises to the agent in its environment attachment, when
    it is the driving session's own (<tmp>/claude-<uid>/<project>/<driver session>/scratchpad).
    It is shared by every job of the session, so it is a channel between attempts and between the
    two verifiers of a pair. Since t-191 (F29) it is no job's own path: K8 does not accept an edit
    there as the agent's own, K15 and the audit's rule S1 flag a write there, and each job has a
    private scratch folder instead (paths.job_scratch). The list is for the refusal text and the
    replay tool, not for any allowance."""
    out = []
    for sp in at.scratchpad_dirs(c.records):
        sp = os.path.normpath(sp)
        if os.path.isabs(sp) and os.path.basename(sp) == "scratchpad" and c.driver_session \
                and os.path.basename(os.path.dirname(sp)) == c.driver_session:
            out.append(os.path.realpath(sp))
    return out


def _own_paths(c: Ctx) -> list:
    """The folders the agent's own Read could name: the job's cwd, read and write paths, its
    prompt path, its private scratch folder and the persisted outputs its own tool results
    announced. The session scratchpad is not one of them (t-191 F29)."""
    out = [c.job.get("cwd"), c.job.get("workdir"), c.job.get("prompt_path"), c.job.get("scratch")]
    out += list(c.job.get("read_paths") or []) + list(c.job.get("write_paths") or [])
    out += list(persisted_of(c))
    return [os.path.realpath(p) for p in out if p]


def results_dir_of(c: Ctx) -> str | None:
    """The `tool-results/` folder of the driving session, beside the folder the agent's transcript
    came from (agent_transcript.results_dir_of); for a replay, the source the result recorded."""
    src = c.store.get("source_path") or (c.result or {}).get("source_transcript") or c.kept
    return at.results_dir_of(src)


def persisted_of(c: Ctx) -> set:
    """The files the agent's own tool results announced as persisted outputs, in the form Claude
    Code writes and nowhere else (agent_transcript.persisted_outputs; in-session-runtime.md
    section 18.1a). Each is allowed as that exact file."""
    return at.persisted_outputs(c.records, results_dir_of(c))


def check_tools(c: Ctx) -> None:
    """K9: every tool call passes `agent_transcript.off_list` against the job's allowed tools (the
    guard denies the same calls before they run, section 26); no call starts a process that
    outlives the call."""
    listed = c.job.get("allowed_tools") or c.event.get("allowed_tools") or []
    allowed = set(listed) | set(at.RUNTIME_TOOLS)
    raw_inputs = {}
    for r in c.records:
        if r.get("type") == "assistant":
            for item in at.content_items(r):
                if item.get("type") == "tool_use" and item.get("id"):
                    raw_inputs[str(item["id"])] = item.get("input") if isinstance(
                        item.get("input"), dict) else {}
    seen = []
    for name in at.tool_names(c.records):
        seen.append(name)
        if at.off_list(name, listed):
            raise Problem("JOB-TOOL-OFF-LIST", "the agent called %s, which is not in its allowed "
                          "tools (%s)" % (name, ", ".join(sorted(allowed))))
    walk = c.calls
    if c.event.get("role") in GUARDED_ROLES:
        # a call the guard denied never ran (section 18.1): it cannot start a process
        walk = ran_calls(c.conn, c.job_id, c.agent_id, c.calls)
    for call in walk:
        if is_stub(call):
            continue
        call = dict(call, raw_input=raw_inputs.get(str(call.get("id"))))
        form = eng.background_hit(call)
        if form:
            raise Problem("JOB-BACKGROUND", "a call starts a process that outlives it (%s)" % form)
    c.store["tools_seen"] = sorted(set(seen))


_FAMILIES = {"haiku": "claude-haiku", "sonnet": "claude-sonnet", "opus": "claude-opus",
             "fable": "claude-fable"}


def check_model(c: Ctx) -> None:
    """K10: every assistant record's message.model starts with the family the job's model names."""
    want = c.event.get("model")
    seen = at.models(c.records)
    c.store["models_seen"] = sorted(set(seen))
    if not want:
        return
    fam = _FAMILIES.get(want, "claude-%s" % want)
    for m in seen:
        if not m.startswith(fam):
            raise Problem("JOB-MODEL-MISMATCH", "the agent ran on %s, the job names %s" % (m, want))


def _trim_newline(data: bytes) -> bytes:
    """The bytes without one trailing line end (`\n` or `\r\n`)."""
    if data.endswith(b"\r\n"):
        return data[:-2]
    if data.endswith(b"\n"):
        return data[:-1]
    return data


def check_final(c: Ctx) -> None:
    """K11: the final message exists (unless the transcript ends in an API error or a pending
    tool call, which the end classifies, section 5.2), and when --final is given its sha256
    equals the transcript's final message."""
    final = at.final_message(c.records)
    c.store["final"] = final
    if final is None:
        if at.api_error(c.records) is None and not at.pending_call(c.records):
            raise Problem("JOB-FINAL-MISSING", "the transcript holds no final message")
        return
    given = c.claim.get("final") if not c.replay else None
    if given:
        try:
            with open(given, "rb") as fh:
                data = fh.read()
        except OSError:
            raise Problem("JOB-FINAL-MISMATCH", "the saved final message %s cannot be read" % given)
        # one trailing newline is the editor's or `echo`'s, not the agent's (G3): compare without it,
        # and exactly otherwise
        if hashlib.sha256(_trim_newline(data)).hexdigest() != hashlib.sha256(
                _trim_newline(final.encode("utf-8"))).hexdigest():
            raise Problem("JOB-FINAL-MISMATCH", "the driver's saved final message differs from the "
                          "transcript's")


def _canon_hash(value) -> str:
    return util.sha256_hex(util.canonical_json(value))


def _input_hashes(value) -> tuple:
    """The hashes a transcript call's input may have in the pool: as the transcript holds it, and
    without the marker the guard puts in front of a Bash command (`updatedInput`, section 12.3):
    the pool line is written from the input the hook was given, the transcript may hold either."""
    value = value or {}
    out = [_canon_hash(value)]
    if isinstance(value, dict) and isinstance(value.get("command"), str):
        plain = agent_marker.unmark(value["command"])
        if plain != value["command"]:
            out.append(_canon_hash(dict(value, command=plain)))
    return tuple(out)


def check_pool(c: Ctx) -> None:
    """K12: the second witness of every tool call. Every pre line of the driver's pool file from
    the hand to the stop that names this agent must have its tool_use_id among the transcript's
    calls (no tolerance); a transcript call with no pool line, or one whose input hash differs,
    is counted, and the check fails when the two counts together pass max(3, 3 percent)."""
    from alpaca import pool
    ids = []
    inputs = {}
    for r in c.records:
        if r.get("type") == "assistant":
            for item in at.content_items(r):
                if item.get("type") == "tool_use" and item.get("id"):
                    ids.append(str(item["id"]))
                    inputs[str(item["id"])] = item.get("input")
    offset = int((c.handed or {}).get("pool_offset") or 0)
    lines = eng.tail_of(pool.path(c.root, c.driver_session or ""), offset)
    mine, by_id = [], {}
    id_set = set(ids)
    for ln in lines:
        if ln.get("phase") != "pre":
            continue
        tid = ln.get("tool_use_id")
        if ln.get("agent_id") == c.agent_id or tid in id_set:
            mine.append(ln)
            if tid:
                by_id[tid] = ln
    for ln in mine:
        if ln.get("agent_id") == c.agent_id and ln.get("tool_use_id") not in id_set:
            raise Problem("JOB-POOL-MISMATCH", "the pool holds a call of agent %s (%s) that the "
                          "transcript dropped" % (c.agent_id, ln.get("tool_use_id")))
    # a transcript with no pool witness at all is refused: the line of the prompt read, the first
    # call of every job, must be there and name this agent (adversarial check round 1, B3), so a
    # transcript a driver wrote by hand cannot hide inside the gap bound
    if ids:
        first = by_id.get(ids[0])
        if first is None or first.get("agent_id") != c.agent_id:
            raise Problem("JOB-POOL-MISMATCH", "the pool holds no line that names agent %s for its "
                          "first call (%s): no witness saw this agent work" % (c.agent_id, ids[0]))
    gaps = diffs = 0
    for tid in ids:
        ln = by_id.get(tid)
        if ln is None:
            gaps += 1
        elif ln.get("input_sha256") and ln["input_sha256"] not in _input_hashes(inputs.get(tid)):
            diffs += 1
    bound = max(POOL_GAP_FLOOR, int(POOL_GAP_RATE * len(ids)))
    c.store["pool_gaps"] = {"gaps": gaps, "diffs": diffs, "calls": len(ids)}
    if gaps + diffs > bound:
        raise Problem("JOB-POOL-MISMATCH", "%d calls have no pool line and %d differ in input, over "
                      "the bound %d" % (gaps, diffs, bound))


def check_config_k13(c: Ctx) -> None:
    """K13 at the judge: the engine pin by stat (JOB-ENGINE-CHANGED), then the config hash
    against the run's baseline (JOB-CONFIG-CHANGED)."""
    if c.replay:
        return
    check_pin(c.conn, c.root, c.scope, run=c.run, driver_session=c.driver_session, point="judge",
              job=c.job_id)
    check_config(c.conn, c.root, c.scope, c.drv)


def check_duplicate(c: Ctx) -> None:
    """K14: among the driver's agents registered after the hand, the first 2 KB of at most one
    transcript names this job's spawn prompt; more than one refuses the claim for all of them and
    books each as a stray."""
    if c.replay:
        return
    token = "ALPACA JOB %s NONCE" % c.job_id
    dup = []
    for ev in stop_events(c.conn, c.driver_session or "", c.handed_id or 0):
        aid = (ev["data"] or {}).get("agent_id")
        if not aid or aid == c.agent_id:
            continue
        loc = locator_for(c.conn, aid, ev)
        if loc and token in _head(loc):
            dup.append({"agent_id": aid, "locator": loc})
    if dup:
        raise Problem("JOB-DUPLICATE-SPAWN", "%d other agent(s) of the driver carry job %s's spawn "
                      "prompt" % (len(dup), c.job_id), extra={"strays": dup})


def check_scope(c: Ctx) -> None:
    """K15 (jobs scanned by `scope`): pathscan.outside over the calls with the shell base reset at
    every call, plus the control-plane names and paths of rule S15."""
    if c.job.get("scanned_by") != "scope":
        return
    from alpaca.research import pathscan
    shell = c.job.get("shell_cwd") or c.job.get("cwd")
    allowed = _own_paths(c)
    pads = session_scratchpads(c)
    # the session scratchpad lies under the temp root, outside the project root and the home, so it
    # is watched by name: since t-191 (F29) it is no job's own path and a write there is a breach
    watched = (os.path.realpath(c.root), os.path.abspath(c.root), eng.home()) + tuple(pads)
    hits = pathscan.outside(c.calls, cwd=shell, allowed=allowed, ignore_prefixes=(),
                            watched=watched, reset_cwd=shell)
    if hits:
        h = hits[0]
        hint = ""
        if any(eng.under(os.path.realpath(str(h["path"])), sp) for sp in pads):
            hint = " (the session scratchpad; use the job's scratch folder %s)" % c.job.get("scratch")
        raise Problem("JOB-SCOPE-BREACH", "call %s reached %s%s" % (h["line"], h["path"], hint))
    cfg = (c.drv["data"].get("config_dir") if c.drv else None)
    plane = control_hits_for(c, tuple(allowed), cfg)
    if plane:
        raise Problem("JOB-SCOPE-BREACH", "call %s %s" % (plane[0]["line"], plane[0]["why"]))


def control_hits_for(c: Ctx, allowed: tuple, cfg=None) -> list:
    scripts = (c.drv["data"].get("hook_scripts") if c.drv else None) or []
    shell = c.job.get("shell_cwd") or c.job.get("cwd")
    return eng.control_hits(c.calls, here=shell, root=c.root, hook_scripts=scripts, cfg=cfg,
                            allowed=allowed)


def check_group(c: Ctx) -> None:
    """K16 for a verifier job. With a barrier (the judge threads of a pair), the barrier runs it
    over both transcripts in both directions; without one the job is judged as a group of one."""
    if c.replay or not c.event.get("group"):
        return
    if c.barrier is not None:
        c.barrier.run_k16(c)
        return
    problems = k16_alone(c)
    if problems:
        raise problems


#: the most characters of a matched text, and of the text around it, a refusal records (F20)
MATCH_TEXT_MAX = 300
#: the most bytes of the sibling's packet the shared-input filter reads, and of one file of it
SHARED_PACKET_BYTES = 16 * 1024 * 1024
SHARED_FILE_BYTES = 2 * 1024 * 1024


def _side(rec: dict) -> str:
    """`own` for a record the agent wrote (an assistant turn: its text, thinking and tool calls),
    `in` for one that reached it (the spawn prompt, a tool result, an attachment, a hook body)."""
    return "own" if rec.get("type") == "assistant" else "in"


def first_seen(records: list, needles) -> dict:
    """{needle: (index, side)} of the first record of `records` whose text holds each needle; a
    needle that no record holds is left out."""
    rest = [x for x in dict.fromkeys(needles) if x]
    found = {}
    if not rest:
        return found
    texts = [at.record_text(r) for r in records]
    whole = "\n".join(texts)
    rest = [x for x in rest if x in whole]
    for i, t in enumerate(texts):
        if not rest:
            break
        if not t:
            continue
        still = []
        for x in rest:
            if x in t:
                found[x] = (i, _side(records[i]))
            else:
                still.append(x)
        rest = still
    return found


def shared_lines(folder: str | None) -> set:
    """The stripped lines of the files of a verifier's packet (`<try folder>/packet`): the
    sealed inputs both verifiers of a pair read (the brief, the claims, the evidence copies, the
    exported events, the frozen files), read up to SHARED_PACKET_BYTES. K16 reads the packet of
    the agent it scans: a line planted there to pass as shared input changes that packet, and the
    packet's re-hash after the pass voids the scanned agent's own verdict (VERIFY-PACKET-CHANGED,
    section 6.3 item 8), so the plant cannot hide a relay."""
    out = set()
    if not folder:
        return out
    left = SHARED_PACKET_BYTES
    for base, dirs, files in os.walk(os.path.join(folder, "packet")):
        dirs.sort()
        for name in sorted(files):
            if left <= 0:
                return out
            path = os.path.join(base, name)
            try:
                if os.path.islink(path) or not os.path.isfile(path):
                    continue
                with open(path, "rb") as fh:
                    data = fh.read(min(left, SHARED_FILE_BYTES))
            except OSError:
                continue
            left -= len(data)
            for ln in data.decode("utf-8", "replace").splitlines():
                ln = ln.strip()
                if len(ln) >= SIBLING_LINE:
                    out.add(ln)
    return out


def sibling_output(sib_job: dict, sib_records: list | None, *, final: str | None = None,
                   verdict_text: str | None = None, shared_folder: str | None = None) -> dict:
    """What only the other verifier of a pair made (K16, section 6.3 item 4; t-173 fix batch 3,
    F19): its identifiers (job id, nonce, key, launch id, try folder, receipts folder and verdict
    file path) as `tokens`, and as `lines` each line of at least SIBLING_LINE characters of its
    recorded verdict (`receipts/verdict.json`) and of its final message that is its own: a line
    of its packet, the sealed inputs both verifiers read, is not, and neither is a line that
    reached it before it wrote it (in its transcript the line first shows in a tool result, an
    attachment or the prompt: a line of the tree, the evidence or a receipt of a command both
    re-run). Both verifiers read the same packet, evidence and tree, so a line quoted from them is
    no sign that one saw the other's work (rr-002, refusals 78439, 78442, 78467, 78470).
    `shared_folder` is the try folder of the agent being scanned, whose packet holds the shared
    lines (shared_lines); with none the sibling's own folder stands in. Each entry is (text,
    what)."""
    tokens = []
    cwd = sib_job.get("cwd") or sib_job.get("workdir")
    for key, what in (("job", "job id"), ("nonce", "nonce"), ("key", "key")):
        v = sib_job.get(key)
        if v:
            tokens.append((str(v), what))
    if sib_job.get("key"):
        try:
            tokens.append((paths.launch_id(sib_job["key"]), "launch id"))
        except (ValueError, TypeError):
            pass
    if cwd:
        tokens += [(os.path.join(cwd, "receipts", "verdict.json"), "verdict file"),
                   (os.path.join(cwd, "receipts"), "receipts folder"), (cwd, "try folder")]
    if verdict_text is None and cwd:
        try:
            with open(os.path.join(cwd, "receipts", "verdict.json"), encoding="utf-8",
                      errors="replace") as fh:
                verdict_text = fh.read()
        except OSError:
            verdict_text = None
    if final is None and sib_records:
        final = at.final_message(sib_records)
    cands = []
    for text, what in ((verdict_text, "verdict file"), (final, "final message")):
        for ln in (text or "").splitlines():
            ln = ln.strip()
            if len(ln) >= SIBLING_LINE and ln not in [c[0] for c in cands]:
                cands.append((ln, what))
    shared = shared_lines(shared_folder or cwd)
    cands = [(ln, what) for ln, what in cands if ln not in shared]
    if sib_records:
        seen = first_seen(sib_records, [ln for ln, _w in cands])
        cands = [(ln, what) for ln, what in cands if seen.get(ln, (0, "own"))[1] != "in"]
    return {"tokens": [(t, w) for t, w in tokens if t and len(t) >= 6], "lines": cands}


def _origin(rec: dict) -> dict:
    """Where a record came from, for a refusal (F20): its type, the tool call a tool result
    answers, the kind and path of an attachment."""
    out = {"type": rec.get("type")}
    for item in at.content_items(rec):
        if item.get("type") == "tool_result" and item.get("tool_use_id"):
            out["tool_use_id"] = item.get("tool_use_id")
            break
        if item.get("type") == "tool_use":
            out["tool"] = item.get("name")
            out["tool_use_id"] = item.get("id")
            break
    att = rec.get("attachment") if isinstance(rec.get("attachment"), dict) else None
    if att:
        out["attachment"] = att.get("type")
        for key in ("path", "filename", "hookName"):
            if isinstance(att.get(key), str):
                out[key] = att[key][:MATCH_TEXT_MAX]
    return out


def _context(text: str, needle: str) -> str:
    k = text.find(needle)
    if k < 0:
        return ""
    a = max(0, k - 100)
    return text[a:k + len(needle) + 100][:MATCH_TEXT_MAX]


def match_info(records: list, index: int, needle: str, *, kind: str, what: str,
               side: str | None = None) -> dict:
    """The bounded record of a K16 match (F20): what matched (a nonce is shown by its hash), from
    which part of the sibling's output, and the record of this transcript that held it."""
    rec = records[index] if 0 <= index < len(records) else {}
    text = needle if what != "nonce" else "nonce sha256 %s" % hashlib.sha256(
        needle.encode("utf-8")).hexdigest()[:16]
    ctx = _context(at.record_text(rec), needle) if what != "nonce" else ""
    return {"kind": kind, "what": what, "text": text[:MATCH_TEXT_MAX], "record": index,
            "side": side or _side(rec), "origin": _origin(rec), "context": ctx}


def scan_for_sibling(records: list, sib: dict, job: dict):
    """K16's content rule for one transcript against the other verifier's output (`sib`, from
    sibling_output): a Problem with the match in `extra["match"]`, or None. An identifier of the
    sibling refuses wherever it shows, the agent's own calls included (naming the other try folder
    is a reach for it). A line of the sibling's own output refuses only where it reached this
    agent: the first record that holds it is a tool result, an attachment, a hook body or a
    prompt, and not this agent's own turn. Two agents that read the same claims may write the
    same verdict line (`{"id": "s1", "verdict": "supported", "reason": null,`); the line then
    shows first in the agent's own submit, and a later read of its own draft or verdict is the
    agent's own text coming back (t-173 fix batch 3, F19)."""
    tokens = [(t, w) for t, w in (sib.get("tokens") or [])]
    seen = first_seen(records, [t for t, _w in tokens])
    for t, what in tokens:
        if t in seen:
            i, side = seen[t]
            return Problem("JOB-SIBLING-REF", "the transcript names the %s of the other verifier "
                           "(record %d)" % (what, i), extra={"match": match_info(
                               records, i, t, kind="token", what=what, side=side)})
    lines = [(ln, w) for ln, w in (sib.get("lines") or [])]
    seen = first_seen(records, [ln for ln, _w in lines])
    for ln, what in lines:
        hit = seen.get(ln)
        if hit is not None and hit[1] == "in":
            i = hit[0]
            return Problem("JOB-SIBLING-REF", "a line of the other verifier's %s reached this agent "
                           "in record %d" % (what, i), extra={"match": match_info(
                               records, i, ln, kind="line", what=what, side="in")})
    return None


def sibling_tokens(conn, root: str, scope: str, job: dict, *, shared_folder: str | None = None) -> dict:
    """The output of the other slot's try of a verifier job's group (a group of one: the other
    slot's counted try, section 6.3 item 4), as sibling_output gives it, read from the record and
    the sibling's folders and its kept transcript when it has one; `shared_folder` is the scanned
    job's try folder (sibling_output)."""
    other = None
    for jid, j in fold(conn, scope).items():
        d = j["job"]
        if jid != job.get("job") and d.get("group") == job.get("group") and \
                d.get("verifier_slot") != job.get("verifier_slot"):
            other = (jid, j)
    if other is None:
        return {"tokens": [], "lines": []}
    jid, j = other
    sib = dict(j["job"], **(read_job(root, scope, jid) or {}))
    records = None
    kept = (j["result"] or {}).get("transcript")
    if kept and os.path.isfile(kept):
        try:
            records = at.read_records(kept)
        except OSError:
            records = None
    return sibling_output(sib, records, shared_folder=shared_folder)


def k16_alone(c: Ctx):
    """The sibling scan for one transcript: a Problem, or None."""
    sib = sibling_tokens(c.conn, c.root, c.scope, dict(c.event, **{"job": c.job_id}),
                         shared_folder=c.job.get("cwd"))
    return scan_for_sibling(c.records, sib, c.job)


def check_stragglers(c: Ctx) -> None:
    """K17: after the stop event the judge waits for the job's processes to end, then lists the
    processes of this user, other than its own, whose working folder or an open file lies under
    the job's folders. It kills only the job's own leftovers: a process that started after the job
    was handed and is no Claude Code process, no driver and no ancestor of one. A process that
    started before the hand (the owner's shell or editor in that folder), the driver, a Claude Code
    process and their ancestors are never killed and never refuse the claim; they are listed in the
    claim's finding (`stragglers_spared`) instead (fix batch 2, F7; k-replay-findings.md G6). For a
    job with write paths an own leftover also refuses the claim."""
    if c.replay:
        return
    folders = [p for p in [c.job.get("cwd"), c.job.get("scratch")]
               + list(c.job.get("write_paths") or []) if p]
    handed = parse_time((c.handed or {}).get("handed_at"))
    deadline = time.monotonic() + STRAGGLER_WAIT_S
    while True:
        own, spared = classify_processes(find_processes(folders), handed,
                                         driver_pid=((c.drv or {}).get("data") or {}).get("pid"))
        if not own or time.monotonic() >= deadline:
            break
        time.sleep(0.2)
    killed = []
    if own:
        from alpaca.research import procs
        for pid in own:
            try:
                killed.extend(procs.kill_tree(pid))
            except Exception:  # noqa: BLE001
                continue
    c.store["stragglers"] = own
    c.store["stragglers_spared"] = spared
    if own and c.job.get("write_paths"):
        raise Problem("JOB-STRAGGLER", "%d process(es) of the job outlive its stop: %s" % (
            len(own), ", ".join(str(p) for p in own[:5])),
            extra={"stragglers": own, "spared": spared})


def _ancestors(pid: int) -> set:
    out = set()
    while pid > 1 and pid not in out:
        out.add(pid)
        try:
            with open("/proc/%d/stat" % pid) as fh:
                pid = int(fh.read().rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            break
    return out


def process_start(pid: int) -> float | None:
    """The epoch second a process started: the boot-time clock of /proc/uptime and field 22 of
    /proc/<pid>/stat, taken against now. Both are on one clock and read in the same call, so it
    does not carry the whole-second error of the boot time."""
    try:
        with open("/proc/%d/stat" % pid) as fh:
            ticks = int(fh.read().rsplit(")", 1)[1].split()[19])
        with open("/proc/uptime") as fh:
            up = float(fh.read().split()[0])
        return time.time() - (up - ticks / os.sysconf("SC_CLK_TCK"))
    except (OSError, ValueError, IndexError):
        return None


def _argv_of(pid: int) -> list:
    try:
        with open("/proc/%d/cmdline" % pid, "rb") as fh:
            return [a.decode("utf-8", "replace") for a in fh.read().split(b"\0") if a]
    except OSError:
        return []


def is_claude_process(argv: list) -> bool:
    """A Claude Code process: its command is `claude` (or a `claude-*` binary), a `ccd-cli` build
    of the desktop app (`~/.claude/remote/ccd-cli/<version>`), or a node run of the claude-code
    package."""
    if not argv:
        return False
    base = os.path.basename(argv[0])
    text = " ".join(argv[:3])
    return base == "claude" or base.startswith("claude-") or "/ccd-cli/" in text \
        or "claude-code" in text or "/.claude/remote/" in text


def classify_processes(pids: list, handed, *, driver_pid=None) -> tuple:
    """(own, spared) for the pids K17 found: `own` are the pids to kill (started more than
    START_SLACK_S after `handed`, a datetime; none is killed when the hand time is unknown),
    `spared` is [{pid, why, cmd}] for the rest, each with the reason it was not killed. A start
    within START_SLACK_S after the hand is below the precision of the process clock, so its order
    is unknown and the process is spared: a job's own process starts long after its hand (the
    driver spawns the agent after next-job answers), and a process that may predate the hand is
    never killed."""
    cutoff = handed.timestamp() if handed is not None else None
    claude = {p for p in pids if is_claude_process(_argv_of(p))}
    protected = set()
    for p in claude:
        protected |= _ancestors(p)
    if driver_pid:
        try:
            protected |= _ancestors(int(driver_pid))
        except (TypeError, ValueError):
            pass
    own, spared = [], []
    for pid in pids:
        cmd = " ".join(_argv_of(pid))[:200]
        start = process_start(pid)
        if pid in claude:
            why = "a Claude Code process"
        elif pid in protected:
            why = "the driver, a Claude Code process or an ancestor of one"
        elif cutoff is None:
            why = "the hand time of the job is unknown"
        elif start is not None and start < cutoff:
            why = "started before the job was handed"
        elif start is not None and start < cutoff + START_SLACK_S:
            why = "started within %g s of the hand, closer than the process clock can order" \
                % START_SLACK_S
        else:
            own.append(pid)
            continue
        spared.append({"pid": pid, "why": why, "cmd": cmd})
    return own, spared


def find_processes(folders: list) -> list:
    """The pids of this user's processes, other than this process and its ancestors, whose cwd
    or an open file lies under one of `folders`."""
    me = os.getpid()
    skip = {me}
    pid = os.getppid()
    while pid > 1 and pid not in skip:
        skip.add(pid)
        try:
            with open("/proc/%d/stat" % pid) as fh:
                pid = int(fh.read().rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            break
    real = [os.path.realpath(f) for f in folders]
    out = []
    try:
        names = [n for n in os.listdir("/proc") if n.isdigit()]
    except OSError:
        return out
    uid = os.getuid()
    for n in names:
        pid = int(n)
        if pid in skip:
            continue
        try:
            if os.stat("/proc/%d" % pid).st_uid != uid:
                continue
            cwd = os.readlink("/proc/%d/cwd" % pid)
        except OSError:
            continue
        hit = any(eng.under(cwd, f) for f in real)
        if not hit:
            try:
                for fd in os.listdir("/proc/%d/fd" % pid):
                    try:
                        target = os.readlink("/proc/%d/fd/%s" % (pid, fd))
                    except OSError:
                        continue
                    if any(eng.under(target, f) for f in real):
                        hit = True
                        break
            except OSError:
                pass
        if hit:
            out.append(pid)
    return out


#: rounds of a reap: list, kill, list again while an own pid is still there
REAP_ROUNDS = 3


def reap(folders: list, since, *, bases, driver_pid=None) -> dict:
    """Kill the leftover processes of a dead try's cut (in-session-runtime.md section 28.2): the
    processes find_processes lists under `folders` that classify_processes calls own with `since`
    (the datetime of the dead cut's own event) in place of a hand time, so a process that started
    after the cut existed and still lives under it is the dead try's, while a Claude Code process,
    the driver, their ancestors and a process older than the cut are spared. A folder that is not
    strictly under one of `bases` is dropped first, so a bad recorded path never widens the reach.
    Each own pid is killed with procs.kill_tree(pid, lead_only=True), up to REAP_ROUNDS rounds
    while the lister still finds own pids. A best effort that bounds resources; correctness comes
    from the cut's own name. Returns {"killed", "spared", "left"} (and "error" on an exception);
    it never raises."""
    out = {"killed": [], "spared": [], "left": []}
    try:
        roots = [os.path.realpath(b) for b in ([bases] if isinstance(bases, str) else bases or [])]
        kept = []
        for f in folders or []:
            if not f:
                continue
            real = os.path.realpath(f)
            if any(real != b and eng.under(real, b) for b in roots):
                kept.append(f)
        if not kept or since is None:
            return out
        from alpaca.research import procs
        killed, spared = set(), {}
        own = []
        for _round in range(REAP_ROUNDS):
            own, sp = classify_processes(find_processes(kept), since, driver_pid=driver_pid)
            for s in sp:
                spared.setdefault(s["pid"], s)
            own = [p for p in own if p not in killed]
            if not own:
                break
            for pid in own:
                try:
                    killed.update(procs.kill_tree(pid, lead_only=True))
                except Exception:  # noqa: BLE001
                    continue
        else:
            own = classify_processes(find_processes(kept), since, driver_pid=driver_pid)[0]
        out["killed"] = sorted(killed)
        out["spared"] = [spared[p] for p in sorted(spared)]
        out["left"] = sorted(own)
    except Exception as exc:  # noqa: BLE001 - a reap never fails the step
        out["error"] = "%s: %s" % (type(exc).__name__, exc)
    return out


def check_intrusion(c: Ctx) -> None:
    """K18: for the roles whose slot thread is the only writer of its attempt's rows while the job
    is open, no research event has the attempt as ref, an id between the research-job row and now
    and a kind outside the job protocol; and no research-launch-end for the job's key was written
    by anyone but this wait."""
    if c.replay or c.event.get("role") in ("verifier",):
        return
    attempt = c.event.get("attempt")
    if attempt:
        marks = ",".join("?" for _ in PROTOCOL_KINDS)
        rows = conn_rows(c.conn, "SELECT id, kind FROM events WHERE ref=? AND id>? AND kind LIKE "
                         "'research-%%' AND kind NOT IN (%s) ORDER BY id" % marks,
                         [attempt, c.fold["event"]] + list(PROTOCOL_KINDS))
        own_launch = c.store.get("own_launch_id")
        for row in rows:
            if row["id"] == own_launch:
                continue
            if row["kind"] == events.LAUNCH and \
                    json_of(c.conn, row["id"]).get("key") == c.event.get("key"):
                continue
            raise Problem("JOB-RECORD-INTRUSION", "event %d (%s) for %s appeared during the job's "
                          "window" % (row["id"], row["kind"], attempt))
    ends = conn_rows(c.conn, "SELECT id, data FROM events WHERE kind=? AND json_extract(data, "
                     "'$.key')=?", [events.LAUNCH_END, c.event.get("key")])
    if ends:
        raise Problem("JOB-RECORD-INTRUSION", "a research-launch-end for %s (event %d) was written "
                      "by something other than this wait" % (c.event.get("key"), ends[0]["id"]))


def conn_rows(conn, sql: str, params) -> list:
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def json_of(conn, event_id: int) -> dict:
    row = conn.execute("SELECT data FROM events WHERE id=?", (event_id,)).fetchone()
    try:
        return json.loads(row["data"]) if row else {}
    except (TypeError, ValueError):
        return {}


def check_context(c: Ctx) -> None:
    """K19: the fingerprint of group M now equals the one the job's own hand event holds. A
    difference is a change inside the job's window and ends the job whoever made it; each changed
    path is put in a writer class, and a change of class agent or unattributed also writes the
    marker and ends the run."""
    if c.replay or c.drv is None:
        return
    ctx_data = (c.handed or {}).get("context")
    ledger = eng.load_ledger(ctx_data)
    if ledger is None:
        return
    cur, diff = context_now(c.conn, c.root, c.scope, c.drv, ledger, check_session=c.driver_session,
                            point="judge")
    if not diff:
        return
    hand_ev = {"kind": events.JOB_HANDED, "id": c.handed_id, "session": c.driver_session,
               "data": c.handed}
    classes = classify_changes(c.root, c.conn, c.scope, c.drv, diff, hand_ev)
    for ch in diff:
        ch["class"] = classes.get(os.path.normpath(ch["path"]), "unattributed")
    bad = [ch for ch in diff if ch["class"] != "driver"]
    extra = {"context_changes": [{k: ch.get(k) for k in ("path", "class", "old_sha256", "new_sha256")}
                                 for ch in diff]}
    if bad:
        write_marker(c.root, c.scope, kind="context", changed=diff,
                     pin_event=(newest_pin(c.conn, c.scope, c.run) or {}).get("id"),
                     ledger_event=c.handed_id, job=c.job_id, point="judge")
        extra.update(end_run=True, kind="context")
    raise Problem("JOB-CONTEXT-CHANGED", "%d file(s) the runtime loads into an agent changed during "
                  "the job: %s" % (len(diff), ", ".join(ch["path"] for ch in diff[:3])), extra=extra)


#: the checks of section 5, in the order they run; the first failure ends the claim. A table, so a
#: test can take one out at a time
CHECKS = (("state", check_state), ("nonce", check_nonce), ("deadline", check_deadline),
          ("witness", check_witness), ("transcript", check_transcript),
          ("first-turn", check_first_turn), ("prompt-read", check_prompt_read),
          ("relay", check_relay), ("tools", check_tools), ("model", check_model),
          ("final", check_final), ("pool", check_pool), ("config", check_config_k13),
          ("duplicate", check_duplicate), ("scope", check_scope), ("group", check_group),
          ("stragglers", check_stragglers), ("intrusion", check_intrusion),
          ("context", check_context))
#: the K number of each check, by name
K_OF = {name: "K%d" % (i + 1) for i, (name, _f) in enumerate(CHECKS)}


def run_checks(c: Ctx, checks=None) -> list:
    """Run the checks in order; the names of those that ran, or a Problem whose `extra["check"]`
    names the check that raised it (F20)."""
    table = checks if checks is not None else CHECKS
    names = []
    for name, fn in table:
        try:
            fn(c)
        except Problem as exc:
            exc.extra.setdefault("check", name)
            raise
        names.append(name)
    c.checks_run = names
    return names


#: the checks that wait for another job's claim, so a diagnosis does not run them after a failure
_NEEDS_SIBLING = ("group",)


def run_checks_all(c: Ctx, checks=None) -> tuple:
    """Evaluate every check, whatever fails (`alpaca research submit --diagnose`, fix batch 2, F11):
    (rows, first), where rows is one dict per check in order {check, k, status, code, detail,
    retriable, after_failure} and first is the Problem the plain run would have refused with, or
    None. `status` is PASS, FAIL, BLOCKED (a retriable refusal), ERROR (a bug in the check, which
    refuses like any failure) or SKIPPED (a check that waits for the sibling's claim, run only while
    nothing has failed). A check after the first failure may fail because of it (the transcript copy
    that K5 did not make, the stop row that K4 did not find): `after_failure` says so. Every check
    runs against the same claim and the same record, so one failing job lists every gap."""
    table = checks if checks is not None else CHECKS
    rows, first, passed = [], None, []
    for name, fn in table:
        row = {"check": name, "k": K_OF.get(name), "status": "PASS", "code": None, "detail": None,
               "retriable": False, "after_failure": first is not None}
        if first is not None and name in _NEEDS_SIBLING and c.barrier is not None:
            row.update(status="SKIPPED", detail="waits for the sibling's claim; not run after a failure")
            rows.append(row)
            continue
        try:
            fn(c)
            passed.append(name)
        except Problem as exc:
            exc.extra.setdefault("check", name)
            row.update(status="BLOCKED" if exc.retriable else "FAIL", code=exc.code,
                       detail=exc.detail[:600], retriable=bool(exc.retriable))
            if first is None:
                first = exc
        except Exception as exc:  # noqa: BLE001 - a bug in a check refuses the claim, never accepts it
            detail = "%s: %s" % (type(exc).__name__, exc)
            row.update(status="ERROR", code="JOB-STATE", detail=detail[:600])
            if first is None:
                first = Problem("JOB-STATE", "the judge could not read the claim: %s" % detail,
                                extra={"check": name})
        rows.append(row)
    if first is None:
        c.checks_run = passed
    return rows, first


def render_table(rows: list) -> list:
    """The diagnosis as text lines: `K4  witness  FAIL  JOB-NO-STOP  detail`."""
    out = []
    for r in rows or []:
        out.append("  %-4s %-11s %-8s %s%s%s" % (
            r.get("k") or "-", r.get("check"), r.get("status"), r.get("code") or "",
            ("  " + str(r.get("detail"))) if r.get("detail") else "",
            "  (after the first failure)" if r.get("after_failure") and r.get("status") != "PASS" else ""))
    return out


REPLAY_CHECKS = ("transcript", "first-turn", "prompt-read", "relay", "tools", "model", "pool")


def verify_kept(conn, root: str, end_event: dict) -> bool:
    """Re-run the checks that need only files on the record's side on the kept copy of the
    transcript (the hash against the result's transcript_sha256, K6 to K10 and K12); a
    launch-end written by something else, with no kept transcript that passes, is not a result
    (section 5.4, 12.3)."""
    d = end_event["data"]
    job_id = d.get("job")
    if not job_id:
        return False
    ev = find_job_event(conn, job_id)
    if ev is None:
        return False
    scope = ev["data"]["scope"]
    j = fold(conn, scope).get(job_id)
    if j is None or j["result"] is None or j["result"].get("launch_end") != end_event["id"]:
        return False
    result = j["result"]
    kept = result.get("transcript")
    try:
        c = Ctx(conn, root, scope, job_id, {"agent_id": result.get("agent_id")}, replay=True,
                kept=kept, result=result)
        for name, fn in CHECKS:
            if name in REPLAY_CHECKS:
                fn(c)
    except Problem:
        return False
    except Exception:  # noqa: BLE001 - a file that cannot be read is not a result
        return False
    return True


# ================================================================ the end
_CLASSIFY = ("quota", "auth", "crash")


def classify_end(c: Ctx) -> tuple:
    """(outcome, infra, reason, reset_at) of the end the judge writes (section 5.2)."""
    from alpaca.research import launcher
    err = at.api_error(c.records)
    if err is not None:
        text = err["text"]
        if str(err.get("error") or "") == "rate_limit" or launcher._QUOTA.search(text or ""):
            return "quota", "quota", text[:300], launcher._reset_at(text)
        if launcher._AUTH.search(text or ""):
            return "auth", "auth", text[:300], None
        return "crash", "crash", text[:300], None
    final = c.store.get("final")
    if final is None:
        return "crash", "crash", "the agent ended with no final message", None
    first, last = at.span(c.records)
    timeout = int(c.event.get("timeout_s") or 0)
    if first and last and timeout and (last - first).total_seconds() > timeout:
        return "timeout", None, "the agent ran %.0f seconds, over %d" % (
            (last - first).total_seconds(), timeout), None
    return "ok", None, "", None


def commit_accept(conn, root: str, c: Ctx, *, session: str) -> dict:
    """Write research-job-result and research-launch-end (and research-infra for an error end) in
    one transaction with the spend booking. Returns the launch end event and the result data."""
    from alpaca.research import launcher, spend
    outcome, infra, reason, reset_at = classify_end(c)
    d = c.event
    spec_session = c.job.get("launch_session") or session
    usage = measure_usage(conn, root, spec_session, c.copy_path)
    final = c.store.get("final")
    final_sha = hashlib.sha256(final.encode("utf-8")).hexdigest() if final is not None else None
    evidence = c.store.get("evidence") or os.path.dirname(c.copy_path)
    first, last = at.span(c.records)
    duration = round((last - first).total_seconds(), 3) if first and last else 0.0
    truncated = final is not None and len(final) > launcher._FINAL_MAX
    if truncated:
        with open(os.path.join(evidence, "final-message.txt"), "wb") as fh:
            fh.write(final.encode("utf-8"))
    elif final is not None:
        with open(os.path.join(evidence, "final-message.txt"), "wb") as fh:
            fh.write(final.encode("utf-8"))
    launch_ev = conn.execute("SELECT id FROM events WHERE kind=? AND json_extract(data, "
                             "'$.key')=? ORDER BY id DESC LIMIT 1",
                             (events.LAUNCH, d.get("key"))).fetchone()
    models = c.store.get("models_seen") or []
    unp = unpriced_models(root, models)
    band = {"ok": vc.PASS, "timeout": vc.FAIL}.get(outcome, vc.BLOCKED)
    witness = (c.drv["data"] or {}).get("witness") if c.drv else None
    end_data = {"key": d.get("key"), "attempt": d.get("attempt"), "role": d.get("role"),
                "session": spec_session, "try": d.get("try"),
                "launch": job_launch_id(d), "outcome": outcome, "rc": 0 if outcome in ("ok", "timeout")
                else None, "timed_out": outcome == "timeout", "duration_s": duration,
                "stream": c.copy_path, "stream_sha256": c.store.get("kept_sha256"),
                "transcript": c.copy_path, "transcript_sha256": c.store.get("kept_sha256"),
                "egress_log": None, "egress_sha256": None, "egress_refused": 0,
                "tokens": usage["tokens"], "cost_usd": usage["cost_usd"], "priced": usage["priced"],
                "usage_sources": usage["sources"], "killed_pids": [], "model": usage["model"],
                "home": None, "final_message": final[:launcher._FINAL_MAX] if final is not None else None,
                "final_message_sha256": final_sha, "final_message_truncated": truncated,
                "redacted": [], "not_redacted": {"count": 0, "paths": [], "walk_stopped": []},
                "infra": infra, "reason": reason, "reset_at": reset_at,
                "launch_event": launch_ev["id"] if launch_ev else None, "mode": "session",
                "job": c.job_id, "agent_id": c.agent_id, "witness": witness, "binary": None,
                "binary_sha256": None, "jail": None}
    result_data = {"job": c.job_id, "scope": c.scope, "agent_id": c.agent_id,
                   "transcript": c.copy_path, "transcript_sha256": c.store.get("kept_sha256"),
                   "source_transcript": c.store.get("source_path"),
                   "source_sha256": c.store.get("source_sha256"), "final_sha256": final_sha,
                   "claimed_usage": c.claim.get("usage"), "measured_usage": {
                       "tokens": usage["tokens"], "cost_usd": usage["cost_usd"],
                       "priced": usage["priced"], "model": usage["model"],
                       "tokens_total": usage.get("tokens_total"),
                       "cache_read_tokens": usage.get("cache_read_tokens")},
                   "checks": list(c.checks_run), "outcome": outcome,
                   "models_seen": models, "tools_seen": c.store.get("tools_seen") or [],
                   "runtime_version": at.version(c.records), "started": _iso(first) if first else None,
                   "ended": _iso(last) if last else None, "pool_gaps": c.store.get("pool_gaps"),
                   "stragglers": c.store.get("stragglers") or [],
                   "stragglers_spared": c.store.get("stragglers_spared") or [],
                   "unpriced_models": unp,
                   "attachments": c.store.get("attachments") or [],
                   "attachment_kinds": c.store.get("attachment_kinds") or {},
                   "tool": c.claim.get("tool"), "tool_use_id": c.claim.get("tool_use_id"),
                   "n": c.claim.get("n"), "stop_event": (c.stop_ev or {}).get("id"),
                   "final_message_sha256": final_sha}
    with db.transaction(conn):
        end = events.emit(conn, events.LAUNCH_END, run=c.run, ref=d.get("attempt") or c.run or c.scope,
                          op=d.get("op"), session=spec_session, code=band, data=end_data, in_txn=True)
        result_data["launch_end"] = end["id"]
        events.emit(conn, events.JOB_RESULT, run=c.run, ref=d.get("attempt") or c.scope,
                    op=d.get("op"), session=session, actor="research-runner", data=result_data,
                    in_txn=True)
        if infra:
            events.emit(conn, events.INFRA, run=c.run, ref=d.get("attempt") or c.run or c.scope,
                        op=d.get("op"), session=spec_session, code=vc.BLOCKED, in_txn=True,
                        data={"attempt": d.get("attempt"), "role": d.get("role"), "try": d.get("try"),
                              "class": infra, "detail": str(reason)[:2000],
                              "wait_until": launcher._wait_until(reset_at, int(d.get("try") or 1))
                              if infra == "quota" else None, "key": d.get("key"),
                              "launch": job_launch_id(d), "job": c.job_id})
        spend.book(conn, c.run, d.get("attempt"), cost_usd=usage["cost_usd"], tokens=usage["tokens"],
                   session=spec_session, in_txn=True)
    return {"end": end, "end_data": end_data, "result": result_data, "usage": usage,
            "outcome": outcome, "infra": infra, "unpriced_models": unp}


#: the most characters of a refusal's detail that its row records
DETAIL_MAX = 2000


def problem_row(problem: Problem) -> dict:
    """The refusal's entry of `research-job-refused.problems` (F20): `check` is the reason code
    (the key report and status tally), `name` and `k` the check that raised it, `detail` its
    text, and for K16 `match`: what matched, from which part of the sibling's output, and the
    record of the transcript that held it, each bounded (MATCH_TEXT_MAX)."""
    name = problem.extra.get("check")
    row = {"check": problem.code, "name": name, "k": K_OF.get(name), "code": problem.code,
           "detail": str(problem.detail)[:DETAIL_MAX]}
    match = problem.extra.get("match")
    if isinstance(match, dict):
        row["match"] = {k: (v[:MATCH_TEXT_MAX] if isinstance(v, str) else v)
                        for k, v in match.items()}
    return row


def job_launch_id(d: dict) -> str:
    try:
        return paths.launch_id(d["key"])
    except (ValueError, KeyError):
        return d.get("job")


def commit_refuse(conn, root: str, c: Ctx, problem: Problem, *, session: str) -> dict:
    """Write research-job-refused and research-infra class lost in one transaction, and book the
    agent's cost from its transcript when it could be read (section 5.4). The slot then retries
    with the next key."""
    from alpaca.research import spend
    d = c.event
    spec_session = c.job.get("launch_session") or session
    usage, strays = None, []
    path = c.copy_path or c.store.get("source_path")
    if path and os.path.isfile(path) and c.agent_id:
        try:
            usage = measure_usage(conn, root, spec_session, path)
        except Exception:  # noqa: BLE001
            usage = None
    for s in problem.extra.get("strays") or []:
        try:
            spend_info = measure_usage(conn, root, "%s-stray-%s" % (spec_session, s["agent_id"][:8]),
                                       s["locator"])
        except Exception:  # noqa: BLE001
            spend_info = None
        strays.append(dict(s, usage=spend_info))
    data = {"job": c.job_id, "scope": c.scope, "problems": [problem_row(problem)],
            "retriable": False, "key": d.get("key"), "agent_id": c.agent_id,
            "strays": strays, "n": c.claim.get("n")}
    if problem.extra.get("table"):
        data["diagnosis"] = problem.extra["table"]       # every check, from `submit --diagnose`
    if usage is not None:
        data["measured_usage"] = {"tokens": usage["tokens"], "cost_usd": usage["cost_usd"],
                                  "priced": usage["priced"], "model": usage["model"],
                                  "tokens_total": usage.get("tokens_total"),
                                  "cache_read_tokens": usage.get("cache_read_tokens")}
        data["priced"] = usage["priced"]
        data["tokens"] = usage["tokens"]
        data["cost_usd"] = usage["cost_usd"]
    for k in ("context_changes", "context_unlisted"):
        if k in problem.extra:
            data[k] = problem.extra[k]
    if problem.extra.get("unlisted"):
        data["context_unlisted"] = problem.extra["unlisted"]
    with db.transaction(conn):
        events.emit(conn, events.JOB_REFUSED, run=c.run, ref=d.get("attempt") or c.scope,
                    op=d.get("op"), session=session, actor="research-runner", code=vc.FAIL,
                    reason=problem.code, data=data, in_txn=True)
        events.emit(conn, events.INFRA, run=c.run, ref=d.get("attempt") or c.run or c.scope,
                    op=d.get("op"), session=spec_session, code=vc.BLOCKED, in_txn=True,
                    data={"attempt": d.get("attempt"), "role": d.get("role"), "try": d.get("try"),
                          "class": "lost", "detail": "%s: %s" % (problem.code, problem.detail[:1800]),
                          "wait_until": None, "key": d.get("key"), "launch": job_launch_id(d),
                          "job": c.job_id})
        if usage is not None and usage.get("priced"):
            spend.book(conn, c.run, d.get("attempt"), cost_usd=usage["cost_usd"],
                       tokens=usage["tokens"], session=spec_session, in_txn=True)
        for s in strays:
            u = s.get("usage")
            if u and u.get("priced"):
                spend.book(conn, c.run, d.get("attempt"), cost_usd=u["cost_usd"],
                           tokens=u["tokens"], session=spec_session, in_txn=True)
    return data


# ================================================================ expiry and late results
def expire(conn, root: str, scope: str, job_id: str, reason: str, *, session: str) -> bool:
    """End a job with no accepted result: research-job-expired and research-infra, class
    start-failed for a job never handed (nothing ran) and lost for a handed one. False when the
    job already ended."""
    j = fold(conn, scope).get(job_id)
    if j is None or not is_open(j):
        return False
    d = j["job"]
    handed = j["handed"] is not None
    spec_session = (read_job(root, scope, job_id) or {}).get("launch_session") or session
    with db.transaction(conn):
        events.emit(conn, events.JOB_EXPIRED, run=d.get("run"), ref=d.get("attempt") or scope,
                    op=d.get("op"), session=session, actor="research-runner",
                    data={"job": job_id, "scope": scope, "reason": reason, "key": d.get("key")},
                    in_txn=True)
        events.emit(conn, events.INFRA, run=d.get("run"), ref=d.get("attempt") or d.get("run") or scope,
                    op=d.get("op"), session=spec_session, code=vc.BLOCKED, in_txn=True,
                    data={"attempt": d.get("attempt"), "role": d.get("role"), "try": d.get("try"),
                          "class": "lost" if handed else "start-failed",
                          "detail": "job %s expired (%s)" % (job_id, reason), "wait_until": None,
                          "key": d.get("key"), "launch": job_launch_id(d), "job": job_id})
    return True


def expire_open(conn, root: str, scope: str, reason: str, *, session: str) -> list:
    out = []
    for jid in list(open_jobs(conn, scope)):
        if expire(conn, root, scope, jid, reason, session=session):
            out.append(jid)
    return out


def expire_priced(conn, root: str, scope: str, reason: str, *, session: str) -> list:
    """expire_open, then reconcile: an agent that already ended is priced at once, so the retry that
    the plan makes next does not meet its own lost launch as unpriced spend and hold the run
    (section 10). An agent still running is priced when its stop is read."""
    out = expire_open(conn, root, scope, reason, session=session)
    if out:
        try:
            reconcile(conn, root, scope, session=session)
        except Exception:  # noqa: BLE001 - pricing is best effort; the owner's grant is the way out
            pass
    return out


def reconcile(conn, root: str, scope: str, *, session: str) -> list:
    """Price the agents of expired jobs (section 10): for each expired job that was handed, look
    among the driver's agents registered after the hand for a transcript whose first user turn
    names the job, and when that agent has a stop event copy and hash the file, read the cost as
    the judge does and record research-job-late with priced true. It records no launch end: the
    agent passed none of the checks, and a recorded end would be taken as a launch to judge by
    the verifier's recovery path."""
    from alpaca.research import spend
    out = []
    for jid, j in fold(conn, scope).items():
        if j["state"] != "expired" or j["handed"] is None:
            continue
        if any(l.get("priced") for l in j["late"]):
            continue
        for aid in agents_for(conn, root, scope, j):
            if any(l.get("agent_id") == aid for l in j["late"]):
                continue
            ev = next((e for e in stop_events(conn, (j["handed"] or {}).get("session") or "",
                                              j["handed_id"] or 0)
                       if (e["data"] or {}).get("agent_id") == aid), None)
            loc = locator_for(conn, aid, ev)
            if ev is None:
                # no stop row: a stopped or expired agent, or one still running. Its transcript is
                # priced once it has been quiet for STOP_IDLE_S, so a running agent is not priced
                # on a file that is still growing; a later reconcile reads it again
                loc = loc or find_transcript(conn, root, (j["handed"] or {}).get("session") or "", aid)
                try:
                    if loc and time.time() - os.path.getmtime(loc) < STOP_IDLE_S:
                        continue
                except OSError:
                    continue
            if not loc or not os.path.isfile(loc):
                continue
            d = j["job"]
            jobd = read_job(root, scope, jid) or {}
            evidence = os.path.join(paths.job_dir(root, scope, jid), "late")
            os.makedirs(evidence, exist_ok=True)
            dst = os.path.join(evidence, "agent-%s.jsonl" % aid)
            shutil.copyfile(loc, dst)
            spec_session = jobd.get("launch_session") or session
            usage = measure_usage(conn, root, "%s-late-%s" % (spec_session, aid[:8]), dst)
            with db.transaction(conn):
                events.emit(conn, events.JOB_LATE, run=d.get("run"), ref=d.get("attempt") or scope,
                            op=d.get("op"), session=session, actor="research-runner",
                            data={"job": jid, "scope": scope, "key": d.get("key"), "agent_id": aid,
                                  "transcript": dst, "transcript_sha256": eng.sha256_file(dst),
                                  "measured_usage": {k: usage[k] for k in ("tokens", "cost_usd",
                                                                          "priced", "model")},
                                  "priced": usage["priced"], "tokens": usage["tokens"],
                                  "cost_usd": usage["cost_usd"]}, in_txn=True)
                if usage["priced"]:
                    spend.book(conn, d.get("run"), d.get("attempt"), cost_usd=usage["cost_usd"],
                               tokens=usage["tokens"], session=spec_session, in_txn=True)
            out.append({"job": jid, "agent_id": aid})
    out += price_strays(conn, root, scope, session=session)
    return out


def new_nonce() -> str:
    return secrets.token_hex(16)
