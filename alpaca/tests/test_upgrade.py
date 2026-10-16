"""M4.2 proof: alpaca upgrade is a crash-safe seed-versus-refresh promotion.

Positive: an upgrade refreshes every mechanism path from a source harness tree, preserves the
human-owned project.yaml, merges CLAUDE.md by its boot-block markers, and leaves every memory path
(.alpaca/, RESUME.md, analytics/) unchanged in meaning (the record's hash chain and event count survive).

Negative / crash-safe: a kill injected at each phase, followed by a recovery run, leaves the tree
either fully old or fully new, never a mix; a mid-promotion partial rolls forward to fully new; a
journal already present refuses a fresh upgrade (it must be recovered first); recovery without a
journal is a no-op.

Timing is stamped from a FixedClock; both trees are throwaway roots under tmp_path, never the live
.alpaca/.
"""
import os
import shutil

import pytest

from alpaca import db, manifest, paths, upgrade, util
from alpaca.clock import FixedClock

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))

# The refresh mechanism paths we assert on (present in both trees, memory excluded, project.yaml
# and CLAUDE.md handled specially). Kept small but real: each is a manifest mechanism path.
REFRESH_FILES = ("bin/alpaca", "ALPACA-MANIFEST", "pytest.ini", ".claude/settings.json")
REFRESH_DIRS = ("alpaca/", "contracts/", "style/")
# marker-tagged paths differ old-vs-new; ALPACA-MANIFEST is copied verbatim into both trees, so it is
# a refresh path that happens to be byte-identical and is not reported as a local edit.
TAGGED = ("bin/alpaca", "pytest.ini", ".claude/settings.json") + REFRESH_DIRS


def _digest(base, rel):
    return upgrade._tree_digest(os.path.join(base, upgrade._rel_norm(rel)))


def _make_tree(base, marker, *, memory):
    """Build a project tree at `base` carrying every manifest mechanism path, each file tagged with
    `marker` so old and new content are distinguishable. When `memory` is True the tree also carries
    a live record (.alpaca/alpaca.db with a real hash chain), RESUME.md and analytics/."""
    os.makedirs(base)
    shutil.copy(os.path.join(REPO, "ALPACA-MANIFEST"), os.path.join(base, "ALPACA-MANIFEST"))

    # boot block: markers plus a house-rule line that must survive an upgrade of the block.
    boot = ("# House rules for %s must survive.\n\n" % marker +
            "%s\n# Alpaca boot %s\n%s\n" % (manifest.BOOT_BEGIN, marker, manifest.BOOT_END))
    util.write_text(os.path.join(base, "CLAUDE.md"), boot)

    os.makedirs(os.path.join(base, "bin"))
    util.write_text(os.path.join(base, "bin", "alpaca"), "#!/usr/bin/env python3\n# alpaca shim %s\n" % marker)
    os.chmod(os.path.join(base, "bin", "alpaca"), 0o755)
    util.write_text(os.path.join(base, "pytest.ini"), "[pytest]\n# %s\n" % marker)
    util.write_text(os.path.join(base, ".claude", "settings.json"),
                    '{"hooks": {}, "marker": "%s"}\n' % marker)

    for d in ("alpaca", "contracts", "style", "intents"):
        os.makedirs(os.path.join(base, d))
        util.write_text(os.path.join(base, d, "stub.txt"), "%s content %s\n" % (d, marker))
    # style/banned.txt is expected by the doctor and the fixtures.
    util.write_text(os.path.join(base, "style", "banned.txt"), "")

    # project.yaml is human-owned and project-specific: the two trees carry different values so a
    # preserve (not a clobber) is provable.
    util.write_text(os.path.join(base, "project.yaml"),
                    "name: proj-%s\nid: id-%s\n" % (marker, marker))

    if memory:
        conn = db.connect(base)          # creates .alpaca/alpaca.db and the schema
        for i in range(3):
            db.append_event(conn, session="s", actor="alpaca", kind="seed", data={"i": i})
        conn.close()
        util.write_text(os.path.join(base, "RESUME.md"), "# resume for %s\n" % marker)
        os.makedirs(os.path.join(base, "analytics"))
        util.write_text(os.path.join(base, "analytics", "index.html"),
                        "<!doctype html><title>%s</title>\n" % marker)


@pytest.fixture
def trees(tmp_path):
    """A live project root (with memory) and a fresh source harness tree (mechanism only)."""
    root = str(tmp_path / "live")
    source = str(tmp_path / "source")
    _make_tree(root, "OLD", memory=True)
    _make_tree(source, "NEW", memory=False)
    return root, source


def _snapshot_memory(root):
    resume = util.read_text(os.path.join(root, "RESUME.md"))
    analytics = util.read_text(os.path.join(root, "analytics", "index.html"))
    conn = db.connect(root)
    ok, _ = db.verify_chain(conn)
    n = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    conn.close()
    return resume, analytics, ok, n


