"""The first chat as verbs (op-062 t-656; design/attach-mode/onboarding-design.md).

    onboard look <git-url|path> --scratch DIR --quote "<owner message>"   read the repo, write nothing in the studio
    onboard answer <slot> ...                                             record one answer (owner words are proved)
    onboard next                                                          the questions still open, or "ready"
    onboard propose [--reask]                                             render the setup card, record the ask
    onboard confirm <card> --quote "<owner reply>"                        the owner's typed yes (or no)
    onboard apply <card>                                                  configure from the confirmed card
    onboard show | skip

Before the owner confirms, a first chat writes only to the places in `onboard_card.PRE_CONFIRM_KINDS`
(record events on op-0) and to `.alpaca/onboard/cards/`. It changes no other file of the studio.

The card is a record object: `propose` stores its bytes and its inputs, `confirm` proves the owner said
yes to those bytes (a lone yes right after the ask, or `card-<id> yes`; a click never counts), and
`apply` configures only from the confirmed card's recorded inputs, never from flags the agent retypes.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import sys

from alpaca import adopt, cli, db, first_chat, onboard_card as card_mod, onboard_look, ownerword, project, util

OP = "op-0"
CARDS = os.path.join(".alpaca", "onboard", "cards")
PROVE_MIN_WORDS = 1


class Refused(Exception):
    """A refusal with a token: str(exc) is `CODE (detail)`."""

    def __init__(self, code, detail):
        super().__init__("%s (%s)" % (code, detail))
        self.code, self.detail = code, detail


# ---------------------------------------------------------------------------------- record helpers

def _rows(conn, kind):
    out = []
    for r in conn.execute("SELECT * FROM events WHERE kind=? ORDER BY id", (kind,)):
        try:
            data = json.loads(r["data"])
        except (TypeError, ValueError):
            data = {}
        out.append({"id": r["id"], "ts": r["ts"], "session": r["session"], "actor": r["actor"], "ref": r["ref"],
                    "data": data if isinstance(data, dict) else {}})
    return out


def rehearsal(data):
    """`data` with rehearsal: true when the run is a rehearsal (ALPACA_REHEARSAL is set). Nothing reads the mark to
    decide anything; it lets a reader tell a rehearsal's events from an owner's."""
    return dict(data, rehearsal=True) if os.environ.get("ALPACA_REHEARSAL") else data


def _append(conn, sid, kind, data, *, actor="alpaca", ref=None, in_txn=False):
    data = rehearsal(data)
    return db.append_event(conn, session=sid, actor=actor, kind=kind, op=OP, ref=ref, data=data, conn_in_txn=in_txn)


def epoch(conn):
    """The current first-chat epoch: (answers, look payload or None, look event id or None, opening event id).
    An epoch starts at the latest `opening` answer; a later `look` or start-empty opening starts a new one."""
    entries = _rows(conn, "onboard-answer")
    start = None
    for i, e in enumerate(entries):
        if e["data"].get("slot") == "opening":
            start = i
    if start is None:
        return {}, None, None, None
    answers = {}
    opening = entries[start]
    for e in entries[start:]:
        d = dict(e["data"])
        slot = d.pop("slot", None)
        d.pop("setup", None)                     # the opening's setup baseline is evidence, not an answer
        d["event"] = e["id"]
        if slot not in card_mod.SLOTS:
            continue
        if slot in card_mod.REPEATABLE:
            lst = answers.setdefault(slot, [])
            if slot == "gate.extra":
                lst[:] = [x for x in lst if x.get("name") != d.get("name")]
            lst.append(d)
        else:
            answers[slot] = d
    look_id = opening["data"].get("look_event")
    look = None
    if look_id:
        row = conn.execute("SELECT data FROM events WHERE id=? AND kind='onboard-look'", (look_id,)).fetchone()
        if row:
            look = json.loads(row["data"])
    return answers, look, look_id, opening["id"]


def answers_of(conn) -> dict:
    return epoch(conn)[0]


def _rounds(conn, since_id):
    return [r for r in _rows(conn, "onboard-round") if r["id"] >= (since_id or 0)]


#: the state reader and the open test live in the leaf alpaca/first_chat.py (cli.main asks it on every agent call, and
#: cli.py must not import this module); the names stay here so no caller changes
_state = first_chat.state
first_chat_open = first_chat.first_chat_open


def agent_wrote_setup(conn):
    """True when the newest flat `onboard` event was written by an agent call (the event carries agent_call)."""
    if conn is None:
        return False
    row = conn.execute("SELECT data FROM events WHERE kind='onboard' ORDER BY id DESC LIMIT 1").fetchone()
    return bool(row) and _json_data(row).get("agent_call") is True


REFUSAL_OPEN = ("the first chat is open in this studio, so setup is written only after the owner says yes to a setup "
                "card: run bin/alpaca onboard propose, or run bin/alpaca onboard skip on the owner's word")
REFUSAL_PLAIN = ("an agent does not attach a repository on its own: the first chat does it, after the owner says yes to a "
                 "setup card (bin/alpaca onboard look <git URL or folder>, answer, propose, confirm, apply). To attach "
                 "without a first chat the owner runs bin/alpaca attach init <git URL> in a terminal of their own")
REFUSAL_NEW = ("--attach is for your own terminal. Inside a chat, run alpaca new without --attach, open the new studio, and "
               "paste the repository there: the first chat reads it, shows a setup card and attaches only after your yes")


def setup_write_refusal(conn, root, verb, *, environ=None, isatty=None):
    """The one gate in front of every agent-callable verb that writes setup outside a confirmed card: the flat
    `onboard`, the plain `attach init` and `new --attach`. Returns None to let the call go on, else (token, text).

    The owner's own terminal (no session, no agent marker, standard input is a terminal) is never refused here. Any
    other caller is an agent call, and is refused by verb and by the state of the first chat:
      onboard     while the first chat is open (due, confirmed, or fresh with a look). Otherwise it goes on and the
                  event it writes records agent_call, which is what the plain attach below reads;
      attach      unless the first chat is done or was skipped on the owner's word. A fresh studio, a due or a
                  confirmed first chat, and a studio whose setup the newest flat onboard event says an agent wrote,
                  are all refused;
      new-attach  always (it makes a new studio and attaches in it, with no card).
    The table of verb, state and caller is pinned by alpaca/tests/test_setup_write_gate.py."""
    if not ownerword.agent_call(environ, isatty):
        return None
    if verb == "new-attach":
        return "ATTACH-NEEDS-CARD", REFUSAL_NEW
    if conn is None:
        state, is_open = "fresh", False
    else:
        state, is_open = _state(conn, root), first_chat_open(conn, root)
    if verb == "onboard":
        return ("ONBOARD-FIRST-CHAT-OPEN", REFUSAL_OPEN) if is_open else None
    if verb == "attach":
        if state in ("done", "skipped"):
            return None
        if is_open or state == "fresh" or (state == "none" and agent_wrote_setup(conn)):
            return "ATTACH-NEEDS-CARD", REFUSAL_PLAIN
        return None
    raise ValueError("unknown setup verb %r" % verb)


def _need_open(conn, root, verb):
    state = _state(conn, root)
    if state in ("fresh", "due"):
        return state
    why = {"confirmed": "the card is confirmed; run bin/alpaca onboard apply <card>",
           "done": "the first chat is done", "skipped": "the first chat was skipped",
           "none": "this project has no first chat to run (it was set up before; edit project.yaml directly)"}[state]
    raise Refused("ONBOARD-NOT-OPEN", "%s: %s" % (verb, why))


def _owner_word(conn, root, sid, quote, since, environ=None):
    """The proof that `quote` is one owner message typed after `since`, or None when the process runs in no
    agent session (nothing to prove it against; the caller then records an agent reading)."""
    if ownerword.real_session(environ) is None:
        return None
    return ownerword.human_write(conn, sid, "owner", subject="onboard-answer", answer="text", quote=quote, root=root,
                                 form="quote", since=since, min_words=PROVE_MIN_WORDS, environ=environ)


def _people(conn, root):
    cfg = project.load(root)
    people = cfg.get("people")
    return [p for p in people if isinstance(p, dict) and p.get("name")] if isinstance(people, list) else []


def _project_name(root, look):
    name = project.load(root).get("name")
    if name and project.load(root).get("template") is not True:
        return str(name)
    if look and look.get("source"):
        base = re.sub(r"\.git$", "", str(look["source"]).rstrip("/").rsplit("/", 1)[-1]) or "project"
        return base
    return "this project"


def _people_for_card(root, look):
    people = _people(None, root)
    if people:
        return [{"name": p["name"], "role": p.get("role") or "member"} for p in people]
    user = ((look or {}).get("host") or {}).get("git_user")
    return [{"name": user, "role": "owner"}] if user else []


# --------------------------------------------------------------------------------------- look

def look_verb(conn, root, sid, source, *, scratch, quote, environ=None):
    """Read `source` into `scratch`, record the look, the opening and round 1. Returns the lines to print."""
    _need_open(conn, root, "look")
    if not (quote or "").strip():
        raise Refused("ONBOARD-QUOTE", "pass the owner's message with --quote")
    date = util.now_iso()[:10]
    try:
        payload = onboard_look.look(root, source, scratch=scratch, now=date)
    except onboard_look.LookError as exc:
        raise Refused(exc.code, exc.detail) from None
    word = _owner_word(conn, root, sid, quote.strip(), None, environ)
    return _record_look(conn, root, sid, payload, quote.strip(), word)


def _record_look(conn, root, sid, payload, quote, word):
    with db.transaction(conn):
        look_ev = _append(conn, sid, "onboard-look", payload, in_txn=True)
        opening = {"slot": "opening", "value": quote, "source": "owner" if word else "agent", "quote": quote,
                   "owner_word": word, "look_event": look_ev["id"],
                   "reason": None if word else "no session transcript to prove it against",
                   "setup": store_setup_baseline(root)}
        open_ev = _append(conn, sid, "onboard-answer", opening, actor=ownerword.session_actor(sid), in_txn=True)
        answers = {"opening": dict(opening, event=open_ev["id"])}
        questions = card_mod.next_round(payload, answers)
        round_ev = _append(conn, sid, "onboard-round", {"n": 1, "questions": questions, "look_event": look_ev["id"], "at": util.now_iso_precise()},
                           in_txn=True)
    guess = card_mod.situation_guess(payload, [quote])
    lines = _look_lines(payload, guess, look_ev, open_ev, round_ev, questions)
    return lines


def _look_lines(payload, guess, look_ev, open_ev, round_ev, questions):
    head, hist = payload["head"], payload["history"]
    out = ["look: read %s into %s (read-only, outside this studio)" % (payload["source"], payload["clone"]),
           "look: head %s on %s (%s), %d tracked files; ran no code from the repo"
           % (head["sha"][:7], head["branch"], head["date"], payload["tracked"]["files"])]
    gates = payload["gates"]
    if gates:
        bits = []
        for g in gates:
            t = g.get("trial")
            bits.append("%s (%s, %s)" % (g["name"], g["kind"],
                                          ("tried: %s %.2f s" % ("pass" if t["exit"] == 0 else "fail", t["seconds"]))
                                          if t else "runs repo code: after the yes"))
        out.append("look: gates sensed: " + "; ".join(bits))
    else:
        out.append("look: gates sensed: none")
    host = payload["host"]
    missing = [t for t in host.get("wanted") or [] if host["tools"].get(t) is None]
    if missing:
        out.append("look: host: %s not found" % ", ".join(missing))
    if not payload["git_safe"]:
        out.append("look: the folder's own git config sets %s; I never ran git inside it"
                   % ", ".join(sorted({f["token"] for f in payload["git_findings"]})))
    reasons = "; ".join(r["text"] + (" [%d]" % r["source"] if r.get("source") else "") for r in guess["reasons"])
    out.append("look: situation guess: %s (%s)" % (guess["situation"], reasons))
    out.append("look: recorded op-0 event %d (onboard-look, digest %s), %d (opening), %d (round 1: %d questions)"
               % (look_ev["id"], payload["digest"][:8], open_ev["id"], round_ev["id"], len(questions)))
    out.append("found (numbered as in the card; say these in turn 1 and nothing else about the code):")
    for text, ns in card_mod._found_lines(payload, {}, card_mod._Cite(payload)):
        out.append(" - %s %s" % (text, " ".join("[%d]" % n for n in ns)))
    out.append("sources:")
    for s in payload["sources"]:
        out.append(" [%d] %s" % (s["n"], s["text"]))
    out.extend(card_mod.format_round(questions, 1))
    return out


# ------------------------------------------------------------------------------------- answers

def _grab(text, value):
    """True when `value` is a part of `text` (white space collapsed on both sides)."""
    return value in text or " ".join(value.split()) in " ".join(text.split())


def _pointer_ok(look, pointer):
    """Check `repo:<path>[:<a>-<b>]` against the look clone at the look commit."""
    m = re.fullmatch(r"repo:([^:\s]+)(?::(\d+)(?:-(\d+))?)?", pointer or "")
    if not m:
        raise Refused("ONBOARD-POINTER", "a finding needs --pointer repo:<path>[:<first>-<last>]")
    path, a, b = m.group(1), m.group(2), m.group(3)
    if path.startswith("/") or ".." in path.split("/"):
        raise Refused("ONBOARD-POINTER", "the pointer path must lie inside the repo")
    clone = (look or {}).get("clone")
    sha = ((look or {}).get("head") or {}).get("sha")
    if not clone or not os.path.isdir(clone) or not sha:
        raise Refused("ONBOARD-POINTER", "the look clone is gone, so the pointer cannot be checked; run look again")
    from alpaca.attach import gitops
    res = gitops.run(clone, "cat-file", "-e", "%s:%s" % (sha, path))
    if res.rc != 0:
        raise Refused("ONBOARD-POINTER", "%s does not exist in the repo at %s" % (path, sha[:7]))
    if a:
        text = gitops.git(clone, "show", "%s:%s" % (sha, path), check=False)
        n = len(text.splitlines())
        hi = int(b or a)
        if int(a) < 1 or hi < int(a) or hi > n:
            raise Refused("ONBOARD-POINTER", "lines %s-%s are outside %s (%d lines)" % (a, b or a, path, n))
    return pointer


def answer_verb(conn, root, sid, slot, *, value=None, option=None, quote=None, source=None, reason=None, pointer=None,
                name=None, argv=None, proves=None, kind=None, timeout=None, environ=None):
    """Record one answer. Returns the lines to print."""
    _need_open(conn, root, "answer")
    if slot not in card_mod.SLOTS:
        raise Refused("ONBOARD-SLOT", "unknown slot %s; the slots are %s" % (slot, ", ".join(sorted(card_mod.SLOTS))))
    spec = card_mod.SLOTS[slot]
    answers, look, look_id, opening_id = epoch(conn)
    if slot == "opening":
        return _answer_opening(conn, root, sid, value, quote, environ)
    if opening_id is None:
        raise Refused("ONBOARD-NO-LOOK", "run onboard look <git-url|path> first, or answer the slot opening for a "
                      "new project")
    if source is None:
        source = "owner" if quote else "agent"
    if source not in ("owner", "agent", "sensed", "default"):
        raise Refused("ONBOARD-SOURCE", "--source is owner, agent, sensed or default")
    if spec["kind"] == "agent" and source == "owner":
        raise Refused("ONBOARD-SOURCE", "%s is written by the agent, not quoted from the owner" % slot)
    data = {"slot": slot, "source": source, "reason": reason, "look_event": look_id}
    opening = _single_opening(conn)
    since = opening["ts"]
    if source == "owner":
        data.update(_prove_owner(conn, root, sid, slot, spec, look, answers, conn_rounds=_rounds(conn, opening_id),
                                 value=value, option=option, quote=quote, since=since, environ=environ))
    else:
        if value is None and option is None and slot not in ("gate.extra", "gate.drop"):
            raise Refused("ONBOARD-VALUE", "give --value or --option")
        data["value"], data["option"] = value, option
        if option is not None and spec["kind"] == "option" and option not in spec["options"]:
            raise Refused("ONBOARD-OPTION", "%s takes one of %s" % (slot, ", ".join(spec["options"])))
        if quote:
            data["quote"] = quote
        if source == "sensed" and (not reason or not _sensed_key(look, reason)):
            raise Refused("ONBOARD-SENSED", "--reason must name a key of the look")
    if slot == "finding":
        if len(answers.get("finding", [])) >= spec["max"]:
            raise Refused("ONBOARD-LIMIT", "at most %d findings" % spec["max"])
        if not value:
            raise Refused("ONBOARD-VALUE", "a finding needs --value")
        data["pointer"] = _pointer_ok(look, pointer)
    elif slot == "unknown":
        if len(answers.get("unknown", [])) >= spec["max"]:
            raise Refused("ONBOARD-LIMIT", "at most %d unknowns" % spec["max"])
        if not value:
            raise Refused("ONBOARD-VALUE", "an unknown needs --value")
    elif slot in ("gate.extra", "gate.drop"):
        data.update(_gate_fields(slot, look, name, argv, proves, kind, timeout, reason, root))
    elif slot == "done-guess" and not value:
        raise Refused("ONBOARD-VALUE", "done-guess needs --value")
    elif slot == "commit-identity" and not card_mod.commit_identity(data.get("value")):
        raise Refused("ONBOARD-VALUE", "commit-identity needs --value in the form: Name <name@host>")
    ev = _append(conn, sid, "onboard-answer", data, actor=ownerword.session_actor(sid))
    line = "answer: %s recorded (%s) as op-0 event %d" % (slot, data["source"], ev["id"])
    out = [line]
    if slot == "finding":
        merged = dict(answers)
        merged.setdefault("finding", []).append(dict(data, event=ev["id"]))
        st = card_mod.settings(look, merged)
        cite = card_mod._Cite(look)
        card_mod._register(cite, look, merged, st)
        out.append("answer: this finding is source [%d] in the card" % cite.extra[("finding", ev["id"], value)])
    if slot == "gate.extra" and data.get("trial"):
        t = data["trial"]
        out.append("answer: tried in the look clone: exit %s, %.2f s" % (t["exit"], t["seconds"]))
    elif slot == "gate.extra":
        out.append("answer: not tried before the yes (%s)" % data["classify"])
    return out


def _sensed_key(look, key):
    return bool(look) and key in look


def _single_opening(conn):
    rows = [r for r in _rows(conn, "onboard-answer") if r["data"].get("slot") == "opening"]
    return rows[-1]


def _answer_opening(conn, root, sid, value, quote, environ):
    text = (quote or value or "").strip()
    if not text:
        raise Refused("ONBOARD-QUOTE", "pass the owner's message with --quote")
    word = _owner_word(conn, root, sid, text, None, environ)
    data = {"slot": "opening", "value": text, "source": "owner" if word else "agent", "quote": text, "owner_word": word,
            "look_event": None, "reason": None if word else "no session transcript to prove it against",
            "setup": store_setup_baseline(root)}
    with db.transaction(conn):
        ev = _append(conn, sid, "onboard-answer", data, actor=ownerword.session_actor(sid), in_txn=True)
        answers = {"opening": dict(data, event=ev["id"])}
        questions = card_mod.next_round(None, answers)
        rev = _append(conn, sid, "onboard-round", {"n": 1, "questions": questions, "look_event": None, "at": util.now_iso_precise()}, in_txn=True)
    guess = card_mod.situation_guess(None, [text])
    return (["answer: no repository named; this is a new project (opening is op-0 event %d)" % ev["id"],
             "situation: %s (%s)" % (guess["situation"], guess["reasons"][0]["text"])]
            + card_mod.format_round(questions, 1)
            + ["recorded op-0 event %d (round 1: %d questions)" % (rev["id"], len(questions))])


def _prove_owner(conn, root, sid, slot, spec, look, answers, *, conn_rounds, value, option, quote, since, environ):
    quote = (quote or "").strip()
    if not quote:
        raise Refused("ONBOARD-QUOTE", "an owner answer needs --quote with the owner's whole message")
    word = _owner_word(conn, root, sid, quote, since, environ)
    if word is None:
        raise Refused("ONBOARD-NO-OWNER-PROOF", "no agent session in the environment, so the owner's message cannot be "
                      "proved; record it with --source agent --reason \"my reading of: ...\"")
    out = {"quote": quote, "owner_word": word}
    if spec["kind"] == "option" and option is not None:
        if option not in spec["options"]:
            raise Refused("ONBOARD-OPTION", "%s takes one of %s" % (slot, ", ".join(spec["options"])))
        if slot == "situation":
            if card_mod.situation_guess(look, [quote])["situation"] != option:
                raise Refused("ONBOARD-OPTION-NOT-IN-REPLY", "the reply does not name the situation %s" % option)
        else:
            num, text = _option_number(conn_rounds, slot, option)
            if num is None:
                # a slot that was never asked (the owner changed a line): the option is backed by the owner's words
                if value is None or not _grab(quote, value):
                    raise Refused("ONBOARD-OPTION-NOT-IN-REPLY", "%s was not a question; pass --value with the words of the "
                                  "owner's message that choose %s" % (slot, option))
            elif not card_mod.option_token_present(quote, num, option, text):
                raise Refused("ONBOARD-OPTION-NOT-IN-REPLY",
                              "the reply holds no token like %s%s and not the option's words; record it with --source "
                              "agent --reason \"my reading of: ...\"" % (num or "<n>", option))
        if value is not None and not _grab(quote, value):
            # the words that name an answer (for merges: who merges) are the owner's, a part of their message
            raise Refused("ONBOARD-NOT-QUOTED", "the value is not a part of the owner's message; pass the owner's own "
                          "words, for example the words that name who merges")
        out["option"] = option
        out["value"] = value
    else:
        if value is None:
            raise Refused("ONBOARD-VALUE", "give --value (a part of the owner's message) or --option")
        if not _grab(quote, value):
            raise Refused("ONBOARD-NOT-QUOTED", "the value is not a part of the owner's message; the goal is quoted, "
                          "never summarized")
        out["value"], out["option"] = value, None
    return out


def _option_number(rounds, slot, key):
    for r in reversed(rounds):
        for q in r["data"].get("questions") or []:
            if q.get("slot") == slot:
                text = next((o["text"] for o in q.get("options") or [] if o["key"] == key), "")
                return q["num"], text
    return None, ""


def _gate_fields(slot, look, name, argv, proves, kind, timeout, reason, root):
    if not name:
        raise Refused("ONBOARD-GATE", "%s needs --name" % slot)
    if slot == "gate.drop":
        return {"name": name, "value": name}
    if not argv:
        raise Refused("ONBOARD-GATE", "gate.extra needs --argv-json, a JSON list of strings")
    if not reason:
        raise Refused("ONBOARD-GATE", "gate.extra needs --reason, the repo file it comes from")
    from alpaca.attach import config
    try:
        errs = config.validate({"repo": "app", "url": "https://example.org/x", "upstream": "origin", "base": "main",
                                "branch_prefix": "alpaca/", "gates": [{"name": name, "command": list(argv),
                                                                        "timeout": int(timeout or 600)}]}, root)
    except (TypeError, ValueError) as exc:
        raise Refused("ONBOARD-GATE", str(exc)) from None
    errs = [e for e in errs if "attach.url" not in e and "attach.repo" not in e]
    if errs:
        raise Refused("ONBOARD-GATE", errs[0])
    gate = {"name": name, "command": list(argv), "timeout": int(timeout or 600)}
    clone = (look or {}).get("clone")
    classify = onboard_look.classify_gate(argv, clone if clone and os.path.isdir(clone) else None, root)
    trial = None
    if classify == "parse" and clone and os.path.isdir(clone):
        trial = onboard_look.trial(clone, gate, root=root)
    return {"name": name, "argv": list(argv), "timeout": int(timeout or 600), "kind": kind or "parse", "proves": proves,
            "classify": classify, "trial": trial, "value": name, "pointer": None}


# ----------------------------------------------------------------------------------------- next

def next_verb(conn, root, sid):
    """The questions still open: the next round for the required slots not answered (up to MAX_REQUIRED_ROUNDS rounds in
    all), else `ready`. Returns (ready, lines)."""
    _need_open(conn, root, "next")
    answers, look, look_id, opening_id = epoch(conn)
    if opening_id is None:
        raise Refused("ONBOARD-NO-LOOK", "run onboard look first")
    rounds = _rounds(conn, opening_id)
    state, got = card_mod.plan_next(look, answers, len(rounds))
    if state == "ready":
        return True, ["ready: run bin/alpaca onboard propose"]
    if state == "stop":
        return False, ["not ready: still missing %s after %d rounds; ask the owner for them in plain words, then record "
                       "them" % (", ".join(got), len(rounds))]
    questions = got
    ev = _append(conn, sid, "onboard-round", {"n": len(rounds) + 1, "questions": questions, "look_event": look_id,
                                                  "at": util.now_iso_precise()})
    return False, card_mod.format_round(questions, len(rounds) + 1) + ["recorded op-0 event %d" % ev["id"]]


# ---------------------------------------------------------------------------------------- cards

def _cards_dir(root):
    return os.path.join(str(root), CARDS)


#: the settings a card records, and the only ones `apply` and `attach init --card` read
SETTING_KEYS = ("situation", "mode", "level", "level_why", "preset", "gates", "branch_prefix", "base", "source",
                "repository", "look_sha", "goal", "what", "limits", "pr_says_agent", "commit_name", "commit_email")
#: keys a card made before the key existed does not hold; `checked_inputs` compares what the stored card has
NEWER_SETTING_KEYS = ("pr_says_agent", "commit_name", "commit_email")


def _write_card_files(root, cid, text, inputs):
    """Store the card text and its inputs. Returns the sha256 of the inputs file: every ask about the card carries
    it, and the owner's yes copies it, so the inputs `apply` reads are the bytes the owner was asked about."""
    from alpaca.attach import safefs
    body = text.encode("utf-8")
    raw = (json.dumps(inputs, indent=1, sort_keys=True) + "\n").encode("utf-8")
    safefs.write_bytes(root, os.path.join(_cards_dir(root), cid + ".md"), body, mode=0o644)
    safefs.write_bytes(root, os.path.join(_cards_dir(root), cid + ".json"), raw, mode=0o644)
    return hashlib.sha256(raw).hexdigest()


