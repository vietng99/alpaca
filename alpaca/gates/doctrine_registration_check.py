"""doctrine_registration_check.py - re-derive doctrine-leaf registration from disk (M4.14).

Ported from the earlier harness gates/doctrine_registration_check.py and re-based onto Alpaca. The mechanism
is generic: a doctrine leaf claims to be registered in three places - a row in the registry
(`doctrine/INDEX.md`), a line in the navigator's territory section (`MAP.md`), and a decision in
the navigator's always-load CORE section. Those are self-descriptions. This instrument re-derives
every one of them from file CONTENT and refuses to let the tree describe itself.

What Alpaca changes from the earlier harness original:

  * The re-derived cell is a DIGEST, not an honesty-vocabulary token. Alpaca leaves carry no
    `**Status:**` tag; each leaf's "status" is re-derived from its OWN BYTES as a sha256 prefix,
    and the registry cell must equal that recomputed digest or the row is a defect. This is the
    literal reading of the Done-when ("status re-derived from its own bytes") and it works for
    every leaf regardless of prose shape, including leaves shipped by other tasks.
  * The leaf population lives under `doctrine/leaves/`; the registry (`INDEX.md`) and the CORE
    card sit one level up at `doctrine/`, so the leaf scan never mistakes them for leaves.
  * I/O and the verdict<->exit-code contract are the Alpaca modules `alpaca.gates.contract` and
    `alpaca.gates.verdict`, never restated here.

The four defeats the naive form suffers, each answered by a named mechanism (kept from the earlier harness):

  R1  a scanner whose matcher structurally cannot match the shape it is aimed at certifies an
      absence it never measured. ANSWER: `prove_matchers()` runs an INDEPENDENT, broader
      harvester first; a strict matcher that cannot see a harvested shape is BLOCKED, never a
      pass; zero harvested shapes is BLOCKED.
  R2  a registry-first enumeration cannot see a leaf that has no row. ANSWER: the population is
      DERIVED FROM THE FILESYSTEM; a leaf with no row is a FAIL the registry cannot hide, and the
      reverse (row -> file must exist) is checked too.
  R3  a scan produces findings, not accountability. ANSWER: every claim carries an `instrument`
      id resolved against a registry of callables; an unbound claim is BLOCKED, never a pass.
  R4  an empty population / empty witness makes every universal vacuously true. ANSWER: an empty
      leaf population BLOCKS; a check that never adjudicated one claim positively BLOCKS.

  * a leaf on disk with no registry row      -> LEAF-NOT-REGISTERED-IN-INDEX (FAIL)
  * a registry row pointing at no leaf file   -> INDEX-ROW-TARGET-ABSENT (FAIL)
  * a registry digest cell != the leaf bytes  -> DIGEST-CELL-MISMATCH (FAIL)
  * a required CORE leaf missing from MAP      -> MAP-CORE-ROW-ABSENT / MAP-TERRITORY-LINE-ABSENT
  * an empty leaf population                   -> EMPTY-LEAF-POPULATION (BLOCKED, the floor)
  * a non-utf-8 byte in a leaf                 -> DECODE-FAILED-NOT-UTF8 (BLOCKED, never repaired)

HONEST LIMIT: this re-derives registration and byte-digests from disk. A green is "every leaf is
registered in both directions and its recorded digest matches its bytes", never "the doctrine is
correct".
"""
from __future__ import annotations

import os
import re
from collections import OrderedDict, namedtuple

from alpaca.gates import contract
from alpaca.gates import verdict as vc

INSTRUMENT = "doctrine-registration"
SCHEMA_ID = "doctrine-registration/1"

# The digest re-derived from each leaf's raw bytes, truncated for a readable registry cell. The
# truncation never weakens the binding: the recorded prefix and the recomputed prefix must be
# byte-identical, so any edit to a leaf that is not mirrored in its row is a FAIL.
DIGEST_LEN = 12

# reason tokens.
R_DECODE = "DECODE-FAILED-NOT-UTF8"
R_EMPTY_POP = "EMPTY-LEAF-POPULATION"
R_EMPTY_CLAIMS = "EMPTY-CLAIM-POPULATION"
R_NOT_REGISTERED = "LEAF-NOT-REGISTERED-IN-INDEX"
R_DUP_ROW = "DUPLICATE-INDEX-ROW"
R_DIGEST_ABSENT = "DIGEST-CELL-ABSENT"
R_DIGEST_MISMATCH = "DIGEST-CELL-MISMATCH"
R_ROW_NO_POINTER = "INDEX-ROW-CARRIES-NO-LEAF-POINTER"
R_ROW_TARGET_ABSENT = "INDEX-ROW-TARGET-ABSENT"
R_CORE_ABSENT = "MAP-CORE-ROW-ABSENT"
R_TERR_ABSENT = "MAP-TERRITORY-LINE-ABSENT"
R_MAP_SECTION_ABSENT = "MAP-SECTION-ABSENT"
R_REQUIRED_ABSENT = "REQUIRED-LEAF-ABSENT"
R_MANDATE_FILE = "MANDATE-FILE-ABSENT"
R_MANDATE_MENTION = "MANDATE-MENTION-ABSENT"
R_NO_WITNESS = "NO-REFERENCE-WITNESS"
R_UNBOUND = "CLAIM-INSTRUMENT-UNBOUND"
R_NO_CLAIM_WITNESS = "NO-CLAIM-WITNESS"
R_MATCHER_NO_WITNESS = "MATCHER-NO-WITNESS"
R_MATCHER_UNPROVEN = "MATCHER-UNPROVEN"
R_DIGEST_COLUMN = "INDEX-DIGEST-COLUMN-UNDERIVABLE"

