"""Required gates run on independent copies and bind their results to frozen inputs."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import uuid

import yaml

from alpaca import barrier, db
from . import approval
from .build import (allowlist, build_release, forbidden, git, inventory, is_memory,
                    selected, tree_inventory, validate_build)
from .config import MANDATORY, contained
from .receipts import BuildReceipt, CheckReceipt, PreflightReceipt, digest, load_receipt
from .environment import command_environment
from .progress import QUIET


def policy(root, source=None):
    """The release leak policy: the barrier block of source's project.yaml (the checked commit's,
    release/source.py) and the private term list it names, read from the project root."""
    cfg = yaml.safe_load(contained(source or root, 'project.yaml').read_text())
    cfg = dict(cfg, tier='public')
    block = dict(cfg.get('barrier') or {})
    block['terms_optional'] = False
    block['protected_paths'] = sorted(set(block.get('protected_paths', [])) | {'.alpaca/', '.git/', '.env', '.venv/'})
    cfg['barrier'] = block
    terms, reasons, _, refusal = barrier._terms(str(root), cfg, base=str(root))
    if refusal is not None or terms is None or barrier.R_TERMS_UNCHECKED in reasons:
        raise ValueError('release leak policy is missing or invalid')
    return cfg, terms, digest({'barrier': block, 'terms_digest': terms.digest})


def seed_git(path, *args, accept=(0,), stdin=None):
    """git for a seed repository that sees only the tree's own ignore rules: no global or system
    config (so no core.excludesFile and no init.templateDir with an info/exclude), no host-wide
    $XDG_CONFIG_HOME/git/ignore, no template. A release host's own ignore settings can then never
    keep a candidate file out of the seed (review M-1)."""
    env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    env.update(GIT_NO_REPLACE_OBJECTS='1', GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull)
    done = subprocess.run(['git', '-c', 'core.excludesFile=' + os.devnull, '-C', str(path), *args],
                          capture_output=True, env=env, input=stdin)
    if done.returncode not in accept:
        raise subprocess.CalledProcessError(done.returncode, ['git', *args], done.stdout, done.stderr)
    return done.stdout


def init_repository(path, tracked=()):
    """Commit the files under path the way a fresh repository would take them.

    Nothing is force-added: a file the tree's own .gitignore ignores stays out of the commit, where
    left_out() reports it, so a candidate member that a clone would never carry cannot pass the
    leak scan or the fresh-clone gate unseen. Only the tree's own ignore rules apply (seed_git).
    tracked names files the source repository already tracks (the development seed); only those are
    added past an ignore rule, one path at a time."""
    seed_git(path, 'init', '-q', '--template=', '-b', 'main')
    for key, value in [('user.name', 'Alpaca Release'), ('user.email', 'release@localhost'),
                       ('commit.gpgsign', 'false'), ('core.autocrlf', 'false'), ('core.fileMode', 'true')]:
        seed_git(path, 'config', key, value)
    hooks = path / '.git/release-empty-hooks'
    hooks.mkdir()
    seed_git(path, 'config', 'core.hooksPath', str(hooks))
    seed_git(path, 'add', '--all')
    missing = sorted(set(tracked) & set(left_out(path)))
    for start in range(0, len(missing), 500):
        seed_git(path, 'add', '--force', '--', *missing[start:start + 500])
    seed_git(path, 'commit', '-qm', 'Alpaca release candidate')
    return seed_git(path, 'rev-parse', 'HEAD').decode().strip()


def left_out(path):
    """Files in the work tree of path that its commit does not hold (ignored by its .gitignore)."""
    return sorted(rel for rel in seed_git(path, 'ls-files', '--others', '-z').decode().split('\0') if rel)


def ignore_sources(path, rels):
    """'<file> (<ignore file>:<line>)' for each file, naming the rule that keeps it out of the seed."""
    out = seed_git(path, 'check-ignore', '--no-index', '-v', '-z', '--stdin', accept=(0, 1),
                   stdin=b'\0'.join(r.encode() for r in rels) + b'\0' if rels else b'')
    fields = out.decode().split('\0')
    found = {fields[i + 3]: '%s:%s' % (fields[i], fields[i + 1]) for i in range(0, len(fields) - 3, 4)}
    return ['%s (%s)' % (rel, found.get(rel, 'no ignore rule')) for rel in rels]


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


#: Seconds a timed-out command has after SIGINT to report before the group is killed.
INTERRUPT_GRACE_S = 60


#: Seconds between checks of a running gate for its timeout and for a stop request.
POLL_S = 0.5
#: The exit code recorded for a gate stopped because another gate failed first.
STOPPED_CODE = 130


def interrupt(process):
    """SIGINT the command's process group first: pytest stops and still prints its summary (the names
    of the tests that failed so far) before the group is killed (op-008 t-571: a CI run cut off by
    SIGKILL at 96% named none of its 13 failures). What is left after INTERRUPT_GRACE_S is killed."""
    for sig in (signal.SIGINT, signal.SIGKILL):
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            break       # the whole group is gone
        try:
            process.wait(timeout=INTERRUPT_GRACE_S if sig == signal.SIGINT else None)
        except subprocess.TimeoutExpired:
            pass
    process.wait()


def run_command(command, cwd, timeout, log, stop=None, state=None):
    """Run command in its own process group with its output in log; return (exit code, timed out).
    A command still running at timeout gets code 124; one still running when stop (a
    threading.Event) is set gets STOPPED_CODE and state['stopped'] = True. Both are interrupted
    first and killed after; so is a command whose caller is interrupted (Ctrl-C).

    The command gets a temp folder of its own as TMPDIR, removed when it ends: a test run that
    shared the user's default temp folder had its pytest folders rotated away by another session's
    pytest run in the middle of a check (op-056). It sits directly under /tmp, so paths under it
    stay short."""
    env = command_environment()
    scratch = tempfile.mkdtemp(prefix='ag', dir='/tmp' if os.path.isdir('/tmp') else None)
    env['TMPDIR'] = scratch
    try:
        return _run(command, cwd, timeout, log, stop, state, env)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _run(command, cwd, timeout, log, stop, state, env):
    timed_out = False
    with log.open('wb') as output:
        try:
            process = subprocess.Popen(command, cwd=cwd, env=env, stdout=output,
                                       stderr=subprocess.STDOUT, start_new_session=True)
            deadline = time.monotonic() + timeout
            try:
                while True:
                    try:
                        code = process.wait(timeout=max(0, min(POLL_S, deadline - time.monotonic())))
                        break
                    except subprocess.TimeoutExpired:
                        if time.monotonic() >= deadline:
                            timed_out, code = True, 124
                        elif stop is not None and stop.is_set():
                            code = STOPPED_CODE
                            if state is not None:
                                state['stopped'] = True
                        else:
                            continue
                        interrupt(process)
                        break
            except BaseException:
                interrupt(process)
                raise
        except OSError as exc:
            output.write(('command could not start: ' + type(exc).__name__).encode())
            code = 127
    return code, timed_out


def stamp(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


def gate_order(gates):
    """The project gates as (configured index, gate) in the order a check runs them: shortest timeout
    first, the configured order among equal timeouts. A short timeout marks a cheap gate, so a
    failure shows before the long gates start (op-056 t-611)."""
    return sorted(enumerate(gates), key=lambda item: item[1].get('timeout', 600))


def runner():
    """What runs the gate commands: this interpreter and host. Part of a gate's definition, so a PASS
    never carries over to another interpreter (the rest of the toolchain is not fingerprinted)."""
    return {'python': sys.executable, 'version': sys.version, 'host': platform.node()}


def definition(gate):
    """A digest of what a project gate runs and where. Every project-gate result records it, so a
    later check can tell which gate a PASS was for (op-011 t-615)."""
    return digest({'command': gate['command'], 'scope': gate.get('scope', 'release'),
                   'timeout': gate.get('timeout', 600), 'runner': runner()})


def run_gates(work, config, seed, source, gates, progress=QUIET, carried=None):
    """Run the project gates in gate_order, each on a fresh clone: of seed (the candidate repository)
    for release scope, of the development files of source for dev scope. One result per gate is
    appended to gates, which may already hold earlier results, in gate_order whatever order the gates
    finish in; each gate that ran carries `started` and `duration_s`.

    release.parallel gates run at once (1, the default, runs them one by one), started in gate_order.
    With release.fail_fast (the default) the first failed check or gate ends the run: a gate not yet
    started is recorded as SKIPPED, a running one is interrupted and recorded as STOPPED, and the
    receipt is FAIL, as it would be after running them (op-056 t-611, t-613).

    carried maps a gate name to the result of an earlier check (carry_plan): that entry is recorded
    in the gate's place, the gate does not run and has no progress step. Every result of a project
    gate carries its `definition`."""
    carried = carried or {}
    # stop is set by the first failure under fail fast, and by an interrupted run (Ctrl-C), so the
    # gates still running are interrupted too
    lock, seed_lock, stop = threading.Lock(), threading.Lock(), threading.Event()
    failed = [g['name'] for g in gates if g['status'] != 'PASS']
    dev = {}

    def dev_seed():
        with seed_lock:
            if 'error' in dev:
                raise dev['error']
            if 'seed' not in dev:
                try:
                    path = work / 'dev-seed'
                    sources = dev_files(source, config)
                    copy_files(sources, path)
                    init_repository(path, tracked=sources)
                except (ValueError, OSError, subprocess.SubprocessError) as exc:
                    dev['error'] = exc
                    raise
                dev['seed'] = path
            return dev['seed']

    def run(index, gate):
        if gate['name'] in carried:
            return dict(carried[gate['name']])
        result = execute(index, gate)
        result['definition'] = definition(gate)
        return result

    def execute(index, gate):
        with lock:
            first = failed[0] if failed and config.fail_fast else None
        if first:
            progress.finish(gate['name'], 'SKIPPED')
            return {'name': gate['name'], 'status': 'SKIPPED', 'returncode': None, 'timed_out': False,
                    'detail': 'not run: %s failed first (release.fail_fast)' % first}
        log = work / (gate['name'] + '.log')
        cwd = work / ('gate-' + str(index))
        started, clock = time.time(), time.monotonic()
        progress.start(gate['name'])
        ran = {}
        try:
            origin = dev_seed() if gate.get('scope', 'release') == 'dev' else seed
            git(work, 'clone', '-q', '--no-local', str(origin), str(cwd))
            tracked = {rel: cwd / rel for rel in git(cwd, 'ls-files', '-z').decode().split('\0') if rel}
            before = inventory(tracked)
            code, timed_out = run_command(gate['command'], cwd, gate.get('timeout', 600), log, stop=stop, state=ran)
            unchanged = inventory(tracked) == before and not git(cwd, 'diff', '--name-only', 'HEAD').strip()
            result = {'name': gate['name'], 'status': 'PASS' if code == 0 and unchanged else 'FAIL',
                      'returncode': code, 'timed_out': timed_out, 'log': str(log),
                      'detail': '' if unchanged else 'gate changed tracked input files'}
            if ran.get('stopped'):
                with lock:
                    why = ('%s failed first (release.fail_fast)' % failed[0]) if failed else 'the check was interrupted'
                result.update(status='STOPPED', detail='stopped: ' + why)
        except (ValueError, OSError, subprocess.SubprocessError) as exc:
            log.write_text('gate failed: ' + type(exc).__name__ + '\n')
            result = dict(name=gate['name'], status='FAIL', returncode=1, detail=type(exc).__name__, log=str(log))
        result.update(started=stamp(started), duration_s=round(time.monotonic() - clock, 1))
        progress.finish(gate['name'], result['status'], result['duration_s'])
        if result['status'] == 'FAIL':
            with lock:
                failed.append(gate['name'])
            if config.fail_fast:
                stop.set()
        return result

    for name in carried:
        progress.drop(name)
    order = gate_order(config.gates)
    if config.parallel <= 1:
        for index, gate in order:
            gates.append(run(index, gate))
        return
    pool = ThreadPoolExecutor(max_workers=config.parallel)
    try:
        for done in [pool.submit(run, index, gate) for index, gate in order]:
            gates.append(done.result())
    except BaseException:
        # Ctrl-C or a crash: interrupt the running gates and start no queued one
        stop.set()
        pool.shutdown(wait=True, cancel_futures=True)
        raise
    pool.shutdown(wait=True)


def first_failure(log):
    """The first line of a gate log that names a failed test (pytest's FAILED and ERROR lines), or ''."""
    try:
        for line in Path(log).read_text(errors='replace').splitlines():
            if line.startswith(('FAILED ', 'ERROR ')):
                return line[:300]
    except OSError:
        pass
    return ''


#: The preflight's own build step; config.py keeps this name from project gates, so the two never
#: share a receipt entry or a progress step.
PREFLIGHT_BUILD = 'preflight-build'


def plan(config, kind):
    """(steps, sequential steps) a progress file lists for a check or a preflight of config."""
    gates = [gate['name'] for _, gate in gate_order(config.gates)]
    first = list(MANDATORY) if kind == 'check' else [PREFLIGHT_BUILD]
    return first + gates, first


def preflight(root, config, source, progress=QUIET):
    """Run the project gates on the commit checked out at source (release/source.py) before anyone
    approves it: build its candidate, then the gates as a release check runs them, fail fast as
    configured. Like release build it runs only the project's own commands, with publication
    credentials removed; it needs no approval and reads no term policy, so the leak scan and the
    other built-in checks wait for the release check (op-056 t-612)."""
    root, source = Path(root).resolve(), Path(source).resolve()
    receipt = PreflightReceipt(git(source, 'rev-parse', 'HEAD').decode().strip(), [], 'FAIL')
    work = contained(root, '.alpaca/releases/' + receipt.attempt + '/preflight')
    work.mkdir(parents=True)
    seed = work / 'seed'
    start = time.time()
    progress.start(PREFLIGHT_BUILD)
    try:
        build = build_release(root, config, work.parent / 'tree', source)
        copy_files({rel: Path(build.tree) / rel for rel in build.inventory}, seed)
        init_repository(seed)
        receipt.gates.append(dict(name=PREFLIGHT_BUILD, status='PASS', returncode=0, detail=''))
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        receipt.gates.append(dict(name=PREFLIGHT_BUILD, status='FAIL', returncode=1,
                                  detail=str(exc) if type(exc) is ValueError else type(exc).__name__))
    receipt.gates[-1].update(started=stamp(start), duration_s=round(time.time() - start, 1))
    progress.finish(PREFLIGHT_BUILD, receipt.gates[-1]['status'], receipt.gates[-1]['duration_s'])
    try:
        if receipt.gates[-1]['status'] != 'PASS':
            return receipt
        run_gates(work, config, seed, source, receipt.gates, progress)
        for gate in receipt.gates:
            if gate['status'] == 'FAIL' and gate.get('log'):
                gate['first_failure'] = first_failure(gate['log'])
        receipt.status = 'PASS' if all(g['status'] == 'PASS' for g in receipt.gates) else 'FAIL'
        return receipt
    finally:
        tidy_preflight(work)


def tidy_preflight(work):
    """A preflight is meant to run often: keep its gate logs, which the receipt and the printed
    first failure point at, and remove its candidate tree, seeds and gate checkouts."""
    for path in [work.parent / 'tree', *work.iterdir()]:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path, ignore_errors=True)


