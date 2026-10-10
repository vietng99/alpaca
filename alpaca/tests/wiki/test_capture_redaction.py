"""op-008 t-069 stage s1-capture: capture redaction at every retain door (FR-26, HS-SCO-05;
dec-804f2708474c Q-A; design/rune2-memory/implementation-plan.md t-069).

The shared redactor (alpaca/analytics/detail.py `_redact`) gains four classes, an AWS access key
(and the AWS secret key named as one), a PEM private key block, a Slack token and a JWT, and the plain
phrasings of Q-A ('password is X', 'PIN is X', 'token is X'). The screen (`drain.screen`) runs at the
shared retain door, `Absorber.absorb_text`, which every ingest path uses: `drain.capture` for event
notes, Alpaca sources and transcripts, `service.ingest` for a wiki ingest document, and the direct
Absorber call of the evaluation rebuild. Each renderer applies it too, so the vault file, the stored
text and `recovery.verify_documents` agree. A secret never reaches a block, an index or a vault file
Rune2 writes, so no surface can serve it. Real drain, real write path, real door; nothing is mocked.
"""
import hashlib
import itertools
import json
import os
import re
import sys
import unicodedata
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from native_helpers import append, events, rebuild_store, store, vault  # noqa: E402

from alpaca import db as record                           # noqa: E402
from alpaca.analytics import detail                       # noqa: E402
from alpaca.wiki import service                           # noqa: E402
from alpaca.wiki.ingest import drain                      # noqa: E402

PEM = ("-----BEGIN RSA PRIVATE KEY-----\nMIIEt069PEMbodyEXAMPLEAQEAzq9Rw1Lp0Tn3Vx7Yb2\n"
       "T069PEMsecondLineEXAMPLE==\n-----END RSA PRIVATE KEY-----")

#: class -> (a line that holds a secret of that class, the parts of it that must never be stored)
SAMPLES = {
    "aws-access-key": ("The uploader uses access key id AKIAQ7EXAMPLE4T069AB for the bucket.",
                       ["AKIAQ7EXAMPLE4T069AB"]),
    "aws-secret-key": ("aws_secret_access_key = t069AwsSecretEXAMPLEkey/Zq9+Rw1Lp0Tn3Vx7Yb2",
                       ["t069AwsSecretEXAMPLEkey"]),
    "pem-block": ("The deploy key is below.\n" + PEM,
                  ["MIIEt069PEMbodyEXAMPLE", "T069PEMsecondLineEXAMPLE"]),
    "slack-token": ("The alert bot posts with xoxb-1234567890-0987654321-t069SlackEXAMPLEtoken daily.",
                    ["t069SlackEXAMPLEtoken", "xoxb-1234567890"]),
    "jwt": ("The session cookie was eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ0MDY5In0.t069JwtSignatureEXAMPLE "
            "at login.", ["t069JwtSignatureEXAMPLE", "eyJzdWIiOiJ0MDY5In0"]),
    "password-is": ("The relay admin password is T069-pass-4711.", ["T069-pass-4711"]),
    "pin-is": ("The dock gate PIN is T069-pin-2024.", ["T069-pin-2024"]),
    "token-is": ("The buoy feed token is T069-tok-9q8w.", ["T069-tok-9q8w"]),
    # the same phrasings with a value of letters only (check round 1 adversarial B2)
    "password-is-letters": ("The wifi password is swordfishmarlin.", ["swordfishmarlin"]),
    "passphrase-is-words": ("The vault passphrase is plankton otter quayside.",
                            ["plankton", "otter quayside"]),
    "token-is-letters": ("The deploy token is QwErTyUiOpAsDfGhJkLzXcVb.", ["QwErTyUiOpAsDfGhJkLzXcVb"]),
    # the classes the redactor had before t-069, now applied to every retain door too
    "api-key-assignment": ("The uploader config line is api_key = T069-api-1234.", ["T069-api-1234"]),
    "bearer": ("The feed client sends Authorization: Bearer T069-bearer-5678.", ["T069-bearer-5678"]),
}
MARKERS = sorted({m for _, parts in SAMPLES.values() for m in parts})
DOCUMENT = "# Harbor credentials note\n\n" + "\n\n".join(line for line, _ in SAMPLES.values()) + "\n"

#: Five planted secrets in the shapes of the evaluation's secret items (an api_key line, an admin
#: password, a bearer header, a 'token is' line and a 'service PIN is' line), with synthetic words and
#: values, never the question set's own. They enter as wiki ingest documents through the Absorber, the
#: way the evaluation rebuild ingests its fixtures.
PLANTED = [
    ("sec-pump-logger", "# Pump logger\n\nThe pump logger config line is api_key = "
     "SYNTHCANARY-7e2b4d91c0.\n", "What API key does the pump logger send?", "SYNTHCANARY-7e2b4d91c0"),
    ("sec-kiln", "# Kiln controller\n\nThe kiln controller admin password is SYNTHCANARY-pass-3c17.\n",
     "What is the admin password of the kiln controller?", "SYNTHCANARY-pass-3c17"),
    ("sec-ferry-feed", "# Ferry feed\n\nThe ferry feed client uses Authorization: Bearer "
     "SYNTHCANARY-tok-a40f6e.\n", "Which bearer token does the ferry feed client use?",
     "SYNTHCANARY-tok-a40f6e"),
    ("sec-rain-gauge", "# Rain gauge\n\nThe rain gauge token is SYNTHCANARY-rg-92d4.\n",
     "What is the rain gauge token?", "SYNTHCANARY-rg-92d4"),
    ("sec-boiler-panel", "# Boiler panel\n\nThe boiler panel service PIN is SYNTHCANARY-pin-7310.\n",
     "What is the boiler panel service PIN?", "SYNTHCANARY-pin-7310"),
]


def _norm(text: str) -> str:
    """As the evaluation report reads a leak (report.py:209-214, common.py:95-103): NFC, casefold,
    whitespace collapsed."""
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", text or "").casefold()).strip()


