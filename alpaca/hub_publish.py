"""alpaca hub publish: write this workspace's tile to a host hub's drop directory.

A host hub is one page at a host's root hostname that every workspace on the machine publishes
its own tile to (push model: the hub never fetches from a workspace). The hub reads one JSON file
per workspace, `<drop>/<user>--<slug>.json`, owned by `<user>`, at most 16 KiB, schema v1:

    {"v": 1, "user": "...", "slug": "...", "name": "...", "what": "...", "href": "https://...",
     "updated": "ISO-8601 with offset",
     "status": {"headline": "...", "progress": {"done": 0, "total": 0, "label": "..."},
                "counts": {"label": 0, "...": "at most 6"}, "next": "...", "level": "...", "live": 0}}

Caps: name 48, what 120, href 200, headline 200, next 240, level 16, progress label 48, count
label 24. Tile text may not hold control, format, private-use, unassigned or blank-looking filler
characters.

The tile builder is `setup/hub-publish.py`, a standalone script (Python standard library only)
that any member of a machine can copy and run without installing Alpaca; this module loads it, so
`alpaca hub publish` and the script write the same tile from the same code. The tile comes from
this project's own projections, never its record or credentials:
  <root>/data.json   (pulse.now: open operation, rows or tasks done, next action, level, sessions),
                     else
  <root>/board.json  (row counts per column).
A projection that is a symlink or not a regular file (a FIFO, a device, a directory) is refused,
never followed or opened for a blocking read. `href` is checked in full before anything is written
(https only, a DNS hostname, no user info, printable ASCII, at most 200 characters). The tile is
written atomically (a hidden temp file of mode 0640 in the drop, then a rename). Nothing here
opens a network connection.

Settings live in project.yaml under `hub:` (all optional):
    enabled: false                 publish at session end and allow `alpaca hub publish` to write
    drop: /srv/alpaca-hub/tiles    the drop directory ($ALPACA_HUB_DROP overrides it)
    slug: null                     default: the project name in lower case, hyphen-joined
    name: null                     the tile title; default: the project name
    href: null                     the cockpit URL the hub links once its admin approves the host
    what: null                     one line; default "Workspace, published from <projection>"

At session end (alpaca/hooks/session_end.py) the publish first refreshes data.json from the
record, so the tile shows the state the session ended with.

Verbs:
    alpaca hub publish [--print] [--drop DIR]   write the tile (or print it and write nothing); a
                                                relative DIR is read from the folder the command
                                                was run in
    alpaca hub timer                            print a systemd user service and timer (never installs)
"""
import importlib.util
import os
import sys

PACKAGE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CORE_PATH = os.path.join(PACKAGE_ROOT, "setup", "hub-publish.py")
_CORE_NAME = "alpaca._hub_publish_core"


