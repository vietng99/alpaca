"""op / task / msg verbs. M0 subset: a tracker with write-ahead events. Verdict rows arrive in M1."""
import datetime, json, os, re, time
from alpaca import cli, db, util

STATUSES = ("open", "doing", "done", "blocked", "deferred")
# op-057 t-623: the After-action items op is found by this marker event, never by its title. It holds
# the filed action items, so it is not the op the work happens in (current_op, the default intake op).
STANDING_KIND = "aar-items-op"
# op-057 t-629: the Release watch op holds the watch tasks of a publish that names no op and runs
# in none; it is found by its own marker and skipped the same way.
RELEASE_WATCH_KIND = "release-watch-op"
STANDING_KINDS = (STANDING_KIND, RELEASE_WATCH_KIND)
_STANDING_OPS = "SELECT op FROM events WHERE kind IN (%s) AND op IS NOT NULL" % ",".join(
    "'%s'" % k for k in STANDING_KINDS)
NOT_STANDING = "id NOT IN (%s)" % _STANDING_OPS

# M4.16: the M4 milestone op cannot close on the mechanical manual clauses alone. Before it
# closes, the owner opens docs/manual.html on a phone over a file link and records
# `alpaca decide --kind owner-countersign --ref manual-phone-read`. `alpaca op close` for the M4 op reads
# ONLY that ref (through decisions.resolve) and refuses (exit 3, PAUSED) while it is absent; it
# ignores render-neutralisation, which belongs to the release door (M4.9/M4.15). An op is the M4
# milestone op when its intent or done-when names milestone M4 (the milestone is the op).
_M4_MILESTONE = re.compile(r"\bM4\b")
MANUAL_PHONE_READ_REF = "manual-phone-read"


def _is_m4_milestone_op(op_row) -> bool:
    text = " ".join(str(op_row.get(k) or "") for k in ("intent", "done_when"))
    return bool(_M4_MILESTONE.search(text))

# op-057 t-616: a deferred task names what brings it back. --reactivate-when is free text that may
# carry structured forms; parse_trigger finds them so t-617 can tell when a trigger is met.
_TRIGGER_PREFIX = re.compile(r"(?<![\w-])(after|op|date):", re.I | re.A)
_TRIGGER_FORMS = {
    "after": (re.compile(r"after:(t-\d+)(?![\w-])", re.I | re.A), "after",
              "after: needs a task id, like after:t-012"),
    "op": (re.compile(r"op:(op-\d+)\s+closed(?![\w-])", re.I | re.A), "op-closed",
           "op: needs an op id and the word closed, like op:op-012 closed"),
    "date": (re.compile(r"date:(\d{4}-\d{2}-\d{2})(?![\w-])", re.I | re.A), "date",
             "date: needs a real date, like date:2026-12-01"),
}

def _is_date(s):
    try:
        datetime.date.fromisoformat(s); return True
    except ValueError:
        return False

def parse_trigger(text):
    """--reactivate-when text -> {"text", "forms", "errors"}. text is the stripped input, the
    source of truth. forms lists {"kind", "target"} in order of appearance, duplicates dropped:
    kind after (target t-NNN, met when that task is done), op-closed (target op-NNN, met when that
    op is closed) or date (target YYYY-MM-DD, met on or after that local date). The trigger is met
    when any form is met; free text alone never is. errors holds one sentence for each after:, op:
    or date: that does not parse. Never raises and never reads the record."""
    raw = str(text or "").strip()
    forms, errors = [], []
    for hit in _TRIGGER_PREFIX.finditer(raw):
        pattern, kind, hint = _TRIGGER_FORMS[hit.group(1).lower()]
        m = pattern.match(raw, hit.start())
        target = m.group(1).lower() if m else None
        if target and kind == "date" and not _is_date(target):
            target = None
        if target is None:
            errors.append("%s is not a trigger; %s" % ((raw[hit.start():].split() or [""])[0], hint))
        elif {"kind": kind, "target": target} not in forms:
            forms.append({"kind": kind, "target": target})
    return {"text": raw, "forms": forms, "errors": errors}

def deferral(conn, task_id):
    """The deferral in force on a task: {"reason", "reactivate_when", "event", "ts"} from its NEWEST
    task-move, when that move went to deferred with a non-blank trigger; None otherwise (a row set
    to deferred some other way, or a task moved on since). t-617 reads the Not now list from here
    and re-parses reactivate_when with parse_trigger."""
    r = conn.execute("SELECT id, ts, data FROM events WHERE kind='task-move' AND ref=? "
                     "ORDER BY id DESC LIMIT 1", (task_id,)).fetchone()
    d = json.loads(r["data"]) if r and r["data"] else {}
    when = str(d.get("reactivate_when") or "").strip()
    if d.get("to") != "deferred" or not when:
        return None
    return {"reason": d.get("reason"), "reactivate_when": when, "event": r["id"], "ts": r["ts"]}

#: the task statuses listed under Not now (op-057 t-617). A reader that leaves parked work out
#: of a list, or lists it, names this tuple, so the decision is easy to find.
NOT_NOW = ("deferred",)
#: the words every projection prints on an entry whose structured trigger has come true.
TRIGGER_MET = "trigger met: reconsider"

def trigger_met_by(conn, forms, today):
    """The first of parse_trigger's `forms` that has come true, in words, or None. after: that
    task row is done; op-closed: that op row is closed; date: `today` (YYYY-MM-DD, the record's
    own date, None on an empty record) is on or after it. Read live on every call and never
    recursive, so a done task moved back makes it unmet again and two tasks waiting on each
    other stay unmet. Free text has no forms, so it is never met."""
    for f in forms:
        kind, target = f.get("kind"), f.get("target")
        if kind == "after":
            r = conn.execute("SELECT status FROM tasks WHERE id=?", (target,)).fetchone()
            if r and r[0] == "done":
                return "%s is done" % target
        elif kind == "op-closed":
            r = conn.execute("SELECT status FROM ops WHERE id=?", (target,)).fetchone()
            if r and r[0] == "closed":
                return "%s is closed" % target
        elif kind == "date" and today and today >= target:
            return "%s has come" % target
    return None

