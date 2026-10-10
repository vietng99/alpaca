"""The contract of one task: its input, expected output, done bar and fail cases.

A task statement often carries all four in one sentence ("cva6 first physical run: 6_final.gds
non-empty, route DRC 0 ... at most 3 single-knob attempts"). An engineer checking the task
needs them apart. `alpaca task contract <id>` appends one `task-contract` event with the four
lists; the newest event per task is its current contract, and earlier ones stay in the record.
A task may name the profile stage it runs (alpaca/profile.py `stages`), so its page can show that
stage's latest run contract. A project without a profile has no stages, so it names none.

A contract restates what the task asks. It never grades the task: a task closes only with a
sealed proof report.

Each line carries an origin (design/intent-fidelity/design.md section 2). Storage stays backward
compatible: the four parts stay lists of strings and a new key `origins` holds, per part, a list
aligned by index with that part's lines. An origin is one of `owner-quote` (needs a `source`; the
line must be a byte-for-byte substring of it, checked by alpaca/gates/quote_check.py), `decision`
(needs a `ref`, a decision id; stored with `owner` true or false), `derived` (needs a one-line
`reason`) or `agent-proposed` (nothing). A line written with no origin is stored as
`agent-proposed`, whoever writes it, so existing callers keep working and stay out of owner
authority. The `origins` key is written only when some line carries another origin, and a contract
without it reads as all agent-proposed (`origins_of`): contracts recorded before this, and the ones
research intake records, stay byte for byte what they were, so a re-intake of an unchanged runbook
still writes no event (alpaca/tests/test_research_contract.py pins that shape). A failing
owner-quote refuses the whole contract write.

A contract may also carry a `research` part: the task's override of a research runbook's pack
contract (design/research-engine/runbook-format-3.md section 7.4). It is kept only when the input
has it, so a contract without one records exactly what it recorded before format 3. While a live
research run is bound to the task, a contract whose `research` part differs from the one the run
froze is refused and recorded (alpaca/research/contract.py guard_task_contract).
"""
from alpaca import db, util

#: P2 (op-019 design section 2): every line of a contract carries one of the brief's four origins.
ORIGINS = ("owner-quote", "decision", "derived", "agent-proposed")
#: the lens rule: printed by `task contract --show`, prepended to every shield prompt
#: (.claude/skills/shield/round-loop.js SHIELD_RULES holds the same text) and stated in shield
#: SKILL.md section 3. A checker that cites an agent-proposed line as the owner's is the fail case.
LENS_RULE = ("A contract line labelled agent-proposed (or with no origin) is never owner authority; "
             "cite the signed brief, an owner-quote line, an owner-proved decision or a batch "
             "answer instead.")
MAX_REASON = 200
KIND = "task-contract"
PARTS = ("input", "expected", "done_bar", "fail_cases")
MAX_ITEMS, MAX_TEXT = 20, 600
#: the most bytes the `research` part may take, as util.canonical_json writes it
MAX_RESEARCH = 16 * 1024


_VALUE_KEY = {"owner-quote": "source", "decision": "ref", "derived": "reason"}


def parse_origin(spec, extra=None):
    """One origin dict from `KIND[=VALUE]` text (the --origin flag), or from a bare kind plus the
    `source`, `ref` or `reason` key of a line object (`extra`). None is agent-proposed."""
    if spec is None or spec == "":
        return {"kind": "agent-proposed"}
    if not isinstance(spec, str):
        raise ValueError("an origin is text: %s" % ", ".join(ORIGINS))
    kind, _, value = spec.partition("=")
    kind, value = kind.strip(), value.strip()
    if kind not in ORIGINS:
        raise ValueError("origin must be one of: %s" % ", ".join(ORIGINS))
    if kind == "agent-proposed":
        return {"kind": kind}
    key = _VALUE_KEY[kind]
    if not value and isinstance(extra, dict) and isinstance(extra.get(key), str):
        value = extra[key].strip()
    if not value:
        raise ValueError("origin %s needs a %s (%s=<%s>)" % (kind, key, kind, key))
    if kind == "derived" and (len(value) > MAX_REASON or "\n" in value):
        raise ValueError("a derived reason is one line of 1 to %d characters" % MAX_REASON)
    return {"kind": kind, key: value}


def _items(value, name, default_origin=None):
    """(lines, origins) of one part. A line is text, or an object {"text", "origin", "source" |
    "ref" | "reason"}; text lines take `default_origin` (the --origin flag), else agent-proposed."""
    if value is None:
        return [], []
    if isinstance(value, (str, dict)):
        value = [value]
    if not isinstance(value, list) or len(value) > MAX_ITEMS:
        raise ValueError("%s must be a list of at most %d lines" % (name, MAX_ITEMS))
    out, origins = [], []
    for item in value:
        origin = parse_origin(default_origin)
        if isinstance(item, dict):
            text = item.get("text")
            origin = parse_origin(item.get("origin"), item)
        else:
            text = item
        if not isinstance(text, str) or not text.strip() or len(text.strip()) > MAX_TEXT:
            raise ValueError("each %s line must be text of 1 to %d characters" % (name, MAX_TEXT))
        out.append(text.strip())
        origins.append(origin)
    return out, origins


