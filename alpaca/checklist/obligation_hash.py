"""One immutable obligation digest before and after persistence.

Status and maturity tags are projections of verdict events, not frozen input.
The stable obligation ID already binds its source artifact and item identity.
"""
from alpaca import util

#: the tag mixed into every row's content hash; a record carried from an earlier harness names
#: its own tag for the rows it carries (alpaca/lineage.py).
TAG = "alpaca-obligation/v1"


def content_hash(row, tag=None):
    fields = ("id", "kind", "op", "phase", "step", "statement", "proof", "how", "why",
              "session", "operator", "supersedes")
    view = {key: row.get(key) for key in fields}
    for canonical, staged in (("where_", "where"), ("when_", "when")):
        view[canonical] = row.get(canonical, row.get(staged, "")) or ""
    for key in ("how", "why"):
        view[key] = view[key] or ""
    return util.sha256_hex((tag or TAG) + "\n" + util.canonical_json(view))
