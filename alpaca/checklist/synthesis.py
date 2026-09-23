"""Row synthesis. Ported from the earlier harness gates/row_synthesis.py and adapted for Alpaca.

One row per (step, item): every step of the phase's step model, crossed with every item
of the parsed acceptance artifact it consumes, becomes exactly one obligation row. A row
is never hand-listed; a hand list is the reproduced defect, where whatever the curator did
not think of ships unaudited. The population is derived from the FULL step x item product,
the rows are staged, then the partition is re-derived from the staged rows and checked to
biject with that population. Nothing is committed unless the whole check is a PASS.

Adaptations from the earlier harness, per the plan (M1.10):

  * The interface is `synthesize(step_model, artifact)`: the step model is already loaded
    (`step_model.load`) and the artifact already parsed (`artifact.parse`), so the config,
    filesystem staging and pack-witnessing plumbing of the earlier harness do not live here.
  * `row_id(step, artifact_path, key, artifact_sha)` is a hash of exactly those four
    things, matching the M1 interface block. No signer, capture or campaign-slice input.
  * The earlier harness SIGN fields never exist. A row carries the generic Alpaca schema only, and an
    empty `why` pointer the design phase (M2.6) must fill with a decision-page reference.
  * "Output staged unless PASS": `stage()` always returns the derived rows plus a verdict
    and a `committed` flag; `synthesize()` returns those rows only on a PASS and raises
    otherwise, so a non-PASS leaves the output staged rather than committed.

Refusals surface through `alpaca.checklist.Halt` with a verdict-band code. There is no
default-on-miss and no cap: a defect is refused, never laundered into a degraded pass.
"""
from __future__ import annotations

import re

from alpaca import util
from alpaca.checklist import Halt
from alpaca.gates import contract, verdict

INSTRUMENT = "row-synthesis"
ROW_SCHEMA = "alpaca-row/v1"

#: the genesis link for the per-batch row chain; the empty-input hash, as in alpaca.db.
GENESIS = util.sha256_hex(b"")

#: the statement floor from the row schema (spec:284): a checkable claim, not a stub.
STATEMENT_MIN_ALNUM = 12
STATEMENT_MIN_WORDS = 3

#: the tag a freshly synthesized obligation carries; the tag oracle promotes it later.
SPECCED = "Specced"

_SLUG_RE = re.compile(r"[^a-z0-9]+")

#: the load-bearing content of a row: what content_hash covers. The chain fields
#: (content_hash, prev_hash) and volatile author identity are excluded so the hash is
#: stable and a later drift on the same obligation is detectable.
_CONTENT_FIELDS = ("id", "kind", "phase", "step", "item", "artifact", "statement",
                   "proof", "where", "how", "when", "why", "tag", "status", "supersedes")


def _slug(text: str) -> str:
    return _SLUG_RE.sub("-", str(text).lower()).strip("-") or "x"


def row_id(step: str, artifact_path: str, key: str, artifact_sha: str) -> str:
    """The stable id of one obligation row: a readable stem plus a digest of exactly the
    step, the artifact path, the item key and the artifact's raw sha. Two rows for the same
    obligation over the same artifact bytes get the same id; any change to the artifact's
    bytes changes every id, so a silently re-encoded artifact cannot re-use stale ids."""
    digest = util.sha256_hex("|".join([step, artifact_path, key, artifact_sha]))
    return "%s.%s-%s" % (_slug(step), _slug(key), digest[:12])


def _applicable_steps(step_model: dict, artifact: dict) -> list:
    """The steps that consume this artifact. A parsed artifact may carry a `kind`; when it
    does, only the steps whose `consumes` names that kind apply. When it does not, every
    step applies to the sole artifact, so the interface stays a two-argument call."""
    steps = step_model.get("steps") or []
    kind = artifact.get("kind")
    if kind is None:
        return list(steps)
    return [s for s in steps if kind in (s.get("consumes") or [])]


#: `{cell:<column>}` in an obligation template: the item's cell in that column of the acceptance
#: table (header matched as written, case and spacing ignored). A column the table lacks is left
#: as written, so a template never silently loses words.
_CELL_RE = re.compile(r"\{cell:([^}]+)\}")


def _cell(item: dict, column: str):
    want = " ".join(column.split()).casefold()
    for name, value in (item.get("cells") or {}).items():
        if " ".join(str(name).split()).casefold() == want:
            return str(value)
    return None