def not_now(conn, today=None):
    """The Not now list, by id: one dict per task whose ROW is deferred, {"id", "op", "op_closed",
    "title", "reason", "reactivate_when", "forms", "trigger_met", "met_by", "since", "error"}.
    The list follows tasks.status; reason and trigger come from deferral() only, and are None for
    a row deferred some other way (listed, never met). today defaults to the record's own date
    (the newest event's stamp, in the offset it was written in), never the wall clock, so two
    renders of one record agree and the freshness gate stays green across midnight. A failure
    reading one deferral is named on that entry (error) instead of dropping it."""
    if today is None:
        from alpaca import sessions_view
        today = (sessions_view.record_now(conn) or "")[:10] or None
    closed = {r[0] for r in conn.execute("SELECT id FROM ops WHERE status='closed'")}
    out = []
    for t in db.rows(conn, "tasks", "status IN (%s) ORDER BY id" % ",".join("'%s'" % s for s in NOT_NOW)):
        row = {"id": t["id"], "op": t["op"], "op_closed": t["op"] in closed,
               "title": t["title"] or t["statement"], "reason": None, "reactivate_when": None,
               "forms": [], "trigger_met": False, "met_by": None, "since": None, "error": None}
        try:
            d = deferral(conn, t["id"])
            if d:
                forms = parse_trigger(d["reactivate_when"])["forms"]
                met_by = trigger_met_by(conn, forms, today)
                row.update(reason=d["reason"], reactivate_when=d["reactivate_when"], since=d["ts"],
                           forms=forms, trigger_met=met_by is not None, met_by=met_by)
        except Exception as exc:
            row["error"] = type(exc).__name__
        out.append(row)
    return out

def not_now_words(row, clean=None):
    """The phrases a Not now entry prints after its id and title: why it waits, what brings it
    back, and TRIGGER_MET with what met it. Free text sits in straight double quotes after a
    fixed label, so a reason such as 'open question' never reads as a '<word>: open' status
    claim to gates/record_check.py. clean prepares each free-text value for its surface; the
    default folds whitespace onto one line. row may be None (a row copied without its deferral)."""
    clean = clean or (lambda v: " ".join(str(v).split()))
    row = row or {}
    if row.get("error"):
        return ["The deferral could not be read (%s)" % row["error"]]
    quoted = lambda v: '"%s"' % clean(v) if v else "(not recorded)"
    words = ["Why not now: " + quoted(row.get("reason")),
             "Comes back when: " + quoted(row.get("reactivate_when"))]
    if row.get("trigger_met"):
        words.append("%s (%s)" % (TRIGGER_MET, row.get("met_by")))
    return words

def _trigger_problem(conn, task_id, parsed):
    """Why a --reactivate-when value can never fire, or None. Free text always passes."""
    if parsed["errors"]:
        return parsed["errors"][0]
    for f in parsed["forms"]:
        if f["kind"] == "after" and f["target"] == task_id:
            return "after:%s names the task itself; it would never fire" % task_id
        if f["kind"] == "after" and not db.rows(conn, "tasks", "id=?", (f["target"],)):
            return "after:%s names no task on the record" % f["target"]
        if f["kind"] == "op-closed" and not db.rows(conn, "ops", "id=?", (f["target"],)):
            return "op:%s closed names no op on the record" % f["target"]
    return None

def current_op(conn):
    r = conn.execute("SELECT * FROM ops WHERE status='open' AND " + NOT_STANDING
                     + " ORDER BY opened DESC LIMIT 1").fetchone()
    return dict(r) if r else None

def first_open_task(conn, op=None):
    q = ("SELECT * FROM tasks WHERE status IN ('open','doing')" + (" AND op=?" if op else "")
         + " ORDER BY (op IN (%s)), id LIMIT 1" % _STANDING_OPS)
    r = conn.execute(q, (op,) if op else ()).fetchone()
    return dict(r) if r else None

def expire_leases(conn, now=None):
    now = now or util.now_iso(); freed = []
    for t in db.rows(conn, "tasks", "status='doing' AND lease_until IS NOT NULL AND lease_until < ?", (now,)):
        with db.transaction(conn):
            db.append_event(conn, session="alpaca", actor="alpaca", kind="lease-expired", op=t["op"], ref=t["id"],
                            data={"claimant": t["claimant"]}, conn_in_txn=True)
            db.patch(conn, "tasks", "id", t["id"], {"status": "open", "claimant": None, "lease_until": None, "updated": now})
        freed.append(t["id"])
    return freed

def _move_checklist_row(conn, sid, crow, args):
    """M1.13: move a checklist obligation row by authoring a verdict row. `done` discharges it
    (PASS, needs a --proof evidence pointer); `blocked` records a BLOCKED verdict; `open`/`doing`
    have no verdict to author on an obligation and are refused. The frozen row is never edited.

    E4: a MANUAL done move through this verb carries the same sealed proof report an ordinary
    task carries. Script discharge (a profile acceptance script, or any other caller that reaches
    verdict_row.discharge directly) is not touched by this gate."""
    from alpaca import proof
    from alpaca.checklist import Halt, verdict_row
    from alpaca.gates import verdict as vc
    if args.status == "done":
        ok, problems = proof.done_gate(conn, cli._root(), args.id, args.proof)
        if not ok:
            proof.refuse_done("alpaca-task-move", args.id, problems)
            return cli.FAIL
    band = {"done": vc.PASS, "blocked": vc.BLOCKED}.get(args.status)
    if band is None:
        print("GATE alpaca-task-move: FAIL (%s is a checklist row; a verdict row can only "
              "discharge (done) or block it)" % args.id)
        return cli.FAIL
    try:
        verdict_row.discharge(conn, crow["id"], crow["content_hash"], instrument="alpaca-task-move",
                              verdict=band, evidence=[p for p in (args.proof,) if p],
                              level=args.level, session=sid)
    except Halt as h:
        print("GATE alpaca-task-move: %s (%s: %s)" % (vc.name_of(h.verdict), h.code, h.detail))
        return h.verdict
    print("%s -> %s (verdict row authored)" % (args.id, args.status))
    return cli.PASS

