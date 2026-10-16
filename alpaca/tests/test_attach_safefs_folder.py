"""safefs.Folder: the descriptor walk the attach seal and the verifier write through (op-062 t-650 fix round 4).

The class these tests close: bytes of the attached repo written by name into a folder that existed before the
writing call, through a link, a hard link or a path resolved more than once.
"""
import os
import shutil
import stat
from pathlib import Path

import pytest

from alpaca.attach import AttachError, safefs
from alpaca.attach.safefs import Folder


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


def listing(folder):
    return sorted(p.name for p in Path(folder).iterdir())


# ------------------------------------------------------------------------------------ the walk
def test_at_makes_the_folders_and_holds_the_last_one(studio):
    with Folder.at(studio, studio / "a" / "b") as f:
        f.put("x.txt", b"x")
    assert (studio / "a" / "b" / "x.txt").read_bytes() == b"x"


def test_at_without_create_refuses_a_missing_folder(studio):
    with pytest.raises(AttachError, match="does not exist"):
        Folder.at(studio, studio / "a" / "b", create=False)
    assert not (studio / "a").exists()


@pytest.mark.parametrize("depth", [0, 1, 2])
def test_at_refuses_a_link_at_every_part(studio, outside, depth):
    parts = ["a", "b", "c"]
    here = studio
    for i, name in enumerate(parts):
        if i == depth:
            os.symlink(str(outside), here / name)
            break
        (here / name).mkdir()
        here = here / name
    with pytest.raises(AttachError, match="is a link"):
        Folder.at(studio, studio.joinpath(*parts))
    assert listing(outside) == []


def test_at_refuses_a_file_where_a_folder_belongs(studio):
    (studio / "a").write_text("x")
    with pytest.raises(AttachError, match="is not a folder"):
        Folder.at(studio, studio / "a" / "b")
    assert (studio / "a").read_text() == "x"


def test_at_refuses_a_path_outside_the_studio(studio, outside):
    with pytest.raises(AttachError, match="outside the studio"):
        Folder.at(studio, outside / "x")


def test_a_studio_spelled_through_a_link_is_still_walked_from_its_real_root(tmp_path, studio):
    alias = tmp_path / "alias"
    os.symlink(str(studio), alias)
    with Folder.at(alias, alias / "a") as f:
        f.put("x", b"1")
    assert (studio / "a" / "x").read_bytes() == b"1"


# ------------------------------------------------------------------------------------- put
def test_put_writes_a_regular_file_with_the_umask_mode(studio):
    with Folder.at(studio, studio / "d") as f:
        f.put("x", b"abc")
        f.put("y", b"abc", mode=0o600)
    assert stat.S_IMODE((studio / "d" / "x").stat().st_mode) == safefs.masked(0o666)
    assert stat.S_IMODE((studio / "d" / "y").stat().st_mode) == 0o600
    assert listing(studio / "d") == ["x", "y"]               # no temp file left


def test_put_replaces_a_symlink_and_leaves_its_target(studio, outside):
    victim = outside / "victim.txt"
    victim.write_text("original\n")
    (studio / "d").mkdir()
    os.symlink(str(victim), studio / "d" / "x")
    with Folder.at(studio, studio / "d") as f:
        f.put("x", b"product code")
    assert victim.read_text() == "original\n"
    assert not (studio / "d" / "x").is_symlink() and (studio / "d" / "x").read_bytes() == b"product code"


def test_put_replaces_a_hard_link_and_leaves_the_other_name(studio, outside):
    victim = outside / "victim.txt"
    victim.write_text("original\n")
    (studio / "d").mkdir()
    os.link(victim, studio / "d" / "x")
    with Folder.at(studio, studio / "d") as f:
        f.put("x", b"product code")
    assert victim.read_text() == "original\n"
    assert (studio / "d" / "x").read_bytes() == b"product code" and (studio / "d" / "x").stat().st_nlink == 1


def test_put_over_a_folder_is_a_refusal_and_leaves_no_temp(studio):
    (studio / "d" / "x").mkdir(parents=True)
    with Folder.at(studio, studio / "d") as f:
        with pytest.raises(safefs.WriteFailed):
            f.put("x", b"1")
    assert listing(studio / "d") == ["x"]


