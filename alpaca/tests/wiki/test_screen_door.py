"""op-008 t-070 RC2: one secret screen, one door, linear time (fix plan RC2; review findings A-1, A-3,
A-5, B-2, the L1 levers of A-2, carried m-inv-2 and m-conf-1; owner decision dec-392bf1d64a89).

Three properties pin the cause, not single findings:

(a) the door. Every call site in Rune2 code that writes text into the wiki store, its vault or a
    record payload is listed below with the door that screens it (`CALL_SITES`); a new call site
    fails the inventory until it is classified. Then a secret is planted at every entry a caller
    has (wiki ingest, transcript capture, event note, Alpaca source, propose with anchors failing
    K-1 and K-2 and with passing anchors, admit, correct, withdraw a candidate, withdraw a source,
    consolidate, import), and the store, the vault, the record payloads Rune2 appended, the
    candidates view and an export are scanned for it, before and after an import.
(b) the screen. It is idempotent on generated inputs (every Q-A phrasing class, glued pairs of
    every class, prose), it settles in two passes when one secret changes the text, and it runs in
    linear time: 1,000 BEGIN lines with no END line in 2 MB in under 1 s of CPU, and four times the
    text in well under sixteen times the time.
(c) the findings: the three A-1 phrasings are masked, and the B-2 probe's anchor quote is not kept
    in clear in the candidate event, its anchor row or the candidates view.

Real record, real drain, real store, real doors; nothing is mocked. The pass counters wrap the
redactor's one pass (`detail._redact_once`) and change nothing it returns.

(d) round 2, class C-C (EX/fix-plan-r2.md section 5; A-3, the error text of A-9, I-4): the rules are
    pinned over generated cases, not chosen ones. Every registered secret class
    (`detail.SCREEN_PATTERNS`) and the screen as a whole take linear time on repeated-input shapes
    built from every credential name, verb, separator and token start literal; the rewritten
    patterns give byte for byte the output of a frozen copy of the bfb66b189 screen
    (`screen_bfb66b189.py`) on exhaustive, random and fixture text, which is what keeps stored bytes
    unchanged; every record append of an exception's text goes through the screen; and only the
    screen can make a `Screened` value.
(e) t-425 (t-070 check round 1, minor A-2): the 21 phrasings attack p01 found stored in clear at all
    five retain doors are one more class, `plain-variant` (`detail.VARIANT_FAMILIES`). Each is masked
    at every door; prose of the same shapes is not; the class takes linear time on the generated
    shapes above and on its own; and the screen changes a text only where a declared variant finds a
    value no bfb66b189 class masks: everywhere else its output is the bfb66b189 output byte for byte.
"""
import ast
import copy
import itertools
import json
import os
import pickle
import random
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from native_helpers import (T10, T11, admit, append, appender, at, capture, events,  # noqa: E402
                            note, proposal, propose, reset_vault, store, vault, withdraw)
from test_capture_redaction import GLUE, PLANTED, SAMPLES                            # noqa: E402
import screen_bfb66b189 as frozen                                                     # noqa: E402

from alpaca import db as record                                  # noqa: E402
from alpaca.analytics import detail                             # noqa: E402
from alpaca.wiki import consolidate, portable, service          # noqa: E402
from alpaca.wiki.ingest import drain                            # noqa: E402

WIKI = Path(__file__).resolve().parents[2] / "wiki"


# ------------------------------------------------------------------ helpers
class Passes:
    """Counts the redactor's passes and the characters they read, leaving each pass as it is."""

    def __init__(self, monkeypatch):
        self.calls, self.chars = 0, 0
        real = detail._redact_once

        def counting(value):
            self.calls += 1
            self.chars += len(value)
            return real(value)
        monkeypatch.setattr(detail, "_redact_once", counting)


def _cpu(fn, *args):
    best = None
    for _ in range(2):
        start = time.process_time()
        fn(*args)
        took = time.process_time() - start
        best = took if best is None else min(best, took)
    return best


# ------------------------------------------------------------------ (c) the findings
#: A-1 (m-conf-1): a bare "PIN" or "secret" took the next credential word as its value, so the
#: credential phrasing that follows was stored in clear.
A1 = [("PIN: password is hunter22x", "hunter22x"),
      ("The PIN is password is hunter22x.", "hunter22x"),
      ("The secret is token: abc123def", "abc123def")]


@pytest.mark.parametrize("text,secret", A1)
def test_a_bare_pin_or_secret_does_not_swallow_the_next_credential_phrasing(text, secret):
    out = drain.screen(text)
    assert secret not in out and "[redacted]" in out, out
    assert detail._redact(text) == out


@pytest.mark.parametrize("text,secret", [
    ("password: token is abc123zz", "abc123zz"),
    ("The PIN for the gate is password: hunter22x", "hunter22x"),
    ("The secret was passphrase is plankton otter quayside.", "plankton"),
    ("pin = token=abc123def9", "abc123def9"),
])
def test_any_credential_name_before_another_phrasing_leaves_that_phrasing_to_mask(text, secret):
    assert secret not in drain.screen(text), drain.screen(text)


@pytest.mark.parametrize("text", [
    "The PIN is 4711.", "the pin is loose", "the secret is out", "Our secret is teamwork",
    "The token is valid.", "The password is passw0rd123 here", "the PIN is password-protected",
])
def test_the_lookahead_keeps_what_the_screen_did_before(text):
    """The lookahead stops a value only where another credential phrasing starts: a value that
    merely begins like a name ("passw0rd123") is still masked, prose stays."""
    before = {"The PIN is 4711.": "The PIN is [redacted].",
              "The password is passw0rd123 here": "The password is [redacted] here"}
    assert drain.screen(text) == before.get(text, text)


def test_the_b2_probe_quote_is_not_kept_in_clear(project):
    """B-2 (`.alpaca/proofs/op-008/t-070.logs/s1/sliceB-probes/test_probe_b2.py.txt`): an anchor quote
    that fails K-2 under a servable source was stored as given in the `wiki-candidate` event, in
    `candidate_anchors` and in the candidates view."""
    private = "the relay admin password is Hunter22-orchid"
    eid = note(project, "Task t-9 is blocked on the review.")
    received = propose(project, proposal(project, eid, private, subject="t-9", obj="blocked"))
    assert received.status == "refused"
    raw = json.dumps([e["data"] for e in _wiki_events(project, "wiki-candidate")])
    with store(project) as conn:
        quotes = [r[0] for r in conn.execute("SELECT quote FROM candidate_anchors")]
    view = json.dumps([v.to_dict() for v in service.candidates(project)])
    assert "Hunter22-orchid" not in raw and "Hunter22-orchid" not in view
    assert quotes == ["the relay admin password is [redacted]"]


# ------------------------------------------------------------------ (b) the screen
#: Every Q-A phrasing class (dec-804f2708474c) and the classes the redactor had before t-069, each
#: with the value that must never be stored.
PHRASES = [(line, parts) for line, parts in SAMPLES.values()] + [
    (text, [marker]) for _, text, _, marker in PLANTED] + [(text, [s]) for text, s in A1] + [
    ("the password for the quay camera is hunter22!", ["hunter22"]),
    ("the wifi password is \"correct horse battery staple\"", ["correct horse"]),
    ("PIN:\n  4096x", ["4096x"]),
    ("the session token was changed to abc12345 yesterday", ["abc12345"]),
    ("PASSWORD=\"abc def\"", ["abc def"]),
    ("{\"password\": \"hunter2\", \"token\": \"tok-1a2b\"}", ["tok-1a2b"]),
]
PROSE = ["The build passed on the second try.", "the token count is 5000", "spin: 12345",
         "key: t-069", "Handling NOFORN material.", "the pin is loose", "op: token-rotation",
         "-----BEGIN CERTIFICATE-----\nMIIC\n-----END CERTIFICATE-----", "event: 4711",
         "The passphrase is stored in the vault.", "a [[wikilink]] and [brackets]"]
JOINTS = (" ", "\n", "\n\n", ". ", "", "\\n", "\"", ": ")