# ---- op
def short_title(intent, limit=48):
    """A short display name derived from an op intent: the lead-in before the first colon, else the
    intent cut at a word near `limit` characters. `op new` records it when no --title is given."""
    head = str(intent or "").strip().split(":")[0].strip()
    if len(head) <= limit:
        return head
    cut = head[:limit]
    space = cut.rfind(" ")
    return (cut[:space] if space > limit // 2 else cut) + "..."


def open_op(conn, intent, done_when, *, phases=None, session, actor, from_intent=None, title=None) -> str:
    """Open one op: the `op-open` event and the ops row in one transaction, then bind the standing
    authority it opened under (authority.bind_op), so a closed op always says what authorized it.
    Binds no intent: `op new --from-intent` binds its own afterwards, and a research run's op
    opens from none (design/research-engine/architecture.md section 2.3). Returns the op id."""
    now = util.now_iso()
    with db.transaction(conn):
        oid = "op-%03d" % (conn.execute("SELECT COUNT(*) FROM ops WHERE id != 'op-0'").fetchone()[0] + 1)
        db.append_event(conn, session=session, actor=actor, kind="op-open", op=oid,
                        data={"intent": intent, "done_when": done_when, "phases": phases,
                              "from_intent": from_intent, **({"title": title} if title else {})},
                        conn_in_txn=True)
        db.upsert(conn, "ops", "id", {"id": oid, "intent": intent, "done_when": done_when,
                                       "status": "open", "opened": now, "closed": None,
                                       "phases": json.dumps(phases)})
    # M3.2: stamp the standing authority (id + level + scope) this op ran under onto the op
    # forever, so a closed op always says what authorized it. Additive; opens no new op.
    from alpaca.posture import authority
    authority.bind_op(conn, oid, session=session)
    return oid


def standing_op(conn, kind):
    """The id of the newest open op marked with `kind` (one of STANDING_KINDS), or None. Found by the
    marker event, never by title."""
    r = conn.execute("SELECT e.op FROM events e JOIN ops o ON o.id = e.op WHERE e.kind=? "
                     "AND o.status='open' ORDER BY e.id DESC LIMIT 1", (kind,)).fetchone()
    return r[0] if r else None


def open_standing(conn, kind, intent, done_when, title, *, session, actor):
    """Open a standing op and mark it with `kind`. Joins the caller's transaction."""
    oid = open_op(conn, intent, done_when, session=session, actor=actor, title=title)
    db.append_event(conn, session=session, actor=actor, kind=kind, op=oid, ref=oid,
                    data={"title": title}, conn_in_txn=True)
    return oid


@cli.command("op")
def cmd_op(args):
    conn = db.connect(cli._root()); sid = args.session or "cli"; now = util.now_iso()
    if args.op_verb == "new":
        phases = args.phases.split(",") if args.phases else None
        # M3.6: `--from-intent` opens the op from the next open intent-queue line, which becomes an
        # intent record (label + bar + the queue file as the owner artifact). Additive: the plain
        # `op new "<intent>"` path is unchanged. Either way the op carries a label and a bar.
        from alpaca import intent as _intent
        from alpaca.gates import verdict as _vc
        intent_rec = None
        if getattr(args, "from_intent", False):
            try:
                intent_rec = _intent.next_from_queue(conn, root=cli._root(), session=sid)
            except _intent.IntentRefusal as e:
                print("GATE alpaca-op-new: %s (%s: %s)" % (_vc.name_of(e.verdict), e.reason, e.detail))
                return e.verdict
            op_intent, op_done_when = intent_rec["label"], intent_rec["done_when"]
        else:
            if not args.intent:
                print("GATE alpaca-op-new: FAIL (op new needs an intent, or --from-intent to open from "
                      "the queue)")
                return cli.FAIL
            op_intent, op_done_when = args.intent, args.done_when
        # Every new op carries a short display name (the roadmap's map label) and is pinned so its
        # operation map shows on the roadmap. --title names it; without one the name is derived
        # from the intent and recorded, so the label never falls back to the full intent text.
        title = (args.title or "").strip() or short_title(op_intent)
        if len(title) > TITLE_MAX or any(ord(c) < 32 for c in title):
            print("GATE alpaca-op-new: FAIL (--title must be one line of at most %d chars, e.g. "
                  "--title \"Watchdog\")" % TITLE_MAX); return cli.FAIL
        oid = open_op(conn, op_intent, op_done_when, phases=phases, session=sid, actor="human",
                      from_intent=intent_rec["id"] if intent_rec else None, title=title)
        # M3.6: bind the intent the op opened from onto the op, so op close can judge its bar.
        if intent_rec is not None:
            _intent.bind_op(conn, oid, intent_rec["id"], session=sid)
        # op-019 t-200: every new op opens its question batch (the six big-choice classes)
        from alpaca import batch as _batch
        _batch.open_batch(conn, oid, session=sid)
        if not args.no_pin:
            db.append_event(conn, session=sid, actor="alpaca", kind="op-pin", op=oid, ref=oid,
                            data={"pinned": True, "on_open": True})
        print("%s opened: %s" % (oid, op_intent))
        print("%s title: %s%s%s" % (oid, title, "" if args.title else " (derived; rename with: alpaca op title %s \"<name>\")" % oid,
                                   "" if args.no_pin else "; pinned on the roadmap"))
        return cli.PASS
    if args.op_verb == "title":
        title = (args.title or "").strip()
        if not db.rows(conn, "ops", "id=?", (args.id,)):
            print("GATE alpaca-op-title: FAIL (no op %s)" % args.id); return cli.FAIL
        if not title or len(title) > TITLE_MAX or any(ord(c) < 32 for c in title):
            print("GATE alpaca-op-title: FAIL (a title is one line of at most %d chars)" % TITLE_MAX); return cli.FAIL
        db.append_event(conn, session=sid, actor=sid, kind="op-title", op=args.id, ref=args.id,
                        data={"title": title})
        print("%s title: %s" % (args.id, title)); return cli.PASS
    if args.op_verb == "close":
        ops_rows = db.rows(conn, "ops", "id=?", (args.id,))
        if not ops_rows: print("GATE alpaca-op-close: FAIL (no op %s)" % args.id); return cli.FAIL
        if ops_rows[0]["status"] == "closed":
            print("GATE alpaca-op-close: FAIL (%s already closed)" % args.id); return cli.FAIL
        # op-057 t-616: a deferred task whose newest move carries a trigger is settled for the close;
        # the op-close event lists each with its trigger. A deferred row with no trigger blocks.
        left, deferred, untriggered = [], [], []
        for row in db.rows(conn, "tasks", "op=? AND status != 'done'", (args.id,)):
            got = deferral(conn, row["id"]) if row["status"] == "deferred" else None
            if got:
                deferred.append({"id": row["id"], "reason": got["reason"], "reactivate_when": got["reactivate_when"]})
                continue
            left.append(row["id"])
            if row["status"] == "deferred": untriggered.append(row["id"])
        if left:
            tail = ("; deferred with no trigger on the record: %s" % ", ".join(untriggered)) if untriggered else ""
            print("GATE alpaca-op-close: BLOCKED (%d tasks not done: %s%s)" % (len(left), ", ".join(left), tail))
            return cli.BLOCKED
        # M4.16: the M4 milestone op refuses to close without the owner's manual-phone-read
        # countersign. Read ONLY that ref through decisions.resolve; a missing row is a paused
        # decision (exit 3) that NAMES the missing ref, never a silent close. render-neutralisation
        # is a different gate's ref and does not stand in here.
        if _is_m4_milestone_op(ops_rows[0]):
            from alpaca import decisions
            from alpaca.gates import verdict as _vc
            try:
                decisions.resolve(conn, MANUAL_PHONE_READ_REF, root=cli._root())
            except Exception:
                print("GATE alpaca-op-close: PAUSED (owner-countersign ref %s is not on the record; "
                      "the M4 op cannot close until the owner records the phone read)"
                      % MANUAL_PHONE_READ_REF)
                return _vc.PAUSED
        # op-057 t-622: an op owes an after-action report when a NO-GO, a task moved back from done,
        # a failed release check or `alpaca aar owe` is on the record after its last sealed report
        # (aar.owed), or a NO-GO or release check FAIL no op is tied to was recorded while this op
        # was open. It closes once the report is sealed or the owner waives it; a seal answers every
        # trigger counted for the op, a NO-GO or release check FAIL no op is tied to included, and
        # `aar owe --event` moves such a trigger to the op it belongs to.
        # Read here, right before the write; not atomic with it (the t-616 r3 N3 window).
        from alpaca import aar
        from alpaca import profile
        try:
            due = aar.owed(conn, cli._root(), args.id)[0].get(args.id)
        except profile.ProfileError as exc:
            print("GATE alpaca-op-close: BLOCKED (the review-flow NO-GOs cannot be read, so an owed AAR "
                  "cannot be ruled out: %s)" % exc)
            return cli.BLOCKED
        if due:
            print("GATE alpaca-op-close: BLOCKED (%s)" % aar.gate_text(conn, cli._root(), args.id, due))
            return cli.BLOCKED
        with db.transaction(conn):
            data = {"basis": args.basis}
            if deferred: data["deferred"] = sorted(deferred, key=lambda d: d["id"])
            db.append_event(conn, session=sid, actor="human", kind="op-close", op=args.id,
                            data=data, conn_in_txn=True)
            db.patch(conn, "ops", "id", args.id, {"status": "closed", "closed": now})
        # M3.6: record who judged the bar met and on what basis (the close basis). Additive; the
        # basis is already required at the CLI boundary, so this records the judgment on the chain.
        from alpaca import intent as _intent
        try:
            _intent.judge(conn, args.id, args.basis, judge=sid, session=sid)
        except _intent.IntentRefusal:
            pass
        print("%s closed" % args.id)
        # M4.8: --package emits the content-addressed op-close package (bundle + digest + record
        # slice + restore line) AFTER the close has already succeeded. Additive: the close
        # conditions above are unchanged; the package is evidence of the close, not a substitute
        # for the done bar being met. A package failure is reported but never un-closes the op.
        if getattr(args, "package", False):
            from alpaca import package
            out_dir = args.package_out or os.path.join(cli._root(), ".alpaca", "packages", args.id)
            try:
                pkg = package.close(conn, args.id, out_dir)
            except package.PackageError as e:
                print("GATE alpaca-op-close-package: FAIL (%s)" % e); return cli.FAIL
            print("package: %s" % pkg["bundle"])
            print("sha256: %s" % pkg["sha256"])
            print("restore: %s" % pkg["restore_cmd"])
        return cli.PASS
    if args.op_verb == "list":
        for o in db.rows(conn, "ops", "1=1 ORDER BY id"):
            print("%-7s %-7s %s" % (o["id"], o["status"], o["intent"]))
        return cli.PASS
    # M2.7: archived and un-archive are recorded transitions in the op index; the index itself
    # (lifecycle state plus resume cursor per op) is a pure derivation. These verbs are additive.
    if args.op_verb in ("archive", "unarchive"):
        from alpaca import opindex
        if not db.rows(conn, "ops", "id=?", (args.id,)):
            print("GATE alpaca-op-%s: FAIL (no op %s)" % (args.op_verb, args.id)); return cli.FAIL
        (opindex.archive if args.op_verb == "archive" else opindex.unarchive)(
            conn, args.id, session=sid, reason=args.reason)
        print("%s %sd" % (args.id, args.op_verb)); return cli.PASS
    # A pinned op gets its own operation map on the cockpit. Pin and unpin are recorded events and
    # the newest one wins; the cockpit never infers a pin from activity.
    if args.op_verb in ("pin", "unpin"):
        if not db.rows(conn, "ops", "id=?", (args.id,)):
            print("GATE alpaca-op-%s: FAIL (no op %s)" % (args.op_verb, args.id)); return cli.FAIL
        db.append_event(conn, session=sid, actor="alpaca", kind="op-" + args.op_verb, op=args.id,
                        ref=args.id, data={"pinned": args.op_verb == "pin"})
        print("%s %sned" % (args.id, args.op_verb)); return cli.PASS
    if args.op_verb == "index":
        from alpaca import opindex
        for e in opindex.list(conn, state=args.state):
            print("%-7s %-9s cursor=%s%s" % (e["op"], e["state"], e["cursor"] or "-",
                  "" if e["cursor_ok"] else " (dangling)"))
        return cli.PASS
    return cli.USAGE

# ---- task
TITLE_MAX = 60


def _contract_args(args):
    """The four contract parts and stage from repeatable CLI flags."""
    return {"input": getattr(args, "input", None) or [], "expected": getattr(args, "expected", None) or [],
            "done_bar": getattr(args, "done_bar", None) or [], "fail_cases": getattr(args, "fail_case", None) or [],
            "stage": getattr(args, "stage", None), "source": getattr(args, "source", None),
            "origin": getattr(args, "origin", None)}


def _contract_flags(parser):
    parser.add_argument("--input", action="append", help="what the task starts from; repeatable")
    parser.add_argument("--expected", action="append", help="an expected output; repeatable")
    parser.add_argument("--done-bar", dest="done_bar", action="append", help="a done bar line; repeatable")
    parser.add_argument("--fail-case", dest="fail_case", action="append", help="a fail case; repeatable")
    parser.add_argument("--stage", help="the profile stage this task runs, if any (alpaca/profile.py stages)")
    parser.add_argument("--source", help="where the contract is restated from")
    parser.add_argument("--origin", help="the origin of every line given by flag: owner-quote=<source> "
                        "(a signed brief file, session:<sid> or dec:<id>), decision=<dec-id>, "
                        "derived=<reason>, agent-proposed (the default)")

def _mapping_flags(parser):
    membership = parser.add_mutually_exclusive_group()
    membership.add_argument('--group', help='recorded workstream ID')
    membership.add_argument('--inherit', help='inherit only the group of a same-operation task')
    order = parser.add_mutually_exclusive_group()
    order.add_argument('--after', action='append', help='prerequisite task; repeatable')
    order.add_argument('--independent', action='store_true', help='explicitly no task prerequisites')
    resources = parser.add_mutually_exclusive_group()
    resources.add_argument('--resource', action='append', help='exclusive shared resource; repeatable')
    resources.add_argument('--no-resources', action='store_true', help='explicitly no exclusive resources')
    parser.add_argument('--requires-stage', action='append', help='required profile stage; repeatable')


def _intent_refused(conn, task, action, sid, gate):
    """op-019: ask the intent gates (alpaca/intentgate.py) about a claim or a move; print the
    refusal and return True when one refuses."""
    from alpaca import intentgate
    try:
        intentgate.task_gate(conn, task, action, session=sid, root=cli._root())
    except intentgate.IntentGateRefusal as exc:
        print("GATE %s: FAIL (%s)" % (gate, exc))
        return True
    return False


def _mapping_args(args):
    result = {key: getattr(args, key) for key in ('group', 'inherit') if getattr(args, key, None)}
    if args.after is not None or args.independent: result['after'] = args.after or []
    if args.resource is not None or args.no_resources: result['resources'] = args.resource or []
    if args.requires_stage is not None: result['requires_stages'] = args.requires_stage
    return result or None


def add_task(conn, op, statement, title, *, phase=None, why=None, session="cli", actor="human",
             extra=None, mapping=None):
    """Add one open task to `op`: the task-add event and the tasks row in one transaction. Returns
    the new task id. `extra` adds keys to the event data (intake names the runbook stage a task came
    from there, so a later intake finds the task again). The caller has checked the op and title.
    On an op with a batch (op-019) a phase of the ladder is required: ValueError TASK-PHASE-*."""
    from alpaca import batch as _batch
    _batch.require_phase(conn, op, phase)
    now = util.now_iso()
    data = {"statement": statement, "title": title, "phase": phase, "why": why}
    if extra:
        data.update({k: v for k, v in extra.items() if k not in data})
    with db.transaction(conn):
        tid = "t-%03d" % (conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] + 1)
        db.append_event(conn, session=session, actor=actor, kind="task-add", op=op, ref=tid,
                        data=data, conn_in_txn=True)
        db.upsert(conn, "tasks", "id", {"id": tid, "op": op, "phase": phase, "statement": statement,
                                         "title": title, "status": "open", "why": why, "created": now,
                                         "updated": now})
        if mapping is not None:
            from alpaca import taskplan
            taskplan.map_task(conn, op, tid, mapping, session=session, actor=actor, in_transaction=True)
    return tid


