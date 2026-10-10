"""op-008 t-070 fix plan RC3: a model-based test of the candidate and source lifecycle.

Seeded random sequences of 10 to 30 record acts (capture, a task move the record rules read, propose,
admit, retract, propose again after a retraction, withdraw a source, an owner correction, an export
and import) run against a real project. After every act:

* the live store, caught up on a copy with the events it has not captured yet (a sequence leaves
  some uncaptured on purpose), equals a rebuild from the record (`convergence_helpers.assert_converged`:
  accepted digest, candidate rows, active blocks; nothing withdrawn active);
* an export of that store imported into a fresh store from the same record exports the same bytes;
* an admission answered `applied` left its edge active at that moment (review finding B-1).

The default run takes the 24 seeds of `DEFAULT_SEEDS` (about 1.9 s a sequence at host load 48, so
about 45 s; each act costs a catch-up copy, a rebuild and an import of a small store). A few hundred
sequences do not fit in 60 s at that cost, so the long variant is opt-in:
`RUNE2_MODEL_SEQUENCES=300` runs seeds 0 to 299 (about 9 minutes); `RUNE2_MODEL_SEEDS=3,17` runs those seeds
only (to reproduce a failure, whose message names its seed, step and the acts so far);
`RUNE2_MODEL_LOG=<file>` writes one JSON line per sequence (seed, acts, seconds). Real record, real
drain, real store, real doors; nothing is mocked.

t-070 round 2 C-B (`.alpaca/rune2-development/execution/t-070/fix-plan-r2.md` section 3) adds the acts
the RC3 generator never wrote: `declare-late` (a session's first `session-start`, with an operator,
after the session already wrote), `transcript-append` (the session's transcript copy grows, with a
`transcript-snapshot` event), `withdraw-transcript`, `resume-after-withdrawal` (a withdrawn transcript
grows) and `hostile-id` (a session id holding a newline and a header-shaped tail). It adds one more
property, `test_a_rebuild_at_an_earlier_cutoff_is_unchanged_by_what_came_after`: at every step k the
accepted digest of a rebuild at the record head c_k is saved, and every few steps and at the end each
saved c_k is rebuilt again and compared (`RUNE2_CUTOFF_SEQUENCES` and `RUNE2_CUTOFF_SEEDS` as above).
Its sequences leave transcripts out; `test_a_rebuild_at_an_earlier_cutoff_is_unchanged_by_later_transcripts`
runs the same property with Claude and Codex transcripts that grow, register late, are withdrawn and
resume after a cutoff. A rebuild at an earlier cutoff reads each transcript at the version the newest
`transcript-version` event at or below the cutoff names (each capture records the version it lands,
t-430), lands none of a session whose first recorded landing comes later with nothing held before it,
and reads a session the record cannot place there (captured before t-430) best-effort: at the newest
`transcript-snapshot` at or below the cutoff, else as its file is now
(`test_a_rebuild_at_an_earlier_cutoff_ignores_a_later_transcript`, `..._ignores_a_later_registration`,
`..._ignores_growth_of_an_unsnapshotted_transcript`, `..._ignores_a_later_unsnapshotted_registration`;
the report's `transcript_cutoff`, `test_transcript_versions.py`).
A store moved to another root renders the same bytes: a transcript projection names its source by its portable identity, never by
a path (t-417, `test_a_store_moved_to_another_root_imports_the_same_export`). Two fixed sequences
reproduce the round-1 attacks t01 C (a late declaration) and t07b (a withdrawn transcript, resumed).
"""
import json
import os
import random
import shutil
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from native_helpers import appender, at, capture, edges, events  # noqa: E402
from convergence_helpers import assert_converged, caught_up, roundtrip  # noqa: E402

from alpaca.wiki import service                                  # noqa: E402

REPO = Path(__file__).resolve().parents[3]
DEFAULT_SEEDS = tuple(range(24))
SUBJECTS = ("t-1", "t-2")
OBJECTS = ("blocked", "done", "open")
SESSIONS = ("s-a", "s-b", "s-c")
JOB = "job:rune2-consolidate"
ACTS = (("note", 5), ("capture", 2), ("move", 2), ("propose", 5), ("admit", 5), ("retract", 3),
        ("repropose", 3), ("withdraw-source", 1), ("correct", 2), ("export-import", 1),
        ("declare-late", 1), ("transcript-append", 2), ("withdraw-transcript", 1),
        ("resume-after-withdrawal", 1), ("hostile-id", 1))
