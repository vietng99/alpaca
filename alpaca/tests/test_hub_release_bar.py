"""op-056 t-614: the sidebar release bar shows the running release step, percent done and time left."""
from pathlib import Path
import shutil
import subprocess

import pytest

WEB = Path(__file__).resolve().parents[1] / 'web'


def node_check(script):
    node = shutil.which('node')
    if not node:
        pytest.skip('node unavailable')
    result = subprocess.run([node, '--input-type=module', '-e', script, str(WEB / 'liveflags.js')],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_release_meter_reads_the_running_step():
    node_check(r"""
import assert from 'node:assert/strict';
import {pathToFileURL} from 'node:url';
const {releaseMeter, timeLeft} = await import(pathToFileURL(process.argv[1]));
assert.equal(releaseMeter([]), null);
assert.equal(releaseMeter(undefined), null);
const steps = [{name:'reproducibility',status:'PASS'},{name:'leak-scan',status:'PASS'},{name:'fresh-clone',status:'PASS'},
  {name:'dev-pytest',status:'running'},{name:'clean-pytest',status:'running'},{name:'documentation',status:'pending'}];
const m = releaseMeter([{kind:'check',state:'running',percent:41.6,seconds_left:2100,steps}]);
assert.equal(m.name,'Release check'); assert.equal(m.pct,'42%'); assert.equal(m.width,'42%'); assert.equal(m.value,42);
assert.equal(m.step,'dev-pytest, clean-pytest'); assert.equal(m.eta,'3 of 6 steps, about 35 min left'); assert.equal(m.band,'ok');
assert.equal(m.valuetext,'Release check: 42% done, running dev-pytest, clean-pytest, 3 of 6 steps, about 35 min left');
const failed = releaseMeter([{kind:'preflight',state:'running',percent:80,seconds_left:30,
  steps:[{name:'build',status:'PASS'},{name:'lint',status:'FAIL'},{name:'suite',status:'STOPPED'},{name:'docs',status:'SKIPPED'},{name:'x',status:'running'}]}]);
assert.equal(failed.band,'fail'); assert.equal(failed.step,'x'); assert.equal(failed.eta,'4 of 5 steps, 1 FAIL, 1 STOPPED, 1 SKIPPED, under a minute left');
const fresh = releaseMeter([{kind:'check',state:'running',percent:25,seconds_left:null,steps:[{name:'a',status:'running'}]}]);
assert.equal(fresh.eta,'0 of 1 steps, no time estimate yet: no earlier timed run');
const ended = releaseMeter([{kind:'check',state:'ended',percent:null,seconds_left:null,steps:[{name:'a',status:'running'}]}]);
assert.equal(ended.band,'ended'); assert.equal(ended.step,'Ended without a result'); assert.equal(ended.pct,'');
// a running step wins over one that ended
assert.equal(releaseMeter([{kind:'check',state:'ended',steps:[]},{kind:'build',state:'running',percent:0,steps:[{name:'build',status:'running'}]}]).name,'Release build');
assert.equal(timeLeft(5400),'about 1 h 30 min left'); assert.equal(timeLeft(89*60),'about 89 min left');
""")


def test_release_bar_markup_is_hidden_until_a_release_runs_and_has_no_tooltip():
    html = (WEB / 'hub.html').read_text()
    start = html.index('id="release-meter"')
    tag = html[html.rindex('<', 0, start):html.index('>', start) + 1]
    assert ' hidden' in tag and 'role="progressbar"' in tag and 'title=' not in tag
    assert 'title=' not in html[start:html.index('</div>', start)]
    hub_js = (WEB / 'hub.js').read_text()
    # the bar is drawn in its own try, after the vitals arrive, and hidden when the poll fails
    assert 'try{releaseBar(h.release);}catch{releaseBar(null);}' in hub_js and 'catch(error){releaseBar(null);' in hub_js
    assert 'e.hidden=!m' in hub_js
