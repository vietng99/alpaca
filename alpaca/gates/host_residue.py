"""host_residue: a read-only MEASUREMENT of the local-only host residue (M4.13).

Ported from the earlier harness `host_residue.py`. The shipment refuses to manage
the local-only host artifact `.claude/settings.local.json` (it is host-local operator state, never
overwritten by an install). But refusal-to-MANAGE must not become refusal-to-MEASURE: before this
instrument existed nothing opened the live file to look at its permission allow-list, so a
dangerous grant sitting on the host (for example `Bash(rm -rf *)`) departed the measured
population with no record. This closes that measurement gap.

What it does (read-only; it never writes, moves, or removes any host file): read the live
`.claude/settings.local.json` allow-list under a given root, enumerate its grants, and NAME any
that match the dangerous-grant markers. `alpaca doctor` (M4.13) folds a named dangerous grant in as a
WARNING, not an error: this instrument MEASURES and SURFACES the residue, it makes no claim that a
grant it names is reachable or exploitable from inside the harness. The reachability leg is not
asserted here; the cure for the gap is measurement plus honest surfacing, not an exploit claim.

Rename-safe: the residue path is joined to the discovered root at runtime; no absolute path and no
harness folder name is written here.
"""
from __future__ import annotations

import json
import os

#: the local-only host artifact the shipment refuses to manage, and therefore -- until this
#: instrument -- never measured. Relative to the project root.
LOCAL_ONLY_RESIDUE = ".claude/settings.local.json"

#: grant substrings that make a residue allow-entry dangerous. Substring match on the lower-cased
#: raw grant string, so `Bash(rm -rf *)` is caught by the `rm -rf` marker. NAMED, never executed.
DANGEROUS_GRANT_MARKERS = ("rm -rf", "rm -fr", "sudo", "curl ", "wget ", ":(){", "mkfs")


def residue_path(root) -> str:
    """The absolute path of the live residue under a project root. The one place the residue name
    is joined to a root, so the doctor and any caller spell it one way."""
    base = os.path.abspath(root) if root else os.getcwd()
    return os.path.join(base, *LOCAL_ONLY_RESIDUE.split("/"))


def read_residue_allowlist(root=None):
    """Read the live residue and return its `permissions.allow` list.

    Returns (allow_list, note). `allow_list` is None when the residue could not be measured
    (absent / unreadable / malformed) and `note` says why; otherwise `allow_list` is the list of
    grant strings the host has granted itself (possibly empty)."""
    path = residue_path(root)
    if not os.path.isfile(path):
        return None, "no residue at %s (nothing to measure)" % path
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError) as e:
        return None, "residue at %s is unreadable/malformed: %s" % (path, e)
    permissions = doc.get("permissions")
    if not isinstance(permissions, dict):
        return [], "residue has no permissions object"
    allow = permissions.get("allow")
    if allow is None:
        return [], "residue permissions carry no allow-list"
    if not isinstance(allow, list):
        return None, "residue permissions.allow is not a list"
    return [str(g) for g in allow], "measured %d allow-grant(s)" % len(allow)


def dangerous_grants(allow_list):
    """The subset of an allow-list that matches a dangerous-grant marker (NAMED, never run)."""
    out = []
    for grant in allow_list or ():
        low = str(grant).lower()
        if any(marker in low for marker in DANGEROUS_GRANT_MARKERS):
            out.append(grant)
    return out


def measure(root=None) -> dict:
    """Measure the residue under `root`. Returns a dict {measured, allow, dangerous, note}. A
    read-only measurement: `measured` is False when the residue could not be read at all."""
    allow, note = read_residue_allowlist(root)
    if allow is None:
        return {"measured": False, "allow": [], "dangerous": [], "note": note}
    return {"measured": True, "allow": allow, "dangerous": dangerous_grants(allow), "note": note}
