#!/usr/bin/env python3
"""Build the pinned Linux SQLite runtime inside project state; never activate it."""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import urllib.request
import zipfile

VERSION = '3.53.4'
PRODUCT = 'sqlite-amalgamation-3530400'
SOURCE = 'https://www.sqlite.org/2026/' + PRODUCT + '.zip'
SHA3 = '628a44cfe82c66aed1ccbbe85a562d2e33ebe64b3288981ed76285612227934e'
FLAGS = ['-O2', '-fPIC', '-shared', '-Wl,-soname,libsqlite3.so.0', '-pthread']
DEFINES = ['SQLITE_THREADSAFE=1', 'SQLITE_USE_URI=1', 'SQLITE_ENABLE_COLUMN_METADATA',
           'SQLITE_ENABLE_DBSTAT_VTAB', 'SQLITE_ENABLE_FTS3', 'SQLITE_ENABLE_FTS3_PARENTHESIS',
           'SQLITE_ENABLE_FTS4', 'SQLITE_ENABLE_FTS5', 'SQLITE_ENABLE_MATH_FUNCTIONS',
           'SQLITE_ENABLE_RTREE', 'SQLITE_ENABLE_UNLOCK_NOTIFY', 'SQLITE_ENABLE_SESSION',
           'SQLITE_ENABLE_PREUPDATE_HOOK', 'SQLITE_SECURE_DELETE', 'SQLITE_SOUNDEX',
           'SQLITE_LIKE_DOESNT_MATCH_BLOBS', 'SQLITE_MAX_VARIABLE_NUMBER=250000']
PROBE = '''import sqlite3,json,sys
assert sqlite3.sqlite_version=='3.53.4',sqlite3.sqlite_version
conn=sqlite3.connect(':memory:')
assert conn.execute("select json_extract('{\\\"x\\\":1}', '$.x')").fetchone()[0]==1
conn.execute('create virtual table search using fts5(body)')
conn.execute("insert into search values ('durable capture')")
assert conn.execute("select count(*) from search where search match 'durable'").fetchone()[0]==1
conn.commit()
target=sqlite3.connect(':memory:');conn.backup(target)
assert target.execute('pragma integrity_check').fetchone()[0]=='ok'
print(json.dumps({'sqlite_version':sqlite3.sqlite_version,'source_id':conn.execute('select sqlite_source_id()').fetchone()[0],
'compile_options':[r[0] for r in conn.execute('pragma compile_options')],'json':True,'fts5':True,'backup':True}))
'''


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def publish_checksum(library, expected):
    checksum = library.with_suffix(library.suffix + '.sha256')
    temporary = checksum.with_suffix(checksum.suffix + '.tmp')
    with temporary.open('w') as stream:
        stream.write(expected + '\n'); stream.flush(); os.fsync(stream.fileno())
    os.replace(temporary, checksum)


def build(root, archive=None):
    if platform.system() != 'Linux':
        raise RuntimeError('This optional loader/build contract supports Linux only')
    root = Path(root).resolve()
    base = root / '.alpaca/toolchain/sqlite'
    source_dir = base / 'sources'
    if not source_dir.resolve().is_relative_to(root):
        raise ValueError('SQLite generated state must remain inside the project')
    source_dir.mkdir(parents=True, exist_ok=True)
    archive = Path(archive).resolve() if archive else source_dir / (PRODUCT + '.zip')
    if not archive.exists():
        request = urllib.request.Request(SOURCE, headers={'User-Agent': 'Alpaca-project-runtime/1'})
        payload = urllib.request.urlopen(request, timeout=60).read()
        temporary = archive.with_suffix('.zip.download')
        temporary.write_bytes(payload)
        if hashlib.sha3_256(payload).hexdigest() != SHA3:
            raise ValueError('SQLite source SHA3 mismatch; download retained for inspection')
        os.replace(temporary, archive)
    if hashlib.sha3_256(archive.read_bytes()).hexdigest() != SHA3:
        raise ValueError('SQLite source SHA3 mismatch; compilation refused')
    version_dir = base / VERSION
    library = version_dir / 'lib/libsqlite3.so.0'
    manifest_path = version_dir / 'manifest.json'
    if not library.resolve().is_relative_to(root) or not manifest_path.resolve().is_relative_to(root):
        raise ValueError('SQLite runtime must remain inside the project')
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text())
        if manifest.get('version') != VERSION or manifest.get('source_sha3_256') != SHA3:
            raise ValueError('Existing SQLite manifest does not match the pinned source')
        if not library.is_file() or sha256(library) != manifest.get('library_sha256'):
            raise ValueError('Existing SQLite library differs from its manifest; repair explicitly')
        publish_checksum(library, manifest['library_sha256'])
        return manifest
    if library.exists():
        raise ValueError('Unrecorded SQLite library exists; refusing to overwrite it')
    build_dir = version_dir / ('build-' + datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S') + '-' + str(os.getpid()))
    build_dir.mkdir(parents=True)
    # Extract only the pinned members, never arbitrary archive paths.
    with zipfile.ZipFile(archive) as bundle:
        for name in ('sqlite3.c', 'sqlite3.h', 'sqlite3ext.h'):
            (build_dir / name).write_bytes(bundle.read(PRODUCT + '/' + name))
    compiler = shutil.which('cc')
    if not compiler:
        raise RuntimeError('A C compiler is required (cc)')
    output = build_dir / 'libsqlite3.so.0'
    command = [compiler, *FLAGS, *('-D' + item for item in DEFINES), str(build_dir / 'sqlite3.c'),
               '-o', str(output), '-ldl', '-lm']
    subprocess.run(command, check=True, timeout=240)
    env = dict(os.environ, LD_LIBRARY_PATH=str(build_dir) + (':' + os.environ['LD_LIBRARY_PATH'] if os.environ.get('LD_LIBRARY_PATH') else ''))
    probe = subprocess.run([sys.executable, '-c', PROBE], env=env, text=True, capture_output=True, check=True, timeout=30)
    verified = json.loads(probe.stdout)
    library.parent.mkdir(parents=True, exist_ok=True)
    with output.open('rb') as stream:
        os.fsync(stream.fileno())
    os.replace(output, library)
    manifest = {'version': VERSION, 'source_url': SOURCE, 'source_sha3_256': SHA3,
                'source_archive': str(archive.relative_to(root)) if archive.is_relative_to(root) else str(archive),
                'library': str(library.relative_to(root)), 'library_sha256': sha256(library),
                'compiler': subprocess.check_output([compiler, '--version'], text=True).splitlines()[0],
                'command': command, 'verified_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
                'probe': verified, 'activated': False}
    temporary = manifest_path.with_suffix('.json.tmp')
    with temporary.open('w') as stream:
        json.dump(manifest, stream, indent=2); stream.write('\n'); stream.flush(); os.fsync(stream.fileno())
    os.replace(temporary, manifest_path)
    publish_checksum(library, manifest['library_sha256'])
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--archive', type=Path)
    args = parser.parse_args()
    try:
        result = build(args.root, args.archive)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError, zipfile.BadZipFile) as error:
        print('SQLite runtime: ' + str(error), file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
