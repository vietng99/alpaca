"""M4.10: honest WHERE derivation, on the positive and the negative paths.

An event's project is the nearest ancestor of the TARGET that carries the manifest, never the
shell cwd. A command or a URL is display, never attribution. A target with no marker above it
attributes nothing. The record's `where_` column and the analytics both read it.
"""
import json
import os
import subprocess
import sys

import pytest

from alpaca import clock, db, paths, util, where
from alpaca.analytics import parse_session as ps

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _mark_root(path, project_id=None):
    """A throwaway project root on disk: a manifest, and an optional project.yaml carrying a
    stable project id. Never the live .alpaca/ tree."""
    os.makedirs(path, exist_ok=True)
    # A byte copy of the harness manifest is enough for the marker walk; keep it tiny.
    with open(os.path.join(path, paths.MANIFEST), "w", encoding="utf-8") as fh:
        fh.write("[mechanism]\nalpaca/\n[memory]\n.alpaca/\n")
    if project_id is not None:
        with open(os.path.join(path, "project.yaml"), "w", encoding="utf-8") as fh:
            fh.write("name: %s\nproject_id: %s\ntier: public\n" % (os.path.basename(path), project_id))
    return path


# --------------------------------------------------------------------------- derive()

def test_derive_walks_up_from_a_file_to_the_nearest_marker(tmp_path):
    root = _mark_root(str(tmp_path / "proj"), "P_ROOT")
    deep = os.path.join(root, "a", "b", "c")
    os.makedirs(deep)
    target = os.path.join(deep, "file.py")
    assert where.derive(target) == os.path.abspath(root)
    # A directory target resolves from itself, not its parent.
    assert where.derive(deep) == os.path.abspath(root)


def test_derive_returns_none_when_no_marker_stands_above_the_target(tmp_path):
    # tmp_path itself carries no manifest, and nothing above it does either under the test tree.
    stray = tmp_path / "no-marker" / "x.txt"
    stray.parent.mkdir(parents=True)
    stray.write_text("", encoding="utf-8")
    assert where.derive(str(stray)) is None
    assert where.derive("") is None
    assert where.derive(None) is None


# --------------------------------------------------------------------------- attribute()

def test_target_outside_the_project_stamps_the_target_project(tmp_path):
    here = _mark_root(str(tmp_path / "here"), "P_HERE")
    other = _mark_root(str(tmp_path / "other"), "P_OTHER")
    # The shell sits in `here`; the tool edits a file in `other`.
    event = {"cwd": here, "tool_input": {"file_path": os.path.join(other, "src", "z.py")}}
    assert where.attribute(event) == "P_OTHER"


def test_session_one_level_above_the_root_attributes_to_the_root(tmp_path):
    root = _mark_root(str(tmp_path / "root"), "P_ROOT")
    above = os.path.dirname(root)  # one level above the root, no marker of its own
    event = {"cwd": above, "tool_input": {"file_path": os.path.join(root, "sub", "f.py")}}
    # The shell cwd is above the root and unmarked; the target is inside the root.
    assert where.derive(above) is None
    assert where.attribute(event) == "P_ROOT"


def test_a_command_string_and_a_url_are_display_never_attribution(tmp_path):
    root = _mark_root(str(tmp_path / "root"), "P_ROOT")
    cmd = {"cwd": root, "tool_input": {"command": "rm -rf %s" % root}}
    url = {"cwd": root, "tool_input": {"url": "https://example.test/%s" % root}}
    assert where.attribute(cmd) is None
    assert where.attribute(url) is None


def test_a_target_with_no_marker_above_it_attributes_nothing(tmp_path):
    loose = tmp_path / "loose" / "f.py"
    loose.parent.mkdir(parents=True)
    loose.write_text("", encoding="utf-8")
    assert where.attribute({"tool_input": {"file_path": str(loose)}}) is None


def test_a_marked_root_without_a_project_yaml_falls_back_to_its_path(tmp_path):
    root = _mark_root(str(tmp_path / "bare"))  # marker but no project.yaml
    event = {"tool_input": {"file_path": os.path.join(root, "x.py")}}
    assert where.attribute(event) == os.path.abspath(root)


# --------------------------------------------------------------- db.py `where_` field