def _donor_fits(donor, build, approved, policy_hash):
    """Whether an earlier check is on this build and holds sound built-in evidence: same tree, commit
    and release inputs, the whole approval reference and the leak policy, all three built-in checks
    passed, and no failed approval or final recheck entry."""
    names = [g.get('name') for g in donor.gates]
    builtin = {g.get('name'): g for g in donor.gates if g.get('name') in MANDATORY}
    return (donor.kind == 'check' and donor.status in ('PASS', 'FAIL') and bool(policy_hash)
            and all(donor.build.get(key) == getattr(build, key) for key in ('tree_hash', 'source_sha', 'source_hash'))
            and donor.approval == approved and donor.policy_hash == policy_hash
            and set(builtin) == set(MANDATORY) and len(names) == len(set(names))
            and all(g.get('status') == 'PASS' and g.get('returncode') == 0 for g in builtin.values())
            and 'approval' not in names and 'input-integrity' not in names)


def _same_build(check, build, approved, policy_hash):
    """Whether a recorded check is on this build, whatever else happened in it: same tree, commit and
    release inputs, the whole approval reference, and the same leak policy or none read yet."""
    return (check.kind == 'check' and check.approval == approved and check.policy_hash in ('', policy_hash)
            and all(check.build.get(key) == getattr(build, key) for key in ('tree_hash', 'source_sha', 'source_hash')))


