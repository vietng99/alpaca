"""contract.py - shared canonicalisation, hashing, pointer and fold primitives.

Ported from the earlier harness gates/contract.py and adapted for Alpaca. This module exists so the
canonicalisation function, the raw-byte hashing primitive, the pointer existence check and
the verdict fold are defined EXACTLY ONCE and referenced everywhere else.

It deliberately does NOT define the verdict<->exit-code mapping. That mapping lives in
`verdict.py`, the single definition for this tree; writing the numbers next to the verdict
names again here would be a restatement of the contract, not a reference to it. `worst`
folds through verdict.py's own codes and its precedence, never a private copy.
"""
from __future__ import annotations

import hashlib
import os

from alpaca import util
from alpaca.gates import verdict

# Which verdict wins when several are produced in one run. BLOCKED dominates: if the
# instrument could not adjudicate, nothing else it says is load-bearing. FAIL beats PAUSED: a
# broken partition is a defect now, not a decision owed. PASS is the weakest, reached only when
# no other verdict was raised. The names are referenced from verdict.py, never restated.
_PRECEDENCE = [verdict.BLOCKED, verdict.FAIL, verdict.PAUSED, verdict.PASS]


class ContractError(Exception):
    """Raised for any condition that must surface rather than launder into a pass."""

    def __init__(self, reason_code: str, detail: str = ""):
        super().__init__("%s: %s" % (reason_code, detail))
        self.reason_code = reason_code
        self.detail = detail


def canonical(obj) -> str:
    """The one canonical string form of a JSON-able object: sorted keys, tight separators,
    UTF-8 text preserved. Two dicts that differ only in key order canonicalise identically, so
    a signature or a content hash over this string is stable."""
    return util.canonical_json(obj)


def sha256_bytes(path: str) -> str:
    """sha256 over the EXACT bytes on disk at `path`. No decode, no rstrip, no row filter, so
    the hash is over what is really there and cannot drift with a re-encoding."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(1 << 16)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def resolve_pointer(root: str, pointer: str) -> str:
    """Resolve a proof pointer of the form `local:<relpath>` or `remote:<opaque>`.

    A `local:` pointer must be a relative path under `root` that actually exists on disk; the
    resolved absolute path is returned. A recorded-but-unresolvable pointer is the reproduced
    defect this refuses, so a missing file raises. A `remote:` pointer is an opaque reference
    that cannot be existence-checked here; its opaque body is returned unchanged.
    """
    if not isinstance(pointer, str) or ":" not in pointer:
        raise ContractError("EVIDENCE-POINTER-MALFORMED", repr(pointer))
    scheme, _, rest = pointer.partition(":")
    rest = rest.strip()
    if scheme == "remote":
        if not rest:
            raise ContractError("EVIDENCE-POINTER-EMPTY", pointer)
        return rest
    if scheme == "local":
        rel = rest.replace("\\", "/")
        if not rel:
            raise ContractError("EVIDENCE-POINTER-EMPTY", pointer)
        if os.path.isabs(rel):
            raise ContractError("EVIDENCE-POINTER-NOT-RELATIVE", pointer)
        if ".." in rel.split("/"):
            raise ContractError("EVIDENCE-POINTER-ESCAPES-ROOT", pointer)
        full = os.path.join(root, rel.replace("/", os.sep))
        if not os.path.isfile(full):
            raise ContractError("EVIDENCE-POINTER-UNRESOLVED", pointer)
        return os.path.abspath(full)
    raise ContractError("EVIDENCE-POINTER-SCHEME", pointer)


def worst(codes: list) -> int:
    """Fold a list of verdict-band codes through the precedence BLOCKED > FAIL > PAUSED > PASS.

    An empty set is a vacuously-true universal; it never passes, so it folds to BLOCKED. A code
    outside the verdict band cannot be folded and raises, rather than being silently dropped.
    """
    cs = list(codes)
    if not cs:
        return verdict.BLOCKED
    for c in cs:
        if c not in verdict.VERDICT_BAND:
            raise ContractError("VERDICT-CODE-UNKNOWN", repr(c))
    for c in _PRECEDENCE:
        if c in cs:
            return c
    return verdict.PASS
