"""t-228: the strict grammar of the verifier's shell (design/research-engine/in-session-runtime.md
section 19).

The first design read each Bash call the way bash reads it and refused what bash would do outside the
try folder; it never converged (five negative checks, each found a new spelling of the same thing). The
guard and the judge now accept a small language every word of which they can read exactly, and refuse
the rest where it stands. This file holds the tests of that language:

* 19.3  the token classes: plain characters, quotes, the two names, redirects, the one heredoc;
* 19.4  the shape of a call: command word, loop, limit;
* 19.5  the command table: every row, every option, no prefix of a long option;
* 19.6  cd, paths, links and folders;
* 19.7  redirects;
* 19.8  the deny text and the rewrites of the honest calls the grammar denies;
* 19.9  one rule source: the guard and the judge name the same calls with the grammar's rule.

Every test uses synthetic data only (try folders made by the fixtures of test_research_calltime.py and
command texts written here): no text of a transcript, no owner name, e-mail address or home path.
Each test fails when the rule it guards is taken out.
"""
from __future__ import annotations

import os
import re

import pytest

from alpaca.research import agent_transcript as at
from alpaca.research import pathscan, verifier
from alpaca.tests import research_rr004_kit as kit
from alpaca.tests.test_research_calltime import GuardWorld, Place, sh

GRAMMAR = verifier.SHELL_ALLOW


# ---------------------------------------------------------------- helpers
@pytest.fixture
def pl(project):
    """A project root with a try folder and the other member's folders, with a link in `out/` that
    leads to the root, one to a file of the root, and one in `tree/` into the other member's `out/`."""
    p = Place(project)
    os.symlink(project, os.path.join(p.folder, "out", "l"))
    os.symlink(os.path.join(project, "CLAUDE.md"), os.path.join(p.folder, "out", "lf"))
    os.symlink(os.path.join(p.other, "out"), os.path.join(p.folder, "tree", "sib"))
    with open(os.path.join(p.other, "out", "draft.json"), "w", encoding="utf-8") as fh:
        fh.write("{}\n")
    for name in ("a.txt", "b.txt"):
        with open(os.path.join(p.folder, "out", name), "w", encoding="utf-8") as fh:
            fh.write(name + "\n")
    os.makedirs(os.path.join(p.folder, "packet", "evidence"), exist_ok=True)
    os.makedirs(os.path.join(p.folder, "base"), exist_ok=True)
    return p


@pytest.fixture
def gw(project, monkeypatch):
    g = GuardWorld(monkeypatch, project)
    yield g
    g.close()


def chk(pl, text: str, **kw):
    """The grammar's refusal of a call as it starts in the project root, or None."""
    return pathscan.grammar_check(text, table=GRAMMAR.table, try_folder=pl.folder, start=pl.root, **kw)


def rule(pl, text: str, *, cd: bool = True):
    """The rule that refuses `text` (after `cd <try folder> &&` unless `cd` is False), or None."""
    got = chk(pl, pl.cd(text) if cd else text)
    return got["rule"] if got else None


def ok(pl, text: str) -> bool:
    return rule(pl, text) is None


def words(text: str) -> list:
    """The words the tokenizer reads from a simple command, each as its text."""
    run = pathscan._GrammarRun
    return [run.text(t[1], {}, True) for t in pathscan._g_tokens(text) if t[0] == "word"]


# ================================================================ 19.3 the token classes
def test_plain_characters_outside_quotes_are_literal(pl):
    assert ok(pl, "echo a_b.c/d:e=f,g+h%i@j^k-l 0123 ABC xyz")
    assert words("echo a_b.c/d:e=f,g+h%i@j^k-l") == ["echo", "a_b.c/d:e=f,g+h%i@j^k-l"]
    # pieces written together are one word
    assert words("echo a'b c'\"d e\"f") == ["echo", "ab cd ef"]
    # an `=` makes an assignment only in the command word, which 19.4.3 refuses
    assert ok(pl, "echo a=b")
    assert rule(pl, "a=b echo x") == "assignment"


def test_single_quotes_hold_any_character_and_must_close(pl):
    assert ok(pl, "echo 'a b $x * ? [ ] { } ~ ( ) ! # \\ ` &'")
    assert words("echo 'a\nb'") == ["echo", "a\nb"]
    assert rule(pl, "echo 'abc") == "unreadable"
    assert rule(pl, "echo \"abc") == "unreadable"


def test_double_quotes_read_the_four_escapes_as_bash_reads_them(pl):
    assert words('echo "a\\"b"') == ["echo", 'a"b']                 # \" is one quote
    assert words('echo "a\\\\b"') == ["echo", "a\\b"]               # \\ is one backslash
    assert words('echo "a\\$b"') == ["echo", "a$b"]                 # \$ is a dollar
    assert words('echo "a\\`b"') == ["echo", "a`b"]                 # \` is a backquote
    assert words('echo "a\\|b"') == ["echo", "a\\|b"]               # any other escape is kept
    assert words('echo "a\\nb"') == ["echo", "a\\nb"]
    assert ok(pl, 'echo "a\\"b" "x\\|y"')
    # a backslash at the end of a line is refused, and `!` inside double quotes is plain text
    assert rule(pl, 'echo "a\\\nb"') == "backslash"
    assert ok(pl, 'echo "a!b" "a * b" "{x}" "a # b"')
    assert words('echo ""') == ["echo", ""]


@pytest.mark.parametrize("ch,expect", [
    ("*", "glob"), ("?", "glob"), ("[", "glob"), ("]", "glob"), ("{", "brace"), ("}", "brace"),
    ("~", "tilde"), ("`", "backquote"), ("\\", "backslash"), ("(", "paren"), (")", "paren"),
    ("!", "bang"), ("#", "comment"), ("$", "expansion"), ("&", "background"),
    ("\r", "character"), ("\x01", "character"), ("\xe9", "character"), ("\x7f", "character"),
])
def test_each_character_outside_quotes_is_refused_with_its_rule_and_plain_inside_quotes(pl, ch, expect):
    assert rule(pl, "echo a%s b" % ch) == expect, repr(ch)
    # inside single quotes every one is plain text
    assert ok(pl, "echo 'a%sb'" % ch), repr(ch)
    # inside double quotes only `$` and the backquote and the backslash have a meaning
    if ch in ("$", "`"):
        assert rule(pl, 'echo "a%sb"' % ch) in ("expansion", "backquote")
    elif ch != "\\":
        assert ok(pl, 'echo "a%sb"' % ch), repr(ch)


def test_a_dollar_form_other_than_the_two_names_is_refused(pl):
    for text in ("echo ${f}", "echo $1", "echo $@", "echo $$", "echo $'x'", 'echo $"x"', "echo $(ls)",
                 "echo $((1+1))", 'echo "${f}"', 'echo "$(ls)"', 'echo "$1"', 'echo "x$"', "echo $"):
        assert rule(pl, text) == "expansion", text
    assert rule(pl, 'echo "`ls`"') == "backquote"
    assert rule(pl, "echo `ls`") == "backquote"


def test_the_separators_are_four_and_the_rest_are_refused(pl):
    assert ok(pl, "ls; ls && ls || ls | cat")
    assert ok(pl, "ls\nls\n\nls\n")
    assert ok(pl, "ls &&\nls")
    assert ok(pl, "ls |\ncat")
    assert rule(pl, "ls &") == "background"
    assert rule(pl, "ls & ls") == "background"
    assert rule(pl, "ls |& cat") == "redirect"
    assert rule(pl, "ls ;; ls") == "character"
    assert rule(pl, "ls ;& ls") == "character"
    assert rule(pl, "ls &> out/x") == "redirect"
    assert rule(pl, "ls &>> out/x") == "redirect"
    # an operator with no command beside it does not read
    for text in ("; ls", "&& ls", "ls &&", "ls ||", "ls |", "ls ; ; ls", "| ls"):
        assert rule(pl, text, cd=False) == "unreadable", text
    assert rule(pl, "", cd=False) == "unreadable"
    assert rule(pl, "\n\n", cd=False) == "unreadable"


def test_the_two_names_of_a_loop_and_of_echo(pl):
    assert ok(pl, "for f in a b; do echo $f \"$f\" x/$f; done")
    assert rule(pl, "echo $f") == "expansion"
    assert rule(pl, "for f in a; do echo $g; done") == "expansion"
    assert rule(pl, "for f in a; do echo ${f}; done") == "expansion"
    assert rule(pl, "for f in a; do echo $f; done; echo $f") == "expansion"      # not around it any more
    assert ok(pl, 'echo "rc=$?"')
    assert ok(pl, "echo $?")
    assert ok(pl, "printf '%s ' $? x")
    assert rule(pl, "cat $?") == "expansion"
    assert rule(pl, "grep -c x $?") == "expansion"
    assert rule(pl, "for f in $?; do ls; done") == "loop"
    assert rule(pl, "echo x > out/$?") == "expansion"
    assert rule(pl, "ls out/$?") == "expansion"


def test_a_name_is_the_longest_run_of_letters_digits_and_underscore(pl):
    assert rule(pl, "for f in a; do echo $fx; done") == "expansion"
    assert rule(pl, "for f in a; do echo $f_1; done") == "expansion"
    assert rule(pl, "for f in a; do echo $f1; done") == "expansion"
    assert ok(pl, 'for f in a; do echo "$f"_1 $f/x $f.y "$f"x; done')
    assert pathscan._g_tokens("echo $fx")[1][1] == [("var", "fx")]
    assert pathscan._g_tokens('echo "a$fx"')[1][1] == [("dq", "a"), ("var", "fx")]


