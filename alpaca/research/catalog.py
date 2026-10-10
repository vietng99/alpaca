"""The format 3 runbooks in the project, with their pack, contract hash and burn-in state
(operator-surface.md section 4.3.8).

`find(root)` lists the git-tracked `*.yaml` and `*.yml` files outside `.alpaca/`, `vendor/`,
`templates/` and `research-refs/` for which `runbook.research_block` returns a block, and for
each one runs the format 3 check and folds the record for the runs under the same contract hash.
It reads the record through a read-only connection and writes nothing: no event, no file.

Design: design/research-engine/operator-surface.md section 4.3.8, architecture.md section 3.9."""

from __future__ import annotations

import json
import os
import subprocess

from alpaca import db
from alpaca.research import events

#: folders a contract is never read from
_SKIP = (".alpaca/", "vendor/", "templates/", "research-refs/")
#: the engine default of research.controls.noop_rounds (runbook-format-3.md section 4)
_NOOP_ROUNDS = 2


def _tracked(root: str) -> list:
    try:
        got = subprocess.run(["git", "-C", root, "ls-files", "-z", "--", "*.yaml", "*.yml"],
                             capture_output=True, timeout=120)
    except (OSError, subprocess.SubprocessError):
        return []
    if got.returncode != 0:
        return []
    out = []
    for name in got.stdout.split(b"\0"):
        rel = name.decode("utf-8", "replace")
        if rel and not rel.startswith(_SKIP):
            out.append(rel)
    return sorted(out)


def _rows(conn, kind: str) -> list:
    out = []
    for row in conn.execute("SELECT id, ts, ref, data FROM events WHERE kind=? ORDER BY id",
                            (kind,)).fetchall():
        try:
            data = json.loads(row["data"] or "{}")
        except (TypeError, ValueError):
            data = {}
        out.append({"id": row["id"], "ts": row["ts"], "ref": row["ref"], "data": data})
    return out


def _accepted_attempt(conn, run) -> str | None:
    """The task id of the first attempt of `run` whose research-attempt verdict is accepted (not
    the baseline), the attempt state.fold reads as control_failed; None when there is none."""
    from alpaca.research import state
    for add in state._run_tasks(conn, run):
        research = add["data"].get("research") or {}
        if research.get("role") != "attempt" or research.get("baseline"):
            continue
        if state.attempt_verdict(conn, add["ref"]).get("verdict") == "accepted":
            return add["ref"]
    return None


def _burn_in(conn, contract_sha256, noop_rounds) -> dict:
    """controls.burn_in when t-102 has built it; until then (a temporary fallback) the same
    reading from the record: `failed` naming the control run and the accepted attempt when a
    control run under the hash accepted an attempt, `done` when a no-op control
    run under it finished noop_rounds rounds, else `needed` with the rounds done so far. The stub
    is detected the way open_run detects it (runner._stub). While it is a stub, open_run does not
    check the burn-in, so `needed` here does not refuse a start; it tells the skill to run the
    no-op control first (operator-surface section 3, step 3)."""
    from alpaca.research import controls, runner
    if not runner._stub(controls.burn_in):
        return controls.burn_in(conn, contract_sha256)
    from alpaca.research import state
    done = 0
    for ev in _rows(conn, events.RUN_START):
        d = ev["data"]
        if d.get("contract_sha256") != contract_sha256 or not d.get("control"):
            continue
        try:
            fold = state.fold(conn, ev["ref"])
        except KeyError:
            continue
        if fold.get("control_failed"):
            attempt = _accepted_attempt(conn, ev["ref"])
            return {"state": "failed", "run": ev["ref"], "attempt": attempt,
                    "reason": "control run %s accepted attempt %s" % (ev["ref"], attempt)}
        if d.get("control") != "noop":
            continue
        rounds = {r["data"].get("round") for r in _rows(conn, events.ROUND)
                  if r["ref"] == ev["ref"] and (r["data"].get("round") or 0) >= 1}
        done = max(done, len(rounds))
    if done >= noop_rounds:
        return {"state": "done", "rounds": done}
    return {"state": "needed", "rounds": done, "of": noop_rounds}


def _signoff(conn, runbook_id) -> int | None:
    """The newest interview-signoff whose slots include research-mode recorded before the
    runbook's first research-contract event, else None."""
    first = None
    for ev in _rows(conn, events.CONTRACT):
        if ev["data"].get("runbook_id") == runbook_id or ev["ref"] == runbook_id:
            first = ev["id"]
            break
    if first is None:
        return None
    found = None
    for ev in _rows(conn, "interview-signoff"):
        if ev["id"] < first and "research-mode" in (ev["data"].get("slots") or []):
            found = ev["id"]
    return found


