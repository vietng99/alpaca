"""The footprint question, the mode derived from the answers and the trace lines of the setup card (op-062 t-691).

The signed brief says the first chat asks whether Alpaca files may live in the repo, then proposes the mode, the
configuration and the first steps; the owner's words are "the product will not have trace of alpaca". So:

  * the footprint question is asked, with a recommended no, unless the repo already answers it;
  * the card states the mode with its reason from the answers, and an owner who allows Alpaca files in the repo on a solo
    take-over is told plainly that the in-place mode is not built in this version (the card still proposes attach);
  * the card always shows the commit identity, the pull request footer and the branch prefix with their values, defaults
    included, and an owner who wants no trace gets a neutral set as a proposal; what the card shows is what apply writes.
"""
import copy
import re

import pytest

from alpaca import onboard_card as oc
from alpaca.attach import DEFAULT_IDENTITY, config
from alpaca.tests import onboardkit as kit


def _fx(name):
    return kit.BUILDERS[name]()


def _card(fx):
    return kit.render(fx)


def _flat(text):
    return " ".join(text.split())


def _part(card, num):
    m = re.search(r"^%d\. [A-Z].*?(?=^\d+\. [A-Z]|^Sources \(|^Reply \"yes\")" % num, card, re.S | re.M)
    return m.group(0)


def _with(fx, **answers):
    fx = copy.deepcopy(fx)
    for slot, entry in answers.items():
        fx["answers"][slot.replace("_", "-")] = entry
    return fx


def _trace_bullets(card):
    return [ln for ln in _part(card, 6).split("\n") if ln.startswith("- ")]


# ---- the mode follows the answers ------------------------------------------------------------------------------------------

def test_an_existing_repo_is_attached_whatever_the_footprint_and_the_committers_answers():
    base = _fx("qtrm64-take-over")
    for foot in (None, "a", "b"):
        for others in ("a", "b", "c"):
            fx = copy.deepcopy(base)
            fx["answers"]["others-commit"] = dict(fx["answers"]["others-commit"], option=others)
            if foot:
                fx["answers"]["files-in-repo"] = kit.own(None, foot, quote="2%s" % foot, ev=4)
            assert oc.settings(fx["look"], fx["answers"])["mode"] == "attach", (foot, others)
    assert oc.settings(None, _fx("start-empty")["answers"])["mode"] == "native"
    assert oc.settings(_fx("migrate-new-repo")["look"], _fx("migrate-new-repo")["answers"])["mode"] == "native"


def test_the_card_says_attach_because_the_owner_wants_no_trace():
    card = _card(_fx("qtrm64-no-trace"))
    assert "3. WHERE ALPACA SITS: attach mode" in card
    assert "Why attach: you want no trace of Alpaca in your repo." in _flat(card)
    assert "Alpaca in your repo: no files and no trace of it in commits, branches or pull requests." in _flat(card)


def test_the_card_says_attach_because_others_push_and_names_both_reasons_when_both_hold():
    team = _flat(_card(_fx("team-repo")))
    assert "Why attach: other people work in your repo, and it stays free of Alpaca files." in team
    both = _flat(_card(_fx("team-no-trace")))
    assert "Why attach: you want no trace of Alpaca in your repo, and other people push to it." in both
    fx = _fx("qtrm64-no-trace")
    fx["answers"]["others-commit"] = dict(fx["answers"]["others-commit"], option="b")
    assert "Why attach: you want no trace of Alpaca in your repo, and other people push to it." in _flat(_card(fx))


def test_files_allowed_on_a_solo_take_over_is_attach_and_the_card_says_the_in_place_mode_is_not_built():
    card = _flat(_card(_fx("qtrm64-files-ok")))
    assert "3. WHERE ALPACA SITS: attach mode" in card
    assert ("Why attach: you said Alpaca files may live in it and nobody else pushes. The in-place mode, which would put "
            "Alpaca files inside your repo, is not built in this version, so I propose attach.") in card
    assert "Alpaca in your repo: you said Alpaca files may live in it." in card
    # no fake native path: the card never offers a native studio for an existing repo
    assert "native" not in card.lower() and "app/" in card


def test_files_allowed_while_others_push_names_the_other_reason_first():
    fx = _fx("qtrm64-files-ok")
    fx["answers"]["others-commit"] = dict(fx["answers"]["others-commit"], option="c")
    card = _flat(_card(fx))
    assert ("Why attach: other people push to your repo. You said Alpaca files may live in it, but the in-place mode, which "
            "would put Alpaca files inside your repo, is not built in this version, so I propose attach.") in card


