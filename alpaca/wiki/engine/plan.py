"""STEP 1 - PLAN. qtype classification, temporal-intent parse, sub-claim decomposition, template
selection. The planner selects WHICH success-predicate templates apply; it never authors them.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..clock import now_iso

_DATE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_YEAR = re.compile(r"\b(19|20)\d{2}\b")
_PRED_VERBS = ("work", "works", "founded", "attended", "cites", "cite", "born",
               "located", "refines", "related")

QTYPES = ("single-hop-lexical", "multi-hop", "as-of-currency", "delta", "thematic-summary")

# retrieval-one-door.2 - the three retrieval MODES that shape the graph walk in retrieve.gather.
#   GLOBAL  : thematic/summary - widen to community/tier hubs (km.7/dm.10)
#   LOCAL   : entity single/multi-hop - seed PPR from the linked entities only
#   DESCENT : as-of / currency / delta - follow the supersedes/successor chain
MODES = ("GLOBAL", "LOCAL", "DESCENT")

# oracle-safety.5 WIRING - the ONE inferred target domain (default None => no compartment filter).
DOMAINS = ("embedded", "career", "general", "personal")
_DOMAIN_CUES = {
    "embedded": ("firmware", "hardware", "register", "microcontroller", "embedded", "gpio",
                 "rtos", "kernel", "driver", "soc", "mcu", "interrupt", "peripheral"),
    "career": ("work", "works", "job", "employer", "company", "career", "engineer", "salary",
               "hired", "colleague", "promotion", "role", "manager", "employed"),
    "personal": ("cooking", "recipe", "family", "mother", "father", "hobby", "personal",
                 "friend", "health", "phở", "pho", "nấu", "món", "vợ", "chồng", "diary"),
}


@dataclass
class Plan:
    qtype: str
    as_of: str
    terms: list[str]
    subclaims: list[str]
    templates: list[str] = field(default_factory=list)
    txn_axis: str = "latest"
    resolved_from: str = "now"
    knowledge_time: str | None = None      # F8: transaction-axis K for audit/delta reads (None = latest)
    mode: str = "LOCAL"                     # r1.2: GLOBAL | LOCAL | DESCENT retrieval mode
    domain: str | None = None              # os.5: inferred compartment; None => all domains (no filter)


def classify_mode(qtype: str) -> str:
    """r1.2 - map a qtype to the retrieval MODE that shapes the walk (deterministic, total)."""
    if qtype == "thematic-summary":
        return "GLOBAL"
    if qtype in ("as-of-currency", "delta"):
        return "DESCENT"
    return "LOCAL"                          # single-hop-lexical, multi-hop


def classify_domain(question: str) -> str | None:
    """os.5 - infer ONE target compartment from surface cues; None (=> all domains) when unsure.

    The LLM/planner may choose the domain but cannot leak past the data-layer WHERE clause; a None
    return means 'no compartment asserted' so the default behaviour (all domains) is preserved.
    """
    low = question.lower()
    toks = set(re.findall(r"[^\W_]+", low, re.UNICODE))
    scores = {d: sum(1 for c in cues if c in toks or c in low) for d, cues in _DOMAIN_CUES.items()}
    best = max(scores, key=lambda d: scores[d])
    return best if scores[best] > 0 else None


_KNOWLEDGE = re.compile(r"\bas (?:known|recorded)(?:\s+at)?\s+(\d{4}-\d{2}-\d{2})\b")


def parse_as_of(question: str, default: str | None = None) -> tuple[str, str]:
    """Return (T_iso, resolved_from). A full date pins T; a bare year pins year-end; else now."""
    if default is not None:
        return default, "api-argument"
    m = _DATE.search(question)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}T00:00:00+00:00", "explicit-date"
    y = _YEAR.search(question)
    if y and re.search(r"\b(in|as of|by|during)\b", question.lower()):
        return f"{y.group(0)}-12-31T23:59:59+00:00", "explicit-year"
    return now_iso(), "now"


def _subclaims(question: str) -> list[str]:
    parts = re.split(r"\band\b|;|,", question)
    parts = [p.strip(" ?.") for p in parts if p.strip(" ?.")]
    return parts or [question.strip(" ?.")]


def classify(question: str, as_of: str | None = None) -> Plan:
    low = question.lower()
    T, resolved_from = parse_as_of(question, as_of)
    terms = [t for t in re.findall(r"[^\W_]+", low, re.UNICODE) if len(t) > 2]

    if re.search(r"\b(difference|changed|change|delta|since|before|after)\b", low):
        qtype = "delta"
    elif re.search(r"\b(as of|currently|current|now|latest)\b", low) or resolved_from != "now":
        qtype = "as-of-currency"
    elif re.search(r"\b(summary|overview|themes?|about|describe)\b", low):
        qtype = "thematic-summary"
    elif low.count("[[") >= 2 or re.search(r"\b(and|both|between|related)\b", low):
        qtype = "multi-hop"
    elif any(v in low for v in _PRED_VERBS):
        qtype = "single-hop-lexical"
    else:
        qtype = "single-hop-lexical"

    base = ["SUCCESSOR_READ", "OVERTURNER_QUERY", "AS_OF_CURRENCY", "SUBCLAIM_COVERAGE"]
    if qtype == "single-hop-lexical":
        templates = ["EXACT_MATCH_FLOOR", "AS_OF_CURRENCY", "SUCCESSOR_READ", "SUBCLAIM_COVERAGE"]
    elif qtype == "delta":
        templates = ["AS_OF_CURRENCY", "SUCCESSOR_READ", "OVERTURNER_QUERY"]
    elif qtype == "thematic-summary":
        templates = ["SUBCLAIM_COVERAGE", "OVERTURNER_QUERY"]
    else:
        templates = base

    km = _KNOWLEDGE.search(low)
    knowledge_time = f"{km.group(1)}T23:59:59+00:00" if km else None

    return Plan(qtype=qtype, as_of=T, terms=terms, subclaims=_subclaims(question),
                templates=templates, resolved_from=resolved_from, knowledge_time=knowledge_time,
                mode=classify_mode(qtype), domain=classify_domain(question))
