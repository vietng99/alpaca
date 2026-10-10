"""The TEST marker parser with its boundary markers, and the metric boundary parser
(FR-023, FR-025).

Design: design/research-engine/architecture.md section 3.3 and
design/research-engine/runbook-format-3.md section 14.4. Only the lines between a line
ALPACA-RESULTS-BEGIN and a line ALPACA-RESULTS-END count; a log without both is refused with
MarkerError, so no verdict and no number is ever read from build output (EC-039). A log
holds exactly one block: a second BEGIN line or an END line with no open block is a conflict.
A test line is `TEST: <name> ... PASS|FAIL|SKIP` and a metric line is
`METRIC: <metric id> <number>`. A name reported twice with different results, a metric id
reported twice with different values, and a number that is not finite are refused as a
conflict.

The log is split on "\n" only (one trailing "\r" per line is dropped, and spaces and tabs
around a line are ignored). Code that runs inside the frozen harness's block can print a
forged line and then an unterminated prefix, so that the harness's own result line no longer
starts with its token and a lenient parser would skip it. So inside the block:

- a line that holds a "\r" or another character Python treats as a line break (vertical tab,
  form feed, \x1c to \x1e, \x85, U+2028, U+2029) is a conflict;
- a line that holds TEST:, METRIC:, EVIDENCE:, ALPACA-RESULTS-BEGIN or ALPACA-RESULTS-END
  anywhere must be exactly that marker line: its token at the start, only once on the line, and
  the whole line matching its pattern. Anything else is a conflict. Lines that hold none of the
  tokens are ignored;
- a line that holds a token and is longer than _MAX_MARKER_LINE characters is a conflict before
  any pattern reads it, and the TEST line is read with string operations rather than a
  backtracking pattern, so tested code that prints one very long line cannot stall the host's
  parse (the parse runs after the jail, outside every timeout).

After the block has closed, a line that holds ALPACA-RESULTS-BEGIN or ALPACA-RESULTS-END in any
form is a conflict too, so a prefix cannot hide the harness's own END behind a forged one. A
report of more than _MAX_RESULTS test names ("too many results"), or a test name longer than
_MAX_NAME characters, is a conflict, which keeps every recorded event bounded.

The gate chain also binds each block to the one run that printed it. For every run it makes a
fresh random token and hands it to the command on file descriptor 3 (ALPACA_RESULTS_FD names
it), and before it parses a log it passes the text through _with_token. Only the line
`ALPACA-RESULTS-END <token>` closes the block, and inside the block every line that holds
TEST:, METRIC: or EVIDENCE: must end with the token as its last word, after a space or a tab:
`TEST: <name> ... PASS <token>`, `METRIC: <id> <number> <token>` and
`EVIDENCE: <path> <sha256> <token>`. _with_token checks the token and strips it, so
parse_tests and parse_metric read the bare lines. A line whose first word is
ALPACA-RESULTS-END without that token (a bare END, or one with another token), and a marker
line inside the block without it, are a conflict. So a runner must read fd 3 and close it
before it runs any code it tests (a shell runner: `read -r T <&3; exec 3<&-`, then
`echo "TEST: t ... PASS $T"` and `echo "ALPACA-RESULTS-END $T"`). Tested code must run in a
child process of the runner, never in the runner's own process: code that shares the
runner's process can read the token from its memory, and then nothing in the log tells its
lines from the runner's. A child cannot read its parent's memory while the host keeps Yama's
ptrace_scope at 1 or higher (a process may then trace only its own descendants), and it never
holds fd 3. So a child that shares the runner's stdout or stderr can print lines into the
block, but none of them counts: a TEST, METRIC or EVIDENCE line it prints has no token and is
a conflict, and it cannot close the block. The BEGIN line carries no token; a BEGIN a child
prints is a second BEGIN or opens a block the child cannot close, so it too ends as a conflict
or a missing block, never as a counted line.
parse_tests and parse_metric keep the design's signatures and read bare lines; the token rule
lives in _with_token, which the gate chain always calls first.

A json-field metric whose file lies in the evidence folder is read from the judged copy, which
the host takes after the jail exits. Tested code that the runner starts can leave a process
behind that rewrites the file after the runner wrote it and before the jail ends, so the gate
chain reads such a value only when the runner's own token-closed block attests the file's bytes
with the line `EVIDENCE: <path relative to the evidence folder> <sha256> <token>` (64
lowercase hex digits). The runner computes the hash from the bytes it wrote, never by reading
the file back.
_parse_evidence returns the attested hash; the gate chain refuses a judged file whose sha256
differs. The attestation is only as good as the runner: tested code that shares the runner's
process can forge it, as it can forge a METRIC line.

Each MarkerError carries two arguments: the reason word ("markers-missing" or
"markers-conflict") and the detail. The gate chain records it as GATE-MARKERS with that word.
"""
from __future__ import annotations

