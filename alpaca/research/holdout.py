"""Holdout checks on the final champion only, each in a holdout jail on its own tree; results never
reach a prompt (FR-045).

A holdout check is a stage of the runbook that research.holdout.stages names. It runs once the loop
has ended, on the champion commit and on nothing else, so a score on held-out data cannot steer an
attempt: no attempt, no proposer and no verifier ever sees it. Three rules keep that true, and
each has a test in alpaca/tests/test_research_controls.py:

- Held-out data is not in any tree an attempt reaches. The clone strips every file a
  research.holdout.paths rule hits (isolation.build_clone), and the freeze copies those files, from
  the base commit, into contract/holdout/, which no agent jail and no gate jail mounts. Only the
  holdout jail of gates.run_holdout_stage overlays them, read-only, on a tree of its own.
- The tree is the check's own. It is cut for the check at paths.holdout_dir(root, run, stage)/tree
  from the run's clone at the champion commit, never from a round's champion checkout (which a
  proposer reads), and removed after the check, so a held-out file never outlives the check in a
  tree an agent could read.
- Nothing here is read by a prompt. The result is one research-holdout event on the run and the
  research-gate event of the stage; the prompt builders read the run's notes, the pack's lessons
  and the repair texts only, and the holdout check writes no note and no message.

Each check is judged as a gate stage is (gates.run_holdout_stage): the host reads only the by-bytes
judged copy of the check's evidence folder, which a refused copy turns into BLOCKED. One
research-holdout event is written per check, keyed on (run, champion, stage): a finalize after an
owner extension that moved the champion runs every check again, and an unchanged champion reuses
the PASS or FAIL it already recorded.

Design: design/research-engine/architecture.md sections 3.9, 4.8, 5.2 and 8.3 (FR-045, EC-019).
"""

from __future__ import annotations

import json
import os
import re

from alpaca.gates import verdict as vc
from alpaca.research import events, gates, isolation, paths
from alpaca.research.errors import IsolationError

_COMMIT = re.compile(r"[0-9a-f]{40}")
_REUSABLE = (vc.PASS, vc.FAIL)


def frozen_paths(root: str, run: str) -> list:
    """(frozen copy, repository path) of every held-out file under contract/holdout/, sorted, no
    link followed. These are the files a holdout check's tree is overlaid with."""
    base = os.path.join(paths.contract_dir(root, run), "holdout")
    out = []
    if not os.path.isdir(base) or os.path.islink(base):
        return out
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = sorted(d for d in dirnames if not os.path.islink(os.path.join(dirpath, d)))
        for name in sorted(filenames):
            src = os.path.join(dirpath, name)
            if os.path.islink(src) or not os.path.isfile(src):
                continue
            out.append((src, os.path.relpath(src, base).replace(os.sep, "/")))
    return out


def recorded(conn, run: str) -> list:
    """The research-holdout events of the run, oldest first, each {id, ref, data}."""
    out = []
    for row in conn.execute("SELECT id, ref, data FROM events WHERE kind=? AND ref=? ORDER BY id",
                            (events.HOLDOUT, run)).fetchall():
        try:
            data = json.loads(row["data"] or "{}")
        except (TypeError, ValueError):
            data = {}
        out.append({"id": row["id"], "ref": row["ref"], "data": data if isinstance(data, dict)
                    else {}})
    return out


def _reusable(conn, run: str, champion: str, stage: str) -> dict | None:
    """The newest research-holdout event for (run, champion, stage) that holds a real result, a
    PASS or a FAIL that was not cut short by a stop; None when there is none."""
    for ev in reversed(recorded(conn, run)):
        data = ev["data"]
        if data.get("champion") != champion or data.get("stage") != stage:
            continue
        if data.get("stopped"):
            continue
        if data.get("exit") in _REUSABLE:
            return ev
    return None


def _gate_data(conn, event_id) -> dict:
    row = conn.execute("SELECT data FROM events WHERE id=?", (event_id,)).fetchone()
    try:
        data = json.loads(row["data"] or "{}") if row else {}
    except (TypeError, ValueError):
        data = {}
    return data if isinstance(data, dict) else {}


def _summary(ev: dict, *, reused: bool) -> dict:
    data = ev["data"]
    return {"stage": data.get("stage"), "champion": data.get("champion"),
            "verdict": data.get("verdict"), "exit": data.get("exit"), "event": ev["id"],
            "reused": reused, "log": data.get("log"), "reason": data.get("reason")}


