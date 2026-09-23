"""project.yaml schema, the skin adapter seam, and the eight-op map (M1.8).

`project.yaml` is the seam that tells Alpaca what build, test, run, lint and drift mean for
this project without a plugin document. This module validates that
file against its required key set and produces a clear error per missing or malformed
key. It is consumed by the doors (M1.15), the fidelity instrument (M1.12) and `alpaca doctor`.

The oracle taxonomy is DATA here, never code: the schema checks that the
oracle-class list is present and non-empty, never that it holds a particular member.

The eight seam ops of the earlier harness map onto the file: preflight, build, run and
test are commands; signature is the fingerprint command and is_drift its frozen expected
value; resource_class and boundary_check are entries. `seam_map` returns that mapping.

The skin adapter seam (`skin_adapter`) is the declared mapping from a project's artifact
shapes to a step model and a register. It is declared, never guessed: a malformed
declaration is refused. Alpaca ships one worked generic example in `project.yaml`; the
domain-specific instance is out of scope for M1-M4.
"""
from __future__ import annotations

import sys

from alpaca.gates import verdict

INSTRUMENT = "project-schema"

#: commands the seam must name (build, test, lint, run).
REQUIRED_COMMANDS = ("build", "test", "lint", "run")
#: paths the seam must name (product tree, artifacts, spec dir).
REQUIRED_PATHS = ("product_tree", "artifacts", "spec_dir")
#: the fingerprint is a command plus its frozen expected value (halt-on-drift).
REQUIRED_FINGERPRINT = ("command", "expected")
#: the eight TD seam ops, mapped onto the file by `seam_map`.
SEAM_OPS = ("preflight", "build", "run", "test", "signature", "is_drift",
            "resource_class", "boundary_check")
#: fields every declared skin adapter carries.
_ADAPTER_FIELDS = ("artifact_shape", "key_column", "step_model", "register")


def _nonempty_str(val) -> bool:
    return isinstance(val, str) and bool(val.strip())


def validate(doc) -> tuple:
    """Validate a loaded project.yaml document. Returns (ok, errors).

    `errors` is a list of {"key": ..., "detail": ...}, one per missing or malformed
    required key. `ok` is True only when the list is empty. A missing key and a present
    but empty (malformed) key are refused the same way; the schema never defaults.
    """
    errors = []

    def err(key, detail):
        errors.append({"key": key, "detail": detail})

    if not isinstance(doc, dict):
        return False, [{"key": "<document>", "detail": "project.yaml must be a mapping"}]

    # name, project id, tier: non-empty strings.
    for key in ("name", "project_id", "tier"):
        if not _nonempty_str(doc.get(key)):
            err(key, "missing or empty")

    # commands: a mapping carrying build, test, lint, run.
    cmds = doc.get("commands")
    if not isinstance(cmds, dict):
        err("commands", "missing or not a mapping of build/test/lint/run")
    else:
        for c in REQUIRED_COMMANDS:
            if not _nonempty_str(cmds.get(c)):
                err("commands.%s" % c, "missing or empty command")

    # paths: product tree, artifacts, spec dir.
    paths = doc.get("paths")
    if not isinstance(paths, dict):
        err("paths", "missing or not a mapping of product_tree/artifacts/spec_dir")
    else:
        for p in REQUIRED_PATHS:
            if not _nonempty_str(paths.get(p)):
                err("paths.%s" % p, "missing or empty path")

    # phases used: a non-empty list, or a mapping carrying a non-empty 'default' list.
    if not _phases_ok(doc.get("phases")):
        err("phases", "missing or empty; need a non-empty list or a 'default' list")

    # oracle classes: a non-empty taxonomy (data, not a fixed set).
    oc = doc.get("oracle_classes")
    if not isinstance(oc, (list, dict)) or not oc:
        err("oracle_classes", "missing or empty; the oracle taxonomy is data")

    # forbidden literals: a list (may be empty; literal_guard also reads style/banned.txt).
    if not isinstance(doc.get("forbidden_literals"), list):
        err("forbidden_literals", "missing or not a list")

    # resource class: a non-empty string entry.
    if not _nonempty_str(doc.get("resource_class")):
        err("resource_class", "missing or empty")

    # boundary rules: a non-empty mapping.
    br = doc.get("boundary_rules")
    if not isinstance(br, dict) or not br:
        err("boundary_rules", "missing or empty mapping")

    # non-adoptions: a non-empty mapping.
    na = doc.get("non_adoptions")
    if not isinstance(na, dict) or not na:
        err("non_adoptions", "missing or empty mapping")

    # fingerprint: a command plus its frozen expected value.
    fp = doc.get("fingerprint")
    if not isinstance(fp, dict):
        err("fingerprint", "missing; need command + expected (frozen value)")
    else:
        for f in REQUIRED_FINGERPRINT:
            if not _nonempty_str(fp.get(f)):
                err("fingerprint.%s" % f, "missing or empty")

    return (not errors), errors


