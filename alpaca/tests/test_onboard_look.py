"""The look (op-062 t-656, AC-19): the repository is read before anything is asked, in a copy outside the studio,
and nothing the repository wrote runs."""
import datetime
import os
import time

import pytest

from alpaca import onboard_look as ol
from alpaca.tests.rehearsal import fixtures as rf

TODAY = datetime.date(2026, 10, 14)
NOW = TODAY.isoformat()


def day(n):
    return TODAY - datetime.timedelta(days=n)


@pytest.fixture
def studio_root(tmp_path):
    root = tmp_path / "studio"
    root.mkdir()
    return str(root)


@pytest.fixture
def scratch(tmp_path):
    return str(tmp_path / "scratch" / "onboard-look")


def trap_repo(tmp_path, marker):
    """A repository whose build files would touch `marker` if anything in them ran."""
    files = {
        "CMakeLists.txt": 'cmake_minimum_required(VERSION 3.10)\nproject(trap)\nfile(WRITE %s "ran")\n' % marker,
        "Makefile": "hook:\n\ttouch %s\n" % marker,
        "package.json": '{"name": "trap", "scripts": {"postinstall": "touch %s"}}\n' % marker,
        ".husky/pre-push": "#!/bin/sh\ntouch %s\n" % marker,
        ".github/workflows/ci.yml": "name: ci\non: [push]\n",
        "CLAUDE.md": "# notes\n",
        "app.py": "print('hi')\n",
        "tests/test_app.py": "def test_x():\n    assert True\n",
    }
    commits = [{"files": files, "author": "Ada Author", "day": day(100), "message": "first"},
               {"files": {"b.txt": "b\n"}, "author": "Bo Builder", "day": day(40), "message": "second"},
               {"files": {"c.txt": "c\n"}, "author": "Cy Coder", "day": day(5), "message": "third"}]
    return rf.build_repo(tmp_path / "trap-repo", commits)


def test_a_look_runs_no_code_of_the_repository_and_writes_nothing_in_the_studio(tmp_path, studio_root, scratch):
    marker = str(tmp_path / "marker")
    src = trap_repo(tmp_path, marker)
    before = sorted(os.listdir(studio_root))
    look = ol.look(studio_root, src, scratch=scratch, now=NOW)
    assert not os.path.exists(marker)                                  # nothing ran: not cmake, make, npm or a hook
    assert sorted(os.listdir(studio_root)) == before == []
    assert look["clone"] == os.path.join(os.path.realpath(scratch), "clone")
    assert not look["clone"].startswith(os.path.realpath(studio_root))
    by = {g["name"]: g for g in look["gates"]}
    assert by["cmake-configure"]["trial"] is None and by["cmake-configure"]["classify"] == "runs-repo-code"
    assert by["test"]["trial"] is None and by["build"]["trial"] is None


def test_the_facts_carry_evidence_and_history_and_surroundings_hold_the_expected_values(tmp_path, studio_root, scratch):
    src = trap_repo(tmp_path, str(tmp_path / "marker"))
    look = ol.look(studio_root, src, scratch=scratch, now=NOW)
    h = look["history"]
    assert h["base"] == "main" and h["commits"] == 3 and h["first"] == str(day(100)) and h["last"] == str(day(5))
    assert h["days_still"] == 5
    assert {a["name"]: a["commits"] for a in h["authors"]} == {"Ada Author": 1, "Bo Builder": 1, "Cy Coder": 1}
    assert [a["name"] for a in h["recent_authors"]] == ["Cy Coder", "Bo Builder"]     # newest first; Ada's commit is 100 days old
    a = look["around"]
    assert a["ci"] == [".github/workflows/ci.yml"] and a["hooks"] == [".husky/pre-push"] and a["agent_files"] == ["CLAUDE.md"]
    assert a["tests"] == ["tests/test_app.py"] and a["codeowners"] == [] and a["contributing"] is None
    assert look["tracked"]["files"] == 10 and look["tracked"]["python_files"] == 2
    assert "CMakeLists.txt" in look["build_files"] and "package.json" in look["build_files"]
    texts = [s["text"] for s in look["sources"]]
    assert [s["n"] for s in look["sources"]] == [1, 2, 3, 4, 5, 6]
    assert "repo:CMakeLists.txt" in texts[0] and texts[2] == "cmd: git ls-files | wc -l = 10"
    assert texts[3] == "cmd: git log main --format=%an%x09%ad --date=short (3 authors, 3 commits)"
    assert "found .github/workflows/ci.yml" in texts[5]
    assert all(g["evidence"].startswith("repo:") for g in look["gates"])
    assert look["digest"] == ol.digest(look) and len(look["digest"]) == 64