import math
import posixpath
import re

from alpaca.research.errors import MarkerError
from alpaca.research.types import TestReport

_BEGIN = "ALPACA-RESULTS-BEGIN"
_END = "ALPACA-RESULTS-END"
_MISSING = "markers-missing"
_CONFLICT = "markers-conflict"

_METRIC_LINE = re.compile(r"^METRIC:[ \t]+(?P<id>\S+)[ \t]+(?P<value>\S+)$")
_EVIDENCE_LINE = re.compile(r"^EVIDENCE:[ \t]+(?P<path>\S+)[ \t]+(?P<sha>[0-9a-f]{64})$")
_TOKENS = ("TEST:", "METRIC:", "EVIDENCE:", _BEGIN, _END)
# the marker lines inside a block that must end with the run's token (_with_token)
_SIGNED = ("TEST:", "METRIC:", "EVIDENCE:")
# every character str.splitlines() breaks on, apart from "\n" itself
_BREAKS = frozenset("\r\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029")
_BREAK_RE = re.compile("[%s]" % re.escape("".join(sorted(_BREAKS))))
_MAX_RESULTS = 10000
_MAX_NAME = 4096
# the longest line inside the block that may hold a marker token: a TEST line with a name of
# _MAX_NAME characters, an EVIDENCE line with a path of _MAX_NAME characters and its hash, or a
# METRIC line, each with room to spare for the spaces between the words. A longer line that
# holds a token is a conflict before any pattern reads it, so the host's parse of one line
# stays short however long the line the tested code printed.
_MAX_MARKER_LINE = _MAX_NAME + 256
_RESULTS = ("PASS", "FAIL", "SKIP")


def _test_line(line: str):
    r"""(name, result) of `TEST:[ \t]+<name>[ \t]+...[ \t]+PASS|FAIL|SKIP`, or None. The name
    starts with a character that is not white space, and it ends where the run of spaces and
    tabs before the last `...` starts. The line is read from its end with plain string
    operations, never a backtracking pattern, so the time is linear in the line's length."""
    if not line.startswith("TEST:"):
        return None
    rest = line[len("TEST:"):]
    if not rest[:1] or rest[0] not in " \t":
        return None
    rest = rest.lstrip(" \t")
    result = rest[-4:]
    if result not in _RESULTS:
        return None
    head = rest[:-4]
    before = head.rstrip(" \t")
    if len(before) == len(head) or not before.endswith("..."):
        return None
    head = before[:-3]
    name = head.rstrip(" \t")
    if len(name) == len(head) or not name or name[0].isspace():
        return None
    return name, result


_PATTERNS = {"TEST:": _test_line, "METRIC:": _METRIC_LINE.match,
             "EVIDENCE:": _EVIDENCE_LINE.match}


def _lines(text: str) -> list:
    """The log's lines, split on "\n" only; one trailing "\r" is dropped and spaces and tabs
    around the line are stripped. No other character ends a line."""
    out = []
    for line in (text or "").split("\n"):
        if line.endswith("\r"):
            line = line[:-1]
        out.append(line.strip(" \t"))
    return out


def _shown(line: str) -> str:
    return repr(line[:80])


