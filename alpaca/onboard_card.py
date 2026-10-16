"""The setup card of the first chat (op-062 t-656; design/attach-mode/onboarding-design.md).

Pure: no I/O, no clock, no environment. Every function here takes the recorded look of the
repository, the recorded answers and a date, and returns text or data; the same inputs give the same
bytes, so a later session, a test and the owner's confirm all see one card. `alpaca/onboard.py`
records the inputs and the owner's yes; `alpaca/onboard_look.py` reads the repository.

The card is written for a person who has never seen Alpaca. It has ten numbered parts, each fact
points at a numbered source, every Alpaca word is explained once where it first appears, and every
line the agent wrote is marked as a guess.

Inputs
------
look     the dict `onboard_look.look` returns, or None when no repository was named (start empty).
answers  `{slot: entry}` for single slots and `{slot: [entry, ...]}` for repeatable ones, built from
         the `onboard-answer` events. An entry is {"value", "option", "source", "quote", "reason",
         "pointer", "name", "argv", "kind", "proves", "trial", "event"}; `source` is owner, agent,
         sensed or default.
"""
from __future__ import annotations

import hashlib
import math
import re
import textwrap

RENDERER_VERSION = 1
PLACEHOLDER_ID = "card-00000000"
WIDTH = 96
MAX_LINES = 110                    # was 100: the footprint and trace lines of op-062 t-691 fill the longest card (QTRM64)
TITLE_WIDTH = 90

#: the situations, in the order the card and the tests list them
SITUATIONS = ("start-empty", "take-over", "team", "audit", "migrate")
LABELS = {"start-empty": "start empty", "take-over": "take over", "team": "team repo",
          "audit": "audit only", "migrate": "migrate or rewrite"}

#: owner words that name a situation, whole words, case-insensitive, first rule that fires wins
WORD_MAP = (
    ("audit", ("audit", "review the code", "understand", "just read", "assess")),
    ("migrate", ("rewrite", "port", "migrate", "move to")),
    ("team", ("my team", "our team", "colleagues", "we review", "pull request review")),
    ("take-over", ("took over", "take over", "inherited", "mine now", "left the company", "handed over")),
)
START_EMPTY_WORDS = ("start empty", "new project", "no code yet")
RECENT_DAYS = 90
STILL_DAYS = 90

#: the words the card explains, once, at first use (design 5.1)
GLOSSARY = {
    "studio": "the folder you made with alpaca new",
    "gate": "a command that must pass before a change can leave the studio",
    "op": "one goal with its own tasks and reports",
    "task": "one small piece of work with its own branch",
    "level": "how much I may do before I ask you, from L1 to L6",
    "baseline": "the result of every check on your code as it is today",
    "study": "a first read of your code, written down as a map",
    "upstream": "the repo your code comes from",
    "base": "the branch your changes start from and are checked against",
}

#: a lone word that counts as the owner's yes / no to the card, as the first message after the ask
ASSENT_WORDS = ("yes", "ok", "okay", "accept", "approve", "approved", "agree", "fine", "go", "signed")
SOLE_YES = frozenset(ASSENT_WORDS) | frozenset({"yes please", "go ahead", "looks good", "sounds good", "do it"})
#: assent and filler phrases an owner may put around the one word yes (rehearsal round 1: "ok got it. yes" and "Okay, yes,
#: please set it up." cost an owner message each). The list is closed: after these are removed, the words left must be
#: exactly `yes`, so every other word ("but use L3", "no", "later") keeps the reply a change.
YES_FILLER = ("please set it up", "set it up", "thank you", "sounds good", "go ahead", "got it", "thanks", "okay", "sure",
              "ok")
_YES_PUNCT = re.compile(r"[.,!;:]+")


def reads_as_yes(text):
    """True when `text`, lower-cased, with `. , ! ; :` read as spaces and every phrase of YES_FILLER removed (whole
    words), leaves exactly the word yes. A question mark, a hyphen or any other word keeps it from being a yes."""
    t = " " + " ".join(_YES_PUNCT.sub(" ", (text or "").lower()).split()) + " "
    for phrase in YES_FILLER:
        t = re.sub(r"(?<=\s)%s(?=\s)" % re.escape(phrase), " ", t)
        t = re.sub(r"(?<=\s)%s(?=\s)" % re.escape(phrase), " ", t)       # adjacent phrases share a space
    return t.split() == ["yes"]


SOLE_NO = frozenset({"no", "reject", "decline", "no thanks", "not yet"})
#: a whole owner message that skips the first chat (the skill asks the owner to type `skip setup`). No one-word phrase
#: and no "no setup": an owner who passes over one question of a round writes those, and that must not end the chat
SKIP_PHRASES = frozenset({"skip setup", "skip the setup", "skip the first chat", "i will set it up myself"})

#: the record events a first chat may write before the owner confirms (design 7.1)
PRE_CONFIRM_KINDS = frozenset({"onboard-look", "onboard-round", "onboard-answer", "onboard-card",
                               "owner-ask", "onboard-declined"})

VENDOR_DIRS = ("Drivers", "vendor", "third_party", "third-party", "external", "extern", "node_modules")
SOURCES_FIXED = 6

LEVEL_TEXT = {
    1: "I only read and advise. I change nothing; you apply every change.",
    2: "I make one bounded change at a time and show it to you before the next.",
    3: "I run a whole task and stop at every fork and before anything that cannot be undone.",
    4: "I work alone inside one scope you set, check my own work, and stop at the edge of that scope.",
}

#: slots: what each one is. kind option = a printed question with lettered options (an option that needs the owner's
#: words, such as a pattern or a list, is answered with --option and --value, both from the owner's reply); text = free
#: words; agent = written by the agent, never asked. `rounds` lists the situations that ask it, in order.
SLOTS = {
    "opening": {"kind": "text"},
    "situation": {"kind": "option", "options": SITUATIONS},
    "goal": {"kind": "option", "options": ("a", "b", "c", "d")},
    "limits": {"kind": "option", "options": ("a", "b")},
    "others-commit": {"kind": "option", "options": ("a", "b", "c")},
    "merges": {"kind": "option", "options": ("a", "b")},
    "ci": {"kind": "option", "options": ("a", "b", "c")},
    "files-in-repo": {"kind": "option", "options": ("a", "b")},
    "base": {"kind": "text"},
    "branch-prefix": {"kind": "option", "options": ("a", "b")},
    "pr-says-agent": {"kind": "option", "options": ("a", "b")},
    "commit-identity": {"kind": "text"},
    "audit-reader": {"kind": "option", "options": ("a", "b", "c")},
    "migrate-target": {"kind": "option", "options": ("a", "b")},
    "keep-working": {"kind": "option", "options": ("a", "b")},
    "stack": {"kind": "option", "options": ("a", "b", "c", "d")},
    "preset": {"kind": "option", "options": ("a", "b")},
    "level-override": {"kind": "text"},
    "done-guess": {"kind": "agent"},
    "finding": {"kind": "agent", "repeatable": True, "max": 3},
    "unknown": {"kind": "agent", "repeatable": True, "max": 3},
    "gate.extra": {"kind": "agent", "repeatable": True},
    "gate.drop": {"kind": "agent", "repeatable": True},
}
REPEATABLE = frozenset(k for k, v in SLOTS.items() if v.get("repeatable"))

#: the slots each situation asks in round 1, in order (at most 4 are printed)
ROUND = {
    "start-empty": ("goal", "stack", "limits"),
    "take-over": ("others-commit", "files-in-repo", "goal", "limits"),
    "team": ("goal", "merges", "files-in-repo", "limits", "branch-prefix"),
    "audit": ("goal", "audit-reader", "limits"),
    "migrate": ("migrate-target", "goal", "keep-working", "limits"),
}
#: slots a move into the same existing repo must answer too, once `migrate-target` is `a`: that repo is attached and hands
#: its changes to it, so who else commits and whether Alpaca files may live in it matter. Round 1 is chosen from the opening
#: alone, before the target is known, so these are asked in the next round, as required slots. A move into a new repo
#: (target `b`) only reads the old repo and never asks them.
MIGRATE_SAME_REPO_ONLY = ("others-commit", "files-in-repo")
REQUIRED = {"start-empty": ("goal",), "take-over": ("goal",), "team": ("goal", "merges"),
            "audit": ("goal",), "migrate": ("goal", "migrate-target")}
#: situations that attach the repo and so ask where its checks run (`ci`); a move asks it once `migrate-target` is `a`
CI_SITUATIONS = ("take-over", "team", "audit")
MAX_QUESTIONS = 4
#: rounds that may ask anything: round 1 is the situation's list, round 2 what the answers still need. A round after
#: these asks only the required slots that are still missing, as options questions, up to MAX_REQUIRED_ROUNDS in all.
MAX_ROUNDS = 2
#: the hard stop: after this many rounds `onboard next` stops printing questions and says to ask in plain words
MAX_REQUIRED_ROUNDS = 4


def _opts(*pairs):
    return [{"key": k, "text": t} for k, t in pairs]


#: the last goal option of every situation: the goal is quoted in the owner's own words, so this is the recommended one
GOAL_OWN = "Something else, in my own words"


def _goal_options(situation):
    """(question text, the starter options before the last one) of the goal question for this situation."""
    if situation == "audit":
        return ("What do you want to find out about this code?",
                ("Find out how a part of it works", "Find out where it is weak or risky"))
    if situation == "start-empty":
        return ("What do you want to build, and for whom?",
                ("A tool or page only I will use", "An app or site for other people to use"))
    if situation == "migrate":
        return ("What do you want to end up with after the move?",
                ("The same behavior in the new code", "The same behavior, with a cleaner design"))
    if situation == "team":
        return ("What do you want from this project first?", ("Add a feature", "Fix a bug"))
    return ("What do you want from this project first?",
            ("Get it into a clean, working state first", "Add a new feature", "Fix a problem I already know about"))


def _tip_prefix(tips):
    """The branch prefix (`feature/`) that every side branch of the look shares, or None."""
    found = set()
    for tip in tips or []:
        m = re.match(r"[^/]+/([a-z][a-z0-9-]{2,30}/)[^/]+", str(tip.get("ref") or ""))     # origin/feature/x
        found.add(m.group(1) if m else None)
    return next(iter(found)) if len(found) == 1 and None not in found else None


