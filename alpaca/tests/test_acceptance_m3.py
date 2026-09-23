"""M3.13 -- M3 acceptance: unattended to the ship card, and paused at every boundary.

One acceptance module drives a SCRIPTED FORMATION over the sample spec fixture (M1.19,
tests/fixtures/sample_spec/SPEC.md) through the real M3 entry points, on a throwaway project
root (the conftest `project` fixture: a tmp root carrying ALPACA-MANIFEST, with CLAUDE_CONFIG_DIR
pointed away from the live one). The live `.alpaca/` is never touched and no `alpaca` write verb runs
against it.

The formation the acceptance scripts is the shipped `solo` formation (M3.9): one agent carries
the op from intent through the phase ladder to the release boundary, where the one human decision
that stays human at every level -- the ship -- is raised as a review card (M3.5). The acceptance
grounds that identity in the real formation registry (`alpaca.formation.manifest.registry`, M3.7) and
asserts the six formations are registered before it drives one of them.

The four Done-when clauses, each proved by its own test:

  (1) L5 to the ship card, unattended. An op set to L5 (alpaca.posture.level, M3.1) under an L5
      standing authority (alpaca.posture.authority, M3.2) runs every phase boundary automatically
      through the real composed door (alpaca.phase.doors.run, M1.15): four `phase-advance` events,
      mode `auto`, and NO pause of any kind is available to it (zero phase-pause, zero
      question-pause). On that clean op exactly ONE review card stands -- the ship card -- and
      zero others.
  (2) L2 pauses at every boundary. The same op shape at L2 PAUSES at all four boundaries
      (requirement->design, design->build, build->verify, verify->release), each recording a
      `phase-pause` event with a decision pending (`human-go`).
  (3) the ship card is human-only. An agent move of the ship card is refused FAIL
      (alpaca.review.move), and the card is left open for a human.
  (4) the authority trail. Each op reads back, forever, the level and the authority id it ran
      under (alpaca.posture.authority.for_op, alpaca.posture.level.in_force).

Every open() passes encoding="utf-8". No subprocess is spawned; the real CLI is driven in-process
through alpaca.cli.main, exactly as the M3 unit suites do. No em dash anywhere; the harness folder
name and any absolute path to it are never written -- the shared tree is derived from __file__.
"""
import os
import shutil

from alpaca import cli, db, review
from alpaca.checklist import artifact as artifact_mod
from alpaca.checklist import bridge, step_model, synthesis, verdict_row
from alpaca.formation import manifest
from alpaca.gates import verdict as vc
from alpaca.phase import doors
from alpaca.posture import authority, level

# The shared project tree, derived from this file's location (rename-safe: no folder-name literal
# and no absolute path is written into the test). The sample spec fixture and the step models are
# read from it, exactly as the M1 acceptance module reads them.
REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SPEC_FIX = os.path.join(REPO, "alpaca", "tests", "fixtures", "sample_spec", "SPEC.md")
MODELS = os.path.join(REPO, "step-models")

# workspace_guard's NO-TMP invariant BLOCKS a pytest tmp root when driven through the production
# door, so -- exactly as the M1.15 door tests and the M1 acceptance module do -- the acceptance
# isolates the CONTAINMENT property from the tmp-root property with an empty tmp-prefix set and
# drives the real `doors.run` the same way. Nothing about the door is reimplemented.
NOTMP = frozenset()

# The phases whose obligations a boundary door composes, and the four boundaries an M3 op reaches
# from requirement to the release edge (alpaca.phase.defaults.BOUNDARY_PHASE, read straight off 7.4).
PHASES = ("requirement", "design", "build", "verify")
BOUNDARIES = ("requirement->design", "design->build", "build->verify", "verify->release")

# The six shipped formations (M3.9). The acceptance drives the `solo` formation.
SIX_FORMATIONS = ("solo", "builder-verifier", "fan-out", "bug-loop", "nuclear", "napalm")
SCRIPTED_FORMATION = "solo"


# --------------------------------------------------------------------------- scripted formation
def _install_spec(root):
    """Land the sample spec fixture into the throwaway root and return its path."""
    dst = os.path.join(root, "SPEC.md")
    shutil.copy(SPEC_FIX, dst)
    return dst


