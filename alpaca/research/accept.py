"""The pure accept rules: strict improvement with margin, saturating deficit then strict Pareto,
repeats failed and UNTESTED, repair acceptance, and the winner pick.

Design: design/research-engine/architecture.md section 3.7 (t-099); the requirements are FR-007 to
FR-012 of design/research-engine/spec.md, with DD-01, DD-11 and DD-15. Every function here is pure:
no I/O, no clock, no record. The lifecycle's `judge` step (t-100) writes the returned
AcceptDecision as one `research-accept` event, and the round's `rank` step writes pick_winner's
ranked list and reason into `research-round`.

The rules, re-implemented from the study of FeSens (design/research-engine/study-fesens.md section
6), not copied from it:

  * rule `champion` (phase `strict`, one metric, no target): accepted only when the attempt beats
    the start-of-round champion by more than the metric's declared margin (FR-007, FR-010).
  * rule `deficit-pareto` over the contract's N axes (FR-008): each targeted axis scores a
    saturating deficit, 0 at or past its target and (value - target) / |target| below it (the sign
    set by the axis direction); an axis without a target takes no deficit (DD-15). While the
    champion is short of some target (phase `deficit`) an attempt is accepted only when its summed
    score rises over the champion's. Once the champion meets every target (phase `pareto`) an
    attempt is accepted only when it strictly Pareto-dominates the champion over every axis,
    targeted or not (DD-15), and an attempt past any target is rejected as a regression past a met
    target. after_targets only decides whether the runner stops there (DD-01); the rule is the same.
    So "a regression past a met target is rejected" (EC-015) holds once every target is met; while
    a target is still short, a loss past another, met target counts in the summed deficit and is
    not refused on its own (FeSens phase 1, row (310, 3300) to (290, 3000)).
  * the margin (FR-010): a gain on an axis counts only when it is larger than that axis's margin,
    and then only net of the margin (gain - margin, the part that cannot be noise); a gain within
    the margin is scored as no change, and a loss always counts in full. So an accepted attempt
    beats the champion by more than the margin on at least one axis, and in phase deficit the
    summed score must still rise after every gain is cut by its margin: a gain that only just
    clears the noise cannot pay for a real loss on another axis, and noise can never hide a loss.
  * repeats (FR-011): an attempt with fewer successful repeats than a metric's min_ok (or with no
    median) gets no score and is rejected as `repeats-failed`. A verdict that rests on a noisy
    metric measured with fewer than its declared repeats, or on any metric whose repeat spread is
    wider than its margin (DD-11), or on any metric that records more repeats than the contract
    declares (max(1, repeats), what gates.measure runs), is recorded UNTESTED: the rule's verdict
    is kept in the reason, and the decision is rejected with outcome `untested`
    (types.VERDICT_OF). The count, the spread and the median are read from the recorded repeats
    themselves (FR-025 records every repeat): a repeat that is not a finite number is not a success whatever n_ok says, so a value
    that records no repeat at all has 0 successful repeats; the spread is max minus min of the
    finite repeats; the median is rebuilt from them by the metric's `median` rule ("worse" or
    "middle" for an even count), and the rule judges that rebuilt median, not the recorded one.
    A recorded spread that is not a finite number of 0 or more, or that disagrees with the
    repeats, and a recorded median that is not exactly the rebuilt one, or that has no finite
    repeat behind it, make the verdict UNTESTED. A value filed under another metric id than its
    own is not a measurement of that metric (no score).
  * repair (FR-012): accepted ("fixed") only when fail-to-pass holds and no test of the frozen base
    pass set is missing from the attempt's pass set. An empty frozen base pass set is refused: the
    runbook's pass_to_pass.min_tests is at least 1, so an empty set is a thin base measurement.

A value is compared with its target, and one attempt's value with the champion's, exactly: a
relative tolerance would grow with the value (1e-9 of a 5e9 cycle count is 5 cycles) and let a
real loss pass as equal. Only two comparisons allow float noise: a gain against its margin (the
tolerance is 1e-9 of the margin, far below it) and one deficit score against another (unitless
sums near 0; the tolerance only ever rejects). pick_winner orders with determinism.stable_rank,
which bands every float at 1e-6 before it compares, so it feeds it unitless keys: the relative gain
over the champion, and the primary metric over the round's largest absolute median. A tie inside
that band then falls to the exact order of the primary metric's recorded medians (an integer rank,
so stable_rank does not band it) before the slot: two medians that differ always rank apart.
"""

from __future__ import annotations

import math

from alpaca import determinism
from alpaca.research.types import (AcceptDecision, Candidate, Contract, FailToPass, MetricValue,
                                   PassToPass, Ranked)
from alpaca.research.types import VERDICT_OF as _VERDICT_OF

_REL_TOL = 1e-9          # float noise between a gain and its margin, and between two scores
_SCORE_ABS_TOL = 1e-12   # deficit scores are unitless sums near 0: an absolute floor for equality
_KEY_LIMIT = 1e300       # a rank key stays inside this, so determinism.band never overflows


# ------------------------------------------------------------------ small helpers

def _num(x) -> str:
    """A number for a reason text: 12 significant digits, so a difference the rule acted on shows
    (5000000004 against 5000000000, not 5e+09 against 5e+09)."""
    if x is None:
        return "none"
    return "%.12g" % x


