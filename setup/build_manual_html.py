#!/usr/bin/env python3
"""Render `MANUAL.md` into the phone-safe `docs/manual.html`, and hold the manual truth gate (M4.16).

This module is both the renderer and the gate. It exists so the manual's HTML is DERIVED from the
markdown rather than hand-kept, and so the seven Done-when clauses are one set of byte checks a
caller (the release door / packaging) can run, never a human reading the page.

The renderer emits the whole dual-theme palette THREE times from ONE property list
(`PALETTE`): once on a bare `:root` (the light palette), once inside a
`@media (prefers-color-scheme: dark)` block, and once on `:root[data-theme="dark"]`. Because the
three blocks are generated from the same list, clause 5 (identical property-name set across the
three blocks) cannot drift. A fourth `:root[data-theme="light"]` block (also from the list) lets an
explicit light choice beat a system-dark preference; it is extra, so it never breaks clause 5.

The page is pure ASCII: every non-ASCII character becomes a numeric entity on its way out
(`_ascii_entities`), the metas put `charset` first and a viewport second, `<img>`/`<table>`/`<pre>`
are governed by `max-width:100%` or an `overflow-x:auto` container, no layout element carries a
fixed width above 400px, a visible theme toggle persists the choice in `localStorage` inside a
try/catch, and motion respects `prefers-reduced-motion`.

Rename-safe: the project root is discovered at runtime (`CLAUDE_PROJECT_DIR`, else walk up to the
directory holding `ALPACA-MANIFEST`); no folder-name literal and no absolute path is baked in.

The gate API the release door and the tests call:

  * ``check_ascii(data)``  -- clause 2 (data is the file bytes or its text).
  * ``check_parse(html)``  -- clause 3 (parses, no unclosed tag, one each of html/head/body).
  * ``check_metas(html)``  -- clause 4 (charset first, viewport present).
  * ``check_palette(html)``-- clause 5 (three blocks, identical property-name set).
  * ``check_layout(html)`` -- clause 6 (no fixed width above 400px; img/table/pre governed).
  * ``check_tree(root)``   -- clause 1 (manual names resolve in the tree; front verbs and hooks
                              named in the manual).
  * ``gate(root)``         -- every clause above over the shipped `docs/manual.html` + tree; an
                              empty list is a clean manual. Clause 7 is the wiring that turns a
                              non-empty gate into a closed release door (alpaca/phase/doors.py).
"""
from __future__ import annotations

import json
import os
import re
import sys
from html.parser import HTMLParser


# --------------------------------------------------------------------------- root discovery
MANIFEST = "ALPACA-MANIFEST"


def discover_root(start=None) -> str:
    """Resolve the explicit start or the script's installation, never an ambient operator root."""
    cur = os.path.abspath(start or os.path.dirname(os.path.abspath(__file__)))
    while True:
        if os.path.isfile(os.path.join(cur, MANIFEST)):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            return os.path.abspath(start or os.getcwd())
        cur = parent


def manual_path(root) -> str:
    return os.path.join(root, "MANUAL.md")


def html_path(root) -> str:
    return os.path.join(root, "docs", "manual.html")


# --------------------------------------------------------------------------- the palette
#: The dual-theme palette as ONE property list: (custom-property, light-value, dark-value). The
#: renderer emits every one of these names in all three theme blocks, so the property-name set is
#: identical across them by construction (clause 5). Values are ASCII hex, straight quotes only.
PALETTE = (
    ("--bg", "#ffffff", "#0f1115"),
    ("--fg", "#1a1d21", "#e6e8eb"),
    ("--muted", "#5a636e", "#9aa4b2"),
    ("--border", "#e2e6ea", "#2a2f37"),
    ("--card", "#f7f9fb", "#171b21"),
    ("--accent", "#1f6feb", "#4a90ff"),
    ("--accent-fg", "#ffffff", "#0b1220"),
    ("--code-bg", "#f2f4f7", "#1b2027"),
    ("--code-fg", "#0b2447", "#cfe3ff"),
    ("--link", "#0b5bd3", "#7cb0ff"),
)


