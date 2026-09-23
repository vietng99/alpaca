"""E4 proof - the proof report, its seal, and the gate on done.

The owner's direction: a proof is not a path, it is the report an engineer hands in. This proves
the whole of that, positive and negative on every control:

  * `alpaca proof new` scaffolds the seven written sections plus Appendix A, with the header and
    the mechanical extract already filled from the record, and refuses to clobber a written
    report without `--force`;
  * `alpaca proof seal` refuses a missing, placeholder or thin section, an Evidence list with no
    pointer, an Evidence list with only a `remote:` ref, and any pointer that does not resolve
    (local, event, receipt, job; a receipt or job resolves only through the domain profile),
    and on success appends exactly ONE `proof-report` event with
    the report hash, the section sizes and the evidence hashes, WITHOUT editing the report;
  * a heading inside a fenced code block is body text, not a section, and the section floor
    counts the words the writer put there rather than the log they pasted under them;
  * a report cannot be its own evidence, and an evidence file that is empty, whitespace only, or
    a symlink out of the project root is refused;
  * the seal keeps a copy of every `local:` evidence file under `<id>.evidence/`, named for its
    position in the Evidence list, and a re-seal rebuilds that directory;
  * an evidence file the work moved on from is a NOTE and not a problem while the kept copy still
    hashes to the sealed value: the task closes, `proof check` passes, the doctor stays ok and
    counts it. It becomes a problem again the moment the kept copy is altered or goes;
  * a file over the copy cap, and a seal written before the copies existed, behave the way they
    always did, and the older one says which command repairs it;
  * `alpaca task move <id> done` and `alpaca board move <row> done` refuse a bare string, a
    `remote:` ref, a stale report hash AND evidence that is gone with no kept copy behind it, and
    print the three commands to run;
  * after a seal, an edited report, and an edited or deleted evidence file whose copy went with
    it, are each caught by `proof check` and by the doctor form;
  * a re-seal is a new event and the latest one wins;
  * a done task closed before this gate existed is counted as an unsealed legacy proof.
"""
import json
import os

import pytest

from alpaca import board, cli, db, profile, proof, util
from alpaca.gates import verdict as vc
from alpaca.tests import proofkit


# --------------------------------------------------------------------------- fixtures
class Fake(profile.Profile):
    """A stand-in domain profile: each keyword replaces one hook."""

    def __init__(self, **hooks):
        self.__dict__.update(hooks)


def _setup(project):
    cli.main(["init"])
    cli.main(["op", "new", "add the shout verb", "--done-when", "the verb prints upper case"])
    cli.main(["task", "add", "--title", "task", "op-001", "write the shout verb", "--phase", "build"])
    return db.connect(project)


def _row(conn, rid, *, op="op-001"):
    """One obligation row landed straight into the store, the way test_board.py seeds one."""
    r = {"id": rid, "kind": "item", "op": op, "phase": "build", "step": "s1",
         "statement": "do the thing properly here now", "proof": "local:spec.md", "where_": "",
         "how": "", "when_": "", "why": "", "session": None, "operator": None, "status": "open",
         "tag": "Specced", "content_hash": util.sha256_hex("row/" + rid), "prev_hash": None,
         "supersedes": None}
    db.upsert(conn, "rows", "id", r)
    return r


def _report(project, ident="t-001"):
    return os.path.join(project, ".alpaca", "proofs", "op-001", "%s.md" % ident)


def _kept_dir(project, ident="t-001"):
    return os.path.join(project, ".alpaca", "proofs", "op-001", "%s.evidence" % ident)


def _rel(project, path):
    """A path in the project as an Evidence line writes it."""
    return os.path.relpath(path, project).replace(os.sep, "/")


def _headings(text):
    return [ln[3:].strip() for ln in text.splitlines() if ln.startswith("## ")]


def _seal_problems(project, ident="t-001"):
    conn = db.connect(project)
    with pytest.raises(proof.Refusal) as e:
        proof.seal(conn, project, ident)
    return e.value.problems


# =========================================================================== proof new
def test_scaffold_writes_every_section_in_order_with_the_extract(project):
    conn = _setup(project)
    assert cli.main(["proof", "new", "t-001"]) == cli.PASS
    path = _report(project)
    assert os.path.isfile(path)
    text = util.read_text(path)
    # the seven written sections, then the appendix, in that order and no other.
    assert _headings(text) == list(proof.SECTIONS)
    # the header carries what the record and the host know.
    for fragment in ("- id: t-001 (task)", "- statement: write the shout verb", "- op: op-001",
                     "- done when: the verb prints upper case", "- sessions: ",
                     "- window: ", "- host: ", "- project: %s" % os.path.basename(project),
                     "- git: "):
        assert fragment in text, fragment
    # every written section starts with its one placeholder line.
    body = proof.split_sections(text)
    for name in proof.REQUIRED:
        assert body[name].strip().startswith(proof.TODO_MARK), name
    # the appendix is filled from the record: the task-add event is in the window.
    extract = body[proof.APPENDIX]
    assert "### Record events" in extract and "task-add" in extract
    assert "### Commands run" in extract and "### Files edited" in extract
    assert "### Test commands seen" in extract
    assert "### Runs and receipts" not in extract, "no profile, no run section"
    # nothing was written to the record by scaffolding.
    assert db.events(conn, kind=proof.KIND) == []


def test_scaffold_refuses_to_overwrite_a_written_report_without_force(project):
    _setup(project)
    assert cli.main(["proof", "new", "t-001"]) == cli.PASS
    util.write_text(_report(project), "my written report\n")
    assert cli.main(["proof", "new", "t-001"]) == cli.FAIL
    assert util.read_text(_report(project)) == "my written report\n"    # kept, byte for byte
    assert cli.main(["proof", "new", "t-001", "--force"]) == cli.PASS
    assert proof.TODO_MARK in util.read_text(_report(project))          # now a fresh scaffold


def test_scaffold_refuses_an_id_that_is_neither_a_task_nor_a_row(project):
    _setup(project)
    assert cli.main(["proof", "new", "t-999"]) == cli.FAIL
    assert not os.path.isdir(os.path.join(project, ".alpaca", "proofs"))


def test_scaffold_serves_a_checklist_row_too(project):
    conn = _setup(project)
    _row(conn, "r-1")
    assert cli.main(["proof", "new", "r-1"]) == cli.PASS
    text = util.read_text(_report(project, "r-1"))
    assert "- id: r-1 (row)" in text and _headings(text) == list(proof.SECTIONS)


# =========================================================================== proof seal refusals
def test_seal_refuses_a_report_that_was_never_scaffolded(project):
    _setup(project)
    problems = _seal_problems(project)
    assert any("no proof report at" in p for p in problems)
    assert cli.main(["proof", "seal", "t-001"]) == cli.FAIL


