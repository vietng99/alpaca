"""Durable source registry and bounded, replayable local collection.

The record holds cursors and sanitized facts atomically. Authorized native files retain
payloads; this catalog never stores prompts, tool arguments, results or private reasoning.
Consumers checkpoint separately and may retry without holding up source ingestion.
"""
from __future__ import annotations

import collections
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
                 'detail=excluded.detail, occurrences=occurrences+1, resolved_at=NULL',
                 (key, source_id, code, str(detail)[:500], now, now))


def _resolve(conn, source_id, codes):
    """Close the source's open issues of `codes`. Every poll of every source calls this, and almost
    always nothing is open: one indexed read (`idx_obs_issue_open`) then answers, with no UPDATE,
    which in autocommit is a write transaction even when it changes no row (t-679: two unindexed
    UPDATEs per source per pass were two thirds of the live collector's CPU)."""
    marks = ','.join('?' * len(codes))
    if not conn.execute('SELECT 1 FROM obs_issue WHERE source_id=? AND code IN (%s) AND resolved_at IS NULL LIMIT 1'
                        % marks, (source_id, *codes)).fetchone():
        return
    conn.execute('UPDATE obs_issue SET resolved_at=? WHERE source_id=? AND code IN (%s) AND resolved_at IS NULL'
                 % marks, (util.now_iso(), source_id, *codes))


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


#: t-679: the stat of a source this process verified to hold no new content at its cursor, by
#: (root, source_id): ((dev, ino, size, mtime_ns, ctime_ns), generation, byte_offset). A poll that
#: finds nothing writes nothing, so the stored stat hint (`obs:source-stat:`) stays the one of the
#: last batch; a stat that changed without new content (a link or touch, or lines that landed between
#: the fstat and the read of the pass that took them) then sent every later poll through a read and
#: hash of the whole file. The entry stands in for the stored hint, under the same trust (an
#: unchanged stat), and while the stat stays the same a poll is one stat call. Only the collector
#: holding the lock writes cursors; `_run` drops a root's entries when its record file is replaced
#: (`_record_identity`) and a source's entry when its poll fails. Bounded; a fresh process verifies
#: each source once.
_VERIFIED = collections.OrderedDict()
MAX_VERIFIED = 65536
_VERIFIED_RECORD = {}


def _record_identity(root):
    """Forget the verified stats of `root` when its record file is not the one they were taken
    against (a restore or a replaced copy may hold other cursors)."""
    from alpaca import paths
    try:
        info = os.stat(paths.db_path(str(root)))
        identity = (info.st_dev, info.st_ino)
    except OSError:
        identity = None
    if _VERIFIED_RECORD.get(str(root)) != identity:
        for key in [key for key in _VERIFIED if key[0] == str(root)]:
            del _VERIFIED[key]
        _VERIFIED_RECORD[str(root)] = identity


def _remember_verified(key, known):
    _VERIFIED[key] = known
    _VERIFIED.move_to_end(key)
    while len(_VERIFIED) > MAX_VERIFIED:
        _VERIFIED.popitem(last=False)


def _collect_source(conn, source, budget, root):
    sid = source['source_id']
    if not source['locator']:
        return 0
    verified_key = (str(root), sid)
    seen = _VERIFIED.get(verified_key)
    if seen is not None:
        # t-679: a source this process verified, with the same stat now, needs no open, no query and
        # no issue check (only `_run` opens this source's issues, and it forgets the entry then). No
        # file is read here, so a link at the locator or above it reads nothing: a link has a stat
        # of its own, and one that leads to the verified file leads to the bytes already verified.
        try:
            now = os.stat(source['locator'], follow_symlinks=False)
        except OSError:
            now = None
        if now is not None and stat.S_ISREG(now.st_mode) and \
                (now.st_dev, now.st_ino, now.st_size, now.st_mtime_ns, now.st_ctime_ns) == seen[0]:
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
        cursor = conn.execute('SELECT * FROM obs_cursor WHERE source_id=?', (sid,)).fetchone()
        stamp = (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)
        fingerprint = None
        if cursor and cursor['byte_offset'] == st.st_size:
            known = (stamp, cursor['generation'], cursor['byte_offset'])
            if _VERIFIED.get(verified_key) != known:
                fingerprint = _id(*stamp)
            if fingerprint is None or db.meta_get(conn,'obs:source-stat:'+sid) == fingerprint:
                _remember_verified(verified_key, known)
                _resolve(conn,sid,('source-unreadable','collector-error'))
                return 0
        _VERIFIED.pop(verified_key, None)
        if fingerprint is None:
            fingerprint = _id(*stamp)
        old = conn.execute('SELECT * FROM obs_generation WHERE source_id=? ORDER BY generation DESC LIMIT 1', (sid,)).fetchone()
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
            if cursor and offset == st.st_size == cursor['byte_offset'] and generation == cursor['generation']:
                _remember_verified(verified_key, (stamp, generation, offset))
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
    from alpaca import _nofollow
    # no link followed at any level below .alpaca (t-424): a linked .alpaca/observability, or a linked
    # collector.lock, is refused rather than followed to a file outside the project
    fd = _nofollow.open_file_below(root, ('observability',), 'collector.lock', os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)  # persistent lock inode, never unlink


