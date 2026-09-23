"""The outbound barrier covers everything a push carries, and its term list stays outside the tree.

A push carries more than blob bytes: path names, author and committer identities, commit messages,
ref names and annotated tag objects all leave with it, so each is scanned for the sealed terms.
The generic shape rules (a mailbox, a home path) fire on known fixtures in this tree, so narrow
allow rules in project.yaml (`barrier.allow`, `<regex>  # <reason>`) clear exactly those values:
a rule matches the WHOLE value, clears shape hits only, and never hides a sealed term. The term
list itself is supplied by each clone (project.yaml `barrier.terms` names a path under the
gitignored `.alpaca/`): when it is configured but absent the push is refused (review 3, B5), and
when the configuration cannot be read the push is refused.
"""
import json
import os
import subprocess

import pytest
import yaml

from alpaca import barrier
from alpaca.gates import verdict as vc
from alpaca.tests.conftest import REPO
from alpaca.tests.test_outbound_barrier import (  # noqa: F401  (fixtures used by name)
    TERM, _commit, _commits, _fixed_clock, _push_env, _run, repo)

# Values the barrier must REFUSE are built at run time (AT, HOME), so this file, which is pushed
# through the barrier itself, carries no shape the allow rules would have to clear.
AT = "@"
HOME = "/" + "home"
PLACEHOLDER = r"[a-z0-9._%+\-]+@(?:[a-z0-9\-]+\.)*(?:example|invalid|test)  # reserved domains"
DECORATOR = (r"[a-z0-9._%+\-]*@(?:pytest\.(?:fixture|mark\.[a-z_]+))[a-z0-9_]*  "
             r"# joined lane meets a decorator")


def _config(root, allow=None, terms=".alpaca/sealed-terms.txt", raw=None):
    p = os.path.join(root, "project.yaml")
    if raw is not None:
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(raw)
        return
    block = {"protected_paths": [".env", "secrets/"]}
    if terms:
        block["terms"] = terms
    if allow is not None:
        block["allow"] = allow
    with open(p, "w", encoding="utf-8") as fh:
        yaml.safe_dump({"name": "work", "tier": "public", "barrier": block}, fh)


def _scan(root):
    return barrier.scan(root, _commits(root))


# ------------------------------------------------------------------ allow rules
# The joined lane collapses whitespace, so a value is kept apart from the next word by a quote or a
# bracket, as the fixtures in this tree already are.
FIXTURE = "contact <t@example.invalid> for the demo\nx = 1\n@pytest.fixture\ndef f():\n    pass\n"


def test_shape_false_positives_fail_without_allow_rules(repo):
    root, _bare = repo
    _config(root)
    _commit(root, "tests/test_demo.py", FIXTURE, "add fixture")
    res = _scan(root)
    assert res.verdict == vc.FAIL and barrier.R_LEAK in res.reasons


def test_allow_rules_clear_the_known_shape_false_positives(repo):
    root, _bare = repo
    _config(root, allow=[PLACEHOLDER, DECORATOR])
    _commit(root, "tests/test_demo.py", FIXTURE, "add fixture")
    res = _scan(root)
    assert res.verdict == vc.PASS, barrier.render(res.report)


def test_an_allow_rule_never_hides_a_sealed_term_on_the_same_line(repo):
    root, _bare = repo
    _config(root, allow=[PLACEHOLDER, DECORATOR, r".*  # a rule that matches anything"])
    _commit(root, "notes.md", "write to <t@example.invalid> about " + TERM + "\n", "add notes")
    res = _scan(root)
    assert res.verdict == vc.FAIL and barrier.R_LEAK in res.reasons


def test_an_allow_rule_matches_the_whole_value_not_a_part_of_it(repo):
    root, _bare = repo
    _config(root, allow=[r"t@example\.invalid  # the one fixture address"])
    _commit(root, "notes.md", "mail <at" + AT + "example.invalid.mail.org> now\n", "add notes")
    res = _scan(root)
    assert res.verdict == vc.FAIL and barrier.R_LEAK in res.reasons


def test_an_allow_rule_without_a_reason_refuses_the_push(repo):
    root, _bare = repo
    _config(root, allow=[r"t@example\.invalid"])
    _commit(root, "src/app.py", "print('ok')\n", "add app")
    res = _scan(root)
    assert res.verdict == vc.BLOCKED and barrier.R_TERMS_INVALID in res.reasons


def test_a_malformed_allow_rule_refuses_the_push(repo):
    root, _bare = repo
    _config(root, allow=[r"([unclosed  # broken regex"])
    _commit(root, "src/app.py", "print('ok')\n", "add app")
    res = _scan(root)
    assert res.verdict == vc.BLOCKED and barrier.R_TERMS_INVALID in res.reasons


# ------------------------------------------------------------------ the term list and the config
def test_an_absent_term_list_refuses_and_says_the_terms_were_not_checked(repo):
    root, _bare = repo
    _config(root, terms=".alpaca/not-supplied.txt")
    _commit(root, "notes.md", "the value is " + TERM + "\n", "add notes")
    res = _scan(root)
    assert res.verdict == vc.BLOCKED
    assert barrier.R_TERMS_MISSING in res.reasons
    text = barrier.render(res.report)
    assert "NOT checked" in text and "absent" in text


def test_an_unreadable_project_yaml_refuses_the_push(repo):
    root, _bare = repo
    _commit(root, ".env", "SECRET=1\n", "add env")     # would be refused if the config were read
    _config(root, raw="barrier: [unclosed\n  protected_paths: - .env\n")
    res = _scan(root)
    assert res.verdict == vc.BLOCKED and barrier.R_CONFIG in res.reasons


