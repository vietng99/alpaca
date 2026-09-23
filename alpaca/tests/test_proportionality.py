"""M1.6 - the proportionality tripwire.

Proof test for alpaca/gates/proportionality.py and the `non_adoptions` block of project.yaml.

Done-when (from the plan): the instrument FAILs when a forbidden import or dependency appears
anywhere under alpaca/ (source text and imports, including dynamic import by name) and PASSes on
the current tree, proved by a violating fixture PER entry so the list cannot rot silently.
The one live exception is `alpaca serve` (loopback-only local reader), listed explicitly, so an
unlisted server is still a FAIL.
"""
import os

import pytest

from alpaca.gates import proportionality, rc_conformance, verdict

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FORBIDDEN, SANCTIONED = proportionality.load_non_adoptions(REPO)


def _make_root(tmp_path):
    root = tmp_path / "proj"
    (root / "alpaca").mkdir(parents=True)
    return root


def _write(path, text):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


# --------------------------------------------------------------- the list is data, present
def test_forbidden_list_lives_in_project_yaml_not_code():
    # Read straight off project.yaml; the module must not carry the names in code.
    assert FORBIDDEN, "project.yaml declares no non_adoptions.forbidden list"
    # D10 names CrewAI / LangGraph explicitly as non-adopted runtimes.
    assert "langgraph" in FORBIDDEN
    assert "crewai" in FORBIDDEN
    # proportionality.py itself must not restate the names (the list is data, never code).
    src = open(os.path.join(REPO, "alpaca", "gates", "proportionality.py"),
               encoding="utf-8").read().lower()
    assert "langgraph" not in src and "crewai" not in src


def test_the_one_exception_is_serve_listed_with_a_justification():
    paths = {str(e.get("path")): e for e in SANCTIONED if isinstance(e, dict)}
    assert "alpaca/serve.py" in paths, "alpaca serve is not listed as the sanctioned local reader"
    entry = paths["alpaca/serve.py"]
    assert entry.get("why"), "the sanctioned entry carries no justification"
    assert entry.get("names"), "the sanctioned entry does not scope which server names it allows"


# --------------------------------------------------------------- PASS on the current tree
def test_passes_on_the_current_tree():
    assert proportionality.check(REPO, FORBIDDEN, SANCTIONED) == verdict.PASS


# --------------------------------------------------------------- one fixture per entry
def test_one_violating_fixture_per_entry(tmp_path):
    # For every forbidden entry: a fixture importing it FAILs; and dropping that one entry from
    # the list makes the SAME fixture pass, so a dead entry cannot sit on the list unexercised.
    for i, entry in enumerate(FORBIDDEN):
        root = tmp_path / ("e%d" % i)
        (root / "alpaca").mkdir(parents=True)
        fx = root / "alpaca" / "fixture.py"
        _write(str(fx), "import %s\n" % entry)

        assert proportionality.check(str(root), FORBIDDEN, SANCTIONED) == verdict.FAIL, \
            "fixture for %r did not FAIL" % entry
        found = proportionality.scan(str(root), FORBIDDEN, SANCTIONED)
        names_hit = {nm for rows in found.values() for (nm, _k, _l) in rows}
        assert entry in names_hit, "scan did not name %r" % entry

        without = [e for e in FORBIDDEN if e != entry]
        assert proportionality.check(str(root), without, SANCTIONED) == verdict.PASS, \
            "dropping %r from the list did not clear its own fixture" % entry


# --------------------------------------------------------------- dynamic import by name
def test_dynamic_import_by_name_is_caught(tmp_path):
    root = _make_root(tmp_path)
    _write(str(root / "alpaca" / "dyn.py"),
           "import importlib\n"
           "mod = importlib.import_module('langgraph')\n"
           "other = __import__('crewai')\n")
    found = proportionality.scan(str(root), FORBIDDEN, SANCTIONED)
    names_hit = {nm for rows in found.values() for (nm, _k, _l) in rows}
    assert "langgraph" in names_hit and "crewai" in names_hit
    assert proportionality.check(str(root), FORBIDDEN, SANCTIONED) == verdict.FAIL


# --------------------------------------------------------------- text / doc surface
def test_string_reference_on_a_doc_surface_is_caught(tmp_path):
    root = _make_root(tmp_path)
    # No import at all - just a name mentioned in prose on a doc surface under alpaca/.
    _write(str(root / "alpaca" / "notes.md"),
           "We considered langgraph for orchestration but did not adopt it.\n")
    found = proportionality.scan(str(root), FORBIDDEN, SANCTIONED)
    hits = [(nm, k) for rows in found.values() for (nm, k, _l) in rows]
    assert ("langgraph", "text") in hits
    assert proportionality.check(str(root), FORBIDDEN, SANCTIONED) == verdict.FAIL


def test_a_bare_substring_is_not_a_false_positive(tmp_path):
    root = _make_root(tmp_path)
    # "ray" is a forbidden name; "array" and "x_ray_" must not trip it.
    assert "ray" in FORBIDDEN, "this guard assumes 'ray' is on the list"
    _write(str(root / "alpaca" / "clean.py"),
           "array = [1, 2, 3]\n"
           "def x_ray_scan():\n"
           "    return len(array)\n")
    assert proportionality.check(str(root), FORBIDDEN, SANCTIONED) == verdict.PASS


# --------------------------------------------------------------- the sanctioned exception
def test_serve_is_exempt_but_an_unlisted_server_still_fails(tmp_path):
    # The sanctioned file may reach for its scoped server modules ...
    root = _make_root(tmp_path)
    _write(str(root / "alpaca" / "serve.py"),
           "import http.server\n"
           "httpd = http.server.ThreadingHTTPServer(('127.0.0.1', 0), None)\n")
    assert proportionality.check(str(root), FORBIDDEN, SANCTIONED) == verdict.PASS

    # ... but any OTHER file that binds a server is still a FAIL.
    _write(str(root / "alpaca" / "rogue.py"), "import http.server\n")
    assert proportionality.check(str(root), FORBIDDEN, SANCTIONED) == verdict.FAIL
    found = proportionality.scan(str(root), FORBIDDEN, SANCTIONED)
    assert "alpaca/rogue.py" in found and "alpaca/serve.py" not in found


def test_sanctioned_file_scoped_to_its_names_only(tmp_path):
    # The exemption is scoped: a sanctioned file importing an off-list forbidden name FAILs.
    root = _make_root(tmp_path)
    _write(str(root / "alpaca" / "serve.py"),
           "import http.server\n"
           "import langgraph\n")
    assert proportionality.check(str(root), FORBIDDEN, SANCTIONED) == verdict.FAIL
    found = proportionality.scan(str(root), FORBIDDEN, SANCTIONED)
    names_hit = {nm for rows in found.values() for (nm, _k, _l) in rows}
    assert "langgraph" in names_hit and "http.server" not in names_hit


# --------------------------------------------------------------- obeys the verdict contract
def test_instrument_obeys_the_verdict_contract():
    # No raw argparse at the CLI boundary; the file routes through verdict.make_parser.
    path = os.path.join(REPO, "alpaca", "gates", "proportionality.py")
    defects = rc_conformance.analyze(path, force_boundary=True)
    assert defects == [], defects
