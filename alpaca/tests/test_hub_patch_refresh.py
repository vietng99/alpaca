"""A live refresh of the Cockpit, the Roadmap and the Mission map patches the page in place (t-579).

The real hub in headless Chrome against a tmp project. A control route bumps the board revision (a push
that changes no record) or renames a task. Every refresh here is an automatic one (the pushed revision,
applied once the reader is idle), so the page must not be mounted again:
  - a refresh over unchanged data writes nothing to #main (a MutationObserver counts zero records) and
    the Cockpit does not read its extra panels again;
  - a renamed task updates its text in place: the cards, the orbs and the map root stay the same nodes;
  - what the reader set (an open card, a closed group, a selected component) survives the patch, and the
    handlers bound at mount still work after it.
"""
import http.server
import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from alpaca import db, serve
from alpaca.tests.test_hub_live_refresh import CHROME_FLAGS, HUB_DRIVER
from alpaca.tests.test_mission_web import fixture

DRIVER_TAIL = r"""
const count = pat => js(`performance.getEntriesByType('resource').filter(e=>e.name.includes(${JSON.stringify(pat)})).length`);
// Record every change under #main (the host panel repaints itself every few seconds and is left out), and
// remember the nodes whose identity a patch must keep.
const watch = marks => js(`(()=>{const main=document.getElementById('main');window.__mut=0;window.__mounts=0;window.__kept=${JSON.stringify(marks)}.map(q=>document.querySelector(q));
  window.__obs?.disconnect();window.__obs=new MutationObserver(records=>{for(const r of records){if(r.target.closest&&r.target.closest('#sys-panel'))continue;window.__mut++;if(r.target===main&&r.addedNodes.length)window.__mounts++;}});
  window.__obs.observe(main,{childList:true,attributes:true,characterData:true,subtree:true});return window.__kept.every(Boolean);})()`);
const kept = marks => js(`${JSON.stringify(marks)}.every((q,i)=>document.querySelector(q)===window.__kept[i])`);
// A push, then wait for the page to have read the record again (the refresh runs 1.5 s after the push).
async function pushAndWait(act) {
  const before = await count('/hub/overview.json');
  await ctl(act);
  const ok = await until(`performance.getEntriesByType('resource').filter(e=>e.name.includes('/hub/overview.json')).length>${before}`, 15000);
  await sleep(1200);
  return ok;
}
const settled = {
  cockpit: `!!document.querySelector('.cockpit-heading')&&!document.querySelector('[data-cockpit-extra] [role=status]')&&!!document.querySelector('[data-cockpit-extra=mission] .mm-overview-area')`,
  roadmap: `!!document.querySelector('.rm-panel [data-rm-node]')`,
  mission: `!!document.querySelector('[data-mm-root] [data-mm-node]')&&document.querySelector('[data-mm-detail]').children.length>0`,
};
let step = 0;
const rename = async title => { await ctl('act=rename&title=' + encodeURIComponent(title)); };
try {
  await send('Runtime.enable');

  // ---- Cockpit
  await load('#cockpit', settled.cockpit);
  await sleep(1500);
  check('cockpit: setup', await watch(['#main alpaca-orb', '[data-cockpit-extra=mission] .mm-overview-area', '.focus-card']));
  const reads = {mission: await count('/hub/mission.json'), research: await count('/hub/research.json'), board: await count('/hub/live-work.json')};
  check('cockpit: the push was applied', await pushAndWait('act=bump'));
  check('cockpit: an unchanged refresh writes nothing', await js('window.__mut') === 0, await js('window.__mut'));
  check('cockpit: the extra panels were not read again', await count('/hub/mission.json') === reads.mission && await count('/hub/research.json') === reads.research && await count('/hub/live-work.json') === reads.board);
  await pushAndWait('act=rename&title=Renamed%20in%20place');
  check('cockpit: renamed', await until(`document.getElementById('main').textContent.includes('Renamed in place')`), await js(`document.querySelector('.focus-card').textContent.slice(0,200)`));
  check('cockpit: a changed task is patched, not mounted', await js('window.__mounts') === 0 && await kept(['#main alpaca-orb', '[data-cockpit-extra=mission] .mm-overview-area', '.focus-card']));
  check('cockpit: the extra panels read their own data after a change', await count('/hub/mission.json') > reads.mission);

  // ---- Roadmap (task view)
  await load('#roadmap', settled.roadmap);
  await sleep(800);
  await js(`document.querySelector('[data-rm-details="t-2"]').click();document.querySelector('[data-rm-group] summary').click();true`);
  await sleep(300);
  const openDetails = `document.querySelector('[data-rm-details="t-2"]').getAttribute('aria-expanded')==='true'`;
  await js(`window.__group=document.querySelector('[data-rm-group]');true`);
  const groupClosed = `document.querySelector('[data-rm-group]')===window.__group&&!window.__group.open`;
  check('roadmap: setup', await watch(['[data-rm-node="t-2"]', '.rm-panel', '#main alpaca-orb']) && await js(`${openDetails}&&${groupClosed}`));
  const paths = await js(`document.querySelectorAll('.rm-connectors path').length`);
  check('roadmap: the push was applied', await pushAndWait('act=bump'));
  check('roadmap: an unchanged refresh writes nothing', await js('window.__mut') === 0, await js('window.__mut'));
  await pushAndWait('act=rename&title=Roadmap%20renamed');
  check('roadmap: renamed', await until(`document.querySelector('[data-rm-node="t-1"]')?.textContent.includes('Roadmap renamed')||document.querySelector('[data-rm-node="t-2"]')?.textContent.includes('Roadmap renamed')||document.querySelector('.rm-panel').textContent.includes('Roadmap renamed')`));
  check('roadmap: patched, not mounted', await js('window.__mounts') === 0 && await kept(['[data-rm-node="t-2"]', '.rm-panel', '#main alpaca-orb']));
  check('roadmap: an open card and a closed group survive the patch', await js(`${openDetails}&&${groupClosed}`));
  check('roadmap: arrows are still drawn', await js(`document.querySelectorAll('.rm-connectors path').length`) === paths, paths);
  await js(`document.querySelector('[data-rm-details="t-2"]').click();true`);
  check('roadmap: handlers still work after a patch', await js(`document.querySelector('[data-rm-details="t-2"]').getAttribute('aria-expanded')==='false'`));
  await js(`document.querySelector('[data-rm-focus="t-2"]').click();true`);
  check('roadmap: tracing a card still works', await js(`document.querySelector('[data-rm-node="t-2"]').classList.contains('rm-selected')`));
  await pushAndWait('act=bump');
  check('roadmap: a traced card stays traced across an unchanged refresh', await js(`document.querySelector('[data-rm-node="t-2"]').classList.contains('rm-selected')`));

  // ---- Mission map
  await load('#roadmap?view=mission', settled.mission);
  await sleep(800);
  const select = `document.querySelector('[data-mm-node] [data-mm-select="usage"]')`;
  await js(`${select}.click();true`);
  await sleep(300);
  check('mission: setup', await watch(['[data-mm-root]', '[data-mm-node]', '[data-mm-canvas]']) && await js(`${select}.getAttribute('aria-pressed')==='true'`));
  const links = await js(`document.querySelectorAll('[data-mm-links] path').length`);
  check('mission: the push was applied', await pushAndWait('act=bump'));
  check('mission: an unchanged refresh writes nothing', await js('window.__mut') === 0, await js('window.__mut'));
  await pushAndWait('act=rename&title=Mission%20renamed');
  check('mission: renamed', await until(`document.querySelector('[data-mm-root]').textContent.includes('Mission renamed')`));
  check('mission: patched, not mounted', await js('window.__mounts') === 0 && await kept(['[data-mm-root]', '[data-mm-node]', '[data-mm-canvas]']));
  check('mission: the selection and its detail survive the patch', await js(`${select}.getAttribute('aria-pressed')==='true'&&document.querySelector('[data-mm-root]').classList.contains('mm-has-selection')&&document.querySelector('[data-mm-detail]').children.length>0`));
  check('mission: links are still drawn', await js(`document.querySelectorAll('[data-mm-links] path').length`) === links, links);
  await js(`document.querySelector('[data-mm-mode="impact"]').click();true`);
  check('mission: handlers still work after a patch', await js(`document.querySelector('[data-mm-root]').dataset.mode==='impact'`));

  // A check recorded while the evidence box is open adds its row; the box stays the one box, with its text.
  await pushAndWait('act=mdef2');
  await js(`document.querySelector('[data-mm-node] [data-mm-select="view"]').click();true`);
  await sleep(300);
  await pushAndWait('act=mcheck&check=behavior');
  await js(`document.querySelector('[data-mm-detail] [data-mm-evidence]').click();true`);
  check('mission: evidence loads', await until(`document.querySelector('[data-mm-log]').textContent.length>0&&!document.querySelector('[data-mm-log]').textContent.startsWith('Loading')`, 8000));
  const evidence = await js(`document.querySelector('[data-mm-log]').textContent`);
  await pushAndWait('act=mcheck&check=extra');
  check('mission: a new check row appears beside the open evidence', await js(`document.querySelectorAll('[data-mm-detail] .mm-check').length`) === 2, await js(`document.querySelectorAll('[data-mm-detail] .mm-check').length`));
  check('mission: one evidence box, text kept', await js(`document.querySelectorAll('[data-mm-detail] [data-mm-log]').length===1&&document.querySelector('[data-mm-log]').textContent===${JSON.stringify(evidence)}`));
} catch (e) {
  out.push('FAIL exception ' + e.message);
  try { out.push('page: ' + (await js(`document.getElementById('main').innerText.slice(0,400)`))); } catch {}
}
console.log(out.join('\n'));
ws.close(); chrome.kill();
process.exit(out.some(line => line.startsWith('FAIL')) ? 1 : 0);
"""


