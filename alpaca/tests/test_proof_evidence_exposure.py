"""op-008 t-463: no copy the app makes and no check-then-open race puts Rune2 vault text or a private
runtime file where the dashboard serves it.

The t-441 review (`.alpaca/proofs/op-008/t-441.logs/review.md`) found three routes past the file
browser's vault rule:

R-1  `alpaca proof seal` copies every `local:` evidence file into `.alpaca/proofs/<op>/<id>.evidence/`,
     which the browser serves. A cited vault note (a transcript summary, a quarantined capture)
     became readable there, and stayed readable after its source was withdrawn.
R-2  the same copy carried private runtime files: `local:.alpaca/files-auth` became
     `01-files-auth`, which the browser's exact-name secret check missed.
R-3  the readers resolved and checked a path, then opened it by name: a folder swapped for a link
     between the two made the open follow the link into the vault.

The tests below are the review's probes P4, P4b and P5, with the swap of P5 injected between the
check and the open so the race is hit every run, not once in 57.
"""
import base64
import http.server
import json
import os
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

from alpaca import cli, db, paths, proof, serve
from alpaca.tests import proofkit

LOGIN = "reader:t463-login-code"
VAULT_WORD = "osprey lantern ninety"
SECRET_WORD = "heron-key-4471"
HARMLESS = "plain docs text"

VAULT_NOTES = (".alpaca/wiki/raw/transcripts/abc/transcript-s1-summary.md",
               ".alpaca/wiki/raw/op-8/000001-result.md",
               ".alpaca/wiki/wiki/private/raw/op-8/000002-result.md")


# --------------------------------------------------------------------------- helpers
def put(root, rel, text):
    path = Path(root, rel)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def tree(project):
    """An initialised project with one op and one task, a vault laid out as Rune2 lays it out, a
    decision page, a docs file and the private runtime files the review names."""
    cli.main(["init"])
    cli.main(["op", "new", "close the evidence exposures", "--done-when", "the probes refuse"])
    cli.main(["task", "add", "--title", "task", "op-001", "seal over vault and private files",
              "--phase", "build"])
    for rel in VAULT_NOTES:
        put(project, rel, "# Note\n\n%s\n" % VAULT_WORD)
    put(project, ".alpaca/wiki/decisions/dec-000000000001.md", "# Decision\n\nKeep the clock.\n")
    put(project, "docs/readme.md", "# Docs\n\n%s\n" % HARMLESS)
    put(project, ".alpaca/files-auth", LOGIN)
    for rel in (".alpaca/web-session-key", ".alpaca/totp-secret", ".alpaca/terminal-url",
                ".alpaca/transcripts/s1.jsonl", ".alpaca/observability/state.json",
                ".alpaca/pool/tools/s1.jsonl", ".env"):
        put(project, rel, "%s\n" % SECRET_WORD)
    return project


@pytest.fixture
def server(tree):
    live = serve.Live(tree)
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), serve.make_handler(live, tree))
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield tree, "http://127.0.0.1:%d" % httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()


def fetch(url, route):
    req = urllib.request.Request(url + route)
    req.add_header("Authorization", "Basic " + base64.b64encode(LOGIN.encode()).decode())
    try:
        r = urllib.request.urlopen(req, timeout=30)
    except urllib.error.HTTPError as exc:
        r = exc
    return r.status, r.read().decode("utf-8", "replace")


def browse(url, verb, rel):
    status, body = fetch(url, "/files/%s?path=%s" % (verb, urllib.parse.quote(rel, safe="")))
    assert status == 200, (verb, rel, status, body)
    return json.loads(body)


def crawl(url, start):
    """Every file the browser lists below `start`, read through `/files/read`: {path: text}."""
    out, todo, seen = {}, [start], set()
    while todo:
        here = todo.pop()
        if here in seen:
            continue
        seen.add(here)
        for entry in browse(url, "list", here).get("entries", []):
            if entry["dir"]:
                todo.append(entry["path"])
            else:
                out[entry["path"]] = browse(url, "read", entry["path"]).get("text") or ""
    return out


