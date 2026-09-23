"""alpaca workspace: the host workspace registry and the one combined tunnel ingress.

One host runs one named Cloudflare tunnel for every instance. Several cloudflared connectors on
one tunnel id with different configs would split requests between them at random, so no instance
runs a cloudflared of its own. Instead a host-level registry lists every instance, and one
combined config routes each registered hostname to that instance's local port.

The registry is a JSON file, `$ALPACA_WORKSPACES` or `~/.config/alpaca/workspaces.json`:

    {"version": 1, "workspaces": [{"id", "name", "root", "port", "hostname", "href"}, ...]}

`id` is the instance id, the first 12 hex of sha256(real root), the same id the generated
systemd unit names carry (`alpaca-<id>-dashboard.service`). `hostname` is optional; `href` is the
absolute address of the instance's cockpit (https://<hostname>/hub/ with a hostname, else
http://127.0.0.1:<port>/hub/).

Verbs (docs/workspaces.md walks the whole pathway):

    alpaca workspace add [--name N] [--port P] [--hostname H]   register this instance
    alpaca workspace list                                       print the registry
    alpaca workspace remove [--id ID]                           drop this instance (or ID)
    alpaca workspace render-ingress --tunnel NAME [--out FILE] [--config FILE]
                                [--tunnel-id ID] [--credentials-file FILE]

Only `add` and `remove` write, and only the registry. `render-ingress` reads the live
cloudflared config (never writes it), writes the combined config to --out (default
`.alpaca/services/cloudflared-<tunnel>.yml`), prints a unified diff against the live file, and prints
(never runs) the DNS route commands and the copy step. Applying it is an owner decision.
"""
import contextlib
import difflib
import hashlib
import json
import os
import re
import secrets
import urllib.error
import urllib.request

from alpaca import cli

REGISTRY_ENV = "ALPACA_WORKSPACES"
#: Ports no instance takes by default: a comma-separated list in $ALPACA_RESERVED_PORTS, empty by
#: default. A host that runs another server on a fixed port (strict bind, restart on any exit)
#: lists that port here, so no Alpaca server fights it for the port.
RESERVED_PORTS_ENV = "ALPACA_RESERVED_PORTS"


class _ReservedPorts:
    """The reserved ports as a live container: `port in RESERVED_PORTS` reads the environment on
    every test, so a setting changed after import applies at once. Entries that are not a port
    number (1 to 65535) are ignored."""

    def ports(self):
        found = set()
        for part in (os.environ.get(RESERVED_PORTS_ENV) or "").replace(";", ",").split(","):
            part = part.strip()
            if part.isdigit() and 0 < int(part) < 65536:
                found.add(int(part))
        return frozenset(found)

    def __contains__(self, port):
        try:
            return int(port) in self.ports()
        except (TypeError, ValueError):
            return False

    def __iter__(self):
        return iter(sorted(self.ports()))

    def __len__(self):
        return len(self.ports())

    def __repr__(self):
        return "RESERVED_PORTS%r" % (tuple(self),)


RESERVED_PORTS = _ReservedPorts()
TUNNEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
HOST_RE = re.compile(r"(?=.{1,253}$)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+")
ID_RE = re.compile(r"[0-9a-f]{12}")
LOCAL_SERVICE_RE = re.compile(r"https?://(?:127\.0\.0\.1|localhost|\[::1\]):(\d+)/?$")
NAME_MAX = 64
SUMMARY_TIMEOUT_S = 2.0
SUMMARY_MAX_BYTES = 16 * 1024 * 1024
HEADER = ("# Combined cloudflared config rendered by `alpaca workspace render-ingress` from the host\n"
          "# workspace registry. One tunnel, one connector, one config. Every rule of the live config\n"
          "# is kept unchanged and in order; registered hostnames are added before its catch-all.\n"
          "# The owner applies it by copying it over the live config (docs/workspaces.md).\n")


# ---- files -------------------------------------------------------------------------------------

def write_file(path, text, mode):
    """Write `text` to `path` atomically, the file carrying `mode` from its creation (never a
    wider mode first and a chmod after)."""
    folder = os.path.dirname(os.path.abspath(path))
    os.makedirs(folder, exist_ok=True)
    tmp = os.path.join(folder, ".tmp-%s-%s" % (os.path.basename(path), secrets.token_hex(6)))
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        os.fchmod(fd, mode)         # the umask may have narrowed it; make it exactly `mode`
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    os.replace(tmp, path)


