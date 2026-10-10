"""t-070 round 2, class C-A rule 4: no file effect of a Rune2 entry point follows a link below the
project's runtime area.

A file effect is a create, write, chmod or unlink. Check round 1 found the SQLite temp folder's
sweep following a `.alpaca/tmp` link: it set an outside folder to mode 0700 and deleted a file of
the user's there (A-4, .alpaca/proofs/op-008/t-070.logs/attacks/b04_sqlite_tmpdir_links.out). The
test is generated rather than a list of the folders someone thought of
(.alpaca/rune2-development/execution/t-070/fix-plan-r2.md section 2):

1. A normal run of each entry point lists every folder below `.alpaca/` that it creates, writes in,
   chmods or unlinks in (a folder whose direct entries change, or that appears; an aged file is put
   in every folder first, so a sweep shows), and every folder above one of those, since a path
   through a linked folder above it is redirected too.
2. Each such folder in turn is made a link to an outside bait folder that holds old files (a copy
   of what the folder held, aged two hours, plus stale temp-looking names).
3. After the same run the bait must be identical: names, bytes, modes and mtimes.

A hit in code outside Rune2 is filed as its own task and listed in FILED, where its own test is
`xfail(strict=True)` naming the task, so it starts failing the day the task lands.

Every entry point runs as an install runs it: `python -I <install root>/alpaca/boot.py -m`, which
loads the install root's package by its location (t-428), where the install root is the project root
(its `alpaca` is a link to this package), from the project's `src/`, with the disk sweep and
autoserve off.
"""
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SID = "s-confine"
OLD = 2 * 3600

#: (entry point, folder below .alpaca) -> the task that owns a hit in code outside Rune2 that is not
#: fixed yet; its test is xfail(strict=True). Empty since t-424 (the collector lock and the other
#: creates of alpaca/observability, alpaca/disk.py).
FILED = {}


def _argv(module, args, root):
    """`python -I -m <module>` as an install runs it (`bin/alpaca-python --isolated`): the boot of
    the install root, which is the project root (its `alpaca` is a link to this package)."""
    return [sys.executable, "-I", str(Path(root, "alpaca", "boot.py")), "-m", module] + list(args)


def _env(root, base):
    env = {k: v for k, v in os.environ.items()
           if k not in ("ALPACA_ROOT", "ALPACA_SESSION_ID", "PYTHONPATH", "SQLITE_TMPDIR",
                        "CLAUDE_CODE_SESSION_ID", "ALPACA_RESEARCH_AGENT")}
    env.update(CLAUDE_PROJECT_DIR=str(root), ALPACA_NO_AUTOSERVE="1", ALPACA_DISK_SWEEP="0",
               PYTHONDONTWRITEBYTECODE="1", CLAUDE_CONFIG_DIR=str(base / "cfg"),
               ALPACA_WORKSPACES=str(base / "alpaca-workspaces.json"),
               ALPACA_CLOUDFLARED_CONFIG=str(base / "cloudflared-config.yml"),
               ALPACA_RESERVED_PORTS="7350", ALPACA_RECORD_SKIP_ROOT=str(REPO))
    return env


def _hook(name, **extra):
    def payload(root, out):
        return json.dumps(dict({"session_id": SID, "cwd": str(root), "hook_event_name": name,
                                "transcript_path": str(out / "transcript.jsonl")}, **extra))
    return ("alpaca.hooks." + name, lambda root, out: [], payload)


def _verb(*words):
    return ("alpaca", lambda root, out: [w(root, out) if callable(w) else w for w in words], None)


def _prep_import(root, out, env):
    export = out / "for-import.jsonl"
    subprocess.run(_argv("alpaca", ["wiki", "export", str(export)], root), cwd=str(root / "src"), env=env,
                   capture_output=True, timeout=180, check=True)
    shutil.rmtree(root / ".alpaca" / "wiki")


def _prep_collect(root, out, env):
    subprocess.run(_argv("alpaca", ["collect", "enable"], root), cwd=str(root / "src"), env=env,
                   capture_output=True, timeout=120, check=True)


def _prep_disk(root, out, env):
    (root / "project.yaml").write_text("disk:\n  sweep_every_hours: 0\n")


