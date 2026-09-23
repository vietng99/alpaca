"""M3.9 -- the six shipped formations register, and every gate they name is wired.

Proof test for the Done-when (every shipped formation is registered):

  * all six manifests (solo, builder-verifier, fan-out, bug-loop, nuclear, napalm)
    load via `alpaca.formation.manifest.load` and register through `manifest.registry(root)`
    in BOTH directions -- name -> formation and formation-file -> name;
  * every gate named in every manifest's `gate_map` resolves to a REAL wired instrument,
    a module that imports under `alpaca/gates/` or `alpaca/checklist/`; a manifest that named a
    gate with no instrument would fail this test (the resolver's discrimination is asserted
    so the check is not a tautology);
  * the builder-verifier manifest is the DEFAULT for the verify phase.

The resolver imports the instrument module by its bare name. A missing module (the gate has
no instrument) is a False; a module that exists but is broken re-raises rather than passing.
"""
import importlib
import os

import pytest

from alpaca import paths
from alpaca.formation import manifest


#: the six formations M3.9 ships. Nothing else, and none missing.
SIX = ("solo", "builder-verifier", "fan-out", "bug-loop", "nuclear", "napalm")

#: where a gate name may resolve to an instrument.
INSTRUMENT_PACKAGES = ("alpaca.gates", "alpaca.checklist")


def _root():
    return paths.root()


def _resolves(gate_name: str) -> bool:
    """True when `gate_name` names a real instrument module under one of the instrument
    packages. A module that is simply absent (this package has no such gate) is swallowed and
    the next package tried; a module that exists but fails to import re-raises, so a wired-but-
    broken instrument is never reported as resolving."""
    for pkg in INSTRUMENT_PACKAGES:
        dotted = "%s.%s" % (pkg, gate_name)
        try:
            importlib.import_module(dotted)
            return True
        except ModuleNotFoundError as exc:
            # only the gate module's own absence is a "try the next package"; a missing
            # transitive dependency of an instrument that DOES exist must surface.
            if exc.name == dotted:
                continue
            raise
    return False


# ---------------------------------------------------------------- the resolver discriminates

def test_resolver_is_not_a_tautology():
    # a real instrument resolves; an invented gate name does not. Without this, a gate_map full
    # of nonsense could pass the whole-suite check below by the resolver never saying no.
    assert _resolves("regression_suite") is True
    assert _resolves("closure") is True
    assert _resolves("no_such_gate_deadbeef") is False


# ---------------------------------------------------------------- all six load

def test_each_of_the_six_loads_from_its_file():
    root = _root()
    forms = os.path.join(root, manifest.FORMATIONS_DIR)
    for name in SIX:
        path = os.path.join(forms, "%s.md" % name)
        assert os.path.isfile(path), "missing formation file %s.md" % name
        m = manifest.load(path)
        assert m.name == name, "%s.md declares name %r" % (name, m.name)


# ---------------------------------------------------------------- both directions

def test_all_six_register_in_both_directions():
    root = _root()
    reg = manifest.registry(root)
    forms = os.path.join(root, manifest.FORMATIONS_DIR)
    for name in SIX:
        # name -> formation
        assert name in reg, "%s not registered by name" % name
        m = reg.get(name)
        assert m.name == name
        # formation-file -> name
        path = os.path.join(forms, "%s.md" % name)
        assert reg.name_of(path) == name, "%s does not register file->name" % name


# ---------------------------------------------------------------- every gate is wired

def test_every_named_gate_resolves_to_a_wired_instrument():
    root = _root()
    reg = manifest.registry(root)
    for name in SIX:
        m = reg.get(name)
        assert m.gate_map, "%s declares an empty gate_map" % name
        for boundary, gate in m.gate_map.items():
            assert isinstance(gate, str) and gate, \
                "%s: boundary %r names no gate" % (name, boundary)
            assert _resolves(gate), \
                "%s: boundary %r names gate %r with no wired instrument" % (name, boundary, gate)


# ---------------------------------------------------------------- builder-verifier is the default

def test_builder_verifier_is_the_verify_phase_default():
    root = _root()
    reg = manifest.registry(root)
    # exactly one formation claims the verify phase as its default, and it is builder-verifier.
    claimants = []
    for fname in reg.names():
        m = reg.get(fname)
        default_for = m.raw.get("default_for") or []
        if "verify" in default_for:
            claimants.append(fname)
    assert claimants == ["builder-verifier"], \
        "verify-phase default should be builder-verifier alone, got %r" % claimants
    # and builder-verifier itself declares it
    bv = reg.get("builder-verifier")
    assert "verify" in (bv.raw.get("default_for") or []), \
        "builder-verifier does not declare itself the verify-phase default"
