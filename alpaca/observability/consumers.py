"""Independent projections with durable checkpoints and capped retry backoff."""
import datetime as dt
import json
import os
from pathlib import Path

from alpaca import db, util
from . import _time, issue

BOUNDARIES = {'session-start','session-end','pre-compact','session-checkpoint','turn-end','subagent-stop'}


def _wiki(root, rows):
    from alpaca.wiki.config import Config
    from alpaca.wiki.ingest.absorb import Absorber
    from alpaca.wiki.ingest.drain import wiki_vault_dir, _note_doc, _write_if_changed, observed_prefixes
    vault = Path(wiki_vault_dir(root))
    prefixes = observed_prefixes(root)
    absorber = Absorber(Config.for_vault(str(vault)))
    try:
        for row in rows:
            doc_id, text = _note_doc(row, prefixes=prefixes)
            path = vault / doc_id
            path.parent.mkdir(parents=True, exist_ok=True)
            absorber.absorb_text(doc_id, text, kind='raw', path=doc_id)
            _write_if_changed(str(path), text)
    finally:
        absorber.close()


def queue_snapshot(conn, session, *, event_id=0, observation_id=0):
    conn.execute("INSERT INTO obs_projection_job VALUES (?,'snapshot',?,?,?,'pending',0,NULL,NULL,?) "
                 "ON CONFLICT(job_key) DO UPDATE SET event_id=MAX(event_id,excluded.event_id), "
                 "observation_id=MAX(observation_id,excluded.observation_id),status='pending',updated_at=excluded.updated_at",
                 ('snapshot:'+session,session,event_id,observation_id,util.now_iso()))


def _snapshots(root, rows):
    from alpaca import transcripts
    conn = db.connect(root)
    try:
        with db.transaction(conn):
            for row in rows:
                if row['kind'] not in BOUNDARIES:
                    continue
                # Unknown/unregistered source coverage remains explicit in the registry.
                # Queue only an actual registered source; a missing file will retry.
                if transcripts.registered(conn,row['session']):
                    queue_snapshot(conn,row['session'],event_id=row['id'])
    finally:
        conn.close()


def _projection(root, rows):
    from alpaca import export
    export.write_if_changed(root)
    # Profile reconciliation is a distinct consumer; its failure cannot stop wiki progress.


def _profile(root, rows):
    """Hand the new rows to the domain profile (alpaca/profile.py `refresh`). The profile decides
    whether they concern it; a result carrying status "error" keeps the checkpoint so the rows
    are retried. A named profile that did not load, or a refresh that raises, fails the consumer
    the same way, so no row is skipped past a broken profile. The empty profile does nothing."""
    from alpaca import profile
    try:
        result = profile.strict(root, 'refresh', root, 'observability-collector', rows)
    except profile.ProfileError:
        raise RuntimeError('profile refresh failed')
    if isinstance(result, dict) and result.get('status') == 'error':
        raise RuntimeError('profile refresh failed')


CONSUMERS = {'wiki': _wiki, 'snapshot-requests': _snapshots, 'projection': _projection, 'profile': _profile}


def consume(root, conn, *, max_records=200):
    result = {}
    for name, callback in CONSUMERS.items():
        prior = conn.execute('SELECT * FROM obs_consumer_checkpoint WHERE consumer=?', (name,)).fetchone()
        prior = dict(prior) if prior else {'event_id':0,'observation_id':0,'failures':0}
        if prior.get('next_attempt') and _time(prior['next_attempt']) > _time():
            result[name] = {'status':'backoff', 'through_event':prior['event_id']}
            continue
        events = [dict(r) for r in conn.execute('SELECT * FROM events WHERE id>? ORDER BY id LIMIT ?', (prior['event_id'], max_records))]
        if not events:
            result[name] = {'status':'current', 'through_event':prior['event_id']}
            continue
        for event in events:
            event['data'] = json.loads(event['data'])
        try:
            callback(root,events)
        except Exception as exc:
            failures = prior['failures'] + 1
            error = type(exc).__name__
            row = dict(prior,consumer=name,failures=failures,error=error, updated_at=util.now_iso(),next_attempt=(_time()+dt.timedelta(seconds=min(900,5*2**min(failures,8)))).isoformat())
            with db.transaction(conn):
                db.upsert(conn,'obs_consumer_checkpoint','consumer',row)
                issue(conn,'consumer-failed',detail=name+': '+error,identity=name)
            result[name] = {'status':'failed','error':error,'through_event':prior['event_id']}
        else:
            with db.transaction(conn):
                db.upsert(conn,'obs_consumer_checkpoint','consumer',dict(consumer=name,event_id=events[-1]['id'],observation_id=prior['observation_id'],updated_at=util.now_iso(),failures=0,next_attempt=None,error=None))
                # Resolve only this consumer's issue, not unrelated failures.
                from . import _id
                conn.execute('UPDATE obs_issue SET resolved_at=? WHERE issue_id=?',(util.now_iso(),_id(None,'consumer-failed',name)))
            result[name] = {'status':'ok','through_event':events[-1]['id']}
    return result


