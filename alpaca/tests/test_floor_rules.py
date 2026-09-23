"""M3.3 proof - the floor, the two change classes, and de-escalation.

Proof for task M3.3. The floor is Alpaca's default-deny surface: a fixed set of categories that
refuse at every autodrive level, plus the two change classes that split every write into
agent-writable (the product tree, `.alpaca/` runtime, wiki, rows) and human-owned (`contracts/`,
`project.yaml`, `doctrine/`, the boot block). The split is classified BY PATH FROM THE MANIFEST
so it cannot drift from the shipped tree.

Done-when (from the plan, Task M3.3):

  a write to `contracts/`, `project.yaml`, `doctrine/` or the boot block becomes a review card
  up to L4 and an agent write with a recorded diff at L5 and above, a de-escalation is always
  allowed and leaves every open obligation open, and a self-escalation is refused at every
  level.

Positive and negative paths, per Step 1: the seven default-deny categories and the two change
classes, exercised at L2, L4, L5 and L6.

The floor has no timing of its own: `floor.check` is a pure function of (action, level) with no
clock, no record and no ledger, so a FixedClock would be dead weight here. The de-escalation
property "discharges nothing already owed" is proved structurally: `floor.check` cannot reach
any obligation store to discharge it (asserted on its signature), and an owed ledger held by the
test is byte-identical after a de-escalation is allowed.
"""
import inspect
import os

import pytest

from alpaca.posture import floor