def _generated(seed, n):
    rnd = random.Random(seed)
    pool = [p for p, _ in PHRASES] + list(GLUE.values()) + PROSE
    for _ in range(n):
        parts = [rnd.choice(pool) for _ in range(rnd.randint(1, 5))]
        yield "".join(p + rnd.choice(JOINTS) for p in parts)


@pytest.mark.parametrize("seed", range(4))
def test_the_screen_is_a_fixed_point_on_generated_text(seed):
    for text in _generated(seed, 400):
        once = drain.screen(text)
        assert drain.screen(str(once)) == once, text
        assert detail._redact(text) == once, text


def test_every_phrasing_class_and_every_glued_pair_is_masked():
    for line, parts in PHRASES:
        out = drain.screen(line)
        assert not [p for p in parts if p in out], (line, out)
    secrets = {"aws-access-key": "AKIAIOSFODNN7EXAMPLE", "pem-block": "t069PEMbodyEXAMPLE",
               "slack-token": "t069SlackEXAMPLEtoken", "jwt": "t069JwtSignatureEXAMPLE",
               "provider-key": "t069providerEXAMPLEkey", "github-token": "t069githubEXAMPLEtoken",
               "bearer": "T069-bearer-5678", "api-key-assignment": "T069-api-1234",
               "password-is": "swordfishmarlin", "pin-was": "T069-pin-2024", "token-is": "T069-tok-9q8w"}
    for (a, first), (b, second) in ((x, y) for x in GLUE.items() for y in GLUE.items()):
        for joint in ("", " ", "\n"):
            out = drain.screen(first + joint + second)
            assert drain.screen(str(out)) == out
            if joint == "":
                continue                  # glued: a fixed point, not always two secrets (t-069)
            for name in (a, b):
                if name in secrets:
                    assert secrets[name] not in out, (a, b, joint, out)


def test_one_secret_settles_in_two_passes(monkeypatch):
    """A-3: the screen's step was the settled redactor inside another settle loop, three passes
    where two reach the fixed point. One settle loop over one pass: a text with one secret is read
    twice (the pass that masks, the pass that confirms), a text with none once."""
    body = "".join("user: looking at module %d, plain prose and paths a/b.py:12.\n" % i for i in range(200))
    counter = Passes(monkeypatch)
    drain.screen(body)
    assert counter.calls == 1
    counter.calls = 0
    drain.screen(body + "The relay admin password is T070-pass-4711.\n")
    assert counter.calls == 2


