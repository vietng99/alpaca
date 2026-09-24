"""Recorded rate cards: parse the official price page, record with provenance, reprice analytics.

No test touches the network: the page comes from a trimmed copy of the official pricing page
saved on 2026-09-25 (fixtures/anthropic_pricing_trimmed.md) and the fetcher is injected.
"""
import hashlib
import json
import os
import re
from pathlib import Path

import pytest

from alpaca import cli, db, transcripts
from alpaca.analytics import metrics_index, pricing, ratecards

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "anthropic_pricing_trimmed.md")
PAGE = Path(FIXTURE).read_text(encoding="utf-8")
PAGE_BYTES = PAGE.encode("utf-8")
PAGE_SHA = hashlib.sha256(PAGE_BYTES).hexdigest()
OPUS_5_ROW_RATES = ("5", "0.5", "6.25", "10", None, "25")
VECTOR = {"input_tokens": 4500, "uncached_input_tokens": 1000, "cached_input_tokens": 2000,
          "cache_write_input_tokens": 1500, "cache_creation_5m_tokens": 1000,
          "cache_creation_1h_tokens": 500, "output_tokens": 100}


# --- transcript helpers -------------------------------------------------------------------------

def write(path, records):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records))
    return path


def register(root, sid, operator="claude"):
    conn = db.connect(root)
    db.upsert(conn, "sessions", "sid", {"sid": sid, "started": "2026-09-25T00:00:00Z"})
    db.meta_set(conn, "operator:" + sid, operator)
    conn.close()


def claude(ident, model, usage=None):
    return {"type": "assistant", "uuid": "u-" + ident, "timestamp": "2026-09-25T01:02:03Z",
            "requestId": "request-" + ident, "message": {
                "id": ident, "model": model,
                "usage": usage if usage is not None else {
                    "input_tokens": 1000, "output_tokens": 100, "cache_read_input_tokens": 2000,
                    "cache_creation_input_tokens": 1500,
                    "cache_creation": {"ephemeral_5m_input_tokens": 1000,
                                       "ephemeral_1h_input_tokens": 500}},
                "content": [{"type": "text", "text": "Visible result"}]}}


def codex_response(ident, model):
    return {"type": "token_usage_record", "timestamp": "2026-09-25T02:00:00Z",
            "payload": {"response_id": ident, "model": model, "usage": {
                "input_tokens": 100, "output_tokens": 10, "cached_input_tokens": 40,
                "cache_write_input_tokens": 0, "total_tokens": 110}}}


