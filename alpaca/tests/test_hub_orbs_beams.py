"""t-349 and t-350: the multi-orb loading sequence, border beams, autodrive level chips, research
runs folded to one card each on the workspace roadmap, and the shared overview read."""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from alpaca import db

WEB = Path(__file__).resolve().parents[1] / "web"
NODE = shutil.which("node")
NEW_FILES = ("beam.css", "beam.js", "loader.js", "autodrive.js")


def node(script):
    if not NODE:
        pytest.skip("node is required")
    run = subprocess.run([NODE, "--input-type=module", "-e", script], capture_output=True, text=True,
                         cwd=str(WEB), timeout=60)
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout)


def test_new_web_files_are_ascii_and_served():
    for name in NEW_FILES + ("hub.css", "hub.html", "roadmap.js", "roadmap.css", "inflight.js"):
        assert re.search(r"[^\x00-\x7f]", (WEB / name).read_text(encoding="utf-8")) is None, name
    from alpaca import serve
    for name in NEW_FILES:                      # the asset allowlist comes from the registry (pages.json)
        assert name in serve.hub_assets()
    assert "beam.css" in serve.hub_styles()     # linked by the served hub page


def test_loader_sequences_use_every_orb_state_at_full_size():
    got = node("""
import {loader,SEQUENCES} from './loader.js';
import {STATE_TO_MODE} from './vendor/thinking-orbs.js';
const used=new Set(Object.values(SEQUENCES).flatMap(s=>s.map(x=>x[0])));
const html=loader('record'),compact=loader('document',{compact:true});
console.log(JSON.stringify({missing:Object.keys(STATE_TO_MODE).filter(s=>!used.has(s)),
 orbs:(html.match(/size="64"/g)||[]).length,lines:(html.match(/class="seq-line"/g)||[]).length,
 steps:SEQUENCES.record.length,compact:/size="32"/.test(compact)&&compact.includes('orb-seq compact'),
 status:html.includes('role="status"')}));""")
    assert got["missing"] == []
    assert got["orbs"] == got["lines"] == got["steps"] >= 4
    assert got["compact"] and got["status"]
    css = (WEB / "hub.css").read_text(encoding="utf-8")
    assert "@keyframes seq-in" in css and "@keyframes seq-settle" in css
    # reduced motion shows the orbs at rest and only the last line
    assert re.search(r"prefers-reduced-motion:reduce\)\s*\{\s*\.seq-orb", css)


def overview_fixture():
    task = lambda i, op, status="open", session=None: {"id": i, "title": "Task " + i, "op": op, "status": status,
                                                         "assigned_session": session}
    node_ = lambda i, state, session=None: {"id": i, "state": state, "label": state, "after": [], "reasons": [],
                                            "session": session}
    return {"project": {"name": "demo"},
            "tasks": [task("t-1", "op-1", "doing", "s-live"), task("t-2", "op-1"), task("t-3", "op-2"),
                      task("t-4", "op-2"), task("t-5", "op-3"), task("t-6", "op-3", "done")],
            "ops": [{"id": "op-1", "title": "Build", "phases": [], "phase_state": {}},
                    {"id": "op-2", "title": "research run rr-001"}, {"id": "op-3", "title": "research run rr-002"}],
            "levels": {"s-live": {"level": "L5", "set": True, "goal": "ship it"}},
            "research_runs": {"op-2": {"run": "rr-001", "state": "running", "holds": 0},
                              "op-3": {"run": "rr-002", "state": "finished", "holds": 0}},
            "roadmap": {"operations": [
                {"op": "op-1", "edges": [], "groups": [{"id": "g1", "title": "Core", "category": "Features",
                                                        "nodes": [node_("t-1", "working", "s-live"), node_("t-2", "approval")]}]},
                {"op": "op-2", "edges": [], "groups": [{"id": "r1", "title": "Research run rr-001", "category": "research",
                                                        "nodes": [node_("t-3", "blocked"), node_("t-4", "ready")]}]},
                {"op": "op-3", "edges": [], "groups": [{"id": "r2", "title": "Research run rr-002", "category": "research",
                                                        "nodes": [node_("t-5", "blocked"), node_("t-6", "done")]}]}]}}


