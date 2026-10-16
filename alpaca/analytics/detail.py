"""Bounded, read-only session conversations from explicitly authorized sources.

The record registers external transcript paths. A request only supplies a session
ID and, optionally, an opaque child ID discovered under that transcript's child
directory. No account-wide discovery, database migration, or capture occurs here.
Counts describe the selected conversation, never a fold of its children. When a
read budget is exhausted, counts are lower bounds and coverage says so explicitly.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import threading
from contextlib import contextmanager

from alpaca import db, paths, pool, transcripts


MAX_READ_BYTES = 32 * 1024 * 1024
MAX_LINE_BYTES = 2 * 1024 * 1024
MAX_CONTENT_CHARS = 256 * 1024
MAX_RECORDS = 50_000
MAX_CHILDREN = 40
MAX_DIRECTORY_ENTRIES = 2000
MAX_PAGE = 200
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,159}\Z")
# The plain-phrasing patterns below match runs of spaces and words possessively (`X*+`, `X++`), which
# keeps the screen linear. Python 3.10, the oldest Python Alpaca runs on, has no possessive
# quantifier (it came in 3.11), so there each one is written as the atomic group it means: a
# lookahead that takes the whole run and a reference that consumes it, `(?=(?P<_atomN>X*))(?P=_atomN)`.
# The lookahead never gives a character back, so the matches and the linear time are those of the
# possessive pattern; only the added `_atomN` groups differ. On 3.11+ a pattern compiles as written.
ATOM_GROUP_PREFIX = "_atom"
_POSSESSIVE = re.compile(r"(\[(?:\\.|[^\]\\])+\]|\\.)([*+])\+")


def atomic_pattern(pattern: str) -> str:
    """The pattern with each possessive class or escape run written as a lookahead atomic group."""
    count = iter(range(len(pattern)))

    def atomic(match: re.Match[str]) -> str:
        name = "%s%d" % (ATOM_GROUP_PREFIX, next(count))
        return "(?=(?P<%s>%s%s))(?P=%s)" % (name, match.group(1), match.group(2), name)
    return _POSSESSIVE.sub(atomic, pattern)


#: Each pattern compiled by `_possessive`, mapped to the pattern as written (with its possessive runs).
POSSESSIVE_SOURCES: dict[re.Pattern[str], str] = {}


def _possessive(pattern: str, flags: int = 0) -> re.Pattern[str]:
    compiled = re.compile(pattern if sys.version_info >= (3, 11) else atomic_pattern(pattern), flags)
    POSSESSIVE_SOURCES[compiled] = pattern
    return compiled


_SECRET_KEY = re.compile(r"(?:password|passwd|passphrase|api[_-]?key|access[_-]?token|refresh[_-]?token|authorization|cookie|client[_-]?secret|private[_-]?key|secret[_-]?(?:access[_-]?)?key)\Z", re.I)
_SECRET_TEXT = re.compile(r"(?i)(\b(?:authorization\s*:\s*(?:bearer|basic)|bearer)\s+)[^\s\"']+|"
                          r"(\b(?=[A-Z0-9_]*\s*[=:])(?:[A-Z0-9_]*(?:API_KEY|ACCESS_TOKEN|SECRET|PASSWORD|PASSPHRASE|SECRET_ACCESS_KEY|SECRET_KEY))\s*[=:]\s*[\"']?)[^\s\"']+")
# The lookahead of the second branch is implied by the rest of it (the name ends the identifier, and
# "=" or ":" follows): it only fails a word with no "=" or ":" after it before the alternation of
# names is tried at each of its letters (t-070 L1, a third less time, same matches).
_PROVIDER_KEY = re.compile(r"\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,})\b")
# The classes FR-26 found missing (HS-SCO-05): an AWS access key id, a PEM private key block (through
# its END line, or through its base64 body when the END line was cut off), a Slack token or webhook
# and a JWT. Each is masked whole.
_AWS_KEY = re.compile(r"\b(?:A3T[A-Z0-9]|AKIA|ASIA|ABIA|ACCA)[A-Z0-9]{16}\b")
# A PEM private key block: its BEGIN line through the first END line of the same label after it
# (anywhere later in the text), else, when no such END line follows, its base64 body and header
# lines. As one regex (`BEGIN ...-----(?:.*?-----END \1-----|...)`) each BEGIN line with no END line
# scanned to the end of the text, so k of them cost k times the text (t-070 A-5: 1,000 in 2 MB took
# 3.5 s a pass); `_pem_spans` finds the same span from one index of the END lines, in linear time.
_PEM_HEAD = re.compile(r"-----BEGIN ([A-Z0-9 ]{0,40}PRIVATE KEY(?: BLOCK)?)-----")
_PEM_FOOT = re.compile(r"-----END ([A-Z0-9 ]{0,40}PRIVATE KEY(?: BLOCK)?)-----")
_PEM_TAIL = re.compile(r"(?:(?:\s|\\[nr])*(?:[A-Za-z0-9+/=]{16,}|[A-Z][A-Za-z-]*:[^\n\\]*))*")
_SLACK_TOKEN = re.compile(r"\bxox[a-z](?:\.xox[a-z])?-[A-Za-z0-9-]{8,}|\bxapp-[A-Za-z0-9-]{8,}"
                          r"|https://hooks\.slack(?:-gov)?\.com/(?:services|workflows|triggers)/[A-Za-z0-9/_-]+")
# A JWT: "eyJ" after a word boundary, then three dot-separated parts of the token alphabet, the first
# two of four characters or more. As one regex (`\beyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\.[...]*`,
# through bfb66b189) every "eyJ" inside one run of the alphabet restarted the match and scanned to the
# end of the run, since "-" both ends a word and stays in the run ("eyJ-" repeated: 2.1 s at 128 KB,
# t-070 check round 1 A-3); `_jwt_spans` reads each run once and gives the regex's matches.
_JWT_RUN = re.compile(r"[A-Za-z0-9_-]*")
_WORD_CHAR = re.compile(r"\w")
# A secret given in a plain phrasing (dec-804f2708474c Q-A): a credential name, an optional "for the
# quay camera" of up to six words, then "is", "was", "are" or "were" (optionally "now", "still",
# "set to", "changed to", "reset to") or "=" or ":", then the value. A bare "code" or "key" is no
# credential name ("the exit code is 2" and a note's "key: t-069" line stay). A quoted value after a
# prose verb is masked whatever it holds, one after "=" or ":" only when it holds a digit, so a code
# or config literal such as pin='release' stays. A bare value is masked when it holds a digit; one
# with no digit only after a prose verb, when it is a word of letters of six or more that no
# ordinary word starts ("The wifi password is swordfishmarlin." is masked, "the token is valid" and
# "the password is stored in the vault" stay), and never for a PIN or a bare "secret", whose
# letters read as prose ("the pin is loose", "the secret is out"). A passphrase's value runs over
# its words to the end of the clause ("The vault passphrase is plankton otter quayside."), up to a
# word that starts another credential phrasing. The value never spans a bracket, quote or
# backslash, so a mask is never extended or re-masked (screening twice gives the text screening
# once gave). The spaces and tabs before the verb, after it, and after "=" or ":" are matched
# possessively (`[ \t]*+`): no value, verb, "=" or ":" starts with a space or a tab, so giving one
# back never makes a match, and the two `[ \t]*` that sat side by side over one run of spaces tried
# every split of it before a value failed ("password is" + 16,000 spaces: 4.8 s through bfb66b189,
# t-070 check round 1 A-3). The output is the one the bfb66b189 pattern gave
# (`T/wiki/test_screen_door.py::test_the_screen_changes_only_where_a_declared_variant_applies`).
_PLAIN_NAMES = (r"(?P<phrase>pass[ \t]?phrases?)|(?P<weak>pin(?:[ \t]+(?:code|number))?|secret)"
                r"|pass(?:word|wd|code)|token|credentials?"
                r"|(?:api|access|secret|private)[ \t_-]?key|(?:access|security)[ \t]+code")
_VERB = r"(?:is|was|are|were)\b"
_BARE = r"[^\s\"'`<>()\[\]{}\\]"
_WORD = r"[^\s.,;:!?\"'`<>()\[\]{}\\]"
_NEXT_NAME = r"pass|token|pin\b|secret|credential|(?:api|access|private)[ \t_-]?key"
_FOR = r"(?:[ \t]+(?:for|of|to|on)(?:[ \t]+(?!" + _VERB + r")[^\s.!?;:=\[\]\"'`\\]+){1,6})?"
# Another credential phrasing starting where a bare value would: a credential name, its optional
# "for the ..." words and its verb, "=" or ":". A bare value never starts there (t-070 A-1, m-conf-1:
# "PIN: password is hunter22x" took "password" as the PIN's value, and the phrasing after it was
# never matched), so that phrasing is matched on its own and masks its own value.
_NEXT_PHRASE = (r"\b(?:pass[ \t]?phrases?|pin(?:[ \t]+(?:code|number))?|secret|pass(?:word|wd|code)|token"
                r"|credentials?|(?:api|access|secret|private)[ \t_-]?key|(?:access|security)[ \t]+code)"
                r"[\"']?" + _FOR + r"[ \t]*+(?:\b" + _VERB + r"|[=:])")
_PLAIN_SECRET = _possessive(
    r"(?i)(?P<head>\b(?:" + _PLAIN_NAMES + r")[\"']?" + _FOR +
    r"[ \t]*+(?:(?P<verb>\b" + _VERB + r"(?:[ \t]+(?:now|still|set[ \t]+to|changed[ \t]+to|reset[ \t]+to))?"
    r"[ \t]*+:?)|[=:])[ \t]*+(?:\n[ \t]*+)?)"
    r"(?:(?P<quote>[\"'`])(?P<quoted>[^\"'`\n]{1,200})(?P=quote)"
    r"|(?(phrase)(?P<words>" + _WORD + r"{4,}(?:[ \t]+(?!" + _VERB + r"|" + _NEXT_NAME + r")"
    + _WORD + r"+){0,7})|(?!" + _NEXT_PHRASE + r")(?P<bare>" + _BARE + r"{4,})))")
#: Words that end a credential phrasing without being its value: grammar words, states and common
#: adjectives. A word with an English inflection or derivation ending is ordinary too.
_ORDINARY = frozenset("""
a an the this that these those it its there here what which who whose whom where when why how
not no nor now still also only just even never always already often usually sometimes rarely
too very so then thus therefore however again more most less least much many few several
some any all both each every either neither none nothing something anything everything one ones
other another such own same
in on at of for from to by with without into onto upon under over inside outside within behind
before after during per via like as about above below between among through across against
toward towards around near beside besides beyond past since until till than
me my mine you your yours him his her hers us our ours them their theirs
be been being do does did done has have had
valid invalid correct incorrect wrong right required optional mandatory needed necessary
empty blank null nil none unknown undefined unset set reset changed rotated
true false yes ok okay fine good bad weak strong short long safe unsafe secure insecure
secret private public sensitive hidden visible plain clear clean simple complex new old
current previous next last first second final default random unique static dynamic fixed
global local temporary permanent real fake dummy sample example placeholder test
live dead stale fresh active inactive enabled disabled off open closed gone lost lower upper
identical missing present absent available unavailable ready busy free read write
opaque expired revoked leaked exposed compromised bearer
""".split())
_ORDINARY_ENDINGS = ("ed", "ing", "ly", "able", "ible", "ive", "ous", "ful", "less", "ent", "ant",
                     "ic", "al", "ary", "ory", "ment", "ness", "tion", "sion", "ity", "ise", "ize")


def _ordinary(word):
    """A word that reads as prose after "is", not as a credential's value: listed, or a part of it
    (split at hyphens) listed or of five letters or more with an English ending."""
    return any(part in _ORDINARY or (len(part) >= 5 and part.endswith(_ORDINARY_ENDINGS))
               for part in word.lower().split("-") if part)


def _plain_span(match):
    """The span of a plain phrasing's value to mask, or None when the phrasing reads as prose."""
    quote = match.group("quote")
    if quote:
        if not match.group("verb") and not any(c.isdigit() for c in match.group("quoted")):
            return None
        return match.span("quoted")
    group = "words" if match.group("words") else "bare"
    value = match.group(group)
    core = value.rstrip(".,;:!?")
    if not any(c.isdigit() for c in core):
        first = core.split()[0] if core.split() else core
        if (not match.group("verb") or match.group("weak") or len(first) < 6
                or not re.fullmatch(r"[A-Za-z][A-Za-z-]*", first) or _ordinary(first)):
            return None
    return match.start(group), match.start(group) + len(core)