def _with_token(text: str, token: str) -> str:
    """text with the run's token checked and stripped, ready for parse_tests or parse_metric:
    the line `ALPACA-RESULTS-END <token>` becomes a bare ALPACA-RESULTS-END, and inside a block
    (after a line that is ALPACA-RESULTS-BEGIN, until that END line) every line that holds
    TEST:, METRIC: or EVIDENCE: must end with a space or a tab and the token, which is removed
    with the spaces and tabs before it. Raises MarkerError (markers-conflict) for any other line
    whose first word is ALPACA-RESULTS-END (a bare END, an END with another token, or an END
    followed by more words) and for a marker line inside the block that does not end with the
    token. A line longer than _MAX_MARKER_LINE is left as it is, so _blocks refuses it for its
    length. The log is split on "\\n" only, one trailing "\\r" is dropped and spaces and tabs
    around a line are ignored, as _lines does; the END line's words are split on any white
    space. Raises ValueError for an empty token or one that holds white space."""
    if not token or any(ch.isspace() for ch in token):
        raise ValueError("a results token is one word, not %r" % (token,))
    out = []
    inside = False
    for number, raw in enumerate((text or "").split("\n"), 1):
        line = raw[:-1] if raw.endswith("\r") else raw
        parts = line.split()
        if parts and parts[0] == _END:
            if parts == [_END, token]:
                out.append(_END)
                inside = False
                continue
            raise MarkerError(_CONFLICT, "log line %d is an %s line without this run's token: %s"
                              % (number, _END, _shown(line.strip(" \t"))))
        bare = line.strip(" \t")
        if bare == _BEGIN:
            inside = True
        elif (inside and len(bare) <= _MAX_MARKER_LINE
              and any(tok in bare for tok in _SIGNED)):
            head = bare[:-len(token)]
            if not bare.endswith(token) or not head or head[-1] not in " \t":
                raise MarkerError(_CONFLICT, "log line %d is a marker line without this run's "
                                             "token: %s" % (number, _shown(bare)))
            out.append(head.rstrip(" \t"))
            continue
        out.append(raw)
    return "\n".join(out)


def _check_inside(line: str, number: int) -> None:
    """Refuse (markers-conflict) a line inside the block that could hide or bend a marker line:
    one with a line-break character, one that holds a token and is longer than
    _MAX_MARKER_LINE, or one that holds a token without being exactly its marker line."""
    if _BREAK_RE.search(line):
        raise MarkerError(_CONFLICT, "log line %d holds a line-break character other than a "
                                     "newline: %s" % (number, _shown(line)))
    held = [tok for tok in _TOKENS if tok in line]
    if not held:
        return
    if len(line) > _MAX_MARKER_LINE:
        raise MarkerError(_CONFLICT, "log line %d holds a marker token and is %d characters "
                                     "long, over the limit of %d: %s"
                          % (number, len(line), _MAX_MARKER_LINE, _shown(line)))
    if len(held) > 1 or line.count(held[0]) != 1:
        raise MarkerError(_CONFLICT, "log line %d holds more than one marker token: %s"
                          % (number, _shown(line)))
    token = held[0]
    pattern = _PATTERNS.get(token)
    if pattern is None or not line.startswith(token) or not pattern(line):
        raise MarkerError(_CONFLICT, "log line %d holds %s but is not a %s line: %s"
                          % (number, token, token.rstrip(":"), _shown(line)))