def _leaks(texts, markers) -> list:
    joined = [_norm(t) for t in texts]
    return [m for m in markers if any(_norm(m) in t for t in joined)]


def _served(project, question) -> list:
    """Every text a caller is handed for one question: the answer, each citation's claim, the
    sources as JSON and the context at the pinned budget (what report.py scans, :124-132)."""
    answer = service.query(project, question)
    texts = [answer.get("answer_text") or ""]
    texts += [c.get("claim_text") or "" for c in answer.get("citations") or []]
    texts += [json.dumps(s, ensure_ascii=False) for s in answer.get("sources") or []]
    texts.append(service.context(project, question, max_chars=2500))
    return texts


def _stored_leaks(project, markers, *, skip=()) -> list:
    """Every file Rune2 keeps under the vault (the store, its WAL, the ledger, every captured
    document) scanned byte for byte, the store also for the lower-cased form an index token takes.
    `skip` names vault files Rune2 did not write (the evaluation rebuild writes its own fixture)."""
    found = []
    for path in sorted(vault(project).rglob("*")):
        if not path.is_file() or path.relative_to(vault(project)).as_posix() in skip:
            continue
        data = path.read_bytes()
        for m in markers:
            for form in {m, m.lower()}:
                if form.encode("utf-8") in data:
                    found.append((path.relative_to(vault(project)).as_posix(), m))
    return found


def _blocks(project, like: str, status: str = "active") -> str:
    with store(project) as conn:
        return "\n".join(r[0] for r in conn.execute(
            "SELECT text FROM blocks WHERE doc_id LIKE ? AND status=? ORDER BY doc_id, ordinal",
            (like, status)))


# ------------------------------------------------------------------ the redactor
@pytest.mark.parametrize("name", sorted(SAMPLES))
def test_the_redactor_masks_each_class(name):
    line, parts = SAMPLES[name]
    out = detail._redact(line)
    assert "[redacted]" in out
    for part in parts:
        assert part not in out, (name, out)
    assert detail._redact(out) == out                     # idempotent


def test_the_redactor_masks_the_five_planted_phrasings():
    for _, text, _, marker in PLANTED:
        out = detail._redact(text)
        assert marker not in out and "[redacted]" in out, out
        assert detail._redact(out) == out


@pytest.mark.parametrize("text", [
    "The token is valid.", "The password is incorrect.", "the pin is loose", "the exit code is 2",
    "key: t-069", "the token count is 5000", "each token is 4 bytes", "op: token-rotation",
    "credentials are in the vault", "max_tokens: 4096", "spin: 12345",
    # "use hunter2-orchid as password" left this list in t-425: the reverse order is a declared
    # variant now (`test_screen_door.py` VARIANTS, reverse-order), masked
    "Handling NOFORN material: the vault door code is amber.", "use sha256 as password hash",
    "artifacts.capture(root, source, pin='acceptance baseline')", "token = secrets.token_hex(16)",
    "-----BEGIN CERTIFICATE-----\nMIIC\n-----END CERTIFICATE-----",
    "event: 4711\nts: 2026-10-04T10:00:00+00:00",
    # a value of letters only is masked only when it reads as no prose word (check round 1 B2)
    "The passphrase is stored in the vault.", "the secret is out",
    "The token is consumed by the hook.", "the token is opaque and short-lived",
    "the token was rotated", "the token is expiring soon", "the password is the same as before",
    "the token is self.token", "the token is session_token", "the token is now required",
    "the credentials are environment variables", "the PIN is wrong", "The token is JWT",
    "the pin is broken", "Our secret is teamwork", "the token is huge",
])
def test_the_redactor_leaves_ordinary_text_alone(text):
    assert detail._redact(text) == text


def test_a_plain_value_of_letters_is_masked_and_a_passphrase_to_the_end_of_its_clause():
    """Check round 1 adversarial B2: Q-A names "password is X", "PIN is X", "token is X", and X need
    not hold a digit. A passphrase's value is its words, up to the end of the clause or a word that
    starts another credential phrasing, which is masked on its own."""
    assert detail._redact("The wifi password is swordfishmarlin.") == "The wifi password is [redacted]."
    assert detail._redact("The deploy token was QwErTyUiOpAsDf, then") == "The deploy token was [redacted], then"
    assert detail._redact("The vault passphrase is plankton otter quayside password is hunter22 here.") \
        == "The vault passphrase is [redacted] password is [redacted] here."
    assert detail._redact("The router password for the north pier is Swordfish!") \
        == "The router password for the north pier is [redacted]!"
    # after "=" or ":" a value of letters stays (a config literal); the old name rules still mask
    # a password, passphrase or secret there whatever it holds
    assert detail._redact("token: abcdefgh") == "token: abcdefgh"
    assert detail._redact("Password: tidewatermoon") == "Password: [redacted]"


def test_the_redactor_is_idempotent_on_mixed_text():
    """The door applies the screen to text a renderer already screened, so the screen must be
    idempotent: screen(screen(x)) == screen(x) for any mix of secret lines and ordinary prose."""
    pieces = [line for line, _ in SAMPLES.values()] + [text for _, text, _, _ in PLANTED] + [
        "token to are 5000 was 5000", "the password for the kiln controller is hunter22!",
        "the wifi password is \"correct horse battery staple\"", "PIN:\n  4096x", "Bearer token is abc123",
        "PASSWORD=\"abc def\"", "{\"password\": \"hunter2\", \"token\": \"tok-1a2b\"}", "token=token2",
        "the session token was changed to abc12345 yesterday", "pin to version 3 is 2026-10-04",
        "The wifi password is swordfishmarlin.", "the passphrase is plankton otter quayside",
        "the passphrase is plankton otter password is hunter22", "token is QwErTyUiOpAsDf"]
    for a, b in itertools.product(pieces, repeat=2):
        for joint in (" ", "\n", "\n\n", ". "):
            text = a + joint + b
            once = detail._redact(text)
            assert detail._redact(once) == once, text
    assert drain.screen(DOCUMENT) == detail._redact(DOCUMENT)


