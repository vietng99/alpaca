"""Read-only work provenance shared by the hub and generated checklist.

The projection never promotes an assignment to completion or a historical seal to
fresh verification. Report readers are supplied by the caller's document policy.
"""
from datetime import datetime
import json
import re

from alpaca import util


_TASK_KINDS = ("task-add", "task-move", "task-claim", "claim", "claim-release", "lease-expired")
_SECTIONS = {"what i did": "what", "what was done": "what", "how i did it": "how",
             "how it was done": "how", "result": "result", "results": "result"}


def number(ident):
    match = re.fullmatch(r"(?:t|AC)-(\d+)", str(ident))
    return int(match.group(1)) if match else None


def _rows(conn, query):
    return [dict(row) for row in conn.execute(query)] if conn is not None else []


def _data(value):
    try:
        data = json.loads(value)
        return data if isinstance(data, dict) else {}
    except (TypeError, ValueError):
        return {}


def completion(event):
    return {"at": event["ts"], "session": event["session"], "actor": event["actor"],
            "event_id": event["id"]} if event else None


def completion_fields(event):
    event = event or {}
    return {"completed_at": event.get("ts"), "completed_session": event.get("session"),
            "completed_actor": event.get("actor"), "completion_event_id": event.get("id")}


def _assigned_session(row, events, now):
    """Resolve an assignment's session without confusing the worker with its session."""
    worker = row.get("claimant")
    if row.get("status") != "doing" or not isinstance(worker, str) or not worker:
        return None
    for event in reversed(events):
        kind, data = event["kind"], event["data"]
        if kind in ("claim", "task-claim"):
            # A newer claim for somebody else must not revive an older match.
            if data.get("worker") != worker:
                return None
            # The task row carries renewals; the original claim may have an old lease.
            lease = row.get("lease_until") or data.get("lease_until")
            if lease is not None:
                try:
                    if datetime.fromisoformat(lease) <= datetime.fromisoformat(now):
                        return None
                except (TypeError, ValueError):
                    return None
            return event.get("session") or None
        if kind in ("claim-release", "lease-expired", "task-add"):
            return None
        if kind == "task-move" and data.get("to") and data["to"] != "doing":
            return None
    return None


def tasks(conn, report_reader=None):
    """Project task rows and their lifecycle events without modifying the record."""
    ops = {row["id"]: row for row in _rows(conn, "SELECT * FROM ops")}
    histories, seals = {}, {}
    kinds = ",".join("'%s'" % kind for kind in (*_TASK_KINDS, "proof-report"))
    for event in _rows(conn, "SELECT * FROM events WHERE kind IN (%s) ORDER BY id" % kinds):
        event["data"] = _data(event["data"])
        if event["kind"] == "proof-report":
            seals[event["ref"]] = event
        else:
            histories.setdefault(event["ref"], []).append(event)
    from alpaca import taskcontract
    try:
        contracts = taskcontract.latest(conn)
    except Exception:              # an older record without the event kind still lists its tasks
        contracts = {}
    items, now = [], util.now_iso()
    for row in _rows(conn, "SELECT * FROM tasks ORDER BY CASE status WHEN 'doing' THEN 0 WHEN 'blocked' THEN 1 WHEN 'open' THEN 2 ELSE 3 END,id"):
        events = histories.get(row["id"], [])
        additions = [e for e in events if e["kind"] == "task-add"]
        moves = [e for e in events if e["kind"] == "task-move" and e["data"].get("to")]
        completions = [e for e in moves if e["data"]["to"] == "done"]
        latest = next((e for e in reversed(events) if e["kind"] != "task-move" or e["data"].get("to")), None)
        finished = (latest if row["status"] == "done" and latest and latest["kind"] == "task-move"
                    and latest["data"].get("to") == "done" else None)
        history = [{"event_id": e["id"], "at": e["ts"], "session": e["session"], "actor": e["actor"],
                    "kind": e["kind"], "from": e["data"].get("from"), "to": e["data"].get("to"),
                    "reason": e["data"].get("reason"), "proof": e["data"].get("proof")} for e in events]
        op = ops.get(row["op"], {})
        items.append({"id": row["id"], "number": number(row["id"]), "title": row["title"] or row["statement"],
                      "description": row["statement"] if row["title"] else None,
                      "status": row["status"], "op": row["op"], "claimant": row["claimant"],
                      "assigned_session": _assigned_session(row, events, now),
                      "proof": row["proof"], "reason": row["why"],
                      "source": row["where_"] if isinstance(row["where_"], str) else None, "ref": row["id"],
                      "purpose": row["why"] or op.get("done_when") or op.get("intent"),
                      "created_at": row["created"] or (additions[0]["ts"] if additions else None),
                      "updated_at": row["updated"] or (events[-1]["ts"] if events else None),
                      **completion_fields(finished),
                      "last_completion": completion(completions[-1]) if completions else None,
                      "history": history, "contract": contracts.get(row["id"]),
                      "report": report_reader(row["proof"], seals.get(row["id"])) if report_reader else None})
    return items