def _blocks(text: str) -> list:
    """The lines inside the one BEGIN..END block. Raises MarkerError (markers-missing)
    when the log has no BEGIN line, or a BEGIN line with no END line after it, and
    (markers-conflict) for a second BEGIN line (nested or after the block closed), an END line
    with no open block, a line inside the block that fails _check_inside, or a line after the
    block that holds a BEGIN or END token in any form. So code that runs inside the frozen
    harness cannot print its own END to cut off the harness's real lines, its own block beside
    the real one, or a prefix that hides the harness's own line."""
    lines = _lines(text)
    if _BEGIN not in lines:
        raise MarkerError(_MISSING, "no %s line" % _BEGIN)
    out = []
    inside = False
    closed = 0
    for number, line in enumerate(lines, 1):
        if line == _BEGIN:
            if inside or closed:
                raise MarkerError(_CONFLICT, "a second %s line (log line %d)" % (_BEGIN, number))
            inside = True
            continue
        if line == _END:
            if not inside:
                raise MarkerError(_CONFLICT, "an %s line with no open block (log line %d)"
                                  % (_END, number))
            inside = False
            closed += 1
            continue
        if inside:
            _check_inside(line, number)
            out.append(line)
        elif closed and (_BEGIN in line or _END in line):
            raise MarkerError(_CONFLICT, "log line %d after the block holds a marker token: %s"
                              % (number, _shown(line)))
    if inside or closed == 0:
        raise MarkerError(_MISSING, "no %s line after %s" % (_END, _BEGIN))
    return out


def parse_tests(text: str) -> TestReport:
    """TEST: <name> ... PASS|FAIL|SKIP between the begin and end markers. Raises MarkerError for a
    log without either marker, a name reported twice with different results (FR-025), a line
    _blocks refuses, or more than _MAX_RESULTS names or a name longer than _MAX_NAME."""
    tests: dict = {}
    for line in _blocks(text):
        found = _test_line(line)
        if not found:
            continue
        name, result = found
        if len(name) > _MAX_NAME:
            raise MarkerError(_CONFLICT, "a test name of %d characters is longer than the limit "
                                         "of %d" % (len(name), _MAX_NAME))
        if name in tests and tests[name] != result:
            raise MarkerError(_CONFLICT, "test %r reported as %s and as %s"
                              % (name, tests[name], result))
        tests[name] = result
        if len(tests) > _MAX_RESULTS:
            raise MarkerError(_CONFLICT, "too many results: more than %d test names"
                              % _MAX_RESULTS)
    return TestReport(tests=tests, begin=True, end=True, conflicts=())


def parse_metric(text: str, metric_id: str) -> float:
    """The number of `METRIC: <metric_id> <number>` between the markers. Raises MarkerError
    (markers-missing) for a log without the markers or without a line for metric_id, and
    (markers-conflict) for a number that is not finite or an id reported twice with different
    values."""
    value = None
    for line in _blocks(text):
        found = _METRIC_LINE.match(line)
        if not found or found.group("id") != metric_id:
            continue
        try:
            number = float(found.group("value"))
        except ValueError:
            raise MarkerError(_CONFLICT, "metric %s value %r is not a number"
                              % (metric_id, found.group("value")[:40])) from None
        if not math.isfinite(number):
            raise MarkerError(_CONFLICT, "metric %s value %r is not finite"
                              % (metric_id, found.group("value")[:40]))
        if value is not None and number != value:
            raise MarkerError(_CONFLICT, "metric %s reported as %r and as %r"
                              % (metric_id, value, number))
        value = number
    if value is None:
        raise MarkerError(_MISSING, "no METRIC line for %s between the markers" % metric_id)
    return value


def _parse_evidence(text: str, path: str) -> str:
    """The sha256 that `EVIDENCE: <path> <sha256>` between the markers attests for path, a path
    relative to the evidence folder (both sides are compared after posixpath.normpath, so
    "./w.json" names w.json). Lines for other paths are ignored. Raises MarkerError
    (markers-missing) for a log without the markers or without a line for path, and
    (markers-conflict) for a path attested twice with different hashes or a line _blocks
    refuses."""
    want = posixpath.normpath(path)
    got = None
    for line in _blocks(text):
        found = _EVIDENCE_LINE.match(line)
        if not found or posixpath.normpath(found.group("path")) != want:
            continue
        sha = found.group("sha")
        if got is not None and sha != got:
            raise MarkerError(_CONFLICT, "evidence %s attested as %s and as %s"
                              % (_shown(want), got, sha))
        got = sha
    if got is None:
        raise MarkerError(_MISSING, "no EVIDENCE line for %s between the markers" % _shown(want))
    return got