def test_an_audit_and_a_move_keep_their_own_reason_and_add_the_in_place_note_when_files_are_allowed():
    for name, reason in (("audit", "you only want a read of the code"), ("migrate-new-repo", None)):
        fx = _fx(name)
        fx["answers"]["files-in-repo"] = kit.own(None, "b", quote="2b", ev=30)
        st = oc.settings(fx["look"], fx["answers"])
        if st["mode"] == "attach":
            text = _flat(_card(fx))
            assert reason in text and "in-place mode" in text and "not built in this version" in text


def test_a_repo_that_already_holds_alpaca_files_has_answered_the_footprint_question():
    fx = _fx("qtrm64-take-over")
    fx["look"]["around"]["alpaca_files"] = ["ALPACA-MANIFEST", ".alpaca/alpaca.db"]
    fx["look"]["sources"] = kit.onboard_look.sources(fx["look"])
    card = _flat(_card(fx))
    assert oc.footprint(fx["look"], fx["answers"])["source"] == "sensed"
    assert "Alpaca in your repo: it already holds Alpaca files (ALPACA-MANIFEST, .alpaca/alpaca.db) [6]" in card
    assert "Why attach: your repo already holds Alpaca files [6] and nobody else pushes. The in-place mode" in card
    assert "ALPACA-MANIFEST" in card.split("Sources (")[1]
    assert oc.lint(oc.render_marked(fx["look"], fx["answers"], date=fx["date"], name=fx["name"], people=fx["people"]),
                   fx["look"], fx["answers"]) == []


# ---- a move into the same repo asks the same two questions as a take-over ------------------------------------------------------

def _migrate_same_repo(footprint=None, others=None):
    """The migrate fixture with the owner's answer to the target set to "a" (the same repo) and nothing else asked yet."""
    fx = _fx("migrate-new-repo")
    fx["answers"]["migrate-target"] = kit.own(None, "a", quote="1a", ev=3)
    if footprint:
        fx["answers"]["files-in-repo"] = kit.own(None, footprint, quote="3%s" % footprint, ev=30)
    if others:
        fx["answers"]["others-commit"] = kit.own(None, others, quote="2%s" % others, ev=31)
    return fx


def _asked(fx, answers):
    return [q["slot"] for q in oc.next_round(fx["look"], answers)]


def test_a_move_into_the_same_repo_requires_the_two_footprint_answers_and_a_new_repo_does_not():
    """Round 1 is built from the opening alone (no target yet), so the two questions are required slots of the next round;
    alpaca/tests/test_onboard_migrate_flow.py drives this through the real verbs."""
    fx = _fx("migrate-new-repo")
    opening = {"opening": fx["answers"]["opening"]}
    assert oc.required_slots(fx["look"], "migrate", opening) == ["goal", "migrate-target"]
    same = dict(opening, **{"migrate-target": kit.own(None, "a", quote="1a", ev=3)})
    assert oc.required_slots(fx["look"], "migrate", same) == ["goal", "migrate-target", "others-commit", "files-in-repo",
                                                              "ci"]
    assert _asked(fx, same) == ["goal", "others-commit", "files-in-repo", "ci"]      # required slots come before keep-working
    new = dict(opening, **{"migrate-target": kit.own(None, "b", quote="1b", ev=3)})
    assert oc.required_slots(fx["look"], "migrate", new) == ["goal", "migrate-target"]
    assert _asked(fx, new) == ["goal", "keep-working", "limits"]
    assert oc.settings(fx["look"], fx["answers"])["mode"] == "native"
    # a slot the look cannot open is not required, so it never blocks ready
    no_authors = copy.deepcopy(fx["look"])
    no_authors["history"]["recent_authors"] = []
    assert oc.required_slots(no_authors, "migrate", same) == ["goal", "migrate-target", "files-in-repo", "ci"]


def test_a_move_into_the_same_repo_with_no_files_gets_the_trace_free_set_like_a_take_over():
    fx = _migrate_same_repo(footprint="a", others="a")
    st = oc.settings(fx["look"], fx["answers"])
    assert st["situation"] == "migrate" and st["mode"] == "attach"
    assert st["footprint"]["value"] == "none" and st["footprint"]["explicit"] is True
    assert st["trace"]["free"] is True and st["trace"]["proposed"] is True
    assert (st["commit_name"], st["commit_email"]) == oc.NEUTRAL_IDENTITY
    assert st["pr_says_agent"] is False and st["branch_prefix"] == oc.NEUTRAL_PREFIX == "fix/"
    card = _card(fx)
    assert _trace_bullets(card) == [
        "- Commit identity: Maintenance bot <maintenance@localhost> (proposed)",
        "- Pull request footer: off, the body names no tool (proposed)",
        "- Branch prefix: fix/ (proposed)"]
    assert "Others who push: nobody now" in _flat(card)
    assert oc.lint(oc.render_marked(fx["look"], fx["answers"], date=fx["date"], name=fx["name"], people=fx["people"]),
                   fx["look"], fx["answers"]) == []


