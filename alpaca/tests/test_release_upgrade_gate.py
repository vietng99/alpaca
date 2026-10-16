"""The upgrade-previous release gate (op-061 t-645): setup/release_checks.py upgrade.

No test here uses the network, a bootstrap or the real install.sh: the gate logic is run with parsed
text, a local git repository and a fake command runner.
"""
import importlib.util
import json
import pathlib
import sqlite3
import subprocess
from types import SimpleNamespace

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]


@pytest.fixture(scope='module')
def rc():
    spec = importlib.util.spec_from_file_location('release_checks_t645', ROOT / 'setup/release_checks.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sha(n):
    return ('%x' % n).rjust(40, '0')


LS = '\n'.join('%s\t%s' % (sha(i), ref) for i, ref in enumerate([
    'refs/tags/v1.1.0', 'refs/tags/v1.9.9', 'refs/tags/v1.10.0', 'refs/tags/v1.10.0^{}',
    'refs/tags/v1.11.0-rc1', 'refs/tags/1.10.5', 'refs/tags/v1.12.0'], 1)) + '\n'


# ---- tag selection
def test_previous_tag_is_the_newest_strictly_below_the_candidate(rc):
    assert rc.previous_tag(LS, '1.11.0') == 'v1.10.0'
    assert rc.previous_tag(LS, '1.10.0') == 'v1.9.9'
    assert rc.previous_tag(LS, '1.12.1') == 'v1.12.0'


@pytest.mark.parametrize('version, override', [
    ('1.1.0', None),            # nothing below
    ('1.4.0.dev0', None),       # the candidate is not X.Y.Z
    ('1.11.0', 'v1.12.0'),      # above the candidate
    ('1.11.0', 'v1.2.3'),       # not on the remote
    ('1.11.0', 'main'),         # not a version tag
])
def test_previous_tag_fails_naming_its_step(rc, version, override):
    with pytest.raises(rc.GateError, match=r'^upgrade-previous: previous-tag failed: '):
        rc.previous_tag(LS, version, override)


def test_previous_tag_override_may_equal_the_candidate(rc):
    assert rc.previous_tag(LS, '1.10.0', 'v1.10.0') == 'v1.10.0'


def test_no_tags_at_all_fails(rc):
    for text in ('', 'garbage\n'):
        with pytest.raises(rc.GateError, match='previous-tag failed'):
            rc.previous_tag(text, '1.3.0')


def test_a_peeled_line_gives_the_commit_of_an_annotated_tag(rc):
    text = '%s\trefs/tags/v1.0.0\n%s\trefs/tags/v1.0.0^{}\n' % (sha(1), sha(2))
    assert rc.tag_commit(text, 'v1.0.0') == sha(2)
    assert rc.tag_commit(text, 'v9.9.9') == ''


# ---- doctor
DOCTOR_OK = ''.join('OK    %-12s fine\n' % name for name in ('record', 'manifest', 'upgrade', 'boot-block', 'hooks', 'project.yaml'))


def test_parse_doctor_keeps_long_names_whole(rc):
    word, levels, _ = rc.parse_doctor('WARN  autoresearch-skill .claude/x is not\n  continued text\n'
                                      'OK    record       19 events\nGATE alpaca-doctor: WARN\n')
    assert word == 'WARN' and levels == {'autoresearch-skill': 'warn', 'record': 'ok'}


@pytest.mark.parametrize('out', ['', 'Traceback (most recent call last):\n  File "x"\nImportError: y\n',
                                 'OK    record       5 events\n'])
def test_doctor_without_its_gate_line_fails(rc, out):
    with pytest.raises(rc.GateError, match=r'upgrade-previous: doctor-after failed'):
        rc.check_doctor('doctor-after', out, 1)


def test_doctor_with_an_error_line_fails(rc):
    out = DOCTOR_OK + 'ERROR chain        chain break at event 3\nGATE alpaca-doctor: FAIL\n'
    with pytest.raises(rc.GateError, match=r'doctor-after failed: .*chain is ERROR'):
        rc.check_doctor('doctor-after', out, 2)


def test_a_crashed_scan_reported_as_warn_fails(rc):
    out = DOCTOR_OK + 'WARN  op-index     op-index scan skipped: ImportError: cannot import name x\nGATE alpaca-doctor: WARN\n'
    with pytest.raises(rc.GateError, match=r'doctor-after failed: .*op-index'):
        rc.check_doctor('doctor-after', out, 1)


def test_doctor_warn_with_exit_one_passes_and_pass_needs_exit_zero(rc):
    assert rc.check_doctor('d', DOCTOR_OK + 'WARN  disk         low\nGATE alpaca-doctor: WARN\n', 1)['disk'] == 'warn'
    assert rc.check_doctor('d', DOCTOR_OK + 'GATE alpaca-doctor: PASS\n', 0)['record'] == 'ok'
    with pytest.raises(rc.GateError):
        rc.check_doctor('d', DOCTOR_OK + 'GATE alpaca-doctor: PASS\n', 1)


def test_environment_checks_may_warn_after_but_others_may_not(rc):
    before = {'uncommitted': 'ok', 'disk': 'ok', 'record': 'ok', 'hooks': 'ok'}
    assert rc.doctor_problems(before, dict(before, uncommitted='warn', disk='warn')) == []
    assert rc.doctor_problems(before, dict(before, hooks='warn'))
    assert rc.doctor_problems(before, dict(before, uncommitted='error'))
    # a check that was ok and is gone is a problem, unless RENAMED names an ok successor (empty today)
    assert rc.RENAMED == {}
    assert rc.doctor_problems({'old-name': 'ok'}, {'new-name': 'ok'}) == ['doctor check old-name was ok before the upgrade and is gone after']
    # a check that was not ok before may stay as it was
    assert rc.doctor_problems({'transcripts': 'warn', 'x': 'warn'}, {'transcripts': 'warn', 'x': 'warn'}) == []


def test_a_renamed_check_passes_only_through_the_rename_map(rc, monkeypatch):
    monkeypatch.setattr(rc, 'RENAMED', {'old-name': 'new-name'})
    assert rc.doctor_problems({'old-name': 'ok'}, {'new-name': 'ok'}) == []
    assert rc.doctor_problems({'old-name': 'ok'}, {'new-name': 'warn'})        # the successor must be ok
    assert rc.doctor_problems({'old-name': 'ok'}, {'other': 'ok'})


def test_a_vanished_ok_check_is_a_problem_even_when_nothing_replaces_it(rc):
    # alpaca/doctor.py CHECK_MODULES skips a module that fails to import: no new name, no crash text
    before = {'record': 'ok', 'proof-integrity': 'ok', 'collector': 'ok'}
    assert rc.doctor_problems(before, {'record': 'ok', 'collector': 'ok'}) == [
        'doctor check proof-integrity was ok before the upgrade and is gone after']
    # a host check may warn, but it may not vanish
    assert rc.doctor_problems({'disk': 'ok'}, {}) and rc.doctor_problems({'disk': 'ok'}, {'disk': 'warn'}) == []


def test_a_new_check_that_is_not_ok_is_a_problem(rc):
    assert rc.doctor_problems({'record': 'ok'}, {'record': 'ok', 'migration': 'ok'}) == []
    assert rc.doctor_problems({'record': 'ok'}, {'record': 'ok', 'migration': 'warn'}) == [
        'doctor check migration is new in this release and is warn after the upgrade']
    assert rc.doctor_problems({'record': 'ok'}, {'record': 'ok', 'transcripts': 'warn'}) == []


def test_doctor_changes_name_what_appeared_vanished_or_moved(rc):
    assert rc.doctor_changes({'a': 'ok', 'b': 'ok', 'c': 'ok'}, {'a': 'ok', 'b': 'warn', 'd': 'ok'}) == [
        'allowed: b ok -> warn', 'gone: c (was ok)', 'new: d ok']


@pytest.mark.parametrize('detail', [
    "checks not loaded: AttributeError: module 'alpaca.research.doctor' has no attribute 'checks'",
    'checks not loaded: ModuleNotFoundError: No module named x',
    'checks returned no finding',
    "malformed row: ('x', 'fatal', 'd')",
    'op-index scan skipped: ImportError: cannot import name x',
])
def test_every_doctor_row_that_says_a_scan_did_not_run_fails(rc, detail):
    out = DOCTOR_OK + 'WARN  research     %s\nGATE alpaca-doctor: WARN\n' % detail
    with pytest.raises(rc.GateError, match=r'doctor-after failed: .*scan that crashed'):
        rc.check_doctor('doctor-after', out, 1)


def test_a_name_printed_twice_keeps_its_worst_level(rc):
    out = DOCTOR_OK.replace('OK    hooks        fine\n', 'WARN  hooks        2 events missing\nOK    hooks        fine\n') + 'GATE alpaca-doctor: WARN\n'
    assert rc.check_doctor('doctor-after', out, 1)['hooks'] == 'warn'
    out = DOCTOR_OK.replace('OK    hooks        fine\n', 'OK    hooks        fine\nWARN  hooks        2 events missing\n') + 'GATE alpaca-doctor: WARN\n'
    assert rc.check_doctor('doctor-after', out, 1)['hooks'] == 'warn'


def test_core_checks_must_read_ok_after(rc):
    assert rc.core_problems({name: 'ok' for name in rc.CORE_CHECKS}) == []
    assert rc.core_problems({'record': 'ok'})
    assert rc.core_problems(dict({name: 'ok' for name in rc.CORE_CHECKS}, hooks='warn'))


# ---- verify, upgrade and plan output
def test_verify_must_pass_and_not_lose_events(rc):
    assert rc.check_verify('verify-after', 'GATE alpaca-verify: PASS (19 events)\n', 0, 19) == 19
    with pytest.raises(rc.GateError, match=r'upgrade-previous: verify-after failed: .*0 events.*19'):
        rc.check_verify('verify-after', 'GATE alpaca-verify: PASS (0 events)\n', 0, 19)
    for out, code in (('Traceback (most recent call last):\n', 1), ('GATE alpaca-verify: FAIL (chain break at event 3)\n', 1),
                      ('GATE alpaca-verify: PASS (3 events)\n', 1), ('', 0)):
        with pytest.raises(rc.GateError, match='verify-after failed'):
            rc.check_verify('verify-after', out, code, 3)


def test_upgrade_and_plan_output_are_read_with_the_exit_code(rc):
    rc.check_upgrade('upgrade-apply', 'GATE alpaca-upgrade: PASS (mechanism refreshed, memory untouched)\n', 0)
    for out, code in (('GATE alpaca-upgrade: FAIL (x)\n', 1), ('', 0), ('GATE alpaca-upgrade: BLOCKED (x)\n', 0),
                      ('GATE alpaca-upgrade: PASS (x)\n', 1)):
        with pytest.raises(rc.GateError, match='upgrade-apply failed'):
            rc.check_upgrade('upgrade-apply', out, code)
    assert rc.check_plan('upgrade-plan', 'GATE alpaca-upgrade-plan: PASS (refresh=57 merge=2 preserve=3 modified=6 remove=0 memory-untouched=4)\n', 0) == 57
    assert rc.check_plan('upgrade-plan', 'GATE alpaca-upgrade-plan: PASS\n  reason: refresh=57 merge=2 preserve=3 modified=4 remove=0 memory-untouched=11\n', 0) == 57
    with pytest.raises(rc.GateError, match='upgrade-plan failed'):
        rc.check_plan('upgrade-plan', 'GATE alpaca-upgrade-plan: PASS (refresh=0 merge=0 preserve=0 modified=0 remove=0 memory-untouched=0)\n', 0)


def test_task_status_reads_the_task_list(rc):
    assert rc.task_status('t-001  done     build       Release smoke\nt-002  open     -   x\n', 't-001') == 'done'
    assert rc.task_status('t-002  open     -   x\n', 't-001') is None


def test_ids_are_found_wherever_the_cli_prints_them(rc):
    assert rc.find_id('op', 'GATE alpaca-op: PASS\nop-007 opened\n') == 'op-007'
    assert rc.find_id('t', 't-012 added to op-007') == 't-012'
    with pytest.raises(rc.GateError):
        rc.find_id('t', 'nothing here\n')


# ---- the record
def row(i, h, kind='k'):
    return (i, 't', 's', 'a', kind, None, None, '{}', 'c%d' % i, 'p%d' % i, h)


def test_event_rows_must_be_an_exact_prefix(rc):
    before = [row(1, 'h1'), row(2, 'h2')]
    assert rc.event_problems(before, before + [row(3, 'h3')]) == []
    assert rc.event_problems(before, [row(1, 'h1'), row(2, 'hX')])
    assert rc.event_problems(before, [row(1, 'h1')])
    assert rc.event_problems(before, [row(1, 'h1'), row(9, 'hN'), row(2, 'h2')])
    assert rc.event_problems([], [row(1, 'h1')])          # nothing compared is not a pass
    changed = list(row(2, 'h2'))
    changed[7] = '{"edited": 1}'                          # any column counts, not only the hash
    assert rc.event_problems(before, [row(1, 'h1'), tuple(changed)])


def test_snapshot_reads_rows_that_are_still_in_the_wal(rc, tmp_path):
    (tmp_path / '.alpaca').mkdir()
    db = tmp_path / '.alpaca/alpaca.db'
    writer = sqlite3.connect(str(db))
    writer.execute('PRAGMA journal_mode=WAL')
    writer.execute('PRAGMA wal_autocheckpoint=0')
    writer.execute('CREATE TABLE events (id INTEGER, ts, session, actor, kind, op, ref, data, content_hash, prev_hash, hash, where_)')
    writer.execute("INSERT INTO events VALUES (1,'t','s','a','k',NULL,NULL,'{}','c','p','h','w')")
    writer.commit()
    assert (tmp_path / '.alpaca/alpaca.db-wal').stat().st_size > 0
    try:
        assert rc.read_events(tmp_path) == [(1, 't', 's', 'a', 'k', None, None, '{}', 'c', 'p', 'h')]
    finally:
        writer.close()


def test_changed_preserved_file_fails(rc, tmp_path):
    (tmp_path / 'project.yaml').write_text('a: 1\n')
    (tmp_path / '.alpaca/proofs/op-001').mkdir(parents=True)
    (tmp_path / '.alpaca/proofs/op-001/t-001.md').write_text('sealed\n')
    before = rc.preserved_digests(tmp_path)
    assert sorted(before) == ['.alpaca/proofs/op-001/t-001.md', 'project.yaml']
    rc.check_preserved(before, rc.preserved_digests(tmp_path))
    (tmp_path / 'project.yaml').write_text('a: 2\n')
    with pytest.raises(rc.GateError, match=r'preserved failed: .*project\.yaml changed'):
        rc.check_preserved(before, rc.preserved_digests(tmp_path))
    (tmp_path / '.alpaca/proofs/op-001/t-001.md').unlink()
    with pytest.raises(rc.GateError, match=r't-001\.md is gone'):
        rc.check_preserved(before, rc.preserved_digests(tmp_path))


def test_merged_files_keep_their_own_lines(rc):
    from alpaca import manifest
    block = manifest.BOOT_BEGIN + '\nharness text\n' + manifest.BOOT_END
    before = 'house rule one\n' + block + '\nhouse rule two\n'
    assert rc.merged_problems(before, 'house rule one\n' + manifest.BOOT_BEGIN + '\nnew text\n' + manifest.BOOT_END + '\nhouse rule two\n') == []
    assert rc.merged_problems(before, block + '\nhouse rule two\n') == ['house rule one']


def write_tree(base, files):
    for name, body in files.items():
        path = base / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)


