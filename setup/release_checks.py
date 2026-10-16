#!/usr/bin/env python3
"""Alpaca-specific release gates, separate from the reusable release engine."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from alpaca import releasetags  # noqa: E402  (one tag parser for this gate and `alpaca update`)
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


class GateError(RuntimeError):
    """A gate failure with a message the log shows as it is."""


def find_id(kind, text):
    """The first op (kind 'op') or task (kind 't') id a command printed."""
    found = re.search(r'\b%s-\d+\b' % re.escape(kind), text)
    if not found:
        raise GateError('no %s id in the command output: %s' % (kind, text.strip()[:200]))
    return found.group(0)


def lifecycle(cli, project, label):
    """One op, one task and one sealed proof report through the CLI `cli` of the project at
    `project`; returns (op, task, relative path of the sealed report). Shared by the fresh
    installation smoke and the upgrade gate, so both exercise the same record."""
    op = find_id('op', cli('op', 'new', 'Exercise release installation', '--done-when', 'A sealed proof completes a task.'))
    task = find_id('t', cli('task', 'add', op, 'Check fresh installation lifecycle.', '--title', label, '--expected', 'A sealed proof.', '--done-bar', 'Doctor and proof cycle succeed.', '--phase', 'build'))
    cli('task', 'move', task, 'doing')
    cli('proof', 'new', task)
    rel = '.alpaca/proofs/' + op + '/' + task + '.md'
    proof = project / rel
    evidence_name = label.lower().split()[-1] + '-evidence.txt'
    evidence = project / '.alpaca' / evidence_name
    evidence.write_text('Fresh bootstrap, onboarding and doctor succeeded. This file is generated only after those commands return zero.\n')
    text = proof.read_text()
    parts = {
        'At a glance': label + ': a fresh installation bootstrapped, onboarded, passed doctor and completed a task with this sealed proof.',
        'What I did': 'Exercised the installed CLI from a fresh release copy through onboarding, doctor, operation and task creation.',
        'How I did it': 'Ran bootstrap with the current matrix interpreter and required every lifecycle command to return zero.',
        'Where': 'All commands run in a disposable installation below the release gate runtime directory.',
        'Result': 'Bootstrap, onboarding and doctor returned zero; this report is now sealed and used to complete the task.',
        'Deviations and issues': 'No acceptance checks were skipped. The installation copy isolates onboarding changes from the release candidate.',
        'How to reproduce': 'Run python setup/release_checks.py smoke from a clean release checkout with access to pinned dependencies.',
        'Evidence': '- local:.alpaca/' + evidence_name + ' - successful bootstrap and lifecycle command evidence',
    }
    for heading, body in parts.items():
        text, count = re.subn(r'(## ' + re.escape(heading) + r'\n\n)TODO\(agent\):[^\n]*', lambda m: m[1] + body, text, count=1)
        if count != 1:
            raise ValueError('proof format changed: ' + heading)
    proof.write_text(text)
    cli('proof', 'seal', task)
    cli('task', 'move', task, 'done', '--proof', 'local:' + rel)
    return op, task, rel


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
        lifecycle(cli, copy, 'Release smoke')
        print('fresh installation and sealed task proof on Python %s: PASS' % '.'.join(
            run('.venv/bin/python', '-c', 'import sys; print(*sys.version_info[:2])').split()))


#: The public repository a release is installed from; ALPACA_PREVIOUS_RELEASE_REPO or --repo replaces it
#: (a test points it at a local repository). Not read from project.yaml: in a release tree that file is
#: the template overlay and has no `release` mapping.
PUBLIC_REPOSITORY = releasetags.DEFAULT_REPO
GATE = 'upgrade-previous'
#: Doctor checks that read the host, not the upgraded project; they may go OK -> WARN across the upgrade
#: (never to ERROR). A copied tree keeps old file times, so `uncommitted` warns after an upgrade of a
#: tree nobody committed.
ENVIRONMENT_CHECKS = frozenset({'uncommitted', 'disk', 'transcripts', 'observability'})
#: Doctor checks that must read OK after the upgrade, whatever the previous release called the rest.
CORE_CHECKS = ('record', 'manifest', 'upgrade', 'boot-block', 'hooks', 'project.yaml')
EVENT_COLUMNS = 'id, ts, session, actor, kind, op, ref, data, content_hash, prev_hash, hash'
#: Scratch folders hold three virtual environments and four copies of the tree.
MIN_FREE_BYTES = 3 * 1024 ** 3
SCRATCH_VARIABLES = ('ALPACA_ROOT', 'ALPACA_SESSION_ID', 'CLAUDE_CODE_SESSION_ID', 'CLAUDE_PROJECT_DIR',
                     'CLAUDECODE', 'CODEX_THREAD_ID', 'CODEX_SANDBOX', 'ALPACA_CALLER_CWD', 'VIRTUAL_ENV',
                     'PYTHONHOME')
TASK_TEXT = 'Check that an upgrade keeps the record of a project.'


def fail(step, reason):
    return GateError('%s: %s failed: %s' % (GATE, step, reason))


def tail(text, limit=1500):
    text = (text or '').strip()
    return text[-limit:] if len(text) > limit else text


def scratch_env():
    """The environment for commands of the scratch project: nothing that points at another record
    or marks the caller as an agent session."""
    from alpaca.release.environment import command_environment
    env = command_environment()
    for key in SCRATCH_VARIABLES:
        env.pop(key, None)
    return env


def git_env():
    """scratch_env for git alone: no prompt, no user or system configuration, no interactive ssh."""
    env = scratch_env()
    env.update(GIT_TERMINAL_PROMPT='0', GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL='/dev/null',
               GIT_SSH_COMMAND='ssh -o BatchMode=yes')
    return env


def repo_url(repo=None):
    return repo or os.environ.get('ALPACA_PREVIOUS_RELEASE_REPO') or PUBLIC_REPOSITORY


def parse_tags(ls_remote_text):
    """{(major, minor, patch): (tag, commit)} for every refs/tags/vX.Y.Z line; peeled (^{}) lines give
    the commit an annotated tag points at, and anything not exactly vX.Y.Z is ignored."""
    found = {}
    peeled = {}
    for line in (ls_remote_text or '').splitlines():
        sha, _, ref = line.strip().partition('\t')
        if ref.endswith('^{}'):
            peeled[ref[:-3]] = sha
            continue
        key = releasetags.parse_tag(ref[len('refs/tags/'):]) if ref.startswith('refs/tags/') else None
        if key and re.fullmatch(r'[0-9a-f]{40}', sha):
            found[key] = (releasetags.tag_of(key), sha, ref)
    return {key: (tag, peeled.get(ref, sha)) for key, (tag, sha, ref) in found.items()}


def previous_tag(ls_remote_text, version, override=None):
    """The newest vX.Y.Z tag strictly below `version`, or `override` when it is on the remote and
    not above `version`. Fails naming the step, never guesses."""
    current = releasetags.parse_version(version or '')
    if not current:
        raise fail('previous-tag', 'the candidate version %r is not X.Y.Z' % version)
    tags = parse_tags(ls_remote_text)
    if override:
        key = releasetags.parse_tag(override)
        if not key:
            raise fail('previous-tag', '--previous %r is not a vX.Y.Z tag' % override)
        if key not in tags:
            raise fail('previous-tag', '%s is not a tag of the repository (tags: %s)' % (
                override, ' '.join(tag for tag, _ in sorted(tags.values())) or 'none'))
        if key > current:
            raise fail('previous-tag', '%s is above the candidate %s' % (override, version))
        return tags[key][0]
    below = [key for key in tags if key < current]
    if not below:
        raise fail('previous-tag', 'no vX.Y.Z tag below %s in the repository (tags: %s)' % (
            version, ' '.join(tag for tag, _ in sorted(tags.values())) or 'none'))
    return tags[max(below)][0]


def tag_commit(ls_remote_text, tag):
    for tag_name, commit in parse_tags(ls_remote_text).values():
        if tag_name == tag:
            return commit
    return ''


def parse_doctor(text):
    """(gate word, {check name: level}, [(level, name, detail)]) of alpaca doctor output. The gate
    word is None unless exactly one `GATE alpaca-doctor:` line is present. Names longer than the
    column are whole; any other line is a continuation."""
    words = re.findall(r'^GATE alpaca-doctor: (\w+)', text or '', re.M)
    lines = [(m[0], m[1], m[2].strip()) for m in re.findall(r'^(OK|WARN|ERROR)[ \t]+(\S+)(?:[ \t]+(.*))?$', text or '', re.M)]
    levels = {}
    for level, name, _ in lines:
        # a name printed twice keeps its worst level, so a later OK never hides an earlier WARN
        if LEVEL_RANK[level.lower()] >= LEVEL_RANK.get(levels.get(name), -1):
            levels[name] = level.lower()
    return (words[0] if len(words) == 1 else None), levels, lines


LEVEL_RANK = {'ok': 0, 'warn': 1, 'error': 2}
#: A doctor row that says a scan did not run: `skipped|failed|not loaded: <Exception>` (a check module that
#: raised or lacks checks()), `checks returned no finding` and `malformed row:` (alpaca/doctor.py _record_free).
CRASHED_SCAN = re.compile(r'(?:(?:skipped|failed|not loaded): [A-Za-z_.]*(?:Error|Exception)\b|checks returned no finding|malformed row:)')


def check_doctor(step, out, code):
    """The doctor levels by name; fails unless doctor ran to its own verdict (PASS or WARN, exit 0 or
    1), printed no ERROR line and reported no crashed scan (a scan that raised shows as WARN)."""
    word, levels, lines = parse_doctor(out)
    if word is None:
        raise fail(step, 'doctor printed no single GATE alpaca-doctor line (exit %s): %s' % (code, tail(out, 600)))
    for level, name, detail in lines:
        if level == 'ERROR':
            raise fail(step, 'doctor check %s is ERROR: %s' % (name, detail))
        if CRASHED_SCAN.search(detail):
            raise fail(step, 'doctor check %s reports a scan that crashed: %s' % (name, detail))
    if word not in ('PASS', 'WARN') or code not in (0, 1) or (word == 'PASS') != (code == 0):
        raise fail(step, 'doctor verdict %s with exit %s: %s' % (word, code, tail(out, 600)))
    if not lines:
        raise fail(step, 'doctor printed no check lines')
    return levels


def core_problems(after):
    return ['doctor check %s is %s after the upgrade, not ok' % (name, after.get(name, 'missing'))
            for name in CORE_CHECKS if after.get(name) != 'ok']


#: Doctor check names a later release renamed: {old name: new name}. Empty today. A check that was OK and is
#: gone passes only when its entry here names a check that is OK after; add the entry in the change that renames.
RENAMED = {}


def doctor_problems(before, after):
    """Checks that went wrong across the upgrade: a check that is ERROR after; a check that was OK and is
    not OK after or is gone (unless RENAMED names its successor); a check the candidate adds that is not
    OK. The host-dependent checks (ENVIRONMENT_CHECKS) may go from OK to WARN, never to ERROR or gone."""
    problems = []
    for name in sorted(set(before) | set(after)):
        was, now = before.get(name), after.get(name)
        if now == 'error':
            problems.append('doctor check %s is error after the upgrade' % name)
        elif name in ENVIRONMENT_CHECKS and now is not None:
            continue
        elif was == 'ok' and now != 'ok':
            if now is None and RENAMED.get(name) and after.get(RENAMED[name]) == 'ok':
                continue
            problems.append('doctor check %s was ok before the upgrade and is %s after' % (name, now or 'gone'))
        elif was is None and now != 'ok':
            problems.append('doctor check %s is new in this release and is %s after the upgrade' % (name, now))
    return problems


def doctor_changes(before, after):
    """The lines the log shows for a check whose level or presence differs across the upgrade."""
    lines = []
    for name in sorted(set(before) | set(after)):
        was, now = before.get(name), after.get(name)
        if was == now:
            continue
        if now is None:
            lines.append('gone: %s (was %s)' % (name, was))
        elif was is None:
            lines.append('new: %s %s' % (name, now))
        else:
            lines.append('allowed: %s %s -> %s' % (name, was, now))
    return lines


def check_verify(step, out, code, before_events=None):
    """The event count of a passing alpaca verify; with before_events, no fewer than that."""
    gate = re.findall(r'^GATE alpaca-verify: (\w+) \(([^)]*)\)', out or '', re.M)
    if len(gate) != 1 or gate[0][0] != 'PASS' or code != 0:
        raise fail(step, 'alpaca verify did not pass (exit %s): %s' % (code, tail(out, 600)))
    count = re.search(r'(\d+) events?', gate[0][1])
    if not count:
        raise fail(step, 'alpaca verify gave no event count: %s' % gate[0][1])
    if before_events is not None and int(count[1]) < before_events:
        raise fail(step, 'alpaca verify read %s events, %d before the upgrade: the record moved' % (count[1], before_events))
    return int(count[1])


def check_upgrade(step, out, code):
    if code != 0 or not re.search(r'^GATE alpaca-upgrade: PASS', out or '', re.M):
        raise fail(step, 'alpaca upgrade did not pass (exit %s): %s' % (code, tail(out, 600)))


def check_plan(step, out, code):
    got = re.search(r'^GATE alpaca-upgrade-plan: PASS\b[^\n]*\n?[^\n]*?\brefresh=(\d+) ', out or '', re.M)
    if code != 0 or not got:
        raise fail(step, 'alpaca upgrade --plan did not pass (exit %s): %s' % (code, tail(out, 600)))
    if int(got[1]) == 0:
        raise fail(step, 'the plan refreshes nothing, so the candidate manifest was not read: ' + tail(out, 300))
    return int(got[1])


def task_status(out, task):
    for line in (out or '').splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] == task:
            return parts[1]
    return None


def event_problems(before, after):
    """Rows (all columns) of the earlier record that the later one does not hold, unchanged and in order."""
    if not before:
        return ['the record held no events before the upgrade, so nothing was compared']
    if len(after) < len(before):
        return ['%d events before the upgrade, %d after' % (len(before), len(after))]
    for index, (old, new) in enumerate(zip(before, after)):
        if tuple(old) != tuple(new):
            return ['event row %d (id %s) changed or moved across the upgrade' % (index + 1, old[0])]
    return []


def read_events(project):
    """Every event row of the project record, read-only (rows still in the WAL are included)."""
    db = Path(project) / '.alpaca/alpaca.db'
    if not db.is_file():
        raise fail('events', 'the project has no record file at %s' % db)
    try:
        conn = sqlite3.connect('file:%s?mode=ro' % db, uri=True)
        try:
            return [tuple(row) for row in conn.execute('SELECT %s FROM events ORDER BY id' % EVENT_COLUMNS)]
        finally:
            conn.close()
    except sqlite3.Error as error:
        raise fail('events', 'the record cannot be read: %s: %s' % (type(error).__name__, error))


def file_digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def preserved_digests(project):
    """sha256 of the files an upgrade must leave alone: the sealed proofs, project.yaml, README.md, intents/."""
    project = Path(project)
    found = {}
    for top in ('.alpaca/proofs', 'intents', 'project.yaml', 'README.md'):
        base = project / top
        files = [base] if base.is_file() else sorted(p for p in base.rglob('*') if p.is_file()) if base.is_dir() else []
        for path in files:
            found[path.relative_to(project).as_posix()] = file_digest(path)
    return found


def check_preserved(before, after):
    problems = ['%s %s' % (name, 'is gone' if name not in after else 'changed') for name, digest in sorted(before.items())
                if after.get(name) != digest]
    if problems:
        raise fail('preserved', 'files the upgrade must keep: ' + '; '.join(problems))


def outside_boot_block(text):
    from alpaca import manifest
    begin = text.find(manifest.BOOT_BEGIN)
    end = text.find(manifest.BOOT_END, begin) if begin != -1 else -1
    if begin != -1 and end != -1:
        text = text[:begin] + text[end + len(manifest.BOOT_END):]
    return [line for line in text.splitlines() if line.strip()]


def merged_problems(before, after):
    """Lines of a merged file (CLAUDE.md, AGENTS.md) outside its boot block that the upgrade dropped."""
    kept = set(outside_boot_block(after))
    return [line[:60] for line in outside_boot_block(before) if line not in kept]


def provenance(project, previous, tag):
    """The project was made by the previous release: its version is the tag's and the alpaca/ files
    the tag's manifest lists are the tag's bytes. Run before the upgrade, so a launcher that quietly
    ran the candidate cannot make the gate compare the candidate with itself."""
    def version_of(tree):
        text = (Path(tree) / 'alpaca/__init__.py').read_text()
        got = re.search(r'''VERSION\s*=\s*['"]([^'"]+)['"]''', text)
        return got[1] if got else None
    have, want = version_of(project), version_of(previous)
    if not have or have != want or 'v' + have != tag:
        raise fail('provenance', 'the project is version %s, the previous release %s is %s' % (have, tag, want))
    try:
        listed = json.loads((Path(previous) / 'MANIFEST.json').read_text()).get('files')
    except (OSError, ValueError, AttributeError) as error:
        raise fail('provenance', '%s has no readable MANIFEST.json: %s: %s' % (tag, type(error).__name__, error))
    if not isinstance(listed, dict):
        raise fail('provenance', 'the MANIFEST.json of %s has no files mapping' % tag)
    compared = {name: digest for name, digest in listed.items() if name.startswith('alpaca/') and not name.startswith('alpaca/tests/')}
    if not compared:
        raise fail('provenance', 'the MANIFEST.json of %s lists no alpaca/ file, so the project bytes cannot be compared' % tag)
    wrong = [name for name, digest in sorted(compared.items())
             if not (Path(project) / name).is_file() or file_digest(Path(project) / name) != (digest if isinstance(digest, str) else digest.get('sha256'))]
    if wrong:
        raise fail('provenance', 'the project differs from %s in %d file(s): %s' % (tag, len(wrong), ' '.join(wrong[:5])))
    return have


def refreshed_bytes(project, candidate):
    """Every file of a refreshed mechanism path of the candidate is the candidate's bytes in the project."""
    from alpaca import manifest
    from alpaca.upgrade import MERGE, PRESERVE
    wrong = []
    for rel in manifest.classes(str(candidate))['mechanism']:
        rel = rel.rstrip('/')
        if rel in MERGE or rel in PRESERVE:
            continue
        base = Path(candidate) / rel
        files = [base] if base.is_file() else sorted(p for p in base.rglob('*') if p.is_file()) if base.is_dir() else []
        for path in files:
            name = path.relative_to(candidate).as_posix()
            if '__pycache__' in name or name.endswith('.pyc'):
                continue
            mine = Path(project) / name
            if not mine.is_file() or file_digest(mine) != file_digest(path):
                wrong.append(name)
    if wrong:
        raise fail('refreshed-bytes', '%d refreshed file(s) are not the candidate\'s: %s' % (len(wrong), ' '.join(wrong[:8])))


