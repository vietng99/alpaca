"""t-099: the pure accept rules (design/research-engine/architecture.md section 3.7).

Done when (the contract of t-099, FR-007 to FR-012 and SC-007 of design/research-engine/spec.md):

  * the FeSens phase 1 and phase 2 example tables reproduce: all ten rows, with their scores
    (design/research-engine/study-fesens.md, section 6);
  * property tests pass for N axes: seeded random contracts of 1 to 6 axes, mixed directions,
    targeted and untargeted axes, margins;
  * when several attempts of a round are accepted, one pure ranking function picks the winner, and
    the ranked list and the reason it returns say why (gap map F06, FR-009, EC-025, EC-056);
  * an attempt with fewer successful repeats than the declared minimum gets no score and is
    recorded as repeats failed, and an accept or reject on a noisy metric without the declared
    repeats is recorded UNTESTED, as is one whose repeat spread is wider than the margin (F40,
    FR-011, DD-11, EC-026, EC-038);
  * the repair rule accepts only fail-to-pass with every frozen base-passing test kept (FR-012,
    EC-063);
  * fail case: a regression past a met target is accepted. It never is once every target is met
    (EC-015), whatever the target's size; while a target is still short, a loss past another, met
    target counts in the summed deficit (FeSens phase 1, row (310, 3300) to (290, 3000)).

Each control has a positive and a negative case. The tests are pure: no file, no record, no
network. The property tests use a fixed seed, so a failure reproduces.
"""
from __future__ import annotations

import dataclasses
import inspect
import itertools
import json
import math
import random

import pytest

from alpaca import determinism
from alpaca.research import accept
from alpaca.research import types as T


# ------------------------------------------------------------------ builders

def metric(mid, direction="higher", target=None, *, margin=0.0, kind="deterministic",
           repeats=1, min_ok=None, median="worse"):
    return T.Metric(id=mid, command="true", parser="markers", unit="u", direction=direction,
                    kind=kind, repeats=repeats, min_ok=repeats if min_ok is None else min_ok,
                    margin=margin, timeout_s=60, target=target, median=median)


def contract(metrics, rule=None, after="stop"):
    """A Contract with only the fields the accept rules read; the rest None."""
    metrics = tuple(metrics)
    targets = tuple(T.Target(m.id, m.target) for m in metrics if m.target is not None)
    if rule is None:
        rule = "deficit-pareto" if targets else "champion"
    kw = {f.name: None for f in dataclasses.fields(T.Contract)}
    kw.update(mode="repair" if rule == "repair" else "improve", metrics=metrics,
              targets=targets, accept_rule=rule, after_targets=after if targets else None)
    return T.Contract(**kw)


def mv(mid, median, *, repeats=None, n_ok=None, spread=None, untested=False):
    reps = (median,) if repeats is None else tuple(repeats)
    ok = [r for r in reps if r is not None]
    if n_ok is None:
        n_ok = len(ok)
    if spread is None and len(ok) >= 2:
        spread = max(ok) - min(ok)
    return T.MetricValue(metric=mid, median=None if median is None else float(median),
                         repeats=reps, n_ok=n_ok, spread=spread if len(ok) >= 2 else spread,
                         untested=untested)


def vals(**medians):
    return {k: mv(k, v) for k, v in medians.items()}


# the FeSens example: CoreMark (higher is better, target 300) and LUT4 (lower, target 3000)
FESENS = contract([metric("perf", "higher", 300.0), metric("lut", "lower", 3000.0)])


# ------------------------------------------------------------------ signatures (architecture 3.7)

SIGNATURES = {
    "improve": "(champion: 'dict[str, MetricValue]', attempt: 'dict[str, MetricValue]', "
               "contract: 'Contract') -> 'AcceptDecision'",
    "deficit_score": "(values: 'dict[str, float]', contract: 'Contract') -> 'float'",
    "targets_met": "(values: 'dict[str, float]', contract: 'Contract') -> 'bool'",
    "pareto_dominates": "(a: 'dict[str, float]', b: 'dict[str, float]', "
                        "contract: 'Contract') -> 'bool'",
    "repair": "(f2p: 'FailToPass', p2p: 'PassToPass', "
              "baseline_p2p: 'PassToPass') -> 'AcceptDecision'",
    "pick_winner": "(candidates: 'list[Candidate]', contract: 'Contract') -> "
                   "'tuple[Candidate | None, list[Ranked], str]'",
}


def test_the_public_functions_keep_the_design_signatures_and_are_built():
    for name, sig in SIGNATURES.items():
        fn = getattr(accept, name)
        assert str(inspect.signature(fn)) == sig, name
    public = {n for n, o in vars(accept).items()
              if not n.startswith("_") and inspect.isfunction(o) and o.__module__ == accept.__name__}
    assert public == set(SIGNATURES)
    # no public constant either: section 3.7 lists only the six functions
    imported = {"annotations", "math", "determinism", "AcceptDecision", "Candidate", "Contract",
                "FailToPass", "MetricValue", "PassToPass", "Ranked"}
    other = {n for n, o in vars(accept).items()
             if not n.startswith("_") and n not in SIGNATURES and n not in imported}
    assert other == set(), other
    # negative: no function of the module is the scaffold stub any more
    src = inspect.getsource(accept)
    assert "NotImplementedError" not in src


# ------------------------------------------------------------------ the FeSens tables

PHASE_1 = [  # (champion, attempt, s_old, s_new, accepted)
    ((200, 5000), (290, 5500), -1.000, -0.867, True),
    ((330, 3300), (600, 4000), -0.100, -0.333, False),
    ((310, 3300), (350, 3000), -0.100, 0.000, True),
    ((310, 3300), (290, 3000), -0.100, -0.033, True),
]
PHASE_2_CHAMPION = (320, 2900)
PHASE_2 = [  # (attempt, accepted, s_new)
    ((340, 2900), True, 0.0),
    ((320, 2700), True, 0.0),
    ((340, 2800), True, 0.0),
    ((340, 2950), False, 0.0),
    ((310, 2900), False, 0.0),
    ((350, 3050), False, -0.017),
]


@pytest.mark.parametrize("old,new,s_old,s_new,want", PHASE_1)
def test_fesens_phase_1_rows_reproduce(old, new, s_old, s_new, want):
    d = accept.improve(vals(perf=old[0], lut=old[1]), vals(perf=new[0], lut=new[1]), FESENS)
    assert d.phase == "deficit"
    assert d.accepted is want
    assert d.outcome == ("improvement" if want else "regression")
    assert round(d.champion_score, 3) == s_old
    assert round(d.score, 3) == s_new
    assert not d.untested and not d.repeats_failed
    assert round(accept.deficit_score({"perf": old[0], "lut": old[1]}, FESENS), 3) == s_old


@pytest.mark.parametrize("new,want,s_new", PHASE_2)
def test_fesens_phase_2_rows_reproduce(new, want, s_new):
    champ = vals(perf=PHASE_2_CHAMPION[0], lut=PHASE_2_CHAMPION[1])
    assert accept.targets_met({"perf": 320, "lut": 2900}, FESENS)
    d = accept.improve(champ, vals(perf=new[0], lut=new[1]), FESENS)
    assert d.phase == "pareto"
    assert d.accepted is want
    assert d.champion_score == 0.0
    assert round(d.score, 3) == s_new


def test_fesens_row_past_the_area_target_is_rejected_as_a_regression_past_a_met_target():
    d = accept.improve(vals(perf=320, lut=2900), vals(perf=350, lut=3050), FESENS)
    assert not d.accepted and d.outcome == "regression"
    assert "past a met target" in d.reason and "lut" in d.reason


# ------------------------------------------------------------------ the fail case (EC-015)

def test_fail_case_a_regression_past_a_met_target_is_never_accepted():
    # champion meets both targets; the attempt gains hugely on perf and slips just past lut's target
    champ = vals(perf=320, lut=2900)
    for lut in (3000.5, 3001, 3100, 9000):
        d = accept.improve(champ, vals(perf=10_000, lut=lut), FESENS)
        assert not d.accepted, lut
        assert d.outcome == "regression"
    # positive: at the target exactly is not past it, and with no other loss it dominates
    d = accept.improve(vals(perf=320, lut=2900), vals(perf=400, lut=2900), FESENS)
    assert d.accepted
    # after_targets pareto runs the same rule (DD-01): still rejected
    pareto = contract(FESENS.metrics, after="pareto")
    assert not accept.improve(champ, vals(perf=10_000, lut=3001), pareto).accepted


def test_a_regression_on_an_untargeted_axis_is_rejected_once_every_target_is_met():
    # DD-15: the untargeted axis joins the Pareto comparison once every target is met
    c = contract([metric("perf", "higher", 300.0), metric("power", "lower")])
    champ = vals(perf=320, power=10)
    assert not accept.improve(champ, vals(perf=400, power=11), c).accepted
    # positive: the same gain with no loss on the untargeted axis is accepted
    d = accept.improve(champ, vals(perf=400, power=10), c)
    assert d.accepted and d.phase == "pareto"
    # and a gain on the untargeted axis alone is accepted too
    assert accept.improve(champ, vals(perf=320, power=9), c).accepted


CYCLES = contract([metric("cycles", "lower", 5e9), metric("size", "lower")])


def test_a_one_unit_regression_past_a_large_met_target_is_rejected():
    # a deterministic cycle count with a target of 5e9: one cycle past it is past it
    champ = vals(cycles=5_000_000_000, size=100)
    for cycles in (5_000_000_001, 5_000_000_004):
        d = accept.improve(champ, vals(cycles=cycles, size=90), CYCLES)
        assert d.phase == "pareto" and not d.accepted and d.outcome == "regression", cycles
        assert "past a met target" in d.reason
        # the reason shows the difference it acted on, not 5e+09 against 5e+09
        assert "cycles %d against target 5000000000" % cycles in d.reason
        assert not accept.targets_met({"cycles": cycles, "size": 90}, CYCLES)
        assert accept.deficit_score({"cycles": cycles, "size": 90}, CYCLES) < 0.0
    # positive: at the target exactly is met, and the size gain then dominates
    assert accept.targets_met({"cycles": 5_000_000_000, "size": 90}, CYCLES)
    d = accept.improve(champ, vals(cycles=5_000_000_000, size=90), CYCLES)
    assert d.accepted and d.outcome == "improvement"
    # a one-cycle loss on a large untargeted axis is a loss too
    c = contract([metric("perf", "higher", 300.0), metric("cyc", "lower")])
    assert not accept.improve(vals(perf=320, cyc=5e9), vals(perf=400, cyc=5e9 + 1), c).accepted


