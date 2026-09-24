"""Cost analytics selftest: arithmetic, coverage, client snapshot bounds, online page check."""
import json
import os
from pathlib import Path

import pytest

from alpaca import cli, db, transcripts
from alpaca.analytics import pricing, ratecards, selftest

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "anthropic_pricing_trimmed.md")
PAGE = Path(FIXTURE).read_text(encoding="utf-8")
PAGE_BYTES = PAGE.encode("utf-8")
PRICED = {"input_tokens": 1000, "output_tokens": 100, "cache_read_input_tokens": 2000,
          "cache_creation_input_tokens": 1500,
          "cache_creation": {"ephemeral_5m_input_tokens": 1000, "ephemeral_1h_input_tokens": 500}}


def write(root, sid, records, operator="claude"):
    path = Path(transcripts.local_path(root, sid))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records))
    conn = db.connect(root)
    db.upsert(conn, "sessions", "sid", {"sid": sid, "started": "2026-09-25T00:00:00Z"})
    db.meta_set(conn, "operator:" + sid, operator)
    conn.close()


def claude(ident, model, usage=None):
    return {"type": "assistant", "uuid": "u-" + ident, "timestamp": "2026-09-25T01:02:03Z",
            "requestId": "request-" + ident, "message": {
                "id": ident, "model": model, "usage": usage if usage is not None else PRICED,
                "content": [{"type": "text", "text": "Visible result"}]}}


def cost_state(models):
    return {"type": "cost-state", "totalCostUSD": sum(v.get("costUSD", 0) for v in models.values()),
            "modelUsage": models}


def client(cost, **tokens):
    values = {"inputTokens": 1000, "outputTokens": 100, "cacheReadInputTokens": 2000,
              "cacheCreationInputTokens": 1500, "costUSD": cost}
    values.update(tokens)
    return values


def check(report, name):
    return next(item for item in report["checks"] if item["name"] == name)


def test_arithmetic_prices_every_card_and_pins_known_answers(project):
    report = selftest.run(project)
    arithmetic = check(report, "arithmetic")
    assert arithmetic["status"] == "pass", arithmetic
    cards = {item["model"]: item for item in arithmetic["items"] if item["kind"] == "card"}
    assert set(pricing._CARDS) <= set(cards)
    assert all(item["status"] == "pass" and item["origin"] == "built-in" for item in cards.values())
    pinned = {item["model"]: item for item in arithmetic["items"] if item["kind"] == "pinned"}
    assert pinned["claude-opus-5-5"]["expected_usd"] == "0.0154"
    assert pinned["claude-opus-5-5"]["estimated_usd"] == pytest.approx(0.0154)
    assert pinned["claude-opus-5"]["expected_usd"] == "0.01975"
    assert all(item["status"] == "pass" for item in pinned.values())
    assert any(item["kind"] == "fast" and item["model"] == "claude-opus-5-5" for item in arithmetic["items"])
    assert {c["name"] for c in report["checks"]} >= {"arithmetic", "coverage", "client-snapshot", "online"}
    assert check(report, "online")["status"] == "skip"
    json.dumps(report, allow_nan=False)


def test_arithmetic_covers_recorded_cards(project):
    ratecards.record(project, "claude-opus-4-7", provider="anthropic", rates=("5", ".5", "6.25", "10", None, "25"),
                     source_url=pricing.CLAUDE_PRICING, method="agent-entered", fast_multiplier="2")
    items = check(selftest.run(project), "arithmetic")["items"]
    recorded = [item for item in items if item["model"] == "claude-opus-4-7"]
    assert {item["kind"] for item in recorded} == {"card", "fast"}
    assert all(item["origin"] == "recorded" and item["status"] == "pass" for item in recorded)