MD = ".md"


# ===========================================================================
# 0.  strict utf-8 read (a non-utf-8 byte BLOCKs, it is never repaired into a clean read)
# ===========================================================================
def read_text_utf8(path, rel):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read()
    except UnicodeDecodeError as e:
        raise contract.ContractError(R_DECODE, "%s: %s" % (rel, e))


def _canon(s):
    return (s or "").strip().lower()


# ===========================================================================
# 1.  Matchers.  One strict matcher, one INDEPENDENT broad harvester per site.
# ===========================================================================
_PATHY = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._-")
_HARVEST = re.compile(r"[A-Za-z0-9._/\\~+-]*[A-Za-z0-9_+-]\.md\b")


def harvest_md_tokens(text):
    """Every markdown-file-shaped literal that actually occurs in `text` (the broad witness)."""
    return [m.group(0) for m in _HARVEST.finditer(text or "")]


def ref_present(text, token):
    """STRICT matcher: is `token` present as a whole path-ish literal (not glued to a longer
    identifier)? Expressed over the PATH character class so a leading separator (an absolute
    path) matches exactly as well as a bare basename."""
    if not text or not token:
        return False
    n = len(token)
    start = 0
    while True:
        i = text.find(token, start)
        if i < 0:
            return False
        start = i + 1
        before = text[i - 1] if i > 0 else ""
        after = text[i + n] if i + n < len(text) else ""
        if before in _PATHY or after in _PATHY:
            continue
        return True


def _weak_ref_present(text, token):
    """The REPRODUCED DEFECT, kept as a control subject only: a leading word-boundary anchor can
    never match a token whose first character is a non-word character."""
    return re.search(r"\b" + re.escape(token), text or "") is not None


_STRICT = {"ref_present": ref_present}


def strict_ref(text, token):
    return _STRICT["ref_present"](text, token)


# ===========================================================================
# 2.  Parsers -- registry table and navigator sections, derived from content.
# ===========================================================================
Row = namedtuple("Row", "lineno cells raw")
Section = namedtuple("Section", "number title text")

_HEADING = re.compile(r"^##+\s+(\d+)\.\s*(.*)$")


def split_sections(text):
    lines = (text or "").splitlines()
    marks = []
    for i, ln in enumerate(lines):
        m = _HEADING.match(ln)
        if m:
            marks.append((i, int(m.group(1)), m.group(2).strip()))
    out = OrderedDict()
    for k, (i, num, title) in enumerate(marks):
        j = marks[k + 1][0] if k + 1 < len(marks) else len(lines)
        out[num] = Section(num, title, "\n".join(lines[i:j]))
    return out


def split_row(raw):
    s = raw.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in s.split("|")]


def is_separator_row(cells):
    return bool(cells) and all(re.fullmatch(r":?-{2,}:?", c or "") for c in cells)


def harvest_table_lines(text):
    return [(i + 1, ln) for i, ln in enumerate((text or "").splitlines())
            if ln.strip().startswith("|")]


def parse_registry_table(text):
    """Return (header, body_rows, digest_idx, practice_idx). The digest column index is DERIVED
    from the table's own header row (a header canonicalising to 'digest' or 'status'), never
    hardcoded: reorder the columns and a hardcoded position reads the wrong cell while reporting
    green."""
    blocks = []
    cur = []
    for lineno, ln in harvest_table_lines(text):
        if cur and lineno != cur[-1][0] + 1:
            blocks.append(cur)
            cur = []
        cur.append((lineno, ln))
    if cur:
        blocks.append(cur)

    for blk in blocks:
        if len(blk) < 2:
            continue
        header = split_row(blk[0][1])
        if not is_separator_row(split_row(blk[1][1])):
            continue
        didx = None
        for k, h in enumerate(header):
            if _canon(h) in ("digest", "status"):
                didx = k
                break
        if didx is None:
            continue
        body = [Row(lineno, split_row(ln), ln) for lineno, ln in blk[2:]]
        pidx = None
        for k, h in enumerate(header):
            if _canon(h) in ("leaf", "practice", "file", "doctrine"):
                pidx = k
                break
        return header, body, didx, pidx
    return None, [], None, None


# ===========================================================================
# 3.  Claims and the instrument registry (R3: claim -> instrument, not a scan)
# ===========================================================================
Claim = namedtuple("Claim", "claim_id family subject site instrument statement")

INSTRUMENTS = {}


def instrument(name):
    def deco(fn):
        if name in INSTRUMENTS:
            raise RuntimeError("duplicate instrument id %r" % name)
        INSTRUMENTS[name] = fn
        return fn
    return deco


