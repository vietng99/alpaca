"""Local backup scheduling with a no-delete storage budget."""
import hashlib
import os
import re
import unicodedata
from pathlib import Path


def backup_once(root, *, budget_bytes=20*1024**3):
    from alpaca import backup
    folder=Path(root)/'.alpaca/backups'
    used=sum(p.stat().st_size for p in folder.rglob('*') if p.is_file() and not p.is_symlink()) if folder.exists() else 0
    # Conservative reserve for one full copy plus staging. Failure is visible, never a purge.
    runtime=Path(root)/'.alpaca'
    bases=['alpaca.db','wiki','transcripts','pool','artifacts','proofs']
    # a domain profile's backup directories count too (alpaca/profile.py `paths` "backup")
    from alpaca import profile
    extra=[Path(root)/p.strip('/') for p in profile.listed(profile.load(str(root)).paths(), 'backup')]
    reserve=0
    for path in [runtime/name for name in bases]+extra:
        if path.is_file(): reserve+=path.stat().st_size
        elif path.is_dir(): reserve+=sum(p.stat().st_size for p in path.rglob('*') if p.is_file() and not p.is_symlink())
    if used+2*reserve > budget_bytes:
        result={'ok':False,'issues':['local backup budget exhausted; inspect retention dry-run and choose storage'],
                'budget_bytes':budget_bytes,'used_bytes':used,'reserve_bytes':2*reserve}
        backup._record_verification(root,None,result)
        return result
    manifest=backup.create(root)
    return {'ok':True,'path':manifest['path'],'watermark':manifest['watermark'],'completeness':manifest['completeness']}


def _safe_path(label, value):
    value = str(value)
    if (not os.path.isabs(value)
            or any(unicodedata.category(char).startswith("C") for char in value)
            or any(char in value for char in ('"', "'", "\\", "$"))):
        # systemd $-expands ExecStart arguments (a `$$` escape would break the executable, which
        # it does not expand), so a `$` is refused outright
        raise ValueError(label+" must be an absolute path without control characters, quotes, backslashes or $")
    return value


def tunnel_unit_name(tunnel):
    """The host-level unit that runs the one named tunnel (one per host, never per instance)."""
    return 'alpaca-tunnel-'+tunnel+'.service'


def watchdog_pairs(entries, live=None):
    """(local url, public url, routed) for every hostname the tunnel serves or will serve: each
    live cloudflared rule with a hostname and a loopback service (routed, so example.com is always
    checked), then each registered hostname the live config does not route yet (not routed)."""
    from alpaca import workspace
    pairs, seen = [], set()
    for rule in workspace._live_rules(live or {}):
        host, port = workspace._host_of(rule), workspace._local_port(rule.get('service'))
        if host and port and host not in seen and workspace.HOST_RE.fullmatch(host):
            seen.add(host)
            pairs.append(('http://127.0.0.1:%d/' % port, 'https://%s/' % host, True))
    for e in sorted((e for e in entries if e.get('hostname')), key=lambda e: e['hostname']):
        if e['hostname'] not in seen:
            seen.add(e['hostname'])
            pairs.append(('http://127.0.0.1:%d/' % int(e['port']), 'https://%s/' % e['hostname'], False))
    return pairs


