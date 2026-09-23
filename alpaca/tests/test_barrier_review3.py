"""Barrier review 3: each gap the independent probes found, pushed for real where a push is the claim.

The review (docs/alpaca-bootstrap/evidence/review3-barrier/FINDINGS.md in the workspace that builds
this repository) found content that reached a bare remote through the installed pre-push hook. Each
test here names the finding it covers (B1 to B15) and drives the same case: a real `git push` through
the hook when the claim is about what reaches the remote, the library scan otherwise.

Values the barrier must refuse are built at run time (AT, HOME), so this file, which is pushed
through the barrier itself, carries no shape the allow rules would have to clear.
"""
import bz2
import gzip
import io
import lzma
import os
import subprocess
import tarfile
import zipfile

import pytest
import yaml

from alpaca import barrier
from alpaca.gates import leak_audit
from alpaca.gates import verdict as vc
from alpaca.tests.conftest import REPO
from alpaca.tests.test_outbound_barrier import (  # noqa: F401  (fixtures used by name)
    TERM, _commit, _commits, _fixed_clock, _push_env, _run, repo)

AT = "@"
HOME = "/" + "home"
WORD = "Zqvox"              # a whole-word regex entry (`re:` line)
LEGACY = "Qwylk"            # a whole-word regex entry in the older `#re:` form
PREFIX = "/opt/zqprivate"   # a path-prefix regex entry
LIST = (TERM + "\n"
        "re: \\b" + WORD.lower() + "\\b\n"
        "#re: \\b" + LEGACY.lower() + "\\b\n"
        "re: " + PREFIX + "[a-z]*\n")


def _cfg(root, **extra):
    block = {"protected_paths": [".alpaca/", ".env", "secrets/"],
             "terms": ".alpaca/sealed-terms.txt"}
    block.update(extra)
    with open(os.path.join(root, "project.yaml"), "w", encoding="utf-8") as fh:
        yaml.safe_dump({"name": "work", "tier": "public", "barrier": block}, fh)


def _terms(root, text, name="sealed-terms.txt"):
    d = os.path.join(root, ".alpaca")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, name), "w", encoding="utf-8") as fh:
        fh.write(text)


def _push(root, *args, cwd=None):
    return subprocess.run(["git", "-C", cwd or root, "push", "origin", *args], capture_output=True,
                          text=True, encoding="utf-8", errors="replace", env=_push_env(root))


def _remote_has(bare, ref):
    return _run(bare, "rev-parse", "--verify", "--quiet", ref).returncode == 0


def _remote_sha(bare, ref):
    return _run(bare, "rev-parse", "--verify", "--quiet", ref).stdout.strip()


def _ready(root, bare, text=LIST, **extra):
    """Config, list, a clean first push (so each probe push carries only its own change), hook."""
    _cfg(root, **extra)
    _terms(root, text)
    barrier.install(root)
    r = _push(root, "main")
    assert r.returncode == 0, r.stdout + r.stderr
    return _remote_sha(bare, "main")


def _refused(root, bare, before, *refspec):
    r = _push(root, *(refspec or ("main",)))
    assert r.returncode != 0, r.stdout + r.stderr
    assert _remote_sha(bare, "main") == before
    return r


def _hash_object(root, data):
    r = subprocess.run(["git", "-C", root, "hash-object", "-w", "--stdin"], input=data,
                       capture_output=True)
    assert r.returncode == 0
    return r.stdout.decode().strip()


def _scan(root):
    return barrier.scan(root, _commits(root))


# ------------------------------------------------------------------ B1: regex lines of the list
@pytest.mark.parametrize("text", [
    "see " + WORD + " about it",
    "ask " + LEGACY.upper() + " first",
    "kept in " + PREFIX + "x/cache",
])
def test_b1_each_regex_line_refuses_a_real_push(repo, text):
    root, bare = repo
    before = _ready(root, bare)
    _commit(root, "notes/r.md", text + "\n", "add a note")
    _refused(root, bare, before)


def test_b1_a_regex_line_is_matched_as_written_whole_word_only(repo):
    root, _bare = repo
    _cfg(root)
    _terms(root, LIST)
    _commit(root, "notes/r.md", "the " + WORD + "en word is another word\n", "add a note")
    assert _scan(root).verdict == vc.PASS


