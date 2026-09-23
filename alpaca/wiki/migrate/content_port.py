"""Vault content-port losslessness - ID-level parity (determinism-machine.13).

Vendored from rune2/migrate/content_port.py. The reused surface is the PARITY REPORTER:
`ParityReport` (the acceptance artifact), its UNACCOUNTED list (the never-silently-lossy rule), the
structural-edge enumerator `_enumerate_source`, the connector->predicate map, and the no-follow
filesystem reader `_read_pages`. These are carried byte-faithful.

Direction. Alpaca drives this reporter in the OUTWARD direction (alpaca.wiki.extract): the emit builds a
standalone vault and then reconciles what survived. The upstream inward `port()` INGESTS a source
vault through the ingest Absorber and retiers it; but the ingest absorber (rune2/ingest/absorb.py,
rune2/ingest/extract.py) is NOT vendored under alpaca.wiki (that is M2.14, not this task) and the retier
step reaches the guarded ranking surface (engine.tiering) that the one-door law (alpaca.wiki.guards)
closes to every module but the read door. So the inward `port()` is not carried here. In its place
`reconcile()` performs the SAME id-for-id reconciliation - source pages / entities / structural
edges against the atoms that survived - reading the surviving atoms from an already-built extract
rather than ingesting them. `slug` and `VERB_MAP` are carried from rune2/ingest/extract.py (also not
vendored) because the structural-edge derivation needs them; they are byte-faithful copies.

Pure stdlib.
"""
from __future__ import annotations

from collections import Counter
import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------------------------------
# Carried from rune2/ingest/extract.py (not vendored under alpaca.wiki): the structural-edge vocabulary
# and the slug function the parity enumerator depends on. Byte-faithful.

# verb-phrase -> canonical predicate (only predicates present in the vocabulary are emitted)
VERB_MAP = {
    "works at": "works_at", "works_at": "works_at",
    "founded": "founded",
    "attended": "attended",
    "cites": "cites", "cited": "cites",
    "refines": "refines",
    "located in": "located_in", "located_in": "located_in",
    "born on": "born_on", "born_on": "born_on",
    "related to": "related_to", "related_to": "related_to",
}


def slug(display: str) -> str:
    """Deterministic node id from a wikilink target. '[[slug|Display]]' / '[[Display#anchor]]'."""
    target = re.split(r"[|#]", display, 1)[0].strip()
    s = re.sub(r"[^\w]+", "-", target.lower(), flags=re.UNICODE).strip("-")
    return s or "unknown"


# ---------------------------------------------------------------------------------------------------

_WIKILINK = re.compile(r"\[\[([^\]]+)\]\]")
# a structural wikilink edge: two links on one line joined by a non-empty, non-link connector
_EDGE_LINE = re.compile(r"\[\[([^\]]+)\]\](.*?)\[\[([^\]]+)\]\]")


@dataclass
class ParityReport:
    source_docs: int = 0
    matched_docs: int = 0
    source_nodes: int = 0
    matched_nodes: int = 0
    source_edges: int = 0
    matched_edges: int = 0
    missing_docs: list[str] = field(default_factory=list)
    missing_nodes: list[str] = field(default_factory=list)
    missing_edges: list[tuple] = field(default_factory=list)  # (subj, obj, doc_id)

    @property
    def unaccounted(self) -> int:
        return len(self.missing_docs) + len(self.missing_nodes) + len(self.missing_edges)

    @property
    def ok(self) -> bool:
        return not (self.missing_docs or self.missing_nodes or self.missing_edges)

    def as_dict(self) -> dict:
        return {
            "source_docs": self.source_docs, "matched_docs": self.matched_docs,
            "source_nodes": self.source_nodes, "matched_nodes": self.matched_nodes,
            "source_edges": self.source_edges, "matched_edges": self.matched_edges,
            "unaccounted": self.unaccounted,
            "missing_docs": list(self.missing_docs),
            "missing_nodes": list(self.missing_nodes),
            "missing_edges": [list(t) for t in self.missing_edges],
            "ok": self.ok,
        }


def _connector_predicate(connector: str) -> str:
    """Map a structural connector to the predicate the deterministic extractor should emit.

    Unknown connectors stay distinct so parity reports them as lost instead of accepting any
    unrelated edge between the same endpoint nodes.
    """
    normalized = " ".join(connector.casefold().split())
    for verb in sorted(VERB_MAP, key=len, reverse=True):
        if re.search(rf"\b{re.escape(verb)}\b", normalized):
            return VERB_MAP[verb]
    return "structural:" + normalized.strip(" .,:;!?()[]{}")