def _exact(x) -> str:
    """A value compared exactly with its target: 12 significant digits when they are the value
    itself, else every digit, so 1000000000.0001 against 1000000000 does not read as equal."""
    text = _num(x)
    return text if x is None or float(text) == x else repr(float(x))


def _score_gt(x: float, y: float) -> bool:
    """x is higher than y past float noise (deficit scores)."""
    return x > y and not math.isclose(x, y, rel_tol=_REL_TOL, abs_tol=_SCORE_ABS_TOL)


def _orient(metric, value: float) -> float:
    """The value on a higher-is-better scale."""
    return value if metric.direction == "higher" else -value


def _better(metric, a: float, b: float) -> bool:
    """a is better than b on this axis. Exact: no tolerance can hide a loss of one unit."""
    return _orient(metric, a) > _orient(metric, b)


def _exceeds(gain: float, margin: float) -> bool:
    """A gain larger than the margin (a gain equal to the margin is not). The float-noise band is
    1e-9 of the margin itself, so it is bounded by the margin and 0 when the margin is 0."""
    return gain > margin and not math.isclose(gain, margin, rel_tol=_REL_TOL, abs_tol=0.0)


def _finite(x) -> bool:
    """A real number that is finite. An int too large for a float (10**400) is not: it could not
    be compared or scored, so it counts as no value instead of raising OverflowError."""
    if not isinstance(x, (int, float)) or isinstance(x, bool):
        return False
    try:
        return math.isfinite(x)
    except OverflowError:
        return False


def _metrics(contract: Contract) -> tuple:
    metrics = tuple(contract.metrics or ())
    ids = [m.id for m in metrics]
    if len(set(ids)) != len(ids):
        raise ValueError("contract metrics repeat an id: %s" % ", ".join(ids))
    for m in metrics:
        if m.direction not in ("higher", "lower"):
            raise ValueError("metric %r has direction %r; it must be higher or lower"
                             % (m.id, m.direction))
        if not _finite(m.margin) or m.margin < 0:
            raise ValueError("metric %r has margin %r; it must be a finite number of 0 or more"
                             % (m.id, m.margin))
        if m.median not in ("worse", "middle"):
            raise ValueError("metric %r has median rule %r; it must be worse or middle"
                             % (m.id, m.median))
        if m.kind not in ("deterministic", "noisy"):
            raise ValueError("metric %r has kind %r; it must be deterministic or noisy"
                             % (m.id, m.kind))
    return metrics


def _targets(contract: Contract) -> dict:
    """metric id -> target value, from Contract.targets and each Metric.target, which must agree."""
    by_id = {m.id: m for m in _metrics(contract)}
    out = {}
    for m in by_id.values():
        if m.target is not None:
            out[m.id] = float(m.target)
    for t in contract.targets or ():
        if t.metric not in by_id:
            raise ValueError("target names metric %r, which the contract does not declare"
                             % t.metric)
        value = float(t.value)
        if t.metric in out and out[t.metric] != value:
            raise ValueError("metric %r has target %s in metrics and %s in targets"
                             % (t.metric, _num(out[t.metric]), _num(value)))
        out[t.metric] = value
    for mid, value in out.items():
        if not math.isfinite(value):
            raise ValueError("metric %r has a target that is not a finite number" % mid)
    return out


def _value(values: dict, metric_id: str) -> float:
    v = values.get(metric_id) if isinstance(values, dict) else None
    if not _finite(v):
        raise ValueError("no finite value for metric %r" % metric_id)
    return float(v)


def _axis_deficit(metric, value: float, target: float) -> float:
    """0 at or past the target; below it the normalized shortfall, a negative number. The value is
    compared with the literal target: one unit short is short, whatever the target's size."""
    if _orient(metric, value) >= _orient(metric, target):
        return 0.0
    scale = abs(target) if target != 0 else 1.0
    return (_orient(metric, value) - _orient(metric, target)) / scale


# ------------------------------------------------------------------ the pure rules

def deficit_score(values: dict[str, float], contract: Contract) -> float:
    """The summed saturating deficit over the targeted axes (FR-008): each axis gives 0 at or past
    its target and (value - target) / |target| short of it, oriented by its direction (a target of
    0 divides by 1). An axis without a target adds nothing (DD-15). The result is 0 or negative;
    0 exactly when targets_met. A targeted axis with no finite value raises ValueError."""
    by_id = {m.id: m for m in _metrics(contract)}
    targets = _targets(contract)
    total = 0.0
    for m in _metrics(contract):
        if m.id in targets:
            total += _axis_deficit(by_id[m.id], _value(values, m.id), targets[m.id])
    return total


def targets_met(values: dict[str, float], contract: Contract) -> bool:
    """True when the contract has at least one target and every targeted axis is at or past its
    target, compared exactly. A contract with no target has nothing to meet and gives False, so a
    caller that stops a run on `targets_met` never stops a no-target run. A missing targeted value
    gives False."""
    targets = _targets(contract)
    if not targets:
        return False
    by_id = {m.id: m for m in _metrics(contract)}
    for mid, target in targets.items():
        v = values.get(mid) if isinstance(values, dict) else None
        if not _finite(v) or _axis_deficit(by_id[mid], float(v), target) != 0.0:
            return False
    return True