def test_put_removes_its_temp_when_the_write_fails(studio, monkeypatch):
    def boom(fd, data):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(safefs, "_write_all", boom)
    with Folder.at(studio, studio / "d") as f:
        with pytest.raises(safefs.WriteFailed) as exc:
            f.put("x", b"1")
    assert "No space" in exc.value.strerror and listing(studio / "d") == []


def test_put_new_refuses_every_existing_name(studio, outside):
    (studio / "d").mkdir()
    (studio / "d" / "file").write_text("a")
    os.symlink(str(outside / "nothing"), studio / "d" / "dangling")
    with Folder.at(studio, studio / "d") as f:
        for name in ("file", "dangling"):
            with pytest.raises(AttachError, match="already exists"):
                f.put_new(name, b"x")
        f.put_new("fresh", b"x", mode=0o755)
    assert (studio / "d" / "file").read_text() == "a" and not (outside / "nothing").exists()
    assert stat.S_IMODE((studio / "d" / "fresh").stat().st_mode) == 0o755


# ------------------------------------------------------------------------------- sub and fresh
def test_sub_exclusive_claims_a_name_and_refuses_anything_there(studio, outside):
    (studio / "d").mkdir()
    os.symlink(str(outside), studio / "d" / "linked")
    (studio / "d" / "file").write_text("x")
    (studio / "d" / "real").mkdir()
    with Folder.at(studio, studio / "d") as f:
        for name in ("linked", "file", "real"):
            with pytest.raises(FileExistsError):
                f.sub(name, exclusive=True)
        with f.sub("new", exclusive=True) as g:
            g.put("x", b"1")
    assert listing(outside) == [] and (studio / "d" / "new" / "x").read_bytes() == b"1"


def test_sub_follows_no_link_and_create_makes_a_missing_folder(studio, outside):
    (studio / "d").mkdir()
    os.symlink(str(outside), studio / "d" / "linked")
    with Folder.at(studio, studio / "d") as f:
        with pytest.raises(AttachError, match="is a link"):
            f.sub("linked", create=True)
        with pytest.raises(AttachError, match="does not exist"):
            f.sub("absent")
        f.sub("made", create=True).close()
    assert listing(outside) == [] and (studio / "d" / "made").is_dir()


def test_fresh_sub_gives_a_new_random_folder_each_time(studio):
    with Folder.at(studio, studio / "d") as f:
        (n1, g1), (n2, g2) = f.fresh_sub(".new-"), f.fresh_sub(".new-")
        g1.put("x", b"1")
        g1.close()
        g2.close()
    assert n1 != n2 and n1.startswith(".new-") and len(n1) == len(".new-") + 16
    assert listing(studio / "d") == sorted([n1, n2])


# ------------------------------------------------------------------------- links, moves, drops
def test_add_link_makes_a_link_and_refuses_a_taken_name(studio):
    with Folder.at(studio, studio / "d") as f:
        f.add_link("l", "target/x")
        with pytest.raises(AttachError, match="already exists"):
            f.add_link("l", "other")
    assert os.readlink(studio / "d" / "l") == "target/x"


def test_move_entry_renames_inside_the_folder(studio):
    with Folder.at(studio, studio / "d") as f:
        f.put("a", b"1")
        f.move_entry("a", "b")
        assert f.kind("a") is None and f.kind("b") == "file"
        with pytest.raises(safefs.WriteFailed):
            f.move_entry("absent", "c")


def test_kind_has_and_names(studio, outside):
    with Folder.at(studio, studio / "d") as f:
        f.put("file", b"1")
        f.sub("dir", create=True).close()
        f.add_link("link", str(outside))
        assert [f.kind(n) for n in ("file", "dir", "link", "gone")] == ["file", "dir", "link", None]
        assert f.has("file") and not f.has("gone")
        assert f.names() == ["dir", "file", "link"]


