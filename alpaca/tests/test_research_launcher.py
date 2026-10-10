"""t-098: the headless agent launcher (design/research-engine/architecture.md sections 3.6 and 6).

Done when (the contract of t-098, printed by .alpaca/research-engine/show_contract.py t-098):

  * tests with a fake runtime cover timeout, crash and cleanup;
  * a launched agent sees no files outside its worktree and the declared read paths;
  * cleanup kills every descendant process of a launched agent, including one that left the
    process group (gap map F13);
  * every prompt that carries repository, issue, log or notes text includes the fixed
    untrusted-text sentence, and every attempt prompt includes the no-Internet paragraph
    (gap map H17, H19; test_research_prompting.py reads the rendered prompts);
  * a launched agent's PWD is its working directory and every path the launcher passes is
    absolute, shown by a launch started from another directory (gap map F03).
Fail case: an agent inherits the operator's settings, memories or credentials beyond what the
runtime needs.

Each control has a positive and a negative case. The fake runtimes are Python scripts
(alpaca/tests/research_fakes.py): no real agent runtime starts and no test reaches the network;
the egress tests tunnel to an echo server on the host's loopback. The one exception is the H15
test, which runs the real claude only when ALPACA_TEST_REAL_RUNTIME=1 is set, with an empty
egress allow list and a fake credential, so it reaches no provider. The jailed cases need bwrap
and skip without it. The budget and the frozen contract belong to t-100 and t-096, so spend.check,
spend.book, contract.read_frozen and contract.check_read_paths are faked here.
"""
from __future__ import annotations

import dataclasses
import hashlib
import inspect
import json
import os
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
import types as pytypes
import uuid

import pytest

from alpaca import db
from alpaca.gates import verdict as vc
from alpaca.research import agent_transcript as at
from alpaca.research import contract as contract_mod
from alpaca.research import egress, events, jail, launcher, paths, procs, spend, texts
from alpaca.research import types as rtypes
from alpaca.research.errors import LaunchError
from alpaca.tests import research_fakes as fakes
from alpaca.tests.research_fakes import ATTEMPT, OTHER, RUN

JAILED = pytest.mark.skipif(not jail.available()[0], reason="bwrap is not available")


def _real_claude() -> str | None:
    """The operator's real claude binary, resolved at import (before a fixture swaps PATH for
    the fakes), only when ALPACA_TEST_REAL_RUNTIME=1: the build rules keep real runtimes out
    of the default suite."""
    if os.environ.get("ALPACA_TEST_REAL_RUNTIME") != "1":
        return None
    try:
        return launcher.resolve_binary("claude", dict(os.environ))
    except LaunchError:
        return None


REAL_CLAUDE = _real_claude()

# the operator's Claude credentials file, shaped like the real one (key names from
# .alpaca/proofs/op-009/t-098.logs/attack-adversarial-r2-run.txt:73-75): the runtime's login,
# plus MCP server tokens and a device token that no agent may see (check round 2, adversarial B1)
OPERATOR_LOGIN = {"accessToken": "operator-oauth", "refreshToken": "operator-refresh-token",
                  "scopes": ["user:inference"], "subscriptionType": "max"}
OPERATOR_CREDENTIALS = {
    "claudeAiOauth": OPERATOR_LOGIN,
    "mcpOAuth": {"plugin:design:slack|x": {"accessToken": "xoxp-mcp-slack-secret",
                                            "serverUrl": "https://mcp.slack.example",
                                            "clientId": "mcp-client-id"}},
    "trustedDeviceToken": "operator-device-token-0123456789"}
OPERATOR_ONLY = ("mcpOAuth", "xoxp-mcp-slack-secret", "mcp.slack.example", "mcp-client-id",
                 "trustedDeviceToken", "operator-device-token")


# ================================================================ fixtures