def files_holding(root, folder, words):
    """Every file under `folder` (on disk) whose bytes hold one of `words`."""
    hits = []
    for path in Path(root, folder).rglob("*"):
        if path.is_file():
            text = path.read_text(encoding="utf-8", errors="replace")
            if any(w in text for w in words):
                hits.append(path.relative_to(root).as_posix())
    return sorted(hits)


def kept_dir(root):
    return os.path.join(root, ".alpaca", "proofs", "op-001", "t-001.evidence")


def seal(root, evidence):
    conn = db.connect(root)
    try:
        proofkit.write_report(root, "t-001", conn=conn, evidence=evidence)
        return proof.seal(conn, root, "t-001", session="t463")
    finally:
        conn.close()


def seal_problems(root, evidence):
    conn = db.connect(root)
    try:
        proofkit.write_report(root, "t-001", conn=conn, evidence=evidence)
        with pytest.raises(proof.Refusal) as e:
            proof.seal(conn, root, "t-001", session="t463")
        return e.value.problems
    finally:
        conn.close()


def subject(root):
    conn = db.connect(root)
    try:
        return proof.subject(conn, "t-001")
    finally:
        conn.close()


def resolved(root, pointers):
    conn = db.connect(root)
    try:
        return [proof.resolve_evidence(conn, root, {"pointer": p, "kind": "local", "note": None})
                for p in pointers]
    finally:
        conn.close()


# --------------------------------------------------------------------------- R-1
def test_r1_a_seal_hashes_a_cited_vault_note_and_copies_none_of_it(server):
    """P4: a report cites a transcript summary, a raw note and a quarantined capture in the vault,
    beside a decision page and a docs file. The seal binds every one by hash; it copies the decision
    page and the docs file and none of the vault files, and a copy an older seal left is pruned. The
    browser then finds no vault text anywhere under `.alpaca/proofs`."""
    root, url = server
    put(root, ".alpaca/proofs/op-001/t-001.evidence/01-transcript-s1-summary.md",
        "# An older seal's copy\n\n%s\n" % VAULT_WORD)
    cited = ["local:%s - a vault file" % rel for rel in VAULT_NOTES] + [
        "local:.alpaca/wiki/decisions/dec-000000000001.md - the decision",
        "local:docs/readme.md - the docs"]
    payload = seal(root, cited)

    by_ptr = {i["pointer"]: i for i in payload["evidence"]}
    for rel in VAULT_NOTES:
        item = by_ptr["local:" + rel]
        assert item["verified"] is True and len(item["sha256"]) == 64 and item["bytes"] > 0
        assert item["kept"] is None and "vault" in item["kept_reason"], item
    assert by_ptr["local:.alpaca/wiki/decisions/dec-000000000001.md"]["kept"].endswith(
        "04-dec-000000000001.md")
    assert by_ptr["local:docs/readme.md"]["kept"].endswith("05-readme.md")
    assert sorted(os.listdir(kept_dir(root))) == ["04-dec-000000000001.md", "05-readme.md"]
    assert files_holding(root, ".alpaca/proofs", [VAULT_WORD]) == []

    seen = crawl(url, ".alpaca/proofs")
    assert VAULT_WORD not in json.dumps(seen)
    assert ".alpaca/proofs/op-001/t-001.evidence/05-readme.md" in seen

    # the check verifies a vault pointer from the tree: it stands while the note is unchanged and
    # becomes a problem when the note changes, since there is no copy to fall back on
    conn = db.connect(root)
    try:
        assert proof.verify_detail(conn, root, "t-001")["ok"] is True
        put(root, VAULT_NOTES[1], "# Note\n\nrewritten\n")
        got = proof.verify_detail(conn, root, "t-001")
    finally:
        conn.close()
    assert got["ok"] is False
    assert any(VAULT_NOTES[1] in p and "changed since it was sealed" in p for p in got["problems"])


