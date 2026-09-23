"""Optional existing-tunnel supervision has no account or credential side effects.

t-089: one host-level tunnel unit named from the tunnel reads the combined config explicitly;
its watchdog and each instance's @reboot web-up script are generated, never installed. Every
test runs with a fake HOME and a tmp registry ($ALPACA_WORKSPACES).
"""
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess

import pytest

from alpaca import cli, workspace
from alpaca.observability.maintenance import watchdog_script, write_services


@pytest.fixture(autouse=True)
def host(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    (home / '.cloudflared').mkdir(parents=True)
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.setenv('ALPACA_CLOUDFLARED_CONFIG', str(home / '.cloudflared' / 'config.yml'))
    monkeypatch.setenv('ALPACA_WORKSPACES', str(tmp_path / 'registry' / 'workspaces.json'))
    return home


def tunnel_unit(result):
    name = next(name for name in result['units'] if name.endswith('.service') and name.startswith('alpaca-tunnel-')
                and not name.endswith('-watchdog.service'))
    return Path(result['directory'], name)


def exec_start(body):
    return shlex.split(next(line.split('=',1)[1] for line in body.splitlines() if line.startswith('ExecStart=')))


def test_default_services_do_not_add_a_tunnel(project):
    result = write_services(project)
    assert len(result['units']) == 4
    assert not any('tunnel' in name for name in result['units'])
    assert result['port'] != 7350 and result['installed'] is False


def test_web_up_script_starts_only_this_instances_units(project):
    result = write_services(project, port=7391)
    [script] = result['scripts']
    body = Path(script).read_text()
    prefix = 'alpaca-' + workspace.instance_id(project)
    assert Path(script).name == prefix + '-web-up.sh'
    assert Path(script).stat().st_mode & 0o777 == 0o700
    assert prefix + '-dashboard.service ' + prefix + '-collector.service' in body
    assert '--port 7391' in body and 'cloudflared' not in body and 'pgrep' not in body
    assert result['crontab'] == '@reboot ' + script
    checked = subprocess.run(['bash', '-n', script], capture_output=True, text=True, timeout=15)
    assert checked.returncode == 0, checked.stderr


def test_explicit_tunnel_is_one_host_unit_reading_the_combined_config(project, host):
    result = write_services(project, tunnel='alpaca', cloudflared='/usr/local/bin/cloudflared')
    path = tunnel_unit(result)
    unit = path.read_text()
    assert path.name == 'alpaca-tunnel-alpaca.service'
    assert exec_start(unit) == ['/usr/local/bin/cloudflared', '--config', str(host / '.cloudflared' / 'config.yml'),
                                'tunnel', 'run', 'alpaca']
    assert 'Restart=on-failure' in unit and 'RestartSec=5' in unit
    assert 'StandardError=journal' in unit
    assert 'token' not in unit.lower() and 'credentials' not in unit.lower()
    assert path.stat().st_mode & 0o777 == 0o600
    # host level: another instance renders the same unit, byte for byte
    other = write_services(str(Path(project).parent / 'different'), tunnel='alpaca')
    assert tunnel_unit(other).name == path.name and tunnel_unit(other).read_text() == unit
    custom = write_services(project, tunnel='alpaca', tunnel_config='/opt/cf/combined.yml')
    assert exec_start(tunnel_unit(custom).read_text())[1:3] == ['--config', '/opt/cf/combined.yml']


def test_watchdog_is_generated_from_the_registry_without_fixed_names(project, tmp_path, host):
    (host / '.cloudflared' / 'config.yml').write_text('tunnel: x\ncredentials-file: /c.json\n')
    none = write_services(project, tunnel='edge')
    assert 'names a hostname' in none['watchdog']
    other = tmp_path / 'other'
    (other / '.alpaca').mkdir(parents=True)
    (other / '.alpaca' / 'files-auth').write_text(':b')
    Path(project, '.alpaca', 'files-auth').write_text(':a')
    workspace.add(str(other), port=7395, hostname='b.example.org')
    workspace.add(project, port=7394, hostname='a.example.org')
    result = write_services(project, tunnel='edge', watchdog_state=str(tmp_path / 'state' / 'wd'))
    names = set(result['units'])
    assert {'alpaca-tunnel-edge-watchdog.service', 'alpaca-tunnel-edge-watchdog.timer'} <= names
    script = Path(result['watchdog']['script']).read_text()
    assert result['watchdog']['pairs'] == [['http://127.0.0.1:7394/', 'https://a.example.org/', False],
                                           ['http://127.0.0.1:7395/', 'https://b.example.org/', False]]
    assert 'UNIT=alpaca-tunnel-edge.service' in script and 'STATE=' + str(tmp_path / 'state' / 'wd') in script
    for fixed in ('7350', 'example.com', '0123456789ab'):
        assert fixed not in script
    folder = Path(result['directory'])
    service = (folder / 'alpaca-tunnel-edge-watchdog.service').read_text()
    assert exec_start(service) == [result['watchdog']['install_as'], str(tmp_path / 'home' / '.cloudflared' / 'config.yml')]
    assert 'Type=oneshot' in service
    timer = (folder / 'alpaca-tunnel-edge-watchdog.timer').read_text()
    assert 'OnUnitActiveSec=30' in timer and 'OnBootSec=90' in timer


def fake_bin(tmp_path):
    folder = tmp_path / 'bin'
    folder.mkdir()
    (folder / 'curl').write_text('#!/bin/bash\nfor url; do :; done\n'
                                 'case "$url" in http://127.0.0.1*) printf %s "$LOCAL_CODE";; *) printf %s "$PUBLIC_CODE";; esac\n')
    (folder / 'systemctl').write_text('#!/bin/bash\necho "$*" >> "$CALLS"\n')
    (folder / 'date').write_text('#!/bin/bash\necho "$NOW"\n')
    for item in folder.iterdir():
        item.chmod(0o700)
    return folder


def test_watchdog_logic_is_carried_unchanged(tmp_path):
    """Two misses while a local server answers restart the unit, at most once per 120 s;
    public 200/401/301/302 is healthy; a local server that does not answer is not the tunnel."""
    state, calls = tmp_path / 'st' / 'state', tmp_path / 'calls'
    script = tmp_path / 'wd.sh'
    config = tmp_path / 'live.yml'
    config.write_text('ingress:\n  - hostname: a.example.org\n    service: http://127.0.0.1:7391\n  - service: http_status:404\n')
    script.write_text(watchdog_script('alpaca-tunnel-x.service', [('http://127.0.0.1:7391/', 'https://a.example.org/', True)],
                                      str(state)))
    bindir = fake_bin(tmp_path)

    def tick(now, local='200', public='530'):
        env = dict(os.environ, PATH=str(bindir) + ':/usr/bin:/bin', NOW=str(now), LOCAL_CODE=local,
                   PUBLIC_CODE=public, CALLS=str(calls))
        subprocess.run(['bash', str(script), str(config)], env=env, capture_output=True, text=True, timeout=15, check=True)
        restarts = calls.read_text().count('restart') if calls.exists() else 0
        return state.read_text().split(), restarts

    assert tick(1000) == (['1', '0'], 0)
    assert tick(1030) == (['0', '1030'], 1)                  # second miss: restart
    assert tick(1060) == (['1', '1030'], 1)
    assert tick(1090) == (['2', '1030'], 1)                  # two misses, but only 60 s since
    assert tick(1160) == (['0', '1160'], 2)                  # 130 s since: restart
    assert tick(1190, local='000') == (['0', '1160'], 2)     # local down: not the tunnel's fault
    assert tick(1200, local='401') == (['1', '1160'], 2)     # 401 counts as a live local server
    for healthy in ('200', '401', '301', '302'):
        assert tick(1300, public=healthy) == (['0', '1160'], 2)
    assert calls.read_text().splitlines() == ['--user restart alpaca-tunnel-x.service'] * 2


@pytest.mark.parametrize('name', ['', '--token=secret', 'alpaca\nExecStart=/bin/sh', 'two names', 'a\r', 'a\x7f', 'x;$HOME', '$(id)'])
def test_unsafe_tunnel_name_is_rejected_before_writing(project, name):
    with pytest.raises(ValueError):
        write_services(project, tunnel=name)
    assert not Path(project, '.alpaca/services').exists()


@pytest.mark.parametrize('executable', ['cloudflared', '/usr/bin/cloudflared\nExecStart=/bin/sh', '/usr/bin/cloudflared\t',  '/usr/bin/cloudflared\x7f', '/tmp/cloud"flared', "/tmp/cloud'flared", '/tmp/cloud\\flared', ''])
def test_unsafe_executable_path_is_rejected_before_writing(project, executable):
    with pytest.raises(ValueError):
        write_services(project, tunnel='alpaca', cloudflared=executable)
    with pytest.raises(ValueError):
        write_services(project, tunnel='alpaca', tunnel_config=executable)
    assert not Path(project, '.alpaca/services').exists()


def test_custom_executable_requires_explicit_tunnel(project):
    with pytest.raises(ValueError):
        write_services(project, cloudflared='/usr/local/bin/cloudflared')
    with pytest.raises(ValueError):
        write_services(project, tunnel_config='/opt/cf.yml')
    assert not Path(project, '.alpaca/services').exists()


def test_collect_services_parses_tunnel_options(project, capsys):
    assert cli.main(['collect','services','--tunnel','alpaca','--cloudflared','/usr/local/bin/cloudflared',
                     '--tunnel-config','/opt/cf/combined.yml']) == cli.PASS
    result = json.loads(capsys.readouterr().out)
    assert tunnel_unit(result).is_file()
    assert cli.main(['collect','services','--tunnel=--token=secret']) == cli.USAGE


def test_systemd_validates_quoted_executable_without_expansion(project, tmp_path):
    if not shutil.which('systemd-analyze'):
        pytest.skip('systemd-analyze unavailable')
    executable = tmp_path / 'cloud flared%literal'
    executable.write_text('#!/bin/sh\nexit 0\n')
    executable.chmod(0o700)
    result = write_services(project, tunnel='alpaca', cloudflared=str(executable))
    checked = subprocess.run(['systemd-analyze','--user','verify',str(tunnel_unit(result))],
                             capture_output=True,text=True,timeout=15)
    assert checked.returncode == 0, checked.stderr
    assert str(tunnel_unit(result)) not in checked.stderr
