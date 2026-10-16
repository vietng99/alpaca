"""safefs: the one place attach writes (op-062 t-647, fix round 3)."""
import fcntl
import os
import stat
from pathlib import Path

import pytest

from alpaca.attach import AttachError, safefs


@pytest.fixture
def studio(tmp_path):
    root = tmp_path / "studio"
    root.mkdir()
    return root


@pytest.fixture
def outside(tmp_path):
    out = tmp_path / "outside"
    out.mkdir()
    return out


def names(folder):
    return sorted(p.name for p in Path(folder).iterdir())


def test_a_file_is_written_with_its_folders_and_the_exact_mode(studio):
    made = []
    safefs.write_bytes(studio, studio / "a" / "b" / "x.json", b"{}\n", mode=0o600, made=made)
    assert (studio / "a" / "b" / "x.json").read_bytes() == b"{}\n"
    assert stat.S_IMODE((studio / "a" / "b" / "x.json").stat().st_mode) == 0o600
    assert names(studio / "a" / "b") == ["x.json"]
    assert made == [str(studio.resolve() / "a"), str(studio.resolve() / "a" / "b")]


def test_a_relative_path_is_taken_below_the_root(studio):
    safefs.write_bytes(studio, "r/x.txt", b"x", mode=0o600)
    assert (studio / "r" / "x.txt").read_bytes() == b"x"


def test_a_link_at_a_middle_folder_is_refused_and_nothing_is_written(studio, outside):
    (studio / "mid").symlink_to(outside)
    with pytest.raises(AttachError, match="mid is a link"):
        safefs.write_bytes(studio, studio / "mid" / "deeper" / "x", b"x", mode=0o600)
    assert names(outside) == []


def test_a_link_at_the_leaf_is_refused_and_its_target_is_untouched(studio, outside):
    (outside / "victim").write_text("victim\n")
    (studio / "leaf").symlink_to(outside / "victim")
    with pytest.raises(AttachError, match="leaf is a link"):
        safefs.write_bytes(studio, studio / "leaf", b"new", mode=0o600)
    assert (outside / "victim").read_text() == "victim\n"
    assert names(studio) == ["leaf"]


def test_a_dangling_link_at_the_leaf_is_not_followed_to_create_a_file(studio, outside):
    (studio / "leaf").symlink_to(outside / "not-yet")
    with pytest.raises(AttachError, match="is a link"):
        safefs.write_bytes(studio, studio / "leaf", b"new", mode=0o600)
    assert names(outside) == []


def test_a_hard_linked_leaf_keeps_its_outside_bytes(studio, outside):
    (outside / "hard").write_text("hard\n")
    os.link(outside / "hard", studio / "h.json")
    safefs.write_bytes(studio, studio / "h.json", b"new\n", mode=0o600)
    assert (outside / "hard").read_text() == "hard\n"
    assert (studio / "h.json").read_text() == "new\n"
    assert (studio / "h.json").stat().st_ino != (outside / "hard").stat().st_ino


def test_a_file_where_a_folder_should_be_is_refused(studio):
    (studio / "a").write_text("file\n")
    with pytest.raises(AttachError, match="a is not a folder"):
        safefs.write_bytes(studio, studio / "a" / "x", b"x", mode=0o600)


def test_a_folder_at_the_leaf_is_not_replaced(studio):
    (studio / "d").mkdir()
    with pytest.raises(AttachError, match="not a plain file"):
        safefs.write_bytes(studio, studio / "d", b"x", mode=0o600)
    assert (studio / "d").is_dir()


@pytest.mark.parametrize("bad", ["../x", "a/../../x", "/etc/passwd-attach-test", "", "."])
def test_a_path_outside_the_studio_or_with_dots_is_refused(studio, bad):
    with pytest.raises(AttachError):
        safefs.write_bytes(studio, bad, b"x", mode=0o600)


def test_a_sibling_folder_that_shares_the_root_prefix_is_outside(studio, tmp_path):
    sibling = tmp_path / "studio-evil"
    sibling.mkdir()
    with pytest.raises(AttachError, match="outside the studio"):
        safefs.write_bytes(studio, sibling / "x", b"x", mode=0o600)
    assert names(sibling) == []


