"""ingest.drain (Alpaca-native, no upstream): the session drain that feeds the vendored write path.

The drain reads one session's record events and its transcript out of the Alpaca store and lands them
as raw markdown notes in the wiki vault, then hands each note to the vendored write path
(ingest.absorb). It is the SOURCE in front of absorb, never a second write door.

Capture is DUMB (M2.14 Step 3): the drain assigns a note's role by a fixed mechanical lookup over
the event kind and NEVER judges the pairing of an intent with its result - that is `alpaca sort`'s job.
Re-running the drain over the same session is a no-op: the note bytes are identical, so absorb's
hashgate skips every unchanged block (idempotent), and the disk write is skipped when unchanged.

The note carries its op, role and pairing key in a plain first paragraph (`key: value` lines) so
`alpaca sort` can read them back off the ingested block without a second schema. No wikilinks are
emitted, so a captured note mints no graph edges of its own.
"""
from __future__ import annotations

import os
import json
import re

from alpaca import db as tedb
from alpaca import paths

# A note's role is a MECHANICAL lookup on the event kind, not a judgment (Step 3: capture is dumb).
# Everything the session emitted is landed; only the intent/result split is pre-labelled here, and
# even that is a fixed table, never a decision about a specific event.
INTENT_KINDS = frozenset({
    "intent", "task-add", "task-claim", "claim", "handoff", "question",
    "phase-enter", "row-open", "op-open", "op-new",
})
RESULT_KINDS = frozenset({
    "result", "task-done", "row-done", "run", "verdict", "phase-gate", "op-close",
})


def wiki_vault_dir(root: str) -> str:
    """The wiki vault for an Alpaca project lives under the runtime dir, beside alpaca.db."""
    return os.path.join(paths.runtime_dir(root), "wiki")


def _slug(value, fallback: str = "none") -> str:
    out = re.sub(r"[^A-Za-z0-9_-]+", "-", str(value or "")).strip("-")
    return out or fallback


def _role_of(kind: str) -> str:
    if kind in INTENT_KINDS:
        return "intent"
    if kind in RESULT_KINDS:
        return "result"
    return "note"


def _pair_key(ev: dict) -> str:
    data = ev.get("data") or {}
    for candidate in (ev.get("ref"), data.get("row"), data.get("task"),
                      data.get("id"), data.get("op"), ev.get("op")):
        if candidate:
            return str(candidate)
    return ""


def _summary(ev: dict) -> str:
    """A plain one-line rendering of the event. Deliberately free of wikilinks so a note mints no
    edges; sort, not capture, writes the assertions."""
    data = ev.get("data") or {}
    bits = ["%s during session %s" % (ev.get("kind", "event"), ev.get("session", ""))]
    for k in ("reason", "statement", "body", "verdict", "note", "detail", "error"):
        v = data.get(k)
        if v:
            bits.append(str(v).replace("\n", " ").strip())
    return ". ".join(b for b in bits if b)


def observed_prefixes(root) -> tuple:
    """Kind prefixes whose event data the wiki keeps verbatim as untrusted JSON: the domain
    profile's observation kinds (alpaca/profile.py `events`). Empty without a profile."""
    from alpaca import profile
    try:
        return tuple(profile.listed(profile.load(root).events(), "observation_prefixes"))
    except Exception:
        return ()


def _note_doc(ev: dict, prefixes=()):
    op = ev.get("op") or "unassigned"
    role = _role_of(ev.get("kind"))
    seq = int(ev.get("id") or 0)
    header = [
        "op: %s" % op,
        "role: %s" % role,
        "key: %s" % _pair_key(ev),
        "kind: %s" % (ev.get("kind") or ""),
        "event: %s" % seq,
        "ts: %s" % (ev.get("ts") or ""),
    ]
    text = "\n".join(header) + "\n\n" + _summary(ev) + "\n"
    if prefixes and (ev.get('kind') or '').startswith(tuple(prefixes)):
        # A profile observation: preserve its receipt/job/source pointers. JSON is untrusted
        # observation data, never instructions and never an automatically admitted lesson.
        text += '\nUntrusted observation data:\n\n```json\n' + json.dumps(
            ev.get('data') or {}, sort_keys=True, ensure_ascii=True) + '\n```\n'
    doc_id = "raw/%s/%06d-%s.md" % (_slug(op), seq, role)
    return doc_id, text


