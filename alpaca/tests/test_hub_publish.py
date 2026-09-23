"""`alpaca hub publish`: this workspace's tile for a host hub's drop directory.

The publisher is the host hub's standalone publisher script carried into the product, so the
cases below are the hub's own publisher tests, copied: tile content from data.json and the
board.json fallback, caps, the shared character rule (every code point), href checks before any
write, symlink and FIFO refusal, --print writing nothing. `hub_accepts` is a copy of the hub's
schema v1 check (the hub's tile reader), so a tile these tests accept is one the hub accepts. Added
here: settings from project.yaml and $ALPACA_HUB_DROP, the atomic write, the session-end call when
hub.enabled is true (and nothing when it is not), a publish error that never breaks the hook, and
the printed systemd timer.
"""
import datetime
import json
import os
import stat
import subprocess
import sys
import unicodedata

import pytest
import yaml

from alpaca import cli, db, hub_publish as pub
from alpaca.tests.conftest import REPO

ME = pub.current_user()


# ---- a copy of the hub's tile check (schema v1) --------------------------------------------------

HUB_BAD_CATEGORIES = {"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"}
HUB_INVISIBLE = {0x115F, 0x1160, 0x3164, 0xFFA0, 0x2800, 0x180E}
HUB_CAPS = {"user": 32, "slug": 40, "name": 48, "what": 120, "href": 200, "updated": 40,
            "headline": 200, "label": 48, "next": 240, "level": 16, "count_key": 24}


def hub_bad_char(value):
    for ch in value:
        if unicodedata.category(ch) in HUB_BAD_CATEGORIES or ord(ch) in HUB_INVISIBLE:
            return ch
    return None


def hub_accepts(data):
    """The hub's validate(): raises AssertionError with the first problem, else returns True."""
    def text(value, field, cap):
        assert isinstance(value, str), "%s must be a string" % field
        assert len(value) <= cap, "%s is over its cap %d" % (field, cap)
        assert hub_bad_char(value) is None, "%s holds a refused character" % field

    def count(value, field):
        assert not isinstance(value, bool) and isinstance(value, int) and 0 <= value <= 10 ** 9, field

    assert isinstance(data, dict)
    assert not set(data) - {"v", "user", "slug", "name", "what", "href", "updated", "status"}
    for key in ("v", "user", "slug", "name", "updated"):
        assert key in data, "missing key %s" % key
    assert data["v"] == 1 and not isinstance(data["v"], bool)
    text(data["user"], "user", HUB_CAPS["user"])
    assert pub.USER_RE.fullmatch(data["user"])
    text(data["slug"], "slug", HUB_CAPS["slug"])
    assert pub.SLUG_RE.fullmatch(data["slug"])
    text(data["name"], "name", HUB_CAPS["name"])
    assert data["name"].strip()
    for key in ("what", "href"):
        if key in data:
            text(data[key], key, HUB_CAPS[key])
    text(data["updated"], "updated", HUB_CAPS["updated"])
    assert datetime.datetime.fromisoformat(data["updated"].replace("Z", "+00:00")).tzinfo is not None
    status = data.get("status", {})
    assert isinstance(status, dict)
    assert not set(status) - {"headline", "progress", "counts", "next", "level", "live"}
    for key in ("headline", "next", "level"):
        if status.get(key) is not None:
            text(status[key], "status." + key, HUB_CAPS[key])
    if status.get("live") is not None:
        count(status["live"], "status.live")
    if status.get("progress") is not None:
        prog = status["progress"]
        assert not set(prog) - {"done", "total", "label"} and "done" in prog and "total" in prog
        count(prog["done"], "done")
        count(prog["total"], "total")
        assert prog["done"] <= prog["total"]
        if prog.get("label") is not None:
            text(prog["label"], "status.progress.label", HUB_CAPS["label"])
    if status.get("counts") is not None:
        assert isinstance(status["counts"], dict) and len(status["counts"]) <= 6
        for key, value in status["counts"].items():
            text(key, "status.counts key", HUB_CAPS["count_key"])
            assert key.strip()
            count(value, "status.counts[%s]" % key)
    return True


# ---- fixtures ------------------------------------------------------------------------------------

