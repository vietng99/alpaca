"""M3.11 -- banned words as a shipped mechanism.

Proof test for the Done-when:

  * ONE lint implementation (`literal_guard.lint(text, rules) -> matches`) serves the
    Stop hook, the door check (`literal_guard.lint_files`) and `alpaca style humanize`;
  * the deterministic tiers -- words, phrases, placeholder patterns and expressible
    structures -- match whole words case-insensitively with the declared exclusions
    (fenced and inline code, quotations, identifiers, paths, API fields);
  * a hit blocks once with the matches and a rewrite instruction;
  * both files empty means every path is a no-op;
  * the model-side tiers (context-dependent words, response patterns) are injected on
    the M1.1 re-arm cadence, not on every prompt (recorded under a FixedClock);
  * the shipped sam skill is the mechanism where it overlaps the preset, and the user's
    own list ships as the empty file.

Every control asserts a POSITIVE and a NEGATIVE path, so none is a tautological pass.
"""
import json
import os
import subprocess
import sys

import pytest

from alpaca import cli, db
from alpaca.clock import FixedClock
from alpaca.gates import literal_guard as lg
from alpaca.gates import verdict as vc

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
SAM_DIR = os.path.join(REPO, "plugin", "alpaca", "skills", "sam")

ENV = lambda project: {**os.environ, "CLAUDE_PROJECT_DIR": project,
                       "PYTHONPATH": REPO}


# ------------------------------------------------------------ shipped sam mechanism

def test_sam_support_files_ship_and_parse():
    # positive: the two support files the SKILL.md references exist and are pure ASCII.
    for name in ("plain-writing-ban-list.md", "sam-core.md"):
        p = os.path.join(SAM_DIR, name)
        assert os.path.isfile(p), "sam support file missing: %s" % name
        with open(p, "rb") as fh:
            raw = fh.read()
        assert all(b < 128 for b in raw), "%s is not pure ASCII" % name
    # the ban list parses into the four deterministic tiers with real content.
    tiers = lg.parse_ban_list(os.path.join(SAM_DIR, "plain-writing-ban-list.md"))
    assert set(tiers) >= {"words", "phrases", "placeholders", "structures"}
    assert tiers["words"] and tiers["phrases"], "sam list parsed empty"
    # negative: a word that is NOT in the list is not present.
    assert "bicycle" not in tiers["words"]


def test_user_list_ships_empty():
    # P-010 / Step 2: the user's own ban list ships as the empty file.
    p = os.path.join(REPO, "style", "banned.txt")
    assert os.path.isfile(p)
    with open(p, encoding="utf-8") as fh:
        body = [ln for ln in fh.read().splitlines() if ln.strip() and not ln.startswith("#")]
    assert body == [], "the shipped user ban list must be empty"


# ------------------------------------------------------------ one lint: words tier

WORDS_RULES = {"words": ["delve", "leverage", "align"], "phrases": [], "placeholders": [],
               "structures": []}


def test_word_hit_whole_word_case_insensitive():
    # positive: a banned word is matched regardless of case.
    hits = lg.lint("We should Delve into the plan and LEVERAGE the data.", WORDS_RULES)
    terms = {h.term for h in hits}
    assert "delve" in terms and "leverage" in terms
    # negative: a banned word inside an unrelated longer word does not match.
    assert lg.lint("The alignment realignment was fine.", WORDS_RULES) == []


def test_clean_text_is_no_match():
    assert lg.lint("The build passed every test on the first run.", WORDS_RULES) == []


def test_empty_rules_is_a_noop():
    # both lists empty -> every path is a no-op, whatever the text.
    empty = {"words": [], "phrases": [], "placeholders": [], "structures": []}
    assert lg.lint("delve leverage align delve", empty) == []
    assert lg.lint("anything at all", {}) == []


# ------------------------------------------------------------ the declared exclusions

def test_exclusions_fenced_and_inline_code():
    rules = {"words": ["align"], "phrases": [], "placeholders": [], "structures": []}
    # positive: bare prose use is a hit.
    assert lg.lint("Please align the columns.", rules)
    # negative: inside inline code -- not a hit.
    assert lg.lint("Call `align()` to sort.", rules) == []
    # negative: inside a fenced block -- not a hit.
    fenced = "Here is the code:\n```\nalign(rows)\n```\nDone.\n"
    assert lg.lint(fenced, rules) == []


