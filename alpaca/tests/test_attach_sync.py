"""`alpaca attach sync`: what happened upstream to the handoffs (op-062 t-686, AC-11; build plan 6.8, P18).

Sync reports merged (by ancestry or by the pull request's merged_at), closed and behind, and changes no ref but the
fetched upstream base."""
import json

import pytest

from alpaca import cli, db
from alpaca.attach import AttachError, github, handoff, study, sync
from alpaca.tests.attachkit import (FakeGitHub, add_tasks, add_worktree, all_refs, attached, break_remote, commit_in, git,  # noqa: F401
                                    owner_approve, ready, remote_branches, studio, symrefs, upstream)

TASK = "t-001"
BRANCH = "alpaca/t-001"


@pytest.fixture
def r(attached):
    return ready(attached)


@pytest.fixture
def gh(r):
    return FakeGitHub(upstream=r.att.upstream)


def handed_off(r, gh):
    owner_approve(r)
    handoff.plan(r.root, TASK)
    return handoff.execute(r.root, TASK, expect=r.head, api=gh)


def upstream_takes(up, how):
    """The upstream's own tool merges the pushed branch: a merge commit, a squash, or a rebase (a cherry-pick)."""
    git(up.work, "fetch", "-q", "origin", BRANCH)
    git(up.work, "checkout", "-q", up.branch)
    if how == "merge":
        git(up.work, "merge", "-q", "--no-ff", "-m", "merge the branch", "origin/" + BRANCH)
    elif how == "squash":
        git(up.work, "merge", "-q", "--squash", "origin/" + BRANCH)
        git(up.work, "commit", "-q", "-m", "the branch, squashed")
    else:
        git(up.work, "cherry-pick", "origin/" + BRANCH)
    git(up.work, "push", "-q", "origin", up.branch)


def sync_it(r, gh):
    return sync.run(r.root, api=gh)


def test_a_branch_merged_with_a_merge_commit_is_merged_by_ancestry(r, gh):
    """AC-11: no API call needed."""
    handed_off(r, gh)
    upstream_takes(r.att.upstream, "merge")
    gh.calls.clear()
    res = sync_it(r, gh)
    assert [(m["task"], m["by"], m["head"], m["pr_number"]) for m in res["merged"]] == [(TASK, "ancestry", r.head, 1)]
    assert res["closed"] == [] and res["open"] == [] and res["behind"] == []
    assert gh.calls == []                                                 # ancestry decided; the API was not asked


@pytest.mark.parametrize("how", ["squash", "rebase"])
def test_a_squash_or_rebase_merge_is_merged_by_the_pull_requests_merged_at(r, gh, how):
    """AC-11 / PM t-654.4 / E5: the head is not an ancestor of the base, so the pull request says it."""
    handed_off(r, gh)
    upstream_takes(r.att.upstream, how)
    gh.merge_outside(1)
    res = sync_it(r, gh)
    assert [(m["task"], m["by"]) for m in res["merged"]] == [(TASK, "merged_at")]
    assert res["merged"][0]["merged_at"] == "2026-10-15T00:00:00Z"
    assert git(r.att.app, "merge-base", "--is-ancestor", r.head, "refs/remotes/origin/main", check=False) == ""


def test_a_squash_merge_without_a_reachable_api_stays_open_with_a_note(r, gh):
    handed_off(r, gh)
    upstream_takes(r.att.upstream, "squash")
    gh.fail[("GET", "/repos/acme/demo/pulls/1")] = "down"
    res = sync_it(r, gh)
    assert res["merged"] == [] and [o["task"] for o in res["open"]] == [TASK] and res["open"][0]["note"] == "state unknown"
    assert any("could not be read" in n for n in res["notes"])


def test_a_closed_pull_request_is_reported_closed(r, gh):
    handed_off(r, gh)
    gh.close(1)
    res = sync_it(r, gh)
    assert [c["task"] for c in res["closed"]] == [TASK] and res["merged"] == [] and res["open"] == []


def test_an_open_pull_request_is_reported_open(r, gh):
    handed_off(r, gh)
    res = sync_it(r, gh)
    assert [(o["task"], o["pr_number"]) for o in res["open"]] == [(TASK, 1)] and res["merged"] == []


def test_a_branch_that_is_behind_the_base_is_listed_with_its_count(r, gh):
    r.att.upstream.commit({"a.txt": "1\n"}, "upstream one")
    r.att.upstream.commit({"b.txt": "2\n"}, "upstream two")
    res = sync_it(r, gh)
    assert res["behind"] == [{"task": TASK, "branch": BRANCH, "behind": 2, "ahead": 1, "handed_off": False}]
    assert res["merged"] == [] and res["base_sha"] == r.att.upstream.sha()


def test_a_merged_branch_is_not_reported_behind(r, gh):
    handed_off(r, gh)
    upstream_takes(r.att.upstream, "merge")
    r.att.upstream.commit({"later.txt": "x\n"}, "more upstream work")
    res = sync_it(r, gh)
    assert [m["task"] for m in res["merged"]] == [TASK] and res["behind"] == []


