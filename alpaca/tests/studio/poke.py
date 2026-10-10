"""Change the Studio fixture's record the way live work would, for the motion and live checks (t-475).

    python poke.py <fixture-root> <action> [--at ISO]

Actions (each a real record write through alpaca's own writers, stamped by the fixed clock):
    message   post a note to the message board                (moves the board revision)
    task      add an open task to the sync op (op-003)          (the overview changes in place)
    claim     claim the newest open sync task with a live lease  (a new in-flight card)
    done      seal a proof and move the next open sync task to done   (Arthur celebrates)
    session   register a new session with a start event         (Arthur greets it)
    gatefail  record a self-check FAIL                          (Arthur shows a problem)
    gateblocked  record a self-check BLOCKED                    (shield_up)
    question  open a new owner question in op-002's batch       (raising_hand)
    release   record a passing release build                    (packing_box)
    finishop  seal and close every remaining task of op-003     (dance, stamping)
    merge     commit on a branch and merge it into main         (merging_papers)
    retitle   edit a task title only, no session event          (a board revision)
    doc       add a document under docs/, no record write       (a library-only revision)
Prints the id it wrote. Only ever run on a fixture built by studio_fixture.py.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))


def main(argv):
    root, action = os.path.abspath(argv[0]), argv[1]
    at = argv[argv.index("--at") + 1] if "--at" in argv else "2026-03-18T14:20:00Z"
    if not os.path.isfile(os.path.join(root, ".studio-fixture", "auth")):
        raise SystemExit("poke: %s is not a Studio fixture" % root)
    sys.path[:0] = [HERE, REPO]
    import fixed_clock
    import time
    os.environ["TZ"] = "UTC"
    time.tzset()
    clock = fixed_clock.install(fixed_clock.parse(at) + 60)
    os.chdir(root)
    from alpaca import db, messages, ops
    conn = db.connect(root)
    try:
        sid = "5b1e0c2a-7d41-4c8e-9a31-2f6d8e0b4c11"
        if action == "message":
            n = conn.execute("SELECT COUNT(*) FROM events WHERE kind='msg'").fetchone()[0]
            row = messages.post(conn, "agent:claude", "broadcast", "note", "Depot sync window moved to 17:00 (note %d)." % n,
                                session=sid, root=root)
            print(row.get("id") or row.get("msg_id") or "message")
        elif action == "task":
            n = conn.execute("SELECT COUNT(*) FROM tasks WHERE op='op-003'").fetchone()[0]
            print(ops.add_task(conn, "op-003", "Retry a sync after a dropped connection (%d)" % n,
                               "Retry a dropped sync %d" % n, session=sid, actor="agent"))
        elif action == "claim":
            row = conn.execute("SELECT id FROM tasks WHERE op='op-003' AND status='open' ORDER BY id DESC LIMIT 1").fetchone()
            tid, s3 = row[0], "c3d2e1f0-a9b8-4c7d-8e6f-5a4b3c2d1e33"
            db.append_event(conn, session=s3, actor="sync-worker", kind="claim", op="op-003", ref=tid,
                            data={"worker": "sync-worker"})
            db.patch(conn, "tasks", "id", tid, {"status": "doing", "claimant": "sync-worker",
                                                "lease_until": "2999-01-01T00:00:00Z"})
            print(tid)
        elif action == "done":
            from alpaca.tests import proofkit
            row = conn.execute("SELECT id FROM tasks WHERE op='op-003' AND status='open' ORDER BY id LIMIT 1").fetchone()
            tid = row[0]
            ptr = proofkit.seal_for(root, tid, conn=conn, session=sid,
                                    filler="I wrote the merge rule for %s and ran the sync checks over it; every check passed." % tid)
            clock.set(clock.now() + 60)
            db.append_event(conn, session=sid, actor="agent", kind="task-move", op="op-003", ref=tid,
                            data={"from": "open", "to": "done", "proof": ptr})
            db.patch(conn, "tasks", "id", tid, {"status": "done", "proof": ptr})
            print(tid)
        elif action == "session":
            n = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
            new = "d%07d-0000-4000-8000-00000000fe%02d" % (n, n)
            db.upsert(conn, "sessions", "sid", {"sid": new, "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
            db.meta_set(conn, "operator:" + new, "claude")
            db.append_event(conn, session=new, actor="agent", kind="session-start", ref=new, data={"operator": "claude"})
            print(new)
        elif action == "doc":
            # a library-only change: a new document under docs/, no record write
            n = len(os.listdir(os.path.join(root, "docs")))
            with open(os.path.join(root, "docs", "depot-note-%d.md" % n), "w", encoding="utf-8") as fh:
                fh.write("# Depot note %d\n\nThe sync window at the depot moved to 17:00.\n" % n)
            print("docs/depot-note-%d.md" % n)
        elif action == "retitle":
            # a board-only change: a task row edit with no session event, so the analytics fold stays
            n = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
            db.patch(conn, "tasks", "id", "t-016", {"title": "Sync load test, two hundred devices (%d)" % n})
            print("t-016")
        elif action == "question":
            from alpaca import batch
            n = conn.execute("SELECT COUNT(*) FROM events WHERE kind='batch-item'").fetchone()[0]
            print(batch.open_batch(conn, "op-002", session=sid, questions=["Which depot hosts the sync window (%d)?" % n]))
        elif action == "gateblocked":
            ev = db.append_event(conn, session=sid, actor="alpaca", kind="selfcheck", ref="op-002",
                                 op="op-002", data={"verdict": "BLOCKED", "check": "fixture door"})
            print(ev["id"])
        elif action == "release":
            ev = db.append_event(conn, session=sid, actor="alpaca", kind="release-build", ref="rc-1",
                                 data={"status": "PASS", "kind": "build"})
            print(ev["id"])
        elif action == "finishop":
            from alpaca.tests import proofkit
            left = [r[0] for r in conn.execute("SELECT id FROM tasks WHERE op='op-003' AND status!='done' ORDER BY id")]
            for tid in left:
                ptr = proofkit.seal_for(root, tid, conn=conn, session=sid, filler="I finished %s and ran the sync checks; all passed." % tid)
                db.append_event(conn, session=sid, actor="agent", kind="task-move", op="op-003", ref=tid,
                                data={"from": "open", "to": "done", "proof": ptr})
                db.patch(conn, "tasks", "id", tid, {"status": "done", "proof": ptr, "lease_until": None})
            print(",".join(left))
        elif action == "merge":
            import subprocess
            stamp = "%d +0000" % int(clock.now())
            env = dict(os.environ, GIT_AUTHOR_NAME="Tovin Harrow", GIT_AUTHOR_EMAIL="tovin@northwind.example",
                       GIT_COMMITTER_NAME="Tovin Harrow", GIT_COMMITTER_EMAIL="tovin@northwind.example",
                       GIT_AUTHOR_DATE=stamp, GIT_COMMITTER_DATE=stamp, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
            git = lambda *a: subprocess.run(["git", "-C", root, *a], check=True, capture_output=True, env=env)
            n = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
            branch = "sync/retry-%d" % n
            git("checkout", "-q", "-b", branch)
            os.makedirs(os.path.join(root, "src", "sync"), exist_ok=True)
            with open(os.path.join(root, "src", "sync", "retry-%d.txt" % n), "w", encoding="utf-8") as fh:
                fh.write("Retry a dropped sync after the depot window.\n")
            git("add", "src/sync")
            git("commit", "-q", "-m", "Retry a dropped sync (op-003)")
            git("checkout", "-q", "main")
            git("merge", "-q", "--no-ff", "-m", "Merge " + branch, branch)
            print(branch)
        elif action == "gatefail":
            ev = db.append_event(conn, session=sid, actor="alpaca", kind="selfcheck", ref="op-002",
                                 op="op-002", data={"verdict": "FAIL", "check": "fixture door"})
            print(ev["id"])
        else:
            raise SystemExit("poke: unknown action %s" % action)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
