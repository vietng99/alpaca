"""attach study, round 2: the checker does not trust what the clone says about its own objects, refs or config.

The clone `app/` is written by the agents that run in the studio. These tests forge what an agent could forge
there (a loose object under a real id, a parent a commit never had, a moved ref, a rewritten url, a link where a
folder was) and show the answer stays what the upstream holds. Each forging test first shows that plain git is
fooled, so a pass never means "the forgery did not work". Classes (t-652.logs/investigate-r2.md):

  A  objects, refs and config of the clone (R2-01 to R2-05 and their siblings)
  B  one stale flag per map (R2-06)
  C  deletes that follow a link (R2-07)
  D  trace switches in the git environment (R2-11, R2-12)
"""
import json
import os
import re
import subprocess
import time
import zlib
from pathlib import Path

import pytest

from alpaca.attach import AttachError, config, gitops, proven, study
from alpaca.tests.attachkit import attached, git, studio, upstream  # noqa: F401
from alpaca.tests.test_attach_study import (AREAS, BODY, MAP, map_text, prepared, upstream_edit,  # noqa: F401
                                            write_map)

OUT = ".alpaca/attach/study/outputs"
PTR50 = BODY.replace("[ptr: app@src/main.py:1]", "[ptr: app@src/main.py:50]")


def plain(app, *args, input=None, env=None):
    """git in the clone with none of the checker's protections (what a scout's shell sees)."""
    full = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1", LC_ALL="C",
                GIT_AUTHOR_NAME="m", GIT_AUTHOR_EMAIL="m@example.org",
                GIT_COMMITTER_NAME="m", GIT_COMMITTER_EMAIL="m@example.org")
    full.update(env or {})
    done = subprocess.run(["git", "-C", str(app), *args], capture_output=True, env=full,
                          input=input if isinstance(input, (bytes, type(None))) else input.encode())
    assert done.returncode == 0, (args, done.stderr.decode()[-300:])
    return done.stdout.decode().strip()


def raw(app, *args):
    done = subprocess.run(["git", "-C", str(app), *args], capture_output=True, check=True)
    return done.stdout


def write_loose(app, kind, data, oid):
    """A loose object file named `oid` that holds `<kind> <len>\\0<data>`, whatever its real hash is. A new
    file and os.replace, so an object hard-linked from the upstream is never changed in place."""
    path = Path(app) / ".git" / "objects" / oid[:2] / oid[2:]
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(oid[2:] + ".forge")
    tmp.write_bytes(zlib.compress(b"%s %d\0" % (kind, len(data)) + data))
    os.replace(tmp, path)


def explode_packs(app):
    """Make every packed object of the clone a loose one, so a forger can replace it."""
    pack_dir = Path(app) / ".git" / "objects" / "pack"
    for pack in sorted(pack_dir.glob("*.pack")):
        moved = Path(app) / ".git" / (pack.name + ".moved")
        os.replace(pack, moved)
        for ext in (".idx", ".rev", ".keep", ".bitmap"):
            if pack.with_suffix(ext).exists():
                pack.with_suffix(ext).unlink()
        with open(moved, "rb") as fh:
            assert subprocess.run(["git", "-C", str(app), "unpack-objects", "-q"], stdin=fh, capture_output=True).returncode == 0
        moved.unlink()


def forge_pack_entry(app, oid, data):
    """A pack and index (version 2) that hold `data` as a blob under the id `oid`. The loose copy of `oid`, if any,
    is removed first, so the pack is the only place git can find the object."""
    import hashlib
    import struct
    loose = Path(app) / ".git" / "objects" / oid[:2] / oid[2:]
    if loose.exists():
        loose.unlink()
    size, head = len(data), bytearray()
    first = (3 << 4) | (size & 0x0F)
    size >>= 4
    while size:
        head.append(first | 0x80)
        first, size = size & 0x7F, size >> 7
    head.append(first)
    entry = bytes(head) + zlib.compress(data)
    pack = b"PACK" + struct.pack(">II", 2, 1) + entry
    pack += hashlib.sha1(pack).digest()
    fan = b"".join(struct.pack(">I", 1 if i >= int(oid[:2], 16) else 0) for i in range(256))
    idx = (b"\xfftOc" + struct.pack(">I", 2) + fan + bytes.fromhex(oid) + struct.pack(">I", zlib.crc32(entry))
           + struct.pack(">I", 12) + pack[-20:])
    idx += hashlib.sha1(idx).digest()
    name = Path(app) / ".git" / "objects" / "pack" / ("pack-" + hashlib.sha1(pack).hexdigest())
    name.with_suffix(".pack").write_bytes(pack)
    name.with_suffix(".idx").write_bytes(idx)


