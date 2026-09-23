"""Dependency-free deterministic provider defaults.

These are NOT production models. They are reproducible stand-ins that let the whole engine run and
its tests pass with pure stdlib. Every one is a documented seam:
  - HashEmbedder     -> swap for a real CPU sentence embedder
  - LexicalReranker  -> swap for mxbai-rerank-v2 (0.5B CPU)
  - StructuralEntailer -> swap for the HHEM/MiniCheck/Paladin ensemble
  - NullLLMExtractor -> swap for an LLM fallback extractor (output stays quarantined)
"""
from __future__ import annotations

import hashlib
import math
import re

_TOKEN = re.compile(r"[^\W_]+", re.UNICODE)
_NEG = re.compile(r"\b(not|no|never|isn't|wasn't|aren't|didn't|doesn't|don't|cannot|can't)\b")
_NUM = re.compile(r"-?\d+(?:[.,]\d+)?")


def _tokens(text: str) -> list[str]:
    return [t for t in _TOKEN.findall(text.lower()) if len(t) > 1]


class HashEmbedder:
    """Feature-hashing bag-of-words embedder. Deterministic, signed buckets, L2-normalized."""
    def __init__(self, dim: int = 64):
        self.dim = dim
        self.name = f"deterministic-hash-{dim}"

    def embed(self, text: str) -> list[float]:
        v = [0.0] * self.dim
        for tok in _tokens(text):
            h = hashlib.sha256(tok.encode("utf-8")).digest()
            bucket = int.from_bytes(h[:4], "big") % self.dim
            sign = 1.0 if h[4] & 1 else -1.0
            v[bucket] += sign
        norm = math.sqrt(sum(x * x for x in v))
        return [x / norm for x in v] if norm > 0 else v


class LexicalReranker:
    """Deterministic token-overlap reranker (a stand-in for a cross-encoder)."""
    name = "lexical-overlap"

    def rerank(self, query: str, blocks: list[tuple[str, str]]) -> dict[str, float]:
        q = set(_tokens(query))
        out: dict[str, float] = {}
        for bid, text in blocks:
            bt = set(_tokens(text))
            inter = len(q & bt)
            union = len(q | bt) or 1
            out[bid] = inter / union
        return out


class IdentityReranker:
    """No-op reranker (config default 'identity'): preserves fusion order."""
    name = "identity"

    def rerank(self, query: str, blocks: list[tuple[str, str]]) -> dict[str, float]:
        return {bid: 0.0 for bid, _text in blocks}


class StructuralEntailer:
    """Deterministic, conservative entailment. Real ensemble plugs in here.

    - number/date mismatch or a polarity flip vs source -> 'contradicted'
    - claim tokens well-covered by source -> 'supported'
    - otherwise -> 'unsupported' (drives ABSTAIN; never fabricates support)
    """
    name = "structural-only"

    def entail(self, claim: str, source_text: str) -> tuple[str, float]:
        ct, st = _tokens(claim), _tokens(source_text)
        if not ct:
            return "unsupported", 0.0
        cset, sset = set(ct), set(st)
        coverage = len(cset & sset) / len(cset)
        # numeric agreement: every number in the claim must appear in the source
        cnums = set(_NUM.findall(claim))
        snums = set(_NUM.findall(source_text))
        if cnums and not cnums.issubset(snums):
            return "contradicted", 0.9
        # polarity flip on an otherwise-covered claim
        if coverage >= 0.6 and (bool(_NEG.search(claim)) != bool(_NEG.search(source_text))):
            return "contradicted", 0.7
        if coverage >= 0.8:
            return "supported", coverage
        if coverage >= 0.5:
            return "unsupported", coverage      # weak -> abstain territory
        return "unsupported", coverage


class NullLLMExtractor:
    """Template ships LLM-free (deterministic-first). Returns nothing; the seam is documented."""
    name = "off"

    def extract(self, block_text: str, context: str) -> list[dict]:
        return []


# ============================================================ oracle-safety.1 abstaining ensemble
# An entailment ensemble of >=3 DISTINCT deterministic checkers. No lone judge. Each member keeps
# the (label, score) protocol; the ensemble ABSTAINS (returns 'unsupported') on disagreement or on
# any 'contradicted' vote - fail-safe by construction. Real HHEM/MiniCheck/Paladin members plug in
# as additional voters without changing the aggregation rule.

class TokenCoverageEntailer:
    """Checker: claim tokens must be well-covered by the source. Aspect = lexical coverage only."""
    name = "token-coverage"

    def entail(self, claim: str, source_text: str) -> tuple[str, float]:
        ct = set(_tokens(claim))
        if not ct:
            return "unsupported", 0.0
        coverage = len(ct & set(_tokens(source_text))) / len(ct)
        if coverage >= 0.8:
            return "supported", coverage
        return "unsupported", coverage


class NumberDateEntailer:
    """Checker: every number/date asserted in the claim must appear verbatim in the source, else
    'contradicted'. A claim with no numbers is not this checker's concern -> 'supported' (abstain-up)."""
    name = "number-date-exact"

    def entail(self, claim: str, source_text: str) -> tuple[str, float]:
        cnums = set(_NUM.findall(claim))
        if cnums and not cnums.issubset(set(_NUM.findall(source_text))):
            return "contradicted", 0.9
        return "supported", 1.0