def _plain_spans(value):
    """The span of each plain phrasing's value to mask (`_plain_span`)."""
    return [span for span in map(_plain_span, _PLAIN_SECRET.finditer(value)) if span]


# The plain phrasings the class above does not read (t-425; op-008 t-070 check round 1, minor A-2: 21
# phrasings stored in clear at all five retain doors, attack p01). They are one more class,
# `plain-variant`, beside `plain-phrasing`, which stays byte for byte what it was at bfb66b189, so the
# screen changes a text only where one of these forms finds a value the other classes leave
# (`T/wiki/test_screen_door.py::test_the_screen_changes_only_where_a_declared_variant_applies`). The
# forms, by family (`VARIANT_FAMILIES`):
#   spacing        a no-break, figure or narrow no-break space, or a carriage return, where a plain
#                  phrasing has a space or a line end ("password is X", "is\r\nX"): the plain
#                  phrasing is read again on the text with each of them as a space (the same positions)
#   verb           a credential name, also plural (passwords, passcodes, API keys, but not tokens,
#                  PINs or secrets, which count things in prose) and the short names pwd and pw, a
#                  closing Markdown bold, a clause (a comma clause of up to eight words, or a "for",
#                  "of", "to", "on", "at", "in", "by", "from" or "with" clause of up to sixteen), then
#                  "is", "was", "are" or "were", "'s", "has (have, had) been changed (set, reset,
#                  updated, rotated) to" or "will (would, shall, should, must) be"; a plural name takes
#                  a list ("X and Y", "X, Y or Z")
#   separator      a credential name, then ":" or "=" with Markdown bold around the name or the colon,
#                  a tab, or a dash between spaces; pwd and pw also with a bare ":" or "=" (never a
#                  path, which `pwd` prints)
#   yaml-block     "password: >" (or "|") with the value on the next indented line
#   spaced-digits  a credential name, its verb, "=" or ":", then digits in groups ("PIN is 7 3 1 9")
#   table          a Markdown table whose header names a password, passcode, passphrase, PIN, pwd,
#                  pw, credential, secret or key: the cells of that column
#   env-name       an upper-case name ending in _PASS, _PASSWD, _PWD, _PW or _PIN, then "=" or ":"
#   reverse        "use X as (the) password", unless a noun follows that makes the name its
#                  modifier ("use sha256 as password hash")
# A value is masked when it holds a digit. A value of letters alone is masked only by the rule of the
# plain phrasing (a word of six letters or more that no ordinary word starts, never for a PIN, a bare
# secret, pwd or pw) and only after "is", "was", "are", "were", "has been changed to" or "will be"
# with no clause. An env-name value too needs a digit: such names also end code constants that hold
# a reason or a state (`R_PIN = "PINNED-BARRIER-UNUSABLE-REFUSES"`, `R_CLEARANCE_NOT_PASS = "..."`,
# both in the evaluation corpus's transcripts).
# Each form is read from a credential name or a table line, never across a bracket, quote or
# backslash, so a mask is never extended and screening twice gives what screening once gave; the
# clause words and the spaces are matched possessively, so each start reads a bounded stretch.
_V_SPACES = {0x00A0: " ", 0x2007: " ", 0x202F: " ", 0x000D: " "}
_V_NAME = (r"pass(?:word|wd|code)s?|pass[ \t]?phrases?|pin(?:[ \t]+(?:code|number))?|secret|token"
           r"|credentials?|(?:api|access|secret|private)[ \t_-]?keys?|(?:access|security)[ \t]+codes?"
           r"|pwd|pw")
_V_WEAK = re.compile(r"(?i)pin(?:[ \t]+(?:code|number))?|secret|pwd|pw")
_V_ITEM = r"[^\s,;\"'`<>()\[\]{}\\]{4,}"
_V_WORD = r"[^\s.,;:!?=\[\]\"'`\\]++"
_V_BOLD = r"(?:\*{1,2}|_{1,2})"
_V_VERB = (r"(?:is|was|are|were)\b(?:[ \t]++(?:now|still|set[ \t]+to|changed[ \t]+to|reset[ \t]+to)\b)?"
           r"|(?:has|have|had)[ \t]++been[ \t]++(?:changed|set|reset|updated|rotated)[ \t]++to\b"
           r"|(?:will|would|shall|should|must)(?:[ \t]++now)?[ \t]++be\b")