def _decls(which: int) -> str:
    """The custom-property declarations for the light (1) or dark (2) column, one per line. Every
    line comes from PALETTE, so the property-name set is identical across the light and dark
    columns by construction."""
    return "\n".join("      %s: %s;" % (row[0], row[which]) for row in PALETTE)


def _stylesheet() -> str:
    light = _decls(1)
    dark = _decls(2)
    # The three blocks (plus the explicit-light override) all come from PALETTE, so their
    # property-name set is identical by construction. Bare :root is the light palette; the media
    # block and the data-theme="dark" block redefine the same names for dark.
    return """
    :root {
%(light)s
    }
    @media (prefers-color-scheme: dark) {
      :root {
%(dark)s
      }
    }
    :root[data-theme="dark"] {
%(dark)s
    }
    :root[data-theme="light"] {
%(light)s
    }
    * { box-sizing: border-box; }
    html { -webkit-text-size-adjust: 100%%; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--fg);
      font: 16px/1.6 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
      padding: 0 16px calc(48px + env(safe-area-inset-bottom, 0px));
    }
    .wrap { max-width: 760px; margin: 0 auto; }
    header.top {
      position: sticky; top: 0; z-index: 5;
      background: var(--bg);
      display: flex; align-items: center; justify-content: space-between;
      gap: 12px; padding: 12px 0; border-bottom: 1px solid var(--border);
    }
    header.top h1 { font-size: 1.15rem; margin: 0; }
    #theme-toggle {
      appearance: none; cursor: pointer;
      background: var(--card); color: var(--fg);
      border: 1px solid var(--border); border-radius: 8px;
      padding: 8px 12px; font: inherit; font-size: 0.9rem;
      min-height: 40px;
    }
    #theme-toggle:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
    main { padding-top: 8px; }
    h2 { font-size: 1.3rem; margin: 1.8em 0 0.4em; padding-top: 0.3em; border-top: 1px solid var(--border); }
    h3 { font-size: 1.08rem; margin: 1.3em 0 0.3em; }
    p { margin: 0.6em 0; }
    a { color: var(--link); }
    ul { margin: 0.5em 0; padding-left: 1.3em; }
    li { margin: 0.25em 0; }
    strong { font-weight: 650; }
    code {
      background: var(--code-bg); color: var(--code-fg);
      padding: 0.1em 0.35em; border-radius: 5px;
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 0.88em; word-break: break-word;
    }
    img { max-width: 100%%; height: auto; }
    pre {
      max-width: 100%%; overflow-x: auto;
      background: var(--code-bg); color: var(--code-fg);
      padding: 12px 14px; border-radius: 8px; border: 1px solid var(--border);
      line-height: 1.45;
    }
    pre code { background: none; padding: 0; color: inherit; }
    .scroll-x { max-width: 100%%; overflow-x: auto; }
    table {
      max-width: 100%%; overflow-x: auto; display: block;
      border-collapse: collapse; width: auto; margin: 0.6em 0;
    }
    th, td { border: 1px solid var(--border); padding: 7px 10px; text-align: left; vertical-align: top; }
    th { background: var(--card); }
    footer { margin-top: 2.5em; padding-top: 1em; border-top: 1px solid var(--border); color: var(--muted); font-size: 0.9rem; }
    @media (prefers-reduced-motion: no-preference) {
      html { scroll-behavior: smooth; }
      #theme-toggle { transition: background 120ms ease, border-color 120ms ease; }
    }
""" % {"light": light, "dark": dark}


