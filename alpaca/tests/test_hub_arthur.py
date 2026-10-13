"""t-484: Arthur acts out the record. The overview's `arthur` block reads each signal from its record
source, and arthur.js turns it into a looping pose (loopFor, by priority) and one-shot reactions
(reactionsFor, on growth between two reads)."""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from alpaca import db, profile, util

WEB = Path(__file__).resolve().parents[1] / "web"
NODE = shutil.which("node")


def node(script):
    if not NODE:
        pytest.skip("node is required")
    run = subprocess.run([NODE, "--input-type=module", "-e", script], capture_output=True, text=True,
                         cwd=str(WEB), timeout=60)
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout)


class Fake(profile.Profile):
    def __init__(self, **hooks):
        self.__dict__.update(hooks)


@pytest.fixture
def record(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    conn = db.connect(str(root))
    yield root, conn
    conn.close()


def event(conn, kind, op=None, ref=None, data=None, session="s1"):
    return db.append_event(conn, session=session, actor="agent", kind=kind, op=op, ref=ref, data=data or {})


def op(conn, ident, status="open"):
    db.upsert(conn, "ops", "id", {"id": ident, "intent": "Op " + ident, "status": status, "opened": "2026-10-01T00:00:00+00:00"})


def task(conn, ident, phase="build", status="doing", lease="2099-01-01T00:00:00+00:00", op_id="op-1"):
    db.upsert(conn, "tasks", "id", {"id": ident, "op": op_id, "phase": phase, "statement": "Task " + ident,
                                    "status": status, "claimant": "c", "lease_until": lease})


def block(root):
    from alpaca import hub
    return hub.overview(root)["arthur"]


def test_an_empty_record_gives_a_quiet_block(record):
    root, conn = record
    a = block(root)
    assert a["waiting"] == {"questions": 0, "briefs": 0, "orders": 0}
    assert a["thinking"] == {"design": 0, "selfchecks": 0} and a["reading"] == {"proofs": 0, "reviews": 0}
    assert a["research"] == 0 and a["sessions"] == 0
    assert a["events"] == {"op_closed": 0, "gate_pass": 0, "gate_fail": 0, "no_go": 0, "tests_failed": 0,
                           "proof_sealed": 0, "release_built": 0, "blocked": 0, "git_blocked": 0}
    assert a["verify"] == 0 and a["research_started"] == [] and a["last_work"] is None
    assert a["jobs_started"] == [] and a["party_days"] == []
    assert a["main"] is None                     # tmp_path is no git checkout


def test_waiting_counts_open_batch_items_unsigned_briefs_and_open_orders_of_open_ops(record):
    root, conn = record
    op(conn, "op-1")
    op(conn, "op-9", status="closed")
    # batch: the newest event per item wins (batch.py); q1 answered later, q2 proposed, a closed op ignored
    event(conn, "batch-item", "op-1", "q1", {"item": "q1", "state": "open"})
    event(conn, "batch-item", "op-1", "q2", {"item": "q2", "state": "proposed"})
    event(conn, "batch-item", "op-1", "q1", {"item": "q1", "state": "answered"})
    event(conn, "batch-item", "op-9", "q1", {"item": "q1", "state": "open"})
    # brief: the newest presented copy counts until a brief-signed names its sha12 (brief.py)
    event(conn, "owner-ask", "op-1", "brief:op-1:aaaaaaaaaaaa", {"subject": "brief:op-1:aaaaaaaaaaaa"})
    assert block(root)["waiting"] == {"questions": 1, "briefs": 1, "orders": 0}
    event(conn, "brief-signed", "op-1", "aaaaaaaaaaaa", {"op": "op-1"})
    # orders: open until an owner-order-close names it (orders.py)
    event(conn, "owner-order", "op-1", "ord-1", {"id": "ord-1", "quote": "keep it small"})
    event(conn, "owner-order", "op-1", "ord-2", {"id": "ord-2", "quote": "and fast"})
    event(conn, "owner-order-close", "op-1", "ord-2", {"id": "ord-2"})
    event(conn, "owner-order", "op-9", "ord-3", {"id": "ord-3", "quote": "closed op"})
    assert block(root)["waiting"] == {"questions": 1, "briefs": 0, "orders": 1}


def test_thinking_and_reading_follow_tasks_in_flight_packets_and_reviews(record, monkeypatch):
    root, conn = record
    op(conn, "op-1")
    task(conn, "t-1", phase="design")
    task(conn, "t-2", phase="design", lease="2000-01-01T00:00:00+00:00")     # lease ran out: not in flight
    task(conn, "t-3")
    task(conn, "t-4")
    event(conn, "proof-scaffold", "op-1", "t-3", {"path": "p"})               # being written
    event(conn, "proof-scaffold", "op-1", "t-4", {"path": "p"})
    event(conn, "proof-report", "op-1", "t-4", {"path": "p"})                 # sealed already
    # a self-check packet built now and not submitted runs; an older submitted one does not
    base = root / ".alpaca/selfcheck/op-1"
    for n in (1, 2):
        (base / ("design-build-%d" % n)).mkdir(parents=True)
        (base / ("design-build-%d" % n) / "packet.json").write_text(json.dumps({"created": util.now_iso()}))
    event(conn, "selfcheck", "op-1", "op-1:design->build", {"packet": ".alpaca/selfcheck/op-1/design-build-1", "verdict": "PASS"})
    # a research verification opened with no end yet, and one that ended
    event(conn, "research-verify-open", "op-1", "t-3", {"verification": "vf-1"})
    event(conn, "research-verify-open", "op-1", "t-3", {"verification": "vf-2"})
    event(conn, "research-verify-end", "op-1", "t-3", {"verification": "vf-2", "verdict": "PASS"})
    a = block(root)
    assert a["thinking"] == {"design": 1, "selfchecks": 1}
    assert a["reading"] == {"proofs": 1, "reviews": 1}
    event(conn, "selfcheck", "op-1", "op-1:design->build", {"packet": ".alpaca/selfcheck/op-1/design-build-2", "verdict": "FAIL"})
    assert block(root)["thinking"]["selfchecks"] == 0


def test_a_profile_review_flow_counts_live_assignments_and_marks_no_go(record, monkeypatch):
    root, conn = record
    monkeypatch.setattr(profile, "load", lambda r: Fake(events=lambda: {"review_flow_kinds": ["run-review-flow"]}))
    later = util_epoch() + 600
    event(conn, "run-review-flow", None, "j-1", {"assignment": {"expires_at": later}, "decision": None})
    event(conn, "run-review-flow", None, "j-2", {"assignment": {"expires_at": later - 3600}, "decision": None})
    a = block(root)
    assert a["reading"]["reviews"] == 1 and a["events"]["no_go"] == 0
    decided = event(conn, "run-review-flow", None, "j-1", {"assignment": {"expires_at": later},
                                                          "decision": {"value": "NO-GO"}})
    a = block(root)
    assert a["reading"]["reviews"] == 0 and a["events"]["no_go"] == decided["id"]


def util_epoch():
    from datetime import datetime
    return datetime.fromisoformat(util.now_iso()).timestamp()


def test_one_shot_marks_are_the_newest_event_ids_of_their_kind(record):
    root, conn = record
    op(conn, "op-1")
    db.upsert(conn, "sessions", "sid", {"sid": "a", "started": "2026-10-08T00:00:00+00:00"})
    db.upsert(conn, "sessions", "sid", {"sid": "b", "started": "2026-10-08T00:00:00+00:00"})
    closed = event(conn, "op-close", "op-1")
    passed = event(conn, "run", None, "boot-check", {"gate": "boot-check", "verdict": "PASS"})
    blocked = event(conn, "run", None, "scan", {"gate": "scan", "verdict": "BLOCKED"})      # not a FAIL
    door = event(conn, "phase-door-closed", "op-1", "design->build", {"verdict": "FAIL"})
    event(conn, "research-gate", "op-1", "t-1", {"stage": "build", "verdict": "FAIL"})   # a build, not a test
    tests = event(conn, "research-gate", "op-1", "t-1", {"stage": "fail-to-pass:h1", "verdict": "FAIL"})
    event(conn, "research-gate", "op-1", "t-1", {"stage": "unit", "verdict": "PASS"})
    a = block(root)
    assert a["sessions"] == 2
    assert a["events"] == {"op_closed": closed["id"], "gate_pass": passed["id"], "gate_fail": door["id"],
                           "no_go": 0, "tests_failed": tests["id"], "proof_sealed": 0, "release_built": 0,
                           "blocked": blocked["id"], "git_blocked": 0}
    check = event(conn, "selfcheck", "op-1", "op-1:design->build", {"verdict": "PASS"})
    assert block(root)["events"]["gate_pass"] == check["id"]


def test_research_counts_runs_the_engine_still_drives(record, monkeypatch):
    from alpaca import hub
    root, conn = record
    monkeypatch.setattr(hub, "_research_states", lambda c: {
        "op-1": {"state": "running"}, "op-2": {"state": "starting"}, "op-3": {"state": "finished"},
        "op-4": {"state": "held"}})
    assert block(root)["research"] == 2


def test_loop_priority_and_captions():
    got = node("""
import {loopFor} from './arthur.js';
const s=(x)=>{const r=loopFor(x);return r.action+'|'+r.caption;};
const all={offline:true,waiting:{orders:1},research:1,jobs:2,reading:{proofs:1},thinking:{design:1},tasks:3};
const drop=(k)=>{const o={...all};delete o[k];return o;};
let x=all; const order=[];
for (const k of ['offline','waiting','research','jobs','reading','thinking','tasks']) { order.push(s(x)); x={...x}; delete x[k]; }
order.push(s(x));
console.log(JSON.stringify({order,
 waitingAll:s({waiting:{orders:2,questions:1,briefs:1}}),
 search:[s({research:2}),s({research:1,jobs:1})],
 read:[s({reading:{proofs:2,reviews:1}}),s({reading:{reviews:1}})],
 think:[s({thinking:{selfchecks:1}}),s({thinking:{design:2,selfchecks:2}})],
 empty:s()}));""")
    assert got["order"] == ["problem|Offline", "waiting|Waiting on you: 1 order", "searching|1 research run active / 2 jobs running",
                            "searching|2 jobs running", "reading|Writing a proof", "thinking|Designing: 1 task",
                            "typing|3 tasks in flight", "idle|Idle"]
    assert got["waitingAll"] == "waiting|Waiting on you: 2 orders, 1 question, 1 brief to sign"
    assert got["search"] == ["searching|2 research runs active", "searching|1 research run active / 1 job running"]
    assert got["read"] == ["reading|Writing 2 proofs / Reviewing a log", "reading|Reviewing a log"]
    assert got["think"] == ["thinking|Self-check running", "thinking|Designing: 2 tasks / 2 self-checks running"]
    assert got["empty"] == "idle|Idle"


def test_reactions_fire_on_growth_between_two_reads_only():
    got = node("""
import {reactionsFor,readOverview} from './arthur.js';
const ev={op_closed:5,gate_pass:6,gate_fail:7,no_go:0,tests_failed:8};
const base={done:2,sessions:3,events:ev,main:{head:'a',merge:false}};
const w=(patch)=>({...base,...patch,events:{...ev,...(patch.events||{})}});
const r=(p,n)=>reactionsFor(p,n).map(x=>x.action+'|'+x.why);
const future=new Date(Date.now()+3600e3).toISOString();
const o={tasks:[{status:'done'},{status:'doing',claimant:'c',lease_until:future}],active_jobs:['j'],
 arthur:{waiting:{orders:1},research:0,reading:{proofs:0,reviews:0},thinking:{design:0,selfchecks:0},sessions:4,
 events:ev,main:{head:'b',merge:true}}};
const old={tasks:[],active_jobs:[]};
console.log(JSON.stringify({
 first:r(null,base), same:r(base,base),
 done:r(base,w({done:3})), opClosed:r(base,w({events:{op_closed:9}})), pass:r(base,w({events:{gate_pass:9}})),
 merge:r(base,w({main:{head:'b',merge:true}})), ffwd:r(base,w({main:{head:'b',merge:false}})),
 fail:r(base,w({events:{gate_fail:9,no_go:1,tests_failed:9}})), session:r(base,w({sessions:4})),sessions:r(base,w({sessions:5})),
 several:r(base,w({done:3,sessions:4,events:{gate_fail:9}})),
 unknown:r({...base,events:null,sessions:null,main:null},base), back:r(base,{...base,events:null,sessions:null}),
 read:readOverview(o), older:readOverview(old)}));""")
    assert got["first"] == [] and got["same"] == []
    assert got["done"] == ["celebrate|A task is done"]
    assert got["opClosed"] == ["celebrate|An op closed"] and got["pass"] == ["celebrate|A gate passed"]
    # t-487: a merge on main plays merging_papers, then the celebrate it always had
    assert got["merge"] == ["merging_papers|Merged to main", "celebrate|Merged to main"] and got["ffwd"] == []
    assert got["fail"] == ["problem|A gate failed / A review said NO-GO / Tests failed"]
    assert got["session"] == ["greeting|A new session joined"] and got["sessions"] == ["greeting|2 new sessions joined"]
    assert got["several"] == ["celebrate|A task is done", "problem|A gate failed", "greeting|A new session joined"]
    assert got["unknown"] == [] and got["back"] == []       # a read without the block never fires
    state, marks = got["read"]["state"], got["read"]["marks"]
    assert state["tasks"] == 1 and state["jobs"] == 1 and state["waiting"] == {"orders": 1}
    assert marks == {"done": 1, "sessions": 4, "events": {"op_closed": 5, "gate_pass": 6, "gate_fail": 7, "no_go": 0,
                                                         "tests_failed": 8}, "main": {"head": "b", "merge": True},
                     "owed": 1, "ops": None}
    assert got["older"]["marks"] == {"done": 0, "sessions": None, "events": None, "main": None, "owed": None, "ops": None}


def test_pacing_waits_for_idle_minutes_and_slides_with_css():
    got = node("""
import {paceDue,PACE_IDLE_MS,LEG_MS} from './arthur.js';
const m=PACE_IDLE_MS;
console.log(JSON.stringify({m,leg:LEG_MS,due:paceDue({action:'idle',busy:null,sinceMove:m,sincePace:m}),
 typing:paceDue({action:'typing',busy:null,sinceMove:m,sincePace:m}),busy:paceDue({action:'idle',busy:'once',sinceMove:m,sincePace:m}),
 moved:paceDue({action:'idle',busy:null,sinceMove:m-1,sincePace:m}),recent:paceDue({action:'idle',busy:null,sinceMove:m,sincePace:1})}));""")
    assert got["m"] == 180000 and got["leg"] == 2 * 1060
    assert got["due"] and not (got["typing"] or got["busy"] or got["moved"] or got["recent"])
    js = (WEB / "arthur.js").read_text(encoding="utf-8")
    assert "addEventListener('hashchange'" in js and "translateX(" in js and "prefers-reduced-motion" in js
    css = (WEB / "hub.css").read_text(encoding="utf-8")
    assert re.search(r"\.arthur-stage \{ transition:transform", css)
    assert re.search(r"prefers-reduced-motion:reduce\) \{ \.arthur-stage \{ transition:none", css)
    for name in ("arthur.js", "hub.css"):
        assert re.search(r"[^\x00-\x7f]", (WEB / name).read_text(encoding="utf-8")) is None, name


# ---- t-487: package 2's 21 actions ---------------------------------------------------------------

VENDOR = WEB / "vendor" / "arthur"
NEW_ACTIONS = ("sleeping", "stamping", "raising_hand", "juggling", "drinking_coffee", "shield_up", "counting_coins",
               "packing_box", "merging_papers", "telescope", "spit", "sunglasses", "nightcap", "yawn_stretch",
               "mimic_typing", "dance", "party_hat", "headphones", "detective", "checklist", "peekaboo")


def manifest(raw=False):
    """The vendored manifest as the avatar reads it (through arthur.js stripsManifest), or as shipped."""
    if not NODE:
        pytest.skip("node is required")
    script = ("import {createRequire} from 'module';import {stripsManifest} from './arthur.js';"
              "const m=createRequire(import.meta.url)(%s);process.stdout.write(JSON.stringify(%s));"
              % (json.dumps(str(VENDOR / "assets" / "avatar-manifest.js")), "m" if raw else "stripsManifest(m)"))
    run = subprocess.run([NODE, "--input-type=module", "-e", script], capture_output=True, text=True, cwd=str(WEB), timeout=60)
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout)


