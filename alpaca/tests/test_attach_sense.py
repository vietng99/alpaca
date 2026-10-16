"""Gate sensing from the files of the attached repository (build plan P10, QTRM64 probe section 4)."""
import json

import pytest

from alpaca.attach import AttachError, repo, sense
from alpaca.tests.attachkit import git, make_upstream, studio  # noqa: F401


def tree(tmp_path, files):
    for rel, text in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return tmp_path


def names(gates):
    return [g["name"] for g in gates]


def test_makefile_targets_become_argv_gates_with_prefixed_traces(tmp_path):
    root = tree(tmp_path, {"Makefile": "build:\n\tcc x\n\ntest:\n\t./t\n\nlint:\n\techo\n\nrun:\n\t./a\n"})
    gates, traces = sense.sense_gates(str(root), "app")
    assert names(gates) == ["build", "test", "lint"]                     # run starts the product, it is not a check
    assert gates[1]["command"] == ["make", "test"] and gates[0]["timeout"] == 600
    assert traces == {"build": "app/Makefile", "test": "app/Makefile", "lint": "app/Makefile"}


def test_package_json_gives_npm_gates_as_argv(tmp_path):
    root = tree(tmp_path, {"package.json": json.dumps({"name": "x"})})
    gates, traces = sense.sense_gates(str(root), "web")
    assert {g["name"]: g["command"] for g in gates} == {"build": ["npm", "run", "build"], "test": ["npm", "test"]}
    assert traces["test"] == "web/package.json"


def test_pyproject_gives_pytest(tmp_path):
    gates, traces = sense.sense_gates(str(tree(tmp_path, {"pyproject.toml": "[project]\nname='x'\n"})), "app")
    assert gates == [{"name": "test", "command": ["python3", "-m", "pytest"], "timeout": 600}]
    assert traces == {"test": "app/pyproject.toml"}


def test_a_repo_with_only_pytest_ini_keeps_its_test_gate(studio, tmp_path):
    """The studio ships its own pytest.ini; a trace of plain 'pytest.ini' would be mistaken for it (PM t-647.6)."""
    up = make_upstream(tmp_path, "py", files={"pytest.ini": "[pytest]\n", "t.py": "x = 1\n"})
    cfg = repo.attach(str(studio), up.url)
    assert [g["command"] for g in cfg.gates] == [["python3", "-m", "pytest"]]
    from alpaca import db
    ev = [e for e in db.events(db.connect(str(studio))) if e["kind"] == "attach-init"][0]
    assert ev["data"]["traces"] == {"test": "app/pytest.ini"}


def test_cmake_project_gets_the_configure_gate(tmp_path):
    """QTRM64 has a root CMakeLists.txt and nothing else the older sensing knows (probe section 4)."""
    root = tree(tmp_path, {"CMakeLists.txt": "cmake_minimum_required(VERSION 3.10)\nproject(x)\n"})
    gates, traces = sense.sense_gates(str(root), "app")
    assert gates == [{"name": "cmake-configure", "command": ["cmake", "-S", ".", "-B", "build/alpaca-gate"], "timeout": 600}]
    assert traces == {"cmake-configure": "app/CMakeLists.txt"}


def test_cmake_comes_after_the_make_gates(tmp_path):
    root = tree(tmp_path, {"Makefile": "test:\n\techo\n", "CMakeLists.txt": "project(x)\n"})
    assert names(sense.sense_gates(str(root), "app")[0]) == ["test", "cmake-configure"]


def test_nothing_sensed_is_empty_not_guessed(tmp_path):
    root = tree(tmp_path, {"README.md": "hello\n", "src/a.c": "int a;\n"})
    assert sense.sense_gates(str(root), "app") == ([], {})


def test_a_target_name_inside_a_recipe_is_not_a_gate(tmp_path):
    root = tree(tmp_path, {"Makefile": "all:\n\techo build:\n"})
    assert sense.sense_gates(str(root), "app")[0] == []


def test_sensing_runs_nothing(tmp_path):
    marker = tmp_path / "ran"
    root = tree(tmp_path / "r", {"Makefile": "test:\n\ttouch %s\n" % marker})
    sense.sense_gates(str(root), "app")
    assert not marker.exists()


def test_an_unsensable_repo_is_refused_by_attach_with_a_hint(studio, tmp_path):
    up = make_upstream(tmp_path, "empty", files={"README.md": "nothing\n"})
    with pytest.raises(AttachError) as exc:
        repo.attach(str(studio), up.url)
    assert "no attach gates" in str(exc.value) and "--gates-file" in str(exc.value)


def test_a_build_file_linked_outside_the_repository_is_refused_and_not_read(tmp_path):
    outside = tmp_path / "outside-Makefile"
    outside.write_text("test:\n\techo outside\n")
    repo_dir = tmp_path / "r"
    repo_dir.mkdir()
    (repo_dir / "Makefile").symlink_to(outside)
    with pytest.raises(AttachError, match="Makefile is a link that points outside"):
        sense.sense_gates(str(repo_dir), "app")


@pytest.mark.parametrize("name", ["package.json", "pyproject.toml", "CMakeLists.txt"])
def test_every_sensed_file_is_checked_for_a_link_out(tmp_path, name):
    outside = tmp_path / "outside-file"
    outside.write_text("x\n")
    repo_dir = tmp_path / "r"
    repo_dir.mkdir()
    (repo_dir / name).symlink_to(outside)
    with pytest.raises(AttachError, match=name):
        sense.sense_gates(str(repo_dir), "app")


def test_a_build_file_linked_to_another_file_of_the_repository_is_read(tmp_path):
    repo_dir = tree(tmp_path / "r", {"build/real.mk": "test:\n\t./t\n"})
    (repo_dir / "Makefile").symlink_to(repo_dir / "build" / "real.mk")
    gates, traces = sense.sense_gates(str(repo_dir), "app")
    assert names(gates) == ["test"] and traces["test"] == "app/Makefile"


def test_attach_init_refuses_a_repository_whose_makefile_links_out(studio, tmp_path):
    outside = tmp_path / "outside-Makefile"
    outside.write_text("test:\n\techo outside\n")
    up = make_upstream(tmp_path, "lnk", files={"README.md": "x\n"})
    import os
    os.symlink(str(outside), up.work / "Makefile")
    git(up.work, "add", "-A")
    git(up.work, "commit", "-q", "-m", "link")
    git(up.work, "push", "-q", "origin", "main")
    with pytest.raises(AttachError, match="points outside the repository"):
        repo.attach(str(studio), up.url)
    assert not (studio / "app").exists()
