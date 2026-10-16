"""The push sends exactly the objects the scan read, and nothing the clone can change afterwards (op-062 t-686, fix round 2, B3).

The adversarial check of round 2 found that the push folder of round 1, which read the clone's objects through
`objects/info/alternates`, also read the clone's commit-graph: a forged graph planted after the last scan gave the approved
head a second parent, and the push sent that parent with a studio file's bytes. The class is "the push folder borrows the
clone's object store": the commit-graph (one file or a split chain, in the clone or in a store the clone names), the
reachability bitmaps, the multi-pack-index and the objects' own bytes all decide what git sends, and the clone can change each
of them after the scan (investigate-r2.md, investigate-r2-probe.log).

The scan and the push now each read a fresh copy of exactly the entering objects (`scan.copy_store`): every object read from the
clone is checked against its name before it is written, the copy has no alternates, refs, hooks, commit-graph, bitmap or
multi-pack-index, its walk must be the walk the scan read, and git runs with `scan.STORE_OFF` and no shallow file.

Each route is planted in the window after the last scan and the last look (the second time execute asks the upstream for the
branch), and the bare upstream's object store is read afterwards. A local bare upstream and a fake GitHub API; no network."""
import os
import shutil
import struct
import zlib
from pathlib import Path

import pytest

from alpaca import barrier
from alpaca.attach import AttachError, gitops, handoff, safefs, scan
from alpaca.tests.attachkit import (FakeGitHub, attached, commit_in, git, owner_approve, ready, studio,  # noqa: F401
                                    upstream)

TASK = "t-001"
BRANCH = "alpaca/t-001"
REF = "refs/heads/" + BRANCH
SIDE = {"GIT_COMMITTER_NAME": "Alpaca agent", "GIT_COMMITTER_EMAIL": "alpaca-agent@localhost",
        "GIT_AUTHOR_NAME": "Alpaca agent", "GIT_AUTHOR_EMAIL": "alpaca-agent@localhost"}


@pytest.fixture
def r(attached):
    return ready(attached)


@pytest.fixture
def gh(r):
    return FakeGitHub(upstream=r.att.upstream)


def reviewed(r):
    owner_approve(r)
    return handoff.plan(r.root, TASK)


def go(r, gh):
    return handoff.execute(r.root, TASK, expect=r.head, api=gh)


def upstream_objects(r) -> set:
    out = git(r.att.upstream.bare, "cat-file", "--batch-all-objects", "--batch-check=%(objectname)")
    return set(out.split())


def in_the_race(monkeypatch, plant):
    """Run `plant()` after the last scan and the last look, right before the push: the second time execute asks the
    upstream for the branch."""
    real = handoff._remote_sha
    seen = []

    def racing(app, url, name):
        if name == REF:
            seen.append(name)
            if len(seen) == 2:
                plant()
        return real(app, url, name)

    monkeypatch.setattr(handoff, "_remote_sha", racing)


def pushes(monkeypatch) -> list:
    """Record each git push: the folder it ran in and the files in it."""
    real = gitops.remote
    seen = []

    def spy(repo, *args, **kw):
        if args and args[0] == "push":
            folder = Path(repo)
            seen.append({"folder": str(folder), "files": sorted(str(p.relative_to(folder)) for p in folder.rglob("*")
                                                                if p.is_file()), "kw": kw, "args": args})
        return real(repo, *args, **kw)

    monkeypatch.setattr(gitops, "remote", spy)
    return seen


def secret_side_commit(r) -> tuple:
    """A blob with the bytes of a studio file, a tree and a commit holding it, written into the clone with no ref."""
    app = r.att.app
    data = (Path(r.root) / ".alpaca" / "instance.json").read_text()
    blob = git(app, "hash-object", "-w", "--stdin", input=data)
    tree = git(app, "mktree", input="100644 blob %s\tleak.txt\n" % blob)
    side = git(app, "commit-tree", tree, "-m", "side", env=SIDE)
    return blob, side