def png_size(path):
    head = path.read_bytes()[:24]
    assert head[:8] == b"\x89PNG\r\n\x1a\n", path
    return int.from_bytes(head[16:20], "big"), int.from_bytes(head[20:24], "big")


def webp_size(path):
    """(width, height) of a lossless WebP (the gaze atlas ships as one, t-595) read from its header."""
    head = path.read_bytes()[:32]
    assert head[:4] == b"RIFF" and head[8:12] == b"WEBP", path
    if head[12:16] == b"VP8X":
        return int.from_bytes(head[24:27], "little") + 1, int.from_bytes(head[27:30], "little") + 1
    assert head[12:16] == b"VP8L" and head[20] == 0x2F, path
    bits = int.from_bytes(head[21:25], "little")
    return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1


def test_the_record_signals_of_package_2(record, monkeypatch):
    from alpaca import hub
    root, conn = record
    op(conn, "op-1")
    task(conn, "t-1", phase="verify")
    task(conn, "t-2", phase="verify", lease="2000-01-01T00:00:00+00:00")     # lease ran out: not in flight
    task(conn, "t-3")
    sealed = event(conn, "proof-report", "op-1", "t-3", {"path": "p"})
    event(conn, "release-build", None, "r1", {"status": "FAIL"})              # a failed build packs nothing
    built = event(conn, "release-build", None, "r2", {"status": "PASS"})
    event(conn, "release-build", None, "r3", {"status": "FAIL"})
    denied = event(conn, "research-guard-deny", "op-1", "j-1", {"rule": "program"})
    a = block(root)
    assert a["verify"] == 1
    assert a["events"]["proof_sealed"] == sealed["id"] and a["events"]["release_built"] == built["id"]
    assert a["events"]["blocked"] == denied["id"]
    gate = event(conn, "run", None, "scan", {"gate": "scan", "verdict": "BLOCKED"})
    event(conn, "run", None, "scan", {"gate": "scan", "verdict": "FAIL"})     # a FAIL is problem, not shield_up
    moved = event(conn, "task-move", "op-1", "t-3", {"to": "doing"})
    event(conn, "heartbeat", None, None, {})                                  # not task or job activity
    a = block(root)
    assert a["events"]["blocked"] == gate["id"]
    # the git guard never opens the record: its deny note file grows instead (brief 2g)
    from alpaca.hooks import git_guard
    git_guard.note_denial(str(root), "git stash")
    first = block(root)["events"]["git_blocked"]
    git_guard.note_denial(str(root), "git reset --hard")
    assert 0 < first < block(root)["events"]["git_blocked"]
    assert a["last_work"] == pytest.approx(hub._epoch(moved["ts"]))
    # research: the start of each run the engine still drives (research-run-start of its run id)
    event(conn, "research-run-start", "op-1", "rr-1", {"run": "rr-1"})
    event(conn, "research-run-start", "op-2", "rr-2", {"run": "rr-2"})
    monkeypatch.setattr(hub, "_research_states", lambda c: {"op-1": {"run": "rr-1", "state": "running"},
                                                            "op-2": {"run": "rr-2", "state": "finished"}})
    a = block(root)
    assert a["research"] == 1 and len(a["research_started"]) == 1


