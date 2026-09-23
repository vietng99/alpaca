"""M1.4 - containment instruments: workspace guard and git-as-execution scan.

Companion module to tests/test_floor_canary.py (the M1.4 proof). This module drives the
two instruments directly on both the positive and the negative path:

  alpaca/gates/workspace_guard.check(root, targets)   NO-TMP + realpath containment
  alpaca/gates/git_containment.scan(repo_dir)         core.hooksPath / diff.*.textconv /
                                                  filter.*.clean|smudge as execution

Both instruments obey the M1.3 verdict contract (they import alpaca.gates.verdict and build
any CLI boundary through make_parser); tests/test_verdict_contract.py already guards that
alpaca/gates/ carries no raw-argparse or restated-map boundary, so a new boundary that broke
the contract would fail there.
"""
import os

import pytest

from alpaca.gates import git_containment, workspace_guard


# ------------------------------------------------------------------ workspace_guard
def test_workspace_guard_passes_a_contained_target(tmp_path):
    root = tmp_path / "root"
    (root / "sub").mkdir(parents=True)
    inside = root / "sub" / "spec.md"
    inside.write_text("# spec\n", encoding="utf-8")
    # pytest's tmp_path is under /tmp; isolate the containment property from the NO-TMP one.
    NO_TMP = frozenset()
    findings = workspace_guard.check(str(root), [str(inside), str(root)], tmp_prefixes=NO_TMP)
    assert findings == [], findings
    assert workspace_guard.verdict_of(findings) == workspace_guard.verdict.PASS