def _assert_memory_unchanged(root, before):
    assert _snapshot_memory(root) == before
    resume, analytics, ok, n = _snapshot_memory(root)
    assert ok, "the record hash chain must still verify after the upgrade"


# --------------------------------------------------------------------------- plan
def test_plan_reports_local_edits_and_preserves(trees):
    root, source = trees
    p = upgrade.plan(root, source)
    # every marker-tagged refresh path differs old-vs-new: a reported local edit before replacement.
    for rel in TAGGED:
        assert rel in p.modified, rel
    assert "ALPACA-MANIFEST" in p.refresh and "ALPACA-MANIFEST" not in p.modified
    assert "project.yaml" in p.preserve
    assert "intents/" in p.preserve
    assert "CLAUDE.md" in p.merge
    # the memory class is named and never enters the refresh/merge/preserve lists.
    assert set(p.memory) == set(manifest.classes(root)["memory"])
    for rel in p.memory:
        assert rel not in p.refresh and rel not in p.merge and rel not in p.preserve
    # plan writes nothing to either tree.
    assert upgrade.status(root) is None


# --------------------------------------------------------------------------- full upgrade
def test_full_upgrade_refreshes_mechanism_and_leaves_memory(trees):
    root, source = trees
    before_mem = _snapshot_memory(root)
    orig_project = util.read_text(os.path.join(root, "project.yaml"))

    res = upgrade.apply(root, source, clock=FixedClock(step=1))
    assert res["ok"]

    # every mechanism refresh path now matches the source.
    for rel in REFRESH_FILES + REFRESH_DIRS:
        assert _digest(root, rel) == _digest(source, rel), rel
    # project.yaml is preserved, not clobbered.
    assert util.read_text(os.path.join(root, "project.yaml")) == orig_project
    # intents/ is the project's own queue: preserved, not refreshed from the source.
    assert util.read_text(os.path.join(root, "intents", "stub.txt")) == "intents content OLD\n"
    # CLAUDE.md keeps the target's house rules and carries the merged boot block.
    claude = util.read_text(os.path.join(root, "CLAUDE.md"))
    assert "House rules for OLD must survive." in claude
    assert "# Alpaca boot NEW" in claude
    assert claude.count(manifest.BOOT_BEGIN) == 1
    # memory is untouched in meaning; the record still verifies with the same event count.
    _assert_memory_unchanged(root, before_mem)
    # the scaffolding is cleaned up on success.
    assert not os.path.isdir(upgrade._upgrade_dir(root))
    assert upgrade.status(root) is None


# --------------------------------------------------------------------------- crash at each phase
@pytest.mark.parametrize("phase", upgrade.PHASES)
def test_kill_at_each_phase_recovers_fully_old_or_new(tmp_path, phase):
    root = str(tmp_path / "live")
    source = str(tmp_path / "source")
    _make_tree(root, "OLD", memory=True)
    _make_tree(source, "NEW", memory=False)

    before_mem = _snapshot_memory(root)
    orig = {rel: _digest(root, rel) for rel in REFRESH_FILES + REFRESH_DIRS}
    orig_project = util.read_text(os.path.join(root, "project.yaml"))

    with pytest.raises(upgrade._KillInjected):
        upgrade.apply(root, source, kill_at=phase, clock=FixedClock(step=1))
    # the crash left the journal in place: recovery has work to do.
    assert upgrade.status(root) is not None

    rec = upgrade.recover(root)
    assert rec["recovered"]

    expect_new = phase in ("promote", "done")
    for rel in REFRESH_FILES + REFRESH_DIRS:
        if expect_new:
            assert _digest(root, rel) == _digest(source, rel), "%s at kill %s -> new" % (rel, phase)
        else:
            assert _digest(root, rel) == orig[rel], "%s at kill %s -> old" % (rel, phase)
    # project.yaml is preserved whichever way recovery went.
    assert util.read_text(os.path.join(root, "project.yaml")) == orig_project
    # memory survives the crash and the recovery unchanged in meaning.
    _assert_memory_unchanged(root, before_mem)
    # recovery clears the scaffolding, so the CLI is never left behind a half tree.
    assert not os.path.isdir(upgrade._upgrade_dir(root))
    assert upgrade.status(root) is None
    # a second recovery is a clean no-op.
    assert upgrade.recover(root)["recovered"] is False


# --------------------------------------------------------------------------- mid-promotion partial
def test_mid_promotion_partial_rolls_forward(trees, monkeypatch):
    root, source = trees
    orig_install = upgrade._install
    state = {"n": 0}

    def flaky_install(r, staged, rel):
        state["n"] += 1
        if state["n"] == 2:
            raise RuntimeError("killed mid-promotion")
        return orig_install(r, staged, rel)

    monkeypatch.setattr(upgrade, "_install", flaky_install)
    with pytest.raises(RuntimeError):
        upgrade.apply(root, source, clock=FixedClock(step=1))
    # the journal is at promote: some paths were installed, some not (a half tree).
    assert upgrade.status(root)["phase"] == "promote"

    monkeypatch.setattr(upgrade, "_install", orig_install)
    rec = upgrade.recover(root)
    assert rec["direction"] == "forward"
    # rolling forward completes the promotion: the tree is fully new.
    for rel in REFRESH_FILES + REFRESH_DIRS:
        assert _digest(root, rel) == _digest(source, rel), rel
    assert not os.path.isdir(upgrade._upgrade_dir(root))


