"""The gaps the rr-002 rehearsal found (t-173 fix batch 3, items F14 to F23; the evidence is in
.alpaca/proofs/op-009/t-173.logs/gap-research/FIX-BATCH-3.md and live-round/rehearsal/).

rr-002 was a noop control run driven through the real Agent tool. Its four auditor jobs were
accepted; every verifier job failed: each gate re-run came back BLOCKED (F15), the verifiers' own
read-only shell forms voided their passes (F16), K16 refused honest pairs for verdict lines both
wrote and for an off-list call (F19), the refusal rows held only a code (F20), a driver that
submitted one verifier at a time was held until the sibling was claimed (F18), the metric claim
named a gate item the chain does not hold (F17), the auditor prompt had no row for rule S15 (F14),
and the token cap counted cache reads (F22). Each test builds the case the rehearsal met and
asserts the fix; each fails when its fix is taken out. The shell texts below are the real
verifiers' own commands, with their try folder written as {f}.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import re
import threading
import time

import pytest

from alpaca import db
from alpaca.research import (agent_transcript as at, audit, claims, gates, jobs, pathscan,
                             session_runtime, verifier)
from alpaca.tests import research_session_kit as rk
from alpaca.tests.conftest import REPO
from alpaca.tests.devfiles import require_dev_files


@pytest.fixture
def world(project, monkeypatch):
    w = rk.JudgeWorld(monkeypatch, project)
    yield w
    w.close()


def _folder(w, slot, try_n=0):
    path = os.path.join(w.root, ".alpaca", "verify", w.attempt, "vf-x", slot, "r0", "t%d" % try_n)
    os.makedirs(os.path.join(path, "out"), exist_ok=True)
    os.makedirs(os.path.join(path, "receipts"), exist_ok=True)
    os.makedirs(os.path.join(path, "packet"), exist_ok=True)
    return path


def _pair(w, group="vf-x/r0"):
    j1 = w.issue("verifier", key="%s/v1/r0/t0" % w.attempt, group=group, group_size=2,
                 verifier_slot="v1", first_pass=True, scanned_cwd=_folder(w, "v1"))
    j2 = w.issue("verifier", key="%s/v2/r0/t0" % w.attempt, group=group, group_size=2,
                 verifier_slot="v2", first_pass=True, scanned_cwd=_folder(w, "v2"))
    handed = w.hand()
    assert {e["job"] for e in handed} == {j1, j2}
    return j1, j2


def _overlapping(w, j1, j2, b1, b2):
    a1, a2 = w.agent(j1, behavior=b1), w.agent(j2, behavior=b2)
    a1.start()
    a2.start()
    for a in (a1, a2):
        a.work()
        a.write()
    a1.stop()
    a2.stop()
    return a1, a2


def _barrier_run(w, j1, j2, agents, group="vf-x/r0", barrier=None):
    """The judge threads of the pair at one barrier, as the runner's slot threads meet."""
    barrier = barrier or session_runtime.GroupBarrier(w.SCOPE, group)
    barrier.add_member(j1, 2)
    barrier.add_member(j2, 2)
    claims_ = {jid: w.claim(jid, agent) for jid, agent in agents}
    out = {}

    def judge(jid):
        conn = db.connect(w.root)
        try:
            c = w.ctx_for(jid, claims_[jid], conn=conn, barrier=barrier)
            try:
                jobs.run_checks(c)
                out[jid] = None
            except jobs.Problem as exc:
                out[jid] = exc
        except BaseException as exc:      # noqa: BLE001 - shown by the assertion of the test
            out[jid] = exc
        finally:
            barrier.leave(jid)
            conn.close()
    ths = [threading.Thread(target=judge, args=(j,)) for j, _a in agents]
    for t in ths:
        t.start()
    for t in ths:
        t.join(60)
    for jid, p in out.items():
        assert p is None or isinstance(p, jobs.Problem), (jid, repr(p))
    return out, claims_


def _bash(cwd, command, output="ok"):
    return ("Bash", {"command": "cd %s && %s" % (cwd, command)}, output)


# ================================================================ F14: rule S15 in the prompt table
def _prompt_rules():
    with open(os.path.join(os.path.dirname(audit.__file__), "prompts", "audit.md"),
              encoding="utf-8") as fh:
        text = fh.read()
    return {m.group(1): m.group(2) for m in re.finditer(r"^\| (S[0-9]+) \| (yes|no) \| .+ \|$",
                                                         text, re.MULTILINE)}, text


def test_f14_every_rule_a_brief_can_name_has_a_row_in_the_auditor_prompt():
    rows, text = _prompt_rules()
    # a brief names its candidates' rules (audit._RULES) and lists BINDING_RULES as
    # binding_rules: each needs a row, and the row's binding column is the engine's
    assert set(audit._RULES) <= set(rows), sorted(set(audit._RULES) - set(rows))
    assert set(audit.BINDING_RULES) <= set(rows)
    for rule in audit._RULES:
        assert rows[rule] == ("yes" if rule in audit.BINDING_RULES else "no"), rule
    s15 = [ln for ln in text.splitlines() if ln.startswith("| S15 |")][0]
    assert "control plane" in s15 and ".alpaca/alpaca.db" in s15 and "bin/alpaca" in s15
    assert text.isascii()


# ================================================================ F15: a gate re-run through serve
@pytest.mark.skipif(not __import__("alpaca.research.jail", fromlist=["available"]).available()[0],
                    reason="bubblewrap is not available on this host")
