"""alpaca sort (Alpaca-native): the L2 historian pass over the drained raw layer.

The drain (alpaca.wiki.ingest.drain) captures dumbly - every session event becomes a raw note with a
mechanical role label and zero judgment. `alpaca sort` is the ONE pass that judges: it groups the raw
layer by op and pairs each intent note with its result note, writing that judgment as assertions
that carry pointers back to the notes. An intent with no result is SURFACED as an unpaired
assertion, never dropped (spec 5.9; absorb-gap AG-A1 "capture may never judge").

Cadence and the never-drop unsorted bucket are M4.6's completeness proof; this task ships the
mechanism and a manual `alpaca sort` invocation.
"""
from __future__ import annotations

import os

from alpaca.gates import verdict as vc
from alpaca.wiki.ingest import drain

# The assertion store `alpaca sort` writes into the wiki db. sort lives OUTSIDE alpaca/wiki, so this is not
# a second wiki write door (the write-door guard fences alpaca/wiki modules, not this historian pass).
_SCHEMA = """
CREATE TABLE IF NOT EXISTS sort_assertion (
  assertion_id TEXT PRIMARY KEY,
  day TEXT, op TEXT NOT NULL,
  intent_doc TEXT NOT NULL, intent_pointer TEXT,
  result_doc TEXT, result_pointer TEXT,
  status TEXT NOT NULL, ts TEXT);
"""


def _parse_meta(text: str) -> dict:
    """Read the `key: value` header paragraph a drained note carries as its first block."""
    meta = {}
    for line in (text or "").splitlines():
        if ":" not in line:
            break
        key, _, value = line.partition(":")
        key = key.strip()
        if not key or " " in key:
            break
        meta[key] = value.strip()
    return meta


def _raw_notes(db, day):
    """Every raw note as (meta, doc_id, pointer), read off its first ingested block."""
    rows = db.conn.execute(
        "SELECT b.doc_id AS doc_id, b.block_content_id AS pointer, b.text AS text "
        "FROM blocks b "
        "JOIN (SELECT doc_id, MIN(ordinal) AS mo FROM blocks WHERE status='active' "
        "      GROUP BY doc_id) m ON b.doc_id = m.doc_id AND b.ordinal = m.mo "
        "JOIN docs d ON d.doc_id = b.doc_id "
        "WHERE b.status='active' AND d.kind='raw' "
        "ORDER BY b.doc_id"
    ).fetchall()
    out = []
    for r in rows:
        meta = _parse_meta(r["text"])
        role = meta.get("role")
        if role not in ("intent", "result"):
            continue
        if day and (meta.get("ts", "")[:10] != day):
            continue
        out.append((meta, r["doc_id"], r["pointer"]))
    return out


def run(root: str, day: str | None = None) -> dict:
    """Group the raw layer by op and pair intents with results.

    Returns {"ops": {op: {"paired": [...], "unpaired": [...]}}, "assertions": [...],
             "paired": n, "unpaired": n}. Idempotent: re-running replaces the same assertion rows.
    """
    vault = drain.wiki_vault_dir(root)
    empty = {"ops": {}, "assertions": [], "paired": 0, "unpaired": 0}
    if not os.path.isfile(os.path.join(vault, "rune.db")):
        return empty

    from alpaca.wiki.config import Config
    from alpaca.wiki.store.db import DB

    db = DB(Config.for_vault(vault))
    try:
        notes = _raw_notes(db, day)

        by_op: dict[str, dict] = {}
        for meta, doc_id, pointer in notes:
            op = meta.get("op") or "unassigned"
            bucket = by_op.setdefault(op, {"intent": [], "result": []})
            bucket[meta["role"]].append((meta, doc_id, pointer))

        assertions = []
        ops_out: dict[str, dict] = {}
        for op in sorted(by_op):
            intents = sorted(by_op[op]["intent"], key=lambda t: (t[0].get("ts", ""), t[1]))
            results = sorted(by_op[op]["result"], key=lambda t: (t[0].get("ts", ""), t[1]))
            # a result is consumable once, matched to an intent that shares its pairing key.
            by_key: dict[str, list] = {}
            for meta, doc_id, pointer in results:
                by_key.setdefault(meta.get("key", ""), []).append((doc_id, pointer))
            paired, unpaired = [], []
            for meta, doc_id, pointer in intents:
                key = meta.get("key", "")
                pool = by_key.get(key) or []
                match = pool.pop(0) if pool else None
                if match:
                    row = {"assertion_id": "%s|%s" % (op, doc_id), "day": day or "", "op": op,
                           "intent_doc": doc_id, "intent_pointer": pointer,
                           "result_doc": match[0], "result_pointer": match[1],
                           "status": "paired", "ts": meta.get("ts", "")}
                    paired.append(row)
                else:
                    row = {"assertion_id": "%s|%s" % (op, doc_id), "day": day or "", "op": op,
                           "intent_doc": doc_id, "intent_pointer": pointer,
                           "result_doc": None, "result_pointer": None,
                           "status": "unpaired", "ts": meta.get("ts", "")}
                    unpaired.append(row)
                assertions.append(row)
            ops_out[op] = {"paired": paired, "unpaired": unpaired}

        _persist(db, assertions)
        return {"ops": ops_out, "assertions": assertions,
                "paired": sum(1 for a in assertions if a["status"] == "paired"),
                "unpaired": sum(1 for a in assertions if a["status"] == "unpaired")}
    finally:
        db.close()