def test_b1_a_regex_line_is_matched_in_commit_metadata(repo):
    root, bare = repo
    before = _ready(root, bare)
    _commit(root, "src/app.py", "print('ok')\n", "port the fix " + WORD + " made")
    _refused(root, bare, before)


def _witness(line):
    """A string that the list line must refuse, derived from the line itself."""
    s = line.strip()
    for prefix in ("#re:", "re:"):
        if s.startswith(prefix):
            body = s[len(prefix):].strip().replace("\\b", "")
            body = body.replace("[a-z]*", "x")
            return body.replace("\\", "")
    return s


def test_b1_every_line_of_the_list_refuses_a_push_carrying_it(repo):
    """Walks every non-blank, non-comment line of the list: a push carrying it alone is refused."""
    root, bare = repo
    before = _ready(root, bare)
    lines = [ln for ln in LIST.splitlines() if ln.strip() and not ln.startswith("# ")]
    assert len(lines) == 4
    for n, line in enumerate(lines):
        _commit(root, "notes/w%d.md" % n, "value: %s\n" % _witness(line), "add value %d" % n)
        _refused(root, bare, before)
        assert _run(root, "reset", "-q", "--hard", "HEAD~1").returncode == 0


@pytest.mark.parametrize("bad_line", [
    "re: ([unclosed",           # does not compile
    "re: z*",                   # matches the empty string, so it would match everything
    "#rx: \\bzqvox\\b",         # a directive the barrier does not know
    "!inclde: other.txt",       # a misspelled directive
])
def test_b1_a_line_the_barrier_cannot_use_refuses_the_push(repo, bad_line):
    root, _bare = repo
    _cfg(root)
    _terms(root, TERM + "\n" + bad_line + "\n")
    _commit(root, "src/app.py", "print('ok')\n", "add app")
    res = _scan(root)
    assert res.verdict == vc.BLOCKED and barrier.R_TERMS_INVALID in res.reasons


# ------------------------------------------------------------------ B2: objects that are not commits
def test_b2_lightweight_tag_on_a_blob_is_refused(repo):
    root, bare = repo
    before = _ready(root, bare)
    blob = _hash_object(root, ("notes on " + TERM + "\n").encode())
    assert _run(root, "tag", "light", blob).returncode == 0
    _refused(root, bare, before, "refs/tags/light")
    assert not _remote_has(bare, "refs/tags/light")


def test_b2_annotated_tag_on_a_tree_is_refused(repo):
    root, bare = repo
    before = _ready(root, bare)
    blob = _hash_object(root, ("notes on " + TERM + "\n").encode())
    tree = subprocess.run(["git", "-C", root, "mktree"], input="100644 blob %s\tn.md\n" % blob,
                          capture_output=True, text=True).stdout.strip()
    assert _run(root, "tag", "-a", "-m", "a tree", "treetag", tree).returncode == 0
    _refused(root, bare, before, "refs/tags/treetag")
    assert not _remote_has(bare, "refs/tags/treetag")


def test_b2_tag_of_a_tag_with_the_term_in_the_inner_message_is_refused(repo):
    root, bare = repo
    before = _ready(root, bare)
    assert _run(root, "tag", "-a", "-m", "notes on " + TERM, "inner", "HEAD").returncode == 0
    assert _run(root, "-c", "advice.nestedTag=false", "tag", "-a", "-m", "clean", "outer",
                "inner").returncode == 0
    assert _run(root, "tag", "-d", "inner").returncode == 0
    _refused(root, bare, before, "refs/tags/outer")
    assert not _remote_has(bare, "refs/tags/outer")


# ------------------------------------------------------------------ B3: gitlink names
def test_b3_gitlink_path_carrying_the_term_is_refused(repo):
    root, bare = repo
    before = _ready(root, bare)
    head = _run(root, "rev-parse", "HEAD").stdout.strip()
    assert _run(root, "update-index", "--add", "--cacheinfo",
                "160000,%s,vendor/%s" % (head, TERM.lower())).returncode == 0
    assert _run(root, "commit", "-q", "-m", "vendor a repo").returncode == 0
    _refused(root, bare, before)


# ------------------------------------------------------------------ B4: compressed containers
def _payload():
    return ("notes: " + TERM + "\n" + "".join("line %d of filler text\n" % i for i in range(300))
            ).encode()


def _tar(members):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for name, data in members:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _zip(members):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in members:
            zf.writestr(name, data)
    return buf.getvalue()


