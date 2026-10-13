"""op-012 t-580, t-581, t-589, t-594, t-596 and t-598: the Records Wiki tab's Open vault shows the evidence graph, and a
Studio search box asks the server only once typing stops.

`hub.evidence_graph` lays the record out as fifteen typed columns in the owner's order (operations,
tasks, decisions, sealed proofs, four kinds of evidence, review cards, phase doors, sessions, messages,
wiki assertions, claims and sources) with links from the item that supports to the item supported;
`hub.graph_item` names the provenance of one item's links. evbrain.js draws the same graph as a 3D
brain. Both read only, behind the web login like the wiki. The
browser half (studio/vault.mjs) drives the fixture Studio: search after typing stops, Enter at once,
the page kept on screen; Open vault draws the graph, find and select keep the item in the address,
Close vault returns to search mode. evgraph.js's chain walk and find run under node."""
import base64
import hashlib
import http.server
import json
import os
import shutil
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from alpaca import hub, serve
from alpaca.tests import studio_shots
from alpaca.wiki import service

HERE = Path(__file__).resolve().parent
WEB = HERE.parent / "web"
NODE = shutil.which("node")


@pytest.fixture(scope="module")
def atlas(tmp_path_factory):
    work = str(tmp_path_factory.mktemp("vault"))
    if os.path.realpath(work).startswith(("/home/", "/Users/")):
        pytest.skip("the Studio fixture is built outside a home folder: run with --basetemp under /tmp")
    return studio_shots.build_fixture(work)


def _record_files(root):
    db = Path(root, ".alpaca", "alpaca.db")
    wal = Path(str(db) + "-wal")
    return (hashlib.sha256(db.read_bytes()).hexdigest(),
            hashlib.sha256(wal.read_bytes()).hexdigest() if wal.exists() and wal.stat().st_size else None)


def test_the_graph_holds_every_record_item_in_its_column_and_writes_nothing(atlas):
    before = _record_files(atlas)
    g = hub.evidence_graph(atlas)
    keys = [c["key"] for c in g["columns"]]
    assert keys == ["op", "task", "decision", "proof", "ev-event", "ev-log", "ev-code", "ev-file", "review", "phase",
                    "session", "message", "assertion", "claim", "source"]
    assert all(c["label"] and c["short"] for c in g["columns"])
    counts = {c["key"]: c["count"] for c in g["columns"]}
    assert counts == {k: sum(1 for n in g["nodes"] if n["c"] == i) for i, k in enumerate(keys)}
    records = {s: hub.records(atlas, s, limit=500) for s in ("ops", "decisions", "proofs")}
    assert counts["op"] == records["ops"]["total"] and counts["decision"] == records["decisions"]["total"]
    assert counts["proof"] == sum(1 for p in records["proofs"]["items"] if p["path"])
    assert counts["task"] >= counts["proof"] and sum(counts[k] for k in keys if k.startswith("ev-")) > 0
    # each column's entries sit together, in column order
    assert [n["c"] for n in g["nodes"]] == sorted(n["c"] for n in g["nodes"])
    assert len({n["id"] for n in g["nodes"]}) == len(g["nodes"])
    assert _record_files(atlas) == before


def test_every_link_runs_from_evidence_toward_conclusions_and_names_its_kind(atlas):
    g = hub.evidence_graph(atlas)
    nodes = g["nodes"]
    key = lambda i: g["columns"][nodes[i]["c"]]["key"]
    assert g["links"] and all(a != b for a, b, _k in g["links"])
    assert len({(a, b, k) for a, b, k in g["links"]}) == len(g["links"])
    pairs = {(key(a), key(b)): k for a, b, k in g["links"]}
    assert pairs[("proof", "task")] == "proves" and pairs[("task", "op")] == "part of"
    assert {k for (a, b), k in pairs.items() if a.startswith("ev-")} == {"cited by"}
    assert all(b == "proof" for a, b in pairs if a.startswith("ev-"))
    # every evidence pointer is cited by a proof, every proof proves one task, every task sits in its op
    cited = {a for a, b, k in g["links"] if k == "cited by"}
    assert cited == {i for i in range(len(nodes)) if key(i).startswith("ev-")}
    proves = [a for a, _b, k in g["links"] if k == "proves"]
    assert sorted(proves) == sorted(i for i in range(len(nodes)) if key(i) == "proof")
    tasks = {i for i in range(len(nodes)) if key(i) == "task"}
    assert {a for a, _b, k in g["links"] if k == "part of"} == tasks
    # a session links to each task it moved, a message to each item it names
    assert all(key(a) == "session" and key(b) == "task" for a, b, k in g["links"] if k == "worked on")
    assert all(key(a) == "message" and key(b) in ("task", "op") for a, b, k in g["links"] if k == "mentions")