def test_unmatched_begin_lines_take_linear_time():
    """A-5: 1,000 BEGIN lines with no END line in 2 MB took 3.5 s a pass (95 s for 1.54 MB of them):
    each BEGIN scanned to the end of the text for its END line. Bounded now, under 1 s of CPU."""
    filler = "ordinary transcript prose line with words and numbers 12345 and paths a/b/c.py\n"
    chunk = 2_000_000 // 1000
    text = "".join("-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA\n" + filler * (chunk // len(filler))
                   for _ in range(1000))
    assert len(text) > 1_900_000
    out = drain.screen(text)
    assert "MIIEowIBAAKCAQEA" not in out and "BEGIN RSA PRIVATE KEY" not in out
    assert _cpu(drain.screen, text) < 1.0


@pytest.mark.parametrize("unit", [
    "-----BEGIN RSA PRIVATE KEY-----\n" + "A" * 15 + "\n",
    "pin for the a b c d e f is token secret password passphrase is is = : ",
    "-----BEGIN EC PRIVATE KEY-----\\nMIIEowIBAAKCAQEAabcdefgh\\n" + "x " * 30 + "\n",
    "The relay admin password is T070-pass-4711. key: value token: abc " * 3 + "\n",
])
def test_the_screen_time_grows_linearly_with_the_text(unit):
    """Four times the text takes well under sixteen times the time (linear is four, quadratic
    sixteen); the bound leaves room for a loaded host."""
    small, large = unit * (100_000 // len(unit)), unit * (400_000 // len(unit))
    drain.screen(small)
    t_small, t_large = _cpu(drain.screen, small), _cpu(drain.screen, large)
    assert t_large < max(8 * t_small, 0.05), (t_small, t_large)


# ------------------------------------------------------------------ (a) the door
def test_a_screened_text_is_not_screened_again(project, monkeypatch):
    """The door screens what it is handed unless the one screen already did (`drain.Screened`): a
    renderer's screened text lands with no second pass (the L1 lever "door re-screen"), any other
    text is screened at the door."""
    from alpaca.wiki.config import Config
    from alpaca.wiki.ingest.absorb import Absorber
    text = "# Note\n\nThe relay admin password is T070-door-4711.\n"
    screened = drain.screen(text)
    assert isinstance(screened, drain.Screened) and drain.screen(screened) is screened
    cfg = Config.for_project(str(project))
    cfg.vault_dir.mkdir(parents=True, exist_ok=True)
    absorber = Absorber(cfg)
    try:
        counter = Passes(monkeypatch)
        absorber.absorb_text("wiki/n/a.md", screened, kind="wiki", path="wiki/n/a.md")
        assert counter.calls == 0
        absorber.absorb_text("wiki/n/b.md", text, kind="wiki", path="wiki/n/b.md")
        assert counter.calls == 2
    finally:
        absorber.close()
    with store(project) as conn:
        texts = "\n".join(r[0] for r in conn.execute("SELECT text FROM blocks"))
    assert "T070-door-4711" not in texts


def test_a_transcript_is_screened_once(project, monkeypatch):
    """The L1 levers of A-2: the base capture class redacted each value and the drain threw that
    copy away and redacted it again, the renderer screened the whole text, and the door screened it
    once more. Now a transcript of string values is read by the redactor once, as one text at the
    door, and lands with no other pass."""
    session = "s-once"
    path = Path(project, "once.jsonl")
    rows = []
    for n in range(60):
        rows.append({"type": "user", "timestamp": "2026-10-04T09:%02d:00Z" % n,
                     "message": {"content": "Question %d about the build of module %d." % (n, n)}})
        rows.append({"type": "assistant", "timestamp": "2026-10-04T09:%02d:05Z" % n,
                     "message": {"content": [{"type": "text", "text": "Answer %d: it passed." % n}]}})
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    append(project, "result", {"body": "The session ran."}, session=session)
    counter = Passes(monkeypatch)
    report = drain.run(str(project), session, transcript_path=str(path))
    (entry,) = report["transcripts"]
    stored = (vault(project) / entry["doc_id"]).read_text(encoding="utf-8")
    note_chars = sum(len(drain._note_doc(dict(e, data=json.loads(e["data"])))[1])
                     for e in events(project) if e["session"] == session)
    assert counter.chars <= len(stored) + 2 * note_chars, (counter.chars, len(stored), note_chars)


def _calls(path: Path):
    """(enclosing def, callee) of every call in a module; a method is named `Class.method`, a nested
    def `outer.inner`. A call through an attribute is named by the attribute (`writer.upsert_doc`
    gives `upsert_doc`, `withdrawal.withdraw` gives `withdrawal.withdraw`); a plain name by itself."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out = []

    def walk(node, where):
        for child in ast.iter_child_nodes(node):
            name = where
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = child.name if not where else where + "." + child.name
            if isinstance(child, ast.Call):
                f = child.func
                if isinstance(f, ast.Attribute):
                    owner = f.value.id if isinstance(f.value, ast.Name) else None
                    out.append((name, owner + "." + f.attr if owner == "withdrawal" else f.attr, False))
                elif isinstance(f, ast.Name):
                    out.append((name, f.id, True))
            walk(child, name)
    walk(tree, "")
    return out


#: The callees that write text into the wiki store, its vault, a file beside it or the record.
WRITERS = {"absorb_text", "_write_if_changed", "append_event", "write_candidate", "apply_correction",
           "record_stale_correction", "admit_candidate_edge", "apply_candidate_edge", "replay_admitted",
           "upsert_doc", "upsert_block", "import_source_version", "withdrawal.withdraw", "write",
           # the one appender of a removal act (t-070 round 2 C-E): it hands the door's data to
           # the door's `record`
           "record_removal_act"}
#: The record appender as Rune2 code receives it: a plain name (`record(kind, data)`, `append`).
APPENDERS = {"record", "append"}

#: Every such call site in Rune2 code (`alpaca/wiki/`, less `store/`, the Writer, DB and ledger the
#: doors hand their text to), with the door that screens what it writes. A new call site fails
#: `test_every_write_call_site_is_behind_the_door` until it is added here with its door.
CALL_SITES = {
    # documents: the one screen (`drain.screen`, a `Screened` text) or the absorb door itself
    ("ingest/drain.py", "capture", "absorb_text"): "the renderers' text is Screened (drain.screen)",
    ("ingest/drain.py", "capture.publish", "_write_if_changed"): "vault bytes are the Screened text "
                                                                 "absorbed, before the commit (C-E)",
    ("ingest/drain.py", "_write_if_changed", "_write_if_changed"): "itself (pinned directory)",
    ("ingest/drain.py", "_write_if_changed", "write"): "the bytes it is handed",
    ("ingest/absorb.py", "Absorber.absorb_vault", "absorb_text"): "the door screens a vault page",
    ("ingest/absorb.py", "Absorber.absorb_text", "upsert_doc"): "the door: screened text only",
    ("ingest/absorb.py", "Absorber.absorb_text", "upsert_block"): "the door: screened text only",
    ("service.py", "ingest", "write"): "the vault copy: the text screened once",
    ("service.py", "ingest", "absorb_text"): "the text screened once (Screened)",
    ("portable.py", "_land_documents", "absorb_text"): "the row text screened once (Screened)",
    ("portable.py", "_land_documents.publish", "_write_if_changed"): "the Screened text absorbed, "
                                                                     "before the commit (C-E)",
    ("portable.py", "_restore_envelopes", "import_source_version"): "ids, hashes, times; no text",
    ("portable.py", "_write_private", "write"): "the export file: every string through _clean",
    # record payloads and candidate rows: the proposal door (`intake.parse_proposal`), the write
    # verbs' free text screened on entry, or ids and codes only
    ("service.py", "propose", "record"): "intake.parse_proposal screens every proposal string",
    ("service.py", "propose", "write_candidate"): "rows of the screened event",
    ("service.py", "admit.settle", "record"): "ids and codes",
    ("service.py", "admit", "record"): "quote and body of the screened candidate row",
    ("service.py", "admit", "admit_candidate_edge"): "quote of the screened candidate row",
    ("service.py", "_admit_observation", "write"): "the lesson from the screened candidate row",
    ("service.py", "_admit_observation", "record"): "lesson and prior from the screened row",
    ("service.py", "withdraw_candidate", "record_removal_act"): "reason screened on entry",
    ("service.py", "withdraw_source", "record_removal_act"): "reason screened on entry",
    ("service.py", "withdraw_source", "withdrawal.withdraw"): "reason screened on entry",
    ("service.py", "correct.refuse", "record"): "reason and replacement screened on entry",
    ("service.py", "correct", "record_removal_act"): "reason and replacement screened on entry",
    ("service.py", "correct", "apply_correction"): "reason and replacement screened on entry",
    ("consolidate.py", "appender.append", "append_event"): "what its callers pass",
    ("consolidate.py", "run_step", "append"): "key, class, attempts, candidate id",
    ("consolidate.py", "run_step_after_capture", "append_event"): "the error text screened",
    ("ingest/drain.py", "_record_quarantine", "append_event"): "doc id and rule id",
    ("ingest/drain.py", "_record_transcript_version", "append_event"): "hashes, byte counts and "
                                                                      "the record head, screened",
    ("recovery.py", "run", "append_event"): "the recovery report (counts, ids)",
    # replay writes what the record holds, screened at the doors above since RC2
    ("recovery.py", "replay_candidates", "write_candidate"): "rows of a recorded wiki-candidate",
    ("recovery.py", "_replay_source_act", "withdrawal.withdraw"): "a recorded withdrawal reason",
    ("recovery.py", "_replay_source_act", "apply_correction"): "a recorded correction",
    ("recovery.py", "_replay_source_act", "record_stale_correction"): "a recorded correction",
    ("recovery.py", "_replay_admission", "apply_candidate_edge"): "a recorded admission",
    ("recovery.py", "_replay_observation", "replay_admitted"): "a recorded admission",
    # legacy writers outside the candidate lifecycle: they copy or project rows already stored
    ("extract.py", "_copy_public", "upsert_doc"): "stored (screened) documents copied",
    ("extract.py", "_copy_public", "upsert_block"): "stored (screened) blocks copied",
    ("project/artifacts.py", "_write", "write"): "a projection of stored rows",
    ("reflect/identity.py", "write_heartbeat_stub", "write"): "the owner's interview prompts",
    ("lint.py", "cli_lint", "write"): "standard output",
}


def test_every_write_call_site_is_behind_the_door():
    found = set()
    for path in sorted(WIKI.rglob("*.py")):
        rel = path.relative_to(WIKI).as_posix()
        if rel.startswith("store/"):
            continue
        for where, callee, plain in _calls(path):
            if callee in WRITERS or (plain and callee in APPENDERS):
                found.add((rel, where, callee))
    unknown = sorted(found - set(CALL_SITES))
    assert unknown == [], "a write call site no door is named for: %s" % unknown
    gone = sorted(set(CALL_SITES) - found)
    assert gone == [], "listed call sites that no longer exist: %s" % gone


def test_only_the_screen_makes_a_screened_text():
    """`Screened` marks text the one screen produced; nothing else may construct it, so a text that
    skips the door cannot skip the absorb door's screen."""
    makers = []
    for path in sorted(WIKI.rglob("*.py")):
        for where, callee, _plain in _calls(path):
            if callee == "Screened":
                makers.append((path.relative_to(WIKI).as_posix(), where))
    assert makers == [("ingest/drain.py", "screen")]


def _wiki_events(project, kind=None):
    return [dict(e, data=json.loads(e["data"])) for e in events(project)
            if e["actor"] == "alpaca-wiki" and (kind is None or e["kind"] == kind)]


def _surfaces(project, tmp_path, label):
    """Every surface a secret could be kept on: each file under the vault (store, WAL, ledger,
    documents), the record payloads Rune2 appended, the candidates view and a private export."""
    out = {}
    for path in sorted(vault(project).rglob("*")):
        if path.is_file():
            out["vault:" + path.relative_to(vault(project)).as_posix()] = path.read_bytes().decode(
                "utf-8", errors="replace")
    out["record"] = json.dumps([[e["kind"], e["data"]] for e in _wiki_events(project)])
    out["candidates"] = json.dumps([v.to_dict() for v in service.candidates(project)])
    exported = tmp_path / ("export-%s.jsonl" % label)
    portable.export_store(project, exported, include_private=True)
    out["export"] = exported.read_text(encoding="utf-8")
    return out


def _leaks(surfaces, markers):
    return sorted({(name, m) for name, text in surfaces.items() for m in markers
                   if m in text or m.lower() in text})


def test_a_secret_planted_at_every_entry_is_kept_nowhere(project, tmp_path):
    markers = {}

    def plant(entry, marker):
        markers[entry] = marker
        return marker

    # wiki ingest
    doc = tmp_path / "ingest.md"
    doc.write_text("# Relay\n\nThe ingest relay password is %s.\n" % plant("ingest", "RC2ingest-4711"),
                   encoding="utf-8")
    service.ingest(str(project), str(doc), doc_id="wiki/notes/relay.md", session="s-ing")
    # an event note (an A-1 phrasing) and an Alpaca source (another)
    note(project, "The note admin PIN: password is %s" % plant("event-note", "RC2note4711x"),
         ref="t-8")
    decided = at(project, "decision", {"id": "dec-0000000000c2", "kind": "scope", "choice": "rotate",
                                       "consequence": "none", "options": "[]", "pointer": "p",
                                       "context": "The secret is token: %s" % plant("source", "RC2dec123def")},
                 T10)
    capture(project, {decided})
    # a transcript: a message, and a quoted assignment inside a tool input (rendered as JSON)
    path = tmp_path / "s-tr.jsonl"
    rows = [{"type": "user", "timestamp": "2026-03-01T10:00:00Z",
             "message": {"content": "The transcript feed token is %s." % plant("transcript", "RC2trans-9q8w")}},
            {"type": "assistant", "timestamp": "2026-03-01T10:00:05Z", "message": {"content": [
                {"type": "tool_use", "id": "c1", "name": "Bash",
                 "input": {"command": "export API_KEY=\"%s\" && run" % plant("tool-input", "RC2dict-7788")}}]}}]
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    append(project, "result", {"body": "The transcript session ran."}, session="s-tr")
    drain.run(str(project), "s-tr", transcript_path=str(path))
    # propose: K-1 fails (a wrong event hash), K-2 fails (the quote is not in the note), and a
    # proposal whose anchor quotes the note's secret as the agent read it before the screen
    eid = note(project, "Task t-9 is blocked since the ops password is %s." % plant("anchor", "RC2adm-1111"))
    bad_hash = proposal(project, eid, "the relay admin password is %s" % plant("k1", "RC2k1orchid9"))
    bad_hash["anchors"][0]["hash"] = "0" * 64
    assert propose(project, bad_hash).status == "refused"
    k2 = proposal(project, eid, "The PIN is password is %s." % plant("k2", "RC2k2orchid9"))
    assert propose(project, k2).status == "refused"
    good = proposal(project, eid, "Task t-9 is blocked since the ops password is RC2adm-1111")
    good["context"] = [{"source_ref": "event:%d" % eid,
                        "quote": "context password is %s" % plant("context", "RC2ctx-5555")}]
    received = propose(project, good)
    assert received.status == "checked", received
    assert admit(project, received.candidate_id).status == "applied"
    # correct the admitted fact, with the secret in the owner's reason
    (edge,) = [e for e in _edges(project) if e["extractor"] == "llm"]
    corrected = service.correct(project, edge["edge_id"], "retract", session="s-owner",
                                record=appender(project, "s-owner"), by="owner",
                                reason="rotated: the old password is %s" % plant("correct", "RC2corr-31337"))
    assert corrected.status == "applied", corrected
    # withdraw a candidate and a source, each with the secret in its reason
    other = note(project, "Task t-7 is blocked on the gate.", ref="t-7")
    cid = propose(project, proposal(project, other, "Task t-7 is blocked", subject="t-7")).candidate_id
    assert withdraw(project, cid, reason="the token is %s" % plant("withdraw-candidate", "RC2wd-8080x")).status == "applied"
    gone = note(project, "Task t-6 is done.", ref="t-6")
    assert service.withdraw_source(project, "event:%d" % gone, session="s-owner", record=appender(project, "s-owner"),
                                   reason="the passphrase is %s" % plant("withdraw-source", "RC2ws-2468")).status == "applied"
    # consolidation: a repeated failure whose error line holds a secret becomes an observation
    for n in (1, 2):
        at(project, "drain-failed", {"error": "relay refused: password is %s" % plant("consolidate", "RC2cons-4242"),
                                     "where": "stop"}, T11, session="s-fail-%d" % n)
    capture(project)
    result = consolidate.run_step(str(project), deadline_s=60)
    assert result.proposed, result
    surfaces = _surfaces(project, tmp_path, "live")
    assert _leaks(surfaces, markers.values()) == []
    # import: an export row's text is landed through the door, and its vault copy is what was stored
    exported = tmp_path / "out.jsonl"
    portable.export_store(project, exported)
    lines = exported.read_text(encoding="utf-8").splitlines()
    text = "# Relay\n\nThe import password is %s.\n" % plant("import", "RC2imp-6060")
    from alpaca.wiki.determinism import sha256_hex
    edited = []
    for line in lines:
        row = json.loads(line)
        if row.get("source_ref") == "document:wiki/notes/relay.md":
            row.update(text=text, content_sha256=sha256_hex(text))
        edited.append(json.dumps(row))
    exported.write_text("\n".join(edited) + "\n", encoding="utf-8")
    reset_vault(project)
    report = portable.import_store(project, exported)
    assert report["documents_landed"] == 1, report
    surfaces = _surfaces(project, tmp_path, "imported")
    assert _leaks(surfaces, markers.values()) == []
    stored = (vault(project) / "wiki/notes/relay.md").read_text(encoding="utf-8")
    with store(project) as conn:
        sha = conn.execute("SELECT content_sha256 FROM docs WHERE doc_id='wiki/notes/relay.md'").fetchone()[0]
    assert sha == sha256_hex(stored) and drain.screen(stored) == stored     # vault bytes = stored text


def _edges(project):
    with store(project) as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM edges ORDER BY edge_id")]


# ------------------------------------------------------------------ (d) round 2, class C-C
#: The secret classes the screen registers (`detail.SCREEN_PATTERNS`), in its order.
CLASSES = ("pem-private-key", "plain-phrasing", "plain-variant", "secret-assignment", "provider-key",
           "aws-access-key", "slack-token", "jwt")
#: The classes of the bfb66b189 screen; `plain-variant` is the one t-425 adds.
FROZEN_NAMES = tuple(name for name in CLASSES if name != "plain-variant")


def _registry():
    return {name: find for name, _hint, find in detail.SCREEN_PATTERNS}


def test_the_registry_names_every_class_the_screen_applies():
    """The timing and equivalence tests below read the classes from the registry, so the registry
    must be what the screen applies: a class added to the screen and not to it would go untested."""
    assert tuple(name for name, _h, _f in detail.SCREEN_PATTERNS) == CLASSES
    text = ("".join(line + "\n" for line, _ in PHRASES) + "\n".join(GLUE.values()) + "\n"
            + "\n".join(line for _, line, _, _ in VARIANTS))
    spans = [span for _n, hint, find in detail.SCREEN_PATTERNS if hint(text) for span in find(text)]
    assert detail._redact_once(text) == detail._mask(text, spans)


# A-3: the shapes are generated, not chosen. A prefix (a credential name, a name with its verb, a
# verb, a separator or a token start literal) and a unit (whitespace of each kind, a token run's
# punctuation, a letter, a digit) give two shapes each: prefix + unit * n and (prefix + unit) * n.
PUMP_NAMES = ("password", "passwd", "passcode", "passphrase", "pass phrase", "pin", "PIN code",
              "pin number", "secret", "token", "credential", "credentials", "api key", "api_key",
              "access key", "secret-key", "private key", "access code", "security code")
PUMP_VERBS = ("is", "was", "are", "were", "is now", "was set to", "changed to", "reset to", "still")
PUMP_SEPARATORS = ("=", ":", "\"", "'", "`", " for the", " of")
PUMP_LITERALS = ("eyJ", "eyJhbGciOi.", "eyJ-eyJ.", "sk-", "ghp_", "gho_", "AKIA", "A3TX", "ASIA",
                 "xoxb-", "xoxa.xoxb-", "xapp-", "https://hooks.slack.com/services/",
                 "-----BEGIN RSA PRIVATE KEY-----", "-----END RSA PRIVATE KEY-----", "bearer",
                 "Authorization: Bearer", "authorization:", "API_KEY", "API_KEY=", "SECRET:",
                 "MY_SECRET_KEY")
#: The shapes of the t-425 variants: plural names, the short names, the verbs and separators they
#: add, a clause, a table, a YAML block, an env name and the reverse order.
PUMP_VARIANTS = ("passwords", "passwords are", "api keys were", "pwd", "pwd:", "pw=", "password's",
                 "password has been changed to", "PIN will be", "password should now be",
                 "**password**", "**password:**", "**password**:", "password\t", "password -",
                 "password: >\n", "password, which the vendor set,", "password for the a b c d e f g h i",
                 "PIN is 1 2 3", "| password |\n|---|\n", "| password | pin |\n|---|---|\n| ",
                 "DB_PASS=", "MYSQL_PWD:", "use ", "use x1234 as", " as the password", "passwords are x1, ")
PUMP_PREFIXES = (PUMP_NAMES + tuple(n + " " + v for n in ("password", "PIN", "passphrase", "token")
                                    for v in ("is", "were", "is now", "was set to"))
                 + PUMP_VERBS + PUMP_SEPARATORS + PUMP_LITERALS
                 + tuple(n + s for n in ("password", "token") for s in ("=", ":", " for the quay camera"))
                 + PUMP_VARIANTS)
PUMP_UNITS = (" ", "\t", "\u00a0", "\r", "\n", "-", "_", ".", "=", ":", "a", "1", ",", "|", "*", " and ")
#: The two shapes check round 1 found (attack p02, `LOGS/attacks/p02_screen_superlinear_inputs.out`).
FOUND_SHAPES = {"jwt-dash": lambda n: "eyJ-" * (n // 4),
                "pw-spaces": lambda n: "password is" + " " * n + "ab"}
#: Linear work gives 4 at four times the text, quadratic 16; 6 leaves room for noise. A time still
#: below the floor (0.5 ms) at the largest size (64,000 characters) is too small to read a ratio
#: from and counts as linear: a quadratic cost that small would take about 0.12 s at 1 MB.
PUMP_RATIO, PUMP_FLOOR, PUMP_START, PUMP_CAP = 6.0, 0.0005, 1_000, 64_000
#: A ratio over the bound counts only when it holds at this many sizes in a row (each four times
#: the last), so a reading near the floor on a loaded host does not fail a linear shape.
PUMP_SIZES_OVER = 2


def _best_cpu(fn, text):
    best = None
    for _ in range(3):
        start = time.process_time()
        fn(text)
        took = time.process_time() - start
        best = took if best is None else min(best, took)
    return best


def _superlinear(fn, make, chars_per_n):
    """None when four times the text takes at most PUMP_RATIO times the CPU (best of three at each
    size, the size grown from about PUMP_START characters until the larger time clears the floor);
    else (chars at 4n, t, t4, ratio). A ratio over the bound is read a second time before it
    counts, so one noisy reading on a loaded host does not fail a linear shape. It must then hold
    at the next size up as well: a quadratic cost reads about 16 at every size, while a linear
    pattern read near the floor (0.1 ms against 0.9 ms) can pass the bound under host load and
    reads about 4 one size up (op-008 t-072d, release check of edda8ea0a)."""
    n = max(1, PUMP_START // chars_per_n)
    confirmed = 0
    while True:
        large = make(4 * n)
        t4 = _best_cpu(fn, large)
        if t4 < PUMP_FLOOR and len(large) < PUMP_CAP:
            n *= 4
            continue
        if t4 < PUMP_FLOOR:
            return None
        readings = []
        for _ in range(2):
            t, t4 = _best_cpu(fn, make(n)), _best_cpu(fn, large)
            ratio = t4 / t if t > 0 else float("inf")
            if ratio <= PUMP_RATIO:
                return None
            readings.append(ratio)
        confirmed += 1
        if confirmed == PUMP_SIZES_OVER:
            return len(large), round(t, 4), round(t4, 4), round(min(readings), 1)
        n *= 4


def _pump_shapes():
    for prefix in PUMP_PREFIXES:
        for unit in PUMP_UNITS:
            yield ("%r + %r * n" % (prefix, unit), (lambda n, p=prefix, u=unit: p + u * n), len(unit))
            yield ("(%r + %r) * n" % (prefix, unit), (lambda n, p=prefix, u=unit: (p + u) * n),
                   len(prefix + unit))


@pytest.mark.parametrize("shape", sorted(FOUND_SHAPES))
def test_the_two_found_shapes_take_linear_time(shape):
    """A-3 (p02 at bfb66b189): "eyJ-" repeated grew about four times per doubling (2.084 s at
    128 KB), "password is" + 16,000 spaces took 4.840 s."""
    assert _superlinear(drain.screen, FOUND_SHAPES[shape], 1) is None


@pytest.mark.parametrize("target", ("screen",) + CLASSES)
def test_screen_is_linear_on_generated_pump_shapes(target):
    fn = detail.screen if target == "screen" else _registry()[target]
    slow = [(name, found) for name, make, per in _pump_shapes()
            for found in [_superlinear(fn, make, per)] if found]
    assert slow == [], "superlinear shapes (chars at 4n, cpu at n, at 4n, ratio): %s" % slow


# The equivalence: what the rewritten patterns find is what the bfb66b189 patterns found.
def _frozen_plain(text):
    return [span for span in map(frozen._plain_span, frozen._PLAIN_SECRET.finditer(text)) if span]


def _frozen_secret_text(text):
    return [(m.start() + len(m.group(1) or m.group(2)), m.end()) for m in frozen._SECRET_TEXT.finditer(text)]


def _frozen_spans(pattern):
    return lambda text: [m.span() for m in pattern.finditer(text)]


#: Each class as the bfb66b189 screen computed it (`frozen._redact_once`), by registry name.
FROZEN_CLASSES = {"pem-private-key": frozen._pem_spans, "plain-phrasing": _frozen_plain,
                  "secret-assignment": _frozen_secret_text,
                  "provider-key": _frozen_spans(frozen._PROVIDER_KEY),
                  "aws-access-key": _frozen_spans(frozen._AWS_KEY),
                  "slack-token": _frozen_spans(frozen._SLACK_TOKEN), "jwt": _frozen_spans(frozen._JWT)}


def _exhaustive(alphabet, longest, prefixes=("",)):
    for prefix in prefixes:
        for size in range(longest + 1):
            for parts in itertools.product(alphabet, repeat=size):
                yield prefix + "".join(parts)


#: Small alphabets, every string up to the length given: the token run of a JWT with each kind of
#: neighbour (a word boundary, a dash or underscore inside the run, a dot, a letter outside it), and
#: the whitespace between a credential name, its verb and its value.
EXHAUSTIVE = {
    "jwt-7": ("jwt", ("eyJ", "abcd", "-", ".", " ", "\u00e9", "_"), 7, ("",)),
    "jwt-12": ("jwt", ("eyJ", "abcd.", "-"), 12, ("",)),
    "plain-6": ("plain-phrasing", (" ", "\t", ":", "\n", "is", "token", "ab12"), 6,
                ("password", "the PIN", "passphrase", "api key for the gate")),
    "plain-12": ("plain-phrasing", (" ", "is", "x9"), 12, ("password",)),
}


def _random_texts(seed, count):
    rnd = random.Random(seed)
    pool = (list(PUMP_PREFIXES) + list(PUMP_UNITS) + [p for p, _ in PHRASES] + list(GLUE.values()) + PROSE
            + ["hunter22x", "swordfishmarlin", "T070-pass-4711", "plankton otter quayside", "valid",
               "stored in the vault", "now", "set to", "[[", "]]", "[redacted]", "\\n", "\u00e9", "(", ")",
               "eyJhbGciOiJIUzI1NiJ9", "abcd", "x9", "\r\n", "  ", "\t\t"])
    for _ in range(count):
        yield "".join(rnd.choice(pool) + rnd.choice(("", " ", "", "\n", "\t", "  ", ". "))
                      for _ in range(rnd.randint(1, 24)))


def _fixture_texts():
    """Every text file of the test suite (its sources hold most secret samples there are) and, when
    the tree has them, the docs and the design notes: each file whole and each of its lines."""
    package = Path(__file__).resolve().parents[2]
    roots = [package / "tests", package.parent / "docs", package.parent / "design"]
    for root in roots:
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if path.suffix not in (".py", ".md", ".txt", ".json", ".jsonl", ".yaml", ".yml", ".html") \
                    or "__pycache__" in path.parts or not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            yield text
            yield from text.splitlines()


def _call_with(patterns, text):
    """The screen with `patterns` as its registry (`detail.SCREEN_PATTERNS`), restored after."""
    real = detail.SCREEN_PATTERNS
    detail.SCREEN_PATTERNS = patterns
    try:
        return detail.screen(text)
    finally:
        detail.SCREEN_PATTERNS = real


def _novel_variant(text):
    """The variant spans that no bfb66b189 class covers, at any pass of the screen over `text`: [] when
    the t-425 class adds nothing to what the bfb66b189 classes mask (then the screen must give the
    bfb66b189 output). Each entry is (pass, family, the value)."""
    found, value = [], text
    for step in range(detail._REDACT_PASSES):
        old = [span for name, hint, find in detail.SCREEN_PATTERNS
               if name in FROZEN_NAMES and hint(value) for span in find(value)]
        if detail._VARIANT_HINT.search(value):
            for family, find in detail.VARIANT_FAMILIES:
                for start, stop in detail._variant_spans(value, ((family, find),)):
                    if not any(a <= start and stop <= b for a, b in old):
                        found.append((step, family, value[start:stop]))
        after = detail._screen_once(value)
        if after == value:
            break
        value = after
    return found


@pytest.mark.parametrize("corpus", sorted(EXHAUSTIVE) + ["random", "fixtures"])
def test_the_screen_changes_only_where_a_declared_variant_applies(corpus):
    """The bfb66b189 classes are unchanged: each finds the spans the bfb66b189 pattern found, and the
    screen without the t-425 class gives the bfb66b189 screen's output. The t-425 class changes a text
    only where one of its declared forms finds a value no bfb66b189 class masks (`_novel_variant`);
    every other text is screened to the bfb66b189 bytes, so a stored projection, an evaluation digest
    and a fixed-set answer change only where such a value was (EX/fix-plan-r2.md section 9; t-425).
    Where it does change a text, the result is still a fixed point of the bfb66b189 screen: it masks
    more, never less. Exhaustive on small alphabets, then seeded random text from every prefix, unit,
    phrasing and glued secret above, then every fixture text."""
    registry = _registry()
    differ, changed = [], []
    if corpus in EXHAUSTIVE:
        name, alphabet, longest, prefixes = EXHAUSTIVE[corpus]
        new, old = registry[name], FROZEN_CLASSES[name]
        for text in _exhaustive(alphabet, longest, prefixes):
            if new(text) != old(text):
                differ.append(text)
            elif name == "plain-phrasing" and ([m.regs for m in detail._PLAIN_SECRET.finditer(text)]
                                               != [m.regs for m in frozen._PLAIN_SECRET.finditer(text)]):
                differ.append(text)
            if len(differ) > 5:
                break
    else:
        count = 0
        texts = (itertools.chain.from_iterable(_random_texts(seed, 5000) for seed in range(20))
                 if corpus == "random" else _fixture_texts())
        frozen_only = [entry for entry in detail.SCREEN_PATTERNS if entry[0] in FROZEN_NAMES]
        for text in texts:
            count += 1
            before = frozen.screen(text)
            out = detail.screen(text)
            if _call_with(frozen_only, text) != before or any(
                    registry[name](text) != FROZEN_CLASSES[name](text) for name in FROZEN_NAMES):
                differ.append(("bfb66b189 class changed", text[:300]))
            elif out != before:
                novel = _novel_variant(text)
                if not novel or frozen.screen(out) != out:
                    differ.append(("changed without a declared variant" if not novel
                                   else "not a bfb66b189 fixed point", text[:300]))
                else:
                    changed.append(sorted({family for _, family, _ in novel}))
            if len(differ) > 5:
                break
    assert differ == [], "the screen differs from bfb66b189 on: %r" % differ
    if corpus not in EXHAUSTIVE:
        assert count > (90_000 if corpus == "random" else 1_000), count


# ------------------------------------------------------------------ (e) t-425, the declared variants
#: Attack p01's 21 leaking phrasings (`.alpaca/proofs/op-008/t-070.logs/attacks/p01_secret_phrasing_variants.py`):
#: (label, text, the value that must never be stored, the screen's output).
VARIANTS = [
    ("passwords-plural", "The router passwords are ATKplural-77 and ATKplural-88.", "ATKplural-77",
     "The router passwords are [redacted] and [redacted]."),
    ("password-long-clause",
     "The password for the quay camera on the north pier by the old harbour gate is ATKlong-5150.",
     "ATKlong-5150", "The password for the quay camera on the north pier by the old harbour gate is [redacted]."),
    ("password-comma-clause", "The admin password, which the vendor set, is ATKcomma-6262.", "ATKcomma-6262",
     "The admin password, which the vendor set, is [redacted]."),
    ("pin-spaced", "The gate PIN is 7 3 1 9.", "7 3 1 9", "The gate PIN is [redacted]."),
    ("password-table", "| system | password |\n|---|---|\n| quay camera | ATKtable-8484 |", "ATKtable-8484",
     "| system | password |\n|---|---|\n| quay camera | [redacted] |"),
    ("env-db-pass", "export DB_PASS=ATKdbpass-9595", "ATKdbpass-9595", "export DB_PASS=[redacted]"),
    ("pwd-abbrev", "pwd: ATKpwd-1616", "ATKpwd-1616", "pwd: [redacted]"),
    ("reverse-order", "use ATKrev-orchid2 as password", "ATKrev-orchid2", "use [redacted] as password"),
    ("crlf-break", "The kiosk password is\r\nATKcrlf-1212", "ATKcrlf-1212", "The kiosk password is\r\n[redacted]"),
    ("nbsp-before-value", "The kiosk password is\u00a0ATKnbsp-7373.", "ATKnbsp-7373",
     "The kiosk password is\u00a0[redacted]."),
    ("nbsp-before-is", "The relay password\u00a0is ATKnbsp2-7474.", "ATKnbsp2-7474",
     "The relay password\u00a0is [redacted]."),
    # the secret-assignment class (bfb66b189) masks the "**" after "Password:" as it did before
    ("md-bold-colon-in", "**Password:** ATKbold-3131", "ATKbold-3131", "**Password:[redacted] [redacted]"),
    ("md-bold-colon-out", "**Password**: ATKbold2-3232", "ATKbold2-3232", "**Password**: [redacted]"),
    ("md-bullet-bold", "- **wifi password** is ATKbold3-3333", "ATKbold3-3333", "- **wifi password** is [redacted]"),
    ("has-been-changed", "The wifi password has been changed to ATKhbc-4545.", "ATKhbc-4545",
     "The wifi password has been changed to [redacted]."),
    ("will-be", "The new gate PIN will be 55117733.", "55117733", "The new gate PIN will be [redacted]."),
    ("pw-abbrev", "pw: ATKpw-6767", "ATKpw-6767", "pw: [redacted]"),
    ("tab-sep", "password\tATKtab-8989", "ATKtab-8989", "password\t[redacted]"),
    ("dash-sep", "Router password - ATKdash-9191", "ATKdash-9191", "Router password - [redacted]"),
    # likewise the ">" after "password:"
    ("yaml-block", "db:\n  password: >\n    ATKyaml-2323", "ATKyaml-2323", "db:\n  password: [redacted]\n    [redacted]"),
    ("contraction", "The wifi password's ATKcontr-1414.", "ATKcontr-1414", "The wifi password's [redacted]."),
]
#: The family each variant is read by (`detail.VARIANT_FAMILIES`).
FAMILY_OF = {"passwords-plural": "verb", "password-long-clause": "verb", "password-comma-clause": "verb",
             "pin-spaced": "spaced-digits", "password-table": "table", "env-db-pass": "env-name",
             "pwd-abbrev": "short-name", "reverse-order": "reverse", "crlf-break": "spacing",
             "nbsp-before-value": "spacing", "nbsp-before-is": "spacing", "md-bold-colon-in": "separator",
             "md-bold-colon-out": "separator", "md-bullet-bold": "verb", "has-been-changed": "verb",
             "will-be": "verb", "pw-abbrev": "short-name", "tab-sep": "separator", "dash-sep": "separator",
             "yaml-block": "yaml-block", "contraction": "verb"}
#: Prose of the same shapes the variants must leave as it is: a possessive, counted tokens, PINs and
#: secrets, a path `pwd` printed, shell and code names, a table of token counts, a hash named as the
#: password's, letters after a clause or a separator, words that read as prose.
VARIANT_PROSE = [
    "The password's length is 12 characters.", "the input tokens were 12000 and output tokens were 3400",
    "pwd: /home/someone/x1", "PWD=/home/x/1", "OLDPWD=/x/2", "first_pass = True", "LED_PIN=13",
    "| model | tokens |\n|---|---|\n| opus | 12345 |", "use sha256 as password hash",
    "The password - see the vault", "password will be rotated",
    "The password has been changed to something stronger",
    "The password for the new account that we created on the server is somewhere safe",
    "The pins are 1234 and 5678", "the secrets are 2026 era", "Passwords are hashed with bcrypt12 rounds",
    "The password of user 2 is unknown", "password\tusername", "the analysis password 1234",
    "| field | password |\n|---|---|\n| required | yes |", "**Password**: required",
    "The passwords are stored in the vault.", "password:\n  - rotate weekly",
    # code constants the env-name form met in the evaluation corpus's transcripts (t-425 X run)
    '82:R_PIN = "PINNED-BARRIER-UNUSABLE-REFUSES"', '    72\tR_CLEARANCE_NOT_PASS = "CLEARANCE-VERDICT-NOT-PASS"',
    "TEST_PASS=PASSED",
]


@pytest.mark.parametrize("label,text,marker,expected", VARIANTS, ids=[v[0] for v in VARIANTS])
def test_every_declared_variant_is_masked(label, text, marker, expected):
    out = drain.screen(text)
    assert marker not in out and out == expected, out
    assert drain.screen(str(out)) == out and detail._redact(text) == out
    families = {family for _, family, _ in _novel_variant(text)}
    assert FAMILY_OF[label] in families, families


@pytest.mark.parametrize("text", VARIANT_PROSE)
def test_the_variants_leave_prose_of_their_shapes(text):
    """Each is screened as bfb66b189 screened it: unchanged, or ("password:" then a line end) with
    only what the secret-assignment class masked before."""
    assert drain.screen(text) == frozen.screen(text) and _novel_variant(text) == [], drain.screen(text)


def test_a_letters_value_is_masked_where_the_plain_phrasing_masks_one():
    """A value of letters alone is masked after "are", "has been changed to" or "will be", by the plain
    phrasing's rule, and never after a separator, a clause or "'s" (operators.md)."""
    masked = ["The router passwords are swordfishmarlin and quaysidekestrel.",
              "The wifi password has been changed to swordfishmarlin.",
              "The wifi password will be swordfishmarlin.", "- **wifi password** is swordfishmarlin"]
    for text in masked:
        out = drain.screen(text)
        assert "swordfishmarlin" not in out and "quaysidekestrel" not in out, out
    kept = ["**Password**: swordfishmarlin", "pw: swordfishmarlin", "The wifi password's swordfishmarlin.",
            "The admin password, which the vendor set, is swordfishmarlin."]
    for text in kept:
        assert drain.screen(text) == text


def test_every_declared_variant_is_masked_at_every_door(project, tmp_path):
    """Attack p01 at the five retain doors (wiki ingest, a direct Absorber call, an event note, a
    Claude transcript, an Alpaca proof source): no active block, vault file, export or served answer
    holds any of the 21 values (t-425 done bar)."""
    import hashlib
    from alpaca.wiki.config import Config
    from alpaca.wiki.ingest.absorb import Absorber
    body = "# Harbour credentials\n\n" + "\n\n".join(line for _, line, _, _ in VARIANTS) + "\n"
    markers = [marker for _, _, marker, _ in VARIANTS]
    page = tmp_path / "creds.md"
    page.write_bytes(body.encode("utf-8"))
    service.ingest(str(project), str(page), doc_id="wiki/notes/creds.md", session="s-ingest")
    absorber = Absorber(Config.for_project(str(project)))
    try:
        absorber.absorb_text("wiki/synthetic/creds.md", body, kind="wiki", path="wiki/synthetic/creds.md")
    finally:
        absorber.close()
    eid = append(project, "result", {"body": body}, session="s-note", op="op-atk", ref="t-atk")
    capture(project, {eid})
    talk = tmp_path / "s-tr.jsonl"
    rows = [{"type": "user", "timestamp": "2026-10-04T09:00:00Z", "message": {"content": body}},
            {"type": "assistant", "timestamp": "2026-10-04T09:00:05Z",
             "message": {"content": [{"type": "text", "text": "Noted."}]}}]
    talk.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    append(project, "result", {"body": "The transcript session ran."}, session="s-tr")
    (entry,) = drain.run(str(project), "s-tr", transcript_path=str(talk))["transcripts"]
    proof = Path(project, ".alpaca", "proofs", "op-atk", "t-atk.md")
    proof.parent.mkdir(parents=True, exist_ok=True)
    proof.write_text("# Proof t-atk\n\n## At a glance\n\n" + body, encoding="utf-8")
    sealed = append(project, "proof-report", {"path": ".alpaca/proofs/op-atk/t-atk.md",
                                              "sha256": hashlib.sha256(proof.read_bytes()).hexdigest()},
                    session="s-src", op="op-atk", ref="t-atk")
    sources = [d for d in capture(project, {sealed})["docs"] if d.startswith("raw/sources/")]
    with store(project) as conn:
        held = {doc: "\n".join(r[0] for r in conn.execute(
            "SELECT text FROM blocks WHERE doc_id LIKE ? AND status='active'", (pattern,)))
            for doc, pattern in (("wiki-ingest", "wiki/notes/creds.md"), ("absorber", "wiki/synthetic/creds.md"),
                                 ("event-note", "raw/%%/%06d-%%" % eid), ("transcript", entry["doc_id"]),
                                 ("alpaca-source", sources[0] if sources else "-"))}
    assert sources and all(held.values()), {k: len(v) for k, v in held.items()}
    leaks = sorted((door, m) for door, text in held.items() for m in markers if m in text)
    surfaces = _surfaces(project, tmp_path, "variants")
    surfaces["served"] = json.dumps(service.context(
        project, "What are the kiosk, relay and wifi passwords and the gate PIN?", max_chars=16000)) + json.dumps(
        service.query(project, "What is the kiosk password?"))
    assert leaks == [] and _leaks(surfaces, markers) == [], (leaks, _leaks(surfaces, markers))


# The door: an exception's text that reaches the record goes through the one screen.
PACKAGE = Path(__file__).resolve().parents[2]
#: The calls that append to the record: `db.append_event`, a door's record appender (a plain
#: `record(...)` or `append(...)`) and an instrument's verdict, whose reason becomes a `run` row
#: (`gates/verdict.py` `_record_run`).
RECORD_APPENDS = {"append_event", "emit_verdict"}
PLAIN_APPENDERS = {"record", "append"}
#: The calls that screen a text: the one screen and the helpers that apply it.
SCREENS = {"screen", "_screen", "_describe", "_redact", "error_reason"}
#: Verdict reasons outside Rune2 that carry an exception's text unscreened into a `run` row, by module
#: and count. Empty since t-429: they go through `verdict.error_reason`, which screens when the screen
#: imports and else writes the exception type and a fixed message. A new one fails this test.
OPEN_VERDICT_REASONS = {}


def _call_name(call):
    f = call.func
    return f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)


def _reads(node, tainted):
    """True when `node` reads a tainted name's text: the exception itself (str(e), repr(e), an
    f-string, a % format) or its `args`, other than through `type(e)` or a screen. Another attribute
    (`h.code`, `exc.reason`) is a field the exception's class defines, not its message."""
    if isinstance(node, ast.Call) and (_call_name(node) in SCREENS
                                       or (_call_name(node) == "type" and len(node.args) == 1)):
        return False
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) \
            and node.value.id in tainted and node.attr != "args":
        return False
    if isinstance(node, ast.Name) and node.id in tainted:
        return True
    return any(_reads(child, tainted) for child in ast.iter_child_nodes(node))


def _own(node):
    """Every node of a scope, without entering a nested function, lambda or class."""
    for child in ast.iter_child_nodes(node):
        yield child
        if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            yield from _own(child)


def _unscreened_error_appends(tree):
    """(line, callee) of every record append, in a function, that reads an exception caught in that
    function (`except ... as e`) or a name assigned from it there, other than through `type(e)`, a
    field of the exception, or a screen."""
    hits = []
    scopes = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))] + [tree]
    for scope in scopes:
        nodes = list(_own(scope))
        handlers = [h for h in nodes if isinstance(h, ast.ExceptHandler) and h.name]
        inside = {id(n): h for h in handlers for n in _own(h)}
        derived = set()
        for _ in range(3):                         # names assigned from the exception, to a fixpoint
            for node in nodes:
                handler = inside.get(id(node))
                if handler is None or not isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)) \
                        or node.value is None:
                    continue
                if _reads(node.value, derived | {handler.name}):
                    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                    derived |= {n.id for t in targets for n in ast.walk(t) if isinstance(n, ast.Name)}
        for node in nodes:
            if not isinstance(node, ast.Call):
                continue
            name = _call_name(node)
            if name in RECORD_APPENDS or (isinstance(node.func, ast.Name) and name in PLAIN_APPENDERS):
                handler = inside.get(id(node))
                tainted = derived | ({handler.name} if handler else set())
                if tainted and any(_reads(arg, tainted) for arg in list(node.args) + [k.value for k in node.keywords]):
                    hits.append((node.lineno, name))
    return sorted(set(hits))