def _reopen_needed(conn, runbook) -> bool:
    from alpaca.research import owner
    return owner._stopped_run(conn, runbook) is not None


def _entry(root, conn, rel) -> dict | None:
    import yaml
    from alpaca import runbook as rb
    from alpaca.research import contract, errors, holds
    path = os.path.join(root, rel)
    try:
        with open(path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        if rel.endswith((".yaml", ".yml")) and "research:" not in _head(path):
            return None
        return {"path": rel, "unreadable": True, "error": "%s: %s" % (type(exc).__name__, exc)}
    block = rb.research_block(data)
    if block is None:
        return None
    checked = rb.check(path)
    errs = checked.get("errors") or []
    check = "PASS" if checked.get("verdict") == "PASS" else (
        errs[0]["code"] if errs else checked.get("verdict"))
    metrics = [m for m in block.get("metrics") or [] if isinstance(m, dict)]
    stop = block.get("stop") if isinstance(block.get("stop"), dict) else {}
    controls = block.get("controls") if isinstance(block.get("controls"), dict) else {}
    out = {"path": rel, "id": data.get("id"), "mode": block.get("mode"),
           "metrics": [{"id": m.get("id"), "unit": m.get("unit"),
                        "direction": m.get("direction")} for m in metrics],
           "targets": {m.get("id"): m.get("target") for m in metrics},
           "k": stop.get("slots"), "n": stop.get("rounds"), "contract_sha256": None,
           "pack_sha256": None, "check": check, "burn_in": None, "runs": [], "holds": [],
           "reopen_needed": False, "signoff": None}
    try:
        view = contract.preview(root, rel, task=None)
    except errors.ResearchRefusal as exc:
        out["check"] = check if check != "PASS" else exc.reason
        view = None
    except Exception as exc:  # noqa: BLE001 - a contract that cannot be previewed is listed
        out["check"] = check if check != "PASS" else "CONTRACT-CHECK-BLOCKED"
        out["error"] = "%s: %s" % (type(exc).__name__, exc)
        view = None
    runbook = rel
    if view is not None:
        out["contract_sha256"] = view["contract_sha256"]
        out["pack_sha256"] = view["pack_sha256"]
        runbook = view["contract"]["runbook"]
        noop = controls.get("noop_rounds", _NOOP_ROUNDS)
        out["burn_in"] = _burn_in(conn, view["contract_sha256"],
                                  int(noop) if isinstance(noop, int) else _NOOP_ROUNDS)
    runs = []
    for ev in _rows(conn, events.RUN_START):
        d = ev["data"]
        if d.get("runbook") == runbook or (out["contract_sha256"] and
                                           d.get("contract_sha256") == out["contract_sha256"]):
            runs.append(ev["ref"])
    out["runs"] = runs
    open_holds = []
    for hold in holds.open_holds(conn):
        ref = str(hold.get("ref") or "")
        if ref.startswith("contract:%s#" % runbook) or ref in runs:
            open_holds.append({"hold": hold["id"], "ref": ref, "gate": hold.get("gate"),
                               "code": hold.get("code")})
    out["holds"] = open_holds
    out["reopen_needed"] = _reopen_needed(conn, runbook)
    out["signoff"] = _signoff(conn, data.get("id"))
    return out


def _head(path: str) -> str:
    try:
        with open(path, "rb") as fh:
            return fh.read(65536).decode("utf-8", "replace")
    except OSError:
        return ""


def find(root) -> list[dict]:
    """One dict per format 3 runbook, in path order: path, id, mode, metrics (id, unit,
    direction), targets (by metric id), k, n, contract_sha256 and pack_sha256 (from
    contract.preview with no task, the hash freeze gives the same runbook), check (PASS or the
    first error code), burn_in (controls.burn_in under that hash), runs (the run ids under the
    runbook path or the hash), holds (open holds on its contract refs and on those runs, the
    holds that make `start` exit 3), reopen_needed and signoff. A runbook that cannot be read is
    listed with `unreadable` true and `error`. Read-only."""
    root = os.path.realpath(root)
    conn = db.connect_readonly(root)
    try:
        out = []
        for rel in _tracked(root):
            got = _entry(root, conn, rel)
            if got is not None:
                out.append(got)
        return out
    finally:
        conn.close()