# ---- redirects
def test_each_redirect_operator_the_grammar_does_not_name_is_refused(pl):
    for text in ("ls >| out/x", "ls <> out/x", "ls <<< x", "cat <<- 'X'\nx\nX", "ls >& out/x", "ls <& x",
                 "ls >&x", "ls 2>&x", "ls {fd}> out/x", "ls &> out/x", "ls &>> out/x", "ls 2>&-", "ls <&-"):
        assert rule(pl, text) in ("redirect", "heredoc"), text
    assert rule(pl, "ls >| out/x") == "redirect"
    assert rule(pl, "ls {fd}> out/x") == "redirect"
    assert rule(pl, "ls >& out/x") == "redirect"
    assert rule(pl, "ls 2>&-") == "redirect"
    # a descriptor of one or more digits written right before the operator joins two descriptors
    assert ok(pl, "ls 2>&1")
    assert ok(pl, "ls >&2")
    assert ok(pl, "ls 2>/dev/null")
    assert ok(pl, "ls > out/x 2>&1")
    assert ok(pl, "cat 3< packet/brief.json <&3")


def test_a_digit_is_a_descriptor_only_when_it_starts_the_word(pl):
    """`a2>x` is the word `a2` and a redirect of the output to `x`: the target is checked either way."""
    assert ok(pl, "echo a2>out/x")
    assert rule(pl, "echo a2>packet/x") == "write"
    assert rule(pl, "echo 2>packet/x") == "write"
    assert ok(pl, "echo 2 >out/x")
    assert words("echo a2>out/x") == ["echo", "a2"]
    assert words("echo 2 >out/x") == ["echo", "2"]


# ---- the heredoc of the submission
SUBMIT = "python3 packet/bin/rerun --submit - <<'VERDICT'\n{\"x\": \"$(id) `id` * ?\"}\nVERDICT"


def test_the_submission_heredoc_passes_and_nothing_in_its_body_is_read(pl):
    assert ok(pl, SUBMIT)
    assert ok(pl, SUBMIT + "\n")
    assert ok(pl, "python3 packet/bin/rerun --submit - <<'V2_X'\n(\n'unclosed\nV2_X")
    assert ok(pl, "python3 packet/bin/rerun --submit - << 'VERDICT'\nx\nVERDICT")
    assert ok(pl, "python3 packet/bin/rerun --submit - <<'VERDICT' && echo done\nbody\nVERDICT")
    # the command stays a command of the call: without the heredoc it reads stdin
    assert ok(pl, "python3 packet/bin/rerun --submit -")


def test_a_heredoc_anywhere_else_or_in_another_form_is_refused(pl):
    sub = "python3 packet/bin/rerun"
    for text, expect in (
        ("cat <<'EOF'\nx\nEOF", "heredoc"),
        ("cat <<EOF\nx\nEOF", "heredoc"),
        ('cat <<"EOF"\nx\nEOF', "heredoc"),
        ("python3 packet/bin/rerun --submit - <<VERDICT\nx\nVERDICT", "heredoc"),
        ("python3 packet/bin/rerun --submit - <<'VERDICT'X\nx\nVERDICT", "heredoc"),
        ("python3 packet/bin/rerun --submit - <<'verdict'\nx\nverdict", "heredoc"),
        ("python3 packet/bin/rerun --submit - <<'1A'\nx\n1A", "heredoc"),
        ("python3 packet/bin/rerun --submit - <<''\nx\n", "heredoc"),
        ("python3 packet/bin/rerun --submit - 3<<'V'\nx\nV", "heredoc"),
        ("python3 packet/bin/rerun --submit - <<'V' <<'W'\nx\nV\nW", "heredoc"),
        (sub + " --submit out/x <<'V'\nx\nV", "heredoc"),
        (sub + " --submit - extra <<'V'\nx\nV", "heredoc"),
        (sub + " ev-1 <<'V'\nx\nV", "heredoc"),
        ("python3 packet/bin/rerun --submit - <<'V'\nx", "unreadable"),
        ("python3 packet/bin/rerun --submit - <<'V'", "unreadable"),
        ("python3 packet/bin/rerun --submit - <<'V\nx\nV", "unreadable"),
        ("for f in a; do python3 packet/bin/rerun --submit - <<'V'\nx\nV\ndone", "heredoc"),
    ):
        assert rule(pl, text) == expect, text


def test_a_line_that_closes_the_heredoc_ends_it_and_a_command_after_it_is_read(pl):
    assert rule(pl, "python3 packet/bin/rerun --submit - <<'VERDICT'\nx\nVERDICT\nrm -rf out") == "program"
    assert rule(pl, "python3 packet/bin/rerun --submit - <<'VERDICT'\nx\nVERDICT\ncat /etc/hostname") == "path"
    # a line that only starts with the delimiter does not close it
    assert ok(pl, "python3 packet/bin/rerun --submit - <<'VERDICT'\nVERDICT2\n VERDICT\nVERDICT")
    assert rule(pl, "python3 packet/bin/rerun --submit - <<'VERDICT'\nVERDICT2\n VERDICT") == "unreadable"


# ================================================================ 19.4 the shape of a call
def test_the_command_word_is_one_bare_name_of_the_table(pl):
    assert rule(pl, "/usr/bin/cat packet/brief.json") == "command-path"
    assert rule(pl, "/bin/cat packet/brief.json") == "command-path"
    assert rule(pl, "./x.sh") == "command-path"
    assert rule(pl, "tree/python3 packet/bin/rerun") == "command-path"
    assert rule(pl, '"cat" packet/brief.json') == "command-word"
    assert rule(pl, "'cat' packet/brief.json") == "command-word"
    assert rule(pl, "ca''t packet/brief.json") == "command-word"
    assert rule(pl, "$P packet/brief.json") == "command-word" or rule(pl, "$P packet/brief.json") == "expansion"
    assert rule(pl, "for p in cat; do $p packet/brief.json; done") == "command-word"
    assert rule(pl, "P=cat; $P x") == "assignment"
    assert rule(pl, "PATH=tree cat x") == "assignment"
    assert rule(pl, "IFS=Q cat x") == "assignment"
    assert rule(pl, "X='a b' cat x") == "assignment"
    assert rule(pl, "X= cat x") == "assignment"


@pytest.mark.parametrize("word", ["if", "then", "else", "elif", "fi", "while", "until", "do", "done",
                                  "case", "esac", "select", "function", "time", "coproc"])
def test_each_reserved_word_is_refused_as_a_command(pl, word):
    assert rule(pl, "%s ls" % word) == "reserved"


@pytest.mark.parametrize("name", ["bash", "sh", "env", "xargs", "find", "builtin", "command", "eval", "exec",
                                  "source", ".", "alias", "set", "shopt", "export", "pushd", "popd", "nohup",
                                  "timeout", "[", "git", "make", "awk", "perl", "ruby", "node", "curl", "tee",
                                  "cp", "mv", "rm", "mkdir", "touch", "ln", "chmod", "unset", "trap", "read",
                                  "wait", "kill", "sleep", "python", "pytest", "ssh", "dd", "install", "tar"])
def test_a_name_off_the_table_is_refused_as_a_program(pl, name):
    got = rule(pl, "%s x" % name)
    assert got in ("program", "glob"), (name, got)      # `[` is a glob character before it is a name
    if name not in ("[",):
        assert got == "program"


def test_python3_runs_only_the_request_client_of_the_try_folder(pl):
    assert ok(pl, "python3 packet/bin/rerun ev-1 --event packet/brief.json")
    assert ok(pl, "python3 %s/packet/bin/rerun ev-1" % pl.folder)
    assert rule(pl, "python3 packet/bin/rerun") is None
    for text in ("python3 -c 1", "python3 -c 'print(1)'", "python3 out/x.py", "python3 tree/x.py",
                 "python3", "python3 packet/bin/rerun2", "python3 ../packet/bin/rerun",
                 "python3 tree/../packet/bin/rerun", "python3 %s/packet/bin/rerun" % pl.other):
        assert rule(pl, text) == "client", text
    # from the project root, `packet/bin/rerun` is not the try folder's
    assert rule(pl, "python3 packet/bin/rerun ev-1", cd=False) == "client"
    # the client's own arguments are words of the grammar and nothing more
    assert ok(pl, "python3 packet/bin/rerun ev-1 --shell 'python3 -m pytest -q $(ls) *'")
    assert ok(pl, "python3 packet/bin/rerun ev-1 -- sh -c 'echo x > /tmp/x'")
    assert rule(pl, "python3 packet/bin/rerun ev-1 --shell $(ls)") == "expansion"
    assert ok(pl, "for g in build unit metric:total_bytes; do python3 packet/bin/rerun --gate $g --side tree; done")
    assert ok(pl, "for e in 83131 83041; do python3 packet/bin/rerun ev-$e --event packet/events/$e.json; done")


