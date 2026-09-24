"""The partner runbook kit: `alpaca runbook kit [--out DIR] [--zip]`.

A partner has none of Alpaca. They hand a folder to their own agent and must get back a
runbook.yaml that `alpaca runbook check` and `alpaca intake` accept. The kit verb builds that
folder from the product's own files, so the kit cannot drift from what the machine takes:

* check_runbook.py is alpaca/runbook.py (with the verdict contract and the OpenSpec change reader
  inlined), carried by the builder, never hand-copied. Run with `python -I` from a folder that
  holds only the kit, it gives the same verdict, codes, messages and exit code as
  `alpaca runbook check` on every runbook fixture of test_runbook.py (captured live while that
  file runs), on the worked example and on OpenSpec change folders.
* runbook.schema.json is generated from the checker's field tables and tested against the
  checker: every captured fixture the checker refuses for a reason a JSON Schema can state is
  refused by the schema, and every fixture the checker refuses only for reasons a schema cannot
  state (cross references, coverage) is accepted by it.
* FORMAT.md and AGENTS.md are built from docs/runbook-format.md and the forge skill; a change
  there that the builder's rules no longer fit stops the build instead of shipping stale text.
* the zip is the same bytes on every build, every text file is pure ASCII, and the kit carries
  no sealed term.
"""
import ast
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import zipfile

import pytest
import yaml

from alpaca import cli, runbook
from alpaca.gates import verdict

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
EXAMPLE = os.path.join(REPO, "templates", "runbook-example")
EXAMPLE_V1 = os.path.join(REPO, "alpaca", "tests", "fixtures", "runbook-example-v1")
KIT_NAME = "alpaca-runbook-kit-v%d" % runbook.FORMAT

EXPECTED_FILES = sorted([
    "README.md", "AGENTS.md", "CLAUDE.md", ".claude/skills/runbook-forge/SKILL.md", "FORMAT.md",
    "runbook.schema.json", "check_runbook.py", "templates/runbook.yaml", "templates/spec.md",
    "templates/OPENSPEC.md", "example/spec.md", "example/runbook.yaml",
    "example/checks/status_codes.py", "VERSION", "LICENSE", "SHA256SUMS",
])
EXECUTABLE = {"check_runbook.py", "example/checks/status_codes.py"}


def _kit_module():
    from alpaca import runbook_kit
    return runbook_kit


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    out = tmp_path_factory.mktemp("kit-out")
    result = _kit_module().build(REPO, str(out), make_zip=True)
    return result


@pytest.fixture(scope="module")
def alone(built, tmp_path_factory):
    """A folder that holds only the kit: the checker runs there with no alpaca on its path."""
    where = tmp_path_factory.mktemp("alone")
    dest = os.path.join(str(where), KIT_NAME)
    shutil.copytree(built["kit"], dest)
    return dest


def kit_run(kit, args, cwd, flags=("-I",)):
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8", "HOME": str(cwd)}
    return subprocess.run([sys.executable] + list(flags) + [os.path.join(kit, "check_runbook.py")] + list(args),
                          cwd=str(cwd), env=env, capture_output=True, text=True, timeout=120)


def product_json(path, spec=None, no_files=False):
    result = runbook.check(path, spec_path=spec, check_files=not no_files)
    result["runbook"] = path
    if spec and result.get("spec"):
        result["spec"]["path"] = spec
    keys = ("verdict", "runbook", "spec", "errors", "warnings", "coverage")
    return json.loads(json.dumps({k: result[k] for k in keys}, sort_keys=True)), result["code"]


# ------------------------------------------------------------------------------ the layout
def test_kit_builds_with_its_layout(built):
    assert os.path.basename(built["kit"]) == KIT_NAME
    found = []
    for base, _dirs, names in os.walk(built["kit"]):
        for name in names:
            found.append(os.path.relpath(os.path.join(base, name), built["kit"]).replace(os.sep, "/"))
    assert sorted(found) == EXPECTED_FILES
    for rel in EXECUTABLE:
        assert os.stat(os.path.join(built["kit"], rel)).st_mode & stat.S_IXUSR, rel
    assert os.path.basename(built["zip"]) == KIT_NAME + ".zip"


def test_zip_holds_the_kit_folder_with_modes(built):
    with zipfile.ZipFile(built["zip"]) as zf:
        infos = zf.infolist()
        names = [i.filename for i in infos]
        assert names == sorted(names)
        assert names == ["%s/%s" % (KIT_NAME, rel) for rel in sorted(EXPECTED_FILES)]
        for info in infos:
            rel = info.filename.split("/", 1)[1]
            mode = (info.external_attr >> 16) & 0o777
            assert mode == (0o755 if rel in EXECUTABLE else 0o644), rel
            with open(os.path.join(built["kit"], rel), "rb") as fh:
                assert zf.read(info) == fh.read(), rel


def test_zip_is_the_same_bytes_on_every_build(tmp_path):
    kit = _kit_module()
    one = kit.build(REPO, str(tmp_path / "one"), make_zip=True)
    two = kit.build(REPO, str(tmp_path / "two"), make_zip=True)
    with open(one["zip"], "rb") as a, open(two["zip"], "rb") as b:
        assert a.read() == b.read()


def test_sha256sums_list_every_other_file(built):
    with open(os.path.join(built["kit"], "SHA256SUMS"), encoding="ascii") as fh:
        lines = fh.read().splitlines()
    listed = {}
    for line in lines:
        digest, rel = line.split("  ", 1)
        listed[rel] = digest
    assert sorted(listed) == [f for f in EXPECTED_FILES if f != "SHA256SUMS"]
    for rel, digest in listed.items():
        with open(os.path.join(built["kit"], rel), "rb") as fh:
            assert hashlib.sha256(fh.read()).hexdigest() == digest, rel


def test_version_names_the_format_and_the_product_commit(built):
    with open(os.path.join(built["kit"], "VERSION"), encoding="ascii") as fh:
        text = fh.read()
    assert re.search(r"^format: %d$" % runbook.FORMAT, text, re.M)
    assert re.search(r"^kit: %s$" % KIT_NAME, text, re.M)
    assert re.search(r"^product_commit: ([0-9a-f]{40}|unknown)( \(.*\))?$", text, re.M)
    assert re.search(r"^sources_sha256: [0-9a-f]{64}$", text, re.M)


def test_license_is_the_product_license(built):
    with open(os.path.join(REPO, "LICENSE"), "rb") as a, open(os.path.join(built["kit"], "LICENSE"), "rb") as b:
        assert a.read() == b.read()
    with open(os.path.join(built["kit"], "LICENSE"), encoding="ascii") as fh:
        assert fh.readline().strip() == "MIT License"


