"""The situation guess, the questions and the level (op-062 t-656): pure rules, so they are pinned row by row."""
import copy

import pytest

from alpaca import onboard_card as oc
from alpaca.tests import onboardkit as kit

OPEN = "hi, I took over this firmware project from my old team https://github.com/example-org/QTRM64 . not sure where to start"


def look(**over):
    fx = kit.qtrm64()["look"]
    fx.update(over)
    return fx


def hist(**over):
    h = copy.deepcopy(kit.qtrm64()["look"]["history"])
    h.update(over)
    return h


# ---- the guess (design 6.1)

def test_the_qtrm64_shape_is_take_over_from_the_owner_words_and_from_the_facts_alone():
    words = oc.situation_guess(look(), [OPEN])
    assert words["situation"] == "take-over" and words["rule"] == 2
    assert words["reasons"][0]["text"] == 'you said "I took over this firmware project from my old team"'
    assert words["reasons"][1] == {"text": "main has not changed for 204 days", "source": 4}
    facts = oc.situation_guess(look(), ["hi, here is the repo"])
    assert facts["situation"] == "take-over" and facts["rule"] == 3 and facts["reasons"][0]["source"] == 4


def test_two_recent_authors_give_team_and_so_do_ci_files_with_a_codeowners_file():
    recent = [{"name": "A", "last": "2026-10-01"}, {"name": "B", "last": "2026-09-30"}]
    g = oc.situation_guess(look(history=hist(recent_authors=recent, days_still=3)), ["hi"])
    assert g["situation"] == "team" and g["rule"] == 3
    around = dict(look()["around"], ci=[".github/workflows/ci.yml"], codeowners=["CODEOWNERS"])
    g = oc.situation_guess(look(around=around, history=hist(recent_authors=[], days_still=3)), ["hi"])
    assert g["situation"] == "team" and g["reasons"][0]["source"] == 6


def test_a_base_that_moved_recently_with_one_author_is_the_safer_team_guess():
    g = oc.situation_guess(look(history=hist(days_still=5, recent_authors=[{"name": "A", "last": "2026-10-09"}])), ["hi"])
    assert g["situation"] == "team"


@pytest.mark.parametrize("said,expected", [
    (["I want to audit this repo"], "audit"), (["just read it and tell me how it works"], "audit"),
    (["please assess the code"], "audit"), (["we will rewrite it in Rust"], "migrate"),
    (["port this to Python"], "migrate"), (["our team reviews every change"], "team"),
    (["my team and I work on it"], "team"), (["the repo was inherited from a colleague"], "take-over"),
    (["it is mine now"], "take-over"), (["handed over to me last week"], "take-over"),
    (["it is a new project"], "start-empty"), (["start empty please"], "start-empty"),
    (["no code yet"], "start-empty")])
def test_the_word_map_fires_on_whole_words_and_in_the_stated_order(said, expected):
    assert oc.situation_guess(look(), said)["situation"] == expected


def test_audit_words_beat_take_over_words_because_the_map_is_in_order():
    assert oc.situation_guess(look(), ["I took over this and want to audit it"])["situation"] == "audit"


def test_a_word_inside_another_word_does_not_fire():
    assert oc.situation_guess(look(), ["the airport is mine"])["situation"] != "migrate"       # "port" in "airport"
    assert oc.situation_guess(look(), ["this is unaudited"])["situation"] != "audit"


def test_no_repository_is_always_start_empty():
    assert oc.situation_guess(None, ["whatever"])["situation"] == "start-empty"
    assert oc.situation_guess(None, [])["rule"] == 1


def test_a_branch_named_like_the_owner_changes_nothing_and_so_does_the_host_git_name():
    base = oc.situation_guess(look(), ["hi"])
    tips = [{"ref": "origin/alpaca-development", "author": "Alpaca Development", "date": "2026-09-11"}]
    h = hist(tips=tips)
    other = oc.situation_guess(look(history=h, host={"tools": {}, "wanted": [], "git_user": "nguyenbach03022003"}), ["hi"])
    assert other["situation"] == base["situation"] and other["rule"] == base["rule"]


def test_only_owner_quoted_text_counts_as_said():
    answers = {"opening": {"value": "hello", "source": "owner", "quote": "hello"},
               "goal": {"value": "audit everything", "source": "agent", "quote": None}}
    assert oc.situation_of(look(history=hist(days_still=3)), answers)[0] == "team"
    answers["goal"] = {"value": "audit everything", "source": "owner", "quote": "audit everything"}
    assert oc.situation_of(look(), answers)[0] == "audit"


def test_an_owner_override_beats_the_guess_and_an_agent_one_needs_its_reason():
    answers = {"opening": {"value": OPEN, "source": "owner", "quote": OPEN},
               "situation": {"option": "team", "source": "owner", "quote": "no, it is a team repo"}}
    s, g = oc.situation_of(look(), answers)
    assert s == "team" and g["rule"] == 0 and g["reasons"][0]["text"] == 'you told me: "no, it is a team repo"'
    answers["situation"] = {"option": "audit", "source": "agent", "reason": "my reading of: just look at it"}
    s, g = oc.situation_of(look(), answers)
    assert s == "audit" and g["reasons"][0]["text"] == "my reading of: just look at it"