def _register_local(root, conn):
    """Discover only project-owned pool files and explicitly registered transcript paths."""
    # t-679: every pass runs this. It reads the few columns each step needs in one query per step,
    # not the whole parsed source list (10,000 rows on the live record) twice plus two queries per
    # session; the provider is the session's `operator:` meta value, 'claude' when there is none.
    known = {(r[0],'pool',r[1]) for r in conn.execute("SELECT session,kind FROM obs_source WHERE provider='pool'")}
    sessions = [dict(r) for r in conn.execute(
        "SELECT p.sid,p.transcript,p.provider,o.source_id,o.locator,o.capabilities FROM "
        "(SELECT s.sid,s.transcript,CASE WHEN m.key IS NULL THEN 'claude' ELSE m.value END AS provider "
        " FROM sessions s LEFT JOIN meta m ON m.key='operator:'||s.sid) p "
        "LEFT JOIN obs_source o ON o.session=p.sid AND o.provider=p.provider AND o.kind='transcript'")]
    for session in sessions:
        if session['transcript']:
            provider = session['provider']
            registered = session if session['source_id'] is not None else None
            if registered and json.loads(registered['capabilities']).get('restore_disabled'):
                continue
            if registered is None or registered['locator'] != str(Path(session['transcript']).expanduser().resolve()):
                expect_source(root,session['sid'],provider,locator=session['transcript'],capabilities={'native_transcript':True})
    for row in conn.execute("SELECT source_id,native_id,capabilities FROM obs_source "
                            "WHERE provider='codex' AND locator IS NULL ORDER BY source_id").fetchall():
        if json.loads(row['capabilities']).get('restore_disabled'):
            continue
        if row['native_id']:
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


def _pollable(conn):
    """The sources a pass polls, in `_sources` order, as `_POLLED` tuples: the columns `_collect_source`
    reads, with no parse of the capabilities and no dict for a source in backoff (t-679: building and
    parsing 10,000 rows was the largest cost of an idle pass)."""
    if not _table(conn, 'obs_source'):
        return []
    return [tuple(r) for r in conn.execute('SELECT %s FROM obs_source ORDER BY source_id' % ','.join(_POLLED))]


_POLLED = ('source_id', 'session', 'parent_session', 'provider', 'kind', 'locator', 'native_id')


_RETRY = 'obs:retry:'


def _retries(conn):
    """Every source's retry backoff by source id, in one read of the meta key range (t-679: one
    query per source per pass before; 5,850 of the live record's 10,000 sources sit in a backoff)."""
    return {key[len(_RETRY):]: json.loads(value) for key, value in conn.execute(
        'SELECT key,value FROM meta WHERE key>=? AND key<?', (_RETRY, _RETRY[:-1] + ';'))}


def _process_identity(pid):
    try:
        ticks = Path('/proc/%d/stat' % int(pid)).read_text().rsplit(') ',1)[1].split()[19]
        boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        return boot+':'+ticks
    except (OSError,ValueError,IndexError,TypeError):
        return None


