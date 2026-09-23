"""Spec fidelity with a data oracle taxonomy (M1.12).

Ported from the earlier harness gates/spec_fidelity.py and adapted for Alpaca.

WHAT THIS IS
------------
Synthesis (M1.10) and the traceability closure (M1.11) prove PRESENCE: every spec item
produced a row and the register bijects with the artifact content. Neither asks the next
question -- does the derivation still SAY WHAT THE SPEC SAID, and does every item land
somewhere honest? This instrument is that total disposition: a function

    dispose(items, rows, taxonomy) -> {item_key: CONSUMED | WAIVED | PENDING | CONDITIONAL}

that partitions the RE-PARSED item population (parsed from the artifact bytes by
`artifact.parse`, never trusted from the derived rows) into exactly the four classes, so
every item lands in exactly one and the totals reconcile to the item count.

Adaptations from the earlier harness, per the plan (M1.12)
--------------------------------------------
  * The hardcoded DV oracle-class taxonomy (self-checking-DUT-only / formal-assertion /
    cosim-vs-Spike) is DELETED. The taxonomy is DATA: it is read from `project.yaml`'s
    `oracle_classes` (`load_taxonomy`) and passed in. An ABSENT taxonomy BLOCKs -- an empty
    oracle codomain is never a vacuous pass.
  * The signed `--floor` / `--enforce` advisory-vs-enforce split is DELETED. A faithfulness
    defect the instrument can adjudicate is a refusal here, not a day-one-advisory no-op.
  * The DV testplan re-parse, the GFM atomiser and the row_synthesis import are gone: the
    items arrive already parsed and the rows already synthesized, matching the M1 seams.
  * The oracle-substitution case the earlier harness was built to catch is kept, re-expressed against the
    taxonomy: an item CONSUMED by an oracle class not declared in `project.yaml` is REFUSED
    (BLOCKED), never silently disposed clean.

KEPT SELF-REFUSALS
------------------
An empty item population BLOCKs; absent rows BLOCK; a CONSUMED item the derivation dropped
BLOCKs rather than being counted clean. A refusal is surfaced through `alpaca.checklist.Halt`
with a verdict-band code, never laundered into a degraded pass.

HONEST CEILING (disclosed, never softened)
------------------------------------------
This gate verifies a STRUCTURAL disposition -- a marker classification plus an oracle-class
membership test -- not SEMANTIC equivalence of the derived criterion to the spec. Two
statements that mean the same thing but read differently are not compared here; semantic
fidelity stays with the human sign. CONSUMED is an upper bound on faithfulness, not a proof.
"""
from __future__ import annotations

import re

from alpaca import project as project_mod
from alpaca.checklist import Halt
from alpaca.gates import verdict as vc

INSTRUMENT = "spec-fidelity"

# re-exported so a caller decoding a verdict does not import the contract twice.
BLOCKED = vc.BLOCKED
PASS = vc.PASS

# --- dispositions (the total function's codomain) ---
CONSUMED, WAIVED, PENDING, CONDITIONAL = "CONSUMED", "WAIVED", "PENDING", "CONDITIONAL"
CLASSES = (CONSUMED, WAIVED, PENDING, CONDITIONAL)

# --- reason tokens: the LEADING WORD of every refusal, so a caller greps ONE token ---
T_TAXONOMY_ABSENT = "SPEC-ORACLE-TAXONOMY-ABSENT"
T_POPULATION_EMPTY = "SPEC-ITEM-POPULATION-EMPTY"
T_ROWS_ABSENT = "SPEC-ROWS-ABSENT"
T_ORACLE_UNDECLARED = "SPEC-ORACLE-UNDECLARED"
T_ITEM_DROPPED = "SPEC-ITEM-DROPPED"
T_NONRECONCILING = "SPEC-DISPOSITION-NONRECONCILING"

# The disposition markers are this instrument's OWN contract vocabulary (unlike the oracle
# taxonomy, which is campaign DATA from project.yaml). The three sets are pairwise
# non-substring so precedence never turns one marker into another; precedence resolves an
# item that carries markers of more than one class.
_WAIVER_MARKERS = ("register exclusion", "owner-attested exclusion",
                   "excluded by owner", "waived by owner")
