"""Shared helpers for the Not now hub tests (op-057 t-618). Not a test module.

The tests drive the real hub modules in node (alpaca/web/*.js, imported as ES modules with a
small `document` stub) and read the real stylesheets in their cascade order (serve.hub_styles()).
Where a module cannot load in node (hub.js touches the DOM at import), the test lifts the one
expression it needs out of the file and evaluates that.

Names the tests assume of the build (nothing else about naming is assumed):
  * the badge class is the status value: `.badge.deferred` (the task text names it)
  * the visible label of the state is taskplan.LABELS['deferred'] (Python already says "Not now");
    the hub must show the same word, read through cockpit.js STATE_LABELS
  * the Not now panel is one element whose class list or data-key contains "notnow" or "not-now"
    (any case, e.g. class="rm-notnow" or data-key="not-now:op-001"); one entry per task inside it
  * the cockpit tone name for deferred is the builder's: the tests read it from the rendered
    class (tone-border-<name>, status-chip tone-<name>, tl-<name>) and then check its CSS
"""
import json
import math
import re
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import alpaca

WEB = Path(alpaca.__file__).resolve().parent / "web"
REPO = WEB.parents[1]
NODE = shutil.which("node")

# The hub modules read `document` and start timers at import or first render (system.js).
STUB = ("globalThis.document={addEventListener(){},removeEventListener(){},getElementById(){return {};},"
        "querySelector(){return null;},querySelectorAll(){return [];},createElement(){return {};}};"
        "globalThis.setInterval=()=>0;globalThis.clearInterval=()=>{};"
        "globalThis.sessionStorage={getItem(){return null;},setItem(){}};")

# The record's words (ops.py): the met marker and the placeholder for a value never recorded.
MET = "trigger met"
UNRECORDED = "not recorded"
UNREADABLE = "could not be read"


def node(body, data=None, tmp=None):
    """Run `body` as an ES module in alpaca/web with the stub. DATA holds `data`; out(x) prints
    x as JSON, which this returns."""
    import pytest
    if not NODE:
        pytest.skip("node is required")
    head = STUB + "import {readFileSync} from 'node:fs';const out=x=>{process.stdout.write(JSON.stringify(x));process.exit(0);};"
    if data is not None:
        path = Path(tmp) / "data.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        head += "const DATA=JSON.parse(readFileSync(%s,'utf8'));" % json.dumps(str(path))
    run = subprocess.run([NODE, "--input-type=module", "-e", head + body + ";out(null);"],
                         capture_output=True, text=True, cwd=str(WEB), timeout=60)
    assert run.returncode == 0, run.stderr[-3000:]
    return json.loads(run.stdout) if run.stdout.strip() else None


def label():
    """The one visible word for the state: taskplan's (Python already prints it in CHECKLIST,
    RESUME and the roadmap node)."""
    from alpaca import taskplan
    return taskplan.LABELS["deferred"]


# ------------------------------------------------------------------ fixtures (plain dicts)
def deferral(reason="waiting on the vendor", when="when the vendor ships v2", met=False, met_by=None,
             op="op-1", tid="t-2", error=None, op_closed=False):
    return {"id": tid, "op": op, "op_closed": op_closed, "title": "Task " + tid, "reason": reason,
            "reactivate_when": when, "forms": [], "trigger_met": met, "met_by": met_by,
            "since": "2026-10-13T10:00:00+07:00", "error": error}


def task(tid, op="op-1", status="open", **extra):
    t = {"id": tid, "op": op, "title": "Task " + tid, "status": status, "updated_at": "2026-10-01T00:00:00+00:00",
         "created_at": "2026-09-01T00:00:00+00:00", "deferral": None}
    t.update(extra)
    return t


def deferred(tid, op="op-1", **kw):
    return task(tid, op, "deferred", updated_at="2026-10-13T00:00:00+00:00", deferral=deferral(op=op, tid=tid, **kw))