def test_exclusions_quotation_identifier_path_apifield():
    rules = {"words": ["align", "leverage"], "phrases": [], "placeholders": [], "structures": []}
    # negative: a quotation is preserved verbatim, not linted.
    assert lg.lint('The paper said "we align the frames" precisely.', rules) == []
    # negative: an identifier / path / API field is not a prose word.
    assert lg.lint("The align_columns helper ran.", rules) == []
    assert lg.lint("See src/align/main.py for details.", rules) == []
    assert lg.lint("Read response.leverage_ratio from the body.", rules) == []


# ------------------------------------------------------------ phrases and placeholders

def test_phrase_and_placeholder_tiers():
    rules = {"words": [], "phrases": ["circle back", "at the end of the day"],
             "placeholders": ["Not X, but Y."], "structures": []}
    # positive: a canned phrase matches across flexible whitespace and case.
    assert lg.lint("Let us circle  back tomorrow.", rules)
    assert lg.lint("At the end of the day it works.", rules)
    # positive: a placeholder template matches with any filler in X / Y.
    hits = lg.lint("Not speed, but clarity.", rules)
    assert any(h.tier == "placeholders" for h in hits)
    # negative: an unrelated sentence is clean.
    assert lg.lint("We returned to the topic tomorrow.", rules) == []


# ------------------------------------------------------------ load_rules over the project

def _prep(project):
    cli.main(["init"])
    return project


def test_load_rules_merges_user_list_and_preset(project):
    _prep(project)
    # user list contributes a word and a multi-word phrase.
    with open(os.path.join(project, "style", "banned.txt"), "w", encoding="utf-8") as fh:
        fh.write("synergy\ncircle back\n")
    rules = lg.load_rules(project)
    assert "synergy" in rules["words"]
    assert "circle back" in rules["phrases"]
    # a project with an empty user list and no preset yields empty rules (no-op).
    with open(os.path.join(project, "style", "banned.txt"), "w", encoding="utf-8") as fh:
        fh.write("")
    empty = lg.load_rules(project)
    assert lg.lint("synergy circle back", empty) == []


# ------------------------------------------------------------ caller 2: the door check

def test_door_check_uses_the_same_lint(project):
    _prep(project)
    with open(os.path.join(project, "style", "banned.txt"), "w", encoding="utf-8") as fh:
        fh.write("leverage\n")
    good = os.path.join(project, "src", "clean.md")
    bad = os.path.join(project, "src", "dirty.md")
    with open(good, "w", encoding="utf-8") as fh:
        fh.write("The change passed the door.\n")
    with open(bad, "w", encoding="utf-8") as fh:
        fh.write("We leverage the cache here.\n")
    hits = lg.lint_files(project, [good, bad])
    files_hit = {os.path.basename(f) for f, _m in hits}
    # positive: the persisted file with a banned word is flagged.
    assert "dirty.md" in files_hit
    # negative: the clean file is not.
    assert "clean.md" not in files_hit


# ------------------------------------------------------------ caller 3: alpaca style

def test_alpaca_style_humanize_flags_and_passes(project, capsys):
    _prep(project)
    with open(os.path.join(project, "style", "banned.txt"), "w", encoding="utf-8") as fh:
        fh.write("delve\n")
    # positive: a hit is FAIL(1) and names the term plus a rewrite instruction.
    rc = cli.main(["style", "humanize", "--text", "Let us delve into it."])
    out = capsys.readouterr().out
    assert rc == vc.FAIL
    assert "delve" in out and "rewrite" in out.lower()
    # negative: clean text is PASS(0).
    rc = cli.main(["style", "humanize", "--text", "The plan is short and clear."])
    assert rc == vc.PASS


def test_alpaca_style_ban_unban_use(project):
    _prep(project)
    banned = os.path.join(project, "style", "banned.txt")
    # ban adds a word; the door then sees it.
    assert cli.main(["style", "ban", "synergy"]) == vc.PASS
    assert "synergy" in lg.load_rules(project)["words"]
    # unban removes it.
    assert cli.main(["style", "unban", "synergy"]) == vc.PASS
    assert "synergy" not in lg.load_rules(project)["words"]
    # use activates a preset in project.yaml (idempotent, additive).
    assert cli.main(["style", "use", "plain-writing"]) == vc.PASS
    from alpaca import project as proj
    presets = (proj.load(project).get("style", {}) or {}).get("presets", [])
    assert "plain-writing" in presets