# ---- the questions (design 6.3)

def _round(situation_look, answers=None):
    return oc.next_round(situation_look, answers or {"opening": {"value": OPEN, "source": "owner", "quote": OPEN}})


def test_round_one_has_at_most_four_questions_each_with_a_why_and_one_recommended_option():
    for name in sorted(kit.BUILDERS):
        fx = kit.BUILDERS[name]()
        answers = {"opening": fx["answers"]["opening"]}
        qs = oc.next_round(fx["look"], answers)
        assert 1 <= len(qs) <= oc.MAX_QUESTIONS, name
        assert [q["num"] for q in qs] == list(range(1, len(qs) + 1))
        for q in qs:
            assert q["why"] and q["text"]
            if q["options"]:
                assert 2 <= len(q["options"]) <= 4
                assert sum(1 for o in q["options"] if o["recommended"]) == 1 and q["recommended"]
            else:
                assert q["recommended"] is None


def test_the_qtrm64_round_matches_the_design_with_the_recommendation_and_its_reason():
    qs = _round(look())
    assert [q["slot"] for q in qs] == ["others-commit", "files-in-repo", "goal", "limits"]
    first = qs[0]
    assert first["recommended"] == "a" and first["options"][0]["note"] == "main has not moved for 204 days"
    assert first["options"][1]["text"] == "Yes, on their own branches (nguyenbach03022003, last 2026-09-11)"
    lines = oc.format_round(qs, 1)
    assert lines[0] == "round 1:"
    assert " 1. [others-commit] Does anyone else still push to this repo?" in lines
    assert "    a) No, only me now (recommended: main has not moved for 204 days)" in lines
    assert "    why: it decides whether each change is checked against their newest work, and who reviews" in lines


def test_a_slot_the_look_answered_is_not_asked():
    quiet = look(history=hist(recent_authors=[]))
    assert "others-commit" not in [q["slot"] for q in _round(quiet)]
    team_look = kit.team()["look"]                      # CONTRIBUTING.md names feature/
    ans = {"opening": {"value": "we review pull requests", "source": "owner", "quote": "we review pull requests"}}
    slots = [q["slot"] for q in oc.next_round(team_look, ans)]
    assert "branch-prefix" not in slots and slots[:3] == ["goal", "merges", "files-in-repo"]
    no_rule = copy.deepcopy(team_look)
    no_rule["around"]["contributing_branch_prefix"] = None
    # the round holds four questions: the footprint question comes before the prefix, which the card can propose
    assert [q["slot"] for q in oc.next_round(no_rule, ans)] == ["goal", "merges", "files-in-repo", "limits"]
    done = dict(ans, goal={"value": "x", "source": "owner"}, limits={"value": "none", "source": "owner"})
    assert [q["slot"] for q in oc.next_round(no_rule, done)][-1:] == ["branch-prefix"]


def test_the_footprint_question_is_asked_for_a_take_over_and_a_team_repo_with_a_recommended_no():
    """The brief says the first chat asks whether Alpaca files may live in the repo (op-062 t-691)."""
    for name in ("qtrm64-take-over", "team-repo"):
        fx = kit.BUILDERS[name]()
        qs = oc.next_round(fx["look"], {"opening": fx["answers"]["opening"]})
        q = next(q for q in qs if q["slot"] == "files-in-repo")
        assert q["text"] == "May Alpaca files live in your repo?"
        assert [o["key"] for o in q["options"]] == ["a", "b"] and q["recommended"] == "a"
        assert q["options"][0]["text"].startswith("No: nothing of Alpaca in your repo") and q["options"][0]["recommended"]
        assert q["options"][1]["text"] == "Yes, Alpaca files may live in the repo"
        assert "what your commits, branches and pull requests say" in q["why"]
    # an empty project, an audit and a move to a new repo have no repo of the owner's to keep clean
    for name in ("start-empty", "audit", "migrate-new-repo"):
        fx = kit.BUILDERS[name]()
        said = {"opening": fx["answers"]["opening"]}
        if name == "migrate-new-repo":
            said["migrate-target"] = fx["answers"]["migrate-target"]          # the owner chose a new repo
        assert "files-in-repo" not in [q["slot"] for q in oc.next_round(fx["look"], said)]



def test_the_footprint_question_is_skipped_when_it_is_answered_or_the_repo_already_answers_it():
    fx = kit.qtrm64()
    opening = {"opening": fx["answers"]["opening"]}
    answered = dict(opening, **{"files-in-repo": {"option": "a", "source": "owner", "quote": "2a"}})
    assert "files-in-repo" not in [q["slot"] for q in oc.next_round(fx["look"], answered)]
    has = copy.deepcopy(fx["look"])
    has["around"]["alpaca_files"] = ["ALPACA-MANIFEST", ".alpaca/alpaca.db"]
    assert "files-in-repo" not in [q["slot"] for q in oc.next_round(has, opening)]
    assert oc.footprint(has, opening) == {"value": "files", "source": "sensed", "explicit": True,
                                          "found": ["ALPACA-MANIFEST", ".alpaca/alpaca.db"]}
    assert oc.footprint(fx["look"], opening)["value"] == "unasked"
    # the owner's own answer wins over what the repo shows
    assert oc.footprint(has, answered)["value"] == "none" and oc.footprint(has, answered)["source"] == "owner"


