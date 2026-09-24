"""Agent-labelled API rate cards, kept in the append-only record.

pricing.py carries the rate cards that were verified when the code was written. A model
released later has no card there, so every response it made shows no cost. This module
closes that gap without a code change: an agent checks the provider's official price page
and records a card as a `ratecard-verified` event, with the source URL, the fetched page
hash and a saved copy of the page. pricing.estimate reads recorded cards after its built-in
ones.

The loop has three parts:
  detect  gaps() / tally() find models that answered but have no card, with the command
          that fixes each one; write_gaps() leaves a small notice file for the next session.
  check   fetch_anthropic() downloads the official Claude price page, keeps one copy per
          content hash, and parse_anthropic() reads the model's row, refusing anything
          that does not match the expected table exactly.
  label   record() validates a card and appends one event. Other providers are labelled by
          an agent that read the official page itself (method "agent-entered").
"""
import hashlib
import json
import os
import re
import shlex
import sqlite3
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from decimal import Decimal

from alpaca import db, paths, util
from alpaca.analytics import pricing

EVENT = "ratecard-verified"
ANTHROPIC_PRICING_MD = "https://platform.claude.com/docs/en/about-claude/pricing.md"
#: The only host an official-page-parse label may come from, before and after redirects.
OFFICIAL_HOST = "platform.claude.com"
METHODS = ("official-page-parse", "agent-entered")
#: The gap notice keeps one list per scope: "work" (the server's index of work sessions) and
#: "all" (every transcript session, from the selftest and price-check scans).
SCOPES = ("work", "all")
#: Pricing gap notice, relative to the project root; read at session start.
GAPS_FILE = os.path.join(paths.runtime_dir(""), "analytics", "pricing-gaps.json")
#: Saved copies of fetched price pages, one file per content hash, relative to the project root.
SOURCES_DIR = "/".join((paths.runtime_dir(""), "analytics", "pricing-sources"))
MAX_PAGE_BYTES = 8 * 1024 * 1024
FETCH_TIMEOUT = 30          # seconds per socket operation
DOWNLOAD_DEADLINE = 60      # seconds for the whole download
MAX_CELL = 2000             # characters in one price table cell; longer cells are refused

_MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@/+\[\]-]{0,159}\Z")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_NAME_NEW = re.compile(r"claude-([a-z]+)-(\d{1,2})(?:-(\d{1,2}))?(?:-(\d{8}))?\Z")
_NAME_OLD = re.compile(r"claude-(\d{1,2})(?:-(\d{1,2}))?-([a-z]+)(?:-(\d{8}))?\Z")
_HEADING = re.compile(r"#{1,6}\s+(.*?)\s*#*\Z")
_LINK_SUFFIX = re.compile(r"\(\[[^\]]*\]\([^)]*\)\)")
_PRICE = re.compile(r"\$(\d+(?:\.\d+)?|\.\d+) / MTok")
# The Model pricing table columns after "Model", and the pricing category each one feeds.
_HEADER = ("Base input tokens", "5m cache writes", "1h cache writes", "Cache hits and refreshes", "Output tokens")
_HEADER_CATEGORIES = ("uncached_input", "cache_write_5m", "cache_write_1h", "cached_input", "output")


def revision(root, *, conn=None):
    """Hash of every recorded rate card event; "" when none is recorded.

    Cheap enough for the live server to call whenever the record head moves, and used as a
    cache key by analytics so a newly recorded card reprices every cached analysis.
    """
    own = conn is None
    if own:
        if not os.path.isfile(paths.db_path(root)):
            return ""
        conn = db.connect_readonly(root)
    try:
        rows = conn.execute("SELECT id, hash FROM events WHERE kind=? ORDER BY id", (EVENT,)).fetchall()
    finally:
        if own:
            conn.close()
    if not rows:
        return ""
    digest = hashlib.sha256()
    for row in rows:
        digest.update(("%s:%s\n" % (row[0], row[1])).encode("ascii"))
    return digest.hexdigest()


# --- validation ---------------------------------------------------------------------------------

