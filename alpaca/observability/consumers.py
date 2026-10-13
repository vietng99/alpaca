"""Independent projections with durable checkpoints and capped retry backoff."""
import atexit
import datetime as dt
import errno
from collections import OrderedDict
import os
from pathlib import Path
import stat
import threading
import time
import weakref
import json

from alpaca import db, util
from . import _time, issue

BOUNDARIES = {'session-start','session-end','pre-compact','session-checkpoint','turn-end','subagent-stop'}


#: Wall-clock budget of one preservation (snapshot) job's capture; a deferred job is retried. The
#: job always lands its first document before the budget is checked (`min_documents`), so a job is
#: never deferred with nothing done. The wiki consumer has no budget (`WIKI_CAPTURE_DEADLINE_S`).
CAPTURE_DEADLINE_S = 45.0
#: The wiki consumer's budget: none, as on main (40c7233ac). Its batch is bounded by the event batch
#: and it runs in the collector, which has no hook ceiling; on the live store (565,000 active blocks,
#: 2026-10-06) reading the echo index alone took 30-37 s, so the 45 s budget deferred every batch
#: and the consumer only backed off (merge review B1).
WIKI_CAPTURE_DEADLINE_S = None
#: Wall-clock budget of the optional consolidation step after a collector capture.
CONSOLIDATE_DEADLINE_S = 15.0
#: How long the collector loop waits for one wiki capture (a consumer batch with its consolidation
#: step, or one preservation job) before it abandons it (t-070 RC4). The work runs in the `wiki` lane
#: (`capture_runner.Lane`), whose thread keeps the shared absorber; an abandoned capture ends on its
#: own, the batch or job is retried after the consumer's backoff, and meanwhile the other consumers
#: and the next passes go on. Above a cold start on the live store (the echo index read was 30-37 s,
#: merge review B1), so a normal batch is never abandoned.
CAPTURE_BUDGET_S = 120.0
#: How long a watch pass waits for the wiki lane's echo index warm-up (`warm_wiki`) before it goes on
#: and leaves the lane `warming` (t-462).
WARM_WAIT_S = 0.5
#: The longest the wiki lane may stay `warming` (one echo index read, or reads started one after the
#: other because another connection's commits keep dropping the index) before the pass stops waiting
#: for it: the lane then answers `busy`, and the capture records `capture-deferred` and the consumer
#: failure, as a capture abandoned in the lane does (review B-1). Five capture budgets: the live
#: store's cold read took about 60 s (t-462) and 290 s with main's read.
WARM_CEILING_S = CAPTURE_BUDGET_S * 5
#: After a warm-up that failed, the passes do not start another one for this long: the wiki lane is
#: `failed`, so the captures read the index themselves inside their budget and record what they come
#: to (`drain-failed`, a `consumer-failed` issue, the backoff) instead of a fresh read hiding the
#: failure as `warming` (review B-1).
WARM_RETRY_S = CAPTURE_BUDGET_S * 5
#: How long the loop waits for one session view (the pad, the analytics page) in the `views` lane
#: before it abandons it; the view stays pending and is tried again after its interval (t-334
#: follow-up: a hung transcript read in an analytics build held the whole loop).
VIEW_BUDGET_S = 120.0
#: An outcome equal to the last one the collector recorded for a lane is not recorded again within
#: this many seconds (`capture_runner.record`), so a batch retried at every backoff step leaves one
#: `drain-failed` or `capture-deferred`.
OUTCOME_QUIET_S = 3600.0
#: t-606: a snapshot job whose registered transcript is still absent after this many failed tries
#: (about five minutes of backoff) is closed as `TranscriptGone` instead of retried every 15 minutes
#: for ever; each retry appended a `drain-failed` (73,618 of them for 976 pruned subagent files).
#: A source that comes back is observed again and re-queued (`queue_snapshot`).
GONE_AFTER_FAILURES = 5


# Metadata only, bounded independently of transcript sizes. A fresh process
# revalidates every source; no persisted capture claim is manufactured by this cache.
_PROJECTED = OrderedDict()
_PROJECTED_LOCK = threading.RLock()
MAX_PROJECTED = 4096


