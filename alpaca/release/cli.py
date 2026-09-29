"""Release commands use this installation's project root and record."""
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

from alpaca import cli
from .config import load_config
from .build import build_release
from .receipts import BuildReceipt, CheckReceipt, PublishReceipt, load_receipt, record_receipt

AGENT_MARKERS = ('CLAUDECODE', 'CLAUDE_CODE_SESSION_ID', 'CODEX_THREAD_ID', 'CODEX_SANDBOX')


def owner_confirmation(sha):
    """An owner decision is typed at a terminal. Agent sessions use --by agent with a delegation."""
    if any(os.environ.get(key) for key in AGENT_MARKERS) or not sys.stdin.isatty():
        raise ValueError('owner approval must be typed in an interactive terminal outside an agent session; '
                         'an agent uses --by agent --delegation PATH[#section]')
    typed = input('Type the first 12 characters of ' + sha + ' to confirm: ').strip()
    if typed != sha[:12]:
        raise ValueError('confirmation did not match; nothing recorded')


@cli.command('release')
def command(args):
    root = Path(cli._root()).resolve()
    try:
        config = load_config(root)
        if args.release_verb == 'init':
            from .workflows import initialize
            print(json.dumps(initialize(root, config, args.dev_output, args.public_output)))
            return 0
        if args.release_verb == 'approve':
            from .approval import approve, summary
            if args.by == 'owner':
                owner_confirmation(args.sha)
            path, receipt = approve(root, args.sha, approver=args.by, delegation=args.delegation or '',
                                    decision='revoke' if args.revoke else 'approve', session=args.session or 'cli')
            print(json.dumps(summary(path, receipt)))
            return 0
        if args.release_verb == 'build':
            out = root / (args.output or ('.alpaca/releases/' + uuid.uuid4().hex + '/tree'))
            receipt = build_release(root, config, out)
            path = record_receipt(root, args.session or 'cli', receipt)
            print(json.dumps({'receipt': str(path), 'tree': receipt.tree, 'tree_hash': receipt.tree_hash}))
            return 0
        if args.release_verb == 'check':
            from .checks import check_release
            build = load_receipt(root, args.receipt, BuildReceipt)
            receipt = check_release(root, config, build)
            path = record_receipt(root, args.session or 'cli', receipt)
            print(json.dumps({'receipt': str(path), 'status': receipt.status}))
            return 0 if receipt.status == 'PASS' else 1
        if args.release_verb == 'publish':
            from .publish import publish_release
            checks = load_receipt(root, args.receipt, CheckReceipt)
            expect = None
            if args.execute:
                if not args.expect:
                    raise ValueError('--execute needs --expect DRY_RUN_RECEIPT from a reviewed dry run')
                reviewed = load_receipt(root, args.expect, PublishReceipt)
                if reviewed.status != 'DRY_RUN' or reviewed.checks.get('attempt') != checks.attempt:
                    raise ValueError('--expect must be a dry-run receipt for these checks')
                expect = reviewed.outgoing
            receipt = publish_release(root, config, checks, dry_run=not args.execute, expect=expect)
            path = record_receipt(root, args.session or 'cli', receipt)
            print(json.dumps({'receipt': str(path), 'status': receipt.status,
                              'report': receipt.report, 'pr_url': receipt.pr_url,
                              'error': receipt.error}))
            return 0 if receipt.status in ('PASS', 'DRY_RUN') else 2
        return 64
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        print(json.dumps({'status': 'BLOCKED', 'error': str(exc)}))
        return 2


def parser(sub):
    release = sub.add_parser('release', help='build and verify clean release trees')
    verbs = release.add_subparsers(dest='release_verb', required=True)
    init = verbs.add_parser('init', help='write reviewable GitHub workflow templates without overwriting edits')
    init.add_argument('--dev-output', default='.github/workflows/alpaca-dev.yml')
    init.add_argument('--public-output', default='.alpaca/releases/workflows/alpaca-public.yml')
    approve = verbs.add_parser('approve', help='approve or revoke one exact development commit for release')
    approve.add_argument('sha', help='full 40-character development commit SHA')
    approve.add_argument('--by', required=True, choices=['owner', 'agent'], help='who decides')
    approve.add_argument('--delegation', help='PATH[#section] of the project file holding the owner delegation an agent acts under')
    approve.add_argument('--revoke', action='store_true', help='withdraw the approval of this commit')
    build = verbs.add_parser('build', help='stage an allowlisted deterministic tree')
    build.add_argument('--output', help='new output under .alpaca/releases/')
    check = verbs.add_parser('check', help='run mandatory and configured checks on a recorded build')
    check.add_argument('receipt', help='recorded build receipt path')
    publish = verbs.add_parser('publish', help='prepare a safe public sync; dry run by default')
    publish.add_argument('receipt', help='recorded passing check receipt path')
    mode = publish.add_mutually_exclusive_group()
    mode.add_argument('--execute', action='store_true', help='push sync branch and create or update its PR')
    mode.add_argument('--dry-run', action='store_true', help='write a report without remote changes (default)')
    publish.add_argument('--expect', help='the reviewed dry-run publish receipt; required with --execute')


cli.register_parser('release', parser)
