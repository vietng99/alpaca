#!/usr/bin/env python3
"""Reusable, write-free shipment path policy for package build and cutover apply (M4.15).

Ported UNCHANGED IN PURPOSE from the earlier harness shipment_policy.py and re-based onto Alpaca: the forbidden
set is derived from the harness manifest's memory class (runtime state that must never travel to
another project) plus the host-local settings file, instead of a single hard-coded literal.

The caller owns archive/source enumeration. It MUST pass the complete staged member population to
`prewrite_shipment_apply` before creating an archive, receipt, backup directory, temporary live
file, or any other mutation. This module first materialises and validates the whole population; the
supplied mutation callback is unreachable on malformed or forbidden input. A refusal therefore
happens BEFORE any byte is written.

This is a path-presence policy, not a replacement for canonical-tar, digest, mode, or exact-member
verification. Those checks remain independently mandatory.
"""
from __future__ import annotations

import os
import posixpath
import re


#: The host-local file that is never part of a shipment: the operator's own settings, gitignored
#: and absent on a fresh clone. A static floor kept even when a manifest cannot be read.
HOST_LOCAL_PATHS = frozenset({".claude/settings.local.json"})

#: The default forbidden set when no manifest-derived set is supplied. Kept narrow and named.
FORBIDDEN_SHIPMENT_PATHS = frozenset(HOST_LOCAL_PATHS)

POLICY_TOKEN = "SHIPMENT-FORBIDDEN-PATH"

MANIFEST_NAME = "ALPACA-MANIFEST"


class ShipmentPolicyError(ValueError):
    """A staged population is malformed or contains a path forbidden from shipment.

    When the refusal is a FORBIDDEN local-only path (not a malformed path), the error carries the
    excluded paths on `departed_paths` -- a machine-readable DEPARTURE RECORD. A boundary that drops
    a local-only residue from the managed population must NAME what departed, so the exit is measured
    rather than silent.
    """

    def __init__(self, *args, departed_paths=None):
        super().__init__(*args)
        # Always a concrete tuple (possibly empty), never None, so a caller can act on it without a
        # None-guard: an empty tuple means "no local-only path departed".
        self.departed_paths = tuple(departed_paths or ())


def _memory_paths(root):
    """The memory-class paths named in `<root>/ALPACA-MANIFEST`, canonicalised (trailing slash removed).

    Parsed inline (no `alpaca` import) so this policy stays runnable when copied out of the tree. An
    absent or unreadable manifest yields the empty set: the static host-local floor still holds.
    """
    out = set()
    path = os.path.join(root, MANIFEST_NAME)
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return out
    section = None
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            continue
        if section == "memory":
            out.add(line.rstrip("/"))
    return out


def forbidden_for_root(root):
    """The forbidden shipment set for a project root: its manifest memory class plus the host-local
    settings file. Read from the manifest so the ignore list and the shipment policy never drift."""
    return frozenset(HOST_LOCAL_PATHS | _memory_paths(root))


def canonical_staged_path(raw):
    """Normalise spelling solely for policy comparison; reject unsafe/non-relative paths."""
    if not isinstance(raw, str) or not raw or "\x00" in raw:
        raise ShipmentPolicyError("SHIPMENT-PATH-MALFORMED: expected non-empty path string")
    portable = raw.replace("\\", "/")
    if portable.startswith("/") or re.match(r"^[A-Za-z]:/", portable):
        raise ShipmentPolicyError("SHIPMENT-PATH-MALFORMED: absolute path %r" % raw)
    canonical = posixpath.normpath(portable)
    if canonical in ("", ".", "..") or canonical.startswith("../"):
        raise ShipmentPolicyError("SHIPMENT-PATH-MALFORMED: escaping path %r" % raw)
    return canonical


def _forbidden_hits(canonical, forbidden):
    """Every canonical staged path that is, or lives under, a forbidden path. A forbidden directory
    (a memory path such as `.alpaca`) refuses every member beneath it, not only an exact match."""
    hits = set()
    for cand in canonical:
        for bad in forbidden:
            if cand == bad or cand.startswith(bad + "/"):
                hits.add(cand)
    return sorted(hits)


def validate_staged_paths(paths, forbidden=None):
    """Materialise and validate a complete staged population; return the canonical path tuple.

    `forbidden` is a set of forbidden relpaths (default: the static host-local floor). A staged
    member equal to, or nested under, any forbidden path refuses the WHOLE population.
    """
    forbidden = FORBIDDEN_SHIPMENT_PATHS if forbidden is None else frozenset(forbidden)
    staged = tuple(paths)
    canonical = tuple(canonical_staged_path(path) for path in staged)
    hits = _forbidden_hits(canonical, forbidden)
    if hits:
        raise ShipmentPolicyError(
            "%s: local-only path(s) may not enter package/cutover: %s"
            % (POLICY_TOKEN, ", ".join(hits)),
            departed_paths=hits)
    return canonical


def prewrite_shipment_apply(staged_paths, mutation_callback, forbidden=None):
    """Validate the complete population, then and only then invoke a zero-argument mutation."""
    if not callable(mutation_callback):
        raise TypeError("mutation_callback must be callable")
    validated = validate_staged_paths(staged_paths, forbidden=forbidden)
    return mutation_callback(), validated
