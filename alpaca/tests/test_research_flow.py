"""Research workflow browsing and truthful candidate decisions (t-324)."""
import re

from alpaca.research import events, view
from alpaca.tests.test_research_view import (
    RUN, OP, S, _conn, _round, improve_run, render,
)


def test_candidates_distinguish_election_champion_rejection_and_baseline(project, tmp_path):
    conn = _conn(project)
    ids = improve_run(conn)
    events.emit(conn, events.ACCEPT, run=RUN, ref=ids['a3'], op=OP, session=S,
                data={'accepted': False, 'reason': 'below the required improvement'})
    conn.close()
    data = view.run(project, RUN)
    pages = render(tmp_path, {
        name: {'data': data, 'params': {'candidate': task}}
        for name, task in ids.items()
    })
    for name, expected in [('a0', 'Baseline'), ('a1', 'Elected'),
                           ('a3', 'Rejected'), ('a5', 'Champion'), ('a6', 'Evaluating')]:
        reader = re.search(r'<section[^>]+data-candidate-reader=.*?</section>', pages[name], re.S)
        assert reader and expected in reader.group(0), name
        assert 'Open task' in reader.group(0)
    assert 'Open proof' in pages['a1']
    assert 'Candidate decisions' in pages['a0']
    assert 'data-round-flow="1"' in pages['a0']
    assert 'candidate=' in pages['a0']


def test_refuted_acceptance_and_unmerged_election_do_not_become_champion(project, tmp_path):
    conn = _conn(project)
    ids = improve_run(conn)
    conn.close()
    data = view.run(project, RUN)
    a = next(a for a in data['run']['attempts'] if a['task'] == ids['a3'])
    a.update(accepted=True, outcome='broken', verdict='broken', reason='proof-refuted',
             verify={'state': 'refuted', 'word': 'refuted'})
    data['run']['rounds'].append({'round': 4, 'winner': ids['a6'], 'event': 9999,
                                'ranking': [], 'reason': 'selected', 'merge': None})
    pages = render(tmp_path, {
        'refuted': {'data': data, 'params': {'candidate': ids['a3']}},
        'elected': {'data': data, 'params': {'candidate': ids['a6']}},
    })
    reader = lambda page: re.search(r'<section[^>]+data-candidate-reader=.*?</section>', page, re.S).group(0)
    assert 'Rejected' in reader(pages['refuted']) and 'proof-refuted' in reader(pages['refuted'])
    assert 'Accepted by rule' in reader(pages['refuted'])
    assert 'Elected' in reader(pages['elected']) and 'Champion' not in reader(pages['elected'])
    assert 'Merge not recorded' in reader(pages['elected'])


def test_progress_uses_distinct_round_decisions_not_current_round(project, tmp_path):
    conn = _conn(project)
    improve_run(conn)
    _round(conn, 3, value=12)
    conn.close()
    data = view.run(project, RUN)
    data['run']['round'] = 4
    page = render(tmp_path, {'p': {'data': data}})['p']
    assert '3 of 4 search rounds judged' in page
    assert '4 of 4 search rounds judged' not in page
    assert page.count('data-round-flow="3"') == 1
    data['run']['rounds_n'] = None
    page = render(tmp_path, {'p': {'data': data}})['p']
    assert '3 search rounds judged' in page and 'No round limit recorded' in page


def test_candidate_filters_and_missing_candidate_are_explicit(project, tmp_path):
    conn = _conn(project)
    ids = improve_run(conn)
    conn.close()
    data = view.run(project, RUN)
    pages = render(tmp_path, {
        'champion': {'data': data, 'params': {'decision': 'champion'}},
        'missing': {'data': data, 'params': {'candidate': 't-missing'}},
    })
    candidates = re.findall(r'data-candidate-row="([^"]+)"', pages['champion'])
    assert candidates == [ids['a5']]
    assert 'Candidate unavailable' in pages['missing']
    assert 'No candidates match' not in pages['champion']


def test_run_browser_separates_controls_and_history(project, tmp_path):
    conn = _conn(project)
    improve_run(conn)
    conn.close()
    data = view.runs(project)
    real = data['runs'][0]
    data['runs'].extend([dict(real, run='rr-002', control='noop'),
                         dict(real, run='rr-003', state='finished')])
    pages = render(tmp_path, {
        'all': {'data': data},
        'control': {'data': data, 'params': {'kind': 'control'}},
    })
    assert 'In progress' in pages['all'] and 'Past runs' in pages['all']
    assert 'Control runs' in pages['all'] and 'Open run' in pages['all']
    assert 'data-run="rr-002"' in pages['control']
    assert 'data-run="rr-001"' not in pages['control']