def test_roadmap_folds_research_runs_and_marks_live_and_alert_cards(tmp_path):
    data = tmp_path / "o.json"
    data.write_text(json.dumps(overview_fixture()))
    got = node("""
import {readFileSync} from 'node:fs';
globalThis.sessionStorage={getItem(){return null},setItem(){}};
const {renderRoadmap,renderPhaseMap}=await import('./roadmap.js');
const o=JSON.parse(readFileSync(%s,'utf8'));
const doc=renderRoadmap(o,{all:true}),full=renderRoadmap(o,{all:true,showAll:true}),map=renderPhaseMap(o,'op-1');
const card=(h,id)=>(h.match(new RegExp('<article class="([^"]*)" data-rm-node="'+id+'"'))||[])[1]||null;
console.log(JSON.stringify({t1:card(doc,'t-1'),t2:card(doc,'t-2'),t3:card(doc,'t-3'),run1:card(doc,'run:op-2'),
 run2:card(full,'run:op-3'),runLink:doc.includes('href="#research?run=rr-001"'),level:/ad-chip ad-l5/.test(doc),
 driver:map.includes('rm-autodrive')&&/rm-driver[^>]*>.*ad-l5/.test(map)}));""" % json.dumps(str(data)))
    assert "beam" in got["t1"].split() and "beam-alert" not in got["t1"]
    assert {"beam", "beam-alert"} <= set(got["t2"].split())
    assert got["t3"] is None                      # research attempts are folded away
    assert "rm-working" in got["run1"] and "beam" in got["run1"].split() and got["runLink"]
    # a finished run is done whatever task it left open: no beam, no red
    assert got["run2"] is not None and "beam" not in got["run2"].split()
    assert got["level"] and got["driver"]


def test_levels_report_set_default_and_off(tmp_path, monkeypatch):
    from alpaca.posture import level
    root = tmp_path / "p"
    root.mkdir()
    (root / "ALPACA-MANIFEST").write_text("")
    conn = db.connect(str(root))
    try:
        monkeypatch.setattr(level, "default_level", lambda root=None: 2)
        level.set(conn, "s-five", "L5", "drive t-1", root=str(root))
        level.set(conn, "s-off", "L2", level.OFF_GOAL, root=str(root))
        got = level.levels(conn, {"s-five", "s-off", "s-none", None}, root=str(root))
    finally:
        conn.close()
    assert got["s-five"] == {"level": "L5", "set": True, "goal": "drive t-1"}
    assert got["s-off"]["level"] == "OFF"
    assert got["s-none"] == {"level": "L2", "set": False, "goal": ""}
    assert None not in got


def test_research_states_follow_the_run_events(tmp_path):
    from alpaca import hub
    from alpaca.research import events as rev
    root = tmp_path / "p"
    root.mkdir()
    conn = db.connect(str(root))
    try:
        add = lambda kind, ref, op, **data: db.append_event(conn, session="s", actor="engine", kind=kind, ref=ref,
                                                            op=op, data=data)
        add(rev.RUNNER, "rr-001", "op-2")
        add(rev.ATTEMPT, "t-3", "op-2")                 # an attempt's ref is its task
        add(rev.RUNNER, "rr-002", "op-3")
        add(rev.ATTEMPT, "t-5", "op-3")
        add(rev.FINALIZE, "rr-002", "op-3", state="finished")
        add(rev.RUNNER, "rr-003", "op-4")
        add(rev.STOP, "rr-003", "op-4", final=True)
        add(rev.RUNNER, "rr-004", "op-5")
        add(rev.HOLD, "rr-004", "op-5", gate="extension")
        got = hub._research_states(conn)
    finally:
        conn.close()
    assert {op: (v["run"], v["state"]) for op, v in got.items()} == {
        "op-2": ("rr-001", "running"), "op-3": ("rr-002", "finished"),
        "op-4": ("rr-003", "stopped"), "op-5": ("rr-004", "held")}