class Ctx(object):
    def __init__(self):
        self.root = None
        self.leaves = OrderedDict()        # basename -> {path, rel, text, digest}
        self.out_of_population = []
        self.index_path = None
        self.index_rel = None
        self.index_text = ""
        self.index_rows = []               # (Row, target_basename or None)
        self.index_by_leaf = {}            # basename -> [Row]
        self.digest_idx = None
        self.map_path = None
        self.map_rel = None
        self.map_text = ""
        self.sections = {}
        self.core_section = None
        self.territory_section = None
        self.mentions = OrderedDict()
        self.reference_corpus = OrderedDict()


@instrument("registry-row-exists")
def _i_registry_row(ctx, cl):
    rows = ctx.index_by_leaf.get(cl.subject, [])
    if not rows:
        return (vc.FAIL, R_NOT_REGISTERED,
                "%s exists on disk but no row in %s points at it (the population is derived from "
                "the filesystem precisely so the registry cannot hide this)"
                % (cl.subject, ctx.index_rel))
    if len(rows) > 1:
        return (vc.FAIL, R_DUP_ROW,
                "%s has %d registry rows (lines %s); two rows can carry two different digest "
                "cells and the check would bind to whichever it read first"
                % (cl.subject, len(rows), [r.lineno for r in rows]))
    return vc.PASS, "", "%s registered at line %d" % (cl.subject, rows[0].lineno)


@instrument("registry-digest-binds-leaf")
def _i_digest_binding(ctx, cl):
    rows = ctx.index_by_leaf.get(cl.subject, [])
    if len(rows) != 1:
        return (vc.FAIL, R_DIGEST_MISMATCH,
                "%s: cannot bind a digest cell without exactly one registry row (found %d)"
                % (cl.subject, len(rows)))
    row = rows[0]
    if ctx.digest_idx is None or ctx.digest_idx >= len(row.cells):
        return (vc.FAIL, R_DIGEST_ABSENT,
                "%s: registry row %d has no cell in the derived Digest column"
                % (cl.subject, row.lineno))
    cell = (row.cells[ctx.digest_idx] or "").strip().strip("`").lower()
    if not cell:
        return (vc.FAIL, R_DIGEST_ABSENT,
                "%s: registry Digest cell is empty; an empty cell binds to nothing" % cl.subject)
    want = ctx.leaves[cl.subject]["digest"]
    if cell != want:
        return (vc.FAIL, R_DIGEST_MISMATCH,
                "%s: registry Digest cell is %r, the leaf's own bytes re-derive %r"
                % (cl.subject, cell, want))
    return vc.PASS, "", "%s: %s" % (cl.subject, want)


@instrument("registry-pointer-resolves")
def _i_registry_pointer(ctx, cl):
    target = cl.subject
    if target is None:
        return (vc.FAIL, R_ROW_NO_POINTER,
                "registry row %s names no leaf file; a row that points at nothing is a digest "
                "claim with no subject and is never skipped silently" % cl.site)
    if target not in ctx.leaves:
        return (vc.FAIL, R_ROW_TARGET_ABSENT,
                "registry row %s points at %s, which does not exist under the leaves directory"
                % (cl.site, target))
    return vc.PASS, "", "%s -> %s" % (cl.site, target)


@instrument("navigator-core-row")
def _i_map_core(ctx, cl):
    sec = ctx.core_section
    if sec is None:
        return (vc.BLOCKED, R_MAP_SECTION_ABSENT,
                "the navigator carries no CORE section to register a decision in")
    if not strict_ref(sec.text, cl.subject):
        return (vc.FAIL, R_CORE_ABSENT,
                "%s has no CORE decision line in navigator section %d" % (cl.subject, sec.number))
    return vc.PASS, "", "%s present in navigator section %d" % (cl.subject, sec.number)


@instrument("navigator-territory-line")
def _i_map_territory(ctx, cl):
    sec = ctx.territory_section
    if sec is None:
        return (vc.BLOCKED, R_MAP_SECTION_ABSENT,
                "the navigator carries no territory section to register an invariant line in")
    if not strict_ref(sec.text, cl.subject):
        return (vc.FAIL, R_TERR_ABSENT,
                "%s has no invariant line in navigator section %d" % (cl.subject, sec.number))
    return vc.PASS, "", "%s present in navigator section %d" % (cl.subject, sec.number)


@instrument("required-leaf-on-disk")
def _i_required_leaf(ctx, cl):
    if cl.subject not in ctx.leaves:
        return (vc.FAIL, R_REQUIRED_ABSENT,
                "%s was required to be registered but does not exist under the leaves directory"
                % cl.subject)
    return vc.PASS, "", "%s on disk" % cl.subject


@instrument("mandate-mention")
def _i_mention(ctx, cl):
    text = ctx.mentions.get(cl.site)
    if text is None:
        return (vc.FAIL, R_MANDATE_FILE,
                "%s does not exist, so it cannot carry the mandate" % cl.site)
    if not strict_ref(text, cl.subject):
        return (vc.FAIL, R_MANDATE_MENTION, "%s does not reference %s" % (cl.site, cl.subject))
    return vc.PASS, "", "%s references %s" % (cl.site, cl.subject)


