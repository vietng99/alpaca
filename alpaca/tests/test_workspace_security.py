"""t-089 security review: one test per finding (H1-H3, M1-M5, L1-L3, L5).

Every test runs with a fake HOME holding a fake ~/.cloudflared and a tmp registry
($ALPACA_WORKSPACES); no test binds a port other than an ephemeral one, and no test touches 7328.
"""
import http.server
import json
import os
import shutil
import socket
import stat
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from alpaca import cli, serve, workspace
from alpaca.observability import maintenance

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LIVE = """tunnel: 0a1b2c3d-0000-4000-8000-000000000000
credentials-file: /creds/0a1b2c3d.json
protocol: http2
ingress:
  - hostname: example.com
    service: http://127.0.0.1:7328
  - service: http_status:404
"""


@pytest.fixture(autouse=True)
def host(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".cloudflared").mkdir(parents=True)
    (home / ".cloudflared" / "config.yml").write_text(LIVE)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("ALPACA_CLOUDFLARED_CONFIG", str(home / ".cloudflared" / "config.yml"))
    monkeypatch.setenv("ALPACA_WORKSPACES", str(tmp_path / "registry" / "workspaces.json"))
    return home


def instance(tmp_path, name, login=True):
    root = tmp_path / name
    (root / ".alpaca").mkdir(parents=True)
    shutil.copy(os.path.join(REPO, "ALPACA-MANIFEST"), root / "ALPACA-MANIFEST")
    if login:
        (root / ".alpaca" / "files-auth").write_text(":" + name + "-code")
    return str(root)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def write_registry(entries):
    path = Path(os.environ["ALPACA_WORKSPACES"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 1, "workspaces": entries}))


def entry_for(root, port, hostname=None, name="X"):
    return {"id": workspace.instance_id(root), "name": name, "root": os.path.realpath(root), "port": port,
            "hostname": hostname, "href": "http://127.0.0.1:%d/hub/" % port}


# ---- H1: registry text never becomes config text ----------------------------------------------

EVIL = "Alpha\n  - hostname: evil.example.com\n    service: http://127.0.0.1:22\n  #"


def test_h1_a_name_with_a_newline_is_refused_on_add(tmp_path):
    root = instance(tmp_path, "a")
    for bad in (EVIL, "tab\there", "x" * 65, "", "bell\x07"):
        with pytest.raises(ValueError, match="name"):
            workspace.add(root, name=bad, hostname="a.example.org")
    assert workspace.load() == []


def test_h1_a_hand_edited_registry_is_validated_on_load_and_render_emits_only_safe_yaml(tmp_path, capsys):
    a = instance(tmp_path, "a")
    write_registry([entry_for(a, 7391, "a.example.org", name=EVIL)])
    # a malformed entry is skipped with a warning (R1) and never reaches the rendered config
    assert workspace.load() == [] and "malformed" in capsys.readouterr().err
    with pytest.raises(ValueError, match="malformed"):
        workspace.entry(a)                     # the entry is needed here, so it refuses
    text = workspace.render("main")["text"]
    assert "evil" not in text and "Alpha" not in text
    for bad in ({"id": "zz"}, {"hostname": "evil.example.com\n  - x"}, {"port": "7391"}, {"port": 0},
                {"root": "relative"}):
        item = dict(entry_for(a, 7391, "a.example.org"), **bad)
        write_registry([item])
        assert workspace.load() == [] and len(workspace.problems()) == 1
    # a valid registry renders through yaml.safe_dump: every rule is data, no registry text in a comment
    write_registry([entry_for(a, 7391, "a.example.org", name="Alpha # not a comment")])
    text = workspace.render("main")["text"]
    assert "Alpha" not in text
    rules = yaml.safe_load(text)["ingress"]
    assert [r.get("hostname") for r in rules] == ["example.com", "a.example.org", None]


# ---- H2: live rules are never replaced; an unreadable live config refuses ---------------------

def test_h2_a_registered_hostname_never_replaces_a_live_rule(tmp_path):
    a = instance(tmp_path, "a")
    # a registry entry (written by hand) claims the live hostname for another port
    write_registry([entry_for(a, 7391, "example.com")])
    with pytest.raises(ValueError, match="collides"):
        workspace.render("main")
    # add refuses any hostname a live rule routes, even to the port it would get
    write_registry([])
    with pytest.raises(ValueError, match="already routed"):
        workspace.add(a, port=free_port(), hostname="example.com")