@pytest.fixture
def lab(project, tmp_path, monkeypatch):
    """A project with one run laid out, fake runtimes on PATH, the operator's secrets in the
    environment, and the budget and contract of other tasks faked."""
    # the launcher is the test and control runtime: it starts a headless agent only when this switch
    # is on and the project root is under a temp prefix (launcher.refuse_headless, in-session-runtime.md
    # section 11). It is set here, in the fixture every module that drives the launcher imports
    # (the red-team suite included), and not in an autouse fixture of one module
    monkeypatch.setenv(launcher.TEST_SWITCH_ENV, launcher.TEST_SWITCH_TOKEN)
    opt = str(tmp_path / "opt")
    claude = fakes.fake_claude(opt)
    codex = fakes.fake_codex(opt)
    links = tmp_path / "userbin"
    links.mkdir()
    os.symlink(claude, links / "claude")
    os.symlink(codex, links / "codex")
    monkeypatch.setenv("PATH", "%s:/usr/local/bin:/usr/bin:/bin" % links)
    for name in ("LC_ALL", "TZ", "SSL_CERT_FILE", "SSL_CERT_DIR", "CLAUDE_CODE_OAUTH_TOKEN",
                 "ANTHROPIC_BASE_URL", "OPENAI_BASE_URL", "CODEX_API_KEY", "HTTPS_PROXY",
                 "https_proxy", "CODEX_HOME"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LANG", "C.UTF-8")
    # the operator's secrets and session identity: none of them may reach an agent
    for name, value in (("GH_TOKEN", "gh-secret"), ("GITHUB_TOKEN", "gh-secret-2"),
                        ("SSH_AUTH_SOCK", "/tmp/ssh-agent.sock"), ("ALPACA_SESSION_ID", "op-s1"),
                        ("CLAUDECODE", "1"), ("CLAUDE_CODE_SESSION_ID", "sess-operator"),
                        ("AWS_SECRET_ACCESS_KEY", "aws-secret"),
                        ("OPENAI_API_KEY", "openai-secret"),
                        ("ANTHROPIC_API_KEY", "anthropic-key")):
        monkeypatch.setenv(name, value)
    # the operator's Claude config home: a global CLAUDE.md, hooks, memories and credentials
    cfg = os.environ["CLAUDE_CONFIG_DIR"]
    fakes.Lab.write(os.path.join(cfg, "CLAUDE.md"), "operator memory: secret preference\n")
    fakes.Lab.write(os.path.join(cfg, "settings.json"),
                    json.dumps({"hooks": {"SessionStart": [{"command": "touch /tmp/pwned"}]}}))
    fakes.Lab.write(os.path.join(cfg, "projects", "-x", "memory.md"), "operator project memory\n")
    fakes.Lab.write(os.path.join(cfg, ".credentials.json"),
                    json.dumps(OPERATOR_CREDENTIALS) + "\n")
    box = fakes.Lab(project, str(tmp_path))
    box.claude, box.codex, box.opt = claude, codex, opt
    box.booked = []
    box.check = fakes.go_check()
    monkeypatch.setattr(spend, "check", lambda conn, root, run, *, role: box.check)
    monkeypatch.setattr(spend, "book", lambda conn, run, task, **kw: box.booked.append(
        dict(kw, run=run, task=task, in_txn=conn.in_transaction)))
    monkeypatch.setattr(contract_mod, "read_frozen", lambda root, run: box.contract)
    monkeypatch.setattr(contract_mod, "check_read_paths", lambda contract: [])
    yield box
    box.conn.close()


def _alive_with(token: str) -> list:
    """Live (not zombie) processes whose command line holds token."""
    found = []
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            with open("/proc/%s/cmdline" % name, "rb") as fh:
                cmd = fh.read().replace(b"\0", b" ").decode("utf-8", "replace")
            with open("/proc/%s/stat" % name) as fh:
                state = fh.read().rsplit(")", 1)[1].split()[0]
        except OSError:
            continue
        if token in cmd and state != "Z":
            found.append(int(name))
    return found


def _egress_path(root, run, launch) -> str:
    """paths.egress_dir's folder, computed without making it."""
    base = os.environ.get("XDG_RUNTIME_DIR") or ""
    if not base or not os.path.isdir(base):
        base = "/tmp"
    leaf = hashlib.sha256("\n".join((root, run or "", launch)).encode()).hexdigest()[:16]
    return os.path.join(base, "alpaca-egress-%d" % os.getuid(), leaf)


def _stream_events(result) -> list:
    out = []
    with open(result["stream"], encoding="utf-8") as fh:
        for line in fh:
            if line.startswith("{"):
                out.append(json.loads(line))
    return out


def _probe(result) -> dict:
    found = [e for e in _stream_events(result) if e.get("type") == "probe"]
    assert found, "the fake printed no probe report"
    return found[0]


# ================================================================ the design names

DESIGN = {
    "launcher": {
        "runtime_in_use": "(environ: 'dict | None' = None) -> 'str | None'",
        "detect_runtime": "(environ: 'dict | None' = None, *, root: 'str | None' = None) -> 'str'",
        "resolve_binary": "(runtime: 'str', environ: 'dict | None' = None) -> 'str'",
        "install_dir": "(runtime: 'str', binary: 'str', environ: 'dict | None' = None) -> 'str'",
        "prepare_home": "(root: 'str', run: 'str', spec: 'LaunchSpec', *, "
                        "environ: 'dict | None' = None) -> 'LaunchHome'",
        "build_argv": "(spec: 'LaunchSpec', home: 'LaunchHome', binary: 'str') -> 'list[str]'",
        "build_env": "(spec: 'LaunchSpec', home: 'LaunchHome', "
                     "environ: 'dict | None' = None) -> 'dict'",
        "check_paths": "(root: 'str', spec: 'LaunchSpec', contract) -> 'None'",
        "jail_spec": "(spec: 'LaunchSpec', home: 'LaunchHome', *, contract, install: 'str', "
                     "egress_dir: 'str') -> 'JailSpec'",
        "launch": "(spec, *, conn=None, root: 'str | None' = None) -> 'dict'",
        "refuse_headless": "(spec, conn, root: 'str') -> 'None'",
        "HeadlessRefused": "(detail: 'str' = '') -> 'None'",
        "classify_failure": "(result: 'LaunchResult', stream_tail: 'str') -> 'str | None'",
        "tool_calls": "(transcript: 'str', runtime: 'str', keep_ids: 'bool' = False) -> 'list'",
    },
    "egress": {
        "allowed_hosts": "(runtime: 'str', environ: 'dict', contract) -> 'tuple'",
        "start": "(sock_dir: 'str', *, allow: 'tuple', log: 'str', upstream: 'str | None' = None)",
    },
    "jail_forward": {"main": "(argv: 'list') -> 'int'"},
    "prompting": {
        "render": "(role: 'str', *, contract, context: 'dict', untrusted: 'dict[str, str]') -> 'str'",
    },
}
CONSTANTS = {"launcher": {"RUNTIMES": ("claude", "codex", "control"),
                          "TEST_SWITCH_ENV": "ALPACA_RESEARCH_TEST_HEADLESS",
                          "TEST_SWITCH_TOKEN": "headless-agent-for-tests-only"},
             "egress": {"PROVIDER_HOSTS": {"claude": ("api.anthropic.com",),
                                           "codex": ("api.openai.com", "chatgpt.com")}}}


def _own(mod) -> set:
    out = set()
    for name, obj in vars(mod).items():
        if name.startswith("_"):
            continue
        if inspect.isfunction(obj) or inspect.isclass(obj):
            if obj.__module__ == mod.__name__:
                out.add(name)
        elif name.isupper() and not inspect.ismodule(obj):
            out.add(name)
    return out


@pytest.mark.parametrize("module", sorted(DESIGN))
def test_the_built_modules_hold_the_design_names_and_signatures(module):
    import importlib
    mod = importlib.import_module("alpaca.research." + module)
    want = set(DESIGN[module]) | set(CONSTANTS.get(module, {}))
    assert _own(mod) == want, sorted(_own(mod) ^ want)
    for name, sig in DESIGN[module].items():
        assert str(inspect.signature(getattr(mod, name))) == sig, name
        # built: no body raises NotImplementedError any more
        assert "NotImplementedError" not in inspect.getsource(getattr(mod, name)), name
    for name, value in CONSTANTS.get(module, {}).items():
        got = getattr(mod, name)
        assert got == value and type(got) is type(value), name
    # negative: a renamed parameter would not match the recorded text
    assert str(inspect.signature(lambda spec, *, conn=None, base=None: 0)) != \
        DESIGN["launcher"]["launch"]


# ================================================================ runtime and binary

def test_runtime_in_use_reads_one_runtime_s_markers():
    assert launcher.runtime_in_use({"CLAUDECODE": "1"}) == "claude"
    assert launcher.runtime_in_use({"CLAUDE_CODE_SESSION_ID": "s"}) == "claude"
    assert launcher.runtime_in_use({"CODEX_THREAD_ID": "t"}) == "codex"
    assert launcher.runtime_in_use({"CODEX_SANDBOX": "seatbelt"}) == "codex"
    # negative: both runtimes' markers, or none, name no runtime
    assert launcher.runtime_in_use({"CLAUDECODE": "1", "CODEX_THREAD_ID": "t"}) is None
    assert launcher.runtime_in_use({}) is None
    assert launcher.runtime_in_use({"CLAUDECODE": ""}) is None


def test_detect_runtime_falls_back_to_the_first_operator_that_resolves(tmp_path):
    root = tmp_path / "p"
    root.mkdir()
    (root / "project.yaml").write_text("operators:\n- terminal\n- codex\n- claude\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = fakes.fake_claude(str(tmp_path / "opt"))
    os.symlink(fake, bin_dir / "claude")
    env = {"PATH": str(bin_dir)}
    assert launcher.detect_runtime(env, root=str(root)) == "claude"   # codex does not resolve
    assert launcher.detect_runtime({"CODEX_SANDBOX": "x", "PATH": ""}, root=str(root)) == "codex"
    # negative: no marker and no operator binary
    with pytest.raises(LaunchError):
        launcher.detect_runtime({"PATH": str(tmp_path / "empty")}, root=str(root))


def test_resolve_binary_uses_a_clean_path_and_the_realpath(tmp_path, monkeypatch):
    fake = fakes.fake_claude(str(tmp_path / "opt"))
    good = tmp_path / "good"
    good.mkdir()
    os.symlink(fake, good / "claude")
    assert launcher.resolve_binary("claude", {"PATH": str(good)}) == os.path.realpath(fake)
    # negative: a relative PATH entry is never searched, so a file in the cwd does not answer
    monkeypatch.chdir(good)
    with pytest.raises(LaunchError):
        launcher.resolve_binary("claude", {"PATH": ".:relative"})
    with pytest.raises(LaunchError):
        launcher.resolve_binary("gemini", {"PATH": str(good)})
    assert launcher.resolve_binary("control", {"PATH": "/usr/bin"}).startswith("/usr/")


def test_install_dir_mounts_the_binary_or_one_release_folder(tmp_path):
    home = tmp_path / "home"
    fake_claude = fakes.fake_claude(str(home / ".local" / "share"))
    fake_codex = fakes.fake_codex(str(home / ".codex" / "packages"))
    env = {"HOME": str(home)}
    assert launcher.install_dir("claude", fake_claude, env) == os.path.realpath(fake_claude)
    release = launcher.install_dir("codex", fake_codex, env)
    assert release.endswith("1.2.3-x86_64-fake") and release.startswith(str(home / ".codex"))
    # negative: a folder that is the operator's home or holds a config home is refused
    stray = home / "bin" / "codex"
    stray.parent.mkdir(parents=True)
    stray.write_text("#!/bin/sh\n")
    with pytest.raises(LaunchError):
        launcher.install_dir("codex", str(stray), env)       # the parent of bin/ is the home
    with pytest.raises(LaunchError):
        launcher.install_dir("codex", str(home / ".codex" / "codex"), env)   # ~/.codex itself


# ================================================================ home, argv, environment

def test_prepare_home_copies_nothing_of_the_operator_s_config(lab):
    spec = lab.implementer()
    home = launcher.prepare_home(lab.root, RUN, spec)
    assert home.root == paths.launch_home(lab.root, RUN, "t-140-implementer-t1")
    assert sorted(os.listdir(home.root)) == ["bin", "claude", "home", "msg", "out"]
    assert os.listdir(home.config) == []                  # a key in the env: no credential file
    assert os.listdir(home.home) == [] and os.listdir(home.out) == []
    # the settings sit in bin/, which the jail binds read-only, never in the agent's config home
    # (check round 4, adversarial m3)
    assert sorted(os.listdir(os.path.join(home.root, "bin"))) == ["jail_forward.py",
                                                                   "launch-settings.json"]
    for sub in ("home", "claude", "msg", "bin", "out"):
        assert os.stat(os.path.join(home.root, sub)).st_mode & 0o777 == 0o700
    settings_file = os.path.join(home.root, "bin", "launch-settings.json")
    assert os.stat(settings_file).st_mode & 0o777 == 0o444
    settings = json.load(open(settings_file))
    assert settings == {"permissions": {"allow": ["Read", "Bash", "Write", "Edit"],
                                        # the one forbidden-tool list (in-session-runtime.md 26)
                                        "deny": list(at.FORBIDDEN_TOOLS),
                                        "defaultMode": "dontAsk"},
                        "disableAllHooks": True, "sandbox": {"enabled": False}}
    assert home.credential_file is None                   # ANTHROPIC_API_KEY is set
    # negative: with no credential variable the runtime's login is taken out of the operator's
    # credentials file into cred/, a folder no jail mounts, and bound read-only over an empty
    # file of the same name; the operator's MCP tokens and device token are left behind
    os.environ.pop("ANTHROPIC_API_KEY")
    home2 = launcher.prepare_home(lab.root, RUN, spec)
    assert home2.credential_file == os.path.join(home2.root, "cred", ".credentials.json")
    assert json.load(open(home2.credential_file)) == {"claudeAiOauth": OPERATOR_LOGIN}
    assert os.stat(home2.credential_file).st_mode & 0o777 == 0o400
    assert os.stat(os.path.dirname(home2.credential_file)).st_mode & 0o777 == 0o700
    copy = open(home2.credential_file).read()
    assert not any(word in copy for word in OPERATOR_ONLY), copy
    placeholder = os.path.join(home2.config, ".credentials.json")
    assert open(placeholder).read() == ""


def test_prepare_home_keeps_only_the_codex_login_of_auth_json(lab, tmp_path, monkeypatch):
    codex_home = tmp_path / "operator-codex"
    auth = {"auth_mode": "chatgpt", "OPENAI_API_KEY": None, "last_refresh": "2026-09-01",
            "tokens": {"access_token": "codex-access-token", "refresh_token": "codex-refresh",
                       "id_token": "codex-id-token", "account_id": "acct"},
            "extra_integration": {"token": "operator-extra-secret"}}
    fakes.Lab.write(str(codex_home / "auth.json"), json.dumps(auth))
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.delenv("OPENAI_API_KEY")
    spec = dataclasses.replace(lab.implementer(), runtime="codex")
    home = launcher.prepare_home(lab.root, RUN, spec)
    assert home.credential_file == os.path.join(home.root, "cred", "auth.json")
    kept = json.load(open(home.credential_file))
    assert kept == {k: auth[k] for k in ("auth_mode", "OPENAI_API_KEY", "last_refresh", "tokens")}
    assert "operator-extra-secret" not in open(home.credential_file).read()
    assert open(os.path.join(home.config, "auth.json")).read() == ""
    # negative: the redaction list comes from the copy, not from what was left behind
    got = launcher._secrets("codex", home, {})
    assert b"codex-access-token" in got and b"operator-extra-secret" not in got


@pytest.mark.parametrize("text,why", [
    ("not json at all", "not a JSON object"),
    ('["a", "list"]', "not a JSON object"),
    (json.dumps({"mcpOAuth": OPERATOR_CREDENTIALS["mcpOAuth"]}), "holds no claude login"),
])
def test_a_credentials_file_that_does_not_parse_or_holds_no_login_is_start_failed(
        lab, monkeypatch, text, why):
    os.environ.pop("ANTHROPIC_API_KEY")
    cred = os.path.join(os.environ["CLAUDE_CONFIG_DIR"], ".credentials.json")
    fakes.Lab.write(cred, text)
    spec = lab.implementer()
    with pytest.raises(LaunchError) as err:
        launcher.prepare_home(lab.root, RUN, spec)
    assert why in str(err.value) and "xoxp" not in str(err.value)
    # the launch records start-failed and no research-launch; no home is left
    monkeypatch.setattr(jail, "binary", lambda: "/bin/true")
    result = launcher.launch(spec, conn=lab.conn, root=lab.root)
    assert result["outcome"] == result["infra"] == "start-failed", result["reason"]
    assert why in result["reason"] and not lab.events(events.LAUNCH)
    assert not os.path.exists(paths.launch_home(lab.root, RUN, "t-140-implementer-t1"))
    # negative: the operator's real file shape starts
    fakes.Lab.write(cred, json.dumps(OPERATOR_CREDENTIALS))
    assert launcher.prepare_home(lab.root, RUN, spec).credential_file


def test_bash_allow_turns_the_bare_bash_rule_into_command_rules(lab):
    spec = lab.implementer(tools=("Read", "Bash"), bash_allow=("bin/rerun", "ls"))
    home = launcher.prepare_home(lab.root, RUN, spec)
    rules = json.load(open(os.path.join(home.root, "bin", "launch-settings.json")))[
        "permissions"]
    assert rules["allow"] == ["Read", "Bash(bin/rerun:*)", "Bash(ls:*)"]
    # negative: without bash_allow the bare rule stays
    assert launcher._claude_settings(lab.implementer(tools=("Read", "Bash")))[
        "permissions"]["allow"] == ["Read", "Bash"]


def test_claude_argv_isolates_settings_denies_web_and_keeps_the_prompt_off_argv(lab):
    spec = lab.implementer(prompt="SECRET PROMPT TEXT", max_usd=2.5, effort="high")
    home = launcher.prepare_home(lab.root, RUN, spec)
    argv = launcher.build_argv(spec, home, lab.claude)
    assert argv[:2] == [lab.claude, "-p"]
    assert argv[argv.index("--setting-sources") + 1] == ""
    assert argv[argv.index("--mcp-config") + 1] == '{"mcpServers":{}}'
    assert "--strict-mcp-config" in argv and "--disable-slash-commands" in argv
    assert argv[argv.index("--permission-mode") + 1] == "dontAsk"
    denied = argv[argv.index("--disallowedTools") + 1].split(",")
    assert {"WebFetch", "WebSearch", "Task"} <= set(denied)
    assert argv[argv.index("--tools") + 1] == "Read,Bash,Write,Edit"
    settings = argv[argv.index("--settings") + 1]
    assert settings == os.path.join(home.root, "bin", "launch-settings.json")
    # the jail binds the settings read-only: bin/ is a read-only mount and no read-write mount
    # holds the file (the config home is read-write)
    jspec = launcher.jail_spec(spec, home, contract=lab.contract, install="/opt/rt/claude",
                               egress_dir="/run/user/1/alpaca-egress-1/abc")
    assert os.path.dirname(settings) in jspec.ro
    assert not any(launcher._inside(settings, rw) for rw in jspec.rw)
    assert argv[argv.index("--max-budget-usd") + 1] == "2.5000"
    assert argv[argv.index("--effort") + 1] == "high"
    uuid.UUID(argv[argv.index("--session-id") + 1])
    assert not any("SECRET PROMPT" in a for a in argv)
    assert "--json-schema" not in argv and "--model" not in argv
    # every path the launcher passes is absolute
    for value in argv:
        if value.startswith(("/", ".")) or os.sep in value and not value.startswith("{"):
            assert os.path.isabs(value), value
    # negative: the proposer gets its schema; a relative cwd is refused
    prop = lab.implementer(role="proposer", output_schema={"type": "object"})
    assert "--json-schema" in launcher.build_argv(prop, home, lab.claude)
    with pytest.raises(LaunchError):
        launcher.build_argv(lab.implementer(cwd="relative/tree"), home, lab.claude)


def test_codex_argv_and_config_turn_off_memories_web_and_the_login_shell(lab):
    spec = lab.implementer(runtime="codex", role="proposer", output_schema={"type": "object"},
                           effort="low")
    home = launcher.prepare_home(lab.root, RUN, spec)
    argv = launcher.build_argv(spec, home, lab.codex)
    assert argv[:3] == [lab.codex, "exec", "--json"] and argv[-1] == "-"
    for flag in ("--skip-git-repo-check", "--dangerously-bypass-approvals-and-sandbox",
                 "--strict-config", "--ignore-rules"):
        assert flag in argv
    assert argv[argv.index("--cd") + 1] == lab.worktree
    assert argv[argv.index("--output-schema") + 1] == os.path.join(home.root, "bin",
                                                                  "proposal.schema.json")
    assert argv[argv.index("--config") + 1] == 'model_reasoning_effort="low"'
    config = open(os.path.join(home.config, "config.toml")).read()
    assert 'web_search = "disabled"' in config and "allow_login_shell = false" in config
    for name in fakes.CODEX_FEATURES:
        assert "%s = false" % name in config
    assert os.listdir(home.config) == ["config.toml"]
    # negative: an implementer gets no output schema
    assert "--output-schema" not in launcher.build_argv(
        dataclasses.replace(spec, role="implementer"), home, lab.codex)


def test_build_env_is_an_allowlist(lab, monkeypatch):
    spec = lab.implementer()
    home = launcher.prepare_home(lab.root, RUN, spec)
    env = launcher.build_env(spec, home)
    assert env["HOME"] == home.home and env["PWD"] == lab.worktree
    assert env["CLAUDE_CONFIG_DIR"] == home.config
    assert env["ANTHROPIC_API_KEY"] == "anthropic-key"
    assert env["HTTPS_PROXY"] == env["https_proxy"] == "http://127.0.0.1:8119"
    assert env["NO_PROXY"] == "" and env["TMPDIR"] == "/tmp"
    assert env["PATH"] == "/usr/local/bin:/usr/bin:/bin"
    # the tag is the launch id and a hash of the home, never the bare launch id, so a tag sweep
    # finds no process of another project's, run's or test root's launch with the same key
    tag = env[procs.TAG_VAR]
    digest = hashlib.sha256(os.path.realpath(home.root).encode()).hexdigest()[:12]
    assert tag == "t-140-implementer-t1-" + digest == launcher._tag(home)
    other = dataclasses.replace(home, root=paths.launch_home(lab.root + "-other", RUN,
                                                             home.launch_id))
    assert launcher._tag(other) != tag and launcher._tag(other).startswith("t-140-implementer-t1-")
    assert env["GIT_OBJECT_DIRECTORY"] == lab.git_objects
    for name in ("CLAUDE_CODE_DISABLE_AUTO_MEMORY", "DISABLE_TELEMETRY",
                 "DISABLE_ERROR_REPORTING", "DISABLE_AUTOUPDATER"):
        assert env[name] == "1"
    # negative: none of the operator's secrets, session ids or other runtime's keys pass
    for name in ("GH_TOKEN", "GITHUB_TOKEN", "SSH_AUTH_SOCK", "ALPACA_SESSION_ID", "CLAUDECODE",
                 "CLAUDE_CODE_SESSION_ID", "AWS_SECRET_ACCESS_KEY", "OPENAI_API_KEY"):
        assert name not in env, name
    assert "openai-secret" not in json.dumps(env) and "gh-secret" not in json.dumps(env)
    codex_env = launcher.build_env(dataclasses.replace(spec, runtime="codex"), home)
    assert codex_env["OPENAI_API_KEY"] == "openai-secret"
    assert codex_env["CODEX_HOME"] == home.config            # never the operator's ~/.codex
    assert codex_env[procs.TAG_VAR] == tag
    assert "ANTHROPIC_API_KEY" not in codex_env and "CLAUDE_CONFIG_DIR" not in codex_env
    # one credential variable of the runtime, the first set, never both (section 6.5; check
    # round 2 of t-098, mutation M9h)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oauth-secret")
    env = launcher.build_env(spec, home)
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "oauth-secret" and "ANTHROPIC_API_KEY" not in env
    assert "anthropic-key" not in json.dumps(env)
    monkeypatch.setenv("CODEX_API_KEY", "codex-secret")
    codex_env = launcher.build_env(dataclasses.replace(spec, runtime="codex"), home)
    assert codex_env["OPENAI_API_KEY"] == "openai-secret" and "CODEX_API_KEY" not in codex_env
    assert "codex-secret" not in json.dumps(codex_env)


# ================================================================ paths

def test_check_paths_admits_the_launch_s_own_folders(lab):
    launcher.check_paths(lab.root, lab.implementer(), lab.contract)
    notes = lab.implementer(role="notes", workdir=lab.notes, cwd=lab.notes, read_paths=(),
                            write_paths=(), git_objects=None, git_alternates=None,
                            git_admin=None, git_common=None)
    launcher.check_paths(lab.root, notes, lab.contract)
    proposer = lab.implementer(role="proposer", workdir=lab.champion, cwd=lab.champion,
                               write_paths=(), git_objects=None, git_admin=lab.champion_admin)
    launcher.check_paths(lab.root, proposer, lab.contract)
    verifier = lab.implementer(role="verifier", run=None, workdir=lab.verify, cwd=lab.verify,
                               read_paths=(lab.verify,),
                               write_paths=(os.path.join(lab.verify, "out"),),
                               git_objects=None, git_alternates=None, git_admin=None,
                               git_common=None)
    launcher.check_paths(lab.root, verifier, None)


@pytest.mark.parametrize("change", [
    "relative", "project-root", "record", "contract", "other-attempt", "run-folder",
    "admin-alone", "admin-elsewhere", "registry-not-empty", "moved-read-path",
    "read-path-finding", "outside-run", "tree-git-host", "champion-git-host",
    "attempt-git-host", "attempt-folder-holds-git-host", "admin-as-write-path",
    "admin-as-read-path", "repair-admin-as-write-path", "repair-git-host", "git-host-as-admin"])
def test_check_paths_refuses(lab, change, monkeypatch):
    spec = lab.implementer()
    host_tree = lab.worktree + ".git-host"               # the engine's host git (section 3.1)
    host_champion = lab.champion + ".git-host"
    host_attempt = os.path.join(lab.attempt_dir, "git", "host")
    for folder in (host_tree, host_champion, host_attempt):
        os.makedirs(folder, exist_ok=True)
    if change == "tree-git-host":
        spec = dataclasses.replace(spec, write_paths=(lab.worktree, host_tree))
    elif change == "champion-git-host":
        spec = dataclasses.replace(spec, write_paths=(lab.worktree, host_champion))
    elif change == "attempt-git-host":
        spec = dataclasses.replace(spec, write_paths=(lab.worktree,
                                                      os.path.join(host_attempt, "objects")))
    elif change == "attempt-folder-holds-git-host":
        spec = dataclasses.replace(spec, read_paths=(lab.refdata, lab.attempt_dir))
    elif change == "admin-as-write-path":
        spec = dataclasses.replace(spec, write_paths=(lab.worktree, lab.champion_admin))
    elif change == "admin-as-read-path":
        spec = dataclasses.replace(spec, read_paths=(lab.refdata, lab.champion_admin))
    elif change == "repair-admin-as-write-path":       # no attempt rule covers repair/
        spec = dataclasses.replace(spec, write_paths=(lab.worktree, os.path.join(
            lab.repair, "writer.git-admin")))
    elif change == "repair-git-host":
        spec = dataclasses.replace(spec, read_paths=(lab.refdata, os.path.join(
            lab.repair, "writer.git-host")))
    elif change == "git-host-as-admin":
        spec = dataclasses.replace(spec, git_admin=host_champion)
    elif change == "relative":
        spec = dataclasses.replace(spec, cwd="worktree")
    elif change == "project-root":
        spec = dataclasses.replace(spec, read_paths=(lab.root,))
    elif change == "record":
        spec = dataclasses.replace(spec, read_paths=(os.path.join(lab.root, ".alpaca"),))
    elif change == "contract":
        spec = dataclasses.replace(spec, read_paths=(lab.contract_dir,))
    elif change == "other-attempt":
        spec = dataclasses.replace(spec, read_paths=(lab.other_tree,))
    elif change == "run-folder":
        spec = dataclasses.replace(spec, write_paths=(paths.run_dir(lab.root, RUN),))
    elif change == "admin-alone":
        spec = dataclasses.replace(spec, git_common=None)
    elif change == "admin-elsewhere":
        spec = dataclasses.replace(spec, git_admin=os.path.join(lab.other_attempt, "git",
                                                                "admin"))
    elif change == "registry-not-empty":
        os.makedirs(os.path.join(lab.git_common, "worktrees", "t-141"))
    elif change == "moved-read-path":
        os.rename(lab.refdata, lab.refdata + ".old")
        os.makedirs(os.path.join(lab.tmp, "elsewhere"))
        os.symlink(os.path.join(lab.tmp, "elsewhere"), lab.refdata)
    elif change == "read-path-finding":
        real = lab.contract.read_real[0]
        monkeypatch.setattr(contract_mod, "check_read_paths",
                            lambda contract: ["%s: socket %s/agent.sock" % (real, real)])
    elif change == "outside-run":
        spec = dataclasses.replace(spec, run=None, git_admin=None, git_common=None,
                                   git_objects=None, git_alternates=None)
    with pytest.raises(LaunchError):
        launcher.check_paths(lab.root, spec, lab.contract if change != "outside-run" else None)


# ================================================================ the jail of each role

def _home(lab, spec):
    return launcher.prepare_home(lab.root, spec.run, spec)


def test_jail_spec_matches_each_agent_row_of_the_mount_table(lab):
    install = "/opt/rt/claude"
    eg = "/run/user/1/alpaca-egress-1/abc"

    def js(spec):
        home = _home(lab, spec)
        return home, launcher.jail_spec(spec, home, contract=lab.contract, install=install,
                                        egress_dir=eg)

    def own(home):
        return (home.home, home.config, os.path.join(home.root, "msg"))
    ref = lab.contract.read_real[0]
    # implementer: the worktree and its admin folder writable, the private store, clone .git ro
    spec = lab.implementer()
    home, got = js(spec)
    assert got.kind == "agent" and got.cwd == lab.worktree and got.egress == eg
    assert got.rw == (lab.worktree, lab.git_objects) + own(home)
    assert got.ro == (ref, lab.git_common, install, os.path.join(home.root, "bin"))
    assert got.git_hide == (lab.git_common,) and got.admins == ((lab.git_admin, True),)
    # proposer: the champion checkout read-only with its admin folder read-only
    spec = lab.implementer(role="proposer", key="t-140/proposer/t1", workdir=lab.champion,
                           cwd=lab.champion, write_paths=(), git_objects=None,
                           git_admin=lab.champion_admin)
    home, got = js(spec)
    assert got.ro[:3] == (lab.champion, ref, lab.git_common) and got.rw == own(home)
    assert got.admins == ((lab.champion_admin, False),)
    # notes writer: its packet only, no .git
    spec = lab.implementer(role="notes", key="t-140/notes/t1", workdir=lab.notes, cwd=lab.notes,
                           read_paths=(), write_paths=(), git_objects=None, git_admin=None,
                           git_common=None)
    home, got = js(spec)
    assert got.ro == (lab.notes, install, os.path.join(home.root, "bin"))
    assert got.rw == own(home) and got.git_hide == () and got.admins == ()
    # auditor (t-089): its folder, out/ writable, no .git
    out = os.path.join(lab.audit, "out")
    spec = lab.implementer(role="auditor", key="t-140/auditor/t1", workdir=lab.audit,
                           cwd=lab.audit, read_paths=(os.path.join(lab.audit, "packet"),),
                           write_paths=(out,), git_objects=None, git_admin=None,
                           git_common=None)
    home, got = js(spec)
    assert got.ro[:2] == (lab.audit, os.path.join(lab.audit, "packet"))
    assert got.rw == (out,) + own(home) and got.admins == () and got.git_hide == ()
    # verifier (t-089): its try folder read-only, out/ and requests/ writable, no .git
    tryf = os.path.join(lab.verify, "v1", "r0", "t1")
    spec = lab.implementer(role="verifier", run=None, key="vf-t-140-1/v1/r0/t1", workdir=tryf,
                           cwd=tryf, read_paths=(tryf,),
                           write_paths=(os.path.join(tryf, "out"), os.path.join(tryf, "requests")),
                           git_objects=None, git_admin=None, git_common=None)
    home, got = js(spec)
    assert got.ro == (tryf, install, os.path.join(home.root, "bin"))
    assert got.rw == (os.path.join(tryf, "out"), os.path.join(tryf, "requests")) + own(home)
    assert got.admins == () and got.git_hide == ()
    # reproducer writer (t-101): its worktree and admin folder writable, no private store
    writer = os.path.join(lab.repair, "writer")
    spec = lab.implementer(role="reproducer-writer", key="t-150/reproducer-writer/t1",
                           attempt="t-150", workdir=writer, cwd=writer,
                           read_paths=(os.path.join(lab.repair, "packet"),),
                           write_paths=(writer,), git_objects=None,
                           git_admin=writer + ".git-admin")
    home, got = js(spec)
    assert got.rw == (writer,) + own(home) and got.admins == ((writer + ".git-admin", True),)
    assert "GIT_OBJECT_DIRECTORY" not in launcher.build_env(spec, home)
    # reproducer reviewer (t-101): its packet only
    packet = os.path.join(lab.repair, "review")
    spec = lab.implementer(role="reproducer-reviewer", key="t-150/reproducer-reviewer/t1",
                           attempt="t-150", workdir=packet, cwd=packet, read_paths=(),
                           write_paths=(), git_objects=None, git_admin=None, git_common=None)
    home, got = js(spec)
    assert got.ro[0] == packet and got.rw == own(home) and got.admins == ()
    # the credential file, when used, is an overlay, never a copy
    os.environ.pop("ANTHROPIC_API_KEY")
    spec = lab.implementer()
    home, got = js(spec)
    assert got.overlays == ((home.credential_file,
                             os.path.join(home.config, ".credentials.json")),)
    # the bound file is the login copy in cred/, which the jail mounts nowhere else
    assert home.credential_file == os.path.join(home.root, "cred", ".credentials.json")
    cred_dir = os.path.dirname(home.credential_file)
    assert not any(p == cred_dir or p.startswith(cred_dir + os.sep) or
                   cred_dir.startswith(p.rstrip(os.sep) + os.sep) for p in got.ro + got.rw)


def test_a_retry_mounts_the_recorded_admin_folder_not_what_the_git_files_say(lab):
    first = lab.implementer()
    home = _home(lab, first)
    before = launcher.jail_spec(first, home, contract=lab.contract, install="/i",
                                egress_dir="/e")
    # the first try rewrote its .git file and its admin commondir to name other folders
    fakes.Lab.write(os.path.join(lab.worktree, ".git"), "gitdir: %s\n" % lab.other_attempt)
    fakes.Lab.write(os.path.join(lab.git_admin, "commondir"), lab.other_tree + "\n")
    second = lab.implementer(key="t-140/implementer/t2", try_n=2)
    home2 = _home(lab, second)
    after = launcher.jail_spec(second, home2, contract=lab.contract, install="/i",
                               egress_dir="/e")
    assert after.admins == before.admins == ((lab.git_admin, True),)
    assert after.git_hide == before.git_hide == (lab.git_common,)
    mounted = " ".join(after.ro + after.rw + tuple(a for a, _ in after.admins))
    # negative: nothing the rewritten files name is mounted
    assert lab.other_attempt not in mounted and lab.other_tree not in mounted


def test_an_ssl_variable_inside_a_forbidden_path_refuses_the_jail(lab, monkeypatch):
    spec = lab.implementer()
    home = _home(lab, spec)
    ca = os.path.join(lab.tmp, "ca.pem")
    open(ca, "w").write("pem\n")
    monkeypatch.setenv("SSL_CERT_FILE", ca)
    got = launcher.jail_spec(spec, home, contract=lab.contract, install="/i", egress_dir="/e")
    assert os.path.realpath(ca) in got.ro
    # negative: a certificate folder inside the project root (or the operator's home) is refused
    inside = os.path.join(lab.root, "certs")
    os.makedirs(inside)
    monkeypatch.setenv("SSL_CERT_DIR", inside)
    with pytest.raises(LaunchError):
        launcher.jail_spec(spec, home, contract=lab.contract, install="/i", egress_dir="/e")


# ================================================================ failure classes

def _res(rc, **kw):
    base = dict(session="s", rc=rc, timed_out=False, held=False, launch_id="l", key="k",
                role="implementer", try_n=1, outcome="ok")
    base.update(kw)
    return rtypes.LaunchResult(**base)


def test_classify_failure_names_each_infrastructure_class():
    ok = json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "x"})
    init = json.dumps({"type": "system", "subtype": "init"})
    assert launcher.classify_failure(_res(0), ok) is None
    assert launcher.classify_failure(_res(1), "bwrap: execvp claude: No such file\n") == \
        "start-failed"
    assert launcher.classify_failure(_res(3), init + "\nTraceback: boom\n") == "crash"
    quota = json.dumps({"type": "result", "is_error": True,
                        "result": "Claude AI usage limit reached|1759300000"})
    assert launcher.classify_failure(_res(1), init + "\n" + quota) == "quota"
    codex_quota = json.dumps({"type": "turn.failed", "error": {"message": "429 Too Many Requests"}})
    assert launcher.classify_failure(_res(1), codex_quota) == "quota"
    auth = json.dumps({"type": "result", "is_error": True,
                       "result": "Invalid API key. Please run /login"})
    assert launcher.classify_failure(_res(1), init + "\n" + auth) == "auth"
    # a rate-limit line on stderr alone is crash, not quota: any process that holds the
    # runtime's stdout can print one (check round 2 of t-098, adversarial m1)
    assert launcher.classify_failure(_res(1), init + "\nError: rate limit reached\n") == "crash"
    # an error result with exit 0 is still classified
    assert launcher.classify_failure(_res(0), init + "\n" + quota) == "quota"
    # exit 0 before the first stream event is still start-failed (section 6.7)
    assert launcher.classify_failure(_res(0), "") == "start-failed"
    assert launcher.classify_failure(_res(0), "only a line on stderr\n") == "start-failed"
    # a flood of lines that are not JSON after the events is not read as no event
    flood = init + "\n" + ok + "\n" + ("noise " * 20 + "\n") * 5000
    assert launcher.classify_failure(_res(0), launcher._classify_tail(flood)) is None
    # negative: a timeout or a stop is the launch's own verdict, never an infrastructure class
    assert launcher.classify_failure(_res(None, timed_out=True, outcome="timeout"), "") is None
    assert launcher.classify_failure(_res(None, outcome="killed"), "") is None
    assert launcher._reset_at(quota) == "2025-10-01T06:26:40Z"


