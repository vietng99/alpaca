"""M4.13 proof: alpaca doctor grows its record-consistency half.

`doctor.consistency(conn) -> list[finding]` reports, each with its pointer:

  * a pad naming an op that does not exist          (error)
  * a cursor that resolves to nothing               (warn)
  * an op marked live whose cursor has not advanced (warn)
  * an expired or revoked authority still being cited (warn)
  * an unresolved collision                          (warn)
  * a store over its retention ceiling               (warn)
  * a dangerous local grant on the host             (warn, the host_residue port)

Every finding is proved on BOTH the positive path (the condition is present, the check fires with
its pointer) and the negative path (a clean record, the check is silent). The doctor carries a
selftest that fails when a check goes inert - a parser that matches nothing on its own positive
fixture - proved by monkeypatching one parser to match nothing. consistency() is read-only: a
checksum of the tree and the event count are unchanged across a scan. The exit-code contract
0 ok / 1 warn / 2 error is proved through the real `alpaca doctor` verb, findings first for the level
a human should act on.

FixedClock drives the staleness timing; the throwaway `project` root keeps every write in one tree.
"""
import hashlib
import json
import os

import pytest

from alpaca import board, clock, db, doctor, opindex, project as project_mod, resolve, retention, util
from alpaca.posture import authority
from alpaca.tests.conftest import project  # noqa: F401  (pytest fixture)

T0 = "2026-01-01T00:00:00+00:00"
NOW = "2026-06-01T00:00:00+00:00"       # far past T0's staleness window and any seeded expiry


# --------------------------------------------------------------- record-building helpers
def _conn(project):
    return db.connect(project)


def _op(conn, oid, status="open"):
    db.upsert(conn, "ops", "id", {"id": oid, "intent": "i", "done_when": "d", "status": status,
                                  "opened": util.now_iso(), "closed": None, "phases": "[]"})
    return oid


def _row(conn, rid, op):
    db.upsert(conn, "rows", "id", {
        "id": rid, "kind": "item", "op": op, "phase": "build", "step": "s1",
        "statement": "do the thing properly here now", "proof": "local:spec.md",
        "where_": "", "how": "", "when_": "", "why": "", "session": None, "operator": None,
        "status": "open", "tag": "Specced", "content_hash": util.sha256_hex("row/" + rid),
        "prev_hash": None, "supersedes": None})
    return rid


def _issue_pad_token(conn, op):
    from alpaca import token
    return db.append_event(conn, session="alpaca", actor="alpaca", kind=token.KIND_ISSUE, op=op,
                           ref="target", data={"token": "tok-x"})


def _has(findings, check):
    return [f for f in findings if f["check"] == check]


# --------------------------------------------------------------- the clean baseline
def test_a_clean_record_has_no_findings(project):
    conn = _conn(project)
    assert doctor.consistency(conn, root=project) == []


# --------------------------------------------------------------- 1. a pad naming a missing op
def test_pad_naming_a_missing_op_is_reported_with_its_pointer(project):
    conn = _conn(project)
    _issue_pad_token(conn, "op-ghost")            # a pad row naming an op never created
    found = _has(doctor.consistency(conn, root=project), "pad-op")
    assert len(found) == 1
    assert found[0]["level"] == "error"
    assert "op-ghost" in found[0]["detail"]
    assert "tok-x" in found[0]["pointer"] and "event" in found[0]["pointer"]


def test_a_pad_naming_a_real_op_is_not_reported(project):
    conn = _conn(project)
    _op(conn, "op-1")
    _issue_pad_token(conn, "op-1")
    assert _has(doctor.consistency(conn, root=project), "pad-op") == []


# --------------------------------------------------------------- 2. a cursor resolving to nothing
def test_a_cursor_resolving_to_nothing_is_reported(project):
    conn = _conn(project)
    _op(conn, "op-1")
    opindex.set_cursor(conn, "op-1", "r-ghost", session="s")   # pinned to a row never committed
    found = _has(doctor.consistency(conn, root=project), "cursor")
    assert len(found) == 1
    assert found[0]["level"] == "warn"
    assert "r-ghost" in found[0]["detail"] and "op-1" in found[0]["pointer"]


