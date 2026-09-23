"""Host load snapshot for the cockpit: CPU, memory, disks, users, and top processes.

Stdlib only; reads Linux /proc. ``snapshot(root)`` is cached for 2 s behind a lock so several
browsers polling at once cost one read. CPU percentages come from deltas between two raw samples:
the previous call's sample when it is at most 30 s old, else two samples 0.25 s apart.
"""
from __future__ import annotations

import datetime as _dt
import os
import pwd
import re
import socket
import sys
import threading
import time
from pathlib import Path

CACHE_S = 2.0
MAX_PREV_AGE_S = 30.0
COLD_GAP_S = 0.25
TOP_N = 15
CMD_MAX = 120
PF_KTHREAD = 0x00200000

_lock = threading.Lock()
_cache: dict = {"at": None, "key": None, "value": None}
_prev: dict | None = None
_uid_names: dict[int, str] = {}

try:
    _HZ = os.sysconf("SC_CLK_TCK")
    _PAGE = os.sysconf("SC_PAGE_SIZE")
except (AttributeError, ValueError, OSError):  # non-POSIX host
    _HZ, _PAGE = 100, 4096


# ---------------------------------------------------------------- redaction

_SECRET_WORD = re.compile(
    r"(?i)(?:[a-z0-9]*(?:pass(?:word)?|passwd|token|secret|key|cookie|credential)s?"
    r"|auth(?:orization|token)?|pin)")
_NAME_SPLIT = re.compile(r"[-_.]+")
_ENV_ASSIGN = re.compile(r"^([A-Za-z_][A-Za-z0-9_.-]*)=(.*)$", re.S)
_URL_USERINFO = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://[^/\s:@]*):[^@\s/]*@")
_HEADER = re.compile(r"(?i)^((?:proxy-)?authorization|cookie|set-cookie|x-api-key)(\s*:\s*)(.+)$", re.S)
_INLINE_FLAG = re.compile(r"(?<![\w-])(--?[A-Za-z][A-Za-z0-9_.-]*)(=|\s+)(?!-)(\S+)")
_INLINE_ENV = re.compile(r"(?<![\w-])([A-Za-z_][A-Za-z0-9_]*)=(\S+)")
MASK = "***"


def _secret_name(name: str) -> bool:
    """True when an option or variable name looks like it carries a secret."""
    parts = [p for p in _NAME_SPLIT.split(name.lstrip("-")) if p]
    return any(_SECRET_WORD.fullmatch(p) for p in parts)


def _redact_text(text: str) -> str:
    """Redact secrets embedded inside one argument (for example a ``sh -c`` script)."""
    text = _URL_USERINFO.sub(lambda m: m.group(1) + ":" + MASK + "@", text)
    if not any(c.isspace() for c in text):
        return text
    text = _INLINE_FLAG.sub(
        lambda m: m.group(1) + m.group(2) + MASK if _secret_name(m.group(1)) else m.group(0), text)
    return _INLINE_ENV.sub(
        lambda m: m.group(1) + "=" + MASK if _secret_name(m.group(1)) else m.group(0), text)


def redact_args(argv: list[str]) -> list[str]:
    """Return argv with secret values replaced by ``***``."""
    out: list[str] = []
    mask_next = False
    for arg in argv:
        if mask_next:
            mask_next = False
            if not arg.startswith("-"):
                out.append(MASK)
                continue
        header = _HEADER.match(arg)
        if header:
            out.append(header.group(1) + header.group(2) + MASK)
            continue
        if arg.startswith("-") and len(arg) > 1:
            name, eq, _value = arg.partition("=")
            if _secret_name(name):
                if eq:
                    out.append(name + "=" + MASK)
                else:
                    out.append(arg)
                    mask_next = True
                continue
        else:
            env = _ENV_ASSIGN.match(arg)
            if env and not any(c.isspace() for c in env.group(1)) and _secret_name(env.group(1)):
                out.append(env.group(1) + "=" + MASK)
                continue
        out.append(_redact_text(arg))
    return out


