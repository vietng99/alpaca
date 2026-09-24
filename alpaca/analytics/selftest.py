"""Selftest for cost analytics: is every response priced, and priced right?

Checks, each {name, status: pass|warn|fail|skip, detail, items}:
  arithmetic       every built-in and recorded card priced on a fixed usage vector with every
                   category nonzero, against an independent Decimal computation; every built-in
                   card pinned to PUBLISHED, a table of the published prices typed in here
                   separately, so a typo in pricing.py fails offline; two hand-computed answers.
  coverage         every session with a transcript analysed; a model without a card fails
                   with the command that records one, and so does a session whose transcript
                   was not read; other unpriced responses warn.
  client-snapshot  the client's own per-model cost (transcript cost-state) must fall between
                   the standard-speed bounds of our card: all cache writes at the 5 minute rate
                   (low) or all at the 1 hour rate (high), with 1% slack.
  online           (opt in) the official Claude price page, fetched once, must agree with every
                   built-in and recorded Claude card.
  label-gaps       (opt in) before coverage, record a card from the official page for every
                   Claude model without one.
"""
import http.client
import json
import math
import os
from datetime import datetime, timezone
from decimal import Decimal

from alpaca import paths, util
from alpaca.analytics import pricing, ratecards

REPORT_DIR = "/".join((paths.runtime_dir(""), "analytics", "selftest"))
# 4500 input tokens = 1000 uncached + 2000 cache reads + 1500 cache writes; 100 output.
ANTHROPIC_VECTOR = {"input_tokens": 4500, "uncached_input_tokens": 1000, "cached_input_tokens": 2000,
                    "cache_write_input_tokens": 1500, "cache_creation_5m_tokens": 1000,
                    "cache_creation_1h_tokens": 500, "output_tokens": 100}
OPENAI_VECTOR = {"input_tokens": 4500, "uncached_input_tokens": 1000, "cached_input_tokens": 2000,
                 "cache_write_input_tokens": 1500, "output_tokens": 100}
_VECTOR_COUNTS = {
    "anthropic": {"uncached_input": 1000, "cached_input": 2000, "cache_write_5m": 1000,
                  "cache_write_1h": 500, "cache_write": 0, "output": 100},
    "openai": {"uncached_input": 1000, "cached_input": 2000, "cache_write_5m": 0,
               "cache_write_1h": 0, "cache_write": 1500, "output": 100},
}
# The published list prices of every built-in card, typed in again from the official pages and
# kept apart from pricing._CARDS on purpose: Claude from the pricing page as read on 2026-09-25
# (trimmed copy: alpaca/tests/fixtures/anthropic_pricing_trimmed.md), gpt-6-astra from its OpenAI model
# page (verified 2026-09-22). USD per million tokens, in the order uncached input, cache read,
# 5m cache write, 1h cache write, cache write (OpenAI), output.
PUBLISHED = {
    "claude-opus-5-5": ("4", "0.20", "5", "8", None, "20"),
    "claude-opus-5": ("5", "0.50", "6.25", "10", None, "25"),
    "claude-opus-4-8": ("5", "0.50", "6.25", "10", None, "25"),
    "claude-fable-5-1": ("10", "0.25", "12.50", "20", None, "50"),
    "claude-sonnet-5": ("2", "0.20", "2.50", "4", None, "10"),
    "claude-haiku-4-5-20251001": ("1", "0.10", "1.25", "2", None, "5"),
    "claude-haiku-4-5": ("1", "0.10", "1.25", "2", None, "5"),
    "gpt-6-astra": ("10", "1", None, None, "12.50", "50"),
}
# Fast mode multipliers from the page's Fast mode pricing table ($8/$40 over $4/$20, $10/$50 over $5/$25).
PUBLISHED_FAST = {"claude-opus-5-5": "2", "claude-opus-5": "2", "claude-opus-4-8": "2"}
# Hand-computed from the official price page on 2026-09-25 for ANTHROPIC_VECTOR.
KNOWN_ANSWERS = (("claude-opus-5-5", "0.0154"), ("claude-opus-5", "0.01975"))
_READ_SOURCES = ("registered-transcript", "project-transcript")
SNAPSHOT_SLACK = Decimal("0.01")
MAX_PASS_ITEMS = 200
_NETWORK_ERRORS = (OSError, ValueError, http.client.HTTPException)
_MILLION = Decimal(1000000)


