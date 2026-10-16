"""The leak audit in attach mode (op-062 t-651; pre-mortem t-654.7 and plan 6.5).

In attach mode the audited tree is somebody else's clone. It is read by the files git tracks or would track,
so the clone's own .git folder and what its .gitignore keeps out do not decide the studio's verdict. A plain
project's digest and walk do not change.
"""
import hashlib
import os

from alpaca.attach import scan
from alpaca.gates import leak_audit
from alpaca.gates import verdict as vc
from alpaca.tests.attachkit import attached, git, studio, upstream, with_term_list  # noqa: F401

TERM = "zebra-sealed-term"
CLEAN = vc.PAUSED          # the mechanical audit alone cannot PASS: the blind-read attestation is absent


def test_sealed_terms_in_the_git_folder_and_in_ignored_output_do_not_fail_attach_mode(attached):
    with_term_list(attached, extra=[TERM])
    (attached.app / ".git" / "SEALED").write_text("the %s is in the git folder\n" % TERM)
    (attached.app / ".git" / "info").mkdir(exist_ok=True); (attached.app / ".git" / "info" / "exclude").write_text("build/\n")
    (attached.app / "build").mkdir()
    (attached.app / "build" / "x").write_text("the %s is in ignored build output\n" % TERM)
    root = str(attached.root)
    # a clean mechanical audit reaches PAUSED (no blind-read attestation), never FAIL
    assert leak_audit.check(root) == CLEAN
    # control: the folder walk (a plain project, or attach without the lister) meets the same files and fails
    tl, _r, _d = leak_audit.load_terms_from_project(root)
    walked = leak_audit.audit(str(attached.app), term_list=tl, require_control=False)
    assert walked.verdict in (vc.FAIL, vc.BLOCKED) and "LEAK-HITS" in walked.reasons


def test_a_tracked_file_and_a_new_unignored_file_still_fail_in_attach_mode(attached):
    with_term_list(attached, extra=[TERM])
    root = str(attached.root)
    (attached.app / "src" / "main.py").write_text("print('%s')\n" % TERM)
    assert leak_audit.check(root) == vc.FAIL
    git(attached.app, "checkout", "--", "src/main.py")
    assert leak_audit.check(root) == CLEAN
    (attached.app / "notes.txt").write_text("%s\n" % TERM)
    assert leak_audit.check(root) == vc.FAIL


def test_the_report_names_its_lister_and_the_digest_ignores_untracked_ignored_files(attached):
    with_term_list(attached, extra=[TERM])
    tl, _r, _d = leak_audit.load_terms_from_project(str(attached.root))
    app = str(attached.app)
    one = leak_audit.audit(app, term_list=tl, require_control=False, lister=scan.leak_lister)
    assert one.report["lister"] == "git-ls-files"
    (attached.app / ".git" / "info").mkdir(exist_ok=True); (attached.app / ".git" / "info" / "exclude").write_text("junk/\n")
    (attached.app / "junk").mkdir()
    (attached.app / "junk" / "a").write_text("noise\n")
    two = leak_audit.audit(app, term_list=tl, require_control=False, lister=scan.leak_lister)
    assert one.report["tree_digest"] == two.report["tree_digest"]
    assert leak_audit.tree_digest(app)[0] != leak_audit.tree_digest(app, scan.leak_lister)[0]


def test_the_lister_gives_sorted_pairs_and_skips_what_is_not_a_file(attached):
    (attached.app / "gone.txt").write_text("x\n")
    git(attached.app, "add", "gone.txt")
    (attached.app / "gone.txt").unlink()
    pairs = list(scan.leak_lister(attached.app))
    rels = [rel for _full, rel in pairs]
    assert rels == sorted(rels) and "src/main.py" in rels and "gone.txt" not in rels
    assert not any(rel.startswith(".git/") for rel in rels)
    assert all(full == os.path.join(str(attached.app), rel) for full, rel in pairs)


def test_a_plain_walk_and_a_plain_digest_are_unchanged(tmp_path):
    folder = tmp_path / "tree"
    (folder / "sub").mkdir(parents=True)
    (folder / "a.txt").write_bytes(b"alpha\n")
    (folder / "sub" / "b.txt").write_bytes(b"beta\n")
    expect = hashlib.sha256()
    for rel, data in (("a.txt", b"alpha\n"), ("sub/b.txt", b"beta\n")):
        expect.update(rel.encode() + b"\x00" + hashlib.sha256(data).hexdigest().encode() + b"\n")
    assert leak_audit.tree_digest(str(folder)) == (expect.hexdigest(), 2)
    assert leak_audit.tree_digest(str(folder), None) == (expect.hexdigest(), 2)
    assert [rel for _f, rel in leak_audit.iter_files(str(folder))] == ["a.txt", "sub/b.txt"]
    tl = leak_audit.TermList()
    tl.terms.append(("zzzz-term", "zzzz-term"))
    report = leak_audit.audit(str(folder), term_list=tl, require_control=False).report
    assert "lister" not in report
