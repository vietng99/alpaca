"""Record mission definitions and execute evidence-bearing capability checks."""
import argparse
import json
from pathlib import Path

from alpaca import cli, mission, mission_model


@cli.command('mission')
def command(args):
    root, session = cli._root(), args.session or 'cli'
    try:
        if args.mission_verb == 'init':
            data = mission_model.starter() if args.preset == 'generic' else json.loads(
                (Path(__file__).with_name('mission-presets') / 'alpaca.json').read_text())
            result = mission.define(root, data, session=session, expected_revision=0)
            result = {'revision': result['id'], 'status': 'configured'}
        elif args.mission_verb == 'define':
            with open(args.file, encoding='utf-8') as stream:
                raw = stream.read(512 * 1024 + 1)
            if len(raw) > 512 * 1024: raise ValueError('definition exceeds 512 KiB')
            result = mission.define(root, json.loads(raw), session=session, expected_revision=args.revision)
            result = {'revision': result['id'], 'status': 'configured'}
        elif args.mission_verb == 'scan':
            event = mission.scan(root, session=session)
            result = {'event_id': event['id'] if event else None, 'status': 'recorded' if event else 'unchanged'}
        elif args.mission_verb == 'show':
            result = mission.project(root, at=args.at)
            if args.definition: result = {'revision': result['revision'], 'definition': result['definition']}
        elif args.mission_verb == 'link':
            result = mission.link(root, args.node, args.task, args.kind, session=session)
            result = {'event_id': result['id'], 'status': 'linked'}
        elif args.mission_verb == 'check':
            command_args = args.check_command
            if command_args[:1] == ['--']: command_args = command_args[1:]
            result = mission.run_check(root, args.node, args.check, command_args, session=session, timeout=args.timeout)
        else:
            raise ValueError('choose init, define, scan, show, link or check')
        print(json.dumps(result, indent=2, ensure_ascii=True))
        return cli.PASS if result.get('result', 'PASS') == 'PASS' else cli.FAIL if result['result'] == 'FAIL' else cli.BLOCKED
    except (ValueError, OSError, TypeError) as exc:
        print(json.dumps({'status': 'error', 'error': str(exc)}))
        return cli.FAIL


def parser(sub):
    entry = sub.add_parser('mission', help='system capabilities, change impact and verification')
    verbs = entry.add_subparsers(dest='mission_verb')
    init = verbs.add_parser('init'); init.add_argument('--preset', choices=('generic', 'alpaca'), default='generic')
    define = verbs.add_parser('define'); define.add_argument('--file', required=True); define.add_argument('--revision', type=int, required=True)
    verbs.add_parser('scan')
    show = verbs.add_parser('show'); show.add_argument('--at', type=int); show.add_argument('--definition', action='store_true')
    link = verbs.add_parser('link'); link.add_argument('node'); link.add_argument('task'); link.add_argument('--kind', choices=('work', 'fix'), default='work')
    check = verbs.add_parser('check'); check.add_argument('node'); check.add_argument('--check', default='tests')
    check.add_argument('--timeout', type=int, default=300)
    check.add_argument('check_command', nargs=argparse.REMAINDER)


cli.register_parser('mission', parser)
