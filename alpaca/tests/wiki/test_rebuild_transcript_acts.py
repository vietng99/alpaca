"""op-008 t-070 stage s3-e2e: a record act that names a transcript meets, in a rebuild or an import,
the store it met live (design/rune2-memory/architecture.md 10.2, 10.4; FR-22, FR-23).

Found by the end-to-end scenario (.alpaca/proofs/op-008/t-070.logs/e2e/): a Claude session transcript
was withdrawn live after it landed, and the export, an import of it into a fresh root and a second
export were not byte-identical. The import refused the withdrawn transcript's source row with
`not-in-record`. Cause: the replay applies the candidate and source acts in record order, landing
every event before an act first (`recovery.replay_candidates`), but it lands transcripts only after
all the acts, in the session loop; so the withdrawal met no transcript, and the transcript, excluded
by then, never landed. A live withdrawal of a transcript the store does not hold is refused
`unknown-source` (`service.withdraw_source`), so live the transcript always landed first. (A
transcript is never a candidate's anchor, K-5, so the withdrawal is the one act that needs it.)

Real record, real drain, real store, real doors: nothing is mocked.
"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from native_helpers import (T10, T11, appender, at, digest_of, rebuild_store,  # noqa: E402
                            reset_vault, store)

from alpaca.wiki import portable, service                      # noqa: E402

TALK = "Task t-9 is blocked on the osprey relay patch."


def _transcript(project, sid, text):
    """A Claude transcript under .alpaca/transcripts/, registered for `sid` as the hooks register one."""
    from alpaca import observability
    path = Path(project, ".alpaca", "transcripts", sid + ".jsonl")
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [{"type": "user", "message": {"content": "What happened to the osprey relay?"}},
             {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}]
    path.write_text("".join(json.dumps(r) + "\n" for r in lines), encoding="utf-8")
    observability.expect_source(project, sid, "claude", locator=str(path))
    return path


def _talk(project, sid="s-talk"):
    """A session with one event and its transcript, both captured, as a SessionEnd leaves them."""
    _transcript(project, sid, TALK)
    at(project, "result", {"body": "A note in the talk session."}, T10, session=sid)
    service.capture(project, sessions=[sid], session_events=True, deadline_s=None)


def _export(project, name) -> Path:
    path = Path(project, name)
    portable.export_store(project, path)
    return path


def _sources(path) -> list:
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines()
            if json.loads(line).get("kind") == "source"]


def _rows_after_manifest(path) -> list:
    return Path(path).read_text(encoding="utf-8").splitlines()[1:]


def _withdraw(project, ref):
    return service.withdraw_source(project, ref, session="s-owner",
                                   record=appender(project, "s-owner"), reason="wrong session")


def test_a_withdrawn_transcript_round_trips_byte_identical(project):
    """A transcript withdrawn after it landed: its source row (withdrawn, no text) is in the export;
    an import into an empty store refuses nothing and the second export has the same bytes; a
    rebuild by `wiki recover` gives the same rows (its manifest names a later cutoff, the event the
    verb appends) and the same accepted digest."""
    _talk(project)
    at(project, "result", {"body": "Another session's note."}, T11, session="s-other")
    service.capture(project, sessions=["s-other"], session_events=True, deadline_s=None)
    assert _withdraw(project, "transcript:s-talk").status == "applied"
    first = _export(project, "first.jsonl")
    talk = [r for r in _sources(first) if r["source_ref"] == "transcript:s-talk"]
    assert [r["status"] for r in talk] == ["withdrawn"]
    assert TALK not in first.read_text(encoding="utf-8")
    digest = digest_of(project)

    reset_vault(project)
    report = portable.import_store(project, first)
    assert (report["status"], report["refused"], report["unrestorable"]) == ("ok", [], {}), report
    assert _export(project, "second.jsonl").read_bytes() == first.read_bytes()
    assert digest_of(project) == digest

    rebuild_store(project)                     # `wiki recover` appends its own event: a later cutoff
    third = _export(project, "third.jsonl")
    assert _rows_after_manifest(third) == _rows_after_manifest(first)
    assert digest_of(project) == digest
    with store(project) as conn:
        assert not conn.execute(
            "SELECT 1 FROM blocks b JOIN source_versions v ON v.doc_id=b.doc_id "
            "WHERE v.source_ref='transcript:s-talk' AND b.status='active'").fetchone()
    assert TALK not in json.dumps(service.query(project, "What is task t-9 blocked on?"))

