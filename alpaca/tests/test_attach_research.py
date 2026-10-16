"""Research packs in the studio (op-062 t-653, requirement R15, acceptance row AC-17) and the pre-mortem attacks of
t-653 (items 3 and 4).

An attached project keeps a research runbook and its pack in the studio while `research.repo.path` is the attached
repository. The contract freezes the pack from disk under `files/@studio/...`, the run tree holds none of it,
and the champion's change reaches a work branch through `alpaca attach land`, which refuses any pack or studio
file. A project without `attach:` meets every old refusal unchanged: the parameter table below covers the three
places that name them (the runbook checker, `contract.load_block`, `contract.check_overlap`).
"""
import dataclasses
import os
import stat
from pathlib import Path

import pytest

from alpaca import cli, runbook
from alpaca.attach import AttachError, gitops, runbooks
from alpaca.research import contract as contract_mod, events, jail, paths, runner
from alpaca.research.errors import ResearchRefusal
from alpaca.tests import research_loopkit as kit
from alpaca.tests.attachkit import (DEFAULT_FILES, AttachedStudio, add_tasks, git, make_upstream, studio)  # noqa: F401

needs_jail = pytest.mark.skipif(not jail.available()[0], reason="bubblewrap is not available on this host")

PACK = "research"
RUNBOOK = PACK + "/rb.yaml"
UPSTREAM_FILES = dict(DEFAULT_FILES, **{"src/value.txt": "100\n"})


def pack_text(rounds=1, slots=1, repo="app"):
    """The loop kit's research runbook, with the pack beside it in the studio and `repo.path` the attached repo."""
    text = (kit.RB.replace("ROUNDS", str(rounds)).replace("SLOTS", str(slots)).replace("RETRIES", "2")
            .replace("DISK", "0.001").replace("USD", "10"))
    text = text.replace("sh ${CONTRACT_DIR}/pack/measure.sh ${WORKTREE}", "sh ${RUNBOOK_DIR}/measure.sh ${WORKTREE}")
    text = text.replace("    protected: [pack/]\n", "    protected: [Makefile]\n")
    return text.replace("research:\n  mode: improve\n", "research:\n  mode: improve\n  repo: {path: %s}\n" % repo)


def write_pack(root, **kw):
    kit.write(str(root), PACK + "/spec.md", kit.SPEC)
    kit.write(str(root), PACK + "/measure.sh", kit.MEASURE, 0o755)
    kit.write(str(root), RUNBOOK, pack_text(**kw))
    return str(Path(root) / RUNBOOK)


@pytest.fixture
def rupstream(tmp_path):
    return make_upstream(tmp_path, files=UPSTREAM_FILES)


@pytest.fixture
def rattached(studio, rupstream):
    from alpaca.attach import repo
    return AttachedStudio(studio, rupstream, repo.attach(str(studio), rupstream.url))


def refusal(call, *args, **kw):
    """The reason code of the ResearchRefusal `call` raises, else None."""
    try:
        call(*args, **kw)
    except ResearchRefusal as exc:
        return exc.reason
    return None


def make_repo(folder):
    """A plain git repository with one commit at `folder` (a repo that is not the attached one)."""
    os.makedirs(str(folder))
    git(folder, "init", "-q", "-b", "main")
    Path(folder, "src").mkdir()
    Path(folder, "src", "value.txt").write_text("100\n")
    git(folder, "add", "-A")
    git(folder, "commit", "-q", "-m", "base")


# ---- the pre-mortem table (t-653.3): three sites, one rule -----------------------------------------------------------

ROWS = [("plain", "app", True), ("plain", "sub", True), ("attached", "other", True),
        ("attached", "app/../app", False), ("attached", "app-link", True), ("attached", "app", False)]


