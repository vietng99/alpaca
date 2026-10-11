"""op-008 t-571: the secret screen on Python 3.10, the oldest Python Alpaca runs on.

The plain-phrasing patterns of `alpaca.analytics.detail` match runs possessively (`X*+`, `X++`), a
quantifier Python has only since 3.11; 1.2.0 failed to import there (CI run 38050329057, tests
(3.10): `re.error: multiple repeat`). On 3.10 each pattern is compiled from `detail.atomic_pattern`,
which writes every possessive run as the atomic group it means. These tests run that 3.10 form on
the Python at hand and hold it to the form 3.11+ compiles: the same matches, group for group, on the
screen door's exhaustive, random and fixture corpora, and the same linear time on its generated
pump shapes. CI runs the whole suite on 3.10 as well, where the screen uses the 3.10 form itself.
"""
import itertools
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))
import test_screen_door as door                                 # noqa: E402

from alpaca.analytics import detail                             # noqa: E402


def _possessive_patterns():
    """The patterns compiled from a possessive source, by name; on 3.10 each is already atomic."""
    return {name: value for name, value in vars(detail).items()
            if isinstance(value, re.Pattern) and value in detail.POSSESSIVE_SOURCES}


def _public_spans(match):
    """The span of the match and of each group but the `_atomN` groups the 3.10 form adds."""
    atoms = {index for name, index in match.re.groupindex.items()
             if name.startswith(detail.ATOM_GROUP_PREFIX)}
    return tuple(match.span(index) for index in range(match.re.groups + 1) if index not in atoms)


def _atomic(pattern):
    return re.compile(detail.atomic_pattern(detail.POSSESSIVE_SOURCES[pattern]), pattern.flags)


def test_every_possessive_pattern_is_compiled_for_the_floor_python():
    """Nine patterns hold a possessive run; each 3.10 form holds none and names its groups as 3.11+
    does, plus only `_atomN` groups. Every pattern of the module that holds one is compiled through
    `_possessive` (a plain `re.compile` of one fails to import on 3.10)."""
    patterns = _possessive_patterns()
    assert sorted(patterns) == ["_PLAIN_SECRET", "_V_DIGITS", "_V_ENV", "_V_LIST_NEXT", "_V_PROSE",
                                "_V_REVERSE", "_V_SEP", "_V_SHORT", "_V_YAML"]
    for name, pattern in patterns.items():
        source = detail.POSSESSIVE_SOURCES[pattern]
        assert detail._POSSESSIVE.search(source), name
        atomic = detail.atomic_pattern(source)
        assert not detail._POSSESSIVE.search(atomic), name
        compiled = re.compile(atomic, pattern.flags)
        extra = set(compiled.groupindex) - set(re.findall(r"\(\?P<(\w+)>", source))
        assert extra and all(group.startswith(detail.ATOM_GROUP_PREFIX) for group in extra), name
    others = [name for name, value in vars(detail).items() if isinstance(value, re.Pattern)
              and value not in detail.POSSESSIVE_SOURCES and value is not detail._POSSESSIVE
              and detail._POSSESSIVE.search(value.pattern)]
    assert others == []


def test_the_floor_python_compiles_the_atomic_form():
    expected = sys.version_info < (3, 11)
    for name, pattern in _possessive_patterns().items():
        assert (detail.ATOM_GROUP_PREFIX + "0" in pattern.groupindex) is expected, name


def _texts():
    for _key, (_name, alphabet, longest, prefixes) in sorted(door.EXHAUSTIVE.items()):
        yield from _exhaustive_small(alphabet, longest, prefixes)
    yield from itertools.chain.from_iterable(door._random_texts(seed, 2000) for seed in range(5))
    yield from door._fixture_texts()


def _exhaustive_small(alphabet, longest, prefixes):
    return door._exhaustive(alphabet, min(longest, 7), prefixes)


@pytest.mark.skipif(sys.version_info < (3, 11), reason="the possessive form compiles on 3.11+ only")
def test_the_atomic_form_finds_what_the_possessive_form_finds():
    patterns = {name: (pattern, _atomic(pattern)) for name, pattern in _possessive_patterns().items()}
    differ, count = [], 0
    for text in _texts():
        count += 1
        for name, (possessive, atomic) in patterns.items():
            if ([_public_spans(m) for m in possessive.finditer(text)]
                    != [_public_spans(m) for m in atomic.finditer(text)]):
                differ.append((name, text[:120]))
        if len(differ) > 5:
            break
    assert count > 10_000 and differ == []


@pytest.fixture
def atomic_screen(monkeypatch):
    for name, pattern in _possessive_patterns().items():
        monkeypatch.setattr(detail, name, _atomic(pattern))


@pytest.mark.parametrize("target", ("screen", "plain-phrasing", "plain-variant"))
def test_the_atomic_form_takes_linear_time(atomic_screen, target):
    fn = detail.screen if target == "screen" else door._registry()[target]
    slow = [(name, found) for name, make, per in door._pump_shapes()
            for found in [door._superlinear(fn, make, per)] if found]
    assert slow == [], "superlinear shapes (chars at 4n, cpu at n, at 4n, ratio): %s" % slow


def test_the_two_found_shapes_take_linear_time_in_the_atomic_form(atomic_screen):
    for shape in sorted(door.FOUND_SHAPES):
        assert door._superlinear(detail.screen, door.FOUND_SHAPES[shape], 1) is None, shape
