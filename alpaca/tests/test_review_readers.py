"""The readers put the summary first: the run log review panel and the proof reader, run in node."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parents[1] / "web"

REVIEW = {"id": "a" * 16, "reviewer": "worker", "submitted_at": "2026-09-29T12:00:00+07:00", "intact": True,
          "outcome": "matches-verdict", "log_sha256": "b" * 64,
          "summary": "The run finished at line 2 and every one of its 12 checks passed.",
          "quick_summary": "Passed: all 12 checks passed <img src=x>.", "next_check": "Nothing before advancing.",
          "primary_finding": "f1", "findings": [{"id": "f1", "line": 2, "end_line": 3, "severity": "info",
                                                "quote": "12 checks passed", "meaning": "All checks passed.",
                                                "mark": None}]}

SCRIPT = r"""
const out = {};
const box = {innerHTML: ''};
globalThis.document = {getElementById: id => id === 'rl-review' ? box : null};
const {renderReview, runDashboard} = await import(WEB + '/runlog.js');
const {proofOverview, documentNavigation} = await import(WEB + '/document.js');
const show = data => {renderReview(data, 'r1'); return box.innerHTML;};
out.reviewed = show({reviews: [REVIEW], workflow: {result: 'PASS', review_state: 'reviewed', reason: '',
  decision: {value: 'GO', rationale: 'Checks passed as the log shows', review: REVIEW.id},
  next_action: 'Advance after the runner\'s own gate checks'}});
out.pending = show({reviews: [], workflow: {result: 'FAIL', review_state: 'pending', reason: 'step exited 2',
  decision: null, next_action: 'Main session: assign an independent reviewer'}});
out.stale = show({reviews: [REVIEW], workflow: {result: 'PASS', review_state: 'stale',
  reason: 'The kept review or the captured log changed', decision: null, next_action: 'Main session: assign a fresh review'}});
out.plain = show({reviews: []});
out.dashboard = runDashboard({id: 'run-1', stages: [], verdict: 'PASS', stage: 'build'}, null, s => s);
const v2 = '# Proof report t-001\n\n## At a glance\n\nThe change landed and all 40 checks passed; nothing is open.\n\n## Report context\n\n- proof-format: 2\n\n## Result\n\nAll 40 checks passed.\n';
out.v2 = proofOverview(v2);
out.v1 = proofOverview('# Proof report t-002\n\n- id: t-002\n\n## What I did\n\nText.\n\n```\n## At a glance\n```\n\n## Result\n\nThe old result.\n');
out.todo = proofOverview('# Proof report t-003\n\n## At a glance\n\nTODO(agent): write it\n');
out.other = proofOverview('# Notes\n\n## Result\n\nNot a proof.\n');
out.nav = documentNavigation([{id: 'doc-section-1', title: 'At a glance'}, {id: 'doc-section-2', title: '<b>Result</b>'}]);
out.nav_one = documentNavigation([{id: 'doc-section-1', title: 'Only'}]);
console.log(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def shown():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    script = "const WEB = %s;\nconst REVIEW = %s;\n%s" % (json.dumps(str(WEB)), json.dumps(REVIEW), SCRIPT)
    done = subprocess.run([node, "--input-type=module", "-e", script], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def test_the_review_panel_opens_with_at_a_glance_and_folds_the_full_review(shown):
    html = shown["reviewed"]
    assert html.index("At a glance") < html.index("AGENT REVIEW") < html.index("12 checks passed</code>")
    assert "Passed: all 12 checks passed &lt;img src=x&gt;." in html and "<img" not in html
    assert "<b>GO</b> Checks passed as the log shows" in html and "Nothing before advancing." in html
    assert 'data-line="2">Evidence: line 2&ndash;3</button>' in html
    assert '<details class="rl-full"><summary>Full review and evidence (1 finding)</summary>' in html


def test_pending_and_stale_states_say_so_without_inventing_a_diagnosis(shown):
    pending = shown["pending"]
    assert "Review pending" in pending and "step exited 2" in pending and "Awaiting decision" in pending
    assert "No agent review of this log yet" in pending and "Evidence:" not in pending
    stale = shown["stale"]
    assert "Review stale" in stale and "The kept review or the captured log changed" in stale
    assert "Passed: all 12" not in stale.split("AGENT REVIEW")[0] and "Evidence: line" not in stale
    assert "Next check" not in stale.split("AGENT REVIEW")[0]


def test_a_profile_without_the_workflow_keeps_the_old_panel(shown):
    assert "At a glance" not in shown["plain"] and "No agent review of this log yet" in shown["plain"]
    board = shown["dashboard"]
    assert board.index('id="rl-review"') < board.index('id="rl-contract"') < board.index('id="rl-kpis"')


def test_the_proof_reader_shows_the_summary_and_section_buttons(shown):
    assert "At a glance</span><p>The change landed and all 40 checks passed; nothing is open.</p>" in shown["v2"]
    assert "Result</span><p>The old result.</p>" in shown["v1"]
    assert shown["todo"] == "" and shown["other"] == ""
    assert 'data-document-section="doc-section-2">&lt;b&gt;Result&lt;/b&gt;</button>' in shown["nav"]
    assert shown["nav_one"] == ""


def test_the_hub_serves_the_document_module():
    serve = (WEB.parent / "serve.py").read_text()
    hub = (WEB / "hub.js").read_text()
    assert '"document.js": "text/javascript; charset=utf-8"' in serve
    assert "from './document.js'" in hub and "data-document-section" in hub
