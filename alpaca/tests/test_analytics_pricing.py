"""Cost arithmetic and provenance, using hand-calculated request examples."""
import json

import pytest

from alpaca.analytics.pricing import estimate


def usage(**overrides):
    values = {"input_tokens": 1000, "cached_input_tokens": 0,
              "cache_write_input_tokens": 0, "output_tokens": 100}
    values.update(overrides)
    return values


@pytest.mark.parametrize("model,want", [
    ("claude-opus-5", 0.0075),
    ("claude-opus-4-8", 0.0075),
    ("claude-fable-5-1", 0.015),
    ("claude-haiku-4-5-20251001", 0.0015),
    ("claude-haiku-4-5", 0.0015),
    ("claude-sonnet-5", 0.003),
    ("gpt-6-astra", 0.015),
])
def test_exact_model_rates(model, want):
    result = estimate(model, usage())
    assert result["status"] == "estimated"
    assert result["total_usd"] == pytest.approx(want)
    assert result["basis"] == "rate_card_estimate"
    card = result["rate_card"]
    assert card["source_url"].startswith("https://")
    assert card["verified_at"] == "2026-09-22"
    assert card["effective_date"] is None  # Retrieval date is not a historical price date.
    assert result["assumptions"]
    json.dumps(result, allow_nan=False)


def test_claude_cache_durations_are_distinct_and_input_is_not_counted_twice():
    result = estimate("claude-opus-5", usage(
        input_tokens=4500, uncached_input_tokens=1000,
        cached_input_tokens=2000, cache_write_input_tokens=1500,
        cache_creation_5m_tokens=1000, cache_creation_1h_tokens=500,
        output_tokens=100))
    assert result["total_usd"] == pytest.approx(0.01975)
    assert result["breakdown_usd"] == pytest.approx({
        "uncached_input": .005, "cached_input": .001,
        "cache_write_5m": .00625, "cache_write_1h": .005,
        "cache_write": 0, "output": .0025})


def test_opus_5_5_rates_come_from_the_pricing_page_with_their_own_date():
    result = estimate("claude-opus-5-5", usage(
        input_tokens=4500, uncached_input_tokens=1000,
        cached_input_tokens=2000, cache_write_input_tokens=1500,
        cache_creation_5m_tokens=1000, cache_creation_1h_tokens=500,
        output_tokens=100, service_tier="standard", speed="standard"))
    assert result["status"] == "estimated"
    assert result["total_usd"] == pytest.approx(0.0154)
    assert result["breakdown_usd"] == pytest.approx({
        "uncached_input": .004, "cached_input": .0004,
        "cache_write_5m": .005, "cache_write_1h": .004,
        "cache_write": 0, "output": .002})
    card = result["rate_card"]
    assert card["source_url"] == "https://platform.claude.com/docs/en/about-claude/pricing"
    assert card["verified_at"] == "2026-09-25"
    assert "2026-09-25" in result["assumptions"][1]


def test_opus_5_5_fast_mode_doubles_every_category():
    result = estimate("claude-opus-5-5", usage(speed="fast"))
    assert result["status"] == "estimated"
    assert result["total_usd"] == pytest.approx(2 * 0.006)
    assert result["rate_card"]["service_tier"] == "fast"


def test_fable_cache_read_does_not_inherit_predecessor_rate():
    result = estimate("claude-fable-5-1", usage(
        input_tokens=100_000, cached_input_tokens=100_000, output_tokens=0))
    assert result["total_usd"] == .025


def test_openai_cache_input_is_inclusive_and_reasoning_is_output_subset():
    result = estimate("gpt-6-astra", usage(
        input_tokens=4000, cached_input_tokens=2000,
        cache_write_input_tokens=1000, output_tokens=100,
        reasoning_output_tokens=80))
    assert result["total_usd"] == .0295
    assert result["breakdown_usd"]["output"] == .005


@pytest.mark.parametrize("count,want,tier", [
    (272000, 2.725, "standard"), (272001, 5.44752, "long")])
def test_astra_long_context_threshold_is_strict_and_applies_to_entire_request(count, want, tier):
    result = estimate("gpt-6-astra", usage(input_tokens=count))
    assert result["total_usd"] == pytest.approx(want)
    assert result["rate_card"]["context_tier"] == tier