_TOGGLE_SCRIPT = """
(function () {
  var root = document.documentElement;
  var KEY = "alpaca-manual-theme";
  var btn = document.getElementById("theme-toggle");
  function stored() {
    try { return window.localStorage.getItem(KEY); } catch (e) { return null; }
  }
  function save(v) {
    try { window.localStorage.setItem(KEY, v); } catch (e) {}
  }
  function systemDark() {
    try { return window.matchMedia("(prefers-color-scheme: dark)").matches; } catch (e) { return false; }
  }
  function current() {
    var a = root.getAttribute("data-theme");
    if (a === "dark" || a === "light") { return a; }
    return systemDark() ? "dark" : "light";
  }
  function apply(v) {
    root.setAttribute("data-theme", v);
    if (btn) {
      btn.setAttribute("aria-pressed", v === "dark" ? "true" : "false");
      btn.textContent = v === "dark" ? "Light mode" : "Dark mode";
    }
  }
  var s = stored();
  if (s === "dark" || s === "light") { apply(s); } else { apply(current()); }
  if (btn) {
    btn.addEventListener("click", function () {
      var next = current() === "dark" ? "light" : "dark";
      apply(next); save(next);
    });
  }
})();
"""


# --------------------------------------------------------------------------- markdown to HTML
_NON_ASCII = re.compile(r"[^\x00-\x7F]")


def _ascii_entities(text: str) -> str:
    """Turn every non-ASCII character into a numeric entity, so the output is pure ASCII and a
    phone that misreads the encoding still shows the text."""
    return _NON_ASCII.sub(lambda m: "&#%d;" % ord(m.group()), text)


def _esc(text: str) -> str:
    """HTML-escape the markup characters. Straight quotes stay straight; ampersand first."""
    return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


_CODE_SPAN = re.compile(r"`([^`]+)`")
_BOLD = re.compile(r"\*\*([^*]+)\*\*")


def _inline(text: str) -> str:
    """Inline markdown: code spans first (their contents are escaped but never bolded), then bold,
    then the surviving text is escaped. Returns HTML (not yet ascii-folded)."""
    out = []
    pos = 0
    for m in _CODE_SPAN.finditer(text):
        out.append(_bold_and_escape(text[pos:m.start()]))
        out.append("<code>%s</code>" % _esc(m.group(1)))
        pos = m.end()
    out.append(_bold_and_escape(text[pos:]))
    return "".join(out)


def _bold_and_escape(text: str) -> str:
    out = []
    pos = 0
    for m in _BOLD.finditer(text):
        out.append(_esc(text[pos:m.start()]))
        out.append("<strong>%s</strong>" % _esc(m.group(1)))
        pos = m.end()
    out.append(_esc(text[pos:]))
    return "".join(out)


def _render_table(rows: list) -> str:
    """rows is a list of cell-lists; rows[0] is the header, rows[1] is the separator (dropped)."""
    header = rows[0]
    body = rows[2:]
    out = ['<div class="scroll-x">', "<table>", "<thead><tr>"]
    for c in header:
        out.append("<th>%s</th>" % _inline(c.strip()))
    out.append("</tr></thead>")
    out.append("<tbody>")
    for r in body:
        out.append("<tr>")
        for c in r:
            out.append("<td>%s</td>" % _inline(c.strip()))
        out.append("</tr>")
    out.append("</tbody></table></div>")
    return "".join(out)


def _split_row(line: str) -> list:
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return s.split("|")