@cli.command("task")
def cmd_task(args):
    conn = db.connect(cli._root()); sid = args.session or "cli"; now = util.now_iso()
    if args.task_verb == "planning":
        value = 'required' if args.require else 'optional' if args.allow_unmapped else None
        if value:
            with db.transaction(conn):
                db.meta_set(conn, 'task-planning', value)
                db.append_event(conn, session=sid, actor=sid, kind='task-planning-policy',
                                data={'policy': value}, conn_in_txn=True)
        print('Task organization: ' + db.meta_get(conn, 'task-planning', 'optional'))
        return cli.PASS
    if args.task_verb in ('group', 'map'):
        from alpaca import taskplan
        try:
            if args.task_verb == 'group':
                taskplan.group(conn, args.op, args.group, args.title, args.category, session=sid, actor=sid)
                print('%s group %s saved' % (args.op, args.group))
            else:
                rows = db.rows(conn, 'tasks', 'id=?', (args.id,))
                if not rows: raise ValueError('no task %s' % args.id)
                mapping = _mapping_args(args)
                if not mapping: raise ValueError('provide the mapping fields to change')
                taskplan.map_task(conn, rows[0]['op'], args.id, mapping, session=sid, actor=sid)
                print('%s mapped' % args.id)
        except ValueError as exc:
            print('GATE alpaca-task-%s: FAIL (%s)' % (args.task_verb, exc)); return cli.FAIL
        return cli.PASS
    if args.task_verb == "plan":
        from alpaca import taskplan
        try:
            if not db.rows(conn, 'ops', 'id=?', (args.op,)):
                raise ValueError('no operation %s' % args.op)
            if args.show:
                print(json.dumps(taskplan.latest(conn, args.op), indent=2, ensure_ascii=True))
            else:
                with open(args.file, encoding='utf-8') as stream:
                    raw = stream.read(512 * 1024 + 1)
                if len(raw) > 512 * 1024:
                    raise ValueError('plan exceeds 512 KiB')
                event = taskplan.record(conn, args.op, json.loads(raw), session=sid, actor=args.by or sid)
                print('%s plan recorded (revision %s)' % (args.op, event['id']))
        except (ValueError, OSError, TypeError) as exc:
            print('GATE alpaca-task-plan: FAIL (%s)' % exc)
            return cli.FAIL
        return cli.PASS
    if args.task_verb == "add":
        ops_rows = db.rows(conn, "ops", "id=?", (args.op,))
        if not ops_rows: print("GATE alpaca-task-add: FAIL (no op %s)" % args.op); return cli.FAIL
        title = (args.title or "").strip()
        if not title:
            print('GATE alpaca-task-add: FAIL (--title is required: a short name of at most %d chars, e.g. --title "cva6 first GDS"; '
                  'the statement is the full description)' % TITLE_MAX); return cli.FAIL
        if len(title) > TITLE_MAX:
            print("GATE alpaca-task-add: FAIL (title is %d chars; keep it to %d)" % (len(title), TITLE_MAX)); return cli.FAIL
        mapping = _mapping_args(args)
        if mapping is None and db.meta_get(conn, 'task-planning') == 'required':
            print('GATE alpaca-task-add: FAIL (organization required: use --group or --inherit, '
                  '--after or --independent, and --resource or --no-resources)'); return cli.FAIL
        # op-019 t-201: the actor is the session inside an agent session, never a fixed `human`;
        # `--by owner` needs owner words (alpaca/ownerword.py) and the next id is its subject
        from alpaca import ownerword
        has_contract = any(getattr(args, part, None) for part in ("input", "expected", "done_bar", "fail_case"))
        try:
            if has_contract:
                from alpaca import taskcontract
                taskcontract.prepare(conn, _contract_args(args), cli._root())   # a bad origin refuses before the task exists
            actor, word = ownerword.writer(
                conn, sid, getattr(args, "by", None), getattr(args, "quote", None), root=cli._root(),
                subject="t-%03d" % (conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] + 1))
            tid = add_task(conn, args.op, args.statement, title, phase=args.phase, why=args.why,
                           session=sid, actor=actor, mapping=mapping,
                           extra={"owner_word": word} if word else None)
        except ValueError as exc:
            print('GATE alpaca-task-add: FAIL (%s)' % exc); return cli.FAIL
        if has_contract:
            try:
                taskcontract.record(conn, tid, _contract_args(args), session=sid, actor=actor,
                                    root=cli._root(), owner_word=word)
            except ValueError as exc:
                print("%s added; GATE alpaca-task-contract: FAIL (%s)" % (tid, exc)); return cli.FAIL
        print("%s added" % tid); return cli.PASS
    if args.task_verb == "contract":
        from alpaca import taskcontract
        import json as _json
        if args.show:
            current = taskcontract.latest(conn).get(args.id)
            if getattr(args, "json", False):
                print(_json.dumps(current, indent=1, ensure_ascii=True))
            else:
                print(taskcontract.show_text(current))
            return cli.PASS
        try:
            from alpaca import ownerword
            data = _contract_args(args)
            if args.file:
                with open(args.file) as stream:
                    loaded = _json.load(stream)
                data = dict(loaded, **{k: v for k, v in data.items() if v})
            trow = db.rows(conn, "tasks", "id=?", (args.id,))
            actor, word = ownerword.writer(
                conn, sid, args.by, getattr(args, "quote", None), subject=args.id, root=cli._root(),
                since=ownerword.ask_ts(conn, args.id) or (trow[0]["created"] if trow else None))
            event = taskcontract.record(conn, args.id, data, session=sid, actor=actor, root=cli._root(), owner_word=word)
        except (ValueError, OSError) as exc:
            print("GATE alpaca-task-contract: FAIL (%s)" % exc); return cli.FAIL
        print("%s contract recorded (event %s)" % (args.id, event["id"])); return cli.PASS
    if args.task_verb == "title":
        # A short display name. The statement stays the full description and is never rewritten.
        t = db.rows(conn, "tasks", "id=?", (args.id,))
        if not t: print("GATE alpaca-task: FAIL (no task %s)" % args.id); return cli.FAIL
        title = args.title.strip()
        if not title or len(title) > TITLE_MAX:
            print("GATE alpaca-task-title: FAIL (title must be 1 to %d chars, got %d)" % (TITLE_MAX, len(title))); return cli.FAIL
        from alpaca import ownerword
        try:
            actor, word = ownerword.writer(conn, sid, args.by, getattr(args, "quote", None), subject=args.id,
                                           root=cli._root(), since=ownerword.ask_ts(conn, args.id) or t[0]["created"])
        except ValueError as exc:
            print("GATE alpaca-task-title: FAIL (%s)" % exc); return cli.FAIL
        with db.transaction(conn):
            db.append_event(conn, session=sid, actor=actor, kind="task-title", op=t[0]["op"], ref=args.id,
                            data=dict({"from": t[0]["title"], "to": title}, **({"owner_word": word} if word else {})),
                            conn_in_txn=True)
            db.patch(conn, "tasks", "id", args.id, {"title": title, "updated": now})
        print("%s titled: %s" % (args.id, title)); return cli.PASS
    if args.task_verb == "claim":
        # M2.5: alpaca task claim moves onto claims. The open-task guard stays here; the lease, its
        # fence and the write-ahead claim event + first heartbeat are owned by claims.take, which
        # also mirrors the current-state row to doing.
        from alpaca import claims
        t = db.rows(conn, "tasks", "id=?", (args.id,))
        if not t: print("GATE alpaca-task: FAIL (no task %s)" % args.id); return cli.FAIL
        if t[0]["status"] != "open": print("GATE alpaca-task-claim: FAIL (%s is %s; move it to open first)" % (args.id, t[0]["status"])); return cli.FAIL
        if _intent_refused(conn, t[0], "claim", sid, "alpaca-task-claim"): return cli.FAIL
        res = claims.take(conn, args.id, args.by, minutes=args.minutes, session=sid)
        from alpaca.attach import config as _attach_config
        if _attach_config.mode(cli._root()) is not None:
            # attach mode (op-062 t-648): one end point cuts the work branch of a won claim, releases the lease it
            # took when anything fails, and checks the lease once more before it says the task is claimed
            from alpaca.attach import worktree as _attach_wt
            refusal = _attach_wt.after_claim(conn, cli._root(), args.id, args.by, res, session=sid)
            if refusal:
                print("GATE alpaca-task-claim: FAIL (%s)" % refusal); return cli.FAIL
        print("%s claimed by %s until %s" % (args.id, args.by, res["lease_until"])); return cli.PASS
    if args.task_verb == "move":
        # M1.13: a checklist obligation row is discharged by AUTHORING a verdict row (machine-
        # authored, with an evidence pointer), never by flipping a status column. Ordinary
        # tracker tasks keep the old path unchanged, so `alpaca task move <t-NNN>` is untouched.
        crow = db.rows(conn, "rows", "id=?", (args.id,))
        if crow:
            return _move_checklist_row(conn, sid, crow[0], args)
        t = db.rows(conn, "tasks", "id=?", (args.id,))
        if not t: print("GATE alpaca-task: FAIL (no task %s)" % args.id); return cli.FAIL
        # op-057 t-616: deferred means "not now". A move into it names a reason and a trigger; a
        # deferred task moves out to open only. Checked before the intent and proof gates, so a
        # refused move writes nothing.
        when = (args.reactivate_when or "").strip()
        if t[0]["status"] == "deferred" and args.status != "open":
            print("GATE alpaca-task-move: FAIL (%s is deferred; move it to open first)" % args.id)
            return cli.FAIL
        if args.reactivate_when is not None and args.status != "deferred":
            print("GATE alpaca-task-move: FAIL (--reactivate-when goes only with deferred)")
            return cli.FAIL
        if args.status == "deferred":
            missing = [f for f, v in (("--reason", args.reason), ("--reactivate-when", when))
                       if not (v or "").strip()]
            if missing:
                print("GATE alpaca-task-move: FAIL (deferred needs %s)" % " and ".join(missing))
                return cli.FAIL
            problem = _trigger_problem(conn, args.id, parse_trigger(when))
            if problem:
                print("GATE alpaca-task-move: FAIL (%s)" % problem); return cli.FAIL
        if _intent_refused(conn, t[0], args.status, sid, "alpaca-task-move"): return cli.FAIL
        # E4: done is closed by a SEALED PROOF REPORT, never by a bare string. `--proof` must be
        # local:<report>, the pointer must resolve, and the latest proof-report event for this
        # task must name that path with the sha256 the file carries right now.
        if args.status == "done":
            from alpaca import proof
            ok, problems = proof.done_gate(conn, cli._root(), args.id, args.proof)
            if not ok:
                proof.refuse_done("alpaca-task-move", args.id, problems)
                return cli.FAIL
        with db.transaction(conn):
            # op-057 t-616: the guard above read the row before this transaction. Re-read it under the
            # write lock, so a deferral another session wrote in between is not overwritten.
            cur = conn.execute("SELECT status FROM tasks WHERE id=?", (args.id,)).fetchone()
            was = cur["status"] if cur else t[0]["status"]
            if was == "deferred" and args.status != "open":
                print("GATE alpaca-task-move: FAIL (%s is deferred; move it to open first)" % args.id)
                return cli.FAIL
            if args.status == "deferred":
                # End a claim still held on the task, so the claim fold and the cockpit stop naming a
                # holder and no sweep or heartbeat acts on a parked task. The release comes BEFORE the
                # task-move event: the research state fold reads the newest status event, and a
                # claim-release after the deferral would read as open.
                from alpaca import board, claims
                held = board.fold_leases(conn, [args.id]).get(args.id)
                if held: claims.release(conn, args.id, held["worker"], session=sid, now=now)
            data = {"from": was, "to": args.status, "proof": args.proof, "reason": args.reason}
            if args.status == "deferred":
                data["reason"] = args.reason.strip(); data["reactivate_when"] = when
            db.append_event(conn, session=sid, actor=args.by or "human", kind="task-move", op=t[0]["op"], ref=args.id,
                            data=data, conn_in_txn=True)
            updates = {"status": args.status, "updated": now}
            if args.proof: updates["proof"] = args.proof
            if args.status != "doing": updates["claimant"] = None; updates["lease_until"] = None
            db.patch(conn, "tasks", "id", args.id, updates)
        print("%s -> %s" % (args.id, args.status)); return cli.PASS
    if args.task_verb == "list":
        expire_leases(conn)
        where = "op=?" if args.op else "1=1"
        for t in db.rows(conn, "tasks", where + " ORDER BY id", (args.op,) if args.op else ()):
            print("%-6s %-8s %-11s %s" % (t["id"], t["status"], t["phase"] or "-", t["title"] or t["statement"]))
        return cli.PASS
    return cli.USAGE

