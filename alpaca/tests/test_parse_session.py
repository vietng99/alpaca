import os
from alpaca.analytics import parse_session as ps

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "transcript_small.jsonl")

def test_parse_counts():
    d = ps.parse(FIX)
    assert d["sid"] == "transcript_small" and d["turns"] == 2 and d["tool_calls"] == 2
    assert d["tools"] == {"Edit": 1, "Bash": 1} and d["files"] == {"/p/src/cli.py": 1}
    assert [p["text"] for p in d["human_prompts"]] == ["add a hello verb", "also write a test"]
    assert d["tokens"] == {"input": 1715, "output": 70, "cache_read": 1500, "cache_write_5m": 200, "cache_write_1h": 0}
    assert d["usage_available"] is True and d["cost_notional_usd"] == 0.00153
    assert d["cost_provenance"] == "rate_card_estimate"
    assert d["models"] == {"claude-sonnet-5": 2} and d["hooks"] == {"UserPromptSubmit": 1}
    assert d["span_s"] == 5400 and len(d["hourly"]) == 2

def test_notional_cost_by_model():
    u = {"input_tokens": 1_000_000, "output_tokens": 0, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
    assert ps.notional_cost("claude-sonnet-5", u) == 2.0
    assert ps.notional_cost("claude-opus-5", u) == 5.0
    assert ps.notional_cost("claude-haiku-4-5", u) == 1.0


def test_missing_or_malformed_usage_is_unknown_not_zero(tmp_path):
    import json
    p = tmp_path / "missing.jsonl"
    records = [
        {"type": "assistant", "timestamp": "2026-09-15T10:00:00.000Z",
         "message": {"model": "claude-sonnet-5", "content": []}},
        {"type": "assistant", "timestamp": "2026-09-15T10:01:00.000Z",
         "message": {"model": "claude-sonnet-5", "content": [],
                     "usage": {"input_tokens": 2, "output_tokens": 3,
                               "cache_creation": {"ephemeral_5m_input_tokens": "bad"}}}},
    ]
    p.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    d = ps.parse(str(p))
    assert d["usage_available"] is False
    assert d["tokens"]["output"] == 3  # measured lower bound, first response is unknown
    assert d["coverage"]["totals_are_lower_bounds"]
    assert d["cost_notional_usd"] is None


def test_unknown_model_has_unknown_notional_price(tmp_path):
    import json
    p = tmp_path / "unknown-model.jsonl"
    p.write_text(json.dumps({"type": "assistant", "timestamp": "2026-09-15T10:00:00.000Z",
                             "message": {"model": "gpt-5.6-codex", "content": [],
                                         "usage": {"input_tokens": 1, "output_tokens": 2,
                                                   "cache_read_input_tokens": 0,
                                                   "cache_creation": {"ephemeral_5m_input_tokens": 0,
                                                                      "ephemeral_1h_input_tokens": 0}}}}) + "\n",
                 encoding="utf-8")
    d = ps.parse(str(p))
    assert d["usage_available"] is True
    assert d["cost_notional_usd"] is None
    assert d["cost_available"] is False


def test_partial_cache_usage_stays_partial_not_zero(tmp_path):
    import json
    p = tmp_path / "partial-cache.jsonl"
    p.write_text(json.dumps({"type": "assistant", "timestamp": "2026-09-15T10:00:00.000Z",
                             "message": {"model": "claude-sonnet-5", "content": [],
                                         "usage": {"input_tokens": 4, "output_tokens": 5}}}) + "\n",
                 encoding="utf-8")
    d = ps.parse(str(p))
    assert d["tokens"] == {"input": None, "output": 5, "cache_read": None,
                           "cache_write_5m": None, "cache_write_1h": None}
    assert d["usage_available"] is False and d["cost_notional_usd"] is None


def test_user_only_transcript_has_unknown_tokens_not_zero(tmp_path):
    import json
    p = tmp_path / "user-only.jsonl"
    p.write_text(json.dumps({"type": "user", "timestamp": "2026-09-15T10:00:00.000Z",
                             "message": {"content": "hello"}}) + "\n", encoding="utf-8")
    d = ps.parse(str(p))
    assert d["usage_available"] is False
    assert d["tokens"] == {"input": None, "output": None, "cache_read": None,
                           "cache_write_5m": None, "cache_write_1h": None}
    assert d["cost_notional_usd"] is None


def test_explicit_all_zero_usage_remains_measured_zero(tmp_path):
    import json
    p = tmp_path / "zero-usage.jsonl"
    p.write_text(json.dumps({"type": "assistant", "timestamp": "2026-09-15T10:00:00.000Z",
                             "message": {"model": "claude-sonnet-5", "content": [],
                                         "usage": {"input_tokens": 0, "output_tokens": 0,
                                                   "cache_read_input_tokens": 0,
                                                   "cache_creation": {"ephemeral_5m_input_tokens": 0,
                                                                      "ephemeral_1h_input_tokens": 0}}}}) + "\n",
                 encoding="utf-8")
    d = ps.parse(str(p))
    assert d["usage_available"] is True
    assert all(value == 0 for value in d["tokens"].values())
    assert d["cost_notional_usd"] == 0.0

def test_ishuman_filters_injected_text():
    assert ps.ishuman("fix the bug") and not ps.ishuman("<system-reminder>x") and not ps.ishuman("")

def test_subagent_transcripts_fold_in(tmp_path):
    import shutil, json
    main = tmp_path / "abc.jsonl"; shutil.copy(FIX, main)
    sub = tmp_path / "abc" / "subagents" / "x"; sub.mkdir(parents=True)
    rec = {"type": "assistant", "timestamp": "2026-09-15T10:05:00.000Z", "message": {"model": "claude-haiku-4-5", "content": [{"type": "tool_use", "name": "Read", "input": {"file_path": "/p/a"}}], "usage": {"input_tokens": 1, "output_tokens": 2}}}
    (sub / "agent-1.jsonl").write_text(json.dumps(rec) + "\n" + json.dumps({"type": "user", "timestamp": "2026-09-15T10:05:01.000Z", "message": {"content": "<agent-message from=\"orch\">go</agent-message>"}}) + "\n", encoding="utf-8")
    (sub / "agent-2.jsonl").write_text("{not json\n", encoding="utf-8")
    d = ps.parse(str(main))
    assert d["subagents"] == 2 and d["tool_calls"] == 3 and d["tools"]["Read"] == 1
    assert len(d["human_prompts"]) == 2 and d["models"]["claude-haiku-4-5"] == 1
    assert d["usage_available"] is False


def test_subagent_content_participates_in_source_signature(tmp_path):
    p = tmp_path / "abc.jsonl"
    p.write_text("", encoding="utf-8")
    sub = tmp_path / "abc" / "subagents"; sub.mkdir(parents=True)
    agent = sub / "agent-1.jsonl"
    agent.write_text("first\n", encoding="utf-8")
    first = ps.source_signature(str(p))
    agent.write_text("other\n", encoding="utf-8")
    assert ps.source_signature(str(p)) != first


def test_malformed_subagent_invalidates_otherwise_complete_usage(tmp_path):
    import json
    main = tmp_path / "abc.jsonl"
    main.write_text(json.dumps({"type": "assistant", "timestamp": "2026-09-15T10:00:00.000Z",
                                "message": {"model": "claude-sonnet-5", "content": [],
                                            "usage": {"input_tokens": 1, "output_tokens": 2,
                                                      "cache_read_input_tokens": 0,
                                                      "cache_creation": {"ephemeral_5m_input_tokens": 0,
                                                                         "ephemeral_1h_input_tokens": 0}}}}) + "\n",
                         encoding="utf-8")
    sub = tmp_path / "abc" / "subagents"; sub.mkdir(parents=True)
    (sub / "agent-1.jsonl").write_text("{bad json\n", encoding="utf-8")
    d = ps.parse(str(main))
    assert d["usage_available"] is False
    assert d["tokens"]["input"] == 1 and d["cost_notional_usd"] is None
    assert d["coverage"]["totals_are_lower_bounds"]

def test_pasted_prompt_as_text_blocks_counts(tmp_path):
    import json
    p = tmp_path / "s.jsonl"
    p.write_text(json.dumps({"type": "user", "timestamp": "2026-09-15T10:00:00.000Z", "message": {"content": [{"type": "text", "text": "pasted prompt"}]}}) + "\n", encoding="utf-8")
    assert [x["text"] for x in ps.parse(str(p))["human_prompts"]] == ["pasted prompt"]
