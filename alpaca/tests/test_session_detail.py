"""Read-only, source-scoped conversation detail for the operations hub."""
import json
from pathlib import Path

import pytest

from alpaca import db, pool, transcripts
from alpaca.analytics import detail as service


def write_jsonl(path, records):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    return path


def register(root, sid="s1", path=None):
    conn = db.connect(root)
    db.upsert(conn, "sessions", "sid", {"sid": sid, "started": "2026-09-22T00:00:00Z",
                                       "transcript": str(path) if path else None})
    db.meta_set(conn, "operator:" + sid, "codex")
    conn.close()


def message(role, text, ident, ts="2026-09-22T01:00:00Z"):
    return {"type": role, "uuid": ident, "timestamp": ts,
            "message": {"role": role, "content": [{"type": "text", "text": text}]}}


def test_distinct_repeated_prompts_full_text_and_stable_pagination(project):
    text = "repeat this instruction " * 130
    path = write_jsonl(transcripts.local_path(project, "s1"),
                       [message("user", text, "u1"), message("assistant", "Understood", "a1"),
                        message("user", text, "u2"), message("assistant", "Done", "a2")])
    register(project)
    page = service.detail(project, "s1", limit=2)
    assert [m["text"] for m in page["messages"]] == [text, "Done"]
    assert page["has_more"] and page["total"] == 4
    old = service.detail(project, "s1", before=page["next_before"], limit=2)
    assert [m["text"] for m in old["messages"]] == [text, "Understood"]
    assert not old["has_more"] and old["next_before"] is None
    assert len({m["id"] for m in old["messages"] + page["messages"]}) == 4
    with path.open("a") as fh:
        fh.write(json.dumps(message("user", "new arrival", "u3")) + "\n")
    again = service.detail(project, "s1", before=page["next_before"], limit=2)
    assert again["messages"] == old["messages"]
    assert page["coverage"]["state"] == "captured"
    assert page["session"]["tokens"] is None


def test_jump_from_usage_source_and_continue_transcript(project):
    write_jsonl(transcripts.local_path(project, "s1"),
                [message("assistant", "response %d" % n, "a%d" % n) for n in range(20)])
    register(project)
    page = service.detail(project, "s1", around=8, limit=4)
    assert [m["source"]["line"] for m in page["messages"]] == [6, 7, 8, 9]
    assert page["has_later"] and page["has_more"]
    later = service.detail(project, "s1", after=page["next_after"], limit=4)
    assert [m["source"]["line"] for m in later["messages"]] == [10, 11, 12, 13]
    with pytest.raises(ValueError):
        service.detail(project, "s1", around=-1)
    with pytest.raises(ValueError):
        service.detail(project, "s1", around=8, before=page["next_before"])


def test_claude_tools_results_pool_dedup_and_hidden_thought_exclusion(project):
    records = [message("user", "inspect", "u1"),
               {"type": "assistant", "uuid": "a1", "message": {"content": [
                   {"type": "thinking", "thinking": "PRIVATE REASONING"},
                   {"type": "text", "text": "Checking the file."},
                   {"type": "tool_use", "id": "call1", "name": "Read", "input": {"file_path": "a.py"}}]}},
               {"type": "user", "uuid": "r1", "message": {"content": [
                   {"type": "tool_result", "tool_use_id": "call1", "content": "full result"}]}},
               message("assistant", "It is present.", "a2")]
    write_jsonl(transcripts.local_path(project, "s1"), records)
    register(project)
    payload = {"tool_use_id": "call1", "tool_name": "Read", "tool_input": {"file_path": "a.py"},
               "tool_response": "full result"}
    pool.record(project, "s1", "pre", payload)
    pool.record(project, "s1", "post", payload)
    result = service.detail(project, "s1")
    tools = [t for m in result["messages"] for t in m["tools"]]
    assert len(tools) == 1 and tools[0]["id"] == "call1"
    assert tools[0]["input"] == {"file_path": "a.py"}
    assert tools[0]["result"] == "full result" and tools[0]["status"] == "completed"
    assert len(result["messages"]) == 3
    assert "PRIVATE REASONING" not in json.dumps(result)
    assert result["session"]["tool_calls"] == 1


