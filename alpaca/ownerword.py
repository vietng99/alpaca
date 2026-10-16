"""Owner words: proof that the owner, and not an agent, said a thing (op-019 t-201, design
design/intent-fidelity/design.md section 1).

Before this module every owner-only write trusted a token: `--by owner` on a review card, an
`actor` on a decision, the fixed actor `human` on `task add`. An agent could write all three, which
is how an agent-written task statement was counted as the owner's (brief R3) and how silence was
counted as consent (brief R1). Every owner-only write now calls `human_write`, which returns the
`owner_word` row the caller stores on its event, or raises OwnerWordRefusal. Two paths satisfy it.

TERMINAL PATH (1.1). No agent marker in the environment, stdin is a terminal, and the person types
the subject id when asked (alpaca.research.owner.confirm_at_terminal). A `--session cli` flag does
not open this path: the environment and the TTY do.

SESSION PATH (1.2). The caller passes `quote`, the whole text of one owner message in the session's
transcript (or, in plain quote mode, a substring of one), and the message must carry the answer:
  * an owner message is one of two entry shapes Claude Code writes: a typed prompt (`type` user,
    `origin.kind` human, text a string or text blocks, never a tool_result) or a message typed
    while the agent is mid-turn (`type` attachment, `attachment.type` queued_command,
    `attachment.commandMode` prompt, `attachment.origin.kind` human). Neither may be isMeta,
    isCompactSummary or isSidechain, and `entrypoint` must not start with `sdk` (a `claude -p`
    run). An entry with no `origin` never counts. A missing `entrypoint` is allowed: the rule
    refuses sdk entries, it does not demand the field.
  * it was typed after the subject existed (`since`, the timestamp of the record event that
    created the subject or asked about it) and, for a batch item id, before the asking session's
    next ask for another op (`until`).
  * id form: the subject id sits at a token boundary (case-insensitive, so `q1` never matches in
    `q10`); its segment runs to the next subject id, `,`, `;` or line end; a segment with `?`
    answers nothing; `yes` is the id, an optional `:` and one assent word, `no` the id, an
    optional `:` and one refusal word, a text answer the id, `:` and the text. One trailing `.` or
    `!` is tolerated: the design says "exactly", a typed full stop is not a different answer.
    Anything else answers nothing ("runtime ok but not headless" is not a yes).
  * batch form: the first owner message after the ask, lower-cased, trimmed and with end
    punctuation dropped, is exactly `all yes`, `yes all`, `yes to all` or `all ok`.
  * session binding: the session of an ask and of an answer is the REAL session, the one in the
    environment before `cli.main` lets `--session` overwrite ALPACA_SESSION_ID (`capture`). A
    `--session` that differs from it is refused (OWNER-WORD-SESSION). With no environment session
    the terminal path applies.
The transcript must be the one the session's first `session-start` event recorded, at
`<root>/projects/<slug>/<sid>.jsonl` where `<root>` is ~/.claude, $CLAUDE_CONFIG_DIR or a folder
directly under /srv/claude-accounts/.

Refusal tokens: OWNER-WORD-NOT-HUMAN, OWNER-WORD-NO-TRANSCRIPT, OWNER-WORD-NOT-FOUND,
OWNER-WORD-UNBOUND, OWNER-WORD-NOT-TERMINAL, OWNER-WORD-SESSION, OWNER-WORD-NOT-NEWEST,
OWNER-WORD-LEVEL-RAISE (a session level raise with no owner `/autodrive` call for that level,
alpaca.posture.level.owner_raise).

HONEST LIMITS (design section 8). The same Unix user can write any file, a transcript included,
and the record database directly; owner words prove the text sits in the file Claude Code writes
for the session, at the place it writes it, not who typed it. A Codex session has another
transcript format, so it records owner words only at the terminal. A module-level caller can pass
any `owner_word` to an event it writes itself; `decision_is_owner` re-reads the transcript for the
session path, but the terminal path leaves no file to re-read.
"""
from __future__ import annotations

import datetime
import json
import os
import re
import sys

from alpaca import db, paths, util
from alpaca.gates import verdict as vc

AGENT_MARKERS = ("CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "ALPACA_SESSION_ID", "CODEX_THREAD_ID",
                 "CODEX_SANDBOX")
#: the environment variables that name the real session, in order. Claude Code exports the first
#: into every Bash call; the SessionStart hook exports the second.
REAL_SESSION_VARS = ("CLAUDE_CODE_SESSION_ID", "ALPACA_SESSION_ID")
HUMAN_IDENTITIES = frozenset({"owner", "human"})
ASSENT = ("yes", "ok", "okay", "accept", "approve", "approved", "agree", "fine", "go", "signed")
REFUSAL = ("no", "reject", "decline")
BATCH_PHRASES = frozenset({"all yes", "yes all", "yes to all", "all ok"})
ACCOUNTS_ROOT = "/srv/claude-accounts"

