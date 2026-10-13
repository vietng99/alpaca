"""alpaca: the one writer of the record. Exit codes: 0 PASS, 1 FAIL, 2 BLOCKED, 64 usage, 67 internal."""
import argparse, json, os, sys
from alpaca import VERSION, db, paths, util

PASS, FAIL, BLOCKED, USAGE, INTERNAL = 0, 1, 2, 64, 67
COMMANDS = {}

#: where a verb finds its session id when the caller passed no --session, in order. The first is
#: the one the SessionStart hook exports; the second is what Claude Code puts in every Bash call.
#: Without them a Claude-run verb lands under the fake session `cli` and the record cannot say who
#: moved the task.
ENV_SESSION_VARS = ("ALPACA_SESSION_ID", "CLAUDE_CODE_SESSION_ID")

# M4.12: the verb modules whose import registers the rest of the dispatch table. main() imports
# these (reporting a shortfall) and surface.lint reads the resulting table via registered_verbs.
# A domain profile adds its own verb modules (alpaca/profile.py `verbs`), see _profile_verb_modules.
VERB_MODULES = ('ops', 'onboard', 'new', 'doctor', 'analytics_cli', 'serve', 'questions', 'board', 'messages', 'decisions', 'sort', 'export', 'review', 'upgrade', 'barrier', 'operator', 'proof', 'observability.cli', 'artifacts', 'backup', 'workspace', 'spec_kits', 'runbook', 'intake', 'start', 'note', 'interview', 'hub_publish', 'mission_cli', 'release.cli', 'research.cli', 'ownerword', 'brief', 'orders', 'selfcheck', 'oob', 'batch', 'watchdog.cli', 'aar')


def _profile_verb_modules():
    """The dotted modules of the current project's profile verbs. Empty without a profile."""
    from alpaca import profile
    try:
        verbs = profile.current().verbs()
    except Exception:
        return []
    if not isinstance(verbs, dict):
        return []
    return [spec["module"] for spec in verbs.values()
            if isinstance(spec, dict) and isinstance(spec.get("module"), str)]

def command(name):
    def deco(fn):
        COMMANDS[name] = fn
        return fn
    return deco

def _root():
    return paths.root()

@command("init")
def cmd_init(args):
    root = _root()
    from alpaca import project
    project.ensure_instance(root)
    conn = db.connect(root)
    if db.meta_get(conn, "initialized") is None:
        db.append_event(conn, session=args.session or "cli", actor="alpaca", kind="init",
                        data={"version": VERSION})
        db.meta_set(conn, "initialized", util.now_iso())
    print("alpaca init: ok (%s)" % paths.runtime_dir(root), file=sys.stderr)
    return PASS

@command("verify")
def cmd_verify(args):
    conn = db.connect(_root())
    ok, reason = db.verify_chain(conn)
    print("GATE alpaca-verify: %s (%s)" % ("PASS" if ok else "FAIL", reason))
    return PASS if ok else FAIL

def status_dict(root):
    conn = db.connect(root)
    n = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    last = db.last_event(conn)
    from alpaca import ops, version
    v = version.info()
    d = {"root": root, "version": v["version"], "channel": v["channel"], "events": n,
         "last_event": {"ts": last["ts"], "kind": last["kind"]} if last else None,
         "onboarded": db.meta_get(conn, "onboarded") is not None,
         "sessions": conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0],
         "ops_open": conn.execute("SELECT COUNT(*) FROM ops WHERE status='open'").fetchone()[0],
         "tasks_open": conn.execute("SELECT COUNT(*) FROM tasks WHERE status IN ('open','doing')").fetchone()[0],
         "not_now": ops.not_now(conn)}
    # M2.7: the op index (states + cursors) rides on alpaca status --json for the dashboard/page.
    try:
        from alpaca import opindex
        d["op_index"] = opindex.status_index(conn)
    except Exception:
        pass
    return d

@command("status")
def cmd_status(args):
    root = _root()
    d = status_dict(root)
    if args.json:
        print(json.dumps(d, indent=1))
    else:
        from alpaca import pad
        print(pad.render(root))
        pad.write(root)
    return PASS