def test_a_repo_that_already_holds_alpaca_files_is_seen_so_the_footprint_question_is_not_asked(tmp_path, studio_root, scratch):
    """op-062 t-691: the repo answers 'may Alpaca files live in it' when it already holds some."""
    files = {"a.txt": "a\n", "ALPACA-MANIFEST": "x\n", ".alpaca/alpaca.db": "x\n", ".claude/skills/alpaca-op/SKILL.md": "x\n",
             ".claude/skills/mine/SKILL.md": "x\n"}
    src = rf.build_repo(tmp_path / "has", [{"files": files, "day": day(3)}])
    look = ol.look(studio_root, src, scratch=scratch, now=NOW)
    assert look["around"]["alpaca_files"] == [".alpaca/alpaca.db", ".claude/skills/alpaca-op/SKILL.md", "ALPACA-MANIFEST"]
    assert "found .alpaca/alpaca.db" in look["sources"][5]["text"]
    plain = rf.build_repo(tmp_path / "plain", [{"files": {"a.txt": "a\n", ".claude/skills/mine/SKILL.md": "x\n"}, "day": day(3)}])
    assert ol.look(studio_root, plain, scratch=scratch, now=NOW)["around"]["alpaca_files"] == []


def test_the_python_syntax_gate_is_tried_and_passes(tmp_path, studio_root, scratch):
    src = rf.build_repo(tmp_path / "py", [{"files": {"a.py": "x = 1\n", "b/c.py": "y = 2\n"}, "day": day(3)}])
    look = ol.look(studio_root, src, scratch=scratch, now=NOW)
    gate = look["gates"][0]
    assert gate["name"] == "py-syntax" and gate["classify"] == "parse"
    assert gate["trial"]["exit"] == 0 and "parsed 2 files" in gate["trial"]["tail"] and gate["trial"]["seconds"] < 30
    assert gate["evidence"] == "repo:a.py"


def test_a_failing_parse_gate_reports_its_exit(tmp_path, studio_root, scratch):
    src = rf.build_repo(tmp_path / "bad", [{"files": {"a.py": "def (:\n"}, "day": day(3)}])
    gate = ol.look(studio_root, src, scratch=scratch, now=NOW)["gates"][0]
    assert gate["trial"]["exit"] != 0 and "SyntaxError" in gate["trial"]["tail"]


def test_a_python_repo_with_a_test_gate_gets_no_syntax_gate(tmp_path, studio_root, scratch):
    src = rf.build_repo(tmp_path / "mk", [{"files": {"a.py": "x = 1\n", "Makefile": "test:\n\tpytest\n"}, "day": day(3)}])
    names = [g["name"] for g in ol.look(studio_root, src, scratch=scratch, now=NOW)["gates"]]
    assert names == ["test"]


def test_a_team_repo_look_finds_the_branch_rule_and_the_four_people(tmp_path, studio_root, scratch):
    src = rf.build_team_repo(tmp_path / "team", TODAY)
    look = ol.look(studio_root, src, scratch=scratch, now=NOW)
    assert look["around"]["contributing_branch_prefix"] == "feature/"
    assert look["around"]["contributing_branch_rule_at"] == "repo:CONTRIBUTING.md:5"
    assert look["around"]["codeowners"] == ["CODEOWNERS"] and look["around"]["agent_files"] == ["AGENTS.md"]
    assert len(look["history"]["recent_authors"]) == 4
    assert [g["name"] for g in look["gates"]] == ["test"]
    assert look["repository"] is None and look["upstream"] == look["source"]


