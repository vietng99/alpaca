"""engine.compartment - the data-access-layer routing predicate (oracle-safety.5 + oracle-safety.6).

TWO dumb, reusable filters that live at the substrate boundary so they are applied ONCE and can
never be re-implemented (and thus diverge) per query:

os.5 DOMAIN COMPARTMENT - `domain_filter(sql, domain, all_domains)` appends the compartment predicate
  to a candidate SELECT exactly once. `plan.classify` picks ONE target domain; the LLM may choose the
  domain but cannot leak past this WHERE clause. `--all-domains` is the sole widener.

os.6 PRIVATE ROUTING has two independent fail-closed barriers:
  (1) WRITE routing: a `private:true` record must live under the local-only `wiki/private/` partition;
      `assert_private_path` refuses the write otherwise (the transaction aborts).
  (2) PUSH routing requires `private=0` and `privacy_scanned=1`; NULL or unscanned rows stay local.

The engine/CLI/hook wiring (threading `--all-domains`, calling `assert_private_path` in absorb,
installing the git pre-push hook) is done by the orchestrator; these functions are pure and are
exercised directly by the tests.
"""
from __future__ import annotations

from pathlib import PurePosixPath
import re

DOMAINS = ("embedded", "career", "general", "personal")
DEFAULT_DOMAIN = "general"
PRIVATE_PARTITION = "wiki/private"
_SQL_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


# =========================================================================== os.5 domain compartment

def domain_filter(sql: str, domain: str | None, all_domains: bool = False) -> tuple[str, dict]:
    """Append the compartment predicate to a candidate SELECT - ONCE, at the data layer.

    Returns (wrapped_sql, extra_params). When `all_domains` is set (the `--all-domains` widener) or
    no domain is inferred, the SQL is returned untouched. Otherwise the incoming query is wrapped as a
    subquery filtered on its `domain` column, so it composes with ANY inner WHERE/ORDER/LIMIT without
    re-implementing the predicate. The candidate rows MUST expose a `domain` column (blocks/docs do).
    """
    if all_domains:
        return sql, {}
    domain = domain or DEFAULT_DOMAIN
    wrapped = f"SELECT * FROM (\n{sql}\n) AS _compartment WHERE _compartment.domain = :__domain"
    return wrapped, {"__domain": domain}


def run_compartmented(db, sql: str, params: dict, domain: str | None,
                      all_domains: bool = False) -> list:
    """Execute a candidate SELECT through `domain_filter` - the single compartmented query wrapper."""
    wrapped, extra = domain_filter(sql, domain, all_domains)
    merged = dict(params or {})
    merged.update(extra)
    return db.conn.execute(wrapped, merged).fetchall()


# =========================================================================== os.6 private routing

class PrivatePathViolation(Exception):
    """A private:true record was routed outside the local-only wiki/private/ partition."""


def _under_partition(path: str) -> bool:
    raw = str(path).replace("\\", "/")
    if not raw or "\x00" in raw:
        return False
    candidate = PurePosixPath(raw)
    if candidate.is_absolute():
        return False
    parts = candidate.parts
    target = PurePosixPath(PRIVATE_PARTITION).parts
    if len(parts) < len(target) or ".." in parts:
        return False
    return parts[:len(target)] == target


def is_private_path(path: str) -> bool:
    """Return True only for a canonical vault-relative path under wiki/private."""
    return _under_partition(path)


def assert_private_path(private, path: str) -> None:
    """WRITE barrier: a private:true doc/block MUST live under wiki/private/, else refuse the write.

    Fail-closed direction that matters: private content escaping its partition is the leak. Raising
    here aborts the enclosing Writer.transaction() so nothing is persisted.
    """
    if _is_true(private) and not _under_partition(path or ""):
        raise PrivatePathViolation(
            f"private:true record '{path}' is outside the '{PRIVATE_PARTITION}/' partition; "
            "refusing the write (os.6 write-routing barrier)")


def is_pushable(private, privacy_scanned) -> bool:
    """PUSH barrier: only explicitly public, trusted-scanned rows are pushable.

    A NULL, unknown, private, or unscanned record is never pushable.
    """
    scan_complete = privacy_scanned is True or (
        isinstance(privacy_scanned, int)
        and not isinstance(privacy_scanned, bool)
        and privacy_scanned == 1
    )
    return scan_complete and not _is_true(private)


def pushable_where_clause(column: str = "private",
                          scanned_column: str = "privacy_scanned") -> str:
    """Frozen outbound SQL predicate, fail-closed on privacy and scan state."""
    if not _SQL_IDENTIFIER.fullmatch(column) or not _SQL_IDENTIFIER.fullmatch(scanned_column):
        raise ValueError("unsafe privacy predicate identifier")
    return (
        f"COALESCE({column}, 1) = 0 AND "
        f"COALESCE({scanned_column}, 0) = 1"
    )


def scan_push_payload(paths) -> list[str]:
    """PUSH barrier for a git pre-push hook: the offending private-partition paths in a payload.

    Returns the staged paths that live under wiki/private/ (which must never leave the machine). A
    non-empty result means the push must be refused, naming the rule.
    """
    return sorted(p for p in paths if _under_partition(p))


def _is_true(v) -> bool:
    if v is None:
        return True
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v != 0
    if isinstance(v, str):
        return True
    return True


# =========================================================================== Alpaca-native audience filter
# M2.12 interface (compartment.filter): the single data-layer visibility filter consumed by the
# ingest and the oracle/extract. It reuses the fail-closed private-routing logic above so no caller
# re-implements it. A row whose visibility label is missing or null FOLDS TO PRIVATE (fail-closed):
# the only way out is an explicit, scanned public label. Requesting any non-public audience returns
# every row unchanged.

PUBLIC = "public"
PRIVATE = "private"


def normalize_label(label) -> str:
    """Fold a row's visibility label to 'public' or 'private'. A missing/null/unknown label is
    private (fail-closed): only an explicit public marking clears the fence."""
    if label is None:
        return PRIVATE
    s = str(label).strip().lower()
    if s in ("public", "shareable", "shared", "open"):
        return PUBLIC
    return PRIVATE


def _row_private_flag(row: dict):
    """The row's private flag: an explicit non-null 'private' key wins, else it is derived from a
    'label' key, else None so is_pushable folds it to private."""
    if "private" in row and row.get("private") is not None:
        return row["private"]
    if "label" in row:
        label = row.get("label")
        if label is None:
            return None
        return 0 if normalize_label(label) == PUBLIC else 1
    return None


def row_is_public(row: dict) -> bool:
    """A row is public only when it is explicitly non-private AND privacy-scanned (fail-closed).
    A missing/null label or an unscanned public label is not public."""
    return is_pushable(_row_private_flag(row), row.get("privacy_scanned"))


def filter(rows, label):
    """Keep only the rows visible to the requested audience `label`, at the data layer, ONCE.

    Requesting 'public' yields only rows that are explicitly public AND scanned; a row whose own
    visibility label is missing or null folds to private and is dropped. Any other audience
    (private / local / all) returns every row unchanged. `rows` is any iterable of dict-like rows
    exposing an optional 'private'/'label' and 'privacy_scanned'."""
    if normalize_label(label) != PUBLIC:
        return list(rows)
    return [r for r in rows if row_is_public(r)]