def test_a_call_holds_at_most_256_simple_commands_a_loop_body_counted_for_each_word(pl):
    assert ok(pl, "; ".join(["echo x"] * 255))          # the cd of pl.cd is the 256th
    assert chk(pl, "; ".join(["echo x"] * 256)) is None
    assert chk(pl, "; ".join(["echo x"] * 257))["rule"] == "too-long"
    assert rule(pl, "; ".join(["echo x"] * 256)) == "too-long"
    # a loop body is counted once for each word: 127 words and two commands is 254, 129 is 258
    assert ok(pl, "for f in %s; do echo $f; echo x; done" % " ".join("a%d" % i for i in range(127)))
    assert rule(pl, "for f in %s; do echo $f; echo x; done" % " ".join("a%d" % i for i in range(129))) == "too-long"
    # a pipeline counts each of its commands
    assert rule(pl, " | ".join(["cat"] * 257)) == "too-long"


# ---- the loop
def test_the_loop_has_one_form_and_a_name_no_shell_or_program_reads(pl):
    assert ok(pl, "for f in a b c; do echo $f; done")
    assert ok(pl, "for f in a b; do echo $f; echo x; done; ls")
    assert ok(pl, "for f in a b; do\necho $f\ndone")
    assert ok(pl, "for f in a b; do echo $f; done && ls || ls")
    assert ok(pl, "for f in a b; do cat packet/brief.json | cat; done")
    assert ok(pl, "for f1 in a b; do echo $f1; done")
    assert ok(pl, "for abcdefghijklmnop in a; do echo $abcdefghijklmnop; done")      # sixteen
    for name in ("IFS", "PATH", "HOME", "CDPATH", "BASH_ENV", "File", "my_f", "_", "abcdefghijklmnopq", "1f",
                 "F"):
        assert rule(pl, "for %s in a; do ls; done" % name) == "loop", name
    # a name bash holds back across `done` is the reason: `for IFS in Q; ...; for f in Q<path>; do cat $f`
    assert rule(pl, "for IFS in Q; do true; done; for f in Qpacket/brief.json; do cat $f; done") == "loop"
    assert rule(pl, "for PATH in tree/b; do cat packet/brief.json; done") == "loop"
    assert rule(pl, "for HOME in tree; do python3 packet/bin/rerun ev-1; done") == "loop"


def test_a_loop_word_is_plain_text_and_the_loop_does_not_nest_or_redirect(pl):
    assert rule(pl, "for f in out/*.txt; do cat $f; done") == "glob"
    assert rule(pl, "for f in a?; do cat $f; done") == "glob"
    assert rule(pl, "for f in {a,b}; do cat $f; done") == "brace"
    assert rule(pl, "for f in $(ls); do cat $f; done") == "expansion"
    assert rule(pl, "for f in $x; do cat $f; done") == "loop"
    assert rule(pl, "for f in 'a b'; do cat $f; done") == "loop"
    assert rule(pl, "for f in ''; do cat $f; done") == "loop"
    assert rule(pl, "for f in; do ls; done") == "loop"
    assert rule(pl, "for f in a; do for g in b; do ls; done; done") == "loop"
    assert rule(pl, "for f in a; do cd tree; done") == "cd"
    assert rule(pl, "for f in a; do ls; done > out/x") == "loop"
    assert rule(pl, "for f in a; do ls; done | cat") == "loop"
    assert rule(pl, "for f in a; do ls; done 2>&1") == "loop"
    assert rule(pl, "for f in a; do ls; done cat") == "loop"
    assert rule(pl, "for f in a; do ls") == "unreadable"
    assert rule(pl, "for f in a; ls; done") == "loop"
    assert rule(pl, "for f in a do ls; done") == "loop"
    assert rule(pl, "for f a; do ls; done") == "loop"
    assert rule(pl, "for; do ls; done") == "loop"
    assert rule(pl, "for f in a; do ; done") == "unreadable"
    assert rule(pl, "for f in a; do ls && done") == "unreadable"
    assert rule(pl, "while true; do ls; done") == "reserved"
    assert rule(pl, "until false; do ls; done") == "reserved"
    # the word `done` as an argument is no end of the loop
    assert ok(pl, "for f in a; do echo done; done")
    # a word of the loop is the word in its place, so an option, a path or a program in it is read
    assert rule(pl, "for f in /etc/hostname; do cat $f; done") == "path"
    assert rule(pl, "for f in -r; do diff $f tree base; done") == "folder"
    assert rule(pl, "for f in -R; do grep $f x tree; done") == "option"
    assert rule(pl, "for f in ../x; do cat $f; done") == "dotdot"


# ================================================================ 19.5 the table
PROGRAM_FILES = ("cat", "head", "tail", "wc", "ls", "stat", "file", "sha256sum", "nl", "tac", "cmp", "comm",
                 "diff", "sort", "uniq", "cut", "paste", "grep", "rg", "sed")


def _rows():
    return [(name, row) for name, row in pathscan.VERIFIER_TABLE.items() if name in PROGRAM_FILES]


def _call(name: str, row: dict, optwords: str, operands: str | None = None) -> str:
    """A call of `name` with `optwords` and operands that suit its kind, written after the cd."""
    kind = row["operands"]
    if operands is not None:
        ops = operands
    elif kind in ("files", "paths", "folders", "uniq"):
        ops = "packet/brief.json" if name != "comm" else "packet/brief.json packet/brief.json"
        if name in ("diff", "cmp"):
            ops = "packet/brief.json packet/brief.json"
    elif kind == "search":
        ops = "x packet/brief.json"
    elif kind == "sed":
        ops = "p packet/brief.json"
    else:
        ops = ""
    return ("%s %s %s" % (name, optwords, ops)).replace("  ", " ")


def _val(kind: str) -> str:
    return "x" if kind in (pathscan._G_TEXT, "script") else "5"


@pytest.mark.parametrize("name,row", _rows(), ids=[n for n, _r in _rows()])
def test_every_flag_value_option_and_long_option_of_a_row_passes_as_the_row_writes_it(pl, name, row):
    for ch in row["flags"]:
        assert ok(pl, _call(name, row, "-" + ch)), (name, ch)
    if len(row["flags"]) > 1 and name not in ("diff",):
        assert ok(pl, _call(name, row, "-" + row["flags"][:3])), name
    for ch, kind in row["vals"].items():
        v = _val(kind)
        operands = None
        if ch == "e":                                  # -e gives the pattern or script itself
            operands = "packet/brief.json"
        assert ok(pl, _call(name, row, "-%s %s" % (ch, v), operands)), (name, ch, "next word")
        assert ok(pl, _call(name, row, "-%s%s" % (ch, v), operands)), (name, ch, "attached")
        if row["flags"]:
            assert ok(pl, _call(name, row, "-%s%s %s" % (row["flags"][0], ch, v), operands)), (name, ch, "joined")
    for opt, kind in row["longs"].items():
        if opt == "--files":
            assert ok(pl, "%s --files packet" % name), name
            continue
        if kind is None:
            assert ok(pl, _call(name, row, opt)), (name, opt)
        else:
            assert ok(pl, _call(name, row, "%s=x" % opt)), (name, opt, "=")
            assert ok(pl, _call(name, row, "%s x" % opt)), (name, opt, "next word")
    # `--` ends the options: an operand that starts with `-` is a file name
    if row["operands"] in ("files", "paths", "folders"):
        assert ok(pl, "%s -- -x" % name) or name in ("diff", "cmp", "comm"), name


@pytest.mark.parametrize("name,row", _rows(), ids=[n for n, _r in _rows()])
def test_a_letter_off_the_row_a_bad_value_and_a_prefix_of_a_long_option_are_refused(pl, name, row):
    bad = [c for c in "ZQXJKWYOPEGMDUVTRSNLHFCBA" if c not in row["flags"] and c not in row["vals"]][0]
    assert rule(pl, _call(name, row, "-" + bad)) in ("option", "folder"), (name, bad)
    got = chk(pl, pl.cd(_call(name, row, "-" + bad)))
    assert got["rule"] == "option" and pathscan._g_row_options(row) in got["why"], name
    if row["flags"]:
        joined = "-%s%s" % (row["flags"][0], bad)
        assert rule(pl, _call(name, row, joined)) == "option", (name, joined)
    for ch, kind in row["vals"].items():
        if kind == pathscan._G_NUMBER:
            assert rule(pl, _call(name, row, "-%s x" % ch)) == "option", (name, ch)
            assert rule(pl, _call(name, row, "-%s5x" % ch)) == "option", (name, ch)
            assert rule(pl, _call(name, row, "-%s -5" % ch)) == "option", (name, ch)
        # a value letter with no value left
        assert rule(pl, "%s -%s" % (name, ch), cd=True) in ("option", "operand"), (name, ch)
    for opt, kind in row["longs"].items():
        prefix = opt[:max(len(opt) - 2, 3)]
        assert rule(pl, _call(name, row, prefix + ("=x" if kind else ""))) == "option", (name, prefix)
        assert rule(pl, _call(name, row, opt + "x" + ("=x" if kind else ""))) == "option", (name, opt)
        if kind is None:
            assert rule(pl, _call(name, row, opt + "=x")) == "option", (name, opt, "a value for a flag")
    assert rule(pl, _call(name, row, "--zzz")) == "option"
    assert rule(pl, _call(name, row, "--zzz=1")) == "option"