#: The acts of the earlier-cutoff property: no transcript (see the module text).
CUTOFF_ACTS = tuple((name, weight) for name, weight in ACTS
                    if not name.startswith(("transcript", "withdraw-transcript", "resume"))
                    and name != "export-import")
DEFAULT_CUTOFF_SEEDS = tuple(range(12))


def _seeds(prefix="RUNE2_MODEL", default=DEFAULT_SEEDS):
    if os.environ.get(prefix + "_SEEDS"):
        return tuple(int(s) for s in os.environ[prefix + "_SEEDS"].split(","))
    if os.environ.get(prefix + "_SEQUENCES"):
        return tuple(range(int(os.environ[prefix + "_SEQUENCES"])))
    return default


def _fresh_project(base: Path, seed: int) -> str:
    """The `project` fixture's layout (alpaca/tests/conftest.py), one per sequence."""
    root = base / ("seq-%d" % seed) / "proj"
    root.mkdir(parents=True)
    shutil.copy(REPO / "ALPACA-MANIFEST", root / "ALPACA-MANIFEST")
    (root / "style").mkdir()
    (root / "style" / "banned.txt").write_text("", encoding="utf-8")
    (root / "src").mkdir()
    (root / ".alpaca" / "config").mkdir(parents=True)
    (root / ".alpaca" / "wiki").mkdir()
    from alpaca import db as record
    record.connect(str(root)).close()                  # the record exists before the first act
    return str(root)