def test_overview_is_shared_until_the_record_head_moves(tmp_path, monkeypatch):
    from alpaca import hub, serve
    root = tmp_path / "p"
    root.mkdir()
    conn = db.connect(str(root))
    calls = []
    monkeypatch.setattr(hub, "overview", lambda r: calls.append(r) or {"n": len(calls)})
    live = serve.Live(str(root))
    try:
        first = live.overview()
        assert live.overview() == first and len(calls) == 1
        db.append_event(conn, session="s", actor="a", kind="note", data={})
        second = live.overview()
        assert second != first and len(calls) == 2
        monkeypatch.setattr(serve, "OVERVIEW_S", 0)
        live.overview()
        assert len(calls) == 3                       # the age bound refreshes file-only inputs
    finally:
        conn.close()


def test_overview_ignores_quiet_events_but_not_board_changes(tmp_path, monkeypatch):
    """t-578: heartbeats, drain-failed and transcript notes do not end a cached overview; a task
    move, a proof seal, a plan change or any other event does."""
    from alpaca import hub, serve
    root = tmp_path / "p"
    root.mkdir()
    conn = db.connect(str(root))
    calls = []
    monkeypatch.setattr(hub, "overview", lambda r: calls.append(r) or {"n": len(calls)})
    live = serve.Live(str(root))
    add = lambda kind, **kw: db.append_event(conn, session="s", actor="a", kind=kind, data={}, **kw)
    try:
        live.overview()
        for kind in serve.OVERVIEW_QUIET_KINDS:
            add(kind)
        add("heartbeat", ref="t-1")
        live.overview()
        assert len(calls) == 1                       # a flood of quiet events reuses the cached overview
        for n, kind in enumerate(("task-move", "proof-report", "task-plan", "task-contract"), 2):
            add(kind, ref="t-1")
            live.overview()
            assert len(calls) == n, kind
        add("heartbeat")
        add("note")
        live.overview()
        assert len(calls) == 6                       # a quiet event never hides a later real one
    finally:
        conn.close()


def test_overview_waiter_takes_the_value_computed_while_it_waited(tmp_path, monkeypatch):
    """t-578: a read that arrives during a compute, with only quiet events landing meanwhile, takes
    that compute's result instead of starting a second one."""
    import threading
    import time
    from alpaca import hub, serve
    root = tmp_path / "p"
    root.mkdir()
    conn = db.connect(str(root))
    calls, started, release = [], threading.Event(), threading.Event()

    def slow(_root):
        calls.append(1)
        started.set()
        release.wait(5)
        return {"n": len(calls)}
    monkeypatch.setattr(hub, "overview", slow)
    live = serve.Live(str(root))
    results = []
    try:
        warm = threading.Thread(target=lambda: results.append(live.overview()))
        warm.start()
        assert started.wait(5)
        db.append_event(conn, session="s", actor="a", kind="heartbeat", data={})
        db.append_event(conn, session="s", actor="a", kind="drain-failed", data={})
        waiter = threading.Thread(target=lambda: results.append(live.overview()))
        waiter.start()
        time.sleep(0.2)
        release.set()
        warm.join(5)
        waiter.join(5)
        assert len(calls) == 1 and len(results) == 2 and results[0] == results[1]
    finally:
        release.set()
        conn.close()


def test_overview_page_counts_capture_sources():
    from alpaca import hub
    got = hub._page_capture({"status": "ok", "sources": [{"source_id": "a", "backlog_bytes": 5},
                                                          {"source_id": "b", "backlog_bytes": "x"}]})
    assert got["sources"] == [] and got["source_count"] == 2 and got["source_backlog_bytes"] == 5


