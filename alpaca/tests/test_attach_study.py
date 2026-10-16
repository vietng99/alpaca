"""attach study: the codebase map, its pointer checker, wiki ingest and the stale flag (R13, AC-14, build plan 6.6).

The pre-mortem attacks of t-652 (PM t-652.1 to t-652.6) are named in the tests that cover them.
"""
import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

from alpaca import cli, db
from alpaca.attach import AttachError, config, health, study
from alpaca.tests.attachkit import (DEFAULT_FILES, attached, git, make_studio, make_upstream,  # noqa: F401
                                    studio, upstream)

MAP = "design/attach/codebase-map.md"
AREAS = [("src", ["src/"]), ("docs", ["README.md", "CHANGELOG.md"]), ("build", ["Makefile"])]
BODY = """## Area: src
- The program prints hello. [ptr: app@src/main.py:1]
## Area: docs
- The changelog starts with the word first. [ptr: app\x40CHANGELOG.md:1]
- The readme names the demo app. [ptr: app\x40README.md:1]
## Area: build
- The test target echoes test. [ptr: app@Makefile:4-5]
"""


def events(root, kind="attach-study"):
    return [e for e in db.events(db.connect(str(root))) if e["kind"] == kind]


def map_text(sha, body=BODY, areas=AREAS):
    head = ['---', 'attach_sha: "%s"' % sha, 'areas:']
    for ident, paths in areas:
        head += ["  - id: %s" % ident, "    paths: [%s]" % ", ".join(json.dumps(p) for p in paths)]
    head.append("---")
    return "\n".join(head) + "\n# Codebase map\n\n" + body


def write_map(att, sha=None, body=BODY, areas=AREAS, text=None):
    sha = sha or att.upstream.sha()
    path = att.root / MAP
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text if text is not None else map_text(sha, body, areas), encoding="utf-8")
    return sha


def upstream_edit(up, writes=None, links=None, removes=(), moves=(), message="edit", gitlinks=None):
    """A commit on the upstream: files (text or bytes), symlinks {name: target}, removals, renames."""
    if up.work.joinpath(".git").exists() and git(up.work, "symbolic-ref", "--short", "HEAD") != up.branch:
        git(up.work, "checkout", "-q", up.branch)
    for rel, data in (writes or {}).items():
        target = up.work / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(data, bytes):
            target.write_bytes(data)
        else:
            target.write_text(data, encoding="utf-8")
    for name, dest in (links or {}).items():
        link = up.work / name
        link.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(dest, link)
    for rel in removes:
        git(up.work, "rm", "-q", "-r", rel)
    for old, new in moves:
        (up.work / new).parent.mkdir(parents=True, exist_ok=True)
        git(up.work, "mv", old, new)
    git(up.work, "add", "-A")
    for name, sha in (gitlinks or {}).items():
        git(up.work, "update-index", "--add", "--cacheinfo", "160000,%s,%s" % (sha, name))
    git(up.work, "commit", "-q", "-m", message)
    git(up.work, "push", "-q", "origin", up.branch)
    return git(up.work, "rev-parse", "HEAD")


@pytest.fixture
def prepared(attached):
    info = study.prepare(str(attached.root))
    return attached, info


# ------------------------------------------------------------------------------------ prepare

def test_prepare_exports_a_read_only_snapshot_at_the_base_sha(prepared):
    att, info = prepared
    sha = att.upstream.sha()
    snap = att.root / ".alpaca/attach/study" / sha[:12]
    assert info["sha"] == sha and info["snapshot"] == ".alpaca/attach/study/%s" % sha[:12]
    assert (snap / "src/main.py").read_text() == "print('hello')\n"
    assert stat.S_IMODE((snap / "src/main.py").stat().st_mode) == 0o444
    assert not (snap / ".git").exists()
    assert info["files"] == len(DEFAULT_FILES) + 1               # the three files plus CHANGELOG.md
    # nothing was written into the attached repository
    assert git(att.app, "status", "--porcelain", "--ignored") == ""


