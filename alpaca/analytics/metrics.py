"""Bounded, source-scoped measured analytics for registered session transcripts.

A provider response is the unit of usage, not an assistant content block. Claude
response IDs and Codex native usage records prevent streamed block and snapshot
mirrors from multiplying usage. Canonical input includes cache reads and writes;
reasoning is an output subset. Neither is added to total tokens a second time.
"""
from __future__ import annotations

from collections import OrderedDict
from datetime import datetime, timezone
import copy
import json
import math
import os
import re
import threading

from alpaca import pool, transcripts
from alpaca.analytics import detail

MAX_LEDGER = 5000
MAX_HOOKS = 2000
MAX_READ_BYTES = 64 * 1024 * 1024
MAX_LINE_BYTES = 16 * 1024 * 1024
MAX_CACHE = 32
MAX_CACHE_BYTES = 32 * 1024 * 1024
TOKEN_KEYS = ('input_tokens', 'uncached_input_tokens', 'cached_input_tokens',
              'cache_write_input_tokens', 'cache_creation_5m_tokens',
              'cache_creation_1h_tokens', 'output_tokens', 'reasoning_output_tokens',
              'total_tokens')
_CACHE = OrderedDict()
_CACHE_LOCK = threading.RLock()


def _number(value):
    return value if type(value) in (int, float) and 0 <= value <= 9007199254740991 and math.isfinite(value) else None


def _int(value):
    return value if type(value) is int and 0 <= value <= 9007199254740991 else None


def _mapping(value):
    return value if isinstance(value, dict) else {}


def _safe(value, limit=200):
    return detail._redact(value)[:limit] if isinstance(value, str) else None


def _hour(value):
    if not isinstance(value, str):
        return None
    try:
        stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if stamp.tzinfo is None:
            return None
        return stamp.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0).isoformat().replace('+00:00', 'Z')
    except (ValueError, OverflowError):
        return None


def _usage(raw, provider):
    if not isinstance(raw, dict):
        return None
    inp, out = _int(raw.get('input_tokens')), _int(raw.get('output_tokens'))
    if inp is None or out is None:
        return None
    if provider == 'anthropic':
        cached = _int(raw.get('cache_read_input_tokens'))
        write = _int(raw.get('cache_creation_input_tokens'))
        creation = _mapping(raw.get('cache_creation'))
        short = _int(creation.get('ephemeral_5m_input_tokens'))
        long = _int(creation.get('ephemeral_1h_input_tokens'))
        if write is None and short is not None and long is not None:
            write = short + long
        if write == 0:
            short = 0 if short is None else short
            long = 0 if long is None else long
        reasoning = _int(_mapping(raw.get('output_tokens_details')).get('thinking_tokens'))
        total_input = inp + cached + write if cached is not None and write is not None else None
        uncached = inp
    else:
        cached = _int(raw.get('cached_input_tokens'))
        write = _int(raw.get('cache_write_input_tokens'))
        short, long = 0, 0
        reasoning = _int(raw.get('reasoning_output_tokens'))
        total_input = inp
        uncached = max(0, inp - cached - write) if cached is not None and write is not None else None
    result = {'input_tokens': total_input, 'uncached_input_tokens': uncached,
            'cached_input_tokens': cached, 'cache_write_input_tokens': write,
            'cache_creation_5m_tokens': short, 'cache_creation_1h_tokens': long,
            'output_tokens': out, 'reasoning_output_tokens': reasoning,
            'total_tokens': total_input + out if total_input is not None else None}
    anomalies = []
    if provider == 'openai' and cached is not None and write is not None and cached + write > inp:
        anomalies.append('Cache input categories exceed total input.')
        result.update(cached_input_tokens=None, cache_write_input_tokens=None, uncached_input_tokens=None)
    if short is not None and long is not None and write is not None and short + long > write:
        anomalies.append('Cache duration categories exceed cache write input.')
        result.update(cache_creation_5m_tokens=None, cache_creation_1h_tokens=None)
    if reasoning is not None and reasoning > out:
        anomalies.append('Reasoning output exceeds total output.')
        result['reasoning_output_tokens'] = None
    if raw.get('total_tokens') is not None and _int(raw['total_tokens']) != result['total_tokens']:
        anomalies.append('Recorded total disagrees with input plus output; total is derived from those counters.')
    if anomalies:
        result['anomalies'] = anomalies
    for key in ('service_tier', 'speed', 'inference_geo', 'data_residency'):
        if isinstance(raw.get(key), str) and raw[key] != 'not_available':
            result[key] = _safe(raw[key], 80)
    return result


def _sum_usage(items):
    items = [item for item in items if item is not None]
    if not items:
        return None
    # A missing category remains unavailable, rather than silently becoming zero.
    return {key: sum(item[key] for item in items) if all(item.get(key) is not None for item in items) else None
            for key in TOKEN_KEYS}