def test_head_and_tail_take_a_number_as_an_option_and_the_others_do_not(pl):
    assert ok(pl, "head -5 packet/brief.json")
    assert ok(pl, "tail -20 packet/brief.json")
    assert ok(pl, "tail -n +5 packet/brief.json")
    assert ok(pl, "tail -c +5 packet/brief.json")
    assert rule(pl, "head -n -5 packet/brief.json") == "option"
    assert rule(pl, "head -n +5 packet/brief.json") == "option"
    assert rule(pl, "tail -n -5 packet/brief.json") == "option"
    assert rule(pl, "tail -f packet/brief.json") == "option"
    assert rule(pl, "cat -5 packet/brief.json") == "option"
    assert rule(pl, "grep -5 x packet/brief.json") == "option"
    assert rule(pl, "head -5x packet/brief.json") == "option"


def test_the_options_the_first_design_found_one_by_one_are_not_in_the_table(pl):
    for text in ("grep -R x tree", "grep -S x tree", "grep -d recurse x tree", "grep --dereference-recursive x tree",
                 "grep --deref x tree", "grep --directories=recurse x tree", "grep -f out/a.txt x packet/brief.json",
                 "grep --exclude-from=out/a.txt x tree", "grep --from=out/a.txt x", "grep --config=out/a.txt x",
                 "grep --filter=cat x packet/brief.json", "grep --pager=cat x packet/brief.json", "grep -Q x",
                 "grep --save-config=x x", "grep -Z x packet/brief.json", "grep -z x packet/brief.json",
                 "rg -L x tree", "rg --follow x tree", "rg --ignore-file=out/a.txt x", "rg -f out/a.txt tree",
                 "rg --pre cat x tree", "rg --pre-glob '*.x' x", "rg --hostname-bin=out/x x tree", "rg -z x tree",
                 "ls -L tree", "ls -H tree", "ls -Z tree", "diff -X out/a.txt packet/brief.json out/a.txt",
                 "diff --from-file=packet/brief.json out/a.txt", "diff -l packet/brief.json out/a.txt",
                 "file -m out/a.txt packet/brief.json", "file -C", "file -f out/a.txt", "file -L tree",
                 "sha256sum -c out/a.txt", "sha256sum --check out/a.txt", "wc --files0-from=out/a.txt",
                 "sort --files0-from=out/a.txt", "sort -o out/x packet/brief.json", "sort -T /tmp packet/brief.json",
                 "sort --compress-program=gzip packet/brief.json", "sort --output=out/x packet/brief.json",
                 "sed -i s/a/b/ out/a.txt", "sed -f out/a.txt packet/brief.json", "sed --in-place s/a/b/ out/a.txt",
                 "sed -r p packet/brief.json", "sed --expression=p packet/brief.json", "sed -z p packet/brief.json",
                 "tail -f packet/brief.json", "stat -L packet/brief.json", "stat --format=%s packet/brief.json",
                 "uniq -z packet/brief.json", "cut -z -f1 packet/brief.json", "paste -z packet/brief.json",
                 "cat -x packet/brief.json", "echo -x ; sh", "pwd -P", "seq -3 3", "basename -a x", "dirname -z x"):
        assert chk(pl, pl.cd(text)) is not None, text
        assert rule(pl, text) in ("option", "program", "operand", "client", "sed", "folder"), text


def test_a_diff_that_asks_for_a_recursive_compare_is_refused_as_a_folder_read_whatever_else_it_holds(pl):
    for text in ("diff -r tree base", "diff -rq tree base", "diff -qr tree base", "diff --recursive tree base",
                 "diff -rq --exclude=.git tree base", "diff --exclude=.git -rq tree base",
                 "diff -rq tree base > out/tree-base.txt", "diff -r out/a.txt out/b.txt", "diff -ur tree base",
                 "diff -qrs tree base"):
        assert rule(pl, text) == "folder", text
    got = chk(pl, pl.cd("diff -rq tree base"))
    assert "folder" in got["why"] or "compares two folders" in got["why"]
    assert ok(pl, "diff -u out/a.txt out/b.txt")
    assert ok(pl, "diff -U 3 out/a.txt out/b.txt")
    assert ok(pl, "diff -U3 out/a.txt out/b.txt")
    assert ok(pl, "diff -q out/a.txt out/b.txt")
    assert ok(pl, "diff -- -r out/a.txt")
    # two folders given with no -r are folders too
    assert rule(pl, "diff -q tree base") == "folder"
    assert rule(pl, "diff tree packet") == "folder"
    # the rewrite of the deny text: list both folders, compare the lists, then the files
    assert ok(pl, "ls -R tree > out/t.txt; ls -R base > out/b2.txt; diff out/t.txt out/b2.txt")
    assert ok(pl, "cmp out/a.txt out/b.txt; sha256sum out/a.txt out/b.txt")


def test_every_program_of_the_table_has_a_row_with_the_fields_the_checker_reads(pl):
    for name, row in pathscan.VERIFIER_TABLE.items():
        assert set(row) == {"flags", "vals", "longs", "operands", "words", "numopt"}, name
        assert row["operands"] in ("files", "paths", "folders", "uniq", "search", "sed", "text", "none",
                                   "test", "echo", "printf", "cd", "client"), name
        assert row["words"]
    # the table is the allow list's own: every name of the launch spec's allow list that the grammar
    # accepts is a row, and `[` is not (19.5)
    for name in GRAMMAR:
        if name != "python3 packet/bin/rerun":
            assert name in pathscan.VERIFIER_TABLE or name == "["
    assert "[" not in pathscan.VERIFIER_TABLE and "find" not in pathscan.VERIFIER_TABLE
    assert isinstance(GRAMMAR, pathscan.Grammar) and isinstance(GRAMMAR, tuple)
    assert GRAMMAR.table is pathscan.VERIFIER_TABLE
    assert GRAMMAR.submission == ("--submit", "-")


def test_a_table_without_a_program_refuses_it_and_the_rows_alone_decide(pl):
    cut = {k: v for k, v in pathscan.VERIFIER_TABLE.items() if k != "cat"}
    got = pathscan.grammar_check(pl.cd("cat packet/brief.json"), table=cut, try_folder=pl.folder, start=pl.root)
    assert got["rule"] == "program"
    assert pathscan.grammar_check(pl.cd("cat packet/brief.json"), table=pathscan.VERIFIER_TABLE,
                                  try_folder=pl.folder, start=pl.root) is None


# ---- the saved help texts
#: the options 19.5 leaves out, by program: each follows a link, reads names or options from a file, or
#: writes or runs something. None may be in the table.
LEFT_OUT = {
    "grep": {"-R", "-S", "-d", "--dereference-recursive", "-f", "--exclude-from", "--from", "--config",
             "--filter", "--pager", "--view", "-Q", "--save-config", "-z", "-Z", "--file"},
    "rg": {"-L", "--follow", "--ignore-file", "-f", "--pre", "--pre-glob", "--hostname-bin", "-z", "--file"},
    "ls": {"-L", "-H", "--dereference"},
    "diff": {"-r", "-X", "--from-file", "--to-file", "--recursive", "-l", "--exclude-from"},
    "file": {"-m", "-C", "-f", "-L", "--compile", "--files-from", "--magic-file"},
    "sha256sum": {"-c", "--check"},
    "wc": {"--files0-from"},
    "sort": {"--files0-from", "-o", "-T", "--compress-program", "--output"},
    "sed": {"-i", "-f", "--in-place", "--file", "-r"},
    "tail": {"-f", "--follow"},
    "stat": {"-L", "--dereference", "--format"},
}


def _help(name: str) -> str:
    from alpaca.tests import research_help_kit as hk
    with open(os.path.join(hk.HELP, name + ".txt"), encoding="utf-8") as fh:
        return fh.read()


def _in_help(text: str, option: str) -> bool:
    if option.startswith("--"):
        return re.search(r"(?<![A-Za-z0-9-])%s(?![A-Za-z0-9-])" % re.escape(option), text) is not None
    return re.search(r"(?<![A-Za-z0-9-])%s(?![A-Za-z0-9])" % re.escape(option), text) is not None


def test_every_flag_and_option_of_the_table_is_in_the_saved_help_text_of_its_program():
    """The texts are saved from this host (ripgrep 14.1.1, ugrep 7.8.4 as grep, uutils coreutils 0.8.0, GNU
    diffutils 3.12, file 5.46, GNU sed 4.9) under alpaca/tests/fixtures/research/help/. A row that names an
    option the program does not have, or one that follows a link, reads a file or writes, fails here."""
    have = {n[:-4] for n in os.listdir(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                    "fixtures", "research", "help")) if n.endswith(".txt")}
    assert {"sed", "tr"} <= have
    checked = 0
    for name, row in pathscan.VERIFIER_TABLE.items():
        if name not in have:
            continue
        text = _help(name)
        opts = ["-" + c for c in row["flags"]] + ["-" + c for c in row["vals"]] + list(row["longs"])
        for opt in opts:
            assert _in_help(text, opt), (name, opt)
            checked += 1
        assert not (set(opts) & LEFT_OUT.get(name, set())), (name, sorted(set(opts) & LEFT_OUT.get(name, set())))
    assert checked > 120
    # negative: an option the help text does not give, and one the left-out list names
    assert not _in_help(_help("cat"), "-Z") and not _in_help(_help("sed"), "--nonesuch")
    assert "-R" in LEFT_OUT["grep"] and "-R" not in pathscan.VERIFIER_TABLE["grep"]["flags"]


