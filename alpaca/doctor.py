"""Read-only self-check. Exit 0 ok / 1 warn / 2 error."""
import json, os, stat, sys, tempfile
from alpaca import cli, db, paths, project, util

HOOK_EVENTS = ("SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop",
               "SubagentStop", "SessionEnd", "PreCompact")

# Generic read-only check modules. Each exposes checks(root, conn) -> [(name, level, detail)],
# the same shape the profile seam returns. A module that is absent contributes nothing.
CHECK_MODULES = (("transcripts", "transcript-snapshots"), ("proof", "proof-integrity"))

# No hook may declare a timeout above this ceiling: a hook that can hang for minutes stalls
# a session, so the registered timeout is the outer bound the fail-open plumbing runs under.
HOOK_TIMEOUT_CEILING = 60

_LEVELS = ("ok", "warn", "error")
_GATE_WORD = ("PASS", "WARN", "FAIL")
_EXIT_CODE = (cli.PASS, cli.FAIL, cli.BLOCKED)


def _read_text(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _read_json(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _manifest_paths(root):
    section = None
    out = {"mechanism": [], "memory": []}
    for line in _read_text(os.path.join(root, paths.MANIFEST)).splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line in ("[mechanism]", "[memory]"):
            section = line[1:-1]
            continue
        if section:
            out[section].append(line)
    return out


def checks(root):
    out = []

    def add(name, level, detail=""):
        out.append({"name": name, "level": level, "detail": detail})

    # manifest: present and every [mechanism] path exists; project.yaml is excluded here
    # because it does not exist until onboarding, and the project.yaml check below already
    # reports that as a warn, not an error.
    try:
        m = _manifest_paths(root)
        missing = [p for p in m["mechanism"] if p != "project.yaml" and not os.path.exists(os.path.join(root, p))]
        if missing:
            add("manifest", "error", "missing: " + ", ".join(missing))
        else:
            add("manifest", "ok", "%d mechanism paths present" % len(m["mechanism"]))
    except OSError as e:
        add("manifest", "error", str(e))

    # record: a copied-and-renamed project has no DB until its first chat, so a missing
    # .alpaca/alpaca.db is not an error; an unreadable or broken one is.
    if os.path.isfile(paths.db_path(root)):
        try:
            conn = db.connect_readonly(root)
            try:
                ok, reason = db.verify_chain(conn)
            finally:
                conn.close()
            add("record", "ok" if ok else "error", reason)
        except Exception as e:
            add("record", "error", "record unreadable: %s: %s" % (type(e).__name__, e))
    else:
        add("record", "warn", "no record yet (created by the first session or alpaca init)")

    # A domain profile (alpaca/profile.py, named by project.yaml `profile:`) may contribute its own
    # read-only checks. The generic doctor names no stage, op or capture file: the empty profile
    # returns nothing, so a project without a domain shows no such line. A profile that is named
    # but does not load is itself a finding.
    from alpaca import profile as _profile
    problem = _profile.error(root)
    if problem:
        add("profile", "error", problem)
    prof = _profile.load(root)
    if prof.name:
        add("profile", "ok", "domain profile %s loaded" % prof.name)
    if os.path.isfile(paths.db_path(root)):
        try:
            conn = db.connect_readonly(root)
            try:
                for row in prof.doctor_checks(root, conn) or ():
                    if isinstance(row, (list, tuple)) and len(row) == 3:
                        add(str(row[0]), str(row[1]), str(row[2]))
            finally:
                conn.close()
        except Exception as e:
            add("profile-checks", "warn", "profile checks skipped: %s: %s" % (type(e).__name__, e))
    if prof.name:
        # read the side-effect-free hooks once so a raising hook or a wrong-typed answer shows
        # here too, then report every hook the profile got wrong (alpaca/profile.py `errors`)
        for hook in ("stages", "verbs", "events", "paths", "web", "routes"):
            getattr(prof, hook)()
        hooks = _profile.errors(root)
        if hooks:
            add("profile-hooks", "warn", "; ".join("%s: %s" % (h, why) for h, why in sorted(hooks.items())))

    # Capture and proof: the local transcript copies still cover their sources, and every sealed
    # proof report and its evidence still hash to the sealed values.
    if os.path.isfile(paths.db_path(root)):
        for modname, label in CHECK_MODULES:
            try:
                mod = __import__("alpaca.%s" % modname, fromlist=["checks"])
            except ImportError:
                continue
            try:
                conn = db.connect_readonly(root)
                try:
                    for name, level, detail in mod.checks(root, conn):
                        add(name, level, detail)
                finally:
                    conn.close()
            except Exception as e:
                add(label, "warn", "checks skipped: %s: %s" % (type(e).__name__, e))

    # hooks: .claude/settings.json registers all five hook events with alpaca.hooks. commands
    sp = os.path.join(root, ".claude", "settings.json")
    if os.path.isfile(sp):
        try:
            hooks = _read_json(sp).get("hooks", {})
            bad = [ev for ev in HOOK_EVENTS
                   if not any("alpaca.hooks." in h.get("command", "")
                              for grp in hooks.get(ev, []) for h in grp.get("hooks", []))]
            if bad:
                add("hooks", "error", "not registered: " + ", ".join(bad))
            else:
                add("hooks", "ok", "%d events registered" % len(HOOK_EVENTS))
        except ValueError as e:
            add("hooks", "error", "settings.json unreadable: %s" % e)
    else:
        add("hooks", "error", "no .claude/settings.json")

    # Codex uses explicit lifecycle commands and project-scoped MCP configuration.
    try:
        import tomllib
    except ModuleNotFoundError:
        import tomli as tomllib
    try:
        agents = _read_text(os.path.join(root, "AGENTS.md"))
        config = tomllib.loads(_read_text(os.path.join(root, ".codex", "config.toml")))
        # MCP servers are optional and belong to a profile; a declared one must name a command.
        servers = config.get("mcp_servers", {}) or {}
        broken = sorted(name for name, spec in servers.items()
                        if not isinstance(spec, dict) or not spec.get("command"))
        if "ALPACA:BOOT:BEGIN" not in agents or "session start" not in agents:
            raise ValueError("missing Alpaca boot instructions in AGENTS.md")
        if broken:
            raise ValueError("MCP server(s) without a command: %s" % ", ".join(broken))
        add("codex", "ok", "explicit lifecycle configured; %d project MCP server(s)" % len(servers))
    except (OSError, ValueError) as exc:
        add("codex", "error", str(exc))

    # hook-timeouts: every registered hook event declares a timeout at or below the ceiling,
    # so the fail-open plumbing (alpaca/hooks/common.py) always runs under a bounded outer limit.
    if os.path.isfile(sp):
        try:
            hooks = _read_json(sp).get("hooks", {})
            problems = []
            for ev in HOOK_EVENTS:
                entries = [h for grp in hooks.get(ev, []) for h in grp.get("hooks", [])]
                if not entries:
                    continue  # a missing event is already reported by the hooks check above
                for h in entries:
                    t = h.get("timeout")
                    if t is None:
                        problems.append("%s: no timeout" % ev)
                    elif not isinstance(t, (int, float)) or isinstance(t, bool) or t <= 0:
                        problems.append("%s: bad timeout %r" % (ev, t))
                    elif t > HOOK_TIMEOUT_CEILING:
                        problems.append("%s: timeout %s over ceiling %s" % (ev, t, HOOK_TIMEOUT_CEILING))
            if problems:
                add("hook-timeouts", "error", "; ".join(problems))
            else:
                add("hook-timeouts", "ok", "all present and <= %ss" % HOOK_TIMEOUT_CEILING)
        except ValueError as e:
            add("hook-timeouts", "error", "settings.json unreadable: %s" % e)

    # shim: bin/alpaca executable
    shim = os.path.join(root, "bin", "alpaca")
    shim_ok = os.path.isfile(shim) and bool(os.stat(shim).st_mode & stat.S_IXUSR)
    add("shim", "ok" if shim_ok else "error", shim)

    # boot block: CLAUDE.md contains the ALPACA:BOOT:BEGIN marker
    cm = os.path.join(root, "CLAUDE.md")
    cm_ok = os.path.isfile(cm) and "ALPACA:BOOT:BEGIN" in _read_text(cm)
    add("boot-block", "ok" if cm_ok else "error", cm)

    # project.yaml: warn if not onboarded; error if onboarded but the schema (M1.8) is
    # incomplete, so alpaca doctor FAILs a project.yaml missing any required key.
    # A shipped template (`template: true`) carries the distribution's name, not this project's,
    # so it reads as not onboarded; a broken template is still a schema error.
    cfg = project.load(root)
    if cfg.get("name"):
        from alpaca import project_schema
        ok, errors = project_schema.validate(cfg)
        if ok and cfg.get("template") is True:
            add("project.yaml", "warn", "template project.yaml, not onboarded yet: run bin/alpaca onboard")
        elif ok:
            add("project.yaml", "ok", "onboarded as %s" % cfg.get("name"))
        else:
            add("project.yaml", "error",
                "schema: " + "; ".join("%s (%s)" % (e["key"], e["detail"]) for e in errors))
    else:
        add("project.yaml", "warn", "not onboarded yet: run bin/alpaca onboard")

    # transcripts: warn if not yet present
    td = paths.transcript_dir(root)
    if os.path.isdir(td):
        add("transcripts", "ok", td)
    else:
        add("transcripts", "warn", "no transcript dir yet (appears after the first session): " + td)

    # style: style/banned.txt present
    add("style", "ok" if os.path.isfile(os.path.join(root, "style", "banned.txt")) else "warn",
        "style/banned.txt")

    # git containment (M1.4): a repo-local git config that turns ordinary git into code
    # execution (core.hooksPath / diff.*.textconv / filter.*.clean|smudge) is a warning here
    # and a BLOCK at the build and verify doors. A tree with no .git resolves to clean.
    try:
        from alpaca.gates import git_containment
        gc = git_containment.scan(root)
        if gc:
            add("git-containment", "warn",
                "; ".join("%s %s:%d" % (tok, os.path.relpath(f, root), ln)
                          for tok, f, ln, _d in gc))
        else:
            add("git-containment", "ok", "no git-as-execution config keys")
    except Exception as e:
        add("git-containment", "warn", "scan skipped: %s: %s" % (type(e).__name__, e))

    # op-index (M2.7): a resume cursor that names a row which does not exist is reported here
    # with its op pointer. A clean or empty record resolves to ok; the deep record-consistency
    # doctor (a pad naming a missing op, a live op not advancing) is M4.13, which builds on this.
    if os.path.isfile(paths.db_path(root)):
        try:
            from alpaca import opindex
            conn = db.connect_readonly(root)
            dangling = opindex.dangling_cursors(conn)
            if dangling:
                add("op-index", "warn",
                    "dangling cursor: " + ", ".join("%s -> %s" % (op, rid) for op, rid in dangling))
            else:
                add("op-index", "ok", "cursors resolve")
        except Exception as e:
            add("op-index", "warn", "op-index scan skipped: %s: %s" % (type(e).__name__, e))

    # upgrade (M4.2): a crashed or in-progress upgrade leaves a journal under .alpaca/upgrade/. It is
    # recovered on the next start (alpaca.upgrade.recover, run from the CLI), never on demand; surface
    # it here so the owner sees a half-finished upgrade named rather than silent.
    if os.path.isdir(paths.runtime_dir(root)):
        try:
            from alpaca import upgrade
            st = upgrade.status(root)
            if st is None:
                add("upgrade", "ok", "no upgrade in progress")
            else:
                add("upgrade", "warn",
                    "upgrade left at phase %s; recovered on next start" % st.get("phase"))
        except Exception as e:
            add("upgrade", "warn", "upgrade scan skipped: %s: %s" % (type(e).__name__, e))

    # standing authority (M3.2): report the authority in force, read from the record, or name its
    # absence. Absence is the safe default (unattended action is refused without one), so it is ok,
    # not a warning. The doctor reconciliation of the per-op authority stamp is task M4.13.
    if os.path.isfile(paths.db_path(root)):
        try:
            from alpaca.posture import authority
            conn = db.connect_readonly(root)
            cur = authority.current(conn)
            if cur is None:
                add("authority", "ok", "no standing authority (unattended action refused)")
            else:
                v = authority.verify(conn, cur["level"], cur["scope"])
                detail = "%s %s scope=%s expiry=%s" % (
                    cur["id"], cur["level"], cur["scope"], cur["expiry"] or "(none)")
                add("authority", "ok" if v.ok else "warn",
                    detail if v.ok else "%s not live: %s" % (detail, v.reason))
        except Exception as e:
            add("authority", "warn", "authority scan skipped: %s: %s" % (type(e).__name__, e))

    # resolve (M4.5): the count of collisions logged and not yet resolved, read from the record.
    # A logged collision that was never resolved is a warning, not an error: the resolve pass is a
    # human-timed action, not a failure. Absence of the record is not this check's concern.
    if os.path.isfile(paths.db_path(root)):
        try:
            from alpaca import resolve
            conn = db.connect_readonly(root)
            n = resolve.unresolved_count(conn)
            if n:
                add("resolve", "warn", "%d unresolved collision(s); run the resolve pass" % n)
            else:
                add("resolve", "ok", "no unresolved collisions")
        except Exception as e:
            add("resolve", "warn", "resolve scan skipped: %s: %s" % (type(e).__name__, e))

    # python: >= 3.10 and yaml importable
    try:
        import yaml  # noqa: F401
        add("python", "ok" if sys.version_info >= (3, 10) else "error",
            "%d.%d, yaml present" % sys.version_info[:2])
    except ImportError:
        add("python", "error", "PyYAML missing")

    # record-consistency half (M4.13): the deep checks over the record itself - a pad naming an
    # op that does not exist, a cursor that resolves to nothing, a live op whose cursor has not
    # advanced, an expired or revoked authority still being cited, an unresolved collision, a
    # store over its retention ceiling, and the host-residue measurement - each folded in with
    # its pointer. Read-only. Guarded so a scan failure can never turn a clean record's PASS into
    # something worse than a single named warning.
    if os.path.isfile(paths.db_path(root)):
        try:
            conn = db.connect_readonly(root)
            for f in consistency(conn, root=root):
                add("consistency:%s" % f["check"], f["level"],
                    "%s [%s]" % (f["detail"], f["pointer"]))
        except Exception as e:
            add("consistency", "warn",
                "consistency scan skipped: %s: %s" % (type(e).__name__, e))

    # A generic project without a collector registration has no collection obligation yet.
    from alpaca.hub import capture_health
    health = capture_health(root)
    if health.get("sources") or health.get("collector", {}).get("status") not in (None, "unavailable", "not_started", "missing", "unknown"):
        add("observability", "ok" if health.get("status") == "ok" else "warn",
            "collector=%s; capture=%s; coverage=%s" % (
                health.get("collector", {}).get("status", "unknown"), health.get("status", "unknown"),
                json.dumps(health.get("coverage", {}), sort_keys=True)))
    return out


# --------------------------------------------------------------------------------------------
# M4.13: alpaca doctor grows its record-consistency half.
#
# `consistency(conn) -> list[finding]` reports, each with its pointer:
#   * a pad naming an op that does not exist          (error)
#   * a cursor that resolves to nothing               (warn)
#   * an op marked live whose cursor has not advanced (warn)
#   * an expired or revoked authority still cited      (warn)
#   * an unresolved collision                          (warn)
#   * a store over its retention ceiling               (warn)
#   * a dangerous local grant on the host (host_residue port, warn)
#
# Every check is a "parser" over the record; the doctor carries a `selftest` that, for each
# check, seeds a throwaway record the check MUST fire on. A parser that has gone inert (matches
# nothing on its own positive fixture) fails the selftest - the predecessor's documented failure
# mode. consistency() is read-only: it opens nothing for writing and mutates no tree.
# --------------------------------------------------------------------------------------------

#: a finding is a plain dict, JSON-serialisable, so alpaca status --json and the manual can read it.
def _finding(check, level, detail, pointer):
    return {"check": check, "level": level, "detail": str(detail), "pointer": str(pointer)}


#: the order a human should act on findings: an error (a broken pointer) before a warning.
_SEVERITY_RANK = {"error": 0, "warn": 1, "ok": 2}


def _ops_ids(conn):
    return {o["id"] for o in db.rows(conn, "ops", "1=1")}


def _scan_pad_op(conn, root, now):
    """A pad naming an op that does not exist. The landing pad surfaces write-ahead token rows
    (token-issue events); a token row whose op is not on the record names a phantom op."""
    from alpaca import token
    ids = _ops_ids(conn)
    out = []
    for e in db.events(conn, kind=token.KIND_ISSUE, limit=10 ** 9):
        op = e.get("op")
        if op and op not in ids:
            tok = (e.get("data") or {}).get("token") or "-"
            out.append(_finding(
                "pad-op", "error",
                "the landing pad names op %s, which is not on the record" % op,
                "event %s token %s" % (e["id"], tok)))
    return out


def _scan_cursor(conn, root, now):
    """A resume cursor that resolves to nothing: a cursor pinned to a row that does not exist."""
    from alpaca import opindex
    return [_finding("cursor", "warn",
                     "op %s has a resume cursor pinned to row %s, which resolves to nothing"
                     % (op, rid),
                     "op %s cursor %s" % (op, rid))
            for op, rid in opindex.dangling_cursors(conn)]


def _scan_live_not_advancing(conn, root, now):
    """An op marked live whose cursor has not advanced within the staleness window."""
    from alpaca import opindex
    now = now or util.now_iso()
    out = []
    for o in db.rows(conn, "ops", "1=1 ORDER BY id"):
        op = o["id"]
        if opindex.is_stale(conn, op, now):
            advanced = opindex._cursor_advanced_at(conn, op)
            out.append(_finding(
                "live-not-advancing", "warn",
                "op %s is marked live but its cursor has not advanced since %s"
                % (op, advanced or "(never)"),
                "op %s" % op))
    return out


def _scan_authority(conn, root, now):
    """An expired or revoked authority still being cited by an op that is not closed."""
    from alpaca.posture import authority
    now = now or util.now_iso()
    grants = {}
    for e in db.events(conn, kind=authority.GRANT_KIND, limit=10 ** 9):
        g = e.get("data") or {}
        if g.get("id"):
            grants[g["id"]] = g
    revoked = set()
    for e in db.events(conn, kind=authority.REVOKE_KIND, limit=10 ** 9):
        d = e.get("data") or {}
        revoked.add(d.get("id") or e.get("ref"))
    out = []
    for o in db.rows(conn, "ops", "1=1 ORDER BY id"):
        if o.get("status") == "closed":
            continue
        stamp = authority.for_op(conn, o["id"])
        aid = (stamp or {}).get("authority")
        if not aid:
            continue
        reason = None
        if aid in revoked:
            reason = "revoked"
        else:
            g = grants.get(aid)
            exp = authority.parse_dt(g.get("expiry")) if g else None
            nd = authority.parse_dt(now)
            if exp is not None and nd is not None and nd >= exp:
                reason = "expired"
        if reason:
            out.append(_finding(
                "authority", "warn",
                "op %s still cites authority %s, which is %s" % (o["id"], aid, reason),
                "op %s authority %s" % (o["id"], aid)))
    return out


def _scan_collision(conn, root, now):
    """An unresolved collision: a concurrent-detected entry no resolved entry answers."""
    from alpaca import resolve
    return [_finding("collision", "warn",
                     "a collision on ref %s was detected and never resolved" % e.get("ref"),
                     "resolve-log event %s ref %s" % (e["id"], e.get("ref")))
            for e in resolve.unresolved(conn)]


def _scan_store(conn, root, now):
    """A store over its declared retention ceiling (the size ceiling read from project.yaml)."""
    from alpaca import retention
    if not root:
        return []
    policy = retention.load_policy(root)
    ceiling = policy.get("max_bytes")
    if ceiling is None:
        return []
    size = retention.store_size(root)
    if size > ceiling:
        return [_finding(
            "store-ceiling", "warn",
            "the store holds %d bytes, over its retention ceiling of %d" % (size, ceiling),
            retention.store_dir(root))]
    return []


def _scan_host_residue(conn, root, now):
    """A dangerous local grant on the host, measured read-only (host_residue port). Reported as a
    warning; an unmeasurable or absent residue is not a warning here."""
    from alpaca.gates import host_residue
    if not root:
        return []
    m = host_residue.measure(root)
    return [_finding("host-residue", "warn",
                     "a dangerous local grant is present on the host: %s" % g,
                     host_residue.residue_path(root))
            for g in m["dangerous"]]


# ---- selftest seeds: each seed installs a throwaway record its check MUST fire on --------------
_SELFTEST_T0 = "2026-01-01T00:00:00+00:00"
_SELFTEST_NOW = "2026-06-01T00:00:00+00:00"   # far past T0's staleness window and any seeded expiry


def _seed_row(conn, rid, op):
    db.upsert(conn, "rows", "id", {
        "id": rid, "kind": "item", "op": op, "phase": "build", "step": "s1",
        "statement": "do the thing properly here now", "proof": "local:spec.md",
        "where_": "", "how": "", "when_": "", "why": "", "session": None, "operator": None,
        "status": "open", "tag": "Specced", "content_hash": util.sha256_hex("row/" + rid),
        "prev_hash": None, "supersedes": None})


def _seed_op(conn, oid, status="open"):
    db.upsert(conn, "ops", "id", {"id": oid, "intent": "i", "done_when": "d", "status": status,
                                  "opened": util.now_iso(), "closed": None, "phases": "[]"})


def _seed_pad_op(conn, root):
    from alpaca import token
    db.append_event(conn, session="alpaca", actor="alpaca", kind=token.KIND_ISSUE, op="op-ghost",
                    ref="target", data={"token": "tok-ghost"})


def _seed_cursor(conn, root):
    from alpaca import opindex
    _seed_op(conn, "op-c")
    opindex.set_cursor(conn, "op-c", "r-ghost", session="selftest")


def _seed_live_not_advancing(conn, root):
    from alpaca import board
    _seed_op(conn, "op-l")
    _seed_row(conn, "r-l", "op-l")
    board.claim(conn, "r-l", "worker-a", "selftest", minutes=600)


def _seed_authority(conn, root):
    from alpaca.posture import authority
    _seed_op(conn, "op-a")
    authority.grant(conn, "L1", "op-a", expiry="2026-02-01T00:00:00+00:00", actor="owner")
    authority.bind_op(conn, "op-a", session="selftest")


def _seed_collision(conn, root):
    from alpaca import resolve
    resolve.log(conn, resolve.CONCURRENT, "R-x",
                {"ref": "R-x", "writers": [{"worker": "a"}, {"worker": "b"}]}, now=_SELFTEST_T0)


def _seed_store(conn, root):
    from alpaca import project as _project, retention
    _project.save(root, {"name": "selftest", "retention": {"max_bytes": 1}})
    src = os.path.join(root, "F.txt")
    with open(src, "w", encoding="utf-8") as fh:
        fh.write("some bytes over one\n")
    retention.snapshot(root, "F.txt", "tag", "a note", now=_SELFTEST_T0)


def _seed_host_residue(conn, root):
    d = os.path.join(root, ".claude")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "settings.local.json"), "w", encoding="utf-8") as fh:
        json.dump({"permissions": {"allow": ["Bash(rm -rf *)"]}}, fh)


