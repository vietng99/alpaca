"""The product's own skills reach Claude Code with no hand copying.

Claude Code loads a project's skills from `.claude/skills/<name>/SKILL.md`. Alpaca's own skills
live there, one copy each, and each is a mechanism path in ALPACA-MANIFEST. So a git clone has
them the moment it is made, and the paths that carry the mechanism class carry them too: a copy
into an existing repo, `setup/ship.py`, a release and `alpaca upgrade`. The rest of
`.claude/skills/` (a project's own skills, the spec-kit and OpenSpec skills `alpaca spec init`
writes) stays the project's.

The first-chat skill is named alpaca-first-chat, so no skill shares its name with the unrelated
`alpaca intake` verb (spec and runbook to rows).
"""
import json
import os
import re
import subprocess
from pathlib import Path

from alpaca import manifest
from alpaca.tests.conftest import REPO
from alpaca.tests.devfiles import names_as_data

names_as_data("docs/alpaca-bootstrap")           # a prefix the stale-path scan passes over

SKILLS = ("alpaca-first-chat", "alpaca-onboard", "alpaca-op", "alpaca-runbook-forge",
          "alpaca-from-notes", "alpaca-interview", "alpaca-log-review", "alpaca-aar", "shield")
CLAUDE_SKILLS = os.path.join(REPO, ".claude", "skills")


def _front_matter(path):
    text = open(path, encoding="utf-8").read()
    assert text.startswith("---\n"), path
    head = text[4:text.index("\n---\n", 4)]
    return dict(re.findall(r"^([a-z]+): *(.*)$", head, re.M)), text


def test_every_product_skill_is_a_claude_code_project_skill():
    for name in SKILLS:
        path = os.path.join(CLAUDE_SKILLS, name, "SKILL.md")
        assert os.path.isfile(path), "not a Claude Code project skill: %s" % name
        meta, _text = _front_matter(path)
        assert meta.get("name") == name, (name, meta.get("name"))
        assert meta.get("description"), name


def test_codex_shield_entry_reaches_the_canonical_skill_and_native_adapter():
    entry = Path(REPO) / ".agents/skills/shield/SKILL.md"
    assert entry.is_file() and not entry.is_symlink()
    meta, text = _front_matter(entry)
    assert meta["name"] == "shield" and meta["description"]
    targets = [(entry.parent / p).resolve() for p in re.findall(r"\]\(([^)]+)\)", text)]
    assert set(targets) == {
        Path(CLAUDE_SKILLS) / "shield/SKILL.md", Path(CLAUDE_SKILLS) / "shield/codex.md"}
    assert all(p.is_file() for p in targets)
    mechanism = {p.rstrip("/") for p in manifest.mechanism(REPO)}
    assert ".agents/skills/shield" in mechanism
    assert ".agents/skills" not in mechanism


def test_one_copy_only():
    assert not os.path.exists(os.path.join(REPO, "skills")), "a second copy under skills/"
    on_disk = {d for d in os.listdir(CLAUDE_SKILLS) if d.startswith("alpaca-") or d in SKILLS}
    assert on_disk == set(SKILLS)


def test_each_skill_is_a_mechanism_path_and_the_rest_of_claude_skills_is_not():
    mech = [entry.rstrip("/") for entry in manifest.mechanism(REPO)]
    for name in SKILLS:
        assert ".claude/skills/%s" % name in mech, name
    assert ".claude/skills" not in mech and ".claude" not in mech
    memory = [entry.rstrip("/") for entry in manifest.memory(REPO)]
    assert not any(m.startswith(".claude/skills") for m in memory)


def test_the_shipped_digest_map_carries_the_skills():
    with open(os.path.join(REPO, "MANIFEST.json"), encoding="utf-8") as fh:
        files = json.load(fh)["files"]
    for name in SKILLS:
        assert ".claude/skills/%s/SKILL.md" % name in files, name
    assert not any(p.startswith("skills/") for p in files)


def test_the_skills_are_tracked_so_a_clone_has_them():
    if not os.path.isdir(os.path.join(REPO, ".git")) and not os.path.isfile(os.path.join(REPO, ".git")):
        return                                      # a release copy: the digest map test covers it
    out = subprocess.run(["git", "-C", REPO, "ls-files", ".claude/skills"], capture_output=True,
                         text=True, check=True).stdout.split()
    for name in SKILLS:
        assert ".claude/skills/%s/SKILL.md" % name in out, name