def markdown_to_html(md: str) -> str:
    """A small, deterministic markdown subset: ATX headings, paragraphs, unordered lists (a bullet
    may wrap onto indented continuation lines), fenced code blocks, GitHub tables, and inline code
    and bold. Enough for the manual, no more."""
    lines = md.split("\n")
    out = []
    i = 0
    n = len(lines)
    in_list = False

    def close_list():
        nonlocal in_list
        if in_list:
            out.append("</ul>")
            in_list = False

    while i < n:
        line = lines[i]
        stripped = line.strip()

        # fenced code block
        if stripped.startswith("```"):
            close_list()
            i += 1
            code = []
            while i < n and not lines[i].strip().startswith("```"):
                code.append(lines[i])
                i += 1
            i += 1  # drop the closing fence
            out.append("<pre><code>%s</code></pre>" % _esc("\n".join(code)))
            continue

        # table: a line with a pipe, followed by a separator row of dashes/pipes
        if "|" in line and i + 1 < n and re.match(r"^\s*\|?[\s:|-]+\|[\s:|-]*$", lines[i + 1]) \
                and "-" in lines[i + 1]:
            close_list()
            rows = []
            while i < n and "|" in lines[i] and lines[i].strip():
                rows.append(_split_row(lines[i]))
                i += 1
            out.append(_render_table(rows))
            continue

        if not stripped:
            close_list()
            i += 1
            continue

        m = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if m:
            close_list()
            level = len(m.group(1))
            out.append("<h%d>%s</h%d>" % (level, _inline(m.group(2).strip()), level))
            i += 1
            continue

        if re.match(r"^[-*]\s+", stripped):
            if not in_list:
                out.append("<ul>")
                in_list = True
            parts = [re.sub(r"^[-*]\s+", "", stripped)]
            i += 1
            # an indented continuation line belongs to the same item (a wrapped bullet)
            while i < n and lines[i][:1] in (" ", "\t") and lines[i].strip() \
                    and not re.match(r"^(#{1,6})\s|^[-*]\s|^```", lines[i].strip()):
                parts.append(lines[i].strip())
                i += 1
            out.append("<li>%s</li>" % _inline(" ".join(parts)))
            continue

        # paragraph: gather consecutive non-blank, non-special lines
        close_list()
        para = [stripped]
        i += 1
        while i < n and lines[i].strip() and not re.match(r"^(#{1,6})\s|^[-*]\s|^```", lines[i].strip()) \
                and "|" not in lines[i]:
            para.append(lines[i].strip())
            i += 1
        out.append("<p>%s</p>" % _inline(" ".join(para)))

    close_list()
    return "\n".join(out)


# --------------------------------------------------------------------------- the page
def render(root=None) -> str:
    """Read `MANUAL.md` under `root` and return the whole phone-safe HTML page, pure ASCII."""
    root = root or discover_root()
    with open(manual_path(root), encoding="utf-8") as fh:
        md = fh.read()
    title = "Alpaca user manual"
    m = re.match(r"^#\s+(.+)$", md.split("\n", 1)[0].strip())
    if m:
        title = m.group(1).strip()
    body_html = markdown_to_html(md)
    page = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>%(title)s</title>