_V_NOT_VERB = r"(?!(?:is|was|are|were|has|have|had|will|would|shall|should|must)\b)"
_V_CLAUSE = (r"(?P<clause>[ \t]*+,(?:[ \t]++" + _V_WORD + r"){1,8}[ \t]*+,"
             r"|[ \t]++(?:for|of|to|on|at|in|by|from|with)\b(?:[ \t]++" + _V_NOT_VERB + _V_WORD
             + r"){1,16})?")
_V_PROSE = _possessive(
    r"(?i)\b(?P<name>" + _V_NAME + r")" + _V_BOLD + r"?" + _V_CLAUSE
    + r"[ \t]*+(?:\b(?P<verb>" + _V_VERB + r")|(?P<owns>['\u2019]s\b))"
    r"[ \t]*+" + _V_BOLD + r"?[ \t]*+(?:\n[ \t]*+)?"
    r"(?:(?P<quote>[\"'`])(?P<quoted>[^\"'`\n]{1,200})(?P=quote)"
    r"|(?!" + _NEXT_PHRASE + r")(?P<bare>" + _V_ITEM + r"))")
_V_LIST_NEXT = _possessive(r"(?i)(?:[ \t]*+,[ \t]*+(?:(?:and|or)[ \t]++)?|[ \t]++(?:and|or|&)[ \t]++)"
                          r"(?P<item>" + _V_ITEM + r")")
_V_SEP = _possessive(
    r"(?i)\b(?P<name>" + _V_NAME + r")(?:" + _V_BOLD + r"[ \t]*+[:=]|[:=][ \t]*+" + _V_BOLD
    + r"|\t++|[ \t]++(?:--?|\u2013|\u2014)[ \t]++)[ \t]*+(?P<bare>" + _V_ITEM + r")")
_V_SHORT = _possessive(r"(?i)\b(?P<name>pwd|pw)[\"']?[ \t]*+[:=][ \t]*+(?P<bare>" + _V_ITEM + r")")
_V_YAML = _possessive(r"(?i)\b(?P<name>" + _V_NAME + r")[\"']?[ \t]*+:[ \t]*+[>|][+-]?[0-9]?"
                     r"(?:[ \t]*+\n(?:[ \t]*+\n){0,3}[ \t]++|[ \t]++)(?P<bare>" + _V_ITEM + r")")
_V_DIGITS = _possessive(
    r"(?i)\b(?P<name>" + _V_NAME + r")[\"']?" + _FOR + r"[ \t]*+(?:\b(?:" + _V_VERB + r")|[=:])"
    r"[ \t]*+(?P<digits>\d{1,4}(?:[ \t.-]\d{1,4}){1,7})(?![\w-])")
_V_ENV = _possessive(r"\b[A-Z][A-Z0-9_]*_(?:PASS|PASSWD|PWD|PW|PIN)[ \t]*+[=:][ \t]*+[\"']?"
                    r"(?P<bare>[^\s\"'`<>()\[\]{}\\]{4,})")
#: A noun after "as (the) password" makes the name a modifier ("use sha256 as password hash"), not
#: the thing the value is used as.
_V_MODIFIED = (r"(?:hash(?:es|ing|er)?|manager|field|prompt|polic(?:y|ies)|length|input|storage|salt"
               r"|stores?|files?|resets?|rules?|algorithms?|schemes?|validat\w*|strength|generat\w*"
               r"|checks?|encod\w*|encrypt\w*|formats?|hints?|protect\w*)\b")
_V_REVERSE = _possessive(
    r"(?im)\b(?:use|using|set|try|enter|type)[ \t]++(?P<bare>" + _V_ITEM + r")[ \t]++as[ \t]++"
    r"(?:(?:the|a|an|my|your|our|its|their|new|admin|root|default|initial)[ \t]++){0,3}"
    r"(?:" + _V_NAME + r")\b(?![ \t]*+(?:-|" + _V_MODIFIED + r"))")
_V_TABLE_HEAD = re.compile(r"(?i)\b(?:pass(?:word|wd|code)s?|pass[ \t]?phrases?|pins?|pwd|pw"
                           r"|credentials?|secrets?|(?:api|access|secret|private)[ \t_-]?keys?)\b")
_V_TABLE_RULE = re.compile(r"[ \t]*\|?(?:[ \t]*:?-{3,}:?[ \t]*\|)+[ \t]*(?::?-{3,}:?[ \t]*)?\Z")
_V_RULE_CELL = re.compile(r"[ \t]*:?-{3,}:?[ \t]*")
_V_CELL = re.compile(r"[ \t]*(?:\*{1,2}|`)?(?P<core>[^\s|\"'`<>()\[\]{}\\]{4,}?)(?:\*{1,2}|`)?[ \t]*\Z")
_V_LETTERS = re.compile(r"[A-Za-z][A-Za-z-]*")


def _v_core(text, start, item):
    """The span of a value without the punctuation or Markdown that closes it."""
    core = item.rstrip(".:!?*")
    return (start, start + len(core)) if core else None


def _v_value(text, start, item, letters):
    """The span of a value to mask: one holding a digit, or with `letters` a word of letters the
    plain phrasing would mask (`_plain_span`), else None."""
    span = _v_core(text, start, item)
    if span is None:
        return None
    core = text[span[0]:span[1]]
    if any(c.isdigit() for c in core):
        return span
    if letters and len(core) >= 6 and _V_LETTERS.fullmatch(core) and not _ordinary(core):
        return span
    return None


def _v_spacing(text):
    return _plain_spans(text)


def _v_prose(text):
    spans = []
    for m in _V_PROSE.finditer(text):
        name = m.group("name")
        weak = _V_WEAK.fullmatch(name) is not None
        letters = bool(m.group("verb")) and not m.group("clause") and not weak
        if m.group("quote"):
            quoted = m.group("quoted")
            if any(c.isdigit() for c in quoted) or letters:
                spans.append(m.span("quoted"))
            continue
        span = _v_value(text, m.start("bare"), m.group("bare"), letters)
        if span:
            spans.append(span)
        if name.lower().endswith("s"):           # a plural name takes a list of values
            at = m.end("bare")
            for _ in range(7):
                more = _V_LIST_NEXT.match(text, at)
                if not more:
                    break
                span = _v_value(text, more.start("item"), more.group("item"), letters)
                if span:
                    spans.append(span)
                at = more.end()
    return spans


def _v_digit_spans(pattern, path_free=False):
    def find(text):
        spans = []
        for m in pattern.finditer(text):
            item = m.group("bare")
            if path_free and item[0] in "/~.":
                continue
            span = _v_value(text, m.start("bare"), item, False)
            if span:
                spans.append(span)
        return spans
    return find


def _v_spaced_digits(text):
    return [m.span("digits") for m in _V_DIGITS.finditer(text)
            if sum(c.isdigit() for c in m.group("digits")) >= 4]


def _v_env(text):
    return [span for span in (_v_value(text, m.start("bare"), m.group("bare"), False)
                              for m in _V_ENV.finditer(text)) if span]


def _v_table(text):
    """The cells under a credential header of each Markdown table: a line of cells, a rule line
    ("|---|---|"), then the lines that start with "|"; and the same table on one line, as an event
    note writes one (its line ends become spaces: "| a | password | |---|---| | b | X |"). Each line
    is split once and each cell read once."""
    spans, columns, previous, start = [], None, None, 0
    for line in text.split("\n"):
        cells, at = line.split("|"), start
        offsets = []
        for cell in cells:
            offsets.append(at)
            at += len(cell) + 1
        piped = line.lstrip(" \t").startswith("|")
        if columns is not None and piped:
            for index in columns:
                if index < len(cells):
                    _v_cell(text, cells[index], offsets[index], spans)
        elif columns is not None:
            columns = None
        if columns is None and piped and previous is not None and _V_TABLE_RULE.match(line):
            head = previous.split("|")
            columns = [i for i, cell in enumerate(head) if _V_TABLE_HEAD.search(cell)] or None
        elif columns is None and len(cells) > 3:
            _v_table_inline(text, cells, offsets, spans)
        previous = line if piped else None
        start += len(line) + 1
    return spans


def _v_cell(text, cell, offset, spans):
    found = _V_CELL.match(cell)
    if found:
        span = _v_value(text, offset + found.start("core"), found.group("core"), False)
        if span:
            spans.append(span)