def redact(argv: list[str]) -> str:
    """Join argv into one display string with secret values replaced by ``***``."""
    return " ".join(redact_args(list(argv)))


def display_cmd(argv: list[str], root: str | os.PathLike | None = None, limit: int = CMD_MAX) -> str:
    """Redacted command line with the project root shown as ``.`` and home dirs as ``~``."""
    text = redact(argv)
    if root:
        r = str(root).rstrip("/")
        if r:
            text = re.sub(re.escape(r) + r"(?=/|\s|$)", ".", text)
    home = os.path.expanduser("~").rstrip("/")
    if home and home != "/":
        text = re.sub(re.escape(home) + r"(?=/|\s|$)", "~", text)
    text = re.sub(r"/home/([A-Za-z0-9_.-]+)(?=/|\s|$)", r"~\1", text)
    if len(text) > limit:
        text = text[: limit - 3] + "..."
    return text


# ---------------------------------------------------------------- pure math

def cpu_busy_pct(prev: tuple[int, int], cur: tuple[int, int]) -> float:
    """Busy percent between two (total_ticks, idle_ticks) readings of a /proc/stat cpu line."""
    dt = cur[0] - prev[0]
    if dt <= 0:
        return 0.0
    busy = dt - (cur[1] - prev[1])
    return round(max(0.0, min(100.0, busy * 100.0 / dt)), 1)


def proc_cpu_pct(dticks: float, dt_s: float, hz: int = _HZ) -> float:
    """Top-style percent of one core for a process that used ``dticks`` clock ticks in ``dt_s``."""
    if dt_s <= 0 or dticks <= 0:
        return 0.0
    return round(dticks / hz / dt_s * 100.0, 1)


def parse_cpu_line(line: str) -> tuple[int, int]:
    """(total, idle) ticks from one /proc/stat cpu line; idle counts idle + iowait."""
    vals = [int(v) for v in line.split()[1:]]
    # guest and guest_nice are already included in user and nice.
    total = sum(vals[:8])
    idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
    return total, idle


def parse_pid_stat(text: str) -> dict:
    """Fields we need from /proc/<pid>/stat; comm may itself contain spaces and parentheses."""
    lpar, rpar = text.index("("), text.rindex(")")
    comm = text[lpar + 1:rpar]
    f = text[rpar + 2:].split()
    # f[0] is field 3 (state); field n lives at f[n - 3].
    return {"comm": comm, "state": f[0], "flags": int(f[6]), "ticks": int(f[11]) + int(f[12]),
            "start": int(f[19]), "rss_pages": int(f[21])}


# ---------------------------------------------------------------- /proc readers

def _read(path: str) -> str:
    with open(path, "rb") as fh:
        return fh.read().decode("utf-8", "replace")


def _cpu_sample() -> list[tuple[int, int]]:
    rows = []
    for line in _read("/proc/stat").splitlines():
        if not line.startswith("cpu"):
            break
        rows.append(parse_cpu_line(line))
    return rows


def _uid_of(pid: str) -> int:
    for line in _read("/proc/%s/status" % pid).splitlines():
        if line.startswith("Uid:"):
            return int(line.split()[1])
    raise ProcessLookupError(pid)


def _proc_sample() -> tuple[dict, int]:
    """pid -> parsed stat plus uid; kernel threads are skipped. Returns (procs, unreadable)."""
    procs: dict[int, dict] = {}
    unreadable = 0
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            st = parse_pid_stat(_read("/proc/%s/stat" % name))
            if st["flags"] & PF_KTHREAD:
                continue
            st["uid"] = _uid_of(name)
        except (FileNotFoundError, ProcessLookupError):
            continue
        except (PermissionError, ValueError, IndexError, OSError):
            unreadable += 1
            continue
        procs[int(name)] = st
    return procs, unreadable


def _sample() -> dict:
    procs, unreadable = _proc_sample()
    return {"mono": time.monotonic(), "cpu": _cpu_sample(), "procs": procs, "unreadable": unreadable}