def tree_digest(project):
    """One digest of the project tree outside the record database, the environment and git."""
    digest = hashlib.sha256()
    project = Path(project)
    for dirpath, dirnames, filenames in os.walk(project):
        dirnames[:] = sorted(d for d in dirnames if d not in ('.venv', '.git', '__pycache__'))
        for name in sorted(filenames):
            path = Path(dirpath) / name
            rel = path.relative_to(project).as_posix()
            if rel.startswith('.alpaca/alpaca.db') or path.is_symlink():
                continue
            digest.update(rel.encode() + b'\0' + file_digest(path).encode() + b'\n')
    return digest.hexdigest()


def leftovers(project):
    """What an upgrade that ended leaves behind: its journal and its folder."""
    base = Path(project) / '.alpaca/upgrade'
    return ['.alpaca/upgrade/journal.json'] if (base / 'journal.json').exists() else \
        ['.alpaca/upgrade'] if base.exists() else []


def copy_candidate(root, candidate):
    shutil.copytree(root, candidate, ignore=shutil.ignore_patterns('.git', '.alpaca', '.venv', '__pycache__', '.pytest_cache'))


def folder_size(path):
    total = 0
    for dirpath, _, filenames in os.walk(path):
        for name in filenames:
            try:
                total += os.lstat(os.path.join(dirpath, name)).st_size
            except OSError:
                pass
    return total