def test_seal_refuses_every_placeholder_section_by_name(project):
    _setup(project)
    cli.main(["proof", "new", "t-001"])
    problems = _seal_problems(project)
    for name in proof.REQUIRED:
        assert any(("section %r still holds" % name) in p for p in problems), name
    assert cli.main(["proof", "seal", "t-001"]) == cli.FAIL


def test_seal_refuses_a_missing_section(project):
    _setup(project)
    proofkit.write_report(project, "t-001")
    text = util.read_text(_report(project))
    # drop the Result heading and its body.
    cut = text.split("## Result", 1)[0] + text.split("## Deviations and issues", 1)[1]
    util.write_text(_report(project), cut)
    assert any("section 'Result' is missing" in p for p in _seal_problems(project))


def test_seal_refuses_a_thin_section(project):
    _setup(project)
    proofkit.write_report(project, "t-001")
    text = util.read_text(_report(project))
    text = text.replace(proofkit.FILLER, "it worked", 1)     # under the non-space floor
    util.write_text(_report(project), text)
    problems = _seal_problems(project)
    assert any("under the 40 floor" in p for p in problems), problems


# --------------------------------------------------------------------------- fences
# A `## Result` line inside a fenced code block is a line of somebody's log, not a heading. Read
# as a heading it either cuts the real section short or lets one blob of fenced headings stand in
# for every section. The floor counts the writer's own words for the same reason: a pasted log is
# what the work printed, not what the engineer has to say about it.
def test_split_sections_reads_a_heading_inside_a_fence_as_body_text():
    text = "\n".join(["## What I did", "",
                      "I rewrote the splitter. The log it prints looks like this:", "",
                      "```", "## Result", "## Deviations and issues", "```", "",
                      "## Result", "", "the real one"])
    got = proof.split_sections(text)
    assert sorted(got) == ["Result", "What I did"]
    assert "## Deviations and issues" in got["What I did"], "the real section was cut short"
    assert got["Result"].strip() == "the real one"


@pytest.mark.parametrize("opening,closing,closed", [
    ("```", "```", True),
    ("~~~", "~~~", True),
    ("````", "````", True),
    ("````", "```", False),              # a shorter run does not close a longer fence
    ("```", "~~~", False),               # nor does the other fence character
    ("```", "``` and more words", False),  # nor does a run carrying an info string
])
def test_a_fence_closes_only_on_a_matching_run(opening, closing, closed):
    text = "\n".join(["## A", "", opening, "## B", closing, "", "## C", "", "tail"])
    got = proof.split_sections(text)
    assert ("C" in got) is closed
    assert "B" not in got, "a heading inside a fence is never a section"


def test_seal_refuses_a_section_that_is_only_a_pasted_log(project):
    _setup(project)
    proofkit.write_report(project, "t-001")
    text = util.read_text(_report(project))
    log = "```\n2026-09-18 10:00:02 INFO run finished, 47 cells placed, 0 errors\n```"
    util.write_text(_report(project), text.replace(proofkit.FILLER, log, 1))
    problems = _seal_problems(project)
    assert any("'What I did'" in p and "a pasted log is not the section" in p
               for p in problems), problems


def test_seal_refuses_a_section_that_is_only_an_html_comment(project):
    _setup(project)
    proofkit.write_report(project, "t-001")
    text = util.read_text(_report(project))
    hidden = "<!--\n%s\n-->" % proofkit.FILLER
    util.write_text(_report(project), text.replace(proofkit.FILLER, hidden, 1))
    problems = _seal_problems(project)
    assert any("'What I did'" in p and "under the 40 floor" in p for p in problems), problems


def test_seal_accepts_prose_beside_a_fenced_block(project):
    conn = _setup(project)
    proofkit.write_report(project, "t-001", conn=conn)
    text = util.read_text(_report(project))
    both = "%s\n\n```\nbin/alpaca verify\nGATE alpaca-verify: PASS\n```\n" % proofkit.FILLER
    util.write_text(_report(project), text.replace(proofkit.FILLER, both, 1))
    payload = proof.seal(conn, project, "t-001", session="fences")
    # the recorded size is the prose, not the block: the pasted lines add nothing to it.
    assert payload["sections"]["What I did"] == proof.prose_len(proofkit.FILLER)
    assert payload["sections"]["What I did"] >= proof.MIN_CHARS


def test_seal_refuses_an_evidence_list_with_no_pointer(project):
    _setup(project)
    proofkit.write_report(project, "t-001", evidence=[])
    problems = _seal_problems(project)
    assert any("lists no pointer" in p for p in problems), problems


def test_seal_refuses_an_evidence_list_that_is_remote_only(project):
    _setup(project)
    proofkit.write_report(project, "t-001",
                          evidence=["remote:ci/build/91 - the shared run nobody here can reach"])
    problems = _seal_problems(project)
    assert any("no verifiable pointer" in p for p in problems), problems


@pytest.mark.parametrize("pointer,fragment", [
    ("local:nowhere.txt - a file that is not there", "does not resolve"),
    ("local:../escape.txt - a file outside the root", "does not resolve"),
    ("local:/etc/hostname - an absolute path", "does not resolve"),
    ("event:99999 - an event that was never appended", "names no event on the record"),
    ("receipt:deadbeef - a run receipt no profile resolves", "names no run receipt"),
    ("job:deadbeef - a run job no profile resolves", "names no run job"),
    ("file:anything.txt - a scheme nobody serves", "names no pointer scheme"),
])
def test_seal_refuses_a_pointer_that_does_not_resolve(project, pointer, fragment):
    _setup(project)
    proofkit.write_report(project, "t-001", evidence=[pointer])
    problems = _seal_problems(project)
    assert any(fragment in p for p in problems), problems


def test_seal_refuses_an_empty_evidence_file(project):
    _setup(project)
    util.write_text(os.path.join(project, "empty.log"), "")
    proofkit.write_report(project, "t-001", evidence=["local:empty.log - nothing at all"])
    assert any("empty file" in p for p in _seal_problems(project))


def test_seal_refuses_a_whitespace_only_evidence_file(project):
    """Zero bytes and three blank lines say the same nothing, so they meet the same refusal."""
    _setup(project)
    util.write_text(os.path.join(project, "blank.log"), "   \n\n\t\n")
    proofkit.write_report(project, "t-001", evidence=["local:blank.log - only blank lines"])
    assert any("holds only whitespace" in p for p in _seal_problems(project))


def test_seal_refuses_a_symlink_that_leaves_the_project_root(project):
    outside = os.path.join(os.path.dirname(project), "outside.log")
    util.write_text(outside, "a run that happened somewhere else entirely\n")
    _setup(project)
    os.symlink(outside, os.path.join(project, "run.log"))
    proofkit.write_report(project, "t-001", evidence=["local:run.log - a link out of the tree"])
    problems = _seal_problems(project)
    assert any("resolves outside the project root" in p for p in problems), problems