def _card_rows(conn):
    return _rows(conn, "onboard-card")


def _latest_card(conn):
    rows = _card_rows(conn)
    return rows[-1] if rows else None


def reply_lines(cid):
    return ['Reply "yes" to set this up as written, or tell me which line to change.',
            "(The long form works too: %s yes)" % cid]


def propose_verb(conn, root, sid, *, reask=False):
    """Render the card, store it and record the ask. Returns (card id, text to print)."""
    _need_open(conn, root, "propose")
    answers, look, look_id, opening_id = epoch(conn)
    if opening_id is None:
        raise Refused("ONBOARD-NO-LOOK", "run onboard look first")
    if reask:
        last = _latest_card(conn)
        if last is None:
            raise Refused("ONBOARD-NEEDS", "there is no card yet to ask about again; run propose")
        cid = last["data"]["id"]
        _stored(root, last)
        prior = ownerword.latest_ask(conn, cid)
        pdata = _json_data(prior) if prior is not None else {}
        isha = pdata.get("inputs_sha256")
        if not isha or _file_sha(os.path.join(str(root), last["data"]["inputs"])) != isha:
            raise Refused("ONBOARD-COPY-CHANGED", "the stored inputs of %s are not the ones the owner was asked about; "
                          "run bin/alpaca onboard propose" % cid)
        ownerword.record_ask(conn, sid, cid, "\n".join(reply_lines(cid)), op=OP,
                             extra=rehearsal({"sha256": last["data"]["sha256"], "inputs_sha256": isha}))
        return cid, "\n".join(reply_lines(cid)) + "\n"
    missing = card_mod.missing_required(look, answers)
    if missing:
        raise Refused("ONBOARD-NEEDS", "missing answers: %s (run bin/alpaca onboard next)" % ", ".join(missing))
    date = util.now_iso()[:10]
    name = _project_name(root, look)
    people = _people_for_card(root, look)
    st0 = card_mod.settings(look, answers, people=people, name=name)
    if st0["mode"] == "attach" and not st0["gates"]:
        raise Refused("ONBOARD-NEEDS", "an attach needs at least one gate and the look found none: record one the repo's "
                      "files point to with answer gate.extra (--name, --argv-json, --reason, --proves), or ask the owner "
                      "for a command that proves the code works")
    marked = card_mod.render_marked(look, answers, date=date, name=name, people=people)
    problems = card_mod.lint(marked, look, answers)             # reads the agent's text only: the owner's words are marked
    if problems:
        raise Refused("ONBOARD-CARD-LINT", "; ".join(problems[:4]))
    text = card_mod.strip_marks(marked)
    cid = card_mod.card_id(text)
    sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
    st = card_mod.settings(look, answers, people=people, name=name)
    inputs = {"schema": 1, "renderer": card_mod.RENDERER_VERSION, "id": cid, "date": date, "name": name, "people": people,
              "look": look, "answers": answers, "look_event": look_id,
              "answer_events": sorted({e["event"] for v in answers.values() for e in (v if isinstance(v, list) else [v])}),
              "settings": {k: st[k] for k in SETTING_KEYS}}
    isha = _write_card_files(root, cid, text, inputs)
    last = _latest_card(conn)
    with db.transaction(conn):
        if last is None or last["data"]["id"] != cid:
            _append(conn, sid, "onboard-card",
                    {"id": cid, "sha256": sha, "copy": os.path.join(CARDS, cid + ".md"),
                     "inputs": os.path.join(CARDS, cid + ".json"), "look_event": look_id,
                     "answer_events": inputs["answer_events"],
                     "supersedes": last["data"]["id"] if last else None}, ref=cid, in_txn=True)
        ownerword.record_ask(conn, sid, cid, "\n".join(reply_lines(cid)), op=OP,
                             extra=rehearsal({"sha256": sha, "inputs_sha256": isha}))
    return cid, text


