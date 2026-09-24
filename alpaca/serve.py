"""alpaca serve: read-only localhost live dashboard.

Two pages, one server, behind the web sign-in (`alpaca/weblogin.py`, page `/login`) when a login is
configured. `/` is the workspace hub (`alpaca/web/workspaces.html`), whose Alpaca tile opens the
cockpit at `/hub/` (the operations hub, `alpaca/web/hub.html`; the older
single-file cockpit stays at `/cockpit/legacy/`): the live mission-control landing page, which
reads the seven-key `/board/data.json` payload (`alpaca.export.payload`), so every obligation row,
gate run, pulse figure and chip number is read from the record. `/analytics/` is the session
analytics app. The retired classic board addresses (`/board/`, `/board.html`) answer a 301 to
`/hub/`. A domain profile (alpaca/profile.py) may add read-only routes of its own, for example the
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
import getpass
import glob
import hashlib
import http.server
import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time

from alpaca import cli, db, export, paths, render, util
from alpaca.analytics import build_index, metrics_index, ratecards

POLL_S = 0.7                 # watcher cadence; also the debounce floor for pushes
FOLD_MIN_S = 60              # the watcher refolds analytics at most this often (a fold reads every
                             # transcript: 16 s of CPU on this record, and live sessions change
                             # its inputs every few seconds, so an unthrottled watcher never rests)
BOARD_MIN_S = 15             # the watcher rebuilds the status board at most this often; each new
                             # board is a live push that makes open pages re-render, so match the
                             # pages' own 15 s refresh
GAPS_MIN_S = 900             # without a viewer, recompute the index for the pricing gap notice this often
INDEX_VIEW_S = 600           # a viewer's index request keeps the watcher warming the index this long
DATA_WAIT_S = 60             # an analytics request waits this long for the first fold
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

#: every path the server itself answers. A profile route may never take one of these (nor any
#: path under a reserved prefix): generic routes, and the rules they carry such as the file
#: browser's 403 without a login, always win. A colliding profile route is ignored and reported.
GENERIC_PATHS = frozenset((
    "/", "/index.html", "/login", "/logout", "/workspaces.json", "/hub", "/hub/", "/hub/index.html",
    "/hub/overview.json", "/hub/history.json", "/hub/system.json", "/hub/runs.json",
    "/hub/documents.json", "/hub/document.json", "/hub/session.json", "/hub/analytics.json",
    "/hub/analytics-index.json", "/hub/analytics-children.json",
    "/analytics", "/analytics.html", "/analytics/", "/analytics/index.html", "/analytics/legacy/",
    "/board", "/board/", "/board.html", "/board/index.html", "/board/data.json",
    "/cockpit", "/cockpit/", "/cockpit/index.html", "/cockpit/legacy/",
    "/files/list", "/files/read", "/data.json", "/health.json", "/library.json", "/events", "/file"))
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


class Live:
    """Latest folded data + library, recomputed by the watcher thread only.

    All sqlite work (the fold) happens on the watcher thread so no connection is
    shared across threads; request handlers only read the cached JSON strings.
    """

    def __init__(self, root):
        self.root = root
        self.lock = threading.Lock()
        self.cond = threading.Condition(self.lock)
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
        self._index_lock = threading.Lock()    # one index computation at a time; waiters reuse it
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
        self._board_at = 0.0
        self.started_at = time.time()
        self.checked_at = None
        self.refreshed_at = None
        self.failures = {}
        self.clients = 0
        self.idle_since = time.time()

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

    def board_snapshot(self):
        """Overlay request-time liveness on the immutable cached record projection."""
        from alpaca import sessions_view
        with self.lock:
            board = json.loads(self.board_json)
        current = (board.get("pulse") or {}).get("now") or {}
        now = datetime.datetime.fromtimestamp(time.time(), datetime.timezone.utc).isoformat()
        for session in (current.get("sessions") or {}).get("work") or []:
            session["active"] = sessions_view.is_active(dict(session, **{"class": sessions_view.WORK}), now)
        # No volatile clock stamp: a conditional request only changes at a liveness boundary.
        revision = util.sha256_hex(util.canonical_json({key: value for key, value in board.items() if key != "generated"}))
        return json.dumps(board, ensure_ascii=True, sort_keys=True).encode("utf-8"), revision

    def health_json(self):
        from alpaca import hub
        capture = hub.capture_health(self.root)
        with self.lock:
            projection = dict(self.health, checked_at=self.checked_at, refreshed_at=self.refreshed_at)
            failures = json.loads(json.dumps(self.failures))
        if self.checked_at is not None and time.time() - self.checked_at > max(15, POLL_S * 5):
            projection.update(state="stale", detail="dashboard watcher has not checked inputs recently")
        # Keep legacy state scoped to projection freshness. Overall status has its own key.
        status = "ok" if projection["state"] == "fresh" and capture.get("status") == "ok" else "degraded"
        return json.dumps({**projection, "status": status, "status_scope": "projection_and_collection",
                           "acceptance": hub.acceptance_health(self.root),
                           "transport": {"status": "live", "started_at": self.started_at},
                           "projection": projection, "observability": capture,
                           "server": {"failures": failures, "since": self.started_at,
                                      "scope": "current_process", "persistent_log": "supervisor_stderr"}}, ensure_ascii=True)

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

    def refresh(self, throttle=False, fold=True):
        """Recompute what changed. The watcher passes throttle=True so the analytics fold runs at
        most every FOLD_MIN_S and the board at most every BOARD_MIN_S; a direct call recomputes
        at once. fold=False skips the analytics fold (the server's fast first refresh)."""
        changed = False
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
                        changed = True
                self._board_sig = dsig
                self._board_at = now
            except Exception as e:
                self.failure("projection", e)
                detail = "status board failed: %s" % type(e).__name__
                state = "stale" if self.board_rev else "error"
                changed = self._set_health(state, detail) or changed
                sys.stderr.write("alpaca serve: board failed: %s\n" % detail)
        if fold and dsig is not None and (dsig != self._data_sig or not self.data_rev or retry_degraded) and (
                not throttle or now - self._fold_at >= FOLD_MIN_S):
            self._fold_at = now
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
            finally:
                self.data_ready.set()
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

    def note_index_request(self):
        """The HTTP handler records that a viewer asked for the index; only such a request makes
        the watcher warm it for viewers (a cockpit-only viewer never pays for it)."""
        with self.lock:
            self._index_requested_at = time.time()

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
            value = metrics_index.index(self.root, sessions)
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

    def _warm_index(self):
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
            wanted = viewer or now - self._gaps_at >= GAPS_MIN_S
        running = self._warm_thread is not None and self._warm_thread.is_alive()
        if not stale:
            self._warm_pending = False
            return
        if not wanted or running:
            self._warm_pending = True
            return
        self._warm_pending = False

        def warm():
            try:
                self.analytics_index()
            except Exception as error:
                self.failure("analytics_index", error)
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


FILES_ROOT_DENY = {".git", ".venv", "node_modules", "__pycache__", ".pytest_cache", "analytics"}
FILES_TEXT_EXTS = {".md", ".markdown", ".txt", ".html", ".htm", ".json", ".yaml", ".yml",
                   ".toml", ".cfg", ".ini", ".csv", ".log", ".py", ".sh", ".sv", ".v", ".vh",
                   ".tcl", ".rst", ".sql", ".c", ".h", ".cpp", ".hpp", ".mk", ".cmake", ".txt", ""}
FILES_MAX_BYTES = 512 * 1024


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
    `proofs/` and `wiki/` (so the record db, transcripts, instance and serve state stay closed).
    Secret-shaped names are refused everywhere. A `..` that climbs out of the root is refused by
    the realpath containment check.
    """
    rel = (rel or "").strip().lstrip("/")
    base = os.path.realpath(root)
    full = os.path.realpath(os.path.join(base, rel))
    if not (full == base or full.startswith(base + os.sep)):
        return None
    relnorm = "" if full == base else os.path.relpath(full, base)
    parts = [] if relnorm in ("", ".") else relnorm.split(os.sep)
    if parts and parts[0] in FILES_ROOT_DENY:
        return None
    # Dot-dirs and dot-files are closed everywhere (.claude and .codex hold session transcripts,
    # .git holds history, .env holds secrets). The one exception is `.alpaca`, and under it only
    # `proofs/` and `wiki/`.
    for idx, part in enumerate(parts):
        if part.startswith(".") and not (idx == 0 and part == ".alpaca"):
            return None
    if parts and parts[0] == ".alpaca" and not (len(parts) >= 2 and parts[1] in ("proofs", "wiki")):
        return None
    name = (parts[-1] if parts else "").lower()
    if (name in ("files-auth", "terminal-url", ".env") or name == "alpaca.db"
            or name.endswith((".db", ".sqlite", ".sqlite3", ".pem", ".key", ".token", ".tar.gz"))
            or name.startswith("id_rsa")):
        return None
    return full


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
    if ext not in FILES_TEXT_EXTS:
        return {"error": "not a readable text file", "path": rel, "ext": ext}
    try:
        size = os.path.getsize(full)
        with open(full, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read(FILES_MAX_BYTES + 1)
    except OSError:
        return {"error": "cannot read", "path": rel}
    truncated = len(text) > FILES_MAX_BYTES
    if truncated:
        text = text[:FILES_MAX_BYTES]
    base = os.path.realpath(root)
    return {"path": os.path.relpath(full, base), "size": size, "truncated": truncated, "text": text}


#: Files the public sign-in page and the workspace hub may load before a session exists.
LOGIN_ASSETS = {"vault.css": "text/css; charset=utf-8", "vault.js": "text/javascript; charset=utf-8",
                "vault-globe.js": "text/javascript; charset=utf-8",
                "countries-110m.json": "application/json",
                "vendor/plex-sans.woff2": "font/woff2", "vendor/plex-sans-medium.woff2": "font/woff2",
                "vendor/plex-mono.woff2": "font/woff2"}


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
    own = {"id": ident, "name": (mine or {}).get("name") or "Alpaca",
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


def _cockpit_html():
    """The legacy cockpit page: a live mission-control view over the same runtime and record
    endpoints as the hub. Crosses the whole-page ASCII boundary like the analytics page."""
    path = os.path.join(os.path.dirname(os.path.dirname(build_index.__file__)), "web", "cockpit.html")
    return render.ascii_entities(util.read_text(path))


def _serve_html(root):
    tpl = util.read_text(os.path.join(os.path.dirname(build_index.__file__), "app.html"))
    html = tpl.replace("__DATA__", "null")
    health = """<div id="alpaca-serve-health" role="status" aria-live="polite" style="position:fixed;right:12px;bottom:12px;z-index:99;padding:6px 9px;border-radius:5px;background:#17324d;color:#ffffff;font:12px sans-serif">Dashboard health: loading</div>
<script>(function(){var e=document.getElementById('alpaca-serve-health');function refresh(){fetch('/health.json').then(function(r){if(!r.ok)throw Error('health status '+r.status);return r.json()}).then(function(h){e.textContent='Page: '+(h.projection?.state||h.state||'unknown')+'; capture: '+(h.observability?.status||'unavailable')+(h.detail?(' ('+h.detail+')'): '');e.style.background=(h.status==='ok'?'#176b3a':'#8a5a00')}).catch(function(){e.textContent='Dashboard health: unavailable';e.style.background='#8a1f2d'}).finally(function(){setTimeout(refresh,1500)})}refresh()}())</script>"""
    html = html.replace("</body>", health + "\n</body>")
    # M4.9: the served page crosses the same whole-page ASCII boundary as the written analytics
    # page (render.ascii_entities), so the phone-safe ASCII rule holds in one place.
    return render.ascii_entities(html)


def make_handler(live, root, remote=False):
    from alpaca import weblogin
    page = _serve_html(root).encode("utf-8")
    throttle = weblogin.Throttle()

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
            except (BrokenPipeError, ConnectionResetError) as error:
                live.failure("disconnect", error)
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
                # The landing page is the workspace hub; the Alpaca tile opens the cockpit at /hub/.
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
            if path == "/analytics/legacy/":
                return self._send(page, "text/html; charset=utf-8")
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
            if path == "/cockpit/legacy/":
                live.touch()
                return self._send(_cockpit_html().encode("utf-8"), "text/html; charset=utf-8")
            if path == "/board/data.json":
                live.touch()
                body, etag = live.board_snapshot()
                return self._send(body, "application/json", etag=etag, cache="no-cache")
            if path == "/files/list" or path == "/files/read":
                live.touch()
                return self._files_api(path, parse_qs(u.query))
            if path == "/data.json":
                live.data_ready.wait(DATA_WAIT_S)    # right after a start the first fold is running
                with live.lock:
                    body, etag = live.data_json.encode("utf-8"), live.data_rev
                return self._send(body, "application/json", etag=etag, cache="no-cache")
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
            """Read services and an explicit static allowlist for the operations workbench."""
            from pathlib import Path
            from alpaca import hub
            from alpaca.analytics.detail import detail
            one = lambda key, default="": (query.get(key) or [default])[0]
            web = Path(__file__).parent / "web"
            assets = {"hub.css": "text/css; charset=utf-8", "hub.js": "text/javascript; charset=utf-8",
                      "analytics.js": "text/javascript; charset=utf-8", "analytics.css": "text/css; charset=utf-8",
                      "cockpit.js": "text/javascript; charset=utf-8", "cockpit.css": "text/css; charset=utf-8",
                      "runlog.js": "text/javascript; charset=utf-8", "runlog.css": "text/css; charset=utf-8",
                      "live.js": "text/javascript; charset=utf-8", "live.css": "text/css; charset=utf-8",
                      "theme.js": "text/javascript; charset=utf-8", "theme.css": "text/css; charset=utf-8",
                      "icons.js": "text/javascript; charset=utf-8",
                      "liveflags.js": "text/javascript; charset=utf-8", "morph.js": "text/javascript; charset=utf-8",
                      "system.js": "text/javascript; charset=utf-8",
                      "vendor/plex-sans.woff2": "font/woff2", "vendor/plex-sans-medium.woff2": "font/woff2",
                      "vendor/plex-mono.woff2": "font/woff2"}
            if path in ("/hub", "/hub/", "/hub/index.html"):
                return self._send((web / "hub.html").read_bytes(), "text/html; charset=utf-8")
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
                name = path[len("/hub/assets/"):]
                if name not in assets or not (web / name).is_file():
                    return self._send(b"not found", "text/plain", 404)
                return self._send((web / name).read_bytes(), assets[name])
            try:
                if path == "/hub/overview.json":
                    value = hub.overview(root)
                elif path == "/hub/history.json":
                    value = hub.history(root, before=one("before") or None, limit=int(one("limit", "50")),
                                        query=one("q"), kind=one("kind", "work"), session=one("session"), ref=one("ref"))
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
                        body, etag = live.analytics_index()
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
            if weblogin.valid(root, want, weblogin.read_cookie(root, self.headers.get("Cookie"))):
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
            except (BrokenPipeError, ConnectionResetError) as error:
                live.failure("disconnect", error)
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
            if not weblogin.credential_ok(want, user, code):
                throttle.fail(client)
                return self._json({"error": "Access code not accepted."}, 401)
            throttle.succeed(client)
            body = json.dumps({"ok": True}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Set-Cookie", weblogin.set_cookie(root, weblogin.issue(root, want), self._secure()))
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return None

        def _files_api(self, path, query):
            """The gated file browser. Closed (403) unless a login is configured; otherwise every
            request carries HTTP Basic credentials that match `files_auth`. The allowlist in
            `_files_resolve` is what keeps the record db, transcripts and secrets out of reach."""
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
            except Exception as error:
                live.failure("profile_route", error)
                return self._json({"error": "The profile could not answer. Try again or check server health."}, 503)
            return self._json(value, code)

        def _file(self, rel):
            if not rel:
                return self._send(b"missing path", "text/plain", 400)
            full = os.path.realpath(os.path.join(root, rel))
            base = os.path.realpath(root)
            if not (full == base or full.startswith(base + os.sep)):
                return self._send(b"forbidden", "text/plain", 403)
            for name in PRIVATE_DIRS:
                closed = os.path.realpath(os.path.join(base, name))
                if full == closed or full.startswith(closed + os.sep):
                    return self._send(b"not found", "text/plain", 404)
            ext = os.path.splitext(full)[1].lower()
            if ext not in READABLE or not os.path.isfile(full):
                return self._send(b"not found", "text/plain", 404)
            try:
                with open(full, "rb") as fh:
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
    probe = ("import alpaca.serve, alpaca.hub, alpaca.work_record, alpaca.export, alpaca.proof")
    try:
        done = subprocess.run([sys.executable, "-c", probe], cwd=root, capture_output=True,
                              text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as e:
        return False, str(e)
    return done.returncode == 0, (done.stderr or "").strip()[-400:]


def _reexec_argv():
    """The command line that started this server, as `python -m alpaca ...`."""
    return [sys.executable, "-m", "alpaca"] + sys.argv[1:]


def _run(root, port, keep=False, remote=False):
    if remote and not files_auth(root):
        raise ValueError("Remote serving requires configured credentials")
    # Bind (or adopt the socket a reload handed over) before any refresh, and serve after the
    # fast one: the board, library and profile live state take well under a second. The analytics fold
    # (16 s on this record) runs on the watcher; analytics requests wait for it (DATA_WAIT_S).
    httpd, port = _bind(port)
    live = Live(root)
    live.refresh(fold=False)
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
            os.execv(sys.executable, _reexec_argv())
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
        try:
            subprocess.Popen([sys.executable, "-m", "alpaca", "serve", "--port", str(port)]
                             + (["--remote"] if public else []),
                             cwd=root, stdout=fh, stderr=fh, stdin=subprocess.DEVNULL,
                             start_new_session=True, close_fds=True)
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
        cmd = [sys.executable, "-m", "alpaca", "serve", "--port", str(port)]
        if keep:
            cmd.append("--keep")
        if remote:
            cmd.append("--remote")
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
    try:
        subprocess.Popen(cmd, cwd=root, stdout=fh, stderr=fh, stdin=subprocess.DEVNULL,
                         start_new_session=True, close_fds=True)
    except OSError:
        return False
    finally:
        if fh is not subprocess.DEVNULL:
            fh.close()
    return True


def service_units(root, *, port=None, python=None):
    """Reproducible user-service contracts. Generation neither installs nor starts units.
    The dashboard port is `port`, else this instance's registered port (alpaca workspace add), else
    _default_port(root); never a reserved port by default."""
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
    executable = python or os.path.join(root, "bin", "alpaca-python")
    ident = hashlib.sha256(root.encode()).hexdigest()[:12]
    prefix = "alpaca-" + ident
    units = {}
    for role, arguments in (("dashboard", "serve --keep --remote --port %d" % int(port)),
                            ("collector", "collect watch --interval 15")):
        units[prefix + "-" + role + ".service"] = (
            "[Unit]\nDescription=Alpaca %s (%s)\nStartLimitIntervalSec=120\nStartLimitBurst=10\n\n"
            "[Service]\nType=simple\nWorkingDirectory=%s\nEnvironmentFile=-%s\n"
            "%sExecStart=%s -m alpaca %s\nRestart=%s\nRestartSec=5\n"
            "TimeoutStopSec=30\nKillMode=control-group\nUMask=0077\n"
            "StandardOutput=journal\nStandardError=journal\nSyslogIdentifier=%s-%s\n\n"
            "[Install]\nWantedBy=default.target\n" % (
                role, ident, root.replace("%", "%%"), os.path.join(root, ".alpaca", "service.env").replace("%", "%%"),
                # The dashboard is restarted on any exit, including a clean `alpaca serve --stop`,
                # and keeps its exact port so the tunnel always finds it.
                "Environment=ALPACA_SERVE_STRICT_PORT=1\n" if role == "dashboard" else "",
                quote(executable), arguments, "always" if role == "dashboard" else "on-failure",
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
    p.add_argument("--keep", action="store_true",
                   help="do not retire the server after an idle spell")


cli.register_parser("serve", _parser)