def _slot_question(slot, look, situation, answers=None):
    """(question text, options, recommended key, why) of one slot for this situation and look. Every question has
    2 to 4 options and one recommended; an option that needs the owner's own words says so in its text."""
    hist = (look or {}).get("history") or {}
    tips = hist.get("tips") or []
    if slot == "goal":
        text, starters = _goal_options(situation)
        opts = _opts(*[(k, t) for k, t in zip("abcd", starters + (GOAL_OWN,))])
        return (text, opts, opts[-1]["key"],
                "every piece of work is checked against it; I quote you, so your own words are best and I never summarize you")
    if slot == "limits":
        opts = _opts(("a", "None"), ("b", "Yes, I will say them"))
        return ("Is there anything I must not touch, or must always do?", opts, "a",
                "I keep your limits word for word and check each change against them")
    if slot == "others-commit":
        base = hist.get("base") or "main"
        still = hist.get("days_still")
        opts = [{"key": "a", "text": "No, only me now"}]
        if tips:
            opts.append({"key": "b", "text": "Yes, on their own branches (%s, last %s)" % (tips[0].get("author", "?"),
                                                                                         tips[0].get("date", "?"))})
        else:
            opts.append({"key": "b", "text": "Yes, on their own branches"})
        opts.append({"key": "c", "text": "Yes, it is still a team repo with reviews"})
        recent = hist.get("recent_authors") or []
        rec = "a" if (isinstance(still, int) and still > STILL_DAYS and len(recent) <= 1) else "b"
        if rec == "a":
            opts[0]["note"] = "%s has not moved for %s days" % (base, still)
        else:
            opts[1]["note"] = "someone committed in the last %d days" % RECENT_DAYS
        return ("Does anyone else still push to this repo?", opts, rec,
                "it decides whether each change is checked against their newest work, and who reviews")
    if slot == "files-in-repo":
        opts = _opts(("a", "No: nothing of Alpaca in your repo, no files and no mention of it in commits or pull requests"),
                     ("b", "Yes, Alpaca files may live in the repo"))
        return ("May Alpaca files live in your repo?", opts, "a",
                "it decides the mode, and what your commits, branches and pull requests say about me")
    if slot == "merges":
        opts = _opts(("a", "My teammates review and merge on GitHub" if host_kind(look) == "github"
                      else "My teammates review and merge"), ("b", "I merge my own changes"))
        rec = "b" if situation == "take-over" and _option(answers, "others-commit") == "b" else "a"
        if rec == "b":
            opts[1]["note"] = "the others work on their own branches"
        return ("Who reviews and merges a change to this repo?", opts, rec,
                "it decides where my work stops: at your approval, or at theirs")
    if slot == "ci":
        opts = _opts(("a", "Only on my machine, there is no CI"),
                     ("b", "On a CI service (GitHub Actions, GitLab CI, Jenkins or another)"), ("c", "I do not know"))
        opts[0]["note"] = "the look found no CI file in the repo"
        return ("Where do this repo's checks run?", opts, "a",
                "it decides whether a green run here is enough, or a CI I cannot see must pass before a change is merged")
    if slot == "branch-prefix":
        found = _tip_prefix(tips)
        opts = _opts(("a", "Anything goes"),
                     ("b", "Yes, this pattern: %s" % (found + "<name>" if found else "one I will say")))
        rec = "b" if found else "a"
        if found:
            opts[1]["note"] = "the branches in your repo use it"
        return ("What do work branches have to look like?", opts, rec,
                "I name every branch that way so your team's rules accept it")
    if slot == "audit-reader":
        opts = _opts(("a", "Only me"), ("b", "My team"), ("c", "Someone outside the team"))
        return ("Who will read the result?", opts, "a", "it decides how much background I write")
    if slot == "migrate-target":
        opts = _opts(("a", "The same repo: new code lands next to the old"), ("b", "A new repo"))
        return ("Does the new code live in this repo, or in a new one?", opts, "a",
                "a new repo means I keep this one read-only and start a fresh folder")
    if slot == "keep-working":
        opts = _opts(("a", "Nothing in particular"), ("b", "Yes, I will list them"))
        return ("What must keep working while you move?", opts, "b",
                "I write it down as a list of things no change may break")
    if slot == "stack":
        opts = _opts(("a", "No preference, you choose"), ("b", "Python"), ("c", "JavaScript or a web page"),
                     ("d", "Something else, I will say"))
        return ("Do you already know what to build it with?", opts, "a",
                "it decides the first check I add, and a wrong guess costs you a rewrite")
    return (slot, [], None, "")


def _slot_open(slot, look, situation):
    """True when `slot` can be asked for this look: the look may already answer it."""
    around = (look or {}).get("around") or {}
    hist = (look or {}).get("history") or {}
    if slot == "others-commit":
        return bool(hist.get("recent_authors"))
    if slot == "branch-prefix":
        return not around.get("contributing_branch_prefix")
    if slot == "files-in-repo":
        return not around.get("alpaca_files")          # a repo that already holds Alpaca files has answered
    if slot == "ci":
        return not around.get("ci")                    # CI files in the repo have answered: the card states them
    return True


# --------------------------------------------------------------------------------- answers helpers

def _single(answers, slot):
    entry = (answers or {}).get(slot)
    if isinstance(entry, list):
        entry = entry[-1] if entry else None
    return entry


def _list(answers, slot):
    entry = (answers or {}).get(slot)
    if entry is None:
        return []
    return entry if isinstance(entry, list) else [entry]


def _value(answers, slot):
    entry = _single(answers, slot)
    if not entry:
        return None
    return entry.get("value") if entry.get("value") not in (None, "") else None


def _option(answers, slot):
    entry = _single(answers, slot)
    return (entry or {}).get("option")


def _owner_said(answers):
    """The owner-quoted texts so far: the opening and every answer that carries an owner quote."""
    said = []
    for slot, entry in (answers or {}).items():
        for e in (entry if isinstance(entry, list) else [entry]):
            if e and e.get("source") == "owner":
                text = e.get("quote") or e.get("value")
                if text and text not in said:
                    said.append(text)
    opening = _single(answers, "opening")
    if opening and opening.get("value"):
        said = [opening["value"]] + [s for s in said if s != opening["value"]]
    return said


# ------------------------------------------------------------------------------------ owner text

#: marks around every span of the card that holds the owner's own words (the goal, the limits, a quote drawn from the
#: opening or from an answer). They live only in the marked form of the card (`render_marked`); the card the owner reads
#: never holds one. Every check of `lint` runs on the card with all marked spans removed, so a refusal can only name
#: text the agent wrote.
ON, OFF = "\x02", "\x03"
_REGION = re.compile("%s[^%s]*%s" % (ON, OFF, OFF))


def strip_marks(text):
    """The text without the owner marks."""
    return text.replace(ON, "").replace(OFF, "")


def _own(text):
    """`text` as one owner span: white space collapsed, wrapped in the marks."""
    t = " ".join(strip_marks(str(text)).split())
    return ON + t + OFF if t else ""


def _quote(text, mark=False):
    """The owner's words in quotation marks, white space collapsed. When the words already begin or end with a
    quotation mark they are never wrapped in a second one: words that are one quotation stand as they are, other words
    that touch a double quote are wrapped in single quotes, and words that touch both stand bare."""
    t = " ".join(strip_marks(str(text)).split())
    body = _own(t) if mark else t
    if len(t) >= 2 and t[0] == '"' and t[-1] == '"' and t.count('"') == 2:
        return body
    for d in ('"', "'"):
        if t[:1] != d and t[-1:] != d:
            return d + body + d
    return body


def _owner_reason(text, owner):
    """`text` (a reason line ending in the quoted owner words `owner`) with those words marked."""
    t = " ".join(str(owner).split())
    at = text.rfind(t) if t else -1
    return text if at < 0 else text[:at] + _own(t) + text[at + len(t):]


# -------------------------------------------------------------------------------------- the guess

def _find_word(text, phrase):
    m = re.search(r"(?<![\w])%s(?![\w])" % re.escape(phrase), text, re.I)
    return m


#: a quoted clause longer than this is not shown whole (it is never cut: the matched words stand in)
CLAUSE_MAX = 140
#: words a quote must not end on when it was ended by a bracket or a link ("my old team at <link>")
_DANGLING = frozenset(("at", "in", "on", "from", "of", "to", "is", "are", "was", "it", "see", "here", "via", "the", "a",
                       "an", "with", "and", "or", "but", "by", "for", "that", "which", "where", "as"))
_LINK = re.compile(r"\S*/\S*")


def _clause(text, start, end):
    """The clause of owner `text` around [start, end): it runs from the nearest . , ; ! ? line end bracket or
    link before the matched words to the nearest after them, so it never holds half a bracket or half a link,
    and it is the owner's own words (nothing inside it is dropped). It is not cut: a clause longer than
    CLAUSE_MAX characters is replaced by the matched words alone. The reason line quotes it."""
    inside = [m.span() for m in _LINK.finditer(text)]

    def free(i):                                       # a boundary character that is not part of a link
        return not any(a <= i < b for a, b in inside)

    stops = ".,;!?\n()[]{}"
    left = max([i for i in range(0, start) if text[i] in stops and free(i)] + [b - 1 for a, b in inside if b <= start] + [-1])
    right = min([i for i in range(end, len(text)) if text[i] in stops and free(i)] + [a for a, b in inside if a >= end]
                + [len(text)])
    clause = " ".join(text[left + 1:right].split())
    matched = " ".join(text[start:end].split())
    if right < len(text) and text[right] not in ".!?\n":
        # ended by a comma, bracket or link, not by the end of a sentence: do not end on a word that needs the next one
        words = clause.split()
        keep = len(matched.split())
        while len(words) > keep and words[-1].lower() in _DANGLING:
            words.pop()
        clause = " ".join(words)
    if len(clause) > CLAUSE_MAX or matched not in clause:
        clause = matched
    return clause


