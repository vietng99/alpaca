"""M4.14 proof - the boot read-order and the cheap navigator.

Done-when (checklist M4.14): the boot block states an ordered read list whose core set loads
unconditionally for every role and phase BEFORE the router narrows anything; and the navigator is
cheap (a router that costs more than it saves is not a router). This module asserts:

  * the CLAUDE.md boot block states the six reads in order: pad, manifest, core set, router,
    level, modes, with the core set positioned before the router;
  * MAP.md carries the ordered boot sequence and a numbered core section (2) that precedes the
    numbered router section (3); every CORE leaf is named in the core section;
  * the SessionStart hook emits the same ordered read-order;
  * the navigator cost is bounded: MAP.md plus CORE-CARD.md is strictly cheaper than loading the
    whole leaf population, so routing through the card saves reads instead of adding them.
"""
import os
import re

from alpaca.tests.conftest import REPO
from alpaca.gates import doctrine_registration_check as reg

CLAUDE = os.path.join(REPO, "CLAUDE.md")
MAP = os.path.join(REPO, "MAP.md")
CORE_CARD = os.path.join(REPO, "doctrine", "CORE-CARD.md")
LEAVES = os.path.join(REPO, "doctrine", "leaves")
SESSION_START = os.path.join(REPO, "alpaca", "hooks", "session_start.py")


def _read(path):
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def _boot_block():
    text = _read(CLAUDE)
    begin = text.find("<!-- ALPACA:BOOT:BEGIN -->")
    end = text.find("<!-- ALPACA:BOOT:END -->")
    assert begin >= 0 and end > begin, "the boot block markers must be present"
    return text[begin:end]


# ---------------------------------------------------------------- the boot block states the order

def test_boot_block_states_the_ordered_read_list():
    block = _boot_block().lower()
    # the six reads all appear.
    for token in ("pad", "manifest", "core set", "router", "level", "modes"):
        assert token in block, "the boot block omits the read: %s" % token
    # and the core set is positioned before the router (the load-bearing ordering).
    assert block.index("core set") < block.index("router"), \
        "the core set must be read before the router narrows"


def test_boot_block_says_the_router_narrows_only_after_the_core_set():
    block = _boot_block().lower()
    assert "before the router" in block or "after the core set" in block, \
        "the boot block must state that the router narrows only after the core set loads"


# ---------------------------------------------------------------- the navigator carries the order

def test_map_core_section_precedes_the_router_section():
    sections = reg.split_sections(_read(MAP))
    assert 2 in sections and 3 in sections and 4 in sections, "MAP must number its sections"
    core = sections[2]
    router = sections[3]
    # the core section is the always-load set; the router is a separate, later section.
    assert "core" in core.title.lower(), core.title
    assert "router" in router.title.lower(), router.title
    # numbered ordering guarantees section 2 (core) is read before section 3 (router).
    assert core.number < router.number


def test_every_core_leaf_is_named_in_the_map_core_section():
    core = reg.split_sections(_read(MAP))[2]
    for leaf in reg.CORE_SET:
        assert reg.strict_ref(core.text, leaf), "CORE leaf missing from MAP section 2: %s" % leaf


def test_map_states_the_full_ordered_boot_sequence():
    text = _read(MAP).lower()
    # key off the numbered list markers so a mention of a later read in the prose intro does not
    # confound the ordering assertion.
    markers = ["1. pad", "2. manifest", "3. core set", "4. router", "5. level", "6. modes"]
    positions = [text.find(tok) for tok in markers]
    assert all(p >= 0 for p in positions), positions
    assert positions == sorted(positions), "MAP does not list the reads in boot order"


# ---------------------------------------------------------------- the hook emits the order

def test_session_start_emits_the_boot_read_order():
    src = _read(SESSION_START)
    assert "BOOT READ-ORDER" in src
    low = src.lower()
    # key off the emitted numbered markers so the parenthetical "(core set loads before the
    # router narrows)" does not confound the ordering assertion.
    markers = ["1 pad", "2 alpaca-manifest", "3 core set", "4 router", "5 level", "6 modes"]
    positions = [low.find(tok) for tok in markers]
    assert all(p >= 0 for p in positions), positions
    assert positions == sorted(positions), "the hook does not emit the reads in boot order"


# ---------------------------------------------------------------- the navigator is cheap

def test_navigator_is_cheaper_than_loading_the_whole_population():
    nav_cost = os.path.getsize(MAP) + os.path.getsize(CORE_CARD)
    leaf_cost = sum(os.path.getsize(os.path.join(LEAVES, n))
                    for n in os.listdir(LEAVES) if n.endswith(".md"))
    # routing through the card + map must be strictly cheaper than reading every leaf, or the
    # router costs more than it saves and is not a router.
    assert nav_cost < leaf_cost, (nav_cost, leaf_cost)
    # and the core card alone is a small fraction of the whole population.
    assert os.path.getsize(CORE_CARD) < leaf_cost // 2, os.path.getsize(CORE_CARD)