def test_prepare_stores_command_outputs_and_a_map_skeleton(prepared):
    att, info = prepared
    out = att.root / ".alpaca/attach/study/outputs" / info["sha12"]
    log = (out / "git-log.txt").read_text().splitlines()
    assert len(log) == 2 and "second commit" in log[0] and "Bo Builder" in log[0]
    assert log[0].split()[0] == att.upstream.sha()
    assert "Ada Author" in (out / "shortlog.txt").read_text()
    assert "src/main.py" in (out / "files.txt").read_text().splitlines()
    assert info["map"]["created"] is True
    text = (att.root / MAP).read_text()
    assert 'attach_sha: "%s"' % att.upstream.sha() in text and "## Area: src" in text
    # a skeleton is not a map: it has no claims
    res = study.check_map(str(att.root))
    assert res.ok is False and any("no claims" in p for p in res.problems)


def test_prepare_twice_keeps_the_snapshot_and_the_map(prepared):
    att, info = prepared
    (att.root / MAP).write_text(map_text(att.upstream.sha()), encoding="utf-8")
    again = study.prepare(str(att.root))
    assert again["reused"] is True and again["map"]["created"] is False
    assert (att.root / MAP).read_text() == map_text(att.upstream.sha())


def test_prepare_records_an_attach_study_event(prepared):
    att, info = prepared
    ev = events(att.root)[-1]
    assert ev["op"] == "op-0" and ev["data"]["action"] == "prepare" and ev["data"]["sha"] == info["sha"]


def test_prepare_fetches_the_new_base_and_prunes_old_snapshots(prepared):
    att, first = prepared
    new = upstream_edit(att.upstream, {"src/new.py": "x = 1\n"})
    second = study.prepare(str(att.root))
    assert second["sha"] == new and (att.root / ".alpaca/attach/study" / new[:12] / "src/new.py").is_file()
    # the map the skeleton wrote names the first commit, so that snapshot is kept for its pointers
    assert (att.root / ".alpaca/attach/study" / first["sha12"]).is_dir()
    write_map(att, sha=new)
    third = study.prepare(str(att.root))
    assert first["sha12"] in third["pruned"] and not (att.root / ".alpaca/attach/study" / first["sha12"]).exists()


def test_prepare_without_fetch_stays_on_the_fetched_base(prepared):
    att, first = prepared
    upstream_edit(att.upstream, {"src/new.py": "x = 1\n"})
    again = study.prepare(str(att.root), fetch=False)
    assert again["sha"] == first["sha"]


def test_the_snapshot_skips_symlinks_and_submodules_and_says_so(attached):
    other = git(attached.upstream.work, "rev-parse", "HEAD")
    upstream_edit(attached.upstream, links={"link-to-studio": "../.alpaca/alpaca.db"}, gitlinks={"vendor/dep": other})
    info = study.prepare(str(attached.root))
    snap = attached.root / ".alpaca/attach/study" / info["sha12"]
    assert not (snap / "link-to-studio").exists() and not (snap / "vendor").exists()
    assert info["skipped"] == 2


def test_prepare_refuses_a_studio_that_is_not_attached(studio):
    with pytest.raises(AttachError, match="not attached"):
        study.prepare(str(studio))


