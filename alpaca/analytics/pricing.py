"""Traceable token-only API list-price estimates for individual requests.

The input is normalized usage: input_tokens INCLUDES cache reads and writes,
and reasoning_output_tokens is a subset of output_tokens. Native provider
records must be normalized before calling this function. Client cost-state
values and imported reported costs are separate evidence, never inputs here.

Rate cards come from two places. _CARDS holds the cards verified when this code
was written. A model released later is priced from a card an agent recorded
from the official price page (alpaca.analytics.ratecards), passed in as `cards`.
A built-in card always wins over a recorded one for the same model ID.
"""
import math
from decimal import Decimal


VERIFIED_AT = "2026-09-22"
OPENAI_PRICING = "https://developers.openai.com/api/docs/pricing"
CLAUDE_PRICING = "https://platform.claude.com/docs/en/about-claude/pricing"
_CATEGORIES = ("uncached_input", "cached_input", "cache_write_5m",
               "cache_write_1h", "cache_write", "output")
_PROVIDERS = {"codex": "openai", "openai": "openai", "claude": "anthropic",
              "claude-code": "anthropic", "anthropic": "anthropic"}
# The categories each provider bills. A card leaves every other category as None.
BILLED = {"anthropic": ("uncached_input", "cached_input", "cache_write_5m", "cache_write_1h", "output"),
          "openai": ("uncached_input", "cached_input", "cache_write", "output")}
# Built-in cards whose model supports fast mode, at twice the standard rates.
_FAST_BUILT_IN = ("claude-opus-5-5", "claude-opus-5", "claude-opus-4-8")
# Bounds for recorded cards, in USD per million tokens. Input and output must cost something;
# a cache category may be free. More than MAX_PLACES decimal places is refused.
MAX_RATE = Decimal(10000)
MAX_PLACES = 9
POSITIVE = ("uncached_input", "output")
FAST_MIN, FAST_MAX = Decimal(1), Decimal(10)
_QUANTUM = Decimal(1).scaleb(-MAX_PLACES)


def _claude(slug, input_rate, read_rate, write_5m, write_1h, output_rate, *, locale="en"):
    source = "https://platform.claude.com/docs/%s/models/%s/overview" % (locale, slug)
    return {"provider": "anthropic",
            "source_url": source,
            "source_urls": [source, CLAUDE_PRICING],
            "rates": (input_rate, read_rate, write_5m, write_1h, None, output_rate),
            "context_threshold_tokens": None}


# Deliberately exact IDs, including only aliases documented by their model page.
_CARDS = {
    # Opus 5.5 rates come from the pricing page table (cache reads are 0.05x input),
    # so that page is the source and the card carries its own verification date.
    "claude-opus-5-5": {"provider": "anthropic", "source_url": CLAUDE_PRICING,
                        "source_urls": [CLAUDE_PRICING],
                        "rates": ("4", ".2", "5", "8", None, "20"),
                        "context_threshold_tokens": None, "verified_at": "2026-09-25"},
    "claude-opus-5": _claude("opus-5", "5", ".5", "6.25", "10", "25"),
    "claude-opus-4-8": _claude("opus-4-8", "5", ".5", "6.25", "10", "25", locale="es"),
    "claude-fable-5-1": _claude("fable-5-1", "10", ".25", "12.5", "20", "50"),
    "claude-sonnet-5": _claude("sonnet-5", "2", ".2", "2.5", "4", "10"),
    "claude-haiku-4-5-20251001": _claude("haiku-4-5", "1", ".1", "1.25", "2", "5"),
    "claude-haiku-4-5": _claude("haiku-4-5", "1", ".1", "1.25", "2", "5"),
    "gpt-6-astra": {
        "provider": "openai",
        "source_url": "https://developers.openai.com/api/docs/models/gpt-6-astra",
        "source_urls": ["https://developers.openai.com/api/docs/models/gpt-6-astra", OPENAI_PRICING],
        "rates": ("10", "1", None, None, "12.5", "50"),
        "context_threshold_tokens": 272000,
    },
}


def decimal_rate(value, maximum=MAX_RATE):
    """A finite Decimal from 0 to `maximum` with at most MAX_PLACES decimal places, or None.

    Never raises: an out-of-range exponent such as 1E+999999999 is None, not an Overflow.
    """
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        return None
    try:
        rate = Decimal(value.strip() if isinstance(value, str) else value)
        if not rate.is_finite() or rate < 0 or rate > maximum or rate.quantize(_QUANTUM) != rate:
            return None
    except (ArithmeticError, ValueError, TypeError):
        return None
    return rate


def rate_text(category, value):
    """Canonical decimal text for one card rate, or ValueError when it is out of bounds."""
    rate = decimal_rate(value)
    if rate is None or (category in POSITIVE and rate == 0):
        raise ValueError("%s rate must be a decimal %s 0 to %s USD per million tokens with at most %d decimal "
                         "places, not %r" % (category, "above" if category in POSITIVE else "from",
                                             MAX_RATE, MAX_PLACES, value))
    return format(rate.normalize(), "f")