def test_seal_accepts_a_symlink_that_stays_inside_the_root(project):
    """The counterpart: it is the escape that is refused, not the symlink."""
    conn = _setup(project)
    util.write_text(os.path.join(project, "real.log"), "the run output, inside the tree\n")
    os.symlink(os.path.join(project, "real.log"), os.path.join(project, "link.log"))
    proofkit.write_report(project, "t-001", conn=conn,
                          evidence=["local:link.log - a link to a file in the tree"])
    payload = proof.seal(conn, project, "t-001", session="links")
    assert payload["evidence"][0]["verified"] is True


# --------------------------------------------------------------------------- self-citation
# A report that cites itself hashes the same bytes twice and calls it two sources. It proves
# nothing, so the seal refuses the report path, its `.seal.json` copy, and any file under
# `.alpaca/proofs/` named for the same id. Another task's report is a file like any other.
def test_seal_refuses_a_report_that_cites_itself(project):
    _setup(project)
    proofkit.write_report(project, "t-001",
                          evidence=["local:.alpaca/proofs/op-001/t-001.md - this very report"])
    problems = _seal_problems(project)
    assert any("cannot stand as its own evidence" in p for p in problems), problems


def test_seal_refuses_a_report_that_cites_its_own_seal_copy(project):
    _setup(project)
    proofkit.seal_for(project, "t-001")            # the .seal.json copy now exists on disk
    proofkit.write_report(project, "t-001", evidence=[
        "local:.alpaca/proofs/op-001/t-001.md.seal.json - the seal I wrote a moment ago"])
    problems = _seal_problems(project)
    assert any("cannot stand as its own evidence" in p for p in problems), problems


def test_seal_refuses_a_second_copy_of_the_report_for_the_same_id(project):
    _setup(project)
    util.write_text(os.path.join(project, ".alpaca", "proofs", "op-002", "t-001.md"),
                    "the same report filed under another op\n")
    proofkit.write_report(project, "t-001",
                          evidence=["local:.alpaca/proofs/op-002/t-001.md - a copy of itself"])
    problems = _seal_problems(project)
    assert any("another copy of the report for t-001" in p for p in problems), problems


def test_seal_accepts_another_tasks_sealed_report_as_evidence(project):
    conn = _setup(project)
    cli.main(["task", "add", "--title", "task", "op-001", "the task this one builds on", "--phase", "build"])
    proofkit.seal_for(project, "t-002", conn=conn)
    proofkit.write_report(project, "t-001", conn=conn, evidence=[
        "local:.alpaca/proofs/op-001/t-002.md - the sealed report this work builds on"])
    payload = proof.seal(conn, project, "t-001", session="cites")
    assert payload["evidence"][0]["pointer"] == "local:.alpaca/proofs/op-001/t-002.md"
    assert payload["evidence"][0]["verified"] is True


# =========================================================================== proof seal success
def test_seal_records_one_event_with_the_whole_shape(project):
    conn = _setup(project)
    ptr = proofkit.seal_for(project, "t-001")
    evs = db.events(conn, kind=proof.KIND)
    assert len(evs) == 1
    ev = evs[0]
    assert ev["ref"] == "t-001" and ev["op"] == "op-001"
    data = ev["data"]
    assert data["path"] == ".alpaca/proofs/op-001/t-001.md"
    assert ptr == "local:" + data["path"]
    path = _report(project)
    assert data["sha256"] == util.sha256_hex(open(path, "rb").read())
    assert data["bytes"] == os.path.getsize(path)
    # the section sizes are recorded per section, the written ones over the floor.
    assert set(proof.REQUIRED) <= set(data["sections"])
    assert all(data["sections"][n] >= proof.MIN_CHARS for n in proof.NARRATIVE)
    # one evidence entry, resolved and hashed.
    item = data["evidence"][0]
    assert item["kind"] == "local" and item["note"]
    assert item["sha256"] and item["bytes"] > 0 and item["verified"] is True


def test_seal_records_an_event_pointer_by_its_chain_hash_and_a_remote_unverified(project):
    conn = _setup(project)
    add = [e for e in db.events(conn, kind="task-add") if e["ref"] == "t-001"][0]
    proofkit.seal_for(project, "t-001", evidence=[
        "%s - the run output" % proofkit.evidence_file(project, "t-001"),
        "event:%d - the task-add row this closes" % add["id"],
        "remote:ci/build/91 - the shared run, recorded unverified"])
    data = db.events(conn, kind=proof.KIND)[-1]["data"]
    by_kind = {i["kind"]: i for i in data["evidence"]}
    assert by_kind["event"]["sha256"] == add["hash"] and by_kind["event"]["verified"] is True
    assert by_kind["remote"]["verified"] is False and by_kind["remote"]["sha256"] is None


def test_seal_never_edits_the_report_and_writes_the_convenience_copy(project):
    _setup(project)
    proofkit.write_report(project, "t-001")
    before = open(_report(project), "rb").read()
    assert cli.main(["proof", "seal", "t-001"]) == cli.PASS
    assert open(_report(project), "rb").read() == before, "sealing changed the report bytes"
    side = _report(project) + ".seal.json"
    assert os.path.isfile(side)
    conn = db.connect(project)
    assert json.loads(util.read_text(side)) == db.events(conn, kind=proof.KIND)[-1]["data"]


# =========================================================================== the done gate
def test_done_refuses_a_bare_pointer_and_prints_the_three_commands(project, capsys):
    _setup(project)
    util.write_text(os.path.join(project, "hello.py"), "print('hi')\n")
    assert cli.main(["task", "move", "t-001", "done", "--proof", "local:hello.py"]) == cli.FAIL
    out = capsys.readouterr().out
    assert "GATE alpaca-task-move: FAIL" in out
    assert "alpaca proof new t-001" in out and "alpaca proof seal t-001" in out
    conn = db.connect(project)
    assert db.rows(conn, "tasks", "id=?", ("t-001",))[0]["status"] != "done"


def test_done_refuses_no_proof_at_all(project):
    _setup(project)
    assert cli.main(["task", "move", "t-001", "done"]) == cli.FAIL


def test_done_refuses_a_remote_ref(project, capsys):
    _setup(project)
    proofkit.seal_for(project, "t-001")
    assert cli.main(["task", "move", "t-001", "done", "--proof", "remote:ci/build/91"]) == cli.FAIL
    assert "is not a local: pointer" in capsys.readouterr().out


