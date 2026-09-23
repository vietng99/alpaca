"""The domain seam: the one module generic Alpaca code uses to reach a domain.

Alpaca core knows the record, sessions, ops, tasks, proofs and the web workspace. A domain (a chip
flow, a data pipeline, a document process) plugs in as a profile. The project names it in
`project.yaml`:

    profile: mydomain.profile

The value is a dotted name that must resolve to a file inside the project root
(`mydomain/profile.py` or `mydomain/profile/__init__.py`); nothing on sys.path is consulted, so
an installed or standard-library module can never be named. The file is loaded under a module
name unique to its root, so two projects in one process never share a profile. The module
exposes `PROFILE`, an instance of `Profile` (or a subclass of it, which is instantiated once). A
project without the key runs on `EMPTY`, and every hook of `EMPTY` returns an empty value and
never raises, so generic Alpaca behaves the same with or without a domain.

Generic modules never import a domain package. They call `load(root)` (or `for_conn(conn)`
when only a record connection is at hand) and then one hook. A loaded profile is wrapped: a hook
that raises, or answers with the wrong type, reads as its empty value and is recorded
(`errors(root)`). A gate that must fail closed calls `strict(root, hook, ...)` instead. A named
profile that does not load falls back to `EMPTY`, is never cached (the next call retries), and
is reported everywhere a reader looks: `error(root)`, `alpaca doctor`, the hub overview and cockpit,
the SessionStart boot block, and a BLOCKED `alpaca proof seal`.

Each hook's docstring names the generic call sites that use it. A profile overrides only the
hooks it needs.
"""
from __future__ import annotations

import importlib
import importlib.machinery
import importlib.util
import os
import sys
import threading

KEY = "profile"