def test_a_project_with_no_handoff_reports_nothing(attached, capsys):
    ready(attached)
    assert cli.main(["attach", "sync"]) == cli.PASS
    out = capsys.readouterr().out
    assert "no handoffs, and no work branch is behind" in out and "GATE alpaca-attach-sync: PASS" in out


# ---- AC-11: no ref moves --------------------------------------------------------------------------------------------------

def test_sync_moves_no_ref_but_the_fetched_upstream_base(r, gh):
    """AC-11."""
    handed_off(r, gh)
    r.att.upstream.commit({"later.txt": "x\n"}, "upstream moves on")
    upstream_takes(r.att.upstream, "squash")
    gh.merge_outside(1)
    app = str(r.att.app)
    before, bare_before, syms = all_refs(app), all_refs(r.att.upstream.bare), symrefs(app)
    heads = {"app": git(app, "rev-parse", "HEAD"), "wt": git(r.wt, "rev-parse", "HEAD"),
             "branch": git(app, "symbolic-ref", "HEAD"), "wt_branch": git(r.wt, "symbolic-ref", "HEAD")}
    res = sync_it(r, gh)
    after = all_refs(app)
    changed = {k for k in set(before) | set(after) if before.get(k) != after.get(k)}
    assert changed == {"refs/remotes/origin/main"}
    assert after["refs/remotes/origin/main"] == r.att.upstream.sha() == res["base_sha"]
    assert all_refs(r.att.upstream.bare) == bare_before and symrefs(app) == syms
    assert heads == {"app": git(app, "rev-parse", "HEAD"), "wt": git(r.wt, "rev-parse", "HEAD"),
                     "branch": git(app, "symbolic-ref", "HEAD"), "wt_branch": git(r.wt, "symbolic-ref", "HEAD")}
    assert git(r.wt, "status", "--porcelain") == "" and git(app, "tag") == ""
    assert not [r_ for r_ in after if r_.startswith("refs/remotes/") and r_ != "refs/remotes/origin/main"
                and r_ not in before]


def test_sync_twice_changes_nothing_the_second_time(r, gh):
    handed_off(r, gh)
    sync_it(r, gh)
    refs = all_refs(str(r.att.app))
    sync_it(r, gh)
    assert all_refs(str(r.att.app)) == refs


def test_sync_records_one_event_with_what_it_found(r, gh):
    handed_off(r, gh)
    gh.close(1)
    sync_it(r, gh)
    conn = db.connect(str(r.root))
    try:
        rows = [e for e in db.events(conn, limit=10 ** 9) if e["kind"] == "attach-sync"]
    finally:
        conn.close()
    assert len(rows) == 1 and rows[0]["data"]["closed"] == [TASK] and rows[0]["data"]["merged"] == []


def test_an_unreachable_upstream_blocks_the_verb(r, capsys):
    restore = break_remote(r.att.upstream)
    assert cli.main(["attach", "sync", "--json"]) == cli.BLOCKED
    out = json.loads(capsys.readouterr().out)
    assert out["verdict"] == "BLOCKED" and "fetch" in out["reason"]
    restore()


def test_the_verb_prints_each_finding(r, gh, capsys, monkeypatch):
    monkeypatch.setattr(github, "default_api", gh)
    handed_off(r, gh)
    r.att.upstream.commit({"later.txt": "x\n"}, "upstream moves on")
    gh.close(1)
    assert cli.main(["attach", "sync"]) == cli.PASS
    out = capsys.readouterr().out
    assert "closed: t-001 (#1)" in out and "behind: t-001 is 1 commit(s) behind the base" in out


# ---- the codebase map ---------------------------------------------------------------------------------------------------

def test_the_map_is_refreshed_only_when_it_exists(r, gh, monkeypatch):
    calls = []
    monkeypatch.setattr(study, "prepare", lambda root, **kw: (calls.append(kw), {"map": {"stale_areas": ["core"]}})[1])
    assert sync_it(r, gh)["stale_areas"] == [] and calls == []
    (r.att.root / "design" / "attach").mkdir(parents=True)
    (r.att.root / "design" / "attach" / "codebase-map.md").write_text("---\nattach_sha: x\n---\n")
    assert sync_it(r, gh)["stale_areas"] == ["core"] and len(calls) == 1


def test_a_map_that_cannot_be_refreshed_is_a_note_not_a_failure(r, gh, monkeypatch):
    def broken(root, **kw):
        raise AttachError("the map has no front matter")

    monkeypatch.setattr(study, "prepare", broken)
    (r.att.root / "design" / "attach").mkdir(parents=True)
    (r.att.root / "design" / "attach" / "codebase-map.md").write_text("x\n")
    res = sync_it(r, gh)
    assert res["stale_areas"] == [] and any("no front matter" in n for n in res["notes"])
