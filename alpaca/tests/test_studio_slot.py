"""t-477: the project slot. A profile adds its own top-level pages, cards and stylesheets through
`web()` (alpaca/profile.py web_meta, docs/studio.md 4.0), with no edit inside alpaca/web; Runs & logs
and Live logs show only when the profile serves their routes.

The sample profile (alpaca/tests/studio/sample_profile, added by studio_fixture.add_sample_profile) has
one page, Bench, that reads its own route. It lives in a project of its own here, never in the standard
baseline fixture, so the t-474 baseline and the t-475 checks keep the default Studio."""
import http.server
import json
import os
import re
import shutil
import subprocess
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from alpaca import db, manifest, profile, serve, upgrade, util
from alpaca.clock import FixedClock
from alpaca.tests import studio_fixture
from alpaca.tests.test_hub_live_refresh import CHROME_FLAGS, HUB_DRIVER
from alpaca.tests.test_studio_registry import LINKS_BEFORE

HERE = Path(__file__).resolve().parent
WEB = HERE.parent / "web"
SAMPLE_STYLES = ["/hub/assets/profile/bench.css", "/hub/assets/profile/look.css"]


def _project(path):
    """A bare project root, as the conftest `project` fixture makes one."""
    path.mkdir(parents=True)
    shutil.copy(HERE.parents[1] / "ALPACA-MANIFEST", path / "ALPACA-MANIFEST")
    (path / "style").mkdir()
    (path / "style" / "banned.txt").write_text("", encoding="utf-8")
    (path / ".alpaca" / "config").mkdir(parents=True)
    return str(path)


def _serve(root):
    db.connect(root).close()
    live = serve.Live(root)
    live.refresh(fold=False)
    live.data_json = json.dumps({"sessions": []})
    live.data_ready.set()
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), serve.make_handler(live, root))
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, "http://127.0.0.1:%d" % httpd.server_address[1]


def _get(url):
    try:
        r = urllib.request.urlopen(url, timeout=15)
    except urllib.error.HTTPError as exc:
        r = exc
    return r.status, r.headers, r.read()


# ------------------------------------------------------------------ the hook
def test_the_sample_page_passes_through_web_meta(project):
    studio_fixture.add_sample_profile(project)
    meta = profile.web_meta(project)
    assert profile.error(project) is None and "hook_errors" not in meta
    assert meta["name"] == "bench" and meta["pages"] == []
    assert meta["slot_pages"] == [{"id": "bench", "label": "Bench", "icon": "research", "nav": 5.5,
                                   "module": "/hub/assets/profile/bench.js", "needs": []}]
    assert meta["styles"] == SAMPLE_STYLES


ODD_PROFILE = '''
import os
from alpaca.profile import Profile
HERE = os.path.dirname(os.path.abspath(__file__))


class Odd(Profile):
    name = "odd"

    def routes(self):
        return {"/live/log": lambda root, query: ({}, 200)}

    def web(self):
        page = lambda pid, **kw: dict({"id": pid, "module": "p.js"}, **kw)
        return {"pages": ["runs", "live", "cockpit", page("cockpit"), page("lib"), page("Bad Id"),
                          page("orphan", module="missing.js"), page("mine", label="Mine", css="p.css",
                                                                    needs=["runs"], nav=True),
                          page("mine"), page("second", nav=float("nan"))],
                "cards": [{"id": "c", "module": "p.js", "css": ["c.css", "nowhere.css"]}],
                "css": "look.css",
                "assets": {n: os.path.join(HERE, n) for n in ("p.js", "p.css", "c.css", "look.css")}}


PROFILE = Odd
'''


def test_a_colliding_or_unserved_page_is_left_out_and_noted(project):
    pkg = Path(project) / "oddslot"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "profile.py").write_text(ODD_PROFILE, encoding="utf-8")
    for name in ("p.js", "p.css", "c.css", "look.css"):
        (pkg / name).write_text("/* %s */\n" % name, encoding="utf-8")
    Path(project, "project.yaml").write_text("name: odd\nprofile: oddslot.profile\n", encoding="utf-8")
    meta = profile.web_meta(project)
    # live is on (its route is served), runs is not (no /hub/log.json); a built-in id never turns on
    assert meta["pages"] == ["live"]
    assert [(p["id"], p["nav"]) for p in meta["slot_pages"]] == [("mine", 100), ("second", 101)]
    assert meta["slot_pages"][0]["label"] == "Mine" and meta["slot_pages"][0]["needs"] == ["runs"]
    assert meta["styles"] == ["/hub/assets/profile/c.css", "/hub/assets/profile/p.css",
                              "/hub/assets/profile/look.css"]
    why = meta["hook_errors"]["web"]
    for part in ("runs (the profile serves no /hub/log.json)", "cockpit (the id is taken)", "lib (the id is taken)",
                 "'Bad Id'", "orphan (its module is not one of the assets)", "mine (the id is taken)"):
        assert part in why, (part, why)
    json.dumps(meta, allow_nan=False)


