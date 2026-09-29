"""Strict project-owned release configuration; no implicit disabled checks."""
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
import re

import yaml


MANDATORY = ('reproducibility', 'leak-scan', 'fresh-clone')
FORBIDDEN = {'.git', '.alpaca', '.venv', '.env', '__pycache__', '.pytest_cache', 'node_modules'}


def relative(value):
    if not isinstance(value, str) or not value or '\\' in value or any(ord(c) < 32 for c in value):
        raise ValueError('invalid relative path')
    value = value.rstrip('/')
    path = PurePosixPath(value)
    if path.is_absolute() or any(x in ('', '.', '..') for x in value.split('/')) or ':' in value:
        raise ValueError('non-canonical relative path')
    return value


def contained(root, value):
    root = Path(root).resolve()
    path = root / relative(value)
    for item in (path, *path.parents):
        if item == root:
            break
        if item.is_symlink():
            raise ValueError('symlink in release path')
    if not path.resolve().is_relative_to(root):
        raise ValueError('release path escapes project')
    return path


def argv(value):
    if not isinstance(value, list) or not value or any(not isinstance(x, str) or not x or '\0' in x for x in value):
        raise ValueError('command must be a nonempty argv list')
    return value


@dataclass(frozen=True)
class ReleaseConfig:
    allowlist: str
    repository: str
    gates: list
    section: str = 'mechanism'
    target_branch: str = 'main'
    sync_branch: str = 'sync/main'
    overlays: dict = None
    generated_manifest: list = None
    public_base: str = None


def load_config(root):
    path = contained(root, 'project.yaml')
    data = yaml.safe_load(path.read_text())
    cfg = data.get('release') if isinstance(data, dict) else None
    if not isinstance(cfg, dict):
        raise ValueError('project.yaml needs a release mapping; see docs/release.md')
    if set(cfg) - set(ReleaseConfig.__dataclass_fields__):
        raise ValueError('unknown release configuration; mandatory checks cannot be disabled')
    for key in ('allowlist', 'repository', 'gates'):
        if key not in cfg:
            raise ValueError('missing release.' + key)
    relative(cfg['allowlist'])
    if not isinstance(cfg['repository'], str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*', cfg['repository']):
        raise ValueError('repository must be owner/name')
    for key in ('target_branch', 'sync_branch'):
        value = cfg.get(key, 'main' if key == 'target_branch' else 'sync/main')
        if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_./-]*', value) or any(x in value for x in ('..', '//', '.lock')) or value.endswith(('/', '.')):
            raise ValueError('invalid branch name')
    if cfg.get('target_branch', 'main') == cfg.get('sync_branch', 'sync/main'):
        raise ValueError('sync branch must differ from target branch')
    if cfg.get('section', 'mechanism') is not None and not isinstance(cfg.get('section', 'mechanism'), str):
        raise ValueError('section must be text or null')
    if cfg.get('section') == 'memory':
        raise ValueError('memory is not a release section')
    gates = cfg['gates']
    if not isinstance(gates, list) or not gates:
        raise ValueError('at least one project gate is required')
    names = set(MANDATORY)
    for gate in gates:
        if not isinstance(gate, dict) or set(gate) - {'name', 'command', 'scope', 'timeout'}:
            raise ValueError('invalid gate configuration')
        name = gate.get('name')
        if not isinstance(name, str) or not re.fullmatch(r'[a-z][a-z0-9-]*', name) or name in names:
            raise ValueError('gate names must be unique and cannot replace mandatory checks')
        names.add(name)
        argv(gate.get('command'))
        if gate.get('scope', 'release') not in ('dev', 'release'):
            raise ValueError('gate scope must be dev or release')
        timeout = gate.get('timeout', 600)
        if isinstance(timeout, bool) or not isinstance(timeout, int) or not 1 <= timeout <= 7200:
            raise ValueError('gate timeout must be 1..7200 seconds')
    overlays = cfg.get('overlays', {})
    if not isinstance(overlays, dict):
        raise ValueError('overlays must map destination paths to source paths')
    for dest, source in overlays.items():
        relative(dest)
        contained(root, source)
    if cfg.get('generated_manifest') is not None:
        argv(cfg['generated_manifest'])
    if cfg.get('public_base') is not None and not re.fullmatch(r'[0-9a-f]{40}', str(cfg['public_base'])):
        raise ValueError('public_base must be a full commit SHA')
    return ReleaseConfig(**dict(cfg, overlays=overlays))