def three_sites(root, repo_path, attached_mode):
    """(checker refused, loader refused, check_overlap refused) for a pack in `research/` and this repo.path."""
    rb = write_pack(root, repo=repo_path)
    site1 = "PACK-OUTSIDE-REPO" in [e["code"] for e in runbook.check(rb)["errors"]]
    site2 = refusal(contract_mod.load_block, rb) == "CONTRACT-PACK-OUTSIDE-REPO"
    # site 3 is judged on a contract that puts its pack outside repo.path, the way a contract.json could
    claim = contract_mod.from_block({"runbook": RUNBOOK, "runbook_id": "x",
                                     "repo": {"path": repo_path, "ref": "HEAD", "commit": ""}, "block": {"mode": "improve"}})
    site3 = refusal(contract_mod.check_overlap, str(root), claim) == "CONTRACT-PACK-OUTSIDE-REPO"
    return site1, site2, site3


@pytest.mark.parametrize("kind,repo_path,refused", ROWS)
def test_a_pack_outside_the_repo_is_allowed_only_for_the_attached_repo(request, tmp_path, kind, repo_path, refused):
    if kind == "attached":
        att = request.getfixturevalue("rattached")
        root = att.root
        make_repo(root / "other")
        os.symlink(str(root / "app"), str(root / "app-link"))
    else:
        root = request.getfixturevalue("studio")
        make_repo(root / "app")
        make_repo(root / "sub")
    sites = three_sites(root, repo_path, kind)
    assert sites == (refused,) * 3, (kind, repo_path, sites)


def test_a_contract_with_its_pack_outside_the_repo_in_a_project_that_is_not_attached_is_refused(studio):
    make_repo(studio / "app")
    write_pack(studio)
    claim = contract_mod.from_block({"runbook": RUNBOOK, "runbook_id": "x", "pack_source": "studio",
                                     "repo": {"path": "app", "ref": "HEAD", "commit": ""}, "block": {"mode": "improve"}})
    assert refusal(contract_mod.check_overlap, str(studio), claim) == "CONTRACT-PACK-OUTSIDE-REPO"


def test_the_decision_to_use_a_studio_pack_is_read_from_the_paths_not_from_a_flag(rattached):
    """No field of types.Contract carries the marker (the design block of types.py is exact): gates read the
    repo.path and runbook paths of the contract."""
    from alpaca.research import gates
    studio_pack = contract_mod.from_block({"runbook": RUNBOOK, "runbook_id": "x", "repo": {"path": "app"}, "block": {}})
    inside = contract_mod.from_block({"runbook": "app/pack/rb.yaml", "runbook_id": "x", "repo": {"path": "app"}, "block": {}})
    root_repo = contract_mod.from_block({"runbook": "pack/rb.yaml", "runbook_id": "x", "repo": {"path": "."}, "block": {}})
    absolute = contract_mod.from_block({"runbook": RUNBOOK, "runbook_id": "x", "repo": {"path": "/abs/app"}, "block": {}})
    assert [gates._studio_pack(c) for c in (studio_pack, inside, root_repo, absolute)] == [True, False, False, False]
    assert gates._runbook_folder(studio_pack) == "@studio/research" and gates._runbook_folder(inside) == "pack"


@pytest.mark.parametrize("folder,ok", [("research", True), ("deep/er/pack", True), (".", False), (".alpaca/x", False),
                                       ("app", False), ("app/pack", False), (".", False)])
def test_the_pack_folder_must_be_a_plain_studio_folder(rattached, folder, ok):
    root = rattached.root
    target = (root / folder).resolve() if folder != "." else root
    if folder != "app":
        target.mkdir(parents=True, exist_ok=True)
    assert runbooks.pack_outside_allowed(str(root), str(root / "app"), folder=str(target)) is ok


def test_a_pack_folder_that_holds_the_repository_is_refused(rattached, tmp_path):
    """The repository inside the pack folder would be frozen with it."""
    root = rattached.root
    assert runbooks.pack_outside_allowed(str(root), str(root / "app"), folder=str(root)) is False


def test_a_link_folder_that_leaves_the_studio_is_refused(rattached, tmp_path):
    outside = tmp_path / "outside-pack"
    outside.mkdir()
    os.symlink(str(outside), str(rattached.root / "linked-pack"))
    assert runbooks.pack_outside_allowed(str(rattached.root), str(rattached.root / "app"),
                                         folder=str(rattached.root / "linked-pack")) is False