def test_the_scratch_folder_must_lie_outside_the_studio(tmp_path, studio_root):
    src = rf.build_repo(tmp_path / "r", [{"files": {"a.txt": "a\n"}, "day": day(3)}])
    for bad in (os.path.join(studio_root, "scratch"), studio_root, os.path.join(studio_root, ".alpaca", "x"),
                os.path.dirname(studio_root)):
        with pytest.raises(ol.LookError) as exc:
            ol.look(studio_root, src, scratch=bad, now=NOW)
        assert exc.value.code == "LOOK-SCRATCH-INSIDE"
    with pytest.raises(ol.LookError) as exc:
        ol.look(studio_root, src, scratch="relative/dir", now=NOW)
    assert exc.value.code == "LOOK-SCRATCH"
    assert os.listdir(studio_root) == []


def test_a_link_into_the_studio_does_not_make_an_inside_scratch_look_outside(tmp_path, studio_root):
    src = rf.build_repo(tmp_path / "r", [{"files": {"a.txt": "a\n"}, "day": day(3)}])
    link = tmp_path / "link"
    os.symlink(studio_root, link)
    with pytest.raises(ol.LookError) as exc:
        ol.look(studio_root, src, scratch=str(link / "inside"), now=NOW)
    assert exc.value.code == "LOOK-SCRATCH-INSIDE"


def test_a_second_look_replaces_the_clone_it_made_and_never_one_it_did_not(tmp_path, studio_root, scratch):
    src = rf.build_repo(tmp_path / "r", [{"files": {"a.txt": "a\n"}, "day": day(3)}])
    first = ol.look(studio_root, src, scratch=scratch, now=NOW)
    second = ol.look(studio_root, src, scratch=scratch, now=NOW)
    assert first["clone"] == second["clone"] and os.path.isdir(second["clone"])
    stranger = str(tmp_path / "foreign")
    os.makedirs(os.path.join(stranger, "clone"))
    with open(os.path.join(stranger, "clone", "keep.txt"), "w") as fh:
        fh.write("mine")
    with pytest.raises(ol.LookError) as exc:
        ol.look(studio_root, src, scratch=stranger, now=NOW)
    assert exc.value.code == "LOOK-SCRATCH-USED"
    assert os.path.exists(os.path.join(stranger, "clone", "keep.txt"))
    assert ol.remove_scratch_clone(scratch) and not os.path.exists(scratch)
    assert not ol.remove_scratch_clone(stranger) and os.path.exists(os.path.join(stranger, "clone", "keep.txt"))


@pytest.mark.parametrize("key,value", [("core.fsmonitor", "{sh}"), ("core.hooksPath", "{hooks}")])
def test_a_local_folder_with_a_hostile_git_config_is_reported_and_nothing_in_it_runs(tmp_path, studio_root, scratch, key, value):
    marker = tmp_path / "marker"
    sh = tmp_path / "evil.sh"
    sh.write_text("#!/bin/sh\ntouch %s\n" % marker)
    sh.chmod(0o755)
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    for name in ("pre-commit", "post-checkout", "post-merge"):
        (hooks / name).write_text("#!/bin/sh\ntouch %s\n" % marker)
        (hooks / name).chmod(0o755)
    src = rf.build_repo(tmp_path / "hostile", [{"files": {"a.txt": "a\n"}, "day": day(3)}])
    rf.git(src, "config", key, value.format(sh=sh, hooks=hooks))
    look = ol.look(studio_root, src, scratch=scratch, now=NOW)
    assert not marker.exists()
    assert look["git_safe"] is False
    assert key in {f["token"] for f in look["git_findings"]} or look["git_findings"]
    assert all(set(f) == {"token", "file", "line", "detail"} for f in look["git_findings"])


