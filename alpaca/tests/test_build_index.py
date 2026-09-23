import json, os, pytest, re, shutil
import html5lib
from alpaca import cli, db, paths
from alpaca.analytics import build_index

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "transcript_small.jsonl")

def _load(p):
    with open(p, encoding="utf-8") as fh:
        return json.load(fh)

def _seed(project):
    cli.main(["init"])
    tdir = paths.transcript_dir(project); os.makedirs(tdir, exist_ok=True)
    shutil.copy(FIX, os.path.join(tdir, "aaaa1111-2222-3333.jsonl"))
    conn = db.connect(project)
    db.upsert(conn, "sessions", "sid", {"sid": "aaaa1111-2222-3333", "started": "2026-09-15T10:00:00+00:00", "beats": 3, "level": "L3"})
    for _ in range(3):
        db.append_event(conn, session="aaaa1111-2222-3333", actor="agent", kind="heartbeat", data={"tool": "Read", "ref": ""})
    return conn

def test_collect_is_read_only_and_joins_db(project):
    _seed(project)
    sessions = build_index.collect(project)
    assert len(sessions) == 1 and sessions[0]["sid"] == "aaaa1111-2222-3333"
    assert sessions[0]["record"]["heartbeats"] == 3 and sessions[0]["record"]["level"] == "L3"
    assert not os.path.exists(os.path.join(project, ".alpaca", "analytics", "sessions"))

def test_collect_is_incremental(project, monkeypatch):
    _seed(project)
    build_index.collect(project)
    calls = []
    monkeypatch.setattr(build_index.ps, "parse", lambda p, **k: calls.append(p) or {"sid": "x"})
    build_index.collect(project)
    assert calls == []

def test_build_writes_ascii_single_file(project):
    _seed(project)
    p = build_index.build(project)
    with open(p, encoding="utf-8") as fh:
        html = fh.read()
    assert p.endswith(os.path.join("analytics", "index.html"))
    assert re.search(r"[^\x00-\x7F]", html) is None
    data = json.loads(re.search(r'<script id="data" type="application/json">(.*?)</script>', html, re.S).group(1))
    assert data["project"]["events"] >= 4 and data["sessions"][0]["tool_calls"] == 2
    assert "add a hello verb" in html

def test_cli_verbs(project, capsys):
    _seed(project)
    assert cli.main(["analytics", "build"]) == 0
    assert cli.main(["recall", "aaaa1111"]) == 0
    assert "heartbeat" in capsys.readouterr().out

def test_collect_reparses_when_transcript_changes_with_older_mtime(project):
    _seed(project)
    tdir = paths.transcript_dir(project); tpath = os.path.join(tdir, "aaaa1111-2222-3333.jsonl")
    assert build_index.collect(project)[0]["turns"] == 2
    with open(FIX, encoding="utf-8") as fh:
        extra = fh.read().splitlines()[2]
    with open(tpath, "a", encoding="utf-8") as fh:
        fh.write(extra + "\n")
    cache = os.path.join(project, ".alpaca", "analytics", "sessions", "aaaa1111-2222-3333.json")
    old = os.path.getmtime(tpath) - 100
    os.utime(tpath, (old, old))
    assert build_index.collect(project)[0]["turns"] == 3

def test_recall_ambiguous_prefix_fails(project, capsys):
    conn = _seed(project)
    db.upsert(conn, "sessions", "sid", {"sid": "aaaa1111-9999", "started": "2026-09-15T11:00:00+00:00", "beats": 0})
    assert cli.main(["recall", "aaaa1111"]) == 1
    assert "ambiguous" in capsys.readouterr().out

def test_collect_marks_unparseable_transcript(project):
    if os.geteuid() == 0:
        pytest.skip("running as root, cannot test permission denied")
    conn = _seed(project)
    tdir = paths.transcript_dir(project)
    tpath = os.path.join(tdir, "aaaa1111-2222-3333.jsonl")
    try:
        os.chmod(tpath, 0)
        sessions = build_index.collect(project)
        assert len(sessions) == 1
        assert "parse_error" in sessions[0]
        assert sessions[0]["sid"] == "aaaa1111-2222-3333"
        assert build_index.build(project)
        assert os.path.isfile(os.path.join(project, "analytics", "index.html"))
    finally:
        os.chmod(tpath, 0o644)

