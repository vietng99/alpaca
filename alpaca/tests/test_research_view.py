"""The research run view (op-009 t-108): alpaca/research/view.py, the /hub/research.json route in
alpaca/serve.py, alpaca/web/research.js and research.css, and the hub, cockpit and live-refresh
wiring (design/research-engine/operator-surface.md section 7, FR-053, FR-054, EC-025, SC-021).

Every fixture is a run built from events appended to a tmp record. Done when (t-108's contract):
  * web tests pass: the route answers 200 with the schema, 400 for a bad run id and 404 for an
    unknown run (test_the_route_answers_the_schema_a_bad_id_and_an_unknown_run); the new assets
    are served as JavaScript and CSS and are ASCII (test_the_new_assets_are_served_and_ascii);
    the real hub draws the page and the cockpit section in a browser
    (test_the_real_hub_draws_the_research_page_and_refreshes_it);
  * empty and failed runs render with their real state: every state of section 7.9 renders its
    banner (test_each_run_state_renders_its_banner), a run the owner stopped at an extension or
    budget hold reads stopped with the stop grant and no resume command
    (test_a_run_the_owner_stopped_at_a_hold_reads_stopped_never_resumable), a ship hold whose
    card the owner moved reads "card moved", never open
    (test_a_ship_hold_whose_card_the_owner_moved_reads_moved_never_open), a repair task never
    verified never reads "verified" (test_a_repair_task_never_verified_never_reads_verified),
    and the empty project shows "No research run is recorded"
    (test_each_run_state_renders_its_banner);
  * the view refreshes with the hub: a new record event redraws the open page with no reload
    (test_the_real_hub_draws_the_research_page_and_refreshes_it), the 15 s interval calls the
    research view (test_the_hub_interval_and_router_call_the_research_view), and
    ROUTE_NEEDS.research depends on overview (test_route_needs_research_depends_on_overview);
  * the repair run's resolved rate: one task in each bucket of section 7.8, "so far" while one is
    pending, "no repair tasks recorded" for none, the runtime-mismatch stop pending with its code
    and lost try, the blocked stops unresolved with "verification stopped: <code>"
    (test_the_resolved_rate_puts_each_repair_task_in_its_bucket,
    test_a_repair_run_with_no_repair_task_says_so_never_zero_percent), and a repair run shows no
    baseline or champion row and no champion line (test_a_repair_run_has_no_champion_line);
    each repair row shows its best attempt's gates
    (test_each_repair_row_shows_its_best_attempt_gates).

Fail case: a missing metric drawn as zero. test_a_missing_metric_is_never_drawn_as_zero and
test_the_chart_in_a_browser_plots_only_recorded_values check that an attempt with no median is
absent from the plotted points, present in the "no metric" strip and counted in the caption, and
that the y range excludes 0 when no value is 0 (test_the_y_range_includes_zero_only_for_a_zero).

Also: view.runs and view.run never open a writer connection and write nothing
(test_the_view_never_opens_a_writer_and_writes_nothing); the champion line breaks at a round with
no champion value (test_the_champion_line_breaks_at_a_round_with_no_value); a lineage break is
flagged (test_a_lineage_break_is_flagged); every source names an event kind or a table
(test_every_source_names_an_event_kind_or_a_table); the report link opens through the library
route with a web login (test_the_report_link_opens_through_the_library_route);
renderResearchSection(undefined) returns the unavailable state and the cockpit still renders
without o.research (test_the_cockpit_section_without_data_says_unavailable); a ruled
verification shows the ruling in place of the verifier chips and each verdict keeps its claims
(test_a_ruled_verification_shows_the_ruling_and_the_verdict_claims).

t-162 (viewport overflow): the browser test measures the page inside an iframe exactly the width
under test and checks that the viewport is that width, because headless Chrome's top-level
viewport under --dump-dom was 0 px or 500 px wide by chance; the page fits at 1440, 390, 320 and
160 px (test_the_chart_in_a_browser_plots_only_recorded_values), and a failed fit names the
element that widened the page (test_the_overflow_probe_names_the_element_that_widens_the_page).
"""
from __future__ import annotations

import base64
import functools
import hashlib
import html
import http.server
import json
import os
import re
import shutil
import subprocess
import textwrap
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from alpaca import cli, db, ops
from alpaca.research import events, state, view
from alpaca.tests.plain_op import plain_op

WEB = Path(__file__).resolve().parents[1] / "web"
RUN = "rr-001"
S = "research-rr-001"
OP = "op-001"
METRICS = [{"id": "value", "unit": "ms", "direction": "lower", "kind": "deterministic",
            "repeats": 1, "min_ok": 1, "margin": 0}]
NODE = shutil.which("node")
CHROME = shutil.which("google-chrome") or shutil.which("chromium")


# ============================================================ the record fixtures
def test_run_history_does_not_materialize_unrelated_events(project):
    """A growing unrelated project history must not be fetched for each run."""
    conn = _conn(project)
    _start(conn)
    task = _task(conn, 0, 0)
    included = db.append_event(conn, session=S, actor='test', kind='msg',
                               ref=task, data={'body': 'run task message'})['id']
    card = db.append_event(conn, session=S, actor='test', kind='review-card',
                           ref='q-001', data={'subject': 'research:rr-001:ship'})['id']
    db.append_event(conn, session=S, actor='test', kind='review-card',
                    ref='q-002', data={'subject': 'research:rr-0010:ship'})
    db.append_event(conn, session=S, actor='test', kind='review-move',
                    ref='q-003', data={'subject': 'research:rr-002:ship'})
    events.emit(conn, events.REPORT, run=RUN, ref=RUN, op=OP, session=S, data={})
    events.emit(conn, events.STATUS, run=RUN, ref=task, op=OP, session=S, data={})
    for i in range(128):
        db.append_event(conn, session=S, actor='test', kind='msg', ref='other-%d' % i,
                        data={'body': 'unrelated', 'run': RUN})
    factory = conn.row_factory
    fetched = []

    def counted(cursor, row):
        fetched.append(1)
        return factory(cursor, row)

    conn.row_factory = counted
    try:
        history = state._run_events(conn, RUN, {task})
    finally:
        conn.row_factory = factory
        conn.close()
    assert [e['kind'] for e in history] == [events.RUN_START, 'task-add', 'msg', 'review-card']
    assert [e['id'] for e in history[-2:]] == [included, card]
    assert len(fetched) <= 8


def _conn(project):
    cli.main(["init"])
    assert plain_op("research work", "runs read")
    return db.connect(project)


def _start(conn, run=RUN, *, mode="improve", control=None, metrics=None, targets=None, n=3, k=2,
           repair=0):
    data = {"mode": mode, "runbook": "pack/rb.yaml", "runbook_sha256": "a" * 64,
            "contract_sha256": "b" * 64, "intent": "intent-1", "override_diff": [], "k": k,
            "n": n, "metrics": METRICS if metrics is None else metrics,
            "targets": [{"metric": "value", "value": 10}] if targets is None else targets,
            "accept": {"rule": "champion"}, "budget": {"usd": 5.0, "tokens": 100000, "wall": None},
            "control": control, "via": "cli", "level": "L5", "session": "s",
            "research_branch": "research/%s" % run, "base_commit": "c0" * 20}
    repair_tasks = []
    for i in range(repair):
        tid = ops.add_task(conn, OP, "repair case %d" % i, "%s repair %d" % (run, i),
                           session=S, actor="research-runner",
                           extra={"research": {"run": run, "role": "repair", "attempt": None,
                                               "key": "%s:repair:%d" % (run, i), "mode": "repair",
                                               "case": "case-%d" % i, "index": i}})
        repair_tasks.append(tid)
    data["repair_tasks"] = repair_tasks
    events.emit(conn, events.RUN_START, run=run, ref=run, op=OP, session=S, data=data)
    return repair_tasks


def _task(conn, rnd, slot, *, run=RUN, repair_task=None, mode="improve", category=None):
    key = "%s:r%d:s%d%s" % (run, rnd, slot, (":" + repair_task) if repair_task else "")
    return ops.add_task(conn, OP, "attempt", "%s r%d s%d" % (run, rnd, slot), session=S,
                        actor="research-runner",
                        extra={"research": {"run": run, "role": "attempt", "attempt": key,
                                            "key": key, "baseline": rnd == 0 and not repair_task,
                                            "round": rnd, "slot": slot, "mode": mode,
                                            "category": category, "repair_task": repair_task}})


def _record(conn, task, outcome, *, run=RUN, value=None, reason=None, forked="c0" * 20,
            head=None, cost=0.25, tokens=1200, priced=True, repeats=None, untested=False,
            broken_class=None):
    metrics = {}
    if value is not None:
        reps = repeats or [value]
        metrics = {"value": {"median": value, "repeats": reps, "n_ok": len(reps), "spread": 0,
                             "untested": untested}}
    events.emit(conn, events.ATTEMPT, run=run, ref=task, op=OP, session=S,
                data={"outcome": outcome, "reason": reason, "broken_class": broken_class,
                      "metrics": metrics, "forked_from": forked, "head": head or forked,
                      "diff": {"sha256": "d" * 64}, "tokens": tokens, "cost_usd": cost,
                      "priced": priced, "alpaca_version": "test"})


def _round(conn, rnd, *, run=RUN, winner=None, commit=None, value=None, repair_task=None,
           ranking=()):
    events.emit(conn, events.ROUND, run=run, ref=run, op=OP, session=S,
                data={"round": rnd, "repair_task": repair_task, "winner": winner,
                      "ranking": list(ranking), "reason": "ranked", "champion_commit": commit,
                      "champion_metrics": {} if value is None else {"value": {"median": value}},
                      "accepted": 1 if winner else 0, "targets_met": False})