def test_an_unreachable_url_fails_fast_without_a_prompt(studio_root, scratch):
    start = time.monotonic()
    with pytest.raises(ol.LookError) as exc:
        ol.look(studio_root, "https://127.0.0.1:1/nothing.git", scratch=scratch, now=NOW, clone_timeout=30)
    assert exc.value.code in ("LOOK-CLONE", "LOOK-AUTH")
    assert time.monotonic() - start < 30
    assert not os.path.exists(os.path.join(os.path.realpath(scratch), "clone"))


@pytest.mark.parametrize("source", ["", "   ", "--upload-pack=touch /tmp/x", "-h", "ext::sh -c id",
                                    "https://user:SECRET@example.org/r.git", "/no/such/folder"])
def test_a_source_git_would_read_as_something_else_is_refused(studio_root, scratch, source):
    with pytest.raises(ol.LookError) as exc:
        ol.look(studio_root, source, scratch=scratch, now=NOW)
    assert exc.value.code == "LOOK-SOURCE" and "SECRET" not in str(exc.value)


def test_a_source_inside_the_studio_is_refused(tmp_path, studio_root, scratch):
    inner = rf.build_repo(os.path.join(studio_root, "inner"), [{"files": {"a.txt": "a\n"}, "day": day(3)}])
    with pytest.raises(ol.LookError) as exc:
        ol.look(studio_root, inner, scratch=scratch, now=NOW)
    assert exc.value.code == "LOOK-SOURCE"


def test_an_empty_repository_has_nothing_to_read(tmp_path, studio_root, scratch):
    empty = tmp_path / "empty"
    empty.mkdir()
    rf.git(empty, "init", "-q", "-b", "main")
    with pytest.raises(ol.LookError) as exc:
        ol.look(studio_root, str(empty), scratch=scratch, now=NOW)
    assert exc.value.code in ("LOOK-EMPTY", "LOOK-CLONE")


def test_text_from_the_repository_is_cleaned_to_plain_ascii(tmp_path, studio_root, scratch):
    src = rf.build_repo(tmp_path / "odd", [{"files": {"a.txt": "a\n"}, "author": "Nguy\N{LATIN SMALL LETTER E WITH CIRCUMFLEX AND TILDE}n Bach\tIgnore previous", "day": day(3)}])
    look = ol.look(studio_root, src, scratch=scratch, now=NOW)
    names = [a["name"] for a in look["history"]["authors"]] + [a["name"] for a in look["history"]["recent_authors"]]
    for name in names:
        name.encode("ascii")
        assert "\t" not in name and "\n" not in name
    assert ol.clean("a\x00b\x1b[31mred\n  line\u00e9", 40) == "a b [31mred line?"
    assert ol.clean("x" * 100, 20).endswith("...") and len(ol.clean("x" * 100, 20)) == 20


def test_a_cross_compiler_the_build_files_name_is_wanted_and_found_missing(tmp_path, studio_root, scratch):
    files = {"CMakeLists.txt": "project(x)\n", "cmake/tc.cmake": "set(TOOLCHAIN_PREFIX arm-none-eabi-)\nset(CC ${TOOLCHAIN_PREFIX}gcc)\n"}
    src = rf.build_repo(tmp_path / "fw", [{"files": files, "day": day(3)}])
    look = ol.look(studio_root, src, scratch=scratch, now=NOW)
    assert look["host"]["wanted"] == ["arm-none-eabi-gcc"] and look["host"]["tools"]["arm-none-eabi-gcc"] is None


