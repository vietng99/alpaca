"""op-013 t-161 proof: the /shield round loop (.claude/skills/shield/round-loop.js) follows the ladder.

The loop runs under node with scripted fake agents and the real resolver output as `args.shield`,
so every model it spawns is checked against formations/shield.yaml. Scenarios: a first-round pass
closes through the confirmer; a failure runs an investigator before the next fix (t-221) and the fix
reads its report; a `capability` cause promotes the fixer; a `wrong-direction` cause re-enters the
think stage; `contract-unclear` asks the owner; five failures stop at the cap with no sixth round;
later rounds recheck only the lenses that failed; the think stage runs before round 1 in its group
order; the class check fires on non-converging rounds and redesigns once, then blocks.
"""
import json
import os
import shutil
import subprocess

import pytest

from alpaca.formation import shield
from alpaca.tests.conftest import REPO

LOOP = os.path.join(REPO, ".claude", "skills", "shield", "round-loop.js")
NODE = shutil.which("node")

HARNESS = r"""
const fs = require('fs')
const input = JSON.parse(fs.readFileSync(0, 'utf8'))
const args = { shield: input.shield, fixerRole: 'builder' }
const calls = []
const script = input.script   // per label prefix: list of responses, consumed in order
function next(kind) {
  const q = script[kind] || []
  return q.length ? q.shift() : script[kind + ':default']
}
const events = []   // 'start:<kind>' and 'end:<kind>', in the order they happen
// op-019: the loop prepends the intent rule (SHIELD_RULES) to every prompt; the fakes read the
// task's own prompt after it, and count how many prompts carried it.
const RULE_PREFIX = /^A contract line labelled agent-proposed[^\n]*\n\n/
let ruled = 0
async function agent(prompt, opts) {
  if (RULE_PREFIX.test(prompt)) { ruled += 1; prompt = prompt.replace(RULE_PREFIX, '') }
  const kind = opts.label.split(' ')[1]
  calls.push({ kind, round: Number(opts.label.split(' r').pop()), model: opts.model || null,
    prompt })
  events.push('start:' + kind)
  await new Promise((r) => setTimeout(r, 2))   // a real agent takes time; let others start
  events.push('end:' + kind)
  return next(kind)
}
async function parallel(thunks) { return Promise.all(thunks.map((f) => f())) }
const SRC = fs.readFileSync(input.loop, 'utf8')
eval(SRC + `
;(async () => {
  const lens = (key, adversarial) => ({ key, adversarial, prompt: () => key })
  const t = { id: 'T', lenses: [lens('contract'), lens('design'), lens('adversarial', true)],
    mechanical: () => 'm', investigate: () => 'i', judge: () => 'j', confirm: () => 'c',
    fix: (round, failed, reports, ctx) => JSON.stringify({ reports: reports.map((r) => r.file),
      brief: ctx && ctx.brief ? ctx.brief.file : null, redesign: !!(ctx && ctx.redesign),
      rules: ctx ? ctx.fixRules : null }),
    classReview: () => 'k',
    think: input.noThink ? undefined : { size: input.size || undefined, hasProbe: !!input.probe,
      prompt: (step, ctx) => step + ':' + ctx.redesign },
    brief: input.brief || undefined, design: !!input.design }
  const out = await shieldRounds(t)
  process.stdout.write(JSON.stringify({ out, calls, events, ruled }))
})()`)
"""

PASS = {"verdict": "pass", "blocking": [], "file": "x"}
FAIL = {"verdict": "fail", "blocking": ["b"], "file": "x"}