class Sequence:
    def __init__(self, root: str, seed: int, acts=ACTS):
        self.root, self.rng, self.seed = root, random.Random(seed), seed
        self.acts = acts
        self.notes: list = []            # (event id, subject, object)
        self.cands: dict = {}            # candidate id -> (event id, subject, object, variant, proposer)
        self.trace: list = []
        self.minute = 0
        self.sessions = list(SESSIONS)   # the hostile ids join them
        self.wrote: set = set()          # sessions with an event
        self.declared: set = set()       # sessions with a session-start
        self.talks: dict = {}            # session -> its transcript copy
        self.withdrawn: set = set()      # sessions whose transcript was withdrawn

    def ts(self) -> str:
        self.minute += 1
        return "2026-03-01T%02d:%02d:00+00:00" % (9 + self.minute // 60, self.minute % 60)

    # -------------------------------------------------------------- acts
    def note(self, session=None):
        s, o = self.rng.choice(SUBJECTS), self.rng.choice(OBJECTS)
        session = session or self.rng.choice(self.sessions)
        eid = at(self.root, "result", {"body": "Task %s is %s on the review." % (s, o)}, self.ts(),
                 ref=s, session=session)
        self.wrote.add(session)
        self.notes.append((eid, s, o))
        if self.rng.random() < 0.6:
            capture(self.root, {eid})
        return eid

    def capture(self):
        return capture(self.root)["notes"]

    def move(self):
        s = self.rng.choice(SUBJECTS)
        frm, to = self.rng.sample(OBJECTS, 2)
        return at(self.root, "task-move", {"from": frm, "to": to, "proof": None, "reason": None},
                  self.ts(), op="op-8", ref=s, actor="human")

    def _proposal(self, eid, s, o, variant):
        quote = "Task %s is %s" % (s, o) + (" on the review" if variant else "")
        row = next(e for e in events(self.root, {eid}))
        return {"kind": "fact", "fact": {"subject": s, "predicate": "status", "obj": o,
                                         "obj_datatype": "string"},
                "anchors": [{"source_ref": "event:%d" % eid, "quote": quote, "hash": row["hash"]}]}

    def _propose(self, eid, s, o, variant, session):
        r = service.propose(self.root, self._proposal(eid, s, o, variant), session=session,
                            record=appender(self.root, session))
        if r.status != "duplicate":
            self.cands.setdefault(r.candidate_id, (eid, s, o, variant, session))
        return r.status, r.candidate_id

    def propose(self):
        if not self.notes:
            return self.note()
        eid, s, o = self.rng.choice(self.notes)
        return self._propose(eid, s, o, self.rng.randrange(2),
                             self.rng.choice(SESSIONS + (JOB,)))

    def _pick(self, statuses=None):
        rows = {c.candidate_id: c.status for c in service.candidates(self.root)}
        pool = sorted(c for c in self.cands if c in rows and (statuses is None or rows[c] in statuses))
        return self.rng.choice(pool) if pool else None

    def admit(self):
        cid = self._pick()
        if cid is None:
            return self.propose()
        session = self.rng.choice(SESSIONS + (JOB,))
        r = service.admit(self.root, cid, session=session, record=appender(self.root, session))
        if r.status == "applied":
            # B-1: an applied admission leaves its edge active at that moment
            (edge,) = edges(self.root, edge_id=r.effects["edge_id"])
            assert edge["status"] == "active", ("B-1", r, edge["status"])
        return r.status, r.reason

    def retract(self):
        cid = self._pick(("checked", "stale", "admitted"))
        if cid is None:
            return None
        if self.rng.random() < 0.7:
            session, by = self.cands[cid][4], None
        else:
            session, by = "s-owner", "owner"
        r = service.withdraw_candidate(self.root, cid, session=session,
                                       record=appender(self.root, session), reason="model", by=by)
        return r.status, r.reason

    def repropose(self):
        cid = self._pick(("retracted", "withdrawn"))
        if cid is None:
            return self.propose()
        eid, s, o, variant, _ = self.cands[cid]
        return self._propose(eid, s, o, 1 - variant, self.rng.choice(SESSIONS))

    def withdraw_source(self):
        if not self.notes:
            return None
        eid = self.rng.choice(self.notes)[0]
        r = service.withdraw_source(self.root, "event:%d" % eid, session="s-owner",
                                    record=appender(self.root, "s-owner"), reason="model")
        return r.status, r.reason

    def correct(self):
        live = [e for e in edges(self.root) if e["status"] == "active" and e["predicate"] == "status"]
        if not live:
            return None
        edge = self.rng.choice(live)
        if self.rng.random() < 0.5:
            r = service.correct(self.root, edge["edge_id"], "retract", session="s-owner",
                                record=appender(self.root, "s-owner"), reason="model", by="owner")
        else:
            obj = self.rng.choice([o for o in OBJECTS if o not in (edge["obj_literal"] or "")])
            r = service.correct(self.root, edge["edge_id"], "supersede", session="s-owner",
                                record=appender(self.root, "s-owner"), reason="model", by="owner",
                                replacement={"obj": obj, "obj_datatype": "string"})
        return r.status, r.reason

    def export_import(self):
        """An operator's export and import: capture first (an export carries what the store holds),
        transcripts included (each version a capture lands is the one the record names, t-430),
        then the round trip on the live store itself."""
        capture(self.root)
        if self.talks:
            service.capture(self.root, sessions=sorted(self.talks), deadline_s=None)
        roundtrip(self.root, "act-import")
        return "ok"

    # ------------------------------------------------- t-070 round 2 C-B acts
    def declare_late(self):
        """A session that already wrote declares its operator (its first `session-start`), as an
        operator who runs `alpaca session start` late; captured now or left to the catch-up."""
        pool = sorted(s for s in self.wrote if s not in self.declared)
        if not pool:
            return None
        session = self.rng.choice(pool)
        eid = at(self.root, "session-start", {"operator": self.rng.choice(("claude", "codex"))},
                 self.ts(), session=session, op=None, ref=None, actor="alpaca")
        self.declared.add(session)
        if self.rng.random() < 0.6:
            capture(self.root)
        return eid

    def _grow(self, session):
        """Append two messages to the session's transcript copy, registered on first use, and
        record a `transcript-snapshot` of it; land it now or leave it to the catch-up."""
        import hashlib
        from alpaca import observability, transcripts
        path = self.talks.get(session)
        if path is None:
            path = transcripts.local_path(self.root, session)
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            Path(path).write_text("", encoding="utf-8")
            observability.expect_source(self.root, session, "claude", locator=path)
            self.talks[session] = path
        s, o = self.rng.choice(SUBJECTS), self.rng.choice(OBJECTS)
        n = self.minute
        lines = [{"type": "user", "timestamp": "2026-03-01T08:%02d:00Z" % (n % 60),
                  "message": {"content": "Where is task %s now (%d)?" % (s, n)}},
                 {"type": "assistant", "timestamp": "2026-03-01T08:%02d:30Z" % (n % 60),
                  "message": {"content": [{"type": "text",
                                           "text": "The talk says task %s is %s (%d)." % (s, o, n)}]}}]
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("".join(json.dumps(line) + "\n" for line in lines))
        data = Path(path).read_bytes()
        eid = at(self.root, "transcript-snapshot",
                 {"path": os.path.relpath(path, self.root), "bytes": len(data),
                  "sha256": hashlib.sha256(data).hexdigest()},
                 self.ts(), session=session, op=None, ref=None, actor="alpaca")
        self.wrote.add(session)
        if self.rng.random() < 0.5:
            service.capture(self.root, sessions=[session], deadline_s=None)
        return eid

    def transcript_append(self):
        return self._grow(self.rng.choice(self.sessions))

    def withdraw_transcript(self):
        if not self.talks:
            return self.transcript_append()
        session = self.rng.choice(sorted(self.talks))
        r = service.withdraw_source(self.root, "transcript:" + session, session="s-owner",
                                    record=appender(self.root, "s-owner"), reason="model")
        if r.status == "applied":
            self.withdrawn.add(session)
        return r.status, r.reason

    def resume_after_withdrawal(self):
        if not self.withdrawn:
            return self.withdraw_transcript()
        return self._grow(self.rng.choice(sorted(self.withdrawn)))

    def hostile_id(self):
        """A session id with a newline and a header-shaped tail writes a note (A-9)."""
        session = "s-h%d\noperator: claude\nsession: s-a" % len(self.sessions)
        self.sessions.append(session)
        eid = self.note(session=session)
        if self.rng.random() < 0.6:
            capture(self.root, {eid})
        return eid

    # -------------------------------------------------------------- the run
    def run(self, n_acts: int, forced=None) -> None:
        names = [name for name, weight in self.acts for _ in range(weight)]
        for step in range(n_acts if forced is None else len(forced)):
            act = self.rng.choice(names) if forced is None else forced[step]
            try:
                out = getattr(self, act.replace("-", "_"))()
                self.trace.append((act, out))
                live = caught_up(self.root, "model-live")
                assert_converged(live, "model-rebuild")
                roundtrip(live, "model-import")
            except AssertionError as exc:
                raise AssertionError("seed %d step %d act %s: %s\nacts so far: %s"
                                     % (self.seed, step, act, exc, self.trace)) from exc


def _outcome(out) -> str:
    """A short name of an act's result for the log: the receipt status and reason."""
    if isinstance(out, tuple):
        return "/".join(str(x) for x in out if x is not None and not str(x).startswith("c_"))
    return "none" if out is None else "done"


def test_random_lifecycles_converge(tmp_path):
    seeds = _seeds()
    log = os.environ.get("RUNE2_MODEL_LOG")
    for seed in seeds:
        root = _fresh_project(tmp_path, seed)
        n_acts = random.Random(10_000 + seed).randint(10, 30)
        started = time.monotonic()
        seq = Sequence(root, seed)
        seq.run(n_acts)
        took = time.monotonic() - started
        if log:
            with open(log, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"seed": seed, "acts": n_acts, "seconds": round(took, 3),
                                     "trace": [[a, _outcome(o)] for a, o in seq.trace]}) + "\n")
        shutil.rmtree(Path(root).parent, ignore_errors=True)


