"""t-008 re-review lows N1 to N4 (evidence/review3-t008/FINDINGS.md in the workspace that builds
this repository): intake names the rows another runbook id holds, a dry run reads the level a
waiver needs, --prepare moves a spec-kit feature added after the move, and the profile line edit
keeps the replaced key's own comments."""
import os
import shutil

import yaml

from alpaca.tests.test_intake import (  # noqa: F401  (fixtures used by name)
    LIVING, _cli, _counts, _edit, _op, _openspec, kit)


def _intake(kit, capsys, *extra, runbook=None):
    return _cli(["intake", kit["spec"], runbook or kit["runbook"], "--json"] + list(extra), capsys)


# ------------------------------------------------------------------ N1: a second runbook id
def test_n1_a_new_file_with_a_new_id_names_the_rows_the_old_id_holds(kit, capsys):
    _op(capsys)
    assert _intake(kit, capsys)[0] == 0
    new_rb = os.path.join(os.path.dirname(kit["runbook"]), "runbook-2.yaml")
    shutil.copy(kit["runbook"], new_rb)
    _edit(new_rb, "id: link-shortener", "id: link-shortener-2")
    rc, out = _intake(kit, capsys, "--dry-run", runbook=new_rb)
    assert rc == 0, out
    held = [w for w in out["warnings"] if w.startswith("KEY-HELD-BY-ANOTHER-RUNBOOK")]
    assert len(held) == 4, out["warnings"]
    assert all("link-shortener" in w and "withdraw" in w for w in held)


def test_n1_a_moved_file_with_a_new_id_is_named_too(kit, capsys):
    _op(capsys)
    assert _intake(kit, capsys)[0] == 0
    new_rb = os.path.join(os.path.dirname(kit["runbook"]), "rb.yaml")
    os.rename(kit["runbook"], new_rb)
    _edit(new_rb, "id: link-shortener", "id: link-shortener-2")
    rc, out = _intake(kit, capsys, runbook=new_rb)
    assert rc == 0, out
    assert any(w.startswith("KEY-HELD-BY-ANOTHER-RUNBOOK") for w in out["warnings"]), out["warnings"]


def test_n1_the_rename_refusal_says_the_old_rows_stay_open(kit, capsys):
    _op(capsys)
    assert _intake(kit, capsys)[0] == 0
    _edit(kit["runbook"], "id: link-shortener", "id: link-shortener-2")
    rc, out = _intake(kit, capsys)
    assert rc == 2
    assert "stay open" in out["reason"] and "by hand" in out["reason"], out["reason"]


def test_n1_the_docs_say_the_old_rows_stay_open():
    from alpaca.tests.conftest import REPO
    text = open(os.path.join(REPO, "docs", "intake.md"), encoding="utf-8").read()
    assert "stay open" in text and "KEY-HELD-BY-ANOTHER-RUNBOOK" in text


# ------------------------------------------------------------------ N2: the dry run reads the level
def test_n2_a_dry_run_refuses_a_level_the_real_run_would_refuse(project, capsys):
    _op(capsys)
    specs, rb = _openspec(project)
    assert _cli(["intake", specs, rb, "--json"], capsys)[0] == 0
    with open(os.path.join(project, "project.yaml"), "a", encoding="utf-8") as fh:
        fh.write("default_level: L7\n")
    living = LIVING.replace("""#### Scenario: invalid url
- **WHEN** a client posts a string that is not a URL
- **THEN** the service answers 400

""", "")
    specs, rb = _openspec(project, living, tests="Shorten/valid url")
    before = _counts(project)
    rc, out = _cli(["intake", specs, rb, "--dry-run", "--json"], capsys)
    assert rc == 2 and "level" in out["reason"], out
    assert _counts(project) == before


# ------------------------------------------------------------------ N3: features added after the move
def _feature(root, name, spec_path):
    feat = os.path.join(root, "specs", name)
    os.makedirs(feat)
    shutil.copy(spec_path, os.path.join(feat, "spec.md"))


def test_n3_a_speckit_feature_added_after_the_move_is_moved(kit, capsys):
    root = kit["root"]
    _feature(root, "001-links", kit["spec"])
    rc, out = _cli(["start", "a change", "--prepare", "--json"], capsys)
    assert rc == 0, out
    _feature(root, "002-stats", kit["spec"])
    rc, out = _cli(["start", "another change", "--prepare", "--json"], capsys)
    assert rc == 0, out
    assert os.path.isfile(os.path.join(root, "openspec", "specs", "stats", "spec.md"))
    assert any("specs/002-stats/spec.md" in d for d in out["prepared"]), out["prepared"]
    with open(os.path.join(root, "project.yaml"), encoding="utf-8") as fh:
        moved = yaml.safe_load(fh)["spec"]["moved_from"]["specs"]
    assert moved == ["specs/001-links/spec.md", "specs/002-stats/spec.md"]


def test_n3_a_moved_living_spec_that_was_deleted_is_named(kit, capsys):
    root = kit["root"]
    _feature(root, "001-links", kit["spec"])
    rc, out = _cli(["start", "a change", "--prepare", "--json"], capsys)
    assert rc == 0, out
    os.remove(os.path.join(root, "openspec", "specs", "links", "spec.md"))
    rc, out = _cli(["start", "another change", "--prepare", "--json"], capsys)
    assert rc == 0, out
    assert any("openspec/specs/links/spec.md" in d and "missing" in d for d in out["prepared"]), \
        out["prepared"]
    assert not os.path.exists(os.path.join(root, "openspec", "specs", "links", "spec.md"))


# ------------------------------------------------------------------ N4: the replaced key's comments
def _template(kit):
    from alpaca.tests.conftest import REPO
    path = os.path.join(kit["root"], "project.yaml")
    shutil.copy(os.path.join(REPO, "project.yaml"), path)
    return path


def test_n4_a_trailing_comment_on_the_profile_line_is_kept(kit, capsys):
    path = _template(kit)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("profile: ~   # set by a domain\n# trailing comment\n")
    _op(capsys)
    rc, out = _intake(kit, capsys)
    assert rc == 0, out
    text = open(path, encoding="utf-8").read()
    assert "profile: intake_profile   # set by a domain\n" in text and "# trailing comment" in text
    assert yaml.safe_load(text)["profile"] == "intake_profile"


def test_n4_an_indented_comment_under_an_empty_profile_is_kept(kit, capsys):
    path = _template(kit)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("profile:\n  # name a module here\nlast_key: 1\n")
    _op(capsys)
    rc, out = _intake(kit, capsys)
    assert rc == 0, out
    text = open(path, encoding="utf-8").read()
    assert "profile: intake_profile\n  # name a module here\nlast_key: 1\n" in text
    assert yaml.safe_load(text)["last_key"] == 1