def seed_gaps(root):
    """s1: two responses from a Claude model with no card, one unpriced for another reason.
    s2: one OpenAI response from a model with no card."""
    missing_duration = claude("r3", "claude-opus-5", usage={
        "input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 7})
    write(transcripts.local_path(root, "s1"),
          [claude("r1", "claude-opus-4-7"), claude("r2", "claude-opus-4-7"), missing_duration])
    register(root, "s1")
    write(transcripts.local_path(root, "s2"), [codex_response("c1", "gpt-7-nova")])
    register(root, "s2", operator="codex")


def events(root):
    conn = db.connect_readonly(root)
    try:
        return db.events(conn, kind=ratecards.EVENT, limit=1000)
    finally:
        conn.close()


def cards(root):
    conn = db.connect_readonly(root)
    try:
        return ratecards.load(conn)
    finally:
        conn.close()


def record_opus_4_7(root, **overrides):
    # An official-page-parse label must point at a saved copy that hashes to source_sha256.
    copy = Path(root, ".alpaca", "analytics", "pricing-sources", PAGE_SHA + ".md")
    copy.parent.mkdir(parents=True, exist_ok=True)
    copy.write_bytes(PAGE_BYTES)
    values = dict(provider="anthropic", rates=OPUS_5_ROW_RATES, source_url=pricing.CLAUDE_PRICING,
                  method="official-page-parse", source_sha256=PAGE_SHA,
                  source_copy=".alpaca/analytics/pricing-sources/%s.md" % PAGE_SHA,
                  row="| Claude Opus 4.7 | $5 / MTok | $6.25 / MTok | $10 / MTok | $0.50 / MTok | $25 / MTok |")
    values.update(overrides)
    return ratecards.record(root, "claude-opus-4-7", **values)


# --- names --------------------------------------------------------------------------------------

@pytest.mark.parametrize("model,name", [
    ("claude-opus-5-5", "Claude Opus 5.5"),
    ("claude-haiku-4-5-20251001", "Claude Haiku 4.5"),
    ("claude-opus-5", "Claude Opus 5"),
    ("claude-fable-5-1", "Claude Fable 5.1"),
    ("claude-opus-4-20250514", "Claude Opus 4"),
    ("claude-3-5-haiku-20241022", "Claude Haiku 3.5"),
    ("gpt-6-astra", None),
    ("claude-opus-5[1m]", None),
    ("unknown", None),
    ("", None),
    (None, None),
])
def test_anthropic_name(model, name):
    assert ratecards.anthropic_name(model) == name


# --- page parsing -------------------------------------------------------------------------------

def test_parse_reads_the_model_row_in_category_order():
    got = ratecards.parse_anthropic(PAGE, "claude-opus-5-5")
    assert got["name"] == "Claude Opus 5.5"
    assert got["rates"] == ("4", "0.2", "5", "8", None, "20")
    assert got["row"].startswith("| Claude Opus 5.5 ")
    assert got["fast_multiplier"] == "2"


def test_opus_5_does_not_match_opus_5_5_and_reads_the_shared_fast_row():
    got = ratecards.parse_anthropic(PAGE, "claude-opus-5")
    assert got["rates"] == OPUS_5_ROW_RATES
    assert got["row"].startswith("| Claude Opus 5 ")
    assert got["fast_multiplier"] == "2"


def test_footnotes_and_trailing_links_are_stripped():
    assert ratecards.parse_anthropic(PAGE, "claude-sonnet-5")["rates"] == ("2", "0.2", "2.5", "4", None, "10")
    retired = ratecards.parse_anthropic(PAGE, "claude-opus-4-1")
    assert retired["rates"] == ("15", "1.5", "18.75", "30", None, "75")
    limited = ratecards.parse_anthropic(PAGE, "claude-mythos-5-1")
    assert limited["rates"] == ("10", "0.25", "12.5", "20", None, "50")
    assert limited["fast_multiplier"] is None


def test_only_the_model_pricing_table_is_read():
    # The batch table lists Claude Haiku 4.5 at $0.50 input; the model table says $1.
    got = ratecards.parse_anthropic(PAGE, "claude-haiku-4-5-20251001")
    assert got["rates"] == ("1", "0.1", "1.25", "2", None, "5")
    assert got["fast_multiplier"] is None


def test_refuses_a_changed_header():
    with pytest.raises(ValueError, match="header"):
        ratecards.parse_anthropic(PAGE.replace("| 5m cache writes |", "| Cache writes    |"), "claude-opus-5")


def test_refuses_an_extra_column():
    changed = PAGE.replace("| Output tokens          |", "| Output tokens          | Batch |", 1)
    with pytest.raises(ValueError, match="header"):
        ratecards.parse_anthropic(changed, "claude-opus-5")


def test_refuses_a_page_without_the_model_table():
    with pytest.raises(ValueError, match="Model pricing"):
        ratecards.parse_anthropic(PAGE.replace("## Model pricing", "## Prices"), "claude-opus-5")


def test_refuses_a_missing_row():
    with pytest.raises(ValueError, match="no row"):
        ratecards.parse_anthropic(PAGE, "claude-opus-9")


def test_refuses_two_matching_rows():
    row = next(line for line in PAGE.splitlines() if line.startswith("| Claude Opus 5 "))
    with pytest.raises(ValueError, match="2 rows"):
        ratecards.parse_anthropic(PAGE.replace(row, row + "\n" + row), "claude-opus-5")


def test_refuses_an_unparseable_price_cell():
    row = next(line for line in PAGE.splitlines() if line.startswith("| Claude Opus 5.5 "))
    with pytest.raises(ValueError, match="price"):
        ratecards.parse_anthropic(PAGE.replace(row, row.replace("$4 / MTok ", "$4 per MTok")), "claude-opus-5-5")


def test_refuses_a_non_claude_model():
    with pytest.raises(ValueError):
        ratecards.parse_anthropic(PAGE, "gpt-6-astra")


def test_fast_multiplier_needs_agreeing_input_and_output_ratios():
    changed = PAGE.replace("| Claude Opus 5.5                 | $8 / MTok  | $40 / MTok |",
                           "| Claude Opus 5.5                 | $8 / MTok  | $45 / MTok |")
    assert changed != PAGE
    got = ratecards.parse_anthropic(changed, "claude-opus-5-5")
    assert got["rates"] == ("4", "0.2", "5", "8", None, "20")
    assert got["fast_multiplier"] is None


# --- fetch --------------------------------------------------------------------------------------

def test_fetch_saves_the_page_once_and_returns_label_pieces(project):
    calls = []

    def fetch(url):
        calls.append(url)
        return PAGE_BYTES

    got = ratecards.fetch_anthropic(project, "claude-opus-4-7", fetch=fetch)
    assert calls == [ratecards.ANTHROPIC_PRICING_MD]
    assert got["model"] == "claude-opus-4-7" and got["provider"] == "anthropic"
    assert got["rates"] == OPUS_5_ROW_RATES
    assert got["source_url"] == pricing.CLAUDE_PRICING
    assert ratecards.ANTHROPIC_PRICING_MD in got["source_urls"]
    label = got["label"]
    assert label["method"] == "official-page-parse"
    assert label["source_sha256"] == PAGE_SHA
    assert label["source_copy"] == ".alpaca/analytics/pricing-sources/%s.md" % PAGE_SHA
    assert label["row"].startswith("| Claude Opus 4.7 ")
    copy = Path(project, label["source_copy"])
    assert copy.read_bytes() == PAGE_BYTES
    stamp = copy.stat().st_mtime_ns
    ratecards.fetch_anthropic(project, "claude-opus-4-6", fetch=fetch)
    assert copy.stat().st_mtime_ns == stamp
    assert len(list(copy.parent.iterdir())) == 1


def test_fetch_refuses_a_plain_http_url(project):
    with pytest.raises(ValueError, match="https"):
        ratecards.fetch_anthropic(project, "claude-opus-4-7", url="http://example.com/pricing.md",
                                  fetch=lambda url: PAGE_BYTES)


# --- record and load ----------------------------------------------------------------------------

def test_record_appends_one_event_and_load_returns_the_card(project):
    out = record_opus_4_7(project)
    assert out["status"] == "recorded"
    rows = events(project)
    assert len(rows) == 1
    assert rows[0]["ref"] == "claude-opus-4-7"
    assert rows[0]["actor"] == "analytics"
    assert rows[0]["session"] == "cli"
    card = cards(project)["claude-opus-4-7"]
    assert card["provider"] == "anthropic"
    assert card["rates"] == OPUS_5_ROW_RATES
    assert card["source_url"] == pricing.CLAUDE_PRICING
    assert card["source_urls"] == [pricing.CLAUDE_PRICING]
    assert card["context_threshold_tokens"] is None
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", card["verified_at"])
    assert "fast_multiplier" not in card or card["fast_multiplier"] is None
    label = card["label"]
    assert label["method"] == "official-page-parse"
    assert label["source_sha256"] == PAGE_SHA
    assert label["source_copy"].endswith(PAGE_SHA + ".md")
    assert label["row"].startswith("| Claude Opus 4.7 ")
    assert label["verified_by"] == "cli"
    assert label["recorded_at"]


def test_record_is_attributed_to_the_running_session(project, monkeypatch):
    monkeypatch.setenv("ALPACA_SESSION_ID", "session-42")
    record_opus_4_7(project)
    assert events(project)[0]["session"] == "session-42"
    assert cards(project)["claude-opus-4-7"]["label"]["verified_by"] == "session-42"


def test_an_identical_card_is_reported_as_duplicate(project):
    record_opus_4_7(project)
    before = ratecards.revision(project)
    out = record_opus_4_7(project)
    assert out["status"] == "duplicate"
    assert len(events(project)) == 1
    assert ratecards.revision(project) == before


def test_a_changed_card_appends_and_the_latest_wins(project):
    record_opus_4_7(project)
    out = record_opus_4_7(project, rates=("5", "0.5", "6.25", "10", None, "30"))
    assert out["status"] == "recorded"
    assert len(events(project)) == 2
    assert cards(project)["claude-opus-4-7"]["rates"][5] == "30"


def test_rates_may_be_given_by_category_name(project):
    record_opus_4_7(project, rates={"uncached_input": "5", "cached_input": ".5", "cache_write_5m": "6.25",
                                    "cache_write_1h": 10, "output": "25"})
    assert cards(project)["claude-opus-4-7"]["rates"] == OPUS_5_ROW_RATES


def test_dry_run_records_nothing(project):
    out = record_opus_4_7(project, dry_run=True)
    assert out["status"] == "dry-run"
    assert out["card"]["rates"] == OPUS_5_ROW_RATES
    assert events(project) == []


@pytest.mark.parametrize("overrides,message", [
    ({"source_url": "http://platform.claude.com/docs/en/about-claude/pricing"}, "https"),
    ({"source_url": "not a url"}, "https"),
    ({"rates": ("5", "0.5", "6.25", None, None, "25")}, "cache_write_1h"),
    ({"rates": ("5", "-0.5", "6.25", "10", None, "25")}, "cached_input"),
    ({"rates": ("5", "abc", "6.25", "10", None, "25")}, "cached_input"),
    ({"rates": ("5", "NaN", "6.25", "10", None, "25")}, "cached_input"),
    ({"rates": ("5", "Infinity", "6.25", "10", None, "25")}, "cached_input"),
    ({"rates": ("5", "0.5", "6.25", "10", "7", "25")}, "cache_write"),
    ({"rates": ("5", "0.5")}, "rates"),
    ({"provider": "azure"}, "provider"),
    ({"method": "guess"}, "method"),
    ({"fast_multiplier": "0"}, "fast"),
    ({"source_copy": ".alpaca/analytics/pricing-sources/missing.md"}, "source_copy"),
    ({"source_copy": "../outside.md"}, "source_copy"),
    ({"source_sha256": "0" * 64}, "source_copy"),
    ({"row": None}, "official-page-parse"),
])
def test_record_refuses_invalid_cards(project, overrides, message):
    with pytest.raises(ValueError, match=message):
        record_opus_4_7(project, **overrides)
    assert events(project) == []


def test_openai_cards_need_a_cache_write_rate(project):
    with pytest.raises(ValueError, match="cache_write"):
        ratecards.record(project, "gpt-7-nova", provider="openai", rates=("2", "0.2", None, None, None, "8"),
                         source_url=pricing.OPENAI_PRICING, method="agent-entered")
    out = ratecards.record(project, "gpt-7-nova", provider="openai", rates=("2", "0.2", None, None, "2.5", "8"),
                           source_url=pricing.OPENAI_PRICING, method="agent-entered")
    assert out["status"] == "recorded"
    assert cards(project)["gpt-7-nova"]["label"]["method"] == "agent-entered"


def test_a_model_with_a_built_in_card_is_refused(project):
    with pytest.raises(ValueError, match="built-in"):
        ratecards.record(project, "claude-opus-5-5", provider="anthropic", rates=OPUS_5_ROW_RATES,
                         source_url=pricing.CLAUDE_PRICING, method="agent-entered")
    assert events(project) == []


def test_load_skips_malformed_payloads_and_keeps_the_last_good_card(project):
    record_opus_4_7(project)
    conn = db.connect(project)
    good = dict(json.loads(conn.execute("SELECT data FROM events WHERE kind=?", (ratecards.EVENT,)).fetchone()[0]))
    for bad in ({"model": "claude-opus-4-7"},
                {**good, "rates": ["5", "0.5"]},
                {**good, "provider": "azure"},
                {**good, "source_url": "http://example.com"},
                {**good, "verified_at": "yesterday"},
                {**good, "label": {"method": "guess"}},
                {**good, "model": "claude-opus-4-6", "rates": ["5", "x", "6.25", "10", None, "25"]}):
        db.append_event(conn, session="cli", actor="analytics", kind=ratecards.EVENT, ref="claude-opus-4-7", data=bad)
    db.append_event(conn, session="cli", actor="analytics", kind=ratecards.EVENT, ref="claude-sonnet-4-6",
                    data={**good, "model": "claude-sonnet-4-6", "rates": ["3", "0.3", "3.75", "6", None, "15"]})
    conn.close()
    loaded = cards(project)
    assert set(loaded) == {"claude-opus-4-7", "claude-sonnet-4-6"}
    assert loaded["claude-opus-4-7"]["rates"] == OPUS_5_ROW_RATES
    assert loaded["claude-sonnet-4-6"]["rates"] == ("3", "0.3", "3.75", "6", None, "15")


def test_load_without_a_record_is_empty(project):
    assert ratecards.load(None) == {}
    assert cards(project) == {}


def test_revision_moves_when_a_card_is_recorded(project):
    assert ratecards.revision(project) == ""
    record_opus_4_7(project)
    first = ratecards.revision(project)
    assert re.fullmatch(r"[0-9a-f]{64}", first)
    record_opus_4_7(project, rates=("5", "0.5", "6.25", "10", None, "30"))
    assert ratecards.revision(project) not in ("", first)


# --- pricing with recorded cards ----------------------------------------------------------------

def test_estimate_uses_a_recorded_card_after_the_built_in_ones(project):
    record_opus_4_7(project)
    recorded = cards(project)
    result = pricing.estimate("claude-opus-4-7", VECTOR, cards=recorded)
    assert result["status"] == "estimated"
    assert result["total_usd"] == pytest.approx(0.01975)
    card = result["rate_card"]
    assert card["origin"] == "recorded"
    assert card["label"] == recorded["claude-opus-4-7"]["label"]
    assert card["verified_at"] == recorded["claude-opus-4-7"]["verified_at"]
    assert recorded["claude-opus-4-7"]["verified_at"] in result["assumptions"][1]
    json.dumps(result, allow_nan=False)
    # A recorded card never overrides a built-in one.
    shadow = {"claude-opus-5": dict(recorded["claude-opus-4-7"], rates=("50", "5", "62.5", "100", None, "250"))}
    built_in = pricing.estimate("claude-opus-5", VECTOR, cards=shadow)
    assert built_in["total_usd"] == pytest.approx(0.01975)
    assert built_in["rate_card"]["origin"] == "built-in"
    assert "label" not in built_in["rate_card"]


def test_recorded_fast_mode_needs_a_recorded_multiplier(project):
    record_opus_4_7(project)
    plain = cards(project)
    fast = dict(VECTOR, speed="fast")
    assert pricing.estimate("claude-opus-4-7", fast, cards=plain)["reason_code"] == "unverified_service_tier"
    record_opus_4_7(project, fast_multiplier="2")
    with_fast = cards(project)
    assert with_fast["claude-opus-4-7"]["fast_multiplier"] == "2"
    result = pricing.estimate("claude-opus-4-7", fast, cards=with_fast)
    assert result["total_usd"] == pytest.approx(2 * 0.01975)
    assert result["rate_card"]["service_tier"] == "fast"


def test_unknown_model_reason_names_the_model_and_the_fix():
    result = pricing.estimate("claude-opus-9", VECTOR)
    assert result["reason_code"] == "unverified_model"
    assert result["reason"] == ("No verified rate card for claude-opus-9. Record one from the official "
                                "price page: alpaca analytics price-check --model claude-opus-9")


# --- analytics integration ----------------------------------------------------------------------

def test_recording_a_card_reprices_a_cached_analysis(project):
    from alpaca.analytics import metrics
    write(transcripts.local_path(project, "s1"), [claude("r1", "claude-opus-4-7")])
    register(project, "s1")
    before = metrics.analyze(project, "s1")
    assert before["summary"]["costed_responses"] == 0
    assert before["ledger"][0]["cost"]["reason_code"] == "unverified_model"
    record_opus_4_7(project)
    after = metrics.analyze(project, "s1")
    assert after["summary"]["costed_responses"] == 1
    assert after["summary"]["estimated_cost_usd"] == pytest.approx(0.01975)
    assert after["ledger"][0]["cost"]["rate_card"]["origin"] == "recorded"


def test_index_lists_unpriced_models_with_their_fix(project):
    seed_gaps(project)
    sessions = [{"sid": "s1", "class": "work"}, {"sid": "s2", "class": "work"}]
    totals = metrics_index.index(project, sessions)["totals"]
    assert totals["unpriced_models"][0] == {
        "model": "claude-opus-4-7", "provider": "anthropic", "responses": 2, "sessions": 1,
        "fix": "bin/alpaca analytics price-check --model claude-opus-4-7"}
    other = totals["unpriced_models"][1]
    assert (other["model"], other["provider"], other["responses"], other["sessions"]) == ("gpt-7-nova", "openai", 1, 1)
    assert other["fix"].startswith("bin/alpaca analytics price-label --model gpt-7-nova --provider openai --source ")
    assert "--cache-write" in other["fix"] and "--cache-write-5m" not in other["fix"]
    assert len(totals["unpriced_models"]) == 2
    assert totals["unpriced_other"] == 1
    record_opus_4_7(project)
    totals = metrics_index.index(project, sessions)["totals"]
    assert [row["model"] for row in totals["unpriced_models"]] == ["gpt-7-nova"]
    assert totals["unpriced_other"] == 1


def test_gaps_scans_every_transcript_session_or_the_ones_given(project):
    seed_gaps(project)
    assert [row["model"] for row in ratecards.gaps(project)] == ["claude-opus-4-7", "gpt-7-nova"]
    assert [row["model"] for row in ratecards.gaps(project, sessions=["s2"])] == ["gpt-7-nova"]


def test_fix_command_shapes():
    assert ratecards.fix_command("claude-opus-4-7", "anthropic") == \
        "bin/alpaca analytics price-check --model claude-opus-4-7"
    openai = ratecards.fix_command("gpt-7-nova", "openai")
    assert openai.startswith("bin/alpaca analytics price-label --model gpt-7-nova --provider openai --source ")
    assert "--cache-write C" in openai
    # A Claude-hosted response whose ID is not a Claude model name cannot be looked up by name.
    odd = ratecards.fix_command("mystery-model", "anthropic")
    assert "price-label" in odd and "--cache-write-5m" in odd and "--cache-write-1h" in odd


# --- pricing gap notice file --------------------------------------------------------------------

def gap(model, provider="anthropic", responses=3):
    return {"model": model, "provider": provider, "responses": responses, "sessions": 1,
            "fix": ratecards.fix_command(model, provider)}


def test_write_gaps_and_read_gaps(project):
    path = Path(project, ratecards.GAPS_FILE)
    assert ratecards.GAPS_FILE == os.path.join(".alpaca", "analytics", "pricing-gaps.json")
    assert ratecards.read_gaps(project) == []
    entries = [gap("claude-opus-4-7"), gap("gpt-7-nova", "openai", 1)]
    first = ratecards.write_gaps(project, entries)
    assert first["written"] is True
    data = json.loads(path.read_text())
    assert data["schema"] == 2 and data["scopes"] == {"work": entries, "all": []} and data["revision"] == ""
    assert data["gaps"] == entries  # the union of the scopes, for plain readers
    assert data["written_at"]
    assert ratecards.read_gaps(project) == entries
    stamp = path.stat().st_mtime_ns
    assert ratecards.write_gaps(project, entries)["written"] is False
    assert path.stat().st_mtime_ns == stamp
    # Labelling a model drops it from the notice even before the file is rewritten.
    record_opus_4_7(project)
    assert ratecards.read_gaps(project) == [entries[1]]
    assert ratecards.write_gaps(project, [])["written"] is True
    assert json.loads(path.read_text())["gaps"] == []


def test_read_gaps_drops_built_in_models_and_survives_a_bad_file(project):
    path = Path(project, ratecards.GAPS_FILE)
    ratecards.write_gaps(project, [gap("claude-opus-5-5"), gap("claude-opus-9")])
    assert [row["model"] for row in ratecards.read_gaps(project)] == ["claude-opus-9"]
    path.write_text("{not json")
    assert ratecards.read_gaps(project) == []
    path.write_text(json.dumps({"schema": 1, "gaps": [{"model": 7}, "x", gap("claude-opus-9")]}))
    assert [row["model"] for row in ratecards.read_gaps(project)] == ["claude-opus-9"]


# --- CLI ----------------------------------------------------------------------------------------

def test_cli_price_check_refuses_a_built_in_model(project, capsys):
    assert cli.main(["analytics", "price-check", "--model", "claude-opus-5-5", "--dry-run"]) == cli.FAIL
    out = capsys.readouterr().out
    assert "built-in" in out and "GATE alpaca-analytics-price-check: FAIL" in out
    assert events(project) == []


def test_cli_price_check_dry_run_parses_but_records_nothing(project, capsys, monkeypatch):
    monkeypatch.setattr(ratecards, "_download", lambda url, timeout=30: PAGE_BYTES)
    assert cli.main(["analytics", "price-check", "--model", "claude-opus-4-7", "--dry-run"]) == cli.PASS
    out = capsys.readouterr().out
    assert "would record" in out and "claude-opus-4-7" in out
    assert events(project) == []
    assert not Path(project, ".alpaca", "analytics", "pricing-sources").exists()


def test_cli_price_check_labels_every_gap(project, capsys, monkeypatch):
    seed_gaps(project)
    seen = []
    monkeypatch.setattr(ratecards, "_download", lambda url, timeout=30: seen.append(url) or PAGE_BYTES)
    assert cli.main(["analytics", "price-check"]) == cli.FAIL  # the OpenAI gap needs price-label
    out = capsys.readouterr().out
    assert "claude-opus-4-7: recorded" in out
    assert "price-label --model gpt-7-nova --provider openai" in out
    assert seen == [ratecards.ANTHROPIC_PRICING_MD]
    assert set(cards(project)) == {"claude-opus-4-7"}
    assert [row["model"] for row in ratecards.read_gaps(project)] == ["gpt-7-nova"]
    notice = json.loads(Path(project, ratecards.GAPS_FILE).read_text())
    assert [row["model"] for row in notice["gaps"]] == ["gpt-7-nova"]
    # Once only the Claude gap is left and labelled, a rerun passes with nothing to do.
    assert cli.main(["analytics", "price-check", "--model", "claude-opus-4-7"]) == cli.PASS
    assert "duplicate" in capsys.readouterr().out
    assert len(events(project)) == 1


def test_cli_price_check_reports_a_network_failure(project, capsys, monkeypatch):
    def fail(url, timeout=30):
        raise OSError("network is unreachable")
    monkeypatch.setattr(ratecards, "_download", fail)
    assert cli.main(["analytics", "price-check", "--model", "claude-opus-4-7"]) == cli.FAIL
    assert "network is unreachable" in capsys.readouterr().out
    assert events(project) == []


def test_cli_price_label_records_an_agent_entered_card(project, capsys):
    argv = ["analytics", "price-label", "--model", "gpt-7-nova", "--provider", "openai",
            "--source", "https://developers.openai.com/api/docs/models/gpt-7-nova",
            "--input", "2", "--output", "8", "--cache-read", ".2", "--cache-write", "2.5"]
    assert cli.main(argv) == cli.PASS
    assert "recorded" in capsys.readouterr().out
    card = cards(project)["gpt-7-nova"]
    assert card["rates"] == ("2", "0.2", None, None, "2.5", "8")
    assert card["label"]["method"] == "agent-entered"
    assert card["label"]["source_sha256"] is None
    anthropic = ["analytics", "price-label", "--model", "claude-opus-4-7", "--provider", "anthropic",
                 "--source", pricing.CLAUDE_PRICING, "--input", "5", "--output", "25", "--cache-read", ".5",
                 "--cache-write-5m", "6.25"]
    assert cli.main(anthropic) == cli.FAIL
    assert "cache_write_1h" in capsys.readouterr().out
    assert cli.main(anthropic + ["--cache-write-1h", "10", "--fast-multiplier", "2"]) == cli.PASS
    assert cards(project)["claude-opus-4-7"]["fast_multiplier"] == "2"
    insecure = [a if a != pricing.CLAUDE_PRICING else "http://example.com" for a in anthropic]
    assert cli.main(insecure + ["--cache-write-1h", "10"]) == cli.FAIL
    built_in = ["analytics", "price-label", "--model", "claude-opus-5-5", "--provider", "anthropic",
                "--source", pricing.CLAUDE_PRICING, "--input", "4", "--output", "20", "--cache-read", ".2",
                "--cache-write-5m", "5", "--cache-write-1h", "8"]
    assert cli.main(built_in) == cli.FAIL
    assert "built-in" in capsys.readouterr().out


# --- review fix round (backend review findings 1-10) ------------------------------------------

def _append(root, data):
    conn = db.connect(root)
    try:
        db.append_event(conn, session="cli", actor="analytics", kind=ratecards.EVENT, ref=data.get("model"), data=data)
    finally:
        conn.close()


def _good_payload(root):
    record_opus_4_7(root)
    conn = db.connect_readonly(root)
    try:
        return dict(json.loads(conn.execute("SELECT data FROM events WHERE kind=?", (ratecards.EVENT,)).fetchone()[0]))
    finally:
        conn.close()


def test_f1_out_of_range_decimals_in_the_record_are_skipped_everywhere(project):
    from alpaca.analytics import metrics
    good = _good_payload(project)
    _append(project, dict(good, rates=["1E+999999999", "0.5", "6.25", "10", None, "25"]))
    _append(project, dict(good, fast_multiplier="1E+999999999"))
    _append(project, dict(good, model="claude-opus-4-6", rates=["5", "1E-999999999", "6.25", "10", None, "25"]))
    assert cards(project)["claude-opus-4-7"]["rates"] == OPUS_5_ROW_RATES
    assert set(cards(project)) == {"claude-opus-4-7"}
    write(transcripts.local_path(project, "s1"), [claude("r1", "claude-opus-4-7")])
    register(project, "s1")
    assert metrics.analyze(project, "s1")["summary"]["costed_responses"] == 1
    ratecards.write_gaps(project, [gap("claude-opus-9")])
    assert [row["model"] for row in ratecards.read_gaps(project)] == ["claude-opus-9"]
    assert metrics_index.index(project, [{"sid": "s1", "class": "work"}])["totals"]["unpriced_models"] == []
    out = ratecards.record(project, "gpt-7-nova", provider="openai", rates=("2", "0.2", None, None, "2.5", "8"),
                           source_url=pricing.OPENAI_PRICING, method="agent-entered")
    assert out["status"] == "recorded"


def test_f1_decimal_helpers_turn_overflow_into_refusals():
    assert pricing.decimal_rate("1E+999999999") is None
    assert pricing.decimal_rate("1E+400") is None
    with pytest.raises(ValueError):
        ratecards._decimal_text("1E+999999999", "output rate")


@pytest.mark.parametrize("overrides,message", [
    ({"rates": ("1e400", "0.5", "6.25", "10", None, "25")}, "uncached_input"),
    ({"rates": ("10001", "0.5", "6.25", "10", None, "25")}, "uncached_input"),
    ({"rates": ("0", "0.5", "6.25", "10", None, "25")}, "uncached_input"),
    ({"rates": ("5", "0.5", "6.25", "10", None, "0")}, "output"),
    ({"rates": ("5", "0.5", "6.25", "10000.5", None, "25")}, "cache_write_1h"),
    ({"rates": ("5", "0.0000000001", "6.25", "10", None, "25")}, "cached_input"),
    ({"fast_multiplier": "0.5"}, "fast"),
    ({"fast_multiplier": "11"}, "fast"),
    ({"fast_multiplier": "1E+400"}, "fast"),
    ({"source_url": "HTTPS://platform.claude.com/docs/en/about-claude/pricing"}, "https"),
])
def test_f2_f4_rates_multipliers_and_scheme_are_bounded(project, overrides, message):
    with pytest.raises(ValueError, match=message):
        record_opus_4_7(project, **overrides)
    assert events(project) == []


def test_f2_zero_cache_rates_and_bounds_are_allowed(project):
    out = record_opus_4_7(project, rates=("10000", "0", "0", "0", None, "10000"), fast_multiplier="10")
    assert out["status"] == "recorded"
    assert record_opus_4_7(project, fast_multiplier="1")["status"] == "recorded"


def test_f2_non_finite_total_is_unavailable_not_infinity(monkeypatch):
    huge = dict(pricing._CARDS["claude-opus-5"], rates=("1E+400", ".5", "6.25", "10", None, "25"))
    monkeypatch.setitem(pricing._CARDS, "claude-opus-5", huge)
    result = pricing.estimate("claude-opus-5", VECTOR)
    assert result["status"] == "unavailable"
    assert result["total_usd"] is None
    assert result["reason_code"] == "non_finite_total"
    json.dumps(result, allow_nan=False)


def test_f2_cli_price_label_refuses_an_absurd_rate(project, capsys):
    argv = ["analytics", "price-label", "--model", "claude-new-1", "--provider", "anthropic",
            "--source", pricing.CLAUDE_PRICING, "--input", "1e400", "--output", "25", "--cache-read", "0.5",
            "--cache-write-5m", "6.25", "--cache-write-1h", "10"]
    assert cli.main(argv) == cli.FAIL
    assert "GATE alpaca-analytics-price-label: FAIL" in capsys.readouterr().out
    assert events(project) == []


def test_f4_uppercase_scheme_is_refused_by_the_cli_and_skipped_by_load(project, capsys):
    argv = ["analytics", "price-label", "--model", "claude-new-2", "--provider", "anthropic",
            "--source", "HTTPS://platform.claude.com/docs/en/about-claude/pricing", "--input", "5",
            "--output", "25", "--cache-read", "0.5", "--cache-write-5m", "6.25", "--cache-write-1h", "10"]
    assert cli.main(argv) == cli.FAIL
    assert events(project) == []
    good = _good_payload(project)
    _append(project, dict(good, model="claude-new-2", source_url="HTTPS://platform.claude.com/x",
                          source_urls=["HTTPS://platform.claude.com/x"]))
    assert "claude-new-2" not in cards(project)


def test_f5_official_page_parse_is_tied_to_the_official_host(project):
    with pytest.raises(ValueError, match="platform.claude.com"):
        ratecards.fetch_anthropic(project, "claude-opus-4-7", url="https://any.example/pricing.md",
                                  fetch=lambda url: PAGE_BYTES)
    for final in ("https://evil.example/pricing.md", "http://platform.claude.com/docs/en/about-claude/pricing.md"):
        with pytest.raises(ValueError, match="redirect"):
            ratecards.fetch_anthropic(project, "claude-opus-4-7", fetch=lambda url, final=final: (PAGE_BYTES, final))
    assert not Path(project, ".alpaca", "analytics", "pricing-sources").exists()
    moved = "https://platform.claude.com/docs/en/about-claude/pricing-v2.md"
    pieces = ratecards.fetch_anthropic(project, "claude-opus-4-7", fetch=lambda url: (PAGE_BYTES, moved))
    assert pieces["source_url"] == moved[:-3]
    assert moved in pieces["source_urls"]
    with pytest.raises(ValueError, match="platform.claude.com"):
        record_opus_4_7(project, source_url="https://any.example/pricing")


def test_f5_redirect_guard_refuses_http_and_other_hosts():
    import urllib.request
    guard = ratecards._RedirectGuard()
    request = urllib.request.Request("https://platform.claude.com/docs/en/about-claude/pricing.md")
    for target in ("http://platform.claude.com/docs/x.md", "https://evil.example/x.md", "ftp://platform.claude.com/x"):
        with pytest.raises(ValueError, match="redirect"):
            guard.redirect_request(request, None, 302, "Found", {}, target)
    moved = guard.redirect_request(request, None, 301, "Moved", {}, "https://platform.claude.com/docs/y.md")
    assert moved.full_url == "https://platform.claude.com/docs/y.md"


def test_f5_cli_price_check_refuses_an_unofficial_url(project, capsys, monkeypatch):
    monkeypatch.setattr(ratecards, "_download", lambda url, timeout=30: PAGE_BYTES)
    argv = ["analytics", "price-check", "--model", "claude-opus-4-7", "--url", "https://any.example/pricing.md"]
    assert cli.main(argv) == cli.FAIL
    assert "platform.claude.com" in capsys.readouterr().out
    assert events(project) == []


def test_f6_footnote_stripping_is_linear_and_long_cells_are_refused():
    import time
    started = time.monotonic()
    assert ratecards._clean_name("<sup>" * 20000 + "Claude Opus 5") == "<sup>" * 20000 + "Claude Opus 5"
    assert ratecards._clean_name("Claude Opus 5<sup>1</sup> ([retired](https://x.example/a))") == "Claude Opus 5"
    assert time.monotonic() - started < 1.0
    row = next(line for line in PAGE.splitlines() if line.startswith("| Claude Opus 4.7 "))
    long_row = row.replace("| Claude Opus 4.7 ", "| Claude Opus 4.7" + " " * 2001, 1)
    with pytest.raises(ValueError, match="2000"):
        ratecards.parse_anthropic(PAGE.replace(row, long_row), "claude-opus-5")


def test_f6_download_has_an_overall_deadline(monkeypatch):
    import time

    def slow(url, timeout, deadline):
        time.sleep(3)
        return PAGE_BYTES, url

    monkeypatch.setattr(ratecards, "_read", slow)
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        ratecards._download(ratecards.ANTHROPIC_PRICING_MD, deadline=0.2)
    assert time.monotonic() - started < 2
    assert ratecards.DOWNLOAD_DEADLINE == 60


def test_f7_the_same_card_on_another_day_is_a_duplicate(project):
    assert record_opus_4_7(project, verified_at="2026-09-25")["status"] == "recorded"
    assert record_opus_4_7(project, verified_at="2026-09-26")["status"] == "duplicate"
    assert len(events(project)) == 1


def test_f9_gap_notice_keeps_one_list_per_scope_and_reads_the_union(project):
    ratecards.write_gaps(project, [gap("claude-opus-9", responses=3)])            # default scope: work
    ratecards.write_gaps(project, [gap("claude-opus-9", responses=5), gap("gpt-7-nova", "openai", 1)], scope="all")
    union = {row["model"]: row for row in ratecards.read_gaps(project)}
    assert set(union) == {"claude-opus-9", "gpt-7-nova"}
    assert union["claude-opus-9"]["responses"] == 5
    ratecards.write_gaps(project, [], scope="work")
    assert {row["model"] for row in ratecards.read_gaps(project)} == {"claude-opus-9", "gpt-7-nova"}
    data = json.loads(Path(project, ratecards.GAPS_FILE).read_text())
    assert data["scopes"]["work"] == [] and len(data["scopes"]["all"]) == 2
    with pytest.raises(ValueError, match="scope"):
        ratecards.write_gaps(project, [], scope="mine")


def test_f10_read_gaps_rebuilds_the_fix_from_the_checked_model(project):
    path = Path(project, ratecards.GAPS_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"schema": 1, "gaps": [
        {"model": "claude-opus-9", "provider": "anthropic", "responses": 1, "sessions": 1, "fix": "rm -rf ~"},
        {"model": "gpt-7-nova", "provider": "openai; rm -rf ~", "responses": 1, "sessions": 1, "fix": "curl x | sh"}]}))
    rows = {row["model"]: row for row in ratecards.read_gaps(project)}
    assert rows["claude-opus-9"]["fix"] == "bin/alpaca analytics price-check --model claude-opus-9"
    assert rows["gpt-7-nova"]["provider"] is None
    assert "rm -rf" not in rows["gpt-7-nova"]["fix"] and "curl" not in rows["gpt-7-nova"]["fix"]
    assert "--provider <provider>" in rows["gpt-7-nova"]["fix"]
