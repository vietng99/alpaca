"""`alpaca verify` sees the attached repo (op-062 t-650, R6, AC-07; pre-mortem t-650.2).

A verification-scope seal of an attach task keeps the work-branch diff as `app.patch` beside `tree.patch`.
The verifier lays the attached repo at the sealed base into each try's `tree/app` and `base/app`, checks the
kept patch against the sealed hash and applies it in `tree/`. A changed kept patch is a stale seal.
"""
import hashlib
import inspect
import os
import subprocess
import sys

import pytest

from alpaca import db, proof, util
from alpaca.research import verifier, verify_gate
from alpaca.tests.attachkit import attached, git, make_upstream, studio, upstream  # noqa: F401  (fixtures)
from alpaca.tests.attachproofkit import commit_in, cut, latest_seal, prepare
from alpaca.tests.test_research_verifier import (MAKE_LOG, Scripted, _rerun_jail, events_of,  # noqa: F401
                                                 run_verify, seal, stand_in_binary, write_report)


@pytest.fixture
def world(attached, tmp_path):
    util.write_text(os.path.join(str(attached.root), "make_log.py"), MAKE_LOG)
    root = prepare(attached)
    wt = cut(attached)

    class W:
        pass

    w = W()
    w.root, w.att, w.wt, w.binary = root, attached, wt, stand_in_binary(tmp_path)
    return w


def sealed(w):
    write_report(w.root)
    return seal(w.root)


def run(w, plan=None):
    s = Scripted(w.root, plan)
    return s, run_verify(w.root, s, binary=w.binary)


def probe(seen):
    """A scripted verifier step: record what its try folder holds of the attached repo."""
    def before(folder):
        def read(rel):
            path = os.path.join(folder, rel)
            return open(path, encoding="utf-8").read() if os.path.isfile(path) else None
        seen.append({"tree_main": read("tree/app/src/main.py"), "base_main": read("base/app/src/main.py"),
                     "tree_new": read("tree/app/src/new.py"), "base_new": read("base/app/src/new.py"),
                     "tree_readme": read("tree/app/README.md"), "base_readme": read("base/app/README.md")})
    return lambda slot, rnd, try_: {"before_submit": before}


def test_verify_applies_the_app_patch_and_passes(world):
    commit_in(world.wt, {"src/main.py": "print('changed')\n"})
    (world.wt / "src" / "new.py").write_text("x = 1\n")
    payload = sealed(world)
    assert payload["tree"]["app"]["bytes"] > 0
    seen = []
    s, got = run(world, probe(seen))
    assert got["state"] == "verified" and got["outcome"] == "verified", got
    assert len(seen) == 2
    for facts in seen:
        assert facts["tree_main"] == "print('changed')\n" and facts["base_main"] == "print('hello')\n"
        assert facts["tree_new"] == "x = 1\n" and facts["base_new"] is None
        assert facts["tree_readme"] == facts["base_readme"] == "# demo app\n"
    opened = events_of(world.root, "research-verify-open")[0]["data"]["tree"]
    assert opened["app_patch_sha256"] == payload["tree"]["app"]["patch_sha256"]
    assert opened["app_base"] == payload["tree"]["app"]["base"]
    end = events_of(world.root, "research-verify-end")[0]["data"]
    assert end["app_patch_sha256"] == payload["tree"]["app"]["patch_sha256"]
    assert end["tree_patch_sha256"] == payload["tree"]["patch_sha256"]


def test_one_byte_appended_to_the_kept_app_patch_stops_with_verify_tree(world):
    commit_in(world.wt, {"src/main.py": "print('changed')\n"})
    payload = sealed(world)
    with open(os.path.join(world.root, payload["tree"]["app"]["patch"]), "ab") as fh:
        fh.write(b"\n")
    s, got = run(world)
    assert got["state"] == "stopped" and got["code"] == "VERIFY-TREE", got
    stop = events_of(world.root, "research-verify-refused")[-1]["data"]["problems"][0]
    assert stop["rule"] == verify_gate.SEALED_RULE and "app.patch" in stop["detail"]
    assert not s.specs and not events_of(world.root, "research-verify-end")