def test_evidence_splits_by_what_it_is():
    assert hub._evidence_column("event:12345") == "ev-event"
    assert hub._evidence_column("local:.alpaca/proofs/op-009/t-117.logs/verification.md") == "ev-log"
    assert hub._evidence_column("local:.alpaca/proofs/op-1/t-2.logs/run.txt") == "ev-log"
    assert hub._evidence_column("local:docs/x/suite.log") == "ev-log"
    assert hub._evidence_column("local:alpaca/hub.py:1456") == "ev-code"
    assert hub._evidence_column("local:.alpaca/research-engine/door_try.py") == "ev-code"
    assert hub._evidence_column("local:design/research-engine/flare-spec.md") == "ev-file"
    assert hub._evidence_column("remote:https://github.com/o/r/commit/abc") == "ev-file"


def test_the_record_items_beyond_proofs_have_their_own_facts(atlas):
    g = hub.evidence_graph(atlas)
    for prefix, kind in (("session:", "session"), ("msg:", "message"), ("door:", "phase")):
        ident = next((n["id"] for n in g["nodes"] if n["id"].startswith(prefix)), None)
        if ident is None:
            continue                                   # the fixture may hold none of this kind
        item = hub.graph_item(atlas, ident)
        assert item["kind"] == kind and item["facts"]


def test_an_item_names_the_record_event_and_the_report_behind_its_links(atlas):
    g = hub.evidence_graph(atlas)
    nodes = g["nodes"]
    proof_col = [c["key"] for c in g["columns"]].index("proof")
    proof = next(n for n in nodes if n["c"] == proof_col)
    task = next(nodes[b]["id"] for a, b, k in g["links"] if k == "proves" and nodes[a]["id"] == proof["id"])
    item = hub.graph_item(atlas, task)
    facts = dict(item["facts"])
    assert item["kind"] == "task" and facts["Recorded"].startswith("event ") and "task-add" in facts["Recorded"]
    assert {"id": proof["id"], "dir": "in", "kind": "proves"} in item["links"]
    sealed = dict(hub.graph_item(atlas, proof["id"])["facts"])
    assert sealed["Task"] == task and "proof-report" in sealed["Sealed"]
    evidence = next(nodes[a]["id"] for a, b, k in g["links"] if k == "cited by" and nodes[b]["id"] == proof["id"])
    cited = dict(hub.graph_item(atlas, evidence)["facts"])
    assert cited["Pointer"] == evidence[len("ev:"):] and "Cited for " + task in cited
    with pytest.raises(ValueError):
        hub.graph_item(atlas, "t-99999")


def test_a_project_without_a_wiki_store_shows_no_claims_and_says_why(atlas):
    g = hub.evidence_graph(atlas)
    assert g["wiki"]["available"] is False and g["wiki"]["reason"]
    assert g["wiki"]["assertions"] is False and g["wiki"]["assertions_reason"]
    counts = {c["key"]: c["count"] for c in g["columns"]}
    assert counts["claim"] == counts["assertion"] == counts["source"] == 0
    assert service.graph_claims(atlas) == {"available": False, "reason": g["wiki"]["reason"], "claims": []}
    assert service.graph_assertions(atlas, "op-001")["available"] is False