def test_latest_election_does_not_inherit_an_older_winners_merge(project, tmp_path):
    conn = _conn(project)
    ids = improve_run(conn)
    _round(conn, 1, winner=ids['a6'], value=14)
    conn.close()
    data = view.run(project, RUN)
    page = render(tmp_path, {'p': {'data': data, 'params': {'candidate': ids['a6']}}})['p']
    reader = re.search(r'<section[^>]+data-candidate-reader=.*?</section>', page, re.S).group(0)
    assert 'Elected in round 1' in reader and 'Merge not recorded' in reader
    assert '2 recorded decisions for this round' in page


def test_chart_links_keep_filters_and_open_missing_metric_candidates(project, tmp_path):
    conn = _conn(project)
    ids = improve_run(conn)
    conn.close()
    data = view.run(project, RUN)
    page = render(tmp_path, {'p': {'data': data, 'params': {'decision': 'rejected', 'round': '1'}}})['p']
    chart = re.search(r'<figure class="rs-chart".*?</figure>', page, re.S).group(0)
    assert 'decision=rejected' in chart and 'round=1' in chart
    assert f'candidate={ids["a2"]}' in chart
    assert 'aria-label="Open candidate' in chart


def test_capped_candidates_and_unknown_acceptance_stay_honest(project, tmp_path):
    conn = _conn(project)
    ids = improve_run(conn)
    conn.close()
    data = view.run(project, RUN)
    data['run']['attempts'] = [a for a in data['run']['attempts'] if a['task'] == ids['a6']]
    # A provisional failing gate can exist on a task still in progress.
    data['run']['attempts'][0].update(recorded=True, running=None, status='doing', accepted=None)
    page = render(tmp_path, {'p': {'data': data, 'params': {'candidate': ids['a1']}}})['p']
    assert '1 displayed attempts of 7' in page
    assert 'Candidate unavailable' in page and 'Evaluating' in page
    assert 'In progress:</strong>' in page
    assert '7 attempts registered' in page


def test_unsafe_candidate_text_is_escaped(project, tmp_path):
    conn = _conn(project)
    ids = improve_run(conn)
    conn.close()
    data = view.run(project, RUN)
    a = next(a for a in data['run']['attempts'] if a['task'] == ids['a3'])
    a['title'] = '<img src=x onerror=alert(1)>'
    a['reason'] = '<script>alert(1)</script>'
    page = render(tmp_path, {'p': {'data': data, 'params': {'candidate': a['task']}}})['p']
    assert '<img src=x' not in page and '<script>alert' not in page
    assert '&lt;img' in page and '&lt;script' in page


def test_structured_audit_flags_show_the_rejection_evidence(project, tmp_path):
    conn = _conn(project)
    ids = improve_run(conn)
    conn.close()
    data = view.run(project, RUN)
    a = next(a for a in data['run']['attempts'] if a['task'] == ids['a3'])
    a['audit'] = {'verdict': 'FAIL', 'flags': [{'flag': 'protected-path', 'rule': 'S2',
                                            'lines': [10], 'source': 'scan'}]}
    page = render(tmp_path, {'p': {'data': data, 'params': {'candidate': a['task']}}})['p']
    assert 'protected-path' in page and 'S2' in page and 'line 10' in page
    assert '[object Object]' not in page


def test_repair_case_verification_is_visible_after_candidate_finishes(tmp_path):
    run = {'run': 'rr-001', 'mode': 'repair', 'state': 'running', 'attempts': [],
           'repair': {'rate': {'resolved': 0, 'total': 1, 'percent': 0, 'so_far': True},
                      'items': [{'task': 't-case', 'state': 'doing', 'bucket': 'pending',
                                 'verify': {'state': 'running', 'word': 'running'}}]}}
    page = render(tmp_path, {'p': {'data': {'run': run}}})['p']
    progress = re.search(r'<section class="rs-progress".*?</section>', page, re.S).group(0)
    assert 't-case' in progress and 'verifying' in progress
    assert 'aria-current="step"' in progress
    assert 'candidate=t-case' not in progress
    assert 'No candidate is currently' not in progress