def test_failed_parse_is_retried_next_run(project):
    if os.geteuid() == 0:
        pytest.skip("root reads anything")
    _seed(project)
    tpath = os.path.join(paths.transcript_dir(project), "aaaa1111-2222-3333.jsonl")
    cache = os.path.join(project, ".alpaca", "analytics", "sessions", "aaaa1111-2222-3333.json")
    os.chmod(tpath, 0)
    try:
        assert "parse_error" in build_index.collect(project)[0]
        assert not os.path.exists(cache)
    finally:
        os.chmod(tpath, 0o644)
    again = build_index.collect(project)[0]
    assert "parse_error" not in again and again["turns"] == 2

def test_build_survives_script_breaking_prompt(project):
    cli.main(["init"])
    tdir = paths.transcript_dir(project); os.makedirs(tdir, exist_ok=True)
    prompt = "<!--<script>alert(1)</script>"
    lines = [
        json.dumps({"type": "user", "timestamp": "2026-09-15T10:00:00.000Z",
                    "message": {"role": "user", "content": prompt}}),
        json.dumps({"type": "assistant", "timestamp": "2026-09-15T10:00:01.000Z",
                    "message": {"model": "claude-sonnet-5", "content": [{"type": "text", "text": "ok"}], "usage": {}}}),
    ]
    with open(os.path.join(tdir, "bbbb2222-3333-4444.jsonl"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    p = build_index.build(project)
    with open(p, encoding="utf-8") as fh:
        html = fh.read()
    doc = html5lib.parse(html, treebuilder="dom")
    scripts = doc.getElementsByTagName("script")
    assert len(scripts) == 2
    data_script = [s for s in scripts if s.getAttribute("id") == "data"][0]
    data = json.loads(data_script.firstChild.nodeValue)
    prompts = [t for s in data["sessions"] for pr in s["human_prompts"] for t in [pr["text"]]]
    assert prompt in prompts


def test_collect_keeps_incomplete_transcript_usage_unknown(project):
    cli.main(["init"])
    tdir = paths.transcript_dir(project); os.makedirs(tdir, exist_ok=True)
    with open(os.path.join(tdir, "missing-usage.jsonl"), "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "assistant", "timestamp": "2026-09-15T10:00:00.000Z",
                             "message": {"model": "claude-sonnet-5", "content": []}}) + "\n")
    session = build_index.collect(project)[0]
    assert session["usage_available"] is False
    assert session["tokens"]["output"] is None
    assert session["cost_notional_usd"] is None


def test_imported_usage_is_deduplicated_and_overrides_record_only_unknown(project, tmp_path):
    from alpaca.analytics import usage
    conn = db.connect(project)
    db.upsert(conn, "sessions", "sid", {"sid": "codex-a", "started": "2026-09-17T00:00:00+00:00"})
    db.meta_set(conn, "hook-cost:codex-a:stop", json.dumps({"runs": 2, "total_ms": 12.5, "last_ms": 7.5}))
    conn.close()
    source = tmp_path / "provider-usage.json"
    source.write_text(json.dumps({"sample_id": "run-123", "provider": "codex", "model": "gpt-5.6-codex",
                                  "tokens": {"input": 9, "output": 4, "cache_read": 0,
                                             "cache_write_5m": 0, "cache_write_1h": 0}, "reported_cost_usd": 1.25}),
                      encoding="utf-8")
    assert usage.ingest(project, "codex-a", str(source))["inserted"] == 1
    assert usage.ingest(project, "codex-a", str(source))["duplicates"] == 1
    session = build_index.collect(project)[0]
    assert session["capture"] == "explicit-usage"
    assert session["usage_available"] is True
    assert session["tokens"]["input"] == 9
    assert session["cost_reported_usd"] == 1.25
    assert session["cost_notional_usd"] is None
    assert session["record"]["hook_runtime_ms"] == 12.5