def fast_text(value):
    """Canonical decimal text for a fast mode multiplier (FAST_MIN to FAST_MAX), or ValueError."""
    rate = decimal_rate(value, maximum=FAST_MAX)
    if rate is None or rate < FAST_MIN:
        raise ValueError("fast_multiplier must be a decimal from %s to %s, not %r" % (FAST_MIN, FAST_MAX, value))
    return format(rate.normalize(), "f")


def _https(url):
    return isinstance(url, str) and url.startswith("https://")


def _usable(card):
    """Shape and bounds check for a recorded card; a malformed card prices nothing.

    ratecards.build runs the same check on every card before it is recorded."""
    try:
        if not isinstance(card, dict) or card.get("provider") not in BILLED:
            return False
        rates = card.get("rates")
        if not isinstance(rates, (list, tuple)) or len(rates) != len(_CATEGORIES):
            return False
        billed = BILLED[card["provider"]]
        for category, rate in zip(_CATEGORIES, rates):
            if category in billed:
                rate_text(category, rate)
            elif rate is not None:
                return False
        if card.get("fast_multiplier") is not None:
            fast_text(card["fast_multiplier"])
        threshold = card.get("context_threshold_tokens")
        if threshold is not None and (type(threshold) is not int or threshold <= 0):
            return False
        urls = card.get("source_urls")
        return (_https(card.get("source_url")) and isinstance(urls, (list, tuple)) and all(_https(u) for u in urls)
                and isinstance(card.get("verified_at"), str) and isinstance(card.get("label"), dict))
    except ValueError:
        return False


def card_for(model, cards=None):
    """(card, origin) for an exact model ID: built-in first, then a usable recorded card."""
    if not isinstance(model, str):
        return None, None
    if model in _CARDS:
        return _CARDS[model], "built-in"
    card = cards.get(model) if isinstance(cards, dict) else None
    if _usable(card):
        return card, "recorded"
    return None, None


def fast_multiplier(model, card, origin):
    """The fast mode multiplier a card supports, as a decimal string, or None."""
    if origin == "built-in":
        return "2" if model in _FAST_BUILT_IN else None
    if origin == "recorded" and card.get("fast_multiplier") is not None:
        return str(card["fast_multiplier"])
    return None


class _UsageError(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def _counter(usage, key, default=None):
    value = usage.get(key, default)
    if value is None:
        raise _UsageError("missing_usage", "Usage field %s is unknown." % key)
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or value < 0 or value > 9007199254740991
            or not math.isfinite(value) or int(value) != value):
        raise _UsageError("invalid_usage", "Usage field %s must be a finite nonnegative integer." % key)
    return int(value)


def _counts(usage, provider):
    input_count = _counter(usage, "input_tokens")
    output_count = _counter(usage, "output_tokens")
    read_count = _counter(usage, "cached_input_tokens")
    write_count = _counter(usage, "cache_write_input_tokens")
    # A missing TTL counter is irrelevant when there were no cache writes.
    five = _counter(usage, "cache_creation_5m_tokens", 0) if usage.get("cache_creation_5m_tokens") is not None else 0
    hour = _counter(usage, "cache_creation_1h_tokens", 0) if usage.get("cache_creation_1h_tokens") is not None else 0
    uncached = input_count - read_count - write_count
    if uncached < 0 or five + hour > write_count:
        raise _UsageError("inconsistent_usage", "Cache token categories exceed their input totals.")
    if "uncached_input_tokens" in usage and _counter(usage, "uncached_input_tokens") != uncached:
        raise _UsageError("inconsistent_usage", "Uncached input disagrees with total input minus cache tokens.")
    if usage.get("reasoning_output_tokens") is not None:
        if _counter(usage, "reasoning_output_tokens") > output_count:
            raise _UsageError("inconsistent_usage", "Reasoning output exceeds total output tokens.")
    if usage.get("total_tokens") is not None:
        if _counter(usage, "total_tokens") != input_count + output_count:
            raise _UsageError("inconsistent_usage", "Total tokens disagree with input plus output.")
    if provider == "anthropic" and five + hour != write_count:
        raise _UsageError("unknown_cache_duration", "Claude cache writes lack a complete 5-minute/1-hour breakdown.")
    if provider == "openai" and five + hour:
        raise _UsageError("inconsistent_usage", "Claude cache-duration counters do not apply to OpenAI usage.")
    return input_count, dict(zip(_CATEGORIES, (uncached, read_count, five, hour,
                                             write_count if provider == "openai" else 0, output_count)))


