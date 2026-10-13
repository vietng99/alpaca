"""(cause C7) A failed AAR read keeps the page up and says so; it is never shown as "not owed" or as
"no overdue items". `aar owed` and `op close` fail closed on the same error (a profile that does not
load makes _no_gos raise). Ways this fails after it lands:

  * no try/except: one broken profile takes /hub/overview.json down for every page
  * a bare except that leaves the AAR keys out, so the page looks exactly like "nothing owed"
  * one reader failing takes the other down (an overdue failure hides the owed badge)
"""
import json
import re

from alpaca.tests.hub_aar_kit import NOT_OWED, OP, OWED, full, op_view, overdue_record, owe, page, setup, text
from alpaca import aar, aaritems, profile


def _boom(*_a, **_k):
    raise profile.ProfileError("the profile could not be read for this test")


def _said(o, rx):
    """The error is on the page: an entry of the errors list that matches `rx`, or an error field
    under an aar/overdue key of an op or task row (that context names the AAR by itself)."""
    found = [json.dumps(e) for e in o.get("errors") or []]

    def walk(v):
        if isinstance(v, dict):
            for k, x in v.items():
                if re.search(r"error|problem|fail", k, re.I) and x:
                    found.append("aar " + json.dumps(x))
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)
    for r in (o.get("ops") or []) + (o.get("tasks") or []):
        walk({k: v for k, v in r.items() if re.search(r"aar|overdue", k, re.I)})
    return any(re.search(rx, f, re.I) for f in found)


def test_owed_reader_failure_keeps_the_page_and_says_so(project, monkeypatch, tmp_path):
    conn = setup(project, monkeypatch)
    owe(conn)
    monkeypatch.setattr(aar, "triggers", _boom)
    o = page(project)                                   # must not raise
    assert _said(o, r"aar"), "the AAR read failed and the page carries no word of it: it reads as not owed"
    t = text(op_view(o, OP, tmp_path))
    assert not NOT_OWED.search(t), "a failed read is shown as not owed"


def test_overdue_reader_failure_keeps_the_page_and_the_owed_badge(project, monkeypatch, tmp_path):
    conn, _tid = overdue_record(project, monkeypatch)
    owe(conn)
    monkeypatch.setattr(aaritems, "overdue", _boom)
    o = page(project)
    assert _said(o, r"overdue|action item|aar"), "the overdue read failed and the page carries no word of it"
    assert OWED.search(text(op_view(o, OP, tmp_path))), "an overdue read failure took the owed badge down"


def test_full_overview_survives_too(project, monkeypatch):
    """The CLI and export call hub.overview directly; the same guard holds there."""
    conn = setup(project, monkeypatch)
    owe(conn)
    monkeypatch.setattr(aar, "triggers", _boom)
    assert full(project)["ops"]