#: name -> (module, args(root, out), stdin(root, out) or None[, prep(root, out, env)])
ENTRY_POINTS = {
    "import-alpaca": ("alpaca.paths", lambda root, out: [], None),
    "hook-session-start": _hook("session_start", source="resume"),
    "hook-stop": _hook("stop"),
    "hook-pre-compact": _hook("pre_compact", trigger="manual"),
    "hook-session-end": _hook("session_end", reason="other"),
    "capture-child": ("alpaca.capture_child", lambda root, out: [], lambda root, out: json.dumps(
        {"target": "alpaca.wiki.service:capture", "root": str(root), "kwargs": {"deadline_s": 120}, "fd": 1,
         "progress": False})),
    "wiki-recover": _verb("wiki", "recover"),
    "wiki-status": _verb("wiki", "status"),
    "wiki-query": _verb("wiki", "query", "which confinement note"),
    "wiki-export": _verb("wiki", "export", lambda root, out: str(out / "export.jsonl")),
    "wiki-import": _verb("wiki", "import", lambda root, out: str(out / "for-import.jsonl"))
                   + (_prep_import,),
    "wiki-extract": _verb("wiki", "extract", lambda root, out: str(root / ".alpaca" / "wiki"),
                          lambda root, out: str(out / "vault")),
    "collector-pass": _verb("collect", "once") + (_prep_collect,),
    # t-424: the other creates under .alpaca by alpaca/observability and the disk sweep
    "collect-enable": _verb("collect", "enable"),
    "collect-services": _verb("collect", "services"),
    "backup": ("alpaca", lambda root, out: ["collect", "backup"], None),
    "disk-sweep": ("alpaca.disk", lambda root, out: ["sweep", "--root", str(root), "--session", SID,
                                                     "--mode", "end"], None, _prep_disk),
}


def _run(entry, root, out, env):
    module, args, stdin = ENTRY_POINTS[entry][:3]
    p = subprocess.run(_argv(module, args(root, out), root), cwd=str(root / "src"), env=env,
                       input=stdin(root, out) if stdin else "", capture_output=True, text=True,
                       timeout=240)
    return p.returncode, (p.stdout[-400:], p.stderr[-800:])


