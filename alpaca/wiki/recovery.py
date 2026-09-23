"""Explicit, idempotent replay of every recorded session to a fixed event cutoff.

This calls the normal capture/write path. It never deletes records, invents graph
assertions, or upgrades profile acceptance. A success event is appended only after
source notes, indexed documents, blocks, database integrity, and the ledger agree.
"""
from __future__ import annotations

import json
from pathlib import Path

from alpaca import db
from .config import Config
from .determinism import sha256_hex
from .ingest import drain
from .ingest.absorb import Absorber
from .ingest.segment import segment


def verify_events(vault, conn, events):
    """Verify both the raw bytes and the searchable projection for source events."""
    # the vault is <root>/.alpaca/wiki, so the project root (and its profile) sits two levels up
    prefixes = drain.observed_prefixes(str(Path(vault).resolve().parent.parent))
    for event in events:
        doc_id, text = drain._note_doc(event, prefixes=prefixes)
        raw = Path(vault, doc_id)
        row = conn.execute("SELECT content_sha256 FROM docs WHERE doc_id=?", (doc_id,)).fetchone()
        expected = [(b['ordinal'], b['text'], b['block_sha256']) for b in segment(doc_id, text)]
        actual = [tuple(r) for r in conn.execute(
            "SELECT ordinal,text,block_sha256 FROM blocks WHERE doc_id=? AND status='active' "
            "ORDER BY ordinal", (doc_id,))]
        if (not raw.is_file() or raw.read_bytes() != text.encode('utf-8')
                or row is None or row[0] != sha256_hex(text) or actual != expected):
            raise RuntimeError(f"wiki recovery verification failed for event {event['id']}: {doc_id}")
    return len(events)


def run(root: str, *, session: str, progress=None) -> dict:
    source = db.connect(str(root))
    try:
        ok, why = db.verify_chain(source)
        if not ok:
            raise RuntimeError(f"source record verification failed: {why}")
        # One SELECT freezes the inventory; later arrivals are left for the next drain.
        events = [dict(row) for row in source.execute("SELECT * FROM events ORDER BY id")]
    finally:
        source.close()
    for event in events:
        event['data'] = json.loads(event['data'])
    cutoff = max((event['id'] for event in events), default=0)
    sessions = list(dict.fromkeys(event['session'] for event in events))
    cfg = Config.for_vault(drain.wiki_vault_dir(str(root)))
    cfg.vault_dir.mkdir(parents=True, exist_ok=True)
    absorber = Absorber(cfg)
    report = {'status': 'ok', 'through_event': cutoff, 'sessions': len(sessions),
              'notes': 0, 'ingested': 0, 'skipped': 0}
    try:
        for index, sid in enumerate(sessions, 1):
            result = drain.run(str(root), sid, through_event=cutoff, absorber=absorber)
            for key in ('notes', 'ingested', 'skipped'):
                report[key] += result[key]
            if progress is not None:
                progress(index, len(sessions), report.copy())
        report['verified_events'] = verify_events(cfg.vault_dir, absorber.db.conn, events)
        if [row[0] for row in absorber.db.conn.execute('PRAGMA integrity_check')] != ['ok']:
            raise RuntimeError('wiki database integrity verification failed')
        if absorber.db.conn.execute('PRAGMA foreign_key_check').fetchone() is not None:
            raise RuntimeError('wiki database foreign key verification failed')
        absorber.writer.sync_ledger()
    finally:
        absorber.close()
    source = db.connect(str(root))
    try:
        event = db.append_event(source, session=session, actor='alpaca-wiki', kind='wiki-recovered',
                                data=report)
        report['recovery_event'] = event['id']
    finally:
        source.close()
    return report
