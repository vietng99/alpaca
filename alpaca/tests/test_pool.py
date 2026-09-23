"""E6 proof: the tool capture pool.

  * a pool line carries exactly the eleven declared keys, in order, for both phases;
  * the whole tool input and the whole tool response are kept, not a digest;
  * a response above the cap is stored capped, flagged, and still hashed and measured whole;
  * an input above the cap is capped the same way, and an input above the post cap is stored on
    the pre line alone, so a 10 MB Write costs the pool once;
  * concurrent writers produce whole lines, never two halves of one;
  * a line cut off part way through never swallows the record that follows it, and the reader
    counts a damaged line rather than raising on it;
  * PostToolUse keeps its heartbeat byte for byte and adds a pool line;
  * PreToolUse writes a pool line, prints nothing and exits 0, so it can never block a call;
  * a session id that names a path cannot leave the pool directory.

Every control asserts a positive and a negative path.
"""
import json
import os
import subprocess
import sys

from alpaca import cli, db, pool, util

ENV = lambda project: {**os.environ, "CLAUDE_PROJECT_DIR": project,
                       "PYTHONPATH": os.path.dirname(os.path.dirname(os.path.dirname(__file__)))}


def hook(module, payload, project):
    return subprocess.run([sys.executable, "-m", module], input=json.dumps(payload),
                          capture_output=True, text=True, encoding="utf-8",
                          cwd=project, env=ENV(project))


# ------------------------------------------------------------------------------ the line shape
def test_a_pool_line_carries_exactly_the_declared_keys(project):
    cli.main(["init"])
    payload = {"session_id": "s1", "cwd": project, "tool_name": "Edit",
               "tool_use_id": "toolu_01", "tool_input": {"file_path": "/x/y.py", "old": "a"},
               "tool_response": {"filePath": "/x/y.py", "success": True}}
    rec = pool.record(project, "s1", "post", payload)
    assert tuple(rec) == pool.KEYS
    assert rec["phase"] == "post" and rec["tool"] == "Edit" and rec["tool_use_id"] == "toolu_01"
    assert rec["input"] == {"file_path": "/x/y.py", "old": "a"}
    assert rec["response"] == {"filePath": "/x/y.py", "success": True}
    assert rec["response_truncated"] is False and rec["cwd"] == project
    assert rec["response_bytes"] == len(util.canonical_json(rec["response"]).encode("utf-8"))
    assert rec["response_sha256"] == util.sha256_hex(util.canonical_json(rec["response"]))
    # the file on disk holds the same object, with the same key order.
    lines = pool.read(project, "s1")
    assert len(lines) == 1 and tuple(lines[0]) == pool.KEYS and lines[0] == rec
    # negative: a pre line has no response at all.
    pre = pool.record(project, "s1", "pre", payload)
    assert pre["response"] is None and pre["response_sha256"] is None
    assert pre["response_bytes"] == 0 and pre["response_truncated"] is False
    assert tuple(pre) == pool.KEYS


def test_a_missing_tool_input_or_name_still_writes_a_line(project):
    cli.main(["init"])
    rec = pool.record(project, "s1", "pre", {"session_id": "s1"})
    assert rec["tool"] == "?" and rec["input"] == {} and rec["tool_use_id"] is None
    assert rec["cwd"] is None
    assert len(pool.read(project, "s1")) == 1


# ------------------------------------------------------------------------------ the 256 KiB cap
def test_a_large_response_is_capped_but_hashed_whole(project):
    cli.main(["init"])
    big = "x" * (pool.CAP + 5000)
    rec = pool.record(project, "s1", "post", {"tool_name": "Bash", "tool_response": big})
    assert rec["response_truncated"] is True
    assert len(rec["response"].encode("utf-8")) == pool.CAP
    assert rec["response_bytes"] == len(big.encode("utf-8"))
    assert rec["response_sha256"] == util.sha256_hex(big)
    # negative: a response at the cap is stored whole and not flagged.
    small = "y" * pool.CAP
    rec2 = pool.record(project, "s1", "post", {"tool_name": "Bash", "tool_response": small})
    assert rec2["response_truncated"] is False and rec2["response"] == small