def _stamp(path):
    info = os.stat(path)
    if not stat.S_ISREG(info.st_mode):
        raise OSError("capture source must be a regular file")
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _absent(root, session, conn):
    """True only when neither the registered locator nor the retained copy exists at all.

    A directory, a special file or a dangling link at either place is a present but
    broken source, which must keep failing rather than become a recorded gap. Only
    ENOENT and ENOTDIR count as absent: a lookup that fails for another reason (a
    parent directory the collector cannot search) is a broken source too."""
    from alpaca import transcripts
    registered = transcripts.registered(conn, session)
    places = [transcripts.local_path(root, session)]
    if registered:
        places.append(registered if os.path.isabs(registered) else os.path.join(root, registered))
    for place in places:
        try:
            os.lstat(place)
        except OSError as error:
            if error.errno in (errno.ENOENT, errno.ENOTDIR):
                continue
            return False
        return False
    return True


class _SharedAbsorber(threading.local):
    """The collector's one wiki `Absorber`, lent to every capture of the process: the wiki consumer's
    batches and the preservation jobs (merge review B1). Its echo index (about 30 s and 3.6 GiB on
    the live store) is read once and then kept current by the absorber's own writes; when another
    connection committed to the store since the last lend (`PRAGMA data_version`, as main's
    snapshot batch did per job), the index, the projection inventory and the vocabulary are read
    again. A lend for another root or another wiki configuration, a store file that was replaced,
    or a connection left inside a transaction opens a fresh absorber and closes the old one (which
    rolls back, as closing the absorber after each capture did). One per thread, since a SQLite
    connection belongs to the thread that opened it."""

    absorber = None
    root = None
    version = None

    def lend(self, root):
        from alpaca.wiki.config import Config
        from alpaca.wiki.ingest.absorb import Absorber
        cfg = Config.for_project(root)
        held = self.absorber
        if held is not None and (self.root != root or held.cfg != cfg or not self._usable(held)):
            self.close()
            held = None
        if held is None:
            cfg.vault_dir.mkdir(parents=True, exist_ok=True)
            held = Absorber(cfg)
            held._capture_projections = {}
            self.absorber, self.root, self.version = held, root, None
        current = held.db.conn.execute('PRAGMA data_version').fetchone()[0]
        if self.version is not None and current != self.version:
            held._reload_echo_index()
            held._capture_projections.clear()
            held.vocab = {r['predicate'] for r in held.db.conn.execute('SELECT predicate FROM predicates')}
        self.version = current
        return held

    @staticmethod
    def _usable(held):
        if held.db.conn.in_transaction:
            return False
        try:
            info = os.stat(held.cfg.db_path)
        except OSError:
            return False
        return (info.st_dev, info.st_ino) == held.db._db_identity

    def settle(self):
        """After a batch: an absorber whose connection was left inside a transaction is closed, which
        rolls it back, as closing the absorber after each batch did; any other is kept for the next."""
        if self.absorber is not None and self.absorber.db.conn.in_transaction:
            self.close()

    def forget_projections(self):
        """After a failed capture: the projection inventory may stop between two writes, so the
        next capture reads it from the store again (main's snapshot batch did the same)."""
        if self.absorber is not None:
            self.absorber._capture_projections.clear()

    def close(self):
        held, self.absorber, self.root, self.version = self.absorber, None, None, None
        if held is not None:
            held.close()


_SHARED = _SharedAbsorber()


def _lanes():
    from alpaca.capture_runner import Lane
    return {'wiki': Lane('wiki'), 'views': Lane('views')}


#: The collector's capture lanes (t-070 RC4): `wiki` runs the wiki consumer's batches and the
#: preservation jobs, one at a time, on one thread that keeps `_SHARED`; `views` runs the pad and
#: analytics refresh, so a hung view never holds a capture.
LANES = _lanes()


class CaptureAbandoned(RuntimeError):
    """The wiki lane did not finish a capture within `CAPTURE_BUDGET_S` (or still runs one it
    abandoned before); the batch or job is retried after the backoff."""


