"""Neutralisation of externally sourced values at render time (M4.9).

Review cards carry values from the product tree and from messages, and every rendered surface
shows them, so an externally sourced value is neutralised before it enters a rendered surface.

A rendered surface (RESUME.md, board.json, data.json, the analytics page) carries values that
did NOT come from the harness: an op intent, a task statement, a message body, a project name.
A raw pipe, newline, `<script>` tag or non-ASCII byte in one of those values can, on its way
onto a surface, split a table row, forge a column, inject live markup, or turn to garbage on a
phone that misreads the encoding. This module is the ONE boundary every such value crosses on
its way in, so no renderer neutralises by hand and no renderer forgets.

The boundary is shaped to its surface, and every shape is REVERSIBLE or lossless, because
neutralisation must protect the surface without destroying the value the surface exists to show:

  * `cell(value)`  -- a table / markdown surface (RESUME.md). Escapes backslash, pipe and the
    two line breaks to a declared backslash form that carries NO raw pipe and NO raw newline, so
    a hostile value can neither split a row nor forge a column. `uncell` reverses it byte for
    byte, so the escape is lossless (Done-when clauses 1 and 2).
  * `html_text(value)` -- a page surface (the analytics page). HTML-escapes the five markup
    characters, then turns every remaining non-ASCII character into a numeric entity, so the
    output is pure ASCII and carries no live markup from the value (clauses 4 and 5). A phone
    that ignores the charset meta still shows the text.
  * `for_json(value)` -- a JSON surface (board.json, data.json). JSON's own encoder is the
    neutraliser here: `json.dumps` escapes the value and `json.loads` returns it byte for byte,
    so the boundary is identity and the parsed field equals the original (clause 3). The
    passthrough exists so a JSON writer routes through this module like every other surface, and
    the wiring census (clause 6) can see it do so.

`ascii_entities(text)` is the final ASCII pass a page renderer applies to a whole built page
(after templating), turning any stray non-ASCII byte into a numeric entity; the analytics page
and the live server share it so the ASCII rule holds in one place.

The declared countersign `ref` table lives here too (Step 5): the two owner-countersign refs in
M1-M4 are told apart by `ref`, never by kind, and each gate reads only its own. The release
door consumes `render-neutralisation`; `alpaca op close` for M4 consumes `manual-phone-read`.
"""
from __future__ import annotations

import html
import re

# --------------------------------------------------------------------- the table-cell boundary
#: The declared cell escape, applied in this order (backslash FIRST so it can never double-escape
#: a later replacement). Each pair is (raw, escaped); `uncell` reverses the whole set losslessly.
#: The pipe maps to a backslash form that carries NO `|` byte, so an escaped cell adds zero pipe
#: delimiters to its row; the two line breaks map to forms that carry no newline, so an escaped
#: cell can neither split its row nor merge with the next.
_CELL_ESCAPES = (
    ("\\", "\\\\"),
    ("|", "\\p"),
    ("\r", "\\r"),
    ("\n", "\\n"),
)
_UNCELL = {"\\": "\\", "p": "|", "r": "\r", "n": "\n"}


def _as_text(value) -> str:
    """A value as text. None renders as the empty string (an absent field is empty, never the
    literal 'None'); everything else is stringified before it crosses the boundary."""
    return "" if value is None else str(value)


def cell(value) -> str:
    """Neutralise `value` for a table / markdown cell: reversible, lossless, pipe-safe and
    newline-safe. A benign value (no backslash, pipe or line break) is returned unchanged, so a
    surface of benign values renders exactly as before this boundary existed."""
    s = _as_text(value)
    for raw, esc in _CELL_ESCAPES:
        s = s.replace(raw, esc)
    return s


def uncell(s: str) -> str:
    """Reverse `cell` byte for byte. Scans the declared backslash escapes left to right; because
    `cell` only ever emits a backslash before one of the declared follow bytes, and escapes a
    literal backslash first, the reversal is unambiguous and lossless."""
    out = []
    i = 0
    n = len(s)
    while i < n:
        c = s[i]
        if c == "\\" and i + 1 < n and s[i + 1] in _UNCELL:
            out.append(_UNCELL[s[i + 1]])
            i += 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


# --------------------------------------------------------------------- the HTML-text boundary
_NON_ASCII = re.compile(r"[^\x00-\x7F]")


