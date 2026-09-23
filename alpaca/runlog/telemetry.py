"""Host and process sampling for the duration of one long job.

A long job (a build, a test campaign, a backend run) is often the longest thing a project
does, and nothing else records what the machine was doing while it ran. A caller starts one
daemon thread that appends a sample line to a JSON-lines file it names (for example
`<jobs dir>/<job_id>.telemetry.jsonl`) every ten seconds, so a job that swapped, ran out of disk
or sat behind another load can be read back afterwards instead of guessed at.
`ALPACA_TELEMETRY_S` overrides the interval; tests set it low. The caller sets `Sampler.stage` as
its job moves between stages; the sampler only records the label.

Every sample carries the same eleven keys. Reading `/proc` can fail at any moment (a process
exits between the directory listing and the read, a container hides a file), so a failed read
writes null for that field. The sampler never raises into the job: a missing sample is a hole
in the data, not a failed stage.

Linux only. On a host without `/proc` the fields stay null and the file still holds one line
per interval, which keeps the reader's shape the same everywhere.

The file path, the process root and the disk to watch are parameters, so a domain profile
decides where its jobs keep their samples.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import threading
import time

DEFAULT_INTERVAL_S = 10.0
KEYS = ("ts", "stage", "load1", "load5", "mem_total_kb", "mem_available_kb", "swap_free_kb",
        "tree_rss_kb", "tree_procs", "cpu_busy_pct", "disk_free_kb")


def interval_s():
    """The sample period: ten seconds, or whatever `ALPACA_TELEMETRY_S` names."""
    try:
        value = float(os.environ.get("ALPACA_TELEMETRY_S") or DEFAULT_INTERVAL_S)
    except ValueError:
        return DEFAULT_INTERVAL_S
    return value if value > 0 else DEFAULT_INTERVAL_S


# --------------------------------------------------------------------------- /proc readers


def _loadavg():
    try:
        fields = Path("/proc/loadavg").read_text().split()
        return float(fields[0]), float(fields[1])
    except (OSError, IndexError, ValueError):
        return None, None


def _meminfo():
    wanted = {"MemTotal:": "mem_total_kb", "MemAvailable:": "mem_available_kb",
              "SwapFree:": "swap_free_kb"}
    out = {name: None for name in wanted.values()}
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            head = line.split(None, 1)[0]
            if head in wanted:
                out[wanted[head]] = int(line.split()[1])
    except (OSError, IndexError, ValueError):
        pass
    return out


def _ppid(pid):
    text = Path("/proc/%d/stat" % pid).read_text()
    return int(text[text.rfind(")") + 2:].split()[1])


def descendants(root_pid):
    """Every live process under one pid, the pid itself included."""
    children = {}
    try:
        entries = [int(name) for name in os.listdir("/proc") if name.isdigit()]
    except OSError:
        return {int(root_pid)}
    for pid in entries:
        try:
            children.setdefault(_ppid(pid), []).append(pid)
        except (OSError, IndexError, ValueError):
            continue
    seen, queue = set(), [int(root_pid)]
    while queue:
        pid = queue.pop()
        if pid in seen:
            continue
        seen.add(pid)
        queue.extend(children.get(pid, ()))
    return seen


def _tree_rss(root_pid):
    try:
        page_kb = os.sysconf("SC_PAGE_SIZE") // 1024
    except (OSError, ValueError):
        return None, None
    pids = descendants(root_pid)
    total, counted = 0, 0
    for pid in pids:
        try:
            resident = int(Path("/proc/%d/statm" % pid).read_text().split()[1])
        except (OSError, IndexError, ValueError):
            continue
        total += resident * page_kb
        counted += 1
    if not counted:
        return None, None
    return total, counted


def _cpu_totals():
    fields = Path("/proc/stat").read_text().split("\n", 1)[0].split()
    values = [int(v) for v in fields[1:]]
    idle = values[3] + (values[4] if len(values) > 4 else 0)
    return sum(values), idle


def _disk_free_kb(path):
    try:
        stat = os.statvfs(str(path))
    except OSError:
        return None
    return int(stat.f_bavail * stat.f_frsize // 1024)


# --------------------------------------------------------------------------- the sampler


class Sampler:
    """One daemon thread appending job samples. `stage` is set by the caller as stages run."""

    def __init__(self, path, *, root_pid=None, disk_path=None, interval=None, name=None):
        self.path = Path(path)
        self.name = name or self.path.name
        self.root_pid = int(root_pid or os.getpid())
        self.disk_path = Path(disk_path or self.path.parent)
        self.interval = float(interval or interval_s())
        self.stage = None
        self._stop = threading.Event()
        self._thread = None
        self._cpu = None
        self._emit_lock = threading.Lock()
        self._health = {"attempted_samples": 0, "written_samples": 0, "dropped_samples": 0,
                        "incomplete_samples": 0, "status_write_failures": 0, "start_failures": 0, "last_error": None}

    def sample(self):
        """One reading of the host and the worker's process tree. Never raises."""
        load1, load5 = _loadavg()
        rss, procs = _tree_rss(self.root_pid)
        row = {"ts": time.time(), "stage": self.stage, "load1": load1, "load5": load5,
               "tree_rss_kb": rss, "tree_procs": procs, "cpu_busy_pct": self._cpu_busy(),
               "disk_free_kb": _disk_free_kb(self.disk_path)}
        row.update(_meminfo())
        return {key: row.get(key) for key in KEYS}

    def _cpu_busy(self):
        try:
            total, idle = _cpu_totals()
        except (OSError, IndexError, ValueError):
            return None
        previous, self._cpu = self._cpu, (total, idle)
        if previous is None or total <= previous[0]:
            return None
        busy = (total - previous[0]) - (idle - previous[1])
        return round(max(0.0, min(100.0, 100.0 * busy / (total - previous[0]))), 2)

    def health(self):
        with self._emit_lock:
            return dict(self._health)

    def _report_failure(self, component, error):
        self._health["last_error"] = {"component": component, "type": type(error).__name__, "at": time.time()}
        sys.stderr.write("alpaca telemetry: %s failure (%s)\n" % (component, type(error).__name__))

    def _save_health(self):
        # Same directory and atomic replacement. If disk/permissions prevent this too,
        # the counter stays in memory and a bounded, payload-free stderr record remains.
        target = self.path.with_suffix(self.path.suffix + ".health.json")
        temporary = target.with_suffix(target.suffix + ".tmp")
        try:
            with temporary.open("w", encoding="ascii") as handle:
                handle.write(json.dumps(self._health, sort_keys=True, ensure_ascii=True) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
        except OSError as error:
            self._health["status_write_failures"] += 1
            self._report_failure("health_write", error)

    def emit(self):
        with self._emit_lock:
            self._health["attempted_samples"] += 1
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                row = self.sample()
                line = (json.dumps(row, sort_keys=True, ensure_ascii=True, allow_nan=False) + "\n").encode("ascii")
                with self.path.open("a+b", buffering=0) as handle:
                    # A previous interrupted/short append must not consume the next sample.
                    handle.seek(0, os.SEEK_END)
                    if handle.tell():
                        handle.seek(-1, os.SEEK_END)
                        if handle.read(1) != b"\n" and handle.write(b"\n") != 1:
                            raise OSError("short telemetry separator")
                    if handle.write(line) != len(line):
                        raise OSError("short telemetry append")
                    os.fsync(handle.fileno())
                self._health["written_samples"] += 1
                if any(row[key] is None for key in KEYS if key not in ("stage", "cpu_busy_pct")):
                    self._health["incomplete_samples"] += 1
                written = True
            except Exception as error:
                self._health["dropped_samples"] += 1
                self._report_failure("sample_append", error)
                written = False
            self._save_health()
            return written

    def _loop(self):
        while not self._stop.is_set():
            self.emit()
            self._stop.wait(self.interval)
        # A closing job sample cannot establish a peak for a stage that already ended.
        self.emit()

    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="alpaca-runlog-telemetry",
                                            daemon=True)
            try:
                self._thread.start()
            except Exception as error:
                self._thread = None
                with self._emit_lock:
                    self._health["start_failures"] += 1
                    self._report_failure("sampler_start", error)
                    self._save_health()
                raise
        return self

    def stop(self, *, timeout=5.0):
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=timeout)