# --------------------------------------------------------------------------- negatives
def test_apply_refuses_when_a_journal_is_present(trees):
    root, source = trees
    upgrade._write_journal(root, {"phase": "stage", "source": source, "mechanism": []})
    with pytest.raises(upgrade.UpgradeLocked):
        upgrade.apply(root, source, clock=FixedClock())


def test_recover_without_journal_is_a_noop(trees):
    root, _ = trees
    res = upgrade.recover(root)
    assert res == {"recovered": False, "reason": "no upgrade in progress"}


def test_shape_check_refuses_an_incomplete_staged_build(trees, monkeypatch):
    root, source = trees
    real_build = upgrade._build_staged

    def build_then_break(r, s, staged, p):
        real_build(r, s, staged, p)
        os.remove(os.path.join(staged, "bin", "alpaca"))  # a path root still has -> shape must refuse

    monkeypatch.setattr(upgrade, "_build_staged", build_then_break)
    with pytest.raises(upgrade.ShapeCheckFailed):
        upgrade.apply(root, source, clock=FixedClock())
    # the bad staged build was never promoted: the tree is still old, and a recovery keeps it old.
    assert util.read_text(os.path.join(root, "bin", "alpaca")).endswith("alpaca shim OLD\n")
    upgrade.recover(root)
    assert util.read_text(os.path.join(root, "bin", "alpaca")).endswith("alpaca shim OLD\n")
    assert not os.path.isdir(upgrade._upgrade_dir(root))


# --------------------------------------------------------------------------- manifests (op-014)
def _write_manifest(base, mechanism, memory):
    util.write_text(os.path.join(base, "ALPACA-MANIFEST"),
                    "[mechanism]\n%s\n[memory]\n%s\n" % ("\n".join(mechanism), "\n".join(memory)))


def test_a_path_the_source_manifest_adds_is_installed_on_the_first_upgrade(trees):
    """A release that adds a mechanism path (a new launcher the new hooks call) must install it on
    the first upgrade. Reading only the live manifest would skip it and leave hooks that point at
    a missing file."""
    root, source = trees
    old = ["CLAUDE.md", "ALPACA-MANIFEST", ".claude/settings.json", "bin/alpaca", "alpaca/", "project.yaml"]
    _write_manifest(root, old, [".alpaca/", "RESUME.md", "analytics/"])
    _write_manifest(source, old + ["bin/alpaca-python", "docs/guide.md"], [".alpaca/", "RESUME.md", "analytics/"])
    util.write_text(os.path.join(source, "bin", "alpaca-python"), "#!/usr/bin/env bash\n# NEW\n")
    util.write_text(os.path.join(source, "docs", "guide.md"), "guide NEW\n")
    p = upgrade.plan(root, source)
    assert "bin/alpaca-python" in p.refresh and "docs/guide.md" in p.refresh
    upgrade.apply(root, source, clock=FixedClock(step=1))
    assert util.read_text(os.path.join(root, "bin", "alpaca-python")).endswith("# NEW\n")
    assert util.read_text(os.path.join(root, "docs", "guide.md")) == "guide NEW\n"
    # the live manifest is now the release's, so the next plan reads the same list.
    assert "bin/alpaca-python" in manifest.classes(root)["mechanism"]


def test_a_path_only_the_live_manifest_names_is_carried_over(trees):
    root, source = trees
    _write_manifest(root, ["ALPACA-MANIFEST", "bin/alpaca", "alpaca/", "legacy.txt"], [".alpaca/"])
    _write_manifest(source, ["ALPACA-MANIFEST", "bin/alpaca", "alpaca/"], [".alpaca/"])
    util.write_text(os.path.join(root, "legacy.txt"), "kept\n")
    upgrade.apply(root, source, clock=FixedClock(step=1))
    assert util.read_text(os.path.join(root, "legacy.txt")) == "kept\n"


def _write_lock(base, shipped):
    """A live MANIFEST.json recording the shipped bytes of each path in `shipped` (rel -> text)."""
    import json
    files = {rel: util.sha256_hex(text.encode("utf-8")) for rel, text in shipped.items()}
    util.write_text(os.path.join(base, "MANIFEST.json"), json.dumps({"files": files}) + "\n")


