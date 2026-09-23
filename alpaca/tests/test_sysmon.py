"""Host monitor: secret redaction, CPU delta math, snapshot shape, and the 2 s cache."""
import sys

import pytest

from alpaca import sysmon

linux = pytest.mark.skipif(not sysmon.supported(), reason="needs Linux /proc")


@pytest.mark.parametrize("argv, expected", [
    (["tool", "--token=abc123"], "tool --token=***"),
    (["tool", "--token", "abc123", "--verbose"], "tool --token *** --verbose"),
    (["tool", "--api-key", "k1"], "tool --api-key ***"),
    (["tool", "--password=hunter2"], "tool --password=***"),
    (["tool", "--pin", "476153"], "tool --pin ***"),
    (["env", "GITHUB_TOKEN=ghp_x", "run"], "env GITHUB_TOKEN=*** run"),
    (["env", "DB_PASSWD=p", "AWS_SECRET_ACCESS_KEY=s"], "env DB_PASSWD=*** AWS_SECRET_ACCESS_KEY=***"),
    (["git", "clone", "https://user:s3cret@example.com/repo.git"], "git clone https://user:***@example.com/repo.git"),
    (["curl", "-H", "Authorization: Bearer xyz"], "curl -H Authorization: ***"),
    (["curl", "--cookie", "sid=1"], "curl --cookie ***"),
    (["sh", "-c", "run --secret=zz && echo ok"], "sh -c run --secret=*** && echo ok"),
    (["sh", "-c", "./x AUTH_TOKEN=q --v"], "sh -c ./x AUTH_TOKEN=*** --v"),
    (["env", "PASSWORD=my pass phrase"], "env PASSWORD=***"),
])
def test_redact_masks_each_secret_form(argv, expected):
    assert sysmon.redact(argv) == expected


def test_redact_leaves_harmless_argv_unchanged():
    argv = ["python3", "-m", "pytest", "alpaca/tests", "-q", "--maxfail=1", "--spin", "PATH=/usr/bin",
            "https://example.com/a:b", "--keyboard", "us", "--author", "me"]
    assert sysmon.redact(argv) == " ".join(argv)


def test_redact_token_flag_followed_by_flag_keeps_next_flag():
    assert sysmon.redact(["x", "--token", "--other"]) == "x --token --other"


def test_display_cmd_shortens_root_home_and_truncates(tmp_path):
    root = str(tmp_path / "proj")
    out = sysmon.display_cmd([root + "/bin/tool", "/home/someone/data", "--key=v"], root)
    assert out == "./bin/tool ~someone/data --key=***"
    long = sysmon.display_cmd(["x" * 300], None)
    assert len(long) == sysmon.CMD_MAX and long.endswith("...")


def test_cpu_delta_math():
    assert sysmon.cpu_busy_pct((1000, 400), (2000, 900)) == 50.0
    assert sysmon.cpu_busy_pct((1000, 400), (1000, 400)) == 0.0
    assert sysmon.cpu_busy_pct((1000, 400), (1100, 400)) == 100.0
    assert sysmon.proc_cpu_pct(50, 0.5, hz=100) == 100.0
    assert sysmon.proc_cpu_pct(300, 1.0, hz=100) == 300.0
    assert sysmon.proc_cpu_pct(0, 1.0, hz=100) == 0.0
    assert sysmon.proc_cpu_pct(5, 0.0, hz=100) == 0.0


def test_parse_cpu_line_counts_iowait_as_idle():
    total, idle = sysmon.parse_cpu_line("cpu  10 0 10 70 10 0 0 0 5 0")
    assert (total, idle) == (100, 80)


def test_parse_pid_stat_handles_parentheses_in_comm():
    fields = ["S"] + ["0"] * 49
    fields[6] = str(sysmon.PF_KTHREAD)
    fields[11], fields[12], fields[19], fields[21] = "7", "3", "12345", "42"
    st = sysmon.parse_pid_stat("99 (we (ird) name) " + " ".join(fields))
    assert st == {"comm": "we (ird) name", "state": "S", "flags": sysmon.PF_KTHREAD, "ticks": 10,
                  "start": 12345, "rss_pages": 42}


@linux
def test_snapshot_shape_and_ranges(tmp_path):
    sysmon._reset()
    s = sysmon.snapshot(tmp_path)
    for key in ("ts", "host", "uptime_s", "sample_s", "load", "cpu", "mem", "disks", "users", "procs", "notes"):
        assert key in s
    assert len(s["load"]) == 3
    assert s["cpu"]["count"] >= 1 and len(s["cpu"]["per_core"]) == s["cpu"]["count"]
    assert 0 <= s["cpu"]["pct"] <= 100 and all(0 <= p <= 100 for p in s["cpu"]["per_core"])
    m = s["mem"]
    assert m["total_b"] > 0 and 0 <= m["used_b"] <= m["total_b"] and 0 <= m["pct"] <= 100
    assert s["sample_s"] > 0
    assert s["disks"] and all(0 <= d["pct"] <= 100 and d["total_b"] > 0 for d in s["disks"])
    assert len({d["mount"] + d["path"] for d in s["disks"]}) == len(s["disks"])
    assert 0 < len(s["procs"]) <= sysmon.TOP_N
    keys = {"pid", "user", "name", "cmd", "cpu_pct", "mem_pct", "rss_b", "elapsed_s", "state"}
    for p in s["procs"]:
        assert set(p) == keys and p["cpu_pct"] >= 0 and p["rss_b"] >= 0 and len(p["cmd"]) <= sysmon.CMD_MAX
    order = [(p["cpu_pct"], p["rss_b"]) for p in s["procs"]]
    assert order == sorted(order, reverse=True)
    assert s["users"] and all({"user", "cpu_pct", "mem_pct", "rss_b", "procs", "top"} <= set(u) for u in s["users"])
    assert [u["cpu_pct"] for u in s["users"]] == sorted((u["cpu_pct"] for u in s["users"]), reverse=True)
    assert sum(u["procs"] for u in s["users"]) >= len(s["procs"])


@linux
def test_snapshot_is_cached_for_two_seconds(tmp_path):
    sysmon._reset()
    a = sysmon.snapshot(tmp_path)
    b = sysmon.snapshot(tmp_path)
    assert a is b and a["ts"] == b["ts"]


def test_non_linux_host_reports_error(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    s = sysmon.snapshot(".")
    assert s["error"] == "host monitor needs Linux /proc" and s["ts"]