def _merge(conn, rnd, attempt, commit, parent, *, run=RUN):
    events.emit(conn, events.MERGE, run=run, ref=run, op=OP, session=S,
                data={"round": rnd, "repair_task": None, "attempt": attempt, "commit": commit,
                      "parent": parent, "research_branch": "research/%s" % run})


def _gate(conn, task, stage, verdict, rc=0, *, run=RUN):
    events.emit(conn, events.GATE, run=run, ref=task, op=OP, session=S,
                data={"stage": stage, "side": "attempt", "verdict": verdict, "rc": rc,
                      "offline": True, "input_sha256": "e" * 64})


def _tamper(conn, task, point, verdict, changed=(), *, run=RUN):
    events.emit(conn, events.TAMPER, run=run, ref=task, op=OP, session=S,
                data={"point": point, "verdict": verdict, "changed": list(changed),
                      "missing": []})


def _move(conn, task, to, reason=None):
    db.append_event(conn, session=S, actor="research-runner", kind="task-move", op=OP, ref=task,
                    data={"from": "doing", "to": to, "reason": reason})
    db.patch(conn, "tasks", "id", task, {"status": to})


def _claim(conn, task, worker=RUN):
    db.append_event(conn, session=S, actor=worker, kind="claim", op=OP, ref=task,
                    data={"worker": worker})
    db.patch(conn, "tasks", "id", task, {"status": "doing", "claimant": worker})


def _seal(conn, task, path=None):
    ev = db.append_event(conn, session=S, actor="agent", kind="proof-report", op=OP, ref=task,
                         data={"path": path or ".alpaca/proofs/%s/%s.md" % (OP, task),
                               "sha256": "f" * 64})
    return ev["id"]


def _hold(conn, gate, code, *, run=RUN, detail="hold"):
    return events.emit(conn, events.HOLD, run=run, ref=run, op=OP, session=S, reason=code,
                       data={"ref": run, "gate": gate, "detail": detail, "question": None,
                             "card": None})["id"]


def _lift(conn, hold, answer, decision="dec-1", *, run=RUN, gate="extension"):
    events.emit(conn, events.HOLD_LIFTED, run=run, ref=run, op=OP, session=S,
                data={"hold": hold, "decision": decision, "answer": answer, "gate": gate})


def _finalize(conn, st, reason, *, run=RUN):
    events.emit(conn, events.FINALIZE, run=run, ref=run, op=OP, session=S,
                data={"state": st, "reason": reason, "summary": {},
                      "log": ".alpaca/research/%s/runner.log" % run,
                      "env_snapshot": ".alpaca/research/%s/env.json" % run, "forensics": None,
                      "control_failed": False})


def improve_run(conn):
    """Four rounds of an improve run: a baseline of 20, a merged 15 in round 1 with a broken
    attempt that has no metric, a round 2 whose summary holds no champion value and a timed out
    attempt with no metric, and a merged 12 in round 3 forked from a commit that is not the
    previous champion (a lineage break), plus one attempt still running a gate."""
    _start(conn, n=4)
    c0, c1, c3 = "c0" * 20, "c1" * 20, "c3" * 20
    a0 = _task(conn, 0, 0)
    _record(conn, a0, "baseline", value=20)
    _round(conn, 0, winner=a0, commit=c0, value=20)
    a1, a2 = _task(conn, 1, 0, category="speed"), _task(conn, 1, 1, category="memory")
    _gate(conn, a1, "build", "PASS")
    _gate(conn, a1, "metric:value", "PASS")
    _tamper(conn, a1, "before-agents", "PASS")
    _tamper(conn, a1, "after-implementer", "PASS")
    _record(conn, a1, "improvement", value=15, forked=c0, head=c1, repeats=[14, 15, 16])
    _gate(conn, a2, "build", "FAIL", rc=1)
    _tamper(conn, a2, "after-implementer", "FAIL", changed=["tests/test_value.py"])
    _record(conn, a2, "broken", reason="gate build failed", forked=c0, broken_class="tamper")
    _merge(conn, 1, a1, c1, c0)
    _round(conn, 1, winner=a1, commit=c1, value=15)
    _seal(conn, a1)
    a3, a4 = _task(conn, 2, 0), _task(conn, 2, 1)
    _record(conn, a3, "regression", value=18, forked=c1)
    _record(conn, a4, "broken", reason="timeout", forked=c1, broken_class="timeout", cost=None,
            priced=False)
    _round(conn, 2, winner=None, commit=c1, value=None)
    a5, a6 = _task(conn, 3, 0), _task(conn, 3, 1)
    _record(conn, a5, "improvement", value=12, forked="c9" * 20, head=c3)
    _merge(conn, 3, a5, c3, c1)
    _round(conn, 3, winner=a5, commit=c3, value=12)
    _claim(conn, a6)
    _gate(conn, a6, "build", "PASS")
    return {"a0": a0, "a1": a1, "a2": a2, "a3": a3, "a4": a4, "a5": a5, "a6": a6}


# ============================================================ node and browser helpers
RENDER = r"""
import {readFileSync} from 'node:fs';
globalThis.document = {getElementById: () => null, head: {appendChild() {}},
                       createElement: () => ({}), addEventListener() {}};
globalThis.fetch = () => new Promise(() => {});
const research = await import(process.argv[2]);
const input = JSON.parse(readFileSync(process.argv[3], 'utf8'));
const out = {};
for (const [name, item] of Object.entries(input)) {
  if (item.kind === 'section') out[name] = research.renderResearchSection(item.data === '__undefined__' ? undefined : item.data);
  else out[name] = research.renderResearchPage(item.data, new URLSearchParams(item.params || {}), item.now || Date.parse('2026-10-01T00:00:00Z'));
}
process.stdout.write(JSON.stringify(out), () => process.exit(0));
"""


def render(tmp_path, items: dict) -> dict:
    if NODE is None:
        pytest.skip("node is not installed")
    runner = tmp_path / "render.mjs"
    runner.write_text(RENDER, encoding="utf-8")
    data = tmp_path / "render-input.json"
    data.write_text(json.dumps(items), encoding="utf-8")
    got = subprocess.run([NODE, str(runner), (WEB / "research.js").as_uri(), str(data)],
                         capture_output=True, text=True, timeout=60)
    assert got.returncode == 0, got.stderr
    return json.loads(got.stdout)


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest() if Path(path).exists() else None


def _wal_sha(dbfile):
    """The -wal file's content, None when it is absent or empty. A read-only connection may leave an
    empty -wal file beside the record (db.connect_readonly documents it), or find none, depending on
    the SQLite build and on when the earlier connection was closed: the 3.10 runner of 2026-10-03 read
    None before the view and the empty file after it (op-008 t-571). A write leaves content."""
    path = Path(str(dbfile) + "-wal")
    return _sha(path) if path.exists() and path.stat().st_size else None


def fetch(url, path, auth=None):
    req = urllib.request.Request(url + path)
    if auth:
        req.add_header("Authorization", "Basic " + base64.b64encode(auth.encode()).decode())
    try:
        r = urllib.request.urlopen(req, timeout=30)
    except urllib.error.HTTPError as exc:
        r = exc
    return r.status, r.headers, r.read()


@pytest.fixture
def hub_server(project):
    from alpaca import serve
    from alpaca.tests.test_hub_live_refresh import _start as start_hub
    db.connect(project).close()
    live = serve.Live(project)
    live.refresh()
    httpd, url = start_hub(live, project)
    yield project, url
    httpd.shutdown()
    httpd.server_close()


# ============================================================ the data
def test_the_view_never_opens_a_writer_and_writes_nothing(project, monkeypatch, tmp_path):
    conn = _conn(project)
    improve_run(conn)
    conn.close()
    dbfile = Path(project) / ".alpaca" / "alpaca.db"
    reader = db.connect_readonly(project)
    rows_before = reader.execute("SELECT COUNT(*), MAX(id) FROM events").fetchone()
    reader.close()
    before = (_sha(dbfile), _wal_sha(dbfile))

    def refuse(*a, **k):
        raise AssertionError("the view opened a writer connection")
    monkeypatch.setattr(db, "connect", refuse)
    listing = view.runs(project)
    one = view.run(project, RUN)
    assert [r["run"] for r in listing["runs"]] == [RUN] and one["run"]["run"] == RUN
    monkeypatch.undo()
    after = (_sha(dbfile), _wal_sha(dbfile))
    rows_after = db.connect_readonly(project).execute("SELECT COUNT(*), MAX(id) FROM events").fetchone()
    assert tuple(rows_before) == tuple(rows_after) and before == after
    # a project with no record yet: an empty list, and no record file is created
    empty = tmp_path / "empty"
    (empty / ".alpaca").mkdir(parents=True)
    assert view.runs(str(empty))["runs"] == []
    assert view.run(str(empty), "rr-001") is None
    assert not (empty / ".alpaca" / "alpaca.db").exists()