@command("phase")
def cmd_phase(args):
    """M1.15: enter a phase by level, or adjudicate a phase boundary door.

    `alpaca phase enter <op> <phase> --level Ln` is the level comparison that replaces the earlier harness
    grant check. `alpaca phase gate <op> <boundary> --level Ln [--go] [--force]` runs the composed
    door and prints its verdict; PAUSED (exit 3) is the low-level human-go outcome."""
    from alpaca.checklist import Halt
    from alpaca.gates import verdict as vc
    from alpaca.phase import doors, phase_gate
    conn = db.connect(_root()); sid = args.session or "cli"
    if args.phase_verb == "enter":
        try:
            res = phase_gate.enter(conn, args.op, args.phase, args.level, session=sid)
        except Halt as h:
            print("GATE alpaca-phase-enter: %s (%s: %s)" % (vc.name_of(h.verdict), h.code, h.detail))
            return h.verdict
        print("phase %s enter: allowed (%s)" % (args.phase, res["reason"]))
        return PASS
    if args.phase_verb == "gate":
        try:
            code = doors.run(conn, args.op, args.boundary, level=args.level, force=args.force,
                             human_go=args.go, session=sid)
        except Halt as h:
            print("GATE alpaca-phase-gate: %s (%s: %s)" % (vc.name_of(h.verdict), h.code, h.detail))
            return h.verdict
        print(vc.gate_line("alpaca-phase-gate:%s" % args.boundary, code))
        return code
    print("GATE alpaca-phase: BLOCKED (unknown phase verb; use enter or gate)")
    return BLOCKED

_WIKI_FILTER_FLAGS = (("op", "--op"), ("task", "--task"), ("operator", "--operator"),
                      ("session", "--filter-session"), ("source_kind", "--source-kind"),
                      ("fact_kind", "--fact-kind"))


def _wiki_filters(args):
    """The FR-11 structured filters named on a `wiki query` or `wiki context` line, or None."""
    from alpaca.wiki.api import Filters
    chosen = {name: getattr(args, "filter_" + name) for name, _ in _WIKI_FILTER_FLAGS
              if getattr(args, "filter_" + name, None) is not None}
    if getattr(args, "include_bookkeeping", False):
        chosen["include_bookkeeping"] = True
    return Filters(**chosen) if chosen else None


def _wiki_record_appender(session):
    """The `RecordAppender` of alpaca/wiki/api.py: appends one record event as actor `alpaca-wiki`
    in the caller's session and returns its id. Rune2 calls it where architecture.md 10.2 says and
    never writes the record itself."""
    def append(kind, data):
        conn = db.connect(_root())
        try:
            return db.append_event(conn, session=session, actor="alpaca-wiki", kind=kind,
                                   data=data)["id"]
        finally:
            conn.close()
    return append


