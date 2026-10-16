"""Studio material never leaves in a handoff (op-062 t-686; AC-10 at the handoff; the research pack follow-up of t-653).

The outgoing scan of t-651 judges the commits, objects, branch name, title and body. Here it is driven through
`handoff.plan`, and the research packs (the studio's `research/`, the folder of each recorded research runbook and the
frozen copies a run keeps) are checked by content and by path. Every refusal leaves the upstream with `main` only."""
from pathlib import Path

import pytest

from alpaca import barrier, db
from alpaca.attach import AttachError, approval, handoff, scan
from alpaca.tests.attachkit import (FakeGitHub, attached, owner_approve, ready, remote_branches, studio, upstream)  # noqa: F401

TASK = "t-001"


def studio_file(root, rel, text):
    path = Path(root) / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def refused(att, files, match, message="add a module", **kw):
    """Check and approve a branch carrying `files`, and expect the dry run and the push to refuse with `match`."""
    r = ready(att, files=files, message=message)
    owner_approve(r)
    with pytest.raises(AttachError, match=match):
        handoff.plan(r.root, TASK, **kw)
    with pytest.raises(AttachError):
        handoff.execute(r.root, TASK, expect=r.head, api=FakeGitHub(upstream=att.upstream), **kw)
    assert remote_branches(att.upstream) == ["main"]
    return r


def test_a_file_that_is_byte_for_byte_a_studio_file_is_refused_under_any_name(attached):
    studio_file(attached.root, "design/plan.md", "the studio's private plan, line one\nline two\n")
    refused(attached, {"docs/harmless-name.md": "the studio's private plan, line one\nline two\n"}, scan.R_STUDIO_BLOB)


def test_an_alpaca_path_is_refused(attached):
    refused(attached, {".alpaca/note.txt": "x\n"}, barrier.R_PROTECTED)


@pytest.mark.parametrize("name", ["RESUME.md", "CHECKLIST.md", "ALPACA-MANIFEST"])
def test_studio_only_file_names_are_refused(attached, name):
    refused(attached, {name: "x\n"}, barrier.R_PROTECTED)


def test_a_commit_message_with_the_studio_path_is_refused(attached):
    refused(attached, {"a.py": "x = 1\n"}, scan.R_TEXT, message="notes from %s" % attached.root.resolve())


def test_a_title_or_a_body_with_a_studio_string_is_refused(attached, tmp_path):
    r = ready(attached)
    owner_approve(r)
    with pytest.raises(AttachError, match=scan.R_TEXT):
        handoff.plan(r.root, TASK, title="see %s" % attached.root.resolve())
    body = tmp_path / "body.md"
    body.write_text("from session 192d7716-be95-4870-a09e-1780e634506b of op-062\n")
    with pytest.raises(AttachError, match=scan.R_TEXT):
        handoff.plan(r.root, TASK, body_file=str(body))
    assert remote_branches(attached.upstream) == ["main"]


# ---- the research packs (t-653 follow-up: the scan did not hash research/) -------------------------------------------------

PACK = "pack prompt: tune the model, held-out data is in the studio\n"


def test_a_file_of_the_studios_research_folder_is_refused_by_content(attached):
    studio_file(attached.root, "research/pack/prompt.md", PACK)
    refused(attached, {"docs/notes.md": PACK}, scan.R_PACK)


def test_a_pack_path_is_refused_even_when_the_bytes_differ(attached):
    studio_file(attached.root, "research/pack/prompt.md", PACK)
    refused(attached, {"research/pack/prompt.md": "an edited copy of the pack file\n"}, scan.R_PACK)