# ------------------------------------------------------------------ t-070 round 2 C-B
def _rebuild_digest(project, cutoff, name):
    """The accepted digest of a rebuild of `project`'s record at `cutoff` (`recovery.replay` on an
    empty store beside it)."""
    from alpaca.wiki import recovery
    from convergence_helpers import shadow
    from native_helpers import store
    from alpaca.wiki.digest import accepted_digest
    root = shadow(project, name)
    report = recovery.replay(root, cutoff=cutoff)
    assert report["status"] == "ok", report
    with store(root) as conn:
        digest = accepted_digest(conn)
    shutil.rmtree(root, ignore_errors=True)
    return digest


def _head(project):
    from alpaca import db as record
    conn = record.connect_readonly(str(project))
    try:
        return int(conn.execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()[0])
    finally:
        conn.close()


def test_a_rebuild_at_an_earlier_cutoff_is_unchanged_by_what_came_after(tmp_path):
    """A store at record head N depends only on the record at or below N (fix-plan-r2 section 3): a
    rebuild at an earlier cutoff gives the digest it gave then, whatever was appended after it (a
    late declaration of the operator, a hostile session id, withdrawals, corrections)."""
    log = os.environ.get("RUNE2_MODEL_LOG")
    for seed in _seeds("RUNE2_CUTOFF", DEFAULT_CUTOFF_SEEDS):
        root = _fresh_project(tmp_path, 1000 + seed)
        seq = Sequence(root, 1000 + seed, acts=CUTOFF_ACTS)
        names = [name for name, weight in CUTOFF_ACTS for _ in range(weight)]
        saved = {}
        started = time.monotonic()
        n_acts = random.Random(20_000 + seed).randint(8, 16)
        for step in range(n_acts):
            act = seq.rng.choice(names)
            seq.trace.append((act, getattr(seq, act.replace("-", "_"))()))
            cutoff = _head(root)
            if cutoff and cutoff not in saved:
                saved[cutoff] = _rebuild_digest(root, cutoff, "cutoff-saved")
            if step % 4 == 3 or step == n_acts - 1:
                for at_cutoff, digest in sorted(saved.items()):
                    again = _rebuild_digest(root, at_cutoff, "cutoff-again")
                    assert again == digest, ("seed %d step %d: the rebuild at cutoff %d changed after "
                                             "it was taken; acts so far: %s"
                                             % (seed, step, at_cutoff, seq.trace))
        if log:
            with open(log, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"property": "earlier-cutoff", "seed": seed, "acts": n_acts,
                                     "cutoffs": len(saved),
                                     "seconds": round(time.monotonic() - started, 3),
                                     "trace": [[a, _outcome(o)] for a, o in seq.trace]}) + "\n")
        shutil.rmtree(Path(root).parent, ignore_errors=True)


