"""The owner-confirmed sandbox of an op, read for the write-time guard (op-059 t-695).

An op's batch has a `sandbox` item (alpaca/batch.py). When the owner answers it, the answer says
where the work may write: "Writes stay in this project root and /home/x/studio plus the session
scratchpad; no system installs; no sudo". Until t-695 the answer was checked only at the op's
final review. The PreToolUse hook `alpaca.hooks.sandbox_guard` refuses a Write, Edit or Bash target
outside the allowed paths the moment the call is made. The harness adds places of its own that every
sandbox allows: the project root, `.alpaca/`, the session's own scratchpad, `~/.claude/`,
`$CLAUDE_CONFIG_DIR`, the device files and the session temp root `/tmp/sbx<sid8>` (made mode 0700 with a
marker file, never inside a project tree), where TMPDIR and pytest's temp root point.

DESIGN NOTE, round 4 (the class change). Rounds 1 to 3 enforced what a parser read from the owner's free
text; three adversarial rounds each found a wording that made a forbidden place writable, and natural
language parsing does not converge. So the parser left the trust path. What the guard enforces comes only
from an explicit path list the owner confirmed, read from the record, never from a file the agent wrote:

  * `sandbox-set` events, written by `alpaca sandbox set <op> --path P [--path P ...] --by owner --quote
    '<owner words>'`; the newest proved set replaces the list. An entry is an absolute or `~/` path, a
    glob (`/tmp/op61-*`), or one of two words: `root` (this project root) and `scratchpad` (this
    project's scratchpad root, the folder that holds the scratchpad of every session of the project, so a
    resumed or forked session that was told to use another session's scratchpad may; it includes the
    older session folders under /tmp/claude-<uid>, which is RAM);
  * `sandbox-widen` events after that set, written by `alpaca sandbox widen`, the same proof. A widen
    only adds, and needs a confirmed list to add to.

ROUND 5 (check-adversarial-r4.md). The owner confirms the LIST, never its parts: the quote is an owner
message (ownerword.human_write, form "quote": typed by the owner in the calling session), typed after the
op's sandbox question was first asked (the batch's owner-ask, or `alpaca sandbox ask`), that holds at the
start of one of its lines the exact command line `alpaca sandbox set <op> --path P ...` (or `widen`)
naming exactly the entries of the call, order aside: the line the readback and `sandbox ask` print, which
the owner pastes. One owner message confirms once (a narrowing set is not undone by replaying the wider
message). Refused whatever the quote: `/`, `/home`, `/tmp`, `/var`, `/etc`, `/usr`, `/var/tmp`, any entry
with fewer than two folders, a folder that is only a wildcard, and anything that covers the home folder or
the folder holding the project root (`broad_reason`). Each entry is resolved once, at set time: a path that
is or passes through a symbolic link is refused, and the stored form is never resolved again, so a link
made later at a confirmed path moves nothing (`entries`). A row is honored only with its proof (`proved`):
its stored entries, the command line in its quote, and the transcript line at the stored offset (when the
transcript still exists). The session transcripts are guarded by name against tool calls while a list is enforced (a hard link made first is a stated limit).

ROUND 6 (check-adversarial-r5.md notes 1 to 3). Only the NEWEST owner message of the session that holds an
`alpaca sandbox set|widen <op>` line confirms, and never one typed before the message a recorded set or widen
of the op rests on, so an older, wider line cannot be replayed once a newer one exists (SANDBOX-NOT-NEWEST).
The owner confirms only by pasting the line in the agent session: ownerword's terminal path (no agent marker,
a TTY, the subject typed) leaves no file to re-read and an agent with a shell can fake a terminal
(design/research-engine/operator-surface.md, "Honest limit"), so `set` and `widen` refuse it
(SANDBOX-NOT-SESSION) and a row that claims it is not honored. A Codex session, whose owner words exist only
at the terminal, therefore cannot confirm a list. A glob is too broad when its static prefix is a protected
folder or names fewer than two folders, when a wildcard stands for a protected folder, or when it sits directly
in one after fewer than MIN_STEM characters (`broad_reason`).

An op with no confirmed list has no sandbox: nothing is enforced for it, which is what the guard did before
t-695. The batch answer still matters, as the PROPOSAL: `parse_answer` reads it (deterministic text
reading, no model), `proposal()` turns the reading into a path list, and `readback()` prints that list,
the entries left out as too broad, and the exact line the owner pastes under the `sandbox` item of the
batch, after the answer is recorded and in `alpaca sandbox show`. Its output is never enforced by itself: a
wrong reading grants nothing until the owner reads it in the line and pastes it.

The parser proves permission instead of hunting for prohibition (round 3). It masks the places
(absolute and `~/` paths, relative paths that name a folder of the project, the words `project root` /
`the repo` / `scratchpad`), cuts the answer into sentences and units (at `;`, a full stop, a comma, a
colon, a parenthesis, and, or, plus, and before but / except / without / other than / with the exception
of ...), and proposes a place only when its unit is made of words from a closed vocabulary (FILLER and
CUE below) and matches a positive pattern ("writes stay in", "write only inside", "rw", "go to", "is
allowed", "put"). Any other word, a negation, a read word, an exclusion or a question proposes nothing
and is listed with its reason (`refused`). It reads English only.

`for_session(conn, sid, root)` says which sandbox binds a session: the ops of the tasks it holds,
else the ops whose batch it last asked the owner about; the union of the confirmed lists of those ops
that have one. An op in that set with no confirmed list adds nothing and switches nothing off (round 5);
when none has a list the answer is None.

Nothing here raises into a hook: the guard wraps every call and an error means "no decision".
The record is only read here (`connect_readonly`); `set` and `widen` are the verbs that write it.
"""
import fnmatch
import json
import os
import re
import shlex
import stat
import time

WIDEN_KIND = "sandbox-widen"
#: The owner-confirmed path list of an op (round 4): the only thing the guard enforces.
SET_KIND = "sandbox-set"
#: The two words a confirmed list may hold besides paths.
ROOT_WORD, SCRATCH_WORD = "root", "scratchpad"
ITEM = "sandbox"
CACHE_S = 20.0
#: The oldest cache entry the hook still uses when the record is locked or unreadable.
STALE_S = 10 * CACHE_S
#: How long the hook waits for the record when another process holds a lock on it. The call must not
#: stall: past this the last cached answer is used (even a stale one), else there is no decision.
LOOKUP_TIMEOUT_S = 0.5
RUNTIME = ".alpaca"
DIR = "sandbox"
#: Where a session's temp root lives. A module constant, not an environment knob: an agent's shell
#: cannot move it. Tests point it at a throwaway folder.
TEMP_BASE = "/tmp"
TEMP_PREFIX = "sbx"
MARKER = ".alpaca-sandbox"
#: Longest temp root that keeps every unix socket the suite and Chrome make under the kernel's 107
#: bytes (investigate-r1.md Item 2): "/tmp/sbx" plus the 8 characters of the session id.
TEMP_MAX = 16


class SandboxError(ValueError):
    """A refused sandbox verb. `reason` is the reason token."""

    def __init__(self, reason, detail):
        super().__init__("%s: %s" % (reason, detail))
        self.reason, self.detail = reason, detail


# ------------------------------------------------------------------ the parser
# Round 3 (investigate-r2.md section 2): the burden of proof is inverted. A place is proposed only when
# the unit of the answer it sits in is proved positive from a CLOSED vocabulary; any unknown word, any
# negation, any read word and any exclusion gives nothing. Round 4: the reading is a PROPOSAL only (see the
# design note above); a misread costs a proposal the owner corrects before confirming it, never a breach.
# Adding a word to a set below is a reviewed change with a row in the table of alpaca/tests/test_sandbox.py.
#: Words that carry no sense of their own in a unit that names a place.
FILLER = frozenset(
    "the this that these those a an my our your its their both either only just solely exclusively "
    "strictly in into inside within under below beneath at to of for on folder folders directory "
    "directories dir dirs path paths tree trees subtree subtrees subfolder subfolders worktree "
    "worktrees repo repos repository root roots project projects workspace area location locations "
    "place places checkout session sessions scratchpad scratch temp tmp temporary file files output "
    "outputs artifact artifacts log logs work task tasks agent agents per-agent per-task per-session "
    "pytest basetemp basetemps own new local home and or plus".split())
#: Words that may stand in a unit that grants (with FILLER); a unit with one more word is refused.
CUE = frozenset(
    "write writes writing written rw stay stays go goes land lands keep put use allowed permitted fine "
    "ok okay writable may can should must will shall is are be you we it all everything".split())
#: A placeless unit made only of these (and FILLER) closes a list ("not elsewhere"); it takes nothing.
CLOSURE = frozenset(
    "not never no nothing nowhere elsewhere else anywhere other outside beyond these them those it that "
    "writes write places place".split())
#: Names for the readback only. Safety does not depend on this list.
NEG = frozenset(
    "no not never nothing none nowhere neither nor without except excluding exclude exception outside "
    "avoid forbid forbids forbidden prohibited banned disallowed refrain off bounds clear alone "
    "untouched read reads reading read-only readonly".split())
READ_WORDS = frozenset("read reads reading read-only readonly".split())
#: Placeless units that start here are an exclusion: split off, never a grant.
HEADS = sorted([("but",), ("except",), ("excluding",), ("besides",), ("without",), ("unless",), ("nor",),
                ("other", "than"), ("apart", "from"), ("aside", "from"), ("save", "for"),
                ("with", "the", "exception", "of")], key=len, reverse=True)
