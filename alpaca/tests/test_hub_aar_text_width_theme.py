"""(cause C9; house rules) Trigger text is user text (aar owe --reason, task titles, attempt refs), the
page is read on a phone at 390 px in both themes, and every web file stays ASCII. Ways this fails
after it lands:

  * the reason reaches innerHTML unescaped
  * a 32-hex attempt token or a long reason scrolls the page sideways at 390 px (no overflow-wrap)
  * a new overdue/AAR colour token is defined for light only, or reads below 4.5:1 in one theme
  * an em dash or another non-ASCII glyph lands in a web file, or the JS reads the wall clock
"""
import re

import pytest

from alpaca.tests.hub_aar_kit import (OP, OVERDUE, OWED, all_rules, added_lines, base, block_of, cards, color, contrast, declared, git,
                     op_view, overdue_record, owe, page, paint, resolve, setup, standing, tokens, tree)

HOSTILE = '<img src=x onerror="window.x=1"> "><b>x</b> & done'
TOKEN = "attempt e3dc143819174608b6835c35b2293c82e3dc143819174608b6835c35b2293c82"


def test_reason_is_escaped(project, monkeypatch, tmp_path):
    conn = setup(project, monkeypatch)
    owe(conn, reason=HOSTILE)
    html = op_view(page(project), OP, tmp_path)
    assert OWED.search(tree(html).text())
    assert "<img" not in html and "<b>x</b>" not in html, "the aar owe reason is not escaped"


def test_long_trigger_wraps_at_phone_width(project, monkeypatch, tmp_path):
    conn = setup(project, monkeypatch)
    owe(conn, reason=TOKEN)
    html = op_view(page(project), OP, tmp_path)
    holder, label = block_of(html, OWED)
    scope = [holder, *holder.walk()] if holder is not None else [label]
    names = {c for e in scope for c in e.classes}
    wraps = [sels for _, _, sels, decls in all_rules()
             if any(("." + n) in s for s in sels for n in names)
             and (decls.get("overflow-wrap") in ("anywhere", "break-word")
                  or decls.get("word-break") in ("break-word", "break-all"))]
    assert wraps, "no overflow-wrap rule on the AAR block's classes %s: a long trigger scrolls the page at 390 px" % sorted(names)


@pytest.mark.parametrize("theme", ("light", "dark"))
def test_new_tokens_exist_in_both_themes_and_read(theme):
    light, dark = tokens("light"), tokens("dark")
    new = sorted(k for k in set(light) | set(dark) if re.search(r"overdue|aar", k, re.I))
    for k in new:
        assert k in light, "%s has no light value" % k
    for k in new:
        if k.endswith("-bg") or (k + "-bg") not in new:
            continue
        fg, bg = paint("var(%s)" % k, "var(%s-bg)" % k, theme)
        assert contrast(fg, bg) >= 4.5, "%s on %s-bg reads %.2f:1 in %s" % (k, k, contrast(fg, bg), theme)
    dark_only = [k for k in new if k in dark and k not in light]
    assert not dark_only, dark_only


@pytest.mark.parametrize("theme", ("light", "dark"))
def test_overdue_mark_reads_in_both_themes(project, monkeypatch, tmp_path, theme):
    _conn, tid = overdue_record(project, monkeypatch)
    card = cards(op_view(page(project), standing(project), tmp_path))[tid["Alpha late by date"]]
    classes = sorted({c for e in [card, *card.walk()] for c in e.classes if OVERDUE.search(c)})
    assert classes, "the overdue mark has no class of its own; it cannot be styled or tested"
    checked = 0
    for c in classes:
        fg = declared("." + c, "color", theme)
        if not fg:
            continue
        bg = declared("." + c, "background", theme) or declared("." + c, "background-color", theme)
        f, b = paint(fg, bg if bg and not bg.startswith(("none", "url")) else None, theme)
        assert contrast(f, b) >= 4.5, ".%s reads %.2f:1 in %s" % (c, contrast(f, b), theme)
        checked += 1
    assert checked, "no colour rule for the overdue classes %s" % classes


def test_web_files_stay_ascii_and_dash_free():
    from alpaca.tests.hub_aar_kit import REPO
    for p in ("alpaca/web/roadmap.js", "alpaca/web/hub.js", "alpaca/web/roadmap.css", "alpaca/web/theme.css",
              "alpaca/aarview.py"):
        assert "\u2014" not in (REPO / p).read_text(encoding="utf-8"), "%s holds an em dash" % p
    for p in ("alpaca/web/roadmap.js", "alpaca/web/hub.js", "alpaca/web/roadmap.css", "alpaca/web/theme.css"):
        assert (REPO / p).read_bytes().isascii(), "%s is not ASCII" % p


def test_js_never_reads_the_wall_clock_for_due():
    from alpaca.tests.hub_aar_kit import REPO
    text = (REPO / "alpaca/web/roadmap.js").read_text(encoding="utf-8")
    bad = [l.strip()[:120] for l in text.splitlines()
           if re.search(r"Date\.now|new Date\(", l) and re.search(r"overdue|aar", l, re.I)]
    assert not bad, "the JS judges lateness on the wall clock: %s" % bad