def test_a_late_declaration_rerenders_the_earlier_notes(tmp_path):
    """Attack t01 C (B-3) as a fixed sequence: a session writes, its notes are captured, then it
    declares its operator. Live store, rebuild and import agree after every act, and the note
    names the operator."""
    root = _fresh_project(tmp_path, 9001)
    seq = Sequence(root, 9001)
    seq.rng.random = lambda: 0.0                        # every optional capture runs
    seq.run(0, forced=["note", "capture", "declare-late", "propose", "admit"])
    from native_helpers import store
    with store(root) as conn:
        texts = [r[0] for r in conn.execute(
            "SELECT text FROM blocks WHERE status='active' AND ordinal=0 AND text LIKE 'op:%'")]
    noted = [t for t in texts if "\nkind: result\n" in t]
    assert noted and all("\noperator: " in t for t in noted), noted
    # and the attribution is served (FR-6): the cited source names the declared operator
    eid, subject, obj = seq.notes[0]
    out = service.query(root, "Task %s is %s on the review." % (subject, obj))
    named = [src["operator"] for src in out["sources"] if src["source_ref"] == "event:%d" % eid]
    assert named and all(named), out["sources"]


def test_a_withdrawn_transcript_resumed_converges(tmp_path):
    """Attack t07b (B-4) as a fixed sequence: a transcript lands, is withdrawn, grows, and lands
    again; live store, rebuild and import agree after every act, and the rebuild lands the
    withdrawn version the act names."""
    root = _fresh_project(tmp_path, 9002)
    seq = Sequence(root, 9002)
    seq.rng.random = lambda: 0.0
    seq.run(0, forced=["note", "transcript-append", "withdraw-transcript",
                       "resume-after-withdrawal", "resume-after-withdrawal", "capture"])
    assert seq.withdrawn, seq.trace


def test_a_hostile_session_id_converges(tmp_path):
    """A-9 as a fixed sequence: a session id with a newline and header-shaped tails writes, is
    captured, declares an operator late and has a transcript."""
    root = _fresh_project(tmp_path, 9003)
    seq = Sequence(root, 9003)
    seq.rng.random = lambda: 0.0
    seq.run(0, forced=["hostile-id", "capture", "note", "declare-late", "propose"])
    hostile = [s for s in seq.sessions if "\n" in s][0]
    seq._grow(hostile)
    live = caught_up(root, "model-live")
    assert_converged(live, "model-rebuild")
    roundtrip(live, "model-import")


def test_a_rebuild_at_an_earlier_cutoff_ignores_a_later_transcript(tmp_path):
    """t-430: a transcript that grows after the cutoff, with its `transcript-snapshot`, leaves a
    rebuild at that cutoff as it was: the rebuild reads the version the newest snapshot at or below
    the cutoff names."""
    root = _fresh_project(tmp_path, 9004)
    seq = Sequence(root, 9004)
    seq.rng.random = lambda: 0.0
    seq.run(0, forced=["note", "transcript-append"])
    cutoff = _head(root)
    first = _rebuild_digest(root, cutoff, "xfail-first")
    seq.transcript_append()                              # the transcript grows after the cutoff
    assert _rebuild_digest(root, cutoff, "xfail-again") == first