def test_astra_long_context_cache_and_fast_multipliers_stack():
    result = estimate("gpt-6-astra", usage(
        input_tokens=300000, cached_input_tokens=200000,
        cache_write_input_tokens=50000, output_tokens=1000,
        service_tier="priority"))
    assert result["total_usd"] == 5.45
    assert result["rate_card"]["input_multiplier"] == 2
    assert result["rate_card"]["output_multiplier"] == 1.5
    assert result["rate_card"]["service_multiplier"] == 2


def test_claude_large_context_has_no_generic_premium():
    result = estimate("claude-opus-5", usage(input_tokens=900000, output_tokens=0))
    assert result["total_usd"] == 4.5


def test_opus_4_8_large_context_and_fast_cache_rates_are_explicit():
    result = estimate("claude-opus-4-8", usage(
        input_tokens=900000, cached_input_tokens=300000,
        cache_write_input_tokens=300000, cache_creation_5m_tokens=200000,
        cache_creation_1h_tokens=100000, output_tokens=1000, speed="fast"))
    assert result["total_usd"] == 7.85
    assert result["rate_card"]["context_tier"] == "standard"
    assert result["rate_card"]["service_multiplier"] == 2
    assert result["rate_card"]["source_url"].endswith("/models/opus-4-8/overview")


@pytest.mark.parametrize("model", ["claude-opus-500", "claude-opus-5[1m]",
                                   "gpt-6-astra-unknown", "<synthetic>", "", None])
def test_unknown_models_never_inherit_prefix_rate(model):
    result = estimate(model, usage())
    assert result["total_usd"] is None
    assert result["reason_code"] == "unverified_model"
    assert result["reason"]


@pytest.mark.parametrize("value", [-1, float("nan"), float("inf"), True, "100", .5])
def test_invalid_usage_does_not_become_zero_or_money(value):
    result = estimate("gpt-6-astra", usage(input_tokens=value))
    assert result["status"] == "unavailable"
    assert result["total_usd"] is None
    assert result["reason_code"] == "invalid_usage"
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize("key", ["input_tokens", "output_tokens", "cached_input_tokens",
                                 "cache_write_input_tokens"])
def test_null_usage_is_unknown(key):
    result = estimate("gpt-6-astra", usage(**{key: None}))
    assert result["total_usd"] is None
    assert result["reason_code"] == "missing_usage"


@pytest.mark.parametrize("key", ["input_tokens", "output_tokens", "cached_input_tokens",
                                 "cache_write_input_tokens"])
def test_omitted_usage_counters_are_unknown(key):
    counters = usage()
    del counters[key]
    result = estimate("gpt-6-astra", counters)
    assert result["total_usd"] is None
    assert result["reason_code"] == "missing_usage"


def test_missing_required_usage_is_unknown_and_measured_zero_is_zero():
    assert estimate("gpt-6-astra", {})["total_usd"] is None
    assert estimate("gpt-6-astra", usage(input_tokens=0, output_tokens=0))["total_usd"] == 0


def test_claude_missing_cache_duration_does_not_guess_five_minutes():
    result = estimate("claude-opus-5", usage(cache_write_input_tokens=100))
    assert result["total_usd"] is None
    assert result["reason_code"] == "unknown_cache_duration"


@pytest.mark.parametrize("values", [
    {"cached_input_tokens": 1001},
    {"uncached_input_tokens": 99},
    {"cache_write_input_tokens": 20, "cache_creation_5m_tokens": 21},
    {"reasoning_output_tokens": 101},
    {"total_tokens": 2},
])
def test_inconsistent_usage_does_not_produce_price(values):
    result = estimate("claude-opus-5", usage(**values))
    assert result["total_usd"] is None
    assert result["reason_code"] == "inconsistent_usage"


def test_unknown_provider_and_service_tier_are_not_assumed_standard():
    assert estimate("gpt-6-astra", usage(), provider="azure")["total_usd"] is None
    assert estimate("gpt-6-astra", usage(service_tier="mystery"))["total_usd"] is None
    assert estimate("claude-fable-5-1", usage(service_tier="fast"))["total_usd"] is None


def test_provider_client_names_identify_api_ratecard_without_changing_cost_basis():
    assert estimate("gpt-6-astra", usage(), provider="codex")["total_usd"] == .015
    assert estimate("claude-opus-5", usage(), provider="claude")["total_usd"] == .0075


