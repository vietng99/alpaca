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
VERB_MODULES = ("ops", "onboard", "doctor", "analytics_cli", "serve", "questions", "board",
                "messages", "decisions", "sort", "export", "review", "upgrade", "barrier", "operator",
                "proof", "observability.cli", "artifacts", "backup", "workspace", "spec_kits",
                "runbook", "intake", "start", "note", "hub_publish")


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
    d = {"root": root, "version": VERSION, "events": n,
         "last_event": {"ts": last["ts"], "kind": last["kind"]} if last else None,
         "onboarded": db.meta_get(conn, "onboarded") is not None,
         "sessions": conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0],
         "ops_open": conn.execute("SELECT COUNT(*) FROM ops WHERE status='open'").fetchone()[0],
         "tasks_open": conn.execute("SELECT COUNT(*) FROM tasks WHERE status IN ('open','doing')").fetchone()[0]}
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

@command("wiki")
def cmd_wiki(args):
    """M2.15: `alpaca wiki extract <root> <out>` emits the project wiki as a portable Rune-2 vault.

    Direction (one way): the extract LEAVES alpaca. The emitted vault is a standalone read-only copy
    with its own schema and its own ledger; nothing outside alpaca writes back into the project record.
    Every non-public row is excluded at the data layer (M2.12) and a page whose structural edge did
    not survive the emit is listed UNACCOUNTED in the manifest, never dropped silently."""
    from alpaca.gates import verdict as vc
    if args.wiki_verb == "recover":
        from alpaca.wiki import recovery
        def progress(done, total, report):
            if done % 100 == 0 or done == total:
                print("wiki recovery: %d/%d sessions, %d ingested, %d unchanged" %
                      (done, total, report['ingested'], report['skipped']), file=sys.stderr)
        try:
            result = recovery.run(_root(), session=args.session or 'cli', progress=progress)
        except Exception as exc:
            conn = db.connect(_root())
            try:
                db.append_event(conn, session=args.session or 'cli', actor='alpaca-wiki',
                                kind='drain-failed', data={'where': 'wiki-recover',
                                'error': f'{type(exc).__name__}: {exc}',
                                'recovery': 'alpaca wiki recover'})
            finally:
                conn.close()
            return vc.emit_verdict('alpaca-wiki-recover', vc.FAIL, f'{type(exc).__name__}: {exc}')
        print(json.dumps(result, indent=2))
        return vc.PASS
    if args.wiki_verb == "extract":
        from alpaca.wiki import extract
        try:
            manifest = extract.emit(args.root, args.out)
        except Exception as e:
            return vc.emit_verdict("alpaca-wiki-extract", vc.FAIL,
                                   "%s: %s" % (type(e).__name__, e))
        c = manifest["counts"]; p = manifest["parity"]
        return vc.emit_verdict(
            "alpaca-wiki-extract", vc.PASS,
            "portable vault emitted (leaves alpaca; nothing outside writes back): "
            "docs=%d nodes=%d edges=%d unaccounted=%d" % (
                c["docs"], c["nodes"], c["edges"], p["unaccounted"]),
            evidence=[args.out])
    print("GATE alpaca-wiki: BLOCKED (unknown wiki verb; use extract or recover)")
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
    sub = p.add_subparsers(dest="verb")
    sub.add_parser("init"); sub.add_parser("verify")
    s = sub.add_parser("status"); s.add_argument("--json", action="store_true")
    # M4.6: alpaca day - the per-project daily digest, a regenerable view over the record.
    dy = sub.add_parser("day", help="regenerate the per-project daily digest (a view, never a source)")
    dy.add_argument("--date", default=None, help="the YYYY-MM-DD to digest (default: today)")
    dy.add_argument("--json", action="store_true")
    wk = sub.add_parser("wiki", help="wiki engine verbs (the extract leaves alpaca; nothing writes back)")
    wkv = wk.add_subparsers(dest="wiki_verb")
    wkv.add_parser("recover", help="replay all recorded sessions and verify wiki capture through a fixed cutoff")
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
    if not args.verb or args.verb not in COMMANDS:
        parser.print_usage(sys.stderr)
        return USAGE
    # op-006: a verb run from inside a session, without --session, is THAT session's write and not
    # the `cli` writer's. The SessionStart hook exports ALPACA_SESSION_ID and Claude Code exports
    # CLAUDE_CODE_SESSION_ID into every Bash call, so read them in that order before dispatch;
    # every verb reads `args.session or "cli"`, so filling it here attributes all of them at once.
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