@instrument("reference-witness")
def _i_reference_witness(ctx, cl):
    """A verifier nothing calls is worthless: some file in the tree must name it. The
    instrument's OWN file is excluded so it cannot satisfy its own wiring requirement."""
    hits = []
    for rel, text in ctx.reference_corpus.items():
        if rel.rsplit("/", 1)[-1] == cl.subject:
            continue
        if strict_ref(text, cl.subject):
            hits.append(rel)
    if not hits:
        return (vc.FAIL, R_NO_WITNESS,
                "no file under the root references %s; an instrument nothing calls cannot be a "
                "gate condition (empty witness set is never a pass)" % cl.subject)
    return vc.PASS, "", "%s referenced by %s" % (cl.subject, hits[:4])


# ===========================================================================
# 4.  Matcher proof (R1) -- runs BEFORE adjudication, blocks on failure
# ===========================================================================
Proof = namedtuple("Proof", "proof_id site witnesses failures")


def prove_matchers(ctx):
    proofs = []
    problems = []

    def _prove(pid, site, text):
        toks = sorted(set(harvest_md_tokens(text)))
        bad = [t for t in toks if not strict_ref(text, t)]
        proofs.append(Proof(pid, site, len(toks), bad))
        if not toks:
            problems.append((vc.BLOCKED, R_MATCHER_NO_WITNESS,
                             "%s: the independent harvester found no leaf-shaped literal in %s, "
                             "so the strict matcher was never exercised there; an unexercised "
                             "matcher cannot certify an absence" % (pid, site)))
        elif bad:
            problems.append((vc.BLOCKED, R_MATCHER_UNPROVEN,
                             "%s: the strict matcher cannot see %d literal shape(s) the "
                             "independent harvester found in %s: %s -- a matcher that cannot "
                             "match what is there certifies absence it never measured"
                             % (pid, len(bad), site, bad[:4])))

    _prove("M-REGISTRY-REF", ctx.index_rel, ctx.index_text)
    if ctx.core_section is not None:
        _prove("M-NAV-CORE-REF", "navigator section %d" % ctx.core_section.number,
               ctx.core_section.text)
    if ctx.territory_section is not None:
        _prove("M-NAV-TERRITORY-REF", "navigator section %d" % ctx.territory_section.number,
               ctx.territory_section.text)

    header, body, didx, _ = parse_registry_table(ctx.index_text)
    if header is None:
        problems.append((vc.BLOCKED, R_DIGEST_COLUMN,
                         "no table in %s carries a column whose header is 'Digest'; the digest "
                         "column index must be derived from the table's own header, never "
                         "hardcoded" % ctx.index_rel))
    else:
        ragged = [(r.lineno, len(r.cells)) for r in body if len(r.cells) != len(header)]
        proofs.append(Proof("M-REGISTRY-ROWS", ctx.index_rel, len(body), ragged))
        if not body:
            problems.append((vc.BLOCKED, R_MATCHER_NO_WITNESS,
                             "M-REGISTRY-ROWS: the registry table has no body row; a registry "
                             "with nothing in it satisfies every universal vacuously"))
        if ragged:
            problems.append((vc.BLOCKED, R_MATCHER_UNPROVEN,
                             "M-REGISTRY-ROWS: %d registry row(s) do not parse to the header's "
                             "%d columns %s -- a row the parser drops is a claim nobody checked"
                             % (len(ragged), len(header), ragged[:4])))
    return proofs, problems


# ===========================================================================
# 5.  Derivation + run
# ===========================================================================
def build_ctx(root, leaves_dir, index_rel, map_rel, core_no, territory_no, mention_files):
    ctx = Ctx()
    ctx.root = os.path.abspath(root)
    if not os.path.isdir(ctx.root):
        raise contract.ContractError("ROOT-ABSENT", ctx.root)

    ldir = os.path.join(ctx.root, leaves_dir.replace("/", os.sep))
    if not os.path.isdir(ldir):
        raise contract.ContractError("LEAVES-DIR-ABSENT", "%s" % leaves_dir)

    # --- population DERIVED from the filesystem (R2) ---
    for name in sorted(os.listdir(ldir)):
        full = os.path.join(ldir, name)
        rel = "%s/%s" % (leaves_dir, name)
        if os.path.isdir(full):
            ctx.out_of_population.append((rel, "a subdirectory of the leaves directory"))
            continue
        if not name.lower().endswith(MD):
            ctx.out_of_population.append((rel, "not a markdown leaf"))
            continue
        text = read_text_utf8(full, rel)      # a non-utf-8 byte raises -> BLOCKED
        ctx.leaves[name] = {
            "path": full, "rel": rel, "text": text,
            "digest": contract.sha256_bytes(full)[:DIGEST_LEN].lower(),
        }

    ctx.index_rel = index_rel
    ctx.index_path = os.path.join(ctx.root, index_rel.replace("/", os.sep))
    if not os.path.isfile(ctx.index_path):
        raise contract.ContractError("INDEX-ABSENT", index_rel)
    ctx.index_text = read_text_utf8(ctx.index_path, index_rel)

    header, body, didx, pidx = parse_registry_table(ctx.index_text)
    ctx.digest_idx = didx
    for row in body:
        target = None
        cell_pool = row.cells[pidx] if (pidx is not None and pidx < len(row.cells)) else row.raw
        for tok in harvest_md_tokens(cell_pool) or harvest_md_tokens(row.raw):
            base = tok.replace("\\", "/").rsplit("/", 1)[-1]
            target = base
            break
        ctx.index_rows.append((row, target))
        if target:
            ctx.index_by_leaf.setdefault(target, []).append(row)

    ctx.map_rel = map_rel
    ctx.map_path = os.path.join(ctx.root, map_rel.replace("/", os.sep))
    if not os.path.isfile(ctx.map_path):
        raise contract.ContractError("MAP-ABSENT", map_rel)
    ctx.map_text = read_text_utf8(ctx.map_path, map_rel)
    ctx.sections = split_sections(ctx.map_text)
    ctx.core_section = ctx.sections.get(core_no)
    ctx.territory_section = ctx.sections.get(territory_no)

    for rel in mention_files:
        p = os.path.join(ctx.root, rel.replace("/", os.sep))
        ctx.mentions[rel] = read_text_utf8(p, rel) if os.path.isfile(p) else None

    # Corpus for the reference-witness instrument: every text file under the root.
    for dirpath, dirnames, filenames in os.walk(ctx.root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".") and d != "__pycache__"]
        for fn in sorted(filenames):
            if not fn.lower().endswith((MD, ".py", ".sh", ".json", ".txt", ".html", ".yaml")):
                continue
            fp = os.path.join(dirpath, fn)
            rel = os.path.relpath(fp, ctx.root).replace(os.sep, "/")
            try:
                ctx.reference_corpus[rel] = read_text_utf8(fp, rel)
            except contract.ContractError:
                ctx.reference_corpus[rel] = ""
    return ctx


