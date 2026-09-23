# Workspaces and the tunnel pathway

This guide takes one Alpaca instance from a local root to its own cockpit on localhost and on a public
hostname through the host's Cloudflare tunnel. It carries what a tunnel host typically runs (its
dashboard unit, `~/.cloudflared/config.yml`, the tunnel watchdog and the `@reboot` web-up script)
as generated files, so nothing on the host is hand-made per instance.

Alpaca never installs, enables or starts a unit, never edits `~/.cloudflared`, never creates a tunnel or
a DNS record and never edits the crontab. It writes files under the instance's `.alpaca/services/` and
one host registry file, and it prints the rest as owner steps.

## The model

- **One tunnel per host.** One named Cloudflare tunnel serves every instance on the host. Several
  cloudflared connectors on one tunnel id with different configs would split requests between them
  at random, so no instance runs a cloudflared of its own.
- **One registry per host.** `alpaca workspace add` records each instance in a JSON file,
  `$ALPACA_WORKSPACES` or `~/.config/alpaca/workspaces.json` (mode 0600):
  `{"version": 1, "workspaces": [{"id", "name", "root", "port", "hostname", "href"}]}`.
  `id` is the first 12 hex of sha256 of the real root, the same id the unit names carry
  (`alpaca-<id>-dashboard.service`). Only `alpaca workspace add`, `move` and `remove` write it.
- **One combined config.** `alpaca workspace render-ingress` turns the registry into one cloudflared
  config, emitted with `yaml.safe_dump`. Every rule of the live file is kept unchanged and in its
  order (hostname rules, path-only rules, the live catch-all), its settings (for example
  `protocol: http2`) are carried, and each registered hostname the live file does not route yet
  is added just before the live catch-all (or before a new `http_status:404` when the live file
  has none). A live rule is never replaced or dropped: a registered hostname that a live rule
  routes any other way is an error. The live file must be readable to render.