#: One secret of each class the redactor knows, as it would sit in a text, for the glued-pair checks
#: (t-069 check round 2 invariants B1: one pass of the classes in a fixed order was not idempotent
#: when one secret was glued to another).
GLUE = {
    "aws-access-key": "AKIAIOSFODNN7EXAMPLE",
    "aws-secret-key": "aws_secret_access_key=t069AwsSecretEXAMPLEkey",
    "pem-block": PEM,
    "slack-token": "xoxb-1234567890-0987654321-t069SlackEXAMPLEtoken",
    "slack-webhook": "https://hooks.slack.com/services/T0/B0/xyz",
    "jwt": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ0MDY5In0.t069JwtSignatureEXAMPLE",
    "provider-key": "sk-t069providerEXAMPLEkey1234",
    "github-token": "ghp_t069githubEXAMPLEtoken12345",
    "bearer": "Bearer T069-bearer-5678",
    "api-key-assignment": "API_KEY: T069-api-1234",
    "password-is": "password is swordfishmarlin",
    "pin-was": "pin was T069-pin-2024",
    "token-is": "token is T069-tok-9q8w",
    "passphrase-is": "passphrase is plankton otter quayside",
}
#: The three strings of the check round's screen cases (.alpaca/proofs/op-008/t-069.logs/
#: check-invariants-r2-screen-cases.txt); the first and the third were not idempotent.
CHECKER_CASES = [
    "The uploader key AKIAIOSFODNN7EXAMPLEhttps://hooks.slack.com/services/T0/B0/xyz stays here.",
    "The key AKIAIOSFODNN7EXAMPLEeyJhbGciOi.eyJzdWIi.sig was pasted.",
    "API_KEY: pin was password is\nswordfishmarlin\n",
]
GLUED = CHECKER_CASES[0]
GLUED_PHRASE = CHECKER_CASES[2]


@pytest.mark.parametrize("text", CHECKER_CASES)
def test_the_screen_is_a_fixed_point_on_the_checker_cases(text):
    once = drain.screen(text)
    assert drain.screen(once) == once
    assert detail._redact(detail._redact(text)) == detail._redact(text)
    assert "AKIAIOSFODNN7EXAMPLEh" not in once                 # the glued key reads as two secrets
    assert "swordfishmarlin" not in drain.screen(GLUED_PHRASE)
    assert drain.screen(GLUED) == "The uploader key [redacted][redacted] stays here."


@pytest.mark.parametrize("first", sorted(GLUE))
def test_the_screen_is_a_fixed_point_on_glued_pairs_of_every_class(first):
    """screen(screen(x)) == screen(x) and _redact(_redact(x)) == _redact(x) for every ordered pair
    of classes glued together with no gap, a space or a newline, alone or inside a sentence."""
    for second in sorted(GLUE):
        for joint in ("", " ", "\n"):
            for frame in ("%s", "The key %s stays here.", "[%s]", "note: %s\n"):
                text = frame % (GLUE[first] + joint + GLUE[second])
                once = drain.screen(text)
                assert drain.screen(once) == once, text
                redacted = detail._redact(text)
                assert detail._redact(redacted) == redacted, text


def test_settle_keeps_a_fixed_point_and_fails_closed_at_its_cap():
    """`settle` stops at the first pass that changes nothing; a step still changing at the cap
    masks the whole string, which is itself a fixed point, so no secret is kept and a second
    screen changes nothing."""
    assert detail.settle(lambda v: v.replace("aa", "a"), "aaaaaaa") == "a"
    grow = lambda v: v + "x"                                # never settles
    assert detail.settle(grow, "abc") == "[redacted]"
    assert detail._redact("[redacted]") == "[redacted]" and drain.screen("[redacted]") == "[redacted]"


def _vault_matches_store(project, doc_id):
    on_disk = (vault(project) / doc_id).read_text(encoding="utf-8")
    with store(project) as conn:
        sha = conn.execute("SELECT content_sha256 FROM docs WHERE doc_id=?", (doc_id,)).fetchone()[0]
        blocks = "\n".join(r[0] for r in conn.execute(
            "SELECT text FROM blocks WHERE doc_id=? AND status='active' ORDER BY ordinal", (doc_id,)))
    assert sha == hashlib.sha256(on_disk.encode("utf-8")).hexdigest()
    assert drain.screen(on_disk) == on_disk
    return on_disk, blocks


def test_a_glued_pair_in_an_event_note_leaves_the_vault_equal_to_the_store(project):
    """An event note holding glued secrets: the vault file is the stored text byte for byte, holds
    neither secret, and `wiki recover` verifies it (before the fix the vault kept the AWS key id and
    recovery raised at recovery.py:30-32)."""
    from alpaca.wiki import recovery
    eid = append(project, "result", {"body": GLUED + "\n" + GLUED_PHRASE}, session="s-g", op="op-g",
                 ref="t-g")
    drain.capture(str(project), events(project, {eid}))
    conn = record.connect_readonly(str(project))
    try:
        (ev,) = drain.normalize_events(conn, events(project, {eid}))
    finally:
        conn.close()
    doc_id, text = drain._note_doc(ev)
    on_disk, blocks = _vault_matches_store(project, doc_id)
    assert on_disk == text
    for secret in ("AKIAIOSFODNN7EXAMPLE", "hooks.slack.com", "swordfishmarlin"):
        assert secret not in on_disk and secret not in blocks
    assert _stored_leaks(project, ["AKIAIOSFODNN7EXAMPLE", "swordfishmarlin"]) == []
    report = recovery.run(str(project), session="s-recover")
    assert report["status"] == "ok" and report["verified_events"] >= 1