class WikiWarming(RuntimeError):
    """The wiki lane is reading the shared absorber's echo index (`warm_wiki`): a state, not a
    failure. The capture or job did not start; the consumer keeps its checkpoint with no failure,
    issue, outcome record or backoff, and runs on the first pass after the read (t-462)."""


WARM = 'echo-index-warm-up'


class _WarmState:
    """What the warm-ups of one wiki lane came to: when the current `warming` streak began (None when
    the last pass found the index ready, or the warm-up failed) and when the last warm-up failed."""

    def __init__(self):
        self.streak = None
        self.failed_at = None
        self.error = None


_WARM_STATES = weakref.WeakKeyDictionary()
_WARM_STATES_LOCK = threading.Lock()


def _warm_state(lane):
    with _WARM_STATES_LOCK:
        state = _WARM_STATES.get(lane)
        if state is None:
            state = _WARM_STATES[lane] = _WarmState()
        return state


def _warm(root, state=None, progress=None):
    """On the wiki lane's thread: lend the shared absorber, which drops its echo index when another
    connection committed since its last lend, and read the index when it is not loaded. A failure is
    kept in `state` before it is raised: the pass that started the read has usually gone on, and the
    lane's own result of an abandoned job is read by no one."""
    try:
        _SHARED.lend(root).echo_index
        _SHARED.settle()
    except BaseException as exc:
        if state is not None:
            state.failed_at, state.error = time.monotonic(), type(exc).__name__
        raise


def _warming(lane, now=None):
    """True while the lane runs an echo index warm-up and its `warming` streak is under
    `WARM_CEILING_S`. Past the ceiling the lane counts as busy (review B-1)."""
    if lane.busy_with() != WARM:
        return False
    state = _warm_state(lane)
    now = time.monotonic() if now is None else now
    if state.streak is None:
        state.streak = lane.busy_since() or now
    return now - state.streak <= WARM_CEILING_S


def warm_wiki(root):
    """A watch collector's first step of every pass (t-462): read the echo index on the `wiki` lane,
    outside any capture budget, so no capture waits for it. After a restart the read of the live
    store (717,000 active blocks) takes minutes, and inside a capture's `CAPTURE_BUDGET_S` it was
    abandoned again and again: every pass recorded `capture-deferred` and a `consumer-failed`
    `CaptureAbandoned` until it ended (t-442 landing, 2026-10-08 15:37 to 15:44:51). The pass waits
    `WARM_WAIT_S` for it and goes on; while it runs, the wiki consumer and the preservation jobs are
    `warming` (`WikiWarming`). An index that is loaded and current costs one `PRAGMA data_version`.

    The state is bounded (review B-1): a warm-up that failed, even after the pass stopped waiting
    for it, makes the next passes `failed` for `WARM_RETRY_S` without another read, so the captures
    read the index inside their budget and record the failure; and a lane `warming` for longer than
    `WARM_CEILING_S` in a row (a hung read, or reads dropped again by other connections' commits) is
    `busy`, so the capture records `capture-deferred` and the consumer failure.

    Returns 'ready' (the index is loaded and current, or there is no store yet: the first capture
    creates it empty), 'warming' (the read goes on), 'busy' (the lane still runs a capture it
    abandoned before, or a warm-up past the ceiling) or 'failed' (the read raised, now or after an
    earlier pass went on; the captures read the index themselves and record what they come to)."""
    from alpaca.wiki.config import Config
    lane = LANES['wiki']
    state = _warm_state(lane)
    now = time.monotonic()
    if lane.busy_with() == WARM:
        return 'warming' if _warming(lane, now) else 'busy'
    if lane.busy_since() is not None:
        return 'busy'
    if state.failed_at is not None and now - state.failed_at < WARM_RETRY_S:
        state.streak = None
        return 'failed'
    root = os.path.realpath(root)
    try:
        if not Config.for_project(root).db_path.exists():
            state.streak = None
            return 'ready'
    except Exception:
        return 'failed'
    result = lane.run(_warm, root, state, budget_s=WARM_WAIT_S, label=WARM)
    if result.status == 'ok':
        state.streak = state.failed_at = state.error = None
        return 'ready'
    if result.status == 'overrun':
        if state.streak is None:
            state.streak = now
        return 'warming' if _warming(lane) or lane.busy_with() != WARM else 'busy'
    if result.status == 'failed':
        state.streak = None
        return 'failed'
    return 'busy'


