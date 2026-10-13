"""Watch tasks after a publish: an executed publish files two tasks, due one hour and 24 hours after
the release went public, in the op the publisher names, else the one op the publishing session holds
tasks in, else the standing Release watch op. It writes only through ops, taskplan and taskcontract, in one transaction, and never
changes a receipt. Nothing here can fail the publish."""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import shlex
import sqlite3

from alpaca import batch as _batch, board, db, ops as _ops, taskcontract, taskplan, util
from .receipts import PublishReceipt, load_receipt

GROUP, GROUP_TITLE, CATEGORY = 'release-watch', 'Release watch', 'release'
#: the two windows as (label, seconds); a ship runbook's `after` is read in seconds, so 60m is 1h
WINDOWS = (('1h', 3600), ('24h', 86400))
ACTOR = 'alpaca-release'
WATCH_TITLE = 'Release watch'
WATCH_INTENT = 'Release watch: the watch tasks of release publishes that name no op and run in none'
WATCH_DONE = 'it stays open as the home of release watch tasks'
#: the checks when no ship runbook is given
DEFAULT_CHECKS = (
    'release check receipt: the check receipt of this release still loads with its recorded hash',
    'hub reachable: the project hub answers',
    'install from the published tag: a clean install from the tag works (the tag exists only after '
    'the owner merges the sync pull request and runs alpaca release tag)',
)


def due_at(created_at, seconds):
    """UTC time `seconds` after a receipt's created_at, in whole seconds with the offset written out."""
    base = datetime.fromisoformat(created_at.replace('Z', '+00:00')).astimezone(timezone.utc)
    return (base.replace(microsecond=0) + timedelta(seconds=seconds)).isoformat()


def one_line(exc):
    return ' '.join(str(exc).split())


def derived(text, reason):
    return {'text': text, 'origin': 'derived', 'reason': reason}


def first_publish(root, key, receipt):
    """The earliest recorded PASS publish receipt of check attempt `key` with a pull request: the
    time the release went public. A rerun of publish makes a new receipt; the due times stay."""
    best = receipt
    conn = db.connect_readonly(str(root))
    try:
        rows = [json.loads(r['data']) for r in
                conn.execute("SELECT data FROM events WHERE kind='release-publish' ORDER BY id")]
    finally:
        conn.close()
    for data in rows:
        if data.get('status') != 'PASS':
            continue
        try:
            found = load_receipt(root, data['receipt'], PublishReceipt)
            if found.checks.get('attempt') != key or not found.pr_url:
                continue
            if _utc(found.created_at) < _utc(best.created_at):
                best = found
        except (ValueError, OSError, KeyError, TypeError):
            continue
    return best


def _utc(text):
    return datetime.fromisoformat(text.replace('Z', '+00:00')).astimezone(timezone.utc)


def filed(conn, key):
    """[(task, op, data)] of the watch tasks filed for check attempt `key`, oldest first."""
    out = []
    for row in conn.execute("SELECT ref, op, data FROM events WHERE kind='task-add' "
                            "AND data LIKE '%release_watch%' ORDER BY id"):
        data = json.loads(row['data'] or '{}')
        if (data.get('release_watch') or {}).get('checks') == key:
            out.append((row['ref'], row['op'], data))
    return out


def resolve_op(conn, session, op):
    """(op, why) or (None, why not). Never guesses."""
    if op:
        target, why = _open_op(conn, op)
        return (op, 'named by --watch-op') if target else (None, why)
    # `cli` is the default writer, not an identity: it holds nothing
    if not session or session == 'cli':
        return None, 'no --watch-op and the shared cli session holds no task'
    doing = [r['id'] for r in conn.execute("SELECT id FROM tasks WHERE status='doing'")]
    now = datetime.fromisoformat(util.now_iso())
    held = []
    for tid, holder in sorted(board.fold_leases(conn, doing).items()):
        if holder.get('session') != session:
            continue
        try:
            live = datetime.fromisoformat(str(holder.get('lease_until')).replace('Z', '+00:00')) > now
        except ValueError:
            live = False
        if live:
            held.append(tid)
    standing = {r['op'] for r in conn.execute(_ops._STANDING_OPS)}   # a standing op is never the held op
    found = {}
    for tid in held:
        held_op = db.rows(conn, 'tasks', 'id=?', (tid,))[0]['op']
        if held_op not in standing:
            found.setdefault(held_op, tid)
    if not found:
        return None, 'no --watch-op and session %s holds no live task' % session
    if len(found) > 1:
        return None, 'no --watch-op and session %s holds tasks in %s' % (session, ', '.join(sorted(found)))
    (only, tid), = found.items()
    target, why = _open_op(conn, only)
    if target is None:
        return None, why
    return target, 'session %s holds %s' % (session, tid)


def _room(conn, op):
    """True when the plan of `op` can take the watch group (if it lacks one) and two tasks."""
    plan = taskplan.latest(conn, op)
    groups = [g.get('id') for g in plan['groups']]
    return ((GROUP in groups or len(groups) < taskplan.MAX_GROUPS)
            and len(plan['tasks']) + len(WINDOWS) <= taskplan.MAX_TASKS)