R_NOT_HUMAN = "OWNER-WORD-NOT-HUMAN"
R_NO_TRANSCRIPT = "OWNER-WORD-NO-TRANSCRIPT"
R_NOT_FOUND = "OWNER-WORD-NOT-FOUND"
R_UNBOUND = "OWNER-WORD-UNBOUND"
R_NOT_TERMINAL = "OWNER-WORD-NOT-TERMINAL"
R_SESSION = "OWNER-WORD-SESSION"
R_NOT_NEWEST = "OWNER-WORD-NOT-NEWEST"
#: a level raise with no owner `/autodrive` call for exactly that level (alpaca.posture.level)
R_LEVEL_RAISE = "OWNER-WORD-LEVEL-RAISE"

#: ids of the record that are not batch items, so a segment stops at them
_GENERIC_ID = r"(?:q\d+|ord-\d+|rc-\d+|dec-[0-9a-f]{12})"


class OwnerWordRefusal(ValueError):
    """An owner write refused. `reason` is one of the OWNER-WORD-* tokens, `code` a verdict-band
    exit code. A ValueError, so a verb that already prints a ValueError as a GATE FAIL line prints
    this one the same way."""

    def __init__(self, reason, detail="", code=vc.FAIL):
        super().__init__("%s: %s" % (reason, detail))
        self.reason, self.detail, self.code = reason, detail, code


# ------------------------------------------------------------------ the real session
_CAPTURED = None


def _real_of(environ):
    session = None
    for name in REAL_SESSION_VARS:
        value = (environ.get(name) or "").strip()
        if value:
            session = value
            break
    sessions = []
    for name in REAL_SESSION_VARS:
        value = (environ.get(name) or "").strip()
        if value and value not in sessions:
            sessions.append(value)
    return {"session": session, "sessions": sessions,
            "markers": [m for m in AGENT_MARKERS if environ.get(m)]}


def capture(environ=None):
    """Remember the real session and the agent markers of the environment as the CLI found them.
    `cli.main` calls this before `--session` overwrites ALPACA_SESSION_ID, so `--session cli` or a
    made-up id cannot hide the session the process runs in."""
    global _CAPTURED
    _CAPTURED = _real_of(os.environ if environ is None else environ)
    return _CAPTURED


def release():
    """Forget the captured environment (cli.main calls this when the verb returns)."""
    global _CAPTURED
    _CAPTURED = None


def _real(environ=None):
    if environ is not None:
        return _real_of(environ)
    return _CAPTURED if _CAPTURED is not None else _real_of(os.environ)


def real_session(environ=None):
    """The session the process really runs in, or None when no session is in the environment."""
    return _real(environ)["session"]


def real_sessions(environ=None):
    """Every session id the environment named before the CLI touched it (CLAUDE_CODE_SESSION_ID and
    ALPACA_SESSION_ID), in that order, without repeats. A gate that reads the level in force takes
    the highest over these and the session the call names (design section 3.3)."""
    return list(_real(environ).get("sessions") or [])


def agent_markers(environ=None):
    return list(_real(environ)["markers"])


def _stdin_is_tty():
    """True when this process's standard input is a terminal. The one place tests replace (an in-process patch;
    no environment variable switches it, because a variable is a switch an agent can also set)."""
    try:
        return bool(sys.stdin.isatty())
    except (OSError, ValueError, AttributeError):
        return False


def owner_terminal(environ=None, isatty=None):
    """True when the process runs in the owner's own terminal: no session id and no agent marker in the environment
    as the CLI found it, and standard input is a terminal. These are the three things the terminal path of
    `human_write` needs (the module text, TERMINAL PATH). A caller that wipes the markers but has no terminal (a
    child process of an agent, with stdin from /dev/null or a pipe) is not the owner's terminal."""
    if real_session(environ) is not None or agent_markers(environ):
        return False
    return bool((isatty or _stdin_is_tty)())


def agent_call(environ=None, isatty=None):
    """True when the call is not from the owner's own terminal (see `owner_terminal`): an agent session, or a
    process with no terminal. The setup writers (flat onboard, plain attach init, new --attach) treat these alike."""
    return not owner_terminal(environ, isatty)


def require_real_session(session, environ=None):
    """Refuse (OWNER-WORD-SESSION) a `session` that differs from the real one. With no environment
    session there is nothing to differ from; the terminal path then applies to the answer."""
    real = real_session(environ)
    if real is not None and session != real:
        raise OwnerWordRefusal(R_SESSION, "this call runs in session %s but names session %s; the "
                               "owner's session is the one in the environment" % (real, session))
    return real