def _service(usage, provider, fast, assumptions):
    tier = usage.get("service_tier")
    speed = usage.get("speed")
    if tier is None or tier == "auto":
        assumptions.append("Standard service tier assumed because the billed tier is not recorded.")
        tier = "standard"
    if tier == "default":
        tier = "standard"
    if speed not in (None, "standard", "fast"):
        raise _UsageError("unverified_service_tier", "The recorded speed has no verified rate.")
    if speed == "fast":
        if tier not in ("standard", "fast", "priority"):
            raise _UsageError("unverified_service_tier", "The recorded speed and service tier conflict.")
        tier = "fast"
    supported = {"standard": "1", "batch": ".5"}
    if provider == "openai":
        supported.update({"flex": ".5", "priority": "2", "fast": "2"})
    elif fast is not None:
        supported["fast"] = fast
    if not isinstance(tier, str) or tier not in supported:
        raise _UsageError("unverified_service_tier", "The recorded service tier has no verified rate for this model.")
    if usage.get("inference_geo") not in (None, "global") or usage.get("data_residency") not in (None, "global"):
        raise _UsageError("unverified_region", "This estimator does not cover the recorded regional pricing modifier.")
    assumptions.append("Global API list pricing; regional modifiers, tool fees, taxes and discounts are excluded.")
    return tier, Decimal(supported[tier])


def unknown_model_reason(model):
    if isinstance(model, str) and model:
        return ("No verified rate card for %s. Record one from the official price page: "
                "alpaca analytics price-check --model %s" % (model, model))
    return "No verified rate card: the response does not name its model."


def estimate(model, usage, *, provider=None, cards=None):
    """Return a JSON-safe cost breakdown or an explicit unavailable reason.

    Missing or null input/output/cache counters mean unknown. Callers must provide
    zero when a cache category is known to be unused. For Claude writes, missing
    TTL data leaves the total unavailable. The result uses
    the current rate card, not a historical invoice or subscription payment.
    `cards` maps model IDs to recorded cards (ratecards.load); built-in cards win.
    """
    card, origin = card_for(model, cards)
    verified_at = card.get("verified_at", VERIFIED_AT) if card else VERIFIED_AT
    result = {"status": "unavailable", "basis": "rate_card_estimate", "currency": "USD",
              "model": model if isinstance(model, str) else None, "provider": provider,
              "total_usd": None, "breakdown_usd": {name: None for name in _CATEGORIES},
              "reason_code": None, "reason": None, "rate_card": None,
              "assumptions": ["Current list-price token estimate, not an invoice or subscription charge.",
                              "Rates were verified on %s; historical effective dates are not established." % verified_at]}
    if card is None:
        result.update(reason_code="unverified_model", reason=unknown_model_reason(model))
        return result
    expected_provider = card["provider"]
    if provider is not None and (not isinstance(provider, str) or _PROVIDERS.get(provider) != expected_provider):
        result.update(provider=provider if isinstance(provider, str) else None,
                      reason_code="unverified_provider", reason="No verified rate card matches this model and provider.")
        return result
    result["provider"] = expected_provider
    result["rate_card"] = {"model": model, "provider": expected_provider,
                           "source_url": card["source_url"], "source_urls": list(card["source_urls"]),
                           "verified_at": verified_at, "effective_date": None,
                           "basis": "Current published API list prices",
                           "context_threshold_tokens": card.get("context_threshold_tokens"),
                           "origin": origin}
    if origin == "recorded":
        result["rate_card"]["label"] = dict(card["label"])
    try:
        if not isinstance(usage, dict):
            raise _UsageError("missing_usage", "Normalized request usage is unavailable.")
        threshold = card.get("context_threshold_tokens")
        if threshold and usage.get("usage_scope", "request") != "request":
            raise _UsageError("request_boundaries_required", "Long-context rates require individual request boundaries.")
        input_count, counts = _counts(usage, expected_provider)
        service_tier, service_multiplier = _service(usage, expected_provider, fast_multiplier(model, card, origin),
                                                    result["assumptions"])
        long_context = threshold is not None and input_count > threshold
        input_multiplier = Decimal(2 if long_context else 1)
        output_multiplier = Decimal("1.5" if long_context else "1")
        rates = {}
        costs = {}
        for category, base_rate in zip(_CATEGORIES, card["rates"]):
            multiplier = output_multiplier if category == "output" else input_multiplier
            rate = Decimal(base_rate) * multiplier * service_multiplier if base_rate is not None else None
            rates[category] = float(rate) if rate is not None else None
            costs[category] = Decimal(counts[category]) * rate / Decimal(1000000) if rate is not None else Decimal(0)
        floats = [float(value) for value in costs.values()] + [float(sum(costs.values()))]
        floats += [value for value in rates.values() if value is not None]
        if not all(math.isfinite(value) for value in floats):
            # JSON has no Infinity; an out-of-range card must not reach a page as money.
            raise _UsageError("non_finite_total", "The priced total is not a finite number; the rate card is out of range.")
        result["rate_card"].update(rates_per_million=rates,
                                   context_tier="long" if long_context else "standard",
                                   input_multiplier=float(input_multiplier), output_multiplier=float(output_multiplier),
                                   service_tier=service_tier, service_multiplier=float(service_multiplier))
        result.update(status="estimated", total_usd=float(sum(costs.values())),
                      breakdown_usd={key: float(value) for key, value in costs.items()})
    except _UsageError as exc:
        result.update(reason_code=exc.code, reason=str(exc))
    return result