def test_reported_or_client_cost_is_not_used_to_replace_independent_estimate():
    result = estimate("gpt-6-astra", usage(reported_cost_usd=99, totalCostUSD=88))
    assert result["total_usd"] == .015


def test_subcent_cost_precision_is_not_rounded_per_request():
    result = estimate("claude-fable-5-1", usage(
        input_tokens=1, cached_input_tokens=1, output_tokens=0))
    assert result["total_usd"] == .00000025


def test_current_rate_estimate_does_not_claim_historical_or_subscription_bill():
    result = estimate("claude-sonnet-5", usage())
    assert result["rate_card"]["basis"] == "Current published API list prices"
    assert any("subscription" in item.lower() for item in result["assumptions"])


def test_astra_cumulative_usage_does_not_apply_request_threshold_to_session_total():
    result = estimate("gpt-6-astra", usage(input_tokens=900000, usage_scope="session"))
    assert result["total_usd"] is None
    assert result["reason_code"] == "request_boundaries_required"


def test_built_in_cards_say_where_they_come_from():
    card = estimate("claude-opus-5", usage())["rate_card"]
    assert card["origin"] == "built-in"
    assert "label" not in card


def test_unknown_model_reason_names_the_model_and_how_to_record_a_card():
    result = estimate("claude-opus-9", usage())
    assert result["reason_code"] == "unverified_model"
    assert "claude-opus-9" in result["reason"]
    assert "alpaca analytics price-check --model claude-opus-9" in result["reason"]


def recorded_card(**overrides):
    card = {"provider": "anthropic", "source_url": "https://platform.claude.com/docs/en/about-claude/pricing",
            "source_urls": ["https://platform.claude.com/docs/en/about-claude/pricing"],
            "rates": ("5", "0.5", "6.25", "10", None, "25"), "context_threshold_tokens": None,
            "verified_at": "2026-09-25",
            "label": {"method": "agent-entered", "source_sha256": None, "source_copy": None, "row": None,
                      "verified_by": "cli", "recorded_at": "2026-09-25T00:00:00+00:00"}}
    card.update(overrides)
    return card


def test_recorded_cards_price_models_without_a_built_in_card():
    result = estimate("claude-opus-4-7", usage(), cards={"claude-opus-4-7": recorded_card()})
    assert result["total_usd"] == pytest.approx(0.0075)
    assert result["rate_card"]["origin"] == "recorded"
    assert result["rate_card"]["label"]["method"] == "agent-entered"
    assert result["rate_card"]["verified_at"] == "2026-09-25"


@pytest.mark.parametrize("broken", [{"rates": ("5", "0.5")}, {"rates": ("5", "x", "6.25", "10", None, "25")},
                                    {"provider": "azure"}, {"label": None}])
def test_malformed_recorded_cards_are_not_used(broken):
    result = estimate("claude-opus-4-7", usage(), cards={"claude-opus-4-7": recorded_card(**broken)})
    assert result["total_usd"] is None
    assert result["reason_code"] == "unverified_model"


@pytest.mark.parametrize("broken", [
    {"rates": ("10001", "0.5", "6.25", "10", None, "25")},
    {"rates": ("0", "0.5", "6.25", "10", None, "25")},
    {"rates": ("5", "0.5", "6.25", "10", None, "0")},
    {"rates": ("1E+400", "0.5", "6.25", "10", None, "25")},
    {"fast_multiplier": "0.5"},
    {"fast_multiplier": "1E+400"},
    {"source_url": "HTTPS://platform.claude.com/docs/en/about-claude/pricing"},
])
def test_recorded_cards_outside_the_bounds_are_not_used(broken):
    result = estimate("claude-opus-4-7", usage(), cards={"claude-opus-4-7": recorded_card(**broken)})
    assert result["total_usd"] is None
    assert result["reason_code"] == "unverified_model"


# --- t-427: Claude Haiku 5.5 is priced by prompt length (two published rows, split at 100,000 tokens) ---

HAIKU_5_5 = "claude-haiku-5-5"
HAIKU_OVER_100K = (".5", ".05", ".625", "1", None, "2.5")   # the row the record holds today (event 134071)