_BREADTH = re.compile(r"\b(?:anywhere|anything|everywhere|whatever|wherever|unrestricted|unlimited)\b|"
                      r"\bany\s+(?:folder|path|directory|place|where)\b")
_BREADTH_PHRASE = re.compile(r"\bas\s+needed\b|\bno\s+(?:limits?|restrictions?)\b")
_POS = [re.compile(p) for p in (
    r"\b(?:write|writes|writing|written|rw)\b",
    r"\b(?:stay|stays|go|goes|land|lands)\s+(?:only\s+|just\s+)?(?:in|inside|into|within|under|to|at)\b",
    r"\bkeep\s+(?:all\s+)?(?:writes|work|files|output|everything)\b",
    r"\bput\b",
    r"\b(?:is|are)\s+(?:allowed|permitted|fine|ok|okay|writable)\b",
    r"^(?:allowed|permitted|writable)\b")]
_RW = re.compile(r"\bread\s*(?:and|&|/|-)\s*write\b", re.I)
_ABS = re.compile(r"(?<![\w.:/~-])(/[\w.@%+=:,~*-]+(?:/[\w.@%+=:,~*-]+)*/?)")
_HOME = re.compile(r"(?<![\w.:/~-])(~(?:/[\w.@%+=:,~*-]+)+/?)")
_REL = re.compile(r"(?<![\w.:/~-])([\w.@%+=:,~*-]+(?:/[\w.@%+=:,~*-]+)+/?)")
_ROOT_WORDS = re.compile(r"\b(?:project\s+root|this\s+repo(?:sitory)?|the\s+repo(?:sitory)?)\b", re.I)
_SCRATCH_WORD = re.compile(r"\bscratchpad\b", re.I)
_SENTENCE = re.compile(r"([;\n]|[.!?](?=\s|$))")
_TOKEN = re.compile("\x01\\d+\x01|[,:()]|[^\\s,:()\x01]+")
_SEPARATORS = frozenset([",", ":", "(", ")", "-", "--", "\u2013", "\u2014"])
_QUOTES = "'\"`\u2018\u2019\u201c\u201d"


def _trim(path):
    return path.rstrip(".,:;=")


def _is_neg(word):
    return word in NEG or word.endswith(("n't", "n\u2019t"))


def _mask(text, root):
    """(text with every place a one-token marker, [(kind, value, shown)]). Kinds: P path, G glob,
    R the project root, S the scratchpad, W the cue `read and write`. A relative path counts only when
    its first folder starts with a dot or exists below `root`: github.com/org/repo is a word."""
    places = []

    def put(kind, value, shown, tail=""):
        places.append((kind, value, shown))
        return " \x01%d\x01 %s" % (len(places) - 1, tail)

    def path_of(m, expand=None):
        raw = m.group(1)
        core = _trim(raw)
        tail = raw[len(core):]
        path = core.rstrip("/")
        if not path or path == "~":
            return None
        return core, tail, path

    def sub_abs(m):
        got = path_of(m)
        if got is None:
            return m.group(0)
        core, tail, path = got
        glob = "*" in path or "?" in path
        return put("G" if glob else "P", path if glob else os.path.realpath(path), core, tail)

    def sub_home(m):
        got = path_of(m)
        if got is None:
            return m.group(0)
        core, tail, path = got
        path = os.path.expanduser(path)
        glob = "*" in path or "?" in path
        return put("G" if glob else "P", path if glob else os.path.realpath(path), core, tail)

    def sub_rel(m):
        got = path_of(m)
        if got is None:
            return m.group(0)
        core, tail, path = got
        head = path.split("/", 1)[0]
        if not (head.startswith(".") or os.path.isdir(os.path.join(root, head))):
            return m.group(0)
        full = os.path.join(root, path)
        glob = "*" in path or "?" in path
        return put("G" if glob else "P", full if glob else os.path.realpath(full), core, tail)

    text = _RW.sub(lambda m: put("W", None, m.group(0)), text)
    text = _ABS.sub(sub_abs, text)
    text = _HOME.sub(sub_home, text)
    text = _REL.sub(sub_rel, text)
    text = _ROOT_WORDS.sub(lambda m: put("R", root, m.group(0)), text)
    text = _SCRATCH_WORD.sub(lambda m: put("S", None, m.group(0)), text)
    return text, places


class _Tok:
    __slots__ = ("kind", "low", "shown", "value")

    def __init__(self, kind, low, shown, value=None):
        self.kind, self.low, self.shown, self.value = kind, low, shown, value


def _tokens(sentence, places):
    out = []
    for m in _TOKEN.finditer(sentence):
        t = m.group(0)
        if t[0] == "\x01":
            kind, value, shown = places[int(t[1:-1])]
            out.append(_Tok(kind, "rw" if kind == "W" else "", shown, value))
        elif t in _SEPARATORS:
            out.append(_Tok("sep", t, t))
        else:
            word = t.strip(_QUOTES)
            if word:
                out.append(_Tok("w", word.lower(), word))
    return out


def _head_at(toks, i):
    for head in HEADS:
        seg = toks[i:i + len(head)]
        if len(seg) == len(head) and all(t.kind == "w" and t.low == h for t, h in zip(seg, head)):
            return head
    return None


def _split_units(toks):
    """The tokens of one sentence cut into units: at , : ( ) and a dash, at the list words and / or /
    plus / as well as (dropped), and before an exclusion head (kept at the head of its unit). Never
    before `not` or `never`: "R, /tmp is not allowed" must keep "/tmp is" with its "not"."""
    units, cur, i = [], [], 0
    while i < len(toks):
        t = toks[i]
        if t.kind == "sep":
            units.append(cur)
            cur = []
        elif t.kind == "w" and t.low in ("and", "or", "plus"):
            units.append(cur)
            cur = []
        elif t.kind == "w" and t.low == "as" and [x.low for x in toks[i:i + 3]] == ["as", "well", "as"]:
            units.append(cur)
            cur = []
            i += 2
        else:
            if t.kind == "w" and _head_at(toks, i):
                units.append(cur)
                cur = []
            cur.append(t)
        i += 1
    units.append(cur)
    return [u for u in units if u]


class _Unit:
    def __init__(self, toks):
        self.toks = toks
        self.places = [t for t in toks if t.kind in ("P", "G", "R", "S")]
        self.words = [t.low for t in toks if t.kind in ("w", "W")]
        self.shown = " ".join(t.shown for t in toks)
        self.cls = None
        self.granted = False
        self.reason = None
        self.qualified_by = None
        self.neg = False
        text = " ".join(self.words)
        self.text = text
        self.breadth = bool(_BREADTH.search(text) or _BREADTH_PHRASE.search(text))
        self.neg = any(_is_neg(w) for w in _BREADTH_PHRASE.sub(" ", text).split())
        self._classify()

    def _classify(self):
        words = self.words
        if not self.places:
            if all(w in FILLER for w in words):
                self.cls = "FILL"
            elif self.breadth and not self.neg:
                self.cls = "BREADTH"
            elif all(w in FILLER or w in CLOSURE for w in words):
                self.cls = "CLOSE"
            elif all(w in FILLER or w in CUE for w in words) and any(p.search(self.text) for p in _POS):
                self.cls = "POS"
            else:
                self.cls = "OTHER"
            return
        text = _BREADTH.sub(" ", self.text)               # next to a place, "anywhere" is filler
        ws = text.split()
        ok = all(w in FILLER or w in CUE for w in ws)
        if ok and any(p.search(text) for p in _POS):
            self.cls = "POS"
        elif ok and "use" in ws and all(p.kind in ("R", "S") for p in self.places):
            self.cls = "POSH"
        elif all(w in FILLER for w in ws):
            self.cls = "BARE"
        else:
            self.cls = "REFUSED"

    def starts_with_place(self):
        for t in self.toks:
            if t.kind in ("P", "G", "R", "S"):
                return True
            if t.kind == "w" and t.low in FILLER:
                continue
            return False
        return False

    def starts_with_exclusion(self):
        first = self.toks[0]
        return first.kind == "w" and (_head_at(self.toks, 0) is not None or _is_neg(first.low))

    def why(self):
        words = self.words
        if any(w in READ_WORDS for w in words):
            return "read-only"
        if any(_is_neg(w) for w in words):
            return "negation"
        for w in words:
            if w not in FILLER and w not in CUE and not _BREADTH.search(w):
                return 'unknown word "%s"' % w
        return "names no place" if not self.places else "no permission word"


def _sentences(masked):
    parts = _SENTENCE.split(masked)
    out = []
    for i in range(0, len(parts), 2):
        term = parts[i + 1] if i + 1 < len(parts) else ""
        if parts[i].strip():
            out.append((parts[i], term))
    return out