<style>%(style)s</style>
</head>
<body>
<div class="wrap">
<header class="top">
<h1>%(title)s</h1>
<button id="theme-toggle" type="button" aria-pressed="false">Dark mode</button>
</header>
<main>
%(body)s
</main>
<footer>
<p>Rendered from MANUAL.md by setup/build_manual_html.py. This page is pure ASCII, so it reads on a phone over a file link even when the browser ignores the charset.</p>
</footer>
</div>
<script>%(script)s</script>
</body>
</html>
""" % {"title": _esc(title), "style": _stylesheet(), "body": body_html, "script": _TOGGLE_SCRIPT}
    return _ascii_entities(page)


def build(root=None) -> str:
    """Render and write `docs/manual.html`. Returns the path written."""
    root = root or discover_root()
    out = html_path(root)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    text = render(root)
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(text)
    return out


# =========================================================================== the gate
def _finding(clause, reason, detail=""):
    return {"clause": clause, "reason": reason, "detail": detail}


# --------------------------------------------------------------------------- clause 2: ASCII
def check_ascii(data) -> list:
    """Clause 2: zero units above 0x7F, and (for bytes) the latin-1 read equals the ascii decode.

    Accepts the file bytes (what the shipped-file test passes) or a string (a code-point check).
    """
    findings = []
    if isinstance(data, str):
        for idx, ch in enumerate(data):
            if ord(ch) > 0x7F:
                findings.append(_finding(2, "non-ascii-char",
                                         "U+%04X at offset %d" % (ord(ch), idx)))
                break
        try:
            data.encode("ascii")
        except UnicodeEncodeError as e:
            findings.append(_finding(2, "not-ascii", str(e)))
        return findings
    raw = bytes(data)
    for idx, b in enumerate(raw):
        if b > 0x7F:
            findings.append(_finding(2, "non-ascii-byte",
                                     "byte 0x%02X at offset %d" % (b, idx)))
            break
    try:
        as_latin1 = raw.decode("latin-1")
        as_ascii = raw.decode("ascii")
        if as_latin1 != as_ascii:
            findings.append(_finding(2, "latin1-ne-ascii",
                                     "the latin-1 read differs from the ascii decode"))
    except UnicodeDecodeError as e:
        findings.append(_finding(2, "not-ascii", str(e)))
    return findings


# --------------------------------------------------------------------------- clause 3: parse
_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link",
         "meta", "param", "source", "track", "wbr"}


class _StackParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.counts = {"html": 0, "head": 0, "body": 0}
        self.mismatches = []

    def handle_starttag(self, tag, attrs):
        if tag in self.counts:
            self.counts[tag] += 1
        if tag not in _VOID:
            self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        if tag in self.counts:
            self.counts[tag] += 1

    def handle_endtag(self, tag):
        if tag in _VOID:
            return
        if tag in self.stack:
            # pop down to the matching tag (close any implicitly-open descendants)
            while self.stack and self.stack[-1] != tag:
                self.mismatches.append(self.stack.pop())
            if self.stack:
                self.stack.pop()
        else:
            self.mismatches.append("stray </%s>" % tag)


def check_parse(html_str) -> list:
    """Clause 3: parses through html.parser with no unclosed tag, and exactly one each of
    <html>, <head>, <body>."""
    findings = []
    p = _StackParser()
    try:
        p.feed(html_str)
        p.close()
    except Exception as e:  # html.parser is lenient, but never let a crash read as a pass
        findings.append(_finding(3, "parse-error", str(e)))
        return findings
    if p.stack:
        findings.append(_finding(3, "unclosed-tag", "still open: %s" % ", ".join(p.stack)))
    if p.mismatches:
        findings.append(_finding(3, "tag-mismatch", "; ".join(p.mismatches)))
    for tag in ("html", "head", "body"):
        if p.counts[tag] != 1:
            findings.append(_finding(3, "wrong-count",
                                     "<%s> appears %d time(s), expected 1" % (tag, p.counts[tag])))
    return findings


# --------------------------------------------------------------------------- clause 4: metas
class _HeadParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.in_head = False
        self.first_head_tag = None
        self.first_head_attrs = None
        self.metas = []

    def handle_starttag(self, tag, attrs):
        if tag == "head":
            self.in_head = True
            return
        if self.in_head and self.first_head_tag is None:
            self.first_head_tag = tag
            self.first_head_attrs = dict(attrs)
        if tag == "meta":
            self.metas.append(dict(attrs))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        if tag == "head":
            self.in_head = False


def check_metas(html_str) -> list:
    """Clause 4: <head>'s first element is <meta charset="utf-8">, and a viewport meta is present."""
    findings = []
    p = _HeadParser()
    p.feed(html_str)
    p.close()
    first = p.first_head_tag
    attrs = p.first_head_attrs or {}
    if first != "meta" or (attrs.get("charset") or "").strip().lower() != "utf-8":
        findings.append(_finding(4, "charset-not-first",
                                 "first head element is %r, want <meta charset=\"utf-8\">" % first))
    viewport = None
    for meta in p.metas:
        if (meta.get("name") or "").strip().lower() == "viewport":
            viewport = (meta.get("content") or "").strip()
            break
    if viewport is None:
        findings.append(_finding(4, "viewport-missing", "no <meta name=\"viewport\">"))
    else:
        norm = re.sub(r"\s+", " ", viewport.lower())
        if "width=device-width" not in norm or "initial-scale=1" not in norm:
            findings.append(_finding(4, "viewport-content",
                                     "viewport content is %r" % viewport))
    return findings


