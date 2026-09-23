"""M1.8 - project.yaml schema (alpaca/project_schema.py).

The required key set (spec 5.11:545-566): name, project id, tier, commands
(build, test, lint, run), paths (product tree, artifacts, spec dir), phases used,
oracle classes, forbidden literals, resource class, boundary rules, non-adoptions,
fingerprint command and its frozen expected value.

The schema check produces a clear error per missing or malformed required key. The
oracle taxonomy is DATA here, never code (spec:245-247): the check verifies the shape
of the list, never its members. The skin adapter seam and the eight-op map are also
proved here.
"""
import os
import subprocess
import sys

import pytest

from alpaca import project_schema
from alpaca.gates import verdict

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _valid():
    """A complete, well-formed project.yaml document."""
    return {
        "name": "Example",
        "project_id": "abc123",
        "tier": "public",
        "commands": {
            "preflight": "true",
            "build": "make build",
            "test": "python3 -m pytest",
            "lint": "make lint",
            "run": "make run",
        },
        "paths": {
            "product_tree": "src",
            "artifacts": "docs/acceptance",
            "spec_dir": "design",
        },
        "phases": {"default": ["requirement", "design", "build", "verify", "release"]},
        "oracle_classes": ["static", "dynamic", "human"],
        "forbidden_literals": ["TODO-SECRET"],
        "resource_class": "small",
        "boundary_rules": {"root_only": True},
        "non_adoptions": {"forbidden": ["langchain"]},
        "fingerprint": {"command": "git rev-parse HEAD", "expected": "deadbeef"},
    }


# ------------------------------------------------------------------ positive path
def test_a_complete_document_validates():
    ok, errors = project_schema.validate(_valid())
    assert ok, "a complete document should validate; got %r" % (errors,)
    assert errors == []


def test_phases_as_a_bare_list_is_accepted():
    doc = _valid()
    doc["phases"] = ["requirement", "build", "release"]
    ok, errors = project_schema.validate(doc)
    assert ok, errors


# ------------------------------------------------------------------ negative path
TOP_LEVEL_REQUIRED = [
    "name", "project_id", "tier", "commands", "paths", "phases",
    "oracle_classes", "forbidden_literals", "resource_class",
    "boundary_rules", "non_adoptions", "fingerprint",
]


@pytest.mark.parametrize("key", TOP_LEVEL_REQUIRED)
def test_missing_top_level_key_is_refused_with_a_named_error(key):
    doc = _valid()
    del doc[key]
    ok, errors = project_schema.validate(doc)
    assert not ok, "removing %r should refuse the document" % key
    assert any(key in e["key"] for e in errors), \
        "no error names %r; errors=%r" % (key, errors)


@pytest.mark.parametrize("sub", ["build", "test", "lint", "run"])
def test_missing_command_is_refused(sub):
    doc = _valid()
    del doc["commands"][sub]
    ok, errors = project_schema.validate(doc)
    assert not ok
    assert any(("commands.%s" % sub) in e["key"] for e in errors), errors


@pytest.mark.parametrize("sub", ["product_tree", "artifacts", "spec_dir"])
def test_missing_path_is_refused(sub):
    doc = _valid()
    del doc["paths"][sub]
    ok, errors = project_schema.validate(doc)
    assert not ok
    assert any(("paths.%s" % sub) in e["key"] for e in errors), errors


@pytest.mark.parametrize("sub", ["command", "expected"])
def test_missing_fingerprint_field_is_refused(sub):
    doc = _valid()
    del doc["fingerprint"][sub]
    ok, errors = project_schema.validate(doc)
    assert not ok
    assert any(("fingerprint.%s" % sub) in e["key"] for e in errors), errors


def test_malformed_key_is_refused_like_a_missing_one():
    doc = _valid()
    doc["name"] = "   "  # present but empty
    ok, errors = project_schema.validate(doc)
    assert not ok
    assert any(e["key"] == "name" for e in errors), errors


