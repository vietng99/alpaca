"""op-013 t-160 proof: the shield policy (formations/shield.yaml) and its resolver.

  * every role of every registered formation resolves to a tier and a Claude model, and a role
    with no tier anywhere, an unknown tier, or an unmapped runtime is refused (never inherits);
  * the ladder: a pass confirms, a failure continues with an investigator, an investigator cause
    maps to its action, and round_cap (5) stops the loop; a formation may lower the cap, never
    raise it;
  * promotion moves one tier up and stops at the ceiling;
  * dispatch spawns and assignments record the model.
Each control checks the positive and the negative path.
"""
import os
import shutil

import pytest

from alpaca import cli, db
from alpaca.formation import dispatch, manifest, shield
from alpaca.tests.conftest import REPO


def _tree(tmp_path, roles_yaml, escalation=""):
    """A minimal project root: the real policy and briefs, plus one formation under test."""
    root = tmp_path / "root"
    (root / "formations").mkdir(parents=True)
    shutil.copy(os.path.join(REPO, "formations", "shield.yaml"), root / "formations")
    shutil.copytree(os.path.join(REPO, "agents"), root / "agents")
    (root / "formations" / "probe.md").write_text(
        "---\nname: probe\nroles:\n%s\nphases: [build]\ngate_map:\n  build->verify: suite\n"
        "budget_class: light\noutput: probe\n%s---\n" % (roles_yaml, escalation))
    return str(root)


# ----------------------------------------------------------------- tiers and models
def test_every_registered_role_resolves_to_a_claude_model():
    table = shield.resolve_all("claude", REPO)
    assert set(table) == set(manifest.registry(REPO).names())
    models = {model for roles in table.values() for _tier, model in roles.values()}
    assert models <= {"sonnet", "opus", shield.SESSION}
    assert table["builder-verifier"]["builder"] == ("mid", "sonnet")
    assert table["nuclear"]["red"] == ("high", "opus")
    assert table["research-improve"]["notes"] == ("mid", "sonnet")
    assert table["nuclear"]["judge"] == ("top", "opus")


def test_the_low_tier_is_sonnet_and_no_research_role_resolves_to_it():
    # owner decision dec-ad4dd702ef73: the Haiku trial of the low tier ended with misses
    # (t-296 haiku-trial.md), so low is Sonnet again (t-440).
    assert shield.policy(REPO)["runtimes"]["claude"] == {
        "low": "sonnet", "mid": "sonnet", "high": "opus", "top": "opus"}
    assert shield.model_for("low", "claude", REPO) == "sonnet"
    assert shield.model_for("mid", "claude", REPO) == "sonnet"
    assert shield.model_for("high", "claude", REPO) == "opus"
    table = shield.resolve_all("claude", REPO)
    for name in ("research-improve", "research-repair"):
        assert {model for _tier, model in table[name].values()} <= {"sonnet", "opus"}, table[name]
    # low and mid share a model now, so pin the tiers too: no research role sits on low.
    assert table["research-improve"] == {
        "proposer": ("mid", "sonnet"), "implementer": ("mid", "sonnet"), "notes": ("mid", "sonnet")}
    assert table["research-repair"] == {
        "proposer": ("mid", "sonnet"), "implementer": ("mid", "sonnet"), "notes": ("mid", "sonnet"),
        "reproducer-writer": ("mid", "sonnet"), "reproducer-reviewer": ("mid", "sonnet")}


def test_the_paired_verifiers_keep_their_tiers_and_models():
    pol = shield.policy(REPO)["verifier"]
    assert pol["paired_verifiers"] == ["mid", "high"]
    assert (pol["judgment_lens_tier"], pol["adversarial_lens_tier"]) == ("mid", "high")
    lad = shield.ladder("claude", REPO)
    assert lad["paired_verifiers"] == ["sonnet", "opus"]
    assert (lad["judgment_lens"], lad["adversarial_lens"], lad["confirmer"]) == ("sonnet", "opus", "opus")