def snapshot_observations(root, conn, *, max_records=1000):
    """Atomically queue preservation and acknowledge observations; jobs retry separately."""
    name = 'source-snapshot-requests'
    prior = conn.execute('SELECT observation_id FROM obs_consumer_checkpoint WHERE consumer=?',(name,)).fetchone()
    watermark = prior[0] if prior else 0
    records = [dict(r) for r in conn.execute('SELECT o.id,s.session,s.kind FROM obs_observation o JOIN obs_source s ON s.source_id=o.source_id WHERE o.id>? ORDER BY o.id LIMIT ?', (watermark,max_records))]
    if not records:
        return {'status':'current','through_observation':watermark}
    with db.transaction(conn):
        for record in records:
            if record['kind']=='transcript':
                queue_snapshot(conn,record['session'],observation_id=record['id'])
        db.upsert(conn,'obs_consumer_checkpoint','consumer',dict(consumer=name,event_id=0,observation_id=records[-1]['id'],updated_at=util.now_iso(),failures=0,next_attempt=None,error=None))
    return {'status':'queued','through_observation':records[-1]['id']}


def run_snapshot_jobs(root, conn, *, limit=8):
    """One unavailable source cannot prevent preservation of another session."""
    from alpaca import transcripts
    from . import _id
    jobs = [dict(r) for r in conn.execute("SELECT * FROM obs_projection_job WHERE status!='done' AND (next_attempt IS NULL OR next_attempt<=?) ORDER BY updated_at LIMIT ?", (_time().isoformat(),limit))]
    result = {'completed':0,'failed':0}
    for job in jobs:
        try:
            fact = transcripts.snapshot(root,job['session'])
            if fact.get('mode') in ('none','busy') or fact.get('source_missing'):
                raise OSError('snapshot unavailable; retry required')
            if any(row.get('mode') in ('busy','missing','truncated') for row in fact.get('registry_children',[])):
                # Each child gets its own retry job, so parent progress is independent.
                with db.transaction(conn):
                    for child in fact.get('registry_children',[]):
                        if child.get('session') and child.get('mode') in ('busy','missing'):
                            queue_snapshot(conn,child['session'],event_id=job['event_id'])
        except Exception as exc:
            failures = job['failures']+1
            with db.transaction(conn):
                conn.execute("UPDATE obs_projection_job SET status='retry',failures=?,next_attempt=?,error=?,updated_at=? WHERE job_key=?", (failures,(_time()+dt.timedelta(seconds=min(900,5*2**min(failures,8)))).isoformat(),type(exc).__name__,util.now_iso(),job['job_key']))
                issue(conn,'snapshot-failed',detail=type(exc).__name__,identity=job['job_key'])
            result['failed'] += 1
        else:
            with db.transaction(conn):
                conn.execute("UPDATE obs_projection_job SET status='done',failures=0,next_attempt=NULL,error=NULL,updated_at=? WHERE job_key=?",(util.now_iso(),job['job_key']))
                conn.execute('UPDATE obs_issue SET resolved_at=? WHERE issue_id=?',(util.now_iso(),_id(None,'snapshot-failed',job['job_key'])))
            result['completed'] += 1
    return result