def parse_answer(text, root, sid=None):
    """What a sandbox answer allows: {"roots": [dir], "globs": [pattern], "scratch": bool,
    "excluded": [dir the owner refused inside a writable one], "refused": [{"clause", "reason"}],
    "not_enforceable": [refused path inside the project root], "broad": bool, "enforced": bool}.
    `root` is the project root (the words `project root` mean it). A place is granted only by a unit
    proved positive (see the vocabulary above); everything else is refused with its reason."""
    root = os.path.realpath(root)
    masked, places = _mask(str(text or ""), root)
    grants, refused, refused_places, broad = [], [], [], False
    for si, (body, term) in enumerate(_sentences(masked)):
        units = [_Unit(u) for u in _split_units(_tokens(body, places))]
        if term == "?":
            refused += [{"clause": u.shown, "reason": "a question, not permission"}
                        for u in units if u.cls != "FILL"]
            continue
        leading = si == 0 and all(u.cls in ("BARE", "FILL") for u in units) and any(
            t.kind in ("R", "S") or (t.kind == "w" and t.low in ("only", "just"))
            for u in units for t in u.toks)
        for i, u in enumerate(units):
            if u.cls in ("POS", "POSH"):
                u.granted = True
            elif u.cls == "BARE":
                before = next((g for g in reversed(units[:i]) if g.cls not in ("BARE", "FILL")), None)
                after = next((g for g in units[i + 1:] if g.cls not in ("BARE", "FILL")), None)
                govs = [g for g in (before, after if after is not None and after.starts_with_place()
                                    else None) if g is not None]
                if govs:
                    u.granted = all(g.cls == "POS" for g in govs)
                    u.reason = "continues a refused unit"
                else:
                    u.granted = leading
                    u.reason = "no permission word names it"
        for k, u in enumerate(units):                    # a trailing qualifier drops the grants before it
            if u.cls == "OTHER" and not u.places:
                j = k - 1
                while j >= 0 and units[j].places and units[j].cls in ("POS", "POSH", "BARE"):
                    units[j].qualified_by = u.shown
                    j -= 1
            if u.cls == "BREADTH":
                nxt = units[k + 1] if k + 1 < len(units) else None
                if not (nxt is not None and nxt.starts_with_exclusion()):
                    broad = True
        for u in units:
            if u.granted:
                for p in u.places:
                    if p.kind in ("R", "S") or not u.qualified_by:
                        grants.append(p)
                if u.qualified_by and any(p.kind in ("P", "G") for p in u.places):
                    refused.append({"clause": u.shown, "reason": 'qualified by "%s"' % u.qualified_by})
            elif u.cls in ("BARE", "REFUSED", "OTHER"):
                reason = u.reason if u.cls == "BARE" else u.why()
                refused.append({"clause": u.shown, "reason": reason})
                if u.cls == "REFUSED" or (u.cls == "BARE" and reason == "continues a refused unit"):
                    refused_places += [p for p in u.places if p.kind in ("P", "G")]
    roots, globs, scratch, any_grant = [], [], False, False
    for p in grants:
        any_grant = True
        if p.kind == "R" and root not in roots:
            roots.append(root)
        elif p.kind == "S":
            scratch = True
        elif p.kind == "G" and p.value not in globs:
            globs.append(p.value)
        elif p.kind == "P" and p.value not in roots:
            roots.append(p.value)
    excluded, not_enforceable = [], []
    for p in refused_places:
        v = p.value
        if p.kind == "P" and v in roots and v != root:
            roots.remove(v)                              # the answer both grants and refuses it: refusal wins
        if v == root or v.startswith(root + "/"):
            if v not in not_enforceable:
                not_enforceable.append(v)
        elif p.kind == "P" and v not in excluded and (
                any(v.startswith(g.rstrip("/") + "/") for g in roots) or _globbed(v, globs)):
            excluded.append(v)
    return {"roots": roots, "globs": globs, "scratch": scratch, "excluded": excluded, "refused": refused,
            "not_enforceable": not_enforceable, "broad": broad, "root": root,
            "enforced": any_grant and not broad}


def _globbed(path, globs):
    parts = path.split("/")
    for g in globs:
        gp = g.split("/")
        if len(parts) >= len(gp) and all(fnmatch.fnmatchcase(t, q) for t, q in zip(parts, gp)):
            return True
    return False


# ------------------------------------------------------------------ list entries (round 5)
#: Folders a confirmed list never names, nor anything that covers them (check-adversarial-r4.md B-1). The
#: user's home folder and the folder that holds the project root (the sibling projects, QTRM64-studio-3 of the
#: trial) are added at run time, and any entry with fewer than two folders (`/`, `/srv`) is refused too.
BROAD = ("/home", "/tmp", "/var", "/etc", "/usr", "/var/tmp")
_WILD_ONLY = re.compile(r"[*?\[\]!]+")
#: directly in a protected folder, a glob names at least this many characters (leading dots aside) before its first
#: wildcard: `/tmp/op61-*` is specific, `/home/<user>/.*` and `<project parent>/Q*` are not (round 6)
MIN_STEM = 3


def _norm(entry):
    """One list entry in the form it is compared and stored in: the word `root` or `scratchpad`, or an
    absolute path or glob with `~` expanded, the leading slashes collapsed and `.`, `..` and a trailing
    slash removed (text only: nothing is resolved here). Raises SandboxError for a relative path."""
    e = str(entry or "").strip()
    if e.lower() in (ROOT_WORD, SCRATCH_WORD):
        return e.lower()
    if e == "~" or e.startswith("~/"):
        e = os.path.expanduser("~") + e[1:]
    if not e.startswith("/"):
        raise SandboxError("SANDBOX-PATH-ABSOLUTE", "%r is not an absolute path, a ~/ path or one of the words "
                           "root and scratchpad" % str(entry))
    return os.path.normpath("/" + e.lstrip("/"))


def _is_glob(e):
    return "*" in e or "?" in e or "[" in e


def _protected(root):
    home = os.path.expanduser("~")
    out = list(BROAD) + [os.path.normpath(home), os.path.realpath(home)]
    if root:
        out += [os.path.dirname(os.path.realpath(root)), os.path.dirname(os.path.normpath(root))]
    return out


def _static_prefix(e):
    """The text of glob `e` before its first wildcard character (`/tmp/op61-` of `/tmp/op61-*`)."""
    return e[:re.search(r"[*?\[]", e).start()]


def broad_reason(e, root):
    """Why the normalised entry `e` is too broad for a confirmed list, or None: it has fewer than two
    folders, a folder that is only a wildcard (`/home/*`), or it covers one of BROAD, the home folder or
    the folder that holds the project root (`/`, `/home`, `/tm?`, the home folder and every folder above).
    A glob is also too broad (round 6, check-adversarial-r5.md note 3) when its static prefix, the text before
    its first wildcard, names fewer than two folders (`/t*/op61`) or is one of those protected folders
    (`/home/<user>/[a-z]*`, `/var/tmp/*x`, `<project parent>/[A-Z]*`: the protected folder's children,
    broadly), or when a wildcard stands for a protected folder on the way down (`/home/<use>?/x`,
    `<project parent minus a letter>*/sibling`), or when its wildcard sits directly in a protected folder after
    fewer than MIN_STEM characters, leading dots aside (`/home/<user>/.*`, `<project parent>/Q*`). A specific
    glob below a protected folder, whose static prefix has two or more folders and is not itself protected,
    stays allowed (`/tmp/op61-*`)."""
    if e in (ROOT_WORD, SCRATCH_WORD):
        return None
    parts = [x for x in e.split("/") if x]
    if len(parts) < 2:
        return "%s names fewer than two folders" % (e or "/")
    glob = _is_glob(e)
    if glob and any(_WILD_ONLY.fullmatch(x) for x in parts):
        return "%s has a folder that is only a wildcard" % e
    protected = [p for p in _protected(root) if p]
    if glob:
        static = _static_prefix(e)
        if len([x for x in static.split("/") if x]) < 2:
            return "the text of %s before its first wildcard names fewer than two folders" % e
        folder = os.path.normpath(static) if static.rstrip("/") else "/"
        if static.endswith("/") and folder in protected:
            return "%s matches the folders of %s broadly; name the folder the work writes" % (e, folder)
        stem, holder = static.rsplit("/", 1)[1], os.path.dirname(static + "x")
        if holder in protected and len(stem.lstrip(".")) < MIN_STEM:
            return ("%s matches the folders of %s broadly: directly in a protected folder a glob names at least %d "
                    "characters (dots aside) before its wildcard" % (e, holder, MIN_STEM))
        g_parts = e.split("/")
        for p in protected:
            p_parts = p.split("/")
            if len(g_parts) > len(p_parts) and any(_is_glob(x) for x in g_parts[:len(p_parts)]) and all(
                    fnmatch.fnmatchcase(t, q) for t, q in zip(p_parts, g_parts)):
                return "a wildcard of %s stands for %s; name the folder the work writes" % (e, p)
    probe = {"roots": [] if glob else [e], "globs": [e] if glob else [], "temp_ok": False}
    for p in protected:
        if covers(p, probe):
            return "%s covers %s" % (e, p)
    return None


def _link_on(path):
    """The first folder of `path` (glob characters cut off first) that is a symbolic link, or None. A part
    that does not exist ends the walk: nothing below it can be a link yet."""
    if _is_glob(path):
        cut = re.search(r"[*?\[]", path).start()
        path = path[:path.rfind("/", 0, cut) + 1] or "/"
    cur = "/"
    for part in [x for x in path.split("/") if x]:
        cur = os.path.join(cur, part)
        try:
            st = os.lstat(cur)
        except OSError:
            return None
        if stat.S_ISLNK(st.st_mode):
            return cur
    return None


