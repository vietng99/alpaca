import os, pytest
from alpaca import paths

def test_root_walks_up_to_manifest(project):
    assert paths.root() == project

def test_root_prefers_env(project, monkeypatch, tmp_path):
    other = tmp_path / "other"; other.mkdir(); (other / "ALPACA-MANIFEST").write_text("x", encoding="utf-8")
    monkeypatch.setenv("ALPACA_ROOT", str(other))
    monkeypatch.chdir(tmp_path)
    assert paths.root() == str(other)

def test_root_not_found(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path); monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    with pytest.raises(paths.RootNotFound):
        paths.root()

def test_runtime_and_db_paths(project):
    assert paths.runtime_dir(project) == os.path.join(project, ".alpaca")
    assert paths.db_path(project) == os.path.join(project, ".alpaca", "alpaca.db")

def test_escape_cwd():
    assert paths.escape_cwd("/home/u/DV/proj.1") == "-home-u-DV-proj-1"

def test_transcript_dir_uses_config_dir(project, tmp_path):
    assert paths.transcript_dir(project) == os.path.join(project, ".alpaca", "transcripts")