def ascii_entities(text: str) -> str:
    """Turn every non-ASCII character in an already-built string into a numeric entity, so the
    result is pure ASCII. Used both per-value (`html_text`) and as a whole-page final pass."""
    return _NON_ASCII.sub(lambda m: "&#%d;" % ord(m.group()), text)


def html_text(value) -> str:
    """Neutralise `value` for an HTML page surface: HTML-escape the markup characters (so the
    value can carry no live tag), then render every non-ASCII character as a numeric entity (so
    the output is pure ASCII and a phone reading the file directly still shows the text). The
    escape is reversible with `html.unescape`, which returns the original value."""
    return ascii_entities(html.escape(_as_text(value), quote=True))


# --------------------------------------------------------------------- the JSON boundary
def for_json(value):
    """The JSON-surface boundary. JSON's encoder neutralises the value and its decoder returns it
    byte for byte, so this boundary is identity: it passes the value through unchanged. It exists
    so a JSON writer routes through this module like every other surface, and so the wiring
    census can witness the routing; it never mutates the value (clause 3 depends on that)."""
    return value


# --------------------------------------------------------------------- the countersign ref table
#: The kind both countersign rows carry. They are the same family -- an owner observation no test
#: can make -- and are told apart by `ref`, never by kind.
COUNTERSIGN_KIND = "owner-countersign"

#: The declared countersign `ref` table. Each ref names the ONE gate that consumes it; a gate
#: reads only its own ref and ignores the other, so neither row can discharge the other's
#: obligation. `render-neutralisation` (this task) is consumed by the release door (M4.15);
#: `manual-phone-read` (M4.16) is consumed by `alpaca op close` for the M4 op.
COUNTERSIGN_REFS = {
    "render-neutralisation": {
        "consumer": "release-door",
        "carries": "the two surface names the owner read (RESUME.md and the analytics page)",
    },
    "manual-phone-read": {
        "consumer": "op-close-m4",
        "carries": "the date, the device and the browser the owner read the manual on",
    },
}


# --------------------------------------------------------------------- the surface wiring census
#: Every module that WRITES one of the rendered surfaces, and the surface it writes. The census
#: below asserts each one routes its externally sourced values through this boundary; an unrouted
#: writer is a bypass and FAILs (Done-when clause 6). Declared once, so the set of writers and
#: the set the census checks can never disagree.
SURFACE_WRITERS = {
    "alpaca/pad.py": "RESUME.md",
    "alpaca/board.py": "board.json",
    "alpaca/export.py": "data.json",
    "alpaca/analytics/build_index.py": "analytics/index.html",
    "alpaca/serve.py": "served-page",
}

#: The boundary entry points a writer may route through. A writer that names none of these while
#: writing a surface is neutralising by hand (or not at all) -- a bypass.
_ROUTES = ("render.cell", "render.html_text", "render.for_json", "render.ascii_entities",
           "render.uncell")


def _routes_through(text: str) -> list:
    """The boundary entry points `text` (a module's source) references. A module that imports
    this module AND calls at least one entry point is routed."""
    if text is None:
        return []
    imports = ("from alpaca import" in text and "render" in text) or "import alpaca.render" in text
    if not imports:
        return []
    return [r for r in _ROUTES if r in text]


def census(sources: dict) -> list:
    """Given {module -> source text} for the surface writers, one row per writer:
    {module, surface, routes, routed}. `routed` is True iff the writer imports this boundary and
    calls at least one of its entry points. Pure over the given sources, so a synthetic source
    proves the negative path (an unrouted writer reads as not routed)."""
    out = []
    for module in sorted(sources):
        surface = SURFACE_WRITERS.get(module, "(unknown)")
        routes = _routes_through(sources.get(module))
        out.append({"module": module, "surface": surface, "routes": routes,
                    "routed": bool(routes)})
    return out


def _read_source(root: str, module: str) -> str | None:
    import os
    p = os.path.join(root, module.replace("/", os.sep))
    try:
        with open(p, "r", encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return None


def surface_wiring(root: str) -> list:
    """Run the census over the real surface writers on disk under `root`. Every writer in
    `SURFACE_WRITERS` is read from the tree and classified; a missing file reads as not routed."""
    sources = {m: _read_source(root, m) for m in SURFACE_WRITERS}
    return census(sources)