def plan_node(tid, state, label_=None, reasons=None, after=None):
    from alpaca import taskplan
    return {"id": tid, "state": state, "label": label_ or taskplan.LABELS.get(state, state), "depth": 0,
            "after": after or [], "reasons": reasons or [], "session": None}


def overview(tasks, nodes_by_op, ops=None, groups=None):
    """An overview payload: tasks, ops and a roadmap with one group per op (or `groups`)."""
    ops = ops or [{"id": op, "title": "Operation " + op, "status": "open", "phases": [], "phase_state": {}}
                  for op in nodes_by_op]
    operations = []
    for op, nodes in nodes_by_op.items():
        gs = (groups or {}).get(op) or [{"id": "g-" + op, "title": "Work " + op, "category": "Features", "nodes": nodes}]
        operations.append({"op": op, "revision": 1, "edges": [], "groups": gs})
    return {"project": {"name": "demo"}, "tasks": tasks, "ops": ops, "items": [], "levels": {},
            "roadmap": {"operations": operations}}


def words(d):
    from alpaca import ops
    return ops.not_now_words(d)


# ------------------------------------------------------------------ html
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}


class El:
    def __init__(self, tag, attrs, parent):
        self.tag, self.attrs, self.parent, self.children = tag, dict(attrs), parent, []

    @property
    def classes(self):
        return (self.attrs.get("class") or "").split()

    def text(self):
        """The visible text, an element boundary read as a space and runs of space squeezed, so
        'id</small><span>Why' never glues into one word."""
        return " ".join("".join(c if isinstance(c, str) else " %s " % c.text() for c in self.children).split())

    def walk(self):
        for c in self.children:
            if isinstance(c, El):
                yield c
                yield from c.walk()

    def ancestors(self):
        p = self.parent
        while p is not None:
            yield p
            p = p.parent