def install_command(previous, prefix, bin_dir):
    """The user's install of the previous release, confined to two scratch folders."""
    return ['bash', str(Path(previous) / 'install.sh'), '--from', str(previous), '--prefix', str(prefix), '--bin-dir', str(bin_dir)]


#: Prints the pinned lines of requirements.txt that the project's own environment lacks.
REQUIREMENTS = """
import importlib.metadata as md, re
have = {re.sub(r'[-_.]+', '-', d.metadata['Name']).lower(): d.version for d in md.distributions()}
for line in open('requirements.txt'):
    line = line.split('#')[0].strip()
    got = re.match(r'([A-Za-z0-9_.-]+)==([^;\\s]+)\\s*(?:;\\s*(.*))?', line)
    if got and got[3]:
        try:
            from packaging.markers import Marker
            if not Marker(got[3]).evaluate():
                continue
        except Exception:
            continue
    if got and have.get(re.sub(r'[-_.]+', '-', got[1]).lower()) != got[2]:
        print(line)
"""

PROBES = {
    'events': read_events,
    'preserved': preserved_digests,
    'provenance': provenance,
    'refreshed': refreshed_bytes,
    'tree_digest': tree_digest,
    'leftovers': leftovers,
    'copy_candidate': copy_candidate,
    'file_digest': file_digest,
    'read_text': lambda path: Path(path).read_text(),
    'same_file': lambda a, b: Path(a).read_bytes() == Path(b).read_bytes(),
    'exists': lambda path: Path(path).exists(),
}