#: the check registry: name, level (the worst a finding may carry), the parser, and the seed that
#: proves the parser is not inert. `needs_clock` marks a check whose fixture needs a FixedClock at
#: T0 installed (so a leased claim reads as live) while the scan runs with `now` far in the future.
_CONSISTENCY_CHECKS = [
    {"name": "pad-op", "level": "error", "scan": _scan_pad_op,
     "seed": _seed_pad_op, "needs_clock": False},
    {"name": "cursor", "level": "warn", "scan": _scan_cursor,
     "seed": _seed_cursor, "needs_clock": False},
    {"name": "live-not-advancing", "level": "warn", "scan": _scan_live_not_advancing,
     "seed": _seed_live_not_advancing, "needs_clock": True},
    {"name": "authority", "level": "warn", "scan": _scan_authority,
     "seed": _seed_authority, "needs_clock": False},
    {"name": "collision", "level": "warn", "scan": _scan_collision,
     "seed": _seed_collision, "needs_clock": False},
    {"name": "store-ceiling", "level": "warn", "scan": _scan_store,
     "seed": _seed_store, "needs_clock": False},
    {"name": "host-residue", "level": "warn", "scan": _scan_host_residue,
     "seed": _seed_host_residue, "needs_clock": False},
]


class DoctorSelftestError(Exception):
    """One or more consistency checks have gone inert: a parser matched nothing on its own
    positive fixture. Raised by `consistency_selftest` so a broken doctor cannot pass silently."""


