"""Collector operator commands. Watch holds one persistent process lock."""
import json
import os
from pathlib import Path
import signal
import threading

from alpaca import cli
from . import _run, collector_lock, expect_source, run_once, status


@cli.command('collect')
def cmd_collect(args):
    root = cli._root()
    if args.collect_action == 'status':
        result = status(root)
    elif args.collect_action == 'register':
        result = expect_source(root,args.source_session,args.provider,locator=args.path,
                               native_id=args.native_id,parent_session=args.parent,
                               closed=args.closed,capabilities={'child_inventory_complete':args.closed})
    elif args.collect_action == 'services':
        from .maintenance import write_services
        try:
            result=write_services(root,port=args.port,tunnel=args.tunnel,cloudflared=args.cloudflared,
                                  tunnel_config=args.tunnel_config,watchdog_state=args.watchdog_state)
        except ValueError as error:
            print(json.dumps({'error':str(error)}))
            return cli.USAGE
    elif args.collect_action == 'backup':
        from .maintenance import backup_once
        result=backup_once(root,budget_bytes=args.budget_bytes)
    elif args.collect_action == 'enable':
        folder = Path(root)/'.alpaca/observability'
        folder.mkdir(parents=True,exist_ok=True,mode=0o700)
        (folder/'enabled').touch(mode=0o600)
        result = {'enabled':True,'next':'collect watch --interval 15'}
    elif args.collect_action == 'watch':
        os.environ['ALPACA_COLLECTOR_WATCH'] = '1'
        stop = threading.Event()
        for sig in (signal.SIGTERM,signal.SIGINT):
            signal.signal(sig,lambda *_:stop.set())
        with collector_lock(root):
            while not stop.is_set():
                result = _run(root,max_records=args.max_records)
                print(json.dumps(result),flush=True)
                stop.wait(max(0.1,args.interval))
        return cli.PASS
    else:
        result = run_once(root,max_records=args.max_records)
    print(json.dumps(result,indent=2))
    return cli.FAIL if result.get('errors') or result.get('ok') is False else cli.PASS


def _parser(sub):
    p=sub.add_parser('collect',help='durable local observability collector')
    verbs=p.add_subparsers(dest='collect_action',required=True)
    for name in ('once','watch'):
        item=verbs.add_parser(name)
        item.add_argument('--max-records',type=int,default=1000)
        if name=='watch':
            item.add_argument('--interval',type=float,default=15)
    verbs.add_parser('status')
    services=verbs.add_parser('services',help='generate project user services and local backup timer')
    services.add_argument('--port',type=int,default=None,
                          help='dashboard port (default: the port `alpaca workspace add` registered, else one derived from the root; never a reserved port)')
    services.add_argument('--tunnel',help='optional existing tunnel name or UUID; adds the host tunnel unit and its watchdog; does not create or change the tunnel')
    services.add_argument('--cloudflared',help='absolute cloudflared executable path (default: /usr/local/bin/cloudflared)')
    services.add_argument('--tunnel-config',help='absolute combined cloudflared config the tunnel unit reads (default: ~/.cloudflared/config.yml)')
    services.add_argument('--watchdog-state',help='absolute watchdog state file (default: ~/.local/state/alpaca/tunnel-<name>-watchdog.state)')
    backup=verbs.add_parser('backup',help='verified scheduled backup with no-delete storage budget')
    backup.add_argument('--budget-bytes',type=int,default=20*1024**3)
    verbs.add_parser('enable',help='defer expensive hook projections to supervised collector')
    register=verbs.add_parser('register')
    register.add_argument('source_session')
    register.add_argument('--provider',required=True,choices=['codex','claude','terminal','pool'])
    register.add_argument('--path')
    register.add_argument('--native-id')
    register.add_argument('--parent')
    register.add_argument('--closed',action='store_true',help='explicitly attest complete child source inventory')

cli.register_parser('collect',_parser)
