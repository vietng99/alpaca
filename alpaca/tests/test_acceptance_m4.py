"""M4.17 -- M4 acceptance: a fresh repo to a verified package.

One acceptance module drives every M4 mechanism through its REAL entry point, on throwaway roots
under pytest's tmp dirs. The live `.alpaca/` is never touched and no `alpaca` write verb ever runs against
it: every install, onboard, op and upgrade below lands in a disposable tree.

"Install by copy" is exactly that: the harness tree is copied into a target with no installer step,
minus the memory class (`.alpaca/`, `RESUME.md`, `analytics/`) which is this project's own runtime
state and never travels (alpaca.manifest), and minus version control and byte caches. The human-owned
identity `project.yaml` (P-001) is re-authored by onboarding, so a fresh copy leaves it out and the
tree reads as true first contact (alpaca.adopt.detect FRESH); a used copy that carried it in would read
as a second-machine clone and onboarding would refuse.

The Done-when clauses, each proved below:

  * install by copy into a FRESH repo and into an EXISTING repo, no installer step;
  * the existing-repo install surfaces the collision report and writes zero bytes to any colliding
    path (alpaca.manifest.collisions is read-only; the copy skips every path it names);
  * `alpaca init` + onboarding run in each install, through the real CLI;
  * the fresh install PASSES boot-check --full: the WHOLE worst-code fold equals vc.PASS, not merely
    that the heavy rows are green, and the per-instrument census rows are each vc.PASS as well. The
    regression row is re-entrancy short-circuited by ALPACA_REGRESSION_SUITE_ACTIVE=1 (boot-check runs
    the pytest suite as its last row, and this module runs inside pytest);
  * the manifest verifies (gen_manifest --write then --verify is clean over the mechanism class);
  * the manual gate passes on all seven M4.16 clauses (build_manual_html.gate == []);
  * `docs/manual.html` opens through html.parser with zero bytes above 0x7F and carries the charset
    and viewport metas;
  * `alpaca op close` for the M4 op refuses (exit 3 naming the missing manual-phone-read ref) while the
    owner-countersign row is absent, succeeds once it is present, and is indifferent to the
    render-neutralisation ref (a close with only that ref still exits 3). The refusal is asserted
    mechanically; no test asserts a phone was used (owner countersign, G3);
  * `alpaca upgrade` from an older copy refreshes the mechanism class and leaves the memory class
    (project.yaml, RESUME.md and the .alpaca/ record) intact in meaning.

Every open() passes encoding="utf-8"; every subprocess passes text=True, encoding="utf-8". No em
dash anywhere; the harness folder name and any absolute path to it are never written -- the shared
tree is derived from alpaca.tests.conftest.REPO, which is derived from __file__.
"""
import hashlib
import importlib.util
import os
import shutil
import subprocess
import sys
from html.parser import HTMLParser

import pytest

from alpaca import db, manifest, upgrade, util
from alpaca.clock import FixedClock
from alpaca.gates import contract, verdict as vc
from alpaca.phase import doors
from alpaca.tests.conftest import REPO

SETUP = os.path.join(REPO, "setup")
GEN_MANIFEST = os.path.join(SETUP, "gen_manifest.py")

# The memory class never travels to another project (alpaca.manifest); version control and byte caches
# are not part of a copy either. project.yaml is handled specially (see the module docstring).
_MEMORY = {".alpaca", "RESUME.md", "analytics"}
_SKIP = {".git", ".pytest_cache", "__pycache__"} | _MEMORY
_IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc")


def _ignore_with_memory(src):
    """Byte caches by pattern, plus every [memory] path of `src`'s manifest anchored at `src`, so a
    memory path nested under a mechanism dir (a host-local tool install) never enters a copy."""
    memory = manifest.memory_ignore(src)

    def _ignore(dirpath, names):
        return set(_IGNORE(dirpath, names)) | set(memory(dirpath, names))

    return _ignore

# The three heavy composed rows boot-check appends under --full; everything else in the row list is
# a per-instrument census row.
_HEAVY_ROWS = ("wiki-lint", "wiki-mutation-oracle", "regression-suite")


def _load(basename, modname):
    """Load a setup/ tool as a module, exactly as the M4.15/M4.16 unit suites do."""
    spec = importlib.util.spec_from_file_location(modname, os.path.join(SETUP, basename))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


BC = _load("boot-check.py", "alpaca_m4_boot_check")
BUILD_MANUAL = _load("build_manual_html.py", "alpaca_m4_build_manual")


