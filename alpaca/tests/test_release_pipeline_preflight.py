"""A preflight runs the project gates on a commit before approval and is never release evidence."""
import json
from pathlib import Path
import sys

import pytest

from alpaca.tests.test_release_pipeline_build import source, api
from alpaca.tests.test_release_pipeline_checks import project, set_gates, gate
from alpaca.tests.test_release_pipeline_source import commit_fixture, git

PYTEST_FAILURE = ('print("tests/test_app.py::test_ok PASSED"); print("FAILED tests/test_app.py::test_broken - assert 1 == 2"); '
                  'print("FAILED tests/test_app.py::test_other"); raise SystemExit(1)')


def run(project, monkeypatch, capsys, *args):
    from alpaca import cli
    monkeypatch.setattr(cli, '_root', lambda: str(project))
    code = cli.main(['release', 'preflight', *args])
    return code, json.loads(capsys.readouterr().out)


def test_preflight_passes_without_approval_or_term_list(project, monkeypatch, capsys):
    (project / '.alpaca/terms.txt').unlink()
    sha = commit_fixture(project)
    code, out = run(project, monkeypatch, capsys)
    assert code == 0 and out['status'] == 'PASS' and out['source_sha'] == sha
    receipt = json.loads(open(out['receipt']).read())
    assert receipt['kind'] == 'preflight'
    assert [g['name'] for g in receipt['gates']] == ['preflight-build', 'product-test']
    assert all(g['duration_s'] >= 0 for g in receipt['gates'])


def test_preflight_names_the_first_failing_test_and_skips_the_rest(project, monkeypatch, capsys):
    set_gates(project, [gate('slow-suite', timeout=3600),
                        {'name': 'unit', 'command': [sys.executable, '-c', PYTEST_FAILURE], 'timeout': 60}])
    commit_fixture(project)
    code, out = run(project, monkeypatch, capsys)
    assert code == 1 and out['status'] == 'FAIL'
    assert out['failed_gate'] == 'unit'
    assert out['first_failure'] == 'FAILED tests/test_app.py::test_broken - assert 1 == 2'
    assert out['log'].endswith('unit.log')
    receipt = json.loads(open(out['receipt']).read())
    assert next(g for g in receipt['gates'] if g['name'] == 'slow-suite')['status'] == 'SKIPPED'


def test_preflight_checks_the_commit_not_the_dirty_root(project, monkeypatch, capsys):
    set_gates(project, [{'name': 'content', 'timeout': 60, 'command': [
        sys.executable, '-c', 'import pathlib; assert pathlib.Path("src/app.txt").read_text() == "product\\n"']}])
    commit_fixture(project)
    (project / 'src/app.txt').write_text('uncommitted\n')
    code, out = run(project, monkeypatch, capsys)
    assert code == 0, out


def test_preflight_takes_an_older_commit(project, monkeypatch, capsys):
    first = commit_fixture(project)
    (project / 'src/app.txt').write_text('newer\n')
    commit_fixture(project)
    code, out = run(project, monkeypatch, capsys, '--commit', first)
    assert code == 0 and out['source_sha'] == first


def test_a_preflight_receipt_is_not_a_check_receipt(project, monkeypatch, capsys):
    commit_fixture(project)
    _, out = run(project, monkeypatch, capsys)
    with pytest.raises(ValueError, match='kind'):
        api('receipts').load_receipt(project, out['receipt'], api('receipts').CheckReceipt)


def test_preflight_keeps_only_its_logs(project, monkeypatch, capsys):
    commit_fixture(project)
    _, out = run(project, monkeypatch, capsys)
    attempt = Path(open(out['receipt']).name).name.split('-')[0]
    folder = project / '.alpaca/releases' / attempt
    assert not (folder / 'tree').exists()
    assert sorted(p.name for p in (folder / 'preflight').iterdir()) == ['product-test.log']