def gate_history(root, build, approved, policy_hash, wanted, before=None):
    """What the record says about each project gate on this build: {gate name: {'data': event data of the
    check holding its latest real run, 'entry': that run, 'donor': whether that check may supply a
    pointer, 'failed': how many real runs FAILed}}, or None when the record cannot be read in full.

    Two sets are named here once (op-011 t-615). The witness set (every recorded check of the same
    build, `_same_build`, whatever its built-in results, gate definitions or interpreter) decides the
    latest run and the failed count, and errs toward inclusion: more witnesses only run more gates.
    The donor set (`_donor_fits` and the gate definitions in `wanted`) may supply the pointer, and errs
    toward exclusion. A run that really ran has no `carried` key and is not SKIPPED. A recorded check
    receipt that cannot be loaded hides what it holds, so the result is None. With `before`, the check
    whose attempt is `before` and every later check are left out."""
    root = Path(root).resolve()
    conn = db.connect_readonly(str(root))
    try:
        rows = [(r['ref'], json.loads(r['data'])) for r in conn.execute(
            "SELECT ref, data FROM events WHERE kind='release-check' AND actor='alpaca-release' ORDER BY id")]
    except (sqlite3.Error, ValueError):
        return None
    finally:
        conn.close()
    history = {}
    for ref, data in rows:
        if before is not None and ref == before:
            break
        try:
            check = load_receipt(root, data.get('receipt', ''), CheckReceipt)
            if not _same_build(check, build, approved, policy_hash):
                continue
            entries = {g['name']: g for g in check.gates if g['name'] not in MANDATORY}
            donor = (_donor_fits(check, build, approved, policy_hash)
                     and {name: g.get('definition') for name, g in entries.items()} == wanted)
            for name, entry in entries.items():
                if 'carried' in entry or entry.get('status') == 'SKIPPED':
                    continue
                slot = history.setdefault(name, {'failed': 0})
                slot.update(data=data, entry=entry, donor=donor, attempt=check.attempt)
                slot['failed'] += entry.get('status') == 'FAIL'
        except (ValueError, OSError, TypeError, KeyError, AttributeError):
            return None
    return history