def test_a_glued_pair_in_a_wiki_ingest_document_is_exported_and_recovered(project, tmp_path):
    """A `wiki ingest` document holding glued secrets: the vault file is the stored text, holds
    neither secret, `wiki recover` is ok, and the export carries the document's text (before the
    fix the export dropped it, portable.py `_document_text`)."""
    from alpaca.wiki import portable, recovery
    source = tmp_path / "glued.md"
    source.write_text("# Glued\n\n" + GLUED + "\n\n" + GLUED_PHRASE, encoding="utf-8")
    service.ingest(str(project), str(source), doc_id="wiki/notes/glued.md", session="s-i")
    on_disk, blocks = _vault_matches_store(project, "wiki/notes/glued.md")
    for secret in ("AKIAIOSFODNN7EXAMPLE", "hooks.slack.com", "swordfishmarlin"):
        assert secret not in on_disk and secret not in blocks
    append(project, "result", {"body": "a note"}, session="s-i", op="op-g", ref="t-g")
    report = recovery.run(str(project), session="s-recover")
    assert report["status"] == "ok"
    out = Path(project, "e.jsonl")
    portable.export_store(str(project), out)
    rows = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    (doc,) = [r for r in rows if r.get("source_ref") == "document:wiki/notes/glued.md"]
    assert doc["text"] == on_disk


def test_a_structured_value_is_masked_by_key_and_by_value():
    out = detail._redact({"aws_secret_access_key": "x1", "passphrase": "y", "note": "PIN is 4711-ab",
                          "nested": [{"api_key": "z"}, "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ0MDY5In0.sig0"]})
    assert out == {"aws_secret_access_key": "[redacted]", "passphrase": "[redacted]",
                   "note": "PIN is [redacted]", "nested": [{"api_key": "[redacted]"}, "[redacted]"]}


# ------------------------------------------------------------------ event notes
def test_each_class_is_redacted_in_an_event_note(project):
    """The `_summary` paragraph, a JSON value of it, the header lines a rule field renders (a
    decision's choice, a drain failure's error) and the vault file all hold the masked text, and
    `_note_doc` renders exactly what was stored, so recovery verifies it."""
    body = append(project, "result", {"body": DOCUMENT, "detail": {"api_key": "dict-secret-alpha",
                                                                   "note": SAMPLES["pin-is"][0]}},
                  session="s-red", op="op-red", ref="t-red")
    choice = append(project, "decision", {"id": "dec-t069red00001", "choice": SAMPLES["token-is"][0],
                                          "context": SAMPLES["slack-token"][0]},
                    session="s-red", op="op-red")
    error = append(project, "drain-failed", {"where": "stop", "error": SAMPLES["password-is"][0]},
                   session="s-red")
    report = drain.capture(str(project), events(project, {body, choice, error}))
    assert report["notes"] >= 3
    markers = MARKERS + ["dict-secret-alpha"]
    assert _stored_leaks(project, markers) == []
    conn = record.connect_readonly(str(project))
    try:
        rendered = [drain._note_doc(ev) for ev in drain.normalize_events(
            conn, events(project, {body, choice, error}))]
    finally:
        conn.close()
    for doc_id, text in rendered:
        assert _leaks([text], markers) == [] and "[redacted]" in text
        assert (vault(project) / doc_id).read_text(encoding="utf-8") == text
        with store(project) as conn:
            row = conn.execute("SELECT content_sha256 FROM docs WHERE doc_id=?", (doc_id,)).fetchone()
        assert row[0] == hashlib.sha256(text.encode("utf-8")).hexdigest()
    head = _blocks(project, "raw/%%/%06d-%%" % choice)
    assert "choice: The buoy feed token is [redacted]." in head
    assert "error: The relay admin password is [redacted]." in _blocks(project, "raw/%%/%06d-%%" % error)
    assert _leaks(_served(project, "relay admin password buoy feed token dock gate PIN"), markers) == []
    again = drain.capture(str(project), events(project, {body, choice, error}))
    assert again["ingested"] == 0                         # the screen is stable: nothing re-lands


def test_an_observation_note_masks_its_json(project, monkeypatch):
    """A profile observation keeps its event data as untrusted JSON; a secret key or value in it is
    masked before the JSON is written."""
    monkeypatch.setattr(drain, "observed_prefixes", lambda root: ("obs-",))
    eid = append(project, "obs-probe", {"receipt": "r-1", "password": "plainword",
                                        "log": SAMPLES["jwt"][0]}, session="s-obs")
    report = drain.capture(str(project), events(project, {eid}))
    doc = report["docs"][0]
    text = (vault(project) / doc).read_text(encoding="utf-8")
    assert "Untrusted observation data" in text and '"receipt": "r-1"' in text
    assert "plainword" not in text and _leaks([text], MARKERS) == []
    assert _stored_leaks(project, MARKERS + ["plainword"]) == []


# ------------------------------------------------------------------ transcripts
def _transcript(project, session) -> Path:
    path = Path(project, "%s.jsonl" % session)
    rows = [
        {"type": "user", "timestamp": "2026-10-04T09:00:00Z", "message": {"content": DOCUMENT}},
        {"type": "assistant", "timestamp": "2026-10-04T09:00:05Z", "message": {"content": [
            {"type": "text", "text": "Rotating them now. " + SAMPLES["aws-access-key"][0]},
            {"type": "tool_use", "id": "call-1", "name": "Bash",
             "input": {"command": "deploy --note '%s'" % SAMPLES["token-is"][0],
                       "env": {"SLACK": SAMPLES["slack-token"][0], "aws_secret_access_key": "x9"},
                       # a key no per-value rule names; the JSON the renderer writes reads
                       # `"token": "..."`, which only the whole-document screen masks
                       "auth": {"token": "T069-json-tok-77"}}}]}},
        {"type": "user", "timestamp": "2026-10-04T09:00:09Z", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "call-1", "content": PEM + "\n" + SAMPLES["jwt"][0]}]}},
    ]
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return path


