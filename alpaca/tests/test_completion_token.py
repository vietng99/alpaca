"""M1.7: completion token, the write-ahead pair, and the resume-time BLOCK.

A non-idempotent verb writes a pre-act intent note (issue) into the record and
carries the token to its target, then writes a post-act result note (settle). A
process killed between the two leaves an unmatched token: an intent with no
result. At the next pickup `resume_check` finds it and reports a BLOCK naming the
token, the target and two candidate resolutions; the verb refuses to re-apply.

The negative path (an intent with no result) and the positive path (a settled
token, and a foreign-command intent whose tree witness proves it completed) are
both asserted here.
"""
import os
import subprocess
import sys
import time

import pytest

from alpaca import cli, db, pad, token


REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _env(project):
    e = dict(os.environ)
    e["CLAUDE_PROJECT_DIR"] = project
    e["PYTHONPATH"] = REPO
    return e


# ------------------------------------------------------------------ library level

def test_issue_then_settle_clears(project):
    cli.main(["init"])
    conn = db.connect(project)
    tok = token.issue(conn, "s1", "widget-A")
    assert tok.startswith("tok-")
    # Before settle: exactly one unmatched token naming the target.
    un = token.unmatched(conn)
    assert [u["token"] for u in un] == [tok]
    assert un[0]["target"] == "widget-A"
    verdict_word, _ = token.resume_check(conn, project)
    assert verdict_word == "BLOCK"
    # After settle: nothing unmatched, resume is clear.
    token.settle(conn, tok, "local:.alpaca/alpaca.db")
    assert token.unmatched(conn) == []
    assert token.resume_check(conn, project)[0] == "clear"


def test_unmatched_reports_token_target_and_two_resolutions(project):
    cli.main(["init"])
    conn = db.connect(project)
    tok = token.issue(conn, "s1", "widget-B")
    un = token.unmatched(conn)
    assert len(un) == 1
    row = un[0]
    assert row["token"] == tok and row["target"] == "widget-B"
    # Exactly two candidate resolutions, and both name the token.
    assert len(row["resolutions"]) == 2
    assert all(tok in r for r in row["resolutions"])
    verdict_word, detail = token.resume_check(conn, project)
    assert verdict_word == "BLOCK"
    assert detail["blocked"][0]["token"] == tok
    assert len(detail["blocked"][0]["resolutions"]) == 2


def test_void_resolves_an_unmatched_token(project):
    cli.main(["init"])
    conn = db.connect(project)
    tok = token.issue(conn, "s1", "widget-C")
    token.void(conn, tok, "operator confirmed the act never ran")
    assert token.unmatched(conn) == []
    assert token.resume_check(conn, project)[0] == "clear"


def test_foreign_witness_present_resolves_not_blocks(project):
    # A foreign command cannot hold a token: issue records a tree witness. When
    # that witness is present at resume the act is treated as resolved, not BLOCK.
    cli.main(["init"])
    conn = db.connect(project)
    tok = token.issue(conn, "s1", "foreign-D", witness={"path": "out/built.txt"})
    # Witness absent -> BLOCK.
    assert token.resume_check(conn, project)[0] == "BLOCK"
    # Witness present in the tree -> resolved (the act completed even with no settle).
    os.makedirs(os.path.join(project, "out"), exist_ok=True)
    with open(os.path.join(project, "out", "built.txt"), "w", encoding="utf-8") as fh:
        fh.write("done\n")
    verdict_word, detail = token.resume_check(conn, project)
    assert verdict_word == "resolved"
    assert detail["resolved"][0]["token"] == tok


# ------------------------------------------------------------------ mid-verb kill

def test_kill_mid_verb_leaves_block_and_refuses_reapply(project):
    cli.main(["init"])
    ready = os.path.join(project, "ready.tok")
    proc = subprocess.Popen(
        [sys.executable, "-m", "alpaca", "apply", "target-X"],
        cwd=project,
        env={**_env(project), "ALPACA_TOKEN_HANG": "30", "ALPACA_TOKEN_READY": ready},
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8")
    # Wait until the intent note is committed and the verb has entered the hang.
    deadline = time.time() + 10
    while not os.path.exists(ready) and time.time() < deadline:
        time.sleep(0.02)
    assert os.path.exists(ready), "verb never reached the pre-act hang"
    proc.kill()  # SIGKILL, between the intent note and the result note
    proc.wait(timeout=10)

    conn = db.connect(project)
    # The intent note survived; no result note; no mutation was applied.
    un = token.unmatched(conn)
    assert len(un) == 1 and un[0]["target"] == "target-X"
    killed_token = un[0]["token"]
    assert db.meta_get(conn, "apply:target-X") in (None, "0")
    assert [e for e in db.events(conn, kind="apply")] == []

    # The pad shows a BLOCK row naming the token and the target.
    text = pad.render(project)
    assert "BLOCK" in text
    assert killed_token in text and "target-X" in text

    # Re-running the verb refuses rather than re-applying.
    rc = cli.main(["apply", "target-X"])
    assert rc == cli.BLOCKED
    conn2 = db.connect(project)
    assert db.meta_get(conn2, "apply:target-X") in (None, "0")
    assert [e for e in db.events(conn2, kind="apply")] == []
    # Still exactly one unmatched token: the re-run did not issue a second one.
    assert len(token.unmatched(conn2)) == 1


def test_clean_verb_settles_and_leaves_no_block(project):
    cli.main(["init"])
    assert cli.main(["apply", "target-Y"]) == cli.PASS
    conn = db.connect(project)
    assert db.meta_get(conn, "apply:target-Y") == "1"
    assert token.unmatched(conn) == []
    assert token.resume_check(conn, project)[0] == "clear"
    assert "BLOCK" not in pad.render(project)


# ------------------------------------------------------------------ resume at pickup

def test_session_start_injects_block_into_pad(project):
    cli.main(["init"])
    conn = db.connect(project)
    tok = token.issue(conn, "s1", "widget-Z")
    p = subprocess.run(
        [sys.executable, "-m", "alpaca.hooks.session_start"],
        input='{"session_id": "sid-1", "cwd": "%s"}' % project,
        cwd=project, env=_env(project),
        capture_output=True, text=True, encoding="utf-8")
    assert p.returncode == 0, p.stderr
    import json
    ctx = json.loads(p.stdout)["hookSpecificOutput"]["additionalContext"]
    assert "BLOCK" in ctx and tok in ctx and "widget-Z" in ctx
