"""A frozen copy of the secret screen as it was at bfb66b189 (op-008 t-070 round 2, class C-C).

`T/wiki/test_screen_door.py::test_the_screen_changes_only_where_a_declared_variant_applies` compares
the live screen (`alpaca.analytics.detail.screen`) with this copy, so a pattern rewritten for linear
time must give the bytes the stored store and the accepted evaluation digest were built with, and the
t-425 class (`plain-variant`) may change a text only where one of its declared forms applies.

Everything below the imports is lines 28 to 288 of `alpaca/analytics/detail.py` at bfb66b189,
byte for byte (sha256 of those lines: 55a17ca871671a44ddaa8c2f6dcc3e8604c2b632838568b19883715bcd8d8d5e;
`git show bfb66b189:alpaca/analytics/detail.py | sed -n 28,288p | sha256sum`). Do not edit it.
"""
import hashlib
import os
import re

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,159}\Z")
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
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]*")
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
# once gave).
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
                r"[\"']?" + _FOR + r"[ \t]*(?:\b" + _VERB + r"|[=:])")
_PLAIN_SECRET = re.compile(
    r"(?i)(?P<head>\b(?:" + _PLAIN_NAMES + r")[\"']?" + _FOR +
    r"[ \t]*(?:(?P<verb>\b" + _VERB + r"(?:[ \t]+(?:now|still|set[ \t]+to|changed[ \t]+to|reset[ \t]+to))?"
    r"[ \t]*:?)|[=:])[ \t]*(?:\n[ \t]*)?)"
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


def _hash(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]


def _inside(path, directory):
    try:
        return os.path.commonpath([os.path.realpath(path), os.path.realpath(directory)]) == os.path.realpath(directory)
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


def _redact_once(value):
    """One pass of every secret class over a string. Each class reads the string as given and the
    masks are the union of what they found, so no class's mask hides another class's secret, in
    whatever order the classes are listed: a passphrase's words do not take the first part of a JWT
    or a webhook and leave the rest unmatched, and a value glued to the next credential name keeps
    that name for its own class (t-070 RC2 glued pairs). A mask that makes another match possible
    is found by the next pass (`settle`)."""
    spans = []
    if "-----BEGIN " in value:
        spans.extend(_pem_spans(value))
    if _PLAIN_HINT.search(value):
        spans.extend(span for span in map(_plain_span, _PLAIN_SECRET.finditer(value)) if span)
    if _SECRET_TEXT_HINT.search(value):
        spans.extend((m.start() + len(m.group(1) or m.group(2)), m.end())
                     for m in _SECRET_TEXT.finditer(value))
    for hints, pattern in ((_PROVIDER_HINTS, _PROVIDER_KEY), (_AWS_HINTS, _AWS_KEY),
                           (_SLACK_HINTS, _SLACK_TOKEN), (("eyJ",), _JWT)):
        if any(hint in value for hint in hints):
            spans.extend(m.span() for m in pattern.finditer(value))
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
    finds nothing more; t-070 A-3). Every pattern is linear in the text. The wiki's write door calls
    it through `alpaca.wiki.ingest.drain.screen`, the redactor below for each string."""
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

