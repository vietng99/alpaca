"""M2.14 proof: the session drain (SessionEnd + PreCompact) and its idempotency.

Done-when properties proved here, on BOTH a positive and a negative path:

  1. a session that ENDS lands its events AND its transcript into the wiki with no human step
     (drain.run, and the SessionEnd hook wiring that calls it);
  2. a session that COMPACTS lands the same, through the PreCompact hook under the M1.5 fail-open
     contract;
  3. re-running the drain over the same session writes NOTHING new (absorb's hashgate is idempotent:
     the second pass ingests zero blocks and the doc/block counts do not move);
  4. NEGATIVE guards: a drain with no transcript still lands the events (never crashes on the
     missing file), and a drain of a session with no events lands nothing and does not raise.

providers.registry is the M2.16 sibling (not vendored yet); a deterministic stand-in lets absorb
import and run so the drain exercises the real vendored write path. store.vec is present, but the
M2.14 write path carries no vector side-write, so the stub embedder is only a name here.
"""
import hashlib
import io
import json
import math
import os
import re
import sys
import types

import pytest

# ---- providers.registry stand-in (M2.16 sibling), installed before drain/absorb import ----------
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
from alpaca import paths              # noqa: E402
from alpaca.wiki.config import Config  # noqa: E402
from alpaca.wiki.ingest import drain   # noqa: E402
from alpaca.wiki.store.db import DB     # noqa: E402


# ------------------------------------------------------------------------- helpers
def _seed(root, session="s1", op="op1", with_result=True):
    conn = tedb.connect(root)
    tedb.append_event(conn, session=session, actor="agent", kind="task-claim", op=op,
                      ref="t1", data={"statement": "build the widget"})
    if with_result:
        tedb.append_event(conn, session=session, actor="agent", kind="result", op=op,
                          ref="t1", data={"body": "the widget is built"})
    return conn


def _write_transcript(root, session="s1"):
    d = paths.transcript_dir(root)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, "%s.jsonl" % session)
    lines = [
        {"type": "custom-title", "customTitle": "Widget work"},
        {"type": "user", "timestamp": "2020-01-01T00:00:00Z",
         "message": {"content": "Please build the widget end to end"}},
        {"type": "assistant", "timestamp": "2020-01-01T00:01:00Z",
         "message": {"model": "opus", "usage": {"input_tokens": 5, "output_tokens": 5},
                     "content": [{"type": "text", "text": "on it"}]}},
    ]
    with open(path, "w", encoding="utf-8") as fh:
        for r in lines:
            fh.write(json.dumps(r) + "\n")
    return path


def _wiki(root):
    return DB(Config.for_vault(drain.wiki_vault_dir(root)))


def _docs(root):
    db = _wiki(root)
    try:
        return [r["doc_id"] for r in db.conn.execute(
            "SELECT doc_id FROM docs WHERE kind='raw' ORDER BY doc_id")]
    finally:
        db.close()


def _counts(root):
    db = _wiki(root)
    try:
        d = db.conn.execute("SELECT COUNT(*) FROM docs").fetchone()[0]
        b = db.conn.execute("SELECT COUNT(*) FROM blocks").fetchone()[0]
        return d, b
    finally:
        db.close()