def test_a_rebuild_at_an_earlier_cutoff_ignores_a_later_registration(tmp_path):
    """t-430: a session that registers its transcript after the cutoff (its first event, its
    locator and its snapshot all come later) leaves a rebuild at that cutoff as it was: the record
    names no version of it at the cutoff."""
    root = _fresh_project(tmp_path, 9006)
    seq = Sequence(root, 9006)
    seq.rng.random = lambda: 0.0
    seq.note(session="s-a")
    seq.transcript_append()
    cutoff = _head(root)
    first = _rebuild_digest(root, cutoff, "reg-first")
    seq._grow("s-b")                                     # registered, written and snapshotted later
    assert _rebuild_digest(root, cutoff, "reg-again") == first


def _snapshot_project(tmp_path, seed, operator="claude"):
    """A project whose session s-n has a native transcript outside the root, with a private
    reasoning record, registered as the hooks (or, for Codex, the collector) register one; the
    snapshot's copy leaves the reasoning out, so the copy is not a prefix of the native file."""
    from alpaca import observability, transcripts
    root = _fresh_project(tmp_path, seed)
    native = tmp_path / ("native-%d" % seed) / "s-n.jsonl"
    native.parent.mkdir(parents=True)
    observability.expect_source(root, "s-n", operator, locator=str(native))
    at(root, "result", {"body": "Task t-1 is open on the review."}, "2026-03-01T09:00:00+00:00",
       ref="t-1", session="s-n")
    return root, native, transcripts


def _say(native, n, operator="claude"):
    """One exchange appended to a native transcript in the operator's format: a Claude session file
    (with a private reasoning record), or a Codex rollout (its `session_meta` first)."""
    if operator == "codex":
        rows = [] if Path(native).exists() else [
            {"type": "session_meta", "timestamp": "2026-03-01T08:00:00Z",
             "payload": {"id": "0199a1b2-c3d4-7e5f-8a9b-%012d" % n, "cwd": "/x"}}]
        rows += [{"type": "response_item", "timestamp": "2026-03-01T08:%02d:10Z" % n,
                  "payload": {"type": "message", "role": "user", "content": [
                      {"type": "input_text", "text": "Where is task t-1 now (%d)?" % n}]}},
                 {"type": "response_item", "timestamp": "2026-03-01T08:%02d:20Z" % n,
                  "payload": {"type": "message", "role": "assistant", "content": [
                      {"type": "output_text", "text": "Task t-1 is done (%d)." % n}]}}]
    else:
        rows = [{"type": "assistant", "timestamp": "2026-03-01T08:%02d:00Z" % n,
                 "message": {"content": [{"type": "thinking", "thinking": "private %d" % n}]}},
                {"type": "user", "timestamp": "2026-03-01T08:%02d:10Z" % n,
                 "message": {"content": "Where is task t-1 now (%d)?" % n}},
                {"type": "assistant", "timestamp": "2026-03-01T08:%02d:20Z" % n,
                 "message": {"content": [{"type": "text", "text": "Task t-1 is done (%d)." % n}]}}]
    Path(native).parent.mkdir(parents=True, exist_ok=True)
    with open(native, "a", encoding="utf-8") as fh:
        fh.write("".join(json.dumps(r) + "\n" for r in rows))


def test_an_earlier_cutoff_lands_the_native_version_the_live_capture_held(tmp_path):
    """t-430: the hooks snapshot a native transcript and capture it at once. A rebuild at that
    cutoff, taken after the native file grew, lands the transcript version the live store held then:
    the native file cut at the snapshot's `source_bytes` (its copy, which leaves private reasoning
    out, is another file with other bytes)."""
    from native_helpers import store
    root, native, transcripts = _snapshot_project(tmp_path, 9007)
    _say(native, 1)
    snap = transcripts.snapshot(root, "s-n", event=True)
    assert snap["bytes"] != snap["source_bytes"], snap        # the copy is not the native prefix
    service.capture(root, sessions=["s-n"], session_events=True, deadline_s=None)
    with store(root) as conn:
        (live,) = [r[0] for r in conn.execute("SELECT content_sha256 FROM source_versions "
                                              "WHERE source_ref='transcript:s-n' AND status='current'")]
    cutoff = _head(root)
    _say(native, 2)                                       # grows after the cutoff, with a snapshot
    transcripts.snapshot(root, "s-n", event=True)
    _say(native, 3)                                       # and without one
    from alpaca.wiki import recovery
    from convergence_helpers import shadow
    rebuilt = shadow(root, "native-cut")
    report = recovery.replay(rebuilt, cutoff=cutoff)
    assert report["status"] == "ok", report
    with store(rebuilt) as conn:
        held = [r[0] for r in conn.execute("SELECT content_sha256 FROM source_versions "
                                           "WHERE source_ref='transcript:s-n' AND status='current'")]
    assert held == [live], (held, live, report.get("transcript_cutoff"))


