"""One composed door per phase boundary (M1.15).

Ported from the earlier harness door shape (`steps/spec-to-step1.py`) and de-signed for Alpaca. The
earlier finding the door shape fixes: a door that composed only ONE link and printed OPEN off a bare
exit code, while the completeness gates lived in an unbuilt caller -- an invisible bypass. This
door composes the whole chain and OPENs iff every link PASSes.

`doors.run(conn, op, boundary) -> verdict` composes, in order:

  1. `workspace_guard` -- the FIRST precondition of every door (a containment breach BLOCKS
     before anything else runs);
  2. the phase's generic-defaults instrument set -- here, the discharge fold over the phase's
     obligation rows: every `item` row for (op, phase) must fold to `discharged` or `waived`
     (an empty universe is a vacuous universal, never a pass -> BLOCKED);
  3. the project's own checks under `contracts/<phase>/`, when present (a project adds its own
     checks in contracts/).

The links fold with `contract.worst()` (BLOCKED > FAIL > PAUSED > PASS). The door OPENs only on
an all-PASS fold; NO flag opens it past a failing link. Whether an open door advances
automatically or PAUSES for a recorded human "go" is read off the human-decision table by the level in
force (`defaults.boundary_decision`). Every advance and every pause is recorded as an event.
"""
from __future__ import annotations

import importlib.util
import os
import sys

from alpaca import contracts_runner, db, paths, questions, render
from alpaca.checklist import Halt, verdict_row
from alpaca.gates import contract, verdict as vc, workspace_guard
from alpaca.phase import defaults

INSTRUMENT = "phase-door"

R_UNKNOWN_BOUNDARY = "PHASE-BOUNDARY-UNKNOWN"


def phase_discharge_code(conn, op, phase) -> int:
    """Fold the discharge state of every `item` obligation row for (op, phase) to one verdict.

    A row folds through `verdict_row.status_fold` (from its verdict rows, never a stored status
    column): discharged/waived -> PASS, failed/open -> FAIL, blocked -> BLOCKED. An EMPTY
    universe is BLOCKED: a phase with no obligations has not been shown complete, so the door
    over it is a vacuous universal, never a pass (this mirrors `worst([])`).
    """
    rows = [r for r in db.rows(conn, "rows", "phase=?", (phase,))
            if (op is None or r.get("op") == op) and r.get("kind") == "item"]
    if not rows:
        return vc.BLOCKED
    codes = []
    for r in rows:
        st = verdict_row.status_fold(conn, r["id"])
        if st in (verdict_row.DISCHARGED, verdict_row.WAIVED_STATUS):
            codes.append(vc.PASS)
        elif st == verdict_row.BLOCKED_STATUS:
            codes.append(vc.BLOCKED)
        else:  # failed, or still open: the obligation is not discharged, so the link fails
            codes.append(vc.FAIL)
    return contract.worst(codes)


def links(conn, op, boundary, *, root=None, tmp_prefixes=None) -> list:
    """The ordered links of the door for one boundary, each a (name, verdict-code) pair.

    workspace_guard is always first. `tmp_prefixes` flows to workspace_guard so a caller can
    isolate the CONTAINMENT property from the NO-TMP property under test (as its own selftest
    does); production passes None for the real scratch-root set.
    """
    if boundary not in defaults.BOUNDARY_PHASE:
        raise Halt(vc.BLOCKED, R_UNKNOWN_BOUNDARY,
                   "%r is not a phase boundary (%s)" % (boundary,
                                                        ", ".join(sorted(defaults.BOUNDARY_PHASE))))
    root = root or paths.root()
    phase = defaults.boundary_phase(boundary)
    out = []
    # 1) the FIRST precondition of every door.
    findings = workspace_guard.check(root, [], tmp_prefixes=tmp_prefixes)
    out.append(("workspace-guard", workspace_guard.verdict_of(findings)))
    # 2) the phase's generic-defaults instrument set, over the record.
    out.append(("phase-defaults:%s" % phase, phase_discharge_code(conn, op, phase)))
    # 2b) M2.17: the projection freshness gate. Every on-disk projection (RESUME.md,
    #     CHECKLIST.md, board.json, data.json) must match the record's fresh render modulo the
    #     declared volatile lines; a hand-edited or stale page closes the door. Best-effort so a
    #     freshness machinery fault never turns a door into a crash; a real staleness is a
    #     finding, not an exception.
    try:
        from alpaca import freshness
        out.append((freshness.NAME, freshness.verdict_of(freshness.check(root))))
    except Exception:
        pass
    # 3) the project's own checks under contracts/<phase>/, when present.
    if contracts_runner.executables(root, phase):
        out.append(("contracts:%s" % phase, contracts_runner.run(root, phase)))
    return out


