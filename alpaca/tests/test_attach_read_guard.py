"""The read guard: nothing in alpaca/attach/ reads a plain studio file to decide an action (op-062 t-648, class review r2).

`state.json`, `branches/<task>.json` and `baseline/<key>.json` are written for people and for
`alpaca attach status`; any process in the studio can write them. Round 1 found one reader that trusted
them (the fetch URL), round 2 found three more (the branch record's worktree, the config digest, the
recorded base), and every hand search for the next one missed members. The rule now has one owner,
`alpaca/attach/proven.py` (which reads the record and derived paths, never a plain file, so it has no entry below), and this test fails the suite the day a second reader appears, the way
`test_attach_write_guard.py` does for writes.

What it proves: no call by name that reads a file or one of the plain-file readers exists in a file under
alpaca/attach/ outside the allow-list below, which names each place and says why it is safe. What it does
not prove: a read through a library that takes a path (`yaml.safe_load`, `shutil.copyfile`), a shell
redirect in a child process, or a read whose path comes from somewhere the scan cannot see by name.
The record (alpaca/db.py) and project.yaml (alpaca/project.py) are read by modules every verb shares.
"""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

#: names of calls that read a file (or hand back a plain studio file's content), wherever the receiver is
READ_CALLS = {"read_state", "read_branch_file", "_json_dict", "read_bytes", "read_existing", "read_text", "load", "read",
              "task_branch", "task_checkout"}
#: for these the receiver decides: json.load(fh), os.read(fd, n), and `config.task_branch` (a delegate to
#: `proven`, and at f8704b92b the raw reader of branches/<task>.json: decide on `proven.branch_view` or
#: `proven.task_checkout` directly)
READ_RECEIVERS = {"load": {"json"}, "read": {"os"}, "task_branch": {"config"}, "task_checkout": {"config"}}

#: (file, enclosing function or "*") -> why a read there is safe
ALLOWED = {
    ("alpaca/attach/safefs.py", "*"): "the primitives themselves: every reader here walks the path with lstat",
    ("alpaca/attach/config.py", "*"): "the plain-file readers (report only) and the delegates to proven",
    ("alpaca/attach/health.py", "*"): "report only: doctor rows and `attach status` read files and decide no action",
    ("alpaca/attach/baseline.py", "load"): "the baseline file, accepted only behind _recorded (the record's attach-baseline event ties key, sha and verdicts)",
    ("alpaca/attach/baseline.py", "_tail"): "the tail of a gate's own log, kept for display",
    ("alpaca/attach/reap.py", "_stat"): "the kernel's process table (/proc/<pid>/stat), not a studio file",
    ("alpaca/attach/gitops.py", "_plain_text"): "git's own admin files (gitfile, gitdir, commondir, HEAD), checked byte by byte by prove_worktree",
    ("alpaca/attach/runbooks.py", "_read_patch"): "the patch file the owner named on the command line (--patch), read once and copied; land decides on the copy",
    # t-656 moved the two command-line readers (_read_answers, _load_gates) into one checked reader; joined at the
    # op-062 integration of t-656
    ("alpaca/attach/repo.py", "read_regular"): "the --answers or --gates-file the owner named on the command line, one regular file, bounded",
    ("alpaca/attach/repo.py", "_read_exclude"): "the studio's .git/info/exclude, read to put it back on a rollback",
    ("alpaca/attach/repo.py", "_file_bytes"): "a studio file read to put it back on a rollback",
    # t-652 (study), joined at the op-062 integration of t-652. The fetch URL and the config digest of
    # study._fetch_base and study._check_fetch_config come from alpaca.attach.proven and have no entry here.
    ("alpaca/attach/study.py", "_read_map"): "the codebase map text under design/, the subject the checker proves claim by claim against hash-proved objects",
    ("alpaca/attach/study.py", "_snapshot"): "the done file of a snapshot, a cache hint: it can only skip re-exporting the scouts' reading copy, which the checker never reads",
    ("alpaca/attach/study.py", "_load_stale"): "stale.json read to merge a flag before it is rewritten: report output for status, it decides no action",
    ("alpaca/attach/study.py", "read_stale"): "stale.json handed to a caller as a report; no attach module decides an action on it",
    ("alpaca/attach/study.py", "_served_record"): "served.json, the areas of the served map: it only feeds the stale flag that status shows",
    ("alpaca/attach/study.py", "_vault_bytes"): "the wiki's vault copy compared byte for byte with the checked text: a mismatch only refuses",
    # t-650 (proof and verify), joined at the op-062 integration of t-650. The branch and base of a seal come
    # from the record through alpaca.attach.proven.branch_record; the plain file is only compared with it.
    # t-686 (approve and handoff). The receipts are record events (proven.receipt_rows); these two reads name a file
    # the owner or the agent chose on the command line, and neither one decides on its own.
    ("alpaca/attach/proven.py", "delegation"): "the delegation file named by --delegation: its hash goes into the receipt, its line counts only with a recorded decision and is read again at every use",
    ("alpaca/attach/handoff.py", "_read_body"): "the body file the owner named on the command line (--body-file), read once; its digest is part of the plan the dry run and the push must agree on",
    ("alpaca/attach/proofhook.py", "_bind"): "branches/<task>.json compared with the record's attach-branch event: a link, junk or a mismatch only refuses",
}


def _name_of(func):
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return ""


def _receiver_of(func):
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        return func.value.id
    return None