def _core():
    """setup/hub-publish.py as a module (one implementation for the verb and the script). Loaded
    on first use, so importing this module (every CLI start does) never needs the file."""
    module = sys.modules.get(_CORE_NAME)
    if module is not None:
        return module
    spec = importlib.util.spec_from_file_location(_CORE_NAME, CORE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[_CORE_NAME] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(_CORE_NAME, None)
        raise
    return module


#: names read from the script: the schema constants, the checks, the tile builder and writer
_FROM_CORE = frozenset((
    "SCHEMA_V", "CAPS", "USER_RE", "SLUG_RE", "SLUG_MAX", "DEFAULT_DROP", "DROP_ENV",
    "TILE_MAX_BYTES", "MAX_INPUT_BYTES", "HOST_RE", "BAD_CATEGORIES", "INVISIBLE", "PublishError",
    "check_href", "is_bad_char", "clip", "from_data", "from_board", "build", "render",
    "write_tile", "default_slug", "current_user"))


def __getattr__(name):
    if name == "core":
        return _core()
    if name in _FROM_CORE:
        return getattr(_core(), name)
    raise AttributeError("module %r has no attribute %r" % (__name__, name))


def _fail(message):
    raise _core().PublishError(message)


# ---- settings ----------------------------------------------------------------------------------

def settings(root, *, drop=None):
    """The publish settings of the project at `root`, checked in full (PublishError on a bad
    value): {"enabled", "drop", "user", "slug", "name", "href", "what"}. The drop is `drop`, else
    $ALPACA_HUB_DROP, else project.yaml hub.drop, else /srv/alpaca-hub/tiles."""
    from alpaca import project
    cfg = project.load(root)
    hub = cfg.get("hub") or {}
    if not isinstance(hub, dict):
        _fail("project.yaml `hub:` must be a mapping")
    on = hub.get("enabled", False)
    if not isinstance(on, bool):
        _fail("project.yaml hub.enabled must be true or false")
    for key in ("drop", "slug", "name", "href", "what"):
        if hub.get(key) is not None and not isinstance(hub.get(key), str):
            _fail("project.yaml hub.%s must be text" % key)
    core = _core()
    user = core.check_user(core.current_user())
    name = hub.get("name") or cfg.get("name") or os.path.basename(root)
    slug = core.check_slug(hub.get("slug") or core.default_slug(cfg.get("name") or os.path.basename(root)),
                           "hub.slug")
    core.check_name(name, "hub.name")
    href = hub.get("href") or None
    if href:
        core.check_href(href)
    target = core.resolve_drop(drop, hub.get("drop"))
    return {"enabled": on, "drop": target, "user": user, "slug": slug, "name": name,
            "href": href, "what": hub.get("what") or None}


def tile_for(root, conf):
    return _core().build(root, user=conf["user"], slug=conf["slug"], name=conf["name"], href=conf["href"],
                 what=conf["what"])


def publish(root, *, drop=None):
    """Write this workspace's tile to the drop; returns the tile path. Refuses when hub.enabled is
    false."""
    conf = settings(root, drop=drop)
    if not conf["enabled"]:
        _fail("hub.enabled is false in project.yaml; set it to true to publish (--print writes nothing)")
    return _core().write_tile(conf["drop"], tile_for(root, conf))


def enabled(root):
    """True when project.yaml says hub.enabled: true (read without checking the other settings)."""
    from alpaca import project
    hub = project.load(root).get("hub")
    return isinstance(hub, dict) and hub.get("enabled") is True


def at_session_end(root):
    """The session-end publish: nothing at all when hub.enabled is not true, else the tile path.
    The caller (alpaca/hooks/session_end.py) refreshes data.json first and keeps any error away
    from the hook."""
    if not enabled(root):
        return None
    return publish(root)


# ---- systemd timer -----------------------------------------------------------------------------

def timer_units(root, slug, *, interval_s=60):
    """The text of a systemd user service and timer that run `alpaca hub publish` every
    `interval_s` seconds. Printed for the owner to install; never installed here."""
    return _core().timer_units([os.path.join(root, "bin", "alpaca"), ("hub",), ("publish",)], slug,
                            unit="alpaca-hub-publish-%s" % slug, interval_s=interval_s)


# ---- CLI ---------------------------------------------------------------------------------------

def cmd_hub(args):
    from alpaca import cli
    from alpaca import util
    root = cli._root()
    action = getattr(args, "hub_verb", None)
    try:
        core = _core()
    except (OSError, SyntaxError) as error:
        print("GATE alpaca-hub: BLOCKED (cannot load %s: %s)" % (CORE_PATH, error), file=sys.stderr)
        return cli.BLOCKED
    try:
        if action == "publish":
            drop = util.from_caller(args.drop)
            if args.print:
                conf = settings(root, drop=drop)
                print(core.render(tile_for(root, conf)))
                return cli.PASS
            path = publish(root, drop=drop)
            print("published %s" % path)
            return cli.PASS
        if action == "timer":
            conf = settings(root)
            if not conf["enabled"]:
                print("alpaca hub timer: note: hub.enabled is false in project.yaml; the service "
                      "refuses to publish until it is true", file=sys.stderr)
            sys.stdout.write(timer_units(root, conf["slug"]))
            return cli.PASS
    except core.PublishError as error:
        print("GATE alpaca-hub: BLOCKED (alpaca hub publish: %s)" % error, file=sys.stderr)
        return cli.BLOCKED
    except OSError as error:
        print("GATE alpaca-hub: BLOCKED (alpaca hub publish: %s)" % error, file=sys.stderr)
        return cli.BLOCKED
    print("usage: alpaca hub publish [--print] [--drop DIR] | alpaca hub timer", file=sys.stderr)
    return cli.USAGE


def _parser(sub):
    p = sub.add_parser("hub", help="publish this workspace's tile to a host hub's drop directory "
                                   "(settings under hub: in project.yaml)")
    v = p.add_subparsers(dest="hub_verb")
    pub = v.add_parser("publish", help="write <drop>/<user>--<slug>.json atomically from data.json "
                                       "or board.json (needs hub.enabled: true)")
    pub.add_argument("--print", action="store_true", help="write the tile to stdout, not the drop")
    pub.add_argument("--drop", default=None, help="the drop directory (default $ALPACA_HUB_DROP, "
                                                  "else hub.drop, else /srv/alpaca-hub/tiles); a "
                                                  "relative path is read from the folder the command "
                                                  "was run in")
    v.add_parser("timer", help="print a systemd user service and timer that publish every 60 s "
                               "(never installs them)")


def _register():
    from alpaca import cli
    cli.command("hub")(cmd_hub)
    cli.register_parser("hub", _parser)


_register()