def test_jobs_started_reads_iso_and_epoch_stamps():
    from alpaca import hub
    data = {"items": [{"id": "a", "started_at": "2026-10-09T01:00:00+00:00"}, {"id": "b", "started_at": 1760000000},
                      {"id": "c", "started_at": 1760000000123}, {"id": "d", "started_at": None},
                      {"id": "e", "started_at": "2026-10-09T00:00:00+00:00"}]}
    assert hub._jobs_started(data, ["a", "b", "c", "d"]) == [1760000000.0, 1760000000.123,
                                                            hub._epoch("2026-10-09T01:00:00+00:00")]


def test_party_days_come_from_an_optional_project_yaml_list(record):
    import datetime
    from alpaca import hub, project_schema
    root, conn = record
    assert block(root)["party_days"] == []
    (root / "project.yaml").write_text("name: Demo\nstudio:\n  party_days: [2026-12-24, '2026-10-09', '2026-02-30', "
                                       "nope, 2026-10-09]\n", encoding="utf-8")
    assert block(root)["party_days"] == ["2026-10-09", "2026-12-24"]
    assert hub.party_days({"studio": {"party_days": [datetime.date(2027, 1, 2)]}}) == ["2027-01-02"]
    assert hub.party_days({"studio": "x"}) == [] and hub.party_days({}) == []
    # the project.yaml schema ignores the optional key: it reports no error for it
    ok, errors = project_schema.validate({"studio": {"party_days": ["2026-10-09"]}})
    assert not any(e["key"].startswith("studio") for e in errors)