def test_orbs_mark_live_state_beyond_loading(tmp_path):
    data = tmp_path / "o.json"
    data.write_text(json.dumps(overview_fixture()))
    got = node("""
import {readFileSync} from 'node:fs';
globalThis.sessionStorage={getItem(){return null},setItem(){}};
const {renderRoadmap,renderPhaseMap}=await import('./roadmap.js');
const {renderInflight}=await import('./inflight.js');
const o=JSON.parse(readFileSync(%s,'utf8'));
const doc=renderRoadmap(o,{all:true});
console.log(JSON.stringify({group:/rm-group-current"><alpaca-orb state="working"/.test(doc),
 idle:renderInflight({tasks:[],generated:0},{href:()=>'#'}).includes('<alpaca-orb state="breathing"')}));""" % json.dumps(str(data)))
    assert got["group"] and got["idle"]
    hub = (WEB / "hub.js").read_text(encoding="utf-8")
    html = (WEB / "hub.html").read_text(encoding="utf-8")
    research = (WEB / "research.js").read_text(encoding="utf-8")
    assert "orb('working',20):'<i class=\"badge-dot\"></i>'" in hub          # running badges
    assert "class=\"empty\">${orb('breathing',32)}" in hub                    # empty states
    assert "orb('listening',20,'Waiting for an answer')" in hub                # open questions
    assert 'id="live-orb"' in html and 'id="connection"><alpaca-orb' in html  # topbar and sidebar
    assert "running:'solving'" in research and "runChip(run.state)" in research
    assert "orb('breathing',32,'No run active')" in (WEB / "cockpit.js").read_text(encoding="utf-8")
    assert "orb('breathing',20,'No job running')" in (WEB / "live.js").read_text(encoding="utf-8")


def test_record_connections_sort_in_memory(tmp_path):
    """t-353: an ORDER BY that spills must not create a temp file in /var/tmp, where a slow disk
    journal stalled hub reads for up to a minute."""
    root = tmp_path / "p"
    root.mkdir()
    writer = db.connect(str(root))
    try:
        assert writer.execute("PRAGMA temp_store").fetchone()[0] == 2
        reader = db.connect_readonly(str(root))
        try:
            assert reader.execute("PRAGMA temp_store").fetchone()[0] == 2
        finally:
            reader.close()
    finally:
        writer.close()


def test_session_list_filters_by_agent_and_sorts():
    """t-361: the Sessions index filters Claude or Codex and sorts by date, activity, cost or size;
    a filtered view sums its own figures."""
    got = node("""
import {sessionList,AGENT_CHOICES,SORT_CHOICES} from './sessionlist.js';
const row=(sid,op,first,last,cost,tok,resp)=>({sid,title:sid,operator:op,first,last,coverage:{complete:true},
 summary:{responses:resp,usage_responses:resp,costed_responses:resp,estimated_cost_usd:cost,tokens:{total_tokens:tok}},
 subagents:{count:0,measured:0},family:{estimated_cost_usd:cost,total_tokens:tok,responses:resp,costed_responses:resp}});
const index={totals:{estimated_cost_usd:99},sessions:[
 row('a','claude','2026-10-01T10:00:00+07:00','2026-10-07T09:00:00+07:00',5,500,10),
 row('b','codex','2026-10-03T10:00:00+07:00','2026-10-03T11:00:00+07:00',1,100,2),
 row('c','claude-code','2026-10-05T10:00:00+07:00','2026-10-05T12:00:00+07:00',null,null,0),
 row('d','codex','2026-10-02T10:00:00+07:00',null,9,900,30)]};
const ids=r=>r.sessions.map(s=>s.sid).join('');
const claude=sessionList(index,'','claude');
console.log(JSON.stringify({newest:ids(sessionList(index)),oldest:ids(sessionList(index,'','','oldest')),
 active:ids(sessionList(index,'','','active')),most:ids(sessionList(index,'','','cost-desc')),
 least:ids(sessionList(index,'','','cost-asc')),tokens:ids(sessionList(index,'','','tokens-desc')),
 claude:ids(claude),codex:ids(sessionList(index,'','codex','cost-asc')),claudeCost:claude.totals.estimated_cost_usd,
 claudeShown:claude.filtered,unfilteredTotals:sessionList(index).totals.estimated_cost_usd,
 choices:[AGENT_CHOICES.map(c=>c[0]),SORT_CHOICES.map(c=>c[0])]}));""")
    assert got["newest"] == "cbda" and got["oldest"] == "adbc"
    assert got["active"] == "acbd"                 # last activity, start time when none recorded
    assert got["most"] == "dabc" and got["least"] == "badc"   # no measured cost sorts last both ways
    assert got["tokens"] == "dabc"
    assert got["claude"] == "ca" and got["codex"] == "bd"     # claude-code counts as Claude
    assert got["claudeCost"] == 5 and got["claudeShown"] == {"shown": 2, "of": 4}
    assert got["unfilteredTotals"] == 99            # no filter: the server totals stand
    assert got["choices"][0] == ["", "claude", "codex", "other"]
    hub = (WEB / "hub.js").read_text(encoding="utf-8")
    assert "select('agent',AGENT_CHOICES" in hub and "select('sort',SORT_CHOICES" in hub
    from alpaca import serve
    assert "sessionlist.js" in serve.hub_assets()