class NegationPolarityEntailer:
    """Checker: a polarity flip between an otherwise-covered claim and its source -> 'contradicted'."""
    name = "negation-polarity"

    def entail(self, claim: str, source_text: str) -> tuple[str, float]:
        ct = set(_tokens(claim))
        if not ct:
            return "unsupported", 0.0
        coverage = len(ct & set(_tokens(source_text))) / len(ct)
        if coverage >= 0.6 and (bool(_NEG.search(claim)) != bool(_NEG.search(source_text))):
            return "contradicted", 0.7
        return "supported", 1.0


class EnsembleEntailer:
    """>=3 distinct deterministic checkers; majority-with-veto aggregation (oracle-safety.1).

    Rule: ANY member voting 'contradicted' -> abstain ('unsupported'); otherwise a STRICT majority of
    'supported' is required to return 'supported', else abstain. Split/tie -> abstain. Never a lone
    judge, never fabricated support.
    """
    name = "ensemble"

    def __init__(self, members=None):
        self.members = list(members) if members else [
            StructuralEntailer(), TokenCoverageEntailer(),
            NumberDateEntailer(), NegationPolarityEntailer(),
        ]
        if len(self.members) < 3:
            raise ValueError("EnsembleEntailer requires >=3 members (no lone judge)")

    def entail(self, claim: str, source_text: str) -> tuple[str, float]:
        votes = [m.entail(claim, source_text) for m in self.members]
        labels = [v[0] for v in votes]
        if any(l == "contradicted" for l in labels):
            return "unsupported", 0.5                 # veto -> ABSTAIN
        supported = [v for v in votes if v[0] == "supported"]
        if len(supported) > len(self.members) / 2:    # strict majority required
            return "supported", sum(v[1] for v in supported) / len(supported)
        return "unsupported", 0.5                      # disagreement -> ABSTAIN


# ====================================================== oracle-safety.7 VN RoutedEntailer (G.1)
# Deterministic language routing: VN text -> a VI structural lane carrying the VN negation lexicon
# (không / chưa / chẳng / không phải, diacritics PRESERVED); everything else -> the EN lane. The
# StructuralEntailer ALWAYS votes; lane vs structural disagreement -> ('unsupported', 0.5) => ABSTAIN
# (fail-safe). With no VI *model* available this is a structural-floor-only VI lane, never a guess.

# VN function words (high-frequency, diacritic-bearing) - a cheap language signal.
_VN_FUNC_WORDS = frozenset({
    "và", "của", "là", "có", "không", "người", "được", "những", "cho", "một",
    "các", "với", "này", "đã", "thì", "ở", "ra", "cũng", "nhà", "nước",
})
# VN negation lexicon - "không phải" MUST precede "không" so the longer form wins.
_VN_NEG = re.compile(r"(không phải|không|chưa|chẳng)")
_VN_DIACRITIC = re.compile(
    r"[àáảãạăắằẳẵặâấầẩẫậđèéẻẽẹêếềểễệìíỉĩịòóỏõọôốồổỗộơớờởỡợùúủũụưứừửữựỳýỷỹỵ]",
    re.IGNORECASE,
)


def detect_lang(text: str) -> str:
    """Deterministic VN detector: VN-diacritic ratio OR a VN function-word hit -> 'vi', else 'en'."""
    alpha = [c for c in text if c.isalpha()]
    ratio = (len(_VN_DIACRITIC.findall(text)) / len(alpha)) if alpha else 0.0
    words = set(text.lower().split())
    if ratio > 0.02 or (words & _VN_FUNC_WORDS):
        return "vi"
    return "en"


class VIStructuralEntailer:
    """Structural entailment floor for Vietnamese: diacritics preserved, VN negation lexicon."""
    name = "structural-vi"

    def entail(self, claim: str, source_text: str) -> tuple[str, float]:
        ct = _tokens(claim)
        if not ct:
            return "unsupported", 0.0
        cset, sset = set(ct), set(_tokens(source_text))
        coverage = len(cset & sset) / len(cset)
        cnums = set(_NUM.findall(claim))
        if cnums and not cnums.issubset(set(_NUM.findall(source_text))):
            return "contradicted", 0.9
        if coverage >= 0.6 and (bool(_VN_NEG.search(claim)) != bool(_VN_NEG.search(source_text))):
            return "contradicted", 0.7
        if coverage >= 0.8:
            return "supported", coverage
        return "unsupported", coverage


class RoutedEntailer:
    """Language-routed entailer with a fail-safe structural co-vote (oracle-safety.7)."""
    name = "routed"

    def __init__(self):
        self.en = StructuralEntailer()
        self.vi = VIStructuralEntailer()
        self.structural = StructuralEntailer()        # always votes

    def entail(self, claim: str, source_text: str) -> tuple[str, float]:
        lane = self.vi if detect_lang(claim + " " + source_text) == "vi" else self.en
        lane_label, lane_score = lane.entail(claim, source_text)
        struct_label, _ = self.structural.entail(claim, source_text)
        if lane_label != struct_label:
            return "unsupported", 0.5                  # disagreement -> ABSTAIN (fail-safe)
        return lane_label, lane_score
