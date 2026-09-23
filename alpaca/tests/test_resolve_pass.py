"""M4.5 proof: the resolve log, its typed events, and the resolve pass (serial-with-resolve).

Done-when (checklist M4.5): two writers on one reference produce a `concurrent-detected` entry,
the resolve pass reconstructs the divergence against the snapshot and either writes `resolved` or
surfaces an unrecoverable clobber, and no collision is ever silently merged.

Positive paths: a claim entry lands, a snapshot-backed pass writes a resolved entry that keeps
both divergences, the pass is idempotent, and two claims.take writers produce the concurrent
entry through the shipped claim path. Negative paths: an unknown typed kind is refused, a pass
with no snapshot surfaces rather than merges (no `resolved` is written), and the unresolved count
is surfaced on the pad and by the doctor. Timing is driven by a FixedClock; the record lives in a
throwaway root (the `project` fixture), never the live .alpaca/.
"""
import os

import pytest

from alpaca import claims, db, doctor, pad, resolve
from alpaca import clock as teclock
from alpaca import util

T0 = "2026-01-01T00:00:00+00:00"


def _conn(project):
    return db.connect(project)


# ------------------------------------------------------------------ the typed log
def test_log_writes_a_typed_entry_under_one_event_kind(project):
    conn = _conn(project)
    ev = resolve.log(conn, resolve.CLAIM, "R1", {"worker": "alice"}, now=T0)
    assert ev["kind"] == resolve.LOG_KIND       # one event kind on the record
    assert ev["data"]["kind"] == resolve.CLAIM  # typed in the data
    assert ev["ts"] == T0                        # the FixedClock stamped it
    got = resolve.entries(conn, kind=resolve.CLAIM, ref="R1")
    assert len(got) == 1 and got[0]["data"]["detail"]["worker"] == "alice"


def test_log_refuses_an_unknown_kind(project):
    conn = _conn(project)
    with pytest.raises(resolve.ResolveError):
        resolve.log(conn, "merged", "R1", {}, now=T0)
    # nothing was laundered onto the record
    assert resolve.entries(conn) == []


# ------------------------------------------------------------------ two writers, one reference
def test_two_writers_on_one_reference_produce_a_concurrent_detected_entry(project):
    conn = _conn(project)
    # driven through the shipped claim path: alice holds a live lease, bob peeks and collides.
    claims.take(conn, "R1", "alice", minutes=60, now=T0)
    res = claims.take(conn, "R1", "bob", minutes=60, now="2026-01-01T00:01:00+00:00")
    assert res["status"] == claims.CONCURRENCY
    # the collision is in the resolve log as a concurrent-detected entry, naming both writers.
    cd = resolve.entries(conn, kind=resolve.CONCURRENT, ref="R1")
    assert len(cd) == 1
    workers = {w["worker"] for w in cd[0]["data"]["detail"]["writers"]}
    assert workers == {"alice", "bob"}
    # the incumbent's live claim was recorded too: a peek in the gap saw a claim, not nothing.
    assert resolve.entries(conn, kind=resolve.CLAIM, ref="R1")


# ------------------------------------------------------------------ the resolve pass, with snapshot
def test_pass_with_a_snapshot_reconstructs_the_divergence_and_writes_resolved(project):
    conn = _conn(project)
    resolve.log(conn, resolve.CONCURRENT, "R1",
                {"ref": "R1", "base": "v0",
                 "writers": [{"worker": "alice", "value": "vA"},
                             {"worker": "bob", "value": "vB"}]}, now=T0)
    out = resolve.pass_(conn, now="2026-01-01T00:05:00+00:00")
    assert out["unresolved"] == 0
    assert len(out["resolved"]) == 1 and not out["unrecoverable"]
    # a resolved entry is on the record, reconstructed against the base snapshot.
    res = resolve.entries(conn, kind=resolve.RESOLVED, ref="R1")
    assert len(res) == 1
    d = res[0]["data"]["detail"]
    assert d["base"] == "v0" and d["resolution"] == resolve.RESOLUTION
    # both writers' divergences are kept: nothing is dropped in silence.
    changed = {x["worker"]: x["changed"] for x in d["divergence"]}
    assert changed == {"alice": True, "bob": True}
    assert d["winner"] == "bob"   # serial: the later writer wins, on the record


