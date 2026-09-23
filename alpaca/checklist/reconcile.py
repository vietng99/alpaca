"""State reconcile: the pad cannot claim more rows back than are discharged or waived (M1.13).

Ported from the earlier harness gates/state_reconcile.py, with the match target changed from
countersigned-or-waived SIGNED rows to obligation rows whose folded verdict status is
DISCHARGED or WAIVED (`verdict_row.status_fold`). The invariant is the honest-tag oracle's:

    the pad must NOT claim more done than the record backs.

For every row the pad marks done, a corresponding DISCHARGED or WAIVED obligation row must
exist, matched by the pad row's id/step-key (PRIMARY) or by its evidence pointer (SECONDARY).
Both directions are reported:

  (a) OVER-CLAIM  the pad marks a row done, but no discharged/waived row backs it. The
      dangerous direction: the pad claims completion the record never discharged.
  (b) STALE PAD   a discharged/waived row the pad does not reflect. The softer direction.

The matching is a MAXIMUM BIPARTITE MATCHING (Kuhn's augmenting paths): each discharged row
backs AT MOST ONE claimed row and vice-versa, so #over-claims = claimed - max_matching is
ORDER-INDEPENDENT (Konig). A single discharge can never back two claimed rows (no
laundering), and a faithful pad that admits any full assignment is never false-flagged. This
is the A1 (injectivity) + A2 (order-independence) property carried over verbatim in spirit.

Adaptations for Alpaca, per the plan (M1.13 Step 4): the war-log / STATE.md file becomes the
`claimed` pad passed in and the events-backed fold; a stale pad is REPORTED, never repaired
silently. An empty pad BLOCKS: reconciling nothing is not a pass (the C3 floor never lifts).
"""
from __future__ import annotations

from alpaca import db
from alpaca.checklist import verdict_row

INSTRUMENT = "state-reconcile"

R_EMPTY_PAD = "RECONCILE-EMPTY-PAD"

#: evidence placeholders that mean "no pointer": they never back a discharged row.
_EMPTY_EVIDENCE = frozenset({"", "-", "n/a", "na", "none", "tbd", "todo", "?"})


def _norm(s) -> str:
    if s is None:
        return ""
    return str(s).strip().strip("`*\"' \t").strip()


def _norm_evidence(s) -> str:
    n = _norm(s)
    return "" if n.casefold() in _EMPTY_EVIDENCE else n


def _norm_pointer(s) -> str:
    """Normalize a proof pointer for the secondary match: drop a known scheme prefix, normalize
    separators and case. A directory-qualified pointer is kept in full; only a bare basename is
    allowed to back a directory-qualified proof, and only when that basename is unambiguous."""
    n = _norm(s)
    if not n:
        return ""
    low = n.lower()
    for scheme in ("local:", "remote:", "file://", "file:"):
        if low.startswith(scheme):
            n = n[len(scheme):]
            break
    return n.replace("\\", "/").strip("/").casefold()


def _basename(ptr) -> str:
    return ptr.rsplit("/", 1)[-1] if "/" in ptr else ptr


def _pointer_backs(pad_ptr, item_ptr, basename_count) -> bool:
    if not pad_ptr:
        return False
    if pad_ptr == item_ptr:
        return True
    if "/" in pad_ptr:
        return False
    return pad_ptr == _basename(item_ptr) and basename_count.get(pad_ptr, 0) == 1


def _label_match(pad_row, signed_row) -> bool:
    cid = _norm(pad_row.get("id")).casefold()
    return bool(cid) and (cid == _norm(signed_row.get("id")).casefold()
                          or cid == _norm(signed_row.get("step")).casefold())


def _edge(pad_row, signed_row, item_ptr, basename_count) -> bool:
    if _label_match(pad_row, signed_row):
        return True
    pad_ptr = _norm_pointer(_norm_evidence(pad_row.get("proof")
                                           if pad_row.get("proof") is not None
                                           else pad_row.get("evidence")))
    return _pointer_backs(pad_ptr, item_ptr, basename_count)


def _match(claimed, items):
    """Maximum bipartite matching. Returns (over_rows, stale_items): the claimed rows left
    UNMATCHED (OVER-CLAIM) and the discharged/waived rows left unmatched (STALE PAD)."""
    n_items = len(items)
    item_ptr = [_norm_pointer(it.get("proof")) for it in items]
    basename_count = {}
    for p in item_ptr:
        b = _basename(p)
        if b:
            basename_count[b] = basename_count.get(b, 0) + 1

    adj = [[ii for ii in range(n_items)
            if _edge(r, items[ii], item_ptr[ii], basename_count)]
           for r in claimed]

    match_item = [-1] * n_items
    matched_row = [False] * len(claimed)

    def _augment(di, seen):
        for ii in adj[di]:
            if not seen[ii]:
                seen[ii] = True
                if match_item[ii] == -1 or _augment(match_item[ii], seen):
                    match_item[ii] = di
                    return True
        return False

    for di in range(len(claimed)):
        if _augment(di, [False] * n_items):
            matched_row[di] = True

    over_rows = [claimed[di] for di in range(len(claimed)) if not matched_row[di]]
    stale_items = [items[ii] for ii in range(n_items) if match_item[ii] == -1]
    return over_rows, stale_items


def discharged_rows(conn, op=None) -> list:
    """Every obligation row whose FOLDED verdict status is discharged or waived. Re-derived by
    content from the verdict rows, never read from a stored status word."""
    where, params = ("op=?", (op,)) if op else ("1=1", ())
    out = []
    for r in db.rows(conn, "rows", where, params):
        status = verdict_row.status_fold(conn, r["id"])
        if status in (verdict_row.DISCHARGED, verdict_row.WAIVED_STATUS):
            out.append(r)
    return out


def reconcile(conn, claimed, op=None):
    """Reconcile a `claimed` pad against the discharged/waived rows. Returns (block, info).

    `claimed` is the pad: a list of row ids (strings) or dicts carrying at least `id` and an
    optional `proof`. `block` is a (reason, detail) tuple when the pad is empty (reconciling
    nothing is not a pass); otherwise None and `info` carries `over_claims` and `stale`. This
    function makes no advisory/enforce decision; it lists both directions and never repairs."""
    pad = []
    for c in (claimed or []):
        pad.append({"id": c} if isinstance(c, str) else dict(c))

    info = {"op": op, "claimed_count": len(pad), "discharged_count": 0,
            "over_claims": [], "stale": []}

    if not pad:
        return (R_EMPTY_PAD,
                "the pad claims nothing; a reconciliation over an empty pad is not a pass"), info

    items = discharged_rows(conn, op)
    info["discharged_count"] = len(items)

    over_rows, stale = _match(pad, items)
    info["over_claims"] = [{"id": r.get("id"),
                            "proof": _norm_evidence(r.get("proof"))} for r in over_rows]
    info["stale"] = [{"id": it.get("id"), "step": it.get("step"), "proof": it.get("proof")}
                     for it in stale]
    return None, info
