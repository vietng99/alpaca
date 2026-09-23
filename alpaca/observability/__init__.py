"""Durable source registry and bounded, replayable local collection.

The record holds cursors and sanitized facts atomically. Authorized native files retain
payloads; this catalog never stores prompts, tool arguments, results or private reasoning.
Consumers checkpoint separately and may retry without holding up source ingestion.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import math
import os
import re
from pathlib import Path
import sqlite3
import stat
import time
import uuid
import socket

from alpaca import db, util

MAX_LINE = 4 * 1024 * 1024
EMPTY_HASH = hashlib.sha256(b'').hexdigest()


def _id(*parts):
    return hashlib.sha256(json.dumps(parts, sort_keys=True).encode()).hexdigest()[:32]


def _time(value=None):
    if value is None:
        return dt.datetime.now(dt.timezone.utc)
    if isinstance(value, str):
        value = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
    return value if value.tzinfo else value.replace(tzinfo=dt.timezone.utc)


def enabled(root):
    return (Path(root) / '.alpaca/observability/enabled').is_file()


def expect_source(root, session, provider, *, locator=None, native_id=None,
                  parent_session=None, capabilities=None, required=True, closed=False,
                  kind='transcript'):
    """Register an explicit expectation; missing locators remain missing, never PASS."""
    locator = str(Path(locator).expanduser().resolve()) if locator else None
    source_id = _id(session, provider, kind)
    now = util.now_iso()
    conn = db.connect(root)
    try:
        with db.transaction(conn):
            prior = conn.execute('SELECT * FROM obs_source WHERE source_id=?', (source_id,)).fetchone()
            row = dict(prior) if prior else {}
            prior_capabilities = json.loads(row.get('capabilities', '{}'))
            merged_capabilities = dict(prior_capabilities)
            if capabilities is not None:
                merged_capabilities.update(capabilities)
            if locator is not None:
                merged_capabilities.pop('restore_disabled', None)
            elif prior_capabilities.get('restore_disabled'):
                merged_capabilities['restore_disabled'] = True
            row.update(source_id=source_id, session=session, provider=provider, kind=kind,
                       native_id=native_id or row.get('native_id'), parent_session=parent_session or row.get('parent_session'),
                       locator=locator or row.get('locator'),
                       capabilities=json.dumps(merged_capabilities),
                       required=int(required), closed=int(closed), created_at=row.get('created_at', now), updated_at=now)
            db.upsert(conn, 'obs_source', 'source_id', row)
        return row
    finally:
        conn.close()


def _table(conn, name):
    return bool(conn.execute('SELECT 1 FROM sqlite_master WHERE name=?', (name,)).fetchone())


def _sources(conn, sid=None):
    if not _table(conn, 'obs_source'):
        return []
    query = 'SELECT * FROM obs_source'
    params = ()
    if sid:
        query += ' WHERE session=? OR parent_session=?'
        params = (sid, sid)
    out = [dict(r) for r in conn.execute(query + ' ORDER BY source_id', params)]
    for row in out:
        row['capabilities'] = json.loads(row['capabilities'])
    return out


def _coverage(conn, sid=None):
    sources = _sources(conn, sid)
    required = [r for r in sources if r['required']]
    registered = [r for r in required if r['locator']]
    readable = [r for r in registered if os.path.isfile(r['locator']) and os.access(r['locator'], os.R_OK)]
    readable_ids = {r['source_id'] for r in readable}
    missing_children = sorted({r['session'] for r in required if r['parent_session'] and r['source_id'] not in readable_ids})
    # A closed expectation inventory is distinct from a closed session. Adapters must
    # explicitly attest child-discovery support; an unknown universe is never complete.
    unknown_children = sorted({r['session'] for r in required if not r['closed'] or not r['capabilities'].get('child_inventory_complete', False)})
    return {'expected': len(required), 'registered': len(registered), 'readable': len(readable),
            'missing_children': missing_children, 'unknown_children': unknown_children,
            'complete': bool(required) and len(readable) == len(required) and not unknown_children,
            'sources': sources, 'basis': 'explicit registered expectations; unknown capability is incomplete'}


def coverage(root, sid=None):
    conn = db.connect_readonly(root)
    try:
        return _coverage(conn, sid)
    finally:
        conn.close()


def issue(conn, code, *, source_id=None, detail='', identity=None):
    """Persist only bounded diagnostic metadata, never an exception message or raw line."""
    now = util.now_iso()
    key = _id(source_id, code, identity)
    conn.execute('INSERT INTO obs_issue VALUES (?,?,?,?,?,?,1,NULL) '
                 'ON CONFLICT(issue_id) DO UPDATE SET last_seen=excluded.last_seen, '
                 'occurrences=occurrences+1, resolved_at=NULL',
                 (key, source_id, code, str(detail)[:500], now, now))


def _resolve(conn, source_id, codes):
    for code in codes:
        conn.execute('UPDATE obs_issue SET resolved_at=? WHERE source_id=? AND code=? AND resolved_at IS NULL',
                     (util.now_iso(), source_id, code))


def _prefix(fh, length):
    fh.seek(0)
    digest = hashlib.sha256()
    left = length
    while left:
        block = fh.read(min(left, 1024 * 1024))
        if not block:
            raise OSError('source shortened during collection')
        digest.update(block)
        left -= len(block)
    return digest


def _facts(obj):
    payload = obj.get('payload') if isinstance(obj.get('payload'), dict) else {}
    message = obj.get('message') if isinstance(obj.get('message'), dict) else {}
    kind = str(payload.get('type') or obj.get('type') or obj.get('phase') or 'unknown')[:100]
    native_id = obj.get('uuid') or obj.get('id') or payload.get('id') or payload.get('response_id')
    native_id = str(native_id)[:256] if native_id is not None else None
    # Do not even select reasoning content. Its existence is metadata only.
    facts = {'kind': kind, 'provider_record_type': str(obj.get('type', 'unknown'))[:100]}
    if kind in ('reasoning', 'thinking', 'redacted_thinking'):
        facts['content_policy'] = 'private-reasoning-excluded'
    else:
        for key in ('role', 'model', 'stop_reason'):
            value = message.get(key, payload.get(key, obj.get(key)))
            if isinstance(value, str):
                facts[key] = value[:150]
        info = payload.get('info') or {}
        usage = message.get('usage') or obj.get('usage') or payload.get('usage') or (info.get('total_token_usage') if isinstance(info,dict) else None)
        if isinstance(usage, dict):
            facts['usage'] = {k:v for k,v in usage.items() if k in (
                'input_tokens','output_tokens','cached_input_tokens','cache_read_input_tokens',
                'cache_creation_input_tokens','total_tokens','reasoning_output_tokens')
                and isinstance(v, (int,float)) and not isinstance(v,bool) and math.isfinite(v) and v >= 0}
        if obj.get('phase') in ('pre','post'):
            facts.update(phase=obj['phase'], tool=str(obj.get('tool') or '')[:100],
                         tool_use_id=str(obj.get('tool_use_id') or '')[:256],
                         response_truncated=bool(obj.get('response_truncated')),
                         input_truncated=bool(obj.get('input_truncated')))
    if kind == 'hook_outcome':
        duration = float(obj.get('duration_ms',0))
        if not math.isfinite(duration):
            raise ValueError('nonfinite hook duration')
        facts.update(hook=str(obj.get('hook') or '?')[:100],outcome=str(obj.get('outcome') or 'unknown')[:30],
                     duration_ms=max(0,duration),error_type=str(obj.get('error_type') or '')[:100])
    if kind == 'function_call_output':
        output = payload.get('output')
        if isinstance(output,str) and len(output) <= MAX_LINE:
            try:
                output = json.loads(output)
            except ValueError:
                output = None
        if isinstance(output,dict):
            child = output.get('agent_id')
            if isinstance(child,str) and re.fullmatch(r'[A-Za-z0-9_-]{8,128}',child):
                facts['child_id'] = child
    source_time = obj.get('timestamp') or obj.get('ts')
    return kind, native_id, source_time if isinstance(source_time, str) else None, facts


def _before_commit(*_):
    """Fault-injection seam: observations and cursor must roll back together."""


def _collect_source(conn, source, budget, root):
    sid = source['source_id']
    if not source['locator']:
        return 0
    if source['provider']=='pool':
        from alpaca.capture_io import checked_path
        checked_path(root,source['locator'])
    fd = os.open(source['locator'], os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as fh:
        st = os.fstat(fh.fileno())
        if not stat.S_ISREG(st.st_mode):
            raise OSError('source is not a regular file')
        identity = '%s:%s' % (st.st_dev, st.st_ino)
        old = conn.execute('SELECT * FROM obs_generation WHERE source_id=? ORDER BY generation DESC LIMIT 1', (sid,)).fetchone()
        cursor = conn.execute('SELECT * FROM obs_cursor WHERE source_id=?', (sid,)).fetchone()
        fingerprint = _id(st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)
        if cursor and db.meta_get(conn,'obs:source-stat:'+sid) == fingerprint and cursor['byte_offset'] == st.st_size:
            _resolve(conn,sid,('source-unreadable','collector-error'))
            return 0
        offset = cursor['byte_offset'] if cursor else 0
        generation = old['generation'] if old else 1
        line = cursor['line_number'] if cursor else 0
        digest = _prefix(fh, min(offset, st.st_size))
        reset = old and (old['identity'] != identity or st.st_size < offset or digest.hexdigest() != old['prefix_sha256'])
        if reset:
            generation += 1
            offset = line = 0
            digest = _prefix(fh, 0)
        fh.seek(offset)
        batch = []
        problems = []
        for _ in range(budget):
            begin = fh.tell()
            raw = fh.readline(MAX_LINE + 1)
            if not raw:
                break
            if len(raw) > MAX_LINE:
                # Scan oversized complete records without parsing or retaining payloads.
                # The independent byte ceiling also bounds an unterminated hostile line.
                oversized = hashlib.sha256(raw)
                candidate_digest = digest.copy()
                candidate_digest.update(raw)
                scanned = len(raw)
                while not raw.endswith(b'\n') and scanned < 64*1024*1024:
                    raw = fh.readline(min(1024*1024,64*1024*1024-scanned))
                    if not raw:
                        break
                    scanned += len(raw)
                    oversized.update(raw)
                    candidate_digest.update(raw)
                if not raw.endswith(b'\n'):
                    problems.append(('oversize-line', 'unterminated line exceeds parser limit; cursor retained', begin))
                    break
                line += 1
                offset = fh.tell()
                digest = candidate_digest
                hashed = oversized.hexdigest()
                problems.append(('oversize-line','quarantined line=%d offset=%d bytes=%d sha256=%s' % (line,begin,scanned,hashed),begin))
                batch.append((sid,generation,begin,offset,line,None,'quarantined',hashed,util.now_iso(),None,{'reason':'oversize-line','bytes':scanned}))
                continue
            if not raw.endswith(b'\n'):
                break
            line += 1
            digest.update(raw)
            offset = fh.tell()
            hashed = hashlib.sha256(raw).hexdigest()
            try:
                obj = json.loads(raw)
                if not isinstance(obj, dict):
                    raise ValueError('object required')
                kind, native_id, source_time, facts = _facts(obj)
            except (ValueError, TypeError, AttributeError, OverflowError):
                problems.append(('malformed-json', 'line=%d offset=%d sha256=%s' % (line, begin, hashed), begin))
                kind, native_id, source_time, facts = 'quarantined', None, None, {'reason':'malformed-json'}
            batch.append((sid,generation,begin,offset,line,native_id,kind,hashed,util.now_iso(),source_time,facts))
        # Reject mixed generations if the inode or consumed bytes changed during the read.
        if _prefix(fh, offset).hexdigest() != digest.hexdigest():
            raise OSError('source changed while collecting')
        # File metadata can change without new content. Whole-prefix verification
        # above is sufficient: do not fsync an unchanged cursor on every poll.
        # Keeping the old stat hint merely forces another safe prefix verification.
        if old and not reset and not batch and not problems:
            _resolve(conn,sid,('source-unreadable','collector-error'))
            return 0
        with db.transaction(conn):
            if not old or reset:
                conn.execute('INSERT INTO obs_generation VALUES (?,?,?,?,?,?)',
                             (sid,generation,identity,0,EMPTY_HASH,util.now_iso()))
                if reset:
                    issue(conn, 'source-generation-changed', source_id=sid,
                          detail='prior generation retained; replacement replayed', identity=generation)
            for record in batch:
                facts = record[-1]
                facts['provenance'] = {'project_id':db.meta_get(conn,'obs:project-id'),'collector_host_id':db.meta_get(conn,'obs:host-id'),'normalizer':'observability/v1'}
                if record[5]:
                    prior = conn.execute('SELECT payload_sha256,kind FROM obs_observation WHERE source_id=? AND native_id=? ORDER BY id DESC LIMIT 1', (sid, record[5])).fetchone()
                    if prior:
                        facts['identity_status'] = 'duplicate' if prior[0] == record[7] else 'revision' if record[6] in ('assistant','message') else 'conflict'
                        if facts['identity_status'] == 'conflict':
                            issue(conn,'native-id-conflict',source_id=sid, detail='native identity has distinct payload hashes',identity=record[5])
                inserted = conn.execute('INSERT OR IGNORE INTO obs_observation (source_id,generation,byte_offset,end_offset,line_number,native_id,kind,payload_sha256,observed_at,source_time,facts) VALUES (?,?,?,?,?,?,?,?,?,?,?)', record[:-1] + (json.dumps(facts),))
                if facts.get('child_id'):
                    child = facts['child_id']
                    child_key = _id(child,source['provider'],'transcript')
                    conn.execute('INSERT OR IGNORE INTO obs_source VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                                 (child_key,child,source['session'],source['provider'],'transcript',None,child,
                                  json.dumps({'child_inventory_complete':False}),1,0,util.now_iso(),util.now_iso()))
                if inserted.rowcount and record[6] == 'hook_outcome' and facts.get('identity_status') != 'duplicate':
                    conn.execute('INSERT INTO obs_hook_outcome (session,hook,outcome,duration_ms,recorded_at,error_type) VALUES (?,?,?,?,?,?)',
                                 (source['session'],facts['hook'],facts['outcome'],facts['duration_ms'],record[8],facts['error_type']))
                    from alpaca.hooks.common import _update_cost
                    _update_cost(conn,source['session'],facts['hook'],facts['duration_ms'])
            for code, detail, identity_ in problems:
                issue(conn, code, source_id=sid, detail=detail, identity=(generation,identity_))
            conn.execute('UPDATE obs_generation SET prefix_bytes=?,prefix_sha256=? WHERE source_id=? AND generation=?', (offset,digest.hexdigest(),sid,generation))
            db.upsert(conn, 'obs_cursor', 'source_id', dict(source_id=sid,generation=generation,byte_offset=offset,line_number=line,updated_at=util.now_iso()))
            _resolve(conn,sid,('source-unreadable','collector-error'))
            db.meta_set(conn,'obs:source-stat:'+sid,fingerprint)
            _before_commit(conn, source)
        return len(batch)


@contextlib.contextmanager
def collector_lock(root):
    folder = Path(root) / '.alpaca/observability'
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(folder/'collector.lock', os.O_CREAT | os.O_RDWR | getattr(os,'O_NOFOLLOW',0), 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)  # persistent lock inode, never unlink


def _register_local(root, conn):
    """Discover only project-owned pool files and explicitly registered transcript paths."""
    known = {(r['session'],r['provider'],r['kind']) for r in _sources(conn)}
    sessions = [dict(r) for r in conn.execute('SELECT sid,transcript FROM sessions')]
    for session in sessions:
        if session['transcript']:
            provider = db.meta_get(conn,'operator:'+session['sid'],'claude')
            registered = conn.execute("SELECT locator,capabilities FROM obs_source WHERE session=? AND provider=? AND kind='transcript'", (session['sid'],provider)).fetchone()
            if registered and json.loads(registered['capabilities']).get('restore_disabled'):
                continue
            if registered is None or registered['locator'] != str(Path(session['transcript']).expanduser().resolve()):
                expect_source(root,session['sid'],provider,locator=session['transcript'],capabilities={'native_transcript':True})
    for row in _sources(conn):
        if row['capabilities'].get('restore_disabled'):
            continue
        if not row['locator'] and row['native_id'] and row['provider']=='codex':
            from alpaca.transcripts import discover_codex
            try:
                found = discover_codex(root,row['native_id'])
            except ValueError:
                found = None
            if found:
                conn.execute('UPDATE obs_source SET locator=?,updated_at=? WHERE source_id=?',
                             (found,util.now_iso(),row['source_id']))
    for folder,kind in (('tools','tools'),('hooks','hook-outcomes')):
        from alpaca.capture_io import checked_path
        try:
            directory = checked_path(root,Path(root)/'.alpaca/pool'/folder)
        except ValueError:
            issue(conn,'pool-path-refused',detail='project pool ancestry is unsafe',identity=folder)
            continue
        for path in sorted(directory.glob('*.jsonl')):
            if path.is_symlink():
                continue
            if (path.stem,'pool',kind) not in known:
                expect_source(root,path.stem,'pool',locator=str(path),kind=kind,required=False,
                              capabilities={'tool_pre_post':kind=='tools','hook_outcomes':kind=='hook-outcomes'})


def _process_identity(pid):
    try:
        ticks = Path('/proc/%d/stat' % int(pid)).read_text().rsplit(') ',1)[1].split()[19]
        boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        return boot+':'+ticks
    except (OSError,ValueError,IndexError,TypeError):
        return None


def _run(root, *, max_records=1000, consumers=True):
    max_records = max(1,min(int(max_records),100000))
    conn = db.connect(root)
    result = {'records':0, 'errors':0, 'sources':0}
    try:
        with db.transaction(conn):
            if not db.meta_get(conn,'obs:project-id'):
                db.meta_set(conn,'obs:project-id',str(uuid.uuid4()))
            try:
                machine = Path('/etc/machine-id').read_text().strip()
            except OSError:
                machine = socket.gethostname()
            host = _id(db.meta_get(conn,'obs:project-id'),machine)
            if db.meta_get(conn,'obs:host-id') != host:
                db.meta_set(conn,'obs:host-id',host)
        _register_local(root,conn)
        sources = _sources(conn)
        rotation = int(db.meta_get(conn,'obs:rotation','0')) % max(1,len(sources))
        sources = sources[rotation:] + sources[:rotation]
        for source in sources:
            retry = json.loads(db.meta_get(conn,'obs:retry:'+source['source_id'],'{}'))
            if retry.get('after') and _time(retry['after']) > _time():
                continue
            if result['records'] >= max_records:
                break
            try:
                result['records'] += _collect_source(conn,source,min(200,max_records-result['records']) if len(sources)>1 else max_records,root)
                if retry:
                    db.meta_set(conn,'obs:retry:'+source['source_id'],'{}')
            except (OSError,sqlite3.Error,RuntimeError,ValueError) as exc:
                result['errors'] += 1
                failures = retry.get('failures',0)+1
                db.meta_set(conn,'obs:retry:'+source['source_id'],json.dumps({'failures':failures,'after':(_time()+dt.timedelta(seconds=min(900,5*2**min(failures,8)))).isoformat()}))
                issue(conn,'source-unreadable' if isinstance(exc,OSError) else 'collector-error',source_id=source['source_id'],detail=type(exc).__name__)
            result['sources'] += 1
        with db.transaction(conn):
            db.meta_set(conn,'obs:rotation',str(rotation+max(1,result['sources'])))
            db.meta_set(conn,'obs:collector',json.dumps({'pid':os.getpid(),'process_identity':_process_identity(os.getpid()),'heartbeat':util.now_iso(), 'last_result':result, 'mode':'watch' if os.environ.get('ALPACA_COLLECTOR_WATCH') else 'once'}))
        if consumers:
            from .consumers import consume, snapshot_observations, run_snapshot_jobs
            result['consumers'] = consume(root,conn,max_records=min(max_records,200))
            result['consumers']['source-snapshot-requests'] = snapshot_observations(root,conn,max_records=max_records)
            result['preservation'] = run_snapshot_jobs(root,conn)
        return result
    finally:
        conn.close()


def run_once(root, *, max_records=1000, consumers=True):
    with collector_lock(root):
        return _run(root,max_records=max_records,consumers=consumers)


def status(root, now=None):
    conn = db.connect_readonly(root)
    try:
        cov = _coverage(conn)
        sources = cov['sources']
        cursors = {r['source_id']:dict(r) for r in conn.execute('SELECT * FROM obs_cursor')} if _table(conn,'obs_cursor') else {}
        backlog = 0
        for source in sources:
            cursor = cursors.get(source['source_id'],{})
            source['cursor'] = cursor
            try:
                size = os.stat(source['locator']).st_size if source['locator'] else None
            except OSError:
                size = None
            source['backlog_bytes'] = max(0,size-cursor.get('byte_offset',0)) if size is not None else None
            backlog += source['backlog_bytes'] or 0
        collector = json.loads(db.meta_get(conn,'obs:collector','{}'))
        age = max(0,(_time(now)-_time(collector['heartbeat'])).total_seconds()) if collector.get('heartbeat') else None
        collector.update(age_seconds=age,status='fresh' if age is not None and age<120 else 'stale' if age is not None else 'unknown')
        # Heartbeat freshness is not process liveness. kill(0) alone risks PID reuse;
        # supervision owns liveness, this record reports only observed collection activity.
        collector['liveness'] = 'unknown'
        if collector.get('mode')=='watch' and collector.get('process_identity'):
            collector['liveness'] = 'alive' if _process_identity(collector.get('pid'))==collector['process_identity'] else 'stopped'
        issues = [dict(r) for r in conn.execute('SELECT * FROM obs_issue WHERE resolved_at IS NULL ORDER BY last_seen DESC LIMIT 100')] if _table(conn,'obs_issue') else []
        consumers = [dict(r) for r in conn.execute('SELECT * FROM obs_consumer_checkpoint')] if _table(conn,'obs_consumer_checkpoint') else []
        head = conn.execute('SELECT COALESCE(MAX(id),0) FROM events').fetchone()[0]
        for consumer in consumers:
            consumer['event_lag'] = max(0,head-consumer['event_id']) if consumer['consumer']!='source-snapshot-requests' else None
        observation_head = conn.execute('SELECT COALESCE(MAX(id),0) FROM obs_observation').fetchone()[0] if _table(conn,'obs_observation') else 0
        acknowledged = next((c['observation_id'] for c in consumers if c['consumer']=='source-snapshot-requests'),0)
        observation_lag = max(0,observation_head-acknowledged)
        pending = conn.execute("SELECT COUNT(*) FROM obs_projection_job WHERE status!='done'").fetchone()[0] if _table(conn,'obs_projection_job') else 0
        consumer_lag = pending or observation_lag or any(c.get('event_lag') or c.get('failures') for c in consumers)
        return {'status':'ok' if collector['status']=='fresh' and not issues and not backlog and consumers and not consumer_lag and cov['complete'] else 'degraded',
                'collector':collector,'sources':sources,'backlog':{'bytes':backlog,'preservation_jobs':pending,'observations':observation_lag},'consumers':consumers,'issues':issues,'coverage':cov}
    finally:
        conn.close()