def test_example_is_the_product_example(built):
    for rel in ("spec.md", "runbook.yaml", "checks/status_codes.py"):
        with open(os.path.join(EXAMPLE, rel), "rb") as a, \
                open(os.path.join(built["kit"], "example", rel), "rb") as b:
            assert a.read() == b.read(), rel


def test_every_text_file_is_pure_ascii(built):
    for rel in EXPECTED_FILES:
        with open(os.path.join(built["kit"], rel), "rb") as fh:
            raw = fh.read()
        bad = [b for b in raw if b > 0x7F]
        assert not bad, "%s holds %d non-ASCII byte(s)" % (rel, len(bad))
        assert b"\r" not in raw, rel


# --------------------------------------------------------------------- the carried checker
def _tree(built):
    with open(os.path.join(built["kit"], "check_runbook.py"), encoding="ascii") as fh:
        src = fh.read()
    return src, ast.parse(src)


def test_checker_is_carried_from_the_product_and_only_reads(built):
    src, tree = _tree(built)
    ast.parse(src, feature_version=(3, 9))          # Python 3.9 syntax
    top = {n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
    with open(os.path.join(REPO, "alpaca", "runbook.py"), encoding="utf-8") as fh:
        product = {n.name for n in ast.parse(fh.read()).body if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
    runner = {"knob_values", "substitute", "_field", "_inside", "_compare", "evaluate", "_run_plugin",
              "next_attempt", "respond", "_step_end", "bar_parts", "_bar_shown", "_bar_check", "_bar_note"}
    cli_glue = {"_from_caller", "cmd_runbook", "_parser", "_register"}
    assert product - runner - cli_glue <= top
    assert not (runner & top), "the kit checker must not carry the parts that run checks"
    for fn in ("name_of", "gate_line", "make_parser", "effective_change", "_is_change", "main"):
        assert fn in top, fn
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
        elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            assert node.value.id not in ("verdict", "intake", "runbook", "util", "cli"), node.value.id
    assert "alpaca" not in imported
    assert "subprocess" not in imported
    assert imported <= {"__future__", "argparse", "difflib", "json", "math", "os", "re", "sys", "yaml"}
    assert "Built by `alpaca runbook kit`" in src


def test_checker_runs_without_alpaca_on_its_path(alone, tmp_path):
    probe = subprocess.run([sys.executable, "-I", "-c", "import alpaca"], cwd=alone,
                           capture_output=True, text=True, env={"PATH": os.environ.get("PATH", "")})
    assert probe.returncode != 0 and "No module named 'alpaca'" in probe.stderr
    proc = kit_run(alone, ["example/runbook.yaml"], cwd=alone)
    assert proc.returncode == verdict.PASS, proc.stdout + proc.stderr
    assert proc.stdout.strip().splitlines()[-1] == "GATE alpaca-runbook-check: PASS"


def test_checker_says_so_when_pyyaml_is_missing(alone):
    # -S leaves out site-packages, where PyYAML lives
    proc = kit_run(alone, ["example/runbook.yaml"], cwd=alone, flags=("-I", "-S"))
    assert proc.returncode == verdict.ABSENT
    assert "PyYAML" in proc.stderr and "pip install pyyaml" in proc.stderr


def test_checker_usage_error_is_64(alone):
    proc = kit_run(alone, [], cwd=alone)
    assert proc.returncode == verdict.USAGE
    proc = kit_run(alone, ["example/runbook.yaml", "--bogus"], cwd=alone)
    assert proc.returncode == verdict.USAGE


@pytest.mark.parametrize("args", [[], ["example/runbook.yaml", "--bogus"]])
def test_checker_usage_error_shows_the_usage_line_first(alone, args):
    # a partner reads the usage line; the HARNESS-ERROR line stays last, as in the product
    proc = kit_run(alone, args, cwd=alone)
    lines = proc.stderr.strip().splitlines()
    assert lines[0].startswith("usage: check_runbook.py"), proc.stderr
    assert lines[-1].startswith("HARNESS-ERROR check_runbook [USAGE]:"), proc.stderr


def test_readme_says_help_exits_64(built):
    text = _read(built, "README.md")
    assert re.search(r"`--help`[^.]*64", text), "README.md does not say --help exits 64"


def test_checker_matches_the_product_on_the_example_and_templates(alone, tmp_path):
    cases = [
        (os.path.join(EXAMPLE, "runbook.yaml"), None, False),
        (os.path.join(EXAMPLE, "runbook.yaml"), os.path.join(EXAMPLE, "spec.md"), False),
        (os.path.join(EXAMPLE, "runbook.yaml"), None, True),
        (os.path.join(alone, "templates", "runbook.yaml"), None, False),
    ]
    # the JSON form of the example: JSON is YAML, so the same reader takes it
    work = tmp_path / "json-form"
    shutil.copytree(EXAMPLE, str(work))
    with open(os.path.join(EXAMPLE, "runbook.yaml"), encoding="utf-8") as fh:
        (work / "runbook.json").write_text(json.dumps(yaml.safe_load(fh), indent=2), encoding="utf-8")
    cases.append((str(work / "runbook.json"), None, False))
    for path, spec, no_files in cases:
        want, code = product_json(path, spec, no_files)
        args = [path, "--json"] + (["--spec", spec] if spec else []) + (["--no-files"] if no_files else [])
        proc = kit_run(alone, args, cwd=tmp_path)
        assert proc.returncode == code, (path, proc.stderr)
        assert json.loads(proc.stdout) == want, path
        assert code == verdict.PASS, (path, want["errors"])
        # the plain lines too
        plain = kit_run(alone, args[:1] + args[2:], cwd=tmp_path)
        assert plain.stdout.strip().splitlines()[-1] == "GATE alpaca-runbook-check: PASS"


CHANGE_MAIN = """\
# auth Specification

## Requirements

### Requirement: Session Timeout
The system SHALL expire a session after 30 minutes.

#### Scenario: Idle timeout
- **WHEN** 30 minutes pass
- **THEN** the session is invalidated

### Requirement: Legacy token
The system SHALL accept old tokens.

#### Scenario: Legacy token accepted
- **WHEN** an old token arrives
- **THEN** it is accepted
"""

CHANGE_DELTA = """\
## ADDED Requirements
### Requirement: Remember me
The system SHALL keep a remembered session for 14 days.

#### Scenario: Remembered session survives a restart
- **WHEN** the browser restarts
- **THEN** the user is still signed in

## REMOVED Requirements
### Requirement: Legacy token
"""


def _change_tree(tmp_path, delta):
    root = tmp_path / "openspec"
    (root / "specs" / "auth").mkdir(parents=True)
    (root / "specs" / "auth" / "spec.md").write_text(CHANGE_MAIN, encoding="utf-8")
    change = root / "changes" / "add-remember-me"
    (change / "specs" / "auth").mkdir(parents=True)
    (change / "proposal.md").write_text("# Remember me\n", encoding="utf-8")
    (change / "specs" / "auth" / "spec.md").write_text(delta, encoding="utf-8")
    data = {"runbook": 1, "id": "auth", "title": "Auth", "stages": [
        {"id": "test", "run": "make test", "checks": [
            {"id": "t", "type": "exit-code", "covers": ["Session Timeout/Idle timeout",
                                                        "Remember me/Remembered session survives a restart"]}]}]}
    path = tmp_path / "runbook.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return str(path), str(change)


CHANGE_CASES = [
    (CHANGE_DELTA, "PASS"),
    (CHANGE_DELTA.replace("### Requirement: Legacy token", "### Requirement: Nowhere"), "FAIL"),
    (CHANGE_DELTA.replace("Remember me", "Session Timeout", 1), "FAIL"),
]


@pytest.mark.parametrize("delta,want", CHANGE_CASES)
def test_checker_reads_an_openspec_change_like_the_product(alone, tmp_path, delta, want):
    path, change = _change_tree(tmp_path, delta)
    expect, code = product_json(path, change)
    assert expect["verdict"] == want
    if want == "FAIL":
        assert "SPEC-DELTA" in [e["code"] for e in expect["errors"]]
    proc = kit_run(alone, [path, "--spec", change, "--json"], cwd=tmp_path)
    assert proc.returncode == code, proc.stderr
    assert json.loads(proc.stdout) == expect


CAPTURE_PLUGIN = r'''
"""Wraps alpaca.runbook.check while test_runbook.py runs: after every call, the kit's checker is
run on the same files with the same arguments from the same folder, and both answers are kept."""
import json
import os
import subprocess
import sys

OUT = os.environ["KIT_CAPTURE_OUT"]
KIT = os.environ["KIT_CAPTURE_KIT"]
KEYS = ("verdict", "runbook", "spec", "errors", "warnings", "coverage")


def _norm(value):
    return json.loads(json.dumps(value, sort_keys=True, default=str))


def pytest_configure(config):
    from alpaca import runbook
    real = runbook.check

    def check(path, spec_path=None, check_files=True, spec=None):
        result = real(path, spec_path=spec_path, check_files=check_files, spec=spec)
        if spec is None:
            _record(path, spec_path, check_files, result)
        return result

    real_parse = runbook.parse_spec

    def parse_spec(path):
        try:
            product = {"items": real_parse(path)}
        except Exception as exc:      # the kit must refuse the same way
            product = {"raised": type(exc).__name__, "message": str(exc)}
        _record_spec(path, product)
        if "raised" in product:
            return real_parse(path)
        return product["items"]

    runbook.check = check
    runbook.parse_spec = parse_spec


SPEC_PROBE = (
    "import json, runpy, sys\n"
    "m = runpy.run_path(sys.argv[1])\n"
    "try:\n"
    "    out = {'items': m['parse_spec'](sys.argv[2])}\n"
    "except Exception as exc:\n"
    "    out = {'raised': type(exc).__name__, 'message': str(exc)}\n"
    "print(json.dumps(out, sort_keys=True, default=str))\n")


def _record_spec(path, product):
    env = {"PATH": os.environ.get("PATH", ""), "LANG": "C.UTF-8"}
    proc = subprocess.run([sys.executable, "-I", "-c", SPEC_PROBE, os.path.join(KIT, "check_runbook.py"), path],
                          cwd=os.getcwd(), env=env, capture_output=True, text=True, timeout=120)
    try:
        kit = json.loads(proc.stdout)
    except ValueError:
        kit = {"unreadable_output": proc.stdout[-2000:], "stderr": proc.stderr[-2000:]}
    row = {"kind": "parse_spec", "test": os.environ.get("PYTEST_CURRENT_TEST", ""), "args": [path],
           "product": _norm(product), "kit": kit, "product_code": 0, "kit_code": proc.returncode, "text": None}
    with open(OUT, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, sort_keys=True) + "\n")


def _record(path, spec_path, check_files, result):
    product = _norm({k: result.get(k) for k in KEYS})
    args = [path, "--json"] + (["--spec", spec_path] if spec_path is not None else []) + \
        ([] if check_files else ["--no-files"])
    env = {"PATH": os.environ.get("PATH", ""), "LANG": "C.UTF-8"}
    proc = subprocess.run([sys.executable, "-I", os.path.join(KIT, "check_runbook.py")] + args,
                          cwd=os.getcwd(), env=env, capture_output=True, text=True, timeout=120)
    try:
        kit = json.loads(proc.stdout)
    except ValueError:
        kit = {"unreadable_output": proc.stdout[-2000:], "stderr": proc.stderr[-2000:]}
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except (OSError, UnicodeDecodeError, TypeError):
        text = None
    row = {"kind": "check", "test": os.environ.get("PYTEST_CURRENT_TEST", ""), "args": args[:1] + args[2:],
           "product_code": result.get("code"), "kit_code": proc.returncode,
           "product": product, "kit": kit, "text": text}
    with open(OUT, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, sort_keys=True) + "\n")
'''


@pytest.fixture(scope="module")
def captured(alone, tmp_path_factory):
    """Run test_runbook.py with the capture plugin; every runbook.check call it makes becomes one
    row holding the product's answer and the kit checker's answer."""
    work = tmp_path_factory.mktemp("capture")
    plugin_dir = work / "plugin"
    plugin_dir.mkdir()
    (plugin_dir / "kit_capture.py").write_text(CAPTURE_PLUGIN, encoding="utf-8")
    out = work / "rows.jsonl"
    env = dict(os.environ)
    env.update(KIT_CAPTURE_OUT=str(out), KIT_CAPTURE_KIT=alone,
               PYTHONPATH=os.pathsep.join([str(plugin_dir), REPO]))
    env.pop("PYTEST_ADDOPTS", None)
    proc = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "kit_capture",
                           "--basetemp", str(work / "inner"), os.path.join(HERE, "test_runbook.py")],
                          cwd=REPO, env=env, capture_output=True, text=True, timeout=1800)
    assert proc.returncode == 0, proc.stdout[-4000:] + proc.stderr[-4000:]
    with open(str(out), encoding="utf-8") as fh:
        rows = [json.loads(line) for line in fh]
    return rows