WATCHDOG_BODY = r"""set -u
set -f   # no pathname expansion: a wildcard hostname read from the config stays text
CONFIG=${1:-}
case "$CONFIG" in /*) ;; *) echo "usage: $0 /absolute/path/to/cloudflared/config.yml" >&2; exit 2;; esac
# The hostnames the live config routes, read at run time with one fixed pattern (never a
# pattern built from data). An unreadable config falls back to the routed set of generation time.
if [ ! -r "$CONFIG" ]; then
  echo "WARNING: cannot read $CONFIG; using the routed hostnames of generation time" >&2
  ROUTED=$GENERATED_ROUTED
else
ROUTED=$(sed -n -E 's/^[[:space:]]*(-[[:space:]]+)?hostname:[[:space:]]*["'\'']?([A-Za-z0-9*][A-Za-z0-9.*-]*)["'\'']?[[:space:]]*$/\2/p' "$CONFIG" 2>/dev/null | tr 'A-Z' 'a-z')
fi
is_routed() {
  local h suffix
  for h in $ROUTED; do
    [ "$h" = "$1" ] && return 0
    case "$h" in '*.'*) suffix=${h#\*}; case "$1" in *"$suffix") return 0;; esac;; esac
  done
  return 1
}
mkdir -p "$(dirname "$STATE")"
misses=0; last=0
[ -f "$STATE" ] && read -r misses last < "$STATE"
now=$(date +%s)
checked=0; public_code=none
for pair in "${PAIRS[@]}"; do
  read -r local_url public_url <<< "$pair"
  host=${public_url#https://}; host=${host%/}
  local_code=$(curl -s -o /dev/null -m 5 -w '%{http_code}' "$local_url" || true)
  case "$local_code" in 200|401) ;; *) echo "local $local_url answered $local_code; not the tunnel's fault, skip"; continue;; esac
  public_code=$(curl -s -o /dev/null -m 15 -w '%{http_code}' "$public_url" || true)
  case "$public_code" in
    200|401|301|302) echo "0 $last" > "$STATE"; exit 0;;
  esac
  if ! is_routed "$host"; then
    case "$public_code" in 404|000) echo "public $public_url answered $public_code: not applied yet, skip"; continue;; esac
  fi
  checked=$((checked + 1))
  echo "public $public_url answered $public_code"
done
if [ "$checked" -eq 0 ]; then echo "0 $last" > "$STATE"; exit 0; fi
misses=$((misses + 1))
echo "every checked public URL failed (miss $misses)"
if [ "$misses" -ge 2 ] && [ $((now - last)) -ge 120 ]; then
  echo "restarting $UNIT"
  systemctl --user restart "$UNIT"
  echo "0 $now" > "$STATE"
else
  echo "$misses $last" > "$STATE"
fi
"""


def watchdog_script(unit, pairs, state):
    """The tunnel watchdog, carried from an earlier harness's script (2026-09-23): two failed checks in a
    row restart the tunnel unit, at most once per 120 s; a public 200/401/301/302 is healthy; a
    local answer other than 200/401 is not the tunnel's fault. One connector serves every hostname,
    so ANY healthy public answer means it is up, and a failed check counts only when EVERY public
    URL whose local server answers has failed. A public 404 (or no answer) from a hostname the
    live config does not route is "not applied yet", never a miss. Which hostnames are routed is
    read from the config at run time: its path is the script's one argument (the unit passes the
    validated tunnel config path), never text written into the script. When that config cannot
    be read, the script warns and uses the routed hostnames of generation time (the pairs whose
    third element is true), so it can still restart a dead tunnel."""
    import shlex
    head = ['#!/usr/bin/env bash',
            '# Restart the host tunnel when none of its public hostnames reaches a healthy local server.',
            '# Generated by `alpaca collect services --tunnel`. Usage: <script> /absolute/path/to/config.yml',
            '# cloudflared can hold a half-dead edge connection after a network drop and never reconnect',
            '# (process up, "does not have any active connection", public 530). Runs every 30 s from its',
            '# timer. Two misses in a row -> restart the tunnel unit, at most once per 120 s. A miss is a',
            '# check where every public URL failed while its local server answered.',
            'UNIT='+shlex.quote(unit),
            'STATE='+shlex.quote(state),
            '# local url, public url',
            'PAIRS=(']
    head += ['  '+shlex.quote('%s %s' % (pair[0], pair[1])) for pair in pairs]
    head += [')']
    # the hostnames the config routed at generation (validated DNS names), used only when the
    # config cannot be read at run time
    routed = [pair[1][len('https://'):].rstrip('/') for pair in pairs if len(pair) > 2 and pair[2]]
    from alpaca import workspace
    routed = [h for h in routed if workspace.HOST_RE.fullmatch(h)]
    head += ['GENERATED_ROUTED='+shlex.quote(' '.join(routed))]
    return '\n'.join(head)+'\n'+WATCHDOG_BODY


