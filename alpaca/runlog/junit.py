"""Versioned normalization of an existing JUnit XML test report; never runs a test.

Null means unavailable or malformed, never zero. Each value retains the exact source digest,
parser version and native unit. These observations do not change any verdict or acceptance
threshold; a domain profile that wants more than status and duration names the extra per-test
properties it reads (`properties`).
"""
from __future__ import annotations

import math
from pathlib import Path
import xml.etree.ElementTree as ET

from alpaca.artifacts import digest

VERSION = 'alpaca-diagnostics/1'
MAX_BYTES = 16 * 1024 * 1024


def _source(path):
    path = Path(path)
    if not path.is_file():
        return {'path': str(path), 'sha256': None, 'bytes': None}, 'source unavailable'
    size = path.stat().st_size
    if size > MAX_BYTES:
        return {'path': str(path), 'sha256': None, 'bytes': size}, 'diagnostic exceeds parser budget'
    return {'path': str(path), 'sha256': digest(path), 'bytes': size}, None


def _number(value, *, nonnegative=False):
    try:
        if value is None or isinstance(value, bool):
            return None
        result = float(value)
        return result if math.isfinite(result) and (not nonnegative or result >= 0) else None
    except (TypeError, ValueError):
        return None


def _measurement(name, value, unit, source, subject=None, reason=None):
    return {'name': name, 'value': value, 'unit': unit, 'subject': subject,
            'source': source, 'parser': VERSION, 'availability': 'measured' if value is not None else 'unavailable',
            'reason': None if value is not None else (reason or 'field absent or malformed')}


def parse_junit(path, *, properties=None):
    """Tests and measurements of one JUnit XML file.

    `properties` maps a measurement name to (value property, unit property): for each test case
    that carries the value property under `<properties>`, one more nonnegative measurement is
    recorded with the unit the unit property names (null when absent).
    """
    source, issue = _source(path)
    out = {'parser': VERSION, 'source': source, 'status': 'unavailable', 'tests': [],
           'measurements': [], 'issues': [], 'coverage': None, 'coverage_reason': 'not emitted by JUnit'}
    try:
        if issue:
            raise ValueError(issue)
        text = Path(path).read_text()
        if '<!DOCTYPE' in text or '<!ENTITY' in text:
            raise ValueError('XML declarations outside JUnit contract')
        root = ET.fromstring(text)
        if root.tag not in ('testsuite', 'testsuites'):
            raise ValueError('unrecognized JUnit root')
        for case in root.iter('testcase'):
            status = ('failed' if case.find('failure') is not None else
                      'error' if case.find('error') is not None else
                      'skipped' if case.find('skipped') is not None else 'passed')
            props = {p.get('name'): p.get('value') for p in case.findall('./properties/property')}
            duration = _number(case.get('time'), nonnegative=True)
            subject = ':'.join(filter(None, [case.get('classname'), case.get('name')]))
            out['tests'].append({'name': case.get('name'), 'classname': case.get('classname'),
                                 'status': status, 'duration_s': duration, 'seed': props.get('random_seed'),
                                 'source': source, 'parser': VERSION})
            out['measurements'].append(_measurement('duration_s', duration, 's', source, subject))
            for name, (value_key, unit_key) in (properties or {}).items():
                if value_key in props:
                    out['measurements'].append(_measurement(name, _number(props[value_key], nonnegative=True),
                                                            props.get(unit_key), source, subject))
        out['status'] = 'available' if out['tests'] else 'unavailable'
        if not out['tests']:
            out['issues'].append('no individual test diagnostics emitted')
    except (OSError, ValueError, ET.ParseError) as exc:
        out['issues'].append(str(exc))
    return out