def _invoke_hook(main, payload, monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    with pytest.raises(SystemExit) as ei:
        main()
    assert ei.value.code == 0


# ------------------------------------------------------------------- (1) drain on SessionEnd
def test_drain_lands_events_and_transcript(project):
    _seed(project)
    _write_transcript(project)
    summary = drain.run(project, "s1")
    assert summary["notes"] >= 3          # two event notes + one transcript note
    assert summary["ingested"] >= 3

    docs = _docs(project)
    assert any(d.endswith("intent.md") for d in docs), docs
    assert any(d.endswith("result.md") for d in docs), docs
    assert any("transcript-s1" in d for d in docs), "the transcript must land in the wiki"

    # the intent note carries its op and role back for sort to read.
    db = _wiki(project)
    try:
        intent = next(d for d in docs if d.endswith("intent.md"))
        text = db.conn.execute(
            "SELECT text FROM blocks WHERE doc_id=? ORDER BY ordinal LIMIT 1", (intent,)
        ).fetchone()["text"]
    finally:
        db.close()
    assert "op: op1" in text and "role: intent" in text


def test_session_end_hook_drains(project, monkeypatch):
    _seed(project)
    _write_transcript(project)
    from alpaca.hooks import session_end
    _invoke_hook(session_end.main, {"session_id": "s1", "cwd": project}, monkeypatch)
    docs = _docs(project)
    assert any(d.endswith("intent.md") for d in docs), "SessionEnd must land the drain"
    assert any("transcript-s1" in d for d in docs)


# --------------------------------------------------------------------- (2) drain on PreCompact
def test_pre_compact_hook_drains_and_exits_zero(project, monkeypatch):
    _seed(project)
    from alpaca.hooks import pre_compact
    _invoke_hook(pre_compact.main, {"session_id": "s1", "cwd": project, "trigger": "auto"},
                 monkeypatch)
    docs = _docs(project)
    assert any(d.endswith("intent.md") for d in docs), "PreCompact must land the drain"
    # the hook also records the pre-compact boundary on the record.
    conn = tedb.connect(project)
    kinds = {e["kind"] for e in tedb.events(conn, session="s1")}
    assert "pre-compact" in kinds


# --------------------------------------------------------------------- (3) idempotent re-drain
def test_redrain_writes_nothing_new(project):
    _seed(project)
    _write_transcript(project)
    first = drain.run(project, "s1")
    before = _counts(project)

    second = drain.run(project, "s1")
    after = _counts(project)

    assert first["ingested"] >= 3
    assert second["ingested"] == 0, "a re-drain must ingest nothing new"
    assert second["skipped"] == second["notes"] >= 3
    assert before == after, "doc/block counts must not move on a re-drain"


# --------------------------------------------------------------------- (4) negative guards
def test_drain_without_transcript_still_lands_events(project):
    _seed(project)                          # no transcript file written
    summary = drain.run(project, "s1")
    assert summary["notes"] == 2            # the two event notes only
    docs = _docs(project)
    assert not any("transcript" in d for d in docs)
    assert any(d.endswith("intent.md") for d in docs)


def test_drain_of_empty_session_is_a_clean_no_op(project):
    tedb.connect(project)                   # a live db, but no events for this session
    summary = drain.run(project, "ghost")
    assert summary["notes"] == 0
    assert summary["ingested"] == 0


if __name__ == "__main__":
    import pytest as _p
    raise SystemExit(_p.main([__file__, "-q"]))


def test_a_transcript_page_that_shrinks_after_a_parser_change_still_drains(project, monkeypatch):
    """A transcript page is a projection of an append-only transcript. When a parser repair
    makes it shorter (duplicate prompts dropped, say), the drain records why instead of failing
    the whole sync, and the vault file follows what the wiki accepted."""
    from alpaca.analytics import parse_session
    _seed(project)
    _write_transcript(project)
    real = parse_session.parse
    many = [{"text": "prompt number %d with enough words to count as a body line" % n} for n in range(40)]
    monkeypatch.setattr(parse_session, "parse", lambda path: dict(real(path), human_prompts=many))
    drain.run(project, "s1")
    monkeypatch.setattr(parse_session, "parse", lambda path: dict(real(path), human_prompts=many[:3]))
    summary = drain.run(project, "s1")
    doc = next(d for d in summary["docs"] if "transcript-s1" in d)
    text = open(os.path.join(drain.wiki_vault_dir(project), doc), encoding="utf-8").read()
    assert text.count("\n- ") == 3
    assert "-summary-" in doc
    assert len([d for d in _docs(project) if "transcript-s1" in d]) == 2


def test_summary_revision_preserves_previous_raw_document(project, monkeypatch):
    from pathlib import Path
    from alpaca.analytics import parse_session
    _seed(project)
    _write_transcript(project)
    many = [{"text": "historical prompt %d with meaningful source detail" % n} for n in range(40)]
    monkeypatch.setattr(parse_session, "parse", lambda path: {"human_prompts": many})
    first = drain.run(project, "s1")
    old = next(d for d in first["docs"] if "transcript-s1" in d)
    before = Path(drain.wiki_vault_dir(project), old).read_bytes()
    monkeypatch.setattr(parse_session, "parse", lambda path: {"human_prompts": many[:1]})
    second = drain.run(project, "s1")
    new = next(d for d in second["docs"] if "transcript-s1" in d)
    assert new != old
    assert Path(drain.wiki_vault_dir(project), old).read_bytes() == before
    assert old in _docs(project) and new in _docs(project)
    assert drain.run(project, "s1")["ingested"] == 0


def test_rejected_event_projection_keeps_previous_raw_file(project, monkeypatch):
    from pathlib import Path
    from alpaca.wiki.ingest.absorb import Absorber
    _seed(project)
    result = drain.run(project, "s1")
    path = Path(drain.wiki_vault_dir(project), result["docs"][0])
    before = path.read_bytes()
    original = drain._note_doc
    monkeypatch.setattr(drain, "_note_doc", lambda row, **kw: (original(row, **kw)[0], "replacement"))
    def refuse(*args, **kwargs):
        raise RuntimeError("injected rejection")
    monkeypatch.setattr(Absorber, "absorb_text", refuse)
    with pytest.raises(RuntimeError, match="injected rejection"):
        drain.run(project, "s1")
    assert path.read_bytes() == before
