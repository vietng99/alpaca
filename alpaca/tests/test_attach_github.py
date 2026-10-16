"""Attach's own GitHub client (op-062 t-686, build plan P19): four calls through one injected callable, no network."""
import io
import json
import urllib.error

import pytest

from alpaca.attach import AttachError, github, gitops

SECRET = "ghp_sentinelSECRET0123456789"


def pr(number, ref="alpaca/t-001", state="open", sha="a" * 40, merged_at=None, base="main"):
    return {"number": number, "html_url": "https://github.com/acme/demo/pull/%d" % number, "state": state,
            "merged_at": merged_at, "merge_commit_sha": None, "head": {"ref": ref, "sha": sha}, "base": {"ref": base}}


class Recorder:
    def __init__(self, *answers):
        self.answers, self.calls = list(answers), []

    def __call__(self, method, path, payload=None):
        self.calls.append((method, path, payload))
        return self.answers.pop(0)


def test_a_repository_must_be_owner_slash_name():
    for bad in ("", "demo", "a/b/c", "../x", "a b/c", None, 3):
        with pytest.raises(AttachError, match="owner/name"):
            github.GitHub(bad)
    assert github.GitHub("acme/demo").repository == "acme/demo"


def test_find_pr_asks_for_every_state_of_the_branch_and_prefers_the_open_one():
    api = Recorder([pr(2, state="closed"), pr(3, ref="other"), pr(5), pr(4, state="closed", merged_at="t")])
    view = github.GitHub("acme/demo", api).find_pr("alpaca/t-001", "main")
    method, path, payload = api.calls[0]
    assert method == "GET" and payload is None
    assert path.startswith("/repos/acme/demo/pulls?") and "state=all" in path and "head=acme%3Aalpaca%2Ft-001" in path
    assert "base=main" in path
    assert view["number"] == 5 and view["state"] == "open"


def test_find_pr_takes_the_newest_when_none_is_open_and_none_when_none_exists():
    api = Recorder([pr(2, state="closed"), pr(4, state="closed", merged_at="2026-01-01T00:00:00Z")], [], [pr(1, ref="x")])
    gh = github.GitHub("acme/demo", api)
    newest = gh.find_pr("alpaca/t-001", "main")
    assert newest["number"] == 4 and newest["merged"] is True and newest["merged_at"] == "2026-01-01T00:00:00Z"
    assert gh.find_pr("alpaca/t-001", "main") is None
    assert gh.find_pr("alpaca/t-001", "main") is None
    with pytest.raises(AttachError, match="not a branch name"):
        gh.find_pr("../x", "main")
    with pytest.raises(AttachError, match="not a list"):
        github.GitHub("acme/demo", Recorder({"message": "x"})).find_pr("alpaca/t-001", "main")


def test_open_pr_posts_the_branch_into_the_base_and_returns_the_facts():
    api = Recorder(pr(9))
    made = github.GitHub("acme/demo", api).open_pr("alpaca/t-001", "main", "A title", "A body")
    assert api.calls == [("POST", "/repos/acme/demo/pulls", {"title": "A title", "body": "A body", "head": "alpaca/t-001", "base": "main"})]
    assert (made["number"], made["html_url"], made["head_sha"], made["state"]) == (9, "https://github.com/acme/demo/pull/9", "a" * 40, "open")
    bad = pr(9)
    bad["html_url"] = ""
    with pytest.raises(AttachError, match="no address"):
        github.GitHub("acme/demo", Recorder(bad)).open_pr("alpaca/t-001", "main", "t", "b")
    with pytest.raises(AttachError, match="not a pull request"):
        github.GitHub("acme/demo", Recorder({"message": "Not Found"})).open_pr("alpaca/t-001", "main", "t", "b")


def test_get_pr_validates_the_number_and_the_answer():
    api = Recorder(pr(4, merged_at="2026-01-01T00:00:00Z", state="closed"))
    view = github.GitHub("acme/demo", api).get_pr(4)
    assert api.calls[0][:2] == ("GET", "/repos/acme/demo/pulls/4") and view["merged"] is True
    for bad in (0, -1, True, "4", None):
        with pytest.raises(AttachError, match="pull request number"):
            github.GitHub("acme/demo", Recorder()).get_pr(bad)