DATA = {"generated": "2026-01-01T00:00:00+00:00", "pulse": {
    "ops_open": 2, "tasks": {"open": 3, "doing": 1, "done": 4},
    "now": {"level": "L3", "next_action": "t-2 [doing] wire the publisher",
            "ops": [{"id": "op-1", "status": "closed", "title": "old", "rows_total": 1, "rows_done": 1},
                    {"id": "op-2", "status": "open", "title": "Build the demo", "rows_total": 6,
                     "rows_done": 5, "rows_blocked": 1, "tasks_total": 3, "tasks_done": 2}],
            "sessions": {"work": [{"active": True}, {"active": False}, {"active": True}]}}}}
BOARD = {"columns": ["todo", "doing", "blocked", "done"], "counts": {"todo": 2, "doing": 1, "blocked": 1, "done": 4},
         "ops": ["op-2"]}


def configure(root, **hub):
    """Write project.yaml with a `hub:` block (the shipped template's keys, overridden by `hub`)."""
    settings = {"enabled": False, "drop": "/srv/alpaca-hub/tiles", "slug": None, "name": None,
                "href": None, "what": None}
    settings.update(hub)
    with open(os.path.join(root, "project.yaml"), "w", encoding="utf-8") as fh:
        yaml.safe_dump({"name": "Demo Space", "hub": settings}, fh, sort_keys=False)


def projections(root, data=DATA, board=BOARD):
    if data is not None:
        with open(os.path.join(root, "data.json"), "w", encoding="utf-8") as fh:
            json.dump(data, fh)
    if board is not None:
        with open(os.path.join(root, "board.json"), "w", encoding="utf-8") as fh:
            json.dump(board, fh)


@pytest.fixture
def drop(tmp_path):
    d = tmp_path / "drop"
    d.mkdir()
    os.chmod(d, 0o2770)
    return str(d)


@pytest.fixture(autouse=True)
def no_env_drop(monkeypatch):
    monkeypatch.delenv(pub.DROP_ENV, raising=False)


# ---- tile content (copied cases) ---------------------------------------------------------------

def test_publish_from_data_json_is_a_tile_the_hub_accepts(project, drop):
    projections(project)
    configure(project, enabled=True, drop=drop, slug="demo", name="Demo", href="https://demo.example.com/",
              what="Demo workspace")
    path = pub.publish(project)
    assert path == os.path.join(drop, "%s--demo.json" % ME)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o640
    assert [n for n in os.listdir(drop) if n.startswith(".")] == []          # no temp left behind
    tile = json.load(open(path))
    assert tile["status"]["headline"] == "op-2: Build the demo"             # the first OPEN op
    assert tile["status"]["progress"] == {"done": 5, "total": 6, "label": "rows discharged, 1 blocked"}
    assert tile["status"]["counts"] == {"open ops": 2, "doing": 1, "queued": 3, "done": 4}
    assert list(tile["status"]["counts"]) == ["open ops", "doing", "queued", "done"]
    assert tile["status"]["next"] == "t-2 [doing] wire the publisher"
    assert tile["status"]["level"] == "L3" and tile["status"]["live"] == 2
    assert (tile["user"], tile["slug"], tile["name"]) == (ME, "demo", "Demo")
    assert tile["what"] == "Demo workspace" and tile["href"] == "https://demo.example.com/"
    assert list(tile) == ["v", "user", "slug", "name", "updated", "status", "what", "href"]
    assert hub_accepts(tile)


def test_tasks_progress_when_the_op_has_no_rows(project):
    data = json.loads(json.dumps(DATA))
    data["pulse"]["now"]["ops"][1].update(rows_total=0, rows_done=0)
    projections(project, data=data)
    tile = pub.build(project, user=ME, slug="s", name="S")
    assert tile["status"]["progress"] == {"done": 2, "total": 3, "label": "tasks done"}
    data["pulse"]["now"]["ops"] = []
    projections(project, data=data)
    assert pub.build(project, user=ME, slug="s", name="S")["status"]["headline"] == "No open operation."


def test_publish_board_only_fallback(project, drop):
    projections(project, data=None)
    configure(project, enabled=True, drop=drop, slug="board-only", name="Board")
    tile = json.load(open(pub.publish(project)))
    assert tile["status"]["counts"] == {c: BOARD["counts"][c] for c in BOARD["columns"]}
    assert tile["status"]["progress"] == {"done": 4, "total": 8, "label": "rows discharged, 1 blocked"}
    assert tile["status"]["headline"] == "1 operations on the board"
    assert tile["what"] == "Workspace, published from board.json"
    assert hub_accepts(tile)
    # a data.json with no pulse falls back to the board too
    projections(project, data={"board": {}})
    assert pub.build(project, user=ME, slug="s", name="S")["what"] == "Workspace, published from board.json"


