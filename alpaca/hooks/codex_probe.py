"""Passive, explicitly scoped Codex diagnostic. No policy or research evidence is emitted."""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import select
import shlex
import stat
import sys
import time
import uuid

MAX_BYTES = 1024 * 1024
READ_SECONDS = 3.0
EVENTS = ('PreToolUse', 'PostToolUse', 'SubagentStart', 'SubagentStop')


def _hash(data):
    return hashlib.sha256(data).hexdigest()


def _directory(path):
    """Open each absolute component without following symlinks; return a held directory fd."""
    p = Path(path)
    if not p.is_absolute() or '.alpaca' not in p.parts or '..' in p.parts:
        raise ValueError('staging path required')
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in p.parts[1:]:
            nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = nxt
        return fd
    except BaseException:
        os.close(fd)
        raise


def _write(fd, name, data):
    target = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600,
                     dir_fd=fd)
    with os.fdopen(target, 'wb') as stream:
        stream.write(data)


def _json(data):
    return (json.dumps(data, indent=2, sort_keys=True) + '\n').encode()


def _reject_constant(token):
    raise ValueError('nonstandard JSON constant')


def _strict_json(data):
    return json.loads(data, parse_constant=_reject_constant)


def _identity(driver, label=None):
    if str(uuid.UUID(driver)) != driver:
        raise ValueError('canonical driver UUID required')
    if label is not None and not re.fullmatch(r'[A-Za-z0-9-]{1,80}', label):
        raise ValueError('definition label invalid')


def prepare(output_dir, driver_session, diagnostic_opt_in=False):
    if not diagnostic_opt_in:
        raise ValueError('diagnostic opt-in required')
    _identity(driver_session)
    output = Path(output_dir)
    parent = _directory(str(output.parent))
    try:
        os.mkdir(output.name, 0o700, dir_fd=parent)  # refuses every existing target
    finally:
        os.close(parent)
    fd = _directory(str(output))
    try:
        os.mkdir('capture', 0o700, dir_fd=fd)
        sink = output / 'capture'
        sfd = _directory(str(sink))
        try:
            _write(sfd, 'sink.json', _json({'purpose': 'diagnostic', 'driver_session': driver_session}))
        finally:
            os.close(sfd)
        script = str(Path(__file__).absolute())
        interpreter = str(Path(sys.executable).absolute())
        config = {'description': 'Passive single-driver diagnostic; no research guard', 'hooks': {}}
        definitions = {}
        for event in EVENTS:
            label = 'diagnostic-' + event
            argv = [interpreter, '-I', '-B', script, 'capture', '--sink', str(sink),
                    '--definition-id', label, '--driver-session', driver_session,
                    '--diagnostic-opt-in']
            config['hooks'][event] = [{'hooks': [{'type': 'command', 'command': shlex.join(argv),
                                                 'timeout': 5}]}]
            definitions[label] = {'event': event, 'argv': argv}
        rendered = _json(config)
        manifest = {'format_version': 1, 'purpose': 'diagnostic', 'passive': True,
                    'installed': False, 'trusted': 'unknown', 'driver_session': driver_session,
                    'sink': str(sink), 'definitions': definitions, 'script': script,
                    'script_sha256': _hash(Path(script).read_bytes()), 'interpreter': interpreter,
                    'interpreter_sha256': _hash(Path(interpreter).read_bytes()),
                    'config': str(output / 'hooks.json'), 'config_sha256': _hash(rendered)}
        _write(fd, 'hooks.json', rendered)
        _write(fd, 'manifest.json', _json(manifest))
        _write(fd, 'README.md', ('# Passive diagnostic candidate\n\n' +
            json.dumps(manifest, indent=2, sort_keys=True) + '\n\n' +
            'Not installed. Trust unknown. Manual calls are synthetic.\n'
            'No stdout policy, replacement input, permission decision or research evidence.\n'
            '1 MiB and 3 second stdin bounds. Receipts are private per-invocation folders.\n'
            'Invalid/unroutable input omits raw bytes. Exit 1 is capture failure, not tool denial.\n'
            'Review/loading route in CLI: /hooks (https://learn.chatgpt.com/docs/hooks).\n'
            'Activation in this desktop driver remains unverified; do not infer reload.\n').encode())
        return manifest
    finally:
        os.close(fd)