def test_the_run_object_carries_the_fold_headline_and_the_design_fields(project):
    conn = _conn(project)
    ids = improve_run(conn)
    got = view.run(project, RUN)
    assert got["schema"] == "research-view/1" and got["through_event"] >= 1 and got["as_of"]
    run = got["run"]
    head = state.fold(conn, RUN, root=project)
    for key in ("run", "mode", "state", "counts", "champion", "baseline", "budget", "started_at",
                "last_event_id", "level", "via"):
        assert run[key] == head[key], key
    for key in ("attempts", "rounds", "lineage", "chart", "repair", "holds", "grants", "budget",
                "sources"):
        assert key in run, key
    assert run["state"] == "running" and run["rounds_n"] == 4 and run["op"] == OP
    by = {a["task"]: a for a in run["attempts"]}
    assert by[ids["a6"]]["outcome_word"] == "running (stage build)"
    assert [s["task"] for s in run["running_slots"]] == [ids["a6"]]
    assert by[ids["a2"]]["first_failing_gate"] == "build"
    assert by[ids["a2"]]["tamper"]["broken"] and by[ids["a2"]]["tamper"]["changed"] == ["tests/test_value.py"]
    assert by[ids["a3"]]["tamper"]["verdict"] is None          # no fingerprint recorded, never PASS
    assert by[ids["a3"]]["audit"] is None                      # audit pending
    assert by[ids["a3"]]["gates"] == [{"stage": "build", "verdict": None, "not_run": True},
                                      {"stage": "metric:value", "verdict": None, "not_run": True}]
    assert by[ids["a1"]]["proof"]["sha256"] == "f" * 64 and by[ids["a1"]]["verify"]["state"] == "required"
    assert by[ids["a1"]]["winner"] and by[ids["a1"]]["metrics"]["value"]["repeats"] == [14.0, 15.0, 16.0]
    assert by[ids["a4"]]["priced"] is False and by[ids["a4"]]["cost_usd"] is None
    # newest round first, slots in order
    assert [(a["round"], a["slot"]) for a in run["attempts"]] == [(3, 0), (3, 1), (2, 0), (2, 1),
                                                                  (1, 0), (1, 1), (0, 0)]
    assert run["attempts_total"] == 7
    # an extension raises N: the view shows the newest research-extend rounds_to
    events.emit(conn, events.EXTEND, run=RUN, ref=RUN, op=OP, session=S,
                data={"decision": "d", "hold": 1, "rounds_from": 4, "rounds_to": 6})
    assert view.run(project, RUN)["run"]["rounds_n"] == 6


def test_the_attempt_list_is_capped_at_2000_newest_first(project, monkeypatch):
    conn = _conn(project)
    improve_run(conn)
    monkeypatch.setattr(view, "MAX_ATTEMPTS", 3)
    run = view.run(project, RUN)["run"]
    assert run["attempts_total"] == 7 and len(run["attempts"]) == 3
    assert all(a["round"] == 3 or a["round"] == 2 for a in run["attempts"])


def test_a_missing_metric_is_never_drawn_as_zero(project):
    conn = _conn(project)
    ids = improve_run(conn)
    chart = view.run(project, RUN)["run"]["chart"]["metrics"][0]
    plotted = {p["task"] for p in chart["points"]}
    strip = {m["task"] for m in chart["missing"]}
    assert plotted == {ids["a0"], ids["a1"], ids["a3"], ids["a5"]}
    assert strip == {ids["a2"], ids["a4"], ids["a6"]}
    assert not plotted & strip
    assert all(p["value"] != 0 for p in chart["points"])
    assert chart["caption"] == "3 attempts have no metric and are not plotted"
    assert {m["reason"] for m in chart["missing"]} >= {"gate build failed", "timeout"}
    assert chart["y"]["min"] > 0, chart["y"]          # no recorded value is 0, so 0 is not in range
    assert chart["y"]["min"] <= 10 <= chart["y"]["max"]  # the target is a recorded value


def test_the_y_range_includes_zero_only_for_a_zero():
    assert view._y_range([]) is None
    for values in ([1.0, 100.0], [5.0], [0.001, 3.0]):
        got = view._y_range(values)
        assert got["min"] > 0 and got["min"] <= min(values) and got["max"] >= max(values), values
    for values in ([-100.0, -1.0], [-5.0]):
        got = view._y_range(values)
        assert got["max"] < 0 and got["max"] >= max(values), values
    got = view._y_range([0.0, 4.0])
    assert got["min"] <= 0 <= got["max"]


def test_the_champion_line_breaks_at_a_round_with_no_value(project):
    conn = _conn(project)
    improve_run(conn)
    chart = view.run(project, RUN)["run"]["chart"]["metrics"][0]
    assert [c["value"] for c in chart["champion"]] == [20.0, 15.0, None, 12.0]
    assert [[c["round"] for c in seg] for seg in chart["champion_segments"]] == [[0, 1], [3]]
    assert all(c["value"] != 0 for seg in chart["champion_segments"] for c in seg)


def test_a_lineage_break_is_flagged(project):
    conn = _conn(project)
    ids = improve_run(conn)
    lineage = view.run(project, RUN)["run"]["lineage"]
    assert [n["round"] for n in lineage] == [0, 1, 3]
    assert [n["lineage_break"] for n in lineage] == [False, False, True]
    assert lineage[1]["change"]["value"] == -5.0 and lineage[0]["change"]["value"] is None
    assert lineage[2]["winner"] == ids["a5"] and lineage[2]["forked_from"] == "c9" * 20
    assert lineage[2]["previous_commit"] == "c1" * 20


def test_every_source_names_an_event_kind_or_a_table(project):
    conn = _conn(project)
    improve_run(conn)
    sources = view.run(project, RUN)["run"]["sources"]
    assert sources == view.SOURCES
    kinds = set(events.ALL_KINDS) | {"task-add", "task-move", "proof-report", "review-card",
                                     "review-move", "budget-spend", "claim"}
    for field, text in sources.items():
        named = set(re.findall(r"\b(?:research|task|proof|review|budget)-[a-z-]+[a-z]", text))
        assert named or "tasks table" in text, (field, text)
        assert named <= kinds, (field, named - kinds)
    posture = sources["budget.posture"]
    for key in ("budget-spend", "project.yaml", "resource_class", "budget.totals", "budget.reserve"):
        assert key in posture
    # the fields the design lists all have a source (operator-surface section 7.3)
    for field in ("state", "attempt.outcome", "attempt.metrics", "attempt.gates", "attempt.tamper",
                  "attempt.audit", "attempt.verifiers", "attempt.verify", "attempt.proof",
                  "lineage", "repair", "holds", "grants", "budget", "report", "stop", "finalize"):
        assert field in sources, field


# ============================================================ repair runs (section 7.8)
def repair_run(conn, *, ended=False):
    """Seven repair tasks, one per bucket case of section 7.8."""
    tasks = _start(conn, mode="repair", repair=7, targets=[])
    r = dict(zip(("resolved", "error", "unproved", "tree", "exhausted", "pending", "mismatch"), tasks))

    def result(task, outcome, attempt=None, head=None):
        events.emit(conn, events.REPAIR_RESULT, run=RUN, ref=task, op=OP, session=S,
                    data={"outcome": outcome, "fix_attempt": attempt, "head": head})
    for i, task in enumerate(tasks):
        events.emit(conn, events.PREPARE, run=RUN, ref=task, op=OP, session=S,
                    data={"ok": True, "head": "b%d" % i * 20})
        events.emit(conn, events.BASELINE, run=RUN, ref=task, op=OP, session=S,
                    data={"pass_to_pass": {"base_pass": ["t1", "t2", "t3"]}})
        events.emit(conn, events.REPRODUCER, run=RUN, ref=task, op=OP, session=S,
                    data={"repair_task": task, "proved": task != r["unproved"]})
    fixes = {}
    for name in ("resolved", "error", "tree", "exhausted", "pending", "mismatch"):
        fixes[name] = _task(conn, 1, 0, repair_task=r[name], mode="repair")
    _record(conn, fixes["resolved"], "fixed", head="f1" * 20)
    result(r["resolved"], "fixed", fixes["resolved"], "f1" * 20)
    _move(conn, r["resolved"], "done")
    for t in range(3):
        events.emit(conn, events.INFRA, run=RUN, ref=fixes["error"], op=OP, session=S,
                    data={"class": "quota", "try": t, "role": "implementer", "detail": "quota"})
    _record(conn, fixes["error"], "abandoned", reason="quota")
    result(r["unproved"], "reproducer-unproved")
    for name, code in (("tree", "VERIFY-TREE"), ("exhausted", "VERIFY-EXHAUSTED")):
        _record(conn, fixes[name], "fixed")
        result(r[name], "fixed", fixes[name], "fx" * 20)
        _move(conn, r[name], "blocked", "verification stopped: %s" % code)
    _record(conn, fixes["pending"], "fixed")
    result(r["pending"], "fixed", fixes["pending"], "fp" * 20)
    _claim(conn, r["pending"])
    _record(conn, fixes["mismatch"], "fixed")
    result(r["mismatch"], "fixed", fixes["mismatch"], "fm" * 20)
    _claim(conn, r["mismatch"])
    seal = _seal(conn, r["mismatch"])
    vf = "vf-%s-%d" % (r["mismatch"], seal)
    events.emit(conn, events.VERIFY_OPEN, run=RUN, ref=r["mismatch"], op=OP, session=S,
                data={"verification": vf, "seal_event": seal, "claims": []})
    events.emit(conn, events.VERIFY_ASSIGN, run=RUN, ref=r["mismatch"], op=OP, session=S,
                data={"verification": vf, "slot": "v1", "round": 0, "try": 1})
    events.emit(conn, events.VERIFY_REFUSED, run=RUN, ref=r["mismatch"], op=OP, session=S,
                reason="VERIFY-LOST", data={"verification": vf, "slot": "v1", "round": 0, "try": 1})
    events.emit(conn, events.VERIFY_REFUSED, run=RUN, ref=r["mismatch"], op=OP, session=S,
                reason="VERIFY-RUNTIME-MISMATCH", data={"verification": vf, "slot": None})
    if ended:
        _finalize(conn, "finished", "every repair task settled")
    return r


