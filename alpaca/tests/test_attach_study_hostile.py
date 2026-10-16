"""attach study under attack: the cases the adversarial lens of t-652 round 1 found (A1 to A14) and their siblings.

Each test asserts the SAFE behaviour. The classes (t-652.logs/investigate-r1.md):

  1  the check and the ingest read different text (A1, A2, A3): one read, one parse, an allow-list
  2  an out: pointer is not bound to the map's commit (A4, A5): the checker recomputes the output
  3  the checker trusts git objects that are not upstream history (A6, A7)
  4  regex work on a hostile map is unbounded (A8 and two more): a line cap and linear scans
  5  no lock, and --map reaches anywhere (A9 to A12)
  6  low: private names, a snapshot folder that is a link (A13, A14)
"""
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from alpaca import cli
from alpaca.attach import AttachError, config, study
from alpaca.tests.attachkit import attached, git, studio, upstream  # noqa: F401
from alpaca.tests.test_attach_study import (AREAS, BODY, MAP, map_text, prepared, upstream_edit,  # noqa: F401
                                            write_map)

OUT = ".alpaca/attach/study/outputs"
DEAD = "[ptr: app@src/nope.c:999]"


def wiki_blocks(root):
    """Every active block (text and heading context) under wiki/attach/map/: what the wiki serves."""
    from alpaca.wiki.config import Config
    from alpaca.wiki.ingest.absorb import Absorber
    if not Config.for_project(str(root)).db_path.exists():
        return []                                            # nothing was ever ingested
    a = Absorber(Config.for_project(str(root)))
    try:
        return a.db.conn.execute(
            "SELECT doc_id, coalesce(text, '') || ' | ' || coalesce(context_header, '') FROM blocks "
            "WHERE status='active' AND doc_id LIKE 'wiki/attach/map/%'").fetchall()
    finally:
        a.close()


def active_docs(root):
    return sorted({r[0] for r in wiki_blocks(root)})


def served(root):
    return " ".join(str(t) for _, t in wiki_blocks(root))


def with_area_lines(*lines):
    """BODY with `lines` (raw, joined by newlines) right after the heading of the src area."""
    return BODY.replace("## Area: src\n", "## Area: src\n" + "\n".join(lines) + "\n", 1)


# ======================================================== class 1: the check and the ingest read the same text

SHAPES = {
    "subheading-3": "### main() erases the flash at boot %s" % DEAD,
    "subheading-6": "###### main() erases the flash at boot %s" % DEAD,
    "comment-sandwich": "<!-- x --> main() erases the flash %s <!-- y -->" % DEAD,
    "comment-prefix": "<!-- x --> main() erases the flash %s" % DEAD,
    "comment-suffix": "main() erases the flash %s <!-- y -->" % DEAD,
    "plain": "main() erases the flash at boot %s" % DEAD,
    "indented-no-bullet": "    main() erases the flash at boot %s" % DEAD,
    "blockquote": "> main() erases the flash at boot %s" % DEAD,
    "table": "| main() erases | the flash %s |" % DEAD,
    "html-tag": "<p>main() erases the flash %s</p>" % DEAD,
    "fence": "```\nmain() erases the flash %s\n```" % DEAD,
    "setext": "main() erases the flash\n======= %s" % DEAD,
    "bare-hash": "#main() erases the flash %s" % DEAD,
    "bullet-no-space": "-main() erases the flash %s" % DEAD,
    "cr-joined-bullet": "- The program prints hello. [ptr: app@src/main.py:1]\r- main() erases the flash",
    "u2028-joined-bullet": "- The program prints hello. [ptr: app@src/main.py:1]\u2028- main() erases the flash",
    "vt-joined-bullet": "- The program prints hello. [ptr: app@src/main.py:1]\x0b- main() erases the flash",
    "nel-joined-bullet": "- The program prints hello. [ptr: app@src/main.py:1]\x85- main() erases the flash",
    "ff-joined-bullet": "- The program prints hello. [ptr: app@src/main.py:1]\x0c- main() erases the flash",
    "nul-in-bullet": "- main() erases the flash \x00 [ptr: app@src/main.py:1]",
    "unterminated-comment": "- main() erases the flash [ptr: app@src/main.py:1] <!-- never closed",
    "unterminated-pointer": "- main() erases the flash [ptr: app@src/main.py:1] [ptr: app@src/main.py:1",
    "dead-in-bullet": "- main() erases the flash %s" % DEAD,
    "dead-on-continuation": "- main() erases the flash\n  and more text %s" % DEAD,
    "dead-after-a-good-pointer": "- main() erases the flash [ptr: app@src/main.py:1] %s" % DEAD,
    "pointer-hidden-in-a-comment": "- main() erases the flash <!-- [ptr: app@src/main.py:1] -->",
}