def clone_commit(app, tree, message="forged", parents=(), date="946684800 +0000"):
    """A commit made inside the clone (never upstream)."""
    args = ["commit-tree", tree, "-m", message]
    for p in parents:
        args += ["-p", p]
    return plain(app, *args, env={"GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date})


def forged_root_commit(app, sha, files):
    """A commit made in the clone whose tree is the tree of `sha` with `files` replaced."""
    idx = Path(app) / ".git" / "forge-index"
    env = {"GIT_INDEX_FILE": str(idx)}
    plain(app, "read-tree", sha, env=env)
    for rel, data in files.items():
        blob = plain(app, "hash-object", "-w", "--stdin", input=data)
        plain(app, "update-index", "--add", "--cacheinfo", "100644,%s,%s" % (blob, rel), env=env)
    tree = plain(app, "write-tree", env=env)
    idx.unlink()
    return clone_commit(app, tree)


def events_of(root, action):
    from alpaca import db
    return [e["data"] for e in db.events(db.connect(str(root)), kind="attach-study") if e["data"].get("action") == action]


# ================================================================ class A: the objects of the clone

@pytest.mark.parametrize("storage", ["packed-and-loose", "loose-only"])
def test_a_forged_loose_blob_under_a_real_id_is_refused(prepared, storage):
    """R2-01: a loose file named after the 1-line blob of src/main.py that holds 50 lines. git serves it as that
    blob (probe: the first assert); the checker hashes what it reads. With a packed copy beside the loose one git
    prints the size of one and the bytes of the other; with the pack gone only the forgery is left."""
    att, info = prepared
    root, app, sha = str(att.root), att.app, info["sha"]
    if storage == "loose-only":
        explode_packs(app)
    oid = plain(app, "rev-parse", "%s:src/main.py" % sha)
    write_loose(app, b"blob", b"line\n" * 50, oid)
    assert plain(app, "cat-file", "blob", oid).count("line") == 50
    res = study.check_map_pointer(root, "app@src/main.py:50", sha)
    assert res.ok is False and "does not hash to its own id" in res.reason, res.reason
    assert study.check_map_pointer(root, "app@src/main.py:1", sha).ok is False       # nothing is claimed from a forged object
    write_map(att, sha=sha, body=PTR50)
    assert study.check_map(root).ok is False
    with pytest.raises(AttachError, match="does not pass the check"):
        study.ingest(root)


def test_a_forged_packed_blob_under_a_real_id_is_refused(prepared):
    """The same forgery with no loose file: a pack and its index that map the real id to other bytes."""
    att, info = prepared
    root, app, sha = str(att.root), att.app, info["sha"]
    explode_packs(app)
    oid = plain(app, "rev-parse", "%s:src/main.py" % sha)
    forge_pack_entry(app, oid, b"line\n" * 50)
    assert plain(app, "cat-file", "blob", oid).count("line") == 50           # the forgery took
    res = study.check_map_pointer(root, "app@src/main.py:50", sha)
    assert res.ok is False and "does not hash to its own id" in res.reason, res.reason


def test_a_forged_loose_tree_under_a_real_id_is_refused(prepared):
    att, info = prepared
    root, app, sha = str(att.root), att.app, info["sha"]
    explode_packs(app)
    tree = plain(app, "rev-parse", "%s:src" % sha)
    fake_blob = plain(app, "hash-object", "-w", "--stdin", input=b"line\n" * 50)
    write_loose(app, b"tree", b"100644 main.py\0" + bytes.fromhex(fake_blob), tree)
    assert plain(app, "cat-file", "blob", "%s:src/main.py" % sha).count("line") == 50
    res = study.check_map_pointer(root, "app@src/main.py:50", sha)
    assert res.ok is False and "does not hash to its own id" in res.reason, res.reason


def test_a_forged_loose_commit_under_a_real_id_is_refused_by_the_checker_and_by_prepare(prepared):
    att, info = prepared
    root, app, sha = str(att.root), att.app, info["sha"]
    explode_packs(app)
    body = raw(app, "cat-file", "commit", sha)
    forged = body.replace(b"second commit", b"a commit that never happened")
    assert forged != body
    write_loose(app, b"commit", forged, sha)
    assert "never happened" in plain(app, "log", "-1", "--format=%s", sha)
    res = study.check_map_pointer(root, "out:%s/%s/git-log.txt:1" % (OUT, sha[:12]), sha)
    assert res.ok is False and "does not hash to its own id" in res.reason
    with pytest.raises(AttachError, match="does not hash to its own id"):
        study.prepare(root, fetch=False)


def test_a_parent_a_commit_never_had_adds_no_line_to_the_log(prepared):
    """The investigator's probe: not the base but its parent is rewritten with an extra parent F, so `git log`
    (with the commit-graph off and replace refs off) prints F too, and F passes `merge-base --is-ancestor`."""
    att, info = prepared
    root, app, sha = str(att.root), att.app, info["sha"]
    explode_packs(app)
    parent = plain(app, "rev-parse", sha + "~1")
    extra = forged_root_commit(app, sha, {"src/main.py": "line\n" * 50})
    body = raw(app, "cat-file", "commit", parent)
    head, _, rest = body.partition(b"\n")
    write_loose(app, b"commit", head + b"\nparent " + extra.encode() + b"\n" + rest, parent)
    shown = plain(app, "-c", "core.commitGraph=false", "--no-replace-objects", "log", "--format=%s", sha).splitlines()
    assert "forged" in shown                                                  # the forgery took
    assert plain(app, "-c", "core.commitGraph=false", "merge-base", "--is-ancestor", extra, sha) == ""
    res = study.check_map_pointer(root, "out:%s/%s/git-log.txt:3" % (OUT, sha[:12]), sha)
    assert res.ok is False and "does not hash to its own id" in res.reason, res.reason
    write_map(att, sha=extra, body=PTR50)                                     # and the forged commit itself is no base
    assert study.check_map(root).ok is False


def test_a_commit_graph_in_the_clone_changes_no_output(prepared):
    """A commit-graph file in the clone (here a damaged one) is not read by the checker, which parses the commit
    objects itself. The hand-forged graph that adds a parent is the frozen attack R2-03 in the lens' folder."""
    att, info = prepared
    root, app, sha = str(att.root), att.app, info["sha"]
    before = {n: study._output_bytes(str(app), sha, n) for n in study.OUTPUT_NAMES}
    plain(app, "commit-graph", "write", "--reachable", "--no-progress")
    graph = app / ".git/objects/info/commit-graph"
    data = bytearray(graph.read_bytes())
    data[-30:-20] = b"\x00" * 10                                              # damage a record near the end
    tmp = graph.with_name("commit-graph.forge")
    tmp.write_bytes(bytes(data))
    os.replace(tmp, graph)
    after = {n: study._output_bytes(str(app), sha, n) for n in study.OUTPUT_NAMES}
    assert after == before
    assert study.check_map_pointer(root, "out:%s/%s/git-log.txt:3" % (OUT, sha[:12]), sha).ok is False


def test_a_forged_blob_stops_prepare_before_anything_is_written(attached):
    root, app = str(attached.root), attached.app
    sha = attached.upstream.sha()
    explode_packs(app)
    oid = plain(app, "rev-parse", "%s:src/main.py" % sha)
    write_loose(app, b"blob", b"line\n" * 50, oid)
    with pytest.raises(AttachError, match="does not hash to its own id"):
        study.prepare(root)
    study_dir = attached.root / ".alpaca/attach/study"
    assert not study_dir.exists() and not (attached.root / MAP).exists()
    assert events_of(root, "prepare") == []


def test_the_checker_runs_no_walk_of_history_trees_or_diffs_through_git(prepared, monkeypatch):
    """The design in one assertion: git is asked for single objects (cat-file) and the object format (rev-parse);
    log, ls-tree, diff, merge-base and rev-list are not run, so the clone's commit-graph, index and grafts have
    nothing to say."""
    att, info = prepared
    root = str(att.root)
    seen = []
    real = study._git

    def spy(app, *args, **kw):
        seen.append(args[0])
        return real(app, *args, **kw)
    monkeypatch.setattr(study, "_git", spy)
    write_map(att, sha=info["sha"])
    body = BODY + "- The history is short. [ptr: out:%s/%s/git-log.txt:2]\n" % (OUT, info["sha12"])
    write_map(att, sha=info["sha"], body=body)
    assert study.check_map(root).ok is True
    new = upstream_edit(att.upstream, {"src/util.py": "u = 1\n"})
    study.prepare(root)
    assert study.stale_areas(root, info["sha"], new) == ["src"]
    assert set(seen) <= {"cat-file", "rev-parse"}, sorted(set(seen))
    source = Path(study.__file__).read_text()
    assert sorted(set(re.findall(r"gitops\.(\w+)", source))) == ["clean_env", "config_digest", "remote", "run", "scrub"]
    assert len(re.findall(r"gitops\.run\(", source)) == 1


def test_git_is_told_not_to_fetch_a_missing_object_from_a_promisor_remote(attached, monkeypatch):
    seen = {}

    def fake_run(repo, *args, env=None, **kw):
        seen["env"], seen["args"] = env, args
        return gitops.Result(0, "sha1\n", "")
    monkeypatch.setattr(gitops, "run", fake_run)
    study._git(str(attached.app), "rev-parse", "--show-object-format")
    assert seen["env"]["GIT_NO_LAZY_FETCH"] == "1" and seen["env"]["GIT_GRAFT_FILE"] == os.devnull
    assert seen["args"][0] == "--no-replace-objects"


# ---------------------------------------------------------------------------------- which commit is the base

def test_prepare_no_fetch_reads_no_ref_of_the_clone(prepared):
    """R2-04: refs/remotes/<upstream>/<base> moved to a commit made in the clone. `--no-fetch` studies the base the
    record names; a prepare that fetches puts the ref back."""
    att, info = prepared
    root, app, cfg = str(att.root), att.app, att.cfg
    ref = "refs/remotes/%s/%s" % (cfg.upstream, cfg.base)
    fake = forged_root_commit(app, info["sha"], {"src/main.py": "line\n" * 50})
    plain(app, "update-ref", ref, fake)
    got = study.prepare(root, fetch=False)
    assert got["sha"] == info["sha"] and [e["sha"] for e in events_of(root, "prepare")] == [info["sha"]] * 2
    write_map(att, sha=fake, body=PTR50)
    assert study.check_map(root).ok is False
    again = study.prepare(root)
    assert again["sha"] == info["sha"] and plain(app, "rev-parse", ref) == info["sha"]


def test_a_rewritten_url_in_the_clone_config_stops_the_fetch(prepared, tmp_path):
    """R2-05, first line: the config digest recorded at attach no longer matches."""
    att, info = prepared
    forged_bare = tmp_path / "forged.git"
    git(tmp_path, "clone", "-q", "--bare", str(att.upstream.bare), str(forged_bare))
    plain(att.app, "config", "url.%s.insteadOf" % forged_bare, att.upstream.url)
    with pytest.raises(AttachError, match="changed since attach"):
        study.prepare(str(att.root))
    assert [e["sha"] for e in events_of(str(att.root), "prepare")] == [info["sha"]]


def _forged_upstream(att, tmp_path):
    """A second bare repository that holds a commit the real upstream does not: src/main.py has 50 lines there."""
    cfg = att.cfg
    forged_bare = tmp_path / "forged.git"
    git(tmp_path, "clone", "-q", "--bare", str(att.upstream.bare), str(forged_bare))
    work = tmp_path / "forged-work"
    git(tmp_path, "clone", "-q", str(forged_bare), str(work))
    git(work, "checkout", "-q", cfg.base)
    (work / "src" / "main.py").write_text("line\n" * 50)
    git(work, "commit", "-qam", "forged")
    git(work, "push", "-q", "origin", cfg.base)
    return forged_bare, git(work, "rev-parse", "HEAD")


def _rewrite_state(att, **changes):
    root = str(att.root)
    state = config.read_state(root)
    state.update(changes)
    config.write_json_atomic(os.path.join(root, ".alpaca/attach/state.json"), state, root=root)


def test_the_fetch_is_anchored_to_the_upstream_even_when_the_digest_is_rewritten_too(prepared, tmp_path, monkeypatch):
    """R2-05, second line: the agent also rewrites the digest in state.json (a file of the studio it can write).
    The fetch goes to the forged repository, but the id comes from `ls-remote` with no repository, so the ref
    that names another commit is refused and nothing is recorded.

    op-062 integration of t-652: the digest now comes from the record (`proven.init_facts`, t-648's one reader),
    so the state.json rewrite alone is refused by line one. To keep testing line two, the record's digest is
    then made to match the rewritten config (as if line one were blind to the rewrite); the line-two
    assertions below are the original ones."""
    att, info = prepared
    root, app = str(att.root), att.app
    forged_bare, fake = _forged_upstream(att, tmp_path)
    plain(app, "config", "url.%s.insteadOf" % forged_bare, att.upstream.url)
    _rewrite_state(att, config_digest=gitops.config_digest(str(app)))
    with pytest.raises(AttachError, match="changed since attach"):                # line one: state.json is not read
        study.prepare(root)
    assert [e["sha"] for e in events_of(root, "prepare")] == [info["sha"]]
    real = proven.init_facts
    monkeypatch.setattr(proven, "init_facts", lambda r, c: dict(real(r, c), config_digest=gitops.config_digest(str(app))))
    with pytest.raises(AttachError) as exc:
        study.prepare(root)
    assert "the upstream says %s" % att.upstream.sha()[:12] in str(exc.value) and fake[:12] in str(exc.value)
    assert [e["sha"] for e in events_of(root, "prepare")] == [info["sha"]]
    write_map(att, sha=fake, body=PTR50)
    assert study.check_map(root).ok is False


def test_a_fetch_url_rewritten_in_state_json_brings_a_commit_the_upstream_does_not_name(prepared, tmp_path, monkeypatch):
    """The address the fetch uses is a studio file an agent can write; the id is asked of attach.url in project.yaml.

    op-062 integration of t-652: the fetch address now comes from `proven.fetch_url` (attach.url, tied to the
    attach-init event), so the state.json rewrite alone sends no fetch anywhere (the last block). The original
    assertions run with the address forced to the forged repository (as if the URL source were wrong), so the
    upstream anchor is still tested on its own."""
    att, info = prepared
    root = str(att.root)
    forged_bare, fake = _forged_upstream(att, tmp_path)
    _rewrite_state(att, fetch_url=str(forged_bare))
    with monkeypatch.context() as mp:
        mp.setattr(proven, "fetch_url", lambda r, c: str(forged_bare))
        with pytest.raises(AttachError) as exc:
            study.prepare(root)
    assert "the upstream says %s" % att.upstream.sha()[:12] in str(exc.value) and fake[:12] in str(exc.value)
    assert [e["sha"] for e in events_of(root, "prepare")] == [info["sha"]]
    got = study.prepare(root)                                                       # state.json alone: not read
    ref = "refs/remotes/%s/%s" % (att.cfg.upstream, att.cfg.base)
    assert got["sha"] == att.upstream.sha() == info["sha"] and plain(att.app, "rev-parse", ref) == info["sha"]


def test_the_base_follows_the_upstream_when_it_moves_between_the_two_asks(prepared, monkeypatch):
    att, info = prepared
    root = str(att.root)
    new = upstream_edit(att.upstream, {"src/new.py": "x = 1\n"})
    real = study._upstream_sha
    answers = iter([info["sha"]])                                            # the first ask is stale, as if asked before the push

    def once_stale(r, cfg, url):
        return next(answers, None) or real(r, cfg, url)
    monkeypatch.setattr(study, "_upstream_sha", once_stale)
    assert study.prepare(root)["sha"] == new


def test_only_a_base_the_record_names_is_a_base(prepared):
    att, info = prepared
    root = str(att.root)
    first = events_of(root, "prepare")
    assert [e["sha"] for e in first] == [info["sha"]]
    assert study._latest_base(root) == info["sha"]
    new = upstream_edit(att.upstream, {"src/new.py": "x = 1\n"})
    study.prepare(root)
    assert study._latest_base(root) == new and study._recorded_bases(root) == [new, info["sha"]]


# ------------------------------------------------------------------------------- stale areas from verified trees

def test_a_forged_tree_cannot_hide_a_change_from_the_stale_flag(prepared):
    """`git diff` compares the trees it reads from the clone; a changed folder whose tree object was rewritten to
    look like the old one reads as unchanged. The Python diff hashes both trees and calls every area stale."""
    att, info = prepared
    root, app = str(att.root), att.app
    a = write_map(att)
    b = upstream_edit(att.upstream, {"src/util.py": "u = 1\n"})
    study.prepare(root)
    assert study.stale_areas(root, a, b) == ["src"]
    explode_packs(app)
    new_src, old_src = plain(app, "rev-parse", "%s:src" % b), plain(app, "rev-parse", "%s:src" % a)
    write_loose(app, b"tree", raw(app, "cat-file", "tree", old_src), new_src)
    assert plain(app, "diff", "--name-only", a, b) == ""                      # the forgery took: plain git sees no change
    assert study.stale_areas(root, a, b) == [ident for ident, _ in AREAS]


def test_a_tree_that_names_one_folder_many_times_is_bounded(prepared, monkeypatch):
    """Ten names for the same subtree, four levels deep: a hundred thousand paths from five objects (each
    level added multiplies it by ten). Listing and diffing stop at their limits instead of expanding it."""
    att, info = prepared
    app = str(att.app)
    blob = plain(app, "hash-object", "-w", "--stdin", input=b"x\n")
    tree = plain(app, "mktree", input="".join("100644 blob %s\tf%d\n" % (blob, i) for i in range(10)))
    for _ in range(4):
        tree = plain(app, "mktree", input="".join("040000 tree %s\td%d\n" % (tree, i) for i in range(10)))
    bomb = clone_commit(app, tree)
    empty = clone_commit(app, plain(app, "mktree", input=""))
    monkeypatch.setattr(study, "MAX_TREE_ENTRIES", 5000)
    monkeypatch.setattr(study, "MAX_CHANGED", 5000)
    t0 = time.monotonic()
    with pytest.raises(AttachError, match="names more than"):
        study._output_bytes(app, bomb, "files.txt")
    with pytest.raises(AttachError, match="differ in more than"):
        study._changed_paths(study._Objects(app), empty, bomb)
    assert time.monotonic() - t0 < 10


# --------------------------------------------------------- the outputs agree with git where git is deterministic

def test_the_outputs_equal_git_log_date_order_and_ls_tree_on_a_history_with_a_merge(attached):
    up = attached.upstream
    work = up.work

    def commit(message, date, name="Ada Author", files=None):
        for rel, text in (files or {}).items():
            (work / rel).parent.mkdir(parents=True, exist_ok=True)
            (work / rel).write_text(text, encoding="utf-8")
        git(work, "add", "-A")
        git(work, "commit", "-q", "-m", message, env={"GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date,
                                                       "GIT_AUTHOR_NAME": name, "GIT_COMMITTER_NAME": name})
    git(work, "checkout", "-q", "-B", "topic", up.branch)
    commit("topic one\nsecond line of the subject\n\nbody text", "2030-01-02T10:00:00 +0000", "Zoe Unicode", {"t/one.txt": "1\n"})
    commit("topic two", "2030-01-01T23:30:00 -1100", "Zoe Unicode", {"t/two.txt": "2\n"})
    git(work, "checkout", "-q", up.branch)
    commit("main three", "2030-01-02T09:00:00 +0000", "Bo Builder", {"naive/caf\u00e9.txt": "3\n"})
    git(work, "merge", "-q", "--no-ff", "-m", "merge topic", "topic",
        env={"GIT_AUTHOR_DATE": "2030-01-03T10:00:00 +0000", "GIT_COMMITTER_DATE": "2030-01-03T10:00:00 +0000"})
    git(work, "push", "-q", "origin", up.branch)
    info = study.prepare(str(attached.root))
    sha, app = info["sha"], str(attached.app)
    mine = study._output_bytes(app, sha, "git-log.txt").decode().splitlines()
    real = plain(app, "log", "--date-order", "--format=%H %ad %an %s", "--date=short", sha).splitlines()
    assert mine == real, (mine, real)
    files = study._output_bytes(app, sha, "files.txt").decode().split("\n")[:-1]
    listed = raw(app, "ls-tree", "-r", "-z", "--name-only", sha).decode().split("\0")[:-1]
    assert files == listed and "naive/caf\u00e9.txt" in files


def test_a_child_is_listed_before_its_parent_even_when_the_parent_is_newer(prepared):
    att, info = prepared
    app = str(att.app)
    tree = plain(app, "rev-parse", info["sha"] + "^{tree}")
    parent = clone_commit(app, tree, "parent", date="1893456000 +0000")           # 2030
    child = clone_commit(app, tree, "child", parents=[parent], date="1577836800 +0000")   # 2020
    lines = study._output_bytes(app, child, "git-log.txt").decode().splitlines()
    assert [l.split()[0] for l in lines] == [child, parent]


def test_a_missing_parent_is_a_refusal_not_a_short_log(prepared):
    att, info = prepared
    app = str(att.app)
    tree = plain(app, "rev-parse", info["sha"] + "^{tree}")
    # a commit object that names a parent the clone does not hold (written by hand: git would refuse commit-tree)
    body = ("tree %s\nparent %s\nauthor m <m@example.org> 1 +0000\ncommitter m <m@example.org> 1 +0000\n\nlonely\n"
            % (tree, "ab" * 20)).encode()
    oid = plain(app, "hash-object", "-t", "commit", "-w", "--stdin", "--literally", input=body)
    with pytest.raises(AttachError, match="is not in the clone"):
        study._output_bytes(app, oid, "git-log.txt")


# ================================================================ class B: one stale flag per map

def test_each_map_has_its_own_stale_flag(prepared):
    att, info = prepared
    root = str(att.root)
    a = info["sha"]
    other = "design/attach/draft.md"
    write_map(att, sha=a)
    (att.root / other).write_text(map_text(a), encoding="utf-8")
    b = upstream_edit(att.upstream, {"src/extra.py": "x = 1\n"})
    study.prepare(root)                                                       # the default map is flagged
    assert study.read_stale(root)["areas"] == ["src"] and study.read_stale(root, other) is None
    study.prepare(root, fetch=False, map_path=other)                          # another map: its own entry
    assert study.read_stale(root, other)["areas"] == ["src"] and study.read_stale(root)["areas"] == ["src"]
    write_map(att, sha=b)
    study.ingest(root)                                                        # the default map reaches the base: its flag clears
    assert study.read_stale(root)["areas"] == [] and study.read_stale(root, other)["areas"] == ["src"]
    assert study.read_stale(root)["served_map"] == MAP and study.read_stale(root)["map"] == MAP
    assert study.read_stale(root, MAP)["areas"] == []
    assert study.flag_stale(root, other) == ["src"] and study.read_stale(root)["areas"] == []


def test_ingest_of_another_map_leaves_the_default_flag_and_says_what_the_wiki_serves(prepared):
    att, info = prepared
    root = str(att.root)
    a = info["sha"]
    write_map(att, sha=a)
    study.ingest(root)
    b = upstream_edit(att.upstream, {"src/extra.py": "x = 1\n"})
    study.prepare(root)
    other = "design/attach/draft.md"
    (att.root / other).write_text(map_text(b), encoding="utf-8")
    study.ingest(root, other)
    flag = study.read_stale(root)
    assert flag["areas"] == ["src"] and flag["since_sha"] == a and flag["served_map"] == other
    assert study.read_stale(root, other)["areas"] == []


def test_a_stale_file_that_is_not_json_is_replaced_not_fatal(prepared):
    att, info = prepared
    root = str(att.root)
    write_map(att, sha=info["sha"])
    (att.root / ".alpaca/attach/study/stale.json").write_text("{not json")
    assert study.flag_stale(root) == []
    assert study.read_stale(root)["areas"] == [] and study.read_stale(root)["schema"] == 2


# ================================================================ class C: deletes that follow a link

@pytest.mark.parametrize("what", ["done-file", "snapshot-folder"])
def test_prune_through_a_study_folder_that_became_a_link_deletes_nothing_outside(prepared, tmp_path, what):
    att, info = prepared
    victim = tmp_path / "victim"
    victim.mkdir()
    if what == "done-file":
        (victim / "aaaaaaaaaaaa.json").write_text("keep")
    else:
        (victim / "bbbbbbbbbbbb").mkdir()
        (victim / "bbbbbbbbbbbb" / "f").write_text("keep")
    st = att.root / ".alpaca/attach/study"
    os.rename(st, st.with_name("study-moved"))
    os.symlink(victim, st)
    with pytest.raises(AttachError, match="is a link"):
        study._prune(str(att.root), {"0" * 12})
    if what == "done-file":
        assert (victim / "aaaaaaaaaaaa.json").read_text() == "keep"
    else:
        assert (victim / "bbbbbbbbbbbb" / "f").read_text() == "keep"


def test_prune_removes_old_snapshots_outputs_and_done_files_and_keeps_the_rest(prepared):
    att, info = prepared
    root = att.root
    old = "0123456789ab"
    (root / ".alpaca/attach/study" / old).mkdir()
    (root / ".alpaca/attach/study" / old / "f").write_text("x")
    (root / ".alpaca/attach/study" / (old + ".json")).write_text("{}")
    (root / OUT / old).mkdir()
    (root / OUT / old / "git-log.txt").write_text("x\n")
    gone = study._prune(str(root), {info["sha12"]})
    assert gone == [old]
    names = sorted(os.listdir(root / ".alpaca/attach/study"))
    assert old not in names and old + ".json" not in names and info["sha12"] in names and "outputs" in names
    assert os.listdir(root / OUT) == [info["sha12"]]


# ================================================================ the batch reader on its own

def _record(kind, body):
    import hashlib
    oid = hashlib.sha1(b"%s %d\0" % (kind, len(body)) + body).hexdigest()
    return oid, oid.encode() + b" %s %d\n" % (kind, len(body)) + body + b"\n"


def test_parse_batch_accepts_true_records_and_missing_ones():
    a, ra = _record(b"blob", b"one\n")
    b, rb = _record(b"blob", b"")
    gone = "c" * 40
    got = study._parse_batch(ra + (gone.encode() + b" missing\n") + rb, [a, gone, b])
    assert got == {a: ("blob", b"one\n"), gone: None, b: ("blob", b"")}


@pytest.mark.parametrize("name, mutate", [
    ("changed byte", lambda r: r.replace(b"one", b"two")),
    ("size lies", lambda r: r.replace(b" 4\n", b" 3\n")),
    ("other type", lambda r: r.replace(b" blob ", b" tree ")),
    ("cut", lambda r: r[:-3]),
    ("left over", lambda r: r + b"extra"),
    ("not a number", lambda r: r.replace(b" 4\n", b" x\n")),
])
def test_parse_batch_refuses_a_record_that_is_not_its_id(name, mutate):
    a, ra = _record(b"blob", b"one\n")
    with pytest.raises(AttachError):
        study._parse_batch(mutate(ra), [a])


def test_parse_batch_refuses_a_record_for_another_id_or_in_the_wrong_order():
    a, ra = _record(b"blob", b"one\n")
    b, rb = _record(b"blob", b"two\n")
    with pytest.raises(AttachError):
        study._parse_batch(rb + ra, [a, b])


def test_later_events_do_not_push_a_recorded_base_out_of_the_window(prepared, monkeypatch):
    """`stale` and `ingest` events share the kind with `prepare`; only prepare events count against the window."""
    att, info = prepared
    root = str(att.root)
    monkeypatch.setattr(study, "HISTORY_EVENTS", 3)
    for i in range(6):
        study._event(root, "cli", {"action": "stale", "map": MAP, "areas": [], "n": i})
    assert study._recorded_bases(root) == [info["sha"]]
    assert study.check_map_pointer(root, "app@src/main.py:1", info["sha"]).ok is True