def test_workspace_guard_flags_a_target_outside_the_root(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    sibling = tmp_path / "sibling"
    sibling.mkdir()
    outside = sibling / "their_file.txt"
    outside.write_text("not ours\n", encoding="utf-8")
    findings = workspace_guard.check(str(root), [str(outside)], tmp_prefixes=frozenset())
    tokens = [tok for tok, _p, _d in findings]
    assert workspace_guard.R_TARGET_OUTSIDE in tokens, findings
    assert workspace_guard.verdict_of(findings) == workspace_guard.verdict.BLOCKED


def test_workspace_guard_flags_a_symlink_that_escapes_the_root(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("foreign\n", encoding="utf-8")
    link = root / "looks_internal.txt"
    try:
        os.symlink(str(outside), str(link))
    except (OSError, NotImplementedError, AttributeError):
        pytest.skip("no symlink support on this platform")
    findings = workspace_guard.check(str(root), [str(link)], tmp_prefixes=frozenset())
    tokens = [tok for tok, _p, _d in findings]
    assert workspace_guard.R_TARGET_OUTSIDE in tokens, findings


def test_workspace_guard_blocks_a_root_under_tmp():
    import tempfile
    tmp_root = tempfile.gettempdir()
    findings = workspace_guard.check(tmp_root, [])
    tokens = [tok for tok, _p, _d in findings]
    assert workspace_guard.R_ROOT_UNDER_TMP in tokens, findings


def test_workspace_guard_blocks_an_absent_root(tmp_path):
    findings = workspace_guard.check(str(tmp_path / "no_such_root"), [])
    tokens = [tok for tok, _p, _d in findings]
    assert workspace_guard.R_ROOT_ABSENT in tokens, findings


def test_workspace_guard_atomic_write_stays_inside_root(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    workspace_guard.atomic_write(str(root), "notes/out.txt", "hello\n")
    written = root / "notes" / "out.txt"
    assert written.read_text(encoding="utf-8") == "hello\n"
    # No temp residue left behind next to the target.
    leftovers = [n for n in os.listdir(str(root / "notes")) if n != "out.txt"]
    assert leftovers == [], leftovers


def test_workspace_guard_atomic_write_refuses_outside_root(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    with pytest.raises(Exception):
        workspace_guard.atomic_write(str(root), "../escape.txt", "no\n")
    assert not (tmp_path / "escape.txt").exists()


# ------------------------------------------------------------------ git_containment
def _make_repo(base, config_body):
    repo = os.path.join(base, "repo")
    gitdir = os.path.join(repo, ".git")
    os.makedirs(gitdir, exist_ok=True)
    with open(os.path.join(gitdir, "config"), "w", encoding="utf-8") as fh:
        fh.write(config_body)
    return repo


DANGEROUS = (
    "[core]\n"
    "\trepositoryformatversion = 0\n"
    "\thooksPath = /tmp/evil-hooks\n"
    "[diff \"spelling\"]\n"
    "\ttextconv = /usr/bin/strings\n"
    "[filter \"secret\"]\n"
    "\tclean = ./scrub.sh\n"
    "\tsmudge = ./expand.sh\n"
)


def test_scan_flags_core_hookspath(tmp_path):
    repo = _make_repo(str(tmp_path), DANGEROUS)
    findings = git_containment.scan(repo)
    tokens = [tok for tok, _f, _ln, _d in findings]
    assert git_containment.R_HOOKSPATH in tokens, findings


def test_scan_flags_diff_textconv(tmp_path):
    repo = _make_repo(str(tmp_path), DANGEROUS)
    findings = git_containment.scan(repo)
    tokens = [tok for tok, _f, _ln, _d in findings]
    assert git_containment.R_TEXTCONV in tokens, findings


def test_scan_flags_filter_clean_and_smudge(tmp_path):
    repo = _make_repo(str(tmp_path), DANGEROUS)
    findings = git_containment.scan(repo)
    tokens = [tok for tok, _f, _ln, _d in findings]
    assert git_containment.R_FILTER_CLEAN in tokens, findings
    assert git_containment.R_FILTER_SMUDGE in tokens, findings


def test_scan_reports_file_and_line_for_each_finding(tmp_path):
    repo = _make_repo(str(tmp_path), DANGEROUS)
    findings = git_containment.scan(repo)
    assert findings, "a repo carrying every dangerous key produced no findings"
    for tok, fpath, line, detail in findings:
        assert os.path.isfile(fpath), fpath
        assert isinstance(line, int) and line >= 1, (tok, line)
        # the reported line really carries the offending key
        with open(fpath, encoding="utf-8") as fh:
            text = fh.read().splitlines()
        assert 1 <= line <= len(text)


def test_scan_verdict_is_blocked_on_a_dangerous_repo(tmp_path):
    repo = _make_repo(str(tmp_path), DANGEROUS)
    findings = git_containment.scan(repo)
    assert git_containment.verdict_of(findings) == git_containment.verdict.BLOCKED


def test_scan_is_clean_on_an_ordinary_repo(tmp_path):
    clean = (
        "[core]\n"
        "\trepositoryformatversion = 0\n"
        "\tfilemode = true\n"
        "\tbare = false\n"
        "[remote \"origin\"]\n"
        "\turl = https://example.invalid/x.git\n"
    )
    repo = _make_repo(str(tmp_path), clean)
    findings = git_containment.scan(repo)
    assert findings == [], findings
    assert git_containment.verdict_of(findings) == git_containment.verdict.PASS


def test_scan_resolves_a_gitdir_pointer_file(tmp_path):
    # A worktree checkout: `.git` is a file pointing at the real gitdir.
    real_gitdir = tmp_path / "real_gitdir"
    real_gitdir.mkdir()
    (real_gitdir / "config").write_text(
        "[core]\n\thooksPath = /tmp/x\n", encoding="utf-8")
    wt = tmp_path / "worktree"
    wt.mkdir()
    (wt / ".git").write_text("gitdir: %s\n" % real_gitdir, encoding="utf-8")
    findings = git_containment.scan(str(wt))
    tokens = [tok for tok, _f, _ln, _d in findings]
    assert git_containment.R_HOOKSPATH in tokens, findings


def test_scan_two_key_override_suppresses_a_finding(tmp_path):
    repo = _make_repo(str(tmp_path), "[core]\n\thooksPath = /tmp/x\n")
    # A single key is not enough; the override must carry both keys.
    partial = git_containment.scan(repo, overrides=[git_containment.R_HOOKSPATH])
    assert any(tok == git_containment.R_HOOKSPATH for tok, *_ in partial), partial
    both = git_containment.scan(
        repo, overrides=[git_containment.OVERRIDE_CONFIRM, git_containment.R_HOOKSPATH])
    assert all(tok != git_containment.R_HOOKSPATH for tok, *_ in both), both
