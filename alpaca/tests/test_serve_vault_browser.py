"""op-008 t-441: the dashboard's file readers are not a second serving door into the Rune2 vault.

The vault (`.alpaca/wiki`, `alpaca/wiki/config.py` `Config.for_project`) keeps a withdrawn source's
note and every capture the marking scanner quarantines under `wiki/private/` as plain files. Rune2's
doors refuse them (`alpaca/wiki/servable.py`); the dashboard's gated file browser (`/files/list`,
`/files/read`) and the report reader (`/hub/document.json`) read files, so they must refuse the vault
by path, whatever name reaches it. The one folder of `.alpaca/wiki` they keep is `decisions/`: the
record's decision pages (`alpaca/decisions.py`), which Rune2 never writes.

The first test is the t-070 check round 3 probe X01
(`.alpaca/proofs/op-008/t-070.logs/check-r3-adv/probes/x01_files_browser_vault_bytes.py`) run against
the real HTTP handler: a real capture, a real withdrawal, a real marking-scanner quarantine.
"""
import base64
import http.server
import json
import os
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "wiki"))
from native_helpers import T10, T11, appender, at  # noqa: E402

from alpaca import db, profile, serve  # noqa: E402

LOGIN = "reader:test-password"
TALK = "The cormorant ledger names the quayside vault combination as amber heron seventeen."
PRIV = "SECRET: REL TO FVEY. The kestrel cipher wheel sits under the north quay."
WITHDRAWN = ("amber heron seventeen", "cormorant ledger")
PRIVATE = ("kestrel cipher wheel",)
SID = "t441-talk"
VAULT_WORD = "osprey lantern ninety"


@pytest.fixture
def server(project):
    Path(project, ".alpaca", "files-auth").write_text(LOGIN)
    live = serve.Live(project)
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), serve.make_handler(live, project))
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield project, "http://127.0.0.1:%d" % httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()


def fetch(url, route, raw=False):
    """GET `route` with the file login; the query is sent as written when `raw` is true."""
    req = urllib.request.Request(url + route)
    req.add_header("Authorization", "Basic " + base64.b64encode(LOGIN.encode()).decode())
    try:
        r = urllib.request.urlopen(req, timeout=30)
    except urllib.error.HTTPError as exc:
        r = exc
    return r.status, r.read().decode("utf-8", "replace")


def files(url, verb, rel):
    status, body = fetch(url, "/files/%s?path=%s" % (verb, urllib.parse.quote(rel, safe="")))
    assert status == 200, (verb, rel, status, body)
    return json.loads(body)


#: Where a browser user starts: the project root, and the two runtime folders the browser opens by
#: typed path (it does not list `.alpaca` itself).
STARTS = ("", ".alpaca/wiki", ".alpaca/proofs")


def crawl(url, starts=STARTS):
    """Every file the browser lists, walking from `starts`, read through `/files/read`: {path: text}."""
    out, todo, seen = {}, list(starts), set()
    while todo:
        here = todo.pop()
        if here in seen:
            continue
        seen.add(here)
        for entry in files(url, "list", here).get("entries", []):
            if entry["dir"]:
                todo.append(entry["path"])
            else:
                out[entry["path"]] = files(url, "read", entry["path"]).get("text") or ""
    return out


def put(root, rel, text):
    path = Path(root, rel)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def capture_withdrawn_and_private(root):
    """X01's setup: a transcript captured then withdrawn, and a note the marking scanner quarantines."""
    from alpaca import observability
    from alpaca.wiki import service
    path = Path(root, ".alpaca", "transcripts", SID + ".jsonl")
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [{"type": "user", "message": {"content": "What does the cormorant ledger say?"}},
             {"type": "assistant", "message": {"content": [{"type": "text", "text": TALK}]}}]
    path.write_text("".join(json.dumps(r) + "\n" for r in lines), encoding="utf-8")
    observability.expect_source(root, SID, "claude", locator=str(path))
    at(root, "result", {"body": "A note in the talk session."}, T10, session=SID)
    service.capture(root, sessions=[SID], session_events=True, deadline_s=None)
    at(root, "result", {"body": PRIV}, T11, session="t441-priv")
    service.capture(root, sessions=["t441-priv"], session_events=True, deadline_s=None)
    done = service.withdraw_source(root, "transcript:" + SID, session="t441-owner",
                                   record=appender(root, "t441-owner"), reason="wrong session")
    assert done.status == "applied"
    return service


