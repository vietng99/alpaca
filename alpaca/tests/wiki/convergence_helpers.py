"""Shared checks of op-008 t-070 (fix plan RC1 and RC3): the live store against a store built from the
record alone, without touching the live one.

`shadow(project, name)` copies the project (its record through the sqlite backup API, its config) to
a sibling directory with no Rune2 store, so a rebuild (`recovery.replay` on an empty store) or an
import (`portable.import_store`) runs there from the same record. `state(root)` is what must be
equal: the accepted digest (architecture.md 10.3), the candidate rows without their clock column,
and the active blocks. `withdrawn_active(root)` lists any block or `current` version a withdrawal
should have taken out and did not (FR-17).
"""
from __future__ import annotations

import os
import shutil
import sqlite3
from pathlib import Path

from native_helpers import store


def shadow(project, name: str) -> str:
    """A copy of `project` beside it: the same record (a consistent sqlite backup) and config, no
    wiki store. An earlier copy of that name is removed first."""
    dst = Path(project).parent / name
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(project, dst, ignore=shutil.ignore_patterns("wiki", "alpaca.db*"))
    src = sqlite3.connect(os.path.join(project, ".alpaca", "alpaca.db"))
    out = sqlite3.connect(str(dst / ".alpaca" / "alpaca.db"))
    try:
        src.backup(out)
    finally:
        src.close()
        out.close()
    return str(dst)


def rebuilt(project, name: str = "rebuild") -> str:
    """A shadow of `project` with its store rebuilt from the record (`recovery.replay` on an empty
    store: every act in record id order, knowledge time from the record)."""
    from alpaca.wiki import recovery
    root = shadow(project, name)
    report = recovery.replay(root)
    assert report["status"] == "ok", report
    return root


def state(root) -> dict:
    from alpaca.wiki.digest import accepted_digest
    with store(root) as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM candidates ORDER BY candidate_id")]
        for r in rows:
            r.pop("updated_at", None)
        anchors = [tuple(r) for r in conn.execute(
            "SELECT candidate_id,ordinal,source_ref,quote_sha256,role FROM candidate_anchors "
            "ORDER BY candidate_id,ordinal")]
        active = sorted(tuple(r) for r in conn.execute(
            "SELECT doc_id,ordinal,block_sha256 FROM blocks WHERE status='active'"))
        return {"digest": accepted_digest(conn), "candidates": rows, "anchors": anchors,
                "active_blocks": active}


def withdrawn_active(root) -> list:
    """Every active block, and every `current` version row, of a source a withdrawal took out."""
    from alpaca.wiki.servable import excluded_source_refs
    with store(root) as conn:
        refs = sorted(excluded_source_refs(conn))
        out = []
        for ref in refs:
            docs = [r[0] for r in conn.execute(
                "SELECT DISTINCT doc_id FROM source_versions WHERE source_ref=?", (ref,))]
            for doc in docs:
                n = conn.execute("SELECT COUNT(*) FROM blocks WHERE doc_id=? AND status='active'",
                                 (doc,)).fetchone()[0]
                if n:
                    out.append(("active-blocks", ref, doc, n))
            if conn.execute("SELECT 1 FROM source_versions WHERE source_ref=? AND status='current'",
                            (ref,)).fetchone():
                out.append(("current-version", ref))
        return out


def caught_up(project, name: str = "caught-up") -> str:
    """A copy of `project` with its live store (no connection is open between two acts), caught up
    with the record by `wiki recover` on a store that is not empty (`recovery.replay`): the events the
    live store has not captured yet land, so what is compared is the live store's own state, not its
    capture lag."""
    from alpaca.wiki import recovery
    dst = Path(project).parent / name
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(project, dst)
    report = recovery.replay(str(dst))
    assert report["status"] == "ok", report
    return str(dst)


def assert_converged(project, name: str = "rebuild", *, catch_up: bool = False) -> dict:
    """The live store equals a rebuild from the record, and nothing withdrawn is active. With
    `catch_up` the live store is compared after a catch-up of its copy (`caught_up`), for a sequence
    that leaves record events uncaptured on purpose."""
    if catch_up:
        project = caught_up(project, name + "-live")
    live = state(project)
    assert withdrawn_active(project) == [], withdrawn_active(project)
    other = state(rebuilt(project, name))
    for key in live:
        assert live[key] == other[key], "live store and rebuild differ in %s" % key
    return live


def roundtrip(project, name: str = "import") -> None:
    """Export the live store, import the export into a fresh store from the same record, export that:
    the two exports are byte-identical (FR-23, architecture.md 10.4)."""
    from alpaca.wiki import portable
    first = Path(project).parent / (name + "-1.jsonl")
    portable.export_store(project, first)
    root = shadow(project, name)
    report = portable.import_store(root, first)
    assert report["status"] == "ok", report
    assert report["refused"] == [], report["refused"]
    second = Path(project).parent / (name + "-2.jsonl")
    portable.export_store(root, second)
    assert second.read_bytes() == first.read_bytes(), "export/import round trip is not byte-identical"