def test_a_cursor_that_resolves_is_not_reported(project):
    conn = _conn(project)
    _op(conn, "op-1")
    _row(conn, "r-1", "op-1")
    opindex.set_cursor(conn, "op-1", "r-1", session="s")
    assert _has(doctor.consistency(conn, root=project), "cursor") == []


# --------------------------------------------------------------- 3. a live op not advancing
def test_a_live_op_whose_cursor_has_not_advanced_is_reported(project):
    conn = _conn(project)
    try:
        util.set_clock(clock.FixedClock(start=T0))
        _op(conn, "op-1")
        _row(conn, "r-1", "op-1")
        board.claim(conn, "r-1", "worker-a", "s", minutes=600)   # live, leased at T0
        found = _has(doctor.consistency(conn, root=project, now=NOW), "live-not-advancing")
    finally:
        util.set_clock(None)
    assert len(found) == 1
    assert found[0]["level"] == "warn" and "op-1" in found[0]["pointer"]


def test_a_fresh_live_op_is_not_reported(project):
    conn = _conn(project)
    try:
        util.set_clock(clock.FixedClock(start=T0))
        _op(conn, "op-1")
        _row(conn, "r-1", "op-1")
        board.claim(conn, "r-1", "worker-a", "s", minutes=600)
        near = "2026-01-01T00:10:00+00:00"          # inside the staleness window
        found = _has(doctor.consistency(conn, root=project, now=near), "live-not-advancing")
    finally:
        util.set_clock(None)
    assert found == []


# --------------------------------------------------------------- 4. a dead authority still cited
def test_an_expired_authority_still_cited_is_reported(project):
    conn = _conn(project)
    _op(conn, "op-1")
    authority.grant(conn, "L1", "op-1", expiry="2026-02-01T00:00:00+00:00", actor="owner")
    authority.bind_op(conn, "op-1", session="s")
    found = _has(doctor.consistency(conn, root=project, now=NOW), "authority")
    assert len(found) == 1
    assert found[0]["level"] == "warn"
    assert "expired" in found[0]["detail"] and "op-1" in found[0]["pointer"]


def test_a_revoked_authority_still_cited_is_reported(project):
    conn = _conn(project)
    _op(conn, "op-1")
    g = authority.grant(conn, "L1", "op-1", expiry="2027-01-01T00:00:00+00:00", actor="owner")
    authority.bind_op(conn, "op-1", session="s")
    authority.revoke(conn, g["id"], "owner", "no longer trusted")
    found = _has(doctor.consistency(conn, root=project, now=T0), "authority")
    assert len(found) == 1 and "revoked" in found[0]["detail"]


def test_a_live_authority_is_not_reported(project):
    conn = _conn(project)
    _op(conn, "op-1")
    authority.grant(conn, "L1", "op-1", expiry="2027-01-01T00:00:00+00:00", actor="owner")
    authority.bind_op(conn, "op-1", session="s")
    assert _has(doctor.consistency(conn, root=project, now=T0), "authority") == []


def test_a_dead_authority_on_a_closed_op_is_not_reported(project):
    # a closed op citing a since-expired authority is history, not an active reliance on it.
    conn = _conn(project)
    _op(conn, "op-1")
    authority.grant(conn, "L1", "op-1", expiry="2026-02-01T00:00:00+00:00", actor="owner")
    authority.bind_op(conn, "op-1", session="s")
    db.patch(conn, "ops", "id", "op-1", {"status": "closed"})
    assert _has(doctor.consistency(conn, root=project, now=NOW), "authority") == []