# ---- msg
@cli.command("msg")
def cmd_msg(args):
    conn = db.connect(cli._root()); sid = args.session or "cli"; now = util.now_iso()
    if args.msg_verb == "post":
        if not args.to.strip(): print("GATE alpaca-msg-post: FAIL (--to must name an agent, a task, or broadcast)"); return cli.FAIL
        with db.transaction(conn):
            db.append_event(conn, session=sid, actor=args.sender, kind="msg", ref=args.ref,
                            data={"to": args.to, "kind": args.kind, "body": args.body}, conn_in_txn=True)
            conn.execute("INSERT INTO messages (ts,session,sender,to_,kind,body,ref) VALUES (?,?,?,?,?,?,?)",
                         (now, sid, args.sender, args.to, args.kind, args.body, args.ref))
        print("posted to %s" % args.to); return cli.PASS
    if args.msg_verb == "read":
        where = "to_=? OR to_='broadcast'" if args.to else "1=1"
        for m in db.rows(conn, "messages", where + " ORDER BY id", (args.to,) if args.to else ()):
            print("%s %s -> %s [%s] %s" % (m["ts"], m["sender"], m["to_"], m["kind"], m["body"]))
        return cli.PASS
    return cli.USAGE

# ---- apply: a non-idempotent verb guarded by the completion-token write-ahead pair
@cli.command("apply")
def cmd_apply(args):
    """Apply a change to a target exactly once. The write-ahead pair (alpaca/token.py)
    makes a mid-verb kill decidable: an intent note is committed before the act and
    a result note after, and the guard refuses to re-apply while an earlier token
    for this target is still unmatched."""
    from alpaca import token
    conn = db.connect(cli._root()); sid = args.session or "cli"
    target = args.target
    blocking = token.guard(conn, target)
    if blocking:
        print("GATE alpaca-apply: BLOCKED (unmatched token %s for %s; resolve the resume before re-applying)"
              % (blocking["token"], target))
        return cli.BLOCKED
    tok = token.issue(conn, sid, target)          # intent note, committed and durable
    hang = os.environ.get("ALPACA_TOKEN_HANG")        # test seam: pause the window between the two notes
    if hang:
        ready = os.environ.get("ALPACA_TOKEN_READY")
        if ready:
            util.write_text(ready, tok + "\n")
        time.sleep(float(hang))
    with db.transaction(conn):                    # the act carries the token so the target dedupes
        prior = int(db.meta_get(conn, "apply:%s" % target, "0") or "0")
        db.append_event(conn, session=sid, actor="alpaca", kind="apply", ref=target,
                        data={"token": tok, "count": prior + 1}, conn_in_txn=True)
        db.meta_set(conn, "apply:%s" % target, str(prior + 1))
    token.settle(conn, tok, "local:.alpaca/alpaca.db")    # result note
    print("alpaca-apply: %s applied (token %s)" % (target, tok)); return cli.PASS