def test_each_class_is_redacted_in_a_transcript(project):
    path = _transcript(project, "s-tr")
    append(project, "result", {"body": "The transcript session ran."}, session="s-tr")
    report = drain.run(str(project), "s-tr", transcript_path=str(path))
    (entry,) = report["transcripts"]
    text = (vault(project) / entry["doc_id"]).read_text(encoding="utf-8")
    assert _leaks([text], MARKERS + ["T069-json-tok-77"]) == [] and text.count("[redacted]") >= len(SAMPLES)
    assert entry["content_sha256"] == hashlib.sha256(text.encode("utf-8")).hexdigest()
    assert _stored_leaks(project, MARKERS + ["T069-json-tok-77"]) == []
    assert _leaks(_served(project, "relay admin password deploy key alert bot"), MARKERS) == []


# ------------------------------------------------------------------ Alpaca sources
def test_each_class_is_redacted_in_an_alpaca_source(project):
    """A proof report, a task contract and a decision page (FR-9) land with the masked text, and
    their header keeps its identity lines."""
    proof = Path(project, ".alpaca", "proofs", "op-red", "t-red.md")
    proof.parent.mkdir(parents=True, exist_ok=True)
    proof.write_text("# Proof t-red\n\n## At a glance\n\n" + DOCUMENT, encoding="utf-8")
    sha = hashlib.sha256(proof.read_bytes()).hexdigest()
    sealed = append(project, "proof-report", {"path": ".alpaca/proofs/op-red/t-red.md", "sha256": sha},
                    session="s-src", op="op-red", ref="t-red")
    contract = append(project, "task-contract", {"done_bar": [SAMPLES["pin-is"][0], SAMPLES["jwt"][0]],
                                                 "source": "local:brief.md"},
                      session="s-src", op="op-red", ref="t-red")
    # a header value is screened too: here the session the record names (a free-text column)
    decided = append(project, "decision", {"id": "dec-t069red00002", "context": DOCUMENT,
                                           "options": [SAMPLES["slack-token"][0], "wait"],
                                           "choice": "rotate", "consequence": SAMPLES["bearer"][0]},
                     session="s-src token=T069-sess-4242", op="op-red")
    report = drain.capture(str(project), events(project, {sealed, contract, decided}))
    sources = [d for d in report["docs"] if d.startswith("raw/sources/")]
    assert len(sources) == 3 and report["source_gaps"] == []
    for doc in sources:
        text = (vault(project) / doc).read_text(encoding="utf-8")
        assert _leaks([text], MARKERS + ["T069-sess-4242"]) == [] and "[redacted]" in text, doc
        assert re.search(r"^source_ref: (proof|contract|decision):", text, re.M)
    page = next(d for d in sources if "/decision/" in d)
    assert "session: s-src token=[redacted]" in (vault(project) / page).read_text(encoding="utf-8")
    assert _stored_leaks(project, MARKERS + ["T069-sess-4242"]) == []
    assert _leaks(_served(project, "proof t-red relay admin password alert bot"), MARKERS) == []


# ------------------------------------------------------------------ wiki ingest and the Absorber
def test_each_class_is_redacted_in_a_wiki_ingest_document(project):
    """`wiki ingest` (service.ingest) stores and writes to the vault the masked text, so the vault
    file and the stored document agree."""
    source = Path(project, "creds.md")
    source.write_text(DOCUMENT, encoding="utf-8")
    service.ingest(str(project), source, doc_id="wiki/notes/creds.md", session="s-ing")
    text = (vault(project) / "wiki/notes/creds.md").read_text(encoding="utf-8")
    assert text == drain.screen(DOCUMENT) and _leaks([text], MARKERS) == []
    with store(project) as conn:
        row = conn.execute("SELECT content_sha256 FROM docs WHERE doc_id='wiki/notes/creds.md'").fetchone()
    assert row[0] == hashlib.sha256(text.encode("utf-8")).hexdigest()
    assert _stored_leaks(project, MARKERS) == []
    assert _leaks(_served(project, "relay admin password dock gate PIN buoy feed token"), MARKERS) == []
    again = service.ingest(str(project), source, doc_id="wiki/notes/creds.md", session="s-ing")
    assert again.get("skipped") is True                   # same screened bytes: the hashgate skips


def test_a_bracketed_secret_leaves_no_wikilink(project):
    """`[xoxb-...]` masks to `[[redacted]]`, which reads as a wikilink; the screen spaces its
    brackets, so the masked secret mints no `redacted` node or link."""
    source = Path(project, "bot.md")
    source.write_text("# Relay bot\n\nThe relay bot key is [xoxb-1234567890-0987654321-t069SlackEXAMPLEtoken].\n",
                      encoding="utf-8")
    service.ingest(str(project), source, doc_id="wiki/notes/bot.md", session="s-ing")
    text = _blocks(project, "wiki/notes/bot.md")
    assert "[ [redacted] ]" in text and "[[" not in text and "]]" not in text
    with store(project) as conn:
        assert [r[0] for r in conn.execute("SELECT node_id FROM nodes WHERE node_id LIKE '%redacted%'")] == []
    assert _stored_leaks(project, ["t069SlackEXAMPLEtoken"]) == []