def _merge_usage(previous, incoming):
    if previous is None:
        return incoming
    if incoming is None:
        return previous
    # Streaming blocks may contain a later final output count. Repeated blocks
    # are one response; retain the largest recorded counter, never their sum.
    result = {key: max(value for value in (previous.get(key), incoming.get(key)) if value is not None)
            if previous.get(key) is not None or incoming.get(key) is not None else None for key in TOKEN_KEYS}
    for key in ('service_tier', 'speed', 'inference_geo', 'data_residency'):
        if key in incoming or key in previous:
            result[key] = incoming.get(key, previous.get(key))
    anomalies = list(dict.fromkeys(previous.get('anomalies', []) + incoming.get('anomalies', [])))
    if anomalies:
        result['anomalies'] = anomalies
    return result


def _entry(ident, ts, model, provider, usage, source, messages=(), association='provider-response-id'):
    return {'id': _safe(str(ident)), 'ts': ts, 'model': model or 'unknown', 'provider': provider,
            'usage': usage, 'source': source, 'message_ids': [], 'excerpt': '', 'tools': [],
            'association': association, '_messages': list(messages), '_sources': [source]}


class _AnalyticsCapture(detail._Capture):
    """Share source safety and IDs while retaining only visible excerpts/metadata.

    Compaction exports can have multi-megabyte replacement histories. The larger
    per-line analytics budget lets us count their metadata; those histories are
    never normalized or returned. Tool inputs and outputs are unnecessary here.
    """
    def rows(self, path, kind):
        return super().rows(path, kind, max_line_bytes=MAX_LINE_BYTES)

    def message(self, role, text, source, ts=None, identity=None, **extra):
        excerpt = _safe(text, 600) or ''
        return super().message(role, excerpt, source, ts, identity, **extra)

    def call(self, msg, ident, name, value, source):
        return super().call(msg, ident, name, None, source)

    def result(self, ident, value, source, ts=None, error=False, truncated=False):
        return super().result(ident, None, source, ts, error)

    def pool(self, path, sid):
        for rec, source in self.rows(path, 'tool-pool'):
            if rec.get('sid') != sid or rec.get('phase') not in ('pre', 'post'):
                continue
            ident = str(rec.get('tool_use_id') or 'pool-line-' + str(source['line']))
            tool = self.tools.get(ident)
            if tool is None:
                msg = self.message('tool', '', source, _safe(rec.get('ts'), 80), identity=ident)
                tool = self.call(msg, ident, rec.get('tool'), None, source)
            if source not in tool['sources']:
                tool['sources'].append(source)
            if rec['phase'] == 'post' and rec.get('response') is not None:
                self.result(ident, None, source, _safe(rec.get('ts'), 80), bool(rec.get('is_error')))