def session_actor(session, environ=None):
    """The actor a write records: the session when the call runs inside an agent session (an agent
    marker in the environment, or a session other than `cli`), else `human`, the person at a
    plain terminal. Replaces the fixed `human` that task add wrote whoever ran it."""
    named = (session or "").strip()
    inside = bool(agent_markers(environ)) or real_session(environ) is not None
    if named in HUMAN_IDENTITIES and inside:
        # `--session human` is a name an agent can type, not the owner: inside an agent session
        # the actor is the session really running, never a human identity.
        return real_session(environ) or "agent"
    if named and named != "cli":
        return named
    if agent_markers(environ):
        return "agent"
    return "human"


# ------------------------------------------------------------------ transcript and messages
def _ts(value):
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.astimezone()
    return moment


def project_slug(root):
    """The folder name Claude Code gives a project: its root with every character that is not a
    letter, a digit or `-` as `-` (so `_`, a space and `.` too, not only `/` and `.`). A narrower
    rule left a root with `_` in it without a findable transcript (derived choice, t-201 r1)."""
    return re.sub(r"[^A-Za-z0-9-]", "-", str(root))


def _config_dirs(environ=None):
    env = os.environ if environ is None else environ
    out = [os.path.normpath(os.path.expanduser("~/.claude"))]
    cfg = (env.get("CLAUDE_CONFIG_DIR") or "").strip()
    if cfg:
        out.append(os.path.normpath(cfg))
    return out


def transcript_path(conn, sid, root, environ=None):
    """The transcript file of session `sid`, or OWNER-WORD-NO-TRANSCRIPT. The path in the session
    row must equal the one its first `session-start` event recorded (a repointed transcript is
    refused) and must sit where Claude Code writes it. The snapshot under .alpaca/transcripts/
    never carries owner words and is never read here."""
    row = conn.execute("SELECT transcript FROM sessions WHERE sid=?", (sid,)).fetchone()
    path = (row["transcript"] if row else None) or ""
    if not path:
        raise OwnerWordRefusal(R_NO_TRANSCRIPT, "session %s has no recorded transcript" % sid)
    first = conn.execute("SELECT data FROM events WHERE kind='session-start' AND session=? "
                         "ORDER BY id LIMIT 1", (sid,)).fetchone()
    try:
        recorded = (json.loads(first["data"]) or {}).get("transcript") if first else None
    except (TypeError, ValueError):
        recorded = None
    if not recorded or os.path.normpath(recorded) != os.path.normpath(path):
        raise OwnerWordRefusal(R_NO_TRANSCRIPT, "the transcript of session %s is not the path its "
                               "first session-start recorded" % sid)
    norm = os.path.normpath(path)
    base = os.path.dirname(os.path.dirname(os.path.dirname(norm)))
    allowed = base in _config_dirs(environ) or os.path.dirname(base) == ACCOUNTS_ROOT
    expect = os.path.join(base, "projects", project_slug(root), "%s.jsonl" % sid)
    if not allowed or norm != expect:
        raise OwnerWordRefusal(R_NO_TRANSCRIPT, "the transcript of session %s is not at "
                               "<config>/projects/%s/%s.jsonl" % (sid, project_slug(root), sid))
    if not os.path.isfile(norm):
        raise OwnerWordRefusal(R_NO_TRANSCRIPT, "no transcript file at %s" % norm)
    return norm


def _entry_text(entry):
    """The owner text of one transcript entry (design 1.2), or None when it is not an owner
    message."""
    if not isinstance(entry, dict):
        return None
    if entry.get("isMeta") or entry.get("isCompactSummary") or entry.get("isSidechain"):
        return None
    entrypoint = entry.get("entrypoint")
    if isinstance(entrypoint, str) and entrypoint.startswith("sdk"):
        return None
    kind = entry.get("type")
    if kind == "user":
        origin = entry.get("origin")
        if not isinstance(origin, dict) or origin.get("kind") != "human":
            return None
        message = entry.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            texts = []
            for block in content:
                if not isinstance(block, dict) or block.get("type") == "tool_result":
                    return None
                if block.get("type") == "text" and isinstance(block.get("text"), str):
                    texts.append(block["text"])
            return "\n".join(texts) if texts else None
        return None
    if kind == "attachment":
        att = entry.get("attachment")
        if not isinstance(att, dict) or att.get("type") != "queued_command":
            return None
        if att.get("commandMode") != "prompt":
            return None
        origin = att.get("origin")
        if not isinstance(origin, dict) or origin.get("kind") != "human":
            return None
        return att["prompt"] if isinstance(att.get("prompt"), str) else None
    return None