@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_no_unchecked_line_passes_the_check_or_reaches_the_wiki(prepared, shape):
    """A1, A2 and their siblings: whatever shape a line takes, a claim on it with no good pointer fails the
    check and nothing of it is ingested."""
    att, info = prepared
    write_map(att, sha=info["sha"], body=with_area_lines(SHAPES[shape]))
    res = study.check_map(str(att.root))
    assert res.ok is False, "the shape %r passed the check" % shape
    with pytest.raises(AttachError, match="does not pass the check"):
        study.ingest(str(att.root))
    assert "erases the flash" not in served(att.root)
    assert active_docs(att.root) == []


def test_a_comment_line_holding_a_dead_pointer_is_a_comment_and_is_never_ingested(prepared):
    att, info = prepared
    write_map(att, sha=info["sha"], body=with_area_lines("<!-- main() erases the flash %s -->" % DEAD))
    assert study.check_map(str(att.root)).ok is True
    study.ingest(str(att.root))
    assert "erases the flash" not in served(att.root) and "nope.c" not in served(att.root)


def test_a_subheading_outside_an_area_is_not_a_claim_and_not_ingested(prepared):
    att, info = prepared
    body = "### Notes\n" + BODY
    write_map(att, sha=info["sha"], body=body)
    assert study.check_map(str(att.root)).ok is True
    study.ingest(str(att.root))
    assert "Notes" not in served(att.root)


def test_comments_around_a_good_bullet_are_removed_from_the_wiki_document(prepared):
    att, info = prepared
    body = with_area_lines("<!-- note to the scouts: keep this short -->",
                           "- The entry point is main(). <!-- verified -->  [ptr: app@src/main.py:1]")
    write_map(att, sha=info["sha"], body=body)
    ids = study.ingest(str(att.root))
    texts = served(att.root)
    assert "entry point is main()" in texts and "note to the scouts" not in texts and "verified" not in texts
    assert any(i.startswith("wiki/attach/map/src/") for i in ids)


def test_every_line_of_an_ingested_document_is_a_line_of_a_checked_bullet(prepared):
    """The invariant behind A1 to A3: what the wiki receives is a subset of what the parse checked."""
    att, info = prepared
    body = with_area_lines(
        "<!-- first comment -->", "",
        "- Parent claim one. [ptr: app@src/main.py:1]",
        "  continued on a second line <!-- aside --> with more words. [ptr: app@src/main.py:1]",
        "  - Nested claim. [ptr: app@src/main.py:1]",
        "1. Numbered claim. [ptr: app@src/main.py:1]",
        "* Star claim. [ptr: app@src/main.py:1]", "",
        "<!-- last comment -->")
    write_map(att, sha=info["sha"], body=body)
    analysis = study._analyze(str(att.root), (att.root / MAP).read_text())
    assert analysis.check.ok, analysis.check.problems
    allowed = {re.sub(r"<!--.*?-->", " ", line).rstrip() for line in body.split("\n")}
    assert analysis.docs
    for ident, doc in analysis.docs:
        text = doc.split("## Area: %s\n" % ident, 1)[1]
        for line in text.split("\n"):
            assert not line.strip() or line in allowed, "%r is not a checked bullet line" % line


def test_the_map_is_read_once_and_the_documents_come_from_that_read(prepared, monkeypatch):
    """A3: a writer that lands after the read cannot change what is ingested."""
    att, info = prepared
    write_map(att, sha=info["sha"])
    reads = []
    real_read = study._read_map
    real_analyze = study._analyze

    def counting_read(root, map_path):
        reads.append(map_path)
        return real_read(root, map_path)

    def analyze_then_swap(root, text):
        out = real_analyze(root, text)
        bad = BODY.replace("- The program prints hello. [ptr: app@src/main.py:1]",
                           "- main() erases the flash at boot. [ptr: app@src/nope.c:999]")
        write_map(att, sha=info["sha"], body=bad)
        return out
    monkeypatch.setattr(study, "_read_map", counting_read)
    monkeypatch.setattr(study, "_analyze", analyze_then_swap)
    study.ingest(str(att.root))
    assert len(reads) == 1
    assert "erases the flash" not in served(att.root) and "prints hello" in served(att.root)