def test_the_left_out_options_are_each_refused(pl):
    for name, opts in LEFT_OUT.items():
        for opt in opts:
            if opt in ("-d", "-r", "-z") and name in ("diff", "ls"):
                continue
            arg = "x" if (name in ("grep", "rg") and not opt.startswith("--") and opt[1] in "dfm") else ""
            line = "%s %s %s packet/brief.json" % (name, opt, arg)
            if name in ("grep", "rg", "sed"):
                line = "%s %s %s p packet/brief.json" % (name, opt, arg)
            assert chk(pl, pl.cd(line)) is not None, line


# ================================================================ 19.6 cd, paths and folders
def test_the_one_cd_is_the_first_command_and_names_the_try_folder_as_the_brief_gives_it(pl):
    f = pl.folder
    assert chk(pl, "cd %s && ls" % f) is None
    assert chk(pl, "cd %s/ && ls" % f) is None                     # a trailing slash is taken off
    assert chk(pl, "cd %s; ls" % f) is None
    assert chk(pl, "cd %s" % f) is None
    assert chk(pl, "cd %s\nls" % f) is None
    # every other form is refused: a second cd, a cd in a pipeline or a loop, an option, no word, two
    # words, a folder of the try folder, another folder, a cd that is not the first command
    for text in ("cd %s && cd tree && ls" % f, "cd %s && cd %s && ls" % (f, f), "ls && cd %s" % f,
                 "ls; cd %s; ls" % f, "cd %s | cat" % f, "cat | cd %s" % f, "cd -P %s && ls" % f,
                 "cd -L %s && ls" % f, "cd -e %s && ls" % f, "cd -- %s && ls" % f, "cd - && ls", "cd && ls",
                 "cd %s tree && ls" % f, "cd %s/tree && ls" % f, "cd tree && ls", "cd %s && ls" % pl.other,
                 "cd /etc && ls", "cd .. && ls", "cd . && ls", "cd %s/../t0 && ls" % f,
                 "for d in a; do cd %s; done" % f, "builtin cd %s && ls" % f, "command cd %s && ls" % f,
                 "pushd %s && ls" % f, "CDPATH=x cd tree && ls", "if true; then cd %s; fi" % f):
        got = chk(pl, text)
        assert got and got["rule"] in ("cd", "program", "reserved", "assignment", "loop", "path"), (text, got)
    # a cd that is not the first command, started in the try folder so that the call reads no other path
    for text in ("ls && cd %s" % f, "ls; cd %s; ls" % f, "cat | cd %s" % f, "for d in a; do cd %s; done" % f):
        got = pathscan.grammar_check(text, table=GRAMMAR.table, try_folder=f, start=f)
        assert got and got["rule"] == "cd", (text, got)
    for text in ("cd %s && cd tree && ls" % f, "cd %s | cat" % f, "cd - && ls", "cd && ls",
                 "cd %s/tree && ls" % f, "cd tree && ls", "cd .. && ls", "cd . && ls", "cd -P %s && ls" % f):
        assert chk(pl, text)["rule"] == "cd", text


def test_a_relative_path_is_placed_in_the_project_root_until_the_cd_and_in_the_try_folder_after(pl):
    assert chk(pl, "cat packet/brief.json")["rule"] == "path"
    assert chk(pl, "cat CLAUDE.md")["rule"] == "path"
    assert chk(pl, "cat %s/packet/brief.json" % pl.folder) is None
    assert chk(pl, pl.cd("cat packet/brief.json")) is None
    assert chk(pl, "cat < CLAUDE.md")["rule"] == "path"
    assert chk(pl, "cat < packet/brief.json")["rule"] == "path"
    assert chk(pl, pl.cd("cat < packet/brief.json")) is None
    # started in the try folder (no cd needed), the same call passes
    assert pathscan.grammar_check("cat packet/brief.json", table=GRAMMAR.table, try_folder=pl.folder,
                                  start=pl.folder) is None


def test_a_path_with_dotdot_an_outside_folder_or_a_link_out_is_refused_with_its_rule(pl):
    cases = (
        ("cat tree/../packet/brief.json", "dotdot"), ("cat ../packet/brief.json", "dotdot"),
        ("cat packet/..", "dotdot"), ("cat ..", "dotdot"), ("cat 'a/../b'", "dotdot"),
        ("cat /etc/hostname", "path"), ("cat //etc/hostname", "path"), ("cat /dev/null/x", "path"),
        ("cat %s/CLAUDE.md" % pl.root, "path"), ("cat %s/out/draft.json" % pl.other, "path"),
        ("ls %s" % pl.root, "path"), ("cat ''", "path"),
        ("cat out/lf", "link"), ("cat tree/sib/draft.json", "link"), ("ls tree/sib", "link"),
        ("cat out/l/CLAUDE.md", "link"), ("head -n 1 out/lf", "link"), ("grep x out/lf", "link"),
        ("test -f out/lf", "link"), ("cat < out/lf", "link"),
    )
    for text, expect in cases:
        assert rule(pl, text) == expect, text
    assert ok(pl, "cat /dev/null")
    assert ok(pl, "cat < /dev/null")
    assert ok(pl, "ls out > /dev/null")
    assert ok(pl, "ls 2>/dev/null")


def test_the_implied_folder_of_ls_grep_r_and_rg_is_the_folder_the_call_is_in(pl):
    for text in ("ls", "ls -la", "grep -r x", "grep -rn x", "rg x", "rg -n x", "rg --files"):
        assert chk(pl, text)["rule"] == "path", text          # from the project root: the root
        assert chk(pl, pl.cd(text)) is None, text               # after the cd: the try folder
    assert chk(pl, pl.cd("grep x")) is None                     # no -r and no file: it reads stdin
    assert chk(pl, "echo x | grep x") is None
    assert chk(pl, "cat packet/brief.json | rg x")["rule"] == "path"


def test_the_prompt_file_and_the_persisted_outputs_pass_by_their_exact_path_only(pl, project):
    results = os.path.join(project, "claude-results")
    os.makedirs(results, exist_ok=True)
    one, other = os.path.join(results, "b1.txt"), os.path.join(results, "b2.txt")
    for p in (one, other):
        with open(p, "w", encoding="utf-8") as fh:
            fh.write("x\n")
    link = os.path.join(results, "lnk.txt")
    os.symlink(other, link)
    exact = (one,)
    assert chk(pl, "cat %s" % one, exact_files=exact) is None
    assert chk(pl, pl.cd("cat < %s" % one), exact_files=exact) is None
    assert chk(pl, "cat %s" % one)["rule"] == "path"                       # not given: outside the folder
    assert chk(pl, "cat %s" % other, exact_files=exact)["rule"] == "path"  # another file of the same folder
    assert chk(pl, "ls %s" % results, exact_files=exact)["rule"] == "path"  # the folder is no file
    assert chk(pl, "cat %s/x" % one, exact_files=exact)["rule"] == "path"
    assert chk(pl, "cat %s" % link, exact_files=exact)["rule"] == "path"
    # a link in the folder to the exact file does not make the link allowed, and the exact file stays
    # allowed when named through the real path
    assert chk(pl, "cat %s" % os.path.realpath(one), exact_files=exact) is None
    # the grammar reaches the judge and the guard through `shell_allowed`'s exact_allowed
    hits = pathscan.shell_allowed([sh("cat %s" % one)], allow=GRAMMAR, try_folder=pl.folder,
                                  client=os.path.join(pl.folder, "packet", "bin", "rerun"),
                                  reset_cwd=pl.root, exact_allowed=exact)
    assert hits == []
    assert pathscan.shell_allowed([sh("cat %s" % one)], allow=GRAMMAR, try_folder=pl.folder,
                                  client=os.path.join(pl.folder, "packet", "bin", "rerun"),
                                  reset_cwd=pl.root)[0]["rule"] == "path"
    # role_rules passes the prompt file (extra_allowed) and the persisted outputs (exact_allowed)
    assert pl.rules(sh("cat %s" % one), exact_allowed=exact) == []
    assert pl.rules(sh("cat %s" % one), extra_allowed=(one,)) == []
    assert pl.rules(sh("cat %s" % other), exact_allowed=exact)


def test_an_operand_that_is_a_folder_is_refused_unless_the_program_reads_folders(pl):
    for text in ("cat tree", "head tree", "tail tree", "wc tree", "sha256sum tree", "grep x tree", "diff tree tree",
                 "cmp tree tree", "sort tree", "cut -f1 tree", "nl tree", "tac tree", "sed -n p tree",
                 "uniq tree", "paste tree", "comm tree tree", "cat packet", "cat out", "grep -n x packet",
                 "echo x | sed p tree", "cat -n tree"):
        assert rule(pl, text) == "folder", text
    for text in ("ls tree", "ls -R tree", "stat tree", "file tree", "test -d tree", "test -e tree",
                 "grep -r x tree", "grep -rn x tree packet", "rg x tree", "rg -n x tree packet", "ls out packet",
                 "rg --files tree", "ls ."):
        assert ok(pl, text), text
    assert rule(pl, "grep -r x tree/sib") == "link"
    assert ok(pl, "grep -r x tree")     # a link inside a folder is not followed by `-r` and `rg`