def test_cli_ingests_only_the_explicit_usage_file(project, tmp_path, capsys):
    source = tmp_path / "provider-usage.json"
    source.write_text(json.dumps({"sample_id": "run-456", "provider": "claude", "model": "claude-sonnet-5",
                                  "tokens": {"input": 9, "output": 4, "cache_read": 0,
                                             "cache_write_5m": 0, "cache_write_1h": 0}}), encoding="utf-8")
    assert cli.main(["analytics", "ingest-usage", "--session", "codex-a", "--file", str(source)]) == 0
    assert "imported 1 sample" in capsys.readouterr().out
    assert os.path.isfile(os.path.join(project, ".alpaca", "analytics", "usage", "codex-a.json"))


def test_usage_import_change_invalidates_transcript_cache(project, tmp_path):
    from alpaca.analytics import usage
    _seed(project)
    first = tmp_path / "first.json"
    first.write_text(json.dumps({"sample_id": "usage-1", "provider": "claude", "model": "claude-sonnet-5",
                                 "tokens": {"input": 1, "output": 2, "cache_read": 0,
                                            "cache_write_5m": 0, "cache_write_1h": 0}}), encoding="utf-8")
    second = tmp_path / "second.json"
    second.write_text(json.dumps({"sample_id": "usage-2", "provider": "claude", "model": "claude-sonnet-5",
                                  "tokens": {"input": 3, "output": 4, "cache_read": 0,
                                             "cache_write_5m": 0, "cache_write_1h": 0}}), encoding="utf-8")
    usage.ingest(project, "aaaa1111-2222-3333", str(first))
    assert build_index.collect(project)[0]["tokens"]["output"] == 70
    assert build_index.collect(project)[0]["imported_usage"]["tokens"]["output"] == 2
    usage.ingest(project, "aaaa1111-2222-3333", str(second))
    assert build_index.collect(project)[0]["tokens"]["output"] == 70
    assert build_index.collect(project)[0]["imported_usage"]["tokens"]["output"] == 6


def test_usage_import_rejects_partial_corrupt_and_conflicting_samples(project, tmp_path):
    from alpaca.analytics import usage
    partial = tmp_path / "partial.json"
    partial.write_text(json.dumps({"sample_id": "s1", "provider": "codex", "model": "gpt",
                                   "tokens": {"input": 1}}), encoding="utf-8")
    with pytest.raises(ValueError, match="input and output"):
        usage.ingest(project, "codex-a", str(partial))
    source = tmp_path / "good.json"
    source.write_text(json.dumps({"sample_id": "s1", "provider": "codex", "model": "gpt",
                                  "tokens": {"input": 1, "output": 2, "cache_read": 0,
                                             "cache_write_5m": 0, "cache_write_1h": 0}}), encoding="utf-8")
    usage.ingest(project, "codex-a", str(source))
    conflict = tmp_path / "conflict.json"
    conflict.write_text(json.dumps({"sample_id": "s1", "provider": "codex", "model": "gpt",
                                    "tokens": {"input": 9, "output": 2, "cache_read": 0,
                                               "cache_write_5m": 0, "cache_write_1h": 0}}), encoding="utf-8")
    with pytest.raises(ValueError, match="conflicts"):
        usage.ingest(project, "codex-a", str(conflict))
    ledger = os.path.join(project, ".alpaca", "analytics", "usage", "codex-a.json")
    assert json.load(open(ledger, encoding="utf-8"))["samples"][0]["tokens"]["input"] == 1
    with open(ledger, "w", encoding="utf-8") as fh:
        fh.write("not json")
    assert usage.ingest(project, "codex-a", str(source))["duplicates"] == 1
    assert json.load(open(ledger, encoding="utf-8"))["samples"][0]["tokens"]["input"] == 1


def test_usage_import_creates_session_and_append_only_event(project, tmp_path):
    from alpaca.analytics import usage
    source = tmp_path / "usage.json"
    source.write_text(json.dumps({"sample_id": "s2", "provider": "codex", "model": "gpt",
                                  "tokens": {"input": 1, "output": 2, "cache_read": 0,
                                             "cache_write_5m": 0, "cache_write_1h": 0}}), encoding="utf-8")
    usage.ingest(project, "codex-new", str(source))
    conn = db.connect(project)
    try:
        assert db.rows(conn, "sessions", "sid=?", ("codex-new",))
        events = db.events(conn, session="codex-new", limit=10)
        assert events[-1]["kind"] == "usage-import" and events[-1]["data"]["samples"][0]["source"]["sha256"]
        assert db.verify_chain(conn)[0]
    finally:
        conn.close()


