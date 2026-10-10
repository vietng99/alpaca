"""The one transition function of candidate rows (design/rune2-memory/architecture.md 7.3, 7.4, 7.6,
10.2; op-008 t-070 fix plan RC3).

A candidate row moves along one table, whoever moves it: the live doors (`service.propose`,
`service.admit`, `service.withdraw_candidate`, `withdrawal.withdraw` step 6), the replay of the record
(`recovery.replay_candidates`, which a rebuild, a catch-up and an import all run) and the Writer,
which refuses a move the table does not hold (`Writer.set_candidate_status`). The doors call the
decision functions below with the row they read inside their write transaction (fix plan RC1), so
a decision is never taken on a status another process has changed since; replay calls the same
functions with the row it holds and the session the record names, so replay can never accept what
the live door refuses (t-066 adv-r1-m2: a `job:` session; adv-r1-m3: the proposer is the row's,
never a later event's).

Statuses (architecture.md 7.3): `refused` or `checked` at intake; `checked` to `admitted`,
`withdrawn` or `stale`; `stale` to `withdrawn`; `admitted` to `retracted`. A refused admission
leaves `checked`. There is no `superseded` candidate status: when a later admission or an owner
correction supersedes the edge a candidate put in place, the edge carries that (`invalidated`) and the
candidate stays `admitted`; a fact proposed again over an edge that is no longer active (retracted,
invalidated or quarantined) is refused at admission with the edge named (`edge-not-active`, review
finding B-1), so an admission is never `applied` with no live edge.

Pure: nothing here reads or writes a store or the record.
"""
from __future__ import annotations

from dataclasses import dataclass, field

#: Every move of a candidate row: (from, to). `None` is "no row yet". The intake outcome (`checked`
#: or `refused`) is the checks', so both are listed for a new row and for a row refused before
#: (dec-38940e1fb35f Q12: a refusal may have been transient and the proposal is checked again).
MOVES = frozenset({
    (None, "checked"), (None, "refused"),
    ("refused", "checked"), ("refused", "refused"),
    ("checked", "admitted"),
    ("checked", "withdrawn"), ("stale", "withdrawn"),
    ("admitted", "retracted"),
    ("checked", "stale"),
})

#: The acts and the table each one reads (the transition table of the t-070 notes).
TABLE = (
    # act                 from         to           door or replay step
    ("propose",           None,        "checked",   "service.propose; replay of wiki-candidate"),
    ("propose",           None,        "refused",   "service.propose; replay of wiki-candidate"),
    ("propose",           "refused",   "checked",   "service.propose (re-check, Q12); replay"),
    ("propose",           "refused",   "refused",   "service.propose (re-check, Q12); replay"),
    ("admit",             "checked",   "admitted",  "service.admit; replay of wiki-admission"),
    ("withdraw",          "checked",   "withdrawn", "service.withdraw_candidate; replay"),
    ("withdraw",          "stale",     "withdrawn", "service.withdraw_candidate; replay"),
    ("withdraw",          "admitted",  "retracted", "service.withdraw_candidate; replay"),
    ("source-withdrawn",  "checked",   "stale",     "withdrawal.withdraw step 6 (live and replay)"),
    ("source-withdrawn",  "admitted",  "retracted", "withdrawal.withdraw step 6 (live and replay)"),
    ("replay-stale",      "checked",   "stale",     "recovery: a recorded admission that no longer applies"),
)


class IllegalTransition(RuntimeError):
    """A candidate row move the table does not hold; the Writer refuses it and writes nothing."""


@dataclass(frozen=True)
class Decision:
    """What one act does to one candidate row. `outcome` is `go` (the move `to` happens), or the
    answer the door gives without writing: `duplicate`, `refused`. `reason` names why."""
    outcome: str
    to: str | None = None
    reason: str | None = None
    detail: dict = field(default_factory=dict)

    @property
    def go(self) -> bool:
        return self.outcome == "go"


def _status(row) -> str | None:
    return None if row is None else row["status"]


def check_move(current: str | None, to: str) -> None:
    """Raise `IllegalTransition` unless (current, to) is a move of the table."""
    if (current, to) not in MOVES:
        raise IllegalTransition("candidate status %s -> %s is not a move of architecture.md 7.3"
                                % (current or "(none)", to))


def propose(row) -> Decision:
    """A proposal of a candidate id: a new id, or a row refused before, is checked (the checks give
    `checked` or `refused`); any other row answers `duplicate` and is left as it is (architecture.md
    7.1)."""
    status = _status(row)
    if status is None or status == "refused":
        return Decision("go")
    return Decision("duplicate", reason="duplicate", detail={"status": status})


def admit(row, *, session: str) -> Decision:
    """An admission by `session` (architecture.md 7.4): a summary is never admitted, an admitted row
    answers `duplicate`, a job never admits, the proposer never admits its own candidate, and only a
    `checked` row is admitted. The live door and the replay of a recorded `wiki-admission` both ask
    this, with the row they hold."""
    if row is None:
        return Decision("refused", reason="unknown-candidate")
    if row["kind"] == "summary":
        return Decision("refused", reason="summary-not-admitted")
    if row["status"] == "admitted":
        return Decision("duplicate", reason="already-admitted")
    if not isinstance(session, str) or session.startswith("job:"):
        return Decision("refused", reason="job-cannot-admit")
    if session == row["proposer"]:
        return Decision("refused", reason="self-admission")
    if row["status"] != "checked":
        return Decision("refused", reason="not-checked", detail={"status": row["status"]})
    return Decision("go", to="admitted")


def withdraw(row, *, session: str, by: str | None) -> Decision:
    """A withdrawal of the candidate by `session` (architecture.md 7.6): the proposer may, and the
    owner (`by` `owner`, declared); a withdrawn or retracted row answers `duplicate`; a refused row has
    nothing to withdraw. `checked` and `stale` become `withdrawn`, `admitted` becomes `retracted`."""
    if row is None:
        return Decision("refused", reason="unknown-candidate")
    status = row["status"]
    if status in ("withdrawn", "retracted"):
        return Decision("duplicate", reason="already-withdrawn", detail={"status": status})
    if session != row["proposer"] and by != "owner":
        return Decision("refused", reason="not-proposer-or-owner")
    if status == "refused":
        return Decision("refused", reason="nothing-to-withdraw", detail={"status": "refused"})
    to = "retracted" if status == "admitted" else "withdrawn"
    check_move(status, to)
    return Decision("go", to=to)


def source_withdrawn(row) -> Decision:
    """A source the candidate cites as an anchor was withdrawn (architecture.md 10.2 step 6): a
    `checked` row becomes `stale`, an `admitted` one `retracted`; any other row is left."""
    to = {"checked": "stale", "admitted": "retracted"}.get(_status(row))
    return Decision("go", to=to) if to else Decision("refused", reason="no-move")


def replay_stale(row) -> Decision:
    """A recorded admission that no longer applies on the store a replay meets marks a `checked` row
    `stale` (architecture.md 10.2); any other row is left."""
    return Decision("go", to="stale") if _status(row) == "checked" else Decision("refused",
                                                                                reason="no-move")


__all__ = ["MOVES", "TABLE", "Decision", "IllegalTransition", "admit", "check_move", "propose",
           "replay_stale", "source_withdrawn", "withdraw"]
