"""One immutable obligation digest before and after persistence.

Status and maturity tags are projections of verdict events, not frozen input.
The stable obligation ID already binds its source artifact and item identity.
"""
from alpaca import util


def content_hash(row):
    fields = ("id", "kind", "op", "phase", "step", "statement", "proof", "how", "why",
              "session", "operator", "supersedes")
    view = {key: row.get(key) for key in fields}
    for canonical, staged in (("where_", "where"), ("when_", "when")):
        view[canonical] = row.get(canonical, row.get(staged, "")) or ""
    for key in ("how", "why"):
        view[key] = view[key] or ""
    return util.sha256_hex("alpaca-obligation/v1\n" + util.canonical_json(view))