def test_pass_via_snapshots_argument_also_resolves(project):
    conn = _conn(project)
    resolve.log(conn, resolve.CONCURRENT, "R1",
                {"ref": "R1", "writers": [{"worker": "alice", "value": "vA"}]}, now=T0)
    # base not in the entry; supplied by the snapshots mapping instead.
    out = resolve.pass_(conn, snapshots={"R1": "v0"}, now=T0)
    assert out["unresolved"] == 0 and len(out["resolved"]) == 1
    assert resolve.unresolved_count(conn) == 0


def test_pass_is_idempotent(project):
    conn = _conn(project)
    resolve.log(conn, resolve.CONCURRENT, "R1",
                {"ref": "R1", "base": "v0",
                 "writers": [{"worker": "alice", "value": "vA"}]}, now=T0)
    resolve.pass_(conn, now=T0)
    before = len(resolve.entries(conn, kind=resolve.RESOLVED))
    resolve.pass_(conn, now=T0)   # a second pass answers nothing new
    assert len(resolve.entries(conn, kind=resolve.RESOLVED)) == before == 1


# ------------------------------------------------------------------ the resolve pass, no snapshot
def test_pass_with_no_snapshot_surfaces_rather_than_merges(project):
    conn = _conn(project)
    resolve.log(conn, resolve.CONCURRENT, "R1",
                {"ref": "R1",
                 "writers": [{"worker": "alice", "value": "vA"},
                             {"worker": "bob", "value": "vB"}]}, now=T0)
    out = resolve.pass_(conn, now=T0)
    # surfaced, not merged: no resolved entry was written for this reference.
    assert out["unresolved"] == 1
    assert len(out["unrecoverable"]) == 1
    assert out["unrecoverable"][0]["ref"] == "R1"
    assert out["unrecoverable"][0]["reason"] == "no-snapshot"
    assert not out["resolved"]
    assert resolve.entries(conn, kind=resolve.RESOLVED) == []   # never silently merged
    assert resolve.unresolved_count(conn) == 1


def test_a_claims_collision_without_a_snapshot_stays_unresolved(project):
    conn = _conn(project)
    claims.take(conn, "R1", "alice", minutes=60, now=T0)
    claims.take(conn, "R1", "bob", minutes=60, now="2026-01-01T00:01:00+00:00")
    # a raw lease collision carries no content snapshot, so the pass surfaces it.
    out = resolve.pass_(conn, now=T0)
    assert out["unresolved"] == 1 and not out["resolved"]
    assert resolve.unresolved_count(conn) == 1


# ------------------------------------------------------------------ the unresolved count is surfaced
def test_unresolved_collision_is_reported_on_the_pad(project):
    conn = _conn(project)
    resolve.log(conn, resolve.CONCURRENT, "R1",
                {"ref": "R1", "writers": [{"worker": "alice"}, {"worker": "bob"}]}, now=T0)
    lines = resolve.pad_lines(conn)
    assert any("Unresolved collisions: 1" in ln for ln in lines)
    assert any("R1" in ln for ln in lines)
    # the whole-pad render surfaces it too, and never raises.
    text = pad.render(project)
    assert "Unresolved collisions" in text


def test_no_unresolved_collision_leaves_the_pad_block_empty(project):
    conn = _conn(project)
    resolve.log(conn, resolve.CONCURRENT, "R1",
                {"ref": "R1", "base": "v0", "writers": [{"worker": "alice", "value": "vA"}]},
                now=T0)
    resolve.pass_(conn, now=T0)
    assert resolve.pad_lines(conn) == []


def test_doctor_warns_while_a_collision_is_unresolved_then_clears(project):
    conn = _conn(project)
    resolve.log(conn, resolve.CONCURRENT, "R1",
                {"ref": "R1", "base": "v0",
                 "writers": [{"worker": "alice", "value": "vA"}]}, now=T0)
    checks = {c["name"]: c for c in doctor.checks(project)}
    assert checks["resolve"]["level"] == "warn"
    # resolve it, and the doctor check clears to ok.
    resolve.pass_(conn, now=T0)
    checks = {c["name"]: c for c in doctor.checks(project)}
    assert checks["resolve"]["level"] == "ok"


# ------------------------------------------------------------------ determinism under a FixedClock
def test_entries_are_stamped_by_the_injected_clock(project):
    conn = _conn(project)
    try:
        util.set_clock(teclock.FixedClock(start=T0))
        # no explicit now: the installed FixedClock stamps the entry.
        ev = resolve.log(conn, resolve.CLAIM, "R1", {"worker": "alice"})
        assert ev["ts"] == T0
    finally:
        util.set_clock(None)
