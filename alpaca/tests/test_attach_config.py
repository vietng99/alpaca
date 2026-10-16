"""The attach: block and its rules (build plan section 4; P1)."""
import json
import os
import stat
from pathlib import Path

import pytest

from alpaca import project as project_mod
from alpaca.attach import AttachError, config
from alpaca.attach.config import MISSING


def block(**over):
    base = {"repo": "app", "url": "https://github.com/example-org/QTRM64", "repository": "example-org/QTRM64",
            "upstream": "origin", "base": "main", "branch_prefix": "alpaca/",
            "gates": [{"name": "test", "command": ["make", "test"], "timeout": 120}]}
    base.update(over)
    return base


def put(root, value):
    cfg = project_mod.load(root)
    if value is MISSING:
        cfg.pop("attach", None)
    else:
        cfg["attach"] = value
    cfg.setdefault("name", "demo")
    project_mod.save(root, cfg)


def test_a_project_without_the_key_has_no_mode_and_no_errors(project):
    put(project, MISSING)
    assert config.raw(project) is MISSING and config.mode(project) is None and config.config_errors(project) == []


@pytest.mark.parametrize("value", [None, {}, [], "app", 7, {"url": "x"}])
def test_only_a_valid_mapping_with_a_repo_is_a_mode(project, value):
    put(project, value)
    assert config.mode(project) is None
    assert config.config_errors(project), "a present but invalid block is a doctor error, never silent"
    with pytest.raises(AttachError):
        config.load(project)


def test_a_valid_block_is_a_mode_and_loads_strictly(project):
    put(project, block())
    cfg = config.mode(project)
    assert cfg is not None and cfg.repo == "app" and cfg.gates[0]["timeout"] == 120
    assert config.load(project) == cfg
    assert str(cfg.repo_path(project)).endswith("/app")
    assert len(cfg.gate_set_digest()) == 64 and cfg.gate_set_digest() == cfg.gate_set_digest()


def test_defaults_fill_the_optional_keys(project):
    put(project, {"repo": "app", "url": "/some/where", "gates": [{"name": "t", "command": ["true"]}]})
    cfg = config.load(project)
    assert (cfg.upstream, cfg.base, cfg.branch_prefix, cfg.repository) == ("origin", "main", "alpaca/", None)
    assert cfg.gates[0]["timeout"] == config.DEFAULT_TIMEOUT


def test_the_gate_set_digest_follows_the_gates(project):
    put(project, block())
    first = config.load(project).gate_set_digest()
    put(project, block(gates=[{"name": "test", "command": ["make", "check"], "timeout": 120}]))
    assert config.load(project).gate_set_digest() != first


BAD = [
    ("repo", ".", "attach.repo"), ("repo", "..", "attach.repo"), ("repo", "/tmp/x", "attach.repo"),
    ("repo", ".alpaca", "attach.repo"), ("repo", "app/../app", "attach.repo"), ("repo", "a b", "attach.repo"),
    ("repo", "", "attach.repo"), ("repo", 3, "attach.repo"), ("repo", "alpaca", "owns"), ("repo", "docs", "owns"),
    ("url", "-oProxyCommand=x", "starts with"), ("url", "https://u:SECRET@h/x.git", "user info"),
    ("url", "ssh://git@h/x.git", "user info"), ("url", "x\ny", "control"), ("url", "", "non-empty"), ("url", 5, "non-empty"),
    ("repository", "no-slash", "owner/name"), ("repository", "a/b/c", "owner/name"), ("repository", 3, "owner/name"),
    ("upstream", "-x", "upstream"), ("upstream", "", "upstream"), ("upstream", "a b", "upstream"),
    ("base", "a..b", "base"), ("base", "x/", "base"), ("base", "x.lock", "base"), ("base", "", "base"), ("base", "-x", "base"),
    ("gates", [], "no attach gates"), ("gates", "make test", "no attach gates"), ("gates", [5], "not a mapping"),
]


@pytest.mark.parametrize("key,value,needle", BAD)
def test_every_rule_refuses_its_bad_value(project, key, value, needle):
    errs = config.validate(block(**{key: value}), project)
    assert any(needle in e for e in errs), errs
    assert "SECRET" not in " ".join(errs)


@pytest.mark.parametrize("prefix", ["", "m", "main", "alpaca", "../x/", "refs/heads/", "refs/", "heads/", "remotes/",
                                    "tags/", "Alpaca/", "al paca/", "x" * 40 + "/", 5])