# --------------------------------------------------------------------------------------- confirm

def _card_event(conn, cid):
    for r in _card_rows(conn):
        if r["data"].get("id") == cid:
            return r
    return None


_READ_CAP = 16 << 20


def _read_regular(path):
    """The bytes of the regular file at `path`, read once, or None. A link is not followed and a pipe, device or
    folder is not read (opening a pipe for reading would wait for ever), so a caller hashes and parses these same
    bytes and no second open can see a file that was swapped in between."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0))
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        with os.fdopen(fd, "rb", closefd=False) as fh:
            data = fh.read(_READ_CAP + 1)
        return data if len(data) <= _READ_CAP else None
    except OSError:
        return None
    finally:
        os.close(fd)


def _stored(root, event):
    path = os.path.join(str(root), event["data"]["copy"])
    data = _read_regular(path)
    if data is None:
        raise Refused("ONBOARD-COPY-CHANGED", "the stored card %s cannot be read" % event["data"]["id"])
    if hashlib.sha256(data).hexdigest() != event["data"]["sha256"]:
        raise Refused("ONBOARD-COPY-CHANGED", "the stored card %s no longer matches its recorded hash" % event["data"]["id"])
    return data.decode("utf-8")


def _json_data(row):
    try:
        data = json.loads(row["data"])
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _file_sha(path):
    data = _read_regular(path)
    return None if data is None else hashlib.sha256(data).hexdigest()


def checked_inputs(conn, root, cid):
    """The inputs of a confirmed card, after proving they are what the owner was asked about: the file hashes to the
    `inputs_sha256` the owner's yes recorded (copied from the ask it answered), and the settings in it follow from
    the look, the answers, the people and the name in it. Raises Refused (ONBOARD-COPY-CHANGED)."""
    ev = _card_event(conn, cid)
    cevs = [r for r in _rows(conn, "onboard-confirmed") if r["data"].get("card") == cid]
    if ev is None or not cevs:
        raise Refused("ONBOARD-NOT-CONFIRMED", "%s is not confirmed by the owner" % cid)
    _stored(root, ev)
    want = cevs[-1]["data"].get("inputs_sha256")
    path = os.path.join(str(root), ev["data"]["inputs"])
    data = _read_regular(path)            # read once: the bytes that are hashed are the bytes that are parsed
    if not want or data is None or hashlib.sha256(data).hexdigest() != want:
        raise Refused("ONBOARD-COPY-CHANGED", "the inputs of %s (%s) are not the bytes the owner was asked about" % (cid, ev["data"]["inputs"]))
    try:
        inputs = json.loads(data.decode("utf-8"))
        got = card_mod.settings(inputs["look"], inputs["answers"], people=inputs["people"], name=inputs["name"])
        keys = [k for k in SETTING_KEYS if k in inputs["settings"] or k not in NEWER_SETTING_KEYS]
        same = json.loads(json.dumps({k: got[k] for k in keys})) == inputs["settings"]
    except (OSError, ValueError, KeyError, TypeError):
        same = False
    if not same:
        raise Refused("ONBOARD-COPY-CHANGED", "the settings in the inputs of %s do not follow from its look and answers" % cid)
    return inputs


def confirm_verb(conn, root, sid, cid, *, quote, environ=None, isatty=None, read=None):
    """Prove the owner's reply to the card. Returns the lines to print. Writes `onboard-confirmed` or
    `onboard-declined`, and no configuration."""
    _need_open(conn, root, "confirm")
    ev = _card_event(conn, cid)
    if ev is None:
        raise Refused("ONBOARD-NO-CARD", "no card %s on the record" % cid)
    last = _latest_card(conn)
    if last["data"]["id"] != cid:
        raise Refused("ONBOARD-SUPERSEDED", "%s was replaced by %s; confirm the newest card" % (cid, last["data"]["id"]))
    text = _stored(root, ev)
    ask = ownerword.latest_ask(conn, cid)
    if ask is None:
        raise Refused("ONBOARD-NOT-ASKED", "the owner has not been asked about %s yet; run propose" % cid)
    isha = _json_data(ask).get("inputs_sha256")
    if not isha or _file_sha(os.path.join(str(root), ev["data"]["inputs"])) != isha:
        raise Refused("ONBOARD-COPY-CHANGED", "the inputs of %s are not the bytes the owner was asked about; run "
                      "bin/alpaca onboard propose" % cid)
    real = ownerword.real_session(environ)
    if real is None:
        print(text)
        word = ownerword.human_write(conn, sid, "owner", subject=cid, answer="yes", quote=quote, root=root,
                                     environ=environ, isatty=isatty, read=read)
        form, answer = "terminal", "yes"
    else:
        word, form, answer = _read_reply(conn, sid, root, cid, quote, ownerword.ask_floor(ask), environ)
        if answer == "yes":
            _card_shown(root, conn, sid, cid, text, word, environ)
    if answer == "no":
        dev = _append(conn, sid, "onboard-declined", {"card": cid, "sha256": ev["data"]["sha256"], "owner_word": word, "form": form},
                      actor="owner", ref=cid)
        return False, ["confirm: %s declined by the owner (op-0 event %d); change a line and run propose again" % (cid, dev["id"])]
    changed, _problem = setup_changes(conn, root)
    with db.transaction(conn):
        cev = _append(conn, sid, "onboard-confirmed",
                      {"card": cid, "sha256": ev["data"]["sha256"], "inputs_sha256": isha, "owner_word": word,
                       "form": form, "setup_changed": changed}, actor="owner", ref=cid, in_txn=True)
        db.meta_set(conn, adopt.FIRST_CHAT, "confirmed")
    line = word.get("line")
    out = ["confirm: %s confirmed by the owner (transcript line %s, form %s); op-0 event %d"
           % (cid, line if line is not None else "none", form, cev["id"])]
    if changed:
        out.append("confirm: these files changed after the look and before this yes: %s. If the owner did not make "
                   "these changes, tell them now." % _paths_text(changed))
    return True, out


REFUSAL_NOT_NEWEST = ("The owner wrote again after answering the card. Read that message first. If it changes "
                      "nothing, ask the owner to say yes again.")
REFUSAL_SKIP_NOT_NEWEST = ("The owner wrote again after `skip setup`. Read that message first. To skip, ask the owner "
                           "to type `skip setup` again on a line of its own.")


def _read_reply(conn, sid, root, cid, quote, since, environ):
    """The owner's reply to the card: the first owner message after the ask is a lone yes or no (a yes may carry the
    assent and filler words of `card.YES_FILLER`, and no other word), or a message says
    `<card> yes` or `<card> no`. Either way it must be the owner's newest message (a message typed after it may take
    it back; ONBOARD-NOT-NEWEST)."""
    attempts = (("sole", "yes", dict(form="sole", phrases=card_mod.SOLE_YES), "sole"),
                ("sole", "yes", dict(form="sole", accepts=card_mod.reads_as_yes), "sole"),
                ("sole", "no", dict(form="sole", phrases=card_mod.SOLE_NO), "sole"),
                ("id", "yes", dict(form="id", strict_ids=True), "id"),
                ("id", "no", dict(form="id", strict_ids=True), "id"))
    for _name, answer, kw, form in attempts:
        try:
            word = ownerword.human_write(conn, sid, "owner", subject=cid, answer=answer, quote=quote, root=root,
                                         since=since, environ=environ, newest=True, **kw)
            return word, form, answer
        except ownerword.OwnerWordRefusal as exc:
            if exc.reason in (ownerword.R_NO_TRANSCRIPT, ownerword.R_SESSION):
                raise
            if exc.reason == ownerword.R_NOT_NEWEST:
                raise Refused("ONBOARD-NOT-NEWEST", REFUSAL_NOT_NEWEST) from None
    raise Refused("ONBOARD-NOT-A-YES", "the reply is not a lone yes after the ask and not `%s yes`; read it as a change or "
                  "a question" % cid)


def _card_shown(root, conn, sid, cid, text, word, environ):
    """The owner saw the card: its bytes, whole, are in the assistant text between the first ask about it and the
    reply. A card that replaced an earlier one may instead be shown as the parts that changed, if the earlier card
    was shown whole and the new id is in the text (the agent then writes only what changed, design section 4)."""
    path = word.get("transcript")
    chain = []
    ev = _card_event(conn, cid)
    while ev is not None:
        chain.append(ev)
        prev = ev["data"].get("supersedes")
        ev = _card_event(conn, prev) if prev else None
    root_ev = chain[-1]
    first_ask = conn.execute("SELECT * FROM events WHERE kind='owner-ask' AND ref=? ORDER BY id LIMIT 1",
                             (root_ev["data"]["id"],)).fetchone()
    texts = (ownerword.assistant_texts(path, since=ownerword.ask_floor(first_ask) if first_ask else None,
                                       until=word.get("timestamp")) if path else [])
    shown = " ".join(card_mod.normalize_shown(t["text"]) for t in texts)
    newest_text = text
    # walk from the root card up: the root must be shown whole; each later card whole, or by its changed parts
    previous = None
    for ev in reversed(chain):
        body = _stored(root, ev)
        whole = card_mod.normalize_shown(body) in shown
        if not whole:
            if previous is None:
                break
            keys = card_mod.changed_parts(previous, body)
            if ev["data"]["id"] not in shown or not all(card_mod.parts(body)[k] in shown for k in keys):
                break
        previous = body
    else:
        return
    raise Refused("ONBOARD-CARD-NOT-SHOWN", "the card is not in the assistant text the owner saw; paste the card unchanged "
                  "as one message (after a change: every part that changed, whole, and the card id), run bin/alpaca "
                  "onboard propose --reask, and ask again")


# ----------------------------------------------------------------------------------------- show

def show_verb(conn, root):
    cevs = _rows(conn, "onboard-confirmed")
    out = []
    if cevs:
        last = cevs[-1]
        ev = _card_event(conn, last["data"]["card"])
        try:
            text = _stored(root, ev)
            out += ["confirmed card %s (op-0 event %d, form %s):" % (last["data"]["card"], last["id"], last["data"].get("form")),
                    text.rstrip("\n")]
        except Refused as exc:
            out.append("changed: %s" % exc)
    else:
        out.append("no card is confirmed yet")
    for d in _rows(conn, "onboard-declined"):
        out.append("declined: %s (op-0 event %d)" % (d["data"]["card"], d["id"]))
    cards = _card_rows(conn)
    for r in cards[:-1]:
        if not any(c["data"].get("card") == r["data"]["id"] for c in cevs):
            out.append("superseded: %s" % r["data"]["id"])
    for level, text in evidence_lines(conn, root):
        if level != "ok":
            out.append("check: %s" % text)
    return out


# ----------------------------------------------------------------------------------------- skip

def _last_first_chat_step(conn):
    """The time of the newest first-chat step (a round of questions or an ask about a card), with microseconds where
    the event has them, or None when the first chat has recorded nothing yet. The owner's skip must be typed after it."""
    best, best_stamp = None, None
    stamps = [r["data"].get("at") or r["ts"] for r in _rows(conn, "onboard-round")]
    for row in conn.execute("SELECT * FROM events WHERE kind=? AND op=?", (ownerword.ASK_KIND, OP)):
        stamps.append(ownerword.ask_floor(row))
    for stamp in stamps:
        moment = ownerword._ts(stamp)
        if moment is not None and (best_stamp is None or moment > best_stamp):
            best, best_stamp = stamp, moment
    return best