def test_a_tiny_regression_past_a_met_target_is_rejected():
    d = accept.improve(vals(perf=300, lut=2900), vals(perf=300 - 2e-7, lut=2800), FESENS)
    assert not d.accepted and d.outcome == "regression" and "perf" in d.reason
    assert not accept.targets_met({"perf": 300 - 2e-7, "lut": 2800}, FESENS)


def test_while_a_target_is_short_a_loss_past_another_met_target_counts_in_the_deficit():
    # FR-008's FeSens phase 1: the champion misses lut, so the summed deficit decides; the perf
    # loss past its met target is charged in the sum, not refused on its own (EC-015 is scoped
    # to the phase where every target is met)
    d = accept.improve(vals(perf=310, lut=3300), vals(perf=290, lut=3000), FESENS)
    assert d.phase == "deficit" and d.accepted
    assert ("worse on perf (310 -> 290 falls past its met target 300, charged in the summed "
            "deficit while lut is short)") in d.reason
    # check-adversarial-r2 M1 (A2): the reason names the crossing for a smaller loss as well
    d = accept.improve(vals(perf=310, lut=3300), vals(perf=295, lut=3000), FESENS)
    assert d.accepted and "perf (310 -> 295 falls past its met target 300" in d.reason
    # negative: a loss that stays at or past the met target is only named as worse
    d = accept.improve(vals(perf=310, lut=3300), vals(perf=305, lut=3000), FESENS)
    assert d.accepted and "worse on perf (310 -> 305)" in d.reason
    assert "falls past" not in d.reason
    # a loss past the met target larger than the gain on the short one is rejected
    d = accept.improve(vals(perf=310, lut=3300), vals(perf=200, lut=3000), FESENS)
    assert d.phase == "deficit" and not d.accepted


def test_the_deficit_reason_names_an_untargeted_axis_that_got_worse():
    c = contract([metric("perf", "higher", 300.0), metric("power", "lower")])
    d = accept.improve(vals(perf=200, power=10), vals(perf=250, power=1e9), c)
    assert d.accepted and d.phase == "deficit"          # DD-15: no deficit on power
    assert "worse on power (10 -> 1000000000, untargeted, no deficit under DD-15)" in d.reason
    # negative: nothing worse, nothing named
    d = accept.improve(vals(perf=200, power=10), vals(perf=250, power=10), c)
    assert d.accepted and "worse on" not in d.reason


def test_an_untargeted_axis_takes_no_deficit_while_a_target_is_short():
    c = contract([metric("perf", "higher", 300.0), metric("power", "lower")])
    assert accept.deficit_score({"perf": 150, "power": 1e9}, c) == pytest.approx(-0.5)
    champ = vals(perf=200, power=10)
    # phase deficit: a loss on the untargeted axis costs nothing, a perf gain is accepted
    d = accept.improve(champ, vals(perf=250, power=50), c)
    assert d.phase == "deficit" and d.accepted
    # negative: a gain on the untargeted axis alone does not raise the score
    d = accept.improve(champ, vals(perf=200, power=1), c)
    assert not d.accepted and d.outcome == "regression"


# ------------------------------------------------------------------ strict improvement and margin

def test_strict_rule_needs_a_gain_larger_than_the_margin():
    c = contract([metric("ops", "higher", margin=5.0, kind="noisy", repeats=3, min_ok=2)])
    champ = {"ops": mv("ops", 100, repeats=(100, 101, 99))}

    def att(v):
        return {"ops": mv("ops", v, repeats=(v, v + 1, v - 1))}

    d = accept.improve(champ, att(106), c)
    assert d.accepted and d.phase == "strict" and d.outcome == "improvement"
    assert d.score == 106 and d.champion_score == 100 and d.margin == 5.0
    for v in (105, 103, 100, 90):          # equal to the margin, within it, equal, worse
        d = accept.improve(champ, att(v), c)
        assert not d.accepted and d.outcome == "regression", v
    assert "only" in accept.improve(champ, att(103), c).reason


def test_strict_rule_with_a_lower_is_better_metric_and_no_margin():
    c = contract([metric("ms", "lower")])
    assert accept.improve(vals(ms=10), vals(ms=9.5), c).accepted
    assert not accept.improve(vals(ms=10), vals(ms=10), c).accepted
    assert not accept.improve(vals(ms=10), vals(ms=10.5), c).accepted


def test_float_noise_does_not_flip_a_verdict_at_the_margin():
    c = contract([metric("x", "higher", margin=0.1, kind="noisy", repeats=2, min_ok=2)])
    champ = {"x": mv("x", 0.2, repeats=(0.2, 0.2))}
    # 0.1 + 0.2 is 0.30000000000000004: a gain equal to the margin up to the last bit
    d = accept.improve(champ, {"x": mv("x", 0.1 + 0.2, repeats=(0.1 + 0.2, 0.1 + 0.2))}, c)
    assert not d.accepted
    d = accept.improve(champ, {"x": mv("x", 0.31, repeats=(0.31, 0.31))}, c)
    assert d.accepted


def test_a_gain_within_the_margin_counts_as_no_change_in_the_deficit_phase():
    c = contract([metric("perf", "higher", 300.0, margin=20.0, kind="noisy", repeats=2, min_ok=2),
                  metric("lut", "lower", 3000.0)])

    def v(p, l):
        return {"perf": mv("perf", p, repeats=(p, p)), "lut": mv("lut", l)}

    # a perf gain of 15 lies within the 20 margin: no credit, so the score does not rise
    d = accept.improve(v(200, 2900), v(215, 2900), c)
    assert not d.accepted and "within the margin" in d.reason
    # positive: a gain of 25 is past the margin
    assert accept.improve(v(200, 2900), v(225, 2900), c).accepted
    # a loss within the margin still counts in full
    d = accept.improve(v(200, 3100), v(190, 3000), c)
    assert d.score == pytest.approx(-110 / 300)


def test_a_gain_that_only_just_clears_its_margin_cannot_pay_for_a_real_loss():
    # check-adversarial-r1-rerun B1 (case A10): perf gains 11 against a margin of 10, lut loses
    # 109 on a deterministic axis with margin 0. Crediting the whole 11 lets the summed deficit
    # rise by 0.1 perf units, inside the noise; credited net of its margin (1) the score falls.
    c = contract([metric("perf", "higher", 300.0, margin=10.0, kind="noisy", repeats=2, min_ok=2),
                  metric("lut", "lower", 3000.0)])

    def v(p, l):
        return {"perf": mv("perf", p, repeats=(p, p)), "lut": mv("lut", l)}

    d = accept.improve(v(280, 3300), v(291, 3409), c)
    assert d.phase == "deficit"
    assert not d.accepted and d.outcome == "regression", d.reason
    assert d.score == pytest.approx((281 - 300) / 300 + (3000 - 3409) / 3000)
    assert d.score < d.champion_score
    assert "perf 280 -> 291 credited as 281 (margin 10)" in d.reason
    assert "worse on lut (3300 -> 3409)" in d.reason
    # positive: a gain well past the margin still pays for the same loss (net 30 of perf)
    d = accept.improve(v(280, 3300), v(320, 3409), c)
    assert d.accepted and d.outcome == "improvement"
    assert d.score == pytest.approx(0.0 + (3000 - 3409) / 3000)
    # the loss side is not cut: a lut loss inside no margin counts in full
    d = accept.improve(v(280, 3300), v(280, 3301), c)
    assert not d.accepted


def test_gains_just_past_their_margins_on_several_axes_cannot_hide_a_loss():
    # check-adversarial-r1-rerun B1 (case A10b): a and b each gain 6 against a margin of 5, and c
    # loses 11.9 on a margin-0 axis. In full the gains sum to 12 and cover it; net they sum to 2.
    c = contract([metric("a", "higher", 100.0, margin=5.0, kind="noisy", repeats=2, min_ok=2),
                  metric("b", "higher", 100.0, margin=5.0, kind="noisy", repeats=2, min_ok=2),
                  metric("c", "higher", 100.0)])

    def v(a, b, x):
        return {"a": mv("a", a, repeats=(a, a)), "b": mv("b", b, repeats=(b, b)),
                "c": mv("c", x)}

    d = accept.improve(v(50, 50, 50), v(56, 56, 38.1), c)
    assert d.phase == "deficit" and not d.accepted, d.reason
    # a lower-is-better axis is credited net of its margin in its own direction
    low = contract([metric("w", "lower", 100.0, margin=5.0, kind="noisy", repeats=2, min_ok=2),
                    metric("x", "higher", 100.0)])
    champ = {"w": mv("w", 200, repeats=(200, 200)), "x": mv("x", 50)}
    d = accept.improve(champ, {"w": mv("w", 194, repeats=(194, 194)), "x": mv("x", 48.5)}, low)
    assert not d.accepted and "w 200 -> 194 credited as 199 (margin 5)" in d.reason
    d = accept.improve(champ, {"w": mv("w", 180, repeats=(180, 180)), "x": mv("x", 48.5)}, low)
    assert d.accepted


def test_a_gain_within_the_margin_does_not_make_pareto_dominance():
    c = contract([metric("perf", "higher", 300.0, margin=20.0, kind="noisy", repeats=2, min_ok=2),
                  metric("lut", "lower", 3000.0)])

    def v(p, l):
        return {"perf": mv("perf", p, repeats=(p, p)), "lut": mv("lut", l)}

    assert not accept.improve(v(320, 2900), v(335, 2900), c).accepted
    assert accept.improve(v(320, 2900), v(345, 2900), c).accepted
    assert accept.improve(v(320, 2900), v(335, 2899), c).accepted   # lut has margin 0


