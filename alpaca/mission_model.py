"""Validated capability definitions and bounded observations of project inputs."""
import fnmatch
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

PROPAGATES = {'depends_on', 'feeds', 'governs'}
KINDS = PROPAGATES | {'documents', 'verifies', 'contains'}
SKIP = {'.git', '.alpaca', '.venv', '__pycache__', '.pytest_cache', 'node_modules', 'dist'}
MAX_FILES, MAX_BYTES = 8000, 64 * 1024 * 1024


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()).hexdigest()


def text(value, label, limit=800):
    if not isinstance(value, str) or not value.strip() or len(value) > limit or any(ord(c) < 32 for c in value):
        raise ValueError('%s must be nonempty text of at most %s characters' % (label, limit))
    return value.strip()


def ident(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}', value):
        raise ValueError('IDs must be short ASCII identifiers')
    return value


def relative(value):
    value = text(value, 'path', 300)
    path = PurePosixPath(value)
    if path.is_absolute() or '..' in path.parts or '\\' in value or ':' in value or str(path) in ('.', ''):
        raise ValueError('paths must stay relative to the project')
    if any(part in SKIP for part in path.parts):
        raise ValueError('runtime and tool directories are excluded from mission inputs')
    return value


def strings(value, label, clean=text, limit=80):
    if not isinstance(value, list) or len(value) > limit:
        raise ValueError('%s must be a bounded list' % label)
    result = [clean(x) if clean != text else text(x, label) for x in value]
    if len(set(result)) != len(result):
        raise ValueError('%s contains duplicates' % label)
    return result


def validate(data):
    if not isinstance(data, dict) or data.get('version') != 1:
        raise ValueError('mission definition version must be 1')
    if len(json.dumps(data)) > 512 * 1024:
        raise ValueError('mission definition exceeds 512 KiB')
    groups, nodes, edges = data.get('groups'), data.get('nodes'), data.get('edges', [])
    if not isinstance(groups, list) or not 1 <= len(groups) <= 80:
        raise ValueError('mission map needs 1 to 80 groups')
    if not isinstance(nodes, list) or not 1 <= len(nodes) <= 500:
        raise ValueError('mission map needs 1 to 500 capabilities')
    if not isinstance(edges, list) or len(edges) > 2000:
        raise ValueError('mission map allows at most 2000 relationships')
    result = {'version': 1, 'title': text(data.get('title'), 'title', 120),
              'purpose': text(data.get('purpose'), 'purpose'),
              'inventory': strings(data.get('inventory', []), 'inventory', relative),
              'groups': [], 'nodes': [], 'edges': []}
    for group in groups:
        if not isinstance(group, dict): raise ValueError('group must be an object')
        result['groups'].append({'id': ident(group.get('id')), 'title': text(group.get('title'), 'group title', 120)})
    group_ids = {g['id'] for g in result['groups']}
    if len(group_ids) != len(groups): raise ValueError('duplicate group ID')
    for node in nodes:
        if not isinstance(node, dict): raise ValueError('node must be an object')
        if node.get('group') not in group_ids: raise ValueError('unknown node group')
        lifecycle = node.get('lifecycle', 'current')
        if lifecycle not in ('current', 'planned', 'retired'): raise ValueError('unknown lifecycle')
        result['nodes'].append({'id': ident(node.get('id')), 'group': node['group'],
                                'title': text(node.get('title'), 'node title', 120),
                                'purpose': text(node.get('purpose'), 'node purpose'),
                                'sources': strings(node.get('sources', []), 'sources', relative),
                                'checks': strings(node.get('checks', []), 'checks', ident),
                                'tags': strings(node.get('tags', []), 'tags', ident), 'lifecycle': lifecycle})
    node_ids = {n['id'] for n in result['nodes']}
    if len(node_ids) != len(nodes): raise ValueError('duplicate node ID')
    seen = set()
    for edge in edges:
        if not isinstance(edge, dict): raise ValueError('edge must be an object')
        if edge.get('source') not in node_ids or edge.get('target') not in node_ids:
            raise ValueError('relationship names an unknown node')
        if edge.get('kind') not in KINDS: raise ValueError('unknown relationship type')
        if edge.get('confidence') not in ('confirmed', 'inferred'): raise ValueError('relationship needs confidence')
        key = (edge['source'], edge['target'], edge['kind'])
        if key in seen: raise ValueError('duplicate relationship')
        seen.add(key)
        evidence = strings(edge.get('evidence', []), 'edge evidence', relative)
        if edge['confidence'] == 'confirmed' and not evidence: raise ValueError('confirmed relationships need evidence')
        result['edges'].append({'source': edge['source'], 'target': edge['target'], 'kind': edge['kind'],
                                'confidence': edge['confidence'], 'reason': text(edge.get('reason'), 'relationship reason'),
                                'evidence': evidence})
    return result


def upstream(definition, node_id):
    reached = {node_id}
    edges = [e for e in definition['edges'] if e['kind'] in PROPAGATES and e['confidence'] == 'confirmed']
    pending = [node_id]
    while pending:
        target = pending.pop()
        for edge in edges:
            if edge['target'] == target and edge['source'] not in reached:
                reached.add(edge['source']); pending.append(edge['source'])
    return reached