def _retire_trees(trees):
    """A live install whose manifest still lists skills/ and a release that dropped it (review L2).
    MANIFEST.json records the bytes the old release shipped there."""
    root, source = trees
    _write_manifest(root, ["ALPACA-MANIFEST", "bin/alpaca", "alpaca/", "skills/"], [".alpaca/"])
    _write_manifest(source, ["ALPACA-MANIFEST", "bin/alpaca", "alpaca/", ".claude/skills/alpaca-op/"],
                    [".alpaca/"])
    util.write_text(os.path.join(source, ".claude", "skills", "alpaca-op", "SKILL.md"), "op NEW\n")
    util.write_text(os.path.join(root, "skills", "old-intake", "SKILL.md"), "intake OLD\n")
    util.write_text(os.path.join(root, "skills", "old-op", "SKILL.md"), "op OLD\n")
    util.write_text(os.path.join(root, "skills", "old-op", "notes.md"), "edited here\n")
    util.write_text(os.path.join(root, "skills", "mine", "SKILL.md"), "the project's own\n")
    _write_lock(root, {"skills/old-intake/SKILL.md": "intake OLD\n",
                       "skills/old-op/SKILL.md": "op OLD\n",
                       "skills/old-op/notes.md": "as shipped\n",
                       "bin/alpaca": "whatever\n"})
    return root, source


def test_a_path_the_release_dropped_loses_its_shipped_files(trees):
    """Review L2: a mechanism path only the live manifest names was dropped by the release. Its
    files that still hold the shipped bytes (MANIFEST.json) are removed; a file the project edited
    or added there stays."""
    root, source = _retire_trees(trees)
    p = upgrade.plan(root, source)
    assert "skills/" in p.dropped and "skills/" not in p.mechanism
    assert sorted(p.remove) == ["skills/old-intake/SKILL.md", "skills/old-op/SKILL.md"]
    assert sorted(p.dropped_kept) == ["skills/mine/SKILL.md", "skills/old-op/notes.md"]
    upgrade.apply(root, source, clock=FixedClock(step=1))
    assert not os.path.lexists(os.path.join(root, "skills", "old-intake"))
    assert not os.path.lexists(os.path.join(root, "skills", "old-op", "SKILL.md"))
    assert util.read_text(os.path.join(root, "skills", "old-op", "notes.md")) == "edited here\n"
    assert util.read_text(os.path.join(root, "skills", "mine", "SKILL.md")) == "the project's own\n"
    assert util.read_text(os.path.join(root, ".claude", "skills", "alpaca-op", "SKILL.md")) == "op NEW\n"


def test_the_plan_verb_names_the_files_a_release_drops(trees, capsys, tmp_path, monkeypatch):
    import argparse
    monkeypatch.chdir(tmp_path)
    root, source = _retire_trees(trees)
    args = argparse.Namespace(target=root, source=source, plan=True, recover=False)
    assert upgrade._cmd_upgrade(args) == 0
    out = capsys.readouterr().out
    assert "dropped by the release, will be removed: skills/old-intake/SKILL.md" in out
    assert "dropped by the release, kept (not the shipped bytes): skills/mine/SKILL.md" in out
    assert os.path.isfile(os.path.join(root, "skills", "old-intake", "SKILL.md"))


@pytest.mark.parametrize("phase", upgrade.PHASES)
def test_a_crash_leaves_the_dropped_files_fully_old_or_fully_new(trees, phase):
    root, source = _retire_trees(trees)
    with pytest.raises(upgrade._KillInjected):
        upgrade.apply(root, source, kill_at=phase, clock=FixedClock(step=1))
    upgrade.recover(root)
    gone = not os.path.lexists(os.path.join(root, "skills", "old-intake", "SKILL.md"))
    assert gone == (phase in upgrade._FORWARD_FROM), phase
    assert util.read_text(os.path.join(root, "skills", "mine", "SKILL.md")) == "the project's own\n"


def test_a_directory_the_release_splits_keeps_the_files_it_still_lists(trees):
    """The live manifest lists docs/ as a whole; the release lists docs/guide.md alone. The guide is
    refreshed, a shipped docs file the release no longer lists is removed."""
    root, source = trees
    _write_manifest(root, ["ALPACA-MANIFEST", "bin/alpaca", "alpaca/", "docs/"], [".alpaca/"])
    _write_manifest(source, ["ALPACA-MANIFEST", "bin/alpaca", "alpaca/", "docs/guide.md"], [".alpaca/"])
    util.write_text(os.path.join(root, "docs", "guide.md"), "guide OLD\n")
    util.write_text(os.path.join(root, "docs", "old.md"), "old\n")
    util.write_text(os.path.join(source, "docs", "guide.md"), "guide NEW\n")
    _write_lock(root, {"docs/guide.md": "guide OLD\n", "docs/old.md": "old\n"})
    p = upgrade.plan(root, source)
    assert p.remove == ["docs/old.md"]
    upgrade.apply(root, source, clock=FixedClock(step=1))
    assert util.read_text(os.path.join(root, "docs", "guide.md")) == "guide NEW\n"
    assert not os.path.lexists(os.path.join(root, "docs", "old.md"))