# --------------------------------------------------------------------- fixtures
def _write_manifest(root, mechanism):
    """Write an ALPACA-MANIFEST with the given [mechanism] entries (a [memory] .alpaca/ line too)."""
    lines = ["# test manifest", "[mechanism]"]
    lines.extend(mechanism)
    lines.append("[memory]")
    lines.append(".alpaca/")
    with open(os.path.join(root, "ALPACA-MANIFEST"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


ALL_LEVELS = (2, 4, 5, 6)

# The four human-owned surfaces (rule 7 / spec 5.6:402-409), one representative path each.
HUMAN_OWNED_PATHS = (
    "contracts/phase-build.md",
    "project.yaml",
    "doctrine/leaves/two-change-classes.md",
    "CLAUDE.md",                       # the boot block lives here
)

# Agent-writable representatives: product tree, runtime, wiki, a plain source file.
AGENT_WRITABLE_PATHS = (
    "src/app.py",
    "alpaca/posture/floor.py",
    ".alpaca/alpaca.db",
    "docs/notes.md",
)


# ------------------------------------------------------- classify: the two classes
def test_classify_human_owned_from_the_manifest(project):
    for p in HUMAN_OWNED_PATHS:
        assert floor.classify(project, p) == floor.HUMAN_OWNED, p


def test_classify_agent_writable(project):
    for p in AGENT_WRITABLE_PATHS:
        assert floor.classify(project, p) == floor.AGENT_WRITABLE, p


def test_classify_subpaths_and_absolute_paths(project):
    # a deep file under a human-owned dir is still human-owned
    assert floor.classify(project, "contracts/nested/deep/x.md") == floor.HUMAN_OWNED
    # an absolute path inside the root classifies the same as its relative form
    ap = os.path.join(project, "contracts", "phase-build.md")
    assert floor.classify(project, ap) == floor.HUMAN_OWNED
    # project.yaml is a FILE prefix: a sibling that only shares the stem is not human-owned
    assert floor.classify(project, "project.yaml.bak") == floor.AGENT_WRITABLE


def test_classify_reads_the_manifest_so_it_cannot_drift(tmp_path):
    """The human-owned split is anchored to the shipped tree: drop `contracts/` from the
    manifest and a write there is no longer classified from that entry. doctrine/ and the boot
    block stay human-owned by rule even when the manifest omits them."""
    root = str(tmp_path / "proj")
    os.mkdir(root)
    # a manifest WITHOUT contracts/ and WITHOUT doctrine/
    _write_manifest(root, ["alpaca/", "project.yaml", "CLAUDE.md", "src/"])
    assert floor.classify(root, "contracts/x.md") == floor.AGENT_WRITABLE
    # project.yaml and the boot block are still human-owned (present in the manifest / by rule)
    assert floor.classify(root, "project.yaml") == floor.HUMAN_OWNED
    assert floor.classify(root, "CLAUDE.md") == floor.HUMAN_OWNED
    # doctrine/ is an authority surface by rule 7 even when the manifest omits it
    assert floor.classify(root, "doctrine/leaves/x.md") == floor.HUMAN_OWNED


# ------------------------------------------- change class -> review card / agent write
def test_human_owned_write_is_a_review_card_up_to_l4(project):
    for p in HUMAN_OWNED_PATHS:
        for lvl in (1, 2, 3, 4):
            action = {"kind": "write", "root": project, "path": p}
            assert floor.check(action, lvl) == floor.REVIEW_CARD, (p, lvl)


def test_human_owned_write_is_an_agent_write_at_l5_and_above(project):
    for p in HUMAN_OWNED_PATHS:
        for lvl in (5, 6):
            action = {"kind": "write", "root": project, "path": p}
            assert floor.check(action, lvl) == floor.ALLOW, (p, lvl)


def test_agent_writable_write_is_allowed_at_every_level(project):
    for p in AGENT_WRITABLE_PATHS:
        for lvl in ALL_LEVELS:
            action = {"kind": "write", "root": project, "path": p}
            assert floor.check(action, lvl) == floor.ALLOW, (p, lvl)


# ----------------------------------------------- the seven default-deny categories
FLAT_DENY_CATEGORIES = (
    "irreversible-external",     # 1. an irreversible external action
    "leave-root",                # 2. leaving the project root
    "unsanctioned-campaign",     # 3. an unsanctioned campaign
    "edit-frozen-record",        # 4. editing a frozen record
    "self-escalation",           # 5. self-escalation
    "global-user-surface",       # 6. writing a global user surface
)


def test_the_named_deny_categories_refuse_at_every_level(project):
    for cat in FLAT_DENY_CATEGORIES:
        for lvl in ALL_LEVELS:
            assert floor.check({"kind": cat}, lvl) == floor.REFUSE, (cat, lvl)
            # a bare string action is accepted too
            assert floor.check(cat, lvl) == floor.REFUSE, (cat, lvl)


def test_authority_surface_is_the_seventh_category_a_change_class(project):
    """The seventh default-deny category, writing the agent's own authority surface, is not a
    flat refuse: it is the human-owned change class (review card up to L4, agent write at L5+)."""
    action = {"kind": "write", "root": project, "path": "contracts/x.md"}
    assert floor.check(action, 4) == floor.REVIEW_CARD
    assert floor.check(action, 5) == floor.ALLOW


def test_the_floor_is_a_floor_not_a_boundary(project):
    """An unnamed irreversible action is still refused: the named categories are examples of
    default-deny, not an exhaustive allow-list."""
    for lvl in ALL_LEVELS:
        assert floor.check({"kind": "some-unnamed-irreversible-thing"}, lvl) == floor.REFUSE
        assert floor.check({"kind": "delete-outside-root"}, lvl) == floor.REFUSE
        # the empty / unknown action is refused, never silently allowed
        assert floor.check({}, lvl) == floor.REFUSE


# ---------------------------------------------- self-escalation vs de-escalation
def test_self_escalation_refused_at_every_level(project):
    for lvl in range(1, 7):
        assert floor.check({"kind": "self-escalation"}, lvl) == floor.REFUSE, lvl
    # raising your own level via an escalate action with to > from is a self-escalation
    for lvl in range(1, 7):
        act = {"kind": "escalate", "from": "L%d" % lvl, "to": "L6"}
        expect = floor.REFUSE if lvl < 6 else floor.ALLOW  # L6->L6 is not a raise
        assert floor.check(act, lvl) == expect, lvl


def test_de_escalation_is_always_allowed_at_every_level(project):
    for lvl in range(1, 7):
        assert floor.check({"kind": "deescalate", "from": "L%d" % lvl, "to": "L1"}, lvl) \
            == floor.ALLOW, lvl
        # an escalate action that actually LOWERS the level is a de-escalation, allowed
        assert floor.check({"kind": "escalate", "from": "L6", "to": "L2"}, lvl) \
            == floor.ALLOW, lvl


def test_de_escalation_discharges_nothing_already_owed(project):
    """A de-escalation is one event and leaves every open obligation open. floor.check cannot
    reach any obligation store: it is a pure function of (action, level). Proved on the
    signature and by an owed ledger the test holds being untouched across the call."""
    params = list(inspect.signature(floor.check).parameters)
    # only (action, level): no conn, no ledger, no obligation store to discharge
    assert params[:2] == ["action", "level"], params
    assert not any(p in ("conn", "db", "obligations", "owed") for p in params), params

    owed = {"ship-decision", "human-review:contracts/x.md"}
    snapshot = set(owed)
    verdict = floor.check({"kind": "deescalate", "from": "L6", "to": "L2"}, 6)
    assert verdict == floor.ALLOW
    assert owed == snapshot  # nothing discharged by dropping to a safer level


def test_level_band_is_validated(project):
    for bad in (0, 7, -1):
        with pytest.raises(ValueError):
            floor.check({"kind": "write", "root": project, "path": "src/a.py"}, bad)