def test_h2_add_refuses_when_the_live_config_cannot_be_read(tmp_path, host):
    a = instance(tmp_path, "a")
    live = host / ".cloudflared" / "config.yml"
    live.write_text("ingress: [unclosed\n")
    with pytest.raises(ValueError, match="does not parse"):
        workspace.add(a, hostname="a.example.org")
    live.unlink()
    with pytest.raises(ValueError, match="no-live-config"):
        workspace.add(a, hostname="a.example.org")
    assert workspace.load() == []
    added = workspace.add(a, hostname="a.example.org", no_live_config=True)["workspace"]
    assert added["hostname"] == "a.example.org"
    with pytest.raises(ValueError, match="does not exist"):
        workspace.render("main", tunnel_id="t", credentials="/c.json")


def test_h2_an_applied_rule_is_kept_once_and_the_instance_can_be_re_added(tmp_path, host):
    a = instance(tmp_path, "a")
    port = free_port()
    workspace.add(a, port=port, hostname="a.example.org")
    rendered = workspace.render("main")["text"]
    (host / ".cloudflared" / "config.yml").write_text(rendered)        # the owner applied it
    again = workspace.add(a)["workspace"]
    assert again["port"] == port and again["hostname"] == "a.example.org"
    rules = yaml.safe_load(workspace.render("main")["text"])["ingress"]
    assert [r.get("hostname") for r in rules] == ["example.com", "a.example.org", None]
    assert workspace.render("main")["new_hosts"] == []


# ---- H3: the hub reads only a minimal status, only with the other instance's own login --------

