"""Fork-state detection: the read-only half of adoption (M4.3).

First contact is derived from the state of the record, never from a flag pair. Onboarding lands
op-0 (its open and its close) and the onboarded mark in one transaction, so the record is
all-or-nothing: a kill between the project.yaml write and the record commit can no longer leave
op-zero unclosed. It leaves a committed identity on disk with no matching record, which reads
exactly like a cloned but unrun copy and is named as such.

Three states, each a pure query with no side effect:
  fresh          no onboarding on the record and no committed identity: true first contact.
  second-machine a committed project identity travelled in (a clone, or a killed first onboard),
                 but this record was never onboarded here; it acts on nothing until work starts.
  populated      the record shows onboarding completed here (op-0 is on the record).

Sense-then-propose (M4.4)
-------------------------
Onboarding senses before it asserts. `sense(root)` is the read-only half: it derives the facts
of the tree (version control, size, shape, build and test entry points, an existing record) by
reading files only, and it is safe against a foreign repository configuration because task M1.4's
git-containment scan runs first and nothing in the tree is ever executed. Each fact carries an
evidence pointer to the file it was read from; an unsensed fact is left blank, never guessed.
`propose(facts)` folds those facts into a draft project.yaml whose every command traces to a
sensed file. Onboarding records the sensed facts as an op-zero page BEFORE it writes a single
asserted value, so the answers a human gives arrive on top of evidence rather than in place of it.
"""
import os

from alpaca import db, project
from alpaca.gates import git_containment

FRESH = "fresh"
SECOND_MACHINE = "second-machine"
POPULATED = "populated"

# Q19 cases the sensing pass distinguishes and records.
FRESH_TREE = "fresh-tree"
EXISTING_REPO = "existing-repo"

# The command entry points a project.yaml must name (M1.8 schema). Sensing fills the ones a file
# on disk implies and leaves the rest blank for the owner; it never invents one.
REQUIRED_COMMANDS = ("build", "test", "lint", "run")

# Shape files whose mere presence is a sensed fact about the tree.
_SHAPE_FILES = ("Makefile", "pyproject.toml", "pytest.ini", "setup.cfg", "setup.py",
                "package.json", "Cargo.toml", "go.mod", "requirements.txt")

_DESCRIBE = {
    FRESH: "fresh copy, never onboarded: the first chat is onboarding",
    SECOND_MACHINE: "cloned but unrun copy: identity is set, this record is empty; acts on nothing",
    POPULATED: "onboarded on this machine: the record carries op-0",
}


def _record_onboarded(conn) -> bool:
    """Derived from the state of the record. op-0 is the atomic artifact of a completed
    onboarding (its open and close land in one transaction), so its presence, not a flag, is the
    signal that this machine was onboarded here."""
    return bool(db.rows(conn, "ops", "id=?", ("op-0",)))


def detect(root, conn=None) -> str:
    """Name the fork state without acting. Pass an open conn to avoid a second handle; the query
    reads the record and project.yaml and mutates neither."""
    conn = conn or db.connect(root)
    if _record_onboarded(conn):
        return POPULATED
    cfg = project.load(root)
    if cfg.get("name") and cfg.get("template") is not True:
        return SECOND_MACHINE
    return FRESH


def describe(state: str) -> str:
    """One human line for the sentinel; the pad and the SessionStart hook carry it."""
    return _DESCRIBE.get(state, state)


# --------------------------------------------------------------------------- sense (read-only)

def _tree_size(root) -> tuple:
    """(file count, byte total) under the tree, skipping runtime state (.alpaca) and the git object
    store (.git). A pure read; os.walk never mutates and getsize opens nothing."""
    files = 0
    total = 0
    for dp, dns, fns in os.walk(root):
        dns[:] = [d for d in dns if d not in (".alpaca", ".git")]
        for fn in fns:
            files += 1
            try:
                total += os.path.getsize(os.path.join(dp, fn))
            except OSError:
                pass
    return files, total