class _Tree(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = El("#root", {}, None)
        self.cur = self.root

    def handle_starttag(self, tag, attrs):
        el = El(tag, attrs, self.cur)
        self.cur.children.append(el)
        if tag not in VOID:
            self.cur = el

    def handle_startendtag(self, tag, attrs):
        self.cur.children.append(El(tag, attrs, self.cur))

    def handle_endtag(self, tag):
        node = self.cur
        while node is not None and node.tag != tag:
            node = node.parent
        if node is not None and node.parent is not None:
            self.cur = node.parent

    def handle_data(self, data):
        self.cur.children.append(data)


def tree(html):
    t = _Tree()
    t.feed(html)
    t.close()
    return t.root


NOT_NOW_RX = re.compile(r"not-?now", re.I)


def panels(html):
    """The Not now panels: outermost elements whose class or data-key names not-now/notnow."""
    root = tree(html)
    hits = [e for e in root.walk()
            if any(NOT_NOW_RX.search(c) for c in e.classes) or NOT_NOW_RX.search(e.attrs.get("data-key") or "")]
    return [e for e in hits if not any(a in hits for a in e.ancestors())]


def one_panel(html):
    got = panels(html)
    assert len(got) == 1, "expected exactly one Not now panel, found %d" % len(got)
    return got[0]


def entry(panel, tid, others=()):
    """The smallest element inside `panel` whose text names `tid` and none of `others`."""
    def names(text, ident):
        return re.search(r"(?<![\w-])%s(?![\w-])" % re.escape(ident), text) is not None
    best = None
    for e in panel.walk():
        t = e.text()
        if names(t, tid) and not any(names(t, o) for o in others if o != tid):
            if best is None or len(t) < len(best.text()):
                best = e
    # widen to the entry: climb while the parent still names only this task
    while best is not None and best.parent is not None and best.parent is not panel:
        t = best.parent.text()
        if any(re.search(r"(?<![\w-])%s(?![\w-])" % re.escape(o), t) for o in others if o != tid):
            break
        best = best.parent
    assert best is not None, "the Not now panel does not name %s" % tid
    return best


def find(html, pred):
    return [e for e in tree(html).walk() if pred(e)]


def by_class(html, cls):
    return find(html, lambda e: cls in e.classes)


# ------------------------------------------------------------------ css
def _strip(text):
    return re.sub(r"/\*.*?\*/", "", text, flags=re.S)


def _block(text, i):
    """Index just past the brace that closes the block opened at text[i] == '{'."""
    depth = 0
    for j in range(i, len(text)):
        if text[j] == "{":
            depth += 1
        elif text[j] == "}":
            depth -= 1
            if depth == 0:
                return j + 1
    return len(text)


def rules(text, ctx=""):
    """[(context, [selectors], {prop: value}, source order)] of one stylesheet; context is the
    at-rule prelude around the rule ('' at the top level)."""
    text, out, i = _strip(text), [], 0
    while True:
        j = text.find("{", i)
        if j < 0:
            return out
        prelude, end = text[i:j].strip(), _block(text, j)
        inner = text[j + 1:end - 1]
        if prelude.startswith("@"):
            out.extend(rules(inner, prelude))
        else:
            decls = {}
            for part in inner.split(";"):
                if ":" in part:
                    k, v = part.split(":", 1)
                    decls[k.strip()] = v.strip().replace("!important", "").strip()
            sels = [" ".join(s.split()) for s in prelude.split(",")]
            out.append((ctx, sels, decls))
        i = end


def sheets():
    from alpaca import serve
    return [(name, (WEB / name).read_text(encoding="utf-8")) for name in serve.hub_styles() if (WEB / name).exists()]


def all_rules():
    out = []
    for name, text in sheets():
        out.extend((name,) + r for r in rules(text))
    return out


LIGHT_ROOT = {":root", ':root[data-theme="light"]', "html"}
DARK_ROOT = ':root[data-theme="dark"]'


def tokens(theme):
    """Custom properties on the root in `theme` ('light' or 'dark'), top-level rules only."""
    got = {}
    for _, ctx, sels, decls in all_rules():
        if ctx:
            continue
        if any(s in LIGHT_ROOT for s in sels):
            got.update({k: v for k, v in decls.items() if k.startswith("--")})
    if theme == "dark":
        for _, ctx, sels, decls in all_rules():
            if not ctx and DARK_ROOT in sels:
                got.update({k: v for k, v in decls.items() if k.startswith("--")})
    return got


def declared(selector, prop, theme="light"):
    """The value the cascade gives `prop` on an element matched by exactly `selector` (top level,
    later sheets and later rules win; a dark-scoped copy of the selector wins in dark)."""
    selector = " ".join(selector.split())
    value = None
    for _, ctx, sels, decls in all_rules():
        if not ctx and selector in sels and prop in decls:
            value = decls[prop]
    if theme == "dark":
        for _, ctx, sels, decls in all_rules():
            if not ctx and (DARK_ROOT + " " + selector) in sels and prop in decls:
                value = decls[prop]
    return value


def has_rule(selector):
    selector = " ".join(selector.split())
    return any(selector in sels or any(s.endswith(" " + selector) for s in sels) for _, _, sels, _ in all_rules())


def resolve(value, theme, local=None):
    """Expand var() against the theme tokens (and `local` custom properties first)."""
    env = dict(tokens(theme))
    env.update(local or {})
    for _ in range(20):
        m = re.search(r"var\(\s*(--[\w-]+)\s*(?:,\s*([^()]*))?\)", value or "")
        if not m:
            return value
        got = env.get(m.group(1), m.group(2))
        assert got is not None, "undefined custom property %s in %s theme" % (m.group(1), theme)
        value = value[:m.start()] + got + value[m.end():]
    raise AssertionError("var() loop in " + str(value))


def _hex(s):
    s = s.lstrip("#")
    if len(s) in (3, 4):
        s = "".join(c * 2 for c in s)
    r, g, b = int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)
    a = int(s[6:8], 16) / 255 if len(s) == 8 else 1.0
    return (r, g, b, a)