def test_live_refresh_patches_cockpit_roadmap_and_mission_in_place(project, tmp_path):
    chrome, node = shutil.which("google-chrome"), shutil.which("node")
    if not chrome or not node:
        pytest.skip("headless Chrome and node are required")
    fixture(Path(project))
    live = serve.Live(project)
    live.refresh()
    base = serve.make_handler(live, project)
    control = {"sse": 0}

    def push():
        with live.cond:
            live.gen += 1
            live.cond.notify_all()

    class Handler(base):
        def _sse(self):
            control["sse"] += 1
            return super()._sse()

        def do_GET(self):
            url = urlsplit(self.path)
            if url.path == "/__ctl":
                from urllib.parse import parse_qs
                q = {k: v[0] for k, v in parse_qs(url.query).items()}
                if q["act"] == "bump":
                    with live.lock:
                        board = json.loads(live.board_json)
                        board["fixture_bump"] = time.time()
                        live.board_json = json.dumps(board)
                    push()
                elif q["act"] == "mdef2":
                    from alpaca import mission
                    from alpaca.tests.test_mission import definition
                    data = definition()
                    for n in data["nodes"]:
                        n["checks"] = ["behavior", "extra"]
                    mission.define(project, data, session="owner")
                    live.refresh()
                    push()
                elif q["act"] == "mcheck":
                    from alpaca import mission
                    mission.run_check(project, "view", q["check"], ["false"], session="owner")
                    live.refresh()
                    push()
                elif q["act"] == "rename":
                    conn = db.connect(project)
                    task = db.rows(conn, "tasks", "id=?", ("t-4",))[0]
                    db.upsert(conn, "tasks", "id", {**task, "title": q["title"]})
                    db.append_event(conn, session="planner", actor="planner", kind="task-edit", ref="t-4", data={})
                    conn.close()
                    live.refresh()
                return self._json(control)
            return super().do_GET()

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    # HUB_DRIVER's header (browser, load(), js(), until(), check()) up to its checks; load() waits on the control
    # route's act=state, which answers with the sse count.
    head = HUB_DRIVER.split("const rows = ")[0]
    driver = tmp_path / "patch_driver.mjs"
    driver.write_text(head + DRIVER_TAIL, encoding="utf-8")
    try:
        run = subprocess.run([node, str(driver), "http://127.0.0.1:%d" % httpd.server_address[1], str(tmp_path / "chrome")],
                             capture_output=True, text=True, timeout=300, env=dict(os.environ, CHROME=chrome, CHROME_FLAGS=json.dumps(CHROME_FLAGS)))
    finally:
        httpd.shutdown()
        httpd.server_close()
    report = run.stdout + run.stderr
    assert run.returncode == 0 and "FAIL" not in report, report
    assert report.count("PASS") == DRIVER_TAIL.count("check("), report