def situation_guess(look, said):
    """The situation the first chat starts from: {"situation", "rule", "reasons": [{"text", "source"}]}.
    Rules in order, the first that fires wins (design 6.1). `said` is the list of owner-quoted texts.
    Pure: the reasons name the owner's words or a numbered source of the card (4 = branch history)."""
    said = list(said or [])
    joined = "\n".join(said)
    if look is None:
        return {"situation": "start-empty", "rule": 1, "reasons": [{"text": "no repository was named", "source": None}]}
    for phrase in START_EMPTY_WORDS:
        if _find_word(joined, phrase):
            return {"situation": "start-empty", "rule": 1,
                    "reasons": [{"text": 'you said "%s"' % phrase, "source": None}]}
    hist = look.get("history") or {}
    for situation, phrases in WORD_MAP:
        for text in said:
            for phrase in phrases:
                m = _find_word(text, phrase)
                if m:
                    clause = _clause(text, m.start(), m.end())
                    reasons = [{"text": "you said %s" % _quote(clause), "source": None, "owner": clause}]
                    if situation == "take-over" and _still(hist):
                        reasons.append({"text": "%s has not changed for %d days" % (hist.get("base") or "main",
                                                                                      hist["days_still"]), "source": 4})
                    return {"situation": situation, "rule": 2, "reasons": reasons}
    recent = hist.get("recent_authors") or []
    around = look.get("around") or {}
    if len(recent) >= 2 or (around.get("ci") and around.get("codeowners")):
        why = ("%d people committed in the last %d days" % (len(recent), RECENT_DAYS) if len(recent) >= 2
               else "it has CI files and a CODEOWNERS file")
        return {"situation": "team", "rule": 3, "reasons": [{"text": why, "source": 4 if len(recent) >= 2 else 6}]}
    if _still(hist) and len(recent) <= 1:
        return {"situation": "take-over", "rule": 3,
                "reasons": [{"text": "%s has not changed for %d days and at most one person committed in the last %d days"
                             % (hist.get("base") or "main", hist["days_still"], RECENT_DAYS), "source": 4}]}
    return {"situation": "team", "rule": 3, "reasons": [{"text": "other people may be working in it, so I take the safer reading",
                                                         "source": 4}]}


def _still(hist):
    days = hist.get("days_still")
    return isinstance(days, int) and not isinstance(days, bool) and days > STILL_DAYS


def situation_of(look, answers):
    """(situation, guess): the owner's override wins, then the guess from owner words and facts."""
    guess = situation_guess(look, _owner_said(answers))
    over = _option(answers, "situation") or _value(answers, "situation")
    entry = _single(answers, "situation")
    if over in SITUATIONS and entry and entry.get("source") in ("owner", "agent"):
        if entry.get("source") == "owner":
            said = " ".join(str(entry.get("quote") or entry.get("value") or over).split())
            reason = {"text": "you told me: %s" % _quote(said), "source": None, "owner": said}
        else:
            reason = {"text": entry.get("reason") or "my reading of your reply", "source": None}
        return over, {"situation": over, "rule": 0, "reasons": [reason]}
    return guess["situation"], guess


# ------------------------------------------------------------------------------------------ rounds

def next_round(look, answers, later=None):
    """The questions still open for this look and these answers, in table order, at most 4:
    [{"num", "slot", "text", "options", "recommended", "why"}]. A slot the owner answered, a slot the
    look answered (a repo that already holds Alpaca files has answered `files-in-repo`) are skipped. `later` says the
    round is not round 1 (`next_verb` knows it from the rounds it recorded); when it is None, a round is later once the
    owner has answered anything beyond the opening. Pure."""
    situation, _ = situation_of(look, answers)
    out = []
    slots = list(ROUND[situation])
    required = required_slots(look, situation, answers)
    if later is None:
        later = any(k != "opening" for k in answers or {})
    if later:
        # a later round: any required slot still missing comes before every optional one, so the cut at MAX_QUESTIONS
        # (and the filter `next_verb` applies afterwards) can never push a required slot out
        extra = [s for s in required if s not in slots]
        at = slots.index("keep-working") if "keep-working" in slots else len(slots)
        slots[at:at] = extra
        slots = [s for s in slots if s in required] + [s for s in slots if s not in required]
    # round 1 is the situation's own list; a required slot outside it waits for round 2
    for slot in slots:
        if _answered(answers, slot, look, situation) or not _slot_open(slot, look, situation):
            continue
        text, options, rec, why = _slot_question(slot, look, situation, answers)
        for o in options:
            o["recommended"] = (o["key"] == rec)
        out.append({"num": len(out) + 1, "slot": slot, "text": text, "options": options,
                    "recommended": rec, "why": why})
        if len(out) == MAX_QUESTIONS:
            break
    return out


def plan_next(look, answers, rounds_done):
    """What `onboard next` does after `rounds_done` rounds were printed: ("ready", []), ("ask", questions) or
    ("stop", missing). A required slot still missing is asked again as an options question in every round up to
    MAX_REQUIRED_ROUNDS, and the questions are numbered from 1; every round after round 1 carries only what is still
    required (MAX_ROUNDS is not read here). After the hard stop the owner is asked in
    plain words. Pure: the one decision `next_verb` and the tests share."""
    missing = missing_required(look, answers)
    if not missing:
        return "ready", []
    if rounds_done >= MAX_REQUIRED_ROUNDS:
        return "stop", missing
    questions = [q for q in next_round(look, answers, later=True) if q["slot"] in missing]
    if not questions:
        return "stop", missing
    for i, q in enumerate(questions, 1):
        q["num"] = i
    return "ask", questions


def format_round(questions, n):
    """The printed round: the text the verbs print and the first reply to the owner repeats."""
    if not questions:
        return []
    lines = ["round %d:" % n]
    for q in questions:
        lines.append(" %d. [%s] %s" % (q["num"], q["slot"], q["text"]))
        for o in q["options"]:
            note = o.get("note")
            tag = ""
            if o.get("recommended"):
                tag = " (recommended%s)" % (": " + note if note else "")
            elif note:
                tag = " (%s)" % note
            lines.append("    %s) %s%s" % (o["key"], o["text"], tag))
        lines.append("    why: %s" % q["why"])
    return lines


def required_slots(look, situation, answers):
    """The slots `situation` must have answered before the card: the fixed ones; for a move into the same repo
    (`migrate-target` `a`) the two footprint slots the look can still open; for a take-over whose answer says others
    push (`others-commit` `b` or `c`) who merges; and, for every situation that attaches the repo, where its checks run
    (`ci`). A slot the look cannot open (no recent authors, a repo that already holds Alpaca files, CI files in the
    repo) is not required, so it never blocks `ready`. The one reader of what is required."""
    out = list(REQUIRED[situation])
    if situation == "migrate" and _option(answers, "migrate-target") == "a":
        out += [s for s in MIGRATE_SAME_REPO_ONLY if _slot_open(s, look, situation)]
    if situation == "take-over" and _option(answers, "others-commit") in ("b", "c"):
        out.append("merges")
    if asks_ci(situation, answers) and _slot_open("ci", look, situation):
        out.append("ci")
    return out


def asks_ci(situation, answers):
    """True when `situation` hands changes to the repo, so where its checks run matters: a take-over, a team repo, an
    audit, and a move into the same repo (`migrate-target` `a`)."""
    return situation in CI_SITUATIONS or (situation == "migrate" and _option(answers, "migrate-target") == "a")


def goal_text(answers, look, situation):
    """(text, from_option): the goal. The owner's words when they gave any; else the text of the starter option they
    picked (printed in the question, so it is theirs by choice, never their words); else empty. The last option, in
    their own words, gives no text of its own."""
    entry = _single(answers, "goal")
    words = _value(answers, "goal")
    if words:
        return words, False
    key = (entry or {}).get("option")
    if key:
        opts = _slot_question("goal", look, situation)[1]
        text = next((o["text"] for o in opts[:-1] if o["key"] == key), "")
        if text:
            return text, True
    return "", False


def _answered(answers, slot, look=None, situation=None):
    """True when the slot has an answer the card can use. The goal needs a text: a pick of the last option with no words
    is not an answer yet."""
    if not _single(answers, slot):
        return False
    if slot == "goal":
        return bool(goal_text(answers, look, situation or situation_of(look, answers)[0])[0])
    return True


def missing_required(look, answers):
    situation, _ = situation_of(look, answers)
    return [s for s in required_slots(look, situation, answers) if not _answered(answers, s, look, situation)]


def option_token_present(reply, num, key, option_text=""):
    """True when the owner's reply picks option `key` of question `num`: the token `<n><key>` (`1a`,
    `1) a`, `1.a`), or the option's own words."""
    if re.search(r"(?<![\w])%d\s*[).:-]?\s*%s(?![\w])" % (int(num), re.escape(str(key))), reply or "", re.I):
        return True
    text = (option_text or "").strip().rstrip(".")
    return bool(text) and len(text) >= 4 and text.lower() in (reply or "").lower()


# -------------------------------------------------------------------------------------- the level

def level_for(situation, gates, answers):
    """(level, why): the level the card proposes (design 6.4). Pure."""
    kinds = {g.get("kind") for g in gates or []}
    has_check = bool(kinds & {"build", "test"})
    others = _option(answers, "others-commit")
    if situation == "start-empty":
        rule = (3, "nothing exists to break, and no change leaves the studio")
    elif situation == "take-over" and has_check and others != "c":
        rule = (3, "a passing build or test gate catches breakage, and you merge every change")
    elif situation == "take-over" and others == "c":
        rule = (2, "others share this repo")
    elif situation in ("team", "migrate") or situation == "audit":
        rule = (2, "others share this repo" if situation == "team" else
                ("you only want a read of the code" if situation == "audit" else
                 "a move is hard to undo, so I check in often"))
    else:
        rule = (2, "no gate here builds or tests the code")
    n, why = rule
    if situation == "take-over" and not has_check and others != "c":
        n, why = 2, "no gate here builds or tests the code"
    override = _single(answers, "level-override")
    ask = (override.get("value") or "").strip().upper() if override else ""
    if re.fullmatch(r"L[1-6]", ask):
        want = int(ask[1])
        if want <= 4:
            return "L%d" % want, "you asked for it"
    return "L%d" % n, why


def level_over_cap(answers):
    override = _single(answers, "level-override")
    ask = (override.get("value") or "").strip().upper() if override else ""
    return bool(re.fullmatch(r"L[56]", ask))


