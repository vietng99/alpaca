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

This module is the standalone publisher script of the host hub carried into the product: it
builds the tile the same way (same fields, caps and character rule) from this project's own
projections, never its record or credentials:
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

Verbs:
    alpaca hub publish [--print] [--drop DIR]   write the tile (or print it and write nothing)
    alpaca hub timer [--python PATH]            print a systemd user service and timer (never installs)
"""
import datetime
import errno
import json
import os
import pwd
import re
import stat
import sys
import unicodedata
from urllib.parse import urlsplit

SCHEMA_V = 1
CAPS = {"name": 48, "what": 120, "href": 200, "headline": 200, "label": 48, "next": 240,
        "level": 16, "count_key": 24}
USER_RE = re.compile(r"[a-z_][a-z0-9_.-]{0,31}")
SLUG_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
SLUG_MAX = 40
DEFAULT_DROP = "/srv/alpaca-hub/tiles"
DROP_ENV = "ALPACA_HUB_DROP"
TILE_MAX_BYTES = 16 * 1024
MAX_INPUT_BYTES = 64 * 1024 * 1024
HOST_RE = re.compile(r"(?=.{1,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)(\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+")

# The hub's tile-text rule, kept identical so this publisher never emits text the hub rejects;
# alpaca/tests/test_hub_publish.py checks every code point against it.
BAD_CATEGORIES = {"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"}
INVISIBLE = {0x115F, 0x1160, 0x3164, 0xFFA0, 0x2800, 0x180E}


class PublishError(Exception):
    """A tile that cannot be built or written; the message says why."""


def _fail(message):
    raise PublishError("alpaca hub publish: " + message)


def check_href(href):
    """`href` when the hub will accept it as a link candidate, else PublishError with the reason."""
    def bad(why):
        _fail("href %s" % why)
    if not isinstance(href, str):
        bad("must be text")
    if len(href) > CAPS["href"]:
        bad("is longer than %d characters" % CAPS["href"])
    if any(not 0x21 <= ord(c) <= 0x7e for c in href):
        bad("must be printable ASCII with no spaces or control characters")
    try:
        parts = urlsplit(href)
        port = parts.port
    except ValueError:
        bad("is not a valid URL")
    if parts.scheme != "https":
        bad("must be an https URL")
    if parts.username or parts.password:
        bad("must not carry a user name or password")
    host = (parts.hostname or "").rstrip(".")
    if not HOST_RE.fullmatch(host):
        bad("must name a DNS hostname such as name.example.com")
    if port is not None and not 1 <= port <= 65535:
        bad("has a bad port")
    return href


def is_bad_char(ch):
    """True for a character the hub refuses in tile text."""
    return unicodedata.category(ch) in BAD_CATEGORIES or ord(ch) in INVISIBLE


def clip(value, cap):
    """Single-line text the hub accepts, at most `cap` characters (an ellipsis marks a cut)."""
    text = " ".join(str(value or "").split())
    text = " ".join("".join(ch for ch in text if not is_bad_char(ch)).split())
    if len(text) <= cap:
        return text
    cut = text[:cap - 3].rstrip()
    space = cut.rfind(" ")
    if space > cap // 2:
        cut = cut[:space]
    return cut + "..."


def _count(value):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, min(number, 10 ** 9))


def _load(path):
    """The JSON object in `path`, None when it does not exist or does not parse. A symlink or a
    non-regular file is refused outright (PublishError): the publisher reads only real files."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except OSError as error:
        if error.errno == errno.ELOOP:
            _fail("refusing %s: it is a symlink" % path)
        _fail("cannot open %s: %s" % (path, error.strerror or error))
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            _fail("refusing %s: not a regular file" % path)
        if info.st_size > MAX_INPUT_BYTES:
            return None
        chunks, left = [], MAX_INPUT_BYTES + 1
        while left > 0:
            chunk = os.read(fd, min(left, 1 << 20))
            if not chunk:
                break
            chunks.append(chunk)
            left -= len(chunk)
    finally:
        os.close(fd)
    try:
        data = json.loads(b"".join(chunks).decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def from_data(data):
    """Tile status from a data.json payload (pulse.now), or None when it has no pulse."""
    pulse = data.get("pulse")
    if not isinstance(pulse, dict):
        return None
    now = pulse.get("now") if isinstance(pulse.get("now"), dict) else {}
    tasks = pulse.get("tasks") if isinstance(pulse.get("tasks"), dict) else {}
    ops = [o for o in (now.get("ops") or []) if isinstance(o, dict) and o.get("status") == "open"]
    status = {}
    op = ops[0] if ops else None
    if op:
        title = op.get("title") or op.get("done_when") or ""
        status["headline"] = clip("%s: %s" % (op.get("id"), title) if op.get("id") else title, CAPS["headline"])
        if _count(op.get("rows_total")):
            total, done = _count(op.get("rows_total")), _count(op.get("rows_done"))
            label = "rows discharged"
            if _count(op.get("rows_blocked")):
                label += ", %d blocked" % _count(op.get("rows_blocked"))
        else:
            total, done, label = _count(op.get("tasks_total")), _count(op.get("tasks_done")), "tasks done"
        status["progress"] = {"done": min(done, total), "total": total, "label": clip(label, CAPS["label"])}
    else:
        status["headline"] = "No open operation."
    status["counts"] = {"open ops": _count(pulse.get("ops_open")), "doing": _count(tasks.get("doing")),
                        "queued": _count(tasks.get("open")), "done": _count(tasks.get("done"))}
    if now.get("next_action"):
        status["next"] = clip(now["next_action"], CAPS["next"])
    if now.get("level"):
        status["level"] = clip(now["level"], CAPS["level"])
    sessions = now.get("sessions") if isinstance(now.get("sessions"), dict) else {}
    work = sessions.get("work") if isinstance(sessions.get("work"), list) else []
    status["live"] = sum(1 for s in work if isinstance(s, dict) and s.get("active"))
    return status


def from_board(board):
    """Tile status from a board.json payload (row counts per column)."""
    counts = board.get("counts") if isinstance(board.get("counts"), dict) else {}
    columns = [c for c in (board.get("columns") or list(counts)) if isinstance(c, str)][:6]
    total = sum(_count(counts.get(c)) for c in columns)
    done = _count(counts.get("done"))
    ops = [o for o in (board.get("ops") or []) if isinstance(o, str)]
    status = {"headline": clip(board.get("op") or ("%d operations on the board" % len(ops) if ops else "No rows on the board."),
                               CAPS["headline"]),
              "counts": {clip(c, CAPS["count_key"]): _count(counts.get(c)) for c in columns},
              "live": 0}
    if total:
        label = "rows discharged"
        if _count(counts.get("blocked")):
            label += ", %d blocked" % _count(counts.get("blocked"))
        status["progress"] = {"done": min(done, total), "total": total, "label": label}
    return status


def build(root, *, user, slug, name, href=None, what=None, now=None):
    """The tile for the workspace at `root`, from data.json, else board.json."""
    data = _load(os.path.join(root, "data.json"))
    status = from_data(data) if data else None
    source = "data.json"
    if status is None:
        board = _load(os.path.join(root, "board.json"))
        if board is None:
            _fail("neither %s/data.json nor %s/board.json is readable" % (root, root))
        status = from_board(board)
        source = "board.json"
    moment = now or datetime.datetime.now(datetime.timezone.utc).astimezone()
    tile = {"v": SCHEMA_V, "user": user, "slug": slug, "name": clip(name, CAPS["name"]),
            "updated": moment.isoformat(timespec="seconds"), "status": status}
    tile["what"] = clip(what or "Workspace, published from %s" % source, CAPS["what"])
    if href:
        tile["href"] = check_href(href)
    return tile


def render(tile):
    """The exact bytes written to the drop (and printed by --print, less the final newline)."""
    return json.dumps(tile, ensure_ascii=True, indent=1)


def write_tile(drop, tile):
    """Write the tile atomically: a hidden temp file (mode 0640) in the drop, then rename."""
    final = os.path.join(drop, "%s--%s.json" % (tile["user"], tile["slug"]))
    tmp = os.path.join(drop, ".%s--%s.json.tmp-%d" % (tile["user"], tile["slug"], os.getpid()))
    body = (render(tile) + "\n").encode("ascii")
    if len(body) > TILE_MAX_BYTES:
        _fail("the tile is %d bytes; the hub accepts at most %d" % (len(body), TILE_MAX_BYTES))
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o640)
    try:
        os.fchmod(fd, 0o640)
        os.write(fd, body)
        os.fsync(fd)
        os.close(fd)
        fd = None
        os.replace(tmp, final)
    except BaseException:
        if fd is not None:
            os.close(fd)
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return final