def test_package_2_loop_priority_and_thresholds():
    got = node("""
import {loopFor,JUGGLE_AT,RESEARCH_LONG_MS,JOB_LONG_MS,SLEEP_QUIET_MS} from './arthur.js';
const now=Date.parse('2026-10-09T12:00:00Z'), s=x=>loopFor({now,...x}).action, ago=ms=>(now-ms)/1000;
const all={offline:true,waiting:{orders:1},typing:true,roadmap:true,tasks:6,jobs:2,research:1,researchStarted:[ago(RESEARCH_LONG_MS+1)],
 jobsStarted:[ago(JOB_LONG_MS+1)],reading:{proofs:1},thinking:{design:1},verify:1,night:true};
const steps=[['offline'],['typing'],['roadmap'],['waiting'],['tasks','juggle'],['researchStarted'],['jobsStarted'],['research','jobs'],
 ['reading'],['thinking'],['verify'],['tasks']];
let x={...all}; const order=[];
for (const keys of steps) { order.push(s(x)); x={...x}; for (const k of keys) { if (k==='juggle') { x.tasks=1; } else if (k!=='tasks'||x.tasks===1) delete x[k]; } }
order.push(s(x)); delete x.night; order.push(s(x));
console.log(JSON.stringify({order,
 juggle:[s({tasks:JUGGLE_AT-1}),s({tasks:JUGGLE_AT}),s({tasks:5,jobs:3}),loopFor({now,tasks:7,jobs:1}).caption],
 research:[s({research:1,researchStarted:[ago(RESEARCH_LONG_MS)]}),s({research:1,researchStarted:[ago(RESEARCH_LONG_MS+1000)]}),
  s({research:0,researchStarted:[ago(RESEARCH_LONG_MS+1000)]}),loopFor({now,research:2,researchStarted:[ago(RESEARCH_LONG_MS+1000)]}).caption],
 jobs:[s({jobs:1,jobsStarted:[ago(JOB_LONG_MS)]}),s({jobs:1,jobsStarted:[ago(JOB_LONG_MS+1000)]}),loopFor({now,jobs:1,jobsStarted:[ago(JOB_LONG_MS+1000)]}).caption],
 sleep:[s({lastWork:ago(SLEEP_QUIET_MS-1000)}),s({lastWork:ago(SLEEP_QUIET_MS)}),loopFor({now,lastWork:ago(SLEEP_QUIET_MS+120000)}).caption,
  s({night:true}),s({night:true,tasks:1}),s({lastWork:ago(SLEEP_QUIET_MS),tasks:1}),s({lastWork:null})],
 captions:[loopFor({now,typing:true}).caption,loopFor({now,roadmap:true}).caption,loopFor({now,verify:2}).caption]}));""")
    assert got["order"] == ["problem", "mimic_typing", "telescope", "waiting", "juggling", "headphones", "drinking_coffee",
                            "searching", "reading", "thinking", "checklist", "typing", "sleeping", "idle"]
    assert got["juggle"] == ["typing", "juggling", "juggling", "7 tasks in flight / 1 job running"]
    assert got["research"] == ["searching", "headphones", "idle", "1 research run active over 30 min"]
    assert got["jobs"] == ["searching", "drinking_coffee", "1 job running over an hour"]
    assert got["sleep"] == ["idle", "sleeping", "Asleep: quiet for 32 min", "sleeping", "typing", "typing", "idle"]
    assert got["captions"] == ["Typing along with you", "Looking over the roadmap", "Verifying: 2 tasks"]