@command("wiki")
def cmd_wiki(args):
    """M2.15: `alpaca wiki extract <root> <out>` emits the project wiki as a portable Rune-2 vault.

    Direction (one way): the extract LEAVES alpaca. The emitted vault is a standalone read-only copy
    with its own schema and its own ledger; nothing outside alpaca writes back into the project record.
    Every non-public row is excluded at the data layer (M2.12) and a page whose structural edge did
    not survive the emit is listed UNACCOUNTED in the manifest, never dropped silently."""
    from alpaca.gates import verdict as vc
    if args.wiki_verb in ("query", "context", "status", "ingest"):
        from alpaca.wiki import service
        try:
            refresh_report = None
            if args.wiki_verb == "status":
                result = service.status(_root())
            elif args.wiki_verb == "ingest":
                result = service.ingest(_root(), args.source, doc_id=args.doc_id, session=args.session)
            else:
                if args.refresh:
                    from alpaca.wiki import recovery
                    refresh_report = recovery.run(_root(), session=args.session or 'cli')
                filters = _wiki_filters(args)
                if args.wiki_verb == "context":
                    print(service.context(_root(), args.question, max_chars=args.max_chars,
                                          filters=filters))
                    return vc.BLOCKED if refresh_report and refresh_report['status'] != 'ok' else vc.PASS
                result = service.query(_root(), args.question, as_of=args.as_of,
                                       knowledge_as_of=args.knowledge_as_of, filters=filters)
            if refresh_report is not None:
                result['refresh'] = refresh_report
            print(json.dumps(result, indent=2))
            return vc.BLOCKED if refresh_report and refresh_report['status'] != 'ok' else vc.PASS
        except Exception as exc:
            print(json.dumps({'error': f'{type(exc).__name__}: {exc}'}), file=sys.stderr)
            return vc.FAIL
    if args.wiki_verb == "briefing":
        # FR-19: built on demand from Rune2 evidence and never stored; it writes nothing
        from alpaca.wiki import service
        try:
            result = service.briefing(_root(), task=args.task, op=args.op,
                                      max_chars=args.max_chars)
        except Exception as exc:
            print(json.dumps({'error': f'{type(exc).__name__}: {exc}'}), file=sys.stderr)
            return vc.FAIL
        print(json.dumps(result.to_dict(), indent=2) if args.json else result.text)
        return vc.PASS
    if args.wiki_verb == "record-facts":
        if not args.dry_run:
            print(json.dumps({'error': 'wiki record-facts runs only as --dry-run; the rules write '
                              'when the drain captures an event note'}), file=sys.stderr)
            return vc.BLOCKED
        import dataclasses
        from alpaca.wiki import service
        try:
            rows = service.record_facts_preview(_root(), from_event=args.from_event,
                                                through_event=args.through)
        except service.StoreBehindRecord as held:
            # the store has not processed a removal act of the record (t-070 round 3, R2-B1)
            print(json.dumps(dict(held.detail, outcome='unavailable'), indent=2))
            return vc.BLOCKED
        except Exception as exc:
            print(json.dumps({'error': f'{type(exc).__name__}: {exc}'}), file=sys.stderr)
            return vc.FAIL
        print(json.dumps([dataclasses.asdict(r) for r in rows], indent=2))
        return vc.PASS
    if args.wiki_verb in ("admit", "withdraw-candidate"):
        from alpaca.wiki import service
        try:
            if not args.session:
                raise ValueError("wiki %s needs --session <sid>: the actor is the live session" % args.wiki_verb)
            appender = _wiki_record_appender(args.session)
            if args.wiki_verb == "admit":
                receipt = service.admit(_root(), args.candidate_id, session=args.session,
                                        record=appender, probe=args.probe)
            else:
                receipt = service.withdraw_candidate(_root(), args.candidate_id, session=args.session,
                                                     record=appender, reason=args.reason, by=args.by)
            print(json.dumps(receipt.to_dict(), indent=2))
            return vc.PASS if receipt.status in ("applied", "duplicate") else vc.BLOCKED
        except Exception as exc:
            print(json.dumps({'error': f'{type(exc).__name__}: {exc}'}), file=sys.stderr)
            return vc.FAIL
    if args.wiki_verb in ("correct", "withdraw"):
        from alpaca.wiki import service
        try:
            if not args.session:
                raise ValueError("wiki %s needs --session <sid>: the actor is the live session" % args.wiki_verb)
            appender = _wiki_record_appender(args.session)
            if args.wiki_verb == "withdraw":
                receipt = service.withdraw_source(_root(), args.source_ref, session=args.session,
                                                  record=appender, reason=args.reason)
            else:
                target = args.target
                if target.endswith(".json"):
                    with open(target, encoding="utf-8") as fh:
                        target = json.load(fh)
                replacement = None
                if args.obj is not None or args.replacement_file:
                    if args.replacement_file:
                        with open(args.replacement_file, encoding="utf-8") as fh:
                            replacement = json.load(fh)
                    else:
                        replacement = {"obj": args.obj, "obj_datatype": args.obj_datatype}
                        for key in ("subject", "predicate"):
                            if getattr(args, key):
                                replacement[key] = getattr(args, key)
                receipt = service.correct(_root(), target, args.action, session=args.session,
                                          record=appender, reason=args.reason, replacement=replacement,
                                          effective_at=args.effective_at, by=args.by)
            print(json.dumps(receipt.to_dict(), indent=2))
            return vc.PASS if receipt.status in ("applied", "duplicate") else vc.BLOCKED
        except Exception as exc:
            print(json.dumps({'error': f'{type(exc).__name__}: {exc}'}), file=sys.stderr)
            return vc.FAIL
    if args.wiki_verb == "consolidate":
        from alpaca.wiki import consolidate
        try:
            # the caller of the job writes its checkpoint and one failure event per tried item
            # that failed (architecture.md 9.2, OB-23); the step itself only proposes
            result = consolidate.run_step(_root(), deadline_s=args.deadline_s)
            print(json.dumps(result.to_dict(), indent=2))
            return vc.PASS
        except Exception as exc:
            print(json.dumps({'error': f'{type(exc).__name__}: {exc}'}), file=sys.stderr)
            return vc.FAIL
    if args.wiki_verb in ("propose", "candidates"):
        from alpaca.wiki import service
        try:
            if args.wiki_verb == "candidates":
                try:
                    views = service.candidates(_root(), status=args.status,
                                               session=args.filter_session, kind=args.kind)
                except service.StoreBehindRecord as held:
                    print(json.dumps(dict(held.detail, outcome='unavailable'), indent=2))
                    return vc.BLOCKED
                print(json.dumps([v.to_dict() for v in views], indent=2))
                return vc.PASS
            if not args.session:
                raise ValueError("wiki propose needs --session <sid>: the proposer is the live session")
            with open(args.file, encoding="utf-8") as fh:
                proposal = json.load(fh)
            receipt = service.propose(_root(), proposal, session=args.session,
                                      record=_wiki_record_appender(args.session))
            print(json.dumps(receipt.to_dict(), indent=2))
            return vc.PASS
        except Exception as exc:
            print(json.dumps({'error': f'{type(exc).__name__}: {exc}'}), file=sys.stderr)
            return vc.FAIL
    if args.wiki_verb == "recover":
        from alpaca.wiki import recovery
        def progress(done, total, report):
            if done % 100 == 0 or done == total:
                print("wiki recovery: %d/%d sessions, %d ingested, %d unchanged" %
                      (done, total, report['ingested'], report['skipped']), file=sys.stderr)
        try:
            result = recovery.run(_root(), session=args.session or 'cli', progress=progress,
                                  rerender_notes=args.rerender_notes)
        except Exception as exc:
            # the error text may quote what failed: the one screen before the record (t-070 round 2 C-C)
            from alpaca import capture_runner
            error = capture_runner._describe(exc)
            conn = db.connect(_root())
            try:
                db.append_event(conn, session=args.session or 'cli', actor='alpaca-wiki',
                                kind='drain-failed', data={'where': 'wiki-recover',
                                'error': error, 'recovery': 'alpaca wiki recover'})
            finally:
                conn.close()
            return vc.emit_verdict('alpaca-wiki-recover', vc.FAIL, error)
        print(json.dumps(result, indent=2))
        return vc.PASS if result['status'] == 'ok' else vc.BLOCKED
    if args.wiki_verb in ("export", "import"):
        # FR-23: the rune2-portable/1 export reads the store; the import pours an empty store from
        # the export and the record. Neither writes the record.
        from alpaca.wiki import portable, service
        try:
            if args.wiki_verb == "export":
                result = portable.export_store(_root(), args.path, include_private=args.private)
            else:
                result = portable.import_store(_root(), args.path)
        except service.StoreBehindRecord as held:
            # the store has not processed a removal act of the record (t-070 round 2 C-E)
            print(json.dumps(dict(held.detail, outcome='unavailable'), indent=2))
            return vc.BLOCKED
        except Exception as exc:
            print(json.dumps({'error': f'{type(exc).__name__}: {exc}'}), file=sys.stderr)
            return vc.FAIL
        print(json.dumps(result, indent=2))
        return vc.BLOCKED if result.get('status') in ('refused', 'incomplete') else vc.PASS
    if args.wiki_verb == "extract":
        from alpaca.wiki import extract, service
        try:
            manifest = extract.emit(args.root, args.out)
        except service.StoreBehindRecord as held:
            return vc.emit_verdict("alpaca-wiki-extract", vc.BLOCKED,
                                   "unavailable: %s" % held.detail['reason'])
        except Exception as e:
            from alpaca import capture_runner
            return vc.emit_verdict("alpaca-wiki-extract", vc.FAIL, capture_runner._describe(e))
        c = manifest["counts"]; p = manifest["parity"]
        return vc.emit_verdict(
            "alpaca-wiki-extract", vc.PASS,
            "portable vault emitted (leaves alpaca; nothing outside writes back): "
            "docs=%d nodes=%d edges=%d unaccounted=%d" % (
                c["docs"], c["nodes"], c["edges"], p["unaccounted"]),
            evidence=[args.out])
    print("GATE alpaca-wiki: BLOCKED (unknown wiki verb; use extract, recover, record-facts, briefing, propose, candidates or consolidate)")
    return vc.BLOCKED

