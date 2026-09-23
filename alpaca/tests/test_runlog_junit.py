"""JUnit report extraction: status, seed and units per test, null never zero."""
from alpaca.runlog import junit


def test_junit_test_status_seed_and_absent_duration(tmp_path):
    m = junit; p = tmp_path / 'results.xml'
    p.write_text('<testsuites><testsuite name="suite"><testcase name="ok" time="0.25"><properties><property name="random_seed" value="42"/></properties></testcase><testcase name="broken"><failure message="bad"/></testcase></testsuite></testsuites>')
    out = m.parse_junit(p)
    assert [r['status'] for r in out['tests']] == ['passed', 'failed']
    assert out['tests'][0]['seed'] == '42'
    assert out['tests'][1]['duration_s'] is None
    assert out['measurements'][0]['unit'] == 's'
    assert out['measurements'][0]['source']['sha256']
    assert out['measurements'][0]['parser']
    p.write_text('<broken')
    assert m.parse_junit(p)['status'] == 'unavailable'


def test_named_properties_become_measurements_and_absent_ones_do_not(tmp_path):
    p = tmp_path / 'results.xml'
    p.write_text('<testsuite><testcase classname="k" name="a" time="1"><properties>'
                 '<property name="run_time" value="12.5"/><property name="run_unit" value="ms"/>'
                 '</properties></testcase><testcase name="b" time="2"/></testsuite>')
    out = junit.parse_junit(p, properties={'run_duration': ('run_time', 'run_unit')})
    extra = [r for r in out['measurements'] if r['name'] == 'run_duration']
    # positive: the named property of the case that carries it is measured with its own unit.
    assert [(r['subject'], r['value'], r['unit']) for r in extra] == [('k:a', 12.5, 'ms')]
    # negative: without the parameter only durations are measured.
    assert {r['name'] for r in junit.parse_junit(p)['measurements']} == {'duration_s'}


def test_a_missing_or_foreign_report_is_unavailable_not_zero(tmp_path):
    out = junit.parse_junit(tmp_path / 'absent.xml')
    assert out['status'] == 'unavailable' and out['tests'] == []
    assert out['source']['sha256'] is None and 'source unavailable' in out['issues']
    p = tmp_path / 'other.xml'
    p.write_text('<report><testcase name="x"/></report>')
    assert junit.parse_junit(p)['issues'] == ['unrecognized JUnit root']