def test_branch_prefix_list_is_refused(project, prefix):
    assert any("branch_prefix" in e for e in config.validate(block(branch_prefix=prefix), project))


def test_a_prefix_that_collides_with_the_base_is_refused(project):
    assert any("collides" in e for e in config.validate(block(base="main", branch_prefix="main/"), project))
    assert any("collides" in e for e in config.validate(block(base="alpaca/dev", branch_prefix="alpaca/"), project))
    assert config.validate(block(base="develop", branch_prefix="alpaca/"), project) == []


@pytest.mark.parametrize("url", ["git\x40github.com:example-org/QTRM64.git", "https://github.com/a/b", "/srv/git/x.git",
                                 "file:///srv/git/x.git", "../rel/path"])
def test_accepted_urls(project, url):
    assert config.validate(block(url=url), project) == []


def test_unknown_keys_are_refused(project):
    errs = config.validate(block(mode="fast"), project)
    assert "attach.mode is not a known key" in errs
    errs = config.validate(block(gates=[{"name": "t", "command": ["x"], "shell": True}]), project)
    assert any("shell is not a known key" in e for e in errs)


def test_gate_rules(project):
    def errs(gates):
        return config.validate(block(gates=gates), project)
    g = {"name": "t", "command": ["x"]}
    assert errs([g]) == []
    assert any("used twice" in e for e in errs([g, dict(g)]))
    assert any("name" in e for e in errs([{"name": "Bad", "command": ["x"]}]))
    assert any("release check" in e for e in errs([{"name": "leak-scan", "command": ["x"]}]))
    assert any("command" in e for e in errs([{"name": "t", "command": "make test"}]))
    assert any("command" in e for e in errs([{"name": "t", "command": []}]))
    assert any("command" in e for e in errs([{"name": "t", "command": ["ok", 3]}]))
    for bad in (True, 0, 7201, "9", 1.5):
        assert any("timeout" in e for e in errs([{"name": "t", "command": ["x"], "timeout": bad}])), bad
    assert errs([{"name": "t", "command": ["x"], "timeout": 7200}]) == []
    many = [{"name": "g%d" % i, "command": ["x"]} for i in range(config.MAX_GATES + 1)]
    assert any("limit" in e for e in errs(many))


def test_a_symlinked_repo_folder_is_refused(project, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    os.symlink(elsewhere, os.path.join(project, "app"))
    assert any("not a real folder" in e for e in config.validate(block(), project))


def test_guard_view_needs_a_block_or_state_and_tolerates_a_bad_block(project):
    """t-648 class review r2: the guard switches on with the `attach:` block (project.yaml) or state.json, so
    deleting the state file cannot turn it off."""
    put(project, MISSING)
    assert config.guard_view(project) is None
    put(project, block())
    assert config.guard_view(project) == (os.path.join(os.path.realpath(project), "app"), "alpaca/")      # block alone
    config.write_json_atomic(config.state_dir(project) / "state.json", {"schema": 1, "repo": "app"})
    assert config.guard_view(project) == (os.path.join(os.path.realpath(project), "app"), "alpaca/")
    put(project, {"repo": "../etc", "branch_prefix": 5})
    assert config.guard_view(project) == (os.path.join(os.path.realpath(project), "app"), "alpaca/")
    put(project, block(repo="code", branch_prefix="bot/"))
    assert config.guard_view(project) == (os.path.join(os.path.realpath(project), "code"), "bot/")
    put(project, MISSING)
    assert config.guard_view(project) == (os.path.join(os.path.realpath(project), "app"), "alpaca/")      # state alone


def test_write_json_atomic_is_private_and_leaves_no_temp_file(tmp_path):
    target = tmp_path / "deep" / "x.json"
    config.write_json_atomic(target, {"b": 1, "a": [2]}, root=tmp_path)
    assert json.loads(target.read_text()) == {"a": [2], "b": 1}
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert [p.name for p in target.parent.iterdir()] == ["x.json"]


def test_the_raw_branch_file_reader_reads_only_well_formed_ids_and_the_proved_one_needs_a_record(project):
    """`read_branch_file` is the report-only reader (the file is output). `task_branch` and `task_checkout`
    return only what the record and a derived folder prove (alpaca.attach.proven), so a hand-written file
    gives them nothing."""
    config.write_json_atomic(config.state_dir(project) / "branches" / "t-001.json", {"task": "t-001", "worktree": "/nope"})
    assert config.read_branch_file(project, "t-001")["task"] == "t-001"
    for bad in ("../t-001", "t-1/../x", "T-001", "", None, 7):
        assert config.read_branch_file(project, bad) is None
        assert config.task_branch(project, bad) is None
    assert config.task_branch(project, "t-001") is None and config.task_checkout(project, "t-001") is None
    wt = os.path.join(project, "wt")
    os.mkdir(wt)
    config.write_json_atomic(config.state_dir(project) / "branches" / "t-002.json", {"task": "t-002", "worktree": wt})
    assert config.task_checkout(project, "t-002") is None


def test_importing_the_config_module_reads_nothing(tmp_path):
    import subprocess, sys
    code = "import sys; import alpaca.attach.config; print(sorted(m for m in sys.modules if m.startswith('alpaca.release') or m == 'yaml'))"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=tmp_path,
                         env=dict(os.environ, PYTHONPATH=os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))).stdout
    assert out.strip() == "[]", "config must import lazily so a plain project pays nothing: " + out


