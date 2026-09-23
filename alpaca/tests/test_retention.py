"""M4.7 proof: store retention, compaction, and the mandatory snapshot note.

The store is history the operator is mutating past (doctrine/leaves/operate-in-place.md), not a
clone to promote from. A snapshot is taken before a non-idempotent act and named
`<file>.pre-<change-tag>.<date>` so the resolve pass can find its diff reference. Compaction obeys a
declared policy (a size ceiling and an age ceiling) and never removes a snapshot a live token or an
unresolved collision still references; a zero-retention policy is refused while unresolved
collisions still need their diff reference.

FixedClock stamps the date deterministically; the throwaway `project` root keeps every write inside
one tree. Positive and negative paths are proved for each Done-when clause.
"""
import os

import pytest

from alpaca import cli, db, resolve, retention, token
from alpaca.tests.conftest import project  # noqa: F401  (pytest fixture)

FIXED = "2026-09-17T00:00:00+00:00"
LATER = "2026-12-01T00:00:00+00:00"   # 75 days after FIXED


def _write(root, rel, text):
    p = os.path.join(root, rel)
    os.makedirs(os.path.dirname(p) or root, exist_ok=True)
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(text)
    return p


# --- clause 1: a snapshot without a note is refused --------------------------------------------

def test_snapshot_without_a_note_is_refused(project):
    _write(project, "RESUME.md", "before\n")
    for empty in (None, "", "   "):
        with pytest.raises(retention.RetentionError):
            retention.snapshot(project, "RESUME.md", "M4.7", empty, now=FIXED)
    # nothing was written to the store on the refused path
    assert retention.entries(project) == []


def test_snapshot_with_a_note_is_taken_and_records_the_note(project):
    _write(project, "RESUME.md", "before\n")
    r = retention.snapshot(project, "RESUME.md", "M4.7", "renamed the seam", now=FIXED)
    ents = retention.entries(project)
    assert len(ents) == 1
    assert ents[0]["note"] == "renamed the seam"
    assert os.path.isfile(r["store"])


# --- clause 2: the store is named <file>.pre-<change-tag>.<date> --------------------------------

def test_store_name_carries_file_tag_and_date(project):
    _write(project, "RESUME.md", "before\n")
    r = retention.snapshot(project, "RESUME.md", "M4.7", "note", now=FIXED)
    assert r["name"] == "RESUME.md.pre-M4.7.2026-09-17"
    # the resolve pass can find the diff reference by ref, and its bytes round-trip
    ref = retention.reference(project, "RESUME.md")
    assert ref is not None and ref["name"] == r["name"]
    with open(ref["path"], encoding="utf-8") as fh:
        assert fh.read() == "before\n"


def test_snapshot_is_idempotent_for_one_change_and_date(project):
    _write(project, "RESUME.md", "before\n")
    a = retention.snapshot(project, "RESUME.md", "M4.7", "n1", now=FIXED)
    # a second change writes on top; the pre-image already captured is not clobbered
    _write(project, "RESUME.md", "after\n")
    b = retention.snapshot(project, "RESUME.md", "M4.7", "n2", now=FIXED)
    assert a["name"] == b["name"]
    with open(b["store"], encoding="utf-8") as fh:
        assert fh.read() == "before\n"   # the earliest pre-image survives
    assert len(retention.entries(project)) == 1


# --- clause 3: compaction never removes a referenced snapshot -----------------------------------

def test_compaction_keeps_a_snapshot_a_live_token_references(project):
    _write(project, "RESUME.md", "held\n")
    _write(project, "scratch.md", "free\n")
    retention.snapshot(project, "RESUME.md", "M4.7", "under a live token", now=FIXED)
    retention.snapshot(project, "scratch.md", "M4.7", "no holder", now=FIXED)
    conn = db.connect(project)
    token.issue(conn, "s1", "RESUME.md")            # a live (unmatched) token on RESUME.md
    # zero retention is allowed here (no unresolved collisions) and would drop everything, yet the
    # referenced snapshot must survive while the unreferenced one is compacted away.
    res = retention.compact(project, {"max_bytes": 0}, conn=conn, now=FIXED)
    names = {e["name"] for e in retention.entries(project)}
    assert "RESUME.md.pre-M4.7.2026-09-17" in names
    assert "scratch.md.pre-M4.7.2026-09-17" not in names
    assert "scratch.md.pre-M4.7.2026-09-17" in res["removed"]


def test_compaction_keeps_a_snapshot_an_unresolved_collision_references(project):
    _write(project, "RESUME.md", "held\n")
    _write(project, "scratch.md", "free\n")
    retention.snapshot(project, "RESUME.md", "M4.7", "under a collision", now=FIXED)
    retention.snapshot(project, "scratch.md", "M4.7", "no holder", now=FIXED)
    conn = db.connect(project)
    resolve.log(conn, resolve.CONCURRENT, "RESUME.md",
                {"writers": [{"worker": "a", "value": 1}, {"worker": "b", "value": 2}]}, now=FIXED)
    assert resolve.unresolved_count(conn) == 1
    # a tiny (non-zero) size ceiling forces compaction of the unreferenced snapshot; the one an
    # unresolved collision still needs as its diff reference is kept regardless of the ceiling.
    res = retention.compact(project, {"max_bytes": 1, "max_age_days": 365}, conn=conn, now=FIXED)
    names = {e["name"] for e in retention.entries(project)}
    assert "RESUME.md.pre-M4.7.2026-09-17" in names
    assert "scratch.md.pre-M4.7.2026-09-17" not in names
    assert "RESUME.md.pre-M4.7.2026-09-17" in res["referenced"]


