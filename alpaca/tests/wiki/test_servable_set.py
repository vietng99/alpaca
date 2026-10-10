"""op-008 t-062: the servable set (FR-2, Z1; design/rune2-memory/architecture.md 6.2; OB-07).

One predicate, alpaca/wiki/servable.py, decides what may be served. A private block is never served;
a source any capture of which was quarantined is not servable even when an older version was public
(OB-07); the door-internal reads (successor walk, conflict note, ambiguity gate) and extract.emit
keep only servable blocks. `Answer(` is built only in engine/loop.py. Real absorber, real door, no
mock.
"""
import ast
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from native_helpers import append, capture, ingest, set_config, store  # noqa: E402

from alpaca.wiki import servable as sv                  # noqa: E402

FACT = "The heron gateway retries uploads three times before failing."


def _absorb(project, docs, *, start="2019-01-01T00:00:00+00:00"):
    """Absorb (doc_id, text, kind, path[, domain]) rows through the real write door with a fixed
    clock. The domain defaults to `general`."""
    from alpaca.wiki.clock import FixedClock
    from alpaca.wiki.config import Config
    from alpaca.wiki.ingest.absorb import Absorber
    cfg = Config.for_project(project)
    cfg.vault_dir.mkdir(parents=True, exist_ok=True)
    absorber = Absorber(cfg, clock=FixedClock(start=start, step=0))
    try:
        for doc_id, text, kind, path, *domain in docs:
            absorber.absorb_text(doc_id, text, kind=kind, path=path,
                                 domain=domain[0] if domain else "general")
    finally:
        absorber.close()


def _quarantined_copy(project, event_id: int, text: str):
    """A later capture of the same event source that landed private under wiki/private/ (the
    quarantine route of architecture.md 9.5): same header identity, private document."""
    header = "op: op-901\nrole: result\nkey: t-901\nkind: result\nevent: %d\nts: x\n\n" % event_id
    path = "wiki/private/raw/op-901/%06d-result-quarantined.md" % event_id
    _absorb(project, [(path, header + text + "\n", "raw", path)])


# ------------------------------------------------------------------ OB-07
def test_mixed_version_quarantine_is_not_servable(project):
    from alpaca.wiki import service
    eid = append(project, "result", {"body": FACT}, op="op-901", ref="t-901")
    capture(project)
    with store(project) as conn:
        assert sv.servable_ref(conn, "event:%d" % eid) is True
    before = service.query(project, "heron gateway retries uploads")
    assert before["outcome"] == "grounded" and FACT in before["answer_text"]
    _quarantined_copy(project, eid, "The heron gateway secret token is hunter2.")
    with store(project) as conn:
        assert sv.servable_ref(conn, "event:%d" % eid) is False
        assert "event:%d" % eid in sv.unservable_source_refs(conn)
    after = service.query(project, "heron gateway retries uploads")
    assert (after["outcome"], after["reason"]) == ("refused", "not-servable")
    dumped = json.dumps(after)
    assert FACT not in dumped and "hunter2" not in dumped
    # a K-pinned read before version 2 serves no version 1 text either: since t-065 reads at K
    # are served, so this holds through the servable filter at K, not through a refusal.
    pinned = service.query(project, "heron gateway retries uploads",
                           knowledge_as_of="2000-01-01T00:00:00+00:00")
    assert pinned["answer_text"] is None and FACT not in json.dumps(pinned)
    # a K after both captures sees version 1's block as a candidate; only the servable filter keeps
    # it out (a source any capture of which was quarantined is not servable, OB-07)
    late = service.query(project, "heron gateway retries uploads",
                         knowledge_as_of="2100-01-01T00:00:00+00:00")
    assert (late["outcome"], late["reason"]) == ("refused", "not-servable")
    assert FACT not in json.dumps(late) and "hunter2" not in json.dumps(late)
    text = service.context(project, "heron gateway retries uploads")
    assert FACT not in text and "hunter2" not in text
    # a partial or reformatted leak of either version: the context does not echo the question
    # (service.py context), so no word of the version 1 text or the quarantined copy may appear
    for word in ("heron", "gateway", "retries", "three times", "secret token"):
        assert word not in text.lower(), word


def test_private_source_version_row_makes_source_unservable(project):
    eid = append(project, "result", {"body": FACT}, op="op-901", ref="t-901")
    capture(project)
    ref = "event:%d" % eid
    with store(project) as conn:
        conn.execute("INSERT INTO source_versions(source_ref,version_seq,doc_id,content_sha256,"
                     "recorded_at,private,status) VALUES(?,?,?,?,?,?,?)",
                     (ref, 2, "wiki/private/x.md", "0" * 64, "2026-01-01", 1, "current"))
    with store(project) as conn:
        assert sv.servable_ref(conn, ref) is False


