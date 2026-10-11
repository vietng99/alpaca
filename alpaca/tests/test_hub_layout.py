"""The hub chrome the owner arranged in op-045: the orb is the reload control and the topbar shows the
current date and time (t-485); the nav order, the topbar layout and the mission map inside Roadmap (t-486)."""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parent.parent / "web"


def _read(name):
    return (WEB / name).read_text(encoding="utf-8")


def test_the_orb_reloads_and_the_reload_button_is_gone():
    html, hub = _read("hub.html"), _read("hub.js")
    assert 'id="refresh"' not in html and "#refresh" not in hub
    assert "click to reload this view" in html
    # Click and Enter/Space reload the view; pulse.js keeps the shapeshift on the same events.
    assert "$('#pulse-orb').addEventListener('click',()=>route(true));" in hub
    assert "$('#pulse-orb').addEventListener('keydown',e=>{if(e.key==='Enter'||e.key===' ')route(true);});" in hub
    assert "addEventListener('click', goCrazy)" in _read("pulse.js")


def test_the_topbar_clock_ticks_and_keeps_the_new_activity_notice():
    hub = _read("hub.js")
    assert "setInterval(paintClock,15000)" in hub
    # the tick never paints over the notice; it only keeps the time behind it current
    assert "if(e.dataset.notice){const t=e.querySelector('.clock-time');" in hub
    assert "e.dataset.notice='1';e.innerHTML=`<span class=\"clock-notice\">New activity available</span><span class=\"clock-time\">" in hub
    # wide screens show the notice in place of the time; phone width shows the time in place of the notice
    assert "#checked-at[data-notice] .clock-time { display:none }" in _read("hub.css")
    theme = _read("theme.css")
    assert "#checked-at[data-notice] .clock-notice { display: none; }" in theme.split("@media (max-width: 700px)", 1)[1]
    assert "#checked-at[data-notice] .clock-time { display: inline; }" in theme.split("@media (max-width: 700px)", 1)[1]
    assert hub.count("newActivity();") == 2 and "toLocaleDateString(undefined,{weekday:'short'" in hub
    assert hub.isascii() and _read("hub.html").isascii()


# ------------------------------------------------------------------ t-486: nav order, topbar, mission map
def test_the_workspace_nav_order_and_no_mission_page():
    # the nav comes from the page registry (pages.json), in its nav slots
    pages = [p["id"] for p in sorted(json.loads(_read("pages.json"))["pages"], key=lambda p: p["nav"])]
    assert pages == ['overview', 'roadmap', 'cockpit', 'inflight', 'research', 'work', 'runs', 'live',
                     'activity', 'messages', 'library', 'records', 'sessions', 'vcs']
    hub = _read("hub.js")
    assert "mission:missionView" not in hub and "'vcs','mission'" not in hub and "'roadmap','mission'" not in hub
    assert "if(state.params.get('view')==='mission')return missionView(ticket,automatic);" in hub
    assert "'#mission" not in _read("mission.js") and "href('mission'" not in _read("mission.js")


def test_the_topbar_holds_find_anything_and_the_clock_and_the_sidebar_lost_the_workspace_row():
    html = _read("hub.html")
    sidebar = html[html.index('<aside class="sidebar"'):html.index('</aside>')]
    top = html[html.index('<header class="topbar"'):html.index('</header>')]
    assert 'class="workspace"' not in html and 'workspace-dot' not in html and 'id="project-name"' not in html
    assert 'id="search-launch"' not in sidebar
    # left breadcrumb (project name in its first crumb), middle Find anything and clock, right theme and orb
    order = [top.index(x) for x in ('class="breadcrumb"', 'id="crumb-project"', 'class="top-center"',
                                    'id="search-launch"', 'id="checked-at"', 'class="top-actions"',
                                    'data-theme-control', 'id="pulse-orb"')]
    assert order == sorted(order)
    assert "$('#crumb-project').textContent=o.project.name" in _read("hub.js")