def test_the_helpers_answer_false_for_things_that_are_not_a_project(tmp_path):
    assert runbooks.attach_on(str(tmp_path / "nope")) is False
    assert runbooks.pack_outside_allowed(str(tmp_path / "nope"), str(tmp_path / "nope" / "app")) is False
    assert runbooks.pack_outside_allowed(None, None) is False


# ---- what the loader freezes from the studio ---------------------------------------------------------------------------

def test_the_loader_reads_the_pack_from_disk_under_the_studio_prefix(rattached):
    rb = write_pack(rattached.root)
    pack = contract_mod.load_block(rb)
    assert pack["pack_source"] == "studio" and pack["folder"] == "@studio/research"
    mine = sorted(k for k in pack["files"] if k.startswith("@studio/"))
    assert mine == ["@studio/research/measure.sh", "@studio/research/spec.md"]      # not the runbook itself
    assert pack["files"]["@studio/research/measure.sh"] == contract_mod._sha(kit.MEASURE.encode())
    assert "Makefile" in pack["files"]                           # a protected rule of the repository still freezes


def test_a_pack_in_the_repository_is_still_read_from_the_repository(rattached):
    """The control: a runbook inside repo.path is the old path, with no studio marker."""
    root = rattached.root
    kit.write(str(root / "app"), "pack/spec.md", kit.SPEC)
    kit.write(str(root / "app"), "pack/measure.sh", kit.MEASURE, 0o755)
    kit.write(str(root / "app"), "pack/rb.yaml", pack_text().replace("sh ${RUNBOOK_DIR}/measure.sh", "sh ${RUNBOOK_DIR}/measure.sh")
              .replace("protected: [Makefile]", "protected: [pack/]"))
    git(root / "app", "add", "-A")
    git(root / "app", "commit", "-q", "-m", "pack")
    pack = contract_mod.load_block(str(root / "app" / "pack" / "rb.yaml"))
    assert "pack_source" not in pack and pack["folder"] == "pack"
    assert "pack/measure.sh" in pack["files"]


def test_a_link_in_the_pack_folder_is_neither_followed_nor_kept(rattached, tmp_path):
    rb = write_pack(rattached.root)
    secret = tmp_path / "secret.txt"
    secret.write_text("secret\n")
    os.symlink(str(secret), str(rattached.root / PACK / "leak.txt"))
    os.symlink(str(tmp_path), str(rattached.root / PACK / "dir-link"))
    names = contract_mod.load_block(rb)["files"]
    assert not [k for k in names if "leak" in k or "dir-link" in k]


def test_compiled_files_and_git_folders_in_the_pack_folder_are_left_out(rattached):
    rb = write_pack(rattached.root)
    kit.write(str(rattached.root), PACK + "/__pycache__/x.cpython-312.pyc", "x")
    kit.write(str(rattached.root), PACK + "/old.pyc", "x")
    names = contract_mod.load_block(rb)["files"]
    assert not [k for k in names if "pyc" in k or "__pycache__" in k]


def test_a_pack_folder_that_is_far_too_big_is_refused(rattached, monkeypatch):
    rb = write_pack(rattached.root)
    monkeypatch.setattr(contract_mod, "_STUDIO_MAX_FILES", 1)
    assert refusal(contract_mod.load_block, rb) == "CONTRACT-CHECK-FAILED"


def test_a_named_lessons_file_must_be_in_the_studio_pack(rattached):
    rb = write_pack(rattached.root)
    text = Path(rb).read_text().replace("  categories:\n", "  lessons:\n    file: lessons.md\n  categories:\n")
    Path(rb).write_text(text)
    assert refusal(contract_mod.load_block, rb) == "CONTRACT-CHECK-FAILED"
    kit.write(str(rattached.root), PACK + "/lessons.md", "one lesson\n")
    assert "@studio/research/lessons.md" in contract_mod.load_block(rb)["files"]