def _formation_identity(root):
    """Ground the scripted formation in the REAL registry (M3.7): the six formations register,
    and the one the acceptance drives (`solo`) is among them. Returns its manifest."""
    reg = manifest.registry(REPO)
    assert sorted(reg.names()) == sorted(SIX_FORMATIONS), reg.names()
    assert SCRIPTED_FORMATION in reg
    return reg.get(SCRIPTED_FORMATION)


def _open_op_at(conn, root, session, rank, scope):
    """Open one op under the posture the formation runs at: mirror the level for the session
    (M3.1) and stand up a single-op authority at that level (M3.2), then open the op through the
    real `alpaca op new`, which stamps the standing authority onto the op forever. Returns the op id.
    """
    level.set(conn, session, rank, "drive the sample spec through the phase ladder")
    authority.grant(conn, "L%d" % rank, scope)
    before = len(db.rows(conn, "ops", "1=1"))
    code = cli.main(["--session", session, "op", "new",
                     "carry the sample spec from intent to the release edge",
                     "--done-when", "every phase boundary is adjudicated"])
    assert code == vc.PASS
    oid = "op-%03d" % (before + 1)
    return oid


def _land_and_discharge(conn, spec, op, session):
    """The formation's requirement->verify legwork: synthesize each phase's obligations from the
    sample spec through the real step models, land them through the bridge (R1), and discharge
    every one by authoring a verdict row bound to its frozen content hash (M1.13). After this the
    obligation fold for every phase in PHASES is `discharged`, so each boundary door can open.
    """
    art = artifact_mod.parse(spec, "item")
    for phase in PHASES:
        model = step_model.load(os.path.join(MODELS, phase + ".json"))
        rows = synthesis.synthesize(model, art, op=op)
        res = bridge.apply(conn, rows, op=op, session=session)
        assert res["verdict"] == vc.PASS, res
        for row in rows:
            verdict_row.discharge(conn, row["id"], row["content_hash"], "probe",
                                  vc.PASS, [], "L%d" % 5, session)
        assert all(verdict_row.status_fold(conn, r["id"]) == verdict_row.DISCHARGED
                   for r in rows), phase


def _adjudicate(conn, root, op, rank, session):
    """Run the composed door at every boundary at the given level, returning the verdict per
    boundary. This is the formation reaching each phase edge in order."""
    out = []
    for boundary in BOUNDARIES:
        code = doors.run(conn, op, boundary, level="L%d" % rank, root=root,
                         tmp_prefixes=NOTMP, session=session)
        out.append((boundary, code))
    return out


# ------------------------------------------------------------- (1) L5: unattended to the ship card
def test_l5_op_runs_unattended_to_a_single_ship_card(project):
    root = project
    cli.main(["init"])
    conn = db.connect(root)
    _formation_identity(root)
    spec = _install_spec(root)

    session = "s-l5"
    op = _open_op_at(conn, root, session, 5, "op-001")
    # the op reads back the posture it runs under: L5, and the L5 authority it was stamped with.
    assert level.in_force(conn, session) == 5
    assert authority.for_op(conn, op)["level"] == "L5"

    _land_and_discharge(conn, spec, op, session)

    # a clean op has raised no review card yet: curate by exception (M3.5).
    assert review.open_cards(conn) == []

    # every boundary advances automatically at L5, with no human step and none available to it.
    results = _adjudicate(conn, root, op, 5, session)
    assert all(code == vc.PASS for _, code in results), results
    advances = db.events(conn, kind="phase-advance", limit=10 ** 9)
    assert [e["data"]["boundary"] for e in advances] == list(BOUNDARIES)
    assert all(e["data"]["mode"] == "auto" for e in advances), advances
    # NO human step was available to the run: no boundary paused, no owner question paused it.
    assert db.events(conn, kind="phase-pause", limit=10 ** 9) == []
    assert db.events(conn, kind="question-pause", limit=10 ** 9) == []

    # the release edge raises the one decision that stays human at every level: the ship card.
    ship = review.ship_card(conn, op, "publish %s outside the box" % op)
    assert ship["kind"] == review.SHIP
    cards = review.open_cards(conn)
    assert len(cards) == 1, cards                     # exactly ONE review card ...
    assert cards[0]["kind"] == review.SHIP            # ... and it is the ship card, zero others.

    # the record is still an intact hash chain over every event the formation appended.
    ok_chain, reason = db.verify_chain(conn)
    assert ok_chain, reason
    conn.close()