# ================================================================ tool calls

def test_tool_calls_reads_a_claude_stream_in_input_order(tmp_path):
    lines = [
        {"type": "system", "subtype": "init"},
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "Reading first."},
            {"type": "tool_use", "id": "a", "name": "Read", "input": {"file_path": "/w/x.py"}}]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "a", "content": [{"type": "text",
                                                                     "text": "print(1)"}]}]}},
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "b", "name": "Bash",
             "input": {"command": "cat <<'EOF' > f\nbody\nEOF"}}]}},
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "c", "name": "Write",
             "input": {"file_path": "/w/y.py", "content": "z"}}]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "b", "content": "done"}]}},
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "d", "name": "WebFetch", "input": {"url": "http://x"}}]}},
    ]
    path = tmp_path / "stream.jsonl"
    path.write_text("\n".join(json.dumps(x) for x in lines) + "\nnot json\n")
    calls = launcher.tool_calls(str(path), "claude")
    assert [c["tool"] for c in calls] == ["Read", "Bash", "Write", "WebFetch"]
    assert [c["kind"] for c in calls] == ["read", "shell", "write", "web"]
    assert calls[0]["paths"] == ["/w/x.py"] and calls[0]["output"] == "print(1)"
    assert calls[0]["text"] == "Reading first."
    assert calls[1]["input"] == "cat <<'EOF' > f\nbody\nEOF" and calls[1]["output"] == "done"
    assert calls[2]["paths"] == ["/w/y.py"] and calls[2]["output"] is None
    # a Claude call also carries the tool's input object and whether its result was an error
    # (in-session-runtime.md section 18.1: the judge tells a call the guard denied from one that ran)
    assert set(calls[0]) == {"tool", "kind", "input", "paths", "ts", "text", "output",
                             "raw_input", "error"}
    assert calls[0]["raw_input"] == {"file_path": "/w/x.py"} and calls[0]["error"] is False
    assert calls[2]["error"] is None
    assert "id" not in calls[0] and launcher.tool_calls(str(path), "claude", keep_ids=True)[0]["id"] == "a"
    # the same text passed directly gives the same calls
    assert launcher.tool_calls(path.read_text(), "claude") == calls
    # negative: an unknown runtime is refused
    with pytest.raises(ValueError):
        launcher.tool_calls(str(path), "gemini")


def test_tool_calls_reads_a_codex_stream_with_patches_and_file_changes():
    patch = ("*** Begin Patch\n*** Update File: src/a.py\n@@\n-x\n+y\n*** Add File: src/b.py\n"
             "+new\n*** End Patch")
    lines = [
        {"type": "thread.started", "thread_id": "t"},
        {"type": "item.started", "item": {"id": "i1", "type": "command_execution",
                                          "command": "bash -lc ls", "aggregated_output": ""}},
        {"type": "item.completed", "item": {"id": "i1", "type": "command_execution",
                                            "command": "bash -lc ls",
                                            "aggregated_output": "a.py\n", "exit_code": 0}},
        {"type": "item.completed", "item": {"id": "i2", "type": "agent_message",
                                            "text": "Now I patch."}},
        {"type": "item.completed", "item": {"id": "i3", "type": "command_execution",
                                            "command": "apply_patch <<'P'\n%s\nP" % patch,
                                            "aggregated_output": "Success"}},
        {"type": "item.completed", "item": {"id": "i4", "type": "file_change",
                                            "changes": [{"path": "/w/c.py", "kind": "add"}]}},
        {"type": "item.completed", "item": {"id": "i5", "type": "web_search", "query": "q"}},
        {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}},
    ]
    calls = launcher.tool_calls("\n".join(json.dumps(x) for x in lines), "codex")
    assert [c["kind"] for c in calls] == ["shell", "write", "write", "web"]
    assert calls[0]["output"] == "a.py\n" and calls[0]["input"] == "bash -lc ls"
    assert calls[1]["paths"] == ["src/a.py", "src/b.py"] and calls[1]["text"] == "Now I patch."
    assert calls[2]["paths"] == ["/w/c.py"]
    # a saved Codex transcript (rollout records): an exec call that runs tools.apply_patch
    rollout = [
        {"timestamp": "T1", "type": "response_item", "payload": {
            "type": "function_call", "name": "exec", "call_id": "c1",
            "arguments": json.dumps({"cmd": "text(await tools.apply_patch(%s))"
                                     % json.dumps(patch)})}},
        {"timestamp": "T2", "type": "response_item", "payload": {
            "type": "function_call_output", "call_id": "c1", "output": "ok"}},
        {"timestamp": "T3", "type": "response_item", "payload": {
            "type": "function_call", "name": "exec_command", "call_id": "c2",
            "arguments": json.dumps({"cmd": "cat x"})}},
    ]
    calls = launcher.tool_calls("\n".join(json.dumps(x) for x in rollout), "codex")
    assert calls[0]["kind"] == "write" and calls[0]["paths"] == ["src/a.py", "src/b.py"]
    assert calls[0]["output"] == "ok" and calls[0]["ts"] == "T1"
    assert calls[1]["kind"] == "shell" and calls[1]["input"] == "cat x"
    # negative: events that are not calls give none
    assert launcher.tool_calls(json.dumps({"type": "turn.started"}), "codex") == []


# ================================================================ launches with a fake runtime

@JAILED
def test_a_launch_sees_only_its_worktree_and_read_paths_from_another_directory(lab, tmp_path,
                                                                             monkeypatch):
    secret_tmp = tmp_path / "host-tmp-secret.txt"
    secret_tmp.write_text("host tmp secret\n")
    home_dir = os.path.expanduser("~")
    cfg = os.environ["CLAUDE_CONFIG_DIR"]
    visible = [lab.worktree, os.path.join(lab.worktree, "README"), lab.refdata,
               os.path.join(lab.refdata, "table.txt")]
    # the parents of a mounted path are empty folders, so hidden files are named one by one
    hidden = [os.path.join(lab.root, "CLAUDE.md"), os.path.join(lab.root, "ALPACA-MANIFEST"),
              os.path.join(lab.root, ".alpaca", "alpaca.db"),
              os.path.join(lab.contract_dir, "contract.json"), lab.contract_dir,
              lab.other_tree, os.path.join(lab.other_tree, "SECRET"), lab.other_attempt,
              home_dir, cfg, os.path.join(cfg, "CLAUDE.md"), os.path.join(cfg, "settings.json"),
              os.path.join(cfg, ".credentials.json"), str(secret_tmp)]
    listed = [lab.root, os.path.join(lab.root, ".alpaca"), "/"]
    prompt = fakes.directive(mode="probe", exists=visible + hidden, list=listed,
                             read=[os.path.join(cfg, "CLAUDE.md")],
                             write=[os.path.join(lab.refdata, "table.txt"),
                                    os.path.join(lab.worktree, "new.txt")])
    spec = lab.implementer(prompt=prompt)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)                    # the launcher starts from another directory
    result = launcher.launch(dataclasses.asdict(spec))  # a mapping; root and conn default
    assert result["outcome"] == "ok", result["reason"]
    rep = _probe(result)
    for p in visible:
        assert rep["exists"][p] is True, p
    for p in hidden:
        assert rep["exists"][p] is False, p
    assert rep["lists"][lab.root] == [".alpaca"]
    assert rep["lists"][os.path.join(lab.root, ".alpaca")] == ["research", "worktrees"]
    assert set(rep["lists"]["/"]) <= {"bin", "dev", "etc", "lib", "lib64", "proc", "run",
                                      "sbin", "tmp", "usr"}
    # PWD is the working directory, which the agent's cwd equals, and the paths are absolute.
    # bwrap's --chdir also sets PWD in the jail, so the launcher's own PWD value is guarded by
    # test_build_env_is_an_allowlist, not by this line.
    assert rep["cwd"] == lab.worktree and rep["env"]["PWD"] == lab.worktree
    for value in rep["argv"]:
        if os.sep in value and not value.startswith("{"):
            assert os.path.isabs(value), value
    assert rep["stdin_sha256"] == hashlib.sha256(prompt.encode()).hexdigest()
    # read paths are read-only, the worktree writable
    assert rep["writes"][os.path.join(lab.refdata, "table.txt")].startswith("ERR")
    assert rep["writes"][os.path.join(lab.worktree, "new.txt")] == "ok"
    # the fail case: no operator setting, memory or credential beyond the runtime's own key
    env = rep["env"]
    assert env["CLAUDE_CONFIG_DIR"] != cfg and env["HOME"] != home_dir
    assert rep["reads"][os.path.join(cfg, "CLAUDE.md")].startswith("ERR")
    expected = {"PATH", "HOME", "TMPDIR", "PWD", "TERM", "NO_COLOR", "SHELL", "LANG", "USER",
                "LOGNAME", "HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "https_proxy",
                "http_proxy", "all_proxy", "NO_PROXY", "no_proxy", "CLAUDE_CONFIG_DIR",
                "CLAUDE_CODE_DISABLE_AUTO_MEMORY", "DISABLE_TELEMETRY",
                "DISABLE_ERROR_REPORTING", "DISABLE_AUTOUPDATER", "ANTHROPIC_API_KEY",
                "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
                "GIT_CONFIG_NOSYSTEM", "GIT_CONFIG_GLOBAL", procs.TAG_VAR}
    assert set(env) == expected, sorted(set(env) ^ expected)
    assert not os.path.exists(result["home"])       # the host no longer holds the home