class _Scan:
    def __init__(self, capture):
        self.capture = capture
        self.claude = OrderedDict()
        self.request_ids = {}
        self.native = OrderedDict()
        self.snapshots = []
        self.pending = []
        self.snapshot_pending = []
        self.model = None
        self.capacity = None
        self.context_points = []
        self.compactions = []
        self.hooks = []
        self.seen_hooks = set()
        self.reported = None
        self.reported_models = []
        self.reported_source = None
        self.duplicate_responses = 0
        self.conflicting_responses = 0
        self.snapshot_resets = 0
        self.snapshot_gaps = 0
        self.native_snapshot_disagreement = False
        self.synthetic_records_excluded = 0
        self.actions = OrderedDict()
        self.title = ''

    def read(self, path):
        for rec, source in self.capture.rows(path, self.capture.kind):
            before = len(self.capture.messages)
            # detail's normalization is the shared policy for visible content and
            # tool pairing. It discards private reasoning and replacement history.
            self.capture.normalize(rec, source)
            fresh = self.capture.messages[before:]
            typ = rec.get('type')
            ts = _safe(rec.get('timestamp') or rec.get('ts'), 80)
            payload = _mapping(rec.get('payload'))
            if typ == 'turn_context':
                self.model = _safe(payload.get('model')) or self.model
            if typ == 'assistant':
                message = _mapping(rec.get('message'))
                if message.get('model') == '<synthetic>':
                    self.synthetic_records_excluded += 1
                    continue
                ident = message.get('id') or rec.get('requestId') or rec.get('uuid') or 'line-' + str(source['line'])
                ident = str(ident)
                request = rec.get('requestId')
                if isinstance(request, str):
                    ident = self.request_ids.setdefault(request, ident)
                entry = self.claude.get(ident)
                measured = _usage(message.get('usage'), 'anthropic')
                if entry is None:
                    entry = self.claude[ident] = _entry(ident, ts, _safe(message.get('model')),
                                                       'anthropic', measured, source, fresh)
                else:
                    self.duplicate_responses += 1
                    entry['usage'] = _merge_usage(entry['usage'], measured)
                    entry['_messages'].extend(fresh)
                    entry['_sources'].append(source)
            if typ in ('response_item', 'event_msg'):
                visible = [msg for msg in fresh if msg['role'] == 'assistant']
                self.pending.extend(visible)
                self.snapshot_pending.extend(visible)
            if typ == 'token_usage_record' or payload.get('type') == 'token_usage_record':
                ident = str(payload.get('response_id') or 'line-' + str(source['line']))
                measured = _usage(payload.get('usage'), 'openai')
                if ident in self.native:
                    self.duplicate_responses += 1
                    previous = self.native[ident]
                    if previous['usage'] != measured or (payload.get('model') and previous['model'] != payload['model']):
                        self.conflicting_responses += 1
                    # Native terminal usage records are not streamed counters.
                    # Keep the first observation and disclose conflicting replay.
                    previous['_messages'].extend(self.pending)
                    previous['_sources'].append(source)
                    self.pending = []
                else:
                    self.native[ident] = _entry(ident, ts, _safe(payload.get('model')) or self.model,
                                               'openai', measured, source, self.pending,
                                               association='response-window')
                    self.pending = []
            if typ == 'event_msg' and payload.get('type') == 'token_count':
                info = _mapping(payload.get('info'))
                capacity = _int(info.get('model_context_window'))
                if capacity:
                    self.capacity = capacity
                last = _usage(info.get('last_token_usage'), 'openai')
                total = _usage(info.get('total_token_usage'), 'openai')
                self.snapshots.append({'ts': ts, 'model': self.model, 'usage': last, 'total': total,
                                       'source': source, 'capacity': self.capacity,
                                       'messages': self.snapshot_pending})
                if total and self.native:
                    measured_input = sum(entry['usage']['input_tokens'] or 0 for entry in self.native.values() if entry['usage'])
                    measured_output = sum(entry['usage']['output_tokens'] or 0 for entry in self.native.values() if entry['usage'])
                    if total['input_tokens'] > measured_input or total['output_tokens'] > measured_output:
                        self.native_snapshot_disagreement = True
                self.snapshot_pending = []
                if last and last['input_tokens'] is not None:
                    self.context_points.append({'ts': ts, 'tokens': last['input_tokens'],
                                                'capacity': self.capacity, 'source': source})
            if typ == 'compacted' or (typ == 'system' and rec.get('subtype') == 'compact_boundary'):
                self.compactions.append({'ts': ts, 'source': source, 'kind': typ})
            if typ == 'cost-state' and _number(rec.get('totalCostUSD')) is not None:
                self.reported = rec['totalCostUSD']
                self.reported_source = source
                # These client aggregates can cover more than the captured parent.
                # Preserve them separately from per-response measured counters.
                self.reported_models = [{'model': _safe(model), 'cost_usd': _number(_mapping(value).get('costUSD'))}
                                        for model, value in list(_mapping(rec.get('modelUsage')).items())[:200]]
            if typ == 'event_msg' and payload.get('type') == 'item_completed':
                item = _mapping(payload.get('item'))
                action_type = item.get('type')
                if action_type in ('CommandExecution', 'FileChange', 'DynamicToolCall',
                                   'Extension', 'ImageView', 'SubAgentActivity'):
                    ident = _safe(item.get('id')) or 'line-' + str(source['line'])
                    duration = _mapping(item.get('duration'))
                    secs, nanos = _number(duration.get('secs')), _number(duration.get('nanos'))
                    code = item.get('exit_code')
                    code = code if type(code) is int and -65536 <= code <= 65536 else None
                    status = _safe(item.get('status'), 80)
                    self.actions[(action_type, ident)] = {
                        'id': ident, 'ts': ts, 'type': action_type, 'status': status,
                        'exit_code': code, 'source': source,
                        'failed': status in ('failed', 'error') or (code is not None and code != 0),
                        'duration_ms': secs * 1000 + (nanos or 0) / 1000000 if secs is not None else None}
            self.hook(rec, source, ts)

    def hook(self, rec, source, ts):
        attachment = _mapping(rec.get('attachment'))
        data = _mapping(rec.get('data'))
        payload = _mapping(rec.get('payload'))
        item = next((obj for obj in (attachment, data, payload)
                     if isinstance(obj.get('type'), str) and obj['type'].startswith('hook_')), None)
        summary = rec.get('type') == 'system' and rec.get('subtype') == 'stop_hook_summary'
        if item is None and not summary:
            return
        identity = str(rec.get('uuid') or source['line'])
        if identity in self.seen_hooks:
            return
        self.seen_hooks.add(identity)
        if summary:
            infos = rec.get('hookInfos') if isinstance(rec.get('hookInfos'), list) else []
            durations = [_number(_mapping(i).get('durationMs')) for i in infos]
            event = {'type': 'stop_hook_summary', 'event': 'Stop', 'name': 'Stop hook summary',
                     'status': 'error' if rec.get('hookErrors') else 'completed',
                     'executions_reported': _int(rec.get('hookCount')),
                     'duration_ms': sum(d for d in durations if d is not None) if any(d is not None for d in durations) else None,
                     'tool': None}
        else:
            kind = _safe(item['type'])
            event = {'type': kind, 'event': _safe(item.get('hookEvent')),
                     'name': _safe(item.get('hookName')),
                     'status': 'completed' if kind == 'hook_success' else 'error' if kind in ('hook_error', 'hook_failure', 'hook_blocking_error') else 'recorded',
                     'tool': _safe(item.get('toolName') or item.get('tool_name')),
                     'executions_reported': None, 'duration_ms': _number(item.get('durationMs'))}
        self.hooks.append({**event, 'ts': ts, 'source': source})

    def responses(self):
        if self.native:
            return list(self.claude.values()) + list(self.native.values()), 'native-response-records'
        if self.snapshots:
            rows, previous = [], None
            for snap in self.snapshots:
                total, last = snap['total'], snap['usage']
                if total is None:
                    # No cumulative identity: repeated last snapshots cannot be
                    # distinguished safely. Keep context but no invented response.
                    self.snapshot_gaps += 1
                    continue
                if previous == total:
                    continue
                if previous and any(total.get(key) is not None and previous.get(key) is not None
                                    and total[key] < previous[key] for key in ('input_tokens', 'output_tokens')):
                    self.snapshot_resets += 1
                    previous = total
                    continue
                if last is None:
                    self.snapshot_gaps += 1
                    previous = total
                    continue
                delta = {key: total[key] - (previous[key] if previous else 0)
                         if total.get(key) is not None and (not previous or previous.get(key) is not None) else None
                         for key in TOKEN_KEYS}
                if any(delta.get(key) is not None and last.get(key) is not None and delta[key] < last[key]
                       for key in ('input_tokens', 'output_tokens')):
                    self.snapshot_gaps += 1
                    previous = total
                    continue
                if any(delta.get(key) != last.get(key) for key in ('input_tokens', 'output_tokens')):
                    self.snapshot_gaps += 1
                previous = total
                rows.append(_entry('snapshot-' + detail._hash(str(snap['source'])), snap['ts'], snap['model'],
                                   'openai', last, snap['source'], snap['messages'],
                                   association='snapshot-window'))
            return list(self.claude.values()) + rows, 'deduplicated-last-usage-snapshots'
        return list(self.claude.values()), 'claude-response-ids' if self.claude else 'unavailable'