def test_test_places_the_file_of_its_operators_and_both_sides_of_the_comparisons(pl):
    assert ok(pl, "test -f packet/brief.json")
    assert ok(pl, "test -s packet/brief.json && echo yes")
    assert ok(pl, "test packet/brief.json -nt out/a.txt")
    assert ok(pl, "test packet/brief.json -ot out/a.txt")
    assert ok(pl, "test packet/brief.json -ef packet/brief.json")
    assert ok(pl, "test x = x")
    assert ok(pl, "test 1 -eq 1")
    assert ok(pl, "test -n abc")
    assert rule(pl, "test -f /etc/hostname") == "path"
    assert rule(pl, "test -e ../x") == "dotdot"
    assert rule(pl, "test /etc/hostname -nt packet/brief.json") == "path"
    assert rule(pl, "test packet/brief.json -ef /etc/hostname") == "path"
    assert rule(pl, "test -L out/lf") == "link"
    assert chk(pl, "test -f CLAUDE.md")["rule"] == "path"
    assert chk(pl, "test CLAUDE.md -nt board.json")["rule"] == "path"
    assert rule(pl, "[ -f packet/brief.json ]") == "glob"
    assert rule(pl, "[[ -f packet/brief.json ]]") == "glob"


#: t-296 (t-228 minor 11a; owner rule dec-6939efd4f97c): `test` is read by its word count, so any
#: operator the two shapes do not hold, `-a` and `-o` among them, and any other count are refused with
#: the rule `test`, whose words give the `-e` form; nothing after an unknown word reaches a path
#: unplaced. These take over the three xfail cases of
#: .alpaca/proofs/op-009/t-228.logs/attack-set/test_t228_adversarial_s3.py:293-312.
TEST_REFUSED = ("test -a /etc/hostname", "test -a ../x", "test -a ../tf/../../x", "test -a packet/brief.json",
                "test -foo /etc/hostname", "test -e packet/brief.json -a -a /etc/hostname",
                "test -e packet/brief.json -o -a /etc/hostname",
                "test -e packet/brief.json -o -foo /etc/hostname", "test -a -a /etc/hostname",
                "test -e packet/brief.json -a -e packet/brief.json", "test -t 1", "test x", "test",
                "test -a /etc/hostname && echo yes")


def test_test_refuses_every_operator_and_count_outside_its_two_shapes(pl):
    for text in TEST_REFUSED:
        assert rule(pl, text) == "test", text
    for text in ("test -z ''", "test -n ''", "test '' = ''", "test -d out", "test -e packet/brief.json || echo no",
                 "test -e packet/brief.json && test -s packet/brief.json", "test 1 -ne 2", "test a '!=' b"):
        assert ok(pl, text), text


def test_the_test_rewrite_in_the_deny_text_is_admitted(gw, pl):
    gw.job("a84427dcadd1ac87b", cwd=pl.folder)
    why = gw.denies("a84427dcadd1ac87b", dict(sh(pl.cd("test -a /etc/hostname")), id="toolu_ta"))
    assert why and why.startswith("VERIFY-RULE test:"), why
    assert "`test -e packet/brief.json`" in why and verifier.RULE_WORDS["test"] in why
    assert ok(pl, "test -e packet/brief.json") and ok(pl, "test -e packet/brief.json && test -s packet/brief.json")


def test_test_taken_out_lets_an_unknown_operator_place_nothing(pl, monkeypatch):
    # mutation: the reader of main (place after a listed operator, skip any other word) admits the
    # escapes; the shape reader refuses them
    def old(self, args):
        for i, a in enumerate(args):
            if a in pathscan._G_TEST_UNARY and i + 1 < len(args):
                self.place(args[i + 1])
            elif a in pathscan._G_TEST_BINARY:
                if i >= 1:
                    self.place(args[i - 1])
                if i + 1 < len(args):
                    self.place(args[i + 1])
    monkeypatch.setattr(pathscan._GrammarRun, "test", old)
    assert ok(pl, "test -a /etc/hostname") and ok(pl, "test -foo /etc/hostname")


def test_the_other_programs_place_their_files_and_leave_their_text_alone(pl):
    assert ok(pl, "sed -n '10,40p' packet/brief.json")
    assert ok(pl, "sed -e p -e p packet/brief.json")
    assert ok(pl, "tr a b < packet/brief.json")
    assert ok(pl, "tr -d '\\n' < packet/brief.json | cut -d, -f1")
    assert ok(pl, "echo /etc/hostname ../x '/dev/tcp/x/80' '~'")        # echo and printf open no file
    assert ok(pl, "printf '%s\\n' /etc/hostname ../x")
    assert ok(pl, "basename /etc/hostname")
    assert ok(pl, "dirname /etc/hostname")
    assert ok(pl, "seq 1 3")
    assert ok(pl, "true")
    assert ok(pl, "false")
    assert ok(pl, "pwd")
    assert ok(pl, "grep -n -A 2 '/etc/hostname' packet/brief.json")   # a pattern is no path
    assert ok(pl, "grep -e '../x' -e '/etc/x' packet/brief.json")
    assert rule(pl, "printf -v x y") == "option"
    assert ok(pl, "printf -- '%s' x")
    assert rule(pl, "true x") == "operand"
    assert rule(pl, "pwd x") == "operand"
    assert rule(pl, "uniq packet/brief.json out/a.txt") == "operand"
    assert rule(pl, "sed") == "operand"
    assert rule(pl, "grep") == "operand"
    assert rule(pl, "grep -n") == "operand"
    assert rule(pl, "rg -n") == "operand"
    assert ok(pl, "rg -e x tree")
    assert chk(pl, pl.cd("rg --files /etc"))["rule"] == "path"
    assert chk(pl, pl.cd("grep -r x /etc"))["rule"] == "path"


def test_a_sed_script_is_read_by_the_script_rules_of_pathscan(pl):
    for script in ("w out/x", "1w out/x", "s/a/b/w out/x", "e id", "s/a/b/e", "r /etc/hostname", "R x", "W x",
                   "v", "s/a/b/q"):
        text = "sed -n '%s' packet/brief.json" % script
        got = chk(pl, pl.cd(text))
        assert got and got["rule"] == "sed", text
        got = chk(pl, pl.cd("sed -e '%s' packet/brief.json" % script))
        assert got and got["rule"] == "sed", script
    assert ok(pl, "sed -n '1,5p;$d' packet/brief.json")
    assert ok(pl, "sed 's/a/b/g' packet/brief.json")
    assert ok(pl, "sed -n '$p' packet/brief.json")
    assert ok(pl, "sed -s -n '/x/,/y/p' packet/brief.json out/a.txt")
    # the script is a word of the call: the same script out of a loop is read in its place
    assert rule(pl, "for s in w out/x; do sed -n $s packet/brief.json; done") in ("sed", "operand", "option")


# ================================================================ 19.7 redirects
def test_input_redirects_read_a_path_of_the_try_folder_and_output_redirects_write_into_out(pl):
    assert ok(pl, "cat < packet/brief.json")
    assert ok(pl, "cat 0< packet/brief.json")
    assert ok(pl, "echo x > out/new.txt")
    assert ok(pl, "echo x >> out/new.txt")
    assert ok(pl, "echo x 1> out/new.txt 2> out/err.txt")
    assert ok(pl, "echo x > out/sub.txt 2>&1")
    assert ok(pl, "echo x > /dev/null")
    assert ok(pl, "echo x >> /dev/null 2>&1")
    assert rule(pl, "cat < /etc/hostname") == "path"
    assert rule(pl, "cat < ../x") == "dotdot"
    assert rule(pl, "echo x > packet/x") == "write"
    assert rule(pl, "echo x > tree/x") == "write"
    assert rule(pl, "echo x > out") == "write"
    assert rule(pl, "echo x > receipts/x") == "write"
    assert rule(pl, "echo x > x") == "write"
    assert rule(pl, "echo x > /tmp/x") == "path"
    assert rule(pl, "echo x > ../x") == "dotdot"
    assert rule(pl, "echo x > out/../packet/x") == "dotdot"
    assert rule(pl, "echo x > out/l") == "link"           # out/l is a link to the project root
    assert rule(pl, "echo x > out/lf") == "link"          # a link to a file of the root
    assert rule(pl, "echo x >> out/lf") == "link"
    assert rule(pl, "echo x > out/lf/x") == "link"
    assert rule(pl, "echo x 2> packet/x") == "write"
    assert rule(pl, "ls > out/*") == "glob"
    assert rule(pl, "ls > out/l*") == "glob"
    assert rule(pl, "ls > out/l*/x") == "glob"
    assert rule(pl, "ls > $x") == "expansion"
    assert rule(pl, "for f in a; do ls > $f; done") == "write"
    assert ok(pl, "for f in a b; do ls > out/$f.txt; done")
    # the redirect of a command that is refused changes nothing: the command word is read first
    assert rule(pl, "git status > out/x") == "program"


def test_a_link_in_out_is_read_by_its_real_path_and_a_regular_file_is_not(pl):
    with open(os.path.join(pl.folder, "out", "ok.txt"), "w", encoding="utf-8") as fh:
        fh.write("x")
    assert ok(pl, "echo y > out/ok.txt")
    os.symlink(os.path.join(pl.folder, "tree"), os.path.join(pl.folder, "out", "intree"))
    assert rule(pl, "echo y > out/intree/x") == "write"          # a link to `tree/`: inside the folder, not out/
    os.symlink(os.path.join(pl.folder, "out", "ok.txt"), os.path.join(pl.folder, "out", "inout"))
    assert ok(pl, "echo y > out/inout")                          # a link from out/ to out/ stays in out/