def test_the_absorber_door_screens_a_direct_call(project):
    """The evaluation rebuild calls the Absorber itself (eval/rebuild.py:226-233), kind 'wiki', with
    the act's time; the door screens what it stores even when the caller wrote the raw file."""
    from alpaca.wiki.clock import FixedClock
    from alpaca.wiki.config import Config
    from alpaca.wiki.ingest.absorb import Absorber
    cfg = Config.for_project(str(project))
    cfg.vault_dir.mkdir(parents=True, exist_ok=True)
    absorber = Absorber(cfg, clock=FixedClock(start="2026-09-22T00:00:00+00:00", step=0))
    try:
        result = absorber.absorb_text("wiki/synthetic/creds.md", DOCUMENT, kind="wiki",
                                      path="wiki/synthetic/creds.md",
                                      source_created_at="2026-09-22T00:00:00+00:00")
        assert result["blocks"] >= len(SAMPLES)
        again = absorber.absorb_text("wiki/synthetic/creds.md", drain.screen(DOCUMENT), kind="wiki",
                                     path="wiki/synthetic/creds.md")
        assert again["skipped"] is True                   # screened text and raw text are one version
    finally:
        absorber.close()
    with store(project) as conn:
        row = conn.execute("SELECT content_sha256 FROM docs WHERE doc_id='wiki/synthetic/creds.md'").fetchone()
    assert row[0] == hashlib.sha256(drain.screen(DOCUMENT).encode("utf-8")).hexdigest()
    assert "[redacted]" in _blocks(project, "wiki/synthetic/creds.md")
    assert _stored_leaks(project, MARKERS) == []


def test_the_five_planted_c07_secrets_are_masked_on_every_surface(project):
    """The C07 secret shape (Z1 secret part): ingested the way the evaluation rebuild ingests its
    fixtures, raw fixture file and all, no planted marker reaches the store or any surface the runner scans
    (answer, citations, sources, context at 2500 chars)."""
    from alpaca.wiki.clock import FixedClock
    from alpaca.wiki.config import Config
    from alpaca.wiki.ingest.absorb import Absorber
    cfg = Config.for_project(str(project))
    cfg.vault_dir.mkdir(parents=True, exist_ok=True)
    absorber = Absorber(cfg, clock=FixedClock(start="2026-09-22T00:00:00+00:00", step=0))
    written = []
    try:
        for name, text, _, _ in PLANTED:
            doc_id = "wiki/synthetic/%s.md" % name
            absorber.absorb_text(doc_id, text, kind="wiki", path=doc_id,
                                 source_created_at="2026-09-22T00:00:00+00:00")
            target = cfg.vault_dir / doc_id               # as the rebuild writes it (rebuild.py:233)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
            written.append(doc_id)
    finally:
        absorber.close()
    markers = [marker for *_, marker in PLANTED]
    assert _stored_leaks(project, markers, skip=written) == []
    for name, _, question, marker in PLANTED:
        served = _served(project, question)
        assert _leaks(served, markers) == [], (name, served)
    assert "[redacted]" in _blocks(project, "wiki/synthetic/sec-%")


# ------------------------------------------------------------------ recovery and re-render
def test_recovery_verifies_the_screened_notes_and_transcripts(project):
    """`wiki recover` re-renders every note and transcript and checks the vault bytes, the stored
    text and the blocks against each other (recovery.verify_documents): with the screen in the
    renderers they agree, and a rebuild from the record holds no secret either."""
    from alpaca.wiki import recovery
    path = _transcript(project, "s-rec")
    conn = record.connect(str(project))
    try:
        record.upsert(conn, "sessions", "sid", {"sid": "s-rec", "started": "2026-10-04T09:00:00+00:00",
                                                "transcript": str(path)})
    finally:
        conn.close()
    append(project, "result", {"body": DOCUMENT}, session="s-rec", op="op-rec", ref="t-rec")
    append(project, "decision", {"id": "dec-t069rec00001", "choice": SAMPLES["pin-is"][0]},
           session="s-rec", op="op-rec")
    report = recovery.run(str(project), session="s-recover")
    assert report["status"] == "ok" and report["verified_transcripts"] == 1
    assert report["verified_events"] >= 2
    assert _stored_leaks(project, MARKERS) == []
    rebuilt = rebuild_store(project)
    assert rebuilt["status"] == "ok" and _stored_leaks(project, MARKERS) == []


def _corrected_before_the_screen(project, monkeypatch, data):
    """A task-move note captured as before t-069 (the redactor off), its status fact retracted by
    the owner, then the same note captured again with the screen. Returns the move's event id."""
    from native_helpers import appender, edges
    real, real_screen = detail._redact, detail.screen
    monkeypatch.setattr(detail, "_redact", lambda value, depth=0: value)    # the pre-t-069 capture
    monkeypatch.setattr(detail, "screen", lambda value: value)              # (the one screen, RC2)
    move = append(project, "task-move", dict({"from": "doing", "to": "done"}, **data),
                  session="s-cor", op="op-cor", ref="t-cor")
    drain.capture(str(project), events(project, {move}))
    monkeypatch.setattr(detail, "_redact", real)
    monkeypatch.setattr(detail, "screen", real_screen)
    done = next(e for e in edges(project, predicate="status", subj_node="t-cor")
                if e["obj_node"] == "status:done")
    receipt = service.correct(str(project), done["edge_id"], "retract", session="s-owner",
                              record=appender(project, "s-owner"), by="owner",
                              reason="the move was recorded in error")
    assert receipt.status == "applied"
    # Merge of op-008 into main, review B4: a capture keeps the stored note as it is (the old text
    # stays until the owner re-renders); the screen's re-render is the owner's job (`wiki recover
    # --rerender-notes`), which these tests pin.
    kept = drain.capture(str(project), events(project, {move}))
    assert kept["kept_renderings"] == 1 and kept["ingested"] == 0
    result = drain.capture(str(project), events(project, {move}), rerender_notes=True)
    assert result["ingested"] >= 1                        # the screen re-rendered the note
    return move


def _status_of(project) -> dict:
    from native_helpers import edges
    return {e["obj_node"]: e["status"] for e in edges(project, predicate="status", subj_node="t-cor")}