def test_memory_named_by_either_manifest_is_never_touched(trees):
    """The release moves data.json to memory while the live manifest still calls it mechanism:
    the upgrade must not overwrite it."""
    root, source = trees
    _write_manifest(root, ["ALPACA-MANIFEST", "bin/alpaca", "alpaca/", "data.json"], [".alpaca/"])
    _write_manifest(source, ["ALPACA-MANIFEST", "bin/alpaca", "alpaca/"], [".alpaca/", "data.json"])
    util.write_text(os.path.join(root, "data.json"), '{"live": true}\n')
    util.write_text(os.path.join(source, "data.json"), '{"live": false}\n')
    p = upgrade.plan(root, source)
    assert "data.json" not in p.mechanism and "data.json" in p.memory
    upgrade.apply(root, source, clock=FixedClock(step=1))
    assert util.read_text(os.path.join(root, "data.json")) == '{"live": true}\n'


def test_intents_are_seeded_when_the_project_has_none(trees):
    root, source = trees
    shutil.rmtree(os.path.join(root, "intents"))
    upgrade.apply(root, source, clock=FixedClock(step=1))
    assert util.read_text(os.path.join(root, "intents", "stub.txt")) == "intents content NEW\n"


def test_push_mode_upgrades_the_target_from_the_given_source(trees, tmp_path, monkeypatch):
    """`alpaca upgrade --target <project>` run from a new install: the target is the root, the record
    survives. The working directory is a scratch dir, so a regression that falls back to the
    current project can never upgrade the repository the test runs from."""
    import argparse
    monkeypatch.chdir(tmp_path)
    root, source = trees
    before_mem = _snapshot_memory(root)
    args = argparse.Namespace(target=root, source=source, plan=False, recover=False)
    assert upgrade._cmd_upgrade(args) == 0
    assert _digest(root, "alpaca/") == _digest(source, "alpaca/")
    _assert_memory_unchanged(root, before_mem)


def test_push_mode_refuses_a_target_without_a_manifest(tmp_path, monkeypatch):
    import argparse
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.chdir(empty)
    args = argparse.Namespace(target=str(empty), source=None, plan=False, recover=False)
    assert upgrade._cmd_upgrade(args) != 0


# --------------------------------------------------------------------------- local edit or release change (t-646)
def _locked_trees(trees, tmp_path):
    """The live tree records, in MANIFEST.json, the bytes of every file its release shipped (all but
    pytest.ini, which the lock leaves out); the source holds a newer release of them."""
    import json
    root, source = trees
    files = {}
    for dp, dn, fn in os.walk(root):
        dn[:] = [d for d in dn if d not in (".alpaca", "analytics")]
        for f in fn:
            rel = os.path.relpath(os.path.join(dp, f), root).replace(os.sep, "/")
            if rel not in ("RESUME.md", "pytest.ini"):
                with open(os.path.join(dp, f), "rb") as fh:
                    files[rel] = util.sha256_hex(fh.read())
    util.write_text(os.path.join(root, "MANIFEST.json"), json.dumps({"files": files}) + "\n")
    return root, source


def test_a_path_that_differs_only_because_the_release_changed_it_is_not_a_local_edit(trees, tmp_path):
    root, source = _locked_trees(trees, tmp_path)
    p = upgrade.plan(root, source)
    assert "alpaca/" in p.changed and "bin/alpaca" in p.changed
    assert "alpaca/" not in p.modified and "bin/alpaca" not in p.modified
    # a path with no shipped sha at all (pytest.ini is not in the lock) stays a local edit
    assert p.modified == ["pytest.ini"] and p.local_files == ["pytest.ini"]


def test_an_edited_an_added_and_a_deleted_file_each_make_a_local_edit(trees, tmp_path):
    root, source = _locked_trees(trees, tmp_path)
    util.write_text(os.path.join(root, "alpaca", "stub.txt"), "edited by the owner\n")
    util.write_text(os.path.join(root, "alpaca", "mine.py"), "ADDED = 1\n")
    p = upgrade.plan(root, source)
    assert "alpaca/" in p.modified and "alpaca/" not in p.changed
    assert {"alpaca/stub.txt", "alpaca/mine.py"} <= set(p.local_files)
    os.remove(os.path.join(root, "alpaca", "mine.py"))
    util.write_text(os.path.join(root, "alpaca", "stub.txt"), "alpaca content OLD\n")     # back to the shipped bytes
    assert "alpaca/" in upgrade.plan(root, source).changed
    os.remove(os.path.join(root, "alpaca", "stub.txt"))
    p = upgrade.plan(root, source)
    assert "alpaca/" in p.modified and p.local_files == ["pytest.ini"] and p.deleted_files == ["alpaca/stub.txt"]