def test_the_resolved_rate_puts_each_repair_task_in_its_bucket(project, tmp_path):
    conn = _conn(project)
    r = repair_run(conn)
    run = view.run(project, RUN)["run"]
    rep = run["repair"]
    assert rep["buckets"] == {"resolved": [r["resolved"]], "error": [r["error"]],
                              "unresolved": [r["unproved"], r["tree"], r["exhausted"]],
                              "pending": [r["pending"], r["mismatch"]]}
    assert rep["rate"] == {"resolved": 1, "total": 7, "percent": 14, "so_far": True}
    items = {i["task"]: i for i in rep["items"]}
    # the first runtime-mismatch stop stays pending, with its code and its lost try
    mm = items[r["mismatch"]]["verify"]
    assert mm["state"] == "stopped" and mm["code"] == "VERIFY-RUNTIME-MISMATCH"
    assert mm["word"] == "stopped: VERIFY-RUNTIME-MISMATCH, sealing again" and mm["lost"] == ["v1/r0/t1"]
    # a blocked fix is unresolved with the code the runner's move gave, as the report reads it
    assert items[r["tree"]]["blocked_reason"] == "verification stopped: VERIFY-TREE"
    assert items[r["exhausted"]]["blocked_reason"] == "verification stopped: VERIFY-EXHAUSTED"
    # a fixed task not yet done is not resolved
    assert items[r["pending"]]["resolved"] is False and items[r["pending"]]["outcome"] == "fixed"
    assert items[r["error"]]["infra_classes"] == ["quota"]
    # the view's rate is the fold's count, the one place the report also reads (section 7.8)
    head = state.fold(conn, RUN)
    assert rep["rate"]["resolved"] == sum(1 for i in head["repair"] if i["resolved"])
    assert rep["rate"]["total"] == len(head["repair"])
    from alpaca.research import report
    try:
        built = report.build(conn, project, RUN)
    except NotImplementedError:
        built = None                      # t-105 builds report.build; the fold count stands in
    if built is not None:
        text = json.dumps(built)
        assert "1 of 7" in text or '"resolved": 1' in text
    page = render(tmp_path, {"p": {"data": view.run(project, RUN)}})["p"]
    assert "1 of 7 resolved (14 percent) so far" in page
    assert "verification stopped: VERIFY-TREE" in page and "verification stopped: VERIFY-EXHAUSTED" in page
    assert "stopped: VERIFY-RUNTIME-MISMATCH, sealing again" in page and "1 lost try" in page
    # once the run has ended, a task with no outcome would be unresolved; here every pending
    # task has outcome fixed, so the rate drops "so far" only when nothing is pending
    _move(conn, r["pending"], "done")
    _move(conn, r["mismatch"], "blocked", "verification stopped: VERIFY-RUNTIME-MISMATCH")
    rep = view.run(project, RUN)["run"]["repair"]
    assert rep["rate"] == {"resolved": 2, "total": 7, "percent": 29, "so_far": False}


def test_a_repair_task_with_no_outcome_is_unresolved_once_the_run_ends(project):
    conn = _conn(project)
    tasks = _start(conn, mode="repair", repair=1, targets=[])
    fix = _task(conn, 1, 0, repair_task=tasks[0], mode="repair")
    _record(conn, fix, "not-fixed")
    assert view.run(project, RUN)["run"]["repair"]["buckets"]["pending"] == tasks
    _finalize(conn, "finished", "rounds done")
    assert view.run(project, RUN)["run"]["repair"]["buckets"]["unresolved"] == tasks


def test_a_repair_run_with_no_repair_task_says_so_never_zero_percent(project, tmp_path):
    conn = _conn(project)
    _start(conn, mode="repair", repair=0, targets=[])
    run = view.run(project, RUN)["run"]
    assert run["repair"]["rate"] is None and run["repair"]["items"] == []
    out = render(tmp_path, {"p": {"data": view.run(project, RUN)},
                            "s": {"kind": "section", "data": view.runs(project)}})
    for page in out.values():
        assert "no repair tasks recorded" in page and "0 percent" not in page


def test_a_repair_run_has_no_champion_line(project, tmp_path):
    conn = _conn(project)
    r = repair_run(conn)
    run = view.run(project, RUN)["run"]
    assert run["champion"] is None and run["baseline"] is None and run["lineage"] == []
    assert all(m["champion"] == [] and m["champion_segments"] == [] for m in run["chart"]["metrics"])
    items = {i["task"]: i for i in run["repair"]["items"]}
    assert items[r["resolved"]]["fix"]["commit"] == "f1" * 20 and items[r["resolved"]]["base_pass"] == 3
    assert items[r["resolved"]]["base_commit"] == "b0" * 20
    page = render(tmp_path, {"p": {"data": view.run(project, RUN)}})["p"]
    assert "Champion lineage" not in page and "rs-champion" not in page
    assert ">Champion<" not in page and "Baseline" not in page
    for task in r.values():
        assert 'data-repair="%s"' % task in page
    assert "base pass 3" in page


def _repair_row(page, task):
    got = re.search(r'<tr data-repair="%s">(.*?)</tr>' % re.escape(task), page, re.S)
    assert got is not None, task
    return got.group(1)


def _cell(row, label):
    got = re.search(r'<td data-label="%s">(.*?)</td>(?=<td|$)' % re.escape(label), row, re.S)
    assert got is not None, (label, row[:400])
    return got.group(1)


def test_each_repair_row_shows_its_best_attempt_gates(project, tmp_path):
    """Section 7.8 (operator-surface.md:2118): one row per repair task with the reproducer, the
    best attempt, its gates and the verify state."""
    conn = _conn(project)
    r = repair_run(conn)
    items = {i["task"]: i for i in view.run(project, RUN)["run"]["repair"]["items"]}
    tree_fix, err_fix = items[r["tree"]]["best_attempt"], items[r["error"]]["best_attempt"]
    _gate(conn, tree_fix, "build", "PASS")
    _gate(conn, tree_fix, "test", "FAIL", rc=3)
    _gate(conn, err_fix, "build", "PASS")
    data = view.run(project, RUN)
    items = {i["task"]: i for i in data["run"]["repair"]["items"]}
    assert [g["stage"] for g in items[r["tree"]]["best_gates"]] == ["build", "test"]
    assert items[r["tree"]]["best_first_failing_gate"] == "test"
    page = render(tmp_path, {"p": {"data": data}})["p"]
    assert "<th>Gates</th><th>Verify</th>" in page
    gates = _cell(_repair_row(page, r["tree"]), "Gates")
    assert "test (first fail): FAIL" in gates and "build: PASS" in gates
    # the error task's best attempt never ran the test stage: the page says so, never a pass
    assert "test: not run" in _cell(_repair_row(page, r["error"]), "Gates")
    # a repair task with no fix attempt has no gates to show
    assert "no attempt" in _cell(_repair_row(page, r["unproved"]), "Gates")


def test_a_repair_task_never_verified_never_reads_verified(project, tmp_path):
    """An error or unresolved repair task carries the verify state the record holds (section 7.7),
    never the word "verified" it was never given."""
    conn = _conn(project)
    r = repair_run(conn)
    data = view.run(project, RUN)
    items = {i["task"]: i for i in data["run"]["repair"]["items"]}
    for name in ("error", "unproved", "tree", "exhausted", "pending", "mismatch", "resolved"):
        v = items[r[name]]["verify"]
        assert v is not None, name
        assert v["state"] != "verified", name          # no task of the fixture holds a verify-end
    assert items[r["error"]]["bucket"] == "error"
    assert items[r["error"]]["verify"]["state"] == "not-sealed"
    page = render(tmp_path, {"p": {"data": data}})["p"]
    for name in ("error", "unproved", "tree", "exhausted"):
        cell = _cell(_repair_row(page, r[name]), "Verify")
        assert ">verified<" not in cell, (name, cell)
    assert "not-sealed" in _cell(_repair_row(page, r["error"]), "Verify")
    # a row whose verify is missing reads "verification not started", never "verified"
    rep = data["run"]["repair"]
    rep["items"] = [dict(rep["items"][0], verify=None)]
    cell = _cell(_repair_row(render(tmp_path, {"p": {"data": data}})["p"], rep["items"][0]["task"]),
                 "Verify")
    assert "verification not started" in cell and ">verified<" not in cell