def test_r1_keep_evidence_copies_no_vault_file_even_when_handed_one(tree):
    """P4 at the copy itself: `keep_evidence` handed vault pointers (as a seal written before this
    rule, or any other caller, would hand them) writes nothing for them."""
    items = [{"pointer": "local:" + rel, "kind": "local", "note": None} for rel in VAULT_NOTES]
    proof.keep_evidence(tree, subject(tree), items)
    assert [i["kept"] for i in items] == [None, None, None]
    assert not os.path.isdir(kept_dir(tree)) or os.listdir(kept_dir(tree)) == []
    assert files_holding(tree, ".alpaca/proofs", [VAULT_WORD]) == []


# --------------------------------------------------------------------------- R-2
PRIVATE_POINTERS = (".alpaca/files-auth", ".alpaca/web-session-key", ".alpaca/totp-secret",
                    ".alpaca/terminal-url", ".alpaca/alpaca.db", ".alpaca/transcripts/s1.jsonl",
                    ".alpaca/observability/state.json", ".alpaca/pool/tools/s1.jsonl", ".env",
                    "docs/login", ".te/files-auth", ".te/transcripts/s1.jsonl")


@pytest.mark.parametrize("rel", PRIVATE_POINTERS)
def test_r2_a_seal_refuses_a_private_runtime_file_as_evidence(tree, rel):
    """P4b: the dashboard login, its session key and authenticator secret, the record db, the
    transcripts, observability and tool pool, `.env`, and the same files reached through a link
    (`docs/login`) or the `.te -> .alpaca` alias, are refused as evidence; nothing is copied."""
    Path(tree, "docs", "login").symlink_to("../.alpaca/files-auth")
    Path(tree, ".te").symlink_to(".alpaca", target_is_directory=True)
    problems = seal_problems(tree, ["local:%s - a private file" % rel,
                                    "local:docs/readme.md - the docs"])
    assert any(("local:" + rel) in p and "private runtime file" in p for p in problems), problems
    assert not os.path.isdir(kept_dir(tree))
    assert files_holding(tree, ".alpaca/proofs", [SECRET_WORD, LOGIN]) == []


def test_r2_keep_evidence_copies_no_private_file_even_when_handed_one(tree):
    """P4b at the copy: handed `local:.alpaca/files-auth` directly, `keep_evidence` copies nothing."""
    items = [{"pointer": "local:" + rel, "kind": "local", "note": None}
             for rel in (".alpaca/files-auth", ".alpaca/totp-secret", ".env")]
    proof.keep_evidence(tree, subject(tree), items)
    assert [i["kept"] for i in items] == [None, None, None]
    assert all("private" in i["kept_reason"] for i in items), items
    assert files_holding(tree, ".alpaca/proofs", [SECRET_WORD, LOGIN]) == []


KEPT_SECRET_NAMES = ("01-files-auth", "02-web-session-key", "03-totp-secret", "04-terminal-url",
                     "05-env", "06-service.env", "07-alpaca.db", "08-credentials.json")


def test_r2_the_browser_refuses_a_secret_behind_an_evidence_number(server):
    """A kept copy is named `NN-<slug of the basename>`, and the slug drops a leading dot: the
    browser's secret rule reads the name with the number (and, inside a kept folder, the dot) put
    back, so a copy an older seal made stays closed. A plain evidence log still reads."""
    root, url = server
    for name in KEPT_SECRET_NAMES:
        put(root, ".alpaca/proofs/op-001/t-003.evidence/" + name, "%s\n" % SECRET_WORD)
    put(root, "docs/01-files-auth", "%s\n" % SECRET_WORD)
    put(root, ".alpaca/proofs/op-001/t-003.evidence/09-run.log", "%s\n" % HARMLESS)
    listed = [e["name"] for e in browse(url, "list", ".alpaca/proofs/op-001/t-003.evidence")["entries"]]
    assert listed == ["09-run.log"]
    assert HARMLESS in browse(url, "read", ".alpaca/proofs/op-001/t-003.evidence/09-run.log")["text"]
    for rel in [".alpaca/proofs/op-001/t-003.evidence/" + n for n in KEPT_SECRET_NAMES] + ["docs/01-files-auth"]:
        got = browse(url, "read", rel)
        assert "text" not in got and got.get("error"), rel
    assert SECRET_WORD not in json.dumps(crawl(url, "")) + json.dumps(crawl(url, ".alpaca/proofs"))