@command("style")
def cmd_style(args):
    """M3.11: the plain-writing ban list as a shipped mechanism.

    `ban`/`unban` edit the user list (style/banned.txt, shipped empty). `use <preset>` seeds the
    project's style/presets/<preset>-ban-list.md from the canonical shipped source (the vendored
    `sam` skill where it overlaps the preset) and records the preset in project.yaml, additively.
    `humanize` runs the ONE lint (alpaca.gates.literal_guard.lint) over text and reports the matches
    with a rewrite instruction: FAIL when a banned tier fires, PASS when the text is clean."""
    import os
    from alpaca import project as proj
    from alpaca.gates import literal_guard as lg
    root = _root()
    verb = getattr(args, "style_verb", None)
    banned = os.path.join(root, "style", "banned.txt")
    if verb in ("ban", "unban"):
        os.makedirs(os.path.dirname(banned), exist_ok=True)
        lines = []
        if os.path.isfile(banned):
            with open(banned, encoding="utf-8") as fh:
                lines = fh.read().splitlines()
        term = args.term.strip()
        kept = [l for l in lines if l.strip().lower() != term.lower()]
        if verb == "ban":
            kept.append(term)
            msg = "banned %r" % term
        else:
            if len(kept) == len(lines):
                print("alpaca style unban: %r was not in the list" % term, file=sys.stderr)
            msg = "unbanned %r" % term
        with open(banned, "w", encoding="utf-8") as fh:
            fh.write("\n".join([l for l in kept if l.strip()]) + ("\n" if kept else ""))
        print("alpaca style: %s (%s)" % (msg, banned), file=sys.stderr)
        return PASS
    if verb == "use":
        preset = args.preset.strip()
        dst = os.path.join(root, "style", "presets", "%s-ban-list.md" % preset)
        if not os.path.isfile(dst):
            src = lg.shipped_preset_path(preset)
            if not os.path.isfile(src):
                print("GATE alpaca-style: BLOCKED (no shipped source for preset %r)" % preset,
                      file=sys.stderr)
                return BLOCKED
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            with open(src, encoding="utf-8") as fh:
                body = fh.read()
            with open(dst, "w", encoding="utf-8") as fh:
                fh.write(body)
        cfg = proj.load(root)
        style = dict(cfg.get("style", {}) or {})
        presets = list(style.get("presets", []) or [])
        if preset not in presets:
            presets.append(preset)
        style["presets"] = presets
        cfg["style"] = style
        proj.save(root, cfg)
        print("alpaca style: using preset %r (%s)" % (preset, dst), file=sys.stderr)
        return PASS
    if verb == "humanize":
        text = args.text if args.text is not None else sys.stdin.read()
        matches = lg.lint(text, lg.load_rules(root))
        if not matches:
            print("alpaca style humanize: clean (no banned wording)")
            return PASS
        print(lg.rewrite_instruction(matches))
        for m in matches:
            print("  %-12s %s" % (m.tier, m.matched))
        return FAIL
    print("GATE alpaca-style: BLOCKED (unknown style verb; use ban|unban|use|humanize)")
    return BLOCKED