# --------------------------------------------------------------------------- clause 5: palette
def _css_of(html_str) -> str:
    return "\n".join(m.group(1) for m in
                     re.finditer(r"<style[^>]*>(.*?)</style>", html_str, re.S | re.I))


def _brace_block(css, open_idx):
    """css[open_idx] is a '{'; return (inner-text, index-of-matching-close-brace)."""
    depth = 0
    for j in range(open_idx, len(css)):
        if css[j] == "{":
            depth += 1
        elif css[j] == "}":
            depth -= 1
            if depth == 0:
                return css[open_idx + 1:j], j
    return "", len(css)


def _props(block) -> set:
    return set(re.findall(r"(--[A-Za-z0-9_-]+)\s*:", block))


def check_palette(html_str) -> list:
    """Clause 5: a bare :root, a @media (prefers-color-scheme: dark) :root, and a
    :root[data-theme="dark"] block, all declaring the identical, non-empty property-name set."""
    css = _css_of(html_str)
    findings = []

    media_props = set()
    mm = re.search(r"@media[^{]*prefers-color-scheme\s*:\s*dark[^{]*\{", css, re.I)
    if not mm:
        findings.append(_finding(5, "no-media-dark",
                                 "no @media (prefers-color-scheme: dark) block"))
        media_inner = ""
        css_nomedia = css
    else:
        media_inner, endj = _brace_block(css, mm.end() - 1)
        css_nomedia = css[:mm.start()] + css[endj + 1:]
        rm = re.search(r":root\s*\{", media_inner)
        if not rm:
            findings.append(_finding(5, "no-media-root",
                                     "the dark @media block has no bare :root"))
        else:
            rblock, _ = _brace_block(media_inner, rm.end() - 1)
            media_props = _props(rblock)

    dark_props = set()
    dm = re.search(r':root\[data-theme\s*=\s*"dark"\]\s*\{', css)
    if not dm:
        findings.append(_finding(5, "no-data-theme-dark",
                                 "no :root[data-theme=\"dark\"] block"))
    else:
        dblock, _ = _brace_block(css, dm.end() - 1)
        dark_props = _props(dblock)

    bare_props = set()
    for m2 in re.finditer(r":root\s*\{", css_nomedia):
        block, _ = _brace_block(css_nomedia, m2.end() - 1)
        bare_props |= _props(block)

    if not bare_props:
        findings.append(_finding(5, "no-bare-root",
                                 "no bare :root block declares custom properties"))

    if bare_props and (bare_props != media_props or bare_props != dark_props):
        findings.append(_finding(5, "palette-drift",
                                 "bare=%d media=%d dark=%d; missing in media=%s, missing in dark=%s"
                                 % (len(bare_props), len(media_props), len(dark_props),
                                    sorted(bare_props - media_props),
                                    sorted(bare_props - dark_props))))
    return findings


# --------------------------------------------------------------------------- clause 6: layout
def _body_of(html_str) -> str:
    m = re.search(r"<body[^>]*>(.*)</body>", html_str, re.S | re.I)
    return m.group(1) if m else html_str


def _tag_governed(css, tag) -> bool:
    for m in re.finditer(r"([^{}]+)\{([^}]*)\}", css):
        sel, body = m.group(1), m.group(2)
        if re.search(r"(?<![\w.\-])%s(?![\w\-])" % tag, sel):
            b = body.replace(" ", "").replace("\n", "").lower()
            if "max-width:100%" in b or "overflow-x:auto" in b:
                return True
    return False


