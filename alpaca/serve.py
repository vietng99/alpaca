"""alpaca serve: read-only localhost live dashboard.

Two pages, one server, behind the web sign-in (`alpaca/weblogin.py`, page `/login`) when a login is
configured. `/` is the workspace hub (`alpaca/web/workspaces.html`), whose own-project tile opens the
cockpit at `/hub/` (the operations hub, `alpaca/web/hub.html`): the live mission-control
landing page, which
reads the seven-key `/board/data.json` payload (`alpaca.export.payload`), so every obligation row,
gate run, pulse figure and badge count is read from the record. `/analytics/` is the session
analytics app. The retired classic board addresses (`/board/`, `/board.html`) answer a 301 to
`/hub/`, and the retired single-file pages `/cockpit/legacy/` and `/analytics/legacy/` answer a 301
to `/hub/` and `/analytics/`. A domain profile (alpaca/profile.py) may add read-only routes of its own, for example the
live state of a running job, which is runtime state rather than a fold of the record and so is
served here and never written into `data.json`.

Serves the analytics single-file app plus JSON data and an SSE stream that pushes a
tiny "rev" ping whenever the record (`.alpaca/alpaca.db` + transcripts) or the library of
generated `.html`/`.md` reports changes. The page re-fetches only the part whose rev
moved, so updates are smooth and never a full-page repaint. This process never writes
the record; `alpaca` remains the one writer.

Verbs:
    alpaca serve [--port N]   start in this shell (idempotent: reuse a live server for this project)
    alpaca serve --detach     start in its own session, so it outlives the shell that launched it
    alpaca serve --keep       do not retire the server after an idle spell
    alpaca serve --status     print the running server's URL, uptime and idle rule, or "stopped"
    alpaca serve --stop       stop the running server for this project
"""
import datetime
from contextlib import contextmanager
import getpass
import glob
import hashlib
import http.server
import io
import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
from collections import deque

from alpaca import cli, db, export, paths, render, util
from alpaca.analytics import build_index, metrics_index, ratecards

SPEED_SAMPLES = 4096         # memory guard on the hub speed reads kept (t-590); they are pruned by
                             # age to healthscore.SPEED_WINDOW_S, and a full ring is named in the reason
#: The reader went away mid-response: counted as a disconnect, never as a server failure (t-590).
CLIENT_GONE = (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)
POLL_S = 0.7                 # watcher cadence; also the debounce floor for pushes
FOLD_MIN_S = 60              # the watcher refolds analytics at most this often (a fold reads every
                             # transcript: 16 s of CPU on this record, and live sessions change
                             # its inputs every few seconds, so an unthrottled watcher never rests)
FOLD_STALL_S = 3 * FOLD_MIN_S  # a fold still running after this long reports the projection stale
BOARD_MIN_S = 15             # the watcher rebuilds the status board at most this often; each new
                             # board is a live push that makes open pages re-render, so match the
                             # pages' own 15 s refresh
GAPS_MIN_S = 900             # without a viewer, recompute the index for the pricing gap notice this often
INDEX_VIEW_S = 600           # a viewer's index request keeps the watcher warming the index this long
DATA_WAIT_S = 60             # an analytics request waits this long for the first fold
INFLIGHT_S = 5                # an In-flight snapshot older than this is rebuilt in the background (seconds)
INFLIGHT_MAX_S = 60           # past this age (since its reads began) a reader waits for a fresh In-flight build (seconds)
VIEW_STALE_S = 60             # a research or mission view is served this long after its inputs were read while it rebuilds
VIEW_RETRY_S = 5              # a failed rebuild is not tried again for this long (seconds)
VIEW_KEEP = 32                # research and mission views kept (a run id or a snapshot id each)
OVERVIEW_S = 15               # an overview is reused this long while the record head stays put (seconds)
VCS_WARM_S = 3                 # the start-up Version control build waits this long after the first fold (seconds)
VCS_S = 60                    # a Version control snapshot older than this is rebuilt in the background (seconds)
BURN_STALE_S = 300            # a token board older than this is rebuilt before it is served
BURN_RETRY_S = 5              # a failed background token board build is not retried for this long
BURN_KEEP = 12                # token boards kept: the three windows plus recent custom ranges
RETRY_FOLD_S = 10            # Retry-After for an index request that arrives before the first fold
HEARTBEAT_S = 15             # SSE keep-alive comment interval
IDLE_SHUTDOWN_S = 1800       # exit when no viewer (SSE client) for this long, so servers never orphan
LIB_EXTS = (".html", ".md")
LIB_SKIP = {".alpaca", ".git", "__pycache__", ".pytest_cache", "node_modules", ".pytest_cache", "analytics"}
LIB_MAX = 2000               # cap the walk so a huge tree cannot stall the watcher
READABLE = {".html", ".htm", ".md", ".markdown", ".txt", ".json", ".csv", ".log"}
#: Project directories `/file` never opens, whatever the extension says. `.alpaca/` holds the
#: record and any profile runtime state (run archives carry absolute input paths, the host
#: name, PATH and the dirty-file list, which stay in their directory). `.git/` and `.venv/`
#: are the repository and the interpreter. The refusal compares REAL paths, so a symlink from a
#: served directory cannot reach in.
PRIVATE_DIRS = (".alpaca", ".git", ".venv")
#: The Studio page registry (alpaca/web/pages.json): every hub page and the shell's own files. The
#: asset allowlist, the hub page's stylesheet links and its inline copy of the registry, and the
#: reserved server paths below all come from it, so a new page is one registry entry and its files.
WEB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
_REGISTRY = [None]
_ASSET_TYPES = {".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8", ".png": "image/png",
                ".woff2": "font/woff2", ".json": "application/json", ".webp": "image/webp"}


def registry():
    """alpaca/web/pages.json as a dict, read again when the file changes (as hub.html is)."""
    path = os.path.join(WEB, "pages.json")
    st = os.stat(path)
    key, cached = (st.st_size, st.st_mtime_ns), _REGISTRY[0]
    if cached is None or cached[0] != key:
        with open(path, encoding="utf-8") as fh:
            cached = _REGISTRY[0] = (key, json.loads(fh.read()))
    return cached[1]


def hub_styles(reg=None):
    """The hub page's stylesheets in cascade order: the shell's first ones, each page's in registry
    order, then the shell's last ones (theme.css last of all)."""
    reg = reg or registry()
    return (list(reg["shell"]["css_before"]) + [name for page in reg["pages"] for name in page.get("css", [])]
            + list(reg["shell"]["css_after"]))


def hub_assets(reg=None):
    """{name: content type} of every file /hub/assets/<name> serves: the shell's modules, files and
    Arthur's strips, and every page's modules and stylesheets."""
    reg = reg or registry()
    shell = reg["shell"]
    names = (hub_styles(reg) + list(shell["js"]) + list(shell["files"])
             + ["vendor/arthur/assets/strips/%s.png" % name for name in shell["arthur_actions"]]
             + [name for page in reg["pages"] for name in page.get("js", [])])
    return {name: _ASSET_TYPES[os.path.splitext(name)[1]] for name in names}


def hub_page(root=None):
    """hub.html with the registry filled in at its marker: the stylesheet links in cascade order and an
    inline copy of the registry (pages.js reads it), so the first paint needs no extra request. The
    profile's own stylesheets of the project at `root` (alpaca/profile.py web_meta "styles") link
    last, after theme.css, so a project restyles the Studio without editing alpaca/web. Every
    served page crosses the whole-page ASCII boundary (render.SURFACE_WRITERS)."""
    import html
    reg = registry()
    links = "\n".join(['<link rel="stylesheet" href="/hub/assets/%s">' % name for name in hub_styles(reg)]
                      + ['<link rel="stylesheet" href="%s">' % html.escape(url) for url in _profile_styles(root)])
    copy = json.dumps(reg, ensure_ascii=True, separators=(",", ":")).replace("</", "<\\/")
    with open(os.path.join(WEB, "hub.html"), encoding="utf-8") as fh:
        page = fh.read()
    page = page.replace("<!-- studio:pages -->",
                        links + '\n<script type="application/json" id="studio-pages">' + copy + "</script>", 1)
    # Arthur's scripts and artwork are kept for a day (hub_assets route), so their URLs carry a content
    # version: the scripts here, the artwork through this meta (arthur.js adds it to each image) (t-595).
    version = arthur_version()
    page = re.sub(r'(src="/hub/assets/vendor/arthur/[^"]+\.js)"', lambda m: '%s?v=%s"' % (m.group(1), version), page)
    page = page.replace("</head>", '<meta name="arthur-version" content="%s">\n</head>' % version, 1)
    return render.ascii_entities(page)


def _profile_styles(root):
    """The served URLs of the project's own stylesheets, or [] (no root, no profile, a failed read)."""
    if not root:
        return []
    from alpaca import profile
    try:
        return profile.web_meta(root)["styles"]
    except Exception:
        return []


#: Arthur's actions (alpaca/web/vendor/arthur): package 1's 11, then package 2's
#: 21, as the registry names them. Each has one strip, assets/strips/<action>.png.
ARTHUR_ACTIONS = tuple(registry()["shell"]["arthur_actions"])
_TAGS = {}


def _content_tag(path, body):
    """The sha256 of a static asset's bytes, its ETag. It is kept per file, size and modification
    time, so a reload of unchanged artwork costs no rehash."""
    st = os.stat(path)
    key = (str(path), st.st_size, st.st_mtime_ns)
    tag = _TAGS.get(key)
    if tag is None:
        if len(_TAGS) > 256:
            _TAGS.clear()
        tag = _TAGS[key] = hashlib.sha256(body).hexdigest()
    return tag


def mission_compact(d):
    """What the cockpit's Mission map overview draws (mission.js renderMission compact): each sector's title
    and, per node, its sector, state and linked tasks' id and status, with the plan's task and node states."""
    plan = d.get("development") or {}
    dev = {k: plan[k] for k in ("unavailable",) if k in plan}
    dev["tasks"] = [{"id": t.get("id"), "status": t.get("status")} for t in plan.get("tasks") or []]
    dev["operations"] = [{"groups": [{"nodes": [{k: n.get(k) for k in ("id", "evidence", "state")} for n in g.get("nodes") or []]}
                                     for g in op.get("groups") or []]} for op in plan.get("operations") or []]
    return {"title": d.get("title"), "edges": [], "development": dev,
            "groups": [{"id": g.get("id"), "title": g.get("title")} for g in d.get("groups") or []],
            "nodes": [{"id": n.get("id"), "group": n.get("group"), "state": n.get("state"),
                       "tasks": [{"id": t.get("id"), "status": t.get("status")} for t in n.get("tasks") or []]}
                      for n in d.get("nodes") or []]}


def research_compact(d):
    """What the cockpit's Research runs section draws (research.js renderResearchSection): the runs not
    finished and the first three that ended, each in full, and the count of runs recorded."""
    ended = ("finished", "stopped", "failed")
    runs = d.get("runs") or []
    shown = [r for r in runs if r.get("state") not in ended] + [r for r in runs if r.get("state") in ended][:3]
    return dict(d, runs=shown, recorded=len(runs), sources=[])


#: The cockpit's session panel reads this many sessions' latest ask (Live.session_asks), each cut to
#: this many characters (the panel shows the first line, 200 characters).
SESSION_ASKS = 40
SESSION_ASK_CHARS = 400


def arthur_version():
    """A short tag of Arthur's files under alpaca/web/vendor/arthur: it changes when any of them does.
    _content_tag keeps each file's hash per size and modification time, so a page load rehashes nothing."""
    parts = []
    for path in sorted(glob.glob(os.path.join(WEB, "vendor", "arthur", "**", "*.*"), recursive=True)):
        st = os.stat(path)
        tag = _TAGS.get((path, st.st_size, st.st_mtime_ns))
        if tag is None:
            with open(path, "rb") as fh:
                tag = _content_tag(path, fh.read())
        parts.append(tag)
    return hashlib.sha256("".join(parts).encode("ascii")).hexdigest()[:12]


_VENDOR_LINK = re.compile(rb"((?:/hub/assets/vendor|\./vendor)/)([\w.-]+\.(?:woff2|js))(?=['\"])")


def vendor_links(name, body):
    """The body of a hub stylesheet or module with the vendored fonts and scripts it names given a
    content version (?v=<first 12 of the file's sha256>): hub.css names the fonts and orb.js imports
    thinking-orbs.js, and both are served each load, so a changed vendor file has a new URL (t-595)."""
    if not name.endswith((".css", ".js")) or b"vendor/" not in body:
        return body

    def link(m):
        path = os.path.join(WEB, "vendor", m.group(2).decode("ascii"))
        try:
            with open(path, "rb") as fh:
                tag = _content_tag(path, fh.read())
        except OSError:
            return m.group(0)
        return m.group(0) + b"?v=" + tag[:12].encode("ascii")
    return _VENDOR_LINK.sub(link, body)


def _state_path(root):
    return os.path.join(paths.runtime_dir(root), "serve.json")


def _read_state(root):
    try:
        return json.loads(util.read_text(_state_path(root)))
    except (OSError, ValueError):
        return None


def _pid_alive(pid):
    try:
        os.kill(int(pid), 0)
    except (OSError, ValueError, TypeError):
        return False
    return True


def _port_open(port, host="127.0.0.1"):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.3)
    try:
        return s.connect_ex((host, int(port))) == 0
    finally:
        s.close()


def is_running(root):
    """Return the live server's state dict for this project, or None."""
    st = _read_state(root)
    if st and _pid_alive(st.get("pid")) and _port_open(st.get("port")):
        return st
    return None


def clear_stale_state(root):
    """Remove a `serve.json` left behind by a server that is gone. Returns the removed state.

    A server started from a login shell dies with that shell, and the state file it wrote stays
    on disk naming a pid nobody owns. Every later `--status` then reported a running server that
    answers nothing. Both `--status` and a fresh start call this first, so the record of the
    running server on disk never outlives the process it names.
    """
    st = _read_state(root)
    if st is None or is_running(root):
        return None
    try:
        os.remove(_state_path(root))
    except OSError:
        return None
    return st


def _log_path(root):
    return os.path.join(paths.runtime_dir(root), "serve.log")


def idle_seconds():
    """The idle spell before a server nobody watches retires.

    `ALPACA_SERVE_IDLE_S` overrides it. That override is what lets a test drive the rule both ways
    in a second or two instead of waiting out the half hour the default asks for.
    """
    try:
        return max(1, int(os.environ.get("ALPACA_SERVE_IDLE_S") or IDLE_SHUTDOWN_S))
    except (TypeError, ValueError):
        return IDLE_SHUTDOWN_S