def test_pool_pairs_and_retains_unmatched_calls_without_transcript(project):
    register(project)
    write_jsonl(pool.path(project, "s1"), [
        {"sid": "s1", "phase": "pre", "tool_use_id": "c1", "tool": "Write", "input": {"content": "full input"}},
        {"sid": "s1", "phase": "post", "tool_use_id": "c1", "tool": "Write", "input": None,
         "input_ref": "pre", "response": "ok"},
        {"sid": "s1", "phase": "pre", "tool_use_id": "c2", "tool": "Read", "input": {}},
        {"sid": "another-session", "phase": "pre", "tool_use_id": "foreign", "tool": "Read"}])
    result = service.detail(project, "s1")
    tools = [t for m in result["messages"] for t in m["tools"]]
    assert len(tools) == 2
    assert tools[0]["input"] == {"content": "full input"} and tools[0]["result"] == "ok"
    assert tools[1]["status"] == "unmatched-start"
    assert result["coverage"]["state"] == "partial"


def test_parent_transcript_does_not_adopt_pool_only_child_calls(project):
    register(project)
    write_jsonl(transcripts.local_path(project, "s1"), [message("user", "parent work", "p1")])
    write_jsonl(pool.path(project, "s1"), [
        {"sid": "s1", "phase": "pre", "tool_use_id": "child-call", "tool": "Write", "input": {}}])
    result = service.detail(project, "s1")
    assert result["session"]["tool_calls"] == 0
    assert [m["text"] for m in result["messages"]] == ["parent work"]


def test_child_navigation_keeps_parent_identity_and_conversation_separate(project):
    register(project)
    write_jsonl(transcripts.local_path(project, "s1"), [message("user", "parent work", "p1")])
    sub = Path(transcripts.subagents_dir(project, "s1")) / "nested" / "agent-worker.jsonl"
    write_jsonl(sub, [message("user", "child assignment", "c1"), message("assistant", "child result", "c2")])
    result = service.detail(project, "s1")
    assert [m["text"] for m in result["messages"]] == ["parent work"]
    assert len(result["children"]) == 1
    child = result["children"][0]
    assert child["turns"] == 1 and child["title"] == "child assignment"
    nested = service.detail(project, "s1", child=child["id"])
    assert nested["session"]["parent_sid"] == "s1"
    assert nested["session"]["child_id"] == child["id"]
    assert [m["text"] for m in nested["messages"]] == ["child assignment", "child result"]
    assert result["coverage"]["totals_include_children"] is False


def test_missing_registered_source_uses_local_copy_without_exposing_path(project, tmp_path):
    missing = tmp_path / "private-account" / "secret-rollout-name.jsonl"
    register(project, path=missing)
    write_jsonl(transcripts.local_path(project, "s1"), [message("user", "retained copy", "u1")])
    result = service.detail(project, "s1")
    assert result["messages"][0]["text"] == "retained copy"
    assert result["coverage"]["source"] == "project-transcript"
    assert "secret-rollout-name" not in json.dumps(result)


def test_missing_capture_falls_back_to_attributed_work_and_messages_read_only(project, tmp_path):
    register(project, path=tmp_path / "missing.jsonl")
    conn = db.connect(project)
    db.append_event(conn, session="s1", actor="codex", kind="session-checkpoint", data={"note": "Verified lint; next simulate"})
    db.append_event(conn, session="other", actor="claude", kind="session-checkpoint", data={"note": "foreign"})
    db.upsert(conn, "messages", "id", {"id": 1, "session": "s1", "sender": "owner", "to_": "codex", "body": "Review report", "kind": "question"})
    conn.close()
    before = Path(project, ".alpaca", "alpaca.db").read_bytes()
    result = service.detail(project, "s1")
    assert result["coverage"]["state"] == "record-only"
    assert "transcript" in result["coverage"]["reason"].lower()
    assert {m["text"] for m in result["messages"]} == {"Verified lint; next simulate", "Review report"}
    assert {m["actor"] for m in result["messages"]} == {"codex", "owner"}
    assert before == Path(project, ".alpaca", "alpaca.db").read_bytes()


