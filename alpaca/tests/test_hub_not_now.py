"""Not now in the hub (op-057 t-618): the JS words match the record words, every record status has a
label and a tone, parked work is never the focus or counted as open, a plan without parked work renders
as before, the op view hosts the Not now panel once, and the touched web files stay ASCII."""
import re

from alpaca import ops, taskplan
from alpaca.tests.hub_not_now_kit import WEB, deferral, deferred, node, overview, plan_node, task, words

ROADMAP = "const {renderRoadmap,renderPhaseMap,notNowWords,NOT_NOW}=await import('./roadmap.js');"
TOUCHED = ("theme.css", "hub.css", "hub.js", "cockpit.js", "cockpit.css", "roadmap.js", "roadmap.css",
           "inflight.js", "inflight.css", "mission.js", "mission.css")


def read(name):
    return (WEB / name).read_text(encoding="utf-8")


def test_js_words_match_the_record_words(tmp_path):
    rows = [deferral(), deferral(reason=None, when=None), deferral(reason="", when=""),
            deferral(reason="line one\n\tline two  spaced", when="after:t-1"),
            deferral(met=True, met_by="t-1 is done"), deferral(error="ValueError"), None]
    got = node(ROADMAP + "out(DATA.map(r=>notNowWords(r)))", rows, tmp_path)
    for row, js in zip(rows, got):
        assert js == ops.not_now_words(row), row


def test_every_record_status_has_a_label_and_a_tone():
    cockpit = read("cockpit.js")
    labels = re.search(r"export const STATE_LABELS=\{(.*?)\};", cockpit).group(1)
    tones = re.search(r"\nconst tones=\{(.*?)\};", cockpit).group(1)
    for status in ops.STATUSES:
        assert re.search(r"\b%s:" % status, labels), status + " has no STATE_LABELS entry"
        if status != "open":                                # open is the quiet tone on purpose
            assert re.search(r"\b%s:" % status, tones), status + " has no tone"
    assert "deferred:'%s'" % taskplan.LABELS["deferred"] in labels
    assert "export const NOT_NOW=%s;" % list(ops.NOT_NOW) in read("roadmap.js").replace('"', "'")


def fixture(status="deferred"):
    t2 = deferred("t-2", reason="vendor API not out", when="after:t-1", met=True, met_by="t-1 is done")
    t3 = deferred("t-3", reason=None, when=None)
    tasks = [task("t-1"), t2, t3]
    if status != "deferred":
        for t in (t2, t3):
            t["status"], t["deferral"] = status, None
    nodes = [plan_node("t-1", "ready")] + [plan_node(t["id"], "deferred" if status == "deferred" else "ready",
                                                     reasons=words(t["deferral"]) if t["deferral"] else []) for t in (t2, t3)]
    return overview(tasks, {"op-1": nodes})


def test_parked_work_is_never_focus_or_counted_open(tmp_path):
    o = fixture()
    got = node("const c=await import('./cockpit.js');out(c.cockpitTaskIds(DATA).slice(0,5))", o, tmp_path)
    assert got[0] == "t-1" and "t-2" not in got and "t-3" not in got, got


def test_nothing_changes_when_nothing_is_parked(tmp_path):
    html = node(ROADMAP + "out(renderRoadmap(DATA,{op:'op-1'}))", fixture("open"), tmp_path)
    assert "rm-not-now" not in html and "rm-dot-deferred" not in html and "not now" not in html.lower()
    parked = node(ROADMAP + "out(renderRoadmap(DATA,{op:'op-1'}))", fixture(), tmp_path)
    assert parked.count('class="rm-not-now"') == 1 and "2 not now" in parked and "rm-dot-deferred" in parked


def test_hub_hosts_the_panel_once_per_view():
    hub = read("hub.js")
    assert "notNowPanel(o,one.id)+'<p class=\"route-hint\">This operation recorded no tasks." in hub
    assert "!['checklist','roadmap'].includes(layout)?notNowPanel(o,op):''" in hub
    assert hub.count("...NOT_NOW") == 2                      # the nav count and the overview active list
    assert "try{releaseBar(h.release);}catch{releaseBar(null);}" in hub and "releaseMeter" in hub  # op-056 intact


def test_touched_web_files_are_ascii():
    for name in TOUCHED:
        assert re.search(r"[^\x00-\x7f]", read(name)) is None, name
