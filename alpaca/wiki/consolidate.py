"""The bounded consolidation job (design/rune2-memory/architecture.md 9.2; FR-16, FR-18; OB-23).

No daemon and no model. One step reads at most `MAX_ITEMS` record events after its checkpoint and
turns two patterns into observation candidates that cite their sources and quote them:

* a failure signature that repeated: two record events of a `*-failed` kind whose error text is the
  same once digits are masked;
* a superseded decision: a `decision` event whose text says `Supersedes dec-<id>`.

Each becomes a declarative observation proposed through `service.propose` under the session
`job:rune2-consolidate`. The text of a candidate is lifted from the quoted spans, so it passes the
token check of K-3 (OB-13). The job never calls `admit`, `lessons.write` or `KnowledgeStore`, never
writes the record itself, and a job session cannot admit (architecture.md 7.4): a live session
agent reviews the candidate, gives its gate metadata and admits it.

The checkpoint is the `obs_consumer_checkpoint` row `rune2-consolidate`. It is written, with the
`rune2-consolidate-failure` events, by the caller that binds the record appender: `run_step` below,
which the `wiki consolidate` verb, the SessionEnd hook and the collector's `wiki` consumer call (the
last two only when `consolidate_after_capture` is true, default false).

A failing item is tried at most three times. Each step seeds an item's `attempts` from the
failure events; at three attempts, or for a class that cannot change on its own, the item is
reported with `retryable` false, is not tried again, gets no new event, no longer holds the
checkpoint and stays listed as failed (OB-23).
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import re
import time

from alpaca import db as recdb
from .api import JobResult

CONSUMER = "rune2-consolidate"
SESSION = "job:rune2-consolidate"
FAILURE_KIND = "rune2-consolidate-failure"
MAX_ITEMS = 200                      # record events per step (provisional, architecture.md 9.4)
MAX_ATTEMPTS = 3
REPEAT_MIN = 2                       # occurrences that make a failure signature "repeated"
#: Failure classes that can pass without anyone acting: the cited note is not captured yet, or the
#: store was busy. Every other class (a hidden source, a quote that is not in its span, a refused
#: check) is the same on the next try, so the item is reported once and left.
RETRYABLE = frozenset({"anchor-unresolved", "store-unavailable"})
LINE_CHARS = 300                     # a quoted error line (an observation holds 400 characters)

_DEC_ID = re.compile(r"^dec-[0-9a-f]+$")
_SUPERSEDES = re.compile(r"\bSupersedes\s+(dec-[0-9a-f]+)", re.I)
_DIGITS = re.compile(r"\d+")
FALSIFY_FAILURE = ("observe: a later event note shows this error absent, or a different error, for "
                   "the same failure kind")
FALSIFY_SUPERSEDED = ("observe: a later decision reaffirms the superseded choice, or the superseding "
                      "choice is absent from the record")


def _data(raw) -> dict:
    try:
        out = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
    except ValueError:
        return {}
    return out if isinstance(out, dict) else {}


def retryable(cls: str, attempts: int) -> bool:
    return cls in RETRYABLE and attempts < MAX_ATTEMPTS


def failure_state(source) -> dict:
    """key -> {class, attempts, candidate_id} from the `rune2-consolidate-failure` events: the most
    attempts seen and the class and candidate id of the latest event. `source` is a read-only
    record connection."""
    out: dict = {}
    for row in source.execute("SELECT data FROM events WHERE kind=? ORDER BY id", (FAILURE_KIND,)):
        d = _data(row[0])
        key = d.get("key")
        if not isinstance(key, str):
            continue
        attempts = d.get("attempts") if isinstance(d.get("attempts"), int) else 1
        old = out.get(key, {"attempts": 0})
        out[key] = {"class": str(d.get("class")), "attempts": max(old["attempts"], attempts),
                    "candidate_id": d.get("candidate_id")}
    return out


def failed_items(root) -> list:
    """The failed items `wiki status` lists (architecture.md 9.3, FR-18): key, class, attempts and
    retryable, oldest first. An item that may still pass and whose candidate has been proposed since
    is not listed; one that is non-retryable or at three attempts stays listed."""
    from .config import Config
    from .store.db import open_db_readonly
    source = recdb.connect_readonly(str(root))
    try:
        state = failure_state(source)
    finally:
        source.close()
    if not state:
        return []
    proposed: set = set()
    cfg = Config.for_project(root, resolve=False)   # a status read (t-070 D-4)
    if cfg.db_path.exists():
        conn = open_db_readonly(cfg)
        try:
            if conn.execute("SELECT 1 FROM sqlite_master WHERE name='candidates'").fetchone():
                proposed = {r[0] for r in conn.execute(
                    "SELECT candidate_id FROM candidates WHERE status<>'refused'")}
        finally:
            conn.close()
    out = []
    for key, st in state.items():
        can = retryable(st["class"], st["attempts"])
        if can and st.get("candidate_id") in proposed:
            continue
        out.append({"key": key, "class": st["class"], "attempts": st["attempts"], "retryable": can})
    return out


# ------------------------------------------------------------------ finding items

def _error_line(data: dict) -> str | None:
    err = data.get("error")
    if not isinstance(err, str) or not err.strip():
        return None
    line = " ".join(err.split("\n")[0].split())[:LINE_CHARS].strip()
    return line or None


def _signature(kind: str, line: str) -> str:
    return kind + "|" + _DIGITS.sub("#", line).casefold()


def _anchor(row, quote: str) -> dict:
    return {"source_ref": "event:%d" % row["id"], "quote": quote, "hash": row["hash"]}


def _sentence_anchors(text: str, n: int) -> list:
    from .intake import _sentences
    return [list(range(n))] * len(_sentences(text))


def _failure_items(source, low: int, high: int) -> list:
    """Failure signatures whose second occurrence lies in (low, high]: each repeated signature is
    emitted once, in the window of its second occurrence, citing its first two occurrences, so a
    third event does not make another candidate."""
    groups: dict = {}
    for row in source.execute("SELECT id,kind,hash,data FROM events WHERE kind LIKE '%-failed' "
                              "AND id<=? ORDER BY id", (high,)):
        line = _error_line(_data(row["data"]))
        if line is not None:
            groups.setdefault(_signature(row["kind"], line), []).append((row, line))
    items = []
    for sig, found in groups.items():
        if len(found) < REPEAT_MIN or not (low < found[REPEAT_MIN - 1][0]["id"] <= high):
            continue
        first = found[:REPEAT_MIN]
        quotes = ["error: " + line for _row, line in first]
        text = quotes[0]
        key = "failure:%s:%s" % (first[0][0]["kind"], hashlib.sha256(sig.encode("utf-8")).hexdigest()[:12])
        items.append({"key": key, "trigger": first[-1][0]["id"],
                      "proposal": {"kind": "observation", "text": text,
                                   "anchors": [_anchor(r, q) for (r, _l), q in zip(first, quotes)],
                                   "sentence_anchors": _sentence_anchors(text, len(first)),
                                   "observation": {"falsifiability": FALSIFY_FAILURE}}})
    return items


def _superseded_items(source, low: int, high: int) -> list:
    items, seen = [], set()
    for row in source.execute("SELECT id,hash,data FROM events WHERE kind='decision' AND id>? AND id<=? "
                              "ORDER BY id", (low, high)):
        d = _data(row["data"])
        new = d.get("id")
        if not isinstance(new, str) or not _DEC_ID.match(new):
            continue
        blob = " ".join(str(d.get(k) or "") for k in ("context", "choice", "consequence"))
        for m in _SUPERSEDES.finditer(blob):
            old = m.group(1)
            if old == new or (new, old) in seen:
                continue
            seen.add((new, old))
            text = "%s supersedes %s." % (new, old)
            items.append({"key": "supersede:%s:%s" % (new, old), "trigger": row["id"],
                          "proposal": {"kind": "observation", "text": text,
                                       "anchors": [_anchor(row, "id: " + new), _anchor(row, m.group(0))],
                                       "sentence_anchors": _sentence_anchors(text, 2),
                                       "observation": {"falsifiability": FALSIFY_SUPERSEDED}}})
    return items


# ------------------------------------------------------------------ the job

def _checkpoint(source) -> int:
    if not source.execute("SELECT 1 FROM sqlite_master WHERE name='obs_consumer_checkpoint'").fetchone():
        return 0
    row = source.execute("SELECT event_id FROM obs_consumer_checkpoint WHERE consumer=?",
                         (CONSUMER,)).fetchone()
    return int(row[0]) if row else 0


def _classify(exc: BaseException) -> str:
    import sqlite3
    if isinstance(exc, (sqlite3.OperationalError, OSError)):
        return "store-unavailable"
    if isinstance(exc, ValueError):
        return "malformed-proposal"
    return "error-" + type(exc).__name__


class ConsolidateJob:
    """`Job` of interfaces.py.txt: `step` proposes and reports; the caller writes the checkpoint and
    the failure events."""
    consumer = CONSUMER
    max_items = MAX_ITEMS

    def step(self, root, *, deadline_s: float, record, clock=time.monotonic) -> JobResult:
        from . import intake, service
        started = clock()
        source = _readonly(root)
        try:
            checkpoint = _checkpoint(source)
            state = failure_state(source)
            row = source.execute("SELECT MAX(id) FROM (SELECT id FROM events WHERE id>? ORDER BY id "
                                 "LIMIT ?)", (checkpoint, MAX_ITEMS)).fetchone()
            high = int(row[0]) if row and row[0] is not None else checkpoint
            items = sorted(_failure_items(source, checkpoint, high) + _superseded_items(source, checkpoint, high),
                           key=lambda i: (i["trigger"], i["key"]))
            head = source.execute("SELECT COALESCE(MAX(id),0) FROM events").fetchone()[0]
        finally:
            source.close()
        failed: dict = {key: {"key": key, "class": st["class"], "retryable": retryable(st["class"], st["attempts"]),
                              "attempts": st["attempts"], "attempted": False}
                        for key, st in state.items() if not retryable(st["class"], st["attempts"])}
        proposed, done, hold = [], 0, None
        known = _proposed_ids(root)
        for item in items:
            prior = state.get(item["key"])
            if prior is not None and not retryable(prior["class"], prior["attempts"]):
                continue                             # reported above; not tried, no new event
            if clock() - started >= deadline_s:
                hold = item["trigger"] if hold is None else min(hold, item["trigger"])
                continue
            cid = reason = None
            try:
                cid = intake.candidate_id(intake.parse_proposal(item["proposal"]))
                if cid in known:
                    done += 1
                    continue
                receipt = service.propose(root, item["proposal"], session=SESSION, record=record)
                if receipt.status == "refused":
                    bad = next((c for c in receipt.checks if not c.passed), None)
                    reason = bad.reason if bad is not None else "refused"
                else:
                    proposed.append(receipt.candidate_id)
                    known.add(receipt.candidate_id)
            except Exception as exc:                 # one bad item never stops the step
                reason = _classify(exc)
            done += 1
            if reason is None:
                continue
            attempts = (prior["attempts"] if prior else 0) + 1
            can = retryable(reason, attempts)
            failed[item["key"]] = {"key": item["key"], "class": reason, "retryable": can,
                                   "attempts": attempts, "attempted": True, "candidate_id": cid}
            if can:
                hold = item["trigger"] if hold is None else min(hold, item["trigger"])
        checkpoint_id = (hold - 1) if hold is not None else max(checkpoint, high)
        checkpoint_id = max(checkpoint, checkpoint_id)
        remaining = _count_after(root, checkpoint_id, head)
        return JobResult(done=done, remaining=remaining, proposed=tuple(proposed),
                         failed=tuple(failed.values()), checkpoint_event_id=checkpoint_id)


def _readonly(root):
    return recdb.connect_readonly(str(root))


def queue_status(root) -> dict:
    """Where the job stands, for `wiki status` and `wiki recover` (architecture.md 9.3, FR-18 queue
    status): `checkpoint_event_id`, the record's last event id (`head`), `remaining`, the record
    events after the checkpoint that a step has still to read, and the `failed` items
    (`failed_items`). Its state lives in the record (the checkpoint row and the failure events), so
    it survives a restart and a rebuild of the store. Read-only."""
    source = _readonly(root)
    try:
        checkpoint = _checkpoint(source)
        head = int(source.execute("SELECT COALESCE(MAX(id),0) FROM events").fetchone()[0])
    finally:
        source.close()
    return {"checkpoint_event_id": checkpoint, "head": head,
            "remaining": _count_after(root, checkpoint, head), "failed": failed_items(root)}


def _proposed_ids(root) -> set:
    """Candidate ids already in the store in any status but `refused`: an item proposed before is
    not proposed again, so a rerun appends no `duplicate` events."""
    from .config import Config
    from .store.db import open_db_readonly
    cfg = Config.for_project(root)
    if not cfg.db_path.exists():
        return set()
    conn = open_db_readonly(cfg)
    try:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='candidates'").fetchone():
            return set()
        return {r[0] for r in conn.execute("SELECT candidate_id FROM candidates WHERE status<>'refused'")}
    finally:
        conn.close()


def _count_after(root, checkpoint_id: int, head: int) -> int:
    source = _readonly(root)
    try:
        return int(source.execute("SELECT COUNT(*) FROM events WHERE id>?", (checkpoint_id,)).fetchone()[0])
    finally:
        source.close()


# ------------------------------------------------------------------ the caller

def appender(root, session: str = SESSION):
    """The `RecordAppender` the callers bind: one record event as actor `alpaca-wiki` in `session`."""
    def append(kind, data):
        conn = recdb.connect(str(root))
        try:
            return recdb.append_event(conn, session=session, actor="alpaca-wiki", kind=kind,
                                       data=data)["id"]
        finally:
            conn.close()
    return append


@contextlib.contextmanager
def claim(root):
    """The consolidation claim (t-070 fix plan RC1, findings M-adv-6 and M-inv-5): an exclusive,
    non-blocking `flock` on `consolidate.lock` in the wiki store's directory, held by one step from
    the read of its checkpoint and failure state to the write of its failure events and checkpoint.
    Yields True when this caller holds it, False when another step does; then the caller does
    nothing, so no item is tried twice on the same state and three attempts stay the cap. The lock
    is released when the process ends, whatever ends it."""
    import fcntl
    import os
    from .config import Config
    cfg = Config.for_project(root)
    cfg.vault_dir.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(cfg.vault_dir / "consolidate.lock"),
                 os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
                 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _claimed_elsewhere(root) -> JobResult:
    """What a step answers when another step holds the claim: nothing done, nothing written."""
    source = _readonly(root)
    try:
        checkpoint = _checkpoint(source)
        head = int(source.execute("SELECT COALESCE(MAX(id),0) FROM events").fetchone()[0])
    finally:
        source.close()
    return JobResult(done=0, remaining=_count_after(root, checkpoint, head), proposed=(), failed=(),
                     checkpoint_event_id=checkpoint)


def run_step(root, *, deadline_s: float, session: str = SESSION, job: ConsolidateJob | None = None) -> JobResult:
    """One step and everything the caller owes it (architecture.md 9.2, OB-23): the step, then one
    `rune2-consolidate-failure` event {key, class, attempts} per failed entry the step tried
    (`attempted` true; an item already non-retryable gets none), then the checkpoint row. The events
    come before the checkpoint, so a crash between them repeats the step and never loses a failure.
    All of it runs under the consolidation claim (`claim`); a caller that finds the claim held does
    nothing and answers `done` 0 at the current checkpoint."""
    from alpaca import util
    with claim(root) as held:
        if not held:
            return _claimed_elsewhere(root)
        append = appender(root, session)
        result = (job or ConsolidateJob()).step(root, deadline_s=deadline_s, record=append)
        for entry in result.failed:
            if entry.get("attempted"):
                append(FAILURE_KIND, {"key": entry["key"], "class": entry["class"],
                                      "attempts": entry["attempts"],
                                      "candidate_id": entry.get("candidate_id")})
        conn = recdb.connect(str(root))
        try:
            with recdb.transaction(conn):
                recdb.upsert(conn, "obs_consumer_checkpoint", "consumer", {
                    "consumer": CONSUMER, "event_id": result.checkpoint_event_id, "observation_id": 0,
                    "updated_at": util.now_iso(),
                    "failures": sum(1 for f in result.failed if f["retryable"]),
                    "next_attempt": None, "error": None})
        finally:
            conn.close()
        return result


def run_step_after_capture(root, *, where: str, session: str, deadline_s: float):
    """The step a hook or the collector runs after its capture, only when `consolidate_after_capture`
    is true. A failure here never fails the hook or the consumer: it is recorded as `drain-failed`
    and the next step meets the same checkpoint. Returns the JobResult, or None when the key is off."""
    from .config import Config
    try:
        if not Config.for_project(root).consolidate_after_capture:
            return None
        return run_step(root, deadline_s=deadline_s)
    except Exception as exc:
        from .ingest.drain import screen
        conn = recdb.connect(str(root))
        try:
            # an exception's text may quote what failed: through the one screen (t-070 RC2)
            recdb.append_event(conn, session=session, actor="alpaca", kind="drain-failed",
                                data={"where": where,
                                      "error": screen("%s: %s" % (type(exc).__name__, exc))})
        finally:
            conn.close()
        return None