def _enumerate_source(pages: dict[str, str]) -> tuple[list[str], set[str], list[tuple]]:
    """Derive doc IDs, node slugs, and structural edges as (subj, predicate, obj, doc)."""
    doc_ids: list[str] = []
    nodes: set[str] = set()
    edges: list[tuple] = []
    for doc_id in sorted(pages):
        doc_ids.append(doc_id)
        text = pages[doc_id]
        for link in _WIKILINK.findall(text):
            nodes.add(slug(link))
        for line in text.split("\n"):
            m = _EDGE_LINE.search(line)
            if m:
                edges.append((slug(m.group(1)), _connector_predicate(m.group(2)),
                              slug(m.group(3)), doc_id))
    return doc_ids, nodes, edges


_DIR_FLAGS = (os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
              | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))
_FILE_FLAGS = (os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
               | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0))


def _open_source_dir(name: str, parent_fd: int, display: Path) -> int:
    try:
        fd = os.open(name, _DIR_FLAGS, dir_fd=parent_fd)
    except OSError as exc:
        raise ValueError(f"symlinked or unstable source directory refused: {display}") from exc
    if not stat.S_ISDIR(os.fstat(fd).st_mode):
        os.close(fd)
        raise ValueError(f"non-directory source path refused: {display}")
    return fd


def _open_source_root(root: Path) -> int:
    """Open an absolute source root without following any path component."""
    fd = os.open(os.sep, _DIR_FLAGS)
    current = Path(os.sep)
    try:
        for component in root.parts[1:]:
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
        fd = os.open(name, _FILE_FLAGS, dir_fd=parent_fd)
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


def _walk_source_pages(dir_fd: int, prefix: Path, pages: dict[str, str]) -> None:
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
                _walk_source_pages(child_fd, relative, pages)
            finally:
                os.close(child_fd)
        elif stat.S_ISREG(info.st_mode) and name.endswith(".md"):
            doc_id = relative.as_posix()
            if doc_id in pages:
                raise ValueError(f"duplicate source document id: {doc_id}")
            pages[doc_id] = _read_source_file(name, dir_fd, relative)


def _read_pages(source_vault: str | Path) -> dict[str, str]:
    """Read source Markdown through no-follow descriptors with stable vault-relative IDs."""
    root = Path(os.path.abspath(Path(source_vault).expanduser()))
    try:
        root_fd = _open_source_root(root)
    except (OSError, ValueError) as exc:
        raise ValueError(f"symlinked or invalid source vault refused: {root}") from exc
    pages: dict[str, str] = {}
    try:
        with os.scandir(root_fd) as entries:
            root_names = {entry.name for entry in entries}
        scopes: list[str] = []
        for name in ("raw", "wiki"):
            if name not in root_names:
                continue
            info = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
            if stat.S_ISLNK(info.st_mode):
                raise ValueError(f"symlinked source entry refused: {name}")
            if stat.S_ISDIR(info.st_mode):
                scopes.append(name)
        if not scopes:
            _walk_source_pages(root_fd, Path(), pages)
        else:
            for name in scopes:
                scope_fd = _open_source_dir(name, root_fd, Path(name))
                try:
                    _walk_source_pages(scope_fd, Path(name), pages)
                finally:
                    os.close(scope_fd)
    finally:
        os.close(root_fd)
    return pages


def reconcile(pages: dict[str, str], actual_docs, actual_nodes, actual_edges) -> ParityReport:
    """OUTWARD id-for-id reconciliation - the reconciliation half of upstream `port()`.

    `pages` is a {doc_id: text} map of the source pages that were meant to travel. `actual_docs` and
    `actual_nodes` are the doc-id / node-id sets that survived into the extract, and `actual_edges`
    is an iterable of (subj, predicate, obj, source_doc_id) tuples for the edges that survived. A
    source structural edge with no surviving twin (e.g. an out-of-vocabulary connector the extractor
    never emitted) is reported UNACCOUNTED - never dropped silently.

    Edge parity is a consumed multiset keyed by predicate and source document. One unrelated edge
    cannot satisfy several expected source assertions with the same endpoint nodes.
    """
    exp_docs, exp_nodes, exp_edges = _enumerate_source(pages)

    report = ParityReport()

    docs_set = set(actual_docs)
    report.source_docs = len(exp_docs)
    for doc_id in exp_docs:
        if doc_id in docs_set:
            report.matched_docs += 1
        else:
            report.missing_docs.append(doc_id)

    nodes_set = set(actual_nodes)
    report.source_nodes = len(exp_nodes)
    for nid in sorted(exp_nodes):
        if nid in nodes_set:
            report.matched_nodes += 1
        else:
            report.missing_nodes.append(nid)

    report.source_edges = len(exp_edges)
    actual = Counter(actual_edges)
    for subj, predicate, obj, doc_id in exp_edges:
        key = (subj, predicate, obj, doc_id)
        if actual[key] > 0:
            report.matched_edges += 1
            actual[key] -= 1
        else:
            report.missing_edges.append((subj, obj, doc_id))

    return report