def prompt(n_in, read=0, w5=0, w1=0, n_out=1000, **extra):
    return usage(input_tokens=n_in, cached_input_tokens=read, cache_write_input_tokens=w5 + w1,
                 cache_creation_5m_tokens=w5, cache_creation_1h_tokens=w1, output_tokens=n_out, **extra)


# Hand-computed from the page rows: up to 100K = .10 in, .01 read, .125 5m, .20 1h, .50 out;
# over 100K = .50, .05, .625, 1, 2.50 (USD per million tokens).
@pytest.mark.parametrize("n_in,read,w5,w1,want,tier", [
    (50000, 0, 0, 0, 0.0055, "standard"),             # 50000*.1 + 1000*.5
    (50000, 20000, 6000, 4000, 0.00425, "standard"),  # 20000*.1 + 20000*.01 + 6000*.125 + 4000*.2 + 1000*.5
    (100000, 0, 0, 0, 0.0105, "standard"),            # exactly 100,000 is not "over"
    (100001, 0, 0, 0, 0.0525005, "long"),             # 100001*.5 + 1000*2.5
    (150000, 0, 0, 0, 0.0775, "long"),                # 150000*.5 + 1000*2.5
    (150000, 60000, 18000, 12000, 0.05875, "long"),   # 60000*.5 + 60000*.05 + 18000*.625 + 12000*1 + 1000*2.5
    (110000, 60000, 0, 0, 0.0305, "long"),            # 50000*.5 + 60000*.05 + 1000*2.5: cache reads count
])
def test_haiku_5_5_prices_each_prompt_at_the_row_of_its_size(n_in, read, w5, w1, want, tier):
    result = estimate(HAIKU_5_5, prompt(n_in, read, w5, w1))
    assert result["status"] == "estimated"
    assert result["total_usd"] == pytest.approx(want, rel=1e-12)
    card = result["rate_card"]
    assert card["origin"] == "built-in" and card["context_tier"] == tier
    assert card["context_threshold_tokens"] == 100000
    assert (card["input_multiplier"], card["output_multiplier"]) == (1, 1)
    assert card["rates_per_million"]["uncached_input"] == (0.1 if tier == "standard" else 0.5)
    assert card["rates_per_million"]["output"] == (0.5 if tier == "standard" else 2.5)


def test_haiku_5_5_built_in_card_wins_over_the_recorded_over_100k_card():
    cards = {HAIKU_5_5: recorded_card(rates=HAIKU_OVER_100K)}
    result = estimate(HAIKU_5_5, prompt(50000), cards=cards)
    assert result["total_usd"] == pytest.approx(0.0055)      # not 0.0275, the over-100K row
    assert result["rate_card"]["origin"] == "built-in"


@pytest.mark.parametrize("n_in,want", [(50000, 0.00275), (150000, 0.03875)])
def test_haiku_5_5_batch_halves_the_row_that_applies(n_in, want):
    # The Batch table rows: up to 100K .05 in / .25 out, over 100K .25 / 1.25.
    result = estimate(HAIKU_5_5, prompt(n_in, service_tier="batch"))
    assert result["total_usd"] == pytest.approx(want, rel=1e-12)


@pytest.mark.parametrize("extra,code", [({"usage_scope": "session"}, "request_boundaries_required"),
                                        ({"speed": "fast"}, "unverified_service_tier")])
def test_haiku_5_5_refuses_summed_usage_and_fast_mode(extra, code):
    result = estimate(HAIKU_5_5, prompt(50000, **extra))
    assert result["total_usd"] is None and result["reason_code"] == code


def test_a_recorded_threshold_card_keeps_the_x2_x1_5_step():
    card = recorded_card(context_threshold_tokens=1000)
    result = estimate("claude-opus-4-7", usage(input_tokens=1001), cards={"claude-opus-4-7": card})
    assert result["total_usd"] == pytest.approx(0.01376)     # 1001*10 + 100*37.5
    assert (result["rate_card"]["input_multiplier"], result["rate_card"]["output_multiplier"]) == (2, 1.5)


def test_a_recorded_card_cannot_carry_a_second_rate_row():
    card = recorded_card(context_threshold_tokens=1000, long_context_rates=HAIKU_OVER_100K)
    result = estimate("claude-opus-4-7", usage(input_tokens=1001), cards={"claude-opus-4-7": card})
    assert result["total_usd"] is None and result["reason_code"] == "unverified_model"