def _passed(entry):
    return entry.get('status') == 'PASS' and entry.get('returncode') == 0 and not entry.get('timed_out')


def carry_plan(root, config, build, approved, policy_hash):
    """{gate name: result to record in its place} for the project gates that passed in earlier
    recorded checks of this same build (op-011 t-615).

    The latest real run of a gate on this build decides (`gate_history`), in whatever recorded check
    of the build it happened: a gate carries only when that run passed and sits in a check that may
    donate (`_donor_fits` the build, approval and leak policy, records for every configured project
    gate, and for no other, the definition that gate has now). A failed or stopped latest run, a gate
    that never ran and a carried entry carry nothing. The result counts that gate's failed runs. A
    recorded check receipt that cannot be loaded, or any other difference, runs every gate."""
    wanted = {gate['name']: definition(gate) for gate in config.gates}
    if not policy_hash or not wanted:
        return {}
    history = gate_history(root, build, approved, policy_hash, wanted)
    plan = {}
    for name, slot in (history or {}).items():
        entry = slot['entry']
        if slot['donor'] and _passed(entry):
            plan[name] = {'name': name, 'status': 'PASS', 'returncode': 0, 'timed_out': False,
                          'detail': 'carried from check ' + slot['attempt'], 'log': entry.get('log', ''),
                          'definition': entry['definition'],
                          'carried': {'receipt': slot['data']['receipt'], 'attempt': slot['attempt'],
                                      'sha256': slot['data']['sha256'], 'started': entry.get('started'),
                                      'failed_runs': slot['failed']}}
    return plan