class Profile:
    """The hook set with empty defaults. Subclass it and override what the domain supplies."""

    #: a short name for the domain, shown on the hub and in `alpaca doctor`. Empty: no domain.
    name = ""

    # ------------------------------------------------------------------ the record and the CLI
    def stages(self):
        """The domain's stage names, in run order. Used by alpaca/taskcontract.py (the --stage flag
        of `alpaca task add` and `alpaca task contract`) and alpaca/hub.py (the rows a failed check blocks),
        and published to the web pages through `web()` meta."""
        return ()

    def verbs(self):
        """Extra CLI verbs: {verb: {"module": dotted module, "keys": (output keys), "group": str}}.
        Used by alpaca/cli.py (the module is imported so it registers the verb) and alpaca/surface.py
        (the verb joins the declared dispatch table)."""
        return {}

    def events(self):
        """How the domain's record events are read. A dict with optional lists:
        "service_sessions" (synthetic writer ids, never counted as sittings),
        "side_effect_kinds" (kinds a closing session writes as a side effect, not work) and
        "observation_prefixes" (kind prefixes whose data the wiki keeps as untrusted JSON).
        Used by alpaca/sessions_view.py, alpaca/hub.py (history work filter) and alpaca/wiki/ingest/drain.py."""
        return {}

    def summarize(self, event):
        """One short line for a domain event, or "". Used by alpaca/sessions_view.py (summary_of:
        the recent-events lines of data.json)."""
        return ""

    def next_action(self, root):
        """The pad's next-action line when the domain has one to give first, or "". Used by
        alpaca/pad.py (next_action)."""
        return ""

    def boot_lines(self, root):
        """Extra lines for the SessionStart boot block, before first-contact routing. Used by
        alpaca/hooks/session_start.py."""
        return []

    def doctor_checks(self, root, conn):
        """Read-only checks as (name, level, detail) tuples, level in ok/warn/error. Used by
        alpaca/doctor.py."""
        return []

    def refresh(self, root, session, events=None):
        """Refresh the domain's projections from the record. `events` is None at a session end
        (alpaca/hooks/session_end.py) and the new record rows when the observability collector
        calls (alpaca/observability/consumers.py, which retries while the result carries
        {"status": "error"}). Returns None or a result dict."""
        return None

    # ---------------------------------------------------------------------------------- proof
    def evidence_file(self, root, kind, ident):
        """The file behind a `receipt:<id>` or `job:<id>` evidence pointer, or None. Used by
        alpaca/proof.py (resolve_evidence and verify)."""
        return None

    def proof_lines(self, events):
        """Markdown lines for the run section of a proof scaffold, from the record events in the
        report's window. Empty leaves the section out. Used by alpaca/proof.py (the scaffold)."""
        return []

    def failed_runs(self, root, first, now):
        """Runs that ended without PASS inside [first, now]: [{"job_id", "stage", "verdict",
        "reason", "when"}]. A seal refuses a report that names none of them. Used by
        alpaca/proof.py (inspect, before a seal)."""
        return []

    # ---------------------------------------------------------------------------------- files
    def paths(self):
        """Project paths the domain owns, as lists of project-relative paths:
        "documents" (report roots the hub library catalogs, as runbooks),
        "tools" (tool installations: never backed up, shown only in the library's tool view),
        "backup" (runtime directories a backup copies), and
        "archives" (glob patterns of archive manifests the artifact catalog imports).
        Used by alpaca/hub.py, alpaca/backup.py, alpaca/artifacts.py and alpaca/observability/maintenance.py."""
        return {}

    # ---------------------------------------------------------------------------- projections
    def stage_rows(self, conn):
        """The latest result per stage folded from the record: [{"stage", "verdict", "reason",
        "ts", "receipt_id"}]. Used by alpaca/export.py (`pulse.now.stages` in data.json)."""
        return []

    def metrics(self, conn):
        """The domain's numbers folded from the record, for the `metrics` key of data.json. Used
        by alpaca/export.py (payload) and read by the legacy cockpit page."""
        return {}

    # -------------------------------------------------------------------------- hub and serve
    def check(self, root):
        """The current check of every stage: [{"stage", "verdict", "reason", "receipt_id"}],
        optionally with "recorded_status", "recorded_at" and "recorded_session". Read-only. An
        exception reads as every stage BLOCKED. Used by alpaca/hub.py (overview, acceptance_health)."""
        return []

    def runs(self, root):
        """Recorded runs for the hub's Runs page: {"items": [...], "total": n, "errors": [...]}.
        An item carries "id", "job_id", "verdict", "stage", "stages" and "started_at". Used by
        alpaca/hub.py (runs: /hub/runs.json and the overview's active jobs)."""
        return {"items": [], "total": 0, "errors": []}

    def acceptance(self, root, conn, checked, stamp):
        """Acceptance outcomes for the hub's Work page, judged against `checked` (the rows of
        `check`). Used by alpaca/hub.py (overview)."""
        return []

    def capture(self, root, conn):
        """The domain's capture health for the hub overview. A dict with optional keys:
        "status" (ok, pending, error, missing), "cutoff" (event id its capture covers),
        "resolved_ops" (ops whose capture-failed rows up to the cutoff are resolved),
        "error_kinds" (extra event kinds shown as capture errors after the cutoff) and
        "errors" (extra error rows). Used by alpaca/hub.py (overview)."""
        return {}

    def routes(self):
        """Extra read-only HTTP GET routes: {path: handler(root, query)}. The handler returns
        (value, code); the value is sent as JSON. Every route sits behind the web sign-in. Used
        by alpaca/serve.py."""
        return {}

    def live_revision(self, root):
        """A cheap revision string of the domain's live state. When it moves the live server
        pushes an SSE `job` event. Used by alpaca/serve.py (Live.refresh)."""
        return ""

    def web(self):
        """What the web pages show for the domain: {"pages": [...], "cards": [...],
        "stage_names": {...}, "assets": {...}}. "pages" turns on optional hub pages ("runs",
        "live"); "cards" lists cockpit cards {"id", "title", "module"} whose module is one of
        "assets" (published name -> absolute file path, served at /hub/assets/profile/<name>).
        Used by alpaca/hub.py (overview: the "profile" block) and alpaca/serve.py (the assets)."""
        return {}


#: the profile of a project that names none. Every hook returns an empty value.
EMPTY = Profile()