def test_a_ruled_verification_shows_the_ruling_and_the_verdict_claims(project, tmp_path):
    """Section 7.4 (operator-surface.md:1993): the verifier chips, or the ruling; section 7.3 keeps
    report_sha256 and claims on each verdict (operator-surface.md:1971)."""
    conn = _conn(project)
    _start(conn)
    a0 = _task(conn, 0, 0)
    _record(conn, a0, "baseline", value=20)
    _round(conn, 0, winner=a0, commit="c0" * 20, value=20)
    a1 = _task(conn, 1, 0)
    _record(conn, a1, "improvement", value=15)
    seal = _seal(conn, a1)
    vf = "vf-%s-%d" % (a1, seal)
    events.emit(conn, events.VERIFY_OPEN, run=RUN, ref=a1, op=OP, session=S,
                data={"verification": vf, "seal_event": seal, "claims": ["c1"]})
    events.emit(conn, events.PROOF_VERDICT, run=RUN, ref=a1, op=OP, session=S,
                data={"verification": vf, "verifier": "v1", "runtime": "claude", "round": 0,
                      "verdict": "REFUTED", "report_sha256": "7" * 64,
                      "claims": [{"id": "c1", "verdict": "REFUTED"}]})
    rule = events.emit(conn, events.PROOF_RULING, run=RUN, ref=a1, op=OP, session=S,
                       data={"verification": vf, "round": 0, "reason": "the claim c1 stands"})
    data = view.run(project, RUN)
    a = next(x for x in data["run"]["attempts"] if x["task"] == a1)
    assert a["verify"]["verifiers"][0]["claims"] == [{"id": "c1", "verdict": "REFUTED"}]
    assert a["verify"]["verifiers"][0]["report_sha256"] == "7" * 64
    assert a["verify"]["ruling"]["event"] == rule["id"]
    assert "report_sha256, claims" in data["run"]["sources"]["attempt.verifiers"]
    page = render(tmp_path, {"p": {"data": data}})["p"]
    row = re.search(r'<tr data-attempt="%s">(.*?)</tr>' % re.escape(a1), page, re.S).group(1)
    assert "ruling (event %d, round 0): the claim c1 stands" % rule["id"] in row
    # the ruling stands in place of the verifier chips
    assert "v1 r0: REFUTED" not in row


# ============================================================ ship hold card (section 7.3)
def test_a_ship_hold_whose_card_the_owner_moved_reads_moved_never_open(project, tmp_path):
    """operator-surface.md:1221-1223: between the owner's card move and holds.settle_ship the view
    already shows the card as moved; the holds row reads the card from review-card and
    review-move (operator-surface.md:1979)."""
    from alpaca import review
    conn = _conn(project)
    _start(conn, n=1)
    a0 = _task(conn, 0, 0)
    _record(conn, a0, "baseline", value=20)
    _round(conn, 0, winner=a0, commit="c0" * 20, value=20)
    _finalize(conn, "finished", "rounds done")
    card = review.ship_card(conn, "research:%s:champion" % RUN, "ship the champion?", op=OP,
                            session=S)["id"]
    hold = events.emit(conn, events.HOLD, run=RUN, ref=RUN, op=OP, session=S,
                       reason="RUN-SHIP-DECISION",
                       data={"ref": RUN, "gate": "ship", "detail": "ship the champion",
                             "question": None, "card": card})["id"]
    before = view.run(project, RUN)
    h = before["run"]["holds"][0]
    assert (h["status"], h["card_status"], h["card_decision"]) == ("open", "open", None)
    move = review.move(conn, card, "ship it", "owner")
    after = view.run(project, RUN)
    h = after["run"]["holds"][0]
    assert after["run"]["open_holds"] == []            # the fold already drops it
    assert (h["status"], h["card_status"], h["card_decision"]) == ("card moved", "moved", "ship it")
    assert h["lifted"] is None and isinstance(h["card_move_event"], int)
    move_ev = conn.execute("SELECT id FROM events WHERE kind='review-move' AND ref=?",
                           (card,)).fetchone()["id"]
    assert h["card_move_event"] == move_ev and move["status"] == "moved"
    out = render(tmp_path, {"before": {"data": before}, "after": {"data": after}})
    assert "Ship decision: open on card %s" % card in out["before"]
    assert "Ship decision: card %s moved: ship it" % card in out["after"]
    assert "open on card" not in out["after"] and "&middot; open<" not in out["after"]
    # settle_ship's lift then reads lifted
    _lift(conn, hold, "ship", decision="dec-ship", gate="ship")
    h = view.run(project, RUN)["run"]["holds"][0]
    assert h["status"] == "lifted" and h["lifted"]["decision"] == "dec-ship"


# ============================================================ run states (section 7.9)
def _state_fixture(conn, name):
    if name == "starting":
        _start(conn)
        return
    _start(conn, control="noop" if name == "control failed" else None)
    a = _task(conn, 0, 0)
    _record(conn, a, "baseline", value=20)
    if name == "control failed":
        b = _task(conn, 1, 0)
        _record(conn, b, "improvement", value=15)
    elif name == "paused":
        _hold(conn, "budget", "RUN-BUDGET-HOLD", detail="the dollar cap is reached")
    elif name == "halted":
        _hold(conn, "extension", "RUN-ROUNDS-REACHED", detail="round 3 of 3")
        _finalize(conn, "halted", "round limit")
    elif name == "resumable":
        h = _hold(conn, "extension", "RUN-ROUNDS-REACHED")
        _finalize(conn, "halted", "round limit")
        _lift(conn, h, "extension", decision="dec-extend")
    elif name == "interrupted":
        _finalize(conn, "interrupted", "a signal with no recorded stop")
    elif name == "stopped":
        events.emit(conn, events.STOP, run=RUN, ref=RUN, op=OP, session="owner-chat",
                    data={"reason": "enough", "final": True, "pid_signalled": None, "decision": None})
    elif name == "failed":
        _finalize(conn, "failed", "the gate chain could not start")
    elif name == "finished":
        _finalize(conn, "finished", "targets met")
        events.emit(conn, events.REPORT, run=RUN, ref=RUN, op=OP, session=S,
                    data={"path": ".alpaca/reports/research/%s/report.md" % RUN,
                          "sha256": "9" * 64, "json_path": None, "state": "finished"})


BANNERS = {"starting": "Measuring the baseline (round 0)", "running": "Running: round",
           "paused": "Paused at a budget hold", "halted": "targets not met; owner extension owed",
           "resumable": "bin/alpaca research resume rr-001",
           "interrupted": "The loop runner stopped without a recorded stop",
           "stopped": "Stopped by session", "failed": "the gate chain could not start",
           "finished": "Open the run report", "control failed": "control failed"}


def test_each_run_state_renders_its_banner(project, tmp_path, monkeypatch):
    items, expect = {}, {}
    for name in BANNERS:
        root = tmp_path / ("p-" + name.replace(" ", "-"))
        (root / ".alpaca" / "config").mkdir(parents=True)
        (root / "style").mkdir()
        (root / "style" / "banned.txt").write_text("", encoding="utf-8")
        shutil.copy(Path(project) / "ALPACA-MANIFEST", root / "ALPACA-MANIFEST")
        monkeypatch.chdir(root)
        conn = _conn(str(root))
        _state_fixture(conn, name)
        conn.close()
        data = view.run(str(root), RUN)
        expect[name] = data["run"]["state"]
        items[name] = {"data": data}
        items[name + ":list"] = {"kind": "section", "data": view.runs(str(root))}
    assert expect == {"starting": "starting", "running": "running", "paused": "paused",
                      "halted": "halted", "resumable": "resumable", "interrupted": "interrupted",
                      "stopped": "stopped", "failed": "failed", "finished": "finished",
                      "control failed": "running"}
    empty = tmp_path / "empty"
    (empty / ".alpaca").mkdir(parents=True)
    items["empty"] = {"data": view.runs(str(empty))}
    items["empty:section"] = {"kind": "section", "data": view.runs(str(empty))}
    out = render(tmp_path, items)
    for name, text in BANNERS.items():
        assert text in out[name], (name, out[name][:1500])
        assert 'data-banner="' in out[name]
        assert RUN in out[name + ":list"]
    assert "No research run is recorded" in out["empty"] and "bin/alpaca research start" in out["empty"]
    assert "No research run is recorded" in out["empty:section"]
    # a failed run keeps its partial table and chart with their real values
    assert 'data-attempt="' in out["failed"] and "rs-dot" in out["failed"]
    assert "Runner log" in out["failed"] and "Environment snapshot" in out["interrupted"]
    assert "dec-extend" in out["resumable"]
    # a starting run draws an empty chart with the note and no axis
    assert "rs-chart-empty" in out["starting"] and "rs-tick" not in out["starting"]
    # the control badge: a control run with an accepted attempt reads "control failed"
    assert "tone-danger" in out["control failed"]


def test_a_stale_running_run_says_so_as_an_observation(project, tmp_path):
    conn = _conn(project)
    _start(conn)
    _record(conn, _task(conn, 0, 0), "baseline", value=3)
    data = view.run(project, RUN)
    import datetime
    last = datetime.datetime.fromisoformat(data["run"]["last_event_at"].replace("Z", "+00:00"))
    later = int((last + datetime.timedelta(minutes=25)).timestamp() * 1000)
    soon = int((last + datetime.timedelta(minutes=2)).timestamp() * 1000)
    out = render(tmp_path, {"late": {"data": data, "now": later}, "soon": {"data": data, "now": soon}})
    assert "No record activity for 25 minutes" in out["late"] and "not a diagnosis" in out["late"]
    assert "No record activity" not in out["soon"]


@pytest.mark.parametrize("gate,code,final", [("extension", "RUN-ROUNDS-REACHED", "halted"),
                                             ("budget", "RUN-BUDGET-HOLD", "paused")])
def test_a_run_the_owner_stopped_at_a_hold_reads_stopped_never_resumable(project, tmp_path, gate,
                                                                         code, final):
    conn = _conn(project)
    _start(conn)
    _record(conn, _task(conn, 0, 0), "baseline", value=20)
    h = _hold(conn, gate, code)
    _finalize(conn, final, "hold")
    events.emit(conn, events.GRANT, run=RUN, ref=RUN, op=OP, session="owner-terminal",
                data={"ref": RUN, "gate": "stop", "hold": h, "decision": "dec-stop-1",
                      "terms": {}, "note": "", "confirmed": "terminal", "by": "owner"})
    _lift(conn, h, "stop", decision="dec-stop-1", gate=gate)
    events.emit(conn, events.STOP, run=RUN, ref=RUN, op=OP, session="owner-terminal",
                data={"reason": "owner stop at the hold", "final": True, "pid_signalled": None,
                      "decision": "dec-stop-1"})
    data = view.run(project, RUN)
    assert data["run"]["state"] == "stopped"
    assert data["run"]["grants"][0]["decision"] == "dec-stop-1"
    page = render(tmp_path, {"p": {"data": data}})["p"]
    assert "Stopped by session" in page and "dec-stop-1" in page
    assert "stopped the run at its %s hold" % gate in page
    assert "research resume" not in page and "Resumable" not in page