def test_r2_the_browser_refuses_a_database_whatever_its_name(server):
    """The record db copied under a name with no `.db` ending, or a browser profile's cookie and
    login stores kept beside a proof (the live tree holds such copies under
    `.alpaca/proofs/op-012/t-324.evidence/`), are SQLite files: the text reader refuses them by
    their first bytes."""
    import sqlite3
    root, url = server
    for rel in (".alpaca/proofs/op-001/t-003.evidence/10-record-snapshot",
                ".alpaca/proofs/op-001/t-003.evidence/chrome/Default/Cookies",
                "docs/Login Data"):
        Path(root, rel).parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(os.path.join(root, rel))
        con.execute("CREATE TABLE t (v TEXT)")
        con.execute("INSERT INTO t VALUES (?)", (SECRET_WORD,))
        con.commit()
        con.close()
        got = browse(url, "read", rel)
        assert "text" not in got and got.get("error") == "not a readable text file", (rel, got)
    put(root, "docs/plain", "%s\n" % HARMLESS)
    assert HARMLESS in browse(url, "read", "docs/plain")["text"]


@pytest.mark.parametrize("rel,private", (
    (".alpaca/files-auth", True), (".alpaca/web-session-key", True), (".alpaca/totp-secret", True),
    (".alpaca/alpaca.db", True), (".alpaca/alpaca.db-wal", True), (".alpaca/transcripts/s1.jsonl", True),
    (".alpaca/observability/state.json", True), (".alpaca/pool/tools/s1.jsonl", True), (".env", True),
    ("docs/01-files-auth", True), (".alpaca/proofs/op-001/t-003.evidence/05-env", True),
    (".alpaca/proofs/op-001/t-003.evidence/12-files-auth", True), ("docs/login", True),
    ("docs/readme.md", False), (".alpaca/proofs/op-001/t-001.md", False),
    (".alpaca/wiki/decisions/dec-000000000001.md", False), (".alpaca/research/rr-001/env/x.json", False),
    ("docs/env", False), (".claude/skills/shield/SKILL.md", False),
    (".alpaca/rune2-development/notes.md", False), (".alpaca/proofs/op-001/t-003.evidence/09-run.log", False),
))
def test_one_private_runtime_rule(tree, rel, private):
    """`paths.private_runtime` is the one rule the seal and the browser both apply."""
    Path(tree, "docs", "login").symlink_to("../.alpaca/files-auth")
    assert paths.private_runtime(tree, os.path.join(tree, rel)) is private


# --------------------------------------------------------------------------- R-3
def _flip_tree(root):
    """`docs/flip` is a real folder with a harmless note; `swap()` turns it into a link to the vault
    folder holding a note of the same name (probe P5's writer, one step)."""
    put(root, "docs/flip/000001-result.md", "# Flip\n\n%s\n" % HARMLESS)
    put(root, "docs/one.md", "# One\n\n%s\n" % HARMLESS)

    def swap():
        os.rename(os.path.join(root, "docs", "flip"), os.path.join(root, "docs", "flip.real"))
        os.symlink("../.alpaca/wiki/raw/op-8", os.path.join(root, "docs", "flip"))
        os.remove(os.path.join(root, "docs", "one.md"))
        os.symlink("../.alpaca/wiki/raw/op-8/000001-result.md", os.path.join(root, "docs", "one.md"))
    return swap