#: every hook, with the types its answer may take and the empty value it falls back to. A loaded
#: profile is wrapped so an exception, or an answer of another type, reads as the empty value and
#: is recorded against the hook (`errors`). A generator answer to a list hook is materialized.
_SEQ, _MAP, _STR, _NONE = (list, tuple), (dict,), (str,), (type(None),)
HOOKS = {
    "stages": (_SEQ, ()), "verbs": (_MAP, {}), "events": (_MAP, {}), "summarize": (_STR, ""),
    "next_action": (_STR, ""), "boot_lines": (_SEQ, []), "doctor_checks": (_SEQ, []),
    "refresh": (_MAP + _NONE, None), "evidence_file": (_STR + _NONE, None),
    "proof_lines": (_SEQ, []), "failed_runs": (_SEQ, []), "paths": (_MAP, {}),
    "stage_rows": (_SEQ, []), "metrics": (_MAP, {}), "check": (_SEQ, []),
    "runs": (_MAP, {"items": [], "total": 0, "errors": []}), "acceptance": (_SEQ, []),
    "capture": (_MAP, {}), "routes": (_MAP, {}), "live_revision": (_STR, ""), "web": (_MAP, {}),
}


class ProfileError(Exception):
    """A strict hook call could not get a trustworthy answer: the named profile did not load, or
    the hook raised or answered with the wrong type. Raised only by `strict`."""


def _empty(name):
    import copy
    return copy.deepcopy(HOOKS[name][1])


def _answer(name, value):
    """(value, problem): the value when its type is right, else (empty, why)."""
    kinds = HOOKS[name][0]
    if kinds == _SEQ and not isinstance(value, (list, tuple, str, bytes, dict)) \
            and hasattr(value, "__iter__"):
        value = list(value)
    if isinstance(value, kinds):
        return value, None
    return _empty(name), "returned %s, expected %s" % (
        type(value).__name__, " or ".join(k.__name__ for k in kinds))


class _Guarded(Profile):
    """A loaded profile behind the seam: every hook call is contained (see HOOKS)."""

    def __init__(self, raw, root):
        self._raw, self._root = raw, root
        self.name = str(getattr(raw, "name", "") or "")

    def _call(self, name, *args):
        try:
            value, problem = _answer(name, getattr(self._raw, name)(*args))
        except Exception as exc:
            value, problem = _empty(name), "%s: %s" % (type(exc).__name__, exc)
        _note(self._root, name, problem)
        return value


def _hook(name):
    def call(self, *args):
        return self._call(name, *args)
    call.__name__ = name
    call.__doc__ = getattr(Profile, name).__doc__
    return call


for _name in HOOKS:
    setattr(_Guarded, _name, _hook(_name))

_LOCK = threading.Lock()
_CACHE = {}      # root -> ((dotted, config stamp, file stamp), profile)
_FAILED = {}     # root -> why the named profile did not load (never cached as a success)
_ERRORS = {}     # root -> {hook: problem} for the latest call of each hook


def _note(root, hook, problem):
    with _LOCK:
        found = _ERRORS.setdefault(root, {})
        if problem:
            found[hook] = problem
        else:
            found.pop(hook, None)


def note(root, hook, problem):
    """Record a problem against a hook for `errors` (a call site that rejects part of an answer,
    such as a route that collides with a generic route). None clears it."""
    if root:
        _note(os.path.abspath(root), hook, problem)


def _config(root):
    """(module path or None, stamp). A missing or broken project.yaml names no profile."""
    path = os.path.join(root, "project.yaml")
    try:
        st = os.stat(path)
    except OSError:
        return None, None
    stamp = (st.st_mtime_ns, st.st_size)
    try:
        import yaml
        with open(path, encoding="utf-8") as fh:
            cfg = yaml.safe_load(fh) or {}
    except Exception:
        return None, stamp
    value = cfg.get(KEY) if isinstance(cfg, dict) else None
    return (value.strip() if isinstance(value, str) and value.strip() else None), stamp