def test_an_app_patch_that_does_not_apply_stops_with_verify_tree(world, monkeypatch):
    """The seal is honest about its bytes (the hash matches) but the patch is not against the base tree."""
    from alpaca.attach import proofhook
    real = proofhook.take_app_patch
    evil = ("diff --git a/src/main.py b/src/main.py\n--- a/src/main.py\n+++ b/src/main.py\n"
            "@@ -1 +1 @@\n-print('not the base')\n+print('x')\n").encode()

    def bad(root, task):
        head, _patch, base, tree_id = real(root, task)
        return head, evil, base, tree_id

    monkeypatch.setattr(proofhook, "take_app_patch", bad)
    sealed(world)
    s, got = run(world)
    assert got["state"] == "stopped" and got["code"] == "VERIFY-TREE", got
    stop = events_of(world.root, "research-verify-refused")[-1]["data"]["problems"][0]
    assert "app.patch does not apply" in stop["detail"] and stop["rule"] == verify_gate.SEALED_RULE
    assert not s.specs


def test_the_end_event_must_name_the_sealed_app_patch(world):
    commit_in(world.wt, {"src/main.py": "print('changed')\n"})
    sealed(world)
    s, got = run(world)
    assert got["outcome"] == "verified"
    conn = db.connect(world.root)
    try:
        seal_ev = proof.latest_seal(conn, "t-001")
        open_ev = verify_gate.events(conn, "research-verify-open", "t-001")[0]
        end_ev = verify_gate.events(conn, "research-verify-end", "t-001")[0]
        assert verify_gate.consistency(conn, world.root, "t-001", seal_ev, open_ev, end_ev) == []
        forged = dict(end_ev, data=dict(end_ev["data"], app_patch_sha256="0" * 64))
        assert any("app patch" in p for p in verify_gate.consistency(conn, world.root, "t-001", seal_ev, open_ev, forged))
        dropped = dict(end_ev, data={k: v for k, v in end_ev["data"].items() if k != "app_patch_sha256"})
        assert any("app patch" in p for p in verify_gate.consistency(conn, world.root, "t-001", seal_ev, open_ev, dropped))
    finally:
        conn.close()


def test_an_empty_app_diff_verifies(world):
    payload = sealed(world)
    assert payload["tree"]["app"]["bytes"] == 0
    seen = []
    s, got = run(world, probe(seen))
    assert got["outcome"] == "verified", got
    assert seen and all(f["tree_main"] == f["base_main"] == "print('hello')\n" for f in seen)


def test_export_ignore_in_the_attached_repo_does_not_drop_files_from_the_base(attached, tmp_path):
    """git archive honours export-ignore; the verifier lays the stored blobs, so tests/ is there."""
    attached.upstream.commit({".gitattributes": "tests/ export-ignore\n*.md export-ignore\n",
                              "tests/test_a.py": "def test_a():\n    assert True\n"}, "attrs")
    from alpaca.attach import gitops
    gitops.git(attached.app, "fetch", "-q", "origin")
    gitops.git(attached.app, "merge", "-q", "--ff-only", "origin/main")
    util.write_text(os.path.join(str(attached.root), "make_log.py"), MAKE_LOG)
    root = prepare(attached)
    wt = cut(attached)
    commit_in(wt, {"tests/test_a.py": "def test_a():\n    assert 1 == 1\n"})
    write_report(root)
    seal(root)
    seen = []

    def before(folder):
        seen.append(open(os.path.join(folder, "tree", "app", "tests", "test_a.py")).read())

    got = run_verify(root, Scripted(root, lambda s, r, t: {"before_submit": before}), binary=stand_in_binary(tmp_path))
    assert got["outcome"] == "verified", got
    assert seen and all("1 == 1" in text for text in seen)


def test_a_base_link_that_leaves_the_tree_stops_the_cut(attached, tmp_path):
    work = attached.upstream.work
    os.symlink("/etc", work / "etc-link")
    git(work, "add", "-A")
    git(work, "commit", "-q", "-m", "link")
    git(work, "push", "-q", "origin", "main")
    from alpaca.attach import gitops
    gitops.git(attached.app, "fetch", "-q", "origin")
    gitops.git(attached.app, "merge", "-q", "--ff-only", "origin/main")
    util.write_text(os.path.join(str(attached.root), "make_log.py"), MAKE_LOG)
    root = prepare(attached)
    wt = cut(attached)
    commit_in(wt, {"src/main.py": "print('changed')\n"})
    write_report(root)
    seal(root)
    s = Scripted(root)
    got = run_verify(root, s, binary=stand_in_binary(tmp_path))
    assert got["state"] == "stopped" and got["code"] == "VERIFY-TREE", got
    assert "leaves it" in str(events_of(root, "research-verify-refused")[-1]["data"])
    assert not s.specs