def skip_verb(conn, root, sid, quote, environ=None, isatty=None, read=None):
    """Skip the first chat on the owner's word. The word is the owner's newest message, and it is a whole skip phrase
    (`onboard_card.SKIP_PHRASES`) typed after the newest step of the first chat. So a message the owner sent about
    something else, a phrase the owner wrote more after, or one typed before the agent asked never skips the setup.
    Without an agent session the owner types the id at a terminal, as for `confirm` (`read` is for tests)."""
    _need_open(conn, root, "skip")
    quote = (quote or "").strip()
    try:
        if ownerword.real_session(environ) is None:
            word = ownerword.human_write(conn, sid, "owner", subject="onboard-skip", answer="yes", quote=quote, root=root,
                                         environ=environ, isatty=isatty, read=read)
        else:
            word = ownerword.human_write(conn, sid, "owner", subject="onboard-skip", answer="yes", quote=quote, root=root,
                                         form="whole", phrases=card_mod.SKIP_PHRASES, since=_last_first_chat_step(conn),
                                         environ=environ, newest=True)
    except ownerword.OwnerWordRefusal as exc:
        if exc.reason == ownerword.R_NOT_NEWEST:
            raise Refused("ONBOARD-NO-OWNER-PROOF", REFUSAL_SKIP_NOT_NEWEST) from None
        raise Refused("ONBOARD-NO-OWNER-PROOF", "skipping needs the owner's own message in the chat: ask the owner to type "
                      "`skip setup` on a line of its own, then run bin/alpaca onboard skip --quote \"skip setup\" (%s: %s)"
                      % (exc.reason, exc.detail)) from None
    with db.transaction(conn):
        ev = _append(conn, sid, "onboard-skipped", {"owner_word": word, "quote": quote}, actor="owner", in_txn=True)
        db.meta_set(conn, adopt.FIRST_CHAT, "skipped")
    return ["skip: the first chat is skipped (op-0 event %d); run bin/alpaca onboard --name ... by hand when you want setup" % ev["id"]]


