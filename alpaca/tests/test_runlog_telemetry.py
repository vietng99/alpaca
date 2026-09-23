"""Job telemetry: what the machine was doing while a long job ran.

Every control asserts a positive and a negative path. The sampler reads `/proc`, so the unit
tests run against the real one and the failure tests break the reader deliberately.
"""
import json
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest

from alpaca.runlog import telemetry


# --------------------------------------------------------------------------- one sample


def test_a_sample_carries_exactly_the_declared_keys(tmp_path):
    sampler = telemetry.Sampler(tmp_path / "j.telemetry.jsonl", interval=0.01)
    sampler.stage = "build"
    row = sampler.sample()
    assert tuple(row) == telemetry.KEYS
    # positive: the host readings are real numbers on this Linux host.
    assert row["mem_total_kb"] > 0 and row["load1"] is not None
    assert row["tree_rss_kb"] > 0 and row["tree_procs"] >= 1
    assert row["disk_free_kb"] > 0 and row["stage"] == "build"
    # negative: the first sample cannot know a CPU delta, so it says nothing.
    assert row["cpu_busy_pct"] is None
    time.sleep(0.05)
    assert 0.0 <= sampler.sample()["cpu_busy_pct"] <= 100.0


def test_an_unreadable_proc_writes_nulls_instead_of_raising(tmp_path, monkeypatch):
    sampler = telemetry.Sampler(tmp_path / "j.telemetry.jsonl", interval=0.01)

    def refuse(*_args, **_kwargs):
        raise OSError("procfs is not mounted")

    class Refused:
        def __init__(self, *_args):
            pass

        read_text = refuse

    monkeypatch.setattr(telemetry, "Path", Refused)
    monkeypatch.setattr(telemetry.os, "listdir", refuse)
    monkeypatch.setattr(telemetry.os, "statvfs", refuse)
    # positive: the sample still has every key and the line still lands on disk.
    row = sampler.sample()
    assert tuple(row) == telemetry.KEYS
    sampler.emit()
    assert len(sampler.path.read_text().splitlines()) == 1
    # negative: nothing was invented for a field that could not be read.
    assert all(row[key] is None for key in telemetry.KEYS if key not in ("ts", "stage"))


def test_the_worker_tree_is_the_pid_and_its_descendants():
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"])
    try:
        pids = telemetry.descendants(__import__("os").getpid())
        # positive: a live child of this process is counted.
        assert child.pid in pids
        # negative: pid 1 is not a descendant of a test process.
        assert 1 not in pids
    finally:
        child.kill()
        child.wait()


def test_the_interval_comes_from_the_environment(monkeypatch):
    monkeypatch.delenv("ALPACA_TELEMETRY_S", raising=False)
    assert telemetry.interval_s() == telemetry.DEFAULT_INTERVAL_S
    monkeypatch.setenv("ALPACA_TELEMETRY_S", "0.25")
    assert telemetry.interval_s() == 0.25
    # negative: a nonsense or non-positive override falls back rather than spinning.
    monkeypatch.setenv("ALPACA_TELEMETRY_S", "0")
    assert telemetry.interval_s() == telemetry.DEFAULT_INTERVAL_S
    monkeypatch.setenv("ALPACA_TELEMETRY_S", "soon")
    assert telemetry.interval_s() == telemetry.DEFAULT_INTERVAL_S


def test_the_sampler_thread_appends_one_json_line_per_interval(tmp_path):
    path = tmp_path / "jobs" / "abc.telemetry.jsonl"
    sampler = telemetry.start(path, interval=0.02)
    sampler.stage = "test"
    time.sleep(0.25)
    sampler.stop()
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    # positive: several samples landed, each with the full key set and the current stage.
    assert len(rows) >= 3
    assert all(set(row) == set(telemetry.KEYS) for row in rows)
    assert {row["stage"] for row in rows} == {"test"}
    assert rows[0]["ts"] < rows[-1]["ts"]
    # negative: a stopped sampler writes nothing more.
    settled = path.read_text()
    time.sleep(0.1)
    assert path.read_text() == settled


# --------------------------------------------------------------------------- readers


def _write(path, rows):
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))


def test_window_reads_back_only_the_stage_it_was_asked_for(tmp_path):
    path = tmp_path / "j.telemetry.jsonl"
    _write(path, [{"ts": 100.0, "tree_rss_kb": 9000, "load1": 9.0},
                  {"ts": 150.0, "tree_rss_kb": 2000, "load1": 1.5},
                  {"ts": 160.0, "tree_rss_kb": 4000, "load1": 2.5},
                  {"ts": 900.0, "tree_rss_kb": 8000, "load1": 8.0}])
    inside = telemetry.window(path, 150.0, 200.0)
    # positive: the two samples inside the window set the peaks.
    assert {key: inside[key] for key in ("wall_s", "peak_tree_rss_kb", "max_load1", "samples")} == {"wall_s": 50.0, "peak_tree_rss_kb": 4000, "max_load1": 2.5, "samples": 2}
    # negative: samples from another stage's window are not borrowed.
    outside = telemetry.window(path, 200.0, 800.0)
    assert {key: outside[key] for key in ("wall_s", "peak_tree_rss_kb", "max_load1", "samples")} == {
        "wall_s": 600.0, "peak_tree_rss_kb": None, "max_load1": None, "samples": 0}
    assert telemetry.window(tmp_path / "absent.jsonl", 1.0, 3.5)["samples"] == 0