@atexit.register
def _close_shared():
    try:
        _SHARED.close()
        lane = LANES['wiki']
        if lane.started() and lane.busy_since() is None:
            lane.run(_SHARED.close, budget_s=5.0)           # the lane thread's own absorber
    except Exception:
        pass


def _run_in_wiki_lane(root, session, fn, *args, batch=None, **kwargs):
    """Run `fn(*args, **kwargs)` in the `wiki` lane within `CAPTURE_BUDGET_S` and record what it came
    to through the one outcome record (`capture_runner.record`, where `collector`): `drain-failed`
    for a failure, `capture-deferred` for a capture the loop abandoned or that never started because
    the lane still runs an abandoned one. Returns the work's value; raises its exception, or
    `CaptureAbandoned`, so the caller's checkpoint or job stays and is retried."""
    from alpaca import capture_runner
    if _warming(LANES['wiki']):
        raise WikiWarming('wiki lane reading the echo index')
    result = LANES['wiki'].run(fn, *args, budget_s=CAPTURE_BUDGET_S, progress=True, **kwargs)
    try:
        capture_runner.record(root, session or 'observability-collector', 'collector', result,
                              batch=batch, quiet_s=OUTCOME_QUIET_S)
    except Exception:
        pass                                    # the obs_issue of the caller still names the fault
    if result.status == 'ok':
        state = _warm_state(LANES['wiki'])
        state.failed_at = state.error = None    # a capture read the index: warm-ups may run again
        return result.value
    if result.status == 'failed':
        raise result.exc
    raise CaptureAbandoned('wiki capture %s after %.0f s' % (result.status, result.seconds))


def _capture_wiki(root, rows, sessions, *, absorber_factory=None, unavailable=None,
                  deadline_s=CAPTURE_DEADLINE_S, progress=None):
    """Capture notes and changed transcripts through `service.capture`; remember only successful
    projections.

    The event notes always land first (FR-8), whatever state a transcript is in. A registered
    transcript that capture could not read (missing, unreadable, malformed, or present but broken)
    then raises `TranscriptUnavailable`, and a capture cut short by its deadline raises
    RuntimeError, so the caller's checkpoint or job stays and the batch is retried (idempotent).
    A caller that passes a list as `unavailable` takes over sessions whose registered source and
    retained copy are both absent: each is appended there and its transcript is not attempted, and
    the caller must record it as a gap. Malformed or present-but-broken sources still raise.
    The snapshot queue uses the same cache, so unchanged native content is not
    normalized again for every old operation event or preservation request.
    """
    from alpaca import paths
    from alpaca.capture_io import checked_path
    from alpaca.wiki import service
    root = os.path.realpath(root)
    conn = db.connect_readonly(root)
    changed, stamps = [], {}
    try:
        for session in dict.fromkeys(sessions):
            try:
                source = service._transcript_source(root, session, conn)
            except service.TranscriptUnavailable:
                if unavailable is not None and _absent(root, session, conn):
                    unavailable.append(session)
                else:
                    # Capture reports it after the notes land, and it raises below.
                    changed.append(session)
                continue
            if not source:
                continue
            before = (source, _stamp(source))
            identity = (root, session)
            with _PROJECTED_LOCK:
                cached = _PROJECTED.get(identity)
            current = False
            if cached and cached[0] == before:
                try:
                    current = _stamp(checked_path(root, cached[1])) == cached[2]
                except (OSError, ValueError):
                    pass
            if not current:
                changed.append(session)
                stamps[session] = before
            elif cached:
                with _PROJECTED_LOCK:
                    if identity in _PROJECTED:
                        _PROJECTED.move_to_end(identity)
    finally:
        conn.close()
    absorber = absorber_factory() if absorber_factory and (rows or changed) else None
    result = service.capture(root, events=rows, sessions=changed, deadline_s=deadline_s,
                             min_documents=1, absorber=absorber,
                             projection_index=getattr(absorber, '_capture_projections', None),
                             progress=progress)
    vault = Path(paths.runtime_dir(root), 'wiki')     # the wiki vault (Config.for_project)
    for fact in result.get('transcripts', []):
        before = stamps.get(fact['session'])
        if before is None:
            continue
        try:
            # the path this pass resolved and stamped, not the summary's `source`: that is the
            # portable `transcript:<session>` ref since t-417 and names no file
            after = (before[0], _stamp(before[0]))
            target = checked_path(root, vault / fact['doc_id'])
            target_stamp = _stamp(target)
        except (OSError, ValueError):
            continue
        if before == after and after[1][2] == fact['source_bytes']:
            with _PROJECTED_LOCK:
                _PROJECTED[(root, fact['session'])] = (before, str(target), target_stamp)
                _PROJECTED.move_to_end((root, fact['session']))
                while len(_PROJECTED) > MAX_PROJECTED:
                    _PROJECTED.popitem(last=False)
    if result['missing_transcripts']:
        # Names the sessions only, never a locator: the consumer records the type name alone.
        raise service.TranscriptUnavailable('registered transcript unavailable: ' + ', '.join(
            sorted({str(item['session']) for item in result['missing_transcripts']})))
    if result['deferred']:
        raise RuntimeError('wiki capture incomplete')
    return result


