"""Synthetic subprocess diagnostics, never native acceptance evidence."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time

import pytest

from alpaca.hooks import codex_probe as probe

DRIVER = '01a1078a-703e-7201-b5b8-7ae0005069dd'
SCRIPT = str(Path(probe.__file__).absolute())


def _run(*args, data=b''):
    return subprocess.run([sys.executable, '-I', '-B', SCRIPT, *args], input=data,
                          capture_output=True, timeout=8)


@pytest.fixture
def staged(tmp_path):
    parent = tmp_path / '.alpaca'
    parent.mkdir()
    output = parent / "prepared 'quoted space"
    got = _run('prepare', '--output-dir', str(output), '--driver-session', DRIVER,
               '--diagnostic-opt-in')
    assert got.returncode == 0 and got.stdout == b'', got.stderr
    return output


def _capture(staged, data, *, sink=None, opt_in=True, driver=DRIVER):
    args = ['capture', '--sink', str(sink or staged / 'capture'), '--definition-id',
            'diagnostic-PreToolUse', '--driver-session', driver]
    if opt_in:
        args.append('--diagnostic-opt-in')
    return _run(*args, data=data)


def _receipts(staged):
    return sorted((staged / 'capture').glob('receipt-*'))


def _meta(receipt):
    return json.loads((receipt / 'metadata.json').read_bytes())


def test_capture_preserves_exact_bytes_and_local_provenance(staged):
    samples = [b' { "session_id": "' + DRIVER.encode() + b'", "hook_event_name": "PreToolUse", '
               b'"tool_name":"Bash","tool_input":{"command":"true"}} \n',
               ('\n{ "session_id": "%s", "hook_event_name": "FutureEvent", '
                '"unknown": "caf\u00e9 cr\u00e8me \u65e5\u672c\u8a9e" }\t' % DRIVER).encode()]
    for raw in samples:
        got = _capture(staged, raw)
        assert got.returncode == 0 and got.stdout == got.stderr == b''
    assert {(p / 'payload.bin').read_bytes() for p in _receipts(staged)} == set(samples)
    for p in _receipts(staged):
        m = _meta(p)
        raw = (p / 'payload.bin').read_bytes()
        assert m['byte_length'] == len(raw) and m['raw_sha256'] == hashlib.sha256(raw).hexdigest()
        assert m['collector_sha256'] == hashlib.sha256(Path(SCRIPT).read_bytes()).hexdigest()
        assert m['parsing_completed'] and m['json_object'] and m['error'] is None
        assert m['runtime_identity_verified'] is False and m['runtime_trust'] == 'unknown'
        assert m['local_capture_time'] and m['definition_id'] == 'diagnostic-PreToolUse'
        assert p.stat().st_mode & 0o777 == 0o700
        assert (p / 'payload.bin').stat().st_mode & 0o777 == 0o600


def test_capture_invalid_and_oversize_are_never_valid_events(staged):
    for raw in (b'{', b'{"session_id":', b'[]', b'null', b'\xff', b'x' * (probe.MAX_BYTES + 10)):
        got = _capture(staged, raw)
        assert got.returncode == 1 and got.stdout == b''
        assert got.stderr == b'codex diagnostic: capture incomplete\n'
    for p in _receipts(staged):
        m = _meta(p)
        assert not m['valid_diagnostic_payload'] and m['raw_omitted']
        assert not (p / 'payload.bin').exists() and 'raw_sha256' not in m
        assert m['observed_prefix_length'] <= probe.MAX_BYTES + 1
        assert (p / 'metadata.json').stat().st_size < 2048
    assert any(_meta(p)['truncated'] for p in _receipts(staged))
    args = [sys.executable, '-I', '-B', SCRIPT, 'capture', '--sink', str(staged / 'capture'),
            '--definition-id', 'diagnostic-PreToolUse', '--driver-session', DRIVER,
            '--diagnostic-opt-in']
    start = time.monotonic()
    process = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE)
    process.stdin.write(b'{')
    process.stdin.flush()
    # the probe gives stdin READ_SECONDS (3 s) after its start; with no read deadline it waits for
    # stdin forever, so 20 s leaves a loaded host's interpreter start plenty of room
    try:
        assert process.wait(timeout=20) == 1
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
    assert process.stdout.read() == b''
    process.stdin.close()
    assert time.monotonic() - start < 20
    assert any(_meta(p)['error'] == 'stdin-timeout' for p in _receipts(staged))


@pytest.mark.parametrize('token', ['NaN', 'Infinity', '-Infinity'])
@pytest.mark.parametrize('nested', [False, True])
def test_capture_rejects_nonstandard_json_constants(staged, token, nested):
    value = '[{"value": %s}]' % token if nested else token
    raw = ('{"session_id":"%s","value":%s}' % (DRIVER, value)).encode()
    got = _capture(staged, raw)
    assert got.returncode == 1 and got.stdout == b''
    assert got.stderr == b'codex diagnostic: capture incomplete\n'
    receipt, = _receipts(staged)
    meta = _meta(receipt)
    assert meta['error'] == 'invalid-json'
    assert not meta['parsing_completed'] and not meta['valid_diagnostic_payload']
    assert meta['raw_omitted'] and 'raw_sha256' not in meta
    assert not (receipt / 'payload.bin').exists()


def test_capture_accepts_quoted_constants_and_json_numbers(staged):
    raw = ('{"session_id":"%s","values":["NaN","Infinity","-Infinity",'
           '0,-1,2.5,1e400]}' % DRIVER).encode()
    got = _capture(staged, raw)
    assert got.returncode == 0 and got.stdout == got.stderr == b''
    receipt, = _receipts(staged)
    assert (receipt / 'payload.bin').read_bytes() == raw
    assert _meta(receipt)['valid_diagnostic_payload']


@pytest.mark.parametrize('token', ['NaN', 'Infinity', '-Infinity'])
def test_capture_sink_marker_rejects_nonstandard_json_constants(staged, token):
    marker = staged / 'capture' / 'sink.json'
    marker.write_text('{"purpose":"diagnostic","driver_session":"%s",'
                      '"extra":[{"value":%s}]}' % (DRIVER, token))
    got = _capture(staged, json.dumps({'session_id': DRIVER}).encode())
    assert got.returncode == 1 and got.stdout == b''
    assert got.stderr == b'codex diagnostic: capture/preparation failed\n'
    assert _receipts(staged) == []
    with pytest.raises(ValueError, match='nonstandard JSON constant'):
        probe._strict_json(marker.read_bytes())


def test_capture_concurrency_no_clobber_or_escape(staged, tmp_path):
    sink = staged / 'capture'
    preserved = sink / 'existing.bin'
    preserved.write_bytes(b'keep')
    payloads = [json.dumps({'session_id': DRIVER, 'index': n, 'filename': '../../escape'}).encode()
                for n in range(12)]
    with ThreadPoolExecutor(max_workers=6) as pool:
        got = list(pool.map(lambda raw: _capture(staged, raw), payloads))
    assert all(g.returncode == 0 and g.stdout == b'' for g in got)
    assert len(_receipts(staged)) == 12
    assert {(r / 'payload.bin').read_bytes() for r in _receipts(staged)} == set(payloads)
    assert preserved.read_bytes() == b'keep' and not (tmp_path / 'escape').exists()
    outside = tmp_path / 'outside'
    outside.mkdir()
    link = staged / 'linked'
    link.symlink_to(outside, target_is_directory=True)
    got = _capture(staged, payloads[0], sink=link)
    assert got.returncode == 1 and got.stdout == b'' and list(outside.iterdir()) == []
    got = _capture(staged, payloads[0], sink=preserved)
    assert got.returncode == 1 and preserved.read_bytes() == b'keep'
    # The sink marker must be a regular private file, not a symlink or FIFO.
    (sink / 'sink.json').unlink()
    (sink / 'sink.json').symlink_to(outside / 'missing')
    assert _capture(staged, payloads[0]).returncode == 1
    (sink / 'sink.json').unlink()
    os.mkfifo(sink / 'sink.json')
    assert _capture(staged, payloads[0]).returncode == 1


def test_prepare_is_staged_passive_and_reproducible(staged, tmp_path):
    config_raw = (staged / 'hooks.json').read_bytes()
    config = json.loads(config_raw)
    manifest = json.loads((staged / 'manifest.json').read_bytes())
    assert set(config['hooks']) == set(probe.EVENTS)
    assert manifest['purpose'] == 'diagnostic' and manifest['passive'] is True
    assert manifest['installed'] is False and manifest['trusted'] == 'unknown'
    assert manifest['driver_session'] == DRIVER
    assert manifest['config_sha256'] == hashlib.sha256(config_raw).hexdigest()
    assert manifest['script_sha256'] == hashlib.sha256(Path(SCRIPT).read_bytes()).hexdigest()
    assert manifest['interpreter_sha256'] == hashlib.sha256(Path(sys.executable).read_bytes()).hexdigest()
    for event, groups in config['hooks'].items():
        assert len(groups) == 1 and set(groups[0]) == {'hooks'}
        hook = groups[0]['hooks'][0]
        assert hook['type'] == 'command' and hook['timeout'] == 5
        argv = shlex.split(hook['command'])
        assert argv == manifest['definitions']['diagnostic-' + event]['argv']
        assert argv[0] == sys.executable and argv[1:3] == ['-I', '-B'] and argv[3] == SCRIPT
        assert '--diagnostic-opt-in' in argv and DRIVER in argv
        # Correct shell quoting for spaces and apostrophes, independent of working directory.
        got = subprocess.run(argv, cwd=tmp_path, input=json.dumps({'session_id': DRIVER}).encode(),
                             capture_output=True, timeout=8)
        assert got.returncode == 0 and got.stdout == b''
    assert not (tmp_path / '.codex').exists() and not (tmp_path / '.alpaca' / 'alpaca.db').exists()
    before = config_raw
    got = _run('prepare', '--output-dir', str(staged), '--driver-session', DRIVER,
               '--diagnostic-opt-in')
    assert got.returncode == 1 and got.stdout == b''
    assert (staged / 'hooks.json').read_bytes() == before
    other = staged.parent / 'second'
    assert _run('prepare', '--output-dir', str(other), '--driver-session', DRIVER,
                '--diagnostic-opt-in').returncode == 0
    second = json.loads((other / 'hooks.json').read_bytes())
    for event in probe.EVENTS:
        a = shlex.split(config['hooks'][event][0]['hooks'][0]['command'])
        b = shlex.split(second['hooks'][event][0]['hooks'][0]['command'])
        a[a.index('--sink') + 1] = b[b.index('--sink') + 1]
        assert a == b  # deterministic except explicitly named output directory
    got = _run('prepare', '--output-dir', 'relative-active', '--driver-session', DRIVER,
               '--diagnostic-opt-in')
    assert got.returncode == 1 and not (tmp_path / 'active').exists()


def test_collector_never_emits_guard_or_permission_output(staged):
    raw = json.dumps({'session_id': DRIVER, 'permissionDecision': 'allow', 'ready': True}).encode()
    for value, opt in ((raw, False), (b'{}', True),
                       (json.dumps({'session_id': 'foreign'}).encode(), True)):
        got = _capture(staged, value, opt_in=opt)
        assert got.returncode == 0 and got.stdout == got.stderr == b''
    assert _receipts(staged) == []
    got = _capture(staged, b'{"session_id": {"parent": "anything"}}')
    assert got.returncode == 1 and got.stdout == b''
    m = _meta(_receipts(staged)[0])
    assert m['error'] == 'unsupported-session-identity' and m['raw_omitted']
    got = _capture(staged, raw)
    assert got.returncode == 0 and got.stdout == got.stderr == b''
    assert not any(p.name.endswith('.jsonl') for p in (staged / 'capture').iterdir())