def test_arithmetic_fails_when_a_card_drifts_from_the_pinned_answer(project, monkeypatch):
    drifted = dict(pricing._CARDS["claude-opus-5-5"], rates=("4", ".2", "5", "8", None, "21"))
    monkeypatch.setitem(pricing._CARDS, "claude-opus-5-5", drifted)
    report = selftest.run(project)
    arithmetic = check(report, "arithmetic")
    assert arithmetic["status"] == "fail"
    failed = [item for item in arithmetic["items"] if item["status"] == "fail"]
    assert {item["model"] for item in failed} == {"claude-opus-5-5"}
    assert {item["kind"] for item in failed} == {"pinned", "pinned-fast", "known-answer"}
    assert report["status"] == "fail"


def test_arithmetic_fails_when_the_estimator_disagrees(project, monkeypatch):
    real = pricing.estimate

    def broken(model, usage, **kwargs):
        result = real(model, usage, **kwargs)
        if result["total_usd"] is not None:
            result["total_usd"] *= 1.5
        return result

    monkeypatch.setattr(pricing, "estimate", broken)
    arithmetic = check(selftest.run(project), "arithmetic")
    assert arithmetic["status"] == "fail"
    assert all(item["status"] == "fail" for item in arithmetic["items"])


def test_coverage_fails_with_the_fix_for_a_model_without_a_card(project):
    write(project, "s1", [claude("r1", "claude-opus-4-7"), claude("r2", "claude-opus-5")])
    report = selftest.run(project)
    coverage = check(report, "coverage")
    assert coverage["status"] == "fail"
    gap = next(item for item in coverage["items"] if item.get("model") == "claude-opus-4-7")
    assert gap["fix"] == "bin/alpaca analytics price-check --model claude-opus-4-7"
    assert gap["responses"] == 1 and gap["sessions"] == 1
    assert "claude-opus-4-7" in coverage["detail"]
    assert report["status"] == "fail"


def test_coverage_warns_for_responses_unpriced_for_other_reasons(project):
    unknown_duration = {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 0,
                        "cache_creation_input_tokens": 7}
    write(project, "s1", [claude("r1", "claude-opus-5"), claude("r2", "claude-opus-5", unknown_duration)])
    coverage = check(selftest.run(project), "coverage")
    assert coverage["status"] == "warn"
    group = next(item for item in coverage["items"] if item.get("reason_code") == "unknown_cache_duration")
    assert group["responses"] == 1 and group["sessions"] == 1
    assert group["models"] == ["claude-opus-5"]


def test_coverage_passes_when_every_response_is_priced(project):
    write(project, "s1", [claude("r1", "claude-opus-5")])
    coverage = check(selftest.run(project), "coverage")
    assert coverage["status"] == "pass", coverage
    assert coverage["counts"]["sessions"] == 1 and coverage["counts"]["priced_responses"] == 1


def test_coverage_limits_itself_to_the_sessions_given(project):
    write(project, "s1", [claude("r1", "claude-opus-5")])
    write(project, "s2", [claude("r1", "claude-opus-4-7")])
    assert check(selftest.run(project, sessions=["s1"]), "coverage")["status"] == "pass"
    assert check(selftest.run(project, sessions=["s2"]), "coverage")["status"] == "fail"


def test_client_snapshot_bounds(project):
    # low = 1000*5 + 2000*0.5 + 1500*6.25 + 100*25 = 17875 per million; high uses 10 for writes = 23500.
    write(project, "s1", [claude("r1", "claude-opus-5"), cost_state({"claude-opus-5": client(0.02)})])
    write(project, "s2", [claude("r1", "claude-opus-5"), cost_state({"claude-opus-5": client(0.05)})])
    write(project, "s3", [claude("r1", "claude-opus-5"), cost_state({"claude-opus-5[1m]": client(0.02)})])
    write(project, "s4", [claude("r1", "claude-opus-5"), cost_state({
        "claude-sonnet-4-5-20250929": client(1.0), "claude-opus-5": client(0, inputTokens=0)})])
    snap = check(selftest.run(project), "client-snapshot")
    assert snap["status"] == "warn"
    items = {(item["session"], item["model"]): item for item in snap["items"]}
    inside = items[("s1", "claude-opus-5")]
    assert inside["status"] == "pass"
    assert inside["low_usd"] == pytest.approx(0.017875)
    assert inside["high_usd"] == pytest.approx(0.0235)
    assert items[("s2", "claude-opus-5")]["status"] == "warn"
    widened = items[("s3", "claude-opus-5[1m]")]
    assert widened["status"] == "pass" and widened["card_model"] == "claude-opus-5" and widened["note"]
    assert not any(session == "s4" for session, _model in items)
    assert snap["counts"] == {"pass": 2, "warn": 1}