def _check(name, status, detail, items=(), **extra):
    return {"name": name, "status": status, "detail": detail, "items": list(items), **extra}


def _close(value, expected):
    target = float(expected)
    return (isinstance(value, float) and math.isfinite(value) and math.isfinite(target)
            and math.isclose(value, target, rel_tol=1e-12, abs_tol=1e-15))


def _text(value):
    return format(value.normalize(), "f")


def _cards(cards):
    """(model, card, origin) for every built-in card, then every recorded card."""
    rows = [(model, card, "built-in") for model, card in sorted(pricing._CARDS.items())]
    rows += [(model, card, "recorded") for model, card in sorted(cards.items()) if model not in pricing._CARDS]
    return rows


def _expected(card):
    counts = _VECTOR_COUNTS[card["provider"]]
    parts = {category: Decimal(counts[category]) * Decimal(rate) / _MILLION
             for category, rate in zip(pricing._CATEGORIES, card["rates"]) if rate is not None}
    return sum(parts.values(), Decimal(0)), parts


def _pinned():
    """One item per built-in or published model: the estimate must equal the PUBLISHED prices."""
    items = []
    for model in sorted(set(pricing._CARDS) | set(PUBLISHED)):
        card, published = pricing._CARDS.get(model), PUBLISHED.get(model)
        item = {"kind": "pinned", "model": model, "origin": "built-in"}
        if card is None or published is None:
            items.append(dict(item, status="fail", detail="No built-in card for this published model."
                              if card is None else "No published prices pinned for this built-in card; "
                              "add them to PUBLISHED in alpaca/analytics/selftest.py."))
            continue
        provider = "openai" if published[4] is not None else "anthropic"
        total, _parts = _expected({"provider": provider, "rates": published})
        vector = ANTHROPIC_VECTOR if provider == "anthropic" else OPENAI_VECTOR
        result = pricing.estimate(model, dict(vector))
        fast = pricing.fast_multiplier(model, card, "built-in") if provider == "anthropic" else None
        good = (_close(result.get("total_usd"), total) and card["provider"] == provider
                and (fast is None) == (PUBLISHED_FAST.get(model) is None)
                and (fast is None or Decimal(fast) == Decimal(PUBLISHED_FAST[model])))
        items.append(dict(item, provider=provider, expected_usd=_text(total), estimated_usd=result.get("total_usd"),
                          status="pass" if good else "fail"))
        if PUBLISHED_FAST.get(model):
            expected = total * Decimal(PUBLISHED_FAST[model])
            fast_result = pricing.estimate(model, dict(vector, speed="fast"))
            items.append({"kind": "pinned-fast", "model": model, "origin": "built-in", "provider": provider,
                          "multiplier": PUBLISHED_FAST[model], "expected_usd": _text(expected),
                          "estimated_usd": fast_result.get("total_usd"),
                          "status": "pass" if _close(fast_result.get("total_usd"), expected) else "fail"})
    return items