def _finish_entries(rows):
    from alpaca.analytics import pricing
    for row in rows:
        messages = row.pop('_messages')
        row.pop('_sources')
        row['message_ids'] = list(dict.fromkeys(msg['id'] for msg in messages))
        texts = list(dict.fromkeys(str(msg['text']) for msg in messages if msg['text']))
        row['excerpt'] = _safe('\n'.join(texts), 600) or ''
        tools = OrderedDict()
        for message in messages:
            for tool in message['tools']:
                tools[tool['id']] = {'id': _safe(tool['id']), 'name': _safe(tool['name']), 'status': tool['status']}
        row['tools'] = list(tools.values())
        row['context_tokens'] = row['usage']['input_tokens'] if row['usage'] else None
        row['cost'] = pricing.estimate(row['model'], row['usage'] or {}, provider=row['provider'])


def _context(scan, rows):
    native_points = [{'ts': row['ts'], 'tokens': row['context_tokens'], 'capacity': None,
                      'source': row['source'], '_native': True}
                     for row in rows if row['context_tokens'] is not None]
    points = native_points if not scan.snapshots else [dict(point) for point in scan.context_points]
    if scan.native:
        # Keep every native measurement, including intermediate peaks with no
        # mirror snapshot. Snapshots supply recorded capacity and can fill gaps.
        points += native_points if scan.snapshots else []
        points.sort(key=lambda point: point['source']['line'])
        merged, capacity = [], None
        for point in points:
            capacity = point.get('capacity') or capacity
            point['capacity'] = capacity
            if not point.get('_native') and merged and merged[-1]['tokens'] == point['tokens']:
                merged[-1]['capacity'] = capacity
            else:
                merged.append(point)
        points = merged
    unique = []
    for point in points:
        if unique and all(unique[-1][key] == point[key] for key in ('ts', 'tokens', 'capacity')):
            continue
        point = dict(point)
        point.pop('_native', None)
        point['percent'] = point['tokens'] / point['capacity'] * 100 if point['capacity'] else None
        unique.append(point)
    latest = unique[-1] if unique else {}
    return {'basis': 'input_tokens_including_cache',
            'description': 'Observed prompt input size, including cache; not cumulative session tokens.',
            'current_tokens': latest.get('tokens'), 'peak_tokens': max((p['tokens'] for p in unique), default=None),
            'capacity_tokens': latest.get('capacity', scan.capacity), 'current_percent': latest.get('percent'),
            'peak_percent': max((p['percent'] for p in unique if p['percent'] is not None), default=None),
            'compactions': scan.compactions[:MAX_LEDGER], 'compactions_total': len(scan.compactions),
            'timeline': unique[-MAX_LEDGER:], 'timeline_truncated': len(unique) > MAX_LEDGER}


