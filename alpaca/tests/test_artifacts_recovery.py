import errno
import hashlib
import json
from pathlib import Path

import pytest


def api():
    from alpaca import artifacts
    return artifacts


ARCHIVES = '.alpaca/domain/archive'


@pytest.fixture
def archives(monkeypatch):
    """A domain profile whose run archives sit under ARCHIVES (alpaca/profile.py `paths`)."""
    from alpaca import profile

    class Archives(profile.Profile):
        def paths(self):
            return {'archives': [ARCHIVES + '/*/manifest.json']}
    monkeypatch.setattr(profile, 'load', lambda root: Archives())


def test_retained_bytes_survive_source_overwrite_and_deletion(tmp_path):
    a = api()
    src = tmp_path / 'flow/out/result.gds'
    src.parent.mkdir(parents=True)
    src.write_bytes(b'approved baseline')
    row = a.capture(tmp_path, src, pin='acceptance baseline')
    src.write_bytes(b'next run')
    assert row['availability'] == 'retained'
    assert (tmp_path / row['object_path']).read_bytes() == b'approved baseline'
    src.unlink()
    assert a.inventory(tmp_path)[0]['availability'] == 'retained'
    assert a.retention_dry_run(tmp_path, budget_bytes=0)['candidates'] == []


def test_budgets_and_failed_publication_never_claim_retained(tmp_path, monkeypatch):
    a = api()
    src = tmp_path / 'result'; src.write_bytes(b'123456')
    assert a.capture(tmp_path, src, max_object_bytes=3)['availability'] == 'indexed-only'
    assert a.capture(tmp_path, src, max_total_bytes=3)['availability'] == 'indexed-only'
    original = a.os.replace
    def fail_object(src, dst):
        if '/objects/' in str(dst):
            raise OSError(errno.ENOSPC, 'injected full disk')
        return original(src, dst)
    monkeypatch.setattr(a.os, 'replace', fail_object)
    row = a.capture(tmp_path, src)
    assert row['availability'] == 'indexed-only'
    assert 'No space' in row['reason'] or 'full disk' in row['reason']
    assert not list((tmp_path / '.alpaca/artifacts/objects').rglob('*.tmp'))


def test_historical_recovery_matches_hash_and_rejects_external_rtl(tmp_path, archives):
    a = api()
    root = tmp_path / 'project'; root.mkdir()
    outside = tmp_path / 'restricted.sv'; outside.write_text('restricted')
    row = a.capture(root, outside)
    assert row['availability'] == 'unavailable'
    assert not row.get('object_path')
    old = hashlib.sha256(b'old').hexdigest()
    src = root / 'result'; src.write_bytes(b'new')
    base = root / ARCHIVES / ('a' * 32); base.mkdir(parents=True)
    (base / 'manifest.json').write_text(json.dumps({'receipt_id': 'a'*32, 'files': [], 'results_index': [
        {'path': 'results/old.gds', 'source': str(src), 'sha256': old, 'bytes': 3}]}))
    a.catalog_archives(root)
    rows = [r for r in a.inventory(root) if r.get('receipt_id')]
    assert rows[0]['availability'] == 'changed'
    assert rows[0]['sha256'] == old
    src.write_bytes(b'old')
    a.catalog_archives(root)
    assert any(r['availability'] == 'retained' and r['sha256'] == old for r in a.inventory(root))


def test_retention_dry_run_selects_only_unpinned_objects_without_mutation(tmp_path):
    a = api()
    one = tmp_path / 'one'; one.write_bytes(b'one')
    two = tmp_path / 'two'; two.write_bytes(b'two')
    pinned = a.capture(tmp_path, one, pin='release')
    loose = a.capture(tmp_path, two)
    before = {str(p): p.read_bytes() for p in (tmp_path / '.alpaca/artifacts').rglob('*') if p.is_file()}
    plan = a.retention_dry_run(tmp_path, budget_bytes=0)
    assert [r['sha256'] for r in plan['candidates']] == [loose['sha256']]
    assert pinned['sha256'] not in [r['sha256'] for r in plan['candidates']]
    assert before == {str(p): p.read_bytes() for p in (tmp_path / '.alpaca/artifacts').rglob('*') if p.is_file()}