def test_create_only_refuses_an_existing_file_and_a_link(studio, outside):
    (studio / "t.txt").write_text("keep\n")
    with pytest.raises(AttachError, match="already exists"):
        safefs.write_bytes(studio, studio / "t.txt", b"x", mode=0o644, create_only=True)
    assert (studio / "t.txt").read_text() == "keep\n"
    (studio / "l.txt").symlink_to(outside / "x")
    with pytest.raises(AttachError, match="is a link"):
        safefs.write_bytes(studio, studio / "l.txt", b"x", mode=0o644, create_only=True)
    assert names(outside) == []


def test_made_lists_folders_shallowest_first_then_the_file(studio):
    made = []
    safefs.write_bytes(studio, studio / "seal" / "deep" / "terms.txt", b"t", mode=0o644, create_only=True, made=made)
    real = studio.resolve()
    assert made == [str(real / "seal"), str(real / "seal" / "deep"), str(real / "seal" / "deep" / "terms.txt")]


def test_made_holds_the_folder_when_the_file_open_fails_afterwards(studio, monkeypatch):
    real_open = os.open

    def failing(path, flags, *a, **k):
        if str(path).endswith("terms.txt"):
            raise PermissionError(13, "Permission denied", str(path))
        return real_open(path, flags, *a, **k)
    monkeypatch.setattr(os, "open", failing)
    made = []
    with pytest.raises(AttachError, match="cannot be written"):
        safefs.write_bytes(studio, studio / "seal" / "terms.txt", b"t", mode=0o644, create_only=True, made=made)
    assert made == [str(studio.resolve() / "seal")]


def test_a_failed_replace_leaves_no_temp_file(studio, monkeypatch):
    def boom(a, b):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(safefs.WriteFailed) as ei:
        safefs.write_bytes(studio, studio / "x.json", b"x", mode=0o600)
    assert ei.value.strerror == "No space left on device"
    assert names(studio) == []


@pytest.mark.parametrize("umask", [0o077, 0o000, 0o022])
def test_an_explicit_mode_is_exact_whatever_the_umask(studio, umask):
    old = os.umask(umask)
    try:
        safefs.write_bytes(studio, studio / "s.json", b"x", mode=0o600)
        safefs.write_bytes(studio, studio / "t.txt", b"x", mode=0o644, create_only=True)
    finally:
        os.umask(old)
    assert stat.S_IMODE((studio / "s.json").stat().st_mode) == 0o600
    assert stat.S_IMODE((studio / "t.txt").stat().st_mode) == 0o644


@pytest.mark.parametrize("umask", [0o077, 0o000, 0o022])
def test_a_new_file_without_a_mode_gets_644_less_the_umask(studio, umask):
    old = os.umask(umask)
    try:
        safefs.write_bytes(studio, studio / "new.txt", b"x")
    finally:
        os.umask(old)
    assert stat.S_IMODE((studio / "new.txt").stat().st_mode) == 0o644 & ~umask


def test_an_existing_file_keeps_its_mode_when_none_is_given(studio):
    path = studio / "keep.txt"
    path.write_text("old\n")
    path.chmod(0o640)
    safefs.write_bytes(studio, path, b"new\n")
    assert path.read_text() == "new\n" and stat.S_IMODE(path.stat().st_mode) == 0o640


def test_a_planted_link_at_a_temp_name_is_never_opened(studio, outside):
    (outside / "victim").write_text("victim\n")
    for guess in ("x.json.tmp", ".tmp-x.json", "x.json.alpaca-%d.tmp" % os.getpid()):
        (studio / guess).symlink_to(outside / "victim")
    (studio / "x.json").write_text("old\n")
    safefs.write_bytes(studio, studio / "x.json", b"new\n", mode=0o600)
    assert (outside / "victim").read_text() == "victim\n"
    assert (studio / "x.json").read_text() == "new\n" and not (studio / "x.json").is_symlink()