def web_up_script(root, prefix, port, public=False):
    """The per-instance @reboot fallback, carried from an earlier harness's web-up script: start this
    instance's own units when installed, else the dashboard directly. Never a cloudflared: the
    one host tunnel has its own unit and watchdog. A public (tunnel-routed) instance starts only
    behind its sign-in: --remote, and a refusal when it has no login. A login is `.alpaca/files-auth`
    or ALPACA_FILES_AUTH, from the script's environment or from the units' `.alpaca/service.env` (read
    with one fixed pattern, never sourced)."""
    import shlex
    units = [prefix+'-dashboard.service', prefix+'-collector.service']
    lines = [
        '#!/usr/bin/env bash',
        '# Bring this Alpaca instance\'s web surface back after a reboot. Its units also run under',
        '# systemd (linger on, WantedBy=default.target); this @reboot script is the fallback and each',
        '# step is a no-op when already up. Generated by `alpaca collect services`.',
        'set -u',
        'ROOT='+shlex.quote(root),
        'LOG="$ROOT/.alpaca/web-up.log"',
        'cd "$ROOT" || exit 1']
    if public:
        lines += [
            '# this instance has a public hostname: it never starts without its web login, which is',
            '# .alpaca/files-auth or ALPACA_FILES_AUTH (this environment, else the units\' .alpaca/service.env)',
            'login="${ALPACA_FILES_AUTH:-}"',
            'if [ -z "$login" ] && [ -f "$ROOT/.alpaca/service.env" ]; then',
            "  login=$(sed -n -E 's/^[[:space:]]*(export[[:space:]]+)?ALPACA_FILES_AUTH=[\"'\\'']?([^\"'\\'']*)[\"'\\'']?[[:space:]]*$/\\2/p' \"$ROOT/.alpaca/service.env\" | tail -n 1)",
            'fi',
            'if [ ! -s "$ROOT/.alpaca/files-auth" ] && [ -z "$login" ]; then',
            '  echo "alpaca web-up: not started: public hostname and no web login ($ROOT/.alpaca/files-auth or ALPACA_FILES_AUTH)" | tee -a "$LOG" >&2',
            '  exit 1',
            'fi',
            'if [ -n "$login" ]; then export ALPACA_FILES_AUTH="$login"; fi']
    lines += [
        'for unit in '+' '.join(units)+'; do',
        '  if systemctl --user cat "$unit" >/dev/null 2>&1; then',
        '    systemctl --user start "$unit" >> "$LOG" 2>&1 || true',
        '  fi',
        'done',
        '# no installed dashboard unit: start the server directly (prints "already running" if up)',
        'if ! systemctl --user cat '+units[0]+' >/dev/null 2>&1; then',
        '  "$ROOT/bin/alpaca-python" -m alpaca serve --detach --keep%s --port %d >> "$LOG" 2>&1 || true'
        % (' --remote' if public else '', int(port)),
        'fi']
    return '\n'.join(lines)+'\n'