def test_a_route_collision_note_survives_web_meta(project):
    """web_meta reads routes() past the guard, so the note serve.py keeps against `routes` stays."""
    studio_fixture.add_sample_profile(project)
    profile.load(project)
    profile.note(project, "routes", "ignored route(s) that collide with the server's own: /events")
    meta = profile.web_meta(project)
    assert "collide" in meta["hook_errors"]["routes"]


# ------------------------------------------------------------------ the server
def test_the_hub_page_links_the_project_stylesheets_after_theme_css(project, tmp_path):
    plain = _project(tmp_path / "plain")
    assert serve.hub_page(plain) == serve.hub_page()           # no profile: the page is unchanged
    studio_fixture.add_sample_profile(project)
    page = serve.hub_page(project)
    links = re.findall(r'<link rel="stylesheet" href="([^"]+)">', page)
    assert links == ["/hub/assets/" + name for name in LINKS_BEFORE] + SAMPLE_STYLES
    httpd, base = _serve(project)
    try:
        status, headers, body = _get(base + "/hub/")
        assert status == 200 and re.findall(r'<link rel="stylesheet" href="([^"]+)">', body.decode("ascii"))[-2:] == SAMPLE_STYLES
        for url, kind in (("/hub/assets/profile/bench.js", "text/javascript"), ("/hub/assets/profile/bench.css", "text/css"),
                          ("/hub/assets/profile/look.css", "text/css"), ("/hub/bench.json", "application/json")):
            status, headers, _ = _get(base + url)
            assert status == 200 and headers["Content-Type"].startswith(kind), url
        overview = json.loads(_get(base + "/hub/overview.json")[2])
        assert [p["id"] for p in overview["profile"]["slot_pages"]] == ["bench"]
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_a_project_page_needs_no_edit_inside_alpaca_web():
    """The fail case: the sample page lives wholly in its own package; nothing under alpaca/web names it."""
    for path in WEB.rglob("*"):
        if path.is_file() and path.suffix in (".js", ".json", ".html", ".css"):
            text = path.read_text(encoding="utf-8", errors="replace")
            assert "studio_sample" not in text and "bench.js" not in text and "/hub/bench.json" not in text, path
    sample = Path(studio_fixture.SAMPLE_PROFILE)
    assert sorted(p.name for p in sample.iterdir() if p.name != "__pycache__") == [
        "__init__.py", "bench.css", "bench.js", "look.css", "profile.py"]
    # the module touches nothing of the hub but ctx
    assert "import" not in (sample / "bench.js").read_text(encoding="utf-8")


# ------------------------------------------------------------------ alpaca upgrade
def test_upgrade_keeps_the_profile_package_and_its_page(tmp_path):
    """The profile package sits outside every mechanism path and project.yaml is preserved, so an
    upgrade refreshes the harness and the page stays in the nav."""
    from alpaca.tests.test_upgrade import _make_tree
    live, source = str(tmp_path / "live"), str(tmp_path / "source")
    _make_tree(live, "old", memory=True)
    _make_tree(source, "new", memory=False)
    dotted = studio_fixture.add_sample_profile(live)
    pkg = os.path.join(live, studio_fixture.SAMPLE_PACKAGE)
    before = {name: util.sha256_hex(open(os.path.join(pkg, name), "rb").read()) for name in sorted(os.listdir(pkg))}
    mechanism = [upgrade._rel_norm(r) for r in manifest.classes(live)["mechanism"]]
    assert not upgrade._under(studio_fixture.SAMPLE_PACKAGE, mechanism)
    p = upgrade.plan(live, source)
    touched = [upgrade._rel_norm(r) for r in p.refresh + p.merge + p.remove + p.dropped + p.dropped_kept]
    assert not any(upgrade._under(studio_fixture.SAMPLE_PACKAGE, [r]) or upgrade._under(r, [studio_fixture.SAMPLE_PACKAGE])
                   for r in touched), touched
    assert upgrade.apply(live, source, clock=FixedClock(step=1))["ok"]
    assert open(os.path.join(source, "alpaca", "stub.txt")).read() == open(os.path.join(live, "alpaca", "stub.txt")).read()
    after = {name: util.sha256_hex(open(os.path.join(pkg, name), "rb").read()) for name in sorted(os.listdir(pkg))
             if name != "__pycache__"}
    assert after == before
    assert ("profile: %s" % dotted) in open(os.path.join(live, "project.yaml"), encoding="utf-8").read()
    assert [p["id"] for p in profile.web_meta(live)["slot_pages"]] == ["bench"]