def test_refreshed_bytes_must_equal_the_candidate(rc, tmp_path):
    cand, proj = tmp_path / 'cand', tmp_path / 'proj'
    manifest = '[mechanism]\nalpaca/\nproject.yaml\nCLAUDE.md\n[memory]\n.alpaca/\n'
    write_tree(cand, {'ALPACA-MANIFEST': manifest, 'alpaca/x.py': 'new', 'project.yaml': 'candidate', 'CLAUDE.md': 'candidate'})
    write_tree(proj, {'ALPACA-MANIFEST': manifest, 'alpaca/x.py': 'new', 'project.yaml': 'mine', 'CLAUDE.md': 'mine'})
    rc.refreshed_bytes(proj, cand)             # preserved and merged paths may differ
    (proj / 'alpaca/x.py').write_text('old')
    with pytest.raises(rc.GateError, match=r'upgrade-previous: refreshed-bytes failed: .*alpaca/x\.py'):
        rc.refreshed_bytes(proj, cand)


def test_provenance_refuses_a_project_not_made_by_the_previous_release(rc, tmp_path):
    prev, proj = tmp_path / 'prev', tmp_path / 'proj'
    for tree, version in ((prev, '1.2.3'), (proj, '1.3.0')):
        write_tree(tree, {'alpaca/__init__.py': 'VERSION = "%s"\n' % version, 'alpaca/a.py': 'a'})
    (prev / 'MANIFEST.json').write_text(json.dumps({'files': {}}))
    with pytest.raises(rc.GateError, match=r'upgrade-previous: provenance failed: .*1\.3\.0.*1\.2\.3'):
        rc.provenance(proj, prev, 'v1.2.3')
    write_tree(proj, {'alpaca/__init__.py': 'VERSION = "1.2.3"\n'})
    (prev / 'MANIFEST.json').write_text(json.dumps({'files': {'alpaca/a.py': rc.file_digest(prev / 'alpaca/a.py')}}))
    assert rc.provenance(proj, prev, 'v1.2.3') == '1.2.3'
    (proj / 'alpaca/a.py').write_text('edited')
    with pytest.raises(rc.GateError, match=r'provenance failed: .*alpaca/a\.py'):
        rc.provenance(proj, prev, 'v1.2.3')