def run_step(step, argv, cwd, env, timeout):
    """Run one command of the gate in its own session; returns (combined output, exit code). A command
    that cannot start (missing executable or folder) returns exit 127 with the reason, so the step that
    ran it fails by name; a timeout kills the whole group and returns 124."""
    print('%s: %s: start' % (GATE, step), flush=True)
    try:
        process = subprocess.Popen(argv, cwd=cwd, env=env, text=True, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, start_new_session=True)
    except OSError as error:
        return 'cannot start %s: %s: %s' % (argv[0], type(error).__name__, error), 127
    try:
        out, _ = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        out, _ = process.communicate()
        return (out or '') + '\ntimed out after %s s' % timeout, 124
    except BaseException:
        os.killpg(process.pid, signal.SIGKILL)
        raise
    return out, process.returncode


def run_sequence(ctx, run, probes=None):
    """The whole gate, step by step. `run(step, argv, cwd, env, timeout)` returns (output, exit code);
    a step that fails raises GateError naming it, so nothing runs after it. Any other exception (a
    probe reading a file or the record, code between two steps) becomes a failure of the step that was
    running; KeyboardInterrupt and SystemExit are not caught."""
    current = ['setup']
    try:
        return _sequence(ctx, run, probes, current)
    except GateError:
        raise
    except Exception as error:
        raise fail(current[0], '%s: %s' % (type(error).__name__, error)) from error