def test_no_projection_is_refused(project):
    with pytest.raises(pub.PublishError, match="neither"):
        pub.build(project, user=ME, slug="s", name="S")


def test_publisher_clips_long_text(project):
    projections(project, data={"pulse": {"ops_open": 1, "tasks": {"open": 1, "doing": 0, "done": 0},
        "now": {"level": "L2-very-long-level-name", "next_action": "n " * 400,
                "ops": [{"id": "op-1", "status": "open", "title": "t\n" * 300, "rows_total": 4, "rows_done": 9}],
                "sessions": {"work": [{"active": True}, {"active": False}]}}}})
    tile = pub.build(project, user=ME, slug="long", name="N" * 80)
    assert len(tile["name"]) <= 48 and len(tile["status"]["next"]) <= 240 and len(tile["status"]["level"]) <= 16
    assert "\n" not in tile["status"]["headline"]
    assert tile["status"]["progress"] == {"done": 4, "total": 4, "label": "rows discharged"}
    assert tile["status"]["live"] == 1
    assert hub_accepts(tile)


def test_print_writes_nothing_and_works_while_disabled(project, drop, capsys):
    projections(project, data=None, board={"columns": ["todo"], "counts": {"todo": 2}})
    configure(project, enabled=False, drop=drop, slug="p", name="P")
    assert cli.main(["hub", "publish", "--print"]) == cli.PASS
    printed = capsys.readouterr().out
    assert json.loads(printed)["slug"] == "p"
    assert os.listdir(drop) == []


# ---- the shared character rule (copied case N4) --------------------------------------------------

def test_publisher_rejects_exactly_what_the_hub_rejects():
    mismatches = [cp for cp in range(0x110000) if pub.is_bad_char(chr(cp)) != (hub_bad_char(chr(cp)) is not None)]
    assert mismatches == []


def test_published_text_is_accepted_by_the_hub(project, drop):
    projections(project, data=None, board={"op": "op\u202e-1 \u3164 title", "columns": ["todo"],
                                           "counts": {"to\u200bdo": 1}})
    configure(project, enabled=True, drop=drop, slug="s", name="A\u3164B\u2800C", what="x\u200by\ufeff")
    tile = json.load(open(pub.publish(project)))
    assert hub_accepts(tile) and tile["name"] == "ABC"


# ---- settings and href checks (copied cases L3) ---------------------------------------------------

def test_bad_settings_are_refused_before_anything_is_written(project, drop):
    projections(project)
    for bad in ("https://a.example.com/\x01", "https://user:pw@a.example.com/", "https://bad_host/",
                "https://a.example.com:99999/", "https://a.example.com/\u202e", "http://a.example.com/",
                "https://" + "a" * 200 + ".example.com/"):
        configure(project, enabled=True, drop=drop, href=bad)
        with pytest.raises(pub.PublishError, match="href"):
            pub.publish(project)
    configure(project, enabled=True, drop=drop, slug="Bad Slug")
    with pytest.raises(pub.PublishError, match="slug"):
        pub.publish(project)
    configure(project, enabled="yes", drop=drop)
    with pytest.raises(pub.PublishError, match="enabled"):
        pub.publish(project)
    configure(project, enabled=True, drop=drop, name="\u200b\u3164")
    with pytest.raises(pub.PublishError, match="name"):
        pub.publish(project)
    assert os.listdir(drop) == []


def test_default_slug_and_name_come_from_the_project_name(project, drop):
    projections(project)
    configure(project, enabled=True, drop=drop)
    conf = pub.settings(project)
    assert (conf["slug"], conf["name"], conf["user"]) == ("demo-space", "Demo Space", ME)
    assert pub.default_slug("  ") == "workspace" and pub.default_slug("A.B__c") == "a-b-c"
    assert os.path.basename(pub.publish(project)) == "%s--demo-space.json" % ME


def test_the_drop_is_the_flag_then_the_env_then_project_yaml(project, tmp_path, monkeypatch):
    configure(project, drop="/from/project/yaml")
    assert pub.settings(project)["drop"] == "/from/project/yaml"
    monkeypatch.setenv(pub.DROP_ENV, "/from/env")
    assert pub.settings(project)["drop"] == "/from/env"
    assert pub.settings(project, drop="/from/flag")["drop"] == "/from/flag"
    monkeypatch.delenv(pub.DROP_ENV)
    configure(project)
    with open(os.path.join(project, "project.yaml"), encoding="utf-8") as fh:
        doc = yaml.safe_load(fh)
    del doc["hub"]
    with open(os.path.join(project, "project.yaml"), "w", encoding="utf-8") as fh:
        yaml.safe_dump(doc, fh)
    conf = pub.settings(project)
    assert conf["drop"] == "/srv/alpaca-hub/tiles" and conf["enabled"] is False


