"""Fold a rate-limit header pair into one reset instant (M3.12).

Ported from the earlier harness ops/reset-wait.py (doctrine/quota-reset-recovery.md). Given the response
headers from a 429 (or an explicit rate-limit signal), parse Retry-After (delta-seconds OR
HTTP-date, RFC 7231 7.1.3) and/or a provider ratelimit-reset header (RFC-3339) into one absolute
reset instant, and compute wait_cost. When both are present and disagree the provider header
wins (more specific, provider-authoritative) and the disagreement is surfaced, never silently
dropped.

ANY parse failure falls through to the header-absent path: this never raises, because a provider
header-format change must not become an unhandled exception. On the fallback path reset_at and
wait_cost are None (undefined, NOT zero: zero would silently disable HOLD, forever would hang),
and parse_error distinguishes "a header was present but unparseable" from "no header sent".

This computes wait_cost only; it does not decide wait-versus-handoff (that is the dispatch
protocol's rule). `now` is injectable so a FixedClock drives the wait math under test.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

_DELTA = re.compile(r"^\d+$")


def _get(headers, name):
    """Case-insensitive header lookup over a mapping, or None."""
    if not headers:
        return None
    for k, v in headers.items():
        if str(k).lower() == name:
            return v
    return None


def _provider_reset(headers):
    """The value of a provider ratelimit-reset header, if any: a key that mentions reset but is
    not Retry-After. Picks the first such header in iteration order."""
    if not headers:
        return None
    for k, v in headers.items():
        kl = str(k).lower()
        if "reset" in kl and "retry" not in kl:
            return v
    return None


def parse_provider_reset_header(value):
    """RFC-3339, e.g. 2026-09-16T00:05:00Z. ANY parse failure -> None, never raise."""
    if value is None or str(value).strip() == "":
        return None
    text = str(value).strip()
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(normalized)
    except (ValueError, TypeError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def parse_retry_after_header(value, now):
    """Shape 1: delta-seconds (a pure digit string) -> now + delta. Shape 2: an HTTP-date
    (RFC 7231 7.1.3). ANY parse failure -> None, never raise."""
    if value is None or str(value).strip() == "":
        return None
    trimmed = str(value).strip()
    if _DELTA.match(trimmed):
        try:
            return now + timedelta(seconds=int(trimmed))
        except (ValueError, OverflowError):
            return None
    try:
        from email.utils import parsedate_to_datetime
        parsed = parsedate_to_datetime(trimmed)
        if parsed is None:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except (ValueError, TypeError, OverflowError):
        return None


def parse(headers, *, now=None) -> dict:
    """Fold a header mapping into one reset instant. Returns a dict:

        {"reset_at": datetime|None, "wait_cost_seconds": float|None,
         "source": "provider-header"|"retry-after-delta"|"retry-after-date"|"absent-fallback",
         "parse_error": bool, "disagreement": bool}

    Never raises. `now` is an aware datetime (a FixedClock's instant under test), defaulting to
    the wall clock in UTC. reset_at/wait_cost are None on the fallback path (undefined, not zero).
    """
    if now is None:
        now = datetime.now(timezone.utc)

    retry_after_raw = _get(headers, "retry-after")
    provider_raw = _provider_reset(headers)

    provider_parsed = parse_provider_reset_header(provider_raw)
    retry_after_parsed = None
    retry_after_shape = None
    if retry_after_raw is not None and str(retry_after_raw).strip() != "":
        retry_after_parsed = parse_retry_after_header(retry_after_raw, now)
        if retry_after_parsed is not None:
            retry_after_shape = ("retry-after-delta"
                                 if _DELTA.match(str(retry_after_raw).strip())
                                 else "retry-after-date")

    reset_at = None
    source = "absent-fallback"
    parse_error = False
    disagreement = False

    if provider_parsed is not None:
        reset_at = provider_parsed
        source = "provider-header"
        if retry_after_parsed is not None and retry_after_parsed != provider_parsed:
            disagreement = True                     # surfaced, never silently dropped
    elif retry_after_parsed is not None:
        reset_at = retry_after_parsed
        source = retry_after_shape
    else:
        source = "absent-fallback"
        present = ((provider_raw is not None and str(provider_raw).strip() != "")
                   or (retry_after_raw is not None and str(retry_after_raw).strip() != ""))
        if present:
            parse_error = True                      # a header WAS present but failed to parse

    wait_cost_seconds = None
    if reset_at is not None:
        wait_cost_seconds = (reset_at - now).total_seconds()
        if wait_cost_seconds < 0:
            wait_cost_seconds = 0

    return {"reset_at": reset_at, "wait_cost_seconds": wait_cost_seconds, "source": source,
            "parse_error": parse_error, "disagreement": disagreement}


def selftest() -> int:
    """Able-to-pass + able-to-fail controls for the header parser, ported from the earlier harness
    ops/reset-wait.py. The contract is that it NEVER raises: a malformed header falls through to
    the fallback. Returns 0 on all-PASS, else 1."""
    now = datetime(2026, 9, 16, 0, 0, 0, tzinfo=timezone.utc)
    res = []

    r = parse({"Retry-After": "120"}, now=now)
    res.append(("able-to-pass: delta-seconds -> retry-after-delta, wait 120s",
                r["source"] == "retry-after-delta" and r["wait_cost_seconds"] == 120))

    r = parse({"Retry-After": "120", "x-ratelimit-reset": "2026-09-16T00:05:00Z"}, now=now)
    res.append(("provider-header wins the pair, one reset instant",
                r["source"] == "provider-header" and r["wait_cost_seconds"] == 300
                and r["disagreement"] is True))

    r = parse({}, now=now)
    res.append(("able-to-fail: absent headers -> fallback, reset_at None, parse_error False",
                r["source"] == "absent-fallback" and r["reset_at"] is None
                and r["wait_cost_seconds"] is None and r["parse_error"] is False))

    r = parse({"Retry-After": "garbage", "x-ratelimit-reset": "also-garbage"}, now=now)
    res.append(("able-to-fail: malformed headers -> fallback, parse_error True, wait None",
                r["source"] == "absent-fallback" and r["parse_error"] is True
                and r["wait_cost_seconds"] is None))

    try:
        parse({"Retry-After": "\x00nonsense", "x-ratelimit-reset": "2026-13-99T99:99:99Z"}, now=now)
        threw = False
    except Exception:
        threw = True
    res.append(("never-raises: a malformed header falls through instead of raising", not threw))

    print("\n  reset-wait selftest")
    print("  " + "-" * 48)
    for label, ok in res:
        print("  %s  %s" % ("PASS" if ok else "FAIL", label))
    all_pass = all(ok for _, ok in res)
    print("SELFTEST PASS: every reset-wait case passed" if all_pass
          else "SELFTEST FAIL: %d of %d case(s) did not pass"
               % (sum(1 for _, ok in res if not ok), len(res)))
    return 0 if all_pass else 1