_CONDITIONAL_MARKERS = ("standing waiver", "out of scope", "known limitation",
                        "conditional accept")
_PENDING_MARKERS = ("open question", "cannot state", "to be decided",
                    "awaiting owner", "awaiting decision")

# Acceptance-table columns that may carry the oracle class, canon-matched (casefold +
# whitespace-collapse), mirroring artifact.parse's column discipline.
_ORACLE_COLUMNS = ("oracle class", "oracle", "verification method", "oracle-class")

_WS = re.compile(r"\s+")


def _canon(s) -> str:
    if s is None:
        return ""
    return _WS.sub(" ", str(s).strip()).casefold()


def _norm_taxonomy(taxonomy) -> set:
    """The declared oracle classes, canonicalised. A dict is read by its keys. An empty or
    absent taxonomy yields the empty set, which the caller turns into a BLOCK."""
    if not taxonomy:
        return set()
    if isinstance(taxonomy, dict):
        taxonomy = list(taxonomy.keys())
    return set(c for c in (_canon(t) for t in taxonomy) if c)


def load_taxonomy(root) -> list:
    """Read the oracle taxonomy from `project.yaml`'s `oracle_classes`.

    The taxonomy is DATA (project.yaml `oracle_classes`), never compiled in. An absent or empty list is a
    refusal, not a silent empty codomain: `dispose` cannot adjudicate an oracle class against
    nothing, so BLOCK here rather than pass every item vacuously.
    """
    cfg = project_mod.load(root)
    tax = cfg.get("oracle_classes")
    if not _norm_taxonomy(tax):
        raise Halt(vc.BLOCKED, T_TAXONOMY_ABSENT,
                   "project.yaml under %r declares no non-empty oracle_classes; the oracle "
                   "taxonomy is DATA and its absence is never a vacuous pass" % root)
    return list(tax)


def _cells_text(item) -> str:
    return " ".join(str(v) for v in (item.get("cells") or {}).values())


def _oracle_class_of(item) -> str:
    """The item's declared oracle class, read from the first oracle-class column present."""
    canon_cols = {}
    for col, val in (item.get("cells") or {}).items():
        canon_cols[_canon(col)] = val
    for cand in _ORACLE_COLUMNS:
        if cand in canon_cols:
            return str(canon_cols[cand] or "").strip()
    return ""


def _marker_in(text_low, markers) -> bool:
    return any(m in text_low for m in markers)


def _class_of(item) -> str:
    """The disposition of one item from its cell markers, precedence WAIVED > CONDITIONAL >
    PENDING > CONSUMED, so every item lands in exactly one class."""
    low = _canon(_cells_text(item))
    if _marker_in(low, _WAIVER_MARKERS):
        return WAIVED
    if _marker_in(low, _CONDITIONAL_MARKERS):
        return CONDITIONAL
    if _marker_in(low, _PENDING_MARKERS):
        return PENDING
    return CONSUMED


def tally(dispositions) -> dict:
    """Count each disposition class. Every class is named (zeros included) so a caller can
    reconcile the totals to the item count without guessing which classes appeared."""
    counts = {c: 0 for c in CLASSES}
    for c in dispositions.values():
        counts[c] = counts.get(c, 0) + 1
    return counts


