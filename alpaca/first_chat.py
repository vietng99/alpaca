"""The first chat's state, and what an agent may run while it is open (op-062 t-656; design/attach-mode/
onboarding-design.md sections 7.1 and 7.6).

Design 7.1: before the owner says yes to the setup card, the first chat changes no file outside `.alpaca/`, does not
touch `.git/info/exclude` or `.git/config`, and makes no `app/`. `cli.main` asks `verb_refusal` before it runs any verb.
An agent call (see `ownerword.agent_call`) in a studio whose first chat is open runs only the verbs in
`FIRST_CHAT_VERBS`; every other verb, a domain-profile verb or a verb added later included, is refused before it runs.
The owner's own terminal is never refused here.

A command can stand in one folder and write another studio (`upgrade --target`, `new --dir`, an output folder). So the
check reads the first chat of every studio the call can write (`written_roots`): the studio around the current folder,
and the studio around each folder or file argument the command writes (`WRITTEN_PATH_ARGS`).

This is a leaf module: it imports only adopt, db, ownerword, paths and the standard library. `cli.py` imports it, so
anything it imports joins the research engine's static import closure (alpaca/research/session_engine.py
`engine_set`); `onboard_flow` would bring `alpaca/attach/` with it.
"""
from __future__ import annotations

import os
import re

from alpaca import adopt, db, ownerword, paths

TOKEN = "ONBOARD-FIRST-CHAT-OPEN"

#: What an agent call may run while the first chat is open. None = every sub-verb; a set = only those sub-verbs.
#: Each entry writes nothing outside .alpaca/ except RESUME.md, CHECKLIST.md, board.json and analytics/ (the views
#: `alpaca status` and the SessionStart hook write in any case), or it has its own gate. Pinned by
#: alpaca/tests/test_first_chat_verbs.py.
FIRST_CHAT_VERBS = {
    "onboard": None,               # look, answer, next, propose, confirm, skip, show, apply; flat form has its own gate
    "attach": {"init", "status"},  # plain init has its own gate (ATTACH-NEEDS-CARD); --card needs a confirmed card
    "new": None,                   # its own gate refuses --attach; refused here when the target is inside this studio
    "status": None, "doctor": None, "verify": None, "recall": None, "init": None,
    "serve": None,                 # alpaca new starts serve --detach in the new studio from a child with no terminal
}

TEXT_STYLE = ("The first chat is still open, so Alpaca's settings stay as they are for now. Plain writing is one line "
              "on the setup card: record the owner's answer with bin/alpaca onboard answer preset, and setup turns it "
              "on after the owner says yes.")
TEXT_GOAL = ("The first chat is still open. The owner's goal goes on the setup card (bin/alpaca onboard answer goal), "
             "and setup keeps it after the owner says yes.")
TEXT_NEW = ("The first chat is still open in this studio, and that folder is inside it. Make the new project in a "
            "folder outside this studio, or finish the first chat first.")
TEXT_ANY = ("The first chat is still open in this studio. Until the owner says yes to the setup card, or types "
            "`skip setup`, an agent may run only these Alpaca commands here: onboard, status, doctor, verify, recall, "
            "init, serve, attach init, attach status, and new for a folder outside this studio.")
TEXT_ELSEWHERE = ("The first chat is still open in the studio at %s, and this command would change files there. Leave "
                  "that studio alone until its owner says yes to its setup card or types `skip setup` in it.")
TEXT_NEW_ELSEWHERE = ("The first chat is still open in the studio at %s, and that folder is inside it. Make the new "
                      "project in a folder outside that studio, or let its owner finish the first chat first.")
_TEXTS = {"style": TEXT_STYLE, "note": TEXT_GOAL, "start": TEXT_GOAL, "intake": TEXT_GOAL, "interview": TEXT_GOAL}


def _has_opening(conn):
    """True when the record holds an `opening` answer: a first chat has started in it."""
    row = conn.execute("SELECT 1 FROM events WHERE kind='onboard-answer' AND json_valid(data) "
                       "AND json_extract(data, '$.slot')='opening' LIMIT 1").fetchone()
    return row is not None


def state(conn, root):
    """"fresh", "due", "confirmed", "done" or "skipped", or "none" for a project this does not apply to."""
    fc = adopt.first_chat_state(conn)
    if fc:
        return fc
    return "fresh" if adopt.detect(root, conn) == adopt.FRESH else "none"


def first_chat_open(conn, root):
    """True while a first chat has started and no setup has been applied: the first chat is due or confirmed, or the
    studio is fresh and a look (an opening answer) has been recorded in it. An agent session writes no configuration
    and attaches nothing in this state except through `onboard apply` after the owner's yes.

    Read in a cheap order, because `cli.main` asks this on every agent call of an unlisted verb: the meta value first,
    then one query for an opening answer, and only then `adopt.detect`, which parses project.yaml."""
    fc = adopt.first_chat_state(conn)
    if fc in ("due", "confirmed"):
        return True
    if fc:
        return False
    if not _has_opening(conn):
        return False
    return adopt.detect(root, conn) == adopt.FRESH