@command("day")
def cmd_day(args):
    """M4.6: `alpaca day` regenerates a per-project daily digest - a VIEW over the record, never a
    source. Byte-identical under a fixed clock (the historian reads the record's own stamps, not
    the wall clock, and writes nothing). Reports the day's op activity and the never-drop unsorted
    bucket: every note placed in no op timeline, surfaced by name with a reason."""
    from alpaca import historian
    from alpaca.gates import verdict as vc
    conn = db.connect(_root())
    date = args.date or util.now_iso()[:10]
    digest = historian.day(conn, date)
    if args.json:
        print(json.dumps(digest, indent=1))
    else:
        print(historian.render_day(digest), end="")
    t = digest["totals"]
    return vc.emit_verdict("alpaca-day", vc.PASS, "%s: %d op(s), %d event(s), %d unsorted" % (
        date, t["ops"], t["events"], t["unsorted"]))

def build_parser():
    p = argparse.ArgumentParser(prog="alpaca", add_help=True)
    p.add_argument("--session", default=None, help="session id (hooks pass it)")
    p.add_argument("--version", action="store_true", help="print the Alpaca version and exit")
    sub = p.add_subparsers(dest="verb")
    sub.add_parser("init"); sub.add_parser("verify")
    s = sub.add_parser("status"); s.add_argument("--json", action="store_true")
    # M4.6: alpaca day - the per-project daily digest, a regenerable view over the record.
    dy = sub.add_parser("day", help="regenerate the per-project daily digest (a view, never a source)")
    dy.add_argument("--date", default=None, help="the YYYY-MM-DD to digest (default: today)")
    dy.add_argument("--json", action="store_true")
    wk = sub.add_parser("wiki", help="wiki engine verbs (the extract leaves alpaca; nothing writes back)")
    wkv = wk.add_subparsers(dest="wiki_verb")
    wr = wkv.add_parser("recover", help="replay all recorded sessions and verify wiki capture through a fixed cutoff")
    # Merge of op-008 into main, review B4: stored event notes keep their rendering unless the
    # owner runs this job (docs/operators.md); --refresh on query/context never passes it.
    wr.add_argument("--rerender-notes", action="store_true",
                    help="owner job: rewrite stored event notes in place in the current note format "
                         "(slow on a large store; time it on a copy first)")
    wkv.add_parser("status", help="show corpus, providers and capture gaps separately from decision pages")
    wq = wkv.add_parser("query", help="ask the wiki for a cited evidence-only answer (JSON)")
    wq.add_argument("question")
    wq.add_argument("--as-of", default=None, help="valid-time snapshot")
    wq.add_argument("--knowledge-as-of", default=None, help="recorded-time snapshot")
    wq.add_argument("--refresh", action="store_true", help="recover recorded sources before answering")
    wc = wkv.add_parser("context", help="retrieve bounded evidence for a task or resume")
    wc.add_argument("question")
    wc.add_argument("--max-chars", type=int, default=2500)
    wc.add_argument("--refresh", action="store_true")
    for sub_parser in (wq, wc):
        # FR-11 structured filters: they narrow retrieval and never authorize
        for name, flag in _WIKI_FILTER_FLAGS:
            sub_parser.add_argument(flag, dest="filter_" + name, default=None,
                                    help="keep only %s matching this value" % name.replace("_", " "))
        sub_parser.add_argument("--include-bookkeeping", action="store_true",
                                help="read heartbeat notes too (left out by default)")
    wbr = wkv.add_parser("briefing", help="FR-19: a cited, budgeted briefing of one task or op, "
                                          "built on demand from Rune2 evidence (evidence-only; "
                                          "never stored)")
    wbr_subject = wbr.add_mutually_exclusive_group(required=True)
    wbr_subject.add_argument("--task", default=None, help="the task id (t-NNN)")
    wbr_subject.add_argument("--op", default=None, help="the op id (op-NNN)")
    wbr.add_argument("--max-chars", type=int, default=2500)
    wbr.add_argument("--json", action="store_true",
                     help="the items, their sources and the text as JSON")
    wrf = wkv.add_parser("record-facts", help="FR-14: show what the record rules would extract from "
                                              "an event range, writing nothing (--dry-run)")
    wrf.add_argument("--dry-run", action="store_true", help="required: the verb only previews")
    wrf.add_argument("--from", dest="from_event", type=int, required=True, help="first event id")
    wrf.add_argument("--through", type=int, required=True, help="last event id")
    wp = wkv.add_parser("propose", help="FR-33: propose a fact, observation or summary candidate "
                                        "from a JSON file; each anchor is checked against its cited "
                                        "span (the proposer is --session)")
    wp.add_argument("--file", required=True, help="the proposal JSON (kind, anchors, context, ...)")
    wco = wkv.add_parser("consolidate", help="FR-16, FR-18: one bounded, model-free step that turns "
                                             "repeated failure signatures and superseded decisions "
                                             "into observation candidates (proposes; never admits)")
    wco.add_argument("--deadline-s", dest="deadline_s", type=float, default=20.0,
                     help="seconds the step may run before it stops and resumes from its checkpoint")
    wcd = wkv.add_parser("candidates", help="list candidates with their anchors (quotes the "
                                            "servable predicate hides are null)")
    wcd.add_argument("--status", default=None)
    wcd.add_argument("--kind", default=None)
    wcd.add_argument("--filter-session", dest="filter_session", default=None,
                     help="only candidates this session proposed")
    wad = wkv.add_parser("admit", help="FR-33: admit a checked fact candidate as a graph edge; the "
                                       "admitting session (--session) must differ from the proposer")
    wad.add_argument("candidate_id")
    wad.add_argument("--probe", default=None, help="probe corpus for an observation (not read for a fact)")
    wwd = wkv.add_parser("withdraw-candidate", help="withdraw a candidate (the proposer or the owner); an "
                                                     "admitted one is retracted and only its net effect is undone")
    wwd.add_argument("candidate_id")
    wwd.add_argument("--reason", required=True)
    wwd.add_argument("--by", default=None, choices=["owner"], help="declare the owner (not verified)")
    wcr = wkv.add_parser("correct", help="FR-21: the owner corrects a fact (supersede, world-change, "
                                         "retract or revert); anyone else is refused and should propose")
    wcr.add_argument("target", help="an edge id, or the path of a .json file holding a FactIdentity")
    wcr.add_argument("--action", required=True, choices=["supersede", "world-change", "retract", "revert"])
    wcr.add_argument("--reason", required=True)
    wcr.add_argument("--by", default=None, choices=["owner"], help="declare the owner (not verified)")
    wcr.add_argument("--effective-at", dest="effective_at", default=None,
                     help="when the world changed (required for world-change)")
    wcr.add_argument("--obj", default=None, help="the replacement object (supersede, world-change)")
    wcr.add_argument("--obj-datatype", dest="obj_datatype", default="string",
                     choices=["node", "date", "number", "string", "bool"])
    wcr.add_argument("--subject", default=None, help="replacement subject (default: the target's)")
    wcr.add_argument("--predicate", default=None, help="replacement predicate (default: the target's)")
    wcr.add_argument("--replacement-file", dest="replacement_file", default=None,
                     help="a JSON file with the replacement (obj, obj_datatype, subject, predicate)")
    wwsr = wkv.add_parser("withdraw", help="FR-22: withdraw a source and everything derived from it "
                                           "(an exclusion, not a purge)")
    wwsr.add_argument("source_ref", help="event:<id>, transcript:<session>, document:<doc-id> (or "
                                         "wiki:<doc-id>), ...; the document of a captured note, "
                                         "transcript or source copy withdraws the source it stands "
                                         "for, which the receipt names")
    wwsr.add_argument("--reason", required=True)
    wxp = wkv.add_parser("export", help="FR-23: write the store's accepted state as rune2-portable/1 "
                                        "JSON lines (no text a source may not serve)")
    wxp.add_argument("path", help="the export file to write (mode 0600)")
    wxp.add_argument("--private", action="store_true",
                     help="keep the text of private and quarantined sources; withdrawn text never leaves")
    wim = wkv.add_parser("import", help="FR-23: pour an empty store from a rune2-portable/1 export and "
                                        "the record; rows the record does not back are refused")
    wim.add_argument("path", help="the export file to read")
    wi = wkv.add_parser("ingest", help="index a local wiki document as evidence")
    wi.add_argument("source")
    wi.add_argument("--doc-id", required=True, help="stable wiki/... .md identity")
    wex = wkv.add_parser("extract",
                         help="emit the project wiki as a portable Rune-2 vault; the extract "
                              "LEAVES alpaca and nothing outside alpaca writes back into the record")
    wex.add_argument("root", help="the wiki vault root to read (rune.db + ledger)")
    wex.add_argument("out", help="the output directory for the standalone portable vault")
    from alpaca.phase import defaults as _phdef
    ph = sub.add_parser("phase"); phv = ph.add_subparsers(dest="phase_verb")
    pe = phv.add_parser("enter"); pe.add_argument("op"); pe.add_argument("phase")
    pe.add_argument("--level", required=True)
    pg = phv.add_parser("gate"); pg.add_argument("op")
    pg.add_argument("boundary", choices=sorted(_phdef.BOUNDARY_PHASE))
    pg.add_argument("--level", required=True)
    pg.add_argument("--go", action="store_true")
    pg.add_argument("--force", action="store_true")
    # M3.11: alpaca style ban|unban|use|humanize -- one lint (alpaca.gates.literal_guard) behind all of it.
    st = sub.add_parser("style", help="the plain-writing ban list: ban/unban a term, use a preset, "
                                      "humanize (lint) text against the deterministic tiers")
    stv = st.add_subparsers(dest="style_verb")
    sb = stv.add_parser("ban"); sb.add_argument("term")
    su = stv.add_parser("unban"); su.add_argument("term")
    se = stv.add_parser("use"); se.add_argument("preset")
    sh = stv.add_parser("humanize")
    sh.add_argument("--text", default=None, help="text to lint (default: read stdin)")
    return p, sub