def test_one_build_serves_until_the_record_moves(atlas, monkeypatch):
    hub._GRAPH_CACHE.clear()
    builds = []
    real = hub._graph_build
    monkeypatch.setattr(hub, "_graph_build", lambda root: builds.append(root) or real(root))
    hub.evidence_graph(atlas, now=1000.0)
    hub.evidence_graph(atlas, now=1030.0)
    assert len(builds) == 1
    hub.evidence_graph(atlas, now=1000.0 + hub.GRAPH_TTL_S + 1)
    assert len(builds) == 2


def test_a_decision_reference_recorded_twice_keeps_both_decisions():
    assert hub._decision_id({"id": 7, "ref": "dec-4561043ab6cd"}) == "dec-4561043ab6cd"
    assert hub._decision_id({"id": 7, "ref": None}) == "decision:7"


@pytest.fixture
def server(atlas):
    live = serve.Live(atlas)
    live.refresh()
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), serve.make_handler(live, atlas))
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield "http://127.0.0.1:%d" % httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()


def _fetch(url, auth=None):
    req = urllib.request.Request(url)
    if auth:
        req.add_header("Authorization", "Basic " + base64.b64encode(auth.encode()).decode())
    try:
        r = urllib.request.urlopen(req, timeout=60)
    except urllib.error.HTTPError as exc:
        r = exc
    return r.status, r.read()


def test_the_graph_routes_need_the_web_login(server, monkeypatch):
    monkeypatch.setenv("ALPACA_FILES_AUTH", "")
    monkeypatch.setattr(serve, "files_auth", lambda root: None)
    for route in ("/hub/graph.json", "/hub/graph-item.json?id=op-001"):
        status, body = _fetch(server + route)
        assert status == 403 and b"login" in body


def test_the_graph_routes_answer_with_the_login(server, monkeypatch):
    monkeypatch.setattr(serve, "files_auth", lambda root: "u:p")
    status, body = _fetch(server + "/hub/graph.json", auth="u:p")
    assert status == 200 and [c["key"] for c in json.loads(body)["columns"]][0] == "op"
    status, body = _fetch(server + "/hub/graph-item.json?id=op-001", auth="u:p")
    assert status == 200 and json.loads(body)["kind"] == "op"
    status, _body = _fetch(server + "/hub/graph-item.json?id=nothing-here", auth="u:p")
    assert status == 404


def test_the_records_page_registers_the_graph_files_and_routes():
    pages = json.loads((WEB / "pages.json").read_text(encoding="utf-8"))["pages"]
    records = next(p for p in pages if p["id"] == "records")
    assert {"evgraph.js", "evbrain.js", "evbrowse.js", "ventry.js"} <= set(records["js"]) and "ventry.css" in records["css"]
    shell = json.loads((WEB / "pages.json").read_text(encoding="utf-8"))["shell"]
    assert "tips.js" in shell["js"] and "tips.css" in shell["css_after"] and "evgraph.css" in records["css"]
    assert {"/hub/graph.json", "/hub/graph-item.json"} <= set(records["endpoints"])
    # a live refresh patches the page in place; an open vault is kept, never rebuilt (t-594: it blinked)
    assert records["quiet"] is True
    assert "if(automatic&&vault&&$('#evidence-graph'))return false;" in (WEB / "hub.js").read_text(encoding="utf-8")


