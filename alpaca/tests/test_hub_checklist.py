from alpaca import db, pad


def test_checklist_leads_with_readable_work_and_proof(project):
    conn=db.connect(project)
    db.upsert(conn,'ops','id',{'id':'op-001','intent':'Improve project visibility','status':'open','done_when':'Engineers can trace outcomes'})
    db.upsert(conn,'tasks','id',{'id':'t-001','op':'op-001','statement':'Explain simulation results','status':'doing','why':'The owner needs the failed test and its log','proof':None})
    db.upsert(conn,'tasks','id',{'id':'t-002','op':'op-001','statement':'Document timing results','status':'done','proof':'local:.alpaca/proofs/op-001/t-002.md'})
    conn.close()
    text=pad.render_checklist(project)
    assert '## Work and next action' in text
    assert 'Continue t-001: Explain simulation results' in text
    assert 'The owner needs the failed test and its log' in text
    assert '[Work report and proof](.alpaca/proofs/op-001/t-002.md)' in text
    assert text.index('## Work and next action') < text.index('## Technical obligation rows')


def test_grouped_outcome_cannot_pass_from_specification_checks_only(project):
    from alpaca.hub_checklist import render
    conn=db.connect(project)
    cards=[{'row_id':step+'.ac-09-abc','step':step,'column':'done' if step in ('state','oracle','proof-kind') else 'blocked',
        'reason':'one stated claim of 7 words: Simulation passes all three tests' if step=='state' else 'Simulation passes all three tests; stage sim is BLOCKED: evidence changed',
        'proof':'local:flow/spec/adder8-runbook-acceptance.md:33','verdict':{'ts':'2026-09-22T00:00:00Z','evidence':['local:flow/results.xml']}}
        for step in ('state','oracle','proof-kind','discharge','no-drift')]
    text='\n'.join(render(conn,cards))
    assert 'Simulation passes all three tests' in text
    assert '**BLOCKED**' in text
    assert '1 runbook outcome; 0 recorded passing' in text
    assert 'Source: flow/spec/adder8-runbook-acceptance.md:33' in text
    assert 'flow/results.xml' in text
    conn.close()
