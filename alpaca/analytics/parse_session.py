"""One transcript jsonl -> one session summary dict. Subagent transcripts under <sid>/subagents/** fold in."""
import collections, datetime, glob, hashlib, json, os
from alpaca import where

def notional_cost(model, u, *, cards=None):
    """Compatibility entry point for the canonical exact-model rate-card estimate.

    `cards` are recorded rate cards (ratecards.load); built-in cards are used first."""
    from alpaca.analytics import metrics, pricing
    measured = metrics._usage(u, 'anthropic')
    return pricing.estimate(model, measured or {}, provider='anthropic', cards=cards)['total_usd']


def _valid_usage(u):
    """Compatibility mapping; provider normalization has one implementation."""
    from alpaca.analytics import metrics
    measured = metrics._usage(u, 'anthropic')
    if measured is None:
        return None
    return {old: measured.get(new) for old, new in {
        'input': 'input_tokens', 'output': 'output_tokens', 'cache_read': 'cached_input_tokens',
        'cache_write_5m': 'cache_creation_5m_tokens', 'cache_write_1h': 'cache_creation_1h_tokens'}.items()}


def transcript_files(path, subagents_dir=None):
    sid = os.path.splitext(os.path.basename(path))[0]
    sub_dir = subagents_dir or os.path.join(os.path.dirname(path), sid, "subagents")
    subs = glob.glob(os.path.join(sub_dir, "**", "agent-*.jsonl"), recursive=True) if os.path.isdir(sub_dir) else []
    return [path] + sorted(subs)


def source_signature(path, subagents_dir=None):
    """Stable content fingerprints include every folded subagent transcript."""
    out = []
    for fpath in transcript_files(path, subagents_dir):
        try:
            with open(fpath, "rb") as fh:
                out.append({"path": os.path.abspath(fpath), "sha256": hashlib.sha256(fh.read()).hexdigest()})
        except OSError:
            out.append({"path": os.path.abspath(fpath), "unreadable": True})
    return out

def ishuman(c):
    if not isinstance(c, str) or not c.strip():
        return False
    fl = c.split("\n", 1)[0]
    if fl.startswith(("<command", "<local-command", "<system-reminder", "<task-notification", "<user-prompt")):
        return False
    return "agent-message from=" not in c

def _dt(ts):
    if not ts:
        return None
    return datetime.datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone()

def _empty():
    return {"turns": 0, "tools": 0, "prompts": 0}

