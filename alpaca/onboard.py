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

def _shipped_hashes(root) -> dict:
    """path -> sha256 of the shipped bytes, from MANIFEST.json; {} when it is absent or unreadable."""
    import json
    try:
        with open(os.path.join(root, "MANIFEST.json"), encoding="utf-8") as fh:
            files = json.load(fh).get("files")
    except (OSError, ValueError, AttributeError):
        return {}
    return files if isinstance(files, dict) else {}

def _sensed_commands(root, draft: dict, template_commands: dict) -> dict:
    """The sensed commands that replace a template value. A command sensed from a file the harness
    itself ships (a mechanism path such as pytest.ini) describes the harness, not the project, and
    the template already names that command, so the template's value stays. A command sensed from
    a project file (a Makefile, package.json) still wins, and so does one sensed from a mechanism
    path whose bytes are not the shipped ones in MANIFEST.json: a project that kept its own
    pytest.ini when the harness was added keeps its own test command."""
    own = {entry.rstrip("/") for entry in manifest.mechanism(root)}
    shipped = _shipped_hashes(root)
    traces = draft.get("traces") or {}

    def harness_file(rel):
        if rel not in own:
            return False
        if rel not in shipped:
            return True
        try:
            with open(os.path.join(root, rel), "rb") as fh:
                return util.sha256_hex(fh.read()) == shipped[rel]
        except OSError:
            return True

    return {name: value for name, value in draft["commands"].items()
            if name not in template_commands or not harness_file(traces.get(name))}

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

def _flat_answers(args) -> dict:
    return {"name": args.name, "who": args.who, "what": args.what, "tasks": args.task or [], "tier": args.tier,
            "preset": args.preset, "existing_repo": args.existing_repo}


def write_config(conn, root, sid, a, *, provisional=False, agent_call=False):
    """The first write of onboarding: the sensed facts, the answers, project.yaml, the intent queue, op-0
    and the onboarded mark, in one transaction. `a` holds name, who, what, tasks, tier, preset,
    existing_repo and, for the first chat, default_level. `provisional` marks the setup `alpaca new`
    makes before the first chat (meta first_chat=due); `revise_config` replaces it after the owner's yes.
    `agent_call` is True when an agent call (not the owner's terminal) writes it; the event then carries
    agent_call: true, which the plain `attach init` gate reads.
    Returns {"people", "tasks", "ok": True}, or {"error": text} when the answers are refused (nothing written)."""
    # M4.4: sense the tree, then propose. Sensing is read-only and safe against a foreign
    # repository configuration (M1.4 scan runs first, nothing in the tree is executed). The
    # proposal supplies commands that trace to a sensed file; unsensed entries stay blank.
    facts = adopt.sense(root)
    draft = adopt.propose(facts)
    people = _people(a["who"])
    if not people:
        return {"error": "--who needs at least one name:role"}
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
    if a.get("preset"):
        style["presets"] = [a["preset"]]
    answers = {
        "name": a["name"],
        "project_id": util.sha256_hex(a["name"] + "|" + util.now_iso())[:12],
        "what": a["what"],
        "people": people,
        "tier": a.get("tier") or template.get("tier") or "public",
        # M4.4: the Q19 case is sensed, not only flagged. A committed .git makes this an existing
        # repository even when the flag was omitted.
        "existing_repo": bool(a.get("existing_repo")) or draft["existing_repo"],
        "commands": {**defaults["commands"], **_mapping(template, "commands"),
                     **_sensed_commands(root, draft, _mapping(template, "commands"))},
        "style": style,
        "cwd_history": [root],
    }
    if a.get("default_level"):
        answers["default_level"] = a["default_level"]
    cfg = _merge(template, defaults, answers)
    tasks = a.get("tasks") or []
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
        event = dict(cfg, provisional=True) if provisional else dict(cfg)
        if agent_call:
            event["agent_call"] = True
        db.append_event(conn, session=sid, actor="human", kind="onboard", data=event, conn_in_txn=True)
        project.save(root, cfg)
        util.write_text(os.path.join(root, "intents", "queue.md"), _queue_md(a["name"], tasks))
        db.append_event(conn, session=sid, actor="alpaca", kind="op-open", op="op-0",
                        data={"intent": "adopt: onboard this project", "done_when": DONE_WHEN_0},
                        conn_in_txn=True)
        db.append_event(conn, session=sid, actor="alpaca", kind="op-close", op="op-0",
                        data={"basis": "onboard verb completed"}, conn_in_txn=True)
        db.upsert(conn, "ops", "id", {"id": "op-0", "intent": "adopt: onboard this project",
                                       "done_when": DONE_WHEN_0, "status": "closed",
                                       "opened": util.now_iso(), "closed": util.now_iso(), "phases": "[]"})
        db.meta_set(conn, "onboarded", util.now_iso())
        if provisional:
            db.meta_set(conn, adopt.FIRST_CHAT, "due")
    return {"ok": True, "people": people, "tasks": tasks}