def test_the_folder_of_a_recorded_research_runbook_is_a_pack(attached):
    studio_file(attached.root, "packs/p1/runbook.yaml", "runbook: 3\n")
    studio_file(attached.root, "packs/p1/data.txt", "rows of the pack\n")
    conn = db.connect(str(attached.root))
    try:
        db.append_event(conn, session="cli", actor="alpaca", kind="research-contract", op="op-0",
                        data={"runbook": "packs/p1/runbook.yaml"})
    finally:
        conn.close()
    assert "packs/p1/data.txt" in scan.pack_path_set(str(attached.root))
    refused(attached, {"vendor/data.txt": "rows of the pack\n"}, scan.R_PACK)


def test_the_frozen_copy_of_an_older_pack_is_still_a_pack(attached):
    frozen = studio_file(attached.root, ".alpaca/research/run-1/contract/files/@studio/packs/p1/old.txt", "the pack as run 1 read it\n")
    assert frozen.is_file()
    refused(attached, {"vendor/old.txt": "the pack as run 1 read it\n"}, scan.R_PACK)


def test_a_branch_without_pack_files_passes_with_packs_present(attached):
    studio_file(attached.root, "research/pack/prompt.md", PACK)
    r = ready(attached, files={"src/feature.py": "VALUE = 2\n"})
    owner_approve(r)
    assert handoff.plan(r.root, TASK)["outgoing"]["files"] == ["A src/feature.py"]


def test_the_pack_sets_skip_links_and_a_missing_folder(attached, tmp_path):
    root = str(attached.root)
    assert scan.pack_path_set(root) == set() and scan.pack_blob_set(root) == set()
    (tmp_path / "outside").mkdir()
    (tmp_path / "outside" / "x.txt").write_text("outside\n")
    (attached.root / "research").symlink_to(tmp_path / "outside")
    assert scan.pack_path_set(root) == set() and scan.pack_blob_set(root) == set()


def test_research_is_one_of_the_studio_folders_the_blob_set_hashes(attached):
    studio_file(attached.root, "research/pack/prompt.md", PACK)
    assert "research" in scan.STUDIO_FOLDERS
    blob = barrier_blob(attached.root, "research/pack/prompt.md")
    assert blob in scan.studio_blob_set(str(attached.root))


def barrier_blob(root, rel):
    from alpaca.attach import gitops
    return gitops.git(str(root), "hash-object", "--no-filters", rel)


@pytest.mark.parametrize("rel", [".alpaca/instance.json", "RESUME.md", "intents/queue.md", "ALPACA-SETUP-NOTES.txt"])
def test_a_studio_file_with_no_studio_commit_is_refused_under_a_neutral_name(attached, rel):
    """t-686 fix round 1, B2: the fixture studio has no commit, the shape `alpaca new` leaves. The copy is refused at the
    dry run and the push, and no blob of it reaches the upstream."""
    from alpaca.attach import gitops
    text = "bytes that only the studio holds, in %s\nwith a second line\n" % rel
    studio_file(attached.root, rel, text)
    assert gitops.run(str(attached.root), "rev-parse", "--verify", "HEAD").rc != 0          # no commit
    r = refused(attached, {"docs/neutral-copy.txt": text}, scan.R_STUDIO_BLOB)
    blob = barrier_blob(attached.root, rel)
    objects = gitops.git(str(attached.upstream.bare), "cat-file", "--batch-all-objects", "--batch-check=%(objectname)")
    assert blob not in objects.split() and r.head


def test_a_clean_handoff_passes_with_the_gate_clones_and_work_folders_full_of_the_repositorys_bytes(attached):
    """The work folder, the gate seeds and the clone hold the branch's own files; none of them is a studio file."""
    r = ready(attached, files={"src/feature.py": "VALUE = 3\n"})
    assert (attached.root / ".alpaca/attach/worktrees/t-001/src/feature.py").is_file()
    studio_file(attached.root, ".alpaca/attach/runs/t-001/seed/src/feature.py", "VALUE = 3\n")
    studio_file(attached.root, "app/vendor/feature-copy.py", "VALUE = 3\n")
    owner_approve(r)
    assert handoff.plan(r.root, TASK)["outgoing"]["files"] == ["A src/feature.py"]