@pytest.mark.parametrize("name", ["input", "openspec", "specs", "notes", "runbooks", "reports", "design", "Input", "OPENSPEC"])
def test_a_folder_alpaca_writes_into_is_not_a_valid_attach_repo(project, name):
    errs = config.validate(block(repo=name), str(project))
    assert errs and "owns or writes into" in errs[0], errs


def test_an_ordinary_folder_name_is_still_a_valid_attach_repo(project):
    for name in ("app", "product", "firmware", "src2"):
        assert config.validate(block(repo=name), str(project)) == []


def test_the_state_writer_refuses_to_write_through_a_linked_state_folder(project, tmp_path):
    project = Path(project)
    outside = tmp_path / "outside-state"
    outside.mkdir()
    (project / ".alpaca").mkdir(exist_ok=True)
    (project / ".alpaca" / "attach").symlink_to(outside)
    with pytest.raises(AttachError, match="is a link"):
        config.write_json_atomic(config.state_dir(str(project)) / "branches" / "t-1.json", {"a": 1})
    assert list(outside.iterdir()) == []


def test_the_state_writer_refuses_a_linked_sub_folder_and_writes_a_plain_path(project, tmp_path):
    outside = tmp_path / "outside-branches"
    outside.mkdir()
    project = Path(project)
    state = config.state_dir(str(project))
    state.mkdir(parents=True, exist_ok=True)
    (state / "branches").symlink_to(outside)
    with pytest.raises(AttachError, match="branches is a link"):
        config.write_json_atomic(state / "branches" / "t-1.json", {"a": 1})
    assert list(outside.iterdir()) == []
    config.write_json_atomic(state / "state.json", {"a": 1})
    assert json.loads((state / "state.json").read_text()) == {"a": 1}


def test_a_state_writer_path_under_a_linked_receipts_folder_is_refused(project, tmp_path):
    """S1 (think/cause-r1.md): the anchor is the studio, not only .alpaca/attach."""
    project = Path(project)
    outside = tmp_path / "outside-receipts"
    outside.mkdir()
    (project / ".alpaca" / "releases").mkdir(parents=True, exist_ok=True)
    (project / ".alpaca" / "releases" / "receipts").symlink_to(outside)
    with pytest.raises(AttachError, match="receipts is a link"):
        config.write_json_atomic(project / ".alpaca" / "releases" / "receipts" / "h.json", {"a": 1}, root=str(project))
    assert list(outside.iterdir()) == []


def test_a_path_with_no_state_folder_and_no_root_is_refused_and_writes_nothing(tmp_path):
    with pytest.raises(AttachError):
        config.write_json_atomic(tmp_path / "loose" / "x.json", {"a": 1})
    assert not (tmp_path / "loose").exists()


def test_the_state_writer_works_in_a_studio_reached_through_a_link_and_still_refuses_a_linked_folder(project, tmp_path):
    """The studio root spelled through a link: every write is checked from the real root (premortem PM-A)."""
    alias = tmp_path / "alias-studio"
    alias.symlink_to(project)
    outside = tmp_path / "outside-alias"
    outside.mkdir()
    state = config.state_dir(str(alias))
    config.write_json_atomic(state / "state.json", {"a": 1})
    assert json.loads((Path(project) / ".alpaca" / "attach" / "state.json").read_text()) == {"a": 1}
    (Path(project) / ".alpaca" / "attach" / "branches").symlink_to(outside)
    with pytest.raises(AttachError, match="branches is a link"):
        config.write_json_atomic(state / "branches" / "t-1.json", {"a": 1})
    assert list(outside.iterdir()) == []
    assert config.read_state(str(alias)) == {"a": 1}