# ============================================================ web route, assets, wiring
def test_the_route_answers_the_schema_a_bad_id_and_an_unknown_run(hub_server):
    project, url = hub_server
    conn = db.connect(project)
    cli.main(["init"])
    assert plain_op("research work", "runs read")
    improve_run(conn)
    status, headers, body = fetch(url, "/hub/research.json")
    assert status == 200 and "application/json" in headers["Content-Type"]
    got = json.loads(body)
    assert got["schema"] == "research-view/1" and [r["run"] for r in got["runs"]] == [RUN]
    status, _, body = fetch(url, "/hub/research.json?run=" + RUN)
    assert status == 200 and json.loads(body)["run"]["run"] == RUN
    for bad in ("../etc", "RR-1", "rr-1", "x" * 80):
        status, _, body = fetch(url, "/hub/research.json?run=" + bad)
        assert status == 400, bad
        assert project.encode() not in body
    status, _, body = fetch(url, "/hub/research.json?run=rr-999")
    assert status == 404 and json.loads(body) == {"error": "Research run not found in this project"}
    from alpaca import serve
    assert "/hub/research.json" in serve.GENERIC_PATHS


def test_the_new_assets_are_served_and_ascii(hub_server):
    _, url = hub_server
    for name, mime in (("research.js", "javascript"), ("research.css", "text/css")):
        status, headers, body = fetch(url, "/hub/assets/" + name)
        assert status == 200 and mime in headers["Content-Type"] and body.isascii(), name
        assert chr(0x2014).encode() not in body
    status, _, body = fetch(url, "/hub/")
    assert status == 200 and body.isascii()
    assert body.index(b"research.css") < body.index(b"theme.css")
    assert body.index(b"theme.js") < body.index(b"hub.css") < body.index(b"theme.css")
    for path in (WEB / "research.js", WEB / "research.css"):
        assert path.read_bytes().isascii()


def test_the_hub_interval_and_router_call_the_research_view():
    hub = (WEB / "hub.js").read_text(encoding="utf-8")
    # the 15 s timer and the quiet refresh take their pages from the registry (pages.json)
    page = {p["id"]: p for p in json.loads((WEB / "pages.json").read_text(encoding="utf-8"))["pages"]}["research"]
    assert page["label"] == "Research" and page["timer"] is True and page["quiet"] is True and not page.get("gate")
    interval = next(line for line in hub.splitlines() if line.startswith("setInterval(async()=>{")
                    and "liveOverviewBusy=true" in line)
    assert "!PAGE[state.route]?.timer" in interval and "await VIEWS[state.route](ticket,true)" in interval
    views = next(line for line in hub.splitlines() if line.startswith("const VIEWS={"))
    assert "research:researchView" in views and "overview" in views
    router = next(line for line in hub.splitlines() if line.startswith("async function route("))
    # a built-in page renders through VIEWS (a project's own page through slotView), quiet per its registry entry
    assert "(VIEWS[state.route]||slotView)(ticket,quiet&&pageOf(state.route).quiet===true)" in router
    assert "const pageOf=id=>PAGE[id]||" in hub
    view_fn = hub[hub.index("async function researchView("):]
    view_fn = view_fn[:view_fn.index("\n}\n")]
    assert "api('/hub/research.json'" in view_fn          # re-fetched on every render
    # Cockpit's delayed and failed research reads are exercised by test_hub_navigation.
    cockpit = (WEB / "cockpit.js").read_text(encoding="utf-8")
    assert cockpit.index("renderResearchSection(o.research)") < cockpit.index("renderMission(o.mission")


def test_route_needs_research_depends_on_overview(tmp_path):
    if NODE is None:
        pytest.skip("node is not installed")
    test = tmp_path / "flags.test.mjs"
    test.write_text(textwrap.dedent("""
        import test from 'node:test';
        import assert from 'node:assert/strict';
        const lf = await import(%r);
        const blob = o => JSON.stringify({data:'d1', lib:'l1', board:'b1', pricing:'', ...o});
        test('the research page reads the overview cache', () => {
          assert.deepEqual(lf.ROUTE_NEEDS.research, ['overview']);
          assert.deepEqual(lf.needs('research'), ['overview']);
        });
        test('a board revision refreshes the research page', () => {
          const marked = lf.changedFlags(blob({}), blob({board:'b2'}));
          assert.deepEqual(marked, ['overview','runs']);
          assert.equal(lf.dependsOn('research', marked), true);
          assert.equal(lf.autoRefresh('research', marked), true);
        });
        test('no new flag or surface', () => {
          assert.deepEqual(lf.FLAGS, ['overview','runs','sessions','analytics','library']);
          assert.deepEqual(lf.SURFACES.board, ['overview','runs']);
        });
    """ % (WEB / "liveflags.js").as_uri()), encoding="utf-8")
    got = subprocess.run([NODE, "--test", str(test)], capture_output=True, text=True, timeout=60)
    assert got.returncode == 0, got.stdout + got.stderr


def test_the_cockpit_section_without_data_says_unavailable(tmp_path):
    out = render(tmp_path, {"undef": {"kind": "section", "data": "__undefined__"},
                            "null": {"kind": "section", "data": None},
                            "page": {"data": None}})
    for name in ("undef", "null"):
        assert "research runs unavailable" in out[name] and 'id="research-section"' in out[name]
    assert "research runs unavailable" in out["page"]
    runner = tmp_path / "cockpit.mjs"
    runner.write_text(textwrap.dedent('''
        globalThis.document = {getElementById: () => null, head: {appendChild() {}},
                               createElement: () => ({}), addEventListener() {}};
        globalThis.fetch = () => new Promise(() => {});
        const cockpit = await import(process.argv[2]);
        const o = {profile: {}, tasks: [], ops: [], items: [], stages: [], active_jobs: [],
                   counts: {items_pass: 0}, capture: {status: 'ok', errors: []}};
        process.stdout.write(cockpit.renderCockpit(o, {items: []}, {items: []}, {}));
        process.exit(0);
    '''), encoding="utf-8")
    got = subprocess.run([NODE, str(runner), (WEB / "cockpit.js").as_uri()], capture_output=True,
                         text=True, timeout=60)
    assert got.returncode == 0, got.stderr
    assert "research runs unavailable" in got.stdout
    # the research section sits after the operation progress and before the mission card
    assert got.stdout.index("operation-progress") < got.stdout.index("research-section")


def test_the_cockpit_card_lists_live_runs_and_the_three_newest_finished(project, tmp_path):
    conn = _conn(project)
    for i in range(1, 6):
        run = "rr-%03d" % i
        _start(conn, run)
        _record(conn, _task(conn, 0, 0, run=run), "baseline", run=run, value=10 + i)
        if i <= 4:
            _finalize(conn, "finished", "done", run=run)
    data = view.runs(project)
    assert [r["run"] for r in data["runs"]] == ["rr-005", "rr-004", "rr-003", "rr-002", "rr-001"]
    out = render(tmp_path, {"s": {"kind": "section", "data": data}})["s"]
    shown = re.findall(r'data-run="(rr-\d+)"', out)
    assert shown == ["rr-005", "rr-004", "rr-003", "rr-002"]


def test_the_report_link_opens_through_the_library_route(hub_server, tmp_path):
    project, url = hub_server
    cli.main(["init"])
    assert plain_op("research work", "runs read")
    conn = db.connect(project)
    _start(conn)
    _record(conn, _task(conn, 0, 0), "baseline", value=20)
    _finalize(conn, "finished", "targets met")
    rel = ".alpaca/reports/research/%s/report.md" % RUN
    path = Path(project) / rel
    path.parent.mkdir(parents=True)
    path.write_text("# Research run rr-001\n\nThe run finished.\n", encoding="utf-8")
    events.emit(conn, events.REPORT, run=RUN, ref=RUN, op=OP, session=S,
                data={"path": rel, "sha256": _sha(path), "json_path": None, "state": "finished"})
    data = view.run(project, RUN)
    assert data["run"]["report"]["path"] == rel
    Path(project, ".alpaca/files-auth").write_text("reader:test-password")
    status, _, body = fetch(url, "/hub/document.json?path=" + rel, "reader:test-password")
    assert status == 200 and "The run finished." in json.loads(body)["text"]
    page = render(tmp_path, {"p": {"data": data}})["p"]
    assert 'href="#library?path=.alpaca%2Freports%2Fresearch%2Frr-001%2Freport.md"' in page