def swap_after_check(monkeypatch, root, rel, swap, nth=1):
    """Run `swap` right after the `nth` vault check of `rel` answers, so the reader's open comes
    after its checks and after the swap: the race of P5, hit on every run."""
    real_in_vault = paths.in_vault
    target = os.path.realpath(os.path.join(root, rel))
    state = {"swapped": False, "seen": 0}

    def hooked(root_dir, path):
        answer = real_in_vault(root_dir, path)
        if not state["swapped"] and os.path.realpath(os.fspath(path)) == target:
            state["seen"] += 1
            if state["seen"] == nth:
                state["swapped"] = True
                swap()
        return answer
    monkeypatch.setattr(paths, "in_vault", hooked)
    return state


@pytest.mark.parametrize("rel", ("docs/flip/000001-result.md", "docs/one.md"))
def test_r3_the_file_browser_rechecks_the_file_it_opened(server, monkeypatch, rel):
    root, url = server
    swap = _flip_tree(root)
    assert HARMLESS in browse(url, "read", rel)["text"]            # before the swap it reads
    state = swap_after_check(monkeypatch, root, rel, swap)
    status, body = fetch(url, "/files/read?path=" + urllib.parse.quote(rel, safe=""))
    assert state["swapped"]
    assert VAULT_WORD not in body and "text" not in json.loads(body), body


@pytest.mark.parametrize("rel", ("docs/flip/000001-result.md", "docs/one.md"))
def test_r3_the_raw_file_route_rechecks_the_file_it_opened(server, monkeypatch, rel):
    root, url = server
    swap = _flip_tree(root)
    status, body = fetch(url, "/file?path=" + urllib.parse.quote(rel, safe=""))
    assert status == 200 and HARMLESS in body
    state = swap_after_check(monkeypatch, root, rel, swap)
    status, body = fetch(url, "/file?path=" + urllib.parse.quote(rel, safe=""))
    assert state["swapped"]
    assert status == 404 and VAULT_WORD not in body, (status, body)


def test_r3_the_report_reader_rechecks_the_file_it_opened(server, monkeypatch):
    """`/hub/document.json` over a docs report: the reader checks the path, then finds it in the
    catalog walk (a second check); the swap lands after that last check, and the open must not
    follow it into the vault."""
    root, url = server
    swap = _flip_tree(root)
    rel = "docs/flip/000001-result.md"
    status, body = fetch(url, "/hub/document.json?path=" + urllib.parse.quote(rel, safe=""))
    assert status == 200 and HARMLESS in body
    state = swap_after_check(monkeypatch, root, rel, swap, nth=2)
    status, body = fetch(url, "/hub/document.json?path=" + urllib.parse.quote(rel, safe=""))
    assert state["swapped"]
    assert VAULT_WORD not in body and status in (400, 404), (status, body)


def test_r3_the_seal_copy_rechecks_the_file_it_opened(tree, monkeypatch):
    """The seal's copy is a reader too: resolved as a docs file, the evidence becomes a link into the
    vault before the copy (plain), or right after the copy's own vault check (injected). Neither way
    does vault text reach `.evidence/`."""
    swap = _flip_tree(tree)
    items = resolved(tree, ["local:docs/flip/000001-result.md"])
    swap()
    proof.keep_evidence(tree, subject(tree), items)
    assert items[0]["kept"] is None, items
    assert files_holding(tree, ".alpaca/proofs", [VAULT_WORD]) == []

    put(tree, "docs/two/000001-result.md", "# Two\n\n%s\n" % HARMLESS)

    def swap_two():
        os.rename(os.path.join(tree, "docs", "two"), os.path.join(tree, "docs", "two.real"))
        os.symlink("../.alpaca/wiki/raw/op-8", os.path.join(tree, "docs", "two"))
    items = resolved(tree, ["local:docs/two/000001-result.md"])
    state = swap_after_check(monkeypatch, tree, "docs/two/000001-result.md", swap_two)
    proof.keep_evidence(tree, subject(tree), items)
    assert state["swapped"]
    assert items[0]["kept"] is None, items
    assert files_holding(tree, ".alpaca/proofs", [VAULT_WORD]) == []