# ---- retention: snapshot-out before a non-idempotent act, and compact the store (M4.7)
def snapshot_out(root, path, change_tag, note, *, now=None):
    """Snapshot-out before a non-idempotent act (`doctrine/leaves/operate-in-place.md`): take the file's pre-image with a
    mandatory note before a verb mutates it in place. A thin, importable wrapper over
    alpaca.retention.snapshot so the guarded verbs share one entry point; the note is required, so a
    caller that omits it is refused. Additive: opens no new op and changes no existing verb."""
    from alpaca import retention
    return retention.snapshot(root, path, change_tag, note, now=now)

@cli.command("retention")
def cmd_retention(args):
    """Inspect and maintain the store: snapshot-out a file's pre-image with a mandatory note, report
    the store size, or compact under the declared policy. Compaction never removes a snapshot a live
    token or an unresolved collision still references, and refuses a zero-retention policy while a
    collision is unresolved (it would delete a diff reference the collision still needs)."""
    from alpaca import retention
    root = cli._root()
    if args.retention_verb == "snapshot":
        try:
            r = retention.snapshot(root, args.path, args.tag, args.note)
        except retention.RetentionError as e:
            print("GATE alpaca-retention-snapshot: FAIL (%s)" % e); return cli.FAIL
        print("snapshot %s (note recorded)" % r["name"]); return cli.PASS
    if args.retention_verb == "compact":
        conn = db.connect(root)
        try:
            r = retention.compact(root, conn=conn)
        except retention.RetentionError as e:
            print("GATE alpaca-retention-compact: BLOCKED (%s)" % e); return cli.BLOCKED
        print("compacted: removed %d, kept %d, store %d bytes"
              % (len(r["removed"]), len(r["kept"]), r["store_bytes"])); return cli.PASS
    if args.retention_verb == "size":
        print("store: %d bytes" % retention.store_size(root)); return cli.PASS
    if args.retention_verb == "gc":
        return _retention_gc(root, args)
    return cli.USAGE