def test_python_caches_and_shipped_links_are_not_local_files(trees, tmp_path):
    import json
    root, source = _locked_trees(trees, tmp_path)
    os.makedirs(os.path.join(root, "alpaca", "__pycache__"))
    util.write_text(os.path.join(root, "alpaca", "__pycache__", "stub.cpython-312.pyc"), "x")
    os.symlink("stub.txt", os.path.join(root, "alpaca", "link"))
    with open(os.path.join(root, "MANIFEST.json"), encoding="utf-8") as fh:
        doc = json.load(fh)
    import hashlib
    doc["files"]["alpaca/link"] = hashlib.sha256(b"symlink:stub.txt").hexdigest()
    util.write_text(os.path.join(root, "MANIFEST.json"), json.dumps(doc) + "\n")
    p = upgrade.plan(root, source)
    assert "alpaca/" in p.changed and "alpaca/" not in p.modified
    os.remove(os.path.join(root, "alpaca", "link"))
    os.symlink("elsewhere", os.path.join(root, "alpaca", "link"))
    assert upgrade.plan(root, source).local_files.count("alpaca/link") == 1


def test_a_refresh_path_a_release_added_to_a_project_without_a_lock_is_a_local_edit(trees):
    root, source = trees                                  # the fixture writes no MANIFEST.json
    p = upgrade.plan(root, source)
    assert p.changed == [] and "alpaca/" in p.modified and "alpaca/stub.txt" in p.local_files


def test_the_plan_verb_says_refreshed_for_a_release_change_and_replaced_for_a_local_edit(trees, tmp_path, capsys, monkeypatch):
    import argparse
    monkeypatch.chdir(tmp_path)
    root, source = _locked_trees(trees, tmp_path)
    args = argparse.Namespace(target=root, source=source, plan=True, recover=False)
    assert upgrade._cmd_upgrade(args) == 0
    out = capsys.readouterr().out
    assert "  will be refreshed: alpaca/" in out and "  will be refreshed: bin/alpaca" in out
    assert "  local edit will be replaced: pytest.ini" in out
    assert "local edit will be replaced: alpaca/" not in out
    assert "modified=" in out and "changed=" in out
    reason = upgrade.print_plan(upgrade.plan(root, source))
    capsys.readouterr()
    assert reason.startswith("refresh=") and " changed=" in reason and reason.endswith("memory-untouched=%d" % len(upgrade.plan(root, source).memory))


def test_push_mode_reads_a_relative_target_from_the_callers_folder(trees, tmp_path, monkeypatch, capsys):
    """bin/alpaca-python runs Python in the install root; a relative --target is the caller's."""
    import argparse
    root, source = trees
    monkeypatch.chdir(REPO)
    monkeypatch.setenv("ALPACA_CALLER_CWD", str(tmp_path))
    args = argparse.Namespace(target="live", source=source, plan=True, recover=False)
    assert upgrade._cmd_upgrade(args) == 0
    assert "GATE alpaca-upgrade-plan: PASS" in capsys.readouterr().out


# --------------------------------------------------------------------------- boot block edits (t-646 round 1)
def _lock_again(root):
    """Record in root's MANIFEST.json the bytes every file holds now (the release shipped them)."""
    import json
    files = {}
    for dp, dn, fn in os.walk(root):
        dn[:] = [d for d in dn if d not in (".alpaca", "analytics")]
        for f in fn:
            rel = os.path.relpath(os.path.join(dp, f), root).replace(os.sep, "/")
            if rel not in ("RESUME.md", "MANIFEST.json"):
                with open(os.path.join(dp, f), "rb") as fh:
                    files[rel] = util.sha256_hex(fh.read())
    util.write_text(os.path.join(root, "MANIFEST.json"), json.dumps({"files": files}) + "\n")


@pytest.mark.parametrize("name", ["CLAUDE.md", "AGENTS.md"])
def test_a_line_added_inside_the_boot_block_is_a_local_edit(trees, name, capsys):
    root, source = trees
    for base in (root, source):
        if name != "CLAUDE.md":
            shutil.copy(os.path.join(base, "CLAUDE.md"), os.path.join(base, name))
    _lock_again(root)
    live = util.read_text(os.path.join(root, name))
    util.write_text(os.path.join(root, name), live.replace(manifest.BOOT_END, "my own rule inside the block\n" + manifest.BOOT_END))
    p = upgrade.plan(root, source)
    assert name in p.merge and p.merge_edited == [name] and name in p.local_files
    upgrade.print_plan(p)
    assert "  local edit in the boot block will be replaced by the release's block: %s" % name in capsys.readouterr().out
    # the plan only reports; apply still replaces the block, which is why update saves the file first
    assert name not in p.modified


@pytest.mark.parametrize("name", ["CLAUDE.md", "AGENTS.md"])
def test_an_edit_outside_the_block_or_an_untouched_file_is_not_listed(trees, name):
    root, source = trees
    for base in (root, source):
        if name != "CLAUDE.md":
            shutil.copy(os.path.join(base, "CLAUDE.md"), os.path.join(base, name))
    _lock_again(root)
    p = upgrade.plan(root, source)                         # the file is the shipped bytes: only the release changed the block
    assert name in p.merge and name not in p.merge_edited and name not in p.local_files
    # an owner line OUTSIDE the block, and the block already the incoming one: nothing is lost
    incoming = util.read_text(os.path.join(source, name))
    util.write_text(os.path.join(root, name), "# my notes\n\n" + incoming)
    p = upgrade.plan(root, source)
    assert name in p.merge and name not in p.merge_edited and name not in p.local_files