def test_f15_a_gate_rerun_through_serve_runs_on_the_try_trees(project):
    """verifier.serve holds the try's trees as folder paths ({"folder", "tree", "base"}, as
    _launch_one hands them, for the session runtime and the headless path alike), and a `--gate`
    request runs the frozen stage on the requested side and answers with its rc and log. Before
    the fix gates.verify_rerun took only WorktreeInfo, and every request of rr-002 came back
    BLOCKED "no tree tree was given for the re-run" (events 77676 to 77738)."""
    from alpaca.tests import test_research_gates as tg
    env = tg.Env(project)
    contract = tg.make_contract(
        stages=(tg.stage("unit", "cat ${WORKTREE}/src/app.txt; grep -q fixed ${WORKTREE}/src/app.txt"),),
        gates=(tg.GateStage("unit", 30),))
    top, tree, base = tg._try_trees(env, "s1", {"src/app.txt": "fixed\n"}, {"src/app.txt": "bug\n"})
    requests = os.path.join(top, "requests")
    os.makedirs(requests, exist_ok=True)
    for n, (rid, side) in enumerate((("g-tree", "tree"), ("g-base", "base")), 1):
        with open(os.path.join(requests, "%d.json" % n), "w", encoding="utf-8") as fh:
            json.dump({"argv": [rid, "--gate", "unit", "--side", side]}, fh)
    stop, ledger = threading.Event(), {}
    trees = {"folder": top, "tree": tree.path, "base": base.path}
    th = threading.Thread(target=verifier.serve, args=(None, project, "vf-t-001-1", "v1", 1, 1),
                          kwargs={"trees": trees, "contract": contract, "stop": stop,
                                  "ledger": ledger, "deadline": time.monotonic() + 600,
                                  "brief": {"engine": {"run": tg.RUN, "stages": ["unit"]}},
                                  "task": "t-001"}, daemon=True)
    th.start()
    end = time.monotonic() + 120
    while time.monotonic() < end and len(ledger.get("requests", {})) < 2:
        time.sleep(0.1)
    stop.set()
    th.join(30)
    receipts = os.path.join(top, "receipts")
    tree_rec = json.load(open(os.path.join(receipts, "g-tree.json"), encoding="utf-8"))
    base_rec = json.load(open(os.path.join(receipts, "g-base.json"), encoding="utf-8"))
    assert tree_rec["kind"] == "gate" and tree_rec["stage"] == "unit" and tree_rec["side"] == "tree"
    assert (tree_rec["rc"], tree_rec["verdict"]) == (0, "PASS"), tree_rec
    assert tree_rec["log"] and tree_rec["log"].startswith(os.path.join(top, "gates") + os.sep)
    assert base_rec["verdict"] == "FAIL" and base_rec["rc"] not in (None, 0), base_rec
    rows = [e for e in env.gate_events("t-001") if e["data"].get("side") == "verify"]
    assert [r["data"]["verdict"] for r in rows] == ["PASS", "FAIL"], rows
    assert not any(r["data"].get("refused") for r in rows)


def test_f15_verify_rerun_takes_a_folder_path_and_refuses_what_is_no_folder(project, tmp_path):
    folder = tmp_path / "t0" / "tree"
    folder.mkdir(parents=True)
    got = gates._rerun_tree(str(folder), "tree")
    assert got is not None and got.path == str(folder) and got.label == "tree"
    assert gates._rerun_tree(None, "tree") is None
    assert gates._rerun_tree(str(tmp_path / "missing"), "tree") is None
    link = tmp_path / "t0" / "link"
    os.symlink(str(folder), str(link))
    assert gates._rerun_tree(str(link), "tree") is None


# ================================================================ F16: the verifier's read forms
#: commands of the rr-002 verifiers (agents afb68fd3, a2899566, a214576d, a4c0b5cf) that the old
#: allow list or path scan refused, with their try folder written as {f}. Twelve of the fifteen pass the
#: strict grammar of section 19 as they stand; the other three are REAL_FORMS_REFUSED (19.8.3).
REAL_FORMS = [
    'for f in h-e1 h-e2 h-e3 h-e4; do echo "--- $f"; cat receipts/$f.json; done; ls receipts',
    'grep -n "def \\|BLOCKED\\|refused" tree/alpaca/research/verifier.py | sed -n \'1,400p\' '
    '| grep -n "" | head -300',
    'for f in packet/evidence/01-attempt-1-1.log packet/evidence/02-attempt-1-1.log; do echo "=== $f"; '
    'cat "$f"; echo; echo "--- end"; done',
    'for f in packet/evidence/05-open.json packet/evidence/06-after-eval.json; do echo "=== $f"; '
    'tail -c 200 "$f"; echo; grep -o \'"errors":\\[[^]]*\\]\' "$f"; done',
    'sed -n 1,1p tree/alpaca/research/verifier.py >/dev/null 2>&1; grep -n "def \\|BLOCKED\\|gate" '
    'tree/alpaca/research/verifier.py | sed -n 1,400p | grep -n "gate\\|BLOCKED" | head -120',
    'sed -n 200,260p tree/design/research-engine/verifier.md',
    'grep -o \'^"[a-z-]*:\' out/fp-open.txt | sort | uniq -c; grep -c \'"main:packs/refpack/\' '
    'out/fp-open.txt',
    'grep -n "\\"any\\"\\|requires.*any\\|\\`any\\`" tree/design/research-engine/verifier.md | head -40',
    'for r in hash-e1 hash-e2; do printf \'%s \' "$r"; grep -o \'"match": [a-z]*\' receipts/$r.json '
    '| tr -d \'\\n\'; echo; done',
    'for i in 1 2 3; do python3 packet/bin/rerun sh-metric-tree-r$i --shell \'python3 '
    '../packet/frozen/packs/refpack/bench/measure.py --src ./packs/refpack-toyzip\'; '
    'echo "exit=$?"; cat receipts/sh-metric-tree-r$i.out; done',
    'python3 packet/bin/rerun g-unit --gate unit --side tree; echo "client-exit=$?"; '
    'cat receipts/g-unit.json',
    'cut -d: -f1 out/fp-open.txt | nl | tac | head -3',
]