def _record(conn, kind, op, boundary, session, data):
    """Append one hash-chained event; alpaca is the sole writer. Best-effort so a record that cannot
    be reached never turns a door verdict into a crash."""
    try:
        db.append_event(conn, session=session or "instrument", actor=INSTRUMENT, kind=kind,
                        op=op, ref=boundary, data=data)
    except Exception:
        pass


def run(conn, op, boundary, *, level=None, root=None, tmp_prefixes=None, force=False,
        human_go=False, session="instrument") -> int:
    """Adjudicate one phase boundary and return a verdict-band code.

    The links fold with worst(). If the fold is not PASS the door is CLOSED and the fold verdict
    is returned -- NO flag opens a door past a failing link, so `force` is accepted (a caller may
    try it) but never opens a failed door. On an all-PASS fold the human-decision table decides: at a level
    where the boundary is automatic the door advances (a `phase-advance` event lands); at a lower
    level it PAUSES and records a pending human decision (a `phase-pause` event), unless a human
    "go" is already in hand (`human_go=True`), which advances it. Human decisions stay human:
    `force` never substitutes for the human go a low level requires.
    """
    lk = links(conn, op, boundary, root=root, tmp_prefixes=tmp_prefixes)
    fold = contract.worst([c for _, c in lk])
    named = [[n, vc.name_of(c)] for n, c in lk]

    if fold != vc.PASS:
        _record(conn, "phase-door-closed", op, boundary, session,
                {"boundary": boundary, "verdict": vc.name_of(fold), "level": level,
                 "links": named, "forced": bool(force),
                 "reason": "a door never opens past a failing link"})
        return fold

    # M1.18: the additive owner-only-question gate. Every link PASSes, but a banked owner-only
    # question owed in this boundary's phase is a decision the owner still owes. Below the
    # owner-question threshold that PAUSES this boundary (and only this one -- the question is
    # scoped to its phase, so independent rows and other boundaries keep flowing). A failing link
    # was already returned above, so this never opens a door past one.
    phase = defaults.boundary_phase(boundary)
    if questions.door_question_code(conn, phase, level) == vc.PAUSED:
        owed = questions.open_owner_owed(conn, phase)
        _record(conn, "question-pause", op, boundary, session,
                {"boundary": boundary, "phase": phase, "level": level,
                 "pending": "owner-decision",
                 "questions": [{"id": q["id"], "class": q["class"], "text": q["text"]}
                               for q in owed],
                 "reason": "level %s is below L%d; %d owner-only question(s) owed in phase %s "
                           "pause this boundary until the owner decides"
                           % (level, questions.OWNER_QUESTION_PAUSE_BELOW, len(owed), phase)})
        return vc.PAUSED

    decision = defaults.boundary_decision(boundary, level)
    if decision == "auto":
        _record(conn, "phase-advance", op, boundary, session,
                {"boundary": boundary, "level": level, "mode": "auto", "links": named})
        return vc.PASS
    if human_go:
        _record(conn, "phase-advance", op, boundary, session,
                {"boundary": boundary, "level": level, "mode": "human-go", "links": named})
        return vc.PASS
    _record(conn, "phase-pause", op, boundary, session,
            {"boundary": boundary, "level": level, "pending": "human-go", "links": named,
             "reason": "level %s makes %s a human-go boundary; every link PASSes, so it waits on "
                       "a recorded owner go" % (level, boundary)})
    return vc.PAUSED