def vault_files_holding(root, words):
    """The vault files whose text holds one of `words`, read from disk (what the browser must refuse)."""
    vault = Path(root, ".alpaca", "wiki")
    hits = set()
    for path in vault.rglob("*"):
        if path.is_file():
            text = path.read_text(encoding="utf-8", errors="replace")
            if any(w in text for w in words):
                hits.add(path.relative_to(root).as_posix())
    return hits


def test_x01_the_file_browser_serves_no_withdrawn_or_private_vault_text(server):
    """X01 against the real handler: Rune2 refuses the withdrawn and the private text, the vault still
    holds both as files, and a crawl of everything the browser lists from the project root, plus a
    direct read of each of those vault files, returns neither."""
    root, url = server
    service = capture_withdrawn_and_private(root)
    q = service.query(root, "What does the cormorant ledger name as the quayside vault combination?")
    q2 = service.query(root, "Where does the kestrel cipher wheel sit?")
    served = json.dumps([{k: v for k, v in x.items() if k != "question"} for x in (q, q2)])
    assert "amber heron seventeen" not in served and "north quay" not in served
    withdrawn_files = vault_files_holding(root, WITHDRAWN)
    private_files = vault_files_holding(root, PRIVATE)
    assert any("/raw/transcripts/" in p for p in withdrawn_files), withdrawn_files
    assert any("/wiki/private/" in p for p in private_files), private_files

    seen = crawl(url)
    leaks = sorted((p, w) for p, text in seen.items() for w in WITHDRAWN + PRIVATE if w in text)
    assert leaks == []
    assert not [p for p in seen if p.startswith(".alpaca/wiki/") and not p.startswith(".alpaca/wiki/decisions/")]
    for rel in sorted(withdrawn_files | private_files):
        got = files(url, "read", rel)
        assert "text" not in got and got.get("error"), rel
    assert files(url, "list", ".alpaca/wiki/raw")["entries"] == []
    assert files(url, "list", ".alpaca/wiki/wiki/private")["entries"] == []


@pytest.fixture
def vault_tree(server):
    """A vault laid out as Rune2 lays it out, a decision page and a proof, plus private record files."""
    root, url = server
    body = "# Note\n\n%s\n" % VAULT_WORD
    for rel in (".alpaca/wiki/raw/transcripts/abc/transcript-s1-summary.md",
                ".alpaca/wiki/raw/op-8/000001-result.md",
                ".alpaca/wiki/wiki/private/raw/op-8/000002-result.md",
                ".alpaca/wiki/wiki/page.md",
                ".alpaca/wiki/projection/view.md",
                ".alpaca/wiki/pages/legacy.md",
                ".alpaca/wiki/reports/legacy.md",
                ".alpaca/wiki/CONTEXT.md",
                ".alpaca/wiki/rune.toml",
                ".alpaca/wiki/ledger/events.txt",
                ".alpaca/transcripts/s1.md",
                ".alpaca/observability/state.json",
                ".alpaca/config/posture.yaml",
                ".alpaca/worktrees/w/notes.md",
                ".alpaca/web-session-key",
                ".alpaca/totp-secret",
                ".claude/projects/p/s.md",
                ".codex/sessions/s.md",
                ".env"):
        put(root, rel, body)
    put(root, ".alpaca/wiki/decisions/dec-000000000001.md", "# Decision dec-000000000001\n\nKeep the clock.\n")
    put(root, ".alpaca/proofs/op-001/t-001.md", "# Proof\n\nAll passed.\n")
    put(root, "docs/readme.md", "# Docs\n")
    return root, url


def _no_vault_word(url, rel):
    for verb in ("read", "list"):
        status, body = fetch(url, "/files/%s?path=%s" % (verb, rel))
        assert VAULT_WORD not in body, (verb, rel)
        if status == 200:
            got = json.loads(body)
            # the vault folder itself lists, showing the record's decision pages only
            assert "text" not in got, (verb, rel, got)
            assert [e["name"] for e in got.get("entries", [])] in ([], ["decisions"]), (verb, rel, got)