def test_a_role_override_beats_its_archetype(tmp_path):
    root = _tree(tmp_path, "  - id: a\n    archetype: scout\n    tier: high")
    m = manifest.registry(root).get("probe")
    assert shield.tier_of(m, "a", root) == "high"
    root2 = _tree(tmp_path / "x", "  - id: a\n    archetype: scout")
    assert shield.tier_of(manifest.registry(root2).get("probe"), "a", root2) == "low"


def test_a_role_with_no_tier_anywhere_is_refused(tmp_path):
    root = _tree(tmp_path, "  - id: a\n    archetype: nobody")
    m = manifest.registry(root).get("probe")
    with pytest.raises(shield.ShieldError, match="has no tier"):
        shield.model_of(m, "a", root=root)


def test_an_unknown_tier_is_refused(tmp_path):
    root = _tree(tmp_path, "  - id: a\n    archetype: eng\n    tier: ultra")
    m = manifest.registry(root).get("probe")
    with pytest.raises(shield.ShieldError, match="names tier"):
        shield.tier_of(m, "a", root)


def test_an_unmapped_runtime_is_refused_and_session_stays_session():
    with pytest.raises(shield.ShieldError, match="runtime 'unmapped'"):
        shield.model_for("mid", "unmapped", REPO)
    assert shield.model_for("session", "claude", REPO) == shield.SESSION
    assert shield.model_for("session", "codex", REPO) == shield.SESSION
    assert shield.model_for("mid", "claude", REPO) == "sonnet"