def watch_op(conn, session):
    """(op, opened): the open Release watch op with room for two tasks; when there is none, or the
    one there is full (it stays open with its tasks), a new one opened and marked. Called only
    inside _file_watches' transaction."""
    found = _ops.standing_op(conn, _ops.RELEASE_WATCH_KIND)
    if found and _room(conn, found):
        return found, False
    return _ops.open_standing(conn, _ops.RELEASE_WATCH_KIND, WATCH_INTENT, WATCH_DONE, WATCH_TITLE,
                              session=session, actor=ACTOR), True


def _open_op(conn, op):
    rows = db.rows(conn, 'ops', 'id=?', (op,))
    if not rows:
        return None, 'no op %s' % op
    if rows[0]['status'] != 'open':
        return None, 'op %s is %s' % (op, rows[0]['status'])
    return op, ''


def watch_checks(root, path):
    """{'1h': [line, ...], '24h': [line, ...]} from a valid kind: ship runbook, or ValueError."""
    from alpaca import runbook
    path = Path(path)
    path = path if path.is_absolute() else Path(root) / path
    data, report = runbook.load(str(path))
    if data is None:
        first = report.errors[0] if report.errors else {}
        raise ValueError('runbook %s cannot be read: %s' % (path, first.get('code', 'unreadable')))
    errors, _ = runbook.validate(data, os.path.dirname(os.path.abspath(path)), check_files=True, report=report)
    if errors:
        raise ValueError('runbook %s does not check: %s at %s' % (path, errors[0]['code'], errors[0]['where']))
    if data.get('kind') != 'ship':
        raise ValueError('runbook %s is not kind: ship' % path)
    out = {label: [] for label, _ in WINDOWS}
    for stage in data.get('stages') or []:
        watch = stage.get('watch')
        if not watch or stage.get('recovery'):
            continue
        found = runbook.WATCH_AFTER.fullmatch(str(watch.get('after')))
        if not found:
            continue
        seconds = int(found.group(1)) * runbook.WATCH_UNITS[found.group(2)]
        label = next((name for name, wanted in WINDOWS if wanted == seconds), None)
        if label is None:
            continue
        own = {c.get('id'): c for c in stage.get('checks') or []}
        for ident in watch.get('looks_at') or []:
            check = own.get(ident) or {}
            line = '%s: %s' % (stage['id'], check.get('description') or ident)
            if line not in out[label]:
                out[label].append(line)
    for label, _ in WINDOWS:
        if not out[label]:
            raise ValueError('runbook %s has no %s watch' % (path, label))
    return out


def fit(lines, limit=taskcontract.MAX_ITEMS):
    """At most `limit` done bar lines of at most MAX_TEXT characters: the rest fold into the last
    line, never dropped silently (the same rule as intake._fit)."""
    lines = [l[:taskcontract.MAX_TEXT] for l in lines if l]
    if len(lines) <= limit:
        return lines
    rest = lines[limit - 1:]
    return lines[:limit - 1] + [('and %d more: %s' % (len(rest), '; '.join(rest)))[:taskcontract.MAX_TEXT]]


def contracts_for(checks, due, anchor, key):
    """{label: (title, statement, extra, contract)} for the two watch tasks."""
    published = due_at(anchor.created_at, 0)
    rel = '.alpaca/releases/receipts/%s-publish.json' % anchor.attempt
    reason = 'release:' + anchor.attempt
    out = {}
    for label, _ in WINDOWS:
        lines = fit(checks[label])
        title = 'Release watch T+%s %s' % (label, (anchor.commit or key)[:12])
        statement = 'Watch the release published at %s UTC (%s) at T+%s, due %s UTC: %s' % (
            published, anchor.pr_url, label, due[label], '; '.join(lines))
        extra = {'release_watch': {'checks': key, 'publish': anchor.attempt, 'window': label,
                                   'published_at': published, 'due': due[label], 'commit': anchor.commit,
                                   'pr_url': anchor.pr_url}}
        contract = {'input': [derived('Publish receipt %s, created %s UTC, pull request %s'
                                      % (rel, published, anchor.pr_url), reason),
                              derived('Due %s UTC (publish receipt time + %s)' % (due[label], label), reason)],
                    'done_bar': [derived(line, reason) for line in lines]}
        taskcontract.check(contract)
        out[label] = (title, statement, extra, contract)
    return out