CONTAINERS = {
    "notes.gz": lambda: gzip.compress(_payload()),
    "pkg.tgz": lambda: gzip.compress(_tar([("package/notes.md", _payload())])),
    "notes.zip": lambda: _zip([("notes.md", _payload())]),
    "notes.xz": lambda: lzma.compress(_payload()),
    "notes.bz2": lambda: bz2.compress(_payload()),
    "names.tgz": lambda: gzip.compress(_tar([("package/" + TERM + ".md", b"clean\n")])),
    "nested.tgz": lambda: gzip.compress(_tar([("package/inner.zip",
                                               _zip([("n.md", _payload())]))])),
}


@pytest.mark.parametrize("name", sorted(CONTAINERS))
def test_b4_a_compressed_file_carrying_the_term_fails(repo, name):
    root, _bare = repo
    _cfg(root)
    _terms(root, LIST)
    data = CONTAINERS[name]()
    assert TERM.lower().encode() not in data.lower()      # the raw byte lane cannot see it
    p = os.path.join(root, "vendor", name)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as fh:
        fh.write(data)
    assert _run(root, "add", "-A").returncode == 0
    assert _run(root, "commit", "-q", "-m", "vendor " + name).returncode == 0
    res = _scan(root)
    assert res.verdict == vc.FAIL and barrier.R_LEAK in res.reasons, barrier.render(res.report)


def test_b4_a_real_push_of_a_tgz_carrying_the_term_is_refused(repo):
    root, bare = repo
    before = _ready(root, bare)
    p = os.path.join(root, "vendor", "pkg.tgz")
    os.makedirs(os.path.dirname(p))
    with open(p, "wb") as fh:
        fh.write(CONTAINERS["pkg.tgz"]())
    assert _run(root, "add", "-A").returncode == 0
    assert _run(root, "commit", "-q", "-m", "vendor pkg").returncode == 0
    _refused(root, bare, before)


def test_b4_a_clean_container_passes(repo):
    root, _bare = repo
    _cfg(root)
    _terms(root, LIST)
    p = os.path.join(root, "vendor", "clean.tgz")
    os.makedirs(os.path.dirname(p))
    with open(p, "wb") as fh:
        fh.write(gzip.compress(_tar([("package/a.md", b"plain words\n")])))
    assert _run(root, "add", "-A").returncode == 0
    assert _run(root, "commit", "-q", "-m", "vendor clean").returncode == 0
    res = _scan(root)
    assert res.verdict == vc.PASS, barrier.render(res.report)


def _unopenable(root):
    data = b"\x1f\x8b\x08\x00" + bytes(range(256)) * 4      # gzip magic, then no valid stream
    p = os.path.join(root, "vendor", "broken.gz")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as fh:
        fh.write(data)
    assert _run(root, "add", "-A").returncode == 0
    assert _run(root, "commit", "-q", "-m", "vendor broken").returncode == 0
    return _run(root, "rev-parse", "HEAD:vendor/broken.gz").stdout.strip()


def test_b4_a_container_the_barrier_cannot_open_is_refused(repo):
    root, _bare = repo
    _cfg(root)
    _terms(root, LIST)
    _unopenable(root)
    res = _scan(root)
    assert res.verdict == vc.BLOCKED and barrier.R_UNOPENED in res.reasons


def test_b4_an_allow_entry_naming_the_blob_digest_with_a_reason_clears_it(repo):
    root, _bare = repo
    _terms(root, LIST)
    _cfg(root)
    sha = _unopenable(root)
    _cfg(root, allow_blobs=["%s  # a truncated test archive, checked by hand" % sha])
    res = _scan(root)
    assert res.verdict == vc.PASS, barrier.render(res.report)
    _cfg(root, allow_blobs=[sha])                         # no reason: unusable, refuses
    res = _scan(root)
    assert res.verdict == vc.BLOCKED and barrier.R_TERMS_INVALID in res.reasons


# ------------------------------------------------------------------ B5: an absent list refuses
def test_b5_a_configured_list_that_is_absent_refuses(repo):
    root, _bare = repo
    _cfg(root, terms=".alpaca/not-supplied.txt")
    _commit(root, "notes.md", "the value is " + TERM + "\n", "add notes")
    res = _scan(root)
    assert res.verdict == vc.BLOCKED and barrier.R_TERMS_MISSING in res.reasons
    assert "NOT checked" in barrier.render(res.report)