def check_layout(html_str) -> list:
    """Clause 6: no CSS rule sets width or min-width to a fixed value above 400px on a layout
    element, and every <img>, <table> and <pre> present is governed by max-width:100% or an
    overflow-x:auto container."""
    css = _css_of(html_str)
    findings = []
    for m in re.finditer(r"(?<![\w\-])(width|min-width)\s*:\s*(\d+(?:\.\d+)?)\s*px", css, re.I):
        val = float(m.group(2))
        if val > 400:
            findings.append(_finding(6, "fixed-width",
                                     "%s: %spx exceeds 400px" % (m.group(1), m.group(2))))
    body = _body_of(html_str)
    for tag in ("img", "table", "pre"):
        if re.search(r"<%s[\s>/]" % tag, body, re.I) and not _tag_governed(css, tag):
            findings.append(_finding(6, "ungoverned-element",
                                     "<%s> is not governed by max-width:100%% or overflow-x:auto"
                                     % tag))
    return findings


# --------------------------------------------------------------------------- clause 1: truth vs tree
#: Runtime / generated artifacts a manual may name that are not checked into the shipped tree. The
#: record and its projections are built at runtime, so naming them is not a broken pointer.
RUNTIME_ALLOW = {
    ".alpaca", ".alpaca/alpaca.db", ".alpaca/instance.json", "RESUME.md", "CHECKLIST.md", "board.json", "data.json",
    "analytics", "analytics/index.html", "MANIFEST.json",
}

_PATH_EXT = (".py", ".md", ".html", ".json", ".yaml", ".yml", ".sh", ".db", ".txt")
_BACKTICK = re.compile(r"`([^`]+)`")
_VERB = re.compile(r"^alpaca\s+([a-z][a-z0-9-]*)\b")
_HOOK_MOD = re.compile(r"^alpaca\.hooks\.([a-z_][a-z0-9_]*)$")
_MODULE = re.compile(r"^alpaca\.[a-z_][a-z0-9_.]*$")


def _looks_like_path(tok: str) -> bool:
    if " " in tok or tok.startswith("alpaca ") or tok.startswith("alpaca."):
        return False
    if tok.startswith("/"):
        return False  # a leading slash is a slash-command (e.g. /autodrive), never a repo path
    if not re.match(r"^[A-Za-z0-9_.\-/]+$", tok):
        return False
    if "/" in tok:
        return True
    return tok.endswith(_PATH_EXT)


def _resolve_module(root, dotted) -> bool:
    parts = dotted.split(".")
    cur = root
    for i, part in enumerate(parts):
        pyfile = os.path.join(cur, part + ".py")
        subdir = os.path.join(cur, part)
        if os.path.isfile(pyfile):
            return True  # a module file; deeper parts are attributes
        if os.path.isdir(subdir):
            cur = subdir
            continue
        return False
    return True


def _registered_verbs(root):
    """The live front dispatch table. Read from the installed alpaca package (the CLI a caller runs),
    with a tolerant fallback so the gate never crashes when the package is not importable."""
    try:
        if root not in sys.path:
            sys.path.insert(0, root)
        from alpaca import cli
        return set(cli.registered_verbs())
    except Exception:
        return None


def _front_verbs():
    try:
        from alpaca import surface
        return set(surface.FRONT)
    except Exception:
        return set()


def _settings_hooks(root):
    """The hook modules registered in .claude/settings.json: the set of alpaca.hooks.<name> tokens and
    the set of event names (SessionStart, ...). Returns (modules, events)."""
    path = os.path.join(root, ".claude", "settings.json")
    modules, events = set(), set()
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return modules, events
    for event, groups in (data.get("hooks") or {}).items():
        events.add(event)
        for group in groups or []:
            for hook in group.get("hooks", []) or []:
                for m in re.finditer(r"alpaca\.hooks\.([a-z_][a-z0-9_]*)", hook.get("command", "")):
                    modules.add("alpaca.hooks.%s" % m.group(1))
    return modules, events


