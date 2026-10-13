"""Shared helpers for the t-629 tests (held triggers, aar owe --event). Not a test module.

These tests drive the real verbs through cli.main on a throwaway project (the `project` fixture,
aarowedkit.two_ops: op-001 with t-001 and op-002 with t-002, both open) and read the record with SQL.

Names these tests assume of the code, and nothing else about naming:
  * `alpaca aar owe <op> --event <id>` attributes one trigger; --reason is optional with --event
    (task text: "'aar owe <op> --event <id>' attributes it to one op")
  * `aar owed` keeps an "unattributed" heading over the triggers no op is tied to (t-622 wording,
    task text "Keep 'aar owed' listing unattributed triggers")
  * the open ops an unattributed trigger blocks are named on that trigger's line or on lines
    indented under it, before the next "event:" line (task text "now also showing which open ops
    they block")
  * the op close BLOCKED line names each trigger as "event:N" (t-622) and, for an unattributed
    one, prints the `aar owe <op> --event N` remedy (task text (2))
"""
import hashlib
import re

from alpaca import cli, db, profile
from alpaca.release import receipts as rc
from alpaca.tests import aarowedkit as k

FLOW = "run-review-flow"
REASON = "the rollback step was skipped in the release"


class Fake(profile.Profile):
    def __init__(self, **hooks):
        self.__dict__.update(hooks)


def flow_profile(monkeypatch):
    """A profile that names one review-flow kind, as test_aar_owed_nogo_trigger.py installs it."""
    monkeypatch.setattr(profile, "load", lambda r: Fake(events=lambda: {"review_flow_kinds": [FLOW]}))


# ------------------------------------------------------------------ triggers
def nogo(conn, session, *, op=None, ref="j-1", at="2026-10-13T10:00:00+00:00", value="NO-GO"):
    """A review-flow decision event as reviewflow._save writes it: op column NULL unless given, the
    deciding session in both the event column and data.decision.session. Returns its id."""
    return db.append_event(conn, session=session, actor="agent", kind=FLOW, op=op, ref=ref,
                           data={"decision": {"value": value, "session": session, "decided_at": at,
                                              "rationale": "the run exited before step two"},
                                 "receipt": ref})["id"]


def real_nogo(project, session, rid="c" * 32):
    """A NO-GO through the real writer (reviewflow.assign then decide, session as main). Returns
    the decision event id."""
    import json
    import os
    from alpaca.runlog import quotes
    from alpaca.runlog import reviewflow as rf
    os.makedirs(os.path.join(project, "runs", "logs"), exist_ok=True)
    with open(os.path.join(project, "runs", rid + ".json"), "w") as fh:
        json.dump({"result": "FAIL"}, fh)
    f = rf.Flow(log=lambda ref: quotes.log_path(os.path.join(project, "runs", "logs"), ref),
                folder=lambda ref: os.path.join(project, "runs", "reviews", ref),
                subject=lambda ref: {"result": "FAIL"}, locks=os.path.join(project, "runs", "locks"),
                kind="run-review", mark_kind="run-review-mark", workflow_kind=FLOW, refs=lambda: [rid])
    rf.assign(project, f, rid, parent_session=session, reviewer_session=session + "-reviewer")
    got = rf.decide(project, f, rid, None, "NO-GO", "Recover the log before any retry", session=session)
    return got["event"]


def fail(project, session):
    """A release check FAIL receipt through the real writer (receipts.record_receipt, op NULL)."""
    rc.record_receipt(project, session, rc.CheckReceipt(build={}, gates=[], status="FAIL"))
    return k.last_id(db.connect(project), "release-check")


def raw_fail(conn, *, session="cli", ref="attempt-x", sha=None, clock=None):
    """A release-check FAIL event written straight to the record, for a chosen ts (clock)."""
    return db.append_event(conn, session=session, actor="alpaca-release", kind="release-check", ref=ref,
                           data={"receipt": ".alpaca/releases/receipts/%s-check.json" % ref,
                                 "sha256": sha or hashlib.sha256(ref.encode()).hexdigest(), "status": "FAIL",
                                 "summary": "Release check: FAIL"}, clock=clock)["id"]


# ------------------------------------------------------------------ reading `aar owed`
def has(text, ev):
    return re.search(r"event:%d\b" % ev, text) is not None


def entry(out, ev):
    """The text of trigger `ev` in an `aar owed` listing: its line plus the lines indented under it,
    up to the next line that names an event."""
    lines = out.splitlines()
    for i, line in enumerate(lines):
        if has(line, ev):
            depth = len(line) - len(line.lstrip())
            got = [line]
            for nxt in lines[i + 1:]:
                if not nxt.strip() or "event:" in nxt or len(nxt) - len(nxt.lstrip()) <= depth:
                    break
                got.append(nxt)
            return "\n".join(got)
    return None


def loose_part(out):
    """The `aar owed` text from the "unattributed" heading on ("" when there is none)."""
    low = out.lower()
    i = low.find("unattributed")
    return out[i:] if i >= 0 else ""


def is_loose(capsys, ev):
    return has(loose_part(k.owed(capsys)), ev)


def blocks_named(capsys, ev):
    """The op ids named on the entry of loose trigger `ev` in plain `aar owed`."""
    text = entry(loose_part(k.owed(capsys)), ev) or ""
    return set(re.findall(r"\bop-\d+\b", text))


# ------------------------------------------------------------------ verbs
def owe_event(capsys, op, ev, *extra):
    try:
        return k.run(capsys, "aar", "owe", op, "--event", ev, *extra)
    except SystemExit as exc:          # argparse refusing a value
        return exc.code, ""


def close_other(project, conn, capsys):
    """Finish t-002 and try to close op-002: (code, out)."""
    row = conn.execute("SELECT status FROM tasks WHERE id='t-002'").fetchone()
    if row["status"] != "done":
        k.finish(project, conn, "t-002")
    return k.close(capsys, k.OTHER)


def new_op(project, title="a later op", task_title="later work"):
    """Open one more op with one task through the verbs; returns (op id, task id)."""
    assert cli.main(["op", "new", title, "--done-when", "nothing to see"]) == 0
    conn = db.connect(project)
    op = conn.execute("SELECT id FROM ops ORDER BY id DESC LIMIT 1").fetchone()[0]
    assert cli.main(["task", "add", "--title", task_title, op, task_title + " here", "--phase", "build"]) == 0
    tid = conn.execute("SELECT id FROM tasks WHERE op=? ORDER BY id DESC LIMIT 1", (op,)).fetchone()[0]
    return op, tid