def write_services(root, *, port=None, tunnel=None, cloudflared=None, tunnel_config=None,
                   watchdog_state=None, watchdog_script_path=None):
    """Write this instance's units (dashboard, collector, backup) and its @reboot web-up script
    under .alpaca/services/, and with `tunnel` the host-level tunnel unit plus its watchdog. Nothing is
    installed, enabled or started; the result lists the manual steps. Every input (names, paths,
    the registry, the live config) is validated and every file body built before the first file is
    written, so a refusal leaves nothing behind."""
    from alpaca import serve, workspace
    root=str(Path(root).resolve())
    # Validate before generating any units. The existing tunnel name is data,
    # never an option string, shell command, credential or token.
    if tunnel is None and any(v is not None for v in (cloudflared, tunnel_config, watchdog_state, watchdog_script_path)):
        raise ValueError("--cloudflared, --tunnel-config and --watchdog-state require --tunnel")
    if tunnel is not None:
        if not isinstance(tunnel, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", tunnel):
            raise ValueError("tunnel must be a name or UUID using letters, digits, dot, underscore or hyphen")
        cloudflared = _safe_path("cloudflared", "/usr/local/bin/cloudflared" if cloudflared is None else cloudflared)
        tunnel_config = _safe_path("tunnel config", workspace.cloudflared_config_path() if tunnel_config is None else tunnel_config)
        watchdog_state = _safe_path("watchdog state", os.path.expanduser(
            '~/.local/state/alpaca/tunnel-%s-watchdog.state' % tunnel) if watchdog_state is None else watchdog_state)
        watchdog_script_path = _safe_path("watchdog script", os.path.expanduser(
            '~/.config/alpaca/tunnel-%s-watchdog.sh' % tunnel) if watchdog_script_path is None else watchdog_script_path)
    # G1: the registered port, else the derived one; never a reserved port by default. A corrupt registry
    # raises here, before any file exists (M4).
    entries = workspace.load()
    port = serve._instance_port(root) if port is None else workspace.valid_port(int(port))
    public = workspace.public(root, port)
    live = workspace.read_live(tunnel_config, required=False)[2] if tunnel is not None else {}
    instance_units = serve.service_units(root, port=port)
    prefix='alpaca-'+hashlib.sha256(root.encode()).hexdigest()[:12]
    def quote(value):
        # systemd expands % specifiers; escape them. `$` never reaches here (_safe_path refuses it)
        return '"'+str(value).replace('%','%%').replace('\\','\\\\').replace('"','\\"')+'"'
    folder=Path(root)/'.alpaca/services'
    service=prefix+'-backup.service'
    timer=prefix+'-backup.timer'
    units=dict(instance_units)
    units.update({service:('[Unit]\nDescription=Alpaca verified local backup\n\n[Service]\nType=oneshot\n'
                   'WorkingDirectory='+root.replace('%','%%')+'\nExecStart='+quote(Path(root)/'bin/alpaca-python')+
                   ' -m alpaca collect backup\nUMask=0077\nTimeoutStartSec=30min\nStandardOutput=journal\nStandardError=journal\n'),
           timer:('[Unit]\nDescription=Alpaca backup every six hours\n\n[Timer]\nOnCalendar=*-*-* 00,06,12,18:15:00\n'
                  'Persistent=true\nRandomizedDelaySec=120\nUnit='+service+'\n\n[Install]\nWantedBy=timers.target\n')})
    scripts={prefix+'-web-up.sh': web_up_script(root, prefix, port, public)}
    notes={}
    if tunnel is not None:
        # G2: one host-level unit for the one tunnel, reading the combined config explicitly
        # (alpaca workspace render-ingress renders it; the owner copies it to tunnel_config).
        tunnel_unit=tunnel_unit_name(tunnel)
        units[tunnel_unit] = (
            '[Unit]\nDescription=Alpaca host tunnel '+tunnel+'\nStartLimitIntervalSec=120\nStartLimitBurst=10\n\n'
            '[Service]\nType=simple\nWorkingDirectory='+os.path.dirname(tunnel_config).replace('%','%%')+'\n'
            'ExecStart='+quote(cloudflared)+' --config '+quote(tunnel_config)+' tunnel run '+quote(tunnel)+'\n'
            'Restart=on-failure\nRestartSec=5\nTimeoutStopSec=30\nKillMode=control-group\n'
            'UMask=0077\nStandardOutput=journal\nStandardError=journal\n'
            'SyslogIdentifier=alpaca-tunnel-'+tunnel+'\n\n[Install]\nWantedBy=default.target\n')
        # G4: the watchdog for that unit, over the live config's hostnames and the registry's.
        pairs=watchdog_pairs(entries, live)
        if pairs:
            base='alpaca-tunnel-'+tunnel+'-watchdog'
            scripts[base+'.sh']=watchdog_script(tunnel_unit, pairs, watchdog_state)
            units[base+'.service']=('[Unit]\nDescription=Alpaca tunnel watchdog for '+tunnel+
                                    ' (restarts a tunnel that lost the edge)\n\n[Service]\nType=oneshot\n'
                                    'ExecStart='+quote(watchdog_script_path)+' '+quote(tunnel_config)+
                                    '\nSyslogIdentifier='+base+'\n')
            units[base+'.timer']=('[Unit]\nDescription=Check the '+tunnel+' tunnel hostnames every 30 s\n\n'
                                  '[Timer]\nOnBootSec=90\nOnUnitActiveSec=30\nAccuracySec=5\n\n'
                                  '[Install]\nWantedBy=timers.target\n')
            notes['watchdog']={'script':str(folder/(base+'.sh')),'install_as':watchdog_script_path,
                               'state':watchdog_state,'pairs':[[l, p, r] for l, p, r in pairs]}
        else:
            notes['watchdog']='not generated: neither the live config nor the registry names a hostname'
    # L3: every file carries its final mode from creation.
    for name,body in units.items():
        workspace.write_file(str(folder/name), body, 0o600)
    for name,body in scripts.items():
        workspace.write_file(str(folder/name), body, 0o700)
    web_up=str(folder/(prefix+'-web-up.sh'))
    out={'directory':str(folder),'units':sorted(units),
         'scripts':sorted(str(folder/name) for name in scripts),'port':port,
         'local':'http://127.0.0.1:%d/' % port,
         # G5: printed, never installed
         'crontab':'@reboot '+web_up,'installed':False,'started':False}
    out.update(notes)
    return out