def test_package_2_reactions_between_two_reads():
    got = node("""
import {reactionsFor,readOverview} from './arthur.js';
const ev={op_closed:1,gate_pass:1,gate_fail:1,no_go:0,tests_failed:0,proof_sealed:4,release_built:2,blocked:3};
const base={done:2,sessions:1,events:ev,main:{head:'a',merge:false},owed:1,ops:{'op-1':false,'op-2':true}};
const w=p=>({...base,...p,events:{...ev,...(p.events||{})}});
const r=(p,n)=>reactionsFor(p,n).map(x=>x.action+'|'+x.why);
const o={tasks:[],active_jobs:[],ops:[{id:'op-1',tasks_total:3,tasks_done:3},{id:'op-2',tasks_total:0,tasks_done:0}],
 arthur:{waiting:{questions:1,briefs:1,orders:1},events:ev,sessions:1,verify:2,research_started:[5],jobs_started:[6],last_work:7,party_days:['2026-10-09']}};
console.log(JSON.stringify({
 owed:r(base,w({owed:2})), answered:r(base,w({owed:0})), unknown:r({...base,owed:null},w({owed:3})),
 blocked:r(base,w({events:{blocked:9}})), gitBlocked:r({...base,events:{...ev,git_blocked:10}},w({events:{git_blocked:90}})), sealed:r(base,w({events:{proof_sealed:9}})), built:r(base,w({events:{release_built:9}})),
 dance:r(base,w({ops:{'op-1':true,'op-2':true}})), newOp:r(base,w({ops:{'op-1':false,'op-2':true,'op-3':true}})), noOps:r({...base,ops:null},base),
 all:r(base,w({owed:2,done:3,main:{head:'b',merge:true},ops:{'op-1':true,'op-2':true},
   events:{blocked:9,proof_sealed:9,release_built:9,gate_fail:9},sessions:2})),
 read:readOverview(o)}));""")
    assert got["owed"] == ["raising_hand|A new question for you"] and got["answered"] == [] and got["unknown"] == []
    assert got["blocked"] == ["shield_up|A guard or a gate blocked a step"]
    assert got["gitBlocked"] == ["shield_up|A guard or a gate blocked a step"]
    assert got["sealed"] == ["stamping|A proof was sealed"] and got["built"] == ["packing_box|A release candidate was built"]
    assert got["dance"] == ["dance|Every task of op-1 is done"]
    assert got["newOp"] == [] and got["noOps"] == []          # an op first seen all done does not dance
    assert [x.split("|")[0] for x in got["all"]] == ["raising_hand", "shield_up", "merging_papers", "celebrate", "dance",
                                                       "stamping", "packing_box", "problem", "greeting"]
    state, marks = got["read"]["state"], got["read"]["marks"]
    assert state["verify"] == 2 and state["researchStarted"] == [5] and state["jobsStarted"] == [6]
    assert state["lastWork"] == 7 and state["partyDays"] == ["2026-10-09"]
    assert marks["owed"] == 3 and marks["ops"] == {"op-1": True, "op-2": False}


def test_page_signals_of_package_2():
    got = node("""
import {rapidClicks,themeReaction,yawnDue,peekDue,newestSpike,partyToday,localDay,routeOf,TUNE} from './arthur.js';
let t=[],spits=[];
for (let i=0;i<10;i++){const c=rapidClicks(t,1000+i*150);t=c.times;spits.push(c.spit);}
const slow=[];t=[];for (let i=0;i<12;i++){const c=rapidClicks(t,i*250);t=c.times;slow.push(c.spit);}
const board={sessions:[{signals:{runaway:{bins:[{start:100},{start:300}]}}},{signals:{}},{signals:{runaway:{bins:[{start:200}]}}}]};
const d=new Date(2026,9,9,8,30);
console.log(JSON.stringify({tune:TUNE,spits,slow,
 theme:[themeReaction('dark','light'),themeReaction('light','dark'),themeReaction('dark','dark'),themeReaction('light',null)],
 yawn:[yawnDue(0,1e9),yawnDue(1000,1000+TUNE.yawnAwayMs-1),yawnDue(1000,1000+TUNE.yawnAwayMs)],
 peek:[peekDue({action:'idle',busy:null,restSince:1,now:1+TUNE.peekIdleMs}),peekDue({action:'sleeping',busy:null,restSince:1,now:1+TUNE.peekIdleMs}),
  peekDue({action:'idle',busy:null,restSince:1,now:TUNE.peekIdleMs}),peekDue({action:'typing',busy:null,restSince:1,now:1e9}),
  peekDue({action:'idle',busy:'once',restSince:1,now:1e9}),peekDue({action:'idle',busy:null,restSince:0,now:1e9})],
 spike:[newestSpike(board),newestSpike({}),newestSpike(null)],
 party:[localDay(d),partyToday(['2026-10-09'],d),partyToday(['2026-10-10'],d),partyToday(null,d)],
 route:[routeOf('#roadmap?view=mission'),routeOf('#mission?node=x'),routeOf('#cockpit'),routeOf('')]}));""")
    assert got["tune"] == {"typingQuietMs": 600, "spitClicks": 10, "spitWindowMs": 2000, "yawnAwayMs": 300000,
                           "peekIdleMs": 60000}
    assert got["spits"] == [False] * 9 + [True]
    assert got["slow"] == [False] * 12                       # 250 ms apart: never ten inside 2 s
    assert got["theme"] == ["sunglasses", "nightcap", None, None]
    assert got["yawn"] == [False, False, True]
    assert got["peek"] == [True, True, False, False, False, False]
    assert got["spike"] == [300, 0, 0]
    assert got["party"] == ["2026-10-09", True, False, False]
    assert got["route"] == ["roadmap", "roadmap", "cockpit", ""]