def test_text_swapped_into_the_wiki_source_file_is_caught_and_retired(prepared, monkeypatch):
    """The window the adversarial lens did not name: the wiki reads its input from a file the studio wrote."""
    att, info = prepared
    write_map(att, sha=info["sha"])
    from alpaca.wiki import service
    real = service.ingest

    def swap_then_ingest(root, source, *, doc_id, session=None):
        Path(source).write_text("---\nvolatile: true\n---\n## Area: src\n\n- main() erases the flash at boot.\n",
                                encoding="utf-8")
        return real(root, source, doc_id=doc_id, session=session)
    monkeypatch.setattr(service, "ingest", swap_then_ingest)
    with pytest.raises(AttachError, match="other than the text that passed the check"):
        study.ingest(str(att.root))
    assert "erases the flash" not in served(att.root)


def test_the_skeleton_is_still_a_map_with_no_claims(prepared):
    """The skeleton's own comments (which hold `[ptr: ...]` text) are comments, not claims and not problems."""
    att, _ = prepared
    res = study.check_map(str(att.root))
    assert res.problems and all("no claims" in p for p in res.problems), res.problems


# ======================================================== class 2: out: pointers are bound to the map's commit

def test_out_pointer_past_the_end_at_the_maps_commit_stays_refused_after_a_later_prepare(attached):
    root = str(attached.root)
    a = study.prepare(root)["sha"]
    n = len((attached.root / OUT / a[:12] / "git-log.txt").read_text().splitlines()) + 1
    ptr = "out:%s/%s/git-log.txt:%d" % (OUT, a[:12], n)
    assert study.check_map_pointer(root, ptr, a).ok is False
    body = BODY + "- There are more commits than at A. [%s]\n" % ptr.replace("out:", "ptr: out:", 1)
    write_map(attached, sha=a, body=body)
    upstream_edit(attached.upstream, writes={"other.txt": "x\n"})
    study.prepare(root)                                      # the map stays at A, the log of B has one more line
    res = study.check_map(root)
    assert res.sha == a and res.ok is False


def test_a_fabricated_output_file_is_not_a_command_output(prepared):
    att, info = prepared
    d = att.root / OUT / info["sha12"]
    (d / "fake.txt").write_text("main() wipes the flash at boot\n")
    (att.root / OUT / "fake.txt").write_text("main() wipes the flash at boot\n")
    for rel in ("%s/fake.txt" % info["sha12"], "fake.txt"):
        assert study.check_map_pointer(str(att.root), "out:%s/%s:1" % (OUT, rel), info["sha"]).ok is False


def test_a_hand_edited_output_adds_no_line(prepared):
    att, info = prepared
    p = att.root / OUT / info["sha12"] / "git-log.txt"
    p.write_text(p.read_text() + "deadbee 2026-01-01 Mallory a commit that never happened\n")
    n = len(p.read_text().splitlines())
    assert study.check_map_pointer(str(att.root), "out:%s/%s/git-log.txt:%d" % (OUT, info["sha12"], n), info["sha"]).ok is False


def test_an_output_is_a_function_of_the_commit(prepared):
    """Same commit, same bytes: later commits, a .mailmap in the checkout and git config do not change it."""
    att, info = prepared
    app, sha = str(att.app), info["sha"]
    before = {n: study._output_bytes(app, sha, n) for n in study.OUTPUT_NAMES}
    upstream_edit(att.upstream, {"later.txt": "x\n"})
    study.prepare(str(att.root))
    (att.app / ".mailmap").write_text("Mallory <m@example.org> Ada Author <ada@example.org>\n")
    git(att.app, "config", "log.showSignature", "true")
    git(att.app, "config", "core.abbrev", "7")
    git(att.app, "config", "i18n.logOutputEncoding", "ISO-8859-1")
    after = {n: study._output_bytes(app, sha, n) for n in study.OUTPUT_NAMES}
    assert after == before
    assert before["git-log.txt"].split()[0].decode() == sha                   # the full id, never an abbreviation