def owner_messages(path, contains=None):
    """Every owner message of the transcript at `path`, in file order: {line, timestamp, text}.
    With `contains`, only the lines whose raw bytes hold that text are parsed: a cheap filter for a
    caller that looks for one command in a long transcript (the posture mirror runs per tool call)."""
    out = []
    try:
        fh = open(path, encoding="utf-8", errors="replace")
    except OSError:
        return out
    with fh:
        for number, raw in enumerate(fh, 1):
            if '"human"' not in raw or (contains and contains not in raw):
                continue
            try:
                entry = json.loads(raw)
            except ValueError:
                continue
            text = _entry_text(entry)
            if text is None:
                continue
            stamp = entry.get("timestamp")
            if stamp is None and isinstance(entry.get("attachment"), dict):
                stamp = entry["attachment"].get("timestamp")
            out.append({"line": number, "timestamp": stamp, "text": text})
    return out


def _assistant_text(entry):
    """The text of one assistant entry of the transcript, or None when it is not one: Claude Code
    writes each content block of an answer as its own `assistant` line, so a long message is several
    entries. A sidechain (subagent) entry never counts."""
    if not isinstance(entry, dict) or entry.get("type") != "assistant":
        return None
    if entry.get("isSidechain") or entry.get("isMeta") or entry.get("isCompactSummary"):
        return None
    message = entry.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return None
    texts = [b["text"] for b in content
             if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str)]
    return "\n".join(texts) if texts else None


def assistant_texts(path, since=None, until=None):
    """Every assistant text entry of the transcript at `path`, in file order, typed after `since` and
    before `until` (both optional, strictly): {line, timestamp, text}."""
    out = []
    try:
        fh = open(path, encoding="utf-8", errors="replace")
    except OSError:
        return out
    with fh:
        for number, raw in enumerate(fh, 1):
            if '"assistant"' not in raw:
                continue
            try:
                entry = json.loads(raw)
            except ValueError:
                continue
            text = _assistant_text(entry)
            if text is None:
                continue
            row = {"line": number, "timestamp": entry.get("timestamp"), "text": text}
            if _after(row, since, until):
                out.append(row)
    return out


def session_messages(conn, sid, root, environ=None):
    """(transcript path, owner messages) of session `sid` under the transcript rules."""
    path = transcript_path(conn, sid, root, environ)
    return path, owner_messages(path)


def typed_at(message):
    """When owner message `message` was typed, as an aware datetime, or None when unstamped."""
    return _ts(message.get("timestamp"))


def typed_after(message, since, until=None):
    """True when owner message `message` was typed after `since` and, when given, before
    `until` (both ISO timestamps or None)."""
    return _after(message, since, until)


def _after(message, since, until):
    """True when the message was typed after `since` and, when given, before `until`."""
    stamp = _ts(message["timestamp"])
    if since is None and until is None:
        return True
    if stamp is None:
        return False
    if since is not None:
        floor = _ts(since)
        if floor is not None and not stamp > floor:
            return False
    if until is not None:
        ceiling = _ts(until)
        if ceiling is not None and not stamp < ceiling:
            return False
    return True


def _word_count(text):
    return len(text.split())


def find_quote(conn, sid, text, root, *, since=None, until=None, min_words=1, environ=None):
    """The owner_word row for `text` as a substring of one owner message of session `sid`, or
    OWNER-WORD-NO-TRANSCRIPT / OWNER-WORD-NOT-FOUND. Quoting needs no answer form: the text is the
    quote (owner-quote sources and orders). The session need not be the caller's own."""
    needle = (text or "").strip()
    if not needle:
        raise OwnerWordRefusal(R_NOT_FOUND, "an empty quote matches no owner message")
    if _word_count(needle) < min_words:
        raise OwnerWordRefusal(R_UNBOUND, "the quote has %d words; at least %d are needed"
                               % (_word_count(needle), min_words))
    path, messages = session_messages(conn, sid, root, environ)
    for m in messages:
        if needle in m["text"] and _after(m, since, until):
            return {"path": "session", "transcript": path, "line": m["line"],
                    "timestamp": m["timestamp"], "quote": needle}
    raise OwnerWordRefusal(R_NOT_FOUND, "no owner message of session %s holds that text%s"
                           % (sid, " typed in the allowed window" if (since or until) else ""))