- **Validated registry.** `add` and every read check each entry: `id` is 12 hex, `name` is
  printable text of at most 64 characters with no newline or control character, `hostname` is a
  DNS name, `port` is 1 to 65535, `root` is an absolute path, and `id` must equal the id of the
  stored `root` string (the real path `add` stored; a root moved or re-symlinked later keeps its
  id); ids, ports and hostnames are unique. One malformed or conflicting entry never makes the
  others unreadable: readers that need other entries skip it with a warning on stderr, and only a
  reader that needs that entry itself refuses (that instance's serve port, for example).
  `alpaca workspace add` repairs its own bad entry and keeps every other one, and its port and
  hostname claims, untouched; `alpaca workspace remove --id <id>` drops any entry, valid or not.
  `alpaca workspace list` shows each valid entry with `stale` (its root is no longer a directory)
  and each skipped entry under `invalid` with the reason.
  A registry file that does not parse fails only the paths that need it: `collect services` and
  `serve --write-services` refuse (and `collect services` validates everything before it writes any
  file), and `alpaca serve` without `--port` refuses because the registry names its port. `alpaca serve`
  with an explicit `--port` (every installed dashboard unit passes one) starts with a one-line
  warning, so a corrupt registry never leaves the units failed. That start fails closed in one
  case: with neither `--remote` nor a web login, it refuses when the raw registry text names this
  root, since the instance may be public.
- **Ports.** An instance serves on its registered port, else on a port derived from its root
  (`serve._default_port`). Ports listed in `$ALPACA_RESERVED_PORTS` (comma-separated, empty by
  default; list a port another server on the host binds exactly) are never a default, a non-strict
  bind scan skips them, and `alpaca workspace add --port P` for a reserved P
  is refused unless the instance already serves on it. A port another entry holds, or a port any
  live ingress rule serves, is refused too.
- **Hostnames.** `add --hostname` refuses a hostname another entry holds, any hostname a live rule
  already routes (except this instance's own rule once applied), and any hostname when this
  instance has no web login (`.alpaca/files-auth` or `ALPACA_FILES_AUTH`), or when this instance already
  runs a server without `--remote` (restart it with `alpaca serve --stop` then `alpaca serve --remote`
  first). `add` refuses outright when the
  live cloudflared config is missing or unparsable, unless `--no-live-config` says the host has no
  tunnel config yet (then `render-ingress` refuses until one is readable).
- **Always behind sign-in when public.** An instance is public when it has a registered hostname
  or when any rule of the live cloudflared config (`$ALPACA_CLOUDFLARED_CONFIG`, else
  `~/.cloudflared/config.yml`, read only) routes to its port; a config that does not parse counts
  as routing the port when its raw text names it. A public instance is started with
  `--remote` by every path (`alpaca serve`, the session-start autostart, the web-up script), and each
  refuses to start, with a message, when the web login is missing. The web-up script accepts the
  login as `.alpaca/files-auth` or as `ALPACA_FILES_AUTH`, from its own environment or from the units'
  `.alpaca/service.env` (read with one fixed pattern, never sourced).
- **Wildcards.** `render-ingress` prints a warning for each new hostname that a live wildcard rule
  (for example `*.example.com`) matches: the live rules come first, so the new rule would never be
  reached until the owner narrows or moves the wildcard.

## Verbs

```
alpaca workspace add [--name N] [--port P] [--hostname H] [--no-live-config]
alpaca workspace list
alpaca workspace remove [--id ID]
alpaca workspace move (--from OLD_ROOT | --id ID) [--name N] [--no-live-config]
alpaca workspace render-ingress --tunnel NAME [--out FILE] [--config FILE]
                            [--tunnel-id ID] [--credentials-file FILE]
alpaca collect services [--port P] [--tunnel NAME] [--cloudflared PATH]
                    [--tunnel-config PATH] [--watchdog-state PATH]
alpaca serve --write-services [--port P]
```

`render-ingress` reads the tunnel id and `credentials-file` from `~/.cloudflared/config.yml`
(read only; `--config` names another file, `--tunnel-id` and `--credentials-file` override). It
writes only `--out` (default `.alpaca/services/cloudflared-<tunnel>.yml`, refused inside
`~/.cloudflared`), prints a unified diff against the live file, and prints, never runs, the
`cloudflared tunnel route dns <tunnel> <hostname>` command for each hostname the live file does
not route yet, plus the copy step.

`collect services` writes, under `.alpaca/services/`:

| file | scope | what it does |
|---|---|---|
| `alpaca-<id>-dashboard.service` | instance | `alpaca serve --keep --remote --port <port>`, exact port, `Restart=always` |
| `alpaca-<id>-collector.service` | instance | `alpaca collect watch --interval 15` |
| `alpaca-<id>-backup.service`, `.timer` | instance | verified local backup every six hours |
| `alpaca-<id>-web-up.sh` | instance | `@reboot` fallback: starts this instance's installed units, else `alpaca serve --detach --keep --port <port>` (plus `--remote`, and a refusal without a web login, when the instance has a hostname); never a cloudflared |
| `alpaca-tunnel-<name>.service` | host (with `--tunnel`) | `cloudflared --config <combined config> tunnel run <name>`; identical from every instance |
| `alpaca-tunnel-<name>-watchdog.sh`, `.service`, `.timer` | host (with `--tunnel` and at least one hostname) | every 30 s, checks each (local, public) pair: every hostname rule of the live config at generation (so `example.com` is always checked) plus each registered hostname. Any healthy public answer (200, 401, 301, 302) means the one connector is up. A check is a miss only when EVERY public URL whose local server answers (200 or 401) has failed; a public 404 or no answer from a hostname the config does not route is "not applied yet", not a miss. Which hostnames are routed is read from the config at run time: the unit passes the tunnel config path as the script's one argument, and the script reads hostnames with one fixed pattern (wildcards included), so applying a new config needs no regeneration. When the config cannot be read, the script prints a warning and falls back to the hostnames the config routed at generation time. Two misses in a row restart the tunnel unit, at most once per 120 s. State in `--watchdog-state` (default `~/.local/state/alpaca/tunnel-<name>-watchdog.state`) |

The command prints the `@reboot` crontab line for the web-up script; it never installs it.

## The hub

`/` on any instance is the workspace hub. Its own tile opens `/hub/` on the same origin. Each other
registered instance gets a tile whose link is absolute: `https://<hostname>/hub/` when it has a
hostname, else `http://127.0.0.1:<port>/hub/`. Another instance's tile shows reachability only:
the hub answers `/workspaces/summary.json?id=<id>` server side with `{"reachable": true|false}`,
from a GET of that instance's public `/login` page on its loopback port (true only for a 200; 2 s
timeout; redirects refused). The hub never reads another instance's `.alpaca/files-auth` and never
sends any credential, because whatever process holds that port (another user on a shared host, an
unregistered server) would receive it, and nothing of the other instance's record reaches this
page. To see its cockpit, open the tile and sign in there.

Each instance signs browsers in with its own cookie, `alpaca_session_<8 hex of the instance id>`, so
two instances on 127.0.0.1 (cookies ignore the port) never share a cookie slot. A cookie under the
old shared name `alpaca_session` reads as signed out.

## Steps: put one new instance on `<name>.example.com`

Run the `alpaca` steps inside the instance root. Steps marked **owner** change files outside the
repository or DNS, and they are the owner's decision.

1. Set a web login first (a public instance always runs with `--remote`): write `user:code` to
   `.alpaca/files-auth` (mode 0600).
2. Register the instance: `bin/alpaca workspace add --name <Name> --hostname <name>.example.com`.
   The output gives the local URL `http://127.0.0.1:<port>/`. Changes
   `~/.config/alpaca/workspaces.json` (created on first use).
3. Write the units: `bin/alpaca collect services`. Add `--tunnel <tunnel>` only to generate the host
   tunnel unit and watchdog (step 7).
4. Render the combined config: `bin/alpaca workspace render-ingress --tunnel <tunnel>`. Read the diff:
   it should add one `<name>.example.com` rule and keep `example.com` and every other live rule.
5. **owner** DNS: `cloudflared tunnel route dns <tunnel> <name>.example.com` (one CNAME record in the
   Cloudflare zone).
6. **owner** config: back up and replace the live file,
   `cp -p ~/.cloudflared/config.yml ~/.cloudflared/config.yml.bak-$(date +%Y%m%d-%H%M%S)` then
   `install -m 600 .alpaca/services/cloudflared-<tunnel>.yml ~/.cloudflared/config.yml`.
7. **owner** tunnel: restart the one unit that runs `cloudflared tunnel run <tunnel>`. For a host
   that already runs one, for example `systemctl --user restart <old-tunnel-unit>.service`, which reads
   `~/.cloudflared/config.yml` by default.

   To move the host to the generated unit instead, first stop and disable everything that runs or
   restarts the old connector:
   `systemctl --user disable --now old-tunnel-watchdog.timer`,
   `systemctl --user stop old-tunnel-watchdog.service`,
   `systemctl --user disable --now <old-tunnel-unit>.service`.
   The old watchdog restarts `<old-tunnel-unit>.service` whenever `example.com` looks
   down, so leaving it enabled would start the old connector again beside the new one: two
   connectors on one tunnel id split requests between them at random. Then install and enable
   `alpaca-tunnel-<tunnel>.service` and, if wanted, its watchdog
   (`install -m 700 .alpaca/services/alpaca-tunnel-<tunnel>-watchdog.sh ~/.config/alpaca/tunnel-<tunnel>-watchdog.sh`
   plus its `.service` and `.timer`). Never run both tunnel units or both watchdogs.
8. **owner** units: `cp .alpaca/services/alpaca-<id>-*.service .alpaca/services/alpaca-<id>-backup.timer ~/.config/systemd/user/`,
   `systemctl --user daemon-reload`,
   `systemctl --user enable --now alpaca-<id>-dashboard.service alpaca-<id>-collector.service alpaca-<id>-backup.timer`.
9. **owner** reboot fallback: add the printed line `@reboot <root>/.alpaca/services/alpaca-<id>-web-up.sh`
   with `crontab -e` (linger must be on for the units themselves: `loginctl show-user $USER -p Linger`).
10. Check: `curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:<port>/` and the same for
    `https://<name>.example.com/` answer 200 or 401.

To take an instance off: `bin/alpaca workspace remove`, render again, and the owner applies the new
config and deletes the DNS record.

## Moving a workspace folder

The instance id is derived from the real root, so a moved folder is a new instance to the
registry. Do not `remove` the old entry and `add` again: once the old entry is gone, its live
ingress rule looks like another service's rule and `add` refuses the port. Instead:

1. `bin/alpaca serve --stop` in the old folder, then move the folder.
2. From the new folder: `bin/alpaca workspace move --from <old root>` (or `--id <old id>`, from
   `bin/alpaca workspace list`). It replaces the old entry with one for the new root that keeps
   the port, the hostname and the name, and adopts the live rule that routes that hostname to that
   port as this instance's own, so the tunnel config does not change (`"adopted_rule": true`).
3. `bin/alpaca collect services` again (the unit names carry the id, which changed), reinstall the
   units, and start the server with `bin/alpaca serve --remote`.

`move` refuses when the old root still exists (that is a copy, not a move), when this folder is
already registered, and when anything other than the old entry claims the port or hostname: an
entry, a malformed entry, or a live rule other than the old entry's own. A refused `add` that
collides with an entry whose root no longer exists names the `move` command to run.