def _v_table_inline(text, cells, offsets, spans):
    """A table on one line: k header cells, a blank cell (a line end made a space), k rule cells,
    then rows of a blank cell and k cells. Read left to right, each cell once."""
    i, n = 0, len(cells)
    while i < n:
        if not _V_RULE_CELL.fullmatch(cells[i]):
            i += 1
            continue
        j = i
        while j < n and _V_RULE_CELL.fullmatch(cells[j]):
            j += 1
        k, first = j - i, i - 1 - (j - i)
        if first >= 0 and not cells[i - 1].strip():
            marked = [c for c in range(k) if _V_TABLE_HEAD.search(cells[first + c])]
            row = j
            while marked and row + k < n and not cells[row].strip():
                for c in marked:
                    _v_cell(text, cells[row + 1 + c], offsets[row + 1 + c], spans)
                row += k + 1
            j = max(j, row)
        i = j


#: The families of `plain-variant`, in the order `_variant_spans` reads them: (name, spans). Each reads
#: the text with the spaces of `_V_SPACES` as plain spaces; the positions are those of the text given.
VARIANT_FAMILIES = (
    ("spacing", _v_spacing),
    ("verb", _v_prose),
    ("separator", _v_digit_spans(_V_SEP)),
    ("short-name", _v_digit_spans(_V_SHORT, path_free=True)),
    ("yaml-block", _v_digit_spans(_V_YAML)),
    ("spaced-digits", _v_spaced_digits),
    ("table", _v_table),
    ("env-name", _v_env),
    ("reverse", _v_digit_spans(_V_REVERSE)),
)


def _variant_spans(value, families=VARIANT_FAMILIES):
    """The span of each value a `plain-variant` form gives (see above)."""
    text = value.translate(_V_SPACES)
    spans = []
    for name, find in families:
        if name == "spacing" and text == value:
            continue                              # the plain phrasing already read this text
        spans.extend(find(text))
    return spans


def _hash(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]


def _real(path):
    """os.path.realpath(path), kept for the rest of the revision batch when one is open on this
    thread (revision_batch): a batch reads every conversation's revision as of its start, and the
    containment checks below resolved the same root and directories about 120 times a
    conversation (t-680). Outside a batch every call resolves."""
    memo = getattr(_BATCH, 'real', None)
    if memo is None:
        return os.path.realpath(path)
    key = os.fspath(path)
    value = memo.get(key)
    if value is None:
        value = memo[key] = os.path.realpath(key)
    return value


def _inside(path, directory):
    try:
        real = _real(directory)
        return os.path.commonpath([_real(path), real]) == real
    except (ValueError, OSError):
        return False


def _local_file(path, root):
    return _inside(path, root) and os.path.isfile(path)


#: The most passes the screen makes over a string before it calls the string unsettled. One pass
#: applies each class once in a fixed order, and a later class can make an earlier one match: a
#: Slack mask glued to an AWS key id gives the key its word boundary. A pass that changes the text
#: only turns secret text into `[redacted]` and never extends a mask, so the passes settle; on glued
#: pairs of every class two changing passes were the most seen (t-069 fix round 2).
_REDACT_PASSES = 8

#: Literal text each class cannot match without (each pattern above names it), so a pass skips a
#: class whose text holds none of it; the test is exact, so a skipped class would have changed
#: nothing. A class matched without regard to case is tested by a regex of the same flags, so the
#: case folding of the class and of its test agree.
_PROVIDER_HINTS = ("sk-", "ghp_", "gho_", "ghu_", "ghs_", "ghr_")
_AWS_HINTS = ("A3T", "AKIA", "ASIA", "ABIA", "ACCA")
_SLACK_HINTS = ("xox", "xapp-", "hooks.slack")
_PLAIN_HINT = re.compile(r"(?i)pass|pin|secret|token|credential|key|code")
_VARIANT_HINT = re.compile(r"(?i)pass|pin|secret|token|credential|key|code|pw")
_SECRET_TEXT_HINT = re.compile(r"(?i)bearer|authorization|api_key|access_token|secret|passw|passphrase")


def _pem_spans(value):
    """The span of each PEM private key block: from its BEGIN line through the first END line of
    the same label after it, else through its base64 body and header lines (`_PEM_TAIL`), as the
    one regex did. The END lines are indexed once, so the cost is linear in the text however many
    BEGIN lines have no END line (t-070 A-5)."""
    from bisect import bisect_left
    ends = {}
    at = value.find("-----END ")
    while at != -1:
        foot = _PEM_FOOT.match(value, at)
        if foot:
            ends.setdefault(foot.group(1), []).append(at)
        at = value.find("-----END ", at + 1)
    spans = []
    head = _PEM_HEAD.search(value)
    while head:
        label = head.group(1)
        found = ends.get(label, ())
        i = bisect_left(found, head.end())
        if i < len(found):
            stop = found[i] + len("-----END ") + len(label) + len("-----")
        else:
            stop = _PEM_TAIL.match(value, head.end()).end()
        spans.append((head.start(), stop))
        head = _PEM_HEAD.search(value, stop)
    return spans


def _mask(value, spans):
    """`value` with each span replaced by `[redacted]`; spans that overlap are one mask, spans that
    only touch stay two."""
    out, pos, open_at, close_at = [], 0, None, None
    for start, stop in sorted(spans):
        if open_at is not None and start < close_at:
            close_at = max(close_at, stop)
            continue
        if open_at is not None:
            out.append(value[pos:open_at])
            out.append("[redacted]")
            pos = close_at
        open_at, close_at = start, stop
    if open_at is not None:
        out.append(value[pos:open_at])
        out.append("[redacted]")
        pos = close_at
    out.append(value[pos:])
    return "".join(out)


def _jwt_spans(value):
    """The span of each JWT, as `\\beyJ[A-Za-z0-9_-]{4,}\\.[A-Za-z0-9_-]{4,}\\.[A-Za-z0-9_-]*` found
    them left to right: the first "eyJ" after a word boundary in a run of the token alphabet whose
    rest of the run is four characters or more, when a "." ends the run and a run of four or more
    ended by a "." follows; the span runs through the run after that. When the first such "eyJ" of
    a run fails, every later one in the run fails too (it leaves fewer characters, and the rest is
    the same), so each run is read once and the cost is linear in the text (t-070 A-3)."""
    spans = []
    at = value.find("eyJ")
    while at != -1:
        if at and _WORD_CHAR.match(value, at - 1):
            at = value.find("eyJ", at + 1)
            continue
        first = _JWT_RUN.match(value, at).end()
        stop = None
        if first - at >= 7 and value.startswith(".", first):
            second = _JWT_RUN.match(value, first + 1).end()
            if second - first > 4 and value.startswith(".", second):
                stop = _JWT_RUN.match(value, second + 1).end()
        if stop is None:
            at = value.find("eyJ", first)
        else:
            spans.append((at, stop))
            at = value.find("eyJ", stop)
    return spans


def _secret_text_spans(value):
    """The span of each bearer token's or secret-named assignment's value (`_SECRET_TEXT`)."""
    return [(m.start() + len(m.group(1) or m.group(2)), m.end()) for m in _SECRET_TEXT.finditer(value)]


def _regex_spans(pattern):
    return lambda value: [m.span() for m in pattern.finditer(value)]


def _holds(hints):
    return lambda value: any(hint in value for hint in hints)


#: Every secret class of the screen, in the order `_redact_once` applies them: (name, a test a text
#: must pass for the class to be tried, the spans of the class's secrets in a text). The test is
#: exact (`_PROVIDER_HINTS` and the rest above). The tests read this registry: each class and the
#: screen as a whole take linear time on generated repeated-input shapes, and each gives the spans
#: the bfb66b189 pattern gave (`T/wiki/test_screen_door.py`, t-070 round 2 C-C).
SCREEN_PATTERNS = (
    ("pem-private-key", _holds(("-----BEGIN ",)), _pem_spans),
    ("plain-phrasing", _PLAIN_HINT.search, _plain_spans),
    ("plain-variant", _VARIANT_HINT.search, _variant_spans),
    ("secret-assignment", _SECRET_TEXT_HINT.search, _secret_text_spans),
    ("provider-key", _holds(_PROVIDER_HINTS), _regex_spans(_PROVIDER_KEY)),
    ("aws-access-key", _holds(_AWS_HINTS), _regex_spans(_AWS_KEY)),
    ("slack-token", _holds(_SLACK_HINTS), _regex_spans(_SLACK_TOKEN)),
    ("jwt", _holds(("eyJ",)), _jwt_spans),
)