def test_codex_translates_every_formation_and_ladder_model(capsys, monkeypatch):
    import json

    def translate(value):
        if isinstance(value, dict):
            return {k: translate(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return type(value)(translate(v) for v in value)
        if isinstance(value, str):
            return {"haiku": "gpt-6.1-sol", "sonnet": "gpt-6.1-sol", "opus": "gpt-6-astra"}.get(value, value)
        return value

    assert shield.policy(REPO)["runtimes"]["codex"] == {
        "low": "gpt-6.1-sol", "mid": "gpt-6.1-sol",
        "high": "gpt-6-astra", "top": "gpt-6-astra"}
    assert shield.resolve_all("codex", REPO) == translate(shield.resolve_all("claude", REPO))
    assert shield.ladder("codex", REPO) == translate(shield.ladder("claude", REPO))
    monkeypatch.chdir(REPO)
    assert shield.main(["builder-verifier", "--runtime", "codex", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["runtime"] == "codex"
    assert result["formations"]["builder-verifier"]["roles"]["builder"]["model"] == "gpt-6.1-sol"
    assert result["ladder"]["paired_verifiers"] == ["gpt-6.1-sol", "gpt-6-astra"]


# ----------------------------------------------------------------- the ladder
def test_the_ladder_actions():
    assert shield.next_step(1, True, root=REPO) == shield.CONFIRM
    assert shield.next_step(1, False, root=REPO) == shield.CONTINUE
    assert shield.next_step(2, False, "builder-error", root=REPO) == "continue"
    assert shield.next_step(2, False, "capability", root=REPO) == "promote-fixer"
    assert shield.next_step(3, False, "checker-false-positive", root=REPO) == "judge-dispute"
    assert shield.next_step(3, False, "wrong-direction", root=REPO) == shield.REDESIGN
    assert shield.next_step(4, False, "contract-unclear", root=REPO) == "cancel-and-ask-owner"
    with pytest.raises(shield.ShieldError, match="unknown investigator cause"):
        shield.next_step(2, False, "bad-luck", root=REPO)


def test_the_round_cap_stops_the_loop():
    assert shield.round_cap(root=REPO) == 5
    assert shield.next_step(4, False, root=REPO) == shield.CONTINUE
    assert shield.next_step(5, False, root=REPO) == shield.STOP_AT_CAP
    assert shield.next_step(5, False, "builder-error", root=REPO) == shield.STOP_AT_CAP
    assert shield.next_step(6, False, root=REPO) == shield.STOP_AT_CAP
    assert shield.next_step(5, True, root=REPO) == shield.CONFIRM


def test_a_formation_may_lower_the_cap_but_not_raise_it(tmp_path):
    roles = "  - id: a\n    archetype: eng"
    low = _tree(tmp_path / "low", roles, "escalation:\n  round_cap: 4\n")
    m = manifest.registry(low).get("probe")
    assert shield.next_step(4, False, m=m, root=low) == shield.STOP_AT_CAP
    assert shield.next_step(3, False, m=m, root=low) == shield.CONTINUE
    high = _tree(tmp_path / "high", roles, "escalation:\n  round_cap: 7\n")
    with pytest.raises(shield.ShieldError, match="past the shield cap"):
        shield.round_cap(manifest.registry(high).get("probe"), high)


def test_promotion_moves_one_tier_and_stops_at_the_ceiling():
    assert shield.promote("low", REPO) == "mid"
    assert shield.promote("mid", REPO) == "high"
    assert shield.promote("high", REPO) == "high"
    assert shield.promote("top", REPO) == "top"
    with pytest.raises(shield.ShieldError):
        shield.promote("session", REPO)


# ----------------------------------------------------------------- dispatch records the model
def _events(conn, kind):
    return [e for e in db.events(conn, limit=10 ** 9) if e["kind"] == kind]


def test_dispatch_records_the_model(project):
    cli.main(["init"])
    conn = db.connect(project)
    dispatch.spawn(conn, "op-001", "w1", role="verifier", model="sonnet")
    dispatch.spawn(conn, "op-001", "w2", role="builder")
    spawns = {e["data"]["worker"]: e["data"] for e in _events(conn, dispatch.SPAWN_KIND)}
    assert spawns["w1"]["model"] == "sonnet"
    assert "model" not in spawns["w2"]
    dispatch.assign(conn, "op-001", ["r-1", "r-2"], {"r-1": "a", "r-2": "b"},
                    model={"r-1": "opus", "r-2": "haiku"})
    got = {e["data"]["row"]: e["data"].get("model") for e in _events(conn, dispatch.ASSIGN_KIND)}
    assert got == {"r-1": "opus", "r-2": "haiku"}


# ----------------------------------------------------------------- the workflow args shape
def test_the_ladder_json_resolves_every_tier_to_a_model(capsys, monkeypatch):
    import json
    lad = shield.ladder("claude", REPO)
    assert lad["round_cap"] == 5
    assert (lad["investigator"], lad["confirmer"], lad["adversarial_lens"],
            lad["judgment_lens"]) == ("opus", "opus", "opus", "sonnet")
    assert lad["paired_verifiers"] == ["sonnet", "opus"]
    monkeypatch.chdir(REPO)
    assert shield.main(["builder-verifier", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["formations"]["builder-verifier"]["roles"]["builder"]["model"] == "sonnet"
    with pytest.raises(shield.ShieldError):
        shield.main(["no-such-formation"])


def test_the_builder_verifier_json_puts_low_scouts_and_runners_on_sonnet(capsys, monkeypatch):
    # t-440: what `alpaca.formation.shield builder-verifier --runtime claude --json` prints.
    import json
    monkeypatch.chdir(REPO)
    assert shield.main(["builder-verifier", "--runtime", "claude", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    lad = out["ladder"]
    assert out["runtime"] == "claude"
    assert lad["tiers"]["low"] == "sonnet"
    scouts = [s for size in ("full", "lite") for s in lad["think"][size] if s["tier"] == "low"]
    assert {s["step"] for s in scouts} == {"trace", "history", "probe"}
    assert {s["model"] for s in scouts} == {"sonnet"}
    assert {s["model"] for group in lad["think"]["groups"]["full"] for s in group
            if s["step"] in ("trace", "history", "probe")} == {"sonnet"}
    assert lad["models"]["build"]["regression"] == lad["models"]["design"]["regression"] == "sonnet"
    assert "haiku" not in json.dumps(out)


# t-221: think first, investigator before the fix, class check.

def test_the_think_plan_names_a_model_for_every_step():
    full = shield.think_plan("full", "claude", REPO)
    assert [s["step"] for s in full] == ["trace", "history", "probe", "cause", "premortem",
                                         "approaches", "pick"]
    models = {s["step"]: s["model"] for s in full}
    assert models == {"trace": "sonnet", "history": "sonnet", "probe": "sonnet", "cause": "opus",
                      "premortem": "opus", "approaches": "opus", "pick": "opus"}
    assert {s["step"]: s["count"] for s in full}["approaches"] == 2
    assert [s["step"] for s in shield.think_plan("lite", "claude", REPO)] == ["trace", "cause", "pick"]
    assert shield.think_plan("off", "claude", REPO) == []
    with pytest.raises(shield.ShieldError, match="think size"):
        shield.think_plan("huge", "claude", REPO)


def test_design_steps_follow_the_design_tier(tmp_path):
    root = _tree(tmp_path, {}, "")
    pol = shield.policy(root)
    pol["design"] = {"tier": "low"}
    step = [s for s in shield.think_plan("full", "claude", root, pol) if s["step"] == "approaches"]
    assert step[0]["model"] == "sonnet"


def test_think_size_goes_full_for_attacks_follow_ups_and_redesigns():
    assert shield.think_size(root=REPO) == "lite"
    assert shield.think_size(adversarial=True, root=REPO) == "full"
    assert shield.think_size(follow_up=True, root=REPO) == "full"
    assert shield.think_size(redesign=True, root=REPO) == "full"


def test_the_class_check_fires_on_the_third_failure_or_when_rounds_stop_converging():
    assert not shield.class_check_due(1, [4], root=REPO)
    assert not shield.class_check_due(2, [4, 1], root=REPO)       # converging: keep fixing
    assert shield.class_check_due(2, [3, 3], root=REPO)           # no progress
    assert shield.class_check_due(2, [2, 5], root=REPO)           # worse
    assert shield.class_check_due(3, [5, 3, 1], root=REPO)        # the latest point


def test_a_class_redesigns_once_then_blocks():
    assert shield.class_action("none", 0, root=REPO) == shield.CONTINUE
    assert shield.class_action(None, 0, root=REPO) == shield.CONTINUE
    assert shield.class_action("shell text parsing", 0, root=REPO) == shield.REDESIGN
    assert shield.class_action("shell text parsing", 1, root=REPO) == shield.STOP_AT_CAP


def test_the_ladder_carries_the_think_stage_and_the_class_check():
    lad = shield.ladder("claude", REPO)
    assert lad["investigator_runs"] == "before-next-fix"
    assert lad["fix_rules"] == ["fix-the-class", "probe-impossibility-claims"]
    assert lad["class_check"] == {"after_failed_rounds": 3, "non_converging": True,
                                  "max_redesigns": 1, "model": "opus"}
    assert [s["step"] for s in lad["think"]["lite"]] == ["trace", "cause", "pick"]
    assert all(s["model"] for s in lad["think"]["full"])
    assert lad["causes"]["contract-unclear"] == "cancel-and-ask-owner"


def test_a_think_step_with_an_unknown_tier_is_refused(tmp_path):
    root = _tree(tmp_path, {}, "")
    path = tmp_path / "root" / "formations" / "shield.yaml"
    path.write_text(path.read_text().replace("trace:      {archetype: scout, tier: low}",
                                             "trace:      {archetype: scout, tier: huge}"))
    with pytest.raises(shield.ShieldError, match="think.steps.trace.tier"):
        shield.policy(str(root))



def _set(path):
    """A mutation of the policy dict at a dotted path."""
    def setter(value):
        def apply(pol):
            node = pol
            keys = path.split(".")
            for k in keys[:-1]:
                node = node[k]
            node[keys[-1]] = value
        return apply
    return setter


@pytest.mark.parametrize("mutate, needle", [                                       # t-221 a05
    (_set("escalation.class_check")(True), "class_check must be a mapping"),
    (_set("escalation.class_check.after_failed_rounds")(None), "after_failed_rounds must be"),
    (_set("escalation.class_check.max_redesigns")(-1), "max_redesigns"),
    (_set("think.steps.approaches.count")("two"), "count must be"),
    (_set("think.steps.pick.count")(-1), "count must be"),
    (_set("think.steps.trace")("low"), "must be a mapping"),
    (_set("think.default_size")("medium"), "default_size"),
    (_set("think.full_when")(["always"]), "full_when"),
    (_set("escalation.fix_rules")("fix-the-class"), "fix_rules"),
    # t-221 round 2 (investigate-r2.md): every key the loop or ladder() reads, one closed vocabulary
    (_set("escalation.causes.builder-error")("block-and-ask-owner"), "causes.builder-error"),
    (_set("escalation.causes.builder-error")("contnue"), "causes.builder-error"),
    (_set("escalation.causes")(["continue"]), "causes must be a mapping"),
    (_set("think.steps.siblings")({"archetype": "scout", "tier": "low"}), "unknown keys siblings"),
    (_set("think.lite")(["trace", "trace", "pick"]), "twice"),
    (_set("think.steps.pick.count")(2), "must be 1"),
    (_set("think.steps.probe.when")("always"), "when"),
    (_set("think.full_when")(["adversarial-lens", "redesign"]), "full_when"),
    (_set("design")("opus"), "design must be a mapping"),
    (_set("escalation.promote.ceiling")("ultra"), "promote.ceiling"),
    (_set("escalation.investigator.runs")("parallel-with-next-round"), "investigator.runs"),
    (_set("verifier.recheck")("all-lenses"), "recheck"),
    (_set("escalation.at_cap")("keep-going"), "at_cap"),
    (_set("escalation.class_check.non_convergng")(True), "unknown keys"),
    (_set("verifier.paired_verifiers")(["mid", "ultra"]), "paired_verifiers"),
])
def test_a_policy_the_loop_would_crash_on_is_refused(tmp_path, mutate, needle):
    import yaml
    root = _tree(tmp_path, "", "")
    path = tmp_path / "root" / "formations" / "shield.yaml"
    pol = yaml.safe_load(path.read_text())
    mutate(pol)
    path.write_text(yaml.safe_dump(pol))
    with pytest.raises(shield.ShieldError, match=needle):
        shield.policy(str(root))



def test_missing_required_blocks_are_refused(tmp_path):
    import yaml
    for path_ in ("escalation.investigator", "verifier.confirmer", "design"):
        root = _tree(tmp_path / path_.replace(".", "-"), "", "")
        f = tmp_path / path_.replace(".", "-") / "root" / "formations" / "shield.yaml"
        pol = yaml.safe_load(f.read_text())
        node = pol
        keys = path_.split(".")
        for k in keys[:-1]:
            node = node[k]
        del node[keys[-1]]
        f.write_text(yaml.safe_dump(pol))
        with pytest.raises(shield.ShieldError, match="is missing"):
            shield.policy(str(root))


def test_the_ladder_hands_the_loop_decisions():
    lad = shield.ladder("claude", REPO)
    assert lad["think"]["size_for"] == {"0,0": "lite", "0,1": "full", "1,0": "full", "1,1": "full"}
    assert lad["think"]["redesign_size"] == "full" == shield.think_size(redesign=True, root=REPO)
    groups = [[s["label"] for s in g] for g in lad["think"]["groups"]["full"]]
    assert groups == [["trace", "history", "probe"], ["cause"],
                      ["premortem", "approaches0", "approaches1"], ["pick"]]
    assert lad["think"]["groups"]["off"] == []
    assert lad["models"]["design"]["judgment_lens"] == lad["design"] == "opus"
    assert lad["models"]["design"]["fixer"] == "opus" and lad["models"]["build"]["fixer"] is None
    assert lad["models"]["build"]["judgment_lens"] == "sonnet"
    assert lad["promote_next"] == {t: shield.promote(t, REPO) for t in ("low", "mid", "high", "top")}
    assert lad["max_redesigns"] == shield.max_redesigns(REPO) == 1


def test_promote_without_a_ceiling_uses_the_last_tier(tmp_path):
    import yaml
    root = _tree(tmp_path, "", "")
    f = tmp_path / "root" / "formations" / "shield.yaml"
    pol = yaml.safe_load(f.read_text())
    del pol["escalation"]["promote"]["ceiling"]
    f.write_text(yaml.safe_dump(pol))
    assert shield.promote("high", str(root)) == "top"
    assert shield.ladder("claude", str(root))["promote_next"]["high"] == "top"


def test_no_redesign_left_means_the_cap_action(tmp_path):
    import yaml
    root = _tree(tmp_path, "", "")
    f = tmp_path / "root" / "formations" / "shield.yaml"
    pol = yaml.safe_load(f.read_text())
    pol["escalation"]["class_check"]["max_redesigns"] = 0
    f.write_text(yaml.safe_dump(pol))
    assert shield.max_redesigns(str(root)) == 0
    assert shield.class_action("x", 0, root=str(root)) == shield.STOP_AT_CAP
    assert shield.next_step(1, False, "wrong-direction", root=str(root)) == shield.STOP_AT_CAP


def _key_paths(node, prefix=()):
    """Every mapping key path in the policy, depth first."""
    if isinstance(node, dict):
        for k, v in node.items():
            yield prefix + (k,)
            yield from _key_paths(v, prefix + (k,))


def test_every_misspelt_key_at_every_level_is_refused(tmp_path):          # t-221 investigate-r3
    import yaml
    base = yaml.safe_load(open(os.path.join(REPO, "formations", "shield.yaml")).read())
    root = _tree(tmp_path, "", "")
    f = tmp_path / "root" / "formations" / "shield.yaml"
    checked = 0
    for path_ in _key_paths(base):
        if path_[:1] == ("runtimes",) and len(path_) == 2:
            continue                      # a runtime name is free: a new name is a new runtime
        pol = yaml.safe_load(yaml.safe_dump(base))
        node = pol
        for k in path_[:-1]:
            node = node[k]
        node[str(path_[-1]) + "x"] = node.pop(path_[-1])
        f.write_text(yaml.safe_dump(pol))
        with pytest.raises(shield.ShieldError):
            shield.policy(str(root))
        checked += 1
    assert checked > 60


@pytest.mark.parametrize("drop", [
    "tiers", "runtimes", "design", "think", "escalation", "verifier",
    "think.steps", "think.lite", "think.steps.pick", "think.steps.probe",
    "escalation.round_cap", "escalation.investigator", "escalation.causes",
    "escalation.causes.wrong-direction", "escalation.fix_rules", "escalation.class_check",
    "verifier.confirmer", "verifier.judgment_lens_tier", "verifier.adversarial_lens_tier",
])
def test_every_required_key_is_required(tmp_path, drop):
    import yaml
    root = _tree(tmp_path, "", "")
    f = tmp_path / "root" / "formations" / "shield.yaml"
    pol = yaml.safe_load(f.read_text())
    node = pol
    keys = drop.split(".")
    for k in keys[:-1]:
        node = node[k]
    del node[keys[-1]]
    f.write_text(yaml.safe_dump(pol))
    with pytest.raises(shield.ShieldError):
        shield.policy(str(root))


@pytest.mark.parametrize("mutate, needle", [                                  # investigate-r3
    (_set("think.steps.pick.when")("task-names-probe"), "pick.when"),
    (_set("think.steps.probe.when")(None), "probe.when"),
    (_set("think.steps.approaches.count")(3), "must be 2"),
    (_set("think.lite")([]), "no pick step"),
    (_set("escalation.class_check.after_failed_rounds")(5), "below round_cap"),
    (_set("tiers")(["low", "mid", "high", "top", "session"]), "reserved tier"),
    (_set("escalation.investigator.report")(["cause", "recommend"]), "report"),
    (_set("escalation.investigator.reports_to")("fixer"), "reports_to"),
    (_set("verifier.confirmer.blind")(False), "blind"),
    (_set("verifier.every_round")(["lint"]), "every_round"),
    (_set("verifier.minor_findings")("every-round"), "minor_findings"),
    (_set("verifier.attack_freeze_round")(9), "attack_freeze_round"),
    (_set("runtimes.claude.low")(["sonnet"]), "must name a model"),
    (_set("think.steps.approaches.archetype")("design"), "no brief"),
    (_set("escalation.fix_rules")([]), "lacks fix-the-class"),
    (_set("version")(2), "version"),
    (_set("version")(True), "version"),                                  # r4: type-strict
    (_set("verifier.every_round")([]), "every_round"),                   # r4 B2
    (_set("verifier.confirmer.blind")(1), "blind"),
    (_set("_normalized")("yes"), "_normalized"),
    (_set("escalation.investigator.report")([]), "lacks cause"),                     # r5 B1
    (_set("escalation.investigator.report")(["file"]), "lacks cause"),
    (_set("escalation.investigator.report")(["alternative", "disputed"]), "lacks cause"),
    (_set("escalation.fix_rules")(["fix-the-class"]), "lacks probe-impossibility-claims"),
])
def test_values_the_loop_cannot_use_are_refused(tmp_path, mutate, needle):
    import yaml
    root = _tree(tmp_path, "", "")
    f = tmp_path / "root" / "formations" / "shield.yaml"
    pol = yaml.safe_load(f.read_text())
    mutate(pol)
    f.write_text(yaml.safe_dump(pol))
    with pytest.raises(shield.ShieldError, match=needle):
        shield.policy(str(root))


def test_a_design_loop_confirms_on_the_design_model():
    lad = shield.ladder("claude", REPO)
    assert lad["models"]["design"]["confirmer"] == lad["design"]



@pytest.mark.parametrize("cap", [1, 2, 3])                                   # r4 B1
def test_a_formation_cap_that_would_switch_off_the_class_check_is_refused(tmp_path, cap):
    roles = "  - id: b\n    archetype: eng"
    root = _tree(tmp_path, roles, "escalation:\n  round_cap: %d\n" % cap)
    with pytest.raises(shield.ShieldError, match="class check could never run"):
        shield.round_cap(manifest.registry(root).get("probe"), root)


def test_a_formation_cap_of_true_is_refused(tmp_path):
    roles = "  - id: b\n    archetype: eng"
    root = _tree(tmp_path, roles, "escalation:\n  round_cap: true\n")
    with pytest.raises(shield.ShieldError, match="positive integer"):
        shield.round_cap(manifest.registry(root).get("probe"), root)



@pytest.mark.parametrize("block", ["escalation:\n  roundcap: 4\n", "escalation:\n  round_cap: 4\n  max_redesigns: 0\n", "escalation: 3\n"])
def test_a_formation_escalation_block_holds_only_its_cap(tmp_path, block):   # r5 follow-up
    roles = "  - id: b\n    archetype: eng"
    root = _tree(tmp_path, roles, block)
    with pytest.raises(shield.ShieldError, match="may hold only round_cap"):
        shield.round_cap(manifest.registry(root).get("probe"), root)


# ------------------------------------------------ strict escalation block (op-013 t-245)
_ROLES = "  - id: b\n    archetype: eng"


@pytest.mark.parametrize("block", ["escalation:\n  round_cap: null\n",
                                   "escalation:\n  round_cap:\n",
                                   "escalation:\n  round_cap: ~\n"])
def test_a_null_round_cap_is_refused(tmp_path, block):
    root = _tree(tmp_path, _ROLES, block)
    with pytest.raises(shield.ShieldError, match=r"positive integer \(got None\)"):
        shield.round_cap(manifest.registry(root).get("probe"), root)


@pytest.mark.parametrize("block", ["escalation:\n", "escalation: null\n", "escalation: {}\n"])
def test_an_empty_escalation_block_is_refused(tmp_path, block):
    root = _tree(tmp_path, _ROLES, block)
    with pytest.raises(shield.ShieldError, match="may hold only round_cap"):
        shield.round_cap(manifest.registry(root).get("probe"), root)


@pytest.mark.parametrize("key", ["escalations", "Escalation"])
def test_a_misspelt_escalation_key_is_refused_not_ignored(tmp_path, key):
    root = _tree(tmp_path, _ROLES, "%s:\n  round_cap: 4\n" % key)
    with pytest.raises(manifest.ManifestError, match="unknown top-level keys"):
        manifest.registry(root)
    with pytest.raises(manifest.ManifestError, match="unknown top-level keys"):
        shield.resolve_all("claude", root)


def test_an_absent_escalation_keeps_the_policy_cap(tmp_path):
    root = _tree(tmp_path, _ROLES, "")
    m = manifest.registry(root).get("probe")
    assert shield.round_cap(m, root) == 5
    assert shield.next_step(5, False, m=m, root=root) == shield.STOP_AT_CAP