def _checks(captured):
    return [r for r in captured if r["kind"] == "check"]


def test_checker_matches_the_product_on_every_runbook_fixture(captured):
    checks = _checks(captured)
    assert len(checks) >= 60, len(checks)
    tests = {r["test"].split(" ")[0] for r in checks}
    assert len(tests) >= 40, len(tests)
    wrong = [(r["test"], r["args"], r["product_code"], r["kit_code"]) for r in checks
             if r["product"] != r["kit"] or r["product_code"] != r["kit_code"]]
    assert wrong == []
    seen = {e["code"] for r in checks for e in r["product"]["errors"]}
    # the replay reaches most refusals, not only the passing path
    assert len(seen) >= 30, sorted(seen)
    assert {r["product_code"] for r in checks} >= {verdict.PASS, verdict.FAIL, verdict.BLOCKED}


def test_checker_reads_every_spec_fixture_like_the_product(captured):
    """test_runbook.py's spec fixtures (spec-kit shapes, OpenSpec main, delta and folder specs)
    read by the kit's parse_spec, loaded from check_runbook.py in an isolated interpreter."""
    specs = [r for r in captured if r["kind"] == "parse_spec"]
    assert len(specs) >= 20, len(specs)
    assert [r["args"] for r in specs if r["product"] != r["kit"] or r["kit_code"] != 0] == []
    assert any("items" in r["product"] and r["product"]["items"]["format"] == "openspec" for r in specs)
    assert any("items" in r["product"] and r["product"]["items"]["format"] == "spec-kit" for r in specs)