@pytest.fixture(scope="module")
def base(tmp_path_factory):
    """One captured project (record, a transcript, a Rune2 store), copied for every run."""
    top = tmp_path_factory.mktemp("confine")
    (top / "cfg").mkdir()
    root = top / "base" / "proj"
    (root / "src").mkdir(parents=True)
    (root / "style").mkdir()
    (root / "style" / "banned.txt").write_text("")
    (root / ".alpaca" / "config").mkdir(parents=True)
    shutil.copy(REPO / "ALPACA-MANIFEST", root / "ALPACA-MANIFEST")
    (root / "alpaca").symlink_to(REPO / "alpaca", target_is_directory=True)
    out = top / "base" / "out"
    out.mkdir()
    rows = [{"type": "user", "message": {"content": "a confinement question about SQLite"}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "a confinement answer"}]}}]
    (out / "transcript.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    env = _env(root, top)
    from alpaca import db
    conn = db.connect(str(root))
    db.append_event(conn, session=SID, actor="alpaca", kind="session-start",
                    data={"transcript": str(out / "transcript.jsonl"), "operator": "claude"})
    for i in range(4):
        db.append_event(conn, session=SID, actor="agent", kind="note",
                        data={"text": "confinement note %d about the runtime area" % i})
    conn.close()
    for entry in ("hook-stop", "hook-session-end"):
        code, tail = _run(entry, root, out, env)
        assert code == 0, (entry, tail)
    assert (root / ".alpaca" / "wiki" / "rune.db").is_file()
    return top, root.parent


def _copy(base_dir, dest):
    shutil.copytree(base_dir, dest, symlinks=True)
    return dest / "proj", dest / "out"


def _listing(folder):
    """The direct entries of a folder with what a file effect changes, and the folder's own mode."""
    try:
        info = os.lstat(folder)
    except FileNotFoundError:
        return None
    entries = {}
    with os.scandir(folder) as it:
        for e in it:
            s = e.stat(follow_symlinks=False)
            entries[e.name] = (stat.S_IFMT(s.st_mode), stat.S_IMODE(s.st_mode), s.st_size, s.st_mtime_ns)
    return stat.S_IMODE(info.st_mode), entries


def _dirs_below(runtime):
    out = {}
    for dirpath, dirnames, _ in os.walk(runtime):
        dirnames[:] = [d for d in dirnames if not os.path.islink(os.path.join(dirpath, d))]
        rel = os.path.relpath(dirpath, runtime)
        if rel != ".":
            out[rel] = _listing(dirpath)
    return out


def touched_dirs(entry, base_dir, scratch):
    """The folders below .alpaca/ a normal run of the entry point creates, writes in, chmods or
    unlinks in."""
    top, base_proj = base_dir
    root, out = _copy(base_proj, scratch)
    env = _env(root, top)
    if len(ENTRY_POINTS[entry]) > 3:
        ENTRY_POINTS[entry][3](root, out, env)
    # an aged file in every folder shows a sweep that unlinks there
    old = time.time() - OLD
    for rel in _dirs_below(root / ".alpaca"):
        probe = root / ".alpaca" / rel / "stale-probe.tmp"
        probe.write_text("probe\n")
        os.utime(probe, (old, old))
    before = _dirs_below(root / ".alpaca")
    code, tail = _run(entry, root, out, env)
    after = _dirs_below(root / ".alpaca")
    changed = {d for d in set(before) | set(after) if before.get(d) != after.get(d)}
    # an effect in a folder resolves through each folder above it, so a link there redirects it
    for d in list(changed):
        while os.path.dirname(d):
            d = os.path.dirname(d)
            changed.add(d)
    return sorted(changed), code, tail


def _snapshot(tree):
    """Every path under the bait with its type, mode, mtime and bytes (or link target)."""
    out = {}
    for dirpath, dirnames, filenames in os.walk(tree):
        for name in [""] + dirnames + filenames:
            path = os.path.join(dirpath, name) if name else dirpath
            s = os.lstat(path)
            rec = (stat.S_IFMT(s.st_mode), stat.S_IMODE(s.st_mode), s.st_mtime_ns)
            if stat.S_ISREG(s.st_mode):
                rec += (hashlib.sha256(open(path, "rb").read()).hexdigest(),)
            elif stat.S_ISLNK(s.st_mode):
                rec += (os.readlink(path),)
            out[os.path.relpath(path, tree)] = rec
    return out


def _age(tree):
    old = time.time() - OLD
    for dirpath, dirnames, filenames in os.walk(tree, topdown=False):
        for name in filenames + dirnames:
            os.utime(os.path.join(dirpath, name), (old, old), follow_symlinks=False)
    os.utime(tree, (old, old))


def link_run(entry, rel, base_dir, scratch):
    """Run the entry point with `.alpaca/<rel>` a link to an outside bait folder; return the bait's
    changes (empty when nothing followed the link) and the run's exit and output tail."""
    top, base_proj = base_dir
    root, out = _copy(base_proj, scratch / "run")
    env = _env(root, top)
    if len(ENTRY_POINTS[entry]) > 3:
        ENTRY_POINTS[entry][3](root, out, env)
    target = root / ".alpaca" / rel
    bait = scratch / "bait"
    if target.is_dir() and not target.is_symlink():
        shutil.copytree(target, bait, symlinks=True)
        shutil.rmtree(target)
    else:
        bait.mkdir()
    for dirpath, _dirs, _files in os.walk(bait):       # every folder: old files, an open mode
        for name in ("users-notes.txt", "etilqs_stale", "stale.tmp"):
            Path(dirpath, name).write_text("the user's %s\n" % name)
        os.chmod(dirpath, 0o755)
    _age(bait)
    target.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(bait, target, target_is_directory=True)
    before = _snapshot(bait)
    code, tail = _run(entry, root, out, env)
    after = _snapshot(bait)
    changes = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
    return changes, code, tail


@pytest.mark.parametrize("entry", sorted(ENTRY_POINTS))
def test_no_file_effect_follows_a_link_below_the_runtime_area(entry, base, tmp_path):
    dirs, code, tail = touched_dirs(entry, base, tmp_path / "discover")
    assert code in (0, 1), (entry, code, tail)           # the entry point ran (1: a FAIL verdict)
    hits = {}
    for i, rel in enumerate(dirs):
        if (entry, rel) in FILED:
            continue
        changes, _code, _tail = link_run(entry, rel, base, tmp_path / ("link-%d" % i))
        if changes:
            hits[rel] = changes[:8]
    assert not hits, "%s followed a link below .alpaca/ (folders it touches: %s):\n%s" % (
        entry, dirs, json.dumps(hits, indent=1))


@pytest.mark.skipif(not FILED, reason="no filed link hit is open")
@pytest.mark.parametrize("entry, rel", [
    pytest.param(entry, rel, marks=pytest.mark.xfail(strict=True, reason=FILED[(entry, rel)]))
    for entry, rel in sorted(FILED)])
def test_a_filed_link_hit_outside_rune2(entry, rel, base, tmp_path):
    """Each FILED pair is xfail(strict=True) under its task: it fails as XPASS once the task lands."""
    changes, _code, _tail = link_run(entry, rel, base, tmp_path)
    assert not changes, changes


def test_the_created_files_are_refused_when_a_link_stands_in_their_place(tmp_path):
    """t-424: a link at the file itself, or at the folder, is refused and nothing outside is made."""
    from alpaca import _nofollow
    from alpaca.observability import collector_lock
    root = tmp_path / "proj"
    (root / ".alpaca" / "observability").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    os.symlink(outside / "lock", root / ".alpaca" / "observability" / "collector.lock")
    with pytest.raises(OSError):
        with collector_lock(str(root)):
            pass
    assert not (outside / "lock").exists()
    shutil.rmtree(root / ".alpaca" / "observability")
    os.symlink(outside, root / ".alpaca" / "observability", target_is_directory=True)
    with pytest.raises(OSError):
        with collector_lock(str(root)):
            pass
    with pytest.raises(OSError):
        _nofollow.write_below(str(root), ("observability",), "x", b"1")
    assert list(outside.iterdir()) == []


def test_a_backup_folder_swapped_for_a_link_after_the_check_is_not_followed(base, tmp_path, monkeypatch):
    """t-424: `.alpaca/backups` is checked, then used; a link put in its place between the two must
    not send the staging folder, the backup or the receipts outside the project."""
    from alpaca import artifacts, backup
    top, base_proj = base
    root, _out = _copy(base_proj, tmp_path / "run")
    outside = tmp_path / "outside"
    outside.mkdir()
    folder = root / ".alpaca" / "backups"
    real = artifacts.contained
    swapped = []

    def swap_after_check(*args, **kwargs):
        result = real(*args, **kwargs)
        if not swapped:
            swapped.append(1)
            if folder.is_dir() and not folder.is_symlink():
                folder.rename(root / ".alpaca" / "backups-moved")
            os.symlink(outside, folder, target_is_directory=True)
        return result

    monkeypatch.setattr(artifacts, "contained", swap_after_check)
    try:
        backup.create(str(root))
    except (OSError, ValueError):
        pass
    assert list(outside.iterdir()) == []


def test_the_no_follow_creates_make_a_fresh_runtime_area_as_before(tmp_path):
    """op008/followups (review M-4, full-suite `test_tunnel_service`): main made `.alpaca/<folder>`
    with its parents for the collector lock, `collect enable`, the disk sweep lock and the service
    files; the no-follow creates make them too, and a linked `.alpaca` is still refused."""
    from alpaca import _nofollow, disk
    from alpaca.observability import collector_lock
    fresh = tmp_path / "fresh" / "proj"
    with collector_lock(str(fresh)):
        pass
    assert (fresh / ".alpaca" / "observability" / "collector.lock").is_file()
    assert stat.S_IMODE((fresh / ".alpaca").stat().st_mode) == 0o700
    other = tmp_path / "other"
    _nofollow.write_below(str(other), ("services",), "x.service", b"1", 0o600)
    assert (other / ".alpaca" / "services" / "x.service").read_bytes() == b"1"
    third = tmp_path / "third"
    third.mkdir()
    lock = disk._lock(str(third))
    assert lock is not None
    lock.close()
    linked = tmp_path / "linked"
    linked.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    os.symlink(outside, linked / ".alpaca", target_is_directory=True)
    with pytest.raises(OSError):
        with collector_lock(str(linked)):
            pass
    with pytest.raises(OSError):
        _nofollow.write_below(str(linked), ("services",), "x.service", b"1")
    assert disk._lock(str(linked)) is None
    assert list(outside.iterdir()) == []