def _statement(step: dict, item: dict, artifact: dict) -> str:
    text = step["obligation"]
    text = text.replace("{item}", item["key"])
    text = text.replace("{artifact}", artifact["path"])
    text = text.replace("{step}", step["key"])

    def one(m):
        value = _cell(item, m.group(1))
        return m.group(0) if value is None else value
    return _CELL_RE.sub(one, text)


def _content_hash(row: dict) -> str:
    from alpaca.checklist.obligation_hash import content_hash
    return content_hash(row)


def derive(step_model: dict, artifact: dict, *, op=None, session=None, operator=None) -> list:
    """One row per (applicable step, item). No checks and no filtering of the population:
    every pair is emitted so the partition check downstream sees the whole surface. The
    rows are hash-chained in derivation order."""
    phase = step_model.get("model_id")
    steps = _applicable_steps(step_model, artifact)
    art_path = artifact["path"]
    art_sha = artifact["sha256_raw"]
    rows = []
    prev = GENESIS
    for step in steps:
        rb = step.get("report_back") or {}
        for item in artifact["items"]:
            rid = row_id(step["key"], art_path, item["key"], art_sha)
            row = {
                "id": rid,
                "kind": "item",
                "op": op,
                "phase": phase,
                "step": step["key"],
                "item": item["key"],
                "artifact": art_path,
                "statement": _statement(step, item, artifact),
                "proof": "local:%s:%d" % (art_path, item["line"]),
                "where": rb.get("where", ""),
                "how": rb.get("how", ""),
                "when": rb.get("when", ""),
                "why": "",                 # design phase (M2.6) fills the decision-page pointer
                "session": session,
                "operator": operator,
                "status": "open",
                "tag": SPECCED,
                "supersedes": None,
            }
            row["content_hash"] = _content_hash(row)
            row["prev_hash"] = prev
            prev = row["content_hash"]
            rows.append(row)
    return rows


def _verify_partition(rows: list, step_model: dict, artifact: dict) -> tuple:
    """Re-derive the (step, item) population and check the staged rows biject with it.

    Returns (verdict_code, findings). An empty population is BLOCKED, never a pass. A
    duplicated, uncovered or unexpected pair is a FAIL: a reported count is not a partition.
    """
    findings = []
    steps = _applicable_steps(step_model, artifact)
    items = artifact["items"]
    expected = set((s["key"], it["key"]) for s in steps for it in items)

    if not expected:
        findings.append((verdict.BLOCKED, "POPULATION-EMPTY",
                         "no (step, item) pair to synthesize; an empty population is never a pass"))
        return verdict.BLOCKED, findings

    observed = [(r["step"], r["item"]) for r in rows]
    counts = {}
    for pair in observed:
        counts[pair] = counts.get(pair, 0) + 1
    for pair in sorted(p for p in counts if counts[p] > 1):
        findings.append((verdict.FAIL, "PARTITION-ROW-DUPLICATED",
                         "%r appears %d times; a partition has each member exactly once"
                         % (pair, counts[pair])))
    for pair in sorted(expected - set(observed)):
        findings.append((verdict.FAIL, "PARTITION-ITEM-UNCOVERED",
                         "obligation %r exists in the population and has no row" % (pair,)))
    for pair in sorted(set(observed) - expected):
        findings.append((verdict.FAIL, "PARTITION-ROW-UNEXPECTED",
                         "row %r traces to no (step, item) in the population" % (pair,)))

    codes = [f[0] for f in findings]
    return (contract.worst(codes) if codes else verdict.PASS), findings


def _check_floor(rows: list) -> list:
    """The row-schema statement floor: a checkable claim, not a stub."""
    findings = []
    for r in rows:
        stmt = r["statement"]
        alnum = sum(1 for ch in stmt if ch.isalnum())
        words = len(stmt.split())
        if alnum < STATEMENT_MIN_ALNUM or words < STATEMENT_MIN_WORDS:
            findings.append((verdict.FAIL, "STATEMENT-BELOW-FLOOR",
                             "row %s statement %r has %d alnum chars and %d words; the floor is "
                             "%d and %d" % (r["id"], stmt, alnum, words,
                                            STATEMENT_MIN_ALNUM, STATEMENT_MIN_WORDS)))
    return findings