def test_leftovers_name_the_journal_and_the_folder(rc, tmp_path):
    assert rc.leftovers(tmp_path) == []
    (tmp_path / '.alpaca/upgrade').mkdir(parents=True)
    assert rc.leftovers(tmp_path) == ['.alpaca/upgrade']
    (tmp_path / '.alpaca/upgrade/journal.json').write_text('{}')
    assert rc.leftovers(tmp_path) == ['.alpaca/upgrade/journal.json']


# ---- environment and install command
def test_scratch_env_drops_session_markers(rc, monkeypatch):
    for key in rc.SCRATCH_VARIABLES + ('PUBLIC_REPO_TOKEN',):
        monkeypatch.setenv(key, '/decoy')
    env = rc.scratch_env()
    assert not [key for key in rc.SCRATCH_VARIABLES + ('PUBLIC_REPO_TOKEN',) if key in env]
    git = rc.git_env()
    assert git['GIT_TERMINAL_PROMPT'] == '0' and git['GIT_CONFIG_GLOBAL'] == '/dev/null' and 'BatchMode=yes' in git['GIT_SSH_COMMAND']
    assert 'GIT_TERMINAL_PROMPT' not in env


def test_the_install_is_confined_to_two_scratch_folders(rc, tmp_path):
    command = rc.install_command(tmp_path / 'prev', tmp_path / 'prefix', tmp_path / 'bin')
    assert command[command.index('--prefix') + 1] == str(tmp_path / 'prefix')
    assert command[command.index('--bin-dir') + 1] == str(tmp_path / 'bin')
    assert command[command.index('--from') + 1] == str(tmp_path / 'prev')