def test_the_browser_keeps_decisions_and_proofs_and_refuses_the_rest_of_the_vault(vault_tree):
    root, url = vault_tree
    assert "Keep the clock." in files(url, "read", ".alpaca/wiki/decisions/dec-000000000001.md")["text"]
    assert "All passed." in files(url, "read", ".alpaca/proofs/op-001/t-001.md")["text"]
    assert [e["name"] for e in files(url, "list", ".alpaca/wiki")["entries"]] == ["decisions"]
    assert files(url, "list", ".alpaca")["entries"] == []
    seen = crawl(url)
    assert VAULT_WORD not in json.dumps(seen)
    assert ".alpaca/wiki/decisions/dec-000000000001.md" in seen and "docs/readme.md" in seen
    for rel in (".alpaca/wiki/raw/op-8/000001-result.md", ".alpaca/wiki/wiki/page.md",
                ".alpaca/wiki/projection/view.md", ".alpaca/wiki/pages/legacy.md",
                ".alpaca/wiki/reports/legacy.md", ".alpaca/wiki/CONTEXT.md", ".alpaca/wiki/rune.toml",
                ".alpaca/wiki/ledger/events.txt", ".alpaca/wiki/wiki/private/raw/op-8/000002-result.md",
                ".alpaca/transcripts/s1.md", ".alpaca/observability/state.json",
                ".alpaca/config/posture.yaml", ".alpaca/worktrees/w/notes.md",
                ".alpaca/web-session-key", ".alpaca/totp-secret", ".alpaca/files-auth",
                ".claude/projects/p/s.md",
                ".codex/sessions/s.md", ".env"):
        _no_vault_word(url, urllib.parse.quote(rel, safe=""))


TRAVERSALS = (
    ".alpaca/proofs/../wiki/raw/op-8/000001-result.md",
    ".alpaca/wiki/decisions/../raw/op-8/000001-result.md",
    "docs/../.alpaca/wiki/raw/op-8/000001-result.md",
    "./.alpaca/./wiki/raw/op-8/000001-result.md",
    ".alpaca//wiki//raw//op-8//000001-result.md",
    "/.alpaca/wiki/raw/op-8/000001-result.md",
    ".alpaca/wiki/raw/op-8/",
    ".alpaca/wiki/wiki/private/",
    ".alpaca/WIKI/raw/op-8/000001-result.md",
    ".ALPACA/wiki/raw/op-8/000001-result.md",
    ".alpaca/wiki/Decisions/../raw/op-8/000001-result.md",
)


@pytest.mark.parametrize("rel", TRAVERSALS)
def test_traversal_spellings_of_a_vault_path_are_refused(vault_tree, rel):
    _root, url = vault_tree
    _no_vault_word(url, urllib.parse.quote(rel, safe=""))


@pytest.mark.parametrize("raw", (
    "%2e%2e%2f.alpaca%2fwiki%2fraw%2fop-8%2f000001-result.md",
    "docs%2f%2e%2e%2f.alpaca%2fwiki%2fraw%2fop-8%2f000001-result.md",
    ".alpaca%2Fwiki%2Fraw%2Fop-8%2F000001-result.md",
    ".alpaca/proofs/%2e%2e/wiki/raw/op-8/000001-result.md",
    ".alpaca/proofs/%252e%252e/wiki/raw/op-8/000001-result.md",
    "%2Fproc%2Fself%2Fcwd%2F..%2F.alpaca%2Fwiki%2Fraw%2Fop-8%2F000001-result.md",
))
def test_url_encoded_spellings_of_a_vault_path_are_refused(vault_tree, raw):
    _root, url = vault_tree
    _no_vault_word(url, raw)