def _phases_ok(phases) -> bool:
    if isinstance(phases, list):
        return bool(phases) and all(_nonempty_str(x) for x in phases)
    if isinstance(phases, dict):
        default = phases.get("default")
        return isinstance(default, list) and bool(default) and all(
            _nonempty_str(x) for x in default)
    return False


def seam_map(doc) -> dict:
    """Map the eight seam ops onto project.yaml.

    preflight, build, run and test are commands; signature is the fingerprint command
    and is_drift its frozen expected value; resource_class and boundary_check are entries.
    Values are read straight from the file; a missing one comes back as None so a caller
    can see the gap rather than a guessed default.
    """
    doc = doc if isinstance(doc, dict) else {}
    cmds = doc.get("commands") or {}
    fp = doc.get("fingerprint") or {}
    return {
        "preflight": cmds.get("preflight"),
        "build": cmds.get("build"),
        "run": cmds.get("run"),
        "test": cmds.get("test"),
        "signature": fp.get("command"),
        "is_drift": fp.get("expected"),
        "resource_class": doc.get("resource_class"),
        "boundary_check": doc.get("boundary_rules"),
    }


def skin_adapter(doc) -> list:
    """The skin adapter seam: the declared mapping from a project's artifact shapes to a
    step model and a register. Returns the normalized adapter list.

    The mapping is declared, never guessed. A missing `skin.adapters` section, an empty
    list, or an adapter missing any of artifact_shape / key_column / step_model / register
    raises ValueError, so a project cannot silently run with no adapter at all.
    """
    skin = doc.get("skin") if isinstance(doc, dict) else None
    if not isinstance(skin, dict):
        raise ValueError("skin: missing skin adapter section")
    adapters = skin.get("adapters")
    if not isinstance(adapters, list) or not adapters:
        raise ValueError("skin.adapters: missing or empty")
    out = []
    for i, ad in enumerate(adapters):
        if not isinstance(ad, dict):
            raise ValueError("skin.adapters[%d]: not a mapping" % i)
        norm = {}
        for f in _ADAPTER_FIELDS:
            if not _nonempty_str(ad.get(f)):
                raise ValueError("skin.adapters[%d].%s: missing or empty" % (i, f))
            norm[f] = ad[f]
        out.append(norm)
    return out


# ------------------------------------------------------------------------- CLI boundary
def main(argv=None) -> int:
    ap = verdict.make_parser(
        name=INSTRUMENT,
        description="Validate project.yaml against its required key set")
    ap.add_argument("path", nargs="?", default=None,
                    help="path to project.yaml (default: the discovered project root)")
    a = ap.parse_args(argv)

    import yaml
    if a.path:
        with open(a.path, encoding="utf-8") as fh:
            doc = yaml.safe_load(fh) or {}
    else:
        from alpaca import paths, project
        doc = project.load(paths.root())

    ok, errors = validate(doc)
    if ok:
        return verdict.emit_verdict(
            INSTRUMENT, verdict.PASS, "all required keys present and well-formed")
    return verdict.emit_verdict(
        INSTRUMENT, verdict.FAIL, "%d key problem(s)" % len(errors),
        evidence=["%s: %s" % (e["key"], e["detail"]) for e in errors])


if __name__ == "__main__":
    sys.exit(main())