def derive_claims(ctx, require_map, mention_files, self_name):
    claims = []
    for base in ctx.leaves:
        claims.append(Claim("REG-ROW/%s" % base, "leaf", base, ctx.leaves[base]["rel"],
                            "registry-row-exists", "the leaf on disk has exactly one registry row"))
        claims.append(Claim("REG-DIGEST/%s" % base, "leaf", base, ctx.leaves[base]["rel"],
                            "registry-digest-binds-leaf",
                            "the registry digest cell equals the leaf's own re-derived digest"))
    for row, target in ctx.index_rows:
        claims.append(Claim("REG-PTR/L%d" % row.lineno, "registry", target,
                            "registry row %d" % row.lineno, "registry-pointer-resolves",
                            "the registry row points at a leaf that exists"))
    for base in require_map:
        claims.append(Claim("REQ-LEAF/%s" % base, "required", base, base,
                            "required-leaf-on-disk", "the required CORE leaf exists"))
        claims.append(Claim("NAV-CORE/%s" % base, "required", base, base,
                            "navigator-core-row", "the CORE leaf carries a CORE decision"))
        claims.append(Claim("NAV-TERR/%s" % base, "required", base, base,
                            "navigator-territory-line", "the CORE leaf carries a territory line"))
        for rel in mention_files:
            claims.append(Claim("MANDATE/%s/%s" % (rel, base), "mandate", base, rel,
                                "mandate-mention", "the mandate file references the leaf"))
    claims.append(Claim("WIRED/%s" % self_name, "wiring", self_name, "<root>",
                        "reference-witness", "some file under the root names this instrument"))
    return claims


Result = namedtuple("Result", "verdict reasons info")

CORE_SET = (
    "file-as-truth.md",
    "provenance-or-die.md",
    "test-or-UNTESTED.md",
    "verification-tags.md",
    "halt-on-drift.md",
    "crash-only-resume.md",
    "double-dispatch.md",
    "movement-gate-pin-sentinel.md",
    "record-over-ceremony.md",
    "board-is-truth.md",
)