def _node(script):
    if not NODE:
        pytest.skip("node is required")
    done = subprocess.run([NODE, "--input-type=module", "-e", script], capture_output=True, text=True, timeout=60, cwd=str(WEB))
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def test_a_selected_item_lights_what_it_rests_on_and_what_rests_on_it_only():
    got = _node("""
import {prepare, lineage, findMatches, FIND_IDLE_MS, labelOf} from './evgraph.js';
const g = prepare({columns: [{key:'ev-log'},{key:'proof'},{key:'task'},{key:'op'}],
  nodes: [{id:'ev:local:a.log',c:0},{id:'ev:local:b.log',c:0},{id:'proof:p1.md',c:1},{id:'proof:p2.md',c:1},
          {id:'t-1',c:2,label:'Build it'},{id:'t-2',c:2,label:'Test t-1'},{id:'op-1',c:3,label:'Op one'}],
  links: [[0,2,'cited by'],[1,3,'cited by'],[2,4,'proves'],[3,5,'proves'],[4,6,'part of'],[5,6,'part of']]});
const t1 = lineage(g, 4), ev = lineage(g, 0);
console.log(JSON.stringify({t1: [...t1.nodes].sort(), t1links: [...t1.links].sort(), ev: [...ev.nodes].sort(),
  find: findMatches(g, 't-1').map(i => g.nodes[i].id), none: findMatches(g, '  '), idle: FIND_IDLE_MS,
  label: labelOf(g.nodes[0]), rows: g.nodes.map(n => n.row)}));
""")
    assert got["t1"] == [0, 2, 4, 6]               # its evidence, its proof, itself, its op; not t-2's chain
    assert got["t1links"] == [0, 2, 4]
    assert got["ev"] == [0, 2, 4, 6]
    assert got["find"][0] == "t-1" and "t-2" in got["find"]  # the exact id first, a label mention after
    assert got["none"] == [] and got["idle"] >= 800
    assert got["label"] == "local:a.log" and got["rows"] == [0, 1, 0, 1, 0, 1, 0]


def test_search_and_the_vault_in_the_browser(atlas, tmp_path):
    node, chrome, why = studio_shots.tools()
    if not node:
        pytest.skip(why)
    out = tmp_path / "vault.json"
    profile = Path("/tmp") / ("vault-%d" % os.getpid())
    with studio_shots.serve(atlas) as base:
        done = subprocess.run([node, str(HERE / "studio" / "vault.mjs"), "--base", base, "--root", atlas,
                               "--profile", str(profile), "--out", str(out)],
                              capture_output=True, text=True, timeout=300, env=dict(os.environ, CHROME=chrome))
    shutil.rmtree(profile, ignore_errors=True)
    assert out.exists(), done.stderr[-2000:]
    results = json.loads(out.read_text())
    failed = {k: v for k, v in results.items() if not v["ok"]}
    assert not failed, json.dumps(failed, indent=2)
    assert len(results) >= 38


def test_the_firing_share_picks_the_same_links_and_every_link_at_100():
    got = _node("""
import {firing, rank, FIRE_DEFAULT, SPEED_DEFAULT} from './evgraph.js';
const a = firing(9389, 30), b = firing(9389, 30);
console.log(JSON.stringify({none: firing(9389, 0).length, all: firing(9389, 100).length, share: a.length / 9389,
  same: JSON.stringify(a) === JSON.stringify(b), grows: firing(9389, 30).every(li => firing(9389, 60).includes(li)),
  over: firing(10, 250).length, ranks: [0, 1, 2, 9388].map(rank).every(r => r >= 0 && r < 1),
  defaults: [FIRE_DEFAULT, SPEED_DEFAULT]}));
""")
    assert got["none"] == 0 and got["all"] == 9389 and got["over"] == 10
    assert 0.27 < got["share"] < 0.33 and got["same"] and got["grows"] and got["ranks"]
    assert got["defaults"] == [30, 100]


