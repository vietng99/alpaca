"""The traceability closure (R2): register <-> artifact-content bijection, plus store presence.

Ported from the earlier harness gates/traceability_closure.py and adapted for Alpaca.

WHY THIS EXISTS
---------------
The completeness proof must catch a requirement the derivation DROPPED. Every artifact
DOWNSTREAM of the derivation (the landed rows, the store) can be forged to launder a drop.
The only un-forgeable ground truth is the ARTIFACT CONTENT itself, so the covered-set is
RE-DERIVED from the parsed artifact (its items, parsed from the artifact bytes) and NOT read
back from the store. To make a requirement K "covered" you must put K back into the
artifact, which is the correct behaviour.

  LEFT-SET   L = the requirement keys of an independent register (trusted input).
  RIGHT-SET  R = the item keys parsed from the artifact CONTENT (re-derived here, not read
               from the store).
  PRESENCE   every canonical row id derivable from (an observed step, the artifact, an item)
             is present exactly once in the `rows` store (the bridge landed it, and it was
             not collapsed last-wins over a duplicate id).
  BIJECTION  L == R, both directions: a register requirement absent from the artifact content
             is SPEC-ITEM-WITHOUT-ROW; an artifact item absent from the register is
             ROW-WITHOUT-SPEC-ITEM. An empty register or an empty artifact population is
             BLOCKED, never a pass.

Adaptations from the earlier harness, per the plan (M1.11):

  * `check(conn, register, artifact)`: the register is an already-loaded collection of
    requirement keys and the artifact is already parsed (`artifact.parse`), so the earlier harness's
    file / pack / model plumbing does not live here. The canonical row ids are recomputed
    with `synthesis.row_id`, the single definition of a row id in this tree.
  * The sign / signature fields and the signature guard are gone; the store is the record's
    `rows` table rather than a signed markdown store.

HONEST LIMIT: the store is checked one-way (presence + uniqueness of the canonical rows). A
rogue store row that is not a canonical row for THIS artifact cannot add coverage -- R is
parsed from the artifact bytes -- so it is not flagged; the bijection proven is L (register)
== R (artifact content), never a store-side bijection.
"""
from __future__ import annotations

from alpaca import db
from alpaca.checklist import synthesis
from alpaca.gates import contract, verdict

INSTRUMENT = "traceability-closure"


def _finding(code: str, detail: str, vcode: int) -> dict:
    return {"verdict": vcode, "code": code, "detail": detail}


def _result(vcode: int, findings: list, info: dict) -> dict:
    return {"verdict": vcode, "findings": findings, "info": info}


def _register_keys(register) -> set:
    """The register's requirement keys, re-derived independently of the store. Accepts a set
    or list of strings, or a list of objects each carrying a `req_key`."""
    keys = set()
    for entry in (register or []):
        if isinstance(entry, str):
            k = entry.strip()
        elif isinstance(entry, dict):
            k = str(entry.get("req_key", "")).strip()
        else:
            k = str(entry).strip()
        if k:
            keys.add(k)
    return keys


def check(conn, register, artifact) -> dict:
    """Prove the register <-> artifact-content bijection and the store presence of every
    canonical row. Returns {"verdict", "findings", "info"}.

    BLOCKED on an empty register, an empty artifact population, a register requirement absent
    from the artifact content, an artifact item absent from the register, or a canonical row
    missing from the store. FAIL on a duplicated canonical row id in the store. PASS only when
    L == R and every canonical row is present exactly once.
    """
    left = _register_keys(register)
    right = set(artifact.get("keys") or [])
    info = {"register_keys": len(left), "artifact_items": len(right)}

    if not left:
        return _result(verdict.BLOCKED,
                       [_finding("CLOSURE-REGISTER-EMPTY",
                                 "the register enumerates no requirement; an empty register is "
                                 "never a pass", verdict.BLOCKED)], info)
    if not right:
        return _result(verdict.BLOCKED,
                       [_finding("CLOSURE-ARTIFACT-ITEMS-EMPTY",
                                 "the artifact yields no item to measure; an empty population is "
                                 "never a pass", verdict.BLOCKED)], info)

    path = artifact["path"]
    sha = artifact["sha256_raw"]

    # Associate the store's rows with THIS artifact's items by recomputing the canonical row
    # id from each store row's own step column crossed with the artifact keys. A store row
    # that matches no (its step, an artifact item) is out of scope for this artifact and is
    # ignored (a rogue row cannot add coverage). This reads the store only for PRESENCE; the
    # covered-set R above was re-derived from the artifact bytes, not from the store.
    by_key = {}                       # item key -> list of store ids covering it
    for srow in db.rows(conn, "rows"):
        sid = srow["id"]
        step = srow["step"]
        if step is None:
            continue
        for key in right:
            if sid == synthesis.row_id(step, path, key, sha):
                by_key.setdefault(key, []).append(sid)
                break

    findings = []

    # store presence + uniqueness of the canonical rows.
    not_in_store = sorted(k for k in right if k not in by_key)
    if not_in_store:
        findings.append(_finding(
            "CLOSURE-ROW-NOT-IN-STORE",
            "%d artifact item(s) have no canonical row in the store (%s); run the bridge first"
            % (len(not_in_store), not_in_store[:5]), verdict.BLOCKED))
    duplicated = sorted(k for k, ids in by_key.items() if len(set(ids)) != len(ids))
    if duplicated:
        findings.append(_finding(
            "CLOSURE-STORE-DUPLICATE-ROW-ID",
            "%d item(s) carry a non-unique canonical row id in the store (%s); a duplicate id is "
            "never de-duplicated into coverage" % (len(duplicated), duplicated[:5]), verdict.FAIL))

    # the L == R bijection, both directions.
    uncovered = sorted(left - right)
    if uncovered:
        findings.append(_finding(
            "SPEC-ITEM-WITHOUT-ROW",
            "%d register requirement(s) are absent from the artifact content (%s); a dropped "
            "requirement is caught here because the covered-set is re-parsed from the artifact "
            "bytes" % (len(uncovered), uncovered[:5]), verdict.BLOCKED))
    orphan = sorted(right - left)
    if orphan:
        findings.append(_finding(
            "ROW-WITHOUT-SPEC-ITEM",
            "%d artifact item(s) trace to no register requirement (%s)"
            % (len(orphan), orphan[:5]), verdict.BLOCKED))

    info.update({"not_in_store": not_in_store, "uncovered": uncovered, "orphan": orphan,
                 "duplicated": duplicated})

    if findings:
        # BLOCKED dominates FAIL in the fold.
        return _result(contract.worst([f["verdict"] for f in findings]), findings, info)
    return _result(verdict.PASS, [], info)
