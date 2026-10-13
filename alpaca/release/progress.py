"""Live progress of a running release step, and its estimated time left (op-056 t-614).

A running build, check, preflight or publish writes .alpaca/releases/running/<id>.json as its steps
start and finish, and rewrites it every HEARTBEAT_S while it runs; it removes the file when it ends.
The hub reads that folder (current) and turns each file into a percent done and a time left
(estimate), from the step durations that earlier check and preflight receipts recorded (history). A
file whose heartbeat stopped is reported as ended, then dropped, so a bar never stays stuck on a step
that died. The file is advisory: a failed write never stops the release step."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import statistics
import threading
import time
import uuid

RUNNING = '.alpaca/releases/running'
RECEIPTS = '.alpaca/releases/receipts'
#: The last receipts of each step name the median duration comes from.
HISTORY_N = 5
#: A running step past its usual duration is counted as this many seconds from done.
OVERRUN_S = 30
#: Seconds between rewrites of a running step's file.
HEARTBEAT_S = 30
#: A file not rewritten for this long belongs to a step that died (its process is gone, whatever its
#: pid now names).
STALE_S = 4 * HEARTBEAT_S
#: A step that died stays listed as ended this long after its last heartbeat, then disappears.
ENDED_SHOWN_S = 600
DONE = ('PASS', 'FAIL', 'SKIPPED', 'STOPPED')


def _now():
    return datetime.now(timezone.utc).isoformat()


def _epoch(stamp):
    try:
        return datetime.fromisoformat(stamp).timestamp()
    except (TypeError, ValueError):
        return None


class Progress:
    """The progress file of one running release step. Thread safe: parallel gates report through it."""

    def __init__(self, root, kind, source_sha, steps, parallel=1, sequential=()):
        self.path = Path(root).resolve() / RUNNING / (uuid.uuid4().hex + '.json')
        self.lock = threading.Lock()
        self.state = {'kind': kind, 'source_sha': source_sha, 'pid': os.getpid(), 'started': _now(),
                      'parallel': parallel, 'sequential': list(sequential),
                      'steps': [{'name': name, 'status': 'pending'} for name in steps]}
        self.stopped = threading.Event()
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            prune(self.path.parent)
        except OSError:
            pass
        self._write()
        self.beat = threading.Thread(target=self._heartbeat, name='release-progress', daemon=True)
        self.beat.start()

    def _heartbeat(self):
        while not self.stopped.wait(HEARTBEAT_S):
            with self.lock:
                self._write()

    def _write(self):
        self.state['updated'] = _now()
        temp = self.path.with_suffix('.tmp-' + uuid.uuid4().hex)
        try:
            temp.write_text(json.dumps(self.state, sort_keys=True))
            os.replace(temp, self.path)
        except OSError:
            # advisory: a full disk or a removed folder costs the bar, never the release step
            try:
                temp.unlink()
            except OSError:
                pass

    def _step(self, name):
        for step in self.state['steps']:
            if step['name'] == name:
                return step
        step = {'name': name, 'status': 'pending'}
        self.state['steps'].append(step)
        return step

    def start(self, name):
        with self.lock:
            self._step(name).update(status='running', started=_now())
            self._write()

    def drop(self, name):
        """Take a step off the list: a gate carried over from an earlier check does not run, so it
        takes no part in the percent or the time left. It stays named in state['carried']."""
        with self.lock:
            self.state['steps'] = [s for s in self.state['steps'] if s['name'] != name]
            self.state.setdefault('carried', []).append(name)
            self._write()

    def finish(self, name, status, duration_s=None):
        with self.lock:
            step = self._step(name)
            step['status'] = status
            if duration_s is None and step.get('started'):
                duration_s = round(time.time() - _epoch(step['started']), 1)
            if duration_s is not None:
                step['duration_s'] = duration_s
            self._write()

    def close(self):
        self.stopped.set()
        with self.lock:
            try:
                self.path.unlink()
            except OSError:
                pass


class _Quiet:
    """Stands in for Progress when nothing records progress (library calls and tests)."""
    def start(self, name):
        pass

    def finish(self, name, status, duration_s=None):
        pass

    def drop(self, name):
        pass


QUIET = _Quiet()


def age(state, now=None):
    """Seconds since a progress state's last write, or None when it has no readable stamp."""
    updated = _epoch(state.get('updated'))
    return None if updated is None else (time.time() if now is None else now) - updated


def prune(folder, now=None):
    """Remove the files of steps whose heartbeat stopped longer ago than ENDED_SHOWN_S, and leftover
    temporary files of a crashed write."""
    now = time.time() if now is None else now
    for path in folder.iterdir():
        try:
            if '.tmp-' in path.name:
                if now - path.stat().st_mtime > STALE_S:
                    path.unlink()
            elif path.suffix == '.json':
                seconds = age(json.loads(path.read_text()), now)
                if seconds is None or seconds > STALE_S + ENDED_SHOWN_S:
                    path.unlink()
        except (OSError, ValueError):
            pass