def test_browse_narrows_tier_by_tier_and_never_leads_back_up():
    got = _node("""
import {prepare} from './evgraph.js';
import {tier, narrow, reach, grouped} from './evbrowse.js';
const g = prepare({columns: [{key: 'op'}, {key: 'task'}, {key: 'proof'}, {key: 'ev-log'}, {key: 'message'}],
  nodes: [{id: 'op-001', c: 0}, {id: 'op-002', c: 0}, {id: 't-1', c: 1, label: 'one'}, {id: 't-10', c: 1, label: 'ten'},
          {id: 'proof:a.md', c: 2}, {id: 'ev:local:x.log', c: 3}, {id: 'msg:1', c: 4, label: 'about t-1'}],
  links: [[2, 0, 'part of'], [3, 0, 'part of'], [4, 2, 'proves'], [5, 4, 'cited by'], [6, 2, 'mentions'], [6, 0, 'mentions']]});
const ids = groups => groups.map(x => [x.ci, x.items.map(i => g.nodes[i].id)]);
console.log(JSON.stringify({root: ids(tier(g, [])), op: ids(tier(g, [0])), task: ids(tier(g, [0, 2])), proof: ids(tier(g, [0, 2, 4])),
  ten: ids(narrow(g, tier(g, [0]), 'ten')), reach: [reach(g, 2, [0]), reach(g, 5, [0, 2, 4])], found: ids(grouped(g, [6, 2, 0]))}));
""")
    assert got["root"] == [[0, ["op-002", "op-001"]]]                      # operations, newest first
    assert got["op"] == [[1, ["t-10", "t-1"]], [4, ["msg:1"]]]             # its tasks newest first, then messages
    assert got["task"] == [[2, ["proof:a.md"]], [4, ["msg:1"]]]            # the operation already on the path is left out
    assert got["proof"] == [[3, ["ev:local:x.log"]]]
    assert got["ten"] == [[1, ["t-10"]]]
    assert got["reach"] == [2, 0]
    assert got["found"] == [[0, ["op-001"]], [1, ["t-1"]], [4, ["msg:1"]]]


def test_pulses_run_from_the_left_column_to_the_right_one():
    got = _node("""
import {leftward} from './evgraph.js';
const graph = {nodes: [{c: 0}, {c: 1}, {c: 3}, {c: 3}], links: [[0, 1, 'part of'], [2, 1, 'cited by'], [1, 2, 'proves'], [2, 3, 'x']]};
console.log(JSON.stringify([...leftward(graph)]));
""")
    assert got == [0, 1, 0, 0]



def test_the_brain_places_every_item_in_its_region_the_same_way_each_time():
    got = _node("""
import {place, REGION, wireframe} from './evbrain.js';
const keys = ['op','task','decision','proof','ev-event','ev-log','ev-code','ev-file','review','phase','session','message','assertion','claim','source'];
const a = keys.map(k => place(REGION[k], 't-' + k)), b = keys.map(k => place(REGION[k], 't-' + k));
const front = [...Array(200)].map((_, i) => place('pole', 'd' + i)[2]), back = [...Array(200)].map((_, i) => place('occipital', 'e' + i)[2]);
const depth = [...Array(300)].map((_, i) => place('frontal', 'f' + i)).map(p => Math.hypot(Math.abs(p[0]) - 0.25, p[1] - 0.1, p[2]));
const frame = wireframe();
const left = [...Array(100)].map((_, i) => place('language', 'm' + i)[0]);
const core = [...Array(100)].map((_, i) => place('thalamus', 'o' + i)).map(p => Math.hypot(...p));
console.log(JSON.stringify({regions: keys.every(k => REGION[k]), same: JSON.stringify(a) === JSON.stringify(b),
  finite: a.every(p => p.length === 3 && p.every(Number.isFinite)), front: Math.min(...front), back: Math.max(...back),
  left: Math.max(...left), core: Math.max(...core), bounded: a.every(p => p.every(v => Math.abs(v) < 1.3)),
  frontMean: front.reduce((x, y) => x + y) / front.length, deepest: Math.min(...depth), outermost: Math.max(...depth),
  rings: frame.length, ringsFinite: frame.every(l => l.length >= 30 && l.every(Number.isFinite))}));
""")
    assert got["regions"] and got["same"] and got["finite"] and got["bounded"]
    assert got["front"] > 0.2 and got["frontMean"] > 0.5 and got["back"] < -0.15   # the frontal pole in front, the occipital lobe behind
    assert got["deepest"] < 0.3 < got["outermost"]         # a lobe fills its volume: some neurons deep, some at the cortex
    assert got["rings"] > 40 and got["ringsFinite"]        # the wireframe outline of both hemispheres, cerebellum and stem
    assert got["left"] < 0                                 # the language areas on the left hemisphere
    assert got["core"] < 0.35                              # operations deep at the centre