def test_a_repository_that_tracks_the_studio_prefix_is_refused(studio, tmp_path):
    from alpaca.attach import repo
    (tmp_path / "u2").mkdir()
    up = make_upstream(tmp_path / "u2", files=dict(UPSTREAM_FILES, **{"@studio/research/measure.sh": "evil\n"}))
    repo.attach(str(studio), up.url)
    rb = write_pack(studio)
    assert refusal(contract_mod.load_block, rb) == "CONTRACT-PACK-OUTSIDE-REPO"


def test_the_preview_marks_the_contract_only_for_a_studio_pack(rattached):
    write_pack(rattached.root)
    view = contract_mod.preview(str(rattached.root), RUNBOOK)
    assert view["contract"]["pack_source"] == "studio"
    assert "files/@studio/research/measure.sh" in view["files"] and "files/Makefile" in view["files"]


def test_a_pack_edit_in_the_studio_is_a_loosening_like_any_pack_edit(rattached):
    """The frozen pack is hashed into pack_sha256 and diffed against the last baseline: an edit to a studio pack
    file is a `files[...]` entry classed loosened, so it needs the owner's grant as an edit inside a repo does."""
    write_pack(rattached.root)
    first = contract_mod.load_block(str(rattached.root / RUNBOOK))
    baseline = {"block": first["block"], "files": first["files"], "stages": first["stages"], "knobs": first["knobs"],
                "spec": first["spec"]}
    assert contract_mod.pack_diff(first, baseline) == []
    kit.write(str(rattached.root), PACK + "/measure.sh", kit.MEASURE + "# changed\n", 0o755)
    second = contract_mod.load_block(str(rattached.root / RUNBOOK))
    assert second["pack_sha256"] != first["pack_sha256"]
    diff = contract_mod.pack_diff(second, baseline)
    assert [(d["field"], d["direction"]) for d in diff] == [("files[@studio/research/measure.sh]", "loosened")]


# ---- AC-17: a research run on app/ with its pack in the studio ----------------------------------------------------------

def run_research(att, monkeypatch, **kw):
    root = str(att.root)
    write_pack(att.root, **kw)
    kit.install(monkeypatch, root, kit.Agents(root))
    got = kit.start(root, runbook=RUNBOOK)
    out = runner.launch(root, got["run"], foreground=True, session="test")
    return root, got["run"], out


def champion_patch(root, run, tmp_path):
    clone = os.path.join(root, ".alpaca", "research", run, "clones", "champion")
    start = kit.events_of(root, events.RUN_START, run)[0]["data"]["base_commit"]
    merge = kit.events_of(root, events.MERGE, run)[-1]["data"]["commit"]
    patch = tmp_path / "champion.patch"
    patch.write_bytes(__import__("subprocess").run(["git", "-C", clone, "diff", "--binary", start, merge],
                                                   check=True, capture_output=True).stdout)
    return clone, merge, str(patch)