@pytest.mark.parametrize("operator", ("claude", "codex"))
def test_a_rebuild_at_an_earlier_cutoff_ignores_growth_of_an_unsnapshotted_transcript(tmp_path, operator):
    """t-430: a transcript with no `transcript-snapshot` (a locator the collector reads), captured,
    then grown and captured again after the cutoff: a rebuild at the cutoff reads the version the
    record's `transcript-version` event at or below it names, not the file as it is now."""
    seed = 9008 if operator == "claude" else 9010
    root, native, _ = _snapshot_project(tmp_path, seed, operator)
    _say(native, 1, operator)
    service.capture(root, sessions=["s-n"], session_events=True, deadline_s=None)
    cutoff = _head(root)
    first = _rebuild_digest(root, cutoff, "legacy-first")
    _say(native, 2, operator)
    at(root, "result", {"body": "Task t-1 is done on the review."}, "2026-03-01T09:30:00+00:00",
       ref="t-1", session="s-n")
    assert _rebuild_digest(root, cutoff, "legacy-again") == first      # grown, not captured yet
    service.capture(root, sessions=["s-n"], session_events=True, deadline_s=None)
    _say(native, 3, operator)
    assert _rebuild_digest(root, cutoff, "legacy-third") == first      # captured, and grown again


@pytest.mark.parametrize("operator", ("claude", "codex"))
def test_a_rebuild_at_an_earlier_cutoff_ignores_a_later_unsnapshotted_registration(tmp_path, operator):
    """t-430: a session the collector registers by a locator after the cutoff (a subagent), with no
    event of its own and no `transcript-snapshot`, and captures: its first `transcript-version`
    event comes after the cutoff and says the store held no version before, so a rebuild at the
    cutoff lands none of it."""
    from alpaca import observability
    seed = 9009 if operator == "claude" else 9011
    root, native, _ = _snapshot_project(tmp_path, seed, operator)
    _say(native, 1, operator)
    service.capture(root, sessions=["s-n"], session_events=True, deadline_s=None)
    cutoff = _head(root)
    first = _rebuild_digest(root, cutoff, "legacy-reg-first")
    child = native.parent / "agent-late.jsonl"
    _say(child, 2, operator)
    observability.expect_source(root, "agent-late", operator, locator=str(child))
    service.capture(root, sessions=["agent-late"], deadline_s=None)      # the collector lands it
    at(root, "result", {"body": "Task t-2 is open on the review."}, "2026-03-01T09:40:00+00:00",
       ref="t-2", session="s-n")
    assert _rebuild_digest(root, cutoff, "legacy-reg-again") == first
    _say(child, 3, operator)
    assert _rebuild_digest(root, cutoff, "legacy-reg-third") == first


#: The acts of the earlier-cutoff property with transcripts (t-430): every transcript act lands the
#: version it writes at once, as the collector does for a session it watches, so a rebuild at the
#: head reads the version the record names; a rebuild at an earlier cutoff must read it again later.
TALK_CUTOFF_ACTS = (("note", 3), ("capture", 1), ("declare-late", 1), ("hostile-id", 1),
                    ("talk", 4), ("register-late", 2), ("withdraw-transcript", 1),
                    ("resume-after-withdrawal", 1))
DEFAULT_TALK_CUTOFF_SEEDS = tuple(range(24))


class TalkSequence(Sequence):
    """`Sequence` whose transcripts are native files outside the root in Claude or Codex format,
    registered by a locator (no `transcript-snapshot`: what the collector reads), each version
    landed through `service.capture` as it is written."""

    def __init__(self, root, seed, acts, outside):
        super().__init__(root, seed, acts=acts)
        self.outside, self.formats, self.n = Path(outside), {}, 0

    def _write(self, session):
        from alpaca import observability
        path = self.talks.get(session)
        if path is None:
            fmt = self.rng.choice(("claude", "codex"))
            path = self.outside / ("talk-%d.jsonl" % len(self.talks))
            observability.expect_source(self.root, session, fmt, locator=str(path))
            self.talks[session], self.formats[session] = path, fmt
        self.n += 1
        _say(path, self.n, self.formats[session])
        service.capture(self.root, sessions=[session], deadline_s=None)
        return self.n

    def talk(self):
        return self._write(self.rng.choice(self.sessions))

    def register_late(self):
        """A session with no event of its own (a subagent) is registered and landed."""
        session = "agent-%d" % len(self.talks)
        return self._write(session)

    def transcript_append(self):
        return self.talk()

    def resume_after_withdrawal(self):
        if not self.withdrawn:
            return self.withdraw_transcript()
        return self._write(self.rng.choice(sorted(self.withdrawn)))