#: The kernel's table of held file locks (Linux); `collector_alive` reads it instead of taking a lock.
_PROC_LOCKS = '/proc/locks'
#: Full scans of /proc/locks before `_flock_holder` reports no holder (t-334, review2 M1). The kernel
#: serves the table one page (about 4 KB) per read call and walks it again from its position on
#: the next call, so a lock taken or released by another process between two calls shifts the list
#: and one scan can skip the holder's line. A miss now needs that race on every scan.
_PROC_LOCKS_SCANS = 3
#: Bytes asked for per read call; the kernel returns at most a page, the loop reads to EOF.
_PROC_LOCKS_CHUNK = 1 << 16


def _read_proc_locks():
    """The whole lock table as text, read call after call until EOF; None when it cannot be read."""
    parts = []
    try:
        with open(_PROC_LOCKS, 'rb', buffering=0) as fh:
            while True:
                part = fh.read(_PROC_LOCKS_CHUNK)
                if not part:
                    break
                parts.append(part)
    except OSError:
        return None
    return b''.join(parts).decode('ascii', 'replace')


def _flock_holder(info):
    """The pid holding an exclusive flock on the file whose `os.stat` is `info`, read from
    /proc/locks (`<n>: FLOCK  ADVISORY  WRITE <pid> <major>:<minor>:<inode> 0 EOF`, device numbers
    in hex; a `->` line is a waiter, not a holder). None when no process holds one or the table
    cannot be read. Takes no lock and never waits: each scan reads the table to EOF, and a scan that
    finds no holder is repeated, up to `_PROC_LOCKS_SCANS` scans in all."""
    want = (os.major(info.st_dev), os.minor(info.st_dev), info.st_ino)
    for _ in range(_PROC_LOCKS_SCANS):
        text = _read_proc_locks()
        if text is None:
            return None
        holder = _holder_in(text.splitlines(), want)
        if holder is not None:
            return holder
    return None


def _holder_in(lines, want):
    """The pid of the FLOCK WRITE holder of `want` (major, minor, inode) in /proc/locks `lines`."""
    for line in lines:
        fields = line.split()
        if len(fields) < 6 or fields[1] != 'FLOCK' or fields[3] != 'WRITE':
            continue
        try:
            major, minor, inode = fields[5].split(':')
            if (int(major, 16), int(minor, 16), int(inode)) == want:
                return int(fields[4])
        except ValueError:
            continue
    return None


def collector_alive(root):
    """Whether this project's `collect watch` collector is running now, so a hook can leave wiki
    capture to it (merge of op-008 into main, review B2). True only when this root's
    `collector.lock` is held (read from /proc/locks: the lock is never taken here, so a collector
    starting at this moment is never refused it) by the very process the record's `obs:collector`
    row names, in watch mode, with the process identity (boot id and start time) the row recorded,
    which is how `status` tells a live collector from a reused pid. A copy of the record whose row
    names another root's collector, a `collect once` run, a dead collector's row: all False.

    Bounded and non-blocking: one stat, one read of /proc/locks and, only when the lock is held,
    one read-only query of the record with no busy wait. Anything missing, unreadable or busy
    gives False, so the caller falls back to doing the work itself."""
    try:
        info = os.stat(Path(root)/'.alpaca/observability/collector.lock', follow_symlinks=False)
    except (OSError, ValueError):
        return False
    if not stat.S_ISREG(info.st_mode):
        return False
    holder = _flock_holder(info)
    if holder is None:
        return False
    from alpaca import paths
    try:
        uri = Path(paths.db_path(str(root))).absolute().as_uri() + '?mode=ro'
        conn = sqlite3.connect(uri, uri=True, timeout=0, isolation_level=None)
        try:
            row = conn.execute("SELECT value FROM meta WHERE key='obs:collector'").fetchone()
        finally:
            conn.close()
        collector = json.loads(row[0]) if row else None
    except (sqlite3.Error, OSError, ValueError, TypeError):
        return False
    if not isinstance(collector, dict) or collector.get('mode') != 'watch':
        return False
    identity = collector.get('process_identity')
    return (collector.get('pid') == holder and bool(identity)
            and _process_identity(holder) == identity)


def capture_path(root):
    """Who captures the wiki now (A-8, t-070 RC4). The hooks leave capture to the collector while the
    observability flag is on or while this project's watch collector is alive; with the flag on and
    no collector alive, nobody captures. `warning` is then `capture_runner.NO_COLLECTOR`, which
    `bin/alpaca doctor` (`checks`) and `wiki status` report. Reads only: no lock, no flag change."""
    from alpaca.capture_runner import NO_COLLECTOR
    on = enabled(root)
    alive = collector_alive(root)
    return {'observability_enabled': on, 'collector_alive': alive,
            'captured_by': 'collector' if alive else ('nobody' if on else 'hooks'),
            'warning': NO_COLLECTOR if on and not alive else None}


