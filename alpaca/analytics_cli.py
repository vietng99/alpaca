"""Verbs: alpaca analytics build [--force] | snapshot [--session SID] | ingest-usage |
price-check [--model M ...] [--dry-run] [--url URL] | price-label ... | selftest [--online]
[--label-gaps] [--json]; alpaca recall <sid-prefix>."""
import http.client
import json
import sys

from alpaca import cli, db
from alpaca.analytics import build_index
from alpaca.analytics import pricing, ratecards
from alpaca.analytics import usage


def _rates_text(rates, fast=None):
    names = {"uncached_input": "input", "cached_input": "cache read", "cache_write_5m": "5m cache write",
             "cache_write_1h": "1h cache write", "cache_write": "cache write", "output": "output"}
    text = ", ".join("%s %s" % (names[c], r) for c, r in zip(pricing._CATEGORIES, rates) if r is not None)
    return text + " USD per million tokens" + ("; fast mode x%s" % fast if fast else "")


def _price_check(root, args):
    gate = "GATE alpaca-analytics-price-check"
    found = None
    if args.models:
        targets = [(model, "anthropic" if ratecards.anthropic_name(model) else None) for model in args.models]
    else:
        found = ratecards.gaps(root)
        targets = [(gap["model"], gap["provider"]) for gap in found]
        if not targets:
            if not args.dry_run:
                ratecards.write_gaps(root, [], scope="all")
            print("%s: PASS (no pricing gaps: every model that answered has a rate card)" % gate)
            return cli.PASS
    page, page_error = None, None
    done, failed, labelled = [], [], []
    for model, provider in targets:
        if model in pricing._CARDS:
            print("%s: refused: it has a built-in rate card in alpaca/analytics/pricing.py; nothing to record" % model)
            failed.append("%s (built-in card)" % model)
            continue
        if provider != "anthropic" or not ratecards.anthropic_name(model):
            print("%s: not labelled: no official page parser for provider %s. Read the provider's official "
                  "price page and run:\n  %s" % (model, provider or "unknown", ratecards.fix_command(model, provider)))
            failed.append("%s (needs price-label)" % model)
            continue
        if page is None and page_error is None:
            try:
                page = ratecards.fetch_page(root, args.url, save=not args.dry_run)
            except (OSError, ValueError, http.client.HTTPException) as exc:
                page_error = "cannot read %s: %s" % (args.url, exc)
        if page_error is not None:
            print("%s: not labelled: %s" % (model, page_error))
            failed.append("%s (page unreadable)" % model)
            continue
        try:
            pieces = ratecards.fetch_anthropic(root, model, page=page)
            out = ratecards.record_pieces(root, pieces, dry_run=args.dry_run)
        except ValueError as exc:
            print("%s: not labelled: %s" % (model, exc))
            failed.append("%s (refused)" % model)
            continue
        rates = _rates_text(pieces["rates"], pieces["fast_multiplier"])
        if out["status"] == "recorded":
            labelled.append(model)
            print("%s: recorded (event %s): %s; row %r; page copy %s"
                  % (model, out["event_id"], rates, pieces["label"]["row"], pieces["label"]["source_copy"]))
        elif out["status"] == "duplicate":
            print("%s: duplicate: the latest recorded card is identical; nothing recorded" % model)
        else:
            print("%s: would record %s from %s (dry run, nothing written)" % (model, rates, page["url"]))
        done.append(model)
    if not args.dry_run:
        if found is not None:
            # A whole-project scan owns the "all" scope of the notice.
            ratecards.write_gaps(root, [gap for gap in found if gap["model"] not in labelled], scope="all")
        if labelled:
            ratecards.refresh_gaps(root)
    if failed:
        print("%s: FAIL (%d of %d model(s) not recorded: %s)" % (gate, len(failed), len(targets), ", ".join(failed)))
        return cli.FAIL
    print("%s: PASS (%d model(s) checked%s)" % (gate, len(done), ", dry run" if args.dry_run else ""))
    return cli.PASS


