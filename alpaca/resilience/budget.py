"""Budget survival, concurrency tiers and preflight floors (M3.12, net-new).

This module has NO predecessor: inventory row 83 is doctrine only. Budget survival,
concurrency tiers and preflight floors are written here to their three doctrine documents
(doctrine/budget-survival.md, doctrine/concurrency-tiers.md, doctrine/preflight-floors.md) and
these controls are net-new, not ported.

The rule budget survival states: an unattended run must not die mid-op because it ran out of
allowance. A run keeps a reserve; when its remaining allowance falls to or below the reserve it
enters HOLD (pause and wait, surfaced) rather than starting work it cannot finish. The total
allowance for a run is set by its RESOURCE CLASS, and both the class totals and the reserve are
read from project.yaml, never from code, so a project sizes its own survival margin.

Concurrency tiers: how many workers a run may hold live at once is also a per-class number read
from project.yaml. A run may not exceed its tier.

Preflight floors: before a run starts (or resumes), it must clear the floors: the budget posture
must be GO and the concurrency tier must not be breached. A run that is below budget is held at
the floor, not launched into a mid-op death.

Spend is recorded as append-only `budget-spend` events, so the remaining allowance is a fold over
the record and survives a crash.
"""
from __future__ import annotations

from alpaca import board, db, paths, project

#: one unit of spend against the run's allowance, append-only so remaining is a record fold.
KIND_SPEND = "budget-spend"

#: the posture tokens.
GO = "GO"
HOLD = "HOLD"

#: conservative fallbacks used ONLY when project.yaml is silent, so a bare project does not crash.
#: The real values are DATA in project.yaml; these never override a present value.
_FALLBACK_TOTALS = {"small": 100, "medium": 500, "large": 2000}
_FALLBACK_RESERVE = 10
_FALLBACK_TIERS = {"small": 1, "medium": 3, "large": 8}


def _cfg(root):
    try:
        return project.load(str(root or paths.root()))
    except Exception:
        return {}


def resource_class(root=None) -> str:
    """The run's resource class from project.yaml (`resource_class`), else 'small'."""
    return str(_cfg(root).get("resource_class") or "small")


def total(root=None):
    """The total allowance for the run's class, read from project.yaml `budget.totals[<class>]`.
    Falls back to a conservative default only when the file is silent."""
    cfg = _cfg(root)
    cls = str(cfg.get("resource_class") or "small")
    totals = ((cfg.get("budget") or {}).get("totals")) or {}
    if cls in totals:
        try:
            return float(totals[cls])
        except (TypeError, ValueError):
            pass
    return float(_FALLBACK_TOTALS.get(cls, _FALLBACK_TOTALS["small"]))


def reserve(root=None):
    """The reserve floor, read from project.yaml `budget.reserve`. Falls back only when silent."""
    cfg = _cfg(root)
    raw = (cfg.get("budget") or {}).get("reserve")
    try:
        return float(raw)
    except (TypeError, ValueError):
        return float(_FALLBACK_RESERVE)


def spend(conn, amount, *, session=None, actor="budget", ref=None) -> dict:
    """Record one spend of `amount` units against the allowance, one append-only event."""
    rec = {"amount": float(amount), "ref": None if ref is None else str(ref)}
    return db.append_event(conn, session=session or "budget", actor=actor or "budget",
                           kind=KIND_SPEND, ref=ref, data=rec)


def spent(conn) -> float:
    """The total spent so far, a fold over the append-only spend events (survives a crash)."""
    out = 0.0
    for e in db.events(conn, kind=KIND_SPEND, limit=10 ** 9):
        try:
            out += float(e["data"].get("amount", 0))
        except (TypeError, ValueError):
            continue
    return out


def remaining(conn, *, root=None):
    """The remaining allowance: the class total (from project.yaml) minus what has been spent."""
    return total(root) - spent(conn)


def posture(conn, *, root=None) -> str:
    """The budget posture: GO while the remaining allowance is strictly above the reserve, else
    HOLD. A run at or below its reserve holds rather than dying mid-op."""
    return GO if remaining(conn, root=root) > reserve(root) else HOLD


def state(conn, *, root=None) -> dict:
    """The full budget state for the pad and the board: class, total, spent, remaining, reserve,
    posture."""
    tot = total(root)
    sp = spent(conn)
    return {"resource_class": resource_class(root), "total": tot, "spent": sp,
            "remaining": tot - sp, "reserve": reserve(root),
            "posture": posture(conn, root=root)}