def run_check(root, leaves_dir="doctrine/leaves", index_rel="doctrine/INDEX.md", map_rel="MAP.md",
              core_no=2, territory_no=4, require_map=CORE_SET,
              mention_files=("doctrine/CORE-CARD.md",), self_name=None, report=None):
    """Adjudicate. Returns Result. Never raises for a subject-state problem."""
    lines = report if report is not None else []
    self_name = self_name or os.path.basename(os.path.abspath(__file__))
    try:
        ctx = build_ctx(root, leaves_dir, index_rel, map_rel, core_no, territory_no,
                        list(mention_files))
    except contract.ContractError as e:
        return Result(vc.BLOCKED, ["%s: %s" % (e.reason_code, e.detail)], {})

    info = {"schema": SCHEMA_ID, "leaves": len(ctx.leaves),
            "registry_rows": len(ctx.index_rows)}
    verdicts, reasons = [], []

    for rel, why in ctx.out_of_population:
        lines.append("  out-of-population  %s  (%s)" % (rel, why))
    info["out_of_population"] = [r for r, _ in ctx.out_of_population]

    # --- CL-EMPTY-INPUT-PASS: an empty population is never a pass ---
    if not ctx.leaves:
        return Result(vc.BLOCKED, ["%s: the leaves directory holds no leaf file, so every 'all "
                                   "leaves are registered' claim is vacuously true and must not "
                                   "pass" % R_EMPTY_POP], info)

    # --- R1: prove the matchers before believing any of their answers ---
    proofs, problems = prove_matchers(ctx)
    lines.append("")
    lines.append("  MATCHER PROOF (strict matcher vs an independent harvester, on this tree)")
    for p in proofs:
        state = "PROVEN" if (p.witnesses and not p.failures) else "UNPROVEN"
        lines.append("    %-22s %-34s witnesses=%-4d %s" % (p.proof_id, p.site, p.witnesses, state))
    for v, code, detail in problems:
        verdicts.append(v)
        reasons.append("%s: %s" % (code, detail))
    if vc.BLOCKED in verdicts:
        return Result(contract.worst(verdicts), reasons, info)

    # --- R3: claim -> instrument binding, printed, and unbound is BLOCKED ---
    claims = derive_claims(ctx, list(require_map), list(mention_files), self_name)
    info["claims"] = len(claims)
    if not claims:
        return Result(vc.BLOCKED, ["%s: nothing was derived to check" % R_EMPTY_CLAIMS], info)

    bind = OrderedDict()
    for cl in claims:
        bind.setdefault(cl.instrument, 0)
        bind[cl.instrument] += 1
    lines.append("")
    lines.append("  CLAIM -> INSTRUMENT BINDING (derived, %d claim(s))" % len(claims))
    for iid, n in bind.items():
        lines.append("    %-30s %-9s x%d" % (iid, "BOUND" if iid in INSTRUMENTS else "UNBOUND", n))

    lines.append("")
    lines.append("  ADJUDICATION")
    passed = 0
    for cl in claims:
        fn = INSTRUMENTS.get(cl.instrument)
        if fn is None:
            verdicts.append(vc.BLOCKED)
            reasons.append("%s: claim %s names instrument %r, which no callable implements; an "
                           "unbound claim is never adjudicated and must never fall through to a "
                           "pass" % (R_UNBOUND, cl.claim_id, cl.instrument))
            lines.append("    BLOCKED  %-34s instrument unbound" % cl.claim_id)
            continue
        v, code, detail = fn(ctx, cl)
        verdicts.append(v)
        if v == vc.PASS:
            passed += 1
            lines.append("    ok       %-34s %s" % (cl.claim_id, detail))
        else:
            reasons.append("%s: %s" % (code, detail))
            lines.append("    %-8s %-34s %s" % (vc.name_of(v), cl.claim_id, code))

    info["passed"] = passed
    if passed == 0:
        verdicts.append(vc.BLOCKED)
        reasons.append("%s: not one claim was adjudicated positively; a check that only ever "
                       "refuses is not evidence that anything is registered" % R_NO_CLAIM_WITNESS)
    return Result(contract.worst(verdicts), reasons, info)


def check(root) -> int:
    """The uniform instrument entry. Runs the Alpaca-default registration adjudication over `root`."""
    return run_check(root).verdict


# ===========================================================================
# 6.  Selftest -- every control binds to a named failure branch + exact reason
# ===========================================================================
GOOD_LEAF = "# {title}\n\n## The rule\n\nBody for {title}.\n\n## Instrument\n\n`alpaca/posture/x.py`\n"


def _digest_of(text):
    import hashlib
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:DIGEST_LEN].lower()


def _good_tree(self_name):
    alpha = GOOD_LEAF.format(title="Alpha")
    beta = GOOD_LEAF.format(title="Beta")
    index = ("# Registry\n\nThe instrument `alpaca/gates/%s` re-derives every row.\n\n"
             "| # | Leaf | One line | Digest |\n|---|---|---|---|\n"
             "| 1 | [alpha](leaves/alpha.md) | first | %s |\n"
             "| 2 | [beta](leaves/beta.md) | second | %s |\n"
             % (self_name, _digest_of(alpha), _digest_of(beta)))
    core_card = ("# CORE-CARD\n\nThe always-load set: `doctrine/leaves/beta.md`.\n")
    mp = ("# Navigator\n\n## 1. Boot read-order\n\ntext\n\n## 2. The core set\n\n"
          "| # | Leaf | Why |\n|---|---|---|\n"
          "| 1 | `doctrine/leaves/alpha.md` | core |\n"
          "| 2 | `doctrine/leaves/beta.md` | core |\n\n"
          "## 3. Router\n\nrole x phase\n\n## 4. Territory\n\n"
          "- `doctrine/leaves/alpha.md` first.\n- `doctrine/leaves/beta.md` second.\n")
    return {
        "doctrine/leaves/alpha.md": alpha,
        "doctrine/leaves/beta.md": beta,
        "doctrine/INDEX.md": index,
        "doctrine/CORE-CARD.md": core_card,
        "MAP.md": mp,
    }


def _mk(root, files):
    for rel, content in files.items():
        p = os.path.join(root, rel.replace("/", os.sep))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        if isinstance(content, bytes):
            with open(p, "wb") as fh:
                fh.write(content)
        else:
            with open(p, "w", encoding="utf-8") as fh:
                fh.write(content)
    return root