#: the three rr-002 forms the grammar denies, each with its rule and the rewrite the deny text gives (19.8.3)
REAL_FORMS_REFUSED = [
    ('for n in 05-open 06-after-proposer; do grep -o -E \'"[^"]*":"[^"]*"\' packet/evidence/$n.json '
     '> out/fp-$n.txt; done && wc -l out/fp-*.txt', "glob",
     'for n in 05-open 06-after-proposer; do grep -o -E \'"[^"]*":"[^"]*"\' packet/evidence/$n.json '
     '> out/fp-$n.txt; done && wc -l out/fp-05-open.txt out/fp-06-after-proposer.txt'),
    ('diff -rq --exclude=.git tree base; echo "diff-exit=$?"', "folder",
     'ls -R tree > out/t.txt; ls -R base > out/b.txt; diff out/t.txt out/b.txt; echo "diff-exit=$?"'),
    ('for f in packet/evidence/01-attempt-1-1.log packet/evidence/02-attempt-1-1.log; do echo "--- $f"; '
     'cat "$f"; echo; done && sha256sum packet/evidence/*', "glob",
     'for f in packet/evidence/01-attempt-1-1.log packet/evidence/02-attempt-1-1.log; do echo "--- $f"; '
     'cat "$f"; echo; done && sha256sum packet/evidence/01-attempt-1-1.log packet/evidence/02-attempt-1-1.log'),
]

#: forms that write, run code or read a path the call does not name: each stays refused
REFUSED_FORMS = [
    "sed -i s/a/b/ tree/x", "sed -i.bak -n 1p tree/x", "sed --in-place 1d tree/x",
    "sed -f out/script tree/x", "sed 's/a/b/w out/x' tree/x", "sed -n '1w out/x' tree/x",
    "sed -n 'r /etc/passwd' tree/x", "sed '1e id' tree/x", "sed 's/a/id/e' tree/x",
    "sed -n '/x/W out/y' tree/x", "sort -o out/x tree/a", "sort --output=out/x tree/a",
    "sort -T /tmp tree/a", "sort --compress-program=gzip tree/a", "sort --files0-from=out/l",
    "uniq tree/a out/b", "python3 -c 'print(1)'", "awk '{print}' tree/x", "tee out/x < tree/y",
    "xargs cat < out/list", "cat $(ls tree)", "while read l; do cat $l; done < out/list",
    "for f in a b; do rm $f; done", "for f in a; cat $f; done", "do cat tree/x",
    "echo x > ../other", "for f in a; do echo x > ../$f; done",
    "for f in python3; do $f -c 'print(1)'; done", "for f in $X; do $f tree/x; done",
    "sed --expression='1w out/x' tree/a", "sed -ne 'w out/x' tree/a", "sed -si 1d tree/x",
    "sort -ro out/x tree/a", "uniq -- tree/a out/b",
]


def _whys(text, folder):
    calls = [{"tool": "Bash", "kind": "shell", "input": "cd %s && %s" % (folder, text)}]
    return [h["why"] for h in pathscan.shell_allowed(
        calls, allow=verifier.SHELL_ALLOW, try_folder=folder,
        client=os.path.join(folder, "packet", "bin", "rerun"), reset_cwd="/proj")]


def test_f16_the_read_forms_of_the_real_verifiers_pass_the_breach_check(tmp_path):
    folder = str(tmp_path / "proj" / ".alpaca" / "verify" / "t-1" / "vf-t-1-9" / "v1" / "r0" / "t0")
    os.makedirs(os.path.join(folder, "out"))
    root = str(tmp_path / "proj")
    for text in REAL_FORMS:
        calls = [{"tool": "Bash", "kind": "shell", "input": "cd %s && %s" % (folder, text),
                  "output": "ok"}]
        got = verifier._breaches(root, folder, calls, [], shell_cwd=root, extra_allowed=(),
                                 runtime_tools=session_runtime.RUNTIME_TOOLS)
        assert got == [], (text, [p["detail"] for p in got])


def test_f16_three_of_the_rr002_forms_are_denied_with_their_rule_and_the_rewrite_passes(tmp_path):
    """19.8.3: the glob and the `diff -rq` of three rr-002 forms are refused by the strict grammar, each
    with the rule that names it, and the rewrite the deny text gives passes the breach check."""
    folder = str(tmp_path / "proj" / ".alpaca" / "verify" / "t-1" / "vf-t-1-9" / "v1" / "r0" / "t0")
    os.makedirs(os.path.join(folder, "out"))
    root = str(tmp_path / "proj")
    for text, rule, rewrite in REAL_FORMS_REFUSED:
        calls = [{"tool": "Bash", "kind": "shell", "input": "cd %s && %s" % (folder, text),
                  "output": "ok"}]
        got = verifier._breaches(root, folder, calls, [], shell_cwd=root, extra_allowed=(),
                                 runtime_tools=session_runtime.RUNTIME_TOOLS)
        assert got and [p for p in got if p["rule"] == "allow-list"], text
        hits = pathscan.shell_allowed(calls, allow=verifier.SHELL_ALLOW, try_folder=folder,
                                      client=os.path.join(folder, "packet", "bin", "rerun"),
                                      reset_cwd=root)
        assert [h["rule"] for h in hits] == [rule], (text, hits)
        calls = [{"tool": "Bash", "kind": "shell", "input": "cd %s && %s" % (folder, rewrite),
                  "output": "ok"}]
        got = verifier._breaches(root, folder, calls, [], shell_cwd=root, extra_allowed=(),
                                 runtime_tools=session_runtime.RUNTIME_TOOLS)
        assert got == [], (rewrite, [p["detail"] for p in got])


