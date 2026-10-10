"""STEP 1 - PLAN. qtype classification, temporal-intent parse, sub-claim decomposition, template
selection. The planner selects WHICH success-predicate templates apply; it never authors them.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..clock import now_iso, to_clock_format

#: An ISO-8601 date, alone or with a time of day (`T` or one space, minutes or seconds, fractions,
#: `Z` or an offset): `2026-03-01`, `2026-03-01T03:30:00+00:00`, `2026-03-01 03:30Z` (t-070 D-2).
_MOMENT = re.compile(r"(?<![\w-])(\d{4})-(\d{2})-(\d{2})"
                     r"(?:[Tt ](\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?)([Zz]|[+-]\d{2}:?\d{2})?)?(?![\w:])")
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


_KNOWLEDGE = re.compile(r"\bas (?:known|recorded)(?:\s+at)?\s+(?=\d{4}-)", re.IGNORECASE)


def _moment(match) -> tuple[str, bool] | None:
    """(instant in the store clock's format, has_time) of a `_MOMENT` match. A date alone is the
    start of that day, as before; a date-time is that instant in UTC (an offset converted, none read
    as UTC). A time of day that is not a readable instant (`25:61`) is read as the date alone."""
    date = "%s-%s-%s" % match.group(1, 2, 3)
    if match.group(4) is None:
        return f"{date}T00:00:00+00:00", False
    zone = match.group(5) or "+00:00"
    if zone in ("Z", "z"):
        zone = "+00:00"
    elif ":" not in zone:
        zone = zone[:3] + ":" + zone[3:]
    instant = to_clock_format(f"{date}T{match.group(4)}{zone}")
    return (instant, True) if instant else (f"{date}T00:00:00+00:00", False)


def knowledge_pin(question: str) -> tuple[str | None, tuple[int, int] | None]:
    """(K, span) of "as known at <date or date-time>" (also "as recorded at", or without "at"): a
    date-time pins K at that instant, a date at the end of that day, so everything recorded during
    the day is known (architecture.md 5.1; t-070 D-2, D-3). `span` is the clause's place in the
    question, which the T parse leaves out. (None, None) when the question names no K."""
    for clause in _KNOWLEDGE.finditer(question):
        m = _MOMENT.match(question, clause.end())
        if not m:
            continue
        found = _moment(m)
        if found is None:
            continue
        instant, has_time = found
        k = instant if has_time else "%s-%s-%sT23:59:59+00:00" % m.group(1, 2, 3)
        return k, (clause.start(), m.end())
    return None, None


def parse_as_of(question: str, default: str | None = None) -> tuple[str, str]:
    """Return (T_iso, resolved_from). A date-time pins T at that instant, a full date at the start of
    that day, a bare year at year-end; else now. The date of an "as known at <date>" clause pins K,
    not T (`knowledge_pin`): a question that names only a K reads the world as it stood at K, so T is
    K (`knowledge-time`) and a fact valid later on K's own day is not hidden (t-070 D-3)."""
    if default is not None:
        return default, "api-argument"
    k, span = knowledge_pin(question)
    rest = question if span is None else question[:span[0]] + " " + question[span[1]:]
    for m in _MOMENT.finditer(rest):
        found = _moment(m)
        if found is not None:
            return found[0], "explicit-datetime" if found[1] else "explicit-date"
    y = _YEAR.search(rest)
    if y and re.search(r"\b(in|as of|by|during)\b", rest.lower()):
        return f"{y.group(0)}-12-31T23:59:59+00:00", "explicit-year"
    if k is not None:
        return k, "knowledge-time"
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

    knowledge_time, _span = knowledge_pin(question)

    return Plan(qtype=qtype, as_of=T, terms=terms, subclaims=_subclaims(question),
                templates=templates, resolved_from=resolved_from, knowledge_time=knowledge_time,
                mode=classify_mode(qtype), domain=classify_domain(question))