def _price_label(root, args):
    gate = "GATE alpaca-analytics-price-label"
    rates = {"uncached_input": args.input, "cached_input": args.cache_read, "output": args.output}
    if args.provider == "anthropic":
        if args.cache_write is not None:
            print("%s: FAIL (--cache-write is for openai cards; anthropic cards take --cache-write-5m and "
                  "--cache-write-1h)" % gate)
            return cli.FAIL
        rates.update(cache_write_5m=args.cache_write_5m, cache_write_1h=args.cache_write_1h)
    else:
        if args.cache_write_5m is not None or args.cache_write_1h is not None:
            print("%s: FAIL (--cache-write-5m and --cache-write-1h are for anthropic cards; openai cards take "
                  "--cache-write)" % gate)
            return cli.FAIL
        rates["cache_write"] = args.cache_write
    try:
        out = ratecards.record(root, args.model, provider=args.provider, rates=rates, source_url=args.source,
                               method="agent-entered", fast_multiplier=args.fast_multiplier)
    except ValueError as exc:
        print("%s: FAIL (%s)" % (gate, exc))
        return cli.FAIL
    card = out["card"]
    if out["status"] == "recorded":
        print("%s: recorded (event %s): %s; source %s" % (args.model, out["event_id"],
              _rates_text(card["rates"], card["fast_multiplier"]), card["source_url"]))
        ratecards.refresh_gaps(root)
    else:
        print("%s: duplicate: the latest recorded card is identical; nothing recorded" % args.model)
    print("%s: PASS (%s)" % (gate, out["status"]))
    return cli.PASS


def _selftest(root, args):
    from alpaca.analytics import selftest
    report = selftest.run(root, online=args.online, label_gaps=args.label_gaps)
    path = selftest.save(root, report)
    if args.json:
        print(json.dumps(report, ensure_ascii=True, indent=2))
    else:
        print("alpaca analytics selftest: %s" % report["status"])
        for check in report["checks"]:
            print("  %s: %s (%s)" % (check["name"], check["status"], check["detail"]))
            if check["name"] == "coverage":
                for item in check["items"]:
                    if item.get("kind") == "model-without-card":
                        print("    fix: %s" % item["fix"])
        print("report: %s" % path)
    summary = report["summary"]
    verdict = "FAIL" if summary["fail"] else "PASS"
    print("GATE alpaca-analytics-selftest: %s (%d pass, %d warn, %d fail, %d skip; report %s)"
          % (verdict, summary["pass"], summary["warn"], summary["fail"], summary["skip"], path),
          file=sys.stderr if args.json else sys.stdout)
    return cli.FAIL if summary["fail"] else cli.PASS

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
    if args.an_verb == "price-check":
        return _price_check(root, args)
    if args.an_verb == "price-label":
        return _price_label(root, args)
    if args.an_verb == "selftest":
        return _selftest(root, args)
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
    pc = av.add_parser("price-check", help="find models without a rate card, read their price from the "
                                           "official Claude price page and record it")
    pc.add_argument("--model", dest="models", action="append", default=None,
                    help="model ID to check (repeatable); default: every model that answered without a card")
    pc.add_argument("--dry-run", action="store_true", help="fetch and parse the page, record nothing")
    pc.add_argument("--url", default=ratecards.ANTHROPIC_PRICING_MD,
                    help="official Claude price page (markdown); must be https on %s, and so must any "
                         "redirect; for any other source read the page yourself and use price-label"
                         % ratecards.OFFICIAL_HOST)
    pl = av.add_parser("price-label", help="record a rate card you read yourself from the provider's official "
                                           "price page (USD per million tokens)")
    pl.add_argument("--model", required=True)
    pl.add_argument("--provider", required=True, choices=("anthropic", "openai"))
    pl.add_argument("--source", required=True, help="the official https page the prices come from")
    pl.add_argument("--input", required=True, help="uncached input rate")
    pl.add_argument("--output", required=True, help="output rate")
    pl.add_argument("--cache-read", required=True, help="cache read (cache hit) rate")
    pl.add_argument("--cache-write-5m", default=None, help="anthropic: 5 minute cache write rate")
    pl.add_argument("--cache-write-1h", default=None, help="anthropic: 1 hour cache write rate")
    pl.add_argument("--cache-write", default=None, help="openai: cache write rate")
    pl.add_argument("--fast-multiplier", default=None, help="fast mode price multiplier, when the page lists one")
    st = av.add_parser("selftest", help="check cost arithmetic, pricing coverage and client cost bounds")
    st.add_argument("--online", action="store_true", help="also compare every Claude card with the official page")
    st.add_argument("--label-gaps", action="store_true",
                    help="first record a card from the official page for every Claude model without one")
    st.add_argument("--json", action="store_true", help="print the whole report as JSON")
    r = sub.add_parser("recall"); r.add_argument("sid")

cli.register_parser("analytics", _parser)