def test_a_link_into_the_vault_under_another_name_is_refused(vault_tree):
    """Links from anywhere the browser serves: a dot-dir alias of the runtime folder (the live project
    has `.te -> .alpaca`), a folder link and a file link from the project tree, a link inside proofs,
    and a decision page that is a link to a vault note."""
    root, url = vault_tree
    r = Path(root)
    (r / ".te").symlink_to(".alpaca", target_is_directory=True)
    (r / "docs" / "vault").symlink_to("../.alpaca/wiki", target_is_directory=True)
    (r / "docs" / "raw").symlink_to("../.alpaca/wiki/raw", target_is_directory=True)
    (r / "docs" / "note.md").symlink_to("../.alpaca/wiki/raw/op-8/000001-result.md")
    (r / "docs" / "runtime").symlink_to("../.alpaca", target_is_directory=True)
    (r / ".alpaca" / "proofs" / "op-001" / "vault").symlink_to("../../wiki", target_is_directory=True)
    (r / ".alpaca" / "proofs" / "op-001" / "x.md").symlink_to("../../wiki/wiki/private/raw/op-8/000002-result.md")
    (r / ".alpaca" / "wiki" / "decisions" / "dec-000000000002.md").symlink_to("../raw/op-8/000001-result.md")
    (r / ".alpaca" / "wiki" / "decisions" / "inner").symlink_to("../wiki/private", target_is_directory=True)
    for rel in (".te/wiki/raw/op-8/000001-result.md", ".te/wiki/raw", ".te/transcripts/s1.md",
                "docs/vault/raw/op-8/000001-result.md", "docs/vault", "docs/raw/op-8/000001-result.md",
                "docs/note.md", "docs/runtime/wiki/raw/op-8/000001-result.md",
                "docs/runtime/transcripts/s1.md", ".alpaca/proofs/op-001/vault/raw/op-8/000001-result.md",
                ".alpaca/proofs/op-001/x.md", ".alpaca/wiki/decisions/dec-000000000002.md",
                ".alpaca/wiki/decisions/inner/raw/op-8/000002-result.md"):
        _no_vault_word(url, urllib.parse.quote(rel, safe=""))
    assert VAULT_WORD not in json.dumps(crawl(url))


def test_the_report_reader_refuses_the_vault_even_when_a_profile_or_a_proof_points_at_it(vault_tree, monkeypatch):
    """`/hub/document.json` is the other file door: a profile documents root or a proof-report event
    naming a vault path must not open it, and the catalog lists no vault note; decisions still read."""
    root, url = vault_tree

    class Wide(profile.Profile):
        def paths(self):
            return {"documents": [".alpaca/wiki"]}
    monkeypatch.setattr(profile, "load", lambda r: Wide())
    conn = db.connect(root)
    try:
        db.append_event(conn, session="t441", actor="codex", kind="proof-report", ref="t-441",
                        data={"path": ".alpaca/wiki/raw/op-8/000001-result.md"})
    finally:
        conn.close()
    status, body = fetch(url, "/hub/document.json?path=.alpaca%2Fwiki%2Fdecisions%2Fdec-000000000001.md")
    assert status == 200 and "Keep the clock." in body
    for rel in (".alpaca/wiki/raw/op-8/000001-result.md", ".alpaca/wiki/wiki/page.md",
                ".alpaca/wiki/pages/legacy.md", ".alpaca/wiki/reports/legacy.md",
                ".alpaca/wiki/CONTEXT.md", ".alpaca/wiki/wiki/private/raw/op-8/000002-result.md"):
        status, body = fetch(url, "/hub/document.json?path=" + urllib.parse.quote(rel, safe=""))
        assert status in (400, 404) and VAULT_WORD not in body, (rel, status)
    for category in ("all", "reports", "proofs", "runbooks", "decisions"):
        status, body = fetch(url, "/hub/documents.json?category=%s&limit=200" % category)
        assert status == 200 and VAULT_WORD not in body
        assert not [i for i in json.loads(body)["items"]
                    if i["path"].startswith(".alpaca/wiki/") and not i["path"].startswith(".alpaca/wiki/decisions/")]


def test_the_raw_file_route_stays_closed_to_the_vault(vault_tree):
    root, url = vault_tree
    (Path(root) / "docs" / "note.md").symlink_to("../.alpaca/wiki/raw/op-8/000001-result.md")
    for rel in (".alpaca/wiki/raw/op-8/000001-result.md", "docs/note.md",
                ".alpaca/wiki/decisions/dec-000000000001.md"):
        status, body = fetch(url, "/file?path=" + urllib.parse.quote(rel, safe=""))
        assert status == 404 and VAULT_WORD not in body, rel
