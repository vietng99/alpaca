"""M4.4 proof: sense-then-propose adoption and the sensed-facts record.

adopt.sense(root) -> facts is read-only and safe against a foreign repository configuration
(task M1.4's git-containment scan runs first, and nothing in the tree is ever executed).
adopt.propose(facts) -> a draft project.yaml whose every command traces to a sensed file, with
an unsensed value left blank rather than guessed. Onboarding records the facts it sensed, with
their evidence pointers, as an op-zero page BEFORE it writes a single asserted value, and records
which Q19 case (a fresh tree or an existing repository) it saw.
"""
import hashlib
import os

from alpaca import adopt, cli, db, project as proj
from alpaca.gates import git_containment
from alpaca.tests.conftest import project  # noqa: F401  (pytest fixture)


# --- helpers -----------------------------------------------------------------------------------

def _manifest(root):
    """A content manifest of the whole tree: every file path -> sha256 of its bytes. Used to
    prove sensing writes nothing (the manifest is identical before and after)."""
    out = {}
    for dp, dns, fns in os.walk(root):
        for fn in sorted(fns):
            p = os.path.join(dp, fn)
            try:
                with open(p, "rb") as fh:
                    out[os.path.relpath(p, root)] = hashlib.sha256(fh.read()).hexdigest()
            except OSError:
                out[os.path.relpath(p, root)] = "unreadable"
    return out


def _write(root, rel, body):
    p = os.path.join(root, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(body)


# --- sensing is read-only ----------------------------------------------------------------------

def test_sense_writes_nothing(project):
    _write(project, "Makefile", "test:\n\tpytest\n")
    before = _manifest(project)
    facts = adopt.sense(project)
    assert isinstance(facts, dict) and "facts" in facts
    assert _manifest(project) == before          # not one byte changed


# --- a hostile repository configuration is never executed (M1.4 scan runs first) ---------------

def test_hostile_git_config_is_not_executed(project):
    # A repo-local config that turns ordinary git into code execution.
    _write(project, ".git/config", "[core]\n\thooksPath = /tmp/alpaca-evil\n")
    sentinel = os.path.join(project, "SENSE-RAN-A-HOOK")
    before = _manifest(project)
    facts = adopt.sense(project)
    # The M1.4 scan ran first and flagged it; sensing never ran git, so nothing executed.
    assert facts["git_safe"] is False
    toks = {f["token"] for f in facts["git_findings"]}
    assert git_containment.R_HOOKSPATH in toks
    assert not os.path.exists(sentinel)
    assert _manifest(project) == before


def test_clean_git_config_is_safe(project):
    _write(project, ".git/config", "[core]\n\tbare = false\n")
    facts = adopt.sense(project)
    assert facts["git_safe"] is True and facts["git_findings"] == []


# --- every proposed command traces to a sensed file --------------------------------------------

def test_each_proposed_command_carries_its_evidence_pointer(project):
    _write(project, "Makefile", "build:\n\tcc\ntest:\n\tpytest\n")
    draft = adopt.propose(adopt.sense(project))
    assert draft["commands"]["test"] == "make test"
    assert draft["traces"]["test"] == "Makefile"
    assert draft["commands"]["build"] == "make build"
    assert draft["traces"]["build"] == "Makefile"
    # every proposed command has a trace back to a real file
    assert set(draft["commands"]) == set(draft["traces"])


def test_pytest_ini_traces_to_the_file(project):
    _write(project, "pytest.ini", "[pytest]\n")
    draft = adopt.propose(adopt.sense(project))
    assert draft["commands"]["test"] == "python3 -m pytest"
    assert draft["traces"]["test"] == "pytest.ini"


# --- an unsensed value is left blank, never guessed --------------------------------------------

def test_unsensed_command_is_left_blank_not_guessed(project):
    # A bare tree: nothing on disk implies a build/test/lint/run command.
    draft = adopt.propose(adopt.sense(project))
    assert draft["commands"] == {} and draft["traces"] == {}
    assert set(draft["blank_commands"]) == set(adopt.REQUIRED_COMMANDS)


def test_partial_sense_leaves_the_rest_blank(project):
    _write(project, "Makefile", "test:\n\tpytest\n")
    draft = adopt.propose(adopt.sense(project))
    assert "test" in draft["commands"]
    assert "build" in draft["blank_commands"] and "build" not in draft["commands"]


# --- Q19: a fresh tree and an existing repository, recording which -----------------------------

def test_q19_fresh_tree_case(project):
    facts = adopt.sense(project)
    assert facts["case"] == adopt.FRESH_TREE == "fresh-tree"


def test_q19_existing_repo_case(project):
    _write(project, ".git/config", "[core]\n\tbare = false\n")
    facts = adopt.sense(project)
    assert facts["case"] == adopt.EXISTING_REPO == "existing-repo"


# --- onboarding records the sensed facts before it writes a single asserted value --------------

def test_onboard_records_sensed_facts_before_asserted_values(project):
    _write(project, "Makefile", "test:\n\tpytest\n")
    cli.main(["init"])
    assert cli.main(["onboard", "--name", "demo", "--who", "v:owner", "--what", "w"]) == 0
    conn = db.connect(project)
    evs = db.events(conn)
    kinds = [e["kind"] for e in evs]
    assert "sensed" in kinds
    sensed_i = kinds.index("sensed")
    onboard_i = kinds.index("onboard")
    # the sensed page lands strictly before the first asserted value (the onboard event / save)
    assert sensed_i < onboard_i
    page = evs[sensed_i]["data"]
    keys = {f["key"] for f in page["facts"]}
    # op-zero page enumerates version control, size, shape, entry points and existing record
    assert {"version-control", "size", "shape", "existing-record"} <= keys
    assert page["case"] == "fresh-tree"
    # each sensed command in the page carries its evidence pointer
    assert page["commands"]["test"]["evidence"] == "Makefile"


def test_generated_command_traces_to_a_sensed_file(project):
    _write(project, "Makefile", "test:\n\tpytest\n")
    cli.main(["init"])
    assert cli.main(["onboard", "--name", "demo", "--who", "v:owner", "--what", "w"]) == 0
    cfg = proj.load(project)
    assert cfg["commands"]["test"] == "make test"       # not a guess: it traces to the Makefile
    conn = db.connect(project)
    page = [e for e in db.events(conn) if e["kind"] == "sensed"][0]["data"]
    assert page["commands"]["test"]["evidence"] == "Makefile"


def test_onboard_records_existing_repo_case(project):
    _write(project, ".git/config", "[core]\n\tbare = false\n")
    cli.main(["init"])
    assert cli.main(["onboard", "--name", "demo", "--who", "v:owner", "--what", "w"]) == 0
    conn = db.connect(project)
    page = [e for e in db.events(conn) if e["kind"] == "sensed"][0]["data"]
    assert page["case"] == "existing-repo"
    assert proj.load(project)["existing_repo"] is True