def test_b5_a_dangling_list_link_refuses(repo):
    root, _bare = repo
    _cfg(root)
    os.makedirs(os.path.join(root, ".alpaca"), exist_ok=True)
    os.symlink(os.path.join(root, "no-such-dir", "forbidden.txt"),
               os.path.join(root, ".alpaca", "sealed-terms.txt"))
    _commit(root, "src/app.py", "print('ok')\n", "add app")
    res = _scan(root)
    assert res.verdict == vc.BLOCKED and barrier.R_TERMS_MISSING in res.reasons


def test_b5_the_opt_out_passes_but_still_runs_the_shape_rules(repo):
    root, _bare = repo
    _cfg(root, terms=".alpaca/not-supplied.txt", terms_optional=True)
    _commit(root, "src/app.py", "print('ok')\n", "add app")
    res = _scan(root)
    assert res.verdict == vc.PASS and barrier.R_TERMS_UNCHECKED in res.reasons
    assert "terms_optional" in barrier.render(res.report)
    _commit(root, "notes.md", "write to <jane.doe" + AT + "gmail.com> and " + HOME + "/jane/x\n",
            "add notes")
    res = _scan(root)
    assert res.verdict == vc.FAIL and barrier.R_LEAK in res.reasons


def test_b5_the_gate_line_says_the_terms_were_not_checked(repo):
    root, bare = repo
    _cfg(root, terms=".alpaca/not-supplied.txt", terms_optional=True)
    barrier.install(root)
    _commit(root, "src/app.py", "print('ok')\n", "add app")
    r = _push(root, "main")
    assert r.returncode == 0, r.stdout + r.stderr
    gate = [ln for ln in (r.stdout + r.stderr).splitlines() if ln.startswith("GATE outbound-barrier")]
    assert gate and "NOT checked" in gate[-1]


def test_b5_a_push_from_a_linked_worktree_uses_the_main_list(repo, tmp_path):
    root, bare = repo
    before = _ready(root, bare)
    wt = str(tmp_path / "wt")
    assert _run(root, "worktree", "add", "-q", "-b", "wtbranch", wt, "main").returncode == 0
    with open(os.path.join(wt, "notes.md"), "w", encoding="utf-8") as fh:
        fh.write("the value is " + TERM + "\n")
    assert _run(wt, "add", "-A").returncode == 0
    assert _run(wt, "commit", "-q", "-m", "from a worktree").returncode == 0
    r = _push(root, "wtbranch", cwd=wt)
    assert r.returncode != 0, r.stdout + r.stderr
    assert not _remote_has(bare, "refs/heads/wtbranch")
    assert _remote_sha(bare, "main") == before


# ------------------------------------------------------------------ B6: replaced and grafted history
def test_b6_a_replaced_commit_is_scanned_as_it_is_sent(repo):
    root, bare = repo
    before = _ready(root, bare)
    bad = _commit(root, "notes/rep.md", "the value is " + TERM + "\n", "carries the term")
    assert _run(root, "reset", "-q", "--hard", "HEAD~1").returncode == 0
    good = _commit(root, "notes/rep.md", "clean\n", "clean twin")
    assert _run(root, "reset", "-q", "--hard", bad).returncode == 0
    assert _run(root, "replace", bad, good).returncode == 0
    _refused(root, bare, before)


def test_b6_a_graft_that_hides_old_history_does_not_hide_it_from_the_barrier(repo, tmp_path):
    root, bare = repo
    _cfg(root)
    _terms(root, LIST)
    barrier.install(root)
    _commit(root, "notes/hist.md", "the value is " + TERM + "\n", "old history")
    _commit(root, "notes/hist.md", "clean\n", "cleaned")
    _commit(root, "notes/new.md", "clean\n", "first public commit")
    assert _run(root, "replace", "--graft", "HEAD").returncode == 0
    r = _push(root, "main")
    assert r.returncode != 0, r.stdout + r.stderr
    assert not _remote_has(bare, "refs/heads/main")


def test_b6_a_grafts_file_refuses(repo):
    root, _bare = repo
    _cfg(root)
    _terms(root, LIST)
    head = _commit(root, "src/app.py", "print('ok')\n", "add app")
    grafts = _run(root, "rev-parse", "--git-path", "info/grafts").stdout.strip()
    grafts = grafts if os.path.isabs(grafts) else os.path.join(root, grafts)
    os.makedirs(os.path.dirname(grafts), exist_ok=True)
    with open(grafts, "w", encoding="utf-8") as fh:
        fh.write(head + "\n")
    res = _scan(root)
    assert res.verdict == vc.BLOCKED and barrier.R_HISTORY in res.reasons


