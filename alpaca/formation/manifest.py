"""Formation manifest: the loader, the registry, and the blind-pair rule (M3.7).

It keeps the protocol ahead of the catalogue, and carries the blind-pairs mechanism
(`doctrine/leaves/blind-pairs.md`) and the agent briefs, doctrine slot and packs. The manifest
SHAPE is ported
generically from the earlier harness `doctrine.template.md` DOCTRINE-SLOT (roles, phases, gate_map,
terminal), with the DV specifics dropped: no seam ops, no domain vocabulary, no RTL skin.

A formation is ONE file. This module never invents a per-formation protocol; it validates a
file against a fixed schema and hands the result to the two consumers named in M3.7's
Interfaces -- the dispatch protocol (M3.8) and the doctrine registry (M4.14).

The schema (M3.7 Step 3): a manifest declares
  * `name`        -- the formation's id, unique in the registry;
  * `roles`       -- the members, each a mapping with an `id`; a role may be `blind`;
  * `phases`      -- the ordered phases this formation MAY run;
  * `gate_map`    -- each phase boundary bound to a gate;
  * `budget_class`-- the resource class it declares up front;
  * `output`      -- the deliverable shape.
A manifest missing any of these is REFUSED (`ManifestError`); absence blocks, never guesses
(the same non-vacuity ethos the doctrine boot gate holds).

The blind-pair rule (KEEP row 12): independence engineered against shared bias -- the verifier
role never receives the builder's narrative. `payload_for` strips the narrative fields from
anything delivered to a `blind` role, so a bare claim + pointer is all a blind verifier sees;
`is_blind` reports which roles that covers.

HONEST LIMITS: this module reads and validates a file; it does not run a formation. Whether a
role's process actually stays blind at runtime is the dispatch protocol's obligation (M3.8).
The schema is parsed from YAML front matter delimited by two `---` lines, the same front-matter
shape the agent briefs under `agents/` carry.
"""
from __future__ import annotations

import glob
import os
from dataclasses import dataclass
from typing import Any

import yaml

from alpaca import paths

#: the five fields M3.7's Done-when names, plus the name a registry keys on.
REQUIRED = ("name", "roles", "phases", "gate_map", "budget_class", "output")

#: fields a blind pair strips before a payload reaches a blind role: the author's framing.
#: A blind verifier sees the bare claim and its pointer, never the narrative that produced them.
BLIND_STRIP = ("narrative", "confidence", "reasoning", "story")

#: the directory, under the project root, one manifest file per formation lives in.
FORMATIONS_DIR = "formations"


class ManifestError(ValueError):
    """A manifest that cannot be admitted: no front matter, a missing required field, a role
    with no id, or a payload addressed to a role the manifest does not declare. Raised before
    anything downstream trusts the file, so a broken formation never registers."""


@dataclass(frozen=True)
class Manifest:
    """One loaded, validated formation. `source` is the file it was read from, so the registry
    can go both directions: name -> formation (`registry.get`) and file -> name
    (`registry.name_of`)."""
    name: str
    roles: list
    phases: list
    gate_map: dict
    budget_class: str
    output: str
    source: str
    raw: dict

    def role(self, role_id: str) -> dict:
        for r in self.roles:
            if r.get("id") == role_id:
                return r
        raise ManifestError("formation %r declares no role %r" % (self.name, role_id))