def forge_graph(path, head, secret):
    """Rewrite the commit-graph entry of `head` (format v1, SHA-1) so that its second parent is `secret`."""
    os.chmod(path, 0o644)
    data = bytearray(Path(path).read_bytes())
    assert data[:4] == b"CGPH"
    chunks = {}
    for i in range(data[6] + 1):
        chunks[bytes(data[8 + 12 * i: 12 + 12 * i])] = struct.unpack(">Q", data[12 + 12 * i: 20 + 12 * i])[0]
    oidl, cdat = chunks[b"OIDL"], chunks[b"CDAT"]
    oids = [bytes(data[oidl + 20 * k: oidl + 20 * k + 20]).hex() for k in range((cdat - oidl) // 20)]
    entry = cdat + 36 * oids.index(head)
    assert struct.unpack(">I", data[entry + 24: entry + 28])[0] == 0x70000000
    data[entry + 24: entry + 28] = struct.pack(">I", oids.index(secret))
    Path(path).write_bytes(bytes(data))


def graph_file(store, head, side, split=False) -> str:
    """Write a commit-graph of `head` and `side` into the object folder `store` and return its path."""
    repo = Path(store).parent
    args = ["commit-graph", "write", "--stdin-commits"] + (["--split"] if split else [])
    if repo.name == ".git":
        git(repo.parent, *args, input="%s\n%s\n" % (head, side))
    else:
        git(repo, *args, input="%s\n%s\n" % (head, side))
    if split:
        return str(next(Path(store, "info", "commit-graphs").glob("*.graph")))
    return os.path.join(store, "info", "commit-graph")


def loose_path(repo_objects, sha) -> Path:
    return Path(repo_objects) / sha[:2] / sha[2:]


def write_loose(path, kind, data):
    """Put `kind data` (zlib, as git stores a loose object) at `path`, whatever name it has."""
    os.chmod(path, 0o644)
    path.write_bytes(zlib.compress(b"%s %d\0" % (kind, len(data)) + data))


def leaked(r, blob, side, gh):
    kinds = upstream_objects(r)
    return {"blob": blob in kinds, "side": side in kinds, "pr": bool(gh.calls_of("POST"))}


def outcome(r, gh):
    try:
        return go(r, gh).status
    except AttachError as exc:
        return "refused: " + str(exc)


# ---- the routes that change what a walk from the approved head reaches --------------------------------------------------

@pytest.mark.parametrize("route", ["graph-file", "graph-split-chain", "graph-in-a-store-the-clone-names",
                                   "bitmap-written-under-a-forged-graph"])
def test_a_route_planted_after_the_scan_sends_no_unscanned_object(r, gh, monkeypatch, route):
    """B3 and its siblings: before this change each sent the side commit and the studio blob, with a PUSHED receipt."""
    reviewed(r)
    app = Path(r.att.app)
    store = str(app / ".git" / "objects")
    planted = {}

    def plant():
        blob, side = secret_side_commit(r)
        planted.update(blob=blob, side=side)
        if route == "graph-file":
            forge_graph(graph_file(store, r.head, side), r.head, side)
        elif route == "graph-split-chain":
            forge_graph(graph_file(store, r.head, side, split=True), r.head, side)
        elif route == "graph-in-a-store-the-clone-names":
            other = app.parent / "other-store.git"
            git(app.parent, "init", "-q", "--bare", str(other))
            shutil.copytree(store, str(other / "objects"), dirs_exist_ok=True)
            forge_graph(graph_file(str(other / "objects"), r.head, side), r.head, side)
            (Path(store) / "info").mkdir(exist_ok=True)
            (Path(store) / "info" / "alternates").write_text(str(other / "objects") + "\n")
        else:
            forge_graph(graph_file(store, r.head, side), r.head, side)
            git(app, "repack", "-q", "-a", "-d", "-b")
            os.unlink(os.path.join(store, "info", "commit-graph"))
            assert list(Path(store, "pack").glob("*.bitmap"))

    in_the_race(monkeypatch, plant)
    seen = pushes(monkeypatch)
    status = outcome(r, gh)
    found = leaked(r, planted["blob"], planted["side"], gh)
    assert not found["blob"] and not found["side"], (route, status, found)
    assert status == handoff.PUSHED                                    # the approved head itself still goes out
    assert len(seen) == 1 and "objects/info/alternates" not in seen[0]["files"]
    assert not [f for f in seen[0]["files"] if "commit-graph" in f or f.endswith((".bitmap", "multi-pack-index"))]


def test_bytes_changed_under_a_name_after_the_scan_are_never_sent(r, gh, monkeypatch):
    """The clone's loose root tree of the head rewritten to also name the studio blob (its bytes no longer give its name).
    Before this change git sent the studio blob in the pack (the upstream then refused the push for the missing tree, after
    the bytes had left); now the copy reads the object, finds the bytes are not that object, and nothing is pushed."""
    reviewed(r)
    app = Path(r.att.app)
    planted = {}

    def plant():
        blob, side = secret_side_commit(r)
        planted.update(blob=blob, side=side)
        tree = git(app, "rev-parse", r.head + "^{tree}")
        listing = git(app, "cat-file", "-p", tree) + "\n100644 blob %s\tleak.txt\n" % blob
        other = git(app, "mktree", input=listing)
        write_loose(loose_path(app / ".git" / "objects", tree), b"tree",
                    zlib.decompress(loose_path(app / ".git" / "objects", other).read_bytes()).split(b"\0", 1)[1])

    in_the_race(monkeypatch, plant)
    seen = pushes(monkeypatch)
    status = outcome(r, gh)
    assert status.startswith("refused") and "are not that object" in status, status
    assert seen == [] and gh.calls == []
    assert planted["blob"] not in upstream_objects(r)
    assert not [p for p, rec in handoff.handoffs(r.root, TASK) if rec.status != handoff.DRY_RUN]


def test_bytes_changed_under_a_name_while_the_scan_reads_are_refused(attached, monkeypatch):
    """A blob holding the studio's path, swapped for clean bytes in the clone's object folder while each scan runs and put
    back after it. Before this change the scan read the clean bytes, passed, and the push sent the real ones."""
    root = os.path.realpath(str(attached.root))
    r = ready(attached, files={"src/feature.py": "WHERE = %r\n" % root})
    gh = FakeGitHub(upstream=r.att.upstream)
    app = Path(r.att.app)
    blob = git(app, "rev-parse", "%s:src/feature.py" % r.head)
    path = loose_path(app / ".git" / "objects", blob)
    real_bytes = path.read_bytes()
    real_outgoing = scan.outgoing

    def swapped(*a, **kw):
        write_loose(path, b"blob", b"WHERE = 'elsewhere'\n")
        try:
            return real_outgoing(*a, **kw)
        finally:
            path.write_bytes(real_bytes)

    monkeypatch.setattr(scan, "outgoing", swapped)
    owner_approve(r)
    try:
        handoff.plan(r.root, TASK)
        status = outcome(r, gh)
    except AttachError as exc:
        status = "refused: " + str(exc)
    assert status.startswith("refused") and "are not that object" in status, status
    assert blob not in upstream_objects(r) and gh.calls == []


# ---- what is not a route (probes F, G, H of investigate-r2-probe.log), kept as regression tests ------------------------

@pytest.mark.parametrize("route", ["replace-ref", "grafts", "shallow"])
def test_replace_refs_grafts_and_shallow_files_planted_after_the_scan_send_nothing_more(r, gh, monkeypatch, route):
    reviewed(r)
    app = Path(r.att.app)
    base = r.ck.preview_base
    planted = {}

    def plant():
        blob, side = secret_side_commit(r)
        planted.update(blob=blob, side=side)
        if route == "replace-ref":
            raw = git(app, "cat-file", "commit", r.head)
            lines = raw.split("\n")
            at = max(i for i, line in enumerate(lines) if line.startswith("parent ") or line.startswith("tree "))
            lines.insert(at + 1, "parent " + side)
            other = git(app, "hash-object", "-t", "commit", "-w", "--stdin", input="\n".join(lines) + "\n")
            git(app, "replace", r.head, other)
        elif route == "grafts":
            (app / ".git" / "info").mkdir(exist_ok=True)
            (app / ".git" / "info" / "grafts").write_text("%s %s %s\n" % (r.head, base, side))
        else:
            (app / ".git" / "shallow").write_text(base + "\n")

    in_the_race(monkeypatch, plant)
    outcome(r, gh)
    found = leaked(r, planted["blob"], planted["side"], gh)
    assert not found["blob"] and not found["side"], (route, found)


# ---- the copies themselves --------------------------------------------------------------------------------------------

def test_a_file_planted_in_the_push_folder_at_the_push_is_reported_unverified(r, gh, monkeypatch):
    """The residue this design cannot close: a same-user process that writes into the fresh push folder between its proof
    and git's read. git runs with the commit-graph, bitmaps and multi-pack-index off, so an alternates line naming a clone
    with a forged graph sends nothing more; the folder's state after the push differs from its proof, so the receipt is
    PUSHED_UNVERIFIED and no pull request is opened."""
    reviewed(r)
    app = Path(r.att.app)
    store = str(app / ".git" / "objects")
    blob, side = secret_side_commit(r)
    forge_graph(graph_file(store, r.head, side), r.head, side)
    real = gitops.remote

    def plant_then_push(repo, *args, **kw):
        if args and args[0] == "push":
            (Path(repo) / "objects" / "info").mkdir(exist_ok=True)
            (Path(repo) / "objects" / "info" / "alternates").write_text(store + "\n")
        return real(repo, *args, **kw)

    monkeypatch.setattr(gitops, "remote", plant_then_push)
    status = outcome(r, gh)
    assert blob not in upstream_objects(r) and side not in upstream_objects(r)
    assert status.startswith("refused") and "changed while git read it" in status, status
    assert handoff.handoffs(r.root, TASK)[-1][1].status == handoff.PUSHED_UNVERIFIED and gh.calls == []


def test_a_file_planted_in_the_scans_copy_while_it_is_read_refuses(r, monkeypatch):
    owner_approve(r)
    real = barrier.scan

    def plant_then_scan(root, *a, **kw):
        if os.path.realpath(str(root)).startswith(os.path.realpath(os.path.join(r.root, scan.COPY_DIR))):
            Path(root, "objects", "info").mkdir(exist_ok=True)
            Path(root, "objects", "info", "alternates").write_text(os.path.join(r.att.app, ".git", "objects") + "\n")
        return real(root, *a, **kw)

    monkeypatch.setattr(barrier, "scan", plant_then_scan)
    with pytest.raises(AttachError, match="changed while it was read"):
        handoff.plan(r.root, TASK)
    assert safefs.list_names(r.root, scan.COPY_DIR) == []


def test_the_push_folder_holds_only_the_scanned_objects_and_the_base_side_it_walks(r, gh, monkeypatch):
    reviewed(r)
    real_remote = gitops.remote
    held = {}

    def spy(repo, *args, **kw):
        if args and args[0] == "push":
            out = git(repo, "cat-file", "--batch-all-objects", "--batch-check=%(objectname) %(objecttype)")
            held.update(dict(line.split(" ") for line in out.splitlines()))
        return real_remote(repo, *args, **kw)

    monkeypatch.setattr(gitops, "remote", spy)
    before = upstream_objects(r)
    assert go(r, gh).status == handoff.PUSHED
    scanned = {sha for sha, _p in barrier.objects_entering(str(r.att.app), r.head, r.ck.preview_base)}
    added = upstream_objects(r) - before
    assert added == scanned
    # everything else in the folder is the upstream's own: commits and trees of the base side, no blob
    rest = {sha: kind for sha, kind in held.items() if sha not in scanned}
    assert rest and set(rest.values()) <= {"commit", "tree"} and set(rest) <= before


# ---- copy_store on plain repositories ---------------------------------------------------------------------------------

def plain_repo(tmp_path, name="repo", fmt=None):
    path = tmp_path / name
    git(tmp_path, "init", "-q", "-b", "main", *(["--object-format=" + fmt] if fmt else []), str(path))
    return path


def test_the_copy_of_a_branch_that_forked_behind_the_base_walks_like_the_clone(tmp_path, studio):
    """The upstream base moved on after the branch forked, and the branch merged one upstream commit: the copy holds the
    base side commits and the boundary trees, and its walk is the clone's."""
    repo = plain_repo(tmp_path)
    commit_in(repo, {"a/one.txt": "1\n", "b/two.txt": "2\n", "c/d/three.txt": "3\n"}, "base 1")
    fork = git(repo, "rev-parse", "HEAD")
    commit_in(repo, {"b/two.txt": "2b\n"}, "upstream 2")
    middle = git(repo, "rev-parse", "HEAD")
    commit_in(repo, {"c/d/three.txt": "3c\n"}, "upstream 3")
    base = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-q", "-b", "work", fork)
    commit_in(repo, {"a/one.txt": "1w\n", "new/file.txt": "n\n"}, "work 1")
    git(repo, "merge", "-q", "--no-edit", middle, env=SIDE)
    commit_in(repo, {"c/d/four.txt": "4\n"}, "work 2")
    head = git(repo, "rev-parse", "HEAD")
    rel, folder, walk, _state = scan.copy_store(studio, str(repo), head, base, "plain")
    try:
        assert {s for s, _p in walk} == {s for s, _p in barrier.objects_entering(str(repo), head, base)}
        assert {s for s, _p in scan.object_walk(folder, head, base)} == {s for s, _p in walk}
        assert not os.path.exists(os.path.join(folder, "objects", "info", "alternates"))
        assert git(folder, "for-each-ref") == ""
    finally:
        safefs.remove_tree(studio, rel)
    assert safefs.list_names(studio, scan.COPY_DIR) == []


def test_the_copy_works_for_a_sha256_repository(tmp_path, studio):
    repo = plain_repo(tmp_path, fmt="sha256")
    commit_in(repo, {"x.txt": "x\n"}, "base")
    base = git(repo, "rev-parse", "HEAD")
    commit_in(repo, {"y/z.txt": "z" * 5000 + "\n"}, "work")
    head = git(repo, "rev-parse", "HEAD")
    assert len(head) == 64
    rel, folder, walk, _state = scan.copy_store(studio, str(repo), head, base, "sha256")
    try:
        assert len(walk) == 4 and {s for s, _p in scan.object_walk(folder, head, base)} == {s for s, _p in walk}
    finally:
        safefs.remove_tree(studio, rel)


def test_read_objects_refuses_bytes_that_do_not_give_their_name(tmp_path):
    repo = plain_repo(tmp_path)
    commit_in(repo, {"x.txt": "the real bytes\n"}, "one")
    blob = git(repo, "rev-parse", "HEAD:x.txt")
    assert scan.read_objects(str(repo), [blob])[blob] == (b"blob", b"the real bytes\n")
    write_loose(loose_path(repo / ".git" / "objects", blob), b"blob", b"other bytes\n")
    with pytest.raises(AttachError, match="are not that object"):
        scan.read_objects(str(repo), [blob])


def test_the_object_walk_reads_no_commit_graph_bitmap_replace_ref_graft_or_shallow_file(tmp_path):
    repo = plain_repo(tmp_path)
    commit_in(repo, {"x.txt": "1\n"}, "base")
    base = git(repo, "rev-parse", "HEAD")
    commit_in(repo, {"y.txt": "2\n"}, "work")
    head = git(repo, "rev-parse", "HEAD")
    truth = {s for s, _p in scan.object_walk(str(repo), head, base)}
    blob = git(repo, "hash-object", "-w", "--stdin", input="secret\n")
    side = git(repo, "commit-tree", git(repo, "mktree", input="100644 blob %s\tl.txt\n" % blob), "-m", "s", env=SIDE)
    store = str(repo / ".git" / "objects")
    forge_graph(graph_file(store, head, side), head, side)
    assert blob in git(repo, "rev-list", "--objects", head, "--not", base)          # plain git follows the forged graph
    assert {s for s, _p in scan.object_walk(str(repo), head, base)} == truth
    (repo / ".git" / "info").mkdir(exist_ok=True)
    (repo / ".git" / "info" / "grafts").write_text("%s %s %s\n" % (head, base, side))
    (repo / ".git" / "shallow").write_text(head + "\n")
    assert {s for s, _p in scan.object_walk(str(repo), head, base)} == truth