def _locate(root, dotted):
    """The files a dotted name resolves to inside the project root: [(package dir, __init__ or
    None)...] for each package part and the module file itself. Refuses a name that is not a
    plain dotted identifier or whose file does not sit inside the root once symlinks are read,
    so an installed or standard-library module can never be named."""
    refuse = "profile %s does not resolve to a file inside the project root" % dotted
    parts = dotted.split(".")
    if not parts or not all(p.isidentifier() for p in parts):
        raise ImportError(refuse)
    real_root = os.path.realpath(root)
    base = os.path.join(root, *parts)
    for candidate in (base + ".py", os.path.join(base, "__init__.py")):
        real = os.path.realpath(candidate)
        if os.path.isfile(candidate) and real.startswith(real_root + os.sep):
            packages = []
            for n in range(1, len(parts)):
                directory = os.path.join(root, *parts[:n])
                init = os.path.join(directory, "__init__.py")
                if not os.path.realpath(directory).startswith(real_root + os.sep):
                    raise ImportError(refuse)
                packages.append((directory, init if os.path.isfile(init) else None))
            return packages, candidate
    raise ImportError(refuse)


def _stamp(path):
    try:
        st = os.stat(path)
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def _import(root, dotted):
    """The PROFILE the named file exposes, as an instance of Profile. Raises on any problem.

    The file is loaded under a module name unique to the project root, so two roots in one
    process never share a module and nothing on sys.path decides what loads. Packages on the
    way are loaded under the same prefix, so relative imports inside the profile work; an
    absolute import of the domain's own package is the profile's own business."""
    import hashlib
    import importlib.util
    packages, target = _locate(root, dotted)
    prefix = "_alpaca_profile_%s" % hashlib.sha256(os.path.realpath(root).encode()).hexdigest()[:16]
    parts = dotted.split(".")
    top = importlib.util.module_from_spec(importlib.machinery.ModuleSpec(prefix, None, is_package=True))
    top.__path__ = [root]
    sys.modules[prefix] = top
    for n, (directory, init) in enumerate(packages, 1):
        name = ".".join([prefix] + parts[:n])
        if init:
            spec = importlib.util.spec_from_file_location(name, init, submodule_search_locations=[directory])
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            spec.loader.exec_module(module)
        else:
            module = importlib.util.module_from_spec(importlib.machinery.ModuleSpec(name, None, is_package=True))
            module.__path__ = [directory]
            sys.modules[name] = module
    name = ".".join([prefix] + parts)
    is_package = target.endswith("__init__.py")
    spec = importlib.util.spec_from_file_location(
        name, target, submodule_search_locations=[os.path.dirname(target)] if is_package else None)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except (Exception, SystemExit):
        sys.modules.pop(name, None)
        raise
    found = getattr(module, "PROFILE", None)
    if isinstance(found, type) and issubclass(found, Profile):
        found = found()
    if not isinstance(found, Profile):
        raise TypeError("%s.PROFILE is not a alpaca.profile.Profile" % dotted)
    return found, target


def _resolve(root):
    """(profile, load error). A success is cached per (root, project.yaml stamp, module file
    stamp); a failure is never cached, so the next call tries again."""
    root = os.path.abspath(root)
    dotted, stamp = _config(root)
    if dotted is None:
        with _LOCK:
            _FAILED.pop(root, None)
        return EMPTY, None
    with _LOCK:
        cached = _CACHE.get(root)
    if cached and cached[0][:2] == (dotted, stamp) and _stamp(cached[0][2]) == cached[0][3]:
        return cached[1], None
    try:
        raw, target = _import(root, dotted)
    except (Exception, SystemExit) as exc:       # a profile that exits on import is a failed load
        problem = "profile %s did not load: %s: %s" % (dotted, type(exc).__name__, exc)
        with _LOCK:
            _CACHE.pop(root, None)
            _FAILED[root] = problem
        return EMPTY, problem
    found = _Guarded(raw, root)
    with _LOCK:
        _CACHE[root] = ((dotted, stamp, target, _stamp(target)), found)
        _FAILED.pop(root, None)
        _ERRORS.pop(root, None)
    return found, None


def load(root):
    """The profile of the project at `root`: its named profile (every hook contained), or
    EMPTY. Never raises."""
    if not root:
        return EMPTY
    try:
        return _resolve(root)[0]
    except Exception:
        return EMPTY


def error(root):
    """Why the named profile did not load, or None when it loaded or none is named."""
    if not root:
        return None
    try:
        return _resolve(root)[1]
    except Exception as exc:
        return "profile lookup failed: %s: %s" % (type(exc).__name__, exc)


