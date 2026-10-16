"""Attach's own GitHub client (op-062 t-686, build plan P19).

`alpaca.release.publish.github_pr` lists only open pull requests, returns no number and cannot merge, so a handoff
has its own small client. It talks to api.github.com with the token of `gitops.token()` (P5) and nothing else; every
call goes through one callable, `api(method, path, payload) -> parsed JSON`, which a test replaces with a fake so no
test touches the network.

The token is never in an exception text, a log or a receipt: a failed call raises AttachError with the status and
GitHub's own message, scrubbed of user info.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request

from alpaca.attach import AttachError, gitops

API = "https://api.github.com"
TIMEOUT = 30
METHODS = ("merge", "squash", "rebase")
_REPOSITORY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*")
_BRANCH = re.compile(r"[A-Za-z0-9][A-Za-z0-9_./-]*")
_SHA = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


def default_api(method, path, payload=None):
    """One call to api.github.com with the attach token; returns the parsed JSON body."""
    token = gitops.token()
    if not token:
        raise AttachError("no GitHub token: set ALPACA_ATTACH_TOKEN or sign in with gh (gh auth login)")
    if not isinstance(path, str) or not path.startswith("/repos/"):
        raise AttachError("attach calls only /repos/ paths of the GitHub API")
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(API + path, data=data, method=method, headers={
        "Authorization": "Bearer " + token, "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28", "Content-Type": "application/json", "User-Agent": "alpaca-attach"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = str(json.loads(exc.read().decode("utf-8", "replace")).get("message", ""))
        except (ValueError, AttributeError, OSError):
            pass
        raise AttachError("GitHub %s %s answered %d%s" % (method, path.split("?", 1)[0], exc.code,
                                                          ": " + gitops.scrub(detail)[:200] if detail else "")) from None
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise AttachError("GitHub could not be reached (%s)" % type(exc).__name__) from None
    try:
        return json.loads(body.decode("utf-8")) if body else {}
    except ValueError:
        raise AttachError("GitHub answered something that is not JSON") from None


def _view(raw) -> dict:
    """The facts attach reads from a pull request object, or an AttachError when the answer is not one."""
    if not isinstance(raw, dict) or isinstance(raw.get("number"), bool) or not isinstance(raw.get("number"), int):
        raise AttachError("GitHub answered something that is not a pull request")
    head, base = raw.get("head") or {}, raw.get("base") or {}
    head_sha = head.get("sha") if isinstance(head, dict) else None
    return {"number": raw["number"], "html_url": str(raw.get("html_url") or ""), "state": str(raw.get("state") or ""),
            "merged": bool(raw.get("merged") or raw.get("merged_at")), "merged_at": raw.get("merged_at") or None,
            "head_sha": head_sha if isinstance(head_sha, str) and _SHA.fullmatch(head_sha) else "",
            "head_ref": str(head.get("ref") or "") if isinstance(head, dict) else "",
            "base_ref": str(base.get("ref") or "") if isinstance(base, dict) else "",
            "merge_commit_sha": str(raw.get("merge_commit_sha") or "")}


class GitHub:
    """The four calls a handoff needs, on one repository (owner/name)."""

    def __init__(self, repository, api=None):
        if not isinstance(repository, str) or not _REPOSITORY.fullmatch(repository):
            raise AttachError("attach.repository must be owner/name to open or merge a pull request")
        self.repository = repository
        self.injected = api is not None
        self.api = api or default_api

    def ready(self) -> None:
        """Refuse early when the default client has no token, before anything is pushed."""
        if not self.injected and not gitops.token():
            raise AttachError("no GitHub token: set ALPACA_ATTACH_TOKEN or sign in with gh (gh auth login); "
                              "nothing was pushed")

    def _path(self, tail) -> str:
        return "/repos/%s/%s" % (self.repository, tail)

    def find_pr(self, branch, base):
        """The pull request from `branch` into `base` (any state): the open one when there is one, else the newest.
        None when there is none."""
        if not _BRANCH.fullmatch(str(branch)) or not _BRANCH.fullmatch(str(base)):
            raise AttachError("not a branch name")
        owner = self.repository.split("/", 1)[0]
        query = urllib.parse.urlencode({"state": "all", "head": "%s:%s" % (owner, branch), "base": base, "per_page": 100})
        found = self.api("GET", self._path("pulls?" + query), None)
        if not isinstance(found, list):
            raise AttachError("GitHub answered something that is not a list of pull requests")
        views = [_view(x) for x in found if isinstance(x, dict) and isinstance(x.get("head"), dict)
                 and x["head"].get("ref") == branch]
        if not views:
            return None
        opened = [v for v in views if v["state"] == "open"]
        return max(opened or views, key=lambda v: v["number"])

    def open_pr(self, branch, base, title, body) -> dict:
        """Open the pull request; returns `{"number", "html_url", "head_sha", ...}`."""
        if not _BRANCH.fullmatch(str(branch)) or not _BRANCH.fullmatch(str(base)):
            raise AttachError("not a branch name")
        made = _view(self.api("POST", self._path("pulls"), {"title": title, "body": body, "head": branch, "base": base}))
        if not made["html_url"]:
            raise AttachError("GitHub opened a pull request and gave no address for it")
        return made

    def get_pr(self, number) -> dict:
        if isinstance(number, bool) or not isinstance(number, int) or number < 1:
            raise AttachError("not a pull request number")
        return _view(self.api("GET", self._path("pulls/%d" % number), None))

    def merge_pr(self, number, sha, method) -> dict:
        """Merge pull request `number` only while its head is `sha` (GitHub refuses with 409 otherwise). Returns
        `{"merged": True, "sha": merge commit}`."""
        if isinstance(number, bool) or not isinstance(number, int) or number < 1:
            raise AttachError("not a pull request number")
        if not _SHA.fullmatch(str(sha)):
            raise AttachError("a merge needs the full head sha to pin")
        if method not in METHODS:
            raise AttachError("the merge method is one of %s" % ", ".join(METHODS))
        done = self.api("PUT", self._path("pulls/%d/merge" % number), {"sha": sha, "merge_method": method})
        if not isinstance(done, dict) or done.get("merged") is not True or not isinstance(done.get("sha"), str):
            raise AttachError("GitHub did not merge the pull request")
        return {"merged": True, "sha": done["sha"]}