# ----------------------------------------------------------------------------------- estimates

def own_files(look):
    tracked = (look or {}).get("tracked") or {}
    own = tracked.get("own")
    return int(own) if isinstance(own, int) else 0


def estimates(look, gates, situation):
    """Fixed bands (design 5.3): {"study": (lo, hi) or text, "baseline": (lo, hi), "questions": minutes, "read": minutes}."""
    own = own_files(look)
    if own <= 100:
        study = "10 to 20 minutes"
    elif own <= 1000:
        study = "20 to 45 minutes"
    elif own <= 5000:
        study = "45 to 90 minutes"
    else:
        study = "90 minutes or more, in parts"
    seconds = 0.0
    for g in gates or []:
        trial = g.get("trial")
        if trial and isinstance(trial.get("seconds"), (int, float)):
            seconds += float(trial["seconds"])
        else:
            seconds += min(60.0, float(g.get("timeout") or 60))
    minutes = max(1, int(math.ceil(seconds / 60.0)))
    questions = 15 if situation in ("audit", "migrate") else 10
    return {"study": study, "baseline": "%d to %d minutes" % (minutes, minutes + 1), "questions": questions}


# -------------------------------------------------------------------------------------- settings

def _gates(look, answers):
    """The gates the card proposes: the sensed ones, minus `gate.drop`, plus `gate.extra`."""
    drop = {(e.get("name") or e.get("value")) for e in _list(answers, "gate.drop")}
    gates = [dict(g) for g in ((look or {}).get("gates") or []) if g.get("name") not in drop]
    for e in _list(answers, "gate.extra"):
        if not e.get("name") or not e.get("argv"):
            continue
        gates = [g for g in gates if g.get("name") != e["name"]]
        gates.append({"name": e["name"], "command": list(e["argv"]), "timeout": int(e.get("timeout") or 600),
                      "kind": e.get("kind") or "parse", "evidence": e.get("pointer") or "",
                      "classify": e.get("classify") or "parse", "trial": e.get("trial"),
                      "proves": e.get("proves") or "", "suggested": True, "reason": e.get("reason") or ""})
    return gates


_URL = re.compile(r"^(?:[a-z][a-z0-9+.-]*://|[\w.-]+@[\w.-]+:)", re.I)


def host_kind(look):
    """Where the repo lives, as the card words it: "github" (the look found a github.com address), "git-host" (any other
    address a push could go to: https, ssh or git) or "folder" (a path on this machine with no address)."""
    look = look or {}
    if look.get("repository"):
        return "github"
    where = str(look.get("upstream") or look.get("source") or "")
    return "git-host" if _URL.match(where) and not where.lower().startswith("file:") else "folder"


_IDENTITY_TEXT = re.compile(r"\s*(?P<name>[^<>@\\\x00-\x1f\x7f][^<>\\\x00-\x1f\x7f]{0,98}?)\s*<(?P<email>[^<>\s]+@[^<>\s]+)>\s*")


def commit_identity(text):
    """(name, email) from an owner's `Name <email>` (the commit-identity answer), else None. The shape the attach
    config accepts is checked here, so a card never records an identity the attach block would refuse."""
    m = _IDENTITY_TEXT.fullmatch(text or "")
    if not m:
        return None
    from alpaca.attach import config
    name, email = m.group("name").strip(), m.group("email")
    block = {"commit_name": name, "commit_email": email}
    return (name, email) if not config._identity_errors(block) else None


#: what the card proposes when the owner wants no trace of Alpaca in the repo (op-062 t-691): a neutral commit identity
#: the owner can change, the pull request footer off and a neutral branch prefix. The defaults are what attach writes
#: when nothing is said (`alpaca.attach.DEFAULT_IDENTITY`, footer on, prefix alpaca/).
NEUTRAL_IDENTITY = ("Maintenance bot", "maintenance@localhost")
NEUTRAL_PREFIX = "fix/"
DEFAULT_PREFIX = "alpaca/"
_NO_PREFIX = ("none", "no", "n/a", "anything")


def footprint(look, answers):
    """May Alpaca files live in the repo? {"value": "none" | "files" | "unasked", "source", "explicit", "found"}. The
    owner's answer wins; a repo that already holds Alpaca files has answered "files"; with neither the card assumes
    "none" and says so (`unasked`, never explicit). Pure."""
    entry = _single(answers, "files-in-repo")
    option = (entry or {}).get("option")
    if entry and option in ("a", "b"):
        source = entry.get("source") or "agent"
        return {"value": "none" if option == "a" else "files", "source": source, "explicit": source != "default",
                "found": []}
    found = list((((look or {}).get("around") or {}).get("alpaca_files")) or [])[:3]
    if found:
        return {"value": "files", "source": "sensed", "explicit": True, "found": found}
    return {"value": "unasked", "source": "default", "explicit": False, "found": []}


def others_push(situation, answers):
    """True when the card has reason to think someone else pushes to the repo: a team repo, or an answer that says so."""
    return situation == "team" or _option(answers, "others-commit") in ("b", "c")


def _chosen(entry):
    """How the card says an answer was given: the owner's own, or the agent's reading of a reply."""
    return "you chose it" if (entry or {}).get("source") == "owner" else "you chose it (my reading of your reply)"


def trace(look, answers, fp):
    """The three things a repo shows of Alpaca, with the value and the reason of each (op-062 t-691):
    {"identity": (name, email), "identity_why", "footer": bool, "footer_why", "prefix", "prefix_why", "free": bool}.
    An answer of the owner always wins. With none, a repo the owner wants kept free of any trace (`free`) gets the
    neutral set, and every other repo gets the defaults. The card shows all three, and apply writes what it shows. Pure."""
    from alpaca.attach import DEFAULT_IDENTITY
    free = fp["value"] == "none" and fp["explicit"]
    ident = _single(answers, "commit-identity")
    chosen = commit_identity(_value(answers, "commit-identity"))
    if chosen:
        identity, identity_why = chosen, _chosen(ident)
    elif free:
        identity, identity_why = NEUTRAL_IDENTITY, "proposed"
    else:
        identity, identity_why = tuple(DEFAULT_IDENTITY), "my default; it names Alpaca"
    foot = _single(answers, "pr-says-agent")
    if (foot or {}).get("option") in ("a", "b"):
        footer, footer_why = foot["option"] == "a", _chosen(foot)
    elif free:
        footer, footer_why = False, "proposed"
    else:
        footer, footer_why = True, "my default"
    prefix = ((look or {}).get("around") or {}).get("contributing_branch_prefix")
    prefix_why = "your repo's own rule" if prefix else None
    said = _value(answers, "branch-prefix")
    if not said and _option(answers, "branch-prefix") == "b":         # "1b" alone: the pattern the look found, as offered
        said = _tip_prefix((((look or {}).get("history") or {}).get("tips")) or [])
    if said and said.strip().lower() not in _NO_PREFIX:
        m = re.search(r"([a-z][a-z0-9-]{2,30}/)", said.strip().lower())
        if m:
            prefix, prefix_why = m.group(1), _chosen(_single(answers, "branch-prefix"))
    if not prefix:
        prefix, prefix_why = (NEUTRAL_PREFIX, "proposed") if free else (
            DEFAULT_PREFIX, "my default; it names Alpaca")
    whys = (identity_why, footer_why, prefix_why)
    return {"identity": tuple(identity), "identity_why": identity_why, "footer": footer, "footer_why": footer_why,
            "prefix": prefix, "prefix_why": prefix_why, "free": free, "proposed": any(w == "proposed" for w in whys)}


def limits_text(answers):
    """The limits the owner gave: their words, or `none` when they chose the option that says there are none."""
    words = _value(answers, "limits")
    if words:
        return words
    return "none" if _option(answers, "limits") == "a" else None


def settings(look, answers, *, people=None, name=None):
    """Everything the card says it will write, as data (the card text and `onboard apply` both read
    this, so they cannot disagree). Pure."""
    situation, guess = situation_of(look, answers)
    gates = _gates(look, answers)
    target = _option(answers, "migrate-target")
    # An existing repo is attached. The in-place mode (Alpaca files inside the repo) is not built in this version, so
    # no answer, the footprint answer included, gives a repo a native studio (op-062 t-691).
    mode = "native"
    if situation in ("take-over", "team", "audit") or (situation == "migrate" and target != "b"):
        mode = "attach" if look is not None else "native"
    level, why = level_for(situation, gates, answers)
    fp = footprint(look, answers)
    tr = trace(look, answers, fp)
    prefix = tr["prefix"] if mode == "attach" else None
    from alpaca.attach import DEFAULT_IDENTITY
    default_identity = tuple(DEFAULT_IDENTITY)
    identity = tr["identity"] if mode == "attach" and tr["identity"] != default_identity else (None, None)
    preset_opt = _option(answers, "preset")
    goal, goal_from_option = goal_text(answers, look, situation)
    first = re.split(r"(?<=[.!?])\s", " ".join(goal.split()), maxsplit=1)[0] if goal else ""
    return {
        "situation": situation, "guess": guess, "mode": mode, "level": level, "level_why": why,
        "preset": preset_opt != "b", "gates": gates, "branch_prefix": prefix,
        "base": (look or {}).get("history", {}).get("base") or ((look or {}).get("head") or {}).get("branch"),
        "source": (look or {}).get("upstream") or (look or {}).get("source"),
        "repository": (look or {}).get("repository"), "host_kind": host_kind(look),
        "look_sha": ((look or {}).get("head") or {}).get("sha"),
        "goal": goal, "goal_from_option": goal_from_option, "what": first[:120], "limits": limits_text(answers),
        "name": name, "people": people or [],
        "pr_says_agent": tr["footer"] if mode == "attach" else _option(answers, "pr-says-agent") != "b",
        "commit_name": identity[0], "commit_email": identity[1],
        "over_cap": level_over_cap(answers),
        "footprint": fp, "others_push": others_push(situation, answers), "trace": tr,
    }


# --------------------------------------------------------------------------------------- the card