def test_done_refuses_a_sealed_report_that_changed_since_the_seal(project, capsys):
    _setup(project)
    ptr = proofkit.seal_for(project, "t-001")
    with open(_report(project), "a", encoding="utf-8") as fh:
        fh.write("\none more thought after the seal\n")
    assert cli.main(["task", "move", "t-001", "done", "--proof", ptr]) == cli.FAIL
    assert "changed since it was sealed" in capsys.readouterr().out


def test_done_refuses_a_pointer_to_a_different_file(project):
    _setup(project)
    proofkit.seal_for(project, "t-001")
    util.write_text(os.path.join(project, "other.md"), "not the sealed report\n")
    assert cli.main(["task", "move", "t-001", "done", "--proof", "local:other.md"]) == cli.FAIL


def test_done_accepts_the_sealed_report(project):
    conn = _setup(project)
    ptr = proofkit.seal_for(project, "t-001")
    assert cli.main(["task", "move", "t-001", "done", "--proof", ptr]) == cli.PASS
    t = db.rows(conn, "tasks", "id=?", ("t-001",))[0]
    assert t["status"] == "done" and t["proof"] == ptr


# The report can hash true over evidence that is gone: hashing the report alone closes the task
# anyway. The gate asks `verify` the question `alpaca proof check` asks, so evidence that has gone
# with the copy the seal kept refuses the move with the same words.
def test_done_refuses_when_the_evidence_and_its_kept_copy_are_both_gone(project, capsys):
    conn = _setup(project)
    ptr = proofkit.seal_for(project, "t-001")
    os.remove(proofkit.evidence_path(project, "t-001"))
    os.remove(proofkit.kept_path(project, "t-001"))
    report = _report(project)
    ok, problems = proof.done_gate(conn, project, "t-001", ptr)
    assert not ok and any("evidence local:proofwork/t-001.txt is gone" in p
                          for p in problems), problems
    # the report itself never changed: it is the evidence under it that went.
    data = db.events(conn, kind=proof.KIND)[-1]["data"]
    assert data["sha256"] == util.sha256_hex(open(report, "rb").read())
    assert cli.main(["task", "move", "t-001", "done", "--proof", ptr]) == cli.FAIL
    assert "is gone" in capsys.readouterr().out
    assert db.rows(conn, "tasks", "id=?", ("t-001",))[0]["status"] != "done"


def test_done_refuses_a_sealed_report_whose_evidence_and_kept_copy_both_changed(project):
    conn = _setup(project)
    ptr = proofkit.seal_for(project, "t-001")
    rewritten = "a different run entirely\n"
    util.write_text(proofkit.evidence_path(project, "t-001"), rewritten)
    util.write_text(proofkit.kept_path(project, "t-001"), rewritten)
    ok, problems = proof.done_gate(conn, project, "t-001", ptr)
    assert not ok and any("the kept copy of local:proofwork/t-001.txt was altered" in p
                          for p in problems), problems
    assert cli.main(["task", "move", "t-001", "done", "--proof", ptr]) == cli.FAIL
    assert db.rows(conn, "tasks", "id=?", ("t-001",))[0]["status"] != "done"


def test_board_move_done_also_asks_after_the_evidence(project):
    conn = _setup(project)
    _row(conn, "r-ev")
    ptr = proofkit.seal_for(project, "r-ev")
    os.remove(proofkit.evidence_path(project, "r-ev"))
    os.remove(proofkit.kept_path(project, "r-ev"))
    assert cli.main(["board", "move", "r-ev", "done", "--proof", ptr]) == vc.FAIL
    assert board.view(conn)["counts"]["done"] == 0


def test_board_move_done_asks_for_the_same_sealed_report(project, capsys):
    conn = _setup(project)
    _row(conn, "r-1")
    assert cli.main(["board", "move", "r-1", "done", "--proof", "local:ALPACA-MANIFEST"]) == vc.FAIL
    out = capsys.readouterr().out
    assert "alpaca proof new r-1" in out and "alpaca proof seal r-1" in out
    ptr = proofkit.seal_for(project, "r-1")
    assert cli.main(["board", "move", "r-1", "done", "--proof", ptr]) == vc.PASS
    assert board.view(conn)["counts"]["done"] == 1


def test_task_move_of_a_checklist_row_asks_for_the_same_sealed_report(project):
    conn = _setup(project)
    _row(conn, "r-2")
    assert cli.main(["task", "move", "r-2", "done", "--proof", "local:ALPACA-MANIFEST"]) == cli.FAIL
    ptr = proofkit.seal_for(project, "r-2")
    assert cli.main(["task", "move", "r-2", "done", "--proof", ptr, "--level", "L2"]) == cli.PASS
    from alpaca.checklist import verdict_row
    assert verdict_row.status_fold(conn, "r-2") == verdict_row.DISCHARGED


def test_script_discharge_is_untouched_by_the_gate(project):
    """verdict_row.discharge, the path alpaca/flow/acceptance.py takes, still needs no report."""
    conn = _setup(project)
    r = _row(conn, "r-3")
    from alpaca.checklist import verdict_row
    verdict_row.discharge(conn, r["id"], r["content_hash"], instrument="alpaca-flow-acceptance",
                          verdict=vc.PASS, evidence=["local:ALPACA-MANIFEST"], level="L2",
                          session="flow")
    assert verdict_row.status_fold(conn, "r-3") == verdict_row.DISCHARGED


# =========================================================================== tamper detection
def test_check_passes_then_catches_an_edited_report(project, capsys):
    _setup(project)
    proofkit.seal_for(project, "t-001")
    assert cli.main(["proof", "check", "t-001"]) == cli.PASS
    with open(_report(project), "a", encoding="utf-8") as fh:
        fh.write("\nan edit made after the seal\n")
    assert cli.main(["proof", "check", "t-001"]) == cli.FAIL
    assert "the report .alpaca/proofs/op-001/t-001.md changed" in capsys.readouterr().out


def test_check_catches_an_edited_evidence_file_whose_copy_went_with_it(project, capsys):
    project_conn = _setup(project)
    proofkit.seal_for(project, "t-001")
    kept = proofkit.kept_path(project, "t-001")
    util.write_text(proofkit.evidence_path(project, "t-001"), "a different run\n")
    os.remove(kept)
    assert cli.main(["proof", "check", "--all"]) == cli.FAIL
    out = capsys.readouterr().out
    assert "evidence local:proofwork/t-001.txt changed since it was sealed" in out
    assert "the copy kept at .alpaca/proofs/op-001/t-001.evidence/01-t-001.txt is gone too" in out
    ok, problems = proof.verify(project_conn, project, "t-001")
    assert not ok and problems


def test_check_catches_a_deleted_evidence_file_whose_copy_went_with_it(project, capsys):
    _setup(project)
    proofkit.seal_for(project, "t-001")
    os.remove(proofkit.kept_path(project, "t-001"))
    os.remove(proofkit.evidence_path(project, "t-001"))
    assert cli.main(["proof", "check", "t-001"]) == cli.FAIL
    assert "is gone" in capsys.readouterr().out