def dispose(items, rows, taxonomy) -> dict:
    """Total disposition of the item population into the four-class codomain.

    `items` is the re-parsed acceptance-table population (`artifact.parse(...)["items"]`),
    the un-forgeable ground truth. `rows` is the synthesized obligation rows (M1.10). `taxonomy`
    is the declared oracle classes (`load_taxonomy`). Returns an ordered {item_key: class}
    mapping with each item present exactly once.

    Refuses (Halt BLOCKED) when: the taxonomy is absent, the population is empty, rows are
    absent, a CONSUMED item names an oracle class not in the taxonomy, or a CONSUMED item has
    no derived row (the obligation was dropped). Reconciliation is asserted before returning:
    exactly one class per item and the tally summing to the item count.
    """
    tax = _norm_taxonomy(taxonomy)
    if not tax:
        raise Halt(vc.BLOCKED, T_TAXONOMY_ABSENT,
                   "no oracle taxonomy supplied; dispose cannot adjudicate an oracle class "
                   "against an empty codomain (read oracle_classes from project.yaml)")
    if rows is None:
        raise Halt(vc.BLOCKED, T_ROWS_ABSENT,
                   "no derived rows supplied; a disposition over absent rows is never a pass")
    if not items:
        raise Halt(vc.BLOCKED, T_POPULATION_EMPTY,
                   "the item population is empty; an empty measured population is never a pass")

    derived_keys = set(str(r.get("item") or "").strip() for r in rows)
    derived_keys.discard("")

    dispositions = {}
    order = []
    for it in items:
        key = it["key"]
        if key in dispositions:
            # artifact.parse already refuses duplicate item keys; a duplicate reaching here
            # would collapse a member and break the partition, so refuse rather than count once.
            raise Halt(vc.BLOCKED, T_NONRECONCILING,
                       "item key %r appears twice in the population; a partition needs distinct "
                       "members" % key)
        cls = _class_of(it)
        if cls == CONSUMED:
            oracle = _oracle_class_of(it)
            if _canon(oracle) not in tax:
                raise Halt(vc.BLOCKED, T_ORACLE_UNDECLARED,
                           "item %r is CONSUMED by oracle class %r, absent from the project.yaml "
                           "taxonomy %s; a substituted or ad-hoc oracle is never a silent pass"
                           % (key, oracle, sorted(tax)))
            if key not in derived_keys:
                raise Halt(vc.BLOCKED, T_ITEM_DROPPED,
                           "item %r is CONSUMED with no derived row and no waiver / pending / "
                           "conditional disposition; the obligation was dropped" % key)
        dispositions[key] = cls
        order.append(key)

    # reconcile: exactly one class per item, the tally sums to the item count, no double count.
    counts = tally(dispositions)
    if len(dispositions) != len(items) or sum(counts.values()) != len(items):
        raise Halt(vc.BLOCKED, T_NONRECONCILING,
                   "the disposition does not reconcile: %d classified over %d items (%s)"
                   % (len(dispositions), len(items), counts))

    # preserve population order for a stable, greppable report.
    return {k: dispositions[k] for k in order}


# ------------------------------------------------------------------------- CLI boundary
def main(argv=None) -> int:
    ap = vc.make_parser(
        name=INSTRUMENT,
        description="Dispose an acceptance artifact's items into CONSUMED / WAIVED / PENDING / "
                    "CONDITIONAL against the oracle taxonomy declared in project.yaml")
    ap.add_argument("--model", required=True, help="the phase step-model JSON")
    ap.add_argument("--artifact", required=True, help="the acceptance-table artifact")
    ap.add_argument("--key-column", default="item", help="header of the key column")
    a = ap.parse_args(argv)
    from alpaca import paths
    from alpaca.checklist import artifact as artifact_mod, step_model as sm, synthesis
    try:
        root = paths.root()
        taxonomy = load_taxonomy(root)
        model = sm.load(a.model)
        art = artifact_mod.parse(a.artifact, a.key_column)
        rows = synthesis.synthesize(model, art)
        dispositions = dispose(art["items"], rows, taxonomy)
    except Halt as h:
        return vc.emit_verdict(INSTRUMENT, h.verdict, "%s: %s" % (h.code, h.detail))
    counts = tally(dispositions)
    reason = "%d items disposed: %s" % (
        len(dispositions), ", ".join("%s=%d" % (c, counts[c]) for c in CLASSES))
    return vc.emit_verdict(INSTRUMENT, vc.PASS, reason,
                           evidence=["%s:%d" % (a.artifact, art["item_table_line"])])


if __name__ == "__main__":
    import sys
    sys.exit(main())