_HISTORY = {'key': None, 'value': {}}


def history(root):
    """{step name: median seconds} over the last HISTORY_N passing runs of each step in the recorded
    check and preflight receipts. Cached until the receipt folder changes."""
    folder = Path(root).resolve() / RECEIPTS
    try:
        names = sorted(p.name for p in folder.iterdir() if p.name.endswith(('-check.json', '-preflight.json')))
    except OSError:
        return {}
    key = (str(folder), tuple(names))
    if _HISTORY['key'] == key:
        return _HISTORY['value']
    runs = []
    for name in names:
        try:
            data = json.loads((folder / name).read_text())
        except (OSError, ValueError):
            continue
        runs.append((data.get('created_at') or '', data.get('gates') or []))
    seen = {}
    for _, gates in sorted(runs, key=lambda r: r[0]):
        for gate in gates:
            if (gate.get('status') == 'PASS' and 'carried' not in gate
                    and isinstance(gate.get('duration_s'), (int, float))):
                seen.setdefault(gate['name'], []).append(float(gate['duration_s']))
    value = {name: statistics.median(times[-HISTORY_N:]) for name, times in seen.items()}
    _HISTORY.update(key=key, value=value)
    return value


def estimate(state, known, now=None):
    """Percent done and seconds left for one progress state, from known step durations.

    Steps named in state['sequential'] run one at a time in their listed places; the others run
    state['parallel'] at once in listed order (the release gates). A step with no known duration
    makes the time left unknown (None), and the percent then counts steps instead of seconds."""
    now = time.time() if now is None else now
    steps = state.get('steps') or []
    sequential = set(state.get('sequential') or [])
    lanes = max(1, int(state.get('parallel') or 1))
    unknown = [s['name'] for s in steps if s.get('status') not in DONE and s['name'] not in known]

    def left(step):
        if step.get('status') in DONE:
            return 0.0
        usual = known.get(step['name'])
        if usual is None:
            return None
        if step.get('status') == 'running':
            began = _epoch(step.get('started'))
            ran = max(0.0, now - began) if began else 0.0
            return max(usual - ran, OVERRUN_S if ran > usual else 0.0)
        return usual

    total_steps = len(steps) or 1
    finished = sum(1 for s in steps if s.get('status') in DONE)
    if unknown:
        return {'percent': round(100.0 * finished / total_steps, 1), 'seconds_left': None, 'unknown': unknown}

    # Seconds left: walk the plan; sequential steps add up, a run of parallel steps is list-scheduled.
    seconds, pool = 0.0, []

    def flush():
        nonlocal seconds
        if pool:
            lanes_end = [0.0] * lanes
            for cost in pool:
                i = lanes_end.index(min(lanes_end))
                lanes_end[i] += cost
            seconds += max(lanes_end)
            pool.clear()

    for step in steps:
        cost = left(step)
        if step['name'] in sequential:
            flush()
            seconds += cost
        else:
            # A running gate holds a lane from now; pending ones queue behind it.
            pool.insert(0, cost) if step.get('status') == 'running' else pool.append(cost)
    flush()
    whole = sum(known.get(s['name'], 0.0) for s in steps) or 1.0
    spent = max(0.0, whole - sum(left(s) for s in steps))
    return {'percent': round(min(99.0, 100.0 * spent / whole), 1) if finished < len(steps) else 100.0,
            'seconds_left': round(seconds), 'unknown': []}


def current(root, now=None):
    """The running release steps, newest first, each with its estimate; a step whose process is gone
    is listed with state 'ended' and no estimate."""
    folder = Path(root).resolve() / RUNNING
    try:
        files = [p for p in folder.iterdir() if p.suffix == '.json']
    except OSError:
        return []
    out = []
    known = None
    for path in files:
        try:
            state = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        step = next((s['name'] for s in state.get('steps', []) if s.get('status') == 'running'), None)
        entry = {'kind': state.get('kind'), 'source_sha': state.get('source_sha'), 'started': state.get('started'),
                 'updated': state.get('updated'), 'step': step,
                 'steps': [{k: s.get(k) for k in ('name', 'status', 'duration_s')} for s in state.get('steps', [])]}
        seconds = age(state, now)
        if seconds is None or seconds > STALE_S:
            if seconds is None or seconds > STALE_S + ENDED_SHOWN_S:
                continue
            entry.update(state='ended', percent=None, seconds_left=None)
        else:
            if known is None:
                known = history(root)
            entry.update(state='running', **estimate(state, known, now))
        out.append(entry)
    return sorted(out, key=lambda e: e.get('started') or '', reverse=True)