# ============================================================ in a browser
CHART_SCRIPT = r"""
const checks = {};
const assert = (name, test) => {checks[name] = Boolean(test);};
const publish = value => {
  const text = JSON.stringify(value);
  document.querySelector('#results').textContent = text;
  if (parent !== window) parent.document.querySelector('#results').textContent = text;
};
// The overflow probe (t-162): the elements whose right edge passes the viewport, widest first,
// each named by tag, class and a text sample, so a failed fit check names the element that pushed
// the page wider than the viewport. An element inside a clipping box (overflow other than visible,
// an SVG root included) cannot widen the page, so only the clipping box itself can be reported.
const clipped = el => {
  for (let up = el.parentElement; up && up !== document.body; up = up.parentElement) {
    if (getComputedStyle(up).overflowX !== 'visible') return true;
  }
  return false;
};
const overflowing = () => [...document.body.querySelectorAll('*')]
  .map(el => ({el, r: el.getBoundingClientRect()}))
  .filter(({el, r}) => r.right > innerWidth + 1 && !clipped(el))
  .sort((a, b) => b.r.right - a.r.right)
  .slice(0, 8)
  .map(({el, r}) => ({tag: el.tagName.toLowerCase(), cls: String(el.getAttribute('class') || ''),
    right: Math.round(r.right), width: Math.round(r.width), scrollWidth: el.scrollWidth,
    text: (el.textContent || '').trim().slice(0, 80)}));
try {
  const {renderResearchPage, bindResearch} = await import('/research.js');
  const main = document.querySelector('#app');
  main.innerHTML = renderResearchPage(DATA, new URLSearchParams({run: 'rr-001'}));
  bindResearch(main);
  const chart = DATA.run.chart.metrics[0];
  const svg = main.querySelector('.rs-chart svg');
  const dots = [...svg.querySelectorAll('.rs-dot')];
  assert('chart stays compact', svg.getBoundingClientRect().height <= 240);
  assert('one dot per recorded value', dots.length === chart.points.length);
  assert('no dot carries a missing attempt', dots.every(d => !chart.missing.some(m => m.task === d.dataset.task)));
  assert('no dot at the value 0', dots.every(d => Number(d.dataset.value) !== 0));
  const plot = svg.querySelector('.rs-plot').getBBox();
  assert('every dot inside the plot', dots.every(d => {const b = d.getBBox(); return b.y >= plot.y - 1 && b.y + b.height <= plot.y + plot.height + 1;}));
  const ticks = [...svg.querySelectorAll('.rs-missing')];
  assert('one tick per missing metric', ticks.length === chart.missing.length && ticks.length === 3);
  assert('ticks sit under the x axis', ticks.every(t => t.getBBox().y > plot.y + plot.height));
  assert('caption counts them', main.querySelector('.rs-caption').textContent.includes('3 attempts have no metric and are not plotted'));
  assert('champion line in two segments', svg.querySelectorAll('.rs-champion').length === 2);
  assert('champion line stays above the axis floor', [...svg.querySelectorAll('.rs-champion')].every(p => {const b = p.getBBox(); return b.y + b.height <= plot.y + plot.height + 1;}));
  assert('target drawn', svg.querySelectorAll('.rs-target').length === 1);
  assert('winner rings', svg.querySelectorAll('.rs-ring').length === chart.points.filter(p => p.winner).length);
  assert('aria label names the champion', svg.getAttribute('role') === 'img' && svg.getAttribute('aria-label').includes('first 20') && svg.getAttribute('aria-label').includes('last 12'));
  assert('lineage break flagged', main.querySelectorAll('.rs-break').length === 1 && main.textContent.includes('lineage break'));
  assert('one row per attempt', main.querySelectorAll('tr[data-attempt]').length === DATA.run.attempts.length);
  assert('a missing metric reads no metric in the table', [...main.querySelectorAll('.rs-nometric')].length === 3);
  assert('every attempt opens its research reader', DATA.run.attempts.every(a => [...main.querySelectorAll('a[href]')].some(link => new URLSearchParams((link.getAttribute('href') || '').split('?')[1] || '').get('candidate') === a.task)));
  assert('column titles name their sources', main.querySelector('th[title^="Source: research-attempt"]') !== null);
  assert('proof link through the library', main.querySelector('a[href^="#library?path="]') !== null);
  const narrow = innerWidth <= 700;
  const head = getComputedStyle(main.querySelector('.rs-table thead')).display;
  assert('rows become cards below 700 px', narrow ? head === 'none' : head !== 'none');
  // WIDEN appends one element twice the viewport wide, the probe's own check (t-162).
  if (WIDEN) main.insertAdjacentHTML('beforeend', '<div class="t162-widen" style="width:' + (2 * WIDTH) + 'px;height:4px">widen</div>');
  assert('All runs action remains readable', parseFloat(getComputedStyle(main.querySelector('.rs-heading > .button')).fontSize)>=12);
  assert('the viewport is the width asked', innerWidth === WIDTH);
  const scrollWidth = document.documentElement.scrollWidth;
  assert('page fits the viewport', scrollWidth <= innerWidth + 1);
  publish({ok: Object.values(checks).every(Boolean), checks, innerWidth, scrollWidth,
           widest: scrollWidth <= innerWidth + 1 ? [] : overflowing()});
} catch (error) {publish({ok: false, checks, innerWidth, error: String(error)});}
"""