def test_check_catches_a_deleted_report(project):
    _setup(project)
    proofkit.seal_for(project, "t-001")
    os.remove(_report(project))
    assert cli.main(["proof", "check", "t-001"]) == cli.FAIL


def test_check_with_nothing_sealed_is_blocked_not_a_vacuous_pass(project):
    _setup(project)
    assert cli.main(["proof", "check", "--all"]) == cli.BLOCKED


# =========================================================================== the kept copies
# A report cites files that the work goes on editing. Re-hashing only the path the report named
# calls the report false a week later, when all that happened is that the tree moved on. So the
# seal keeps the attachment: the copy is the evidence as it was, and a later check reads it when
# the original has drifted.
def test_seal_keeps_a_copy_of_every_local_evidence_file(project):
    conn = _setup(project)
    util.write_text(os.path.join(project, "proofwork", "run one.txt"), "the first run\n")
    util.write_text(os.path.join(project, "proofwork", "second.log"), "the second run\n")
    add = [e for e in db.events(conn, kind="task-add") if e["ref"] == "t-001"][0]
    proofkit.seal_for(project, "t-001", conn=conn, evidence=[
        "local:proofwork/run one.txt - the first run",
        "event:%d - the task-add row this closes" % add["id"],
        "local:proofwork/second.log - the second run"])

    # one copy per local pointer, named for its position in the Evidence list, the basename
    # slugged. The event pointer is position two and has no file to keep.
    assert sorted(os.listdir(_kept_dir(project))) == ["01-run_one.txt", "03-second.log"]
    by_ptr = {i["pointer"]: i for i in db.events(conn, kind=proof.KIND)[-1]["data"]["evidence"]}
    first = by_ptr["local:proofwork/run one.txt"]
    assert first["kept"] == ".alpaca/proofs/op-001/t-001.evidence/01-run_one.txt"
    assert by_ptr["local:proofwork/second.log"]["kept"] == \
        ".alpaca/proofs/op-001/t-001.evidence/03-second.log"
    assert by_ptr["event:%d" % add["id"]]["kept"] is None
    # the copy is the bytes, and it hashes to what the seal recorded for the pointer.
    kept = os.path.join(project, first["kept"].replace("/", os.sep))
    assert util.read_text(kept) == "the first run\n"
    assert util.sha256_hex(open(kept, "rb").read()) == first["sha256"]
    # nothing else in the entry moved: `kept` sits beside the keys that were always there.
    assert first["bytes"] == len("the first run\n") and first["verified"] is True
    # the seal copy on disk carries the same shape as the event.
    side = json.loads(util.read_text(_report(project) + ".seal.json"))
    assert side["evidence"] == db.events(conn, kind=proof.KIND)[-1]["data"]["evidence"]


def test_an_edited_evidence_file_is_a_note_and_the_proof_still_stands(project, capsys):
    conn = _setup(project)
    ptr = proofkit.seal_for(project, "t-001")
    assert cli.main(["task", "move", "t-001", "done", "--proof", ptr]) == cli.PASS
    kept = proofkit.kept_path(project, "t-001")
    util.write_text(proofkit.evidence_path(project, "t-001"), "the run I did after that one\n")

    got = proof.verify_detail(conn, project, "t-001")
    assert got["ok"] and got["problems"] == []
    assert got["notes"] == ["evidence local:proofwork/t-001.txt changed since sealing; the sealed "
                            "copy is kept at .alpaca/proofs/op-001/t-001.evidence/01-t-001.txt"]
    assert proof.verify(conn, project, "t-001") == (True, [])
    assert proof.done_gate(conn, project, "t-001", ptr) == (True, [])
    # the copy still reads as the evidence did when the report was written.
    assert util.read_text(kept) == "run output for t-001\nexit 0\n"

    assert cli.main(["proof", "check", "t-001"]) == cli.PASS
    out = capsys.readouterr().out
    assert "GATE alpaca-proof-check: PASS" in out
    assert ("evidence local:proofwork/t-001.txt changed since sealing; the sealed copy is kept "
            "at .alpaca/proofs/op-001/t-001.evidence/01-t-001.txt") in out

    name, level, detail = proof.checks(project, conn)[0]
    assert (name, level) == ("proof-integrity", "ok")
    assert detail == ("1 done task(s), 1 sealed; 1 evidence file(s) changed since sealing, "
                      "sealed copies intact")


def test_a_deleted_evidence_file_is_a_note_too(project, capsys):
    conn = _setup(project)
    ptr = proofkit.seal_for(project, "t-001")
    assert cli.main(["task", "move", "t-001", "done", "--proof", ptr]) == cli.PASS
    os.remove(proofkit.evidence_path(project, "t-001"))
    got = proof.verify_detail(conn, project, "t-001")
    assert got["ok"] and got["problems"] == [] and len(got["notes"]) == 1
    assert "the sealed copy is kept at" in got["notes"][0]
    assert proof.done_gate(conn, project, "t-001", ptr) == (True, [])
    assert cli.main(["proof", "check", "t-001"]) == cli.PASS
    assert "the sealed copy is kept at" in capsys.readouterr().out
    _name, level, detail = proof.checks(project, conn)[0]
    assert level == "ok" and "1 evidence file(s) changed since sealing" in detail


def test_a_tampered_kept_copy_is_a_problem(project, capsys):
    conn = _setup(project)
    ptr = proofkit.seal_for(project, "t-001")
    assert cli.main(["task", "move", "t-001", "done", "--proof", ptr]) == cli.PASS
    rewritten = "a run that never happened\n"
    util.write_text(proofkit.evidence_path(project, "t-001"), rewritten)
    util.write_text(proofkit.kept_path(project, "t-001"), rewritten)
    ok, problems = proof.verify(conn, project, "t-001")
    assert not ok
    assert any("the kept copy of local:proofwork/t-001.txt was altered" in p
               for p in problems), problems
    assert cli.main(["proof", "check", "t-001"]) == cli.FAIL
    assert "was altered" in capsys.readouterr().out
    _name, level, detail = proof.checks(project, conn)[0]
    assert level == "warn" and "the kept copy of local:proofwork/t-001.txt was altered" in detail


def test_a_kept_copy_that_is_altered_under_an_untouched_original_is_not_a_problem(project):
    """The original is what the report named. While it still hashes true the proof stands on it,
    and the copy is the spare nobody had to reach for."""
    conn = _setup(project)
    proofkit.seal_for(project, "t-001")
    util.write_text(proofkit.kept_path(project, "t-001"), "somebody scribbled on the copy\n")
    assert proof.verify(conn, project, "t-001") == (True, [])