def test_a_median_too_large_for_a_float_gives_no_score_and_does_not_crash():
    # check-adversarial-r1-rerun m3 (case B8): 10**400 cannot become a float
    c = contract([metric("perf", "higher", margin=0.0, kind="noisy", repeats=3, min_ok=2)])
    huge = T.MetricValue("perf", 10 ** 400, (10 ** 400,) * 3, 3, 0)
    d = accept.improve({"perf": mv("perf", 100, repeats=(100, 100, 100))}, {"perf": huge}, c)
    assert not d.accepted and d.outcome == "repeats-failed" and d.score is None
    assert "perf has no median" in d.reason
    with pytest.raises(ValueError, match="no finite median"):
        accept.improve({"perf": huge}, {"perf": mv("perf", 100, repeats=(100, 100, 100))}, c)
    # positive: a large int that fits a float is a value like any other
    big = T.MetricValue("perf", 10 ** 20, (10 ** 20,) * 3, 3, 0)
    d = accept.improve({"perf": mv("perf", 100, repeats=(100, 100, 100))}, {"perf": big}, c)
    assert d.accepted and d.outcome == "improvement"


# ------------------------------------------------------------------ repeats failed and UNTESTED

NOISY = contract([metric("wall", "lower", 120.0, margin=4.0, kind="noisy", repeats=5, min_ok=3)])