@JAILED
def test_a_launch_is_recorded_snapshotted_and_cleaned(lab):
    spec = lab.implementer(op="op-010", worker="rr-001-implementer-s1")
    from alpaca.formation import dispatch
    dispatch.declare_budget(lab.conn, "op-010", 3)
    result = launcher.launch(spec, conn=lab.conn, root=lab.root)
    assert result["outcome"] == "ok" and result["rc"] == 0 and not result["held"]
    started = lab.events(events.LAUNCH)
    ended = lab.events(events.LAUNCH_END)
    assert len(started) == len(ended) == 1
    s, e = started[0]["data"], ended[0]["data"]
    assert result["launch_event"] == started[0]["id"] and result["launch_end"] == ended[0]["id"]
    assert s["key"] == "t-140/implementer/t1" and s["runtime"] == "claude"
    assert s["binary"] == lab.claude and s["pinned"] is False and s["runtime_version"] == "9.9.9"
    assert s["binary_sha256"] == hashlib.sha256(open(lab.claude, "rb").read()).hexdigest()
    assert "ANTHROPIC_API_KEY" in s["env_keys"] and "anthropic-key" not in json.dumps(s)
    assert s["egress_allow"] == ["api.anthropic.com:443"] and s["max_usd"] == 4.0
    assert s["tag"].startswith("t-140-implementer-t1-") and len(s["tag"]) == 33
    assert s["prompt_sha256"] == hashlib.sha256(spec.prompt.encode()).hexdigest()
    assert open(s["prompt"]).read() == spec.prompt
    assert e["outcome"] == "ok" and e["verdict"] == "PASS" and e["exit"] == vc.PASS
    assert e["stream_sha256"] == hashlib.sha256(open(e["stream"], "rb").read()).hexdigest()
    evidence = os.path.join(lab.attempt_dir, "launches", "t-140-implementer-t1")
    assert os.path.dirname(e["stream"]) == evidence
    assert sorted(os.listdir(evidence)) >= ["egress.jsonl", "prompt.txt", "stream.jsonl"]
    assert e["tokens"] and e["tokens"] > 0 and e["priced"] is True
    assert result["final_message"] == "done"
    # the transcript is copied into the record before the home goes
    assert result["transcript"] == os.path.join(lab.root, ".alpaca", "transcripts",
                                                "%s.jsonl" % spec.session)
    assert os.path.isfile(result["transcript"])
    snaps = lab.events("transcript-snapshot")
    assert snaps and snaps[-1]["session"] == spec.session
    row = db.rows(lab.conn, "sessions", "sid=?", (spec.session,))[0]
    assert row["ended"] and row["transcript"] == result["transcript"] and row["cwd"] == spec.cwd
    spawns = lab.events(dispatch.SPAWN_KIND)
    assert spawns and spawns[-1]["data"]["worker"] == "rr-001-implementer-s1"
    assert lab.booked == [{"cost_usd": e["cost_usd"], "tokens": e["tokens"],
                           "session": spec.session, "in_txn": True, "run": RUN,
                           "task": ATTEMPT}]
    # the home and the socket folder are gone; no credential reached the evidence
    assert not os.path.exists(result["home"])
    assert not os.path.exists(_egress_path(lab.root, RUN, "t-140-implementer-t1"))
    assert not any("credentials" in n for n in os.listdir(evidence))
    calls = launcher.tool_calls(result["stream"], "claude")
    assert calls[0]["tool"] == "Bash" and calls[0]["output"] == "total 0"
    # a resumed step with the same key gets the recorded result and launches nothing
    again = launcher.launch(spec, conn=lab.conn, root=lab.root)
    assert again == result
    assert len(lab.events(events.LAUNCH)) == 1 and len(lab.booked) == 1


def test_a_launch_with_no_end_is_lost_once(lab):
    spec = lab.implementer()
    started = events.emit(lab.conn, events.LAUNCH, run=RUN, ref=ATTEMPT, op=None,
                          session=spec.session, data={"key": spec.key})
    first = launcher.launch(spec, conn=lab.conn, root=lab.root)
    assert first["outcome"] == first["infra"] == "lost"
    assert first["launch_event"] == started["id"]
    second = launcher.launch(spec, conn=lab.conn, root=lab.root)
    assert second["outcome"] == "lost"
    infra = lab.events(events.INFRA)
    assert len(infra) == 1 and infra[0]["data"]["class"] == "lost"
    assert infra[0]["data"]["verdict"] == "BLOCKED"
    # negative: nothing ran and no end was recorded
    assert lab.events(events.LAUNCH_END) == []


def test_a_budget_hold_records_nothing_and_launches_nothing(lab):
    lab.check = fakes.go_check(posture="HOLD")
    before = lab.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    result = launcher.launch(lab.implementer(), conn=lab.conn, root=lab.root)
    assert result["held"] is True and result["outcome"] == "held"
    assert result["reason"] == "budget test"
    assert lab.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == before
    assert db.rows(lab.conn, "sessions", "sid=?", (lab.implementer().session,)) == []
    # negative: with GO the same key launches (checked in the recorded launch test)
    assert not os.path.exists(paths.launch_home(lab.root, RUN, "t-140-implementer-t1"))


@JAILED
def test_a_pinned_binary_runs_exactly_and_a_changed_one_refuses(lab, tmp_path):
    pinned = fakes.fake_claude(str(tmp_path / "pinned"))
    sha = hashlib.sha256(open(pinned, "rb").read()).hexdigest()
    spec = lab.implementer(binary=pinned, binary_sha256=sha, prompt=fakes.directive(
        mode="probe"))
    result = launcher.launch(spec, conn=lab.conn, root=lab.root)
    assert result["outcome"] == "ok"
    assert _probe(result)["argv"][0] == os.path.realpath(pinned)
    s = lab.events(events.LAUNCH)[-1]["data"]
    assert s["pinned"] is True and s["binary"] == os.path.realpath(pinned)
    # negative: a pin whose file changed refuses the launch before any research-launch
    with open(pinned, "a") as fh:
        fh.write("# changed\n")
    bad = lab.implementer(key="t-140/implementer/t2", try_n=2, binary=pinned,
                          binary_sha256=sha)
    refused = launcher.launch(bad, conn=lab.conn, root=lab.root)
    assert refused["outcome"] == refused["infra"] == "start-failed"
    assert "pinned" in refused["reason"]
    assert len(lab.events(events.LAUNCH)) == 1
    infra = lab.events(events.INFRA)[-1]["data"]
    assert infra["class"] == "start-failed" and infra["key"] == "t-140/implementer/t2"
    nopin = lab.implementer(key="t-140/implementer/t3", try_n=3, binary=pinned)
    assert launcher.launch(nopin, conn=lab.conn, root=lab.root)["outcome"] == "start-failed"


def test_a_path_outside_the_subject_folders_refuses_the_launch(lab):
    spec = lab.implementer(read_paths=(lab.other_tree,))
    result = launcher.launch(spec, conn=lab.conn, root=lab.root)
    assert result["outcome"] == "start-failed" and "outside" in result["reason"]
    assert lab.events(events.LAUNCH) == []
    assert lab.events(events.INFRA)[-1]["data"]["class"] == "start-failed"


@JAILED
def test_a_timeout_kills_the_agent_and_is_its_own_verdict(lab):
    token = "613.%d" % (os.getpid() % 100000)
    spec = lab.implementer(timeout_s=3, prompt=fakes.directive(mode="escape", token=token,
                                                               then="sleep"))
    t0 = time.monotonic()
    result = launcher.launch(spec, conn=lab.conn, root=lab.root)
    assert time.monotonic() - t0 < 30
    assert result["outcome"] == "timeout" and result["timed_out"] is True and result["rc"] is None
    assert result["infra"] is None and result["killed"]
    end = lab.events(events.LAUNCH_END)[-1]["data"]
    assert end["verdict"] == "FAIL" and end["killed_pids"]
    for _ in range(40):
        if not _alive_with(token):
            break
        time.sleep(0.05)
    # the child that left the process group died with the jail
    assert _alive_with(token) == []
    assert lab.events(events.INFRA) == []            # a timeout is not an infrastructure class


@JAILED
@pytest.mark.parametrize("case,lines,rc,want", [
    ("start-failed", ["exec failed: no such file"], 1, "start-failed"),   # bare: no event
    ("crash", ['{"type": "system", "subtype": "init"}', "Traceback (most recent call last)"], 3,
     "crash"),
    ("quota", ['{"type": "result", "is_error": true, '
               '"result": "Claude AI usage limit reached|1759300000"}'], 1, "quota"),
    ("auth", ['{"type": "result", "is_error": true, '
              '"result": "Invalid API key. Please run /login"}'], 1, "auth"),
])
def test_a_failed_launch_is_classified_and_recorded(lab, case, lines, rc, want):
    spec = lab.implementer(prompt=fakes.directive(mode="lines", lines=lines, rc=rc,
                                                  bare=case == "start-failed"))
    result = launcher.launch(spec, conn=lab.conn, root=lab.root)
    assert result["outcome"] == result["infra"] == want
    end = lab.events(events.LAUNCH_END)[-1]["data"]
    assert end["outcome"] == want and end["verdict"] == "BLOCKED" and end["rc"] == rc
    infra = lab.events(events.INFRA)[-1]["data"]
    assert infra["class"] == want and infra["key"] == spec.key
    if want == "quota":
        assert result["reset_at"] == "2025-10-01T06:26:40Z"
        assert infra["wait_until"] == "2025-10-01T06:26:40Z"
    else:
        assert infra["wait_until"] is None
    assert len(lab.booked) == 1


def _unjailed(monkeypatch, tmp_path):
    """Run the runtime without bwrap, so only the launcher's own cleanup can catch a child that
    left the process group: the wrapper drops the forwarder and execs the runtime argv."""
    wrapper = tmp_path / "nojail.py"
    wrapper.write_text("#!/usr/bin/python3\nimport os, sys\na = sys.argv[1:]\n"
                       "i = a.index('--', 1)\nos.execv(a[i + 1], a[i + 1:])\n")
    os.chmod(wrapper, 0o755)
    monkeypatch.setattr(jail, "argv", lambda spec: [str(wrapper)])
    monkeypatch.setattr(jail, "binary", lambda: "/bin/true")


def test_cleanup_kills_a_child_that_left_the_process_group_after_a_normal_exit(
        lab, monkeypatch, tmp_path):
    _unjailed(monkeypatch, tmp_path)
    token = "617.%d" % (os.getpid() % 100000)
    spec = lab.implementer(prompt=fakes.directive(mode="escape", token=token))
    try:
        result = launcher.launch(spec, conn=lab.conn, root=lab.root)
        assert result["outcome"] == "ok", result["reason"]
        for _ in range(40):
            if not _alive_with(token):
                break
            time.sleep(0.05)
        assert _alive_with(token) == [], "the escaped child outlived its launch"
        assert result["killed"], "the sweep listed no pid"
    finally:
        for pid in _alive_with(token):
            os.kill(pid, 9)
    # negative: without the launcher's sweep the same child survives the normal exit
    token2 = "619.%d" % (os.getpid() % 100000)
    monkeypatch.setattr(launcher, "_sweep", lambda tag: ())
    spec2 = lab.implementer(key="t-140/implementer/t2", try_n=2,
                            prompt=fakes.directive(mode="escape", token=token2))
    try:
        launcher.launch(spec2, conn=lab.conn, root=lab.root)
        end = time.monotonic() + 10                   # a loaded host may not have exec'd it yet
        while not _alive_with(token2) and time.monotonic() < end:
            time.sleep(0.05)
        assert _alive_with(token2), "the control case should leave the child alive"
    finally:
        for pid in _alive_with(token2):
            os.kill(pid, 9)


def _bystander(tag: str, token: str):
    """A process outside any launch, in its own session, carrying ALPACA_RESEARCH_LAUNCH=tag."""
    import subprocess
    return subprocess.Popen(["/usr/bin/sleep", token], env={"PATH": "/usr/bin:/bin",
                                                             procs.TAG_VAR: tag},
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, start_new_session=True)


@pytest.mark.parametrize("end", ["exit", "timeout"])
def test_cleanup_kills_no_process_of_another_launch_that_shares_the_key(
        lab, monkeypatch, tmp_path, end):
    """Check round 2 (B1): the tag sweep reaches every process of this user, so the tag must be
    unique on the machine. A process that carries the bare launch id, or the tag of the same
    key under another project root, is not this launch's and must outlive both a normal exit
    and a timeout. A process that carries this launch's own tag is killed although it is not a
    descendant of the agent: that is the sweep of gap map F13, the positive control here."""
    _unjailed(monkeypatch, tmp_path)
    spec = lab.implementer()
    if end == "timeout":
        spec = dataclasses.replace(spec, timeout_s=2,
                                   prompt=fakes.directive(mode="sleep", seconds=60))
    bare = paths.launch_id(spec.key)
    assert bare == "t-140-implementer-t1"

    def home_under(root):
        base = paths.launch_home(root, RUN, bare)
        return rtypes.LaunchHome(launch_id=bare, root=base, home=base + "/home",
                                 config=base + "/claude", out=base + "/out")

    own = launcher._tag(home_under(lab.root))
    elsewhere = launcher._tag(home_under(str(tmp_path / "another-project")))
    n = os.getpid() % 100000
    foreign = {"bare": _bystander(bare, "641.%d" % n),
               "elsewhere": _bystander(elsewhere, "643.%d" % n)}
    mine = _bystander(own, "647.%d" % n)
    try:
        result = launcher.launch(spec, conn=lab.conn, root=lab.root)
        assert result["outcome"] == ("ok" if end == "exit" else "timeout"), result["reason"]
        assert lab.events(events.LAUNCH)[-1]["data"]["tag"] == own != bare
        killed = set(result["killed"])
        assert killed == set(lab.events(events.LAUNCH_END)[-1]["data"]["killed_pids"])
        for name, proc in foreign.items():
            assert proc.poll() is None, "the %s bystander was killed" % name
            assert proc.pid not in killed, name
        # positive control: the launch's own tag is swept wherever the process is
        assert mine.wait(timeout=10) == -9
        assert mine.pid in killed
    finally:
        for proc in list(foreign.values()) + [mine]:
            if proc.poll() is None:
                proc.kill()
            proc.wait()


def test_a_spawn_over_the_op_budget_records_the_refusal_and_launches_nothing(
        lab, monkeypatch, tmp_path):
    """Check round 2 (m3): dispatch.spawn runs in the launch's transaction; a refusal keeps its
    dispatch-spawn-refused event, records research-infra start-failed, records no
    research-launch and no sessions row, removes the home, and never starts the runtime."""
    from alpaca.formation import dispatch
    _unjailed(monkeypatch, tmp_path)
    dispatch.declare_budget(lab.conn, "op-010", 1)
    dispatch.spawn(lab.conn, "op-010", "rr-001-proposer-s1")
    spec = lab.implementer(op="op-010", worker="rr-001-implementer-s1",
                           prompt=fakes.directive(mode="probe"))
    result = launcher.launch(spec, conn=lab.conn, root=lab.root)
    assert result["outcome"] == result["infra"] == "start-failed"
    assert "budget" in result["reason"]
    refused = lab.events(dispatch.SPAWN_REFUSED_KIND)
    assert len(refused) == 1 and refused[0]["data"]["workers"] == ["rr-001-implementer-s1"]
    infra = lab.events(events.INFRA)
    assert len(infra) == 1 and infra[0]["data"]["class"] == "start-failed"
    assert "budget" in infra[0]["data"]["detail"]
    assert lab.events(events.LAUNCH) == [] and lab.events(events.LAUNCH_END) == []
    assert db.rows(lab.conn, "sessions", "sid=?", (spec.session,)) == []
    assert not os.path.exists(paths.launch_home(lab.root, RUN, "t-140-implementer-t1"))
    assert [e["data"]["worker"] for e in lab.events(dispatch.SPAWN_KIND)] == [
        "rr-001-proposer-s1"]
    # negative: with room in the budget the same launch spawns its worker and runs
    dispatch.declare_budget(lab.conn, "op-010", 2)
    again = launcher.launch(dataclasses.replace(spec, key="t-140/implementer/t2", try_n=2),
                            conn=lab.conn, root=lab.root)
    assert again["outcome"] == "ok", again["reason"]
    assert len(lab.events(events.LAUNCH)) == 1
    assert lab.events(dispatch.SPAWN_KIND)[-1]["data"]["worker"] == "rr-001-implementer-s1"


