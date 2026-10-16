"""attach study, fix round 3: `alpaca attach status` shows the stale areas of the map the wiki serves.

The wiki serves the map that the last complete ingest stored (`study/served.json`: map path, sha and the area
paths of that one parse). The flag of that map is recomputed from those stored paths against the newest recorded
base at every prepare, flag_stale and ingest, and `health.status` trusts it only when it names that map at that
sha for that base; anything else reads as every served area stale. Definition: "alpaca attach sync flags the map
as stale when upstream changes touch the areas it describes" (design/attach-mode/definition.md:155-156).

Cases s1 to s9 come from t-652.logs/think/premortem-r1-attacks/test_pm_stale.py (s8b is the failed-ingest
rollback); c4 from think/approach-1-stale-probe.py; the rest are added in fix round 3.
"""
import json

import pytest

from alpaca.attach import AttachError, health, study
from alpaca.tests.attachkit import attached, git, studio, upstream  # noqa: F401
from alpaca.tests.test_attach_study import AREAS, MAP, map_text, prepared, upstream_edit, write_map  # noqa: F401

OTHER = "design/attach/draft.md"
STALE = ".alpaca/attach/study/stale.json"


def shown(root):
    return health.status(str(root))["stale_areas"]


def _active(root):
    from alpaca.wiki.config import Config
    from alpaca.wiki.ingest.absorb import Absorber
    ab = Absorber(Config.for_project(str(root)))
    try:
        return sorted(r[0] for r in ab.db.conn.execute(
            "SELECT DISTINCT doc_id FROM blocks WHERE status='active' AND doc_id LIKE 'wiki/attach/map/%'"))
    finally:
        ab.close()


def _move_upstream_and_put_default_map_at_new_base(att, root):
    """Upstream changes src/; the default map is rewritten at the new base and prepare records it, so the
    default map's own flag (the top-level `areas`) is empty and cannot hide a miss on the served map."""
    b = upstream_edit(att.upstream, writes={"src/extra.py": "x = 1\n"})
    write_map(att, sha=b)
    study.prepare(root)
    assert study._latest_base(root) == b
    assert study.read_stale(root, MAP)["areas"] == []
    return b


def test_stale_s1_other_map_ingested_at_an_older_base(prepared):
    """C1, P3-01 (probe_adversarial_r3.py:54-76): the served map is at A, the newest base is B."""
    att, info = prepared
    root = str(att.root)
    a = info["sha"]
    (att.root / OTHER).write_text(map_text(a), encoding="utf-8")
    _move_upstream_and_put_default_map_at_new_base(att, root)
    study.ingest(root, OTHER)
    assert "src" in shown(root), "the wiki serves %s at %s; status shows %s" % (OTHER, a[:12], shown(root))


@pytest.mark.parametrize("then", ["prepare", "prepare-and-flag-stale"])
def test_stale_s2_served_map_current_at_ingest_goes_stale_at_the_next_prepare(prepared, then):
    """C2: the served map was at the newest base when ingested; upstream moves; prepare (and flag_stale)
    run on the default map only (study.py:1475-1476, :1188-1189), so nothing re-flags the served map."""
    att, info = prepared
    root = str(att.root)
    a = info["sha"]
    (att.root / OTHER).write_text(map_text(a), encoding="utf-8")
    study.ingest(root, OTHER)
    assert shown(root) == []
    _move_upstream_and_put_default_map_at_new_base(att, root)
    if then == "prepare-and-flag-stale":
        study.flag_stale(root)
    assert "src" in shown(root), "the wiki serves %s at %s; status shows %s" % (OTHER, a[:12], shown(root))


def test_stale_s3_default_map_ingested_at_an_older_base_with_no_earlier_flag(prepared):
    """The default map flag is [] at B (map at B), then the map is rewritten at A and ingested: ingest writes
    None for a map not at the newest base (study.py:1588-1592), so the [] for B stays."""
    att, info = prepared
    root = str(att.root)
    a = info["sha"]
    _move_upstream_and_put_default_map_at_new_base(att, root)
    write_map(att, sha=a)
    study.ingest(root)
    assert "src" in shown(root), "the wiki serves %s at %s; status shows %s" % (MAP, a[:12], shown(root))