def test_b6_a_shallow_repository_refuses(repo, tmp_path):
    root, _bare = repo
    _commit(root, "src/app.py", "print('ok')\n", "add app")
    shallow = str(tmp_path / "shallow")
    assert subprocess.run(["git", "clone", "-q", "--depth", "1", "file://" + root, shallow],
                          capture_output=True).returncode == 0
    _cfg(shallow)
    _terms(shallow, LIST)
    res = barrier.scan(shallow, _commits(shallow))
    assert res.verdict == vc.BLOCKED and barrier.R_HISTORY in res.reasons


# ------------------------------------------------------------------ B7: more lists, identities
def test_b7_an_included_list_is_enforced(repo):
    root, bare = repo
    _terms(root, TERM + "\n", name="host-terms.txt")
    before = _ready(root, bare, text="ZqOtherTokenBeta\n!include: host-terms.txt\n")
    _commit(root, "notes.md", "the value is " + TERM + "\n", "add notes")
    _refused(root, bare, before)


def test_b7_an_included_list_that_is_absent_refuses(repo):
    root, _bare = repo
    _cfg(root)
    _terms(root, TERM + "\n!include: not-there.txt\n")
    _commit(root, "src/app.py", "print('ok')\n", "add app")
    res = _scan(root)
    assert res.verdict == vc.BLOCKED and barrier.R_TERMS_INVALID in res.reasons


def test_b7_a_real_mailbox_as_author_is_refused(repo):
    root, bare = repo
    before = _ready(root, bare)
    _run(root, "config", "user.email", "jane.doe.personal" + AT + "gmail.com")
    _commit(root, "src/app.py", "print('ok')\n", "add app")
    _refused(root, bare, before)


def test_b7_an_allow_rule_clears_the_chosen_public_identity(repo):
    root, _bare = repo
    _cfg(root, allow=[r"jane\.doe\.personal@gmail\.com  # the public identity the owner chose"])
    _terms(root, LIST)
    _run(root, "config", "user.email", "jane.doe.personal" + AT + "gmail.com")
    _commit(root, "src/app.py", "print('ok')\n", "add app")
    res = _scan(root)
    assert res.verdict == vc.PASS, barrier.render(res.report)


# ------------------------------------------------------------------ B8: the hook runs a pinned copy
def test_b8_the_hook_runs_the_copy_pinned_at_install(repo):
    root, _bare = repo
    _cfg(root)
    body = open(barrier.install(root), encoding="utf-8").read()
    assert "-m alpaca.barrier" not in body                 # never the checked-out package
    pin = barrier.pin_dir(root)
    assert os.path.isfile(os.path.join(pin, "run.py"))
    assert os.path.isfile(os.path.join(pin, "alpaca", "barrier.py"))
    assert os.path.isfile(os.path.join(pin, "STAMP.json"))


def test_b8_an_older_barrier_in_the_checkout_does_not_let_the_term_through(repo):
    root, bare = repo
    before = _ready(root, bare)
    # the checkout now carries a barrier that passes everything (an older commit, say)
    fake = os.path.join(root, "alpaca")
    os.makedirs(fake)
    with open(os.path.join(fake, "__init__.py"), "w") as fh:
        fh.write("")
    with open(os.path.join(fake, "barrier.py"), "w") as fh:
        fh.write("import sys\nsys.exit(0)\n")
    with open(os.path.join(root, ".git", "info", "exclude"), "a") as fh:
        fh.write("alpaca/\n")
    _commit(root, "notes.md", "the value is " + TERM + "\n", "add notes")
    _refused(root, bare, before)


def test_b8_a_checkout_config_without_terms_does_not_switch_the_terms_off(repo):
    root, bare = repo
    before = _ready(root, bare)
    with open(os.path.join(root, "project.yaml"), "w", encoding="utf-8") as fh:
        fh.write("name: work\ntier: public\n")             # an older project.yaml, no barrier block
    _commit(root, "notes.md", "the value is " + TERM + "\n", "add notes")
    r = _refused(root, bare, before)
    assert "differs" in (r.stdout + r.stderr)