# --------------------------------------------------------------- 5. an unresolved collision
def test_an_unresolved_collision_is_reported(project):
    conn = _conn(project)
    resolve.log(conn, resolve.CONCURRENT, "R1",
                {"ref": "R1", "writers": [{"worker": "a"}, {"worker": "b"}]}, now=T0)
    found = _has(doctor.consistency(conn, root=project), "collision")
    assert len(found) == 1 and found[0]["level"] == "warn" and "R1" in found[0]["pointer"]


def test_a_resolved_collision_is_not_reported(project):
    conn = _conn(project)
    resolve.log(conn, resolve.CONCURRENT, "R1",
                {"ref": "R1", "base": "v0", "writers": [{"worker": "a", "value": "vA"}]}, now=T0)
    resolve.pass_(conn, now=T0)
    assert _has(doctor.consistency(conn, root=project), "collision") == []


# --------------------------------------------------------------- 6. a store over its ceiling
def test_a_store_over_its_retention_ceiling_is_reported(project):
    conn = _conn(project)
    project_mod.save(project, {"name": "d", "retention": {"max_bytes": 1}})
    src = os.path.join(project, "F.txt")
    with open(src, "w", encoding="utf-8") as fh:
        fh.write("well over one byte\n")
    retention.snapshot(project, "F.txt", "tag", "a note", now=T0)
    found = _has(doctor.consistency(conn, root=project), "store-ceiling")
    assert len(found) == 1 and found[0]["level"] == "warn"
    assert retention.store_size(project) > 1


def test_a_store_under_its_ceiling_is_not_reported(project):
    conn = _conn(project)
    project_mod.save(project, {"name": "d", "retention": {"max_bytes": 10 ** 9}})
    src = os.path.join(project, "F.txt")
    with open(src, "w", encoding="utf-8") as fh:
        fh.write("small\n")
    retention.snapshot(project, "F.txt", "tag", "a note", now=T0)
    assert _has(doctor.consistency(conn, root=project), "store-ceiling") == []


def test_no_retention_ceiling_is_not_reported(project):
    conn = _conn(project)              # no project.yaml retention policy at all -> no ceiling
    src = os.path.join(project, "F.txt")
    with open(src, "w", encoding="utf-8") as fh:
        fh.write("bytes\n")
    retention.snapshot(project, "F.txt", "tag", "a note", now=T0)
    assert _has(doctor.consistency(conn, root=project), "store-ceiling") == []


# --------------------------------------------------------------- 7. host residue (the port)
def _write_residue(project, allow):
    d = os.path.join(project, ".claude")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "settings.local.json"), "w", encoding="utf-8") as fh:
        json.dump({"permissions": {"allow": allow}}, fh)


def test_a_dangerous_local_grant_is_reported_as_a_warning(project):
    conn = _conn(project)
    _write_residue(project, ["Bash(ls)", "Bash(rm -rf *)"])
    found = _has(doctor.consistency(conn, root=project), "host-residue")
    assert len(found) == 1 and found[0]["level"] == "warn"
    assert "rm -rf" in found[0]["detail"]


def test_a_benign_residue_is_not_reported(project):
    conn = _conn(project)
    _write_residue(project, ["Bash(ls)", "Read(*)"])
    assert _has(doctor.consistency(conn, root=project), "host-residue") == []


def test_an_absent_residue_is_not_reported(project):
    conn = _conn(project)
    assert _has(doctor.consistency(conn, root=project), "host-residue") == []


# --------------------------------------------------------------- ordering: errors before warnings
def test_findings_are_ordered_error_before_warn(project):
    conn = _conn(project)
    _issue_pad_token(conn, "op-ghost")                       # an error
    resolve.log(conn, resolve.CONCURRENT, "R1", {"ref": "R1", "writers": []}, now=T0)  # a warn
    findings = doctor.consistency(conn, root=project)
    levels = [f["level"] for f in findings]
    assert "error" in levels and "warn" in levels
    assert levels.index("error") < levels.index("warn")     # act on the error first


