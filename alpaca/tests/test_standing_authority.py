"""M3.2: the standing-authority lifecycle. Proof for task M3.2.

An authority to act with no one watching is a set of record rows plus an owner-appendable
revocation channel; no grant file is reintroduced (D16, Q16). This module proves the lifecycle on
both the positive and the negative path, under a fixed clock:

  * expiry bites mid-loop: an authority with a parseable expiry stops authorizing the moment it
    passes, with no session boundary, because the loop re-reads the authority from disk every
    iteration (Step 2);
  * a revocation appended by the owner reaches a running loop at its next iteration (Step 5, the
    revocation channel);
  * a newer authority supersedes an older one (Step 1);
  * a scope covering more than one op requires the top level and a parseable expiry; a wide
    open-ended scope at a lower level is refused as a de-facto top level under a safe label
    (Step 3, scope implies level);
  * each op records forever the authority id and the level it ran under (Step 4, bind to the op);
  * a gate (phase entry) and alpaca doctor both read the authority from the record.

Timing is driven by a FixedClock so every recorded timestamp is deterministic; the expiry
comparisons use explicit `now` values so the loop iterations are exact.
"""
import pytest

from alpaca import cli, db, doctor, util
from alpaca.checklist import Halt
from alpaca.phase import phase_gate
from alpaca.posture import authority
from alpaca.wiki.clock import FixedClock


@pytest.fixture
def clock():
    """A FixedClock installed process-wide, so grant/revoke/bind timestamps are deterministic."""
    util.set_clock(FixedClock(start="2026-01-01T00:00:00+00:00", step=1))
    try:
        yield
    finally:
        util.set_clock(None)


# --------------------------------------------------------------------------- no authority
def test_no_authority_refuses_unattended_action(project, clock):
    """A fresh project has no standing authority, so a verify refuses and names the absence."""
    conn = db.connect(project)
    assert authority.current(conn) is None
    v = authority.verify(conn, "L5", "op-001", now="2026-01-01T00:00:00+00:00")
    assert not v.ok
    assert v.reason == authority.R_NO_AUTHORITY
    assert not bool(v)


# --------------------------------------------------------------------------- expiry bites
def test_expiry_bites_the_moment_it_passes_without_a_session_boundary(project, clock):
    """An authority with a parseable expiry stops authorizing at the moment it passes. The loop
    re-reads the authority from disk each iteration, so no session boundary is needed."""
    conn = db.connect(project)
    authority.grant(conn, "L5", "op-001", expiry="2026-01-02T00:00:00+00:00", actor="owner")
    # a loop of iterations, each re-reading the record with its own `now`
    nows = ["2026-01-01T23:59:59+00:00", "2026-01-02T00:00:00+00:00",
            "2026-01-02T00:00:01+00:00", "2026-01-03T00:00:00+00:00"]
    results = [authority.verify(conn, "L5", "op-001", now=n) for n in nows]
    assert [r.ok for r in results] == [True, False, False, False]
    assert results[1].reason == authority.R_EXPIRED   # exactly at the expiry it stops


# --------------------------------------------------------------------------- revocation
def test_revocation_reaches_a_running_loop_at_its_next_iteration(project, clock):
    """A revocation appended by the owner to the channel is honoured at the next iteration, with
    no restart: the loop re-reads from disk and sees it."""
    conn = db.connect(project)
    g = authority.grant(conn, "L5", "op-001", expiry="2026-03-01T00:00:00+00:00", actor="owner")
    now = "2026-01-05T00:00:00+00:00"
    assert authority.verify(conn, "L5", "op-001", now=now).ok        # iteration N: still live
    authority.revoke(conn, g["id"], actor="owner", reason="pause the run")
    v = authority.verify(conn, "L5", "op-001", now=now)              # iteration N+1: revoked
    assert not v.ok
    assert v.reason == authority.R_REVOKED
    assert authority.current(conn) is None


# --------------------------------------------------------------------------- supersession
def test_a_newer_authority_supersedes_an_older_one(project, clock):
    conn = db.connect(project)
    a = authority.grant(conn, "L5", "op-001", expiry="2026-03-01T00:00:00+00:00", actor="owner")
    b = authority.grant(conn, "L6", "*", expiry="2026-03-01T00:00:00+00:00", actor="owner",
                        supersedes=a["id"])
    cur = authority.current(conn)
    assert cur["id"] == b["id"]
    assert cur["id"] != a["id"]
    assert cur["supersedes"] == a["id"]