def test_the_shipped_template_carries_hub_settings_off_and_neutral():
    with open(os.path.join(REPO, "project.yaml"), encoding="utf-8") as fh:
        hub = yaml.safe_load(fh)["hub"]
    assert hub == {"enabled": False, "drop": "/srv/alpaca-hub/tiles", "slug": None, "name": None,
                   "href": None, "what": None}


def test_disabled_publish_is_refused_and_writes_nothing(project, drop, capsys):
    projections(project)
    configure(project, enabled=False, drop=drop)
    with pytest.raises(pub.PublishError, match="hub.enabled is false"):
        pub.publish(project)
    assert cli.main(["hub", "publish"]) == cli.BLOCKED
    assert "hub.enabled is false" in capsys.readouterr().err
    assert os.listdir(drop) == []


# ---- projection refusal (copied cases L3) --------------------------------------------------------

def test_a_symlinked_projection_is_refused(project, drop, tmp_path):
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps({"columns": ["todo"], "counts": {"todo": 1}}))
    os.symlink(outside, os.path.join(project, "board.json"))
    configure(project, enabled=True, drop=drop)
    with pytest.raises(pub.PublishError, match="symlink"):
        pub.publish(project)
    assert os.listdir(drop) == []


def test_a_fifo_projection_is_refused_without_hanging(project, drop):
    os.mkfifo(os.path.join(project, "data.json"))
    configure(project, enabled=True, drop=drop)
    code = ("import sys\nfrom alpaca import hub_publish as p\n"
            "try:\n    p.publish(sys.argv[1])\nexcept p.PublishError as e:\n    print(e, file=sys.stderr); sys.exit(2)\n")
    try:
        run = subprocess.run([sys.executable, "-c", code, project], capture_output=True, text=True, timeout=20,
                             env={**os.environ, "PYTHONPATH": REPO})
    except subprocess.TimeoutExpired:
        pytest.fail("alpaca hub publish hung on a FIFO projection")
    assert run.returncode == 2 and "not a regular file" in run.stderr, run.stderr
    assert os.listdir(drop) == []


# ---- the atomic write ----------------------------------------------------------------------------

def test_the_tile_is_replaced_by_rename_never_rewritten_in_place(project, drop, monkeypatch):
    projections(project)
    configure(project, enabled=True, drop=drop, slug="atomic")
    first = pub.publish(project)
    keep = os.path.join(os.path.dirname(drop), "old-tile-link")
    os.link(first, keep)                                   # a second name for the old inode
    old_bytes = open(keep, "rb").read()
    data = json.loads(json.dumps(DATA))
    data["pulse"]["ops_open"] = 7
    projections(project, data=data)
    second = pub.publish(project)
    assert second == first
    assert open(keep, "rb").read() == old_bytes            # the old file was never written into
    assert os.stat(keep).st_ino != os.stat(second).st_ino
    assert json.load(open(second))["status"]["counts"]["open ops"] == 7
    # a failed rename leaves the old tile whole and no temp file behind
    def boom(src, dst):
        raise OSError("rename failed")
    monkeypatch.setattr(pub.os, "replace", boom)
    data["pulse"]["ops_open"] = 9
    projections(project, data=data)
    with pytest.raises(OSError):
        pub.publish(project)
    assert json.load(open(second))["status"]["counts"]["open ops"] == 7
    assert sorted(os.listdir(drop)) == ["%s--atomic.json" % ME]


def test_a_symlink_planted_at_the_temp_name_is_not_followed(project, drop, tmp_path):
    projections(project)
    configure(project, enabled=True, drop=drop, slug="plant")
    target = tmp_path / "victim.txt"
    target.write_text("untouched\n")
    os.symlink(target, os.path.join(drop, ".%s--plant.json.tmp-%d" % (ME, os.getpid())))
    with pytest.raises(OSError):
        pub.publish(project)
    assert target.read_text() == "untouched\n"
    assert not os.path.exists(os.path.join(drop, "%s--plant.json" % ME))