def color(value):
    """(r, g, b, a) of a resolved colour: #hex, rgb()/rgba(), transparent, color-mix(in srgb, ...)."""
    v = " ".join(str(value).split()).lower()
    if v == "transparent":
        return (0, 0, 0, 0.0)
    if v.startswith("#"):
        return _hex(v)
    m = re.fullmatch(r"rgba?\(([^)]*)\)", v)
    if m:
        parts = [p for p in re.split(r"[ ,/]+", m.group(1)) if p]
        r, g, b = (float(p) for p in parts[:3])
        a = float(parts[3].rstrip("%")) / (100 if parts[3].endswith("%") else 1) if len(parts) > 3 else 1.0
        return (r, g, b, a)
    m = re.fullmatch(r"color-mix\(in srgb,(.*)\)", v.replace(", ", ","))
    if m:
        depth, parts, cur = 0, [], ""
        for ch in m.group(1):
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            if ch == "," and depth == 0:
                parts.append(cur.strip())
                cur = ""
            else:
                cur += ch
        parts.append(cur.strip())
        (c1, p1), (c2, p2) = [_mixpart(p) for p in parts[:2]]
        if p1 is None and p2 is None:
            p1 = p2 = 50.0
        elif p1 is None:
            p1 = 100 - p2
        elif p2 is None:
            p2 = 100 - p1
        a, b = color(c1), color(c2)
        w = p1 / (p1 + p2)
        return tuple(a[i] * w + b[i] * (1 - w) for i in range(4))
    raise AssertionError("cannot read colour %r" % value)


def _mixpart(p):
    m = re.fullmatch(r"(.*?)\s+([\d.]+)%", p)
    return (m.group(1), float(m.group(2))) if m else (p, None)


def over(fg, bg):
    """fg composited over an opaque bg."""
    a = fg[3]
    return tuple(fg[i] * a + bg[i] * (1 - a) for i in range(3)) + (1.0,)


def luminance(c):
    def ch(v):
        v = v / 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    return 0.2126 * ch(c[0]) + 0.7152 * ch(c[1]) + 0.0722 * ch(c[2])


def contrast(fg, bg):
    a, b = luminance(fg), luminance(bg)
    return (max(a, b) + 0.05) / (min(a, b) + 0.05)


def distance(a, b):
    return math.sqrt(sum((a[i] - b[i]) ** 2 for i in range(3)))


def paint(fg_value, bg_value, theme):
    """(text colour, background colour) as opaque RGB, the background over --surface."""
    surface = color(resolve("var(--surface)", theme))
    bg = over(color(resolve(bg_value, theme)), surface) if bg_value else surface
    fg = over(color(resolve(fg_value, theme)), bg)
    return fg, bg


# ------------------------------------------------------------------ hub.js expressions
def hub_js():
    return (WEB / "hub.js").read_text(encoding="utf-8")


def hub_line(prefix):
    """The one top-level line of hub.js that starts with `prefix` (e.g. 'const badge')."""
    lines = [l for l in hub_js().splitlines() if l.startswith(prefix)]
    assert len(lines) == 1, "hub.js has %d lines starting %r" % (len(lines), prefix)
    return lines[0]


# ------------------------------------------------------------------ git
def git(*args):
    return subprocess.run(["git", "-C", str(REPO)] + list(args), capture_output=True, text=True).stdout


def base():
    """Where this task's change starts: the merge base with op057/learning-loop."""
    return git("merge-base", "HEAD", "op057/learning-loop").strip() or "HEAD"


def added_lines(*paths):
    """[(path, line)] added by the change (committed and uncommitted) under `paths`."""
    out, path = [], None
    for line in git("diff", "--unified=0", base(), "--", *paths).splitlines():
        if line.startswith("+++ "):
            path = line[6:] if line.startswith("+++ b/") else None
        elif line.startswith("+") and not line.startswith("+++") and path:
            out.append((path, line[1:]))
    return out
