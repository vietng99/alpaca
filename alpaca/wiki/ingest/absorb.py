"""ingest.absorb - the write path (§4). ONE of the two writers; the answer path never writes.

Curator-gated, incremental, atomic all-or-nothing. Orchestrates:
hashgate -> segment+anchor -> contextual enrich -> deterministic-first extract (llm fallback
quarantined) -> set-returning resolve -> agentic ENCODE (reconcile+place) -> incremental upsert +
index delta -> chained ingest_event -> events.jsonl. Heavy consolidation is deferred to Dream.

Alpaca vendoring note (M2.14): the upstream write path also side-writes the vector index
(store.vec) and re-tiers on ingest (engine.tiering). Both are members of the ranking surface
the Alpaca read-door guard (M2.9) fences off from every module except the retrieval door, and
vectors are off by default (M2.11 keeps meta.retrieval_profile at NARROW). So this vendored copy
carries the write path WITHOUT those two ranking side-writes; the graph, edges, echo-provenance,
shelf-life and retire-not-delete behaviour are byte-faithful. When a project turns vectors on, the
vector-write and on-ingest re-tier land behind the write door, not by a second import of the
guarded surface here.
"""
from __future__ import annotations

import os
from pathlib import Path
import stat

from ..clock import Clock, now_iso
from ..config import Config
from ..providers.registry import Providers
from ..store.db import DB
from ..store.ledger import Ledger
from ..store.write import Writer
from . import extract as ex
from .encode import encode_candidate
from .enrich import context_header
from .firewall import scan as fw_scan, escalate_sensitivity   # os.4 advisory marking tripwire (Fork B)
from .hashgate import doc_changed, block_changed              # ADR-042 skip-if-unchanged (doc+block)
from .resolve import ensure_node
from .segment import segment
from .shelf_life import parse_shelf_life, stamp_edges
from .shrink import assess_shrink, shrink_floor_of            # pc.5 body-shrink sanity gate
from ..engine.compartment import is_private_path
from ..engine.echo import (EchoIndex, ensure_schema as echo_ensure_schema,
                           detect_echo, record_provenance)    # os.2 echo-provenance capture at absorb


_SOURCE_DIR_FLAGS = (os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                     | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))
_SOURCE_FILE_FLAGS = (os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                      | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0))


def _open_source_dir(name: str, parent_fd: int, display: Path) -> int:
    try:
        fd = os.open(name, _SOURCE_DIR_FLAGS, dir_fd=parent_fd)
    except OSError as exc:
        raise ValueError(f"symlinked or unstable source directory refused: {display}") from exc
    if not stat.S_ISDIR(os.fstat(fd).st_mode):
        os.close(fd)
        raise ValueError(f"non-directory source path refused: {display}")
    return fd


def _open_vault_root(vault: Path) -> int:
    """Open vault without following any path component."""
    fd = os.open(os.sep, _SOURCE_DIR_FLAGS)
    current = Path(os.sep)
    try:
        for component in vault.parts[1:]:
            current = current / component
            child_fd = _open_source_dir(component, fd, current)
            os.close(fd)
            fd = child_fd
        return fd
    except BaseException:
        os.close(fd)
        raise


def _read_source_file(name: str, parent_fd: int, display: Path) -> str:
    try:
        fd = os.open(name, _SOURCE_FILE_FLAGS, dir_fd=parent_fd)
    except OSError as exc:
        raise ValueError(f"symlinked or unstable source file refused: {display}") from exc
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError(f"non-regular source file refused: {display}")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks).decode("utf-8")
    finally:
        os.close(fd)