def test_vendored_package_2_runtime_and_manifest():
    # brief 2f: the package's manifest ships as it is; its new actions still name the full-size sheets
    raw = manifest(raw=True)["actions"]
    assert all(raw[name]["image"].startswith("new-sheets/") and "frameRects" in raw[name] for name in NEW_ACTIONS)
    assert "manifest: stripsManifest(window.ArthurAssets," in (WEB / "arthur.js").read_text(encoding="utf-8")
    m = manifest()
    actions = m["actions"]
    assert len(actions) == 32 and set(NEW_ACTIONS) <= set(actions)
    for name, action in actions.items():
        path = VENDOR / "assets" / action["image"]
        assert action["image"] == "strips/%s.png" % name and path.is_file(), name
        assert png_size(path) == (len(action["frames"]) * m["width"], m["height"]), name
        assert len(action["durationsMs"]) == len(action["frames"]), name
        if name in NEW_ACTIONS:
            assert action["frames"] == [0, 1, 2, 3, 4, 5] and action["label"] and action["trigger"], name
        assert not {"frameRects", "sourceSize", "destinationRect"} & set(action), name
    assert webp_size(VENDOR / "assets" / m["atlas"]["image"]) == (m["atlas"]["width"], m["atlas"]["height"])
    # each new action keeps the package's timing and loop flag
    assert actions["checklist"]["durationsMs"] == [200, 180, 180, 180, 180, 320] and actions["checklist"]["loop"] is False
    assert actions["telescope"]["loop"] is True and actions["sleeping"]["loop"] is False
    for name in ("arthur-core.js", "arthur-geometry.js", "arthur-avatar.js", "arthur-avatar.css"):
        assert (VENDOR / name).is_file()
    html = (WEB / "hub.html").read_text(encoding="utf-8")
    assert html.index("arthur-core.js") < html.index("arthur-geometry.js") < html.index("arthur-avatar.js")


def test_checklist_strip_repeats_its_first_pose():
    # the package plays checklist's poses [1,1,2,3,4,5]: slots 0 and 1 of the strip are one pose
    image = pytest.importorskip("PIL.Image")
    strip = image.open(VENDOR / "assets" / "strips" / "checklist.png").convert("RGBA")
    first, second, third = (strip.crop((i * 192, 0, (i + 1) * 192, 208)).tobytes() for i in range(3))
    assert first == second != third


def test_every_arthur_asset_is_served_and_revalidates(project):
    import http.server
    import threading
    import urllib.error
    import urllib.request
    from alpaca import serve
    m = manifest()
    names = ["vendor/arthur/" + n for n in ("arthur-core.js", "arthur-geometry.js", "arthur-avatar.js", "arthur-avatar.css",
                                            "assets/avatar-manifest.js")]
    names += ["vendor/arthur/assets/" + m["atlas"]["image"]] + ["vendor/arthur/assets/" + a["image"] for a in m["actions"].values()]
    assert sorted(serve.ARTHUR_ACTIONS) == sorted(m["actions"])
    live = serve.Live(project)
    live.refresh()
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), serve.make_handler(live, project))
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = "http://127.0.0.1:%d/hub/assets/" % httpd.server_address[1]

    def get(name, tag=None):
        req = urllib.request.Request(url + name)
        if tag:
            req.add_header("If-None-Match", tag)
        try:
            r = urllib.request.urlopen(req, timeout=15)
        except urllib.error.HTTPError as exc:
            r = exc
        return r.status, r.headers, r.read()

    try:
        import hashlib
        for name in names:
            status, headers, body = get(name)
            assert status == 200, name
            # kept for a day only under a versioned URL (?v=); unversioned it revalidates (t-595)
            assert headers["Cache-Control"] == "no-cache", name
            if not name.endswith(".css"):
                assert get(name + "?v=x")[1]["Cache-Control"] == "max-age=86400", name
            assert headers["ETag"] == '"%s"' % hashlib.sha256((WEB / name).read_bytes()).hexdigest(), name
        status, headers, body = get(names[-1], headers["ETag"])
        assert status == 304 and body == b"" and headers["ETag"]
        assert get(names[-1], '"stale"')[0] == 200
        # the other hub assets keep their rule: no ETag, never stored
        status, headers, _ = get("hub.js")
        assert status == 200 and headers["Cache-Control"] == "no-store" and headers.get("ETag") is None
        assert get("vendor/arthur/assets/new-sheets/spit.png")[0] == 404
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_vendored_arthur_stays_inside_its_size_budget():
    total = sum(p.stat().st_size for p in VENDOR.rglob("*") if p.is_file())
    assert total < 12 * 1024 * 1024, total