def test_the_post_create_check_refuses_a_parent_that_moved(studio, monkeypatch):
    (studio / "a").mkdir()
    real = os.path.realpath
    parent = str(studio.resolve() / "a")

    def moved(p, *a, **k):
        return "/somewhere/else" if str(p) == parent else real(p, *a, **k)
    monkeypatch.setattr(os.path, "realpath", moved)
    with pytest.raises(AttachError, match="is a link"):
        safefs.write_bytes(studio, studio / "a" / "x", b"x", mode=0o600)
    monkeypatch.undo()
    assert names(studio / "a") == []
    with pytest.raises(AttachError, match="is a link"):
        monkeypatch.setattr(os.path, "realpath", moved)
        safefs.write_bytes(studio, studio / "a" / "y", b"x", mode=0o644, create_only=True)
    monkeypatch.undo()
    assert names(studio / "a") == []


def test_make_dir_exclusive_is_a_claim(studio):
    made = []
    path = safefs.make_dir(studio, studio / "app", exclusive=True, made=made)
    assert path == str(studio.resolve() / "app") and (studio / "app").is_dir() and made == [path]
    with pytest.raises(FileExistsError):
        safefs.make_dir(studio, studio / "app", exclusive=True)


def test_make_dir_accepts_a_real_folder_and_refuses_a_link(studio, outside):
    (studio / "real").mkdir()
    assert safefs.make_dir(studio, studio / "real") == str(studio.resolve() / "real")
    (studio / "lnk").symlink_to(outside)
    with pytest.raises(AttachError, match="lnk is a link"):
        safefs.make_dir(studio, studio / "lnk")
    with pytest.raises(AttachError, match="lnk is a link"):
        safefs.make_dir(studio, studio / "lnk" / "sub", exclusive=True)
    assert names(outside) == []


def test_make_dir_exclusive_on_a_link_does_not_follow_it(studio, outside):
    (studio / "app").symlink_to(outside)
    with pytest.raises(FileExistsError):
        safefs.make_dir(studio, studio / "app", exclusive=True)
    assert names(outside) == []


def test_read_bytes_reads_a_plain_file_and_not_a_link(studio, outside):
    (studio / "f").write_bytes(b"plain")
    assert safefs.read_bytes(studio, studio / "f") == b"plain"
    (outside / "o").write_text("secret")
    (studio / "l").symlink_to(outside / "o")
    assert safefs.read_bytes(studio, studio / "l") is None
    (studio / "mid").symlink_to(outside)
    assert safefs.read_bytes(studio, studio / "mid" / "o") is None
    assert safefs.read_bytes(studio, studio / "missing" / "x") is None
    assert safefs.read_bytes(studio, studio / "..") is None
    (studio / "d").mkdir()
    assert safefs.read_bytes(studio, studio / "d") is None


def test_open_lock_gives_a_lockable_private_file_and_refuses_links(studio, outside):
    fd = safefs.open_lock(studio, studio / "locks" / "fetch.lock")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(fd)
    assert stat.S_IMODE((studio / "locks" / "fetch.lock").stat().st_mode) == 0o600
    (studio / "badlock").symlink_to(outside / "x.lock")
    with pytest.raises(AttachError, match="is a link"):
        safefs.open_lock(studio, studio / "badlock")
    (studio / "linked").symlink_to(outside)
    with pytest.raises(AttachError, match="linked is a link"):
        safefs.open_lock(studio, studio / "linked" / "y.lock")
    assert names(outside) == []


def test_a_studio_reached_through_a_link_writes_from_either_spelling(tmp_path, outside):
    real = tmp_path / "real-studio"
    (real / ".alpaca" / "attach").mkdir(parents=True)
    alias = tmp_path / "alias"
    alias.symlink_to(real)
    safefs.write_bytes(alias, alias / ".alpaca" / "attach" / "state.json", b"1", mode=0o600)
    safefs.write_bytes(alias, real / ".alpaca" / "attach" / "state2.json", b"2", mode=0o600)
    assert (real / ".alpaca" / "attach" / "state.json").read_bytes() == b"1"
    assert (real / ".alpaca" / "attach" / "state2.json").read_bytes() == b"2"
    (real / ".alpaca" / "attach" / "branches").symlink_to(outside)
    with pytest.raises(AttachError, match="branches is a link"):
        safefs.write_bytes(alias, alias / ".alpaca" / "attach" / "branches" / "t-1.json", b"3", mode=0o600)
    with pytest.raises(AttachError, match="branches is a link"):
        safefs.write_bytes(alias, real / ".alpaca" / "attach" / "branches" / "t-1.json", b"3", mode=0o600)
    assert names(outside) == []
    assert safefs.read_bytes(alias, alias / ".alpaca" / "attach" / "state.json") == b"1"