def test_drop_of_a_link_keeps_its_target_and_drop_of_a_folder_removes_it_all(studio, outside):
    (outside / "important.txt").write_text("keep me\n")
    with Folder.at(studio, studio / "d") as f:
        f.add_link("l", str(outside))
        with f.sub("dir", create=True) as g:
            g.put("x", b"1")
            with g.sub("deeper", create=True) as h:
                h.add_link("out", str(outside))
        assert f.drop("l") is True and f.drop("dir") is True and f.drop("gone") is False
    assert (outside / "important.txt").read_text() == "keep me\n" and listing(studio / "d") == []


def test_drop_refuses_a_folder_when_the_platform_cannot_remove_it_safely(studio, monkeypatch):
    with Folder.at(studio, studio / "d") as f:
        f.sub("dir", create=True).close()
        monkeypatch.setattr(shutil.rmtree, "avoids_symlink_attacks", False, raising=False)
        with pytest.raises(AttachError, match="cannot be removed safely"):
            f.drop("dir")
    assert (studio / "d" / "dir").is_dir()


def test_drop_prefixed_removes_only_that_prefix(studio):
    with Folder.at(studio, studio / "d") as f:
        for n in (".a.new-1", ".a.new-2", ".a.old-1", "keep", ".b.new-1"):
            f.put(n, b"1")
        assert f.drop_prefixed(".a.new-") == 2
        assert f.names() == [".a.old-1", ".b.new-1", "keep"]


@pytest.mark.parametrize("bad", ["", ".", "..", "a/b", "a\0b", None, 3])
def test_every_name_must_be_one_path_part(studio, bad):
    with Folder.at(studio, studio / "d") as f:
        for call in (lambda: f.put(bad, b"x"), lambda: f.put_new(bad, b"x"), lambda: f.sub(bad),
                     lambda: f.drop(bad), lambda: f.kind(bad), lambda: f.add_link(bad, "x"),
                     lambda: f.move_entry(bad, "x")):
            with pytest.raises(AttachError, match="not one file name"):
                call()


def test_close_is_idempotent_and_the_context_manager_closes(studio):
    f = Folder.at(studio, studio / "d")
    fd = f._fd
    f.close()
    f.close()
    with pytest.raises(OSError):
        os.fstat(fd)
    with Folder.at(studio, studio / "d") as g:
        fd = g._fd
    with pytest.raises(OSError):
        os.fstat(fd)


def test_a_folder_swapped_for_a_link_after_it_was_opened_changes_nothing(studio, outside):
    """The descriptor names the folder that was opened: replacing its path with a link afterwards does not
    move the writes (the check-then-write gap the path functions of this module narrow but cannot close)."""
    (studio / "d").mkdir()
    with Folder.at(studio, studio / "d") as f:
        os.rename(studio / "d", studio / "d.moved")
        os.symlink(str(outside), studio / "d")
        f.put("x", b"product code")
    assert listing(outside) == [] and (studio / "d.moved" / "x").read_bytes() == b"product code"


# ---------------------------------------------------------------------------------- put_stream
def test_put_stream_writes_what_it_hashes_and_replaces_a_link(studio, outside):
    import hashlib
    import io
    victim = outside / "victim.txt"
    victim.write_text("original\n")
    (studio / "d").mkdir()
    os.symlink(str(victim), studio / "d" / "x")
    data = os.urandom(3 * (1 << 20) + 17)
    with Folder.at(studio, studio / "d") as f:
        got = f.put_stream("x", io.BytesIO(data), want_sha=hashlib.sha256(data).hexdigest())
    assert got == hashlib.sha256(data).hexdigest() and (studio / "d" / "x").read_bytes() == data
    assert victim.read_text() == "original\n" and listing(studio / "d") == ["x"]


def test_put_stream_leaves_nothing_on_a_hash_mismatch_or_over_the_limit(studio):
    import io
    with Folder.at(studio, studio / "d") as f:
        assert f.put_stream("x", io.BytesIO(b"abc"), want_sha="0" * 64) is None
        assert f.put_stream("y", io.BytesIO(b"x" * 100), limit=99) is None
        assert f.put_stream("z", io.BytesIO(b"x" * 99), limit=99) is not None
    assert listing(studio / "d") == ["z"]