# --------------------------------------------------------------------------- install by copy
def _copy_tree_minus_memory(src, dst, *, skip_project_yaml=False):
    """Install by copy: every top-level source path except the memory class, version control and
    byte caches, so the target carries the whole harness (alpaca/, setup/, docs/, contracts/, ...) and
    boot-check --full and the manual gate can be driven over it. project.yaml is left out when the
    target is a fresh project that will re-author it through onboarding."""
    os.makedirs(dst, exist_ok=True)
    ignore = _ignore_with_memory(src)
    names = sorted(os.listdir(src))
    dropped = ignore(src, names)
    for name in names:
        if name in _SKIP or name in dropped:
            continue
        if skip_project_yaml and name == "project.yaml":
            continue
        s = os.path.join(src, name)
        d = os.path.join(dst, name)
        if os.path.isdir(s):
            shutil.copytree(s, d, ignore=ignore)
        else:
            os.makedirs(os.path.dirname(d) or ".", exist_ok=True)
            shutil.copy2(s, d)


def _install_mechanism(src, dst, *, skip=()):
    """Install only the manifest mechanism class into an existing target, skipping every path in
    `skip` (the collision report) and the human-owned project.yaml. Returns the paths written."""
    skip = set(skip)
    ignore = _ignore_with_memory(src)
    written = []
    for rel in manifest.mechanism(src):
        if rel in skip:
            continue
        n = rel.rstrip("/")
        if n == "project.yaml":
            continue
        s = os.path.join(src, n)
        d = os.path.join(dst, n)
        if not os.path.lexists(s):
            continue
        if os.path.isdir(s):
            shutil.copytree(s, d, ignore=ignore, dirs_exist_ok=True)
        else:
            os.makedirs(os.path.dirname(d) or ".", exist_ok=True)
            shutil.copy2(s, d)
        written.append(rel)
    return written