def test_partial_import_roundtrips_from_events_when_projection_is_missing(project, tmp_path):
    from alpaca.analytics import usage
    source = tmp_path / "partial-cache.json"
    source.write_text(json.dumps({"sample_id": "partial-1", "provider": "codex", "model": "gpt",
                                  "tokens": {"input": 7, "output": 3}}), encoding="utf-8")
    usage.ingest(project, "codex-partial", str(source))
    ledger = os.path.join(project, ".alpaca", "analytics", "usage", "codex-partial.json")
    os.unlink(ledger)
    sample = usage.load(project, "codex-partial")[0]
    assert sample["tokens"] == {"input": 7, "output": 3, "cache_read": None,
                                 "cache_write_5m": None, "cache_write_1h": None}
    session = build_index.collect(project)[0]
    assert session["tokens"]["input"] == 7 and session["tokens"]["cache_read"] is None
    assert session["usage_available"] is False


# --------------------------------------------- op-006: a probe is counted, never listed or summed
def _probe(conn, sid, ts="2026-09-15T12:00:00+00:00"):
    """What the desktop app leaves behind: a session row, an open and a close, nothing else."""
    db.upsert(conn, "sessions", "sid", {"sid": sid, "started": ts, "ended": ts, "beats": 0})
    db.append_event(conn, session=sid, actor="agent", kind="session-start")
    db.append_event(conn, session=sid, actor="agent", kind="session-end")


def _service(conn, sid="instrument"):
    db.upsert(conn, "sessions", "sid", {"sid": sid, "started": "2026-09-15T09:00:00+00:00",
                                        "beats": 0})
    db.append_event(conn, session=sid, actor=sid, kind="run", data={"gate": "g", "verdict": "PASS"})


def test_every_session_carries_its_class(project):
    conn = _seed(project)
    _probe(conn, "probe-1")
    _service(conn)
    by_sid = {s["sid"]: s["class"] for s in build_index.collect(project)}
    assert by_sid["aaaa1111-2222-3333"] == "work"
    assert by_sid["probe-1"] == "probe"
    assert by_sid["instrument"] == "service"


def test_the_fold_lists_no_probe_and_counts_them_beside_the_totals(project):
    conn = _seed(project)
    for n in range(7):
        _probe(conn, "probe-%d" % n)
    data = build_index._fold(project, build_index.collect(project))
    listed = {s["sid"] for s in data["sessions"]}
    assert listed == {"aaaa1111-2222-3333"}, "a probe is never a row in the session list"
    assert data["project"]["sessions"] == 1, "the totals count working sessions only"
    assert data["project"]["probe_sessions"] == 7
    assert data["probes"]["count"] == 7
    assert data["probes"]["first"] and data["probes"]["last"]


def test_probe_turns_and_tool_calls_never_dilute_the_totals(project):
    conn = _seed(project)
    plain = build_index._fold(project, build_index.collect(project))["project"]
    for n in range(20):
        _probe(conn, "probe-%d" % n)
    flooded = build_index._fold(project, build_index.collect(project))["project"]
    for key in ("turns", "tool_calls", "prompts"):
        assert flooded[key] == plain[key], key
    assert flooded["probe_sessions"] == 20 and plain["probe_sessions"] == 0


def test_a_service_id_stays_listed_but_out_of_the_totals(project):
    conn = _seed(project)
    _service(conn)
    data = build_index._fold(project, build_index.collect(project))
    assert "instrument" in {s["sid"] for s in data["sessions"]}, "the service writer is still visible"
    assert data["project"]["sessions"] == 1, "but it is not a sitting anyone had"


def test_the_page_still_carries_the_working_session_after_a_probe_flood(project):
    conn = _seed(project)
    for n in range(30):
        _probe(conn, "probe-%02d" % n)
    p = build_index.build(project)
    with open(p, encoding="utf-8") as fh:
        html = fh.read()
    data = json.loads(re.search(r'<script id="data" type="application/json">(.*?)</script>',
                                html, re.S).group(1))
    assert [s["sid"] for s in data["sessions"]] == ["aaaa1111-2222-3333"]
    assert data["project"]["probe_sessions"] == 30