def checks(root, conn):
    """The doctor finding for `capture_path` (alpaca/doctor.py CHECK_MODULES), as (name, level,
    detail): a warning while the flag is on and no collector is alive."""
    path = capture_path(root)
    if path['warning']:
        return [('collector', 'warn', path['warning'] + ': the hooks leave wiki capture to the collector, '
                 'so no event is captured until one runs (bin/alpaca collect watch)')]
    if path['collector_alive']:
        return [('collector', 'ok', 'collector alive: it captures the wiki')]
    return [('collector', 'ok', 'observability off and no collector: the hooks capture the wiki')]


def _run(root, *, max_records=1000, consumers=True, source_batch=200, event_batch=200, preservation_batch=8, stop_requested=None, retry_consumers=(), warm=None):
    """One collector pass. `warm` (default: in watch mode, `ALPACA_COLLECTOR_WATCH`, as the heartbeat's
    `mode`) first starts or polls the wiki lane's echo index read (`consumers.warm_wiki`, t-462), so a
    long-lived collector never reads it inside a capture's budget; `collect once` is one pass in a
    process of its own and reads it inside its capture, as before."""
    if warm is None:
        warm = bool(os.environ.get('ALPACA_COLLECTOR_WATCH'))
    max_records = max(1,min(int(max_records),100000))
    source_batch = max(1,min(int(source_batch),10000))
    event_batch = max(1,min(int(event_batch),10000))
    preservation_batch = max(1,min(int(preservation_batch),256))
    conn = db.connect(root)
    result = {'records':0, 'errors':0, 'sources':0}
    try:
        if consumers and warm:
            from .consumers import warm_wiki
            result['wiki_lane'] = warm_wiki(root)
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
        _record_identity(root)
        sources = _pollable(conn)
        retries = _retries(conn)
        rotation = int(db.meta_get(conn,'obs:rotation','0')) % max(1,len(sources))
        sources = sources[rotation:] + sources[:rotation]
        now = _time()
        for row in sources:
            retry = retries.get(row[0], {})
            if retry.get('after') and _time(retry['after']) > now:
                continue
            source = dict(zip(_POLLED, row))
            if result['records'] >= max_records:
                break
            try:
                result['records'] += _collect_source(conn,source,min(source_batch,max_records-result['records']) if len(sources)>1 else max_records,root)
                if retry:
                    db.meta_set(conn,'obs:retry:'+source['source_id'],'{}')
            except (OSError,sqlite3.Error,RuntimeError,ValueError) as exc:
                _VERIFIED.pop((str(root),source['source_id']), None)
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
            result['consumers'] = consume(root,conn,max_records=min(max_records,event_batch),retry_consumers=retry_consumers)
            result['consumers']['source-snapshot-requests'] = snapshot_observations(root,conn,max_records=max_records)
            result['preservation'] = run_snapshot_jobs(root,conn,limit=preservation_batch,stop_requested=stop_requested)
        return result
    finally:
        conn.close()


def run_once(root, *, max_records=1000, consumers=True, source_batch=200, event_batch=200, preservation_batch=8, retry_consumers=()):
    with collector_lock(root):
        return _run(root,max_records=max_records,consumers=consumers,
                    source_batch=source_batch,event_batch=event_batch,preservation_batch=preservation_batch,retry_consumers=retry_consumers,
                    warm=False)


def _collector(conn, now):
    collector = json.loads(db.meta_get(conn,'obs:collector','{}'))
    age = max(0,(_time(now)-_time(collector['heartbeat'])).total_seconds()) if collector.get('heartbeat') else None
    collector.update(age_seconds=age,status='fresh' if age is not None and age<120 else 'stale' if age is not None else 'unknown')
    # Heartbeat freshness is not process liveness. kill(0) alone risks PID reuse;
    # supervision owns liveness, this record reports only observed collection activity.
    collector['liveness'] = 'unknown'
    if collector.get('mode')=='watch' and collector.get('process_identity'):
        collector['liveness'] = 'alive' if _process_identity(collector.get('pid'))==collector['process_identity'] else 'stopped'
    return collector


