"""M3.7 -- the formation manifest loader, its registry, and the blind-pair rule.

Proof test for the Done-when (the formation manifest, agent briefs and blind pairs):

  * dropping a manifest file into `formations/` registers the formation in BOTH
    directions -- name -> formation and formation-file -> name -- with no code change;
  * a manifest missing roles, phases, gate map, budget class or output is REFUSED,
    every one of the five checked on its own;
  * the blind-pair rule holds: the verifier role never receives the builder's
    narrative, while a non-blind role receives the payload intact.

Every control asserts the POSITIVE and the NEGATIVE path, so none is a tautological refuser.
"""
import os

import pytest

from alpaca.formation import manifest


GOOD = """---
name: builder-verifier
roles:
  - id: builder
    archetype: eng
    produces: the change and its narrative
  - id: verifier
    archetype: red
    blind: true
    receives: [claim, pointer]
phases: [build, verify]
gate_map:
  build->verify: gate(verify, blind-verifier)
budget_class: standard
output: a verified change carrying an independent verdict
---

# builder + blind verifier

Prose after the front matter is documentation, not part of the manifest.
"""

REQUIRED = ["name", "roles", "phases", "gate_map", "budget_class", "output"]


def _write(dir_path, name, text):
    path = os.path.join(dir_path, name)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


def _drop_key(text, key):
    """Return the manifest text with one top-level YAML key removed."""
    out = []
    skip = False
    for line in text.splitlines(keepends=True):
        stripped = line.rstrip("\n")
        if skip:
            # keep skipping the block-indented lines that belong to the dropped key
            if stripped and not stripped[0].isspace() and not stripped.startswith("---"):
                skip = False
            elif stripped.startswith("---"):
                skip = False
            else:
                continue
        if stripped == "%s:" % key or stripped.startswith("%s:" % key + " ") \
                or stripped.startswith("%s: " % key):
            skip = True
            continue
        out.append(line)
    return "".join(out)


# ---------------------------------------------------------------- load()

def test_load_reads_the_five_fields(tmp_path):
    path = _write(str(tmp_path), "builder-verifier.md", GOOD)
    m = manifest.load(path)
    assert m.name == "builder-verifier"
    assert [r["id"] for r in m.roles] == ["builder", "verifier"]
    assert m.phases == ["build", "verify"]
    assert "build->verify" in m.gate_map
    assert m.budget_class == "standard"
    assert m.output
    assert m.source == path


@pytest.mark.parametrize("missing", ["roles", "phases", "gate_map", "budget_class", "output"])
def test_load_refuses_an_incomplete_manifest(tmp_path, missing):
    # negative: each of the five required fields, dropped one at a time, is refused
    text = _drop_key(GOOD, missing)
    assert ("%s:" % missing) not in text.split("---", 2)[1]
    path = _write(str(tmp_path), "broken.md", text)
    with pytest.raises(manifest.ManifestError) as exc:
        manifest.load(path)
    assert missing in str(exc.value)


def test_load_refuses_a_file_with_no_front_matter(tmp_path):
    path = _write(str(tmp_path), "prose.md", "# just prose\n\nno front matter here\n")
    with pytest.raises(manifest.ManifestError):
        manifest.load(path)


# ---------------------------------------------------------------- registry()

def _formations(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "ALPACA-MANIFEST").write_text("alpaca\n", encoding="utf-8")
    forms = root / "formations"
    forms.mkdir()
    return str(root), str(forms)


def test_registry_registers_in_both_directions(tmp_path):
    root, forms = _formations(tmp_path)
    path = _write(forms, "builder-verifier.md", GOOD)
    reg = manifest.registry(root)
    # forward: name -> formation
    assert "builder-verifier" in reg.names()
    assert reg.get("builder-verifier").budget_class == "standard"
    # reverse: formation-file -> name
    assert reg.name_of(path) == "builder-verifier"


def test_registry_no_code_change_second_file(tmp_path):
    # dropping a SECOND manifest registers it too, with no code change
    root, forms = _formations(tmp_path)
    _write(forms, "builder-verifier.md", GOOD)
    solo = GOOD.replace("name: builder-verifier", "name: solo")
    _write(forms, "solo.md", solo)
    reg = manifest.registry(root)
    assert set(reg.names()) == {"builder-verifier", "solo"}


def test_registry_skips_a_readme_without_front_matter(tmp_path):
    root, forms = _formations(tmp_path)
    _write(forms, "builder-verifier.md", GOOD)
    _write(forms, "README.md", "# formations\n\nDrop a manifest file to add one.\n")
    reg = manifest.registry(root)
    assert reg.names() == ["builder-verifier"]


def test_registry_refuses_an_incomplete_candidate(tmp_path):
    # negative: a file WITH front matter but missing a field is refused, not silently skipped
    root, forms = _formations(tmp_path)
    _write(forms, "broken.md", _drop_key(GOOD, "output"))
    with pytest.raises(manifest.ManifestError):
        manifest.registry(root)


def test_registry_empty_directory(tmp_path):
    root, _ = _formations(tmp_path)
    reg = manifest.registry(root)
    assert reg.names() == []


# ---------------------------------------------------------------- blind pairs

def _payload():
    return {
        "claim": "the change makes gate G pass",
        "pointer": "local:build/out.txt:12",
        "narrative": "I was confident because the refactor looked clean",
        "confidence": "high",
    }


def test_verifier_never_receives_the_builder_narrative(tmp_path):
    path = _write(str(tmp_path), "builder-verifier.md", GOOD)
    m = manifest.load(path)
    assert manifest.is_blind(m, "verifier") is True
    seen = manifest.payload_for(m, "verifier", _payload())
    # the bare claim and its pointer survive; the narrative and confidence do not
    assert seen["claim"] == _payload()["claim"]
    assert seen["pointer"] == _payload()["pointer"]
    assert "narrative" not in seen
    assert "confidence" not in seen


def test_non_blind_role_receives_the_payload_intact(tmp_path):
    # negative control: the strip is not unconditional -- a non-blind role sees everything
    path = _write(str(tmp_path), "builder-verifier.md", GOOD)
    m = manifest.load(path)
    assert manifest.is_blind(m, "builder") is False
    seen = manifest.payload_for(m, "builder", _payload())
    assert seen["narrative"] == _payload()["narrative"]
    assert seen["confidence"] == _payload()["confidence"]


def test_payload_for_unknown_role_is_refused(tmp_path):
    path = _write(str(tmp_path), "builder-verifier.md", GOOD)
    m = manifest.load(path)
    with pytest.raises(manifest.ManifestError):
        manifest.payload_for(m, "ghost", _payload())