def test_every_record_append_of_error_text_is_screened():
    """A-9 and the door rule: an exception's message can quote what failed (a config line, a
    command, a payload), so every record append of it goes through the one screen, here or in
    `capture_runner._describe`. RC2's hand list of door call sites missed
    `hooks/session_end.py` (`drain-failed` {where: session_end}) and `cli.py` (`wiki recover`)."""
    found, verdicts = [], {}
    for path in sorted(PACKAGE.rglob("*.py")):
        rel = path.relative_to(PACKAGE).as_posix()
        if rel.startswith("tests/") or "__pycache__" in rel:
            continue
        for line, name in _unscreened_error_appends(ast.parse(path.read_text(encoding="utf-8"))):
            if name == "emit_verdict" and rel in OPEN_VERDICT_REASONS:
                verdicts[rel] = verdicts.get(rel, 0) + 1
                continue
            found.append("%s:%d %s" % (rel, line, name))
    assert found == [], "screen the error text of these record appends: %s" % found
    assert verdicts == OPEN_VERDICT_REASONS


def test_the_error_text_check_sees_each_way_of_formatting_an_exception():
    """The AST check above reads every form an exception's text takes: an f-string, a % format,
    str(e), a name assigned from it, and through a door's record appender; type(e) alone and a
    screened text pass."""
    bad = '''
def f(conn, record):
    try:
        pass
    except Exception as e:
        db.append_event(conn, data={"error": "%s: %s" % (type(e).__name__, e)})
        db.append_event(conn, data={"error": f"{e}"})
        msg = str(e)
        record("drain-failed", {"error": msg})
        vc.emit_verdict("g", 1, repr(e))
'''
    good = '''
def f(conn, record):
    try:
        pass
    except Exception as e:
        db.append_event(conn, data={"error": capture_runner._describe(e)})
        db.append_event(conn, data={"type": type(e).__name__})
        record("drain-failed", {"error": detail.screen(str(e))})
'''
    assert [n for _l, n in _unscreened_error_appends(ast.parse(bad))] == [
        "append_event", "append_event", "record", "emit_verdict"]
    assert _unscreened_error_appends(ast.parse(good)) == []