def test_a_file_over_the_cap_is_recorded_with_no_copy_and_behaves_like_an_old_seal(
        project, monkeypatch):
    conn = _setup(project)
    monkeypatch.setattr(proof, "KEPT_FILE_CAP", 64)
    big = os.path.join(project, "proofwork", "big.log")
    util.write_text(big, "x" * 200 + "\n")
    proofkit.seal_for(project, "t-001", conn=conn,
                      evidence=["local:proofwork/big.log - a run log over the cap"])
    item = db.events(conn, kind=proof.KIND)[-1]["data"]["evidence"][0]
    assert item["kept"] is None and item["kept_reason"] == "over cap"
    assert item["sha256"] and item["bytes"] == 201        # hashed and recorded all the same
    assert not os.path.isdir(_kept_dir(project))
    # with no copy behind it, a changed original is the problem it always was. Re-sealing would
    # not keep this one either, so the repair line is not offered.
    util.write_text(big, "x" * 200 + "and one line more\n")
    ok, problems = proof.verify(conn, project, "t-001")
    assert not ok and problems == ["t-001: evidence local:proofwork/big.log changed since it was "
                                   "sealed"]


def test_the_total_cap_stops_at_the_report_budget(project, monkeypatch):
    conn = _setup(project)
    monkeypatch.setattr(proof, "KEPT_TOTAL_CAP", 40)
    util.write_text(os.path.join(project, "proofwork", "a.log"), "a" * 30 + "\n")
    util.write_text(os.path.join(project, "proofwork", "b.log"), "b" * 30 + "\n")
    proofkit.seal_for(project, "t-001", conn=conn, evidence=[
        "local:proofwork/a.log - the first run", "local:proofwork/b.log - the second run"])
    kept = db.events(conn, kind=proof.KIND)[-1]["data"]["evidence"]
    assert kept[0]["kept"] == ".alpaca/proofs/op-001/t-001.evidence/01-a.log"
    assert kept[1]["kept"] is None and kept[1]["kept_reason"] == "over cap"
    assert os.listdir(_kept_dir(project)) == ["01-a.log"]


def test_a_reseal_rebuilds_the_kept_copies(project):
    conn = _setup(project)
    util.write_text(os.path.join(project, "proofwork", "a.log"), "the first run\n")
    util.write_text(os.path.join(project, "proofwork", "b.log"), "the second run\n")
    proofkit.seal_for(project, "t-001", conn=conn,
                      evidence=["local:proofwork/a.log - the run"])
    assert os.listdir(_kept_dir(project)) == ["01-a.log"]

    util.write_text(os.path.join(project, "proofwork", "a.log"), "the run I did again\n")
    proofkit.seal_for(project, "t-001", conn=conn, evidence=[
        "local:proofwork/a.log - the run I did again", "local:proofwork/b.log - and the second"])
    assert sorted(os.listdir(_kept_dir(project))) == ["01-a.log", "02-b.log"]
    assert util.read_text(os.path.join(_kept_dir(project), "01-a.log")) == "the run I did again\n"
    assert proof.verify(conn, project, "t-001") == (True, [])

    # a pointer the new report drops takes its copy with it: the directory is rebuilt, not added to.
    proofkit.seal_for(project, "t-001", conn=conn,
                      evidence=["local:proofwork/b.log - only this one now"])
    assert os.listdir(_kept_dir(project)) == ["01-b.log"]
    assert util.read_text(os.path.join(_kept_dir(project), "01-b.log")) == "the second run\n"


def test_a_seal_from_before_the_copies_keeps_todays_behaviour_and_says_how_to_repair_it(
        project, capsys):
    """A `proof-report` event with no `kept` key at all is what every seal wrote until now. The
    file under it changed and nothing kept what it was, so it refuses exactly as it used to, and
    names the one command that keeps the file from here on."""
    conn = _setup(project)
    ptr = proofkit.seal_for(project, "t-001")
    old = json.loads(json.dumps(db.events(conn, kind=proof.KIND)[-1]["data"]))
    for item in old["evidence"]:
        item.pop("kept", None)
        item.pop("kept_reason", None)
    db.append_event(conn, session="legacy", actor="agent", kind=proof.KIND, op="op-001",
                    ref="t-001", data=old)
    assert cli.main(["task", "move", "t-001", "done", "--proof", ptr]) == cli.PASS
    util.write_text(proofkit.evidence_path(project, "t-001"), "a different run\n")

    got = proof.verify_detail(conn, project, "t-001")
    assert got["notes"] == [] and not got["ok"]
    assert got["problems"] == ["t-001: evidence local:proofwork/t-001.txt changed since it was "
                              "sealed; re-run alpaca proof seal t-001 to keep a copy of the "
                              "evidence as it is now"]
    assert cli.main(["proof", "check", "t-001"]) == cli.FAIL
    assert "re-run alpaca proof seal t-001" in capsys.readouterr().out
    _name, level, detail = proof.checks(project, conn)[0]
    assert level == "warn" and "re-run alpaca proof seal t-001" in detail
    # and the repair does what it says: sealing again keeps the file as it is now.
    assert cli.main(["proof", "seal", "t-001"]) == cli.PASS
    assert proof.verify(conn, project, "t-001") == (True, [])
    assert util.read_text(proofkit.kept_path(project, "t-001")) == "a different run\n"


def test_seal_refuses_a_pointer_into_its_own_kept_evidence_directory(project):
    """The copies under `<id>.evidence/` came from pointers this report already carried. Citing
    one back is the report standing on itself, one copy further out."""
    conn = _setup(project)
    proofkit.seal_for(project, "t-001", conn=conn)
    mine = _rel(project, proofkit.kept_path(project, "t-001"))
    proofkit.write_report(project, "t-001", conn=conn,
                          evidence=["local:%s - the copy the last seal kept" % mine])
    problems = _seal_problems(project)
    assert any("is a copy this seal kept for t-001" in p for p in problems), problems


def test_seal_accepts_the_kept_copy_of_another_task(project):
    """The counterpart: another task's kept copy is a file in the tree like any other."""
    conn = _setup(project)
    cli.main(["task", "add", "--title", "task", "op-001", "the task this one builds on", "--phase", "build"])
    proofkit.seal_for(project, "t-002", conn=conn)
    theirs = _rel(project, proofkit.kept_path(project, "t-002"))
    proofkit.write_report(project, "t-001", conn=conn,
                          evidence=["local:%s - the evidence t-002 sealed" % theirs])
    payload = proof.seal(conn, project, "t-001", session="cites")
    assert payload["evidence"][0]["verified"] is True
    assert payload["evidence"][0]["kept"] == ".alpaca/proofs/op-001/t-001.evidence/01-01-t-002.txt"