@JAILED
def test_the_egress_proxy_is_the_only_way_out(lab, monkeypatch):
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(4)
    port = server.getsockname()[1]

    def echo():
        while True:
            try:
                conn, _ = server.accept()
            except OSError:
                return
            data = conn.recv(100)
            conn.sendall(b"echo:" + data)
            conn.close()
    threading.Thread(target=echo, daemon=True).start()
    allowed = "127.0.0.1:%d" % port
    monkeypatch.setattr(egress, "allowed_hosts", lambda runtime, environ, contract: (allowed,))
    spec = lab.implementer(prompt=fakes.directive(mode="probe", proxy=[allowed, "example.com:443"],
                                                  connect=[["127.0.0.1", port]]))
    try:
        result = launcher.launch(spec, conn=lab.conn, root=lab.root)
    finally:
        server.close()
    rep = _probe(result)
    assert rep["proxy"][allowed]["status"].split()[1] == "200"
    assert rep["proxy"][allowed]["echo"] == "echo:ping\n"
    assert rep["proxy"]["example.com:443"]["status"].split()[1] == "403"
    # negative: the host's loopback is out of reach without the proxy
    assert rep["connect"][allowed].startswith("ERR")
    log = [json.loads(x) for x in open(result["egress_log"])]
    assert [(x["host"], x["allowed"]) for x in log] == [("127.0.0.1", True),
                                                          ("example.com", False)]
    end = lab.events(events.LAUNCH_END)[-1]["data"]
    assert end["egress_refused"] == 1
    assert end["egress_sha256"] == hashlib.sha256(open(result["egress_log"], "rb").read()).hexdigest()


@JAILED
def test_the_operator_credentials_file_is_bound_read_only_and_never_kept(lab):
    os.environ.pop("ANTHROPIC_API_KEY")
    cred = os.path.join(os.environ["CLAUDE_CONFIG_DIR"], ".credentials.json")
    before = open(cred).read()
    # the runtime sees its login at the config home's own path, bound read-only
    home_cfg = os.path.join(paths.launch_home(lab.root, RUN, "t-140-implementer-t1"), "claude",
                            ".credentials.json")
    cred_dir = os.path.join(os.path.dirname(os.path.dirname(home_cfg)), "cred")
    spec = lab.implementer(prompt=fakes.directive(mode="probe", slurp=[home_cfg],
                                                  write=[home_cfg], exists=[cred_dir, cred]))
    result = launcher.launch(spec, conn=lab.conn, root=lab.root)
    rep = _probe(result)
    # the runtime's own login works: it read its login, whose tokens the launcher then replaced
    # in the stream before the stream became evidence
    seen = json.loads(rep["slurps"][home_cfg])
    assert seen == {"claudeAiOauth": dict(OPERATOR_LOGIN, accessToken="[REDACTED-CREDENTIAL]",
                                          refreshToken="[REDACTED-CREDENTIAL]")}
    stream = open(result["stream"]).read()
    assert "operator-oauth" not in stream and "operator-refresh-token" not in stream
    # the fail case (check round 2, adversarial B1): the agent never sees the operator's MCP
    # server tokens or device token, which _secrets does not redact, so they would show here
    assert not any(word in stream for word in OPERATOR_ONLY), stream
    assert rep["exists"] == {cred_dir: False, cred: False}
    assert "stream.jsonl" in lab.events(events.LAUNCH_END)[-1]["data"]["redacted"]
    assert rep["writes"][home_cfg].startswith("ERR")         # and is never written back
    assert "ANTHROPIC_API_KEY" not in rep["env"]
    assert open(cred).read() == before
    # negative: the evidence holds no copy of the file, and the home with its bind is gone
    evidence = os.path.dirname(result["stream"])
    kept = sorted(os.listdir(evidence))
    assert {"egress.jsonl", "prompt.txt", "stream.jsonl"} <= set(kept)
    assert not any("credential" in name for name in kept), kept
    assert not os.path.exists(result["home"])


@JAILED
def test_a_codex_launch_checks_its_features_and_reads_its_last_message(lab):
    spec = lab.implementer(runtime="codex", prompt=fakes.directive(mode="ok"))
    result = launcher.launch(spec, conn=lab.conn, root=lab.root)
    assert result["outcome"] == "ok", result["reason"]
    assert result["final_message"] == "final note from codex"
    assert result["transcript"] and os.path.isfile(result["transcript"])
    s = lab.events(events.LAUNCH)[-1]["data"]
    assert s["egress_allow"] == ["api.openai.com:443", "chatgpt.com:443"]
    assert s["runtime_version"] == "1.2.3"
    end = lab.events(events.LAUNCH_END)[-1]["data"]
    assert end["tokens"] == 27
    calls = launcher.tool_calls(result["stream"], "codex")
    assert calls and calls[0]["kind"] == "shell" and calls[0]["output"] == "hello\n"
    # negative: a Codex whose features list shows one of them on refuses the launch
    bad = fakes.fake_codex(os.path.join(lab.tmp, "badcodex"), on=("hooks",))
    sha = hashlib.sha256(open(bad, "rb").read()).hexdigest()
    spec2 = lab.implementer(runtime="codex", key="t-140/implementer/t2", try_n=2,
                            binary=bad, binary_sha256=sha)
    refused = launcher.launch(spec2, conn=lab.conn, root=lab.root)
    assert refused["outcome"] == "start-failed" and "hooks" in refused["reason"]
    assert len(lab.events(events.LAUNCH)) == 1


@JAILED
@pytest.mark.parametrize("runtime,mode", [("claude", "link_transcript"),
                                          ("codex", "link_message"),
                                          ("codex", "link_sessions")])
def test_a_link_the_agent_plants_is_never_read_through(lab, runtime, mode):
    record = os.path.join(lab.root, ".alpaca", "alpaca.db")
    target = record if mode != "link_sessions" else os.path.join(lab.root, ".alpaca")
    spec = lab.implementer(runtime=runtime, prompt=fakes.directive(mode=mode, target=target))
    result = launcher.launch(spec, conn=lab.conn, root=lab.root)
    assert result["outcome"] == result["infra"] == "lost"
    assert "collect refused" in result["reason"]
    assert lab.events(events.LAUNCH_END) == []
    assert lab.events(events.INFRA)[-1]["data"]["class"] == "lost"
    # negative: no copy of the record was made through the link
    assert not os.path.exists(os.path.join(lab.root, ".alpaca", "transcripts",
                                           "%s.jsonl" % spec.session))
    assert result["final_message"] is None
    assert not os.path.exists(result["home"])


def test_the_evidence_copy_skips_links_in_out(tmp_path):
    src = tmp_path / "out"
    src.mkdir()
    (src / "stream.jsonl").write_text("{}\n")
    secret = tmp_path / "secret"
    secret.write_text("do not copy\n")
    os.symlink(secret, src / "egress.jsonl")
    dst = tmp_path / "evidence"
    skipped = launcher._copy_out(str(src), str(dst))
    assert skipped == ["egress.jsonl"]
    assert os.listdir(dst) == ["stream.jsonl"]
    # negative: a plain file is copied by its bytes
    assert (dst / "stream.jsonl").read_text() == "{}\n"


def test_a_symlinked_message_or_sessions_folder_is_refused_by_the_readers(tmp_path):
    home = rtypes.LaunchHome(launch_id="l", root=str(tmp_path / "h"), home=str(tmp_path / "h/home"),
                             config=str(tmp_path / "h/codex"), out=str(tmp_path / "h/out"))
    for sub in ("home", "codex", "out", "msg"):
        os.makedirs(os.path.join(home.root, sub))
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "x").write_text("secret")
    os.symlink(outside / "x", os.path.join(home.root, "msg", "last-message.txt"))
    with pytest.raises(launcher._Broken):
        launcher._final_message("codex", home, "")
    os.symlink(outside, os.path.join(home.config, "sessions"))
    spec = rtypes.LaunchSpec(role="notes", runtime="codex", session="s", workdir="/w", cwd="/w",
                             prompt="", timeout_s=1, key="t-1/notes/t1")
    with pytest.raises(launcher._Broken):
        launcher._codex_transcript(home, spec, json.dumps({"type": "thread.started",
                                                           "thread_id": "abc"}))
    # negative: a plain file inside msg/ is read
    os.unlink(os.path.join(home.root, "msg", "last-message.txt"))
    fakes.Lab.write(os.path.join(home.root, "msg", "last-message.txt"), "note")
    assert launcher._final_message("codex", home, "") == "note"


# ================================================================ round-1 check findings

@JAILED
def test_a_subagents_link_the_agent_plants_makes_the_launch_lost_and_copies_nothing(lab):
    """Adversarial B1: <projects>/<entry>/<uuid>/subagents as a link to a host folder the jail
    hid. The snapshot must not copy what the link names into .alpaca/transcripts/."""
    hidden = os.path.join(lab.tmp, "operator-projects")
    fakes.Lab.write(os.path.join(hidden, "-home-op-secret", "abc", "subagents", "agent-1.jsonl"),
                    '{"type": "user", "text": "OPERATOR-PRIVATE-TRANSCRIPT"}\n')
    spec = lab.implementer(prompt=fakes.directive(mode="subagent_link", target=hidden))
    result = launcher.launch(spec, conn=lab.conn, root=lab.root)
    assert result["outcome"] == result["infra"] == "lost", result["reason"]
    assert "collect refused" in result["reason"]
    assert lab.events(events.LAUNCH_END) == []
    kept = os.path.join(lab.root, ".alpaca", "transcripts")
    copied = []
    for folder, _dirs, names in os.walk(kept):
        for name in names:
            path = os.path.join(folder, name)
            copied.append(path)
            assert "OPERATOR-PRIVATE" not in open(path, errors="replace").read(), path
    assert not any(spec.session in p for p in copied), copied
    assert not os.path.exists(result["home"])


@JAILED
def test_subagent_transcripts_are_staged_by_bytes_and_a_deep_link_is_lost(lab):
    # positive: a real subagents tree is copied into the record through the stage
    spec = lab.implementer(prompt=fakes.directive(mode="subagent_ok"))
    result = launcher.launch(spec, conn=lab.conn, root=lab.root)
    assert result["outcome"] == "ok", result["reason"]
    local = os.path.join(lab.root, ".alpaca", "transcripts", spec.session, "subagents",
                         "task-1", "agent-1.jsonl")
    assert os.path.isfile(local) and '"sub"' in open(local).read()
    # negative: a link one folder below subagents/ makes the launch lost
    hidden = os.path.join(lab.tmp, "hidden-deep")
    fakes.Lab.write(os.path.join(hidden, "agent-9.jsonl"), '{"secret": "DEEP"}\n')
    spec2 = lab.implementer(key="t-140/implementer/t2", try_n=2,
                            session="%s-%s-implementer-t2" % (RUN, ATTEMPT),
                            prompt=fakes.directive(mode="subagent_deep_link", target=hidden))
    lost = launcher.launch(spec2, conn=lab.conn, root=lab.root)
    assert lost["outcome"] == "lost" and "link" in lost["reason"], lost["reason"]
    assert not os.path.exists(os.path.join(lab.root, ".alpaca", "transcripts", spec2.session,
                                           "subagents"))


def _stage_home(tmp_path, *, subagents=()):
    """A launch home with a transcript of 60 bytes and the given subagent files (name, bytes)."""
    root = tmp_path / "h"
    home = rtypes.LaunchHome(launch_id="l", root=str(root), home=str(root / "home"),
                             config=str(root / "claude"), out=str(root / "out"))
    folder = root / "claude" / "projects" / "-p"
    folder.mkdir(parents=True)
    src = folder / "u.jsonl"
    src.write_bytes(b"x" * 59 + b"\n")
    for name, size in subagents:
        leaf = folder / "u" / "subagents" / name
        leaf.parent.mkdir(parents=True, exist_ok=True)
        leaf.write_bytes(b"y" * (size - 1) + b"\n")
    return home, str(src), root


def test_the_stage_copies_at_most_its_byte_cap_in_all(tmp_path, monkeypatch):
    """Check round 2 of t-098 (adversarial B2): the transcript and every subagent file count
    against one cap, checked on the open file before a byte is read."""
    monkeypatch.setattr(launcher, "_TRANSCRIPT_MAX", 100)
    home, src, root = _stage_home(tmp_path, subagents=[("agent-1.jsonl", 30),
                                                       ("agent-2.jsonl", 30)])
    with pytest.raises(launcher._Broken, match="over the"):      # 60 + 30 + 30 > 100
        launcher._stage_transcript(src, home, ())
    # a transcript over the cap on its own is refused before anything is written
    big, big_src, big_root = _stage_home(tmp_path / "big")
    monkeypatch.setattr(launcher, "_TRANSCRIPT_MAX", 59)
    with pytest.raises(launcher._Broken, match="60 bytes"):
        launcher._stage_transcript(big_src, big, ())
    assert os.listdir(big_root / "stage") == []
    # negative: at the cap exactly, all of it is staged
    monkeypatch.setattr(launcher, "_TRANSCRIPT_MAX", 120)
    home2, src2, root2 = _stage_home(tmp_path / "ok", subagents=[("agent-1.jsonl", 30),
                                                                 ("agent-2.jsonl", 30)])
    staged = launcher._stage_transcript(src2, home2, ())
    assert os.path.getsize(staged) == 60
    assert sorted(os.listdir(root2 / "stage" / "u" / "subagents")) == ["agent-1.jsonl",
                                                                      "agent-2.jsonl"]


def test_the_redacting_copy_stops_at_its_byte_cap(tmp_path):
    """A file that grows past its fstat size is still cut off at max_bytes (a byte cap, not
    only a size check)."""
    src = tmp_path / "src"
    src.write_bytes(b"z" * 5000)
    with open(src, "rb") as fh, pytest.raises(launcher._Broken, match="more than 4096"):
        launcher._copy_redacted(fh, str(tmp_path / "dst"), (), block=1024, max_bytes=4096)
    assert os.path.getsize(tmp_path / "dst") <= 4096
    # negative: with room for every byte the copy is whole
    with open(src, "rb") as fh:
        launcher._copy_redacted(fh, str(tmp_path / "dst2"), (), block=1024, max_bytes=5000)
    assert (tmp_path / "dst2").read_bytes() == b"z" * 5000


def test_the_stage_counts_folders_toward_its_entry_cap(tmp_path, monkeypatch):
    """Adversarial m2 of check round 2: a wide tree of empty folders under subagents/ is not
    recreated folder by folder past _SCAN_MAX."""
    monkeypatch.setattr(launcher, "_SCAN_MAX", 5)
    home, src, root = _stage_home(tmp_path)
    for i in range(8):
        (root / "claude" / "projects" / "-p" / "u" / "subagents" / ("d%d" % i)).mkdir(
            parents=True)
    with pytest.raises(launcher._Broken, match="more than 5 entries"):
        launcher._stage_transcript(src, home, ())
    assert not (root / "stage" / "u").exists()          # refused before any folder is made
    # negative: under the cap the folders are staged
    home2, src2, root2 = _stage_home(tmp_path / "ok")
    for i in range(4):
        (root2 / "claude" / "projects" / "-p" / "u" / "subagents" / ("d%d" % i)).mkdir(
            parents=True)
    launcher._stage_transcript(src2, home2, ())
    assert len(os.listdir(root2 / "stage" / "u" / "subagents")) == 4


@JAILED
@pytest.mark.parametrize("where", ["transcript", "subagent"])
def test_a_sparse_transcript_of_gigabytes_is_lost_at_once_and_never_copied(lab, monkeypatch,
                                                                          where):
    """Check round 2 of t-098 (adversarial B2): the agent truncates its transcript (or a
    subagent file) to 8 GiB, which costs it no disk. The launch must end lost well inside its
    timeout, and the host must never start copying the file: the spy below refuses any copy of
    a source over the cap before a byte is written, so a regression fails here without filling
    the disk."""
    copies = []
    real = launcher._copy_redacted

    def spy(src_fh, dst, secrets, **kw):
        size = os.fstat(src_fh.fileno()).st_size
        copies.append((dst, size))
        if size > launcher._TRANSCRIPT_MAX:
            raise OSError("test guard: the host began to copy %d bytes into %s" % (size, dst))
        return real(src_fh, dst, secrets, **kw)

    monkeypatch.setattr(launcher, "_copy_redacted", spy)
    spec = lab.implementer(timeout_s=5, prompt=fakes.directive(mode="sparse", where=where,
                                                               bytes=8 * 1024 ** 3))
    started = time.monotonic()
    result = launcher.launch(spec, conn=lab.conn, root=lab.root)
    took = time.monotonic() - started
    assert result["outcome"] == result["infra"] == "lost", result["reason"]
    assert "over the" in result["reason"] and "8589934593 bytes" in result["reason"]
    assert took < spec.timeout_s, took
    assert all(size <= launcher._TRANSCRIPT_MAX for _, size in copies), copies
    assert not lab.events(events.LAUNCH_END)
    assert not os.path.exists(result["home"])