# ---- paths and identity ----------------------------------------------------------------------

def registry_path():
    """The host registry file: $ALPACA_WORKSPACES, else ~/.config/alpaca/workspaces.json."""
    value = (os.environ.get(REGISTRY_ENV) or "").strip()
    return os.path.abspath(os.path.expanduser(value)) if value else os.path.expanduser(
        os.path.join("~", ".config", "alpaca", "workspaces.json"))


CLOUDFLARED_ENV = "ALPACA_CLOUDFLARED_CONFIG"


def cloudflared_config_path():
    """The live cloudflared config of this host (read only, never written here):
    $ALPACA_CLOUDFLARED_CONFIG, else ~/.cloudflared/config.yml."""
    value = (os.environ.get(CLOUDFLARED_ENV) or "").strip()
    return os.path.abspath(os.path.expanduser(value)) if value else os.path.expanduser(
        os.path.join("~", ".cloudflared", "config.yml"))


def instance_id(root):
    """The instance id: the first 12 hex of sha256(real root), as in the unit names."""
    return _id_of(os.path.realpath(root))


def _id_of(stored_root):
    """The id of a root string exactly as `add` stored it (add stores the real path)."""
    return hashlib.sha256(stored_root.encode()).hexdigest()[:12]


def href_of(entry):
    """The absolute cockpit address of a registry entry."""
    if entry.get("hostname"):
        return "https://%s/hub/" % entry["hostname"]
    return "http://127.0.0.1:%d/hub/" % int(entry["port"])


# ---- validation --------------------------------------------------------------------------------

def valid_name(name):
    """A tile name: printable text, no newline or control character, 1..NAME_MAX characters."""
    if not isinstance(name, str) or not name.strip() or len(name) > NAME_MAX or not name.isprintable():
        raise ValueError("name must be printable text of 1 to %d characters with no newline or control character" % NAME_MAX)
    return name


def valid_hostname(hostname):
    value = str(hostname or "").strip().lower().rstrip(".")
    if not HOST_RE.fullmatch(value):
        raise ValueError("hostname must be a DNS name such as name.example.com")
    return value


_valid_hostname = valid_hostname


def valid_port(port):
    if isinstance(port, bool) or not isinstance(port, int) or not 0 < port < 65536:
        raise ValueError("port must be an integer between 1 and 65535")
    return port


def _check_entry(item, path):
    """A registry entry exactly as `add` writes it; anything else is a corrupt registry."""
    try:
        if not isinstance(item, dict):
            raise ValueError("not an object")
        if not (isinstance(item.get("id"), str) and ID_RE.fullmatch(item["id"])):
            raise ValueError("id must be 12 hex")
        valid_name(item.get("name"))
        valid_port(item.get("port"))
        root = item.get("root")
        if not (isinstance(root, str) and os.path.isabs(root) and root.isprintable()):
            raise ValueError("root must be an absolute printable path")
        # the stored string, not its current realpath: a moved or re-symlinked root keeps its id
        # (the entry then reads as stale), and never makes the registry unreadable
        if item["id"] != _id_of(root):
            raise ValueError("id %s is not the id of root %s" % (item["id"], root))
        if item.get("hostname") is not None and valid_hostname(item["hostname"]) != item["hostname"]:
            raise ValueError("hostname is not in canonical form")
    except ValueError as error:
        raise ValueError("workspace registry %s holds a malformed entry (%s)" % (path, error))
    out = {k: item[k] for k in ("id", "name", "root", "port")}
    out["hostname"] = item.get("hostname")
    out["href"] = href_of(out)          # derived, never trusted from the file
    return out


# ---- registry file -----------------------------------------------------------------------------