def start(path, **kwargs):
    """A started sampler for one job."""
    return Sampler(path, **kwargs).start()


# --------------------------------------------------------------------------- readers


def _read(path):
    rows = []
    diagnostics = {"malformed_lines": 0, "partial_tail": False, "read_error": None}
    try:
        text = Path(path).read_text(errors="replace")
    except OSError as error:
        diagnostics["read_error"] = type(error).__name__
        return rows, diagnostics
    lines = text.splitlines(keepends=True)
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        if index == len(lines) - 1 and not line.endswith("\n"):
            diagnostics["partial_tail"] = True
            continue
        try:
            row = json.loads(line)
            valid = isinstance(row, dict) and isinstance(row.get("ts"), (int, float)) and not isinstance(row.get("ts"), bool)
        except ValueError:
            valid = False
        if valid:
            rows.append(row)
        else:
            diagnostics["malformed_lines"] += 1
    return rows, diagnostics


def read(path):
    """Read complete samples; retain malformed/partial counts in summary diagnostics."""
    return _read(path)[0]


def _measurement(samples):
    return {"status": "sampled" if samples else "unavailable",
            "rss": "sampled_process_tree_sum_may_double_count_shared_pages",
            "cpu": "host_busy_percent_not_job_cpu"}


def _terminal(start_ts, end_ts):
    return {"started_at": start_ts, "finished_at": end_ts,
            "wall_s": round(float(end_ts) - float(start_ts), 3),
            "cpu_s": None, "cpu_status": "unavailable_not_instrumented"}