def entries(paths_, root):
    """(entries as typed, normalised; resolved entries) of a path list the owner confirms. Each entry is
    resolved ONCE, here, at set time (check-adversarial-r4.md B-2): the stored form is read as it is and never
    resolved again, so a link made later at a confirmed path does not move the sandbox (every target is
    resolved, so a write through that link lands outside and is denied). Refused: a relative path
    (SANDBOX-PATH-ABSOLUTE), an entry that is too broad (SANDBOX-PATH-BROAD, `broad_reason`) and a path that
    is, or passes through, a symbolic link now (SANDBOX-PATH-LINK: confirm the real folder instead)."""
    clean, resolved = [], []
    for p in paths_ or []:
        typed_ = str(p or "").strip()
        e = _norm(typed_)
        why = broad_reason(e, root)
        if why:
            raise SandboxError("SANDBOX-PATH-BROAD", "%s is too broad for a sandbox list (%s); name the "
                               "folders the work writes" % (typed_, why))
        if e not in (ROOT_WORD, SCRATCH_WORD):
            link = _link_on(e)
            if link:
                raise SandboxError("SANDBOX-PATH-LINK", "%s passes through the link %s (to %s); confirm the "
                                   "real folder instead" % (typed_, link, os.path.realpath(link)))
        if e not in resolved:
            resolved.append(e)
            clean.append(e if e in (ROOT_WORD, SCRATCH_WORD) else typed_.rstrip("/") or typed_)
    if not resolved:
        raise SandboxError("SANDBOX-PATH-ABSOLUTE", "give at least one --path")
    return clean, resolved


#: One `alpaca sandbox set|widen <op> --path P ...` command line, as the readback prints it and the owner
#: pastes it: at the start of a line of the message (after spaces, a `$ ` prompt or a backtick).
_CMD_LINE = re.compile(r"^\s*(?:[$>]\s*)?`?(?:\S*/)?alpaca\s+sandbox\s+(set|widen)\s+(\S+)(.*)$")


def command_lines(text):
    """[(verb, op, [normalised entries], the line)] of each line of `text` that is an `alpaca sandbox set` or
    `widen` command line with one or more `--path` and nothing else before an optional `--by` / `--quote`."""
    out = []
    for line in str(text or "").splitlines():
        m = _CMD_LINE.match(line)
        if not m:
            continue
        try:
            words = shlex.split(m.group(3).strip().rstrip("`"))
        except ValueError:
            continue
        got, i, ok = [], 0, True
        while i < len(words):
            w = words[i]
            if w == "--path" and i + 1 < len(words):
                got.append(words[i + 1])
                i += 2
            elif w.startswith("--path="):
                got.append(w[len("--path="):])
                i += 1
            elif w in ("--by", "--quote") or w.startswith(("--by=", "--quote=")):
                break
            else:
                ok = False
                break
        if not ok or not got:
            continue
        try:
            norm = [_norm(g) for g in got]
        except SandboxError:
            continue
        out.append((m.group(1), m.group(2), norm, line.strip()))
    return out


def confirming_line(text, verb, op, resolved):
    """The line of `text` that is the `alpaca sandbox <verb> <op>` command line naming exactly the entries
    `resolved` (order does not matter), or None."""
    want = set(resolved)
    for v, o, norm, line in command_lines(text):
        if v == verb and o == op and set(norm) == want:
            return line
    return None


def command_line(verb, op, paths_):
    """The line the owner pastes to confirm `paths_` for `op`."""
    where = " ".join("--path %s" % shlex.quote(p) for p in paths_) or "--path <dir> [--path <dir> ...]"
    return "alpaca sandbox %s %s %s" % (verb, op or "<op>", where)


def proposal(parsed):
    """The path list a parsed answer proposes for `alpaca sandbox set`: `root` when the answer names the
    project root, then each other folder and glob it names, then `scratchpad` when it names the
    scratchpad. Empty when the reading gives no place or the answer says anywhere."""
    if not parsed.get("enforced"):
        return []
    root = parsed.get("root")
    out = [ROOT_WORD if r == root else r for r in parsed["roots"]] + list(parsed["globs"])
    if parsed.get("scratch"):
        out.append(SCRATCH_WORD)
    return [p for i, p in enumerate(out) if p not in out[:i]]


def set_command(op, paths_, verb="set"):
    """The exact command the agent runs once the owner pasted `command_line(verb, op, paths_)` as a message:
    that line, with the owner's line as the quote (round 5: the owner confirms the LIST, check-adversarial-r4.md
    B-1)."""
    line = command_line(verb, op, paths_)
    return "%s --by owner --quote %s" % (line, shlex.quote(line))


def split_broad(paths_, root):
    """(entries a confirmed list may hold, [(entry, why it is too broad)]) of a proposed list."""
    ok, broad = [], []
    for p in paths_:
        try:
            why = broad_reason(_norm(p), root)
        except SandboxError as exc:
            why = exc.detail
        if why:
            broad.append((p, why))
        else:
            ok.append(p)
    return ok, broad


def readback_lines(parsed, op=None):
    """The reading of a parsed answer, for the owner: the same lines in the batch text, after the
    answer and in `alpaca sandbox show`. It is a proposal: nothing in it is enforced until the owner
    pastes the command line it prints (`command_line`) and the agent runs `set_command`."""
    paths_, broad = split_broad(proposal(parsed), parsed.get("root"))
    lines = ["  Reading of this answer: the write guard's proposal, never enforced by itself:"]
    if paths_:
        lines.append("    proposed paths: %s" % ", ".join(paths_))
    if broad:
        lines.append("    left out, too broad for a sandbox list (never confirmable): %s" % "; ".join(
            "%s (%s)" % b for b in broad))
    if not paths_:
        lines.append("    proposed paths: none (%s; the reader knows English only). Name the paths yourself, "
                     "for example 'Writes stay in this project root and /path'." % (
                         "the answer says anywhere" if parsed["broad"] else
                         "no place the reader can read as writable"))
    lines.append("    the harness always allows: the project root, .alpaca/, the session scratchpad, ~/.claude/, "
                 "the session temp root /tmp/sbx<first 8 characters of the session id>")
    if parsed["excluded"]:
        lines.append("    excluded by the answer inside a proposed folder (a confirmed list only adds, so confirm "
                     "a narrower folder to keep these out): %s" % ", ".join(parsed["excluded"]))
    if parsed["not_enforceable"]:
        lines.append("    cannot be enforced (inside the project root, which is always allowed): %s"
                     % ", ".join(parsed["not_enforceable"]))
    if parsed["refused"]:
        lines.append("    not read as permission (nothing is proposed from these):")
        lines += ["      - %s  [%s]" % (r["clause"], r["reason"]) for r in parsed["refused"]]
    lines.append("    NOT ENFORCED until the owner confirms a path list: the owner checks the list and pastes "
                 "this line as a message of this session, after this question (a confirmation typed at a terminal "
                 "is not accepted; root stands for this project root; scratchpad for every "
                 "session scratchpad of this project, its folders under /var/tmp and, for older sessions, under "
                 "/tmp, which is RAM):")
    lines.append("      " + command_line("set", op, paths_))
    lines.append("    then the agent runs it with the owner's line as the quote:")
    lines.append("      " + set_command(op, paths_))
    return lines


def readback(text, root, op=None):
    """The reading of the answer `text` as one string (pure: no record, no folder made)."""
    return "\n".join(readback_lines(parse_answer(text, root), op))


# ------------------------------------------------------------------ the record
def _data(row):
    try:
        data = json.loads(row["data"])
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _answer(conn, op):
    """(text, event id, state, source) of the op's `sandbox` batch item: its answer when answered, else its
    proposal; (None, None, None, None) when the op has no batch or no such item."""
    from alpaca import batch
    if not batch.has_batch(conn, op):
        return None, None, None, None
    item = batch.items(conn, op).get(ITEM)
    if not item:
        return None, None, None, None
    text = item.get("answer") if item.get("state") == batch.ANSWERED else item.get("proposal")
    return (None if text is None else str(text)), item.get("event"), item.get("state"), item.get("source")


_VERB = {SET_KIND: "set", WIDEN_KIND: "widen"}


def _line_at(path, offset):
    """The transcript line that starts at byte `offset` of `path`, or None."""
    try:
        with open(path, "rb") as fh:
            fh.seek(offset)
            return fh.readline(4 * 1024 * 1024).decode("utf-8", "replace")
    except (OSError, ValueError):
        return None