def _sequence(ctx, run, probes, current):
    probe = dict(PROBES, **(probes or {}))
    env, genv = scratch_env(), git_env()
    tmp, proj, prev, cand = ctx.tmp, ctx.proj, ctx.prev, ctx.cand
    alpaca_proj, alpaca_cand = str(proj / 'bin/alpaca'), str(cand / 'bin/alpaca')

    def step_ok(step, detail=''):
        print('%s: %s: PASS%s' % (GATE, step, ' (%s)' % detail if detail else ''), flush=True)

    def call(step, argv, cwd, environment=None, timeout=600):
        current[0] = step
        return run(step, [str(a) for a in argv], cwd, environment or env, timeout)

    def checked(step, name, *args):
        """probe[name](*args) as part of `step`: a GateError passes, any other exception fails the step."""
        current[0] = step
        try:
            return probe[name](*args)
        except GateError:
            raise
        except Exception as error:
            raise fail(step, '%s: %s' % (type(error).__name__, error)) from error

    def must(step, argv, cwd, environment=None, timeout=600, show=False):
        out, code = call(step, argv, cwd, environment, timeout)
        if code:
            raise fail(step, 'exit %s: %s' % (code, tail(out)))
        if show:
            print(tail(out, 4000))
        return out

    def project_cli(step, *args, **kwargs):
        return must(step, [alpaca_proj] + list(args), proj, **kwargs)

    # 1. the previous release
    out, code = call('previous-tag', ['git', '-c', 'credential.helper=', 'ls-remote', '--tags', ctx.repo], tmp, genv, 120)
    if code:
        raise fail('previous-tag', 'git ls-remote %s exited %s: %s' % (ctx.repo, code, tail(out, 600)))
    tag = previous_tag(out, ctx.version, ctx.previous)
    commit = tag_commit(out, tag)
    step_ok('previous-tag', '%s %s below %s' % (tag, commit, ctx.version) if not ctx.previous else
            '%s %s, given, candidate %s' % (tag, commit, ctx.version))
    must('clone-previous', ['git', '-c', 'credential.helper=', 'clone', '-q', '--depth', '1', '--branch', tag, ctx.repo, prev], tmp, genv, 300)
    head = must('clone-previous', ['git', '-C', prev, 'rev-parse', 'HEAD'], tmp, genv, 60).strip()
    if head != commit:
        raise fail('clone-previous', 'the clone is at %s, the tag %s is %s' % (head, tag, commit))
    step_ok('clone-previous', tag)

    # 2. the user's install and a project made by it
    if not checked('install-previous', 'exists', prev / 'install.sh'):
        raise fail('install-previous', '%s has no install.sh' % tag)
    helptext, code = call('install-previous', ['bash', prev / 'install.sh', '--help'], tmp)
    if code or '--from' not in helptext or '--bin-dir' not in helptext:
        raise fail('install-previous', '%s install.sh does not take --from and --bin-dir' % tag)
    must('install-previous', install_command(prev, ctx.prefix, ctx.bin_dir), tmp, timeout=900)
    release_cli = ctx.prefix / 'current/bin/alpaca'
    if not checked('install-previous', 'exists', release_cli):
        raise fail('install-previous', 'the install made no %s' % release_cli)
    step_ok('install-previous', tag)
    must('new-previous', [release_cli, 'new', '--name', 'Upgrade check', '--dir', proj, '--yes', '--no-serve',
                          '--who', 'Tester:owner', '--what', TASK_TEXT], tmp, timeout=900)
    step_ok('new-previous', 'alpaca new by the installed ' + tag)
    checked('provenance', 'provenance', proj, prev, tag)
    step_ok('provenance', 'the project holds the files of ' + tag)

    def cli(*args):
        return project_cli('lifecycle ' + ' '.join(args[:2]), *args)
    current[0] = 'lifecycle'
    try:
        op, task, rel = lifecycle(cli, proj, 'Upgrade check')
    except GateError as error:
        if str(error).startswith(GATE + ':'):
            raise
        raise fail('lifecycle', error)
    except Exception as error:
        raise fail('lifecycle', '%s: %s' % (type(error).__name__, error))
    step_ok('lifecycle', '%s %s sealed and done' % (op, task))

    # 3. what must read back unchanged
    before_events = check_verify('verify-before', project_cli('verify-before', 'verify', show=True), 0)
    rows_before = checked('events', 'events', proj)
    current[0] = 'snapshot'
    if before_events != len(rows_before):
        raise fail('snapshot', 'alpaca verify counted %d events, the record holds %d' % (before_events, len(rows_before)))
    kinds = {row[4] for row in rows_before}
    if not kinds >= {'op-open', 'task-add', 'task-move', 'proof-report'}:
        raise fail('snapshot', 'the earlier record lacks the op, task or proof events (kinds: %s)' % ' '.join(sorted(kinds)))
    dout, dcode = call('doctor-before', [alpaca_proj, 'doctor'], proj)
    doctor_before = check_doctor('doctor-before', dout, dcode)
    tasks_before = project_cli('tasks-before', 'task', 'list')
    if task_status(tasks_before, task) != 'done':
        raise fail('snapshot', 'task %s is not done before the upgrade: %s' % (task, tail(tasks_before, 300)))
    proof_before = checked('snapshot', 'file_digest', proj / rel)
    preserved_before = checked('snapshot', 'preserved', proj)
    claude_before = {name: checked('snapshot', 'read_text', proj / name) for name in ('CLAUDE.md', 'AGENTS.md')
                     if checked('snapshot', 'exists', proj / name)}
    left = checked('snapshot', 'leftovers', proj)
    if left:
        raise fail('snapshot', 'an upgrade is already in progress in the new project: ' + ' '.join(left))
    step_ok('snapshot', '%d events (%s), %d doctor checks, %d preserved files' % (
        len(rows_before), ' '.join(sorted(kinds)), len(doctor_before), len(preserved_before)))

    # 4. the candidate and the upgrade
    checked('candidate-copy', 'copy_candidate', ctx.root, cand)
    must('candidate-bootstrap', ['bash', 'setup/bootstrap.sh', '--python', ctx.python], cand, timeout=900)
    candidate_version = must('candidate-version', [alpaca_cand, '--version'], cand).strip()
    digest_before = checked('upgrade-plan', 'tree_digest', proj)
    plan = must('upgrade-plan', [alpaca_cand, 'upgrade', '--target', proj, '--plan'], cand, show=True)
    refresh = check_plan('upgrade-plan', plan, 0)
    if checked('upgrade-plan', 'tree_digest', proj) != digest_before or checked('upgrade-plan', 'leftovers', proj):
        raise fail('upgrade-plan', 'the plan changed the project')
    step_ok('upgrade-plan', '%d paths to refresh' % refresh)
    out, code = call('upgrade-apply', [alpaca_cand, 'upgrade', '--target', proj], cand)
    print(tail(out, 4000))
    check_upgrade('upgrade-apply', out, code)
    left = checked('upgrade-journal', 'leftovers', proj)
    if left:
        raise fail('upgrade-journal', 'the upgrade ended and left ' + ' '.join(left))
    step_ok('upgrade-apply', 'journal removed')
    checked('refreshed-bytes', 'refreshed', proj, cand)
    step_ok('refreshed-bytes', 'every refreshed file is the candidate\'s')

    # 5. the project after the upgrade, run by its own environment
    must('import-all', [proj / '.venv/bin/python', '-c', IMPORT_ALL], proj, timeout=300)
    step_ok('import-all', 'every module imports in the project')
    missing = must('requirements', [proj / '.venv/bin/python', '-c', REQUIREMENTS], proj, timeout=120).split()
    if missing:
        print('%s: requirements: NOTE the project environment lacks %s; alpaca upgrade does not install packages' % (GATE, ' '.join(missing)))
    if not checked('manifest', 'same_file', proj / 'MANIFEST.json', cand / 'MANIFEST.json'):
        print('%s: manifest: NOTE the project MANIFEST.json is not the candidate\'s; alpaca upgrade does not refresh it' % GATE)
    version = must('version-after', [alpaca_proj, '--version'], proj).strip()
    if version != candidate_version:
        raise fail('version-after', 'the project says %r, the candidate %r' % (version, candidate_version))
    step_ok('version-after', version)
    check_verify('verify-after', project_cli('verify-after', 'verify', show=True), 0, before_events)
    dout, dcode = call('doctor-after', [alpaca_proj, 'doctor'], proj)
    print(tail(dout, 6000))
    doctor_after = check_doctor('doctor-after', dout, dcode)
    problems = core_problems(doctor_after) + doctor_problems(doctor_before, doctor_after)
    if problems:
        raise fail('doctor-after', '; '.join(problems))
    for line in doctor_changes(doctor_before, doctor_after):
        print('%s: doctor-after: %s' % (GATE, line))
    step_ok('doctor-after', '%d checks, none worse' % len(doctor_after))
    problems = event_problems(rows_before, checked('events', 'events', proj))
    if problems:
        raise fail('events', '; '.join(problems))
    step_ok('events', 'the %d earlier rows read back unchanged' % len(rows_before))
    if task_status(project_cli('tasks-after', 'task', 'list'), task) != 'done':
        raise fail('tasks', 'task %s is not done after the upgrade' % task)
    out = project_cli('proof-check', 'proof', 'check', task)
    if not re.search(r'^GATE alpaca-proof-check: PASS', out, re.M) or checked('proof-check', 'file_digest', proj / rel) != proof_before:
        raise fail('proof-check', 'the sealed proof of %s no longer checks: %s' % (task, tail(out, 400)))
    step_ok('proof-check', task)
    current[0] = 'preserved'
    check_preserved(preserved_before, checked('preserved', 'preserved', proj))
    for name, text in claude_before.items():
        if not checked('preserved', 'exists', proj / name):
            raise fail('preserved', '%s is gone after the upgrade' % name)
        dropped = merged_problems(text, checked('preserved', 'read_text', proj / name))
        if dropped:
            raise fail('preserved', '%s lost its own lines: %s' % (name, '; '.join(dropped[:3])))
    step_ok('preserved', 'proofs, project.yaml, README.md, intents and the project lines of CLAUDE.md')
    return tag


