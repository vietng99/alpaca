"""M1.19 -- M1 acceptance on the sample repo (spec 10 M1 row; Q23 dogfood rule).

One end-to-end test drives the sample spec (tests/fixtures/sample_spec/SPEC.md) through the
whole M1 checklist engine and phase ladder, preferring the REAL entry points (`python3 -m
alpaca ...`, `python3 -m alpaca.checklist.*`, `python3 -m alpaca.project_schema`) and reusing the real
in-code interfaces where no CLI exists (bridge R1, closure R2 have no verb) or where the
real CLI cannot be reached against a throwaway root (the phase door runs workspace_guard,
whose NO-TMP invariant BLOCKS a tmp root when driven through the production CLI; the door's
own M1.15 tests drive `doors.run(..., tmp_prefixes=frozenset())` in-process for exactly this
reason, so this acceptance test does the same and never reimplements the door).

The Done-when, proved below on the sample:

  * a sample spec becomes rows            -- artifact.parse + synthesis + bridge land 15
                                             obligation rows (3 requirement steps x 5 items);
  * discharged by instruments             -- every row is discharged through the real
                                             `alpaca task move <row-id> done --proof ... --level`
                                             verb (M1.13 authors a verdict row, not a status
                                             flip), and the discharge fold reads them back;
  * a phase advances or pauses by level   -- the requirement->design boundary ADVANCES
                                             automatically at L5 and PAUSES the same boundary
                                             at L2 with a recorded pending human decision;
  * `alpaca verify` still passes              -- the append-only hash chain recomputes clean over
                                             every event the run appended.

Every subprocess runs against a THROWAWAY project root (the conftest `project` fixture: a tmp
root carrying ALPACA-MANIFEST, with CLAUDE_PROJECT_DIR / CLAUDE_CONFIG_DIR pointed at it), never
the live `.alpaca/`. Every open() passes encoding="utf-8"; every subprocess passes text=True,
encoding="utf-8". No em dash anywhere.
"""
import os
import shutil
import subprocess
import sys

# The real interfaces reused in-process where no CLI exists (bridge R1, closure R2) or where
# the production door CLI cannot run against a tmp root (see NOTMP below). No reimplementation.
from alpaca import db, project_schema
from alpaca.checklist import artifact as artifact_mod
from alpaca.checklist import step_model as sm
from alpaca.checklist import synthesis, bridge, closure, verdict_row
from alpaca.gates import verdict as vc
from alpaca.phase import doors
from alpaca.tests import proofkit

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FIX = os.path.join(REPO, "alpaca", "tests", "fixtures", "sample_spec")
SPEC_FIX = os.path.join(FIX, "SPEC.md")
YAML_FIX = os.path.join(FIX, "project.yaml")
MODEL = os.path.join(REPO, "step-models", "requirement.json")
PROBE = os.path.join(REPO, "contracts", "verify", "example-probe.sh")

# workspace_guard's NO-TMP invariant would BLOCK a pytest tmp root; the door's own M1.15 tests
# isolate the containment property from the tmp-root property with an empty tmp-prefix set, and
# this acceptance test drives the real `doors.run` the same way.
NOTMP = frozenset()


def _env(root):
    """A subprocess environment pointed at the throwaway root, with the harness importable.

    CLAUDE_PROJECT_DIR sends every `alpaca` write to the throwaway `.alpaca/`, never the live one;
    PYTHONPATH makes the `alpaca` package importable although the throwaway root is a bare tree.
    CLAUDE_CONFIG_DIR is already set on os.environ by the `project` fixture."""
    return {**os.environ,
            "CLAUDE_PROJECT_DIR": root,
            "PYTHONPATH": REPO + os.pathsep + os.environ.get("PYTHONPATH", "")}


def _run(args, root):
    return subprocess.run(args, cwd=root, env=_env(root),
                          capture_output=True, text=True, encoding="utf-8")


def _alpaca(sub_args, root):
    return _run([sys.executable, "-m", "alpaca", *sub_args], root)