def test_the_repository_comes_from_an_argument_the_environment_or_the_public_default(rc, monkeypatch):
    monkeypatch.delenv('ALPACA_PREVIOUS_RELEASE_REPO', raising=False)
    assert rc.repo_url() == 'https://github.com/vietng99/alpaca.git'
    monkeypatch.setenv('ALPACA_PREVIOUS_RELEASE_REPO', 'file:///x')
    assert rc.repo_url() == 'file:///x'
    assert rc.repo_url('file:///y') == 'file:///y'


# ---- the whole sequence with a fake runner
class Fake:
    """Answers every step with a passing output; `answers` replaces one by step name or prefix."""

    def __init__(self, proj):
        self.proj, self.calls = proj, []
        self.make_proof = True
        self.answers = {
            'previous-tag': ('%s\trefs/tags/v1.2.3\n' % sha(7), 0),
            'clone-previous': (sha(7) + '\n', 0),
            'install-previous': ('--from --bin-dir\n', 0),
            'lifecycle op new': ('op-001 opened\n', 0),
            'lifecycle task add': ('t-001 added\n', 0),
            'verify-before': ('GATE alpaca-verify: PASS (4 events)\n', 0),
            'verify-after': ('GATE alpaca-verify: PASS (5 events)\n', 0),
            'doctor-before': (DOCTOR_OK + 'GATE alpaca-doctor: PASS\n', 0),
            'doctor-after': (DOCTOR_OK + 'GATE alpaca-doctor: PASS\n', 0),
            'tasks-before': ('t-001  done  build  Upgrade check\n', 0),
            'tasks-after': ('t-001  done  build  Upgrade check\n', 0),
            'candidate-version': ('Alpaca 1.3.0\n', 0),
            'version-after': ('Alpaca 1.3.0\n', 0),
            'upgrade-plan': ('GATE alpaca-upgrade-plan: PASS\n  reason: refresh=5 merge=2 preserve=3 modified=0 remove=0 memory-untouched=4\n', 0),
            'upgrade-apply': ('GATE alpaca-upgrade: PASS (mechanism refreshed, memory untouched)\n', 0),
            'proof-check': ('GATE alpaca-proof-check: PASS (1 report(s), 2 hash(es) still match)\n', 0),
        }

    def __call__(self, step, argv, cwd, env, timeout):
        self.calls.append((step, argv))
        if step == 'lifecycle proof new' and self.make_proof:
            path = self.proj / '.alpaca/proofs/op-001/t-001.md'
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(''.join('## %s\n\nTODO(agent): x\n\n' % heading for heading in (
                'At a glance', 'What I did', 'How I did it', 'Where', 'Result', 'Deviations and issues', 'How to reproduce', 'Evidence')))
        return self.answers.get(step, ('', 0))