# =========================================================================== re-seal
def test_a_reseal_is_a_new_event_and_the_latest_one_wins(project):
    conn = _setup(project)
    ptr = proofkit.seal_for(project, "t-001")
    first = db.events(conn, kind=proof.KIND)[-1]
    with open(_report(project), "a", encoding="utf-8") as fh:
        fh.write("\nI went back and added the number I had left out: 47 cells.\n")
    assert cli.main(["task", "move", "t-001", "done", "--proof", ptr]) == cli.FAIL
    assert cli.main(["proof", "seal", "t-001"]) == cli.PASS
    evs = db.events(conn, kind=proof.KIND)
    assert len(evs) == 2, "a re-seal appends, it never rewrites"
    assert evs[-1]["data"]["sha256"] != first["data"]["sha256"]
    assert proof.latest_seal(conn, "t-001")["id"] == evs[-1]["id"]
    assert cli.main(["task", "move", "t-001", "done", "--proof", ptr]) == cli.PASS


# =========================================================================== the doctor form
def test_doctor_form_is_silent_without_a_done_task(project):
    conn = _setup(project)
    assert proof.checks(project, conn) == []


def test_doctor_form_reports_ok_then_names_the_drift(project):
    conn = _setup(project)
    ptr = proofkit.seal_for(project, "t-001")
    cli.main(["task", "move", "t-001", "done", "--proof", ptr])
    name, level, detail = proof.checks(project, conn)[0]
    assert (name, level) == ("proof-integrity", "ok") and "1 sealed" in detail
    util.write_text(proofkit.evidence_path(project, "t-001"), "a different run\n")
    os.remove(proofkit.kept_path(project, "t-001"))
    name, level, detail = proof.checks(project, conn)[0]
    assert level == "warn" and "t-001" in detail and "changed since it was sealed" in detail


def test_doctor_form_counts_an_unsealed_legacy_proof(project):
    """A task closed before this gate existed carries a bare string and no seal. It is counted
    and named, never silently passed."""
    conn = _setup(project)
    db.append_event(conn, session="legacy", actor="human", kind="task-move", op="op-001",
                    ref="t-001", data={"from": "open", "to": "done", "proof": "local:hello.py"})
    db.patch(conn, "tasks", "id", "t-001", {"status": "done", "proof": "local:hello.py"})
    name, level, detail = proof.checks(project, conn)[0]
    assert (name, level) == ("proof-integrity", "warn")
    assert "1 done task(s) carry an unsealed legacy proof: t-001" in detail


def test_doctor_form_reports_drift_and_the_legacy_count_together(project):
    conn = _setup(project)
    cli.main(["task", "add", "--title", "task", "op-001", "the second task", "--phase", "verify"])
    ptr = proofkit.seal_for(project, "t-001")
    cli.main(["task", "move", "t-001", "done", "--proof", ptr])
    db.patch(conn, "tasks", "id", "t-002", {"status": "done", "proof": "local:hello.py"})
    os.remove(proofkit.kept_path(project, "t-001"))
    os.remove(proofkit.evidence_path(project, "t-001"))
    _name, level, detail = proof.checks(project, conn)[0]
    assert level == "warn" and "t-001" in detail and "t-002" in detail


# =========================================================================== the extract sources
def test_the_extract_reads_the_tool_pool_first(project):
    conn = _setup(project)
    sid = db.events(conn, kind="task-add")[-1]["session"]
    pool = os.path.join(project, ".alpaca", "pool", "tools", "%s.jsonl" % sid)
    util.write_text(pool, "\n".join(json.dumps(r) for r in [
        {"ts": "2026-09-18T10:00:00+07:00", "sid": sid, "phase": "pre", "tool": "Bash",
         "tool_use_id": "tu-1", "input": {"command": "pytest -q alpaca/tests/test_proof.py"}},
        {"ts": "2026-09-18T10:00:02+07:00", "sid": sid, "phase": "post", "tool": "Bash",
         "tool_use_id": "tu-1", "input": {"command": "pytest -q alpaca/tests/test_proof.py"}},
        {"ts": "2026-09-18T10:01:00+07:00", "sid": sid, "phase": "post", "tool": "Edit",
         "tool_use_id": "tu-2", "input": {"file_path": "alpaca/proof.py"}},
    ]) + "\n")
    text = proof.render(conn, project, "t-001")
    assert "Source: pool." in text
    # the pre/post pair folds to ONE command, and it is recognised as a test command.
    assert text.count("pytest -q alpaca/tests/test_proof.py") == 2   # commands + test commands
    assert "- alpaca/proof.py (1 write(s))" in text


def test_the_extract_falls_back_to_the_transcript_then_to_the_record(project):
    conn = _setup(project)
    sid = db.events(conn, kind="task-add")[-1]["session"]
    tpath = os.path.join(project, ".alpaca", "transcripts", "%s.jsonl" % sid)
    util.write_text(tpath, json.dumps({
        "type": "assistant", "timestamp": "2026-09-18T10:00:00+07:00",
        "message": {"content": [{"type": "tool_use", "name": "Bash",
                                 "input": {"command": "make test"}}]}}) + "\n")
    text = proof.render(conn, project, "t-001")
    assert "Source: transcript." in text and "make test" in text
    os.remove(tpath)
    db.append_event(conn, session=sid, actor="agent", kind="heartbeat",
                    data={"tool": "Bash", "ref": "pytest sha256:0123456789ab"})
    text = proof.render(conn, project, "t-001")
    assert "Source: record heartbeats." in text and "pytest sha256:0123456789ab" in text


def test_the_extract_survives_an_absent_pool_and_names_the_profile_runs(project, monkeypatch):
    conn = _setup(project)
    sid = db.events(conn, kind="task-add")[-1]["session"]
    db.append_event(conn, session=sid, actor="alpaca", kind="domain-receipt", op="op-001",
                    data={"receipt_id": "r9", "job_id": "j9", "stage": "stage-a",
                          "verdict": "PASS", "metrics": {"cells": 570}})
    seen = []

    def lines(events):
        seen.extend(e["kind"] for e in events)
        return ["- receipt %s stage=%s verdict=%s" % (e["data"]["receipt_id"], e["data"]["stage"],
                                                        e["data"]["verdict"])
                for e in events if e["kind"] == "domain-receipt"]
    monkeypatch.setattr(profile, "load", lambda root: Fake(proof_lines=lines))
    text = proof.render(conn, project, "t-001")
    assert "domain-receipt" in seen and "task-add" in seen, "the profile reads the window's events"
    assert "### Runs and receipts" in text
    assert "- receipt r9 stage=stage-a verdict=PASS" in text
    assert "Source: none." in text          # no pool, no transcript, no heartbeat


