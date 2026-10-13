"""Shield: model tiers, the escalation ladder and the verifier rules for every formation.

The policy lives in one file, `formations/shield.yaml`; this module reads it and answers the three
questions an orchestrator asks before and during a multi-agent run:

  * `model_of(m, role_id)` -- which model does this role run on? A role's tier is its own `tier:`
    in the formation manifest, else the `tier:` of its archetype brief (`agents/<archetype>.md`).
    The tier maps to a model per runtime. A role with no tier, a tier the policy does not know, or
    a runtime with no map is refused (`ShieldError`): nothing silently inherits the session model.
  * `next_step(round_no, passed, cause)` -- a round of checks just finished; what now? The ladder
    caps rounds at `escalation.round_cap` (5), maps an investigator's cause to an action, and
    stops at the cap with a block for the owner.
  * `promote(tier)` -- one tier up, never past the ceiling. Promotion needs evidence (the
    investigator's `capability` cause); lowering a tier needs none.
  * `think_plan(size)` -- the think-stage steps that run before round 1, each with its model
    (t-221). `class_check_due(...)` and `class_action(...)` -- when the rounds stop converging,
    whether to stop patching, and what a class reviewer's answer leads to.

HONEST LIMIT: this module decides; it does not launch agents. The orchestrator (main session) passes
the resolved model to its runtime's spawn call and records it with `dispatch.spawn(..., model=)`.
"""
from __future__ import annotations

import os

import yaml

from alpaca import paths

#: the policy file, under the project root.
POLICY_PATH = os.path.join("formations", "shield.yaml")

#: the main chat itself: it keeps the operator's model and is never spawned.
SESSION = "session"

#: every action the ladder can return.
CONFIRM = "confirm"                      # all lenses passed: one blind high confirmer, then close
CONTINUE = "continue"                    # next round, plus one investigator on this failure
STOP_AT_CAP = "block-and-ask-owner"      # round_cap reached without a pass
REDESIGN = "redesign"                    # back through the think stage (size full), rounds kept
CLASS_CHECK = "class-check"              # stop patching; one class reviewer reads every finding

#: think-stage sizes; `off` must be justified in the task's proof.
THINK_SIZES = ("full", "lite", "off")

#: the pseudo-tier of design steps: it resolves through the policy's `design.tier`.
DESIGN_TIER = "design"

#: conditions `think.full_when` may name. A redesign always runs the full stage (REDESIGN_SIZE).
FULL_WHEN = ("adversarial-lens", "follow-up")
REDESIGN_SIZE = "full"

#: every action a cause may map to; the round loop has a branch for each and nothing else.
ACTIONS = ("continue", "judge-dispute", "cancel-and-ask-owner", "promote-fixer", REDESIGN)

#: the actions a round loop copied before t-221 knows; `causes` in the ladder JSON maps any newer
#: action onto `cancel-and-ask-owner` so such a copy stops and asks instead of patching on.
LEGACY_ACTIONS = ("continue", "judge-dispute", "cancel-and-ask-owner", "promote-fixer")

#: the think steps in the order they run; the steps of one group run in parallel.
THINK_GROUPS = (("trace", "history", "probe"), ("cause",), ("premortem", "approaches"), ("pick",))
THINK_STEPS = tuple(s for g in THINK_GROUPS for s in g)
#: how many agents each step spawns (approaches: index 0 patches in place, 1 removes the class).
STEP_COUNTS = {"approaches": 2}
#: the only conditional step; every other step always runs.
STEP_WHEN = {"probe": "task-names-probe"}

#: the causes an investigator may report, and the fields of its report (round-loop.js SHIELD_REPORT).
CAUSES = ("builder-error", "checker-false-positive", "contract-unclear", "wrong-direction",
          "capability")
REPORT_FIELDS = ("cause", "how", "direction", "alternative", "disputed", "file")
REPORT_REQUIRED = ("cause", "how", "direction", "file")   # SHIELD_REPORT.required in the loop

#: tier names with a meaning of their own; a policy may not list them as tiers.
RESERVED_TIERS = (SESSION, DESIGN_TIER)