def test_malformed_and_incomplete_tail_do_not_hide_valid_messages(project):
    register(project)
    path = write_jsonl(transcripts.local_path(project, "s1"), [message("user", "visible one", "u1")])
    with path.open("a") as fh:
        fh.write("{broken}\n[]\n")
        fh.write(json.dumps(message("assistant", "visible two", "a1")) + "\n")
        fh.write('{"type":"user","message":')
    result = service.detail(project, "s1")
    assert [m["text"] for m in result["messages"]] == ["visible one", "visible two"]
    assert result["coverage"]["state"] == "partial"
    assert result["coverage"]["malformed_lines"] == 2
    assert result["coverage"]["partial_lines"] == 1


def test_codex_visible_shapes_pair_tools_and_deduplicate_mirrors(project):
    register(project)
    records = [
        {"type": "event_msg", "timestamp": "2026-09-22T01:00:00Z", "payload": {"type": "user_message", "message": "Do it"}},
        {"type": "response_item", "timestamp": "2026-09-22T01:00:00Z", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Do it"}]}},
        {"type": "response_item", "payload": {"type": "reasoning", "summary": [{"text": "hidden"}]}},
        {"type": "response_item", "payload": {"type": "function_call", "name": "exec_command", "call_id": "call1", "arguments": '{"cmd":"pwd"}'}},
        {"type": "response_item", "payload": {"type": "function_call_output", "call_id": "call1", "output": "project"}},
        {"type": "event_msg", "timestamp": "2026-09-22T01:00:02Z", "payload": {"type": "agent_message", "message": "Done"}},
        {"type": "response_item", "timestamp": "2026-09-22T01:00:02Z", "payload": {"type": "message", "role": "assistant", "phase": "final_answer", "content": [{"type": "output_text", "text": "Done"}]}},
        {"type": "event_msg", "timestamp": "2026-09-22T01:00:03Z", "payload": {"type": "user_message", "message": "Do it"}},
    ]
    write_jsonl(transcripts.local_path(project, "s1"), records)
    result = service.detail(project, "s1")
    assert [m["text"] for m in result["messages"] if m["text"]] == ["Do it", "Done", "Do it"]
    tools = [t for m in result["messages"] for t in m["tools"]]
    assert len(tools) == 1 and tools[0]["input"] == {"cmd": "pwd"} and tools[0]["result"] == "project"
    assert "hidden" not in json.dumps(result)


@pytest.mark.parametrize("sid,child", [("../s1", None), ("s1", "../../escape"), ("s1", "/tmp/secret"), ("s1", "unknown")])
def test_invalid_parent_or_child_identifier_never_becomes_a_path(project, sid, child):
    register(project)
    with pytest.raises(ValueError):
        service.detail(project, sid, child=child)


def test_only_registered_external_sources_and_contained_children_are_read(project, tmp_path):
    external = write_jsonl(tmp_path / "account" / "session.jsonl", [message("user", "registered", "u1")])
    write_jsonl(tmp_path / "account" / "unregistered.jsonl", [message("user", "NOT REGISTERED", "u2")])
    private = write_jsonl(tmp_path / "private.jsonl", [message("user", "ESCAPED CHILD", "u3")])
    child_dir = tmp_path / "account" / "session" / "subagents"
    child_dir.mkdir(parents=True)
    (child_dir / "agent-escape.jsonl").symlink_to(private)
    register(project, path=external)
    result = service.detail(project, "s1")
    assert [m["text"] for m in result["messages"]] == ["registered"]
    assert result["children"] == []
    assert "account" not in json.dumps(result)


def test_content_and_scan_budgets_are_explicit(project, monkeypatch):
    register(project)
    write_jsonl(transcripts.local_path(project, "s1"), [message("user", "x" * 5000, "u1")])
    monkeypatch.setattr(service, "MAX_CONTENT_CHARS", 300)
    result = service.detail(project, "s1")
    assert len(result["messages"][0]["text"]) <= 300
    assert result["messages"][0]["truncated"]
    assert result["coverage"]["truncated"] and result["coverage"]["state"] == "partial"
    monkeypatch.setattr(service, "MAX_READ_BYTES", 100)
    result = service.detail(project, "s1")
    assert result["coverage"]["bytes_read"] <= 100
    assert not result["coverage"]["total_is_exact"]


def test_credentials_are_redacted_from_text_and_structured_tool_values(project):
    register(project)
    write_jsonl(transcripts.local_path(project, "s1"), [message("user", "Authorization: Bearer abcdef-secret", "u1"),
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "c1", "name": "request", "input": {"api_key": "secret-value", "path": "source.py"}}]}}])
    result = service.detail(project, "s1")
    encoded = json.dumps(result)
    assert "abcdef-secret" not in encoded and "secret-value" not in encoded
    assert "source.py" in encoded and "[redacted]" in encoded