ALIAS_TEST = r"""
import assert from 'node:assert/strict';
import {pathToFileURL} from 'node:url';
const {routeAlias} = await import(pathToFileURL(process.argv[1]));
assert.equal(routeAlias('#mission'), '#roadmap?view=mission');
assert.equal(routeAlias('#mission?node=parser&at=12'), '#roadmap?view=mission&node=parser&at=12');
assert.equal(routeAlias('#mission?group=core&view=x'), '#roadmap?view=mission&group=core');
for (const kept of ['', '#', '#roadmap', '#roadmap?view=mission', '#missions', '#cockpit?mission=1'])
  assert.equal(routeAlias(kept), null, kept);
console.log('ok');
"""


def test_an_old_mission_address_redirects_into_the_roadmap_mission_view():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required")
    run = subprocess.run([node, "--input-type=module", "-e", ALIAS_TEST, str(WEB / "liveflags.js")],
                         capture_output=True, text=True, timeout=60)
    assert run.returncode == 0 and run.stdout.strip() == "ok", run.stderr


# ------------------------------------------------------------------ t-488: phone width
def _css_rules(css, selector):
    return [m.group(0) for m in re.finditer(r"(?<![\w-])" + re.escape(selector) + r"\s*\{[^}]*\}", css)]


def test_the_project_name_is_never_hidden_in_the_breadcrumb():
    css = _read("hub.css")
    rules = _css_rules(css, ".crumb-project")
    assert rules, "the .crumb-project rules disappeared"
    assert not any(re.search(r"display\s*:\s*none", rule) for rule in rules), rules
    # It shortens instead: an ellipsis on the project name, and the bar keeps one line.
    assert any("text-overflow:ellipsis" in rule.replace(" ", "") for rule in _css_rules(css, ".crumb-project, .breadcrumb strong"))
    assert "white-space:nowrap" in _css_rules(css, ".breadcrumb")[0].replace(" ", "")
    # The id the hub fills with the project name is still wired.
    assert 'id="crumb-project"' in _read("hub.html")


def test_the_operation_map_heading_wraps_instead_of_widening_the_page():
    css = _read("roadmap.css")
    block = css[css.rindex("@container(max-width:700px)"):]
    assert ".rm-shell-heading strong" in block and "white-space:normal" in block
    assert ".rm-toggle-label" in block and "white-space:nowrap" in block


