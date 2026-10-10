"""The owner grant: the terminal confirmation, the grant record and the grant check. Names owned by
operator-surface.md section 4.6; `authorized` is t-096's, `OwnerRefusal`, `confirm_at_terminal`
and `grant` are t-105's.

A grant lifts an owner hold (extension, budget, a stop at a hold) or answers a contract ref
before a run exists (loosen, budget, reopen). The record cannot tell who typed a command, so the
grant is taken only at an interactive terminal outside any agent session, the way the release
approval is (alpaca/release/cli.py:14-24): `confirm_at_terminal` refuses when an agent marker is
set or stdin is not a terminal, and asks the owner to type the run id or the runbook path. Only
then does `grant` record, in one transaction, an owner decision (decisions.record with actor
"owner") and a `research-grant` event with confirmed "terminal". `authorized` reads both back.

Design: design/research-engine/operator-surface.md sections 4.3.7 and 4.6."""

from __future__ import annotations

import json
import os
import re
import sys

from alpaca import db
from alpaca.gates import verdict as vc
from alpaca.research import paths


KIND_GRANT = "research-grant"


AGENT_MARKERS = ("CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID", "CODEX_SANDBOX")


GATES = ("extension", "budget", "stop", "loosen", "reopen")


class OwnerRefusal(Exception):
    def __init__(self, code: int, reason: str, detail: str = ""):
        """A grant the owner check refuses: `code` an exit code of alpaca.gates.verdict, `reason`
        a RESEARCH-* code of operator-surface section 4.4, `detail` the text the verb prints."""
        super().__init__("%s %s: %s" % (vc.name_of(code), reason, detail))
        self.code, self.reason, self.detail = int(code), reason, detail


def confirm_at_terminal(expected: str, *, prompt: str, environ=None, isatty=None,
                        read=None) -> None:
    """Raise OwnerRefusal(FAIL, RESEARCH-OWNER-NOT-TERMINAL) when an agent marker is set or stdin
    is not a terminal, and OwnerRefusal(FAIL, RESEARCH-OWNER-CONFIRM-MISMATCH) when the typed text
    differs from `expected`. environ, isatty and read default to os.environ, sys.stdin.isatty and
    input; tests pass their own (operator-surface section 4.6)."""
    env = os.environ if environ is None else environ
    tty = sys.stdin.isatty if isatty is None else isatty
    ask = input if read is None else read
    marked = [name for name in AGENT_MARKERS if env.get(name)]
    if marked:
        raise OwnerRefusal(vc.FAIL, "RESEARCH-OWNER-NOT-TERMINAL",
                           "an agent session is set (%s); the owner types a grant in a terminal "
                           "of their own" % ", ".join(marked))
    try:
        interactive = bool(tty())
    except (OSError, ValueError, AttributeError):
        interactive = False
    if not interactive:
        raise OwnerRefusal(vc.FAIL, "RESEARCH-OWNER-NOT-TERMINAL",
                           "stdin is not a terminal; the owner types a grant at a terminal")
    try:
        typed = ask(prompt)
    except (EOFError, OSError, KeyboardInterrupt):
        typed = None
    if typed is None or str(typed).strip() != str(expected):
        raise OwnerRefusal(vc.FAIL, "RESEARCH-OWNER-CONFIRM-MISMATCH",
                           "the typed confirmation is not %s; nothing recorded" % expected)