# --------------------------------------------------------------------- M2.6 why-shape check
#: A filled `why` is a decision-page pointer, never prose. `decision:<id>` is the native form;
#: a `local:`/`remote:` proof-style pointer is also accepted so a page reached by path resolves.
_WHY_POINTER_PREFIXES = ("decision:", "local:", "remote:")


def why_shape(rows: list) -> list:
    """Structural check on a row's `why` (M2.6): a NON-empty `why` must be shaped as a
    decision-page pointer, not prose. Returns findings as (code, reason, detail) tuples, the
    same tuple shape the floor/partition checks use.

    An EMPTY `why` is not a shape defect here: a freshly synthesized row carries an empty `why`
    the design phase fills, and the design door's resolution check
    (`alpaca.decisions.design_door_why_code`) is what refuses an unfilled or dangling `why`. This
    check refuses only a non-empty `why` that is not shaped as a pointer, so a decision page
    written as prose in the `why` column is caught before the door has to resolve it.
    """
    findings = []
    for r in rows:
        why = str(r.get("why") or "").strip()
        if not why:
            continue
        if not any(why.startswith(p) for p in _WHY_POINTER_PREFIXES):
            findings.append((verdict.FAIL, "WHY-NOT-A-POINTER",
                             "row %s why %r is prose, not a decision-page pointer"
                             % (r.get("id"), why)))
    return findings


def stage(step_model: dict, artifact: dict, *, op=None, session=None, operator=None) -> dict:
    """Derive the rows and adjudicate WITHOUT committing.

    Returns {"verdict", "rows", "committed", "findings", "expected"}. On a PASS the rows
    are committable (`committed` is True); on any non-PASS they remain staged (`committed`
    is False) and the findings say why. Nothing here writes to the record; the bridge
    (M1.11) is what lands committed rows in the store.
    """
    rows = derive(step_model, artifact, op=op, session=session, operator=operator)
    findings = list(_check_floor(rows))                 # each: (code, reason, detail)
    _part_code, part_findings = _verify_partition(rows, step_model, artifact)
    findings.extend(part_findings)
    codes = [f[0] for f in findings]
    code = contract.worst(codes) if codes else verdict.PASS
    expected = len(set((s["key"], it["key"])
                       for s in _applicable_steps(step_model, artifact)
                       for it in artifact["items"]))
    return {"verdict": code, "rows": rows, "committed": code == verdict.PASS,
            "findings": [{"verdict": c, "code": k, "detail": d} for c, k, d in findings],
            "expected": expected}


def synthesize(step_model: dict, artifact: dict, *, op=None, session=None, operator=None) -> list:
    """One row per (step, item), committed only on a clean partition.

    Returns the committed rows on a PASS. On a non-PASS the derived rows stay staged and
    are not returned; the refusal is raised so a caller cannot mistake a defective batch
    for a committed one.
    """
    result = stage(step_model, artifact, op=op, session=session, operator=operator)
    if result["verdict"] != verdict.PASS:
        reasons = "; ".join("%s: %s" % (f["code"], f["detail"]) for f in result["findings"])
        raise Halt(result["verdict"], "SYNTHESIS-NOT-PASS",
                   "%d rows staged, not committed (%s)" % (len(result["rows"]), reasons))
    return result["rows"]


# ------------------------------------------------------------------------- CLI boundary
def main(argv=None) -> int:
    ap = verdict.make_parser(
        name=INSTRUMENT,
        description="Synthesize obligation rows from a step model and an acceptance artifact")
    ap.add_argument("--model", required=True, help="the phase step-model JSON")
    ap.add_argument("--artifact", required=True, help="the acceptance-table artifact")
    ap.add_argument("--key-column", default="item", help="header of the key column")
    a = ap.parse_args(argv)
    from alpaca.checklist import artifact as artifact_mod, step_model as sm
    try:
        model = sm.load(a.model)
        art = artifact_mod.parse(a.artifact, a.key_column)
        result = stage(model, art)
    except Halt as h:
        return verdict.emit_verdict(INSTRUMENT, h.verdict, "%s: %s" % (h.code, h.detail))
    reason = "%d rows over %d obligations (%s)" % (
        len(result["rows"]), result["expected"],
        "committed" if result["committed"] else "staged, not committed")
    return verdict.emit_verdict(INSTRUMENT, result["verdict"], reason,
                                evidence=["%s:%d" % (a.artifact, art["item_table_line"])])


if __name__ == "__main__":
    import sys
    sys.exit(main())
