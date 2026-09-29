"""Explicit, idempotent replay of every recorded session to a fixed event cutoff.

This calls the normal capture/write path. It never deletes records, invents graph
assertions, or upgrades profile acceptance. A success event is appended only after
source notes, indexed documents, blocks, database integrity, and the ledger agree.
"""
from __future__ import annotations

from pathlib import Path

from alpaca import db, transcripts
from .config import Config
from .determinism import sha256_hex
from .ingest import drain
from .ingest.absorb import Absorber
from .ingest.segment import segment


def verify_documents(vault, conn, documents):
    """Verify exact raw bytes and active searchable blocks for each captured source."""
    for doc_id, text in documents:
        raw = Path(vault, doc_id)
        row = conn.execute("SELECT content_sha256 FROM docs WHERE doc_id=?", (doc_id,)).fetchone()
        expected = [(b['ordinal'], b['text'], b['block_sha256']) for b in segment(doc_id, text)]
        actual = [tuple(r) for r in conn.execute(
            "SELECT ordinal,text,block_sha256 FROM blocks WHERE doc_id=? AND status='active' "
            "ORDER BY ordinal", (doc_id,))]
        if (not raw.is_file() or raw.read_bytes() != text.encode('utf-8')
                or row is None or row[0] != sha256_hex(text) or actual != expected):
            raise RuntimeError(f"wiki recovery verification failed: {doc_id}")
    return len(documents)


def verify_events(vault, conn, events):
    """Verify both the raw bytes and the searchable projection for source events."""
    prefixes = drain.observed_prefixes(str(Path(vault).resolve().parent.parent))
    return verify_documents(vault, conn, [drain._note_doc(event, prefixes=prefixes) for event in events])


def run(root: str, *, session: str, progress=None) -> dict:
    source = db.connect(str(root))
    try:
        ok, why = db.verify_chain(source)
        if not ok:
            raise RuntimeError(f"source record verification failed: {why}")
        # One SELECT freezes the inventory; later arrivals are left for the next drain.
        events = drain.normalize_events(source, [dict(row) for row in source.execute("SELECT * FROM events ORDER BY id")])
        registered = [row[0] for row in source.execute("SELECT sid FROM sessions WHERE transcript IS NOT NULL AND transcript<>''")]
        registered += [row[0] for row in source.execute("SELECT DISTINCT session FROM obs_source WHERE kind='transcript' AND locator IS NOT NULL")]
    finally:
        source.close()
    cutoff = max((event['id'] for event in events), default=0)
    sessions = list(dict.fromkeys([event['session'] for event in events] + registered))
    cfg = Config.for_project(root)
    cfg.vault_dir.mkdir(parents=True, exist_ok=True)
    absorber = Absorber(cfg)
    report = {'status': 'ok', 'through_event': cutoff, 'sessions': len(sessions),
              'notes': 0, 'ingested': 0, 'skipped': 0}
    try:
        report['verified_transcripts'] = 0
        report['transcript_sources'] = []
        report['missing_transcripts'] = []
        for index, sid in enumerate(sessions, 1):
            try:
                result = drain.run(str(root), sid, through_event=cutoff, absorber=absorber)
            except drain.TranscriptUnavailable as exc:
                # Historical source absence must not hide this session's events or
                # prevent capture of later sessions. Coverage remains incomplete.
                source = db.connect(str(root))
                try:
                    locator = transcripts.registered(source, sid)
                finally:
                    source.close()
                report['missing_transcripts'].append({
                    'session': sid, 'source': locator, 'error': str(exc),
                })
                report['status'] = 'incomplete'
                result = drain.capture(str(root), [event for event in events if event['session'] == sid],
                                       absorber=absorber)
            for key in ('notes', 'ingested', 'skipped'):
                report[key] += result[key]
            for transcript in result['transcripts']:
                # Verify the captured source cutoff, never reread a growing native
                # transcript. The manifest hash was computed before publication.
                raw = cfg.vault_dir / transcript['doc_id']
                if not raw.is_file():
                    raise RuntimeError("wiki recovery transcript verification failed: missing projection")
                text = raw.read_bytes().decode('utf-8')
                if sha256_hex(text) != transcript['content_sha256']:
                    raise RuntimeError("wiki recovery transcript verification failed: changed projection")
                report['verified_transcripts'] += verify_documents(
                    cfg.vault_dir, absorber.db.conn, [(transcript['doc_id'], text)])
                report['transcript_sources'].append(transcript)
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
        event = db.append_event(source, session=session, actor='alpaca-wiki', kind=('wiki-recovered' if report['status'] == 'ok'
                                      else 'wiki-recovery-incomplete'), data=report)
        report['recovery_event'] = event['id']
    finally:
        source.close()
    return report