def test_historical_unhashed_archive_entry_stays_unknown(tmp_path, archives):
    a = api()
    base = tmp_path / ARCHIVES / ('b' * 32); base.mkdir(parents=True)
    source = tmp_path / 'large.log'; source.write_text('current bytes cannot establish old identity')
    (base / 'manifest.json').write_text(json.dumps({'receipt_id': 'b'*32, 'indexed': [
        {'path': 'large.log', 'source': str(source), 'sha256': None, 'bytes': 999, 'reason': 'over cap, not hashed'}]}))
    result = a.catalog_archives(tmp_path)
    assert result['cataloged'] == 1
    row = a.inventory(tmp_path)[0]
    assert row['sha256'] is None
    assert row['availability'] == 'indexed-only'
    assert row['bytes'] == 999


def test_sealed_proof_reference_is_conservatively_pinned(tmp_path):
    a = api(); source = tmp_path / 'value'; source.write_bytes(b'proof evidence')
    row = a.capture(tmp_path, source)
    seal = tmp_path / '.alpaca/proofs/task.md.seal.json'; seal.parent.mkdir()
    seal.write_text(json.dumps({'evidence': [{'sha256': row['sha256']}]}))
    assert a.retention_dry_run(tmp_path)['candidates'] == []


def test_recovered_existing_object_keeps_original_size_when_source_changes(tmp_path):
    a = api(); source = tmp_path / 'result'; source.write_bytes(b'old')
    first = a.capture(tmp_path, source)
    source.write_bytes(b'changed result with a different size')
    recovered = a.capture(tmp_path, source, expected_sha256=first['sha256'])
    assert recovered['availability'] == 'retained'
    assert recovered['bytes'] == 3
    source.unlink()
    a.capture(tmp_path, source, expected_sha256=first['sha256'])
    assert all(row['bytes'] == 3 for row in a.inventory(tmp_path))


def test_capture_returns_retained_object_when_original_source_is_gone(tmp_path):
    a = api(); source = tmp_path / 'result'; source.write_bytes(b'baseline')
    first = a.capture(tmp_path, source)
    source.unlink()
    recovered = a.capture(tmp_path, source, expected_sha256=first['sha256'])
    assert recovered['availability'] == 'retained'
    assert recovered['source_availability'] == 'missing'
    assert recovered['bytes'] == 8
    assert (tmp_path / recovered['object_path']).read_bytes() == b'baseline'


@pytest.mark.parametrize('subdir', ['objects', 'manifests', 'pins'])
def test_artifact_storage_symlink_cannot_redirect_writes_outside_project(tmp_path, subdir):
    a = api(); root = tmp_path / 'project'; root.mkdir()
    outside = tmp_path / 'outside'; outside.mkdir()
    store = root / '.alpaca/artifacts'; store.mkdir(parents=True)
    (store / subdir).symlink_to(outside, target_is_directory=True)
    source = root / 'result'; source.write_bytes(b'approved result')
    try:
        result = a.capture(root, source, pin='acceptance baseline')
        assert result['availability'] != 'retained'
    except ValueError:
        pass
    assert list(outside.iterdir()) == []


def test_without_a_profile_no_archive_is_cataloged(tmp_path):
    a = api()
    base = tmp_path / ARCHIVES / ('c' * 32); base.mkdir(parents=True)
    (base / 'manifest.json').write_text(json.dumps({'receipt_id': 'c' * 32, 'indexed': [
        {'path': 'x.log', 'source': str(tmp_path / 'x.log'), 'sha256': None, 'bytes': 1}]}))
    assert a.catalog_archives(tmp_path)['cataloged'] == 0
    assert a.inventory(tmp_path) == []