# ---- settings ----------------------------------------------------------------------------------

def default_slug(name):
    """A slug from a project name: lower-case letters and digits joined by single hyphens."""
    slug = "-".join(re.findall(r"[a-z0-9]+", str(name or "").lower()))[:SLUG_MAX].strip("-")
    return slug or "workspace"


def current_user():
    return pwd.getpwuid(os.getuid()).pw_name


def settings(root, *, drop=None):
    """The publish settings of the project at `root`, checked in full (PublishError on a bad
    value): {"enabled", "drop", "user", "slug", "name", "href", "what"}. The drop is `drop`, else
    $ALPACA_HUB_DROP, else project.yaml hub.drop, else /srv/alpaca-hub/tiles."""
    from alpaca import project
    cfg = project.load(root)
    hub = cfg.get("hub") or {}
    if not isinstance(hub, dict):
        _fail("project.yaml `hub:` must be a mapping")
    enabled = hub.get("enabled", False)
    if not isinstance(enabled, bool):
        _fail("project.yaml hub.enabled must be true or false")
    for key in ("drop", "slug", "name", "href", "what"):
        if hub.get(key) is not None and not isinstance(hub.get(key), str):
            _fail("project.yaml hub.%s must be text" % key)
    user = current_user()
    if not USER_RE.fullmatch(user):
        _fail("user %s is not a login name the hub accepts" % user)
    name = hub.get("name") or cfg.get("name") or os.path.basename(root)
    slug = hub.get("slug") or default_slug(cfg.get("name") or os.path.basename(root))
    if not SLUG_RE.fullmatch(slug) or len(slug) > SLUG_MAX:
        _fail("hub.slug must be lower-case letters and digits joined by single hyphens, at most %d" % SLUG_MAX)
    if not clip(name, CAPS["name"]):
        _fail("hub.name is empty after removing non-printable characters")
    href = hub.get("href") or None
    if href:
        check_href(href)
    target = drop or (os.environ.get(DROP_ENV) or "").strip() or hub.get("drop") or DEFAULT_DROP
    return {"enabled": enabled, "drop": target, "user": user, "slug": slug, "name": name,
            "href": href, "what": hub.get("what") or None}