def test_a_boot_block_edit_is_listed_when_the_project_has_no_lock_and_a_missing_block_loses_nothing(trees):
    root, source = trees                                   # no MANIFEST.json in the live tree
    p = upgrade.plan(root, source)
    assert "CLAUDE.md" in p.merge_edited and "CLAUDE.md" in p.local_files
    util.write_text(os.path.join(root, "CLAUDE.md"), "# no block here, the release appends one\n")
    assert upgrade.plan(root, source).merge_edited == []
    os.remove(os.path.join(root, "CLAUDE.md"))
    assert upgrade.plan(root, source).merge_edited == []


# --------------------------------------------------------------------------- hardening (t-673)
def _within(seconds, fn):
    """Run fn() in a thread and wait at most `seconds`: (thread, {"value"|"error": ...}). A call that
    blocks leaves a daemon thread behind instead of hanging the test run."""
    import threading
    box = {}

    def run():
        try:
            box["value"] = fn()
        except BaseException as e:      # noqa: BLE001 - handed to the test
            box["error"] = e
    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(seconds)
    return t, box


def _unblock_fifo(path):
    """Let a reader that is stuck opening a named pipe go (it sees end of file)."""
    try:
        os.close(os.open(path, os.O_WRONLY | os.O_NONBLOCK))
    except OSError:
        pass