@pytest.mark.parametrize("site", ["session_end", "wiki-recover"])
def test_a_secret_in_a_failure_reaches_the_record_masked(project, monkeypatch, site):
    """The two appends the hand list missed, run: a failure whose message holds a secret is
    recorded with the secret masked (`drain-failed` {where: session_end}; `drain-failed` {where:
    wiki-recover} and the verb's `run` row)."""
    secret = "T070-err-4711"

    def boom(*_a, **_k):
        raise RuntimeError("relay refused: password is %s" % secret)
    if site == "session_end":
        from alpaca import observability, profile
        from alpaca.hooks import common, session_end
        monkeypatch.setattr(common, "run_step", lambda *a, **k: None)      # pad, snapshot, analytics
        monkeypatch.setattr(observability, "enabled", lambda root: False)
        monkeypatch.setattr(observability, "collector_alive", lambda root: True)
        monkeypatch.setattr(profile, "load", boom)
        session_end.handle({"session_id": "s-err", "cwd": project})
        kinds = {"drain-failed"}
    else:
        from alpaca import cli
        from alpaca.wiki import recovery
        monkeypatch.setattr(recovery, "run", boom)
        cli.main(["--session", "s-err", "wiki", "recover"])
        kinds = {"drain-failed", "run"}
    rows = [e for e in events(project) if e["kind"] == "drain-failed"
            or (e["kind"] == "run" and e["ref"] == "alpaca-wiki-recover")]
    assert {e["kind"] for e in rows} == kinds, rows
    raw = json.dumps([e["data"] for e in rows])
    assert secret not in raw and "[redacted]" in raw, raw