def test_stored_outputs_are_the_recomputed_bytes_and_old_folders_are_pruned(prepared):
    att, info = prepared
    d = att.root / OUT / info["sha12"]
    for name in study.OUTPUT_NAMES:
        assert (d / name).read_bytes() == study._output_bytes(str(att.app), info["sha"], name)
    new = upstream_edit(att.upstream, {"later.txt": "x\n"})
    write_map(att, sha=new)                                  # the map moves to the new base, so the old folder goes
    study.prepare(str(att.root))
    assert not d.exists() and (att.root / OUT / new[:12] / "git-log.txt").is_file()


# ======================================================== class 3: upstream history only

def _fake_commit(att, sha, lines=50):
    """A commit made inside app/ with a src/main.py of `lines` lines, parented on nothing upstream."""
    app = str(att.app)
    blob = subprocess.run(["git", "-C", app, "hash-object", "-w", "--stdin"], input=b"line\n" * lines,
                          capture_output=True, check=True).stdout.decode().strip()
    sub = subprocess.run(["git", "-C", app, "mktree"], input=("100644 blob %s\tmain.py\n" % blob).encode(),
                         capture_output=True, check=True).stdout.decode().strip()
    top = [l for l in git(app, "ls-tree", sha).splitlines() if not l.endswith("\tsrc")] + ["040000 tree %s\tsrc" % sub]
    tree = subprocess.run(["git", "-C", app, "mktree"], input=("\n".join(top) + "\n").encode(),
                          capture_output=True, check=True).stdout.decode().strip()
    env = dict(os.environ, GIT_AUTHOR_NAME="m", GIT_AUTHOR_EMAIL="m@example.org",
               GIT_COMMITTER_NAME="m", GIT_COMMITTER_EMAIL="m@example.org")
    return subprocess.run(["git", "-C", app, "commit-tree", tree, "-m", "fake"], env=env,
                          capture_output=True, check=True).stdout.decode().strip()


def test_a_commit_made_inside_the_clone_is_not_upstream_history(prepared):
    att, info = prepared
    fake = _fake_commit(att, info["sha"])
    body = BODY.replace("[ptr: app@src/main.py:1]", "[ptr: app@src/main.py:50]")
    write_map(att, sha=fake, body=body)
    res = study.check_map(str(att.root))
    assert res.ok is False and any("not a base that alpaca attach study recorded" in p for p in res.problems), res.problems
    assert study.check_map_pointer(str(att.root), "app@src/main.py:50", fake).ok is False


def test_a_local_branch_commit_is_not_upstream_history_either(prepared):
    att, info = prepared
    git(att.app, "checkout", "-q", "-b", "scratch")
    (att.app / "src/main.py").write_text("line\n" * 50)
    git(att.app, "add", "-A")
    git(att.app, "-c", "user.name=m", "-c", "user.email=m@example.org", "commit", "-q", "-m", "local")
    local = git(att.app, "rev-parse", "HEAD")
    assert study.check_map_pointer(str(att.root), "app@src/main.py:50", local).ok is False


def test_only_a_recorded_base_is_accepted(prepared):
    """The ancestors of a recorded base are not accepted any more: ancestry was read from the clone's own
    commits (R2-02 family), the record of prepared bases is not."""
    att, info = prepared
    older = att.upstream.sha("main~1")
    git(att.app, "fetch", "-q", "origin")
    assert study.check_map_pointer(str(att.root), "app@src/main.py:1", info["sha"]).ok is True
    res = study.check_map_pointer(str(att.root), "app\x40README.md:1", older)
    assert res.ok is False and "not a base that alpaca attach study recorded" in res.reason
    newer = upstream_edit(att.upstream, {"src/new.py": "x = 1\n"})
    git(att.app, "fetch", "-q", "origin")
    assert study.check_map_pointer(str(att.root), "app@src/new.py:1", newer).ok is False   # fetched, not yet prepared
    study.prepare(str(att.root))
    assert study.check_map_pointer(str(att.root), "app@src/new.py:1", newer).ok is True