def test_sidebar_pulse_orb_follows_load_and_time_of_day():
    """t-374: the always-on orb under the brand: pulse grows with concurrent work, hue follows the
    local hour, the caption states the counts."""
    got = node("""
import {hueAt,periodAt,pulseFor,caption,CYCLE,fadePath} from './pulse.js';
const at=(h,m=0)=>new Date(2026,9,7,h,m);
console.log(JSON.stringify({hues:[0,6,12,18].map(h=>hueAt(at(h))),slots:[hueAt(at(12,0)),hueAt(at(12,4)),hueAt(at(12,5)),hueAt(at(23,59))],
 fade:fadePath(50,51.25,4),wrap:fadePath(359,1,4),periods:[3,6,10,14,19,23].map(h=>periodAt(at(h))),
 idle:pulseFor(0),busy:pulseFor(3),cap:pulseFor(50),captions:[caption(0,0),caption(1,0),caption(3,2)],cycle:CYCLE}));""")
    assert got["hues"] == [230, 320, 50, 140]
    assert got["slots"] == [50, 50, 51.25, 228.75]          # t-378: 5-minute slots, 1.25 degrees each
    assert got["fade"] == [50.31, 50.63, 50.94, 51.25]      # each step fades in small moves
    assert got["wrap"] == [359.5, 0, 0.5, 1]                # the short way round the wheel
    assert got["periods"] == ["Night", "Dawn", "Morning", "Afternoon", "Evening", "Night"]
    idle, busy, cap = got["idle"], got["busy"], got["cap"]
    assert idle["scale"] < busy["scale"] < cap["scale"] and idle["alpha"] < busy["alpha"] < cap["alpha"]
    assert idle["seconds"] > busy["seconds"] > cap["seconds"] >= 3     # stronger load, quicker, never frantic
    assert cap == {"scale": 1.58, "alpha": 0.8, "seconds": 3.2, "waves": 4, "sparks": 18, "reach": 64}  # capped at eight
    assert idle["waves"] == 2 and busy["waves"] == 3 and idle["sparks"] < busy["sparks"]   # t-377 ripples and sparks
    assert got["captions"] == ["Idle", "1 task in flight", "3 tasks in flight / 2 jobs running"]
    assert "breathing" not in got["cycle"]
    html = (WEB / "hub.html").read_text(encoding="utf-8")
    # t-482: Arthur takes the slot under the brand; the orb sits at the right end of the topbar.
    # t-486: the workspace row under Arthur is gone, so the nav label follows him.
    assert html.index('class="brand"') < html.index('id="arthur-slot"') < html.index('class="nav-label"')
    assert 'class="workspace"' not in html
    top = html[html.index('class="top-actions"'):html.index('</header>')]
    assert 'id="pulse-orb"' in top and 'class="avatar"' not in html
    orb_block = top[top.index('id="pulse-orb"'):]
    assert orb_block.count('class="pulse-ring"') == 4 and 'pulse-sparks' in orb_block
    hub = (WEB / "hub.js").read_text(encoding="utf-8")
    assert "updatePulse(o)" in hub and "startPulse()" in hub
    from alpaca import serve
    assert "pulse.js" in serve.hub_assets()
    assert "prefers-reduced-motion:reduce) { .pulse-ring" in (WEB / "hub.css").read_text(encoding="utf-8")
