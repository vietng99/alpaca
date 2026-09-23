"""op / task / msg verbs. M0 subset: a tracker with write-ahead events. Verdict rows arrive in M1."""
import datetime, json, os, re, time
from alpaca import cli, db, util

STATUSES = ("open", "doing", "done", "blocked")

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

def current_op(conn):
    r = conn.execute("SELECT * FROM ops WHERE status='open' ORDER BY opened DESC LIMIT 1").fetchone()
    return dict(r) if r else None

def first_open_task(conn, op=None):
    q = "SELECT * FROM tasks WHERE status IN ('open','doing')" + (" AND op=?" if op else "") + " ORDER BY id LIMIT 1"
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
        with db.transaction(conn):
            oid = "op-%03d" % (conn.execute("SELECT COUNT(*) FROM ops WHERE id != 'op-0'").fetchone()[0] + 1)
            db.append_event(conn, session=sid, actor="human", kind="op-open", op=oid,
                            data={"intent": op_intent, "done_when": op_done_when, "phases": phases,
                                  "from_intent": intent_rec["id"] if intent_rec else None},
                            conn_in_txn=True)
            db.upsert(conn, "ops", "id", {"id": oid, "intent": op_intent, "done_when": op_done_when,
                                           "status": "open", "opened": now, "closed": None, "phases": json.dumps(phases)})
        # M3.2: stamp the standing authority (id + level + scope) this op ran under onto the op
        # forever, so a closed op always says what authorized it. Additive; opens no new op.
        from alpaca.posture import authority
        authority.bind_op(conn, oid, session=sid)
        # M3.6: bind the intent the op opened from onto the op, so op close can judge its bar.
        if intent_rec is not None:
            _intent.bind_op(conn, oid, intent_rec["id"], session=sid)
        print("%s opened: %s" % (oid, op_intent)); return cli.PASS
    if args.op_verb == "close":
        ops_rows = db.rows(conn, "ops", "id=?", (args.id,))
        if not ops_rows: print("GATE alpaca-op-close: FAIL (no op %s)" % args.id); return cli.FAIL
        if ops_rows[0]["status"] == "closed":
            print("GATE alpaca-op-close: FAIL (%s already closed)" % args.id); return cli.FAIL
        left = db.rows(conn, "tasks", "op=? AND status != 'done'", (args.id,))
        if left:
            print("GATE alpaca-op-close: BLOCKED (%d tasks not done: %s)" % (len(left), ", ".join(t["id"] for t in left)))
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
        with db.transaction(conn):
            db.append_event(conn, session=sid, actor="human", kind="op-close", op=args.id,
                            data={"basis": args.basis}, conn_in_txn=True)
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
            "stage": getattr(args, "stage", None), "source": getattr(args, "source", None)}


def _contract_flags(parser):
    parser.add_argument("--input", action="append", help="what the task starts from; repeatable")
    parser.add_argument("--expected", action="append", help="an expected output; repeatable")
    parser.add_argument("--done-bar", dest="done_bar", action="append", help="a done bar line; repeatable")
    parser.add_argument("--fail-case", dest="fail_case", action="append", help="a fail case; repeatable")
    parser.add_argument("--stage", help="the profile stage this task runs, if any (alpaca/profile.py stages)")
    parser.add_argument("--source", help="where the contract is restated from")

def add_task(conn, op, statement, title, *, phase=None, why=None, session="cli", actor="human",
             extra=None):
    """Add one open task to `op`: the task-add event and the tasks row in one transaction. Returns
    the new task id. `extra` adds keys to the event data (intake names the runbook stage a task came
    from there, so a later intake finds the task again). The caller has checked the op and title."""
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
    return tid