def test_a_move_into_the_same_repo_that_allows_files_keeps_the_defaults():
    fx = _migrate_same_repo(footprint="b", others="a")
    st = oc.settings(fx["look"], fx["answers"])
    assert st["mode"] == "attach" and st["trace"]["free"] is False
    assert st["trace"]["identity"] == tuple(DEFAULT_IDENTITY) and st["pr_says_agent"] is True
    assert st["branch_prefix"] == oc.DEFAULT_PREFIX


def test_others_push_for_a_move_follows_the_committers_answer():
    for others, want in (("a", False), ("b", True), ("c", True)):
        fx = _migrate_same_repo(footprint="a", others=others)
        assert oc.settings(fx["look"], fx["answers"])["others_push"] is want, others
    assert oc.others_push("migrate", {}) is False


# ---- the trace lines -------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("name", ["qtrm64-take-over", "team-repo", "audit", "qtrm64-no-trace", "qtrm64-files-ok",
                                  "team-no-trace", "qtrm64-gitlab"])
def test_every_attach_card_shows_the_commit_identity_the_pull_request_footer_and_the_branch_prefix(name):
    fx = _fx(name)
    card = _card(fx)
    bullets = _trace_bullets(card)
    assert [b.split(":")[0] for b in bullets] == ["- Commit identity", "- Pull request footer", "- Branch prefix"]
    st = oc.settings(fx["look"], fx["answers"])
    tr = st["trace"]
    assert bullets[0].startswith("- Commit identity: %s <%s> (" % tr["identity"])
    assert bullets[1].startswith("- Pull request footer: %s," % ("on" if tr["footer"] else "off"))
    assert bullets[2].startswith("- Branch prefix: %s (" % tr["prefix"])
    # part 10 repeats what is written, and the settings hold the same values
    ten = _flat(_part(card, 10))
    assert "branch prefix %s, commit identity %s, pull request footer %s," % (
        tr["prefix"], tr["identity"][0], "on" if tr["footer"] else "off") in ten
    assert st["branch_prefix"] == tr["prefix"] and st["pr_says_agent"] is tr["footer"]


def test_without_a_footprint_answer_the_trace_lines_are_the_defaults_and_say_so():
    card = _card(_fx("qtrm64-take-over"))
    assert _trace_bullets(card) == [
        "- Commit identity: Alpaca agent <alpaca-agent@localhost> (my default; it names Alpaca)",
        "- Pull request footer: on, the body says it was made with an agent assistant (my default)",
        "- Branch prefix: alpaca/ (my default; it names Alpaca)"]
    st = oc.settings(_fx("qtrm64-take-over")["look"], _fx("qtrm64-take-over")["answers"])
    # the defaults are written as no setting at all, as before (a default identity is the one attach holds anyway)
    assert (st["commit_name"], st["commit_email"]) == (None, None) and st["pr_says_agent"] is True
    assert tuple(DEFAULT_IDENTITY) == ("Alpaca agent", "alpaca-agent@localhost")


def test_an_owner_who_wants_no_trace_gets_a_neutral_set_as_a_proposal_the_card_shows():
    fx = _fx("qtrm64-no-trace")
    card = _card(fx)
    assert _trace_bullets(card) == [
        "- Commit identity: Maintenance bot <maintenance@localhost> (proposed)",
        "- Pull request footer: off, the body names no tool (proposed)",
        "- Branch prefix: fix/ (proposed)"]
    assert "You want no trace of Alpaca, so I propose this set; tell me any line to change:" in _flat(card)
    st = oc.settings(fx["look"], fx["answers"])
    assert (st["commit_name"], st["commit_email"]) == oc.NEUTRAL_IDENTITY and st["pr_says_agent"] is False
    assert st["branch_prefix"] == "fix/" == oc.NEUTRAL_PREFIX
    ten = _flat(_part(card, 10))
    assert "branch prefix fix/, commit identity Maintenance bot, pull request footer off," in ten
    # the neutral identity is one the attach block accepts
    assert config._identity_errors({"commit_name": oc.NEUTRAL_IDENTITY[0], "commit_email": oc.NEUTRAL_IDENTITY[1]}) == []
    assert re.fullmatch(r"[a-z][a-z0-9-]{2,30}/", oc.NEUTRAL_PREFIX)
    # no word of the neutral set names the tool
    assert "alpaca" not in " ".join(oc.NEUTRAL_IDENTITY).lower() and "alpaca" not in oc.NEUTRAL_PREFIX