def test_stale_s4_the_flag_is_computed_from_the_served_sha_not_an_older_one(prepared):
    """C3: the flag entry kept from map sha X (since X, to B) is diff(X, B); the served map is at Y, and a
    file changed X->Y and back Y->B is in diff(Y, B) only."""
    att, info = prepared
    root = str(att.root)
    x = info["sha"]
    original = (att.upstream.work / "src" / "main.py").read_text(encoding="utf-8")
    write_map(att, sha=x)
    study.ingest(root)
    y = upstream_edit(att.upstream, writes={"src/main.py": "print('changed')\n"})
    study.prepare(root)
    b = upstream_edit(att.upstream, writes={"src/main.py": original})
    study.prepare(root)
    assert study._latest_base(root) == b and study.stale_areas(root, x, b) == []
    write_map(att, sha=y)
    assert study.stale_areas(root, y, b) == ["src"]
    study.ingest(root)
    assert "src" in shown(root), "the wiki serves the map at %s (src changed by %s); status shows %s" \
        % (y[:12], b[:12], shown(root))


@pytest.mark.parametrize("damage", ["deleted", "front-matter-broken"])
def test_stale_s5_served_map_file_gone_does_not_break_prepare_and_reads_as_stale(prepared, damage):
    """Approach 0 recomputes the served flag with stale_areas(..., served_map), which reads the map file
    (`_map_front` raises AttachError when it is missing or unusable, study.py:1030-1034). The served map is a
    draft an agent may delete or rewrite. prepare and flag_stale must still work, and the served areas must
    read as stale (unknown is stale, the fail-safe of study.py:1090-1093)."""
    att, info = prepared
    root = str(att.root)
    a = info["sha"]
    (att.root / OTHER).write_text(map_text(a), encoding="utf-8")
    study.ingest(root, OTHER)
    if damage == "deleted":
        (att.root / OTHER).unlink()
    else:
        (att.root / OTHER).write_text("no front matter here\n", encoding="utf-8")
    b = upstream_edit(att.upstream, writes={"src/extra.py": "x = 1\n"})
    write_map(att, sha=b)
    try:
        study.prepare(root)
        study.flag_stale(root)
    except AttachError as exc:
        pytest.fail("prepare or flag_stale on the default map failed because the served map changed: %s" % exc)
    assert "src" in shown(root), "the wiki serves %s at %s; status shows %s" % (OTHER, a[:12], shown(root))


def test_stale_s6_served_flag_uses_the_area_paths_that_were_ingested(prepared):
    """The served map is edited after ingest (its src area now says lib/) and not ingested again. The wiki
    still serves the claims about src/; a recompute that re-reads the edited file uses lib/ and misses."""
    att, info = prepared
    root = str(att.root)
    a = info["sha"]
    (att.root / OTHER).write_text(map_text(a), encoding="utf-8")
    study.ingest(root, OTHER)
    moved = [("src", ["lib/"])] + [x for x in AREAS if x[0] != "src"]
    (att.root / OTHER).write_text(map_text(a, areas=moved), encoding="utf-8")
    _move_upstream_and_put_default_map_at_new_base(att, root)
    assert "src" in shown(root), "the wiki serves src claims of %s at %s; status shows %s" % (OTHER, a[:12], shown(root))


@pytest.mark.parametrize("doc", [
    {"areas": "src"},
    {"areas": [1, None]},
    {"areas": [], "served_areas": "src"},
    {"areas": [], "served_areas": [{"x": 1}]},
], ids=["areas-string", "areas-not-strings", "served-string", "served-not-strings"])
def test_stale_s7_status_reads_only_area_ids_from_a_hand_edited_flag(prepared, doc):
    """stale.json is a plain file an agent can edit. health.status does list(value) (health.py:173): a string
    becomes its characters, and `", ".join` in the status verb (health.py:199) raises on a non-string."""
    att, info = prepared
    root = str(att.root)
    write_map(att, sha=info["sha"])
    (att.root / STALE).write_text(json.dumps(doc), encoding="utf-8")
    got = shown(root)
    assert isinstance(got, list) and all(isinstance(x, str) and x for x in got), got
    assert got != ["s", "r", "c"], got
    ", ".join(got)


