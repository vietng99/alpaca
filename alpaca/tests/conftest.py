import os, shutil, pytest

# Tests copy the repository, project.yaml and its disk: block included, and run the session hooks
# in those copies; a detached disk sweep (alpaca/disk.py) must never start from a test run.
os.environ["ALPACA_DISK_SWEEP"] = "0"

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))


def shipped_project_yaml(repo=REPO):
    """The project.yaml a release ships. A private development tree names its shipped template
    as the release overlay for project.yaml; a clean distribution ships project.yaml itself."""
    import yaml
    path = os.path.join(repo, "project.yaml")
    try:
        with open(path, encoding="utf-8") as fh:
            doc = yaml.safe_load(fh) or {}
    except (OSError, yaml.YAMLError):
        return path
    overlay = ((doc.get("release") or {}).get("overlays") or {}).get("project.yaml")
    if isinstance(overlay, str) and os.path.isfile(os.path.join(repo, overlay)):
        return os.path.join(repo, overlay)
    return path


SHIPPED_PROJECT_YAML = shipped_project_yaml()


@pytest.fixture(autouse=True, scope="session")
def contained_tempdir(tmp_path_factory):
    """Route every tempfile.mkdtemp() of the run, in this process and in child processes, into one
    dir under pytest's basetemp, and delete it when the session ends.

    Gate selftests, the wiki tests and the CLI subprocesses create throwaway stores with
    tempfile.mkdtemp() and never remove them; they piled up in the RAM-backed /tmp and filled
    zram swap on the development host (2026-10-02). The dir stays on the same filesystem as basetemp, so the
    sqlite-heavy tests keep their RAM speed.
    """
    import tempfile
    base = str(tmp_path_factory.mktemp("tempfile", numbered=False))
    old_env, old_tempdir = os.environ.get("TMPDIR"), tempfile.tempdir
    os.environ["TMPDIR"] = base
    tempfile.tempdir = base
    yield base
    tempfile.tempdir = old_tempdir
    if old_env is None:
        os.environ.pop("TMPDIR", None)
    else:
        os.environ["TMPDIR"] = old_env
    shutil.rmtree(base, ignore_errors=True)

# Integration glue for the vendored wiki suite. Wiki tests authored across M2.8-M2.16 install
# sys.modules.setdefault(...) stand-ins for wiki modules that were not vendored yet at authoring
# time. Every such module is now real, so import them here in the root conftest (loaded before any
# test module) and each later setdefault becomes a no-op. Without this a fake bare package (for
# example "alpaca.wiki.providers" with no submodules) installed by an earlier-collected test would
# shadow the real package and break a real import such as `from alpaca.wiki.providers import base`.
import importlib as _importlib

for _name in (
    "alpaca.wiki.engine",
    "alpaca.wiki.engine.templates",
    "alpaca.wiki.engine.compartment",
    "alpaca.wiki.ingest.resolve",
    "alpaca.wiki.ingest.reconcile",
    "alpaca.wiki.providers",
    "alpaca.wiki.providers.base",
    "alpaca.wiki.providers.deterministic",
    "alpaca.wiki.providers.registry",
    "alpaca.wiki.store.vec",
):
    try:
        _importlib.import_module(_name)
    except Exception:
        pass

@pytest.fixture(autouse=True)
def no_ambient_session(monkeypatch):
    """Every test runs with no ambient session id.

    The suite runs inside a live agent session: Claude Code exports CLAUDE_CODE_SESSION_ID into
    every Bash call and the SessionStart hook exports ALPACA_SESSION_ID. `alpaca.cli.main` reads both
    when no --session is passed (that is the point of op-006), so without this fixture a verb a
    test ran would be attributed to the session that ran pytest, and a test asserting the `cli`
    fallback would pass or fail by where it was run from. A test that wants either variable sets
    it explicitly.
    """
    from alpaca import cli
    for name in cli.ENV_SESSION_VARS:
        monkeypatch.delenv(name, raising=False)
    # the research guard marks the shell of a subagent with ALPACA_RESEARCH_AGENT while its
    # session drives a run; a suite run by such a subagent must not inherit it, or every driving
    # verb a test runs would refuse as RUN-DRIVER-IS-SUBAGENT. A test that wants it sets it.
    monkeypatch.delenv("ALPACA_RESEARCH_AGENT", raising=False)


@pytest.fixture(autouse=True)
def no_repo_record(monkeypatch):
    """No test writes into this repository's own record.

    An instrument's `emit_verdict` appends a `run` row to the record of whatever project the
    working directory resolves to; under pytest that is often this repository, so a plain test
    run left a stray `.alpaca/alpaca.db` in the checkout (op-014 finding F9). ALPACA_RECORD_SKIP_ROOT is
    inherited by subprocesses; rows aimed at a tmp project still land there as before.
    """
    monkeypatch.setenv("ALPACA_RECORD_SKIP_ROOT", REPO)