def _rollups(rows, capture, hooks):
    model_rows, hourly, tool_stats = OrderedDict(), OrderedDict(), OrderedDict()
    for row in rows:
        model_rows.setdefault(row['model'], []).append(row)
        hour_key = _hour(row['ts'])
        if hour_key:
            hour = hourly.setdefault(hour_key, {'_rows': [], 'tools': 0, 'hooks': 0})
            hour['_rows'].append(row)
        cost = row['cost']['total_usd']
        for tool in row['tools']:
            stat = tool_stats.setdefault(tool['name'], {'name': tool['name'], 'count': 0, 'errors': 0,
                'estimated_allocated_cost_usd': None, 'costed_calls': 0,
                'allocation': 'equal share across tools in a response; not intrinsic tool pricing'})
            if cost is not None and row['tools']:
                stat['estimated_allocated_cost_usd'] = (stat['estimated_allocated_cost_usd'] or 0) + cost / len(row['tools'])
                stat['costed_calls'] += 1
    # Count every captured tool once, including unmatched or unpriced calls.
    for tool in capture.tools.values():
        name = _safe(tool['name']) or 'tool'
        stat = tool_stats.setdefault(name, {'name': name, 'count': 0, 'errors': 0,
            'estimated_allocated_cost_usd': None, 'costed_calls': 0,
            'allocation': 'equal share across tools in a response; not intrinsic tool pricing'})
        stat['count'] += 1
        stat['errors'] += tool['status'] == 'error'
    for msg in capture.messages:
        hour_key = _hour(msg['ts'])
        if hour_key:
            hourly.setdefault(hour_key, {'_rows': [], 'tools': 0, 'hooks': 0})['tools'] += len(msg['tools'])
    for hook in hooks:
        hour_key = _hour(hook['ts'])
        if hour_key:
            hourly.setdefault(hour_key, {'_rows': [], 'tools': 0, 'hooks': 0})['hooks'] += 1
    def aggregate(group):
        costs = [row['cost']['total_usd'] for row in group if row['cost']['total_usd'] is not None]
        return {'responses': len(group), 'tokens': _sum_usage(row['usage'] for row in group),
                'estimated_cost_usd': sum(costs) if costs else None, 'costed_responses': len(costs)}
    return ([{'model': model, **aggregate(group)} for model, group in model_rows.items()],
            [{'hour': hour, 'tools': value['tools'], 'hooks': value['hooks'], **aggregate(value['_rows'])}
             for hour, value in sorted(hourly.items())], list(tool_stats.values()))


def _imported_usage(conn, sid, capture):
    """Bounded explicit samples, separate from native and client session scope."""
    from alpaca.analytics import usage
    samples, sources, seen = [], [], {}
    incomplete = False
    query = ("SELECT id,length(CAST(data AS BLOB)) AS size FROM events "
             "WHERE session=? AND kind='usage-import' ORDER BY id LIMIT ?")
    records = conn.execute(query, (sid, detail.MAX_RECORDS + 1)) if conn is not None else []
    for record in records:
        size = record['size'] or 0
        if size > capture.budget.remaining or capture.budget.records >= detail.MAX_RECORDS:
            incomplete = True
            break
        capture.budget.records += 1
        if size > detail.MAX_LINE_BYTES:
            incomplete = True
            continue
        capture.budget.remaining -= size
        capture.stats['bytes_read'] += size
        try:
            raw = conn.execute('SELECT data FROM events WHERE id=?', (record['id'],)).fetchone()[0]
            data = json.loads(raw)
            if not isinstance(data.get('samples'), list):
                raise ValueError('invalid samples')
            for value in data['samples']:
                sample = usage._normalise(value, value.get('source'))
                ident = (sample['provider'], sample['sample_id'])
                if ident in seen:
                    incomplete |= usage._identity(seen[ident]) != usage._identity(sample)
                    continue
                seen[ident] = sample
                samples.append(sample)
                sources.append({'event_id': record['id'], 'kind': 'usage-import',
                                'sample_id': _safe(sample['sample_id']),
                                'sha256': sample['source']['sha256']})
        except (ValueError, TypeError, AttributeError, RecursionError):
            incomplete = True
    token_names = ('input', 'output', 'cache_read', 'cache_write_5m', 'cache_write_1h')
    tokens = {key: sum(item['tokens'][key] for item in samples)
                   if samples and all(item['tokens'][key] is not None for item in samples) else None
              for key in token_names}
    costs = [item.get('reported_cost_usd') for item in samples]
    cost_complete = bool(samples and not incomplete and all(cost is not None for cost in costs))
    return {'samples': len(samples), 'tokens': tokens, 'sources': sources[:MAX_LEDGER],
            'partial': incomplete, 'reported_cost_usd': sum(cost for cost in costs if cost is not None)
             if any(cost is not None for cost in costs) else None,
            'cost_complete': cost_complete,
            'usage_complete': bool(samples and not incomplete and all(value is not None for value in tokens.values())),
            'scope': 'explicit usage-import samples; overlap with transcript and child scope is unproven'}