class _Cite:
    """Numbered sources of one card: the fixed 1 to 6 first, then findings, trials and host facts."""

    def __init__(self, look):
        self.entries = []
        fixed = (look or {}).get("sources") or []
        for i in range(SOURCES_FIXED):
            text = fixed[i]["text"] if i < len(fixed) else "none found"
            self.entries.append(text)
        self.extra = {}

    def add(self, key, text):
        if key not in self.extra:
            self.entries.append(text)
            self.extra[key] = len(self.entries)
        return self.extra[key]


def _count(n, word):
    return "%d %s%s" % (n, word, "" if n == 1 else "s")


def _nums(*ns):
    return " " + " ".join("[%d]" % n for n in ns if n) if any(ns) else ""


def _build_names(files):
    names = {"CMakeLists.txt": "CMake", "Makefile": "Make", "package.json": "npm", "pyproject.toml": "Python packaging",
             "setup.py": "Python packaging", "Cargo.toml": "Cargo", "go.mod": "Go modules", "pom.xml": "Maven",
             "build.gradle": "Gradle", "meson.build": "Meson", "CMakePresets.json": "CMake"}
    out = []
    top = [f for f in files if "/" not in f] or list(files)           # a Makefile deep in a folder is not how it is built
    for f in top:
        label = names.get(f.rsplit("/", 1)[-1])
        if label and label not in out:
            out.append(label)
    return out


def _trial_text(gate):
    argv = gate.get("command") or []
    if gate.get("name") == "py-syntax":
        return "python3 parse (ast) of every tracked *.py file"
    short = " ".join(str(a) for a in argv[:5])
    return short + (" ..." if len(argv) > 5 else "")


def _register(cite, look, answers, st):
    """Number the sources after the fixed 1 to 6. The order is the order in which things become known, so a number the
    agent quotes in its first reply never changes when the agent records more: the host facts the look found, then the
    look's own gates (a trial, or the file a gate was read from) in gate order, then the findings in answer order, then
    the trials of the checks the agent suggested."""
    host = (look or {}).get("host") or {}
    for tool in host.get("wanted") or []:
        if (host.get("tools") or {}).get(tool) is None:
            cite.add(("host", tool), "host: which %s (not found)" % tool)
    if host.get("git_user"):
        cite.add(("host", "git_user"), "host: git config user.name")     # numbered even when the card does not cite it
    builds = (look or {}).get("build_files") or []
    ordered = [g for g in st["gates"] if not g.get("suggested")] + [g for g in st["gates"] if g.get("suggested")]
    for phase in ("look", "findings", "extra"):
        if phase == "findings":
            for e in _list(answers, "finding")[:3]:
                cite.add(("finding", e.get("event"), e.get("value")), e.get("pointer") or "")
            continue
        for g in ordered:
            if bool(g.get("suggested")) != (phase == "extra"):
                continue
            trial = g.get("trial")
            if trial:
                cite.add(("trial", g["name"]), "look trial: %s, exit %s, %.2f s"
                         % (_trial_text(g), trial.get("exit"), float(trial.get("seconds") or 0)))
            else:
                file = (g.get("evidence") or "").replace("repo:", "", 1)
                if file not in builds:
                    cite.add(("evid", file), "repo:%s" % file if file else "the build files")


def _found_lines(look, answers, cite):
    """Part 2: what the look found, as (text, [source numbers]) pairs, at most 8."""
    if look is None:
        return []
    out = []
    tracked = look.get("tracked") or {}
    builds = look.get("build_files") or []
    langs = tracked.get("languages") or []
    parts = []
    if langs:
        parts.append("Written in " + " and ".join("%s (%s)" % (l["name"], _count(l["files"], "file")) for l in langs[:2]))
    names = _build_names(builds)
    if names:
        parts.append("built with " + " and ".join(names))
    elif builds or langs:
        parts.append("no build file found")
    if parts:
        line = "; ".join(parts)
        out.append((line[0].upper() + line[1:], [1, 2]))
    n_files = tracked.get("files")
    if isinstance(n_files, int):
        vendor = [v for v in tracked.get("vendor") or [] if v.get("files")]
        if vendor:
            line = "%s; %d are vendor code (libraries copied in) in %s, so about %d are your own" % (
                _count(n_files, "file"), sum(x["files"] for x in vendor), ", ".join(x["dir"] + "/" for x in vendor),
                tracked.get("own", n_files))
        else:
            line = "%s, all yours (no vendor folder found)" % _count(n_files, "file")
        out.append((line, [3]))
    hist = look.get("history") or {}
    if hist.get("commits"):
        people = len(hist.get("authors") or [])
        line = "%s on %s by %d %s, %s to %s" % (
            _count(hist["commits"], "commit"), hist.get("base") or "main", people, "person" if people == 1 else "people",
            hist.get("first"), hist.get("last"))
        if _still(hist):
            line += "; %s is unchanged for %d days" % (hist.get("base") or "main", hist["days_still"])
        out.append((line, [4]))
    tips = hist.get("tips") or []
    if tips and tips[0].get("date") and hist.get("last") and tips[0]["date"] > hist["last"]:
        out.append(("Newer work exists only on side branches, by %s, last on %s" % (tips[0]["author"], tips[0]["date"]), [5]))
    around = look.get("around") or {}
    found, missing = [], []
    for label, key in (("CI files", "ci"), ("git hooks", "hooks"), ("tests", "tests"),
                       ("CLAUDE.md or AGENTS.md", "agent_files"), ("CODEOWNERS", "codeowners"),
                       ("CONTRIBUTING.md", "contributing")):
        (found if around.get(key) else missing).append(label)
    if found and missing:
        out.append(("Found: %s. Not found: %s" % (", ".join(found), ", ".join(missing)), [6]))
    elif found:
        out.append(("Found: %s" % ", ".join(found), [6]))
    else:
        out.append(("None of these exist: %s" % ", ".join(missing), [6]))
    host = look.get("host") or {}
    for tool in host.get("wanted") or []:
        if (host.get("tools") or {}).get(tool) is None:
            n = cite.add(("host", tool), "")
            out.append(("This machine has no %s, so the real build cannot run here" % tool, [n]))
    room = 8 - len(out)
    for e in _list(answers, "finding")[:max(room, 0)]:
        n = cite.add(("finding", e.get("event"), e.get("value")), "")
        out.append((" ".join(str(e.get("value")).split()) + " (my reading of the code)", [n]))
    return out[:8]


def _gate_lines(look, answers, st, cite):
    """Part 4: one bullet per check, and one for each tool the real build needs that the host lacks."""
    bullets = []
    host = (look or {}).get("host") or {}
    builds = (look or {}).get("build_files") or []
    for g in st["gates"]:
        name = g["name"]
        proves = g.get("proves") or _proves(name, g, look)
        suffix = " (suggested by me)" if g.get("suggested") else ""
        trial = g.get("trial")
        if trial:
            n = cite.add(("trial", name), "")
            tried = "Tried it: %s, %.2f s" % ("pass" if trial.get("exit") == 0 else "fail (exit %s)" % trial.get("exit"),
                                              float(trial.get("seconds") or 0))
        else:
            file = (g.get("evidence") or "").replace("repo:", "", 1)
            n = 1 if file in builds else cite.add(("evid", file), "")
            what = {"test": "your tests", "build": "your build", "lint": "your lint command",
                    "configure": "your CMake files"}.get(g.get("kind"), "code from your repo")
            tried = "Not tried yet: it runs %s, so it runs only after your yes" % what
        bullets.append("%s%s: %s. %s [%d]" % (name, suffix, proves, tried, n))
    for tool in host.get("wanted") or []:
        if (host.get("tools") or {}).get(tool) is None:
            n = cite.add(("host", tool), "")
            bullets.append("Not a gate here: the real build. It needs %s, which this machine lacks [%d]" % (tool, n))
    return bullets


def _proves(name, g, look):
    tracked = (look or {}).get("tracked") or {}
    if name == "py-syntax":
        return "the %d Python files parse" % tracked.get("python_files", 0)
    if name == "cmake-configure":
        return "CMake still configures with this machine's compiler"
    if name in ("build", "test", "lint"):
        verb = {"build": "the project builds", "test": "the tests pass", "lint": "the lint command passes"}[name]
        return "%s, with `%s`" % (verb, " ".join(g.get("command") or []))
    return "`%s` exits 0" % " ".join((g.get("command") or [])[:4])


def _footprint_line(fp):
    """Part 1: what the card understood about Alpaca files in the repo, in one line."""
    by = "" if fp["source"] in ("owner", "sensed", "default") else " (my reading of your reply)"
    if fp["value"] == "none":
        return "Alpaca in your repo: no files and no trace of it in commits, branches or pull requests.%s" % by
    if fp["value"] == "files" and fp["source"] == "sensed":
        return "Alpaca in your repo: it already holds Alpaca files (%s)%s" % (", ".join(fp["found"]), _nums(6))
    if fp["value"] == "files":
        return "Alpaca in your repo: you said Alpaca files may live in it.%s" % by
    return "Alpaca in your repo: no files (my default; tell me if they may live there)"


def _why_attach(st, situation):
    """Part 3: why this existing repo is attached, from the answers (op-062 t-691), without the closing full stop. Attach
    holds when the repo must stay free of Alpaca files or when others push. When the owner allows Alpaca files in the
    repo and nobody else pushes, the mode that fits is in place, which is not built in this version: the card says so
    and proposes attach, never a native studio for an existing repo."""
    fp, others = st["footprint"], st["others_push"]
    fixed = {"audit": "you only want a read of the code, and a copy kept apart cannot change it",
             "migrate": "the old code stays in its own repo, and the move happens beside it"}.get(situation)
    if fp["value"] == "files":
        said = ("your repo already holds Alpaca files%s" % _nums(6)) if fp["source"] == "sensed" \
            else "you said Alpaca files may live in it"
        in_place = ("in-place mode, which would put Alpaca files inside your repo, is not built in this version, so I "
                    "propose attach")
        lead = fixed or ("other people push to your repo" if others else None)
        if lead is None:
            return "%s and nobody else pushes. The %s" % (said, in_place)
        return "%s. %s, but the %s" % (lead, said[:1].upper() + said[1:], in_place)
    if fixed:
        return fixed
    if fp["value"] == "none" and fp["explicit"]:
        return "you want no trace of Alpaca in your repo" + (", and other people push to it" if others else "")
    if others:
        return "other people work in your repo, and it stays free of Alpaca files"
    return "your repo already exists with its own history, and it stays clean"