def test_client_snapshot_skips_without_token_counts(project):
    write(project, "s1", [claude("r1", "claude-opus-5"), cost_state({"claude-opus-5": {"costUSD": 0.02}})])
    assert check(selftest.run(project), "client-snapshot")["status"] == "skip"


def test_online_compares_every_anthropic_card_with_the_page(project):
    report = selftest.run(project, online=True, fetch=lambda url: PAGE_BYTES)
    online = check(report, "online")
    assert online["status"] == "pass", online
    compared = {item["model"] for item in online["items"] if item["status"] == "pass"}
    expected = {model for model, card in pricing._CARDS.items() if card["provider"] == "anthropic"}
    assert expected <= compared
    changed = PAGE.replace("| Claude Opus 5                                                                                                                         | $5 / MTok             | $6.25 / MTok    | $10 / MTok      | $0.50 / MTok             | $25 / MTok             |",
                           "| Claude Opus 5                                                                                                                         | $5 / MTok             | $6.25 / MTok    | $10 / MTok      | $0.50 / MTok             | $30 / MTok             |")
    assert changed != PAGE
    online = check(selftest.run(project, online=True, fetch=lambda url: changed.encode()), "online")
    assert online["status"] == "fail"
    assert [item["model"] for item in online["items"] if item["status"] == "fail"] == ["claude-opus-5"]


def test_online_network_error_is_a_warning(project):
    def fail(url):
        raise OSError("network is unreachable")
    online = check(selftest.run(project, online=True, fetch=fail), "online")
    assert online["status"] == "warn"
    assert "network is unreachable" in online["detail"]


def test_label_gaps_records_anthropic_gaps_before_coverage(project):
    write(project, "s1", [claude("r1", "claude-opus-4-7")])
    report = selftest.run(project, label_gaps=True, fetch=lambda url: PAGE_BYTES)
    label = check(report, "label-gaps")
    assert label["status"] == "pass", label
    assert label["items"][0]["model"] == "claude-opus-4-7" and label["items"][0]["status"] == "recorded"
    assert check(report, "coverage")["status"] == "pass"
    conn = db.connect_readonly(project)
    assert ratecards.load(conn)["claude-opus-4-7"]["label"]["method"] == "official-page-parse"
    conn.close()
    assert ratecards.read_gaps(project) == []


def test_run_writes_the_gap_notice(project):
    write(project, "s1", [claude("r1", "claude-opus-4-7")])
    report = selftest.run(project)
    assert report["gaps_file"] == ratecards.GAPS_FILE
    assert [row["model"] for row in ratecards.read_gaps(project)] == ["claude-opus-4-7"]


def test_cli_selftest_saves_the_report(project, capsys):
    write(project, "s1", [claude("r1", "claude-opus-5")])
    assert cli.main(["analytics", "selftest", "--json"]) == cli.PASS
    report = json.loads(capsys.readouterr().out)
    path = Path(project, report["report_path"])
    assert path.parent == Path(project, ".alpaca", "analytics", "selftest")
    assert json.loads(path.read_text())["status"] == report["status"] == "pass"
    write(project, "s2", [claude("r1", "claude-opus-4-7")])
    assert cli.main(["analytics", "selftest"]) == cli.FAIL
    out = capsys.readouterr().out
    assert "coverage: fail" in out and "price-check --model claude-opus-4-7" in out
    assert "GATE alpaca-analytics-selftest: FAIL" in out
    assert len(list(path.parent.iterdir())) == 2