def sequence(rc, tmp_path, fake=None, **probes):
    ctx = SimpleNamespace(root=tmp_path / 'root', tmp=tmp_path, prev=tmp_path / 'previous', proj=tmp_path / 'project',
                          cand=tmp_path / 'candidate', prefix=tmp_path / 'prefix', bin_dir=tmp_path / 'bin',
                          repo='file:///none', previous=None, version='1.3.0', python='python')
    fake = fake or Fake(ctx.proj)
    rows = [row(1, 'h1', 'op-open'), row(2, 'h2', 'task-add'), row(3, 'h3', 'task-move'), row(4, 'h4', 'proof-report')]
    state = {'rows': rows, 'left': []}
    probe = dict(events=lambda proj: state['rows'], provenance=lambda *a: '1.2.3', refreshed=lambda *a: None,
                 tree_digest=lambda proj: 'd', leftovers=lambda proj: state['left'], copy_candidate=lambda *a: None,
                 file_digest=lambda path: 'f', preserved=lambda proj: {'project.yaml': 'p'}, read_text=lambda path: 'x\n',
                 same_file=lambda a, b: True, exists=lambda path: True)
    probe.update(probes)
    return ctx, fake, state, probe


def test_the_sequence_passes_when_every_step_passes(rc, tmp_path, capsys):
    ctx, fake, state, probe = sequence(rc, tmp_path)
    state['rows'] = state['rows']  # unchanged after the upgrade
    assert rc.run_sequence(ctx, fake, probe) == 'v1.2.3'
    out = capsys.readouterr().out
    for step in ('previous-tag', 'clone-previous', 'install-previous', 'provenance', 'lifecycle', 'snapshot', 'upgrade-plan',
                 'upgrade-apply', 'refreshed-bytes', 'import-all', 'version-after', 'doctor-after', 'events', 'proof-check', 'preserved'):
        assert 'upgrade-previous: %s: PASS' % step in out
    install = [argv for step, argv in fake.calls if step == 'install-previous'][-1]
    assert '--prefix' in install and '--bin-dir' in install
    assert [argv for step, argv in fake.calls if step == 'new-previous'][0][0].endswith('prefix/current/bin/alpaca')


@pytest.mark.parametrize('bad, answer', [
    ('doctor-after', DOCTOR_OK + 'ERROR chain        chain break at event 3\nGATE alpaca-doctor: FAIL\n'),
    ('doctor-after', 'Traceback (most recent call last):\nImportError: x\n'),
    ('verify-after', 'GATE alpaca-verify: FAIL (chain break at event 3)\n'),
    ('verify-after', 'GATE alpaca-verify: PASS (0 events)\n'),
])
def test_gate_fails_naming_the_step_when_doctor_or_verify_fails_after(rc, tmp_path, bad, answer):
    ctx, fake, state, probe = sequence(rc, tmp_path)
    fake.answers[bad] = (answer, 1 if bad == 'verify-after' or 'Traceback' in answer else 2)
    with pytest.raises(rc.GateError, match=r'^upgrade-previous: %s failed: ' % bad):
        rc.run_sequence(ctx, fake, probe)
    assert fake.calls[-1][0] == bad               # nothing ran after the failing step


def test_a_check_that_was_ok_and_is_not_fails_the_gate_but_a_host_check_may_warn(rc, tmp_path, capsys):
    ctx, fake, state, probe = sequence(rc, tmp_path)
    fake.answers['doctor-before'] = (DOCTOR_OK + 'OK    uncommitted  none\nGATE alpaca-doctor: PASS\n', 0)
    fake.answers['doctor-after'] = (DOCTOR_OK + 'WARN  uncommitted  1531 code files modified\nGATE alpaca-doctor: WARN\n', 1)
    rc.run_sequence(ctx, fake, probe)
    assert 'allowed: uncommitted ok -> warn' in capsys.readouterr().out
    ctx, fake, state, probe = sequence(rc, tmp_path / 'again')
    (tmp_path / 'again').mkdir()
    fake.answers['doctor-after'] = (DOCTOR_OK.replace('OK    hooks', 'WARN  hooks') + 'GATE alpaca-doctor: WARN\n', 1)
    with pytest.raises(rc.GateError, match=r'doctor-after failed: .*hooks'):
        rc.run_sequence(ctx, fake, probe)


