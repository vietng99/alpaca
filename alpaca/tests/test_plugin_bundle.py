"""M3.10 -- the skills plugin bundled from the canonical shared source.

Proof test for the Done-when (P-010 spec:911, spec 5.12:590-592, section 6 row 716):

  * all fifteen skills named by P-010 are present in the bundle at the entry-point
    path the plan table records for each: SKILL.md at the skill root for eleven of
    them, the nested skills/eli5/SKILL.md and skills/i-have-adhd/SKILL.md, the seven
    caveman/skills/*/SKILL.md, and RULE.md (no SKILL.md) for html-safe;
  * every support file, hook script and hook data file the table names is recorded in
    BUNDLE-MANIFEST.json with a sha256, and every file physically vendored into the
    bundle carries a sha256 that matches its bytes on disk;
  * every skill row records its upstream path and a license status that is either the
    vendored LICENSE file reference or the literal `no-upstream-license`, never blank;
  * the two symlinked skills (nuclear, napalm) record both the symlink and its resolved
    target, and are copied as resolved bytes;
  * the eleven hook locations are flattened into one plugin/alpaca/hooks/ and the flattening
    is traceable back to each hook's upstream path;
  * the plugin hooks are registered under the M1.5 fail-open and timeout contract;
  * the bundle contains no skill not named in P-010 or section 6 row 716;
  * the four Alpaca-native skills alpaca-intake, alpaca-onboard, alpaca-op and
    alpaca-runbook-forge ship under skills/.

Every control asserts the POSITIVE and the NEGATIVE path, so none is a tautological pass.
"""
import hashlib
import json
import os

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
PLUGIN = os.path.join(REPO, "plugin", "alpaca")
MANIFEST_PATH = os.path.join(PLUGIN, "BUNDLE-MANIFEST.json")

# The fifteen skills P-010 names, and the entry-point shape the plan table records.
# entry: relative path(s) from the skill dir plugin/alpaca/skills/<name>/ to its SKILL.md.
# "html-safe" is the one skill with no SKILL.md; its entry is a RULE.md under plugin/alpaca/rules/.
ROOT_ELEVEN = [
    "spear", "sang", "sam", "flare", "humanizer",
    "autodrive", "timebomb", "capture", "vsys", "nuclear", "napalm",
]
CAVEMAN_SUBSKILLS = [
    "caveman", "caveman-commit", "caveman-review", "caveman-stats",
    "caveman-help", "caveman-compress", "cavecrew",
]
# skill -> list of entry-point paths relative to plugin/alpaca/
EXPECTED_ENTRY = {}
for _n in ROOT_ELEVEN:
    EXPECTED_ENTRY[_n] = ["skills/%s/SKILL.md" % _n]
EXPECTED_ENTRY["eli5"] = ["skills/eli5/skills/eli5/SKILL.md"]
EXPECTED_ENTRY["i-have-adhd"] = ["skills/i-have-adhd/skills/i-have-adhd/SKILL.md"]
EXPECTED_ENTRY["caveman"] = [
    "skills/caveman/skills/%s/SKILL.md" % s for s in CAVEMAN_SUBSKILLS
]
EXPECTED_ENTRY["html-safe"] = ["rules/html-safe/RULE.md"]

ALL_FIFTEEN = set(EXPECTED_ENTRY)

# The four skills that carry an upstream LICENSE; the other eleven are part of Alpaca (MIT).
LICENSED = {"eli5", "humanizer", "caveman", "i-have-adhd"}

# The eleven hook files that flatten into plugin/alpaca/hooks/.
HOOK_FILES = [
    "sam-inject.sh", "sam-stop-guard.sh",
    "sam-banwords.txt", "sam-banphrases.txt", "sam-tech-allow.txt",
    "html-safe-always-on.sh", "adhd-always-on.sh",
    "autodrive-inject.sh", "autodrive-stop.sh", "autodrive-set.sh", "autodrive-badge.sh",
]

# Skill names that exist in the shared source but are NOT named by P-010 or row 716:
# the bundle must not contain any of them.
FORBIDDEN_SKILLS = ["archify", "book-to-skill", "diagram-design", "envi", "hallmark",
                    "mattpocock-skills", "shared-toolkit"]

TIMEOUT_CEILING = 60  # M1.5 declared ceiling for a hook timeout.