# ============================================================================================================
# Fix round 4 (class: bytes of the attached repo written by name into a folder the agent prepared). The verifier
# copies the kept app.patch, lays tree/app and base/app and places app/ evidence in each try's packet. Each of
# those writes is made by descriptor from the real studio root (alpaca/attach/applay.py); a link planted at any
# name of the verification folder takes no product code out of the studio. Tests below use `alpaca verify`
# and the existing kits only; the unit tests at the end import applay.
# ============================================================================================================
SECRET = "SECRET = 'product code'\n"


def _leaked(top):
    out = []
    for here, _dirs, files in os.walk(top):
        for name in files:
            with open(os.path.join(here, name), encoding="utf-8", errors="replace") as fh:
                if "product code" in fh.read():
                    out.append(os.path.relpath(os.path.join(here, name), top))
    return out


def _files_under(top):
    return sorted(os.path.relpath(os.path.join(h, f), top) for h, _d, fs in os.walk(top) for f in fs)


def _seal_with_app_evidence(w):
    (w.att.app / "secret_algo.py").write_text(SECRET)
    commit_in(w.wt, {"src/main.py": "print('changed')\n"})
    write_report(w.root, evidence=["local:logs/run.txt - the saved run output",
                                   "local:app/secret_algo.py - the algorithm"])
    payload = seal(w.root)
    vf = verify_gate.verification_id("t-001", latest_seal(w.root)["id"])
    return payload, verify_gate.verification_dir(w.root, "t-001", vf)


def _try_run(w, plan=None):
    try:
        return run(w, plan)[1]
    except Exception as exc:                                    # noqa: BLE001  a refusal that raises is a stop too
        return {"state": "raised", "error": exc}


def test_p0_control_a_clean_run_places_the_app_evidence_in_each_packet(world):
    payload, folder = _seal_with_app_evidence(world)
    got = _try_run(world)
    assert got.get("outcome") == "verified", got
    placed = [rel for rel in _files_under(folder) if rel.endswith("secret_algo.py")]
    assert len([p for p in placed if "packet/evidence/" in p]) == 2, placed          # the sink is live
    assert all(open(os.path.join(folder, p)).read() == SECRET for p in placed)


@pytest.mark.parametrize("where", ["round", "verification", "try-evidence", "try"])
def test_p1_to_p4_a_link_in_the_verification_folder_takes_no_app_evidence_out(world, tmp_path, where):
    """P1 a link at <try>/packet/evidence, P2 at the try, P3 at the round, P4 at <vf> itself."""
    _payload, folder = _seal_with_app_evidence(world)
    outside = tmp_path / "outside"
    outside.mkdir()
    if where == "verification":
        os.makedirs(os.path.dirname(folder), exist_ok=True)
        os.symlink(str(outside), folder)
    else:
        for slot in ("v1", "v2"):
            target = outside / slot
            target.mkdir()
            if where == "round":
                os.makedirs(os.path.join(folder, slot), exist_ok=True)
                os.symlink(str(target), os.path.join(folder, slot, "r0"))
            elif where == "try":
                os.makedirs(os.path.join(folder, slot, "r0"), exist_ok=True)
                os.symlink(str(target), os.path.join(folder, slot, "r0", "t0"))
            else:
                packet = os.path.join(folder, slot, "r0", "t0", "packet")
                os.makedirs(packet, exist_ok=True)
                os.symlink(str(target), os.path.join(packet, "evidence"))
    _try_run(world)
    assert not _leaked(outside), "verify wrote product bytes through a link (%s): %s" % (where, _leaked(outside))


def test_p5_an_unbound_task_gets_the_same_protection(attached, tmp_path):
    """A task with no attach record has no app patch, so only the evidence copy is on the path."""
    util.write_text(os.path.join(str(attached.root), "make_log.py"), MAKE_LOG)
    root = prepare(attached)
    (attached.app / "secret_algo.py").write_text(SECRET)
    write_report(root, evidence=["local:logs/run.txt - the saved run output",
                                 "local:app/secret_algo.py - the algorithm"])
    payload = seal(root)
    assert "app" not in (payload.get("tree") or {})
    folder = verify_gate.verification_dir(root, "t-001", verify_gate.verification_id("t-001", latest_seal(root)["id"]))
    outside = tmp_path / "outside"
    outside.mkdir()
    for slot in ("v1", "v2"):
        os.makedirs(os.path.join(folder, slot), exist_ok=True)
        (outside / slot).mkdir()
        os.symlink(str(outside / slot), os.path.join(folder, slot, "r0"))
    try:
        run_verify(root, Scripted(root, None), binary=stand_in_binary(tmp_path))
    except Exception:                                           # noqa: BLE001
        pass
    assert not _leaked(outside), _leaked(outside)