def _extra_cases(tmp_path):
    """Runbooks for the codes test_runbook.py reaches only through validate() or not at all:
    (runbook path, spec path or None)."""
    def put(name, data):
        path = tmp_path / name
        path.write_text(data if isinstance(data, str) else yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
        return str(path)

    def base(**over):
        data = {"runbook": 1, "id": "extra", "title": "Extra",
                "knobs": [{"id": "N", "description": "count", "type": "int", "default": 2, "min": 1, "max": 9}],
                "stages": [{"id": "s", "run": "make", "checks": [{"id": "c", "type": "exit-code"}],
                            "retry": {"max_attempts": 2, "move": {"knob": "N", "by": 1}}}]}
        data.update(over)
        return data

    cases = []
    bad = base(title=5)
    bad["stages"][0]["env"] = {"A": True}
    bad["knobs"].append("not a mapping")
    cases.append((put("field-type.yaml", bad), None))
    plug = base()
    plug["stages"][0]["checks"].append({"id": "p", "type": "plugin", "script": "x.sh", "args": [True],
                                        "timeout": "soon"})
    cases.append((put("field-type-plugin.yaml", plug), None))
    frac = base()
    frac["stages"][0]["retry"]["move"]["by"] = 0.5
    cases.append((put("move-not-int.yaml", frac), None))
    folder = tmp_path / "mixed"
    (folder / "cap").mkdir(parents=True)
    (folder / "cap" / "spec.md").write_text("# Spec\n\n- **SC-001**: it works.\n", encoding="utf-8")
    cases.append((put("mixed.yaml", base()), str(folder)))
    for n, (delta, _want) in enumerate(CHANGE_CASES):
        where = tmp_path / ("change-%d" % n)
        where.mkdir()
        cases.append(_change_tree(where, delta))
    return cases


def test_checker_matches_the_product_on_the_extra_fixtures(alone, tmp_path, captured):
    reached = {e["code"] for r in _checks(captured) for e in r["product"]["errors"]}
    warned = {w["code"] for r in _checks(captured) for w in r["product"]["warnings"]}
    for path, spec in _extra_cases(tmp_path):
        want, code = product_json(path, spec)
        args = [path, "--json"] + (["--spec", spec] if spec else [])
        proc = kit_run(alone, args, cwd=tmp_path)
        assert proc.returncode == code, (path, proc.stderr)
        assert json.loads(proc.stdout) == want, path
        reached |= {e["code"] for e in want["errors"]}
        warned |= {w["code"] for w in want["warnings"]}
    # together the fixtures reach every refusal and every warning the checker has
    assert reached == set(runbook.ERROR_CODES), sorted(set(runbook.ERROR_CODES) - reached)
    assert warned == set(runbook.WARNING_CODES), sorted(set(runbook.WARNING_CODES) - warned)


# ----------------------------------------------------------------------------- the schema
#: codes a JSON Schema states in every case the checker reports them
SCHEMA_ALWAYS = {"FIELD-MISSING", "FIELD-UNKNOWN", "FIELD-EMPTY", "FIELD-TYPE", "FIELD-NOT-LIST",
                 "VERSION-UNSUPPORTED", "ID-INVALID", "RUN-MISSING", "CHECKS-EMPTY",
                 "CHECKS-WITHOUT-RUN", "CHECK-TYPE-UNKNOWN", "OP-UNKNOWN", "PLUGIN-SCRIPT-VARIABLE", "KNOB-TYPE-UNKNOWN",
                 "THEN-MISSING", "THEN-UNKNOWN"}
#: codes a JSON Schema states in some cases only (a number that must lie between two other
#: fields, a path that climbs out after normalising, a threshold that names a text knob)
SCHEMA_SOME = {"RANGE", "PATH-ESCAPES", "VALUE-NOT-NUMBER", "KNOB-DEFAULT-TYPE", "KNOB-DEFAULT-RANGE",
               # a detect is a check without id and covers: the schema states its shape, not the rest
               "DETECT-INVALID"}
#: codes that need another part of the file, the spec, the file system or the YAML text itself
SCHEMA_NEVER = {"ID-DUPLICATE", "NEEDS-UNKNOWN", "VAR-UNKNOWN", "RETRY-CHECK-UNKNOWN", "RETRY-OVERLAP",
                "KNOB-UNKNOWN", "KNOB-OWNER-ONLY", "KNOB-NOT-NUMBER", "MOVE-NOT-INT", "REGEX-INVALID",
                "PLUGIN-MISSING", "PLUGIN-NOT-EXECUTABLE", "SPEC-MISSING", "SPEC-EMPTY", "SPEC-MIXED",
                "SPEC-UNCOVERED", "COVERS-UNKNOWN", "COVERS-AMBIGUOUS", "SPEC-DELTA", "EC-UNNUMBERED",
                "RECOVERY-UNKNOWN", "RECOVERY-NOT-MARKED", "RECOVERY-NEEDED", "RECOVERY-SELF"}
NOT_A_MAPPING = {"FILE-UNREADABLE", "YAML-SYNTAX", "NOT-MAPPING", "KEY-DUPLICATE"}


def test_schema_code_classes_cover_every_error_code():
    classes = [SCHEMA_ALWAYS, SCHEMA_SOME, SCHEMA_NEVER, NOT_A_MAPPING]
    union = set().union(*classes)
    assert union == set(runbook.ERROR_CODES)
    assert sum(len(c) for c in classes) == len(union)


class SchemaError(Exception):
    pass


KNOWN_KEYWORDS = {"$schema", "$id", "$defs", "$ref", "$comment", "title", "description", "type",
                  "properties", "required", "additionalProperties", "items", "minItems", "enum",
                  "const", "pattern", "minimum", "maximum", "not", "anyOf", "oneOf", "allOf", "if",
                  "then", "else", "examples"}


def _is_type(value, name):
    if name == "object":
        return isinstance(value, dict)
    if name == "array":
        return isinstance(value, list)
    if name == "string":
        return isinstance(value, str)
    if name == "boolean":
        return isinstance(value, bool)
    if name == "null":
        return value is None
    if name == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if name == "integer":
        return (isinstance(value, int) and not isinstance(value, bool)) or (
            isinstance(value, float) and value.is_integer())
    raise SchemaError("unknown type %s" % name)


def _equal(a, b):
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    return a == b


def schema_errors(schema, value, root=None, where="$"):
    """A small draft 2020-12 validator for the keywords runbook.schema.json uses; an unknown
    keyword is an error, so the schema cannot pass here by using a keyword this reader skips."""
    root = root if root is not None else schema
    if schema is True:
        return []
    if schema is False:
        return ["%s: not allowed" % where]
    unknown = set(schema) - KNOWN_KEYWORDS
    if unknown:
        raise SchemaError("keywords this validator does not know: %s" % sorted(unknown))
    out = []
    if "$ref" in schema:
        ref = schema["$ref"]
        assert ref.startswith("#/$defs/"), ref
        out += schema_errors(root["$defs"][ref[len("#/$defs/"):]], value, root, where)
    if "type" in schema:
        types = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
        if not any(_is_type(value, t) for t in types):
            return out + ["%s: not %s" % (where, "/".join(types))]
    if "const" in schema and not _equal(value, schema["const"]):
        out.append("%s: not %r" % (where, schema["const"]))
    if "enum" in schema and not any(_equal(value, v) for v in schema["enum"]):
        out.append("%s: not one of %r" % (where, schema["enum"]))
    if isinstance(value, str) and "pattern" in schema and not re.search(schema["pattern"], value):
        out.append("%s: does not match %s" % (where, schema["pattern"]))
    if _is_type(value, "number"):
        if "minimum" in schema and value < schema["minimum"]:
            out.append("%s: below %s" % (where, schema["minimum"]))
        if "maximum" in schema and value > schema["maximum"]:
            out.append("%s: above %s" % (where, schema["maximum"]))
    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            out.append("%s: fewer than %d items" % (where, schema["minItems"]))
        if "items" in schema:
            for n, item in enumerate(value):
                out += schema_errors(schema["items"], item, root, "%s[%d]" % (where, n))
    if isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value:
                out.append("%s: %s is required" % (where, key))
        props = schema.get("properties", {})
        for key, item in value.items():
            if key in props:
                out += schema_errors(props[key], item, root, "%s.%s" % (where, key))
            elif "additionalProperties" in schema:
                out += schema_errors(schema["additionalProperties"], item, root, "%s.%s" % (where, key))
    for sub in schema.get("allOf", []):
        out += schema_errors(sub, value, root, where)
    if "anyOf" in schema and not any(not schema_errors(s, value, root, where) for s in schema["anyOf"]):
        out.append("%s: matches none of anyOf" % where)
    if "oneOf" in schema and sum(1 for s in schema["oneOf"] if not schema_errors(s, value, root, where)) != 1:
        out.append("%s: does not match exactly one of oneOf" % where)
    if "not" in schema and not schema_errors(schema["not"], value, root, where):
        out.append("%s: matches a schema it must not" % where)
    if "if" in schema:
        if not schema_errors(schema["if"], value, root, where):
            out += schema_errors(schema.get("then", True), value, root, where)
        else:
            out += schema_errors(schema.get("else", True), value, root, where)
    return out


def _schema(built):
    with open(os.path.join(built["kit"], "runbook.schema.json"), encoding="ascii") as fh:
        return json.load(fh)


def _as_json(data):
    """The runbook as a JSON document would hold it (keys are text; dates become text)."""
    return json.loads(json.dumps(data, default=str))


def test_schema_is_draft_2020_12_and_follows_the_checker_tables(built):
    schema = _schema(built)
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert set(schema["properties"]) == set(runbook.TOP_KEYS)
    assert set(schema["required"]) == {k for k, req in runbook.TOP_KEYS.items() if req}
    defs = schema["$defs"]
    for name, table in (("knob", runbook.KNOB_KEYS), ("stage", runbook.STAGE_KEYS),
                        ("retry", runbook.RETRY_KEYS), ("move", runbook.MOVE_KEYS),
                        ("owner_gate", runbook.GATE_KEYS), ("fail", runbook.FAIL_KEYS)):
        assert set(defs[name]["properties"]) == set(table), name
        assert set(defs[name]["required"]) == {k for k, req in table.items() if req}, name
    assert defs["check"]["properties"]["type"]["enum"] == list(runbook.CHECK_TYPES)
    for kind, extra in runbook.TYPE_KEYS.items():
        branch = defs["check_" + kind.replace("-", "_")]
        assert set(branch["properties"]) == set(runbook.CHECK_KEYS) | set(extra), kind
        assert set(branch["required"]) == {k for k, r in list(runbook.CHECK_KEYS.items()) + list(extra.items()) if r}
    assert defs["knob"]["properties"]["type"]["enum"] == list(runbook.KNOB_TYPES)


def test_schema_accepts_the_example_and_the_template(built):
    schema = _schema(built)
    for path in (os.path.join(EXAMPLE, "runbook.yaml"), os.path.join(built["kit"], "templates", "runbook.yaml")):
        with open(path, encoding="utf-8") as fh:
            assert schema_errors(schema, _as_json(yaml.safe_load(fh))) == [], path


def test_schema_agrees_with_the_checker_on_every_fixture(built, captured, tmp_path):
    schema = _schema(built)
    refused = accepted = 0
    disagree = []
    rows = list(_checks(captured))
    for path, _spec in _extra_cases(tmp_path):
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        rows.append({"test": path, "text": text,
                     "product": {"errors": runbook.check(path, check_files=False)["errors"]}})
    for row in rows:
        codes = {e["code"] for e in row["product"]["errors"]}
        if row["text"] is None or codes & NOT_A_MAPPING:
            continue
        try:
            data = yaml.safe_load(row["text"])
        except yaml.YAMLError:
            continue
        if not isinstance(data, dict):
            continue
        found = schema_errors(schema, _as_json(data))
        if codes & SCHEMA_ALWAYS:
            refused += 1
            if not found:
                disagree.append(("schema accepts", row["test"], sorted(codes)))
        elif codes <= SCHEMA_NEVER:
            accepted += 1
            if found:
                disagree.append(("schema refuses", row["test"], sorted(codes), found[:3]))
    assert disagree == []
    assert refused >= 20 and accepted >= 20, (refused, accepted)


def test_schema_with_a_full_validator_when_one_is_installed(built, captured):
    jsonschema = pytest.importorskip("jsonschema")
    schema = _schema(built)
    jsonschema.Draft202012Validator.check_schema(schema)
    validator = jsonschema.Draft202012Validator(schema)
    for row in _checks(captured):
        codes = {e["code"] for e in row["product"]["errors"]}
        if row["text"] is None or codes & NOT_A_MAPPING:
            continue
        data = yaml.safe_load(row["text"])
        if not isinstance(data, dict):
            continue
        mine = bool(schema_errors(schema, _as_json(data)))
        theirs = bool(list(validator.iter_errors(_as_json(data))))
        assert mine == theirs, row["test"]


# ------------------------------------------------------------------------------- the texts
def _read(built, rel):
    with open(os.path.join(built["kit"], rel), encoding="ascii") as fh:
        return fh.read()


PRODUCT_ONLY = ("alpaca runbook", "alpaca intake", "alpaca/", "docs/", "contracts/", "bin/alpaca",
                "ALPACA_CALLER_CWD", ".claude/skills/alpaca", "templates/runbook-example", "autodrive",
                "project.yaml", "next_attempt", "{{")


def test_kit_texts_name_no_product_only_path(built):
    for rel in EXPECTED_FILES:
        if rel.endswith(".md") or rel in ("VERSION", "templates/runbook.yaml"):
            text = _read(built, rel)
            for word in PRODUCT_ONLY:
                assert word not in text, (rel, word)


def test_format_names_every_code_and_every_field(built):
    text = _read(built, "FORMAT.md")
    for code in runbook.ERROR_CODES + runbook.WARNING_CODES:
        assert "`%s`" % code in text, code
    tables = [runbook.TOP_KEYS, runbook.KNOB_KEYS, runbook.STAGE_KEYS, runbook.CHECK_KEYS, runbook.RETRY_KEYS,
              runbook.MOVE_KEYS, runbook.GATE_KEYS, runbook.OUTPUT_KEYS, runbook.FAIL_KEYS]
    tables += list(runbook.TYPE_KEYS.values())
    for table in tables:
        for key in table:
            assert "`%s`" % key in text, key
    assert "python3 check_runbook.py <file> [--spec <path>] [--no-files] [--json]" in text
    assert "## What happens on our side" in text


def test_agents_md_carries_the_forge_skill(built):
    with open(os.path.join(REPO, ".claude", "skills", "alpaca-runbook-forge", "SKILL.md"), encoding="utf-8") as fh:
        skill = fh.read()
    agents = _read(built, "AGENTS.md")
    for heading in re.findall(r"^## .+$", skill, re.M):
        assert heading in agents, heading
    never = skill.split("## Never", 1)[1]
    for line in re.findall(r"^- .+$", never, re.M):
        assert line in agents, line
    for must in ("python3 <kit>/check_runbook.py runbook.yaml --spec spec.md",
                 "GATE alpaca-runbook-check: PASS", "## Step 7: deliver", "owner gate"):
        assert must in agents, must
    skill_kit = _read(built, ".claude/skills/runbook-forge/SKILL.md")
    head, body = skill_kit.split("\n---\n", 1)
    meta = yaml.safe_load(head.lstrip("-\n"))
    assert meta["name"] == "runbook-forge" and meta["description"]
    assert body.strip() == agents.strip()
    assert "AGENTS.md" in _read(built, "CLAUDE.md")


def test_readme_tells_the_partner_what_to_send_back(built):
    text = _read(built, "README.md")
    for must in ("runbook.yaml", "check_runbook.py", "AGENTS.md", "plugin", "SHA256SUMS", KIT_NAME):
        assert must in text, must


def test_readme_commands_work_from_a_runbook_in_a_subfolder(built):
    """The runbook may live below the repository root, so no command or schema line may assume
    the kit folder sits next to it."""
    text = _read(built, "README.md")
    assert "python3 %s/check_runbook.py" % KIT_NAME not in text
    assert "$schema=%s/" % KIT_NAME not in text
    assert "python3 <kit>/check_runbook.py runbook.yaml --spec spec.md" in text
    assert "../%s/check_runbook.py" % KIT_NAME in text


def test_agents_md_names_the_kit_folder_without_this_file(built):
    # the skill copy lives in .claude/skills/, where "the folder that holds this file" is wrong
    for rel in ("AGENTS.md", ".claude/skills/runbook-forge/SKILL.md"):
        text = _read(built, rel)
        assert "and this file" not in text, rel
        assert "`<kit>` is the kit folder, named `%s`" % KIT_NAME in text, rel


def test_agents_md_writes_a_gate_only_stage_without_checks(built):
    text = " ".join(_read(built, "AGENTS.md").split())
    assert "a stage that is only an owner gate has `owner_gate` and no `run` and no `checks`" in text
    assert "`CHECKS-WITHOUT-RUN`" in text


def test_agents_md_says_which_source_of_a_value_wins(built):
    text = " ".join(_read(built, "AGENTS.md").split())
    for must in ("the spec first, then the person's answers",
                 "Never choose a threshold yourself",
                 "`# default chosen by the agent: <why>`",
                 "on every value you choose"):
        assert must in text, must
    # the Never list agrees with the intro: a threshold is asked for, or left to an owner gate
    never = text.split("## Never", 1)[1]
    assert "owner gate" in never.split("Never invent a threshold", 1)[1].split("- Never", 1)[0]


def test_format_says_a_gate_only_stage_has_no_checks(built):
    text = " ".join(_read(built, "FORMAT.md").split())
    assert "`CHECKS-WITHOUT-RUN` | a stage without `run` lists `checks`" in text
    assert "A stage may be only a gate (no `run`, no `checks`)" in text


def test_intake_doc_says_to_copy_a_partner_delivery_into_the_project():
    with open(os.path.join(REPO, "docs", "intake.md"), encoding="utf-8") as fh:
        text = " ".join(fh.read().split())
    assert "A runbook a partner sends" in text and "copy the delivered folder into the project" in text
    with open(os.path.join(REPO, "docs", "runbook-format.md"), encoding="utf-8") as fh:
        doc = " ".join(fh.read().split())
    assert "copy the delivered folder into the project" in doc


BANNED = ("robust", "seamless", "leverage", "comprehensive", "ensure", "crucial", "utilize")


def test_kit_prose_is_plain(built):
    for rel in EXPECTED_FILES:
        if rel.endswith(".md"):
            text = _read(built, rel).lower()
            for word in BANNED:
                assert not re.search(r"\b%s" % word, text), (rel, word)


def test_template_runbook_passes_against_the_template_spec(alone, tmp_path):
    proc = kit_run(alone, ["templates/runbook.yaml", "--json"], cwd=alone)
    out = json.loads(proc.stdout)
    assert proc.returncode == 0, out["errors"]
    assert out["spec"]["format"] == "spec-kit" and out["coverage"]["missing"] == []


# ---------------------------------------------------------------------- sealed-term scan
def _term_list():
    from alpaca import barrier
    from alpaca.gates import leak_audit
    path = os.environ.get("ALPACA_KIT_TERMS")
    if path:
        tl, reasons, _detail = leak_audit.load_terms(path)
        assert tl is not None, reasons
        return tl
    try:
        tl, reasons, _detail = leak_audit.load_terms_from_project(REPO, base=barrier.main_worktree(REPO))
    except Exception:
        tl = None
    return tl if tl is not None and (tl.terms or tl.regexes) else None


def test_kit_carries_no_sealed_term(built):
    """Counts only: a hit report never prints the term. Without a term list (a clean clone has
    none) the shape rules still run; set ALPACA_KIT_TERMS to scan with a list."""
    from alpaca.gates import leak_audit
    tl = _term_list() or leak_audit.TermList()
    if os.environ.get("ALPACA_KIT_TERMS"):
        assert tl.terms or tl.regexes, "the term list named by ALPACA_KIT_TERMS holds no rule"
    # able to fail: every term is found when planted in a compound word
    assert leak_audit.matcher_selfcheck(tl) == []
    if tl.terms:
        probe = "Zq" + tl.terms[0][0] + "Wk"
        assert leak_audit.scan_text(tl, probe, "probe", shape_on=False)[0]
    hits = 0
    for rel in EXPECTED_FILES:
        with open(os.path.join(built["kit"], rel), "rb") as fh:
            raw = fh.read()
        text = raw.decode("ascii")
        found, _stats = leak_audit.scan_text(tl, text, rel, shape_on=True)
        hits += len(found) + len(leak_audit.byte_hits(tl, [raw]))
        hits += len(leak_audit.scan_name(tl, rel, shape_on=True)[0])
    assert hits == 0, "%d sealed-term or shape hit(s) in the kit" % hits


# ----------------------------------------------------------------------------- the verb
def test_verb_builds_the_kit_and_replaces_an_older_one(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    rc = cli.main(["runbook", "kit", "--out", str(tmp_path), "--zip"])
    out = capsys.readouterr().out
    assert rc == verdict.PASS, out
    assert out.strip().splitlines()[-1] == "GATE alpaca-runbook-kit: PASS"
    kit = tmp_path / KIT_NAME
    assert (kit / "check_runbook.py").is_file() and (tmp_path / (KIT_NAME + ".zip")).is_file()
    (kit / "stale.txt").write_text("left from an older build\n", encoding="ascii")
    rc = cli.main(["runbook", "kit", "--out", str(tmp_path)])
    assert rc == verdict.PASS
    assert not (kit / "stale.txt").exists()


def test_verb_refuses_a_folder_that_is_not_a_kit(tmp_path, capsys):
    other = tmp_path / KIT_NAME
    other.mkdir()
    (other / "notes.txt").write_text("mine\n", encoding="ascii")
    rc = cli.main(["runbook", "kit", "--out", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == verdict.BLOCKED, out
    assert (other / "notes.txt").read_text(encoding="ascii") == "mine\n"
    assert "GATE alpaca-runbook-kit: BLOCKED" in out


def test_usage_names_both_runbook_verbs(capsys):
    rc = cli.main(["runbook"])
    err = capsys.readouterr().err
    assert rc == verdict.USAGE
    assert "alpaca runbook check" in err and "alpaca runbook kit" in err


# ----------------------------------------------------------------- drift stops the build
def _copy_sources(tmp_path):
    root = tmp_path / "root"
    for rel in _kit_module().SOURCES:
        src = os.path.join(REPO, rel)
        dest = os.path.join(str(root), rel)
        if os.path.isdir(src):
            shutil.copytree(src, dest)
        else:
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.copy2(src, dest)
    return root


def test_build_stops_when_the_format_doc_no_longer_fits_the_rules(tmp_path):
    kit = _kit_module()
    root = _copy_sources(tmp_path)
    doc = root / "docs" / "runbook-format.md"
    text = doc.read_text(encoding="utf-8")
    doc.write_text(text.replace("`alpaca.runbook.next_attempt` implements this rule.",
                                "The runner in alpaca/runbook.py implements this rule."), encoding="utf-8")
    with pytest.raises(kit.KitError) as err:
        kit.build(str(root), str(tmp_path / "out"))
    assert "docs/runbook-format.md" in str(err.value)
    assert not (tmp_path / "out" / KIT_NAME).exists()


def test_build_stops_when_the_skill_no_longer_fits_the_rules(tmp_path):
    kit = _kit_module()
    root = _copy_sources(tmp_path)
    skill = root / ".claude" / "skills" / "alpaca-runbook-forge" / "SKILL.md"
    text = skill.read_text(encoding="utf-8")
    skill.write_text(text + "\nRun `alpaca intake spec.md runbook.yaml` when done.\n", encoding="utf-8")
    with pytest.raises(kit.KitError) as err:
        kit.build(str(root), str(tmp_path / "out"))
    assert "SKILL.md" in str(err.value)


def test_build_stops_when_the_example_names_a_product_only_path(tmp_path):
    kit = _kit_module()
    root = _copy_sources(tmp_path)
    spec = root / "templates" / "runbook-example" / "spec.md"
    spec.write_text(spec.read_text(encoding="utf-8") + "\nSee contracts/README.md.\n", encoding="utf-8")
    with pytest.raises(kit.KitError) as err:
        kit.build(str(root), str(tmp_path / "out"))
    assert "example/spec.md" in str(err.value) and "contracts/" in str(err.value)


def test_example_names_product_paths_only_where_it_names_the_kit_too(built):
    kit = _kit_module()
    for rel, allowed in kit.EXAMPLE_ALLOW.items():
        text = _read(built, rel)
        for phrase in allowed:
            assert phrase in text, (rel, phrase)
            assert "partner" in text, rel


def test_schema_ranges_come_from_the_checker(monkeypatch):
    kit = _kit_module()
    monkeypatch.setattr(runbook, "EXPECT_RANGE", (0, 7))
    monkeypatch.setattr(runbook, "PLUGIN_TIMEOUT_RANGE", (2, 8))
    monkeypatch.setattr(runbook, "STAGE_TIMEOUT_RANGE", (3, 9))
    defs = kit.schema()["$defs"]
    got = [(defs["check_exit_code"]["properties"]["expect"]["minimum"],
            defs["check_exit_code"]["properties"]["expect"]["maximum"]),
           (defs["check_plugin"]["properties"]["timeout"]["minimum"],
            defs["check_plugin"]["properties"]["timeout"]["maximum"]),
           (defs["stage"]["properties"]["timeout"]["minimum"], defs["stage"]["properties"]["timeout"]["maximum"])]
    assert got == [(0, 7), (2, 8), (3, 9)]


def test_checker_ranges_come_from_the_same_constants(tmp_path, monkeypatch):
    monkeypatch.setattr(runbook, "STAGE_TIMEOUT_RANGE", (1, 99))
    monkeypatch.setattr(runbook, "EXPECT_RANGE", (0, 7))
    monkeypatch.setattr(runbook, "PLUGIN_TIMEOUT_RANGE", (2, 8))
    data = {"runbook": 1, "id": "r", "title": "R", "stages": [
        {"id": "s", "run": "make", "timeout": 100, "checks": [
            {"id": "c", "type": "exit-code", "expect": 8},
            {"id": "p", "type": "plugin", "script": "x.sh", "timeout": 9}]}]}
    path = tmp_path / "runbook.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    got = {(e["code"], e["where"]) for e in runbook.check(str(path), check_files=False)["errors"]}
    assert got == {("RANGE", "stages[0].timeout"), ("RANGE", "stages[0].checks[0].expect"),
                   ("RANGE", "stages[0].checks[1].timeout")}


def test_build_stops_when_carried_code_needs_a_name_it_does_not_carry(tmp_path):
    kit = _kit_module()
    root = _copy_sources(tmp_path)
    src = root / "alpaca" / "runbook.py"
    text = src.read_text(encoding="utf-8")
    src.write_text(text.replace("def _norm(text):\n", "def _norm(text):\n    util.touch()\n", 1), encoding="utf-8")
    with pytest.raises(kit.KitError) as err:
        kit.build(str(root), str(tmp_path / "out"))
    assert "util" in str(err.value)


# ------------------------------------------------------------------------ format 2 (op-003)
def test_the_kit_is_format_2(built):
    assert runbook.FORMAT == 2 and KIT_NAME == "alpaca-runbook-kit-v2"
    assert re.search(r"^format: 2$", _read(built, "VERSION"), re.M)
    template = _read(built, "templates/runbook.yaml")
    assert re.search(r"^runbook: 2\b", template, re.M)
    assert "detect:" in template and "then:" in template and "source:" in template
    assert "**EC-001**" in _read(built, "templates/spec.md")
    assert "EC-001" in _read(built, "README.md")
    src, _tree_ = _tree(built)
    assert "respond" in src.split('"""', 2)[1]          # the head says what stays out


def test_kit_checker_covers_edge_cases_in_the_template(alone):
    proc = kit_run(alone, ["templates/runbook.yaml", "--json"], cwd=alone)
    out = json.loads(proc.stdout)
    assert proc.returncode == 0, out["errors"]
    assert "EC-001" in out["coverage"]["covered"]


def test_checker_matches_the_product_on_the_format_1_example(alone, tmp_path):
    path = os.path.join(EXAMPLE_V1, "runbook.yaml")
    want, code = product_json(path)
    assert code == verdict.PASS and [w["code"] for w in want["warnings"]] == ["EC-IGNORED"]
    proc = kit_run(alone, [path, "--json"], cwd=tmp_path)
    assert proc.returncode == code and json.loads(proc.stdout) == want


def test_schema_takes_format_2_and_keeps_format_1_to_its_fields(built):
    schema = _schema(built)
    with open(os.path.join(EXAMPLE_V1, "runbook.yaml"), encoding="utf-8") as fh:
        v1 = _as_json(yaml.safe_load(fh))
    assert schema_errors(schema, v1) == []
    v1["knobs"][0]["source"] = "default"
    assert schema_errors(schema, v1)
    v1["runbook"] = 2
    assert schema_errors(schema, v1) == []
    base = {"runbook": 2, "id": "r", "title": "R", "stages": [
        {"id": "s", "run": "make", "checks": [{"id": "c", "type": "exit-code"}],
         "fails": [{"id": "f", "when": "w", "detect": {"type": "exit-code", "expect": 3}, "then": {"run": "fix"}}]},
        {"id": "fix", "recovery": True, "run": "make fix", "checks": [{"id": "d", "type": "exit-code"}]}]}
    assert schema_errors(schema, base) == []
    for change in (lambda d: d["stages"][0]["fails"][0].pop("then"),
                   lambda d: d["stages"][0]["fails"][0].update(then="again"),
                   lambda d: d["stages"][0]["fails"][0].update(then={"run": "fix", "wait": 1}),
                   lambda d: d["stages"][0]["fails"][0]["detect"].update(id="x"),
                   lambda d: d["stages"][1].pop("run"),
                   lambda d: d["stages"][1].update(owner_gate={"approve": "a person frees it"}),
                   lambda d: d.update(runbook=3)):
        data = json.loads(json.dumps(base))
        change(data)
        assert schema_errors(schema, data), data


def test_agents_md_carries_format_2(built):
    text = " ".join(_read(built, "AGENTS.md").split())
    for must in ("`EC-001`", "`detect`", "`then`", "`recovery: true`", "`source`", "`EC-UNNUMBERED`"):
        assert must in text, must


def test_format_md_carries_format_2(built):
    text = _read(built, "FORMAT.md")
    for must in ("## Format 1 and format 2", "## Edge cases", "## Known failures", "## Provenance",
                 "Our runner applies it."):
        assert must in text, must