def _imported_cost(conn, sid, capture):
    value = _imported_usage(conn, sid, capture)
    return value['reported_cost_usd'], value['sources'], not value['cost_complete'] if value['samples'] else value['partial']


def _scope_coverage(root, sid, conn, children):
    """Read declared source expectations; discovery alone never closes coverage."""
    missing, expected, closed = [], None, False
    manifest = {}
    if conn is not None:
        found = conn.execute('SELECT value FROM meta WHERE key=?', ('capture-manifest:' + sid,)).fetchone()
        try:
            manifest = json.loads(found[0]) if found else {}
        except (ValueError, TypeError):
            manifest = {}
    if not isinstance(manifest, dict):
        manifest = {}
    if isinstance(manifest.get('expected_children'), list):
        expected = manifest['expected_children']
        available = set(children) | {os.path.basename(str(path)).removeprefix('agent-').removesuffix('.jsonl')
                                    for path in children.values()}
        missing = [ident for ident in expected if ident not in available]
        closed = manifest.get('children_closed') is True
    result = {'session_complete': manifest.get('session_complete') is True,
              'child_coverage': {'state': 'missing' if missing else 'complete' if closed else 'unknown',
                                 'expected': len(expected) if expected is not None else None,
                                 'discovered': len(children), 'missing': missing},
              'capabilities': {'private_reasoning': False, 'discovery': 'registered or explicitly scoped sources',
                               **(manifest.get('capabilities') if isinstance(manifest.get('capabilities'), dict) else {})}}
    try:
        from alpaca.observability import coverage
        registered = coverage(root, sid=sid)
    except (ImportError, AttributeError):
        registered = None
    if isinstance(registered, dict) and registered.get('expected'):
        result['source_registry'] = {key: registered.get(key) for key in ('expected', 'registered', 'readable', 'missing_children', 'unknown_children', 'complete')}
        result['session_complete'] = registered.get('complete') is True
        result['provider_capabilities'] = [{'provider': _safe(row.get('provider')),
            'kind': _safe(row.get('kind')), 'capabilities': detail._redact(row.get('capabilities', {}))}
            for row in registered.get('sources', [])]
        result['child_coverage']['expected'] = sum(bool(row.get('parent_session')) for row in registered.get('sources', []) if row.get('required'))
        if 'missing_children' in registered:
            unknown = registered.get('unknown_children', True)
            missing = registered['missing_children']
            result['child_coverage'].update(missing=missing,
                state='missing' if missing else 'unknown' if unknown else 'complete')
    return result