def _read_source_pages(dir_fd: int, prefix: Path = Path()) -> list[tuple[Path, str]]:
    pages: list[tuple[Path, str]] = []
    with os.scandir(dir_fd) as entries:
        names = sorted(entry.name for entry in entries)
    for name in names:
        relative = prefix / name
        try:
            info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        except OSError as exc:
            raise ValueError(f"source tree changed during read: {relative}") from exc
        if stat.S_ISLNK(info.st_mode):
            raise ValueError(f"symlinked source entry refused: {relative}")
        if stat.S_ISDIR(info.st_mode):
            child_fd = _open_source_dir(name, dir_fd, relative)
            try:
                pages.extend(_read_source_pages(child_fd, relative))
            finally:
                os.close(child_fd)
        elif name.endswith(".md"):
            if not stat.S_ISREG(info.st_mode):
                raise ValueError(f"non-regular source file refused: {relative}")
            pages.append((relative, _read_source_file(name, dir_fd, relative)))
    pages.sort(key=lambda item: item[0].as_posix())
    return pages


class Absorber:
    def __init__(self, cfg: Config, clock: Clock = now_iso):
        self.cfg = cfg
        self.db = DB(cfg)
        self.db.pour()
        echo_ensure_schema(self.db)                    # os.2 provenance/independent-witness columns
        self.ledger = Ledger(cfg.ledger_path, vault_dir=cfg.vault_dir)
        self.writer = Writer(self.db, self.ledger, clock)
        self.providers = Providers(cfg)
        self.vocab = {r["predicate"] for r in self.db.conn.execute("SELECT predicate FROM predicates")}
        self.echo_index = EchoIndex()
        for row in self.db.conn.execute(
            "SELECT block_id,doc_id,text FROM blocks WHERE status='active' ORDER BY block_id"
        ):
            self.echo_index.add(row["block_id"], row["text"], row["doc_id"])

    # ---- public API -----------------------------------------------------
    def absorb_text(self, doc_id: str, text: str, kind: str = "raw", path: str | None = None,
                    domain: str = "general", source_created_at: str | None = None,
                    waive_reason: str | None = None, retier_after: bool = True) -> dict:
        stored_path = path or doc_id
        path_obj = Path(stored_path)
        if path_obj.is_absolute():
            resolved_path = path_obj.resolve(strict=False)
            try:
                stored_path = resolved_path.relative_to(self.cfg.vault_dir.resolve()).as_posix()
            except ValueError as exc:
                raise ValueError(f"source path escapes selected vault: {path_obj}") from exc
        else:
            stored_path = path_obj.as_posix()
        prior_doc = self.db.conn.execute(
            "SELECT byte_len,sensitivity,private,privacy_scanned FROM docs WHERE doc_id=?",
            (doc_id,),
        ).fetchone()
        fw = fw_scan(text)
        sensitivity = escalate_sensitivity(prior_doc["sensitivity"] if prior_doc else None, fw)
        private_flag = 1 if (
            fw.tripped or is_private_path(stored_path)
            or (prior_doc is not None and bool(prior_doc["private"])
                and bool(prior_doc["privacy_scanned"]))
        ) else 0
        changed, sha = doc_changed(self.db, doc_id, text)
        retired_same_content = bool(prior_doc and self.db.conn.execute(
            "SELECT 1 FROM blocks WHERE doc_id=? AND status='superseded' "
            "AND NOT EXISTS (SELECT 1 FROM blocks a WHERE a.doc_id=? AND a.status='active') LIMIT 1",
            (doc_id, doc_id),
        ).fetchone())
        if retired_same_content:
            changed = True
        if not changed:
            with self.writer.transaction():
                self.writer.refresh_doc_metadata(
                    doc_id, path=stored_path, kind=kind, domain=domain,
                    source_created_at=source_created_at, private=private_flag,
                    sensitivity=sensitivity,
                )
            return {"doc_id": doc_id, "skipped": True, "reason": "unchanged",
                    "metadata_repaired": True}
        blocks = segment(doc_id, text)
        if retired_same_content:
            for block in blocks:
                base_id = block["block_id"]
                revival = 1
                while self.db.conn.execute(
                    "SELECT 1 FROM blocks WHERE block_id=?", (f"{base_id}@r{revival}",)
                ).fetchone():
                    revival += 1
                block["block_id"] = f"{base_id}@r{revival}"
                block["block_content_id"] = f"{block['block_content_id']}:revival:{revival}"
        if prior_doc is not None:
            new_atom_count = 0
            for _blk in blocks:
                _det, _quar = ex.extract(_blk, self.vocab)
                new_atom_count += len(_det) + len(_quar)
            assess_shrink(self.db, doc_id, new_atom_count,
                          old_text_len=prior_doc["byte_len"],
                          new_text_len=len(text.encode("utf-8")),
                          shrink_floor=shrink_floor_of(self.cfg), waive_reason=waive_reason)
        shelf_life = parse_shelf_life(text)
        summary = {"doc_id": doc_id, "blocks": 0, "edges": 0, "quarantined": 0,
                   "corroborated": 0, "corrections": 0, "world_changes": 0,
                   "retired_blocks": 0, "retracted_edges": 0,
                   "marking": fw.rule_id if fw.tripped else None}
        with self.writer.transaction():
            self.writer.upsert_doc(doc_id, stored_path, kind, sha,
                                   byte_len=len(text.encode("utf-8")), domain=domain,
                                   source_created_at=source_created_at,
                                   sensitivity=sensitivity, private=private_flag)
            current_block_ids = {block["block_id"] for block in blocks}
            for block in blocks:
                block["domain"] = domain                 # os.5 propagate compartment to blocks
                block["source_created_at"] = source_created_at
                # ADR-042 HASHGATE: a block whose sha is unchanged is SKIPPED - idempotent re-ingest,
                # no re-embed / re-extract / duplicate work / churn on the unchanged part of a doc.
                if not block_changed(self.db, block["block_id"], block["block_sha256"]):
                    self.writer.upsert_block(block)
                    continue
                block["context_header"] = context_header(block)
                block["embedded_model"] = self.providers.embedder.name
                self.writer.upsert_block(block)
                summary["blocks"] += 1
                # os.2: record this block's derivation at absorb so N corroborating echoes of the same
                # claim later COLLAPSE to ONE independent witness (never the echo-inflated raw count).
                _rel, _cb, _cn = detect_echo(
                    block["text"], self.echo_index, exclude_doc_id=doc_id,
                )
                record_provenance(self.db, block["block_id"], _rel,
                                  cites_block_id=_cb, cites_node_id=_cn)
                self.echo_index.add(block["block_id"], block["text"], doc_id)

                # entity mentions -> nodes + mention links (node vectors are off by default; see the
                # Alpaca vendoring note at the top of this module)
                for nid, disp in ex.mentions(block["text"]):
                    ensure_node(self.db, self.writer, nid, disp, block["block_id"])

                det, quar = ex.extract(block, self.vocab,
                                       llm_extractor=(self.providers.llm_extractor
                                                      if self.cfg.llm_extractor != "off" else None))
                bcid = block["block_content_id"]
                for cand in det:
                    self._place(cand, bcid, block, summary, quarantined=False)
                for cand in quar:
                    self._place(cand, bcid, block, summary, quarantined=True)
            stale_ids = [r["block_id"] for r in self.db.conn.execute(
                "SELECT block_id FROM blocks WHERE doc_id=? AND status='active'",
                (doc_id,),
            ).fetchall() if r["block_id"] not in current_block_ids]
            for stale_id in sorted(stale_ids):
                retired = self.writer.retire_block(stale_id)
                self.echo_index.remove(stale_id)
                summary["retired_blocks"] += retired["blocks"]
                summary["retracted_edges"] += retired["retracted"]
            stamp_edges(
                self.db, doc_id,
                expires_at=shelf_life["expires_at"],
                volatile=shelf_life["volatile"],
            )
            # on-ingest re-tier (engine.tiering) is a ranking side-write, off by default here; see
            # the Alpaca vendoring note at the top of this module. retier_after is kept in the signature
            # so the upstream caller contract is unchanged.
            _ = retier_after
        return summary

    def absorb_vault(self, include_kinds: tuple[str, ...] = ("raw", "wiki")) -> list[dict]:
        results = []
        seen_doc_ids: set[str] = set()
        vault = Path(os.path.abspath(self.cfg.vault_dir.expanduser()))
        try:
            vault_fd = _open_vault_root(vault)
        except (OSError, ValueError) as exc:
            raise ValueError(f"symlinked or invalid vault root refused: {vault}") from exc
        try:
            for kind, sub in (("raw", "raw"), ("wiki", "wiki")):
                if kind not in include_kinds:
                    continue
                try:
                    info = os.stat(sub, dir_fd=vault_fd, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                except OSError as exc:
                    raise ValueError(f"source root changed during read: {sub}") from exc
                if stat.S_ISLNK(info.st_mode):
                    raise ValueError(f"symlinked source root refused: {sub}")
                if not stat.S_ISDIR(info.st_mode):
                    raise ValueError(f"non-directory source root refused: {sub}")
                root_fd = _open_source_dir(sub, vault_fd, Path(sub))
                try:
                    pages = _read_source_pages(root_fd)
                finally:
                    os.close(root_fd)
                for relative, text in pages:
                    doc_id = (Path(sub) / relative).as_posix()
                    seen_doc_ids.add(doc_id)
                    results.append(self.absorb_text(
                        doc_id, text, kind=kind, path=doc_id, retier_after=False,
                    ))
        finally:
            os.close(vault_fd)
        kind_marks = ",".join("?" for _ in include_kinds)
        existing = self.db.conn.execute(
            f"SELECT doc_id FROM docs WHERE kind IN ({kind_marks}) ORDER BY doc_id",
            tuple(include_kinds),
        ).fetchall() if include_kinds else []
        missing = [r["doc_id"] for r in existing if r["doc_id"] not in seen_doc_ids]
        if missing:
            with self.writer.transaction():
                for doc_id in missing:
                    retired_block_ids = [r["block_id"] for r in self.db.conn.execute(
                        "SELECT block_id FROM blocks WHERE doc_id=? AND status='active'", (doc_id,)
                    ).fetchall()]
                    retired = self.writer.retire_doc(doc_id)
                    for block_id in retired_block_ids:
                        self.echo_index.remove(block_id)
                    results.append({"doc_id": doc_id, "retired": True, **retired})
        # on-ingest re-tier is a ranking side-write, off by default here (Alpaca vendoring note above).
        return results

    # ---- internal -------------------------------------------------------
    def _place(self, cand: dict, bcid: str, block: dict, summary: dict, quarantined: bool) -> None:
        # ensure subject/object nodes exist even if not wikilinked as bare mentions
        ensure_node(self.db, self.writer, cand["subj_node"], cand["subj_node"], block["block_id"])
        if cand["obj_datatype"] == "node" and cand.get("obj_node"):
            ensure_node(self.db, self.writer, cand["obj_node"], cand["obj_node"], block["block_id"])
        cand["source_created_at"] = block.get("source_created_at")   # pc.7 observation-date anchor
        out = encode_candidate(
            self.db, self.writer, cand, bcid,
            quarantined=quarantined, block_text=block["text"],
        )
        if out["action"] not in ("noop", "corroborated"):
            self.writer.add_node_block(cand["subj_node"], block["block_id"], role="claim")
            if cand.get("obj_node"):
                self.writer.add_node_block(cand["obj_node"], block["block_id"], role="claim")
            summary["edges"] += 1
        if out["quarantined"]:
            summary["quarantined"] += 1
        if out["action"] == "corroborated":
            summary["corroborated"] += 1
        elif out["action"] == "correction":
            summary["corrections"] += 1
        elif out["action"] == "world-change":
            summary["world_changes"] += 1

    def close(self) -> None:
        self.db.close()


def absorb_vault(cfg: Config, clock: Clock = now_iso,
                 include_kinds: tuple[str, ...] = ("raw", "wiki")) -> list[dict]:
    a = Absorber(cfg, clock)
    try:
        return a.absorb_vault(include_kinds=include_kinds)
    finally:
        a.close()