def _wiki(root, rows):
    """Land the new rows as event notes and their sessions' transcripts, through service.capture.

    The event notes land first (FR-8). A registered transcript that is absent (neither the
    locator nor the retained copy exists) becomes an explicit gap: one `transcript-unavailable`
    issue and one queued snapshot retry per session, committed before this returns, so before
    `consume` advances the checkpoint. The issue names the session only, never the private
    locator, and a restored original clears it through the normal `run_snapshot_jobs` success
    path. A present-but-broken or malformed transcript fails the consumer so the checkpoint stays
    and the batch is retried (the retry is idempotent). Gaps found in a batch that then fails are
    still recorded, since they hold either way. The capture has no deadline of its own
    (`WIKI_CAPTURE_DEADLINE_S`) and borrows the process's shared absorber (`_SharedAbsorber`), so
    the echo index is read once per process, not once per batch; a watch collector reads it before
    the first batch, outside the budget (`warm_wiki`), and the batch waits as `warming` meanwhile.

    It runs in the `wiki` lane (t-070 RC4): the loop waits at most `CAPTURE_BUDGET_S`, then abandons
    the batch (`CaptureAbandoned`, so the checkpoint stays and the batch is retried after the
    backoff) while the other consumers go on, and the outcome is recorded as the hooks record theirs
    (`drain-failed`, `capture-deferred`, where `collector`)."""
    _run_in_wiki_lane(root, rows[0]["session"] if rows else None, _wiki_batch, root, rows,
                      batch=len(rows))


def _wiki_batch(root, rows, progress=None):
    """`_wiki`'s work, on the `wiki` lane's thread (whose `_SHARED` absorber it lends)."""
    gaps = []
    try:
        _capture_wiki(root, rows, [row["session"] for row in rows], unavailable=gaps,
                      absorber_factory=lambda: _SHARED.lend(os.path.realpath(root)),
                      deadline_s=WIKI_CAPTURE_DEADLINE_S, progress=progress)
    except BaseException:
        _SHARED.forget_projections()
        raise
    finally:
        _SHARED.settle()
        if gaps:
            _record_gaps(root, rows, gaps)
    # FR-18: the same bounded consolidation step as the SessionEnd hook, under the same key
    # (`consolidate_after_capture`, default false). It proposes and admits nothing, and its failure
    # never fails this consumer (architecture.md 9.2).
    from alpaca.wiki import consolidate
    consolidate.run_step_after_capture(root, where="collector_consolidate",
                                       session=rows[-1]["session"], deadline_s=CONSOLIDATE_DEADLINE_S)


