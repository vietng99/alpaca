"""CONTEXTUAL ENRICH (§4.3). Anthropic Contextual Retrieval header.

Deterministic parts (heading path + wikilink targets) are added mechanically. The prose blurb is
an LLM seam left empty in the template (LLM-free). The header is indexable metadata, never a claim.
"""
from __future__ import annotations

import re

_WIKILINK = re.compile(r"\[\[([^\]|#]+)(?:[#|][^\]]*)?\]\]")


def context_header(block: dict) -> str:
    """A 50-100-token-ish deterministic breadcrumb: page, section path, linked entities."""
    parts = []
    if block.get("heading_path"):
        parts.append(f"Section: {block['heading_path']}")
    parts.append(f"Doc: {block['doc_id']}")
    links = _WIKILINK.findall(block.get("text", ""))
    if links:
        uniq = []
        for l in links:
            l = l.strip()
            if l and l not in uniq:
                uniq.append(l)
        parts.append("Entities: " + ", ".join(uniq[:12]))
    return " | ".join(parts)