# ================================================================ 19.8 the deny text and the rewrites
#: one call for each rule of the grammar, written after `cd <try folder> &&` unless the rule is about
#: the call's first command
RULE_CALLS = {
    "glob": "cat packet/*.json", "expansion": "echo $HOME", "backquote": "echo `id`", "brace": "echo {a,b}",
    "tilde": "cat ~/x", "backslash": "echo a\\ b", "paren": "(ls)", "bang": "echo !x",
    "comment": "echo a # b", "character": "echo a\rb", "background": "ls &", "redirect": "cat <<< x",
    "heredoc": "cat <<'EOF'\nx\nEOF", "unreadable": "echo 'x", "too-long": "; ".join(["echo x"] * 257),
    "program": "git status", "command-path": "/usr/bin/cat packet/brief.json", "command-word": "'cat' x",
    "assignment": "P=1", "reserved": "if true; then ls; fi", "option": "ls -z", "operand": "true x",
    "folder": "cat tree", "loop": "for IFS in a; do true; done", "cd": "cd tree", "path": "cat /etc/hostname",
    "dotdot": "cat tree/../packet/brief.json", "link": "cat out/lf", "write": "echo x > packet/x",
    "client": "python3 -c 1", "sed": "sed -n 'w out/x' packet/brief.json", "test": "test -a /etc/hostname",
}


def test_every_rule_of_the_grammar_has_a_call_that_the_grammar_names_with_it(pl):
    from alpaca.tests.test_research_prompt_rules import _grammar_rule_names
    assert set(RULE_CALLS) == _grammar_rule_names()
    for name, text in RULE_CALLS.items():
        assert rule(pl, text) == name, (name, text)