def test_the_stage_refuses_every_link_in_the_transcript_tree(tmp_path):
    root = tmp_path / "h"
    home = rtypes.LaunchHome(launch_id="l", root=str(root), home=str(root / "home"),
                             config=str(root / "claude"), out=str(root / "out"))
    folder = root / "claude" / "projects" / "-p"
    folder.mkdir(parents=True)
    src = folder / "u.jsonl"
    src.write_text('{"type": "user"}\n')
    outside = tmp_path / "outside"
    (outside / "subagents").mkdir(parents=True)
    (outside / "subagents" / "agent-1.jsonl").write_text("hidden\n")
    os.symlink(outside, folder / "u")                      # the sibling <stem>/ is a link
    with pytest.raises(launcher._Broken):
        launcher._stage_transcript(str(src), home, ())
    os.unlink(folder / "u")
    (folder / "u" / "subagents").mkdir(parents=True)
    os.symlink(outside / "subagents" / "agent-1.jsonl",
               folder / "u" / "subagents" / "agent-2.jsonl")
    with pytest.raises(launcher._Broken):                  # a linked subagent file
        launcher._stage_transcript(str(src), home, ())
    os.unlink(folder / "u" / "subagents" / "agent-2.jsonl")
    # negative: a plain tree is staged by bytes, with the credential values replaced
    (folder / "u" / "subagents" / "agent-3.jsonl").write_text("key sk-secret-value-123\n")
    staged = launcher._stage_transcript(str(src), home, (b"sk-secret-value-123",))
    assert staged == str(root / "stage" / "u.jsonl")
    copy = root / "stage" / "u" / "subagents" / "agent-3.jsonl"
    assert copy.read_text() == "key [REDACTED-CREDENTIAL]\n"
    assert not os.path.islink(staged)


@JAILED
@pytest.mark.parametrize("case,directive,want", [
    ("exit-0-before-any-event", dict(mode="lines", lines=[], rc=0, bare=True), "start-failed"),
    ("exit-0-stderr-only", dict(mode="lines", lines=["boom"], rc=0, bare=True), "start-failed"),
    ("result-but-no-transcript", dict(mode="no_transcript"), "lost"),
    ("events-no-result-no-transcript",
     dict(mode="lines", lines=['{"type": "assistant", "message": {"content": []}}'], rc=0),
     "lost"),
])
def test_a_runtime_that_leaves_no_event_or_no_transcript_never_passes(lab, case, directive,
                                                                       want):
    """Adversarial B2: exit 0 with no stream event is start-failed; a normal end with no
    transcript is lost (section 6.1, step 8: the glob resolves to one regular file or the
    launch is broken)."""
    spec = lab.implementer(prompt=fakes.directive(**directive))
    result = launcher.launch(spec, conn=lab.conn, root=lab.root)
    assert result["outcome"] == result["infra"] == want, result["reason"]
    assert lab.events(events.INFRA)[-1]["data"]["class"] == want
    if want == "lost":
        assert "no transcript" in result["reason"]
        assert lab.events(events.LAUNCH_END) == []
    else:
        end = lab.events(events.LAUNCH_END)[-1]["data"]
        assert end["verdict"] == "BLOCKED" and end["rc"] == 0


@JAILED
def test_a_codex_run_with_no_rollout_is_lost(lab):
    spec = lab.implementer(runtime="codex", prompt=fakes.directive(mode="no_transcript"))
    result = launcher.launch(spec, conn=lab.conn, root=lab.root)
    assert result["outcome"] == "lost" and "no transcript" in result["reason"]


def _open_planted_fifos(root: str) -> int:
    """Open, for writing, every FIFO the fake planted under the research folder, which releases
    a reader blocked on it; returns how many were opened."""
    opened = 0
    for folder, _dirs, names in os.walk(paths.research_dir(root)):
        for name in names:
            path = os.path.join(folder, name)
            try:
                if stat.S_ISFIFO(os.lstat(path).st_mode):
                    os.close(os.open(path, os.O_WRONLY | os.O_NONBLOCK))
                    opened += 1
            except OSError:
                pass
    return opened


def test_the_codex_sessions_check_refuses_special_files_and_counts_entries(tmp_path,
                                                                          monkeypatch):
    """No bwrap needed: _check_sessions raises _Broken for a FIFO, a socket or a link whose name
    ends in <thread>.jsonl, ignores one named for another thread, never opens what it checks
    (the FIFO has no writer, so an open would block), and stops past _SCAN_MAX entries,
    folders included."""
    thread = "0199a0e0-0000-7000-8000-000000000001"
    sessions = tmp_path / "sessions"
    day = sessions / "2026" / "09" / "30"
    day.mkdir(parents=True)
    (day / ("rollout-%s.jsonl" % thread)).write_text("{}\n")
    os.mkfifo(day / "other-0199a0e0-0000-7000-8000-000000000002.jsonl")
    launcher._check_sessions(str(sessions), thread)          # a regular rollout and a stranger
    fifo = day / ("x-%s.jsonl" % thread)
    os.mkfifo(fifo)
    with pytest.raises(launcher._Broken, match="not a regular file"):
        launcher._check_sessions(str(sessions), thread)
    fifo.unlink()
    sock = socket.socket(socket.AF_UNIX)
    monkeypatch.chdir(day)                                # the full path is over 108 bytes
    try:
        sock.bind("s-%s.jsonl" % thread)
        with pytest.raises(launcher._Broken, match="not a regular file"):
            launcher._check_sessions(str(sessions), thread)
    finally:
        sock.close()
        (day / ("s-%s.jsonl" % thread)).unlink()
    os.symlink(tmp_path / "elsewhere", day / ("l-%s.jsonl" % thread))
    with pytest.raises(launcher._Broken, match="link or not a regular file"):
        launcher._check_sessions(str(sessions), thread)
    (day / ("l-%s.jsonl" % thread)).unlink()
    launcher._check_sessions(str(sessions), thread)
    monkeypatch.setattr(launcher, "_SCAN_MAX", 5)
    for i in range(4):
        (sessions / ("d%d" % i)).mkdir()                   # folders count too
    with pytest.raises(launcher._Broken, match="more than 5 entries"):
        launcher._check_sessions(str(sessions), thread)


@JAILED
@pytest.mark.parametrize("kind,want", [("fifo", "lost"), ("link", "lost"), (None, "ok")])
def test_a_special_file_named_for_the_codex_thread_ends_the_launch_lost_at_once(lab, kind,
                                                                                 want):
    """Check round 3, adversarial B1: the Codex agent plants x-<thread>.jsonl beside its real
    rollout in $CODEX_HOME/sessions. A FIFO there once blocked the collect step forever, after
    the runtime ended and outside the launch's timeout, since transcripts.discover_codex opened
    it with a plain open(). Now the launcher lstat()s every name that ends in <thread>.jsonl
    first and a FIFO or a link makes the launch lost within seconds. Control (kind None): the
    same launch with only the regular rollout ends ok with its transcript."""
    target = os.path.join(lab.root, ".alpaca", "alpaca.db")
    spec = lab.implementer(runtime="codex", timeout_s=30, prompt=fakes.directive(
        mode="special_rollout", kind=kind, target=target))
    box: dict = {}

    def go():
        try:
            box["result"] = launcher.launch(spec, conn=None, root=lab.root)
        except BaseException as exc:          # noqa: BLE001
            box["error"] = repr(exc)

    t0 = time.monotonic()
    worker = threading.Thread(target=go, daemon=True)
    worker.start()
    worker.join(20)
    hung = worker.is_alive()
    if hung:
        _open_planted_fifos(lab.root)          # release the blocked reader before failing
        worker.join(30)
    took = time.monotonic() - t0
    assert not hung, "the collect step was still blocked %.0f s after the launch started" % took
    assert "error" not in box, box
    result = box["result"]
    assert result["outcome"] == want, result["reason"]
    if want == "lost":
        assert result["infra"] == "lost" and "not a regular file" in result["reason"]
        assert "x-" in result["reason"]
        assert lab.events(events.LAUNCH_END) == []
        assert lab.events(events.INFRA)[-1]["data"]["class"] == "lost"
        assert not os.path.exists(result["home"])
    else:
        assert result["transcript"] and os.path.exists(result["transcript"])
        assert len(lab.events(events.LAUNCH_END)) == 1


@JAILED
def test_cleanup_kills_a_child_with_an_empty_environment_after_a_normal_exit_in_the_jail(
        lab, monkeypatch, tmp_path):
    """Adversarial M2: a child that calls setsid and execs with an empty environment carries
    no launch tag, so the sweep cannot see it; in the jail the PID namespace ends with bwrap
    and takes the child with it. killed_pids stays empty in that case, since the kernel, not
    the launcher, killed it."""
    token = "623.%d" % (os.getpid() % 100000)
    spec = lab.implementer(prompt=fakes.directive(mode="escape_clean", token=token))
    try:
        result = launcher.launch(spec, conn=lab.conn, root=lab.root)
        assert result["outcome"] == "ok", result["reason"]
        for _ in range(40):
            if not _alive_with(token):
                break
            time.sleep(0.05)
        assert _alive_with(token) == [], "the untagged child outlived its jailed launch"
    finally:
        for pid in _alive_with(token):
            os.kill(pid, 9)
    # negative: unjailed, the same child evades the tag sweep, so the jail is what kills it
    _unjailed(monkeypatch, tmp_path)
    token2 = "627.%d" % (os.getpid() % 100000)
    spec2 = lab.implementer(key="t-140/implementer/t2", try_n=2,
                            prompt=fakes.directive(mode="escape_clean", token=token2))
    try:
        launcher.launch(spec2, conn=lab.conn, root=lab.root)
        end = time.monotonic() + 10                   # a loaded host may not have exec'd it yet
        while not _alive_with(token2) and time.monotonic() < end:
            time.sleep(0.05)
        assert _alive_with(token2), "the control case should leave the child alive"
    finally:
        for pid in _alive_with(token2):
            os.kill(pid, 9)


@JAILED
def test_the_credential_the_runtime_gets_is_redacted_from_what_outlives_the_launch(lab):
    """Adversarial M1: the agent prints its API key and copies it into its worktree."""
    old = os.path.join(lab.worktree, "OLD")
    fakes.Lab.write(old, "anthropic-key from before the launch\n")
    time.sleep(0.2)                                  # OLD's ctime is before the launch starts
    leak = os.path.join(lab.worktree, "leak.txt")
    # the agent sets the copy's mtime back an hour; its ctime still shows the change
    spec = lab.implementer(prompt=fakes.directive(mode="leak", into=leak, backdate=True))
    result = launcher.launch(spec, conn=lab.conn, root=lab.root)
    assert result["outcome"] == "ok", result["reason"]
    # in a write path the value is overwritten in place by a mask of its length (check round 4)
    assert open(leak).read() == "stolen=%s\n" % launcher._mask(len("anthropic-key")).decode()
    stream = open(result["stream"]).read()
    assert "anthropic-key" not in stream and "[REDACTED-CREDENTIAL]" in stream
    end = lab.events(events.LAUNCH_END)[-1]["data"]
    assert "stream.jsonl" in end["redacted"] and leak in end["redacted"]
    assert end["not_redacted"] == {"count": 0, "paths": [], "walk_stopped": []}
    assert "anthropic-key" not in json.dumps(end)
    # negative: a file the launch did not change is left as it was
    assert open(old).read() == "anthropic-key from before the launch\n"
    assert old not in end["redacted"]


def test_secrets_are_the_credential_variable_and_the_file_s_token_values(lab, tmp_path):
    cred = tmp_path / "c.json"
    cred.write_text(json.dumps({"claudeAiOauth": {"accessToken": "tok-12345678",
                                                  "scopes": ["user:inference"],
                                                  "refreshToken": "r" * 30,
                                                  "subscriptionType": "max"}}))
    home = rtypes.LaunchHome(launch_id="l", root="/h", home="/h/home", config="/h/claude",
                             out="/h/out", credential_file=str(cred))
    got = launcher._secrets("claude", home, {"ANTHROPIC_API_KEY": "anthropic-key"})
    assert set(got) == {b"anthropic-key", b"tok-12345678", b"r" * 30}
    # negative: a codex launch does not treat the Anthropic variable as its credential
    codex = launcher._secrets("codex", home, {"ANTHROPIC_API_KEY": "anthropic-key"})
    assert b"anthropic-key" not in codex


@JAILED
def test_a_long_final_message_is_capped_in_the_event_and_read_back_whole(lab):
    text = "y" * (launcher._FINAL_MAX + 100)
    spec = lab.implementer(prompt=fakes.directive(mode="ok", result=text))
    result = launcher.launch(spec, conn=lab.conn, root=lab.root)
    assert result["final_message"] == text
    end = lab.events(events.LAUNCH_END)[-1]["data"]
    assert len(end["final_message"]) == launcher._FINAL_MAX and end["final_message_truncated"]
    assert end["final_message_sha256"] == hashlib.sha256(text.encode()).hexdigest()
    assert launcher.launch(spec, conn=lab.conn, root=lab.root) == result
    # negative: a short message stays whole in the event and makes no file
    spec2 = lab.implementer(key="t-140/implementer/t2", try_n=2)
    short = launcher.launch(spec2, conn=lab.conn, root=lab.root)
    assert lab.events(events.LAUNCH_END)[-1]["data"]["final_message"] == "done"
    assert "final-message.txt" not in os.listdir(os.path.dirname(short["stream"]))


def test_a_launch_without_an_evidence_folder_or_attempt_is_refused(lab):
    spec = lab.implementer(attempt=None)
    with pytest.raises(LaunchError):
        launcher._evidence_dir(lab.root, spec, "l")
    # negative: an explicit evidence folder, or an attempt, names one
    assert launcher._evidence_dir(lab.root, dataclasses.replace(spec, evidence_dir="/e"),
                                  "l") == "/e"
    assert launcher._evidence_dir(lab.root, lab.implementer(), "l").endswith(
        os.path.join("t-140", "launches", "l"))


def test_the_stream_read_is_bounded(tmp_path):
    path = tmp_path / "s.jsonl"
    with open(path, "wb") as fh:
        fh.write(b'{"type": "system"}\n' + b"x" * 200 + b"\n" + b'{"type": "result"}\n')
    text = launcher._read_text(str(path), limit=100)
    assert len(text) <= 100
    # negative: a stream within the limit is read whole
    assert launcher._read_text(str(path)) == open(path).read()


@JAILED
@pytest.mark.skipif(REAL_CLAUDE is None, reason="needs ALPACA_TEST_REAL_RUNTIME=1 and a real "
                    "claude on the PATH")
def test_h15_a_headless_launch_runs_no_hook_of_the_worktree_s_settings(lab, monkeypatch):
    """Gap map H15 (design/research-engine/gap-map.md:143; architecture.md section 6.2): with
    --setting-sources "" the worktree's .claude/settings.json and its hooks stay out.

    The real claude runs in the launch jail with an empty egress allow list, so it reaches no
    provider and makes no model call; the fixture's credential is a fake. The worktree carries
    a settings file whose SessionStart and UserPromptSubmit hooks touch a marker in the
    worktree. Three launches: (a) as shipped, argv and settings untouched; (b)
    --setting-sources "" with the launch settings' disableAllHooks taken away, so the setting
    sources alone are tested; (c) the positive control, --setting-sources project without
    disableAllHooks, which must run the hook, or the absent marker of (a) and (b) would prove
    nothing. Each run must print the runtime's init event, so the session started offline.
    Opt-in: ALPACA_TEST_REAL_RUNTIME=1.

    The H15 answer, claude 2.1.285 (.alpaca/proofs/op-009/t-098.logs/h15-real-claude.log): a
    session starts offline and retries the API until the timeout; (a) and (b) print init and
    api_retry events and leave no marker; (c) prints hook_started and hook_response before init
    and the marker appears. So a headless launch with setting sources off runs no project
    hook."""
    monkeypatch.setattr(egress, "allowed_hosts", lambda runtime, environ, contract: ())
    sha = hashlib.sha256(open(REAL_CLAUDE, "rb").read()).hexdigest()
    marker = os.path.join(lab.worktree, "hook-marker")
    hooks = {"hooks": [{"type": "command", "command": "touch %s" % marker}]}
    fakes.Lab.write(os.path.join(lab.worktree, ".claude", "settings.json"),
                    json.dumps({"hooks": {"SessionStart": [hooks],
                                          "UserPromptSubmit": [hooks]}}))
    real_settings, real_argv = launcher._claude_settings, launcher.build_argv

    def run(n, *, sources, hooks_off):
        def settings(spec):
            out = real_settings(spec)
            if not hooks_off:
                out.pop("disableAllHooks")
            return out

        def argv(spec, home, binary):
            out = real_argv(spec, home, binary)
            if sources is not None:                  # None: the argv as build_argv made it
                out[out.index("--setting-sources") + 1] = sources
            return out
        monkeypatch.setattr(launcher, "_claude_settings", settings)
        monkeypatch.setattr(launcher, "build_argv", argv)
        if os.path.exists(marker):
            os.unlink(marker)
        spec = lab.implementer(key="t-140/implementer/t%d" % n, try_n=n, timeout_s=45,
                               session="%s-%s-implementer-t%d" % (RUN, ATTEMPT, n),
                               prompt="Say hi.", binary=REAL_CLAUDE, binary_sha256=sha)
        result = launcher.launch(spec, conn=lab.conn, root=lab.root)
        kinds = [(e.get("type"), e.get("subtype")) for e in _stream_events(result)]
        assert ("system", "init") in kinds, (result["outcome"], result["reason"],
                                             open(result["stream"]).read()[-2000:])
        end = lab.events(events.LAUNCH_END)[-1]["data"]
        print("H15 run %d sources=%r disableAllHooks=%s: outcome %s, egress refused %s, "
              "events %s, marker %s" % (n, sources, hooks_off, result["outcome"],
                                        end["egress_refused"], kinds[:4],
                                        os.path.exists(marker)))
        return os.path.exists(marker)

    assert run(1, sources=None, hooks_off=True) is False
    assert run(2, sources="", hooks_off=False) is False
    assert run(3, sources="project", hooks_off=False) is True