# ------------------------------------------------------------------ what else a push carries
def test_a_sealed_term_in_a_path_name_fails(repo):
    root, _bare = repo
    _config(root)
    _commit(root, "docs/" + TERM.lower() + "-notes.md", "plain text\n", "add notes")
    res = _scan(root)
    assert res.verdict == vc.FAIL and barrier.R_LEAK in res.reasons
    assert TERM.lower() not in json.dumps(res.report).lower()


def test_a_sealed_term_in_a_commit_message_fails(repo):
    root, _bare = repo
    _config(root)
    _commit(root, "src/app.py", "print('ok')\n", "port the fix from " + TERM)
    res = _scan(root)
    assert res.verdict == vc.FAIL and barrier.R_LEAK in res.reasons
    assert any(b.get("where") == "commit-metadata" for b in res.report["blocked"])
    assert TERM not in barrier.render(res.report)


def test_a_sealed_term_in_the_author_identity_fails(repo):
    root, _bare = repo
    _config(root)
    _run(root, "config", "user.name", "dev " + TERM)
    _commit(root, "src/app.py", "print('ok')\n", "add app")
    res = _scan(root)
    assert res.verdict == vc.FAIL and barrier.R_LEAK in res.reasons


def test_commit_metadata_is_matched_on_terms_not_on_shapes(repo):
    root, _bare = repo
    _config(root)
    _commit(root, "src/app.py", "print('ok')\n",
            "add app\n\nCo-Authored-By: Tool <noreply" + AT + "tool.dev>\nSee " + HOME + "/dev/src")
    res = _scan(root)
    assert res.verdict == vc.PASS, barrier.render(res.report)


def _push(root, *args):
    return subprocess.run(["git", "-C", root, "push", "origin", *args], capture_output=True,
                          text=True, encoding="utf-8", env=_push_env(root))


def _remote_has(bare, ref):
    return _run(bare, "rev-parse", "--verify", "--quiet", ref).returncode == 0


def test_real_push_of_an_annotated_tag_carrying_a_term_is_refused(repo):
    root, bare = repo
    _config(root)
    barrier.install(root)
    _commit(root, "src/app.py", "print('ok')\n", "add app")
    assert _run(root, "tag", "-a", "v1", "-m", "release notes for " + TERM).returncode == 0
    r = _push(root, "v1")
    assert r.returncode != 0, r.stdout + r.stderr
    assert not _remote_has(bare, "refs/tags/v1")


def test_real_push_of_a_branch_named_with_a_term_is_refused(repo):
    root, bare = repo
    _config(root)
    barrier.install(root)
    _commit(root, "src/app.py", "print('ok')\n", "add app")
    r = _push(root, "HEAD:refs/heads/port-" + TERM.lower())
    assert r.returncode != 0, r.stdout + r.stderr
    assert not _remote_has(bare, "refs/heads/port-" + TERM.lower())
    r = _push(root, "HEAD:refs/heads/main")
    assert r.returncode == 0, r.stdout + r.stderr


def test_the_hook_prefers_the_install_launcher(repo):
    root, _bare = repo
    body = open(barrier.install(root), encoding="utf-8").read()
    assert 'exec bin/alpaca-python "$pin/run.py" prepush "$@"' in body
    assert body.rstrip().endswith('exec python3 "$pin/run.py" prepush "$@"')


def test_each_blob_is_read_once_however_many_commits_carry_it(repo, monkeypatch):
    root, _bare = repo
    _config(root)
    _commit(root, "big.txt", "same bytes\n", "one")
    for i in range(4):
        _commit(root, "n%d.txt" % i, "n%d\n" % i, "more %d" % i)
    reads = []
    real = barrier._Objects.read
    monkeypatch.setattr(barrier._Objects, "read",
                        lambda self, sha: reads.append(sha) or real(self, sha))
    res = _scan(root)
    assert res.verdict == vc.PASS
    big = _run(root, "rev-parse", "HEAD:big.txt").stdout.strip()
    assert big in reads and len(reads) == len(set(reads))


# ------------------------------------------------------------------ the shipped template
def _template_rules():
    from alpaca.gates import leak_audit
    with open(os.path.join(REPO, "project.yaml"), encoding="utf-8") as fh:
        block = yaml.safe_load(fh)["barrier"]
    rules, reasons, _detail = leak_audit.parse_shape_allow(block.get("allow"), "template")
    return block, rules, reasons


def test_the_template_names_a_clone_local_term_list_and_reasoned_allow_rules():
    block, rules, reasons = _template_rules()
    assert block["terms"].startswith(".alpaca/")          # gitignored and a protected path
    assert ".alpaca/" in block["protected_paths"]
    assert rules and not reasons and len(rules) == len(block["allow"])


@pytest.mark.parametrize("value, allowed", [
    ("t@example.invalid", True),
    ("pw@a.example.com", True),
    ("t@e.test", True),
    ("thefreshinstall@pytest.fixture", True),
    ("returnconn@pytest.fixturedefproj", True),
    ("sv.PROBE@pytest.mark.parametrize", True),
    ("andits@rebootweb-upscriptunder.alpaca", True),
    ("/home/owner/source", True),
    ("/home/secret/pathleaked", True),
    ("someone" + AT + "gmail.com", False),
    ("dev" + AT + "example.com.evil.org", False),
    (HOME + "/realuser/project", False),
    (HOME + "/ownerx/data", False),
])
def test_the_template_allow_rules_are_narrow(value, allowed):
    _block, rules, _reasons = _template_rules()
    assert any(rx.fullmatch(value) for rx, _r, _n in rules) is allowed