@cli.command("task")
def cmd_task(args):
    conn = db.connect(cli._root()); sid = args.session or "cli"; now = util.now_iso()
    if args.task_verb == "add":
        ops_rows = db.rows(conn, "ops", "id=?", (args.op,))
        if not ops_rows: print("GATE alpaca-task-add: FAIL (no op %s)" % args.op); return cli.FAIL
        title = (args.title or "").strip()
        if not title:
            print('GATE alpaca-task-add: FAIL (--title is required: a short name of at most %d chars, e.g. --title "cva6 first GDS"; '
                  'the statement is the full description)' % TITLE_MAX); return cli.FAIL
        if len(title) > TITLE_MAX:
            print("GATE alpaca-task-add: FAIL (title is %d chars; keep it to %d)" % (len(title), TITLE_MAX)); return cli.FAIL
        tid = add_task(conn, args.op, args.statement, title, phase=args.phase, why=args.why,
                       session=sid, actor="human")
        if any(getattr(args, part, None) for part in ("input", "expected", "done_bar", "fail_case")):
            from alpaca import taskcontract
            try:
                taskcontract.record(conn, tid, _contract_args(args), session=sid, actor=args.by if getattr(args, "by", None) else "human")
            except ValueError as exc:
                print("%s added; GATE alpaca-task-contract: FAIL (%s)" % (tid, exc)); return cli.FAIL
        print("%s added" % tid); return cli.PASS
    if args.task_verb == "contract":
        from alpaca import taskcontract
        import json as _json
        if args.show:
            print(_json.dumps(taskcontract.latest(conn).get(args.id), indent=1, ensure_ascii=True)); return cli.PASS
        try:
            data = _contract_args(args)
            if args.file:
                with open(args.file) as stream:
                    loaded = _json.load(stream)
                data = dict(loaded, **{k: v for k, v in data.items() if v})
            event = taskcontract.record(conn, args.id, data, session=sid, actor=args.by or "human")
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
        with db.transaction(conn):
            db.append_event(conn, session=sid, actor=args.by or "human", kind="task-title", op=t[0]["op"], ref=args.id,
                            data={"from": t[0]["title"], "to": title}, conn_in_txn=True)
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
        res = claims.take(conn, args.id, args.by, minutes=args.minutes, session=sid)
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
            db.append_event(conn, session=sid, actor=args.by or "human", kind="task-move", op=t[0]["op"], ref=args.id,
                            data={"from": t[0]["status"], "to": args.status, "proof": args.proof, "reason": args.reason},
                            conn_in_txn=True)
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
    """Snapshot-out before a non-idempotent act (spec 5.1:218): take the file's pre-image with a
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
    return cli.USAGE

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
    ix = ov.add_parser("index"); ix.add_argument("--state")
    tk = sub.add_parser("task"); tv = tk.add_subparsers(dest="task_verb")
    a = tv.add_parser("add"); a.add_argument("op"); a.add_argument("statement"); a.add_argument("--phase"); a.add_argument("--why")
    a.add_argument("--title", help="required: short display name, at most %d chars" % TITLE_MAX)
    _contract_flags(a)
    ct = tv.add_parser("contract", help="record a task's input, expected output, done bar and fail cases; the newest is current")
    ct.add_argument("id"); _contract_flags(ct); ct.add_argument("--file", help="a JSON object with the same keys")
    ct.add_argument("--by"); ct.add_argument("--show", action="store_true")
    ti = tv.add_parser("title"); ti.add_argument("id"); ti.add_argument("title"); ti.add_argument("--by")
    cl = tv.add_parser("claim"); cl.add_argument("id"); cl.add_argument("--by", required=True); cl.add_argument("--minutes", type=int, default=60)
    mv = tv.add_parser("move"); mv.add_argument("id"); mv.add_argument("status", choices=STATUSES)
    mv.add_argument("--proof"); mv.add_argument("--reason"); mv.add_argument("--by")
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
    tk2 = sub.add_parser("token"); tnv = tk2.add_subparsers(dest="token_verb")
    tnv.add_parser("list")
    ts = tnv.add_parser("settle"); ts.add_argument("id"); ts.add_argument("--result")
    tvd = tnv.add_parser("void"); tvd.add_argument("id"); tvd.add_argument("--reason")

cli.register_parser("ops", _parser)