def test_empty_phases_is_refused():
    doc = _valid()
    doc["phases"] = {"default": []}
    ok, errors = project_schema.validate(doc)
    assert not ok
    assert any(e["key"] == "phases" for e in errors), errors


def test_non_mapping_document_is_refused():
    ok, errors = project_schema.validate(["not", "a", "mapping"])
    assert not ok
    assert errors


def test_oracle_classes_taxonomy_is_data_not_a_fixed_set():
    # any non-empty taxonomy is accepted; the schema never dictates the members
    doc = _valid()
    doc["oracle_classes"] = ["some-project-specific-class"]
    ok, errors = project_schema.validate(doc)
    assert ok, errors


# ------------------------------------------------------------------ the real project.yaml
def test_the_repos_project_yaml_validates():
    import yaml
    with open(os.path.join(REPO, "project.yaml"), encoding="utf-8") as fh:
        doc = yaml.safe_load(fh)
    ok, errors = project_schema.validate(doc)
    assert ok, "the shipped project.yaml must satisfy its own schema; errors=%r" % (errors,)


# ------------------------------------------------------------------ CLI under the contract
def test_cli_fails_a_document_missing_a_required_key(tmp_path):
    import yaml
    doc = _valid()
    del doc["resource_class"]
    p = tmp_path / "project.yaml"
    p.write_text(yaml.safe_dump(doc), encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, "-m", "alpaca.project_schema", str(p)],
        cwd=REPO, text=True, encoding="utf-8", capture_output=True)
    assert proc.returncode == verdict.FAIL, proc.stdout + proc.stderr
    assert "resource_class" in (proc.stdout + proc.stderr)


def test_cli_passes_a_complete_document(tmp_path):
    import yaml
    p = tmp_path / "project.yaml"
    p.write_text(yaml.safe_dump(_valid()), encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, "-m", "alpaca.project_schema", str(p)],
        cwd=REPO, text=True, encoding="utf-8", capture_output=True)
    assert proc.returncode == verdict.PASS, proc.stdout + proc.stderr


# ------------------------------------------------------------------ skin adapter seam
def test_skin_adapter_maps_artifact_shapes_to_a_step_model_and_register():
    doc = {
        "skin": {"adapters": [
            {"artifact_shape": "acceptance-table", "key_column": "item",
             "step_model": "step-models/requirement.json", "register": "req_key"},
        ]},
    }
    adapters = project_schema.skin_adapter(doc)
    assert len(adapters) == 1
    a = adapters[0]
    assert a["artifact_shape"] == "acceptance-table"
    assert a["key_column"] == "item"
    assert a["step_model"] == "step-models/requirement.json"
    assert a["register"] == "req_key"


def test_skin_adapter_refuses_a_malformed_declaration():
    with pytest.raises(ValueError):
        project_schema.skin_adapter({"skin": {"adapters": [{"artifact_shape": "x"}]}})
    with pytest.raises(ValueError):
        project_schema.skin_adapter({})


def test_the_repos_skin_adapter_is_declared():
    import yaml
    with open(os.path.join(REPO, "project.yaml"), encoding="utf-8") as fh:
        doc = yaml.safe_load(fh)
    adapters = project_schema.skin_adapter(doc)
    assert adapters, "project.yaml ships one worked generic skin adapter example"


# ------------------------------------------------------------------ the eight seam ops
def test_the_eight_seam_ops_map_onto_the_file():
    m = project_schema.seam_map(_valid())
    for op in ("preflight", "build", "run", "test", "signature", "is_drift",
               "resource_class", "boundary_check"):
        assert op in m, "seam op %r not mapped" % op
    assert m["build"] == "make build"
    assert m["signature"] == "git rev-parse HEAD"
    assert m["is_drift"] == "deadbeef"
    assert m["resource_class"] == "small"
    assert m["boundary_check"] == {"root_only": True}
