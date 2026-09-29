"""The installed wiki surface uses real configuration, capture and answer doors."""
import json
from pathlib import Path

import pytest

from alpaca import cli, db
from alpaca.wiki.config import Config
from alpaca.wiki.ingest.absorb import Absorber


def test_project_configuration_and_vault_precedence(project):
    root = Path(project)
    (root / 'project.yaml').write_text('wiki_providers:\n  reranker: lexical\n  meta:\n    retrieval_profile: full\n')
    cfg = Config.for_project(root)
    assert cfg.reranker == 'lexical'
    assert cfg.meta['retrieval_profile'] == 'full'
    cfg.vault_dir.mkdir(parents=True)
    (cfg.vault_dir / 'rune.toml').write_text('reranker = "identity"\n[meta]\nembed_dim = 32\n')
    cfg = Config.for_project(root)
    assert cfg.reranker == 'identity'
    assert cfg.meta['embed_dim'] == '32'
    assert cfg.meta['retrieval_profile'] == 'full'


@pytest.mark.parametrize('setting', ['embedder: invented', 'mispelled: value', 'meta: nope'])
def test_invalid_project_settings_fail_visibly(project, setting):
    Path(project, 'project.yaml').write_text('wiki_providers:\n  ' + setting + '\n')
    with pytest.raises((ValueError, TypeError)):
        Config.for_project(project)


def test_cli_ingest_query_and_status(project, capsys):
    page = Path(project, 'design.md')
    page.write_text('The project uses SQLite because deployments must run offline.\n')
    assert cli.main(['wiki', 'ingest', str(page), '--doc-id', 'wiki/design.md']) == 0
    ingest = json.loads(capsys.readouterr().out)
    assert ingest['doc_id'] == 'wiki/design.md'
    assert cli.main(['wiki', 'query', 'SQLite offline']) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['verdict'] == 'grounded'
    assert result['sources'][0]['doc_id'] == 'wiki/design.md'
    assert result['sources'][0]['source_block_id']
    assert result['trust'] == 'evidence-only'
    assert cli.main(['wiki', 'status']) == 0
    status = json.loads(capsys.readouterr().out)
    assert status['corpus']['documents'] == 1
    assert status['operational_pages'] == 0
    assert status['providers']['embedder'] == 'deterministic'
    assert status['learning']['dream_armed'] is False


def test_status_reports_capture_gap_without_creating_vault(project):
    from alpaca.wiki import service
    conn = db.connect(project)
    db.append_event(conn, session='s', actor='test', kind='result', data={'body': 'uncaptured'})
    conn.close()
    info = service.status(project)
    assert info['capture']['missing_events'] == 1
    assert not Path(project, '.alpaca/wiki/rune.db').exists()


def test_ingest_rejects_paths_outside_document_namespace(project):
    from alpaca.wiki import service
    page = Path(project, 'design.md')
    page.write_text('Safe content')
    for doc_id in ('../outside.md', '/tmp/outside.md', 'raw/../escape.md', 'wiki/../../escape.md'):
        with pytest.raises(ValueError):
            service.ingest(project, page, doc_id=doc_id)


def test_context_is_bounded_cited_evidence(project):
    from alpaca.wiki import service
    page = Path(project, 'design.md')
    page.write_text('The project uses SQLite because deployments must run offline.\n')
    service.ingest(project, page, doc_id='wiki/design.md')
    text = service.context(project, 'SQLite offline', max_chars=1500)
    assert 'evidence-only' in text
    assert 'wiki/design.md' in text
    assert 'SQLite' in text
    assert len(text) <= 1500


def test_session_start_offers_wiki_context_on_resume(project):
    from alpaca.hooks import session_start
    result = session_start.handle({'cwd': project, 'session_id': 'resume-wiki', 'operator': 'codex'})
    assert 'wiki context' in result['context']
    assert 'wiki status' in result['context']