def parse(path, subagents_dir=None, *, include_children=True, cards=None):
    """Legacy shape backed by the canonical response/call and pricing normalizer.

    Direct callers may fold children explicitly; project session summaries pass
    include_children=False so their scope matches the selected-conversation API.
    `cards` are recorded rate cards (ratecards.load) for models without a built-in card.
    """
    from alpaca.analytics import detail, metrics
    class SummaryCapture(metrics._AnalyticsCapture):
        def call(self, msg, ident, name, value, source):
            # Legacy edited-file attribution needs only the explicit target path.
            target = {'file_path': value['file_path']} if isinstance(value, dict) and isinstance(value.get('file_path'), str) else None
            return detail._Capture.call(self, msg, ident, name, target, source)

    sid = os.path.splitext(os.path.basename(path))[0]
    files = transcript_files(path, subagents_dir)
    selected = files if include_children else files[:1]
    d = {"sid": sid, "title": "", "first": None, "last": None, "span_s": 0,
         "turns": 0, "tool_calls": 0, "human_prompts": [], "tools": collections.Counter(),
         "files": collections.Counter(), "projects": collections.Counter(),
         "models": collections.Counter(), "hooks": collections.Counter(),
         "hourly": collections.defaultdict(_empty), "subagents": len(files) - 1,
         "transcript": path, "cost_reported_usd": None, "cost_available": False,
         "scope": "main and discovered children" if include_children else "main conversation only"}
    rows, seen_prompts, stamps = [], set(), []
    complete = True
    anomalies = False
    for index, fpath in enumerate(selected):
        budget = detail._Budget()
        budget.remaining = metrics.MAX_READ_BYTES
        capture = SummaryCapture(sid + ":" + str(index), "registered-transcript", budget)
        scan = metrics._Scan(capture)
        scan.read(fpath)
        if index == 0 and capture.stats["unreadable_sources"]:
            raise OSError("transcript source is unreadable")
        responses, _method = scan.responses()
        metrics._finish_entries(responses, cards)
        rows.extend(responses)
        complete &= (capture.stats["total_is_exact"] and not capture.stats["unreadable_sources"]
                     and not scan.snapshot_gaps and not scan.snapshot_resets
                     and not scan.native_snapshot_disagreement)
        anomalies |= any(bool((r["usage"] or {}).get("anomalies")) for r in responses)
        if not index:
            d["title"] = capture.title
        for row in responses:
            if row.get("ts"):
                try:
                    stamps.append(_dt(row["ts"]))
                except (ValueError, TypeError):
                    pass
            d["models"][row["model"]] += 1
            hk = (metrics._hour(row["ts"]) or "?")[:13]
            d["hourly"][hk]["turns"] += 1
        for message in capture.messages:
            stamp = message.get("ts")
            if stamp:
                try:
                    stamps.append(_dt(stamp))
                except (ValueError, TypeError):
                    pass
            hk = (metrics._hour(stamp) or "?")[:13]
            text = message["text"]
            if not index and message["role"] == "user" and ishuman(text) and text.strip() not in seen_prompts:
                seen_prompts.add(text.strip())
                d["human_prompts"].append({"ts": stamp, "text": text[:1600],
                                           **({"queued": True} if message.get("queued") else {})})
                d["hourly"][hk]["prompts"] += 1
        for tool in capture.tools.values():
            # A result without a captured call is a pairing gap, not another call.
            if not any(source["kind"].endswith("transcript") for source in tool["sources"]):
                continue
            name = tool["name"]
            if tool["status"] == "unmatched-result":
                continue
            d["tools"][name] += 1
            owner = next((message for message in capture.messages if tool in message["tools"]), {})
            hk = (metrics._hour(owner.get("ts")) or "?")[:13]
            d["hourly"][hk]["tools"] += 1
            value = tool.get("input")
            fp = value.get("file_path") if isinstance(value, dict) else None
            if fp and name in ("Edit", "Write", "NotebookEdit"):
                d["files"][fp] += 1
                pid = where.attribute({"name": name, "input": value})
                if pid:
                    d["projects"][pid] += 1
        for hook in scan.hooks:
            d["hooks"][hook.get("event") or hook.get("name") or "?"] += 1
    if not d["title"] and d["human_prompts"]:
        d["title"] = d["human_prompts"][0]["text"].split("\n", 1)[0][:180]
    d["turns"] = len(rows)
    d["tool_calls"] = sum(d["tools"].values())
    if stamps:
        d.update(first=min(stamps).isoformat(timespec="seconds"), last=max(stamps).isoformat(timespec="seconds"),
                 span_s=int((max(stamps) - min(stamps)).total_seconds()))
    measured = metrics._sum_usage(row["usage"] for row in rows) or {}
    mapping = {"input": "input_tokens", "output": "output_tokens", "cache_read": "cached_input_tokens",
               "cache_write_5m": "cache_creation_5m_tokens", "cache_write_1h": "cache_creation_1h_tokens"}
    d["tokens"] = {old: measured.get(new) for old, new in mapping.items()}
    d["usage_available"] = bool(rows and complete and not anomalies and all(row["usage"] for row in rows)
                                and all(value is not None for value in d["tokens"].values()))
    d["usage_incomplete"] = not d["usage_available"]
    d["cost_estimate_available"] = bool(rows and complete and all(row["cost"]["total_usd"] is not None for row in rows))
    d["cost_notional_usd"] = sum(row["cost"]["total_usd"] for row in rows) if d["cost_estimate_available"] else None
    d["cost_provenance"] = "rate_card_estimate" if d["cost_estimate_available"] else None
    d["coverage"] = {"file_complete": bool(complete), "session_complete": False,
                      "usage_total_is_exact": d["usage_available"], "totals_are_lower_bounds": not d["usage_available"]}
    for key in ("tools", "files", "models", "hooks", "projects", "hourly"):
        d[key] = dict(d[key])
    return d