def verify_carried(root, checks, gate):
    """Refuse a carried gate unless carry_plan would have chosen it: its pointer names a recorded check
    of the same build, approval and leak policy, other than this one, in which a gate of that name
    passed by running, and no later real run of the gate, up to this check's own record, failed or
    stopped; its failed_runs equals the recount."""
    def refuse(why):
        raise ValueError('carried gate %s: %s' % (gate.get('name'), why))

    pointer = gate['carried']
    if gate.get('name') in MANDATORY or not isinstance(pointer, dict) or not gate.get('definition'):
        refuse('not a project gate that records its definition')
    try:
        donor = load_receipt(root, pointer.get('receipt', ''), CheckReceipt)
        sha = hashlib.sha256(contained(root, pointer['receipt']).read_bytes()).hexdigest()
    except (ValueError, OSError, TypeError, KeyError, AttributeError):
        refuse('its receipt is not a recorded check receipt')
    if sha != pointer.get('sha256'):
        refuse('its receipt hash differs')
    if donor.attempt != pointer.get('attempt') or donor.attempt == checks.attempt:
        refuse('it does not name an earlier check')
    build = BuildReceipt(**checks.build)
    if not _donor_fits(donor, build, checks.approval, checks.policy_hash):
        refuse('its check is not on this build, approval and leak policy')
    entry = next((g for g in donor.gates if g.get('name') == gate['name']), {})
    if not _passed(entry) or 'carried' in entry or entry.get('definition') != gate['definition']:
        refuse('the gate did not pass by running there')
    wanted = {g['name']: g.get('definition') for g in checks.gates if g.get('name') not in MANDATORY}
    history = gate_history(root, build, checks.approval, checks.policy_hash, wanted, before=checks.attempt)
    if history is None:
        refuse('a recorded check receipt cannot be read')
    slot = history.get(gate['name'])
    if not slot or not slot['donor'] or slot['attempt'] != donor.attempt or slot['entry'] != entry:
        refuse('the gate ran again after that check, or that check may not donate')
    if pointer.get('failed_runs') != slot['failed']:
        refuse('its failed run count differs from the record')