#: the fix rules SKILL.md section 4 defines.
FIX_RULES = ("fix-the-class", "probe-impossibility-claims")


class ShieldError(ValueError):
    """A role that cannot be given a model, or a policy file that cannot be trusted. Raised before
    a spawn, so a run never falls back to the session model by accident."""


def policy(root: str | None = None) -> dict:
    """Load `formations/shield.yaml`, check every key the resolver and the round loop read, and
    return it normalized: defaults filled in, so no reader needs its own default (t-221)."""
    path = os.path.join(root or paths.root(), POLICY_PATH)
    if not os.path.isfile(path):
        raise ShieldError("no shield policy at %s" % path)
    with open(path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    return _normalize(data, path, os.path.join(os.path.dirname(os.path.dirname(path)), "agents"))


class _Check:
    """Typed reads of one policy file; every failure names the key path."""

    def __init__(self, path: str, tiers: list | None = None):
        self.path, self.tiers = path, tiers or []

    def fail(self, key: str, why: str):
        raise ShieldError("shield policy %s: %s %s" % (self.path, key, why))

    def mapping(self, node, key: str, *, required=True, known=None) -> dict:
        if node is None and not required:
            return {}
        if not isinstance(node, dict) or (required and not node):
            self.fail(key, "must be a mapping" if node is not None else "is missing")
        if known is not None:
            extra = sorted(set(node) - set(known))
            if extra:
                self.fail(key, "has unknown keys %s (known: %s)" % (", ".join(map(str, extra)),
                                                                   ", ".join(known)))
        return node

    def integer(self, value, key: str, low: int) -> int:
        if not isinstance(value, int) or isinstance(value, bool) or value < low:
            self.fail(key, "must be an integer of at least %d" % low)
        return value

    def boolean(self, value, key: str) -> bool:
        if not isinstance(value, bool):
            self.fail(key, "must be true or false")
        return value

    def enum(self, value, key: str, allowed) -> object:
        # Type-strict: True is not 1 and 1.0 is not 1 here.
        if not any(value == a and type(value) is type(a) for a in allowed):
            self.fail(key, "is %r (allowed: %s)" % (value, ", ".join(map(str, allowed))))
        return value

    def tier(self, value, key: str, extra=()) -> str:
        if not isinstance(value, str) or value not in list(self.tiers) + list(extra):
            self.fail(key, "names tier %r (known: %s)" % (value, ", ".join(self.tiers)))
        return value

    def strings(self, value, key: str, allowed=None) -> list:
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            self.fail(key, "must be a list of names")
        if allowed is not None:
            for v in value:
                self.enum(v, key, allowed)
        if len(set(value)) != len(value):
            self.fail(key, "lists a name twice")
        return list(value)


def _normalize(data: dict, path: str, agents_dir: str | None = None) -> dict:
    """Every key `ladder()`, the helpers and the round loop read, checked and with its default.
    Unknown keys are refused at every level, every block the loop needs is required, and each
    value the loop branches on comes from a closed set, so a misspelt or missing key fails here,
    not at run time (t-221 rounds 2 and 3). `agents_dir`, when given, is where step archetypes
    must exist."""
    c = _Check(path)
    if not isinstance(data, dict):
        c.fail("the file", "must be a mapping")
    c.mapping(data, "the top level", known=("version", "tiers", "runtimes", "design", "think",
                                            "escalation", "verifier", _NORMALIZED))
    c.enum(data.get("version", 1), "version", (1,))
    c.enum(data.get(_NORMALIZED, True), _NORMALIZED, (True,))
    tiers = data.get("tiers")
    if not tiers:
        c.fail("tiers", "is missing")
    c.tiers = c.strings(tiers, "tiers")
    for t in c.tiers:
        if t in RESERVED_TIERS:
            c.fail("tiers", "names the reserved tier %r" % t)
    runtimes = c.mapping(data.get("runtimes"), "runtimes")
    for runtime, table in runtimes.items():
        table = c.mapping(table, "runtimes.%s" % runtime, known=c.tiers)
        for t in c.tiers:
            if not isinstance(table.get(t), str) or not table.get(t):
                c.fail("runtimes.%s.%s" % (runtime, t), "must name a model")
    design = c.mapping(data.get("design"), "design", known=("tier",))
    c.tier(design.get("tier"), "design.tier")

    esc = c.mapping(data.get("escalation"), "escalation",
                    known=("round_cap", "investigator", "causes", "promote", "at_cap", "fix_rules",
                           "class_check", "judge_tier"))
    if esc.get("round_cap") is None:
        c.fail("escalation.round_cap", "is missing")
    cap = c.integer(esc["round_cap"], "escalation.round_cap", 1)
    esc["at_cap"] = c.enum(esc.get("at_cap", STOP_AT_CAP), "escalation.at_cap", (STOP_AT_CAP,))
    inv = c.mapping(esc.get("investigator"), "escalation.investigator",
                    known=("tier", "when", "runs", "reports_to", "report"))
    c.tier(inv.get("tier"), "escalation.investigator.tier")
    esc["judge_tier"] = c.tier(esc.get("judge_tier", inv["tier"]), "escalation.judge_tier")
    inv["when"] = c.enum(inv.get("when", "every-failed-round"), "escalation.investigator.when",
                         ("every-failed-round",))
    inv["runs"] = c.enum(inv.get("runs", "before-next-fix"), "escalation.investigator.runs",
                         ("before-next-fix",))
    inv["reports_to"] = c.enum(inv.get("reports_to", "orchestrator"),
                               "escalation.investigator.reports_to", ("orchestrator",))
    inv["report"] = c.strings(inv.get("report", list(REPORT_FIELDS)),
                              "escalation.investigator.report", REPORT_FIELDS)
    lacking = [f for f in REPORT_REQUIRED if f not in inv["report"]]
    if lacking:
        c.fail("escalation.investigator.report", "lacks %s, which the loop always asks for"
               % ", ".join(lacking))
    causes = c.mapping(esc.get("causes"), "escalation.causes", known=CAUSES)
    for cause in CAUSES:
        if cause not in causes:
            c.fail("escalation.causes.%s" % cause, "is missing")
        c.enum(causes[cause], "escalation.causes.%s" % cause, ACTIONS)
    promote_rule = c.mapping(esc.get("promote"), "escalation.promote", required=False,
                             known=("steps", "ceiling"))
    promote_rule["steps"] = c.integer(promote_rule.get("steps", 1), "escalation.promote.steps", 1)
    promote_rule["ceiling"] = c.tier(promote_rule.get("ceiling", c.tiers[-1]),
                                     "escalation.promote.ceiling")
    esc["promote"] = promote_rule
    if esc.get("fix_rules") is None:
        c.fail("escalation.fix_rules", "is missing")
    esc["fix_rules"] = c.strings(esc["fix_rules"], "escalation.fix_rules", FIX_RULES)
    lacking = [r for r in FIX_RULES if r not in esc["fix_rules"]]
    if lacking:
        c.fail("escalation.fix_rules", "lacks %s (every fix brief carries every rule)"
               % ", ".join(lacking))
    cc = c.mapping(esc.get("class_check"), "escalation.class_check",
                   known=("after_failed_rounds", "non_converging", "tier", "max_redesigns"))
    cc["after_failed_rounds"] = c.integer(cc.get("after_failed_rounds", 3),
                                          "escalation.class_check.after_failed_rounds", 1)
    if cc["after_failed_rounds"] >= cap:
        c.fail("escalation.class_check.after_failed_rounds",
               "must be below round_cap (%d): the loop stops at the cap before it counts" % cap)
    cc["non_converging"] = c.boolean(cc.get("non_converging", False),
                                     "escalation.class_check.non_converging")
    cc["tier"] = c.tier(cc.get("tier", esc["judge_tier"]), "escalation.class_check.tier")
    cc["max_redesigns"] = c.integer(cc.get("max_redesigns", 1),
                                    "escalation.class_check.max_redesigns", 0)
    esc["class_check"] = cc

    ver = c.mapping(data.get("verifier"), "verifier",
                    known=("mechanical", "judgment_lens_tier", "adversarial_lens_tier", "recheck",
                           "every_round", "confirmer", "attack_freeze_round", "minor_findings",
                           "paired_verifiers", "regression_tier"))
    ver["mechanical"] = c.enum(ver.get("mechanical", "script"), "verifier.mechanical", ("script",))
    ver["regression_tier"] = c.tier(ver.get("regression_tier", c.tiers[0]),
                                    "verifier.regression_tier")
    c.tier(ver.get("judgment_lens_tier"), "verifier.judgment_lens_tier")
    c.tier(ver.get("adversarial_lens_tier"), "verifier.adversarial_lens_tier")
    ver["recheck"] = c.enum(ver.get("recheck", "failed-lenses-only"), "verifier.recheck",
                            ("failed-lenses-only",))
    ver["every_round"] = c.strings(ver.get("every_round", ["regression-script"]),
                                   "verifier.every_round", ("regression-script",))
    if not ver["every_round"]:
        c.fail("verifier.every_round", "is empty (the loop runs the regression script every round)")
    conf = c.mapping(ver.get("confirmer"), "verifier.confirmer", known=("tier", "blind", "when"))
    c.tier(conf.get("tier"), "verifier.confirmer.tier")
    conf["blind"] = c.enum(conf.get("blind", True), "verifier.confirmer.blind", (True,))
    conf["when"] = c.enum(conf.get("when", "all-lenses-pass"), "verifier.confirmer.when",
                          ("all-lenses-pass",))
    if ver.get("attack_freeze_round") is not None:
        c.integer(ver["attack_freeze_round"], "verifier.attack_freeze_round", 1)
        if ver["attack_freeze_round"] > cap:
            c.fail("verifier.attack_freeze_round", "must not be above round_cap (%d)" % cap)
    ver["minor_findings"] = c.enum(ver.get("minor_findings", "ledger-once"),
                                   "verifier.minor_findings", ("ledger-once",))
    pv = ver.get("paired_verifiers", [])
    if not isinstance(pv, list):
        c.fail("verifier.paired_verifiers", "must be a list of tiers")
    for i, t in enumerate(pv):
        c.tier(t, "verifier.paired_verifiers[%d]" % i)

    think = c.mapping(data.get("think"), "think",
                      known=("default_size", "full_when", "steps", "lite"))
    think["default_size"] = c.enum(think.get("default_size", "lite"), "think.default_size",
                                   THINK_SIZES)
    think["full_when"] = c.strings(think.get("full_when", []), "think.full_when", FULL_WHEN)
    steps = c.mapping(think.get("steps"), "think.steps", known=THINK_STEPS)
    for name in THINK_STEPS:
        key = "think.steps.%s" % name
        if name not in steps:
            c.fail(key, "is missing (the loop runs every step of a full think stage)")
        step = c.mapping(steps[name], key, known=("archetype", "tier", "count", "when"))
        arch = step.get("archetype")
        if not isinstance(arch, str) or not arch:
            c.fail(key + ".archetype", "must name an archetype")
        if agents_dir and os.path.isdir(agents_dir) and \
                not os.path.isfile(os.path.join(agents_dir, "%s.md" % arch)):
            c.fail(key + ".archetype", "names %r, which has no brief in agents/" % arch)
        c.tier(step.get("tier"), key + ".tier", extra=(DESIGN_TIER,))
        want = STEP_COUNTS.get(name, 1)
        step["count"] = c.integer(step.get("count", want), key + ".count", 1)
        if step["count"] != want:
            c.fail(key + ".count", "must be %d" % want)
        step["when"] = c.enum(step.get("when", STEP_WHEN.get(name)), key + ".when",
                              (STEP_WHEN.get(name),))
    think["steps"] = steps
    if think.get("lite") is None:
        c.fail("think.lite", "is missing")
    think["lite"] = c.strings(think["lite"], "think.lite", THINK_STEPS)
    if "pick" not in think["lite"]:
        c.fail("think.lite", "has no pick step; the build starts from the pick's brief")
    data["think"] = think
    data[_NORMALIZED] = True
    return data


#: set on a dict policy() returned (a marker for readers; helpers normalize every dict they get).
_NORMALIZED = "_normalized"


def _pol(pol: dict | None, root: str | None) -> dict:
    """The normalized policy: load it, or normalize a dict a caller built by hand."""
    if pol is None:
        return policy(root)
    import copy
    # Normalizing again is cheap and catches a dict a caller changed after policy() returned it.
    return _normalize(copy.deepcopy(pol), "<dict given to a shield helper>")


def known_tiers(pol: dict) -> list:
    return list(pol["tiers"]) + [SESSION]


def archetype_tier(archetype: str, root: str | None = None) -> str | None:
    """The default tier an archetype brief declares in its front matter, or None."""
    from alpaca.formation.manifest import _front_matter
    path = os.path.join(root or paths.root(), "agents", "%s.md" % archetype)
    if not os.path.isfile(path):
        return None
    with open(path, "r", encoding="utf-8") as fh:
        data = _front_matter(fh.read()) or {}
    tier = data.get("tier")
    return str(tier) if tier else None


def tier_of(m, role_id: str, root: str | None = None, pol: dict | None = None) -> str:
    """The tier a formation role runs at: its own `tier:`, else its archetype's. Refuses a role
    with neither, and a tier the policy does not list."""
    pol = _pol(pol, root)
    role = m.role(role_id)
    tier = role.get("tier") or archetype_tier(str(role.get("archetype", "")), root)
    if not tier:
        raise ShieldError("formation %r role %r has no tier and its archetype %r declares none"
                          % (m.name, role_id, role.get("archetype")))
    tier = str(tier)
    if tier not in known_tiers(pol):
        raise ShieldError("formation %r role %r names tier %r; known tiers: %s"
                          % (m.name, role_id, tier, ", ".join(known_tiers(pol))))
    return tier


def model_for(tier: str, runtime: str = "claude", root: str | None = None,
              pol: dict | None = None) -> str:
    """The model a tier runs on for `runtime`. `session` stays `session`: the main chat keeps the
    operator's model. Refuses a runtime the policy does not map."""
    pol = _pol(pol, root)
    if tier == SESSION:
        return SESSION
    table = pol["runtimes"].get(runtime)
    if not table:
        raise ShieldError("shield policy maps no models for runtime %r (mapped: %s)"
                          % (runtime, ", ".join(sorted(pol["runtimes"]))))
    if tier not in table:
        raise ShieldError("shield policy has no %r model for tier %r" % (runtime, tier))
    return str(table[tier])


def model_of(m, role_id: str, runtime: str = "claude", root: str | None = None) -> str:
    """The model a formation role runs on: its tier, mapped for `runtime`."""
    pol = policy(root)
    return model_for(tier_of(m, role_id, root, pol), runtime, root, pol)


def round_cap(m=None, root: str | None = None, pol: dict | None = None) -> int:
    """The round cap for a formation: the policy's, or the formation's own `escalation.round_cap`
    when it is lower. A formation may lower the cap, never raise it."""
    pol = _pol(pol, root)
    cap = int(pol["escalation"]["round_cap"])
    from alpaca.formation import manifest
    if m is None or manifest.ESCALATION not in m.raw:
        return cap                                   # absent: the policy cap
    block = m.raw[manifest.ESCALATION]
    if not isinstance(block, dict) or set(block) != set(manifest.ESCALATION_KEYS):
        raise ShieldError("formation %r: escalation may hold only round_cap and must set it "
                          "(got %r)" % (m.name, block))
    own = block["round_cap"]
    if not isinstance(own, int) or isinstance(own, bool) or own < 1:
        raise ShieldError("formation %r: escalation.round_cap must be a positive integer "
                          "(got %r)" % (m.name, own))
    if own > cap:
        raise ShieldError("formation %r raises round_cap to %d past the shield cap %d"
                          % (m.name, own, cap))
    limit = pol["escalation"]["class_check"]["after_failed_rounds"]
    if own <= limit:
        raise ShieldError("formation %r lowers round_cap to %d, at or below the class check's "
                          "after_failed_rounds %d: the class check could never run"
                          % (m.name, own, limit))
    return own


def next_step(round_no: int, passed: bool, cause: str | None = None, *, m=None,
              root: str | None = None) -> str:
    """The ladder. Called after round `round_no` (1-based) of checks.

    * passed: `confirm` (one blind high confirmer, then close).
    * failed at or past the cap: `block-and-ask-owner`; no further round.
    * failed with no investigator report yet: `continue` (spawn one investigator on this failure
      and wait for its report before the next fix starts; t-221).
    * failed with the investigator's `cause`: the action the policy maps that cause to.
    """
    pol = policy(root)
    if round_no < 1:
        raise ShieldError("rounds count from 1, got %r" % round_no)
    if passed:
        return CONFIRM
    if round_no >= round_cap(m, root, pol):
        return str(pol["escalation"].get("at_cap") or STOP_AT_CAP)
    if cause is None:
        return CONTINUE
    causes = pol["escalation"]["causes"]
    if cause not in causes:
        raise ShieldError("unknown investigator cause %r (known: %s)"
                          % (cause, ", ".join(sorted(causes))))
    action = str(causes[cause])
    if action == REDESIGN and max_redesigns(pol=pol) == 0:
        return str(pol["escalation"]["at_cap"])
    return action


def promote(tier: str, root: str | None = None, pol: dict | None = None) -> str:
    """One promotion step up from `tier`, never past the policy ceiling. A tier already at or
    above the ceiling stays where it is."""
    pol = _pol(pol, root)
    order = list(pol["tiers"])
    if tier not in order:
        raise ShieldError("cannot promote tier %r (known: %s)" % (tier, ", ".join(order)))
    rule = pol["escalation"]["promote"]
    top = order.index(rule["ceiling"])
    if order.index(tier) >= top:
        return tier
    return order[min(order.index(tier) + rule["steps"], top)]


def _step_model(step: dict, runtime: str, root, pol: dict) -> str:
    tier = str(step["tier"])
    if tier == DESIGN_TIER:
        tier = str(pol["design"]["tier"])
    return model_for(tier, runtime, root, pol)


def think_plan(size: str = "full", runtime: str = "claude", root: str | None = None,
               pol: dict | None = None) -> list:
    """The think-stage steps for `size`, in run order, each as {step, archetype, tier, model,
    count, when}. `off`, or a policy with no think steps, is the empty plan."""
    pol = _pol(pol, root)
    if size not in THINK_SIZES:
        raise ShieldError("think size %r (known: %s)" % (size, ", ".join(THINK_SIZES)))
    think = pol["think"]
    steps = think["steps"]
    if size == "off" or not steps:
        return []
    chosen = set(steps) if size == "full" else set(think["lite"])
    out = []
    for name in THINK_STEPS:
        if name not in chosen:
            continue
        step = steps[name]
        out.append({"step": name, "archetype": step.get("archetype"), "tier": str(step["tier"]),
                    "model": _step_model(step, runtime, root, pol), "count": step["count"],
                    "when": step["when"]})
    return out


def think_groups(size: str = "full", runtime: str = "claude", root: str | None = None,
                 pol: dict | None = None) -> list:
    """The think plan as the round loop runs it: a list of groups in order, each a list of single
    spawns {step, index, model, needs_probe, label}. The loop holds no step list, count or rule of
    its own; it runs these groups one after another and the spawns of a group in parallel."""
    plan = {s["step"]: s for s in think_plan(size, runtime, root, pol)}
    groups = []
    for group in THINK_GROUPS:
        spawns = []
        for name in group:
            s = plan.get(name)
            if not s:
                continue
            for i in range(s["count"]):
                spawns.append({"step": name, "index": i, "model": s["model"],
                               "needs_probe": s["when"] == "task-names-probe",
                               "label": name + (str(i) if s["count"] > 1 else "")})
        if spawns:
            groups.append(spawns)
    return groups


def think_size(*, adversarial: bool = False, follow_up: bool = False, redesign: bool = False,
               root: str | None = None, pol: dict | None = None) -> str:
    """The size a task's think stage runs at: REDESIGN_SIZE for a redesign; `full` when a
    `full_when` condition holds; otherwise the policy default."""
    pol = _pol(pol, root)
    if redesign:
        return REDESIGN_SIZE
    think = pol["think"]
    held = {"adversarial-lens": adversarial, "follow-up": follow_up}
    if any(held[c] for c in think["full_when"]):
        return "full"
    return str(think["default_size"])


def max_redesigns(root: str | None = None, pol: dict | None = None) -> int:
    """How many times one task may go back through the think stage: 0 without a class_check."""
    pol = _pol(pol, root)
    cc = pol["escalation"].get("class_check")
    return int(cc["max_redesigns"]) if cc else 0


def class_check_due(failed_rounds: int, blocking_counts=(), *, root: str | None = None,
                    pol: dict | None = None) -> bool:
    """After a failed round: is it time to stop patching and look for the bug class?

    `failed_rounds` counts the failed rounds so far, this one included. `blocking_counts` lists
    the number of blocking items per failed round, oldest first. Fires at `after_failed_rounds`,
    or earlier when `non_converging` holds and the latest failed round has as many or more
    blocking items than the one before it (the fixes are not converging). round-loop.js holds the
    same rule in shieldClassDue; test_shield_round_loop.py compares the two."""
    pol = _pol(pol, root)
    rule = pol["escalation"].get("class_check")
    if not rule:
        return False
    if failed_rounds >= rule["after_failed_rounds"]:
        return True
    counts = list(blocking_counts)
    return rule["non_converging"] and len(counts) >= 2 and counts[-1] >= counts[-2] > 0


def class_action(found_class: str | None, redesigns_used: int, *, root: str | None = None,
                 pol: dict | None = None) -> str:
    """What a class reviewer's answer leads to. No class: `continue`. A class with a redesign
    left: `redesign`. A class after the last redesign: the cap action, because patching a known
    class again is the loop this rule exists to stop."""
    pol = _pol(pol, root)
    if not found_class or str(found_class).lower() == "none":
        return CONTINUE
    if redesigns_used < max_redesigns(pol=pol):
        return REDESIGN
    return str(pol["escalation"]["at_cap"])


def resolve_all(runtime: str = "claude", root: str | None = None) -> dict:
    """Every role of every registered formation, resolved to (tier, model). Raises on the first
    role that cannot be resolved, so one broken manifest cannot hide behind the others."""
    from alpaca.formation import manifest
    pol = policy(root)
    out = {}
    reg = manifest.registry(root)
    for name in reg.names():
        m = reg.get(name)
        round_cap(m, root, pol)
        out[name] = {}
        for role in m.roles:
            tier = tier_of(m, role["id"], root, pol)
            out[name][role["id"]] = (tier, model_for(tier, runtime, root, pol))
    return out


def ladder(runtime: str = "claude", root: str | None = None) -> dict:
    """The escalation and verifier rules with every tier resolved to a model, in the shape a
    workflow script takes through its `args` (a script cannot read files). Since t-221 it hands
    the round loop decisions (think groups per size, the size per task flags, a model per role
    and mode, the promotion table, the redesign limit) so the loop keeps no copy of a rule."""
    pol = policy(root)
    esc, ver = pol["escalation"], pol["verifier"]

    def mdl(tier):
        return model_for(tier, runtime, root, pol)

    cc = esc["class_check"]
    design = mdl(pol["design"]["tier"])
    build_models = {"regression": mdl(ver["regression_tier"]),
                    "judgment_lens": mdl(ver["judgment_lens_tier"]),
                    "adversarial_lens": mdl(ver["adversarial_lens_tier"]),
                    "judge": mdl(esc["judge_tier"]),
                    "investigator": mdl(esc["investigator"]["tier"]),
                    "confirmer": mdl(ver["confirmer"]["tier"]),
                    "class": mdl(cc["tier"]) if cc else None,
                    "fixer": None}              # None: the formation role's tier, promoted
    design_models = dict(build_models, judgment_lens=design, adversarial_lens=design,
                         fixer=design, confirmer=design)
    return {
        "round_cap": round_cap(None, root, pol),
        "investigator_runs": esc["investigator"]["runs"],
        "fix_rules": list(esc["fix_rules"]),
        "class_check": ({"after_failed_rounds": cc["after_failed_rounds"],
                         "non_converging": cc["non_converging"],
                         "max_redesigns": cc["max_redesigns"],
                         "model": mdl(cc["tier"])} if cc else None),
        "max_redesigns": max_redesigns(pol=pol),
        "think": {"default_size": pol["think"]["default_size"],
                  "full_when": list(pol["think"]["full_when"]),
                  "full": think_plan("full", runtime, root, pol),
                  "lite": think_plan("lite", runtime, root, pol),
                  "groups": {size: think_groups(size, runtime, root, pol)
                             for size in THINK_SIZES},
                  # key "<adversarial>,<follow_up>" with 0 or 1 each
                  "size_for": {"%d,%d" % (a, f): think_size(adversarial=bool(a),
                                                            follow_up=bool(f), pol=pol)
                               for a in (0, 1) for f in (0, 1)},
                  "redesign_size": think_size(redesign=True, pol=pol)},
        "models": {"build": build_models, "design": design_models},
        "promote_next": {t: promote(t, pol=pol) for t in pol["tiers"]},
        "at_cap": esc["at_cap"],
        "investigator": mdl(esc["investigator"]["tier"]),
        "investigator_report": list(esc["investigator"]["report"]),
        # `causes` keeps the meaning old round-loop copies expect; `cause_actions` is the policy.
        "causes": {k: (v if v in LEGACY_ACTIONS else "cancel-and-ask-owner")
                   for k, v in esc["causes"].items()},
        "cause_actions": dict(esc["causes"]),
        "promote": dict(esc["promote"]),
        "tiers": {t: mdl(t) for t in pol["tiers"]},
        "judgment_lens": mdl(ver["judgment_lens_tier"]),
        "adversarial_lens": mdl(ver["adversarial_lens_tier"]),
        "confirmer": mdl(ver["confirmer"]["tier"]),
        "design": design,
        "paired_verifiers": [mdl(t) for t in ver["paired_verifiers"]],
        "recheck": ver["recheck"],
        "every_round": list(ver["every_round"]),
        "attack_freeze_round": ver.get("attack_freeze_round"),
        "minor_findings": ver.get("minor_findings"),
        "mechanical": ver["mechanical"],
    }


def main(argv=None) -> int:
    """`python -m alpaca.formation.shield [formation ...] [--runtime claude] [--json]`: print each
    role's tier and model and the round cap for the named formations (all when none are named).
    `--json` prints the formations plus the resolved ladder, ready for a workflow's `args`."""
    import argparse
    import json
    ap = argparse.ArgumentParser(prog="alpaca.formation.shield")
    ap.add_argument("formation", nargs="*")
    ap.add_argument("--runtime", default="claude")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    from alpaca.formation import manifest
    table = resolve_all(args.runtime)
    reg = manifest.registry()
    names = args.formation or sorted(table)
    for name in names:
        if name not in table:
            raise ShieldError("no formation named %r is registered" % name)
    if args.json:
        out = {"runtime": args.runtime,
               "formations": {n: {"round_cap": round_cap(reg.get(n)),
                                  "roles": {r: {"tier": t, "model": mo}
                                            for r, (t, mo) in table[n].items()}}
                              for n in names},
               "ladder": ladder(args.runtime)}
        print(json.dumps(out, indent=1, sort_keys=True))
        return 0
    for name in names:
        print("%s  round_cap=%d" % (name, round_cap(reg.get(name))))
        for role_id, (tier, model) in table[name].items():
            print("  %-20s %-8s %s" % (role_id, tier, model))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
