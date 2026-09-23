"""BLOCK SEGMENT + ANCHOR (§4.2). Heading-aware split into the smallest citable unit.

The permanent citation anchor is `block_content_id` (content-derived, extractor-independent), NOT
the positional `block_id = doc_id#ordinal` (Op-3-Keystone: positional anchors were the confirmed
defect). `occurrence_index` disambiguates identical text within a doc (boilerplate).
"""
from __future__ import annotations

import re

from ..determinism import block_content_id, normalize_text, sha256_hex

CHUNK_RULESET_VERSION = "md-heading-v1"

_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")


def segment(doc_id: str, content: str) -> list[dict]:
    """Split markdown into blocks. Each heading opens a section; paragraphs are blocks under it."""
    lines = content.split("\n")
    heading_stack: list[str] = []
    blocks: list[dict] = []
    buf: list[str] = []
    buf_start = 0
    pos = 0
    seen_counts: dict[str, int] = {}

    def flush(end_pos: int):
        nonlocal buf, buf_start
        text = "\n".join(buf).strip()
        if text:
            norm = normalize_text(text)
            occ = seen_counts.get(norm, 0)
            seen_counts[norm] = occ + 1
            ordinal = len(blocks)
            content_id = block_content_id(text, doc_id, occ)
            blocks.append({
                "block_id": f"{doc_id}#b_{sha256_hex(content_id)[:24]}",
                "block_content_id": content_id,
                "occurrence_index": occ,
                "chunk_ruleset_version": CHUNK_RULESET_VERSION,
                "doc_id": doc_id,
                "ordinal": ordinal,
                "heading_path": " / ".join(heading_stack),
                "char_start": buf_start,
                "char_end": end_pos,
                "block_sha256": sha256_hex(norm),
                "block_type": "prose",
                "text": text,
            })
        buf = []

    for line in lines:
        m = _HEADING.match(line)
        if m:
            flush(pos)
            level = len(m.group(1))
            title = m.group(2).strip()
            heading_stack = heading_stack[: level - 1]
            while len(heading_stack) < level - 1:
                heading_stack.append("")
            heading_stack.append(title)
            buf_start = pos + len(line) + 1
        elif line.strip() == "":
            flush(pos)
            buf_start = pos + len(line) + 1
        else:
            if not buf:
                buf_start = pos
            buf.append(line)
        pos += len(line) + 1
    flush(pos)
    return blocks