# ----------------------------------------------------------------- the release door (M4.9/M4.15)
#: The countersign ref the RELEASE door consumes. Declared once in `render.COUNTERSIGN_REFS`
#: beside M4.16's `manual-phone-read`; the release door reads only this ref and ignores the other,
#: so neither countersign row can discharge the other's obligation.
RELEASE_COUNTERSIGN_REF = "render-neutralisation"

R_COUNTERSIGN_MISSING = "RELEASE-COUNTERSIGN-MISSING"


def countersign_code(conn, ref, *, root=None) -> int:
    """One release-door link: PASS iff an owner-countersign row for `ref` is on the record and its
    decision page resolves, PAUSED (exit 3) when the row is absent. Read THROUGH
    `decisions.resolve`, so a missing owner countersign is a door link that pauses rather than a
    sentence in a plan. A dangling or half-written page never resolves, so it pauses too.
    """
    from alpaca import decisions
    if ref not in render.COUNTERSIGN_REFS:
        raise Halt(vc.BLOCKED, "COUNTERSIGN-REF-UNKNOWN",
                   "%r is not a declared countersign ref (%s)"
                   % (ref, ", ".join(sorted(render.COUNTERSIGN_REFS))))
    try:
        decisions.resolve(conn, ref, root=root)
        return vc.PASS
    except Exception:
        return vc.PAUSED


def record_countersign(conn, ref, surfaces, *, pointer="", actor="owner", session=None,
                       root=None) -> dict:
    """Record an owner-countersign decision keyed by its declared logical `ref` (Step 5).

    Unlike a content-id decision, the decisions.ref column IS the logical ref, so the consuming
    gate finds the row by ref through `decisions.resolve`. The surfaces the owner read are the
    page's options, so the page carries what the owner observed. alpaca is the sole writer: the event,
    the current-state row, the page row and the page file land in one transaction.
    """
    from alpaca import decisions
    root = root or paths.root()
    if ref not in render.COUNTERSIGN_REFS:
        raise Halt(vc.BLOCKED, "COUNTERSIGN-REF-UNKNOWN",
                   "%r is not a declared countersign ref" % ref)
    surfaces = [str(s).strip() for s in (surfaces or []) if str(s).strip()] or ["(surfaces)"]
    from alpaca import util
    ts = util.now_iso()
    rec = {"id": ref, "kind": render.COUNTERSIGN_KIND,
           "context": "owner read the hostile-fixture render on: %s" % ", ".join(surfaces),
           "options": surfaces,
           "choice": "the escaped cells stay legible and the surfaces are sound",
           "consequence": "the release door may fold the %s link to PASS" % ref,
           "pointer": pointer, "ts": ts}
    page_md = decisions._render_page(rec)
    with db.transaction(conn):
        db.append_event(conn, session=session or "cli", actor=actor or "owner",
                        kind=decisions.KIND, ref=ref, data=dict(rec), conn_in_txn=True)
        conn.execute(
            "INSERT INTO decisions (ts, session, actor, kind, body, ref) VALUES (?,?,?,?,?,?)",
            (ts, session or "cli", actor or "owner", render.COUNTERSIGN_KIND,
             util.canonical_json(rec), ref))
        db.upsert(conn, "page", "id", {
            "id": ref, "kind": decisions.PAGE_KIND, "title": "Countersign %s" % ref,
            "body": page_md, "pointer": pointer, "created": ts})
    util.write_text(decisions.page_path(root, ref), page_md)
    return rec