def upgrade(root, previous=None, repo=None):
    from alpaca import VERSION
    scratch = root / '.alpaca/release-upgrade'
    started = time.time()
    try:
        scratch.mkdir(parents=True, exist_ok=True)
        free = shutil.disk_usage(scratch).free
        temporary = tempfile.TemporaryDirectory(dir=scratch, ignore_cleanup_errors=True)
    except OSError as error:
        raise fail('scratch', 'cannot use %s: %s: %s' % (scratch, type(error).__name__, error))
    if free < MIN_FREE_BYTES:
        temporary.cleanup()
        raise fail('disk', 'only %d MB free below %s, %d MB needed' % (free >> 20, scratch, MIN_FREE_BYTES >> 20))
    with temporary as name:
        tmp = Path(name)
        ctx = type('Context', (), dict(
            root=root, tmp=tmp, prev=tmp / 'previous', proj=tmp / 'project', cand=tmp / 'candidate',
            prefix=tmp / 'prefix', bin_dir=tmp / 'bin', repo=repo_url(repo), previous=previous,
            version=VERSION, python=sys.executable))
        try:
            tag = run_sequence(ctx, run_step)
        finally:
            print('%s: scratch peak %d MB' % (GATE, folder_size(tmp) >> 20))
    print('%s: PASS (%d s) from %s to %s' % (GATE, time.time() - started, tag, VERSION))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('gate', choices=['docs', 'smoke', 'floor', 'upgrade'])
    parser.add_argument('--previous', help='upgrade: the release tag to upgrade from, default the newest below VERSION')
    parser.add_argument('--repo', help='upgrade: the repository to read release tags from')
    args = parser.parse_args(argv)
    if args.gate != 'upgrade':
        {'docs': documentation, 'smoke': smoke, 'floor': floor}[args.gate](ROOT)
        return 0
    try:
        upgrade(ROOT, previous=args.previous, repo=args.repo)
    except GateError as error:
        print(error, file=sys.stderr)
        return 1
    except Exception as error:
        print('%s: setup failed: %s: %s' % (GATE, type(error).__name__, error), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