def test_the_module_imports_only_the_standard_library_and_attach_error():
    import ast
    tree = ast.parse(Path(safefs.__file__).read_text(encoding="utf-8"))
    mods = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            mods.add(node.module)
    # shutil: remove_tree and clear_path (t-648, t-652) and safefs.Folder; hashlib and secrets: safefs.Folder (fix
    # round 4 of t-650) hashes what it streams and names its temp and new folders at random; all standard library
    assert mods <= {"__future__", "errno", "hashlib", "os", "secrets", "shutil", "stat", "tempfile",
                    "alpaca.attach"}, mods


# ---- fix round 4 (check-adversarial-r4.md S3 and S4): no OSError out, and "absent" only when it is absent

NOT_ROOT = pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root reads a mode 000 folder")


@NOT_ROOT
@pytest.mark.parametrize("call", ["write_bytes", "make_dir", "make_dir_exclusive", "open_lock", "read_existing"])
def test_a_folder_the_user_cannot_search_gives_an_attach_error_not_an_oserror(studio, call):
    locked = studio / "locked"
    locked.mkdir()
    os.chmod(locked, 0)
    try:
        fn = {"write_bytes": lambda: safefs.write_bytes(studio, locked / "a" / "f", b"x"),
              "make_dir": lambda: safefs.make_dir(studio, locked / "a" / "b"),
              "make_dir_exclusive": lambda: safefs.make_dir(studio, locked / "a", exclusive=True),
              "open_lock": lambda: os.close(safefs.open_lock(studio, locked / "a" / "l.lock")),
              "read_existing": lambda: safefs.read_existing(studio, locked / "a" / "f")}[call]
        with pytest.raises(AttachError) as info:
            fn()
        assert not isinstance(info.value, OSError)
    finally:
        os.chmod(locked, 0o755)
    assert names(locked) == []


def test_a_failing_lstat_after_the_mkdir_is_an_attach_error(studio, monkeypatch):
    real = os.lstat
    seen = []

    def lstat(path, *a, **k):
        if str(path).endswith("/newdir"):
            seen.append(path)
            if len(seen) == 2:
                raise PermissionError(13, "Permission denied", path)
        return real(path, *a, **k)

    monkeypatch.setattr(os, "lstat", lstat)
    with pytest.raises(safefs.WriteFailed):
        safefs.write_bytes(studio, studio / "newdir" / "f", b"x")


def test_read_existing_gives_none_only_for_a_file_or_folder_that_is_not_there(studio):
    (studio / "f").write_bytes(b"plain")
    assert safefs.read_existing(studio, studio / "f") == b"plain"
    assert safefs.read_existing(studio, studio / "nope") is None
    assert safefs.read_existing(studio, studio / "no" / "deeper" / "f") is None


def test_read_existing_refuses_a_link_a_folder_a_file_in_the_chain_and_a_path_outside(studio, outside):
    (outside / "o").write_text("o")
    os.symlink(outside / "o", studio / "l")
    os.symlink(outside, studio / "mid")
    (studio / "d").mkdir()
    (studio / "plain").write_text("p")
    for path, word in (("l", "is a link"), ("mid/o", "is a link"), ("d", "not a plain file"),
                       ("plain/x", "not a folder")):
        with pytest.raises(AttachError, match=word):
            safefs.read_existing(studio, studio / path)
    with pytest.raises(AttachError, match="outside"):
        safefs.read_existing(studio, studio / ".." / "x")
    os.symlink(studio / "missing", studio / "dangling")
    with pytest.raises(AttachError, match="is a link"):
        safefs.read_existing(studio, studio / "dangling")


@NOT_ROOT
def test_read_existing_refuses_a_file_it_cannot_read_instead_of_calling_it_absent(studio):
    f = studio / "locked.json"
    f.write_text("{}")
    os.chmod(f, 0)
    try:
        with pytest.raises(AttachError, match="cannot be read"):
            safefs.read_existing(studio, f)
        assert safefs.read_bytes(studio, f) is None                   # the old reader folds it into None, which is why it is not used here
    finally:
        os.chmod(f, 0o600)


