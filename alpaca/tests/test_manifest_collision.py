"""M4.1 Step 1 (the proof): the collision protocol.

Copying the harness into an existing repo must surface every colliding MECHANISM path to the
owner instead of writing over it: one collision report, zero writes. The memory class never
travels to another project (it is this project's own runtime state), so it is never a collision
and never a copy source. The boot block merges by markers rather than clobbering the target's own
`CLAUDE.md`.
"""
import hashlib
import os

from alpaca import manifest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))


def _snapshot(root):
    """A content snapshot of a tree: {relpath: sha256}. Used to prove zero writes."""
    snap = {}
    for dp, dn, fn in os.walk(root):
        dn.sort()
        for f in sorted(fn):
            p = os.path.join(dp, f)
            with open(p, "rb") as fh:
                snap[os.path.relpath(p, root)] = hashlib.sha256(fh.read()).hexdigest()
    return snap


def _make_existing_repo(tmp_path):
    """A product repo that already carries its own tests/, design/, docs/ AND, by coincidence,
    one path the harness also owns as mechanism (CLAUDE.md)."""
    root = tmp_path / "product"
    for d in ("tests", "design", "docs", "src"):
        (root / d).mkdir(parents=True)
    (root / "tests" / "test_app.py").write_text("def test_app():\n    assert True\n", encoding="utf-8")
    (root / "design" / "notes.md").write_text("product design\n", encoding="utf-8")
    (root / "docs" / "readme.md").write_text("product docs\n", encoding="utf-8")
    (root / "src" / "app.py").write_text("x = 1\n", encoding="utf-8")
    (root / "CLAUDE.md").write_text("# Product boot\n\nour own house rules.\n", encoding="utf-8")
    return root


def test_existing_repo_surfaces_colliding_mechanism_and_writes_nothing(tmp_path):
    target = _make_existing_repo(tmp_path)
    before = _snapshot(str(target))

    report = manifest.collisions(REPO, str(target))

    # the target's own CLAUDE.md is a harness mechanism path -> it is surfaced, not clobbered.
    assert "CLAUDE.md" in report
    # zero writes: the collision pass only reads.
    assert _snapshot(str(target)) == before


def test_product_tests_design_docs_do_not_collide_p002(tmp_path):
    # because P-002 took them out of the mechanism class, a product's own tests/design/docs are
    # not reported as collisions at all.
    target = _make_existing_repo(tmp_path)
    report = manifest.collisions(REPO, str(target))
    for rel in ("tests/", "tests", "design/", "design", "docs/", "docs"):
        assert rel not in report, rel


def test_the_memory_class_never_travels(tmp_path):
    # even if the target already holds a .alpaca/ and a RESUME.md, the memory class is never surfaced
    # as a collision and never proposed for copy: it is this project's runtime state alone.
    target = _make_existing_repo(tmp_path)
    (target / ".alpaca").mkdir()
    (target / ".alpaca" / "alpaca.db").write_text("stale\n", encoding="utf-8")
    (target / "RESUME.md").write_text("stale resume\n", encoding="utf-8")
    (target / "analytics").mkdir()

    report = manifest.collisions(REPO, str(target))
    for rel in manifest.classes(REPO)["memory"]:
        assert rel not in report, rel
    # and the report only ever names mechanism paths.
    mech = set(manifest.classes(REPO)["mechanism"])
    assert set(report) <= mech


def test_boot_block_merges_by_markers_not_clobber():
    with open(os.path.join(REPO, "CLAUDE.md"), encoding="utf-8") as fh:
        harness = fh.read()

    # a target with its own content and NO harness markers: the block is appended, content kept.
    plain = "# Product boot\n\nour own house rules must survive.\n"
    merged = manifest.merge_boot_block(plain, harness)
    assert "our own house rules must survive." in merged
    assert manifest.BOOT_BEGIN in merged and manifest.BOOT_END in merged
    assert merged.count(manifest.BOOT_BEGIN) == 1

    # a second merge is idempotent: it replaces the marked region in place, never stacks a copy.
    merged2 = manifest.merge_boot_block(merged, harness)
    assert merged2.count(manifest.BOOT_BEGIN) == 1
    assert "our own house rules must survive." in merged2

    # updating the harness block replaces only the region between the markers.
    updated_harness = harness.replace("# Alpaca boot", "# Alpaca boot (v2)")
    merged3 = manifest.merge_boot_block(merged, updated_harness)
    assert "# Alpaca boot (v2)" in merged3
    assert "our own house rules must survive." in merged3
    assert merged3.count(manifest.BOOT_BEGIN) == 1