@needs_jail
def test_a_research_run_works_on_app_with_its_pack_in_the_studio_and_lands_only_source(rattached, monkeypatch, tmp_path):
    """AC-17 and PM t-653.4: the run freezes the pack from the studio and runs an attempt on app/; the pack is
    in no tree the run builds; the landed change brings only source files and no pack blob."""
    (task,) = add_tasks(rattached.root, 1)
    assert cli.main(["task", "claim", task, "--by", "w1"]) == cli.PASS
    studio_head = git(rattached.root, "rev-parse", "HEAD", check=False)
    root, run, out = run_research(rattached, monkeypatch)
    assert out["code"] == 3                                              # halted: the target (10) is not met in one round
    assert len(kit.events_of(root, events.MERGE, run)) == 1
    # the frozen copy holds the pack, read-only, byte for byte
    frozen = os.path.join(paths.contract_dir(root, run), "files", "@studio", "research")
    assert open(os.path.join(frozen, "measure.sh"), "rb").read() == kit.MEASURE.encode()
    assert stat.S_IMODE(os.stat(os.path.join(frozen, "measure.sh")).st_mode) == 0o555
    assert not os.path.exists(os.path.join(frozen, "rb.yaml"))
    assert kit.events_of(root, events.RUN_START, run)[0]["data"]["block"]["repo"]["path"] == "app"
    # the attempt measured the number through the frozen script: the champion holds 60
    clone, merge, patch = champion_patch(root, run, tmp_path)
    start = kit.events_of(root, events.RUN_START, run)[0]["data"]["base_commit"]
    assert git(clone, "show", "%s:src/value.txt" % merge).strip() == "60"
    # nothing of the pack is in the run's repository: not in the base, not in the champion branch
    tree = git(clone, "ls-tree", "-r", "--name-only", merge).split()
    assert not [p for p in tree if p.startswith(("research", "@studio"))] and "src/value.txt" in tree
    assert not [p for p in git(clone, "diff", "--name-only", start, merge).split() if p != "src/value.txt"]
    # the attached repository and its main checkout are as they were
    assert git(rattached.app, "status", "--porcelain", "--ignored") == ""
    # landing: one commit on the task's work branch with the source change and no pack blob
    head = runbooks.land(root, task, patch, message="lower the value")
    folder = os.path.realpath(os.path.join(root, ".alpaca/attach/worktrees", task))
    names = gitops.in_worktree(str(rattached.app), task, folder, "diff", "--name-only", "HEAD~1", "HEAD").split()
    assert names == ["src/value.txt"]
    assert gitops.in_worktree(str(rattached.app), task, folder, "rev-parse", "HEAD") == head
    assert gitops.in_worktree(str(rattached.app), task, folder, "log", "-1", "--format=%an <%ae>") == "Alpaca agent <alpaca-agent@localhost>"
    pack_blobs = runbooks.pack_blob_ids(root)
    landed = set(gitops.in_worktree(str(rattached.app), task, folder, "ls-tree", "-r", "HEAD").split())
    assert pack_blobs and pack_blobs.isdisjoint(landed)
    assert git(rattached.root, "rev-parse", "HEAD", check=False) == studio_head           # the studio's own history is not touched


@needs_jail
def test_a_second_run_of_the_same_pack_freezes_the_same_pack_hash(rattached, monkeypatch):
    write_pack(rattached.root)
    first = contract_mod.preview(str(rattached.root), RUNBOOK)
    second = contract_mod.preview(str(rattached.root), RUNBOOK)
    assert first["pack_sha256"] == second["pack_sha256"] and first["files_sha256"] == second["files_sha256"]
    assert first["contract_sha256"] == second["contract_sha256"]


# ---- landing a patch: what it refuses (t-653.4) ----------------------------------------------------------------------

def claimed(att):
    (task,) = add_tasks(att.root, 1)
    assert cli.main(["task", "claim", task, "--by", "w1"]) == cli.PASS
    return task, os.path.realpath(str(att.root / ".alpaca/attach/worktrees" / task))


def diff_of_new_file(tmp_path, rel, data, name="p.patch"):
    """A patch that adds `rel` with the bytes `data`, made in a scratch repository."""
    scratch = tmp_path / ("scratch-" + name)
    scratch.mkdir()
    git(scratch, "init", "-q", "-b", "main")
    (scratch / "keep").write_text("k\n")
    git(scratch, "add", "-A")
    git(scratch, "commit", "-q", "-m", "b")
    target = scratch / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data if isinstance(data, bytes) else data.encode())
    git(scratch, "add", "-A")
    patch = tmp_path / name
    patch.write_bytes(__import__("subprocess").run(["git", "-C", str(scratch), "diff", "--cached", "--binary"],
                                                   check=True, capture_output=True).stdout)
    return str(patch)