def test_no_skill_is_named_after_the_intake_verb():
    names = {d for d in os.listdir(CLAUDE_SKILLS)}
    assert "alpaca-intake" not in names
    for name in SKILLS:
        meta, text = _front_matter(os.path.join(CLAUDE_SKILLS, name, "SKILL.md"))
        assert "/alpaca-intake" not in text, name
    meta, text = _front_matter(os.path.join(CLAUDE_SKILLS, "alpaca-first-chat", "SKILL.md"))
    assert "/alpaca-first-chat" in meta["description"] or "/alpaca-first-chat" in text


def test_no_file_points_at_the_old_skill_paths():
    """Every reference names .claude/skills/<name>/ (or the skill by name), never skills/<name>/."""
    listed = subprocess.run(["git", "-C", REPO, "ls-files"], capture_output=True, text=True)
    if listed.returncode:
        return
    stale = re.compile(r"(?<![./\w])skills/alpaca-|alpaca-intake/|skill alpaca-intake|`alpaca-intake`")
    hits = []
    for rel in listed.stdout.split("\n"):
        # .alpaca/ (the record) and docs/alpaca-bootstrap/ (the private build history) quote the
        # old paths on purpose as evidence; a private tree tracks them and neither ships.
        if not rel or rel.startswith(("vendor/", "plugin/", ".alpaca/", "docs/alpaca-bootstrap/")) or rel == "MANIFEST.json":
            continue
        path = os.path.join(REPO, rel)
        try:
            text = open(path, encoding="utf-8").read()
        except (UnicodeDecodeError, OSError):
            continue
        for number, line in enumerate(text.splitlines(), 1):
            if stale.search(line) and "test_product_skills.py" not in rel:
                hits.append("%s:%d: %s" % (rel, number, line.strip()[:120]))
    assert hits == [], "\n".join(hits)


# ---- the first chat skills (op-062 t-656) -------------------------------------------------------------------

def _cli_commands(text):
    """Every `bin/alpaca ...` command in the inline code and the code blocks of a skill, as token lists."""
    spans = [" ".join(m.split()) for m in re.findall(r"`([^`]+)`", text)]
    spans += re.findall(r"```[a-z]*\n(.*?)```", text, re.S)
    out = []
    for span in spans:
        for line in span.split("\n"):
            if line.strip().startswith("bin/alpaca "):
                out.append(line.strip().split())
    return out


def _subparsers(parser):
    for action in parser._actions:
        if hasattr(action, "_name_parser_map"):
            return action._name_parser_map
    return {}


def _options(parser):
    return {opt for action in parser._actions for opt in action.option_strings}


def _command_problems(text):
    """What is wrong with the `bin/alpaca` commands of `text`: an unknown verb, subverb or flag."""
    from alpaca import cli
    for mod in cli.VERB_MODULES:
        __import__("alpaca.%s" % mod)
    parser, sub = cli.build_parser()
    for _name, reg in cli._EXTRA_PARSERS:
        reg(sub)
    verbs = _subparsers(parser)
    problems = []
    for tokens in _cli_commands(text):
        verb = tokens[1]
        if verb not in verbs:
            problems.append("unknown verb: %s" % " ".join(tokens))
            continue
        vparser, rest = verbs[verb], tokens[2:]
        subs = _subparsers(vparser)
        if rest and subs and not rest[0].startswith(("-", "<", "[", '"')):
            if rest[0] not in subs:
                problems.append("unknown subverb: %s" % " ".join(tokens))
                continue
            vparser, rest = subs[rest[0]], rest[1:]
        known = _options(vparser) | {"-h", "--help"}
        problems += ["unknown flag %s: %s" % (tok, " ".join(tokens)) for tok in rest
                     if re.fullmatch(r"--[a-z][a-z-]*(=.*)?", tok) and tok.split("=")[0] not in known]
    return problems


def test_every_alpaca_command_in_the_first_chat_skills_names_a_real_verb_subverb_and_flag():
    for name in ("alpaca-first-chat", "alpaca-onboard"):
        text = open(os.path.join(CLAUDE_SKILLS, name, "SKILL.md"), encoding="utf-8").read()
        assert len(_cli_commands(text)) >= 5, name
        assert _command_problems(text) == [], name


def test_the_command_check_catches_a_wrong_verb_subverb_and_flag():
    bad = ("`bin/alpaca onboarding look x`\n`bin/alpaca onboard glance x`\n"
           "`bin/alpaca onboard look x --scrath d`\n`bin/alpaca onboard look x --scratch d --quote q`")
    problems = _command_problems(bad)
    assert len(problems) == 3 and "unknown verb" in problems[0] and "unknown subverb" in problems[1] and "--scrath" in problems[2]