def _digest(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def _alpaca(root, cfg, *args):
    """Drive the COPIED harness through its real CLI (`python3 -m alpaca`), with the project root and a
    throwaway config dir supplied through the environment. PYTHONPATH points at the install itself,
    so the copied alpaca package is exercised, never the live source tree."""
    env = {**os.environ, "CLAUDE_PROJECT_DIR": root, "CLAUDE_CONFIG_DIR": cfg,
           "PYTHONPATH": root, "PYTHONDONTWRITEBYTECODE": "1"}
    return subprocess.run([sys.executable, "-m", "alpaca", *args], cwd=root, env=env,
                          capture_output=True, text=True, encoding="utf-8")


# --------------------------------------------------------------------------- the fresh install
@pytest.fixture(scope="module")
def fresh_install(tmp_path_factory):
    """A fresh repository: the harness copied in (no installer step), then alpaca init and onboarding
    run through the real CLI. Module-scoped so the one heavy boot-check --full sweep is paid once."""
    base = tmp_path_factory.mktemp("m4-fresh")
    root = str(base / "fresh")
    cfg = str(base / "cfg")
    os.makedirs(cfg)
    _copy_tree_minus_memory(REPO, root, skip_project_yaml=True)
    assert os.path.isfile(os.path.join(root, "ALPACA-MANIFEST"))
    assert not os.path.exists(os.path.join(root, ".alpaca")), "the memory class never travels"
    assert not os.path.isfile(os.path.join(root, "project.yaml")), "identity is re-authored"

    assert _alpaca(root, cfg, "init").returncode == vc.PASS
    r = _alpaca(root, cfg, "onboard", "--name", "m4-fresh", "--who", "v:owner",
            "--what", "M4 acceptance install")
    assert r.returncode == vc.PASS, r.stdout + r.stderr
    assert os.path.isfile(os.path.join(root, "project.yaml")), "onboarding writes project.yaml"
    return {"root": root, "cfg": cfg}


# ------------------------------------- (1) fresh install passes boot-check --full, verifies, gates
def test_fresh_install_passes_boot_check_full_and_verifies(fresh_install, monkeypatch):
    root = fresh_install["root"]
    cfg = fresh_install["cfg"]

    # boot-check --full runs the pytest suite as its LAST row via regression_suite; this module IS
    # inside pytest, so set the re-entrancy guard. It rides in os.environ, so the in-process
    # regression row short-circuits PASS and the subprocessed instruments inherit it too.
    monkeypatch.setenv("ALPACA_REGRESSION_SUITE_ACTIVE", "1")

    # the WHOLE fold. worst() over EVERY composed row (BLOCKED > FAIL > PAUSED > PASS) must be PASS:
    # a strict all-green, not merely the heavy rows being green. This is exactly what boot_check.run
    # folds, driven through the real entry point.
    rows = BC.rows(root, full=True)
    names = [n for n, _ in rows]
    assert names[-1] == "regression-suite", names
    fold = contract.worst([c for _n, c in rows])
    assert fold == vc.PASS, [(n, vc.name_of(c)) for n, c in rows if c != vc.PASS]
    assert BC.run(root, full=True) == vc.PASS, "boot_check.run(freshroot, full=True) == vc.PASS"

    # and, separately, every per-instrument census row is itself vc.PASS (the fix that lets a passing
    # selftest fold PASS): confirm the instrument rows, not only the folded verdict.
    instrument = [(n, c) for n, c in rows if n not in _HEAVY_ROWS]
    assert instrument, "the census population is non-empty on the fresh install"
    assert all(c == vc.PASS for _n, c in instrument), \
        [(n, vc.name_of(c)) for n, c in instrument if c != vc.PASS]

    # verify the manifest: the per-file digest map over the mechanism class writes clean and then
    # re-verifies clean over the freshly installed tree.
    w = subprocess.run([sys.executable, GEN_MANIFEST, "--write", "--root", root],
                       capture_output=True, text=True, encoding="utf-8")
    assert w.returncode == 0, w.stdout + w.stderr
    v = subprocess.run([sys.executable, GEN_MANIFEST, "--verify", "--root", root],
                       capture_output=True, text=True, encoding="utf-8")
    assert v.returncode == 0, v.stdout + v.stderr
    assert "OK -- tree matches manifest" in v.stdout

    # the manual gate passes on all seven M4.16 clauses over the installed tree.
    assert BUILD_MANUAL.gate(root) == [], "the shipped manual must pass every clause"

    # the record is still an intact hash chain, and alpaca verify agrees.
    assert _alpaca(root, cfg, "verify").returncode == vc.PASS


# ------------------------------------------------- (2) docs/manual.html opens through html.parser
def test_installed_manual_html_is_ascii_and_parses_with_the_metas(fresh_install):
    path = os.path.join(fresh_install["root"], "docs", "manual.html")
    with open(path, "rb") as fh:
        raw = fh.read()
    assert not any(b > 0x7F for b in raw), "docs/manual.html carries a byte above 0x7F"

    class _Collect(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.metas = []

        def handle_starttag(self, tag, attrs):
            if tag == "meta":
                self.metas.append(dict(attrs))

    parser = _Collect()
    parser.feed(raw.decode("ascii"))     # pure-ASCII bytes decode cleanly; html.parser accepts them
    parser.close()
    charset = [m for m in parser.metas if "charset" in m]
    viewport = [m for m in parser.metas if m.get("name") == "viewport"]
    assert charset and charset[0]["charset"].lower() == "utf-8", parser.metas
    assert viewport and "width=device-width" in viewport[0].get("content", ""), parser.metas


# ------------------------------------------- (3) the M4 op close countersign gate (Step 4)
def test_m4_op_close_countersign_gate(fresh_install):
    root = fresh_install["root"]
    cfg = fresh_install["cfg"]

    # open the M4 milestone op through the real CLI (the milestone IS the op; its intent names M4).
    assert _alpaca(root, cfg, "op", "new", "M4 milestone: packaging and the user manual",
               "--done-when", "boot-check green and the manual gate passes").returncode == vc.PASS

    # (a) with no owner-countersign row it refuses: exit 3, naming the missing manual-phone-read ref.
    r = _alpaca(root, cfg, "op", "close", "op-001", "--basis", "done")
    assert r.returncode == vc.PAUSED, r.stdout + r.stderr
    assert "manual-phone-read" in r.stdout, r.stdout

    # (b) the two refs do not substitute: a close with ONLY the render-neutralisation row present
    # (that ref belongs to the release door, M4.9/M4.15) must still exit 3.
    conn = db.connect(root)
    doors.record_countersign(conn, "render-neutralisation", ["RESUME.md", "analytics/index.html"],
                             pointer="journal:owner read both surfaces", root=root)
    conn.close()
    r = _alpaca(root, cfg, "op", "close", "op-001", "--basis", "done")
    assert r.returncode == vc.PAUSED, r.stdout + r.stderr
    assert "manual-phone-read" in r.stdout, r.stdout

    # (c) once the owner records the manual-phone-read countersign the M4 op closes. The refusal is
    # asserted mechanically above; this asserts only that the recorded row lets the close proceed,
    # never that a phone was used (owner countersign, G3).
    conn = db.connect(root)
    doors.record_countersign(conn, "manual-phone-read",
                             ["iPhone 15, Safari, file://, 2026-09-17"],
                             pointer="journal:owner read the manual on a phone", root=root)
    conn.close()
    r = _alpaca(root, cfg, "op", "close", "op-001", "--basis", "done")
    assert r.returncode == vc.PASS, r.stdout + r.stderr


# ----------------------------------------- (4) existing-repo install: collisions, zero writes
def _make_existing_repo(base):
    """A product repository that already carries its own tree AND, by coincidence, two paths the
    harness owns as mechanism (CLAUDE.md and pytest.ini)."""
    root = base / "existing"
    for d in ("tests", "design", "src"):
        (root / d).mkdir(parents=True)
    (root / "tests" / "test_app.py").write_text("def test_app():\n    assert True\n",
                                                encoding="utf-8")
    (root / "src" / "app.py").write_text("x = 1\n", encoding="utf-8")
    (root / "CLAUDE.md").write_text("# Product boot\n\nour own house rules must survive.\n",
                                    encoding="utf-8")
    (root / "pytest.ini").write_text("[pytest]\n# the product's own config\n", encoding="utf-8")
    return str(root)


def test_existing_repo_install_surfaces_collisions_and_writes_nothing(tmp_path):
    root = _make_existing_repo(tmp_path)
    cfg = str(tmp_path / "cfg")
    os.makedirs(cfg)

    # the collision report is read-only and names only mechanism paths that already exist under the
    # target. Here the product's own CLAUDE.md and pytest.ini collide.
    report = manifest.collisions(REPO, root)
    assert set(report) == {"CLAUDE.md", "pytest.ini"}, report
    assert set(report) <= set(manifest.classes(REPO)["mechanism"])

    # digests of the colliding paths and one product file, before the install.
    watched = {rel: _digest(os.path.join(root, rel)) for rel in report}
    watched["src/app.py"] = _digest(os.path.join(root, "src", "app.py"))

    # install the mechanism class, skipping every colliding path: zero bytes are written to any of
    # them, and the product's own files are untouched.
    written = _install_mechanism(REPO, root, skip=report)
    assert "alpaca/" in written and "CLAUDE.md" not in written and "pytest.ini" not in written
    for rel, before in watched.items():
        assert _digest(os.path.join(root, rel)) == before, "%s must not be written" % rel

    # a copy-in with no installer step still leaves a runnable harness: the manifest travelled, so
    # alpaca init and onboarding run in the existing repo too. A real .git makes onboarding sense the
    # existing-repo case (Q19) rather than a fresh tree.
    assert os.path.isfile(os.path.join(root, "ALPACA-MANIFEST"))
    subprocess.run(["git", "-C", root, "init", "-b", "main"],
                   capture_output=True, text=True, encoding="utf-8")
    assert _alpaca(root, cfg, "init").returncode == vc.PASS
    r = _alpaca(root, cfg, "onboard", "--name", "existing-product", "--who", "v:owner",
            "--what", "adopting the harness into an existing repo")
    assert r.returncode == vc.PASS, r.stdout + r.stderr
    assert os.path.isfile(os.path.join(root, "project.yaml"))


# --------------------------------------------- (5) upgrade from an older copy keeps the memory class
def test_upgrade_from_an_older_copy_leaves_the_memory_class_intact(tmp_path):
    older = str(tmp_path / "older")
    # an older live project: the whole harness copied in, INCLUDING its own human-owned identity and
    # a real record, then aged with a stale mechanism residue the newer source no longer carries.
    _copy_tree_minus_memory(REPO, older, skip_project_yaml=False)
    util.write_text(os.path.join(older, "contracts", "OLD_RESIDUE.txt"),
                    "a stale mechanism file the newer harness dropped\n")
    util.write_text(os.path.join(older, "project.yaml"),
                    "name: older-product\nid: keep-this-identity\n")
    util.write_text(os.path.join(older, "RESUME.md"), "# older resume, do not touch\n")
    conn = db.connect(older)                       # a real .alpaca/ record with a hash chain
    for i in range(3):
        db.append_event(conn, session="s", actor="alpaca", kind="seed", data={"i": i})
    ok, _ = db.verify_chain(conn)
    n_before = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    conn.close()
    assert ok and n_before == 3

    project_before = util.read_text(os.path.join(older, "project.yaml"))
    resume_before = util.read_text(os.path.join(older, "RESUME.md"))

    # upgrade the older copy from the newer source harness: a clean copy of REPO, mechanism only,
    # the way a distribution arrives. A used source root also holds its own memory class (a local
    # tool install under a mechanism dir), which is not part of what an upgrade ships.
    newer = str(tmp_path / "newer")
    _copy_tree_minus_memory(REPO, newer, skip_project_yaml=False)
    res = upgrade.apply(older, newer, clock=FixedClock(step=1))
    assert res["ok"]

    # the mechanism class was refreshed: the stale residue the newer harness does not carry is gone.
    assert not os.path.isfile(os.path.join(older, "contracts", "OLD_RESIDUE.txt")), \
        "the mechanism class must be refreshed from source"

    # the memory class is intact in meaning: project.yaml preserved (not clobbered), RESUME.md
    # unchanged, and the record still verifies with the same event count.
    assert util.read_text(os.path.join(older, "project.yaml")) == project_before
    assert util.read_text(os.path.join(older, "RESUME.md")) == resume_before
    conn = db.connect(older)
    ok, reason = db.verify_chain(conn)
    n_after = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    conn.close()
    assert ok, reason
    assert n_after == n_before, "the .alpaca/ record survives the upgrade unchanged in meaning"