def _analyze(root, sid, child, revision):
    conn = detail._connection(root)
    try:
        row = conn.execute('SELECT * FROM sessions WHERE sid=?', (sid,)).fetchone() if conn else None
        row = dict(row) if row else {}
        operator_row = conn.execute('SELECT value FROM meta WHERE key=?', ('operator:' + sid,)).fetchone() if conn else None
        operator = operator_row[0] if operator_row else None
        registered = transcripts.registered(conn, sid)
        if registered and not os.path.isabs(registered):
            registered = os.path.join(root, registered)
        local = transcripts.local_path(root, sid)
        pool_path = pool.path(root, sid)
        chosen, source = None, 'none'
        if registered and os.path.isfile(registered):
            chosen, source = registered, 'registered-transcript'
        elif detail._local_file(local, root):
            chosen, source = local, 'project-transcript'
        if not row and not chosen and not detail._local_file(pool_path, root):
            raise KeyError('unknown session')
        children, children_truncated = detail._children(root, sid, registered)
        if child is not None:
            if child not in children:
                raise ValueError('unknown child identifier')
            chosen = children[child]
            source = 'project-transcript' if detail._inside(chosen, root) else 'registered-transcript'
        budget = detail._Budget()
        budget.remaining = MAX_READ_BYTES
        capture = _AnalyticsCapture(sid + (':' + child if child else ''), source, budget)
        scan = _Scan(capture)
        if chosen:
            scan.read(chosen)
        has_transcript = bool(chosen and not capture.stats['unreadable_sources'])
        excluded_pool_calls = 0
        if not child and detail._local_file(pool_path, root):
            native_tools = set(capture.tools)
            native_messages = len(capture.messages)
            capture.pool(pool_path, sid)
            if has_transcript:
                # A parent-attributed pool can also contain worker activity or
                # uncaptured history. Only native parent IDs establish scope.
                excluded_pool_calls = len(set(capture.tools) - native_tools)
                capture.tools = {ident: tool for ident, tool in capture.tools.items() if ident in native_tools}
                del capture.messages[native_messages:]
            if not chosen:
                source = 'tool-pool'
        rows, method = scan.responses()
        imported_usage = _imported_usage(conn, sid, capture) if not child else _imported_usage(None, sid, capture)
        _finish_entries(rows)
        if rows and all(row['ts'] for row in rows):
            rows.sort(key=lambda row: row['ts'])
        coverage = detail._coverage(capture, has_transcript, False, source, children_truncated)
        coverage.update(_scope_coverage(root, sid, conn, children) if not child else {})
        coverage['session_complete'] = bool(coverage['session_complete'] and coverage['file_complete'])
        coverage.update(read_budget_bytes=MAX_READ_BYTES, line_budget_bytes=MAX_LINE_BYTES)
        coverage.update(usage_method=method, usage='measured' if any(row['usage'] for row in rows) else 'unavailable',
                        excluded_unlinked_pool_calls=excluded_pool_calls,
                        duplicate_response_records=scan.duplicate_responses,
                        conflicting_response_records=scan.conflicting_responses,
                        snapshot_resets=scan.snapshot_resets, snapshot_gaps=scan.snapshot_gaps,
                        native_snapshot_disagreement=scan.native_snapshot_disagreement,
                        synthetic_records_excluded=scan.synthetic_records_excluded,
                        token_semantics='Input includes cache reads/writes; reasoning is an output subset.',
                        response_unit='unique provider response; snapshot fallback uses distinct recorded last usage')
        if scan.conflicting_responses:
            coverage.update(state='partial', total_is_exact=False)
            coverage['reason'] += ' Conflicting native response records retain the first observation.'
        if scan.snapshot_resets or scan.snapshot_gaps:
            coverage.update(state='partial', total_is_exact=False)
            coverage['reason'] += ' Cumulative usage reset, missing prefix, or snapshot gaps make measured totals a lower bound.'
        if scan.native_snapshot_disagreement:
            coverage.update(state='partial', total_is_exact=False)
            coverage['reason'] += ' Native response records cover less usage than recorded cumulative snapshots; snapshots are not added to native totals.'
        if any(row['usage'] is None for row in rows):
            coverage['reason'] += ' Some responses have no usable token counters; token totals cover measured responses only.'
        coverage['usage_anomalies'] = sum(bool((item['usage'] or {}).get('anomalies')) for item in rows)
        if coverage['usage_anomalies']:
            coverage.update(state='partial', total_is_exact=False)
            coverage['reason'] += ' Inconsistent token categories are unavailable; valid counters remain visible.'
        coverage['usage_total_is_exact'] = bool(rows and coverage['total_is_exact'] and all(row['usage'] is not None and row['usage'].get('total_tokens') is not None for row in rows))
        models, hourly, tool_stats = _rollups(rows, capture, scan.hooks)
        measured = [row for row in rows if row['usage'] is not None]
        costs = [row['cost']['total_usd'] for row in rows if row['cost']['total_usd'] is not None]
        estimated = sum(costs) if costs else None
        tokens = _sum_usage(item['usage'] for item in rows)
        if not measured and imported_usage['samples']:
            raw = imported_usage['tokens']
            tokens = {key: None for key in TOKEN_KEYS}
            tokens.update(input_tokens=raw['input'], output_tokens=raw['output'],
                          cached_input_tokens=raw['cache_read'],
                          cache_creation_5m_tokens=raw['cache_write_5m'],
                          cache_creation_1h_tokens=raw['cache_write_1h'],
                          total_tokens=raw['input'] + raw['output'])
            coverage.update(usage_method='explicit-usage-samples', usage='measured',
                            usage_total_is_exact=imported_usage['usage_complete'])
            coverage['reason'] += ' Imported samples supply usage only; session overlap and completeness are unproven.'
        coverage['complete'] = bool(coverage['session_complete'] and coverage['usage_total_is_exact']
                                    and coverage['child_coverage']['state'] == 'complete')
        session = detail._session(capture, sid, row if not child else {}, operator, child)
        session.update(usage_available=bool(measured or imported_usage['samples']), tokens=tokens,
                       cost_reported_usd=scan.reported)
        kinds = OrderedDict()
        for hook in scan.hooks:
            kinds[hook['type']] = kinds.get(hook['type'], 0) + 1
        return {'schema': 1, 'revision': revision,
                'session': session,
                'coverage': coverage,
                'summary': {'responses': len(rows), 'usage_responses': len(measured),
                            'tokens': tokens,
                            'estimated_cost_usd': estimated, 'costed_responses': len(costs),
                            'average_cost_usd': estimated / len(costs) if costs else None,
                            'average_cost_basis': 'priced responses only',
                            'tool_calls': sum(tool['status'] != 'unmatched-result' for tool in capture.tools.values()), 'hook_events': len(scan.hooks)},
                'cost': {'estimated_usd': estimated, 'reported_usd': scan.reported,
                         'reported_kind': 'client-reported estimate' if scan.reported is not None else None,
                         'reported_scope': 'client session scope; child inclusion unspecified' if scan.reported is not None else None,
                         'reported_source': scan.reported_source, 'reported_models': scan.reported_models,
                         'imported_reported_usd': imported_usage['reported_cost_usd'], 'imported_sources': imported_usage['sources'],
                         'imported_partial': imported_usage['partial'] or (bool(imported_usage['samples']) and not imported_usage['cost_complete']),
                         'imported_scope': 'explicit usage-import samples; separate from transcript and client estimates',
                         'pricing_coverage': len(costs) / len(rows) if rows else None,
                         'estimated_scope': 'captured priced responses in the selected conversation; children excluded',
                         'basis': 'API list-price estimate; not provider billing or subscription cost'},
                'imported_usage': imported_usage,
                'context': _context(scan, rows), 'models': models, 'hourly': hourly, 'tools': tool_stats,
                'nested_actions': {'total': len(scan.actions), 'failed': sum(item['failed'] for item in scan.actions.values()),
                                   'items': list(scan.actions.values())[-MAX_HOOKS:],
                                   'items_truncated': len(scan.actions) > MAX_HOOKS,
                                   'basis': 'Unique captured action IDs; separate from outer tool-result error flags.'},
                'hooks': {'total': len(scan.hooks), 'executions_reported': sum(h['executions_reported'] or 0 for h in scan.hooks),
                          'by_type': [{'type': kind, 'count': count} for kind, count in kinds.items()],
                          'events': scan.hooks[-MAX_HOOKS:], 'events_truncated': len(scan.hooks) > MAX_HOOKS,
                          'basis': 'Hook records; injection/progress records are not execution counts.'},
                'ledger': rows[-MAX_LEDGER:], 'ledger_total': len(rows), 'ledger_truncated': len(rows) > MAX_LEDGER,
                'children': [{'id': ident, 'parent_sid': sid, 'title': 'Child conversation',
                              'scope': 'separate; open child for its usage'} for ident in children] if not child else []}
    finally:
        if conn:
            conn.close()


