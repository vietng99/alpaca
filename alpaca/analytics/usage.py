"""Explicit, project-local measured usage imports.

The append-only project record is authoritative. The JSON file below it is only a
rebuildable convenience projection. This module never searches provider accounts.
"""
import json
import math
import os
import re

from alpaca import db, paths, util

_SID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_TOKEN_NAMES = ("input", "output", "cache_read", "cache_write_5m", "cache_write_1h")


def _path(root, session):
    if not isinstance(session, str) or not _SID.fullmatch(session):
        raise ValueError("invalid session id")
    return os.path.join(paths.runtime_dir(root), "analytics", "usage", session + ".json")


def _number(value, name, *, integer=False):
    if isinstance(value, bool):
        raise ValueError("%s must be numeric" % name)
    try:
        out = int(value) if integer else float(value)
    except (OverflowError, TypeError, ValueError):
        raise ValueError("%s must be numeric" % name)
    if not math.isfinite(out) or out < 0 or (integer and out != value):
        raise ValueError("%s must be a nonnegative %s" % (name, "integer" if integer else "number"))
    return out


def _tokens(raw):
    if not isinstance(raw, dict):
        raise ValueError("tokens must be an object")
    aliases = {"input": "input", "input_tokens": "input", "output": "output", "output_tokens": "output",
               "cache_read": "cache_read", "cache_read_input_tokens": "cache_read",
               "cache_write_5m": "cache_write_5m", "cache_write_1h": "cache_write_1h"}
    out = {name: None for name in _TOKEN_NAMES}
    seen = set()
    for key, value in raw.items():
        target = aliases.get(key)
        if target is None:
            continue
        if target in seen:
            raise ValueError("tokens repeats %s" % target)
        if value is None and target not in ("input", "output"):
            seen.add(target)
            continue
        out[target] = _number(value, "tokens.%s" % key, integer=True)
        seen.add(target)
    if not {"input", "output"}.issubset(seen) or out["input"] is None or out["output"] is None:
        raise ValueError("tokens must include input and output")
    return out


def _rows(raw):
    rows = raw.get("samples") if isinstance(raw, dict) and "samples" in raw else raw
    if isinstance(rows, dict):
        rows = [rows]
    if not isinstance(rows, list) or not rows:
        raise ValueError("usage file must contain one sample or a nonempty samples list")
    return rows


def _normalise(row, source_pointer):
    if not isinstance(row, dict):
        raise ValueError("usage sample must be an object")
    sample_id = row.get("sample_id")
    provider = row.get("provider")
    model = row.get("model")
    if not all(isinstance(value, str) and value.strip() for value in (sample_id, provider, model)):
        raise ValueError("usage sample requires nonempty sample_id, provider, and model")
    if not isinstance(source_pointer, dict) or not isinstance(source_pointer.get("path"), str) or not isinstance(source_pointer.get("sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", source_pointer["sha256"]):
        raise ValueError("usage sample requires source path and sha256 provenance")
    out = {"sample_id": sample_id, "provider": provider, "model": model,
           "tokens": _tokens(row.get("tokens")), "source": source_pointer}
    if "reported_cost_usd" in row and row["reported_cost_usd"] is not None:
        if row.get("currency", "USD") != "USD":
            raise ValueError("reported_cost_usd requires currency USD")
        out["reported_cost_usd"] = _number(row["reported_cost_usd"], "reported_cost_usd")
        out["currency"] = "USD"
    return out


def _identity(row):
    return {name: row.get(name) for name in ("sample_id", "provider", "model", "tokens", "reported_cost_usd", "currency")}


def _event_samples(conn, session):
    """Fold authoritative import events. Bad event payload means usage is unavailable."""
    out = []
    try:
        rows = conn.execute("SELECT data FROM events WHERE session=? AND kind='usage-import' ORDER BY id", (session,))
        for event in rows:
            data = json.loads(event["data"])
            for row in data["samples"]:
                out.append(_normalise(row, row.get("source") if isinstance(row, dict) else None))
    except (KeyError, TypeError, ValueError):
        return None
    seen = set()
    for row in out:
        if row["sample_id"] in seen:
            return None
        seen.add(row["sample_id"])
    return out


def _projection(root, session, samples):
    """Best-effort rebuildable cache. The event record remains valid if this write is interrupted."""
    target = _path(root, session)
    util.write_text(target, json.dumps({"schema": 1, "session": session, "samples": samples},
                                       ensure_ascii=True, indent=2) + "\n")
    return target


def _source(source):
    source = os.path.abspath(source)
    try:
        with open(source, "rb") as fh:
            raw_bytes = fh.read()
        raw = json.loads(raw_bytes.decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise ValueError("cannot read usage file: %s" % exc)
    return _rows(raw), {"path": source, "sha256": util.sha256_hex(raw_bytes)}


def ingest(root, session, source):
    """Append explicit samples to the SQLite record and refresh a non-authoritative cache."""
    rows, pointer = _source(source)
    incoming = [_normalise(row, pointer) for row in rows]
    if len({row["sample_id"] for row in incoming}) != len(incoming):
        raise ValueError("usage file repeats a sample_id")
    target = _path(root, session)
    conn = db.connect(root)
    try:
        with db.transaction(conn):
            existing = _event_samples(conn, session)
            if existing is None:
                raise ValueError("recorded usage events are invalid")
            known = {row["sample_id"]: row for row in existing}
            added = []
            duplicates = 0
            for row in incoming:
                prior = known.get(row["sample_id"])
                if prior is None:
                    added.append(row)
                elif _identity(prior) == _identity(row):
                    duplicates += 1
                else:
                    raise ValueError("sample_id %s conflicts with recorded content" % row["sample_id"])
            if added:
                if not db.rows(conn, "sessions", "sid=?", (session,)):
                    db.upsert(conn, "sessions", "sid", {"sid": session, "started": util.now_iso(), "cwd": root})
                db.append_event(conn, session=session, actor="usage-import", kind="usage-import", ref=target,
                                data={"source": pointer, "samples": added}, conn_in_txn=True)
        all_samples = _event_samples(conn, session)
    finally:
        conn.close()
    if all_samples is None:
        raise ValueError("recorded usage events are invalid")
    _projection(root, session, all_samples)
    return {"session": session, "inserted": len(added), "duplicates": duplicates, "path": target}


def load(root, session, *, conn=None):
    """Load usage solely from the append-only record, never from its JSON projection."""
    if conn is not None:
        return _event_samples(conn, session)
    conn = db.connect_readonly(root)
    try:
        return _event_samples(conn, session)
    finally:
        conn.close()


def signature(root, session, *, conn=None):
    """Event hashes are the cache key, so a projection write cannot affect analytics truth."""
    own = conn is None
    conn = conn or db.connect_readonly(root)
    try:
        rows = conn.execute("SELECT id,hash FROM events WHERE session=? AND kind='usage-import' ORDER BY id", (session,))
        return [{"id": row["id"], "hash": row["hash"]} for row in rows]
    finally:
        if own:
            conn.close()
