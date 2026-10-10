"""The engine side of control runs: the attempt id parser, the burn-in state and the path of the
control agent script.

A control run is a run of the no-op or the random-edit control agent (control_agent.py) under
the same contract as a real run (FR-044). Its proposer, implementer and notes roles run the
control script; everything else (the gates, the accept rule, the audit and the two verifiers) is
the real run's, so a control run tests the same trust path. The burn-in is the no-op control run
a real run waits for (gap map F33 and F38, gap decision R11): runner.open_run refuses a real run
under a contract hash until burn_in reads `done` for it.

parse_attempt_id is the one rule both sides use to read the attempt task id from a prompt: the id
after the LAST texts.ENGINE_MARKER (EC-053, gap decision R13). The renderer writes that marker
once, as the prompt's last line, and replaces the marker inside every inserted value
(prompting._defang), so a stale id quoted earlier in the prompt never wins. control_agent.py
restates the marker and the parser, since it imports nothing from alpaca; the t-102 tests check
that the two agree.

Design: design/research-engine/architecture.md sections 3.9, 4.1 (step 5) and 6.4; spec FR-044,
EC-030, EC-052, EC-053, SC-004."""

from __future__ import annotations

import json
import os
import re

from alpaca.research import events, texts

#: the fewest rounds a no-op control run needs before it counts as the burn-in (gap map F38: a
#: path that runs only from round 2 on is missed by a one-round test), whatever the contract says
MIN_ROUNDS = 2
#: an attempt task id: the shape paths.NAME gives every id prompting.render accepts
ATTEMPT_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
#: the control kinds a research-run-start may name
KINDS = ("noop", "random")


def parse_attempt_id(prompt: str) -> str:
    """The attempt task id after the last texts.ENGINE_MARKER in prompt: the first word on the
    rest of that marker's line. Raises ValueError when the prompt is not text, holds no marker, or
    the word after the last marker is not an attempt id (ATTEMPT_ID). An id that an earlier marker
    names is never read, whatever it is (EC-053)."""
    if not isinstance(prompt, str):
        raise ValueError("a prompt is text, not %s" % type(prompt).__name__)
    at = prompt.rfind(texts.ENGINE_MARKER)
    if at < 0:
        raise ValueError("the prompt holds no engine marker")
    rest = prompt[at + len(texts.ENGINE_MARKER):]
    line = rest.splitlines()[0] if rest else ""
    words = line.split()
    if not words or not ATTEMPT_ID.fullmatch(words[0]):
        raise ValueError("the last engine marker names no attempt id: %r" % line[:80])
    return words[0]


def script_path() -> str:
    """The file of control_agent.py, which the launcher copies into a control launch's bin/."""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "control_agent.py")


def agent_argv(python: str, script: str, mode: str, *, role: str, seed: int,
               mutate: str | None = None) -> list:
    """The command line of one control launch: `<python> -I <script> <mode> --role R --seed N
    [--mutate F]`. -I (isolated mode) keeps the script's own folder off sys.path, so a sibling
    file in the launch's bin/ (the pack's mutation script is copied there under its own name, and
    a folder holding `types.py` or `random.py` would shadow the standard library) can never be
    imported in its place. The launcher builds its argv here, and so do the tests."""
    if mode not in KINDS:
        raise ValueError("a control agent is noop or random, not %r" % (mode,))
    argv = [python, "-I", script, mode, "--role", str(role), "--seed", str(int(seed))]
    if mutate:
        argv += ["--mutate", mutate]
    return argv


def _rows(conn, kind: str, ref: str | None = None) -> list:
    if ref is None:
        got = conn.execute("SELECT id, ts, ref, data FROM events WHERE kind=? ORDER BY id",
                           (kind,)).fetchall()
    else:
        got = conn.execute("SELECT id, ts, ref, data FROM events WHERE kind=? AND ref=? "
                           "ORDER BY id", (kind, ref)).fetchall()
    out = []
    for row in got:
        try:
            data = json.loads(row["data"] or "{}")
        except (TypeError, ValueError):
            data = {}
        out.append({"id": row["id"], "ts": row["ts"], "ref": row["ref"],
                    "data": data if isinstance(data, dict) else {}})
    return out


def required_rounds(start_data: dict) -> int:
    """The rounds a no-op control run under this run-start's contract must finish: the frozen
    block's research.controls.noop_rounds, never fewer than MIN_ROUNDS."""
    block = start_data.get("block") if isinstance(start_data.get("block"), dict) else {}
    controls = block.get("controls") if isinstance(block.get("controls"), dict) else {}
    try:
        declared = int(controls.get("noop_rounds") or 0)
    except (TypeError, ValueError):
        declared = 0
    return max(MIN_ROUNDS, declared)