def _run(script, size="lite", probe=False, brief=None, design=False, no_think=False, cap=5,
         runtime="claude"):
    data = {"shield": {"formations": {"builder-verifier": {"round_cap": cap, "roles": {
                k: {"tier": t, "model": m}
                for k, (t, m) in shield.resolve_all(runtime, REPO)["builder-verifier"].items()}}},
            "ladder": shield.ladder(runtime, REPO)},
            "script": script, "loop": LOOP, "size": size, "probe": probe, "brief": brief,
            "design": design, "noThink": no_think}
    proc = subprocess.run([NODE, "-e", HARNESS], input=json.dumps(data), capture_output=True,
                          text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")


def test_a_first_round_pass_closes_through_one_confirmer():
    r = _run({"regression:default": PASS, "contract:default": PASS, "design:default": PASS,
              "adversarial:default": PASS, "confirm:default": PASS})
    assert r["out"]["passed"] and r["out"]["round"] == 1
    models = {c["kind"]: c["model"] for c in r["calls"]}
    assert models == {"think-trace": "sonnet", "think-cause": "opus", "think-pick": "opus",
                      "regression": "sonnet", "contract": "sonnet", "design": "sonnet",
                      "adversarial": "opus", "confirm": "opus"}
    assert all(c["model"] for c in r["calls"]), "a spawn without a model"


def test_codex_models_flow_through_the_reference_loop_after_a_failure():
    # This simulates Workflow with Codex policy data; native dispatch is separate evidence.
    script = {"regression:default": PASS, "contract": [FAIL], "contract:default": PASS,
              "design:default": PASS, "adversarial:default": PASS, "confirm:default": PASS,
              "investigate:default": {"cause": "builder-error", "how": "h", "direction": "right",
                                      "file": "investigator"}, "fix:default": "done"}
    claude = _run(script, size="full")
    codex = _run(script, size="full", runtime="codex")
    assert codex["out"]["passed"] and codex["out"]["round"] == 2
    assert [(c["kind"], c["model"]) for c in codex["calls"]] == [
        (c["kind"], {"haiku": "gpt-6.1-sol", "sonnet": "gpt-6.1-sol", "opus": "gpt-6-astra"}[c["model"]])
        for c in claude["calls"]]


def test_a_failure_runs_an_investigator_and_rechecks_only_failed_lenses():
    r = _run({"regression:default": PASS, "contract": [FAIL], "contract:default": PASS,
              "design:default": PASS, "adversarial:default": PASS, "confirm:default": PASS,
              "investigate:default": {"cause": "builder-error", "how": "h", "direction": "right",
                                      "file": "x"},
              "fix:default": "done"})
    assert r["out"]["passed"] and r["out"]["round"] == 2
    round2 = [c["kind"] for c in r["calls"] if c["round"] == 2]
    assert "contract" in round2 and "design" not in round2 and "adversarial" not in round2
    inv = [c for c in r["calls"] if c["kind"] == "investigate"]
    assert len(inv) == 1 and inv[0]["model"] == "opus"
    assert [c["model"] for c in r["calls"] if c["kind"] == "fix"] == ["sonnet"]


def test_a_capability_cause_promotes_the_fixer():
    # the investigator reports before its round's fix, so the promotion applies to that fix
    r = _run({"regression:default": PASS, "contract": [FAIL, FAIL], "contract:default": PASS,
              "design:default": PASS, "adversarial:default": PASS, "confirm:default": PASS,
              "investigate": [{"cause": "builder-error", "how": "h", "direction": "right",
                               "file": "x"},
                              {"cause": "capability", "how": "h", "direction": "right",
                               "file": "x"}],
              "fix:default": "done"})
    assert r["out"]["passed"] and r["out"]["round"] == 3
    assert [c["model"] for c in r["calls"] if c["kind"] == "fix"] == ["sonnet", "opus"]


def test_a_contract_unclear_cause_stops_for_the_owner_in_the_same_round():
    r = _run({"regression:default": PASS, "contract:default": FAIL, "design:default": PASS,
              "adversarial:default": PASS,
              "investigate:default": {"cause": "contract-unclear", "how": "h", "direction": "right",
                                      "file": "x"},
              "fix:default": "done"})
    assert not r["out"]["passed"]
    assert r["out"]["action"] == "cancel-and-ask-owner" and r["out"]["round"] == 1
    assert not [c for c in r["calls"] if c["kind"] == "fix"]


def test_a_wrong_direction_goes_back_through_a_full_think_stage():
    r = _run({"regression:default": PASS, "contract": [FAIL], "contract:default": PASS,
              "design:default": PASS, "adversarial:default": PASS, "confirm:default": PASS,
              "investigate:default": {"cause": "wrong-direction", "how": "h", "direction": "wrong",
                                      "file": "x"},
              "think1-pick:default": {"summary": "s", "file": "brief1"},
              "fix:default": "done"})
    assert r["out"]["passed"] and r["out"]["redesigns"] == 1
    kinds = [c["kind"] for c in r["calls"]]
    assert {"think1-trace", "think1-history", "think1-cause", "think1-premortem",
            "think1-approaches0", "think1-approaches1", "think1-pick"} <= set(kinds)
    fix = [c for c in r["calls"] if c["kind"] == "fix"][0]
    assert json.loads(fix["prompt"])["brief"] == "brief1" and json.loads(fix["prompt"])["redesign"]
    # after a redesign every lens checks again
    assert {"contract", "design", "adversarial"} <= {c["kind"] for c in r["calls"] if c["round"] == 2}


def test_five_failures_stop_at_the_cap():
    r = _run({"regression:default": FAIL, "contract:default": FAIL, "design:default": FAIL,
              "adversarial:default": FAIL, "class:default": {"class": "none", "file": "c"},
              "investigate:default": {"cause": "builder-error", "how": "h", "direction": "right",
                                      "file": "x"},
              "fix:default": "done"})
    assert not r["out"]["passed"]
    assert r["out"]["action"] == "block-and-ask-owner" and r["out"]["round"] == 5
    assert max(c["round"] for c in r["calls"]) == 5
    assert len([c for c in r["calls"] if c["kind"] == "investigate"]) == 4
    assert len([c for c in r["calls"] if c["kind"] == "fix"]) == 4
    # the class reviewer found no class, so no round was spent on a redesign
    assert r["out"]["redesigns"] == 0

    # non-converging at round 2, the limit at round 3; never again in the same cycle
    assert [c["round"] for c in r["calls"] if c["kind"] == "class"] == [2, 3]


def test_the_think_stage_runs_before_round_one_in_group_order():
    r = _run({"regression:default": PASS, "contract:default": PASS, "design:default": PASS,
              "adversarial:default": PASS, "confirm:default": PASS,
              "think-pick:default": {"summary": "s", "file": "brief0"}}, size="full", probe=True)
    think = [c for c in r["calls"] if c["round"] == 0]
    assert all(c["kind"].startswith("think-") for c in think)
    assert not [c for c in r["calls"][:len(think)] if c["round"] != 0], "a round ran before think"
    pos = {c["kind"]: i for i, c in enumerate(r["calls"])}
    first = max(pos["think-trace"], pos["think-history"], pos["think-probe"])
    assert first < pos["think-cause"] < min(pos["think-premortem"], pos["think-approaches0"],
                                            pos["think-approaches1"])
    assert max(pos["think-premortem"], pos["think-approaches1"]) < pos["think-pick"]
    models = {c["kind"]: c["model"] for c in think}
    assert models["think-trace"] == "sonnet" and models["think-approaches0"] == "opus"
    assert r["out"]["briefs"] == [{"summary": "s", "file": "brief0"}]


def test_the_probe_step_runs_only_when_the_task_names_one():
    r = _run({"regression:default": PASS, "contract:default": PASS, "design:default": PASS,
              "adversarial:default": PASS, "confirm:default": PASS}, size="full")
    assert "think-probe" not in {c["kind"] for c in r["calls"]}
    r = _run({"regression:default": PASS, "contract:default": PASS, "design:default": PASS,
              "adversarial:default": PASS, "confirm:default": PASS}, size="off")
    assert not [c for c in r["calls"] if c["round"] == 0]


def test_the_investigator_reports_before_the_fix_and_the_fix_reads_it():
    r = _run({"regression:default": PASS, "contract": [FAIL, FAIL], "contract:default": PASS,
              "design:default": PASS, "adversarial:default": PASS, "confirm:default": PASS,
              "investigate": [{"cause": "builder-error", "how": "h", "direction": "right",
                               "file": "inv1"},
                              {"cause": "builder-error", "how": "h", "direction": "right",
                               "file": "inv2"}],
              "think-pick:default": {"summary": "s", "file": "brief0"},
              "fix:default": "done"})
    kinds = [(c["kind"], c["round"]) for c in r["calls"]]
    for n in (1, 2):
        assert kinds.index(("investigate", n)) < kinds.index(("fix", n))
    fixes = [json.loads(c["prompt"]) for c in r["calls"] if c["kind"] == "fix"]
    assert fixes[0]["reports"] == ["inv1"] and fixes[1]["reports"] == ["inv1", "inv2"]
    assert fixes[0]["brief"] == "brief0"
    assert fixes[0]["rules"] == ["fix-the-class", "probe-impossibility-claims"]


def test_converging_rounds_skip_the_class_check():
    big = {"verdict": "fail", "blocking": ["a", "b", "c"], "file": "x"}
    r = _run({"regression:default": PASS, "contract": [big, FAIL], "contract:default": PASS,
              "design:default": PASS, "adversarial:default": PASS, "confirm:default": PASS,
              "investigate:default": {"cause": "builder-error", "how": "h", "direction": "right",
                                      "file": "x"},
              "fix:default": "done"})
    assert r["out"]["passed"] and r["out"]["round"] == 3
    assert "class" not in {c["kind"] for c in r["calls"]}


def test_a_found_class_redesigns_once_then_blocks():
    r = _run({"regression:default": FAIL, "contract:default": FAIL, "design:default": PASS,
              "adversarial:default": PASS,
              "investigate:default": {"cause": "builder-error", "how": "h", "direction": "right",
                                      "file": "x"},
              "class:default": {"class": "shell text parsing", "file": "c"},
              "fix:default": "done"})
    assert not r["out"]["passed"]
    assert r["out"]["redesigns"] == 1
    assert r["out"]["action"] == "block-and-ask-owner" and r["out"]["round"] == 4
    assert "persists" in r["out"]["reason"]
    assert [c["round"] for c in r["calls"] if c["kind"] == "class"] == [2, 4]
    assert len([c for c in r["calls"] if c["kind"] == "think1-pick"]) == 1
    assert not [c for c in r["calls"] if c["kind"].startswith("think2")]


def test_each_think_group_waits_for_the_one_before():
    r = _run({"regression:default": PASS, "contract:default": PASS, "design:default": PASS,
              "adversarial:default": PASS, "confirm:default": PASS}, size="full", probe=True)
    ev = r["events"]
    at = ev.index
    for before in ("think-trace", "think-history", "think-probe"):
        assert at("end:" + before) < at("start:think-cause")
    for after in ("think-premortem", "think-approaches0", "think-approaches1"):
        assert at("end:think-cause") < at("start:" + after) < at("end:think-premortem") + 1
        assert at("end:" + after) < at("start:think-pick")
    # steps of one group run in parallel: both approaches start before either ends
    assert at("start:think-approaches1") < at("end:think-approaches0")
    assert at("end:think-pick") < at("start:regression")


def test_a_brief_from_the_design_loop_skips_the_think_stage():
    r = _run({"regression:default": PASS, "contract": [FAIL], "contract:default": PASS,
              "design:default": PASS, "adversarial:default": PASS, "confirm:default": PASS,
              "investigate:default": {"cause": "builder-error", "how": "h", "direction": "right",
                                      "file": "x"},
              "fix:default": "done"}, brief={"summary": "s", "file": "design-brief"})
    assert not [c for c in r["calls"] if c["round"] == 0]
    fix = [c for c in r["calls"] if c["kind"] == "fix"][0]
    assert json.loads(fix["prompt"])["brief"] == "design-brief"


def test_a_design_loop_fixes_on_the_design_model():
    r = _run({"regression:default": PASS, "contract": [FAIL], "contract:default": PASS,
              "design:default": PASS, "adversarial:default": PASS, "confirm:default": PASS,
              "investigate:default": {"cause": "builder-error", "how": "h", "direction": "right",
                                      "file": "x"},
              "fix:default": "done"}, design=True)
    assert [c["model"] for c in r["calls"] if c["kind"] == "fix"] == ["opus"]


def test_a_second_wrong_direction_blocks_instead_of_a_second_redesign():
    r = _run({"regression:default": FAIL, "contract:default": PASS, "design:default": PASS,
              "adversarial:default": PASS,
              "investigate:default": {"cause": "wrong-direction", "how": "h", "direction": "wrong",
                                      "file": "x"},
              "fix:default": "done"})
    assert not r["out"]["passed"] and r["out"]["redesigns"] == 1
    assert r["out"]["action"] == "block-and-ask-owner" and r["out"]["round"] == 2


# t-221 adversarial round 1 (.alpaca/proofs/op-013/t-221.logs/attack-set): one test per finding.

def test_an_adversarial_task_with_no_size_runs_the_full_think_stage():          # a03
    r = _run({"regression:default": PASS, "contract:default": PASS, "design:default": PASS,
              "adversarial:default": PASS, "confirm:default": PASS}, size=None)
    kinds = {c["kind"] for c in r["calls"] if c["round"] == 0}
    assert {"think-premortem", "think-approaches0", "think-approaches1"} <= kinds


def test_a_reopened_confirmer_is_never_re_rolled_in_the_same_round():           # a01
    r = _run({"regression:default": PASS, "contract:default": PASS, "design:default": PASS,
              "adversarial:default": PASS, "confirm": [FAIL], "confirm:default": PASS,
              "investigate:default": {"cause": "checker-false-positive", "how": "h",
                                      "direction": "right", "file": "x", "disputed": ["confirm"]},
              "judge:default": {"upheld": [], "file": "j"},
              "fix:default": "done"})
    assert r["out"]["passed"] and r["out"]["round"] == 2
    assert [c["round"] for c in r["calls"] if c["kind"] == "confirm"] == [1, 2]
    assert [c["round"] for c in r["calls"] if c["kind"] == "fix"] == [1]
    assert "judge" not in {c["kind"] for c in r["calls"]}


def test_a_redesign_without_think_prompts_blocks_instead_of_crashing():          # a02
    r = _run({"regression:default": FAIL, "contract:default": PASS, "design:default": PASS,
              "adversarial:default": PASS,
              "investigate:default": {"cause": "wrong-direction", "how": "h", "direction": "wrong",
                                      "file": "x"}, "fix:default": "done"}, size="off")
    # size off skips the first think stage only; the redesign still runs the full one
    assert not [c for c in r["calls"] if c["kind"].startswith("think-")]
    assert "think1-pick" in {c["kind"] for c in r["calls"]} and r["out"]["redesigns"] == 1
    r = _run({"regression:default": PASS}, no_think=True, size=None)
    assert r["out"]["round"] == 0 and "no think prompts" in r["out"]["reason"]
    assert not r["calls"]


def test_a_confirmer_reopen_counts_toward_non_convergence():                     # a04 case A
    r = _run({"regression:default": PASS, "contract": [FAIL], "contract:default": PASS,
              "design:default": PASS, "adversarial:default": PASS,
              "confirm": [FAIL], "confirm:default": PASS,
              "investigate:default": {"cause": "builder-error", "how": "h", "direction": "right",
                                      "file": "x"},
              "class:default": {"class": "none", "file": "c"},
              "fix:default": "done"})
    # round 1: one lens item; round 2: lenses pass, the confirmer reopens with one item
    assert [c["round"] for c in r["calls"] if c["kind"] == "class"][:1] == [2]


def test_findings_the_judge_dismissed_do_not_count():                            # a04 case B
    r = _run({"regression:default": PASS, "contract": [FAIL, FAIL], "contract:default": PASS,
              "design:default": PASS, "adversarial:default": PASS, "confirm:default": PASS,
              "investigate": [{"cause": "builder-error", "how": "h", "direction": "right",
                               "file": "x"},
                              {"cause": "checker-false-positive", "how": "h",
                               "direction": "right", "file": "x", "disputed": ["contract"]}],
              "judge:default": {"upheld": [], "file": "j"},
              "fix:default": "done"})
    assert r["out"]["passed"] and r["out"]["round"] == 2
    assert "class" not in {c["kind"] for c in r["calls"]}
    assert [c["round"] for c in r["calls"] if c["kind"] == "fix"] == [1]


def test_an_old_loop_copy_still_stops_on_wrong_direction():                     # a06
    lad = shield.ladder("claude", REPO)
    assert lad["causes"]["wrong-direction"] == "cancel-and-ask-owner"
    assert lad["cause_actions"]["wrong-direction"] == "redesign"
    assert set(lad["causes"].values()) <= set(shield.LEGACY_ACTIONS)



# t-221 round 2: the loop keeps no copy of a policy rule; what stays is compared with Python.

HELPERS = r"""
const fs = require('fs')
const input = JSON.parse(fs.readFileSync(0, 'utf8'))
const args = { shield: input.shield }
eval(fs.readFileSync(input.loop, 'utf8') + `
;(() => {
  const due = input.due.map(([f, c]) => shieldClassDue(f, c))
  const standing = input.standing.map((r) => shieldStanding(r))
  const lens = (adversarial) => ({ key: 'k', adversarial, prompt: () => 'p' })
  const sizes = input.flags.map(([a, f]) => shieldThinkSize({ lenses: [lens(!!a)], followUp: !!f }))
  process.stdout.write(JSON.stringify({ due, standing, sizes }))
})()`)
"""


def test_the_js_helpers_that_stay_agree_with_python():
    import random
    rng = random.Random(221)
    due = [[rng.randint(1, 5), [rng.randint(0, 6) for _ in range(rng.randint(0, 4))]]
           for _ in range(400)]
    standing = [None, {"verdict": "pass", "blocking": []}, {"verdict": "pass", "blocking": ["x"]},
                {"verdict": "fail", "blocking": []}, {"verdict": "fail", "blocking": ["a", "b"]},
                {"blocking": ["a"]}]
    flags = [[a, f] for a in (0, 1) for f in (0, 1)]
    lad = shield.ladder("claude", REPO)
    data = {"shield": {"formations": {"bv": {"roles": {"builder": {"tier": "mid", "model": "sonnet"}}}},
                       "ladder": lad}, "loop": LOOP, "due": due, "standing": standing,
            "flags": flags}
    proc = subprocess.run([NODE, "-e", HELPERS], input=json.dumps(data), capture_output=True,
                          text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    got = json.loads(proc.stdout)
    assert got["due"] == [shield.class_check_due(f, c, root=REPO) for f, c in due]
    assert got["standing"] == [1, 0, 0, 1, 2, 1]   # nothing returned still failed
    assert got["sizes"] == [shield.think_size(adversarial=bool(a), follow_up=bool(f), root=REPO)
                            for a, f in flags]


def test_every_ladder_key_the_loop_reads_is_emitted():
    import re
    src = open(LOOP, encoding="utf-8").read()
    used = set(re.findall(r"SHIELD\.([a-z_]+)", src))
    lad = shield.ladder("claude", REPO)
    assert used <= set(lad), sorted(used - set(lad))
    assert "causes" not in used, "the loop must read cause_actions, not the legacy causes view"
    for sub in set(re.findall(r"SHIELD\.think\.([a-z_]+)", src)):
        assert sub in lad["think"], sub
    for sub in set(re.findall(r"\bM\.([a-z_]+)", src)):
        assert sub in lad["models"]["build"], sub


def test_a_lens_that_returns_nothing_counts_as_one_blocking_item():               # r2-a11
    r = _run({"regression:default": PASS, "contract": [None, FAIL], "contract:default": PASS,
              "design:default": PASS, "adversarial:default": PASS, "confirm:default": PASS,
              "investigate:default": {"cause": "builder-error", "how": "h", "direction": "right",
                                      "file": "x"},
              "class:default": {"class": "none", "file": "c"},
              "fix:default": "done"})
    # round 1: the null lens counts 1; round 2: one item again, so the rounds do not converge
    assert [c["round"] for c in r["calls"] if c["kind"] == "class"] == [2]


def test_a_design_loop_runs_every_lens_on_the_design_model():                      # r2-a15
    r = _run({"regression:default": PASS, "contract:default": PASS, "design:default": PASS,
              "adversarial:default": PASS, "confirm:default": PASS}, design=True)
    models = {c["kind"]: c["model"] for c in r["calls"] if c["round"] == 1}
    assert models["contract"] == models["design"] == models["adversarial"] == "opus"
    assert models["regression"] == "sonnet"


def test_a_formation_cap_lower_than_the_policy_reaches_the_loop():
    r = _run({"regression:default": FAIL, "contract:default": PASS, "design:default": PASS,
              "adversarial:default": PASS,
              "investigate:default": {"cause": "builder-error", "how": "h", "direction": "right",
                                      "file": "x"},
              "class:default": {"class": "none", "file": "c"},
              "fix:default": "done"}, cap=4)
    assert r["out"]["round"] == 4 and r["out"]["action"] == "block-and-ask-owner"


def test_an_unknown_think_size_blocks_before_any_spawn():                         # r2-a17
    r = _run({"regression:default": PASS}, size="medium")
    assert r["out"]["round"] == 0 and "unknown think size" in r["out"]["reason"]
    assert not r["calls"]



def test_object_property_names_are_not_think_sizes():                         # r3 minor 2
    for size in ("constructor", "toString", "__proto__"):
        r = _run({"regression:default": PASS}, size=size)
        assert r["out"]["round"] == 0 and "unknown think size" in r["out"]["reason"], size
        assert not r["calls"]


def test_every_prompt_the_loop_sends_carries_the_intent_rule():
    """op-019 (brief P2): the loop prepends SHIELD_RULES to every prompt, the think stage, the
    investigator and the fixer included; the fakes strip it and count it."""
    r = _run({"regression:default": PASS, "contract": [FAIL], "contract:default": PASS,
              "design:default": PASS, "adversarial:default": PASS, "confirm:default": PASS,
              "investigate:default": {"cause": "builder-error", "how": "h", "direction": "right",
                                      "file": "inv"},
              "think-pick:default": {"summary": "s", "file": "brief0"},
              "fix:default": "done"})
    assert r["out"]["passed"] is True
    assert r["calls"] and r["ruled"] == len(r["calls"])