def test_an_answered_slot_is_skipped():
    answers = {"opening": {"value": OPEN, "source": "owner", "quote": OPEN}, "goal": {"value": "x", "source": "owner"}}
    assert "goal" not in [q["slot"] for q in oc.next_round(look(), answers)]


def test_the_questions_follow_the_situation():
    fx = kit.start_empty()
    assert [q["slot"] for q in oc.next_round(None, {"opening": fx["answers"]["opening"]})] == ["goal", "stack", "limits"]
    fx = kit.migrate_new()
    assert [q["slot"] for q in oc.next_round(fx["look"], {"opening": fx["answers"]["opening"]})] == [
        "migrate-target", "goal", "keep-working", "limits"]
    fx = kit.audit()
    qs = oc.next_round(fx["look"], {"opening": fx["answers"]["opening"]})
    assert [q["slot"] for q in qs] == ["goal", "audit-reader", "limits"]
    assert qs[0]["text"].startswith("What do you want to find out")


def test_required_slots_per_situation():
    assert oc.missing_required(look(), {"opening": {"value": OPEN, "source": "owner", "quote": OPEN}}) == ["goal", "ci"]
    fx = kit.team()
    assert oc.missing_required(fx["look"], {"opening": fx["answers"]["opening"]}) == ["goal", "merges"]
    fx = kit.migrate_new()
    assert oc.missing_required(fx["look"], {"opening": fx["answers"]["opening"]}) == ["goal", "migrate-target"]
    assert oc.missing_required(fx["look"], fx["answers"]) == []


# ---- the level (design 6.4)

def _gate(kind):
    return {"name": kind, "kind": kind}


def test_level_for_row_by_row():
    assert oc.level_for("start-empty", [], {}) == ("L3", "nothing exists to break, and no change leaves the studio")
    assert oc.level_for("take-over", [_gate("build")], {})[0] == "L3"
    assert oc.level_for("take-over", [_gate("test")], {"others-commit": {"option": "a"}})[0] == "L3"
    assert oc.level_for("take-over", [_gate("parse"), _gate("configure")], {}) == ("L2", "no gate here builds or tests the code")
    assert oc.level_for("take-over", [_gate("build")], {"others-commit": {"option": "c"}}) == ("L2", "others share this repo")
    assert oc.level_for("team", [_gate("build")], {}) == ("L2", "others share this repo")
    assert oc.level_for("audit", [_gate("build")], {})[0] == "L2"
    assert oc.level_for("migrate", [_gate("build")], {})[0] == "L2"


def test_a_level_override_wins_inside_l1_to_l4_and_is_capped_at_l4():
    for want, level in (("L1", "L1"), ("L2", "L2"), ("L3", "L3"), ("l4", "L4")):
        assert oc.level_for("team", [], {"level-override": {"value": want, "source": "owner"}}) == (level, "you asked for it")
    for want in ("L5", "L6"):
        answers = {"level-override": {"value": want, "source": "owner"}}
        assert oc.level_for("team", [], answers)[0] == "L2" and oc.level_over_cap(answers)
    assert oc.level_for("team", [], {"level-override": {"value": "high please", "source": "owner"}})[0] == "L2"


# ---- t-687 (F6-4): a quoted clause is never cut inside

@pytest.mark.parametrize("opening,quoted", [
    ("hi, I took over this firmware project from my old team (it is at https://github.com/example-org/QTRM64) so help", "I took over this firmware project from my old team"),
    ("I took over this project from my old team at https://github.com/example-org/QTRM64 and need help", "I took over this project from my old team"),
    ("I took over src/legacy from my old team", "I took over"),
    ("I took over this project [the old one], what now", "I took over this project"),
])
def test_a_situation_quote_ends_before_a_bracket_or_a_link_never_inside_one(opening, quoted):
    g = oc.situation_guess(look(), [opening])
    assert g["reasons"][0]["text"] == 'you said "%s"' % quoted
    assert quoted in opening


def test_a_situation_quote_too_long_to_show_whole_is_the_matched_words_not_a_cut_sentence():
    said = "I want to say a great many things about the firmware and how I took over " + "the project from the team " * 12 + "and more"
    reason = oc.situation_guess(look(), [said])["reasons"][0]["text"]
    assert reason == 'you said "took over"' and "..." not in reason


def test_a_long_others_reply_is_quoted_whole_or_to_a_clause_end_or_not_at_all():
    whole = "bach was only testing uart stuff"
    assert oc._clause_of("1a, %s. I want more" % whole, "a") == whole
    long_ = "bach tested " + "the uart and the spi and the i2c bus " * 6
    got = oc._clause_of("1a, %s, and then he left. more" % long_.strip(), "a")
    assert "..." not in got and got in long_ + ", and then he left" and (got == "" or not got.endswith(" and"))
    assert oc._clause_of("1a, " + "word " * 60, "a") == ""