def test_f16_every_form_that_writes_runs_or_reads_unnamed_paths_stays_refused(tmp_path):
    folder = str(tmp_path / "proj" / "v1" / "r0" / "t0")
    os.makedirs(os.path.join(folder, "out"))
    for text in REFUSED_FORMS:
        assert _whys(text, folder), text
    # a loop over a path outside the try folder is a path the breach check names
    calls = [{"tool": "Bash", "kind": "shell", "input": "cd %s && for f in ../../x tree/y; do cat "
              "\"$f\"; done" % folder}]
    hits = pathscan.outside(calls, cwd=folder, allowed=[folder], ignore_prefixes=(),
                            watched=(str(tmp_path),), reset_cwd=folder, exact=True)
    assert [h["path"] for h in hits] == [os.path.normpath(os.path.join(folder, "../../x"))]
    # a loop over words the scan cannot know leaves the path unplaced, which is named
    calls = [{"tool": "Bash", "kind": "shell", "input": "cd %s && for f in $HOME; do cat $f; done"
              % folder}]
    assert pathscan.outside(calls, cwd=folder, allowed=[folder], ignore_prefixes=(),
                            watched=(str(tmp_path),), reset_cwd=folder, exact=True)
    # a loop that moves into the other slot's folder names that folder
    calls = [{"tool": "Bash", "kind": "shell", "input": "cd %s && for d in ../../v2/r0/t0; do cd "
              "$d; cat x; done" % folder}]
    hits = pathscan.outside(calls, cwd=folder, allowed=[folder], ignore_prefixes=(),
                            watched=(str(tmp_path),), reset_cwd=folder, exact=True)
    # the folder the loop moves into, and the `x` that `cat` opens there (every operand of a read is a
    # path in the folder the call is in, in-session-runtime.md section 18.1b)
    # (section 19.9.3, W2: the folder is unknown after a `cd` that is not the first command of the
    # call, so the scan no longer places the word of the `cd` or the `x` after it: both are named
    # as written, unplaced)
    assert [h["path"] for h in hits] == ["../../v2/r0/t0", "x"]
    # an echo in a loop names no path; its redirect still counts
    calls = [{"tool": "Bash", "kind": "shell", "input": "cd %s && for f in /etc/x; do echo \"== $f\" "
              "> ../log; done" % folder}]
    hits = pathscan.outside(calls, cwd=folder, allowed=[folder], ignore_prefixes=(),
                            watched=(str(tmp_path),), reset_cwd=folder, exact=True)
    assert [h["path"] for h in hits] == [os.path.normpath(os.path.join(folder, "../log"))]


def test_f16_unroll_reads_a_for_loop_as_its_body_once_for_each_word():
    got = pathscan.unroll(pathscan.parse('for f in a b; do cat "$f" x/${f}; done > out/l; echo z'))
    assert got["error"] is None
    assert [c["argv"] for c in got["commands"]] == [["cat", "a", "x/a"], ["cat", "b", "x/b"], [],
                                                    ["echo", "z"]]
    assert got["commands"][2]["redirects"] == [(">", "out/l")]
    nested = pathscan.unroll(pathscan.parse("for i in 1 2; do for j in a; do cat $i/$j; done; done"))
    assert [c["argv"] for c in nested["commands"]] == [["cat", "1/a"], ["cat", "2/a"]]
    # a quoted `$f` is a plain character; `$fx` is another name; a value with a blank is unknown
    lit = pathscan.unroll(pathscan.parse("for f in a; do cat '$f' $fx; done"))
    assert [c["argv"] for c in lit["commands"]] == [["cat", "$f", "$fx"]]
    assert lit["commands"][0]["live"] == [[], [], [0]]
    braces = pathscan.unroll(pathscan.parse('for f in p/{a,b}.json; do cat "$f"; done'))
    assert [c["argv"] for c in braces["commands"]] == [["cat", "p/a.json"], ["cat", "p/b.json"]]
    blank = pathscan.unroll(pathscan.parse('for f in "a b"; do cat $f; done'))
    assert blank["commands"][0]["argv"] == ["cat", "$f"] and blank["commands"][0]["live"][1] == [0]
    for bad in ("for f in a; cat x; done", "do cat x", "for 1x in a; do cat x; done",
                "for f in a; do cat x"):
        assert pathscan.unroll(pathscan.parse(bad))["error"], bad


def test_f16_sed_scripts_are_read_and_only_print_forms_pass():
    ok = ["1,400p", "/^x/,/^y/p", "s/a/b/g", "s|/x|/y|2", "$!d", "1~2p", "/a/{p;d}", "y/ab/cd/",
          "0,/re/d", "s/[/]/x/", "a text; with a semicolon", "=", "s/a/b/;p", "\\,x,p"]
    for script in ok:
        if script == "s/[/]/x/":
            # a bracket that holds the delimiter is read differently by sed versions: refused
            assert pathscan.sed_script_problem(script), script
            continue
        assert pathscan.sed_script_problem(script) is None, script
    for script in ("w out/x", "1w x", "s/a/b/w x", "s/a/b/gw x", "e id", "s/a/b/e", "r /etc/x",
                   "R x", "W x", "v", "1,2", "s/a/b/q"):
        assert pathscan.sed_script_problem(script), script


