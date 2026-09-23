"""M4.11 proof: the outbound barrier and the tier rule at the push boundary.

Every claim in the Done-when is driven on a POSITIVE and a NEGATIVE path against a LOCAL BARE
REMOTE (a push to a shared remote stays a human decision: CLAUDE.md boot rule 3):

  * a clean push passes; a push carrying a protected prefix or exact path is BLOCKED;
  * EVERY COMMIT entering the remote is scanned, not just the working tree: a secret deleted from
    the tip still ships in the commit that added it, and is still caught;
  * a sealed term hidden in a committed blob FAILs (the term list is DATA from project.yaml);
  * ANY error in the scan REFUSES the push (fail-closed), never lets it through;
  * the report names DIGESTS, never paths: the protected path string never appears in the report;
  * install writes a real pre-push hook and refuses to clobber a foreign one, and a real `git push`
    to the bare remote is refused by that hook while a clean one succeeds;
  * the blind leak classifier scores against a sealed canary set (canary_score), able to fail as
    well as pass;
  * the push stays a HUMAN decision: the barrier refuses mechanically; deciding to push at all is
    still a review card (M3.5).
"""
import os
import shutil
import stat
import subprocess
import sys

import pytest

from alpaca.clock import FixedClock
from alpaca import util
from alpaca.tests.conftest import REPO
from alpaca.gates import verdict as vc
from alpaca import barrier

TERM = "ZqSealedTokenAlpha"


@pytest.fixture(autouse=True)
def _fixed_clock():
    util.set_clock(FixedClock("2026-01-01T00:00:00+00:00"))
    yield
    util.set_clock(None)


def _run(root, *args, env=None):
    return subprocess.run(["git", "-C", root, *args], capture_output=True, text=True,
                          encoding="utf-8", env=env)


def _commit(root, rel, body, msg):
    p = os.path.join(root, rel)
    os.makedirs(os.path.dirname(p) or root, exist_ok=True)
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(body)
    assert _run(root, "add", "-A").returncode == 0
    assert _run(root, "commit", "-m", msg).returncode == 0
    return _run(root, "rev-parse", "HEAD").stdout.strip()


@pytest.fixture
def repo(tmp_path):
    """A throwaway Alpaca root that is a git work repo with a local BARE remote.

    project.yaml declares tier + protected paths (a prefix and an exact path) and a sealed term.
    """
    root = tmp_path / "work"
    root.mkdir()
    shutil.copy(os.path.join(REPO, "ALPACA-MANIFEST"), root / "ALPACA-MANIFEST")
    # The sealed-term list lives OUTSIDE the committed tree (under gitignored .alpaca/): the answer key
    # never ships with the thing it grades. barrier.terms points leak_audit at it (DATA, Step 3).
    (root / "project.yaml").write_text(
        "name: work\ntier: public\n"
        "barrier:\n"
        "  protected_paths:\n"
        "  - .env\n"
        "  - secrets/\n"
        "  terms: .alpaca/sealed-terms.txt\n", encoding="utf-8")
    (root / ".gitignore").write_text(".alpaca/\n.hookbin/\n", encoding="utf-8")
    (root / ".alpaca").mkdir()
    (root / ".alpaca" / "sealed-terms.txt").write_text(TERM + "\n", encoding="utf-8")
    assert _run(str(root), "-c", "init.defaultBranch=main", "init").returncode == 0
    _run(str(root), "config", "user.email", "t@example.invalid")
    _run(str(root), "config", "user.name", "t")
    _run(str(root), "config", "commit.gpgsign", "false")
    bare = tmp_path / "remote.git"
    assert _run(str(tmp_path), "init", "--bare", str(bare)).returncode == 0
    _run(str(root), "remote", "add", "origin", str(bare))
    _commit(str(root), "README.md", "hello\n", "init")
    return str(root), str(bare)


def _commits(root):
    return barrier.commits_entering(root, "HEAD", None)


# ------------------------------------------------------------------ scan (library form)
def test_clean_push_passes(repo):
    root, _bare = repo
    _commit(root, "src/app.py", "print('ok')\n", "add app")
    res = barrier.scan(root, _commits(root))
    assert res.verdict == vc.PASS


def test_protected_exact_path_blocks(repo):
    root, _bare = repo
    _commit(root, ".env", "SECRET=1\n", "add env")
    res = barrier.scan(root, _commits(root))
    assert res.verdict == vc.BLOCKED and barrier.R_PROTECTED in res.reasons


def test_protected_prefix_blocks(repo):
    root, _bare = repo
    _commit(root, "secrets/key.txt", "private\n", "add key")
    res = barrier.scan(root, _commits(root))
    assert res.verdict == vc.BLOCKED and barrier.R_PROTECTED in res.reasons


def test_history_is_scanned_not_only_the_working_tree(repo):
    root, _bare = repo
    _commit(root, "secrets/key.txt", "private\n", "add key")      # commit 1: carries the secret
    os.remove(os.path.join(root, "secrets", "key.txt"))
    _commit(root, "src/app.py", "print('ok')\n", "remove key, add app")  # commit 2: tree clean now
    assert not os.path.exists(os.path.join(root, "secrets", "key.txt"))
    res = barrier.scan(root, _commits(root))
    # the working tree no longer holds it, but the commit that added it still would enter the remote.
    assert res.verdict == vc.BLOCKED and barrier.R_PROTECTED in res.reasons