def check_release(root, config, build, source=None, progress=QUIET, carry=True):
    """Run every gate on build. Source files come from source, a checkout of the build's commit
    (release/source.py), or the project root when None; record, term list and scratch stay in root.

    With carry (the default) a project gate that passed in an earlier recorded check of the same build
    is not run again but carried over, pointing at the check that ran it (carry_plan). The built-in
    checks and the final recheck always run. carry=False runs every gate."""
    root = Path(root).resolve()
    source = Path(source or root).resolve()
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
        approved = approval.require(root, config, build.source_sha, source=source)
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        add('approval', False, str(exc) if type(exc) is ValueError else type(exc).__name__)
        return CheckReceipt(asdict(build), gates, 'BLOCKED', attempt=attempt)

    try:
        validate_build(root, config, build, source)
    except (ValueError, OSError) as exc:
        add('input-integrity', False, str(exc))
        return CheckReceipt(asdict(build), gates, 'BLOCKED', attempt=attempt, approval=approved)

    def timed(start):
        gates[-1].update(started=stamp(start), duration_s=round(time.time() - start, 1))
        progress.finish(gates[-1]['name'], gates[-1]['status'], gates[-1]['duration_s'])

    start = time.time()
    progress.start('reproducibility')
    try:
        second = build_release(root, config, work.parent / 'rebuild/tree', source)
        add('reproducibility', second.tree_hash == build.tree_hash)
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        add('reproducibility', False, type(exc).__name__)
    timed(start)

    seed = work / 'seed'
    start = time.time()
    progress.start('leak-scan')
    try:
        copy_files({rel: Path(build.tree) / rel for rel in build.inventory}, seed)
        candidate = init_repository(seed)
        ignored = left_out(seed)
        cfg, terms, policy_hash = policy(root, source)
        report = barrier.scan(str(seed), [candidate], cfg=cfg, term_list=terms)
        reasons = list(report.reasons)
        if ignored:
            # The scan reads the commit; a member the candidate's .gitignore keeps out of it is unscanned.
            reasons.append('UNSCANNED-IGNORED-MEMBER')
        add('leak-scan', report.verdict == 0 and not ignored, ','.join(reasons))
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        ignored = []
        add('leak-scan', False, type(exc).__name__)
    timed(start)

    start = time.time()
    progress.start('fresh-clone')
    try:
        clone = work / 'fresh'
        git(work, 'clone', '-q', '--no-local', str(seed), str(clone))
        detail = ('kept out of the seed by the candidate\'s own ignore rules: '
                  + ', '.join(ignore_sources(seed, ignored[:5]))
                  + (' and %d more' % (len(ignored) - 5) if len(ignored) > 5 else '')) if ignored else ''
        add('fresh-clone', digest(tree_inventory(clone)) == build.tree_hash and not ignored, detail)
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        add('fresh-clone', False, type(exc).__name__)
    timed(start)

    reuse = {}
    if carry and policy_hash and all(g['status'] == 'PASS' for g in gates):
        try:
            reuse = carry_plan(root, config, build, approved, policy_hash)
        except (ValueError, OSError, sqlite3.Error):
            reuse = {}
    run_gates(work, config, seed, source, gates, progress, reuse)
    try:
        validate_build(root, config, build, source)
        if policy(root, source)[2] != policy_hash:
            raise ValueError('release policy changed during checks')
        approval.require(root, config, build.source_sha, approved, source=source)
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        add('input-integrity', False, str(exc) if type(exc) is ValueError else type(exc).__name__)
    status = 'PASS' if all(g['status'] == 'PASS' for g in gates) else 'FAIL'
    return CheckReceipt(asdict(build), gates, status, policy_hash=policy_hash, attempt=attempt, approval=approved)


def validate_checks(root, config, checks, source=None):
    expected = set(MANDATORY) | {gate['name'] for gate in config.gates}
    if checks.status != 'PASS' or len(checks.gates) != len(expected) or {g.get('name') for g in checks.gates} != expected:
        raise ValueError('release checks are failed or incomplete')
    if any(g.get('status') != 'PASS' or g.get('returncode') != 0 for g in checks.gates):
        raise ValueError('release checks did not all pass')
    # The approval is confirmed before the private term policy is read.
    if not checks.approval:
        raise ValueError('release checks carry no owner approval')
    approval.require(root, config, checks.build['source_sha'], checks.approval, source=source)
    validate_build(root, config, BuildReceipt(**checks.build), source)
    if policy(root, source)[2] != checks.policy_hash:
        raise ValueError('release policy changed since checks')
    for gate in checks.gates:
        if 'carried' in gate:
            verify_carried(root, checks, gate)