def _retention_gc(root, args):
    """Plan, and with --apply run, the project.yaml `disk:` cleanup (alpaca/disk.py): dead
    sessions' scratch, stale declared /tmp leftovers and the declared run-tree prune rules. A dry
    run is the default; it removes nothing."""
    import json as _json
    import os as _os
    from alpaca import disk
    errs = disk.policy_errors(root)
    if errs:
        print("GATE alpaca-retention-gc: FAIL (%s)" % "; ".join(errs)); return cli.FAIL
    sid = _os.environ.get("ALPACA_SESSION_ID") or _os.environ.get("CLAUDE_CODE_SESSION_ID")
    planned = disk.plan(root, current_sid=sid)
    if not planned["policy"]:
        print("no disk: block in project.yaml; nothing is swept"); return cli.PASS
    for c in planned["remove"]:
        c["bytes"] = disk.tree_bytes(c["path"])
    total = sum(c["bytes"] for c in planned["remove"])
    result = None
    if args.apply:
        result = disk.apply(root, planned, session=sid or "cli", measure=False)
        shown, total = result["removed"], result["bytes"]
    else:
        shown = planned["remove"]
    failed = (result or {}).get("failed", [])
    if args.json:
        print(_json.dumps({"verdict": "applied" if args.apply else "dry-run",
                           "kept": planned["keep"], "dropped": shown, "bytes": total,
                           "failed": failed}, indent=2))
        return cli.FAIL if failed else cli.PASS
    for c in shown:
        print("%s %-7s %8.2fG  %s  (%s)" % ("removed" if args.apply else "would remove", c["kind"],
                                          c["bytes"] / disk.GIB, c["path"], c["reason"]))
    if args.verbose:
        for c in planned["keep"]:
            print("keep    %-7s  %s  (%s)" % (c["kind"], c["path"], c["reason"]))
    print("%s %d path(s), %.2fG; kept %d" % ("removed" if args.apply else "dry run: would remove",
                                            len(shown), total / disk.GIB, len(planned["keep"])))
    for f in failed:
        print("failed  %s: %s" % (f["path"], f["error"]))
    if failed:
        return cli.FAIL
    return cli.PASS

