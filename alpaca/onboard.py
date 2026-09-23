"""First-chat onboarding writer. The chat asks; this verb records. Runs once."""
import os
from alpaca import adopt, cli, db, project, util

DONE_WHEN_0 = "project.yaml and intents/queue.md written"

def _people(spec: str):
    out = []
    for part in [p for p in spec.split(",") if p.strip()]:
        name, _, role = part.partition(":")
        out.append({"name": name.strip(), "role": (role.strip() or "member")})
    return out

def _queue_md(name, tasks):
    lines = ["# Intent queue - %s" % name, "",
             "Open tasks the people on this project want done. Edit freely. Each line becomes an op when it is picked up.", ""]
    lines += ["- [ ] %s" % t for t in tasks] or ["- [ ] (nothing yet)"]
    return "\n".join(lines) + "\n"

@cli.command("onboard")
def cmd_onboard(args):
    root = cli._root()
    conn = db.connect(root)
    # M4.3: idempotent by precondition, not by a flag. Onboarding runs only when the record shows
    # first contact (fresh); a populated record or a clone's committed identity refuses, naming
    # the state so a human knows why. edit project.yaml directly, or just start work on a clone.
    state = adopt.detect(root, conn)
    if state != adopt.FRESH:
        print("GATE alpaca-onboard: FAIL (state: %s; %s)" % (state, adopt.describe(state)))
        return cli.FAIL
    # M4.4: sense the tree, then propose. Sensing is read-only and safe against a foreign
    # repository configuration (M1.4 scan runs first, nothing in the tree is executed). The
    # proposal supplies commands that trace to a sensed file; unsensed entries stay blank.
    facts = adopt.sense(root)
    draft = adopt.propose(facts)
    people = _people(args.who)
    if not people:
        print("GATE alpaca-onboard: FAIL (--who needs at least one name:role)")
        return cli.FAIL
    cfg = {
        "name": args.name,
        "project_id": util.sha256_hex(args.name + "|" + util.now_iso())[:12],
        "what": args.what,
        "people": people,
        "tier": args.tier,
        # M4.4: the Q19 case is sensed, not only flagged. A committed .git makes this an existing
        # repository even when the flag was omitted.
        "existing_repo": bool(args.existing_repo) or draft["existing_repo"],
        "phases": {"default": list(project.DEFAULT_PHASES)},
        # M1.8 schema requires build/test/lint/run. Start from generic defaults the owner
        # edits per project, then overlay the sensed commands, each of which traces to a file.
        "commands": {**{"build": "python3 -m compileall -q .",
                        "test": "python3 -m pytest",
                        "lint": "python3 -m compileall -q .",
                        "run": "python3 -c \"import sys; sys.exit(0)\""},
                     **draft["commands"]},
        "style": {"presets": [args.preset] if args.preset else [], "banned": "style/banned.txt"},
        "cwd_history": [root],
        "default_level": "L2",
        # M1.8 schema (alpaca/project_schema.py): onboarding writes a complete project.yaml so
        # alpaca doctor passes. These are generic, shape-only defaults the owner edits per project.
        "paths": {"product_tree": ".", "artifacts": "docs", "spec_dir": "design"},
        "oracle_classes": ["static", "dynamic", "selftest", "human"],
        # Empty is schema-valid; literal_guard also reads style/banned.txt. Kept empty so the
        # written project.yaml stays pure ASCII (project.save uses allow_unicode=True).
        "forbidden_literals": [],
        "resource_class": "small",
        "boundary_rules": {"root_only": True, "no_tmp_root": True, "realpath_containment": True,
                           "atomic_writes": "temp-then-rename"},
        "non_adoptions": {"why": "runtime is Claude Code sessions, hooks, skills and the "
                          "Workflow tool; edit per project.", "forbidden": []},
        "fingerprint": {"command": "git rev-parse HEAD", "expected": "unset-until-frozen"},
    }
    sid = args.session or "cli"
    # P-005 fold: project.yaml, the onboarded flag and the op-0 close land in one
    # transaction. A fault anywhere inside leaves the record untouched, never half-open.
    with db.transaction(conn):
        # M4.4: the sensed facts land as an op-zero page BEFORE a single asserted value is written,
        # so the answers the human gave arrive on top of evidence. Its evidence pointers name the
        # file each fact was read from; an unsensed fact carries a null value, never a guess.
        db.append_event(conn, session=sid, actor="alpaca", kind="sensed", op="op-0",
                        data={"case": facts["case"], "git_safe": facts["git_safe"],
                              "git_findings": facts["git_findings"], "facts": facts["facts"],
                              "commands": facts["commands"]}, conn_in_txn=True)
        db.append_event(conn, session=sid, actor="human", kind="onboard", data=cfg, conn_in_txn=True)
        project.save(root, cfg)
        util.write_text(os.path.join(root, "intents", "queue.md"), _queue_md(args.name, args.task or []))
        db.append_event(conn, session=sid, actor="alpaca", kind="op-open", op="op-0",
                        data={"intent": "adopt: onboard this project", "done_when": DONE_WHEN_0},
                        conn_in_txn=True)
        db.append_event(conn, session=sid, actor="alpaca", kind="op-close", op="op-0",
                        data={"basis": "onboard verb completed"}, conn_in_txn=True)
        db.upsert(conn, "ops", "id", {"id": "op-0", "intent": "adopt: onboard this project",
                                       "done_when": DONE_WHEN_0, "status": "closed",
                                       "opened": util.now_iso(), "closed": util.now_iso(), "phases": "[]"})
        db.meta_set(conn, "onboarded", util.now_iso())
    print("alpaca onboard: ok (%s, %d people, %d tasks)" % (args.name, len(people), len(args.task or [])))
    return cli.PASS

def _parser(sub):
    p = sub.add_parser("onboard")
    p.add_argument("--name", required=True)
    p.add_argument("--who", required=True, help="name:role,name:role")
    p.add_argument("--what", required=True)
    p.add_argument("--task", action="append")
    p.add_argument("--tier", choices=["public", "on-prem"], default="public")
    p.add_argument("--preset", choices=["plain-writing"], default=None)
    p.add_argument("--existing-repo", action="store_true")

cli.register_parser("onboard", _parser)