def test_f16_the_verifier_and_auditor_prompts_name_the_allowed_forms():
    text = verifier.render_prompt()
    flat = " ".join(text.split())
    # the verifier's prompt states the strict grammar of section 19: a table row for each program
    for form in ("`sed -n '10,40p' FILE`", "`sort`: options", "`uniq`: options", "operands: at most one file",
                 "`for NAME in WORD ...; do COMMANDS; done`", "`echo \"rc=$?\"`", "`tr`: options",
                 "`cut`: options"):
        assert form in flat, form
    assert text.isascii()
    with open(os.path.join(os.path.dirname(audit.__file__), "prompts", "audit.md"),
              encoding="utf-8") as fh:
        aflat = " ".join(fh.read().split())
    assert "`sed` that only prints" in aflat and "`for NAME in WORD ...; do ...; done`" in aflat
    for word in pathscan.READ_FORMS:
        assert word in verifier.SHELL_ALLOW and word in audit._SHELL_ALLOW


# ================================================================ F17: the metric claim's gate item
def test_f17_the_metric_claim_names_its_gate_item_as_the_chain_does(project):
    from alpaca import ops
    from alpaca.gates import verdict as vc
    from alpaca.research import events as rev
    from alpaca.research.types import GateStage, Metric
    from alpaca.tests import test_research_gates as tg
    conn = db.connect(project)
    try:
        task = ops.add_task(conn, "op-001", "attempt", "attempt", extra={"research": {
            "run": "rr-001", "role": "attempt", "attempt": "rr-001:r0:s0"}})
        rev.emit(conn, rev.ATTEMPT, run="rr-001", ref=task, op="op-001", session="research-rr-001",
                 code=vc.PASS, data={"forked_from": "0" * 40, "head": "0" * 40,
                                     "outcome": "baseline", "champion_metrics": {},
                                     "metrics": {"total_bytes": {"repeats": [3.0, 3.0, 3.0],
                                                                 "median": 3.0}}})
        got = {c["id"]: c for c in claims.engine_claims(conn, task)}
    finally:
        conn.close()
    metric = Metric(id="total_bytes", command="true", parser="markers", unit="B",
                    direction="lower", kind="deterministic", repeats=3, min_ok=3, margin=0,
                    timeout_s=10)
    contract = tg.make_contract(gates=(GateStage("build", 10),), metrics=(metric,))
    assert got["m1"]["commands"] == [{"gate": "metric:total_bytes", "cwd": "tree"}]
    assert got["m1"]["commands"][0]["gate"] in gates.chain(contract)


# ================================================================ F18: one member submitted alone
def _submit(w, jid, agent, wait):
    from alpaca import cli
    argv = ["--session", w.sid, "research", "submit", jid, "--nonce", w.entries[jid]["nonce"],
            "--agent-id", agent.agent_id, "--wait", str(wait), "--json"]
    buf = io.StringIO()
    t0 = time.monotonic()
    with contextlib.redirect_stdout(buf):
        rc = cli.main(argv)
    return rc, json.loads(buf.getvalue().strip().splitlines()[-1]), time.monotonic() - t0


def test_f18_a_driver_that_submits_the_members_one_by_one_is_not_held(world):
    """The driver submits v1 as soon as its agent finishes, before v2's agent does. The judge of
    v1 waits at the barrier for v2's claim, so the verb answers JOB-GROUP-WAIT (exit 2) at once
    instead of holding the driver for its whole wait; the driver then submits v2, and the same
    call for v1 gets the judge's answer. In rr-002 a submit of one member blocked for over ten
    minutes (claims 78366 and 78438)."""
    from alpaca.research import cli as research_cli
    w = world
    w.mp.setattr(research_cli, "_judge_alive", lambda v, scope: True)
    w.mp.setattr(research_cli, "GROUP_GRACE_S", 0.3)
    w.mp.setattr(session_runtime, "_BARRIERS", {})
    j1, j2 = _pair(w, group="vf-f18/r0")
    honest = (lambda role, jobd: {"calls": [_bash(jobd["cwd"], "ls", "packet")]})
    a1, a2 = _overlapping(w, j1, j2, honest, honest)
    results = {}

    def slot(jid, slot_name):
        spec = w.make_spec("verifier", key="%s/%s/r0/t0" % (w.attempt, slot_name),
                           group="vf-f18/r0", group_size=2, verifier_slot=slot_name, first_pass=True,
                           scanned_cwd=_folder(w, slot_name))
        conn = db.connect(w.root)
        try:
            results[jid] = session_runtime.wait(conn, w.root, spec, w.SCOPE,
                                                jobs.read_job(w.root, w.SCOPE, jid), None)
        finally:
            conn.close()
    ths = [threading.Thread(target=slot, args=(j1, "v1"), daemon=True),
           threading.Thread(target=slot, args=(j2, "v2"), daemon=True)]
    for t in ths:
        t.start()
    rc, body, took = _submit(w, j1, a1, wait=60)
    assert rc == 2 and body["code"] == "JOB-GROUP-WAIT" and body["retriable"] is True, body
    assert body["waiting"] == [j2] and took < 30, (body, took)
    rc2, body2, _t = _submit(w, j2, a2, wait=60)
    assert rc2 == 0 and body2["accepted"] is True, body2
    rc3, body3, _t = _submit(w, j1, a1, wait=60)
    assert rc3 == 0 and body3["accepted"] is True, body3
    for t in ths:
        t.join(30)
    assert results[j1]["outcome"] == "ok" and results[j2]["outcome"] == "ok", results
    # the repeated call was the same claim, not a second one
    filed = [c["n"] for c in jobs.fold(w.conn, w.SCOPE)[j1]["claims"]]
    assert filed == [1], filed


