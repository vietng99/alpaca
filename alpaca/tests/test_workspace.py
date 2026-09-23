"""t-089: the host workspace registry, the combined tunnel ingress and the hub tiles.

Every test runs against a tmp registry ($ALPACA_WORKSPACES) and a fake HOME holding a fake
~/.cloudflared, so nothing here reads or writes the real ~/.config or ~/.cloudflared, and the only
ports bound are ephemeral ones.
"""
import base64
import builtins
import http.server
import json
import os
import shutil
import socket
import threading
import time
import urllib.request
from pathlib import Path

import pytest
import yaml

from alpaca import cli, serve, workspace

LIVE = """tunnel: 0a1b2c3d-0000-4000-8000-000000000000
credentials-file: {home}/.cloudflared/0a1b2c3d-0000-4000-8000-000000000000.json

protocol: http2

ingress:
  - hostname: example.com
    service: http://127.0.0.1:7328
  - service: http_status:404
"""


@pytest.fixture(autouse=True)
def host(tmp_path, monkeypatch):
    """A fake host: HOME with a fake ~/.cloudflared/config.yml, and a tmp registry."""
    home = tmp_path / "home"
    (home / ".cloudflared").mkdir(parents=True)
    (home / ".cloudflared" / "config.yml").write_text(LIVE.format(home=home))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("ALPACA_CLOUDFLARED_CONFIG", str(home / ".cloudflared" / "config.yml"))
    monkeypatch.setenv("ALPACA_WORKSPACES", str(tmp_path / "registry" / "workspaces.json"))
    return home


def make_instance(tmp_path, name, login=True):
    root = tmp_path / name
    (root / ".alpaca").mkdir(parents=True)
    shutil.copy(os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                             "ALPACA-MANIFEST"), root / "ALPACA-MANIFEST")
    if login:
        (root / ".alpaca" / "files-auth").write_text(":" + name + "-code")
    return str(root)


def login(root, value=":246810"):
    Path(root, ".alpaca").mkdir(parents=True, exist_ok=True)
    Path(root, ".alpaca", "files-auth").write_text(value)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_add_registers_this_instance_with_a_local_url_never_7328(project, host):
    login(project)
    result = workspace.add(project, name="Scratch")
    item = result["workspace"]
    assert item["id"] == workspace.instance_id(project)
    assert item["port"] != 7328 and item["port"] not in workspace.RESERVED_PORTS
    assert result["local"] == "http://127.0.0.1:%d/" % item["port"]
    assert item["href"] == "http://127.0.0.1:%d/hub/" % item["port"]
    assert item["hostname"] is None and item["root"] == os.path.realpath(project)
    registry = Path(os.environ["ALPACA_WORKSPACES"])
    assert registry.stat().st_mode & 0o777 == 0o600
    assert json.loads(registry.read_text())["workspaces"] == [item]
    # re-adding keeps the registered port and adds the hostname
    again = workspace.add(project, hostname="Scratch.Example.com")["workspace"]
    assert again["port"] == item["port"] and again["hostname"] == "scratch.example.com"
    assert again["href"] == "https://scratch.example.com/hub/"
    assert workspace.instance_port(project) == item["port"]


def test_add_refuses_a_taken_port_or_hostname_and_the_reserved_port(project, tmp_path):
    other = make_instance(tmp_path, "other")
    held = workspace.add(other, port=free_port(), hostname="other.example.com")["workspace"]
    with pytest.raises(ValueError, match="registered to"):
        workspace.add(project, port=held["port"])
    with pytest.raises(ValueError, match="registered to"):
        workspace.add(project, hostname="other.example.com")
    with pytest.raises(ValueError, match="reserved"):
        workspace.add(project, port=7328)
    # the live config routes example.com to 7328: that hostname is not free either
    with pytest.raises(ValueError, match="already routed"):
        workspace.add(project, hostname="example.com")
    with pytest.raises(ValueError):
        workspace.add(project, hostname="not a host")
    assert [e["id"] for e in workspace.load()] == [held["id"]]


def test_the_reserved_port_is_allowed_only_when_it_is_this_instances_own(project, host, monkeypatch):
    # with the foreign live rule gone, 7328 is refused for a new instance and allowed for the
    # instance that already serves on it
    (host / ".cloudflared" / "config.yml").write_text("tunnel: x\ncredentials-file: /c.json\n")
    login(project)
    with pytest.raises(ValueError, match="reserved"):
        workspace.add(project, port=7328)
    monkeypatch.setattr(serve, "_read_state", lambda root: {"port": 7328})
    assert workspace.add(project, port=7328)["workspace"]["port"] == 7328