def _spell_words(seconds):
    return "%d s" % seconds if seconds < 60 else "%d min" % (seconds // 60)


def _uptime_words(started):
    """`started` as an ISO stamp read back as plain words, or "unknown" when it will not parse."""
    try:
        began = datetime.datetime.fromisoformat(str(started))
    except (TypeError, ValueError):
        return "unknown"
    now = datetime.datetime.now(began.tzinfo) if began.tzinfo else datetime.datetime.now()
    seconds = int(max(0, (now - began).total_seconds()))
    if seconds < 60:
        return "%d s" % seconds
    if seconds < 3600:
        return "%d min" % (seconds // 60)
    return "%d h %d min" % (seconds // 3600, (seconds % 3600) // 60)


def tunnel_line(port):
    """The exact command a remote reader runs to reach this loopback server from their own
    machine. The server binds 127.0.0.1 and nothing else, so a tunnel is the only way in."""
    try:
        user = getpass.getuser()
    except Exception:
        user = os.environ.get("USER") or os.environ.get("LOGNAME") or "user"
    return "ssh -N -L %d:127.0.0.1:%d %s@%s" % (int(port), int(port), user, socket.gethostname())


def address_lines(url, port):
    """The lines a reader needs: the pages and the tunnel that reaches them. The cockpit is the
    landing page at the root; the session analytics sit beside it."""
    return ["alpaca serve: cockpit %s" % url,
            "alpaca serve: session analytics %sanalytics/" % url,
            "alpaca serve: tunnel from your machine: %s" % tunnel_line(port)]


def _default_port(root):
    """A free port derived from the root, never a reserved one (alpaca/workspace.py RESERVED_PORTS,
    read from $ALPACA_RESERVED_PORTS, empty by default)."""
    from alpaca.workspace import RESERVED_PORTS
    base = 7300 + int(util.sha256_hex(os.path.abspath(root))[:4], 16) % 90
    for off in range(20):
        port = base + off
        if port not in RESERVED_PORTS and not _port_open(port):
            return port
    return base if base not in RESERVED_PORTS else base + 1


def _instance_port(root):
    """This instance's port: its host-registry entry (alpaca workspace add), else _default_port.
    A corrupt registry raises ValueError: a guessed port could be another instance's."""
    from alpaca import workspace
    return workspace.instance_port(root)


def _listed_in_registry_text(root):
    """True when this root's real path appears anywhere in the raw registry file (read as text,
    so a registry too corrupt to parse still answers). Unreadable file: False."""
    from alpaca import workspace
    try:
        with open(workspace.registry_path(), "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return False
    real = os.path.realpath(root)
    return real in text or json.dumps(real)[1:-1] in text


def _public(root, port=None):
    """True when this instance is reachable through the tunnel: the host registry gives it a
    public hostname, or a live cloudflared rule routes to its port. Every start path then runs it
    with --remote behind its sign-in."""
    from alpaca import workspace
    return workspace.public(root, port)


# ---- library scan --------------------------------------------------------

def scan_library(root):
    items = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in LIB_SKIP and not d.startswith(".git")]
        for fn in filenames:
            ext = os.path.splitext(fn)[1].lower()
            if ext not in LIB_EXTS:
                continue
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, root)
            try:
                stat = os.stat(full)
            except OSError:
                continue
            top = rel.split(os.sep)[0] if os.sep in rel else "."
            items.append({"rel": rel.replace(os.sep, "/"), "name": fn, "dir": top.replace(os.sep, "/"),
                          "ext": ext.lstrip("."), "size": stat.st_size, "mtime": int(stat.st_mtime)})
            if len(items) >= LIB_MAX:
                items.sort(key=lambda x: -x["mtime"])
                return items
    items.sort(key=lambda x: -x["mtime"])
    return items


def _lib_sig(items):
    return util.sha256_hex(util.canonical_json([[i["rel"], i["mtime"], i["size"]] for i in items]))


def _content_rev(data):
    """Hash the fold minus the volatile wall-clock `generated` stamp, so a
    content-identical refold yields the same rev and does not push a spurious update."""
    proj = dict(data.get("project") or {})
    proj.pop("generated", None)
    return util.canonical_json({**data, "project": proj})


# ---- data (record + transcripts) ----------------------------------------

def _file_revision(path):
    """A content revision for one small registered input (a config file).

    Hashing the registered input covers an edit the filesystem timestamp cannot tell apart.
    Reserve it for files of a few kilobytes: the watcher reads them every POLL_S.
    """
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def _stat_revision(path):
    """A revision for one transcript: its size and its nanosecond mtime.

    A transcript grows to tens of megabytes and the watcher looks at it every POLL_S, so
    re-hashing the bytes each tick costs more than the whole fold. Size plus the nanosecond
    stamp moves on an append and on an in-place rewrite, which is every way a transcript
    changes; `st_mtime_ns` also tells apart two writes inside one second.
    """
    st = os.stat(path)
    return (st.st_size, st.st_mtime_ns)


def _transcript_inputs(transcripts):
    """Yield every file parsed for each registered transcript session.

    `parse_session.parse` folds `agent-*.jsonl` files under the registered
    transcript's sibling `<sid>/subagents/` tree into the primary session. The
    dashboard revision must track the same input set or a subagent update can
    leave a current-looking aggregate stale.
    """
    for sid, path in sorted(transcripts.items()):
        yield sid, "primary", path
        subagents = os.path.join(os.path.dirname(path), os.path.splitext(os.path.basename(path))[0],
                                 "subagents", "**", "agent-*.jsonl")
        for subagent in sorted(glob.glob(subagents, recursive=True)):
            yield sid, "subagent", subagent


#: Event kinds the overview cache key leaves out, so they do not end a cached overview. They are
#: most of the record's traffic. `heartbeat`: a lease renewal also patches tasks.updated
#: (claims.renew), which stays in the key, and research state folds it to "doing" as the claim
#: before it did. `drain-failed`: the overview lists the newest 20 in capture.errors. The rest are
#: read by no module hub.overview calls (grep of alpaca/ finds them in hooks, the collector, the
#: wiki, research jobs, sessions_view, the serve fold and the history feed's filter only):
#: `transcript-version`, `transcript-snapshot`, `subagent-start`, `subagent-stop`, `turn-end`,
#: `style-rearm`. The fields that do show them, `as_of` (the newest event's time),
#: capture.errors and observability (observability.status reads MAX(id) over all events for each
#: consumer's event_lag), then lag by at most OVERVIEW_S, the ceiling every cached overview
#: already has for inputs outside the record.
OVERVIEW_QUIET_KINDS = ("heartbeat", "drain-failed", "transcript-version", "transcript-snapshot",
                        "subagent-start", "subagent-stop", "turn-end", "style-rearm")


def _overview_head(root):
    """The record head an overview depends on: the event chain (minus OVERVIEW_QUIET_KINDS), tasks,
    ops, messages and decisions. The record is append-only and every other write lands in one of
    these, so an unchanged head means an unchanged overview. None when the record cannot be read
    (the overview is then never reused)."""
    try:
        conn = db.connect_readonly(root)
    except Exception:
        return None
    try:
        # newest first by id, so it stops at the first event that counts
        return tuple(conn.execute(
            "SELECT COALESCE((SELECT id FROM events WHERE kind NOT IN (%s) ORDER BY id DESC LIMIT 1), 0),"
            " (SELECT COUNT(*) FROM tasks),"
            " (SELECT COALESCE(MAX(updated), '') FROM tasks), (SELECT COUNT(*) FROM ops),"
            " (SELECT COALESCE(MAX(id), 0) FROM messages), (SELECT COALESCE(MAX(id), 0) FROM decisions)"
            % ",".join("?" * len(OVERVIEW_QUIET_KINDS)), OVERVIEW_QUIET_KINDS).fetchone())
    except Exception:
        return None
    finally:
        conn.close()


def _data_sig(root):
    """Revision of exactly the inputs consumed by the analytics fold.

    SQLite's main database file can stay unchanged while committed events live in its WAL, so a
    main-file mtime is not a record revision. The event-chain head is durable in the same query
    path that the fold reads. Transcripts are taken by size and nanosecond mtime; the two small
    config files, which a hand edit can rewrite at the same length, are content hashed.
    """
    parts = []
    if os.path.isfile(paths.db_path(root)):
        conn = db.connect_readonly(root)
        try:
            event_head = conn.execute(
                "SELECT COUNT(*), COALESCE(MAX(id), 0), COALESCE(MAX(hash), '') FROM events"
            ).fetchone()
            session_state = conn.execute(
                "SELECT COUNT(*), COALESCE(MAX(sid), ''), COALESCE(MAX(started), ''), "
                "COALESCE(MAX(ended), ''), COALESCE(MAX(last_beat), ''), COALESCE(SUM(beats), 0) "
                "FROM sessions"
            ).fetchone()
            parts.append(("events", tuple(event_head)))
            parts.append(("sessions", tuple(session_state)))
            # The analytics fold consumes per-session operator labels and hook-cost
            # summaries from meta. Hash the durable key/value set so a hook's
            # post-fold timing write triggers the next refresh even without an event.
            parts.append(("meta", [tuple(row) for row in conn.execute(
                "SELECT key, value FROM meta ORDER BY key")]))
            transcripts = build_index._transcripts(root, conn)
            from alpaca.analytics.detail import source_revision
            for sid in sorted(transcripts):
                if db.meta_get(conn, "operator:" + sid) == "codex":
                    parts.append(("codex-capture", sid, source_revision(root, sid)))
        finally:
            conn.close()
    else:
        transcripts = {}
        tdir = paths.transcript_dir(root)
        try:
            for name in os.listdir(tdir):
                if name.endswith(".jsonl"):
                    transcripts[os.path.splitext(name)[0]] = os.path.join(tdir, name)
        except OSError:
            pass
    for sid, role, path in _transcript_inputs(transcripts):
        try:
            rev = _stat_revision(path)
        except OSError:
            rev = None
        parts.append(("transcript", sid, role, os.path.realpath(path), rev))
    # `build_index._fold` loads this project configuration. Include the template
    # instance identity too, because `project.load` overlays it for templates.
    for rel in ("project.yaml", os.path.join(".alpaca", "instance.json")):
        path = os.path.join(root, rel)
        if os.path.isfile(path):
            parts.append(("config", rel, _file_revision(path)))
    return util.sha256_hex(util.canonical_json(parts))


# ---- profile live state (never a projection) ------------------------------

#: The server's own route table: every GET path `_get` answers before the hub's data endpoints. The
#: data endpoints the Studio pages read are named in the registry (pages.json `endpoints`).
SERVER_PATHS = (
    "/", "/index.html", "/login", "/logout", "/workspaces.json", "/workspaces/summary.json",
    "/hub", "/hub/", "/hub/index.html",
    "/analytics", "/analytics.html", "/analytics/", "/analytics/index.html", "/analytics/legacy/",
    "/board", "/board/", "/board.html", "/board/index.html", "/board/data.json",
    "/cockpit", "/cockpit/", "/cockpit/index.html", "/cockpit/legacy/",
    "/files/list", "/files/read", "/data.json", "/health.json", "/library.json", "/events", "/file")
#: every path the server itself answers: the route table plus every registry endpoint. A profile route
#: may never take one of these (nor any path under a reserved prefix): generic routes, and the rules
#: they carry such as the file browser's 403 without a login, always win. A colliding profile route is
#: ignored and reported.
GENERIC_PATHS = frozenset(SERVER_PATHS) | frozenset(
    path for part in [registry()["shell"]] + registry()["pages"] for path in part.get("endpoints", []))
GENERIC_PREFIXES = ("/login/", "/hub/assets/")


def _profile_routes(root):
    """The profile's extra GET routes (alpaca/profile.py `routes`): {path: handler(root, query)},
    without any path the server answers itself (GENERIC_PATHS, GENERIC_PREFIXES). A collision is
    ignored and recorded against the profile's `routes` hook, where the hub and doctor show it."""
    from alpaca import profile
    try:
        routes = profile.load(root).routes() or {}
    except Exception:
        return {}
    if not isinstance(routes, dict):
        return {}
    out, clash = {}, []
    for path, handler in routes.items():
        path = str(path)
        if path in GENERIC_PATHS or path.startswith(GENERIC_PREFIXES) or not path.startswith("/"):
            clash.append(path)
        elif callable(handler):
            out[path] = handler
    if clash:
        profile.note(root, "routes", "ignored route(s) that collide with the server's own: %s"
                     % ", ".join(sorted(clash)))
    return out


def _profile_live_sig(root):
    """The profile's live-state revision (alpaca/profile.py `live_revision`). The watcher carries it
    at its normal cadence and pushes a `job` event the moment it moves."""
    from alpaca import profile
    return str(profile.load(root).live_revision(root) or "")


class FoldPending(Exception):
    """The analytics index was asked for before the first successful fold."""


_NO_VIEW = object()           # a build's outcome when the view does not exist (an unknown run)


class _ViewRun:
    """One running build of a cached view (Live.view): the key it builds for, when that key was read,
    an event set when it ends, and its outcome ((body, etag), _NO_VIEW, or None when it failed)."""

    def __init__(self, key, key_at):
        self.key, self.key_at, self.done, self.outcome = key, key_at, threading.Event(), None


class Live:
    """Latest folded data + library, recomputed by the watcher thread only.

    All sqlite work (the fold) happens on the watcher thread so no connection is
    shared across threads; request handlers only read the cached JSON strings.
    """

    def __init__(self, root):
        self.root = root
        self.lock = threading.Lock()
        self.cond = threading.Condition(self.lock)
        self._foreground_reads = 0
        self.gen = 0
        self.data_json = "{}"
        self.data_rev = ""
        self.lib_json = "[]"
        self.lib_rev = ""
        self.board_json = "{}"
        self.board_rev = ""
        self._online_board_rev = ""
        self.pricing_rev = ""                  # ratecards.revision: recorded rate cards reprice analytics
        self._pricing_sig = None
        self._index = None                     # ((data_rev, pricing_rev), json body, etag) of the index
        self._gaps_at = 0.0                    # when the index (and the pricing gap notice) was last computed
        self._slims = {}                       # kind -> (etag of the full view, body, etag) of the cockpit's slim reads
        self._asks = None                      # (data_rev, json body, etag) of /hub/session-asks.json
        self._index_ready = threading.Event()
        self._index_lock = threading.Lock()    # one index computation at a time; waiters reuse it
        self._burn = {}                        # (window, start, end) -> ((data_rev, pricing_rev, window, start, end, minute), body, etag)
        self._burn_lock = threading.Lock()     # guards _burn and _burn_locks only; a board builds under its key's lock
        self._burn_locks = {}                  # (window, start, end) -> [lock, callers]; one build per key, and a key in use is never evicted
        self._inflight = None                  # [taken at, value, refreshing, retry not before] of the In-flight snapshot
        self._inflight_lock = threading.Lock() # guards _inflight only; a build never runs under it
        self._inflight_build = threading.Lock()  # one synchronous build at a time (first read, or past INFLIGHT_MAX_S)
        self._overview = None                  # (computed at, record head, body, etag, tasks, messages) of /hub/overview.json
        self._overview_lock = threading.Lock()
        self._overview_warm = None             # the background thread computing the next overview
        self._views = {}                       # (view, arg) -> (computed at, key, body, etag): research and mission views
        self._view_locks = {}                  # (view, arg) -> [running builds, callers]; a reader waits only for a build with its key or newer inputs
        self._view_guard = threading.Lock()
        self._view_building = set()            # slots with a background rebuild running
        self._view_retry = {}                  # slot -> monotonic time before which a failed rebuild is not retried
        self._vcs = {}                         # days -> [taken at, value, refreshing, retry not before] of the Version control snapshot
        self._vcs_lock = threading.Lock()     # guards the _vcs table only; a build never runs under it
        self._vcs_build = {}                   # days -> lock held while that window builds (windows do not block each other)
        from alpaca import gitview
        self._vcs_sweep = gitview.Sweeper(self.root)   # keeps worktree statuses fresh while the page is requested (t-592)
        self._warm_thread = None
        self._index_requested_at = 0.0         # last /hub/analytics-index.json request (HTTP handler only)
        self._warm_pending = False             # a warm was due but skipped; the watcher re-checks it
        self._data_sig = None
        self._lib_sig = None
        self._job_sig = None
        self.job_gen = 0
        self.health = {"state": "starting", "detail": "first analytics fold pending"}
        self.data_ready = threading.Event()    # set once the first analytics fold has finished
        self._board_sig = None
        self._fold_at = 0.0
        self._fold_thread = None               # the watcher's fold, one at a time
        self._fold_running = False             # true from the fold's start until its last step
        self._board_at = 0.0
        self.started_at = time.time()
        self.checked_at = None
        self.refreshed_at = None
        self.failures = {}
        self.speed = deque(maxlen=SPEED_SAMPLES)  # (at, seconds) of the page data reads of the last 5 min (t-590)
        self.clients = 0
        self.idle_since = time.time()

    @contextmanager
    def foreground_read(self, path):
        """Give current-work data reads priority over the usage index rebuild."""
        priority = path in ('/hub/overview.json', '/hub/tasks.json', '/hub/messages.json', '/hub/research.json')
        if priority:
            with self.cond:
                self._foreground_reads += 1
        try:
            yield
        finally:
            if priority:
                with self.cond:
                    self._foreground_reads -= 1
                    self.cond.notify_all()

    def yield_analytics(self):
        # Pause between conversations, with a bound so a slow/disconnected
        # page client cannot stop analytics indefinitely.
        with self.cond:
            self.cond.wait_for(lambda: not self._foreground_reads, timeout=2.0)

    def touch(self):
        """Count one viewer request as viewer activity.

        The idle timer exists so a forgotten server never orphans, not so a watched one dies. A
        person may keep only the cockpit open, and it falls back to polling when the event
        stream cannot open, so a poll of the board data, of a profile route or of health counts
        the same as an open stream.
        """
        with self.lock:
            self.idle_since = time.time()

    def client_enter(self):
        with self.lock:
            self.clients += 1

    def client_exit(self):
        with self.lock:
            self.clients -= 1
            if self.clients <= 0:
                self.idle_since = time.time()

    def idle_for(self):
        with self.lock:
            return 0 if self.clients > 0 else time.time() - self.idle_since

    def _board_live(self):
        """The cached record projection with request-time liveness on each work session."""
        from alpaca import sessions_view
        with self.lock:
            board = json.loads(self.board_json)
        current = (board.get("pulse") or {}).get("now") or {}
        now = datetime.datetime.fromtimestamp(time.time(), datetime.timezone.utc).isoformat()
        for session in (current.get("sessions") or {}).get("work") or []:
            session["active"] = sessions_view.is_active(dict(session, **{"class": sessions_view.WORK}), now)
        return board

    def live_work(self):
        """/hub/live-work.json as (body, etag): the work sessions of /board/data.json's pulse, with the
        fields the cockpit's Parallel sessions panel reads, in the same place (pulse.now.sessions.work),
        instead of the whole 770 KB board (t-595)."""
        keep = ("sid", "active", "last_beat", "last_tool", "claims", "lapsed")
        work = (((self._board_live().get("pulse") or {}).get("now") or {}).get("sessions") or {}).get("work") or []
        body = json.dumps({"pulse": {"now": {"sessions": {"work": [{k: s[k] for k in keep if k in s} for s in work]}}}},
                          ensure_ascii=True, sort_keys=True)
        return body.encode("utf-8"), util.sha256_hex(body)

    def board_snapshot(self):
        """Overlay request-time liveness on the immutable cached record projection."""
        board = self._board_live()
        # No volatile clock stamp: a conditional request only changes at a liveness boundary.
        revision = util.sha256_hex(util.canonical_json({key: value for key, value in board.items() if key != "generated"}))
        return json.dumps(board, ensure_ascii=True, sort_keys=True).encode("utf-8"), revision

    def _projection(self):
        with self.lock:
            projection = dict(self.health, checked_at=self.checked_at, refreshed_at=self.refreshed_at)
            failures = json.loads(json.dumps(self.failures))
        if self.checked_at is not None and time.time() - self.checked_at > max(15, POLL_S * 5):
            projection.update(state="stale", detail="dashboard watcher has not checked inputs recently")
        # The fold runs off the watcher thread, so a hung fold no longer stalls the watcher's own
        # check-in; report it here instead.
        if self._folding() and time.time() - self._fold_at > FOLD_STALL_S:
            projection.update(state="stale", detail="analytics fold has run for over %d s" % FOLD_STALL_S)
        return projection, failures

    def health_json(self):
        from alpaca import hub
        capture = hub.capture_health(self.root)
        projection, failures = self._projection()
        # Keep legacy state scoped to projection freshness. Overall status has its own key.
        status = "ok" if projection["state"] == "fresh" and capture.get("status") == "ok" else "degraded"
        return json.dumps({**projection, "status": status, "status_scope": "projection_and_collection",
                           "acceptance": hub.acceptance_health(self.root),
                           "transport": {"status": "live", "started_at": self.started_at},
                           "projection": projection, "observability": capture,
                           "server": {"failures": failures, "since": self.started_at,
                                      "scope": "current_process", "persistent_log": "supervisor_stderr"}}, ensure_ascii=True)

    def note_speed(self, seconds):
        from alpaca.healthscore import SPEED_WINDOW_S
        now = time.time()
        with self.lock:
            self.speed.append((now, seconds))
            while self.speed and now - self.speed[0][0] > SPEED_WINDOW_S:
                self.speed.popleft()

    def vitals_json(self):
        """What the hub's 10 s poll reads (t-590): the connection chip's facts and the health score.
        /health.json keeps the full capture lists; this one counts them, about 1 KB."""
        from alpaca import healthscore, hub
        capture = hub.capture_summary(self.root)
        projection, failures = self._projection()
        with self.lock:
            speed = list(self.speed)
        issues = capture.get("issues") or {}
        coverage = capture.get("coverage") or {}
        value = healthscore.score({"page": projection, "speed": speed, "speed_cap": SPEED_SAMPLES,
                                   "collector": capture.get("collector"),
                                   "backlog": (capture.get("backlog") or {}).get("bytes"),
                                   "issues": issues.get("last_seen"), "issues_open": issues.get("open"),
                                   "failures": failures, "missing_children": coverage.get("missing_children")},
                                  time.time())
        return json.dumps({"projection": projection,
                           "observability": {**capture, "issues": {"open": issues.get("open", 0)}},
                           "score": value}, ensure_ascii=True)

    def failure(self, component, error):
        """Count failures without logging request queries, source payloads or credentials."""
        entry = {"at": time.time(), "type": type(error).__name__}
        with self.lock:
            previous = self.failures.get(component, {})
            self.failures[component] = {"count": previous.get("count", 0) + 1, "last": entry}
        sys.stderr.write("alpaca serve: %s failure (%s)\n" % (component, entry["type"]))

    def _set_health(self, state, detail):
        value = {"state": state, "detail": detail}
        with self.lock:
            if self.health == value:
                return False
            self.health = value
            return True

    def _folding(self):
        return self._fold_running

    def _fold_async(self, dsig):
        """The watcher's fold, off the watcher thread: publish what it changed like the tick would.

        The fold counts as finished (the running mark clears, data_ready sets) only after the last
        step, so a caller that waits on data_ready and refreshes again always gets its fold."""
        try:
            changed, index_moved = self._run_fold(dsig)
            if changed:
                with self.cond:
                    self.gen += 1
                    self.cond.notify_all()
            if index_moved:
                self._warm_index()
        except Exception as error:
            self.failure("projection", error)
        finally:
            self._fold_running = False
            self.data_ready.set()

    def _run_fold(self, dsig):
        """Fold the analytics for input signature dsig; returns (changed, index_moved).
        The caller sets data_ready once everything it does after the fold is done."""
        changed = index_moved = False
        try:
            data = build_index._fold(self.root, build_index.collect(self.root))
            dj = json.dumps(data, ensure_ascii=True)
            rev = util.sha256_hex(_content_rev(data))
            with self.lock:
                if rev != self.data_rev:
                    self.data_json, self.data_rev = dj, rev
                    changed = index_moved = True
            self._data_sig = dsig
            self.refreshed_at = time.time()
            changed = self._set_health("fresh", "analytics fold and status board match current inputs") or changed
        except Exception as e:
            self.failure("projection", e)
            detail = "analytics fold failed: %s" % type(e).__name__
            state = "stale" if self.data_rev else "error"
            changed = self._set_health(state, detail) or changed
            sys.stderr.write("alpaca serve: fold failed: %s\n" % detail)
        return changed, index_moved

    def refresh(self, throttle=False, fold=True):
        """Recompute what changed. The watcher passes throttle=True so the analytics fold runs at
        most every FOLD_MIN_S and the board at most every BOARD_MIN_S; a direct call recomputes
        at once, except that a direct call made while the watcher's fold is still running skips its own
        fold (one fold at a time). fold=False skips the analytics fold (the server's fast first
        refresh)."""
        changed = board_moved = False
        with self.lock:
            self.checked_at = time.time()
        try:
            dsig = _data_sig(self.root)
        except Exception as e:
            self.failure("input_revision", e)
            detail = "dashboard input revision failed: %s; retrying" % type(e).__name__
            state = "stale" if self.data_rev else "error"
            changed = self._set_health(state, detail) or changed
            sys.stderr.write("alpaca serve: %s\n" % detail)
            dsig = None
        with self.lock:
            retry_degraded = self.health.get("state") != "fresh"
        now = time.time()
        index_moved = False
        # A recorded rate card is an event, so the pricing revision can only move when the input
        # signature (which covers the event-chain head) moves. It reprices every session analysis.
        if dsig is not None and dsig != self._pricing_sig:
            # A failed read waits for the next input change rather than retrying every poll.
            self._pricing_sig = dsig
            try:
                revision = ratecards.revision(self.root)
                with self.lock:
                    if revision != self.pricing_rev:
                        self.pricing_rev = revision
                        changed = index_moved = True
            except Exception as e:
                self.failure("pricing", e)
        # The status board first: it is the cockpit's payload and takes a fraction of a second.
        if dsig is not None and (dsig != (self._board_sig or self._data_sig) or not self.board_rev) and (
                not throttle or now - self._board_at >= BOARD_MIN_S):
            try:
                # The status board payload is the same projection `alpaca export` writes,
                # rendered from the record on this watcher thread. Its generation stamp is volatile
                # and stays out of the revision, so an unchanged record pushes nothing.
                conn = db.connect_readonly(self.root)
                try:
                    board = export.payload(conn)
                finally:
                    conn.close()
                bj = json.dumps(board, ensure_ascii=True, sort_keys=True)
                brev = util.sha256_hex(util.canonical_json(
                    {k: v for k, v in board.items() if k != "generated"}))
                with self.lock:
                    if brev != self.board_rev:
                        self.board_json, self.board_rev = bj, brev
                        changed = board_moved = True
                self._board_sig = dsig
                self._board_at = now
            except Exception as e:
                self.failure("projection", e)
                detail = "status board failed: %s" % type(e).__name__
                state = "stale" if self.board_rev else "error"
                changed = self._set_health(state, detail) or changed
                sys.stderr.write("alpaca serve: board failed: %s\n" % detail)
        if fold and dsig is not None and (dsig != self._data_sig or not self.data_rev or retry_degraded) and (
                not throttle or now - self._fold_at >= FOLD_MIN_S) and not self._folding():
            self._fold_at = now
            if throttle:
                # The watcher hands the fold to its own thread (one at a time), so the board,
                # library, profile and liveness steps below keep their cadence while it runs.
                self._fold_running = True
                self._fold_thread = threading.Thread(target=self._fold_async, args=(dsig,),
                                                     name="alpaca-fold", daemon=True)
                self._fold_thread.start()
            else:
                try:
                    fold_changed, fold_index = self._run_fold(dsig)
                finally:
                    self.data_ready.set()
                changed = fold_changed or changed
                index_moved = fold_index or index_moved
        try:
            items = scan_library(self.root)
            lsig = _lib_sig(items)
        except Exception as error:
            self.failure("library", error)
            items, lsig = None, self._lib_sig
        if items is not None and (lsig != self._lib_sig or not self.lib_rev):
            lj = json.dumps(items, ensure_ascii=True)
            rev = util.sha256_hex(lj)
            with self.lock:
                if rev != self.lib_rev:
                    self.lib_json, self.lib_rev = lj, rev
                    changed = True
            self._lib_sig = lsig
        # a profile's live state is not a projection, so it carries its own revision and its own
        # SSE event: the panel reacts when a job starts or finishes without refetching the board.
        try:
            jsig = _profile_live_sig(self.root)
        except Exception as error:
            self.failure("profile_state", error)
            jsig = self._job_sig
        if jsig != self._job_sig:
            self._job_sig = jsig
            with self.cond:
                self.job_gen += 1
                self.cond.notify_all()
        # The browser can remain on SSE without polling. A liveness expiry must push
        # a board revision even when the underlying record and its export never changed.
        online_revision = self.board_snapshot()[1]
        if online_revision != self._online_board_rev:
            self._online_board_rev = online_revision
            changed = True
        if changed:
            with self.cond:
                self.gen += 1
                self.cond.notify_all()
        if index_moved or self._warm_pending:
            self._warm_index()
        if board_moved and self.clients:
            self._warm_overview()

    def _warm_overview(self):
        """Compute the next overview on a background thread when the board moves and a viewer is
        connected, so the page's own read after the push finds it ready. One at a time."""
        if self._overview_warm is not None and self._overview_warm.is_alive():
            return

        def run():
            try:
                self.overview()
            except Exception as error:
                self.failure("overview", error)
        self._overview_warm = threading.Thread(target=run, name="alpaca-overview-warm", daemon=True)
        self._overview_warm.start()

    def note_index_request(self):
        """The HTTP handler records that a viewer asked for the index; only such a request makes
        the watcher warm it for viewers (a cockpit-only viewer never pays for it)."""
        with self.lock:
            self._index_requested_at = time.time()

    def analytics_snapshot(self):
        """Serve completed measurements while one background rebuild updates them.

        A cold build is explicit (202), never an empty zero-usage index. Cached
        measurements retain their body/etag and get a refreshing header until
        the current data and pricing revisions have completed.
        """
        with self.lock:
            key = (self.data_rev, self.pricing_rev)
            cached = self._index
        if not key[0]:
            raise FoldPending("first analytics fold pending")
        if cached is not None and cached[0] == key:
            return cached[1], cached[2], 200, "fresh"
        self._warm_index(force=True)
        if cached is not None:
            return cached[1], cached[2], 200, "refreshing"
        self._index_ready.wait(0.5)
        with self.lock:
            cached = self._index
            current = (self.data_rev, self.pricing_rev)
        if cached is not None:
            return cached[1], cached[2], 200, "fresh" if cached[0] == current else "refreshing"
        if self._index_ready.is_set():
            raise FoldPending("analytics build failed; retry after checking capture health")
        return json.dumps({"state": "building", "message": "Preparing session measurements.",
                           "retry_after": 2}), None, 202, "building"

    def session_asks(self):
        """/hub/session-asks.json as (body, etag): the cockpit's Parallel sessions panel reads a
        title and the latest ask of each live session, so it takes every session of the board's work
        list first, then the most recent ones of the fold up to SESSION_ASKS, with those two fields
        instead of the whole 830 KB /data.json (t-595). Kept for the current (data_rev, board_rev)."""
        with self.lock:
            key, data_json, board_json, cached = (self.data_rev, self.board_rev), self.data_json, self.board_json, self._asks
        if cached is not None and cached[0] == key:
            return cached[1], cached[2]
        work = (((json.loads(board_json).get("pulse") or {}).get("now") or {}).get("sessions") or {}).get("work") or []
        live = {s.get("sid") for s in work}
        recent = sorted(json.loads(data_json).get("sessions", []), key=lambda s: s.get("last") or "", reverse=True)
        rows = []
        for s in [s for s in recent if s.get("sid") in live] + [s for s in recent if s.get("sid") not in live][:SESSION_ASKS]:
            asks = [a for a in (s.get("human_prompts") or [])[-1:] if isinstance(a, dict)]
            rows.append({"sid": s.get("sid"), "title": s.get("title"),
                         "human_prompts": [{"ts": a.get("ts"), "text": str(a.get("text") or "")[:SESSION_ASK_CHARS]} for a in asks]})
        body = json.dumps({"sessions": rows}, ensure_ascii=True)
        etag = util.sha256_hex(body)
        with self.lock:
            self._asks = (key, body, etag)
        return body, etag

    def analytics_index(self):
        """The project analytics index as (JSON body, etag), computed once per revision pair.

        metrics_index.index analyzes every work session (seconds on a large record), and the
        index changes only with the analytics fold or the recorded rate cards. The result is kept
        for the current (data_rev, pricing_rev); concurrent requests wait on one computation.
        """
        with self._index_lock:
            with self.lock:
                key = (self.data_rev, self.pricing_rev)
                cached = self._index
                data_json = self.data_json
            if not key[0]:
                # No successful fold yet (still running, or failed and set data_ready): an index
                # over no sessions would read as "0 sessions" and would erase the gap notice.
                raise FoldPending("first analytics fold pending")
            if cached is not None and cached[0] == key:
                return cached[1], cached[2]
            sessions = json.loads(data_json).get("sessions", [])
            value = metrics_index.index(self.root, sessions, yield_control=self.yield_analytics)
            body = json.dumps(value, ensure_ascii=True)
            etag = util.sha256_hex(body)
            with self.lock:
                self._index = (key, body, etag)
                self._gaps_at = time.time()
            try:
                # The pricing gap notice the next session start reads (models without a rate card).
                # With no gap and no notice yet there is nothing to say, so a read creates no file.
                gaps = value.get("totals", {}).get("unpriced_models") or []
                if gaps or os.path.isfile(ratecards.gaps_path(self.root)):
                    ratecards.write_gaps(self.root, gaps, scope="work")
            except Exception as error:
                self.failure("pricing_gaps", error)
            return body, etag

    def overview(self):
        """/hub/overview.json as (body, etag). Every page reads it and one computation takes about a
        second on a large record (task contracts, proof gates, capture health), so viewers share one
        while the record head (_overview_head) stays put, for at most OVERVIEW_S so inputs outside
        the record (reports, receipts, leases running out) still refresh. A read that arrives while
        the value is computed waits for it and takes it when the head still matches, so a flood of
        events the key ignores (OVERVIEW_QUIET_KINDS) never makes it compute twice."""
        return self._overview_entry()[2:4]

    def _overview_entry(self):
        from alpaca import hub
        head = _overview_head(self.root)
        with self._overview_lock:
            cached = self._overview
            if cached is not None and head is not None and cached[1] == head and time.time() - cached[0] < OVERVIEW_S:
                return cached
            full = hub.overview(self.root)
            # each task carries its text_rev in both bodies, so the page keys a task's text on it (t-591)
            tasks = [{**t, "text_rev": hub.text_rev(t)} for t in full.get("tasks", [])]
            # compact separators: the page body is read by every tab (t-591)
            body = json.dumps(hub.page_overview({**full, "tasks": tasks}), ensure_ascii=True,
                              separators=(",", ":")).encode("utf-8")
            etag = util.sha256_hex(body)
            self._overview = (time.time(), head, body, etag, tasks, full.get("messages", []))
            return self._overview

    def work_view(self, name, ids=None):
        """/hub/tasks.json (every task whole, or the comma-separated `ids`) or /hub/messages.json as
        (body, etag), cut from the last overview computation. A drawer open never recomputes it: the
        page's own refresh does, and the text_rev on each task tells the page which text is current
        (t-591). Only a server that has computed no overview yet computes one here."""
        entry = self._overview or self._overview_entry()
        if name == "tasks":
            want = {i for i in (ids or "").split(",") if i}
            value = {"tasks": [t for t in entry[4] if t["id"] in want] if ids else entry[4]}
        else:
            value = {"messages": entry[5]}
        body = json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
        return body, util.sha256_hex(body)

    def view(self, name, arg, key, compute):
        """A cached JSON view (/hub/research.json, /hub/mission.json) as (body, etag), or None when
        `compute` returns None (an unknown run, never cached).

        `key` is a function returning the view's key: the record head from _overview_head plus what
        the view reads outside the record (None when the record cannot be read); a constant stands in
        for a function in tests. view() notes the time just before it calls the function (key_at), so
        a key is ordered by when it was really read, and takes the age of a body after the function
        returns. A stored body is (build start, key, body, etag, key_at of its key) and a body is
        never stored over one whose key was read later.

        At most one compute runs per slot at any time, foreground or background.
          * A body whose key is unchanged is served as is while it is under OVERVIEW_S old.
          * Aged only (key unchanged, up to VIEW_STALE_S after its inputs were read): the body is
            served at once and a background build is started if none is running. A build that fails
            keeps the old body and is not tried again for VIEW_RETRY_S.
          * Key moved, no body yet, past VIEW_STALE_S or a key of None: the reader needs a build that
            read its key at or after the reader's own read. If one is running it waits for it and
            takes its body when that holds (or when the build has the reader's key, however long it
            ran). Otherwise the first reader to find none running starts the next build, which reads
            the key again at its own start, and every reader waiting by then takes its result. Readers
            that arrive during that build wait for the one after it: one running and one pending.
        The worst case for a moved-key reader is the rest of the running build plus one build; a
        failure in a build raises to the reader that ran it, and the others go on to run their own."""
        slot = (name, arg)
        key_fn = key if callable(key) else (lambda: key)
        key_at = time.monotonic()
        key = key_fn()
        now = time.monotonic()
        with self._view_guard:
            cached = self._views.get(slot)
            if cached is not None and key is not None and cached[1] == key:
                if now - cached[0] < OVERVIEW_S:
                    return cached[2], cached[3]
                if now - cached[0] < VIEW_STALE_S:
                    self._view_start_refresh(slot, name, now, key_fn, compute)
                    return cached[2], cached[3]
            held = self._view_hold(slot)
        waited = False
        try:
            while True:
                with self._view_guard:
                    cached = self._views.get(slot)
                    if cached is not None and (cached[4] >= key_at or (
                            key is not None and cached[1] == key and time.monotonic() - cached[0] < OVERVIEW_S)):
                        return cached[2], cached[3]
                    running = held[0][0] if held[0] else None
                    if running is None:
                        mine = _ViewRun(key, key_at)
                        held[0].append(mine)
                if running is None:
                    return self._view_run(slot, held, mine, compute, key_fn if waited else None)
                running.done.wait()            # a build with inputs as new as ours: take its body or go again
                waited = True
                if running.outcome is not None and (running.key_at >= key_at or (key is not None and running.key == key)):
                    return None if running.outcome is _NO_VIEW else running.outcome
        finally:
            self._view_release(slot, held)

    def _view_run(self, slot, held, run, compute, key_fn=None):
        """Compute and store the slot's view for `run` (registered in held[0]); wakes its waiters.
        With key_fn the key is read again first (a build that follows another reads it at its start)."""
        try:
            if key_fn is not None:
                at = time.monotonic()
                key = key_fn()
                with self._view_guard:
                    run.key, run.key_at = key, at
            started = time.monotonic()         # the age bound runs from when the inputs were read
            value = compute()
            if value is None:
                with self._view_guard:
                    cached = self._views.get(slot)
                    if cached is not None and cached[4] <= run.key_at:
                        del self._views[slot]
                        self._view_retry.pop(slot, None)
                run.outcome = _NO_VIEW
                return None
            body = json.dumps(value, ensure_ascii=True).encode("utf-8")
            etag = util.sha256_hex(body)
            with self._view_guard:
                cached = self._views.get(slot)
                if cached is not None and cached[4] > run.key_at:
                    run.outcome = (cached[2], cached[3])   # a body whose key was read later stays
                    return run.outcome
                self._views[slot] = (started, run.key, body, etag, run.key_at)
                self._view_retry.pop(slot, None)
                # evict the oldest slot nobody is using; a slot being computed or waited on stays
                while len(self._views) > VIEW_KEEP:
                    idle = [k for k in self._views if self._view_locks[k][1] == 0]
                    if not idle:
                        break
                    stale = min(idle, key=lambda k: self._views[k][0])
                    del self._views[stale]
                    del self._view_locks[stale]
                    self._view_retry.pop(stale, None)
            run.outcome = (body, etag)
            return run.outcome
        finally:
            with self._view_guard:
                held[0].remove(run)
            run.done.set()

    def _view_start_refresh(self, slot, name, now, key_fn, compute):
        """Start the slot's background build unless a build runs or a failed one is backing off. Call
        with _view_guard held. A thread that cannot start leaves the slot as it was."""
        entry = self._view_locks.get(slot)
        if (entry is not None and entry[0]) or slot in self._view_building or now < self._view_retry.get(slot, 0.0):
            return
        held = self._view_hold(slot)
        run = _ViewRun(None, now)              # reads its key when it starts (_view_run)
        held[0].append(run)
        self._view_building.add(slot)
        try:
            threading.Thread(target=self._view_refresh, args=(slot, held, run, key_fn, compute),
                             name="view-%s" % name, daemon=True).start()
        except Exception as error:
            held[0].remove(run)
            run.done.set()
            self._view_building.discard(slot)
            held[1] -= 1
            self.failure("view_refresh", error)

    def _view_hold(self, slot):
        """The slot's [running builds (at most one), callers], counting one more caller. Call with
        _view_guard held."""
        held = self._view_locks.setdefault(slot, [[], 0])
        held[1] += 1                           # callers waiting on or running this slot
        return held

    def _view_release(self, slot, held):
        with self._view_guard:
            held[1] -= 1
            if held[1] == 0 and slot not in self._views:
                self._view_locks.pop(slot, None)   # nothing stored: no entry left to grow the table

    def _view_refresh(self, slot, held, run, key_fn, compute):
        """The background build behind an aged body. It reads the key when it starts, so its body carries
        the key then current. A failure is recorded and keeps the old body."""
        try:
            self._view_run(slot, held, run, compute, key_fn)
        except Exception as error:
            with self._view_guard:
                if slot in self._views:
                    self._view_retry[slot] = time.monotonic() + VIEW_RETRY_S
            self.failure("view_refresh", error)
        finally:
            with self._view_guard:
                self._view_building.discard(slot)
            self._view_release(slot, held)

    def research_view(self, run):
        """/hub/research.json (the run list, or one run) through view(): keyed on the record head and
        the stamps research.view.source_stamp takes."""
        from alpaca.research import view as research_view

        def key():
            head = _overview_head(self.root)
            return None if head is None else (head, research_view.source_stamp(self.root))
        return self.view("research", run, key, lambda: (
            research_view.run(self.root, run) if run else research_view.runs(self.root)))

    def slim(self, kind, got, project):
        """A cockpit projection of a cached view (body, etag), built once per revision of the view: the
        cockpit draws a fraction of /hub/mission.json and /hub/research.json, so it reads this instead
        and the other pages keep the full reads (t-595)."""
        body, etag = got
        with self.lock:
            cached = self._slims.get(kind)
        if cached is not None and cached[0] == etag:
            return cached[1], cached[2]
        out = json.dumps(project(json.loads(body)), ensure_ascii=True, separators=(",", ":"))
        tag = util.sha256_hex(out)
        with self.lock:
            self._slims[kind] = (etag, out.encode("utf-8"), tag)
        return out.encode("utf-8"), tag

    def mission_view(self, at):
        """/hub/mission.json through view(): keyed on the record head (a new mission check, link,
        snapshot or task change moves it) and project.yaml. The source files the map observes are not
        in the key: a change only to them reaches readers through the background rebuild that starts
        once the body is OVERVIEW_S old, so within VIEW_STALE_S."""
        from alpaca import mission, project

        def key():
            head = _overview_head(self.root)
            if head is None:
                return None
            try:
                st = os.stat(project.path(self.root))
                return (head, (st.st_size, st.st_mtime_ns, st.st_ino))
            except OSError:
                return (head, None)
        return self.view("mission", at, key, lambda: mission.project(self.root, at=at))

    def inflight(self):
        """The In-flight snapshot (alpaca.analytics.inflight), served stale-while-revalidate like vcs().

        The first read builds it. After that the last snapshot is returned at once; once it is older than
        INFLIGHT_S one background thread rebuilds it (a failed rebuild keeps the old one and backs off 5 s).
        A snapshot older than INFLIGHT_MAX_S, counted from when its reads began, is never served: that
        reader waits for a build. So a viewer polling every 15 s sees data at most one poll behind in
        normal running (about INFLIGHT_S + 15 s) and never older than INFLIGHT_MAX_S.
        Transcripts are read incrementally between snapshots."""
        with self._inflight_lock:
            entry = self._inflight
            if entry is not None:
                age = time.time() - entry[0]
                if age < INFLIGHT_MAX_S:
                    if age >= INFLIGHT_S and time.time() >= entry[3] and not entry[2]:
                        entry[2] = True
                        threading.Thread(target=self._inflight_refresh, args=(entry,), daemon=True,
                                         name="inflight-refresh").start()
                    return entry[1]
        from alpaca.analytics import inflight
        with self._inflight_build:            # first build, or past the age cap: readers share one read
            with self._inflight_lock:
                entry = self._inflight
                if entry is not None and time.time() - entry[0] < INFLIGHT_MAX_S:
                    return entry[1]
            taken = time.time()
            value = inflight.snapshot(self.root)
            with self._inflight_lock:
                self._inflight = [taken, value, False, 0.0]
            return value

    def _inflight_refresh(self, entry):
        """Rebuild `entry`, the snapshot this refresh was started from. A reader past the age cap may
        have replaced it with a newer synchronous build meanwhile; that one is left as it is."""
        from alpaca.analytics import inflight
        taken = time.time()
        try:
            value = inflight.snapshot(self.root)
        except Exception:
            value = None                      # keep serving the old snapshot; the next call tries again
        with self._inflight_lock:
            if self._inflight is entry:
                if value is not None:
                    entry[0], entry[1], entry[3] = taken, value, 0.0
                else:
                    entry[3] = time.time() + 5
            entry[2] = False

    def vcs(self, days):
        """The Version control snapshot (alpaca.gitview), served stale-while-revalidate (t-585).

        The first call for a window builds it; after that the last snapshot is returned at once and, once it
        is older than VCS_S, one background thread per window rebuilds it. A failed rebuild keeps the old one."""
        from alpaca import gitview
        if days not in (1, 7, 30, 90):
            raise ValueError("days must be 1, 7, 30 or 90")
        with self._vcs_lock:
            entry = self._vcs.get(days)
            build = self._vcs_build.setdefault(days, threading.Lock())
            if entry is not None:
                if time.time() - entry[0] >= VCS_S and time.time() >= entry[3] and not entry[2]:
                    entry[2] = True
                    threading.Thread(target=self._vcs_refresh, args=(days,), daemon=True, name="vcs-refresh").start()
                return dict(entry[1], snapshot_at=entry[0], refreshing=entry[2])
        with build:                           # first build: viewers of this window wait for one read
            with self._vcs_lock:
                entry = self._vcs.get(days)
            if entry is None:
                taken = time.time()                # snapshot_at: when the git reads began
                value = gitview.snapshot(self.root, days, opened=self._vcs_sweep.opened_paths())
                entry = [taken, value, False, 0.0]
                with self._vcs_lock:
                    self._vcs[days] = entry
            return dict(entry[1], snapshot_at=entry[0], refreshing=entry[2])

    def vcs_touch(self, value=None):
        """A Version control request: keep the status sweeper running (t-592). `value` is a snapshot, whose live
        sessions tell the sweeper which worktrees to read every tick."""
        from alpaca import gitview
        live = gitview.live_paths(value["worktrees"]) if value and value.get("worktrees") else None
        self._vcs_sweep.touch(live)

    def vcs_status(self):
        """/hub/vcs-status.json: the cached status rows of the worktrees the sweeper keeps live (the watched set and
        those opened on demand), and the overall line over every worktree. Nothing is read here."""
        from alpaca import gitview
        trees = gitview.worktrees(self.root)
        watch = self._vcs_sweep.select(trees)
        rows = gitview.status_rows(self.root, trees)
        return {"generated": time.time(), "worktrees": {p: r for p, r in rows.items() if r and p in watch},
                "overall": gitview.overall(rows, watch), "hot_backlog": self._vcs_sweep.backlog}

    def vcs_tree(self, path):
        """/hub/vcs-tree.json: one worktree read now and kept live for the next SWEEP_IDLE_S (the viewer's "Load
        status"). None when `path` is not exactly one of the repository's worktree paths; nothing is read then."""
        from alpaca import gitview
        trees = gitview.worktrees(self.root)
        if not path or not any(w["path"] == path for w in trees):
            return None
        self._vcs_sweep.open(path)
        return gitview.tree_row(self.root, path, trees)

    def warm_vcs(self, days=7):
        """Build the page's default Version control window in the background after a start (t-585), at low
        priority and after the first fold, so the first page loads do not compete with it. It goes through
        vcs(), so a request that arrives during the warm waits on the same build lock and shares its result."""
        def run():
            self.data_ready.wait(DATA_WAIT_S * 4)
            time.sleep(VCS_WARM_S)
            try:
                os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), 10)    # git children inherit it
            except (OSError, AttributeError):
                pass
            try:
                self.vcs(days)
            except Exception:
                pass                          # the first request builds it and reports the error
        thread = threading.Thread(target=run, daemon=True, name="vcs-warm")
        thread.start()
        return thread

    def _vcs_refresh(self, days):
        from alpaca import gitview
        taken = time.time()
        try:
            value = gitview.snapshot(self.root, days, opened=self._vcs_sweep.opened_paths())
        except Exception:
            value = None                      # keep serving the old snapshot; the next call tries again
        with self._vcs_lock:
            entry = self._vcs[days]
            if value is not None:
                entry[0], entry[1] = taken, value
            else:
                entry[3] = time.time() + 5             # back off 5 s before the next attempt
            entry[2] = False

    def burn(self, window, start=None, end=None):
        """The Sessions tab token and cost board (alpaca.analytics.burn) as (JSON body, etag).

        Served stale-while-revalidate (t-583). The first request for a window (or range) builds it
        and waits. After that the last board is returned at once; when its inputs moved (the fold,
        the pricing revision, or the minute a rolling window is cut at) one background thread per
        key builds the next, a request during that build gets the old board, and a failed build
        keeps the old board and is retried after BURN_RETRY_S. A board older than BURN_STALE_S is
        not served stale: that request waits for a fresh one, and while builds keep failing it
        gets the error (backed off BURN_RETRY_S between attempts), not the old board. So a viewer sees a board at most one
        minute plus one build old while the page is polled, and never more than BURN_STALE_S.
        While a rebuild runs the body carries "refreshing": true ("generated" is the build time).
        A custom range (start and end from the browser) replaces the window; a bad one is a
        ValueError (400) before any work. Only the last BURN_KEEP boards are kept, since ranges
        are open-ended. Different windows build side by side.
        """
        from alpaca.analytics import burn as burn_board
        if start or end:
            burn_board.custom_range(start, end, time.time())
            window = "range"
        elif window not in burn_board.WINDOWS:
            raise ValueError("unknown window; use one of %s" % ", ".join(burn_board.WINDOWS))
        which = (window, start or None, end or None)
        key = self._burn_key(which)
        now = time.time()
        with self._burn_lock:
            entry = self._burn.get(which)
            if entry is not None and now - entry[3] < BURN_STALE_S:
                if entry[0] != key and not entry[4] and now >= entry[5]:
                    entry[4] = True
                    self._burn_locks.setdefault(which, [threading.Lock(), 0])[1] += 1    # the thread is a caller
                    threading.Thread(target=self._burn_refresh, args=(which, entry), daemon=True, name="burn-refresh").start()
                return self._burn_served(entry)
            held = self._burn_locks.setdefault(which, [threading.Lock(), 0])
            held[1] += 1                        # callers waiting on or running this key: its lock is not dropped
        try:
            with held[0]:                       # a first (or too old) board: viewers of this key wait for one build
                key = self._burn_key(which)
                with self._burn_lock:
                    entry = self._burn.get(which)
                    if entry is not None:
                        if entry[0] == key or time.time() - entry[3] < BURN_STALE_S:
                            return self._burn_served(entry)
                        if time.time() < entry[5]:
                            # backing off after a failed build: say so, not a stale board. A new exception each
                            # time, so the stored one's traceback does not grow with every request.
                            raise RuntimeError("the last board build failed: %s" % entry[6]) from None
                try:
                    entry = self._burn_build(which, key)
                except Exception as exc:
                    with self._burn_lock:
                        old = self._burn.get(which)
                        if old is not None:
                            old[5], old[6] = time.time() + BURN_RETRY_S, exc
                    raise
                return self._burn_served(entry)
        finally:
            self._burn_release(which)

    def _burn_release(self, which):
        with self._burn_lock:
            held = self._burn_locks.get(which)
            if held is not None:
                held[1] -= 1
                if held[1] == 0 and which not in self._burn:
                    del self._burn_locks[which]      # nothing stored: no lock left to grow the table

    def _burn_key(self, which):
        with self.lock:
            key = (self.data_rev, self.pricing_rev) + which + (int(time.time() // 60),)
        if not key[0]:
            raise FoldPending("first analytics fold pending")
        return key

    @staticmethod
    def _burn_served(entry):
        if not entry[4] and not entry[5] > time.time():     # not rebuilding, and no retry pending after a failed build
            return entry[1], entry[2]
        body = entry[1][:-1] + ', "refreshing": true}'
        return body, util.sha256_hex(body)

    def _burn_build(self, which, key):
        """Compute the board for `which` and store it; returns the stored entry
        [key, body, etag, built at, refreshing, retry not before, last error]."""
        from alpaca.analytics import burn as burn_board
        window, start, end = which
        with self.lock:
            data_json = self.data_json
        sessions = json.loads(data_json).get("sessions", [])
        built = time.time()
        if window == "range":
            value = burn_board.board(self.root, sessions, start=start, end=end)
        else:
            value = burn_board.board(self.root, sessions, window)
        body = json.dumps(value, ensure_ascii=True)
        entry = [key, body, util.sha256_hex(body), built, False, 0.0, None]
        with self._burn_lock:
            self._burn.pop(which, None)
            self._burn[which] = entry
            # evict the oldest board nobody is building or waiting on; a key in use stays
            while len(self._burn) > BURN_KEEP:
                idle = [k for k in self._burn if k != which and self._burn_locks.get(k, (None, 0))[1] == 0]
                if not idle:
                    break
                stale = min(idle, key=lambda k: self._burn[k][3])
                del self._burn[stale]
                self._burn_locks.pop(stale, None)
        return entry

    def _burn_refresh(self, which, mine):
        """The background rebuild of one key (the caller set `mine`'s refreshing flag and counted this
        thread as a caller); a failure keeps the old board and backs off. Only `mine` is touched."""
        try:
            with self._burn_locks[which][0]:
                self._burn_build(which, self._burn_key(which))
        except Exception as exc:
            with self._burn_lock:
                mine[5], mine[6] = time.time() + BURN_RETRY_S, exc
        finally:
            with self._burn_lock:
                mine[4] = False
            self._burn_release(which)

    def _warm_index(self, *, force=False):
        """Recompute a stale index off the watcher thread after the fold or the pricing moved.

        Two reasons to warm: a viewer is connected and asked for the index within INDEX_VIEW_S,
        or GAPS_MIN_S passed since the last computation, so the pricing gap notice stays current
        for the next session start without a viewer. Never before the first successful fold. A
        warm that is due but skipped (no reason yet, or one already running) stays pending and
        the watcher re-checks it on every tick.
        """
        now = time.time()
        with self.lock:
            key = (self.data_rev, self.pricing_rev)
            stale = bool(key[0]) and (self._index is None or self._index[0] != key)
            viewer = self.clients > 0 and now - self._index_requested_at < INDEX_VIEW_S
            wanted = force or viewer or now - self._gaps_at >= GAPS_MIN_S
        running = self._warm_thread is not None and self._warm_thread.is_alive()
        if not stale:
            self._warm_pending = False
            return
        if not wanted or running:
            self._warm_pending = True
            return
        self._warm_pending = False

        self._index_ready.clear()

        def warm():
            try:
                self.analytics_index()
            except Exception as error:
                self.failure("analytics_index", error)
            finally:
                self._index_ready.set()
        self._warm_thread = threading.Thread(target=warm, daemon=True)
        self._warm_thread.start()

    def watch(self):
        while True:
            time.sleep(POLL_S)
            self.refresh(throttle=True)

    def rev_blob(self):
        """One revision per surface. The board payload carries its own component, so a
        transcript that moved does not make the board refetch, and a card that moved does not
        make the analytics view refetch. `pricing` moves when a rate card is recorded."""
        board_revision = self.board_snapshot()[1]
        with self.lock:
            return json.dumps({"data": self.data_rev, "lib": self.lib_rev,
                               "board": board_revision, "pricing": self.pricing_rev})


FILES_ROOT_DENY = paths.BROWSER_ROOT_DENY
#: The generic text files the file browser reads. A domain profile adds its own extensions through
#: `paths()["text_exts"]` (alpaca/profile.py), merged in by `files_text_exts`.
FILES_TEXT_EXTS = {".md", ".markdown", ".txt", ".html", ".htm", ".json", ".yaml", ".yml",
                   ".toml", ".cfg", ".ini", ".csv", ".log", ".py", ".sh", ".rst", ".sql", ".c", ".h",
                   ".cpp", ".hpp", ".mk", ".cmake", ".sv", ".v", ".vh", ".tcl", ""}
FILES_MAX_BYTES = 512 * 1024
#: The first bytes of every SQLite database file; the text reader refuses a file that starts so.
SQLITE_MAGIC = paths.SQLITE_MAGIC


def files_text_exts(root):
    """The extensions the file browser reads for the project at `root`: FILES_TEXT_EXTS plus the
    profile's `paths()["text_exts"]` (".proto" or "proto"), lower case. A failed profile read adds none."""
    from alpaca import profile
    try:
        extra = profile.listed(profile.load(root).paths(), "text_exts")
    except Exception:
        extra = []
    return FILES_TEXT_EXTS | {("" if e.startswith(".") else ".") + e.lower() for e in extra if e.strip(".")}


def files_auth(root):
    """The user:pass the file browser requires, or None when browsing is off.

    From ALPACA_FILES_AUTH or the file `.alpaca/files-auth`. With neither, the browse API is closed
    (403), so proofs and files are never public by default: a login must be configured first.
    """
    value = (os.environ.get("ALPACA_FILES_AUTH") or "").strip()
    if not value:
        try:
            with open(os.path.join(paths.runtime_dir(root), "files-auth"), "r", encoding="utf-8") as fh:
                value = fh.read().strip()
        except OSError:
            value = ""
    # Basic authentication permits an empty username. Preserve established
    # password-only logins while still rejecting an absent or empty password.
    _user, separator, password = value.partition(":")
    return value if separator and password else None


def _files_resolve(root, rel):
    """The absolute path for a browse request, or None when the allowlist forbids it.

    Allowed: the project tree, minus `.git`/`.venv`/build dirs, and under `.alpaca` ONLY
    `proofs/` and `wiki/decisions/` (so the record db, transcripts, instance and serve state stay
    closed). The rest of `.alpaca/wiki` is the Rune2 vault, which holds withdrawn and quarantined
    captures as files: only Rune2's doors serve it, so `paths.in_vault` refuses it here by its real
    path, through any link or other name. Private runtime files are refused
    everywhere by the same rule the proof seal applies (`paths.private_runtime`), which reads a
    kept evidence copy's name without its `NN-` number. A `..` that climbs out of
    the root is refused by the realpath containment check. A reader opens the answer through
    `_files_open`, which checks the opened file again.
    """
    rel = (rel or "").strip().lstrip("/")
    base = os.path.realpath(root)
    full = os.path.realpath(os.path.join(base, rel))
    # The rule itself is `paths.browser_serves`: dot folders and dot files are closed everywhere
    # (.claude and .codex hold session transcripts, .git holds history, .env holds secrets) except
    # `.alpaca`, and under it only `proofs/` and `wiki/`, minus the vault and private runtime
    # files. The proof seal copies evidence by the same rule.
    return full if paths.browser_serves(base, full) else None


def _files_open(root, full):
    """Open `full`, an answer of `_files_resolve`, as a binary file object, or None. The opened
    file's own path is run back through `_files_resolve` (`paths.open_checked`), so a folder on
    the path that became a link after the check cannot lead the open into the vault or a private
    file."""
    base = os.path.realpath(root)
    return paths.open_checked(
        full, lambda real: _files_resolve(root, os.path.relpath(real, base)) == real)


def files_list(root, rel):
    """A directory listing within the allowlist: dirs first, then files, each with size."""
    full = _files_resolve(root, rel)
    if full is None or not os.path.isdir(full):
        return {"error": "no such directory", "path": rel, "entries": []}
    base = os.path.realpath(root)
    out = []
    try:
        names = sorted(os.listdir(full))
    except OSError:
        return {"error": "cannot read", "path": rel, "entries": []}
    for nm in names:
        child = os.path.join(full, nm)
        if _files_resolve(root, os.path.relpath(child, base)) is None:
            continue
        is_dir = os.path.isdir(child)
        try:
            size = 0 if is_dir else os.path.getsize(child)
        except OSError:
            size = 0
        out.append({"name": nm, "dir": is_dir, "size": size,
                    "path": os.path.relpath(child, base)})
    out.sort(key=lambda e: (not e["dir"], e["name"].lower()))
    rp = "" if full == base else os.path.relpath(full, base)
    parent = None if rp == "" else os.path.dirname(rp)
    return {"path": rp, "parent": parent, "entries": out}


def files_read(root, rel):
    """The text of one allowed file, capped at FILES_MAX_BYTES, or an error."""
    full = _files_resolve(root, rel)
    if full is None or not os.path.isfile(full):
        return {"error": "no such file", "path": rel}
    ext = os.path.splitext(full)[1].lower()
    if ext not in files_text_exts(root):
        return {"error": "not a readable text file", "path": rel, "ext": ext}
    fh = _files_open(root, full)
    if fh is None:
        return {"error": "no such file", "path": rel}
    try:
        with io.TextIOWrapper(fh, encoding="utf-8", errors="replace") as text_fh:
            if fh.read(len(SQLITE_MAGIC)) == SQLITE_MAGIC:
                # a database by its bytes, whatever its name: a renamed record db, a browser
                # profile's cookie or login store
                return {"error": "not a readable text file", "path": rel, "ext": ext}
            fh.seek(0)
            size = os.fstat(text_fh.fileno()).st_size
            text = text_fh.read(FILES_MAX_BYTES + 1)
    except OSError:
        fh.close()
        return {"error": "cannot read", "path": rel}
    truncated = len(text) > FILES_MAX_BYTES
    if truncated:
        text = text[:FILES_MAX_BYTES]
    base = os.path.realpath(root)
    return {"path": os.path.relpath(full, base), "size": size, "truncated": truncated, "text": text}


def _file_resolve(root, rel):
    """The real path `/file` may serve for `rel`; False when it climbs out of the root, None when
    it is closed: `PRIVATE_DIRS` (all of `.alpaca`, `.git`, `.venv`), the Rune2 vault and private
    runtime files (`paths.in_vault`, `paths.private_runtime`). The route opens the answer with
    `paths.open_checked` and runs the opened file's own path back through here."""
    full = os.path.realpath(os.path.join(root, rel))
    base = os.path.realpath(root)
    if not (full == base or full.startswith(base + os.sep)):
        return False
    for name in PRIVATE_DIRS:
        closed = os.path.realpath(os.path.join(base, name))
        if full == closed or full.startswith(closed + os.sep):
            return None
    if paths.in_vault(base, full) or paths.private_runtime(base, full):
        return None
    return full


#: Files the public sign-in page and the workspace hub may load before a session exists.
LOGIN_ASSETS = {"vault.css": "text/css; charset=utf-8", "vault.js": "text/javascript; charset=utf-8",
                "vault-globe.js": "text/javascript; charset=utf-8",
                "orb.js": "text/javascript; charset=utf-8",
                "vendor/thinking-orbs.js": "text/javascript; charset=utf-8",
                "countries-110m.json": "application/json",
                "vendor/plex-sans.woff2": "font/woff2", "vendor/plex-sans-medium.woff2": "font/woff2",
                "vendor/plex-mono.woff2": "font/woff2"}


def _project_name(root):
    """The project's name from `project.yaml`, else its folder name."""
    from alpaca import project
    try:
        name = project.load(root).get("name")
    except Exception:
        name = None
    return str(name or os.path.basename(os.path.realpath(root)))


def workspaces(root):
    """The workspaces the hub offers: this instance first, then every other instance in the host
    registry (alpaca/workspace.py, `alpaca workspace add`). Each entry names where its cockpit and its
    live summary (the seven-key board payload) are served.

    This instance's tile keeps the same-origin `/hub/` and `/board/data.json`, so it opens
    whichever way the viewer reached this server; it is the only tile when the registry is absent,
    unreadable or does not list this instance. Another instance's href is absolute (its public
    https://<hostname>/hub/, else http://127.0.0.1:<port>/hub/) and its summary is fetched server
    side through `/workspaces/summary.json?id=<id>` as reachability only (alpaca/workspace.py
    remote_status): no stored credential is ever sent and nothing of its record leaves it."""
    from alpaca import workspace
    ident = workspace.instance_id(root)
    try:
        entries = workspace.load()
    except ValueError:
        entries = []
    mine = next((e for e in entries if e.get("id") == ident), None)
    own = {"id": ident, "name": (mine or {}).get("name") or _project_name(root),
           "project": os.path.basename(os.path.realpath(root)), "kind": "Operations workspace",
           "href": "/hub/", "summary": "/board/data.json", "self": True}
    if mine:
        own["public"] = workspace.href_of(mine)
    out = [own]
    for item in entries:
        if item.get("id") == ident:
            continue
        out.append({"id": item["id"], "name": str(item.get("name") or item["id"]),
                    "project": os.path.basename(str(item.get("root") or "")) or item["id"],
                    "kind": "Operations workspace", "href": workspace.href_of(item),
                    "summary": "/workspaces/summary.json?id=" + item["id"], "self": False})
    return out


def workspace_summary(root, ident):
    """(status, body) for another registered instance's tile: {"reachable": bool}, from a GET of
    its public `/login` page on its loopback port (2 s timeout, no redirects, no credential sent).
    404 for an id the registry does not hold."""
    from alpaca import workspace
    try:
        entries = workspace.load()
    except ValueError:
        entries = []
    item = next((e for e in entries if e.get("id") == ident and ident != workspace.instance_id(root)), None)
    if item is None:
        return 404, json.dumps({"error": "no such workspace"}).encode("utf-8")
    return 200, json.dumps(workspace.remote_status(item), ensure_ascii=True).encode("utf-8")


def make_handler(live, root, remote=False):
    from alpaca import weblogin
    throttle = weblogin.Throttle()
    totp_guard = weblogin.TotpGuard()

    class H(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _send(self, body, ctype, code=200, etag=None, cache="no-store", headers=None):
            # Conditional GET: when the caller gives an ETag (the payload's content revision) and the
            # client already holds it, answer 304 with no body. The revision already excludes the
            # volatile `generated` stamp, so an unchanged record revalidates to an empty 304 instead
            # of resending the payload. `cache="no-cache"` lets the browser store and revalidate.
            if etag is not None:
                tag = '"' + etag + '"'
                inm = self.headers.get("If-None-Match")
                if inm and (inm == "*" or tag in [p.strip() for p in inm.split(",")]):
                    self.send_response(304)
                    self.send_header("ETag", tag)
                    self.send_header("Cache-Control", cache)
                    for name, value in (headers or {}).items():
                        self.send_header(name, value)
                    self.end_headers()
                    return
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            # gzip the large text/JSON bodies when the client accepts it. data.json is ~0.5 MB of
            # highly compressible JSON pulled on a timer; over the http2 tunnel (QUIC is blocked
            # here) the uncompressed body could not keep up and the page stalled. gzip cuts it about
            # tenfold. Small or binary bodies are sent unchanged.
            ae = self.headers.get("Accept-Encoding") or ""
            compressible = any(t in ctype for t in ("json", "text", "javascript", "svg", "xml"))
            if compressible and "gzip" in ae.lower() and len(body) >= 1024:
                import gzip as _gz
                # The decoded size, so a reader of the stream can show true byte progress
                # (Content-Length below counts the compressed bytes; vault.js prefetchWorkspace).
                self.send_header("X-Body-Length", str(len(body)))
                body = _gz.compress(body, 6)
                self.send_header("Content-Encoding", "gzip")
                self.send_header("Vary", "Accept-Encoding")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", cache)
            if etag is not None:
                self.send_header("ETag", '"' + etag + '"')
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            try:
                return self._get()
            except CLIENT_GONE as error:
                live.failure("disconnect", error)
                self.close_connection = True
            except Exception as error:
                live.failure("endpoint", error)
                self.close_connection = True
                return self._json({"error": "Request failed. Check server health."}, 503)

        def _get(self):
            from urllib.parse import urlparse, parse_qs, unquote
            # Whole-server gate: when a web login is configured (ALPACA_FILES_AUTH or
            # `.alpaca/files-auth`), every route needs HTTP Basic credentials. WWW-Authenticate is
            # sent here so a browser navigating to any page prompts once and caches for the origin.
            want = files_auth(root)
            if remote and not want:
                return self._json({"error": "Remote serving requires configured credentials"}, 503)
            u = urlparse(self.path)
            path = u.path
            # The sign-in page and its assets are public; everything else sits behind the login.
            if path == "/login" or path.startswith("/login/"):
                return self._login_get(path, want)
            if path == "/logout":
                # A plain link can sign out (the cockpit sidebar uses one): clear the cookie, go to /login.
                self.send_response(302)
                self.send_header("Set-Cookie", weblogin.clear_cookie(root, self._secure()))
                self.send_header("Location", "/login")
                self.send_header("Content-Length", "0")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                return None
            if want and not self._authed(want):
                return self._locked_out(path)
            if path == "/workspaces.json":
                live.touch()
                return self._json({"workspaces": workspaces(root)})
            if path == "/workspaces/summary.json":
                live.touch()
                code, body = workspace_summary(root, (parse_qs(u.query).get("id") or [""])[0])
                return self._send(body, "application/json", code)
            if path.startswith("/hub/") or path == "/hub":
                live.touch()
                return self._hub(path, parse_qs(u.query))
            if path == "/" or path == "/index.html":
                # The landing page is the workspace hub; this project's tile opens the cockpit at /hub/.
                live.touch()
                return self._login_page("workspaces.html")
            if path == "/analytics" or path == "/analytics.html":
                self.send_response(301)
                self.send_header("Location", "/analytics/")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return None
            if path == "/analytics/" or path == "/analytics/index.html":
                live.touch()
                return self._hub("/hub/", parse_qs(u.query))
            if path in ("/cockpit/legacy/", "/analytics/legacy/"):
                # The two single-file legacy pages are retired; an old bookmark lands on
                # the workspace page that replaced it.
                self.send_response(301)
                self.send_header("Location", "/hub/" if path.startswith("/cockpit") else "/analytics/")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return None
            if path in ("/board", "/board/", "/board.html", "/board/index.html"):
                # The classic board page is retired; an old bookmark lands on the cockpit.
                self.send_response(301)
                self.send_header("Location", "/hub/")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return None
            if path in ("/cockpit", "/cockpit/", "/cockpit/index.html"):
                live.touch()
                return self._hub("/hub/", parse_qs(u.query))
            if path == "/board/data.json":
                live.touch()
                body, etag = live.board_snapshot()
                return self._send(body, "application/json", etag=etag, cache="no-cache")
            if path == "/files/list" or path == "/files/read":
                live.touch()
                return self._files_api(path, parse_qs(u.query))
            if path == "/data.json":
                start = time.monotonic()
                live.data_ready.wait(DATA_WAIT_S)    # right after a start the first fold is running
                with live.lock:
                    body, etag = live.data_json.encode("utf-8"), live.data_rev
                try:
                    return self._send(body, "application/json", etag=etag, cache="no-cache")
                finally:
                    live.note_speed(time.monotonic() - start)
            if path == "/health.json":
                live.touch()
                return self._send(live.health_json().encode("utf-8"), "application/json")
            if path == "/library.json":
                with live.lock:
                    return self._send(live.lib_json.encode("utf-8"), "application/json")
            if path == "/events":
                return self._sse()
            if path == "/file":
                rel = unquote((parse_qs(u.query).get("path") or [""])[0])
                return self._file(rel)
            # a profile route comes after every generic one and can never take a generic path
            if path in _profile_routes(root):
                live.touch()
                return self._profile_route(path, parse_qs(u.query))
            return self._send(b"not found", "text/plain", 404)

        def _json(self, value, code=200):
            return self._send(json.dumps(value, ensure_ascii=True).encode("utf-8"),
                              "application/json", code)

        def _hub(self, path, query):
            # The page's data reads are timed for the health score's hub speed (t-590); its own
            # vitals poll is not, nor the page and its assets, nor a git status a viewer asked for by hand
            # (vcs-tree.json: one worktree read on demand, up to the sweeper's read timeout).
            start = time.monotonic()
            try:
                with live.foreground_read(path):
                    return self._hub_read(path, query)
            finally:
                if path.endswith(".json") and not path.startswith("/hub/assets/") and path not in ("/hub/vitals.json", "/hub/vcs-tree.json"):
                    live.note_speed(time.monotonic() - start)

        def _hub_read(self, path, query):
            """Read services and an explicit static allowlist for the operations workbench."""
            from pathlib import Path
            from alpaca import hub
            from alpaca.analytics.detail import detail
            one = lambda key, default="": (query.get(key) or [default])[0]
            web = Path(__file__).parent / "web"
            if path in ("/hub", "/hub/", "/hub/index.html"):
                return self._send(hub_page(root).encode("utf-8"), "text/html; charset=utf-8")
            if path.startswith("/hub/assets/profile/"):
                # a profile's own web modules and styles (alpaca/profile.py `web` assets)
                from alpaca import profile
                name = path[len("/hub/assets/profile/"):]
                found = profile.asset(root, name)
                if found is None:
                    return self._send(b"not found", "text/plain", 404)
                kind = {".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
                        ".json": "application/json", ".svg": "image/svg+xml",
                        ".woff2": "font/woff2"}.get(os.path.splitext(name)[1].lower())
                if kind is None:
                    return self._send(b"not found", "text/plain", 404)
                with open(found, "rb") as fh:
                    return self._send(fh.read(), kind)
            if path.startswith("/hub/assets/"):
                name, assets = path[len("/hub/assets/"):], hub_assets()
                if name not in assets or not (web / name).is_file():
                    return self._send(b"not found", "text/plain", 404)
                body = (web / name).read_bytes()
                if name.startswith("vendor/"):
                    # A vendor file is kept for a day only under a URL that carries a content version
                    # (?v=): Arthur's links get arthur_version, and the fonts and thinking-orbs.js are
                    # versioned where hub.css and orb.js name them (vendor_links), so a changed file
                    # has a new URL. Any other request for it revalidates by its sha256 ETag (t-595).
                    versioned = "v" in query and name.endswith((".png", ".webp", ".woff2", ".js"))
                    return self._send(body, assets[name], etag=_content_tag(web / name, body),
                                      cache="max-age=86400" if versioned else "no-cache")
                return self._send(vendor_links(name, body), assets[name])
            try:
                if path == "/hub/vitals.json":
                    return self._send(live.vitals_json().encode("utf-8"), "application/json")
                elif path == "/hub/overview.json":
                    body, etag = live.overview()
                    return self._send(body, "application/json", etag=etag, cache="no-cache")
                elif path == "/hub/session-asks.json":
                    live.data_ready.wait(DATA_WAIT_S)
                    body, etag = live.session_asks()
                    return self._send(body.encode("utf-8"), "application/json", etag=etag, cache="no-cache")
                elif path in ("/hub/tasks.json", "/hub/messages.json"):
                    got = live.work_view(path[len("/hub/"):-len(".json")], one("ids"))
                    return self._send(got[0], "application/json", etag=got[1], cache="no-cache")
                elif path == "/hub/mission.json":
                    got = live.mission_view(one("at") or None)
                    if one("view") == "compact" and got is not None:
                        got = live.slim("mission", got, mission_compact)
                    return self._send(got[0], "application/json", etag=got[1], cache="no-cache")
                elif path == "/hub/live-work.json":
                    got = live.live_work()
                    return self._send(got[0], "application/json", etag=got[1], cache="no-cache")
                elif path == "/hub/mission-evidence.json":
                    if not files_auth(root):
                        return self._json({"error": "Evidence reading requires a configured web login"}, 403)
                    from alpaca import mission
                    value = mission.evidence(root, int(one("event", "0")))
                elif path == "/hub/research.json":
                    # the research view (operator-surface.md section 7.2): a bad run id is a
                    # ValueError (400); an unknown run is answered here, since the chain's
                    # KeyError answer names a session
                    from alpaca.research import view as research_view
                    run = None
                    if "run" in query:
                        run = research_view._check_id(one("run"))
                    got = live.research_view(run)
                    if got is None:
                        return self._json({"error": "Research run not found in this project"}, 404)
                    if one("view") == "compact" and not run:
                        got = live.slim("research", got, research_compact)
                    return self._send(got[0], "application/json", etag=got[1], cache="no-cache")
                elif path == "/hub/history.json":
                    value = hub.history(root, before=one("before") or None, limit=int(one("limit", "50")),
                                        query=one("q"), kind=one("kind", "work"), session=one("session"), ref=one("ref"),
                                        day=one("day"), calendar=one("calendar") == "1")
                elif path == "/hub/system.json":
                    from alpaca import sysmon
                    value = sysmon.snapshot(root)
                elif path == "/hub/runs.json":
                    value = hub.runs(root)
                elif path == "/hub/documents.json":
                    value = hub.documents(root, query=one("q"), category=one("category", "reports"),
                                          ext=one("ext"), limit=int(one("limit", "100")), offset=int(one("offset", "0")))
                elif path == "/hub/document.json":
                    if not files_auth(root):
                        return self._json({"error": "Report reading requires a configured web login"}, 403)
                    value = hub.document(root, one("path"))
                elif path == "/hub/records.json":
                    value = hub.records(root, one("section", "ops"), query=one("q"), limit=one("limit", "200"),
                                        offset=one("offset", "0") or 0)
                elif path in ("/hub/graph.json", "/hub/graph-item.json"):
                    # the evidence graph carries wiki claims: the same login as the wiki and the reports
                    if not files_auth(root):
                        return self._json({"error": "The evidence graph requires a configured web login"}, 403)
                    if path == "/hub/graph.json":
                        value = hub.evidence_graph(root)
                    else:
                        try:
                            value = hub.graph_item(root, one("id"))
                        except ValueError as exc:
                            return self._json({"error": str(exc)}, 404)
                elif path in ("/hub/wiki-status.json", "/hub/wiki-briefing.json"):
                    # wiki text is project knowledge: read behind the same login as the reports
                    if not files_auth(root):
                        return self._json({"error": "Wiki reading requires a configured web login"}, 403)
                    if path == "/hub/wiki-status.json":
                        value = hub.wiki_status(root)
                    else:
                        try:
                            value = hub.wiki_briefing(root, op=one("op"), task=one("task"))
                        except ValueError as exc:
                            return self._json({"error": str(exc)}, 400)
                elif path == "/hub/session.json":
                    value = detail(root, one("sid"), before=one("before") or None,
                                   limit=int(one("limit", "80")), child=one("child") or None,
                                   around=one("around") or None, after=one("after") or None)
                elif path == "/hub/analytics.json":
                    from alpaca.analytics.metrics import analyze
                    value = analyze(root, one("sid"), child=one("child") or None)
                elif path == "/hub/analytics-index.json":
                    live.data_ready.wait(DATA_WAIT_S)
                    live.note_index_request()
                    try:
                        body, etag, code, freshness = live.analytics_snapshot()
                    except FoldPending as pending:
                        return self._send(json.dumps({"error": str(pending)}).encode("utf-8"), "application/json",
                                          503, headers={"Retry-After": str(RETRY_FOLD_S)})
                    return self._send(body.encode("utf-8"), "application/json", code=code, etag=etag, cache="no-cache",
                                      headers={"X-Alpaca-Analytics-State": freshness, "Retry-After": "2"} if code == 202
                                      else {"X-Alpaca-Analytics-State": freshness})
                elif path == "/hub/vcs.json":
                    # Read-only git (alpaca/gitview.py), reused for VCS_S.
                    from alpaca import gitview
                    live.vcs_touch()                  # watched from the request's start: a first build can take a minute
                    try:
                        value = live.vcs(int(one("days", "7") or "7"))
                    except gitview.GitError as error:
                        return self._json({"error": "Git could not be read: %s" % str(error)[:200]}, 503)
                    live.vcs_touch(value)
                    value = dict(value, served_at=time.time(), hot_backlog=live._vcs_sweep.backlog)
                elif path == "/hub/vcs-status.json":
                    # Per-worktree status from the cache the sweeper keeps fresh; polled every 5 s by the open page (t-592).
                    from alpaca import gitview
                    live.vcs_touch()
                    try:
                        value = live.vcs_status()
                    except gitview.GitError as error:
                        return self._json({"error": "Git could not be read: %s" % str(error)[:200]}, 503)
                elif path == "/hub/vcs-tree.json":
                    # One worktree outside the watched set, read on demand ("Load status") and kept live a while.
                    from alpaca import gitview
                    live.vcs_touch()
                    try:
                        value = live.vcs_tree(one("path") or "")
                    except gitview.GitError as error:
                        return self._json({"error": "Git could not be read: %s" % str(error)[:200]}, 503)
                    if value is None:
                        return self._json({"error": "Not a worktree of this repository"}, 404)
                elif path == "/hub/inflight.json":
                    # Polled every 15 s by the In-flight tab; "before" reads earlier activity.
                    from alpaca.analytics import inflight
                    if one("before"):
                        value = inflight.more(root, one("task"), int(one("before")))
                    else:
                        value = live.inflight()
                elif path == "/hub/burn.json":
                    live.data_ready.wait(DATA_WAIT_S)
                    try:
                        # start/end: a custom range the browser computed from its local days.
                        body, etag = live.burn(one("window", "24h") or "24h", one("start") or None, one("end") or None)
                    except FoldPending as pending:
                        return self._send(json.dumps({"error": str(pending)}).encode("utf-8"), "application/json",
                                          503, headers={"Retry-After": str(RETRY_FOLD_S)})
                    return self._send(body.encode("utf-8"), "application/json", etag=etag, cache="no-cache")
                elif path == "/hub/analytics-children.json":
                    from alpaca.analytics.metrics_index import family
                    value = family(root, one("sid"))
                elif path in _profile_routes(root):
                    return self._profile_route(path, query)
                else:
                    return self._json({"error": "not found"}, 404)
                return self._json(value)
            except (ValueError, TypeError) as exc:
                return self._json({"error": str(exc)}, 400)
            except FileNotFoundError:
                return self._json({"error": "No readable report at this path"}, 404)
            except KeyError:
                return self._json({"error": "Session not found in this project"}, 404)
            except CLIENT_GONE:
                raise                            # do_GET counts the reader leaving as a disconnect (t-590)
            except Exception as error:
                live.failure("endpoint", error)
                # Never disclose host paths, SQL, or source content in the HTTP error response.
                return self._json({"error": "The project record could not be read. Try again or check server health."}, 503)

        def _basic_ok(self, want):
            import base64, hmac
            header = self.headers.get("Authorization") or ""
            if not header.startswith("Basic "):
                return False
            try:
                got = base64.b64decode(header[6:]).decode("utf-8", "replace")
            except Exception:
                return False
            return hmac.compare_digest(got, want)

        def _client(self):
            # Behind the Cloudflare tunnel every request arrives from 127.0.0.1; the edge names
            # the real client in CF-Connecting-IP. Off the tunnel the header is absent.
            return (self.headers.get("CF-Connecting-IP") or self.client_address[0] or "?").strip()

        def _secure(self):
            return (self.headers.get("X-Forwarded-Proto") or "").lower() == "https"

        def _authed(self, want):
            """True for a valid session cookie or matching Basic credentials. A wrong Basic
            header counts as a failed attempt, so the header path cannot bypass the throttle."""
            if weblogin.valid(root, weblogin.session_credential(want, weblogin.totp_secret(root)),
                              weblogin.read_cookie(root, self.headers.get("Cookie"))):
                return True
            if not (self.headers.get("Authorization") or "").startswith("Basic "):
                return False
            # Browsers send Sec-Fetch-* on every request; curl and scripts do not. A browser that
            # still caches the old Basic password from the retired dialog would otherwise stay
            # signed in after sign-out, so browsers must hold the session cookie.
            if self.headers.get("Sec-Fetch-Mode") or self.headers.get("Sec-Fetch-Site"):
                return False
            client = self._client()
            if throttle.blocked(client):
                return False
            if self._basic_ok(want):
                return True
            throttle.fail(client)
            return False

        def _locked_out(self, path):
            """Answer an unauthenticated request: the sign-in page for a page navigation, a
            JSON 401 for data. Status 401 either way (the tunnel watchdog reads 200 or 401 as
            a live origin); no WWW-Authenticate, so the browser never shows its own dialog."""
            accept = self.headers.get("Accept") or ""
            if ("text/html" in accept and not path.endswith(".json") and path != "/events"
                    and path not in _profile_routes(root)):
                return self._login_page("login.html", 401)
            return self._json({"error": "Sign in required", "login": "/login"}, 401)

        def _login_page(self, name, code=200):
            from pathlib import Path
            body = (Path(__file__).parent / "web" / name).read_bytes()
            return self._send(body, "text/html; charset=utf-8", code)

        def _login_get(self, path, want):
            from pathlib import Path
            from urllib.parse import urlparse, parse_qs
            web = Path(__file__).parent / "web"
            if path.startswith("/login/assets/"):
                name = path[len("/login/assets/"):]
                if name not in LOGIN_ASSETS or not (web / name).is_file():
                    return self._send(b"not found", "text/plain", 404)
                # Scripts and styles revalidate on every load so a fix reaches the page at once.
                cache = "max-age=86400" if name.endswith((".woff2", ".json")) else "no-cache"
                return self._send((web / name).read_bytes(), LOGIN_ASSETS[name], cache=cache)
            if path == "/login/info.json":
                return self._json({"configured": bool(want), "user": weblogin.needs_user(want),
                                   "totp": bool(weblogin.totp_secret(root)),
                                   "signed_in": bool(want) and self._authed(want)})
            if path in ("/login", "/login/"):
                if want and self._authed(want):
                    target = weblogin.safe_next((parse_qs(urlparse(self.path).query).get("next") or ["/"])[0])
                    self.send_response(302)
                    self.send_header("Location", target)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return None
                return self._login_page("login.html")
            return self._send(b"not found", "text/plain", 404)

        def do_POST(self):
            try:
                return self._post()
            except CLIENT_GONE as error:
                live.failure("disconnect", error)
                self.close_connection = True
            except Exception as error:
                live.failure("endpoint", error)
                self.close_connection = True
                return self._json({"error": "Request failed. Check server health."}, 503)

        def _post(self):
            from urllib.parse import urlparse
            path = urlparse(self.path).path
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(min(length, 4096)) if length > 0 else b""
            if path == "/auth/logout":
                self.send_response(204)
                self.send_header("Set-Cookie", weblogin.clear_cookie(root, self._secure()))
                self.send_header("Content-Length", "0")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                return None
            if path != "/auth/login":
                return self._send(b"not found", "text/plain", 404)
            want = files_auth(root)
            if not want:
                return self._json({"error": "No web login is configured on this server."}, 403)
            client = self._client()
            wait = throttle.blocked(client)
            if wait:
                return self._json({"error": "Too many attempts. Try again in %d minutes." % max(1, (wait + 59) // 60),
                                   "retry_after": wait}, 429)
            try:
                form = json.loads(raw.decode("utf-8") or "{}")
            except ValueError:
                form = {}
            user = str(form.get("user") or "") if isinstance(form, dict) else ""
            code = str(form.get("code") or "") if isinstance(form, dict) else ""
            # With an authenticator enrolled only its current code signs in; the fixed code does not.
            secret = weblogin.totp_secret(root)
            if secret:
                accepted = weblogin.user_ok(want, user) and totp_guard.check(secret, code)
            else:
                accepted = weblogin.credential_ok(want, user, code)
            if not accepted:
                throttle.fail(client)
                return self._json({"error": "Access code not accepted."}, 401)
            throttle.succeed(client)
            body = json.dumps({"ok": True}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Set-Cookie", weblogin.set_cookie(
                root, weblogin.issue(root, weblogin.session_credential(want, secret)), self._secure()))
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return None

        def _files_api(self, path, query):
            """The gated file browser. Closed (403) unless a login is configured; otherwise every
            request carries HTTP Basic credentials that match `files_auth`. The allowlist in
            `_files_resolve` is what keeps the record db, transcripts, secrets and the Rune2 vault out
            of reach."""
            want = files_auth(root)
            if not want:
                return self._json({"error": "file browsing is not enabled on this server"}, 403)
            if not self._authed(want):
                # No WWW-Authenticate on purpose: the cockpit renders its own in-page login and
                # sends Basic credentials itself, so the browser must not race it with a native
                # auth dialog. curl -u still works (it sends the header preemptively).
                body = b"authenticate"
                self.send_response(401)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
                return
            rel = (query.get("path") or [""])[0]
            if path == "/files/list":
                return self._json(files_list(root, rel))
            return self._json(files_read(root, rel))

        def _profile_route(self, path, query):
            """A profile route (alpaca/profile.py `routes`): read-only, never a projection of the
            record, behind the same sign-in as every other route. The handler validates its own
            parameters before any of them reaches a path, and returns (value, code)."""
            handler = _profile_routes(root).get(path)
            if handler is None:
                return self._send(b"not found", "text/plain", 404)
            try:
                value, code = handler(root, query)
            except (ValueError, TypeError) as exc:
                return self._json({"error": str(exc)}, 400)
            except FileNotFoundError:
                return self._json({"error": "not found"}, 404)
            except CLIENT_GONE:
                raise                            # do_GET counts the reader leaving as a disconnect (t-590)
            except Exception as error:
                live.failure("profile_route", error)
                return self._json({"error": "The profile could not answer. Try again or check server health."}, 503)
            return self._json(value, code)

        def _file(self, rel):
            if not rel:
                return self._send(b"missing path", "text/plain", 400)
            full = _file_resolve(root, rel)
            if full is False:
                return self._send(b"forbidden", "text/plain", 403)
            if full is None:
                return self._send(b"not found", "text/plain", 404)
            ext = os.path.splitext(full)[1].lower()
            if ext not in READABLE or not os.path.isfile(full):
                return self._send(b"not found", "text/plain", 404)
            base = os.path.realpath(root)
            fh = paths.open_checked(
                full, lambda real: _file_resolve(root, os.path.relpath(real, base)) == real)
            if fh is None:
                return self._send(b"not found", "text/plain", 404)
            try:
                with fh:
                    body = fh.read()
            except OSError:
                return self._send(b"not found", "text/plain", 404)
            ct = {".html": "text/html; charset=utf-8", ".htm": "text/html; charset=utf-8",
                  ".json": "application/json", ".csv": "text/csv; charset=utf-8"}.get(ext, "text/plain; charset=utf-8")
            return self._send(body, ct)

        def _sse(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            live.client_enter()
            try:
                self.wfile.write(b"retry: 3000\n")
                self.wfile.write(("event: rev\ndata: %s\n\n" % live.rev_blob()).encode("utf-8"))
                self.wfile.write(b'event: job\ndata: {"moved":true}\n\n')
                self.wfile.flush()
                seen, seen_job = live.gen, live.job_gen
                while True:
                    with live.cond:
                        fired = live.cond.wait_for(
                            lambda: live.gen != seen or live.job_gen != seen_job,
                            timeout=HEARTBEAT_S)
                        moved_rev = live.gen != seen
                        moved_job = live.job_gen != seen_job
                        seen, seen_job = live.gen, live.job_gen
                    # Recheck long-lived streams after rotation/removal as well as new requests.
                    current_auth = files_auth(root)
                    if (remote and not current_auth) or (current_auth and not self._authed(current_auth)):
                        self.close_connection = True
                        return
                    if fired and moved_rev:
                        self.wfile.write(("event: rev\ndata: %s\n\n" % live.rev_blob()).encode("utf-8"))
                    if fired and moved_job:
                        # active.json or the job file moved: a stage started, moved or finished.
                        self.wfile.write(b'event: job\ndata: {"moved":true}\n\n')
                    if not fired:
                        self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                return
            finally:
                live.client_exit()

    return H


class _Server(http.server.ThreadingHTTPServer):
    # The stdlib default backlog is 5; a burst of tunnel connections during a reload must queue,
    # not be refused.
    request_queue_size = 128


#: The listening socket a reloading server hands to its exec'd successor.
LISTEN_FD_ENV = "ALPACA_SERVE_FD"


def _adopt(fd):
    """A server on the already-listening socket `fd`, or None when it is not usable."""
    try:
        sock = socket.socket(fileno=int(fd))
        address = sock.getsockname()[:2]
        sock.listen(_Server.request_queue_size)
    except (OSError, ValueError):
        return None
    httpd = _Server(address, None, bind_and_activate=False)
    httpd.socket.close()
    httpd.socket = sock
    httpd.server_address = address
    httpd.server_name, httpd.server_port = address[0], address[1]
    return httpd


def _bind(port):
    """Bind the requested port, scanning a few up if it is taken (lost port race).

    A reload hands its listening socket over in ALPACA_SERVE_FD, so the port never closes and
    no request meets a refused connection. Under a supervisor (ALPACA_SERVE_STRICT_PORT=1) the
    port is exact: a server that drifted to another port would sit behind no tunnel."""
    fd = os.environ.pop(LISTEN_FD_ENV, "")
    if fd:
        httpd = _adopt(fd)
        if httpd is not None:
            return httpd, httpd.server_port
    strict = os.environ.get("ALPACA_SERVE_STRICT_PORT") == "1"
    from alpaca.workspace import RESERVED_PORTS
    last = None
    for p in range(port, port + (1 if strict else 20)):
        if p in RESERVED_PORTS and not strict:
            continue          # a scan never lands on a reserved port; only a strict bind asked for it
        try:
            httpd = _Server(("127.0.0.1", p), None)
            return httpd, p
        except OSError as e:
            last = e
    raise last or OSError("no port to bind near %d" % port)


CODE_POLL_S = 5.0            # how often a running server compares its own source files
CODE_QUIET_S = 30.0          # how long the source must stay unchanged before a reload


def code_signature():
    """Size and nanosecond mtime of every alpaca module the server can import, tests excluded.
    A long-lived server holds the code it started with; this tells it the files moved."""
    base = os.path.dirname(os.path.abspath(__file__))
    parts = []
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = sorted(d for d in dirnames if d not in ("tests", "__pycache__"))
        for name in sorted(filenames):
            if name.endswith(".py"):
                path = os.path.join(dirpath, name)
                try:
                    st = os.stat(path)
                except OSError:
                    continue
                parts.append((os.path.relpath(path, base), st.st_size, st.st_mtime_ns))
    return tuple(parts)


def code_importable(root):
    """Import the new code in a child process before the server replaces itself, so a file
    caught half-written or a syntax error keeps the old server up instead of taking it down."""
    from alpaca import child
    probe = ("alpaca.serve", "alpaca.hub", "alpaca.work_record", "alpaca.export", "alpaca.proof")
    try:
        # through the one launcher: no working folder on the child's path
        done = subprocess.run(child.python_argv("alpaca.import_probe", *probe, served_root=root),
                              cwd=root, env=child.child_env(served_root=root), capture_output=True,
                              text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as e:
        return False, str(e)
    return done.returncode == 0, (done.stderr or "").strip()[-400:]


def _reexec_argv(root=None):
    """The command line that started this server, as `python -m alpaca ...` through the one
    launcher."""
    from alpaca import child
    return child.python_argv("alpaca", *sys.argv[1:], served_root=root)


def _run(root, port, keep=False, remote=False):
    if remote and not files_auth(root):
        raise ValueError("Remote serving requires configured credentials")
    # Bind (or adopt the socket a reload handed over) before any refresh, and serve after the
    # fast one: the board, library and profile live state take well under a second. The analytics fold
    # (16 s on this record) runs on the watcher; analytics requests wait for it (DATA_WAIT_S).
    httpd, port = _bind(port)
    live = Live(root)
    live.refresh(fold=False)
    live.warm_vcs()
    httpd.RequestHandlerClass = make_handler(live, root, remote=remote)
    httpd.daemon_threads = True
    url = "http://127.0.0.1:%d/" % port
    state = {"pid": os.getpid(), "port": port, "url": url, "started": util.now_iso(),
             "keep": bool(keep), "remote": bool(remote)}
    util.write_text(_state_path(root), json.dumps(state, indent=1))

    reload = {"now": False}

    def _cleanup(*a):
        if reload["now"]:
            return    # exec keeps this pid, so the state file stays true
        try:
            if _read_state(root) and _read_state(root).get("pid") == os.getpid():
                os.remove(_state_path(root))
        except OSError:
            pass
        raise SystemExit(0)

    def _code_watch():
        # A reader must never see pages built by code older than the files on disk: when the
        # alpaca modules change and stay unchanged for one more poll, the server checks the new
        # code imports, then stops serving and execs itself with the same command line.
        seen = code_signature()
        while True:
            time.sleep(CODE_POLL_S)
            now = code_signature()
            if now == seen:
                continue
            # Several sessions edit alpaca/ in bursts: reload once the tree has been quiet for
            # CODE_QUIET_S, not after every save.
            quiet_since = time.time()
            while time.time() - quiet_since < CODE_QUIET_S:
                time.sleep(CODE_POLL_S)
                current = code_signature()
                if current != now:
                    now, quiet_since = current, time.time()
            ok, why = code_importable(root)
            seen = code_signature()
            if not ok:
                sys.stderr.write("alpaca serve: source changed but does not import; keeping the "
                                 "running code: %s\n" % why)
                continue
            sys.stderr.write("alpaca serve: source changed, reloading\n")
            reload["now"] = True
            httpd.shutdown()
            return

    def _idle_watch():
        spell = idle_seconds()
        while True:
            time.sleep(min(60, spell))
            if live.idle_for() >= spell:
                sys.stderr.write("alpaca serve: idle %ds, shutting down\n" % spell)
                httpd.shutdown()
                return

    signal.signal(signal.SIGTERM, _cleanup)
    threading.Thread(target=live.watch, daemon=True).start()
    if os.environ.get("ALPACA_SERVE_RELOAD", "1") != "0":
        threading.Thread(target=_code_watch, daemon=True).start()
    if keep:
        # `--keep` is for a run that takes longer than the idle spell: a reader who steps away
        # from a five-hour stage comes back to the same server rather than a dead port.
        sys.stderr.write("alpaca serve: idle shutdown off (--keep)\n")
    else:
        threading.Thread(target=_idle_watch, daemon=True).start()
    sys.stderr.write("alpaca serve: %s (pid %d)\n" % (url, os.getpid()))
    for line in address_lines(url, port):
        sys.stderr.write(line + "\n")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if reload["now"]:
            # Keep the listening socket open through the exec: connections queue in its backlog
            # and the successor adopts it (_bind), so the tunnel never sees a closed port.
            fd = httpd.socket.fileno()
            os.set_inheritable(fd, True)
            os.environ[LISTEN_FD_ENV] = str(fd)
            sys.stderr.flush()
            from alpaca import child
            argv = _reexec_argv(root)
            os.execve(argv[0], argv, child.child_env(served_root=root))
        _cleanup()


def ensure_running(root, wait_s=1.5):
    """Idempotently start the live server for this project; return its URL or None.

    Safe to call from a hook: reuses a live server, spawns a detached one otherwise,
    and never blocks longer than wait_s. A no-op under pytest (the suite must not
    detach real servers) and serialized by a file lock so a concurrent session-start
    race cannot bind two servers for one root.
    """
    if os.environ.get("PYTEST_CURRENT_TEST") or os.environ.get("ALPACA_NO_AUTOSERVE"):
        return None
    st = is_running(root)
    if st:
        return st.get("url")
    os.makedirs(paths.runtime_dir(root), exist_ok=True)
    import fcntl
    lockp = os.path.join(paths.runtime_dir(root), "serve.lock")
    lockf = open(lockp, "w")
    try:
        fcntl.flock(lockf, fcntl.LOCK_EX)
        st = is_running(root)          # re-check inside the lock: the race winner already spawned
        if st:
            return st.get("url")
        try:
            port = _instance_port(root)
            public = _public(root, port)
        except ValueError as error:
            print("alpaca serve: not started: %s" % error, file=sys.stderr)
            return None
        if public and not files_auth(root):
            print("alpaca serve: not started: this instance is public (a hostname in the workspace registry "
                  "or a live cloudflared rule to its port) and has no web login; write .alpaca/files-auth first",
                  file=sys.stderr)
            return None
        log = os.path.join(paths.runtime_dir(root), "serve.log")
        try:
            fh = open(log, "ab")
        except OSError:
            fh = subprocess.DEVNULL
        from alpaca import child
        try:
            subprocess.Popen(child.python_argv("alpaca", "serve", "--port", str(port),
                                               *(["--remote"] if public else []), served_root=root),
                             cwd=root, env=child.child_env(served_root=root), stdout=fh, stderr=fh,
                             stdin=subprocess.DEVNULL, start_new_session=True, close_fds=True)
        except OSError:
            return None
        deadline = time.time() + wait_s
        while time.time() < deadline:
            time.sleep(0.1)
            st = is_running(root)
            if st:
                return st.get("url")
        return "http://127.0.0.1:%d/" % port
    finally:
        try:
            fcntl.flock(lockf, fcntl.LOCK_UN)
        except OSError:
            pass
        lockf.close()


def start_detached(root, port=None, keep=False, wait_s=15.0, remote=False):
    """Start the server in its own process session and return its state dict, or None.

    `start_new_session=True` is the double fork: the child leads a new session with no
    controlling terminal, so the hangup that reaches a login shell on logout never reaches the
    server. A detached process has no terminal to write to, so its output goes to
    `.alpaca/serve.log`. The wait is for the port to answer, not for the spawn to return: the
    caller prints a URL only once that URL serves.
    """
    clear_stale_state(root)
    st = is_running(root)
    if st:
        return st
    os.makedirs(paths.runtime_dir(root), exist_ok=True)
    unit = supervised_unit(root)
    if unit:
        # The supervised server is the one the tunnel relies on: start it rather than a second,
        # unsupervised copy that would race it for the port.
        try:
            subprocess.run(["systemctl", "--user", "start", unit], timeout=30,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError):
            unit = None
    if not unit:
        port = port or _instance_port(root)
        from alpaca import child
        cmd = child.python_argv("alpaca", "serve", "--port", str(port), *(["--keep"] if keep else []),
                                *(["--remote"] if remote else []), served_root=root)
        if not _spawn(root, cmd):
            return None
    deadline = time.time() + wait_s
    while time.time() < deadline:
        time.sleep(0.1)
        st = is_running(root)
        if st:
            return st
    return None


def supervised_unit(root):
    """The enabled systemd user unit that runs this project's dashboard, or None."""
    if os.environ.get("PYTEST_CURRENT_TEST") or os.environ.get("INVOCATION_ID"):
        return None    # under test, or already the supervised process itself
    name = "alpaca-%s-dashboard.service" % hashlib.sha256(os.path.realpath(root).encode()).hexdigest()[:12]
    try:
        done = subprocess.run(["systemctl", "--user", "is-enabled", name], capture_output=True,
                              text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return name if done.returncode == 0 and done.stdout.strip() == "enabled" else None


def _spawn(root, cmd):
    try:
        fh = open(_log_path(root), "ab")
    except OSError:
        fh = subprocess.DEVNULL
    from alpaca import child
    try:
        subprocess.Popen(cmd, cwd=root, env=child.child_env(served_root=root), stdout=fh, stderr=fh,
                         stdin=subprocess.DEVNULL, start_new_session=True, close_fds=True)
    except OSError:
        return False
    finally:
        if fh is not subprocess.DEVNULL:
            fh.close()
    return True


def service_units(root, *, port=None, python=None):
    """Reproducible user-service contracts. Generation neither installs nor starts units.
    The dashboard port is `port`, else this instance's registered port (alpaca workspace add), else
    _default_port(root); never a reserved port by default.

    Each unit starts Python by the launcher rule of `alpaca/child.py`:
    `-I` right after the interpreter, so neither the working folder nor PYTHONPATH (nor any
    other PYTHON* variable, from the unit's environment file included) reaches the import path.
    `bin/alpaca-python -I -m` starts the module through the tree's `alpaca/boot.py`, which loads the
    package by its location, with the venv interpreter or else python3; an explicit
    `python` gets the boot of this process's package in the argv. `UnsetEnvironment=PYTHONPATH`
    keeps it from the children the services start."""
    root = os.path.realpath(root)
    if port is None:
        port = _instance_port(root)
    if any(ord(char) < 32 for char in root):
        raise ValueError("service root must not contain control characters")
    if not 0 < int(port) < 65536:
        raise ValueError("port must be between 1 and 65535")
    # systemd expands percent specifiers; quote paths and escape those specifiers. (The quoted
    # value here is the executable, which systemd never $-expands.)
    def quote(value):
        return '"' + str(value).replace("%", "%%").replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "\\r") + '"'
    executable = quote(python or os.path.join(root, "bin", "alpaca-python"))
    if python:
        from alpaca import child
        executable += " -I " + quote(child.boot_path())
    else:
        executable += " -I"
    ident = hashlib.sha256(root.encode()).hexdigest()[:12]
    prefix = "alpaca-" + ident
    units = {}
    for role, arguments in (("dashboard", "serve --keep --remote --port %d" % int(port)),
                            ("collector", "collect watch --interval 15")):
        units[prefix + "-" + role + ".service"] = (
            "[Unit]\nDescription=Alpaca %s (%s)\nStartLimitIntervalSec=120\nStartLimitBurst=10\n\n"
            "[Service]\nType=simple\nWorkingDirectory=%s\nEnvironmentFile=-%s\n"
            "UnsetEnvironment=PYTHONPATH\n"
            "%sExecStart=%s -m alpaca %s\nRestart=%s\nRestartSec=5\n"
            "TimeoutStopSec=30\nKillMode=control-group\nUMask=0077\n"
            "StandardOutput=journal\nStandardError=journal\nSyslogIdentifier=%s-%s\n\n"
            "[Install]\nWantedBy=default.target\n" % (
                role, ident, root.replace("%", "%%"), os.path.join(root, ".alpaca", "service.env").replace("%", "%%"),
                # The dashboard is restarted on any exit, including a clean `alpaca serve --stop`,
                # and keeps its exact port so the tunnel always finds it.
                "Environment=ALPACA_SERVE_STRICT_PORT=1\n" if role == "dashboard" else "",
                executable, arguments, "always" if role == "dashboard" else "on-failure",
                prefix, role))
    return units


def write_services(root, *, port=None):
    from alpaca import workspace
    units = service_units(root, port=port)
    directory = os.path.join(paths.runtime_dir(root), "services")
    for name, body in units.items():
        workspace.write_file(os.path.join(directory, name), body, 0o600)
    return [os.path.join(directory, name) for name in units]


@cli.command("serve")
def cmd_serve(args):
    root = cli._root()
    if getattr(args, "write_services", False):
        try:
            files = write_services(root, port=args.port or None)
        except ValueError as error:
            print("alpaca serve: %s" % error)
            return cli.USAGE
        print(json.dumps({"files": files, "installed": False, "started": False}))
        return cli.PASS
    if getattr(args, "totp_enroll", False):
        from alpaca import weblogin
        try:
            secret = weblogin.totp_enroll(root)
        except FileExistsError as error:
            print("alpaca serve: an authenticator is already enrolled (%s). Delete that file to "
                  "enroll a new one; every browser is then signed out." % error)
            return cli.USAGE
        print("Authenticator enrolled. In Google Authenticator: + > Enter a setup key,")
        print("  account: Alpaca %s   key: %s   type: Time based" % (os.path.basename(os.path.realpath(root)), secret))
        print("or import: %s" % weblogin.totp_uri(secret, os.path.basename(os.path.realpath(root))))
        print("From now on /login takes the 6-digit code. Existing browser sessions are signed out.")
        return cli.PASS
    if args.stop:
        st = _read_state(root)
        if st and _pid_alive(st.get("pid")):
            try:
                os.kill(int(st["pid"]), signal.SIGTERM)
            except OSError:
                pass
            for _ in range(30):      # let the server run its own cleanup before the file goes
                if not _pid_alive(st.get("pid")):
                    break
                time.sleep(0.1)
            print("alpaca serve: stopped (pid %s)" % st.get("pid"))
        else:
            print("alpaca serve: not running")
        try:
            os.remove(_state_path(root))
        except OSError:
            pass
        return cli.PASS
    if args.status:
        stale = clear_stale_state(root)
        st = is_running(root)
        if not st:
            print("alpaca serve: stopped"
                  + (" (cleared a stale record of pid %s)" % stale.get("pid") if stale else ""))
            return cli.PASS
        print("alpaca serve: running at %s (pid %s, up %s, idle shutdown %s)"
              % (st["url"], st.get("pid"), _uptime_words(st.get("started")),
                 "off (--keep)" if st.get("keep") else "on after %s" % _spell_words(idle_seconds())))
        for line in address_lines(st["url"], st["port"]):
            print(line)
        return cli.PASS
    clear_stale_state(root)
    # Only a start that needs the registry fails on a corrupt one: without --port the registry
    # names the port. With --port (every installed unit passes one) the server starts with a
    # warning, and the public-hostname rule fails closed: an unreadable registry never lets an
    # instance whose root it may list run with no login at all.
    try:
        public = _public(root, args.port)
    except ValueError as error:
        if not args.port:
            print("alpaca serve: %s" % error)
            return cli.FAIL
        # stderr, flushed: a unit's journal keeps it even when SIGTERM ends the process early
        print("alpaca serve: WARNING: %s; starting on --port %d" % (error, args.port), file=sys.stderr, flush=True)
        from alpaca import workspace
        public = workspace.live_routes_port(args.port)
        if not (getattr(args, "remote", False) or files_auth(root)) and _listed_in_registry_text(root):
            print("alpaca serve: refusing: the unreadable workspace registry names this root, so it may be "
                  "public; set a web login (.alpaca/files-auth) and start with --remote, or repair the registry")
            return cli.FAIL
    try:
        port = args.port or _instance_port(root)
        if not public:
            from alpaca import workspace
            public = workspace.live_routes_port(port)
    except ValueError as error:
        print("alpaca serve: %s" % error)
        return cli.FAIL
    if public:
        # a tunnel-routed instance is never served without its sign-in
        args.remote = True
    # A reload successor holds its predecessor's socket (LISTEN_FD_ENV): the server that answers
    # on the port is this same process, so it must not stop as "already running".
    st = None if os.environ.get(LISTEN_FD_ENV) else is_running(root)
    if st:
        if getattr(args, "remote", False) and not st.get("remote"):
            print("alpaca serve: existing server is local; restart explicitly with --remote")
            return cli.FAIL
        print("alpaca serve: already running at %s" % st["url"])
        for line in address_lines(st["url"], st["port"]):
            print(line)
        return cli.PASS
    remote = getattr(args, "remote", False)
    if remote and not files_auth(root):
        print("alpaca serve: remote serving requires configured credentials"
              + (" (this instance is public: a hostname in the workspace registry or a live cloudflared "
                 "rule to its port; write .alpaca/files-auth)" if public else ""))
        return cli.FAIL
    if args.detach:
        st = start_detached(root, port, keep=args.keep, remote=remote)
        if not st:
            print("alpaca serve: the detached server did not answer; read .alpaca/serve.log")
            return cli.FAIL
        print("alpaca serve: detached (pid %s), log .alpaca/serve.log" % st.get("pid"))
        for line in address_lines(st["url"], st["port"]):
            print(line)
        return cli.PASS
    _run(root, port, keep=args.keep, remote=remote)
    return cli.PASS


def _parser(sub):
    p = sub.add_parser("serve")
    p.add_argument("--port", type=int, default=None)
    p.add_argument("--write-services", action="store_true",
                   help="write project-specific user service units under .alpaca/services; do not install")
    p.add_argument("--remote", action="store_true",
                   help="require web credentials continuously, including through a tunnel")
    p.add_argument("--stop", action="store_true")
    p.add_argument("--status", action="store_true")
    p.add_argument("--detach", action="store_true",
                   help="start in its own session, so it outlives this shell")
    p.add_argument("--totp-enroll", action="store_true",
                   help="create an authenticator (TOTP) secret; /login then takes its codes instead of the fixed code")
    p.add_argument("--keep", action="store_true",
                   help="do not retire the server after an idle spell")


cli.register_parser("serve", _parser)