# --------------------------------------------------------------- the selftest
def test_the_selftest_passes_when_every_check_is_live():
    report = doctor.selftest()
    assert report["ok"] is True
    assert report["inert"] == []
    names = {r["name"] for r in report["results"]}
    assert {"pad-op", "cursor", "live-not-advancing", "authority", "collision",
            "store-ceiling", "host-residue"} <= names
    assert doctor.consistency_selftest()["ok"] is True


def test_the_selftest_fails_when_a_check_goes_inert(monkeypatch):
    # a parser that matches nothing on its own fixture is inert: the predecessor's documented
    # failure. Blunt one check's scan to match nothing and the selftest must catch it.
    checks = doctor._CONSISTENCY_CHECKS
    target = next(c for c in checks if c["name"] == "collision")
    patched = dict(target)
    patched["scan"] = lambda conn, root, now: []            # matches nothing: gone inert
    monkeypatch.setattr(doctor, "_CONSISTENCY_CHECKS",
                        [patched if c is target else c for c in checks])
    report = doctor.selftest()
    assert report["ok"] is False
    assert "collision" in report["inert"]
    with pytest.raises(doctor.DoctorSelftestError):
        doctor.consistency_selftest()


# --------------------------------------------------------------- read-only
def _tree_digest(root):
    """A digest of the working tree, excluding the runtime record dir (.alpaca), which a read
    connection legitimately touches (WAL). What must not change is the working tree and the
    record's event count, both asserted around a scan."""
    h = hashlib.sha256()
    for dp, dn, fn in os.walk(root):
        dn[:] = sorted(d for d in dn if d not in (".alpaca", ".git", "__pycache__"))
        for name in sorted(fn):
            p = os.path.join(dp, name)
            h.update(os.path.relpath(p, root).encode("utf-8"))
            try:
                with open(p, "rb") as fh:
                    h.update(fh.read())
            except OSError:
                pass
    return h.hexdigest()


def test_consistency_is_read_only(project):
    conn = _conn(project)
    # seed a record carrying several findings, then prove a scan mutates nothing.
    _issue_pad_token(conn, "op-ghost")
    _op(conn, "op-1")
    opindex.set_cursor(conn, "op-1", "r-ghost", session="s")
    resolve.log(conn, resolve.CONCURRENT, "R1", {"ref": "R1", "writers": []}, now=T0)
    _write_residue(project, ["Bash(rm -rf *)"])

    before_tree = _tree_digest(project)
    before_events = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]

    findings = doctor.consistency(conn, root=project)
    assert findings                                     # the scan did find something to report

    after_tree = _tree_digest(project)
    after_events = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    assert after_tree == before_tree                    # not one working-tree byte changed
    assert after_events == before_events                # not one event appended


# --------------------------------------------------------------- the exit-code contract
def _doctor_ready(project):
    """A doctor-complete project (from the M1.x doctor test scaffold), onboarded, so `alpaca doctor`
    is clean (exit 0) before a consistency finding is introduced."""
    from alpaca import cli
    from alpaca.tests.test_doctor import _full
    _full(project)
    cli.main(["init"])
    cli.main(["onboard", "--name", "d", "--who", "a:owner", "--what", "w"])
    os.makedirs(doctor.paths.transcript_dir(project), exist_ok=True)


def test_alpaca_doctor_exit_code_folds_the_consistency_half(project, capsys):
    from alpaca import cli
    _doctor_ready(project)
    assert cli.main(["doctor"]) == 0                     # clean: exit 0 ok
    capsys.readouterr()

    # a warning-level finding lifts the verdict to WARN (exit 1), no higher.
    conn = _conn(project)
    resolve.log(conn, resolve.CONCURRENT, "R1", {"ref": "R1", "writers": []}, now=T0)
    assert cli.main(["doctor"]) == 1
    out = capsys.readouterr().out
    assert "consistency:collision" in out and "WARN" in out

    # an error-level finding (a pad naming a missing op) lifts the verdict to ERROR (exit 2).
    _issue_pad_token(conn, "op-ghost")
    assert cli.main(["doctor"]) == 2
    out = capsys.readouterr().out
    assert "consistency:pad-op" in out and "ERROR" in out
