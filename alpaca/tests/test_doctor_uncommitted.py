"""alpaca doctor's `uncommitted` line: code left modified or untracked in the main tree too long."""
import os, subprocess, time
from alpaca import doctor

OLD = time.time() - doctor.STALE_UNCOMMITTED_SECONDS - 600


def _git(root, *args):
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


def _repo(tmp_path):
    root = tmp_path / "proj"
    (root / "alpaca").mkdir(parents=True)
    (root / ".alpaca").mkdir()
    (root / "alpaca" / "code.py").write_text("x = 1\n")
    _git(root, "init", "-q", "-b", "main")
    _git(root, "-c", "user.name=t", "-c", "user.email=t@t", "add", "-A")
    _git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base")
    return root


def _line(root):
    rows = []
    doctor._uncommitted_code(str(root), lambda n, lvl, d="": rows.append((n, lvl, d)))
    return rows


def test_stale_modified_and_untracked_code_warns(tmp_path):
    root = _repo(tmp_path)
    code = root / "alpaca" / "code.py"
    code.write_text("x = 2\n")
    new = root / "alpaca" / "new.py"
    new.write_text("y = 1\n")
    for p in (code, new):
        os.utime(p, (OLD, OLD))
    [(name, level, detail)] = _line(root)
    assert (name, level) == ("uncommitted", "warn")
    assert "2 code file(s)" in detail and "alpaca/code.py" in detail and "alpaca/new.py" in detail


def test_fresh_edits_and_record_files_stay_quiet(tmp_path):
    root = _repo(tmp_path)
    (root / "alpaca" / "code.py").write_text("x = 3\n")          # fresh: still being worked on
    rec = root / ".alpaca" / "proof.md"
    rec.write_text("record\n")
    os.utime(rec, (OLD, OLD))                                    # stale, but runtime/record
    assert _line(root) == [("uncommitted", "ok",
                            "no code left uncommitted in the main tree for over 2 h")]


def test_a_linked_worktree_and_a_non_repo_add_no_line(tmp_path):
    root = _repo(tmp_path)
    linked = tmp_path / "linked"
    _git(root, "worktree", "add", "-q", "--detach", str(linked))
    (linked / "alpaca" / "code.py").write_text("x = 4\n")
    os.utime(linked / "alpaca" / "code.py", (OLD, OLD))
    assert _line(linked) == []
    plain = tmp_path / "plain"
    plain.mkdir()
    assert _line(plain) == []