# ------------------------------------------------------------------ answer forms
def _segments(text, subject, others, strict=False):
    """The stretches of `text` that start at `subject`. Plain mode (interviews) ends a stretch at a
    comma, a semicolon or any other id. Strict mode (batch items) ends it at a semicolon, a generic
    id, or another id only where that id starts an answer (`id:` or `id yes|no`): the six class ids
    are common words, and a comma or a class word inside an answer must not cut it or leave a
    qualifier (`runtime yes, but run it headless`) outside the stretch that is read."""
    pat = re.compile(r"(?<![\w-])%s(?![\w-])" % re.escape(subject), re.I)
    if strict:
        words = "|".join(re.escape(w) for w in tuple(ASSENT) + tuple(REFUSAL))
        start = r"(?=\s*:|\s+(?:%s)(?![\w-]))" % words
        stops = [r";", r"(?<![\w-])%s(?![\w-])" % _GENERIC_ID]
        stops += [r"(?<![\w-])%s(?![\w-])%s" % (re.escape(o), start)
                  for o in others if o and o != subject]
    else:
        stops = [r"[,;]", r"(?<![\w-])%s(?![\w-])" % _GENERIC_ID]
        stops += [r"(?<![\w-])%s(?![\w-])" % re.escape(o) for o in others if o and o != subject]
    stop = re.compile("|".join(stops), re.I)
    for line in text.splitlines():
        for m in pat.finditer(line):
            cut = stop.search(line, m.end())
            seg = line[m.start(): cut.start() if cut else len(line)].strip()
            yield seg.rstrip(",").strip() if strict else seg


def read_answer(text, subject, others=(), extra_yes=(), strict=False):
    """What message `text` answers for `subject` in the id form: ("yes", None), ("no", None),
    ("text", the text) or None. More than one distinct answer reads as none."""
    yes = "|".join(re.escape(w) for w in tuple(ASSENT) + tuple(extra_yes))
    no = "|".join(re.escape(w) for w in REFUSAL)
    head = re.escape(subject)
    found = set()
    for seg in _segments(text, subject, others, strict):
        if "?" in seg:
            continue
        if re.fullmatch(r"%s\s*:?\s*(?:%s)[.!]?" % (head, yes), seg, re.I):
            found.add(("yes", None))
        elif re.fullmatch(r"%s\s*:?\s*(?:%s)[.!]?" % (head, no), seg, re.I):
            found.add(("no", None))
        else:
            m = re.fullmatch(r"%s\s*:\s*(.+)" % head, seg, re.I)
            if m and m.group(1).strip():
                found.add(("text", m.group(1).strip()))
    return next(iter(found)) if len(found) == 1 else None


def is_sole_phrase(text, phrases):
    """True when the whole message, lower-cased, with spaces collapsed and one trailing run of `.` or
    `!` dropped, is one of `phrases`. The batch rule with a phrase set of the caller's choosing."""
    norm = " ".join((text or "").lower().split())
    norm = re.sub(r"[.!]+$", "", norm).strip()
    return norm in phrases


def is_batch_phrase(text):
    """True when the whole message is one of the whole-batch phrases."""
    return is_sole_phrase(text, BATCH_PHRASES)


# ------------------------------------------------------------------ human_write
def _terminal(subject, environ, isatty, read):
    from alpaca.research import owner as research_owner
    marked = agent_markers(environ)
    if marked:
        raise OwnerWordRefusal(R_NOT_TERMINAL, "an agent session is set (%s); the owner answers "
                               "in a terminal of their own" % ", ".join(marked))
    try:
        research_owner.confirm_at_terminal(
            subject, prompt="Type %s to confirm: " % subject, environ={}, isatty=isatty, read=read)
    except research_owner.OwnerRefusal as exc:
        raise OwnerWordRefusal(R_NOT_TERMINAL, exc.detail)
    return {"path": "terminal", "transcript": None, "line": None,
            "timestamp": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
            "quote": subject}


