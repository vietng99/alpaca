"""ingest.firewall - classification-boundary marking scanner (oracle-safety.4, Fork B).

BINDING nuclear ruling (Fork B): this scanner is a **RAISE-ONLY ADVISORY tripwire**, NOT a
firewall, NOT fail-closed, NOT a leak barrier. A no-trip result NEVER implies a document is
pushable/safe - it only means no formal marking was recognised. The scanner may only RAISE a
document's `sensitivity` label; it may never clear it. The one refusal UX string is
``marking detected: <rule>`` - never ``this is unclassified``.

The RULE (research fold, brief 1): a real marking is *structure, not vocabulary*. Fire only on
  (a) a TIER-1 token (single, unambiguous control/dissemination/authority marking), or
  (b) a TIER-2 banner word + >=1 structural corroborator.
Case is a hint, never sufficient. Vocabulary alone (the word "secret" / "confidential") never trips.

Normalization pipeline before matching (defeats zero-width / homoglyph / spaced-letter evasion):
  strip zero-width -> NFKC -> collapse intra-token whitespace  == the `base` surface;
  confusable-fold (Cyrillic/Greek homoglyph -> Latin)          == the `folded` surface (Latin scan);
  single-space between single capitals removed                 == the `despaced` surface.
CN (绝密/机密/秘密) + RU (СЕКРЕТНО) + VN markings are matched on `base` BEFORE the confusable fold
(folding would destroy a genuine Cyrillic/Han banner). Bare-word banners are case-sensitive
(ALL-CAPS); only structural tokens (//SI, CUI//SP, `Classified By:`) are casefolded.

Vietnamese (binding ruling): match diacritic-EXACT ``TUYỆT MẬT`` / ``TỐI MẬT`` (Tier-1). NEVER match
diacritic-stripped ``toi mat`` (it collides with the benign ``tôi mất`` = "I lost"). Bare ``MẬT`` is
Tier-2 and fires ONLY beside a Vietnamese state-secret context anchor, never on lowercase prose.

Export control (binding ruling, corrects the build-plan): ``EAR99`` is UNCONTROLLED - it must NOT
trip. Bare ``ITAR`` / ``ECCN`` / ``USML`` are Tier-2 corroborator-required, not hard Tier-1.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

# --------------------------------------------------------------------------- normalization

_ZERO_WIDTH = {c: None for c in (0x200B, 0x200C, 0x200D, 0x2060, 0xFEFF, 0x00AD, 0x180E, 0x200E, 0x200F)}

# Cyrillic / Greek homoglyphs -> Latin uppercase (adversarial-evasion defence). Applied AFTER the
# CN/RU/VN scan so a genuine Cyrillic banner (СЕКРЕТНО) is matched before it is folded away.
_CONFUSABLES = {
    # Cyrillic
    "А": "A", "В": "B", "Е": "E", "К": "K", "М": "M", "Н": "H", "О": "O", "Р": "P",
    "С": "C", "Т": "T", "Х": "X", "У": "Y", "І": "I", "Ѕ": "S", "Ј": "J", "Ԛ": "Q",
    "а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "х": "x", "у": "y",
    # Greek
    "Α": "A", "Β": "B", "Ε": "E", "Ζ": "Z", "Η": "H", "Ι": "I", "Κ": "K", "Μ": "M",
    "Ν": "N", "Ο": "O", "Ρ": "P", "Τ": "T", "Υ": "Y", "Χ": "X",
}
_CONFUSABLE_TABLE = {ord(k): v for k, v in _CONFUSABLES.items()}


def _base(text: str) -> str:
    """strip zero-width -> NFKC -> collapse intra-token whitespace runs to a single space."""
    t = text.translate(_ZERO_WIDTH)
    t = unicodedata.normalize("NFKC", t)
    t = t.replace("\t", " ")
    t = re.sub(r"[ ]{2,}", " ", t)
    return t


def _fold(base: str) -> str:
    """Confusable-fold homoglyphs to Latin (for the Latin-token scan only)."""
    return base.translate(_CONFUSABLE_TABLE)


def _despace(folded: str) -> str:
    """Collapse a single space between two single capital letters: 'N O F O R N' -> 'NOFORN'."""
    prev = None
    while folded != prev:
        prev = folded
        folded = re.sub(r"(?<=\b[A-Z]) (?=[A-Z]\b)", "", folded)
    return folded


# --------------------------------------------------------------------------- rule tables

CI = re.IGNORECASE
ML = re.MULTILINE

# surface: 'latin' -> scan the confusable-folded (+ despaced for dissemination) text;
#          'raw'   -> scan the pre-fold `base` text (CN/RU/VN precomposed forms).
# TIER-1: a single hit trips.
_TIER1: list[tuple[str, re.Pattern, str, bool]] = [
    # (rule_id, compiled, surface, also_despaced)
    ("US-DISSEM", re.compile(
        r"\b(NOFORN|ORCON|PROPIN|RELIDO|IMCON|NOCONTRACT|RSEN|FISA|FVEY|FGI)\b"), "latin", True),
    ("US-REL-TO", re.compile(r"\bREL TO\b"), "latin", False),
    ("US-EYES-ONLY", re.compile(r"\bEYES ONLY\b"), "latin", False),
    ("US-DEA-SENSITIVE", re.compile(r"\bDEA SENSITIVE\b"), "latin", False),
    ("US-SCI-SLASH", re.compile(r"//\s?(SI|TK|HCS|KDK|RSV|G)(-[A-Z]+)?\b", CI), "latin", False),
    ("US-SAP", re.compile(r"\bSPECIAL ACCESS REQUIRED\b|\bSAR-[A-Z0-9]{2,}\b|\b(HVSACO|BIGOT)\b"),
     "latin", False),
    ("NATO", re.compile(
        r"\b(COSMIC TOP SECRET|NATO SECRET|NATO CONFIDENTIAL|NATO RESTRICTED|ATOMAL)\b"), "latin", False),
    ("US-CUI-SPECIFIED", re.compile(r"\bCUI//[A-Z0-9\-/]+", CI), "latin", False),
    ("US-CUI-SP", re.compile(r"\bSP-(EXPT|PROPIN|PRVCY|NOFORN|LEI)\b", CI), "latin", False),
    ("US-AUTHORITY-BLOCK", re.compile(
        r"^\s*(Classified By|Derived From|Declassify On|Downgrade To)\s*:", CI | ML), "latin", False),
    ("US-BANNER-CONTROL", re.compile(r"\b(TOP SECRET|SECRET|CONFIDENTIAL|RESTRICTED)//[A-Z0-9]{2}"),
     "latin", False),
    # export control (Tier-1 phrase / citation forms; bare ITAR/ECCN/USML are Tier-2 below)
    ("EXPORT-CFR", re.compile(r"\b22 ?CFR ?1[23]\d\b|\b15 ?CFR ?7[3-7]\d\b"), "latin", False),
    ("EXPORT-CONTROL-PHRASE", re.compile(
        r"subject to (U\.S\.|United States) export control", CI), "latin", False),
    ("EXPORT-DEEMED", re.compile(r"\bdeemed export\b", CI), "latin", False),
    ("EXPORT-DCS", re.compile(
        r"These (commodities|items|goods),? (technology|technical data),? (or|and) software "
        r"were exported from the United States", CI), "latin", False),
    # Vietnamese (diacritic-EXACT, raw surface). Full phrases are Tier-1.
    ("VN-TUYET-MAT", re.compile(r"TUYỆT\s+MẬT"), "raw", False),
    ("VN-TOI-MAT", re.compile(r"TỐI\s+MẬT"), "raw", False),
    ("VN-DAU", re.compile(r"\bDấu\s+[ABC]\b|\bĐộ mật\s*:?\s*[ABC]\b|\bDo mat\s*:?\s*[ABC]\b"),
     "raw", False),
    # CN / RU state-secret banners (raw surface, pre-fold)
    ("CN-SECRET", re.compile(r"绝密|机密|秘密"), "raw", False),
    ("RU-SECRET", re.compile(r"СЕКРЕТНО|СОВЕРШЕННО СЕКРЕТНО", CI), "raw", False),
]

# TIER-2: a candidate word; trips only with a structural corroborator (or an allowlist-free banner
# stamp accompanied by >=2 portion marks). Never trips on vocabulary alone.
_TIER2_BANNER = re.compile(r"^\s*(TOP SECRET|SECRET|CONFIDENTIAL|UNCLASSIFIED|CUI)"
                           r"(//[A-Z0-9 ,/\-]+)?\s*$", ML)
_PORTION_MARK = re.compile(r"\((TS|S|C|U|CUI)(//[A-Z0-9\-]+)?\)")
_TIER2_EXPORT = re.compile(r"\b(ITAR|ECCN|USML)\b")

# Vietnamese state-secret context anchors that license a bare ALL-CAPS MẬT stamp.
_VN_ANCHOR = re.compile(
    r"bí mật nhà nước|độ mật|Luật Bảo vệ bí mật nhà nước|Nghị định 26/2020|"
    r"Thông tư 24/2020/TT-BCA", CI)
_VN_BARE_MAT = re.compile(r"(?<![A-Za-zÀ-ỹ])MẬT(?![A-Za-zÀ-ỹ])")

# TIER-3 allowlist: pure corporate/legal markings. Suppress a Tier-2-only candidate; NEVER a Tier-1.
_ALLOWLIST = re.compile(
    r"\b(Company Confidential|Confidential and Proprietary|Private and Confidential|"
    r"Attorney[- ]Client Privileged|Attorney Work Product|Work Product|Trade Secret|"
    r"Internal Use Only|IUO|Proprietary|Confidential|"
    r"Non[- ]Disclosure|NDA)\b", CI)


# --------------------------------------------------------------------------- result type

@dataclass
class Marking:
    rule_id: str
    tier: int
    quoted_trigger: str


@dataclass
class ScanResult:
    tripped: bool
    tier: int | None
    rule_id: str | None
    quoted_trigger: str | None
    hits: list[Marking] = field(default_factory=list)
    corporate_allowlist: bool = False

    @property
    def message(self) -> str:
        # The single refusal UX string (binding ruling). NEVER "this is unclassified".
        return f"marking detected: {self.rule_id}" if self.tripped else ""

    @property
    def sensitivity_label(self) -> str:
        if self.tripped:
            return "controlled-marking" if self.tier == 1 else "possible-marking"
        return "corporate" if self.corporate_allowlist else "none"

    def escalation(self) -> dict | None:
        if not self.tripped:
            return None
        return {
            "rule_id": self.rule_id,
            "quoted_trigger": self.quoted_trigger,
            "tier": self.tier,
            "override_prompt": (
                "Refusing to ingest: a formal classification/control marking was detected "
                f"({self.rule_id}: {self.quoted_trigger!r}). This tripwire is advisory and RAISE-ONLY; "
                "a clean scan never implies a document is safe to distribute. To override, re-run "
                "with an explicit acknowledgement that you are authorized to handle marked material."
            ),
        }


# --------------------------------------------------------------------------- scanner

def scan(text: str) -> ScanResult:
    """Deterministic marking scan over a doc/block banner+body. Pure; no I/O, no probabilities."""
    base = _base(text)
    folded = _fold(base)
    despaced = _despace(folded)

    hits: list[Marking] = []

    # -- TIER-1: any single hit trips ------------------------------------------------
    for rule_id, rx, surface, also_despaced in _TIER1:
        surfaces = [base] if surface == "raw" else ([folded, despaced] if also_despaced else [folded])
        for s in surfaces:
            m = rx.search(s)
            if m:
                hits.append(Marking(rule_id, 1, m.group(0).strip()))
                break

    if hits:
        first = hits[0]
        return ScanResult(True, 1, first.rule_id, first.quoted_trigger, hits,
                          corporate_allowlist=bool(_ALLOWLIST.search(base)))

    # -- TIER-2: banner word + structural corroborator -------------------------------
    allow = bool(_ALLOWLIST.search(base))
    portion = {p[0] for p in _PORTION_MARK.findall(folded)}          # distinct portion-mark bodies
    banners = _TIER2_BANNER.findall(folded)                          # standalone ALL-CAPS banner lines
    export_tokens = {t if isinstance(t, str) else t[0] for t in _TIER2_EXPORT.findall(folded)}

    # STRONG structural signals - genuine markings that trip on their own. The Tier-3 corporate
    # allowlist NEVER suppresses these (a prose word like "sensitive" cannot cancel real structure).
    strong: list[Marking] = []
    if len(portion) >= 2:      # >=2 distinct portion marks; a lone (C)/(U) never fires
        strong.append(Marking("US-PORTION-MARKS", 2, "".join(sorted(f"({p})" for p in portion))))
    if len(export_tokens) >= 2:  # bare single ITAR/ECCN/USML stays sub-threshold; EAR99 is never here
        strong.append(Marking("EXPORT-CONTROL", 2, "/".join(sorted(export_tokens))))
    if _VN_BARE_MAT.search(base) and _VN_ANCHOR.search(base):  # bare MẬT + VN state-secret anchor
        m = _VN_BARE_MAT.search(base)
        strong.append(Marking("VN-MAT-ANCHORED", 2, m.group(0)))

    # WEAK banner candidate - an ALL-CAPS banner stamp corroborated by a repeat stamp or a portion
    # mark. This is the ONE Tier-2 path the corporate allowlist suppresses (CONFIDENTIAL is ambiguous
    # between a gov banner and a corporate stamp; "Company Confidential"/"Proprietary" => corporate).
    weak: list[Marking] = []
    banner_candidate = len(banners) >= 2 or (len(banners) >= 1 and len(portion) >= 1)
    if banner_candidate and not allow:
        weak.append(Marking("US-BANNER-STAMP", 2, banners[0][0]))

    tier2 = strong + weak
    if tier2:
        first = tier2[0]
        return ScanResult(True, 2, first.rule_id, first.quoted_trigger, tier2, corporate_allowlist=allow)

    # No formal marking recognised. (RAISE-ONLY: this is NOT an assertion of safety.)
    return ScanResult(False, None, None, None, tier2, corporate_allowlist=allow)


# --------------------------------------------------------------------------- sensitivity raise-only

_SENS_RANK = {"none": 0, "corporate": 1, "possible-marking": 2, "controlled-marking": 3}


def escalate_sensitivity(current: str | None, result: ScanResult) -> str:
    """RAISE-ONLY: return the higher of the current label and the scan's label; never lower it."""
    cur = current or "none"
    new = result.sensitivity_label
    return new if _SENS_RANK.get(new, 0) > _SENS_RANK.get(cur, 0) else cur