def test_f18_a_sibling_whose_claims_were_all_answered_counts_as_unclaimed(world):
    w = world
    j1, j2 = _pair(w, group="vf-f18b/r0")
    honest = (lambda role, jobd: {"calls": [_bash(jobd["cwd"], "ls", "packet")]})
    a1, a2 = _overlapping(w, j1, j2, honest, honest)
    assert jobs.unclaimed_siblings(w.conn, w.SCOPE, j1, root=w.root) == [j2]
    n2 = w.claim(j2, a2)
    assert jobs.unclaimed_siblings(w.conn, w.SCOPE, j1, root=w.root) == []
    # the judge answered the sibling's claim as retriable (a witness that had not landed): the
    # sibling's judge waits for its next claim, so the sibling counts as unclaimed again
    jobs.write_reply(w.root, w.SCOPE, j2, n2, {"code": "JOB-NO-STOP", "retriable": True})
    assert jobs.unclaimed_siblings(w.conn, w.SCOPE, j1, root=w.root) == [j2]
    assert jobs.unclaimed_siblings(w.conn, w.SCOPE, j1) == []
    assert jobs.unclaimed_siblings(w.conn, w.SCOPE, j2, root=w.root) == [j1]


def test_f18_the_skill_submits_the_members_of_a_group_together():
    with open(os.path.join(REPO, ".claude", "skills", "autoresearch", "SKILL.md"),
              encoding="utf-8") as fh:
        flat = " ".join(fh.read().split())
    assert "JOB-GROUP-WAIT" in flat
    assert "Submit the jobs of a group together" in flat
    assert "then repeat the first call, which then returns the judge's answer" in flat


# ================================================================ F19 and F20: K16 on honest pairs
VERDICT_LINES = ['{"id": "s1", "verdict": "supported", "reason": null,',
                 '{"id": "r1", "verdict": "cannot-verify", "reason": "environment",']
SHARED_LINE = "the stored build log holds only ALPACA-GATE-JAIL-STARTED, 25 bytes long"
#: a line of a file of the tree (not of the packet) that both verifiers read and quote as it is
TREE_LINE = "def verify_rerun(conn, root, *, run, task, contract, request, trees, session):"


def _verifier(own_lines, *, quote=SHARED_LINE, extra=(), read_back=False, final=None):
    """A verifier that reads a packet file holding SHARED_LINE and a tree file holding TREE_LINE,
    writes a draft, submits a verdict whose lines start with `own_lines`, and quotes both lines in
    its final message, the tree line on a line of its own."""
    def behavior(role, jobd):
        cwd = jobd["cwd"]
        verdict = "\n".join(own_lines + ['  "note": "%s"}' % quote])
        with open(os.path.join(cwd, "packet", "evidence.log"), "w", encoding="utf-8") as fh:
            fh.write(SHARED_LINE + "\n")
        with open(os.path.join(cwd, "receipts", "verdict.json"), "w", encoding="utf-8") as fh:
            fh.write(verdict + "\n")
        calls = [_bash(cwd, "cat packet/evidence.log", SHARED_LINE + "\n"),
                 _bash(cwd, "grep -n verify_rerun tree/gates.py", "726:" + TREE_LINE + "\n"),
                 _bash(cwd, "cat > out/draft.json <<'D'\n%s\nD" % verdict, "")]
        if read_back:
            calls.append(_bash(cwd, "cat out/draft.json", verdict + "\n"))
        calls += list(extra)
        calls.append(_bash(cwd, "python3 packet/bin/rerun --submit - <<'VERDICT'\n%s\nVERDICT"
                           % verdict, "rerun: receipt submit 0123"))
        return {"calls": calls, "final": final or ("Done. The note says: %s\nThe gate re-run is:\n%s"
                                                   % (quote, TREE_LINE))}
    return behavior


def test_f19_honest_verifiers_that_write_the_same_verdict_lines_pass_the_pair_check(world):
    w = world
    j1, j2 = _pair(w)
    a1, a2 = _overlapping(w, j1, j2, _verifier(VERDICT_LINES), _verifier(VERDICT_LINES,
                                                                        read_back=True))
    got, _n = _barrier_run(w, j1, j2, [(j1, a1), (j2, a2)])
    assert got == {j1: None, j2: None}, {j: (p.code, p.detail) if p else None for j, p in got.items()}


def test_f19_a_line_of_the_siblings_packet_is_shared_even_without_its_transcript(world):
    """A group of one reads the other slot's counted try with no transcript of it at hand (its
    kept copy may be gone): a line of its verdict that is a line of the scanned agent's own
    packet, the sealed inputs both read, is shared input, not its output; a line it wrote itself
    still refuses. The packet read is the scanned agent's: a line planted there to pass as shared
    changes that packet, whose re-hash voids the scanned agent's own pass."""
    w = world
    j1, j2 = _pair(w)
    sib = jobs.read_job(w.root, w.SCOPE, j1)
    mine = jobs.read_job(w.root, w.SCOPE, j2)["cwd"]
    with open(os.path.join(mine, "packet", "evidence.log"), "w", encoding="utf-8") as fh:
        fh.write(SHARED_LINE + "\n")
    own = '"observed": "the verifier of slot v1 wrote this line itself and nobody else did",'
    verdict = '{"id": "e1",\n %s\n %s' % (SHARED_LINE, own)
    out = jobs.sibling_output(sib, None, verdict_text=verdict, shared_folder=mine)
    assert [ln for ln, _w in out["lines"]] == [own], out["lines"]
    # the sibling's own packet is not the filter: there the line stays the sibling's
    assert SHARED_LINE in [ln for ln, _w in jobs.sibling_output(sib, None, verdict_text=verdict,
                                                                shared_folder=sib["cwd"])["lines"]]
    seen = [{"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1",
                                                      "content": SHARED_LINE}]}}]
    assert jobs.scan_for_sibling(seen, out, {}) is None
    seen[0]["message"]["content"][0]["content"] = own
    assert jobs.scan_for_sibling(seen, out, {}).code == "JOB-SIBLING-REF"