def _https(url, what="source URL"):
    """The URL unchanged when it starts with lowercase "https://" and names a host; the same
    rule pricing._usable applies, so a recorded card is always usable."""
    try:
        parsed = urllib.parse.urlsplit(url) if isinstance(url, str) else None
        good = (parsed is not None and url.startswith("https://") and bool(parsed.hostname)
                and url.isascii() and not any(ch.isspace() for ch in url))
    except ValueError:
        good = False
    if not good:
        raise ValueError("%s must be an https:// URL, not %r" % (what, url))
    return url


def _official(url):
    """True when url is https on OFFICIAL_HOST (default port)."""
    try:
        parsed = urllib.parse.urlsplit(_https(url))
        return parsed.hostname == OFFICIAL_HOST and parsed.port in (None, 443)
    except ValueError:
        return False


def _decimal_text(value, what):
    rate = pricing.decimal_rate(value)
    if rate is None:
        raise ValueError("%s must be a decimal from 0 to %s with at most %d decimal places, not %r"
                         % (what, pricing.MAX_RATE, pricing.MAX_PLACES, value))
    return format(rate.normalize(), "f")


def _provider(provider):
    name = pricing._PROVIDERS.get(provider) if isinstance(provider, str) else None
    if name not in pricing.BILLED:
        raise ValueError("provider must be anthropic or openai, not %r" % (provider,))
    return name


def _rates(provider, rates):
    """Rates in pricing._CATEGORIES order as canonical decimal strings, None where not billed."""
    categories = pricing._CATEGORIES
    if isinstance(rates, dict):
        unknown = sorted(str(key) for key in rates if key not in categories)
        if unknown:
            raise ValueError("rates name unknown categories: %s" % ", ".join(unknown))
        values = [rates.get(category) for category in categories]
    elif isinstance(rates, (list, tuple)) and len(rates) == len(categories):
        values = list(rates)
    else:
        raise ValueError("rates must map category names or list %d values in the order %s"
                         % (len(categories), ", ".join(categories)))
    billed = pricing.BILLED[provider]
    out = []
    for category, value in zip(categories, values):
        if category in billed:
            if value is None:
                raise ValueError("%s rate is required for %s cards" % (category, provider))
            out.append(pricing.rate_text(category, value))
        elif value is not None:
            raise ValueError("%s rate does not apply to %s cards" % (category, provider))
        else:
            out.append(None)
    return tuple(out)


def _fast(value):
    return None if value is None else pricing.fast_text(value)


def _model(model):
    if not isinstance(model, str) or not _MODEL_ID.match(model) or model == "unknown":
        raise ValueError("model must be an exact model ID, not %r" % (model,))
    return model


def _text(value, what, limit=4000):
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > limit:
        raise ValueError("%s must be text of at most %d characters" % (what, limit))
    return value.strip()


def _card(data):
    """(model, card) from one event payload, or None when the payload is malformed."""
    try:
        model = _model(data["model"])
        provider = data["provider"]
        if provider not in pricing.BILLED:
            return None
        source_url = _https(data["source_url"])
        urls = data.get("source_urls")
        urls = [source_url] if urls is None else [_https(url) for url in urls]
        if not isinstance(data.get("verified_at"), str) or not _DATE.match(data["verified_at"]):
            return None
        threshold = data.get("context_threshold_tokens")
        if threshold is not None and (type(threshold) is not int or threshold <= 0):
            return None
        label = data["label"]
        if not isinstance(label, dict) or label.get("method") not in METHODS:
            return None
        sha = label.get("source_sha256")
        if sha is not None and (not isinstance(sha, str) or not _SHA.match(sha)):
            return None
        card = {"provider": provider, "source_url": source_url, "source_urls": urls,
                "rates": _rates(provider, data["rates"]), "context_threshold_tokens": threshold,
                "verified_at": data["verified_at"], "fast_multiplier": _fast(data.get("fast_multiplier")),
                "label": {"method": label["method"], "source_sha256": sha,
                          "source_copy": _text(label.get("source_copy"), "source_copy"),
                          "row": _text(label.get("row"), "row"),
                          "fast_row": _text(label.get("fast_row"), "fast_row"),
                          "verified_by": _text(label.get("verified_by"), "verified_by", 200),
                          "recorded_at": _text(label.get("recorded_at"), "recorded_at", 80)}}
    except Exception:
        # Any malformed payload is skipped; one bad event must never stop analytics.
        return None
    return model, card