def test_a_truncated_last_line_is_skipped_not_fatal(tmp_path):
    path = tmp_path / "j.telemetry.jsonl"
    path.write_text(json.dumps({"ts": 1.0, "tree_rss_kb": 5}) + "\n" + '{"ts": 2.0, "tree_')
    rows = telemetry.read(path)
    assert [row["ts"] for row in rows] == [1.0]


def test_summary_describes_the_whole_job(tmp_path):
    path = tmp_path / "j.telemetry.jsonl"
    _write(path, [{"ts": 10.0, "tree_rss_kb": 100, "tree_procs": 2, "load1": 1.0,
                   "cpu_busy_pct": None, "mem_available_kb": 900, "disk_free_kb": 50},
                  {"ts": 20.0, "tree_rss_kb": 700, "tree_procs": 9, "load1": 4.0,
                   "cpu_busy_pct": 88.5, "mem_available_kb": 300, "disk_free_kb": 20}])
    summary = telemetry.summary(path, name="jobs/j.telemetry.jsonl")
    assert {key: value for key, value in summary.items() if key not in ("measurement", "capture")} == {"path": "jobs/j.telemetry.jsonl", "samples": 2,
                       "first_ts": 10.0, "last_ts": 20.0, "peak_tree_rss_kb": 700,
                       "max_tree_procs": 9, "max_load1": 4.0, "max_cpu_busy_pct": 88.5,
                       "min_mem_available_kb": 300, "min_disk_free_kb": 20}
    # negative: an empty file summarizes to nulls, never to zero-as-a-measurement.
    empty = telemetry.summary(tmp_path / "none.jsonl")
    assert empty["samples"] == 0 and empty["peak_tree_rss_kb"] is None


# ------------------------------------------------------------- sampler health and capture status


def test_sampler_failed_append_is_counted_and_survives_recovery(tmp_path, capsys):
    path = tmp_path / 'broken' / 'job.telemetry.jsonl'
    path.parent.write_text('not a directory')
    sampler = telemetry.Sampler(path)
    assert sampler.emit() is False
    assert sampler.health()['dropped_samples'] == 1
    assert sampler.health()['attempted_samples'] == 1
    assert 'FileExistsError' in capsys.readouterr().err
    path.parent.unlink()
    assert sampler.emit() is True
    status = telemetry.summary(path)['capture']
    assert status['dropped_samples'] == 1 and status['written_samples'] == 1
    assert status['status'] == 'partial'


def test_terminal_stage_without_samples_is_explicitly_unmeasured(tmp_path):
    result = telemetry.window(tmp_path / 'absent.jsonl', 100, 100.005)
    assert result['samples'] == 0 and result['peak_tree_rss_kb'] is None
    assert result['measurement']['status'] == 'unavailable'
    assert result['measurement']['rss'] == 'sampled_process_tree_sum_may_double_count_shared_pages'
    assert result['measurement']['cpu'] == 'host_busy_percent_not_job_cpu'
    assert result['terminal']['wall_s'] == 0.005
    assert result['terminal']['cpu_s'] is None


def test_diagnostics_report_bad_lines_and_partial_tail(tmp_path):
    path = tmp_path / 'job.telemetry.jsonl'
    path.write_text('{"ts":1,"tree_rss_kb":20}\nnot-json\n{"ts":')
    data = telemetry.summary(path)
    assert data['samples'] == 1
    assert data['capture']['malformed_lines'] == 1
    assert data['capture']['partial_tail'] is True
    assert data['capture']['status'] == 'partial'


def test_corrupt_sampler_health_is_unavailable_not_a_failed_summary(tmp_path):
    path = tmp_path / 'job.telemetry.jsonl'
    path.write_text('{"ts":1}\n')
    path.with_suffix(path.suffix + '.health.json').write_text('{}')
    result = telemetry.summary(path)
    assert result['capture']['status'] == 'unknown'
    assert result['capture']['attempted_samples'] is None


def test_sampler_short_append_does_not_hide_next_sample(tmp_path, monkeypatch):
    path = tmp_path / 'job.telemetry.jsonl'
    sampler = telemetry.Sampler(path)
    original = Path.open
    class ShortAppend:
        def __init__(self, stream):
            self.stream = stream
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.stream.close()
        def __getattr__(self, key):
            return getattr(self.stream, key)
        def write(self, value):
            return self.stream.write(value[:len(value)//2])
    def short_open(self, mode='r', *args, **kwargs):
        stream = original(self, mode, *args, **kwargs)
        return ShortAppend(stream) if self == path and 'a' in mode else stream
    with monkeypatch.context() as context:
        context.setattr(Path, 'open', short_open)
        assert sampler.emit() is False
    assert sampler.emit() is True
    result = telemetry.summary(path)
    assert result['samples'] == 1
    assert result['capture']['dropped_samples'] == 1
    assert result['capture']['malformed_lines'] == 1


def test_sampler_thread_start_failure_is_reported(tmp_path, monkeypatch, capsys):
    sampler = telemetry.Sampler(tmp_path / 'job.telemetry.jsonl')
    def refuse(thread):
        raise RuntimeError('cannot start thread')
    monkeypatch.setattr(threading.Thread, 'start', refuse)
    with pytest.raises(RuntimeError):
        sampler.start()
    assert sampler.health()['start_failures'] == 1
    assert 'sampler_start' in capsys.readouterr().err


def test_a_missing_job_file_reports_missing_measurement(tmp_path):
    # carried from the HTTP telemetry test: the reader under that route, with no sample file.
    path = tmp_path / 'jobs' / ('a' * 32 + '.telemetry.jsonl')
    assert telemetry.read(path) == []
    assert telemetry.summary(path)['capture']['status'] == 'unavailable'