def proved(row, op, root):
    """The resolved entries of a `sandbox-set` / `sandbox-widen` row when the row carries its proof, else None
    (MINOR 5 of check-adversarial-r4.md: a row written straight into the record without it is not honored).
    The proof: `resolved` entries in stored form, none too broad; an `owner_word` whose quote is the
    `alpaca sandbox <verb> <op>` command line naming exactly those entries; the session path (round 6: a
    terminal-path row is never honored); and the transcript line at the stored byte offset is an owner message
    that holds that quote. A transcript that
    is gone (Claude Code removes old ones) leaves the rest of the check: removing it must not switch the
    guard off. Reads one line, never the whole transcript."""
    data = _data(row)
    word, resolved = data.get("owner_word"), data.get("resolved")
    if not isinstance(word, dict) or not isinstance(resolved, list) or not resolved:
        return None
    for r in resolved:
        if not isinstance(r, str):
            return None
        if r not in (ROOT_WORD, SCRATCH_WORD) and not (r.startswith("/") and os.path.normpath(r) == r):
            return None
        if broad_reason(r, root):
            return None
    quote = word.get("quote")
    if not isinstance(quote, str) or confirming_line(quote, _VERB.get(row["kind"]), op, resolved) is None:
        return None
    if word.get("path") != "session":
        # round 6 (check-adversarial-r5.md note 2): a terminal confirmation leaves no file to re-read and an agent
        # with a shell can fake a terminal, so a list rests only on an owner message pasted in the session
        return None
    path, offset = word.get("transcript"), word.get("offset")
    if not isinstance(path, str) or not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        return None
    if os.path.isfile(path):
        from alpaca import ownerword
        raw = _line_at(path, offset)
        try:
            text = ownerword._entry_text(json.loads(raw)) if raw else None
        except ValueError:
            text = None
        if text is None or quote not in text:
            return None
    return resolved


def _confirmed(conn, op, root):
    """(the newest `sandbox-set` row of `op` that carries its proof, its resolved entries, [(widen row,
    resolved entries)] of the proved `sandbox-widen` rows after it]) or (None, None, [])."""
    found = None
    for row in conn.execute("SELECT id, kind, data FROM events WHERE kind=? AND op=? ORDER BY id DESC",
                            (SET_KIND, op)):
        got = proved(row, op, root)
        if got is not None:
            found = (row, got)
            break
    if found is None:
        return None, None, []
    widens = []
    for w in conn.execute("SELECT id, kind, data FROM events WHERE kind=? AND op=? AND id>? ORDER BY id",
                          (WIDEN_KIND, op, found[0]["id"])):
        got = proved(w, op, root)
        if got is not None:
            widens.append((w, got))
    return found[0], found[1], widens


def scratch_roots(root):
    """This project's scratchpad roots: the folders that hold the scratchpad of every session of the
    project (one per scratch base, alpaca/disk.py), what the list word `scratchpad` allows."""
    from alpaca import disk
    try:
        bases = disk.scratch_bases(root, None)
    except Exception:
        return []
    return [r for r in (_real_unless_link(b) for b in bases) if r]


def _real_unless_link(path):
    """The resolved `path`, or None when `path` or the folder that holds it is a symbolic link: an agent may
    remove a folder it is allowed to write and put a link to `/` in its place (check-adversarial-r4.md B-2)."""
    if os.path.islink(path) or os.path.islink(os.path.dirname(path)):
        return None
    return os.path.realpath(path)


def _place(entry, root, into):
    """Add one entry, in stored form, to `into` (roots, globs, scratch). Never resolved here: the stored
    form was resolved once at set time (`entries`)."""
    if entry == ROOT_WORD:
        real = os.path.realpath(root)
        if real not in into["roots"]:
            into["roots"].append(real)
    elif entry == SCRATCH_WORD:
        into["scratch"] = True
    elif _is_glob(entry):
        if entry not in into["globs"]:
            into["globs"].append(entry)
    elif entry not in into["roots"]:
        into["roots"].append(entry)


def sandbox_of(conn, op, root, sid=None):
    """The sandbox of one open op, or None: an op that is not open, or one with no owner-confirmed path
    list (round 4: the free-text answer alone is a proposal and binds nothing). Keys: op, event (the
    `sandbox-set` event), paths (the confirmed list and its widens, as recorded), roots, globs, scratch,
    scratch_roots, excluded (always empty: a confirmed list only adds), widened (paths `sandbox widen` added
    after the set), events (every event id the result rests on), text and answer_event (the batch answer
    or proposal, shown as the proposal), proposal."""
    status = conn.execute("SELECT status FROM ops WHERE id=?", (op,)).fetchone()
    if status is None or status["status"] != "open":
        return None
    row, resolved, widens = _confirmed(conn, op, root)
    if row is None:
        return None
    widened = [p for _w, got in widens for p in got]
    into = _from_resolved(resolved + widened, root)
    text, answer_event, _state, _source = _answer(conn, op)
    return dict(into, op=op, event=row["id"], paths=resolved + widened,
                widened=widened, events=[row["id"]] + [w["id"] for w, _got in widens], excluded=[],
                text=text or "", answer_event=answer_event,
                proposal=proposal(parse_answer(text, root)) if text else [])


def _from_resolved(resolved, root):
    into = {"roots": [], "globs": [], "scratch": False}
    for e in resolved:
        _place(e, root, into)
    into["scratch_roots"] = scratch_roots(root) if into["scratch"] else []
    return into


def from_list(paths_, root, strict=False):
    """{roots, globs, scratch, scratch_roots} a path list allows (no record read). `strict` checks the list
    as `alpaca sandbox set` does (`entries`: too broad, a link, relative) and raises SandboxError; otherwise
    an entry that is not absolute is skipped."""
    if strict:
        return _from_resolved(entries(paths_, root)[1], root)
    resolved = []
    for p in paths_:
        try:
            resolved.append(_norm(p))
        except SandboxError:
            continue
    return _from_resolved(resolved, root)


def _held_ops(conn, sid):
    """The ops of the tasks `sid` holds now, oldest claim first."""
    from alpaca import board
    rows = conn.execute("SELECT id, op FROM tasks WHERE status='doing' AND op IS NOT NULL").fetchall()
    if not rows:
        return []
    held = board.fold_leases(conn, [r["id"] for r in rows])
    out = []
    for r in rows:
        h = held.get(r["id"])
        if h and h.get("session") == sid and not h.get("ended") and r["op"] not in out:
            out.append(r["op"])
    return out


def _asked_ops(conn, sid):
    """The open ops whose batch was last shown to the owner by `sid`: the op's driver between claims."""
    from alpaca import batch, ownerword
    out = []
    for r in conn.execute("SELECT DISTINCT e.op FROM events e JOIN ops o ON o.id=e.op "
                          "WHERE e.kind=? AND o.status='open' ORDER BY e.op", (batch.ITEM_KIND,)):
        ask = ownerword.latest_ask(conn, batch.ask_subject(r["op"]))
        if ask is not None and ask["session"] == sid:
            out.append(r["op"])
    return out


def _open_ops(conn, ops):
    out = []
    for op in ops:
        r = conn.execute("SELECT status FROM ops WHERE id=?", (op,)).fetchone()
        if r is not None and r["status"] == "open":
            out.append(op)
    return out


def scratch_dirs(root, sid):
    """The session scratchpad folders: one per scratch base of the project (alpaca/disk.py)."""
    from alpaca import disk
    out = []
    try:
        bases = disk.scratch_bases(root, None)
    except Exception:
        bases = []
    for b in bases:
        real = _real_unless_link(os.path.join(b, sid))
        if real:
            out.append(real)
    return out


def _config_dirs():
    return [d for d in (os.path.join(os.path.expanduser("~"), ".claude"), os.environ.get("CLAUDE_CONFIG_DIR")) if d]


def _always(root, sid):
    """Paths allowed inside any sandbox: the project root, the record area, the session scratchpad, the
    Claude config. A place that resolves to a folder too broad for a list (a config folder replaced by a link
    to `/`) is dropped."""
    out = [os.path.realpath(root), os.path.realpath(os.path.join(root, RUNTIME))] + scratch_dirs(root, sid)
    out += [os.path.realpath(d) for d in _config_dirs()]
    out = [p for p in out if not broad_reason(p, root)]
    return [p for i, p in enumerate(out) if p not in out[:i]]


def sid8(sid):
    return re.sub(r"[^\w.-]", "_", str(sid))[:8]


def temp_root(root=None, sid=None, sb=None):
    """The folder TMPDIR and pytest's temp root point at: `/tmp/sbx<sid8>` (16 bytes), one per session,
    on RAM, never inside a project tree. The same for every answer; the harness owns it, and the deny
    text, `sandbox show` and the sandbox question all name it."""
    return os.path.join(os.path.realpath(TEMP_BASE), TEMP_PREFIX + sid8(sid))


def _uid():
    return os.getuid()


def _marker_of(path):
    """The session id the marker of temp root `path` holds, or None: `path` must be a real folder (no
    link), owned by this user and closed to everyone else, and the marker a plain file."""
    try:
        st = os.lstat(path)
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != _uid() or st.st_mode & 0o077:
            return None
        fd = os.open(os.path.join(path, MARKER), os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, encoding="utf-8") as fh:
            return fh.read(512)
    except OSError:
        return None


def make_temp_root(path, sid):
    """Make the session's temp root, safe in a shared /tmp. True when `path` is a folder of ours
    (made now, or found with a marker holding exactly this session id, or empty and ours). Anything
    else, such as a folder another user or another session made there first, a link or a file, is
    False and is never written into or allowed."""
    try:
        os.mkdir(path, 0o700)
    except FileExistsError:
        if _marker_of(path) == str(sid):
            return True
        try:                                     # a crash between mkdir and the marker: ours and empty
            st = os.lstat(path)
            if stat.S_ISDIR(st.st_mode) and st.st_uid == _uid() and not st.st_mode & 0o077 \
                    and not os.listdir(path):
                return _write_marker(path, sid)
        except OSError:
            pass
        return False
    except OSError:
        return False
    if _write_marker(path, sid):
        return True
    try:
        os.rmdir(path)
    except OSError:
        pass
    return False