def selftest() -> int:
    import tempfile
    self_name = os.path.basename(os.path.abspath(__file__))
    base = tempfile.mkdtemp(prefix="doctreg-selftest-")
    rows = []
    failures = 0
    seq = [0]

    def run(mutate, require_map=("beta.md",), mention=("doctrine/CORE-CARD.md",)):
        seq[0] += 1
        files = _good_tree(self_name)
        mutate(files)
        root = os.path.join(base, "t%02d" % seq[0])
        os.makedirs(root, exist_ok=True)
        _mk(root, files)
        return run_check(root, require_map=require_map, mention_files=mention, self_name=self_name)

    def ctl(cid, branch, mutate, want_verdict, want_reason, **kw):
        nonlocal failures
        res = run(mutate, **kw)
        hit = [r for r in res.reasons if want_reason in r]
        ok = (res.verdict == want_verdict) and (bool(hit) or want_reason == "")
        if not ok:
            failures += 1
        observed = "%s | %s" % (vc.name_of(res.verdict),
                                (hit[0] if hit else (res.reasons[0] if res.reasons
                                                     else "<no reason>")))
        rows.append((cid, branch, "FIRED" if ok else "DID-NOT-FIRE", observed[:150]))
        return res

    def m_missing_registration(f):
        f["doctrine/INDEX.md"] = "\n".join(
            ln for ln in f["doctrine/INDEX.md"].splitlines() if "beta.md" not in ln) + "\n"
    ctl("D-01", "a leaf on disk with NO registry row",
        m_missing_registration, vc.FAIL, R_NOT_REGISTERED)

    def m_digest_mismatch(f):
        f["doctrine/INDEX.md"] = f["doctrine/INDEX.md"].replace(
            _digest_of(GOOD_LEAF.format(title="Beta")), "deadbeefcafe")
    ctl("D-02", "registry digest cell contradicts the leaf's own bytes",
        m_digest_mismatch, vc.FAIL, R_DIGEST_MISMATCH)

    def m_no_core(f):
        f["MAP.md"] = f["MAP.md"].replace("| 2 | `doctrine/leaves/beta.md` | core |\n", "")
    ctl("D-03", "no CORE decision in the navigator's always-load section",
        m_no_core, vc.FAIL, R_CORE_ABSENT)

    def m_no_territory(f):
        f["MAP.md"] = f["MAP.md"].replace("- `doctrine/leaves/beta.md` second.\n", "")
    ctl("D-04", "no invariant line in the navigator's territory section",
        m_no_territory, vc.FAIL, R_TERR_ABSENT)

    def m_dangling_row(f):
        f["doctrine/INDEX.md"] = f["doctrine/INDEX.md"].replace(
            "[beta](leaves/beta.md)", "[gamma](leaves/gamma.md)")
        f.pop("doctrine/leaves/beta.md")
    ctl("D-05", "registry row pointing at a leaf that does not exist",
        m_dangling_row, vc.FAIL, R_ROW_TARGET_ABSENT)

    def m_empty(f):
        f.pop("doctrine/leaves/alpha.md")
        f.pop("doctrine/leaves/beta.md")
        f["doctrine/leaves/README.txt"] = "the leaves dir exists but holds no markdown leaf\n"
    ctl("D-06", "empty leaf population must BLOCK, never pass vacuously",
        m_empty, vc.BLOCKED, R_EMPTY_POP)

    def m_ragged_row(f):
        f["doctrine/INDEX.md"] = f["doctrine/INDEX.md"] + "| 3 | [beta](leaves/beta.md) | ragged |\n"
    ctl("D-07", "a registry row the parser cannot parse is never skipped",
        m_ragged_row, vc.BLOCKED, R_MATCHER_UNPROVEN)

    def m_no_digest_column(f):
        f["doctrine/INDEX.md"] = f["doctrine/INDEX.md"].replace("| Digest |", "| Notes |")
    ctl("D-08", "digest column index must be derivable from the table's own header",
        m_no_digest_column, vc.BLOCKED, R_DIGEST_COLUMN)

    def m_dup_row(f):
        f["doctrine/INDEX.md"] = f["doctrine/INDEX.md"] + \
            ("| 3 | [beta](leaves/beta.md) | again | %s |\n"
             % _digest_of(GOOD_LEAF.format(title="Beta")))
    ctl("D-09", "two registry rows for one leaf (two digest cells, one leaf)",
        m_dup_row, vc.FAIL, R_DUP_ROW)

    def m_no_mandate(f):
        f["doctrine/CORE-CARD.md"] = "# CORE-CARD\n\nNothing about any leaf here.\n"
    ctl("D-10", "the mandate file does not reference the leaf",
        m_no_mandate, vc.FAIL, R_MANDATE_MENTION)

    def m_required_absent(f):
        f.pop("doctrine/leaves/beta.md")
        f["doctrine/INDEX.md"] = "\n".join(
            ln for ln in f["doctrine/INDEX.md"].splitlines() if "beta.md" not in ln) + "\n"
        f["MAP.md"] = f["MAP.md"].replace("| 2 | `doctrine/leaves/beta.md` | core |\n", "") \
                                 .replace("- `doctrine/leaves/beta.md` second.\n", "")
        f["doctrine/CORE-CARD.md"] = "# CORE-CARD\n\nThe always-load set is empty here.\n"
    ctl("D-11", "a leaf required to be registered that is not on disk",
        m_required_absent, vc.FAIL, R_REQUIRED_ABSENT)

    def m_bad_encoding(f):
        f["doctrine/leaves/beta.md"] = ("# Beta\n\n".encode("utf-8") + b"\xff\xfe raw\n")
    ctl("D-12", "a non-utf-8 byte in a leaf BLOCKs (never repaired into a clean read)",
        m_bad_encoding, vc.BLOCKED, R_DECODE)

    # ---- R1: the reproduced matcher defect must be caught ----
    seq[0] += 1
    files = _good_tree(self_name)
    files["MAP.md"] = files["MAP.md"].replace("- `doctrine/leaves/beta.md` second.",
                                              "- `/doctrine/leaves/beta.md` second.")
    weak_root = os.path.join(base, "t%02d" % seq[0])
    os.makedirs(weak_root, exist_ok=True)
    _mk(weak_root, files)
    _STRICT["ref_present"] = _weak_ref_present
    try:
        res = run_check(weak_root, require_map=("beta.md",), self_name=self_name)
    finally:
        _STRICT["ref_present"] = ref_present
    hit = [r for r in res.reasons if R_MATCHER_UNPROVEN in r]
    ok = res.verdict == vc.BLOCKED and bool(hit)
    if not ok:
        failures += 1
    rows.append(("D-13", "a leading-word-boundary matcher cannot see an absolute-path literal",
                 "FIRED" if ok else "DID-NOT-FIRE",
                 ("%s | %s" % (vc.name_of(res.verdict), hit[0] if hit else res.reasons[:1]))[:150]))

    # the SAME tree with the real matcher must NOT report MATCHER-UNPROVEN.
    res_real = run_check(weak_root, require_map=("beta.md",), self_name=self_name)
    ok_real = not any(R_MATCHER_UNPROVEN in r for r in res_real.reasons)
    if not ok_real:
        failures += 1
    rows.append(("D-14", "the same tree with the real matcher is NOT reported unproven",
                 "FIRED" if ok_real else "DID-NOT-FIRE",
                 "%s | %s" % (vc.name_of(res_real.verdict), res_real.reasons[:1])))

    # ---- unbound instrument must BLOCK, not fall through ----
    seq[0] += 1
    root_u = os.path.join(base, "t%02d" % seq[0])
    os.makedirs(root_u, exist_ok=True)
    _mk(root_u, _good_tree(self_name))
    _saved = INSTRUMENTS.pop("navigator-core-row")
    try:
        res_u = run_check(root_u, require_map=("beta.md",), self_name=self_name)
    finally:
        INSTRUMENTS["navigator-core-row"] = _saved
    hit_u = [r for r in res_u.reasons if R_UNBOUND in r]
    ok_u = res_u.verdict == vc.BLOCKED and bool(hit_u)
    if not ok_u:
        failures += 1
    rows.append(("D-15", "a claim whose instrument is unbound BLOCKs (never falls through)",
                 "FIRED" if ok_u else "DID-NOT-FIRE",
                 ("%s | %s" % (vc.name_of(res_u.verdict), hit_u[0] if hit_u else res_u.reasons[:1]))[:150]))

    # ---- POSITIVE CONTROL: a conformant tree must PASS ----
    seq[0] += 1
    root_ok = os.path.join(base, "t%02d" % seq[0])
    os.makedirs(root_ok, exist_ok=True)
    _mk(root_ok, _good_tree(self_name))
    res_ok = run_check(root_ok, require_map=("beta.md",), self_name=self_name)
    okp = res_ok.verdict == vc.PASS
    if not okp:
        failures += 1
    rows.append(("D-16", "positive control: a fully conformant tree still PASSes",
                 "FIRED" if okp else "DID-NOT-FIRE",
                 "%s | %s" % (vc.name_of(res_ok.verdict), res_ok.reasons[:2])))

    w = max(len(r[1]) for r in rows)
    print("CONTROL TABLE -- %s (every row generated from an executed run)" % SCHEMA_ID)
    for cid, branch, state, observed in rows:
        print("  %-5s %-*s %-12s %s" % (cid, w, branch, state, observed))
    print("  %d control(s), %d did not fire" % (len(rows), failures))
    v = vc.FAIL if failures else vc.PASS
    return vc.emit_verdict(INSTRUMENT + "-selftest", v,
                           "%d control(s) did not fire" % failures if failures else
                           "every control fired")