def test_a_structured_response_is_measured_by_its_canonical_json(project):
    cli.main(["init"])
    body = {"stdout": "z" * (pool.CAP + 10), "code": 0}
    rec = pool.record(project, "s1", "post", {"tool_name": "Bash", "tool_response": body})
    blob = util.canonical_json(body).encode("utf-8")
    assert rec["response_truncated"] is True
    assert rec["response_bytes"] == len(blob) and rec["response_sha256"] == util.sha256_hex(blob)
    assert isinstance(rec["response"], str) and rec["response"].startswith('{"code":0')


def test_a_response_that_cannot_be_serialised_still_writes_a_line(project):
    cli.main(["init"])
    rec = pool.line("s1", "post", {"tool_name": "X", "tool_response": {1, 2, 3}})
    assert rec["response_bytes"] > 0 and rec["response_sha256"]
    pool.append(project, "s1", rec)
    assert len(pool.read(project, "s1")) == 1


# -------------------------------------------------------------------------- the input cap
def test_a_large_input_is_capped_but_hashed_whole(project):
    cli.main(["init"])
    big = {"content": "x" * (pool.CAP + 5000)}
    rec = pool.record(project, "s1", "pre", {"tool_name": "Write", "tool_input": big})
    blob = util.canonical_json(big).encode("utf-8")
    assert rec["input_truncated"] is True and rec["input_ref"] is None
    assert isinstance(rec["input"], str) and len(rec["input"].encode("utf-8")) == pool.CAP
    assert rec["input_bytes"] == len(blob) and rec["input_sha256"] == util.sha256_hex(blob)
    assert tuple(rec) == pool.KEYS
    # negative: an input under the cap is stored whole, unflagged, and still measured and hashed.
    small = {"file_path": "/x/y.py"}
    rec2 = pool.record(project, "s1", "pre", {"tool_name": "Read", "tool_input": small})
    assert rec2["input_truncated"] is False and rec2["input"] == small
    assert rec2["input_bytes"] == len(util.canonical_json(small).encode("utf-8"))
    assert rec2["input_sha256"] == util.sha256_hex(util.canonical_json(small))


def test_a_big_input_is_stored_once_on_the_pre_line(project):
    cli.main(["init"])
    payload = {"tool_name": "Write", "tool_use_id": "toolu_big",
               "tool_input": {"content": "z" * (10 * 1024 * 1024)}}
    pre = pool.record(project, "s1", "pre", payload)
    post = pool.record(project, "s1", "post", dict(payload, tool_response="ok"))
    # the post line keeps the digest and the size and points at the pre line for the bytes.
    assert post["input"] is None and post["input_ref"] == "pre"
    assert post["input_truncated"] is False
    assert post["input_sha256"] == pre["input_sha256"]
    assert post["input_bytes"] == pre["input_bytes"] > 10 * 1024 * 1024
    # a 10 MB Write costs the pool one capped copy, not two whole ones.
    assert os.path.getsize(pool.path(project, "s1")) < 2 * pool.CAP
    # negative: an input under the post cap is carried by the post line itself, with no ref.
    small = {"tool_name": "Read", "tool_use_id": "toolu_small", "tool_input": {"file_path": "/a"}}
    p2 = pool.record(project, "s1", "post", dict(small, tool_response="ok"))
    assert p2["input"] == {"file_path": "/a"} and p2["input_ref"] is None