def _persist(db, assertions) -> None:
    db.conn.executescript(_SCHEMA)
    for a in assertions:
        db.conn.execute(
            "INSERT INTO sort_assertion "
            "(assertion_id, day, op, intent_doc, intent_pointer, result_doc, result_pointer, "
            " status, ts) VALUES (?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(assertion_id) DO UPDATE SET "
            "day=excluded.day, op=excluded.op, intent_doc=excluded.intent_doc, "
            "intent_pointer=excluded.intent_pointer, result_doc=excluded.result_doc, "
            "result_pointer=excluded.result_pointer, status=excluded.status, ts=excluded.ts",
            (a["assertion_id"], a["day"], a["op"], a["intent_doc"], a["intent_pointer"],
             a["result_doc"], a["result_pointer"], a["status"], a["ts"]))
    db.conn.commit()


def cmd_sort(args) -> int:
    from alpaca import paths
    # M4.6 Step 4: the boundary is in code, not only in prose. `alpaca sort` surfaces intents and
    # results for a human reading and writes assertions; it REFUSES to be run as an automatic
    # classifier over a whole backlog, which would mechanise the very judgment sort exists to
    # make. The capture path already carries no classifier (alpaca.wiki.ingest.drain assigns a role by
    # a fixed mechanical lookup, never a decision); this refusal keeps the other half of the
    # boundary - the judgment - human, not a background sweep.
    if getattr(args, "auto", False):
        return vc.emit_verdict(
            "alpaca-sort", vc.BLOCKED,
            "alpaca sort refuses to mechanise the judgment: it surfaces intents and results for a "
            "reading and writes assertions, it is not an automatic classifier over a whole backlog")
    root = paths.root()
    result = run(root, day=getattr(args, "day", None))
    if getattr(args, "json", False):
        import json
        print(json.dumps(result, indent=1))
    reason = "%d op(s), %d paired, %d surfaced unpaired" % (
        len(result["ops"]), result["paired"], result["unpaired"])
    return vc.emit_verdict("alpaca-sort", vc.PASS, reason,
                           evidence=[a["assertion_id"] for a in result["assertions"]])


def _parser(sub) -> None:
    s = sub.add_parser("sort")
    s.add_argument("--day", default=None, help="restrict to notes stamped on this YYYY-MM-DD")
    s.add_argument("--json", action="store_true")
    # M4.6 Step 4: refused on sight. Named so a caller sees the boundary, and BLOCKED when passed:
    # sort surfaces for judgment, it never mechanises a backlog into assertions on its own.
    s.add_argument("--auto", action="store_true",
                   help="refused: alpaca sort surfaces for judgment and never mechanises a backlog")


def _register() -> None:
    from alpaca import cli
    cli.command("sort")(cmd_sort)
    cli.register_parser("sort", _parser)


_register()