def test_the_guard_denies_each_rule_with_its_name_and_its_words(gw, pl):
    """19.8.1: the guard's text starts with `VERIFY-RULE <rule>:` and holds RULE_WORDS[rule], and the deny
    event names the rule."""
    agents = list(kit.AGENTS)
    n = 0
    for i, (name, text) in enumerate(RULE_CALLS.items()):
        agent = agents[(i // 8) % len(agents)]
        if agent not in gw.jobs or i % 8 == 0:
            gw.job(agent, cwd=pl.folder)
        call = dict(sh(pl.cd(text)), id="toolu_rule_%d" % i)
        why = gw.denies(agent, call)
        assert why and why.startswith("VERIFY-RULE %s:" % name), (name, why)
        assert verifier.RULE_WORDS[name] in why, name
        n += 1
    assert n == len(RULE_CALLS)
    events_ = gw.deny_events()
    assert [e["rule"] for e in events_] == list(RULE_CALLS)


def test_the_deny_text_of_an_option_lists_the_row_and_the_words_name_the_table(gw, pl):
    gw.job("a84427dcadd1ac87b", cwd=pl.folder)
    why = gw.denies("a84427dcadd1ac87b", dict(sh(pl.cd("ls -z")), id="toolu_opt"))
    assert why.startswith("VERIFY-RULE option: -z is not an option of ls: ls takes only these options")
    assert pathscan._g_row_options(pathscan.VERIFIER_TABLE["ls"]) in why
    assert verifier.RULE_WORDS["option"] in why


def test_the_two_listing_form_of_the_folder_words_passes_and_diff_r_is_denied_with_it(gw, pl):
    gw.job("a84427dcadd1ac87b", cwd=pl.folder)
    for i, text in enumerate(("diff -rq tree base", "diff -qr tree base", "diff --recursive tree base",
                              "diff -rq --exclude=.git tree base", "diff --exclude=.git -rq tree base")):
        why = gw.denies("a84427dcadd1ac87b", dict(sh(pl.cd(text)), id="toolu_d%d" % i))
        assert why and why.startswith("VERIFY-RULE folder:"), (text, why)
        assert "list both" in why and "ls -R tree/x > out/t.txt" in why
    assert ok(pl, "ls -R tree > out/t.txt; ls -R base > out/b.txt; diff out/t.txt out/b.txt")


# ---- the honest calls the grammar denies, each with its rewrite (19.8.2, 19.8.3)
#: the shapes the 24 counted verifier transcripts of 19.12 held, rebuilt with synthetic names: (the call
#: after the cd, the rule that denies it, the rewrite the deny text gives)
HONEST_DENIED = [
    ("ls -R gates; for f in gates/*/*.log; do echo \"== $f\"; cat \"$f\"; done", "glob",
     "ls -R gates; for f in 01-a/v-1.log 02-b/v-1.log; do echo \"== $f\"; cat gates/$f; done"),
    ("cat packet/other_out/*.json", "glob", "cat packet/other_out/a.json packet/other_out/b.json"),
    ("sha256sum packet/evidence/*", "glob", "sha256sum packet/evidence/01-a.log packet/evidence/02-b.log"),
    ("for f in packet/evidence/0[1-4]*.log; do cat $f; done", "glob",
     "for f in 01-a.log 02-b.log; do cat packet/evidence/$f; done"),
    ("grep -H '\"match\"' receipts/h-e*.json", "glob",
     "grep -r -H --include='h-e*.json' '\"match\"' receipts"),
    ("grep -rn --include=*.py 'x' tree/alpaca", "glob", "grep -rn --include='*.py' 'x' tree/alpaca"),
    ("grep -rn \"events.ATTEMPT\" tree/alpaca/research/*.py", "glob",
     "grep -rn --include='*.py' \"events.ATTEMPT\" tree/alpaca/research"),
    ("wc -l out/fp-*.txt", "glob", "wc -l out/fp-open.txt out/fp-eval.txt out/fp-close.txt"),
    ("grep -E 'x' receipts/apply.json; cd tree && echo; cd ..; ls out", "cd",
     "grep -E 'x' receipts/apply.json; ls tree; ls out"),
    ("diff -rq tree base > out/tree-base.txt", "folder",
     "ls -R tree > out/t.txt; ls -R base > out/b.txt; diff out/t.txt out/b.txt > out/tree-base.txt"),
    ("for n in a b; do grep -o -E '\"[^\"]*\":\"[^\"]*\"' packet/evidence/$n.json > out/fp-$n.txt; done "
     "&& wc -l out/fp-*.txt", "glob",
     "for n in a b; do grep -o -E '\"[^\"]*\":\"[^\"]*\"' packet/evidence/$n.json > out/fp-$n.txt; done "
     "&& wc -l out/fp-a.txt out/fp-b.txt"),
    ("diff -rq --exclude=.git tree base", "folder", "ls -R tree > out/t.txt; ls -R base > out/b.txt; diff out/t.txt out/b.txt"),
    ("[ -s packet/brief.json ]", "glob", "test -s packet/brief.json"),
    ("cat <<< hello", "redirect", "echo hello | cat"),
    ("/usr/bin/cat packet/brief.json", "command-path", "cat packet/brief.json"),
    ("python3 -c 1", "client", "python3 packet/bin/rerun ev-1 --shell 'python3 -c 1'"),
    ("echo $(ls tree)", "expansion", "ls tree"),
    ("R=ev-1; python3 packet/bin/rerun $R --event packet/events/1.json", "assignment",
     "python3 packet/bin/rerun ev-1 --event packet/events/1.json"),
    ("sed -n \"$(grep -n x packet/brief.json),+45p\" packet/brief.json", "expansion",
     "grep -n -A 45 x packet/brief.json"),
]


@pytest.mark.parametrize("text,expect,rewrite", HONEST_DENIED, ids=["%s-%d" % (r, i) for i, (_t, r, _w) in
                                                                      enumerate(HONEST_DENIED)])
def test_an_honest_shape_the_grammar_denies_is_named_with_its_rule_and_the_rewrite_passes(pl, text, expect, rewrite):
    for sub in ("gates/01-a", "gates/02-b"):
        os.makedirs(os.path.join(pl.folder, sub), exist_ok=True)
    assert rule(pl, text) == expect, text
    assert pl.rules(sh(pl.cd(text))), text
    assert pl.breaches(sh(pl.cd(text))), text
    assert rule(pl, rewrite) is None, rewrite
    assert pl.rules(sh(pl.cd(rewrite))) == [], rewrite
    assert pl.breaches(sh(pl.cd(rewrite))) == [], rewrite


# the forms the task names pass: every `cd <TRY> && ...` start, the rerun loop, the submissions, the
# descriptors, the output into out/, the input from the folder, `echo "rc=$?"`, quoted patterns that
# hold `*`, `[`, `$` and `\|`, and `sed -n 'A,Bp' FILE`
HONEST_PASSING = [
    "ls", "ls -la packet", "cat packet/brief.json", "head -n 20 packet/brief.json", "tail -n 20 packet/brief.json",
    "wc -l packet/brief.json out/a.txt", "grep -n 'def x' packet/brief.json", "grep -rn 'x' tree",
    "grep -rn --include='*.py' 'x' tree", "grep -r -H --include='h-*.json' '\"match\"' receipts",
    "grep -n -A 45 'pattern' packet/brief.json", "grep -c 'a\\|b' packet/brief.json",
    "grep -n \"\\\"any\\\"\\|requires.*any\\|\\`any\\`\" packet/brief.json | head -40",
    "rg -n 'x\\[0\\]' tree", "rg -n -g '*.py' x tree", "sed -n '10,40p' packet/brief.json",
    "sed 's/a/b/' packet/brief.json", "sort packet/brief.json | uniq -c | sort -rn | head -5",
    "cut -d: -f1 out/a.txt | nl | tac | head -3", "tr ',' '\\n' < packet/brief.json > out/t.txt",
    "cat packet/brief.json 2>/dev/null", "cat packet/brief.json 2>&1 | head -3",
    "echo hi > out/x.txt", "echo \"rc=$?\"", "python3 packet/bin/rerun apply --apply",
    "python3 packet/bin/rerun ev-1 --event packet/events/1.json; echo \"client-exit=$?\"; cat receipts/ev-1.json",
    "python3 packet/bin/rerun sh-1 --shell 'python3 -m pytest -q tests/x.py'",
    "python3 packet/bin/rerun --gate unit --side tree",
    "for g in build source unit metric:total_bytes; do python3 packet/bin/rerun --gate $g --side tree; done",
    "for e in 83131 83041; do python3 packet/bin/rerun ev-$e --event packet/events/$e.json; done",
    "for f in a.txt b.txt; do echo \"== $f\"; cat out/$f; done",
    "for r in hash-e1 hash-e2; do printf '%s ' \"$r\"; grep -o '\"match\": [a-z]*' receipts/$r.json "
    "| tr -d '\\n'; echo; done",
    "diff -u out/a.txt out/b.txt", "cmp out/a.txt out/b.txt", "test -s packet/brief.json && echo ok",
    "stat -c %s packet/brief.json", "file packet/brief.json", "sha256sum packet/brief.json",
    "ls tree | head -5; ls out", "ls -R tree > out/t.txt; ls -R base > out/b.txt; diff out/t.txt out/b.txt",
    SUBMIT,
]


@pytest.mark.parametrize("text", HONEST_PASSING)
def test_the_honest_forms_the_verifiers_used_pass_the_grammar_the_guard_and_the_judge(pl, text):
    assert rule(pl, text) is None, text
    assert pl.rules(sh(pl.cd(text))) == [], text
    assert pl.breaches(sh(pl.cd(text))) == [], text


# ================================================================ 19.9 one rule source
def _all_cases(pl) -> list:
    out = [(pl.cd(t), None) for t in HONEST_PASSING]
    out += [(pl.cd(t), r) for t, r, _w in HONEST_DENIED]
    out += [(pl.cd(t), r) for r, t in RULE_CALLS.items()]
    return out


def test_the_guard_the_judge_and_the_grammar_name_the_same_calls(gw, pl):
    """Over every case above, the calls the guard denies are the calls `_breaches` names are the calls the
    grammar refuses, and the deny event's rule is the grammar's rule."""
    for sub in ("gates/01-a", "gates/02-b"):
        os.makedirs(os.path.join(pl.folder, sub), exist_ok=True)
    agents = list(kit.AGENTS)
    denied_n, i = 0, 0
    for k, (text, expect) in enumerate(_all_cases(pl)):
        refused = chk(pl, text)
        named = pl.breaches(sh(text))
        allow_named = [p for p in named if p["rule"] == "allow-list"]
        agent = agents[(denied_n // 40) % len(agents)]
        if agent not in gw.jobs or (denied_n % 40 == 0 and denied_n and i != denied_n):
            gw.job(agent, cwd=pl.folder)
            i = denied_n
        why = gw.denies(agent, dict(sh(text), id="toolu_one_%d" % k))
        if expect is None:
            assert refused is None and not named and why is None, text
        else:
            assert refused is not None and refused["rule"] == expect, (text, refused)
            assert allow_named and why and why.startswith("VERIFY-RULE %s:" % expect), (text, why)
            denied_n += 1
    assert denied_n >= 50
    for ev in gw.deny_events():
        assert ev["rule"] in verifier.RULE_WORDS and ev["rule"] not in ("tool", "deny-cap")


def test_a_codex_call_is_read_as_the_text_it_runs_and_the_wrapper_is_no_program(pl):
    inner = pl.cd("cat packet/brief.json")
    wrapped = "bash -lc '%s'" % inner
    assert pathscan.shell_allowed([sh(wrapped)], allow=GRAMMAR, try_folder=pl.folder,
                                  client=os.path.join(pl.folder, "packet", "bin", "rerun"),
                                  reset_cwd=pl.root) == []
    bad = "bash -lc '%s'" % pl.cd("cat /etc/hostname")
    got = pathscan.shell_allowed([sh(bad)], allow=GRAMMAR, try_folder=pl.folder,
                                 client=os.path.join(pl.folder, "packet", "bin", "rerun"), reset_cwd=pl.root)
    assert got and got[0]["rule"] == "path"
    # a wrapper that holds more than one script, or a call after the wrapper, is a program `bash`
    got = pathscan.shell_allowed([sh("bash -c 'ls' && ls")], allow=GRAMMAR, try_folder=pl.folder,
                                 client=os.path.join(pl.folder, "packet", "bin", "rerun"), reset_cwd=pl.root)
    assert got and got[0]["rule"] == "program"


def test_the_auditor_keeps_section_18_and_the_verifier_does_not(pl):
    from alpaca.research import audit
    # section 18 lets the auditor write a heredoc to out/ and a path of /usr/bin; the verifier's grammar does not
    text = pl.cd("/usr/bin/cat packet/brief.json")
    assert audit.role_rules(pl.folder, [sh(text)], shell_cwd=pl.root, runtime_tools=at.RUNTIME_TOOLS) == []
    assert rule(pl, "/usr/bin/cat packet/brief.json") == "command-path"
    text = pl.cd("cat > out/audit.json <<'AUDIT'\n{}\nAUDIT")
    assert audit.role_rules(pl.folder, [sh(text)], shell_cwd=pl.root, runtime_tools=at.RUNTIME_TOOLS) == []
    assert rule(pl, "cat > out/audit.json <<'AUDIT'\n{}\nAUDIT") == "heredoc"
    # a plain tuple of names is read as section 18 reads it, a Grammar as the grammar
    names = tuple(GRAMMAR)
    assert not isinstance(names, pathscan.Grammar)
    plain = pathscan.shell_allowed([sh(pl.cd("/usr/bin/cat packet/brief.json"))], allow=names,
                                   try_folder=pl.folder, client=os.path.join(pl.folder, "packet", "bin", "rerun"),
                                   reset_cwd=pl.root)
    assert plain == []
    assert audit.RULE_WORDS_18 == audit.RULE_WORDS


# a wrapper whose script word the outer shell expands is no Codex call: the outer shell runs the
# expansion before the inner shell reads the script (t-228 round s2, adversarial lens)
def _live_wrappers(pl) -> list:
    v1, v2 = pl.folder, pl.other
    return [
        'bash -c "cd %s && echo \'$(echo refuted > %s/out/f)\'"' % (v1, v2),
        'bash -lc "cd %s && echo \'$(echo refuted > %s/receipts/verdict.json)\'"' % (v1, v2),
        'bash -c "cd %s && cat \'$(echo %s)/receipts/verdict.json\'"' % (v1, v2),
        'bash -c "cd %s && cat \'$HOME/.bashrc\'"' % v1,
        'bash -c "cd %s && cat \'$OLDPWD\'/CLAUDE.md"' % v1,
        'bash -c "cd %s && echo \'`id`\'"' % v1,
        'bash -c "cd %s && cat \'${HOME}/x\'"' % v1,
        "sh -c \"cd %s && cat '$HOME/x'\"" % v1,
        'bash -c "bash -c \\"cd %s && echo \'\\$(id)\'\\""' % v1,
    ]


def test_a_wrapper_whose_script_the_outer_shell_expands_is_refused(gw, pl):
    for text in _live_wrappers(pl):
        got = pathscan.shell_allowed([sh(text)], allow=GRAMMAR, try_folder=pl.folder,
                                     client=os.path.join(pl.folder, "packet", "bin", "rerun"),
                                     reset_cwd=pl.root)
        assert got and got[0]["rule"] in ("expansion", "backquote", "program"), text
        assert [h for h in pl.named(text) if h.get("rule") == "allow-list"], text
    # the single-quoted Codex form of the same inner text still passes, and so does a
    # double-quoted wrapper whose script holds no expansion
    inner = pl.cd("cat packet/brief.json")
    for text in ("bash -lc '%s'" % inner, 'bash -c "%s"' % inner, "bash -lc '%s'" % pl.cd("echo hi")):
        got = pathscan.shell_allowed([sh(text)], allow=GRAMMAR, try_folder=pl.folder,
                                     client=os.path.join(pl.folder, "packet", "bin", "rerun"),
                                     reset_cwd=pl.root)
        assert got == [], text


def test_the_unwrap_of_a_wrapper_with_live_positions_is_the_rule_that_refuses_it(pl, monkeypatch):
    """Mutation: with the literal-script condition taken out the double-quoted wrapper is unwrapped
    again and the grammar passes the single-quoted `$(...)` inside it."""
    text = 'bash -c "cd %s && echo \'$(id)\'"' % pl.folder
    assert pathscan._unwrapped_text(text) == text
    assert rule(pl, text, cd=False) in ("expansion", "backquote", "program")
    monkeypatch.setattr(pathscan, "_literal_script", lambda parsed, cmd: True)
    assert pathscan._unwrapped_text(text) == "cd %s && echo '$(id)'" % pl.folder
    assert rule(pl, pathscan._unwrapped_text(text), cd=False) is None