# ------------------------------------------------------------------ the grant record
_TERMS = "RESEARCH-GRANT-TERMS"
_NONE = "RESEARCH-HOLD-NONE"
_RUN_ID = paths.RUN_ID
#: a field path of the research block: dotted from `research.`, a keyed list item in brackets
_FIELD_PATH = re.compile(r"^research(\.[A-Za-z_][A-Za-z0-9_-]*(\[[^\[\]]+\])?)+$")
#: the four forms that name what lies outside the block, which only a pack edit changes
_PACK_PATH = re.compile(r"^(stages|knobs|files)\[[^\[\]]+\]$|^spec$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


def _refuse(code, reason, detail):
    raise OwnerRefusal(code, reason, detail)


def _run_start(conn, run):
    row = conn.execute("SELECT id, op, data FROM events WHERE kind='research-run-start' AND ref=? "
                       "ORDER BY id LIMIT 1", (run,)).fetchone()
    if row is None:
        return None
    try:
        data = json.loads(row["data"] or "{}")
    except (TypeError, ValueError):
        data = {}
    return {"id": row["id"], "op": row["op"], "data": data}


def _split_contract_ref(ref):
    """(runbook path, task or None) of contract:<runbook path>#<task or ->, else None."""
    if not isinstance(ref, str) or not ref.startswith("contract:") or "#" not in ref:
        return None
    path, _, task = ref[len("contract:"):].rpartition("#")
    if not path:
        return None
    return path, (None if task in ("", "-") else task)


def _bound(conn, root, run):
    """The run's round bound now: N of its frozen contract (contract.read_frozen), or the newest
    owner extension, by the one rule the runner applies (runner.rounds_bound). A contract that no
    longer reads as frozen refuses the grant with read_frozen's RUN-CONTRACT-CHANGED."""
    from alpaca.research import contract as contract_mod, runner
    from alpaca.research.errors import ResearchRefusal
    try:
        frozen = contract_mod.read_frozen(root, run)
    except ResearchRefusal as exc:
        _refuse(vc.FAIL, exc.reason, exc.detail or str(exc))
    return runner.rounds_bound(conn, run, frozen)


def _num(value, what, *, whole=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value \
            or value <= 0:
        _refuse(vc.FAIL, _TERMS, "%s must be a number above 0, not %r" % (what, value))
    if whole and int(value) != value:
        _refuse(vc.FAIL, _TERMS, "%s must be a whole number, not %r" % (what, value))
    return int(value) if whole else value


def _fmt(value):
    if value is None:
        return "none"
    if isinstance(value, float) and value == int(value):
        return "%d" % value
    return "%s" % value


def _check_budget_terms(conn, root, run, terms):
    """The rules of operator-surface section 4.3.7 for a budget grant on a run: each given cap
    above the current one, every reached cap raised, --wall only on a run with a wall cap, and
    no caps at all only with no cap reached. The project budget posture does not count: it never
    holds a research run (t-297, in-session-runtime.md section 21). Returns the caps after the
    grant and the spend check."""
    from alpaca.research import spend
    usd, tokens, wall = terms.get("usd"), terms.get("tokens"), terms.get("wall")
    if usd is not None:
        usd = _num(usd, "--usd")
    if tokens is not None:
        tokens = _num(tokens, "--tokens", whole=True)
    if wall is not None:
        wall = _num(wall, "--wall", whole=True)
    caps = spend.caps(conn, run)
    check = spend.check(conn, root, run, role="proposer")
    reached = []
    if caps.get("usd") is not None and check.usd_spent >= float(caps["usd"]):
        reached.append("usd")
    if caps.get("tokens") is not None and check.tokens_spent >= int(caps["tokens"]):
        reached.append("tokens")
    if caps.get("wall") is not None and check.wall_s_spent >= int(caps["wall"]):
        reached.append("wall")
    given = {"usd": usd, "tokens": tokens, "wall": wall}
    if wall is not None and caps.get("wall") is None:
        _refuse(vc.FAIL, _TERMS, "%s has no wall-time cap to raise; leave --wall out" % run)
    for key, value in given.items():
        if value is not None and caps.get(key) is not None and value <= caps[key]:
            _refuse(vc.FAIL, _TERMS, "--%s %s does not raise the current cap %s"
                    % (key, _fmt(value), _fmt(caps[key])))
    unraised = [key for key in reached if given[key] is None]
    if unraised:
        _refuse(vc.FAIL, _TERMS, "the %s cap is reached and the grant leaves it as it is; "
                "raise it with %s" % (" and ".join(unraised),
                                      " ".join("--%s" % k for k in unraised)))
    after = {key: given[key] if given[key] is not None else caps.get(key) for key in given}
    return after, given


def _check_loosen_terms(terms, task):
    """The loosen terms of operator-surface section 4.3.7: {"set": {path: value}}, each path one of
    the five forms, the four pack forms only on the pack ref, a files value 64 hex digits or
    null, a spec value text, a stage or knob value a mapping or null."""
    sets = terms.get("set") if isinstance(terms, dict) else None
    if not isinstance(sets, dict) or not sets:
        _refuse(vc.FAIL, _TERMS, "a loosen grant names at least one --set PATH=VALUE")
    for path, value in sets.items():
        if _FIELD_PATH.match(path or ""):
            continue
        if not _PACK_PATH.match(path or ""):
            _refuse(vc.FAIL, _TERMS, "%r is not a field path (research.<...>, stages[<id>], "
                    "knobs[<id>], spec or files[<path>])" % (path,))
        if task is not None:
            _refuse(vc.FAIL, _TERMS, "%s names what only a pack edit changes; grant it on the pack "
                    "ref, without --task" % path)
        if path.startswith("files[") and value is not None and \
                not (isinstance(value, str) and _HEX64.match(value)):
            _refuse(vc.FAIL, _TERMS, "%s takes a sha256 of 64 hex digits or null" % path)
        if path == "spec" and not isinstance(value, str):
            _refuse(vc.FAIL, _TERMS, "spec takes a path as text")
        if path.startswith(("stages[", "knobs[")) and value is not None \
                and not isinstance(value, dict):
            _refuse(vc.FAIL, _TERMS, "%s takes a mapping or null" % path)
    return {"set": dict(sets)}


_LOOSEN_LINE = re.compile(r"^(?P<field>\S.*?): accepted (?P<accepted>.*), new (?P<new>.*)$")


def _accepted_values(detail):
    """field path -> the accepted value text, from a loosen hold's detail lines."""
    out = {}
    for line in str(detail or "").splitlines():
        m = _LOOSEN_LINE.match(line.strip())
        if m:
            out.setdefault(m.group("field"), m.group("accepted"))
    return out


def _stopped_run(conn, runbook):
    """The newest real run under `runbook` when the owner stopped it at a hold and no reopen grant
    on the runbook path came after that stop, else None."""
    newest = None
    for ev in _events(conn, "research-run-start"):
        d = _data(ev)
        if d.get("runbook") == runbook and not d.get("control"):
            newest = ev
    if newest is None:
        return None
    stops = [g for g in _events(conn, KIND_GRANT)
             if g.get("ref") == newest.get("ref") and _data(g).get("gate") == "stop"]
    if not stops:
        return None
    reopens = [g for g in _events(conn, KIND_GRANT)
               if _data(g).get("gate") == "reopen" and g.get("ref") == runbook
               and g["id"] > stops[-1]["id"]]
    return None if reopens else newest.get("ref")


def _hold_of(conn, ref, gates):
    from alpaca.research import holds
    for hold in holds.open_holds(conn, ref):
        if hold.get("gate") in gates:
            return hold
    return None


def _page(conn, root, *, ref, gate, terms, applied):
    """(kind, context, options, choice, consequence, pointer, hold, expected, terms) of the
    decision a grant records, after the hold and term checks (operator-surface section 4.3.7)."""
    run = ref if _RUN_ID.match(ref or "") else None
    split = _split_contract_ref(ref)
    kind = _DECISION_KIND[gate]
    held = "; %s stays held until `alpaca research extend %s` or `alpaca research resume %s` " \
           "runs" % (ref, ref, ref)
    if gate in ("extension", "stop") or (gate == "budget" and run):
        if run is None:
            _refuse(vc.FAIL, _TERMS, "a %s grant names a run id, not %r" % (gate, ref))
        start = _run_start(conn, run)
        if start is None:
            _refuse(vc.FAIL, "RESEARCH-RUN-UNKNOWN", "no research-run-start names %s" % run)
        gates = {"extension": ("extension",), "budget": ("budget",),
                 "stop": ("extension", "budget")}[gate]
        hold = _hold_of(conn, run, gates)
        if hold is None:
            _refuse(vc.FAIL, _NONE, "%s has no open %s hold" % (run, " or ".join(gates)))
        runbook = start["data"].get("runbook")
        tail = " (hold %s)" % hold["id"]
        if gate == "extension":
            n = _bound(conn, root, run)
            rounds = (terms or {}).get("rounds")
            if isinstance(rounds, bool) or not isinstance(rounds, int) or rounds <= n:
                _refuse(vc.FAIL, _TERMS, "--rounds must be a whole number above the run's current "
                        "%d rounds, not %r" % (n, rounds))
            choice = "extend %s to %d rounds" % (run, rounds)
            return (kind, "%s halted at %d rounds, targets unmet: %s%s"
                    % (run, n, hold["detail"], tail),
                    [choice, "stop %s" % run, "keep the hold"], choice,
                    "%s runs on to %d rounds%s" % (run, rounds, held if not applied else ""),
                    "research-grant:%s:extension:%s" % (run, hold["id"]), hold, run,
                    {"rounds": rounds})
        if gate == "budget":
            after, given = _check_budget_terms(conn, root, run, terms or {})
            if all(v is None for v in given.values()):
                choice = "continue %s at the project budget" % run
                outcome = "%s runs on at the project budget" % run
            else:
                caps = "%s USD / %s tokens" % (_fmt(after["usd"]), _fmt(after["tokens"]))
                if after["wall"] is not None:
                    caps += " / %s seconds wall" % _fmt(after["wall"])
                choice = "raise %s budget to %s" % (run, caps)
                outcome = "%s runs on under %s" % (run, caps)
            return (kind, "%s held on its budget: %s%s" % (run, hold["detail"], tail),
                    [choice, "stop %s" % run, "keep the hold"], choice,
                    outcome + (held if not applied else ""),
                    "research-grant:%s:budget:%s" % (run, hold["id"]), hold, run, given)
        choice = "stop %s" % run
        return (kind, "%s held at %s: %s%s" % (run, hold["gate"], hold["detail"], tail),
                [choice, "keep the hold"], choice,
                "%s ends as stopped; a new real run under %s needs a reopen grant"
                % (run, runbook), "research-grant:%s:stop:%s" % (run, hold["id"]), hold, run, {})
    if gate == "reopen":
        runbook = ref
        if split is not None or run is not None or not runbook:
            _refuse(vc.FAIL, _TERMS, "a reopen grant names the runbook path with --runbook")
        stopped = _stopped_run(conn, runbook)
        if stopped is None:
            _refuse(vc.FAIL, _TERMS, "no real run under %s ended by the owner's stop without a "
                    "later reopen grant; nothing to reopen" % runbook)
        choice = "allow new real runs under %s" % runbook
        return (kind, "the owner stopped %s, the last real run under %s, at a hold%s"
                % (stopped, runbook, _no_hold(conn)),
                [choice, "keep new real runs under %s refused" % runbook], choice,
                "start may begin new real runs under %s" % runbook,
                "research-grant:%s:reopen:%s" % (runbook, stopped), None, runbook, {})
    if split is None:
        _refuse(vc.FAIL, _TERMS, "a %s grant on a contract names --runbook PATH" % gate)
    runbook, task = split
    if gate == "budget":
        if any(v is not None for v in (terms or {}).values()):
            _refuse(vc.FAIL, _TERMS, "the --runbook budget form takes no cap; a raised cap on a "
                    "contract is a loosen grant")
        hold = _hold_of(conn, ref, ("budget",))
        if hold is None:
            _refuse(vc.FAIL, _NONE, "%s has no open budget hold" % ref)
        # the project budget posture no longer holds a research start (t-297, section 21), so a
        # start hold opened before that rule is lifted whatever the posture reads
        choice = "start %s at the project budget" % ref
        return (kind, "start of %s held on the project budget: %s (hold %s)"
                % (ref, hold["detail"], hold["id"]), [choice, "keep the hold"], choice,
                "the next start of %s runs at the project budget" % ref,
                "research-grant:%s:budget:%s" % (ref, hold["id"]), hold, runbook, {})
    # loosen
    clean = _check_loosen_terms(terms, task)
    hold = _hold_of(conn, ref, ("loosen",))
    accepted = _accepted_values(hold["detail"]) if hold else {}
    from alpaca import util
    pairs = [(path, accepted.get(path, "the accepted value"), util.canonical_json(value))
             for path, value in sorted(clean["set"].items())]
    tail = " (hold %s)" % hold["id"] if hold else _no_hold(conn)
    choice = "; ".join("loosen %s from %s to %s for %s" % (p, a, b, ref) for p, a, b in pairs)
    return (kind, "; ".join("%s loosens %s from %s to %s" % (ref, p, a, b) for p, a, b in pairs)
            + tail, [choice, "keep the accepted values"], choice,
            "the next start of %s may use the granted values" % ref,
            "research-grant:%s:loosen" % ref, hold, runbook, clean)


def _no_hold(conn):
    last = db.last_event(conn)
    return " (no hold open, record through event %s)" % (last["id"] if last else 0)


def grant(conn, root, *, ref: str, gate: str, terms: dict, note: str,
          session: str, environ=None, isatty=None, read=None, applied: bool = True) -> dict:
    """Record the decision and the research-grant event in one transaction after
    confirm_at_terminal. Returns {"decision": id, "event": id, "hold": id or None}.

    The checks come first and write nothing: the gate (one of GATES), the ref (a run id for
    extension, stop and a run's budget; contract:<runbook path>#<task or -> for loosen and a
    contract's budget; the runbook path for reopen), the open hold the gate answers
    (RESEARCH-HOLD-NONE), and the terms (RESEARCH-GRANT-TERMS). Then the owner types the run id,
    or the runbook path for the contract forms, at the terminal (environ, isatty and read go to
    confirm_at_terminal). The decision has actor "owner", the kind, choice and pointer of
    operator-surface section 4.3.7, and a context that ends with the hold id or the newest event
    id, so two grants with the same terms get two ids. `applied` False says in the consequence
    that the run stays held until extend or resume runs (`--no-apply`). Raises OwnerRefusal."""
    if gate not in GATES:
        _refuse(vc.FAIL, _TERMS, "the gate is one of %s, not %r" % (", ".join(GATES), gate))
    kind, context, options, choice, consequence, pointer, hold, expected, clean = _page(
        conn, root, ref=ref, gate=gate, terms=terms or {}, applied=applied)
    what = "the run id" if _RUN_ID.match(str(expected)) else "the runbook path"
    confirm_at_terminal(str(expected), prompt="Owner grant: %s\nType %s (%s) to confirm: "
                        % (choice, what, expected), environ=environ, isatty=isatty, read=read)
    from alpaca import decisions
    from alpaca.research import events
    run = ref if _RUN_ID.match(ref) else None
    start = _run_start(conn, run) if run else None
    with db.transaction(conn):
        rec = decisions.record(conn, kind, context, options, choice, consequence, pointer,
                               actor="owner", session=session, root=root)
        ev = events.emit(conn, events.GRANT, run=run, ref=ref, op=start["op"] if start else None,
                         session=session, actor="owner", in_txn=True,
                         data={"ref": ref, "gate": gate, "hold": hold["id"] if hold else None,
                               "decision": rec["id"], "terms": clean, "note": note or "",
                               "confirmed": "terminal", "by": "owner"})
    return {"decision": rec["id"], "event": ev["id"], "hold": hold["id"] if hold else None}


#: the decision kind a grant of each gate records (operator-surface.md section 4.3.7)
_DECISION_KIND = {"extension": "go", "budget": "go", "stop": "go", "loosen": "done_when", "reopen": "go"}
#: the hold gate each grant gate answers; stop answers an open hold of any gate (section 4.7)
_HOLD_OF = {"extension": ("extension",), "budget": ("budget",), "stop": None}
_MISSING = "RESEARCH-OWNER-GRANT-MISSING"


def _data(event):
    data = event.get("data")
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except ValueError:
            data = None
    return data if isinstance(data, dict) else {}


def _events(conn, kind):
    return db.events(conn, kind=kind, limit=10 ** 9)


def _open_holds(conn, ref, gates):
    """Ids of the research-hold events on `ref` (of one of `gates`, or any gate for None) that no
    research-hold-lifted event answers."""
    lifted = {str(_data(e).get("hold")) for e in _events(conn, "research-hold-lifted")}
    out = []
    for event in _events(conn, "research-hold"):
        data = _data(event)
        if event.get("ref") != ref and data.get("ref") != ref:
            continue
        if gates is not None and data.get("gate") not in gates:
            continue
        if str(event["id"]) not in lifted:
            out.append(event["id"])
    return out


def _used(conn, decision):
    """True when a lifted hold, a run contract or a landed pack contract already used the decision."""
    for event in _events(conn, "research-hold-lifted"):
        if _data(event).get("decision") == decision:
            return True
    for kind in ("research-run-start", "research-contract"):
        for event in _events(conn, kind):
            if decision in (_data(event).get("grants") or []):
                return True
    token = "decision:%s" % decision
    for event in _events(conn, "intent-record"):
        data = _data(event)
        label, pointer = str(data.get("label", "")), str(data.get("pointer", ""))
        if label.startswith("research contract ") and token in pointer.split():
            return True
    return False


def authorized(conn, ref: str, gate: str, decision_id: str | None = None) -> tuple:
    """(True, decision_id) when a research-grant event for (ref, gate) has confirmed == "terminal",
    names a decision whose event has actor "owner" and the kind operator-surface section 4.3.7
    gives the gate, answers the hold that is open now (its `hold` equals the open hold's id, or the
    gate is loosen or reopen, which may come before any hold), and no research-hold-lifted event
    or later start already used it; the newest such grant, or the one `decision_id` names.
    Otherwise (False, "RESEARCH-OWNER-GRANT-MISSING"). Reads only (built by t-096; design:
    operator-surface.md section 4.6, architecture.md section 3.5)."""
    if gate not in GATES:
        return False, _MISSING
    decisions = {}
    for event in _events(conn, "decision"):
        decisions[event.get("ref")] = event
    holds = None
    for event in reversed(_events(conn, KIND_GRANT)):
        data = _data(event)
        if (event.get("ref") != ref and data.get("ref") != ref) or data.get("gate") != gate:
            continue
        decision = data.get("decision")
        if decision_id is not None and decision != decision_id:
            continue
        if data.get("confirmed") != "terminal" or not isinstance(decision, str):
            continue
        record = decisions.get(decision)
        if record is None or record.get("actor") != "owner" or _data(record).get("kind") != _DECISION_KIND[gate]:
            continue
        if gate not in ("loosen", "reopen"):
            if holds is None:
                holds = [str(h) for h in _open_holds(conn, ref, _HOLD_OF.get(gate))]
            if str(data.get("hold")) not in holds:
                continue
        if _used(conn, decision):
            continue
        return True, decision
    return False, _MISSING