# --------------------------------------------------------------------------- the vault walk
def test_in_vault_stops_its_walk_at_the_project_root(tree, monkeypatch):
    """A path inside the root meets the vault before it meets the root, so the walk never needs a
    folder above the root (review minor M-1: the Library catalog paid for every one of them)."""
    above = []
    real_stat = os.stat
    top = os.path.realpath(tree)

    def counting(p, *a, **k):
        s = os.path.realpath(os.fspath(p)) if isinstance(p, (str, bytes, os.PathLike)) else ""
        if s and top.startswith(s.rstrip(os.sep) + os.sep):
            above.append(s)
        return real_stat(p, *a, **k)
    monkeypatch.setattr(os, "stat", counting)
    assert paths.in_vault(tree, os.path.join(tree, ".alpaca/proofs/op-001/t-001.md")) is False
    assert paths.in_vault(tree, os.path.join(tree, VAULT_NOTES[1])) is True
    assert paths.in_vault(tree, os.path.join(tree, ".alpaca/wiki/decisions/dec-000000000001.md")) is False
    assert above == []


# --------------------------------------------------------------------------- the browser's own rule
#: Review B-2 (op008/followups, `probe_privacy.py`): secret files no name list knew. The browser
#: closes each source (a dot folder or dot file, a closed `.alpaca` folder); the seal's slug dropped
#: the dot, so the copy `NN-netrc` was served. By the fix each is refused (a credential file by
#: name), bound by hash alone (a dot file, a file in a dot folder), or copied into the kept
#: folder's closed folder, which no reader serves (any other file the browser closes).
REFUSED = (".netrc", ".git-credentials", ".pgpass", ".claude/.credentials.json", ".ssh/id_ed25519_sk")
HASH_ONLY = (".npmrc", ".pypirc", ".envrc", ".git/config", ".claude/shell-snapshots/snapshot-bash-1.sh",
             ".alpaca/research/homes/vf-1/.claude/projects/p/s.jsonl", ".ssh/config")
CLOSED_COPY = (".alpaca/wiki-snapshot/documents/abc.json", ".alpaca/serve.log", ".alpaca/sealed-terms.txt",
               ".alpaca/audits/a/snapshot/wiki/raw/op-8/000001-result.md", "analytics/sessions.json",
               "docs/archive.tar.gz", ".alpaca/reports/research/rr-001/report.json")