def test_every_launch_role_prompt_names_no_forbidden_runtime_word():
    # the prompts ship under alpaca/, which the proportionality scan reads
    folder = os.path.join(os.path.dirname(launcher.__file__), "prompts")
    for name in ("proposer.md", "implementer.md", "notes.md"):
        text = open(os.path.join(folder, name), encoding="ascii").read()
        assert text.isascii() and "\u2014" not in text
        assert texts.ENGINE_MARKER not in text


# ================================================================ the stdio relay (check round 3, B1)
#
# Any process in the jail could reopen a file the jail's processes hold open through
# /proc/<pid>/fd/<n>: with the stream and the prompt handed to bwrap as files, an agent rewrote
# its own stream and the evidence prompt (check-adversarial-r3.md, B1). The relay keeps both files
# on the host and gives the jail sockets, which a /proc fd link cannot reopen (ENXIO, errno 6).

_ENXIO = "errno=6"


def _tamper_report(result) -> dict:
    found = [e for e in _stream_events(result) if e.get("type") == "tamper"]
    assert found, "the fake printed no tamper report"
    rep = found[0]
    return dict(rep, child=json.loads(rep["child"]))


@JAILED
def test_the_agent_cannot_rewrite_its_stream_or_its_prompt_through_proc_fd_links(lab):
    """Shapes A1, A1b and A2 of check-adversarial-r3.md: a tool child pwrites over a line the
    runtime wrote through /proc/<ppid>/fd/1, opens that link for reading, and truncates
    /proc/<ppid>/fd/0; a shell writes the stream's head through the forwarder's
    /proc/2/fd/1. Every open fails, no jail process holds a file of out/, and the stream, its
    recorded sha256, the tool calls read back and the evidence prompt keep the true bytes."""
    spec = lab.implementer(prompt=fakes.directive(mode="tamper"))
    result = launcher.launch(spec, conn=lab.conn, root=lab.root)
    assert result["outcome"] == "ok", result["reason"]
    rep = _tamper_report(result)
    for name in ("overwrite_stream", "read_stream", "truncate_prompt"):
        assert rep["child"][name].startswith("ERR") and _ENXIO in rep["child"][name], rep
    assert rep["shell"].strip().endswith("ERR"), rep["shell"]
    home_out = lab.events(events.LAUNCH)[-1]["data"]["home"] + "/out/"
    assert not [k for k, v in rep["fds"].items() if home_out in v], rep["fds"]
    runtime = str(rep["pid"])
    assert rep["fds"][runtime + "/0"].startswith("socket:")
    assert rep["fds"][runtime + "/1"].startswith("socket:")
    raw = open(result["stream"], "rb").read()
    assert raw.startswith(b'{"type": "system", "subtype": "init"'), raw[:80]
    assert b"EVIDENCE-OF-TAMPER" in raw and b"forged-by-shell" not in raw
    end = lab.events(events.LAUNCH_END)[-1]["data"]
    assert end["stream_sha256"] == hashlib.sha256(raw).hexdigest()
    commands = [c["input"] for c in launcher.tool_calls(result["stream"], "claude")]
    assert "cat /EVIDENCE-OF-TAMPER" in commands and "ls -la" in commands
    start = lab.events(events.LAUNCH)[-1]["data"]
    kept = open(start["prompt"], "rb").read()
    assert kept == spec.prompt.encode("utf-8")
    assert hashlib.sha256(kept).hexdigest() == start["prompt_sha256"]


@JAILED
def test_without_the_relay_the_same_agent_rewrites_its_stream(lab, monkeypatch):
    """Negative control of the test above: with the stream and the prompt handed to bwrap as
    files (the relay taken out), the same child and shell succeed and the stream loses its
    line. The evidence prompt still holds the true bytes, because out/prompt.txt, which the
    child truncated, is never copied over the copy made before the run."""
    monkeypatch.setattr(launcher, "_relay_argv", lambda: [])
    spec = lab.implementer(prompt=fakes.directive(mode="tamper"))
    result = launcher.launch(spec, conn=lab.conn, root=lab.root)
    raw = open(result["stream"], "rb").read()
    assert raw.startswith(b'{"type":"forged-by-shell"}\n'), raw[:80]
    assert b"EVIDENCE-OF-TAMPER" not in raw
    rep = _tamper_report(result)
    assert rep["child"]["overwrite_stream"] == "ok"
    assert rep["child"]["truncate_prompt"] == "ok"
    assert rep["shell"].strip().endswith("ok")
    start = lab.events(events.LAUNCH)[-1]["data"]
    kept = open(start["prompt"], "rb").read()
    assert kept == spec.prompt.encode("utf-8")
    assert hashlib.sha256(kept).hexdigest() == start["prompt_sha256"]


_RELAY_CHILD = r"""
import json, os, subprocess, sys
prompt = sys.stdin.read()
rep = {"prompt": prompt, "fd0": os.readlink("/proc/self/fd/0"),
       "fd1": os.readlink("/proc/self/fd/1"), "fd2": os.readlink("/proc/self/fd/2")}
for name, flags in (("stdout", os.O_WRONLY), ("stdin", os.O_WRONLY | os.O_TRUNC)):
    fd = 1 if name == "stdout" else 0
    try:
        os.close(os.open("/proc/self/fd/%d" % fd, flags))
        rep[name] = "ok"
    except OSError as exc:
        rep[name] = "ERR errno=%d" % exc.errno
for i in range(3000):
    sys.stdout.write("line %05d %s\n" % (i, "x" * 60))
sys.stdout.write(json.dumps(rep) + "\n")
sys.stdout.flush()
sys.stderr.write("to stderr\n")
if len(sys.argv) > 1:
    subprocess.Popen(["/usr/bin/sleep", sys.argv[1]], start_new_session=True)
sys.exit(3)
"""


def _relay_run(tmp_path, *, relayed: bool, token: str | None = None):
    prompt = tmp_path / "prompt.txt"
    prompt.write_bytes(b"the prompt\n" * 20000)
    log = tmp_path / ("relayed.log" if relayed else "plain.log")
    argv = ["/usr/bin/python3", "-c", _RELAY_CHILD] + ([token] if token else [])
    full = (launcher._relay_argv() if relayed else []) + argv
    t0 = time.monotonic()
    proc = procs.run(full, cwd=str(tmp_path), env={"PATH": "/usr/bin:/bin"}, timeout_s=30,
                     log=str(log), stdin=str(prompt))
    lines = log.read_bytes().decode().splitlines()
    rep = json.loads([line for line in lines if line.startswith("{")][0])
    return proc, lines, rep, time.monotonic() - t0, prompt


def test_the_stdio_relay_hands_its_child_sockets_that_cannot_be_reopened(tmp_path):
    """No bwrap needed: the relay carries the whole prompt in and the whole stream out, in
    order, passes the exit code, and its child's fds 0, 1 and 2 are sockets that a
    /proc/self/fd reopen refuses. A child that leaves a process holding the stream socket
    ends the relay within the grace. Negative control: run directly, the same child reopens
    and truncates the files."""
    token = "691.%d" % (os.getpid() % 100000)
    try:
        proc, lines, rep, took, prompt = _relay_run(tmp_path, relayed=True, token=token)
        assert proc.rc == 3 and not proc.timed_out
        assert rep["prompt"] == "the prompt\n" * 20000
        for fd in ("fd0", "fd1", "fd2"):
            assert rep[fd].startswith("socket:"), rep
        assert rep["stdout"] == rep["stdin"] == "ERR errno=6"
        assert lines[:3000] == ["line %05d %s" % (i, "x" * 60) for i in range(3000)]
        assert lines[-1] == "to stderr"
        assert took < 10                          # the grace, not the sleeper, ended it
        assert prompt.read_bytes() == b"the prompt\n" * 20000
    finally:
        for pid in _alive_with(token):
            os.kill(pid, 9)
    proc, lines, rep, took, prompt = _relay_run(tmp_path, relayed=False)
    assert proc.rc == 3
    assert not rep["fd1"].startswith("socket:")
    assert rep["stdout"] == rep["stdin"] == "ok"
    assert prompt.read_bytes() == b""               # the direct child truncated the prompt


def test_the_evidence_copy_leaves_the_prompt_the_launch_recorded(tmp_path):
    src = tmp_path / "out"
    src.mkdir()
    (src / "prompt.txt").write_text("FORGED\n")
    (src / "stream.jsonl").write_text("{}\n")
    dst = tmp_path / "evidence"
    dst.mkdir()
    (dst / "prompt.txt").write_text("the true prompt\n")
    assert launcher._copy_out(str(src), str(dst), exclude=("prompt.txt",)) == []
    assert (dst / "prompt.txt").read_text() == "the true prompt\n"
    assert (dst / "stream.jsonl").read_text() == "{}\n"
    # negative: without the exclusion the out/ copy replaces it
    launcher._copy_out(str(src), str(dst))
    assert (dst / "prompt.txt").read_text() == "FORGED\n"


def test_a_changed_evidence_prompt_makes_the_launch_lost(lab, monkeypatch, tmp_path):
    """The collect step hashes the evidence prompt.txt again: when it no longer matches the
    prompt_sha256 of research-launch, the launch is lost with no research-launch-end."""
    _unjailed(monkeypatch, tmp_path)
    real_run = procs.run

    def run_then_change(*a, **kw):
        out = real_run(*a, **kw)
        with open(lab.events(events.LAUNCH)[-1]["data"]["prompt"], "a") as fh:
            fh.write("changed\n")
        return out

    monkeypatch.setattr(procs, "run", run_then_change)
    result = launcher.launch(lab.implementer(), conn=lab.conn, root=lab.root)
    assert result["outcome"] == "lost" and "prompt_sha256" in result["reason"]
    assert lab.events(events.LAUNCH_END) == []
    assert lab.events(events.INFRA)[-1]["data"]["class"] == "lost"
    # negative: the same launch with the prompt left alone ends normally
    monkeypatch.setattr(procs, "run", real_run)
    again = launcher.launch(lab.implementer(key="t-140/implementer/t2", try_n=2),
                            conn=lab.conn, root=lab.root)
    assert again["outcome"] == "ok", again["reason"]
    assert len(lab.events(events.LAUNCH_END)) == 1


# ================================================================ the relay dies with the runner (check round 1 resume, design B1)
#
# bwrap's --die-with-parent watches the relay, not the runner. The relay asks for SIGKILL when
# the runner ends (PR_SET_PDEATHSIG), so a SIGKILL of the runner still ends every agent jail
# (architecture.md section 10.2).

_RUNNER = r"""
import sys
sys.path.insert(0, sys.argv[1])
from alpaca.research import jail, launcher, procs
from alpaca.research.types import JailSpec
work, tag, relayed = sys.argv[2], sys.argv[3], sys.argv[4] == "1"
inner = (jail.argv(JailSpec(kind="gate", cwd=work, rw=(work,)))
         + ["--", "/bin/sleep", "300"])
full = (launcher._relay_argv() if relayed else []) + inner
procs.run(full, cwd=work, env={"PATH": "/usr/bin:/bin", procs.TAG_VAR: tag}, timeout_s=600,
          log=work + "/log")
"""


def _kill_runner_and_count(tmp_path, *, relayed: bool) -> tuple:
    repo = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(launcher.__file__))))
    tag = "t098-pdeath-%d-%d" % (os.getpid(), 1 if relayed else 0)
    work = tmp_path / ("relayed" if relayed else "direct")
    work.mkdir()
    runner = subprocess.Popen([sys.executable, "-c", _RUNNER, repo, str(work), tag,
                               "1" if relayed else "0"], start_new_session=True)
    try:
        deadline = time.monotonic() + 20
        while len(procs._tagged(tag)) < (3 if relayed else 2) and time.monotonic() < deadline:
            time.sleep(0.05)
        before = procs._tagged(tag)
        os.kill(runner.pid, signal.SIGKILL)
        runner.wait()
        deadline = time.monotonic() + 5
        while procs._tagged(tag) and time.monotonic() < deadline:
            time.sleep(0.05)
        return before, procs._tagged(tag)
    finally:
        for pid in procs._tagged(tag):
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass


@JAILED
def test_a_sigkill_of_the_runner_ends_the_relay_bwrap_and_the_agent(tmp_path):
    """The runner runs procs.run(relay + bwrap + sleep) the way _launch does, and is killed
    with SIGKILL. Within 5 s no process with the tag is left: the relay died with the runner and
    bwrap with the relay. Control: the direct bwrap argv of design 6.1 step 7 also leaves none."""
    before, after = _kill_runner_and_count(tmp_path, relayed=True)
    assert len(before) >= 3, before                   # the relay, bwrap and the jail's sleep
    assert after == [], after
    before, after = _kill_runner_and_count(tmp_path, relayed=False)
    assert before and after == [], (before, after)


def test_the_relay_refuses_to_run_under_a_parent_it_was_not_built_for(tmp_path):
    """A relay whose parent is not the pid _relay_argv named (the runner already died and the
    relay was reparented) exits 126 before it starts its child. Positive case: with the right
    parent it runs the child."""
    mark = tmp_path / "started"
    argv = launcher._relay_argv()
    child = ["/usr/bin/touch", str(mark)]
    wrong = list(argv)
    wrong[wrong.index("--") - 1] = "1"
    proc = subprocess.run(wrong + child, capture_output=True, timeout=30)
    assert proc.returncode == 126, proc
    assert b"cannot tie this relay to its runner" in proc.stderr
    assert not mark.exists()
    proc = subprocess.run(argv + child, capture_output=True, timeout=30)
    assert proc.returncode == 0, proc
    assert mark.exists()


# ================================================================ the redacting copy reads blocks (check round 1 resume, adversarial B2)


def _whole_pass(data: bytes, secrets: tuple) -> bytes:
    import re
    rx = re.compile(b"|".join(re.escape(v) for v in sorted(secrets, key=len, reverse=True)))
    return rx.sub(launcher._REDACTED, data)


def test_the_redacting_copy_replaces_a_value_that_spans_two_blocks(tmp_path):
    """Every block size from 1 to 40 bytes, with values that start in one block and end in the
    next, gives the bytes of one pass over the whole file."""
    import io
    import random
    secrets = (b"sk-long-secret-value", b"tok-12345678")
    rng = random.Random(98)
    pieces = []
    for _ in range(60):
        pieces.append(bytes(rng.choice(b"ab-sk\n") for _ in range(rng.randint(0, 9))))
        pieces.append(rng.choice(secrets + (b"sk-long-secret-valu", b"tok-1234567")))
    data = b"".join(pieces)
    want = _whole_pass(data, secrets)
    assert want.count(launcher._REDACTED) > 30
    for block in range(1, 41):
        dst = tmp_path / ("out-%d" % block)
        hit = launcher._copy_redacted(io.BytesIO(data), str(dst), secrets, block=block)
        assert hit and dst.read_bytes() == want, block
    # no secret: the bytes are copied as they are, and no hit is reported
    assert not launcher._copy_redacted(io.BytesIO(data), str(tmp_path / "none"), ())
    assert (tmp_path / "none").read_bytes() == data


def test_the_redacting_copy_never_reads_a_line_whole(tmp_path):
    """The copy asks its source for blocks of a fixed size and never for a line: a source that
    refuses readline and iteration still copies, and no read asks for more than one block."""
    import io

    class Blocks(io.BytesIO):
        sizes: list = []

        def readline(self, *a):
            raise AssertionError("read a line")

        def __iter__(self):
            raise AssertionError("iterated lines")

        def read(self, n=-1):
            self.sizes.append(n)
            return super().read(n)

    data = b"x" * (3 * 1024 * 1024 + 17) + b"anthropic-key" + b"y" * 99
    src = Blocks(data)
    dst = tmp_path / "copy"
    assert launcher._copy_redacted(src, str(dst), (b"anthropic-key",))
    assert all(0 < n <= launcher._REDACT_BLOCK for n in src.sizes), src.sizes
    assert dst.read_bytes() == data.replace(b"anthropic-key", launcher._REDACTED)


def _rss_kib(field: str) -> int:
    with open("/proc/self/status") as fh:
        for line in fh:
            if line.startswith(field + ":"):
                return int(line.split()[1])
    raise AssertionError("no %s in /proc/self/status" % field)


def _reset_peak() -> bool:
    try:
        with open("/proc/self/clear_refs", "w") as fh:
            fh.write("5")                     # reset VmHWM, the peak resident set size
        return True
    except OSError:
        return False