def test_land_commits_a_plain_source_change(rattached, tmp_path):
    task, folder = claimed(rattached)
    patch = diff_of_new_file(tmp_path, "src/new.py", "x = 1\n")
    head = runbooks.land(str(rattached.root), task, patch, message="add new.py")
    assert gitops.in_worktree(str(rattached.app), task, folder, "show", "HEAD:src/new.py") == "x = 1"
    assert gitops.in_worktree(str(rattached.app), task, folder, "rev-parse", "HEAD") == head
    assert gitops.in_worktree(str(rattached.app), task, folder, "status", "--porcelain") == ""
    # git apply writes relative to the folder it runs in: the clone's main checkout must not receive the file
    assert not (rattached.app / "src" / "new.py").exists() and git(rattached.app, "status", "--porcelain", "--ignored") == ""


def test_land_refuses_a_file_that_is_byte_for_byte_a_pack_file_under_any_name(rattached, tmp_path):
    write_pack(rattached.root)
    task, folder = claimed(rattached)
    before = gitops.in_worktree(str(rattached.app), task, folder, "rev-parse", "HEAD")
    patch = diff_of_new_file(tmp_path, "tools/renamed.sh", kit.MEASURE)
    with pytest.raises(AttachError, match="byte for byte"):
        runbooks.land(str(rattached.root), task, patch, message="m")
    assert gitops.in_worktree(str(rattached.app), task, folder, "rev-parse", "HEAD") == before
    assert gitops.in_worktree(str(rattached.app), task, folder, "status", "--porcelain") == ""      # untouched


def test_land_refuses_a_file_that_is_byte_for_byte_a_studio_file(rattached, tmp_path):
    folder_rb = rattached.root / "runbooks" / "x"
    folder_rb.mkdir(parents=True)
    (folder_rb / "runbook.yaml").write_text("runbook: 1\nid: private\n")
    task, _folder = claimed(rattached)
    patch = diff_of_new_file(tmp_path, "docs/copy.yaml", "runbook: 1\nid: private\n")
    with pytest.raises(AttachError, match="byte for byte"):
        runbooks.land(str(rattached.root), task, patch, message="m")


@pytest.mark.parametrize("rel", [".alpaca/x.txt", "ALPACA-MANIFEST", "RESUME.md", ".git/config.txt", "CHECKLIST.md",
                                 "sub/.alpaca/x.txt", ".ALPACA/x.txt", ".Alpaca/x.txt", "alpaca-manifest",
                                 "docs/Resume.md", "a/../.alpaca/z"])
def test_land_refuses_alpaca_paths(rattached, tmp_path, rel):
    task, _folder = claimed(rattached)
    patch = diff_of_new_file(tmp_path, rel, "x\n")
    with pytest.raises(AttachError):
        runbooks.land(str(rattached.root), task, patch, message="m")


def test_land_refuses_a_dirty_work_folder(rattached, tmp_path):
    """What a stage wrote (metrics, logs) must not ride along in the landed commit."""
    task, folder = claimed(rattached)
    Path(folder, "stage-output.json").write_text("{}\n")
    patch = diff_of_new_file(tmp_path, "src/new.py", "x = 1\n")
    with pytest.raises(AttachError, match="changes of its own"):
        runbooks.land(str(rattached.root), task, patch, message="m")
    assert gitops.in_worktree(str(rattached.app), task, folder, "status", "--porcelain") == "?? stage-output.json"


def test_land_refuses_a_patch_that_does_not_apply_and_changes_nothing(rattached, tmp_path):
    task, folder = claimed(rattached)
    bad = tmp_path / "bad.patch"
    bad.write_text("--- a/src/main.py\n+++ b/src/main.py\n@@ -1 +1 @@\n-print('other')\n+print('x')\n")
    with pytest.raises(AttachError):
        runbooks.land(str(rattached.root), task, str(bad), message="m")
    assert gitops.in_worktree(str(rattached.app), task, folder, "status", "--porcelain") == ""


