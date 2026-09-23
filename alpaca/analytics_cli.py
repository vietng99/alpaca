"""Verbs: alpaca analytics build [--force] | snapshot [--session SID]; alpaca recall <sid-prefix>."""
from alpaca import cli, db
from alpaca.analytics import build_index
from alpaca.analytics import usage

@cli.command("analytics")
def cmd_analytics(args):
    root = cli._root()
    if args.an_verb == "build":
        p = build_index.build(root, force=args.force)
        print("alpaca analytics: %s" % p); return cli.PASS
    if args.an_verb == "ingest-usage":
        try:
            result = usage.ingest(root, args.usage_session, args.file)
        except ValueError as exc:
            print("GATE alpaca-analytics-ingest-usage: FAIL (%s)" % exc)
            return cli.FAIL
        print("alpaca analytics: imported %d sample(s), %d duplicate(s) for %s" %
              (result["inserted"], result["duplicates"], result["session"]))
        return cli.PASS
    if args.an_verb == "snapshot":
        # Backfill: copy every registered transcript whose source still exists into
        # .alpaca/transcripts/, one transcript-snapshot event each. The hooks do this at every
        # boundary; this verb covers the sessions that ended before the hooks were in place.
        from alpaca import transcripts
        conn = db.connect(root)
        where = "transcript IS NOT NULL AND transcript != ''"
        rows = db.rows(conn, "sessions", where + (" AND sid=?" if args.snap_session else ""),
                       (args.snap_session,) if args.snap_session else ())
        copied = gone = 0
        for r in rows:
            out = transcripts.snapshot(root, r["sid"], conn=conn, event=True)
            if out.get("source_missing") and not out.get("bytes"):
                gone += 1
            else:
                copied += 1
            print("%-44s %-8s %s bytes" % (r["sid"], out.get("mode"), out.get("bytes")))
        print("alpaca analytics: %d snapshot(s), %d source(s) already gone" % (copied, gone))
        return cli.PASS
    return cli.USAGE

@cli.command("recall")
def cmd_recall(args):
    conn = db.connect_readonly(cli._root())
    sids = [r["sid"] for r in db.rows(conn, "sessions", "sid LIKE ? ORDER BY started", (args.sid + "%",))]
    if not sids:
        print("GATE alpaca-recall: FAIL (no session starting with %s)" % args.sid); return cli.FAIL
    if len(sids) > 1:
        print("GATE alpaca-recall: FAIL (ambiguous prefix %s: %s)" % (args.sid, ", ".join(sids))); return cli.FAIL
    for e in db.events(conn, session=sids[0], limit=100000):
        print("%s %-14s %-8s %s %s" % (e["ts"], e["kind"], e["op"] or "", e["ref"] or "", e["data"]))
    return cli.PASS

def _parser(sub):
    a = sub.add_parser("analytics"); av = a.add_subparsers(dest="an_verb")
    b = av.add_parser("build"); b.add_argument("--force", action="store_true")
    sn = av.add_parser("snapshot", help="copy registered transcripts into .alpaca/transcripts/")
    sn.add_argument("--session", dest="snap_session", default=None)
    u = av.add_parser("ingest-usage", help="import explicit provider usage for one session")
    u.add_argument("--session", dest="usage_session", required=True)
    u.add_argument("--file", required=True)
    r = sub.add_parser("recall"); r.add_argument("sid")

cli.register_parser("analytics", _parser)
