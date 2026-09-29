"""Allowlisted staging with explicit overlays and input/output drift detection."""
import hashlib
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile

from .config import FORBIDDEN, contained, relative
from .receipts import BuildReceipt, digest
from .environment import command_environment


def git(root, *args):
    env = dict(os.environ, GIT_NO_REPLACE_OBJECTS='1', GIT_CONFIG_NOSYSTEM='1')
    return subprocess.run(['git', '-C', str(root), *args], check=True, capture_output=True, env=env).stdout


def forbidden(rel):
    return any(p in FORBIDDEN or p.startswith('.env.') for p in Path(rel).parts) or rel.endswith(('.pyc', '-wal', '-shm')) or rel in ('RESUME.md', 'CHECKLIST.md', 'board.json', 'data.json', '.claude/settings.local.json') or rel.startswith('.claude/worktrees/') or Path(rel).parts[0] in ('nuclear', 'napalm')


def allowlist(root, cfg):
    section, entries, memory = None, [], []
    for line in contained(root, cfg.allowlist).read_text().splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if line.startswith('[') and line.endswith(']'):
            section = line[1:-1]
            continue
        if section == cfg.section:
            entries.append(relative(line))
        if section == 'memory':
            memory.append(relative(line))
    if not entries:
        raise ValueError('allowlist has no release members')
    return entries, memory


def is_memory(rel, memory):
    return any(rel == item or rel.startswith(item + '/') for item in memory)


def selected(root, cfg):
    root = Path(root).resolve()
    entries, memory = allowlist(root, cfg)
    out = {}
    for entry in entries:
        if forbidden(entry) or is_memory(entry, memory):
            raise ValueError('forbidden release member')
        path = contained(root, entry)
        if not path.exists():
            raise ValueError('missing release member: ' + entry)
        paths = [path]
        if path.is_dir():
            paths = []
            for base, dirs, files in os.walk(path, followlinks=False):
                for name in dirs + files:
                    candidate = Path(base) / name
                    if candidate.is_symlink():
                        raise ValueError('symlink in release member')
                dirs[:] = sorted(d for d in dirs if not forbidden((Path(base) / d).relative_to(root).as_posix()) and not is_memory((Path(base) / d).relative_to(root).as_posix(), memory))
                paths.extend(Path(base) / f for f in sorted(files))
        for member in paths:
            rel = member.relative_to(root).as_posix()
            if forbidden(rel) or is_memory(rel, memory):
                continue
            if not stat.S_ISREG(member.lstat().st_mode):
                raise ValueError('non-regular release member')
            out[rel] = member
    for dest, source in cfg.overlays.items():
        if dest not in out:
            raise ValueError('overlay target must already be allowlisted')
        path = contained(root, source)
        if not path.is_file():
            raise ValueError('missing overlay source')
        out[dest] = path
    return dict(sorted(out.items()))


def inventory(files):
    return {rel: {'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                  'mode': 0o755 if path.stat().st_mode & 0o111 else 0o644}
            for rel, path in sorted(files.items())}


def tree_inventory(root):
    root = Path(root)
    files = {}
    for base, dirs, names in os.walk(root, followlinks=False):
        for name in dirs + names:
            if (Path(base) / name).is_symlink():
                raise ValueError('symlink in candidate tree')
        dirs[:] = sorted(d for d in dirs if d != '.git')
        for name in names:
            path = Path(base) / name
            if not stat.S_ISREG(path.lstat().st_mode):
                raise ValueError('non-regular candidate member')
            files[path.relative_to(root).as_posix()] = path
    return inventory(files)


def committed_inventory(root, tree):
    """Read actual Git blob bytes and modes, independent of checkout conversion."""
    result = {}
    for entry in git(root, 'ls-tree', '-r', '-z', tree).split(b'\0'):
        if not entry:
            continue
        meta, rel = entry.split(b'\t', 1)
        mode, kind, oid = meta.split()
        if kind != b'blob' or mode not in (b'100644', b'100755'):
            raise ValueError('unsupported object in committed release tree')
        result[rel.decode()] = {'mode': int(mode, 8) & 0o777,
                                'sha256': hashlib.sha256(git(root, 'cat-file', 'blob', oid.decode())).hexdigest()}
    return dict(sorted(result.items()))


def source_state(root, cfg):
    root = Path(root).resolve()
    files = selected(root, cfg)
    _, memory = allowlist(root, cfg)
    # Include tracked development scripts and policy, not volatile runtime state.
    for entry in git(root, 'ls-files', '--stage', '-z').decode().split('\0'):
        if not entry:
            continue
        meta, rel = entry.split('\t', 1)
        if meta.startswith('160000'):
            raise ValueError('gitlinks are unsupported release inputs')
        if forbidden(rel) or is_memory(rel, memory):
            continue
        path = contained(root, rel)
        if not path.is_file():
            raise ValueError('missing tracked release input')
        files['source:' + rel] = path
    for rel in ('project.yaml', cfg.allowlist, *cfg.overlays.values()):
        files['policy:' + rel] = contained(root, rel)
    sha = git(root, 'rev-parse', 'HEAD').decode().strip()
    return digest({'head': sha, 'files': inventory(files)}), sha


def build_release(root, config, out):
    root, out = Path(root).resolve(), Path(out).absolute()
    try:
        rel = out.relative_to(root).as_posix()
    except ValueError as exc:
        raise ValueError('release output must be project-local .alpaca/releases state') from exc
    if not rel.startswith('.alpaca/releases/'):
        raise ValueError('release output must be under .alpaca/releases/')
    out = contained(root, rel)
    if out.exists():
        raise ValueError('release output already exists')
    before, sha = source_state(root, config)
    files = selected(root, config)
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.staging-', dir=out.parent) as tmp:
        stage = Path(tmp)
        for rel, source in files.items():
            target = stage / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read_bytes())
            target.chmod(0o755 if source.stat().st_mode & 0o111 else 0o644)
        if config.generated_manifest:
            subprocess.run(config.generated_manifest, cwd=stage, env=command_environment(),
                           check=True, capture_output=True, timeout=120)
        members = tree_inventory(stage)
        _, memory = allowlist(root, config)
        if any(forbidden(rel) or is_memory(rel, memory) for rel in members):
            raise ValueError('generated output contains forbidden runtime state')
        after, _ = source_state(root, config)
        if after != before:
            raise ValueError('source changed during release build')
        os.rename(stage, out)
    return BuildReceipt(before, digest(members), sha, str(out), members)


def validate_build(root, config, build):
    root = Path(root).resolve()
    tree = Path(build.tree)
    try:
        rel = tree.relative_to(root).as_posix()
    except ValueError as exc:
        raise ValueError('candidate tree is outside project') from exc
    if not rel.startswith('.alpaca/releases/'):
        raise ValueError('candidate tree is outside release state')
    contained(root, rel)
    current, sha = source_state(root, config)
    if current != build.source_hash or sha != build.source_sha:
        raise ValueError('source changed since release build')
    if not tree.is_dir() or digest(tree_inventory(tree)) != build.tree_hash:
        raise ValueError('candidate tree changed since release build')
