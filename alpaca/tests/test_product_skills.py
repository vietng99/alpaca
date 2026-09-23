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

from alpaca import manifest
from alpaca.tests.conftest import REPO

SKILLS = ("alpaca-first-chat", "alpaca-onboard", "alpaca-op", "alpaca-runbook-forge",
          "alpaca-from-notes")
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


def test_one_copy_only():
    assert not os.path.exists(os.path.join(REPO, "skills")), "a second copy under skills/"
    on_disk = {d for d in os.listdir(CLAUDE_SKILLS) if d.startswith("alpaca-")}
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
        if not rel or rel.startswith(("vendor/", "plugin/")) or rel == "MANIFEST.json":
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