def analyze(root, sid, child=None):
    """Return selected-conversation analytics, cached by bounded source revision.

    Parent totals exclude all child conversations. Child IDs must have been
    discovered by detail's contained-directory scanner. Cache storage is bounded
    by both entries and serialized size; callers receive independent objects.
    """
    if not isinstance(sid, str) or not detail._IDENTIFIER.fullmatch(sid):
        raise ValueError('invalid session identifier')
    if child is not None and (not isinstance(child, str) or not re.fullmatch(r'child-[0-9a-f]{24}', child)):
        raise ValueError('invalid child identifier')
    root = os.path.realpath(root)
    revision = detail.source_revision(root, sid)
    key = (root, sid, child, json.dumps(revision, sort_keys=True), MAX_READ_BYTES, MAX_LINE_BYTES,
           detail.MAX_RECORDS, MAX_LEDGER, MAX_HOOKS)
    with _CACHE_LOCK:
        if key in _CACHE:
            _CACHE.move_to_end(key)
            return copy.deepcopy(_CACHE[key][0])
    result = _analyze(root, sid, child, revision)
    size = len(json.dumps(result, ensure_ascii=True))
    if size <= MAX_CACHE_BYTES:
        with _CACHE_LOCK:
            # Appends invalidate old revisions without retaining every version.
            for stale in [candidate for candidate in _CACHE if candidate[:3] == key[:3]]:
                del _CACHE[stale]
            _CACHE[key] = (copy.deepcopy(result), size)
            while len(_CACHE) > MAX_CACHE or sum(value[1] for value in _CACHE.values()) > MAX_CACHE_BYTES:
                _CACHE.popitem(last=False)
    return result