def arithmetic(cards):
    items = []
    for model, card, origin in _cards(cards):
        vector = ANTHROPIC_VECTOR if card["provider"] == "anthropic" else OPENAI_VECTOR
        total, parts = _expected(card)
        result = pricing.estimate(model, dict(vector), cards=cards)
        breakdown = result.get("breakdown_usd") or {}
        good = (_close(result.get("total_usd"), total)
                and all(_close(breakdown.get(category), value) for category, value in parts.items())
                and (result.get("rate_card") or {}).get("origin") == origin)
        items.append({"kind": "card", "model": model, "origin": origin, "provider": card["provider"],
                      "expected_usd": str(total), "estimated_usd": result.get("total_usd"),
                      "reason_code": result.get("reason_code"), "status": "pass" if good else "fail"})
        fast = pricing.fast_multiplier(model, card, origin) if card["provider"] == "anthropic" else None
        if fast is not None:
            expected = total * Decimal(fast)
            fast_result = pricing.estimate(model, dict(vector, speed="fast"), cards=cards)
            items.append({"kind": "fast", "model": model, "origin": origin, "provider": card["provider"],
                          "multiplier": fast, "expected_usd": str(expected),
                          "estimated_usd": fast_result.get("total_usd"),
                          "status": "pass" if _close(fast_result.get("total_usd"), expected) else "fail"})
    items += _pinned()
    for model, answer in KNOWN_ANSWERS:
        result = pricing.estimate(model, dict(ANTHROPIC_VECTOR))
        items.append({"kind": "known-answer", "model": model, "origin": "built-in", "provider": "anthropic",
                      "expected_usd": answer, "estimated_usd": result.get("total_usd"),
                      "status": "pass" if _close(result.get("total_usd"), Decimal(answer)) else "fail"})
    failed = [item for item in items if item["status"] == "fail"]
    count = lambda *kinds: sum(item["kind"] in kinds for item in items)
    detail = ("%d card(s) priced on a vector with every category nonzero, %d fast mode check(s), "
              "%d built-in card(s) pinned to the published prices, %d hand-computed answer(s)"
              % (count("card"), count("fast", "pinned-fast"), count("pinned"), len(KNOWN_ANSWERS)))
    if failed:
        detail += "; %d disagree: %s" % (len(failed), ", ".join("%s (%s)" % (i["model"], i["kind"]) for i in failed))
    return _check("arithmetic", "fail" if failed else "pass", detail + ".", items)


def _bounds(entry, card):
    rates = {category: Decimal(rate) for category, rate in zip(pricing._CATEGORIES, card["rates"]) if rate is not None}
    base = (Decimal(entry["input_tokens"]) * rates["uncached_input"]
            + Decimal(entry["cache_read_tokens"]) * rates["cached_input"]
            + Decimal(entry["output_tokens"]) * rates["output"])
    writes = Decimal(entry["cache_write_tokens"])
    return (base + writes * rates["cache_write_5m"]) / _MILLION, (base + writes * rates["cache_write_1h"]) / _MILLION


def snapshot_items(sid, reported, cards):
    """Client cost snapshot items for one analysis: only models with token counts, a positive
    cost and a Claude card."""
    items = []
    for entry in reported or []:
        cost = entry.get("cost_usd")
        keys = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")
        if not isinstance(cost, (int, float)) or isinstance(cost, bool) or cost <= 0:
            continue
        if any(type(entry.get(key)) is not int for key in keys):
            continue
        model = entry.get("model")
        card_model, note = model, None
        card, origin = pricing.card_for(model, cards)
        if card is None and isinstance(model, str) and model.endswith("[1m]"):
            card_model = model[:-len("[1m]")]
            card, origin = pricing.card_for(card_model, cards)
            note = ("The client's [1m] context label was dropped: the price page bills the full 1M "
                    "context window at standard rates for these models.")
        if card is None or card["provider"] != "anthropic":
            continue
        low, high = _bounds(entry, card)
        amount = Decimal(str(cost))
        inside = low * (1 - SNAPSHOT_SLACK) <= amount <= high * (1 + SNAPSHOT_SLACK)
        if not inside:
            note = ((note + " ") if note else "") + ("Outside the standard-speed bounds: fast mode, a changed "
                                                     "price, or charges other than tokens.")
        items.append({"session": sid, "model": model, "card_model": card_model, "origin": origin,
                      "cost_usd": cost, "low_usd": float(low), "high_usd": float(high),
                      "tokens": {key: entry[key] for key in keys},
                      "status": "pass" if inside else "warn", "note": note})
    return items


def _snapshot_check(items):
    if not items:
        return _check("client-snapshot", "skip",
                      "No client cost snapshot with token counts and a positive cost on a priced Claude model.",
                      counts={"pass": 0, "warn": 0})
    counts = {"pass": sum(i["status"] == "pass" for i in items), "warn": sum(i["status"] == "warn" for i in items)}
    kept = [i for i in items if i["status"] != "pass"] + [i for i in items if i["status"] == "pass"][:MAX_PASS_ITEMS]
    detail = ("%d client snapshot(s) inside the standard-speed bounds, %d outside"
              % (counts["pass"], counts["warn"]))
    return _check("client-snapshot", "warn" if counts["warn"] else "pass", detail + ".", kept,
                  counts=counts, items_truncated=len(kept) < len(items))