def _user_name(uid: int) -> str:
    if uid not in _uid_names:
        try:
            _uid_names[uid] = pwd.getpwuid(uid).pw_name
        except KeyError:
            _uid_names[uid] = str(uid)
    return _uid_names[uid]


def _meminfo() -> dict:
    out = {}
    for line in _read("/proc/meminfo").splitlines():
        key, _, rest = line.partition(":")
        parts = rest.split()
        if parts:
            out[key] = int(parts[0]) * (1024 if len(parts) > 1 and parts[1] == "kB" else 1)
    return out


def _mounts() -> list[tuple[str, str]]:
    rows = []
    try:
        for line in _read("/proc/self/mounts").splitlines():
            parts = line.split()
            if len(parts) >= 3:
                mount = parts[1].replace("\\040", " ")
                rows.append((mount, parts[2]))
    except OSError:
        pass
    return rows


def _mount_of(path: str, mounts: list[tuple[str, str]]) -> tuple[str, str]:
    best = ("/", "")
    for mount, fs in mounts:
        if path == mount or path.startswith(mount.rstrip("/") + "/"):
            if len(mount) >= len(best[0]):
                best = (mount, fs)
    return best


def _disks(root: str) -> list[dict]:
    mounts = _mounts()
    seen: dict[int, dict] = {}
    out: list[dict] = []
    for label, path in (("/tmp", "/tmp"), ("/var/tmp", "/var/tmp"), ("project", root)):
        try:
            real = os.path.realpath(path)
            dev = os.stat(real).st_dev
            vfs = os.statvfs(real)
        except OSError:
            continue
        if dev in seen:
            seen[dev]["paths"].append(label)
            continue
        total = vfs.f_blocks * vfs.f_frsize
        free = vfs.f_bfree * vfs.f_frsize
        avail = vfs.f_bavail * vfs.f_frsize
        used = total - free
        denom = used + avail
        mount, fs = _mount_of(real, mounts)
        row = {"mount": mount, "path": real, "paths": [label], "fs": fs, "total_b": total,
               "used_b": used, "avail_b": avail,
               "pct": round(used * 100.0 / denom, 1) if denom else 0.0}
        seen[dev] = row
        out.append(row)
    return out


def _now_iso() -> str:
    return _dt.datetime.now().astimezone().isoformat(timespec="seconds")


# ---------------------------------------------------------------- snapshot