def test_what_the_owner_answered_wins_over_the_proposal_and_the_repos_own_rule_over_the_neutral_prefix():
    fx = _fx("team-no-trace")
    card = _card(fx)
    assert _trace_bullets(card) == [
        "- Commit identity: Shop Bot <bot@shop.example> (you chose it)",
        "- Pull request footer: on, the body says it was made with an agent assistant (you chose it)",
        "- Branch prefix: feature/ (your repo's own rule) [6]"]
    assert "propose this set" not in _flat(card) and "Your repo will show this of me; tell me any line to change:" in _flat(card)
    # the owner names a prefix: it wins over the repo's rule and over the neutral one
    fx["answers"]["branch-prefix"] = kit.own("work/<ticket>", quote="work/<ticket>", ev=50)
    assert "- Branch prefix: work/ (you chose it)" in _card(fx)
    # an answer the agent read from a reply is marked as the agent's reading
    fx["answers"]["commit-identity"] = kit.agent("Shop Bot <bot@shop.example>", ev=51)
    assert "(you chose it (my reading of your reply))" in _card(fx)


def test_a_footprint_yes_or_no_answer_the_owner_gave_by_the_agents_reading_still_counts():
    fx = _fx("qtrm64-no-trace")
    fx["answers"]["files-in-repo"] = kit.agent(None, ev=60, option="a")
    card = _flat(_card(fx))
    assert "no trace of it in commits, branches or pull requests. (my reading of your reply)" in card
    assert oc.settings(fx["look"], fx["answers"])["trace"]["free"] is True


def test_a_footprint_answer_with_the_default_source_is_not_an_ask_for_no_trace():
    fx = _fx("qtrm64-no-trace")
    fx["answers"]["files-in-repo"] = {"option": "a", "source": "default", "value": None, "event": 61}
    st = oc.settings(fx["look"], fx["answers"])
    assert st["trace"]["free"] is False and st["branch_prefix"] == "alpaca/"


def test_a_repo_off_github_still_shows_the_footer_and_says_it_is_not_used():
    fx = _fx("qtrm64-gitlab")
    flat = _flat(_card(fx))
    assert ("- Pull request footer: on, the body says it was made with an agent assistant (my default). It is not used "
            "here: I send no pull request for this repo") in flat
    assert "GitHub" not in flat.replace("This repo is not on GitHub", "")


def test_the_audit_card_shows_the_set_for_later_and_native_says_nothing_applies():
    audit = _flat(_card(_fx("audit")))
    assert "Your repo shows nothing now. If you let changes leave later, it shows this; tell me any line to change:" in audit
    assert [b.split(":")[0] for b in _trace_bullets(_card(_fx("audit")))] == [
        "- Commit identity", "- Pull request footer", "- Branch prefix"]
    native = _flat(_card(_fx("start-empty")))
    assert ("No repo of yours is attached, so there is no commit identity, pull request footer or branch prefix to set."
            in native)
    st = oc.settings(None, _fx("start-empty")["answers"])
    assert st["branch_prefix"] is None and st["commit_name"] is None


def test_a_card_whose_trace_set_is_proposed_still_passes_the_lint_with_every_gloss_once():
    for name in ("qtrm64-no-trace", "qtrm64-files-ok", "team-no-trace"):
        fx = _fx(name)
        marked = oc.render_marked(fx["look"], fx["answers"], date=fx["date"], name=fx["name"], people=fx["people"])
        assert oc.lint(marked, fx["look"], fx["answers"]) == [], name


def test_clause_quoting_skips_every_option_token_of_a_reply():
    reply = "1a, 2a, bach was only testing uart stuff. I want a clean state"
    assert oc._clause_of(reply, "a") == "bach was only testing uart stuff"
    assert oc._clause_of("1a. 2a. bach was only testing", "a") == ""
    assert oc._clause_of("1a and then my goal", "a") == "and then my goal"
    assert oc._clause_of("1a, an old project", "a") == "an old project"
