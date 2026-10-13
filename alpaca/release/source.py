"""A private checkout of one development commit, so a release reads that commit and nothing else.

The project root is shared: other sessions edit, commit and merge there while a release builds,
checks and publishes. A release therefore reads its source files from .alpaca/releases/source/<sha>,
a clone of the project repository checked out at that commit, and keeps only its record, receipts,
term list and scratch in the project root. An edit or a commit in the project root cannot change
what a running release reads; an edit inside the checkout still fails the release (op-056 t-610).

The checkout leaves out .alpaca/: no release input lives there (build.forbidden), and its tracked
record files are most of the commit's size."""
import hashlib
import os
from pathlib import Path
import re
import stat
import shutil
import subprocess
import tempfile
import time

from .build import git
from .config import contained

SNAPSHOTS = '.alpaca/releases/source'
#: A checkout unused this long is removed when another one is made; a later release step on its
#: commit makes it again, byte for byte.
KEEP_S = 3 * 24 * 3600
SPARSE = ('/*', '!/.alpaca/')


def head(root):
    return git(root, 'rev-parse', 'HEAD').decode().strip()


def _clone(root, sha, target):
    """Clone root into target at sha: objects shared with root, no hooks, no checkout conversion."""
    run = lambda *args: git(target, '-c', 'core.hooksPath=' + os.devnull, *args)
    git(target.parent, 'clone', '-q', '--shared', '--no-checkout', '--template=', str(root), str(target))
    for key, value in [('core.autocrlf', 'false'), ('core.fileMode', 'true'),
                       ('core.attributesFile', os.devnull), ('advice.detachedHead', 'false')]:
        run('config', key, value)
    run('sparse-checkout', 'set', '--no-cone', *SPARSE)
    run('checkout', '-q', '--detach', sha)
    if head(target) != sha:
        raise ValueError('release source checkout is not at the approved commit')


def _git_bytes(root, *args, stdin=b''):
    env = dict(os.environ, GIT_NO_REPLACE_OBJECTS='1', GIT_CONFIG_NOSYSTEM='1')
    return subprocess.run(['git', '-C', str(root), *args], input=stdin, check=True, capture_output=True, env=env).stdout


def verify(tree, sha):
    """Refuse a checkout whose files differ from the commit's, outside the .alpaca/ it leaves out.

    Each file's blob id (as Git would store it, under the commit's own attributes) and mode is compared
    with the commit's tree, and the index must list exactly the commit's paths. Nothing here trusts the
    index's change flags, so an edit hidden with `git update-index --assume-unchanged` or
    `--skip-worktree` is still found before a build reads it."""
    expected = {}
    for entry in _git_bytes(tree, 'ls-tree', '-r', '-z', '--full-tree', sha).split(b'\0'):
        if entry:
            meta, rel = entry.split(b'\t', 1)
            mode, _, oid = meta.split()
            if not rel.startswith(b'.alpaca/'):
                expected[rel.decode()] = (mode.decode(), oid.decode())
    listed = {rel for rel in _git_bytes(tree, 'ls-files', '-z').decode().split('\0')
              if rel and not rel.startswith('.alpaca/')}
    if listed != set(expected):
        raise ValueError('release source checkout lists files its commit does not hold')
    files = sorted(rel for rel, (mode, _) in expected.items() if mode in ('100644', '100755'))
    for rel, (mode, oid) in expected.items():
        path = tree / rel
        if mode == '120000':
            target = os.readlink(path).encode() if path.is_symlink() else None
            if target is None or hashlib.sha1(b'blob %d\0' % len(target) + target).hexdigest() != oid:
                raise ValueError('release source checkout differs from its commit: ' + rel)
        elif mode in ('100644', '100755'):
            info = path.lstat() if os.path.lexists(path) else None
            if info is None or not stat.S_ISREG(info.st_mode) or bool(info.st_mode & 0o111) != (mode == '100755'):
                raise ValueError('release source checkout differs from its commit: ' + rel)
    found = _git_bytes(tree, 'hash-object', '--stdin-paths', stdin='\n'.join(files).encode() + b'\n').decode().split()
    for rel, oid in zip(files, found):
        if oid != expected[rel][1]:
            raise ValueError('release source checkout differs from its commit: ' + rel)


def _prune(base, keep):
    now = time.time()
    for path in base.iterdir():
        if path.name.startswith('.') or path == keep or not path.is_dir():
            continue
        try:
            if now - path.stat().st_mtime > KEEP_S:
                shutil.rmtree(path)
        except OSError:
            pass


def snapshot(root, sha):
    """The checkout of sha under the project's release state, made on first use."""
    root = Path(root).resolve()
    if not isinstance(sha, str) or not re.fullmatch(r'[0-9a-f]{40}', sha):
        raise ValueError('release source needs a full 40-character commit SHA')
    target = contained(root, SNAPSHOTS + '/' + sha)
    if not target.is_dir():
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='.clone-', dir=target.parent) as tmp:
            made = Path(tmp) / 'repo'
            try:
                _clone(root, sha, made)
            except subprocess.CalledProcessError:
                raise ValueError('release source commit cannot be checked out') from None
            try:
                os.rename(made, target)
            except OSError:
                if not target.is_dir():
                    raise
        _prune(target.parent, target)
    if head(target) != sha:
        raise ValueError('release source checkout is not at the approved commit')
    verify(target, sha)
    os.utime(target)
    return target
