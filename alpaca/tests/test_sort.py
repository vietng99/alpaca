"""M2.14: `alpaca sort` groups the drained raw layer by op and pairs intent notes with result notes.

Proved on BOTH paths:
  1. POSITIVE: an intent note and a result note that share an op and a pairing key are PAIRED, and
     the assertion carries pointers back to both notes;
  2. NEGATIVE (the never-drop rule): an intent with no matching result is SURFACED as an unpaired
     assertion, not dropped (capture may never judge);
  3. grouping is per-op: two ops sort into two independent buckets;
  4. sort is idempotent: a second pass replaces its own assertions rather than duplicating them;
  5. `alpaca sort` writes its judgment (capture stays dumb; sort is the only pass that judges).
"""
import hashlib
import math
import re
import sys
import types

# ---- providers.registry stand-in (M2.16 sibling) so drain -> absorb can run -----------------
_TOK = re.compile(r"\w+", re.UNICODE)


class _HashEmbedder:
    def __init__(self, dim=64):
        self.dim = dim
        self.name = "deterministic-hash-%d" % dim

    def embed(self, text):
        v = [0.0] * self.dim
        for tok in (t for t in _TOK.findall(text.lower()) if len(t) > 1):
            h = hashlib.sha256(tok.encode("utf-8")).digest()
            v[int.from_bytes(h[:4], "big") % self.dim] += 1.0 if h[4] & 1 else -1.0
        n = math.sqrt(sum(x * x for x in v))
        return [x / n for x in v] if n > 0 else v


class _NullLLM:
    name = "off"

    def extract(self, block_text, context):
        return []


class _Providers:
    def __init__(self, cfg):
        self.embedder = _HashEmbedder(int(cfg.meta.get("embed_dim", 64)))
        self.reranker = None
        self.entailer = None
        self.llm_extractor = _NullLLM()


_pp = types.ModuleType("alpaca.wiki.providers")
_pp.__path__ = []
_pr = types.ModuleType("alpaca.wiki.providers.registry")
_pr.Providers = _Providers
sys.modules.setdefault("alpaca.wiki.providers", _pp)
sys.modules.setdefault("alpaca.wiki.providers.registry", _pr)

from alpaca import db as tedb          # noqa: E402
from alpaca import sort as tesort      # noqa: E402
from alpaca.wiki.config import Config   # noqa: E402
from alpaca.wiki.ingest import drain    # noqa: E402
from alpaca.wiki.store.db import DB      # noqa: E402


def _ev(conn, kind, op, ref, session="s1", **data):
    tedb.append_event(conn, session=session, actor="agent", kind=kind, op=op, ref=ref, data=data)


def _drain(root, session="s1"):
    return drain.run(root, session)


def _assertions(root):
    db = DB(Config.for_vault(drain.wiki_vault_dir(root)))
    try:
        db.conn.execute(tesort._SCHEMA)
        return [dict(r) for r in db.conn.execute(
            "SELECT * FROM sort_assertion ORDER BY assertion_id")]
    finally:
        db.close()


# ------------------------------------------------------------------- (1) intent+result pair
def test_intent_and_result_pair_with_pointers(project):
    conn = tedb.connect(project)
    _ev(conn, "task-claim", "op1", "t1", statement="build the widget")
    _ev(conn, "result", "op1", "t1", body="the widget is built")
    _drain(project)

    out = tesort.run(project)
    assert out["paired"] == 1 and out["unpaired"] == 0
    pair = out["ops"]["op1"]["paired"][0]
    assert pair["status"] == "paired"
    assert pair["intent_doc"].endswith("intent.md")
    assert pair["result_doc"].endswith("result.md")
    # the assertion carries pointers (block content ids) back to the notes.
    assert pair["intent_pointer"] and pair["result_pointer"]


# ----------------------------------------------------------- (2) intent with no result surfaced
def test_intent_without_result_is_surfaced_not_dropped(project):
    conn = tedb.connect(project)
    _ev(conn, "task-claim", "op1", "t1", statement="paired work")
    _ev(conn, "result", "op1", "t1", body="done")
    _ev(conn, "question", "op1", "t2", body="unanswered question")   # intent, no result
    _drain(project)

    out = tesort.run(project)
    unpaired = out["ops"]["op1"]["unpaired"]
    assert len(unpaired) == 1, out["ops"]["op1"]
    assert unpaired[0]["status"] == "unpaired"
    assert unpaired[0]["result_doc"] is None
    # it is SURFACED (present), never dropped: the intent still points at its note.
    assert unpaired[0]["intent_doc"].endswith("intent.md")
    assert out["paired"] == 1 and out["unpaired"] == 1


# ------------------------------------------------------------------- (3) grouped per op
def test_notes_group_by_op(project):
    conn = tedb.connect(project)
    _ev(conn, "task-claim", "op1", "a", statement="op1 intent")
    _ev(conn, "result", "op1", "a", body="op1 result")
    _ev(conn, "task-claim", "op2", "b", statement="op2 intent")
    _drain(project)

    out = tesort.run(project)
    assert set(out["ops"]) == {"op1", "op2"}
    assert out["ops"]["op1"]["paired"] and not out["ops"]["op1"]["unpaired"]
    assert out["ops"]["op2"]["unpaired"] and not out["ops"]["op2"]["paired"]


# ------------------------------------------------------------------- (4) sort is idempotent
def test_resort_replaces_not_duplicates(project):
    conn = tedb.connect(project)
    _ev(conn, "task-claim", "op1", "t1", statement="x")
    _ev(conn, "result", "op1", "t1", body="y")
    _drain(project)

    tesort.run(project)
    first = _assertions(project)
    tesort.run(project)
    second = _assertions(project)
    assert len(first) == len(second) == 1
    assert first[0]["assertion_id"] == second[0]["assertion_id"]


# ------------------------------------------------------------------- (5) empty layer no-op
def test_sort_with_no_raw_layer_is_empty(project):
    out = tesort.run(project)
    assert out == {"ops": {}, "assertions": [], "paired": 0, "unpaired": 0}


if __name__ == "__main__":
    import pytest as _p
    raise SystemExit(_p.main([__file__, "-q"]))