# ------------------------------------------------------------------- evidence (read-only, never a refusal)
#
# Design 7.6: the gates stop an agent that runs Alpaca's commands as they are. An agent that hides itself (removes its
# markers, makes a terminal, types the owner's reply at a prompt) or writes the record, the meta table, a transcript or
# a setup file directly is not stopped by code on one machine. For that agent the record and the files are read back
# here, by `doctor` and `onboard show`, and what does not add up is named. Nothing here refuses or writes, except the
# baseline file the opening of a first chat stores (`store_setup_baseline`).

SETUP_DIR = os.path.join(".alpaca", "onboard", "setup")
#: files at the studio root that Alpaca's own views rewrite at any time (pad.write: RESUME.md, CHECKLIST.md and
#: board.json, written by `alpaca status` and the SessionStart hook), so the setup digest leaves them out
VIEW_FILES = ("RESUME.md", "CHECKLIST.md", "board.json")
#: folders at the studio root the digest leaves out: the record, git's own store (its config and exclude file are
#: read), the local interpreter, the attached clone (only whether it exists counts) and the analytics views
#: (alpaca/hook_steps.py `analytics`, run by the hooks and `alpaca analytics build`)
SKIP_TOP_DIRS = (".alpaca", ".git", ".venv", "app", "analytics")
DIGEST_MAX_FILES = 20000
DIGEST_MAX_BYTES = 256 << 20
DIGEST_MAX_ENTRIES = 200000