def _record_gaps(root, rows, gaps):
    last = {}
    for row in rows:
        last[row["session"]] = max(last.get(row["session"], 0), row["id"])
    conn = db.connect(root)
    try:
        with db.transaction(conn):
            for session in gaps:
                queue_snapshot(conn, session, event_id=last.get(session, 0))
                issue(conn, 'transcript-unavailable', identity=session,
                      detail='wiki: registered transcript unavailable for session ' + session
                      + '; snapshot retry queued')
    finally:
        conn.close()


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


def _mission(root, rows):
    from alpaca import mission
    mission.scan(root, session='observability-collector')


#: t-334: the record views the hooks stop refreshing once `.alpaca/observability/enabled` is on
#: (`stop.handle` and `session_end.handle` return early): the pad (RESUME.md, CHECKLIST.md,
#: board.json) at a session end, and the analytics page at a session end and at every
#: `ANALYTICS_EVERY_TURNS`-th turn end of a session (the Stop hook's `EVERY`). Each view runs at most
#: once per interval; a trigger inside the interval stays pending and runs when it has passed, on a
#: later batch or an idle pass. One analytics build read the live record (813 sessions) in about
#: 23 s of CPU on 2026-10-07, so it is held to one build per 10 minutes.
VIEW_INTERVALS_S = {'pad': 60.0, 'analytics': 600.0}
ANALYTICS_EVERY_TURNS = 10
VIEWS_STATE_KEY = 'obs:session-views'


def _write_pad(root):
    from alpaca import pad
    pad.write(root)


def _build_analytics(root):
    from alpaca.analytics import build_index
    build_index.build(root)


VIEW_WRITERS = {'pad': _write_pad, 'analytics': _build_analytics}


class ViewAbandoned(RuntimeError):
    """A view refresh the `views` lane did not finish within `VIEW_BUDGET_S` (or one it still runs
    from before): the view stays pending and is tried again after its interval."""


def _run_view(writer, root):
    """One view refresh in the `views` lane (t-070 RC4): the loop waits at most `VIEW_BUDGET_S`, so a
    build that hangs (a transcript read that never returns) holds neither the loop nor a capture."""
    result = LANES['views'].run(writer, root, budget_s=VIEW_BUDGET_S)
    if result.status == 'ok':
        return
    if result.status == 'failed':
        raise result.exc
    raise ViewAbandoned('view refresh %s after %.0f s' % (result.status, result.seconds))


def _views_due(conn, rows):
    """The views the rows ask for: both at a session end, analytics at every tenth turn end of a
    session, counted in the record (so a retried batch counts the same)."""
    due, turns = set(), {}
    for row in rows:
        session = row.get('session')
        if row['kind'] == 'session-end':
            due.update(VIEW_WRITERS)
        elif row['kind'] == 'turn-end' and session:
            if session not in turns:
                turns[session] = conn.execute(
                    "SELECT COUNT(*) FROM events WHERE session=? AND id<? AND kind='turn-end'",
                    (session, row['id'])).fetchone()[0]
            turns[session] += 1
            if turns[session] % ANALYTICS_EVERY_TURNS == 0:
                due.add('analytics')
    return due