def test_list_and_remove(project, tmp_path, capsys):
    other = make_instance(tmp_path, "other")
    workspace.add(other, port=free_port())
    assert cli.main(["workspace", "add", "--name", "Scratch"]) == cli.PASS
    added = json.loads(capsys.readouterr().out)["workspace"]
    assert cli.main(["workspace", "list"]) == cli.PASS
    listing = json.loads(capsys.readouterr().out)
    assert listing["self"] == added["id"] and len(listing["workspaces"]) == 2
    assert cli.main(["workspace", "remove"]) == cli.PASS
    assert json.loads(capsys.readouterr().out)["removed"] == added["id"]
    assert [e["root"] for e in workspace.load()] == [os.path.realpath(other)]
    assert cli.main(["workspace", "add", "--port", "7328"]) == cli.USAGE
    assert "reserved" in json.loads(capsys.readouterr().out)["error"]


def test_render_ingress_combines_every_hostname_and_carries_the_live_file(project, tmp_path, host, capsys):
    other = make_instance(tmp_path, "other")
    b = workspace.add(other, port=free_port(), hostname="b.example.com")["workspace"]
    login(project)
    a = workspace.add(project, name="A", hostname="a.example.com")["workspace"]
    live = host / ".cloudflared" / "config.yml"
    before = live.read_bytes()
    assert cli.main(["workspace", "render-ingress", "--tunnel", "main"]) == cli.PASS
    printed = capsys.readouterr().out
    out = Path(project, ".alpaca", "services", "cloudflared-main.yml")
    config = yaml.safe_load(out.read_text())
    assert config["tunnel"] == "0a1b2c3d-0000-4000-8000-000000000000"
    assert config["credentials-file"].endswith("0a1b2c3d-0000-4000-8000-000000000000.json")
    assert config["protocol"] == "http2"
    rules = config["ingress"]
    # the live rules first and unchanged, the registered hostnames before the live catch-all
    assert rules == [{"hostname": "example.com", "service": "http://127.0.0.1:7328"},
                     {"hostname": "a.example.com", "service": "http://127.0.0.1:%d" % a["port"]},
                     {"hostname": "b.example.com", "service": "http://127.0.0.1:%d" % b["port"]},
                     {"service": "http_status:404"}]
    assert out.stat().st_mode & 0o777 == 0o600
    assert any(line.startswith("+") and "http://127.0.0.1:%d" % a["port"] in line for line in printed.splitlines())
    assert "--- " + str(live) in printed
    assert "cloudflared tunnel route dns main a.example.com" in printed
    assert "cloudflared tunnel route dns main b.example.com" in printed
    assert "route dns main example.com" not in printed
    assert "install -m 600 %s %s" % (out, live) in printed
    assert live.read_bytes() == before


def test_render_ingress_flags_and_refusals(project, host, tmp_path):
    login(project)
    workspace.add(project, hostname="a.example.com")
    (host / ".cloudflared" / "config.yml").write_text("ingress:\n  - service: http_status:404\n")
    with pytest.raises(ValueError, match="tunnel-id"):
        workspace.render_ingress(project, "t")
    out = tmp_path / "combined.yml"
    result = workspace.render_ingress(project, "t", out=str(out), tunnel_id="abc", credentials="/c/abc.json")
    config = yaml.safe_load(out.read_text())
    assert config["tunnel"] == "abc" and config["protocol"] == "http2"
    assert [r.get("hostname") for r in config["ingress"]] == ["a.example.com", None]
    assert result["dns"] == ["cloudflared tunnel route dns t a.example.com"]
    before = (host / ".cloudflared" / "config.yml").read_bytes()
    for bad in (str(host / ".cloudflared" / "config.yml"), str(host / ".cloudflared" / "x.yml")):
        with pytest.raises(ValueError, match="owner"):
            workspace.render_ingress(project, "t", out=bad, tunnel_id="abc", credentials="/c/abc.json")
    assert (host / ".cloudflared" / "config.yml").read_bytes() == before
    assert not (host / ".cloudflared" / "x.yml").exists()
    (host / ".cloudflared" / "config.yml").unlink()
    with pytest.raises(ValueError, match="does not exist"):
        workspace.render_ingress(project, "t", out=str(out), tunnel_id="abc", credentials="/c/abc.json")
    with pytest.raises(ValueError):
        workspace.render_ingress(project, "--token=x", tunnel_id="abc", credentials="/c")