def test_a_replace_ref_in_the_clone_does_not_change_what_a_pointer_reads(prepared):
    att, info = prepared
    app = str(att.app)
    real = git(app, "rev-parse", "%s:src/main.py" % info["sha"])
    fake = subprocess.run(["git", "-C", app, "hash-object", "-w", "--stdin"], input=b"line\n" * 50,
                          capture_output=True, check=True).stdout.decode().strip()
    git(app, "replace", real, fake)
    assert git(app, "cat-file", "-p", "%s:src/main.py" % info["sha"]).count("line") == 50     # plain git is fooled
    assert study.check_map_pointer(str(att.root), "app@src/main.py:50", info["sha"]).ok is False
    assert study.check_map_pointer(str(att.root), "app@src/main.py:1", info["sha"]).ok is True


def test_a_replace_ref_does_not_change_the_snapshot_or_the_outputs(attached):
    app = str(attached.app)
    first = study.prepare(str(attached.root))
    real = git(app, "rev-parse", "%s:src/main.py" % first["sha"])
    fake = subprocess.run(["git", "-C", app, "hash-object", "-w", "--stdin"], input=b"line\n" * 50,
                          capture_output=True, check=True).stdout.decode().strip()
    git(app, "replace", real, fake)
    snap = attached.root / first["snapshot"]
    for dirpath, dirs, files in os.walk(snap):
        os.chmod(dirpath, 0o755)
    shutil.rmtree(snap)
    (attached.root / (first["snapshot"] + ".json")).unlink()
    again = study.prepare(str(attached.root))
    assert (attached.root / again["snapshot"] / "src/main.py").read_text() == "print('hello')\n"


def test_a_graft_file_in_the_clone_cannot_make_a_forged_commit_upstream_history(prepared):
    """`--no-replace-objects` alone leaves .git/info/grafts in force (probe-fix-r1.txt): a graft that gives the
    recorded base the forged commit as a parent would make it an ancestor."""
    att, info = prepared
    fake = _fake_commit(att, info["sha"])
    (att.app / ".git/info").mkdir(exist_ok=True)
    (att.app / ".git/info/grafts").write_text("%s %s %s\n" % (info["sha"], fake, att.upstream.sha("main~1")))
    assert git(att.app, "-c", "advice.graftFileDeprecated=false", "merge-base", "--is-ancestor", fake, info["sha"]) == ""
    res = study.check_map_pointer(str(att.root), "app@src/main.py:50", fake)
    assert res.ok is False and "not a base that alpaca attach study recorded" in res.reason


def test_a_graft_file_does_not_change_the_outputs(prepared):
    att, info = prepared
    before = {n: study._output_bytes(str(att.app), info["sha"], n) for n in study.OUTPUT_NAMES}
    (att.app / ".git/info").mkdir(exist_ok=True)
    (att.app / ".git/info/grafts").write_text("%s\n" % info["sha"])             # cuts the history to one commit
    assert git(att.app, "-c", "advice.graftFileDeprecated=false", "log", "--format=%H", info["sha"]).count("\n") == 0
    assert {n: study._output_bytes(str(att.app), info["sha"], n) for n in study.OUTPUT_NAMES} == before


# ======================================================== class 4: bounded work on a hostile map

BUDGET = 3.0


def _timed(fn):
    t0 = time.monotonic()
    out = fn()
    return time.monotonic() - t0, out


def test_a_long_pointer_line_is_refused_fast(prepared):
    att, info = prepared
    body = BODY.replace("- The test target echoes test. [ptr: app@Makefile:4-5]",
                        "- The test target echoes test. [ptr: app@Makefile:4-5] [ptr:" + " " * 5000 + "x")
    write_map(att, sha=info["sha"], body=body)
    took, res = _timed(lambda: study.check_map(str(att.root)))
    assert res.ok is False and any("longer than" in p for p in res.problems) and took < BUDGET, took


@pytest.mark.parametrize("name, line", [
    ("spaces-in-pointer", "- x [ptr:" + " " * 3900),
    ("pointers-never-closed", "- x " + "[ptr:" * 780),
    ("comments-never-closed", "- x " + "<!--" * 990),
    ("comments-closed-late", "- x " + "<!--" * 600 + "-->" * 3),
    ("area-heading-spaces", "## Area: x" + " " * 3900 + "y"),
    ("bullet-spaces", " " * 3900 + "x"),
    ("many-brackets", "- x " + "]" * 3900),
    ("many-colons", "- x [ptr: " + "app@a:1" * 500 + "]"),
])
def test_a_line_just_under_the_cap_costs_linear_time(prepared, name, line):
    att, info = prepared
    write_map(att, sha=info["sha"], body=with_area_lines(line))
    took, res = _timed(lambda: study.check_map(str(att.root)))
    assert res.ok is False and took < BUDGET, (name, took)