def errors(root):
    """{hook: problem} for the hooks whose latest call raised or answered with the wrong type
    (plus "routes" when a route collided with a generic one). Empty when all is well."""
    if not root:
        return {}
    with _LOCK:
        return dict(_ERRORS.get(os.path.abspath(root), {}))


def problems(root):
    """Every profile problem as lines: the load error first, then each hook error."""
    out = [error(root)] if error(root) else []
    return out + ["profile hook %s: %s" % (hook, why) for hook, why in sorted(errors(root).items())]


def strict(root, name, *args):
    """Call one hook and insist on a trustworthy answer, for a gate that must fail closed (the
    proof failed-run check). Raises ProfileError when the named profile did not load, or the
    hook raised or answered with the wrong type."""
    problem = error(root)
    if problem:
        raise ProfileError(problem)
    prof = load(root)
    raw = prof._raw if isinstance(prof, _Guarded) else prof
    try:
        value, problem = _answer(name, getattr(raw, name)(*args))
    except Exception as exc:
        problem = "%s: %s" % (type(exc).__name__, exc)
    if problem:
        if isinstance(prof, _Guarded):
            _note(prof._root, name, problem)
        raise ProfileError("profile hook %s: %s" % (name, problem))
    return value


def listed(mapping, key):
    """mapping[key] as a list of strings when it is a list or tuple, else []. For the list
    values inside `paths()` and `events()`, so a string or a number never reads as a list."""
    value = mapping.get(key) if isinstance(mapping, dict) else None
    if not isinstance(value, (list, tuple)):
        return []
    return [str(v) for v in value if isinstance(v, (str, int, float)) and str(v)]


def root_of(conn):
    """The project root behind a record connection (`<root>/.alpaca/alpaca.db`), or None."""
    try:
        for row in conn.execute("PRAGMA database_list"):
            if row[1] == "main" and row[2]:
                return os.path.dirname(os.path.dirname(os.path.abspath(row[2])))
    except Exception:
        pass
    return None


def for_conn(conn):
    """The profile of the project whose record `conn` holds. EMPTY for an in-memory record."""
    return load(root_of(conn))


def current():
    """The profile of the project the process runs in (cwd, then ALPACA_ROOT), or EMPTY."""
    try:
        from alpaca import paths
        return load(paths.root())
    except Exception:
        return EMPTY


def web_meta(root):
    """The profile block of the hub overview: the web() meta with every key present, plus the
    name, the stages and any profile problem ("error": the load error, "hook_errors": per hook).
    Always JSON-safe; empty lists for the empty profile."""
    prof = load(root)
    meta = prof.web()
    meta = meta if isinstance(meta, dict) else {}
    stages = prof.stages()
    stages = [str(s) for s in stages] if isinstance(stages, (list, tuple)) else []
    assets = meta.get("assets") if isinstance(meta.get("assets"), dict) else {}
    cards = []
    for card in meta.get("cards") if isinstance(meta.get("cards"), (list, tuple)) else []:
        if isinstance(card, dict) and card.get("id") and card.get("module") in assets:
            cards.append({"id": str(card["id"]), "title": str(card.get("title") or card["id"]),
                          "module": "/hub/assets/profile/" + str(card["module"])})
    names = meta.get("stage_names") if isinstance(meta.get("stage_names"), dict) else {}
    pages = meta.get("pages") if isinstance(meta.get("pages"), (list, tuple)) else []
    block = {"name": str(getattr(prof, "name", "") or ""),
             "pages": [str(p) for p in pages if p in ("runs", "live")],
             "cards": cards, "stages": stages,
             "stage_names": {str(k): str(v) for k, v in names.items()}}
    problem, hooks = error(root), errors(root)
    if problem:
        block["error"] = problem
    if hooks:
        block["hook_errors"] = hooks
    return block


def asset(root, name):
    """The absolute file a profile publishes as web asset `name`, or None."""
    web = load(root).web()
    assets = web.get("assets") if isinstance(web, dict) else None
    path = assets.get(name) if isinstance(assets, dict) else None
    return path if isinstance(path, str) and os.path.isfile(path) else None