def test_latest_gate_is_not_presented_as_current_after_audit(tmp_path):
    run = {'run': 'rr-001', 'mode': 'improve', 'state': 'running', 'attempts': [
        {'task': 't-work', 'round': 1, 'status': 'doing', 'recorded': False,
         'accepted': True, 'running': 'metric:value', 'outcome_word': 'running',
         'gates': [{'stage': 'metric:value', 'verdict': 'PASS'}],
         'audit': {'verdict': 'PASS', 'flags': []},
         'verify': {'state': 'not-sealed', 'word': 'not-sealed'}}]}
    page = render(tmp_path, {'p': {'data': {'run': run}}})['p']
    progress = re.search(r'<section class="rs-progress".*?</section>', page, re.S).group(0)
    assert 'aria-current="step"' not in progress
    assert 'Latest evidence' in progress and 'audit PASS' in progress


# t-325: visual summaries use complete chart records, not the capped reader payload.
def test_visual_summaries_count_each_metric_and_outcome_once(tmp_path):
    from html import unescape
    chart = {'metric': 'size', 'points': [
        {'task': 't-0', 'value': 0, 'outcome': 'baseline'},
        {'task': 't-1', 'value': 10, 'outcome': 'improvement'}],
        'missing': [{'task': 't-2', 'outcome': 'gate-failed'},
                    {'task': 't-3', 'outcome': None}]}
    second = dict(chart, metric='latency', points=chart['points'][:1],
                  missing=chart['missing'] + [{'task': 't-1', 'outcome': 'improvement'}])
    run = {'run': RUN, 'mode': 'improve', 'state': 'running',
           'attempts': [{'task': 't-3', 'round': 1}], 'attempts_total': 4,
           'chart': {'metrics': [chart, second], 'x_count': 4}}
    page = unescape(render(tmp_path, {'p': {'data': {'run': run}}})['p'])
    coverage = re.search(r'<section[^>]+data-viz="coverage".*?</section>', page, re.S).group(0)
    outcomes = re.search(r'<section[^>]+data-viz="outcomes".*?</section>', page, re.S).group(0)
    assert '2 measured / 4 attempts' in coverage and '1 measured / 4 attempts' in coverage
    assert '2 without a value' in coverage and '3 without a value' in coverage
    assert '4 attempts, including baseline' in outcomes
    assert 'Gate failed' in outcomes and 'No outcome recorded' in outcomes
    assert 'candidate=' not in outcomes  # Aggregate bars do not impersonate candidate links.
    assert 'metric=latency' in coverage


def test_visual_summaries_do_not_infer_totals_without_full_chart_data(tmp_path):
    run = {'run': RUN, 'mode': 'improve', 'state': 'running',
           'attempts': [{'task': 't-1', 'round': 1}], 'attempts_total': 30}
    page = render(tmp_path, {'p': {'data': {'run': run}}})['p']
    assert 'Outcome summary unavailable' in page
    assert '1 attempts, including baseline' not in page
    assert 'No declared metrics' in page


def test_repair_visual_summary_counts_cases_not_attempts(tmp_path):
    items = [{'task': 't-'+k, 'bucket': k, 'verify': {'state': 'required'}}
             for k in ['resolved', 'error', 'unresolved', 'pending']]
    run = {'run': RUN, 'mode': 'repair', 'state': 'running', 'attempts_total': 9,
           'repair': {'items': items, 'rate': {'resolved': 1, 'total': 4, 'percent': 25, 'so_far': True},
                      'buckets': {it['bucket']: [it['task']] for it in items}}}
    page = render(tmp_path, {'p': {'data': {'run': run}}})['p']
    bars = re.search(r'<div[^>]+data-viz="repair-status".*?</div>', page, re.S).group(0)
    assert '4 cases' in bars and '1 resolved' in bars and '1 pending' in bars
    assert '9 cases' not in bars and 'verified' not in bars.lower()


def test_visual_summary_empty_and_unknown_outcomes_are_explicit(tmp_path):
    chart = {'metric': 'latency', 'points': [], 'missing': []}
    run = {'run': RUN, 'mode': 'improve', 'state': 'starting',
           'chart': {'metrics': [chart], 'x_count': 0}}
    empty = render(tmp_path, {'p': {'data': {'run': run}}})['p']
    assert '0 measured / 0 attempts' in empty and 'No attempts recorded.' in empty
    assert 'NaN' not in empty and 'Infinity' not in empty
    chart['points'] = [{'task': 't-0', 'value': 0, 'outcome': '<custom>'}]
    page = render(tmp_path, {'p': {'data': {'run': run}}})['p']
    assert '&lt;custom&gt;' in page and '<custom>' not in page
    assert '1 measured / 1 attempts' in page