def test_the_first_chat_skill_holds_the_rules_the_verbs_rely_on():
    text = open(os.path.join(CLAUDE_SKILLS, "alpaca-first-chat", "SKILL.md"), encoding="utf-8").read()
    flat = " ".join(text.split())
    assert "never AskUserQuestion" in flat and "unchanged as one message" in flat
    for situation in ("take over", "team repo", "audit only", "migrate or rewrite", "start empty"):
        assert situation in flat, situation
    assert "never has more than four questions" in flat
    for part in ("what I understood", "what I found", "where Alpaca sits", "the checks", "the first steps",
                 "how a change reaches the repo", "how much I do alone", "what stays with the owner", "how to undo it",
                 "the settings"):
        assert part in flat, part
    assert "Nothing is configured, cloned into the studio or attached before the owner's typed yes" in flat
    assert "a click is not an owner word" in flat and "Run `bin/alpaca attach init` on your own" in flat
    for token in ("ONBOARD-NOT-A-YES", "ONBOARD-CARD-NOT-SHOWN", "LOOK-AUTH", "ONBOARD-NOT-NEWEST"):
        assert token in flat, token
    # op-062 t-656 fix round 3: the first-chat gate at the dispatcher and the honest limit the code cannot close
    assert "Do not wrap a command in `script`" in flat and "as if the owner ran it" in flat
    assert "refused with ONBOARD-FIRST-CHAT-OPEN. Tell the owner what the command would do" in flat


def test_the_first_chat_skills_read_plainly():
    """No em dash, plain quotes, and none of the words the plain-writing ban list names."""
    from alpaca.gates import literal_guard as lg
    tiers = {"words": [], "phrases": [], "placeholders": [], "structures": []}
    parsed = lg.parse_ban_list(lg.shipped_preset_path("plain-writing"))
    for tier in lg.STYLE_TIERS:
        tiers[tier].extend(parsed[tier])
    for name in ("alpaca-first-chat", "alpaca-onboard"):
        text = open(os.path.join(CLAUDE_SKILLS, name, "SKILL.md"), encoding="utf-8").read()
        assert "\N{EM DASH}" not in text and "\N{LEFT DOUBLE QUOTATION MARK}" not in text, name
        assert "\N{RIGHT SINGLE QUOTATION MARK}" not in text, name
        body = text.split("---\n", 2)[2]
        hits = lg.lint(body, tiers)
        assert hits == [], (name, [(m.tier, m.matched) for m in hits])


def _first_chat_flat():
    text = open(os.path.join(CLAUDE_SKILLS, "alpaca-first-chat", "SKILL.md"), encoding="utf-8").read()
    return text, " ".join(text.split())


def test_the_first_chat_skill_glosses_every_alpaca_word_at_its_first_use_in_each_message():
    """Rehearsal round 1 (P3, R11): a summary turn used "baseline" bare. The skill carries the glossary the card uses,
    with the rule, and the glossary holds baseline, gate, op, task, level, study, upstream and base."""
    from alpaca import onboard_card as oc
    text, flat = _first_chat_flat()
    assert set(oc.GLOSSARY) >= {"studio", "baseline", "gate", "op", "task", "level", "study", "upstream", "base"}
    for word, gloss in oc.GLOSSARY.items():
        assert "- %s: %s" % (word, gloss) in text, word
    assert "In each message you write, the first time you use one of the words below" in flat
    assert "even when an earlier message already did" in flat


def test_the_first_chat_skill_runs_the_study_scouts_and_the_map_check_in_the_attach_first_win():
    """Rehearsal round 1 (R9 = 1): the host ran `attach study` and stopped, so the first summary showed an empty map."""
    text, flat = _first_chat_flat()
    first_win = flat[flat.index("In attach mode"):flat.index("If the owner wants to skip")]
    order = [first_win.index(t) for t in ("`bin/alpaca attach study`", "study scouts", "`bin/alpaca attach study check`",
                                          "`bin/alpaca attach study ingest`", "Turn 4 shows the map")]
    assert order == sorted(order)
    assert "[ptr: app@<path>:<line>]" in first_win and "A first summary that does not show the map is not finished" in first_win


def test_the_first_chat_skill_takes_a_yes_with_filler_and_records_who_merges():
    from alpaca import onboard_card as oc
    text, flat = _first_chat_flat()
    for phrase in oc.YES_FILLER:
        assert "`%s`" % phrase in flat, phrase
    assert "the words left must be exactly `yes`" in flat and "Any other word keeps the reply a change" in flat
    assert 'answer merges --option a --value "<the words that name the person>"' in flat