def _record(conn, run: str, *, op, session: str, code: int, champion: str, stage: str,
            tree: str, gate: dict, gate_event, stopped: bool, reason: str | None) -> dict:
    data = {"champion": champion, "stage": stage, "tree": tree, "log": gate.get("log"),
            "sha256": gate.get("log_sha256"), "gate_event": gate_event,
            "judged": gate.get("judged"), "reason": reason or gate.get("reason"),
            "stopped": bool(stopped)}
    got = events.emit(conn, events.HOLDOUT, run=run, ref=run, op=op, session=session, code=code,
                      data=data)
    got["data"] = json.loads(got["data"]) if isinstance(got.get("data"), str) else got["data"]
    return {"id": got["id"], "ref": run, "data": got["data"]}


def _run_op(conn, run: str):
    row = conn.execute("SELECT op FROM events WHERE kind=? AND ref=? ORDER BY id LIMIT 1",
                       (events.RUN_START, run)).fetchone()
    return row["op"] if row else None


def run_holdout(conn, root: str, run: str, *, champion: str, contract, session: str) -> list[dict]:
    """Each stage of contract.holdout_stages on the champion commit in a holdout jail. Each check
    gets its own tree, isolation.fresh_tree(root, <the run's clone>, commit=champion,
    path=paths.holdout_dir(root, run, stage)/tree) with the frozen held-out files laid into it, never
    the champion checkout of a round, and destroy_worktree removes it after the check; the same
    files are overlaid read-only in the jail (gates.run_holdout_stage), so a main-checkout edit
    during the run cannot reach the check. Each check is judged as a gate stage is: only the judged
    copy of its evidence folder is read. One research-holdout event per check, keyed on (run,
    champion, stage): a PASS or FAIL already recorded for the same champion is returned again
    (`reused` true) and nothing is written. Returns one dict per stage, in the contract's order:
    {stage, champion, verdict, exit, event, reused, log, reason}. A contract with no holdout stage
    returns []. Raises ValueError for a champion that is not a full commit id."""
    if not isinstance(champion, str) or not _COMMIT.fullmatch(champion):
        raise ValueError("a holdout check runs on one champion commit, not %r" % (champion,))
    stages = [str(s) for s in tuple(getattr(contract, "holdout_stages", ()) or ())]
    if not stages:
        return []
    op = _run_op(conn, run)
    held = frozen_paths(root, run)
    out = []
    for stage in stages:
        prior = _reusable(conn, run, champion, stage)
        if prior is not None:
            out.append(_summary(prior, reused=True))
            continue
        tree_path = os.path.join(paths.holdout_dir(root, run, stage), "tree")
        rel_tree = os.path.relpath(tree_path, os.path.abspath(root))
        clone = None
        wt = None
        gate, gate_event, stopped, reason = {}, None, False, None
        code = vc.BLOCKED
        try:
            clone = isolation.open_clone(root, run)
            if clone is None:
                reason = "the run has no clone to cut a holdout tree from"
            else:
                # a tree a crashed check left at the path is the check's own: remove it, so the
                # fresh cut is the only tree this check ever sees
                stale = isolation.tree_info(tree_path, commit=champion, label="holdout",
                                            admin=False)
                if os.path.lexists(tree_path) or os.path.lexists(stale.host_git):
                    isolation.destroy_worktree(clone, stale)
                wt = isolation.fresh_tree(root, clone, commit=champion, path=tree_path)
                # the clone holds none of the held-out files, nor their folders: lay them in
                # now, by bytes and never through a link, so the jail can bind them read-only
                isolation._overlay(wt.path, tuple(held), True)
                result = gates.run_holdout_stage(conn, root, run=run, contract=contract,
                                                 stage=stage, wt=wt, session=session)
                code, gate_event = result.verdict, result.event_id
                gate = _gate_data(conn, gate_event)
                stopped = bool(gate.get("stopped"))
        except IsolationError as exc:
            reason = "the holdout tree could not be cut: %s" % exc.reason
        except (OSError, ValueError) as exc:
            reason = "the holdout check could not run: %s" % (exc,)
        finally:
            if wt is not None and clone is not None:
                try:
                    isolation.destroy_worktree(clone, wt)
                except (IsolationError, OSError) as exc:
                    reason = reason or "the holdout tree was not removed: %s" % (exc,)
                    code = vc.BLOCKED
        out.append(_summary(_record(conn, run, op=op, session=session, code=code,
                                    champion=champion, stage=stage, tree=rel_tree, gate=gate,
                                    gate_event=gate_event, stopped=stopped, reason=reason),
                            reused=False))
    return out