def test_s2_a_link_at_the_verification_folders_app_patch_is_replaced_not_written_through(world, tmp_path):
    commit_in(world.wt, {"src/main.py": "print('changed')\n"})
    sealed(world)
    folder = verify_gate.verification_dir(world.root, "t-001",
                                          verify_gate.verification_id("t-001", latest_seal(world.root)["id"]))
    os.makedirs(folder, exist_ok=True)
    victim = tmp_path / "victim.txt"
    victim.write_text("original\n")
    os.symlink(str(victim), os.path.join(folder, "app.patch"))
    got = _try_run(world)
    assert victim.read_text() == "original\n"
    assert got.get("outcome") == "verified", got
    assert os.path.isfile(os.path.join(folder, "app.patch")) and not os.path.islink(os.path.join(folder, "app.patch"))


def test_k2_a_link_at_tree_app_is_refused_and_nothing_is_laid_through_it(attached, tmp_path):
    repo = str(attached.app)
    base = git(repo, "rev-parse", "HEAD")
    outside = tmp_path / "outside-lay"
    outside.mkdir()
    tree = tmp_path / "try" / "tree"
    tree.mkdir(parents=True)
    os.symlink(str(outside), str(tree / "app"))
    with pytest.raises(Exception) as exc:
        verifier._lay_app_tree(str(tmp_path), repo, base, str(tree / "app"))
    assert "already holds app/" in str(exc.value), exc.value
    assert _files_under(str(outside)) == []


def test_k3_a_studio_commit_that_tracks_app_as_a_link_stops_the_verification(world, tmp_path):
    outside = tmp_path / "outside-e2e"
    outside.mkdir()
    root = world.root
    blob = subprocess.run(["git", "hash-object", "-w", "--stdin"], cwd=root, input=str(outside),
                          capture_output=True, text=True, check=True).stdout.strip()
    git(root, "update-index", "--add", "--cacheinfo", "120000,%s,app" % blob)
    git(root, "commit", "-q", "-m", "app as a link")
    commit_in(world.wt, {"src/main.py": SECRET})
    try:
        sealed(world)
    except proof.Refusal:
        return                                                  # refused at the seal: also holds it off
    got = _try_run(world)
    assert got.get("outcome") != "verified", got
    assert _files_under(str(outside)) == []


def test_f3_a_kept_app_patch_over_the_cap_is_a_stale_seal_and_is_never_read_whole(world, monkeypatch):
    commit_in(world.wt, {"src/main.py": "print('changed')\n"})
    payload = sealed(world)
    kept = os.path.join(world.root, payload["tree"]["app"]["patch"])
    with open(kept, "wb") as fh:                                # a sparse file one byte over the cap
        fh.truncate(proof.KEPT_FILE_CAP + 1)
    from alpaca import paths
    reads = []
    real = paths.open_checked

    def spy(path, allowed):
        fh = real(path, allowed)
        if fh is None or not os.fspath(path).endswith("app.patch"):
            return fh

        class Spy:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                fh.close()

            def read(self, n=-1):
                reads.append(n)
                return fh.read(n)

            def __getattr__(self, name):
                return getattr(fh, name)
        return Spy()
    monkeypatch.setattr(paths, "open_checked", spy)
    s, got = run(world)
    assert got["state"] == "stopped" and got["code"] == "VERIFY-TREE", got
    assert "app.patch" in str(events_of(world.root, "research-verify-refused")[-1]["data"])
    assert reads and all(n <= proof.KEPT_FILE_CAP + 1 for n in reads), reads             # one bounded read, never to the end
    assert not s.specs


def test_f6_the_verifier_stops_when_the_attach_code_cannot_be_loaded(world, monkeypatch):
    commit_in(world.wt, {"src/main.py": "print('changed')\n"})
    sealed(world)
    import alpaca.attach
    monkeypatch.delattr(alpaca.attach, "applay", raising=False)
    monkeypatch.setitem(sys.modules, "alpaca.attach.applay", None)      # `import` of it raises ImportError
    got = _try_run(world)
    assert got.get("code") == "VERIFY-TREE" or "VERIFY-TREE" in str(got.get("error", "")), got