def _trace_lines(st, situation, mode):
    """Part 6: (a sentence, three bullets) for the things the repo shows of Alpaca: the commit identity, the pull request
    footer and the branch prefix, each with its value, defaults included, and why it has it. They are what `apply` writes
    (part 10 repeats them). Native mode has no repo of the owner's, so it has the sentence and no bullets."""
    if mode != "attach":
        return ("No repo of yours is attached, so there is no commit identity, pull request footer or branch prefix to set.", [])
    tr = st["trace"]
    name, email = tr["identity"]
    if situation == "audit":
        intro = "Your repo shows nothing now. If you let changes leave later, it shows this; tell me any line to change:"
    elif tr["proposed"]:
        intro = "You want no trace of Alpaca, so I propose this set; tell me any line to change:"
    else:
        intro = "Your repo will show this of me; tell me any line to change:"
    if tr["footer"]:
        footer = "Pull request footer: on, the body says it was made with an agent assistant (%s)" % tr["footer_why"]
    else:
        footer = "Pull request footer: off, the body names no tool (%s)" % tr["footer_why"]
    if st["host_kind"] != "github":
        footer += ". It is not used here: I send no pull request for this repo"
    cite = _nums(6) if tr["prefix_why"] == "your repo's own rule" else ""
    return intro, ["Commit identity: %s <%s> (%s)" % (name, email, tr["identity_why"]), footer,
                   "Branch prefix: %s (%s)%s" % (tr["prefix"], tr["prefix_why"], cite)]


def render(look, answers, *, date, name, people=None):
    """The card, as text. Same inputs, same bytes. The id in the title and in the long reply form is
    computed over the text with both set to card-00000000 (`card_id`)."""
    text = strip_marks(_render(look, answers, date=date, name=name, people=people))
    return _set_id(text, card_id(text))


def render_marked(look, answers, *, date, name, people=None):
    """The card as `render` makes it, with every span of the owner's words between the marks `ON` and `OFF`.
    `strip_marks` of it is the card. `lint` reads this form, so the owner's words are never checked as the agent's."""
    marked = _render(look, answers, date=date, name=name, people=people)
    return _set_id(marked, card_id(strip_marks(marked)))


#: the two places that hold the card's own id: the title and the long reply form (a card id the owner typed elsewhere
#: is the owner's word and is never read as, or rewritten to, the card's id)
_TITLE_ID = re.compile(r"\A(SETUP PROPOSAL )card-[0-9a-f]{8}")
_LONG_ID = re.compile(r"^(\(The long form works too: )card-[0-9a-f]{8}( yes\))", re.M)


def _set_id(text, cid):
    text = _TITLE_ID.sub(lambda m: m.group(1) + cid, text)
    return _LONG_ID.sub(lambda m: m.group(1) + cid + m.group(2), text)


def card_id(text):
    """`card-` and the first 8 hex of the sha256 of the card with its two id slots at card-00000000."""
    return "card-" + hashlib.sha256(_set_id(text, PLACEHOLDER_ID).encode("utf-8")).hexdigest()[:8]


def _ci_line(look, answers, situation, mode):
    """The part-1 line on where the repo's checks run, or None. CI files in the repo state it themselves (source 6);
    otherwise the owner's answer does. A `do not know` answer gives no line: the card lists it under `Still unknown`."""
    if mode != "attach" or not asks_ci(situation, answers):
        return None
    files = list((((look or {}).get("around") or {}).get("ci")) or [])
    if files:
        shown = ", ".join(files[:2]) + (" and %d more" % (len(files) - 2) if len(files) > 2 else "")
        return "Where checks run: on a CI; the repo holds its files (%s)%s. Its result is not one of my checks." % (
            shown, _nums(6))
    entry = _single(answers, "ci")
    key = (entry or {}).get("option")
    if key not in ("a", "b"):
        return None
    quote = _clause_of((entry or {}).get("quote") or "", key)
    mark = "" if (entry or {}).get("source") == "owner" else " (my reading of your reply)"
    said = " You said %s." % _quote(quote, mark=True) if quote and (entry or {}).get("source") == "owner" else ""
    if key == "a":
        return "Where checks run: only on your machine, no CI%s.%s" % (mark, said)
    return "Where checks run: on a CI service I cannot see from the repo's files%s.%s Its result is not one of my checks." % (
        mark, said)