def _chart_in_browser(project, tmp_path, width, *, widen=False):
    """Render the improve run's page at `width` px in headless Chrome and return CHART_SCRIPT's
    outcome. Headless Chrome started with --dump-dom often lays the top-level page out in a
    viewport 0 px wide (innerWidth 0 for the page's whole life, whatever --window-size says) and
    never narrower than its 500 px minimum window, so the fit check measured nothing real and
    failed or passed by chance (t-162). The page is measured inside an iframe exactly `width` px
    wide, whose viewport is the iframe box, as test_mission_web.py does."""
    conn = _conn(project)
    improve_run(conn)
    data = view.run(project, RUN)
    page = ('<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" '
            'content="width=device-width,initial-scale=1"><link rel="stylesheet" href="/hub.css">'
            '<link rel="stylesheet" href="/cockpit.css"><link rel="stylesheet" href="/research.css">'
            '<link rel="stylesheet" href="/theme.css"></head><body><main id="app" '
            'style="padding:16px;min-width:0;max-width:1300px"></main><pre id="results" '
            'style="white-space:pre-wrap;overflow-wrap:anywhere">pending</pre><script type="module">'
            + "const WIDTH=%d;const WIDEN=%s;const DATA=" % (width, "true" if widen else "false")
            + json.dumps(data).replace("<", "\\u003c") + ";" + CHART_SCRIPT
            + "</script></body></html>")
    outer = ('<!doctype html><html><head><meta charset="utf-8"></head><body style="margin:0">'
             '<iframe src="/fixture" style="display:block;border:0;width:%dpx;height:1000px">'
             '</iframe><pre id="results">pending</pre></body></html>' % width)

    class Handler(http.server.SimpleHTTPRequestHandler):
        def do_GET(self):
            if self.path in ("/outer", "/fixture"):
                body = (outer if self.path == "/outer" else page).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(body)
            else:
                super().do_GET()

        def log_message(self, *args):
            pass
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0),
                                             functools.partial(Handler, directory=str(WEB)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        result = subprocess.run([CHROME, "--headless=new", "--no-sandbox", "--disable-gpu",
                                 "--no-first-run", "--password-store=basic",
                                 "--no-default-browser-check",
                                 "--user-data-dir=" + str(tmp_path / "chrome"),
                                 "--window-size=%d,1000" % width, "--virtual-time-budget=8000",
                                 "--dump-dom", "http://127.0.0.1:%d/outer" % server.server_port],
                                text=True, capture_output=True, timeout=60)
    finally:
        server.shutdown()
        server.server_close()
    match = re.search(r'<pre id="results">(.*?)</pre>', result.stdout, re.S)
    assert match, result.stderr[-1000:]
    observed = html.unescape(match.group(1))
    assert observed != "pending", result.stderr[-1000:]
    return json.loads(observed)


# 160 px is a 320 px phone at 200 percent zoom: the facts grid track min(150px, 100%) in
# research.css keeps it inside the viewport, where a fixed 150 px track pushed the page to 166 px
# (t-162).
@pytest.mark.parametrize("width", [1440, 768, 414, 390, 375, 320, 160])
def test_the_chart_in_a_browser_plots_only_recorded_values(project, tmp_path, width):
    if not CHROME:
        pytest.skip("headless Chrome not available")
    outcome = _chart_in_browser(project, tmp_path, width)
    assert outcome["ok"], json.dumps(outcome, indent=2)
    assert outcome["innerWidth"] == width and outcome["widest"] == []


def test_the_overflow_probe_names_the_element_that_widens_the_page(project, tmp_path):
    """t-162: when the page is wider than the viewport the fit check fails and the probe names
    the element that pushed it, and only that one: the chart's SVG and the table sit inside
    boxes that fit, so they are not reported."""
    if not CHROME:
        pytest.skip("headless Chrome not available")
    outcome = _chart_in_browser(project, tmp_path, 390, widen=True)
    assert outcome["checks"]["the viewport is the width asked"], json.dumps(outcome, indent=2)
    assert outcome["checks"]["page fits the viewport"] is False
    assert outcome["ok"] is False and outcome["scrollWidth"] > 390
    widest = outcome["widest"]
    assert widest and widest[0]["cls"] == "t162-widen", json.dumps(outcome, indent=2)
    assert widest[0]["width"] == 780 and widest[0]["text"] == "widen"
    assert len(widest) == 1, json.dumps(widest, indent=2)


def test_the_real_hub_draws_the_research_page_and_refreshes_it(project, tmp_path):
    from urllib.parse import urlsplit
    from alpaca import serve
    from alpaca.tests.test_hub_live_refresh import HUB_DRIVER, CHROME_FLAGS
    if not CHROME or not NODE:
        pytest.skip("headless Chrome and node are required")
    conn = _conn(project)
    ids = improve_run(conn)
    conn.close()
    live = serve.Live(project)
    live.refresh()
    control = {"sse": 0}
    base = serve.make_handler(live, project)

    class Handler(base):
        def _sse(self):
            control["sse"] += 1
            return super()._sse()

        def do_GET(self):
            url = urlsplit(self.path)
            if url.path == "/__ctl":
                if url.query == "act=finish":
                    c = db.connect(project)
                    _record(c, ids["a6"], "improvement", value=11)
                    _move(c, ids["a6"], "done")
                    c.close()
                    live.refresh()
                return self._json(control)
            return super().do_GET()

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    a6 = ids["a6"]
    driver = HUB_DRIVER.split("try {\n  await send('Runtime.enable');")[0] + r"""
try {
  await send('Runtime.enable');
  await load('#research?run=rr-001', `!!document.querySelector('tr[data-attempt]')`);
  check('research page drawn by the hub', await js(`document.querySelectorAll('tr[data-attempt]').length===7`));
  check('navigation lists Research', await js(`[...document.querySelectorAll('#navigation a')].some(a=>a.getAttribute('href')==='#research')`));
  check('running attempt shown', await js(`document.querySelector('tr[data-attempt="%(a6)s"]').textContent.includes('running (stage build)')`));
  check('chart drawn without a zero', await js(`[...document.querySelectorAll('.rs-dot')].every(d=>Number(d.dataset.value)!==0)&&document.querySelectorAll('.rs-missing').length===3`));
  await js(`document.querySelector('[data-candidate-row="%(a6)s"] a').click();true`);
  check('candidate row opens its reader', await until(`!!document.querySelector('[data-candidate-reader="%(a6)s"]')`));
  check('reader gets focus on explicit selection', await js(`document.activeElement?.dataset.candidateReader==='%(a6)s'`));
  await js(`document.querySelector('.rs-svg a').focus();true`);
  await send('Input.dispatchKeyEvent',{type:'keyDown',key:'Enter',code:'Enter',windowsVirtualKeyCode:13});
  await send('Input.dispatchKeyEvent',{type:'keyUp',key:'Enter',code:'Enter',windowsVirtualKeyCode:13});
  check('chart opens a candidate with the keyboard', await until(`!!document.querySelector('[data-candidate-reader]')&&document.querySelector('[data-candidate-reader]').dataset.candidateReader!=='%(a6)s'`));
  await js(`document.querySelector('[data-candidate-row="%(a1)s"] a').click();true`);
  check('elected candidate exposes proof', await until(`document.querySelector('[data-candidate-reader="%(a1)s"]')?.textContent.includes('Open proof')`));
  await js(`document.querySelector('[data-candidate-reader] button[data-task]').click();true`);
  check('separate task action opens inspector', await until(`document.querySelector('#inspector').open`));
  await js(`document.querySelector('#inspector').close();document.activeElement?.blur?.();document.querySelector('[data-rs-disclosure="rr-001:technical"]').open=true;true`);
  await sleep(100);
  await js(`document.activeElement?.blur?.();true`);
  await ctl('act=finish');
  check('a new record event redraws the page', await until(`document.querySelector('tr[data-attempt="%(a6)s"]')?.textContent.includes('improvement')`, 30000));
  check('the redraw kept the run open', await js(`location.hash.includes('run=rr-001')&&document.querySelectorAll('.rs-missing').length===2`));
  check('candidate selection and disclosure survive refresh', await js(`location.hash.includes('candidate=%(a1)s')&&!!document.querySelector('[data-candidate-reader="%(a1)s"]')&&document.querySelector('[data-rs-disclosure="rr-001:technical"]').open`));
  await load('#research', `!!document.querySelector('.rs-card')`);
  check('run list drawn', await js(`!!document.querySelector('.rs-card[data-run="rr-001"]')`));
  await load('#cockpit', `!!document.querySelector('#research-section')`);
  check('cockpit research section', await js(`document.querySelector('#research-section').textContent.includes('rr-001')`));
  check('cockpit keeps the mission card after it', await js(`(()=>{const s=document.querySelector('#research-section');return !!s&&!!document.querySelector('.cockpit-hero, .operation-progress');})()`));
} catch(e){out.push('FAIL exception '+e.message);}
console.log(out.join('\n'));ws.close();chrome.kill();
process.exit(out.some(line=>line.startsWith('FAIL'))?1:0);
""" % {"a6": a6, "a1": ids["a1"]}
    path = tmp_path / "research_driver.mjs"
    path.write_text(driver, encoding="utf-8")
    try:
        got = subprocess.run([NODE, str(path), "http://127.0.0.1:%d" % server.server_port,
                              str(tmp_path / "chrome")], capture_output=True, text=True,
                             timeout=120, env=dict(os.environ, CHROME=CHROME,
                                                   CHROME_FLAGS=json.dumps(CHROME_FLAGS)))
        assert got.returncode == 0, got.stdout + got.stderr
        assert got.stdout.count("PASS") == 15, got.stdout
    finally:
        server.shutdown()
        server.server_close()


# ============================================================ the kept run summaries (t-593)
def _uncached_runs(project):
    view._KEPT.clear()
    return view.runs(project)


def test_a_new_event_of_one_run_rebuilds_only_that_run_and_shows(project):
    """t-593: runs() keeps each run's summary; an event of the run, of one of its tasks or naming
    it in its data reaches the next read, and a run it does not concern is not built again."""
    conn = _conn(project)
    _start(conn, "rr-001")
    _start(conn, "rr-002")
    first, second = _task(conn, 1, 0, run="rr-001"), _task(conn, 1, 0, run="rr-002")
    _record(conn, first, "accepted", run="rr-001", value=5.0)
    _record(conn, second, "accepted", run="rr-002", value=6.0)
    before = {r["run"]: r for r in view.runs(project)["runs"]}
    kept = dict(view._KEPT[str(project)])
    # an event on a task of rr-002
    third = _task(conn, 2, 0, run="rr-002")
    _record(conn, third, "accepted", run="rr-002", value=4.0)
    # an unrelated event, and one that names rr-001 only in its data
    db.append_event(conn, session=S, actor="test", kind="msg", ref="elsewhere", data={"body": "x"})
    db.append_event(conn, session=S, actor="test", kind="note", ref="elsewhere", data={"run": "rr-001"})
    conn.close()
    after = view.runs(project)
    assert json.loads(json.dumps(after)) == json.loads(json.dumps(_uncached_runs(project)))
    got = {r["run"]: r for r in after["runs"]}
    assert got["rr-002"]["attempt_count"] == before["rr-002"]["attempt_count"] + 1
    assert got["rr-002"] != before["rr-002"]
    assert kept["rr-001"] is not view._KEPT[str(project)]["rr-001"]     # the second read built it afresh


def test_an_event_that_does_not_concern_a_run_leaves_its_kept_summary(project):
    conn = _conn(project)
    _start(conn)
    _record(conn, _task(conn, 1, 0), "accepted", value=5.0)
    view.runs(project)
    kept = view._KEPT[str(project)][RUN]
    body = kept.body
    db.append_event(conn, session=S, actor="test", kind="msg", ref="elsewhere", data={"body": "x"})
    db.append_event(conn, session=S, actor="test", kind="mission-snapshot", ref=None,
                    data={"tasks": ['"%s"' % RUN, RUN]})
    conn.close()
    view.runs(project)
    assert view._KEPT[str(project)][RUN] is kept and kept.body is body


def test_a_new_run_and_the_project_posture_show_without_a_rebuild_of_the_others(project):
    conn = _conn(project)
    _start(conn, "rr-001")
    view.runs(project)
    _start(conn, "rr-002")
    from alpaca.resilience import budget
    budget.spend(conn, 3.0, ref="elsewhere", session=S, actor="test")
    conn.close()
    got = view.runs(project)
    assert [r["run"] for r in got["runs"]] == ["rr-002", "rr-001"]
    assert json.loads(json.dumps(got)) == json.loads(json.dumps(_uncached_runs(project)))


def test_a_kept_ended_run_follows_the_runner_lock_and_the_clock_at_read_time(project, monkeypatch):
    """t-593 review N1: runner.alive and the age of the oldest handed job are patched into the kept
    body on every read, so an ended run needs no timed rebuild to show them."""
    from alpaca.research import runner
    conn = _conn(project)
    _start(conn)
    events.emit(conn, events.RUNNER, run=RUN, ref=RUN, op=OP, session=S,
                data={"pid": 1, "host": "h", "cause": "start", "foreground": False})
    events.emit(conn, events.STOP, run=RUN, ref=RUN, op=OP, session=S, data={"reason": "x", "final": True})
    conn.close()
    alive = {"now": True}
    monkeypatch.setattr(runner, "is_alive", lambda root, run: alive["now"])
    first = view.runs(project)["runs"][0]
    assert first["state"] in view.ENDED and first["runner"]["alive"] is True
    kept = view._KEPT[str(project)][RUN]
    alive["now"] = False
    second = view.runs(project)["runs"][0]
    assert view._KEPT[str(project)][RUN] is kept        # not rebuilt
    assert second["runner"]["alive"] is False
    assert json.loads(json.dumps(view.runs(project))) == json.loads(json.dumps(_uncached_runs(project)))
    # the lock being held or not is part of what the server keys the view on
    os.makedirs(os.path.dirname(view.paths.runner_lock(str(project), RUN)), exist_ok=True)
    open(view.paths.runner_lock(str(project), RUN), "w").close()
    stamp = view.source_stamp(project)
    alive["now"] = True
    assert view.source_stamp(project) != stamp