def _root_of(conn):
    """Best-effort project root for a connection, from the sqlite database file path
    (`<root>/.alpaca/alpaca.db`), or None for an in-memory / detached connection."""
    try:
        for _id, name, filename in conn.execute("PRAGMA database_list"):
            if name == "main" and filename:
                return os.path.dirname(os.path.dirname(os.path.abspath(filename)))
    except Exception:
        pass
    return None


def consistency(conn, root=None, now=None):
    """The record-consistency findings, most-urgent first (errors before warnings), each a dict
    {check, level, detail, pointer}. Read-only: it opens nothing for writing and mutates no tree.
    `root` is derived from the connection when not given; `now` defaults to the process clock."""
    if root is None:
        root = _root_of(conn)
    findings = []
    for chk in _CONSISTENCY_CHECKS:
        findings.extend(chk["scan"](conn, root, now))
    findings.sort(key=lambda f: _SEVERITY_RANK.get(f["level"], 9))
    return findings


def selftest():
    """Run every consistency check against its own positive fixture in a throwaway record and
    report which checks fired and which went inert. Returns {ok, results, inert}. A check that
    matches nothing on its fixture is inert: `ok` is then False and its name is in `inert`."""
    results, inert = [], []
    for chk in _CONSISTENCY_CHECKS:
        fired = False
        error = None
        with tempfile.TemporaryDirectory() as root:
            installed = chk.get("needs_clock")
            if installed:
                from alpaca import clock as _clock
                util.set_clock(_clock.FixedClock(start=_SELFTEST_T0))
            try:
                conn = db.connect(root)
                chk["seed"](conn, root)
                scan_now = _SELFTEST_NOW if installed else None
                fired = len(chk["scan"](conn, root, scan_now)) > 0
            except Exception as e:                       # a seed or scan that blew up is not "fired"
                error = "%s: %s" % (type(e).__name__, e)
            finally:
                if installed:
                    util.set_clock(None)
        results.append({"name": chk["name"], "ok": fired, "error": error})
        if not fired:
            inert.append(chk["name"])
    return {"ok": not inert, "results": results, "inert": inert}


def consistency_selftest():
    """Raise DoctorSelftestError when any consistency check has gone inert; otherwise return the
    selftest report. This is the guard a release door or a test runs so a doctor whose parser
    matches nothing cannot ship as if it still checked anything."""
    report = selftest()
    if not report["ok"]:
        raise DoctorSelftestError(
            "consistency check(s) inert (matched nothing on their own fixture): %s"
            % ", ".join(report["inert"]))
    return report


@cli.command("doctor")
def cmd_doctor(args):
    root = cli._root()
    res = checks(root)
    for c in res:
        print("%-5s %-12s %s" % (c["level"].upper(), c["name"], c["detail"]))
    worst = max(_LEVELS.index(c["level"]) for c in res)
    print("GATE alpaca-doctor: %s" % _GATE_WORD[worst])
    return _EXIT_CODE[worst]


def _parser(sub):
    sub.add_parser("doctor")


cli.register_parser("doctor", _parser)
