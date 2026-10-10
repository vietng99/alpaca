"""op-008 t-068: the briefing at session start for Claude and Codex (requirements.md FR-19;
implementation-plan.md t-068: `alpaca/hooks/session_start.py` serves the briefing when a task or
question identifies the work; Codex reaches the same handler through `alpaca/operator.py`).

The Claude side runs the real hook as Claude Code does (`python -m alpaca.hooks.session_start`
with the payload on stdin); the Codex side runs `operator.run('start', ...)`, the handler behind
`bin/alpaca session start`. The dashboard line is Claude's only, so the two are compared on the
briefing block, which must be the same text, and the same text `service.briefing` builds.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from native_helpers import capture  # noqa: E402

from alpaca import cli, db as record, operator              # noqa: E402
from alpaca.wiki import briefing as briefings, service      # noqa: E402

REPO = Path(__file__).resolve().parents[3]
TITLE = "Restore the kestrel queue backup"
STATEMENT = "Restore the kestrel queue from the nightly backup and verify that it drains."


def claimed_task(project, claimant: str) -> str:
    """An op and a task added and claimed through the real CLI, every event captured. Returns the
    task id."""
    assert cli.main(["init"]) == 0
    assert cli.main(["op", "new", "kestrel recovery"]) == 0
    # Merge of op-008 into main (2026-10-06): on main every new op opens its question batch
    # (op-019 t-200), and a task of an op with a batch names its phase (TASK-PHASE-REQUIRED).
    assert cli.main(["task", "add", "--title", TITLE, "--phase", "build", "op-001", STATEMENT]) == 0
    assert cli.main(["task", "claim", "t-001", "--by", claimant, "--minutes", "60"]) == 0
    capture(project)
    return "t-001"


def claude_hook(project, session: str, source: str = "startup") -> str:
    """The SessionStart hook as Claude Code runs it; returns the additionalContext it emits."""
    payload = {"cwd": project, "session_id": session, "source": source,
               "hook_event_name": "SessionStart",
               "transcript_path": str(Path(project, "%s.jsonl" % session))}
    env = dict(os.environ, PYTHONPATH=str(REPO), ALPACA_NO_AUTOSERVE="1")
    env.pop("CLAUDE_ENV_FILE", None)
    out = subprocess.run([sys.executable, "-m", "alpaca.hooks.session_start"],
                         input=json.dumps(payload), capture_output=True, text=True, cwd=project,
                         env=env, timeout=120)
    assert out.returncode == 0 and out.stdout.strip(), out.stderr
    return json.loads(out.stdout)["hookSpecificOutput"]["additionalContext"]


def block(text: str, subject: str) -> str:
    """The briefing block of a session-start context: from the marker line to its gap line."""
    lines = text.splitlines()
    start = lines.index(briefings.MARKER % subject)
    end = next(n for n in range(start, len(lines)) if lines[n].startswith("Gaps: "))
    return "\n".join(lines[start:end + 1])


def tasks(project) -> list:
    conn = record.connect(project)
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM tasks ORDER BY id")]
    finally:
        conn.close()


def test_claude_hook_and_codex_operator_serve_the_same_briefing(project):
    task = claimed_task(project, "claude-s1")
    before = tasks(project)
    claude = claude_hook(project, "claude-s1")
    codex = operator.run("start", project, "codex-s2", operator="codex",
                         wiki_question="Resume %s: what should I know?" % task)["context"]
    expected = service.briefing(project, task=task,
                                max_chars=int(2500 * service.BRIEFING_SHARE)).text
    assert block(claude, "task:" + task) == block(codex, "task:" + task) == expected
    # a claim is the task's latest state event; no record rule turns it into a status fact
    assert TITLE in expected and "- state [source]: claim during session" in expected
    assert "no current status fact of %s" % task in expected
    # the dashboard line is Claude's only; the briefing is not
    assert "Live dashboard" not in codex
    # serving a briefing changes no task authority
    assert tasks(project) == before


def test_a_resumed_codex_session_gets_the_briefing_of_its_claimed_task(project):
    assert cli.main(["init"]) == 0
    first = operator.run("start", project, "codex-s3", operator="codex")["context"]
    assert "Rune2 briefing" not in first
    assert "wiki briefing --task" in first                  # the verb is named in the banner
    assert cli.main(["op", "new", "kestrel recovery"]) == 0
    # Merge of op-008 into main (2026-10-06): on main every new op opens its question batch
    # (op-019 t-200), and a task of an op with a batch names its phase (TASK-PHASE-REQUIRED).
    assert cli.main(["task", "add", "--title", TITLE, "--phase", "build", "op-001", STATEMENT]) == 0
    assert cli.main(["task", "claim", "t-001", "--by", "codex-s3", "--minutes", "60"]) == 0
    capture(project)
    resumed = operator.run("start", project, "codex-s3", operator="codex")
    text = resumed["context"]
    assert briefings.MARKER % "task:t-001" in text and TITLE in text
    # the Claude side on resume: its own claimed task, its own briefing
    assert cli.main(["task", "add", "--title", "Drain the owl ledger", "--phase", "build", "op-001",
                     "Drain it."]) == 0                      # --phase: merge note at claimed_task
    assert cli.main(["task", "claim", "t-002", "--by", "claude-s4", "--minutes", "60"]) == 0
    capture(project)
    claude_hook(project, "claude-s4")
    again = claude_hook(project, "claude-s4", source="resume")
    assert briefings.MARKER % "task:t-002" in again and "Drain the owl ledger" in again


def test_a_question_naming_an_op_gets_the_op_briefing(project):
    claimed_task(project, "claude-s5")
    text = operator.run("start", project, "codex-s6", operator="codex",
                        wiki_question="How is op-001 going?")["context"]
    assert briefings.MARKER % "op:op-001" in text
    assert "t-001 belongs to op-001 (no status fact yet)" in block(text, "op:op-001")
    assert cli.main(["task", "move", "t-001", "blocked", "--reason", "The volume is offline."]) == 0
    capture(project)
    text = operator.run("start", project, "codex-s6", operator="codex",
                        wiki_question="How is op-001 going?")["context"]
    assert "t-001 status is blocked" in block(text, "op:op-001")
    assert "The volume is offline." in block(text, "op:op-001")


def test_no_task_and_no_question_serves_no_briefing(project):
    claimed_task(project, "someone-else")
    text = claude_hook(project, "claude-s7")
    assert "Rune2 briefing" not in text and "WIKI EVIDENCE" in text


def test_a_codex_session_with_a_claimed_task_and_a_topical_question_gets_its_briefing(project):
    """Check round 1 (invariants B4): FR-19 serves the briefing when a task, resumed task or
    question identifies the work. A Codex session that claimed t-001 and starts with a question
    that names no work (`--wiki-question "kestrel queue backup"`, the shape docs/operators.md
    shows) gets its claimed task's briefing beside the answer, the same block it gets with no
    question and the same `service.briefing` builds."""
    task = claimed_task(project, "codex-s9")
    expected = service.briefing(project, task=task,
                                max_chars=int(2500 * service.BRIEFING_SHARE)).text
    plain = operator.run("start", project, "codex-s9", operator="codex")["context"]
    asked = operator.run("start", project, "codex-s9", operator="codex",
                         wiki_question="kestrel queue backup")["context"]
    assert block(plain, "task:" + task) == block(asked, "task:" + task) == expected
    # a question that names other work gets that work's briefing, not the claimed task's
    named = operator.run("start", project, "codex-s9", operator="codex",
                         wiki_question="How is op-001 going?")["context"]
    assert briefings.MARKER % "op:op-001" in named and briefings.MARKER % "task:t-001" not in named
    # a session with no claim and a topical question gets no briefing
    other = operator.run("start", project, "codex-s10", operator="codex",
                         wiki_question="kestrel queue backup")["context"]
    assert "Rune2 briefing" not in other


def test_a_dated_task_title_still_gets_its_briefing_and_a_pinned_question_says_why_not(project):
    """The claimed task's question leaves the title's dates and years out, so a dated title pins
    no time and the session still gets the current briefing (Claude and Codex alike); a topical
    question that pins a time gets the line that says the briefing is not served."""
    assert cli.main(["init"]) == 0
    assert cli.main(["op", "new", "kestrel recovery"]) == 0
    assert cli.main(["task", "add", "--title", "Restore the 2026-03-03 kestrel backup by 2026",
                     "--phase", "build", "op-001", STATEMENT]) == 0
    assert cli.main(["task", "add", "--title", "Drain the owl ledger", "--phase", "build", "op-001",
                     "Drain it."]) == 0                      # --phase: merge note at claimed_task
    assert cli.main(["task", "claim", "t-001", "--by", "claude-s11", "--minutes", "60"]) == 0
    assert cli.main(["task", "claim", "t-002", "--by", "codex-s12", "--minutes", "60"]) == 0
    capture(project)
    from alpaca.hooks import session_start
    assert session_start._claimed_question(
        {"id": "t-001", "title": "Restore the 2026-03-03 kestrel backup by 2026"}) == \
        "t-001 Restore the kestrel backup by"
    # a task id that reads as a year, with "by" in the title, would still pin: the id alone
    assert session_start._claimed_question({"id": "t-2026", "title": "Ship it by Friday"}) == "t-2026"
    claude = claude_hook(project, "claude-s11")
    assert block(claude, "task:t-001") == service.briefing(
        project, task="t-001", max_chars=int(2500 * service.BRIEFING_SHARE)).text
    assert "not served" not in claude
    pinned = operator.run("start", project, "codex-s12", operator="codex",
                          wiki_question="What happened to the queue on 2026-03-03?")["context"]
    assert "Rune2 briefing task:t-002 (evidence-only" not in pinned
    assert ("Briefing of task t-002 not served: the question pins a time (valid time "
            "2026-03-03T00:00:00+00:00)") in pinned