def load(conn):
    """Recorded cards by model ID: `ratecard-verified` events in id order, latest per model wins.

    A malformed payload is skipped, so it never replaces an earlier good card.
    """
    if conn is None:
        return {}
    try:
        rows = conn.execute("SELECT data FROM events WHERE kind=? ORDER BY id", (EVENT,)).fetchall()
    except sqlite3.Error:
        return {}
    cards = {}
    for row in rows:
        try:
            data = json.loads(row[0])
            parsed = _card(data) if isinstance(data, dict) else None
        except Exception:
            continue
        if parsed is not None and pricing._usable(parsed[1]):
            cards[parsed[0]] = parsed[1]
    return cards


def recorded(root):
    """load() over a read-only connection; {} when the project has no record yet."""
    if not os.path.isfile(paths.db_path(root)):
        return {}
    conn = db.connect_readonly(root)
    try:
        return load(conn)
    finally:
        conn.close()


# --- names and page parsing ---------------------------------------------------------------------

def anthropic_name(model):
    """The price page name for a Claude model ID: claude-opus-5-5 -> "Claude Opus 5.5".

    A trailing snapshot date is dropped (claude-haiku-4-5-20251001 -> "Claude Haiku 4.5").
    Returns None for anything that is not a Claude model ID.
    """
    if not isinstance(model, str):
        return None
    match = _NAME_NEW.match(model)
    if match:
        family, major, minor = match.group(1), match.group(2), match.group(3)
    else:
        match = _NAME_OLD.match(model)
        if not match:
            return None
        major, minor, family = match.group(1), match.group(2), match.group(3)
    return "Claude %s %s" % (family.capitalize(), major + ("." + minor if minor else ""))


def _cells(line):
    line = line.strip()
    if len(line) < 2 or not (line.startswith("|") and line.endswith("|")):
        return None
    cells = line[1:-1].split("|")
    if any(len(cell) > MAX_CELL for cell in cells):
        raise ValueError("a price table cell is longer than %d characters" % MAX_CELL)
    return [cell.strip() for cell in cells]


def _separator(cells):
    return bool(cells) and all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells)


def _strip_sup(text):
    """Text without <sup>...</sup> footnotes. One left-to-right pass, so linear in the length."""
    lower, out, pos = text.lower(), [], 0
    while True:
        start = lower.find("<sup>", pos)
        end = lower.find("</sup>", start + 5) if start != -1 else -1
        if end == -1:
            break
        out.append(text[pos:start])
        pos = end + len("</sup>")
    out.append(text[pos:])
    return "".join(out)


def _clean_name(cell):
    """A model name cell without footnotes or a trailing parenthesised link."""
    text = _strip_sup(cell).strip()
    if text.endswith("))"):
        start = text.rfind("([")
        if start != -1 and _LINK_SUFFIX.fullmatch(text, start):
            text = text[:start]
    return text.strip()


def _table(lines, heading):
    """The table lines of the first table under the heading named `heading`, or None."""
    start = None
    for index, line in enumerate(lines):
        match = _HEADING.match(line.strip())
        if match and match.group(1).strip().lower() == heading.lower():
            start = index + 1
            break
    if start is None:
        return None
    rows = []
    for line in lines[start:]:
        text = line.strip()
        if text.startswith("|"):
            rows.append(text)
        elif rows or text.startswith("#"):
            break
    return rows


def _price(cell, name):
    match = _PRICE.fullmatch(_strip_sup(cell).strip())
    if not match:
        raise ValueError("the %s price cell %r does not read as $N / MTok" % (name, cell))
    return _decimal_text(match.group(1), "the %s price" % name)