def test_b8_a_pinned_copy_that_was_changed_refuses(repo):
    root, bare = repo
    before = _ready(root, bare)
    with open(os.path.join(barrier.pin_dir(root), "alpaca", "barrier.py"), "a") as fh:
        fh.write("\n# edited\n")
    _commit(root, "src/app.py", "print('ok')\n", "add app")
    _refused(root, bare, before)


# ------------------------------------------------------------------ B9: invisible marks, look-alikes
@pytest.mark.parametrize("variant", [
    TERM[:2] + "\u034f" + TERM[2:],                     # combining grapheme joiner (Mn)
    TERM[:2] + "\ufe0f" + TERM[2:],                     # variation selector (Mn)
    TERM.replace("o", "\u03bf", 1),                     # Greek small omicron
    TERM.replace("a", "\u03b1", 1),                     # Greek small alpha
    TERM.replace("p", "\u03c1", 1),                     # Greek small rho
    TERM.replace("S", "\u0405", 1),                     # Cyrillic capital dze
    TERM.replace("e", "\u0435\u0301", 1),               # Cyrillic ie plus a combining accent
])
def test_b9_invisible_marks_and_look_alikes_are_refused(repo, variant):
    root, _bare = repo
    _cfg(root)
    _terms(root, LIST)
    _commit(root, "notes.md", "the value is " + variant + "\n", "add notes")
    res = _scan(root)
    assert res.verdict == vc.FAIL and barrier.R_LEAK in res.reasons


# ------------------------------------------------------------------ B10: narrow template allow rules
def _template_rules():
    with open(os.path.join(REPO, "project.yaml"), encoding="utf-8") as fh:
        block = yaml.safe_load(fh)["barrier"]
    rules, reasons, _detail = leak_audit.parse_shape_allow(block.get("allow"), "template")
    assert not reasons
    return rules


@pytest.mark.parametrize("value, allowed", [
    ("jane.doe" + AT + "rebootsystems.com", False),
    ("jane.doe.personal" + AT + "pytest.fixtures", False),
    ("jane" + AT + "common.failover.io", False),
    ("andits@rebootweb-upscriptunder.alpaca", True),
    ("returnconn@pytest.fixturedefproj", True),
    ("sv.PROBE@pytest.mark.parametrize", True),
    ("raisereturnconn@contextlib.contextmanagerdeftransaction", True),
])
def test_b10_the_template_allow_rules_clear_only_the_glued_values(value, allowed):
    assert any(rx.fullmatch(value) for rx, _r, _n in _template_rules()) is allowed


# ------------------------------------------------------------------ B11: bytes that are not UTF-8
def test_b11_a_latin1_commit_message_is_scanned_not_refused(repo, tmp_path):
    root, _bare = repo
    _cfg(root)
    _terms(root, LIST)
    msg = tmp_path / "msg.txt"
    msg.write_bytes(b"caf\xe9 notes\n")
    _commit(root, "src/app.py", "print('ok')\n", "add app")
    open(os.path.join(root, "src", "b.py"), "w").write("x = 1\n")
    _run(root, "add", "-A")
    assert _run(root, "-c", "i18n.commitEncoding=ISO-8859-1", "commit", "-q", "-F",
                str(msg)).returncode == 0
    res = _scan(root)
    assert res.verdict == vc.PASS, barrier.render(res.report)
    msg.write_bytes(b"caf\xe9 " + TERM.encode() + b"\n")
    open(os.path.join(root, "src", "c.py"), "w").write("x = 2\n")
    _run(root, "add", "-A")
    assert _run(root, "-c", "i18n.commitEncoding=ISO-8859-1", "commit", "-q", "-F",
                str(msg)).returncode == 0
    res = _scan(root)
    assert res.verdict == vc.FAIL and barrier.R_LEAK in res.reasons


def test_b11_a_latin1_path_name_is_scanned_not_refused(repo):
    root, _bare = repo
    _cfg(root)
    _terms(root, LIST)
    blob = _hash_object(root, b"clean\n")
    r = subprocess.run([b"git", b"-C", root.encode(), b"update-index", b"--add", b"--cacheinfo",
                        b"100644," + blob.encode() + b",caf\xe9.md"], capture_output=True)
    assert r.returncode == 0
    assert _run(root, "commit", "-q", "-m", "latin-1 name").returncode == 0
    res = _scan(root)
    assert res.verdict == vc.PASS, barrier.render(res.report)