def test_read_existing_refuses_a_file_over_its_limit(studio):
    (studio / "big").write_bytes(b"a" * 11)
    assert safefs.read_existing(studio, studio / "big", limit=11) == b"a" * 11
    with pytest.raises(AttachError, match="larger than 10 bytes"):
        safefs.read_existing(studio, studio / "big", limit=10)


def test_read_existing_works_from_either_spelling_of_a_studio_reached_through_a_link(tmp_path, outside):
    real = tmp_path / "real"
    real.mkdir()
    (real / "f").write_bytes(b"1")
    alias = tmp_path / "alias"
    os.symlink(real, alias)
    assert safefs.read_existing(alias, alias / "f") == b"1" and safefs.read_existing(alias, real / "f") == b"1"


# ---- t-648 fix round 1 (B1): deleting a file is as link-safe as writing one -------------------------------------------

def test_remove_deletes_a_plain_file_and_says_whether_it_did(studio):
    safefs.write_bytes(studio, ".alpaca/attach/branches/t-1.json", b"{}")
    assert safefs.remove(studio, ".alpaca/attach/branches/t-1.json") is True
    assert not (studio / ".alpaca/attach/branches/t-1.json").exists()
    assert safefs.remove(studio, ".alpaca/attach/branches/t-1.json") is False          # already gone
    assert safefs.remove(studio, ".alpaca/attach/nothing/t-2.json") is False           # folder above is missing too


def test_remove_does_not_follow_a_link_at_a_folder_of_the_chain(studio, outside):
    (outside / "t-1.json").write_text("keep\n")
    (studio / ".alpaca/attach").mkdir(parents=True)
    (studio / ".alpaca/attach/branches").symlink_to(outside)
    with pytest.raises(AttachError, match="is a link"):
        safefs.remove(studio, ".alpaca/attach/branches/t-1.json")
    assert (outside / "t-1.json").read_text() == "keep\n"


def test_remove_does_not_follow_a_link_at_the_leaf_and_keeps_a_folder(studio, outside):
    (outside / "victim").write_text("keep\n")
    folder = studio / ".alpaca/attach"
    folder.mkdir(parents=True)
    (folder / "leaf.json").symlink_to(outside / "victim")
    with pytest.raises(AttachError, match="is a link"):
        safefs.remove(studio, ".alpaca/attach/leaf.json")
    assert (outside / "victim").read_text() == "keep\n" and (folder / "leaf.json").is_symlink()
    (folder / "sub").mkdir()
    with pytest.raises(AttachError, match="not a plain file"):
        safefs.remove(studio, ".alpaca/attach/sub")
    assert (folder / "sub").is_dir()


def test_remove_refuses_a_path_outside_the_studio(studio, outside):
    (outside / "x").write_text("keep\n")
    with pytest.raises(AttachError, match="outside the studio"):
        safefs.remove(studio, str(outside / "x"))
    with pytest.raises(AttachError, match="outside the studio"):
        safefs.remove(studio, "../outside/x")
    assert (outside / "x").read_text() == "keep\n"


# ---- t-648 fix round 2 (U1, U2): deleting a folder, and removing a swapped link, are as link-safe as writing -------------

def test_remove_tree_deletes_a_real_folder_with_its_content_and_says_whether_it_did(studio):
    safefs.write_bytes(studio, ".alpaca/attach/worktrees/t-1/src/x.txt", b"x")
    assert safefs.remove_tree(studio, ".alpaca/attach/worktrees/t-1") is True
    assert not (studio / ".alpaca/attach/worktrees/t-1").exists() and (studio / ".alpaca/attach/worktrees").is_dir()
    assert safefs.remove_tree(studio, ".alpaca/attach/worktrees/t-1") is False         # already gone
    assert safefs.remove_tree(studio, ".alpaca/attach/nothing/t-2") is False           # a folder above is missing


def test_remove_tree_does_not_follow_a_link_at_a_folder_of_the_chain(studio, outside):
    (outside / "t-1").mkdir()
    (outside / "t-1" / "keep.txt").write_text("keep\n")
    (studio / ".alpaca/attach").mkdir(parents=True)
    (studio / ".alpaca/attach/worktrees").symlink_to(outside)
    with pytest.raises(AttachError, match="is a link"):
        safefs.remove_tree(studio, ".alpaca/attach/worktrees/t-1")
    assert (outside / "t-1" / "keep.txt").read_text() == "keep\n"