def human_write(conn, session, by, *, subject, answer, quote, root=None, form="id", since=None,
                until=None, bound_session=None, others=(), extra_yes=(), min_words=1,
                strict_ids=False, phrases=None, accepts=None, environ=None, isatty=None, read=None, newest=False):
    """Prove the owner said `answer` about `subject`, or raise OwnerWordRefusal. Returns the
    `owner_word` row {path, transcript, line, timestamp, quote} (plus `answer` and `text`) the
    caller stores on its event. Nothing is written here.

    `by` must be a human identity. `form` is `id` (the answer forms of 1.2), `batch` (the whole
    batch phrase, `subject` only names the batch) or `quote` (a substring of one owner message
    with at least `min_words` words: owner orders) or `sole` (the batch window rule with the phrase
    set `phrases`, or the test `accepts(text)` when the caller passes one: the first owner message after `since` is
    exactly one of them, and `answer` is what that phrase means, "yes" or "no") or `whole` (like `sole`, but any owner message in the window may
    be the phrase, not only the first: the owner may say a sentence and type the phrase when asked).
    `since` and `until` bound the typing time;
    `bound_session` is the session a batch item id may be answered from (the one that asked);
    `others` are the other ids of the same message (batch items); `extra_yes` adds assent words
    (order close). `strict_ids` reads the id form with the strict segments of `_segments` (batch
    items). `newest` (session path, forms sole, whole and id) also needs the message that answers to be the
    newest owner message of the session, by place in the file and never by the clock: an owner message typed
    after it may take it back, so it must be read first (OWNER-WORD-NOT-NEWEST). In the whole form the newest
    message must itself be one of `phrases`. `environ`, `isatty` and `read` are for tests."""
    if str(by or "").strip() not in HUMAN_IDENTITIES:
        raise OwnerWordRefusal(R_NOT_HUMAN, "%r is not a human identity (%s)"
                               % (by, ", ".join(sorted(HUMAN_IDENTITIES))))
    root = root or paths.root()
    real = real_session(environ)
    if real is None:
        word = _terminal(subject, environ, isatty, read)
        word["answer"] = answer
        if answer not in ("yes", "no"):
            word["text"] = answer
        return word
    if session != real:
        raise OwnerWordRefusal(R_SESSION, "this call runs in session %s but names session %s; "
                               "owner words come from the session in the environment"
                               % (real, session))
    if bound_session is not None and bound_session != real:
        raise OwnerWordRefusal(R_UNBOUND, "%s is answered only from session %s, which asked; "
                               "this is session %s" % (subject, bound_session, real))
    needle = (quote or "").strip()
    if not needle:
        raise OwnerWordRefusal(R_NOT_FOUND, "no quote: pass the owner's message with --quote")
    if form == "quote":
        word = find_quote(conn, real, needle, root, since=since, until=until,
                          min_words=min_words, environ=environ)
        word["answer"] = answer
        return word
    path, messages = session_messages(conn, real, root, environ)
    window = [m for m in messages if _after(m, since, until)]
    if form == "sole":
        if not phrases and accepts is None:
            raise OwnerWordRefusal(R_UNBOUND, "the sole form needs a phrase set")
        if not window or window[0]["text"].strip() != needle:
            if not any(m["text"].strip() == needle for m in messages):
                raise OwnerWordRefusal(R_NOT_FOUND, "no owner message of session %s is that "
                                       "text" % real)
            raise OwnerWordRefusal(R_UNBOUND, "a lone word counts only in the first owner message "
                                   "after the ask")
        if not (accepts(needle) if accepts is not None else is_sole_phrase(needle, phrases)):
            raise OwnerWordRefusal(R_UNBOUND, "the first message after the ask is not one of: "
                                   + ", ".join(sorted(phrases or ())))
        m = window[0]
        if newest:
            _newest_or_refuse(m, messages)
        return {"path": "session", "transcript": path, "line": m["line"],
                "timestamp": m["timestamp"], "quote": needle, "answer": answer}
    if form == "whole":
        # one whole owner message, typed in the window, that is one of `phrases` (the owner may say a sentence first and
        # type the phrase when asked): the agent's quote must be that phrase and some owner message must be exactly it
        if not phrases:
            raise OwnerWordRefusal(R_UNBOUND, "the whole form needs a phrase set")
        if not is_sole_phrase(needle, phrases):
            raise OwnerWordRefusal(R_UNBOUND, "the quote is not one of: " + ", ".join(sorted(phrases)))
        hits = [m for m in window if is_sole_phrase(m["text"], phrases)]
        if not hits:
            raise OwnerWordRefusal(R_NOT_FOUND, "no owner message of session %s, typed after the first chat's last "
                                   "step, is one of: %s" % (real, ", ".join(sorted(phrases))))
        m = hits[0]
        if newest:
            m = hits[-1]
            _newest_or_refuse(m, messages)
        return {"path": "session", "transcript": path, "line": m["line"],
                "timestamp": m["timestamp"], "quote": needle, "answer": answer}
    if form == "batch":
        if not window or window[0]["text"].strip() != needle:
            if not any(m["text"].strip() == needle for m in messages):
                raise OwnerWordRefusal(R_NOT_FOUND, "no owner message of session %s is that "
                                       "text" % real)
            raise OwnerWordRefusal(R_UNBOUND, "the whole-batch phrase counts only in the first "
                                   "owner message after the ask")
        if not is_batch_phrase(needle):
            raise OwnerWordRefusal(R_UNBOUND, "the first message after the ask is not one of: "
                                   + ", ".join(sorted(BATCH_PHRASES)))
        m = window[0]
        return {"path": "session", "transcript": path, "line": m["line"],
                "timestamp": m["timestamp"], "quote": needle, "answer": "yes"}
    same = [m for m in messages if m["text"].strip() == needle]
    if not same:
        raise OwnerWordRefusal(R_NOT_FOUND, "no owner message of session %s is exactly that text"
                               % real)
    inside = [m for m in same if m in window]
    if not inside:
        raise OwnerWordRefusal(R_UNBOUND, "that message was not typed in the window after %s "
                               "existed" % subject)
    read_back = read_answer(needle, subject, others, extra_yes, strict=(strict_ids or False))
    want = "text" if answer not in ("yes", "no") else answer
    if read_back is None or read_back[0] != want:
        raise OwnerWordRefusal(R_UNBOUND, "the message does not answer %s with %s" % (subject, want))
    if want == "text" and answer != "text" and read_back[1] != str(answer).strip():
        raise OwnerWordRefusal(R_UNBOUND, "the message answers %s with a different text" % subject)
    m = inside[0]
    if newest:
        m = inside[-1]
        _newest_or_refuse(m, messages)
    word = {"path": "session", "transcript": path, "line": m["line"], "timestamp": m["timestamp"],
            "quote": needle, "answer": want}
    if want == "text":
        word["text"] = read_back[1]
    return word