# ===========================================================================
# 7.  CLI
# ===========================================================================
def main(argv=None) -> int:
    ap = vc.make_parser(
        name=INSTRUMENT,
        description="Re-derive doctrine-leaf registration from file content: every leaf under the "
                    "leaves directory has exactly one registry row, every row points at a real "
                    "leaf, and every digest cell equals the leaf's own re-derived bytes.")
    ap.add_argument("--root", default=None, help="the harness tree to check")
    ap.add_argument("--leaves-dir", default="doctrine/leaves",
                    help="directory holding the leaves, relative to --root")
    ap.add_argument("--index", default="doctrine/INDEX.md", help="registry file, relative to --root")
    ap.add_argument("--map", dest="map_rel", default="MAP.md",
                    help="navigator file, relative to --root")
    ap.add_argument("--core-section", type=int, default=2)
    ap.add_argument("--territory-section", type=int, default=4)
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    from alpaca import paths
    root = a.root or paths.root()
    report = []
    res = run_check(root, leaves_dir=a.leaves_dir, index_rel=a.index, map_rel=a.map_rel,
                    core_no=a.core_section, territory_no=a.territory_section, report=report)
    for ln in report:
        print(ln)
    return vc.emit_verdict(INSTRUMENT, res.verdict, res.reasons[0] if res.reasons else "registered")


if __name__ == "__main__":
    import sys
    sys.exit(main())