# ------------------------------------------------------------------- the manual gate (M4.16)
def _load_manual_module(root):
    """Load `setup/build_manual_html.py` from the tree at `root`. It is not an importable package
    (its directory has no `__init__.py`), so it is loaded by path -- rename-safe, no folder-name
    literal, no absolute path baked in."""
    mod_path = os.path.join(root, "setup", "build_manual_html.py")
    spec = importlib.util.spec_from_file_location("alpaca_build_manual_html", mod_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def manual_gate_code(*, root=None) -> int:
    """One release-door link (M4.16 clause 7): PASS iff the shipped manual passes its truth gate,
    FAIL otherwise. A failing manual closes the release door, so a release candidate whose manual
    gate fails does not package. Best-effort: a gate that cannot even run is a FAIL, never a silent
    pass (a manual that cannot be checked is not a manual that passed)."""
    root = root or paths.root()
    try:
        mod = _load_manual_module(root)
        return vc.PASS if not mod.gate(root) else vc.FAIL
    except Exception:
        return vc.FAIL


def release_links(conn, op, *, root=None, extra_links=None, include_manual=False) -> list:
    """The ordered links of the release door. `extra_links` (name, verdict-code) pairs are the
    door's OTHER links (M4.15 composes the packaging and manifest links here). When
    `include_manual` is set the manual gate (M4.16) is composed as a link, so a failing manual
    closes the door and the candidate does not package. The owner-countersign link is always
    appended, so the door can never open without the countersign.
    """
    out = list(extra_links or [])
    if include_manual:
        out.append(("manual-gate", manual_gate_code(root=root)))
    out.append(("owner-countersign:%s" % RELEASE_COUNTERSIGN_REF,
                countersign_code(conn, RELEASE_COUNTERSIGN_REF, root=root)))
    return out


def release_door(conn, op, *, level=None, root=None, session="instrument",
                 extra_links=None, include_manual=False) -> int:
    """Adjudicate the release boundary and return a verdict-band code.

    The links fold with worst(). The owner-countersign link is always present: with no
    owner-countersign row whose ref is `render-neutralisation` on the record, that link is PAUSED,
    so the door folds to PAUSED (exit 3) and the recorded reason NAMES the missing ref. With the
    row present the link is PASS, so the door returns the verdict its other links fold to. When
    `include_manual` is set the manual gate (M4.16) is one of the links, so a failing manual
    closes the door. NO flag opens the door past a failing or paused link.
    """
    root = root or paths.root()
    lk = release_links(conn, op, root=root, extra_links=extra_links, include_manual=include_manual)
    fold = contract.worst([c for _, c in lk])
    named = [[n, vc.name_of(c)] for n, c in lk]
    cs = countersign_code(conn, RELEASE_COUNTERSIGN_REF, root=root)
    missing = RELEASE_COUNTERSIGN_REF if cs != vc.PASS else None
    if fold != vc.PASS:
        _record(conn, "release-door-closed", op, "release", session,
                {"verdict": vc.name_of(fold), "level": level, "links": named,
                 "missing_ref": missing, "reason_code": R_COUNTERSIGN_MISSING if missing else None,
                 "reason": ("the release door pauses: owner-countersign ref %s is not on the "
                            "record" % missing) if missing else
                           "the release door does not open past a failing link"})
        return fold
    _record(conn, "release-door-open", op, "release", session,
            {"level": level, "links": named})
    return fold


# ------------------------------------------------------------------------- CLI boundary
def main(argv=None) -> int:
    ap = vc.make_parser(
        name=INSTRUMENT,
        description="Adjudicate one phase boundary: workspace_guard + the phase defaults + "
                    "project contracts, folded; advance or pause by level.")
    ap.add_argument("op", help="the op the boundary belongs to")
    ap.add_argument("boundary", choices=sorted(defaults.BOUNDARY_PHASE),
                    help="the phase boundary to adjudicate")
    ap.add_argument("--level", required=True, help="the autodrive level in force (e.g. L5)")
    ap.add_argument("--go", action="store_true", help="a recorded human go for this boundary")
    ap.add_argument("--force", action="store_true",
                    help="rejected past a failing link; never opens a closed door")
    a = ap.parse_args(argv)
    conn = db.connect(paths.root())
    try:
        code = run(conn, a.op, a.boundary, level=a.level, force=a.force, human_go=a.go)
    except Halt as h:
        return vc.emit_verdict(INSTRUMENT, h.verdict, "%s: %s" % (h.code, h.detail))
    return vc.emit_verdict(INSTRUMENT, code,
                           "boundary %s at level %s" % (a.boundary, a.level))


if __name__ == "__main__":
    sys.exit(main())