def main(argv=None) -> int:
    # `alpaca --version` answers before any verb module loads, so it works in a broken tree too.
    if list(sys.argv[1:] if argv is None else argv) == ["--version"]:
        from alpaca import version
        print("Alpaca %s" % version.string())
        return PASS
    parser, sub = build_parser()
    for mod in VERB_MODULES:
        try:
            __import__("alpaca.%s" % mod)
        except ImportError as e:
            print("alpaca: verb module alpaca.%s unavailable (%s: %s)" % (mod, type(e).__name__, e), file=sys.stderr)
    for mod in _profile_verb_modules():
        try:
            __import__(mod)
        except Exception as e:
            print("alpaca: profile verb module %s unavailable (%s: %s)" % (mod, type(e).__name__, e), file=sys.stderr)
    # M2.4: the messages module extends `alpaca msg` additively over the M0 handler in ops.py. The
    # `msg` subparser stays ops-owned; bind the extended handler last so it wins regardless of
    # module import order (the decorator alone would be order-dependent).
    try:
        from alpaca import messages as _messages
        COMMANDS["msg"] = _messages.cmd_msg
    except ImportError:
        pass
    for name, reg in _EXTRA_PARSERS:
        reg(sub)
    # M4.2: a crashed upgrade is recovered on the next start, not on demand, so it can never leave
    # a half tree behind a working CLI. Fail-open: recovery never blocks an ordinary command.
    try:
        from alpaca import upgrade
        upgrade.recover(paths.root())
    except Exception:
        pass
    try:
        args = parser.parse_args(argv)
    except SystemExit as e:
        return USAGE if e.code not in (0,) else PASS
    if getattr(args, "version", False) and not args.verb:
        from alpaca import version
        print("Alpaca %s" % version.string())
        return PASS
    if not args.verb or args.verb not in COMMANDS:
        parser.print_usage(sys.stderr)
        return USAGE
    # op-006: a verb run from inside a session, without --session, is THAT session's write and not
    # the `cli` writer's. The SessionStart hook exports ALPACA_SESSION_ID and Claude Code exports
    # CLAUDE_CODE_SESSION_ID into every Bash call, so read them in that order before dispatch;
    # every verb reads `args.session or "cli"`, so filling it here attributes all of them at once.
    # op-019: the real session and agent markers are read BEFORE --session overwrites
    # ALPACA_SESSION_ID below, so owner words are bound to the process's own session
    from alpaca import ownerword
    ownerword.capture(os.environ)
    if not getattr(args, "session", None):
        for name in ENV_SESSION_VARS:
            value = (os.environ.get(name) or "").strip()
            if value:
                args.session = value
                break
    previous_session = os.environ.get("ALPACA_SESSION_ID")
    if args.session:
        os.environ["ALPACA_SESSION_ID"] = args.session
    try:
        return COMMANDS[args.verb](args)
    except paths.RootNotFound as e:
        print("GATE alpaca: BLOCKED (%s)" % e)
        return BLOCKED
    except Exception as e:
        print("GATE alpaca-%s: FAIL (internal: %s: %s)" % (args.verb, type(e).__name__, e), file=sys.stderr)
        return INTERNAL
    finally:
        ownerword.release()
        if previous_session is None:
            os.environ.pop("ALPACA_SESSION_ID", None)
        else:
            os.environ["ALPACA_SESSION_ID"] = previous_session

_EXTRA_PARSERS = []   # (name, fn(subparsers)) appended by later modules

def register_parser(name, fn):
    _EXTRA_PARSERS.append((name, fn))


def registered_verbs():
    """M4.12: import every verb module and return the live dispatch table's verb names.

    surface.lint (M4.12) and boot-check (M4.15) check the declared surface against what `alpaca`
    actually dispatches, so the authoritative set is COMMANDS once every verb module has
    registered. Import failures are swallowed the way main() tolerates them; the lint then reports
    the shortfall as an unregistered front verb rather than crashing here. This is the same
    population main() performs, so the two never drift."""
    for mod in VERB_MODULES:
        try:
            __import__("alpaca.%s" % mod)
        except ImportError:
            pass
    for mod in _profile_verb_modules():
        try:
            __import__(mod)
        except Exception:
            pass
    try:
        from alpaca import messages as _messages
        COMMANDS["msg"] = _messages.cmd_msg
    except ImportError:
        pass
    return set(COMMANDS)