def test_the_journal_is_checked_before_any_project_command_runs(rc, tmp_path):
    ctx, fake, state, probe = sequence(rc, tmp_path)
    original = fake.__call__

    def leave_journal(step, argv, cwd, env, timeout):
        if step == 'upgrade-apply':
            state['left'] = ['.alpaca/upgrade/journal.json']
        return original(step, argv, cwd, env, timeout)
    with pytest.raises(rc.GateError, match=r'^upgrade-previous: upgrade-journal failed: .*journal\.json'):
        rc.run_sequence(ctx, leave_journal, probe)
    assert fake.calls[-1][0] == 'upgrade-apply'


def test_a_plan_that_writes_fails(rc, tmp_path):
    ctx, fake, state, probe = sequence(rc, tmp_path)
    digests = iter(['a', 'b'])
    probe['tree_digest'] = lambda proj: next(digests)
    with pytest.raises(rc.GateError, match='upgrade-plan failed: the plan changed the project'):
        rc.run_sequence(ctx, fake, probe)


def test_the_record_must_read_back_unchanged(rc, tmp_path):
    ctx, fake, state, probe = sequence(rc, tmp_path)
    seen = []

    def events(proj):
        seen.append(1)
        return list(state['rows']) if len(seen) == 1 else [state['rows'][0], row(2, 'CHANGED')] + state['rows'][2:]
    probe['events'] = events
    with pytest.raises(rc.GateError, match=r'^upgrade-previous: events failed: '):
        rc.run_sequence(ctx, fake, probe)


def test_the_task_must_still_be_done_and_the_proof_must_still_check(rc, tmp_path):
    ctx, fake, state, probe = sequence(rc, tmp_path)
    fake.answers['tasks-after'] = ('t-001  open  build  Upgrade check\n', 0)
    with pytest.raises(rc.GateError, match='tasks failed'):
        rc.run_sequence(ctx, fake, probe)
    ctx, fake, state, probe = sequence(rc, tmp_path / 'p2')
    (tmp_path / 'p2').mkdir()
    fake.answers['proof-check'] = ('GATE alpaca-proof-check: FAIL (1 report(s), 1 problem(s))\n', 1)
    with pytest.raises(rc.GateError, match='proof-check failed'):
        rc.run_sequence(ctx, fake, probe)


def test_the_project_must_be_the_candidates_version(rc, tmp_path):
    ctx, fake, state, probe = sequence(rc, tmp_path)
    fake.answers['version-after'] = ('Alpaca 1.2.3\n', 0)
    with pytest.raises(rc.GateError, match=r'version-after failed'):
        rc.run_sequence(ctx, fake, probe)


def test_a_previous_release_without_install_sh_fails_naming_the_step(rc, tmp_path):
    ctx, fake, state, probe = sequence(rc, tmp_path)
    probe['exists'] = lambda path: pathlib.Path(path).name != 'install.sh'
    with pytest.raises(rc.GateError, match=r'^upgrade-previous: install-previous failed: v1\.2\.3 has no install\.sh'):
        rc.run_sequence(ctx, fake, probe)


def test_a_clone_at_another_commit_than_the_tag_fails(rc, tmp_path):
    ctx, fake, state, probe = sequence(rc, tmp_path)
    fake.answers['clone-previous'] = (sha(9) + '\n', 0)
    with pytest.raises(rc.GateError, match='clone-previous failed'):
        rc.run_sequence(ctx, fake, probe)


@pytest.mark.parametrize('answer', [('fatal: unable to access\n', 128), ('', 0), ('%s\trefs/tags/v1.3.0\n' % sha(1), 0),
                                    ('%s\trefs/tags/v1.4.0\n' % sha(1), 0)])
def test_no_network_or_no_tag_below_fails_naming_the_step(rc, tmp_path, answer):
    ctx, fake, state, probe = sequence(rc, tmp_path)
    fake.answers['previous-tag'] = answer
    with pytest.raises(rc.GateError, match=r'^upgrade-previous: previous-tag failed: '):
        rc.run_sequence(ctx, fake, probe)
    assert len(fake.calls) == 1


# ---- the real entry point against a local repository, no network
def local_repo(path, files=None, tag='v0.0.1'):
    path.mkdir()
    env = {'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_CONFIG_NOSYSTEM': '1', 'PATH': '/usr/bin:/bin:/usr/local/bin'}
    write_tree(path, files or {'README.md': 'x'})
    for args in (['init', '-q'], ['add', '-A'], ['-c', 'user.name=t', '-c', 'user.email=t@invalid', 'commit', '-qm', 'x'], ['tag', tag]):
        subprocess.run(['git', '-C', str(path)] + args, check=True, env=env, capture_output=True)


def test_an_unreachable_repository_fails_and_leaves_no_scratch(rc, tmp_path):
    with pytest.raises(rc.GateError, match=r'^upgrade-previous: previous-tag failed: '):
        rc.upgrade(tmp_path, repo='file://' + str(tmp_path / 'none.git'))
    assert list((tmp_path / '.alpaca/release-upgrade').iterdir()) == []