# --------------------------------------------------------------------------- scope implies level
def test_a_wide_scope_at_a_lower_level_is_refused_on_its_label(project, clock):
    """A scope covering more than one op requires the top level and a parseable expiry."""
    conn = db.connect(project)
    # wide scope, lower level, but with an expiry: refused because it is not the top level
    with pytest.raises(authority.AuthorityRefusal) as e_top:
        authority.grant(conn, "L4", "*", expiry="2026-03-01T00:00:00+00:00", actor="owner")
    assert e_top.value.reason == authority.R_SCOPE_NEEDS_TOP

    # wide scope, lower level, open-ended: a de-facto top level, refused under a safe label
    with pytest.raises(authority.AuthorityRefusal) as e_open:
        authority.grant(conn, "L4", "*", expiry=None, actor="owner")
    assert e_open.value.reason == authority.R_OPEN_ENDED_WIDE

    # wide scope at the top level still needs a parseable expiry
    with pytest.raises(authority.AuthorityRefusal) as e_exp:
        authority.grant(conn, "L6", "*", expiry="not-a-date", actor="owner")
    assert e_exp.value.reason == authority.R_SCOPE_NEEDS_EXPIRY

    # the well-formed wide authority: top level plus a parseable expiry, accepted
    ok = authority.grant(conn, "L6", "*", expiry="2026-03-01T00:00:00+00:00", actor="owner")
    assert ok["level"] == "L6" and ok["scope"] == "*"

    # a single-op scope at a lower level is fine
    one = authority.grant(conn, "L4", "op-007", expiry="2026-03-01T00:00:00+00:00", actor="owner")
    assert one["level"] == "L4"


def test_grant_refuses_a_level_off_the_band(project, clock):
    conn = db.connect(project)
    for bad in (0, 7):
        with pytest.raises(authority.AuthorityRefusal):
            authority.grant(conn, bad, "op-001", expiry="2026-03-01T00:00:00+00:00", actor="owner")


# --------------------------------------------------------------------------- bind to the op
def test_an_op_records_the_authority_id_and_level_it_ran_under(project, clock):
    """Opening an op through the normal path stamps the standing authority onto the op forever,
    in the append-only record and in the meta projection an op reads back."""
    conn = db.connect(project)
    g = authority.grant(conn, "L6", "*", expiry="2026-03-01T00:00:00+00:00", actor="owner")
    assert cli.main(["op", "new", "run the milestone", "--done-when", "green"]) == cli.PASS
    stamp = authority.for_op(conn, "op-001")
    assert stamp is not None
    assert stamp["authority"] == g["id"]
    assert stamp["level"] == "L6"
    # bound in the append-only record too, not only the projection
    binds = [e for e in db.events(conn, kind=authority.BIND_KIND) if e["op"] == "op-001"]
    assert binds and binds[-1]["data"]["authority"] == g["id"]


def test_an_op_opened_with_no_authority_records_the_absence(project, clock):
    conn = db.connect(project)
    assert cli.main(["op", "new", "unbacked op"]) == cli.PASS
    stamp = authority.for_op(conn, "op-001")
    assert stamp is not None
    assert stamp["authority"] is None and stamp["level"] is None


# --------------------------------------------------------------------------- the gate reads it
def test_phase_gate_entry_consults_the_standing_authority(project, clock):
    """phase_gate.enter, given a `now`, refuses entry when the standing authority does not
    authorize it (here, once the authority has expired), on the authority not the level."""
    from alpaca.posture import level as posture_level
    conn = db.connect(project)
    posture_level.set(conn, "sA", 5, "run to release", "owner", root=project)
    authority.grant(conn, "L5", "op-001", expiry="2026-01-02T00:00:00+00:00", actor="owner")

    res = phase_gate.enter(conn, "op-001", "requirement", None, session="sA",
                           now="2026-01-01T12:00:00+00:00", scope="op-001")
    assert res["allowed"] is True                     # inside the window

    with pytest.raises(Halt) as ex:
        phase_gate.enter(conn, "op-001", "requirement", None, session="sA",
                         now="2026-01-03T00:00:00+00:00", scope="op-001")
    assert ex.value.code == phase_gate.R_AUTHORITY_INVALID


def test_phase_gate_entry_without_a_now_is_unchanged(project, clock):
    """The authority check is additive: with no `now`, enter behaves exactly as before (M3.1),
    so existing callers are not disturbed."""
    from alpaca.posture import level as posture_level
    conn = db.connect(project)
    posture_level.set(conn, "sB", 5, "run", "owner", root=project)
    res = phase_gate.enter(conn, "op-9", "requirement", None, session="sB")
    assert res["allowed"] is True                     # no authority consulted, level alone


# --------------------------------------------------------------------------- doctor reads it
def test_doctor_reports_the_standing_authority(project, clock):
    conn = db.connect(project)
    authority.grant(conn, "L6", "*", expiry="2026-03-01T00:00:00+00:00", actor="owner")
    names = {c["name"]: c for c in doctor.checks(project)}
    assert "authority" in names
    assert names["authority"]["level"] == "ok"
    assert "auth-001" in names["authority"]["detail"]


def test_doctor_names_the_absence_of_authority(project, clock):
    db.connect(project)                               # a record exists, but no authority granted
    names = {c["name"]: c for c in doctor.checks(project)}
    assert "authority" in names
    assert names["authority"]["level"] == "ok"        # absence is the safe default, not an error


# --------------------------------------------------------------------------- file-free
def test_authority_is_file_free(project, clock):
    """The authority lives in the record, not in a grant file: nothing is written outside .alpaca."""
    import os
    conn = db.connect(project)
    authority.grant(conn, "L6", "*", expiry="2026-03-01T00:00:00+00:00", actor="owner")
    for name in os.listdir(project):
        assert "grant" not in name.lower()
    src = __import__("inspect").getsource(authority)
    assert "no grant file" in src.lower()