def _redact_once(value):
    """One pass of every secret class (`SCREEN_PATTERNS`) over a string. Each class reads the string
    as given and the masks are the union of what they found, so no class's mask hides another
    class's secret, in whatever order the classes are listed: a passphrase's words do not take the
    first part of a JWT or a webhook and leave the rest unmatched, and a value glued to the next
    credential name keeps that name for its own class (t-070 RC2 glued pairs). A mask that makes
    another match possible is found by the next pass (`settle`)."""
    spans = []
    for _name, applies, find in SCREEN_PATTERNS:
        if applies(value):
            spans.extend(find(value))
    return _mask(value, spans) if spans else value


def _screen_once(value):
    """One pass of the screen: every secret class (`_redact_once`), then a mask written in brackets
    spaced apart, since `[[redacted]]` reads as a wikilink to Rune2 (as `drain._summary` spaces
    every `[[`)."""
    value = _redact_once(value)
    if "[redacted]" in value:
        value = value.replace("[[redacted]", "[ [redacted]").replace("[redacted]]", "[redacted] ]")
    return value


def settle(step, value, passes=_REDACT_PASSES):
    """`step` applied until the string stops changing, so the result is a fixed point of `step`
    and applying the whole again changes nothing. A string still changing after `passes` passes is
    masked whole: that keeps the result a fixed point and keeps no secret (fail closed)."""
    for _ in range(passes):
        screened = step(value)
        if screened == value:
            return value
        value = screened
    return "[redacted]"


def screen(value: str) -> str:
    """The one secret screen of a string (FR-26, HS-SCO-05; dec-804f2708474c Q-A): every secret
    class above replaced by `[redacted]` and the masks spaced out of wikilinks, pass after pass in one
    loop until the text stops changing (`settle`), so `screen(screen(x)) == screen(x)`. A text with
    no secret is read once, a text with one secret twice (the pass that masks it and the pass that
    finds nothing more; t-070 A-3). Each class takes time linear in the text on every repeated-input
    shape the tests generate from the credential names, verbs, separators and token start literals
    (`T/wiki/test_screen_door.py::test_screen_is_linear_on_generated_pump_shapes`, t-070 round 2
    C-C); that is a tested property of those shapes, not a proof for every input. The wiki's write
    door calls it through `alpaca.wiki.ingest.drain.screen`, the redactor below for each string."""
    return settle(_screen_once, value)


def _redact(value, depth=0):
    """The secret redactor of Alpaca: a secret-named key's value whole, and every string through the
    one screen (`screen`), so `_redact(_redact(x)) == _redact(x)`."""
    if depth > 24:
        return "[nested content omitted]"
    if isinstance(value, str):
        return screen(value)
    if isinstance(value, dict):
        return {str(k): "[redacted]" if _SECRET_KEY.search(str(k)) else _redact(v, depth + 1)
                for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(v, depth + 1) for v in value]
    return value


def _bounded(value):
    value = _redact(value)
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return (text[:MAX_CONTENT_CHARS], True) if len(text) > MAX_CONTENT_CHARS else (value, False)


def _text(content):
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(b["text"] for b in content if isinstance(b, dict)
                     and b.get("type") in ("text", "input_text", "output_text")
                     and isinstance(b.get("text"), str))


class _Budget:
    def __init__(self):
        self.remaining = MAX_READ_BYTES
        self.records = 0