def test_unknown_session_does_not_create_runtime_state(tmp_path):
    with pytest.raises(KeyError):
        service.detail(str(tmp_path), "missing")
    assert list(tmp_path.iterdir()) == []


def test_repeated_source_identity_is_not_a_second_message(project):
    register(project)
    first = message("user", "same source event", "source-u1")
    write_jsonl(transcripts.local_path(project, "s1"), [first, first, message("user", "same source event", "source-u2")])
    result = service.detail(project, "s1")
    assert len(result["messages"]) == 2
    assert len({m["id"] for m in result["messages"]}) == 2


def test_record_fallback_respects_shared_read_budget(project, monkeypatch):
    register(project)
    conn = db.connect(project)
    for n in range(8):
        db.append_event(conn, session="s1", actor="codex", kind="session-checkpoint", data={"note": str(n) * 1000})
    conn.close()
    monkeypatch.setattr(service, "MAX_READ_BYTES", 1500)
    result = service.detail(project, "s1")
    assert result["coverage"]["bytes_read"] <= 1500
    assert result["coverage"]["truncated"] and not result["coverage"]["total_is_exact"]
    assert len(result["messages"]) < 8


def test_symlinked_child_directory_is_not_an_authorized_source(project, tmp_path):
    parent = write_jsonl(tmp_path / "account" / "session.jsonl", [message("user", "parent", "u1")])
    external = tmp_path / "unrelated"
    write_jsonl(external / "agent-private.jsonl", [message("user", "private", "p1")])
    children = parent.parent / parent.stem / "subagents"
    children.parent.mkdir()
    children.symlink_to(external, target_is_directory=True)
    register(project, path=parent)
    assert service.detail(project, "s1")["children"] == []


def test_codex_mirrors_with_provider_timestamp_skew_and_analysis_channel(project):
    register(project)
    write_jsonl(transcripts.local_path(project, "s1"), [
        {"type": "event_msg", "timestamp": "2026-09-22T01:00:00.100Z", "payload": {"type": "agent_message", "message": "Visible answer", "turn_id": "turn1"}},
        {"type": "response_item", "timestamp": "2026-09-22T01:00:00.101Z", "payload": {"type": "message", "role": "assistant", "turn_id": "turn1", "content": [{"type": "output_text", "text": "Visible answer"}]}},
        {"type": "response_item", "payload": {"type": "message", "role": "assistant", "channel": "analysis", "content": [{"type": "output_text", "text": "PRIVATE ANALYSIS"}]}},
    ])
    result = service.detail(project, "s1")
    assert [m["text"] for m in result["messages"]] == ["Visible answer"]


def test_invalid_cursor_is_rejected_instead_of_resetting_history(project):
    register(project)
    with pytest.raises(ValueError):
        service.detail(project, "s1", before="a" * 24)