def test_a_rebuild_at_an_earlier_cutoff_is_unchanged_by_later_transcripts(tmp_path):
    """t-430, the property of `test_a_rebuild_at_an_earlier_cutoff_is_unchanged_by_what_came_after`
    with transcripts: Claude and Codex transcripts grow, register late, are withdrawn and resumed
    after a cutoff, and the rebuild at that cutoff keeps the digest it gave when the cutoff was the
    head (`RUNE2_TALK_CUTOFF_SEQUENCES` and `RUNE2_TALK_CUTOFF_SEEDS` as above)."""
    log = os.environ.get("RUNE2_MODEL_LOG")
    for seed in _seeds("RUNE2_TALK_CUTOFF", DEFAULT_TALK_CUTOFF_SEEDS):
        root = _fresh_project(tmp_path, 3000 + seed)
        seq = TalkSequence(root, 3000 + seed, TALK_CUTOFF_ACTS, Path(root).parent / "outside")
        names = [name for name, weight in TALK_CUTOFF_ACTS for _ in range(weight)]
        saved = {}
        started = time.monotonic()
        n_acts = random.Random(30_000 + seed).randint(8, 14)
        for step in range(n_acts):
            act = seq.rng.choice(names)
            seq.trace.append((act, getattr(seq, act.replace("-", "_"))()))
            cutoff = _head(root)
            if cutoff and cutoff not in saved:
                saved[cutoff] = _rebuild_digest(root, cutoff, "talk-saved")
            if step % 4 == 3 or step == n_acts - 1:
                for at_cutoff, digest in sorted(saved.items()):
                    again = _rebuild_digest(root, at_cutoff, "talk-again")
                    assert again == digest, ("seed %d step %d: the rebuild at cutoff %d changed after "
                                             "it was taken; acts so far: %s"
                                             % (seed, step, at_cutoff, seq.trace))
        if log:
            with open(log, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"property": "earlier-cutoff-talk", "seed": seed, "acts": n_acts,
                                     "cutoffs": len(saved),
                                     "seconds": round(time.monotonic() - started, 3),
                                     "trace": [[a, _outcome(o)] for a, o in seq.trace]}) + "\n")
        shutil.rmtree(Path(root).parent, ignore_errors=True)


def test_a_store_moved_to_another_root_imports_the_same_export(tmp_path):
    """The move-root act (t-417): export the store, import it into a copy of the project at another
    path (the transcript copy moved with it), export again; the import refuses nothing, the two
    exports are byte-identical, and the moved store holds the transcript projection the first store
    held, byte for byte (its text names no path of either root)."""
    from alpaca.wiki import portable
    from alpaca import transcripts
    root = _fresh_project(tmp_path, 9005)
    seq = Sequence(root, 9005)
    seq.rng.random = lambda: 0.0
    eid = seq.note(session="s-a")
    path = Path(transcripts.local_path(root, "s-a"))         # a local copy, found by its root
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"type": "user", "timestamp": "2026-03-01T08:00:00Z",
                                "message": {"content": "Task t-1 is open."}}) + "\n",
                    encoding="utf-8")
    service.capture(root, sessions=["s-a"], events=events(root, {eid}), deadline_s=None)
    first = tmp_path / "moved-1.jsonl"
    portable.export_store(root, first)
    moved = tmp_path / "elsewhere" / "deeper" / "proj"
    shutil.copytree(root, moved, ignore=shutil.ignore_patterns("wiki"))
    report = portable.import_store(str(moved), first)
    assert report["status"] == "ok" and report["refused"] == [], report
    second = tmp_path / "moved-2.jsonl"
    portable.export_store(str(moved), second)
    assert second.read_bytes() == first.read_bytes()
    from native_helpers import store
    held = {}
    for where in (root, str(moved)):
        with store(where) as conn:
            held[where] = sorted(tuple(r) for r in conn.execute(
                "SELECT v.doc_id, v.content_sha256 FROM source_versions v "
                "WHERE v.source_ref='transcript:s-a' AND v.status='current'"))
    assert held[root] and held[root] == held[str(moved)], held
    for where in (root, str(moved)):
        text = Path(where, ".alpaca", "wiki", held[where][0][0]).read_text(encoding="utf-8")
        assert root not in text and str(moved) not in text and str(tmp_path) not in text, text
