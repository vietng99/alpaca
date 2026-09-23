"""M1.1: banned-list injection on the re-arm cadence.

A re-arm point (first prompt, every Nth, after compaction, danger band) injects
the full rule text; any other prompt injects only a short reminder line that is
under 40 tokens. Both paths are proved by counting characters / marker text.
"""
import json, os, subprocess, sys
from alpaca import cli, db

ENV = lambda project: {**os.environ, "CLAUDE_PROJECT_DIR": project,
                       "PYTHONPATH": os.path.dirname(os.path.dirname(os.path.dirname(__file__)))}

FULL_MARK = "do not use these words"
REMINDER_MARK = "avoid the banned words"


def hook(payload, project):
    return subprocess.run([sys.executable, "-m", "alpaca.hooks.user_prompt"], input=json.dumps(payload),
                          capture_output=True, text=True, encoding="utf-8", cwd=project, env=ENV(project))


def ctx(out):
    return json.loads(out)["hookSpecificOutput"]["additionalContext"] if out.strip() else ""


def write_banned(project, words):
    with open(os.path.join(project, "style", "banned.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(words) + "\n")


def test_full_on_first_reminder_between_full_on_nth(project):
    cli.main(["init"])
    write_banned(project, ["synergy", "leverage", "circle back"])
    sid = "cadence-1"
    outs = [ctx(hook({"session_id": sid, "prompt": "p%d" % i}, project).stdout) for i in range(1, 6)]
    # prompt 1 is a re-arm point (first prompt): full list.
    assert FULL_MARK in outs[0] and "synergy" in outs[0]
    # prompts 2..4 are steady state: reminder only, no full list, no word bodies.
    for mid in outs[1:4]:
        assert REMINDER_MARK in mid
        assert FULL_MARK not in mid and "synergy" not in mid
    # prompt 5 is the Nth re-arm point: full list again.
    assert FULL_MARK in outs[4] and "synergy" in outs[4]


def test_compaction_forces_full(project):
    cli.main(["init"])
    write_banned(project, ["synergy"])
    sid = "cadence-2"
    hook({"session_id": sid, "prompt": "p1"}, project)                       # first (full)
    mid = ctx(hook({"session_id": sid, "prompt": "p2"}, project).stdout)     # steady-state reminder
    assert REMINDER_MARK in mid and FULL_MARK not in mid
    comp = ctx(hook({"session_id": sid, "prompt": "p3", "compacted": True}, project).stdout)
    assert FULL_MARK in comp and "synergy" in comp


def test_danger_band_forces_full(project):
    cli.main(["init"])
    write_banned(project, ["synergy"])
    sid = "cadence-3"
    hook({"session_id": sid, "prompt": "p1"}, project)                       # first (full)
    danger = ctx(hook({"session_id": sid, "prompt": "p2", "context_percent": 95}, project).stdout)
    assert FULL_MARK in danger and "synergy" in danger


def test_reminder_when_preset_off_but_banned_txt_nonempty(project):
    cli.main(["init"])
    write_banned(project, ["synergy"])   # no preset configured in project.yaml
    sid = "cadence-4"
    hook({"session_id": sid, "prompt": "p1"}, project)                       # first (full)
    mid = ctx(hook({"session_id": sid, "prompt": "p2"}, project).stdout)     # reminder
    assert REMINDER_MARK in mid and "synergy" not in mid


def test_steady_state_reminder_under_40_tokens(project):
    cli.main(["init"])
    write_banned(project, ["synergy", "leverage", "circle back", "at the end of the day", "moving forward"])
    sid = "cadence-5"
    hook({"session_id": sid, "prompt": "p1"}, project)                       # first (full)
    mid = ctx(hook({"session_id": sid, "prompt": "p2"}, project).stdout)     # reminder
    assert REMINDER_MARK in mid
    proxy_tokens = (len(mid) + 3) // 4   # ~4 characters per token
    assert proxy_tokens <= 40, "steady-state reminder is %d proxy tokens" % proxy_tokens


def test_style_rearm_event_recorded_only_on_rearm(project):
    cli.main(["init"])
    write_banned(project, ["synergy"])
    sid = "cadence-6"
    hook({"session_id": sid, "prompt": "p1"}, project)   # re-arm -> one event
    hook({"session_id": sid, "prompt": "p2"}, project)   # steady state -> no event
    evs = db.events(db.connect(project), kind="style-rearm", session=sid)
    assert len(evs) == 1 and evs[0]["data"]["prompt"] == 1
