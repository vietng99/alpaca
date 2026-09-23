"""lessons.* - procedural self-critique write-gate, ported from the earlier harness ops/lessons_store.py and
retargeted at the wiki tables (a sqlite conn) instead of the append-only war-log JSONL.

Kept faithfully from the upstream port (the mechanism, not the storage):
  1. a write-gate on the propose path: a discriminating probe run() over held-out corpus instances;
  2. a retention/decay recheck riding the either-direction HALT rule;
  3. a block schema, projected onto a conn-backed append-only log table (never a parallel store).

H7 (domain-knowledge-store): lessons are procedural self-critique ONLY. Declarative domain facts
are knowledge.* territory and are rejected here on sight.

THE VACUOUS-REWARD HAZARD. A weak or constant probe green-lights everything, defeating the gate.
So the probe demands DISCRIMINATION, not approval: a candidate declares which situation-tags it
governs and a predicate over actions, and it is scored on held-out corpus instances. It must, on
out-of-sample data, correctly classify EVERY held-out PASS and FAIL instance, with at least one of
each. A constant lesson ("always be careful") lands all its predictions on one side and cannot
clear both halves. That is the anti-vacuity property, a structural consequence of the scoring rule.

coverage>=1 fails safe: no corpus, no matching held-out instance, or an unreadable probe means NOT
ADMITTED. Never a silent pass-through on an unavailable gate. Interface: lessons.write(conn, lesson,
probe) admits only on entailment AND a discriminating probe, else raises RejectedLesson.

Stdlib only.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import re
import sqlite3
from pathlib import Path

CHAR_LIMIT = 400  # a lesson is a terse prior, not an essay


class RejectedLesson(Exception):
    def __init__(self, reason: str, control: str = ""):
        super().__init__(reason)
        self.reason = reason
        self.control = control


class RetentionHalt(Exception):
    """A retained lesson's frozen probe flipped. HALT + stuck-report, never a silent retire."""


# H7 - a lesson that states what is TRUE OF THE DOMAIN is a knowledge claim wearing a lesson's
# clothes. These markers catch the common shapes.
_DECLARATIVE = re.compile(
    r"\b(the (?:dut|design|subject|core|cpu|rtl|unit|block)\b|"
    r"is implemented|is not implemented|has \d+ entries|reset value|"
    r"granularity is|supports? (?:na4|sv39|tor)|opcode 0x)",
    re.I,
)


def _is_declarative(text: str):
    m = _DECLARATIVE.search(text or "")
    return (True, m.group(0)) if m else (False, "")


def _normalize_corpus(probe):
    """The probe is the held-out corpus: a list of {id, tag, outcome, action} instances (or a path
    to a jsonl file of them). Returns the list; a missing/empty probe yields [] (fails safe)."""
    if probe is None:
        return []
    if isinstance(probe, (str, Path)):
        p = Path(probe)
        if not p.exists():
            return []
        out = []
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return out
    return list(probe)


def _split_out_of_sample(corpus, governs, holdout_frac=0.5):
    """Stratified split. The probe scores ONLY on held-out instances.

    Stratification is by (tag, outcome) so a held-out fold cannot end up all-PASS or all-FAIL,
    which would silently make the both-halves requirement unsatisfiable and turn a fail-safe into a
    fail-shut."""
    matching = [c for c in corpus if c.get("tag") in governs]
    strata: dict = {}
    for c in matching:
        strata.setdefault((c.get("tag"), c.get("outcome")), []).append(c)
    held, seen = [], []
    for _, items in sorted(strata.items()):
        items = sorted(items, key=lambda x: x.get("id", ""))
        n_hold = max(1, int(len(items) * holdout_frac))
        held.extend(items[:n_hold])
        seen.extend(items[n_hold:])
    return held, seen