def test_a_tile_over_16_kib_is_refused_before_writing(drop):
    tile = {"v": 1, "user": ME, "slug": "big", "name": "Big", "updated": "2026-01-01T00:00:00+00:00",
            "status": {"headline": "x" * 20000}}
    with pytest.raises(pub.PublishError, match="16384"):
        pub.write_tile(drop, tile)
    assert os.listdir(drop) == []


# ---- session end ---------------------------------------------------------------------------------

ENV = lambda project: {**os.environ, "CLAUDE_PROJECT_DIR": project, "PYTHONPATH": REPO}


def end_session(project):
    return subprocess.run([sys.executable, "-m", "alpaca.hooks.session_end"],
                          input=json.dumps({"session_id": "s1", "reason": "exit"}), capture_output=True,
                          text=True, encoding="utf-8", cwd=project, env=ENV(project))


def test_session_end_publishes_when_enabled(project, drop):
    cli.main(["init"])
    projections(project)
    configure(project, enabled=True, drop=drop, slug="ended")
    run = end_session(project)
    assert run.returncode == 0 and run.stdout == "", run.stderr
    tile = json.load(open(os.path.join(drop, "%s--ended.json" % ME)))
    assert hub_accepts(tile) and tile["status"]["headline"] == "op-2: Build the demo"
    assert db.events(db.connect(project), kind="hub-publish-failed") == []


def test_session_end_is_silent_when_disabled(project, drop, monkeypatch):
    cli.main(["init"])
    projections(project)
    configure(project, enabled=False, drop=drop)
    run = end_session(project)
    assert run.returncode == 0 and run.stdout == "" and "hub" not in run.stderr
    assert os.listdir(drop) == []
    conn = db.connect(project)
    assert [e for e in db.events(conn) if e["kind"].startswith("hub-")] == []
    assert db.events(conn, kind="session-end")
    # in process too: the publisher is asked, and returns without reading a projection
    from alpaca.hooks import session_end
    called = []
    monkeypatch.setattr(pub, "publish", lambda *a, **k: called.append(a))
    session_end.handle({"session_id": "s2", "reason": "exit", "cwd": project})
    assert called == []


def test_a_publish_error_never_breaks_the_session_end(project, tmp_path):
    cli.main(["init"])
    projections(project)
    configure(project, enabled=True, drop=str(tmp_path / "no-such-drop"))
    run = end_session(project)
    assert run.returncode == 0 and run.stdout == "", run.stderr
    conn = db.connect(project)
    failed = db.events(conn, kind="hub-publish-failed")
    assert len(failed) == 1 and "FileNotFoundError" in failed[0]["data"]["error"]
    assert db.rows(conn, "sessions", "sid='s1'")[0]["ended"]      # the rest of the hook ran
    assert os.path.isfile(os.path.join(project, "analytics", "index.html"))


def test_session_end_in_process_calls_the_publisher_when_enabled(project, drop, monkeypatch):
    cli.main(["init"])
    projections(project)
    configure(project, enabled=True, drop=drop)
    from alpaca.hooks import session_end
    called = []
    monkeypatch.setattr(pub, "publish", lambda root, **k: called.append(root))
    session_end.handle({"session_id": "s3", "reason": "exit", "cwd": project})
    assert called == [project]
    def boom(root, **k):
        raise RuntimeError("drop gone")
    monkeypatch.setattr(pub, "publish", boom)
    assert session_end.handle({"session_id": "s4", "reason": "exit", "cwd": project, "_strict": True})["ended"]


# ---- the timer -----------------------------------------------------------------------------------

def test_timer_prints_a_service_and_timer_and_writes_nothing(project, capsys):
    configure(project, slug="demo")
    before = sorted(os.listdir(project))
    assert cli.main(["hub", "timer"]) == cli.PASS
    captured = capsys.readouterr()
    out = captured.out
    assert "~/.config/systemd/user/alpaca-hub-publish-demo.service" in out
    assert "~/.config/systemd/user/alpaca-hub-publish-demo.timer" in out
    assert "Type=oneshot" in out and "OnUnitActiveSec=60s" in out and "WantedBy=timers.target" in out
    assert 'ExecStart="%s" hub publish' % os.path.join(os.path.realpath(project), "bin", "alpaca") in out
    assert "UMask=0027" in out
    assert "hub.enabled is false" in captured.err
    assert sorted(os.listdir(project)) == before


def test_timer_quotes_the_path_for_systemd():
    text = pub.timer_units('/w s/a"b%c$d', "x")
    assert 'ExecStart="/w s/a\\"b%%c$$d/bin/alpaca" hub publish' in text