def safe_path(root, name):
    """No symlink traversal, including symlinks whose target happens to remain inside root."""
    root = Path(root).resolve()
    path = root / name
    try:
        path.relative_to(root)
        if not path.resolve().is_relative_to(root): return None
        cursor = path
        while cursor != root:
            if cursor.is_symlink(): return None
            cursor = cursor.parent
    except (ValueError, OSError):
        return None
    return path


def observe(root, definition):
    root = Path(root).resolve()
    files, errors, excluded = {}, [], []
    size = 0
    candidates = set()
    for name in definition['inventory']:
        if any(c in name for c in '*?['):
            errors.append('Inventory roots cannot contain wildcards: ' + name); continue
        base = safe_path(root, name)
        if base is None:
            errors.append('Unsafe inventory root: ' + name); continue
        if base.is_file(): candidates.add(name)
        elif base.is_dir():
            for current, dirs, names in os.walk(base, followlinks=False, onerror=lambda exc: errors.append(str(exc))):
                allowed = []
                for part in dirs:
                    p = Path(current) / part
                    if p.is_symlink(): excluded.append(p.relative_to(root).as_posix())
                    elif part not in SKIP: allowed.append(part)
                dirs[:] = sorted(allowed)
                for part in names:
                    if part.endswith(('.pyc', '.pyo')): continue
                    candidates.add((Path(current) / part).relative_to(root).as_posix())
                if len(candidates) > MAX_FILES:
                    dirs[:] = []; errors.append('Inventory exceeds %d files' % MAX_FILES); break
    for name in sorted(candidates)[:MAX_FILES]:
        path = safe_path(root, name)
        if path is None:
            files[name] = {'error': 'Symlink or path escape'}; continue
        try:
            before = path.stat()
            if not path.is_file(): continue
            if before.st_size > 8 * 1024 * 1024 or size + before.st_size > MAX_BYTES:
                files[name] = {'error': 'Input byte limit exceeded'}; continue
            content = path.read_bytes(); size += len(content)
            after = path.stat()
            if (before.st_mtime_ns, before.st_ctime_ns, before.st_size) != (after.st_mtime_ns, after.st_ctime_ns, after.st_size):
                files[name] = {'error': 'Input changed during observation'}; continue
            files[name] = {'sha256': hashlib.sha256(content).hexdigest(),
                           'modified': datetime.fromtimestamp(after.st_mtime, timezone.utc).isoformat()}
        except OSError as exc:
            files[name] = {'error': str(exc)}
    own, mapped = {}, set()
    for node in definition['nodes']:
        matches = {p: d for p, d in files.items() if any(fnmatch.fnmatchcase(p, pattern) for pattern in node['sources'])}
        mapped.update(matches)
        problems = [p + ': ' + d['error'] for p, d in matches.items() if 'error' in d]
        for pattern in node['sources']:
            if not any(c in pattern for c in '*?[') and pattern not in matches:
                problems.append('Missing declared input: ' + pattern)
        for path in excluded:
            prefixes = [re.split(r'[*?\[]', pattern, maxsplit=1)[0] for pattern in node['sources']]
            if any(prefix.startswith(path + '/') or (path + '/').startswith(prefix) for prefix in prefixes):
                problems.append('Excluded symlink: ' + path)
        if not matches: problems.append('No matching source files')
        own[node['id']] = {'files': sorted(matches), 'errors': problems,
                           'modified': max((d.get('modified', '') for d in matches.values()), default='') or None,
                           'digest': digest({'definition': node, 'files': {p: d.get('sha256', d.get('error')) for p, d in matches.items()}})}
    observed = {}
    for node in definition['nodes']:
        sources = upstream(definition, node['id'])
        edges = [e for e in definition['edges'] if e['target'] in sources and e['source'] in sources
                 and e['kind'] in PROPAGATES and e['confidence'] == 'confirmed']
        observed[node['id']] = {**own[node['id']], 'own_digest': own[node['id']]['digest'],
                               'digest': digest({'inputs': {i: own[i]['digest'] for i in sorted(sources)}, 'edges': edges}),
                               'errors': errors + [i + ': ' + p for i in sorted(sources) for p in own[i]['errors']],
                               'upstream': sorted(sources - {node['id']})}
    return {'nodes': observed, 'files': {p: d.get('sha256', d.get('error')) for p, d in files.items()},
            'coverage': {'total': len(files), 'mapped': len(mapped), 'unmapped': sorted(set(files) - mapped),
                         'excluded': excluded, 'complete': not errors and not excluded and not any('error' in d for d in files.values()),
                         'scope': definition['inventory']}, 'errors': errors}


def starter():
    return validate({'version': 1, 'title': 'Project mission map', 'purpose': 'Connect project capabilities, work and evidence.',
                     'inventory': ['src', 'docs', 'tests'], 'groups': [{'id': 'project', 'title': 'Project'}],
                     'nodes': [{'id': 'project', 'group': 'project', 'title': 'Project capabilities',
                                'purpose': 'Define the capabilities and dependencies that matter to this project.',
                                'sources': ['src/**', 'docs/**', 'tests/**'], 'checks': []}], 'edges': []})
