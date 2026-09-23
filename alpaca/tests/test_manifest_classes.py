"""M4.1 Step 2/3/4: the manifest states BOTH classes and the rule they exist for.

mechanism = committed with the project; memory = runtime state, gitignored. P-002 takes
`design/`, `docs/` and the top-level `tests/` OUT of the mechanism class (self-tests now ride
under the harness package at `alpaca/tests/`, which is already covered by the `alpaca/` mechanism path
and so cannot collide with a product's own `tests/`). P-001 keeps `project.yaml` in the
mechanism class. `.gitignore` is fed from the memory class so the two never drift.
"""
import os
import shutil

from alpaca import manifest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))


def _manifest_text():
    with open(os.path.join(REPO, manifest.MANIFEST), encoding="utf-8") as fh:
        return fh.read()


def test_both_classes_are_named_and_non_empty():
    cls = manifest.classes(REPO)
    assert set(cls) == {"mechanism", "memory"}
    assert cls["mechanism"], "the mechanism class must not be empty"
    assert cls["memory"], "the memory class must not be empty"


def test_manifest_states_both_classes_and_the_rule():
    # the header must name each class, say what it is (committed vs runtime/gitignored) and carry
    # the contamination rule the memory class exists for.
    txt = _manifest_text().lower()
    assert "[mechanism]" in _manifest_text() and "[memory]" in _manifest_text()
    assert "committed" in txt
    assert "gitignored" in txt or "runtime state" in txt
    # the contamination rule: the memory class must never travel to another project.
    assert "never travel" in txt or "never travels" in txt


def test_project_yaml_stays_mechanism_p001():
    assert "project.yaml" in manifest.classes(REPO)["mechanism"]


def test_pytest_ini_stays_mechanism():
    assert "pytest.ini" in manifest.classes(REPO)["mechanism"]


def test_design_and_docs_leave_the_mechanism_class_p002():
    mech = manifest.classes(REPO)["mechanism"]
    assert "design/" not in mech and "design" not in mech
    assert "docs/" not in mech and "docs" not in mech


def test_top_level_tests_is_not_a_mechanism_path_p002():
    # the whole point of P-002: the self-test suite ships under a path that cannot collide with a
    # product's own tests/. So `tests/` is NOT a top-level mechanism entry ...
    mech = manifest.classes(REPO)["mechanism"]
    assert "tests/" not in mech and "tests" not in mech
    # ... and the self-tests ride under the harness package, covered by the `alpaca/` mechanism path.
    assert "alpaca/" in mech
    assert os.path.isdir(os.path.join(REPO, "alpaca", "tests"))


def test_memory_class_is_the_runtime_state():
    mem = manifest.classes(REPO)["memory"]
    for rel in (".alpaca/", "RESUME.md", "analytics/"):
        assert rel in mem, rel


def test_gitignore_is_fed_from_the_memory_class():
    # every memory path is an entry in .gitignore, and the helper derives exactly from the class.
    fed = manifest.gitignore_from_memory(REPO)
    assert set(fed) == set(manifest.classes(REPO)["memory"])
    with open(os.path.join(REPO, ".gitignore"), encoding="utf-8") as fh:
        lines = {ln.strip().lstrip('/') for ln in fh}
    for entry in fed:
        assert entry in lines, "%s must be in .gitignore" % entry


def test_generated_analytics_is_ignored_but_source_is_trackable(tmp_path):
    import shutil
    import subprocess
    shutil.copy(os.path.join(REPO, '.gitignore'), tmp_path / '.gitignore')
    subprocess.run(['git', 'init', '-q', str(tmp_path)], check=True)
    for name in ('analytics/index.html', 'alpaca/analytics/build_index.py'):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('fixture')
    assert subprocess.run(['git', 'check-ignore', '-q', 'analytics/index.html'], cwd=tmp_path).returncode == 0
    assert subprocess.run(['git', 'check-ignore', '-q', 'alpaca/analytics/build_index.py'], cwd=tmp_path).returncode == 1


def _root_with_memory(tmp_path, memory_lines):
    root = tmp_path / "proj"
    root.mkdir()
    (root / manifest.MANIFEST).write_text(
        "[mechanism]\npkg/\n[memory]\n" + "".join(line + "\n" for line in memory_lines),
        encoding="utf-8")
    return root


def test_memory_ignore_drops_nested_memory_paths_anchored_at_the_root(tmp_path):
    # a memory path nested under a mechanism dir stays behind; the same basename elsewhere travels.
    root = _root_with_memory(tmp_path, [".state/", "pkg/cache/", "pkg/conf/local.json"])
    for rel in (".state/db", "pkg/cache/big.bin", "pkg/conf/local.json", "pkg/conf/shared.json",
                "pkg/sub/cache/keep.txt", "pkg/mod.py"):
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x", encoding="utf-8")
    dst = tmp_path / "copy"
    shutil.copytree(root, dst, ignore=manifest.memory_ignore(root))
    kept = sorted(p.relative_to(dst).as_posix() for p in dst.rglob("*") if p.is_file())
    assert kept == sorted([manifest.MANIFEST, "pkg/conf/shared.json", "pkg/mod.py",
                           "pkg/sub/cache/keep.txt"])


def test_memory_ignore_serves_a_walk_that_starts_below_the_root(tmp_path):
    root = _root_with_memory(tmp_path, ["pkg/cache/"])
    ignore = manifest.memory_ignore(root)
    assert ignore(str(root / "pkg"), ["cache", "mod.py"]) == ["cache"]
    assert ignore(str(root), ["pkg"]) == []


def test_every_nested_memory_path_of_this_repo_is_dropped_by_memory_ignore():
    # each nested memory entry must be answered at its own parent dir, whatever the entry is.
    ignore = manifest.memory_ignore(REPO)
    for entry in manifest.memory(REPO):
        parent, name = os.path.split(entry.strip("/"))
        assert ignore(os.path.join(REPO, parent), [name, "unrelated"]) == [name], entry
