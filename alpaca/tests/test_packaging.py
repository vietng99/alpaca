"""M4.15 proof (packaging) - the four ported packaging tools, Alpaca-manifest driven.

Each tool is ported UNCHANGED IN PURPOSE from an earlier harness with paths read from `ALPACA-MANIFEST`:

  * gen_manifest.py     - the per-file digest map over the manifest MECHANISM class; it verifies on
                          a fresh clone, a memory-class change is not drift, a mechanism change is.
  * package_candidate.py- the deterministic archive (path-sorted, normalised tar, gzip mtime 0) that
                          rebuilds byte-for-byte equal, and the transactional cutover that rolls
                          back cleanly.
  * shipment_policy.py  - a write-free path policy that refuses the WHOLE staged population, before
                          any mutation, when a forbidden (memory-class / host-local) path is staged.
  * cutover.sh          - the policy-bound dry-run/apply/rollback wrapper (usage routes to band 2).

Every apply / rollback runs against a DISPOSABLE tree in a pytest tmp dir; nothing here touches the
live record or the live repo.
"""
import importlib.util
import os
import shutil
import subprocess
import sys

import pytest

from alpaca.tests.conftest import REPO

SETUP = os.path.join(REPO, "setup")


def _yaml_name(root):
    try:
        with open(os.path.join(root, "project.yaml"), encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("name:"):
                    return line.split(":", 1)[1].strip().strip("'").strip('"')
    except OSError:
        pass
    return os.path.basename(root)


# The distribution identity, read from the shipped project.yaml at runtime (rename-safe: no
# folder-name literal in the test). package_candidate.EXPECTED_PRODUCT reads the same file, so a
# synthetic archive built with this name is accepted as this distribution.
REPO_NAME = _yaml_name(REPO)


def _load(basename, modname):
    spec = importlib.util.spec_from_file_location(modname, os.path.join(SETUP, basename))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


PC = _load("package_candidate.py", "alpaca_package_candidate")
SHP = _load("shipment_policy.py", "alpaca_shipment_policy_test")
GEN_MANIFEST = os.path.join(SETUP, "gen_manifest.py")


def _w(root, rel, body, mode=0o644):
    p = os.path.join(root, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(body)
    os.chmod(p, mode)


def _mksource(root):
    """A minimal, self-consistent mechanism tree: a manifest naming alpaca/ + setup/, the two packaging
    helpers the verifier subprocesses, and a memory-class residue that must never be measured."""
    _w(root, "ALPACA-MANIFEST", "[mechanism]\nalpaca/\nsetup/\nCLAUDE.md\n[memory]\n.alpaca/\nRESUME.md\n")
    _w(root, "CLAUDE.md", "# boot\n")
    # project.yaml is read at runtime for the product name; it is not in the mechanism list, so it is
    # not measured or shipped and does not change the archive member set.
    _w(root, "project.yaml", "name: %s\n" % REPO_NAME)
    _w(root, "alpaca/__init__.py", "")
    _w(root, "alpaca/mod.py", "value = 1\n")
    os.makedirs(os.path.join(root, "setup"), exist_ok=True)
    shutil.copy(GEN_MANIFEST, os.path.join(root, "setup", "gen_manifest.py"))
    shutil.copy(os.path.join(SETUP, "shipment_policy.py"),
                os.path.join(root, "setup", "shipment_policy.py"))
    _w(root, ".alpaca/alpaca.db", "runtime-state\n")          # memory: never shipped, never measured
    _w(root, "RESUME.md", "a stale projection\n")     # memory: never shipped, never measured


def _gen(root, *args):
    return subprocess.run([sys.executable, GEN_MANIFEST, *args, "--root", root],
                          capture_output=True, text=True, encoding="utf-8")


# --------------------------------------------------------------- the per-file digest map on a clone
def test_manifest_verifies_on_a_fresh_clone(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    _mksource(str(src))
    assert _gen(str(src), "--write").returncode == 0
    # a fresh clone (copytree of every tracked byte) verifies clean.
    clone = tmp_path / "clone"
    shutil.copytree(src, clone)
    res = _gen(str(clone), "--verify")
    assert res.returncode == 0, res.stdout + res.stderr
    assert "OK -- tree matches manifest" in res.stdout


def test_a_memory_class_change_is_not_drift(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    _mksource(str(src))
    assert _gen(str(src), "--write").returncode == 0
    _w(str(src), ".alpaca/alpaca.db", "runtime state mutated\n")     # memory class
    _w(str(src), "RESUME.md", "a different stale projection\n")
    res = _gen(str(src), "--verify")
    assert res.returncode == 0, "a memory-class change must not be scored as shipment drift"


def test_a_mechanism_class_change_is_drift(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    _mksource(str(src))
    assert _gen(str(src), "--write").returncode == 0
    _w(str(src), "alpaca/mod.py", "value = 999\n")               # mechanism class
    res = _gen(str(src), "--verify")
    assert res.returncode == 1, "a mechanism-class byte change is drift (FAIL)"
    assert "~ changed" in res.stdout


# ------------------------------------------------------------ the deterministic archive rebuild
def test_archive_rebuilds_deterministically_equal(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    _mksource(str(src))
    assert _gen(str(src), "--write").returncode == 0
    a1 = tmp_path / "one" / "cand.tar.gz"
    a2 = tmp_path / "two" / "cand.tar.gz"
    PC.build_archive(str(src), str(a1), str(a1) + ".sha256")
    PC.build_archive(str(src), str(a2), str(a2) + ".sha256")
    assert a1.read_bytes() == a2.read_bytes(), "two builds of the same tree are byte-for-byte equal"
    # and the archive verifies against its own re-derived tree_digest and pinned product.
    manifest, _raw, content = PC.verified_archive(str(a1))
    assert manifest["product"] == REPO_NAME
    assert "MANIFEST.json" in content and "alpaca/mod.py" in content


def test_archive_excludes_the_memory_class(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    _mksource(str(src))
    assert _gen(str(src), "--write").returncode == 0
    a = tmp_path / "cand.tar.gz"
    PC.build_archive(str(src), str(a), str(a) + ".sha256")
    _m, _raw, content = PC.verified_archive(str(a))
    assert ".alpaca/alpaca.db" not in content and "RESUME.md" not in content, \
        "the memory class never enters the shipment archive"


# ---------------------------------------------------------------- the cutover rolls back cleanly
def test_dry_run_cutover_rolls_back_cleanly(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    _mksource(str(src))
    assert _gen(str(src), "--write").returncode == 0
    archive = tmp_path / "cand.tar.gz"
    PC.build_archive(str(src), str(archive), str(archive) + ".sha256")

    # a self-consistent live tree that differs from the archive in one audited mechanism file.
    live = tmp_path / "live"
    shutil.copytree(src, live)
    _w(str(live), "alpaca/mod.py", "value = 42\n")
    assert _gen(str(live), "--write").returncode == 0     # live is now its own audited baseline
    before = (live / "alpaca" / "mod.py").read_text(encoding="utf-8")

    backup_root = tmp_path / "backups"
    PC.apply_cutover(str(archive), str(live), str(backup_root))
    assert (live / "alpaca" / "mod.py").read_text(encoding="utf-8") == "value = 1\n", \
        "apply installed the archived bytes"
    PC.rollback_cutover(str(live), str(backup_root))
    assert (live / "alpaca" / "mod.py").read_text(encoding="utf-8") == before, \
        "rollback restored the pre-apply bytes exactly"
    # the restored tree matches its own manifest.
    assert _gen(str(live), "--verify").returncode == 0


def test_rollback_over_an_absent_backup_blocks(tmp_path):
    live = tmp_path / "live"
    live.mkdir()
    _mksource(str(live))
    with pytest.raises(PC.BlockError):
        PC.rollback_cutover(str(live), str(tmp_path / "no-such-backup"))


# --------------------------------------------------- a forbidden shipment path refuses the population
def test_forbidden_shipment_path_refuses_before_any_mutation():
    mutated = {"ran": False}

    def mutation():
        mutated["ran"] = True
        return "did-mutate"

    with pytest.raises(SHP.ShipmentPolicyError) as ei:
        SHP.prewrite_shipment_apply([".claude/settings.local.json", "alpaca/mod.py"], mutation)
    assert mutated["ran"] is False, "the mutation callback is unreachable on a forbidden population"
    assert ".claude/settings.local.json" in ei.value.departed_paths


def test_memory_class_path_from_the_manifest_is_forbidden(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    _mksource(str(root))
    forbidden = SHP.forbidden_for_root(str(root))
    assert ".alpaca" in forbidden and "RESUME.md" in forbidden
    mutated = {"ran": False}

    def mutation():
        mutated["ran"] = True
        return "x"

    # a member nested UNDER a forbidden memory directory refuses the whole population.
    with pytest.raises(SHP.ShipmentPolicyError) as ei:
        SHP.prewrite_shipment_apply(["alpaca/mod.py", ".alpaca/alpaca.db"], mutation, forbidden=forbidden)
    assert mutated["ran"] is False
    assert ".alpaca/alpaca.db" in ei.value.departed_paths


def test_a_clean_population_reaches_the_mutation():
    ran = {"ok": False}

    def mutation():
        ran["ok"] = True
        return "reached"

    result, validated = SHP.prewrite_shipment_apply(["alpaca/mod.py", "MANIFEST.json"], mutation)
    assert ran["ok"] and result == "reached"
    assert "alpaca/mod.py" in validated


# --------------------------------------------------------------------- the tools are sound + ASCII
def test_gen_manifest_selftest_passes():
    res = subprocess.run([sys.executable, GEN_MANIFEST, "--selftest"],
                         capture_output=True, text=True, encoding="utf-8")
    assert res.returncode == 0, res.stdout + res.stderr
    assert "GATE gen-manifest-selftest: PASS" in res.stdout


def test_package_candidate_help_routes_to_the_harness_band(tmp_path):
    # --help is a non-adjudication event: it terminates in the reserved harness band (64), never 0.
    res = subprocess.run([sys.executable, os.path.join(SETUP, "package_candidate.py"), "--help"],
                         capture_output=True, text=True, encoding="utf-8")
    assert res.returncode == 64
    out = res.stdout + res.stderr
    for sub in ("verify-source", "verify-archive", "build", "extract", "changes", "apply",
                "rollback"):
        assert sub in out


def test_verify_archive_on_a_missing_archive_is_clean(tmp_path):
    res = subprocess.run([sys.executable, os.path.join(SETUP, "package_candidate.py"),
                          "verify-archive", "--archive", str(tmp_path / "absent.tar.gz")],
                         capture_output=True, text=True, encoding="utf-8")
    combined = res.stdout + res.stderr
    assert res.returncode == 1 and "PACKAGE ERROR" in combined and "Traceback" not in combined


def test_cutover_sh_bad_arg_prints_usage(tmp_path):
    live = tmp_path / "live"
    live.mkdir()
    res = subprocess.run(["bash", os.path.join(SETUP, "cutover.sh"), "--not-a-mode"],
                         capture_output=True, text=True, encoding="utf-8",
                         env={**os.environ, "ALPACA_LIVE_ROOT": str(live),
                              "ALPACA_ARCHIVE": str(live / "none.tar.gz"),
                              "ALPACA_BACKUP_ROOT": str(live / "backups")})
    assert res.returncode == 2 and "usage" in (res.stdout + res.stderr).lower()


@pytest.mark.parametrize("name", ["gen_manifest.py", "package_candidate.py",
                                  "shipment_policy.py", "cutover.sh", "boot-check.py"])
def test_setup_tool_is_pure_ascii(name):
    raw = open(os.path.join(SETUP, name), "rb").read()
    over = [b for b in raw if b > 0x7F]
    assert over == [], "%s must be pure ASCII (no byte above 0x7F)" % name
    # em dash is U+2014, a multi-byte glyph; the pure-ASCII assertion above already forbids it.
    assert 0x2014 not in [ord(c) for c in raw.decode("ascii")], "no em dash anywhere"