def test_a_run_pointer_resolves_through_the_profile_and_stays_in_the_root(project, monkeypatch, tmp_path):
    _setup(project)
    receipt = os.path.join(project, ".alpaca", "domain", "receipts", "r9.json")
    util.write_text(receipt, '{"receipt_id": "r9", "verdict": "PASS"}\n')
    outside = str(tmp_path / "outside.json")
    util.write_text(outside, '{"secret": true}\n')
    files = {"r9": receipt, "r0": outside}
    monkeypatch.setattr(profile, "load", lambda root: Fake(
        evidence_file=lambda root, kind, ident: files.get(ident) if kind == "receipt" else None))
    proofkit.write_report(project, "t-001", evidence=["receipt:r9 - the run receipt"])
    conn = db.connect(project)
    sealed = proof.seal(conn, project, "t-001")
    kinds = {e["pointer"]: e for e in sealed["evidence"]}
    assert kinds["receipt:r9"]["verified"] is True and kinds["receipt:r9"]["bytes"] > 0
    proofkit.write_report(project, "t-001", evidence=["receipt:r0 - a receipt outside the root"])
    assert any("names no run receipt" in p for p in _seal_problems(project))


def test_the_extract_caps_the_command_list_and_says_how_many_it_left_out(project, monkeypatch):
    conn = _setup(project)
    sid = db.events(conn, kind="task-add")[-1]["session"]
    monkeypatch.setattr(proof, "COMMAND_CAP", 5)
    pool = os.path.join(project, ".alpaca", "pool", "tools", "%s.jsonl" % sid)
    util.write_text(pool, "\n".join(json.dumps(
        {"ts": "2026-09-18T10:00:00+07:00", "sid": sid, "phase": "post", "tool": "Bash",
         "tool_use_id": "tu-%d" % i, "input": {"command": "echo %d" % i}}) for i in range(9)) + "\n")
    text = proof.render(conn, project, "t-001")
    assert "4 further command(s) ran and are not listed here" in text
    assert "echo 4" in text and "echo 5" not in text


# =========================================================================== end to end
def test_the_scaffolded_template_seals_once_an_agent_fills_it_then_catches_a_tamper(project):
    """The whole round trip on the shipped scaffold: `proof new`, an agent writing every section
    in sentences over a fenced command block, `proof seal`, the done gate, then one tamper."""
    conn = _setup(project)
    assert cli.main(["proof", "new", "t-001"]) == cli.PASS
    path = _report(project)
    util.write_text(os.path.join(project, "proofwork", "run.txt"),
                    "bin/alpaca verify\nGATE alpaca-verify: PASS (14 checks)\n")
    written = ("I wrote the shout verb, wired it into the parser and ran the suite over it twice. "
               "The command and the line it printed are below, and the run file holds both.\n"
               "\n"
               "```\n"
               "bin/alpaca verify\n"
               "GATE alpaca-verify: PASS (14 checks)\n"
               "```\n")
    util.write_text(path, proofkit.fill(
        util.read_text(path), ["local:proofwork/run.txt - the run and the GATE line it printed"],
        written))

    # every written section now carries prose of its own on top of the pasted block.
    body = proof.split_sections(util.read_text(path))
    assert list(body) == list(proof.SECTIONS)
    for name in proof.NARRATIVE:
        assert proof.prose_len(body[name]) >= proof.MIN_CHARS, name

    assert cli.main(["proof", "seal", "t-001"]) == cli.PASS
    ptr = "local:.alpaca/proofs/op-001/t-001.md"
    assert proof.done_gate(conn, project, "t-001", ptr) == (True, [])
    assert cli.main(["proof", "check", "t-001"]) == cli.PASS

    # the run file goes while the report stays byte for byte the same. The copy the seal kept
    # still holds it, so the proof stands and the gate says so in a note rather than a refusal.
    before = open(path, "rb").read()
    kept = proofkit.kept_path(project, "t-001")
    assert util.read_text(kept) == "bin/alpaca verify\nGATE alpaca-verify: PASS (14 checks)\n"
    os.remove(os.path.join(project, "proofwork", "run.txt"))
    assert open(path, "rb").read() == before
    got = proof.verify_detail(conn, project, "t-001")
    assert got["ok"] and got["problems"] == [] and len(got["notes"]) == 1
    assert proof.done_gate(conn, project, "t-001", ptr) == (True, [])
    assert cli.main(["proof", "check", "t-001"]) == cli.PASS

    # tamper: the kept copy goes too, and now nothing holds what the report stood on.
    os.remove(kept)
    ok, problems = proof.done_gate(conn, project, "t-001", ptr)
    assert not ok and any("is gone" in p for p in problems), problems
    assert cli.main(["task", "move", "t-001", "done", "--proof", ptr]) == cli.FAIL
    assert db.rows(conn, "tasks", "id=?", ("t-001",))[0]["status"] != "done"


def test_the_report_is_stable_across_two_renders_apart_from_the_clock(project):
    conn = _setup(project)
    first = proof.render(conn, project, "t-001")
    second = proof.render(conn, project, "t-001")
    strip = lambda t: "\n".join(l for l in t.splitlines() if not l.startswith("- window: "))
    assert strip(first) == strip(second)


def test_the_seal_refuses_a_report_that_does_not_name_a_failed_job_in_its_window(project, monkeypatch):
    """The C1/K1 cure: a run narrative may not be true by omission.

    A report once credited a whole run with a pass while two of its jobs carried `verdict: FAIL`,
    and a third failed job appeared in no section. The seal accepted it, because nothing compared
    the report against the job ledger. The domain profile names the failed runs in the window.
    """
    _setup(project)
    asked = []

    def failed_runs(root, first, now):
        asked.append((first, now))
        return [{"job_id": "a" * 32, "stage": "stage-a", "verdict": "FAIL",
                 "reason": "stage-a command exited 2"}, {"stage": "no id, ignored"}]
    monkeypatch.setattr(profile, "load", lambda root: Fake(failed_runs=failed_runs))
    found = proof.failed_jobs_in_window(project, None, None)
    assert [j["job_id"] for j in found] == ["a" * 32]
    proofkit.write_report(project, "t-001")
    problems = _seal_problems(project)
    assert any("run %s (stage-a) ended FAIL" % ("a" * 32) in p for p in problems), problems
    assert asked and asked[-1][0] is not None, "the gate hands the report window to the profile"
    # positive: a report that names the failed job seals
    proofkit.write_report(project, "t-001", filler="job %s failed and was rerun; " % ("a" * 32))
    conn = db.connect(project)
    assert proof.seal(conn, project, "t-001")["path"]
    # negative control: without a profile there is no run to name
    monkeypatch.setattr(profile, "load", lambda root: profile.EMPTY)
    assert proof.failed_jobs_in_window(project, None, None) == []