def test_land_refuses_a_link_a_missing_patch_and_an_empty_message(rattached, tmp_path):
    task, _folder = claimed(rattached)
    patch = diff_of_new_file(tmp_path, "src/new.py", "x = 1\n")
    link = tmp_path / "link.patch"
    os.symlink(patch, str(link))
    for bad in (str(link), str(tmp_path / "missing.patch")):
        with pytest.raises(AttachError):
            runbooks.land(str(rattached.root), task, bad, message="m")
    with pytest.raises(AttachError, match="commit message"):
        runbooks.land(str(rattached.root), task, patch, message="  ")


def test_land_on_a_task_without_a_work_folder_is_refused(rattached, tmp_path):
    (task,) = add_tasks(rattached.root, 1)
    patch = diff_of_new_file(tmp_path, "src/new.py", "x = 1\n")
    with pytest.raises(AttachError, match="no proved work folder"):
        runbooks.land(str(rattached.root), task, patch, message="m")


def test_the_land_verb_prints_a_gate_line(rattached, tmp_path, capsys):
    task, _folder = claimed(rattached)
    patch = diff_of_new_file(tmp_path, "src/new.py", "x = 1\n")
    capsys.readouterr()
    assert cli.main(["attach", "land", task, "--patch", patch, "-m", "add new.py"]) == cli.PASS
    assert "GATE alpaca-attach-land: PASS" in capsys.readouterr().out
    bad = diff_of_new_file(tmp_path, "ALPACA-MANIFEST", "x\n", name="q.patch")
    assert cli.main(["attach", "land", task, "--patch", bad, "-m", "again"]) == cli.FAIL
    assert "GATE alpaca-attach-land: FAIL" in capsys.readouterr().out


def test_land_refuses_a_pack_file_of_a_folder_the_record_names(rattached, tmp_path):
    """The pack folders come from the research-contract events too, so a pack in `packs/x/` (not `research/`)
    is as protected as the default one."""
    from alpaca import db
    root = rattached.root
    (root / "packs" / "x").mkdir(parents=True)
    (root / "packs" / "x" / "tool.sh").write_text("echo pack only\n")
    task, _folder = claimed(rattached)
    patch = diff_of_new_file(tmp_path, "scripts/tool.sh", "echo pack only\n")
    conn = db.connect(str(root))
    try:
        db.append_event(conn, session="test", actor="alpaca", kind="research-contract", ref="x",
                        data={"runbook": "packs/x/rb.yaml", "runbook_id": "x"})
    finally:
        conn.close()
    assert runbooks.recorded_pack_folders(str(root)) == ["packs/x"]
    with pytest.raises(AttachError, match="byte for byte"):
        runbooks.land(str(root), task, patch, message="m")


def freeze_a_copy(root, run, rel, text):
    """What `contract.freeze` leaves: the pack file under files/@studio/, and the research-contract event."""
    from alpaca import db
    frozen = Path(root, ".alpaca", "research", run, "contract", "files", "@studio", os.path.dirname(rel))
    frozen.mkdir(parents=True, exist_ok=True)
    (frozen / os.path.basename(rel)).write_text(text)
    conn = db.connect(str(root))
    try:
        db.append_event(conn, session="test", actor="alpaca", kind="research-contract", ref=run,
                        data={"runbook": os.path.dirname(rel) + "/rb.yaml", "runbook_id": "x"})
    finally:
        conn.close()


def test_land_refuses_the_older_frozen_bytes_of_a_pack_file_after_the_owner_edited_it(rattached, tmp_path):
    """PM t-653.4, adversarial F4: the run's agents read the frozen copy; the owner then edits the pack. The
    frozen bytes are still a pack file and never reach the repository."""
    root = rattached.root
    write_pack(root)
    freeze_a_copy(root, "r-001", "research/measure.sh", kit.MEASURE)
    (root / "research" / "measure.sh").write_text(kit.MEASURE + "# edited after the run\n")
    task, folder = claimed(rattached)
    before = gitops.in_worktree(str(rattached.app), task, folder, "rev-parse", "HEAD")
    patch = diff_of_new_file(tmp_path, "tools/measure.sh", kit.MEASURE)
    with pytest.raises(AttachError, match="byte for byte"):
        runbooks.land(str(root), task, patch, message="m")
    assert gitops.in_worktree(str(rattached.app), task, folder, "rev-parse", "HEAD") == before