# ------------------------------------------------------------------------- whole lines, always
def test_a_line_cut_off_part_way_never_swallows_the_next_record(project):
    cli.main(["init"])
    pool.record(project, "s1", "pre", {"tool_name": "Read", "tool_input": {"file_path": "/a"}})
    with open(pool.path(project, "s1"), "a", encoding="utf-8") as fh:
        fh.write('{"ts": "2026-09-18T10:00:00", "sid": "s1", "ph')   # a write cut off at a deadline
    rec = pool.record(project, "s1", "post", {"tool_name": "Bash", "tool_response": "ok"})
    lines = pool.read(project, "s1")
    # the new record is a line of its own; only the damaged one is lost, and it is counted.
    assert [ln["tool"] for ln in lines] == ["Read", "Bash"]
    assert lines.damaged == 1
    assert lines[-1]["response"] == "ok" and lines[-1]["ts"] == rec["ts"]
    # negative: an undamaged file reads back with nothing counted.
    pool.record(project, "s2", "pre", {"tool_name": "Read"})
    assert pool.read(project, "s2").damaged == 0


def test_read_skips_a_line_that_does_not_parse_instead_of_raising(project):
    cli.main(["init"])
    pool.record(project, "s1", "pre", {"tool_name": "Read"})
    with open(pool.path(project, "s1"), "a", encoding="utf-8") as fh:
        fh.write("}{ not json\n")
        fh.write("12345\n")                              # parses, but is not a pool line
    lines = pool.read(project, "s1")
    assert len(lines) == 1 and lines[0]["tool"] == "Read" and lines.damaged == 2
    # negative: a blank line is not damage, just a blank line.
    with open(pool.path(project, "s1"), "a", encoding="utf-8") as fh:
        fh.write("\n\n")
    assert pool.read(project, "s1").damaged == 2


def test_short_writes_and_torn_tail_preserve_next_record(project, monkeypatch):
    cli.main(["init"])
    pool.record(project, "s1", "pre", {"tool_name": "Read"})
    with open(pool.path(project,"s1"),"a") as fh:
        fh.write('{"partial"')
    real_write = os.write
    syncs = []
    real_sync = os.fsync
    monkeypatch.setattr(os,"write",lambda fd,data:real_write(fd,data[:7]))
    monkeypatch.setattr(os,"fsync",lambda fd:(syncs.append(fd),real_sync(fd))[1])
    pool.record(project,"s1","post",{"tool_name":"Bash","tool_response":"ok"})
    lines = pool.read(project,"s1")
    assert [r['tool'] for r in lines] == ['Read','Bash']
    assert lines.damaged == 1
    assert len(syncs) == 2


def test_concurrent_writers_never_interleave_a_line(project):
    cli.main(["init"])
    script = (
        "import sys\n"
        "sys.path.insert(0, %r)\n"
        "from alpaca import pool\n"
        "root, sid, mark = sys.argv[1], sys.argv[2], sys.argv[3]\n"
        "for i in range(40):\n"
        "    pool.record(root, sid, 'post', {'tool_name': mark,\n"
        "        'tool_input': {'n': i}, 'tool_response': mark * 20000})\n"
    ) % os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
    procs = [subprocess.Popen([sys.executable, "-c", script, project, "s1", mark])
             for mark in ("a", "b", "c")]
    for p in procs:
        assert p.wait() == 0
    path = pool.path(project, "s1")
    with open(path, encoding="utf-8") as fh:
        raw = [ln for ln in fh.read().splitlines() if ln.strip()]
    assert len(raw) == 120
    # positive: every line parses on its own, so no write landed inside another.
    for ln in raw:
        obj = json.loads(ln)
        assert tuple(obj) == pool.KEYS
    # negative: the three writers are all represented, so this was a real race.
    assert {json.loads(ln)["tool"] for ln in raw} == {"a", "b", "c"}