class _Capture:
    #: How a message text, a tool input or a tool result is kept: `(value, cut)`. Here redacted and
    #: cut to `MAX_CONTENT_CHARS` for a page; the wiki's durable capture keeps each value whole and
    #: screens the projection once (`alpaca.wiki.ingest.drain._visible_transcript`, t-070 RC2).
    _bound = staticmethod(_bounded)

    def __init__(self, sid, kind, budget):
        self.sid = sid
        self.kind = kind
        self.budget = budget
        self.messages = []
        self.tools = {}
        self.title = ""
        self.operator = None
        self.stats = {"malformed_lines": 0, "partial_lines": 0, "bytes_read": 0,
                      "truncated": False, "total_is_exact": True, "unreadable_sources": 0}
        self.visible = None
        self.seen_records = set()

    def rows(self, path, kind, *, max_line_bytes=None):
        # A registered file and its durable project copy are one logical source.
        family = "transcript" if kind.endswith("transcript") else kind
        source_id = _hash(self.sid + ":" + family)
        line_bytes = MAX_LINE_BYTES if max_line_bytes is None else max_line_bytes
        try:
            try:
                with open(str(path) + ".source.json", "rb") as state_file:
                    state = json.loads(state_file.read(65536))
                if state.get("partial_line"):
                    self.stats["partial_lines"] += 1
                    self.stats["total_is_exact"] = False
            except FileNotFoundError:
                pass
            except (OSError, ValueError, AttributeError):
                self.stats["total_is_exact"] = False
                self.stats["malformed_lines"] += 1
            with open(path, "rb") as fh:
                size = os.fstat(fh.fileno()).st_size
                number = 0
                while fh.tell() < size:
                    if self.budget.remaining <= 0 or self.budget.records >= MAX_RECORDS:
                        self.stats.update(truncated=True, total_is_exact=False)
                        break
                    offset = fh.tell()
                    amount = min(line_bytes + 1, self.budget.remaining, size - offset)
                    raw = fh.readline(amount)
                    number += 1
                    self.stats["bytes_read"] += len(raw)
                    self.budget.remaining -= len(raw)
                    self.budget.records += 1
                    if not raw.endswith(b"\n"):
                        if fh.tell() == size:
                            self.stats["partial_lines"] += 1
                            self.stats["total_is_exact"] = False
                        else:
                            self.stats.update(truncated=True, total_is_exact=False)
                            if len(raw) > line_bytes:
                                # A large image/tool result must not hide all later work.
                                # Discard the rest of this one record within the same budget.
                                while self.budget.remaining > 0 and fh.tell() < size:
                                    tail = fh.readline(min(65536, self.budget.remaining))
                                    self.budget.remaining -= len(tail)
                                    self.stats["bytes_read"] += len(tail)
                                    if tail.endswith(b"\n"):
                                        break
                                else:
                                    break
                                if tail.endswith(b"\n"):
                                    continue
                        break
                    if len(raw) > line_bytes:
                        self.stats.update(truncated=True, total_is_exact=False)
                        continue
                    if not raw.strip():
                        continue
                    try:
                        obj = json.loads(raw)
                    except (ValueError, UnicodeError, RecursionError):
                        self.stats["malformed_lines"] += 1
                        self.stats["total_is_exact"] = False
                        continue
                    if not isinstance(obj, dict):
                        self.stats["malformed_lines"] += 1
                        self.stats["total_is_exact"] = False
                        continue
                    if obj.get("type") == "capture-malformed-record":
                        self.stats["malformed_lines"] += 1
                        self.stats["total_is_exact"] = False
                    yield obj, {"id": source_id, "kind": kind, "line": number, "offset": offset}
        except OSError:
            self.stats["unreadable_sources"] += 1

    def message(self, role, text, source, ts=None, identity=None, **extra):
        text, cut = self._bound(text)
        self.stats["truncated"] |= cut
        ident = _hash(source["id"] + ":" + str(identity or source.get("offset", source.get("event_id"))) + ":" + role)
        msg = {"id": ident, "ts": ts, "role": role, "text": text, "tools": [],
               "source": source, "truncated": cut, **extra}
        self.messages.append(msg)
        return msg

    def call(self, msg, ident, name, value, source):
        ident = str(ident or _hash(msg["id"] + ":" + str(len(msg["tools"]))))
        if ident in self.tools:
            return self.tools[ident]
        value, cut = self._bound(value)
        tool = {"id": ident, "name": str(name or "tool"), "input": value, "result": None,
                "status": "unmatched-start", "input_truncated": cut, "result_truncated": False,
                "sources": [source]}
        msg["tools"].append(tool)
        self.tools[ident] = tool
        self.stats["truncated"] |= cut
        return tool

    def result(self, ident, value, source, ts=None, error=False, truncated=False):
        tool = self.tools.get(str(ident))
        if tool is None:
            msg = self.message("tool", "", source, ts, identity=ident)
            tool = self.call(msg, ident, "tool result", None, source)
            tool["status"] = "unmatched-result"
        else:
            tool["status"] = "error" if error else "completed"
        tool["result"], cut = self._bound(value)
        tool["result_truncated"] = bool(cut or truncated)
        if source not in tool["sources"]:
            tool["sources"].append(source)
        self.stats["truncated"] |= tool["result_truncated"]
        return tool

    def transcript(self, path):
        for record, source in self.rows(path, self.kind):
            self.normalize(record, source)

    def normalize(self, rec, source):
        rec = transcripts.visible_record(rec)
        if rec is None:
            return
        kind = rec.get("type")
        ts = rec.get("timestamp") or rec.get("ts")
        identity = rec.get("uuid") or rec.get("id")
        identity = str(identity) if isinstance(identity, (str, int)) else None
        ts = ts if isinstance(ts, str) else None
        if identity:
            key = (str(kind), identity)
            if key in self.seen_records:
                return
            self.seen_records.add(key)
        if kind in ("ai-title", "custom-title"):
            self.title = rec.get("customTitle") or rec.get("aiTitle") or self.title
        elif kind == "queue-operation" and rec.get("operation") == "enqueue":
            text = _text(rec.get("content"))
            if text:
                self.message("user", text, source, ts, identity, queued=True,
                             queue_id=rec.get("queueId") or rec.get("queue_id"))
        elif kind in ("user", "assistant"):
            self.operator = self.operator or "claude"
            message = rec.get("message")
            if not isinstance(message, dict):
                return
            content = message.get("content")
            text = _text(content)
            blocks = content if isinstance(content, list) else []
            calls = [b for b in blocks if isinstance(b, dict) and b.get("type") == "tool_use"]
            msg = None
            if text or calls:
                msg = self.message(kind, text, source, ts, identity,
                                   model=message.get("model"))
                usage = message.get("usage")
                if isinstance(usage, dict):
                    msg["usage"] = {k: v for k, v in usage.items() if k in (
                        "input_tokens", "output_tokens", "cache_read_input_tokens",
                        "cache_creation_input_tokens") and type(v) is int and v >= 0}
                for block in calls:
                    self.call(msg, block.get("id"), block.get("name"), block.get("input"), source)
            for block in blocks:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    value = block.get("content")
                    if isinstance(value, list):
                        value = _text(value)
                    self.result(block.get("tool_use_id"), value, source, ts, block.get("is_error", False))
        elif kind in ("response_item", "event_msg"):
            self.operator = "codex"
            payload = rec.get("payload")
            if not isinstance(payload, dict):
                return
            typ = payload.get("type")
            if kind == "response_item" and typ == "message":
                role = payload.get("role")
                # Only user-visible conversation, never private analysis or summaries.
                if role not in ("user", "assistant") or payload.get("channel") == "analysis":
                    return
                self.codex_message(role, _text(payload.get("content")), source, ts,
                                   payload.get("id") or identity, kind, payload.get("phase"), payload.get("turn_id"))
            elif kind == "event_msg" and typ in ("user_message", "agent_message"):
                self.codex_message("user" if typ == "user_message" else "assistant",
                                   _text(payload.get("message")), source, ts,
                                   payload.get("id") or identity, kind, payload.get("phase"), payload.get("turn_id"))
            elif kind == "response_item" and typ in ("function_call", "custom_tool_call"):
                value = payload.get("arguments", payload.get("input"))
                if isinstance(value, str):
                    try:
                        value = json.loads(value)
                    except (ValueError, RecursionError):
                        pass
                ident = payload.get("call_id") or payload.get("id")
                msg = self.message("assistant", "", source, ts, ident)
                self.call(msg, ident, payload.get("name"), value, source)
                self.visible = None
            elif kind == "response_item" and typ in ("function_call_output", "custom_tool_call_output"):
                self.result(payload.get("call_id"), payload.get("output"), source, ts)

    def codex_message(self, role, text, source, ts, identity, shape, phase, turn_id):
        if not text:
            return
        previous = self.visible
        # Codex exports the same visible turn through two adjacent event shapes.
        # Pair only opposite shapes at the same source timestamp (or native ID),
        # never repeated text within one shape or prompts at different times.
        if previous and previous["shape"] != shape and previous["role"] == role and previous["text"] == text:
            if ((ts and ts == previous["ts"]) or (identity and identity == previous["identity"])
                    or (turn_id and turn_id == previous["turn_id"])):
                previous["message"].setdefault("mirror_sources", []).append(source)
                self.visible = None
                return
        msg = self.message(role, text, source, ts, identity, phase=phase)
        self.visible = {"shape": shape, "role": role, "text": text, "ts": ts,
                        "identity": identity, "turn_id": turn_id, "message": msg}

    def pool(self, path, sid, *, matching_only=False):
        for rec, source in self.rows(path, "tool-pool"):
            if rec.get("sid") != sid or rec.get("phase") not in ("pre", "post"):
                continue
            ident = rec.get("tool_use_id")
            tool = self.tools.get(str(ident)) if ident else None
            if tool is None and matching_only:
                # A session pool can contain child or earlier retained history.
                # Only the selected transcript establishes conversation membership.
                continue
            if tool is None:
                msg = self.message("tool", "", source, rec.get("ts"), identity=ident)
                tool = self.call(msg, ident, rec.get("tool"), rec.get("input"), source)
            elif tool["input"] is None and rec.get("input") is not None:
                tool["input"], tool["input_truncated"] = _bounded(rec["input"])
            tool["input_truncated"] |= bool(rec.get("input_truncated"))
            self.stats["truncated"] |= tool["input_truncated"]
            if source not in tool["sources"]:
                tool["sources"].append(source)
            if rec["phase"] == "post" and rec.get("response") is not None and tool["result"] is None:
                self.result(tool["id"], rec["response"], source, rec.get("ts"),
                            truncated=rec.get("response_truncated", False))


class _SharedConnection:
    """The scope's one read-only connection; callers still close() it, which does nothing."""

    def __init__(self, conn):
        self._conn = conn

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def close(self):
        pass


_SCOPE = threading.local()


@contextlib.contextmanager
def revision_scope(root):
    """Within one batch run (the analytics index), reuse one read-only connection instead of
    opening one per source_revision call (t-577); scoped() memoizes run-wide values."""
    root = os.path.realpath(root)
    if getattr(_SCOPE, "state", None) is not None:
        yield
        return
    conn = db.connect_readonly(root)
    _SCOPE.state = (root, conn, {})
    try:
        yield
    finally:
        _SCOPE.state = None
        conn.close()


def scoped(root, key, compute):
    """compute() once per revision_scope for (root, key); every call outside a scope computes."""
    state = getattr(_SCOPE, "state", None)
    if state is None or state[0] != _real(root):
        return compute()
    memo = state[2]
    if (key,) not in memo:
        memo[(key,)] = compute()
    return memo[(key,)]


def _connection(root):
    state = getattr(_SCOPE, "state", None)
    if state is not None and state[0] == _real(root):
        return _SharedConnection(state[1])
    return db.connect_readonly(root)


_BATCH = threading.local()


@contextmanager
def revision_batch(root):
    """Read the obs_source table once for every source_revision call made on this thread inside the block.

    Each per-conversation query costs one statement and a connection, and a fold that asks for
    every conversation's revision paid that twice per conversation (indexes on session and
    parent_session help each query but not the count). The
    rows are read once, in source_id order, and each call filters them in Python, which returns what
    the query returned as of the start of the block. The caller keys a cached row on that revision,
    so a row that moves later changes the next revision and recomputes. Real paths are kept for
    the block the same way (_real)."""
    prior, prior_real = getattr(_BATCH, 'obs', None), getattr(_BATCH, 'real', None)
    _BATCH.real = {} if prior_real is None else prior_real
    try:
        conn = _connection(root)
        try:
            rows = [dict(item) for item in conn.execute("SELECT * FROM obs_source ORDER BY source_id")]
        except sqlite3.OperationalError as exc:
            if 'no such table' not in str(exc):
                raise
            rows = []
        finally:
            conn.close()
        by_session, by_parent = {}, {}
        for position, row in enumerate(rows):
            by_session.setdefault(row['session'], []).append(position)
            by_parent.setdefault(row['parent_session'], []).append(position)
        _BATCH.obs = (_real(root), rows, by_session, by_parent)
        yield
    finally:
        _BATCH.obs, _BATCH.real = prior, prior_real