def test_a_two_mebibyte_map_of_long_lines_is_checked_in_bounded_time(prepared):
    att, info = prepared
    line = "- x [ptr:" + " " * 3980 + "\n"
    big = BODY + line * (study.MAX_MAP_BYTES // len(line) - 20)
    assert len(big) < study.MAX_MAP_BYTES
    write_map(att, sha=info["sha"], body=big)
    took, res = _timed(lambda: study.check_map(str(att.root)))
    assert res.ok is False and took < 15, took


def test_a_map_with_too_many_lines_is_refused_without_a_parse(prepared):
    att, info = prepared
    write_map(att, sha=info["sha"], body=BODY + "\n" * (study.MAX_LINES + 5))
    took, res = _timed(lambda: study.check_map(str(att.root)))
    assert res.ok is False and "lines; the limit is" in res.problems[0] and took < BUDGET


def test_a_yaml_alias_bomb_in_the_front_matter_is_refused_fast(prepared):
    att, info = prepared
    bomb = "x: &a [1,1,1,1,1,1,1,1,1]\n"
    prev = "a"
    for i in range(9):
        name = "b%d" % i
        bomb += "%s: &%s [%s]\n" % (name, name, ",".join("*" + prev for _ in range(9)))
        prev = name
    text = '---\nattach_sha: "%s"\n%sareas:\n  - id: src\n    paths: ["src/"]\n---\n' % (info["sha"], bomb) + BODY
    write_map(att, text=text)
    took, res = _timed(lambda: study.check_map(str(att.root)))
    assert res.ok is False and any("alias" in p for p in res.problems) and took < BUDGET, (took, res.problems[:2])


def test_a_deeply_nested_front_matter_is_a_problem_not_a_crash(prepared):
    att, info = prepared
    text = '---\nattach_sha: "%s"\nareas: %s\n---\n' % (info["sha"], "[" * 20000 + "]" * 20000) + BODY
    write_map(att, text=text)
    res = study.check_map(str(att.root))
    assert res.ok is False


def test_many_dead_pointers_stop_at_the_problem_limit(prepared):
    att, info = prepared
    rows = "".join("- claim number %d here. [ptr: app@d%d/f.c:1]\n" % (i, i) for i in range(3000))
    write_map(att, sha=info["sha"], body=BODY + rows)
    took, res = _timed(lambda: study.check_map(str(att.root)))
    assert res.ok is False and len(res.problems) <= study.MAX_PROBLEMS + 1 and took < 30, took


# ======================================================== class 5: the lock and --map

def test_two_ingests_at_once_leave_one_active_version(prepared):
    att, info = prepared
    root = str(att.root)
    wt = str(Path(study.__file__).resolve().parents[2])
    write_map(att, sha=info["sha"])
    study.ingest(root)
    for attempt in range(3):
        procs, go = [], time.time() + 2.0
        for tag in ("a", "b"):
            body = BODY.replace("The program prints hello.", "The program prints hello, run %d %s." % (attempt, tag))
            rel = "design/attach/map-%s.md" % tag
            (att.root / rel).write_text(map_text(info["sha"], body), encoding="utf-8")
            code = ("import sys, time; sys.path.insert(0, %r); from alpaca.attach import study; "
                    "time.sleep(max(0, %r - time.time())); study.ingest(%r, %r)" % (wt, go, root, rel))
            procs.append(subprocess.Popen([sys.executable, "-c", code], cwd=root, stdout=subprocess.PIPE,
                                          stderr=subprocess.PIPE))
        errs = []
        for p in procs:
            _, err = p.communicate(timeout=240)
            if p.returncode:
                errs.append(err.decode()[-300:])
        src = [d for d in active_docs(att.root) if d.startswith("wiki/attach/map/src/")]
        assert len(src) == 1 and not errs, (attempt, src, errs[:1])


def _hold_the_lock(root):
    import fcntl
    from alpaca.attach import STATE_DIR, safefs
    fd = safefs.open_lock(root, STATE_DIR + "/study.lock")
    fcntl.flock(fd, fcntl.LOCK_EX)
    return fd


@pytest.mark.parametrize("which", ["ingest", "flag_stale", "prepare"])
def test_ingest_flag_stale_and_prepare_wait_for_the_study_lock(prepared, which):
    att, info = prepared
    root = str(att.root)
    write_map(att, sha=info["sha"])
    calls = {"ingest": lambda: study.ingest(root), "flag_stale": lambda: study.flag_stale(root),
             "prepare": lambda: study.prepare(root, fetch=False)}
    fd = _hold_the_lock(root)
    done = []
    t = threading.Thread(target=lambda: done.append(calls[which]()))
    t.start()
    t.join(1.0)
    waiting = t.is_alive() and not done
    os.close(fd)
    t.join(120)
    assert waiting, "%s did not wait for the study lock" % which
    assert done and not t.is_alive()


@pytest.mark.parametrize("target", ["app/map.md", "app/src/main.py", "contracts/map.md", "doctrine/map.md",
                                    "project.yaml", ".git/config", ".alpaca/alpaca.db", "/etc/passwd",
                                    "../outside.md", "design/../project.yaml", "design/x.txt", "design", "design/.md",
                                    "Design/map.md", "design//map.md"])
def test_the_map_option_names_only_a_md_file_under_design(attached, capsys, target):
    root = attached.root
    before_app = git(attached.app, "status", "--porcelain", "--ignored")
    before_yaml = (root / "project.yaml").read_bytes()
    existed = (root / target).exists()
    for action in ("prepare", "check", "ingest", "stale"):
        rc = cli.main(["attach", "study", action, "--map", target])
        out = capsys.readouterr().out
        assert rc != 0 and "GATE alpaca-attach-study: FAIL" in out, (action, rc, out)
    assert git(attached.app, "status", "--porcelain", "--ignored") == before_app
    assert (root / "project.yaml").read_bytes() == before_yaml
    assert existed or not (root / target).exists()
    assert not (root.parent / "outside.md").exists()


def test_the_map_option_accepts_another_md_file_under_design(prepared, capsys):
    att, info = prepared
    rel = "design/attach/second-map.md"
    (att.root / rel).write_text(map_text(info["sha"]), encoding="utf-8")
    rc = cli.main(["attach", "study", "check", "--map", rel])
    assert rc == 0 and "4 claims" in capsys.readouterr().out


def test_prepare_does_not_write_a_skeleton_where_the_map_option_points_at_the_repository(attached, capsys):
    rc = cli.main(["attach", "study", "prepare", "--map", "app/map.md"])
    capsys.readouterr()
    assert rc != 0 and not (attached.app / "map.md").exists()
    assert git(attached.app, "status", "--porcelain") == ""


@pytest.mark.parametrize("text", ["# not a map\n\nplain text\n", "---\nonly: front\n---\nbody\n", ""])
def test_prepare_fails_when_the_named_map_has_no_usable_front_matter(prepared, capsys, text):
    att, _ = prepared
    (att.root / MAP).write_text(text, encoding="utf-8")
    rc = cli.main(["attach", "study", "prepare"])
    out = capsys.readouterr().out
    assert rc != 0 and "GATE alpaca-attach-study: FAIL" in out and "no usable front matter" in out


# ======================================================== class 6: low

@pytest.mark.parametrize("name", [".npmrc", "secrets.json", "secret.yml", "certs/a.p12", "certs/a.pfx", "keys/a.jks",
                                  "a/.pypirc", ".netrc", ".ssh/config", "x/.aws/credentials", "credentials.json"])
def test_more_private_names_are_refused(attached, name):
    sha = upstream_edit(attached.upstream, writes={name: "KEY=abc\n"})
    study.prepare(str(attached.root))
    res = study.check_map_pointer(str(attached.root), "app@%s:1" % name, sha)
    assert res.ok is False and "private" in res.reason, res.reason


def test_a_snapshot_folder_that_is_a_link_is_not_reused(prepared):
    att, info = prepared
    snap = att.root / info["snapshot"]
    for dirpath, dirs, files in os.walk(snap):
        os.chmod(dirpath, 0o755)
        for f in files:
            os.chmod(os.path.join(dirpath, f), 0o644)
    shutil.rmtree(snap)
    os.symlink(att.app, snap)
    again = study.prepare(str(att.root))
    folder = att.root / info["snapshot"]
    assert again["reused"] is False and folder.is_dir() and not folder.is_symlink()
    assert (folder / "src/main.py").read_text() == "print('hello')\n"
    assert git(att.app, "status", "--porcelain", "--ignored") == ""