def test_f19_a_line_of_the_siblings_own_output_that_reaches_the_agent_is_refused(world):
    w = world
    j1, j2 = _pair(w)
    own = "the v1 verifier found that claim r1 hides the run's exit status behind a cat"
    line = '  "observed": "%s",' % own
    # the driver copied v1's verdict into a file v2 then read: the line reaches v2 in a tool result
    relay = _bash("/x", "cat out/notes.txt", "notes:\n" + line + "\n")
    a1, a2 = _overlapping(w, j1, j2, _verifier(VERDICT_LINES + [line]),
                          _verifier(VERDICT_LINES, extra=[relay]))
    got, claims_ = _barrier_run(w, j1, j2, [(j1, a1), (j2, a2)])
    assert got[j1] is None, got[j1]
    p = got[j2]
    assert p is not None and p.code == "JOB-SIBLING-REF", p
    m = p.extra["match"]
    assert m["kind"] == "line" and m["what"] == "verdict file" and own in m["text"]
    assert m["side"] == "in" and m["origin"]["type"] == "user" and m["origin"].get("tool_use_id")
    assert own not in p.detail                  # the reply to the driver carries no verdict text
    # F20: the refusal row records the check, its detail and the match, bounded
    c = w.ctx_for(j2, claims_[j2])
    c.agent_id = a2.agent_id
    p.extra.setdefault("check", "group")
    row = jobs.commit_refuse(w.conn, w.root, c, p, session=w.SCOPE)["problems"][0]
    assert (row["check"], row["name"], row["k"]) == ("JOB-SIBLING-REF", "group", "K16"), row
    assert row["detail"] == p.detail and own in row["match"]["text"]
    assert len(row["match"]["text"]) <= jobs.MATCH_TEXT_MAX
    assert len(row["match"]["context"]) <= jobs.MATCH_TEXT_MAX
    stored = json.loads(w.conn.execute("SELECT data FROM events WHERE kind='research-job-refused' "
                                       "ORDER BY id DESC").fetchone()["data"])
    assert stored["problems"][0]["match"]["text"] == row["match"]["text"]


def test_f19_an_off_list_call_is_the_breach_checks_not_a_sibling_reference(world):
    w = world
    j1, j2 = _pair(w)
    odd = [_bash("/x", "python3 -c 'print(1)'", "1")]
    a1, a2 = _overlapping(w, j1, j2, _verifier(VERDICT_LINES, extra=odd), _verifier(VERDICT_LINES))
    got, _n = _barrier_run(w, j1, j2, [(j1, a1), (j2, a2)])
    assert got == {j1: None, j2: None}, {j: (p.code, p.detail) if p else None for j, p in got.items()}
    # the off-list call still voids the offender's pass in the verifier's own breach check
    folder = jobs.read_job(w.root, w.SCOPE, j1)["cwd"]
    hits = pathscan.shell_allowed(at.calls(at.read_records(a1.path)), allow=verifier.SHELL_ALLOW,
                                  try_folder=folder, client=os.path.join(folder, "packet", "bin",
                                                                         "rerun"),
                                  reset_cwd=w.root, runtime_tools=session_runtime.RUNTIME_TOOLS)
    assert hits and all(h.get("rule") for h in hits), hits          # the grammar names each call


def test_f19_a_shell_write_outside_the_own_try_folder_still_refuses_both(world):
    w = world
    j1, j2 = _pair(w)
    # a write outside v1's own folder that names no identifier of v2 (the project root here)
    write = [_bash(jobs.read_job(w.root, w.SCOPE, j1)["cwd"], "echo forged > %s/hint.txt" % w.root,
                   "")]
    a1, a2 = _overlapping(w, j1, j2, _verifier(VERDICT_LINES, extra=write), _verifier(VERDICT_LINES))
    got, _n = _barrier_run(w, j1, j2, [(j1, a1), (j2, a2)])
    assert got[j1] is not None and got[j1].code == "JOB-SIBLING-REF"
    assert got[j2] is not None and got[j2].code == "JOB-SIBLING-REF"
    assert got[j1].extra["match"]["kind"] == "write" and "hint.txt" in got[j1].extra["match"]["text"]


def test_f19_a_sibling_that_wakes_after_the_other_judge_ended_still_refuses(world):
    # the member that waits at the barrier wakes only after its sibling has run K16 and left, the
    # order a loaded host produces; the leaving member's K16 data must still be there for it
    w = world
    j1, j2 = _pair(w)
    write = [_bash(jobs.read_job(w.root, w.SCOPE, j1)["cwd"], "echo forged > %s/hint.txt" % w.root,
                   "")]
    a1, a2 = _overlapping(w, j1, j2, _verifier(VERDICT_LINES, extra=write), _verifier(VERDICT_LINES))
    barrier = session_runtime.GroupBarrier(w.SCOPE, "vf-x/r0")

    class LateWake(threading.Condition):
        def wait(self, timeout=None):
            deadline = time.monotonic() + 60
            while not barrier.gone and time.monotonic() < deadline:
                super().wait(0.05)
            return True

    barrier.cond = LateWake()
    got, _n = _barrier_run(w, j1, j2, [(j1, a1), (j2, a2)], barrier=barrier)
    assert len(barrier.gone) == 2
    assert got[j1] is not None and got[j1].code == "JOB-SIBLING-REF"
    assert got[j2] is not None and got[j2].code == "JOB-SIBLING-REF"


def test_f19_the_siblings_identifiers_refuse_wherever_they_show(world):
    w = world
    j1, j2 = _pair(w)
    sib = jobs.read_job(w.root, w.SCOPE, j1)
    for token, what in ((sib["nonce"], "nonce"), (os.path.join(sib["cwd"], "receipts"),
                                                  "receipts folder")):
        rec = [{"type": "assistant", "message": {"content": [{"type": "text", "text": "x"}]}},
               {"type": "user", "message": {"content": [{"type": "tool_result",
                                                         "tool_use_id": "t1",
                                                         "content": "see %s" % token}]}}]
        p = jobs.scan_for_sibling(rec, jobs.sibling_output(sib, None), {})
        assert p is not None and p.code == "JOB-SIBLING-REF" and p.extra["match"]["what"] == what
        if what == "nonce":
            assert sib["nonce"] not in json.dumps(p.extra) and sib["nonce"] not in p.detail