def _obs_sources(conn, root, sid, parent_only=False):
    """obs_source rows naming sid as the parent (parent_only, with a locator) or as either side."""
    batch = getattr(_BATCH, 'obs', None)
    # A batch answers only for the root it read; another root's call queries its own record.
    if batch is not None and batch[0] == _real(root):
        _root, rows, by_session, by_parent = batch
        if parent_only:
            return [rows[at] for at in by_parent.get(sid, ()) if rows[at]['locator'] is not None]
        return [rows[at] for at in sorted(set(by_session.get(sid, ())) | set(by_parent.get(sid, ())))]
    if parent_only:
        return conn.execute("SELECT source_id,session,locator FROM obs_source WHERE parent_session=? AND locator IS NOT NULL ORDER BY source_id", (sid,))
    return [dict(item) for item in conn.execute("SELECT * FROM obs_source WHERE session=? OR parent_session=? ORDER BY source_id", (sid, sid))]


def _record_rows(conn, sid, capture, table):
    """Read payloads only after checking their size against the shared budget."""
    payload = "data" if table == "events" else "body"
    columns = "actor,kind,ref" if table == "events" else "sender,to_,kind,ref"
    exclude = " AND kind NOT IN ('heartbeat','transcript-snapshot','msg')" if table == "events" else ""
    query = ("SELECT id,ts,%s,length(CAST(%s AS BLOB)) AS size FROM %s "
             "WHERE session=?%s ORDER BY id DESC LIMIT ?") % (columns, payload, table, exclude)
    rows = []
    for row in conn.execute(query, (sid, MAX_RECORDS + 1)):
        size = row["size"] or 0
        if size > capture.budget.remaining or capture.budget.records >= MAX_RECORDS:
            capture.stats.update(truncated=True, total_is_exact=False)
            break
        row = dict(row)
        if size > MAX_LINE_BYTES:
            capture.stats.update(truncated=True, total_is_exact=False)
            continue
        value = conn.execute("SELECT %s FROM %s WHERE id=?" % (payload, table), (row["id"],)).fetchone()[0]
        capture.stats["bytes_read"] += size
        capture.budget.remaining -= size
        capture.budget.records += 1
        row[payload] = value
        rows.append(row)
    return reversed(rows)


def _record(conn, sid, capture):
    if conn is None:
        return
    for row in _record_rows(conn, sid, capture, "events"):
        try:
            data = json.loads(row["data"])
        except (ValueError, TypeError):
            continue
        if not isinstance(data, dict):
            continue
        texts = [str(data[k]) for k in ("note", "body", "statement", "reason", "summary", "next_action") if data.get(k)]
        text = "\n".join(dict.fromkeys(texts)) or row["kind"].replace("-", " ")
        capture.message("record", text, {"id": "record", "kind": "record", "event_id": row["id"]},
                        row["ts"], identity="event:" + str(row["id"]), actor=row["actor"],
                        kind=row["kind"], ref=row["ref"])
    for row in _record_rows(conn, sid, capture, "messages"):
        capture.message("message", row["body"] or "", {"id": "messages", "kind": "record-message", "event_id": row["id"]},
                        row["ts"], identity="message:" + str(row["id"]), actor=row["sender"],
                        recipient=row["to_"], kind=row["kind"], ref=row["ref"])


def _children(root, sid, registered):
    directories = []
    if registered:
        path = Path(registered)
        directories.append(path.parent / path.stem / "subagents")
    directories.append(Path(transcripts.subagents_dir(root, sid)))
    found = {}
    scanned = 0
    truncated = False
    for directory in directories:
        if directory.is_symlink() or directory.parent.is_symlink():
            continue
        if directory == directories[-1] and not _inside(directory, root):
            continue
        stack = [directory]
        while stack:
            current = stack.pop()
            if not _inside(current, directory):
                continue
            try:
                with os.scandir(current) as entries:
                    for entry in entries:
                        scanned += 1
                        if scanned > MAX_DIRECTORY_ENTRIES:
                            return found, True
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                        elif entry.name.startswith("agent-") and entry.name.endswith(".jsonl") and entry.is_file(follow_symlinks=False):
                            rel = Path(entry.path).relative_to(directory).as_posix()
                            ident = "child-" + _hash(rel)
                            if ident not in found:
                                if len(found) >= MAX_CHILDREN:
                                    truncated = True
                                    continue
                                found[ident] = Path(entry.path)
            except OSError:
                continue
    conn = _connection(root)
    try:
        registered_children = _obs_sources(conn, root, sid, parent_only=True)
        for row in registered_children:
            child_path = Path(row['locator'])
            if not child_path.is_absolute():
                child_path = Path(root) / child_path
            ident = 'child-' + _hash(row['source_id'])
            matching = []
            for old_id, candidate in found.items():
                native_candidate = candidate
                local_dir = Path(transcripts.subagents_dir(root, sid))
                if registered and _inside(candidate, local_dir):
                    native_candidate = Path(registered).parent / Path(registered).stem / 'subagents' / Path(candidate).relative_to(local_dir)
                if _real(native_candidate) == _real(child_path):
                    matching.append((old_id, candidate))
            retained = Path(transcripts.local_path(root, row['session']))
            available = child_path if child_path.is_file() and not child_path.is_symlink() else retained if _local_file(retained, root) else matching[0][1] if matching else None
            if available is None:
                continue
            for old_id, _candidate in matching:
                del found[old_id]
            if len(found) >= MAX_CHILDREN:
                truncated = True
                break
            found[ident] = available
    except sqlite3.OperationalError as exc:
        if 'no such table' not in str(exc):
            raise
    finally:
        conn.close()
    return dict(sorted(found.items())), truncated


def _subagent_fingerprint(root, sid, primary):
    """Hash of every agent-*.jsonl under the subagent directories, uncapped: names and stats.

    The child list above stops at MAX_CHILDREN and MAX_DIRECTORY_ENTRIES, but parse_session counts
    every such file (its `subagents` figure), so a file past the cap must still move the key."""
    from alpaca.analytics import parse_session
    names = set()
    for directory in (None, transcripts.subagents_dir(root, sid)):
        names.update(parse_session.transcript_files(str(primary), directory)[1:])
    stamps = []
    for name in sorted(names):
        try:
            stat = os.stat(name)
            stamps.append([name, stat.st_size, stat.st_mtime_ns])
        except OSError:
            stamps.append([name, None, None])
    return _hash(json.dumps(stamps))


def _locator_fingerprint(sources):
    """Hash of whether each registered source locator is a readable file, for every source row.

    observability._coverage checks exactly this for every required source of the session, children
    included, so it is not limited to the capped child list."""
    state = [[row['source_id'], row['locator'],
              bool(row['locator']) and os.path.isfile(row['locator']) and os.access(row['locator'], os.R_OK)]
             for row in sources]
    return _hash(json.dumps(state))


def source_revision(root, sid):
    """Metadata revision of every bounded detail source, including record fallback."""
    root = _real(root)
    conn = _connection(root)
    try:
        row = conn.execute("SELECT transcript,started,ended FROM sessions WHERE sid=?", (sid,)).fetchone() if conn else None
        registered = transcripts.registered(conn, sid)
        if registered and not os.path.isabs(registered):
            registered = os.path.join(root, registered)
        local = transcripts.local_path(root, sid)
        primary = registered if registered and os.path.isfile(registered) else local
        children, cut = _children(root, sid, registered)
        candidates = [primary, str(primary) + ".source.json", pool.path(root, sid), *children.values(),
                      *(str(path) + ".source.json" for path in children.values())]
        values = []
        for candidate in candidates:
            if candidate != registered and not _inside(candidate, root):
                # Authorized children of a registered transcript may be external.
                if candidate not in children.values():
                    continue
            try:
                stat = os.stat(candidate)
                stamp = [stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns]
            except OSError:
                stamp = None
            values.append([_hash(str(candidate)), stamp])
        heads = [conn.execute("SELECT COALESCE(MAX(id),0) FROM " + table + " WHERE session=?", (sid,)).fetchone()[0]
                 for table in ("events", "messages")] if conn else [0, 0]
        manifest = conn.execute("SELECT key,value FROM meta WHERE key IN (?,?) ORDER BY key", ('capture-manifest:' + sid, 'operator:' + sid)).fetchall()
        try:
            sources = _obs_sources(conn, root, sid)
        except sqlite3.OperationalError as exc:
            if 'no such table' not in str(exc):
                raise
            sources = []
        return {"sources": values, "record_heads": heads, "children_truncated": cut,
                "subagent_files": _subagent_fingerprint(root, sid, primary),
                "locators": _locator_fingerprint(sources),
                "capability_revision": _hash(json.dumps([dict(item) for item in manifest] + sources, sort_keys=True)),
                "window": [row["started"], row["ended"]] if row else []}
    finally:
        if conn:
            conn.close()


