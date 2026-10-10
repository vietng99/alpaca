"""The gate chain, fail-to-pass, pass-to-pass, metrics, markers and machine locks (op-009 t-093).

Done when (t-093's contract and design/research-engine/architecture.md section 3.3):
  * a test that already passes on the base tree is rejected as weak;
  * a metric run records every repeat and the median;
  * the first failing gate stops the chain and is named;
  * heavy gates take a machine lock, so two slots never run the same heavy gate at once (F07);
  * the eval runs in a jail with no network access, and the gate record says whether it did (H04);
  * fail case: a gate result is never reused after its inputs changed (EC-008).
Also covered: every research-gate event holds the command, return code, log and input
fingerprint (FR-022); a log without its boundary markers, or with a test named twice with
different results, is its own verdict (FR-025); pass-to-pass runs the frozen suite and fails on a
lost test (FR-024); the host judges only the by-bytes copy of the evidence (judged_copy); plugin
checks keep runbook._run_plugin's environment keys and verdict mapping (section 13.2); a
verifier's re-run runs one stage on one tree and a bad request is refused.
Check round 1 added: a log holds exactly one marker block, and a run killed by a signal gives no
TEST report; the fingerprint covers the read paths, an unreadable entry and the top .git, and is
taken after the heavy lock; every run keeps its own log and judged copy, a reused event keeps
the reused run's log hash, and a verifier's re-run works in its own try folder; a read path
finding (or a read path check that cannot run) blocks a gate jail; an evidence name that is not
UTF-8 is refused and recorded; DD-11's spread rule holds for any metric; the reproducer event
must say proved.
Check round 2 added: a changed contract, a changed tool file and a reproducer that was replaced
and proved again each make the item run again instead of reusing the recorded result.
Check round 3 added: a line inside the block that holds a marker token must be exactly its
marker line, and a line break other than a newline is refused, so an unterminated prefix cannot
hide the harness's own line; a prefixed BEGIN or END after the block is refused; the two-slot
test runs at tier 2; a tree with a socket, FIFO or device is refused before its gate jail; a
report and an evidence folder are bounded in size.
Check round 1 of the second generation added: a metric and the fail-to-pass and pass-to-pass
sides are reused only while their tree is unchanged, and their events hold command, rc, log and
fingerprint; a block counts only when it ends with the run's own token from fd 3, so tested code
that prints a bare END and exits 0 forges nothing; the middle of two large values does not
overflow and a non-finite median or spread is refused; a write error while copying the evidence
is recorded as BLOCKED and the copy is bounded in bytes; a log over its cap stops the run and
still records one event, and the host reads only a bounded part of a log; code in the tree
cannot move a frozen script's folder aside; the offline flag is read from the jail's argv.
Fix round 10 (builder attacks A3a and A3b) added: the tree digest writes each entry as one JSON
line with its permission bits, so a file name that spells another tree's lines, or a chmod,
is never answered with a reused result.
Check round 6 added: every gate jail runs under the log relay, so its stdout is a socket and no
process in the jail can seek, truncate or reopen the log; a background patcher cannot rewrite
the runner's METRIC value or turn the attempt's FAIL into PASS; the relay keeps the return code,
enforces the log cap and drains a log it cannot write.
Check round 7 added: a result is reused only when its run left its tree unchanged, so a stage
that writes into its tree (a build, the repair prepare step) runs again on a rebuilt tree, and
the prepare step is never reused; a folder above a frozen file that the host cannot search, an
evidence folder nested past its depth bound and a deep home each leave one recorded event
instead of a raise, and a home that cannot be removed is recorded without changing the result;
the tree digest covers xattrs; every run and event records the host's Yama ptrace_scope.
Check round 8 added: a json-field metric under ${EVIDENCE_DIR} counts only when the runner's
token-closed block attests the file's sha256 (`EVIDENCE: <path> <sha256>`), so a process that
tested code left in the jail cannot swap the file for a forged number; a tree folder the host
cannot list, or an entry it cannot stat, is refused before the jail starts, so a host socket
hidden in it is never reached.
Check round 9 added: a line inside the block that holds a marker token and is longer than
markers._MAX_MARKER_LINE is a quick markers-conflict, and the TEST line reader is linear, so one
long line from tested code cannot stall the host's parse; an attested json-field file that the
decoder cannot read (nesting too deep, an integer too large for a float) is an unreadable repeat
with one recorded event, never a raise out of run_chain.
Check round 11 added: every TEST, METRIC and EVIDENCE line inside the block must end with the
run's token, so a tested child that shares the runner's stdout or stderr cannot add a counted
line for a test the runner does not report, or a METRIC line before it fails; the host's tree
digest reads a bounded number of bytes and entries, so a stage that leaves a sparse file of a
petabyte in its tree records its event at once.

Each control has a positive and a negative case. Tests that run a command need bubblewrap and skip
without it; the pure parts (chain order, markers, medians, judged_copy, locks) always run.
"""
from __future__ import annotations

import contextlib
import dataclasses
import errno
import hashlib
import json
import os
import re
import shutil
import signal
import socket
import stat
import subprocess
import sys
import threading
import time

import pytest

from alpaca import db, runbook
from alpaca.gates import verdict as vc
from alpaca.research import events, gates, jail, locks, markers, paths
from alpaca.research.errors import GateInfraError, LockTimeout, MarkerError
from alpaca.research.types import (Contract, FailToPassSpec, GateStage, Metric, PassToPass,
                                   PassToPassSpec, RepairItem, WorktreeInfo)

RUN = "rr-001"
JAIL = jail.available()[0]
needs_jail = pytest.mark.skipif(not JAIL, reason="bubblewrap is not available on this host")

BEGIN, END = "ALPACA-RESULTS-BEGIN", "ALPACA-RESULTS-END"
# a frozen runner reads its run's results token from fd 3 before it runs anything it tests, and
# closes its block with it (markers._with_token)
RD = "read -r T <&3; exec 3<&-; "
ENDT = END + " $T"

CONTRACT_DEFAULTS = dict(
    runbook="pack/rb.yaml", runbook_id="rb", runbook_sha256="0" * 64, pack=None, task=None,
    ref="contract:pack/rb.yaml#-", mode="improve", repo_path=".", repo_ref="HEAD",
    repo_commit="0" * 40, stages=(), knobs={}, metrics=(), targets=(), accept_rule="champion",
    after_targets=None, editable=("src/",), protected=("pack/",), hidden=(), tests=(),
    read_paths=(), read_real=(), keep=(), outputs=(), tools=(), tools_real=(), shared=(),
    gates=(), slots=2, rounds=1, budget_usd=1.0, budget_tokens=1, budget_wall_s=None,
    disk_gib=1.0, proposal=(), categories=(), lessons_file=None, note_chars=300, noop_rounds=0,
    controls_random=False, controls_mutate=None, controls_syntax=None, controls_seed=0,
    holdout_stages=(), holdout_paths=(), repair=(), pass_to_pass=None, repair_prepare=None,
    coupled=(), agents=(), infra_retries=2, verifier_timeout_s=1800,
    verifier_rerun_timeout_s=1200, audit_hints=None, audit_assert_tokens=(),
    audit_build_files=(), audit_steering=(), egress_extra=(), block={}, override_diff=(),
    grants=(), reproducers={}, pack_sha256="", sha256="c" * 64, files_sha256="f" * 64)


def make_contract(**over) -> Contract:
    fields = dict(CONTRACT_DEFAULTS)
    fields.update(over)
    return Contract(**fields)


def stage(ident, run, *checks, timeout=30, **more):
    body = {"id": ident, "run": run, "timeout": timeout,
            "checks": list(checks) or [{"id": ident + "-exit", "type": "exit-code"}]}
    body.update(more)
    return body