def test_remove_tree_leaves_a_link_and_a_file_at_the_leaf_alone_and_removes_a_link_inside_as_a_link(studio, outside):
    (outside / "keep.txt").write_text("keep\n")
    folder = studio / ".alpaca/attach/worktrees"
    folder.mkdir(parents=True)
    (folder / "t-1").symlink_to(outside)
    assert safefs.remove_tree(studio, ".alpaca/attach/worktrees/t-1") is False
    assert (folder / "t-1").is_symlink() and (outside / "keep.txt").read_text() == "keep\n"
    (folder / "t-2").write_text("a file\n")
    assert safefs.remove_tree(studio, ".alpaca/attach/worktrees/t-2") is False and (folder / "t-2").is_file()
    (folder / "t-3").mkdir()
    (folder / "t-3" / "inner").symlink_to(outside)                                       # a link inside the tree
    assert safefs.remove_tree(studio, ".alpaca/attach/worktrees/t-3") is True
    assert not (folder / "t-3").exists() and (outside / "keep.txt").read_text() == "keep\n"


def test_remove_tree_refuses_a_path_outside_the_studio(studio, outside):
    (outside / "x").mkdir()
    with pytest.raises(AttachError, match="outside the studio"):
        safefs.remove_tree(studio, str(outside / "x"))
    assert (outside / "x").is_dir()


def test_drop_link_removes_the_link_and_never_its_target(studio, outside):
    (outside / "keep.txt").write_text("keep\n")
    folder = studio / ".alpaca/attach/worktrees"
    folder.mkdir(parents=True)
    (folder / "t-1").symlink_to(outside)
    assert safefs.drop_link(studio, ".alpaca/attach/worktrees/t-1") is True
    assert not os.path.lexists(folder / "t-1") and (outside / "keep.txt").read_text() == "keep\n"
    assert safefs.drop_link(studio, ".alpaca/attach/worktrees/t-1") is False           # nothing there now


def test_drop_link_leaves_a_real_folder_and_a_file_alone_and_refuses_a_link_in_the_chain(studio, outside):
    folder = studio / ".alpaca/attach/worktrees"
    (folder / "t-1").mkdir(parents=True)
    (folder / "t-2").write_text("x\n")
    assert safefs.drop_link(studio, ".alpaca/attach/worktrees/t-1") is False and (folder / "t-1").is_dir()
    assert safefs.drop_link(studio, ".alpaca/attach/worktrees/t-2") is False and (folder / "t-2").is_file()
    (outside / "t-9").symlink_to(outside)
    (studio / ".alpaca/attach/linked").symlink_to(outside)
    with pytest.raises(AttachError, match="is a link"):
        safefs.drop_link(studio, ".alpaca/attach/linked/t-9")
    assert (outside / "t-9").is_symlink()


# ------------------------------------------------------------------------------ deletes (t-652 round 2, R2-07)

def test_list_names_and_remove_file_work_on_a_plain_folder(studio):
    safefs.write_bytes(studio, "a/b/one.json", b"1")
    safefs.write_bytes(studio, "a/b/two.json", b"2")
    assert safefs.list_names(studio, "a/b") == ["one.json", "two.json"]
    assert safefs.list_names(studio, "a/missing") == [] and safefs.list_names(studio, "nope/deeper") == []
    assert safefs.remove_file(studio, "a/b/one.json") is True
    assert safefs.remove_file(studio, "a/b/one.json") is False and safefs.remove_file(studio, "x/y/z") is False
    assert names(studio / "a" / "b") == ["two.json"]


def test_a_folder_of_the_chain_that_is_a_link_is_refused_by_every_delete(studio, outside):
    (outside / "keep.json").write_text("keep")
    (outside / "tree").mkdir()
    (outside / "tree" / "f").write_text("keep")
    (studio / "a").mkdir()
    os.symlink(outside, studio / "a" / "link")
    for call in (lambda: safefs.list_names(studio, "a/link"), lambda: safefs.remove_file(studio, "a/link/keep.json"),
                 lambda: safefs.remove_tree(studio, "a/link/tree"), lambda: safefs.clear_path(studio, "a/link/tree")):
        with pytest.raises(AttachError, match="is a link"):
            call()
    assert names(outside) == ["keep.json", "tree"] and (outside / "tree" / "f").read_text() == "keep"


