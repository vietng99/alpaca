"""What an attach seal keeps and where (op-062 t-650 fix round 4; class: bytes of the attached repo written by
name into a folder the agent prepared).

An attach seal builds its kept folder fresh, writes every file by descriptor and swaps the folder in last
(alpaca/attach/sealkeep.py). These tests plant a link, a hard link or a wrong kind of file at every name the
plain writers use, before the seal, and check three things each time: nothing outside the studio changes, every
kept file is a regular file whose sha256 is the sealed value, and a seal that cannot keep its files is refused
before the record event and leaves the latest seal's folder as it was.

Everything here goes through `proof.seal` and the existing kits, so the file runs against an older head too
(the negative control of the fix report): no top-level import of the new modules.
"""
import ast
import copy
import hashlib
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from alpaca import db, paths, proof, util
from alpaca.tests import proofkit
from alpaca.tests.attachkit import attached, git, studio, upstream  # noqa: F401  (fixtures)
from alpaca.tests.attachproofkit import commit_in, cut, prepare
from alpaca.tests.attachtrace import Tracer
from alpaca.tests.test_attach_proof import task  # noqa: F401  (fixture)

SECRET = "SECRET = 'product code'\n"
BASE = "0b433ab2c"                                    # the merge of t-647: the plain writers' last change


def write(root, evidence, *, verify=True, ident="t-001"):
    """The report for `ident`, filled in, with a reproduce command when the seal is in verification scope."""
    conn = db.connect(root)
    try:
        path = proofkit.write_report(root, ident, conn=conn, evidence=evidence)
        if verify:
            text = util.read_text(path)
            util.write_text(path, text.replace("## How to reproduce\n", "## How to reproduce\n\n```\n"
                                               "python3 make_log.py > logs/run.txt 2>&1\n```\n", 1))
    finally:
        conn.close()


def seal_now(root, *, verify=True, ident="t-001"):
    conn = db.connect(root)
    try:
        return proof.seal(conn, root, ident, session="builder", verify=verify)
    finally:
        conn.close()


def seal_events(root, ident="t-001"):
    conn = db.connect(root)
    try:
        return len([e for e in db.events(conn, kind=proof.KIND, limit=10 ** 9) if e.get("ref") == ident])
    finally:
        conn.close()


def kept_of(root, ident="t-001"):
    conn = db.connect(root)
    try:
        return proof.kept_dir(root, proof.subject(conn, ident))
    finally:
        conn.close()


def evidence_for(root, att):
    """One served file and one file of the attached repo, cited the way a report cites them."""
    (att.app / "secret_algo.py").write_text(SECRET)
    return [proofkit.evidence_file(root, "t-001") + " - the run output", "local:app/secret_algo.py - the algorithm"]