#: The drain's private token: only `drain.screen` passes it to `Screened`.
SCREEN_TOKEN = "_SCREEN_TOKEN"


def test_only_screen_makes_a_screened_value():
    """I-4: `Screened` tells the write door a text needs no second pass, so a caller that builds one
    from unscreened text skips the screen. Only `drain.screen` can make one: the class refuses any
    call without the drain's private token, a copy or a pickle is a plain `str` again (the door
    screens it), and no module but the drain builds one or names the token."""
    forged = "The relay admin password is T070-forge-4711."
    with pytest.raises(TypeError):
        drain.Screened(forged)
    with pytest.raises(TypeError):
        drain.Screened.__new__(drain.Screened, forged)
    made = drain.screen(forged)
    assert type(made) is drain.Screened and "T070-forge-4711" not in made
    for again in (copy.copy(made), copy.deepcopy(made), pickle.loads(pickle.dumps(made))):
        assert again == made and type(again) is str
    builders = []
    for path in sorted(PACKAGE.rglob("*.py")):
        rel = path.relative_to(PACKAGE).as_posix()
        if rel.startswith("tests/") or "__pycache__" in rel:
            continue
        for where, node in _walk_with_scope(ast.parse(path.read_text(encoding="utf-8"))):
            built = isinstance(node, ast.Call) and (_call_name(node) == "Screened" or (
                _call_name(node) == "__new__" and any(getattr(a, "id", getattr(a, "attr", None)) == "Screened"
                                                      for a in node.args)))
            named = getattr(node, "id", None) == SCREEN_TOKEN or getattr(node, "attr", None) == SCREEN_TOKEN
            if built or named:
                builders.append((rel, where, "builds" if built else "names the token"))
    outside = [b for b in builders if b[0] != "wiki/ingest/drain.py"]
    assert outside == [], outside
    assert {where for rel, where, what in builders if what == "builds"} == {"screen"}, builders