def test_a_file_in_the_chain_is_not_a_folder(studio):
    (studio / "a").write_text("file")
    with pytest.raises(AttachError, match="not a folder"):
        safefs.remove_file(studio, "a/b/c")
    assert (studio / "a").read_text() == "file"


# op-062 integration of t-652: t-648 and t-652 each gave `remove_tree` a contract, and they differ on a link or a
# file at the leaf and on a missing path (t-648: left alone, False; t-652: unlinked, True). Both are link-safe.
# `remove_tree` keeps t-648's contract (its tests above, and `worktree._undo` pairs it with `drop_link`); t-652's
# contract is `clear_path`, which `study._prune` and `study._snapshot` call. The two tests below call `clear_path`
# where they asserted t-652's contract; every assertion is unchanged.

def test_a_link_at_the_leaf_is_unlinked_and_never_followed(studio, outside):
    (outside / "keep.json").write_text("keep")
    os.symlink(outside, studio / "tree-link")
    os.symlink(outside / "keep.json", studio / "file-link")
    assert safefs.clear_path(studio, "tree-link") is True
    assert safefs.remove_file(studio, "file-link") is True
    assert names(studio) == [] and names(outside) == ["keep.json"]


def test_remove_tree_removes_a_folder_and_leaves_a_link_inside_it_alone(studio, outside):
    (outside / "keep.json").write_text("keep")
    safefs.write_bytes(studio, "t/deep/f.txt", b"x")
    os.symlink(outside, studio / "t" / "deep" / "out")
    assert safefs.clear_path(studio, "t") is True
    assert names(studio) == [] and names(outside) == ["keep.json"]
    assert safefs.clear_path(studio, "t") is True                        # already gone


def test_remove_file_refuses_a_folder(studio):
    (studio / "d").mkdir()
    with pytest.raises(AttachError, match="is a folder"):
        safefs.remove_file(studio, "d")


def test_a_folder_swapped_for_a_link_after_it_was_opened_is_not_followed(studio, outside, monkeypatch):
    """The delete acts through the descriptor of the folder it opened: swapping the path for a link a moment
    later deletes nothing outside."""
    (outside / "leaf").mkdir()
    (outside / "leaf" / "keep").write_text("keep")
    safefs.write_bytes(studio, "a/leaf/f.txt", b"x")
    real = safefs._open_folder

    def open_then_swap(root, parts):
        fd = real(root, parts)
        os.rename(studio / "a", studio / "a-moved")
        os.symlink(outside, studio / "a")
        return fd
    monkeypatch.setattr(safefs, "_open_folder", open_then_swap)
    assert safefs.remove_tree(studio, "a/leaf") is True
    assert (outside / "leaf" / "keep").read_text() == "keep"
    assert not (studio / "a-moved" / "leaf").exists()                     # the folder that was opened is the one removed


def test_clear_path_acts_through_the_folder_it_opened_and_unlinks_a_file_at_the_leaf(studio, outside, monkeypatch):
    """op-062 integration of t-652: `clear_path` shares `remove_tree`'s descriptor walk, so the swap case holds for it
    too; and unlike `remove_tree` it unlinks a plain file at the leaf (the study prune relies on that)."""
    (studio / "f").write_text("x")
    assert safefs.remove_tree(studio, "f") is False and (studio / "f").is_file()
    assert safefs.clear_path(studio, "f") is True and not (studio / "f").exists()
    (outside / "leaf").mkdir()
    (outside / "leaf" / "keep").write_text("keep")
    safefs.write_bytes(studio, "a/leaf/f.txt", b"x")
    real = safefs._open_folder

    def open_then_swap(root, parts):
        fd = real(root, parts)
        os.rename(studio / "a", studio / "a-moved")
        os.symlink(outside, studio / "a")
        return fd
    monkeypatch.setattr(safefs, "_open_folder", open_then_swap)
    assert safefs.clear_path(studio, "a/leaf") is True
    assert (outside / "leaf" / "keep").read_text() == "keep"
    assert not (studio / "a-moved" / "leaf").exists()