def _refresh_views(root, rows, now=None):
    """Mark the views the rows ask for as pending and run each pending view whose interval has
    passed since its last attempt. A view that fails is recorded as one open issue
    (`<view>-refresh-failed`, the exception type only), stays pending and is tried again after its
    interval; a success resolves that issue. Never raises for a view's failure, so the consumer's
    checkpoint moves on and no other consumer waits on it."""
    from . import _id
    conn = db.connect(root)
    try:
        raw = db.meta_get(conn, VIEWS_STATE_KEY, '{}')
        try:
            state = json.loads(raw)
        except ValueError:
            state = {}
        state = {name: dict(state[name]) if isinstance(state, dict) and isinstance(state.get(name), dict)
                 else {} for name in VIEW_WRITERS}
        for name in _views_due(conn, rows):
            state[name]['pending'] = True
        for name, writer in VIEW_WRITERS.items():
            view = state[name]
            last = view.get('last_attempt')
            if not view.get('pending') or (last and (_time(now) - _time(last)).total_seconds()
                                           < VIEW_INTERVALS_S[name]):
                continue
            view['last_attempt'] = _time(now).isoformat()
            try:
                _run_view(writer, root)
            except Exception as exc:
                view['error'] = type(exc).__name__
                with db.transaction(conn):
                    issue(conn, name + '-refresh-failed', identity=name,
                          detail='collector %s refresh: %s' % (name, type(exc).__name__))
            else:
                view.update(pending=False, error=None, last_ok=view['last_attempt'])
                with db.transaction(conn):
                    conn.execute('UPDATE obs_issue SET resolved_at=? WHERE issue_id=? AND resolved_at IS NULL',
                                 (util.now_iso(), _id(None, name + '-refresh-failed', name)))
        encoded = json.dumps(state, sort_keys=True)
        if encoded != raw:
            with db.transaction(conn):
                db.meta_set(conn, VIEWS_STATE_KEY, encoded)
        return state
    finally:
        conn.close()


def _session_views(root, rows):
    """The pad and analytics refresh the hooks leave to the collector (VIEW_INTERVALS_S). With
    observability off the hooks still refresh both, so this does nothing."""
    from . import enabled
    if enabled(root):
        _refresh_views(root, rows)


def _session_views_idle(root):
    """A pass with no new event still runs a pending view whose interval has passed, so the last
    session end of the day is not left waiting for the next event."""
    from . import enabled
    if enabled(root):
        _refresh_views(root, [])


CONSUMERS = {'wiki': _wiki, 'snapshot-requests': _snapshots, 'projection': _projection, 'profile': _profile,
             'mission': _mission, 'session-views': _session_views}
#: Consumers that also run on a pass with no new event (no checkpoint move).
IDLE = {'session-views': _session_views_idle}


def consume(root, conn, *, max_records=200, retry_consumers=()):
    retry_consumers = set(retry_consumers)
    if retry_consumers - CONSUMERS.keys():
        raise ValueError("unknown consumer retry")
    result = {}
    for name, callback in CONSUMERS.items():
        prior = conn.execute('SELECT * FROM obs_consumer_checkpoint WHERE consumer=?', (name,)).fetchone()
        prior = dict(prior) if prior else {'event_id':0,'observation_id':0,'failures':0}
        if name not in retry_consumers and prior.get('next_attempt') and _time(prior['next_attempt']) > _time():
            result[name] = {'status':'backoff', 'through_event':prior['event_id']}
            continue
        events = [dict(r) for r in conn.execute('SELECT * FROM events WHERE id>? ORDER BY id LIMIT ?', (prior['event_id'], max_records))]
        if not events:
            if name in IDLE:
                try:
                    IDLE[name](root)
                except Exception as exc:
                    with db.transaction(conn):
                        issue(conn,'consumer-failed',detail=name+': '+type(exc).__name__,identity=name)
            result[name] = {'status':'current', 'through_event':prior['event_id']}
            continue
        for event in events:
            event['data'] = json.loads(event['data'])
        try:
            callback(root,events)
        except WikiWarming:
            # t-462: the wiki lane reads the echo index; a state, so nothing is counted or recorded.
            result[name] = {'status':'warming', 'through_event':prior['event_id']}
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


def _source_gone(root, conn, session):
    """True when the session's registered transcript is not a file (none registered counts as gone)."""
    from alpaca import transcripts
    src = transcripts.registered(conn, session)
    if src and not os.path.isabs(src):
        src = os.path.join(root, src)
    return not (src and os.path.isfile(src))