def _shape(root):
    """The recognised project files present at the root, in a stable order. Each one is a sensed
    fact about the tree's shape and is its own evidence pointer."""
    return [f for f in _SHAPE_FILES if os.path.isfile(os.path.join(root, f))]


def _sense_commands(root) -> dict:
    """Read-only detection of build/test/lint/run entry points, each traced to the file it was
    read from. Never guesses: a command appears only when a file on disk implies it, and the
    evidence is the relative name of that file so the trace is rename-safe."""
    out = {}
    present = lambda name: os.path.isfile(os.path.join(root, name))
    if present("Makefile"):
        from alpaca import util
        txt = util.read_text(os.path.join(root, "Makefile"))
        for t in ("build", "test", "lint", "run"):
            if ("\n%s:" % t) in ("\n" + txt):
                out[t] = {"value": "make %s" % t, "evidence": "Makefile"}
    for f in ("pyproject.toml", "pytest.ini", "setup.cfg"):
        if present(f) and "test" not in out:
            out["test"] = {"value": "python3 -m pytest", "evidence": f}
            break
    if present("package.json"):
        out.setdefault("test", {"value": "npm test", "evidence": "package.json"})
        out.setdefault("build", {"value": "npm run build", "evidence": "package.json"})
    return out


def sense(root) -> dict:
    """Derive the facts of the tree without asserting anything and without executing anything.

    The M1.4 git-containment scan runs FIRST, so a foreign repository configuration is flagged
    (git_safe False, the findings named) before any other sensing. Nothing here runs git or any
    other command in the tree, so a hostile config buys no code execution: sensing reads bytes
    only. Every fact carries an evidence pointer to the file it was read from; a fact that no
    file implies is left blank (value None), never guessed.
    """
    findings = git_containment.scan(root)
    git_findings = [{"token": t, "file": f, "line": ln, "detail": d}
                    for (t, f, ln, d) in findings]
    git_safe = not git_findings
    dot_git = os.path.join(root, ".git")
    has_git = os.path.isdir(dot_git) or os.path.isfile(dot_git)
    case = EXISTING_REPO if has_git else FRESH_TREE
    n_files, n_bytes = _tree_size(root)
    shape = _shape(root)
    has_record = os.path.isfile(os.path.join(root, ".alpaca", "alpaca.db"))
    commands = _sense_commands(root)

    facts = [
        {"key": "version-control",
         "value": ("git" if has_git else None),
         "evidence": (".git" if has_git else None)},
        {"key": "size",
         "value": "%d files, %d bytes" % (n_files, n_bytes),
         "evidence": "read-only walk of the tree under the project root"},
        {"key": "shape",
         "value": (shape or None),
         "evidence": (shape or None)},
        {"key": "existing-record",
         "value": ("present" if has_record else None),
         "evidence": (os.path.join(".alpaca", "alpaca.db") if has_record else None)},
    ]
    for name in REQUIRED_COMMANDS:
        c = commands.get(name)
        facts.append({"key": "%s-entry" % name,
                      "value": (c["value"] if c else None),
                      "evidence": (c["evidence"] if c else None)})

    return {
        "case": case,
        "git_safe": git_safe,
        "git_findings": git_findings,
        "facts": facts,
        "commands": commands,
    }


# --------------------------------------------------------------------------- propose (draft)

def propose(facts: dict) -> dict:
    """Fold sensed facts into a draft project.yaml. Every command in the draft traces to a sensed
    file (the parallel `traces` map), and a command no file implied is left blank: it appears in
    `blank_commands` for the owner to fill, never as a guessed default. `existing_repo` and the
    Q19 case ride along so onboarding can record which tree it saw."""
    commands = {}
    traces = {}
    for name, c in (facts.get("commands") or {}).items():
        commands[name] = c["value"]
        traces[name] = c["evidence"]
    blank = [n for n in REQUIRED_COMMANDS if n not in commands]
    return {
        "case": facts.get("case"),
        "existing_repo": facts.get("case") == EXISTING_REPO,
        "commands": commands,
        "traces": traces,
        "blank_commands": blank,
    }