def sha(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def snapshot(top):
    """{relative path: sha256, or 'dir', or '-> target'} of everything below `top`, no link followed."""
    out = {}
    for here, dirs, files in os.walk(top):
        for name in dirs + files:
            full = os.path.join(here, name)
            rel = os.path.relpath(full, top)
            st = os.lstat(full)
            if stat.S_ISLNK(st.st_mode):
                out[rel] = "-> " + os.readlink(full)
            elif stat.S_ISDIR(st.st_mode):
                out[rel] = "dir"
            else:
                out[rel] = sha(full)
    return out


def modes(top):
    return {os.path.relpath(os.path.join(h, n), top): stat.S_IMODE(os.lstat(os.path.join(h, n)).st_mode)
            for h, d, f in os.walk(top) for n in f}


def assert_kept_is_clean(root, payload, ident="t-001"):
    assert_kept_is_clean_except(root, payload, set(), ident)


def assert_kept_is_clean_except(root, payload, tolerated, ident="t-001"):
    """Every kept file is a regular file with one name whose sha256 is the sealed value; no leftover."""
    kept = kept_of(root, ident)
    wanted = {}
    for item in payload["evidence"]:
        if item.get("kept"):
            wanted[item["kept"]] = item["sha256"]
    tree = payload.get("tree") or {}
    if tree:
        wanted[tree["patch"]] = tree["patch_sha256"]
        if tree.get("app"):
            wanted[tree["app"]["patch"]] = tree["app"]["patch_sha256"]
    assert wanted, "the seal kept nothing, so the check proves nothing"
    for rel, want in wanted.items():
        full = os.path.join(root, rel)
        st = os.lstat(full)
        assert stat.S_ISREG(st.st_mode) and st.st_nlink == 1, (rel, oct(st.st_mode), st.st_nlink)
        assert sha(full) == want, rel
    for here, dirs, files in os.walk(kept):
        for name in dirs + files:
            st = os.lstat(os.path.join(here, name))
            assert not stat.S_ISLNK(st.st_mode), os.path.join(here, name)
            if stat.S_ISREG(st.st_mode):
                assert st.st_nlink == 1, os.path.join(here, name)
    left = [n for n in os.listdir(os.path.dirname(kept)) if n.startswith(".%s." % os.path.basename(kept))]
    assert not [n for n in left if n not in tolerated], left


@pytest.fixture
def victim(tmp_path):
    path = tmp_path / "victim.txt"
    path.write_text("original\n")
    return path


@pytest.fixture
def outside(tmp_path):
    folder = tmp_path / "outside"
    folder.mkdir()
    (folder / "important.txt").write_text("keep me\n")
    return folder


def plant(kind, victim, path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if kind == "symlink":
        os.symlink(str(victim), path)
    else:
        os.link(str(victim), path)


# --------------------------------------------------------------------- C2, C2b, C3, S1, S5 (promoted probes)
def test_c2_a_linked_closed_folder_carries_no_app_evidence_outside(task, outside):
    att, root, wt = task
    evidence = evidence_for(root, att)
    kept = kept_of(root)
    os.makedirs(kept, exist_ok=True)
    os.symlink(str(outside), os.path.join(kept, proof.CLOSED_KEPT))
    write(root, evidence)
    payload = seal_now(root)
    assert sorted(os.listdir(outside)) == ["important.txt"], os.listdir(outside)      # C2 and C2b: kept, none added
    assert (outside / "important.txt").read_text() == "keep me\n"
    assert os.path.isdir(os.path.join(kept, proof.CLOSED_KEPT)) and not os.path.islink(os.path.join(kept, proof.CLOSED_KEPT))
    assert_kept_is_clean(root, payload)


def test_c3_a_link_or_hard_link_at_the_closed_part_name_is_not_written_through(task, victim):
    att, root, wt = task
    evidence = evidence_for(root, att)
    write(root, evidence)
    closed = os.path.join(kept_of(root), proof.CLOSED_KEPT)
    plant("symlink", victim, os.path.join(closed, ".02-secret_algo.py.part"))
    os.link(str(victim), os.path.join(closed, ".app.patch.part"))
    payload = seal_now(root)
    assert victim.read_text() == "original\n"
    assert_kept_is_clean(root, payload)


def test_s1_an_unbound_task_of_an_attach_project_keeps_the_same_way(attached, outside):
    root = prepare(attached)                       # no worktree is cut: the task has no attach record
    evidence = evidence_for(root, attached)
    kept = kept_of(root)
    os.makedirs(kept, exist_ok=True)
    os.symlink(str(outside), os.path.join(kept, proof.CLOSED_KEPT))
    write(root, evidence, verify=False)
    payload = seal_now(root, verify=False)
    assert sorted(os.listdir(outside)) == ["important.txt"]
    assert_kept_is_clean(root, payload)
    item = [e for e in payload["evidence"] if "secret_algo" in e["pointer"]][0]
    assert "/.closed/" in item["kept"], item


def test_s3_a_link_at_the_seal_copy_name_is_replaced_not_written_through(task, victim):
    att, root, wt = task
    write(root, None)
    report = proof.report_path(root, {"id": "t-001", "op": "op-001"})
    os.symlink(str(victim), report + ".seal.json")
    seal_now(root)
    assert victim.read_text() == "original\n"
    assert os.path.isfile(report + ".seal.json") and not os.path.islink(report + ".seal.json")


# ----------------------------------------------------- a link or hard link at every name a plain seal writes
SERVED = [".tree.patch.part", "tree.patch", "01-t-001.txt", ".01-t-001.txt.part", ".app.patch.part", "app.patch"]
CLOSED = ["02-secret_algo.py", ".02-secret_algo.py.part", "app.patch", ".app.patch.part"]


@pytest.mark.parametrize("kind", ["symlink", "hardlink"])
@pytest.mark.parametrize("where,name", [("", n) for n in SERVED] + [(".closed", n) for n in CLOSED])
def test_a_planted_name_in_the_kept_folder_changes_nothing_outside(task, victim, where, name, kind):
    att, root, wt = task
    evidence = evidence_for(root, att)
    write(root, evidence)
    kept = kept_of(root)
    plant(kind, victim, os.path.join(kept, where, name))
    before = seal_events(root)
    try:
        payload = seal_now(root)
    except proof.Refusal:
        assert seal_events(root) == before                    # refused: no new record event
    else:
        assert seal_events(root) == before + 1
        assert_kept_is_clean(root, payload)
    assert victim.read_text() == "original\n"


@pytest.mark.parametrize("shape", ["closed-link", "evidence-link", "closed-file", "evidence-file"])
def test_a_planted_folder_changes_nothing_outside(task, outside, shape):
    """A link at `.closed` or a file in its place is replaced by the fresh folder and the seal goes through; a
    link at `<id>.evidence` is the early refusal of `kept_folder_problem` (B8b), before any write."""
    att, root, wt = task
    evidence = evidence_for(root, att)
    write(root, evidence)
    kept = kept_of(root)
    os.makedirs(os.path.dirname(kept), exist_ok=True)
    before_outside = snapshot(outside)
    if shape == "closed-link":
        os.makedirs(kept)
        os.symlink(str(outside), os.path.join(kept, proof.CLOSED_KEPT))
    elif shape == "evidence-link":
        os.symlink(str(outside), kept)
    elif shape == "closed-file":
        os.makedirs(kept)
        Path(kept, proof.CLOSED_KEPT).write_text("not a folder\n")
    else:
        Path(kept).write_text("not a folder\n")
    events = seal_events(root)
    if shape == "evidence-link":
        with pytest.raises(proof.Refusal) as exc:
            seal_now(root)
        assert "is a link" in " ".join(exc.value.problems) and seal_events(root) == events
        assert snapshot(outside) == before_outside
        return
    payload = seal_now(root)
    assert snapshot(outside) == before_outside
    assert os.path.isdir(kept) and not os.path.islink(kept)
    assert_kept_is_clean(root, payload)


def test_a_folder_at_the_seal_copy_name_refuses_the_seal_before_the_event(task):
    att, root, wt = task
    evidence = evidence_for(root, att)
    write(root, evidence)
    report = proof.report_path(root, {"id": "t-001", "op": "op-001"})
    os.makedirs(report + ".seal.json")
    before = seal_events(root)
    with pytest.raises(proof.Refusal) as exc:
        seal_now(root)
    assert "seal.json is a folder" in " ".join(exc.value.problems) and seal_events(root) == before
    assert not os.path.exists(kept_of(root))


def test_a_leftover_that_cannot_be_removed_does_not_stop_the_next_seal(task):
    att, root, wt = task
    evidence = evidence_for(root, att)
    write(root, evidence)
    op_folder = os.path.dirname(kept_of(root))
    stuck = os.path.join(op_folder, ".t-001.evidence.old-0123456789abcdef")
    os.makedirs(os.path.join(stuck, "inner"))
    Path(stuck, "inner", "x").write_text("x")
    os.chmod(stuck, 0)
    try:
        if os.access(stuck, os.R_OK):
            pytest.skip("this user can read a mode 000 folder (root)")
        payload = seal_now(root)
        assert_kept_is_clean_except(root, payload, {os.path.basename(stuck)})
    finally:
        os.chmod(stuck, 0o755)


def test_a_linked_op_folder_refuses_the_seal_and_writes_nothing_outside(task, outside):
    att, root, wt = task
    evidence = evidence_for(root, att)
    write(root, evidence)
    kept = kept_of(root)
    op_folder = os.path.dirname(kept)
    for name in os.listdir(op_folder):
        shutil.move(os.path.join(op_folder, name), str(outside / name))
    os.rmdir(op_folder)
    os.symlink(str(outside), op_folder)
    before_outside, before = snapshot(outside), seal_events(root)
    with pytest.raises(proof.Refusal) as exc:
        seal_now(root)
    assert "link" in " ".join(exc.value.problems)
    assert snapshot(outside) == before_outside and seal_events(root) == before


# ------------------------------------------------------------------- the latest seal's folder stays whole
def test_a_seal_that_fails_midway_leaves_the_latest_seals_folder_as_it_was(task, monkeypatch):
    att, root, wt = task
    evidence = evidence_for(root, att)
    write(root, evidence)
    first = seal_now(root)
    kept = kept_of(root)
    before = snapshot(kept)
    assert len(before) >= 5, before
    from alpaca.attach import safefs
    real, seen = safefs.Folder.put, []

    def flaky(self, name, data, *, mode=None):
        seen.append(name)
        if len(seen) == 2:
            raise safefs.WriteFailed("the disk is full", "No space left on device")
        return real(self, name, data, mode=mode)
    monkeypatch.setattr(safefs.Folder, "put", flaky)
    write(root, evidence)
    events = seal_events(root)
    with pytest.raises(proof.Refusal) as exc:
        seal_now(root)
    assert "ATTACH-KEPT" in " ".join(exc.value.problems) and len(seen) == 2
    assert snapshot(kept) == before and seal_events(root) == events
    assert [n for n in os.listdir(os.path.dirname(kept)) if n.startswith(".")] == []
    monkeypatch.setattr(safefs.Folder, "put", real)
    again = seal_now(root)                                                    # and the next seal works
    assert_kept_is_clean(root, again)
    assert again["tree"]["app"]["patch"] == first["tree"]["app"]["patch"]


def test_a_failed_swap_puts_the_old_folder_back(task, monkeypatch):
    att, root, wt = task
    evidence = evidence_for(root, att)
    write(root, evidence)
    seal_now(root)
    kept = kept_of(root)
    before = snapshot(kept)
    from alpaca.attach import safefs
    real, calls = safefs.Folder.move_entry, []

    def second_fails(self, old, new):
        calls.append((old, new))
        if len(calls) == 2:
            raise safefs.WriteFailed("rename failed", "Input/output error")
        return real(self, old, new)
    monkeypatch.setattr(safefs.Folder, "move_entry", second_fails)
    write(root, evidence)
    events = seal_events(root)
    with pytest.raises(proof.Refusal):
        seal_now(root)
    assert snapshot(kept) == before and seal_events(root) == events
    assert [n for n in os.listdir(os.path.dirname(kept)) if n.startswith(".")] == []


def test_a_crash_between_the_two_renames_is_recovered_by_the_next_seal(task, monkeypatch):
    att, root, wt = task
    evidence = evidence_for(root, att)
    write(root, evidence)
    seal_now(root)
    kept = kept_of(root)
    from alpaca.attach import safefs

    class Crash(BaseException):
        """The process dies here: nothing after it runs."""

    real, calls = safefs.Folder.move_entry, []

    def dies(self, old, new):
        calls.append((old, new))
        if len(calls) == 2:
            raise Crash()
        return real(self, old, new)
    monkeypatch.setattr(safefs.Folder, "move_entry", dies)
    write(root, evidence)
    with pytest.raises(Crash):
        seal_now(root)
    op_folder = os.path.dirname(kept)
    assert not os.path.exists(kept) and any(n.startswith(".t-001.evidence.old-") for n in os.listdir(op_folder))
    monkeypatch.setattr(safefs.Folder, "move_entry", real)
    write(root, evidence)
    payload = seal_now(root)
    assert_kept_is_clean(root, payload)          # also checks that no `.t-001.evidence.*` name is left


def test_a_seal_that_keeps_nothing_leaves_no_folder_and_no_stale_copy(task):
    att, root, wt = task
    evidence = evidence_for(root, att)
    write(root, evidence, verify=False)
    seal_now(root, verify=False)
    kept = kept_of(root)
    assert os.path.isdir(kept)
    (Path(root) / "notes").mkdir(exist_ok=True)
    (Path(root) / "notes" / ".hidden").write_text("never copied\n")
    write(root, ["local:notes/.hidden - a dot file the seal never copies"], verify=False)
    payload = seal_now(root, verify=False)
    assert not os.path.exists(kept) and payload["evidence"][0]["kept"] is None
    assert [n for n in os.listdir(os.path.dirname(kept)) if n.startswith(".t-001.evidence")] == []


# ------------------------------------------------------------------------------ where the copies go
def test_app_patch_is_kept_closed_and_the_served_tree_patch_stays_served(task):
    att, root, wt = task
    commit_in(wt, {"src/main.py": SECRET})
    write(root, None)
    payload = seal_now(root)
    app, tree = payload["tree"]["app"], payload["tree"]
    assert app["patch"].endswith("t-001.evidence/.closed/app.patch")
    assert not paths.browser_serves(root, os.path.realpath(os.path.join(root, app["patch"])))
    served = os.listdir(os.path.dirname(os.path.join(root, tree["patch"])))
    assert "app.patch" not in served and "tree.patch" in served
    assert b"product code" in Path(root, app["patch"]).read_bytes()


def test_k4_an_invalid_attach_block_never_gives_app_files_a_served_copy(attached):
    root = prepare(attached)
    evidence = evidence_for(root, attached)
    from alpaca import project as project_mod
    cfg = project_mod.path(root)
    text = util.read_text(cfg)
    assert "attach:" in text and os.path.isfile(os.path.join(root, ".alpaca", "attach", "state.json"))
    util.write_text(cfg, text.replace("attach:", "attach:\n  bogus_key_xyz: 1", 1))
    write(root, evidence, verify=False)
    payload = seal_now(root, verify=False)
    studio_item, app_item = payload["evidence"]
    assert studio_item["kept"] and "/.closed/" not in studio_item["kept"], studio_item
    assert app_item["kept"] and "/.closed/" in app_item["kept"], app_item
    assert not paths.browser_serves(root, os.path.realpath(os.path.join(root, app_item["kept"])))
    assert_kept_is_clean(root, payload)


def test_f6_a_project_with_attach_state_is_not_sealed_when_the_hook_cannot_load(task, monkeypatch):
    att, root, wt = task
    write(root, None)
    before = seal_events(root)
    monkeypatch.setattr(proof, "_attach_hook", lambda: None)
    with pytest.raises(proof.Refusal) as exc:
        seal_now(root)
    assert "ATTACH-KEPT" in " ".join(exc.value.problems) and seal_events(root) == before
    assert not os.path.exists(kept_of(root))


# --------------------------------------------------------------------------------------------- parity
def _evidence_items(root, tmp_cap):
    """inspect()'s evidence list for a report that cites every shape the keeper must treat the same way."""
    (Path(root) / "notes").mkdir(exist_ok=True)
    (Path(root) / "notes" / "a.txt").write_text("alpha\n")
    (Path(root) / "notes" / "big.bin").write_bytes(b"x" * (tmp_cap + 10))
    (Path(root) / "notes" / ".hidden").write_text("secret\n")
    (Path(root) / "notes" / "db.sqlite").write_bytes(paths.SQLITE_MAGIC + b"\0" * 64)
    (Path(root) / "notes" / "moved.txt").write_text("before\n")
    (Path(root) / "notes" / "gone.txt").write_text("here\n")
    (Path(root) / "notes" / "b.tar.gz").write_text("not served\n")
    pointers = ["local:notes/a.txt", "local:notes/big.bin", "local:notes/.hidden", "local:notes/db.sqlite",
                "local:notes/moved.txt", "local:notes/gone.txt", "local:notes/b.tar.gz",
                "remote:https://example.org/run/1"]
    write(root, ["%s - shape %d" % (p, i) for i, p in enumerate(pointers)], verify=False)
    conn = db.connect(root)
    try:
        items = copy.deepcopy(proof.inspect(conn, root, "t-001")["evidence"])
    finally:
        conn.close()
    (Path(root) / "notes" / "moved.txt").write_text("after, so the sealed hash is stale\n")
    os.remove(os.path.join(root, "notes", "gone.txt"))
    return items


def test_the_attach_keeper_and_the_plain_keeper_agree_on_every_shape(task, monkeypatch):
    """Same evidence list into both writers: equal `kept`, `kept_reason` and bytes, name for name."""
    att, root, wt = task
    monkeypatch.setattr(proof, "KEPT_FILE_CAP", 50)
    items = _evidence_items(root, 50)
    conn = db.connect(root)
    try:
        subj = proof.subject(conn, "t-001")
    finally:
        conn.close()
    kept = proof.kept_dir(root, subj)
    plain = copy.deepcopy(items)
    proof.keep_evidence(root, subj, plain)
    plain_files, plain_modes = snapshot(kept), modes(kept)
    shutil.rmtree(kept)
    from alpaca.attach import proofhook
    fresh = copy.deepcopy(items)
    proofhook.keep(root, subj, fresh, None, None)
    assert snapshot(kept) == plain_files and len(plain_files) >= 3, plain_files
    assert modes(kept) == plain_modes
    assert [(i.get("kept"), i.get("kept_reason")) for i in fresh] == [(i.get("kept"), i.get("kept_reason")) for i in plain]
    reasons = {i.get("kept_reason") for i in plain if i.get("kept_reason")}
    assert {proof.OVER_CAP, proof.HIDDEN_NOT_KEPT, proof.MOVED_NOT_KEPT} <= reasons, reasons
    assert any("the file went" in r for r in reasons), reasons
    assert any("/.closed/" in (i.get("kept") or "") for i in plain)             # a closed copy took part


def test_a_plain_project_still_writes_with_the_plain_writers(project):
    """The dispatch is per project: without `attach:` and without `.alpaca/attach` the plain keeper runs."""
    from alpaca import cli
    from alpaca.attach import proofhook
    cli.main(["init"])
    cli.main(["op", "new", "add the shout verb", "--done-when", "the verb prints upper case"])
    cli.main(["task", "add", "--title", "task", "op-001", "write the shout verb", "--phase", "build"])
    assert proofhook.attach_project(project) is False
    write(project, None, verify=False)
    payload = seal_now(project, verify=False)
    assert payload["evidence"][0]["kept"] and "attach" not in payload


def test_a_stray_attach_folder_makes_a_project_an_attach_project(project):
    from alpaca.attach import proofhook
    assert proofhook.attach_project(project) is False
    os.makedirs(os.path.join(project, ".alpaca", "attach", "branches"))
    assert proofhook.attach_project(project) is True


# --------------------------------------------------------------- no attach seal writes by name (tracer)
def test_an_attach_seal_writes_nothing_by_name(task, monkeypatch):
    att, root, wt = task
    evidence = evidence_for(root, att)
    commit_in(wt, {"src/main.py": SECRET})
    write(root, evidence)
    from alpaca.research import verify_gate

    def refuse(*a, **k):
        raise AssertionError("the plain writer ran for an attach seal")
    for owner, name in ((proof, "keep_evidence"), (proof, "_copy_checked"), (proof, "_prune_kept"),
                        (verify_gate, "keep_tree")):
        monkeypatch.setattr(owner, name, refuse)
    tracer = Tracer()
    with monkeypatch.context() as mp:
        tracer.install(mp)
        payload = seal_now(root)
    assert tracer.calls > 0
    assert not tracer.hits, tracer.describe()
    assert_kept_is_clean(root, payload)


def test_the_tracer_sees_the_plain_writers(project, monkeypatch):
    """Liveness: the tracer is not blind. A plain project's seal writes copies by name from alpaca/proof.py."""
    from alpaca import cli
    cli.main(["init"])
    cli.main(["op", "new", "add the shout verb", "--done-when", "the verb prints upper case"])
    cli.main(["task", "add", "--title", "task", "op-001", "write the shout verb", "--phase", "build"])
    write(project, None, verify=False)
    tracer = Tracer()
    with monkeypatch.context() as mp:
        tracer.install(mp)
        seal_now(project, verify=False)
    assert tracer.from_product("alpaca/proof.py"), tracer.describe()


# ------------------------------------------------------------------------- the plain writers' bytes
def _function_text(source, name):
    for node in ast.parse(source).body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(source, node)
    raise AssertionError("no function %s" % name)


@pytest.mark.parametrize("relfile,names", [
    ("alpaca/proof.py", ["keep_evidence", "_copy_checked", "_prune_kept"]),
    ("alpaca/research/verify_gate.py", ["keep_tree"]),
])
def test_the_plain_writers_are_the_text_of_the_base_commit(relfile, names):
    """A project without `attach:` keeps its kept files byte for byte because these functions were not edited
    (op-062 t-650 fix round 4; the base is the merge of t-647)."""
    repo = Path(__file__).resolve().parents[2]
    shown = subprocess.run(["git", "-C", str(repo), "show", "%s:%s" % (BASE, relfile)], capture_output=True, text=True)
    if shown.returncode != 0:
        pytest.skip("the base commit %s is not in this clone's history" % BASE)
    current = (repo / relfile).read_text(encoding="utf-8")
    for name in names:
        assert _function_text(current, name) == _function_text(shown.stdout, name), name