@pytest.mark.parametrize("rel", REFUSED + HASH_ONLY + CLOSED_COPY)
def test_b2_a_seal_serves_no_copy_of_a_file_the_browser_closes(server, rel):
    """The seal copies an evidence file into the served `.evidence/` folder only when the browser
    would serve the source file itself (`paths.browser_serves`, the browser's own predicate). A
    file it closes is refused, bound by its hash alone, or kept in `.evidence/.closed/`; in every
    case no served file holds its text, and the check verifies it."""
    root, url = server
    put(root, rel, "token=%s\n" % SECRET_WORD)
    assert serve._files_resolve(root, rel) is None
    assert not paths.browser_serves(root, os.path.realpath(os.path.join(root, rel)))
    if rel in REFUSED:
        problems = seal_problems(root, ["local:%s - the file" % rel, "local:docs/readme.md - the docs"])
        assert any("private runtime file" in p for p in problems), problems
        assert not os.path.isdir(kept_dir(root))
        return
    payload = seal(root, ["local:%s - the file" % rel, "local:docs/readme.md - the docs"])
    item = {i["pointer"]: i for i in payload["evidence"]}["local:" + rel]
    assert item["verified"] is True and len(item["sha256"]) == 64, item
    closed = os.path.join(kept_dir(root), proof.CLOSED_KEPT)
    if rel in HASH_ONLY:
        assert item["kept"] is None and item["kept_reason"] == proof.HIDDEN_NOT_KEPT, item
        assert files_holding(root, ".alpaca/proofs", [SECRET_WORD]) == []
        assert not os.path.exists(closed)
    else:
        assert item["kept"] == os.path.relpath(os.path.join(closed, "01-" + os.path.basename(rel)),
                                               root), item
        assert serve._files_resolve(root, item["kept"]) is None
        assert files_holding(root, ".alpaca/proofs", [SECRET_WORD]) == [item["kept"]]
    assert sorted(n for n in os.listdir(kept_dir(root)) if n != proof.CLOSED_KEPT) == ["02-readme.md"]
    assert SECRET_WORD not in json.dumps(crawl(url, ".alpaca/proofs"))
    conn = db.connect(root)
    try:
        assert proof.verify_detail(conn, root, "t-001")["ok"] is True
        put(root, rel, "token=changed\n")
        got = proof.verify_detail(conn, root, "t-001")
    finally:
        conn.close()
    # a hash-only file is checked in place; a kept copy stands for its source as any copy does
    assert got["ok"] is (rel not in HASH_ONLY), got


@pytest.mark.parametrize("rel", [".netrc", ".git-credentials", ".pgpass", ".claude/.credentials.json"])
def test_b2_the_usual_credential_files_are_refused_outright(tree, rel):
    put(tree, rel, "token=%s\n" % SECRET_WORD)
    assert paths.private_runtime(tree, os.path.join(tree, rel))
    problems = seal_problems(tree, ["local:%s - x" % rel])
    assert any("private runtime file" in p for p in problems), problems


def test_b2_a_database_by_its_bytes_is_never_copied(server):
    """The browser refuses a SQLite file by its first bytes whatever its name; the seal binds one by
    hash and copies none (the copy would be a served name the reader then refuses, in a tracked
    folder that backups and exports carry)."""
    root, _url = server
    Path(root, "docs").mkdir(exist_ok=True)
    Path(root, "docs", "record-snapshot").write_bytes(serve.SQLITE_MAGIC + b"\x00" * 64 + SECRET_WORD.encode())
    payload = seal(root, ["local:docs/record-snapshot - x", "local:docs/readme.md - the docs"])
    item = {i["pointer"]: i for i in payload["evidence"]}["local:docs/record-snapshot"]
    assert item["kept"] is None and item["kept_reason"] == proof.HIDDEN_NOT_KEPT and item["sha256"]
    assert sorted(os.listdir(kept_dir(root))) == ["02-readme.md"]


def test_b2_a_seal_made_before_the_rule_still_verifies_from_its_copy(tree, monkeypatch):
    """Old seals are unchanged: a seal that kept a copy of a file the browser closes still verifies
    from that copy, and the copy is not removed by the check."""
    put(tree, ".alpaca/serve.log", "started on 7391\n")
    monkeypatch.setattr(proof, "_not_kept", lambda root, path: None)
    monkeypatch.setattr(proof, "_served_copy", lambda root, path: True)
    payload = seal(tree, ["local:.alpaca/serve.log - the log", "local:docs/readme.md - the docs"])
    kept = {i["pointer"]: i for i in payload["evidence"]}["local:.alpaca/serve.log"]["kept"]
    assert kept == ".alpaca/proofs/op-001/t-001.evidence/01-serve.log"
    monkeypatch.undo()
    put(tree, ".alpaca/serve.log", "started on 7392\n")
    conn = db.connect(tree)
    try:
        got = proof.verify_detail(conn, tree, "t-001")
    finally:
        conn.close()
    assert got["ok"] is True, got
    assert os.path.isfile(os.path.join(tree, kept))