def test_explicit_resume_question_retrieves_real_context(project):
    from alpaca import operator
    from alpaca.wiki import service
    page = Path(project, 'design.md')
    page.write_text('The project uses SQLite because deployments must run offline.\n')
    service.ingest(project, page, doc_id='wiki/design.md')
    result = operator.run('start', project, 'wiki-reader', operator='codex', wiki_question='SQLite offline')
    assert 'Verdict: grounded' in result['context']
    assert 'wiki/design.md' in result['context']


def test_invalid_config_is_visible_in_cli(project, capsys):
    Path(project, 'project.yaml').write_text('wiki_providers:\n  embedder: nonexistent\n')
    assert cli.main(['wiki', 'query', 'anything']) != 0
    assert 'unknown embedder' in capsys.readouterr().err


def test_ingest_refuses_symlink_destination(project, tmp_path):
    from alpaca.wiki import service
    vault = Path(project, '.alpaca/wiki')
    vault.mkdir(parents=True)
    outside = tmp_path / 'outside'
    outside.mkdir()
    (vault / 'wiki').symlink_to(outside, target_is_directory=True)
    source = Path(project, 'source.md')
    source.write_text('safe evidence')
    with pytest.raises((ValueError, OSError)):
        service.ingest(project, source, doc_id='wiki/source.md')
    assert not list(outside.iterdir())


def test_status_counts_answers_and_vector_cache(project):
    from alpaca.wiki import service
    Path(project, 'project.yaml').write_text('wiki_providers:\n  meta:\n    retrieval_profile: full\n')
    page = Path(project, 'design.md')
    page.write_text('The project uses SQLite because deployments must run offline.\n')
    service.ingest(project, page, doc_id='wiki/design.md')
    service.query(project, 'SQLite offline')
    report = service.status(project)
    assert report['corpus']['answers'] == 1
    assert report['vectors']['blocks'] >= 1


def test_project_vault_symlink_is_not_followed(project, tmp_path):
    outside = tmp_path / 'other-vault'
    outside.mkdir()
    Path(project, '.alpaca/wiki').symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match='symlink'):
        Config.for_project(project)


def test_context_surfaces_uncaptured_events(project):
    from alpaca.wiki import service
    conn = db.connect(project)
    db.append_event(conn, session='s', actor='test', kind='result', data={'body': 'not captured'})
    conn.close()
    text = service.context(project, 'SQLite offline')
    assert '1 event(s) not captured' in text
    assert '--refresh' in text


def test_ingest_cannot_publish_an_existing_private_source(project):
    from alpaca.wiki import service
    source = Path(project, 'private-source.md')
    source.write_text('The project has a private recovery detail.\n')
    service.ingest(project, source, doc_id='wiki/private/secret.md')
    private = Path(project, '.alpaca/wiki/wiki/private/secret.md')
    with pytest.raises(ValueError, match='private'):
        service.ingest(project, private, doc_id='wiki/shared.md')
    cfg = Config.for_project(project)
    from alpaca.wiki.store.db import open_db_readonly
    conn = open_db_readonly(cfg)
    try:
        assert conn.execute("SELECT count(*) FROM docs WHERE doc_id='wiki/shared.md'").fetchone()[0] == 0
    finally:
        conn.close()


def test_status_and_context_report_missing_transcript(project):
    from alpaca import observability
    from alpaca.wiki import service
    observability.expect_source(project, 'missing-child', 'claude', locator=str(Path(project, 'gone.jsonl')))
    report = service.status(project)
    assert report['capture']['missing_transcripts'] == 1
    assert report['capture']['registered_transcripts'] == 1
    assert '1 registered transcript(s) unavailable' in service.context(project, 'anything')


def test_recovery_missing_source_never_reports_cli_pass(project, capsys):
    from alpaca import observability
    observability.expect_source(project, 'missing-child', 'claude', locator=str(Path(project, 'gone.jsonl')))
    assert cli.main(['wiki', 'recover']) != 0
    report = json.loads(capsys.readouterr().out)
    assert report['status'] == 'incomplete'
    assert cli.main(['wiki', 'query', 'anything', '--refresh']) != 0