def test_f20_every_refusal_row_names_its_check_and_k_number(world):
    w = world
    jid = w.issue("implementer")
    w.hand()
    a = w.run_agent(jid, agent_type="Explore")
    n = w.claim(jid, a)
    c, problem = w.judge(jid, n)
    assert problem is not None and problem.code == "JOB-AGENT-TYPE"
    row = jobs.commit_refuse(w.conn, w.root, c, problem, session=w.SCOPE)["problems"][0]
    assert (row["check"], row["name"], row["k"], row["code"]) == (
        "JOB-AGENT-TYPE", "witness", "K4", "JOB-AGENT-TYPE")
    assert row["detail"] == problem.detail and "match" not in row


# ================================================================ F22: the token cap and cache reads
def _cached(n):
    def edit(records, agent):
        for r in records:
            if r.get("type") == "assistant":
                r["message"]["usage"]["cache_read_input_tokens"] = n
                r["message"]["usage"]["cache_creation_input_tokens"] = 50
                r["message"]["usage"]["cache_creation"] = {"ephemeral_5m_input_tokens": 50,
                                                           "ephemeral_1h_input_tokens": 0}
    return edit


def test_f22_the_budget_counts_no_cache_read_and_the_dollar_cost_is_unchanged(world):
    from alpaca.analytics import metrics
    w = world
    jid = w.issue("implementer")
    w.hand()
    a = w.run_agent(jid, edit=_cached(100000), model="claude-sonnet-5")
    c, problem = w.judge(jid, w.claim(jid, a))
    assert problem is None, problem
    out = w.accept(c)
    usages = [r["message"]["usage"] for r in a.records if r.get("type") == "assistant"]
    responses = len(usages)
    # input, output and cache writes count; the cache reads do not
    fresh = sum(u["input_tokens"] + u["output_tokens"] + u["cache_creation_input_tokens"]
                for u in usages)
    end = out["end_data"]
    assert end["tokens"] == fresh, (end["tokens"], fresh)
    assert out["usage"]["cache_read_tokens"] == responses * 100000
    assert out["usage"]["tokens_total"] == fresh + responses * 100000
    # the dollar figure is the analysis's own, which prices every kind, cache reads included
    analysis = metrics.analyze(w.root, c.job.get("launch_session"))
    assert end["cost_usd"] == pytest.approx(analysis["cost"]["estimated_usd"])
    assert end["cost_usd"] > responses * 100000 * 0.2e-6
    # the launch end on the record, which spend.totals sums for the cap, holds the budget count
    recorded = json.loads(w.conn.execute("SELECT data FROM events WHERE id=?",
                                         (out["end"]["id"],)).fetchone()["data"])
    assert recorded["tokens"] == fresh and recorded["cost_usd"] == end["cost_usd"]
    assert out["result"]["measured_usage"]["cache_read_tokens"] == responses * 100000


def test_f22_budget_tokens_reads_the_split_of_the_analysis():
    assert jobs.budget_tokens({"total_tokens": 1000, "cached_input_tokens": 900}) == (100, 1000, 900)
    assert jobs.budget_tokens({"total_tokens": 1000, "cached_input_tokens": None}) == (1000, 1000,
                                                                                      None)
    assert jobs.budget_tokens({"total_tokens": 0}) == (None, None, None)
    assert jobs.budget_tokens(None) == (None, None, None)


# ================================================================ F23: the replay's corpus pass
def test_f23_the_replay_runs_the_corpus_pass_only_when_asked(monkeypatch, tmp_path):
    import importlib.util
    import sys
    require_dev_files("tools/k_replay.py")
    tool = os.path.join(REPO, "tools", "k_replay.py")
    spec = importlib.util.spec_from_file_location("k_replay_f23", tool)
    kr = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(kr)
    import alpaca.pool as pool_mod
    from alpaca.research import session_engine as eng
    for mod, name in ((eng, "COMPILE_SITE_PACKAGES"), (eng, "PRELOAD_BLOCK"), (eng, "compile_engine"),
                      (jobs, "STRAGGLER_WAIT_S"), (jobs, "find_processes"), (pool_mod, "pool_dir")):
        monkeypatch.setattr(mod, name, getattr(mod, name))
    stand_in = tmp_path / "stand-in.jsonl"
    stand_in.write_text("{}\n", encoding="utf-8")
    for name in ("TRANSCRIPT", "PROMPT"):
        monkeypatch.setattr(kr, name, str(stand_in))
    monkeypatch.setattr(kr, "REAL_DATA", (str(stand_in),))
    monkeypatch.setattr(kr, "make_base", lambda scratch: str(stand_in))
    monkeypatch.setattr(kr, "PROFILES", {})
    ran = []
    monkeypatch.setattr(kr, "corpus_pass", lambda scratch, base: ran.append(scratch))
    for argv, want in ((["--scratch", str(tmp_path / "s")], []),
                       (["--scratch", str(tmp_path / "s"), "--no-corpus"], []),
                       (["--scratch", str(tmp_path / "s"), "--corpus"], [str(tmp_path / "s")])):
        ran.clear()
        monkeypatch.setattr(kr, "OUT", io.StringIO())
        monkeypatch.setattr(kr, "RESULTS", [])
        monkeypatch.setattr(sys, "argv", ["k_replay.py"] + argv)
        kr.main()
        assert ran == want, (argv, ran)