def _write_marker(path, sid):
    try:
        fd = os.open(os.path.join(path, MARKER), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(str(sid))
        return True
    except OSError:
        return False


def remove_temp_root(path, sid):
    """Remove temp root `path` when its marker holds exactly `sid`. Never by the 8-character name alone."""
    if _marker_of(path) != str(sid):
        return False
    import shutil
    shutil.rmtree(path, ignore_errors=True)
    return not os.path.lexists(path)


def for_session(conn, sid, root):
    """The sandbox that binds session `sid`, or None. The ops of the session are the ops of the tasks it
    holds, else the ops whose batch it last asked about; the sandbox is the union of the confirmed lists of
    those ops that have one (round 5, check-adversarial-r4.md B-3: an op with no confirmed list adds nothing
    and does not switch the others off), None when none has. Keys: ops (the ops with a list), unconfirmed
    (the others), events (op to its `sandbox-set` event), event_ids (every event the sandbox rests on), paths
    (op to its confirmed entries), roots, globs, scratch_roots, excluded, widened, always, temp_root."""
    ops = _open_ops(conn, _held_ops(conn, sid)) or _asked_ops(conn, sid)
    if not ops:
        return None
    parts, unconfirmed = [], []
    for op in ops:
        sb = sandbox_of(conn, op, root, sid)
        if sb is None:
            unconfirmed.append(op)                # round 5 (B-3): it adds nothing, and switches nothing off
        else:
            parts.append(sb)
    if not parts:
        return None
    ops = [sb["op"] for sb in parts]
    roots, globs, scratch, widened, event_ids = [], [], [], [], []
    for sb in parts:
        roots += [r for r in sb["roots"] if r not in roots]
        globs += [g for g in sb["globs"] if g not in globs]
        scratch += [s for s in sb["scratch_roots"] if s not in scratch]
        widened += sb["widened"]
        event_ids += sb["events"]
    merged = {"ops": ops, "events": {sb["op"]: sb["event"] for sb in parts},
              "event_ids": event_ids, "roots": roots, "globs": globs, "excluded": [],
              "widened": widened, "scratch": any(sb["scratch"] for sb in parts), "scratch_roots": scratch,
              "paths": {sb["op"]: sb["paths"] for sb in parts}, "root": root, "unconfirmed": unconfirmed,
              "always": _always(root, sid), "texts": {sb["op"]: sb["text"] for sb in parts}}
    merged["temp_root"] = temp_root(root, sid, merged)
    merged["temp_ok"] = True                 # `sync_env` sets it False when the folder is not ours
    return merged


# ------------------------------------------------------------------ matching
def _under(target, path):
    return target == path or target.startswith(path.rstrip("/") + "/")


def covers(target, sb):
    """True when the normalised absolute `target` lies in an allowed path of `sb`. A `guarded` path
    (the harness's own `.alpaca/sandbox/`) and an `excluded` one (a place the owner refused inside a
    writable folder) are never allowed, whatever else covers them. A guarded entry with a glob character
    matches whole paths of its own depth (the session transcripts `<config>/projects/*/*.jsonl`)."""
    t_parts = target.split("/")
    for g in (sb.get("guarded") or []) + (sb.get("excluded") or []):
        if _is_glob(g):
            g_parts = g.split("/")
            if len(t_parts) == len(g_parts) and all(fnmatch.fnmatchcase(t, p) for t, p in zip(t_parts, g_parts)):
                return False
        elif _under(target, g):
            return False
    temp = [sb.get("temp_root") or ""] if sb.get("temp_ok", True) else []
    for r in list(sb.get("roots") or []) + list(sb.get("always") or []) + list(sb.get("scratch_roots") or []) \
            + temp:
        if r and (target == r or target.startswith(r.rstrip("/") + "/")):
            return True
    for g in sb.get("globs") or []:
        g_parts = g.split("/")
        if len(t_parts) >= len(g_parts) and all(
                fnmatch.fnmatchcase(t, p) for t, p in zip(t_parts, g_parts)):
            return True
    return False


# ------------------------------------------------------------------ cache and env file
def hook_root(cwd=None):
    """The project root a hook works on: the folder the settings command `cd`s into (CLAUDE_PROJECT_DIR),
    else the payload's folder. Never the payload's folder first: a linked worktree has an ALPACA-MANIFEST
    of its own and no record."""
    from alpaca import paths
    return paths.root(os.environ.get("CLAUDE_PROJECT_DIR") or cwd or os.getcwd())


def export_hook(root, sid):
    """SessionStart: put the env line in the Claude env file (once) and mark the session wired. False when
    there is no env file (Codex, a terminal): then nothing is wired and the guard's pytest rule applies."""
    path = os.environ.get("CLAUDE_ENV_FILE")
    if not path or not sid or sid == "unknown":
        return False
    line = env_hook_line(root, sid)
    existing = ""
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as fh:
            existing = fh.read()
    if line not in existing:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line)
    mark_wired(root, sid)
    return True


def _safe(sid):
    return re.sub(r"[^\w.-]", "_", str(sid))[:96] or "unknown"


def _dir(root, *parts):
    return os.path.join(root, RUNTIME, DIR, *parts)


def cache_path(root, sid):
    return _dir(root, "cache", _safe(sid) + ".json")


def env_path(root, sid):
    return _dir(root, "env", _safe(sid) + ".sh")


def wired_path(root, sid):
    return _dir(root, "env", _safe(sid) + ".wired")


def _write(root, parts, name, text):
    from alpaca import _nofollow
    _nofollow.write_below(root, (DIR,) + tuple(parts), name, text.encode("utf-8"), mode=0o600)


def _seal(entry):
    """The hash a cache entry carries: of its session, root, time and sandbox. A file copied in from elsewhere
    (another session's entry, or one an agent wrote) does not match it (MINOR 3 of check-adversarial-r4.md)."""
    import hashlib
    body = {k: entry.get(k) for k in ("at", "root", "sid", "sandbox")}
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode("utf-8")).hexdigest()


def _cached(root, sid):
    """(cache entry or None) for `sid`: the last answer written for this root and this session, whatever its
    age, when its hash matches."""
    try:
        with open(cache_path(root, sid), encoding="utf-8") as fh:
            cached = json.load(fh)
        if isinstance(cached, dict) and cached.get("root") == root and cached.get("sid") == str(sid) \
                and cached.get("hash") == _seal(cached):
            return cached
    except (OSError, ValueError, TypeError):
        pass
    return None


def _guarded(root):
    """What a tool call may never write while a list is enforced: the harness's own folder inside `.alpaca/`
    (the cache, the env file and the denial log, which the hook writes from its own process), so an agent
    cannot switch the guard off by writing its cache; and the session transcripts under each Claude config
    folder (`projects/*/*.jsonl` and the subagent ones), where the owner's words are read from (MINOR 4 of
    check-adversarial-r4.md). Claude Code writes those itself, never through a tool call."""
    out = [os.path.join(os.path.realpath(root), RUNTIME, DIR)]
    for d in _config_dirs():
        for base in {os.path.normpath(d), os.path.realpath(d)}:
            out += [os.path.join(base, "projects", "*", "*.jsonl"),
                    os.path.join(base, "projects", "*", "*", "subagents", "*.jsonl")]
    return out


def resolve(root, sid, now=None):
    """The sandbox that binds `sid` (a dict or None), from the per-session cache when its entry is
    under CACHE_S old (an entry dated in the future, or one whose sandbox is neither None nor a dict, is
    stale), else from the record. Writes the cache, the temp root and the env file when it reads the
    record. The record is waited for LOOKUP_TIMEOUT_S; when it is locked or unreadable the last cache
    entry is used if it is at most 10 * CACHE_S (200 s) old, and with none, or an older one, this raises
    (the hook turns that into no decision)."""
    now = time.time() if now is None else now
    cached = _cached(root, sid)
    age = None
    if cached is not None:
        try:
            age = now - float(cached.get("at", 0))
        except (TypeError, ValueError):
            cached = None
        else:
            if not (cached.get("sandbox") is None or isinstance(cached.get("sandbox"), dict)):
                cached = None
    if cached is not None and 0 <= age < CACHE_S:
        return _with_guard(cached.get("sandbox"), root)
    import sqlite3
    from alpaca import db, paths
    if not os.path.isfile(paths.db_path(root)):
        return None
    try:
        conn = db.connect_readonly(root, timeout=LOOKUP_TIMEOUT_S)
        try:
            sb = for_session(conn, sid, root)
        finally:
            conn.close()
    except sqlite3.Error:
        if cached is not None and 0 <= age <= STALE_S:
            return _with_guard(cached.get("sandbox"), root)
        raise
    sync_env(root, sid, sb)
    sb = _with_guard(sb, root)
    try:
        entry = {"at": now, "root": root, "sid": str(sid), "sandbox": sb}
        entry["hash"] = _seal(entry)
        _write(root, ("cache",), _safe(sid) + ".json", json.dumps(entry))
    except OSError:
        pass
    return sb


def _with_guard(sb, root):
    if isinstance(sb, dict):
        sb["guarded"] = _guarded(root)
    return sb


def env_text(sb):
    tmp = shlex.quote(sb["temp_root"])
    return "export TMPDIR=%s\nexport PYTEST_DEBUG_TEMPROOT=%s\nexport ALPACA_SANDBOX=%s\n" % (
        tmp, tmp, shlex.quote(",".join(sb["ops"])))