def _is_read(node) -> bool:
    name = _name_of(node.func)
    # open(p) and os.fdopen(fd): a file object whose .read() the receiver rule below cannot see (op-062
    # integration of t-656: t-656's reader read through os.fdopen(fd, "rb") and passed the guard unseen)
    if (name == "open" and isinstance(node.func, ast.Name)) or (name == "fdopen" and _receiver_of(node.func) == "os"):
        mode = node.args[1] if len(node.args) > 1 else next((k.value for k in node.keywords if k.arg == "mode"), None)
        return mode is None or not (isinstance(mode, ast.Constant) and isinstance(mode.value, str) and set(mode.value) & set("wax+"))
    if name not in READ_CALLS:
        return False
    return name not in READ_RECEIVERS or _receiver_of(node.func) in READ_RECEIVERS[name]


def scan_source(source, label):
    """[(label, line, enclosing function, call name)] for every read found in `source`."""
    hits = []

    def visit(node, scope):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            scope = node.name
        if isinstance(node, ast.Call) and _is_read(node):
            hits.append((label, node.lineno, scope, _name_of(node.func)))
        for child in ast.iter_child_nodes(node):
            visit(child, scope)

    visit(ast.parse(source), "<module>")
    return hits


def unallowed(hits):
    return [h for h in hits if (h[0], "*") not in ALLOWED and (h[0], h[2]) not in ALLOWED]


def attach_files():
    return sorted((ROOT / "alpaca" / "attach").rglob("*.py"))


def scan_tree(files):
    hits = []
    for path in files:
        hits += scan_source(path.read_text(encoding="utf-8"), path.relative_to(ROOT).as_posix())
    return unallowed(hits)


def test_no_module_in_alpaca_attach_reads_a_plain_studio_file_outside_the_allow_list():
    bad = scan_tree(attach_files())
    assert not bad, ("alpaca/attach/ reads a file outside the allow-list (decide on alpaca.attach.proven, not on a "
                     "plain studio file):\n  " + "\n  ".join("%s:%d in %s: %s" % h for h in bad))


def test_every_allow_list_entry_gives_its_reason_in_one_line():
    """op-062 integration of t-652: an entry is a decision someone must be able to check, so it says why in one
    line of words, not an empty string or a pasted paragraph."""
    bad = {k: v for k, v in ALLOWED.items() if not isinstance(v, str) or "\n" in v or not 20 <= len(v.strip()) <= 160}
    assert not bad, "allow-list entries without a one-line reason: %s" % bad


def test_every_allow_list_entry_is_used_and_names_a_real_file():
    seen = set()
    for path in attach_files():
        label = path.relative_to(ROOT).as_posix()
        for hit in scan_source(path.read_text(encoding="utf-8"), label):
            seen.add((hit[0], "*") if (hit[0], "*") in ALLOWED else (hit[0], hit[2]))
    stale = sorted(k for k in ALLOWED if k not in seen)
    assert not stale, "allow-list entries that no read uses any more (remove them): %s" % stale
    assert all((ROOT / f).is_file() for f, _ in ALLOWED)


FLAGGED = {
    "read_state": "config.read_state(root)",
    "the raw branch reader": "config.read_branch_file(root, task)",
    "a plain json dict": "config._json_dict(root, path)",
    "safefs.read_bytes": "safefs.read_bytes(root, path)",
    "safefs.read_existing": "safefs.read_existing(root, path)",
    "read_text": "path.read_text()",
    "read_bytes on a path": "Path(p).read_bytes()",
    "open default": "open(p)",
    "open rb": 'open(p, "rb")',
    "open encoding": 'open(p, encoding="utf-8")',
    "json.load": "json.load(fh)",
    "os.read": "os.read(fd, 10)",
    "os.fdopen rb": 'os.fdopen(fd, "rb", closefd=False)',
    "os.fdopen default": "os.fdopen(fd)",
    "config.task_branch": "config.task_branch(root, task)",
    "config.task_checkout": "config.task_checkout(root, task)",
}
CLEAN = {
    "open for write": 'open(p, "w")',
    "os.fdopen for write": 'os.fdopen(fd, "wb")',
    "json.loads of a string": "json.loads(text)",
    "yaml-free call": "foo(bar)",
    "a load method of another module": "baseline.load(root, sha)",
    "gitops.git": "gitops.git(app, 'status')",
    "proven.branch_view": "proven.branch_view(root, cfg, task)",
    "proven.task_checkout": "proven.task_checkout(root, task)",
}


def test_the_checker_flags_every_read_shape_and_passes_the_others():
    missed = [name for name, src in FLAGGED.items() if not scan_source(src, "snippet")]
    assert not missed, "the guard does not flag: %s" % missed
    wrong = {name: scan_source(src, "snippet") for name, src in CLEAN.items() if scan_source(src, "snippet")}
    assert not wrong, "the guard flags a non-read: %s" % wrong


#: the readers of the tree at f8704b92b that the class review named; the guard must flag exactly these shapes
OLD_READERS = {
    "worktree._need_unchanged": "def _need_unchanged(root, app):\n    state = config.read_state(root) or {}\n",
    "worktree._fetch_url": "def _fetch_url(root, cfg):\n    state = config.read_state(root)\n",
    "worktree._resumable": "def _resumable(root, app, task, branch):\n    info = config.task_branch(root, task)\n    return json.loads(open(info).read())\n",
    "worktree.cut raw reader": "def cut(root, task):\n    return config._json_dict(root, p)\n",
}


def test_the_guard_is_red_on_the_readers_of_the_old_worktree_module():
    for name, src in OLD_READERS.items():
        hits = unallowed(scan_source(src, "alpaca/attach/worktree.py"))
        assert hits, "the guard missed %s" % name


def test_a_new_module_that_reads_state_json_is_flagged():
    src = "import json\n\ndef decide(root):\n    return json.load(open(root + '/.alpaca/attach/state.json'))\n"
    assert unallowed(scan_source(src, "alpaca/attach/newthing.py"))