def test_service_units_use_the_registered_port_and_never_default_to_7328(project, monkeypatch):
    units = serve.service_units(project)
    dashboard = next(body for name, body in units.items() if name.endswith("-dashboard.service"))
    assert "--port 7328" not in dashboard
    port = free_port()
    workspace.add(project, port=port)
    dashboard = next(body for name, body in serve.service_units(project).items() if name.endswith("-dashboard.service"))
    assert dashboard.split("ExecStart=", 1)[1].splitlines()[0].endswith("--port %d" % port)
    # the derived default skips the reserved port even when the root hashes onto it
    monkeypatch.setattr(serve.util, "sha256_hex", lambda value: "%04x" % (28 + 90 * 7))
    monkeypatch.setattr(serve, "_port_open", lambda port, host="127.0.0.1": False)
    assert 7300 + (28 + 90 * 7) % 90 == 7328
    assert serve._default_port(project) != 7328


def test_collect_services_defaults_to_the_registered_port(project, capsys):
    port = free_port()
    workspace.add(project, port=port)
    assert cli.main(["collect", "services"]) == cli.PASS
    result = json.loads(capsys.readouterr().out)
    assert result["port"] == port and result["local"] == "http://127.0.0.1:%d/" % port
    body = Path(result["directory"], next(n for n in result["units"] if n.endswith("-dashboard.service"))).read_text()
    assert "--port %d" % port in body and "7328" not in body
    assert cli.main(["serve", "--write-services"]) == cli.PASS
    files = json.loads(capsys.readouterr().out)["files"]
    assert "--port %d" % port in Path(next(f for f in files if f.endswith("-dashboard.service"))).read_text()