def sync_env(root, sid, sb):
    """Make the env file match `sb`: written (with its temp root made) when a sandbox binds the
    session, removed when none does. The session's Bash calls source it through the line SessionStart
    put in the Claude env file (`env_hook_line`). When the temp root cannot be made safely (another
    user's folder or a link is already there) `sb["temp_ok"]` is False, no env file is written and the
    folder is not an allowed path."""
    path = env_path(root, sid)
    if sb is not None:
        sb["temp_ok"] = make_temp_root(sb["temp_root"], sid)
    if sb is None or not sb["temp_ok"]:
        try:
            os.unlink(path)
        except OSError:
            pass
        return
    text = env_text(sb)
    try:
        with open(path, encoding="utf-8") as fh:
            if fh.read() == text:
                return
    except OSError:
        pass
    _write(root, ("env",), _safe(sid) + ".sh", text)


def env_hook_line(root, sid):
    """The one static line SessionStart puts in the Claude env file."""
    p = shlex.quote(env_path(root, sid))
    return "if [ -r %s ]; then . %s; fi\n" % (p, p)


def mark_wired(root, sid):
    """Record that this session's Bash calls source the env file (SessionStart did it)."""
    _write(root, ("env",), _safe(sid) + ".wired", "1\n")


def is_wired(root, sid):
    """True when the session's Bash calls source an env file that exists: SessionStart marked it wired
    and the guard has written the file (it writes none when the temp root is not safe)."""
    return os.path.exists(wired_path(root, sid)) and os.path.exists(env_path(root, sid))


def clear_cache(root):
    base = _dir(root, "cache")
    try:
        names = os.listdir(base)
    except OSError:
        return 0
    n = 0
    for name in names:
        try:
            os.unlink(os.path.join(base, name))
            n += 1
        except OSError:
            pass
    return n


def cleanup_session(root, sid):
    """SessionEnd: remove the session's temp root (only when its marker holds this full session id),
    env file, wired mark and cache. Best effort."""
    try:
        remove_temp_root(temp_root(root, sid), sid)
    except Exception:
        pass
    for p in (env_path(root, sid), wired_path(root, sid), cache_path(root, sid)):
        try:
            os.unlink(p)
        except OSError:
            pass


# ------------------------------------------------------------------ verbs
ASK_SUBJECT = "sandbox:%s"


def asked_at(conn, op):
    """When the sandbox question of `op` was first asked: the first owner-ask of its batch (`alpaca batch
    show`) or of `alpaca sandbox ask`, as ownerword.ask_floor gives it; None when it never was."""
    from alpaca import batch, ownerword
    row = conn.execute("SELECT * FROM events WHERE kind=? AND ref IN (?, ?) ORDER BY id LIMIT 1",
                       (ownerword.ASK_KIND, batch.ask_subject(op), ASK_SUBJECT % op)).fetchone()
    return ownerword.ask_floor(row) if row is not None else None


def _offset(path, line_no):
    """The byte offset where line `line_no` (1-based) of `path` starts."""
    pos = 0
    with open(path, "rb") as fh:
        for n, raw in enumerate(fh, 1):
            if n == line_no:
                return pos
            pos += len(raw)
    raise SandboxError("SANDBOX-NOT-CONFIRMED", "transcript line %s is gone" % line_no)


def _used(conn, op, word):
    """True when an earlier `set` or `widen` of `op` rests on the same owner message."""
    for row in conn.execute("SELECT data FROM events WHERE kind IN (?, ?) AND op=?", (SET_KIND, WIDEN_KIND, op)):
        w = _data(row).get("owner_word")
        if isinstance(w, dict) and w.get("path") == "session" and w.get("transcript") == word["transcript"] \
                and w.get("line") == word["line"]:
            return True
    return False


def _names_op(text, op):
    """True when a line of `text` is an `alpaca sandbox set|widen <op>` command line, valid or not."""
    for line in str(text or "").splitlines():
        m = _CMD_LINE.match(line)
        if m and m.group(2) == op:
            return True
    return False


def _newer_recorded(conn, op, message):
    """The newest typing time of an owner message an earlier `set` or `widen` of `op` rests on, when it was typed
    after `message` (in any session), else None. Inside one session the place in the transcript decides
    (`_record_paths`); across sessions only the clock can, so an equal time does not count as newer."""
    from alpaca import ownerword
    mine = ownerword.typed_at(message)
    newest = None
    for row in conn.execute("SELECT data FROM events WHERE kind IN (?, ?) AND op=?", (SET_KIND, WIDEN_KIND, op)):
        w = _data(row).get("owner_word")
        if not isinstance(w, dict) or w.get("path") != "session":
            continue
        at = ownerword.typed_at({"timestamp": w.get("timestamp")})
        if at is not None and (mine is None or at > mine) and (newest is None or at > newest):
            newest = at
    return newest


def _record_paths(conn, op, kind, paths_, *, by, quote, session, root, environ):
    """Round 5 (check-adversarial-r4.md B-1): the owner confirms the LIST, never its parts. The owner's message,
    typed after the op's sandbox question was asked, holds at the start of one of its lines the exact command
    line `alpaca sandbox <verb> <op> --path P ...` naming exactly the entries of this call (order does not
    matter), the line the readback and `alpaca sandbox ask` print. One owner message confirms once.

    Round 6 (check-adversarial-r5.md notes 1 and 2). Only the NEWEST owner message of the session that holds an
    `alpaca sandbox set|widen <op>` line (valid or not, either verb) confirms, by place in the transcript, and
    never one typed before the owner message a recorded `set` or `widen` of the op rests on (in any session, by
    the clock): an older, wider line can never be replayed once a newer one exists (SANDBOX-NOT-NEWEST). And the
    owner confirms only by pasting the line in the agent session: the terminal path of ownerword leaves no file
    to re-read and can be faked by an agent with a shell, so it is refused here (SANDBOX-NOT-SESSION) and a row
    that claims it is not honored (`proved`)."""
    from alpaca import db, ownerword
    verb = _VERB[kind]
    clean, resolved = entries(paths_, root)
    line = command_line(verb, op, clean)
    if ownerword.real_session(environ) is None:
        raise SandboxError("SANDBOX-NOT-SESSION", "a sandbox list is confirmed only by the owner pasting the line `%s` "
                           "as a message of the agent session that runs this verb; a confirmation typed at a "
                           "terminal is not accepted for a sandbox list" % line)
    floor = asked_at(conn, op)
    if floor is None:
        raise SandboxError("SANDBOX-NOT-ASKED", "the sandbox question of %s was never asked; show the batch "
                           "(`alpaca batch show %s`) or run `alpaca sandbox ask %s --path P ...`, then the owner "
                           "pastes the line it prints" % (op, op, op))
    if confirming_line(quote, verb, op, resolved) is None:
        raise SandboxError("SANDBOX-NOT-CONFIRMED", "the quote must be the owner's message holding the line `%s` "
                           "(these paths, in any order) at the start of a line; the owner confirms the list, not "
                           "its parts" % line)
    word = ownerword.human_write(conn, session, by, subject=ASK_SUBJECT % op, answer="yes", quote=quote, root=root,
                                 form="quote", min_words=2, since=floor, environ=environ)
    if word.get("path") == "session":
        needle = str(quote).strip()
        path, messages = ownerword.session_messages(conn, ownerword.real_session(environ), root, environ)
        hits = [m for m in messages if needle in m["text"] and ownerword.typed_after(m, floor)
                and confirming_line(m["text"], verb, op, resolved) is not None]
        if not hits:
            raise SandboxError("SANDBOX-NOT-CONFIRMED", "no owner message typed after the sandbox question holds "
                               "the line `%s` at the start of a line" % line)
        m = hits[-1]
        word = {"path": "session", "transcript": path, "line": m["line"], "timestamp": m["timestamp"],
                "quote": confirming_line(m["text"], verb, op, resolved), "answer": "yes",
                "offset": _offset(path, m["line"])}
        if _used(conn, op, word):
            raise SandboxError("SANDBOX-WORD-USED", "that owner message (transcript line %s) already confirmed a "
                               "list of %s; the owner pastes the line again to confirm it again" % (m["line"], op))
        newest = [x for x in messages if _names_op(x["text"], op)][-1]
        if newest["line"] != m["line"]:
            raise SandboxError("SANDBOX-NOT-NEWEST", "the owner wrote a newer `alpaca sandbox` line for %s (transcript "
                               "line %s); only the newest owner message holding one confirms, so an older line is "
                               "never replayed" % (op, newest["line"]))
        if _newer_recorded(conn, op, m) is not None:
            raise SandboxError("SANDBOX-NOT-NEWEST", "a list of %s already rests on an owner message typed after this "
                               "one; the owner pastes the line again to confirm it now" % op)
    else:
        raise SandboxError("SANDBOX-NOT-SESSION", "a sandbox list is confirmed only by an owner message pasted in "
                           "the agent session; a terminal confirmation is not accepted")
    ev = db.append_event(conn, session=session, actor=str(by).strip(), kind=kind, op=op,
                         ref=ITEM, data={"op": op, "paths": clean, "resolved": resolved, "owner_word": word})
    clear_cache(root)
    return ev