def check_tree(root, manual_text=None) -> list:
    """Clause 1, both directions: every command, path, verb, hook and file NAMED in MANUAL.md
    resolves in the shipped tree; and every front verb and every registered hook APPEARS in
    MANUAL.md."""
    root = root or discover_root()
    findings = []
    if manual_text is None:
        try:
            with open(manual_path(root), encoding="utf-8") as fh:
                manual_text = fh.read()
        except OSError as e:
            return [_finding(1, "manual-missing", str(e))]

    registered = _registered_verbs(root)
    tokens = [m.group(1).strip() for m in _BACKTICK.finditer(manual_text)]

    # manual -> tree
    for tok in tokens:
        vm = _VERB.match(tok)
        if vm:
            verb = vm.group(1)
            if registered is not None and verb not in registered:
                findings.append(_finding(1, "unknown-verb",
                                         "`alpaca %s` names a verb the CLI does not dispatch" % verb))
            continue
        hm = _HOOK_MOD.match(tok)
        if hm:
            if not os.path.isfile(os.path.join(root, "alpaca", "hooks", hm.group(1) + ".py")):
                findings.append(_finding(1, "unknown-hook",
                                         "%s names a hook module with no file" % tok))
            continue
        if _MODULE.match(tok):
            if not _resolve_module(root, tok):
                findings.append(_finding(1, "unknown-module",
                                         "%s does not resolve to a module in the tree" % tok))
            continue
        if _looks_like_path(tok):
            rel = tok.rstrip("/")
            if rel in RUNTIME_ALLOW or tok in RUNTIME_ALLOW:
                continue
            if not os.path.exists(os.path.join(root, rel)):
                findings.append(_finding(1, "unknown-path",
                                         "`%s` names a path that is not in the tree" % tok))

    # tree -> manual: every front verb and every registered hook appears in MANUAL.md
    for verb in sorted(_front_verbs()):
        if ("`alpaca %s`" % verb) not in manual_text:
            findings.append(_finding(1, "front-verb-undocumented",
                                     "front verb `alpaca %s` is not named in MANUAL.md" % verb))
    modules, events = _settings_hooks(root)
    for mod in sorted(modules):
        if mod not in manual_text:
            findings.append(_finding(1, "hook-undocumented",
                                     "registered hook %s is not named in MANUAL.md" % mod))
    for event in sorted(events):
        if event not in manual_text:
            findings.append(_finding(1, "hook-event-undocumented",
                                     "registered hook event %s is not named in MANUAL.md" % event))
    return findings


# --------------------------------------------------------------------------- the composed gate
def gate(root=None) -> list:
    """Every mechanical clause over the shipped docs/manual.html and the tree. An empty list is a
    clean manual; a non-empty list closes the release door (clause 7, alpaca/phase/doors.py)."""
    root = root or discover_root()
    findings = []
    hp = html_path(root)
    try:
        with open(hp, "rb") as fh:
            raw = fh.read()
    except OSError as e:
        return [_finding(2, "html-missing", str(e))]
    findings += check_ascii(raw)
    text = raw.decode("latin-1")
    findings += check_parse(text)
    findings += check_metas(text)
    findings += check_palette(text)
    findings += check_layout(text)
    findings += check_tree(root)
    return findings


# --------------------------------------------------------------------------- CLI
def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(
        prog="build_manual_html",
        description="Render MANUAL.md into docs/manual.html and run the manual truth gate.")
    ap.add_argument("--root", default=None, help="the project root (default: discovered at runtime)")
    ap.add_argument("--check", action="store_true",
                    help="run the gate over the shipped page and print findings; write nothing")
    a = ap.parse_args(argv)
    root = a.root or discover_root()
    if a.check:
        findings = gate(root)
        for f in findings:
            print("MANUAL-GATE clause %s: %s (%s)" % (f["clause"], f["reason"], f["detail"]))
        print("manual gate: %s (%d finding(s))" % ("PASS" if not findings else "FAIL", len(findings)))
        return 0 if not findings else 1
    out = build(root)
    print("wrote %s" % out)
    findings = gate(root)
    if findings:
        for f in findings:
            print("MANUAL-GATE clause %s: %s (%s)" % (f["clause"], f["reason"], f["detail"]))
        return 1
    print("manual gate: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
