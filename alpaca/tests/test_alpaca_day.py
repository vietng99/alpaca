"""M4.6 proof (companion) - `alpaca day`, the per-project daily digest.

The digest is a VIEW over the record and never a source: `alpaca day` writes nothing and a re-render
under a fixed clock is byte-identical. Proved on BOTH paths - a day with activity and a hostile
note surfaced, and the empty day - through the CLI verb and the historian view.
"""
import json

from alpaca import cli, db as tedb
from alpaca import historian
from alpaca.clock import FixedClock


def _clock(start="2026-04-01T00:00:00+00:00", step=1):
    return FixedClock(start=start, step=step)


def _seed(conn, clk):
    tedb.append_event(conn, session="s1", actor="human", kind="op-open", op="op-001",
                      data={"intent": "ship the digest", "done_when": "it renders"}, clock=clk)
    tedb.upsert(conn, "ops", "id", {"id": "op-001", "intent": "ship the digest",
                                    "done_when": "it renders", "status": "open", "opened": "t",
                                    "closed": None, "phases": None})
    tedb.append_event(conn, session="s1", actor="agent", kind="note",
                      data={"body": "a loose note attached to no op"}, clock=clk)


# ------------------------------------------------------------ the view is byte-stable, no writes
def test_alpaca_day_regenerates_byte_identical_under_fixed_clock(project):
    conn = tedb.connect(project)
    _seed(conn, _clock())
    a = historian.render_day(historian.day(conn, "2026-04-01"))
    b = historian.render_day(historian.day(conn, "2026-04-01"))
    assert a == b
    assert a.startswith("# Day 2026-04-01")


def test_alpaca_day_is_a_view_never_a_source(project):
    conn = tedb.connect(project)
    _seed(conn, _clock())
    before = len(tedb.events(conn, limit=10 ** 9))
    historian.day(conn, "2026-04-01")
    historian.render_day(historian.day(conn, "2026-04-01"))
    after = len(tedb.events(conn, limit=10 ** 9))
    assert after == before          # regenerating the digest wrote nothing to the record


def test_alpaca_day_surfaces_the_unsorted_bucket(project):
    conn = tedb.connect(project)
    _seed(conn, _clock())
    digest = historian.day(conn, "2026-04-01")
    assert digest["totals"]["unsorted"] == 1
    text = historian.render_day(digest)
    assert "surfaced not dropped" in text


# ------------------------------------------------------------ the CLI verb
def test_alpaca_day_cli_prints_digest_and_passes(project, capsys):
    conn = tedb.connect(project)
    _seed(conn, _clock())
    rc = cli.main(["day", "--date", "2026-04-01"])
    assert rc == cli.PASS
    out = capsys.readouterr().out
    assert "# Day 2026-04-01" in out
    assert "op-001" in out
    assert "GATE alpaca-day: PASS" in out


def test_alpaca_day_cli_json_is_parseable(project, capsys):
    conn = tedb.connect(project)
    _seed(conn, _clock())
    rc = cli.main(["day", "--date", "2026-04-01", "--json"])
    assert rc == cli.PASS
    out = capsys.readouterr().out
    payload = json.loads(out.split("GATE alpaca-day")[0])
    assert payload["date"] == "2026-04-01"
    assert payload["totals"]["ops"] == 1
    assert payload["totals"]["unsorted"] == 1


def test_alpaca_day_empty_day_is_a_clean_pass(project, capsys):
    tedb.connect(project)
    rc = cli.main(["day", "--date", "2026-04-02"])
    assert rc == cli.PASS
    out = capsys.readouterr().out
    assert "(no op activity)" in out
    assert "every note is placed in a timeline" in out


if __name__ == "__main__":
    import pytest as _p
    raise SystemExit(_p.main([__file__, "-q"]))
