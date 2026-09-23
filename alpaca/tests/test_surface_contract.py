"""M4.12: the operating-surface verb budget and the orphan-verb lint.

The CLI is the workspace (D9). This proves the operating surface is a tested contract:
FRONT is small and within budget, every dispatched verb declares its output keys and is
documented, and the lint names any verb that is undocumented, undeclared or unregistered.

Positive path: the shipped tree (REPO) lints clean. Negative paths: a fake dispatched verb with
no declaration and no documentation fails the lint by name; a MANUAL missing a verb reports that
verb undocumented; a declared-but-unregistered verb is named. Throwaway roots and a FixedClock
keep the negative fixtures off the live record.
"""
import os

from alpaca import cli, surface
from alpaca.clock import FixedClock
from alpaca.tests.conftest import REPO


# --------------------------------------------------------------------------- front budget
def test_front_is_small_and_within_budget():
    assert surface.FRONT, "FRONT must not be empty"
    assert len(surface.FRONT) <= surface.FRONT_BUDGET
    # kept small: the front page is a fraction of the whole surface, not most of it.
    assert len(surface.FRONT) < len(surface.table())


def test_front_is_a_subset_of_the_live_dispatch():
    reg = cli.registered_verbs()
    for v in surface.FRONT:
        assert v in reg, "FRONT verb %r is not registered" % v


def test_front_has_no_duplicates():
    assert len(surface.FRONT) == len(set(surface.FRONT))


# --------------------------------------------------------------------------- table shape
def test_table_covers_exactly_the_registered_verbs():
    reg = cli.registered_verbs()
    tab = set(surface.table())
    assert tab == reg, "table and dispatch disagree: %r" % (tab ^ reg)


def test_every_verb_declares_output_keys():
    for verb, spec in surface.table().items():
        assert spec["keys"], "verb %r declares no output keys" % verb
        assert isinstance(spec["keys"], tuple)


def test_a_non_front_verb_is_reachable_but_not_front_page():
    reg = cli.registered_verbs()
    non_front = sorted(reg - set(surface.FRONT))
    assert non_front, "expected verbs beyond the front page"
    v = non_front[0]
    assert surface.reachable(v)          # reachable
    assert not surface.is_front(v)       # but not front-page


# --------------------------------------------------------------------------- positive lint
def test_shipped_tree_lints_clean():
    findings = surface.lint(REPO)
    assert findings == [], "shipped surface is not clean: %r" % findings


# --------------------------------------------------------------------------- negative lint
def test_a_fake_dispatched_verb_fails_the_lint_by_name():
    """Step 1: a fake verb with no declared keys and no documentation must fail, named."""
    fake = "zzfakeverb"
    cli.COMMANDS[fake] = lambda args: 0
    try:
        findings = surface.lint(REPO)
        assert findings, "an undeclared, undocumented verb should fail the lint"
        named = [f for f in findings if f["verb"] == fake]
        assert named, "the finding must name the offending verb %r" % fake
        reasons = {f["reason"] for f in named}
        assert "undeclared" in reasons or "undocumented" in reasons
    finally:
        del cli.COMMANDS[fake]


def test_an_undocumented_verb_is_named(tmp_path):
    """A MANUAL that omits one verb reports that verb undocumented (throwaway root)."""
    clock = FixedClock("2026-01-01T00:00:00+00:00")
    _ = clock()  # timing seam; the lint reads bytes, not the wall clock
    reg = cli.registered_verbs()
    victim = sorted(reg)[0]
    # a MANUAL that documents every verb except the victim
    lines = ["# MANUAL", "", "## Verbs", ""]
    for v in sorted(reg):
        if v == victim:
            continue
        lines.append("- `alpaca %s` - a verb." % v)
    (tmp_path / "MANUAL.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    findings = surface.lint(str(tmp_path))
    named = [f for f in findings if f["verb"] == victim and f["reason"] == "undocumented"]
    assert named, "the omitted verb %r must be reported undocumented" % victim


def test_a_declared_but_unregistered_verb_is_named():
    """A phantom in the table that the CLI does not dispatch fails the lint."""
    phantom = "zzphantom"
    surface.TABLE[phantom] = {"keys": ("verdict",), "group": "ghost"}
    try:
        findings = surface.lint(REPO)
        named = [f for f in findings if f["verb"] == phantom and f["reason"] == "unregistered"]
        assert named, "a declared-but-unregistered verb must be named"
    finally:
        del surface.TABLE[phantom]


def test_lint_reports_a_front_set_over_budget(monkeypatch):
    monkeypatch.setattr(surface, "FRONT_BUDGET", 0)
    findings = surface.lint(REPO)
    assert any(f["reason"] == "over-budget" for f in findings)


def test_missing_manual_reports_every_verb_undocumented(tmp_path):
    # no MANUAL.md at all: every registered verb is undocumented, none crashes the lint.
    reg = cli.registered_verbs()
    findings = surface.lint(str(tmp_path))
    undoc = {f["verb"] for f in findings if f["reason"] == "undocumented"}
    assert reg <= undoc