def _coverage(capture, has_transcript, has_record, source, children_truncated=False):
    stats = capture.stats.copy()
    partial = (stats["truncated"] or stats["malformed_lines"] or stats["partial_lines"]
               or stats["unreadable_sources"] or children_truncated)
    if has_transcript:
        state = "partial" if partial else "captured"
        reason = "Visible records from the available transcript."
    elif capture.tools:
        state = "partial"
        reason = "Transcript unavailable; captured tool calls and attributed record entries are shown."
    elif has_record:
        state = "record-only"
        reason = "Transcript unavailable; attributed record entries do not capture the full conversation."
    else:
        state = "unavailable"
        reason = "No readable transcript or attributed conversation capture is available."
    if partial:
        reason += " Some source content is incomplete, malformed, unreadable, or exceeds the read budget."
    stats["total_is_exact"] = bool(has_transcript and not partial)
    return {"state": state, "reason": reason, "source": source, **stats,
            "file_complete": bool(has_transcript and not partial),
            "session_complete": False,
            "child_coverage": {"state": "unknown", "expected": None, "missing": []},
            "children_truncated": children_truncated, "usage": "unavailable",
            "totals_include_children": False}


def _session(capture, sid, row, operator, child=None):
    title = capture.title or next((m["text"].split("\n", 1)[0] for m in capture.messages if m["role"] == "user" and m["text"] and not m["text"].lstrip().startswith(
        ("<recommended_plugins>", "<environment_context>", "# AGENTS.md", "<INSTRUCTIONS>"))), "")
    return {"sid": sid, "title": _redact(str(title))[:180] or (operator or capture.operator or "Operator") + " session",
            "operator": operator or capture.operator or "unknown", "started": row.get("started"),
            "ended": row.get("ended"), "parent_sid": sid if child else None, "child_id": child,
            "turns": sum(m["role"] == "assistant" for m in capture.messages), "tool_calls": len(capture.tools),
            "usage_available": False, "tokens": None, "cost_reported_usd": None}


def _rollup(capture):
    tools, hourly, prompts, stamps = {}, {}, [], []
    for message in capture.messages:
        stamp = message.get("ts")
        if isinstance(stamp, str) and len(stamp) >= 13:
            stamps.append(stamp)
            hour = hourly.setdefault(stamp[:13], {"turns": 0, "tools": 0, "prompts": 0})
        else:
            hour = None
        if message["role"] == "assistant" and hour is not None:
            hour["turns"] += 1
        if message["role"] == "user" and message["text"]:
            prompts.append({"ts": stamp, "text": str(message["text"])[:1600]})
            if hour is not None:
                hour["prompts"] += 1
        for tool in message["tools"]:
            tools[tool["name"]] = tools.get(tool["name"], 0) + 1
            if hour is not None:
                hour["tools"] += 1
    return {"tools": tools, "hourly": hourly, "human_prompts": prompts,
            "first": min(stamps) if stamps else None, "last": max(stamps) if stamps else None}


def detail(root, sid, before=None, limit=80, child=None, *, include_rollup=False, around=None, after=None):
    """Return the latest chronological page; next_before retrieves earlier records.

    IDs are stable across appends. Invalid/expired cursors raise ValueError rather
    than silently restarting pagination. KeyError means an unknown session.
    Individual content is capped with flags; total_is_exact is false when the
    bounded scan cannot establish the complete number of available messages.
    """
    if not isinstance(sid, str) or not _IDENTIFIER.fullmatch(sid):
        raise ValueError("invalid session identifier")
    if child is not None and (not isinstance(child, str) or not re.fullmatch(r"child-[0-9a-f]{24}", child)):
        raise ValueError("invalid child identifier")
    if before is not None and (not isinstance(before, str) or not re.fullmatch(r"[0-9a-f]{24}", before)):
        raise ValueError("invalid conversation cursor")
    if after is not None and (not isinstance(after, str) or not re.fullmatch(r"[0-9a-f]{24}", after)):
        raise ValueError("invalid conversation cursor")
    if sum(value is not None for value in (before, after, around)) > 1:
        raise ValueError("choose one conversation cursor")
    if around is not None:
        try:
            around = int(around)
        except (TypeError, ValueError):
            raise ValueError("invalid source line") from None
        if around < 1:
            raise ValueError("invalid source line")
    try:
        limit = max(1, min(MAX_PAGE, int(limit)))
    except (TypeError, ValueError):
        raise ValueError("invalid page limit") from None
    root = os.path.realpath(root)
    conn = _connection(root)
    try:
        row = conn.execute("SELECT * FROM sessions WHERE sid=?", (sid,)).fetchone() if conn else None
        row = dict(row) if row else {}
        operator_row = conn.execute("SELECT value FROM meta WHERE key=?", ("operator:" + sid,)).fetchone() if conn else None
        operator = operator_row[0] if operator_row else None
        registered = transcripts.registered(conn, sid)
        if registered and not os.path.isabs(registered):
            registered = os.path.join(root, registered)
        local = transcripts.local_path(root, sid)
        pool_path = pool.path(root, sid)
        source = "none"
        chosen = None
        if registered and os.path.isfile(registered):
            chosen, source = registered, "registered-transcript"
        elif _local_file(local, root):
            chosen, source = local, "project-transcript"
        if not row and not chosen and not _local_file(pool_path, root):
            raise KeyError("unknown session")
        children, children_truncated = _children(root, sid, registered)
        if child is not None and child not in children:
            raise ValueError("unknown child identifier")
        budget = _Budget()
        if child:
            chosen = children[child]
            source = "project-transcript" if _inside(chosen, root) else "registered-transcript"
        capture = _Capture(sid + (":" + child if child else ""), source, budget)
        if chosen:
            capture.transcript(chosen)
        has_transcript = bool(chosen and not capture.stats["unreadable_sources"])
        if not child:
            if _local_file(pool_path, root):
                capture.pool(pool_path, sid, matching_only=has_transcript)
                if not chosen:
                    source = "tool-pool"
            if not has_transcript or not capture.messages:
                _record(conn, sid, capture)
                if not chosen and source == "none" and capture.messages:
                    source = "explicit-record"
        child_items = []
        if not child:
            for ident, path in children.items():
                nested = _Capture(sid + ":" + ident, "project-transcript" if _inside(path, root) else "registered-transcript", budget)
                nested.transcript(path)
                info = _session(nested, sid, {}, operator, ident)
                coverage = _coverage(nested, not nested.stats["unreadable_sources"], False, nested.kind)
                child_items.append({"id": ident, "title": info["title"], "parent_sid": sid,
                                    "turns": info["turns"], "tool_calls": info["tool_calls"], "coverage": coverage})
                capture.stats["bytes_read"] += nested.stats["bytes_read"]
                if not nested.stats["total_is_exact"]:
                    children_truncated = True
        # Stable source order wins ties. Records without timestamps retain capture
        # order, rather than sorting undated tool calls ahead of their conversation.
        messages = capture.messages
        if messages and all(m["ts"] for m in messages):
            messages.sort(key=lambda m: m["ts"])
        end = len(messages)
        if before:
            end = next((i for i, message in enumerate(messages) if message["id"] == before), -1)
            if end < 0:
                raise ValueError("conversation cursor is unavailable; reload this session")
        start = max(0, end - limit)
        if around is not None:
            candidates = [(abs(m["source"]["line"] - around), i) for i, m in enumerate(messages)
                          if isinstance(m["source"].get("line"), int)
                          and m["source"].get("kind", "").endswith("transcript")]
            if not candidates:
                raise ValueError("source line is not available in the captured conversation")
            center = min(candidates)[1]
            start = max(0, center - limit // 2)
            end = min(len(messages), start + limit)
        elif after:
            index = next((i for i, message in enumerate(messages) if message["id"] == after), -1)
            if index < 0:
                raise ValueError("conversation cursor is unavailable; reload this session")
            start = index + 1
            end = min(len(messages), start + limit)
        page = messages[start:end]
        return {"session": _session(capture, sid, row if not child else {}, operator, child),
                "messages": page, "children": child_items,
                "coverage": _coverage(capture, has_transcript, bool(messages), source, children_truncated),
                "next_before": page[0]["id"] if start and page else None,
                "next_after": page[-1]["id"] if end < len(messages) and page else None,
                "has_later": end < len(messages),
                "total": len(messages), "has_more": start > 0,
                **({"rollup": _rollup(capture)} if include_rollup else {})}
    finally:
        if conn:
            conn.close()