def report_sections(text, path, seal=None, read_truncated=False):
    """Extract bounded authored sections, ignoring comments and headings inside code."""
    text = re.sub(r"<!--.*?(?:-->|\Z)", "", text, flags=re.S)
    sections = {key: [] for key in ("what", "how", "result")}
    active, fence = None, None
    for line in text.splitlines():
        marker = re.match(r"^\s{0,3}(`{3,}|~{3,})(.*)$", line)
        if fence:
            if active:
                sections[active].append(line)
            if marker and marker[1][0] == fence[0] and len(marker[1]) >= len(fence) and not marker[2].strip():
                fence = None
            continue
        if marker:
            fence = marker[1]
            if active:
                sections[active].append(line)
            continue
        heading = re.match(r"^#{1,2}\s+(.+?)\s*#*\s*$", line)
        if heading:
            active = _SECTIONS.get(heading[1].casefold()) if line.startswith("## ") else None
        elif active:
            sections[active].append(line)
    result, truncated = {}, read_truncated
    for key, lines in sections.items():
        value = "\n".join(lines).strip()
        # A scaffold placeholder is explicitly missing authored work.
        if value.startswith("TODO(agent):"):
            value = ""
        result[key] = value[:1200] or None
        truncated = truncated or len(value) > 1200
    return {"path": path, **result, "truncated": truncated, "sealed": seal is not None,
            "sealed_at": seal.get("ts") if seal else None,
            "sealed_session": seal.get("session") if seal else None,
            "verification": "not_checked", "verified_at": None}


def verdict_index(conn):
    """Latest actual verdict per bound row; an event's payload session is not its actor."""
    result = {}
    for event in _rows(conn, "SELECT * FROM events WHERE kind='verdict' ORDER BY id"):
        event["data"] = _data(event["data"])
        binds = event["data"].get("binds")
        if isinstance(binds, dict) and isinstance(binds.get("row_id"), str):
            result[binds["row_id"]] = event
    return result


def acceptance_provenance(ident, observation, status, verdicts):
    """Attach provenance only when the recorded observation matches its latest verdict."""
    ref = observation.get("row_id")
    event = verdicts.get(ref)
    if event and (event["data"].get("verdict_name") != observation.get("verdict")
                  or event["data"].get("reason") != observation.get("reason")):
        event = None
    recorded_done = event if event and observation.get("verdict") == "PASS" else None
    return {"number": number(ident), "ref": ref, "created_at": None,
            "updated_at": event["ts"] if event else None,
            **completion_fields(recorded_done if status == "PASS" else None),
            "recorded_completed_at": recorded_done["ts"] if recorded_done else None,
            "recorded_completed_session": recorded_done["session"] if recorded_done else None}