def _read(path=None):
    """(entries, problems, raw items, path). A file that is not a JSON object with a workspaces
    list raises ValueError (nothing in it can be trusted). A single malformed or conflicting entry
    only lands in `problems` ({index, id, root, reason}): it never makes the others unreadable."""
    path = path or registry_path()
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return [], [], [], path
    except (OSError, ValueError) as error:
        raise ValueError("workspace registry %s is unreadable: %s" % (path, error))
    items = data.get("workspaces") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise ValueError("workspace registry %s has no workspaces list" % path)
    checked, problems = [], []

    def problem(index, item, reason):
        item = item if isinstance(item, dict) else {}
        problems.append({"index": index, "reason": reason,
                         "id": item.get("id") if isinstance(item.get("id"), str) else None,
                         "root": item.get("root") if isinstance(item.get("root"), str) else None})

    for index, item in enumerate(items):
        try:
            checked.append((index, _check_entry(item, path)))
        except ValueError as error:
            problem(index, item, str(error))
    for key in ("id", "port", "hostname"):
        counts = {}
        for _index, e in checked:
            if e[key] is not None:
                counts[e[key]] = counts.get(e[key], 0) + 1
        clash = {value for value, n in counts.items() if n > 1}
        if clash:
            for index, e in [(i, e) for i, e in checked if e[key] in clash]:
                problem(index, items[index], "duplicate %s %s" % (key, e[key]))
            checked = [(i, e) for i, e in checked if e[key] not in clash]
    return [e for _i, e in checked], sorted(problems, key=lambda p: p["index"]), items, path


def _warn(problems, path):
    import sys
    for p in problems:
        print("alpaca workspace: WARNING: skipping registry entry %d of %s (%s)" % (p["index"], path, p["reason"]),
              file=sys.stderr, flush=True)


def load(path=None):
    """The valid registered entries, [] when the file is absent. A file that does not parse
    raises ValueError; a malformed or conflicting entry is skipped with a warning on stderr, so
    one bad entry never makes the other instances unreadable. `entry` refuses when the skipped
    entry is the caller's own."""
    entries, problems, _items, path = _read(path)
    _warn(problems, path)
    return entries


def problems(path=None):
    """The skipped registry entries ({index, id, root, reason}) without printing anything."""
    return _read(path)[1]


def _is_own(problem, root):
    real = os.path.realpath(root)
    return problem.get("id") == _id_of(real) or problem.get("root") == real


def _save(entries, path):
    folder = os.path.dirname(path)
    os.makedirs(folder, mode=0o700, exist_ok=True)
    body = json.dumps({"version": 1, "workspaces": entries}, indent=2, sort_keys=True) + "\n"
    write_file(path, body, 0o600)