def noisy(reps):
    ok = sorted(r for r in reps if r is not None)
    med = ok[(len(ok) - 1) // 2 + (1 if len(ok) % 2 == 0 else 0)] if ok else None
    return {"wall": mv("wall", med, repeats=reps)}


def test_fewer_successful_repeats_than_the_minimum_gets_no_score():
    champ = noisy((130, 131, 130, 132, 131))
    d = accept.improve(champ, noisy((100, 101, None, None, None)), NOISY)
    assert not d.accepted and d.outcome == "repeats-failed" and d.repeats_failed
    assert d.score is None
    assert "fewer than the minimum 3" in d.reason
    assert d.untested                       # measured without its declared repeats as well
    assert T.VERDICT_OF[d.outcome] == "rejected"
    # a metric that was not measured at all, and one with no median
    d = accept.improve(champ, {}, NOISY)
    assert d.outcome == "repeats-failed" and "not measured" in d.reason
    d = accept.improve(champ, {"wall": mv("wall", None, repeats=(None,) * 5, n_ok=0)}, NOISY)
    assert d.outcome == "repeats-failed" and d.score is None
    # negative: at the minimum it gets a score (and is UNTESTED, below)
    d = accept.improve(champ, noisy((100, 101, 100, None, None)), NOISY)
    assert d.outcome != "repeats-failed" and d.score is not None and not d.repeats_failed


def test_a_noisy_metric_without_its_declared_repeats_is_untested_accept_or_reject():
    champ = noisy((130, 131, 130, 132, 131))
    # the rule alone would accept: 100 beats 130 by more than 4
    d = accept.improve(champ, noisy((100, 101, 100, None, None)), NOISY)
    assert d.untested and not d.accepted and d.outcome == "untested"
    assert "3 of 5 declared repeats" in d.reason
    assert "would have accepted (improvement)" in d.reason
    # the rule alone would reject
    d = accept.improve(champ, noisy((140, 141, 140, None, None)), NOISY)
    assert d.untested and d.outcome == "untested"
    assert "would have rejected (regression)" in d.reason
    # positive: all five repeats within the margin, so the verdict stands
    d = accept.improve(champ, noisy((100, 101, 100, 102, 101)), NOISY)
    assert d.accepted and not d.untested and d.outcome == "improvement"


def test_a_spread_wider_than_the_margin_is_untested():
    champ = noisy((130, 131, 130, 132, 131))
    d = accept.improve(champ, noisy((100, 110, 100, 101, 100)), NOISY)   # spread 10 > 4
    assert d.untested and d.outcome == "untested" and "spread 10" in d.reason
    # a spread equal to the margin is not wider
    d = accept.improve(champ, noisy((100, 104, 100, 101, 100)), NOISY)
    assert not d.untested and d.accepted
    # an untested champion makes the comparison untested too
    wide = noisy((130, 140, 130, 132, 131))
    d = accept.improve(wide, noisy((100, 101, 100, 102, 101)), NOISY)
    assert d.untested and "champion wall" in d.reason
    # the gate chain's own untested flag is honored
    flagged = {"wall": dataclasses.replace(noisy((100, 101, 100, 102, 101))["wall"], untested=True)}
    assert accept.improve(champ, flagged, NOISY).untested


def test_the_repeat_count_rule_is_for_noisy_metrics_only():
    det = contract([metric("n", "higher", repeats=3, min_ok=2)])
    champ = {"n": mv("n", 10, repeats=(10, 10, 10))}
    d = accept.improve(champ, {"n": mv("n", 12, repeats=(12, 12, None))}, det)
    assert d.accepted and not d.untested
    # a deterministic metric whose repeats disagree has a spread wider than its margin of 0
    d = accept.improve(champ, {"n": mv("n", 12, repeats=(12, 13, 12))}, det)
    assert d.untested



def test_more_recorded_repeats_than_the_contract_declares_is_untested():
    # check-adversarial-r2-rerun: extra repeats move the rebuilt median, so padding an honest
    # record with four more 97s would turn a gain inside the margin into an accept
    c = contract([metric("t", "lower", margin=5.0, kind="noisy", repeats=5, min_ok=3)])
    champ = {"t": T.MetricValue("t", 107.0, (107.0,) * 5, 5, 0.0)}
    honest = (102.0, 102.0, 102.0, 97.0, 97.0)

    def value(reps):
        s = sorted(reps)
        return {"t": T.MetricValue("t", s[len(s) // 2], reps, len(reps), max(reps) - min(reps))}

    d = accept.improve(champ, value(honest), c)
    assert not d.accepted and d.outcome == "regression" and not d.untested
    assert "by only 5, not more than the margin 5" in d.reason
    d = accept.improve(champ, value(honest + (97.0,) * 4), c)
    assert not d.accepted and d.outcome == "untested" and d.untested
    assert "9 repeats recorded, the contract declares 5" in d.reason
    assert "would have accepted (improvement)" in d.reason
    # the champion side is checked the same way
    padded = {"t": T.MetricValue("t", 107.0, (107.0,) * 6, 6, 0.0)}
    d = accept.improve(padded, value((96.0,) * 5), c)
    assert d.untested and "champion t: 6 repeats recorded, the contract declares 5" in d.reason
    # a deterministic metric declares max(1, repeats): one repeat too many is untested as well
    det = contract([metric("n", "higher")])
    d = accept.improve(vals(n=10), {"n": mv("n", 12, repeats=(12, 12))}, det)
    assert d.untested and "2 repeats recorded, the contract declares 1" in d.reason
    zero = contract([dataclasses.replace(metric("n", "higher"), repeats=0, min_ok=1)])
    assert accept.improve(vals(n=10), vals(n=12), zero).accepted
    # negative: exactly the declared count keeps the verdict
    d = accept.improve(champ, value((96.0,) * 5), c)
    assert d.accepted and not d.untested

T3 = contract([metric("t", "lower", margin=2.0, kind="noisy", repeats=3, min_ok=2)])
T3_MIN3 = contract([metric("t", "lower", margin=2.0, kind="noisy", repeats=3, min_ok=3)])
T3_CHAMP = {"t": T.MetricValue(metric="t", median=100.0, repeats=(100.0, 100.0, 100.0), n_ok=3,
                               spread=0.0)}


def raw(median, repeats, n_ok, spread):
    """A MetricValue exactly as given, n_ok and spread unchecked (what a faulty gate could send)."""
    return {"t": T.MetricValue(metric="t", median=median, repeats=tuple(repeats), n_ok=n_ok,
                               spread=spread)}


@pytest.mark.parametrize("bad", [float("nan"), None, float("inf")])
def test_a_repeat_that_gave_no_number_is_not_a_success_whatever_n_ok_says(bad):
    att = raw(50.0, (50.0, bad, 50.0), 3, None)
    d = accept.improve(T3_CHAMP, att, T3)
    assert not d.accepted and d.untested and d.outcome == "untested"
    assert "2 of 3 declared repeats succeeded" in d.reason
    # with min_ok 3 the same measurement has no score
    d = accept.improve(T3_CHAMP, att, T3_MIN3)
    assert d.outcome == "repeats-failed" and d.repeats_failed and d.score is None
    assert "2 successful repeats, fewer than the minimum 3" in d.reason
    # positive: three numbers and n_ok 3 stand
    d = accept.improve(T3_CHAMP, raw(50.0, (50.0, 50.0, 50.0), 3, 0.0), T3_MIN3)
    assert d.accepted and not d.untested


@pytest.mark.parametrize("given", [0.0, -80.0, float("nan"), float("inf")])
def test_a_recorded_spread_that_disagrees_with_the_repeats_is_untested(given):
    d = accept.improve(T3_CHAMP, raw(50.0, (10.0, 50.0, 90.0), 3, given), T3)
    assert not d.accepted and d.untested and d.outcome == "untested"
    assert "repeat spread 80 is wider than the margin 2" in d.reason    # computed from the repeats
    # positive: the true spread of 1 within the margin is honored
    d = accept.improve(T3_CHAMP, raw(50.0, (49.5, 50.0, 50.5), 3, 1.0), T3)
    assert d.accepted and not d.untested


def test_a_recorded_spread_that_is_not_a_number_with_no_repeats_is_untested():
    for given in (float("nan"), -1.0, float("inf")):
        d = accept.improve(T3_CHAMP, raw(50.0, (), 3, given), T3)
        assert d.untested and not d.accepted, given
        assert "is not a finite number of 0 or more" in d.reason
    # negative: no repeats recorded is no success, whatever n_ok and a sound spread say
    d = accept.improve(T3_CHAMP, raw(50.0, (), 3, 1.0), T3)
    assert not d.accepted and d.outcome == "repeats-failed" and d.score is None
    assert "0 successful repeats, fewer than the minimum 2" in d.reason
    # the recorded spread is also used when only one repeat is a number
    d = accept.improve(T3_CHAMP, raw(50.0, (50.0,), 1, 5.0), contract(
        [metric("t", "lower", margin=2.0)]))
    assert d.untested and "spread 5 is wider" in d.reason


T5 = contract([metric("t", "lower", margin=2.0, kind="noisy", repeats=5, min_ok=3)])
T5_CHAMP = {"t": T.MetricValue(metric="t", median=100.0, repeats=(100.0,) * 5, n_ok=5,
                               spread=0.0)}


@pytest.mark.parametrize("spread", [None, 0.0])
def test_a_value_that_records_no_repeat_has_no_success_whatever_n_ok_says(spread):
    # check-adversarial-r2 B1 (A9, A9b): a noisy metric, repeats=() but n_ok equal to the count
    d = accept.improve(T5_CHAMP, raw(50.0, (), 5, spread), T5)
    assert not d.accepted and d.outcome == "repeats-failed" and d.repeats_failed
    assert d.score is None and d.untested
    assert "0 successful repeats, fewer than the minimum 3" in d.reason
    assert "0 of 5 declared repeats succeeded" in d.reason
    # with min_ok 0 it gets a score, but the verdict is UNTESTED, never an accept
    t5_min0 = contract([metric("t", "lower", margin=2.0, kind="noisy", repeats=5, min_ok=0)])
    d = accept.improve(T5_CHAMP, raw(50.0, (), 5, spread), t5_min0)
    assert not d.accepted and d.outcome == "untested" and d.untested
    assert "would have accepted (improvement)" in d.reason
    # positive: the same n_ok with its five repeats recorded is an improvement
    d = accept.improve(T5_CHAMP, raw(50.0, (50.0,) * 5, 5, 0.0), T5)
    assert d.accepted and d.outcome == "improvement" and not d.untested


def test_a_deterministic_value_that_records_no_repeat_is_refused():
    # check-adversarial-r2 A9c: deterministic, min_ok 3, no repeat recorded
    det = contract([metric("t", "lower", repeats=3, min_ok=3)])
    d = accept.improve(T3_CHAMP, raw(50.0, (), 3, None), det)
    assert d.outcome == "repeats-failed" and d.score is None and not d.accepted
    # with min_ok 0 the median has no repeat behind it: UNTESTED
    det0 = contract([metric("t", "lower", repeats=3, min_ok=0)])
    d = accept.improve(T3_CHAMP, raw(50.0, (), 3, None), det0)
    assert d.outcome == "untested" and "has no finite repeat behind it" in d.reason
    # positive
    d = accept.improve(T3_CHAMP, raw(50.0, (50.0,) * 3, 3, 0.0), det)
    assert d.accepted and not d.untested


def test_a_recorded_median_that_disagrees_with_the_repeats_is_untested():
    # check-adversarial-r2 B2 (A12): every lut repeat is 3100, past the met target 3000, but the
    # recorded median says 2900; it must not pass as a strict Pareto improvement
    c = contract([metric("perf", "higher", 300.0, repeats=3), metric("lut", "lower", 3000.0,
                                                                      repeats=3)])
    champ = {"perf": T.MetricValue(metric="perf", median=320.0, repeats=(320.0,) * 3, n_ok=3,
                                   spread=0.0),
             "lut": T.MetricValue(metric="lut", median=2950.0, repeats=(2950.0,) * 3, n_ok=3,
                                  spread=0.0)}
    forged = {"perf": T.MetricValue(metric="perf", median=400.0, repeats=(400.0,) * 3, n_ok=3,
                                    spread=0.0),
              "lut": T.MetricValue(metric="lut", median=2900.0, repeats=(3100.0,) * 3, n_ok=3,
                                   spread=0.0)}
    d = accept.improve(champ, forged, c)
    assert not d.accepted and d.outcome == "untested" and d.untested
    assert "the recorded median 2900.0 disagrees with the repeats' median 3100.0" in d.reason
    assert "The rule alone would have rejected (regression): regresses past a met target" \
        in d.reason
    # positive: the honest median 3100 is rejected as a regression past the met target
    honest = dict(forged, lut=dataclasses.replace(forged["lut"], median=3100.0))
    d = accept.improve(champ, honest, c)
    assert not d.accepted and d.outcome == "regression" and not d.untested
    assert "regresses past a met target" in d.reason
    # A12b: every repeat equals the champion's 100, a median of 10 is no improvement
    d = accept.improve(T3_CHAMP, raw(10.0, (100.0,) * 3, 3, 0.0), T3)
    assert not d.accepted and d.outcome == "untested"
    assert "the recorded median 10.0 disagrees with the repeats' median 100.0" in d.reason
    # a forged champion median makes the comparison untested too
    champ_forged = {"t": dataclasses.replace(T3_CHAMP["t"], median=1000.0)}
    d = accept.improve(champ_forged, raw(50.0, (50.0,) * 3, 3, 0.0), T3)
    assert d.outcome == "untested" and "champion t: the recorded median 1000" in d.reason


@pytest.mark.parametrize("direction,rule,reps,median", [
    ("lower", "worse", (10.0, 20.0, 30.0, 40.0), 30.0),    # the higher middle is worse
    ("higher", "worse", (10.0, 20.0, 30.0, 40.0), 20.0),   # the lower middle is worse
    ("lower", "middle", (0.1, 0.2, 0.7, 0.9), (0.2 + 0.7) / 2),
    ("higher", "worse", (5.0, None, 1.0, 3.0), 3.0),        # a failed repeat is left out
])
def test_the_median_is_rebuilt_by_the_metric_rule(direction, rule, reps, median):
    c = contract([metric("t", direction, kind="noisy", repeats=len(reps), min_ok=1,
                         margin=100.0, median=rule)])
    ok = [r for r in reps if r is not None]
    champ = {"t": T.MetricValue(metric="t", median=median, repeats=reps, n_ok=len(ok),
                                spread=max(ok) - min(ok))}
    d = accept.improve(champ, champ, c)
    assert "recorded median" not in d.reason
    # negative: the other middle value is refused
    s = sorted(ok)
    other = s[len(s) // 2] if median != s[len(s) // 2] else s[len(s) // 2 - 1]
    bad = {"t": dataclasses.replace(champ["t"], median=other)}
    d = accept.improve(champ, bad, c)
    assert d.outcome == "untested" and "disagrees with the repeats' median" in d.reason


def test_a_mean_median_must_be_exactly_the_mean_of_the_middle_repeats():
    # measure computes (low + high) / 2.0, so an honest record matches bit for bit
    c = contract([metric("t", "lower", kind="noisy", repeats=2, min_ok=2, margin=1.0,
                         median="middle")])
    a, b = 0.1, 0.7
    champ = {"t": T.MetricValue(metric="t", median=1.0, repeats=(1.0, 1.0), n_ok=2, spread=0.0)}
    ok = {"t": T.MetricValue(metric="t", median=(a + b) / 2.0, repeats=(a, b), n_ok=2,
                             spread=b - a)}
    d = accept.improve(champ, ok, c)
    assert not d.untested and d.outcome == "regression" and "recorded median" not in d.reason
    # negative: one unit in the last place off the mean is a disagreement
    off = {"t": dataclasses.replace(ok["t"], median=math.nextafter((a + b) / 2.0, 1.0))}
    d = accept.improve(champ, off, c)
    assert d.outcome == "untested" and "disagrees" in d.reason


# check-adversarial-r3 B1: the recorded median of a "middle" metric sits within 1e-12 of the
# repeats' mean, which at 1e9 is 1e-3. The rule must judge the repeats, and the record is UNTESTED.
BIG = contract([metric("perf", "higher", 300.0, margin=5.0, repeats=2, median="middle"),
                metric("lut", "lower", 1e9, margin=0.0, repeats=2, median="middle")])


def _two(mid, median, reps):
    reps = tuple(float(r) for r in reps)
    return T.MetricValue(metric=mid, median=float(median), repeats=reps, n_ok=len(reps),
                         spread=max(reps) - min(reps))


def test_a_mean_median_forged_inside_a_relative_band_cannot_pass_a_regression_past_a_target():
    # M-A: every lut repeat is 1e9 + 1e-4, past the met target 1e9; the recorded median says 1e9
    past = 1e9 + 1e-4
    champ = {"perf": _two("perf", 320, (320, 320)), "lut": _two("lut", 1e9, (1e9, 1e9))}
    att = {"perf": _two("perf", 400, (400, 400)), "lut": _two("lut", 1e9, (past, past))}
    assert not accept.targets_met({"perf": 400.0, "lut": past}, BIG)
    d = accept.improve(champ, att, BIG)
    assert not d.accepted and d.outcome == "untested" and d.untested and d.phase == "pareto"
    assert "The rule alone would have rejected (regression): regresses past a met target" \
        in d.reason
    assert "the recorded median 1000000000.0 disagrees with the repeats' median %r" % past \
        in d.reason
    # positive: the honest record is rejected by the rule alone, and not UNTESTED
    honest = dict(att, lut=_two("lut", past, (past, past)))
    d = accept.improve(champ, honest, BIG)
    assert not d.accepted and d.outcome == "regression" and not d.untested
    # the reason shows the difference the rule acted on, not 1000000000 against 1000000000
    assert "lut %r against target 1000000000 (lower is better)" % past in d.reason
    # and an honest record at the target is an improvement
    at = dict(att, lut=_two("lut", 1e9, (1e9, 1e9)))
    d = accept.improve(champ, at, BIG)
    assert d.accepted and d.outcome == "improvement" and not d.untested


@pytest.mark.parametrize("value,margin,median", [
    (1e9, 1e-4, 1e9 + 5e-4),       # M-B: a tie turned into a gain past the margin
    (100.0, 0.0, 100.0 + 9e-11),   # M-C: a tie at margin 0
])
def test_a_forged_mean_median_cannot_turn_a_tie_into_an_improvement(value, margin, median):
    c = contract([metric("m", "higher", margin=margin, repeats=2, median="middle")])
    champ = {"m": _two("m", value, (value, value))}
    d = accept.improve(champ, {"m": _two("m", median, (value, value))}, c)
    assert not d.accepted and d.outcome == "untested"
    assert "The rule alone would have rejected (regression)" in d.reason
    assert d.score == value                         # the score is the repeats' median
    # positive: the honest tie is a plain regression, and a real gain past the margin wins
    d = accept.improve(champ, {"m": _two("m", value, (value, value))}, c)
    assert not d.accepted and d.outcome == "regression" and not d.untested
    gain = value + max(1.0, 10 * margin)
    d = accept.improve(champ, {"m": _two("m", gain, (gain, gain))}, c)
    assert d.accepted and d.outcome == "improvement"


def test_the_champion_is_judged_on_the_median_of_its_repeats():
    # a champion whose recorded median is lower than its repeats cannot make a tie look like a gain
    c = contract([metric("m", "higher", margin=0.0, repeats=2, median="middle")])
    champ = {"m": _two("m", 99.0, (100.0, 100.0))}
    d = accept.improve(champ, {"m": _two("m", 100.0, (100.0, 100.0))}, c)
    assert not d.accepted and d.outcome == "untested" and d.champion_score == 100.0
    assert "champion m: the recorded median 99.0 disagrees" in d.reason
    assert "The rule alone would have rejected (regression)" in d.reason


def test_an_unknown_median_rule_is_refused():
    with pytest.raises(ValueError, match="median rule"):
        accept.improve(T3_CHAMP, T3_CHAMP, contract([metric("t", "lower", median="mean")]))


def test_a_value_filed_under_another_metric_id_is_not_that_metric():
    # check-adversarial-r2 A17
    c = contract([metric("cyc", "lower")])
    champ = {"cyc": T.MetricValue(metric="cyc", median=10.0, repeats=(10.0,), n_ok=1,
                                  spread=None)}
    wrong = {"cyc": T.MetricValue(metric="other", median=1.0, repeats=(1.0,), n_ok=1,
                                  spread=None)}
    d = accept.improve(champ, wrong, c)
    assert not d.accepted and d.outcome == "repeats-failed" and d.score is None
    assert "value filed under it is for metric 'other'" in d.reason
    with pytest.raises(ValueError, match="is for metric 'other'"):
        accept.improve(wrong, champ, c)
    # positive: the same value under its own id is an improvement
    right = {"cyc": dataclasses.replace(wrong["cyc"], metric="cyc")}
    assert accept.improve(champ, right, c).accepted


# ------------------------------------------------------------------ the baseline (round 0)

def test_the_baseline_is_judged_with_an_empty_champion():
    d = accept.improve({}, vals(perf=200, lut=5000), FESENS)
    assert d.accepted and d.outcome == "baseline" and d.phase == "baseline"
    assert d.score == pytest.approx(-1.0) and d.champion_score is None
    assert T.VERDICT_OF[d.outcome] == "baseline"
    d = accept.improve({}, noisy((100, 101, None, None, None)), NOISY)
    assert d.outcome == "repeats-failed" and d.phase == "baseline"
    d = accept.improve({}, noisy((100, 110, 100, 101, 100)), NOISY)
    assert d.outcome == "untested" and not d.accepted and d.phase == "baseline"


# ------------------------------------------------------------------ refused inputs

def test_a_contract_that_does_not_fit_its_rule_is_refused():
    with pytest.raises(ValueError):
        accept.improve(vals(a=1), vals(a=2), contract([metric("a")], rule="repair"))
    with pytest.raises(ValueError):
        accept.improve(vals(a=1, b=1), vals(a=2, b=2),
                       contract([metric("a"), metric("b")], rule="champion"))
    with pytest.raises(ValueError):
        accept.improve(vals(a=1), vals(a=2), contract([metric("a")], rule="deficit-pareto"))
    with pytest.raises(ValueError):          # the champion always has every metric
        accept.improve(vals(perf=300), vals(perf=310, lut=2000), FESENS)
    with pytest.raises(ValueError):
        accept.deficit_score({"perf": 300}, FESENS)
    with pytest.raises(ValueError):
        accept.pareto_dominates({"perf": 1, "lut": float("nan")}, {"perf": 1, "lut": 1}, FESENS)
    # a target on an undeclared metric, and two disagreeing copies of one target
    bad = dataclasses.replace(FESENS, targets=FESENS.targets + (T.Target("area", 1.0),))
    with pytest.raises(ValueError):
        accept.deficit_score({"perf": 1, "lut": 1}, bad)
    bad = dataclasses.replace(FESENS, targets=(T.Target("perf", 301.0), T.Target("lut", 3000.0)))
    with pytest.raises(ValueError):
        accept.targets_met({"perf": 1, "lut": 1}, bad)
    # positive: the valid forms do not raise
    assert accept.improve(vals(a=1), vals(a=2), contract([metric("a")])).accepted


def test_a_metric_with_an_unknown_direction_or_a_bad_margin_is_refused():
    for bad in (dict(direction="Higher"), dict(direction=""), dict(margin=-0.5),
                dict(margin=float("nan")), dict(margin=float("inf")), dict(kind="Noisy"),
                dict(kind=None)):
        m = dataclasses.replace(metric("t", "higher"), **bad)
        with pytest.raises(ValueError):
            accept.improve(vals(t=10), vals(t=5), contract([m]))
    # positive: both directions and a margin of 0 are fine
    assert accept.improve(vals(t=10), vals(t=11), contract([metric("t", "higher")])).accepted
    assert accept.improve(vals(t=10), vals(t=9), contract([metric("t", "lower")])).accepted


def test_a_metric_kind_typo_cannot_skip_the_declared_repeats_check():
    # check-adversarial-r3 A31: kind 'Noisy' would skip the noisy repeat count, 1 of 5 accepted
    m = metric("m", "higher", margin=5.0, kind="noisy", repeats=5, min_ok=1)
    champ = {"m": mv("m", 100, repeats=(100.0,) * 5)}
    att = {"m": mv("m", 200, repeats=(200.0, None, None, None, None))}
    with pytest.raises(ValueError, match="kind 'Noisy'"):
        accept.improve(champ, att, contract([dataclasses.replace(m, kind="Noisy")]))
    d = accept.improve(champ, att, contract([m]))
    assert not d.accepted and d.outcome == "untested" and "1 of 5 declared repeats" in d.reason


def test_targets_met_is_false_with_no_target_or_a_missing_value():
    assert not accept.targets_met({"a": 5}, contract([metric("a")]))
    assert not accept.targets_met({"perf": 400}, FESENS)
    assert accept.targets_met({"perf": 300, "lut": 3000}, FESENS)      # at the target is met
    assert not accept.targets_met({"perf": 299.9, "lut": 3000}, FESENS)


def test_deficit_uses_the_absolute_target_as_scale_and_one_for_a_zero_target():
    c = contract([metric("gain", "higher", -5.0), metric("err", "lower", 0.0)])
    assert accept.deficit_score({"gain": -6.0, "err": 0.0}, c) == pytest.approx(-0.2)
    assert accept.deficit_score({"gain": -5.0, "err": 0.5}, c) == pytest.approx(-0.5)
    assert accept.deficit_score({"gain": -4.0, "err": -1.0}, c) == 0.0


# ------------------------------------------------------------------ property tests over N axes

SEED = 20260930
TRIALS = 400


def _random_contract(rng, n, *, margins=False):
    ms = []
    for i in range(n):
        direction = rng.choice(("higher", "lower"))
        target = rng.choice((None, round(rng.uniform(50, 150), 1))) if i else \
            round(rng.uniform(50, 150), 1)                       # at least one target
        margin = round(rng.uniform(0, 5), 1) if margins else 0.0
        ms.append(metric("m%d" % i, direction, target, margin=margin))
    return contract(ms)


def _random_point(rng, c):
    return {m.id: float(rng.choice((rng.uniform(0, 200), m.target if m.target is not None
                                    else rng.uniform(0, 200), rng.randint(0, 200))))
            for m in c.metrics}


def _as_mv(point):
    return {k: mv(k, v) for k, v in point.items()}


def _cases():
    rng = random.Random(SEED)
    for _ in range(TRIALS):
        n = rng.randint(1, 6)
        c = _random_contract(rng, n)
        yield rng, c, _random_point(rng, c), _random_point(rng, c), _random_point(rng, c)


def test_property_pareto_dominance_is_a_strict_partial_order():
    for rng, c, a, b, x in _cases():
        assert not accept.pareto_dominates(a, a, c)
        if accept.pareto_dominates(a, b, c):
            assert not accept.pareto_dominates(b, a, c)
            if accept.pareto_dominates(b, x, c):
                assert accept.pareto_dominates(a, x, c)


def test_property_deficit_is_zero_exactly_when_targets_are_met_and_never_positive():
    for rng, c, a, b, x in _cases():
        for p in (a, b, x):
            s = accept.deficit_score(p, c)
            assert s <= 0.0
            assert (s == 0.0) == accept.targets_met(p, c)


def test_property_improving_one_axis_never_lowers_the_deficit_score():
    for rng, c, a, b, x in _cases():
        m = rng.choice(c.metrics)
        better = dict(a)
        better[m.id] = a[m.id] + (7.0 if m.direction == "higher" else -7.0)
        assert accept.deficit_score(better, c) >= accept.deficit_score(a, c)
        assert accept.pareto_dominates(better, a, c)


def _o(m, x):
    return x if m.direction == "higher" else -x


def _ref_deficit(p, c):
    """The FR-008 formula written out here, not taken from accept: the sum over targeted axes of
    min(0, oriented (v - t) / |t|), with 1 for a target of 0."""
    total = 0.0
    for m in c.metrics:
        if m.target is not None:
            total += min(0.0, (_o(m, p[m.id]) - _o(m, m.target)) / (abs(m.target) or 1.0))
    return total


def _ref_met(p, c):
    return all(_o(m, p[m.id]) >= _o(m, m.target) for m in c.metrics if m.target is not None)


def _ref_dominates(a, b, c):
    """a strictly Pareto-dominates b: a plain loop over every axis."""
    strictly = False
    for m in c.metrics:
        if _o(m, a[m.id]) < _o(m, b[m.id]):
            return False
        if _o(m, a[m.id]) > _o(m, b[m.id]):
            strictly = True
    return strictly


def test_property_the_phase_follows_the_champion_and_the_verdict_follows_its_rule():
    seen = {"deficit": 0, "pareto": 0, True: 0, False: 0}
    for rng, c, a, b, x in _cases():
        if rng.random() < 0.5:           # make half the champions meet every target
            for m in c.metrics:
                if m.target is not None:
                    a[m.id] = m.target + (3.0 if m.direction == "higher" else -3.0)
        d = accept.improve(_as_mv(a), _as_mv(b), c)
        met = _ref_met(a, c)
        assert d.phase == ("pareto" if met else "deficit")
        if met:
            want = _ref_met(b, c) and _ref_dominates(b, a, c)
        else:
            want = _ref_deficit(b, c) > _ref_deficit(a, c) + 1e-12
        assert d.accepted is want, (c.metrics, a, b, d.reason)
        assert d.outcome == ("improvement" if want else "regression")
        assert d.score == pytest.approx(_ref_deficit(b, c), abs=1e-12)
        assert d.champion_score == pytest.approx(_ref_deficit(a, c), abs=1e-12)
        seen[d.phase] += 1
        seen[d.accepted] += 1
    assert min(seen.values()) > 20, seen          # both phases and both verdicts were exercised


def test_property_no_regression_past_a_met_target_is_ever_accepted():
    for rng, c, a, b, x in _cases():
        for m in c.metrics:                        # a champion that meets every target
            if m.target is not None:
                a[m.id] = m.target + (rng.uniform(0, 5) if m.direction == "higher"
                                      else -rng.uniform(0, 5))
        targeted = [m for m in c.metrics if m.target is not None]
        m = rng.choice(targeted)
        b[m.id] = m.target + (-rng.uniform(0.5, 50) if m.direction == "higher"
                              else rng.uniform(0.5, 50))
        for other in c.metrics:                    # and gains everywhere else
            if other.id != m.id:
                b[other.id] = a[other.id] + (100.0 if other.direction == "higher" else -100.0)
        d = accept.improve(_as_mv(a), _as_mv(b), c)
        assert not d.accepted and d.outcome == "regression", (a, b, d.reason)


def test_property_every_accepted_attempt_beats_the_champion_by_more_than_a_margin():
    rng = random.Random(SEED + 1)
    accepted = 0
    for _ in range(TRIALS):
        c = _random_contract(rng, rng.randint(1, 6), margins=True)
        a, b = _random_point(rng, c), _random_point(rng, c)
        if rng.random() < 0.5:
            for m in c.metrics:                    # a small step from the champion
                b[m.id] = a[m.id] + rng.uniform(-6, 6)
        d = accept.improve(_as_mv(a), _as_mv(b), c)
        if d.accepted:
            accepted += 1
            gains = [(b[m.id] - a[m.id]) * (1 if m.direction == "higher" else -1) - m.margin
                     for m in c.metrics]
            assert max(gains) > 0, (c.metrics, a, b)
    assert accepted > 20


def _ref_credit(champ, att, c):
    """The FR-010 crediting written out here: a gain within its margin is no gain, a gain past it
    counts net of the margin, a loss counts in full."""
    out = {}
    for m in c.metrics:
        gain = _o(m, att[m.id]) - _o(m, champ[m.id])
        if gain <= 0 or m.margin == 0:
            out[m.id] = att[m.id]
        elif gain <= m.margin:
            out[m.id] = champ[m.id]
        else:
            out[m.id] = champ[m.id] + (gain - m.margin) * (1 if m.direction == "higher" else -1)
    return out


def test_property_a_deficit_verdict_holds_with_every_gain_cut_by_its_margin():
    # check-adversarial-r1-rerun B1: half the attempts gain just past the margin on one axis and
    # lose a little on another, the trade the old full credit let through
    rng = random.Random(SEED + 2)
    seen = {True: 0, False: 0, "trade": 0}
    for _ in range(TRIALS):
        c = _random_contract(rng, rng.randint(2, 6), margins=True)
        a, b = _random_point(rng, c), _random_point(rng, c)
        if rng.random() < 0.5:
            b = dict(a)
            up, down = rng.sample(list(c.metrics), 2)
            b[up.id] = a[up.id] + _o(up, up.margin + rng.uniform(0.01, 2.0))
            b[down.id] = a[down.id] - _o(down, rng.uniform(0.01, 2.0))
            seen["trade"] += 1
        if _ref_met(a, c):
            continue
        d = accept.improve(_as_mv(a), _as_mv(b), c)
        assert d.phase == "deficit"
        want = _ref_deficit(_ref_credit(a, b, c), c) > _ref_deficit(a, c) + 1e-9
        tie = abs(_ref_deficit(_ref_credit(a, b, c), c) - _ref_deficit(a, c)) <= 1e-9
        if not tie:
            assert d.accepted is want, (c.metrics, a, b, d.reason)
        if d.accepted:
            assert _ref_deficit(_ref_credit(a, b, c), c) > _ref_deficit(a, c)
        seen[d.accepted] += 1
    assert min(seen.values()) > 20, seen


def test_property_the_verdict_does_not_depend_on_axis_order_or_axis_orientation():
    for rng, c, a, b, x in _cases():
        d = accept.improve(_as_mv(a), _as_mv(b), c)
        shuffled = list(c.metrics)
        rng.shuffle(shuffled)
        c2 = contract(shuffled)
        d2 = accept.improve(_as_mv(a), _as_mv(b), c2)
        assert (d2.accepted, d2.outcome, d2.phase) == (d.accepted, d.outcome, d.phase)
        # flip each axis: negate values and target, swap the direction
        flip = contract([dataclasses.replace(
            m, direction="lower" if m.direction == "higher" else "higher",
            target=None if m.target is None else -m.target) for m in c.metrics])
        neg = lambda p: {k: -v for k, v in p.items()}    # noqa: E731
        d3 = accept.improve(_as_mv(neg(a)), _as_mv(neg(b)), flip)
        assert (d3.accepted, d3.outcome, d3.phase) == (d.accepted, d.outcome, d.phase)
        assert d3.score == pytest.approx(d.score)



def test_a_gain_past_the_margin_is_named_net_even_when_its_net_part_rounds_away():
    # 1e17 + 32 against a margin of 31 clears the margin; its net 1 is below the float spacing
    # of 1e17 (16), so it is credited as the champion's value, and the reason must not call it
    # a gain within the margin
    c = contract([metric("x", "higher", 2e17, margin=31.0)])
    d = accept.improve(vals(x=1e17), vals(x=1e17 + 32), c)
    assert d.phase == "deficit" and not d.accepted
    assert "gains past the margin count net of it: x" in d.reason
    assert "within the margin" not in d.reason
    # negative: a gain inside the margin is named so
    d = accept.improve(vals(x=1e17), vals(x=1e17 + 16), c)
    assert "gains within the margin on x count as no change" in d.reason
    assert "past the margin" not in d.reason

# ------------------------------------------------------------------ the winner pick (F06)

def cand(task, slot, decision, metrics=None, round_n=1):
    return T.Candidate(task=task, round=round_n, slot=slot, metrics=metrics or {},
                       decision=decision)


def _judged(task, slot, champ, point, c=FESENS):
    m = _as_mv(point)
    return cand(task, slot, accept.improve(_as_mv(champ), m, c), m)


def test_several_accepted_attempts_one_winner_with_the_ranking_and_the_reason():
    champ = {"perf": 200, "lut": 5000}
    cs = [_judged("t-11", 0, champ, {"perf": 250, "lut": 5000}),     # score -0.833
          _judged("t-12", 1, champ, {"perf": 290, "lut": 4000}),     # score -0.367, the best
          _judged("t-13", 2, champ, {"perf": 190, "lut": 5000}),     # rejected
          _judged("t-14", 3, champ, {"perf": 280, "lut": 4500})]     # score -0.567
    assert [c.decision.accepted for c in cs] == [True, True, False, True]
    winner, ranked, reason = accept.pick_winner(cs, FESENS)
    assert winner.task == "t-12"
    assert [r.task for r in ranked] == ["t-12", "t-14", "t-11", "t-13"]
    assert [r.rank for r in ranked] == [1, 2, 3, 4]
    assert "t-12 (slot 1) wins, 3 of 4 attempts accepted" in reason
    assert "higher score" in reason and "t-14" in reason
    assert ranked[-1].reason.startswith("rejected (regression")
    # the record keeps it: plain data with no NaN, the same bytes on every call
    blob = determinism.canonical([T.to_dict(r) for r in ranked] + [reason])
    assert blob == determinism.canonical(
        [T.to_dict(r) for r in accept.pick_winner(list(reversed(cs)), FESENS)[1]] + [reason])


def test_an_accepted_attempt_ranks_before_a_rejected_one_with_a_better_score():
    c = contract([metric("wall", "lower", 120.0, margin=4.0, kind="noisy", repeats=5, min_ok=3),
                  metric("n", "higher", 10.0)])

    def point(reps, n):
        return {"wall": mv("wall", sorted(r for r in reps if r)[2], repeats=reps),
                "n": mv("n", n)}

    champ = point((200, 201, 200, 202, 201), 5)
    good = point((150, 151, 150, 152, 151), 5)             # accepted, score above the champion's
    untested = point((121, 130, 121, 121, 121), 10)        # a better score, spread 9 > 4
    cs = [cand("t-u", 0, accept.improve(champ, untested, c), untested),
          cand("t-g", 1, accept.improve(champ, good, c), good)]
    assert cs[0].decision.outcome == "untested"
    assert cs[0].decision.score > cs[1].decision.score and cs[1].decision.accepted
    winner, ranked, reason = accept.pick_winner(cs, c)
    assert winner.task == "t-g" and [r.task for r in ranked] == ["t-g", "t-u"]
    assert reason.endswith("the only accepted attempt")



@pytest.mark.parametrize("forge", [
    {"accepted": True},                                              # outcome untested
    {"accepted": True, "untested": False},                           # outcome untested, flag off
    {"accepted": True, "outcome": "regression", "untested": False},
    {"accepted": True, "outcome": "improvement", "untested": True},
    {"accepted": True, "outcome": "improvement", "untested": False, "repeats_failed": True},
    {"accepted": True, "outcome": "baseline", "untested": False},
])
def test_a_decision_that_disagrees_with_itself_never_wins(forge):
    # check-adversarial-r2-rerun A14c: an untested decision with accepted forced to True won
    c = contract([metric("t", "lower", margin=2.0, kind="noisy", repeats=3, min_ok=3)])
    champ = {"t": mv("t", 100, repeats=(100, 100, 100))}
    good = {"t": mv("t", 90, repeats=(90, 90, 90))}
    wide = {"t": mv("t", 1, repeats=(1, 1, 9))}                   # spread 8 > 2: untested
    bad = accept.improve(champ, wide, c)
    assert bad.outcome == "untested" and not bad.accepted
    forged = dataclasses.replace(bad, **forge)
    cs = [cand("x", 0, forged, wide), cand("a", 1, accept.improve(champ, good, c), good)]
    winner, ranked, reason = accept.pick_winner(cs, c)
    assert winner.task == "a" and [r.task for r in ranked] == ["a", "x"]
    assert ranked[1].key[0] == 1 and ranked[1].reason.startswith("rejected")
    assert reason.endswith("the only accepted attempt")
    # alone it gives no winner
    assert accept.pick_winner([cs[0]], c)[0] is None

def test_the_winner_pick_is_the_same_for_every_input_order():
    champ = {"perf": 200, "lut": 5000}
    cs = [_judged("t-%d" % i, i, champ, {"perf": 200 + 10 * (i % 3), "lut": 5000 - 100 * (i % 2)})
          for i in range(6)]
    first = accept.pick_winner(cs, FESENS)
    for perm in itertools.permutations(cs):
        w, ranked, reason = accept.pick_winner(list(perm), FESENS)
        assert (w.task, [r.task for r in ranked], reason) == \
            (first[0].task, [r.task for r in first[1]], first[2])


def test_ties_fall_to_the_primary_metric_then_the_slot():
    champ = {"perf": 320, "lut": 2900}                 # pareto phase: every score is 0
    cs = [_judged("t-a", 2, champ, {"perf": 340, "lut": 2900}),
          _judged("t-b", 1, champ, {"perf": 330, "lut": 2800}),
          _judged("t-c", 0, champ, {"perf": 340, "lut": 2850})]
    winner, ranked, reason = accept.pick_winner(cs, FESENS)
    # t-a and t-c tie on score and perf (the primary metric); the lower slot wins
    assert [r.task for r in ranked] == ["t-c", "t-a", "t-b"]
    assert "lower slot 0 breaks the tie" in reason
    cs2 = [cs[1], cs[0]]
    winner, ranked, reason = accept.pick_winner(cs2, FESENS)
    assert winner.task == "t-a" and "better perf" in reason


def test_strict_rule_ranks_by_the_metric_direction():
    c = contract([metric("ms", "lower")])
    cs = [_judged("t-1", 0, {"ms": 10}, {"ms": 9}, c),
          _judged("t-2", 1, {"ms": 10}, {"ms": 7}, c),
          _judged("t-3", 2, {"ms": 10}, {"ms": 8}, c)]
    winner, ranked, reason = accept.pick_winner(cs, c)
    assert winner.task == "t-2" and [r.task for r in ranked] == ["t-2", "t-3", "t-1"]


ERR = contract([metric("err", "lower")])


def test_the_better_of_two_tiny_medians_wins_and_the_reason_is_true():
    cs = [_judged("t-a", 1, {"err": 1e-5}, {"err": 4e-7}, ERR),
          _judged("t-b", 2, {"err": 1e-5}, {"err": 1e-7}, ERR)]
    assert all(c.decision.accepted for c in cs)
    winner, ranked, reason = accept.pick_winner(cs, ERR)
    assert winner.task == "t-b" and [r.task for r in ranked] == ["t-b", "t-a"]
    assert "better score, err 1e-07 against 4e-07 for t-a (lower is better)" in reason
    assert "equal" not in reason
    assert all(len(r.key) == 7 for r in ranked)
    # a champion of 0 scales by the round's largest median, so small units still separate
    hi = contract([metric("hits", "higher")])
    cs = [_judged("t-a", 1, {"hits": 0.0}, {"hits": 1e-7}, hi),
          _judged("t-b", 2, {"hits": 0.0}, {"hits": 4e-7}, hi)]
    assert accept.pick_winner(cs, hi)[0].task == "t-b"


def test_a_tie_inside_the_ranking_band_is_named_as_a_band_not_as_equal():
    cs = [_judged("t-a", 1, {"err": 1e-5}, {"err": 4e-7}, ERR),
          _judged("t-b", 2, {"err": 1e-5}, {"err": 4e-7 * (1 - 1e-9)}, ERR)]
    winner, ranked, reason = accept.pick_winner(cs, ERR)
    # within 1e-6 relative the banded parts tie; the exact medians decide before the slot
    assert winner.task == "t-b" and [r.task for r in ranked] == ["t-b", "t-a"]
    assert "equal within the 1e-6 ranking band" in reason
    assert "the exact values decide, better err (lower is better)" in reason
    assert "slot" not in reason.split(": ", 1)[1]
    # negative: an exact tie says equal and no band
    cs = [_judged("t-a", 1, {"err": 1e-5}, {"err": 4e-7}, ERR),
          _judged("t-b", 2, {"err": 1e-5}, {"err": 4e-7}, ERR)]
    reason = accept.pick_winner(cs, ERR)[2]
    assert "score equal and err equal; the lower slot 1" in reason and "band" not in reason


CYC = contract([metric("cyc", "lower", margin=0.0)])


def test_a_band_tie_on_a_large_count_falls_to_the_exact_order_before_the_slot():
    # check-adversarial-r4.md: 10000 and 12000 cycles off a 5e9 champion band together at 1e-6
    for ga, gb in [(10000, 12000), (10000, 10001)]:
        cs = [_judged("t-A", 1, {"cyc": 5e9}, {"cyc": 5e9 - ga}, CYC),
              _judged("t-B", 2, {"cyc": 5e9}, {"cyc": 5e9 - gb}, CYC)]
        assert all(c.decision.accepted for c in cs)
        for order in (cs, cs[::-1]):
            winner, ranked, reason = accept.pick_winner(order, CYC)
            assert winner.task == "t-B" and [r.task for r in ranked] == ["t-B", "t-A"]
            assert [r.key[4] for r in ranked] == [0, 1]
            assert "the exact values decide, better cyc (lower is better): %s against %s for t-A" % (
                accept._exact(5e9 - gb), accept._exact(5e9 - ga)) in reason
            assert "equal within the 1e-6 ranking band" in reason and "slot 1 breaks" not in reason
    # the same in a deficit-pareto contract, with two attempts both still short of the target
    two = contract([metric("cyc", "lower", 1e9), metric("area", "lower")])
    cs = [_judged("t-A", 1, {"cyc": 5e9, "area": 100.0}, {"cyc": 5e9 - 1000, "area": 100.0}, two),
          _judged("t-B", 2, {"cyc": 5e9, "area": 100.0}, {"cyc": 5e9 - 1001, "area": 100.0}, two)]
    assert all(c.decision.accepted for c in cs)
    assert accept.pick_winner(cs, two)[0].task == "t-B"
    # negative: equal medians still fall to the slot, and the exact part is 0 for both
    cs = [_judged("t-A", 1, {"cyc": 5e9}, {"cyc": 5e9 - 12000}, CYC),
          _judged("t-B", 2, {"cyc": 5e9}, {"cyc": 5e9 - 12000}, CYC)]
    winner, ranked, reason = accept.pick_winner(cs, CYC)
    assert winner.task == "t-A" and [r.key[4] for r in ranked] == [0, 0]
    assert "the lower slot 1 breaks the tie" in reason


def test_a_winner_by_a_gap_inside_the_margin_says_so():
    # check-adversarial-r1-rerun m1 (case A12): both beat the champion 100 by more than the margin
    # of 10, and the 1-unit gap between them is inside that margin
    c = contract([metric("perf", "higher", margin=10.0, kind="noisy", repeats=2, min_ok=2)])

    def v(p):
        return {"perf": mv("perf", p, repeats=(p, p))}

    def judged(task, slot, p):
        return cand(task, slot, accept.improve(v(100), v(p), c), v(p))

    cs = [judged("t-a", 1, 111), judged("t-b", 2, 112)]
    assert all(x.decision.accepted for x in cs)
    winner, ranked, reason = accept.pick_winner(cs, c)
    assert winner.task == "t-b"
    assert "(within the margin of 10: the perf gap of 1 is not more than the noise)" in reason
    # negative: a gap past the margin reads as a real difference
    cs = [judged("t-a", 1, 111), judged("t-b", 2, 125)]
    reason = accept.pick_winner(cs, c)[2]
    assert "better score" in reason and "within the margin" not in reason
    # the primary part decides under deficit-pareto: the note follows it there too
    two = contract([metric("perf", "higher", 300.0, margin=10.0, kind="noisy", repeats=2,
                           min_ok=2), metric("lut", "lower", 3000.0)])

    def w(p, l):
        return {"perf": mv("perf", p, repeats=(p, p)), "lut": mv("lut", l)}

    champ = w(320, 2900)                        # every target met: phase pareto, score 0 for all
    cs = [cand("t-a", 1, accept.improve(champ, w(335, 2800), two), w(335, 2800)),
          cand("t-b", 2, accept.improve(champ, w(340, 2800), two), w(340, 2800))]
    assert all(x.decision.accepted for x in cs)
    reason = accept.pick_winner(cs, two)[2]
    assert "better perf" in reason and "within the margin of 10" in reason


def test_a_ranked_key_is_plain_record_data():
    cs = [_judged("t-A", 1, {"cyc": 5e9}, {"cyc": 5e9 - 10000}, CYC),
          _judged("t-B", 2, {"cyc": 5e9}, {"cyc": 5e9 - 12000}, CYC),
          cand("t-C", 3, accept.improve(_as_mv({"cyc": 5e9}), {}, CYC), {})]
    ranked = accept.pick_winner(cs, CYC)[1]
    assert [r.task for r in ranked] == ["t-B", "t-A", "t-C"]
    assert ranked[-1].key[4] == 2                   # a missing median ranks after every value
    for r in ranked:
        determinism.canonical(T.to_dict(r))         # JSON-safe: ints, floats and strings only


def test_a_missing_metric_ranks_last_and_no_accepted_attempt_gives_no_winner():
    champ = {"perf": 200, "lut": 5000}
    missing = cand("t-9", 0, accept.improve(_as_mv(champ), {"perf": mv("perf", 400)}, FESENS),
                   {"perf": mv("perf", 400)})
    assert missing.decision.outcome == "repeats-failed"
    rejected = _judged("t-8", 5, champ, {"perf": 150, "lut": 5000})
    winner, ranked, reason = accept.pick_winner([missing, rejected], FESENS)
    assert winner is None
    assert [r.task for r in ranked] == ["t-8", "t-9"]
    assert ranked[-1].key[1] == 1 and "no score" in ranked[-1].reason
    assert reason == "no attempt accepted: 2 attempts (regression 1, repeats-failed 1)"
    ok = _judged("t-7", 9, champ, {"perf": 250, "lut": 5000})
    winner, ranked, reason = accept.pick_winner([missing, rejected, ok], FESENS)
    assert winner.task == "t-7" and ranked[0].task == "t-7" and ranked[-1].task == "t-9"
    assert reason.endswith("the only accepted attempt")
    assert accept.pick_winner([], FESENS) == (None, [], "no attempt in the round")


def test_repair_candidates_rank_by_slot():
    c = contract([], rule="repair")
    fixed = T.AcceptDecision(accepted=True, outcome="fixed", phase="repair", reason="fixed")
    cs = [cand("t-r2", 2, fixed), cand("t-r1", 1, fixed)]
    winner, ranked, reason = accept.pick_winner(cs, c)
    assert winner.task == "t-r1" and ranked[0].key[1] == 0
    assert "lower slot 1" in reason
    # the same slot twice: the task id makes the order total, and the reason says so
    winner, ranked, reason = accept.pick_winner([cand("t-r9", 1, fixed), cand("t-r1", 1, fixed)], c)
    assert winner.task == "t-r1" and reason.endswith("the task id breaks the tie")


# ------------------------------------------------------------------ the repair rule (FR-012)

def f2p(base, att, weak=(), ok=None):
    tests = tuple(sorted(base))
    good = all(base[t] == "FAIL" and att.get(t) == "PASS" for t in tests) and not weak
    return T.FailToPass(tests=tests, base=dict(base), attempt=dict(att), weak=tuple(weak),
                        ok=good if ok is None else ok)


def p2p(baseline, base_pass, attempt_pass):
    lost = tuple(sorted(set(baseline) - set(attempt_pass)))
    return T.PassToPass(baseline=tuple(baseline), base_pass=tuple(base_pass),
                        attempt_pass=tuple(attempt_pass), lost=lost, ok=not lost)


BASE_SET = ("a", "b", "c")
FROZEN = p2p((), BASE_SET, BASE_SET)


def test_repair_accepts_fail_to_pass_with_every_base_passing_test_kept():
    d = accept.repair(f2p({"r1": "FAIL", "r2": "FAIL"}, {"r1": "PASS", "r2": "PASS"}),
                      p2p(BASE_SET, BASE_SET, BASE_SET + ("d",)), FROZEN)
    assert d.accepted and d.outcome == "fixed" and d.phase == "repair"
    assert T.VERDICT_OF[d.outcome] == "accepted"


def test_repair_refuses_a_lost_pass_to_pass_test():
    d = accept.repair(f2p({"r1": "FAIL"}, {"r1": "PASS"}),
                      p2p(BASE_SET, BASE_SET, ("a", "c")), FROZEN)
    assert not d.accepted and d.outcome == "not-fixed" and "lost 1 of 3" in d.reason
    assert "b" in d.reason


def test_repair_refuses_a_reproducer_that_is_not_fail_to_pass():
    kept = p2p(BASE_SET, BASE_SET, BASE_SET)
    for base, att, weak in (({"r1": "PASS"}, {"r1": "PASS"}, ("r1",)),     # weak: passes on base
                            ({"r1": "FAIL"}, {"r1": "FAIL"}, ()),           # still fails
                            ({"r1": "FAIL"}, {"r1": "SKIP"}, ()),           # skipped
                            ({"r1": "FAIL"}, {}, ()),                        # absent on attempt
                            ({}, {}, ())):                                   # no test
        d = accept.repair(f2p(base, att, weak), kept, FROZEN)
        assert not d.accepted and d.outcome == "not-fixed", (base, att)
    # the recorded flag alone does not make it fixed, and a false flag is honored
    d = accept.repair(f2p({"r1": "FAIL"}, {"r1": "FAIL"}, ok=True), kept, FROZEN)
    assert not d.accepted
    d = accept.repair(f2p({"r1": "FAIL"}, {"r1": "PASS"}, ok=False), kept, FROZEN)
    assert not d.accepted and "recorded it as not met" in d.reason
    # a forged flag on the base side: the reproducer PASSes on the base, weak is left empty and
    # ok is True, so only the base-FAIL recheck refuses it (check-contract-r1-rerun M13)
    d = accept.repair(f2p({"r1": "PASS"}, {"r1": "PASS"}, ok=True), kept, FROZEN)
    assert not d.accepted and "not FAIL on the base: r1 (PASS)" in d.reason


def test_a_test_failing_on_the_base_is_left_out_and_a_shrunk_set_is_refused():
    # "e" fails on the base, so it is not in the frozen set: its failing on the attempt is fine
    d = accept.repair(f2p({"r1": "FAIL"}, {"r1": "PASS"}),
                      p2p(BASE_SET, BASE_SET, BASE_SET), FROZEN)
    assert d.accepted and "all 3 base-passing tests" in d.reason
    # the gate checked a smaller set than the frozen one: refused even though nothing it saw failed
    d = accept.repair(f2p({"r1": "FAIL"}, {"r1": "PASS"}),
                      p2p(("a", "b"), ("a", "b"), ("a", "b")), FROZEN)
    assert not d.accepted and "shrinks below the frozen base set" in d.reason
    # the frozen set is the one judged, not a base pass set measured later beside the attempt
    d = accept.repair(f2p({"r1": "FAIL"}, {"r1": "PASS"}),
                      p2p(BASE_SET, ("a", "b"), ("a", "b")), FROZEN)
    assert not d.accepted and "lost 1 of 3" in d.reason


def test_repair_refuses_an_empty_frozen_base_pass_set():
    # check-adversarial-r2 M3 (R4): min_tests is at least 1, so an empty set guards nothing
    empty = p2p((), (), ())
    d = accept.repair(f2p({"r1": "FAIL"}, {"r1": "PASS"}), p2p((), (), ("a",)), empty)
    assert not d.accepted and d.outcome == "not-fixed"
    assert "the frozen base pass set is empty" in d.reason and "REPAIR-BASELINE-THIN" in d.reason
    # positive: a one-test frozen set that is kept is fixed
    one = p2p((), ("a",), ("a",))
    d = accept.repair(f2p({"r1": "FAIL"}, {"r1": "PASS"}), p2p(("a",), ("a",), ("a",)), one)
    assert d.accepted and "all 1 base-passing tests" in d.reason


def test_repair_honors_the_lost_list_the_pass_to_pass_gate_recorded():
    bad = T.PassToPass(baseline=BASE_SET, base_pass=BASE_SET, attempt_pass=BASE_SET,
                       lost=("c",), ok=False)
    d = accept.repair(f2p({"r1": "FAIL"}, {"r1": "PASS"}), bad, FROZEN)
    assert not d.accepted and "recorded base-passing tests as lost: c" in d.reason
    # a lost test outside the frozen set is not a frozen base-passing test
    extra = T.PassToPass(baseline=BASE_SET + ("z",), base_pass=BASE_SET, attempt_pass=BASE_SET,
                         lost=("z",), ok=False)
    assert accept.repair(f2p({"r1": "FAIL"}, {"r1": "PASS"}), extra, FROZEN).accepted


def test_decisions_are_plain_data_for_the_record():
    for d in (accept.improve(vals(perf=200, lut=5000), vals(perf=290, lut=5500), FESENS),
              accept.improve(_as_mv({"perf": 1, "lut": 1}), {"perf": mv("perf", 2)}, FESENS),
              accept.repair(f2p({"r": "FAIL"}, {"r": "PASS"}), FROZEN, FROZEN)):
        data = T.to_dict(d)
        assert set(data) == {"accepted", "outcome", "phase", "reason", "score", "champion_score",
                             "untested", "repeats_failed", "margin"}
        assert data["outcome"] in T.OUTCOMES
        json.loads(determinism.canonical(data))
        assert data["reason"].isascii()