def _peak(rows, key, pick=max):
    values = [row[key] for row in rows
              if isinstance(row.get(key), (int, float)) and not isinstance(row.get(key), bool)]
    return pick(values) if values else None


def window(path, start_ts, end_ts):
    """The resources of one stage window [start_ts, end_ts], read back from the file."""
    rows = [row for row in read(path) if start_ts <= row["ts"] <= end_ts]
    return {"wall_s": round(float(end_ts) - float(start_ts), 3),
            "peak_tree_rss_kb": _peak(rows, "tree_rss_kb"),
            "max_load1": _peak(rows, "load1"),
            "samples": len(rows), "measurement": _measurement(len(rows)),
            "terminal": _terminal(start_ts, end_ts)}


def empty_window(start_ts, end_ts):
    """The same shape for a stage window that ran without a sampler."""
    return {"wall_s": round(float(end_ts) - float(start_ts), 3), "peak_tree_rss_kb": None,
            "max_load1": None, "samples": 0, "measurement": _measurement(0),
            "terminal": _terminal(start_ts, end_ts)}


def summary(path, *, name=None):
    """What the whole job did to the machine; `name` is the path to report (default: `path`)."""
    rows, capture = _read(path)
    try:
        status_path = Path(path).with_suffix(Path(path).suffix + ".health.json")
        counters = json.loads(status_path.read_text())
        required = ("attempted_samples", "written_samples", "dropped_samples", "incomplete_samples", "status_write_failures")
        if not isinstance(counters, dict) or any(not isinstance(counters.get(k), int) for k in required):
            raise ValueError("invalid health")
        capture.update(counters)
    except (OSError, ValueError):
        capture.update(attempted_samples=None, written_samples=None, dropped_samples=None,
                       incomplete_samples=None, status_write_failures=None, last_error=None)
    capture["status"] = ("partial" if capture["malformed_lines"] or capture["partial_tail"]
                         or capture.get("dropped_samples") or capture.get("incomplete_samples")
                         or capture.get("start_failures") or capture.get("status_write_failures")
                         else "unavailable" if capture["read_error"] else
                         "unknown" if capture["attempted_samples"] is None else "ok")
    return {"path": name or str(path), "samples": len(rows),
            "first_ts": rows[0]["ts"] if rows else None,
            "last_ts": rows[-1]["ts"] if rows else None,
            "peak_tree_rss_kb": _peak(rows, "tree_rss_kb"),
            "max_tree_procs": _peak(rows, "tree_procs"),
            "max_load1": _peak(rows, "load1"),
            "max_cpu_busy_pct": _peak(rows, "cpu_busy_pct"),
            "min_mem_available_kb": _peak(rows, "mem_available_kb", min),
            "min_disk_free_kb": _peak(rows, "disk_free_kb", min),
            "measurement": _measurement(len(rows)), "capture": capture}