@contextlib.contextmanager
def _locked(path):
    import fcntl
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    with open(path + ".lock", "a", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def entry(root, entries=None):
    """This instance's registry entry, or None. A registry file that does not parse, or a
    malformed entry that is this instance's own, raises ValueError (the entry is needed here);
    another instance's bad entry is skipped with a warning."""
    ident = instance_id(root)
    if entries is None:
        entries, bad, _items, path = _read()
        own = [p for p in bad if _is_own(p, root)]
        if own:
            raise ValueError("this instance's workspace registry entry %d of %s is malformed (%s); "
                             "repair it with `alpaca workspace add` or drop it with `alpaca workspace remove --id`"
                             % (own[0]["index"], path, own[0]["reason"]))
        _warn(bad, path)
    return next((e for e in entries if e.get("id") == ident), None)


def instance_port(root):
    """The port this instance serves on: its registered port, else serve._default_port(root)
    (which never answers a reserved port). Never a reserved port by default. A corrupt registry raises
    ValueError rather than falling back to a port the registry may give to another instance."""
    found = entry(root)
    if found:
        return int(found["port"])
    from alpaca import serve
    return serve._default_port(root)


def public(root, port=None):
    """True when this instance is reachable through the tunnel, so it must always run behind its
    sign-in: it has a registered public hostname, or a rule of the live cloudflared config routes
    to its port (`port`, else its registered port). A corrupt registry raises ValueError."""
    found = entry(root)
    if found and found.get("hostname"):
        return True
    port = port or (found or {}).get("port")
    return bool(port) and live_routes_port(int(port))


def live_routes_port(port, config=None):
    """True when a rule of the live cloudflared config (read only) routes to 127.0.0.1:<port>.
    A config that does not parse fails closed: True when its raw text names the port."""
    path = config or cloudflared_config_path()
    try:
        _path, _text, live = read_live(path, required=False)
    except ValueError:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                return (":%d" % int(port)) in fh.read()
        except OSError:
            return False
    return any(_local_port(r.get("service")) == int(port) for r in _live_rules(live))


# ---- live cloudflared config (read only) -------------------------------------------------------

def read_live(config=None, *, required=True):
    """(path, text, parsed dict) of the live cloudflared config. A missing or unparsable file
    raises ValueError; with required=False a missing file gives ('', {})."""
    path = config or cloudflared_config_path()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
    except FileNotFoundError:
        if required:
            raise ValueError("cloudflared config %s does not exist; create the tunnel config first, "
                             "or pass --no-live-config to `alpaca workspace add` when this host has no tunnel yet" % path)
        return path, "", {}
    except OSError as error:
        raise ValueError("cloudflared config %s is unreadable: %s" % (path, error))
    import yaml
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError as error:
        raise ValueError("cloudflared config %s does not parse: %s" % (path, error))
    if not isinstance(data, dict):
        raise ValueError("cloudflared config %s is not a mapping" % path)
    rules = data.get("ingress")
    if rules is not None and not (isinstance(rules, list) and all(isinstance(r, dict) for r in rules)):
        raise ValueError("cloudflared config %s has an ingress that is not a list of rules" % path)
    return path, text, data


def _live_rules(data):
    return list(data.get("ingress") or []) if isinstance(data, dict) else []


def _host_of(rule):
    host = rule.get("hostname")
    return str(host).strip().lower().rstrip(".") if host else None


def _local_port(service):
    match = LOCAL_SERVICE_RE.match(str(service or ""))
    return int(match.group(1)) if match else None


def _applied(rule, item):
    """True when a live rule is exactly the rule render-ingress writes for `item`."""
    return (set(rule) == {"hostname", "service"} and _host_of(rule) == item.get("hostname")
            and _local_port(rule.get("service")) == int(item["port"]))


def _is_catch_all(rule):
    return not rule.get("hostname") and not rule.get("path")


def plan(live, entries):
    """(rules, new): every live rule unchanged and in order, each registered hostname the live
    config does not route yet added before the live catch-all (or before a new http_status:404
    when the live config has none). A registered hostname a live rule routes any other way is an
    error, never an override."""
    rules = _live_rules(live)
    by_host = {}
    for rule in rules:
        if _host_of(rule):
            by_host.setdefault(_host_of(rule), []).append(rule)
    new = []
    for item in sorted((e for e in entries if e.get("hostname")), key=lambda e: e["hostname"]):
        found = by_host.get(item["hostname"])
        if not found:
            new.append(item)
        elif not (len(found) == 1 and _applied(found[0], item)):
            raise ValueError("hostname %s of workspace %s collides with a live cloudflared rule (%s); "
                             "the live rule stays, remove or change the registration"
                             % (item["hostname"], item["id"], found[0].get("service")))
    added = [{"hostname": e["hostname"], "service": "http://127.0.0.1:%d" % int(e["port"])} for e in new]
    if rules and _is_catch_all(rules[-1]):
        combined = rules[:-1] + added + [rules[-1]]
    else:
        combined = rules + added + [{"service": "http_status:404"}]
    return combined, new


# ---- verbs -------------------------------------------------------------------------------------

def _pick_port(root, taken):
    from alpaca import serve
    port = serve._default_port(root)
    for _ in range(200):
        if port not in taken and port not in RESERVED_PORTS and not serve._port_open(port):
            return port
        port += 1
    raise ValueError("no free port near the default; pass --port")


def add(root, *, name=None, port=None, hostname=None, config=None, no_live_config=False):
    """Register (or update) this instance.

    Refuses: a port another entry holds; a reserved port ($ALPACA_RESERVED_PORTS) unless --port names it and it is
    already this instance's own; any port or hostname a live cloudflared rule serves, except this
    instance's own rule once applied; a hostname another entry holds; a hostname without a web
    login (ALPACA_FILES_AUTH or .alpaca/files-auth), since a public instance always runs behind its
    sign-in; and any change when the live cloudflared config cannot be read, unless
    no_live_config says this host has no tunnel config yet."""
    from alpaca import serve
    root = os.path.realpath(root)
    ident = instance_id(root)
    if port is not None:
        valid_port(int(port))
    if name is not None:
        valid_name(name)
    path = registry_path()
    with _locked(path):
        entries, bad, items, _p = _read(path)
        # this instance's own malformed entry is repaired (rewritten) here; another instance's
        # stays in the file untouched and keeps its port and hostname claims
        keep_raw = [items[p["index"]] for p in bad if not _is_own(p, root)]
        _warn([p for p in bad if not _is_own(p, root)], path)
        raw_ports = {i.get("port") for i in keep_raw if isinstance(i, dict) and isinstance(i.get("port"), int)}
        raw_hosts = {i.get("hostname") for i in keep_raw if isinstance(i, dict) and isinstance(i.get("hostname"), str)}
        mine = next((e for e in entries if e.get("id") == ident), None)
        # no --hostname keeps the registered one; --hostname "" clears it
        if hostname is None:
            host = (mine or {}).get("hostname") or None
        else:
            host = valid_hostname(hostname) if hostname else None
        others = [e for e in entries if e.get("id") != ident]
        if no_live_config:
            _cfg, _text, live = read_live(config, required=False)
            if _text:
                raise ValueError("--no-live-config was given but %s exists; drop the flag" % _cfg)
        else:
            _cfg, _text, live = read_live(config)
        rules = _live_rules(live)
        own_rule = next((r for r in rules if mine and mine.get("hostname") and _applied(r, mine)), None)
        foreign = [r for r in rules if r is not own_rule]
        live_ports = {_local_port(r.get("service")) for r in foreign} - {None}
        live_hosts = {_host_of(r) for r in foreign} - {None}
        taken = {int(e["port"]) for e in others} | live_ports | raw_ports
        if port is None:
            chosen = int(mine["port"]) if mine and int(mine["port"]) not in taken else _pick_port(root, taken)
        else:
            chosen = int(port)
            if chosen in RESERVED_PORTS:
                state = serve._read_state(root) or {}
                own = (mine and int(mine["port"]) == chosen) or state.get("port") == chosen
                if not own:
                    raise ValueError("port %d is reserved on this host ($ALPACA_RESERVED_PORTS); "
                                     "choose another --port" % chosen)
            holder = next((e for e in others if int(e["port"]) == chosen), None)
            if holder:
                raise ValueError("port %d is registered to %s (%s)" % (chosen, holder.get("name"), holder["id"]))
            if chosen in live_ports:
                raise ValueError("port %d is served by a live ingress rule of %s" % (chosen, _cfg))
            if chosen in raw_ports:
                raise ValueError("port %d is claimed by a malformed registry entry; repair or remove it first" % chosen)
        if host:
            holder = next((e for e in others if e.get("hostname") == host), None)
            if holder:
                raise ValueError("hostname %s is registered to %s (%s)" % (host, holder.get("name"), holder["id"]))
            if host in raw_hosts:
                raise ValueError("hostname %s is claimed by a malformed registry entry; repair or remove it first" % host)
            if host in live_hosts or (own_rule is not None and _host_of(own_rule) == host and int(mine["port"]) != chosen):
                raise ValueError("hostname %s is already routed by the live cloudflared config %s" % (host, _cfg))
            if not serve.files_auth(root):
                raise ValueError("a public hostname needs a web login first: write user:code to "
                                 ".alpaca/files-auth (mode 0600), then add the hostname")
            running = serve.is_running(root)
            if running and not running.get("remote"):
                raise ValueError("this instance already runs a server without --remote (pid %s, port %s); "
                                 "restart it with `alpaca serve --stop` then `alpaca serve --remote`, then add the hostname"
                                 % (running.get("pid"), running.get("port")))
        if name is None:
            name = (mine or {}).get("name")
        if not name:
            from alpaca import project
            try:
                name = project.load(root).get("name")
            except Exception:
                name = None
            name = name or os.path.basename(root) or "Alpaca"
            name = str(name)[:NAME_MAX]
        item = {"id": ident, "name": valid_name(str(name)), "root": root, "port": chosen, "hostname": host}
        item["href"] = href_of(item)
        entries = sorted(others + [item], key=lambda e: (str(e.get("name")), e["id"]))
        _save(entries + keep_raw, path)
    return {"registry": path, "workspace": item, "added": mine is None,
            "local": "http://127.0.0.1:%d/" % chosen,
            "next": ["bin/alpaca collect services", "bin/alpaca workspace render-ingress --tunnel <name>"]}


def remove(root, ident=None):
    """Drop the entry with this id (default: this instance's), valid or malformed, so a bad entry
    can always be taken out. Every other entry is written back exactly as it was."""
    path = registry_path()
    ident = ident or instance_id(root)
    with _locked(path):
        _entries, _bad, items, _p = _read(path)
        kept = [i for i in items if not (isinstance(i, dict) and i.get("id") == ident)]
        removed = len(kept) != len(items)
        if removed:
            _save(kept, path)
    entries, bad, _items, _p = _read(path)
    return {"registry": path, "removed": ident if removed else None, "workspaces": entries, "invalid": bad}


def listing(root):
    """The registry for `alpaca workspace list`: every valid entry with `stale` (its root no longer
    exists as a directory) and every skipped entry with its reason."""
    entries, bad, _items, path = _read()
    for e in entries:
        e["stale"] = not os.path.isdir(e["root"])
    return {"registry": path, "self": instance_id(root), "workspaces": entries, "invalid": bad}


def default_out(root, tunnel):
    return os.path.join(root, ".alpaca", "services", "cloudflared-%s.yml" % tunnel)


def render(tunnel, *, config=None, tunnel_id=None, credentials=None, entries=None):
    """The combined config text for `tunnel`, emitted by yaml.safe_dump (no hand-built lines and
    no registry text in a comment): the live settings, every live rule unchanged and in order, and
    each not yet routed registered hostname before the live catch-all. The live config must be
    readable."""
    import yaml
    if not isinstance(tunnel, str) or not TUNNEL_RE.fullmatch(tunnel):
        raise ValueError("tunnel must be a name or UUID using letters, digits, dot, underscore or hyphen")
    live_path, live_text, live = read_live(config)
    entries = load() if entries is None else entries
    tunnel_id = tunnel_id or live.get("tunnel")
    if not tunnel_id:
        raise ValueError("no tunnel id: %s names none; pass --tunnel-id" % live_path)
    credentials = credentials or live.get("credentials-file")
    if not credentials:
        raise ValueError("no credentials file: %s names none; pass --credentials-file" % live_path)
    rules, new = plan(live, entries)
    config_out = {"tunnel": str(tunnel_id), "credentials-file": str(credentials)}
    for key, value in live.items():
        if key not in ("tunnel", "credentials-file", "ingress"):
            config_out[key] = value
    config_out.setdefault("protocol", "http2")
    config_out["ingress"] = rules
    text = HEADER + yaml.safe_dump(config_out, default_flow_style=False, sort_keys=False, allow_unicode=False)
    return {"text": text, "live_path": live_path, "live_text": live_text,
            "warnings": shadowed(live, [e["hostname"] for e in new]),
            "carried": [_host_of(r) or r.get("path") or r.get("service") for r in _live_rules(live)],
            "registered": [e["hostname"] for e in entries if e.get("hostname")],
            "new_hosts": [e["hostname"] for e in new]}


def shadowed(live, hosts):
    """One warning per new hostname that a live wildcard rule (for example `*.example.com`)
    matches: cloudflared takes the first matching rule, and the live rules come first."""
    out = []
    for rule in _live_rules(live):
        pattern = _host_of(rule)
        if not (pattern and pattern.startswith("*.")):
            continue
        suffix = pattern[1:]
        for host in hosts:
            if host.endswith(suffix) and host != suffix[1:]:
                out.append("live rule %s (%s) comes first and matches %s: the new rule would never be "
                           "reached; narrow or move the wildcard rule" % (pattern, rule.get("service"), host))
    return out


def render_ingress(root, tunnel, *, out=None, config=None, tunnel_id=None, credentials=None):
    """Write the combined config to `out` and return the diff, DNS commands and copy step.
    Refuses an `out` inside ~/.cloudflared: putting the file there is the owner's step."""
    result = render(tunnel, config=config, tunnel_id=tunnel_id, credentials=credentials)
    out = os.path.abspath(out or default_out(root, tunnel))
    guard = os.path.realpath(os.path.dirname(cloudflared_config_path()))
    live_real = os.path.realpath(result["live_path"])
    real_out = os.path.realpath(out)
    if real_out == live_real or real_out.startswith(guard + os.sep):
        raise ValueError("--out must not be inside %s; copying the rendered file there is the owner's step" % guard)
    write_file(out, result["text"], 0o600)
    diff = "".join(difflib.unified_diff(result["live_text"].splitlines(True), result["text"].splitlines(True),
                                        fromfile=result["live_path"], tofile=out))
    live = result["live_path"]
    dns = ["cloudflared tunnel route dns %s %s" % (tunnel, host) for host in result["new_hosts"]]
    copy = ["cp -p %s %s.bak-$(date +%%Y%%m%%d-%%H%%M%%S)" % (live, live),
            "install -m 600 %s %s" % (out, live),
            "systemctl --user restart <the one unit that runs `cloudflared tunnel run %s`>" % tunnel]
    return {"out": out, "live": live, "diff": diff, "dns": dns, "copy": copy, "warnings": result["warnings"],
            "carried": result["carried"], "registered": result["registered"], "applied": False}


# ---- hub tiles ---------------------------------------------------------------------------------

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse every 3xx: a registered port never hands the hub on to another address."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, "redirect refused", headers, fp)


def _get(port, path, timeout=SUMMARY_TIMEOUT_S):
    """The status of GET http://127.0.0.1:<port><path> with no credential of any kind and no
    redirect followed; None when nothing answered in time."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    request = urllib.request.Request("http://127.0.0.1:%d%s" % (int(port), path), headers={"Accept": "text/html"})
    try:
        with opener.open(request, timeout=timeout) as response:
            response.read(SUMMARY_MAX_BYTES)
            return response.status
    except urllib.error.HTTPError as error:
        return error.code
    except (urllib.error.URLError, OSError, ValueError):
        return None


def remote_status(item, timeout=SUMMARY_TIMEOUT_S):
    """{"reachable": bool} for another registered instance: its public sign-in page `/login`
    answers 200. No stored credential is ever read or sent (a process squatting on the port would
    harvest it), no redirect is followed, and nothing of the other instance's record leaves it."""
    return {"reachable": _get(item["port"], "/login", timeout) == 200}


# ---- CLI ---------------------------------------------------------------------------------------

@cli.command("workspace")
def cmd_workspace(args):
    root = cli._root()
    action = args.workspace_action
    try:
        if action == "add":
            result = add(root, name=args.name, port=args.port, hostname=args.hostname,
                         no_live_config=args.no_live_config)
        elif action == "list":
            result = listing(root)
        elif action == "remove":
            result = remove(root, args.id)
        else:
            result = render_ingress(root, args.tunnel, out=args.out, config=args.config,
                                    tunnel_id=args.tunnel_id, credentials=args.credentials_file)
            for warning in result["warnings"]:
                print("alpaca workspace: WARNING: " + warning)
            print("alpaca workspace: rendered %s (not applied)" % result["out"])
            print("alpaca workspace: diff against the live %s" % result["live"])
            print(result["diff"] or "(no difference)")
            print("alpaca workspace: DNS routes to create (owner runs these, once per new hostname):")
            for line in result["dns"] or ["(none: every registered hostname is already in the live config)"]:
                print("  " + line)
            print("alpaca workspace: apply step (owner decision, never run by alpaca):")
            for line in result["copy"]:
                print("  " + line)
            return cli.PASS
    except ValueError as error:
        print(json.dumps({"error": str(error)}))
        return cli.USAGE
    print(json.dumps(result, indent=2))
    return cli.PASS


def _parser(sub):
    p = sub.add_parser("workspace", help="host workspace registry and the combined tunnel ingress")
    verbs = p.add_subparsers(dest="workspace_action", required=True)
    a = verbs.add_parser("add", help="register this instance in the host registry")
    a.add_argument("--name", help="tile name (default: project name)")
    a.add_argument("--port", type=int, help="local dashboard port (default: registered, else derived; never a reserved port)")
    a.add_argument("--hostname", help="public hostname served through the host tunnel, e.g. name.example.com "
                   "(needs a web login in .alpaca/files-auth)")
    a.add_argument("--no-live-config", action="store_true",
                   help="this host has no cloudflared config yet; render-ingress refuses until it has one")
    verbs.add_parser("list", help="print the host registry")
    r = verbs.add_parser("remove", help="drop this instance (or --id) from the registry")
    r.add_argument("--id", help="instance id to drop (default: this instance)")
    g = verbs.add_parser("render-ingress", help="render one combined cloudflared config; never applies it")
    g.add_argument("--tunnel", required=True, help="existing tunnel name or UUID")
    g.add_argument("--out", help="output file (default: .alpaca/services/cloudflared-<tunnel>.yml)")
    g.add_argument("--config", help="live cloudflared config to read (default: ~/.cloudflared/config.yml)")
    g.add_argument("--tunnel-id", help="tunnel UUID (default: from the live config)")
    g.add_argument("--credentials-file", help="tunnel credentials file (default: from the live config)")


cli.register_parser("workspace", _parser)