def _fast_mode(lines, name, rates):
    """(multiplier, row) from the Fast mode pricing table; the multiplier only when the input
    and output ratios to the standard rates agree."""
    table = _table(lines, "Fast mode pricing")
    if not table or _cells(table[0]) != ["Model", "Input", "Output"]:
        return None, None
    for line in table[1:]:
        cells = _cells(line)
        if not cells or len(cells) != 3 or _separator(cells):
            continue
        if name not in [_clean_name(part) for part in _clean_name(cells[0]).split(" / ")]:
            continue
        try:
            fast_in, fast_out = Decimal(_price(cells[1], name)), Decimal(_price(cells[2], name))
        except ValueError:
            return None, line
        base_in, base_out = Decimal(rates["uncached_input"]), Decimal(rates["output"])
        if not base_in or not base_out:
            return None, line
        ratio_in, ratio_out = fast_in / base_in, fast_out / base_out
        if ratio_in != ratio_out:
            return None, line
        try:
            return pricing.fast_text(ratio_in), line
        except ValueError:
            return None, line
    return None, None


def parse_anthropic(markdown, model):
    """Read one model's rates from the markdown of the official Claude pricing page.

    Refuses (ValueError) unless the "Model pricing" table has exactly the expected columns and
    exactly one row whose name, without footnotes and a trailing parenthesised link, equals the
    model's page name. "Claude Opus 5" never matches "Claude Opus 5.5".
    """
    name = anthropic_name(model)
    if name is None:
        raise ValueError("%r is not a Claude model ID, so it has no row on the Claude price page" % (model,))
    if not isinstance(markdown, str):
        raise ValueError("the price page is not text")
    lines = markdown.splitlines()
    table = _table(lines, "Model pricing")
    if not table:
        raise ValueError("the page has no Model pricing table")
    header = _cells(table[0]) or []
    if not header or header[0] != "Model" or tuple(header[1:]) != _HEADER:
        raise ValueError("the Model pricing header changed: expected Model | %s, found %s"
                         % (" | ".join(_HEADER), " | ".join(header) or table[0]))
    if len(table) < 2 or not _separator(_cells(table[1])):
        raise ValueError("the Model pricing table has no separator row under its header")
    matches = []
    for line in table[2:]:
        cells = _cells(line)
        if cells and _clean_name(cells[0]) == name:
            matches.append((line, cells))
    if not matches:
        raise ValueError("the Model pricing table has no row for %s" % name)
    if len(matches) > 1:
        raise ValueError("the Model pricing table has %d rows for %s" % (len(matches), name))
    line, cells = matches[0]
    if len(cells) != len(header):
        raise ValueError("the %s row has %d cells, the header has %d" % (name, len(cells), len(header)))
    parsed = {category: _price(cell, name) for category, cell in zip(_HEADER_CATEGORIES, cells[1:])}
    fast, fast_row = _fast_mode(lines, name, parsed)
    return {"model": model, "name": name, "rates": tuple(parsed.get(c) for c in pricing._CATEGORIES),
            "row": line, "fast_multiplier": fast, "fast_row": fast_row}


# --- fetching -----------------------------------------------------------------------------------