def test_a_session_id_that_names_a_path_stays_inside_the_pool_dir(project):
    cli.main(["init"])
    p = pool.path(project, "../../escape")
    assert os.path.dirname(os.path.abspath(p)) == os.path.abspath(pool.pool_dir(project))
    assert os.path.basename(p) == "..-..-escape.jsonl" and "/" not in os.path.basename(p)
    # negative: an ordinary session id is not mangled.
    assert os.path.basename(pool.path(project, "codex-1.2_a")) == "codex-1.2_a.jsonl"


# ----------------------------------------------------------------------------------- the hooks
def test_post_tool_keeps_the_heartbeat_and_adds_a_pool_line(project):
    cli.main(["init"])
    p = hook("alpaca.hooks.post_tool",
             {"session_id": "s1", "cwd": project, "tool_name": "Edit",
              "tool_input": {"file_path": "/x/y.py"},
              "tool_response": {"success": True}}, project)
    assert p.returncode == 0 and p.stdout == ""
    conn = db.connect(project)
    e = db.events(conn, kind="heartbeat")[-1]
    # the heartbeat shape is unchanged: the two keys it always had, nothing more.
    assert e["data"] == {"tool": "Edit", "ref": "/x/y.py"}
    assert db.rows(conn, "sessions", "sid='s1'")[0]["beats"] == 1
    conn.close()
    lines = pool.read(project, "s1")
    assert len(lines) == 1 and lines[0]["phase"] == "post"
    assert lines[0]["response"] == {"success": True}
    # negative: the pool keeps what the heartbeat drops. A Bash command is a digest in the
    # record and the whole command in the pool.
    cmd = 'curl -H "Authorization: Bearer sk-live-SECRET"'
    hook("alpaca.hooks.post_tool",
         {"session_id": "s1", "cwd": project, "tool_name": "Bash",
          "tool_input": {"command": cmd}, "tool_response": "ok"}, project)
    conn = db.connect(project)
    assert "SECRET" not in db.events(conn, kind="heartbeat")[-1]["data"]["ref"]
    conn.close()
    assert pool.read(project, "s1")[-1]["input"]["command"] == cmd


def test_pre_tool_writes_a_pool_line_and_stays_silent(project):
    cli.main(["init"])
    p = hook("alpaca.hooks.pre_tool",
             {"session_id": "s1", "cwd": project, "tool_name": "Bash",
              "tool_use_id": "toolu_9", "tool_input": {"command": "ls -la"}}, project)
    # a PreToolUse hook that writes on stdout can block the call; this one must not.
    assert p.returncode == 0 and p.stdout == "" and p.stderr == ""
    lines = pool.read(project, "s1")
    assert len(lines) == 1
    assert lines[0]["phase"] == "pre" and lines[0]["input"]["command"] == "ls -la"
    assert lines[0]["response"] is None and lines[0]["tool_use_id"] == "toolu_9"
    # negative: PreToolUse records no event, so the chain is untouched by a tool call starting.
    conn = db.connect(project)
    assert [e for e in db.events(conn, limit=1000) if e["kind"] == "heartbeat"] == []
    conn.close()


def test_pre_tool_stays_silent_on_a_broken_payload(project):
    cli.main(["init"])
    p = subprocess.run([sys.executable, "-m", "alpaca.hooks.pre_tool"],
                       input="not json at all {[}]", capture_output=True, text=True,
                       encoding="utf-8", cwd=project, env=ENV(project), timeout=20)
    assert p.returncode == 0 and p.stdout == ""
    assert pool.read(project, "s1") == []


def test_both_phases_land_in_one_file_in_order(project):
    cli.main(["init"])
    base = {"session_id": "s1", "cwd": project, "tool_name": "Read",
            "tool_input": {"file_path": "/a"}}
    hook("alpaca.hooks.pre_tool", base, project)
    hook("alpaca.hooks.post_tool", dict(base, tool_response="body"), project)
    phases = [ln["phase"] for ln in pool.read(project, "s1")]
    assert phases == ["pre", "post"]
    assert os.path.isfile(os.path.join(project, ".alpaca", "pool", "tools", "s1.jsonl"))