# ------------------------------------------------------- (2) L2: a pause at every phase boundary
def test_l2_op_pauses_at_every_phase_boundary(project):
    root = project
    cli.main(["init"])
    conn = db.connect(root)
    _formation_identity(root)
    spec = _install_spec(root)

    session = "s-l2"
    op = _open_op_at(conn, root, session, 2, "op-001")
    assert level.in_force(conn, session) == 2

    _land_and_discharge(conn, spec, op, session)

    # the same op shape at L2 pauses at every boundary: every link PASSes, but the level makes each
    # boundary a human-go edge, so the door records a pending decision rather than advancing.
    results = _adjudicate(conn, root, op, 2, session)
    assert all(code == vc.PAUSED for _, code in results), results

    pauses = db.events(conn, kind="phase-pause", limit=10 ** 9)
    paused_boundaries = [e["data"]["boundary"] for e in pauses]
    for boundary in BOUNDARIES:
        assert boundary in paused_boundaries, boundary
        row = [e for e in pauses if e["data"]["boundary"] == boundary][-1]
        assert row["data"]["pending"] == "human-go"    # a decision is pending at each boundary
        assert row["data"]["level"] == "L2"
    # nothing advanced: the run reached no edge on its own.
    assert db.events(conn, kind="phase-advance", limit=10 ** 9) == []
    conn.close()


# ------------------------------------------------------- (3) the ship card is a human decision
def test_the_ship_card_is_never_moved_by_an_agent(project):
    root = project
    cli.main(["init"])
    conn = db.connect(root)
    _formation_identity(root)
    spec = _install_spec(root)

    session = "s-ship"
    op = _open_op_at(conn, root, session, 5, "op-001")
    _land_and_discharge(conn, spec, op, session)
    _adjudicate(conn, root, op, 5, session)

    ship = review.ship_card(conn, op, "publish %s outside the box" % op)

    # an agent move of the ship card is refused FAIL, for every agent-shaped identity.
    for agent in ("agent", "harness", "instrument", "cli"):
        try:
            review.move(conn, ship["id"], "ship it", agent)
            assert False, "an agent (%s) must not move the ship card" % agent
        except review.ReviewRefusal as e:
            assert e.reason == review.R_AGENT_MOVE
            assert e.verdict == vc.FAIL
    # the ship card is left standing for a human: it was never moved by an agent.
    standing = review.open_cards(conn)
    assert len(standing) == 1 and standing[0]["id"] == ship["id"]
    assert standing[0]["status"] == review.OPEN

    # and a human identity CAN move it, so the refusal is about the mover, not a frozen card.
    moved = review.move(conn, ship["id"], "approve the ship", "owner")
    assert moved["status"] == review.MOVED and moved["moved_by"] == "owner"
    conn.close()


# ------------------------------------------------------- (4) the authority trail, per op
def test_each_op_reads_back_the_level_and_authority_it_ran_under(project):
    root = project
    cli.main(["init"])
    conn = db.connect(root)
    _formation_identity(root)

    # two ops of the same shape, one at L5 and one at L2, each under its own standing authority.
    op_hi = _open_op_at(conn, root, "s-hi", 5, "op-001")
    op_lo = _open_op_at(conn, root, "s-lo", 2, "op-002")

    # each op reads back FOREVER the level and the authority id it ran under (stamped at open).
    trail_hi = authority.for_op(conn, op_hi)
    trail_lo = authority.for_op(conn, op_lo)
    assert trail_hi["level"] == "L5" and trail_hi["authority"] == "auth-001"
    assert trail_lo["level"] == "L2" and trail_lo["authority"] == "auth-002"
    assert trail_hi["authority"] != trail_lo["authority"]

    # and the session posture reads back the level in force for each op's session, from the record.
    assert level.in_force(conn, "s-hi") == 5
    assert level.in_force(conn, "s-lo") == 2
    conn.close()