def test_event_carries_the_derived_where_stamped_with_a_fixed_clock(tmp_path):
    dbroot = _mark_root(str(tmp_path / "dbroot"), "P_DB")
    other = _mark_root(str(tmp_path / "elsewhere"), "P_OTHER")
    conn = db.connect(dbroot)
    fc = clock.FixedClock("2026-05-05T00:00:00+00:00")

    payload = {"cwd": dbroot, "tool_input": {"file_path": os.path.join(other, "f.py")}}
    db.append_event(conn, session="s1", actor="agent", kind="heartbeat",
                    data={"tool": "Edit", "ref": "f.py"},
                    where=where.attribute(payload), clock=fc)
    row = conn.execute("SELECT ts, where_, data FROM events ORDER BY id DESC LIMIT 1").fetchone()
    assert row["ts"] == "2026-05-05T00:00:00+00:00"
    assert row["where_"] == "P_OTHER"
    # The where field does not disturb the hash chain (it is not a hashed field).
    ok, _ = db.verify_chain(conn)
    assert ok is True

    # Negative path: a command-only heartbeat attributes nothing, and the default INSERT
    # (no where_) still lands the row.
    db.append_event(conn, session="s1", actor="agent", kind="heartbeat",
                    data={"tool": "Bash", "ref": "ls sha256:deadbeef"},
                    where=where.attribute({"cwd": dbroot, "tool_input": {"command": "ls"}}))
    last = conn.execute("SELECT where_ FROM events ORDER BY id DESC LIMIT 1").fetchone()
    assert last["where_"] is None
    ok, _ = db.verify_chain(conn)
    assert ok is True


def test_append_event_without_where_is_unchanged(tmp_path):
    # A caller that never passes `where` gets exactly the old behavior: no where_ set.
    dbroot = _mark_root(str(tmp_path / "plain"), "P_PLAIN")
    conn = db.connect(dbroot)
    db.append_event(conn, session="s", actor="agent", kind="note", data={"i": 1})
    row = conn.execute("SELECT where_ FROM events ORDER BY id DESC LIMIT 1").fetchone()
    assert row["where_"] is None


# ------------------------------------------------------ the heartbeat writer wiring

def _hook(module, payload, cwd):
    env = {**os.environ, "CLAUDE_PROJECT_DIR": cwd, "PYTHONPATH": REPO}
    return subprocess.run([sys.executable, "-m", module], input=json.dumps(payload),
                          capture_output=True, text=True, encoding="utf-8", cwd=cwd, env=env)


def test_post_tool_stamps_where_from_the_target(tmp_path):
    dbroot = _mark_root(str(tmp_path / "shell"), "P_SHELL")
    (tmp_path / "shell" / "style").mkdir(parents=True, exist_ok=True)
    (tmp_path / "shell" / "style" / "banned.txt").write_text("", encoding="utf-8")
    other = _mark_root(str(tmp_path / "target"), "P_TARGET")

    p = _hook("alpaca.hooks.post_tool",
              {"session_id": "s1", "cwd": dbroot, "tool_name": "Edit",
               "tool_input": {"file_path": os.path.join(other, "y.py")}}, dbroot)
    assert p.returncode == 0 and p.stdout == ""
    conn = db.connect(dbroot)
    row = conn.execute("SELECT where_ FROM events WHERE kind='heartbeat' ORDER BY id DESC LIMIT 1").fetchone()
    assert row["where_"] == "P_TARGET"

    # A command heartbeat records no attribution.
    _hook("alpaca.hooks.post_tool",
          {"session_id": "s1", "cwd": dbroot, "tool_name": "Bash",
           "tool_input": {"command": "echo hi"}}, dbroot)
    row = conn.execute("SELECT where_ FROM events WHERE kind='heartbeat' ORDER BY id DESC LIMIT 1").fetchone()
    assert row["where_"] is None


# ------------------------------------------------------- analytics reconciliation

def test_parse_session_attributes_edits_by_target_project(tmp_path):
    root = _mark_root(str(tmp_path / "an"), "P_AN")
    edited = os.path.join(root, "src", "cli.py")
    os.makedirs(os.path.dirname(edited))
    open(edited, "w", encoding="utf-8").close()

    rec = {"type": "assistant", "timestamp": "2026-09-15T10:05:00.000Z",
           "message": {"model": "claude-sonnet-5",
                       "content": [{"type": "tool_use", "name": "Edit", "input": {"file_path": edited}}],
                       "usage": {"input_tokens": 1, "output_tokens": 2}}}
    tpath = tmp_path / "sess.jsonl"
    tpath.write_text(json.dumps(rec) + "\n", encoding="utf-8")
    d = ps.parse(str(tpath))
    # Keyed by the stable project id, so a renamed folder keeps this session.
    assert d["projects"] == {"P_AN": 1}


def test_parse_session_leaves_unmarked_edits_unattributed(tmp_path):
    loose = tmp_path / "loose.py"
    loose.write_text("", encoding="utf-8")
    rec = {"type": "assistant", "timestamp": "2026-09-15T10:05:00.000Z",
           "message": {"model": "claude-sonnet-5",
                       "content": [{"type": "tool_use", "name": "Write", "input": {"file_path": str(loose)}}],
                       "usage": {"input_tokens": 1, "output_tokens": 2}}}
    tpath = tmp_path / "sess.jsonl"
    tpath.write_text(json.dumps(rec) + "\n", encoding="utf-8")
    d = ps.parse(str(tpath))
    assert d["projects"] == {}