class Board(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        self.server.seen.append((self.path, self.headers.get("Authorization")))
        if self.server.redirect_to:
            self.send_response(302)
            self.send_header("Location", self.server.redirect_to)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = json.dumps({"secret": "transcript text", "rows": ["private"],
                           "pulse": {"ops_open": 1, "tasks": {"open": 2, "doing": 1, "done": 5},
                                     "now": {"ops": [{"id": "op-1", "title": "Build", "status": "open",
                                                      "tasks_total": 3, "tasks_done": 1, "notes": "private"}],
                                             "next_action": "t-9", "sessions": {"work": [{"active": True}]}}}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def board(redirect_to=None):
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Board)
    httpd.seen, httpd.redirect_to = [], redirect_to
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def status_of(root, ident):
    code, body = serve.workspace_summary(root, ident)
    assert code == 200
    return json.loads(body)


def test_h3_n1_the_tile_is_reachability_only_and_no_stored_credential_is_sent(project, tmp_path):
    """N1: the hub never reads another instance's files-auth or sends any credential (a process
    squatting on the port would harvest it); it GETs the public /login page and returns
    {"reachable"} only, so nothing of the other record leaves it (H3)."""
    b_server, c_server = board(), board()
    try:
        b = instance(tmp_path, "b")                    # has its own sign-in
        c = instance(tmp_path, "c", login=False)       # loopback-only
        workspace.add(b, port=b_server.server_address[1])
        workspace.add(c, port=c_server.server_address[1])
        assert status_of(project, workspace.instance_id(b)) == {"reachable": True}
        assert status_of(project, workspace.instance_id(c)) == {"reachable": True}
        assert b_server.seen == [("/login", None)] and c_server.seen == [("/login", None)]
        down = instance(tmp_path, "d")
        workspace.add(down, port=free_port())
        assert status_of(project, workspace.instance_id(down)) == {"reachable": False}
    finally:
        for s in (b_server, c_server):
            s.shutdown()
            s.server_close()


def test_h3_a_redirect_is_never_followed(project, tmp_path):
    target = board()
    hop = board(redirect_to="http://127.0.0.1:%d/login" % target.server_address[1])
    try:
        b = instance(tmp_path, "b")
        workspace.add(b, port=hop.server_address[1])
        assert status_of(project, workspace.instance_id(b)) == {"reachable": False}
        assert target.seen == [] and hop.seen == [("/login", None)]
    finally:
        for s in (target, hop):
            s.shutdown()
            s.server_close()


def test_n1_a_registry_entry_whose_id_is_not_its_roots_is_refused(tmp_path):
    a, b = instance(tmp_path, "a"), instance(tmp_path, "b")
    item = entry_for(a, 7391)
    item["id"] = workspace.instance_id(b)
    write_registry([item])
    assert workspace.load() == []
    assert "not the id of root" in workspace.problems()[0]["reason"]
    for root in (a, b):                        # needed by either instance: refused
        with pytest.raises(ValueError, match="malformed"):
            workspace.entry(root)


# ---- M1: a public instance never starts without its sign-in ------------------------------------

def test_m1_add_hostname_needs_a_web_login(tmp_path):
    a = instance(tmp_path, "a", login=False)
    with pytest.raises(ValueError, match="web login"):
        workspace.add(a, hostname="a.example.org")
    assert workspace.add(a)["workspace"]["hostname"] is None


def test_m1_serve_forces_remote_for_a_public_instance(project, monkeypatch, capsys):
    write_registry([entry_for(project, free_port(), "pub.example.org")])
    calls = []
    monkeypatch.setattr(serve.cli, "_root", lambda: project)
    monkeypatch.setattr(serve, "_run", lambda root, port, keep=False, remote=False: calls.append(remote))
    args = SimpleNamespace(stop=False, status=False, write_services=False, remote=False, detach=False,
                           keep=True, port=None)
    assert serve.cmd_serve(args) == cli.FAIL and calls == []
    assert "files-auth" in capsys.readouterr().out
    Path(project, ".alpaca", "files-auth").write_text(":pin")
    args.remote = False
    assert serve.cmd_serve(args) == cli.PASS and calls == [True]


def test_m1_autostart_passes_remote_and_refuses_without_a_login(project, monkeypatch, capsys):
    write_registry([entry_for(project, free_port(), "pub.example.org")])
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("ALPACA_NO_AUTOSERVE", raising=False)
    spawned = []
    monkeypatch.setattr(serve.subprocess, "Popen", lambda cmd, **k: spawned.append(cmd))
    monkeypatch.setattr(serve, "is_running", lambda root: None)
    assert serve.ensure_running(project, wait_s=0.1) is None and spawned == []
    assert "files-auth" in capsys.readouterr().err
    Path(project, ".alpaca", "files-auth").write_text(":pin")
    serve.ensure_running(project, wait_s=0.1)
    assert spawned and "--remote" in spawned[0]


def test_m1_web_up_script_requires_the_login_and_passes_remote(tmp_path):
    root = instance(tmp_path, "a", login=False)
    port = free_port()
    write_registry([entry_for(root, port, "pub.example.org")])
    result = maintenance.write_services(root)
    script = next(p for p in result["scripts"] if p.endswith("-web-up.sh"))
    body = Path(script).read_text()
    assert "--remote --port %d" % port in body
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "systemctl").write_text("#!/bin/bash\nexit 1\n")
    Path(root, "bin").mkdir()
    Path(root, "bin", "alpaca-python").write_text('#!/bin/bash\necho "$*" >> "$CALLS"\n')
    for f in (bindir / "systemctl", Path(root, "bin", "alpaca-python")):
        f.chmod(0o700)
    calls = tmp_path / "calls"
    env = dict(os.environ, PATH=str(bindir) + ":/usr/bin:/bin", CALLS=str(calls))
    done = subprocess.run(["bash", script], env=env, capture_output=True, text=True, timeout=15)
    assert done.returncode == 1 and "files-auth" in done.stderr and not calls.exists()
    Path(root, ".alpaca", "files-auth").write_text(":pin")
    done = subprocess.run(["bash", script], env=env, capture_output=True, text=True, timeout=15)
    assert done.returncode == 0 and calls.read_text().split() == [
        "-m", "alpaca", "serve", "--detach", "--keep", "--remote", "--port", str(port)]


# ---- M2: one unapplied hostname never restarts the shared tunnel -------------------------------

def fake_bin(tmp_path, codes):
    folder = tmp_path / "wdbin"
    folder.mkdir()
    lines = ['#!/bin/bash', 'for url; do :; done', 'case "$url" in']
    lines += ['  %s) printf %%s %s;;' % (url, code) for url, code in codes.items()]
    lines += ['  *) printf 000;;', 'esac']
    (folder / "curl").write_text("\n".join(lines) + "\n")
    (folder / "systemctl").write_text('#!/bin/bash\necho "$*" >> "$CALLS"\n')
    (folder / "date").write_text('#!/bin/bash\necho "$NOW"\n')
    for item in folder.iterdir():
        item.chmod(0o700)
    return folder


def write_config(path, hosts):
    path.write_text("tunnel: t\ningress:\n" + "".join("  - hostname: %s\n    service: http://127.0.0.1:1\n" % h
                                                     for h in hosts) + "  - service: http_status:404\n")


def run_watchdog(tmp_path, pairs, codes, ticks, routed=None):
    """Run the generated watchdog once per tick. The live config it reads at run time routes the
    pairs flagged True (or `routed`)."""
    state, calls, config = tmp_path / "wdstate", tmp_path / "wdcalls", tmp_path / "live.yml"
    if routed is None:
        routed = [p[1][len("https://"):-1] for p in pairs if p[2]]
    write_config(config, routed)
    script = tmp_path / "wd.sh"
    script.write_text(maintenance.watchdog_script("alpaca-tunnel-x.service", pairs, str(state)))
    bindir = fake_bin(tmp_path, codes) if not (tmp_path / "wdbin").exists() else tmp_path / "wdbin"
    for now in ticks:
        env = dict(os.environ, PATH=str(bindir) + ":/usr/bin:/bin", NOW=str(now), CALLS=str(calls))
        subprocess.run(["bash", str(script), str(config)], env=env, capture_output=True, text=True,
                       timeout=15, check=True)
    return calls.read_text().count("restart") if calls.exists() else 0


def test_m2_an_unapplied_hostname_answering_404_is_not_a_miss(tmp_path):
    pairs = [("http://127.0.0.1:7391/", "https://new.example.org/", False),
             ("http://127.0.0.1:7328/", "https://example.com/", True)]
    codes = {"http://127.0.0.1:7328/": 200, "http://127.0.0.1:7391/": 200,
             "https://example.com/": 200, "https://new.example.org/": 404}
    assert run_watchdog(tmp_path, pairs, codes, [1000, 1200, 1400, 1600]) == 0


def test_m2_restart_only_when_every_checked_public_url_fails(tmp_path):
    pairs = [("http://127.0.0.1:7328/", "https://example.com/", True),
             ("http://127.0.0.1:7391/", "https://a.example.org/", True)]
    one_down = {"http://127.0.0.1:7328/": 200, "http://127.0.0.1:7391/": 200,
                "https://example.com/": 530, "https://a.example.org/": 200}
    assert run_watchdog(tmp_path, pairs, one_down, [1000, 1200, 1400]) == 0
    all_down = dict(one_down, **{"https://a.example.org/": 530})
    other = tmp_path / "all"
    other.mkdir()
    assert run_watchdog(other, pairs, all_down, [1000, 1200]) == 1
    only_unapplied = tmp_path / "unapplied"
    only_unapplied.mkdir()
    assert run_watchdog(only_unapplied, [("http://127.0.0.1:7391/", "https://new.example.org/", False)],
                        {"http://127.0.0.1:7391/": 200, "https://new.example.org/": 404}, [1000, 1200, 1400]) == 0


def test_m2_the_watchdog_checks_the_live_config_hostnames(tmp_path):
    root = instance(tmp_path, "a")
    workspace.add(root, port=7391, hostname="new.example.org")
    result = maintenance.write_services(root, tunnel="main", watchdog_state=str(tmp_path / "st"))
    assert result["watchdog"]["pairs"] == [["http://127.0.0.1:7328/", "https://example.com/", True],
                                           ["http://127.0.0.1:7391/", "https://new.example.org/", False]]


# ---- M3: the docs stop the old watchdog before switching tunnel units ------------------------

def test_m3_docs_stop_the_old_watchdog_before_switching_units():
    text = Path(REPO, "docs", "workspaces.md").read_text()
    step = text[text.index("7. **owner** tunnel"):text.index("8. **owner** units")]
    assert "disable --now old-tunnel-watchdog.timer" in step
    assert "stop old-tunnel-watchdog.service" in step
    assert "two" in step.lower() and "connectors" in step
    assert step.index("old-tunnel-watchdog.timer") < step.index("alpaca-tunnel-<tunnel>.service")


# ---- M4: a corrupt registry is an error, and nothing is written first --------------------------

def test_m4_a_corrupt_registry_writes_no_unit(project, capsys):
    path = Path(os.environ["ALPACA_WORKSPACES"])
    path.parent.mkdir(parents=True)
    path.write_text("{not json")
    with pytest.raises(ValueError):
        workspace.instance_port(project)
    assert cli.main(["collect", "services"]) == cli.USAGE
    assert cli.main(["serve", "--write-services"]) == cli.USAGE
    assert "unreadable" in capsys.readouterr().out
    assert not Path(project, ".alpaca", "services").exists()


def test_m4_collect_services_validates_everything_before_writing(project, host):
    (host / ".cloudflared" / "config.yml").write_text("ingress: [unclosed\n")
    with pytest.raises(ValueError, match="does not parse"):
        maintenance.write_services(project, tunnel="main")
    assert not Path(project, ".alpaca", "services").exists()


# ---- M5: every live rule is carried, in order, with the live catch-all last ---------------------

def test_m5_path_rules_and_the_live_catch_all_are_kept(tmp_path, host):
    (host / ".cloudflared" / "config.yml").write_text(
        "tunnel: t\ncredentials-file: /c.json\ningress:\n"
        "  - path: ^/status$\n    service: http://127.0.0.1:9101\n"
        "  - hostname: example.com\n    service: http://127.0.0.1:7328\n"
        "    originRequest:\n      noTLSVerify: true\n"
        "  - service: http_status:503\n")
    a = instance(tmp_path, "a")
    workspace.add(a, port=7391, hostname="a.example.org")
    rules = yaml.safe_load(workspace.render("main")["text"])["ingress"]
    assert rules == [{"path": "^/status$", "service": "http://127.0.0.1:9101"},
                     {"hostname": "example.com", "service": "http://127.0.0.1:7328",
                      "originRequest": {"noTLSVerify": True}},
                     {"hostname": "a.example.org", "service": "http://127.0.0.1:7391"},
                     {"service": "http_status:503"}]


# ---- L1: a bind scan never lands on 7328 --------------------------------------------------------

def test_l1_bind_scan_skips_the_reserved_port(monkeypatch):
    tried = []

    class Fake:
        def __init__(self, address, handler):
            tried.append(address[1])
    monkeypatch.setattr(serve, "_Server", Fake)
    monkeypatch.delenv("ALPACA_SERVE_STRICT_PORT", raising=False)
    monkeypatch.delenv(serve.LISTEN_FD_ENV, raising=False)
    _httpd, port = serve._bind(7328)
    assert port == 7329 and 7328 not in tried
    monkeypatch.setenv("ALPACA_SERVE_STRICT_PORT", "1")
    tried.clear()
    _httpd, port = serve._bind(7328)                    # strict and asked for: a dashboard unit's own case
    assert port == 7328 and tried == [7328]


# ---- L2: no $ in a unit path --------------------------------------------------------------------

def test_l2_a_dollar_in_a_unit_path_is_refused(project):
    for kwargs in ({"cloudflared": "/opt/cf$HOME/cloudflared"}, {"tunnel_config": "/opt/${X}/c.yml"},
                   {"watchdog_state": "/var/$USER/state"}):
        with pytest.raises(ValueError, match=r"\$"):
            maintenance.write_services(project, tunnel="main", **kwargs)
    assert not Path(project, ".alpaca", "services").exists()


# ---- L3: units and scripts are created with their final mode ------------------------------------

def test_l3_units_and_scripts_never_exist_with_a_wider_mode(tmp_path, monkeypatch):
    root = instance(tmp_path, "a")
    workspace.add(root, port=7391, hostname="a.example.org")
    seen = []
    real_replace, real_chmod = os.replace, os.chmod

    def replace(src, dst, *a, **k):
        if str(dst).endswith((".service", ".timer", ".sh", ".yml", ".json")):
            seen.append((os.path.basename(str(dst)), stat.S_IMODE(os.stat(src).st_mode)))
        return real_replace(src, dst, *a, **k)

    def chmod(path, mode, *a, **k):
        if str(path).endswith((".service", ".timer", ".sh")):
            raise AssertionError("chmod after write: %s" % path)
        return real_chmod(path, mode, *a, **k)
    monkeypatch.setattr(os, "replace", replace)
    monkeypatch.setattr(os, "chmod", chmod)
    result = maintenance.write_services(root, tunnel="main")
    serve.write_services(root)
    workspace.render_ingress(root, "main")
    modes = dict(seen)
    for name in result["units"]:
        assert modes[name] == 0o600, name
    for path in result["scripts"]:
        assert modes[os.path.basename(path)] == 0o700, path
    assert modes["cloudflared-main.yml"] == 0o600


# ---- L5: a skipped run row is named on stderr ---------------------------------------------------

def test_l5_a_skipped_run_row_is_reported(project, monkeypatch, capsys):
    from alpaca.gates import verdict
    monkeypatch.setenv("ALPACA_RECORD_SKIP_ROOT", project)
    verdict._record_run("demo-gate", 0, "ok", [])
    err = capsys.readouterr().err
    assert "demo-gate" in err and "ALPACA_RECORD_SKIP_ROOT" in err
    assert not Path(project, ".alpaca", "alpaca.db").exists()


# ---- N2: a corrupt registry fails only the paths that need it -----------------------------------

def corrupt_registry(text="{not json"):
    path = Path(os.environ["ALPACA_WORKSPACES"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def serve_args(**kw):
    base = dict(stop=False, status=False, write_services=False, remote=False, detach=False, keep=True, port=None)
    base.update(kw)
    return SimpleNamespace(**base)


def test_n2_an_explicit_port_starts_with_a_warning_on_a_corrupt_registry(project, monkeypatch, capsys):
    corrupt_registry()
    calls = []
    monkeypatch.setattr(serve.cli, "_root", lambda: project)
    monkeypatch.setattr(serve, "_run", lambda root, port, keep=False, remote=False: calls.append((port, remote)))
    port = free_port()
    Path(project, ".alpaca", "files-auth").write_text(":pin")
    # the installed unit's command line: --remote --port N
    assert serve.cmd_serve(serve_args(remote=True, port=port)) == cli.PASS and calls == [(port, True)]
    assert "WARNING" in capsys.readouterr().err
    # without --port the registry names the port, so that start fails
    assert serve.cmd_serve(serve_args()) == cli.FAIL and len(calls) == 1
    # still refused: write-services and collect services
    assert cli.main(["serve", "--write-services"]) == cli.USAGE
    assert cli.main(["collect", "services"]) == cli.USAGE


def test_n2_a_corrupt_registry_that_names_this_root_fails_closed_without_a_login(project, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(serve.cli, "_root", lambda: project)
    monkeypatch.setattr(serve, "_run", lambda root, port, keep=False, remote=False: calls.append(remote))
    port = free_port()
    corrupt_registry('{"workspaces": [{"root": %s, "hostname": "pub.example.org", ' % json.dumps(os.path.realpath(project)))
    assert serve.cmd_serve(serve_args(port=port)) == cli.FAIL and calls == []
    assert "refusing" in capsys.readouterr().out
    corrupt_registry('{"workspaces": [{"root": "/somewhere/else", ')
    assert serve.cmd_serve(serve_args(port=port)) == cli.PASS and calls == [False]
    assert "WARNING" in capsys.readouterr().err


# ---- N3: a hostname is not added while a local-only server runs ---------------------------------

def test_n3_add_hostname_refuses_while_a_server_runs_without_remote(tmp_path, monkeypatch):
    a = instance(tmp_path, "a")
    monkeypatch.setattr(serve, "is_running", lambda root: {"pid": 4242, "port": 7391, "remote": False})
    with pytest.raises(ValueError, match="--remote"):
        workspace.add(a, port=7391, hostname="a.example.org")
    assert workspace.load() == []
    monkeypatch.setattr(serve, "is_running", lambda root: {"pid": 4242, "port": 7391, "remote": True})
    assert workspace.add(a, port=7391, hostname="a.example.org")["workspace"]["hostname"] == "a.example.org"


# ---- N5: wildcard shadowing, run-time routing, ALPACA_FILES_AUTH for web-up -------------------------

def test_n5_render_warns_when_a_live_wildcard_shadows_a_new_hostname(tmp_path, host, capsys, monkeypatch):
    (host / ".cloudflared" / "config.yml").write_text(
        "tunnel: t\ncredentials-file: /c.json\ningress:\n"
        "  - hostname: '*.example.org'\n    service: http://127.0.0.1:9000\n"
        "  - service: http_status:404\n")
    a = instance(tmp_path, "a")
    workspace.add(a, port=7391, hostname="a.example.org")
    assert any("*.example.org" in w and "a.example.org" in w for w in workspace.render("t")["warnings"])
    monkeypatch.chdir(a)
    assert cli.main(["workspace", "render-ingress", "--tunnel", "t"]) == cli.PASS
    assert "WARNING" in capsys.readouterr().out


def test_n5_the_watchdog_reads_the_routed_hostnames_at_run_time(tmp_path):
    """A hostname the config does not route yet answering 404 is skipped; once the owner applies a
    config that routes it, the same script counts its 404 as a miss, with no regeneration."""
    pairs = [("http://127.0.0.1:7391/", "https://new.example.org/", False)]
    codes = {"http://127.0.0.1:7391/": 200, "https://new.example.org/": 404}
    assert run_watchdog(tmp_path, pairs, codes, [1000, 1200, 1400]) == 0
    applied = tmp_path / "applied"
    applied.mkdir()
    assert run_watchdog(applied, pairs, codes, [1000, 1200], routed=["new.example.org"]) == 1
    wildcard = tmp_path / "wild"
    wildcard.mkdir()
    assert run_watchdog(wildcard, pairs, codes, [1000, 1200], routed=["'*.example.org'"]) == 1
    script = tmp_path / "wd.sh"
    done = subprocess.run(["bash", str(script), "relative.yml"], capture_output=True, text=True, timeout=15)
    assert done.returncode == 2 and "usage" in done.stderr
    text = script.read_text()
    assert "new.example.org" in text and "example.com" not in text and "live.yml" not in text


def test_n5_the_watchdog_unit_passes_the_config_path(tmp_path):
    root = instance(tmp_path, "a")
    workspace.add(root, port=7391, hostname="new.example.org")
    result = maintenance.write_services(root, tunnel="main", tunnel_config="/opt/cf/combined.yml")
    unit = Path(result["directory"], "alpaca-tunnel-main-watchdog.service").read_text()
    command = next(line.split("=", 1)[1] for line in unit.splitlines() if line.startswith("ExecStart="))
    import shlex
    assert shlex.split(command) == [result["watchdog"]["install_as"], "/opt/cf/combined.yml"]


def test_n5_web_up_accepts_alpaca_files_auth_as_the_login(tmp_path):
    root = instance(tmp_path, "a", login=False)
    port = free_port()
    write_registry([entry_for(root, port, "pub.example.org")])
    script = next(p for p in maintenance.write_services(root)["scripts"] if p.endswith("-web-up.sh"))
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "systemctl").write_text("#!/bin/bash\nexit 1\n")
    Path(root, "bin").mkdir()
    Path(root, "bin", "alpaca-python").write_text('#!/bin/bash\necho "$ALPACA_FILES_AUTH $*" >> "$CALLS"\n')
    for f in (bindir / "systemctl", Path(root, "bin", "alpaca-python")):
        f.chmod(0o700)
    calls = tmp_path / "calls"
    env = dict(os.environ, PATH=str(bindir) + ":/usr/bin:/bin", CALLS=str(calls))
    env.pop("ALPACA_FILES_AUTH", None)
    done = subprocess.run(["bash", script], env=dict(env, ALPACA_FILES_AUTH=":from-env"), capture_output=True,
                          text=True, timeout=15)
    assert done.returncode == 0 and calls.read_text().split()[:2] == [":from-env", "-m"]
    calls.unlink()
    Path(root, ".alpaca", "service.env").write_text("OTHER=1\nALPACA_FILES_AUTH=':from-unit-env'\n")
    done = subprocess.run(["bash", script], env=env, capture_output=True, text=True, timeout=15)
    assert done.returncode == 0 and calls.read_text().split()[0] == ":from-unit-env"
    assert "--remote" in calls.read_text()


# ---- R1: one bad or moved entry never makes the registry unreadable ----------------------------

def test_r1_a_re_symlinked_root_keeps_every_entry_readable(tmp_path, capsys):
    parent = tmp_path / "parent"
    parent.mkdir()
    a = instance(parent, "a")
    b = instance(tmp_path, "b")
    workspace.add(a, port=free_port())
    workspace.add(b, port=free_port())
    stored = os.path.realpath(a)
    (tmp_path / "parent").rename(tmp_path / "moved")
    (tmp_path / "parent").symlink_to(tmp_path / "moved")     # the stored root now resolves elsewhere
    assert os.path.realpath(stored) != stored
    entries = workspace.load()
    assert sorted(e["root"] for e in entries) == sorted([stored, os.path.realpath(b)])
    assert workspace.problems() == []
    assert workspace.instance_port(b) == workspace.entry(b)["port"]


def test_r1_a_bad_entry_is_skipped_for_the_others_and_refused_only_where_needed(tmp_path, capsys):
    good, gone = instance(tmp_path, "good"), instance(tmp_path, "gone")
    good_port, bad_port = free_port(), free_port()
    bad = dict(entry_for(gone, bad_port, "bad.example.org"), name="two\nlines")
    write_registry([entry_for(good, good_port, "good.example.org"), bad])
    assert [e["root"] for e in workspace.load()] == [os.path.realpath(good)]
    assert "skipping registry entry 1" in capsys.readouterr().err
    assert workspace.instance_port(good) == good_port                      # other readers work
    assert [r.get("hostname") for r in yaml.safe_load(workspace.render("main")["text"])["ingress"]] == [
        "example.com", "good.example.org", None]
    with pytest.raises(ValueError, match="malformed"):
        workspace.instance_port(gone)                                       # its own entry: needed
    other = instance(tmp_path, "other")
    with pytest.raises(ValueError, match="malformed registry entry"):
        workspace.add(other, port=bad_port)                                 # its claims still hold
    added = workspace.add(other, port=free_port())["workspace"]
    raw = json.loads(Path(os.environ["ALPACA_WORKSPACES"]).read_text())["workspaces"]
    assert bad in raw and len(raw) == 3 and added in [dict(r, href=r["href"]) for r in raw]
    repaired = workspace.add(gone, name="Gone", port=bad_port)["workspace"]  # add repairs its own
    assert repaired["name"] == "Gone" and workspace.problems() == []


def test_r1_remove_by_id_drops_a_malformed_entry_and_list_flags_stale_roots(tmp_path, monkeypatch, capsys):
    good = instance(tmp_path, "good")
    bad = dict(entry_for(good, 7391), id="abcdefabcdef", root="/nowhere/else")
    stale = entry_for(Path("/no/such/root"), free_port())
    stale["root"] = "/no/such/root"
    stale["id"] = workspace._id_of("/no/such/root")
    write_registry([entry_for(good, free_port()), bad, stale])
    monkeypatch.chdir(good)
    assert cli.main(["workspace", "list"]) == cli.PASS
    listed = json.loads(capsys.readouterr().out)
    assert {e["root"]: e["stale"] for e in listed["workspaces"]} == {os.path.realpath(good): False,
                                                                    "/no/such/root": True}
    assert [p["id"] for p in listed["invalid"]] == ["abcdefabcdef"]
    assert cli.main(["workspace", "remove", "--id", "abcdefabcdef"]) == cli.PASS
    assert json.loads(capsys.readouterr().out)["removed"] == "abcdefabcdef"
    raw = json.loads(Path(os.environ["ALPACA_WORKSPACES"]).read_text())["workspaces"]
    assert [r["id"] for r in raw] == [workspace.instance_id(good), stale["id"]]


# ---- R2: an unreadable config falls back to the generation-time routed set ----------------------

def test_r2_the_watchdog_falls_back_to_the_generated_routed_set(tmp_path):
    pairs = [("http://127.0.0.1:7391/", "https://a.example.org/", True)]
    codes = {"http://127.0.0.1:7391/": 200, "https://a.example.org/": 404}
    state, calls = tmp_path / "wdstate", tmp_path / "wdcalls"
    script = tmp_path / "wd.sh"
    script.write_text(maintenance.watchdog_script("alpaca-tunnel-x.service", pairs, str(state)))
    bindir = fake_bin(tmp_path, codes)
    errors = []
    for now in (1000, 1200):
        env = dict(os.environ, PATH=str(bindir) + ":/usr/bin:/bin", NOW=str(now), CALLS=str(calls))
        done = subprocess.run(["bash", str(script), str(tmp_path / "missing.yml")], env=env,
                              capture_output=True, text=True, timeout=15, check=True)
        errors.append(done.stderr)
    assert calls.read_text().count("restart") == 1                  # routed at generation: a miss
    assert all("WARNING" in e and "missing.yml" in e for e in errors)


# ---- R3: the corrupt-registry warning goes to stderr --------------------------------------------

def test_r3_the_corrupt_registry_warning_is_on_stderr(project, monkeypatch, capsys):
    corrupt_registry()
    monkeypatch.setattr(serve.cli, "_root", lambda: project)
    monkeypatch.setattr(serve, "_run", lambda root, port, keep=False, remote=False: None)
    Path(project, ".alpaca", "files-auth").write_text(":pin")
    assert serve.cmd_serve(serve_args(remote=True, port=free_port())) == cli.PASS
    captured = capsys.readouterr()
    assert "WARNING" in captured.err and "WARNING" not in captured.out


# ---- R4: a live rule to this port makes the instance public -------------------------------------

def test_r4_a_live_rule_to_the_port_makes_the_instance_public(project, host, monkeypatch, capsys):
    port = free_port()
    (host / ".cloudflared" / "config.yml").write_text(
        "tunnel: t\ningress:\n  - hostname: pub.example.org\n    service: http://127.0.0.1:%d\n"
        "  - service: http_status:404\n" % port)
    assert workspace.public(project, port) and not workspace.public(project, free_port())
    calls = []
    monkeypatch.setattr(serve.cli, "_root", lambda: project)
    monkeypatch.setattr(serve, "_run", lambda root, port, keep=False, remote=False: calls.append(remote))
    assert serve.cmd_serve(serve_args(port=port)) == cli.FAIL and calls == []   # no login: refused
    assert "public" in capsys.readouterr().out
    Path(project, ".alpaca", "files-auth").write_text(":pin")
    assert serve.cmd_serve(serve_args(port=port)) == cli.PASS and calls == [True]
    # the web-up script of a live-routed instance passes --remote
    script = next(p for p in maintenance.write_services(project, port=port)["scripts"] if p.endswith("-web-up.sh"))
    assert "--remote --port %d" % port in Path(script).read_text()
    # an unparsable config fails closed when its raw text names the port
    (host / ".cloudflared" / "config.yml").write_text("ingress: [http://127.0.0.1:%d\n" % port)
    assert workspace.live_routes_port(port) and not workspace.live_routes_port(free_port())


def test_r4_autostart_of_a_live_routed_port_needs_a_login(project, host, monkeypatch, capsys):
    port = free_port()
    write_registry([entry_for(project, port)])                   # registered, no hostname
    (host / ".cloudflared" / "config.yml").write_text(
        "tunnel: t\ningress:\n  - hostname: pub.example.org\n    service: http://localhost:%d\n"
        "  - service: http_status:404\n" % port)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("ALPACA_NO_AUTOSERVE", raising=False)
    spawned = []
    monkeypatch.setattr(serve.subprocess, "Popen", lambda cmd, **k: spawned.append(cmd))
    monkeypatch.setattr(serve, "is_running", lambda root: None)
    assert serve.ensure_running(project, wait_s=0.1) is None and spawned == []
    Path(project, ".alpaca", "files-auth").write_text(":pin")
    serve.ensure_running(project, wait_s=0.1)
    assert spawned and "--remote" in spawned[0]