def run_snapshot_jobs(root, conn, *, limit=8, stop_requested=None):
    """One unavailable source cannot prevent preservation of another session."""
    from alpaca import transcripts
    from . import _id
    jobs = [dict(r) for r in conn.execute("SELECT * FROM obs_projection_job WHERE status!='done' AND (next_attempt IS NULL OR next_attempt<=?) ORDER BY updated_at LIMIT ?", (_time().isoformat(),limit))]
    # The jobs borrow the process's shared absorber (`_SharedAbsorber`, the wiki consumer's too) on
    # the `wiki` lane's thread (t-070 RC4): each job, the snapshot copy included (a transcript read
    # that hangs), runs there within `CAPTURE_BUDGET_S`; one the loop abandons is retried like a
    # failed one. External commits invalidate the absorber's in-memory witnesses before the next
    # source; normal absorber writes update them directly.
    def preserve(session, progress=None):
        used = []

        def shared_absorber():
            used.append(True)
            return _SHARED.lend(os.path.realpath(root))
        try:
            fact = transcripts.snapshot(root, session)
            if fact.get('mode') in ('none','busy') or fact.get('source_missing'):
                raise OSError('snapshot unavailable; retry required')
            # New transcript observations may arrive without any new operation event.
            # Treat preservation plus wiki projection as one retryable job; a transcript
            # capture could not read, or a capture cut by its deadline, raises in it.
            _capture_wiki(root, [], [session], absorber_factory=shared_absorber, progress=progress)
            return fact
        except BaseException:
            if used:
                # A failed job may have stopped between two writes; the next job reads
                # the projection inventory from the store again.
                _SHARED.forget_projections()
            raise
        finally:
            _SHARED.settle()
    result = {'completed':0,'failed':0}
    for job in jobs:
        if stop_requested and stop_requested():
            break
        if job['failures'] >= GONE_AFTER_FAILURES and _source_gone(root, conn, job['session']):
            # Nothing is left to copy: close the job without a lane run, so no outcome event is
            # appended. The gap stays visible as one `transcript-gone` issue for the session.
            retained = os.path.isfile(transcripts.local_path(root, job['session']))
            with db.transaction(conn):
                conn.execute("UPDATE obs_projection_job SET status='done',next_attempt=NULL,error='TranscriptGone',updated_at=? WHERE job_key=?",(util.now_iso(),job['job_key']))
                conn.execute('UPDATE obs_issue SET resolved_at=? WHERE issue_id=? AND resolved_at IS NULL',(util.now_iso(),_id(None,'snapshot-failed',job['job_key'])))
                issue(conn,'transcript-gone',identity=job['session'],
                      detail='registered transcript absent after %d tries; local copy %s' % (job['failures'],'retained' if retained else 'absent'))
            result['gone'] = result.get('gone',0)+1
            continue
        try:
            fact = _run_in_wiki_lane(root, job['session'], preserve, job['session'])
            if any(row.get('mode') in ('busy','missing','truncated') for row in fact.get('registry_children',[])):
                # Each child gets its own retry job, so parent progress is independent.
                with db.transaction(conn):
                    for child in fact.get('registry_children',[]):
                        if child.get('session') and child.get('mode') in ('busy','missing'):
                            queue_snapshot(conn,child['session'],event_id=job['event_id'])
        except WikiWarming:
            # t-462: the job did not start; it stays as it is for the first pass after the read.
            result['warming'] = True
            break
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
                # A restored original also closes the wiki consumer's recorded gap.
                conn.execute('UPDATE obs_issue SET resolved_at=? WHERE issue_id=? AND resolved_at IS NULL',(util.now_iso(),_id(None,'transcript-unavailable',job['session'])))
                # A transcript that came back after a TranscriptGone close also closes that gap.
                conn.execute('UPDATE obs_issue SET resolved_at=? WHERE issue_id=? AND resolved_at IS NULL',(util.now_iso(),_id(None,'transcript-gone',job['session'])))
            result['completed'] += 1
        # Large catch-up batches must report actual progress between jobs,
        # rather than appearing stale while the supervised process is working.
        heartbeat = json.loads(db.meta_get(conn, 'obs:collector', '{}'))
        if heartbeat.get('pid') == os.getpid():
            heartbeat.update(heartbeat=util.now_iso(), preservation_progress={**result, 'jobs':len(jobs)})
            with db.transaction(conn):
                db.meta_set(conn, 'obs:collector', json.dumps(heartbeat))
    return result