# ------------------------------------------------------------------ B12: the report names no term
def test_b12_a_short_term_is_reported_by_line_number_only(repo):
    root, _bare = repo
    _cfg(root)
    _terms(root, TERM + "\nQzx\n")
    _commit(root, "src/app.py", "print('ok')\n", "add app")
    res = _scan(root)
    assert res.verdict == vc.BLOCKED
    text = barrier.render(res.report)
    assert "Qzx" not in text and root not in text
    assert "line 2" in text


# ------------------------------------------------------------------ B13: protected path variants
@pytest.mark.parametrize("path", [".ALPACA/state.json", "sub/.env", ".env.local",
                                  "deep/.alpaca/alpaca.db", "app/.env.production.local"])
def test_b13_protected_path_variants_are_refused(repo, path):
    root, _bare = repo
    _cfg(root, protected_paths=[".alpaca/", ".env", ".env.local", ".env.*.local"])
    _terms(root, LIST)
    p = os.path.join(root, path)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as fh:
        fh.write("TOKEN=x\n")
    assert _run(root, "add", "-f", path).returncode == 0
    assert _run(root, "commit", "-q", "-m", "add a file").returncode == 0
    res = _scan(root)
    assert res.verdict == vc.BLOCKED and barrier.R_PROTECTED in res.reasons


def test_b13_an_env_template_is_not_a_protected_path(repo):
    root, _bare = repo
    _cfg(root, protected_paths=[".alpaca/", ".env", ".env.local", ".env.*.local"])
    _terms(root, LIST)
    _commit(root, ".env.example", "TOKEN=\n", "add the template")
    res = _scan(root)
    assert res.verdict == vc.PASS, barrier.render(res.report)


# ------------------------------------------------------------------ B14: large blobs are streamed
def test_b14_a_blob_over_the_text_cap_is_streamed_and_the_term_found(repo, monkeypatch):
    root, _bare = repo
    _cfg(root)
    _terms(root, LIST)
    monkeypatch.setattr(leak_audit, "TEXT_SCAN_MAX_BYTES", 4096)
    monkeypatch.setattr(leak_audit, "BYTE_CHUNK", 1000)
    body = "x" * 995 + TERM + "y" * 9000                # the term straddles the first chunk edge
    _commit(root, "big.txt", body, "add a big file")
    whole = []
    real = barrier._Objects.read
    monkeypatch.setattr(barrier._Objects, "read",
                        lambda self, sha: whole.append(sha) or real(self, sha))
    res = _scan(root)
    assert res.verdict == vc.FAIL and barrier.R_LEAK in res.reasons
    big = _run(root, "rev-parse", "HEAD:big.txt").stdout.strip()
    assert big not in whole                               # never read whole into memory


def test_b14_a_clean_blob_over_the_text_cap_needs_an_allow_entry(repo, monkeypatch):
    root, _bare = repo
    _cfg(root)
    _terms(root, LIST)
    monkeypatch.setattr(leak_audit, "TEXT_SCAN_MAX_BYTES", 4096)
    _commit(root, "big.txt", "plain words\n" * 1000, "add a big file")
    res = _scan(root)
    assert res.verdict == vc.BLOCKED and barrier.R_UNOPENED in res.reasons
    sha = _run(root, "rev-parse", "HEAD:big.txt").stdout.strip()
    _cfg(root, allow_blobs=["%s  # a large data file, read in full by hand" % sha])
    res = _scan(root)
    assert res.verdict == vc.PASS, barrier.render(res.report)


# ------------------------------------------------------------------ B15: a .venv link is ignored
def test_b15_gitignore_ignores_a_venv_link():
    with open(os.path.join(REPO, ".gitignore"), encoding="utf-8") as fh:
        lines = {ln.strip() for ln in fh}
    assert ".venv" in lines


def test_b15_a_venv_symlink_is_not_staged(tmp_path):
    import shutil
    shutil.copy(os.path.join(REPO, ".gitignore"), tmp_path / ".gitignore")
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    os.symlink(str(tmp_path), str(tmp_path / ".venv"))
    out = subprocess.run(["git", "-C", str(tmp_path), "status", "--porcelain", "--ignored"],
                         capture_output=True, text=True).stdout
    assert "!! .venv" in out
