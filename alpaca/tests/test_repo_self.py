import os, shutil, subprocess, sys, uuid
from alpaca.tests.conftest import REPO
from alpaca.tests.test_acceptance_m0 import _ignore
from alpaca import doctor, manifest

_SKIP_DIRS = (".git", "__pycache__", ".venv", ".pytest_cache", "tools", "out")
# product-name prose is allowed to say the name in README.md/CLAUDE.md and in the prose-only
# design/docs trees (spec docs, inventories); the rename-safe rule binds code, config, hooks
# and paths, not prose (T13 ruling, progress.md).
_PROSE_DIRS = ("design", "docs")
# generated per-project state: project.yaml's cwd_history (and, if it is ever scanned,
# intents/queue.md's header) holds the project's own absolute root path by design (spec
# Q20 rename-safety keys on the project id, with cwd history recorded); the folder's own
# name legitimately appears there in every onboarded copy. The em-dash check still applies.
_GENERATED_STATE = ("project.yaml", os.path.join("intents", "queue.md"))


def test_repo_itself_is_a_valid_alpaca_root():
    p = subprocess.run(
        [sys.executable, "-m", "alpaca", "doctor"], cwd=REPO, capture_output=True, text=True,
        encoding="utf-8", env={**os.environ, "CLAUDE_PROJECT_DIR": REPO})
    assert p.returncode in (0, 1), p.stdout + p.stderr     # warn allowed: not onboarded
    assert "ERROR" not in p.stdout


def _mechanism_files(root):
    """Every file under a [mechanism] path in ALPACA-MANIFEST (parsed the way alpaca.doctor does),
    minus the [memory] paths nested beneath one: host-local runtime files (tool locks, build
    outputs) are absent from a fresh clone and never ship, so the scan does not read them."""
    mech = doctor._manifest_paths(root)["mechanism"]
    memory = manifest.memory_ignore(root)
    out = []
    for rel in mech:
        path = os.path.join(root, rel)
        if os.path.isdir(path):
            for dp, dn, fn in os.walk(path):
                gone = set(memory(dp, dn + fn))
                dn[:] = [d for d in dn if d not in _SKIP_DIRS and d not in gone]
                for f in fn:
                    if f not in gone:
                        out.append(os.path.join(dp, f))
        elif os.path.isfile(path):
            out.append(path)
    return out


def test_no_em_dash_and_no_folder_name_in_shipped_files():
    bad = []
    for path in _mechanism_files(REPO):
        rel = os.path.relpath(path, REPO)
        f = os.path.basename(path)
        top = rel.split(os.sep, 1)[0]
        try:
            with open(path, encoding="utf-8") as fh:
                txt = fh.read()
        except (UnicodeDecodeError, OSError):
            continue
        has_em_dash = "\u2014" in txt
        # Product branding is allowed. The running user's home path is not portable.
        leaks_folder_name = (rel not in _GENERATED_STATE and os.path.join(os.path.expanduser("~"), "") in txt)
        if has_em_dash or leaks_folder_name:
            bad.append(rel)
    assert bad == [], bad


def test_self_scan_passes_in_an_onboarded_copy(tmp_path):
    # the folder/project name is built at runtime, not written as a literal anywhere in this
    # file: a hardcoded name here would itself become a "folder name" match once this very
    # file is copied into the project it names, which is a self-reference bug, not the one
    # under test (project.yaml's cwd_history leaking the copy's own absolute path).
    name = "selfscan-" + uuid.uuid4().hex[:8]
    proj = tmp_path / name
    shutil.copytree(REPO, proj, ignore=_ignore)
    cfg = tmp_path / "cfg"; cfg.mkdir()
    env = {**os.environ, "CLAUDE_PROJECT_DIR": str(proj), "CLAUDE_CONFIG_DIR": str(cfg)}
    r = subprocess.run(
        [sys.executable, "-m", "alpaca", "onboard", "--name", name, "--who", "alex:owner",
         "--what", "a tiny cli"],
        cwd=proj, env=env, capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, r.stdout + r.stderr
    assert os.path.isfile(proj / "project.yaml")
    # target the two non-recursive tests by node id: the whole file would re-run this very
    # test inside the copy, which would copy+onboard+run again, unbounded.
    r = subprocess.run(
        [sys.executable, "-m", "pytest", "-q",
         "alpaca/tests/test_repo_self.py::test_repo_itself_is_a_valid_alpaca_root",
         "alpaca/tests/test_repo_self.py::test_no_em_dash_and_no_folder_name_in_shipped_files"],
        cwd=proj, env=env, capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, r.stdout + r.stderr
