"""Required gates run on independent copies and bind their results to frozen inputs."""
from dataclasses import asdict
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import uuid

import yaml

from alpaca import barrier
from . import approval
from .build import (allowlist, build_release, forbidden, git, inventory, is_memory,
                    selected, tree_inventory, validate_build)
from .config import MANDATORY, contained
from .receipts import BuildReceipt, CheckReceipt, digest
from .environment import command_environment


def policy(root):
    cfg = yaml.safe_load(contained(root, 'project.yaml').read_text())
    cfg = dict(cfg, tier='public')
    block = dict(cfg.get('barrier') or {})
    block['terms_optional'] = False
    block['protected_paths'] = sorted(set(block.get('protected_paths', [])) | {'.alpaca/', '.git/', '.env', '.venv/'})
    cfg['barrier'] = block
    terms, reasons, _, refusal = barrier._terms(str(root), cfg, base=str(root))
    if refusal is not None or terms is None or barrier.R_TERMS_UNCHECKED in reasons:
        raise ValueError('release leak policy is missing or invalid')
    return cfg, terms, digest({'barrier': block, 'terms_digest': terms.digest})


def init_repository(path):
    git(path, 'init', '-q', '-b', 'main')
    for key, value in [('user.name', 'Alpaca Release'), ('user.email', 'release@localhost'),
                       ('commit.gpgsign', 'false'), ('core.autocrlf', 'false'), ('core.fileMode', 'true')]:
        git(path, 'config', key, value)
    hooks = path / '.git/release-empty-hooks'
    hooks.mkdir()
    git(path, 'config', 'core.hooksPath', str(hooks))
    git(path, 'add', '--all', '--force')
    git(path, 'commit', '-qm', 'Alpaca release candidate')
    return git(path, 'rev-parse', 'HEAD').decode().strip()


def copy_files(files, target):
    target.mkdir(parents=True, exist_ok=False)
    for rel, source in files.items():
        path = target / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(source.read_bytes())
        path.chmod(0o755 if source.stat().st_mode & 0o111 else 0o644)


def dev_files(root, cfg):
    files = selected(root, cfg)
    # Development gates use original source bytes, before release overlays.
    for rel in list(files):
        original = contained(root, rel)
        if original.is_file():
            files[rel] = original
    _, memory = allowlist(root, cfg)
    for rel in git(root, 'ls-files', '-z').decode().split('\0'):
        if rel and not forbidden(rel) and not is_memory(rel, memory):
            files[rel] = contained(root, rel)
    return files


def run_command(command, cwd, timeout, log):
    env = command_environment()
    timed_out = False
    with log.open('wb') as output:
        try:
            process = subprocess.Popen(command, cwd=cwd, env=env, stdout=output,
                                       stderr=subprocess.STDOUT, start_new_session=True)
            try:
                code = process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
                code = 124
        except OSError as exc:
            output.write(('command could not start: ' + type(exc).__name__).encode())
            code = 127
    return code, timed_out


def check_release(root, config, build):
    root = Path(root).resolve()
    attempt = uuid.uuid4().hex
    work = contained(root, '.alpaca/releases/' + attempt + '/checks')
    work.mkdir(parents=True)
    gates = []
    policy_hash = ''

    def add(name, passed, detail='', **extra):
        gates.append(dict(name=name, status='PASS' if passed else 'FAIL',
                          returncode=0 if passed else 1, detail=detail, **extra))

    # Nothing runs, and the private term policy is not read, before an exact-commit approval.
    try:
        approved = approval.require(root, config, build.source_sha)
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        add('approval', False, str(exc) if type(exc) is ValueError else type(exc).__name__)
        return CheckReceipt(asdict(build), gates, 'BLOCKED', attempt=attempt)

    try:
        validate_build(root, config, build)
    except (ValueError, OSError) as exc:
        add('input-integrity', False, str(exc))
        return CheckReceipt(asdict(build), gates, 'BLOCKED', attempt=attempt, approval=approved)

    try:
        second = build_release(root, config, work.parent / 'rebuild/tree')
        add('reproducibility', second.tree_hash == build.tree_hash)
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        add('reproducibility', False, type(exc).__name__)

    seed = work / 'seed'
    try:
        copy_files({rel: Path(build.tree) / rel for rel in build.inventory}, seed)
        candidate = init_repository(seed)
        cfg, terms, policy_hash = policy(root)
        report = barrier.scan(str(seed), [candidate], cfg=cfg, term_list=terms)
        add('leak-scan', report.verdict == 0, ','.join(report.reasons))
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        add('leak-scan', False, type(exc).__name__)

    try:
        clone = work / 'fresh'
        git(work, 'clone', '-q', '--no-local', str(seed), str(clone))
        add('fresh-clone', digest(tree_inventory(clone)) == build.tree_hash)
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        add('fresh-clone', False, type(exc).__name__)

    dev_seed = None
    for index, gate in enumerate(config.gates):
        log = work / (gate['name'] + '.log')
        cwd = work / ('gate-' + str(index))
        try:
            origin = seed
            if gate.get('scope', 'release') == 'dev':
                if dev_seed is None:
                    dev_seed = work / 'dev-seed'
                    copy_files(dev_files(root, config), dev_seed)
                    init_repository(dev_seed)
                origin = dev_seed
            git(work, 'clone', '-q', '--no-local', str(origin), str(cwd))
            tracked = {rel: cwd / rel for rel in git(cwd, 'ls-files', '-z').decode().split('\0') if rel}
            before = inventory(tracked)
            code, timed_out = run_command(gate['command'], cwd, gate.get('timeout', 600), log)
            unchanged = inventory(tracked) == before and not git(cwd, 'diff', '--name-only', 'HEAD').strip()
            gates.append({'name': gate['name'], 'status': 'PASS' if code == 0 and unchanged else 'FAIL',
                          'returncode': code, 'timed_out': timed_out, 'log': str(log),
                          'detail': '' if unchanged else 'gate changed tracked input files'})
        except (ValueError, OSError, subprocess.SubprocessError) as exc:
            log.write_text('gate failed: ' + type(exc).__name__ + '\n')
            add(gate['name'], False, type(exc).__name__, log=str(log))
    try:
        validate_build(root, config, build)
        if policy(root)[2] != policy_hash:
            raise ValueError('release policy changed during checks')
        approval.require(root, config, build.source_sha, approved)
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        add('input-integrity', False, str(exc) if type(exc) is ValueError else type(exc).__name__)
    status = 'PASS' if all(g['status'] == 'PASS' for g in gates) else 'FAIL'
    return CheckReceipt(asdict(build), gates, status, policy_hash=policy_hash, attempt=attempt, approval=approved)


def validate_checks(root, config, checks):
    expected = set(MANDATORY) | {gate['name'] for gate in config.gates}
    if checks.status != 'PASS' or len(checks.gates) != len(expected) or {g.get('name') for g in checks.gates} != expected:
        raise ValueError('release checks are failed or incomplete')
    if any(g.get('status') != 'PASS' or g.get('returncode') != 0 for g in checks.gates):
        raise ValueError('release checks did not all pass')
    # The approval is confirmed before the private term policy is read.
    if not checks.approval:
        raise ValueError('release checks carry no owner approval')
    approval.require(root, config, checks.build['source_sha'], checks.approval)
    validate_build(root, config, BuildReceipt(**checks.build))
    if policy(root)[2] != checks.policy_hash:
        raise ValueError('release policy changed since checks')
