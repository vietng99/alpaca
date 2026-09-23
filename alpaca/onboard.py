"""First-chat onboarding writer. The chat asks; this verb records. Runs once."""
import os
from alpaca import adopt, cli, db, manifest, project, util

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

def _template(root) -> dict:
    """The project.yaml on disk as written (no instance overlay), or {} when there is none.
    Onboarding runs only on a fresh copy, so this is the shipped template or a nameless draft."""
    p = project.path(root)
    if not os.path.isfile(p):
        return {}
    import yaml
    with open(p, encoding="utf-8") as fh:
        doc = yaml.safe_load(fh)
    return doc if isinstance(doc, dict) else {}

def _mapping(doc: dict, key: str) -> dict:
    value = doc.get(key)
    return value if isinstance(value, dict) else {}

def _sensed_commands(root, draft: dict, template_commands: dict) -> dict:
    """The sensed commands that replace a template value. A command sensed from a file the harness
    itself ships (a mechanism path such as pytest.ini) describes the harness, not the project, and
    the template already names that command, so the template's value stays. A command sensed from
    a project file (a Makefile, package.json) still wins."""
    own = {entry.rstrip("/") for entry in manifest.mechanism(root)}
    traces = draft.get("traces") or {}
    return {name: value for name, value in draft["commands"].items()
            if name not in template_commands or traces.get(name) not in own}

def _merge(template: dict, defaults: dict, answers: dict) -> dict:
    """Template keys first, in the template's order, with the `template` marker removed; a
    default fills only a key the template lacks; the answers replace their keys in place. Keys
    the template does not have are appended in the order onboarding has always written them."""
    order = ["name", "project_id", "what", "people", "tier", "existing_repo", "phases",
             "commands", "style", "cwd_history", "default_level", "paths", "oracle_classes",
             "forbidden_literals", "resource_class", "boundary_rules", "non_adoptions",
             "fingerprint"]
    cfg = {k: v for k, v in template.items() if k != "template"}
    for key in order:
        if key in answers:
            cfg[key] = answers[key]
        elif key not in cfg and key in defaults:
            cfg[key] = defaults[key]
    return cfg

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
    # t-002: onboarding merges the answers into the project.yaml already on disk (the shipped
    # template on a fresh copy) instead of rebuilding a subset, so every template key the answers
    # do not touch survives with its value and in its order. The precedence per key is: the
    # generic default below < the template's value < a command sensed from a project file (see
    # _sensed_commands) < an answer the person gave.
    template = _template(root)
    defaults = {
        "phases": {"default": list(project.DEFAULT_PHASES)},
        # M1.8 schema requires build/test/lint/run. Start from generic defaults the owner
        # edits per project, then overlay the sensed commands, each of which traces to a file.
        "commands": {"build": "python3 -m compileall -q .",
                     "test": "python3 -m pytest",
                     "lint": "python3 -m compileall -q .",
                     "run": "python3 -c \"import sys; sys.exit(0)\""},
        "style": {"presets": [], "banned": "style/banned.txt"},
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
    style = {**defaults["style"], **_mapping(template, "style")}
    if args.preset:
        style["presets"] = [args.preset]
    answers = {
        "name": args.name,
        "project_id": util.sha256_hex(args.name + "|" + util.now_iso())[:12],
        "what": args.what,
        "people": people,
        "tier": args.tier or template.get("tier") or "public",
        # M4.4: the Q19 case is sensed, not only flagged. A committed .git makes this an existing
        # repository even when the flag was omitted.
        "existing_repo": bool(args.existing_repo) or draft["existing_repo"],
        "commands": {**defaults["commands"], **_mapping(template, "commands"),
                     **_sensed_commands(root, draft, _mapping(template, "commands"))},
        "style": style,
        "cwd_history": [root],
    }
    cfg = _merge(template, defaults, answers)
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
    p.add_argument("--tier", choices=["public", "on-prem"], default=None,
                   help="default: the template's tier, else public")
    p.add_argument("--preset", choices=["plain-writing"], default=None)
    p.add_argument("--existing-repo", action="store_true")

cli.register_parser("onboard", _parser)