def test_cursor_survives_registered_source_expiry_when_local_copy_remains(project, tmp_path):
    records = [message("user", "first", "u1"), message("assistant", "second", "a1")]
    registered = write_jsonl(tmp_path / "account" / "source.jsonl", records)
    write_jsonl(transcripts.local_path(project, "s1"), records)
    register(project, path=registered)
    page = service.detail(project, "s1", limit=1)
    registered.unlink()
    previous = service.detail(project, "s1", before=page["next_before"], limit=1)
    assert previous["messages"][0]["text"] == "first"

def test_codex_rollup_feeds_summary_collection_without_fabricated_usage(project):
    from alpaca.analytics import build_index
    from alpaca import db
    from pathlib import Path
    import json
    path=Path(project)/'.alpaca/transcripts/codex-one.jsonl'
    path.parent.mkdir(parents=True,exist_ok=True)
    rows=[{'type':'event_msg','timestamp':'2026-09-22T01:00:00Z','payload':{'type':'user_message','message':'Explain the run result'}},
          {'type':'response_item','timestamp':'2026-09-22T01:00:01Z','payload':{'type':'function_call','name':'exec_command','call_id':'c1','arguments':'{"cmd":"pwd"}'}},
          {'type':'response_item','timestamp':'2026-09-22T01:00:02Z','payload':{'type':'function_call_output','call_id':'c1','output':'project'}},
          {'type':'event_msg','timestamp':'2026-09-22T01:00:03Z','payload':{'type':'agent_message','message':'The recorded test passed.'}}]
    path.write_text(''.join(json.dumps(r)+'\n' for r in rows))
    conn=db.connect(project)
    db.upsert(conn,'sessions','sid',{'sid':'codex-one','transcript':str(path),'started':'2026-09-22T01:00:00Z'})
    db.meta_set(conn,'operator:codex-one','codex');conn.close()
    item=build_index.collect(project,force=True,only='codex-one')[0]
    assert item['title']=='Explain the run result'
    assert item['tool_calls']==1 and item['tools']=={'exec_command':1}
    assert item['human_prompts'][0]['text']=='Explain the run result'
    assert item['hourly']['2026-09-22T01']['tools']==1
    assert item['usage_available'] is False and item['cost_reported_usd'] is None

def test_oversized_image_record_does_not_hide_later_work(project, monkeypatch):
    register(project)
    monkeypatch.setattr(service, 'MAX_LINE_BYTES', 300)
    write_jsonl(transcripts.local_path(project,'s1'), [message('user','Start','u1'),
        {'type':'response_item','payload':{'type':'function_call_output','call_id':'image','output':'x'*1200}},
        message('assistant','Latest result remains visible','a1')])
    result=service.detail(project,'s1')
    assert any(m['text']=='Latest result remains visible' for m in result['messages'])
    assert result['coverage']['truncated'] and not result['coverage']['total_is_exact']


def test_session_title_uses_request_instead_of_bootstrap_context(project):
    register(project)
    write_jsonl(transcripts.local_path(project,'s1'), [message('user','<recommended_plugins>context</recommended_plugins>','u1'),message('user','Explain the simulation result','u2')])
    assert service.detail(project,'s1')['session']['title']=='Explain the simulation result'

def test_codex_pool_append_invalidates_summary_and_live_revision(project):
    from alpaca import serve
    from alpaca.analytics import build_index
    path=write_jsonl(transcripts.local_path(project,'s1'), [message('user','Read project state','u1')])
    register(project, path=str(path))
    first=build_index.collect(project,force=True,only='s1')[0]
    revision=serve._data_sig(project)
    assert first['tool_calls']==0
    write_jsonl(pool.path(project,'s1'), [{'sid':'s1','phase':'pre','tool':'Bash','tool_use_id':'new-call','input':{'command':'pwd'},'ts':'2026-09-22T01:02:00Z'}])
    refreshed=build_index.collect(project,only='s1')[0]
    assert refreshed['tool_calls']==0  # Pool-only calls have no parent conversation membership.
    assert refreshed['_src']!=first['_src']
    assert serve._data_sig(project)!=revision