def test_merge_pr_pins_the_sha_and_the_method_and_needs_a_merged_answer():
    api = Recorder({"merged": True, "sha": "c" * 40}, {"merged": False, "message": "no"}, {"merged": True})
    gh = github.GitHub("acme/demo", api)
    assert gh.merge_pr(4, "a" * 40, "squash") == {"merged": True, "sha": "c" * 40}
    assert api.calls[0] == ("PUT", "/repos/acme/demo/pulls/4/merge", {"sha": "a" * 40, "merge_method": "squash"})
    with pytest.raises(AttachError, match="did not merge"):
        gh.merge_pr(4, "a" * 40, "merge")
    with pytest.raises(AttachError, match="did not merge"):
        gh.merge_pr(4, "a" * 40, "merge")
    for args, why in (((4, "abc", "merge"), "full head sha"), ((4, "a" * 40, "ff"), "merge method"), ((0, "a" * 40, "merge"), "pull request number")):
        with pytest.raises(AttachError, match=why):
            gh.merge_pr(*args)
    assert len(api.calls) == 3


def test_ready_refuses_the_default_client_without_a_token(monkeypatch):
    monkeypatch.setattr(gitops, "token", lambda: None)
    with pytest.raises(AttachError, match="no GitHub token"):
        github.GitHub("acme/demo").ready()
    github.GitHub("acme/demo", Recorder()).ready()                                  # an injected client needs none
    monkeypatch.setattr(gitops, "token", lambda: SECRET)
    github.GitHub("acme/demo").ready()


# ---- the default callable, with urllib replaced --------------------------------------------------------------------------------

class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_the_default_api_sends_the_token_in_a_header_and_parses_json(monkeypatch):
    monkeypatch.setattr(gitops, "token", lambda: SECRET)
    seen = {}

    def urlopen(req, timeout=None):
        seen.update(url=req.full_url, method=req.get_method(), auth=req.get_header("Authorization"), data=req.data, timeout=timeout)
        return FakeResponse(json.dumps({"number": 1}).encode())

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    assert github.default_api("POST", "/repos/acme/demo/pulls", {"a": 1}) == {"number": 1}
    assert seen["url"] == "https://api.github.com/repos/acme/demo/pulls" and seen["method"] == "POST"
    assert seen["auth"] == "Bearer " + SECRET and json.loads(seen["data"]) == {"a": 1} and seen["timeout"] == github.TIMEOUT


def test_the_default_api_needs_a_token_and_a_repos_path(monkeypatch):
    monkeypatch.setattr(gitops, "token", lambda: None)
    with pytest.raises(AttachError, match="no GitHub token"):
        github.default_api("GET", "/repos/acme/demo/pulls/1")
    monkeypatch.setattr(gitops, "token", lambda: SECRET)
    for path in ("/user", "https://evil.example/repos/x", "repos/x"):
        with pytest.raises(AttachError, match="/repos/"):
            github.default_api("GET", path)


def test_an_http_error_names_the_status_and_githubs_message_but_never_the_token(monkeypatch):
    monkeypatch.setattr(gitops, "token", lambda: SECRET)

    def urlopen(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 409, "Conflict", {}, io.BytesIO(json.dumps(
            {"message": "Head branch was modified. Token %s https://u:p\x40github.com/x" % "redacted"}).encode()))

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    with pytest.raises(AttachError) as caught:
        github.default_api("PUT", "/repos/acme/demo/pulls/1/merge", {"sha": "a"})
    text = str(caught.value)
    assert "409" in text and "Head branch was modified" in text and SECRET not in text and "u:p@" not in text


def test_a_network_failure_or_a_bad_body_is_a_refusal_without_the_token(monkeypatch):
    monkeypatch.setattr(gitops, "token", lambda: SECRET)
    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=None: (_ for _ in ()).throw(urllib.error.URLError("no route " + SECRET)))
    with pytest.raises(AttachError, match="could not be reached") as caught:
        github.default_api("GET", "/repos/acme/demo/pulls/1")
    assert SECRET not in str(caught.value)
    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=None: FakeResponse(b"<html>"))
    with pytest.raises(AttachError, match="not JSON"):
        github.default_api("GET", "/repos/acme/demo/pulls/1")
    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=None: FakeResponse(b""))
    assert github.default_api("GET", "/repos/acme/demo/pulls/1") == {}
