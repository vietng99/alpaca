"""t-070 RC5 part B: Alpaca's processes put SQLite temp files on the project's own filesystem.

A SQLite sort or temp B-tree that outgrows the page cache writes temp files to the first of
SQLITE_TMPDIR, TMPDIR, /var/tmp. The hooks run with neither variable set, so they wrote to /var/tmp,
which on the owner's host is a busy spinning disk while the project is on NVMe: one `pwrite64` there
slept 56 s (A-4, `.alpaca/proofs/op-008/t-070.logs/rc4/a4-strace-scan.log`). `import alpaca` now sets
SQLITE_TMPDIR to `<root>/.alpaca/tmp/sqlite` (mode 0700) before any Alpaca module imports `sqlite3`
(`alpaca.sqlite_tmpdir`), and leaves `temp_store` alone.

The probe: a child interpreter that imports the package from a project root, then runs a sort larger
than its page cache and, while the sort's output is read, lists the files it holds open
(`/proc/self/fd`): SQLite unlinks a temp file right after it opens it, so the open descriptor is the
only trace of where it went.
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from alpaca import db

PACKAGE = Path(db.__file__).resolve().parent

PROBE = r'''
import json, os, sys
import alpaca, sqlite3
path = sys.argv[1]
conn = sqlite3.connect(path)
conn.execute("PRAGMA cache_size=-256")              # a 256 KiB page cache: the sort spills
conn.execute("CREATE TABLE t(a)")
conn.executemany("INSERT INTO t VALUES (?)", ((os.urandom(150).hex(),) for _ in range(30000)))
conn.commit()
temp_store = conn.execute("PRAGMA temp_store").fetchone()[0]
held = set()
for i, _ in enumerate(conn.execute("SELECT a FROM t ORDER BY a")):
    if i % 5000 == 0:
        for fd in os.listdir("/proc/self/fd"):
            try:
                target = os.readlink("/proc/self/fd/" + fd)
            except OSError:
                continue
            if "etilqs_" in target:
                held.add(os.path.dirname(target))
print(json.dumps({"env": os.environ.get("SQLITE_TMPDIR"), "dirs": sorted(held), "temp_store": temp_store}))
'''


def _root_with_package(tmp_path):
    """A project root whose `alpaca` is this package (a link, so `import alpaca` reads its root as
    this folder), with the runtime area a project has."""
    root = tmp_path / "proj"
    (root / ".alpaca").mkdir(parents=True)
    (root / "alpaca").symlink_to(PACKAGE, target_is_directory=True)
    # the folder belongs to the project found from the working folder (I-2), so the root is one
    (root / "ALPACA-MANIFEST").write_bytes((PACKAGE.parent / "ALPACA-MANIFEST").read_bytes())
    return root


def _probe(root, tmp_path, **env_over):
    env = {k: v for k, v in os.environ.items() if k not in ("SQLITE_TMPDIR", "TMPDIR", "PYTHONPATH")}
    env.update(PYTHONPATH=str(root), PYTHONDONTWRITEBYTECODE="1", **env_over)
    proc = subprocess.run([sys.executable, "-c", PROBE, str(tmp_path / "probe.db")], cwd=str(root),
                          env=env, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr[-2000:]
    return json.loads(proc.stdout.strip().splitlines()[-1])


@pytest.mark.skipif(not os.path.isdir("/proc/self/fd"), reason="needs /proc")
def test_a_large_sort_writes_its_temp_files_under_the_project_runtime_area(tmp_path):
    root = _root_with_package(tmp_path)
    got = _probe(root, tmp_path)
    folder = str(root / ".alpaca" / "tmp" / "sqlite")
    assert got["env"] == folder
    assert got["dirs"] == [folder], got                     # never /var/tmp
    assert got["temp_store"] == 0                           # temp_store left at its default
    assert (os.stat(folder).st_mode & 0o777) == 0o700


@pytest.mark.skipif(not os.path.isdir("/proc/self/fd"), reason="needs /proc")
def test_an_operator_choice_of_sqlite_tmpdir_wins(tmp_path):
    root = _root_with_package(tmp_path)
    mine = tmp_path / "mine"
    mine.mkdir()
    got = _probe(root, tmp_path, SQLITE_TMPDIR=str(mine))
    assert got["env"] == str(mine) and got["dirs"] == [str(mine)]
    assert not (root / ".alpaca" / "tmp").exists()


def test_the_folder_is_private_and_cleaned_of_stale_files(tmp_path):
    import alpaca
    root = tmp_path / "proj"
    folder = root / ".alpaca" / "tmp" / "sqlite"
    folder.mkdir(parents=True, mode=0o755)
    os.chmod(folder, 0o755)
    stale, fresh = folder / "etilqs_stale", folder / "etilqs_fresh"
    stale.write_bytes(b"x")
    fresh.write_bytes(b"y")
    old = time.time() - alpaca.SQLITE_TMP_STALE_S - 60
    os.utime(stale, (old, old))
    env = {}
    assert alpaca.sqlite_tmpdir(str(root), environ=env) == str(folder)
    assert env == {"SQLITE_TMPDIR": str(folder)}
    assert (os.stat(folder).st_mode & 0o777) == 0o700
    assert not stale.exists() and fresh.exists()


def test_no_runtime_area_no_folder_and_a_link_is_refused(tmp_path):
    import alpaca
    bare = tmp_path / "bare"
    bare.mkdir()
    env = {}
    assert alpaca.sqlite_tmpdir(str(bare), environ=env) is None and env == {}
    assert not (bare / ".alpaca").exists()
    linked = tmp_path / "linked"
    (linked / ".alpaca" / "tmp").mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (linked / ".alpaca" / "tmp" / "sqlite").symlink_to(elsewhere, target_is_directory=True)
    assert alpaca.sqlite_tmpdir(str(linked), environ=env) is None and env == {}


# ---------------------------------------------------------------- I-2: the served project's folder
def _child_report(root, **_):
    """Run in a capture child (capture_runner.run_process): the folder it was given."""
    return {"sqlite_tmpdir": os.environ.get("SQLITE_TMPDIR")}


def _isolated_import_argv(code, where):
    """`python -I alpaca/boot.py <script>`, as the hooks run (t-428): this tree's package loaded by its
    location, the code in a script file under `where`."""
    script = os.path.join(str(where), "probe_i2.py")
    with open(script, "w") as fh:
        fh.write(code + "\n")
    return [sys.executable, "-I", str(PACKAGE / "boot.py"), script]


def test_the_folder_is_the_served_projects(tmp_path):
    """I-2: `import alpaca` roots the folder at the project found from the working folder, not at
    the tree that holds the package, and a child started for a served root gets that root's."""
    other = tmp_path / "other"
    (other / ".alpaca").mkdir(parents=True)
    (other / "src").mkdir()
    (other / "ALPACA-MANIFEST").write_bytes((PACKAGE.parent / "ALPACA-MANIFEST").read_bytes())
    env = {k: v for k, v in os.environ.items() if k not in ("SQLITE_TMPDIR", "PYTHONPATH")}
    got = subprocess.run(_isolated_import_argv(
        "import os, alpaca; print(alpaca.__file__); print(os.environ.get('SQLITE_TMPDIR'))", tmp_path),
        cwd=str(other / "src"), env=env, capture_output=True, text=True, timeout=60)
    assert got.returncode == 0, got.stderr
    package_file, folder = got.stdout.split()
    assert package_file == str(PACKAGE / "__init__.py")
    assert folder == os.path.join(os.path.realpath(str(other)), ".alpaca", "tmp", "sqlite")
    # outside any project: nothing is set and nothing is created at the package tree
    bare = tmp_path / "bare"
    bare.mkdir()
    got = subprocess.run(_isolated_import_argv(
        "import os, alpaca; print(os.environ.get('SQLITE_TMPDIR'))", tmp_path),
        cwd=str(bare), env=env, capture_output=True, text=True, timeout=60)
    assert got.returncode == 0 and got.stdout.strip() == "None", (got.stdout, got.stderr)

    from alpaca import capture_runner
    served = tmp_path / "served"
    (served / ".alpaca").mkdir(parents=True)
    (served / "ALPACA-MANIFEST").write_bytes((PACKAGE.parent / "ALPACA-MANIFEST").read_bytes())
    result = capture_runner.run_process("alpaca.tests.test_sqlite_tmpdir:_child_report", {},
                                        root=str(served), kill_s=60)
    assert result.status == "ok", result
    assert result.value["sqlite_tmpdir"] == os.path.join(str(served), ".alpaca", "tmp", "sqlite"), result