@pytest.fixture(autouse=True)
def private_workspace_registry(tmp_path, monkeypatch):
    """The host workspace registry (alpaca/workspace.py) is never read or written for real by a test."""
    monkeypatch.setenv("ALPACA_WORKSPACES", str(tmp_path / "alpaca-workspaces.json"))
    # alpaca serve reads the live cloudflared config to learn whether its port is public; a test
    # reads a tmp path (absent unless the test writes it), never ~/.cloudflared
    monkeypatch.setenv("ALPACA_CLOUDFLARED_CONFIG", str(tmp_path / "cloudflared-config.yml"))
    # the reserved-port rules are exercised with one sample port (the default list is empty;
    # test_reserved_ports.py covers the default and the parsing). 7350 is a neutral number inside
    # the range serve._default_port derives from (7300 and up), so the skip rule is exercised.
    monkeypatch.setenv("ALPACA_RESERVED_PORTS", "7350")


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A throwaway project root: ALPACA-MANIFEST present, no .alpaca yet. cwd inside it."""
    root = tmp_path / "proj"
    root.mkdir()
    shutil.copy(os.path.join(REPO, "ALPACA-MANIFEST"), root / "ALPACA-MANIFEST")
    (root / "style").mkdir(); (root / "style" / "banned.txt").write_text("", encoding="utf-8")
    (root / "src").mkdir()
    monkeypatch.chdir(root / "src")
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg"))
    (tmp_path / "cfg").mkdir()
    (root / ".alpaca" / "config").mkdir(parents=True)
    monkeypatch.delenv("ALPACA_ROOT", raising=False)
    yield str(root)
    # the attempt audit's packets are read-only trees (verifier.md section 9.4): make them
    # writable again so the basetemp can be removed
    research = root / ".alpaca" / "research"
    if research.is_dir():
        from alpaca.research import audit
        audit._writable_tree(str(research))


# Shipped tests that need a development-only file name it through alpaca/tests/devfiles.py
# (op-008 t-072b): a release tree skips them, the development tree runs them, and a development
# tree that lost the file fails them here.
from alpaca.tests.devfiles import pytest_runtest_setup  # noqa: E402,F401


def pytest_configure(config):
    if not hasattr(config, "workerinput"):
        # Compile the tree once before any test or pytest-xdist worker starts. In a fresh clone (a
        # release gate) two processes importing one module at once both write its .pyc, and a
        # research session that pinned the first one reads the second as an engine change (op-056).
        # Test fixture folders are data, read file by file (rr004): nothing in them is compiled.
        import compileall, re, warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")      # a test module's own SyntaxWarning shows when it runs
            compileall.compile_dir(os.path.join(REPO, "alpaca"), quiet=2, workers=0,
                                   rx=re.compile(r"[/\\]fixtures[/\\]"))
    config.addinivalue_line("markers", "dev_files(*paths): the test reads these development-only "
                            "files (alpaca/tests/devfiles.py)")
    config.addinivalue_line("markers", "dev_files_lost(*paths, reason): the development tree lost a "
                            "file the test reads; the test fails")
    config.addinivalue_line("markers", "dev_tree: the test reads the development tree itself (its git "
                            "index, its release configuration) and skips in a release tree; the "
                            "release's dev-pytest gate selects it with dev_files (op-056)")


# The release runs this suite on pytest-xdist workers with `--dist loadgroup` (project.yaml, op-056).
# Each test file stays on one worker, so its module fixtures are built once, except:
# - the files in SPREAD: slow files of independent tests, which go to any free worker test by test;
# - the files that run research sessions against the live engine files (ENGINE_FILES): they share
#   one worker, because some of them touch those files on purpose and a session running in another
#   worker would read the touch as an engine change and end `interrupted`;
# The tests in SLOW_FIRST are handed out first, so the longest ones do not start last.
# Without loadgroup (a plain pytest run) nothing is marked or reordered.
SPREAD = {
    "test_research_controls.py", "test_research_runner.py", "test_research_verifier.py", "test_boot_check.py", "test_instrument_wiring.py",
    "wiki/test_screen_door.py", "wiki/test_kill_states.py", "wiki/test_screen_floor_python.py",
    "wiki/test_lifecycle_model.py",
}
ENGINE_FILES = ("research_session", "session_engine", "install_root()")
#: node id prefixes, slowest first (about 280, 100 and 50 s on 2026-10-13). The browser tests are
#: left in place: started together on a loaded host, the timing checks of test_studio_motion failed.
SLOW_FIRST = (
    "test_research_controls.py::test_an_accepted_control_attempt_still_fails_the_burn_in",
    "wiki/test_kill_states.py", "wiki/test_lifecycle_model.py", "test_research_redteam.py",
)


def _source(item, cache):
    path = str(item.path)
    if path not in cache:
        try:
            with open(path, encoding="utf-8") as fh:
                cache[path] = fh.read()
        except OSError:
            cache[path] = ""
    return cache[path]


@pytest.hookimpl(tryfirst=True)       # before xdist's own hook, which reads the marks
def pytest_collection_modifyitems(config, items):
    # A module that calls require_dev_files() skips some of its tests in a release tree from inside
    # the test or a fixture, where no mark can be read; all its tests count as dev_tree, so the
    # release's dev-pytest gate (`-m "dev_files or dev_tree"`) runs every test a release tree skips.
    sources = {}
    for item in items:
        if "require_dev_files(" in _source(item, sources):
            item.add_marker(pytest.mark.dev_tree)
    if not getattr(config.option, "loadgroup", False):    # xdist sets it on its workers
        return
    def first(item):
        rel = os.path.relpath(str(item.path), HERE).replace(os.sep, "/") + "::" + item.name
        return next((n for n, prefix in enumerate(SLOW_FIRST) if rel.startswith(prefix)), len(SLOW_FIRST))
    items.sort(key=first)              # stable: every other test keeps its place
    groups = {}
    for item in items:
        rel = os.path.relpath(str(item.path), HERE).replace(os.sep, "/")
        if rel in SPREAD:
            continue
        if rel not in groups:
            text = _source(item, sources)
            groups[rel] = "research-engine" if any(n in text for n in ENGINE_FILES) else rel
        item.add_marker(pytest.mark.xdist_group(groups[rel]))