def _newest_or_refuse(message, messages):
    """Refuse (OWNER-WORD-NOT-NEWEST) unless `message` is the last of `messages`, the owner messages of one read of
    the transcript in file order. The place in the file decides, never a timestamp: two lines may share one."""
    if not messages or messages[-1]["line"] != message["line"]:
        raise OwnerWordRefusal(R_NOT_NEWEST, "the owner wrote again after that message (transcript line %s); read "
                               "the newer message first" % (messages[-1]["line"] if messages else "none"))


# ------------------------------------------------------------------ asks
ASK_KIND = "owner-ask"


def record_ask(conn, session, subject, text, *, op=None, extra=None):
    """Append one owner-ask event: the owner is shown `text` about `subject`. Returns the event."""
    data = {"subject": subject, "text": text, "at": util.now_iso_precise()}
    if extra:
        data.update(extra)
    return db.append_event(conn, session=session, actor="alpaca", kind=ASK_KIND, op=op,
                           ref=subject, data=data)


def latest_ask(conn, subject):
    """The newest owner-ask event row for `subject`, or None."""
    return conn.execute("SELECT * FROM events WHERE kind=? AND ref=? ORDER BY id DESC LIMIT 1",
                        (ASK_KIND, subject)).fetchone()


def ask_floor(row):
    """The time after which an owner message answers the ask `row`. An event's `ts` has whole seconds; `at` (set by
    record_ask) has microseconds. A message typed in the same second but before the ask must not count as typed
    after it, so the floor is `at` when the ask has one (an ask recorded before `at` existed falls back to `ts`)."""
    try:
        at = json.loads(row["data"]).get("at")
    except (TypeError, ValueError, AttributeError):
        at = None
    return at if isinstance(at, str) and _ts(at) is not None else row["ts"]


def ask_ts(conn, subject):
    row = latest_ask(conn, subject)
    return ask_floor(row) if row else None


def writer(conn, session, by, quote, *, subject, answer="yes", root=None, since=None,
           environ=None, isatty=None, read=None):
    """(actor, owner_word) for a write that may name a human `by`. A human `by` goes through
    human_write; any other `by` is recorded as given; no `by` records the session actor."""
    if by and str(by).strip() in HUMAN_IDENTITIES:
        word = human_write(conn, session, by, subject=subject, answer=answer, quote=quote,
                           root=root, since=since, environ=environ, isatty=isatty, read=read)
        return str(by).strip(), word
    if by:
        return by, None
    return session_actor(session, environ), None


# ------------------------------------------------------------------ decisions
GRANDFATHER_KEY = "ownerword-grandfathered"
#: the decisions recorded before this landed whose pointer quotes the whole owner message of the
#: session that wrote them. The list is fixed here, not computed at run time, so a decision an
#: agent writes later with a quoting pointer cannot join it.
GRANDFATHER_CANDIDATES = ("dec-ab31c8b6fd4b",)


def _decision_event(conn, ref):
    row = conn.execute("SELECT * FROM events WHERE kind='decision' AND ref=? ORDER BY id DESC "
                       "LIMIT 1", (ref,)).fetchone()
    if row is None:
        return None, {}
    try:
        data = json.loads(row["data"])
    except (TypeError, ValueError):
        data = {}
    return row, data if isinstance(data, dict) else {}


