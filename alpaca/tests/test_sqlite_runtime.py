"""The optional project SQLite runtime must reach Python and its child processes."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

REPO = Path(__file__).resolve().parents[2]


def installation(tmp_path):
    root = tmp_path / 'installation'
    (root / 'bin').mkdir(parents=True)
    (root / '.venv/bin').mkdir(parents=True)
    shutil.copy2(REPO / 'bin/alpaca-python', root / 'bin/alpaca-python')
    (root / '.venv/bin/python').symlink_to(sys.executable)
    return root


def run(root, program):
    env = dict(os.environ)
    env.pop('LD_LIBRARY_PATH', None)
    return subprocess.run([str(root / 'bin/alpaca-python'), '-c', program], env=env,
                          text=True, capture_output=True, timeout=20)


def test_invalid_runtime_selector_fails_closed(tmp_path):
    root = installation(tmp_path)
    runtime = root / '.alpaca/toolchain/sqlite'
    runtime.mkdir(parents=True)
    (runtime / 'enabled').write_text('../../outside\n')
    result = run(root, 'print("launched")')
    assert result.returncode != 0
    assert 'launched' not in result.stdout
    assert 'SQLite' in result.stderr


def test_missing_selected_runtime_fails_closed(tmp_path):
    root = installation(tmp_path)
    runtime = root / '.alpaca/toolchain/sqlite'
    runtime.mkdir(parents=True)
    (runtime / 'enabled').write_text('3.53.4\n')
    result = run(root, 'print("launched")')
    assert result.returncode != 0 and 'launched' not in result.stdout


def test_unselected_runtime_preserves_python(tmp_path):
    root = installation(tmp_path)
    result = run(root, 'import sqlite3;print(sqlite3.sqlite_version)')
    baseline = subprocess.check_output([sys.executable, '-c', 'import sqlite3;print(sqlite3.sqlite_version)'],
                                      text=True, env={k:v for k,v in os.environ.items() if k!='LD_LIBRARY_PATH'})
    assert result.returncode == 0 and result.stdout == baseline


def test_selected_runtime_reaches_python_descendants(tmp_path):
    candidate = os.environ.get('ALPACA_TEST_SQLITE_LIBRARY')
    if not candidate:
        pytest.skip('set ALPACA_TEST_SQLITE_LIBRARY to the verified project-local build')
    root = installation(tmp_path)
    runtime = root / '.alpaca/toolchain/sqlite'
    library = runtime / '3.53.4/lib/libsqlite3.so.0'
    library.parent.mkdir(parents=True)
    shutil.copy2(candidate, library)
    library.with_suffix(library.suffix + '.sha256').write_text(hashlib.sha256(library.read_bytes()).hexdigest() + '\n')
    (runtime / 'enabled').write_text('3.53.4\n')
    result = run(root, '''import sqlite3,subprocess,sys,json,os
child=subprocess.check_output([sys.executable,'-c','import sqlite3;print(sqlite3.sqlite_version)'],text=True).strip()
print(json.dumps({'self':sqlite3.sqlite_version,'child':child,'path':os.environ.get('LD_LIBRARY_PATH')}))''')
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload['self'] == payload['child'] == '3.53.4'
    assert payload['path'].split(':')[0] == str(library.parent)


def test_builder_rejects_unpinned_source_before_compilation(tmp_path):
    archive = tmp_path / 'wrong.zip'
    archive.write_bytes(b'wrong archive')
    result = subprocess.run([sys.executable, str(REPO / 'setup/install-sqlite-runtime.py'),
                             '--root', str(tmp_path / 'project'), '--archive', str(archive)],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode != 0
    assert 'SHA3' in result.stderr
    assert not list((tmp_path / 'project').rglob('*.so*'))


def test_selected_runtime_checksum_failure_stops_launch(tmp_path):
    root = installation(tmp_path)
    runtime = root / '.alpaca/toolchain/sqlite'
    library = runtime / '3.53.4/lib/libsqlite3.so.0'
    library.parent.mkdir(parents=True)
    library.write_bytes(b'changed runtime')
    library.with_suffix(library.suffix + '.sha256').write_text('0' * 64 + '\n')
    (runtime / 'enabled').write_text('3.53.4\n')
    result = run(root, 'print("launched")')
    assert result.returncode != 0 and 'launched' not in result.stdout
    assert 'checksum' in result.stderr


def test_builder_does_not_write_through_external_state_symlink(tmp_path):
    root = tmp_path / 'project'
    root.mkdir()
    outside = tmp_path / 'outside'
    outside.mkdir()
    (root / '.alpaca').symlink_to(outside, target_is_directory=True)
    archive = tmp_path / 'wrong.zip'
    archive.write_bytes(b'wrong archive')
    result = subprocess.run([sys.executable, str(REPO / 'setup/install-sqlite-runtime.py'),
                             '--root', str(root), '--archive', str(archive)],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode != 0
    assert list(outside.iterdir()) == []


def test_builder_recovers_missing_checksum_after_verified_manifest(tmp_path):
    candidate = os.environ.get('ALPACA_TEST_SQLITE_LIBRARY')
    archive = os.environ.get('ALPACA_TEST_SQLITE_ARCHIVE')
    if not candidate or not archive:
        pytest.skip('requires the pinned project-local build and source archive')
    root = tmp_path / 'project'
    library = root / '.alpaca/toolchain/sqlite/3.53.4/lib/libsqlite3.so.0'
    library.parent.mkdir(parents=True)
    shutil.copy2(candidate, library)
    manifest = Path(candidate).parents[1] / 'manifest.json'
    shutil.copy2(manifest, library.parents[1] / 'manifest.json')
    result = subprocess.run([sys.executable, str(REPO / 'setup/install-sqlite-runtime.py'),
                             '--root', str(root), '--archive', archive],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert library.with_suffix(library.suffix + '.sha256').read_text().strip() == hashlib.sha256(library.read_bytes()).hexdigest()


# The flow MCP launcher (bin/alpaca-mcp) left with the chip flow; a profile that ships an MCP server
# brings its own launcher. The CLI chain is the one core ships.
@pytest.mark.parametrize('launcher', ['alpaca'])
def test_cli_exec_chain_selects_the_runtime(tmp_path, launcher):
    candidate = os.environ.get('ALPACA_TEST_SQLITE_LIBRARY')
    if not candidate:
        pytest.skip('requires the pinned project-local build')
    root = installation(tmp_path)
    shutil.copy2(REPO / 'bin' / launcher, root / 'bin' / launcher)
    runtime = root / '.alpaca/toolchain/sqlite'
    library = runtime / '3.53.4/lib/libsqlite3.so.0'
    library.parent.mkdir(parents=True)
    shutil.copy2(candidate, library)
    library.with_suffix(library.suffix + '.sha256').write_text(hashlib.sha256(library.read_bytes()).hexdigest() + '\n')
    (runtime / 'enabled').write_text('3.53.4\n')
    code = 'import sqlite3;print(sqlite3.sqlite_version)\n'
    (root / 'alpaca').mkdir()
    (root / 'alpaca/__main__.py').write_text(code)
    env = {key:value for key,value in os.environ.items() if key not in ('LD_LIBRARY_PATH','PYTHONPATH')}
    result = subprocess.run([str(root / 'bin' / launcher)], env=env, cwd=tmp_path,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == '3.53.4'