def test_the_study_folder_as_a_link_is_refused_and_nothing_lands_outside(attached, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    state = attached.root / ".alpaca/attach"
    os.symlink(outside, state / "study")
    with pytest.raises(AttachError, match="link"):
        study.prepare(str(attached.root))
    assert list(outside.iterdir()) == []


def test_the_map_folder_as_a_link_is_refused(attached, tmp_path):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (attached.root / "design").mkdir()
    os.symlink(outside, attached.root / "design/attach")
    with pytest.raises(AttachError, match="link"):
        study.prepare(str(attached.root))
    assert list(outside.iterdir()) == []


# ------------------------------------------------------------------------------ the pointer table

def good(att, pointer):
    return study.check_map_pointer(str(att.root), pointer, att.upstream.sha())


@pytest.mark.parametrize("pointer", [
    "app@src/main.py:1",
    "app@Makefile:2-4",
    "[ptr: app\x40README.md:1]",
])
def test_pointers_that_resolve(prepared, pointer):
    att, _ = prepared
    res = good(att, pointer)
    assert res.ok is True, res.reason


@pytest.mark.parametrize("pointer, why", [
    ("app@../.alpaca/alpaca.db:1", "'..'"),                 # PM t-652.1: reaches the studio record
    ("app/../.alpaca/alpaca.db:1", "pointer is"),           # the old spelling has no app@
    ("app@/etc/passwd:1", "relative path"),
    ("/etc/passwd:1", "pointer is"),
    ("app@src/main.py:999", "pointer names line"),
    ("app@src/main.py:0", "start at 1"),
    ("app@src/main.py:3-2", "backwards"),
    ("app@src/main.py", "pointer is"),                      # no line
    ("src/main.py:1", "pointer is"),                        # a bare path
    ("app@src/missing.py:1", "does not exist"),
    ("app@missing/dir/x.py:1", "does not exist"),
    ("app@src:1", "not a regular file"),                    # a folder
    ("app@src/main.py/x:1", "does not exist"),              # a file used as a folder
    ("app@src//main.py:1", "empty part"),
    ("app@./src/main.py:1", "empty part"),
    ("app@.git/config:1", "inside .git"),
    ("app@.env:1", "private"),                              # PM t-652.6
    ("app@config/.env.example:1", "private"),
    ("app@certs/server.key:1", "private"),
    ("app@src\\main.py:1", "relative path"),
    ("cmd: git log --stat", "cannot be checked later"),     # PM t-652.2
    ("cmd:git log:1", "cannot be checked later"),
    ("", "pointer is"),
    ("out:design/notes.txt:1", "under .alpaca/attach/study/outputs"),
])
def test_pointers_that_must_not_be_accepted(attached, pointer, why):
    upstream_edit(attached.upstream, {".env": "KEY=1\n", "config/.env.example": "KEY=\n",
                                      "certs/server.key": "k\n"})
    study.prepare(str(attached.root))
    res = good(attached, pointer)
    assert res.ok is False and why in res.reason, res.reason


def test_a_tracked_symlink_to_the_studio_is_refused(attached):
    """PM t-652.1: a link inside the repository that points into the studio would pass a studio-wide realpath test."""
    sha = upstream_edit(attached.upstream, links={"link-to-studio": "../.alpaca/alpaca.db", "linkdir": "src"})
    study.prepare(str(attached.root))
    bad = study.check_map_pointer(str(attached.root), "app@link-to-studio:1", sha)
    assert bad.ok is False and "symbolic link" in bad.reason
    through = study.check_map_pointer(str(attached.root), "app@linkdir/main.py:1", sha)
    assert through.ok is False and "does not exist" in through.reason


def test_a_binary_file_is_refused(attached):
    sha = upstream_edit(attached.upstream, {"bin/blob.zip": b"PK\x03\x04\x00\x00data\n" * 3})
    study.prepare(str(attached.root))
    res = study.check_map_pointer(str(attached.root), "app@bin/blob.zip:2", sha)
    assert res.ok is False and "binary" in res.reason


def test_a_submodule_is_refused(attached):
    sha = upstream_edit(attached.upstream, gitlinks={"vendor/dep": attached.upstream.sha()})
    study.prepare(str(attached.root))
    res = study.check_map_pointer(str(attached.root), "app@vendor/dep:1", sha)
    assert res.ok is False and "submodule" in res.reason


def test_a_commit_the_clone_does_not_hold_is_a_refusal_not_a_crash(prepared):
    att, _ = prepared
    res = study.check_map_pointer(str(att.root), "app@src/main.py:1", "0" * 40)
    assert res.ok is False and "is not in app/" in res.reason
    assert study.check_map_pointer(str(att.root), "app@src/main.py:1", "main").ok is False


def test_the_checkout_is_never_read(prepared):
    """The pointer reads the object store at the sha: a dirty or moved checkout changes nothing (P21)."""
    att, _ = prepared
    sha = att.upstream.sha()
    (att.app / "src/main.py").write_text("changed\nlines\nhere\n")
    assert study.check_map_pointer(str(att.root), "app@src/main.py:3", sha).ok is False
    (att.app / "src/main.py").unlink()
    assert study.check_map_pointer(str(att.root), "app@src/main.py:1", sha).ok is True


def test_out_pointers_resolve_at_the_commit_not_in_the_folder(prepared):
    att, _ = prepared
    root = str(att.root)
    sha = att.upstream.sha()
    base = ".alpaca/attach/study/outputs/%s" % sha[:12]
    assert study.check_map_pointer(root, "out:%s/git-log.txt:1" % base, sha).ok is True
    assert study.check_map_pointer(root, "out:%s/git-log.txt:2" % base, sha).ok is True
    assert study.check_map_pointer(root, "out:%s/git-log.txt:3" % base, sha).ok is False         # two commits only
    assert study.check_map_pointer(root, "out:%s/files.txt:1" % base, sha).ok is True
    assert study.check_map_pointer(root, "out:%s/shortlog.txt:2" % base, sha).ok is True
    for bad in ("out:%s/none.txt:1" % base,                                      # not an output prepare makes
                "out:.alpaca/attach/study/outputs/git-log.txt:1",                # the old, unbound form
                "out:.alpaca/attach/study/outputs/%s/git-log.txt:1" % ("0" * 12),  # another commit's folder
                "out:.alpaca/attach/state.json:1",
                "out:.alpaca/attach/study/outputs/../../state.json:1",
                "out:%s/../%s/git-log.txt:1" % (base, sha[:12])):
        assert study.check_map_pointer(root, bad, sha).ok is False, bad


def test_the_files_in_the_outputs_folder_are_never_evidence(prepared):
    """A file dropped there, an edited copy, a link or a folder: the checker recomputes from the commit."""
    att, _ = prepared
    root, sha = str(att.root), att.upstream.sha()
    out = att.root / ".alpaca/attach/study/outputs" / sha[:12]
    ptr = "out:.alpaca/attach/study/outputs/%s/" % sha[:12]
    (out / "fake.txt").write_text("main() wipes the flash at boot\n" * 5)
    assert study.check_map_pointer(root, ptr + "fake.txt:1", sha).ok is False
    (out / "git-log.txt").write_text((out / "git-log.txt").read_text() + "deadbee 2026 Mallory x\n")
    assert study.check_map_pointer(root, ptr + "git-log.txt:3", sha).ok is False
    (out / "git-log.txt").unlink()
    os.symlink(att.root / ".alpaca/attach/state.json", out / "git-log.txt")
    assert study.check_map_pointer(root, ptr + "git-log.txt:2", sha).ok is True       # recomputed, the link is not read
    with pytest.raises(AttachError, match="link"):                       # prepare never writes through a link
        study.prepare(root, fetch=False)
    (out / "git-log.txt").unlink()
    study.prepare(root, fetch=False)                                      # and puts the real bytes back
    assert len((out / "git-log.txt").read_text().splitlines()) == 2


def test_an_out_pointer_is_bound_to_the_maps_commit_after_a_later_prepare(prepared):
    """A4: a line past the end of the log at the map's commit stays refused after the base moves on."""
    att, info = prepared
    root, a = str(att.root), info["sha"]

    def with_line(n):
        ptr = "[ptr: out:.alpaca/attach/study/outputs/%s/git-log.txt:%d]" % (a[:12], n)
        write_map(att, sha=a, body=BODY + "- The history is short. " + ptr + "\n")
        return study.check_map(root)
    assert with_line(2).ok is True
    res = with_line(3)
    assert res.ok is False and any("has 2 lines" in p for p in res.problems), res.problems
    upstream_edit(att.upstream, {"zzz.txt": "x\n"})
    b = study.prepare(root)["sha"]                            # the base moves to B, the map stays at A
    assert len((att.root / ".alpaca/attach/study/outputs" / b[:12] / "git-log.txt").read_text().splitlines()) == 3
    assert with_line(2).ok is True
    assert with_line(3).ok is False
    # the folder of A is kept for the scouts while the map names A
    assert (att.root / ".alpaca/attach/study/outputs" / a[:12] / "git-log.txt").is_file()


# --------------------------------------------------------------------------------------- the map

def test_a_good_map_passes(prepared):
    att, _ = prepared
    write_map(att)
    res = study.check_map(str(att.root))
    assert res.ok is True, res.problems
    assert res.claims == 4 and res.sha == att.upstream.sha()


def test_a_claim_with_a_pointer_that_does_not_resolve_is_not_accepted(prepared):
    """The contract fail case of t-652."""
    att, _ = prepared
    write_map(att, body=BODY.replace("src/main.py:1", "src/main.py:40"))
    res = study.check_map(str(att.root))
    assert res.ok is False and any("src/main.py:40" in p and "line 40" in p for p in res.problems)


@pytest.mark.parametrize("body, why", [
    (BODY.replace(" [ptr: app@src/main.py:1]", ""), "no [ptr"),
    (BODY + "\nThe code is clean and simple.\n", "plain text"),
    (BODY.replace("## Area: docs", "Overview of the repository.\n## Area: docs"), "plain text"),
    ("Intro text before any area.\n" + BODY, "outside an area"),
    (BODY.replace("hello.", "hello, see [[main]] related to start."), "double-bracket"),
    (BODY.replace("## Area: build", "## Area: unknown-one"), "not in the front matter"),
    (BODY + "## Area: src\n- Again. [ptr: app@src/main.py:1]\n", "two sections"),
    (BODY.replace("## Area: build\n- The test target echoes test. [ptr: app@Makefile:4-5]\n", ""), "no '## Area: build'"),
    (BODY.replace("[ptr: app@Makefile:4-5]", "[ptr: app@src/main.py:1]"), "outside the paths of area build"),
    (BODY.replace("The program prints hello.", "- "), "no claim text"),
    (BODY.replace("[ptr: app@src/main.py:1]", "[ptr: cmd: git log --stat]"), "cannot be checked later"),
    (BODY.replace("hello.", "the key is AKIAIOSFODNN7EXAMPLE and more."), "secret screen"),
])
def test_map_rules(prepared, body, why):
    att, _ = prepared
    write_map(att, body=body)
    res = study.check_map(str(att.root))
    assert res.ok is False and any(why in p for p in res.problems), res.problems


def test_a_pointer_to_the_env_file_is_refused_in_a_map(attached):
    upstream_edit(attached.upstream, {".env": "TOKEN=1\n"})
    study.prepare(str(attached.root))
    areas = AREAS + [("root", [".env"])]
    write_map(attached, body=BODY + "## Area: root\n- The env file holds a token. [ptr: app@.env:1]\n", areas=areas)
    res = study.check_map(str(attached.root))
    assert res.ok is False and any("private" in p for p in res.problems)


@pytest.mark.parametrize("front, why", [
    ("", "no front matter"),
    ("---\nareas: [{id: a, paths: [x]}]\n---\n", "no attach_sha"),
    ('---\nattach_sha: "%s"\n---\n', "lists no areas"),
    ('---\nattach_sha: "%s"\nareas: [{id: Bad_Id, paths: [x]}]\n---\n', "areas[0].id"),
    ('---\nattach_sha: "%s"\nareas: [{id: a, paths: []}]\n---\n', "non-empty paths"),
    ('---\nattach_sha: "%s"\nareas: [{id: a, paths: ["../x"]}]\n---\n', "not a relative file or folder"),
    ('---\nattach_sha: "%s"\nareas: [{id: a, paths: [x]}, {id: a, paths: [y]}]\n---\n', "listed twice"),
])
def test_front_matter_rules(prepared, front, why):
    att, _ = prepared
    write_map(att, text=(front % att.upstream.sha() if "%s" in front else front) + "## Area: a\n- x. [ptr: app@src/main.py:1]\n")
    res = study.check_map(str(att.root))
    assert res.ok is False and any(why in p for p in res.problems), res.problems


def test_no_map_is_an_error_not_a_pass(prepared):
    att, _ = prepared
    (att.root / MAP).unlink()
    with pytest.raises(AttachError, match="no map"):
        study.check_map(str(att.root))


def test_a_map_that_is_a_link_is_refused(prepared, tmp_path):
    att, _ = prepared
    real = tmp_path / "real-map.md"
    real.write_text(map_text(att.upstream.sha()))
    (att.root / MAP).unlink()
    os.symlink(real, att.root / MAP)
    with pytest.raises(AttachError, match="link"):
        study.check_map(str(att.root))


def test_an_attach_sha_with_the_commit_missing_gives_a_problem(prepared):
    att, _ = prepared
    write_map(att, sha="a" * 40)
    res = study.check_map(str(att.root))
    assert res.ok is False and any("is not in app/" in p for p in res.problems)


# -------------------------------------------------------------------------- recorded sha and stale

def test_pointers_still_resolve_at_the_recorded_sha_after_upstream_renames(prepared):
    """PM t-652.3: the checkout and the base move; the map keeps describing the commit it names."""
    att, _ = prepared
    sha = write_map(att)
    new = upstream_edit(att.upstream, moves=[("src/main.py", "src/other.py")])
    study.prepare(str(att.root))
    assert git(att.app, "rev-parse", "refs/remotes/origin/main") == new
    assert study.check_map(str(att.root)).ok is True
    assert study.stale_areas(str(att.root), sha, new) == ["src"]


@pytest.mark.parametrize("name, edit, expect", [
    ("rename-old-path", dict(moves=[("src/main.py", "lib/main.py")]), ["src"]),
    ("rename-into-area", dict(moves=[("Makefile", "src/Makefile")]), ["src", "build"]),
    ("delete", dict(removes=["CHANGELOG.md"]), ["docs"]),
    ("add", dict(writes={"src/util.py": "u = 1\n"}), ["src"]),
    ("edit-file-area", dict(writes={"README.md": "# changed\n"}), ["docs"]),
    ("unrelated", dict(writes={"other/x.txt": "x\n"}), []),
    ("sibling-prefix", dict(writes={"src2/x.py": "x\n"}), []),
])
def test_stale_areas_see_renames_deletes_and_adds(prepared, name, edit, expect):
    """PM t-652.4: a path-prefix test on the new path alone misses the old area of a rename or a delete."""
    att, _ = prepared
    sha = write_map(att)
    new = upstream_edit(att.upstream, **edit)
    study.prepare(str(att.root))
    assert study.stale_areas(str(att.root), sha, new) == expect


def test_stale_areas_of_a_commit_the_clone_lost_is_every_area(prepared):
    att, _ = prepared
    write_map(att)
    assert study.stale_areas(str(att.root), "a" * 40, att.upstream.sha()) == ["src", "docs", "build"]
    assert study.stale_areas(str(att.root), att.upstream.sha(), att.upstream.sha()) == []


def test_prepare_flags_the_stale_areas_of_an_older_map_and_status_shows_them(prepared):
    att, _ = prepared
    write_map(att)
    upstream_edit(att.upstream, {"src/util.py": "u = 1\n"})
    info = study.prepare(str(att.root))
    assert info["map"]["stale_areas"] == ["src"]
    assert study.read_stale(str(att.root))["areas"] == ["src"]
    assert health.status(str(att.root))["stale_areas"] == ["src"]
    # the map is still checked at its own sha, and ingest clears the flag only for a map at the base
    assert study.check_map(str(att.root)).ok is True
    study.ingest(str(att.root))
    assert study.read_stale(str(att.root))["areas"] == ["src"]
    write_map(att, sha=info["sha"])
    study.ingest(str(att.root))
    assert study.read_stale(str(att.root))["areas"] == []


def test_flag_stale_compares_the_map_with_the_newest_recorded_base(prepared):
    """What sync calls after its own prepare: the base is the one the record names, not a ref of the clone."""
    att, first = prepared
    root = str(att.root)
    write_map(att)
    upstream_edit(att.upstream, removes=["CHANGELOG.md"])
    second = study.prepare(root)                              # fetches, records B and flags the docs area
    (att.root / ".alpaca/attach/study/stale.json").unlink()
    assert study.flag_stale(root) == ["docs"]
    flag = study.read_stale(root)
    assert flag["since_sha"] == first["sha"] and flag["to_sha"] == second["sha"]
    assert events(att.root)[-1]["data"]["action"] == "stale"
    # a fetch that no prepare recorded changes nothing: the ref moves, the base does not
    upstream_edit(att.upstream, {"src/more.py": "m = 1\n"})
    git(att.app, "fetch", "-q", "origin")
    assert study.flag_stale(root) == ["docs"] and study.read_stale(root)["to_sha"] == second["sha"]


def test_flag_stale_without_a_recorded_base_is_a_refusal(attached):
    write_map(attached, sha=attached.upstream.sha())
    with pytest.raises(AttachError, match="no base was recorded"):
        study.flag_stale(str(attached.root))


# ----------------------------------------------------------------------------------- wiki ingest

def vault_file(att, doc_id):
    return att.root / ".alpaca/wiki" / doc_id


def active_map_docs(att):
    """doc ids with active blocks under wiki/attach/map/, read from the wiki store."""
    from alpaca.wiki.config import Config
    from alpaca.wiki.store.db import open_db_readonly
    conn = open_db_readonly(Config.for_project(str(att.root)))
    try:
        return sorted(r[0] for r in conn.execute(
            "SELECT DISTINCT doc_id FROM blocks WHERE status='active' AND doc_id LIKE 'wiki/attach/map/%'"))
    finally:
        conn.close()


def area_of(doc_id):
    return doc_id.split("/")[3]


def test_ingest_makes_one_wiki_document_per_area(prepared):
    att, _ = prepared
    write_map(att)
    ids = study.ingest(str(att.root))
    assert [area_of(i) for i in ids] == ["src", "docs", "build"]
    assert all(i.startswith("wiki/attach/map/") and i.endswith(".md") for i in ids)
    docs = {area_of(i): i for i in ids}
    text = vault_file(att, docs["docs"]).read_text()
    assert "volatile: true" in text and "The changelog starts with the word first." in text
    assert "The program prints hello." not in text
    assert active_map_docs(att) == sorted(ids)
    assert events(att.root)[-1]["data"]["action"] == "ingest"


def test_ingest_of_an_unchanged_map_changes_nothing(prepared):
    att, _ = prepared
    write_map(att)
    first = study.ingest(str(att.root))
    again = study.ingest(str(att.root))
    assert again == first and active_map_docs(att) == sorted(first)
    assert events(att.root)[-1]["data"]["retired"] == []


def test_an_area_refreshed_with_fewer_lines_ingests(prepared):
    """PM t-652.5: the wiki's body-shrink gate (a new body under 70 percent of the old one) must not
    reject a smaller refresh of one area, and the old version must leave the current indexes."""
    att, _ = prepared
    many = "## Area: src\n" + "".join(
        "- Claim number %d about the demo program output. [ptr: app@src/main.py:1]\n" % i for i in range(1, 9))
    rest = BODY.split("## Area: docs")[1]
    write_map(att, body=many + "## Area: docs" + rest)
    first = {area_of(i): i for i in study.ingest(str(att.root))}
    big = vault_file(att, first["src"]).read_text()
    few = "## Area: src\n- One claim only. [ptr: app@src/main.py:1]\n"
    write_map(att, body=few + "## Area: docs" + rest)
    second = {area_of(i): i for i in study.ingest(str(att.root))}
    small = vault_file(att, second["src"]).read_text()
    assert len(small) < 0.5 * len(big) and "One claim only." in small and "Claim number 3" not in small
    assert second["src"] != first["src"] and second["docs"] == first["docs"]
    assert first["src"] not in active_map_docs(att) and second["src"] in active_map_docs(att)
    assert events(att.root)[-1]["data"]["retired"] == [first["src"]]


def test_an_area_that_leaves_the_map_leaves_the_wiki(prepared):
    att, _ = prepared
    write_map(att)
    first = {area_of(i): i for i in study.ingest(str(att.root))}
    smaller = BODY.split("## Area: build")[0]
    write_map(att, body=smaller, areas=AREAS[:2])
    study.ingest(str(att.root))
    assert first["build"] not in active_map_docs(att) and first["src"] in active_map_docs(att)


def test_a_map_that_fails_the_check_ingests_nothing(prepared):
    att, _ = prepared
    write_map(att, body=BODY.replace("src/main.py:1", "src/main.py:9"))
    with pytest.raises(AttachError, match="does not pass the check"):
        study.ingest(str(att.root))
    assert not (att.root / ".alpaca/wiki/wiki/attach").exists()


def test_a_secret_pasted_into_the_map_never_reaches_the_wiki(prepared):
    """PM t-652.6: the map check refuses it, and the wiki's own screen redacts it if the check is skipped."""
    att, _ = prepared
    write_map(att, body=BODY.replace("hello.", "hello and the key AKIAIOSFODNN7EXAMPLE."))
    with pytest.raises(AttachError):
        study.ingest(str(att.root))
    from alpaca.wiki import service
    path = att.root / "plain.md"
    path.write_text("The demo uses key AKIAIOSFODNN7EXAMPLE for tests.\n")
    service.ingest(str(att.root), str(path), doc_id="wiki/attach/map/raw.md")
    assert "AKIAIOSFODNN7EXAMPLE" not in vault_file(att, "wiki/attach/map/raw.md").read_text()


# ------------------------------------------------------------------------------------------ verb

def run_cli(capsys, *argv):
    rc = cli.main(["attach", "study", *argv])
    return rc, capsys.readouterr().out


def test_the_verb_prepares_checks_ingests_and_flags(attached, capsys):
    rc, out = run_cli(capsys)
    assert rc == 0 and "GATE alpaca-attach-study: PASS" in out and "map skeleton written" in out
    rc, out = run_cli(capsys, "check")
    assert rc == 1 and "GATE alpaca-attach-study: FAIL" in out and "no claims" in out
    write_map(attached)
    rc, out = run_cli(capsys, "check")
    assert rc == 0 and "4 claims" in out
    rc, out = run_cli(capsys, "ingest")
    assert rc == 0 and "ingested 3 area documents" in out
    rc, out = run_cli(capsys, "stale")
    assert rc == 0 and "stale areas: none" in out
    rc, out = run_cli(capsys, "check", "--json")
    receipt = json.loads(out)
    assert rc == 0 and receipt["verdict"] == "PASS" and receipt["receipt"]["claims"] == 4


def test_the_verb_is_blocked_in_a_studio_that_is_not_attached(studio, capsys):
    rc, out = run_cli(capsys)
    assert rc == 2 and "GATE alpaca-attach-study: BLOCKED" in out


def test_the_verb_fails_on_a_missing_map_with_a_plain_reason(attached, capsys):
    rc, out = run_cli(capsys, "check")
    assert rc == 1 and "no map" in out


def test_the_verb_never_prints_url_user_info(attached, capsys):
    rc, out = run_cli(capsys)
    assert rc == 0 and "@github" not in out


def test_no_fetch_needs_a_base_an_earlier_prepare_recorded(attached, capsys):
    rc, out = run_cli(capsys, "--no-fetch")
    assert rc == 1 and "no base was recorded yet" in out
    rc, out = run_cli(capsys)
    assert rc == 0
    rc, out = run_cli(capsys, "--no-fetch")
    assert rc == 0 and "GATE alpaca-attach-study: PASS" in out


# ------------------------------------------------------------------------------- environment, links

def test_inherited_git_variables_cannot_point_the_study_at_the_studio(prepared, monkeypatch):
    """PM X1: GIT_DIR and friends from a hook or a test runner must not redirect a read to the studio."""
    att, _ = prepared
    write_map(att)
    monkeypatch.setenv("GIT_DIR", str(att.root / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(att.root))
    monkeypatch.setenv("GIT_INDEX_FILE", str(att.root / ".git/index"))
    assert study.check_map(str(att.root)).ok is True
    assert study.prepare(str(att.root), fetch=False)["sha"] == att.upstream.sha()


def test_a_clone_that_lost_its_git_folder_is_never_read_as_the_studio(prepared):
    """Without app/.git, `git -C app` would answer for the studio repository that contains it."""
    att, _ = prepared
    write_map(att)
    os.rename(att.app / ".git", att.app / ".git-away")
    res = study.check_map(str(att.root))
    assert res.ok is False and any("not a git repository of its own" in p for p in res.problems)
    with pytest.raises(AttachError, match="not a git repository of its own"):
        study.prepare(str(att.root), fetch=False)


def test_the_map_option_cannot_leave_the_studio(prepared, capsys, tmp_path):
    att, _ = prepared
    outside = tmp_path / "outside.md"
    outside.write_text(map_text(att.upstream.sha()))
    for target in ("../outside.md", str(outside), "design/../../outside.md"):
        rc, out = run_cli(capsys, "check", "--map", target)
        assert rc == 1 and "GATE alpaca-attach-study: FAIL" in out, out


def test_the_verb_flags_stale_areas_and_ingest_of_a_bad_map_fails(prepared, capsys):
    att, _ = prepared
    write_map(att)
    upstream_edit(att.upstream, {"src/util.py": "u = 1\n"})
    rc, out = run_cli(capsys)
    assert rc == 0 and "stale areas: src" in out
    rc, out = run_cli(capsys, "stale", "--json")
    assert rc == 0 and json.loads(out)["receipt"]["stale_areas"] == ["src"]
    write_map(att, body=BODY.replace("README.md:1", "README.md:50"))
    rc, out = run_cli(capsys, "ingest")
    assert rc == 1 and "does not pass the check" in out