def _hold_project_lock(root):
    """Take the project lock on a file descriptor of our own, as another process would hold it."""
    import fcntl
    os.makedirs(os.path.join(root, ".alpaca"), exist_ok=True)
    fd = os.open(upgrade.lock_path(root), os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return fd


def test_a_held_project_lock_refuses_apply_and_leaves_a_journal_to_its_holder(trees):
    root, source = trees
    before = [_digest(root, rel) for rel in TAGGED]
    fd = _hold_project_lock(root)
    try:
        with pytest.raises(upgrade.UpgradeLocked, match="is running"):
            upgrade.apply(root, source)
        assert not upgrade.pending(root) and [_digest(root, rel) for rel in TAGGED] == before
    finally:
        os.close(fd)
    # the lock is the holder's own: a journal it wrote is not rolled back by another start
    with pytest.raises(upgrade._KillInjected):
        upgrade.apply(root, source, kill_at="stage")
    with open(upgrade._journal_path(root), "rb") as fh:
        journal = fh.read()
    fd = _hold_project_lock(root)
    try:
        assert upgrade.busy(root)
        res = upgrade.recover(root)
        assert res["recovered"] is False and "running" in res["reason"]
        with open(upgrade._journal_path(root), "rb") as fh:
            assert fh.read() == journal
    finally:
        os.close(fd)
    assert not upgrade.busy(root)
    assert upgrade.recover(root)["recovered"] is True and not upgrade.pending(root)
    assert [_digest(root, rel) for rel in TAGGED] == before


def test_the_project_lock_is_reentrant_in_one_thread_and_a_clean_start_makes_no_lock_file(trees):
    root, source = trees
    with upgrade.project_lock(root):
        assert upgrade.busy(root)
        with upgrade.project_lock(root):           # apply inside a caller that holds the lock
            assert upgrade.busy(root)
        assert upgrade.busy(root)
        t, box = _within(5, lambda: upgrade.busy(root))
        assert box["value"] is True                # another thread sees it held
    assert not upgrade.busy(root)
    os.remove(upgrade.lock_path(root))
    assert upgrade.recover(root)["recovered"] is False
    assert not os.path.exists(upgrade.lock_path(root))      # the fast path writes nothing


def test_a_named_pipe_under_a_refresh_path_is_listed_and_never_opened(trees, tmp_path):
    root, source = _locked_trees(trees, tmp_path)
    pipe = os.path.join(root, "alpaca", "pipe")
    os.mkfifo(pipe)
    os.symlink("pipe", os.path.join(root, "alpaca", "pipe-link"))
    try:
        t, box = _within(10, lambda: upgrade.plan(root, source))
        assert not t.is_alive(), "plan() is stuck opening the named pipe"
    finally:
        _unblock_fifo(pipe)
    p = box["value"]
    assert "alpaca/" in p.modified and "alpaca/" not in p.changed
    assert "alpaca/pipe" in p.local_files and "alpaca/pipe-link" in p.local_files
    assert upgrade._file_sha(pipe) is None                  # a special file has no content hash
    assert upgrade._tree_digest(pipe) is not None           # but it still takes part in the digest


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a mode 000 file")
def test_an_unreadable_added_file_and_an_unreadable_claude_md_are_local_edits(trees, tmp_path):
    root, source = _locked_trees(trees, tmp_path)
    secret = os.path.join(root, "alpaca", "secret.py")
    private = os.path.join(root, "alpaca", "private")
    claude = os.path.join(root, "CLAUDE.md")
    util.write_text(secret, "SECRET = 1\n")
    os.makedirs(private)
    util.write_text(os.path.join(private, "inner.py"), "INNER = 1\n")
    try:
        for path in (secret, private, claude):
            os.chmod(path, 0)
        p = upgrade.plan(root, source)
    finally:
        os.chmod(private, 0o755)
        os.chmod(secret, 0o644)
        os.chmod(claude, 0o644)
    assert "alpaca/" in p.modified
    assert "alpaca/secret.py" in p.local_files                # was counted as "not edited" (None == None)
    assert "alpaca/private" in p.local_files                  # a folder the plan cannot read is named
    assert p.merge_edited == ["CLAUDE.md"] and "CLAUDE.md" in p.local_files


def _link_claude_to_agents(root, source):
    for base in (root, source):
        shutil.copy(os.path.join(base, "CLAUDE.md"), os.path.join(base, "AGENTS.md"))
    os.remove(os.path.join(root, "CLAUDE.md"))
    os.symlink("AGENTS.md", os.path.join(root, "CLAUDE.md"))


@pytest.mark.parametrize("kill_at", [None, "backup", "stage", "promote"])
def test_a_claude_md_link_to_agents_md_stays_a_link_through_apply_and_recovery(trees, kill_at, capsys):
    root, source = trees
    _link_claude_to_agents(root, source)
    p = upgrade.plan(root, source)
    assert p.merge_links == ["CLAUDE.md"] and "CLAUDE.md" not in p.merge_edited
    upgrade.print_plan(p)
    assert "kept as a link" in capsys.readouterr().out
    if kill_at is None:
        upgrade.apply(root, source)
    else:
        with pytest.raises(upgrade._KillInjected):
            upgrade.apply(root, source, kill_at=kill_at)
        assert upgrade.recover(root)["recovered"] is True
    link = os.path.join(root, "CLAUDE.md")
    assert os.path.islink(link) and os.readlink(link) == "AGENTS.md"
    agents = util.read_text(os.path.join(root, "AGENTS.md"))
    assert "House rules for OLD must survive." in agents
    assert ("Alpaca boot NEW" if kill_at in (None, "promote") else "Alpaca boot OLD") in agents


def test_links_inside_a_mechanism_folder_are_backed_up_and_restored_as_links(trees):
    root, source = trees
    os.symlink("stub.txt", os.path.join(root, "alpaca", "alias"))
    os.symlink("nowhere-at-all", os.path.join(root, "alpaca", "dangling"))
    with pytest.raises(upgrade._KillInjected):                # a dangling link must not break the backup
        upgrade.apply(root, source, kill_at="stage")
    backup = os.path.join(upgrade._backup_dir(root), "alpaca")
    assert os.path.islink(os.path.join(backup, "alias")) and os.path.islink(os.path.join(backup, "dangling"))
    shutil.rmtree(os.path.join(root, "alpaca"))
    assert upgrade.recover(root)["recovered"] is True
    assert os.readlink(os.path.join(root, "alpaca", "alias")) == "stub.txt"
    assert os.readlink(os.path.join(root, "alpaca", "dangling")) == "nowhere-at-all"


def test_apply_inside_a_caller_that_holds_the_project_lock_does_not_wait_for_itself(trees):
    root, source = trees                       # alpaca update holds the lock for its plan and calls apply inside it
    with upgrade.project_lock(root):
        upgrade.apply(root, source)
        assert upgrade.busy(root)
    assert not upgrade.pending(root) and not upgrade.busy(root)
    assert "NEW" in util.read_text(os.path.join(root, "bin", "alpaca"))


def test_the_recover_verb_is_blocked_while_the_upgrade_it_would_roll_is_running(trees, tmp_path, monkeypatch, capsys):
    import argparse
    monkeypatch.chdir(tmp_path)
    root, source = trees
    with pytest.raises(upgrade._KillInjected):
        upgrade.apply(root, source, kill_at="stage")
    with open(upgrade._journal_path(root), "rb") as fh:
        journal = fh.read()
    args = argparse.Namespace(target=root, source=None, plan=False, recover=True)
    fd = _hold_project_lock(root)
    try:
        assert upgrade._cmd_upgrade(args) == 2
        out = capsys.readouterr().out
        assert "GATE alpaca-upgrade-recover: BLOCKED" in out and "is running now" in out, out
        with open(upgrade._journal_path(root), "rb") as fh:
            assert fh.read() == journal
        # push mode, which recovers a crashed upgrade before it begins one, leaves it alone too
        args = argparse.Namespace(target=root, source=source, plan=False, recover=False)
        assert upgrade._cmd_upgrade(args) == 2
        capsys.readouterr()
        with open(upgrade._journal_path(root), "rb") as fh:
            assert fh.read() == journal
    finally:
        os.close(fd)
    args = argparse.Namespace(target=root, source=None, plan=False, recover=True)
    assert upgrade._cmd_upgrade(args) == 0 and not upgrade.pending(root)