def _failed_second_area_ingest(att, monkeypatch):
    root = str(att.root)
    a = study._latest_base(root)
    write_map(att, sha=a)
    study.ingest(root)
    before = (att.root / STALE).read_bytes()
    body = ("## Area: src\n- The program prints hello, again. [ptr: app@src/main.py:1]\n"
            "## Area: docs\n- The changelog starts with first, again. [ptr: app\x40CHANGELOG.md:1]\n"
            "## Area: build\n- The test target echoes test. [ptr: app@Makefile:4-5]\n")
    (att.root / OTHER).write_text(map_text(a, body=body), encoding="utf-8")
    from alpaca.wiki import service
    real, calls = service.ingest, {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise ValueError("store is busy")
        return real(*args, **kwargs)

    monkeypatch.setattr(service, "ingest", flaky)
    with pytest.raises(AttachError):
        study.ingest(root, OTHER)
    monkeypatch.setattr(service, "ingest", real)
    return before


def test_stale_s8a_a_failed_ingest_leaves_the_flag_file_as_it_was(prepared, monkeypatch):
    """The served record and the flag change only when the whole map went in (both are written after the loop)."""
    att, _ = prepared
    served = att.root / ".alpaca/attach/study/served.json"
    stale_before = _failed_second_area_ingest(att, monkeypatch)
    assert (att.root / STALE).read_bytes() == stale_before, "a failed ingest changed the stale flag file"
    assert json.loads(served.read_text(encoding="utf-8"))["map"] == MAP, "a failed ingest changed served.json"


def test_stale_s8b_a_failed_ingest_leaves_one_version_per_area(prepared, monkeypatch):
    """Pre-existing, not approach 0's surface (study.py:1559-1583): documents are stored one by one and the
    replaced ones are retired only after the loop, so a wiki failure on the second area leaves the first area
    with two active versions, against the ingest docstring ("the wiki never serves two versions of an area",
    study.py:1550-1551). Post-freeze class: a follow-up unless the pick takes it in."""
    att, _ = prepared
    _failed_second_area_ingest(att, monkeypatch)
    per_area = {}
    for d in _active(att.root):
        per_area.setdefault(d.split("/")[3], []).append(d)
    assert all(len(v) == 1 for v in per_area.values()), "two versions served after a failed ingest: %s" % per_area


def test_stale_s9_a_writer_that_follows_the_plan_text_cannot_hide_the_served_map(prepared):
    """build-plan.md:394 names t-654 sync as the writer of study/stale.json with the shape
    {"areas","since_sha","at"}, and :884-885 has sync write study.stale_areas(old, new) there itself. A sync built
    from that text replaces the file: the per-map entries and any served-map keys are gone. Status must then
    not report nothing while the wiki serves a map that is stale at the newest base."""
    att, info = prepared
    root = str(att.root)
    a = info["sha"]
    (att.root / OTHER).write_text(map_text(a), encoding="utf-8")
    study.ingest(root, OTHER)
    b = _move_upstream_and_put_default_map_at_new_base(att, root)
    (att.root / STALE).write_text(json.dumps({"areas": [], "since_sha": b, "at": "2026-10-15T00:00:00Z"}),
                                  encoding="utf-8")
    assert "src" in shown(root), "after a plan-shaped rewrite of stale.json status shows %s" % shown(root)


def test_stale_c4_a_base_recorded_without_a_refresh_reads_as_every_served_area_stale(prepared):
    """A later writer records a new base without refreshing the served flag. The flag was computed for another
    base, so status must not trust it: every served area reads as stale until the flag is refreshed."""
    att, info = prepared
    root, a = str(att.root), info["sha"]
    write_map(att, sha=a)
    study.ingest(root)
    assert shown(root) == []
    b = upstream_edit(att.upstream, writes={"docs-only.txt": "x\n"})
    study._event(root, "probe", {"action": "prepare", "sha": b, "files": 0, "bytes": 0, "map": {}})
    assert study._latest_base(root) == b
    assert set(shown(root)) == {"src", "docs", "build"}, "status trusts a flag computed for another base: %s" % shown(root)
    study.prepare(root)                          # fetches b and refreshes the served flag for it
    assert shown(root) == [], "after prepare the served flag is for the newest base: %s" % shown(root)


@pytest.mark.parametrize("served", [
    "src",
    ["src"],
    {"map": MAP, "sha": "x", "areas": "src"},
    {"map": MAP, "sha": "x", "areas": [["src"], 3, None, ["", []], [["a"], []]]},
    {"map": MAP, "sha": "x", "areas": [["src", "lib/"]]},
], ids=["string", "list", "areas-string", "pairs-wrong-shape", "paths-string"])
def test_stale_malformed_served_file_never_shows_characters_or_raises(prepared, served):
    """served.json is a plain file an agent can edit. Status shows only non-empty string ids, never the
    characters of a string, and prepare and flag_stale on the default map still work."""
    att, info = prepared
    root = str(att.root)
    write_map(att, sha=info["sha"])
    (att.root / ".alpaca/attach/study/served.json").write_text(json.dumps(served), encoding="utf-8")
    got = shown(root)
    assert isinstance(got, list) and all(isinstance(x, str) and x for x in got), got
    assert got not in (["s", "r", "c"],), got
    ", ".join(got)
    b = upstream_edit(att.upstream, writes={"src/extra.py": "x = 1\n"})
    write_map(att, sha=b)
    study.prepare(root)
    study.flag_stale(root)


def test_stale_flag_follows_a_later_prepare_of_the_same_served_map(prepared):
    """The served map is current when ingested, goes stale when upstream touches its area, and is clean again
    after it is re-ingested at the new base."""
    att, info = prepared
    root, a = str(att.root), info["sha"]
    write_map(att, sha=a)
    study.ingest(root)
    assert shown(root) == []
    b = upstream_edit(att.upstream, writes={"src/extra.py": "x = 1\n"})
    study.prepare(root)
    assert shown(root) == ["src"]
    write_map(att, sha=b)
    study.ingest(root)
    assert shown(root) == []


def test_stale_status_reads_the_record_without_writing_it(prepared):
    """`attach status` opens the record read-only: the bytes of the record file do not change."""
    att, info = prepared
    root = str(att.root)
    write_map(att, sha=info["sha"])
    study.ingest(root)
    db_files = [p for p in (att.root / ".alpaca").glob("alpaca.db*") if p.name == "alpaca.db"]
    before = [(p, p.read_bytes()) for p in db_files]
    assert before, "no record file found"
    shown(root)
    assert [(p, p.read_bytes()) for p in db_files] == before


def test_stale_a_retry_after_a_failed_ingest_serves_every_area(prepared, monkeypatch):
    """The rollback retires the documents the failed run stored; the same ingest run again (the wiki works)
    must serve one active version of each area, not skip the texts the wiki saw before."""
    att, _ = prepared
    root = str(att.root)
    _failed_second_area_ingest(att, monkeypatch)
    ids = study.ingest(root, OTHER)
    per_area = {}
    for d in _active(root):
        per_area.setdefault(d.split("/")[3], []).append(d)
    assert sorted(per_area) == ["build", "docs", "src"], per_area
    assert all(len(v) == 1 for v in per_area.values()), per_area
    assert sorted(set(ids)) == sorted(sum(per_area.values(), []))


def test_stale_the_failed_ingest_refusal_says_the_documents_were_retired(prepared, monkeypatch):
    att, _ = prepared
    root = str(att.root)
    a = study._latest_base(root)
    write_map(att, sha=a)
    study.ingest(root)
    body = ("## Area: src\n- The program prints hello, again. [ptr: app@src/main.py:1]\n"
            "## Area: docs\n- The changelog starts with first, again. [ptr: app\x40CHANGELOG.md:1]\n"
            "## Area: build\n- The test target echoes test. [ptr: app@Makefile:4-5]\n")
    (att.root / OTHER).write_text(map_text(a, body=body), encoding="utf-8")
    from alpaca.wiki import service
    real, calls = service.ingest, {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise ValueError("store is busy")
        return real(*args, **kwargs)

    monkeypatch.setattr(service, "ingest", flaky)
    with pytest.raises(AttachError) as err:
        study.ingest(root, OTHER)
    assert "store is busy" in str(err.value) and "retired again" in str(err.value), str(err.value)