def _transcript_path(root: str, session: str, conn, override: str | None) -> str | None:
    if override and os.path.isfile(override):
        return override
    row = tedb.rows(conn, "sessions", "sid=?", (session,))
    if row and row[0].get("transcript") and os.path.isfile(row[0]["transcript"]):
        return row[0]["transcript"]
    cand = os.path.join(paths.transcript_dir(root), "%s.jsonl" % session)
    return cand if os.path.isfile(cand) else None


def _transcript_doc(root: str, session: str, op: str, conn, override: str | None):
    tp = _transcript_path(root, session, conn, override)
    if not tp:
        return None
    try:
        from alpaca.analytics import parse_session
        summary = parse_session.parse(tp)
    except Exception:
        return None
    lines = ["role: transcript", "op: %s" % op, "session: %s" % session, ""]
    if summary.get("title"):
        lines.append("# %s" % str(summary["title"]).replace("\n", " ").strip())
        lines.append("")
    for prompt in summary.get("human_prompts") or []:
        txt = (prompt.get("text") or "").replace("\n", " ").strip()
        if txt:
            lines.append("- %s" % txt)
    # Derived summaries may shrink after parser repairs or source replacement. Keep
    # each distinct summary immutable instead of waiving the knowledge shrink gate.
    import hashlib
    text = "\n".join(lines) + "\n"
    version = hashlib.sha256(text.encode("utf-8")).hexdigest()
    doc_id = "raw/%s/transcript-%s-summary-%s.md" % (_slug(op), _slug(session), version)
    return doc_id, text


def _write_if_changed(path: str, text: str) -> bool:
    """Write only when the bytes differ, so a re-drain touches nothing on disk. Returns True on a
    real write."""
    data = text.encode("utf-8")
    try:
        with open(path, "rb") as fh:
            if fh.read() == data:
                return False
    except OSError:
        pass
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return True


def run(root: str, session: str, *, clock=None, transcript_path: str | None = None,
        through_event: int | None = None, absorber=None) -> dict:
    """Land one session's events and transcript into the wiki, idempotently.

    Returns a summary: how many notes were landed, how many were freshly ingested versus skipped by
    the hashgate, and the doc ids. A second call over the same session ingests nothing new.
    Recovery may lend an absorber to reuse its index across sessions; the caller owns it.
    """
    vault = wiki_vault_dir(root)
    raw_dir = os.path.join(vault, "raw")
    os.makedirs(raw_dir, exist_ok=True)

    conn = tedb.connect(root)
    try:
        query = "SELECT * FROM events WHERE session=?"
        params = [session]
        if through_event is not None:
            query += " AND id<=?"
            params.append(through_event)
        events = [dict(row) for row in conn.execute(query + " ORDER BY id", params)]
        for event in events:
            event['data'] = json.loads(event['data'])
        prefixes = observed_prefixes(root)
        docs = [_note_doc(ev, prefixes=prefixes) for ev in events]
        op = next((ev["op"] for ev in events if ev.get("op")), "session")
        td = _transcript_doc(root, session, op, conn, transcript_path)
    finally:
        conn.close()
    if td:
        docs.append(td)

    from alpaca.wiki.config import Config
    from alpaca.wiki.ingest.absorb import Absorber

    cfg = Config.for_vault(vault)
    kwargs = {"clock": clock} if clock is not None else {}
    owns_absorber = absorber is None
    if owns_absorber:
        absorber = Absorber(cfg, **kwargs)
    elif absorber.cfg.vault_dir.resolve() != cfg.vault_dir.resolve():
        raise ValueError("recovery absorber belongs to another vault")
    summary = {"session": session, "notes": 0, "ingested": 0, "skipped": 0, "docs": [],
               "through_event": max((event['id'] for event in events), default=0)}
    try:
        for doc_id, text in docs:
            fpath = os.path.join(vault, doc_id)
            os.makedirs(os.path.dirname(fpath), exist_ok=True)
            # The vault file follows the database: a page the absorb refuses is not written, so the
            # file on disk never disagrees with what the wiki holds.
            result = absorber.absorb_text(doc_id, text, kind="raw", path=doc_id)
            _write_if_changed(fpath, text)
            summary["notes"] += 1
            summary["docs"].append(doc_id)
            if result.get("skipped"):
                summary["skipped"] += 1
            else:
                summary["ingested"] += 1
    finally:
        if owns_absorber:
            absorber.close()
    return summary