def accepted_attempt(conn, run: str) -> str | None:
    """The task id of the first attempt of `run` (the round 0 baseline excluded) that the accept
    rule took (state.accept_taken), the attempt state.fold reads as control_failed; None when
    there is none. The fact is kept whatever the trust path did afterwards: a verification that
    ends refuted or unverified, or an audit that blocks, still leaves the attempt a control the
    accept rule took. A provisional accept before the research-attempt event counts too, so a
    control run killed after its accept step still reads as failed (fail closed)."""
    from alpaca.research import state
    for add in state._run_tasks(conn, run):
        research = add["data"].get("research") or {}
        if research.get("role") != "attempt" or research.get("baseline"):
            continue
        if state.accept_taken(conn, add["ref"]):
            return add["ref"]
    return None


def _ran_rounds(conn, run: str) -> set:
    """The round numbers from 1 up in which at least one attempt of `run` reached the gate chain:
    the attempt has a research-accept event (the accept rule judged it), or a research-gate event
    on the attempt side whose verdict is not BLOCKED. A round whose every attempt was abandoned
    or never got to a gate (every launch failed to start, say) is not a round the control ran."""
    from alpaca.research import state
    ran = set()
    for add in state._run_tasks(conn, run):
        research = add["data"].get("research") or {}
        if research.get("role") != "attempt" or research.get("baseline"):
            continue
        try:
            n = int(research.get("round") or 0)
        except (TypeError, ValueError):
            continue
        if n < 1 or n in ran:
            continue
        task = add["ref"]
        if _rows(conn, events.ACCEPT, task) or any(
                g["data"].get("side") == "attempt" and g["data"].get("verdict") != "BLOCKED"
                for g in _rows(conn, events.GATE, task)):
            ran.add(n)
    return ran


def _rounds_done(conn, run: str) -> int:
    """The distinct round numbers from 1 up that have a research-round event of `run` and an
    attempt that reached the gate chain (_ran_rounds); round 0, the baseline, is not a control
    round."""
    done = set()
    for ev in _rows(conn, events.ROUND, run):
        try:
            n = int(ev["data"].get("round") or 0)
        except (TypeError, ValueError):
            continue
        if n >= 1:
            done.add(n)
    return len(done & _ran_rounds(conn, run))


def _finished(conn, run: str) -> bool:
    finals = _rows(conn, events.FINALIZE, run)
    return bool(finals) and finals[-1]["data"].get("state") == "finished"


def burn_in(conn, contract_sha256: str) -> dict:
    """The burn-in state of the contract hash, read from the record (FR-044, EC-030, EC-052):

    - {"state": "failed", "run", "attempt", "control", "reason"} when any control run under the
      hash, no-op or random, has an accepted attempt: a gate or the accept rule is broken, and no
      later control run clears that, since the same contract still holds the same gates;
    - {"state": "done", "run", "rounds", "of"} for the first no-op control run under the hash
      that finished (its newest research-finalize has state `finished`) after at least `of`
      rounds (required_rounds: the contract's noop_rounds, never fewer than MIN_ROUNDS), a round
      counting only when one of its attempts reached the gate chain (_ran_rounds), so a run
      whose launches all failed to start does not unlock a real run (EC-052);
    - else {"state": "needed", "rounds", "of"}: the most rounds a no-op control run under the
      hash has done so far, and how many the burn-in needs.

    A random control run never counts as the burn-in. Reads only; writes nothing."""
    runs = [ev for ev in _rows(conn, events.RUN_START)
            if ev["data"].get("contract_sha256") == contract_sha256
            and ev["data"].get("control") in KINDS]
    for ev in runs:
        attempt = accepted_attempt(conn, ev["ref"])
        if attempt is not None:
            kind = ev["data"].get("control")
            return {"state": "failed", "run": ev["ref"], "attempt": attempt, "control": kind,
                    "reason": "control run %s (%s) accepted attempt %s, so a gate or the accept "
                              "rule is broken" % (ev["ref"], kind, attempt)}
    best, of = 0, MIN_ROUNDS
    for ev in runs:
        if ev["data"].get("control") != "noop":
            continue
        need = required_rounds(ev["data"])
        rounds = _rounds_done(conn, ev["ref"])
        if rounds >= need and _finished(conn, ev["ref"]):
            return {"state": "done", "run": ev["ref"], "rounds": rounds, "of": need}
        if rounds >= best:
            best, of = rounds, need
    return {"state": "needed", "rounds": best, "of": of}