def _render(look, answers, *, date, name, people):
    st = settings(look, answers, people=people, name=name)
    situation, mode = st["situation"], st["mode"]
    cite = _Cite(look)
    _register(cite, look, answers, st)
    items = []                         # (kind, text): kind in head, par, bullet, num, blank

    def put(kind, text):
        items.append((kind, text))

    # ---- part 1
    put("head", "1. WHAT I UNDERSTOOD (tell me any line that is wrong)")
    goal_by = (_single(answers, "goal") or {}).get("source")
    if st["goal_from_option"]:
        put("bullet", "Your goal, from the option you chose%s: %s" % ("" if goal_by == "owner" else " (my reading of your reply)",
                                                                       _quote(st["goal"])))
    else:
        put("owner", "Your goal, in your words: %s" % _quote(st["goal"], mark=True) if goal_by == "owner"
            else "Your goal, as I read it (my reading of your reply): %s" % _quote(st["goal"]))
    limits = (st["limits"] or "").strip()
    limits_by = (_single(answers, "limits") or {}).get("source")
    if limits and limits.lower().rstrip(".!") not in ("none", "no", "nothing", "n/a"):
        put("owner", "Limits you gave: %s" % _quote(limits, mark=True) if limits_by == "owner"
            else "Limits, as I read them (my reading of your reply): %s" % _quote(limits))
    elif limits:
        put("bullet", "Limits you gave: none")
    done = _value(answers, "done-guess")
    if done:
        put("bullet", "Done when (my first guess; we confirm it before any change): %s" % " ".join(done.split()))
    else:
        put("bullet", "Done when: not agreed yet; we settle it in the first piece of work, before any change")
    why = "; ".join((_owner_reason(r["text"], r["owner"]) if r.get("owner") else r["text"]) + _nums(r.get("source"))
                    for r in st["guess"].get("reasons") or [])
    put("bullet", "Situation: %s. %s." % (LABELS[situation], why[:1].upper() + why[1:]) if why
        else "Situation: %s." % LABELS[situation])
    oc = _single(answers, "others-commit")
    if oc and (situation == "take-over" or (situation == "migrate" and mode == "attach")):
        key = oc.get("option")
        quote = _clause_of(oc.get("quote") or "", key)
        label = {"a": "nobody now", "b": "others, on their own branches", "c": "a team, with reviews"}.get(key, "unknown")
        mark = "" if oc.get("source") == "owner" else " (my reading of your reply)"
        put("bullet", "Others who push: %s%s.%s%s" % (label, mark, " You said %s." % _quote(quote, mark=True) if quote else "",
                                                       " Only you commit, so you merge." if key == "a" else ""))
    mg = _single(answers, "merges")
    said = " ".join(str((mg or {}).get("value") or "").split())
    if situation == "team" and said and (mg or {}).get("option") != "b":
        # the owner named who merges ("Hana merges"): the card says it, in their words, whoever the person is
        if mg.get("source") == "owner":
            put("bullet", "Who merges: your team. You said %s." % _quote(said, mark=True))
        else:
            put("bullet", "Who merges: your team (my reading of your reply: %s)." % _quote(said))
    elif situation == "take-over" and mg and (mg.get("option") in ("a", "b")):
        if mg.get("option") == "b":
            put("bullet", "Who merges: you%s." % ("" if mg.get("source") == "owner" else " (my reading of your reply)"))
        elif said and mg.get("source") == "owner":
            put("bullet", "Who merges: your team. You said %s." % _quote(said, mark=True))
        else:
            put("bullet", "Who merges: your team%s." % ("" if mg.get("source") == "owner" else " (my reading of your reply)"))
    ci = _ci_line(look, answers, situation, mode)
    if ci:
        put("bullet", ci)
    if mode == "attach" and st["footprint"]["value"] != "unasked":          # unasked: part 3 already says the repo gets none
        put("bullet", _footprint_line(st["footprint"]))
    allu = [_own(e.get("value")) if e.get("source") == "owner" else e.get("value") for e in _list(answers, "unknown")[:3]]
    host = (look or {}).get("host") or {}
    if look is not None and not (look.get("around") or {}).get("ci") and (
            _option(answers, "ci") in (None, "c") or not asks_ci(situation, answers) or mode != "attach"):
        allu.append("whether a CI runs outside the repo (none in it%s)" % _nums(6))
    for tool in host.get("wanted") or []:
        if (host.get("tools") or {}).get(tool) is None:
            allu.append("which %s version the real build uses" % tool)
    if allu:
        put("bullet", "Still unknown: " + "; ".join(allu))
    put("blank", "")

    # ---- part 2
    if look is not None:
        put("head", "2. WHAT I FOUND (in a read-only copy outside this folder; your repo is unchanged)")
        for line, ns in _found_lines(look, answers, cite):
            put("bullet", line + _nums(*ns))
    else:
        put("head", "2. WHAT I FOUND")
        put("bullet", "No existing code. We start from nothing, so there is nothing to read yet")
    put("blank", "")

    # ---- part 3
    put("head", "3. WHERE ALPACA SITS: %s mode" % mode)
    if mode == "attach":
        put("par", "Your code stays in its own repo with its own history. Alpaca keeps its notes, records and reports in "
                   "the [[studio]]. A copy of your repo goes into app/ inside the studio, and the studio's own git ignores "
                   "that folder. Your repo gets no Alpaca file.")
        put("par", "Why attach: %s." % _why_attach(st, situation))
    elif situation == "migrate":
        put("par", "The new code lives in the [[studio]], in its own git history. The old repo is read once, "
                   "to learn what it does; it is not kept in sync.")
        put("par", "Why native: you want the new code in a new repo.")
    else:
        put("par", "Your code lives in the [[studio]], in its own git history. Nothing is shared with anyone "
                   "until you decide.")
        put("par", "Why native: there is no code yet, so nothing needs to be kept apart.")
    put("blank", "")

    # ---- part 4
    put("head", "4. CHECKS")
    if st["gates"]:
        put("par", "Each of these is a [[gate]]. I run every gate on your current %s first, so a gate that already fails there "
                   "is not blamed on new work." % (st["base"] or "code"))
        for b in _gate_lines(look, answers, st, cite):
            put("bullet", b)
    elif mode == "attach":
        put("par", "None found. I cannot attach without one check; tell me one command that proves the code works.")
    else:
        put("par", "None yet. The first piece of work adds a test, and that test becomes the first check.")
    put("blank", "")

    # ---- part 5
    est = estimates(look, st["gates"], situation)
    put("head", "5. FIRST STEPS AFTER YOUR YES (your yes approves %s)" % ("a and b" if mode == "attach" else "a only"))
    ask = "about %d minutes of questions to agree on \"done\", then I write the plan" % est["questions"]
    if mode == "attach":
        steps = [
            "Copy your repo into app/ and record the [[baseline]], taken on %s at %s: about %s, no input from you."
            % (st["base"] or "main", (st["look_sha"] or "")[:7], est["baseline"]),
            "Run the [[study]]: modules, how to build, risky areas, each line pointing at a file; about %s for %s, "
            "no input from you." % (est["study"], _count(own_files(look), "own file")),
            {"audit": "Write the answer to your question as the first [[op]]: about %d minutes of questions, then the "
                      "written result." % est["questions"],
             "migrate": "Write a list of what must not change, then start the first [[op]]: %s." % ask,
             "team": "Start the first [[op]], scoped to one %s: %s." % (
                 "pull request" if st["host_kind"] == "github" else "branch", ask)}.get(
                situation, "Start the first [[op]] from your goal: %s." % ask),
        ]
    else:
        steps = ["Agree on what \"done\" means for your goal: about %d minutes of questions, then I write a short plan for "
                 "you to read." % est["questions"],
                 "Add the first [[task]] with its first test, so the first check exists.",
                 "Start the first [[op]] only when you say go."]
    for letter, text in zip("abc", steps):
        put("num", "%s. %s" % (letter, text))
    put("blank", "")

    # ---- part 6
    prefix = st["branch_prefix"] or "alpaca/"
    put("head", "6. HOW A CHANGE REACHES %s" % ("YOUR REPO" if mode == "attach" else "YOU"))
    intro, trace_bullets = _trace_lines(st, situation, mode)
    if mode == "native":
        put("par", "Changes are commits in this folder's own git history. Nothing leaves your machine unless you push it "
                   "yourself. Each [[task]] works on its own branch there, and I merge it into main only when its checks pass "
                   "and you say so: you decide every merge. " + intro)
    elif situation == "audit":
        put("par", "No change leaves the studio. Sending changes is switched off for this setup; I send nothing %s. %s" % ({
            "github": "to GitHub", "git-host": "to your git host", "folder": "anywhere"}[st["host_kind"]], intro))
    else:
        branch = ("Each [[task]] gets its own branch, named %s and the task number (for example %st-001), cut from the "
                  "newest %s. The gates run on the branch and on a trial merge with the newest %s. You approve the exact "
                  "commit." % (prefix, prefix, st["base"], st["base"]))
        team_merge = ((situation == "team" and _option(answers, "merges") != "b")
                      or (situation == "take-over" and _option(answers, "merges") == "a"))
        if st.get("repository"):
            tail = (" Then I push that branch and open a pull request on GitHub. Your team reviews and merges as usual; "
                    "their hooks and CI decide." if team_merge else
                    " Then I push that branch and open a pull request on GitHub, and you merge it there.")
        elif st["host_kind"] == "git-host":
            tail = (" This repo is not on GitHub, so I cannot send the branch for you: I give you the branch and you send it "
                    "where you send changes today.")
        else:
            tail = (" This repo is only a folder on this machine, so I cannot send the branch anywhere for you: I give you "
                    "the branch and you send it where you send changes today.")
        put("par", branch + tail + " " + intro)
    for bullet in trace_bullets:
        put("bullet", bullet)
    put("blank", "")

    # ---- part 7
    put("head", "7. HOW MUCH I DO ALONE: %s" % st["level"])
    put("par", "The [[level]] is %s. %s Why %s: %s. Say \"raise the level\" when you trust the work; I never raise it "
               "myself." % (st["level"], LEVEL_TEXT.get(int(st["level"][1]), ""), st["level"], st["level_why"]))
    if st["over_cap"]:
        put("par", "L5 and L6 are set later, with the level command, after you have seen the work.")
    put("blank", "")

    # ---- part 8
    put("head", "8. WHAT STAYS WITH YOU")
    setting = {"github": "any GitHub setting, ", "git-host": "any setting on your git host, ", "folder": ""}[st["host_kind"]]
    put("par", {
        "start-empty": "Publishing anywhere, merging a task into main, raising the level, deleting files outside this folder.",
        "take-over": ("Every push, approving each commit, raising the level, %sdeleting branches. Merging is your team's call."
                      if _option(answers, "merges") == "a" else
                      "Every push and merge, approving each commit, raising the level, %sdeleting branches.") % setting,
        "team": "Merging (your team's call), approving each commit, any message to your team, raising the level, %s"
                "deleting branches." % setting,
        "audit": "Everything outside this folder, and raising the level.",
        "migrate": "Every push and merge, the cut-over to the new code, deleting old code, approving each commit, raising the "
                   "level.",
    }[situation])
    put("blank", "")

    # ---- part 9
    put("head", "9. UNDO")
    made = "branches and pull requests" if st["host_kind"] == "github" else "branches"
    undo_attach = ("Before your yes there is nothing to undo (the read-only copy goes away after setup). After it: delete this studio "
                   "folder. ")
    put("par", {
        "start-empty": "Before your yes there is nothing to undo. After it: delete this folder; copy your new code out first, "
                       "because it lives inside.",
        "take-over": undo_attach + "Your repo then holds only the %s %s you approved." % (prefix, made),
        "team": undo_attach + "Your repo then holds only the %s %s you approved, and your team can see them." % (prefix, made),
        "audit": undo_attach + "Your repo is never changed.",
        "migrate": undo_attach + "Your repo then holds only what you approved." if mode == "attach" else
        "Before your yes there is nothing to undo (the read-only copy goes away after setup). After it: delete this folder; copy the new "
        "code out first. The old repo is never changed.",
    }[situation])
    put("blank", "")

    # ---- part 10
    put("head", "10. SETTINGS I WILL WRITE")
    parts = []
    if mode == "attach":
        tr = st["trace"]
        parts.append("[[upstream]] %s, [[base]] %s, branch prefix %s, commit identity %s, pull request footer %s, gates %s" % (
            st["source"], st["base"], prefix, tr["identity"][0], "on" if tr["footer"] else "off",
            ", ".join(g["name"] for g in st["gates"])))
    else:
        parts.append("mode native")
    parts.append("level %s" % st["level"])
    if st["people"]:
        who = ", ".join("%s as %s" % (_own(p["name"]), p["role"]) for p in st["people"])    # a name the owner gave is theirs
        parts.append("people: %s%s" % (who, _nums(cite.extra.get(("host", "git_user")))))
    parts.append("plain-writing check %s%s" % ("on" if st["preset"] else "off",
                 " for Alpaca's own reports (suggested by me; your code is not affected)" if st["preset"] else ""))
    put("par", "; ".join(parts) + ".")
    put("blank", "")

    # ---- assemble: glosses at first use, wrap, sources
    lines = _assemble(items, set())
    cited = set(int(n) for n in re.findall(r"\[(\d+)\]", _REGION.sub("", "\n".join(lines))))   # not the owner's [n]
    src_lines = []
    for i, entry in enumerate(cite.entries, 1):
        if i in cited:
            src_lines.extend(_wrap("[%d] %s" % (i, entry), "", " " * len("[%d] " % i)))
    if src_lines:
        src_lines.insert(0, "Sources (repo paths at commit %s)" % (st["look_sha"] or "")[:7])
        src_lines.append("")
    total = len(lines) + len(src_lines) + 5
    minutes = max(1, int(math.ceil(total / 40.0)))
    title = "SETUP PROPOSAL %s for %s" % (PLACEHOLDER_ID, (name or "this project")[:40])
    head = [title + " " * max(2, TITLE_WIDTH - len(title) - len(date)) + date,
            "Nothing is set up until you reply. It takes about %d minutes to read." % minutes]
    head += _wrap("Alpaca records what I do, checks each change, and stops me at the points below.", "", "")
    head.append("")
    tail = ['Reply "yes" to set this up as written, or tell me which line to change.',
            "(The long form works too: %s yes)" % PLACEHOLDER_ID]
    return "\n".join(head + lines + src_lines + tail) + "\n"


def _balanced(text):
    pairs = {")": "(", "]": "[", "}": "{"}
    stack = []
    for ch in text:
        if ch in "([{":
            stack.append(ch)
        elif ch in pairs and (not stack or stack.pop() != pairs[ch]):
            return False
    return not stack


def _whole(text, limit=CLAUSE_MAX):
    """`text` when it fits and has no half a bracket; else its longest start that ends at a comma or semicolon and
    fits; else "". The card quotes the owner's words whole or not at all: it never cuts inside a quote."""
    text = text.strip()
    if len(text) <= limit and _balanced(text):
        return text
    for i in range(min(len(text), limit + 1) - 1, 0, -1):
        if text[i] in ",;" and _balanced(text[:i]):
            return text[:i].rstrip()
    return ""


def _clause_of(quote, key):
    """The sentence of the owner's reply that holds the option token, for the card to quote: whole, or up to a
    comma or semicolon when it is long, or nothing. Never cut inside."""
    if not quote:
        return ""
    parts = [p for p in re.split(r"(?<=[.!?])\s+|\n", quote) if p.strip()]
    for p in parts:
        if key and re.search(r"(?<![\w])\d\s*[).:-]?\s*%s(?![\w])" % re.escape(key), p, re.I):
            p = re.sub(r"^\s*\d\s*[).:-]?\s*%s\s*[,.:);-]*\s*" % re.escape(key), "", p, flags=re.I)
            while re.match(r"\s*\d\s*[).:-]?\s*[a-d](?![\w])", p, re.I):      # "1a, 2a, ..." holds one token for each question
                p = re.sub(r"^\s*\d\s*[).:-]?\s*[a-d](?![\w])\s*[,.:);-]*\s*", "", p, flags=re.I)
            return _whole(p.strip().rstrip(".!?"))
    return ""


