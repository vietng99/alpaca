import os, shutil, pytest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))

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
    # test_reserved_ports.py covers the default and the parsing)
    monkeypatch.setenv("ALPACA_RESERVED_PORTS", "7328")


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
    return str(root)