def test_only_a_lost_connection_counts_toward_offline():
    # brief L4, 2a, 2d: a lost live connection is offline after 30 s; the other amber states of the
    # connection row (a reachable server) are not
    got = node("""
import {connectionLost} from './arthur.js';
const row=(cls,label)=>({classList:{contains:c=>c===cls},querySelector:()=>({textContent:label})});
console.log(JSON.stringify(['Reconnecting','Offline','Stale','Preparing','Live - capture gaps','Page error','Status unknown']
  .map(l=>[l,connectionLost(row('error',l))]).concat([['Live (no error)',connectionLost(row('',"Reconnecting"))]])));""")
    assert dict(got) == {"Reconnecting": True, "Offline": True, "Stale": False, "Preparing": False,
                         "Live - capture gaps": False, "Page error": False, "Status unknown": False,
                         "Live (no error)": False}


def test_a_failed_quiet_refresh_neither_starts_nor_clears_the_offline_wait():
    # brief L4, 2a: while the server is gone the health check writes "Offline" every 10 s and the 15 s refresh
    # writes "Stale" in between; "Stale" must not clear the 30 s wait, or Arthur never goes offline on a timer page
    got = node("""
import {connectionStep} from './arthur.js';
const row=(cls,label)=>({classList:{contains:c=>c===cls},querySelector:()=>({textContent:label})});
console.log(JSON.stringify(['Reconnecting','Offline','Stale','Preparing','Live - capture gaps','Page error']
  .map(l=>[l,connectionStep(row('error',l))]).concat([['Live',connectionStep(row('','Live'))]])));""")
    assert dict(got) == {"Reconnecting": "start", "Offline": "start", "Stale": "hold", "Preparing": "clear",
                         "Live - capture gaps": "clear", "Page error": "clear", "Live": "clear"}


def test_avatar_loads_the_idle_strip_first_and_keeps_its_frame_while_another_loads():
    # t-595: only the idle strip loads before the avatar is ready; a strip the player reaches loads on first
    # use and the canvas keeps the frame it shows until that image decodes; the atlas waits for the cursor
    got = node("""
import {createRequire} from 'module';
import {stripsManifest} from './arthur.js';
const req = createRequire(import.meta.url), vendor = %s;
const raw = req(vendor + '/assets/avatar-manifest.js');
const Geometry = req(vendor + '/arthur-geometry.js');
const m = stripsManifest(raw, 'abc');
const wanted = [], pending = {}, draws = [];
const sizeOf = src => { const image = new URL(src).pathname.replace('/assets/', '') + new URL(src).search;
  const a = Object.values(m.actions).find(a => a.image === image);
  return a ? Object.values(Geometry.sourceSize(a, m.width, m.height)) : [m.atlas.width, m.atlas.height]; };
class Img {
  set src(v) { wanted.push(v); [this.naturalWidth, this.naturalHeight] = sizeOf(v); this.href = v; pending[v] = () => this.onload(); }
  removeAttribute() {}
}
const el = () => ({style: {}, setAttribute() {}, appendChild() {}, remove() {}, getBoundingClientRect: () => ({left: 0, top: 0, width: 1, height: 1}),
  getContext: () => ({clearRect() {}, drawImage: image => draws.push(image.href)})});
const target = {addEventListener() {}, removeEventListener() {}};
const doc = {hidden: false, baseURI: 'http://x/', createElement: el, ...target, documentElement: target};
const win = {Image: Img, matchMedia: () => ({matches: false, ...target}), ...target, requestAnimationFrame: () => 1, cancelAnimationFrame() {},
  CustomEvent: class { constructor(name, o) { this.type = name; this.detail = o.detail; } }};
doc.defaultView = win;
globalThis.document = doc;
globalThis.ArthurCore = req(vendor + '/arthur-core.js'); globalThis.ArthurGeometry = Geometry;
const Avatar = req(vendor + '/arthur-avatar.js');
const tick = () => new Promise(r => setTimeout(r, 0));
const made = Avatar.create({ownerDocument: doc, appendChild() {}, dispatchEvent() {}}, {assetBaseURL: 'http://x/assets/', manifest: m});
await tick();
const out = {created: [...wanted]};
pending[wanted[0]]();
const avatar = await made;
out.first = [...draws];
avatar.setAction('greeting');
await tick();
out.asked = [...wanted];
out.during = [...draws];
pending[wanted[1]]();
await tick();
out.after = [...draws];
avatar.setAction('greeting');
await tick();
out.again = [...wanted];
console.log(JSON.stringify(out));
""" % json.dumps(str(VENDOR)))
    idle, greeting = "http://x/assets/strips/idle.png?v=abc", "http://x/assets/strips/greeting.png?v=abc"
    assert got["created"] == [idle]
    assert got["first"] == [idle]
    assert got["asked"] == [idle, greeting]
    assert got["during"] == [idle]
    assert got["after"] == [idle, greeting]
    assert got["again"] == [idle, greeting]


def test_hub_page_versions_arthur_and_the_vendor_files_are_kept_for_a_day(project):
    # t-595: the page links Arthur's scripts with a content version and carries it for the artwork
    from alpaca import serve
    page = serve.hub_page(str(project))
    version = serve.arthur_version()
    assert re.fullmatch(r"[0-9a-f]{12}", version)
    assert '<meta name="arthur-version" content="%s">' % version in page
    for name in ("assets/avatar-manifest.js", "arthur-core.js", "arthur-geometry.js", "arthur-avatar.js"):
        assert 'src="/hub/assets/vendor/arthur/%s?v=%s"' % (name, version) in page, name