def test_compaction_drops_an_unreferenced_snapshot_past_the_age_ceiling(project):
    _write(project, "old.md", "stale\n")
    retention.snapshot(project, "old.md", "M4.7", "aged out", now=FIXED)
    conn = db.connect(project)
    # 75 days later against a 30-day ceiling: no holder, so it is compacted.
    res = retention.compact(project, {"max_age_days": 30}, conn=conn, now=LATER)
    assert "old.md.pre-M4.7.2026-09-17" in res["removed"]
    assert retention.entries(project) == []


def test_compaction_keeps_a_young_unreferenced_snapshot(project):
    _write(project, "fresh.md", "recent\n")
    retention.snapshot(project, "fresh.md", "M4.7", "still young", now=FIXED)
    conn = db.connect(project)
    res = retention.compact(project, {"max_age_days": 30}, conn=conn, now=FIXED)
    assert res["removed"] == []
    assert len(retention.entries(project)) == 1


# --- clause 4: a zero-retention policy is refused while unresolved collisions exist -------------

def test_zero_retention_is_refused_while_unresolved_collisions_exist(project):
    _write(project, "RESUME.md", "held\n")
    retention.snapshot(project, "RESUME.md", "M4.7", "note", now=FIXED)
    conn = db.connect(project)
    resolve.log(conn, resolve.CONCURRENT, "RESUME.md",
                {"writers": [{"worker": "a", "value": 1}, {"worker": "b", "value": 2}]}, now=FIXED)
    for zero in ({"max_bytes": 0}, {"max_age_days": 0}):
        with pytest.raises(retention.RetentionError):
            retention.compact(project, zero, conn=conn, now=FIXED)
    # refused: the store is untouched
    assert len(retention.entries(project)) == 1


def test_zero_retention_is_allowed_when_no_collision_is_unresolved(project):
    _write(project, "scratch.md", "free\n")
    retention.snapshot(project, "scratch.md", "M4.7", "note", now=FIXED)
    conn = db.connect(project)
    res = retention.compact(project, {"max_bytes": 0}, conn=conn, now=FIXED)
    assert "scratch.md.pre-M4.7.2026-09-17" in res["removed"]
    assert retention.entries(project) == []


# --- the store size the doctor reports ---------------------------------------------------------

def test_store_size_grows_with_snapshots_and_shrinks_on_compaction(project):
    assert retention.store_size(project) == 0
    _write(project, "a.md", "aaaa\n")
    retention.snapshot(project, "a.md", "M4.7", "note", now=FIXED)
    grown = retention.store_size(project)
    assert grown > 0
    conn = db.connect(project)
    retention.compact(project, {"max_bytes": 0}, conn=conn, now=FIXED)
    assert retention.store_size(project) < grown


# --- the policy lives in project.yaml (data, not code) -----------------------------------------

def test_policy_is_read_from_project_yaml(project):
    # the onboarded config carries the retention policy the repo ships; load it from a real root
    from alpaca import project as proj
    proj.save(project, {"name": "demo",
                        "retention": {"store": ".alpaca/store", "max_bytes": 4096, "max_age_days": 7}})
    pol = retention.load_policy(project)
    assert pol["max_bytes"] == 4096 and pol["max_age_days"] == 7


# --- the ops.py snapshot helper and the CLI verb ------------------------------------------------

def test_ops_snapshot_helper_refuses_without_a_note(project):
    from alpaca import ops
    _write(project, "RESUME.md", "before\n")
    with pytest.raises(retention.RetentionError):
        ops.snapshot_out(project, "RESUME.md", "M4.7", "", now=FIXED)
    r = ops.snapshot_out(project, "RESUME.md", "M4.7", "helper note", now=FIXED)
    assert r["name"] == "RESUME.md.pre-M4.7.2026-09-17"


def test_cli_retention_snapshot_and_compact(project, capsys):
    _write(project, "RESUME.md", "before\n")
    assert cli.main(["retention", "snapshot", "RESUME.md", "--tag", "M4.7",
                     "--note", "a real note"]) == cli.PASS
    assert len(retention.entries(project)) == 1
    capsys.readouterr()
    # a snapshot with no --note is refused at the CLI boundary (argparse requires it)
    rc = cli.main(["retention", "snapshot", "RESUME.md", "--tag", "M4.7"])
    assert rc == cli.USAGE
    assert cli.main(["retention", "compact"]) == cli.PASS


def test_cli_retention_compact_blocks_on_zero_retention_with_collisions(project, capsys):
    from alpaca import project as proj
    proj.save(project, {"name": "demo", "retention": {"max_bytes": 0}})
    _write(project, "RESUME.md", "before\n")
    cli.main(["retention", "snapshot", "RESUME.md", "--tag", "M4.7", "--note", "n"])
    conn = db.connect(project)
    resolve.log(conn, resolve.CONCURRENT, "RESUME.md",
                {"writers": [{"worker": "a", "value": 1}, {"worker": "b", "value": 2}]})
    capsys.readouterr()
    assert cli.main(["retention", "compact"]) == cli.BLOCKED