def check(data, stages=()):
    """The contract as it will be recorded, or ValueError. `stages` is the profile's stage list
    (alpaca/profile.py); a stage outside it is refused, and with no profile every stage is."""
    if not isinstance(data, dict):
        raise ValueError("contract must be an object")
    out, origins = {}, {}
    for part in PARTS:
        out[part], origins[part] = _items(data.get(part), part, data.get("origin"))
    if any(o["kind"] != "agent-proposed" for part in PARTS for o in origins[part]):
        out["origins"] = origins
    if not out["done_bar"]:
        raise ValueError("a contract needs at least one done bar line")
    stage = data.get("stage") or None
    if stage is not None and stage not in stages:
        if not stages:
            raise ValueError("no profile declares stages, so a contract cannot name one")
        raise ValueError("stage must be one of the profile stages: " + ", ".join(stages))
    out["stage"] = stage
    source = data.get("source")
    out["source"] = source.strip()[:MAX_TEXT] if isinstance(source, str) and source.strip() else None
    if "research" in data:
        research = data["research"]
        if not isinstance(research, dict):
            raise ValueError("research must be an object: the task's override of a research runbook's pack contract")
        try:
            size = len(util.canonical_json(research).encode("utf-8"))
        except (TypeError, ValueError) as exc:
            raise ValueError("research must hold JSON values only: %s" % exc)
        if size > MAX_RESEARCH:
            raise ValueError("research is %d bytes as canonical JSON; at most %d" % (size, MAX_RESEARCH))
        out["research"] = research
    return out


def origins_of(contract):
    """The origins of a stored contract, {part: [origin per line]}, with agent-proposed for every
    line of a contract that carries none."""
    given = (contract or {}).get("origins") or {}
    out = {}
    for part in PARTS:
        lines = (contract or {}).get(part) or []
        have = given.get(part) or []
        out[part] = [have[i] if i < len(have) else {"kind": "agent-proposed"}
                     for i in range(len(lines))]
    return out


def verify_origins(conn, kept, root=None):
    """Check every origin of a checked contract against the record, in place. An owner-quote is
    adjudicated by quote_check.check_source_quote (QUOTE-SOURCE-MISMATCH, QUOTE-SOURCE-UNSIGNED:
    the whole contract is refused); a decision must resolve and is stored with `owner` true or
    false from ownerword.decision_is_owner."""
    origins = origins_of(kept)
    if not any(o["kind"] in ("owner-quote", "decision") for part in PARTS for o in origins[part]):
        return kept
    from alpaca import decisions, ownerword, paths
    from alpaca.gates import quote_check, verdict as vc
    root = root or paths.root()
    ownerword.ensure_grandfathered(conn, root)
    for part in PARTS:
        for text, origin in zip(kept[part], origins[part]):
            if origin["kind"] == "owner-quote":
                res = quote_check.check_source_quote(conn, text, origin["source"], root)
                if res.verdict != vc.PASS:
                    raise ValueError("%s line %r: %s" % (part, text[:60], res.reasons[0]))
            elif origin["kind"] == "decision":
                if decisions._latest(conn, origin["ref"]) is None:
                    raise ValueError("ORIGIN-DECISION-UNKNOWN: %s line %r cites %s, which is not a "
                                     "recorded decision" % (part, text[:60], origin["ref"]))
                origin["owner"] = bool(ownerword.decision_is_owner(conn, origin["ref"], root))
    return kept


def prepare(conn, data, root=None):
    """The contract as it would be recorded for a task that has no id yet (task add checks it
    before adding the task), origins verified, or ValueError."""
    from alpaca import profile
    stages = profile.for_conn(conn).stages()
    kept = check(data, tuple(str(s) for s in stages) if isinstance(stages, (list, tuple)) else ())
    return verify_origins(conn, kept, root)


def record(conn, task, data, *, session, actor, root=None, owner_word=None):
    rows = db.rows(conn, "tasks", "id=?", (task,))
    if not rows:
        raise ValueError("no task %s" % task)
    kept = prepare(conn, data, root)
    if owner_word is not None:
        kept["owner_word"] = owner_word
    # a live research run froze this task's `research` part: an edit that changes it is refused and
    # recorded; for a task no live run names this reads the record and does nothing
    from alpaca.research import contract
    contract.guard_task_contract(conn, task, kept, session=session)
    return db.append_event(conn, session=session, actor=actor, kind=KIND, op=rows[0]["op"], ref=task, data=kept)


def latest(conn):
    """{task id: the newest contract with its event id, time and author}."""
    out = {}
    import json
    for row in conn.execute("SELECT id,ts,actor,ref,data FROM events WHERE kind=? ORDER BY id", (KIND,)):
        try:
            data = json.loads(row["data"])
        except (TypeError, ValueError):
            continue
        if isinstance(data, dict):
            out[row["ref"]] = dict(data, event_id=row["id"], at=row["ts"], by=row["actor"])
    return out


def show_text(contract):
    """A contract as text: each line with its origin, then the lens rule."""
    if not contract:
        return "no contract recorded\n\n" + LENS_RULE
    origins = origins_of(contract)
    out = []
    for part in PARTS:
        for i, text in enumerate(contract.get(part) or []):
            origin = origins[part][i]
            value = origin.get(_VALUE_KEY.get(origin["kind"], ""), "")
            label = origin["kind"] + ("=" + value if value else "")
            if origin["kind"] == "decision":
                label += " (owner-proved)" if origin.get("owner") else " (not owner-proved)"
            out.append("%s[%d] [%s] %s" % (part, i, label, text))
    out.append("by %s at %s" % (contract.get("by"), contract.get("at")))
    out.append("")
    out.append(LENS_RULE)
    return "\n".join(out)