def test_a_release_without_install_sh_is_cloned_then_refused_and_cleaned_up(rc, tmp_path, capsys):
    local_repo(tmp_path / 'repo')
    with pytest.raises(rc.GateError, match=r'^upgrade-previous: install-previous failed: v0\.0\.1 has no install\.sh'):
        rc.upgrade(tmp_path, repo='file://' + str(tmp_path / 'repo'))
    assert 'clone-previous: PASS (v0.0.1)' in capsys.readouterr().out
    assert list((tmp_path / '.alpaca/release-upgrade').iterdir()) == []


def test_the_command_line_exits_nonzero_with_the_step_named(tmp_path):
    result = subprocess.run([str(ROOT / '.venv/bin/python') if (ROOT / '.venv/bin/python').exists() else 'python3',
                             str(ROOT / 'setup/release_checks.py'), 'upgrade', '--repo', 'file://' + str(tmp_path / 'none.git')],
                            capture_output=True, text=True, timeout=180)
    assert result.returncode != 0
    assert 'upgrade-previous: previous-tag failed:' in result.stderr


def test_a_record_without_the_op_task_and_proof_events_is_refused(rc, tmp_path):
    ctx, fake, state, probe = sequence(rc, tmp_path)
    state['rows'] = state['rows'][:3]            # no proof-report event
    fake.answers['verify-before'] = ('GATE alpaca-verify: PASS (3 events)\n', 0)
    with pytest.raises(rc.GateError, match=r'^upgrade-previous: snapshot failed: .*kinds'):
        rc.run_sequence(ctx, fake, probe)


# ---- a check that went inert or appeared after the upgrade (adversarial B1)
RESEARCH_OK = ''.join('OK    %-12s fine\n' % name for name in ('research-tools', 'research-guard', 'research-isolation', 'proof-integrity'))
GATE_WARN = 'GATE alpaca-doctor: WARN\n'


@pytest.mark.parametrize('after', [
    DOCTOR_OK + "WARN  research     checks not loaded: AttributeError: module 'alpaca.research.doctor' has no attribute 'checks'\n" + GATE_WARN,
    DOCTOR_OK + 'WARN  research     checks returned no finding\n' + GATE_WARN,
    DOCTOR_OK + RESEARCH_OK + 'WARN  migration    3 rows the upgrade did not carry over\n' + GATE_WARN,
    DOCTOR_OK + 'OK    research-tools fine\nOK    research-guard fine\nOK    research-isolation fine\n' + GATE_WARN,
], ids=['not-loaded', 'no-finding', 'new-check-warns', 'proof-integrity-gone'])
def test_the_gate_fails_doctor_after_when_a_check_goes_inert_vanishes_or_is_new_and_not_ok(rc, tmp_path, after):
    ctx, fake, state, probe = sequence(rc, tmp_path)
    fake.answers['doctor-before'] = (DOCTOR_OK + RESEARCH_OK + 'GATE alpaca-doctor: PASS\n', 0)
    fake.answers['doctor-after'] = (after, 1)
    with pytest.raises(rc.GateError, match=r'^upgrade-previous: doctor-after failed: '):
        rc.run_sequence(ctx, fake, probe)
    assert fake.calls[-1][0] == 'doctor-after'


def test_the_log_shows_a_new_check_and_a_host_check_that_moved(rc, tmp_path, capsys):
    ctx, fake, state, probe = sequence(rc, tmp_path)
    fake.answers['doctor-before'] = (DOCTOR_OK + 'OK    disk         fine\nGATE alpaca-doctor: PASS\n', 0)
    fake.answers['doctor-after'] = (DOCTOR_OK + 'WARN  disk         low\nOK    shim         fine\n' + GATE_WARN, 1)
    rc.run_sequence(ctx, fake, probe)
    out = capsys.readouterr().out
    assert 'upgrade-previous: doctor-after: allowed: disk ok -> warn' in out
    assert 'upgrade-previous: doctor-after: new: shim ok' in out


# ---- an exception outside a subprocess names the step (adversarial B2)
def test_a_proof_that_is_not_where_expected_fails_the_lifecycle_step(rc, tmp_path):
    ctx, fake, state, probe = sequence(rc, tmp_path)
    fake.make_proof = False
    ctx.proj.mkdir(parents=True)
    with pytest.raises(rc.GateError, match=r'^upgrade-previous: lifecycle failed: FileNotFoundError'):
        rc.run_sequence(ctx, fake, probe)


def test_a_merged_file_the_upgrade_removed_fails_the_preserved_step(rc, tmp_path):
    ctx, fake, state, probe = sequence(rc, tmp_path)
    probe['exists'] = lambda path: not (pathlib.Path(path).name == 'AGENTS.md' and state.get('upgraded'))
    original = fake.__call__

    def runner(step, argv, cwd, env, timeout):
        if step == 'upgrade-apply':
            state['upgraded'] = True
        return original(step, argv, cwd, env, timeout)
    with pytest.raises(rc.GateError, match=r'^upgrade-previous: preserved failed: AGENTS\.md is gone after the upgrade'):
        rc.run_sequence(ctx, runner, probe)


def test_a_record_that_cannot_be_read_after_fails_the_events_step(rc, tmp_path):
    ctx, fake, state, probe = sequence(rc, tmp_path)
    calls = []

    def events(proj):
        calls.append(1)
        if len(calls) > 1:
            raise sqlite3.OperationalError('unable to open database file')
        return state['rows']
    probe['events'] = events
    with pytest.raises(rc.GateError, match=r'^upgrade-previous: events failed: OperationalError: unable to open'):
        rc.run_sequence(ctx, fake, probe)