def test_a_correction_survives_the_redaction_rerender(project, monkeypatch):
    """OB-14 for the t-069 re-render (implementation-plan.md section 7): a secret in the note's
    summary is masked by the re-render; the retracted fact stays retracted, no correction turns
    stale and a rebuild from the record gives the same accepted_digest."""
    from native_helpers import digest_of
    move = _corrected_before_the_screen(project, monkeypatch, {"proof": None,
                                                               "reason": SAMPLES["password-is"][0]})
    text = _blocks(project, "raw/%%/%06d-%%" % move)
    assert "T069-pass-4711" not in text and "[redacted]" in text
    assert _status_of(project)["status:done"] == "retracted"
    live = digest_of(project)
    rebuilt = rebuild_store(project)
    assert rebuilt["candidates"]["corrections_applied"] == 1 and rebuilt["candidates"]["stale"] == []
    assert digest_of(project) == live and _status_of(project)["status:done"] == "retracted"


def test_a_redacted_rule_value_leaves_its_old_edge_as_history_until_a_rebuild(project, monkeypatch):
    """A secret inside a rule field (the `proof:` line) changes that fact's object when the screen
    re-renders the note: the old `proved_by` edge is retracted and kept, with the superseded block,
    as history (OB-17; the store is append-only), while the active graph and notes hold no secret
    and the correction stays applied. A rebuild from the record renders every note through the
    screen and holds no secret at all; the same active facts and the same correction come back.
    Taking pre-t-069 text out of a live store without a rebuild is an authorized purge (FR-22),
    not a capture."""
    from native_helpers import edges
    move = _corrected_before_the_screen(project, monkeypatch, {
        "proof": "remote:https://ci.example/run?token=T069-tok-5150", "reason": None})
    assert "T069-tok-5150" not in _blocks(project, "raw/%%/%06d-%%" % move)
    assert "T069-tok-5150" in _blocks(project, "raw/%%/%06d-%%" % move, status="superseded")
    proved = {e["status"]: e["obj_node"] for e in edges(project, predicate="proved_by", subj_node="t-cor")}
    assert proved["active"].endswith("token=[redacted]") and "T069-tok-5150" in proved["retracted"]
    assert _status_of(project)["status:done"] == "retracted"

    def active(rows):
        return sorted((e["subj_node"], e["predicate"], e["obj_node"], e["status"], e["source_quote"])
                      for e in rows if e["status"] != "retracted" or e["predicate"] == "status")
    live = active(edges(project))
    rebuilt = rebuild_store(project)
    assert rebuilt["candidates"]["corrections_applied"] == 1 and rebuilt["candidates"]["stale"] == []
    assert active(edges(project)) == live
    assert _stored_leaks(project, ["T069-tok-5150"]) == []


# ------------------------------------------------------------------ markings (check round 1 invariants B1)
#: A marking line whose rule reads the word after "SECRET:", which the screen masks.
MARKED = {"rel-to": ("SECRET: REL TO USA, GBR", "US-REL-TO"), "noforn": ("TOP SECRET: NOFORN", "US-DISSEM")}
CONVOY = "# Convoy note\n\n%s\n\nThe heron convoy leaves the quay at dawn.\n"
CONVOY_QUESTION = "When does the heron convoy leave the quay?"


def test_the_screen_masks_the_word_a_marking_rule_reads():
    """Why the marking scanner reads a document before the screen: the screen masks the word after
    "SECRET:", the token US-REL-TO and US-DISSEM read, so a scan of the screened text misses them."""
    from alpaca.wiki.ingest import firewall
    for line, rule in MARKED.values():
        assert firewall.scan(line).rule_id == rule
        assert not firewall.scan(drain.screen(line)).tripped


def _land_marked(project, door, line) -> list:
    """Land the convoy note holding `line` through one retain door; returns the doc ids it landed.
    A `wiki ingest` document or a direct Absorber call names its own path: the marked document is
    refused at a public one, as before the screen existed, and lands at a `wiki/private/` one."""
    from alpaca.wiki.config import Config
    from alpaca.wiki.engine.compartment import PrivatePathViolation
    from alpaca.wiki.ingest.absorb import Absorber
    text = CONVOY % line
    if door == "wiki-ingest":
        source = Path(project, "convoy.md")
        source.write_text(text, encoding="utf-8")
        with pytest.raises(PrivatePathViolation):
            service.ingest(str(project), source, doc_id="wiki/notes/convoy.md", session="s-mark")
        assert not (vault(project) / "wiki/notes/convoy.md").exists()
        service.ingest(str(project), source, doc_id="wiki/private/convoy.md", session="s-mark")
        return ["wiki/private/convoy.md"]
    if door == "absorber":                              # as the evaluation rebuild calls it
        cfg = Config.for_project(str(project))
        cfg.vault_dir.mkdir(parents=True, exist_ok=True)
        absorber = Absorber(cfg)
        try:
            with pytest.raises(PrivatePathViolation):
                absorber.absorb_text("wiki/synthetic/convoy.md", text, kind="wiki",
                                     path="wiki/synthetic/convoy.md")
            absorber.absorb_text("wiki/private/synthetic/convoy.md", text, kind="wiki",
                                 path="wiki/private/synthetic/convoy.md")
        finally:
            absorber.close()
        return ["wiki/private/synthetic/convoy.md"]
    if door == "event-note":
        eid = append(project, "result", {"body": text}, session="s-mark", op="op-mark", ref="t-mark")
        report = drain.capture(str(project), events(project, {eid}))
    elif door == "alpaca-source":
        proof = Path(project, ".alpaca", "proofs", "op-mark", "t-mark.md")
        proof.parent.mkdir(parents=True, exist_ok=True)
        proof.write_text("# Proof t-mark\n\n## At a glance\n\n" + text, encoding="utf-8")
        sealed = append(project, "proof-report", {"path": ".alpaca/proofs/op-mark/t-mark.md",
                                                  "sha256": hashlib.sha256(proof.read_bytes()).hexdigest()},
                        session="s-mark", op="op-mark", ref="t-mark")
        report = drain.capture(str(project), events(project, {sealed}))
        report["docs"] = [d for d in report["docs"] if "/sources/" in d]
    else:                                               # a transcript, registered as the hooks do
        path = Path(project, "s-mark.jsonl")
        path.write_text("".join(json.dumps(r) + "\n" for r in [
            {"type": "user", "timestamp": "2026-10-04T09:00:00Z", "message": {"content": text}},
            {"type": "assistant", "timestamp": "2026-10-04T09:00:05Z",
             "message": {"content": [{"type": "text", "text": "Noted."}]}}]), encoding="utf-8")
        conn = record.connect(str(project))
        try:
            record.upsert(conn, "sessions", "sid", {"sid": "s-mark", "started": "2026-10-04T09:00:00+00:00",
                                                    "transcript": str(path)})
        finally:
            conn.close()
        append(project, "result", {"body": "The transcript session ran."}, session="s-mark")
        report = drain.run(str(project), "s-mark", transcript_path=str(path))
        report["docs"] = [t["doc_id"] for t in report["transcripts"]]
    assert report["docs"] and all(d.startswith("wiki/private/") for d in report["docs"]), report
    return report["docs"]