def test_an_attach_verification_writes_no_app_bytes_by_name(world, monkeypatch):
    """Caller-agnostic: no Python call writes to <vf>/app.patch, tree/app, base/app or a packet's evidence folder
    by path; the studio's old writers are patched to fail, and the attach functions run."""
    from alpaca.attach import applay
    from alpaca.tests.attachtrace import Tracer
    payload, folder = _seal_with_app_evidence(world)
    used = []
    for name in ("copy_patch", "read_patch", "lay", "place_copy"):
        real = getattr(applay, name)
        monkeypatch.setattr(applay, name, (lambda real, name: lambda *a, **k: (used.append(name), real(*a, **k))[1])(real, name))
    tracer = Tracer()
    with monkeypatch.context() as mp:
        tracer.install(mp)
        s, got = run(world)
    assert got.get("outcome") == "verified", got
    assert tracer.calls > 0 and not tracer.hits, tracer.describe()
    assert set(used) == {"copy_patch", "read_patch", "lay", "place_copy"}, used
    assert "os.makedirs" not in inspect.getsource(verifier._lay_app_tree)


def test_the_tracer_sees_the_plain_verifier(project, tmp_path, monkeypatch):
    """Liveness for the test above: a plain project's verification writes into packet/evidence/ by name."""
    from alpaca.tests.attachtrace import Tracer
    from alpaca.tests.test_research_verifier import setup_project
    root = setup_project(project)
    write_report(root)
    seal(root)
    tracer = Tracer()
    with monkeypatch.context() as mp:
        tracer.install(mp)
        got = run_verify(root, Scripted(root, None), binary=stand_in_binary(tmp_path))
    assert got["outcome"] == "verified", got
    assert [h for h in tracer.hits if "/packet/evidence/" in h[1]], tracer.describe()


# ------------------------------------------------------------------------------------------ applay itself
def test_lay_writes_files_with_modes_then_links_and_refuses_a_path_below_a_link_or_named_twice(tmp_path):
    from alpaca.attach import AttachError, applay, safefs
    entries = [("100644", "a", "d/x.txt"), ("100755", "b", "d/run.sh"), ("120000", "c", "d/link"),
               ("100644", "a", "top.txt")]
    blobs = {"a": b"alpha\n", "b": b"#!/bin/sh\n", "c": b"x.txt"}
    box = tmp_path / "box"
    (box / "try" / "tree").mkdir(parents=True)
    applay.lay(str(box), str(box / "try" / "tree" / "app"), entries, blobs)
    app = box / "try" / "tree" / "app"
    assert (app / "d" / "x.txt").read_bytes() == b"alpha\n" and (app / "top.txt").read_bytes() == b"alpha\n"
    assert os.access(app / "d" / "run.sh", os.X_OK) and not os.access(app / "d" / "x.txt", os.X_OK)
    assert os.readlink(app / "d" / "link") == "x.txt"
    with pytest.raises(AttachError, match="already holds"):                 # the folder is claimed exclusively
        applay.lay(str(box), str(app), entries, blobs)
    below = [("120000", "c", "l"), ("120000", "c", "l/m")]
    with pytest.raises(AttachError, match="below a link"):
        applay.lay(str(box), str(box / "try" / "tree" / "app2"), below, blobs)
    twice = [("100644", "a", "same"), ("120000", "c", "same")]
    with pytest.raises(AttachError, match="names a path twice"):
        applay.lay(str(box), str(box / "try" / "tree" / "app3"), twice, blobs)
    outside = tmp_path / "outside"
    outside.mkdir()
    os.symlink(str(outside), box / "try" / "tree" / "app4")
    with pytest.raises(AttachError, match="already holds"):
        applay.lay(str(box), str(box / "try" / "tree" / "app4"), entries, blobs)
    assert os.listdir(outside) == []


def test_read_patch_is_bounded_and_checks_the_hash(tmp_path):
    from alpaca.attach import applay
    studio_root = tmp_path / "studio"
    (studio_root / "vf").mkdir(parents=True)
    data = b"diff --git a/x b/x\n"
    (studio_root / "vf" / "app.patch").write_bytes(data)
    want = hashlib.sha256(data).hexdigest()
    assert applay.read_patch(str(studio_root), str(studio_root / "vf"), want) == data
    assert applay.read_patch(str(studio_root), str(studio_root / "vf"), "0" * 64) is None
    with open(studio_root / "vf" / "app.patch", "wb") as fh:
        fh.truncate(proof.KEPT_FILE_CAP + 1)
    assert applay.read_patch(str(studio_root), str(studio_root / "vf"), want) is None
    os.remove(studio_root / "vf" / "app.patch")
    os.symlink(str(tmp_path / "elsewhere"), studio_root / "vf" / "app.patch")
    assert applay.read_patch(str(studio_root), str(studio_root / "vf"), want) is None
