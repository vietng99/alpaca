"""Tagging a merged public release and creating its release page, against a fake GitHub API."""
import base64
from pathlib import Path

import pytest

from alpaca.tests.test_release_pipeline_build import source, api
from alpaca.tests.test_release_pipeline_checks import project

HEAD = 'a' * 40
MERGE = 'b' * 40
NOTES = 'Alpaca 1.2.3 adds the release page step.\n\n## New\n\n- Tags after the merge.\n'


class FakeGitHub:
    def __init__(self, merged=True, head=HEAD, version='1.2.3', tag=None, release=None):
        self.pull = {'merged': merged, 'merge_commit_sha': MERGE if merged else None,
                     'head': {'sha': head}, 'base': {'ref': 'main'}}
        self.init = None if version is None else 'VERSION = "%s"\n' % version
        self.tag, self.release, self.posts = tag, release, []

    def __call__(self, method, path, payload=None):
        if method == 'POST':
            self.posts.append((path, payload))
            return {'html_url': 'https://github.com/sample/project/releases/tag/' + payload['tag_name']}
        if path == '/repos/sample/project/pulls/7':
            return self.pull
        if path.startswith('/repos/sample/project/contents/alpaca/__init__.py?ref='):
            return self.init and {'content': base64.b64encode(self.init.encode()).decode()}
        if path.startswith('/repos/sample/project/git/ref/tags/'):
            return self.tag
        if path.startswith('/repos/sample/project/releases/tags/'):
            return self.release
        raise AssertionError('unexpected call ' + path)


def published(**kwargs):
    receipt = api('receipts').PublishReceipt({'attempt': 'c' * 32}, 'PASS', commit=HEAD,
                                              pr_url='https://github.com/sample/project/pull/7')
    for key, value in kwargs.items():
        setattr(receipt, key, value)
    return receipt


def run(project, fake, notes=NOTES, receipt=None, **kwargs):
    path = project / 'notes.md'
    path.write_text(notes)
    return api('tag').tag_release(project, api('config').load_config(project), receipt or published(),
                                  '1.2.3', path, api=fake, **kwargs)


def test_dry_run_plans_the_tag_and_makes_no_remote_change(project):
    fake = FakeGitHub()
    result = run(project, fake)
    assert result.status == 'DRY_RUN', result.error
    assert result.plan['tag'] == 'v1.2.3' and result.plan['target'] == MERGE and result.plan['latest']
    assert result.plan['title'] == 'Alpaca 1.2.3'
    assert Path(result.notes).read_text() == NOTES
    assert fake.posts == []


def test_execute_creates_the_release_at_the_merge_commit(project):
    plan = run(project, FakeGitHub()).plan
    fake = FakeGitHub()
    result = run(project, fake, dry_run=False, expect=plan)
    assert result.status == 'PASS' and result.release_url.endswith('/v1.2.3')
    [(path, payload)] = fake.posts
    assert path == '/repos/sample/project/releases'
    assert payload['tag_name'] == 'v1.2.3' and payload['target_commitish'] == MERGE
    assert payload['body'] == NOTES and payload['make_latest'] == 'true'


def test_execute_needs_the_reviewed_plan(project):
    plan = run(project, FakeGitHub()).plan
    fake = FakeGitHub()
    result = run(project, fake, notes=NOTES + 'Changed after review.\n', dry_run=False, expect=plan)
    assert result.status == 'BLOCKED' and 'reviewed dry run' in result.error
    assert run(project, fake, dry_run=False).status == 'BLOCKED'
    assert fake.posts == []


@pytest.mark.parametrize('fake, words', [
    (FakeGitHub(merged=False), 'not merged'),
    (FakeGitHub(head='d' * 40), 'not the one'),
    (FakeGitHub(version='1.2.2'), 'declares VERSION 1.2.2'),
    (FakeGitHub(tag={'object': {'sha': 'e' * 40}}), 'another commit'),
])
def test_blocks_before_any_write(project, fake, words):
    result = run(project, fake, dry_run=False, expect={})
    assert result.status == 'BLOCKED' and words in result.error
    assert fake.posts == []


def test_notes_that_fail_the_leak_policy_block(project):
    fake = FakeGitHub()
    result = run(project, fake, notes=NOTES + 'SecretCanaryValue\n')
    assert result.status == 'BLOCKED' and 'leak policy' in result.error
    assert not result.notes


def test_only_an_executed_publish_can_be_tagged(project):
    for receipt in (published(status='DRY_RUN'), published(pr_url='https://github.com/other/repo/pull/7')):
        assert run(project, FakeGitHub(), receipt=receipt).status == 'BLOCKED'


def test_existing_release_on_the_same_commit_passes_without_writing(project):
    fake = FakeGitHub(tag={'object': {'sha': MERGE}},
                      release={'html_url': 'https://github.com/sample/project/releases/tag/v1.2.3'})
    result = run(project, fake, dry_run=False, expect={})
    assert result.status == 'PASS' and result.release_url.endswith('/v1.2.3')
    assert fake.posts == []


def test_a_project_without_alpaca_version_file_tags_by_the_given_version(project):
    fake = FakeGitHub(version=None)
    assert run(project, fake).status == 'DRY_RUN'
