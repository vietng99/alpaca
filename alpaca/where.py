"""Honest WHERE derivation (M4.10).

An event's project is the nearest ancestor of the TARGET that carries a project marker
(the manifest), never the shell cwd (absorb-gap AG-M16, spec 5.1:204-209). A tool call whose
target lies outside the project stamps the project of the target rather than the directory the
shell happens to sit in; a session opened one level above the root still attributes an event
that touches a file inside the root to the root. A command string or a URL is display, never an
attribution source, and a target with no marker above it attributes nothing rather than guessing.

Interfaces (spec 5.14:616-618, 634-635; consumed by the heartbeat writer and the analytics):

  derive(target_path)  -> project_root | None     walk up from the target to the marker
  attribute(event)     -> project_id | None        the id of the project owning the target
  project_id(root)     -> project_id | None         the stable id, reconciled across renames
"""
import os

from alpaca import paths, project

# The fields of a tool call that name a real filesystem target and so may carry attribution.
# Read in this fixed order, so a call carrying both a file_path and a cwd attributes to the
# file, not the shell. `cwd` is deliberately absent: AG-M16 is "never the shell cwd".
TARGET_FIELDS = ("file_path", "path", "notebook_path", "target")

# The fields that are display only. A command string, a URL, a search pattern or a query is
# shown to a reader; it is never walked for a project marker. Named here so the exclusion is
# data, not an accident of which keys TARGET_FIELDS happens to omit.
DISPLAY_FIELDS = ("command", "url", "pattern", "query", "skill", "description")


def derive(target_path):
    """The project root that owns `target_path`: the nearest ancestor directory carrying the
    manifest, walking up from the target (from its own directory when it names a file). None
    when no marker stands above it, so an unmarked target attributes nothing rather than a guess.

    Rename-safe: the marker name is `paths.MANIFEST`, never a literal folder name or an
    absolute path baked into this file.
    """
    if not target_path:
        return None
    cur = os.path.abspath(os.path.expanduser(str(target_path)))
    if not os.path.isdir(cur):
        cur = os.path.dirname(cur)
    while True:
        if os.path.isfile(os.path.join(cur, paths.MANIFEST)):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            return None
        cur = parent


def project_id(root):
    """The stable id of the project rooted at `root`: the `project_id` stamped in its
    `project.yaml`, or the root's absolute path when the file carries none. None when `root` is
    None. The stamped id is what lets a renamed folder keep its earlier sessions: the id
    survives a rename, the path does not (spec 5.14:634-635)."""
    if not root:
        return None
    try:
        pid = project.load(root).get("project_id")
    except Exception:
        pid = None
    return pid or os.path.abspath(root)


def _target_of(event):
    """The first path-like field of an event, or None when it carries only display fields.

    Looks in the event itself and in a nested tool-input mapping (a raw hook payload keeps the
    fields under `tool_input`; a folded analytics block keeps them under `input`), so one
    function serves both the heartbeat writer and the analytics.
    """
    if not isinstance(event, dict):
        return None
    scopes = [event]
    for nest in ("tool_input", "input", "data"):
        sub = event.get(nest)
        if isinstance(sub, dict):
            scopes.append(sub)
    for scope in scopes:
        for k in TARGET_FIELDS:
            v = scope.get(k)
            if v:
                return str(v)
    return None


def attribute(event):
    """The project id an event attributes to: the id of the project owning its target, derived
    from the target rather than the shell cwd. A display-only event (a command or a URL) or a
    target with no marker above it attributes nothing (None)."""
    return project_id(derive(_target_of(event)))