# ---- token: inspect and resolve the write-ahead pair
@cli.command("token")
def cmd_token(args):
    from alpaca import token
    conn = db.connect(cli._root())
    if args.token_verb == "list":
        for u in token.unmatched(conn):
            print("%s target=%s session=%s opened=%s" % (u["token"], u["target"], u["session"], u["ts"]))
        return cli.PASS
    if args.token_verb == "settle":
        token.settle(conn, args.id, args.result or "local:.alpaca/alpaca.db")
        print("token %s settled" % args.id); return cli.PASS
    if args.token_verb == "void":
        token.void(conn, args.id, args.reason or "resolved at resume")
        print("token %s voided" % args.id); return cli.PASS
    return cli.USAGE

def _parser(sub):
    op = sub.add_parser("op"); ov = op.add_subparsers(dest="op_verb")
    n = ov.add_parser("new"); n.add_argument("intent", nargs="?"); n.add_argument("--done-when", dest="done_when"); n.add_argument("--phases")
    n.add_argument("--title", help="short display name for the roadmap label, at most %d chars "
                   "(derived from the intent when omitted)" % TITLE_MAX)
    n.add_argument("--no-pin", dest="no_pin", action="store_true",
                   help="do not pin the new op's operation map on the roadmap")
    t = ov.add_parser("title", help="rename an op's short display name; the newest title wins")
    t.add_argument("id"); t.add_argument("title")
    n.add_argument("--from-intent", dest="from_intent", action="store_true",
                   help="open the op from the next open line in intents/queue.md (M3.6)")
    c = ov.add_parser("close"); c.add_argument("id"); c.add_argument("--basis", required=True)
    c.add_argument("--package", action="store_true",
                   help="also emit the op-close package (bundle + digest + restore line) (M4.8)")
    c.add_argument("--package-out", dest="package_out",
                   help="the directory to write the package into (default .alpaca/packages/<op>)")
    ov.add_parser("list")
    ar = ov.add_parser("archive"); ar.add_argument("id"); ar.add_argument("--reason")
    ua = ov.add_parser("unarchive"); ua.add_argument("id"); ua.add_argument("--reason")
    ov.add_parser("pin", help="show the op's phase map on the cockpit").add_argument("id")
    ov.add_parser("unpin", help="remove the op's phase map from the cockpit").add_argument("id")
    ix = ov.add_parser("index"); ix.add_argument("--state")
    tk = sub.add_parser("task"); tv = tk.add_subparsers(dest="task_verb")
    pl = tv.add_parser('plan', help='record workstreams, dependencies and resource constraints for an operation')
    pl.add_argument('op'); pl.add_argument('--by')
    pm = pl.add_mutually_exclusive_group(required=True)
    pm.add_argument('--file', help='version-1 JSON plan, including its previous revision')
    pm.add_argument('--show', action='store_true', help='read the latest plan and revision')
    a = tv.add_parser("add"); a.add_argument("op"); a.add_argument("statement"); a.add_argument("--phase"); a.add_argument("--why")
    a.add_argument("--title", help="required: short display name, at most %d chars" % TITLE_MAX)
    _contract_flags(a); _mapping_flags(a)
    a.add_argument("--by", help="a human identity (owner, human) needs --quote: the owner's words; any other value is recorded as the actor")
    a.add_argument("--quote", help="the owner's whole message answering the id (`<id> yes`)")
    gr = tv.add_parser('group', help='create or rename a workstream within a work area')
    gr.add_argument('op'); gr.add_argument('group')
    gr.add_argument('--title', required=True); gr.add_argument('--category', required=True)
    mp = tv.add_parser('map', help='edit task relationships without replacing the operation plan')
    mp.add_argument('id'); _mapping_flags(mp)
    policy = tv.add_parser('planning', help='set project-local organization policy for new CLI tasks')
    choice = policy.add_mutually_exclusive_group()
    choice.add_argument('--require', action='store_true'); choice.add_argument('--allow-unmapped', action='store_true')
    choice.add_argument('--show', action='store_true')
    ct = tv.add_parser("contract", help="record a task's input, expected output, done bar and fail cases; the newest is current")
    ct.add_argument("id"); _contract_flags(ct); ct.add_argument("--file", help="a JSON object with the same keys")
    ct.add_argument("--by"); ct.add_argument("--show", action="store_true")
    ct.add_argument("--quote", help="with --by owner|human: the owner's whole message answering the task id")
    ct.add_argument("--json", action="store_true", help="with --show: the stored contract as JSON")
    ti = tv.add_parser("title"); ti.add_argument("id"); ti.add_argument("title"); ti.add_argument("--by")
    ti.add_argument("--quote", help="with --by owner|human: the owner's whole message answering the task id")
    cl = tv.add_parser("claim"); cl.add_argument("id"); cl.add_argument("--by", required=True); cl.add_argument("--minutes", type=int, default=60)
    mv = tv.add_parser("move"); mv.add_argument("id"); mv.add_argument("status", choices=STATUSES)
    mv.add_argument("--proof"); mv.add_argument("--reason", help="why; required with deferred"); mv.add_argument("--by")
    mv.add_argument("--reactivate-when", help="with deferred: what brings the task back; free text that may "
                    "name after:t-NNN, op:op-NNN closed or date:YYYY-MM-DD")
    mv.add_argument("--level")   # M1.13: the level a checklist-row discharge is authored at
    ls = tv.add_parser("list"); ls.add_argument("--op")
    ms = sub.add_parser("msg"); mvb = ms.add_subparsers(dest="msg_verb")
    po = mvb.add_parser("post"); po.add_argument("body"); po.add_argument("--from", dest="sender", required=True)
    po.add_argument("--to", required=True); po.add_argument("--kind", default="note"); po.add_argument("--ref")
    rd = mvb.add_parser("read"); rd.add_argument("--to")
    ap = sub.add_parser("apply"); ap.add_argument("target")
    rt = sub.add_parser("retention"); rtv = rt.add_subparsers(dest="retention_verb")
    rsn = rtv.add_parser("snapshot"); rsn.add_argument("path")
    rsn.add_argument("--tag", required=True); rsn.add_argument("--note", required=True)
    rtv.add_parser("compact"); rtv.add_parser("size")
    rgc = rtv.add_parser("gc", help="plan (default) or --apply the project.yaml disk: cleanup")
    rgc.add_argument("--apply", action="store_true"); rgc.add_argument("--json", action="store_true")
    rgc.add_argument("--verbose", "-v", action="store_true")
    tk2 = sub.add_parser("token"); tnv = tk2.add_subparsers(dest="token_verb")
    tnv.add_parser("list")
    ts = tnv.add_parser("settle"); ts.add_argument("id"); ts.add_argument("--result")
    tvd = tnv.add_parser("void"); tvd.add_argument("id"); tvd.add_argument("--reason")

cli.register_parser("ops", _parser)