class _RedirectGuard(urllib.request.HTTPRedirectHandler):
    """Follow a redirect only to https on the official host."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _official(newurl):
            raise ValueError("refusing a redirect to %r: the price page must stay on https://%s"
                             % (newurl, OFFICIAL_HOST))
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _read(url, timeout, deadline):
    """(body, final URL). Reads in chunks and stops at the deadline or MAX_PAGE_BYTES + 1."""
    opener = urllib.request.build_opener(_RedirectGuard)
    request = urllib.request.Request(url, headers={
        "User-Agent": "alpaca-analytics-price-check", "Accept": "text/markdown, text/plain;q=0.9, */*;q=0.1"})
    chunks, size = [], 0
    with opener.open(request, timeout=timeout) as response:
        while size <= MAX_PAGE_BYTES:
            if time.monotonic() > deadline:
                raise TimeoutError("the price page download took longer than %d s" % DOWNLOAD_DEADLINE)
            chunk = response.read1(65536)
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
        return b"".join(chunks), response.geturl()


def _download(url, timeout=FETCH_TIMEOUT, deadline=DOWNLOAD_DEADLINE):
    """(body, final URL) within `deadline` seconds in total, however slowly the server sends.

    The read runs in a daemon thread; past the deadline the caller gets TimeoutError and the
    thread ends at its next socket timeout."""
    result = {}

    def work():
        try:
            result["value"] = _read(url, timeout, time.monotonic() + deadline)
        except BaseException as exc:
            result["error"] = exc

    thread = threading.Thread(target=work, name="alpaca-price-page", daemon=True)
    thread.start()
    thread.join(deadline)
    if thread.is_alive():
        raise TimeoutError("the price page download did not finish within %s s" % deadline)
    if "error" in result:
        raise result["error"]
    return result["value"]


def _save_once(path, body):
    """Write body to path unless the same bytes are already there. Atomic."""
    digest = hashlib.sha256(body).hexdigest()
    if os.path.isfile(path):
        with open(path, "rb") as fh:
            if hashlib.sha256(fh.read()).hexdigest() == digest:
                return False
    folder = os.path.dirname(path)
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(body)
        mask = os.umask(0)
        os.umask(mask)
        os.chmod(tmp, 0o666 & ~mask)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return True


def fetch_page(root, url=ANTHROPIC_PRICING_MD, *, fetch=None, save=True):
    """Download the official price page once. With save, keep its bytes at SOURCES_DIR/<sha256>.md.

    The URL must be https on OFFICIAL_HOST, and so must the final URL after redirects; the
    returned "url" is the final one. `fetch(url)` returns the body, or (body, final URL).
    """
    if not _official(url):
        raise ValueError("price page URL must be https on %s, not %r" % (OFFICIAL_HOST, url))
    got = (fetch or _download)(url)
    body, final = got if isinstance(got, tuple) else (got, url)
    if final != url and not _official(final):
        raise ValueError("the price page redirected to %r, which is not https on %s" % (final, OFFICIAL_HOST))
    if isinstance(body, str):
        body = body.encode("utf-8")
    if not isinstance(body, bytes) or not body:
        raise ValueError("the price page download is empty")
    if len(body) > MAX_PAGE_BYTES:
        raise ValueError("the price page is larger than %d bytes" % MAX_PAGE_BYTES)
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError("the price page is not UTF-8 text")
    sha = hashlib.sha256(body).hexdigest()
    copy = "%s/%s.md" % (SOURCES_DIR, sha)
    if save:
        _save_once(os.path.join(root, *copy.split("/")), body)
    return {"url": final, "text": text, "sha256": sha, "copy": copy if save else None}


def fetch_anthropic(root, model, *, url=ANTHROPIC_PRICING_MD, fetch=None, save=True, page=None):
    """Fetch (or reuse `page` from fetch_page), save and parse the official Claude price page.

    Returns the pieces record() needs: provider, rates, fast_multiplier, source_url(s) and the
    label (method, source_sha256, source_copy, row, fast_row).
    """
    if page is None:
        page = fetch_page(root, url, fetch=fetch, save=save)
    parsed = parse_anthropic(page["text"], model)
    human = page["url"][:-3] if page["url"].endswith(".md") else page["url"]
    return {"model": model, "provider": "anthropic", "name": parsed["name"], "rates": parsed["rates"],
            "fast_multiplier": parsed["fast_multiplier"], "source_url": human,
            "source_urls": list(dict.fromkeys([human, page["url"]])),
            "label": {"method": "official-page-parse", "source_sha256": page["sha256"],
                      "source_copy": page["copy"], "row": parsed["row"], "fast_row": parsed["fast_row"]}}


# --- recording ----------------------------------------------------------------------------------

def _session():
    for name in ("ALPACA_SESSION_ID", "CLAUDE_CODE_SESSION_ID"):
        value = (os.environ.get(name) or "").strip()
        if value:
            return value
    return "cli"


def _identity(card):
    """What decides a duplicate on any day: provider, rates, fast multiplier, context threshold,
    source URL and page hash. Dates, the matched row text and who recorded it do not count."""
    return json.dumps({"provider": card["provider"], "rates": list(card["rates"]),
                       "fast_multiplier": card.get("fast_multiplier"),
                       "context_threshold_tokens": card.get("context_threshold_tokens"),
                       "source_url": card["source_url"], "source_sha256": card["label"].get("source_sha256")},
                      sort_keys=True)


def build(root, model, *, provider, rates, source_url, method, source_urls=None, verified_at=None,
          fast_multiplier=None, context_threshold_tokens=None, source_sha256=None, source_copy=None,
          row=None, fast_row=None, verified_by=None, require_copy=True):
    """Validate the inputs and return (model, card) without touching the record.

    An official-page-parse label needs the page hash, the matched row and, unless
    require_copy is false (a dry run saves nothing), the saved page copy."""
    model = _model(model)
    if model in pricing._CARDS:
        raise ValueError("%s has a built-in rate card; change alpaca/analytics/pricing.py instead of recording one"
                         % model)
    provider = _provider(provider)
    card_rates = _rates(provider, rates)
    source_url = _https(source_url)
    urls = list(dict.fromkeys([source_url] + [_https(url) for url in (source_urls or [])]))
    if method not in METHODS:
        raise ValueError("method must be one of %s, not %r" % (", ".join(METHODS), method))
    verified_at = verified_at or util.now_iso()[:10]
    if not isinstance(verified_at, str) or not _DATE.match(verified_at):
        raise ValueError("verified_at must be YYYY-MM-DD, not %r" % (verified_at,))
    if context_threshold_tokens is not None and (type(context_threshold_tokens) is not int
                                                 or context_threshold_tokens <= 0):
        raise ValueError("context_threshold_tokens must be a positive integer")
    if source_sha256 is not None and (not isinstance(source_sha256, str) or not _SHA.match(source_sha256)):
        raise ValueError("source_sha256 must be 64 lowercase hex characters")
    if source_copy is not None:
        parts = source_copy.split("/") if isinstance(source_copy, str) else None
        if not parts or os.path.isabs(source_copy) or ".." in parts or source_sha256 is None:
            raise ValueError("source_copy must be a path inside the project with its source_sha256")
        try:
            with open(os.path.join(root, *parts), "rb") as fh:
                held = hashlib.sha256(fh.read()).hexdigest()
        except OSError:
            raise ValueError("source_copy %s is not readable" % source_copy)
        if held != source_sha256:
            raise ValueError("source_copy %s does not hash to source_sha256" % source_copy)
    if method == "official-page-parse" and not (source_sha256 and row and (source_copy or not require_copy)):
        raise ValueError("an official-page-parse label needs source_sha256, source_copy and row")
    if method == "official-page-parse" and not all(_official(url) for url in urls):
        raise ValueError("an official-page-parse label must come from https://%s, not %s"
                         % (OFFICIAL_HOST, ", ".join(urls)))
    session = _session()
    card = {"provider": provider, "source_url": source_url, "source_urls": urls, "rates": card_rates,
            "context_threshold_tokens": context_threshold_tokens, "verified_at": verified_at,
            "fast_multiplier": _fast(fast_multiplier),
            "label": {"method": method, "source_sha256": source_sha256, "source_copy": source_copy,
                      "row": _text(row, "row"), "fast_row": _text(fast_row, "fast_row"),
                      "verified_by": _text(verified_by, "verified_by", 200) or session,
                      "recorded_at": util.now_iso()}}
    if not pricing._usable(card):
        raise ValueError("the card for %s does not pass pricing's own checks" % model)
    return model, card


def record(root, model, *, provider, rates, source_url, method, dry_run=False, **fields):
    """Validate and append one `ratecard-verified` event.

    Returns {"status": "recorded"|"duplicate"|"dry-run", "model", "card", "event_id"}. A card
    identical to the latest recorded one for the model is a duplicate and appends nothing.
    Raises ValueError for invalid input or a model that already has a built-in card.
    """
    model, card = build(root, model, provider=provider, rates=rates, source_url=source_url,
                        method=method, require_copy=not dry_run, **fields)
    out = {"status": None, "model": model, "card": card, "event_id": None}
    if dry_run:
        latest = recorded(root).get(model)
        out["status"] = "duplicate" if latest and _identity(latest) == _identity(card) else "dry-run"
        return out
    conn = db.connect(root)
    try:
        with db.transaction(conn):
            latest = load(conn).get(model)
            if latest is not None and _identity(latest) == _identity(card):
                out["status"] = "duplicate"
            else:
                data = dict(card, model=model, rates=list(card["rates"]))
                event = db.append_event(conn, session=_session(), actor="analytics", kind=EVENT,
                                        ref=model, data=data, conn_in_txn=True)
                out.update(status="recorded", event_id=event["id"])
    finally:
        conn.close()
    return out


def record_pieces(root, pieces, *, dry_run=False):
    """record() the output of fetch_anthropic."""
    label = pieces["label"]
    return record(root, pieces["model"], provider=pieces["provider"], rates=pieces["rates"],
                  source_url=pieces["source_url"], source_urls=pieces["source_urls"],
                  method=label["method"], fast_multiplier=pieces["fast_multiplier"],
                  source_sha256=label["source_sha256"], source_copy=label["source_copy"],
                  row=label["row"], fast_row=label.get("fast_row"), dry_run=dry_run)


# --- gap detection ------------------------------------------------------------------------------

def fixable(model):
    """A model ID a card could be recorded for (not the placeholder for a missing name)."""
    return isinstance(model, str) and model != "unknown" and bool(_MODEL_ID.match(model))


def fix_command(model, provider):
    """The command that records a card for a model without one."""
    quoted = shlex.quote(model)
    provider = provider if provider in pricing.BILLED else None
    if provider == "anthropic" and anthropic_name(model):
        return "bin/alpaca analytics price-check --model %s" % quoted
    writes = {"anthropic": "--cache-write-5m A --cache-write-1h B",
              "openai": "--cache-write C"}.get(provider, "[--cache-write-5m A --cache-write-1h B | --cache-write C]")
    return ("bin/alpaca analytics price-label --model %s --provider %s --source <official URL> "
            "--input X --output Y --cache-read Z %s" % (quoted, provider or "<provider>", writes))


def session_ids(root, sessions=None):
    """The sessions to scan: the ones given (IDs or {"sid": ...} rows), else every session
    with a transcript."""
    if sessions is not None:
        return [item.get("sid") if isinstance(item, dict) else item for item in sessions]
    from alpaca.analytics import build_index
    conn = db.connect_readonly(root)
    try:
        return sorted(build_index._transcripts(root, conn))
    finally:
        conn.close()


def analyses(root, sessions=None, errors=None):
    """Yield (sid, analysis) for each session; append unreadable ones to `errors`."""
    from alpaca.analytics import metrics
    for sid in session_ids(root, sessions):
        try:
            yield sid, metrics.analyze(root, sid)
        except (KeyError, ValueError, OSError) as exc:
            if errors is not None:
                errors.append({"session": sid, "error": "%s: %s" % (type(exc).__name__, exc)})


def tally(items):
    """Fold (sid, analysis) pairs into pricing gaps.

    Returns unpriced_models (models with responses but no card, most responses first, each
    with its fix command), unpriced_other (responses unpriced for any other reason),
    other_reasons (those grouped by reason_code) and counts.
    """
    models, other = {}, {}
    counts = {"sessions": 0, "responses": 0, "priced_responses": 0}
    for sid, analysis in items:
        counts["sessions"] += 1
        for row in analysis.get("models") or []:
            counts["responses"] += row.get("responses") or 0
            counts["priced_responses"] += row.get("costed_responses") or 0
            for code, count in (row.get("unpriced") or {}).items():
                if code == "unverified_model" and fixable(row.get("model")):
                    entry = models.setdefault(row["model"], {"provider": row.get("provider"), "responses": 0,
                                                             "sessions": set()})
                    entry["responses"] += count
                    entry["sessions"].add(sid)
                    continue
                code = "missing_model" if code == "unverified_model" else code
                group = other.setdefault(code, {"responses": 0, "sessions": set(), "models": set()})
                group["responses"] += count
                group["sessions"].add(sid)
                group["models"].add(str(row.get("model")))
    unpriced = [{"model": model, "provider": entry["provider"], "responses": entry["responses"],
                 "sessions": len(entry["sessions"]), "fix": fix_command(model, entry["provider"])}
                for model, entry in models.items()]
    unpriced.sort(key=lambda item: (-item["responses"], item["model"]))
    reasons = [{"reason_code": code, "responses": group["responses"], "sessions": len(group["sessions"]),
                "models": sorted(group["models"])} for code, group in other.items()]
    reasons.sort(key=lambda item: (-item["responses"], item["reason_code"]))
    return {"unpriced_models": unpriced, "unpriced_other": sum(item["responses"] for item in reasons),
            "other_reasons": reasons, "counts": counts}


def gaps(root, sessions=None):
    """Models with responses but no card (built-in or recorded), in the unpriced_models shape."""
    return tally(analyses(root, sessions))["unpriced_models"]


# --- gap notice file ----------------------------------------------------------------------------
#
# {"schema": 2, "written_at", "revision", "scopes": {"work": [...], "all": [...]}, "gaps": [union]}
# Each writer owns one scope, so the server's work-session index and a whole-project scan never
# erase each other's gaps. Readers get the union with models that now have a card dropped.

def gaps_path(root):
    return os.path.join(root, GAPS_FILE)


def _gap_entry(entry):
    """A checked gap entry. `fix` is always rebuilt from the checked model and provider; text
    from the file is never passed on."""
    if not isinstance(entry, dict) or not fixable(entry.get("model")):
        return None
    provider = entry.get("provider") if entry.get("provider") in pricing.BILLED else None
    number = lambda key: entry[key] if type(entry.get(key)) is int and entry[key] >= 0 else 0
    return {"model": entry["model"], "provider": provider, "responses": number("responses"),
            "sessions": number("sessions"), "fix": fix_command(entry["model"], provider)}


def _entries(items):
    return [item for item in (_gap_entry(entry) for entry in items or []) if item]


def _union(lists):
    """One entry per model, keeping the larger counts, most responses first."""
    merged = {}
    for items in lists:
        for entry in items:
            known = merged.get(entry["model"])
            if known is None:
                merged[entry["model"]] = dict(entry)
            else:
                known.update(responses=max(known["responses"], entry["responses"]),
                             sessions=max(known["sessions"], entry["sessions"]),
                             provider=known["provider"] or entry["provider"])
                known["fix"] = fix_command(known["model"], known["provider"])
    return sorted(merged.values(), key=lambda item: (-item["responses"], item["model"]))


def _notice(root):
    """(raw file content or None, {scope: checked entries}). A schema 1 file is the work scope."""
    try:
        data = json.loads(util.read_text(gaps_path(root)))
    except (OSError, ValueError):
        return None, {}
    if not isinstance(data, dict):
        return None, {}
    scopes = data.get("scopes")
    if isinstance(scopes, dict):
        return data, {scope: _entries(scopes.get(scope)) for scope in SCOPES if isinstance(scopes.get(scope), list)}
    if isinstance(data.get("gaps"), list):
        return data, {"work": _entries(data["gaps"])}
    return data, {}


def _write_notice(root, previous, scopes):
    scopes = {scope: scopes.get(scope, []) for scope in SCOPES}
    content = {"schema": 2, "revision": revision(root), "scopes": scopes, "gaps": _union(scopes.values())}
    if isinstance(previous, dict) and {key: value for key, value in previous.items() if key != "written_at"} == content:
        return False
    util.write_text(gaps_path(root), json.dumps(dict(content, written_at=util.now_iso()),
                                                ensure_ascii=True, indent=2, sort_keys=True) + "\n")
    return True


def write_gaps(root, unpriced_models, scope="work"):
    """Replace one scope of the pricing gap notice ("work" or "all") and keep the others; skip
    the write when only written_at would change."""
    if scope not in SCOPES:
        raise ValueError("scope must be one of %s, not %r" % (", ".join(SCOPES), scope))
    entries = _entries(unpriced_models)
    previous, scopes = _notice(root)
    scopes[scope] = entries
    written = _write_notice(root, previous, scopes)
    return {"path": GAPS_FILE, "written": written, "scope": scope, "gaps": entries}


def _without_cards(root, entries):
    try:
        cards = recorded(root)
    except sqlite3.Error:
        cards = {}
    return [entry for entry in entries if pricing.card_for(entry["model"], cards)[0] is None]


def read_gaps(root):
    """The union of every scope of the notice file, without models that now have a card;
    [] when the file is missing or malformed. Never raises on file content."""
    try:
        _data, scopes = _notice(root)
        return _without_cards(root, _union(scopes.values()))
    except Exception:
        return []


def refresh_gaps(root):
    """After a card is recorded, drop carded models from every scope. Never creates the file."""
    previous, scopes = _notice(root)
    if previous is None:
        return False
    return _write_notice(root, previous, {scope: _without_cards(root, entries) for scope, entries in scopes.items()})
