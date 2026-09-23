"""M1.9 - step-model loader (alpaca/checklist/step_model.py).

Each of the five shipped step models (one per phase) loads and carries, per step,
key / name / ordinal / witness / consumes / obligation / report_back {proof, where,
how, when}; ordinals form the partition 1..N; a malformed model is refused.
"""
import json
import os

import pytest

from alpaca.checklist import Halt, step_model
from alpaca.gates import verdict

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MODELS = os.path.join(REPO, "step-models")
PHASES = ["requirement", "design", "build", "verify", "release"]

AXES = ("proof", "where", "how", "when")
STEP_FIELDS = ("key", "name", "ordinal", "witness", "consumes", "obligation", "report_back")


def _write(tmp_path, obj):
    p = tmp_path / "model.json"
    p.write_text(json.dumps(obj), encoding="utf-8")
    return str(p)


# ------------------------------------------------------------------ the five shipped models
@pytest.mark.parametrize("phase", PHASES)
def test_each_phase_model_loads_with_the_full_step_shape(phase):
    path = os.path.join(MODELS, "%s.json" % phase)
    assert os.path.isfile(path)
    model = step_model.load(path)
    assert model["steps"], "a phase with no steps maps no work"
    ordinals = []
    for step in model["steps"]:
        for field in STEP_FIELDS:
            assert field in step, "%s step missing %s" % (phase, field)
        assert isinstance(step["ordinal"], int)
        assert isinstance(step["consumes"], list)
        assert "{item}" in step["obligation"], "obligation must be item-bound"
        for axis in AXES:
            assert axis in step["report_back"]
        ordinals.append(step["ordinal"])
    assert sorted(ordinals) == list(range(1, len(model["steps"]) + 1))


def test_all_five_phases_are_present():
    present = {f[:-5] for f in os.listdir(MODELS) if f.endswith(".json")}
    assert set(PHASES) <= present


# ------------------------------------------------------------------ refusals
def test_missing_report_back_axis_is_refused(tmp_path):
    bad = {"model_id": "x", "steps": [
        {"key": "k1", "name": "n", "ordinal": 1, "witness": "w", "consumes": ["a"],
         "obligation": "do {item}", "report_back": {"proof": "p", "where": "w", "how": "h"}}]}
    path = _write(tmp_path, bad)
    with pytest.raises(Halt) as ei:
        step_model.load(path)
    assert ei.value.verdict == verdict.BLOCKED


def test_ordinals_not_a_partition_is_refused(tmp_path):
    bad = {"model_id": "x", "steps": [
        {"key": "k1", "name": "n", "ordinal": 1, "witness": "w", "consumes": ["a"],
         "obligation": "do {item}",
         "report_back": {"proof": "p", "where": "w", "how": "h", "when": "t"}},
        {"key": "k2", "name": "n", "ordinal": 3, "witness": "w", "consumes": ["a"],
         "obligation": "do {item}",
         "report_back": {"proof": "p", "where": "w", "how": "h", "when": "t"}}]}
    path = _write(tmp_path, bad)
    with pytest.raises(Halt) as ei:
        step_model.load(path)
    assert ei.value.verdict == verdict.BLOCKED


def test_duplicate_step_key_is_refused(tmp_path):
    bad = {"model_id": "x", "steps": [
        {"key": "same", "name": "n", "ordinal": 1, "witness": "w", "consumes": ["a"],
         "obligation": "do {item}",
         "report_back": {"proof": "p", "where": "w", "how": "h", "when": "t"}},
        {"key": "same", "name": "n", "ordinal": 2, "witness": "w", "consumes": ["a"],
         "obligation": "do {item}",
         "report_back": {"proof": "p", "where": "w", "how": "h", "when": "t"}}]}
    path = _write(tmp_path, bad)
    with pytest.raises(Halt) as ei:
        step_model.load(path)
    assert ei.value.verdict == verdict.BLOCKED


def test_obligation_not_item_bound_is_refused(tmp_path):
    bad = {"model_id": "x", "steps": [
        {"key": "k1", "name": "n", "ordinal": 1, "witness": "w", "consumes": ["a"],
         "obligation": "do the thing once",
         "report_back": {"proof": "p", "where": "w", "how": "h", "when": "t"}}]}
    path = _write(tmp_path, bad)
    with pytest.raises(Halt) as ei:
        step_model.load(path)
    assert ei.value.verdict == verdict.BLOCKED


def test_malformed_json_is_refused(tmp_path):
    p = tmp_path / "model.json"
    p.write_text("{ not json", encoding="utf-8")
    with pytest.raises(Halt) as ei:
        step_model.load(str(p))
    assert ei.value.verdict == verdict.BLOCKED