def test_session_asks_are_the_recent_sessions_title_and_latest_ask(project):
    # t-595: the cockpit's Parallel sessions panel reads this instead of the whole /data.json
    from alpaca import serve
    live = serve.Live(project)
    sessions = [{"sid": "s%02d" % i, "title": "T%d" % i, "last": "2026-10-%02dT00:00:00+00:00" % (i % 28 + 1),
                 "human_prompts": [{"ts": "a", "text": "first"}, {"ts": "b", "text": "x" * 900}], "tokens": {"input": 9}}
                for i in range(60)]
    live.data_json, live.data_rev = json.dumps({"sessions": sessions}), "r1"
    body, etag = live.session_asks()
    rows = json.loads(body)["sessions"]
    assert len(rows) == serve.SESSION_ASKS
    assert [r["sid"] for r in rows[:2]] == ["s27", "s55"]       # the newest first (two share the last day)
    assert set(rows[0]) == {"sid", "title", "human_prompts"}
    assert [a["ts"] for a in rows[0]["human_prompts"]] == ["b"] and len(rows[0]["human_prompts"][0]["text"]) == serve.SESSION_ASK_CHARS
    assert len(body) < 40000
    assert live.session_asks() == (body, etag)
    live.data_json, live.data_rev = json.dumps({"sessions": sessions[:1]}), "r2"
    assert len(json.loads(live.session_asks()[0])["sessions"]) == 1


def test_fonts_and_thinking_orbs_are_versioned_where_hub_css_and_orb_js_name_them():
    # t-595: a vendor file is kept for a day only under a content-versioned URL, so the stylesheet and the module
    # that name it carry the version and a changed file has a new URL
    import hashlib
    from alpaca import serve
    css = serve.vendor_links("hub.css", (WEB / "hub.css").read_bytes()).decode()
    font = hashlib.sha256((WEB / "vendor" / "plex-sans.woff2").read_bytes()).hexdigest()[:12]
    assert "url('/hub/assets/vendor/plex-sans.woff2?v=%s')" % font in css
    assert not re.search(r"url\('[^']*woff2'\)", css)
    orb = serve.vendor_links("orb.js", (WEB / "orb.js").read_bytes()).decode()
    tag = hashlib.sha256((WEB / "vendor" / "thinking-orbs.js").read_bytes()).hexdigest()[:12]
    assert "from './vendor/thinking-orbs.js?v=%s';" % tag in orb
    assert serve.vendor_links("vendor/thinking-orbs.js", b"x") == b"x"


def test_session_asks_always_include_the_live_sessions(project):
    # t-595: a live session is never cut by the window, however old its last record is
    from alpaca import serve
    live = serve.Live(project)
    sessions = [{"sid": "s%02d" % i, "title": "T%d" % i, "last": "2026-10-%02dT00:00:00+00:00" % (i % 28 + 1),
                 "human_prompts": [{"ts": "b", "text": "ask %d" % i}]} for i in range(60)]
    sessions.append({"sid": "quiet", "title": "Quiet", "last": "2020-01-01T00:00:00+00:00", "human_prompts": []})
    live.data_json, live.data_rev = json.dumps({"sessions": sessions}), "r1"
    live.board_json, live.board_rev = json.dumps({"pulse": {"now": {"sessions": {"work": [{"sid": "quiet"}]}}}}), "b1"
    rows = json.loads(live.session_asks()[0])["sessions"]
    assert len(rows) == serve.SESSION_ASKS + 1 and "quiet" in [r["sid"] for r in rows]


def test_cockpit_reads_keep_only_what_the_panels_draw(project):
    # t-595: slim reads of /board/data.json, /hub/mission.json and /hub/research.json for the cockpit
    from alpaca import serve
    live = serve.Live(project)
    live.board_json = json.dumps({"pulse": {"now": {"sessions": {"work": [
        {"sid": "a", "last_beat": "t", "last_tool": "Bash", "claims": ["t-1"], "lapsed": [], "turns": 5, "operator": "x", "beats": [1] * 99}]}}},
        "messages": ["x"] * 50})
    body = json.loads(live.live_work()[0])
    work = body["pulse"]["now"]["sessions"]["work"]
    assert set(work[0]) == {"sid", "active", "last_beat", "last_tool", "claims", "lapsed"} and "messages" not in body
    full = {"title": "M", "groups": [{"id": "g", "title": "G", "state": "x", "extra": 1}], "edges": [{"a": 1}],
            "nodes": [{"id": "n", "group": "g", "state": "ok", "purpose": "p" * 99, "tasks": [{"id": "t-1", "status": "done", "title": "long"}]}],
            "development": {"tasks": [{"id": "t-1", "status": "done", "title": "long"}],
                            "operations": [{"op": "o", "groups": [{"id": "g", "nodes": [{"id": "t-1", "evidence": "valid", "state": "done", "after": [1]}]}]}]}}
    slim = serve.mission_compact(full)
    assert slim["nodes"] == [{"id": "n", "group": "g", "state": "ok", "tasks": [{"id": "t-1", "status": "done"}]}]
    assert slim["groups"] == [{"id": "g", "title": "G"}] and slim["title"] == "M"
    assert slim["development"]["operations"][0]["groups"][0]["nodes"] == [{"id": "t-1", "evidence": "valid", "state": "done"}]
    runs = [{"run": "r%d" % i, "state": "running" if i == 0 else "stopped"} for i in range(7)]
    got = serve.research_compact({"runs": runs, "sources": [1]})
    assert [r["run"] for r in got["runs"]] == ["r0", "r1", "r2", "r3"] and got["recorded"] == 7


def test_the_gaze_atlas_holds_only_the_gaze_rows_and_the_manifest_follows():
    # t-595: the shipped atlas is cut to the rows of the 16 gaze frames (decoding the full sheet stalled Chrome's
    # main thread); stripsManifest moves the frames up by the cut, so each still points at its own pixels
    raw, m = manifest(raw=True), manifest()
    top = min(f["y"] for f in raw["gaze"]["frames"])
    assert [f["y"] for f in m["gaze"]["frames"]] == [f["y"] - top for f in raw["gaze"]["frames"]]
    assert m["atlas"]["height"] == max(f["y"] + f["height"] for f in m["gaze"]["frames"]) < raw["atlas"]["height"]
    assert webp_size(VENDOR / "assets" / m["atlas"]["image"]) == (m["atlas"]["width"], m["atlas"]["height"])
    assert all(f["y"] >= 0 and f["x"] + f["width"] <= m["atlas"]["width"] for f in m["gaze"]["frames"])