_WS = "\t\n\x0b\x0c\r "


def _wrap(text, first, rest, owner=False):
    """Wrap `text` to the card width. A word longer than a line (a long local path) is cut at the line end rather than
    run past it; the owner's own words are the exception, they keep every word whole and a word longer than a line runs
    past the width. Owner marks are not text: the width counts the words alone, and each wrapped line closes and
    reopens the marks it needs, so every line holds its own owner spans."""
    plain = strip_marks(text)
    lines = textwrap.wrap(plain, width=WIDTH, initial_indent=first, subsequent_indent=rest,
                          break_long_words=not owner, break_on_hyphens=False) or [first.rstrip()]
    if plain == text:
        return lines
    flags, on = [], False
    for ch in text:
        if ch == ON:
            on = True
        elif ch == OFF:
            on = False
        else:
            flags.append(on)
    out, at = [], 0
    for k, line in enumerate(lines):
        indent = first if k == 0 else rest
        res, state = [indent], False
        for ch in line[len(indent):]:
            if ch in _WS:
                res.append(ch)
                continue
            while at < len(plain) and plain[at] in _WS:
                at += 1
            if flags[at] != state:
                state = flags[at]
                res.append(ON if state else OFF)
            at += 1
            res.append(ch)
        if state:
            res.append(OFF)
        out.append("".join(res))
    return out


def _assemble(items, used):
    """Replace each [[word]] by the word with its gloss at the first use and by the bare word after,
    then wrap every item. The gloss is a parenthesis, so a part-4 heading that already holds a
    parenthesis gets the gloss inside its own."""
    out = []
    for kind, text in items:
        if kind == "owner":                      # the owner's goal or limits: wrapped, never reworded
            out.extend(_wrap(text, "- ", "  ", owner=True))
            continue

        def sub(m, text=text):
            word = m.group(1)
            if word in used:
                return word
            used.add(word)
            return "%s (%s)" % (word, GLOSSARY[word])
        # a gloss marker is the agent's: a "[[gate]]" the owner typed stays as typed
        pieces = re.split("(%s[^%s]*%s)" % (ON, OFF, OFF), text)
        text = "".join(p if i % 2 else re.sub(r"\[\[([a-z]+)\]\]", sub, p) for i, p in enumerate(pieces))
        if kind == "blank":
            out.append("")
        elif kind == "head":
            out.extend(_wrap(text, "", "   "))
        elif kind == "bullet":
            out.extend(_wrap(text, "- ", "  "))
        elif kind == "num":
            out.extend(_wrap(text, "", "   "))
        else:
            out.extend(_wrap(text, "", ""))
    return out


# ------------------------------------------------------------------------------------------- lint

HEADINGS = ("1. WHAT I UNDERSTOOD", "2. WHAT I FOUND", "3. WHERE ALPACA SITS", "4. CHECKS", "5. FIRST STEPS AFTER YOUR YES",
            "6. HOW A CHANGE REACHES", "7. HOW MUCH I DO ALONE", "8. WHAT STAYS WITH YOU", "9. UNDO", "10. SETTINGS I WILL WRITE")


def owner_block_lines(lines):
    """The indexes of the lines that hold the owner's goal or limits (the bullets of part 1 that quote them, with
    their wrapped lines). They are the owner's words, so they do not count toward the line cap of the card."""
    block, inside = set(), False
    for i, line in enumerate(lines):
        if line.startswith("- "):
            inside = line.startswith(("- Your goal,", "- Limits you gave:", "- Limits, as I read them"))
        elif not line.startswith("  "):
            inside = False
        if inside:
            block.add(i)
    return block


def owner_spans(look, answers, people=None):
    """The owner's words the card prints, as strings: the goal, the limits, and each quote the card draws from the
    opening or from an answer. They are the spans `render_marked` marks, read back from a card made from the same look
    and answers (the date, the name and the people of a card do not change them)."""
    flat = " ".join(_render(look, answers, date="2000-01-01", name="x", people=people).split())
    flat = flat.replace(OFF + " " + ON, " ")                    # one span wrapped over several lines
    return [m.group(0)[1:-1].strip() for m in _REGION.finditer(flat)]


def _bare(card, look, answers, people=None):
    """(the card without marks, the card with every owner span removed). A marked card (`render_marked`) names its
    spans exactly. A plain card has them read from the look and the answers, and each is taken out wherever its words
    stand in a row, over any wrap."""
    if ON in card or OFF in card:
        return strip_marks(card), _REGION.sub("", card)
    bare = card
    for span in sorted(set(owner_spans(look, answers, people)), key=len, reverse=True):
        words = span.split()
        if words:
            bare = re.sub(r"\s+".join(re.escape(w) for w in words), "", bare)
    return card, bare


def agent_text(card, look, answers, people=None):
    """The card with all owner spans removed: the text the agent wrote, which every check of `lint` reads."""
    return _bare(card, look, answers, people)[1]


def lint(card, look, answers, people=None):
    """Problems of a card against the rules of design 5.1; an empty list is a clean card. Every check runs on the card
    with all the owner's words taken out (`agent_text`), so a problem always names text the agent wrote: the owner's
    accents, [n], card ids and Alpaca words never make a card fail. Give it the marked card (`render_marked`), or a
    plain one (then name the `people` it was made with, when it has any)."""
    problems = []
    card, bare = _bare(card, look, answers, people)
    lines = card.split("\n")
    if lines and lines[-1] == "":
        lines = lines[:-1]
    owner = owner_block_lines(lines)
    if len(lines) - len(owner) > MAX_LINES:
        problems.append("%d lines outside your goal and limits; at most %d" % (len(lines) - len(owner), MAX_LINES))
    for i, line in enumerate(lines, 1):
        if len(line) > WIDTH and not (i - 1 in owner and " " not in line.strip()):
            problems.append("line %d is %d characters; at most %d" % (i, len(line), WIDTH))
    flat = " ".join(bare.split())
    bad = sorted({c for c in flat if ord(c) > 127})
    if bad:
        problems.append("non-ASCII characters outside the owner's words: %s" % "".join(bad)[:20])
    last = -1
    for h in HEADINGS:
        at = bare.find("\n" + h)
        if at < 0:
            problems.append("missing part: %s" % h)
        elif at < last:
            problems.append("part out of order: %s" % h)
        else:
            last = at
    body, _, sources = bare.partition("\nSources (")
    if not sources and re.search(r"\[\d+\]", body):
        problems.append("the card cites a source but has no Sources list")
    listed = set(int(n) for n in re.findall(r"^\[(\d+)\] ", sources, re.M))
    used = set(int(n) for n in re.findall(r"\[(\d+)\]", body))
    for n in sorted(used - listed):
        problems.append("[%d] has no Sources entry" % n)
    for n in sorted(listed - used):
        problems.append("Sources entry [%d] is never cited" % n)
    part2 = re.search(r"\n2\. WHAT I FOUND.*?\n\n", bare, re.S)
    if part2 and look is not None:
        text = re.sub(r"\n   ", " ", part2.group(0))
        for line in text.split("\n- ")[1:]:
            if not re.search(r"\[\d+\]\s*$", line.strip()):
                problems.append("a found line has no source: %s" % line.strip()[:50])
    said_body = flat.partition(" Sources (repo paths")[0]          # the card's own words: a word the owner typed needs no gloss
    for word, gloss in GLOSSARY.items():
        n = flat.count(gloss)
        used_word = re.search(r"(?<![\w])%s(?![\w])" % word, said_body, re.I)
        if n > 1:
            problems.append("the gloss of %s appears %d times" % (word, n))
        if n == 1 and not used_word:
            problems.append("the gloss of %s appears but the word is not used" % word)
        if used_word and n == 0:
            first = re.search(r"(?<![\w])%s(?![\w])" % word, said_body, re.I)
            problems.append("%s is used without its gloss (first at: %s)" % (word, said_body[first.start():first.start() + 30].replace("\n", " ")))
    if "WHAT STAYS WITH YOU" not in bare or "UNDO" not in bare:
        problems.append("the undo or the stays-with-you part is missing")
    if bare.count(PLACEHOLDER_ID):
        problems.append("the id slots are not filled")
    ids = set(re.findall(r"card-[0-9a-f]{8}", bare))
    if len(ids) != 1:
        problems.append("the card must hold exactly one id, found %d" % len(ids))
    return problems


def normalize_shown(text):
    """The card as the check for 'the owner was shown it' compares: lines that are only a code fence
    dropped, a leading `> ` of each line removed, every run of white space one space."""
    out = []
    for line in str(text).split("\n"):
        stripped = line.strip()
        if re.fullmatch(r"`{3,}[A-Za-z0-9_-]*", stripped):
            continue
        stripped = re.sub(r"^(?:>\s?)+", "", stripped)
        out.append(stripped)
    return " ".join(" ".join(out).split())


_HEAD = re.compile(r"^(\d{1,2})\. [A-Z]")


def parts(card):
    """The card split into its ten numbered parts and its Sources: {1: text, ..., 10: text, "sources": text}, each
    with white space collapsed. The title, the read note and the reply lines are left out (they carry the id)."""
    out, key, buf = {}, None, []
    for line in card.split("\n"):
        m = _HEAD.match(line)
        if m and int(m.group(1)) == (key or 0) + 1 and key != "sources":
            if key is not None:
                out[key] = " ".join(" ".join(buf).split())
            key, buf = int(m.group(1)), [line]
        elif line.startswith("Sources ("):
            if key is not None:
                out[key] = " ".join(" ".join(buf).split())
            key, buf = "sources", [line]
        elif line.startswith('Reply "yes"'):
            break
        elif key is not None:
            buf.append(line)
    if key is not None:
        out[key] = " ".join(" ".join(buf).split())
    return out


def changed_parts(old, new):
    """The keys of the parts of `new` that differ from `old` (or are new)."""
    a, b = parts(old), parts(new)
    return [k for k in b if a.get(k) != b[k]]