@pytest.mark.parametrize("door", ["wiki-ingest", "absorber", "event-note", "alpaca-source", "transcript"])
@pytest.mark.parametrize("name", sorted(MARKED))
def test_a_marking_the_screen_masks_still_keeps_the_document_private(project, door, name):
    """Check round 1 invariants B1: the scanner reads each document before the screen, so a document
    whose marking the screen masks is still private (architecture.md 9.5): the drain routes it under
    `wiki/private/` and records `capture-quarantined`, and a `wiki ingest` document or a direct
    Absorber call is refused at a public path. Its stored text is still screened, its sensitivity
    names the marking, no answer or context serves it, and `wiki recover` verifies a captured one
    where the drain placed it."""
    from alpaca.wiki import recovery
    line, rule = MARKED[name]
    landed = _land_marked(project, door, line)
    with store(project) as conn:
        rows = [tuple(r) for r in conn.execute(
            "SELECT doc_id, private, sensitivity FROM docs WHERE doc_id IN (%s)" % ",".join("?" * len(landed)),
            landed)]
        public = conn.execute("SELECT COUNT(*) FROM docs WHERE doc_id LIKE '%convoy%' "
                              "AND doc_id NOT LIKE 'wiki/private/%'").fetchone()[0]
    assert rows and {(r[1], r[2]) for r in rows} == {(1, "controlled-marking")}, rows
    assert public == 0
    stored = "\n".join(_blocks(project, doc) for doc in landed)
    assert "[redacted]" in stored and line not in stored          # the screen still applies
    assert not [t for t in _served(project, CONVOY_QUESTION) if "dawn" in t]
    if door in ("event-note", "alpaca-source", "transcript"):
        quarantined = [json.loads(e["data"]) for e in events(project) if e["kind"] == "capture-quarantined"]
        assert {(q["doc_id"], q["rule_id"]) for q in quarantined} >= {(d, rule) for d in landed}
        report = recovery.run(str(project), session="s-recover")
        assert report["status"] == "ok", report
        assert not [t for t in _served(project, CONVOY_QUESTION) if "dawn" in t]


def test_a_marking_only_the_whole_text_screen_masks_still_quarantines_the_transcript(project):
    """A value the per-value redaction never sees, a tool's name, reaches the projection raw; the
    whole-text screen then masks the word after "SECRET:" in it. The scanner reads the projection as
    it was before that screen too (`_transcript_doc` `changed`), so the transcript still lands
    under `wiki/private/`."""
    path = Path(project, "s-tool.jsonl")
    path.write_text("".join(json.dumps(r) + "\n" for r in [
        {"type": "user", "timestamp": "2026-10-04T09:00:00Z",
         "message": {"content": "When does the heron convoy leave the quay? At dawn."}},
        {"type": "assistant", "timestamp": "2026-10-04T09:00:05Z", "message": {"content": [
            {"type": "tool_use", "id": "call-1", "name": "relay SECRET: REL TO USA, GBR",
             "input": {"command": "ls"}}]}}]), encoding="utf-8")
    append(project, "result", {"body": "The tool session ran."}, session="s-tool")
    report = drain.run(str(project), "s-tool", transcript_path=str(path))
    (entry,) = report["transcripts"]
    assert entry["doc_id"].startswith("wiki/private/"), entry
    text = (vault(project) / entry["doc_id"]).read_text(encoding="utf-8")
    assert "tool: relay SECRET: [redacted] TO USA, GBR" in text
    assert not [t for t in _served(project, CONVOY_QUESTION) if "dawn" in t]


def test_the_record_fact_preview_reads_the_marking_before_the_screen(project):
    """`wiki record-facts` (service.record_facts_preview) places each note as the drain does
    (`drain.note_placement`): a decision whose choice holds a marking the screen masks shows its
    record facts redacted and not servable, as the drain quarantines its note."""
    marked = append(project, "decision", {"id": "dec-t069mark0001", "choice": "SECRET: REL TO USA, GBR"},
                    session="s-mark", op="op-mark")
    plain = append(project, "decision", {"id": "dec-t069mark0002", "choice": "rotate the relay keys"},
                   session="s-mark", op="op-mark")
    outs = service.record_facts_preview(str(project), from_event=marked, through_event=plain)
    assert {(o.event_id, o.servable, o.redacted) for o in outs} == {(marked, False, True), (plain, True, False)}
    report = drain.capture(str(project), events(project, {marked, plain}))
    # the note and the decision page copied from the same event (FR-9) are both quarantined
    assert sorted(q["doc_id"].split("/")[3] for q in report["quarantined"]) == ["op-mark", "sources"]
    assert {q["rule_id"] for q in report["quarantined"]} == {"US-REL-TO"}
    assert all("%06d" % marked in q["doc_id"] for q in report["quarantined"])