def _walk_with_scope(tree):
    """(enclosing def or class, a dotted name, or "" at module level; node) for every node."""
    def walk(node, where):
        for child in ast.iter_child_nodes(node):
            name = where
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = child.name if not where else where + "." + child.name
            yield name, child
            yield from walk(child, name)
    yield from walk(tree, "")


def test_error_reason_falls_back_to_type_and_fixed_text_when_the_screen_cannot_import(monkeypatch):
    """t-429: a half-failed upgrade may not import `alpaca.analytics.detail`; the verdict reason then
    carries only the exception type and a fixed message, never the exception's text."""
    import builtins
    import sys
    from alpaca.gates import verdict as vc
    secret = "api_key=sk-abcdefghijklmnopqrstuvwx"
    err = RuntimeError(secret)
    ok = vc.error_reason(err)
    assert ok.startswith("RuntimeError: ") and "sk-abcdefghijklmnopqrstuvwx" not in ok
    real = builtins.__import__

    def refuse(name, *a, **k):
        if name == "alpaca.analytics.detail" or name == "alpaca.analytics" and a[2:3] == (("detail",),):
            raise ImportError("broken tree")
        return real(name, *a, **k)

    monkeypatch.delitem(sys.modules, "alpaca.analytics.detail", raising=False)
    monkeypatch.setattr(builtins, "__import__", refuse)
    out = vc.error_reason(err)
    assert out.startswith("RuntimeError: ") and secret not in out and "sk-" not in out
    assert out == vc.error_reason(RuntimeError("something else"))


@pytest.mark.parametrize("label,text,marker,expected", VARIANTS, ids=[v[0] for v in VARIANTS])
def test_error_reason_masks_every_declared_variant(label, text, marker, expected):
    """t-429 with t-425 (op008/followups): a verdict reason goes through the same screen as the
    write door, so each of attack p01's 21 phrasings in an exception's text is masked there too, and
    the reason is the exception type and the screen's output for that text."""
    from alpaca.gates import verdict as vc
    out = vc.error_reason(RuntimeError(text))
    assert marker not in out and out == "RuntimeError: " + expected, out