def _build(root: str) -> dict:
    global _prev
    notes: list[str] = []
    prev = _prev
    if prev is None or time.monotonic() - prev["mono"] > MAX_PREV_AGE_S:
        prev = _sample()
        time.sleep(COLD_GAP_S)
        notes.append("CPU measured over a short %.2f s window (first sample)." % COLD_GAP_S)
    else:
        gap = COLD_GAP_S - (time.monotonic() - prev["mono"])
        if gap > 0:  # a very recent sample gives a noisy delta
            time.sleep(gap)
    cur = _sample()
    _prev = cur
    dt = max(cur["mono"] - prev["mono"], 1e-6)

    cpu_rows = [cpu_busy_pct(a, b) for a, b in zip(prev["cpu"], cur["cpu"])]
    total_pct = cpu_rows[0] if cpu_rows else 0.0
    per_core = cpu_rows[1:]

    mem = _meminfo()
    mem_total = mem.get("MemTotal", 0)
    mem_avail = mem.get("MemAvailable", mem.get("MemFree", 0))
    uptime = float(_read("/proc/uptime").split()[0])

    rows = []
    users: dict[int, dict] = {}
    prev_procs = prev["procs"]
    for pid, st in cur["procs"].items():
        old = prev_procs.get(pid)
        base = old["ticks"] if old and old["start"] == st["start"] else 0
        if not old and st["start"] / _HZ < uptime - dt - 1.0:
            base = st["ticks"]  # existed but was unreadable in the earlier sample: no delta known
        pct = proc_cpu_pct(st["ticks"] - base, dt)
        rss = st["rss_pages"] * _PAGE
        mem_pct = round(rss * 100.0 / mem_total, 2) if mem_total else 0.0
        row = {"pid": pid, "uid": st["uid"], "name": st["comm"], "cpu_pct": pct, "mem_pct": mem_pct,
               "rss_b": rss, "elapsed_s": round(max(0.0, uptime - st["start"] / _HZ), 1),
               "state": st["state"]}
        rows.append(row)
        u = users.get(st["uid"])
        if u is None:
            u = users[st["uid"]] = {"cpu": 0.0, "rss": 0, "procs": 0, "best": None}
        u["cpu"] += pct
        u["rss"] += rss
        u["procs"] += 1
        if u["best"] is None or (pct, rss) > (u["best"]["cpu_pct"], u["best"]["rss_b"]):
            u["best"] = row

    rows.sort(key=lambda r: (r["cpu_pct"], r["rss_b"]), reverse=True)
    procs = []
    for row in rows:
        if len(procs) >= TOP_N:
            break
        try:
            raw = _read("/proc/%d/cmdline" % row["pid"])
        except (FileNotFoundError, ProcessLookupError):
            continue
        except OSError:
            raw = ""
        argv = [a for a in raw.split("\0") if a] or ["[" + row["name"] + "]"]
        uid = row.pop("uid")
        procs.append({"pid": row["pid"], "user": _user_name(uid), "name": row["name"],
                      "cmd": display_cmd(argv, root), "cpu_pct": row["cpu_pct"],
                      "mem_pct": row["mem_pct"], "rss_b": row["rss_b"],
                      "elapsed_s": row["elapsed_s"], "state": row["state"]})

    user_rows = [{"user": _user_name(uid), "cpu_pct": round(u["cpu"], 1),
                  "mem_pct": round(u["rss"] * 100.0 / mem_total, 1) if mem_total else 0.0,
                  "rss_b": u["rss"], "procs": u["procs"], "top": u["best"]["name"] if u["best"] else ""}
                 for uid, u in users.items()]
    user_rows.sort(key=lambda r: (r["cpu_pct"], r["rss_b"]), reverse=True)

    if cur["unreadable"]:
        notes.append("%d processes could not be read." % cur["unreadable"])
    disks = _disks(root)
    if any(d["mount"] == "/tmp" and d["fs"] == "tmpfs" for d in disks):
        notes.append("/tmp is RAM-backed tmpfs; its per-user quota is not shown by the size.")

    swap_total = mem.get("SwapTotal", 0)
    return {
        "ts": _now_iso(),
        "host": socket.gethostname(),
        "uptime_s": uptime,
        "sample_s": round(dt, 3),
        "load": [round(x, 2) for x in os.getloadavg()],
        "cpu": {"count": len(per_core) or (os.cpu_count() or 1), "pct": total_pct, "per_core": per_core},
        "mem": {"total_b": mem_total, "used_b": max(0, mem_total - mem_avail), "available_b": mem_avail,
                "pct": round((mem_total - mem_avail) * 100.0 / mem_total, 1) if mem_total else 0.0,
                "swap_total_b": swap_total, "swap_used_b": max(0, swap_total - mem.get("SwapFree", 0))},
        "disks": disks,
        "users": user_rows,
        "procs": procs,
        "notes": notes,
    }


def supported() -> bool:
    return sys.platform.startswith("linux") and os.path.exists("/proc/stat")


def snapshot(root) -> dict:
    """Current host load; cached for 2 s and shared by every caller."""
    if not supported():
        return {"error": "host monitor needs Linux /proc", "ts": _now_iso()}
    key = str(Path(root).resolve()) if root else ""
    with _lock:
        now = time.monotonic()
        if _cache["value"] is not None and _cache["key"] == key and now - _cache["at"] < CACHE_S:
            return _cache["value"]
        value = _build(key)
        _cache.update(at=time.monotonic(), key=key, value=value)
        return value


def _reset() -> None:
    """Drop the cache and the previous sample (tests)."""
    global _prev
    with _lock:
        _prev = None
        _cache.update(at=None, key=None, value=None)