def test_sealed_term_in_a_blob_fails(repo):
    root, _bare = repo
    _commit(root, "notes.md", "the value is " + TERM + " do not ship\n", "add notes")
    res = barrier.scan(root, _commits(root))
    assert res.verdict == vc.FAIL and barrier.R_LEAK in res.reasons


def test_scan_error_refuses_the_push(repo):
    root, _bare = repo
    _commit(root, "src/app.py", "print('ok')\n", "add app")
    # a bogus commit sha makes git ls-tree fail; a barrier that cannot look must refuse.
    res = barrier.scan(root, ["deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"])
    assert res.verdict == vc.BLOCKED and barrier.R_SCAN_ERROR in res.reasons


def test_report_names_digests_not_paths(repo):
    root, _bare = repo
    _commit(root, ".env", "SECRET=1\n", "add env")
    res = barrier.scan(root, _commits(root))
    text = barrier.render(res.report)
    # the offending path never appears; a digest does.
    assert ".env" not in text
    assert res.report["blocked"] and res.report["blocked"][0]["path_digest"] in text
    # and the raw path is not stashed anywhere in the structured report either.
    import json
    assert ".env" not in json.dumps(res.report)


# ------------------------------------------------------------------ install + real git push
def test_install_writes_hook_and_refuses_a_foreign_one(repo):
    root, _bare = repo
    p = barrier.install(root)
    assert os.path.isfile(p) and os.path.basename(p) == "pre-push"
    body = util.read_text(p)
    assert "alpaca outbound barrier" in body and "alpaca.barrier prepush" in body
    assert os.stat(p).st_mode & stat.S_IXUSR
    # rename-safe: no absolute path and no folder-name literal baked into the hook.
    assert root not in body and os.path.basename(REPO) not in body
    # idempotent over its own hook; refuses to clobber a foreign one.
    barrier.install(root)
    with open(p, "w", encoding="utf-8") as fh:
        fh.write("#!/bin/sh\necho someone elses hook\n")
    with pytest.raises(RuntimeError):
        barrier.install(root)


def _push_env(root):
    # Give the hook a python3 that is THIS interpreter (so PyYAML and alpaca import), and point it at
    # the throwaway root. PYTHONPATH makes `alpaca` importable from REPO.
    bindir = os.path.join(root, ".hookbin")
    os.makedirs(bindir, exist_ok=True)
    shim = os.path.join(bindir, "python3")
    if not os.path.exists(shim):
        os.symlink(sys.executable, shim)
    env = {**os.environ, "PYTHONPATH": REPO, "CLAUDE_PROJECT_DIR": root,
           "PATH": bindir + os.pathsep + os.environ.get("PATH", "")}
    env.pop("CLAUDE_CONFIG_DIR", None)
    return env


def test_real_push_is_refused_by_the_hook_on_a_protected_path(repo):
    root, _bare = repo
    barrier.install(root)
    env = _push_env(root)
    _commit(root, ".env", "SECRET=1\n", "add env")
    r = subprocess.run(["git", "-C", root, "push", "origin", "main"],
                       capture_output=True, text=True, encoding="utf-8", env=env)
    assert r.returncode != 0, r.stdout + r.stderr           # the hook refused the push
    # the bare remote received nothing on main.
    ls = subprocess.run(["git", "-C", _bare, "rev-parse", "--verify", "main"],
                        capture_output=True, text=True, encoding="utf-8")
    assert ls.returncode != 0


def test_real_clean_push_succeeds(repo):
    root, _bare = repo
    barrier.install(root)
    env = _push_env(root)
    _commit(root, "src/app.py", "print('ok')\n", "add app")
    r = subprocess.run(["git", "-C", root, "push", "origin", "main"],
                       capture_output=True, text=True, encoding="utf-8", env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    ls = subprocess.run(["git", "-C", _bare, "rev-parse", "--verify", "main"],
                        capture_output=True, text=True, encoding="utf-8")
    assert ls.returncode == 0


# ------------------------------------------------------------------ the canary classifier
def test_canary_classifier_scores_against_a_sealed_set(repo, tmp_path):
    root, _bare = repo
    sealed = tmp_path / "sealed.md"
    sealed.write_text("| 1 | CANARY |\n| 2 | CONTROL |\n| 3 | CANARY |\n| 4 | CONTROL |\n",
                      encoding="utf-8")
    good = tmp_path / "good.md"
    good.write_text("| 1 | SEALED |\n| 2 | PUBLIC |\n| 3 | QUARANTINE |\n| 4 | PUBLIC |\n",
                    encoding="utf-8")
    assert barrier.score_canary(root, str(sealed), str(good))[0] == vc.PASS
    bad = tmp_path / "bad.md"
    bad.write_text("| 1 | PUBLIC |\n| 2 | PUBLIC |\n| 3 | SEALED |\n| 4 | PUBLIC |\n",
                   encoding="utf-8")
    assert barrier.score_canary(root, str(sealed), str(bad))[0] == vc.FAIL


# ------------------------------------------------------------------ the push stays human
def test_barrier_is_mechanical_and_leaves_the_decision_to_a_review_card(repo):
    # The barrier only produces a verdict; it never pushes and never records a decision. The
    # decision to push at all is a review card (M3.5), asserted here by the absence of any writer:
    # scan is a pure read of the object store.
    root, _bare = repo
    _commit(root, "src/app.py", "print('ok')\n", "add app")
    before = _run(_bare, "rev-parse", "--verify", "main").returncode
    barrier.scan(root, _commits(root))
    after = _run(_bare, "rev-parse", "--verify", "main").returncode
    assert before != 0 and after != 0        # scanning pushed nothing to the remote
