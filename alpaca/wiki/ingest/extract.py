"""EXTRACT (§4.4, deterministic-first / GBrain-style).

Typed-edge extraction over the declared `predicates` vocabulary with ZERO LLM for the common
cases. HARD RULE: every emitted edge carries `source_block_id` or it is rejected. LLM extraction
runs ONLY where deterministic yields nothing -> quarantined (kept out of PPR mass until Dream
re-confirms). Entities are `[[wikilinks]]`; distinct link targets stay distinct nodes (ADR-029).
"""
from __future__ import annotations

import re

_WIKILINK = re.compile(r"\[\[([^\]]+)\]\]")
_DATE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
_VALID_FROM = re.compile(r"@(\d{4}-\d{2}-\d{2})")

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
# longest verb phrases first so "works at" wins over a bare token
_VERBS_SORTED = sorted(VERB_MAP.keys(), key=len, reverse=True)


def slug(display: str) -> str:
    """Deterministic node id from a wikilink target. '[[slug|Display]]' / '[[Display#anchor]]'."""
    target = re.split(r"[|#]", display, 1)[0].strip()
    s = re.sub(r"[^\w]+", "-", target.lower(), flags=re.UNICODE).strip("-")
    return s or "unknown"


def display_of(link: str) -> str:
    return re.split(r"[#]", link, 1)[0].split("|")[-1].strip()


def mentions(text: str) -> list[tuple[str, str]]:
    """All wikilink mentions as (node_id, display). Stable, de-duplicated by node_id."""
    seen, out = set(), []
    for l in _WIKILINK.findall(text):
        nid = slug(l)
        if nid not in seen:
            seen.add(nid)
            out.append((nid, display_of(l)))
    return out


def extract_deterministic(block: dict, vocab: set[str]) -> list[dict]:
    """Return edge-candidate dicts from one block. Only vocabulary predicates on typed values."""
    text = block["text"]
    bid = block["block_id"]
    doc_id = block["doc_id"]
    cands: list[dict] = []
    vf_m = _VALID_FROM.search(text)
    valid_from = vf_m.group(1) + "T00:00:00+00:00" if vf_m else ""

    for line in text.split("\n"):
        links = list(_WIKILINK.finditer(line))
        low = line.lower()
        # pattern A: [[Subj]] <verb> [[Obj]]
        if len(links) >= 2:
            for verb in _VERBS_SORTED:
                pred = VERB_MAP[verb]
                if pred not in vocab:
                    continue
                # verb must sit between the first two links
                between = line[links[0].end():links[1].start()].lower()
                if re.search(rf"\b{re.escape(verb)}\b", between):
                    subj = slug(links[0].group(1))
                    obj = slug(links[1].group(1))
                    cands.append({
                        "subj_node": subj, "predicate": pred, "obj_node": obj,
                        "obj_literal": None, "obj_datatype": "node",
                        "source_block_id": bid, "source_doc_id": doc_id,
                        "source_quote": line.strip(), "extractor": "deterministic",
                        "confidence": 1.0, "valid_from": valid_from,
                    })
                    break
        # pattern B: [[Subj]] born on <date>
        if links and "born on" in low and "born_on" in vocab:
            dm = _DATE.search(line)
            if dm:
                cands.append({
                    "subj_node": slug(links[0].group(1)), "predicate": "born_on",
                    "obj_node": None, "obj_literal": dm.group(1), "obj_datatype": "date",
                    "source_block_id": bid, "source_doc_id": doc_id,
                    "source_quote": line.strip(), "extractor": "deterministic",
                    "confidence": 1.0, "valid_from": valid_from,
                })
    return cands


def extract(block: dict, vocab: set[str], llm_extractor=None) -> tuple[list[dict], list[dict]]:
    """Return (confident_deterministic, quarantined_llm). LLM runs only if deterministic is empty."""
    det = extract_deterministic(block, vocab)
    quarantined: list[dict] = []
    if not det and llm_extractor is not None:
        for raw in llm_extractor.extract(block["text"], block.get("context_header", "")):
            if not raw.get("source_block_id"):
                raw["source_block_id"] = block["block_id"]      # HARD RULE: anchor or reject
            raw.setdefault("source_doc_id", block["doc_id"])
            raw.setdefault("source_quote", block["text"][:200])
            raw["extractor"] = "llm"
            raw["confidence"] = min(0.5, float(raw.get("confidence", 0.3)))
            quarantined.append(raw)
    return det, quarantined