def _front_matter(text: str) -> dict:
    """Parse the YAML block delimited by the first two `---` lines. A file with no such block is
    not a manifest candidate: return None so the registry can skip a README or plain prose."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    body = []
    for line in lines[1:]:
        if line.strip() == "---":
            data = yaml.safe_load("\n".join(body))
            return data if isinstance(data, dict) else {}
        body.append(line)
    return None  # an unterminated block is not a front matter block


def _validate(data: dict, source: str) -> Manifest:
    missing = [k for k in REQUIRED if k not in data or data[k] in (None, "", [], {})]
    if missing:
        raise ManifestError(
            "manifest %s is refused: missing %s" % (source, ", ".join(sorted(missing))))
    roles = data["roles"]
    if not isinstance(roles, list) or not all(isinstance(r, dict) for r in roles):
        raise ManifestError("manifest %s: roles must be a list of mappings" % source)
    for r in roles:
        if not r.get("id"):
            raise ManifestError("manifest %s: every role needs an id" % source)
    ids = [r["id"] for r in roles]
    if len(ids) != len(set(ids)):
        raise ManifestError("manifest %s: role ids must be unique" % source)
    phases = data["phases"]
    if not isinstance(phases, list) or not phases:
        raise ManifestError("manifest %s: phases must be a non-empty list" % source)
    gate_map = data["gate_map"]
    if not isinstance(gate_map, dict) or not gate_map:
        raise ManifestError("manifest %s: gate_map must map phase boundaries to gates" % source)
    return Manifest(
        name=str(data["name"]),
        roles=roles,
        phases=list(phases),
        gate_map=dict(gate_map),
        budget_class=str(data["budget_class"]),
        output=str(data["output"]),
        source=source,
        raw=dict(data),
    )


def load(path: str) -> Manifest:
    """Read and validate ONE manifest file. Refuses a file with no front matter or a missing
    required field. Returns a frozen `Manifest`."""
    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read()
    data = _front_matter(text)
    if data is None:
        raise ManifestError("manifest %s has no YAML front matter" % path)
    return _validate(data, path)


class Registry:
    """The loaded formations, keyed BOTH directions. `get(name)` and `names()` are the
    name -> formation side the dispatch protocol reads; `name_of(path)` is the file -> name
    side the doctrine registry reads. Dropping a manifest file into `formations/` populates both
    with no code change (D7)."""

    def __init__(self, by_name: dict, by_path: dict):
        self._by_name = by_name
        self._by_path = by_path

    def names(self) -> list:
        return sorted(self._by_name)

    def get(self, name: str) -> Manifest:
        if name not in self._by_name:
            raise ManifestError("no formation named %r is registered" % name)
        return self._by_name[name]

    def name_of(self, path: str) -> str:
        key = os.path.abspath(path)
        if key not in self._by_path:
            raise ManifestError("no formation registered from %s" % path)
        return self._by_path[key]

    def __contains__(self, name: str) -> bool:
        return name in self._by_name

    def __iter__(self):
        return iter(self.names())

    def __len__(self) -> int:
        return len(self._by_name)


def registry(root: str | None = None) -> Registry:
    """Scan `<root>/formations/*.md` and register every manifest in both directions. A file with
    no front matter (a README, plain prose) is skipped, not refused; a file WITH front matter but
    missing a required field is REFUSED so a broken formation never registers silently. `root`
    defaults to the discovered project root."""
    base = root or paths.root()
    forms = os.path.join(base, FORMATIONS_DIR)
    by_name: dict = {}
    by_path: dict = {}
    if not os.path.isdir(forms):
        return Registry(by_name, by_path)
    for path in sorted(glob.glob(os.path.join(forms, "*.md"))):
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
        data = _front_matter(text)
        if data is None:
            continue  # not a manifest candidate (README, documentation)
        m = _validate(data, path)
        if m.name in by_name:
            raise ManifestError(
                "two formations claim the name %r: %s and %s"
                % (m.name, by_name[m.name].source, path))
        by_name[m.name] = m
        by_path[os.path.abspath(path)] = m.name
    return Registry(by_name, by_path)


# ------------------------------------------------------------------ blind pairs

def is_blind(m: Manifest, role_id: str) -> bool:
    """True when this role is a blind verifier -- one that must not inherit the builder's
    framing. `role()` refuses a role the manifest does not declare."""
    return bool(m.role(role_id).get("blind"))


def payload_for(m: Manifest, role_id: str, payload: dict) -> dict:
    """The payload a role actually receives. For a `blind` role every narrative field
    (`BLIND_STRIP`) is removed, so the bare claim and its pointer are all that survive; for a
    non-blind role the payload passes through intact. Refuses an unknown role."""
    role = m.role(role_id)  # raises ManifestError for an undeclared role
    if not role.get("blind"):
        return dict(payload)
    return {k: v for k, v in payload.items() if k not in BLIND_STRIP}