def ask(conn, op, paths_, *, session, root):
    """Record the sandbox question of `op` (an owner-ask, subject `sandbox:<op>`) with the proposed list and
    return its text: the line the owner pastes to confirm it. The floor for `set` when the op has no batch."""
    from alpaca import ownerword
    status = conn.execute("SELECT status FROM ops WHERE id=?", (op,)).fetchone()
    if status is None or status["status"] != "open":
        raise SandboxError("SANDBOX-OP", "%s is not an open op" % op)
    clean, _resolved = entries(paths_, root)
    text = ("Sandbox of %s: the agent proposes the path list %s. Nothing is enforced until you confirm it. To "
            "confirm, paste this line as your message in this session (a confirmation typed at a terminal is not "
            "accepted; only your newest sandbox line for %s counts):\n  %s\nThe agent then runs:\n  %s"
            % (op, ", ".join(clean), op, command_line("set", op, clean), set_command(op, clean)))
    ownerword.record_ask(conn, session, ASK_SUBJECT % op, text, op=op)
    return text


def set_paths(conn, op, paths_, *, by, quote, session, root, environ=None):
    """Record the owner-confirmed path list of `op` (event `sandbox-set`), the only thing the guard
    enforces. `quote` is the owner's pasted line `alpaca sandbox set <op> --path ...` naming exactly these
    entries (`_record_paths`). The newest proved set replaces the list. Returns the event."""
    status = conn.execute("SELECT status FROM ops WHERE id=?", (op,)).fetchone()
    if status is None or status["status"] != "open":
        raise SandboxError("SANDBOX-OP", "%s is not an open op" % op)
    return _record_paths(conn, op, SET_KIND, paths_, by=by, quote=quote, session=session, root=root,
                         environ=environ)


def widen(conn, op, paths_, *, by, quote, session, root, environ=None):
    """Add `paths_` to the confirmed list of `op` on the owner's word: `quote` is the owner's pasted line
    `alpaca sandbox widen <op> --path ...` naming exactly these entries (`_record_paths`). Returns the event."""
    if sandbox_of(conn, op, root, session) is None:
        raise SandboxError("SANDBOX-NONE", "%s has no owner-confirmed path list to widen; the owner confirms "
                           "one first: the owner pastes `%s`" % (op, command_line("set", op, [])))
    return _record_paths(conn, op, WIDEN_KIND, paths_, by=by, quote=quote, session=session, root=root,
                         environ=environ)


def _confirmed_lines(sb, op):
    lines = ["  confirmed by the owner (event %s): %s" % (sb["events"][op], ", ".join(
        (sb.get("paths") or {}).get(op) or []))]
    return lines


def show_text(sb, sid=None):
    if sb is None:
        return "No sandbox binds %s: nothing is enforced." % (sid or "this session")
    lines = ["Sandbox of %s (owner-confirmed path list, event(s) %s; ENFORCED):" % (
        ", ".join(sb["ops"]), ", ".join(str(sb["events"][o]) for o in sb["ops"]))]
    for op in sb["ops"]:
        lines += _confirmed_lines(sb, op)
    lines += ["  allowed: %s" % p for p in sb["roots"] + sb["globs"]]
    if sb.get("scratch"):
        lines.append("  scratchpad (every session of this project): %s" % (
            ", ".join(sb.get("scratch_roots") or []) or "no scratchpad folder of this project yet"))
    lines.append("  always (harness): %s" % ", ".join(sb["always"]))
    lines.append("  session temp root (harness, always allowed, removed at session end; TMPDIR and "
                 "the pytest temp root): %s%s" % (sb["temp_root"], "" if sb.get("temp_ok", True)
                                                  else "  (NOT usable: not made by this session)"))
    if sb.get("widened"):
        lines.append("  widened by the owner: %s" % ", ".join(sb["widened"]))
    if sb.get("unconfirmed"):
        lines.append("  also worked on, with no confirmed path list (adds nothing): %s" % ", ".join(sb["unconfirmed"]))
    for op in sb["ops"]:
        text = (sb.get("texts") or {}).get(op)
        if text and sb.get("root"):
            lines.append("  Answer of %s (its reading is a proposal; the confirmed list above is what binds):"
                         % op)
            lines += readback_lines(parse_answer(text, sb["root"]), op)
    return "\n".join(lines)


def unconfirmed_text(conn, op, root):
    """`alpaca sandbox show <op>` for an op with no confirmed list: not enforced, and the proposal."""
    text, _ev, state, source = _answer(conn, op)
    lines = ["%s: not enforced: no confirmed path list. Nothing is blocked for this op until the owner "
             "confirms one." % op]
    if text:
        lines.append("  Sandbox answer of %s (%s%s): %s" % (op, state, ", " + source if source else "", text))
        lines += readback_lines(parse_answer(text, root), op)
    else:
        lines.append("  The sandbox item has no answer or proposal yet. To enforce, the owner names the paths, "
                     "then: %s" % set_command(op, []))
    return "\n".join(lines)


def _cmd_sandbox(args):
    from alpaca import cli, db, ownerword
    root = cli._root()
    verb = getattr(args, "sandbox_verb", None)
    session = getattr(args, "session", None) or ownerword.real_session() or "cli"
    try:
        if verb == "show":
            conn = db.connect_readonly(root)
            try:
                sid = args.session_id or getattr(args, "session", None) or "cli"
                proposed = None
                if args.op:
                    sb_op = sandbox_of(conn, args.op, root, sid)
                    sb = None
                    if sb_op is not None:
                        sb = dict(sb_op, ops=[args.op], events={args.op: sb_op["event"]},
                                  paths={args.op: sb_op["paths"]}, always=_always(root, sid),
                                  texts={args.op: sb_op["text"]}, root=root)
                        sb["temp_root"] = temp_root(root, sid, sb)
                        proposed = sb_op["proposal"]
                    else:
                        text = _answer(conn, args.op)[0]
                        proposed = proposal(parse_answer(text, root)) if text else []
                        unconfirmed = unconfirmed_text(conn, args.op, root)
                else:
                    sb = for_session(conn, sid, root)
            finally:
                conn.close()
            if args.json:
                out = {"verdict": "PASS", "sandbox": sb, "enforced": sb is not None}
                if proposed is not None:
                    out["proposal"] = proposed
                print(json.dumps(out, indent=1, ensure_ascii=True))
            elif args.op and sb is None:
                print(unconfirmed)
            else:
                print(show_text(sb, sid))
        elif verb == "ask":
            conn = db.connect(root)
            ownerword.require_real_session(session)
            print(ask(conn, args.op, args.path or [], session=session, root=root))
        elif verb in ("set", "widen"):
            conn = db.connect(root)
            if not args.by or not args.quote:
                raise SandboxError("SANDBOX-BY", "give --by owner and the owner's message with --quote")
            do = set_paths if verb == "set" else widen
            ev = do(conn, args.op, args.path or [], by=args.by, quote=args.quote, session=session, root=root)
            if verb == "set":
                print("%s sandbox set by the owner (event %s): %s. ENFORCED: a write outside these paths, the "
                      "project root and the harness places is denied." % (
                          args.op, ev["id"], ", ".join(_data(ev).get("paths") or [])))
            else:
                print("%s sandbox widened by %s (event %s)" % (args.op, ", ".join(args.path), ev["id"]))
        else:
            return cli.USAGE
    except ownerword.OwnerWordRefusal as exc:
        print("GATE alpaca-sandbox: FAIL (%s: %s)" % (exc.reason, exc.detail))
        return cli.FAIL
    except (ValueError, OSError) as exc:
        print("GATE alpaca-sandbox: FAIL (%s)" % exc)
        return cli.FAIL
    return cli.PASS


def _parser(sub):
    s = sub.add_parser("sandbox", help="show the write-time sandbox of an op or session; set or widen its "
                                       "path list on the owner's word")
    sv = s.add_subparsers(dest="sandbox_verb")
    p = sv.add_parser("show", help="the allowed paths and temp root of an op, or of a session; for an op with "
                                   "no confirmed list, the proposal read from its answer")
    p.add_argument("op", nargs="?"); p.add_argument("--session-id", dest="session_id")
    p.add_argument("--json", action="store_true")
    p = sv.add_parser("ask", help="ask the owner to confirm a path list for an op: records the question and "
                                  "prints the line the owner pastes")
    p.add_argument("op"); p.add_argument("--path", action="append", required=True,
                                         help="an absolute or ~/ path or glob, or the word root or scratchpad")
    p = sv.add_parser("set", help="record the owner-confirmed path list of an op, the only thing the write "
                                  "guard enforces (the owner pasted this command line; replaces an earlier list)")
    p.add_argument("op"); p.add_argument("--path", action="append", required=True,
                                         help="an absolute or ~/ path or glob, or the word root or scratchpad")
    p.add_argument("--by", help="a human identity (owner, human)")
    p.add_argument("--quote", help="the owner's message: the line `alpaca sandbox set <op> --path ...` they pasted")
    p = sv.add_parser("widen", help="add paths to an op's confirmed list (the owner pasted this command line)")
    p.add_argument("op"); p.add_argument("--path", action="append", required=True)
    p.add_argument("--by", help="a human identity (owner, human)")
    p.add_argument("--quote", help="the owner's message: the line `alpaca sandbox widen <op> --path ...` they pasted")


def _register():
    from alpaca import cli
    cli.command("sandbox")(_cmd_sandbox)
    cli.register_parser("sandbox", _parser)


_register()