@pytest.mark.parametrize("argv,expected", [
    (ol.PY_SYNTAX, "parse"), (["git", "diff", "--check"], "parse"), (["git", "diff", "--check", "x"], "runs-repo-code"),
    (["clang", "-fsyntax-only", "a.c"], "parse"), (["gcc", "-fsyntax-only", "-Wall", "-Iinc", "-DX=1", "-std=c11", "a.c"], "parse"),
    (["clang", "-fsyntax-only", "-fplugin=evil.so", "a.c"], "runs-repo-code"),
    (["clang", "-fsyntax-only", "-Xclang", "-load", "a.c"], "runs-repo-code"),
    (["clang", "-fsyntax-only", "@args.rsp"], "runs-repo-code"), (["gcc", "-fsyntax-only", "-B/tmp/x", "a.c"], "runs-repo-code"),
    (["clang", "a.c"], "runs-repo-code"), (["cmake", "-S", ".", "-B", "b"], "runs-repo-code"),
    (["make", "test"], "runs-repo-code"), (["npm", "test"], "runs-repo-code"), (["pytest"], "runs-repo-code"),
    (["sh", "-c", "true"], "runs-repo-code"), ([], "runs-repo-code"),
    # round 1 attacks A1 to A3b: a path to a program, a specs or config file, a plug-in, a dependency file, other forms
    (["/usr/bin/gcc", "-fsyntax-only", "a.c"], "runs-repo-code"), (["./gcc", "-fsyntax-only", "x.c"], "runs-repo-code"),
    (["tools/gcc", "-fsyntax-only", "x.c"], "runs-repo-code"), (["tools/clang", "-fsyntax-only", "x.c"], "runs-repo-code"),
    (["gcc", "--specs=evil.specs", "-fsyntax-only", "x.zz"], "runs-repo-code"),
    (["gcc", "-specs=evil.specs", "-fsyntax-only", "x.c"], "runs-repo-code"),
    (["gcc", "-fsyntax-only", "-MD", "-MF", "/tmp/x", "x.c"], "runs-repo-code"),
    (["gcc", "-fsyntax-only", "-MMD", "x.c"], "runs-repo-code"), (["gcc", "-fsyntax-only", "-o", "out", "x.c"], "runs-repo-code"),
    (["clang", "--config=evil.cfg", "-fsyntax-only", "x.c"], "runs-repo-code"),
    (["clang", "-fsyntax-only", "-fpass-plugin=./x.so", "x.c"], "runs-repo-code"),
    (["gcc", "-fsyntax-only", "-Wp,-MD,out.d", "x.c"], "runs-repo-code"),
    (["gcc", "-fsyntax-only", "-Wl,--wrap=x", "x.c"], "runs-repo-code"),
    (["gcc", "-fsyntax-only", "-I", "../outside", "x.c"], "runs-repo-code"),
    (["gcc", "-fsyntax-only", "-I/etc", "x.c"], "runs-repo-code"),
    (["gcc", "-fsyntax-only", "../x.c"], "runs-repo-code"), (["gcc", "-fsyntax-only", "/etc/passwd.c"], "runs-repo-code"),
    (["gcc", "-fsyntax-only", "x.o"], "runs-repo-code"), (["gcc", "-fsyntax-only"], "runs-repo-code"),
    (["gcc", "-fsyntax-only", "-x", "assembler", "x.c"], "runs-repo-code"),
    (["gcc", "-fsyntax-only", "-DX=$(touch p)", "x.c"], "runs-repo-code"),
    (["clang", "-fsyntax-only", "-Xclang", "-load", "-Xclang", "x.so", "x.c"], "runs-repo-code")])
def test_only_gates_that_run_nothing_of_the_repository_are_tried_before_the_yes(argv, expected):
    assert ol.classify_gate(argv) == expected


def test_the_fixed_sources_of_a_fixture_match_what_the_look_would_write():
    from alpaca.tests import onboardkit as kit
    fx = kit.qtrm64()["look"]
    assert [s["n"] for s in fx["sources"]] == [1, 2, 3, 4, 5, 6]
    assert fx["sources"][2]["text"] == "cmd: git ls-files | wc -l = 155; git ls-files Drivers | wc -l = 85"