def _consumer_lag(conn):
    consumers = [dict(r) for r in conn.execute('SELECT * FROM obs_consumer_checkpoint')] if _table(conn,'obs_consumer_checkpoint') else []
    head = conn.execute('SELECT COALESCE(MAX(id),0) FROM events').fetchone()[0]
    for consumer in consumers:
        consumer['event_lag'] = max(0,head-consumer['event_id']) if consumer['consumer']!='source-snapshot-requests' else None
    observation_head = conn.execute('SELECT COALESCE(MAX(id),0) FROM obs_observation').fetchone()[0] if _table(conn,'obs_observation') else 0
    acknowledged = next((c['observation_id'] for c in consumers if c['consumer']=='source-snapshot-requests'),0)
    observation_lag = max(0,observation_head-acknowledged)
    pending = conn.execute("SELECT COUNT(*) FROM obs_projection_job WHERE status!='done'").fetchone()[0] if _table(conn,'obs_projection_job') else 0
    consumer_lag = pending or observation_lag or any(c.get('event_lag') or c.get('failures') for c in consumers)
    return consumers, {'preservation_jobs':pending,'observations':observation_lag}, consumer_lag


def _overall(collector, issues, backlog, consumers, consumer_lag, complete):
    return 'ok' if collector['status']=='fresh' and not issues and not backlog and consumers and not consumer_lag and complete else 'degraded'


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
        collector = _collector(conn, now)
        issues = [dict(r) for r in conn.execute('SELECT * FROM obs_issue WHERE resolved_at IS NULL ORDER BY last_seen DESC LIMIT 100')] if _table(conn,'obs_issue') else []
        consumers, lag, consumer_lag = _consumer_lag(conn)
        return {'status':_overall(collector, issues, backlog, consumers, consumer_lag, cov['complete']),
                'collector':collector,'sources':sources,'backlog':{'bytes':backlog,**lag},'consumers':consumers,'issues':issues,'coverage':cov}
    finally:
        conn.close()


def summary(root, now=None):
    """status() in counts, not lists (t-590): the hub polls it every 10 s, and on a busy record the
    per-source list is thousands of rows. One stat per source answers both readability and backlog;
    the open issues come as their count and the last_seen of the newest 100 (status keeps 100)."""
    conn = db.connect_readonly(root)
    try:
        expected = registered = readable = backlog = 0
        missing, unknown = set(), set()
        if _table(conn, 'obs_source'):
            offsets = dict(conn.execute('SELECT source_id, byte_offset FROM obs_cursor').fetchall()) if _table(conn,'obs_cursor') else {}
            for source_id, session, parent, locator, required, closed, capabilities in conn.execute(
                    'SELECT source_id, session, parent_session, locator, required, closed, capabilities FROM obs_source'):
                try:
                    info = os.stat(locator) if locator else None
                except OSError:
                    info = None
                if info is not None:
                    backlog += max(0, info.st_size-offsets.get(source_id, 0))
                if not required:
                    continue
                expected += 1
                registered += bool(locator)
                ok = info is not None and stat.S_ISREG(info.st_mode) and os.access(locator, os.R_OK)
                readable += ok
                if parent and not ok:
                    missing.add(session)
                if not closed or not json.loads(capabilities).get('child_inventory_complete', False):
                    unknown.add(session)
        complete = bool(expected) and readable == expected and not unknown
        collector = _collector(conn, now)
        seen = [r[0] for r in conn.execute('SELECT last_seen FROM obs_issue WHERE resolved_at IS NULL ORDER BY last_seen DESC LIMIT 100')] if _table(conn,'obs_issue') else []
        consumers, lag, consumer_lag = _consumer_lag(conn)
        return {'status':_overall(collector, seen, backlog, consumers, consumer_lag, complete),
                'collector':collector,'backlog':{'bytes':backlog,**lag},
                'issues':{'open':conn.execute('SELECT COUNT(*) FROM obs_issue WHERE resolved_at IS NULL').fetchone()[0] if seen else 0,'last_seen':seen},
                'coverage':{'expected':expected,'registered':registered,'readable':readable,'missing_children':len(missing),
                            'unknown_children':len(unknown),'complete':complete}}
    finally:
        conn.close()