def not_filed(why, due, checks=None, op=None, phase=False):
    """The `watch` value when nothing was filed: why, and the commands to file them by hand (one
    `task add` per window, each with the done bar lines the filed task would carry)."""
    checks = checks or {label: list(DEFAULT_CHECKS) for label, _ in WINDOWS}
    target = shlex.quote(op) if op else 'OP'
    tail = ' --phase PHASE' if phase else ''
    adds = []
    for label, _ in WINDOWS:
        bars = ' '.join('--done-bar %s' % shlex.quote(line) for line in fit(checks[label]))
        adds.append('alpaca task add %s %s --title "Release watch T+%s" --group %s --independent --no-resources%s '
                    '--input %s %s' % (target, shlex.quote('Watch the release at T+' + label), label, GROUP, tail,
                                       shlex.quote('Due %s UTC' % due[label]), bars))
    text = ('not filed: %s; rerun this publish with --watch-op OP, or file them by hand: alpaca task group %s '
            '%s --title "%s" --category %s, then %s'
            % (why, target, GROUP, GROUP_TITLE, CATEGORY, ', then '.join(adds)))
    return {'op': None, 'tasks': [], 'due': due, 'note': ' '.join(text.split())}


def file_watches(root, session, receipt, op=None, runbook=None):
    """The `watch` value of an executed publish `receipt`, as _file_watches returns it. The pull request
    exists by now, so any other error (a RecursionError from the YAML loader, a bug) comes back as a
    not-filed note too; the caller's exit code and pr_url output never depend on this step."""
    try:
        return _file_watches(root, session, receipt, op, runbook)
    except Exception as exc:
        try:
            anchor = first_publish(root, receipt.checks.get('attempt', ''), receipt)
        except Exception:
            anchor = receipt
        due = {label: due_at(anchor.created_at, seconds) for label, seconds in WINDOWS}
        return not_filed('internal: %s: %s' % (type(exc).__name__, one_line(exc)), due, op=op)


def _file_watches(root, session, receipt, op, runbook):
    """The `watch` value of an executed publish `receipt` (PASS, with a pr_url):
    {op, tasks, due, note}. Files the T+1h and T+24h watch tasks once per check receipt; a rerun
    reports the tasks it filed before. With no op named and none held they go to the standing Release
    watch op, also when the op the session holds has a batch or no room. A task of a standing op never counts as the held op. Nothing is filed when the op
    named by --watch-op is not open or needs a phase, or the runbook is not a valid ship runbook; `note` says why and how to file them by hand. Never raises for these or for record errors."""
    key = receipt.checks.get('attempt', '')
    target, standing = None, False
    try:
        anchor = first_publish(root, key, receipt)
    except (ValueError, OSError, sqlite3.Error):
        anchor = receipt
    due = {label: due_at(anchor.created_at, seconds) for label, seconds in WINDOWS}
    checks = None
    try:
        if runbook:
            checks = watch_checks(root, runbook)
        else:
            checks = {label: list(DEFAULT_CHECKS) for label, _ in WINDOWS}
        plan = contracts_for(checks, due, anchor, key)
    except (ValueError, OSError) as exc:
        text = one_line(exc)
        if runbook and 'runbook' not in text:
            text = 'runbook %s: %s' % (runbook, text)
        return not_filed(text, due, checks=checks, op=op)
    try:
        conn = db.connect(str(root))
    except (ValueError, OSError, sqlite3.Error) as exc:
        return not_filed(one_line(exc), due, checks=checks, op=op)
    try:
        with db.transaction(conn):
            found = filed(conn, key)
            if found:
                return {'op': found[0][1], 'tasks': [f[0] for f in found],
                        'due': {f[2]['release_watch']['window']: f[2]['release_watch']['due'] for f in found},
                        'note': 'already filed for these checks in %s' % found[0][1]}
            target, why = resolve_op(conn, session, op)
            unfit = ''
            if target is not None and not op:   # the held op cannot take the tasks: a batch needs a phase
                if _batch.has_batch(conn, target):
                    unfit = '%s has a batch, so its tasks need a phase' % target
                elif not _room(conn, target):
                    unfit = '%s has no room in its plan' % target
            if (target is None or unfit) and not op:   # a named op is the publisher's choice: never rerouted
                target, opened = watch_op(conn, session)
                standing = True
                why = '%s; the standing %s op takes them%s' % (unfit or why, WATCH_TITLE,
                                                                ' (opened now)' if opened else '')
            if target is None:
                return not_filed(why, due, checks=checks, op=op)
            if not any(g['id'] == GROUP for g in taskplan.latest(conn, target)['groups']):
                taskplan.group(conn, target, GROUP, GROUP_TITLE, CATEGORY, session=session, actor=ACTOR)
            ids = []
            for label, _ in WINDOWS:
                title, statement, extra, contract = plan[label]
                tid = _ops.add_task(conn, target, statement, title, session=session, actor=ACTOR, extra=extra,
                                    mapping={'group': GROUP, 'after': [], 'resources': []})
                taskcontract.record(conn, tid, contract, session=session, actor=ACTOR, root=str(root))
                ids.append(tid)
            return {'op': target, 'tasks': ids, 'due': due, 'note': 'filed in %s (%s)' % (target, why)}
    except (ValueError, OSError, sqlite3.Error) as exc:
        text = one_line(exc)
        # the standing op was rolled back with the transaction: a hand command must not name it
        return not_filed(text, due, checks=checks, op=op if standing else (target or op),
                         phase='TASK-PHASE' in text)
    finally:
        conn.close()
