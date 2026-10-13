"""(class E1): pad.py guards optional sections with try/except: pass. Copying that idiom
for Not now turns any helper failure into an empty list, the silent drop the bars forbid. When the
deferral read or the trigger parse raises, RESUME still renders every other section and the Not
now section still says something (the ids, or a line saying it could not be read); CHECKLIST,
data.json and the status verb do not crash."""
import pytest

from alpaca.tests.not_now_kit import HEAD, checklist, data, defer, has_id, resume, run, section, setup
from alpaca import ops


def _boom(*a, **k):
    raise RuntimeError("deferral read exploded")


@pytest.mark.parametrize("name", ["deferral", "parse_trigger"])
def test_helper_failure_is_visible_not_silent(project, monkeypatch, name):
    setup(project, 2)
    defer("t-002", "parked", "after:t-001")
    monkeypatch.setattr(ops, name, _boom)
    text = resume(project)
    for s in ("## Next action", "## Progress", "## Current op", "## Open tasks", "## Recent events"):
        assert s in text, s
    body = section(text)
    assert body is not None, "the Not now heading vanished on a helper failure"
    joined = "\n".join(body).lower()
    assert any(has_id(l, "t-002") for l in body) or "could not" in joined or "error" in joined, body
    checklist(project)
    data(project)
    code, out = run("status")
    assert code == 0 and "## Next action" in out