def test_land_still_takes_a_source_file_when_frozen_copies_exist(rattached, tmp_path):
    """Only the @studio part of a frozen contract is a pack: the repository files it froze beside it are not."""
    root = rattached.root
    write_pack(root)
    freeze_a_copy(root, "r-001", "research/measure.sh", kit.MEASURE)
    other = Path(root, ".alpaca", "research", "r-001", "contract", "files", "Makefile")
    other.write_text("all:\n")
    task, _folder = claimed(rattached)
    runbooks.land(str(root), task, diff_of_new_file(tmp_path, "Makefile2", "all:\n"), message="m")


def test_land_refuses_the_bytes_of_an_ignored_studio_file_under_a_neutral_name(rattached, tmp_path):
    """t-686 fix round 1, B2: the studio's blob set covers its whole working tree, `.alpaca/` included, so a patch cannot
    land a runtime file's bytes; land passes the sizes of the blobs it lands, as the handoff scan does."""
    root = rattached.root
    text = "runtime bytes that only the studio holds\nsecond line\n"
    Path(root, ".alpaca", "runtime-note.json").write_text(text)
    task, _folder = claimed(rattached)
    with pytest.raises(AttachError, match="byte for byte a file of the studio"):
        runbooks.land(str(root), task, diff_of_new_file(tmp_path, "docs/neutral.txt", text), message="m")
    runbooks.land(str(root), task, diff_of_new_file(tmp_path, "docs/other.txt", "plain repository text\n", name="q.patch"),
                  message="m")


def test_the_pack_blob_set_hashes_a_file_named_with_a_newline_and_survives_an_unreadable_file(rattached):
    """t-686 fix round 1, B2 sibling: the pack set skipped a path with a newline, and one unreadable file made git fail the
    whole batch and hide every other pack file."""
    root = rattached.root
    pack = root / "research"
    pack.mkdir(exist_ok=True)
    (pack / "odd\nname.txt").write_text("a pack file with a newline in its name\n")
    (pack / "fine.txt").write_text("a readable pack file\n")
    locked = pack / "locked.txt"
    locked.write_text("an unreadable pack file\n")
    locked.chmod(0)
    try:
        blobs = runbooks.pack_blob_ids(str(root))
    finally:
        locked.chmod(0o644)
    for name in ("odd\nname.txt", "fine.txt"):
        assert git(pack, "hash-object", "--no-filters", "--", name) in blobs


def test_the_frozen_pack_folders_skip_links_and_folders_that_leave_the_studio(rattached, tmp_path):
    root = rattached.root
    outside = tmp_path / "elsewhere"
    (outside / "contract" / "files" / "@studio").mkdir(parents=True)
    (outside / "contract" / "files" / "@studio" / "x.txt").write_text("secret\n")
    research = root / ".alpaca" / "research"
    research.mkdir(parents=True)
    os.symlink(str(outside), str(research / "r-evil"))                      # a run folder that is a link
    freeze_a_copy(root, "r-ok", "research/measure.sh", "m\n")
    assert [os.path.relpath(f, str(root)) for f in runbooks.frozen_pack_folders(str(root))] == [".alpaca/research/r-ok/contract/files/@studio"]
    assert runbooks.pack_blob_ids(str(root))                                  # hashed, and the linked one is not


def test_the_runbook_runs_only_from_a_studio_folder_outside_dot_alpaca(rattached):
    from alpaca.attach import runbooks as rb_mod
    (rattached.root / ".alpaca" / "x").mkdir(parents=True, exist_ok=True)
    (rattached.root / ".alpaca" / "x" / "r.yaml").write_text("runbook: 1\n")
    with pytest.raises(AttachError, match=r"\.alpaca/"):
        rb_mod._runbook_file(str(rattached.root), str(rattached.root / ".alpaca" / "x" / "r.yaml"))
