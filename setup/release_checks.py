#!/usr/bin/env python3
"""Alpaca-specific release gates, separate from the reusable release engine."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
#: The oldest Python setup/bootstrap.sh accepts. A release is installed and imported on it before a
#: publish (the floor-python gate): 1.2.0 shipped a pattern only 3.11+ compiles (op-008 t-571).
FLOOR = (3, 10)
#: Imports every Alpaca module but the tests and the command entry point; prints each that fails.
IMPORT_ALL = """
import importlib, pkgutil, sys
import alpaca
failed = []
for info in pkgutil.walk_packages(alpaca.__path__, 'alpaca.', onerror=lambda name: failed.append(name)):
    if info.name.startswith('alpaca.tests') or info.name == 'alpaca.__main__':
        continue
    try:
        importlib.import_module(info.name)
    except Exception as exc:
        failed.append(info.name + ': ' + type(exc).__name__ + ': ' + str(exc)[:200])
print('\\n'.join(failed) or 'every module imports on Python ' + sys.version.split()[0])
sys.exit(1 if failed else 0)
"""


def documentation(root):
    spec = importlib.util.spec_from_file_location('release_manual', root / 'setup/build_manual_html.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if module.render(str(root)) != (root / 'docs/manual.html').read_text():
        raise ValueError('manual HTML differs from regeneration')
    # Shipped HTML only; local runtime reports do not belong to the candidate.
    for path in root.rglob('*.html'):
        if any(p.startswith('.') for p in path.relative_to(root).parts):
            continue
        path.read_bytes().decode('ascii')
    for target in re.findall(r'\]\(([^)]+)\)', (root / 'README.md').read_text()):
        if '://' in target or target.startswith(('#', 'mailto:')):
            continue
        rel = unquote(target.split('#', 1)[0].split('?', 1)[0])
        if rel and not (root / rel).exists():
            raise ValueError('README local link is missing: ' + rel)
    print('documentation: regenerated manual, ASCII HTML and README links PASS')


def floor_python():
    """The interpreter of the floor version: ALPACA_FLOOR_PYTHON, else python3.10 on PATH."""
    python = os.environ.get('ALPACA_FLOOR_PYTHON') or shutil.which('python%d.%d' % FLOOR)
    if not python:
        raise RuntimeError('no Python %d.%d found: install it or set ALPACA_FLOOR_PYTHON' % FLOOR)
    version = subprocess.run([python, '-c', 'import sys; print(*sys.version_info[:2])'], text=True,
                             capture_output=True, check=True).stdout.split()
    if tuple(map(int, version)) != FLOOR:
        raise RuntimeError('%s is Python %s, not %d.%d' % (python, '.'.join(version), *FLOOR))
    return python


def floor(root):
    """The fresh installation smoke run on the floor Python, then every module imported there."""
    smoke(root, floor_python(), import_all=True)


def smoke(root, python=None, import_all=False):
    python = python or sys.executable
    scratch = root / '.alpaca/release-smoke'
    scratch.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=scratch) as temporary:
        copy = Path(temporary) / 'install'
        shutil.copytree(root, copy, ignore=shutil.ignore_patterns('.git', '.alpaca', '.venv', '__pycache__', '.pytest_cache'))
        from alpaca.release.environment import command_environment
        env = command_environment()
        def run(*args):
            result = subprocess.run(args, cwd=copy, env=env, text=True, capture_output=True)
            print(result.stdout, end='')
            if result.returncode:
                print(result.stderr, file=sys.stderr)
                raise RuntimeError('smoke command failed: ' + args[0])
            return result.stdout
        run('bash', 'setup/bootstrap.sh', '--python', python)
        if import_all:
            run('.venv/bin/python', '-c', IMPORT_ALL)
        def cli(*args):
            return run('bin/alpaca', *args)
        (copy / '.alpaca/transcripts').mkdir(parents=True)
        cli('onboard', '--name', 'Release smoke', '--who', 'Tester:owner', '--what', 'Validate a fresh installation.', '--tier', 'public')
        cli('doctor')
        op = cli('op', 'new', 'Exercise release installation', '--done-when', 'A sealed proof completes a task.').split()[0]
        task = cli('task', 'add', op, 'Check fresh installation lifecycle.', '--title', 'Release smoke', '--expected', 'A sealed proof.', '--done-bar', 'Doctor and proof cycle succeed.', '--phase', 'build').split()[0]
        cli('task', 'move', task, 'doing')
        cli('proof', 'new', task)
        rel = '.alpaca/proofs/' + op + '/' + task + '.md'
        proof = copy / rel
        evidence = copy / '.alpaca/smoke-evidence.txt'
        evidence.write_text('Fresh bootstrap, onboarding and doctor succeeded. This file is generated only after those commands return zero.\n')
        text = proof.read_text()
        parts = {
            'At a glance': 'Release smoke: a fresh installation bootstrapped, onboarded, passed doctor and completed a task with this sealed proof.',
            'What I did': 'Exercised the installed CLI from a fresh release copy through onboarding, doctor, operation and task creation.',
            'How I did it': 'Ran bootstrap with the current matrix interpreter and required every lifecycle command to return zero.',
            'Where': 'All commands run in a disposable installation below the release gate runtime directory.',
            'Result': 'Bootstrap, onboarding and doctor returned zero; this report is now sealed and used to complete the task.',
            'Deviations and issues': 'No acceptance checks were skipped. The installation copy isolates onboarding changes from the release candidate.',
            'How to reproduce': 'Run python setup/release_checks.py smoke from a clean release checkout with access to pinned dependencies.',
            'Evidence': '- local:.alpaca/smoke-evidence.txt - successful bootstrap and lifecycle command evidence',
        }
        for heading, body in parts.items():
            text, count = re.subn(r'(## ' + re.escape(heading) + r'\n\n)TODO\(agent\):[^\n]*', lambda m: m[1] + body, text, count=1)
            if count != 1:
                raise ValueError('proof format changed: ' + heading)
        proof.write_text(text)
        cli('proof', 'seal', task)
        cli('task', 'move', task, 'done', '--proof', 'local:' + rel)
        print('fresh installation and sealed task proof on Python %s: PASS' % '.'.join(
            run('.venv/bin/python', '-c', 'import sys; print(*sys.version_info[:2])').split()))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('gate', choices=['docs', 'smoke', 'floor'])
    args = parser.parse_args()
    {'docs': documentation, 'smoke': smoke, 'floor': floor}[args.gate](ROOT)