# --- review fix round (backend review) ---------------------------------------------------------

def test_f2_close_never_accepts_infinity():
    from decimal import Decimal
    assert selftest._close(float("inf"), Decimal("1E+400")) is False
    assert selftest._close(float("nan"), Decimal("1")) is False
    assert selftest._close(0.0154, Decimal("0.0154")) is True


def test_f3_a_registered_transcript_that_is_gone_fails_coverage(project, tmp_path, capsys):
    write(project, "s1", [claude("r1", "claude-opus-5")])
    conn = db.connect(project)
    db.upsert(conn, "sessions", "sid", {"sid": "gone", "started": "2026-09-25T00:00:00Z",
                                        "transcript": str(tmp_path / "expired" / "gone.jsonl")})
    conn.close()
    report = selftest.run(project)
    coverage = check(report, "coverage")
    assert coverage["status"] == "fail"
    assert "gone" in coverage["detail"]
    assert any(item.get("kind") == "unread-session" and item["session"] == "gone" for item in coverage["items"])
    assert report["status"] == "fail"
    assert cli.main(["analytics", "selftest"]) == cli.FAIL
    assert "GATE alpaca-analytics-selftest: FAIL" in capsys.readouterr().out


def test_f3_an_analysis_that_raises_fails_coverage(project, monkeypatch, capsys):
    from alpaca.analytics import metrics
    write(project, "s1", [claude("r1", "claude-opus-5")])
    write(project, "s2", [claude("r1", "claude-opus-5")])
    real = metrics.analyze

    def flaky(root, sid, child=None):
        if sid == "s2":
            raise OSError("disk read error")
        return real(root, sid, child)

    monkeypatch.setattr(metrics, "analyze", flaky)
    coverage = check(selftest.run(project), "coverage")
    assert coverage["status"] == "fail"
    assert "s2" in coverage["detail"] and "disk read error" in json.dumps(coverage["items"])
    assert cli.main(["analytics", "selftest"]) == cli.FAIL
    capsys.readouterr()


def test_f8_every_built_in_card_is_pinned_to_the_published_table(project, monkeypatch):
    items = check(selftest.run(project), "arithmetic")["items"]
    pinned = {item["model"] for item in items if item["kind"] == "pinned" and item["status"] == "pass"}
    assert pinned == set(pricing._CARDS)
    typo = dict(pricing._CARDS["claude-sonnet-5"], rates=("2", ".2", "2.5", "5", None, "10"))
    monkeypatch.setitem(pricing._CARDS, "claude-sonnet-5", typo)
    arithmetic = check(selftest.run(project), "arithmetic")
    assert arithmetic["status"] == "fail"
    assert {item["model"] for item in arithmetic["items"] if item["status"] == "fail"} == {"claude-sonnet-5"}


def test_f8_a_built_in_card_without_published_rates_fails(project, monkeypatch):
    monkeypatch.setitem(pricing._CARDS, "claude-new-9", dict(pricing._CARDS["claude-opus-5"]))
    arithmetic = check(selftest.run(project), "arithmetic")
    assert arithmetic["status"] == "fail"
    assert "claude-new-9" in arithmetic["detail"]


def test_f9_selftest_writes_the_all_scope(project):
    write(project, "s1", [claude("r1", "claude-opus-4-7")])
    ratecards.write_gaps(project, [], scope="work")
    selftest.run(project)
    data = json.loads(Path(project, ratecards.GAPS_FILE).read_text())
    assert [row["model"] for row in data["scopes"]["all"]] == ["claude-opus-4-7"]
    assert data["scopes"]["work"] == []
    assert [row["model"] for row in ratecards.read_gaps(project)] == ["claude-opus-4-7"]
