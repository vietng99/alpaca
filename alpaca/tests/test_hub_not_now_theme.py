"""(class 6, done_bar[0]): the deferred badge has its own tone in BOTH themes. Fails if the badge
falls back to the neutral look, borrows a hue another state owns (amber is blocked/stale/open,
green done, red fail, accent doing), is a literal defined only for light, or is unreadable."""
import pytest

from alpaca.tests.hub_not_now_kit import declared, distance, contrast, paint, tokens

THEMES = ("light", "dark")
# the badges a reader already knows, by selector (theme.css:185-189)
OTHERS = {"neutral": ".badge", "done": ".badge.done", "blocked": ".badge.blocked", "open": ".badge.open",
          "fail": ".badge.fail", "doing": ".badge.doing"}


def badge_paint(selector, theme):
    fg, bg = declared(selector, "color", theme), declared(selector, "background", theme)
    bg = bg or declared(selector, "background-color", theme)
    assert fg and bg, "%s has no color/background in %s" % (selector, theme)
    return paint(fg, bg, theme)


def test_badge_deferred_rule_exists():
    assert declared(".badge.deferred", "color"), "no .badge.deferred colour rule in any hub stylesheet"
    assert declared(".badge.deferred", "background") or declared(".badge.deferred", "background-color"), \
        "no .badge.deferred background rule"


@pytest.mark.parametrize("theme", THEMES)
def test_badge_deferred_is_readable(theme):
    fg, bg = badge_paint(".badge.deferred", theme)
    assert contrast(fg, bg) >= 4.5, "deferred badge text contrast %.2f < 4.5 in %s" % (contrast(fg, bg), theme)


@pytest.mark.parametrize("theme", THEMES)
@pytest.mark.parametrize("other", sorted(OTHERS))
def test_badge_deferred_differs_from_every_other_badge(theme, other):
    fg, bg = badge_paint(".badge.deferred", theme)
    ofg, obg = badge_paint(OTHERS[other], theme)
    assert distance(fg, ofg) > 40 or distance(bg, obg) > 40, \
        "deferred badge looks like the %s badge in %s: %s/%s vs %s/%s" % (other, theme, fg, bg, ofg, obg)


def test_dark_theme_gives_the_badge_its_own_values():
    """A literal light colour in :root only stays light on the dark page."""
    light = badge_paint(".badge.deferred", "light")
    dark = badge_paint(".badge.deferred", "dark")
    assert tokens("dark").get("--surface") != tokens("light").get("--surface"), "setup: dark tokens not read"
    assert light != dark, "the deferred badge paints the same in light and dark: no dark value"