def _ensure_table(conn: sqlite3.Connection) -> None:
    """Create the conn-backed lessons log table (additive, idempotent). This is the wiki-table
    retarget of the upstream war-log JSONL: an append-only projection, never a parallel store."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS lessons_log ("
        "seq INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, event TEXT NOT NULL, "
        "id TEXT NOT NULL, block_json TEXT, supersedes TEXT, reward INTEGER, control TEXT, "
        "reason TEXT, run_id TEXT)"
    )


class LessonsStore:
    def __init__(self, conn: sqlite3.Connection, corpus=None, roots=None):
        self.conn = conn
        self.corpus = _normalize_corpus(corpus)
        self.roots = [Path(r) for r in (roots or [Path.cwd()])]
        _ensure_table(self.conn)

    # -- the probe (external oracle; never the proposer's self-vote) ------

    def _probe(self, governs, prescribes, corpus=None):
        """Score a candidate on held-out instances. Returns (reward, detail)."""
        corpus = corpus if corpus is not None else self.corpus
        if not corpus:
            return 0, "coverage=0: probe corpus absent or empty - fails safe, NOT admitted"

        held, seen = _split_out_of_sample(corpus, set(governs))
        if held and not seen:
            return 0, (
                f"coverage=0: no IN-SAMPLE fold. Every matching instance landed in the held-out "
                f"set ({len(held)} held / 0 seen), so there is no out-of-sample property to test - "
                f"the corpus needs >=2 instances per (tag, outcome). Failing closed rather than "
                f"reporting a discrimination that was not measured")
        if not held:
            return 0, (
                f"coverage=0: no held-out instance matches governs={sorted(governs)} "
                "- fails safe, NOT admitted")

        # A predicate must be a PRESCRIPTION, not a lucky character.
        bare = re.sub(r"[\\^$.|?*+()\[\]{}]", "", prescribes or "").strip()
        if len(bare) < 6:
            return 0, (f"prescribes {prescribes!r} is too thin to be a prescription "
                       f"({len(bare)} literal chars) - a lesson must name an action shape, "
                       f"not a character that happens to correlate")
        for inst in corpus:
            act = inst.get("action", "")
            if bare.lower() in act.lower() and len(bare) > 0.6 * len(act):
                return 0, ("prescribes reproduces a corpus action almost verbatim - that is "
                           "memorisation, not a generalisable prior")

        try:
            pred = re.compile(prescribes, re.I)
        except re.error as e:
            return 0, f"prescribes predicate is not a valid pattern: {e}"

        right_pass = right_fail = 0
        wrong = []
        for inst in held:
            matched = bool(pred.search(inst.get("action", "")))
            predicted = "PASS" if matched else "FAIL"
            actual = inst.get("outcome")
            if predicted == actual:
                if actual == "PASS":
                    right_pass += 1
                else:
                    right_fail += 1
            else:
                wrong.append(f"{inst.get('id')}(said {predicted}, was {actual})")

        n_pass = sum(1 for i in held if i.get("outcome") == "PASS")
        n_fail = len(held) - n_pass
        if right_pass == n_pass and right_fail == n_fail and n_pass and n_fail:
            return 1, (
                f"discriminates out-of-sample on {len(held)} held-out: "
                f"{right_pass} PASS-correct, {right_fail} FAIL-correct"
                + (f"; missed {', '.join(wrong)}" if wrong else ""))
        return 0, (
            f"does not discriminate: {right_pass} PASS-correct, {right_fail} FAIL-correct "
            f"on {len(held)} held-out (need to be right on every held-out, >=1 of each - a lesson "
            f"that cannot separate a good action from a bad one is a slogan)"
            + (f"; missed {', '.join(wrong)}" if wrong else ""))

    # -- the entailment gate (composes; BOTH must pass) -------------------

    def _entailment(self, value, provenance):
        """Does the lesson actually follow from what it cites? Entailment runs first (cheaper)."""
        if not provenance:
            return False, "no provenance pointer"
        m = re.search(r"([A-Za-z0-9_./\-]+\.[A-Za-z0-9_]+)(?::(\d+))?", provenance)
        if not m:
            return False, f"provenance {provenance!r} names no resolvable file"
        raw = m.group(1)
        for root in self.roots:
            cand = root / raw
            if cand.exists() and cand.is_file():
                return True, f"grounding resolves: {cand}"
        p = Path(raw)
        if p.exists() and p.is_file():
            return True, f"grounding resolves: {p}"
        return False, f"provenance path {raw!r} does not resolve - ungrounded lesson"

    # -- write path ------------------------------------------------------

    def propose(self, label, description, value, provenance, governs, prescribes,
                run_id="", proposer="analyst"):
        """lessons.propose(candidate). Admits only on entailment AND a discriminating probe."""
        if len(value) > CHAR_LIMIT:
            raise RejectedLesson(
                f"value is {len(value)} chars, cap is {CHAR_LIMIT} - a lesson is a terse "
                "prior, not an essay",
                control="NC-char-limit")

        decl, hit = _is_declarative(f"{description} {value}")
        if decl:
            raise RejectedLesson(
                f"H7 violation: {hit!r} reads as a declarative domain fact. Procedural "
                "self-critique only; route this to knowledge.record",
                control="H7")

        ok, detail = self._entailment(value, provenance)
        if not ok:
            self._log("lesson-reject", label, control="NC-entailment", reason=detail, run_id=run_id)
            raise RejectedLesson(detail, control="NC-entailment")

        reward, pdetail = self._probe(governs, prescribes)
        if reward != 1:
            self._log("lesson-reject", label, control="NC-reward", reason=pdetail, run_id=run_id)
            raise RejectedLesson(pdetail, control="NC-reward")

        lesson_id = self._mint_id(label)
        sig = self._signature(governs, prescribes)
        block = {
            "id": lesson_id,
            "label": label,
            "description": description,
            "value": value,
            "char_limit": CHAR_LIMIT,
            "read_only": True,
            "provenance": provenance,
            "admission_probe_sig": sig,
            "governs": sorted(governs),
            "prescribes": prescribes,
        }
        self._log("lesson-admit", lesson_id, block=block, reward=reward, run_id=run_id)
        return block

    # -- retention (Sentinel, either-direction HALT) ----------------------

    def recheck(self, lesson_id=None, corpus=None):
        """Re-run each retained lesson's frozen probe. A GREEN->FAIL flip HALTs for owner review;
        it is never silently retired and never silently kept."""
        corpus = self.corpus if corpus is None else _normalize_corpus(corpus)
        checked, halts = [], []
        for block in self._retained():
            if lesson_id and block["id"] != lesson_id:
                continue
            reward, detail = self._probe(block["governs"], block["prescribes"], corpus)
            now_sig = self._signature(block["governs"], block["prescribes"], reward)
            frozen = block["admission_probe_sig"]
            checked.append((block["id"], reward))
            if now_sig != frozen:
                self._log("lesson-signature-drift", block["id"], reason=f"{frozen} -> {now_sig}")
                halts.append((block["id"], f"admission signature changed {frozen} -> {now_sig}"))
                continue
            if reward != 1:
                self._log("lesson-retention-halt", block["id"], reason=detail)
                halts.append((block["id"], detail))
        if halts:
            raise RetentionHalt(
                "retention probe flipped GREEN->FAIL for: "
                + "; ".join(f"{i} ({d})" for i, d in halts)
                + " - HALT + stuck-report for owner review (halt-on-drift)")
        return checked

    def supersede(self, lesson_id, new_label, **kw):
        """read_only: a lesson is amended by superseding, never mutated in place."""
        block = self.propose(label=new_label, **kw)
        self._log("lesson-supersede", block["id"], supersedes=lesson_id)
        return block

    # -- helpers ---------------------------------------------------------

    def _retained(self):
        out = {}
        for row in self.conn.execute(
            "SELECT id, block_json FROM lessons_log WHERE event='lesson-admit' "
            "AND block_json IS NOT NULL ORDER BY seq"
        ).fetchall():
            block = json.loads(row["block_json"] if isinstance(row, sqlite3.Row) else row[1])
            out[block["id"]] = block
        for row in self.conn.execute(
            "SELECT supersedes FROM lessons_log WHERE event='lesson-supersede'"
        ).fetchall():
            sup = row["supersedes"] if isinstance(row, sqlite3.Row) else row[0]
            out.pop(sup, None)
        return list(out.values())

    @staticmethod
    def _mint_id(label):
        return "L-" + re.sub(r"[^a-z0-9]+", "-", label.lower())[:40].strip("-")

    @staticmethod
    def _signature(governs, prescribes, reward=1):
        raw = json.dumps({"g": sorted(governs), "p": prescribes, "r": reward}, sort_keys=True)
        return "sha256:" + hashlib.sha256(raw.encode()).hexdigest()[:16]

    def _log(self, event, ident, block=None, supersedes=None, reward=None, control=None,
             reason=None, run_id=""):
        self.conn.execute(
            "INSERT INTO lessons_log(ts,event,id,block_json,supersedes,reward,control,reason,run_id) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (_dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S"), event, ident,
             json.dumps(block, sort_keys=True) if block is not None else None,
             supersedes, reward, control, reason, run_id),
        )


def write(conn: sqlite3.Connection, lesson: dict, probe) -> dict:
    """Interface (M2.12): admit a procedural lesson through the discriminating-probe write-gate.

    `lesson` is a dict {label, description, value, provenance, governs, prescribes, [run_id]} and
    `probe` is the held-out corpus (a list of {id, tag, outcome, action}, or a jsonl path). Returns
    the admitted block; raises RejectedLesson when the probe does not discriminate (or any other
    control refuses). A lesson without a discriminating probe is refused, never silently admitted.
    """
    roots = lesson.pop("roots", None)
    store = LessonsStore(conn, corpus=probe, roots=roots)
    return store.propose(**lesson)


# --------------------------------------------------------------------- selftest control table

# A minimal, self-seeded probe corpus so the control table is HERMETIC. This is a FIXTURE, not a
# softened assertion: it changes what the positive control is scored against, never HOW it is scored.
SEED_CORPUS = [
    {"id": "d-p1", "tag": "detector", "outcome": "PASS",
     "action": "proved the guard able-to-pass on known-good input before trusting it"},
    {"id": "d-p2", "tag": "detector", "outcome": "PASS",
     "action": "confirmed the detector able-to-pass as well as able-to-fail"},
    {"id": "d-f1", "tag": "detector", "outcome": "FAIL",
     "action": "shipped the checker after only a red case, never a success case"},
    {"id": "d-f2", "tag": "detector", "outcome": "FAIL",
     "action": "trusted the guard on the strength of one failing test alone"},
    {"id": "w-p1", "tag": "witness", "outcome": "PASS",
     "action": "re-read the primary artifact before booking the verdict"},
    {"id": "w-p2", "tag": "witness", "outcome": "PASS",
     "action": "went back to the primary artifact rather than the summary line"},
    {"id": "w-f1", "tag": "witness", "outcome": "FAIL",
     "action": "took the verdict from the watcher's summary line"},
    {"id": "w-f2", "tag": "witness", "outcome": "FAIL",
     "action": "booked the step from the digest without opening the file"},
]


def _good(prov):
    return dict(
        label="pair able-to-fail with able-to-pass",
        description="a checker proven only able-to-fail is unfalsified in the direction that "
                    "matters, and will book a success as a miss",
        value="Before trusting any detector or guard, prove it able-to-pass on known-good input "
              "as well as able-to-fail on known-bad. Four guards misfired in one day.",
        provenance=prov,
        governs=["detector", "witness"],
        prescribes=r"able-to-pass|re-read the primary artifact|primary artifact",
    )


def selftest():
    """The ported negative-control receipt, run against a throwaway in-memory conn + seed corpus.

    Returns (passed, total, results). results is a list of (name, expect, ok, detail). The pytest
    wrapper fails on any non-PASS. Both directions exercised: refusals AND the able-to-pass admit.
    """
    import tempfile

    results = []

    def check_reject(name, fn):
        try:
            fn()
            results.append((name, "REJECT", False, "NO REFUSAL - control did not fire"))
        except RejectedLesson as e:
            results.append((name, "REJECT", True, f"[{e.control}] {e.reason[:70]}"))
        except Exception as e:  # noqa: BLE001
            results.append((name, "REJECT", False, f"wrong exception {type(e).__name__}: {e}"))

    def check_pass(name, fn, expect="ADMIT", detail="admitted as required"):
        try:
            fn()
            results.append((name, expect, True, detail))
        except Exception as e:  # noqa: BLE001
            results.append((name, expect, False, f"{type(e).__name__}: {str(e)[:70]}"))

    tmp = Path(tempfile.mkdtemp(prefix="alpaca-lessons-selftest-"))
    prov = tmp / "lessons-provenance.md"
    prov.write_text("grounding for the able-to-pass lesson\n", encoding="utf-8")
    provname = prov.name
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    store = LessonsStore(conn, corpus=SEED_CORPUS, roots=[tmp])

    # NC-L2 - ABLE TO PASS first: if this fails, every refusal below is meaningless.
    check_pass("NC-L2 able-to-pass (coverage>=1)", lambda: store.propose(**_good(provname)),
               detail="admitted; probe discriminated out-of-sample")

    # NC-L1 - a known-bad lesson: prescribes exactly the action that FAILED.
    check_reject("NC-L1 known-bad lesson (inverted)", lambda: store.propose(
        label="trust the watcher summary", description="reading the summary suffices",
        value="Take the step verdict from the watcher's summary line.", provenance=provname,
        governs=["witness"], prescribes=r"watcher's summary"))

    # NC-L4 - a CONSTANT lesson (matches all) -> lands all predictions on PASS.
    check_reject("NC-L4 constant lesson (matches all)", lambda: store.propose(
        label="be careful", description="exercise care", value="Always be careful.",
        provenance=provname, governs=["detector", "witness"], prescribes=r".*"))

    # NC-L5 - a VACUOUS lesson matching nothing -> all predictions land on FAIL.
    check_reject("NC-L5 vacuous lesson (matches none)", lambda: store.propose(
        label="unreachable prescription", description="prescribes nothing in the record",
        value="Prefer the quantum-entangled verification strategy.", provenance=provname,
        governs=["detector", "witness"], prescribes=r"quantum-entangled"))

    # NC-L6 - H7: a declarative domain fact wearing a lesson's clothes.
    check_reject("NC-L6 H7 declarative fact", lambda: store.propose(
        label="pmp granularity", description="the subject's PMP granularity is G=1",
        value="The DUT implements 8-byte PMP granularity and does not support NA4.",
        provenance=provname, governs=["detector"], prescribes=r"able-to-pass"))

    # NC-L7 - entailment: fabricated grounding, refused before the reward probe.
    check_reject("NC-L7 fabricated grounding", lambda: store.propose(
        label="ungrounded claim", description="cites a file that does not exist",
        value="Prefer able-to-pass proofs when validating a detector.",
        provenance="work/ghost/never-existed.md:42", governs=["detector"],
        prescribes=r"able-to-pass"))

    # NC-L8 - coverage=0 fails safe: empty corpus admits nothing.
    def _empty():
        LessonsStore(sqlite3.connect(":memory:"), corpus=[], roots=[tmp]).propose(**_good(provname))
    check_reject("NC-L8 coverage=0 fails safe", _empty)

    # NC-L8b - governs a tag with no corpus instance: also coverage=0.
    check_reject("NC-L8b no matching held-out instance", lambda: store.propose(
        label="governs an unknown situation", description="a class the corpus never saw",
        value="When performing thermal analysis, prefer able-to-pass proofs.",
        provenance=provname, governs=["thermal-analysis"], prescribes=r"able-to-pass"))

    # NC-L9 - char limit: a lesson is a terse prior.
    check_reject("NC-L9 over char limit", lambda: store.propose(
        label="essay", description="too long", value="x" * 500, provenance=provname,
        governs=["detector"], prescribes=r"able-to-pass"))

    # NC-L3 - RETENTION. Flip the world under the admitted lesson; it must HALT.
    def _flip():
        flipped = [dict(r) for r in SEED_CORPUS]
        for r in flipped:
            r["outcome"] = "PASS" if r["outcome"] == "FAIL" else "FAIL"
        store.recheck(corpus=flipped)
    try:
        _flip()
        n_retained = len(store._retained())
        results.append((
            "NC-L3 retention GREEN->FAIL", "HALT", False,
            "NO HALT - lesson silently retained on flipped evidence" if n_retained
            else "INCONCLUSIVE (cascade): nothing retained; fix NC-L2 first"))
    except RetentionHalt as e:
        results.append(("NC-L3 retention GREEN->FAIL", "HALT", True, str(e)[:70]))

    # NC-L3b - retention must NOT halt on unchanged evidence (no false HALT).
    check_pass("NC-L3b retention stable on unchanged corpus", lambda: store.recheck(),
               expect="NO HALT", detail="frozen probe still GREEN; no spurious drift")

    passed = sum(1 for *_, ok, _ in results if ok)
    return passed, len(results), results


if __name__ == "__main__":
    import sys
    p, t, rows = selftest()
    for name, exp, ok, detail in rows:
        print(f"  {'PASS' if ok else 'FAIL'}  {name:<40}  {exp:<8}  {detail}")
    print(f"  {p}/{t} controls behaved as required.")
    sys.exit(0 if p == t else 1)