def _digest_entry(full, budget):
    """One file of the digest, read without following a link and without opening anything but a regular file."""
    try:
        st = os.lstat(full)
    except OSError:
        return "gone"
    mode = st.st_mode
    if stat.S_ISLNK(mode):
        try:
            return "link:" + os.readlink(full)
        except OSError:
            return "link:?"
    if stat.S_ISFIFO(mode):
        return "fifo"
    if stat.S_ISSOCK(mode):
        return "socket"
    if stat.S_ISCHR(mode) or stat.S_ISBLK(mode):
        return "device"
    if not stat.S_ISREG(mode):
        return "other"
    if budget["capped"] or budget["files"] >= DIGEST_MAX_FILES or budget["bytes"] + st.st_size > DIGEST_MAX_BYTES:
        budget["capped"] = True
        return "stat:%d:%d" % (st.st_size, st.st_mtime_ns)
    try:
        fd = os.open(full, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0))
    except OSError:
        return "unreadable"
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return "other"
        h = hashlib.sha256()
        with os.fdopen(fd, "rb", closefd=False) as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
    except OSError:
        return "unreadable"
    finally:
        os.close(fd)
    budget["files"] += 1
    budget["bytes"] += st.st_size
    return h.hexdigest()[:16]


def setup_digest(root):
    """What design 7.1 says the first chat must leave alone, as {"v", "files": {path: digest}, "app", "capped"}:
    every file of the studio outside the folders of SKIP_TOP_DIRS, any `__pycache__` folder and the root files of
    VIEW_FILES, plus `.git/config` and `.git/info/exclude`; `app` is whether `app/` exists. A link is recorded by its
    target and never followed, and a pipe, socket or device by its type, never opened. Past DIGEST_MAX_FILES files or
    DIGEST_MAX_BYTES bytes a file is recorded by its size and time instead of its bytes, and `capped` is set."""
    root = str(root)
    files, budget = {}, {"files": 0, "bytes": 0, "capped": False}
    for dp, dns, fns in os.walk(root):
        top = os.path.normpath(dp) == os.path.normpath(root)
        keep = []
        for d in sorted(dns):
            full = os.path.join(dp, d)
            if d == "__pycache__" or (top and d in SKIP_TOP_DIRS):
                continue
            if os.path.islink(full):
                files[os.path.relpath(full, root)] = _digest_entry(full, budget)
            else:
                keep.append(d)
        dns[:] = keep
        for fn in sorted(fns):
            if top and fn in VIEW_FILES:
                continue
            if len(files) >= DIGEST_MAX_ENTRIES:
                budget["capped"] = True
                dns[:] = []
                break
            full = os.path.join(dp, fn)
            files[os.path.relpath(full, root)] = _digest_entry(full, budget)
    for rel in (".git/config", ".git/info/exclude"):
        full = os.path.join(root, rel)
        if os.path.lexists(full):
            files[rel] = _digest_entry(full, budget)
    return {"v": 1, "files": files, "app": os.path.lexists(os.path.join(root, "app")), "capped": budget["capped"]}


def store_setup_baseline(root):
    """Take the setup digest at the opening of a first chat and keep it under .alpaca/onboard/setup/, named by its
    hash. Returns the small summary the opening event carries ({"v", "sha256", "file", "files", "app", "capped"}):
    the event is chained, so a baseline file changed later no longer matches it. The file is written through the
    link-safe writer the card files use, so a linked folder never sends it outside the studio; when it cannot be
    written the summary carries `error` instead of `file`, the look goes on, and doctor names it."""
    from alpaca.attach import AttachError, safefs
    digest = setup_digest(root)
    raw = json.dumps(digest, sort_keys=True, separators=(",", ":")).encode("utf-8")
    sha = hashlib.sha256(raw).hexdigest()
    rel = os.path.join(SETUP_DIR, "setup-%s.json" % sha[:16])
    out = {"v": 1, "sha256": sha, "files": len(digest["files"]), "app": digest["app"], "capped": digest["capped"]}
    try:
        safefs.write_bytes(root, os.path.join(str(root), rel), raw, mode=0o644)
    except AttachError as exc:
        return dict(out, error=str(exc))
    return dict(out, file=rel)


def _first_chat_floor(conn):
    """The id of the newest provisional `onboard` event (the setup `alpaca new` makes before the first chat), or 0."""
    best = 0
    for r in _rows(conn, "onboard"):
        if r["data"].get("provisional"):
            best = r["id"]
    return best


def _baseline(conn):
    """The opening event that holds the baseline of the current first chat: the EARLIEST opening after the newest
    provisional onboard event. A later look starts a new epoch but never replaces the baseline, so an edit made
    between two looks is still named."""
    floor = _first_chat_floor(conn)
    for r in _rows(conn, "onboard-answer"):
        if r["id"] > floor and r["data"].get("slot") == "opening":
            return r
    return None


def setup_changes(conn, root):
    """(changed paths, problem): the setup files that differ from the baseline of the current first chat, or
    (None, None) when no first chat recorded a baseline, or (None, text) when the baseline cannot be read back."""
    ev = _baseline(conn)
    base = (ev or {}).get("data", {}).get("setup") if ev else None
    if isinstance(base, dict) and base.get("error"):
        return None, ("the record of the setup files could not be kept when the first chat began (op-0 event %d: %s), "
                      "so the files that changed since cannot be listed" % (ev["id"], base["error"]))
    if not isinstance(base, dict) or not base.get("file"):
        return None, None
    data = _read_regular(os.path.join(str(root), str(base["file"])))
    if data is None or hashlib.sha256(data).hexdigest() != base.get("sha256"):
        return None, ("the record of the setup files taken when the first chat began (%s, op-0 event %d) is missing "
                      "or changed, so the files that changed since cannot be listed" % (base["file"], ev["id"]))
    try:
        then = json.loads(data.decode("utf-8"))
        old = then["files"]
    except (ValueError, KeyError, TypeError):
        return None, "the record of the setup files taken when the first chat began cannot be read"
    now = setup_digest(root)
    changed = sorted(k for k in set(old) | set(now["files"]) if old.get(k) != now["files"].get(k))
    if bool(then.get("app")) != now["app"]:
        changed.append("app/")
    return changed, None


def _first_chat_sessions(conn, floor):
    """The sessions that wrote a step of the current first chat (a look, an answer, a card or an ask about one)."""
    out = []
    for r in conn.execute("SELECT id, kind, op, session FROM events WHERE id>? AND kind IN "
                          "('onboard-look','onboard-answer','onboard-card','owner-ask') ORDER BY id", (floor,)):
        if r["kind"] == ownerword.ASK_KIND and r["op"] != OP:
            continue
        if r["session"] and r["session"] not in out:
            out.append(r["session"])
    return out


def _recorded_transcript(conn, sid):
    """The transcript the first `session-start` event of `sid` recorded (the hook writes it for a Claude Code chat),
    or None. Read from the record only, so the answer does not depend on who runs doctor."""
    row = conn.execute("SELECT data FROM events WHERE kind='session-start' AND session=? ORDER BY id LIMIT 1",
                       (sid,)).fetchone()
    data = _json_data(row) if row else {}
    path = data.get("transcript")
    return path if isinstance(path, str) and path.strip() else None