def rehearsal(data):
    from alpaca import onboard_flow
    return onboard_flow.rehearsal(data)


def revise_config(conn, root, sid, changes, *, card, revises=None):
    """Revision 2 of the setup, after the owner's yes to `card`: replace only the keys in `changes`
    (what, people, default_level, existing_repo) whose value is given and differs. One event
    records the keys and their old and new values. Returns {"changes": {key: [old, new]}}."""
    cfg = project.load(root)
    done = {}
    for key, value in changes.items():
        if value is None or cfg.get(key) == value:
            continue
        done[key] = [cfg.get(key), value]
        cfg[key] = value
    with db.transaction(conn):
        if done:
            project.save(root, cfg)
        db.append_event(conn, session=sid, actor="owner", kind="onboard", op="op-0",
                        data=rehearsal({"revision": 2, "card": card, "revises": revises, "changes": done}), conn_in_txn=True)
    return {"changes": done}


@cli.command("onboard")
def cmd_onboard(args):
    root = cli._root()
    conn = db.connect(root)
    if getattr(args, "onboard_verb", None):
        from alpaca import onboard_flow
        return onboard_flow.run(args, root, conn)
    # M4.3: idempotent by precondition, not by a flag. Onboarding runs only when the record shows
    # first contact (fresh); a populated record or a clone's committed identity refuses, naming
    # the state so a human knows why. edit project.yaml directly, or just start work on a clone.
    state = adopt.detect(root, conn)
    if state != adopt.FRESH:
        print("GATE alpaca-onboard: FAIL (state: %s; %s)" % (state, adopt.describe(state)))
        return cli.FAIL
    if not (args.name and args.who is not None and args.what):
        print("GATE alpaca-onboard: FAIL (--name, --who and --what are required; or start with onboard look)")
        return cli.USAGE
    from alpaca import onboard_flow, ownerword
    refused = onboard_flow.setup_write_refusal(conn, root, "onboard")
    if refused:
        print("GATE alpaca-onboard: FAIL (%s) [%s]" % (refused[1], refused[0]))
        return cli.FAIL
    res = write_config(conn, root, args.session or "cli", _flat_answers(args), provisional=bool(args.provisional),
                       agent_call=ownerword.agent_call())
    if res.get("error"):
        print("GATE alpaca-onboard: FAIL (%s)" % res["error"])
        return cli.FAIL
    print("alpaca onboard: ok (%s, %d people, %d tasks)" % (args.name, len(res["people"]), len(res["tasks"])))
    return cli.PASS


def _parser(sub):
    p = sub.add_parser("onboard")
    p.add_argument("--name")
    p.add_argument("--who", help="name:role,name:role")
    p.add_argument("--what")
    p.add_argument("--task", action="append")
    p.add_argument("--tier", choices=["public", "on-prem"], default=None,
                   help="default: the template's tier, else public")
    p.add_argument("--preset", choices=["plain-writing"], default=None)
    p.add_argument("--existing-repo", action="store_true")
    p.add_argument("--provisional", action="store_true",
                   help="what alpaca new runs: set the project up now and leave the first chat due")
    from alpaca import onboard_flow
    onboard_flow.add_subparsers(p)

cli.register_parser("onboard", _parser)