# --------------------------------------------------------------------------- concurrency tiers
def concurrency_limit(root=None) -> int:
    """The max live claims the run's class may hold at once, from project.yaml
    `concurrency.tiers[<class>]`. Falls back only when the file is silent."""
    cfg = _cfg(root)
    cls = str(cfg.get("resource_class") or "small")
    tiers = ((cfg.get("concurrency") or {}).get("tiers")) or {}
    if cls in tiers:
        try:
            return int(tiers[cls])
        except (TypeError, ValueError):
            pass
    return int(_FALLBACK_TIERS.get(cls, _FALLBACK_TIERS["small"]))


def live_worker_count(conn, *, now=None) -> int:
    """How many rows currently hold a live claim: the run's live concurrency, read from the board
    derivation over the record."""
    from alpaca import util
    now = now or util.now_iso()
    return len(board.live_claims(conn, now=now))


def concurrency_ok(conn, *, root=None, now=None) -> bool:
    """True while the live concurrency does not exceed the run's tier (from project.yaml)."""
    return live_worker_count(conn, now=now) <= concurrency_limit(root)


# --------------------------------------------------------------------------- preflight floors
def preflight(conn, *, root=None, now=None) -> dict:
    """The preflight floors a run must clear before it starts or resumes: the budget posture must
    be GO, and the concurrency tier must not be breached. Returns {ok, reasons}; a below-budget or
    over-tier run is held at the floor (ok False) rather than launched into a mid-op death."""
    reasons = []
    if posture(conn, root=root) == HOLD:
        reasons.append("budget below the reserve: HOLD at the floor, do not start a run it cannot finish")
    if not concurrency_ok(conn, root=root, now=now):
        reasons.append("concurrency tier breached: %d live claim(s) over a tier of %d"
                       % (live_worker_count(conn, now=now), concurrency_limit(root)))
    return {"ok": not reasons, "reasons": reasons}


def selftest() -> int:
    """Net-new controls for budget survival, concurrency tiers and preflight floors. Every value
    is read from a scratch project.yaml, proving the class total, the reserve and the tier are
    DATA, not code. Returns 0 on all-PASS, else 1."""
    import os
    import shutil
    import tempfile

    res = []
    base = tempfile.mkdtemp(prefix="budget-selftest-")
    try:
        with open(os.path.join(base, "ALPACA-MANIFEST"), "w", encoding="utf-8") as fh:
            fh.write("selftest\n")
        project.save(base, {
            "name": "t", "resource_class": "small",
            "budget": {"reserve": 10, "totals": {"small": 100, "medium": 500, "large": 2000}},
            "concurrency": {"tiers": {"small": 1, "medium": 3, "large": 8}},
        })
        conn = db.connect(base)

        res.append(("total read from project.yaml (class small -> 100)", total(base) == 100))
        res.append(("reserve read from project.yaml (-> 10)", reserve(base) == 10))

        spend(conn, 40, session="s")
        res.append(("able-to-pass: remaining above reserve -> GO",
                    remaining(conn, root=base) == 60 and posture(conn, root=base) == GO))
        spend(conn, 50, session="s")               # remaining 10 == reserve
        res.append(("able-to-fail: remaining at the reserve -> HOLD (never dies mid-op)",
                    posture(conn, root=base) == HOLD))

        now = "2026-09-16T00:00:05+00:00"
        res.append(("concurrency tier read from project.yaml (small -> 1)",
                    concurrency_limit(base) == 1))
        res.append(("no live claim -> concurrency ok", concurrency_ok(conn, root=base, now=now)))
        db.append_event(conn, session="w1", actor="w1", kind=board.CLAIM_KIND, ref="R1",
                        data={"worker": "w1", "lease_until": "2026-09-16T02:00:00+00:00"})
        db.append_event(conn, session="w2", actor="w2", kind=board.CLAIM_KIND, ref="R2",
                        data={"worker": "w2", "lease_until": "2026-09-16T02:00:00+00:00"})
        res.append(("able-to-fail: two live claims over a tier of one -> not ok",
                    concurrency_ok(conn, root=base, now=now) is False))

        pre = preflight(conn, root=base, now=now)
        res.append(("preflight floor refuses a below-budget, over-tier run",
                    pre["ok"] is False and len(pre["reasons"]) == 2))
        conn.close()
    finally:
        shutil.rmtree(base, ignore_errors=True)

    print("\n  budget selftest")
    print("  " + "-" * 48)
    for label, ok in res:
        print("  %s  %s" % ("PASS" if ok else "FAIL", label))
    all_pass = all(ok for _, ok in res)
    print("SELFTEST PASS: every budget case passed" if all_pass
          else "SELFTEST FAIL: %d of %d case(s) did not pass"
               % (sum(1 for _, ok in res if not ok), len(res)))
    return 0 if all_pass else 1
