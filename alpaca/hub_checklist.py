"""Readable checklist projection from recorded work, without checking or running any profile stage.

The live hub performs current evidence checks. This document labels its results as
last recorded observations so rendering never turns a filesystem probe into truth.
"""
import re
from pathlib import Path
from urllib.parse import quote
from alpaca import db, render as text, work_record


def _cell(value):
    return text.cell(str(value or 'Not recorded'))


def _reason(card):
    return card.get('reason') or (card.get('verdict') or {}).get('reason')


def _proof(value):
    value = str(value or '')
    if value.startswith('local:'):
        path = value[6:]
        if not path.startswith('/') and '..' not in path.split('/'):
            return '[Work report and proof](%s)' % quote(path, safe='/.-_')
    return _cell(value)


def render(conn, cards):
    # The database path supplies the project root without a second connection or
    # any migration. In-memory records can still render provenance without files.
    database = next((row[2] for row in conn.execute('PRAGMA database_list') if row[1] == 'main'), '')
    if database and Path(database).parent.name == '.alpaca':
        from alpaca.hub import _tasks
        tasks = _tasks(Path(database).resolve().parent.parent, conn)
    else:
        tasks = work_record.tasks(conn)
    ops = {row['id']:row for row in db.rows(conn, 'ops')}
    active = [row for row in tasks if row['status'] not in ('done','cancelled','canceled')]
    lines = ['', '## Work and next action', '',
             'This is the shared work record for people and agents. Completion is a recorded result backed by a report. '
             'The live Operations hub checks whether recorded evidence still matches current inputs.', '']
    if active:
        next_task = active[0]
        verb = 'Unblock' if next_task['status'] == 'blocked' else 'Continue'
        lines += ['Next action: %s %s: %s' % (verb,next_task['id'],_cell(next_task['title'])), '']
    else:
        lines += ['Next action: no open task is recorded. Review current evidence before accepting or extending the work.', '']
    lines += ['%d tasks recorded complete; %d tasks remain.' % (sum(t['status']=='done' for t in tasks),len(active)), '']
    for task in tasks:
        op = ops.get(task['op'], {})
        label = '%s. %s' % (task['number'],task['id']) if task['number'] is not None else task['id']
        lines += ['### %s: %s' % (label,_cell(task['title'])), '',
                  '- State: **%s**' % _cell(task['status'].upper()),
                  '- Purpose: %s' % _cell(task['purpose']),
                  '- Source operation: %s / %s' % (_cell(task['op']),_cell(op.get('intent'))),
                  '- Created / updated: %s / %s' % (_cell(task['created_at']),_cell(task['updated_at'])),
                  '- Completed at: %s' % _cell(task['completed_at']),
                  '- Completion session: %s' % _cell(task['completed_session'])]
        if task['claimant']:
            lines.append('- Assigned worker: %s' % _cell(task['claimant']))
            lines.append('- Assigned session: %s' % _cell(task['assigned_session']))
        report = task['report'] or {}
        if report and task['last_completion'] and task['status'] != 'done':
            lines.append('- Historical report from an earlier completion. This task is currently %s; these excerpts do not establish a new completion.' % _cell(task['status']))
        for label, key in [('What was done','what'),('How it was done','how'),('Result','result')]:
            # These are prose excerpts, not technical table cells. Fold authored
            # wrapping before neutralisation so humans do not see literal \n escapes.
            excerpt = ' '.join(str(report.get(key) or 'Not recorded').split())
            lines.append('- %s: %s' % (label,_cell(excerpt)))
        if report.get('truncated'):
            lines.append('- Report excerpts are shortened; open the linked report for the full account.')
        lines.append('- Work report and evidence: %s' % (_proof(task['proof']) if task['proof'] else 'No work report is linked yet.'))
        if report.get('sealed'):
            lines.append('- Seal recorded at: %s / session %s. Current seal validity is not checked by this document.' % (_cell(report['sealed_at']),_cell(report['sealed_session'])))
        lines.append('')
    groups = {}
    for card in cards:
        match = re.match(r'(state|oracle|proof-kind|discharge|no-drift)\.(ac-\d+)-',card['row_id'])
        if match:
            groups.setdefault(match.group(2).upper(), {})[match.group(1)] = card
    if groups:
        outcomes = []
        for ident, group in sorted(groups.items()):
            verification = [group.get(step,{}) for step in ('discharge','no-drift')]
            columns = [c.get('column') for c in verification]
            status = 'PASS' if columns == ['done','done'] else 'BLOCKED' if 'blocked' in columns else 'NOT VERIFIED'
            claim = _reason(group.get('state',{})) or ''
            claim = re.sub(r'^one stated claim of \d+ words:\s*','',claim)
            primary = group.get('discharge') or group.get('state') or next(iter(group.values()))
            title = claim or (_reason(primary) or primary.get('statement') or ident).split('; stage ')[0]
            method = ' | '.join(_reason(group.get(step,{})) for step in ('oracle','proof-kind') if _reason(group.get(step,{})))
            outcomes.append((ident, title, status, primary, verification, method))
        lines += ['## Runbook outcomes', '',
                  '%d runbook outcome%s; %d recorded passing. Internal specification checks do not count as completed outcomes.'
                  % (len(outcomes), '' if len(outcomes)==1 else 's',sum(row[2]=='PASS' for row in outcomes)), '',
                  'These are the last recorded checks, not a fresh execution. Use the hub Work & evidence view for current validity.', '']
        for ident,title,status,primary,verification,method in outcomes:
            reasons = list(dict.fromkeys(_reason(c) for c in verification if _reason(c)))
            evidence = list(dict.fromkeys(ref for c in verification for ref in (c.get('verdict') or {}).get('evidence',[])))
            stamp = max(((c.get('verdict') or {}).get('ts') or '' for c in verification),default='')
            last = max((c.get('verdict') or {} for c in verification),key=lambda v:v.get('ts') or '',default={})
            lines += ['### %s. %s: %s' % (work_record.number(ident),ident,_cell(title)), '',
                      '- Last recorded result: **%s**%s' % (status,' / '+stamp if stamp else ''),
                      '- Completed at (recorded): %s' % _cell(stamp if status == 'PASS' else None),
                      '- Completion session (recorded): %s' % _cell(last.get('session') if status == 'PASS' else None),
                      '- Recorded checking method: %s' % _cell(method),
                      '- What the check found: %s' % _cell(' | '.join(reasons)),
                      '- Source: %s' % _cell(str(primary.get('proof') or '').removeprefix('local:')),
                      '- Proof and observations: %s' % _cell(' | '.join(evidence)), '']
    lines += ['## Technical obligation rows', '',
              'Detailed internal checks follow for audit and agent tooling. Their counts are separate from task and runbook progress.']
    return lines