def pareto_dominates(a: dict[str, float], b: dict[str, float], contract: Contract) -> bool:
    """a strictly Pareto-dominates b over every axis of the contract, targeted or not (DD-15): no
    axis worse and at least one axis better, each by its direction, compared exactly. A missing or
    non-finite value on any axis raises ValueError."""
    metrics = _metrics(contract)
    if not metrics:
        raise ValueError("the contract declares no metric to compare")
    strictly = False
    for m in metrics:
        va, vb = _value(a, m.id), _value(b, m.id)
        if _better(m, vb, va):
            return False
        if _better(m, va, vb):
            strictly = True
    return strictly


def _ok_repeats(mv: MetricValue) -> list:
    """The finite recorded repeats, in order. A value that records no repeat has none: FR-025
    records every repeat, so an empty list is a measurement with no success behind it."""
    return [float(x) for x in tuple(mv.repeats or ()) if _finite(x)]


def _n_ok(mv: MetricValue) -> int:
    """The successful repeats: n_ok, but never more than the finite repeats recorded beside it."""
    n = int(mv.n_ok) if _finite(mv.n_ok) else 0
    return min(n, len(_ok_repeats(mv)))


def _median_of(metric, ok: list) -> float:
    """The median of the finite repeats by the metric's rule: the middle one for an odd count; for
    an even count "worse" takes the worse of the two middle values for the metric's direction and
    "middle" their mean (architecture.md section 3.3, gates.measure; architecture.md:955-960)."""
    s = sorted(ok)
    n = len(s)
    if n % 2:
        return s[n // 2]
    lo, hi = s[n // 2 - 1], s[n // 2]
    if metric.median == "middle":
        return (lo + hi) / 2.0
    return lo if metric.direction == "higher" else hi


def _judged(metric, mv: MetricValue) -> float:
    """The median the rule judges: the one rebuilt from the finite recorded repeats, or the
    recorded median when no repeat is a finite number (that value is then UNTESTED or has no
    score, see _median_notes and _repeats_failed). The caller has checked mv.median is finite."""
    ok = _ok_repeats(mv)
    return _median_of(metric, ok) if ok else float(mv.median)


def _median_notes(metric, mv: MetricValue) -> list:
    """Why the recorded median cannot be trusted: it has no finite repeat behind it, or it is not
    exactly the median rebuilt from the repeats. Exact for both rules: measure computes the same
    pick, or the same (low + high) / 2.0 (gates._median on branch op009/t-093), so an honest record
    matches bit for bit, and any tolerance relative to the value would grow with it (1e-12 of 1e9
    is 1e-3). Both numbers print in full, so the reason shows the difference the rule acted on."""
    if not _finite(mv.median):
        return []                                   # no median at all: repeats failed, no score
    ok = _ok_repeats(mv)
    given = float(mv.median)
    if not ok:
        return ["the recorded median %s has no finite repeat behind it" % _num(given)]
    rebuilt = _median_of(metric, ok)
    if given == rebuilt:
        return []
    return ["the recorded median %r disagrees with the repeats' median %r (off by %r); the rule "
            "judged the repeats' median" % (given, rebuilt, given - rebuilt)]


def _spread_notes(mv: MetricValue):
    """(the spread to judge, why the recorded spread cannot be trusted). DD-11: the spread is max
    minus min of the successful repeats, so it is computed from them whenever there are two."""
    ok = _ok_repeats(mv)
    computed = (max(ok) - min(ok)) if len(ok) >= 2 else None
    given = mv.spread
    why = []
    if given is not None:
        if not _finite(given) or given < 0:
            why.append("the recorded spread %r is not a finite number of 0 or more" % (given,))
        elif computed is not None and not math.isclose(float(given), computed,
                                                       rel_tol=_REL_TOL, abs_tol=0.0):
            why.append("the recorded spread %s disagrees with the repeats' spread %s"
                       % (_num(given), _num(computed)))
    if computed is not None:
        return computed, why
    if given is not None and _finite(given) and given >= 0:
        return float(given), why
    return None, why


def _untested_notes(side: str, values: dict, metrics: tuple) -> list:
    """Why a verdict resting on these measurements is UNTESTED (FR-011, DD-11)."""
    notes = []
    for m in metrics:
        mv = values.get(m.id)
        if mv is None:
            continue
        why = []
        if mv.untested:
            why.append("the gate chain marked it untested")
        n = _n_ok(mv)
        if m.kind == "noisy" and n < m.repeats:
            why.append("%d of %d declared repeats succeeded" % (n, m.repeats))
        recorded = len(tuple(mv.repeats or ()))
        declared = max(1, int(m.repeats))
        if recorded > declared:
            # measure runs exactly max(1, repeats) repeats (gates.measure, t-093); extra ones would
            # move the rebuilt median the rule judges, so a record that has them is not trusted
            why.append("%d repeats recorded, the contract declares %d" % (recorded, declared))
        why.extend(_median_notes(m, mv))
        spread, spread_why = _spread_notes(mv)
        why.extend(spread_why)
        if spread is not None and _exceeds(spread, float(m.margin)):
            why.append("repeat spread %s is wider than the margin %s"
                       % (_num(spread), _num(m.margin)))
        if why:
            notes.append("%s %s: %s" % (side, m.id, "; ".join(why)))
    return notes


def _repeats_failed(values: dict, metrics: tuple) -> list:
    """Metrics with no score: absent, filed under another metric id, no finite median, or fewer
    successful repeats than min_ok (counted from the finite recorded repeats, see _n_ok)."""
    failed = []
    for m in metrics:
        mv = values.get(m.id)
        if mv is None:
            failed.append("%s was not measured" % m.id)
            continue
        if mv.metric != m.id:
            failed.append("%s was not measured: the value filed under it is for metric %r"
                          % (m.id, mv.metric))
            continue
        n = _n_ok(mv)
        if not _finite(mv.median):
            failed.append("%s has no median (%d successful repeats)" % (m.id, n))
        elif n < m.min_ok:
            failed.append("%s has %d successful repeats, fewer than the minimum %d"
                          % (m.id, n, m.min_ok))
    return failed


def _credited(champion: dict, attempt: dict, metrics: tuple) -> dict:
    """The attempt's values as the rule scores them (FR-010): a gain within the axis's margin is
    no gain (the champion's value); a gain past it is credited net of the margin, the least of it
    that is not noise (champion + (gain - margin), oriented by the direction); every loss counts
    as measured. So a gain that only just clears the noise cannot pay for a real loss on another
    axis. A margin of 0 keeps the measured value exactly."""
    out = {}
    for m in metrics:
        a, c = attempt[m.id], champion[m.id]
        margin = float(m.margin)
        gain = _orient(m, a) - _orient(m, c)
        if gain <= 0 or margin == 0.0:
            out[m.id] = a
        elif not _exceeds(gain, margin):
            out[m.id] = c
        else:
            net = gain - margin
            out[m.id] = c + net if m.direction == "higher" else c - net
    return out


def _check_rule(contract: Contract, metrics: tuple, targets: dict) -> str:
    rule = contract.accept_rule
    if rule == "champion":
        if len(metrics) != 1 or targets:
            raise ValueError("accept rule champion needs exactly one metric and no target; "
                             "this contract has %d metrics and %d targets"
                             % (len(metrics), len(targets)))
    elif rule == "deficit-pareto":
        if not metrics or not targets:
            raise ValueError("accept rule deficit-pareto needs at least one target")
    elif rule == "repair":
        raise ValueError("accept rule repair judges with accept.repair, not accept.improve")
    else:
        raise ValueError("unknown accept rule %r" % (rule,))
    return rule


def improve(champion: dict[str, MetricValue], attempt: dict[str, MetricValue],
            contract: Contract) -> AcceptDecision:
    """Judge an improve attempt against the start-of-round champion (FR-007 to FR-011).

    champion and attempt map metric id to the full MetricValue, recorded repeats included (for
    the champion: the round winner's measured values as recorded). A champion value that keeps
    only its median has no repeat behind it, so every verdict against it is UNTESTED. An empty
    champion judges the round 0 baseline (phase `baseline`): it is accepted with outcome
    `baseline` unless its repeats failed or it is UNTESTED. The order of the checks: repeats
    failed (no score, FR-011); the rule's verdict (phase `strict`, `deficit` or `pareto`, see the
    module docstring); then UNTESTED, which turns the verdict into a rejection with outcome
    `untested` and keeps the rule's verdict in the reason. Each side is judged on the median
    rebuilt from its recorded repeats (_judged), so a recorded median that disagrees with them
    makes the verdict UNTESTED and can never move it.
    `score` and `champion_score` are the numbers the rule compared: the metric's medians in phase
    strict, deficit scores otherwise (the attempt's from its credited values, each gain past its
    margin cut by that margin). `margin` is in the metric's raw units: the metric's margin in
    phase strict, and the widest raw margin of the axes otherwise. In phases deficit and pareto it
    cannot be compared with the unitless `score`: the margins are already taken out of `score`
    there, so for SC-007 an accepted deficit verdict is `score > champion_score` itself, and
    `score - champion_score` is never measured against `margin`.

    Raises ValueError for a contract whose rule is not an improve rule, or that does not fit its
    rule, and for a champion without a finite median on every metric."""
    metrics = _metrics(contract)
    targets = _targets(contract)
    rule = _check_rule(contract, metrics, targets)
    champion = champion or {}
    attempt = attempt or {}
    baseline = not champion
    by_id = {m.id: m for m in metrics}

    champ_vals = {}
    if not baseline:
        for m in metrics:
            mv = champion.get(m.id)
            if mv is None or not _finite(mv.median):
                raise ValueError("the champion has no finite median for metric %r" % m.id)
            if mv.metric != m.id:
                raise ValueError("the champion's value filed under metric %r is for metric %r"
                                 % (m.id, mv.metric))
            champ_vals[m.id] = _judged(m, mv)

    if rule == "champion":
        margin = float(metrics[0].margin)
    else:
        margin = max(float(m.margin) for m in metrics)

    if baseline:
        phase = "baseline"
    elif rule == "champion":
        phase = "strict"
    else:
        phase = "pareto" if targets_met(champ_vals, contract) else "deficit"

    if rule == "champion":
        champion_score = champ_vals[metrics[0].id] if not baseline else None
    else:
        champion_score = deficit_score(champ_vals, contract) if not baseline else None

    untested = _untested_notes("attempt", attempt, metrics)
    if not baseline:
        untested += _untested_notes("champion", champion, metrics)

    failed = _repeats_failed(attempt, metrics)
    if failed:
        reason = "repeats failed, no score: " + "; ".join(failed)
        if untested:
            reason += ". UNTESTED: " + "; ".join(untested)
        return AcceptDecision(accepted=False, outcome="repeats-failed", phase=phase, reason=reason,
                              score=None, champion_score=champion_score,
                              untested=bool(untested), repeats_failed=True, margin=margin)

    att_vals = {m.id: _judged(m, attempt[m.id]) for m in metrics}

    if baseline:
        if rule == "champion":
            score = att_vals[metrics[0].id]
            what = "%s %s" % (metrics[0].id, _num(score))
        else:
            score = deficit_score(att_vals, contract)
            what = "deficit score %s%s" % (
                _num(score), ", every target met" if targets_met(att_vals, contract) else "")
        accepted, outcome = True, "baseline"
        reason = "baseline measured: " + what
    elif phase == "strict":
        m = metrics[0]
        a, c = att_vals[m.id], champ_vals[m.id]
        gain = _orient(m, a) - _orient(m, c)
        score = a
        accepted = _exceeds(gain, float(m.margin))
        outcome = "improvement" if accepted else "regression"
        if accepted:
            reason = ("%s %s beats the champion's %s by %s, more than the margin %s (%s is better)"
                      % (m.id, _num(a), _num(c), _num(gain), _num(m.margin), m.direction))
        elif gain > 0:
            reason = ("%s %s beats the champion's %s by only %s, not more than the margin %s"
                      % (m.id, _num(a), _num(c), _num(gain), _num(m.margin)))
        else:
            reason = ("%s %s does not beat the champion's %s (%s is better)"
                      % (m.id, _num(a), _num(c), m.direction))
    elif phase == "deficit":
        credited = _credited(champ_vals, att_vals, metrics)
        score = deficit_score(credited, contract)
        accepted = _score_gt(score, champion_score)
        outcome = "improvement" if accepted else "regression"
        gains = {m.id: _orient(m, att_vals[m.id]) - _orient(m, champ_vals[m.id]) for m in metrics}
        # named by the gain against the margin, not by the credited value: a gain past the margin
        # whose net part is below the float spacing of the champion value is credited as the
        # champion's value, yet it cleared the margin
        within = [m.id for m in metrics if gains[m.id] > 0 and float(m.margin) != 0.0
                  and not _exceeds(gains[m.id], float(m.margin))]
        net = [m for m in metrics if gains[m.id] > 0 and float(m.margin) != 0.0
               and _exceeds(gains[m.id], float(m.margin))]
        parts = []
        if within:
            parts.append("gains within the margin on %s count as no change" % ", ".join(within))
        if net:
            parts.append("gains past the margin count net of it: %s" % ", ".join(
                "%s %s -> %s credited as %s (margin %s)" % (
                    m.id, _num(champ_vals[m.id]), _num(att_vals[m.id]), _num(credited[m.id]),
                    _num(m.margin)) for m in net))
        note = ""
        if parts:
            note = " (%s; measured score %s)" % ("; ".join(parts),
                                                  _num(deficit_score(att_vals, contract)))
        reason = ("deficit score %s %s the champion's %s%s"
                  % (_num(score), "rises over" if accepted else "does not rise over",
                     _num(champion_score), note))
        worse = [m for m in metrics if _better(m, champ_vals[m.id], att_vals[m.id])]
        short = [mid for mid, t in targets.items()
                 if _axis_deficit(by_id[mid], champ_vals[mid], t) != 0.0]

        def fell_past(m):
            t = targets.get(m.id)
            return (t is not None and _axis_deficit(m, champ_vals[m.id], t) == 0.0
                    and _axis_deficit(m, att_vals[m.id], t) != 0.0)

        def worse_note(m):
            if fell_past(m):
                return ("%s (%s -> %s falls past its met target %s, charged in the summed deficit "
                        "while %s %s short)" % (
                            m.id, _exact(champ_vals[m.id]), _exact(att_vals[m.id]),
                            _exact(targets[m.id]), ", ".join(short),
                            "is" if len(short) == 1 else "are"))
            return "%s (%s -> %s%s)" % (m.id, _num(champ_vals[m.id]), _num(att_vals[m.id]),
                                        "" if m.id in targets
                                        else ", untargeted, no deficit under DD-15")

        if worse:
            reason += "; worse on " + ", ".join(worse_note(m) for m in worse)
    else:  # pareto: the champion meets every target
        score = deficit_score(att_vals, contract)
        past = [mid for mid, t in targets.items()
                if _axis_deficit(by_id[mid], att_vals[mid], t) != 0.0]
        if past:
            accepted, outcome = False, "regression"
            reason = "regresses past a met target: " + "; ".join(
                "%s %s against target %s (%s is better)"
                % (mid, _exact(att_vals[mid]), _exact(targets[mid]), by_id[mid].direction)
                for mid in past)
        else:
            credited = _credited(champ_vals, att_vals, metrics)
            accepted = pareto_dominates(credited, champ_vals, contract)
            outcome = "improvement" if accepted else "regression"
            worse = [m.id for m in metrics if _better(m, champ_vals[m.id], att_vals[m.id])]
            better = [m.id for m in metrics if _better(m, credited[m.id], champ_vals[m.id])]
            if accepted:
                reason = ("every target met; strictly Pareto-dominates the champion: better past "
                          "the margin on %s, no axis worse" % ", ".join(better))
            elif worse:
                reason = ("every target met; does not dominate the champion: worse on %s"
                          % ", ".join(worse))
            else:
                reason = ("every target met; does not dominate the champion: no axis better by "
                          "more than its margin")

    if untested:
        return AcceptDecision(
            accepted=False, outcome="untested", phase=phase,
            reason="UNTESTED: %s. The rule alone would have %s (%s): %s" % (
                "; ".join(untested), "accepted" if accepted else "rejected", outcome, reason),
            score=score, champion_score=champion_score, untested=True, repeats_failed=False,
            margin=margin)
    return AcceptDecision(accepted=accepted, outcome=outcome, phase=phase, reason=reason,
                          score=score, champion_score=champion_score, untested=False,
                          repeats_failed=False, margin=margin)


def repair(f2p: FailToPass, p2p: PassToPass, baseline_p2p: PassToPass) -> AcceptDecision:
    """Accepted ("fixed") when f2p.ok (every reproducer test FAIL on the base and PASS on the
    attempt, none weak) and no test of baseline_p2p.base_pass is missing from p2p.attempt_pass
    or named in p2p.lost. baseline_p2p is the repair task's base measurement: pass_to_pass with
    the base tree on both sides and baseline=(), recorded by the repair plan's base-measure step
    as one `research-baseline` event (types.to_dict(PassToPass)) before the first fix round, so its
    base_pass is the base pass set; the fix attempt's `gates` step passes that base_pass to
    run_chain and its `judge` step reads the same field (section 7.4).

    The fail-to-pass condition is checked again from f2p's own tests, base and attempt results,
    and both the recorded flag and the check must hold. A test that fails on the base is not in
    the frozen base pass set, so it is left out of pass-to-pass (EC-063). The set the gate checked
    (p2p.baseline) must hold every test of the frozen base pass set: a smaller set is refused as a
    pass-to-pass set that shrinks below the frozen baseline (EC-063)."""
    problems = []
    tests = tuple(f2p.tests or ())
    if not tests:
        problems.append("the reproducer names no test")
    if f2p.weak:
        problems.append("weak reproducer tests (not FAIL on the base): %s"
                        % ", ".join(sorted(f2p.weak)))
    base = f2p.base or {}
    att = f2p.attempt or {}
    not_failing = sorted(t for t in tests if base.get(t) != "FAIL")
    not_passing = sorted(t for t in tests if att.get(t) != "PASS")
    if not_failing:
        problems.append("not FAIL on the base: " + ", ".join(
            "%s (%s)" % (t, base.get(t, "absent")) for t in not_failing))
    if not_passing:
        problems.append("not PASS on the attempt: " + ", ".join(
            "%s (%s)" % (t, att.get(t, "absent")) for t in not_passing))
    if not f2p.ok and not problems:
        problems.append("the fail-to-pass gate recorded it as not met")

    frozen = set(baseline_p2p.base_pass or ())
    if not frozen:
        problems.append("the frozen base pass set is empty: pass_to_pass.min_tests is at least 1 "
                        "(runbook-format-3.md), so the base measurement is thin "
                        "(REPAIR-BASELINE-THIN) and nothing guards against a lost test")
    shrunk = sorted(frozen - set(p2p.baseline or ()))
    if shrunk:
        problems.append("the pass-to-pass set the gate checked shrinks below the frozen base set, "
                        "missing %d: %s" % (len(shrunk), ", ".join(shrunk)))
    lost = sorted(frozen - set(p2p.attempt_pass or ()))
    if lost:
        problems.append("pass-to-pass lost %d of %d base-passing tests: %s"
                        % (len(lost), len(frozen), ", ".join(lost)))
    gate_lost = sorted((set(p2p.lost or ()) & frozen) - set(lost))
    if gate_lost:
        problems.append("the pass-to-pass gate recorded base-passing tests as lost: %s"
                        % ", ".join(gate_lost))
    note = ""
    if p2p.base_pass is not None and set(p2p.base_pass) != frozen:
        note = ("; the base pass set measured beside this attempt differs from the frozen one, "
                "which is the one judged")

    if problems:
        return AcceptDecision(accepted=False, outcome="not-fixed", phase="repair",
                              reason="not fixed: " + "; ".join(problems) + note)
    return AcceptDecision(
        accepted=True, outcome="fixed", phase="repair",
        reason=("fixed: %d reproducer tests FAIL on the base and PASS on the attempt; all %d "
                "base-passing tests still pass%s" % (len(tests), len(frozen), note)))


# ------------------------------------------------------------------ the winner pick

_KEY_NAMES = ("verdict", "metric missing", "score", "primary metric", "primary metric exact",
              "slot", "task")


def _clamp(x: float) -> float:
    return max(-_KEY_LIMIT, min(_KEY_LIMIT, x))


def _reference(cands: list, metrics: tuple) -> float:
    """The round's scale for the primary metric: its largest absolute median among the
    candidates (1 when there is none or it is 0), so the primary key is unitless."""
    if not metrics:
        return 1.0
    mid = metrics[0].id
    ref = 0.0
    for c in cands:
        pv = (c.metrics or {}).get(mid)
        if pv is not None and _finite(pv.median):
            ref = max(ref, abs(float(pv.median)))
    return ref if ref > 0 else 1.0


def _exact_ranks(cands: list, metrics: tuple) -> dict:
    """id(candidate) -> the dense rank of its primary median, exactly, best first (0), a missing
    one last. An integer, so stable_rank compares it as it is: a recorded median is read from the
    record, not computed, so its exact value is the same on every machine."""
    if not metrics:
        return {id(c): 0 for c in cands}
    m = metrics[0]

    def oriented(c):
        pv = (c.metrics or {}).get(m.id)
        return _orient(m, float(pv.median)) if pv is not None and _finite(pv.median) else None

    values = sorted({v for v in map(oriented, cands) if v is not None}, reverse=True)
    pos = {v: i for i, v in enumerate(values)}
    return {id(c): (pos[oriented(c)] if oriented(c) is not None else len(values)) for c in cands}


def _counts_accepted(d: AcceptDecision) -> bool:
    """The decision is an accept the round may merge: accepted is set, its outcome is an accepted
    verdict (types.VERDICT_OF), and it is neither UNTESTED nor repeats-failed. improve and repair
    never return any other combination, so a decision that disagrees with itself is corrupt and
    ranks as rejected."""
    return (d.accepted is True and _VERDICT_OF.get(d.outcome) == "accepted"
            and not d.untested and not d.repeats_failed)


def _rank_key(c: Candidate, metrics: tuple, rule: str, ref: float, exact: int) -> tuple:
    """(rejected, metric missing, score, primary metric, primary metric exact, slot, task), smaller
    ranks first: an accepted attempt before a rejected one, a missing metric or score last, then
    the higher score, the better primary metric (the contract's first metric), the lower slot, the
    task id.

    The float parts are unitless, because stable_rank bands them at 1e-6: under rule champion the
    score part is the relative gain over the start-of-round champion, (a - c) / |c| oriented by the
    metric's direction (the round's reference when c is 0 or absent); under deficit-pareto it is
    the deficit score, unitless already; the primary part is the median over `ref`. `exact` is the
    primary median's exact rank among the round's candidates (_exact_ranks), so two medians inside
    one band (5e9 - 10000 against 5e9 - 12000 cycles) still rank by their true order before the
    slot decides."""
    d = c.decision
    vals = c.metrics or {}
    missing = any(vals.get(m.id) is None or not _finite(vals[m.id].median) for m in metrics)
    if d.phase != "repair" and not _finite(d.score):
        missing = True
    primary = metrics[0] if metrics else None
    if not _finite(d.score):
        score_key = 0.0
    elif rule == "champion" and primary is not None:
        champ = float(d.champion_score) if _finite(d.champion_score) else 0.0
        scale = abs(champ) if champ != 0 else ref
        score_key = -_clamp((_orient(primary, float(d.score)) - _orient(primary, champ)) / scale)
    else:
        score_key = -_clamp(float(d.score))               # a deficit score: higher is better
    pv = vals.get(primary.id) if primary else None
    primary_key = -_clamp(_orient(primary, float(pv.median)) / ref) \
        if (pv is not None and _finite(pv.median)) else 0.0
    return (0 if _counts_accepted(d) else 1, 1 if missing else 0, score_key, primary_key,
            int(exact), int(c.slot), str(c.task))


def _banded(key: tuple) -> tuple:
    return tuple(determinism.band(p) if isinstance(p, float) else p for p in key)


def _describe(c: Candidate, metrics: tuple) -> str:
    d = c.decision
    parts = ["%s (%s, phase %s)" % ("accepted" if _counts_accepted(d) else "rejected", d.outcome,
                                    d.phase)]
    if _finite(d.score):
        parts.append("score %s" % _num(d.score))
    else:
        parts.append("no score")
    if metrics:
        pv = (c.metrics or {}).get(metrics[0].id)
        parts.append("%s %s" % (metrics[0].id, _num(pv.median) if pv is not None else "missing"))
    parts.append("slot %d" % c.slot)
    return "; ".join(parts)


def pick_winner(candidates: list[Candidate], contract: Contract) -> tuple[Candidate | None, list[Ranked], str]:
    """Pure: every candidate given is ranked with determinism.stable_rank, accepted ones first,
    and the winner is the first accepted one; returns the winner, the ranked list and the reason
    text. A candidate counts as accepted only when its decision agrees with itself: accepted set,
    an outcome whose verdict is accepted, and neither untested nor repeats_failed
    (_counts_accepted); any other candidate ranks as rejected. Over the accepted candidates the
    order is the design's (metric missing, score, primary metric, slot) (architecture.md section
    3.7).

    Each Ranked.key has seven parts, (rejected, metric missing, score, primary metric, primary
    metric exact, slot, task): the design's four with the accepted flag first, the exact primary
    rank after the banded primary part, and the task id last. The score and primary parts are
    unitless (see _rank_key), so two values that tie there are within 1e-6 of each other relative
    to the champion or to the round's largest median; such a band tie falls to the exact order of
    the primary medians, and the reason says when a tie is a band and not an exact equality.

    Every candidate of the round is ranked, so the record keeps the whole order: accepted
    candidates first, then the rejected ones; within each, a missing metric (or no score) ranks
    last, then the higher score, the better primary metric (the contract's first metric, by its
    direction, banded and then exact), the lower slot, and the task id so the order is total
    (FR-009, EC-025, EC-056).
    The winner is the first ranked candidate when it is accepted, else None. Each Ranked carries
    its key and a one-line reason; the returned reason says why the winner beat the runner-up (the
    first key part that differs) or why no attempt won."""
    metrics = _metrics(contract)
    cands = list(candidates or [])
    if not cands:
        return None, [], "no attempt in the round"
    ref = _reference(cands, metrics)
    exact = _exact_ranks(cands, metrics)
    keys = {id(c): _rank_key(c, metrics, contract.accept_rule, ref, exact[id(c)]) for c in cands}
    ordered = determinism.stable_rank(cands, key=lambda c: keys[id(c)])
    ranked = [Ranked(task=c.task, rank=i + 1, key=keys[id(c)], reason=_describe(c, metrics))
              for i, c in enumerate(ordered)]
    accepted = [c for c in ordered if _counts_accepted(c.decision)]
    if not accepted:
        counts = {}
        for c in ordered:
            counts[c.decision.outcome] = counts.get(c.decision.outcome, 0) + 1
        return None, ranked, "no attempt accepted: %d attempts (%s)" % (
            len(ordered), ", ".join("%s %d" % (k, counts[k]) for k in sorted(counts)))
    winner = accepted[0]
    head = "%s (slot %d) wins, %d of %d attempts accepted" % (
        winner.task, winner.slot, len(accepted), len(ordered))
    if len(accepted) == 1:
        return winner, ranked, head + ": the only accepted attempt"
    runner = accepted[1]
    wk, rk = _banded(keys[id(winner)]), _banded(keys[id(runner)])
    i = next(i for i in range(len(wk)) if wk[i] != rk[i])
    name = _KEY_NAMES[i]
    band = "equal within the 1e-6 ranking band"

    def tie(x, y):
        return "equal" if x == y else "%s (%s against %s)" % (band, _num(x), _num(y))

    def primary_of(c):
        pv = (c.metrics or {}).get(metrics[0].id) if metrics else None
        return float(pv.median) if pv is not None and _finite(pv.median) else None

    if name == "metric missing":
        why = "%s is missing a metric or its score" % runner.task
    elif name == "score":
        if contract.accept_rule == "champion" and metrics:
            m = metrics[0]
            why = "better score, %s %s against %s for %s (%s is better)" % (
                m.id, _num(winner.decision.score), _num(runner.decision.score), runner.task,
                m.direction)
        else:
            why = "higher score %s against %s for %s" % (
                _num(winner.decision.score), _num(runner.decision.score), runner.task)
    elif name == "primary metric":
        m = metrics[0]
        why = "score %s; better %s (%s is better): %s against %s for %s" % (
            tie(winner.decision.score, runner.decision.score), m.id, m.direction,
            _num(primary_of(winner)), _num(primary_of(runner)), runner.task)
    elif name == "primary metric exact":
        m = metrics[0]
        why = ("score %s and %s %s; the exact values decide, better %s (%s is better): %s "
               "against %s for %s" % (
                   tie(winner.decision.score, runner.decision.score), m.id,
                   tie(primary_of(winner), primary_of(runner)), m.id, m.direction,
                   _exact(primary_of(winner)), _exact(primary_of(runner)), runner.task))
    else:
        if metrics:
            what = "score %s and %s %s" % (
                tie(winner.decision.score, runner.decision.score), metrics[0].id,
                tie(primary_of(winner), primary_of(runner)))
        else:
            what = "equal score"
        if name == "slot":
            why = "%s; the lower slot %d breaks the tie with %s (slot %d)" % (
                what, winner.slot, runner.task, runner.slot)
        else:
            why = "%s, the same slot %d as %s; the task id breaks the tie" % (
                what, winner.slot, runner.task)
    if metrics and (name in ("primary metric", "primary metric exact")
                    or (name == "score" and contract.accept_rule == "champion")):
        # FR-009 allows this order, but a gap no larger than the declared noise is not a measured
        # difference, and the record must not read as one
        pw, pr = primary_of(winner), primary_of(runner)
        m = metrics[0]
        if pw is not None and pr is not None and pw != pr and abs(pw - pr) <= float(m.margin):
            why += " (within the margin of %s: the %s gap of %s is not more than the noise)" % (
                _num(m.margin), m.id, _num(abs(pw - pr)))
    return winner, ranked, head + ": " + why