def _was_read(analysis):
    """True when the analysis read a transcript file (registered or the project copy)."""
    coverage = analysis.get("coverage") or {}
    return coverage.get("source") in _READ_SOURCES and not coverage.get("unreadable_sources")


def _coverage_check(tallied, unread):
    unpriced, reasons, counts = tallied["unpriced_models"], tallied["other_reasons"], tallied["counts"]
    items = ([dict(entry, kind="unread-session") for entry in unread]
             + [dict(entry, kind="model-without-card") for entry in unpriced]
             + [dict(entry, kind="unpriced-reason") for entry in reasons])
    detail = "%d session(s), %d response(s), %d priced." % (
        counts["sessions"], counts["responses"], counts["priced_responses"])
    if unpriced:
        detail += " No rate card for %s; run the fix command for each." % ", ".join(
            "%s (%d response(s))" % (entry["model"], entry["responses"]) for entry in unpriced)
    if reasons:
        detail += " %d response(s) unpriced for other reasons: %s." % (tallied["unpriced_other"], ", ".join(
            "%s x%d" % (entry["reason_code"], entry["responses"]) for entry in reasons))
    if unread:
        shown = ", ".join(entry["session"] for entry in unread[:20])
        detail += " %d session(s) with a transcript were not read: %s%s." % (
            len(unread), shown, ", ..." if len(unread) > 20 else "")
    status = "fail" if unpriced or unread else "warn" if reasons else "pass"
    return _check("coverage", status, detail, items,
                  counts=dict(counts, unpriced_models=len(unpriced), unpriced_other=tallied["unpriced_other"],
                              unread_sessions=len(unread)))


def _online_check(root, cards, fetch):
    try:
        page = ratecards.fetch_page(root, fetch=fetch)
    except _NETWORK_ERRORS as exc:
        return _check("online", "warn", "Could not read the official price page %s: %s"
                      % (ratecards.ANTHROPIC_PRICING_MD, exc))
    items = []
    for model, card, origin in _cards(cards):
        if card["provider"] != "anthropic":
            continue
        item = {"model": model, "origin": origin, "name": ratecards.anthropic_name(model)}
        try:
            parsed = ratecards.parse_anthropic(page["text"], model)
        except ValueError as exc:
            items.append(dict(item, status="warn", detail="Not compared: %s" % exc))
            continue
        mismatches = []
        for category, ours, theirs in zip(pricing._CATEGORIES, card["rates"], parsed["rates"]):
            if (ours is None) != (theirs is None) or (ours is not None and Decimal(ours) != Decimal(theirs)):
                mismatches.append({"category": category, "card": ours, "page": theirs})
        fast = pricing.fast_multiplier(model, card, origin)
        if fast is not None and (parsed["fast_multiplier"] is None
                                 or Decimal(fast) != Decimal(parsed["fast_multiplier"])):
            mismatches.append({"category": "fast_multiplier", "card": fast, "page": parsed["fast_multiplier"]})
        items.append(dict(item, status="fail" if mismatches else "pass", mismatches=mismatches, row=parsed["row"]))
    failed = [i["model"] for i in items if i["status"] == "fail"]
    unchecked = [i["model"] for i in items if i["status"] == "warn"]
    detail = "Compared %d Claude card(s) with %s (sha256 %s, saved at %s)." % (
        len(items), page["url"], page["sha256"], page["copy"])
    if failed:
        detail += " The page disagrees for %s." % ", ".join(failed)
    if unchecked:
        detail += " Not compared: %s." % ", ".join(unchecked)
    status = "fail" if failed else "warn" if unchecked else "pass"
    return _check("online", status, detail, items, source_sha256=page["sha256"], source_copy=page["copy"])