@JAILED
def test_one_huge_stream_line_does_not_grow_the_runners_memory_with_it(lab):
    """Adversarial B2: the agent writes one 512 MiB stdout line with no line break, the
    credential inside it (once across a 1 MiB block edge). The runner's peak resident set grows
    by less than 320 MiB, and the value is still replaced in the evidence stream. Before the
    block copy the redacting pass read the line whole and the peak grew by the line's size."""
    if not _reset_peak():
        pytest.skip("/proc/self/clear_refs cannot reset the peak resident set here")
    mb = 512
    edge = 1024 * 1024 - 5
    spec = lab.implementer(prompt=fakes.directive(mode="flood", mb=mb,
                                                  at=[100, edge, 300 * 1024 * 1024]))
    base = _rss_kib("VmRSS")
    _reset_peak()
    result = launcher.launch(spec, conn=lab.conn, root=lab.root)
    grew_mib = (_rss_kib("VmHWM") - base) / 1024
    print("t-098 flood: runner peak resident set grew by %.0f MiB for a %d MiB line"
          % (grew_mib, mb))
    assert result["outcome"] == "ok", result["reason"]
    assert grew_mib < 320, "the runner's peak grew by %.0f MiB" % grew_mib
    marks, tail = 0, b""
    with open(result["stream"], "rb") as fh:
        size = os.fstat(fh.fileno()).st_size
        while True:
            data = fh.read(1024 * 1024)
            if not data:
                break
            window = tail + data              # tail is shorter than the marker: no double count
            assert b"anthropic-key" not in window
            marks += window.count(launcher._REDACTED)
            tail = window[-(len(launcher._REDACTED) - 1):]
    assert size > mb * 1024 * 1024
    assert marks == 3
    end = lab.events(events.LAUNCH_END)[-1]["data"]
    assert "stream.jsonl" in end["redacted"]


# ================================================================ the stream on disk is capped (check round 3, adversarial m1)


@JAILED
@pytest.mark.parametrize("cap_mib,want", [(1, "lost"), (4, "ok")])
def test_the_relay_writes_at_most_its_cap_of_the_stream_and_past_it_the_launch_is_lost(
        lab, monkeypatch, cap_mib, want):
    """Check round 3, adversarial m1: the relay copied everything the jail printed, and the
    redacting pass and the evidence copy each made another full copy, so disk use had no bound.
    Now the relay writes at most _STREAM_DISK_MAX + 1 bytes and drains the rest, and a stream
    over the cap makes the launch lost. The agent prints 3 MiB. With a 1 MiB cap the evidence
    stream holds exactly cap + 1 bytes and the launch is lost; the control, a 4 MiB cap, keeps
    the whole stream and ends ok."""
    cap = cap_mib * 1024 * 1024
    monkeypatch.setattr(launcher, "_STREAM_DISK_MAX", cap)
    spec = lab.implementer(timeout_s=30, prompt=fakes.directive(mode="flood", mb=3))
    t0 = time.monotonic()
    result = launcher.launch(spec, conn=lab.conn, root=lab.root)
    assert time.monotonic() - t0 < 25              # drained, so the runtime never blocked
    size = os.path.getsize(result["stream"])
    assert result["outcome"] == want, result["reason"]
    if want == "lost":
        assert size == cap + 1
        assert result["infra"] == "lost" and "stream is over" in result["reason"]
        assert lab.events(events.LAUNCH_END) == []
    else:
        assert size > 3 * 1024 * 1024
        assert len(lab.events(events.LAUNCH_END)) == 1


# ================================================================ the write-path redaction is bounded (check round 4, adversarial B1)

KEY = b"anthropic-key"
LONG = b"tok-" + b"z" * 40
MIB = 1024 * 1024


def _allocated(folder: str) -> int:
    """The bytes the files under folder take on disk (st_blocks), not their sizes."""
    total = 0
    for base, _dirs, names in os.walk(folder):
        for name in names:
            total += os.lstat(os.path.join(base, name)).st_blocks * 512
    return total


def _sparse(path: str, size: int, at=(0,), value: bytes = KEY) -> None:
    with open(path, "wb") as fh:
        for offset in at:
            fh.seek(offset)
            fh.write(value)
        fh.truncate(size)


def test_the_write_path_redaction_masks_in_place_and_keeps_a_sparse_file_sparse(tmp_path,
                                                                                monkeypatch):
    """Check round 4, adversarial B1: _redact_tree rewrote a changed file whole, so a sparse
    file of 8 MiB that cost the agent no disk became 8 MiB written on the host. Now each value
    is overwritten in place with a mask of its length: the sparse file keeps its size and its
    holes, and a value past a hole is found too. A long value gets the marker text; a file the
    agent made read-only is still masked and keeps its mode; a file over _READ_MAX is listed as
    not checked. Negative: a file that did not change during the launch is left alone."""
    folder = tmp_path / "wt"
    folder.mkdir()
    old = folder / "OLD"
    old.write_bytes(b"from before: " + KEY + b"\n")
    time.sleep(0.05)
    since = time.time()
    time.sleep(0.05)
    sparse = str(folder / "sparse.bin")
    _sparse(sparse, 8 * MIB - 1, at=(0, 5 * MIB))
    before = os.stat(sparse).st_blocks * 512
    (folder / "small.txt").write_bytes(b"a=" + KEY + b" b=" + LONG + b"\n")
    locked = folder / "locked.txt"
    locked.write_bytes(b"x" + KEY)
    os.chmod(locked, 0o444)
    monkeypatch.setattr(launcher, "_READ_MAX", 2 * MIB)
    big = folder / "sub" / "big.bin"
    big.parent.mkdir()
    big.write_bytes(KEY + b"." * (3 * MIB))
    hits, missed, stopped = launcher._redact_tree(str(folder), (KEY, LONG), since)
    # the sparse file is larger than _READ_MAX, but its data is not: only data counts
    assert sorted(hits) == ["locked.txt", "small.txt", "sparse.bin"]
    assert missed == ["sub/big.bin"] and stopped is False
    st = os.stat(sparse)
    assert st.st_size == 8 * MIB - 1
    assert st.st_blocks * 512 <= before + 64 * 1024, (before, st.st_blocks * 512)
    with open(sparse, "rb") as fh:
        head = fh.read(len(KEY))
        fh.seek(5 * MIB)
        mid = fh.read(len(KEY))
    assert head == mid == launcher._mask(len(KEY)) == b"*" * len(KEY)
    assert (folder / "small.txt").read_bytes() == (
        b"a=" + b"*" * len(KEY) + b" b=" + launcher._REDACTED
        + b"*" * (len(LONG) - len(launcher._REDACTED)) + b"\n")
    assert locked.read_bytes() == b"x" + b"*" * len(KEY)
    assert os.stat(locked).st_mode & 0o777 == 0o444
    assert big.read_bytes().startswith(KEY)
    assert old.read_bytes() == b"from before: " + KEY + b"\n"


def test_the_write_path_redaction_reads_at_most_its_byte_budget_and_not_the_holes(tmp_path,
                                                                                monkeypatch):
    """The walk reads at most _REDACT_TREE_MAX bytes in all, and only the data of a file, never
    its holes: under a budget of 192 KiB, three dense files of 64 KiB with the key are masked,
    the next two are listed as not checked and keep it, and no read passes the budget. A sparse
    file of 8 MiB whose data is one block costs one block of it, so it is masked too.
    Negative: a budget the files fit in masks all of them."""
    monkeypatch.setattr(launcher, "_REDACT_TREE_MAX", 3 * 64 * 1024 + 8192)
    read = []
    real_pread = os.pread

    def counting(fd, n, offset):
        data = real_pread(fd, n, offset)
        read.append(len(data))
        return data
    monkeypatch.setattr(os, "pread", counting)
    folder = tmp_path / "wt"
    folder.mkdir()
    since = time.time() - 1
    _sparse(str(folder / "a-sparse.bin"), 8 * MIB - 1)
    for i in range(5):
        (folder / ("dense-%d.bin" % i)).write_bytes(KEY + b"." * (64 * 1024 - len(KEY)))
    hits, missed, stopped = launcher._redact_tree(str(folder), (KEY,), since)
    assert "a-sparse.bin" in hits
    dense_hits = [h for h in hits if h.startswith("dense-")]
    assert len(dense_hits) == 3 and len(missed) == 2 and not stopped
    for name in missed:
        assert (folder / name).read_bytes().startswith(KEY)
    assert sum(read) <= launcher._REDACT_TREE_MAX
    # negative: with room for every file, all of them are masked
    monkeypatch.setattr(launcher, "_REDACT_TREE_MAX", 16 * MIB)
    hits2, missed2, _ = launcher._redact_tree(str(folder), (KEY,), since)
    assert sorted(hits2) == sorted(missed) and missed2 == []


def test_the_write_path_redaction_counts_folders_and_names_what_it_cannot_list(tmp_path,
                                                                              monkeypatch):
    """The walk counts folders as well as files toward _SCAN_MAX and says when it stopped; a
    folder it cannot list is named in missed with a trailing slash, never passed over in
    silence. Negative: under the entry cap the walk ends by itself."""
    folder = tmp_path / "wt"
    folder.mkdir()
    since = time.time() - 1
    locked = folder / "locked"
    locked.mkdir()
    (locked / "k.txt").write_bytes(KEY)
    os.chmod(locked, 0)
    try:
        hits, missed, stopped = launcher._redact_tree(str(folder), (KEY,), since)
        assert hits == [] and missed == ["locked/"] and stopped is False
    finally:
        os.chmod(locked, 0o700)
    for i in range(10):
        (folder / ("d%02d" % i)).mkdir()
    monkeypatch.setattr(launcher, "_SCAN_MAX", 5)
    assert launcher._redact_tree(str(folder), (KEY,), since)[2] is True
    monkeypatch.setattr(launcher, "_SCAN_MAX", 50)
    hits, missed, stopped = launcher._redact_tree(str(folder), (KEY,), since)
    assert stopped is False and hits == ["locked/k.txt"]
    # a folder with more files than the cap: the walk masks files up to the cap, then stops
    # (the replayed attack A7b with 20000 files masked none when a folder was skipped whole)
    many = folder / "many"
    many.mkdir()
    for i in range(60):
        (many / ("k%02d" % i)).write_bytes(KEY)
    hits, missed, stopped = launcher._redact_tree(str(folder), (KEY,), since)
    assert stopped is True
    assert 30 <= sum(1 for h in hits if h.startswith("many/")) < 60


@JAILED
def test_sparse_files_with_the_key_under_a_write_path_cost_the_host_no_disk_or_time(
        lab, monkeypatch):
    """Check round 4, adversarial B1, in the jail (the checker's test_a7b): the agent writes the
    key at the start of 300 files, truncates each to 8 MiB - 1 and ends at once. Before, the
    collect step wrote 2.5 GB into the worktree and launch() returned after 14.55 s. Now every
    copy is masked in place: launch() returns within a few seconds, the worktree takes a few
    MiB on disk, every file keeps its size and no copy of the key. Controls: the small file
    with the key is masked too; a dense 5 MiB file with the key, over the 4 MiB budget this
    test sets, is not read, keeps the key and is the one path research-launch-end names in
    not_redacted."""
    monkeypatch.setattr(launcher, "_REDACT_TREE_MAX", 4 * MIB)
    # time the collect step itself, where the old copy of 2.5 GB was, apart from the agent's run
    collect_s = []
    real_collect = launcher._collect

    def timed_collect(*args, **kwargs):
        start = time.monotonic()
        try:
            return real_collect(*args, **kwargs)
        finally:
            collect_s.append(time.monotonic() - start)
    monkeypatch.setattr(launcher, "_collect", timed_collect)
    into = os.path.join(lab.worktree, "sparse")
    spec = lab.implementer(timeout_s=30, prompt=fakes.directive(
        mode="sparse_tree", into=into, files=300, bytes=8 * MIB - 1, dense=5 * MIB))
    t0 = time.monotonic()
    result = launcher.launch(spec, conn=lab.conn, root=lab.root)
    took = time.monotonic() - t0
    assert result["outcome"] == "ok", result["reason"]
    assert len(collect_s) == 1 and collect_s[0] < 4, "the collect step took %s s" % collect_s
    assert took < 30, "launch() took %.1f s" % took
    dense = os.path.join(into, "dense.bin")
    allocated = _allocated(into) - os.stat(dense).st_blocks * 512
    assert allocated < 32 * MIB, "the worktree takes %d bytes" % allocated
    names = sorted(n for n in os.listdir(into) if n.endswith(".bin") and n != "dense.bin")
    assert len(names) == 300
    for name in names:
        path = os.path.join(into, name)
        assert os.path.getsize(path) == 8 * MIB - 1
        with open(path, "rb") as fh:
            assert fh.read(len(KEY)) == b"*" * len(KEY)
    assert open(os.path.join(into, "small.txt"), "rb").read() == (
        b"stolen=" + b"*" * len(KEY) + b"\n")
    assert open(dense, "rb").read(len(KEY)) == KEY
    end = lab.events(events.LAUNCH_END)[-1]["data"]
    assert sum(1 for p in end["redacted"] if p.startswith(into + os.sep)) == 301
    assert end["not_redacted"] == {"count": 1, "paths": [dense], "walk_stopped": []}


# ================================================================ the egress log is capped (check round 4, adversarial m1)

@JAILED
@pytest.mark.parametrize("cap,want", [(2048, "lost"), (1024 * 1024, "ok")])
def test_an_egress_log_past_its_cap_makes_the_launch_lost(lab, monkeypatch, cap, want):
    """Check round 4, adversarial m1: the agent's refused requests grew the egress log with no
    bound. Now the proxy writes at most egress._LOG_MAX bytes of it and counts the rest, and a
    launch whose log reached the cap is lost. 40 refused CONNECTs with long host names under a
    2 KiB cap: the evidence log stays under the cap and the launch is lost. The control, a
    1 MiB cap, keeps every line and ends ok; each logged host holds at most 255 characters."""
    monkeypatch.setattr(egress, "_LOG_MAX", cap)
    targets = ["%s%02d.example:443" % ("h" * 300, i) for i in range(40)]
    spec = lab.implementer(prompt=fakes.directive(mode="probe", proxy=targets))
    result = launcher.launch(spec, conn=lab.conn, root=lab.root)
    log = os.path.join(os.path.dirname(result["stream"]), "egress.jsonl")
    assert os.path.getsize(log) <= cap
    lines = [json.loads(x) for x in open(log)]
    assert all(len(x["host"]) <= 255 for x in lines)
    assert result["outcome"] == want, result["reason"]
    if want == "lost":
        assert result["infra"] == "lost" and "egress log reached" in result["reason"]
        assert lab.events(events.LAUNCH_END) == []
    else:
        assert len(lines) == 40
        assert lab.events(events.LAUNCH_END)[-1]["data"]["egress_refused"] == 40


# ================================================================ usage takes the larger figure (check round 4, adversarial m2)

@JAILED
@pytest.mark.parametrize("stream_usage,want", [
    ({"input_tokens": 9000000, "output_tokens": 5}, 9000005),
    ({"input_tokens": 1, "output_tokens": 1}, 15)])
def test_a_launch_books_the_larger_of_the_transcript_and_the_stream_usage(lab, stream_usage,
                                                                          want):
    """Check round 4, adversarial m2: the transcript lives in the agent's config home, and it
    alone set the tokens a launch booked. Now the launch books the larger of what the
    transcript measures (15 tokens here) and what the runtime's result event states, and
    research-launch-end keeps both. A stream that states more than the transcript wins; the
    control, a stream that states less, leaves the transcript's figure."""
    spec = lab.implementer(prompt=fakes.directive(mode="ok", usage=stream_usage, cost=40.0,
                                                  measured=True))
    result = launcher.launch(spec, conn=lab.conn, root=lab.root)
    assert result["outcome"] == "ok", result["reason"]
    end = lab.events(events.LAUNCH_END)[-1]["data"]
    assert end["tokens"] == want and lab.booked[-1]["tokens"] == want
    assert end["usage_sources"]["transcript"]["tokens"] == 15
    assert end["usage_sources"]["stream"]["tokens"] == sum(stream_usage.values())
    assert end["usage_sources"]["stream"]["cost_usd"] == 40.0
    assert end["cost_usd"] >= 40.0


def test_tool_calls_keeps_the_folder_a_codex_command_runs_in():
    # op-009 t-095 check round 11 B3: the audit reads a command's relative paths in its workdir
    rollout = [
        {"timestamp": "T1", "type": "response_item", "payload": {
            "type": "function_call", "name": "exec_command", "call_id": "c1",
            "arguments": json.dumps({"cmd": "sed -i s/a/b/ t.py", "workdir": "/w/tests"})}},
        {"timestamp": "T2", "type": "response_item", "payload": {
            "type": "local_shell_call", "call_id": "c2",
            "action": {"type": "exec", "command": ["bash", "-lc", "echo x > t.py"],
                       "working_directory": "/w/src"}}},
        {"timestamp": "T3", "type": "response_item", "payload": {
            "type": "function_call", "name": "exec_command", "call_id": "c3",
            "arguments": json.dumps({"cmd": "cat x"})}},
    ]
    calls = launcher.tool_calls("\n".join(json.dumps(x) for x in rollout), "codex")
    assert [(c["kind"], c["input"], c.get("workdir")) for c in calls] == [
        ("shell", "sed -i s/a/b/ t.py", "/w/tests"),
        ("shell", "bash -lc 'echo x > t.py'", "/w/src"),
        ("shell", "cat x", None)]