class Board(http.server.BaseHTTPRequestHandler):
    code = 200

    def log_message(self, *a):
        pass

    def do_GET(self):
        self.server.seen.append((self.path, self.headers.get("Authorization")))
        body = json.dumps({"pulse": {"ops_open": 2, "tasks": {"open": 3, "doing": 1, "done": 9},
                                     "now": {"ops": [{"id": "op-7", "title": "Ship it", "status": "open",
                                                      "tasks_total": 4, "tasks_done": 1}],
                                             "next_action": "review t-1", "as_of": "2026-09-23T10:00:00Z",
                                             "sessions": {"work": [{"active": True}, {"active": False}]}}},
                           "who": self.server.who}).encode()
        self.send_response(self.code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def start(handler, who="other"):
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    httpd.who = who
    httpd.seen = []
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def test_hub_lists_registered_instances_and_reads_their_status_server_side(project, tmp_path):
    assert [w["href"] for w in serve.workspaces(project)] == ["/hub/"]     # no registry: self only
    up, open_ = start(Board), start(Board)
    try:
        b = workspace.add(make_instance(tmp_path, "b"), name="B", port=up.server_address[1],
                          hostname="b.example.com")["workspace"]
        c = workspace.add(make_instance(tmp_path, "c", login=False), name="C", port=open_.server_address[1])["workspace"]
        d = workspace.add(make_instance(tmp_path, "d"), name="D", port=free_port())["workspace"]
        login(project)
        workspace.add(project, name="Self", hostname="self.example.com")
        tiles = serve.workspaces(project)
        assert tiles[0]["self"] and tiles[0]["href"] == "/hub/" and tiles[0]["name"] == "Self"
        assert tiles[0]["public"] == "https://self.example.com/hub/"
        by = {t["id"]: t for t in tiles[1:]}
        assert by[b["id"]]["href"] == "https://b.example.com/hub/"
        assert by[c["id"]]["href"] == "http://127.0.0.1:%d/hub/" % c["port"]
        code, body = serve.workspace_summary(project, b["id"])
        assert code == 200 and json.loads(body) == {"reachable": True}          # reachability only
        code, body = serve.workspace_summary(project, c["id"])          # no login of its own
        assert json.loads(body) == {"reachable": True}
        assert up.seen == [("/login", None)] and open_.seen == [("/login", None)]
        started = time.monotonic()
        assert json.loads(serve.workspace_summary(project, d["id"])[1]) == {"reachable": False}
        assert time.monotonic() - started < 3
        assert serve.workspace_summary(project, "nope")[0] == 404
        assert serve.workspace_summary(project, workspace.instance_id(project))[0] == 404
    finally:
        for httpd in (up, open_):
            httpd.shutdown()
            httpd.server_close()


def test_status_read_gives_up_after_two_seconds():
    silent = socket.socket()
    silent.bind(("127.0.0.1", 0))
    silent.listen(1)                  # accepts the connection, never answers
    try:
        started = time.monotonic()
        assert workspace._get(silent.getsockname()[1], "/login") is None
        assert time.monotonic() - started < workspace.SUMMARY_TIMEOUT_S + 1.5
    finally:
        silent.close()


def test_unreadable_registry_keeps_the_self_tile(project):
    path = Path(os.environ["ALPACA_WORKSPACES"])
    path.parent.mkdir(parents=True)
    path.write_text("{not json")
    assert [w["href"] for w in serve.workspaces(project)] == ["/hub/"]
    with pytest.raises(ValueError):
        workspace.load()


def test_t089_done_bar_scratch_instance_gets_local_url_and_ingress_entry(tmp_path, host, monkeypatch, capsys):
    """The t-089 done bar: a scratch instance gets a local URL that serves and a rendered ingress
    entry, and the live cloudflared config is never opened for writing."""
    live = host / ".cloudflared" / "config.yml"
    before = (live.read_bytes(), live.stat().st_mtime_ns)
    written = []
    real_open, real_os_open = builtins.open, os.open

    def guard_open(file, mode="r", *a, **k):
        if any(c in mode for c in "wax+") and os.path.realpath(str(file)).startswith(str(host / ".cloudflared")):
            written.append(str(file))
        return real_open(file, mode, *a, **k)

    def guard_os_open(path, flags, *a, **k):
        if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT) and os.path.realpath(str(path)).startswith(str(host / ".cloudflared")):
            written.append(str(path))
        return real_os_open(path, flags, *a, **k)

    monkeypatch.setattr(builtins, "open", guard_open)
    monkeypatch.setattr(os, "open", guard_os_open)
    scratch = make_instance(tmp_path, "scratch")      # with its web login
    (Path(scratch) / "style").mkdir()
    (Path(scratch) / "style" / "banned.txt").write_text("")
    monkeypatch.chdir(scratch)
    port = free_port()
    assert cli.main(["workspace", "add", "--name", "Scratch", "--port", str(port),
                     "--hostname", "scratch.example.com"]) == cli.PASS
    added = json.loads(capsys.readouterr().out)
    assert added["local"] == "http://127.0.0.1:%d/" % port
    assert cli.main(["workspace", "render-ingress", "--tunnel", "main"]) == cli.PASS
    printed = capsys.readouterr().out
    rendered = yaml.safe_load(Path(scratch, ".alpaca/services/cloudflared-main.yml").read_text())
    assert rendered["ingress"] == [{"hostname": "example.com", "service": "http://127.0.0.1:7328"},
                                   {"hostname": "scratch.example.com", "service": "http://127.0.0.1:%d" % port},
                                   {"service": "http_status:404"}]
    assert "cloudflared tunnel route dns main scratch.example.com" in printed
    assert cli.main(["collect", "services", "--tunnel", "main"]) == cli.PASS
    services = json.loads(capsys.readouterr().out)
    assert services["port"] == port and services["crontab"].startswith("@reboot ")
    # the local URL serves this instance
    live_state = serve.Live(scratch)
    live_state.refresh()
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port), serve.make_handler(live_state, scratch))
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        auth = {"Authorization": "Basic " + base64.b64encode(b":scratch-code").decode()}
        with urllib.request.urlopen(urllib.request.Request(added["local"] + "health.json", headers=auth),
                                    timeout=15) as response:
            assert response.status == 200
        with urllib.request.urlopen(urllib.request.Request(added["local"] + "workspaces.json", headers=auth),
                                    timeout=15) as response:
            tiles = json.loads(response.read())["workspaces"]
        assert tiles[0]["public"] == "https://scratch.example.com/hub/"
    finally:
        httpd.shutdown()
        httpd.server_close()
    assert written == []
    assert (live.read_bytes(), live.stat().st_mtime_ns) == before