def tile_for(root, conf):
    return build(root, user=conf["user"], slug=conf["slug"], name=conf["name"], href=conf["href"],
                 what=conf["what"])


def publish(root, *, drop=None):
    """Write this workspace's tile to the drop; returns the tile path. Refuses when hub.enabled is
    false."""
    conf = settings(root, drop=drop)
    if not conf["enabled"]:
        _fail("hub.enabled is false in project.yaml; set it to true to publish (--print writes nothing)")
    return write_tile(conf["drop"], tile_for(root, conf))


def at_session_end(root):
    """The session-end publish: nothing at all when hub.enabled is not true, else the tile path.
    The caller (alpaca/hooks/session_end.py) keeps any error away from the hook."""
    from alpaca import project
    hub = project.load(root).get("hub")
    if not (isinstance(hub, dict) and hub.get("enabled") is True):
        return None
    return publish(root)


# ---- systemd timer -----------------------------------------------------------------------------

def _unit_quote(value):
    """systemd quoting: double quotes, with backslash, double quote, percent and dollar escaped."""
    text = str(value).replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$")
    return '"%s"' % text


def timer_units(root, slug, *, interval_s=60):
    """The text of a systemd user service and timer that run `alpaca hub publish` every
    `interval_s` seconds. Printed for the owner to install; never installed here."""
    unit = "alpaca-hub-publish-%s" % slug
    exe = os.path.join(root, "bin", "alpaca")
    return ("# ---- ~/.config/systemd/user/{unit}.service ----\n"
            "[Unit]\n"
            "Description=Publish the {slug} tile to the host hub\n"
            "\n"
            "[Service]\n"
            "Type=oneshot\n"
            "ExecStart={exe} hub publish\n"
            "UMask=0027\n"
            "\n"
            "# ---- ~/.config/systemd/user/{unit}.timer ----\n"
            "[Unit]\n"
            "Description=Publish the {slug} tile to the host hub every {n} s\n"
            "\n"
            "[Timer]\n"
            "OnBootSec=30s\n"
            "OnUnitActiveSec={n}s\n"
            "AccuracySec=5s\n"
            "Persistent=false\n"
            "\n"
            "[Install]\n"
            "WantedBy=timers.target\n"
            "\n"
            "# Install (as yourself): save the two parts as the files named above, then\n"
            "#   systemctl --user daemon-reload && systemctl --user enable --now {unit}.timer\n"
            ).format(unit=unit, slug=slug, exe=_unit_quote(exe), n=int(interval_s))


# ---- CLI ---------------------------------------------------------------------------------------

def cmd_hub(args):
    from alpaca import cli
    root = cli._root()
    action = getattr(args, "hub_verb", None)
    try:
        if action == "publish":
            if args.print:
                conf = settings(root, drop=args.drop)
                print(render(tile_for(root, conf)))
                return cli.PASS
            path = publish(root, drop=args.drop)
            print("published %s" % path)
            return cli.PASS
        if action == "timer":
            conf = settings(root)
            if not conf["enabled"]:
                print("alpaca hub timer: note: hub.enabled is false in project.yaml; the service "
                      "refuses to publish until it is true", file=sys.stderr)
            sys.stdout.write(timer_units(root, conf["slug"]))
            return cli.PASS
    except PublishError as error:
        print("GATE alpaca-hub: BLOCKED (%s)" % error, file=sys.stderr)
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
    pub.add_argument("--drop", default=None, help="the drop directory (default $%s, else hub.drop, "
                                                  "else %s)" % (DROP_ENV, DEFAULT_DROP))
    v.add_parser("timer", help="print a systemd user service and timer that publish every 60 s "
                               "(never installs them)")


def _register():
    from alpaca import cli
    cli.command("hub")(cmd_hub)
    cli.register_parser("hub", _parser)


_register()