PHONE_PAGE = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="stylesheet" href="%(web)s/hub.css"><link rel="stylesheet" href="%(web)s/roadmap.css"><link rel="stylesheet" href="%(web)s/theme.css">
</head><body>
<div class="app-shell">%(header)s<main style="padding:24px 16px;min-width:0">
<section class="panel rm-panel rm-map"><div class="rm-shell-heading"><button type="button" data-rm-toggle aria-expanded="true">
<span class="rm-chevron" aria-hidden="true">&gt;</span><strong>Studio template: strip today's hard-coded tasks and make a new project start empty</strong>
<span class="rm-shell-count">3 in progress / 4 ready / 10 of 30 done</span><span class="rm-toggle-label">Collapse</span></button></div>
<div class="rm-body"><p>Body</p></div></section></main></div></body></html>"""

PHONE_DRIVER = """
const results = {};
const frames = [...document.querySelectorAll('iframe')];
let loaded = 0;
function measure() {
  for (const f of frames) {
    const d = f.contentDocument, w = f.contentWindow;
    d.querySelector('#crumb-project').textContent = 'Alpaca';
    const proj = d.querySelector('#crumb-project'), page = d.querySelector('#crumb'), bar = d.querySelector('.topbar');
    const range = d.createRange(); range.selectNodeContents(proj);
    const label = d.querySelector('.rm-toggle-label').getBoundingClientRect();
    results[f.dataset.width] = {
      innerWidth: w.innerWidth, scrollWidth: d.documentElement.scrollWidth,
      projectDisplay: w.getComputedStyle(proj).display, projectWidth: proj.getBoundingClientRect().width,
      textWidth: range.getBoundingClientRect().width, barHeight: bar.getBoundingClientRect().height,
      sameLine: Math.abs(proj.getBoundingClientRect().top - page.getBoundingClientRect().top) < 3 || w.getComputedStyle(page).display === 'none',
      labelRight: label.right, labelWidth: label.width,
      clockDisplay: w.getComputedStyle(d.querySelector('.top-clock')).display,
    };
    // the markup newActivity() writes while new activity waits
    const clock = d.querySelector('#checked-at');
    clock.dataset.notice = '1';
    clock.innerHTML = '<span class="clock-notice">New activity available</span><span class="clock-time">09:41</span>';
    results[f.dataset.width].noticeTime = clock.querySelector('.clock-time').getBoundingClientRect().width;
    results[f.dataset.width].noticeText = clock.querySelector('.clock-notice').getBoundingClientRect().width;
  }
  document.querySelector('#results').textContent = JSON.stringify(results);
}
for (const f of frames) f.addEventListener('load', () => { if (++loaded === frames.length) setTimeout(measure, 300); });
"""


def test_the_phone_page_does_not_scroll_sideways_and_the_project_name_stays(tmp_path):
    chrome = shutil.which("google-chrome") or shutil.which("chromium")
    if not chrome:
        pytest.skip("headless Chrome is required")
    import html as htmllib
    import json
    header = re.search(r'<header class="topbar">.*?</header>', _read("hub.html"), re.S).group(0)
    inner = PHONE_PAGE % {"web": WEB.as_uri(), "header": header}
    widths = (320, 390, 800, 1440)
    frames = "".join('<iframe data-width="%d" style="width:%dpx;height:700px;border:0;display:block" srcdoc="%s"></iframe>'
                     % (w, w, htmllib.escape(inner, quote=True)) for w in widths)
    page = tmp_path / "phone.html"
    page.write_text('<!doctype html><html><body style="margin:0">%s<pre id="results">pending</pre><script>%s</script></body></html>'
                    % (frames, PHONE_DRIVER), encoding="utf-8")
    run = subprocess.run([chrome, "--headless=new", "--no-sandbox", "--disable-gpu", "--no-first-run", "--password-store=basic",
                          "--allow-file-access-from-files", "--no-default-browser-check", "--user-data-dir=%s" % (tmp_path / "chrome"),
                          "--window-size=1500,3200", "--virtual-time-budget=6000", "--dump-dom", page.as_uri()],
                         capture_output=True, text=True, timeout=60)
    found = re.search(r'<pre id="results">(.*?)</pre>', run.stdout, re.S)
    assert found and found.group(1) != "pending", run.stderr[-800:]
    seen = json.loads(htmllib.unescape(found.group(1)))
    assert set(seen) == {str(w) for w in widths}, seen
    for width, got in seen.items():
        assert got["innerWidth"] == int(width), (width, got)
        assert got["scrollWidth"] <= got["innerWidth"], ("page scrolls sideways", width, got)
        assert got["projectDisplay"] != "none" and got["projectWidth"] > 0, ("project name hidden", width, got)
        assert got["textWidth"] <= got["projectWidth"] + 1, ("project name is clipped", width, got)
        assert got["sameLine"], ("breadcrumb wrapped", width, got)
        assert got["barHeight"] <= 64, ("topbar grew", width, got)
        assert got["labelWidth"] > 0 and got["labelRight"] <= got["innerWidth"], ("toggle label off screen", width, got)
        assert got["clockDisplay"] != "none", ("brief 2e: the phone topbar keeps the time", width, got)
        if int(width) <= 700:   # brief 2e: phone width keeps the time and drops the notice
            assert got["noticeTime"] > 0 and got["noticeText"] == 0, ("time hidden while the notice waits", width, got)
        else:                   # wider screens show the notice in the time's place (docs/studio.md 2.3)
            assert got["noticeText"] > 0 and got["noticeTime"] == 0, ("notice not shown", width, got)