def _ok(r):
    assert r.returncode == vc.PASS, r.stdout + r.stderr
    return r


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def test_m1_acceptance_end_to_end(project):
    root = project

    # ---- 0. the sample project.yaml is schema-complete, through the real project-schema CLI.
    _ok(_run([sys.executable, "-m", "alpaca.project_schema", YAML_FIX], root))
    # and in-code, so a schema regression is caught here too (not only by exit code).
    import yaml
    with open(YAML_FIX, encoding="utf-8") as fh:
        doc = yaml.safe_load(fh)
    ok, errors = project_schema.validate(doc)
    assert ok, errors

    # ---- 1. onboard the sample through the real verb, then install its schema-complete config.
    _ok(_alpaca(["onboard", "--name", "sample-greet", "--who", "alex:owner",
             "--what", "greet CLI"], root))
    # onboard writes a generic project.yaml; overlay the sample's, which declares the oracle
    # classes the SPEC.md uses so fidelity reads a real taxonomy.
    shutil.copy(YAML_FIX, os.path.join(root, "project.yaml"))
    shutil.copy(SPEC_FIX, os.path.join(root, "SPEC.md"))
    spec = os.path.join(root, "SPEC.md")

    # ---- 2. open the op and add the tracker row the pad will name as the first non-done row.
    _ok(_alpaca(["op", "new", "drive the sample spec through the requirement gate",
             "--done-when", "every requirement obligation is discharged"], root))
    _ok(_alpaca(["task", "add", "--title", "task", "op-001",
             "carry requirement obligations to the design boundary",
             "--phase", "requirement"], root))

    # ---- 3. the front of the pipeline, through the real instrument CLIs (each PASSes).
    _ok(_run([sys.executable, "-m", "alpaca.checklist.artifact", spec, "--key-column", "item"], root))
    _ok(_run([sys.executable, "-m", "alpaca.checklist.synthesis",
              "--model", MODEL, "--artifact", spec], root))
    _ok(_run([sys.executable, "-m", "alpaca.checklist.fidelity",
              "--model", MODEL, "--artifact", spec], root))

    # ---- 4. drive the sample spec into rows: parse + synthesize (real interfaces), then land
    #         them through the bridge (R1, no CLI) and prove the traceability closure (R2, no CLI).
    art = artifact_mod.parse(spec, "item")
    assert [it["key"] for it in art["items"]] == ["AC-01", "AC-02", "AC-03", "AC-04", "AC-05"]
    model = sm.load(MODEL)
    rows = synthesis.synthesize(model, art, op="op-001")
    assert len(rows) == 15, "3 requirement steps x 5 acceptance items"

    conn = db.connect(root)
    res = bridge.apply(conn, rows, op="op-001", session="s1")
    assert res["verdict"] == vc.PASS and res["appended"] == 15, res
    clo = closure.check(conn, list(art["keys"]), art)
    assert clo["verdict"] == vc.PASS, clo

    # the record names the first non-done obligation row: before any discharge it folds `open`.
    assert verdict_row.status_fold(conn, rows[0]["id"]) == verdict_row.OPEN
    conn.close()

    # ---- 5. discharge every row by an instrument, through the real `alpaca task move` verb (M1.13
    #         authors a verdict row bound to the frozen content hash, never a status-column flip).
    for row in rows:
        # E4: a manual done move carries the SEALED proof report for that row.
        ptr = proofkit.seal_for(root, row["id"], session="s1")
        _ok(_alpaca(["task", "move", row["id"], "done",
                 "--proof", ptr, "--level", "L5"], root))

    conn = db.connect(root)
    assert all(verdict_row.status_fold(conn, r["id"]) == verdict_row.DISCHARGED for r in rows), \
        "every obligation folds discharged after the instrument discharges"

    # ---- 6. the requirement->design boundary ADVANCES automatically at L5.
    before_adv = len(db.events(conn, kind="phase-advance", limit=10 ** 9))
    code_l5 = doors.run(conn, "op-001", "requirement->design", level="L5",
                        root=root, tmp_prefixes=NOTMP, session="s1")
    assert code_l5 == vc.PASS, "at L5 an all-PASS door advances automatically"
    adv = db.events(conn, kind="phase-advance", limit=10 ** 9)
    assert len(adv) == before_adv + 1
    assert adv[-1]["data"]["boundary"] == "requirement->design"
    assert adv[-1]["data"]["mode"] == "auto"
    assert adv[-1]["data"]["level"] == "L5"

    # ---- 7. the SAME boundary PAUSES at L2 and records a pending human decision.
    code_l2 = doors.run(conn, "op-001", "requirement->design", level="L2",
                        root=root, tmp_prefixes=NOTMP, session="s1")
    assert code_l2 == vc.PAUSED, "at L2 the same boundary pauses for a human go"
    pau = db.events(conn, kind="phase-pause", limit=10 ** 9)
    assert pau, "a pause is recorded as an event"
    assert pau[-1]["data"]["boundary"] == "requirement->design"
    assert pau[-1]["data"]["pending"] == "human-go"
    assert pau[-1]["data"]["level"] == "L2"
    conn.close()

    # ---- 8. the pad and the record name the first non-done row, the phase, and the pending
    #         decision. Render RESUME.md through the real `alpaca status` projection.
    _ok(_alpaca(["status"], root))
    pad = _read(os.path.join(root, "RESUME.md"))
    assert "op-001" in pad
    assert "t-001" in pad, "the pad names the first non-done row"
    assert "requirement" in pad, "the pad names the phase"
    assert "requirement->design" in pad and "phase-pause" in pad, \
        "the pad surfaces the pending decision from the record"

    # ---- 9. alpaca verify still passes: the hash chain recomputes clean over every appended event.
    verify = _alpaca(["verify"], root)
    assert verify.returncode == vc.PASS, verify.stdout + verify.stderr
    assert "PASS" in verify.stdout
    conn = db.connect(root)
    ok_chain, reason = db.verify_chain(conn)
    assert ok_chain, reason
    conn.close()


def test_verify_phase_example_probe_is_an_executable_ascii_behavioral_probe():
    """The verify-phase contract example is executable, pure ASCII, and reports PASS under the
    verdict contract when its observed behavior holds (contracts/verify/example-probe.sh)."""
    assert os.access(PROBE, os.X_OK), "the example probe must be executable"
    with open(PROBE, "rb") as fh:
        raw = fh.read()
    assert all(b < 128 for b in raw), "the probe must be pure ASCII"
    r = subprocess.run([PROBE], capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == vc.PASS, r.stdout + r.stderr
    assert "PASS" in r.stdout