@pytest.mark.parametrize('name, step', [('copy_candidate', 'candidate-copy'), ('tree_digest', 'upgrade-plan'),
                                        ('refreshed', 'refreshed-bytes'), ('same_file', 'manifest'),
                                        ('file_digest', 'snapshot'), ('preserved', 'snapshot'), ('leftovers', 'snapshot'),
                                        ('provenance', 'provenance')])
def test_any_probe_that_raises_fails_the_step_it_belongs_to(rc, tmp_path, name, step):
    ctx, fake, state, probe = sequence(rc, tmp_path)

    def boom(*args):
        raise OSError('disk gone')
    probe[name] = boom
    with pytest.raises(rc.GateError, match=r'^upgrade-previous: %s failed: OSError: disk gone' % step):
        rc.run_sequence(ctx, fake, probe)


def test_code_between_two_steps_fails_the_step_that_was_running(rc, tmp_path):
    ctx, fake, state, probe = sequence(rc, tmp_path)
    state['rows'] = [row[:4] for row in state['rows']]          # a record with fewer columns than expected
    with pytest.raises(rc.GateError, match=r'^upgrade-previous: snapshot failed: IndexError'):
        rc.run_sequence(ctx, fake, probe)


def test_the_snapshot_of_an_older_record_without_a_column_fails_the_events_step(rc, tmp_path):
    (tmp_path / '.alpaca').mkdir()
    conn = sqlite3.connect(str(tmp_path / '.alpaca/alpaca.db'))
    conn.execute('CREATE TABLE events (id INTEGER, ts)')
    conn.commit()
    conn.close()
    with pytest.raises(rc.GateError, match=r'^upgrade-previous: events failed: the record cannot be read: OperationalError.*no such column'):
        rc.read_events(tmp_path)


def test_a_project_without_a_record_file_fails_the_events_step(rc, tmp_path):
    (tmp_path / '.alpaca').mkdir()
    with pytest.raises(rc.GateError, match=r'^upgrade-previous: events failed: the project has no record file'):
        rc.read_events(tmp_path)


@pytest.mark.parametrize('manifest', [None, 'not json', '[]', json.dumps({'files': ['alpaca/a.py']}), json.dumps({'files': {}}),
                                      json.dumps({'files': {'README.md': 'x'}})])
def test_a_tag_whose_manifest_cannot_prove_the_bytes_fails_provenance(rc, tmp_path, manifest):
    for tree in (tmp_path / 'prev', tmp_path / 'proj'):
        write_tree(tree, {'alpaca/__init__.py': 'VERSION = "1.2.3"\n', 'alpaca/a.py': 'a'})
    if manifest is not None:
        (tmp_path / 'prev/MANIFEST.json').write_text(manifest)
    with pytest.raises(rc.GateError, match=r'^upgrade-previous: provenance failed: '):
        rc.provenance(tmp_path / 'proj', tmp_path / 'prev', 'v1.2.3')


def test_a_command_that_cannot_start_fails_the_step_that_ran_it(rc, tmp_path, capsys):
    out, code = rc.run_step('new-previous', [str(tmp_path / 'no/bin/alpaca'), 'doctor'], tmp_path, {}, 10)
    assert code == 127 and 'cannot start' in out and 'FileNotFoundError' in out
    out, code = rc.run_step('x', ['true'], tmp_path / 'no-such-folder', {}, 10)
    assert code == 127
    ctx, fake, state, probe = sequence(rc, tmp_path)
    fake.answers['version-after'] = (out, code)
    with pytest.raises(rc.GateError, match=r'^upgrade-previous: version-after failed: exit 127: cannot start'):
        rc.run_sequence(ctx, fake, probe)


def test_the_real_runner_returns_output_exit_code_and_timeout(rc, tmp_path):
    assert rc.run_step('x', ['sh', '-c', 'echo hi; exit 3'], tmp_path, {}, 10) == ('hi\n', 3)
    out, code = rc.run_step('x', ['sleep', '30'], tmp_path, {'PATH': '/usr/bin:/bin'}, 1)
    assert code == 124 and 'timed out after 1 s' in out


def test_an_unusable_scratch_folder_fails_the_scratch_step(rc, tmp_path):
    (tmp_path / '.alpaca').write_text('a file where the folder should be')
    with pytest.raises(rc.GateError, match=r'^upgrade-previous: scratch failed: cannot use '):
        rc.upgrade(tmp_path, repo='file:///none')


def test_the_command_line_names_a_failure_that_is_not_a_step(rc, monkeypatch, capsys):
    monkeypatch.setattr(rc, 'upgrade', lambda *a, **k: 1 / 0)
    assert rc.main(['upgrade']) == 1
    assert 'upgrade-previous: setup failed: ZeroDivisionError: division by zero' in capsys.readouterr().err
    monkeypatch.setattr(rc, 'upgrade', lambda *a, **k: (_ for _ in ()).throw(rc.fail('previous-tag', 'x')))
    assert rc.main(['upgrade']) == 1
    assert 'upgrade-previous: previous-tag failed: x' in capsys.readouterr().err