def write(path, text, mode=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    if mode is not None:
        os.chmod(path, mode)


class Env:
    """A tmp project with a record, the frozen contract folder and attempt trees."""

    def __init__(self, root):
        self.root = root
        self.conn = db.connect(root)
        self.files = os.path.join(paths.contract_dir(root, RUN), "files")
        os.makedirs(self.files, exist_ok=True)

    def tree(self, task, files=None) -> WorktreeInfo:
        path = paths.worktree_dir(self.root, RUN, task)
        os.makedirs(os.path.join(path, "pack"), exist_ok=True)
        for rel, text in (files or {"src/app.txt": "hello\n"}).items():
            write(os.path.join(path, rel), text)
        return WorktreeInfo(path=path, label=task, commit="0" * 40, admin_dir=None)

    def freeze(self, rel, text, mode=0o644):
        write(os.path.join(self.files, rel), text, mode)

    def gate_events(self, task=None):
        out = []
        for ev in db.events(self.conn, kind=events.GATE, limit=10 ** 9):
            if task is None or ev["ref"] == task:
                out.append(ev)
        return out


@pytest.fixture
def env(project):
    return Env(project)


# ------------------------------------------------------------------------------ chain order
def test_chain_orders_gates_then_metrics_then_repair_items():
    metric = Metric(id="speed", command="true", parser="markers", unit="s", direction="lower",
                    kind="deterministic", repeats=1, min_ok=1, margin=0, timeout_s=10)
    improve = make_contract(gates=(GateStage("build", 10), GateStage("unit", 10)),
                            metrics=(metric,))
    assert gates.chain(improve) == ("build", "unit", "metric:speed")
    case = RepairItem(index=0, case_id="stable", bug_report="bugs/a.md",
                      fail_to_pass=FailToPassSpec(origin="store"))
    repair = make_contract(mode="repair", gates=(GateStage("build", 10),), repair=(case,),
                           pass_to_pass=PassToPassSpec("sh t.sh", 1, 60))
    assert gates.chain(repair) == ("build",)
    assert gates.chain(repair, repair_case="stable") == ("build", "fail-to-pass:stable",
                                                        "pass-to-pass")
    # negative: a case the contract does not declare is refused
    with pytest.raises(ValueError):
        gates.chain(repair, repair_case="other")


# ------------------------------------------------------------------------------ markers
def test_parse_tests_reads_only_between_the_markers():
    log = "\n".join(["TEST: outside ... PASS", BEGIN, "TEST: a ... PASS", "noise",
                     "TEST: b c ... FAIL", "TEST: a ... PASS", "TEST: d ... SKIP", END,
                     "TEST: after ... FAIL"])
    report = markers.parse_tests(log)
    assert report.tests == {"a": "PASS", "b c": "FAIL", "d": "SKIP"}
    assert report.begin and report.end


@pytest.mark.parametrize("log, word", [
    ("TEST: a ... PASS\n" + END, "markers-missing"),
    (BEGIN + "\nTEST: a ... PASS\n", "markers-missing"),
    ("TEST: a ... PASS\n", "markers-missing"),
    (BEGIN + "\nTEST: a ... PASS\nTEST: a ... FAIL\n" + END, "markers-conflict"),
    # exactly one block: a nested BEGIN, a second block, a stray END after the block or before it
    (BEGIN + "\nTEST: a ... FAIL\n" + BEGIN + "\nTEST: a ... PASS\n" + END + "\n" + END,
     "markers-conflict"),
    (BEGIN + "\nTEST: a ... PASS\n" + END + "\n" + BEGIN + "\nTEST: b ... PASS\n" + END,
     "markers-conflict"),
    (BEGIN + "\nTEST: a ... PASS\n" + END + "\nTEST: a ... FAIL\n" + END, "markers-conflict"),
    (END + "\n" + BEGIN + "\nTEST: a ... PASS\n" + END, "markers-conflict"),
])
def test_parse_tests_refuses_a_log_without_markers_or_with_a_conflict(log, word):
    with pytest.raises(MarkerError) as exc:
        markers.parse_tests(log)
    assert exc.value.args[0] == word


def test_parse_metric_reads_its_id_and_refuses_what_it_cannot_trust():
    log = "\n".join(["METRIC: speed 99", BEGIN, "METRIC: other 1", "METRIC: speed 12.5",
                     "METRIC: speed 12.5", END])
    assert markers.parse_metric(log, "speed") == 12.5
    assert markers.parse_metric(log, "other") == 1.0
    for bad, word in ((log.replace(BEGIN, "begin"), "markers-missing"),
                      (BEGIN + "\nMETRIC: other 1\n" + END, "markers-missing"),
                      (BEGIN + "\nMETRIC: speed nan\n" + END, "markers-conflict"),
                      (BEGIN + "\nMETRIC: speed 1\nMETRIC: speed 2\n" + END, "markers-conflict"),
                      # a forged block inside the real one, and a forged END that cuts it off
                      (BEGIN + "\n" + BEGIN + "\nMETRIC: speed 1\n" + END + "\nMETRIC: speed 500\n"
                       + END, "markers-conflict"),
                      (BEGIN + "\nMETRIC: speed 1\n" + END + "\nMETRIC: speed 500\n" + END,
                       "markers-conflict")):
        with pytest.raises(MarkerError) as exc:
            markers.parse_metric(bad, "speed")
        assert exc.value.args[0] == word


@pytest.mark.parametrize("inside", [
    # an unterminated prefix hides the harness's own line (check round 3, a2b and a2c)
    ["TEST: t ... PASS", "XTEST: t ... FAIL"],
    ["METRIC: speed 1", "XMETRIC: speed 500"],
    # a token that is not at the start, or twice on one line
    ["TEST: t ... PASS", "noise TEST: t ... FAIL"],
    ["TEST: t ... PASSTEST: t ... FAIL"],
    ["METRIC: speed 1 METRIC: speed 500"],
    ["TEST: t ... PASS METRIC: speed 1"],
    # a marker line that does not match its pattern
    ["TEST: t ... PASS", "TEST: t ... FAIL x"],
    ["METRIC: speed 500 x"],
    # a prefix on the harness's own END or BEGIN line inside the block
    ["TEST: t ... PASS", "X" + END],
    ["X" + BEGIN],
    # a line break other than a newline: \r, vertical tab, form feed, \x1c to \x1e, \x85,
    # U+2028 and U+2029, each of which str.splitlines() would split on
    ["METRIC: speed 1\r500"], ["TEST: t ... PASS\rnoise"], ["x\x0bTEST: t ... PASS"],
    ["x\x0cy"], ["x\x1cy"], ["x\x1dy"], ["x\x1ey"], ["x\x85y"], ["x\u2028y"], ["x\u2029y"],
])
def test_a_line_inside_the_block_that_bends_a_marker_is_a_conflict(inside):
    log = "\n".join([BEGIN] + inside + ["TEST: t ... FAIL", "METRIC: speed 500", END])
    for parse in (markers.parse_tests, lambda text: markers.parse_metric(text, "speed")):
        with pytest.raises(MarkerError) as exc:
            parse(log)
        assert exc.value.args[0] == "markers-conflict", (inside, exc.value.args)


def test_a_prefixed_marker_after_the_block_is_a_conflict_and_honest_logs_still_parse():
    # a forged block closed early, then the harness's own END hidden behind a prefix
    for tail in ("X" + END, "X" + BEGIN, "noise " + END):
        log = "\n".join([BEGIN, "TEST: t ... PASS", END, "TEST: t ... FAIL", tail])
        with pytest.raises(MarkerError) as exc:
            markers.parse_tests(log)
        assert exc.value.args[0] == "markers-conflict", tail
    # negative: CRLF line ends, indented lines, noise without a token, a set -x trace before
    # the block and TEST lines after it are all read as before
    log = "\r\n".join(["+ echo " + BEGIN, "progress\r50%\r100%", BEGIN, "  TEST: a b ... PASS",
                       "building\tmodule", "\tMETRIC: speed 12.5", "TEST: c ... SKIP", END,
                       "TEST: after ... FAIL", ""])
    assert markers.parse_tests(log).tests == {"a b": "PASS", "c": "SKIP"}
    assert markers.parse_metric(log, "speed") == 12.5


def test_a_report_is_bounded_in_names_and_name_length():
    def log(lines):
        return "\n".join([BEGIN] + lines + [END])
    many = ["TEST: t%d ... PASS" % i for i in range(markers._MAX_RESULTS)]
    assert len(markers.parse_tests(log(many)).tests) == markers._MAX_RESULTS
    with pytest.raises(MarkerError) as exc:
        markers.parse_tests(log(many + ["TEST: one-more ... PASS"]))
    assert exc.value.args == ("markers-conflict",
                              "too many results: more than %d test names" % markers._MAX_RESULTS)
    name = "n" * markers._MAX_NAME
    assert markers.parse_tests(log(["TEST: %s ... PASS" % name])).tests == {name: "PASS"}
    with pytest.raises(MarkerError) as exc:
        markers.parse_tests(log(["TEST: %sx ... PASS" % name]))
    assert exc.value.args[0] == "markers-conflict" and "longer than the limit" in exc.value.args[1]


def test_median_of_an_even_count_follows_the_metric_rule():
    assert gates._median([3.0, 1.0, 2.0], "lower", "worse") == 2.0
    assert gates._median([1.0, 4.0, 2.0, 3.0], "lower", "worse") == 3.0
    assert gates._median([1.0, 4.0, 2.0, 3.0], "higher", "worse") == 2.0
    assert gates._median([1.0, 4.0, 2.0, 3.0], "higher", "middle") == 2.5
    assert gates._median([], "lower", "worse") is None


def test_input_fingerprint_changes_with_every_part():
    base = dict(tree="t", contract_sha256="c", files_sha256="f", tools_sha256="x", stage="s")
    first = gates.input_fingerprint(**base)
    assert first == gates.input_fingerprint(**base)
    for key in base:
        changed = dict(base, **{key: base[key] + "2"})
        assert gates.input_fingerprint(**changed) != first, key


# ------------------------------------------------------------------------------ judged copy
def test_judged_copy_copies_bytes_and_returns_hashes(tmp_path):
    src = tmp_path / "evidence"
    (src / "sub").mkdir(parents=True)
    (src / "a.json").write_text('{"x": 1}')
    (src / "sub" / "b.txt").write_text("b")
    dest = tmp_path / "judged" / "attempt-1"
    got = gates.judged_copy(str(src), str(dest))
    assert set(got) == {"a.json", "sub/b.txt"}
    assert (dest / "sub" / "b.txt").read_text() == "b"
    assert oct(os.stat(dest).st_mode & 0o777) == oct(0o700)
    # negative: the destination must be new
    with pytest.raises(ValueError):
        gates.judged_copy(str(src), str(dest))


def test_judged_copy_refuses_links_and_special_files_and_leaves_nothing(tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("SECRET-MARKER-7f3a")
    src = tmp_path / "evidence"
    (src / "one" / "two").mkdir(parents=True)
    (src / "ok.txt").write_text("fine")
    os.symlink(str(secret), str(src / "one" / "two" / "leak.txt"))
    dest = tmp_path / "judged"
    with pytest.raises(GateInfraError) as exc:
        gates.judged_copy(str(src), str(dest))
    assert exc.value.args[0] == "GATE-EVIDENCE-LINK"
    assert exc.value.args[1] == "one/two/leak.txt: symlink"
    assert not dest.exists()
    os.unlink(str(src / "one" / "two" / "leak.txt"))
    os.mkfifo(str(src / "one" / "pipe"))
    with pytest.raises(GateInfraError) as exc:
        gates.judged_copy(str(src), str(dest))
    assert exc.value.args[1] == "one/pipe: special file"
    assert not dest.exists()
    # a linked evidence folder itself is refused too
    os.unlink(str(src / "one" / "pipe"))
    link = tmp_path / "linked"
    os.symlink(str(src), str(link))
    with pytest.raises(GateInfraError):
        gates.judged_copy(str(link), str(tmp_path / "judged2"))
    assert gates.judged_copy(str(src), str(dest)) == {
        "ok.txt": hashlib.sha256(b"fine").hexdigest()}


# ------------------------------------------------------------------------------ locks
def test_machine_lock_counts_its_slots_and_times_out():
    with locks.machine_lock("demo", capacity=2, timeout_s=1) as one:
        with locks.machine_lock("demo", capacity=2, timeout_s=1) as two:
            assert {one.slot, two.slot} == {0, 1}
            with pytest.raises(LockTimeout):
                with locks.machine_lock("demo", capacity=2, timeout_s=0.3, poll_s=0.05):
                    pass
    # released on exit: taken again at once
    # a held slot raises or costs a poll of 0.5 s; one free take is far quicker even on a loaded host
    with locks.machine_lock("demo", capacity=1, timeout_s=5) as again:
        assert again.slot == 0 and again.waited_s < 0.4
    lock_dir = paths.machine_lock_dir()
    assert os.path.isfile(os.path.join(lock_dir, "research-demo-0.lock"))
    assert lock_dir == os.path.join(os.path.dirname(os.environ["ALPACA_WORKSPACES"]), "locks")
    with pytest.raises(ValueError):
        with locks.machine_lock("Bad Name", timeout_s=1):
            pass


def test_machine_lock_holds_across_processes():
    code = ("import sys, time\n"
            "from alpaca.research import locks\n"
            "with locks.machine_lock('stage-build', timeout_s=5):\n"
            "    print('held', flush=True)\n"
            "    time.sleep(1.2)\n")
    repo = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    child = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True,
                             env=dict(os.environ, PYTHONPATH=repo))
    try:
        assert child.stdout.readline().strip() == "held"
        with pytest.raises(LockTimeout):
            with locks.machine_lock("stage-build", timeout_s=0.3, poll_s=0.05):
                pass
        with locks.machine_lock("stage-build", timeout_s=10, poll_s=0.05) as hold:
            assert hold.waited_s > 0.3
    finally:
        child.wait(timeout=10)


def test_heavy_takes_the_stage_lock_then_a_tier_slot(project):
    # the project has no project.yaml: the smallest tier, one heavy slot
    with locks.heavy("build.fast", project, timeout_s=1) as (stage_hold, heavy_hold):
        assert stage_hold.name == "stage-" + paths.stage_slug("build.fast")
        assert heavy_hold.name == "heavy"
        # the same stage is held
        with pytest.raises(LockTimeout):
            with locks.heavy("build.fast", project, timeout_s=0.2):
                pass
        # another heavy stage waits for the one tier slot
        with pytest.raises(LockTimeout):
            with locks.heavy("unit", project, timeout_s=0.2):
                pass
    with locks.heavy("unit", project, timeout_s=0.2):
        pass


# ------------------------------------------------------------------------------ the chain
@needs_jail
def test_the_first_failing_gate_stops_the_chain_and_is_named(env):
    wt = env.tree("t-001")
    stages = (stage("build", "true"), stage("unit", "exit 1"),
              stage("lint", "echo ran > ${EVIDENCE_DIR}/lint.txt"))
    contract = make_contract(stages=stages, gates=tuple(GateStage(s["id"], 30) for s in stages))
    result = gates.run_chain(env.conn, env.root, run=RUN, task="t-001", contract=contract, wt=wt,
                             mode="improve", session="s")
    assert result.verdict == vc.FAIL
    assert result.first_failure == "unit"
    assert [r.stage for r in result.results] == ["build", "unit"]
    recorded = [(e["data"]["stage"], e["data"]["verdict"]) for e in env.gate_events()]
    assert recorded == [("build", "PASS"), ("unit", "FAIL")]
    # negative: with every stage passing the chain runs to its end and names nothing
    fixed = make_contract(stages=(stages[0], stage("unit", "true"), stages[2]),
                          gates=contract.gates)
    wt2 = env.tree("t-002")
    good = gates.run_chain(env.conn, env.root, run=RUN, task="t-002", contract=fixed, wt=wt2,
                           mode="improve", session="s")
    assert good.verdict == vc.PASS and good.first_failure is None
    assert [r.stage for r in good.results] == ["build", "unit", "lint"]


@needs_jail
def test_each_gate_records_command_rc_log_and_input_fingerprint(env):
    wt = env.tree("t-001")
    contract = make_contract(stages=(stage("build", "echo built ${WORKTREE}; exit 0"),),
                             gates=(GateStage("build", 30),))
    result = gates.run_chain(env.conn, env.root, run=RUN, task="t-001", contract=contract, wt=wt,
                             mode="improve", session="s")
    (ev,) = env.gate_events()
    data = ev["data"]
    assert ev["ref"] == "t-001" and data["run"] == RUN and data["side"] == "attempt"
    assert data["command"] == "echo built %s; exit 0" % wt.path
    assert data["rc"] == 0 and data["verdict"] == "PASS" and data["exit"] == 0
    log = os.path.join(env.root, data["log"])
    assert "built %s" % wt.path in open(log).read()
    assert data["log_sha256"] == hashlib.sha256(open(log, "rb").read()).hexdigest()
    assert len(data["input_sha256"]) == 64
    assert data["input_sha256"] == result.results[0].input_sha256
    assert data["offline"] is True and data["reused"] is False and data["lock_wait_s"] == 0.0
    assert result.results[0].event_id == ev["id"]


@needs_jail
def test_a_gate_result_is_reused_only_while_its_inputs_are_unchanged(env):
    wt = env.tree("t-001")
    env.freeze("pack/tests.sh", "echo frozen\n")
    contract = make_contract(stages=(stage("build", "sh tests.sh; cat ${WORKTREE}/src/app.txt"),),
                             gates=(GateStage("build", 30),))
    kw = dict(run=RUN, task="t-001", contract=contract, wt=wt, mode="improve", session="s")
    first = gates.run_chain(env.conn, env.root, **kw).results[0]
    again = gates.run_chain(env.conn, env.root, **kw).results[0]
    evs = env.gate_events()
    assert again.reused is True and again.input_sha256 == first.input_sha256
    assert evs[-1]["data"]["reused"] is True and evs[-1]["data"]["ran"] is False
    assert evs[-1]["data"]["reused_from"] == first.event_id
    # the fail case: the tree changed, so the gate runs again on the new fingerprint (EC-008)
    write(os.path.join(wt.path, "src", "app.txt"), "changed\n")
    third = gates.run_chain(env.conn, env.root, **kw).results[0]
    assert third.reused is False and third.input_sha256 != first.input_sha256
    assert env.gate_events()[-1]["data"]["ran"] is True
    assert "changed" in open(third.log).read()
    # a frozen contract file changed: not reused either
    env.freeze("pack/tests.sh", "echo frozen again\n")
    fourth = gates.run_chain(env.conn, env.root, **kw).results[0]
    assert fourth.reused is False and fourth.input_sha256 != third.input_sha256
    # another side never takes this side's result, and a verifier's re-run is never reused
    other = gates.run_chain(env.conn, env.root, **dict(kw, side="base")).results[0]
    assert other.reused is False
    verify = gates.run_chain(env.conn, env.root, **dict(kw, side="verify")).results[0]
    verify2 = gates.run_chain(env.conn, env.root, **dict(kw, side="verify")).results[0]
    assert verify.reused is False and verify2.reused is False


@needs_jail
def test_a_failed_result_is_reused_but_a_blocked_one_runs_again(env, monkeypatch):
    wt = env.tree("t-001")
    contract = make_contract(stages=(stage("build", "exit 1"),), gates=(GateStage("build", 30),))
    kw = dict(run=RUN, task="t-001", contract=contract, wt=wt, mode="improve", session="s")
    assert gates.run_chain(env.conn, env.root, **kw).results[0].verdict == vc.FAIL
    assert gates.run_chain(env.conn, env.root, **kw).results[0].reused is True
    wt2 = env.tree("t-002")
    with monkeypatch.context() as m:
        m.setattr(gates.jail, "available", lambda **k: (False, "not installed"))
        blocked = gates.run_chain(env.conn, env.root, **dict(kw, task="t-002", wt=wt2))
    assert blocked.results[0].verdict == vc.BLOCKED
    again = gates.run_chain(env.conn, env.root, **dict(kw, task="t-002", wt=wt2)).results[0]
    assert again.reused is False and again.verdict == vc.FAIL


@needs_jail
def test_stages_run_the_frozen_copy_and_judge_the_judged_copy(env):
    wt = env.tree("t-001", {"src/app.txt": "x\n", "pack/check.sh": "echo TREE-COPY\n"})
    env.freeze("pack/check.sh", "echo FROZEN-COPY\n")
    contract = make_contract(
        stages=(stage("build", "sh check.sh > ${EVIDENCE_DIR}/out.txt",
                      {"id": "frozen", "type": "regex-in-file", "path": "${EVIDENCE_DIR}/out.txt",
                       "pattern": "FROZEN-COPY"}),),
        gates=(GateStage("build", 30),))
    result = gates.run_chain(env.conn, env.root, run=RUN, task="t-001", contract=contract, wt=wt,
                             mode="improve", session="s")
    assert result.verdict == vc.PASS
    data = env.gate_events()[0]["data"]
    assert data["evidence"] == {"out.txt": hashlib.sha256(
        b"FROZEN-COPY\n").hexdigest()}
    judged = os.path.join(env.root, data["judged"])
    assert open(os.path.join(judged, "out.txt")).read() == "FROZEN-COPY\n"
    # the attempt's own copy in the tree is untouched on the host
    assert open(os.path.join(wt.path, "pack", "check.sh")).read() == "echo TREE-COPY\n"


@needs_jail
def test_a_planted_evidence_link_blocks_the_stage_and_the_metric_and_leaks_nothing(env):
    marker = "LEAK-MARKER-%d" % os.getpid()
    contract_json = os.path.join(paths.contract_dir(env.root, RUN), "contract.json")
    write(contract_json, '{"secret": "%s", "failed": 0}' % marker)
    other = os.path.join(paths.attempt_dir(env.root, RUN, "t-009"), "gates", "02-metric",
                         "evidence", "metrics.json")
    write(other, '{"value": 1, "note": "%s"}' % marker)
    wt = env.tree("t-001")
    plant = stage("unit", "ln -s %s ${EVIDENCE_DIR}/x.json" % contract_json,
                  {"id": "json", "type": "json-field", "path": "${EVIDENCE_DIR}/x.json",
                   "field": "failed", "op": "==", "value": 0},
                  {"id": "absent", "type": "regex-in-file", "path": "${EVIDENCE_DIR}/x.json",
                   "pattern": "secret", "absent": True})
    contract = make_contract(stages=(plant,), gates=(GateStage("unit", 30),))
    result = gates.run_chain(env.conn, env.root, run=RUN, task="t-001", contract=contract, wt=wt,
                             mode="improve", session="s")
    assert result.verdict == vc.BLOCKED and result.first_failure == "unit"
    data = env.gate_events()[-1]["data"]
    assert data["code"] == "GATE-EVIDENCE-LINK" and data["verdict"] == "BLOCKED"
    assert data["reason"] == "x.json: symlink"
    metric = Metric(id="value", command="ln -s %s ${EVIDENCE_DIR}/m.json" % other,
                    parser="json-field", path="${EVIDENCE_DIR}/m.json", field="value", unit="u",
                    direction="lower", kind="deterministic", repeats=1, min_ok=1, margin=0,
                    timeout_s=30)
    mc = make_contract(metrics=(metric,))
    wt2 = env.tree("t-002")
    values = gates.measure(env.conn, env.root, run=RUN, task="t-002", contract=mc, wt=wt2,
                           session="s")
    assert values["value"].n_ok == 0 and values["value"].median is None
    mdata = env.gate_events("t-002")[-1]["data"]
    assert mdata["verdict"] == "BLOCKED" and mdata["code"] == "GATE-EVIDENCE-LINK"
    # no byte of either target in the record, the gate logs or any judged folder
    rows = env.conn.execute("SELECT data FROM events").fetchall()
    assert not any(marker in (r["data"] or "") for r in rows)
    for top in (paths.attempt_dir(env.root, RUN, t) for t in ("t-001", "t-002")):
        for dirpath, _dirs, names in os.walk(top):
            for name in names:
                full = os.path.join(dirpath, name)
                if os.path.islink(full):
                    continue
                assert marker not in open(full, errors="replace").read(), full
        for dirpath, dirs, _names in os.walk(top):
            if os.path.basename(dirpath) == "judged":
                assert os.listdir(dirpath) == []


@needs_jail
def test_a_retry_moves_its_knob_only_for_its_own_stage(env):
    wt = env.tree("t-001")
    flaky = stage("flaky", "test ${LEVEL} -ge 2", retry={"max_attempts": 3,
                                                         "move": {"knob": "LEVEL", "by": 1}})
    after = stage("after", "echo level=${LEVEL} > ${EVIDENCE_DIR}/level.txt")
    contract = make_contract(stages=(flaky, after), knobs={"LEVEL": {"default": 1, "max": 5}},
                             gates=(GateStage("flaky", 30), GateStage("after", 30)))
    result = gates.run_chain(env.conn, env.root, run=RUN, task="t-001", contract=contract, wt=wt,
                             mode="improve", session="s")
    assert result.verdict == vc.PASS
    first, second = env.gate_events()
    assert first["data"]["attempts"] == 2
    assert [r["knobs"]["LEVEL"] for r in first["data"]["repeats"]] == [1, 2]
    judged = os.path.join(env.root, second["data"]["judged"], "level.txt")
    assert open(judged).read() == "level=1\n"
    # negative: without a retry rule the same stage fails at once
    once = make_contract(stages=(stage("flaky", "test ${LEVEL} -ge 2"), after),
                         knobs={"LEVEL": {"default": 1}}, gates=contract.gates)
    wt2 = env.tree("t-002")
    failed = gates.run_chain(env.conn, env.root, run=RUN, task="t-002", contract=once, wt=wt2,
                             mode="improve", session="s")
    assert failed.first_failure == "flaky" and failed.verdict == vc.FAIL


@needs_jail
def test_a_timeout_is_its_own_verdict(env):
    wt = env.tree("t-001")
    contract = make_contract(stages=(stage("slow", "sleep 20", timeout=1),),
                             gates=(GateStage("slow", 1),))
    start = time.monotonic()
    result = gates.run_chain(env.conn, env.root, run=RUN, task="t-001", contract=contract, wt=wt,
                             mode="improve", session="s")
    assert time.monotonic() - start < 15
    assert result.verdict == vc.BLOCKED and result.results[0].timed_out is True
    assert env.gate_events()[0]["data"]["timed_out"] is True


# ------------------------------------------------------------------------------ offline
@needs_jail
def test_the_eval_runs_without_network_and_the_record_says_so(env):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    try:
        probe = ("python3 -c \"import socket\ntry:\n socket.create_connection(('127.0.0.1', %d),"
                 " 2)\n print('reached')\nexcept OSError:\n print('no-network')\" > "
                 "${EVIDENCE_DIR}/net.txt" % port)
        wt = env.tree("t-001")
        contract = make_contract(stages=(stage("net", probe),), gates=(GateStage("net", 30),))
        result = gates.run_chain(env.conn, env.root, run=RUN, task="t-001", contract=contract,
                                 wt=wt, mode="improve", session="s")
        assert result.verdict == vc.PASS
        data = env.gate_events()[0]["data"]
        assert data["offline"] is True and result.results[0].offline is True
        assert open(os.path.join(env.root, data["judged"], "net.txt")).read() == "no-network\n"
        # the same probe on the host does reach the listener, so the jail made the difference
        host = subprocess.run([sys.executable, "-c",
                               "import socket; socket.create_connection(('127.0.0.1', %d), 2); "
                               "print('reached')" % port], capture_output=True, text=True)
        assert host.stdout.strip() == "reached"
    finally:
        listener.close()


def test_without_bubblewrap_nothing_runs_and_the_record_says_so(env, monkeypatch):
    monkeypatch.setattr(gates.jail, "available", lambda **k: (False, "bwrap not found"))
    wt = env.tree("t-001")
    contract = make_contract(stages=(stage("build", "touch ${WORKTREE}/ran"),),
                             gates=(GateStage("build", 30),))
    result = gates.run_chain(env.conn, env.root, run=RUN, task="t-001", contract=contract, wt=wt,
                             mode="improve", session="s")
    assert result.verdict == vc.BLOCKED and result.first_failure == "build"
    assert not os.path.exists(os.path.join(wt.path, "ran"))
    data = env.gate_events()[0]["data"]
    assert data["offline"] is None and data["ran"] is False
    assert "bubblewrap is not available" in data["reason"]


@needs_jail
def test_a_jail_that_does_not_start_blocks_the_stage(env, monkeypatch):
    real = gates.jail.argv
    monkeypatch.setattr(gates.jail, "argv", lambda spec: real(spec) + ["--chdir", "/no/such"])
    wt = env.tree("t-001")
    contract = make_contract(stages=(stage("build", "touch ${WORKTREE}/ran"),),
                             gates=(GateStage("build", 30),))
    result = gates.run_chain(env.conn, env.root, run=RUN, task="t-001", contract=contract, wt=wt,
                             mode="improve", session="s")
    assert result.verdict == vc.BLOCKED
    assert not os.path.exists(os.path.join(wt.path, "ran"))
    data = env.gate_events()[0]["data"]
    assert data["offline"] is None and "did not start" in data["reason"]


# ------------------------------------------------------------------------------ heavy gates
def _times(env, task):
    data = env.gate_events(task)[-1]["data"]
    text = open(os.path.join(env.root, data["judged"], "times.txt")).read().split()
    return float(text[0]), float(text[1]), data["lock_wait_s"]


def _two_slots(env, heavy):
    cmd = ("python3 -c \"import time; print(time.time())\" > ${EVIDENCE_DIR}/times.txt; "
           "sleep 1.5; python3 -c \"import time; print(time.time())\" >> "
           "${EVIDENCE_DIR}/times.txt")
    contract = make_contract(stages=(stage("bench", cmd),),
                             gates=(GateStage("bench", 60, heavy=heavy, lock_timeout_s=60),))
    trees = {t: env.tree(t) for t in ("t-001", "t-002")}
    errors = []

    def slot(task):
        conn = db.connect(env.root)
        try:
            gates.run_chain(conn, env.root, run=RUN, task=task, contract=contract,
                            wt=trees[task], mode="improve", session="s")
        except Exception as exc:          # surfaced below
            errors.append(exc)
        finally:
            conn.close()
    threads = [threading.Thread(target=slot, args=(t,)) for t in trees]
    for th in threads:
        th.start()
    for th in threads:
        th.join(timeout=120)
    assert not errors, errors
    return _times(env, "t-001"), _times(env, "t-002")


@needs_jail
def test_two_slots_never_run_the_same_heavy_gate_at_once(env, monkeypatch):
    # tier 2: two heavy slots, so only the stage lock keeps the two runs apart
    from alpaca.resilience import budget
    monkeypatch.setattr(budget, "concurrency_limit", lambda root=None: 2)
    (s1, e1, w1), (s2, e2, w2) = _two_slots(env, heavy=True)
    assert e1 <= s2 or e2 <= s1, "the heavy runs overlapped"
    assert max(w1, w2) > 0.5


@needs_jail
def test_light_gates_do_overlap_so_the_heavy_check_can_fail(env):
    (s1, e1, _w1), (s2, e2, _w2) = _two_slots(env, heavy=False)
    assert s2 < e1 and s1 < e2


@needs_jail
def test_a_heavy_gate_past_its_lock_timeout_is_blocked(env):
    wt = env.tree("t-001")
    contract = make_contract(stages=(stage("bench", "touch ${WORKTREE}/ran"),),
                             gates=(GateStage("bench", 30, heavy=True, lock_timeout_s=1),))
    with locks.machine_lock("stage-bench", timeout_s=1):
        result = gates.run_chain(env.conn, env.root, run=RUN, task="t-001", contract=contract,
                                 wt=wt, mode="improve", session="s")
    assert result.verdict == vc.BLOCKED
    data = env.gate_events()[0]["data"]
    assert data["code"] == "GATE-LOCK-TIMEOUT" and data["lock_wait_s"] == 1.0
    assert not os.path.exists(os.path.join(wt.path, "ran"))


# ------------------------------------------------------------------------------ metrics
@needs_jail
def test_a_metric_run_records_every_repeat_and_the_median(env):
    wt = env.tree("t-001")
    metric = Metric(id="speed", command=RD + "echo %s; echo METRIC: speed ${SEED} $T; echo %s"
                    % (BEGIN, ENDT), parser="markers", unit="ms", direction="lower",
                    kind="noisy", repeats=4, min_ok=3, margin=10, timeout_s=30,
                    seeds=(40, 10, 30, 20), median="worse")
    contract = make_contract(metrics=(metric,))
    result = gates.run_chain(env.conn, env.root, run=RUN, task="t-001", contract=contract, wt=wt,
                             mode="improve", session="s")
    value = result.metrics["speed"]
    assert value.repeats == (40.0, 10.0, 30.0, 20.0) and value.n_ok == 4
    assert value.median == 30.0 and value.spread == 30.0 and value.untested is True
    data = env.gate_events()[0]["data"]
    assert data["stage"] == "metric:speed" and data["median"] == 30.0
    assert [r["value"] for r in data["repeats"]] == [40.0, 10.0, 30.0, 20.0]
    assert [r["seed"] for r in data["repeats"]] == [40, 10, 30, 20]
    assert data["metric"]["values"] == [40.0, 10.0, 30.0, 20.0]
    assert all(r["log"] and r["log_sha256"] for r in data["repeats"])
    # "middle" takes the mean of the two middle values
    mid = make_contract(metrics=(dataclasses.replace(metric, median="middle"),))
    wt2 = env.tree("t-002")
    got = gates.measure(env.conn, env.root, run=RUN, task="t-002", contract=mid, wt=wt2,
                        session="s")
    assert got["speed"].median == 25.0


@needs_jail
def test_a_metric_without_markers_gets_no_score(env):
    wt = env.tree("t-001")
    metric = Metric(id="speed", command="echo METRIC: speed 5", parser="markers", unit="ms",
                    direction="lower", kind="deterministic", repeats=1, min_ok=1, margin=0,
                    timeout_s=30)
    contract = make_contract(metrics=(metric,))
    result = gates.run_chain(env.conn, env.root, run=RUN, task="t-001", contract=contract, wt=wt,
                             mode="improve", session="s")
    assert result.verdict == vc.FAIL and result.first_failure == "metric:speed"
    assert result.metrics["speed"].median is None and result.metrics["speed"].n_ok == 0
    data = env.gate_events()[0]["data"]
    assert data["code"] == "GATE-MARKERS"
    assert data["repeats"][0]["reason"].startswith("markers-missing")


def _attesting(name, text):
    """A shell runner that writes text and a newline to ${EVIDENCE_DIR}/<name> and attests those
    bytes in its token-closed block, hashing what it wrote rather than the file."""
    return (RD + "echo %s; j=%s; printf '%%s\\n' \"$j\" > ${EVIDENCE_DIR}/%s; "
            "echo \"EVIDENCE: %s $(printf '%%s\\n' \"$j\" | sha256sum | cut -c1-64) $T\"; "
            "echo %s" % (BEGIN, text, name, name, ENDT))


@needs_jail
def test_a_json_field_metric_reads_the_judged_copy(env):
    wt = env.tree("t-001")
    metric = Metric(id="wall",
                    command=_attesting("w-${REPEAT}.json", "'{\"wall\": ${SEED}}'"),
                    parser="json-field", path="${EVIDENCE_DIR}/w-${REPEAT}.json", field="wall",
                    unit="ms", direction="lower", kind="noisy", repeats=3, min_ok=2, margin=5,
                    timeout_s=30, seeds=(7, 5, 6))
    got = gates.measure(env.conn, env.root, run=RUN, task="t-001",
                        contract=make_contract(metrics=(metric,)), wt=wt, session="s")
    assert got["wall"].repeats == (7.0, 5.0, 6.0) and got["wall"].median == 6.0
    data = env.gate_events()[0]["data"]
    assert data["repeats"][1]["evidence"] == {"w-2.json": hashlib.sha256(
        b'{"wall": 5}\n').hexdigest()}


# ------------------------------------------------------------------------------ repair items
def _repair(env, *, tests=("t_bug",), origin="store", ptp_min=2, repeats=1):
    case = RepairItem(index=0, case_id="bug", bug_report="bugs/bug.md",
                      fail_to_pass=FailToPassSpec(origin=origin, tests=tests, timeout_s=30,
                                                  repeats=repeats))
    spec = PassToPassSpec(command="sh ${CONTRACT_DIR}/pack/suite.sh", min_tests=ptp_min,
                          timeout_s=30)
    contract = make_contract(mode="repair", repair=(case,), pass_to_pass=spec,
                             stages=(stage("build", "true"),), gates=(GateStage("build", 30),))
    # the reproducer: t_bug fails while src/app.txt says bug; t_side reports what the tree says
    rep = (paths.hidden_dir(env.root, RUN, "bug") if origin == "store"
           else paths.reproducer_dir(env.root, RUN, "bug"))
    write(os.path.join(rep, "run.sh"),
          RD + "echo %s\n"
          "if grep -q fixed ${WORKTREE}/src/app.txt; then echo \"TEST: t_bug ... PASS $T\";\n"
          "else echo \"TEST: t_bug ... FAIL $T\"; fi\n"
          "echo \"TEST: t_always ... PASS $T\"\n"
          "echo %s\n" % (BEGIN, ENDT))
    events.emit(env.conn, events.REPRODUCER, run=RUN, ref="t-090", op=None, session="s",
                code=vc.PASS, data={"case": "bug", "proved": True, "tests": list(tests),
                                    "files": {"run.sh": gates._sha256_file(
                                        os.path.join(rep, "run.sh"))}})
    env.freeze("pack/suite.sh",
               RD + "echo %s\necho \"TEST: s1 ... PASS $T\"\n"
               "if grep -q broken ${WORKTREE}/src/app.txt; then echo \"TEST: s2 ... FAIL $T\";\n"
               "else echo \"TEST: s2 ... PASS $T\"; fi\necho %s\n" % (BEGIN, ENDT))
    return contract, rep


@needs_jail
def test_fail_to_pass_needs_a_base_fail_and_an_attempt_pass(env):
    contract, _rep = _repair(env)
    base = env.tree("t-001-base", {"src/app.txt": "bug\n"})
    fixed = env.tree("t-001", {"src/app.txt": "fixed\n"})
    got = gates.fail_to_pass(env.conn, env.root, run=RUN, task="t-001", contract=contract,
                             case="bug", base=base, attempt=fixed, session="s")
    assert got.ok is True and got.weak == ()
    assert got.base == {"t_bug": "FAIL"} and got.attempt == {"t_bug": "PASS"}
    sides = [(e["data"]["side"], e["data"]["verdict"]) for e in env.gate_events("t-001")]
    assert sides == [("base", "PASS"), ("attempt", "PASS")]
    # negative: an attempt that does not fix it
    still = env.tree("t-002", {"src/app.txt": "bug\n"})
    nope = gates.fail_to_pass(env.conn, env.root, run=RUN, task="t-002", contract=contract,
                              case="bug", base=base, attempt=still, session="s")
    assert nope.ok is False and nope.attempt == {"t_bug": "FAIL"}
    assert env.gate_events("t-002")[-1]["data"]["verdict"] == "FAIL"


@needs_jail
def test_a_test_that_already_passes_on_the_base_is_rejected_as_weak(env):
    contract, _rep = _repair(env, tests=("t_always",))
    base = env.tree("t-001-base", {"src/app.txt": "bug\n"})
    fixed = env.tree("t-001", {"src/app.txt": "fixed\n"})
    got = gates.fail_to_pass(env.conn, env.root, run=RUN, task="t-001", contract=contract,
                             case="bug", base=base, attempt=fixed, session="s")
    assert got.weak == ("t_always",) and got.ok is False
    base_ev = env.gate_events("t-001")[0]["data"]
    assert base_ev["side"] == "base" and base_ev["verdict"] == "FAIL"
    assert base_ev["weak"] == ["t_always"] and "weak" in base_ev["reason"]
    # in the chain the weak test stops it at the fail-to-pass item
    chain = gates.run_chain(env.conn, env.root, run=RUN, task="t-003", contract=contract,
                            wt=env.tree("t-003", {"src/app.txt": "fixed\n"}), mode="repair",
                            session="s", repair_case="bug", base=base,
                            baseline=PassToPass(baseline=(), base_pass=("s1", "s2"),
                                                attempt_pass=(), lost=(), ok=True))
    assert chain.first_failure == "fail-to-pass:bug" and chain.verdict == vc.FAIL
    assert chain.fail_to_pass.weak == ("t_always",) and chain.pass_to_pass is None


@needs_jail
def test_fail_to_pass_refuses_a_reproducer_that_changed_since_its_proof(env):
    contract, rep = _repair(env, origin="generate")
    base = env.tree("t-001-base", {"src/app.txt": "bug\n"})
    fixed = env.tree("t-001", {"src/app.txt": "fixed\n"})
    assert gates.fail_to_pass(env.conn, env.root, run=RUN, task="t-001", contract=contract,
                              case="bug", base=base, attempt=fixed, session="s").ok is True
    with open(os.path.join(rep, "run.sh"), "a") as fh:
        fh.write("# edited\n")
    got = gates.fail_to_pass(env.conn, env.root, run=RUN, task="t-002", contract=contract,
                             case="bug", base=base, attempt=fixed, session="s")
    assert got.ok is False
    data = env.gate_events("t-002")[-1]["data"]
    assert data["verdict"] == "BLOCKED" and "no longer hashes" in data["reason"]


@needs_jail
def test_a_reproducer_log_without_markers_is_its_own_verdict(env):
    contract, rep = _repair(env)
    write(os.path.join(rep, "run.sh"), "echo 'TEST: t_bug ... FAIL'\n")
    events.emit(env.conn, events.REPRODUCER, run=RUN, ref="t-090", op=None, session="s",
                code=vc.PASS, data={"case": "bug", "proved": True, "tests": ["t_bug"], "files": {
                    "run.sh": gates._sha256_file(os.path.join(rep, "run.sh"))}})
    base = env.tree("t-001-base", {"src/app.txt": "bug\n"})
    fixed = env.tree("t-001", {"src/app.txt": "fixed\n"})
    got = gates.fail_to_pass(env.conn, env.root, run=RUN, task="t-001", contract=contract,
                             case="bug", base=base, attempt=fixed, session="s")
    assert got.ok is False and got.base == {}
    data = env.gate_events("t-001")[-1]["data"]
    assert data["code"] == "GATE-MARKERS" and data["verdict"] == "FAIL"
    assert data["reason"].startswith("markers-missing")


@needs_jail
def test_pass_to_pass_runs_the_frozen_suite_and_names_a_lost_test(env):
    contract, _rep = _repair(env)
    base = env.tree("t-001-base", {"src/app.txt": "bug\n"})
    good = env.tree("t-001", {"src/app.txt": "fixed\n"})
    got = gates.pass_to_pass(env.conn, env.root, run=RUN, task="t-001", contract=contract,
                             base=base, attempt=good, baseline=("s1", "s2"), session="s")
    assert got.ok is True and got.base_pass == ("s1", "s2") and got.lost == ()
    # the attempt breaks s2 and edits its own copy of the suite to hide it: the frozen copy runs
    broken = env.tree("t-002", {"src/app.txt": "broken\n",
                                "pack/suite.sh": RD + "echo %s\necho \"TEST: s1 ... PASS $T\"\n"
                                                 "echo \"TEST: s2 ... PASS $T\"\necho %s\n"
                                                 % (BEGIN, ENDT)})
    lost = gates.pass_to_pass(env.conn, env.root, run=RUN, task="t-002", contract=contract,
                              base=base, attempt=broken, baseline=("s1", "s2"), session="s")
    assert lost.ok is False and lost.lost == ("s2",) and lost.attempt_pass == ("s1",)
    data = env.gate_events("t-002")[-1]["data"]
    assert data["verdict"] == "FAIL" and data["pass_to_pass"]["lost"] == ["s2"]
    # a non-empty baseline under min_tests is BLOCKED before anything runs
    thin = gates.pass_to_pass(env.conn, env.root, run=RUN, task="t-003", contract=contract,
                              base=base, attempt=good, baseline=("s1",), session="s")
    assert thin.ok is False
    assert env.gate_events("t-003")[-1]["data"]["verdict"] == "BLOCKED"
    # base_measure's call: baseline () on the base tree
    measured = gates.pass_to_pass(env.conn, env.root, run=RUN, task="t-004", contract=contract,
                                  base=base, attempt=base, baseline=(), session="s")
    assert measured.ok is True and measured.base_pass == ("s1", "s2")


@needs_jail
def test_a_repair_chain_passes_when_the_fix_holds(env):
    contract, _rep = _repair(env)
    base = env.tree("t-001-base", {"src/app.txt": "bug\n"})
    fixed = env.tree("t-001", {"src/app.txt": "fixed\n"})
    baseline = PassToPass(baseline=(), base_pass=("s1", "s2"), attempt_pass=(), lost=(), ok=True)
    got = gates.run_chain(env.conn, env.root, run=RUN, task="t-001", contract=contract, wt=fixed,
                          mode="repair", session="s", repair_case="bug", base=base,
                          baseline=baseline)
    assert got.verdict == vc.PASS and got.first_failure is None
    assert got.fail_to_pass.ok and got.pass_to_pass.ok
    assert [(r.stage, r.side) for r in got.results] == [
        ("build", "attempt"), ("fail-to-pass:bug", "base"), ("fail-to-pass:bug", "attempt"),
        ("pass-to-pass", "base"), ("pass-to-pass", "attempt")]
    with pytest.raises(ValueError):
        gates.run_chain(env.conn, env.root, run=RUN, task="t-001", contract=contract, wt=fixed,
                        mode="repair", session="s", repair_case="bug")


@needs_jail
def test_run_item_runs_one_tree_and_records_its_side(env):
    contract, rep = _repair(env)
    base = env.tree("t-001-base", {"src/app.txt": "bug\n"})
    got = gates.run_item(env.conn, env.root, run=RUN, task="t-090", contract=contract,
                         item="fail-to-pass:bug", wt=base, side="base", session="s",
                         reproducer_dir=rep)
    assert got.verdict == vc.PASS and got.tests.tests == {"t_bug": "FAIL", "t_always": "PASS"}
    data = env.gate_events("t-090")[-1]["data"]
    assert data["side"] == "base" and data["tests"]["t_bug"] == "FAIL"
    with pytest.raises(ValueError):
        gates.run_item(env.conn, env.root, run=RUN, task="t-090", contract=contract,
                       item="build", wt=base, side="base", session="s")


@needs_jail
def test_run_prepare_records_the_prepare_side_and_writes_the_tree(env):
    contract, _rep = _repair(env)
    prep = stage("prepare", "echo data > ${WORKTREE}/src/data.txt")
    contract = dataclasses.replace(contract, stages=contract.stages + (prep,),
                                   repair_prepare="prepare")
    tree = env.tree("t-090-prep", {"src/app.txt": "bug\n"})
    got = gates.run_prepare(env.conn, env.root, run=RUN, task="t-090", contract=contract,
                            wt=tree, session="s")
    assert got.verdict == vc.PASS and got.side == "prepare"
    assert open(os.path.join(tree.path, "src", "data.txt")).read() == "data\n"
    assert env.gate_events("t-090")[-1]["data"]["side"] == "prepare"


# ------------------------------------------------------------------------------ verifier
def _try_trees(env, slot, tree_files, base_files, task="t-001"):
    """A verifier try folder <verify>/<task>/v1/<slot>/r1/t1/ with its tree/ and base/."""
    top = os.path.join(env.root, ".alpaca", "verify", task, "v1", slot, "r1", "t1")
    out = [top]
    for sub, files in (("tree", tree_files), ("base", base_files)):
        path = os.path.join(top, sub)
        os.makedirs(os.path.join(path, "pack"), exist_ok=True)
        for rel, text in files.items():
            write(os.path.join(path, rel), text)
        out.append(WorktreeInfo(path=path, label=sub, commit="0" * 40, admin_dir=None))
    return tuple(out)


@needs_jail
def test_verify_rerun_runs_one_stage_on_one_tree_or_refuses(env):
    contract, _rep = _repair(env)
    top, fixed, base = _try_trees(env, "s1", {"src/app.txt": "fixed\n"}, {"src/app.txt": "bug\n"})
    trees = {"tree": fixed, "base": base}
    kw = dict(run=RUN, task="t-001", contract=contract, trees=trees, session="s")
    # the repair case comes from the task's record only: with no fail-to-pass of its own yet,
    # the task's chain has no repair item, so the request is refused and nothing runs
    early = gates.verify_rerun(env.conn, env.root, request={"stage": "fail-to-pass:bug",
                                                            "side": "base", "n": 0}, **kw)
    assert early["verdict"] == "BLOCKED" and "no fail-to-pass" in early["refused"]
    assert not os.path.exists(os.path.join(top, "gates"))
    assert gates.fail_to_pass(env.conn, env.root, run=RUN, task="t-001", contract=contract,
                              case="bug", base=env.tree("t-001-base", {"src/app.txt": "bug\n"}),
                              attempt=env.tree("t-001", {"src/app.txt": "fixed\n"}),
                              session="s").ok
    receipt = gates.verify_rerun(env.conn, env.root, request={"stage": "fail-to-pass:bug",
                                                              "side": "base", "n": 1}, **kw)
    assert receipt["verdict"] == "PASS" and receipt["tests"]["t_bug"] == "FAIL"
    assert receipt["log_sha256"] == gates._sha256_file(receipt["log"])
    # the re-run works in its own try folder, never in the attempt's gates folder
    assert receipt["log"].startswith(os.path.join(top, "gates") + os.sep)
    ev = env.gate_events("t-001")[-1]
    assert ev["id"] == receipt["event"] and ev["data"]["side"] == "verify"
    assert ev["data"]["log_sha256"] == receipt["log_sha256"]
    build = gates.verify_rerun(env.conn, env.root, request={"stage": "build", "side": "tree",
                                                            "n": 2}, **kw)
    assert build["verdict"] == "PASS" and build["rc"] == 0
    # refused: nothing runs, one BLOCKED event with the reason
    for bad in ({"stage": "nope", "side": "tree", "n": 3},
                {"stage": "build", "side": "attempt", "n": 4}):
        before = sorted(os.listdir(os.path.join(top, "gates")))
        refused = gates.verify_rerun(env.conn, env.root, request=bad, **kw)
        assert refused["verdict"] == "BLOCKED" and refused["refused"]
        data = env.gate_events("t-001")[-1]["data"]
        assert data["refused"] == refused["refused"] and data["side"] == "verify"
        assert sorted(os.listdir(os.path.join(top, "gates"))) == before


@needs_jail
def test_verifier_reruns_keep_their_own_folder_and_log(env):
    """Two verifier slots re-run the same stage on the same task, and one slot re-runs it on
    its tree and then on its base: each run keeps its own folder and log."""
    contract = make_contract(stages=(stage("unit", "cat ${WORKTREE}/src/app.txt; "
                                                   "grep -q fixed ${WORKTREE}/src/app.txt"),),
                             gates=(GateStage("unit", 30),))
    top1, tree1, base1 = _try_trees(env, "s1", {"src/app.txt": "fixed\n"},
                                    {"src/app.txt": "bug\n"})
    top2, tree2, base2 = _try_trees(env, "s2", {"src/app.txt": "fixed\n"},
                                    {"src/app.txt": "bug\n"})
    kw = dict(run=RUN, task="t-001", contract=contract, session="s")
    one = {"tree": tree1, "base": base1}
    r1 = gates.verify_rerun(env.conn, env.root, request={"stage": "unit", "side": "tree", "n": 1},
                            trees=one, **kw)
    r2 = gates.verify_rerun(env.conn, env.root, request={"stage": "unit", "side": "base", "n": 2},
                            trees=one, **kw)
    r3 = gates.verify_rerun(env.conn, env.root, request={"stage": "unit", "side": "tree", "n": 1},
                            trees={"tree": tree2, "base": base2}, **kw)
    assert (r1["verdict"], r2["verdict"], r3["verdict"]) == ("PASS", "FAIL", "PASS")
    assert r1["log"] != r2["log"]
    # the tree receipt's log still holds the tree run after the base run
    assert gates._sha256_file(r1["log"]) == r1["log_sha256"]
    assert "fixed" in open(r1["log"]).read() and "bug" in open(r2["log"]).read()
    assert r1["log"].startswith(os.path.join(top1, "gates") + os.sep)
    assert r3["log"].startswith(os.path.join(top2, "gates") + os.sep)
    assert not os.path.exists(os.path.join(paths.attempt_dir(env.root, RUN, "t-001"), "gates"))


# ------------------------------------------------------------------------------ plugin parity
PLUGIN = """#!/bin/sh
env | sed -n 's/^\\(ALPACA_[A-Z_]*\\)=.*/\\1/p' | sort | tr '\\n' ',' ; echo
echo "keys: $(env | sed -n 's/^\\(ALPACA_[A-Z_]*\\)=.*/\\1/p' | sort | tr '\\n' ' ')"
echo "secret: ${SECRET_TOKEN:-none}"
exit "$1"
"""


@needs_jail
@pytest.mark.parametrize("code", [0, 1, 2, 3, 9])
def test_plugin_checks_keep_the_run_plugin_keys_and_verdicts(env, monkeypatch, code):
    monkeypatch.setenv("SECRET_TOKEN", "operator-only")
    env.freeze("pack/plug.sh", PLUGIN, 0o755)
    chk = {"id": "plug", "type": "plugin", "script": "plug.sh", "args": [str(code)]}
    frozen = os.path.join(env.files, "pack")
    host_code, host_reason = runbook.evaluate(chk, workdir=frozen, runbook_dir=frozen,
                                              exit_code=0, knobs={"K": 1}, stage="build",
                                              attempt=1, evidence_dir=frozen)
    wt = env.tree("t-001")
    contract = make_contract(stages=(stage("build", "true", chk),), knobs={"K": 1},
                             gates=(GateStage("build", 30),))
    gates.run_chain(env.conn, env.root, run=RUN, task="t-001", contract=contract, wt=wt,
                    mode="improve", session="s")
    data = env.gate_events()[0]["data"]
    jail_code, jail_reason = data["repeats"][0]["checks"]["plug"]
    assert jail_code == vc.name_of(host_code)
    if code in vc.VERDICT_BAND:
        assert host_reason == "secret: operator-only"
        assert jail_reason == "secret: none"
    else:
        assert "outside the verdict band" in host_reason
        assert "outside the verdict band" in jail_reason
    # the keys: the jail's plugin gets every key runbook._run_plugin sets, plus two
    # the check's own run tag is <the stage run's tag>-<k>, and its log <that tag>-<attempt>.log
    log = open(os.path.join(env.root, os.path.dirname(data["log"]), "checks",
                            "plug", "%s-1-1.log" % data["tag"])).read()
    jail_keys = set(log.split("keys: ")[1].split("\n")[0].split())
    host = subprocess.run([os.path.join(frozen, "plug.sh"), "0"], capture_output=True,
                          text=True, env=dict(os.environ, ALPACA_RUNBOOK_DIR="x",
                                              ALPACA_STAGE="x", ALPACA_CHECK="x",
                                              ALPACA_ATTEMPT="x", ALPACA_EVIDENCE_DIR="x",
                                              ALPACA_KNOB_K="1"))
    run_plugin_keys = {"ALPACA_RUNBOOK_DIR", "ALPACA_STAGE", "ALPACA_CHECK", "ALPACA_ATTEMPT",
                       "ALPACA_EVIDENCE_DIR", "ALPACA_KNOB_K"}
    assert run_plugin_keys <= set(host.stdout.split("keys: ")[1].split("\n")[0].split())
    assert jail_keys - run_plugin_keys == {"ALPACA_WORKTREE", "ALPACA_CONTRACT_DIR"}
    assert run_plugin_keys <= jail_keys


def test_the_plugin_verdict_mapping_matches_run_plugin():
    # the pure mapping, without a jail: band exits kept, others and failures BLOCKED
    for rc in (0, 1, 2, 3):
        assert gates._plugin_verdict(script="p", started=True, timed_out=False, rc=rc,
                                     output="a\nlast line\n", timeout=5) == (rc, "last line")
    assert gates._plugin_verdict(script="p", started=True, timed_out=False, rc=7, output="",
                                 timeout=5)[0] == vc.BLOCKED
    assert gates._plugin_verdict(script="p", started=True, timed_out=True, rc=None, output="",
                                 timeout=5) == (vc.BLOCKED, "plugin p timed out after 5s")
    assert gates._plugin_verdict(script="p", started=False, timed_out=False, rc=None,
                                 output="", timeout=5)[0] == vc.BLOCKED
    env = gates._plugin_env(runbook_dir="r", stage="s", check="c", attempt=2, evidence="e",
                            knobs={"N": 3}, worktree="w", contract_dir="d")
    assert env == {"ALPACA_RUNBOOK_DIR": "r", "ALPACA_STAGE": "s", "ALPACA_CHECK": "c",
                   "ALPACA_ATTEMPT": "2", "ALPACA_EVIDENCE_DIR": "e", "ALPACA_WORKTREE": "w",
                   "ALPACA_CONTRACT_DIR": "d", "ALPACA_KNOB_N": "3"}


# ------------------------------------------------------- check round 1: forged reports
def _one_stage(cmd, **gate):
    return make_contract(stages=(stage("unit", cmd),), gates=(GateStage("unit", 30, **gate),))


def _chain(env, contract, wt, task="t-001", **kw):
    return gates.run_chain(env.conn, env.root, run=RUN, task=task, contract=contract, wt=wt,
                           mode=kw.pop("mode", "improve"), session="s", **kw)


def _prove(env, rep, *, proved=True):
    events.emit(env.conn, events.REPRODUCER, run=RUN, ref="t-090", op=None, session="s",
                code=vc.PASS, data={"case": "bug", "proved": proved, "tests": ["t_bug"],
                                    "files": {"run.sh": gates._sha256_file(
                                        os.path.join(rep, "run.sh"))}})


def _hooked_repair(env):
    """A repair contract whose frozen reproducer runs src/hook.sh of the tree inside its block,
    as a real reproducer runs the code it tests."""
    contract, rep = _repair(env)
    write(os.path.join(rep, "run.sh"),
          RD + "echo %s\nsh ${WORKTREE}/src/hook.sh\n"
          "if grep -q fixed ${WORKTREE}/src/app.txt; then echo \"TEST: t_bug ... PASS $T\";\n"
          "else echo \"TEST: t_bug ... FAIL $T\"; fi\necho %s\n" % (BEGIN, ENDT))
    _prove(env, rep)
    base = env.tree("t-001-base", {"src/app.txt": "bug\n", "src/hook.sh": "true\n"})
    return contract, base


@needs_jail
def test_code_under_the_frozen_bench_cannot_forge_a_metric_block(env):
    env.freeze("pack/bench.sh", RD + "echo %s\nsh ${WORKTREE}/src/solver.sh\n"
                                "echo METRIC: speed 500 $T\necho %s\n" % (BEGIN, ENDT))
    metric = Metric(id="speed", command="sh ${CONTRACT_DIR}/pack/bench.sh", parser="markers",
                    unit="ms", direction="lower", kind="deterministic", repeats=1, min_ok=1,
                    margin=0, timeout_s=30)
    contract = make_contract(metrics=(metric,))
    forged = env.tree("t-001", {"src/solver.sh": "echo %s; echo METRIC: speed 1; echo %s\n"
                                % (BEGIN, END)})
    res = _chain(env, contract, forged)
    assert res.verdict == vc.FAIL and res.metrics["speed"].median is None
    data = env.gate_events("t-001")[-1]["data"]
    assert data["code"] == "GATE-MARKERS"
    assert data["repeats"][0]["reason"].startswith("markers-conflict")
    # negative: an honest solver is measured by the bench's own line
    honest = env.tree("t-002", {"src/solver.sh": "true\n"})
    assert _chain(env, contract, honest, task="t-002").metrics["speed"].median == 500.0


def _bench_contract(env, bench):
    env.freeze("pack/bench.sh", bench)
    metric = Metric(id="speed", command="sh ${CONTRACT_DIR}/pack/bench.sh", parser="markers",
                    unit="ms", direction="lower", kind="deterministic", repeats=1, min_ok=1,
                    margin=0, timeout_s=30)
    return make_contract(metrics=(metric,))


@needs_jail
def test_code_under_the_frozen_bench_cannot_hide_its_metric_line_behind_a_prefix(env):
    """Check round 3, a2b: the solver prints a forged line and an unterminated prefix, so the
    bench's own line reads XMETRIC: speed 500."""
    contract = _bench_contract(env, RD + "echo %s\nsh ${WORKTREE}/src/solver.sh\n"
                                    "echo METRIC: speed 500 $T\necho %s\n" % (BEGIN, ENDT))
    forged = env.tree("t-001", {"src/solver.sh": "printf 'METRIC: speed 1\\nX'\n"})
    res = _chain(env, contract, forged)
    data = env.gate_events("t-001")[-1]["data"]
    assert res.verdict == vc.FAIL and res.metrics["speed"].median is None
    assert data["code"] == "GATE-MARKERS"
    assert data["repeats"][0]["reason"].startswith("markers-conflict")
    # negative: a solver that prints whole lines is measured by the bench's own line
    honest = env.tree("t-002", {"src/solver.sh": "echo building\n"})
    assert _chain(env, contract, honest, task="t-002").metrics["speed"].median == 500.0


@needs_jail
def test_a_carriage_return_in_the_log_is_never_read_as_a_line_end(env):
    """Check round 3, a2 with \r: the bench prints `METRIC: speed ` and the solver prints `1\r`
    before the bench's 500. The log keeps the \r (no universal newlines), and the line is
    refused instead of read as speed 1."""
    contract = _bench_contract(env, RD + "echo %s\nprintf 'METRIC: speed '\n"
                                    "sh ${WORKTREE}/src/solver.sh\necho \"500 $T\"\necho %s\n"
                                    % (BEGIN, ENDT))
    forged = env.tree("t-001", {"src/solver.sh": "printf '1\\r'\n"})
    res = _chain(env, contract, forged)
    data = env.gate_events("t-001")[-1]["data"]
    assert res.metrics["speed"].median is None and data["code"] == "GATE-MARKERS"
    assert "line-break" in data["repeats"][0]["reason"]
    assert b"1\r500" in open(os.path.join(env.root, data["log"]), "rb").read()
    # negative: a solver that prints nothing leaves the bench's line whole
    honest = env.tree("t-002", {"src/solver.sh": "true\n"})
    assert _chain(env, contract, honest, task="t-002").metrics["speed"].median == 500.0


@needs_jail
def test_code_under_the_reproducer_cannot_end_its_block_early(env):
    contract, base = _hooked_repair(env)
    bad = env.tree("t-001", {"src/app.txt": "bug\n",
                             "src/hook.sh": "echo 'TEST: t_bug ... PASS'; echo %s\n" % END})
    got = gates.fail_to_pass(env.conn, env.root, run=RUN, task="t-001", contract=contract,
                             case="bug", base=base, attempt=bad, session="s")
    data = env.gate_events("t-001")[-1]["data"]
    assert got.ok is False and got.attempt == {}
    assert data["side"] == "attempt" and data["code"] == "GATE-MARKERS"
    assert data["reason"].startswith("markers-conflict")
    # negative: the real fix passes through the same reproducer
    fixed = env.tree("t-002", {"src/app.txt": "fixed\n", "src/hook.sh": "true\n"})
    assert gates.fail_to_pass(env.conn, env.root, run=RUN, task="t-002", contract=contract,
                              case="bug", base=base, attempt=fixed, session="s").ok is True


@needs_jail
def test_code_under_the_reproducer_cannot_hide_its_fail_line_behind_a_prefix(env):
    """Check round 3, a2c: the hook prints a forged PASS and an unterminated prefix, so the
    reproducer's own line reads XTEST: t_bug ... FAIL on a tree that still says bug."""
    contract, base = _hooked_repair(env)
    bad = env.tree("t-001", {"src/app.txt": "bug\n",
                             "src/hook.sh": "printf 'TEST: t_bug ... PASS\\nX'\n"})
    got = gates.fail_to_pass(env.conn, env.root, run=RUN, task="t-001", contract=contract,
                             case="bug", base=base, attempt=bad, session="s")
    data = env.gate_events("t-001")[-1]["data"]
    assert got.ok is False and got.attempt == {}
    assert data["side"] == "attempt" and data["code"] == "GATE-MARKERS"
    assert data["reason"].startswith("markers-conflict")
    # negative: a hook that prints whole lines, on a tree with the real fix, passes
    fixed = env.tree("t-002", {"src/app.txt": "fixed\n", "src/hook.sh": "echo building\n"})
    assert gates.fail_to_pass(env.conn, env.root, run=RUN, task="t-002", contract=contract,
                              case="bug", base=base, attempt=fixed, session="s").ok is True


@needs_jail
def test_code_that_kills_the_reproducer_after_a_forged_block_gets_no_pass(env):
    contract, base = _hooked_repair(env)
    bad = env.tree("t-001", {"src/app.txt": "bug\n",
                             "src/hook.sh": "echo 'TEST: t_bug ... PASS'; echo %s; kill -9 $PPID\n"
                                            % END})
    got = gates.fail_to_pass(env.conn, env.root, run=RUN, task="t-001", contract=contract,
                             case="bug", base=base, attempt=bad, session="s")
    data = env.gate_events("t-001")[-1]["data"]
    assert got.ok is False and got.attempt == {}
    assert data["code"] == "GATE-MARKERS" and data["reason"].startswith("killed by signal")
    assert data["rc"] is not None and (data["rc"] < 0 or data["rc"] >= 128)


@needs_jail
def test_a_run_killed_by_a_signal_gives_no_report_but_a_plain_exit_keeps_it(env):
    contract, _rep = _repair(env)
    tree = env.tree("t-001", {"src/app.txt": "bug\n"})
    env.freeze("pack/suite.sh", RD + "echo %s\necho \"TEST: s1 ... PASS $T\"\necho %s\nexit 1\n"
               % (BEGIN, ENDT))
    plain = gates.run_item(env.conn, env.root, run=RUN, task="t-001", contract=contract,
                           item="pass-to-pass", wt=tree, side="base", session="s")
    assert plain.verdict == vc.PASS and plain.rc == 1 and plain.tests.tests == {"s1": "PASS"}
    env.freeze("pack/suite.sh", RD + "echo %s\necho \"TEST: s1 ... PASS $T\"\necho %s\n"
               "kill -9 $$\n" % (BEGIN, ENDT))
    killed = gates.run_item(env.conn, env.root, run=RUN, task="t-002", contract=contract,
                            item="pass-to-pass", wt=tree, side="base", session="s")
    data = env.gate_events("t-002")[-1]["data"]
    assert killed.verdict == vc.FAIL and killed.tests is None
    assert data["code"] == "GATE-MARKERS" and data["reason"].startswith("killed by signal")


# ------------------------------------------------ check round 1: reuse after a change
@needs_jail
def test_an_unreadable_folder_in_the_tree_is_never_reused(env):
    """A tree folder the host cannot list is refused before the jail starts (check round 8 M2),
    so no run on it gives a result to reuse; the digest's fresh random value backs that up."""
    if os.geteuid() == 0:
        pytest.skip("root lists a mode 000 folder")
    wt = env.tree("t-001", {"src/app.txt": "x\n", "src/secret/val.txt": "good\n"})
    sec = os.path.join(wt.path, "src", "secret")
    contract = _one_stage("chmod 700 ${WORKTREE}/src/secret; "
                          "v=$(cat ${WORKTREE}/src/secret/val.txt); "
                          "chmod 000 ${WORKTREE}/src/secret; test \"$v\" = good")
    os.chmod(sec, 0)
    try:
        first = _chain(env, contract, wt).results[0]
        assert first.verdict == vc.BLOCKED
        again = _chain(env, contract, wt).results[0]
        assert again.reused is False and again.verdict == vc.BLOCKED
        assert env.gate_events()[-1]["data"]["reason"] == \
            "tree refused: src/secret: folder cannot be listed"
    finally:
        os.chmod(sec, 0o700)


@needs_jail
def test_an_unreadable_file_in_the_tree_is_never_reused(env):
    wt = env.tree("t-001", {"src/app.txt": "x\n", "src/val.txt": "good\n"})
    val = os.path.join(wt.path, "src", "val.txt")
    contract = _one_stage("chmod 600 ${WORKTREE}/src/val.txt; v=$(cat ${WORKTREE}/src/val.txt); "
                          "chmod 000 ${WORKTREE}/src/val.txt; test \"$v\" = good")
    os.chmod(val, 0)
    try:
        assert _chain(env, contract, wt).results[0].verdict == vc.PASS
        os.chmod(val, 0o600)
        write(val, "bad\n")
        os.chmod(val, 0)
        again = _chain(env, contract, wt).results[0]
        assert again.reused is False and again.verdict == vc.FAIL
    finally:
        os.chmod(val, 0o600)


def test_the_digest_of_an_unreadable_entry_never_repeats(tmp_path):
    (tmp_path / "d").mkdir()
    (tmp_path / "d" / "f").write_text("x")
    (tmp_path / "g").write_text("y")
    assert gates._dir_digest(str(tmp_path)) == gates._dir_digest(str(tmp_path))
    if os.geteuid() == 0:
        pytest.skip("root reads a mode 000 entry")
    for entry in (tmp_path / "d", tmp_path / "g"):
        os.chmod(entry, 0)
        try:
            assert gates._dir_digest(str(tmp_path)) != gates._dir_digest(str(tmp_path))
        finally:
            os.chmod(entry, 0o700)


def _spelled_trees(tmp_path):
    """Tree A holds a (text X) and b (text Y) at its top. Each other tree holds one file with text
    Y whose name spells A's two lines in a line format that joins the parts with spaces: 'a <mode
    word> <sha(X)>' newline 'F b', the mode word being the exec bit (the format before fix round
    10) or the permission bits as a number."""
    x, y = "one\n", "two\n"
    one = tmp_path / "one"
    one.mkdir()
    (one / "a").write_text(x)
    (one / "b").write_text(y)
    sha = hashlib.sha256(x.encode()).hexdigest()
    spelled = []
    for word in ("0", str(stat.S_IMODE(os.stat(one / "a").st_mode))):
        other = tmp_path / ("spelled-" + word)
        other.mkdir()
        (other / ("a %s %s\nF b" % (word, sha))).write_text(y)
        spelled.append(str(other))
    return str(one), spelled


def test_a_name_that_holds_a_newline_cannot_spell_another_trees_digest(tmp_path):
    """Builder attack A3a (fix round 10): each entry is one JSON line, so a crafted name cannot
    make a different tree hash like an earlier one."""
    one, spelled = _spelled_trees(tmp_path)
    for other in spelled:
        assert gates._dir_digest(one) != gates._dir_digest(other), other
    # a link target that spells a line, and a folder name that does, are no different
    links = tmp_path / "links"
    (links / "l").mkdir(parents=True)
    os.symlink("t\nF x 0 y", links / "l" / "k")
    other = tmp_path / "other"
    (other / "l").mkdir(parents=True)
    os.symlink("t", other / "l" / "k")
    (other / "l" / "x").write_text("")
    assert gates._dir_digest(str(links)) != gates._dir_digest(str(other))


def test_the_digest_covers_every_permission_bit_of_files_and_folders(tmp_path):
    """Builder attack A3b (fix round 10): a stage that tests the write bit gives another result
    after a chmod, so the digest must change with it."""
    (tmp_path / "d").mkdir()
    (tmp_path / "d" / "f").write_text("x")
    first = gates._dir_digest(str(tmp_path))
    os.chmod(tmp_path / "d" / "f", 0o444)
    second = gates._dir_digest(str(tmp_path))
    assert second != first
    mode = stat.S_IMODE(os.stat(tmp_path / "d").st_mode)
    os.chmod(tmp_path / "d", 0o555)
    try:
        assert gates._dir_digest(str(tmp_path)) != second
    finally:
        os.chmod(tmp_path / "d", mode)
    assert gates._dir_digest(str(tmp_path)) == second


@needs_jail
@pytest.mark.parametrize("what", ["spelled-name", "write-bit"])
def test_a_crafted_name_or_a_changed_mode_is_not_answered_with_a_reused_result(env, what):
    """Builder attacks A3a and A3b (fix round 10), end to end: the stage passes on the first tree
    and fails on the changed one, so a reused PASS would be a stale result."""
    x, y = "one\n", "two\n"
    if what == "spelled-name":
        wt = env.tree("t-001", {"a": x, "b": y})
        contract = _one_stage("test -f ${WORKTREE}/a")
    else:
        wt = env.tree("t-001", {"src/app.txt": x})
        contract = _one_stage("test -w ${WORKTREE}/src/app.txt")
    first = _chain(env, contract, wt).results[0]
    assert first.verdict == vc.PASS
    assert _chain(env, contract, wt).results[0].reused is True
    if what == "spelled-name":
        os.remove(os.path.join(wt.path, "a"))
        os.remove(os.path.join(wt.path, "b"))
        write(os.path.join(wt.path, "a 0 %s\nF b" % hashlib.sha256(x.encode()).hexdigest()), y)
    else:
        os.chmod(os.path.join(wt.path, "src", "app.txt"), 0o444)
    again = _chain(env, contract, wt).results[0]
    assert again.reused is False and again.verdict == vc.FAIL


@needs_jail
def test_a_change_under_the_top_git_is_not_answered_with_a_reused_result(env):
    wt = env.tree("t-001", {"src/app.txt": "x\n", ".git/payload": "good\n"})
    contract = _one_stage("test \"$(cat ${WORKTREE}/.git/payload)\" = good")
    first = _chain(env, contract, wt).results[0]
    assert first.verdict == vc.PASS
    assert _chain(env, contract, wt).results[0].reused is True
    write(os.path.join(wt.path, ".git", "payload"), "bad\n")
    again = _chain(env, contract, wt).results[0]
    assert again.reused is False and again.verdict == vc.FAIL
    # a worktree's .git file (its gitdir line) is hashed too
    wt2 = env.tree("t-002", {"src/app.txt": "x\n", ".git": "gitdir: /a\n"})
    before = gates._dir_digest(wt2.path)
    write(os.path.join(wt2.path, ".git"), "gitdir: /b\n")
    assert gates._dir_digest(wt2.path) != before


@needs_jail
def test_a_changed_read_path_is_not_answered_with_a_reused_result(env, tmp_path, monkeypatch):
    monkeypatch.setattr(gates.rcontract, "check_read_paths", lambda contract: [])
    data = tmp_path / "dataset"
    data.mkdir()
    (data / "value.txt").write_text("one\n")
    single = tmp_path / "single.txt"
    single.write_text("one\n")
    contract = make_contract(
        stages=(stage("unit", "test \"$(cat %s/value.txt)\" = one && "
                              "test \"$(cat %s)\" = one" % (data, single)),),
        gates=(GateStage("unit", 30),), read_paths=(str(data), str(single)),
        read_real=(str(data), str(single)))
    wt = env.tree("t-001")
    first = _chain(env, contract, wt).results[0]
    assert first.verdict == vc.PASS
    same = _chain(env, contract, wt).results[0]
    assert same.reused is True and same.input_sha256 == first.input_sha256
    (data / "value.txt").write_text("two\n")
    folder = _chain(env, contract, wt).results[0]
    assert folder.reused is False and folder.input_sha256 != first.input_sha256
    assert folder.verdict == vc.FAIL
    (data / "value.txt").write_text("one\n")
    single.write_text("two\n")
    one_file = _chain(env, contract, wt).results[0]
    assert one_file.reused is False and one_file.verdict == vc.FAIL
    assert one_file.input_sha256 not in (first.input_sha256, folder.input_sha256)


# ------------------------------- check round 2: the contract, tool and reproducer parts
@needs_jail
def test_a_changed_contract_is_not_answered_with_a_reused_result(env):
    """Same task, same tree; only the stage command changes. The contract digest is part of
    the fingerprint, so the old PASS is not handed back (EC-008)."""
    wt = env.tree("t-001")
    passing = _one_stage("true")
    failing = _one_stage("exit 1")
    first = _chain(env, passing, wt).results[0]
    assert first.verdict == vc.PASS
    assert _chain(env, passing, wt).results[0].reused is True
    again = _chain(env, failing, wt).results[0]
    assert again.reused is False and again.verdict == vc.FAIL
    assert again.input_sha256 != first.input_sha256


@needs_jail
def test_a_changed_tool_file_is_not_answered_with_a_reused_result(env, tmp_path):
    """The stage runs a tool file named in tools_real. The tool's bytes change between two
    runs on the same tree: the tool digest is part of the fingerprint, so the gate runs again."""
    tool = tmp_path / "bin" / "mytool"
    tool.parent.mkdir()
    write(str(tool), "#!/bin/sh\nexit 0\n", 0o755)
    contract = make_contract(stages=(stage("unit", str(tool)),), gates=(GateStage("unit", 30),),
                             tools=("mytool",), tools_real=(str(tool),))
    wt = env.tree("t-001")
    first = _chain(env, contract, wt).results[0]
    assert first.verdict == vc.PASS
    assert _chain(env, contract, wt).results[0].reused is True
    write(str(tool), "#!/bin/sh\nexit 1\n", 0o755)
    again = _chain(env, contract, wt).results[0]
    assert again.reused is False and again.verdict == vc.FAIL
    assert again.input_sha256 != first.input_sha256


@needs_jail
def test_a_reproved_reproducer_is_not_answered_with_a_reused_result(env):
    """The reproducer is replaced and proved again (a new research-reproducer event with the new
    hash, so the hash check passes). t_bug now passes on the base too; a reused base result
    would still say FAIL and hide the weak test. The reproducer digest is part of the
    fail-to-pass fingerprint, so the base side runs again and names t_bug weak."""
    contract, rep = _repair(env)
    base = env.tree("t-001-base", {"src/app.txt": "bug\n"})
    fixed = env.tree("t-001", {"src/app.txt": "fixed\n"})
    kw = dict(run=RUN, task="t-001", contract=contract, case="bug", base=base, attempt=fixed,
              session="s")
    assert gates.fail_to_pass(env.conn, env.root, **kw).ok is True
    write(os.path.join(rep, "run.sh"),
          RD + "echo %s\necho \"TEST: t_bug ... PASS $T\"\necho %s\n" % (BEGIN, ENDT))
    _prove(env, rep)
    got = gates.fail_to_pass(env.conn, env.root, **kw)
    assert got.weak == ("t_bug",) and got.ok is False
    base_ev = [e["data"] for e in env.gate_events("t-001") if e["data"]["side"] == "base"][-1]
    assert base_ev["reused"] is False and base_ev["weak"] == ["t_bug"]


@needs_jail
def test_the_fingerprint_is_taken_after_the_heavy_lock_is_held(env, monkeypatch):
    """The tree changes while the stage waits for its lock: the result is recorded under the
    fingerprint of the tree it ran on, so the old tree never gets it back."""
    wt = env.tree("t-001", {"src/app.txt": "bad\n"})
    app = os.path.join(wt.path, "src", "app.txt")
    contract = _one_stage("grep -q good ${WORKTREE}/src/app.txt", heavy=True, lock_timeout_s=30)

    @contextlib.contextmanager
    def changed_while_waiting(stage_id, root, *, timeout_s):
        write(app, "good\n")
        yield ()
    with monkeypatch.context() as m:
        m.setattr(gates.locks, "heavy", changed_while_waiting)
        first = _chain(env, contract, wt).results[0]
    assert first.verdict == vc.PASS
    write(app, "bad\n")
    again = _chain(env, contract, wt).results[0]
    assert again.reused is False and again.verdict == vc.FAIL
    write(app, "good\n")
    assert _chain(env, contract, wt).results[0].input_sha256 == first.input_sha256


# ------------------------------------------ check round 1: logs, judged copies, record
@needs_jail
def test_each_run_keeps_its_own_log_and_a_reused_event_keeps_the_reused_log(env):
    wt = env.tree("t-001", {"src/app.txt": "good\n"})
    contract = _one_stage("cat ${WORKTREE}/src/app.txt; grep -q good ${WORKTREE}/src/app.txt")
    assert _chain(env, contract, wt).results[0].verdict == vc.PASS
    ev1 = env.gate_events()[-1]["data"]
    write(os.path.join(wt.path, "src", "app.txt"), "bad\n")
    assert _chain(env, contract, wt).results[0].verdict == vc.FAIL
    ev2 = env.gate_events()[-1]["data"]
    assert ev1["log"] != ev2["log"] and ev1["judged"] != ev2["judged"]
    assert ev1["tag"] != ev2["tag"]
    for ev in (ev1, ev2):
        assert gates._sha256_file(os.path.join(env.root, ev["log"])) == ev["log_sha256"]
    # the reused event names the reused run's log with the hash that run recorded; it never
    # re-hashes the file (here the file is touched after its run, and the record still holds
    # the recorded hash)
    log1 = os.path.join(env.root, ev1["log"])
    kept = open(log1, "rb").read()
    with open(log1, "ab") as fh:
        fh.write(b"appended later\n")
    write(os.path.join(wt.path, "src", "app.txt"), "good\n")
    third = _chain(env, contract, wt).results[0]
    ev3 = env.gate_events()[-1]["data"]
    assert third.reused is True and third.verdict == vc.PASS
    assert ev3["log"] == ev1["log"] and ev3["log_sha256"] == ev1["log_sha256"]
    assert ev3["reused_from"] and "bad" not in open(third.log).read()
    with open(log1, "wb") as fh:
        fh.write(kept)
    assert gates._sha256_file(log1) == ev3["log_sha256"]


def test_a_run_tag_is_never_handed_out_twice(tmp_path):
    stage_dir = str(tmp_path / "01-unit")
    tags = [gates._claim_tag(stage_dir, "attempt") for _ in range(3)]
    assert tags == ["attempt-1", "attempt-2", "attempt-3"]
    assert gates._claim_tag(stage_dir, "verify") == "verify-1"


@needs_jail
def test_an_evidence_name_that_is_not_utf8_is_refused_and_recorded(env):
    wt = env.tree("t-001")
    res = _chain(env, _one_stage("printf x > \"${EVIDENCE_DIR}/$(printf 'a\\377b')\""), wt)
    assert res.verdict == vc.BLOCKED and res.results[0].event_id
    data = env.gate_events()[-1]["data"]
    assert data["code"] == "GATE-EVIDENCE-LINK" and data["reason"] == "a\\xffb: name not UTF-8"


def test_judged_copy_refuses_an_unreadable_entry_and_a_name_that_is_not_utf8(tmp_path):
    src = tmp_path / "evidence"
    (src / "d").mkdir(parents=True)
    (src / "d" / "f").write_text("x")
    dest = tmp_path / "judged"
    if os.geteuid() != 0:
        os.chmod(src / "d", 0)
        try:
            with pytest.raises(GateInfraError) as exc:
                gates.judged_copy(str(src), str(dest))
            assert exc.value.args[1] == "d: unreadable"
            assert not dest.exists()
        finally:
            os.chmod(src / "d", 0o700)
    with open(os.path.join(os.fsencode(str(src)), b"a\xffb"), "wb") as fh:
        fh.write(b"x")
    with pytest.raises(GateInfraError) as exc:
        gates.judged_copy(str(src), str(dest))
    assert exc.value.args[1] == "a\\xffb: name not UTF-8"
    assert not dest.exists()


# ------------------------------------------------------------ check round 1: read paths
@needs_jail
def test_a_read_path_finding_blocks_every_gate_jail_before_it_starts(env, tmp_path,
                                                                    monkeypatch):
    data = tmp_path / "dataset"
    data.mkdir()
    calls = []

    def finding(contract):
        calls.append(tuple(contract.read_real))
        return ["%s: socket s.sock" % data]
    monkeypatch.setattr(gates.rcontract, "check_read_paths", finding)
    contract = make_contract(stages=(stage("unit", "touch ${WORKTREE}/ran"),),
                             gates=(GateStage("unit", 30),), read_paths=(str(data),),
                             read_real=(str(data),))
    wt = env.tree("t-001")
    res = _chain(env, contract, wt)
    ev = env.gate_events()[-1]["data"]
    assert res.verdict == vc.BLOCKED and calls == [(str(data),)]
    assert ev["reason"] == "read path refused: %s: socket s.sock" % data and ev["ran"] is False
    assert not os.path.exists(os.path.join(wt.path, "ran"))
    # run_item and verify_rerun start their jails through the same check
    repair, _rep = _repair(env)
    repair = dataclasses.replace(repair, read_paths=(str(data),), read_real=(str(data),))
    item = gates.run_item(env.conn, env.root, run=RUN, task="t-002", contract=repair,
                          item="pass-to-pass", wt=wt, side="base", session="s")
    assert item.verdict == vc.BLOCKED
    top, tree, base = _try_trees(env, "s1", {"src/app.txt": "x\n"}, {"src/app.txt": "x\n"},
                                 task="t-003")
    receipt = gates.verify_rerun(env.conn, env.root, run=RUN, task="t-003", contract=contract,
                                 request={"stage": "unit", "side": "tree", "n": 1},
                                 trees={"tree": tree, "base": base}, session="s")
    assert receipt["verdict"] == "BLOCKED"
    assert not os.path.exists(os.path.join(tree.path, "ran"))
    # a check that cannot run (t-096's stub raises NotImplementedError) fails closed
    def not_built(contract):
        raise NotImplementedError("t-096")
    monkeypatch.setattr(gates.rcontract, "check_read_paths", not_built)
    wt4 = env.tree("t-004")
    assert _chain(env, contract, wt4, task="t-004").verdict == vc.BLOCKED
    assert "not built" in env.gate_events("t-004")[-1]["data"]["reason"]
    # negative: clean read paths let the stage run, and a contract without any skips the check
    monkeypatch.setattr(gates.rcontract, "check_read_paths", lambda contract: [])
    wt5 = env.tree("t-005")
    assert _chain(env, contract, wt5, task="t-005").verdict == vc.PASS
    assert os.path.exists(os.path.join(wt5.path, "ran"))
    monkeypatch.setattr(gates.rcontract, "check_read_paths", finding)
    calls.clear()
    plain = dataclasses.replace(contract, read_paths=(), read_real=())
    assert _chain(env, plain, env.tree("t-006"), task="t-006").verdict == vc.PASS
    assert calls == []


# ----------------------------------------------------- check round 1: minor findings
@needs_jail
def test_a_spread_wider_than_the_margin_is_untested_for_any_metric(env):
    metric = Metric(id="speed", command=RD + "echo %s; echo METRIC: speed ${SEED} $T; echo %s"
                    % (BEGIN, ENDT), parser="markers", unit="ms", direction="lower",
                    kind="deterministic", repeats=2, min_ok=2, margin=5, timeout_s=30,
                    seeds=(10, 20))
    wide = gates.measure(env.conn, env.root, run=RUN, task="t-001",
                         contract=make_contract(metrics=(metric,)), wt=env.tree("t-001"),
                         session="s")
    assert wide["speed"].spread == 10.0 and wide["speed"].untested is True
    narrow = gates.measure(env.conn, env.root, run=RUN, task="t-002",
                           contract=make_contract(metrics=(dataclasses.replace(
                               metric, margin=50),)), wt=env.tree("t-002"), session="s")
    assert narrow["speed"].untested is False


def test_fail_to_pass_needs_a_proved_reproducer(env):
    contract, rep = _repair(env)
    _prove(env, rep, proved=False)
    got = gates.fail_to_pass(env.conn, env.root, run=RUN, task="t-001", contract=contract,
                             case="bug", base=env.tree("t-001-base", {"src/app.txt": "bug\n"}),
                             attempt=env.tree("t-001", {"src/app.txt": "fixed\n"}), session="s")
    data = env.gate_events("t-001")[-1]["data"]
    assert got.ok is False and data["verdict"] == "BLOCKED"
    assert "does not say proved" in data["reason"]


# ------------------------------------------ check round 3: special files, bounded events
@needs_jail
def test_a_tree_with_a_socket_or_fifo_is_refused_before_the_gate_jail(env, monkeypatch):
    contract = make_contract(stages=(stage("unit", "touch ${WORKTREE}/ran"),),
                             gates=(GateStage("unit", 30),))
    wt = env.tree("t-001")
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        monkeypatch.chdir(os.path.join(wt.path, "src"))     # a short path for bind
        listener.bind("leak.sock")
        listener.listen(1)
        res = _chain(env, contract, wt)
    finally:
        listener.close()
    data = env.gate_events("t-001")[-1]["data"]
    assert res.verdict == vc.BLOCKED
    assert data["reason"] == "tree refused: src/leak.sock: socket" and data["ran"] is False
    assert data["offline"] is None
    assert not os.path.exists(os.path.join(wt.path, "ran"))
    fifo = env.tree("t-002")
    os.mkfifo(os.path.join(fifo.path, "src", "pipe"))
    assert _chain(env, contract, fifo, task="t-002").verdict == vc.BLOCKED
    assert env.gate_events("t-002")[-1]["data"]["reason"] == "tree refused: src/pipe: FIFO"
    # negative: a tree of plain files and links runs
    clean = env.tree("t-003")
    os.symlink("app.txt", os.path.join(clean.path, "src", "link"))
    assert _chain(env, contract, clean, task="t-003").verdict == vc.PASS
    assert os.path.exists(os.path.join(clean.path, "ran"))


def test_the_tree_refusal_names_five_special_files_and_counts_the_rest(tmp_path):
    (tmp_path / "a" / "b").mkdir(parents=True)
    for i in range(7):
        os.mkfifo(str(tmp_path / "a" / "b" / ("p%d" % i)))
    (tmp_path / "plain.txt").write_text("x")
    got = gates._tree_specials(str(tmp_path))
    assert got == ["a/b/p%d: FIFO" % i for i in range(5)] + ["and 2 more"]
    assert gates._tree_specials(str(tmp_path / "a" / "missing")) == []


def test_judged_copy_refuses_a_folder_with_too_many_entries(tmp_path, monkeypatch):
    monkeypatch.setattr(gates, "_MAX_EVIDENCE", 3)
    src = tmp_path / "evidence"
    (src / "d").mkdir(parents=True)
    (src / "d" / "one.txt").write_text("1")
    (src / "two.txt").write_text("2")
    assert sorted(gates.judged_copy(str(src), str(tmp_path / "j1"))) == ["d/one.txt", "two.txt"]
    (src / "d" / "three.txt").write_text("3")
    with pytest.raises(GateInfraError) as exc:
        gates.judged_copy(str(src), str(tmp_path / "j2"))
    assert exc.value.args == ("GATE-EVIDENCE-LINK", ".: more than 3 entries")
    assert not (tmp_path / "j2").exists()


@needs_jail
def test_a_flood_of_test_lines_is_a_bounded_marker_failure(env, monkeypatch):
    monkeypatch.setattr(markers, "_MAX_RESULTS", 3)
    contract, _rep = _repair(env)
    tree = env.tree("t-001", {"src/app.txt": "bug\n"})
    env.freeze("pack/suite.sh", RD + "echo %s\n"
               "for i in 1 2 3 4; do echo \"TEST: s$i ... PASS $T\"; done\necho %s\n"
               % (BEGIN, ENDT))
    flood = gates.run_item(env.conn, env.root, run=RUN, task="t-001", contract=contract,
                           item="pass-to-pass", wt=tree, side="base", session="s")
    data = env.gate_events("t-001")[-1]["data"]
    assert flood.verdict == vc.FAIL and flood.tests is None and data["tests"] is None
    assert data["code"] == "GATE-MARKERS"
    assert data["reason"] == "markers-conflict: too many results: more than 3 test names"
    # negative: a report at the limit is recorded whole
    env.freeze("pack/suite.sh", RD + "echo %s\n"
               "for i in 1 2 3; do echo \"TEST: s$i ... PASS $T\"; done\necho %s\n" % (BEGIN, ENDT))
    ok = gates.run_item(env.conn, env.root, run=RUN, task="t-002", contract=contract,
                        item="pass-to-pass", wt=tree, side="base", session="s")
    assert ok.verdict == vc.PASS and len(ok.tests.tests) == 3


# ------------------- check round 1 (generation 2): reuse on the metric and test paths
@needs_jail
def test_a_metric_is_reused_only_while_its_tree_is_unchanged(env):
    """The fail case on the metric path: the same metric on the same task and tree is reused;
    after the tree changes it runs again and measures the new tree."""
    wt = env.tree("t-001", {"src/app.txt": "3\n"})
    metric = Metric(id="speed", command=RD + "echo %s; echo METRIC: speed "
                    "$(cat ${WORKTREE}/src/app.txt) $T; echo %s" % (BEGIN, ENDT), parser="markers",
                    unit="ms", direction="lower", kind="deterministic", repeats=1, min_ok=1,
                    margin=0, timeout_s=30)
    kw = dict(run=RUN, task="t-001", contract=make_contract(metrics=(metric,)), wt=wt,
              session="s")
    first = gates.measure(env.conn, env.root, **kw)["speed"]
    assert first.median == 3.0 and env.gate_events()[-1]["data"]["reused"] is False
    again = gates.measure(env.conn, env.root, **kw)["speed"]
    data = env.gate_events()[-1]["data"]
    assert data["reused"] is True and data["ran"] is False and again.median == 3.0
    write(os.path.join(wt.path, "src", "app.txt"), "9\n")
    changed = gates.measure(env.conn, env.root, **kw)["speed"]
    data = env.gate_events()[-1]["data"]
    assert data["reused"] is False and data["ran"] is True and changed.median == 9.0


@needs_jail
def test_a_fail_to_pass_side_is_reused_only_while_its_tree_is_unchanged(env):
    contract, _rep = _repair(env)
    base = env.tree("t-001-base", {"src/app.txt": "bug\n"})
    att = env.tree("t-001", {"src/app.txt": "fixed\n"})
    kw = dict(run=RUN, task="t-001", contract=contract, case="bug", base=base, attempt=att,
              session="s")
    assert gates.fail_to_pass(env.conn, env.root, **kw).ok is True
    assert gates.fail_to_pass(env.conn, env.root, **kw).ok is True
    assert [e["data"]["reused"] for e in env.gate_events("t-001")[-2:]] == [True, True]
    # the fail case: the attempt tree no longer holds the fix, so its side runs again and fails
    write(os.path.join(att.path, "src", "app.txt"), "bug\n")
    got = gates.fail_to_pass(env.conn, env.root, **kw)
    base_ev, att_ev = [e["data"] for e in env.gate_events("t-001")[-2:]]
    assert base_ev["side"] == "base" and base_ev["reused"] is True
    assert att_ev["side"] == "attempt" and att_ev["reused"] is False and att_ev["ran"] is True
    assert got.ok is False and got.attempt == {"t_bug": "FAIL"}


@needs_jail
def test_a_pass_to_pass_side_is_reused_only_while_its_tree_is_unchanged(env):
    contract, _rep = _repair(env)
    base = env.tree("t-001-base", {"src/app.txt": "bug\n"})
    att = env.tree("t-001", {"src/app.txt": "fixed\n"})
    kw = dict(run=RUN, task="t-001", contract=contract, base=base, attempt=att,
              baseline=("s1", "s2"), session="s")
    assert gates.pass_to_pass(env.conn, env.root, **kw).ok is True
    assert gates.pass_to_pass(env.conn, env.root, **kw).ok is True
    assert env.gate_events("t-001")[-1]["data"]["reused"] is True
    write(os.path.join(att.path, "src", "app.txt"), "broken\n")
    got = gates.pass_to_pass(env.conn, env.root, **kw)
    data = env.gate_events("t-001")[-1]["data"]
    assert data["reused"] is False and data["ran"] is True
    assert got.ok is False and got.lost == ("s2",)


@needs_jail
def test_metric_and_test_item_events_record_command_rc_log_and_fingerprint(env):
    wt = env.tree("t-001")
    metric = Metric(id="speed", command=RD + "echo %s; echo METRIC: speed 5 $T; echo %s"
                    % (BEGIN, ENDT), parser="markers", unit="ms", direction="lower",
                    kind="deterministic", repeats=1, min_ok=1, margin=0, timeout_s=30)
    result = _chain(env, make_contract(metrics=(metric,)), wt)
    data = env.gate_events("t-001")[-1]["data"]
    assert data["input_sha256"] == result.results[0].input_sha256
    assert len(data["input_sha256"]) == 64 and data["command"].endswith(ENDT)
    assert data["rc"] == 0 and data["log"] and data["log_sha256"] == gates._sha256_file(
        os.path.join(env.root, data["log"]))
    contract, _rep = _repair(env)
    got = gates.fail_to_pass(env.conn, env.root, run=RUN, task="t-002", contract=contract,
                             case="bug", base=env.tree("t-002-base", {"src/app.txt": "bug\n"}),
                             attempt=env.tree("t-002", {"src/app.txt": "fixed\n"}), session="s")
    assert got.ok is True
    for ev in env.gate_events("t-002"):
        side = ev["data"]
        assert side["command"] and side["command"].startswith("sh ")
        assert side["rc"] == 0 and side["log"] and len(side["input_sha256"]) == 64
        assert side["log_sha256"] == gates._sha256_file(os.path.join(env.root, side["log"]))


# ---------------------------- check round 1 (generation 2): the results token (B1, B2)
def test_only_the_runs_token_closes_the_block():
    log = "\n".join([BEGIN, "TEST: a ... PASS tok123", END + " tok123", "TEST: after ... FAIL"])
    assert markers.parse_tests(markers._with_token(log, "tok123")).tests == {"a": "PASS"}
    # CRLF and spaces around the line are read as _lines reads them
    assert markers.parse_tests(markers._with_token(
        log.replace("\n", "\r\n").replace(END + " tok123", "  " + END + "\ttok123 "),
        "tok123")).tests == {"a": "PASS"}
    for bad in (log.replace(END + " tok123", END),                    # a bare END
                log.replace("tok123", "tok999"),                      # another run's token
                log.replace(END + " tok123", END + " tok123 x"),      # more words
                log.replace(BEGIN, END + "\n" + BEGIN),               # a bare END before
                log + "\n" + END):                                    # a bare END after
        with pytest.raises(MarkerError) as exc:
            markers.parse_tests(markers._with_token(bad, "tok123"))
        assert exc.value.args[0] == "markers-conflict", bad
        assert "token" in exc.value.args[1]
    for token in ("", "a b"):
        with pytest.raises(ValueError):
            markers._with_token(log, token)


IN_PROCESS_BENCH = (
    "import os, sys\n"
    "token = os.read(int(os.environ['ALPACA_RESULTS_FD']), 64).decode().strip()\n"
    "os.close(int(os.environ['ALPACA_RESULTS_FD']))\n"
    "print('%s', flush=True)\n"
    "sys.path.insert(0, os.path.join(os.environ['WORKTREE'], 'src'))\n"
    "import solver\n"
    "solver.solve()\n"
    "print('METRIC: speed 500 ' + token, flush=True)\n"
    "print('%s ' + token, flush=True)\n" % (BEGIN, END))


@needs_jail
def test_tested_code_that_prints_a_tokenless_end_and_exits_zero_gets_no_metric(env):
    """Check round 1 (generation 2), B1: the frozen bench imports the solver, as a real bench
    imports the code it measures; the solver prints its own METRIC line and a bare END and calls
    os._exit(0), so the bench's own lines never appear. Without the run's token the END is
    refused."""
    env.freeze("pack/bench.py", IN_PROCESS_BENCH)
    metric = Metric(id="speed", command="python3 ${CONTRACT_DIR}/pack/bench.py",
                    parser="markers", unit="ms", direction="lower", kind="deterministic",
                    repeats=1, min_ok=1, margin=0, timeout_s=30)
    contract = make_contract(metrics=(metric,))
    forged = env.tree("t-001", {"src/solver.py": "import os\n"
                                                 "def solve():\n"
                                                 "    print('METRIC: speed 1', flush=True)\n"
                                                 "    print('%s', flush=True)\n"
                                                 "    os._exit(0)\n" % END})
    res = _chain(env, contract, forged)
    data = env.gate_events("t-001")[-1]["data"]
    assert res.verdict == vc.FAIL and res.metrics["speed"].median is None
    assert data["rc"] == 0 and data["code"] == "GATE-MARKERS"
    assert data["repeats"][0]["reason"].startswith("markers-conflict")
    assert "token" in data["repeats"][0]["reason"]
    # negative: an honest solver is measured by the bench's own tokened block
    honest = env.tree("t-002", {"src/solver.py": "def solve():\n    pass\n"})
    assert _chain(env, contract, honest, task="t-002").metrics["speed"].median == 500.0


@needs_jail
def test_a_non_fix_that_prints_a_pass_and_a_tokenless_end_does_not_pass_fail_to_pass(env):
    """Check round 1 (generation 2), B2: a single-process reproducer, as pytest with a marker
    plugin is. On import the attempt's module prints a PASS line and a bare END and exits 0; the
    bug is still there."""
    contract, rep = _repair(env)
    write(os.path.join(rep, "run.sh"),
          "exec python3 -c \"import os, sys; fd = int(os.environ['ALPACA_RESULTS_FD']); "
          "t = os.read(fd, 64).decode().strip(); os.close(fd); print('%s', flush=True); "
          "sys.path.insert(0, '${WORKTREE}/src'); import app; "
          "print('TEST: t_bug ... ' + ('PASS' if app.fixed() else 'FAIL') + ' ' + t, flush=True); "
          "print('%s ' + t, flush=True)\"\n" % (BEGIN, END))
    _prove(env, rep)
    base = env.tree("t-001-base", {"src/app.txt": "bug\n",
                                   "src/app.py": "def fixed():\n    return False\n"})
    cheat = env.tree("t-001", {"src/app.txt": "bug\n",
                               "src/app.py": "import os\n"
                                             "print('TEST: t_bug ... PASS', flush=True)\n"
                                             "print('%s', flush=True)\n"
                                             "os._exit(0)\n"
                                             "def fixed():\n    return False\n" % END})
    got = gates.fail_to_pass(env.conn, env.root, run=RUN, task="t-001", contract=contract,
                             case="bug", base=base, attempt=cheat, session="s")
    data = env.gate_events("t-001")[-1]["data"]
    assert got.ok is False and got.attempt == {} and got.base == {"t_bug": "FAIL"}
    assert data["side"] == "attempt" and data["rc"] == 0 and data["code"] == "GATE-MARKERS"
    assert data["reason"].startswith("markers-conflict") and "token" in data["reason"]
    # negative: the real fix passes through the same reproducer
    real = env.tree("t-002", {"src/app.txt": "x\n",
                              "src/app.py": "def fixed():\n    return True\n"})
    assert gates.fail_to_pass(env.conn, env.root, run=RUN, task="t-002", contract=contract,
                              case="bug", base=base, attempt=real, session="s").ok is True


@needs_jail
def test_the_token_reaches_only_the_runner(env):
    """The runner reads the token from fd 3 and closes it; a child it starts afterwards finds no
    fd 3 and cannot open the runner's; stdin is /dev/null."""
    contract = _one_stage(
        "read -r T <&3; exec 3<&-; echo \"$T\" > ${EVIDENCE_DIR}/runner.txt; "
        "(cat <&3 > ${EVIDENCE_DIR}/child.txt 2>&1 || echo closed > ${EVIDENCE_DIR}/child.txt; "
        "cat /proc/$$/fd/3 >> ${EVIDENCE_DIR}/child.txt 2>/dev/null || true); "
        "wc -c < /dev/stdin > ${EVIDENCE_DIR}/stdin.txt")
    assert _chain(env, contract, env.tree("t-001")).verdict == vc.PASS
    judged = os.path.join(env.root, env.gate_events()[-1]["data"]["judged"])
    token = open(os.path.join(judged, "runner.txt")).read().strip()
    assert len(token) == 32 and int(token, 16) >= 0
    assert token not in open(os.path.join(judged, "child.txt")).read()
    assert open(os.path.join(judged, "stdin.txt")).read().strip() == "0"
    # each run gets its own token
    assert _chain(env, contract, env.tree("t-002"), task="t-002").verdict == vc.PASS
    judged2 = os.path.join(env.root, env.gate_events("t-002")[-1]["data"]["judged"])
    assert open(os.path.join(judged2, "runner.txt")).read().strip() != token


# ------------------------------- check round 1 (generation 2): an overflowing median (B3)
def test_the_middle_of_two_large_values_does_not_overflow():
    assert gates._median([1.7e308, 1.7e308], "higher", "middle") == 1.7e308
    assert gates._median([-1.7e308, 1.7e308], "higher", "middle") == 0.0
    assert gates._median([1.0, 4.0, 2.0, 3.0], "higher", "middle") == 2.5


@needs_jail
def test_a_non_finite_median_or_spread_is_refused_and_never_recorded(env):
    def contract(seeds):
        return make_contract(metrics=(Metric(
            id="speed", command=RD + "echo %s; echo METRIC: speed ${SEED} $T; echo %s"
            % (BEGIN, ENDT), parser="markers", unit="u", direction="higher",
            kind="deterministic", repeats=2, min_ok=2, margin=1e309, timeout_s=30,
            seeds=seeds, median="middle"),))

    def strict(task):
        row = env.conn.execute("SELECT data FROM events WHERE kind=? AND ref=? ORDER BY id DESC "
                               "LIMIT 1", (events.GATE, task)).fetchone()["data"]
        return json.loads(row, parse_constant=lambda c: (_ for _ in ()).throw(ValueError(c)))
    big = _chain(env, contract(("1.7e308", "1.7e308")), env.tree("t-001"))
    assert big.verdict == vc.PASS and big.metrics["speed"].median == 1.7e308
    assert strict("t-001")["median"] == 1.7e308
    # the spread of 1.7e308 and -1.7e308 overflows: FAIL with GATE-MARKERS, nothing infinite
    wide = _chain(env, contract(("1.7e308", "-1.7e308")), env.tree("t-002"), task="t-002")
    data = strict("t-002")
    assert wide.verdict == vc.FAIL and wide.metrics["speed"].median is None
    assert wide.metrics["speed"].spread is None
    assert data["code"] == "GATE-MARKERS" and data["reason"].startswith("markers-conflict")
    assert data["median"] is None and data["metric"]["spread"] is None


# ------------------------------ check round 1 (generation 2): evidence copy failures (B4)
def test_judged_copy_turns_a_write_error_into_a_refusal(tmp_path, monkeypatch):
    src = tmp_path / "evidence"
    src.mkdir()
    (src / "big.bin").write_bytes(b"x" * 300000)
    real = gates.os.write

    def full(fd, data):
        raise OSError(errno.ENOSPC, "No space left on device")
    monkeypatch.setattr(gates.os, "write", full)
    with pytest.raises(GateInfraError) as exc:
        gates.judged_copy(str(src), str(tmp_path / "judged"))
    monkeypatch.setattr(gates.os, "write", real)
    assert exc.value.args == ("GATE-EVIDENCE-LINK", "big.bin: copy failed (No space left on "
                                                    "device)")
    assert not (tmp_path / "judged").exists()
    # the destination itself cannot be made: "." with the reason
    (tmp_path / "ro").mkdir()
    os.chmod(tmp_path / "ro", 0o500)
    try:
        if os.geteuid() != 0:
            with pytest.raises(GateInfraError) as exc:
                gates.judged_copy(str(src), str(tmp_path / "ro" / "judged"))
            assert exc.value.args[1].startswith(".: copy failed (")
    finally:
        os.chmod(tmp_path / "ro", 0o700)
    assert set(gates.judged_copy(str(src), str(tmp_path / "judged"))) == {"big.bin"}


def test_judged_copy_is_bounded_in_bytes_and_in_name_bytes(tmp_path, monkeypatch):
    src = tmp_path / "evidence"
    (src / "sub").mkdir(parents=True)
    (src / "a.bin").write_bytes(b"a" * 60)
    (src / "sub" / "b.bin").write_bytes(b"b" * 60)
    monkeypatch.setattr(gates, "_MAX_EVIDENCE_BYTES", 120)
    assert set(gates.judged_copy(str(src), str(tmp_path / "j1"))) == {"a.bin", "sub/b.bin"}
    monkeypatch.setattr(gates, "_MAX_EVIDENCE_BYTES", 119)
    with pytest.raises(GateInfraError) as exc:
        gates.judged_copy(str(src), str(tmp_path / "j2"))
    assert exc.value.args == ("GATE-EVIDENCE-LINK", ".: more than 119 bytes")
    assert not (tmp_path / "j2").exists()
    monkeypatch.setattr(gates, "_MAX_EVIDENCE_BYTES", 1000)
    # names: "a.bin" (5) + "sub" (3) + "sub/b.bin" (9) = 17 bytes
    monkeypatch.setattr(gates, "_MAX_EVIDENCE_NAMES", 17)
    assert len(gates.judged_copy(str(src), str(tmp_path / "j3"))) == 2
    monkeypatch.setattr(gates, "_MAX_EVIDENCE_NAMES", 16)
    with pytest.raises(GateInfraError) as exc:
        gates.judged_copy(str(src), str(tmp_path / "j4"))
    assert exc.value.args == ("GATE-EVIDENCE-LINK", ".: more than 16 bytes of entry names")


@needs_jail
def test_a_full_disk_while_copying_the_evidence_is_recorded_as_blocked(env, monkeypatch):
    real = gates.os.write

    def full_under_judged(fd, data):
        try:
            where = os.readlink("/proc/self/fd/%d" % fd)
        except OSError:
            where = ""
        if os.sep + "judged" + os.sep in where:
            raise OSError(errno.ENOSPC, "No space left on device")
        return real(fd, data)
    monkeypatch.setattr(gates.os, "write", full_under_judged)
    res = _chain(env, _one_stage("head -c 3000000 /dev/zero > ${EVIDENCE_DIR}/big.bin"),
                 env.tree("t-001"))
    monkeypatch.setattr(gates.os, "write", real)
    (ev,) = env.gate_events("t-001")
    assert res.verdict == vc.BLOCKED and res.first_failure == "unit"
    assert ev["data"]["code"] == "GATE-EVIDENCE-LINK"
    assert ev["data"]["reason"] == "big.bin: copy failed (No space left on device)"


# ---------------------------------- check round 1 (generation 2): a flood of output (B5)
@needs_jail
def test_a_log_over_the_cap_stops_the_run_and_still_records_one_event(env, monkeypatch):
    monkeypatch.setattr(gates, "_MAX_LOG_BYTES", 200000)
    # about 2 MB a second: the temp folder stays small even if the cap did not hold
    flood = "while :; do head -c 20000 /dev/zero | tr '\\0' f; echo; sleep 0.01; done"
    start = time.monotonic()
    res = _chain(env, _one_stage(flood), env.tree("t-001"))
    assert time.monotonic() - start < 20            # stopped long before its 30 s timeout
    (ev,) = env.gate_events("t-001")
    data = ev["data"]
    assert res.verdict == vc.BLOCKED and data["verdict"] == "BLOCKED"
    assert "passed the cap of 200000 bytes" in data["reason"]
    assert data["ran"] is True and data["timed_out"] is False
    assert 200000 < os.path.getsize(os.path.join(env.root, data["log"])) < 20000000
    # a test run over the cap is a marker failure
    contract, _rep = _repair(env)
    env.freeze("pack/suite.sh", RD + "echo %s\nwhile :; do for i in 1 2 3 4 5 6 7 8; do "
               "echo \"TEST: s1 ... PASS $T\"; done; sleep 0.001; done\n" % BEGIN)
    item = gates.run_item(env.conn, env.root, run=RUN, task="t-002", contract=contract,
                          item="pass-to-pass", wt=env.tree("t-002"), side="base", session="s")
    data = env.gate_events("t-002")[-1]["data"]
    assert item.verdict == vc.FAIL and item.tests is None and data["code"] == "GATE-MARKERS"
    assert data["reason"].startswith("markers-conflict: the log passed the cap")
    # negative: a log under the cap is read whole
    ok = _chain(env, _one_stage("yes line | head -c 100000"), env.tree("t-003"), task="t-003")
    assert ok.verdict == vc.PASS


def test_the_host_reads_only_a_bounded_part_of_a_log(tmp_path, monkeypatch):
    log = tmp_path / "x.log"
    log.write_bytes(gates._JAIL_MARK.encode() + b"\n" + b"a" * 100000 + b"\nlast line\n")
    monkeypatch.setattr(gates, "_MAX_LOG_BYTES", 5000)
    assert len(gates._log_text(str(log))) == 5000
    assert gates._log_head(str(log)) == (gates._JAIL_MARK + "\n" + "a" * 100000)[:gates._LOG_HEAD]
    tail = gates._log_tail(str(log))
    assert len(tail) == gates._LOG_TAIL and tail.endswith("last line\n")
    small = tmp_path / "y.log"
    small.write_text(gates._JAIL_MARK + "\nonly\n")
    assert gates._log_tail(str(small)) == "only\n"


# ----------------------------- check round 1 (generation 2): the frozen folder stays (B6)
@needs_jail
def test_code_in_the_tree_cannot_move_the_runbook_folder_aside(env):
    """Check round 1 (generation 2), B6: the build runs the attempt's code, which moves pack/
    aside and writes its own pack/check.sh; the stage then runs check.sh by the runbook
    folder's path."""
    env.freeze("pack/build.sh", "sh ${WORKTREE}/src/build.sh\n")
    env.freeze("pack/check.sh", "grep -q good ${WORKTREE}/src/app.txt\n")
    contract = _one_stage("sh ${RUNBOOK_DIR}/build.sh; sh ${RUNBOOK_DIR}/check.sh")
    evil = ("W=${WORKTREE}\nmv $W/pack $W/pack.old\nmkdir -p $W/pack\n"
            "echo 'exit 0' > $W/pack/check.sh\n")
    bad = env.tree("t-001", {"src/app.txt": "bad\n", "pack/build.sh": "x\n",
                             "pack/check.sh": "x\n", "src/build.sh": evil})
    res = _chain(env, contract, bad)
    assert res.verdict == vc.FAIL
    assert sorted(os.listdir(bad.path)) == ["pack", "src"]
    assert open(os.path.join(bad.path, "pack", "check.sh")).read() == "x\n"
    # negative: the same stage passes on a tree that holds the fix
    good = env.tree("t-002", {"src/app.txt": "good\n", "pack/build.sh": "x\n",
                              "pack/check.sh": "x\n", "src/build.sh": "true\n"})
    assert _chain(env, contract, good, task="t-002").verdict == vc.PASS


@needs_jail
def test_code_in_the_tree_cannot_swap_a_frozen_script_a_metric_runs(env):
    """The metric form of B6: the bench runs the tree's solver, then a frozen score script by
    its path in the tree (the runbook folder) or in the contract folder (whose parent folders
    sit in the jail's own root)."""
    env.freeze("pack/bench.sh", RD + "echo %s\nsh ${WORKTREE}/src/solver.sh\n"
               "T=$T sh ${WORKTREE}/pack/score.sh\n"
               "T=$T sh ${CONTRACT_DIR}/pack/score2.sh\necho %s\n"
               % (BEGIN, ENDT))
    env.freeze("pack/score.sh", "echo METRIC: speed 500 $T\n")
    env.freeze("pack/score2.sh", "echo METRIC: other 7 $T\n")
    contract = make_contract(metrics=tuple(
        Metric(id=ident, command="sh ${CONTRACT_DIR}/pack/bench.sh", parser="markers",
               unit="ms", direction="lower", kind="deterministic", repeats=1, min_ok=1,
               margin=0, timeout_s=30) for ident in ("speed", "other")))
    files = {"pack/bench.sh": "x\n", "pack/score.sh": "x\n", "pack/score2.sh": "x\n"}
    tree_swap = ("W=${WORKTREE}\nmv $W/pack $W/pack.old\nmkdir -p $W/pack\n"
                 "echo 'echo METRIC: speed 1' > $W/pack/score.sh\ntrue\n")
    one = env.tree("t-001", dict(files, **{"src/solver.sh": tree_swap}))
    got = _chain(env, contract, one)
    assert got.metrics["speed"].median == 500.0 and got.metrics["other"].median == 7.0
    assert sorted(os.listdir(one.path)) == ["pack", "src"]
    root_swap = ("C=${CONTRACT_DIR}\nP=$(dirname $C)\nmv $P $P.old\nmkdir -p $C/pack\n"
                 "echo 'echo METRIC: other 1' > $C/pack/score2.sh\ntrue\n")
    two = env.tree("t-002", dict(files, **{"src/solver.sh": root_swap}))
    got = _chain(env, contract, two, task="t-002")
    assert got.metrics["speed"].median == 500.0 and got.metrics["other"].median == 7.0


def test_every_folder_above_an_overlay_is_listed_once_parents_first(tmp_path):
    tree = str(tmp_path / "tree")
    overlays = [("/f/a", tree + "/pack/sub/a.sh"), ("/f/b", tree + "/pack/b.sh"),
                ("/f/c", tree + "/top.txt"), ("/f/d", tree + "/pack/sub/deep/d.sh")]
    assert gates._overlay_parents(tree, overlays) == [
        tree + "/pack", tree + "/pack/sub", tree + "/pack/sub/deep"]
    assert gates._overlay_parents(tree, []) == []


def test_the_parents_of_bound_paths_are_made_read_only():
    from alpaca.research.types import JailSpec
    spec = JailSpec(kind="gate", cwd="/tmp/p/tree", ro=("/tmp/p/files", "/home/u/tool"),
                    rw=("/tmp/p/tree", "/tmp/q"), overlays=(("/tmp/p/files/a", "/tmp/p/tree/a"),))
    base = jail.argv(spec)
    got = gates._read_only_parents(base, spec, spec.cwd)
    fixed = len(jail.argv(JailSpec(kind="gate", cwd=spec.cwd))) - 2
    # /tmp/p gets its own tmpfs before the first bind; /tmp/q is itself bound, so it gets none
    assert got[:fixed] == base[:fixed] and got[fixed:fixed + 2] == ["--tmpfs", "/tmp/p"]
    assert got[fixed + 2:-4] == base[fixed:]
    assert got[-4:] == ["--remount-ro", "/tmp/p", "--remount-ro", "/"]
    home = JailSpec(kind="gate", cwd="/home/u/tree", rw=("/home/u/tree",))
    assert gates._read_only_parents(jail.argv(home), home, home.cwd) == (
        jail.argv(home) + ["--remount-ro", "/"])


# ---------------------------------- check round 1 (generation 2): minor findings
@needs_jail
def test_the_offline_flag_is_read_from_the_jail_argv(env, monkeypatch):
    real = gates.jail.argv
    monkeypatch.setattr(gates.jail, "argv",
                        lambda spec: [a for a in real(spec) if a != "--unshare-net"])
    res = _chain(env, _one_stage("true"), env.tree("t-001"))
    data = env.gate_events("t-001")[-1]["data"]
    assert data["ran"] is True and data["offline"] is False
    assert res.results[0].offline is False
    monkeypatch.setattr(gates.jail, "argv", real)
    _chain(env, _one_stage("true"), env.tree("t-002"), task="t-002")
    assert env.gate_events("t-002")[-1]["data"]["offline"] is True


# ------------------------- check round 6: no jailed process can rewrite the log (log relay)
# A process in the jail tries every way to reach the log in place: pread and pwrite on its own
# fd 1, and opening /proc/<pid>/fd/1 of every process it can see, read-write and (after an
# fchmod, the way a pipe could be taken) read-only. It writes each outcome to ${EVIDENCE_DIR}.
REACH = r"""
import errno, os, sys
out = []
def note(what, fn):
    try:
        got = fn()
        out.append("%s ok" % what)
        return got
    except OSError as exc:
        out.append("%s %s" % (what, errno.errorcode.get(exc.errno, exc.errno)))
note("pread", lambda: os.pread(1, 10, 0))
note("pwrite", lambda: os.pwrite(1, b"x", 0))
note("ftruncate", lambda: os.ftruncate(1, 0))
note("fchmod", lambda: os.fchmod(1, 0o600))
note("open-rdwr", lambda: os.close(os.open("/proc/self/fd/1", os.O_RDWR)))
note("open-rdonly", lambda: os.close(os.open("/proc/self/fd/1", os.O_RDONLY | os.O_NONBLOCK)))
print("still writing", flush=True)
with open(os.path.join(sys.argv[1], "reach.txt"), "w") as fh:
    fh.write("\n".join(out) + "\n")
"""

# A background patcher the tested code leaves behind: until the runner ends it looks for
# sys.argv[1] in the log through every route REACH names and writes sys.argv[2] over it.
PATCHER = r"""
import os, sys, time
old, new = sys.argv[1].encode(), sys.argv[2].encode()
fds = [1]
for name in os.listdir("/proc"):
    if name.isdigit():
        for flags in (os.O_RDWR, os.O_RDONLY | os.O_NONBLOCK):
            try:
                fds.append(os.open("/proc/%s/fd/1" % name, flags))
            except OSError:
                pass
end = time.time() + 5
while time.time() < end:
    for fd in fds:
        try:
            data = os.pread(fd, 1 << 16, 0)
        except OSError:
            try:
                data = os.read(fd, 1 << 16)
            except OSError:
                continue
            if old in data:
                os.write(1, data.replace(old, new))
            continue
        at = data.find(old)
        if at >= 0:
            try:
                os.pwrite(fd, new, at)
            except OSError:
                pass
"""


@needs_jail
def test_a_command_cannot_reach_its_log_in_place(env):
    """Check round 6: the jail's stdout is a socket the relay outside the jail copies into the
    log, so the command can neither seek, truncate nor reopen it (ENXIO), and an fchmod gains
    it nothing."""
    env.freeze("pack/reach.py", REACH)
    res = _chain(env, _one_stage("python3 ${CONTRACT_DIR}/pack/reach.py ${EVIDENCE_DIR}"),
                 env.tree("t-001"))
    data = env.gate_events("t-001")[-1]["data"]
    judged = os.path.join(env.root, data["judged"])
    reach = open(os.path.join(judged, "reach.txt")).read().split("\n")
    assert res.verdict == vc.PASS
    assert reach[:6] == ["pread ESPIPE", "pwrite ESPIPE", "ftruncate EINVAL", "fchmod ok",
                         "open-rdwr ENXIO", "open-rdonly ENXIO"]
    # negative: what the command prints still reaches the log, after the jail's start line
    log = open(os.path.join(env.root, data["log"])).read()
    assert log.startswith(gates._JAIL_MARK + "\n") and "still writing\n" in log


@needs_jail
def test_a_background_patcher_cannot_rewrite_the_runners_metric(env):
    """Check round 6 (blocking): the solver leaves a patcher running that tries to rewrite the
    bench's `METRIC: speed 500` to 001 in the log while the bench is still alive, keeping the
    token line. Every repeat and the median are the bench's own 500."""
    env.freeze("pack/patch.py", PATCHER)
    env.freeze("pack/bench.sh", RD + "echo %s\nsh ${WORKTREE}/src/solver.sh\nsleep 0.3\n"
               "echo METRIC: speed 500 $T\nsleep 0.3\necho %s\nsleep 0.3\n" % (BEGIN, ENDT))
    metric = Metric(id="speed", command="sh ${CONTRACT_DIR}/pack/bench.sh", parser="markers",
                    unit="ms", direction="lower", kind="deterministic", repeats=3, min_ok=3,
                    margin=1000, timeout_s=30)
    solver = ("python3 ${CONTRACT_DIR}/pack/patch.py 'METRIC: speed 500' 'METRIC: speed 001' "
              "</dev/null &\nsleep 0.2\n")
    res = _chain(env, make_contract(metrics=(metric,)),
                 env.tree("t-001", {"src/solver.sh": solver}))
    data = env.gate_events("t-001")[-1]["data"]
    assert res.verdict == vc.PASS and res.metrics["speed"].median == 500.0
    assert list(res.metrics["speed"].repeats) == [500.0, 500.0, 500.0]
    assert data["median"] == 500.0
    for one in data["repeats"]:
        text = open(os.path.join(env.root, one["log"])).read()
        assert "METRIC: speed 500 " in text and "speed 001" not in text


@needs_jail
def test_a_background_patcher_cannot_turn_the_attempts_fail_into_pass(env):
    """Check round 6 (blocking): an unfixed attempt leaves a patcher running under the frozen
    reproducer that tries to rewrite `TEST: t_bug ... FAIL` to PASS in the log, keeping the token
    line. The attempt side stays FAIL, so the unfixed repair does not pass."""
    contract, rep = _repair(env)
    env.freeze("pack/patch.py", PATCHER)
    write(os.path.join(rep, "run.sh"),
          RD + "echo %s\nsh ${WORKTREE}/src/hook.sh\nsleep 0.3\n"
          "if grep -q fixed ${WORKTREE}/src/app.txt; then echo \"TEST: t_bug ... PASS $T\";\n"
          "else echo \"TEST: t_bug ... FAIL $T\"; fi\nsleep 0.3\necho %s\nsleep 0.3\n"
          % (BEGIN, ENDT))
    _prove(env, rep)
    hook = ("python3 ${CONTRACT_DIR}/pack/patch.py 'TEST: t_bug ... FAIL' "
            "'TEST: t_bug ... PASS' </dev/null &\nsleep 0.2\n")
    base = env.tree("t-001-base", {"src/app.txt": "bug\n", "src/hook.sh": "true\n"})
    unfixed = env.tree("t-001", {"src/app.txt": "bug\n", "src/hook.sh": hook})
    got = gates.fail_to_pass(env.conn, env.root, run=RUN, task="t-001", contract=contract,
                             case="bug", base=base, attempt=unfixed, session="s")
    data = env.gate_events("t-001")[-1]["data"]
    assert data["side"] == "attempt"
    assert got.ok is False and got.base == {"t_bug": "FAIL"} and got.attempt == {"t_bug": "FAIL"}
    assert "TEST: t_bug ... PASS" not in open(os.path.join(env.root, data["log"])).read()
    # negative: the real fix passes through the same reproducer, patcher and all
    fixed = env.tree("t-002", {"src/app.txt": "fixed\n", "src/hook.sh": hook})
    assert gates.fail_to_pass(env.conn, env.root, run=RUN, task="t-002", contract=contract,
                              case="bug", base=base, attempt=fixed, session="s").ok is True


def _relay(tmp_path, cap, script, sink=None):
    """Run gates._RELAY on `sh -c script` as procs.run would, with stdout and stderr on sink
    (a log file under tmp_path by default); the return code and the log's bytes."""
    log = str(tmp_path / "relay.log")
    with open(sink or log, "wb") as out:
        proc = subprocess.run([gates._PYTHON, "-I", "-S", "-c", gates._RELAY, str(cap),
                               "/bin/sh", "-c", script], stdout=out, stderr=subprocess.STDOUT,
                              timeout=60)
    return proc.returncode, (open(log, "rb").read() if sink is None else b"")


def test_the_relay_copies_the_stream_and_keeps_the_return_code(tmp_path):
    rc, text = _relay(tmp_path, 1000, "echo out; echo err >&2; exit 7")
    assert rc == 7 and text == b"out\nerr\n"
    # the command's stdout is a socket: it cannot be opened again by path
    rc, text = _relay(tmp_path, 1000, "echo x > /proc/self/fd/1 || echo refused >&2")
    assert rc == 0 and text.endswith(b"refused\n") and b"x\n" not in text
    # a command killed by a signal: the relay dies of the same signal
    rc, _text = _relay(tmp_path, 1000, "kill -TERM $$")
    assert rc == -signal.SIGTERM


def test_the_relay_kills_the_command_past_the_cap_and_drains_a_failed_log(tmp_path):
    start = time.monotonic()
    rc, text = _relay(tmp_path, 5000, "while :; do echo aaaaaaaaaaaaaaaaaaaa; done")
    assert time.monotonic() - start < 30
    assert rc == -9 and 5000 < len(text) <= 5000 + 65536
    # negative: output under the cap is copied whole and the command is left alone
    rc, text = _relay(tmp_path, 5000, "head -c 4000 /dev/zero")
    assert rc == 0 and text == b"\0" * 4000
    # a log that cannot be written (a full disk) stops the copy, never the drain: no hang
    if os.path.exists("/dev/full"):
        rc, _text = _relay(tmp_path, 1 << 30, "head -c 3000000 /dev/zero; exit 3",
                           sink="/dev/full")
        assert rc == 3


# ------------------------------------------- check round 7: tree-writing stages, crashes
def _rebuild(env, wt, files=None):
    """A resume cuts the tree again with the same files (the design rebuilds it from the diff)."""
    shutil.rmtree(wt.path)
    return env.tree(wt.label, files)


@needs_jail
def test_a_prepare_on_a_fresh_tree_runs_again_and_prepares_it(env):
    """Adversarial r7 A4: the prepare step's product is its tree, so a resume that cuts the same
    fresh tree again gets it prepared instead of a reused PASS that left nothing there."""
    contract, _rep = _repair(env)
    prep = stage("prepare", "echo data > ${WORKTREE}/src/data.txt")
    contract = dataclasses.replace(contract, stages=contract.stages + (prep,),
                                   repair_prepare="prepare")
    kw = dict(run=RUN, task="t-090", contract=contract, session="s")
    tree = env.tree("t-090-prep", {"src/app.txt": "bug\n"})
    first = gates.run_prepare(env.conn, env.root, wt=tree, **kw)
    assert first.verdict == vc.PASS
    data = env.gate_events("t-090")[-1]["data"]
    assert data["tree_sha256"] != data["tree_after_sha256"]
    tree = _rebuild(env, tree, {"src/app.txt": "bug\n"})
    second = gates.run_prepare(env.conn, env.root, wt=tree, **kw)
    assert second.reused is False and second.verdict == vc.PASS
    assert second.input_sha256 == first.input_sha256
    assert open(os.path.join(tree.path, "src", "data.txt")).read() == "data\n"


@needs_jail
def test_a_prepare_is_never_reused_even_when_it_left_its_tree_unchanged(env):
    contract, _rep = _repair(env)
    contract = dataclasses.replace(contract, stages=contract.stages + (stage("prepare", "true"),),
                                   repair_prepare="prepare")
    tree = env.tree("t-090-prep", {"src/app.txt": "bug\n"})
    kw = dict(run=RUN, task="t-090", contract=contract, wt=tree, session="s")
    first = gates.run_prepare(env.conn, env.root, **kw)
    data = env.gate_events("t-090")[-1]["data"]
    assert data["tree_sha256"] == data["tree_after_sha256"]
    second = gates.run_prepare(env.conn, env.root, **kw)
    assert second.input_sha256 == first.input_sha256
    assert second.reused is False and env.gate_events("t-090")[-1]["data"]["ran"] is True


@needs_jail
def test_a_stage_that_wrote_its_tree_runs_again_on_a_rebuilt_tree(env):
    """Adversarial r7 A4b: build writes into the tree and unit needs what it wrote. After the
    worktree is rebuilt, build runs again (it changed its tree, so it is never reused) and unit,
    whose tree is now as it was, is reused: the honest attempt still passes."""
    stages = (stage("build", "echo built > ${WORKTREE}/src/out.bin"),
              stage("unit", "test -f ${WORKTREE}/src/out.bin"))
    contract = make_contract(stages=stages, gates=tuple(GateStage(s["id"], 30) for s in stages))
    wt = env.tree("t-001")
    assert _chain(env, contract, wt).verdict == vc.PASS
    build, unit = [e["data"] for e in env.gate_events("t-001")]
    assert build["tree_sha256"] != build["tree_after_sha256"]
    assert unit["tree_sha256"] == unit["tree_after_sha256"] == build["tree_after_sha256"]
    wt = _rebuild(env, wt)
    again = _chain(env, contract, wt)
    assert again.verdict == vc.PASS, again.first_failure
    assert [(r.stage, r.reused) for r in again.results] == [("build", False), ("unit", True)]
    assert os.path.exists(os.path.join(wt.path, "src", "out.bin"))


@needs_jail
def test_a_metric_that_wrote_its_tree_is_measured_again_on_a_rebuilt_tree(env):
    wt = env.tree("t-001")
    metric = Metric(id="speed", command=RD + "echo x >> ${WORKTREE}/src/runs.txt; echo %s; "
                    "echo METRIC: speed 5 $T; echo %s" % (BEGIN, ENDT), parser="markers", unit="ms",
                    direction="lower", kind="deterministic", repeats=1, min_ok=1, margin=0,
                    timeout_s=30)
    kw = dict(run=RUN, task="t-001", contract=make_contract(metrics=(metric,)), session="s")
    gates.measure(env.conn, env.root, wt=wt, **kw)
    first = env.gate_events("t-001")[-1]["data"]
    wt = _rebuild(env, wt)
    gates.measure(env.conn, env.root, wt=wt, **kw)
    data = env.gate_events("t-001")[-1]["data"]
    assert data["input_sha256"] == first["input_sha256"]
    assert data["reused"] is False and data["ran"] is True
    assert os.path.exists(os.path.join(wt.path, "src", "runs.txt"))


@needs_jail
def test_a_test_side_that_wrote_its_tree_runs_again_on_a_rebuilt_tree(env):
    contract, _rep = _repair(env)
    spec = dataclasses.replace(contract.pass_to_pass, command=(
        RD + "echo x >> ${WORKTREE}/src/cache.txt; echo %s; echo \"TEST: s1 ... PASS $T\"; echo %s"
        % (BEGIN, ENDT)))
    contract = dataclasses.replace(contract, pass_to_pass=spec)
    kw = dict(run=RUN, task="t-001", contract=contract, item="pass-to-pass", side="attempt",
              session="s")
    wt = env.tree("t-001")
    first = gates.run_item(env.conn, env.root, wt=wt, **kw)
    wt = _rebuild(env, wt)
    again = gates.run_item(env.conn, env.root, wt=wt, **kw)
    assert again.input_sha256 == first.input_sha256
    assert again.reused is False and again.verdict == vc.PASS
    assert open(os.path.join(wt.path, "src", "cache.txt")).read() == "x\n"


@needs_jail
def test_an_unsearchable_folder_above_a_frozen_file_blocks_the_next_item(env):
    """Adversarial r7 A5a: a stage runs chmod 000 on the tree folder that holds a frozen file.
    The next item is BLOCKED and recorded, never a raise."""
    env.freeze("pack/tests.sh", "echo frozen\n")
    stages = (stage("build", "chmod 000 ${WORKTREE}/pack"), stage("unit", "sh tests.sh"))
    contract = make_contract(stages=stages, gates=tuple(GateStage(s["id"], 30) for s in stages))
    wt = env.tree("t-001", {"src/app.txt": "x\n", "pack/tests.sh": "echo tree\n"})
    try:
        res = _chain(env, contract, wt)
    finally:
        os.chmod(os.path.join(wt.path, "pack"), 0o755)
    evs = [e["data"] for e in env.gate_events("t-001")]
    assert [(d["stage"], d["verdict"]) for d in evs] == [("build", "PASS"), ("unit", "BLOCKED")]
    assert res.verdict == vc.BLOCKED and res.first_failure == "unit"
    # the tree refusal (check round 8 M2) comes before the overlay's own stat, which
    # test_an_overlay_path_the_host_cannot_stat_is_refused covers
    assert evs[1]["reason"] == "tree refused: pack: folder cannot be listed"


def test_an_overlay_path_the_host_cannot_stat_is_refused(env, tmp_path):
    if os.geteuid() == 0:
        pytest.skip("root stats a file under a mode 000 folder")
    env.freeze("pack/tests.sh", "echo frozen\n")
    wt = env.tree("t-001", {"pack/tests.sh": "echo tree\n"})
    cx = gates._Cx(env.conn, env.root, RUN, "t-001", make_contract(), "s")
    assert gates._overlays(cx, wt.path) == [(os.path.join(env.files, "pack", "tests.sh"),
                                             os.path.join(wt.path, "pack", "tests.sh"))]
    os.chmod(os.path.join(wt.path, "pack"), 0)
    try:
        with pytest.raises(gates._Refused):
            gates._overlays(cx, wt.path)
    finally:
        os.chmod(os.path.join(wt.path, "pack"), 0o755)


def _nest(top, levels, leaf="f"):
    """levels folders named a under top (made from descriptors, so no path grows), a file at the
    bottom; returns the bottom folder's descriptor."""
    flags = os.O_RDONLY | os.O_DIRECTORY
    fd = os.open(str(top), flags)
    for _ in range(levels):
        os.mkdir("a", dir_fd=fd)
        nxt = os.open("a", flags, dir_fd=fd)
        os.close(fd)
        fd = nxt
    wfd = os.open(leaf, os.O_WRONLY | os.O_CREAT, 0o600, dir_fd=fd)
    os.write(wfd, b"x")
    os.close(wfd)
    return fd


def test_judged_copy_refuses_evidence_deeper_than_its_bound(tmp_path):
    ok = tmp_path / "ok"
    ok.mkdir()
    os.close(_nest(ok, gates._MAX_EVIDENCE_DEPTH))
    got = gates.judged_copy(str(ok), str(tmp_path / "judged-ok"))
    assert list(got) == ["/".join(["a"] * gates._MAX_EVIDENCE_DEPTH + ["f"])]
    deep = tmp_path / "deep"
    deep.mkdir()
    os.close(_nest(deep, gates._MAX_EVIDENCE_DEPTH + 1))
    with pytest.raises(GateInfraError) as exc:
        gates.judged_copy(str(deep), str(tmp_path / "judged-deep"))
    assert exc.value.args == ("GATE-EVIDENCE-LINK",
                              ".: deeper than %d folders" % gates._MAX_EVIDENCE_DEPTH)
    assert not (tmp_path / "judged-deep").exists()


@needs_jail
def test_deep_evidence_is_recorded_as_blocked(env):
    """Adversarial r7 A5b: an evidence folder nested 1500 levels deep once raised RecursionError
    out of the chain; it is now one BLOCKED event with GATE-EVIDENCE-LINK."""
    cmd = ("python3 -c \"import os; os.chdir(os.environ['EVIDENCE_DIR'])\n"
           "for _ in range(1500):\n os.mkdir('a'); os.chdir('a')\"")
    res = _chain(env, _one_stage(cmd), env.tree("t-001"))
    evs = [e["data"] for e in env.gate_events("t-001")]
    assert len(evs) == 1 and evs[0]["verdict"] == "BLOCKED" and res.verdict == vc.BLOCKED
    assert evs[0]["code"] == "GATE-EVIDENCE-LINK"
    assert evs[0]["reason"] == ".: deeper than %d folders" % gates._MAX_EVIDENCE_DEPTH
    # the next run of the stage clears the deep evidence folder and runs
    again = _chain(env, _one_stage("true"), env.tree("t-001"))
    assert again.verdict == vc.PASS


@needs_jail
def test_a_deep_home_is_removed_and_its_run_recorded(env):
    """Adversarial r7 A5c: a home folder nested 1500 levels deep once raised RecursionError from
    the removal after the run; the run is now recorded and its home removed."""
    cmd = ("python3 -c \"import os; os.chdir(os.environ['HOME'])\n"
           "for _ in range(1500):\n os.mkdir('a'); os.chdir('a')\n"
           "os.chmod('..', 0)\"")
    res = _chain(env, _one_stage(cmd), env.tree("t-001"))
    evs = [e["data"] for e in env.gate_events("t-001")]
    assert len(evs) == 1 and evs[0]["verdict"] == "PASS" and res.verdict == vc.PASS
    assert "cleanup" not in evs[0]["repeats"][0]
    stage_dir = os.path.dirname(os.path.join(env.root, evs[0]["log"]))
    assert not os.path.lexists(paths.stage_home(stage_dir, evs[0]["tag"], 1))


@needs_jail
def test_a_home_that_cannot_be_removed_is_recorded_and_the_result_kept(env, monkeypatch):
    real = gates._remove

    def stuck(path):
        if os.path.basename(os.path.dirname(path)) == "home" and os.path.lexists(path):
            raise RecursionError("maximum recursion depth exceeded")
        return real(path)

    monkeypatch.setattr(gates, "_remove", stuck)
    res = _chain(env, _one_stage("exit 1"), env.tree("t-001"))
    evs = [e["data"] for e in env.gate_events("t-001")]
    assert len(evs) == 1 and evs[0]["verdict"] == "FAIL" and res.verdict == vc.FAIL
    assert evs[0]["repeats"][0]["cleanup"].endswith(
        "could not be removed: RecursionError: maximum recursion depth exceeded")


def test_removing_a_tree_opens_up_every_depth_and_never_follows_a_link(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    os.chmod(outside, 0o555)
    home = tmp_path / "home"
    (home / "d").mkdir(parents=True)
    os.symlink(str(outside), str(home / "d" / "link"))
    # deeper than both the recursion limit and the path length limit, with a mode 000 folder
    # that holds a file at the bottom
    fd = _nest(home / "d", 2100)
    try:
        os.mkdir("z", dir_fd=fd)
        zfd = os.open("z", os.O_RDONLY | os.O_DIRECTORY, dir_fd=fd)
        os.close(os.open("f", os.O_WRONLY | os.O_CREAT, 0o600, dir_fd=zfd))
        os.close(zfd)
        os.chmod("z", 0, dir_fd=fd)
    finally:
        os.close(fd)
    os.chmod(home / "d", 0)
    gates._remove(str(home))
    assert not os.path.lexists(home)
    assert outside.exists() and stat.S_IMODE(os.stat(outside).st_mode) == 0o555
    os.chmod(outside, 0o755)


def _user_xattrs(path):
    try:
        os.setxattr(path, "user.alpaca-probe", b"1")
        os.removexattr(path, "user.alpaca-probe")
    except OSError as exc:
        pytest.skip("no user xattrs here: %s" % exc)


def test_the_digest_covers_the_xattrs_of_files_and_folders(tmp_path):
    (tmp_path / "d").mkdir()
    (tmp_path / "d" / "f").write_text("x")
    _user_xattrs(str(tmp_path / "d" / "f"))
    plain = gates._dir_digest(str(tmp_path))
    for target in (tmp_path / "d" / "f", tmp_path / "d"):
        os.setxattr(str(target), "user.ok", b"1")
        one = gates._dir_digest(str(tmp_path))
        os.setxattr(str(target), "user.ok", b"0")
        zero = gates._dir_digest(str(tmp_path))
        assert len({plain, one, zero}) == 3, target
        assert gates._dir_digest(str(tmp_path)) == zero
        os.removexattr(str(target), "user.ok")
        assert gates._dir_digest(str(tmp_path)) == plain


@needs_jail
def test_a_changed_xattr_is_not_answered_with_a_reused_result(env):
    """Adversarial r7 A8: the stage exits 0 only while it reads user.ok=1."""
    wt = env.tree("t-001")
    f = os.path.join(wt.path, "src", "app.txt")
    _user_xattrs(f)
    os.setxattr(f, "user.ok", b"1")
    contract = _one_stage("python3 -c \"import os,sys; sys.exit(0 if os.getxattr('%s', "
                          "'user.ok') == b'1' else 1)\"" % f)
    assert _chain(env, contract, wt).verdict == vc.PASS
    assert _chain(env, contract, wt).results[0].reused is True
    os.setxattr(f, "user.ok", b"0")
    again = _chain(env, contract, wt)
    assert again.results[0].reused is False and again.verdict == vc.FAIL


@needs_jail
def test_every_run_and_event_records_the_hosts_ptrace_scope(env, tmp_path, monkeypatch):
    """Adversarial r7 M1: the results token holds only while Yama ptrace_scope is 1 or more, so
    the record says what the host had."""
    host = gates._ptrace_scope()
    if os.path.exists(gates._PTRACE_SCOPE):
        assert host == int(open(gates._PTRACE_SCOPE).read().strip())
    _chain(env, _one_stage("true"), env.tree("t-001"))
    data = env.gate_events("t-001")[-1]["data"]
    assert data["ptrace_scope"] == host and data["repeats"][0]["ptrace_scope"] == host
    fake = tmp_path / "scope"
    fake.write_text("0\n")
    monkeypatch.setattr(gates, "_PTRACE_SCOPE", str(fake))
    _chain(env, _one_stage("true"), env.tree("t-002"), task="t-002")
    data = env.gate_events("t-002")[-1]["data"]
    assert data["ptrace_scope"] == 0 and data["repeats"][0]["ptrace_scope"] == 0
    monkeypatch.setattr(gates, "_PTRACE_SCOPE", str(tmp_path / "missing"))
    _chain(env, _one_stage("true"), env.tree("t-003"), task="t-003")
    data = env.gate_events("t-003")[-1]["data"]
    assert data["ptrace_scope"] is None and data["repeats"][0]["ptrace_scope"] is None


def test_the_event_ptrace_scope_is_the_lowest_over_runs_that_ran():
    assert gates._ptrace([{"ran": True, "ptrace_scope": 2}, {"ran": True, "ptrace_scope": 1}]) == 1
    assert gates._ptrace([{"ran": True, "ptrace_scope": 1}, {"ran": True, "ptrace_scope": None}]) \
        is None
    assert gates._ptrace([{"ran": False, "ptrace_scope": None}]) is None
    assert gates._ptrace([]) is None


# ------------------------------------ check round 8: json-field attestation, unlistable folders
SHA_A = hashlib.sha256(b"a").hexdigest()
SHA_B = hashlib.sha256(b"b").hexdigest()


def _block(*lines):
    return "\n".join((BEGIN,) + lines + (END,)) + "\n"


def test_an_evidence_line_attests_its_path():
    assert markers._parse_evidence(_block("EVIDENCE: w.json " + SHA_A), "w.json") == SHA_A
    # both sides are normalized; lines for other paths are ignored; a repeat with the same hash
    # is not a conflict
    text = _block("EVIDENCE: ./sub/w.json " + SHA_A, "EVIDENCE: other.json " + SHA_B,
                  "EVIDENCE: sub/w.json " + SHA_A)
    assert markers._parse_evidence(text, "sub/./w.json") == SHA_A
    # the other marker parsers ignore an EVIDENCE line
    assert markers.parse_metric(_block("EVIDENCE: w.json " + SHA_A, "METRIC: m 3"), "m") == 3.0
    assert markers.parse_tests(_block("EVIDENCE: w.json " + SHA_A, "TEST: t ... PASS")).tests \
        == {"t": "PASS"}


@pytest.mark.parametrize("text, word", [
    ("no block at all\nEVIDENCE: w.json %s\n" % SHA_A, "markers-missing"),
    (_block("METRIC: m 1"), "markers-missing"),
    (_block("EVIDENCE: x.json " + SHA_A), "markers-missing"),
    (_block("EVIDENCE: w.json " + SHA_A, "EVIDENCE: w.json " + SHA_B), "markers-conflict"),
    (_block("EVIDENCE: w.json " + SHA_A.upper()), "markers-conflict"),
    (_block("EVIDENCE: w.json"), "markers-conflict"),
    (_block("EVIDENCE: w.json %s extra" % SHA_A), "markers-conflict"),
    # an unterminated prefix that hides the runner's own line behind tested code's output
    (_block("junkEVIDENCE: w.json " + SHA_A, "EVIDENCE: w.json " + SHA_B), "markers-conflict"),
], ids=["no-block", "no-line", "other-path", "two-hashes", "upper-hex", "no-hash", "extra-word",
        "hidden-prefix"])
def test_a_missing_or_bent_evidence_line_is_refused(text, word):
    with pytest.raises(MarkerError) as exc:
        markers._parse_evidence(text, "w.json")
    assert exc.value.args[0] == word


def _json_metric(**over):
    fields = dict(id="wall", command="true", parser="json-field", path="${EVIDENCE_DIR}/w.json",
                  field="wall", unit="ms", direction="lower", kind="deterministic", repeats=1,
                  min_ok=1, margin=1000, timeout_s=30)
    fields.update(over)
    return Metric(**fields)


@needs_jail
def test_an_unattested_json_field_file_in_the_evidence_folder_is_refused(env):
    """Without the runner's EVIDENCE line the judged file is not read (markers-missing): a bare
    write, and a token-closed block that attests nothing."""
    cases = (("t-001", "echo '{\"wall\": 500}' > ${EVIDENCE_DIR}/w.json",
              "markers-missing: no ALPACA-RESULTS-BEGIN line"),
             ("t-002", RD + "echo %s; echo '{\"wall\": 500}' > ${EVIDENCE_DIR}/w.json; echo %s"
              % (BEGIN, ENDT),
              "markers-missing: no EVIDENCE line for 'w.json' between the markers"))
    for task, command, reason in cases:
        contract = make_contract(metrics=(_json_metric(command=command),))
        res = _chain(env, contract, env.tree(task), task=task)
        assert res.verdict == vc.FAIL and res.metrics["wall"].repeats == (None,)
        data = env.gate_events(task)[-1]["data"]
        assert data["code"] == "GATE-MARKERS" and data["repeats"][0]["reason"] == reason


@needs_jail
def test_a_judged_json_file_that_differs_from_the_attestation_is_refused(env):
    """The runner attests {"wall": 500} and the file holds {"wall": 1}, as it does after a
    process tested code left behind swapped it: markers-conflict, and 1.0 is never recorded."""
    command = (RD + "echo %s; echo '{\"wall\": 1}' > ${EVIDENCE_DIR}/w.json; "
               "echo \"EVIDENCE: w.json $(echo '{\"wall\": 500}' | sha256sum | cut -c1-64) $T\"; "
               "echo %s" % (BEGIN, ENDT))
    contract = make_contract(metrics=(_json_metric(command=command),))
    res = _chain(env, contract, env.tree("t-001"))
    assert res.verdict == vc.FAIL and res.metrics["wall"].repeats == (None,)
    data = env.gate_events()[-1]["data"]
    assert data["code"] == "GATE-MARKERS"
    held = hashlib.sha256(b'{"wall": 1}\n').hexdigest()
    assert data["repeats"][0]["reason"] == (
        "markers-conflict: ${EVIDENCE_DIR}/w.json in the judged copy has sha256 %s, but the "
        "runner's block attests %s" % (held, hashlib.sha256(b'{"wall": 500}\n').hexdigest()))


# tested code that leaves a process in the jail which swaps the bench's JSON for {"wall": 1}
# once the bench has written it (adversarial r8 A2 and A2d)
JSON_PATCHER = r"""
E=${EVIDENCE_DIR}
( while :; do
    if grep -q 500 $E/w.json 2>/dev/null; then
      echo '{"wall": 1}' > $E/w.tmp && mv $E/w.tmp $E/w.json; fi
  done ) >/dev/null 2>&1 &
true
"""
ATTESTING_BENCH = (
    "import hashlib, json, os, subprocess\n"
    "fd = open(3, closefd=True)\n"
    "T = fd.readline().strip()\n"
    "fd.close()\n"
    "print('%s', flush=True)\n"
    "subprocess.run(['sh', os.environ['WORKTREE'] + '/src/solver.sh'], check=True)\n"
    "data = (json.dumps({'wall': 500}) + '\\n').encode()\n"
    "with open(os.environ['EVIDENCE_DIR'] + '/w.json', 'wb') as fh:\n"
    "    fh.write(data)\n"
    "print('EVIDENCE: w.json ' + hashlib.sha256(data).hexdigest() + ' ' + T, flush=True)\n"
    "print('%s ' + T, flush=True)\n" % (BEGIN, END))


@needs_jail
def test_a_background_process_cannot_forge_a_json_field_metric(env):
    """Adversarial r8 B1 (A2d): the frozen bench runs the tested solver in a child, writes its
    JSON and attests the bytes it wrote. The solver's background process swaps the file before
    the jail ends. Every repeat records 500 or is refused as markers-conflict; 1.0 never."""
    env.freeze("pack/bench.py", ATTESTING_BENCH)
    metric = _json_metric(command="python3 ${CONTRACT_DIR}/pack/bench.py", repeats=5)
    forged = env.tree("t-001", {"src/solver.sh": JSON_PATCHER, "pack/bench.py": "x\n"})
    res = _chain(env, make_contract(metrics=(metric,)), forged)
    got = res.metrics["wall"]
    assert 1.0 not in got.repeats, got
    assert set(got.repeats) <= {500.0, None}, got
    rows = env.gate_events()[-1]["data"]["repeats"]
    for row in rows:
        assert row["value"] == 500.0 or row["reason"].startswith(
            "markers-conflict: ${EVIDENCE_DIR}/w.json in the judged copy has sha256"), row
    # the same bench without the solver's patcher records 500 on every repeat
    honest = env.tree("t-002", {"src/solver.sh": "true\n", "pack/bench.py": "x\n"})
    assert _chain(env, make_contract(metrics=(metric,)), honest,
                  task="t-002").metrics["wall"].repeats == (500.0,) * 5


@needs_jail
def test_a_shell_bench_that_does_not_attest_cannot_be_forged(env):
    """Adversarial r8 A2: the shell bench writes its JSON and lingers; without an attestation
    the value is refused, so the patcher's 1.0 is never recorded."""
    env.freeze("pack/bench.sh", "sh ${WORKTREE}/src/solver.sh\n"
               "echo '{\"wall\": 500}' > ${EVIDENCE_DIR}/w.json\nsleep 0.2\n")
    metric = _json_metric(command="sh ${CONTRACT_DIR}/pack/bench.sh")
    forged = env.tree("t-001", {"src/solver.sh": JSON_PATCHER, "pack/bench.sh": "x\n"})
    res = _chain(env, make_contract(metrics=(metric,)), forged)
    assert res.metrics["wall"].repeats == (None,) and res.verdict == vc.FAIL


@needs_jail
def test_a_json_field_path_in_the_frozen_runbook_folder_needs_no_attestation(env):
    """No jail can write the frozen runbook folder, so a json-field path there is read as is."""
    env.freeze("pack/fixed.json", '{"wall": 9}\n')
    metric = _json_metric(path="fixed.json")
    res = _chain(env, make_contract(metrics=(metric,)), env.tree("t-001"))
    assert res.verdict == vc.PASS and res.metrics["wall"].repeats == (9.0,)


def test_a_tree_folder_the_host_cannot_list_is_refused(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("root lists a mode 000 folder")
    (tmp_path / "src" / "hid").mkdir(parents=True)
    (tmp_path / "src" / "hid" / "f").write_text("x")
    (tmp_path / "src" / "shut").mkdir()
    (tmp_path / "src" / "shut" / "g").write_text("y")
    assert gates._tree_specials(str(tmp_path)) == []
    os.chmod(tmp_path / "src" / "hid", 0)
    os.chmod(tmp_path / "src" / "shut", 0o444)     # listable, but its entries cannot be stat'ed
    try:
        assert gates._tree_specials(str(tmp_path)) == [
            "src/hid: folder cannot be listed", "src/shut/g: entry cannot be read"]
    finally:
        os.chmod(tmp_path / "src" / "hid", 0o700)
        os.chmod(tmp_path / "src" / "shut", 0o700)


@needs_jail
def test_a_host_socket_in_a_folder_the_host_cannot_list_is_never_reached(env, monkeypatch):
    """Adversarial r8 A7: a host listener at src/hid/s.sock with src/hid at mode 000. The stage
    would chmod the folder open and connect; the tree is refused before the jail starts."""
    if os.geteuid() == 0:
        pytest.skip("root lists a mode 000 folder")
    wt = env.tree("t-001")
    hidden = os.path.join(wt.path, "src", "hid")
    os.makedirs(hidden)
    listener = socket.socket(socket.AF_UNIX)
    monkeypatch.chdir(hidden)                   # a short path for bind
    listener.bind("s.sock")
    listener.listen(1)
    listener.settimeout(0.5)
    monkeypatch.chdir(wt.path)
    os.chmod(hidden, 0)
    probe = ("chmod 700 ${WORKTREE}/src/hid; cd ${WORKTREE}/src/hid; python3 -c \"import socket; "
             "u = socket.socket(socket.AF_UNIX); u.connect('s.sock'); u.send(b'hi')\"")
    try:
        res = _chain(env, _one_stage(probe), wt)
        with pytest.raises(OSError):
            listener.accept()
    finally:
        os.chmod(hidden, 0o700)
        listener.close()
    assert res.verdict == vc.BLOCKED
    data = env.gate_events()[-1]["data"]
    assert data["reason"] == "tree refused: src/hid: folder cannot be listed"
    assert data["ran"] is False and data["offline"] is None


# ------------------------------- check round 9: a long marker line, an undecodable json-field file
LONG_TEST_LINE = "TEST: x" + " " * 400000 + "y"


def _timed(fn, *args, n=3):
    """Best of `n` readings: a loaded host stalls one call, the quadratic parse slows every one."""
    took = []
    for _ in range(n):
        start = time.monotonic()
        with pytest.raises(MarkerError) as exc:
            fn(*args)
        took.append(time.monotonic() - start)
    return min(took), exc.value.args


def test_a_long_line_with_a_marker_token_is_refused_before_any_pattern_runs():
    """Adversarial r9 B1: one TEST line with a long run of spaces made the host's parse take
    quadratic time. A line inside the block that holds a token and is longer than
    _MAX_MARKER_LINE is a conflict at once, for every parser the gate chain calls."""
    for line in (LONG_TEST_LINE, "TEST: x" + " " * (32 * 1024 * 1024) + "y",
                 "METRIC: speed " + "1" * 400000, "EVIDENCE: " + "p" * 400000 + " " + SHA_A):
        text = _block(line, "METRIC: speed 500", "EVIDENCE: w.json " + SHA_A)
        for fn, args in ((markers.parse_metric, (text, "speed")), (markers.parse_tests, (text,)),
                         (markers._parse_evidence, (text, "w.json"))):
            took, args_ = _timed(fn, *args)
            assert took < 1.0, (fn.__name__, took)
            assert args_[0] == "markers-conflict"
            assert "holds a marker token and is %d characters long, over the limit of %d" % (
                len(line), markers._MAX_MARKER_LINE) in args_[1]
    # the longest name a report may hold still fits under the line limit
    name = "n" * markers._MAX_NAME
    assert markers.parse_tests(_block("TEST: %s ... PASS" % name)).tests == {name: "PASS"}
    # a long line that holds no token is build output and is still ignored
    assert markers.parse_metric(_block("x" * 400000, "METRIC: speed 5"), "speed") == 5.0


def test_the_test_line_reader_is_linear_in_the_line_length():
    """Adversarial r9 B1: the TEST reader itself never backtracks, so a line under any limit is
    read in linear time; the 400000-character line took 47.8 s with the old pattern."""
    took = []
    for _ in range(3):                                  # best of 3, as _timed
        start = time.monotonic()
        assert markers._test_line(LONG_TEST_LINE) is None
        assert markers._test_line("TEST: x" + " " * 400000 + "... PASS") == ("x", "PASS")
        assert markers._test_line("TEST: " + "a " * 200000 + " ... FAIL") == (
            ("a " * 200000).rstrip(), "FAIL")
        took.append(time.monotonic() - start)
    assert min(took) < 1.0, took


@pytest.mark.parametrize("line, want", [
    ("TEST: a ... PASS", ("a", "PASS")),
    ("TEST:\ta b\t...  \tSKIP", ("a b", "SKIP")),
    ("TEST: a ... b ... FAIL", ("a ... b", "FAIL")),
    ("TEST: x... ... PASS", ("x...", "PASS")),
    ("TEST: a....PASS", None),
    ("TEST: a .... PASS", None),
    ("TEST: a ...PASS", None),
    ("TEST: a... PASS", None),
    ("TEST:a ... PASS", None),
    ("TEST: ... PASS", None),
    ("TEST: a ... PASSED", None),
    ("TEST: a ... xPASS", None),
    ("TEST: \u00a0a ... PASS", None),
    ("METRIC: a ... PASS", None),
])
def test_the_test_line_reader_keeps_the_grammar(line, want):
    assert markers._test_line(line) == want


@needs_jail
def test_one_long_line_from_tested_code_is_a_quick_conflict_in_the_jail(env):
    """Adversarial r9 A5a: the solver prints one 400000-character TEST line inside the bench's
    block. The item records FAIL GATE-MARKERS with the length reason, and the host's parse of
    the recorded log is quick."""
    env.freeze("pack/bench.sh", RD + "echo %s\nsh ${WORKTREE}/src/solver.sh\n"
               "echo METRIC: speed 500 $T\necho %s\n" % (BEGIN, ENDT))
    metric = Metric(id="speed", command="sh ${CONTRACT_DIR}/pack/bench.sh", parser="markers",
                    unit="ms", direction="lower", kind="deterministic", repeats=1, min_ok=1,
                    margin=0, timeout_s=60)
    solver = "python3 -c \"import sys; sys.stdout.write('TEST: x' + ' ' * 400000 + 'y\\n')\"\n"
    wt = env.tree("t-001", {"src/solver.sh": solver})
    res = _chain(env, make_contract(metrics=(metric,)), wt)
    assert res.verdict == vc.FAIL and res.metrics["speed"].repeats == (None,)
    events_ = env.gate_events()
    assert len(events_) == 1
    data = events_[0]["data"]
    assert data["code"] == "GATE-MARKERS"
    assert re.match(r"markers-conflict: log line \d+ holds a marker token and is 400008 "
                    r"characters long, over the limit", data["repeats"][0]["reason"])
    text = open(os.path.join(env.root, data["log"])).read()
    text = "\n".join(END if line.startswith(END + " ") else line for line in text.split("\n"))
    took, args_ = _timed(markers.parse_metric, text, "speed")
    assert took < 1.0 and args_[0] == "markers-conflict"


def _attested_run(tmp_path, body: bytes) -> dict:
    """A run dict as the gate chain holds it: a judged copy with w.json and a log whose
    token-closed block attests those bytes."""
    judged = tmp_path / "judged"
    judged.mkdir()
    (judged / "w.json").write_bytes(body)
    log = tmp_path / "run.log"
    log.write_text("%s\nEVIDENCE: w.json %s tok\n%s tok\n"
                   % (BEGIN, hashlib.sha256(body).hexdigest(), END))
    return {"judged": str(judged), "log": str(log), "token": "tok", "values": {}}


@pytest.mark.parametrize("body, reason", [
    (b'{"wall": ' + b"[" * 200000 + b"]" * 200000 + b"}",
     "cannot read ${EVIDENCE_DIR}/w.json as JSON: "),
    (b'{"wall": 1' + b"0" * 4000 + b"}",
     "${EVIDENCE_DIR}/w.json wall is not a finite number"),
    (b'{"wall": 1e999}', "${EVIDENCE_DIR}/w.json wall is not a finite number"),
    (b'{"wall": 12}', None),
], ids=["deep-nesting", "huge-int", "infinite", "plain"])
def test_an_attested_json_field_file_that_cannot_be_decoded_is_unreadable(tmp_path, body, reason):
    """Adversarial r9 B2: json.loads raised RecursionError on deep nesting, and math.isfinite
    raises OverflowError on an integer too large for a float. Both are an unreadable repeat."""
    run = _attested_run(tmp_path, body)
    if reason is None:
        assert gates._metric_value(None, _json_metric(), run) == 12.0
        return
    with pytest.raises(gates._Unreadable) as exc:
        gates._metric_value(None, _json_metric(), run)
    assert str(exc.value).startswith(reason)


@needs_jail
def test_a_deeply_nested_attested_json_value_records_one_event(env):
    """Adversarial r9 A5b: an honest bench writes the solver's output to w.json and attests
    it; the solver prints a value nested 200000 deep. run_chain raises nothing and the item
    records one event whose repeat carries the reason."""
    env.freeze("pack/bench.sh",
               RD + "echo %s\nj=$(sh ${WORKTREE}/src/solver.sh)\n"
               "printf '%%s\\n' \"$j\" > ${EVIDENCE_DIR}/w.json\n"
               "echo \"EVIDENCE: w.json $(printf '%%s\\n' \"$j\" | sha256sum | cut -c1-64) $T\"\n"
               "echo %s\n" % (BEGIN, ENDT))
    metric = _json_metric(command="sh ${CONTRACT_DIR}/pack/bench.sh", timeout_s=60)
    solver = "python3 -c \"print('{\\\"wall\\\": ' + '[' * 200000 + ']' * 200000 + '}')\"\n"
    wt = env.tree("t-001", {"src/solver.sh": solver})
    res = _chain(env, make_contract(metrics=(metric,)), wt)
    assert res.verdict == vc.FAIL and res.metrics["wall"].repeats == (None,)
    events_ = env.gate_events("t-001")
    assert len(events_) == 1
    assert events_[0]["data"]["repeats"][0]["reason"].startswith(
        "cannot read ${EVIDENCE_DIR}/w.json as JSON: ")


# the design names and signatures of t-093's modules (architecture.md section 3.3), moved here
# from test_research_scaffold.py's stub tables once the modules were built (t-098's convention)
DESIGN_NAMES = {
    "gates": {
        "chain": "(contract: 'Contract', *, repair_case: 'str | None' = None) -> 'tuple[str, ...]'",
        "run_chain": ("(conn, root: 'str', *, run: 'str', task: 'str', contract: 'Contract', "
                      "wt: 'WorktreeInfo', mode: 'str', session: 'str', side: 'str' = 'attempt', "
                      "stages: 'tuple | None' = None, repair_case: 'str | None' = None, "
                      "base: 'WorktreeInfo | None' = None, "
                      "baseline: 'PassToPass | None' = None) -> 'ChainResult'"),
        "input_fingerprint": ("(*, tree: 'str', contract_sha256: 'str', files_sha256: 'str', "
                              "tools_sha256: 'str', stage: 'str') -> 'str'"),
        "judged_copy": "(src: 'str', dest: 'str') -> 'dict'",
        "run_item": ("(conn, root: 'str', *, run: 'str', task: 'str', contract: 'Contract', "
                     "item: 'str', wt: 'WorktreeInfo', side: 'str', session: 'str', "
                     "reproducer_dir: 'str | None' = None, "
                     "timeout_s: 'float | None' = None) -> 'GateResult'"),
        "run_prepare": ("(conn, root: 'str', *, run: 'str', task: 'str', contract: 'Contract', "
                        "wt: 'WorktreeInfo', session: 'str') -> 'GateResult'"),
        "run_holdout_stage": ("(conn, root: 'str', *, run: 'str', contract: 'Contract', "
                              "stage: 'str', wt: 'WorktreeInfo', session: 'str') -> "
                              "'GateResult'"),
        "fail_to_pass": ("(conn, root: 'str', *, run: 'str', task: 'str', "
                         "contract: 'Contract', case: 'str', base: 'WorktreeInfo', "
                         "attempt: 'WorktreeInfo', session: 'str') -> 'FailToPass'"),
        "pass_to_pass": ("(conn, root: 'str', *, run: 'str', task: 'str', "
                         "contract: 'Contract', base: 'WorktreeInfo', attempt: 'WorktreeInfo', "
                         "baseline: 'tuple', session: 'str') -> 'PassToPass'"),
        "verify_rerun": ("(conn, root: 'str', *, run: 'str', task: 'str', "
                         "contract: 'Contract', request: 'dict', trees: 'dict', "
                         "session: 'str') -> 'dict'"),
        "measure": ("(conn, root: 'str', *, run: 'str', task: 'str', contract: 'Contract', "
                    "wt: 'WorktreeInfo', session: 'str') -> 'dict[str, MetricValue]'"),
    },
    "locks": {
        "machine_lock": ("(name: 'str', *, capacity: 'int' = 1, timeout_s: 'float', "
                         "poll_s: 'float' = 0.5)"),
        "heavy": "(stage: 'str', root: 'str', *, timeout_s: 'float')",
        "LockHold": None,
    },
    "markers": {
        "parse_tests": "(text: 'str') -> 'TestReport'",
        "parse_metric": "(text: 'str', metric_id: 'str') -> 'float'",
    },
}


@pytest.mark.parametrize("module", sorted(DESIGN_NAMES))
def test_the_built_modules_keep_the_design_names_and_signatures(module):
    import importlib
    import inspect
    import re
    mod = importlib.import_module("alpaca.research." + module)
    own = set()
    for name, obj in vars(mod).items():
        if name.startswith("_"):
            continue
        if inspect.isfunction(obj) or inspect.isclass(obj):
            if obj.__module__ == mod.__name__:
                own.add(name)
        elif re.match(r"^[A-Z][A-Z0-9_]*$", name) and not inspect.ismodule(obj):
            own.add(name)
    assert own == set(DESIGN_NAMES[module]), sorted(own ^ set(DESIGN_NAMES[module]))
    for name, shape in DESIGN_NAMES[module].items():
        if shape is not None:
            assert str(inspect.signature(getattr(mod, name))) == shape, name



# ------------------------------------------------ check round 11: token on every marker line
@pytest.mark.parametrize("line", [
    "TEST: a ... PASS", "METRIC: speed 1", "EVIDENCE: w.json " + "a" * 64,
    "TEST: a ... PASS tok999", "METRIC: speed 1 tok999", "TEST: a ... PASStok123",
    "TEST: a ... PASS tok123 x", "tok123 TEST: a ... PASS", "noise TEST: a ... PASS",
])
def test_every_marker_line_in_the_block_must_end_with_the_runs_token(line):
    """Adversarial r11 B2: a line that holds TEST:, METRIC: or EVIDENCE: inside the block counts
    only when its last word is the run's token after a space or a tab; any other shape is a
    conflict that names the token."""
    good = ["TEST: b ... FAIL tok123", "METRIC: speed 5 tok123",
            "EVIDENCE: w.json %s tok123" % ("b" * 64)]
    log = "\n".join([BEGIN] + good[:1] + [line] + good[1:] + [END + " tok123"])
    with pytest.raises(MarkerError) as exc:
        markers._with_token(log, "tok123")
    assert exc.value.args[0] == "markers-conflict" and "token" in exc.value.args[1], line


def test_the_token_is_stripped_and_lines_outside_the_block_need_none():
    log = "\n".join(["TEST: before ... PASS", "METRIC: speed 9", BEGIN, "noise",
                     "TEST: a b ... PASS tok123", "  METRIC: speed 5\ttok123  \r",
                     "EVIDENCE: w.json %s tok123" % ("c" * 64), "TEST: c ... SKIP \t tok123",
                     END + " tok123", "TEST: after ... FAIL", "METRIC: speed 1"])
    text = markers._with_token(log, "tok123")
    assert markers.parse_tests(text).tests == {"a b": "PASS", "c": "SKIP"}
    assert markers.parse_metric(text, "speed") == 5.0
    assert markers._parse_evidence(text, "w.json") == "c" * 64
    # an over-long marker line is left for _blocks, which refuses it for its length
    long_line = "TEST: x" + " " * (markers._MAX_MARKER_LINE + 10) + "y"
    with pytest.raises(MarkerError) as exc:
        markers.parse_tests(markers._with_token(
            "\n".join([BEGIN, long_line, END + " tok123"]), "tok123"))
    assert "over the limit" in exc.value.args[1]


def _child_reproducer(env):
    """A repair contract whose frozen reproducer keeps every stated rule: it reads and closes
    fd 3 first, runs the tested code in a child (test_bug.py imports src/app.py of the tree),
    prints t_bug's line only from the child's RESULT line, and closes its block with the token.
    The child shares the runner's stdout and stderr."""
    contract, rep = _repair(env)
    write(os.path.join(rep, "run.sh"),
          RD + "echo %s\n"
          "out=$(python3 ${REPRODUCER_DIR}/test_bug.py)\n"
          "case \"$out\" in *'RESULT t_bug ok'*) echo \"TEST: t_bug ... PASS $T\";;"
          " *'RESULT t_bug fail'*) echo \"TEST: t_bug ... FAIL $T\";; esac\n"
          "echo %s\n" % (BEGIN, ENDT))
    write(os.path.join(rep, "test_bug.py"),
          "import os, sys\nsys.path.insert(0, os.environ['WORKTREE'] + '/src')\n"
          "import app\nprint('RESULT t_bug ' + ('ok' if app.fixed() else 'fail'))\n")
    events.emit(env.conn, events.REPRODUCER, run=RUN, ref="t-090", op=None, session="s",
                code=vc.PASS, data={"case": "bug", "proved": True, "tests": ["t_bug"],
                                    "files": {n: gates._sha256_file(os.path.join(rep, n))
                                              for n in ("run.sh", "test_bug.py")}})
    contract = dataclasses.replace(contract, repair=(dataclasses.replace(
        contract.repair[0], fail_to_pass=dataclasses.replace(
            contract.repair[0].fail_to_pass, command="sh ${REPRODUCER_DIR}/run.sh")),))
    base = env.tree("t-001-base", {"src/app.py": "def fixed():\n    return False\n"})
    return contract, base


@needs_jail
def test_a_child_that_prints_a_named_test_line_does_not_pass_fail_to_pass(env):
    """Adversarial r11 A9: the attempt's src/app.py writes `TEST: t_bug ... PASS` to the stderr
    it shares with the runner and exits before the runner sees a RESULT line, so the runner
    prints nothing for t_bug. The child's line has no token: the attempt side is a conflict and
    the non-fix does not pass."""
    contract, base = _child_reproducer(env)
    cheat = env.tree("t-001", {"src/app.py": "import os, sys\n"
                                             "sys.stderr.write('TEST: t_bug ... PASS\\n')\n"
                                             "sys.stderr.flush()\nos._exit(0)\n"})
    got = gates.fail_to_pass(env.conn, env.root, run=RUN, task="t-001", contract=contract,
                             case="bug", base=base, attempt=cheat, session="s")
    data = env.gate_events("t-001")[-1]["data"]
    assert got.ok is False and got.base == {"t_bug": "FAIL"} and got.attempt == {}
    assert data["side"] == "attempt" and data["code"] == "GATE-MARKERS"
    assert data["reason"].startswith("markers-conflict") and "token" in data["reason"]
    log = open(os.path.join(env.root, data["log"])).read()
    assert "TEST: t_bug ... PASS\n" in log
    # negative: the real fix passes through the same runner and child
    real = env.tree("t-002", {"src/app.py": "def fixed():\n    return True\n"})
    assert gates.fail_to_pass(env.conn, env.root, run=RUN, task="t-002", contract=contract,
                              case="bug", base=base, attempt=real, session="s").ok is True


@needs_jail
def test_a_child_that_prints_a_metric_line_and_fails_gets_no_metric(env):
    """Adversarial r11 A9m: the bench prints METRIC only when its timed child exits 0. The
    cheat writes `METRIC: speed 1` to the stderr it shares with the bench and exits 1. Its line
    has no token, so no repeat reads 1 and the metric has no median."""
    env.freeze("pack/bench.sh", RD + "echo %s\n"
               "if v=$(python3 ${WORKTREE}/src/solver.py); then echo \"METRIC: speed $v $T\"; "
               "fi\necho %s\n" % (BEGIN, ENDT))
    metric = Metric(id="speed", command="sh ${CONTRACT_DIR}/pack/bench.sh", parser="markers",
                    unit="ms", direction="lower", kind="noisy", repeats=3, min_ok=3, margin=0,
                    timeout_s=30)
    contract = make_contract(metrics=(metric,))
    cheat = env.tree("t-001", {"src/solver.py": "import os, sys\n"
                                                "sys.stderr.write('METRIC: speed 1\\n')\n"
                                                "sys.stderr.flush()\nos._exit(1)\n"})
    res = _chain(env, contract, cheat)
    value = res.metrics["speed"]
    data = env.gate_events("t-001")[-1]["data"]
    assert res.verdict == vc.FAIL and value.median is None and 1.0 not in value.repeats
    assert data["code"] == "GATE-MARKERS"
    assert all(r["reason"].startswith("markers-conflict") and "token" in r["reason"]
               for r in data["repeats"])
    # negative: the honest solver is measured by the bench's own line
    honest = env.tree("t-002", {"src/solver.py": "print(500)\n"})
    assert _chain(env, contract, honest, task="t-002").metrics["speed"].median == 500.0


# ------------------------------------------------ check round 11: the digest is bounded
def _run_in_thread(fn, limit):
    """(finished, seconds, result or exception) of fn run in a daemon thread for at most limit
    seconds."""
    done = {}

    def go():
        try:
            done["r"] = fn()
        except Exception as exc:          # noqa: BLE001 - the test reports it
            done["r"] = exc
    th = threading.Thread(target=go, daemon=True)
    t0 = time.monotonic()
    th.start()
    th.join(limit)
    return not th.is_alive(), time.monotonic() - t0, done.get("r")


@needs_jail
@pytest.mark.parametrize("where", ["stage", "metric"])
def test_a_sparse_petabyte_file_in_the_tree_records_its_event_at_once(env, where):
    """Adversarial r10 B1 and r11 B1: the command leaves a sparse file of 1 PiB in its tree.
    The host's digest after the run does not read it (its st_size passes the byte bound), so
    the item records its one event within seconds and is never reused."""
    make = "truncate -s 1P ${WORKTREE}/big"
    if where == "stage":
        contract = make_contract(stages=(stage("build", make),),
                                 gates=(GateStage("build", 30),))
    else:
        contract = make_contract(metrics=(Metric(
            id="speed", command=RD + make + "; echo %s; echo METRIC: speed 5 $T; echo %s"
            % (BEGIN, ENDT), parser="markers", unit="ms", direction="lower",
            kind="deterministic", repeats=1, min_ok=1, margin=0, timeout_s=30),))
    wt = env.tree("t-001")

    def chain():
        conn = db.connect(env.root)      # a connection of the thread that runs the chain
        return gates.run_chain(conn, env.root, run=RUN, task="t-001", contract=contract,
                               wt=wt, mode="improve", session="s")
    try:
        finished, took, res = _run_in_thread(chain, 60)
        assert finished and took < 20, (finished, took)
    finally:
        with contextlib.suppress(OSError):
            os.truncate(os.path.join(wt.path, "big"), 0)
    assert not isinstance(res, Exception), res
    assert os.stat(os.path.join(wt.path, "big")).st_size in (0, 1 << 50)
    (ev,) = env.gate_events("t-001")
    data = ev["data"]
    assert res.verdict == vc.PASS and data["ran"] is True and data["reused"] is False
    assert data["tree_sha256"] != data["tree_after_sha256"]


def test_the_tree_digest_reads_a_bounded_number_of_bytes(tmp_path, monkeypatch):
    tree = tmp_path / "tree"
    (tree / "src").mkdir(parents=True)
    (tree / "src" / "a.txt").write_bytes(b"x" * 600)
    (tree / "src" / "b.txt").write_bytes(b"y" * 300)
    monkeypatch.setattr(gates, "_MAX_DIGEST_BYTES", 1000)
    read = []
    real = gates._sha256_file
    monkeypatch.setattr(gates, "_sha256_file", lambda p, limit=None: read.append(
        os.path.basename(p)) or real(p, limit))
    # under the bound: stable, and every file is read
    assert gates._dir_digest(str(tree)) == gates._dir_digest(str(tree))
    assert sorted(read) == ["a.txt", "a.txt", "b.txt", "b.txt"]
    # past the bound: the file that would pass it is a gap and is never opened
    (tree / "src" / "c.txt").write_bytes(b"z" * 200)
    read.clear()
    assert gates._dir_digest(str(tree)) != gates._dir_digest(str(tree))
    assert "c.txt" not in read
    # unbounded (the frozen-file check) reads everything and stays stable
    assert gates._dir_digest(str(tree), bounded=False) == gates._dir_digest(str(tree),
                                                                            bounded=False)
    assert read.count("c.txt") == 2
    # a sparse file of 1 PiB costs one stat, not a read
    os.truncate(str(tree / "src" / "c.txt"), 0)
    big = tree / "big"
    big.touch()
    os.truncate(str(big), 1 << 50)
    monkeypatch.setattr(gates, "_MAX_DIGEST_BYTES", 1 << 30)
    finished, took, _ = _run_in_thread(lambda: gates._dir_digest(str(tree)), 30)
    os.truncate(str(big), 0)
    assert finished and took < 5, (finished, took)
    # a read that goes past the limit stops there
    assert real(str(tree / "src" / "a.txt"), 599) is None
    assert real(str(tree / "src" / "a.txt"), 600) == hashlib.sha256(b"x" * 600).hexdigest()
    # and so does a file whose st_size understates what a read gives (a /proc file says 0)
    assert os.stat("/proc/self/status").st_size == 0
    assert real("/proc/self/status", 16) is None and real("/proc/self/status") is not None


def test_the_tree_digest_walks_a_bounded_number_of_entries(tmp_path, monkeypatch):
    tree = tmp_path / "tree"
    tree.mkdir()
    for i in range(3):
        (tree / ("f%d" % i)).write_text("x")
    monkeypatch.setattr(gates, "_MAX_DIGEST_ENTRIES", 6)
    assert gates._dir_digest(str(tree)) == gates._dir_digest(str(tree))
    for i in range(3, 10):
        (tree / ("f%d" % i)).write_text("x")
    listed = []
    real = os.lstat
    monkeypatch.setattr(gates.os, "lstat", lambda p, *a, **k: listed.append(p) or real(p, *a, **k))
    assert gates._dir_digest(str(tree)) != gates._dir_digest(str(tree))
    assert len(listed) <= 2 * 6
    monkeypatch.setattr(gates.os, "lstat", real)
    assert gates._dir_digest(str(tree), bounded=False) == gates._dir_digest(str(tree),
                                                                            bounded=False)


@needs_jail
def test_frozen_files_past_the_byte_bound_do_not_read_as_changed(env, monkeypatch):
    """The frozen-file check digests without the bounds, so frozen files larger than the byte
    bound do not read as a change while the item ran; the input fingerprint is bounded, so the
    same item on the same tree is run again rather than reused."""
    monkeypatch.setattr(gates, "_MAX_DIGEST_BYTES", 64)
    env.freeze("pack/data.txt", "x" * 1000)
    contract = _one_stage("true")
    wt = env.tree("t-001")
    first = _chain(env, contract, wt)
    second = _chain(env, contract, wt)
    assert first.verdict == vc.PASS and second.verdict == vc.PASS
    events_ = env.gate_events("t-001")
    assert len(events_) == 2 and [e["data"]["reused"] for e in events_] == [False, False]
    assert "changed while the item ran" not in (events_[0]["data"].get("reason") or "")