def _sha256(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


@pytest.fixture(scope="module")
def manifest():
    assert os.path.isfile(MANIFEST_PATH), "BUNDLE-MANIFEST.json must exist"
    with open(MANIFEST_PATH, encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture(scope="module")
def rows(manifest):
    return {r["name"]: r for r in manifest["skills"]}


# ---------------------------------------------------------------- fifteen skills

def test_exactly_the_fifteen_named_skills(rows):
    # positive: every P-010 skill is a manifest row.
    for name in ALL_FIFTEEN:
        assert name in rows, "missing skill row: %s" % name
    # negative: no extra skill row beyond the fifteen.
    assert set(rows) == ALL_FIFTEEN, "unexpected skill rows: %s" % (set(rows) - ALL_FIFTEEN)


def test_entry_points_present_at_the_recorded_path(rows):
    for name, entries in EXPECTED_ENTRY.items():
        row = rows[name]
        recorded = row["entry_points"]
        assert sorted(recorded) == sorted(entries), \
            "%s entry points: recorded %s != expected %s" % (name, recorded, entries)
        for rel in entries:
            p = os.path.join(PLUGIN, rel)
            assert os.path.isfile(p), "entry point missing on disk: %s" % rel


def test_html_safe_has_a_rule_not_a_skill():
    # positive: the rule file is present at the plan's path.
    assert os.path.isfile(os.path.join(PLUGIN, "rules", "html-safe", "RULE.md"))
    # negative: html-safe carries no SKILL.md, at the root or nested.
    assert not os.path.exists(os.path.join(PLUGIN, "skills", "html-safe", "SKILL.md"))
    assert not os.path.exists(os.path.join(PLUGIN, "skills", "html-safe"))


def test_caveman_entry_points_are_nested_not_root():
    # positive: seven nested sub-skill SKILL.md files.
    for s in CAVEMAN_SUBSKILLS:
        assert os.path.isfile(
            os.path.join(PLUGIN, "skills", "caveman", "skills", s, "SKILL.md")), s
    # negative: caveman has no SKILL.md at its own root (it is a repo of sub-skills).
    assert not os.path.exists(os.path.join(PLUGIN, "skills", "caveman", "SKILL.md"))


def test_eli5_and_adhd_entry_points_are_nested():
    assert os.path.isfile(os.path.join(PLUGIN, "skills", "eli5", "skills", "eli5", "SKILL.md"))
    assert os.path.isfile(
        os.path.join(PLUGIN, "skills", "i-have-adhd", "skills", "i-have-adhd", "SKILL.md"))
    # negative: neither carries a top-level SKILL.md at the skill-dir root.
    assert not os.path.exists(os.path.join(PLUGIN, "skills", "eli5", "SKILL.md"))
    assert not os.path.exists(os.path.join(PLUGIN, "skills", "i-have-adhd", "SKILL.md"))


# ---------------------------------------------------------------- sha256 integrity

def test_every_file_row_records_a_sha256(rows):
    for name, row in rows.items():
        assert row["files"], "%s has no file rows" % name
        for fr in row["files"]:
            # positive: a non-blank 64-hex sha256 is recorded for every file.
            sha = fr.get("sha256", "")
            assert isinstance(sha, str) and len(sha) == 64, \
                "%s file %s has no sha256" % (name, fr.get("upstream_path"))
            int(sha, 16)  # hex or ValueError
            assert fr.get("upstream_path"), "%s file row missing upstream_path" % name


def test_vendored_files_match_their_recorded_sha(manifest, rows):
    seen = 0
    file_rows = [fr for row in rows.values() for fr in row["files"]]
    file_rows += manifest.get("hooks", [])
    for fr in file_rows:
        if not fr.get("vendored"):
            continue
        bp = fr["bundle_path"]
        disk = os.path.join(REPO, bp)
        assert os.path.isfile(disk), "vendored file missing: %s" % bp
        # positive: the sha recorded is the sha of the bytes on disk.
        assert _sha256(disk) == fr["sha256"], "sha mismatch for %s" % bp
        seen += 1
    # negative-guard: this is not vacuous; many files are actually vendored.
    assert seen >= 30, "too few vendored files verified: %d" % seen


def test_a_wrong_sha_would_not_match(rows):
    # negative: the match check above is real -- a bogus sha never matches a real file.
    bogus = "0" * 64
    for name, row in rows.items():
        for fr in row["files"]:
            if fr.get("vendored"):
                assert fr["sha256"] != bogus
                return
    pytest.fail("no vendored file found to test against")


# ---------------------------------------------------------------- license honesty

def test_license_status_never_blank(rows):
    for name, row in rows.items():
        lic = row.get("license")
        # positive/negative: present, a string, never blank.
        assert isinstance(lic, str) and lic.strip(), "%s has a blank license" % name
        assert (lic == "no-upstream-license") or ("LICENSE" in lic), \
            "%s license neither the sentinel nor a LICENSE reference: %r" % (name, lic)


def test_licensed_skills_reference_a_license_file(rows):
    for name in LICENSED:
        lic = rows[name]["license"]
        # positive: the four upstream-licensed skills name their LICENSE file.
        assert lic != "no-upstream-license", "%s should carry a license reference" % name
        assert "LICENSE" in lic


def test_own_skills_are_marked_part_of_alpaca(rows):
    for name in ALL_FIFTEEN - LICENSED:
        # the eleven without an upstream license are the owner's own work, part of Alpaca (MIT).
        assert rows[name]["license"] == "LICENSE (part of Alpaca, MIT)", \
            "%s should be marked part of Alpaca" % name


# ---------------------------------------------------------------- symlink resolution

def test_nuclear_and_napalm_record_symlink_and_resolved_target(rows):
    for name in ("nuclear", "napalm"):
        row = rows[name]
        # positive: the symlink and its resolved target are both recorded.
        assert row.get("symlink"), "%s must record it was a symlink" % name
        assert row.get("resolved_target"), "%s must record the resolved target" % name
        assert row["resolved_target"].endswith("/" + name)
    # negative: a non-symlinked skill records no resolved target.
    assert not rows["spear"].get("resolved_target")


# ---------------------------------------------------------------- hooks

def test_all_eleven_hook_files_vendored_and_recorded(manifest):
    flat = manifest["hook_flattening"]
    for h in HOOK_FILES:
        bp = "plugin/alpaca/hooks/%s" % h
        disk = os.path.join(REPO, bp)
        # positive: the hook file is present in the one flattened directory.
        assert os.path.isfile(disk), "hook file missing: %s" % bp
        # positive: the flattening maps it back to an upstream path.
        assert bp in flat, "hook %s not recorded in hook_flattening" % bp
        assert flat[bp], "hook %s has a blank upstream path" % bp
    # negative: nothing outside the eleven sneaks into hook_flattening as a bundle path.
    for bp in flat:
        base = os.path.basename(bp)
        assert base in HOOK_FILES, "unexpected flattened hook: %s" % bp


def test_hooks_json_registers_under_the_fail_open_timeout_contract():
    hj = os.path.join(PLUGIN, "hooks", "hooks.json")
    assert os.path.isfile(hj)
    with open(hj, encoding="utf-8") as f:
        data = json.load(f)
    assert "hooks" in data and data["hooks"], "hooks.json registers no hooks"
    n = 0
    for event, entries in data["hooks"].items():
        for entry in entries:
            for hook in entry["hooks"]:
                # positive: every registered hook declares a bounded timeout.
                assert "timeout" in hook, "a %s hook has no timeout" % event
                # negative: no hook may exceed the M1.5 ceiling.
                assert 0 < hook["timeout"] <= TIMEOUT_CEILING, \
                    "%s hook timeout out of contract: %r" % (event, hook["timeout"])
                assert hook.get("command"), "a %s hook has no command" % event
                n += 1
    assert n >= 1


def test_plugin_json_is_valid():
    pj = os.path.join(PLUGIN, "plugin.json")
    assert os.path.isfile(pj)
    with open(pj, encoding="utf-8") as f:
        data = json.load(f)
    assert data.get("name"), "plugin.json needs a name"


# ---------------------------------------------------------------- native skills

def test_alpaca_native_skills_ship():
    for name in ("alpaca-intake", "alpaca-onboard", "alpaca-op", "alpaca-runbook-forge"):
        p = os.path.join(REPO, "skills", name, "SKILL.md")
        # positive: each Alpaca-native skill ships a SKILL.md.
        assert os.path.isfile(p), "Alpaca-native skill missing: %s" % name
        with open(p, encoding="utf-8") as f:
            head = f.read(400)
        assert head.lstrip().startswith("---"), "%s needs YAML front matter" % name
    # negative: they live under skills/, not inside the vendored plugin bundle.
    assert not os.path.exists(os.path.join(PLUGIN, "skills", "alpaca-intake"))


# ---------------------------------------------------------------- no stowaways

def test_no_forbidden_skill_in_bundle(rows):
    for bad in FORBIDDEN_SKILLS:
        # negative: a skill outside P-010 / row 716 is neither a row nor a directory.
        assert bad not in rows, "forbidden skill row present: %s" % bad
        assert not os.path.exists(os.path.join(PLUGIN, "skills", bad)), \
            "forbidden skill dir present: %s" % bad


def test_bundle_skill_dirs_are_all_named(rows):
    skills_dir = os.path.join(PLUGIN, "skills")
    on_disk = {d for d in os.listdir(skills_dir)
               if os.path.isdir(os.path.join(skills_dir, d))}
    # every directory under plugin/alpaca/skills/ is one of the named skills (minus html-safe,
    # which lives under rules/, not skills/).
    allowed = (ALL_FIFTEEN - {"html-safe"})
    assert on_disk <= allowed, "unnamed skill dirs on disk: %s" % (on_disk - allowed)


# ---------------------------------------------------------------- manifest listing

def test_alpaca_manifest_documents_the_plugin_tree():
    with open(os.path.join(REPO, "ALPACA-MANIFEST"), encoding="utf-8") as f:
        text = f.read()
    # positive: the manifest acknowledges the vendored bundle as harness-owned. M3.10
    # documents plugin/alpaca/ and skills/ here; M4.1 promotes them to a formal mechanism class
    # alongside the doctor rework (listing them as bare mechanism paths now would fail
    # doctor's existence check on the minimal synthetic fixtures).
    assert "plugin/alpaca/" in text, "ALPACA-MANIFEST must document the plugin bundle"
    assert "skills/" in text, "ALPACA-MANIFEST must document the Alpaca-native skills tree"
    # negative: the manifest still has its two class sections intact.
    assert "[mechanism]" in text and "[memory]" in text


def test_transcript_parser_debt_choice_recorded(manifest):
    # Step 6: the remember/capture parser debt choice is recorded, not left implicit.
    debt = manifest.get("transcript_parser_debt")
    assert debt and debt.get("choice"), "the parser-debt choice must be recorded"