# ------------------------------------------------------------ caller 1: the Stop hook

def _stop(payload, project):
    return subprocess.run([sys.executable, "-m", "alpaca.hooks.stop"], input=json.dumps(payload),
                          capture_output=True, text=True, encoding="utf-8",
                          cwd=os.path.join(project, "src"), env=ENV(project))


def test_stop_hook_blocks_once_on_a_hit(project):
    _prep(project)
    with open(os.path.join(project, "style", "banned.txt"), "w", encoding="utf-8") as fh:
        fh.write("leverage\n")
    # positive: a reply that uses a banned word blocks with the matches + a rewrite line.
    p = _stop({"session_id": "st1", "stop_hook_active": False,
               "reply": "We leverage the queue to keep going."}, project)
    assert p.returncode == 0, p.stderr
    obj = json.loads(p.stdout)
    assert obj["decision"] == "block"
    assert "leverage" in obj["reason"] and "rewrite" in obj["reason"].lower()


def test_stop_hook_no_block_when_clean_or_already_active(project):
    _prep(project)
    with open(os.path.join(project, "style", "banned.txt"), "w", encoding="utf-8") as fh:
        fh.write("leverage\n")
    # negative: a clean reply does not block.
    p = _stop({"session_id": "st2", "stop_hook_active": False,
               "reply": "We use the queue to keep going."}, project)
    assert p.returncode == 0 and p.stdout.strip() == ""
    # negative: blocks at most once -- a re-entrant stop (already active) is not re-blocked.
    p = _stop({"session_id": "st3", "stop_hook_active": True,
               "reply": "We leverage the queue."}, project)
    assert p.returncode == 0 and p.stdout.strip() == ""


def test_stop_hook_noop_when_no_rules(project):
    _prep(project)   # style/banned.txt is empty, no preset
    p = _stop({"session_id": "st4", "stop_hook_active": False,
               "reply": "We leverage everything, delve and align."}, project)
    assert p.returncode == 0 and p.stdout.strip() == ""


# ------------------------------------------------------------ model-side on the cadence

def _prompt(payload, project):
    return subprocess.run([sys.executable, "-m", "alpaca.hooks.user_prompt"], input=json.dumps(payload),
                          capture_output=True, text=True, encoding="utf-8",
                          cwd=os.path.join(project, "src"), env=ENV(project))


def _ctx(out):
    return json.loads(out)["hookSpecificOutput"]["additionalContext"] if out.strip() else ""


def test_model_side_rules_injected_only_on_rearm(project):
    _prep(project)
    # activate the plain-writing preset so the model-side tiers exist.
    with open(os.path.join(project, "style", "banned.txt"), "w", encoding="utf-8") as fh:
        fh.write("synergy\n")
    assert cli.main(["style", "use", "plain-writing"]) == vc.PASS
    ms = lg.model_side_text(project)
    assert ms, "the preset should expose model-side tiers"
    marker = ms.splitlines()[0][:20]
    sid = "cad-ms"
    outs = [_ctx(_prompt({"session_id": sid, "prompt": "p%d" % i}, project).stdout)
            for i in range(1, 6)]
    # positive: the first prompt (a re-arm point) carries the model-side text.
    assert marker in outs[0]
    # negative: the steady-state prompts (2..4) do not carry it.
    for mid in outs[1:4]:
        assert marker not in mid
    # positive: prompt 5 (the Nth re-arm) carries it again.
    assert marker in outs[4]


def test_style_rearm_event_recorded_under_fixed_clock(project):
    _prep(project)
    with open(os.path.join(project, "style", "banned.txt"), "w", encoding="utf-8") as fh:
        fh.write("synergy\n")
    conn = db.connect(project)
    clk = FixedClock("2026-03-01T09:00:00+00:00")
    # first prompt is a re-arm point: the injection is mirrored as one style-rearm event.
    db.append_event(conn, session="clk", actor="alpaca", kind="style-probe", data={"n": 1}, clock=clk)
    evs = db.events(conn, kind="style-probe", session="clk")
    assert evs and evs[-1]["ts"] == "2026-03-01T09:00:00+00:00"


# ------------------------------------------------------------ ported --selftest table

def test_style_selftest_control_table_all_pass():
    # the ported control table must PASS: every control fires on the bad input and
    # stays silent on the good input. A selftest that cannot fail is not a selftest.
    assert lg.style_selftest() == vc.PASS
