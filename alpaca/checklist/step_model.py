"""Step-model loading. Ported from the earlier harness gates/row_synthesis.load_step_model.

Adapted for Alpaca: a single free function `load(path)` takes the model's filesystem path.
The earlier harness also witnessed each step key against a separate method pack (path:line) chosen by
the run; that pack-witnessing belongs to a later synthesis pass, so it is not carried
here. What is kept is the load-bearing shape check: every step carries `key, name,
ordinal, witness, consumes, obligation, report_back {proof, where, how, when}`; step keys
are distinct; ordinals form the partition 1..N with no gap (a missing ordinal is a step
quietly dropped from the map of the work ahead); an obligation names its item (a statement
claimed N times without `{item}` is a vacuous universal, not N obligations).

There is no default-on-miss: a missing or empty load-bearing field is refused, never a
default. Refusals raise `alpaca.checklist.Halt` with a verdict-band code.
"""
from __future__ import annotations

import json

from alpaca.checklist import Halt
from alpaca.gates import contract, verdict

_STEP_AXES = ("proof", "where", "how", "when")


def _req(mapping, key, where, want=str):
    """Read a load-bearing field. A missing or empty field is a refusal, never a default."""
    if not isinstance(mapping, dict):
        raise Halt(verdict.BLOCKED, "FIELD-CONTAINER-NOT-OBJECT", where)
    if key not in mapping:
        raise Halt(verdict.BLOCKED, "FIELD-MISSING", "%s.%s" % (where, key))
    val = mapping[key]
    if want is str:
        if not isinstance(val, str) or not val.strip():
            raise Halt(verdict.BLOCKED, "FIELD-EMPTY", "%s.%s" % (where, key))
    elif want is list:
        if not isinstance(val, list) or not val:
            raise Halt(verdict.BLOCKED, "FIELD-EMPTY", "%s.%s" % (where, key))
    elif want is dict:
        if not isinstance(val, dict) or not val:
            raise Halt(verdict.BLOCKED, "FIELD-EMPTY", "%s.%s" % (where, key))
    elif want is int:
        if isinstance(val, bool) or not isinstance(val, int):
            raise Halt(verdict.BLOCKED, "FIELD-NOT-INT", "%s.%s" % (where, key))
    return val


def load(path: str) -> dict:
    """Load and validate one step model. Returns the normalized model on success."""
    with open(path, "rb") as fh:
        raw = fh.read()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as e:
        raise Halt(verdict.BLOCKED, "MODEL-NOT-UTF8", "%s: %s" % (path, e))
    try:
        model = json.loads(text)
    except Exception as e:
        raise Halt(verdict.BLOCKED, "MODEL-MALFORMED-JSON", "%s: %s" % (path, e))
    if not isinstance(model, dict):
        raise Halt(verdict.BLOCKED, "MODEL-NOT-OBJECT", path)

    model_id = _req(model, "model_id", "model")
    steps = _req(model, "steps", "model", list)

    out_steps = []
    keys_seen = {}
    ordinals = []
    for pos in range(len(steps)):
        st = steps[pos]
        where = "model.steps[%d]" % pos
        key = _req(st, "key", where)
        name = _req(st, "name", where)
        ordinal = _req(st, "ordinal", where, int)
        witness = _req(st, "witness", where)
        consumes = _req(st, "consumes", where, list)
        obligation = _req(st, "obligation", where)
        report_back = _req(st, "report_back", where, dict)
        for axis in _STEP_AXES:
            _req(report_back, axis, where + ".report_back")

        if key in keys_seen:
            raise Halt(verdict.BLOCKED, "STEP-KEY-DUPLICATE",
                       "%s repeats key %r first declared at index %d"
                       % (where, key, keys_seen[key]))
        keys_seen[key] = pos
        ordinals.append(ordinal)

        for k in consumes:
            if not isinstance(k, str) or not k.strip():
                raise Halt(verdict.BLOCKED, "CONSUMES-KIND-NOT-STRING",
                           "%s consumes a non-string artifact kind %r" % (where, k))

        if "{item}" not in obligation:
            raise Halt(verdict.BLOCKED, "OBLIGATION-TEMPLATE-NOT-ITEM-BOUND",
                       "%s obligation does not name its item; one statement claimed N times is a "
                       "vacuous universal, not N obligations" % where)

        out_steps.append({"key": key, "name": name, "ordinal": ordinal, "witness": witness,
                          "consumes": list(consumes), "obligation": obligation,
                          "report_back": dict(report_back)})

    if sorted(ordinals) != list(range(1, len(out_steps) + 1)):
        raise Halt(verdict.BLOCKED, "STEP-ORDINAL-NOT-A-PARTITION",
                   "ordinals %s do not form 1..%d with no gap; a missing ordinal is a step "
                   "quietly dropped from the map of the work ahead"
                   % (sorted(ordinals), len(out_steps)))

    return {"model_id": model_id, "model_path": path,
            "model_sha256": contract.sha256_bytes(path), "steps": out_steps}


# ------------------------------------------------------------------------- CLI boundary
def main(argv=None) -> int:
    ap = verdict.make_parser(
        name="step-model-load",
        description="Load and validate a phase step model")
    ap.add_argument("path", help="the step-model JSON to load")
    a = ap.parse_args(argv)
    try:
        model = load(a.path)
    except Halt as h:
        return verdict.emit_verdict("step-model-load", h.verdict, "%s: %s" % (h.code, h.detail))
    return verdict.emit_verdict(
        "step-model-load", verdict.PASS,
        "%s: %d steps" % (model["model_id"], len(model["steps"])),
        evidence=[a.path])


if __name__ == "__main__":
    import sys
    sys.exit(main())