#: The folder or file arguments a command creates or writes, by "verb" or "verb sub-verb". The command may stand in
#: one folder and write another studio, so `verb_refusal` also checks the first chat of the studio around each of these
#: paths. `test_first_chat_verbs.py` walks every command's arguments and fails when an argument with a path-like name
#: is in neither this table nor its list of arguments that only read a file or name a record entry.
WRITTEN_PATH_ARGS = {
    "upgrade": ("target",),                # push mode: refreshes the studio named here from --source
    "update": ("target", "prefix", "bin_dir"),  # main's alpaca update (op-061): the project, the install, the launcher
    "new": ("dir",),                       # the folder of the new project (or ./<name> when --dir is left out)
    "onboard look": ("scratch",),          # the read-only copy of the owner's repository
    "wiki export": ("path",),
    "wiki extract": ("out",),
    "op close": ("package_out",),
    "backup restore": ("destination",),
    "workspace render-ingress": ("out",),
    "runbook kit": ("out",),
    "hub publish": ("drop",),
    "research report": ("out",),
    "research template": ("out",),
    "release build": ("output",),
    "release init": ("dev_output", "public_output"),
}

#: the sub-verb attribute of the commands whose parser does not name it "<verb>_verb"
SUB_DEST = {"analytics": "an_verb", "session": "session_action", "collect": "collect_action",
            "workspace": "workspace_action"}


def _slug(name):
    """alpaca.new.slug, copied so this leaf does not import alpaca.new (see the module text)."""
    s = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return s or "project"


def _roots_around(path):
    """Every Alpaca root that holds `path` (the path itself included), nearest first. A root further up counts only
    when `path` is outside that root's `.alpaca/` folder, because design 7.1 lets `.alpaca/` change (the worktrees
    under a project's `.alpaca/worktrees/` are roots of their own)."""
    found = []
    real = cur = os.path.realpath(path)
    while True:
        if os.path.isfile(paths.manifest_path(cur)):
            if not found or not path_in(real, os.path.join(cur, ".alpaca")):
                found.append(cur)
        parent = os.path.dirname(cur)
        if parent == cur:
            return found
        cur = parent


def path_in(path, top):
    """True when `path` is `top` or inside it (both real paths)."""
    return path == top or path.startswith(top + os.sep)


def _sub(args, verb):
    return getattr(args, SUB_DEST.get(verb, "%s_verb" % verb), None)


def _written_paths(args, verb, environ):
    """The absolute paths this call writes through its arguments (WRITTEN_PATH_ARGS). A relative path is read from
    the current folder and also from ALPACA_CALLER_CWD (bin/alpaca's caller folder, see util.from_caller and
    alpaca.new `_caller_dir`), so either reading is checked."""
    names = list(WRITTEN_PATH_ARGS.get(verb, ()))
    sub = _sub(args, verb)
    if sub:
        names += WRITTEN_PATH_ARGS.get("%s %s" % (verb, sub), ())
    raws = [getattr(args, n, None) for n in names]
    if verb == "new" and not getattr(args, "dir", None) and getattr(args, "name", None):
        raws.append(_slug(args.name))        # alpaca.new's default folder: ./<slug of the name>
    caller = (environ if environ is not None else os.environ).get("ALPACA_CALLER_CWD") or ""
    bases = [os.getcwd()] + ([caller] if os.path.isabs(caller) else [])
    out = []
    for raw in raws:
        if not raw or not isinstance(raw, str):
            continue
        raw = os.path.expanduser(raw)
        for base in ([None] if os.path.isabs(raw) else bases):
            full = os.path.abspath(raw if base is None else os.path.join(base, raw))
            if full not in out:
                out.append(full)
    return out


def _cwd_roots(environ):
    """The roots around the current folder, or around ALPACA_ROOT when the current folder is in none (paths.root's
    order)."""
    roots = _roots_around(os.getcwd())
    if not roots:
        env = (environ if environ is not None else os.environ).get("ALPACA_ROOT")
        if env:
            roots = _roots_around(env)
    return roots


def written_roots(args, *, environ=None, listed=False):
    """[(root, nearest_to_cwd)] the Alpaca roots this call can write, in order, without duplicates: the roots around
    the current folder (left out for a command on FIRST_CHAT_VERBS, which may run there), then the roots around each
    path the command writes. `nearest_to_cwd` is True for the studio the command runs in. A listed command other than
    `new` keeps its own rule for a path in its own studio (`onboard look` refuses a scratch folder inside the studio,
    alpaca/onboard_look.py `check_scratch`), so only the other studios its paths reach are listed for it."""
    verb = getattr(args, "verb", None)
    here = _cwd_roots(environ)
    seen, out = set(), []
    if listed and verb != "new" and here:
        seen.add(here[0])

    def add(root):
        if root not in seen:
            seen.add(root)
            out.append((root, bool(here) and root == here[0]))

    if not listed:
        for root in here:
            add(root)
    for path in _written_paths(args, verb, environ):
        for root in _roots_around(path):
            add(root)
    return out


def verb_refusal(args, *, environ=None, isatty=None):
    """None to let the verb run, or (token, text) when an agent call would run a verb the open first chat does not
    allow, or would write a studio whose first chat is open. The owner's own terminal is never refused here; a listed
    verb with no path argument costs no record read."""
    if not ownerword.agent_call(environ, isatty):
        return None
    verb = getattr(args, "verb", None)
    allowed = FIRST_CHAT_VERBS.get(verb, False)
    sub = getattr(args, "%s_verb" % verb, None)
    listed = allowed is None or (allowed is not False and sub in allowed)
    for root, here in written_roots(args, environ=environ, listed=listed):
        if not os.path.isfile(paths.db_path(root)):
            continue
        conn = db.connect_readonly(root)
        try:
            if not first_chat_open(conn, root):
                continue
        finally:
            conn.close()
        if verb == "new":
            return TOKEN, (TEXT_NEW if here else TEXT_NEW_ELSEWHERE % root)
        if not here:
            return TOKEN, TEXT_ELSEWHERE % root
        return TOKEN, _TEXTS.get(verb, TEXT_ANY)
    return None