def _reads_back(word):
    """'ok', 'gone' or 'no': whether the transcript line a session-path owner word names still holds that message."""
    path = word.get("transcript")
    if not isinstance(path, str) or not path:
        return "no"
    try:
        st = os.lstat(path)
    except OSError:
        return "gone"
    if not stat.S_ISREG(st.st_mode):
        return "no"
    quote = " ".join(str(word.get("quote") or "").lower().split())
    quote = re.sub(r"[.!]+$", "", quote).strip()
    for m in ownerword.owner_messages(path):
        if m["line"] == word.get("line"):
            return "ok" if quote and (ownerword.is_sole_phrase(m["text"], {quote}) or m["text"].strip() == word.get("quote")) \
                else "no"
    return "no"


_WHAT = {"onboard-confirmed": "yes", "onboard-skipped": "skip"}


def evidence_lines(conn, root):
    """[(level, text)]: what the record and the files say about the first chat that does not add up. Read-only, never
    a refusal (design 7.6). Used by `doctor` and `onboard show`. Empty for a project with no first chat."""
    out = []
    state = adopt.first_chat_state(conn)
    needs = {"skipped": "onboard-skipped", "confirmed": "onboard-confirmed", "done": "onboard-applied"}
    if state in needs:
        rows = _rows(conn, needs[state])
        if state == "done":
            rows = [r for r in rows if r["data"].get("step") == "done"]
        if not rows:
            out.append(("warn", "The first chat is marked %s, but the record has no %s event behind it. Alpaca's own "
                                "commands write the two together; check how this happened." % (state, needs[state])))
    floor = _first_chat_floor(conn)
    chat_sessions = None
    for kind in ("onboard-confirmed", "onboard-skipped"):
        rows = [r for r in _rows(conn, kind) if r["id"] > floor]
        if not rows:
            continue
        last = rows[-1]
        word = last["data"].get("owner_word")
        word = word if isinstance(word, dict) else {}
        what = _WHAT[kind]
        if word.get("path") == "terminal":
            if chat_sessions is None:
                chat_sessions = [s for s in _first_chat_sessions(conn, floor) if _recorded_transcript(conn, s)]
            if chat_sessions:
                out.append(("warn", "The owner's %s (op-0 event %d) was typed at a terminal, while this first chat ran "
                                    "in a Claude Code chat (session %s). There the owner usually types the %s in the "
                                    "chat. If the owner did not type it, say so." % (what, last["id"], chat_sessions[0],
                                                                                    what)))
        elif word.get("path") == "session":
            back = _reads_back(word)
            recorded = _recorded_transcript(conn, last["session"])
            if recorded and os.path.normpath(recorded) != os.path.normpath(str(word.get("transcript") or "")):
                out.append(("warn", "The owner's %s (op-0 event %d) points at a transcript that is not the one its "
                                    "session recorded." % (what, last["id"])))
            elif back == "no":
                out.append(("warn", "The owner's %s (op-0 event %d) points at transcript line %s, which does not hold "
                                    "that message." % (what, last["id"], word.get("line"))))
            elif back == "gone":
                out.append(("ok", "the transcript behind the owner's %s (op-0 event %d) is no longer on disk"
                                  % (what, last["id"])))
        else:
            out.append(("warn", "The owner's %s (op-0 event %d) carries no owner word." % (what, last["id"])))
    if first_chat_open(conn, root):
        changed, problem = setup_changes(conn, root)
        if problem:
            out.append(("warn", problem[0].upper() + problem[1:] + "."))
        elif changed:
            out.append(("warn", "These files changed after the first chat began: %s." % _paths_text(changed)))
    return out


def _paths_text(paths, limit=12):
    shown = ", ".join(paths[:limit])
    return shown + (" and %d more" % (len(paths) - limit) if len(paths) > limit else "")


# ----------------------------------------------------------------------------------------- apply