def _label_check(root, sessions, fetch):
    found = ratecards.tally(ratecards.analyses(root, sessions, []))["unpriced_models"]
    items, page, page_error = [], None, None
    for gap in found:
        model = gap["model"]
        base = {"model": model, "provider": gap["provider"]}
        if gap["provider"] != "anthropic" or not ratecards.anthropic_name(model):
            items.append(dict(base, status="needs-agent",
                              detail="Read the official price page and run: %s" % gap["fix"]))
            continue
        if page is None and page_error is None:
            try:
                page = ratecards.fetch_page(root, fetch=fetch)
            except _NETWORK_ERRORS as exc:
                page_error = str(exc)
        if page_error is not None:
            items.append(dict(base, status="network-error", detail=page_error))
            continue
        try:
            pieces = ratecards.fetch_anthropic(root, model, page=page)
            out = ratecards.record_pieces(root, pieces)
        except ValueError as exc:
            items.append(dict(base, status="refused", detail=str(exc)))
            continue
        items.append(dict(base, status=out["status"], event_id=out["event_id"], rates=list(pieces["rates"]),
                          fast_multiplier=pieces["fast_multiplier"], source_copy=pieces["label"]["source_copy"]))
    if not items:
        return _check("label-gaps", "pass", "No pricing gaps to label.")
    done = [i["model"] for i in items if i["status"] in ("recorded", "duplicate")]
    refused = [i["model"] for i in items if i["status"] == "refused"]
    open_ = [i["model"] for i in items if i["status"] in ("needs-agent", "network-error")]
    detail = "Labelled %d of %d gap(s) from the official page." % (len(done), len(items))
    if refused:
        detail += " Refused: %s." % ", ".join(refused)
    if open_:
        detail += " Still open: %s." % ", ".join(open_)
    return _check("label-gaps", "fail" if refused else "warn" if open_ else "pass", detail, items)


def run(root, *, online=False, label_gaps=False, sessions=None, fetch=None):
    """Run every check and return the report. Writes nothing except, with label_gaps, the
    recorded cards and their page copy, with online the page copy, and after a whole-project
    coverage scan the pricing gap notice (ratecards.GAPS_FILE)."""
    report = {"schema": 1, "kind": "alpaca-analytics-selftest", "started_at": util.now_iso(),
              "options": {"online": bool(online), "label_gaps": bool(label_gaps),
                          "sessions": list(sessions) if sessions is not None else None},
              "checks": []}
    checks = report["checks"]
    if label_gaps:
        checks.append(_label_check(root, sessions, fetch))
    cards = ratecards.recorded(root)
    checks.append(arithmetic(cards))
    errors, pairs, snapshots, unread = [], [], [], []
    for sid, analysis in ratecards.analyses(root, sessions, errors):
        if not _was_read(analysis):
            coverage = analysis.get("coverage") or {}
            unread.append({"session": sid, "reason": "No transcript was read (state %s, source %s): %s"
                           % (coverage.get("state"), coverage.get("source"), coverage.get("reason"))})
            continue
        pairs.append((sid, {"models": analysis.get("models")}))
        snapshots.extend(snapshot_items(sid, (analysis.get("cost") or {}).get("reported_models"), cards))
    unread += [{"session": error["session"], "reason": "Analysis failed: %s" % error["error"]} for error in errors]
    tallied = ratecards.tally(pairs)
    checks.append(_coverage_check(tallied, unread))
    report["gaps_file"] = None
    if sessions is None:
        ratecards.write_gaps(root, tallied["unpriced_models"], scope="all")
        report["gaps_file"] = ratecards.GAPS_FILE
    checks.append(_snapshot_check(snapshots))
    if online:
        checks.append(_online_check(root, cards, fetch))
    else:
        checks.append(_check("online", "skip", "Not requested; pass --online to compare cards with the official page."))
    if not label_gaps:
        checks.append(_check("label-gaps", "skip", "Not requested; pass --label-gaps to record cards for Claude gaps."))
    summary = {status: sum(check["status"] == status for check in checks) for status in ("pass", "warn", "fail", "skip")}
    report.update(finished_at=util.now_iso(), summary=summary, ratecards_revision=ratecards.revision(root),
                  status="fail" if summary["fail"] else "warn" if summary["warn"] else "pass")
    return report


def save(root, report):
    """Write the report to REPORT_DIR/<UTC stamp>.json; return its path relative to the root."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    folder = os.path.join(root, *REPORT_DIR.split("/"))
    name, count = stamp + ".json", 1
    while os.path.exists(os.path.join(folder, name)):
        name, count = "%s-%d.json" % (stamp, count), count + 1
    report["report_path"] = "%s/%s" % (REPORT_DIR, name)
    util.write_text(os.path.join(folder, name), json.dumps(report, ensure_ascii=True, indent=2) + "\n")
    return report["report_path"]