def _read_input():
    data = bytearray()
    deadline = time.monotonic() + READ_SECONDS
    while len(data) <= MAX_BYTES:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not select.select([sys.stdin.fileno()], [], [], remaining)[0]:
            return bytes(data), 'stdin-timeout'
        chunk = os.read(sys.stdin.fileno(), min(65536, MAX_BYTES + 1 - len(data)))
        if not chunk:
            return bytes(data), None
        data.extend(chunk)
    return bytes(data), 'oversize'


def capture(sink, definition_id, driver_session, diagnostic_opt_in=False):
    if not diagnostic_opt_in:
        return 0
    _identity(driver_session, definition_id)
    fd = _directory(sink)
    try:
        mode = os.fstat(fd)
        if mode.st_uid != os.getuid() or stat.S_IMODE(mode.st_mode) != 0o700:
            raise ValueError('private sink required')
        marker_fd = os.open('sink.json', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        with os.fdopen(marker_fd, 'rb') as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError('regular sink marker required')
            marker = _strict_json(stream.read(4097))
        if marker != {'purpose': 'diagnostic', 'driver_session': driver_session}:
            raise ValueError('sink driver mismatch')
        raw, error = _read_input()
        payload = None
        parsed = False
        if error is None:
            try:
                payload = _strict_json(raw)
                parsed = True
            except (ValueError, UnicodeError, RecursionError):
                error = 'invalid-json'
        is_object = isinstance(payload, dict)
        if parsed and not is_object:
            error = 'nonobject-json'
        if is_object:
            sid = payload.get('session_id')
            if sid is None or isinstance(sid, str) and sid != driver_session:
                return 0
            if not isinstance(sid, str):
                error = 'unsupported-session-identity'
        retain = is_object and error is None and payload.get('session_id') == driver_session
        meta = {'format_version': 1, 'purpose': 'diagnostic', 'passive': True,
                'definition_id': definition_id, 'local_capture_time':
                datetime.datetime.now(datetime.timezone.utc).isoformat(),
                'collector_sha256': _hash(Path(__file__).read_bytes()),
                'parsing_completed': parsed, 'json_object': is_object,
                'error': error, 'truncated': error == 'oversize', 'raw_omitted': not retain,
                'valid_diagnostic_payload': retain, 'runtime_identity_verified': False,
                'runtime_trust': 'unknown', 'observed_prefix_length': len(raw)}
        if retain:
            meta.update({'byte_length': len(raw), 'raw_sha256': _hash(raw)})
        name = 'receipt-' + uuid.uuid4().hex
        os.mkdir(name, 0o700, dir_fd=fd)
        receipt_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
        try:
            if retain:
                _write(receipt_fd, 'payload.bin', raw)
            _write(receipt_fd, 'metadata.json', _json(meta))
        finally:
            os.close(receipt_fd)
        return 1 if error else 0
    finally:
        os.close(fd)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='verb', required=True)
    for verb in ('prepare', 'capture'):
        p = sub.add_parser(verb)
        p.add_argument('--driver-session', required=True)
        p.add_argument('--diagnostic-opt-in', action='store_true')
        if verb == 'prepare':
            p.add_argument('--output-dir', required=True)
        else:
            p.add_argument('--sink', required=True)
            p.add_argument('--definition-id', required=True)
    args = vars(parser.parse_args())
    verb = args.pop('verb')
    try:
        result = prepare(**args) if verb == 'prepare' else capture(**args)
        if verb == 'capture' and result:
            print('codex diagnostic: capture incomplete', file=sys.stderr)
        return result if verb == 'capture' else 0
    except (OSError, ValueError, RecursionError):
        print('codex diagnostic: capture/preparation failed', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