def test_excluded_source_is_not_servable(project):
    from alpaca.wiki import service
    eid = append(project, "result", {"body": FACT}, op="op-901", ref="t-901")
    capture(project)
    with store(project) as conn:
        conn.execute("INSERT INTO exclusions(source_ref,reason,requested_by,record_event_id,"
                     "recorded_at) VALUES(?,?,?,?,?)",
                     ("event:%d" % eid, "test", "owner", 99, "2026-01-01"))
    with store(project) as conn:
        assert sv.servable_ref(conn, "event:%d" % eid) is False
    result = service.query(project, "heron gateway retries uploads")
    assert result["outcome"] == "refused" and FACT not in json.dumps(result)


def test_private_block_is_never_served(project):
    from alpaca.wiki import service
    ingest(project, "The heron gateway uses a public relay for uploads.", "wiki/heron.md", "a.md")
    ingest(project, "The heron gateway private relay key is kestrel-77.", "wiki/private/heron.md",
           "b.md")
    for question in ("heron gateway relay", "heron gateway private relay key kestrel-77"):
        result = service.query(project, question)
        served = {k: v for k, v in result.items() if k != "question"}
        assert "kestrel" not in json.dumps(served)
        assert all(not s["doc_id"].startswith("wiki/private/") for s in result["sources"])
        assert "kestrel" not in service.context(project, question)
    with store(project) as conn:
        blocks = [r[0] for r in conn.execute("SELECT block_id FROM blocks WHERE status='active'")]
        kept = sv.servable(conn, blocks, compartment=None)
    assert kept and all(not b.startswith("wiki/private/") for b in kept)


