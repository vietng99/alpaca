"""A08 (cause C5): `_cmd_aar` ends in `else: seal(...)`, so any verb that reaches that chain and is
not new or check SEALS the report, and a seal clears every owed trigger. A new verb dispatched after
the chain, or one a typo misses, turns `aar owe` / `aar owed` / `aar waive` into a silent seal.

Every new verb, success and refusal, leaves the aar-seal count unchanged; `aar owed` writes no event
at all; the bare `aar` usage line names the new verbs."""
import pytest

from alpaca import cli
from alpaca.tests import aarowedkit as k

REASON = "the rollback step was skipped in the release"


def safe_run(capsys, *argv):
    try:
        return k.run(capsys, *argv)
    except SystemExit as exc:
        return exc.code, ""


@pytest.fixture
def drafted(project, capsys):
    """A filled, check-clean draft with an accepted review: the one state in which a stray seal()
    would succeed."""
    conn = k.two_ops(project)
    k.finish(project, conn, "t-001")
    code, out = k.run(capsys, "aar", "new", k.OP)
    assert code == cli.PASS, out
    k.write(k.report(project, k.OP), k.fill(k.read(k.report(project, k.OP)), k.op_open_event(conn, k.OP)))
    assert k.run(capsys, "aar", "check", k.OP)[0] == cli.PASS
    k.review(project)
    return conn


@pytest.mark.parametrize("argv", [
    ("aar", "owe", k.OP, "--reason", REASON),
    ("aar", "owe", k.OP),
    ("aar", "owe", k.OP, "--reason", ""),
    ("aar", "owe", "op-404", "--reason", REASON),
    ("aar", "owed"),
    ("aar", "owed", k.OP),
    ("aar", "owed", "op-404"),
    ("aar", "waive", k.OP, "--by", "owner", "--quote", "skip the report this time"),
    ("aar", "waive", k.OP, "--by", "agent", "--quote", "skip the report this time"),
    ("aar", "waive", k.OP),
])
def test_no_new_verb_writes_a_seal(drafted, capsys, argv):
    before = k.count(drafted, "aar-seal")
    safe_run(capsys, *argv)
    assert k.count(drafted, "aar-seal") == before, argv


def test_owed_is_read_only(drafted, capsys):
    k.run(capsys, "aar", "owe", k.OP, "--reason", REASON)
    before = k.count(drafted)
    k.owed(capsys)
    k.owed(capsys, k.OP)
    assert k.count(drafted) == before


def test_the_usage_line_names_the_new_verbs(project, capsys):
    k.two_ops(project)
    code, out = k.run(capsys, "aar")
    assert code == cli.USAGE
    for verb in ("owe", "owed", "waive"):
        assert verb in out, out
