"""alpaca/hooks/temp_guard.py: a warning, never a block, for a pytest --basetemp that leaks."""
import json
import subprocess
import sys

import pytest

from alpaca.hooks import temp_guard
from alpaca.tests.conftest import REPO


def _bash(command):
    return {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": REPO}


@pytest.mark.parametrize("command", [
    "python -m pytest --basetemp /tmp/t173-full alpaca/tests",
    ".venv/bin/python -m pytest -q --basetemp=/tmp/run2 x.py",
    "cd a && pytest --basetemp /var/tmp/pt alpaca/tests/test_x.py",
    "FOO=1 bin/alpaca-python -m pytest --basetemp /tmp/zz",
    "rm -rf /tmp/x; pytest --basetemp /tmp/x",                      # removed before, not after
    "pytest --basetemp /tmp/x && rm -rf /tmp/x",                    # skipped when the suite fails
    "python -X dev -m pytest --basetemp /tmp/x",
    "bash -c 'cd a && pytest --basetemp /tmp/x'",
    "pytest --basetemp=.ptmp",                                      # relative: on the disk
    "cat <<'EOF' > x\ndata\nEOF\npytest --basetemp /tmp/x",          # after the heredoc ends
])
def test_fresh_or_disk_basetemp_gets_a_note(command):
    out = temp_guard.decide(_bash(command))
    ctx = out["hookSpecificOutput"]
    assert ctx["hookEventName"] == "PreToolUse" and "permissionDecision" not in ctx
    assert "Alpaca temp guard" in ctx["additionalContext"]


@pytest.mark.parametrize("command", [
    "python -m pytest alpaca/tests",
    "python -m pytest --basetemp /tmp/pytest-t-206 alpaca/tests; rm -rf /tmp/pytest-t-206",
    "trap 'rm -rf /tmp/op020-t-206-1' EXIT; pytest --basetemp=/tmp/op020-t-206-1 x.py",
    "pytest --basetemp /tmp/x || true; rm -rf /tmp/x",
    "bash -c 'pytest --basetemp /tmp/x; rm -rf /tmp/x'",
    "echo pytest --basetemp /tmp/x",
    "cat > note.txt <<'EOF'\n  .venv/bin/python -m pytest --basetemp=/tmp/x a.py\nEOF\nls",
    "python3 - <<EOF\npytest --basetemp /tmp/x\nEOF",
    "grep -- --basetemp docs/temp-and-scratch.md",
    "ls -la",
])
def test_rule_following_or_unrelated_commands_say_nothing(command):
    assert temp_guard.decide(_bash(command)) is None


def test_non_bash_says_nothing():
    assert temp_guard.decide({"tool_name": "Read", "tool_input": {"file_path": "x"}}) is None


def test_disk_path_note_names_the_slowdown():
    assert "100 times slower" in temp_guard.note_for("/var/tmp/claude-1/x/scratchpad/pt")
    assert "not under /tmp" in temp_guard.note_for(".ptmp")
    assert "not under /tmp" in temp_guard.note_for("$HOME/pt")


def test_main_prints_json_and_exits_zero():
    p = subprocess.run([sys.executable, "-m", "alpaca.hooks.temp_guard"],
                       input=json.dumps(_bash("pytest --basetemp /tmp/t1-a")),
                       capture_output=True, text=True, cwd=REPO, timeout=30)
    assert p.returncode == 0
    assert "additionalContext" in json.loads(p.stdout)["hookSpecificOutput"]
    p = subprocess.run([sys.executable, "-m", "alpaca.hooks.temp_guard"], input="not json",
                       capture_output=True, text=True, cwd=REPO, timeout=30)
    assert p.returncode == 0 and p.stdout == ""