def test_answer_is_built_only_in_the_loop():
    root = Path(__file__).resolve().parents[2] / "wiki"
    builders = []
    for py in sorted(root.rglob("*.py")):
        for node in ast.walk(ast.parse(py.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "Answer":
                builders.append(py.relative_to(root).as_posix())
    assert builders == ["engine/loop.py"]


def test_servable_imports_no_ranking_module():
    from alpaca.wiki.guards import GUARDED_MODULES, _read_violations
    path = Path(sv.__file__)
    tree = ast.parse(path.read_text(encoding="utf-8"))
    assert _read_violations(tree, "", GUARDED_MODULES) == []


# ------------------------------------------------------------------ door-internal reads
def _exclude(project, source_ref: str):
    """An exclusions row for one source, as test_excluded_source_is_not_servable writes it."""
    with store(project) as conn:
        conn.execute("INSERT INTO exclusions(source_ref,reason,requested_by,record_event_id,"
                     "recorded_at) VALUES(?,?,?,?,?)",
                     (source_ref, "test", "owner", 99, "2026-01-01"))


# How the second source is made non-servable: a private document under wiki/private/, a public
# document with an exclusions row, or (successor walk only) a public document in a domain outside
# the question's compartment ("works" puts the question in `career`; the block is `personal`).
HIDDEN = ("private", "excluded")


def _hidden_doc(kind: str, name: str) -> tuple[str, str]:
    """(doc_id, domain) of the second source for one way of hiding it."""
    if kind == "private":
        return "wiki/private/%s.md" % name, "general"
    if kind == "excluded":
        return "wiki/%s-2.md" % name, "general"
    return "wiki/%s-2.md" % name, "personal"


@pytest.mark.parametrize("kind", HIDDEN + ("other-domain",))
def test_non_servable_successor_is_not_followed(project, kind):
    """A corrected edge whose active successor sits in a private, excluded or other-compartment
    block: the successor walk does not follow it, so its text never reaches the answer or `extra`,
    and the superseded block it falls back to is never served as a current answer
    (architecture.md 6.1 rows 5-7: historical when nothing cited is current)."""
    from alpaca.wiki import service
    set_config(project, "wiki_providers:\n  meta:\n    retrieval_profile: full\n")
    doc, domain = _hidden_doc(kind, "ada")
    _absorb(project, [("wiki/ada.md", "[[Ada]] works at [[Acme]]\n", "wiki", "wiki/ada.md"),
                      (doc, "[[Ada]] works at [[Globex]]\n", "wiki", doc, domain)])
    if kind == "excluded":
        _exclude(project, "document:" + doc)
    with store(project) as conn:
        prior = conn.execute("SELECT superseded_by_edge_id FROM edges WHERE obj_node='acme'"
                             ).fetchone()[0]
        assert prior, "setup: the second source must correct the first edge"
        anchor = conn.execute("SELECT source_doc_id FROM edges WHERE edge_id=?", (prior,)
                              ).fetchone()[0]
        assert anchor == doc, "setup: the successor edge is anchored in the hidden source"
    result = service.query(project, "Ada works at")
    dumped = json.dumps(result).lower()
    assert "globex" not in dumped
    assert result["outcome"] != "grounded"
    if result["outcome"] in ("historical", "conflict"):  # text outcomes left once grounded is out
        assert (result["outcome"], result["reason"]) == ("historical", "not-current")
    assert "globex" not in service.context(project, "Ada works at").lower()


@pytest.mark.parametrize("kind", HIDDEN)
def test_non_servable_overturner_never_names_the_conflict_winner(project, kind):
    """A live contradicts edge anchored in a private or excluded block never makes a conflict, never
    names the conflict_note winner and lends no text to `extra`."""
    from alpaca.wiki import service
    set_config(project, "wiki_providers:\n  meta:\n    retrieval_profile: full\n")
    doc, _ = _hidden_doc(kind, "bob")
    _absorb(project, [("wiki/ada.md", "[[Ada]] works at [[Acme]]\n", "wiki", "wiki/ada.md"),
                      ("wiki/bob.md", "[[Bob]] works at [[Hooli]]\n", "wiki", "wiki/bob.md"),
                      (doc, "[[Bob]] works at [[Acme]]\n", "wiki", doc)])
    if kind == "excluded":
        _exclude(project, "document:" + doc)
    with store(project) as conn:
        live = conn.execute("SELECT COUNT(*) FROM edges WHERE reconcile_verdict='contradicts' "
                            "AND status='active' AND source_doc_id=?", (doc,)).fetchone()[0]
        assert live == 1, "setup: a live hidden contradicts edge on node acme"
    # an as-of-currency question runs the OVERTURNER_QUERY template (engine/plan.py classify)
    result = service.query(project, "Where does Ada currently work")
    assert result["verdict"] != "conflict" and result["outcome"] != "conflict"
    assert "bob" not in (result.get("extra", {}).get("conflict_note") or "").lower()
    served = {k: v for k, v in result.items() if k != "question"}
    dumped = json.dumps(served).lower()
    assert doc not in dumped and "bob works_at acme" not in dumped


@pytest.mark.parametrize("kind", HIDDEN)
def test_non_servable_overturner_never_wins_conflict_arbitration(project, kind, monkeypatch):
    """The conflict-arbitration read (engine/oracle.py LAYER 5, `disagreeing = ...`): when the oracle
    reaches a conflict, a live contradicts edge anchored in a private or excluded block is not
    ranked, so it never names the conflict_note winner and lends no text to the result.

    Fault injection, declared: no captured source drives the structural entailer to `contradicted`
    on this setup, so every deterministic entailer is patched to call each claim contradicted. That
    enters the arbitration branch without the OVERTURNER_QUERY template ("Ada works at" is not an
    as-of-currency question), so this test isolates the arbitration read from the template read that
    test_non_servable_overturner_never_names_the_conflict_winner covers."""
    from alpaca.wiki import service
    from alpaca.wiki.providers import deterministic
    set_config(project, "wiki_providers:\n  entailer: structural\n  meta:\n"
                        "    retrieval_profile: full\n")
    doc, _ = _hidden_doc(kind, "bob")
    _absorb(project, [("wiki/ada.md", "[[Ada]] works at [[Acme]]\n", "wiki", "wiki/ada.md"),
                      ("wiki/bob.md", "[[Bob]] works at [[Hooli]]\n", "wiki", "wiki/bob.md"),
                      (doc, "[[Bob]] works at [[Acme]]\n", "wiki", doc)])
    if kind == "excluded":
        _exclude(project, "document:" + doc)
    with store(project) as conn:
        live = conn.execute("SELECT COUNT(*) FROM edges WHERE reconcile_verdict='contradicts' "
                            "AND status='active' AND source_doc_id=?", (doc,)).fetchone()[0]
        assert live == 1, "setup: a live hidden contradicts edge on node acme"
    for name in dir(deterministic):                                     # fault injection only
        cls = getattr(deterministic, name)
        if isinstance(cls, type) and hasattr(cls, "entail"):
            monkeypatch.setattr(cls, "entail", lambda self, claim, source: ("contradicted", 0.9))
    from alpaca.wiki.config import Config
    from alpaca.wiki.engine.answer import answer as door
    # The oracle returns conflict and its note reaches the door's `extra`; the loop's sentence-level
    # entailment door (engine/loop.py, oracle-safety.3) then drops every sentence under the same
    # fault, so the served verdict is abstained. The note is what the arbitration read decides, so
    # it is read from the door itself: service.query withholds the note under an outcome that
    # withholds the answer text.
    note = (door(Config.for_project(project), "Ada works at").extra.get("conflict_note")
            or "").lower()
    assert note.startswith("sources disagree; per authority"), (
        "setup: the door must enter conflict arbitration and rank a winner", note)
    assert "bob" not in note and "ada works_at acme" in note
    result = service.query(project, "Ada works at")
    assert result["outcome"] not in ("grounded", "historical", "conflict")
    assert result["extra"]["conflict_note"] is None
    served = {k: v for k, v in result.items() if k != "question"}
    dumped = json.dumps(served).lower()
    assert doc not in dumped and "bob works_at acme" not in dumped and "[[bob]]" not in dumped


def test_ambiguity_gate_counts_no_private_block(project):
    from alpaca.wiki import service
    set_config(project, "wiki_providers:\n  meta:\n    retrieval_profile: full\n")
    _absorb(project, [
        ("wiki/dat-a.md", "[[Dat Nguyen|Dat]] works at [[Acme]]\n", "wiki", "wiki/dat-a.md"),
        ("wiki/private/dat-b.md", "[[Dat Tran|Dat]] works at [[Hooli]]\n", "wiki",
         "wiki/private/dat-b.md")])
    with store(project) as conn:
        nodes = {r[0] for r in conn.execute(
            "SELECT node_id FROM aliases WHERE norm_surface='dat' AND status='bound'")}
    assert nodes == {"dat-nguyen", "dat-tran"}, nodes
    result = service.query(project, "Where does Dat work")
    assert "ambiguous_entities" not in (result.get("extra") or {})
    assert "dat-tran" not in json.dumps(result)


# ------------------------------------------------------------------ extract.emit
def test_extract_leaves_out_excluded_doc_and_its_edges(project, tmp_path):
    from alpaca.wiki import extract
    _absorb(project, [("wiki/ada.md", "[[Ada]] works at [[Acme]]\n", "wiki", "wiki/ada.md"),
                      ("wiki/bob.md", "[[Bob]] works at [[Hooli]]\n", "wiki", "wiki/bob.md")])
    with store(project) as conn:
        conn.execute("INSERT INTO exclusions(source_ref,reason,requested_by,record_event_id,"
                     "recorded_at) VALUES(?,?,?,?,?)",
                     ("document:wiki/bob.md", "test", "owner", 1, "2026-01-01"))
    vault = Path(project, ".alpaca", "wiki")
    manifest = extract.emit(str(vault), str(tmp_path / "out"))
    assert manifest["schema_version"] == "4"
    import sqlite3
    out = sqlite3.connect(str(tmp_path / "out" / "rune.db"))
    try:
        docs = {r[0] for r in out.execute("SELECT doc_id FROM docs")}
        edges = {(r[0], r[1]) for r in out.execute("SELECT subj_node, obj_node FROM edges")}
    finally:
        out.close()
    assert "wiki/bob.md" not in docs and "wiki/ada.md" in docs
    assert ("bob", "hooli") not in edges and ("ada", "acme") in edges



def test_extract_leaves_out_an_edge_anchored_in_an_excluded_block(project, tmp_path):
    """An edge whose doc is public but whose anchor block lies in an excluded source (the two
    columns disagree after a re-anchor) stays behind: the edge filter reads the anchor block."""
    from alpaca.wiki import extract
    _absorb(project, [("wiki/ada.md", "[[Ada]] works at [[Acme]]\n", "wiki", "wiki/ada.md"),
                      ("wiki/bob.md", "[[Bob]] works at [[Hooli]]\n", "wiki", "wiki/bob.md")])
    with store(project) as conn:
        bob_block = conn.execute("SELECT block_id FROM blocks WHERE doc_id='wiki/bob.md' "
                                 "AND status='active' ORDER BY ordinal LIMIT 1").fetchone()[0]
        conn.execute("UPDATE edges SET source_block_id=? WHERE subj_node='ada'", (bob_block,))
        conn.execute("INSERT INTO exclusions(source_ref,reason,requested_by,record_event_id,"
                     "recorded_at) VALUES(?,?,?,?,?)",
                     ("document:wiki/bob.md", "test", "owner", 1, "2026-01-01"))
    manifest = extract.emit(str(Path(project, ".alpaca", "wiki")), str(tmp_path / "out"))
    import sqlite3
    out = sqlite3.connect(str(tmp_path / "out" / "rune.db"))
    try:
        edges = {(r[0], r[1]) for r in out.execute("SELECT subj_node, obj_node FROM edges")}
    finally:
        out.close()
    assert ("ada", "acme") not in edges
    assert manifest["counts"]["edges"] == 0