def _sha_file(path):
    try:
        with open(path, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()[:12]
    except OSError:
        return None


def _applied(conn, cid):
    return {r["data"].get("step"): r for r in _rows(conn, "onboard-applied") if r["data"].get("card") == cid}


def apply_verb(conn, root, sid, cid, *, environ=None):
    """Configure from the confirmed card. Returns (ok, lines). A second run resumes after a failed step; a run
    after success writes nothing."""
    state = _state(conn, root)
    cevs = [r for r in _rows(conn, "onboard-confirmed") if r["data"].get("card") == cid]
    if not cevs:
        raise Refused("ONBOARD-NOT-CONFIRMED", "%s is not confirmed by the owner; run bin/alpaca onboard confirm" % cid)
    last = _latest_card(conn)
    if last is None or last["data"]["id"] != cid:
        raise Refused("ONBOARD-SUPERSEDED", "%s is not the newest card" % cid)
    ev = _card_event(conn, cid)
    _stored(root, ev)
    done = _applied(conn, cid)
    if "done" in done:
        return True, ["apply: already applied (op-0 event %d); nothing written" % done["done"]["id"]]
    if state not in ("fresh", "due", "confirmed"):
        raise Refused("ONBOARD-NOT-OPEN", "apply: the first chat is %s" % state)
    inputs = checked_inputs(conn, root, cid)
    st = inputs["settings"]
    lines = []
    first = {}
    if not done:
        # the first step of the first run only: a resumed apply would see its own writes
        changed, _problem = setup_changes(conn, root)
        first = {"setup_changed": changed}
        if changed:
            lines.append("apply: these files changed after the look and before this setup: %s. If the owner did not "
                         "make these changes, tell them now." % _paths_text(changed))
    steps = (("config", _step_config), ("attach", _step_attach), ("preset", _step_preset), ("note", _step_note),
             ("level", _step_level), ("scratch", _step_scratch))
    for step, fn in steps:
        if step in done:
            lines.append("apply: %s ... done earlier (op-0 event %d)" % (step, done[step]["id"]))
            continue
        try:
            result, text = fn(conn, root, sid, cid, inputs, st, environ)
        except Refused:
            raise
        except Exception as exc:                                    # noqa: BLE001 - named with its step
            raise Refused("ONBOARD-APPLY-STEP", "step %s failed: %s; run apply again to resume" % (step, exc)) from None
        _append(conn, sid, "onboard-applied", dict({"card": cid, "step": step, "result": result}, **first))
        first = {}
        lines.append("apply: %s" % text)
    dev = _append(conn, sid, "onboard-applied", {"card": cid, "step": "done", "mode": st["mode"], "level": st["level"],
                                                   "date": util.now_iso()[:10]})
    db.meta_set(conn, adopt.FIRST_CHAT, "done")
    lines.append("apply: recorded op-0 event %d (onboard-applied)" % dev["id"])
    return True, lines


def _step_config(conn, root, sid, cid, inputs, st, environ):
    from alpaca import onboard
    people = inputs.get("people") or []
    before = _sha_file(project.path(root))
    if not project.is_onboarded(root):
        clean = lambda t: " ".join(str(t).replace(",", " ").replace(":", " ").split()) or "owner"          # noqa: E731
        who = ",".join("%s:%s" % (clean(p["name"]), clean(p.get("role") or "member")) for p in people) or "owner:owner"
        res = onboard.write_config(conn, root, sid, {
            "name": inputs["name"], "who": who, "what": st["what"] or inputs["name"], "tasks": [], "tier": None,
            "preset": None, "existing_repo": st["mode"] == "attach", "default_level": st["level"]}, provisional=False)
        if res.get("error"):
            raise Refused("ONBOARD-APPLY-STEP", "step config failed: %s" % res["error"])
        text = "onboarded %s as %s at level %s (project.yaml %s -> %s)" % (inputs["name"], who, st["level"], before,
                                                                          _sha_file(project.path(root)))
        return {"kind": "first-write", "sha_before": before, "sha_after": _sha_file(project.path(root))}, text
    prov = [r for r in _rows(conn, "onboard") if r["data"].get("provisional")]
    res = onboard.revise_config(conn, root, sid, {"what": st["what"] or None, "people": people or None,
                                                  "default_level": st["level"],
                                                  "existing_repo": st["mode"] == "attach"}, card=cid,
                                revises=prov[-1]["id"] if prov else None)
    after = _sha_file(project.path(root))
    return ({"kind": "revision", "changes": res["changes"], "sha_before": before, "sha_after": after},
            "onboard revision of the provisional setup: %s (project.yaml %s -> %s)"
            % (", ".join(sorted(res["changes"])) or "no change", before, after))


def _step_attach(conn, root, sid, cid, inputs, st, environ):
    if st["mode"] != "attach":
        return {"skipped": "native mode"}, "attach ... skipped (native mode: the code lives in the studio)"
    from alpaca.attach import config, init
    if config.raw(str(root)) is not config.MISSING:
        cfg = config.mode(str(root))
        if cfg is not None and cfg.url.rstrip("/") != str(st["source"]).rstrip("/"):
            raise Refused("ONBOARD-ATTACH-MISMATCH", "this studio is already attached to %s, not to %s"
                          % (cfg.url, st["source"]))
        return {"skipped": "already attached"}, "attach ... already attached by alpaca new --attach; kept as is"
    look = inputs.get("look") or {}
    reference = look.get("clone") if look.get("clone") and os.path.isdir(look.get("clone", "")) else None
    res = init.init_verb(conn, root, sid, st["source"], card=cid, reference=reference)
    return ({"head": res["head"], "look_sha": st["look_sha"], "same_as_look": res["head"] == st["look_sha"]},
            "attach init %s --card %s ... app/ at %s%s" % (st["source"], cid, res["head"][:7],
                                                            " (same as the look)" if res["head"] == st["look_sha"]
                                                            else " (the upstream moved since the look; gates stay as confirmed)"))


def _step_preset(conn, root, sid, cid, inputs, st, environ):
    if not st["preset"]:
        return {"skipped": "off"}, "style: plain-writing off (as the card said)"
    from alpaca.gates import literal_guard as lg
    preset = "plain-writing"
    dst = os.path.join(str(root), "style", "presets", "%s-ban-list.md" % preset)
    if not os.path.isfile(dst):
        src = lg.shipped_preset_path(preset)
        if os.path.isfile(src):
            util.write_text(dst, util.read_text(src))
    cfg = project.load(root)
    style = dict(cfg.get("style") or {})
    presets = list(style.get("presets") or [])
    if preset not in presets:
        presets.append(preset)
    style["presets"] = presets
    cfg["style"] = style
    project.save(root, cfg)
    return {"preset": preset}, "style: plain-writing on"


def _step_note(conn, root, sid, cid, inputs, st, environ):
    from alpaca import note
    goal = st.get("goal") or ""
    if not goal.strip():
        return {"skipped": "no goal"}, "goal note ... none"
    res = note.add(str(root), goal.encode("utf-8"), "owner", by="owner", session=sid)
    queue = os.path.join(str(root), "intents", "queue.md")
    line = "- [ ] %s" % " ".join(goal.split())
    text = util.read_text(queue) if os.path.isfile(queue) else ""
    if line not in text:
        text = text.replace("- [ ] (nothing yet)\n", "")
        text = text + ("" if text.endswith("\n") or not text else "\n") + line + "\n"
        util.write_text(queue, text)
    return ({"note": res["file"], "sha256": res["sha256"], "new": res["new"]},
            "goal kept as note %s (owner words, byte for byte); intents/queue.md: 1 line" % res["file"])


def _step_level(conn, root, sid, cid, inputs, st, environ):
    from alpaca.posture import level
    if level._latest_decision(conn, sid) is not None:
        return {"kept": True}, "session level: kept (the session already has one; default_level is %s)" % st["level"]
    rec = level.set(conn, sid, int(st["level"][1]), "setup %s: %s" % (cid, (st.get("what") or "")[:80]), root=str(root))
    return {"decision": rec.get("id")}, "session level: %s recorded (the session had none)" % st["level"]


def _step_scratch(conn, root, sid, cid, inputs, st, environ):
    scratch = (inputs.get("look") or {}).get("scratch")
    if scratch and onboard_look.remove_scratch_clone(scratch):
        return {"removed": scratch}, "scratch copy removed"
    return {"removed": None}, "scratch copy ... none to remove"


# -------------------------------------------------------------------------------------- the CLI

def _fail(args, refused):
    print("GATE alpaca-onboard: FAIL (%s)" % refused)
    return cli.FAIL


def _change_hint(conn, root, cid):
    """For a card that replaced one: which parts changed. For the agent, not the owner (stderr)."""
    ev = _card_event(conn, cid)
    prev = ev["data"].get("supersedes") if ev else None
    pev = _card_event(conn, prev) if prev else None
    if pev is None:
        return None
    keys = card_mod.changed_parts(_stored(root, pev), _stored(root, ev))
    return ("propose: this card replaces %s. Parts that changed: %s. The owner has seen the old card whole, so you may show "
            "just those parts, whole, and the new card id (%s), then the reply line." % (prev, ", ".join(str(k) for k in keys) or "none", cid))


def add_subparsers(p):
    sp = p.add_subparsers(dest="onboard_verb")
    a = sp.add_parser("look", help="read a repository before asking anything; writes nothing in the studio")
    a.add_argument("source", help="git URL or folder path the owner named")
    a.add_argument("--scratch", required=True, help="a folder outside the studio for the read-only copy")
    a.add_argument("--quote", required=True, help="the owner's whole first message")
    a = sp.add_parser("answer", help="record one answer of the first chat")
    a.add_argument("slot")
    a.add_argument("--value")
    a.add_argument("--option")
    a.add_argument("--quote", help="the owner's whole message the answer comes from")
    a.add_argument("--source", choices=["owner", "agent", "sensed", "default"])
    a.add_argument("--reason")
    a.add_argument("--pointer", help="repo:<path>[:<first>-<last>] for a finding")
    a.add_argument("--name", help="gate name (gate.extra, gate.drop)")
    a.add_argument("--argv-json", dest="argv_json", help="gate command as a JSON list of strings")
    a.add_argument("--proves", help="one plain sentence: what the gate passing proves")
    a.add_argument("--kind", help="gate kind: parse, configure, build, test, lint")
    a.add_argument("--timeout", type=int)
    sp.add_parser("next", help="the questions still open, or ready")
    a = sp.add_parser("propose", help="render the setup card and record the ask")
    a.add_argument("--reask", action="store_true", help="ask again about the newest card, same bytes")
    a = sp.add_parser("confirm", help="the owner's typed yes to a card")
    a.add_argument("card")
    a.add_argument("--quote", help="the owner's whole reply")
    a = sp.add_parser("apply", help="configure from a confirmed card")
    a.add_argument("card")
    sp.add_parser("show", help="print the confirmed card")
    a = sp.add_parser("skip", help="skip the first chat on the owner's word")
    a.add_argument("--quote", required=True)


def run(args, root, conn) -> int:
    verb = args.onboard_verb
    sid = args.session or "cli"
    try:
        if verb == "look":
            lines = look_verb(conn, root, sid, args.source, scratch=args.scratch, quote=args.quote)
        elif verb == "answer":
            argv = None
            if args.argv_json:
                try:
                    argv = json.loads(args.argv_json)
                except ValueError:
                    raise Refused("ONBOARD-GATE", "--argv-json is not valid JSON") from None
                if not isinstance(argv, list) or not all(isinstance(x, str) for x in argv):
                    raise Refused("ONBOARD-GATE", "--argv-json must be a JSON list of strings")
            lines = answer_verb(conn, root, sid, args.slot, value=args.value, option=args.option, quote=args.quote,
                                source=args.source, reason=args.reason, pointer=args.pointer, name=args.name, argv=argv,
                                proves=args.proves, kind=args.kind, timeout=args.timeout)
        elif verb == "next":
            ready, lines = next_verb(conn, root, sid)
            for line in lines:
                print(line)
            return cli.PASS if ready else cli.FAIL
        elif verb == "propose":
            cid, text = propose_verb(conn, root, sid, reask=args.reask)
            print(text.rstrip("\n"))
            hint = _change_hint(conn, root, cid) if not args.reask else None
            if hint:
                print(hint, file=sys.stderr)
            return cli.PASS
        elif verb == "confirm":
            ok, lines = confirm_verb(conn, root, sid, args.card, quote=args.quote)
            for line in lines:
                print(line)
            return cli.PASS if ok else cli.FAIL
        elif verb == "apply":
            ok, lines = apply_verb(conn, root, sid, args.card)
            for line in lines:
                print(line)
            print("GATE alpaca-onboard: PASS (applied %s)" % args.card)
            return cli.PASS
        elif verb == "show":
            lines = show_verb(conn, root)
        elif verb == "skip":
            lines = skip_verb(conn, root, sid, args.quote)
        else:
            return cli.USAGE
    except Refused as exc:
        return _fail(args, exc)
    except ownerword.OwnerWordRefusal as exc:
        return _fail(args, "%s %s" % (exc.reason, exc.detail))
    except Exception as exc:                                       # noqa: BLE001 - the verbs name every failure
        from alpaca.attach import AttachError
        if isinstance(exc, AttachError):
            return _fail(args, "ATTACH %s" % exc)
        raise
    for line in lines:
        print(line)
    return cli.PASS
