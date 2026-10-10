"""FR-11 structured filters as SQL (design/rune2-memory/architecture.md 6.1, 6.2; requirements.md FR-11).

A filter narrows what retrieval may read; it never authorizes. The servable predicate
(`alpaca/wiki/servable.py`) still runs after every filter, so a filter can only remove blocks.

A note, a transcript and an Alpaca source each open with a header paragraph of `key: value` lines
(`ingest/drain.py` `_note_header`, `_transcript_doc`; `ingest/alpaca_sources.py`), which is block
0 of the document. A filter on op, task, operator, session or source kind matches a line of that
header, whole line and exact value: `op: op-8` never matches `op: op-80`. `source_kind` matches the
`kind:` line (an event kind, `proof-report`) or the `role:` line (`transcript`, `proof`, `contract`,
`decision`). Only a document the capture rendered (`docs.kind` `raw`: an event note, a transcript,
an Alpaca source) has such a header: a `wiki ingest` page or an imported document (`kind` `wiki`)
matches none of them, whatever lines its first paragraph holds, so a page cannot claim an op,
session, operator or kind scope by writing them (t-070 D-1). The bookkeeping default below reads
the same rendered headers.

`fact_kind` matches the blocks that anchor an edge with a live support claim of that kind.

The default of FR-11 leaves bookkeeping out: a heartbeat note is evidence only
(dec-5ea0e31009b7, requirements.md 6.2 item 4) and stays out of retrieval unless the caller sets
`include_bookkeeping`.

Pure SQL builders: no connection is opened here and no module of the retrieval path is imported,
so the guarded `store.read` surface stays the only caller that runs them.

Cost (op-008 t-065 s6-latency). A header predicate read as a subquery scans every first block of the
store on every statement, and one answer runs it four times per retrieval pass (the visible set, the
lexical read and the two floor reads); on a store of 8,600 documents that was about 15 ms each. When
the caller hands `block_clause` its connection (`db`), the documents whose header meets the predicate
are read once per connection state and passed to the statement as a JSON list. The set is the same
rows as the subquery, so no filter is weaker; it is read again whenever the connection or another
writer changed the file, and never kept while a transaction is open.
"""
from __future__ import annotations

import json

from ..api import FACT_KINDS, Filters

#: Event kinds a default read leaves out (dec-5ea0e31009b7).
BOOKKEEPING_KINDS = ("heartbeat",)
#: `Filters` fields that match one header line of the same name.
HEADER_FIELDS = ("op", "task", "operator", "session")

_HEADER = "char(10)||h.text||char(10)"
#: The first blocks whose header a filter reads: those of documents the capture rendered (module text).
_RENDERED = "h.doc_id IN (SELECT d.doc_id FROM docs d WHERE d.kind='raw')"


def _line(key: str, value) -> str:
    """The header line as the renderer writes it: one line, runs of whitespace collapsed."""
    return "\n%s: %s\n" % (key, " ".join(str(value).split()))


def header_doc_ids(db, condition: str, params: dict) -> str:
    """The doc_ids whose first block (alias `h`) meets `condition`, as a JSON list for `json_each`.
    Kept on the connection handle under a stamp of what can change the answer: this connection's
    own writes (`total_changes`), another writer's commit (`PRAGMA data_version`), and an open
    transaction (never kept, so a rolled-back write cannot leave a stale set behind)."""
    conn = db.conn
    key = (condition, tuple(sorted(params.items())))
    stamp = (conn.total_changes, conn.execute("PRAGMA data_version").fetchone()[0])
    held = db.__dict__.setdefault("_header_doc_ids", {})
    entry = held.get(key)
    if entry is not None and entry[0] == stamp and not conn.in_transaction:
        return entry[1]
    rows = conn.execute("SELECT h.doc_id FROM blocks h WHERE h.ordinal=0 AND %s AND %s"
                        % (_RENDERED, condition), params).fetchall()
    text = json.dumps(sorted(r[0] for r in rows))
    if not conn.in_transaction:
        held[key] = (stamp, text)
    return text


def block_clause(filters: Filters | None, alias: str = "b", db=None) -> tuple[str, dict]:
    """(sql, params): the condition a block row of `alias` must meet. `filters` None means the
    defaults, so a heartbeat note is left out; the SQL is `1` when nothing narrows. Parameter names
    start with `flt_`, so the clause composes with any other named parameter. With `db` (the
    connection the statement runs on) the header predicates read their documents once per
    connection state instead of once per statement (module docstring); without it they are
    subqueries."""
    flt = filters if filters is not None else Filters()
    positive, params = [], {}
    for name in HEADER_FIELDS:
        value = getattr(flt, name)
        if value is not None:
            params["flt_" + name] = _line(name, value)
            positive.append("instr(%s, :flt_%s)>0" % (_HEADER, name))
    if flt.source_kind is not None:
        params["flt_kind"] = _line("kind", flt.source_kind)
        params["flt_role"] = _line("role", flt.source_kind)
        positive.append("(instr(%s, :flt_kind)>0 OR instr(%s, :flt_role)>0)" % (_HEADER, _HEADER))
    clauses = []
    if positive:
        condition = " AND ".join(positive)
        if db is None:
            clauses.append("%s.doc_id IN (SELECT h.doc_id FROM blocks h WHERE h.ordinal=0 AND %s AND %s)"
                           % (alias, _RENDERED, condition))
        else:
            params["flt_docs"] = header_doc_ids(db, condition, params)
            clauses.append("%s.doc_id IN (SELECT value FROM json_each(:flt_docs))" % alias)
    if not flt.include_bookkeeping:
        marks, bk_params = [], {}
        for index, kind in enumerate(BOOKKEEPING_KINDS):
            bk_params["flt_bk%d" % index] = _line("kind", kind)
            marks.append("instr(%s, :flt_bk%d)>0" % (_HEADER, index))
        condition = " OR ".join(marks)
        if db is None:
            params.update(bk_params)
            clauses.append("%s.doc_id NOT IN (SELECT h.doc_id FROM blocks h WHERE h.ordinal=0 AND %s AND (%s))"
                           % (alias, _RENDERED, condition))
        else:
            params["flt_bkdocs"] = header_doc_ids(db, "(%s)" % condition, bk_params)
            clauses.append("%s.doc_id NOT IN (SELECT value FROM json_each(:flt_bkdocs))" % alias)
    if flt.fact_kind is not None:
        params["flt_fact"] = flt.fact_kind
        clauses.append("%s.block_id IN (SELECT e.source_block_id FROM edges e "
                       "JOIN edge_origin o ON o.edge_id=e.edge_id "
                       "WHERE o.fact_kind=:flt_fact AND o.status='live')" % alias)
    return (" AND ".join(clauses) if clauses else "1"), params


__all__ = ["BOOKKEEPING_KINDS", "HEADER_FIELDS", "block_clause", "header_doc_ids"]