# ------------------------------------------------------------------ in the browser
DRIVER = HUB_DRIVER.split('let loads = 0;')[0] + r"""
// every console error, exception and failed load counts, except the browser's own favicon request
const problems=[];
ws.addEventListener('message',m=>{const d=JSON.parse(m.data);if(d.method==='Runtime.exceptionThrown')problems.push('exception: '+JSON.stringify(d.params.exceptionDetails).slice(0,300));if(d.method==='Runtime.consoleAPICalled'&&d.params.type==='error')problems.push('console.error: '+JSON.stringify(d.params.args).slice(0,300));if(d.method==='Log.entryAdded'&&d.params.entry.level==='error'&&!/\/favicon\.ico$/.test(d.params.entry.url||''))problems.push('log: '+d.params.entry.text+' '+(d.params.entry.url||''));});
const [slotBase, plainBase] = [base, process.argv[4]];
const nav = `[...document.querySelectorAll('#navigation a.nav-link')].map(a=>a.getAttribute('href')+'='+a.textContent.trim()).join('|')`;
try {
  await send('Runtime.enable'); await send('Log.enable'); await send('Page.enable');
  await send('Page.navigate', {url: slotBase + '/hub/#bench'});
  check('the sample page renders through render(ctx)', await until(`!!document.querySelector('#main .bench-page li')`, 30000));
  check('it reads its own profile route', await js(`document.querySelector('.bench-list').textContent.includes('Bench A: 21.5 C')`));
  check('ctx is version 1', await js(`document.querySelector('.bench-page').dataset.ctxVersion==='1'`));
  const links = await js(nav);
  check('the nav lists the page by its nav number', /#research=Research\|#bench=Bench\|#work=/.test(links), links);
  check('the nav marks it current', await js(`document.querySelector('#navigation a[href="#bench"]').getAttribute('aria-current')==='page'`));
  check('the title names it', await js(`document.title.startsWith('Bench |')`));
  const sheets = await js(`[...document.querySelectorAll('link[rel=stylesheet]')].map(l=>l.getAttribute('href')).slice(-3).join(',')`);
  check('the stylesheet hook links after theme.css', sheets === '/hub/assets/theme.css,/hub/assets/profile/bench.css,/hub/assets/profile/look.css', sheets);
  check('the project stylesheet applies', await js(`getComputedStyle(document.documentElement).getPropertyValue('--slot-sample').trim()==='1'`));
  check('the page stylesheet applies', await js(`getComputedStyle(document.querySelector('.bench-list')).letterSpacing==='0.5px'`));
  await js(`document.querySelector('[data-bench-ping]').click();true`);
  check('bind(ctx) wires the page and ctx.patch updates it', await until(`document.getElementById('bench-out').textContent==='Bench pinged'`, 5000));
  await js(`location.hash='#cockpit';true`);
  check('leaving the page calls stop()', await until(`globalThis.benchStops===1&&!!document.querySelector('.cockpit-heading')`, 20000));
  await js(`location.hash='#bench';true`);
  check('coming back renders it again', await until(`!!document.querySelector('#main .bench-page li')&&!document.querySelector('#main .error-box')`, 20000));
  await send('Page.navigate', {url: plainBase + '/hub/#runs'});
  check('a project with no profile lands on the cockpit for #runs', await until(`location.port===${JSON.stringify(new URL(plainBase).port)}&&!!document.querySelector('.cockpit-heading')`, 30000));
  const plain = await js(nav);
  check('its nav has no Runs or Live page and no project page', plain.includes('#cockpit=') && !/#runs|#live|#bench/.test(plain), plain);
  check('no broken page is shown', await js(`!document.querySelector('#main .error-box')`));
  await js(`location.hash='#live';true`); await sleep(1200);
  check('#live also lands on the cockpit', await js(`!!document.querySelector('.cockpit-heading')&&!document.querySelector('#main .error-box')&&document.title.startsWith('Cockpit')`));
  await sleep(500);
  check('no console error or exception', problems.length === 0, problems.join(' / '));
} catch (e) { out.push('FAIL exception ' + e.message); }
console.log(out.join('\n'));
ws.close(); chrome.kill('SIGTERM');
if (out.some(row => row.startsWith('FAIL'))) process.exitCode = 1;
"""


def test_the_sample_page_in_the_browser(project, tmp_path):
    chrome, node = shutil.which("google-chrome"), shutil.which("node")
    if not chrome or not node:
        pytest.skip("google-chrome and node are required")
    studio_fixture.add_sample_profile(project)
    plain = _project(tmp_path / "plain")
    servers = [_serve(project), _serve(plain)]
    driver = tmp_path / "slot.mjs"
    driver.write_text(DRIVER, encoding="utf-8")
    try:
        run = subprocess.run([node, str(driver), servers[0][1], str(tmp_path / "chrome"), servers[1][1]],
                             capture_output=True, text=True, timeout=180,
                             env=dict(os.environ, CHROME=chrome, CHROME_FLAGS=json.dumps(CHROME_FLAGS)))
    finally:
        for httpd, _ in servers:
            httpd.shutdown()
            httpd.server_close()
    print(run.stdout)
    assert run.returncode == 0 and "FAIL" not in run.stdout + run.stderr, run.stdout + run.stderr
    assert run.stdout.count("PASS") == 17, run.stdout