def _pointer_quoted(pointer, wholes):
    """The owner message texts in `wholes` that `pointer` quotes whole, between double or single
    quote marks (the live pointer of dec-ab31c8b6fd4b uses single marks)."""
    text = pointer or ""
    return [w for w in wholes if w and ('"%s"' % w in text or "'%s'" % w in text)]


def _grandfather_ok(conn, ref, root, environ=None):
    row, data = _decision_event(conn, ref)
    if row is None:
        return False
    try:
        _path, messages = session_messages(conn, row["session"], root, environ)
    except OwnerWordRefusal:
        return False
    whole = {m["text"].strip() for m in messages}
    return bool(_pointer_quoted(data.get("pointer"), whole))


def ensure_grandfathered(conn, root=None, environ=None):
    """Write the grandfather list into `meta` once, when every candidate decision that exists
    verifies. Returns the list in force (empty when none is recorded yet)."""
    raw = db.meta_get(conn, GRANDFATHER_KEY)
    if raw is not None:
        try:
            return list(json.loads(raw))
        except (TypeError, ValueError):
            return []
    root = root or paths.root()
    present = [c for c in GRANDFATHER_CANDIDATES if _decision_event(conn, c)[0] is not None]
    if not present:
        return []
    passing = [c for c in present if _grandfather_ok(conn, c, root, environ)]
    if len(passing) != len(present):
        return passing
    db.meta_set(conn, GRANDFATHER_KEY, json.dumps(passing))
    return passing


def decision_words(conn, ref, root=None, environ=None):
    """The owner words behind decision `ref` (the quote of its verified `owner_word`, or the
    quoted owner message of a grandfathered pointer), or None when it is not owner-proved. Read,
    not written: a session-path owner_word is re-read from the transcript."""
    root = root or paths.root()
    row, data = _decision_event(conn, ref)
    if row is None:
        return None
    word = data.get("owner_word")
    if isinstance(word, dict) and word.get("path") == "terminal" and word.get("quote"):
        return str(word["quote"])
    if isinstance(word, dict) and word.get("path") == "session" and word.get("quote"):
        try:
            _path, messages = session_messages(conn, row["session"], root, environ)
        except OwnerWordRefusal:
            return None
        return str(word["quote"]) if any(m["text"].strip() == word["quote"] for m in messages) \
            else None
    raw = db.meta_get(conn, GRANDFATHER_KEY)
    try:
        listed = json.loads(raw) if raw else []
    except (TypeError, ValueError):
        listed = []
    if ref in listed:
        try:
            _path, messages = session_messages(conn, row["session"], root, environ)
        except OwnerWordRefusal:
            return None
        whole = {m["text"].strip() for m in messages}
        quoted = _pointer_quoted(data.get("pointer"), whole)
        if quoted:
            return quoted[0]
    return None


def decision_is_owner(conn, ref, root=None, environ=None):
    """True when decision `ref` is owner-proved: it carries a verified owner_word, or it is on the
    grandfather list. A decision written after landing is owner-proved only through `--quote`."""
    return decision_words(conn, ref, root, environ) is not None


# ------------------------------------------------------------------ CLI: alpaca owner
def _cmd_owner(args):
    from alpaca import cli, decisions
    if getattr(args, "owner_verb", None) != "ask":
        return cli.USAGE
    conn = db.connect(cli._root())
    sid = args.session or "cli"
    try:
        require_real_session(sid)
        subject = args.subject
        text = args.text or ""
        if subject == "decision":
            subject = decisions.preview_id(args.kind, args.context, args.option or [], args.choice,
                                           args.consequence, args.pointer or "")
            text = text or "Decision %s: %s" % (subject, args.choice)
        if not text.strip():
            raise ValueError("--text is required")
    except ValueError as exc:
        print("GATE alpaca-owner: FAIL (%s)" % exc)
        return cli.FAIL
    record_ask(conn, sid, subject, text)
    print(text)
    print("Reply with: %s yes | %s no | %s: <your answer>" % (subject, subject, subject))
    return cli.PASS


def _parser(sub):
    o = sub.add_parser("owner", help="owner words: ask the owner about a subject")
    ov = o.add_subparsers(dest="owner_verb")
    a = ov.add_parser("ask", help="record that the owner is asked about <subject> and print the ask")
    a.add_argument("subject", help="the record id the owner will answer, or `decision`")
    a.add_argument("--text", help="what the owner is shown")
    a.add_argument("--kind"); a.add_argument("--context"); a.add_argument("--option", action="append")
    a.add_argument("--choice"); a.add_argument("--consequence"); a.add_argument("--pointer")


def _register():
    from alpaca import cli
    cli.command("owner")(_cmd_owner)
    cli.register_parser("ownerword", _parser)


_register()
