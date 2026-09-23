"""`alpaca workspace move`: re-register a workspace whose folder was moved.

Found while renaming a workspace folder: after `mv old new`, `workspace remove` of the old entry
then `workspace add --port P --hostname H` from the new folder was refused ("port P is served by
a live ingress rule"), because the live rule that routes H to P belonged to the removed entry and
now looked foreign. `workspace move --from <old root>` keeps the port and hostname and adopts that
rule as its own. Every test runs against a tmp registry and a fake ~/.cloudflared.
"""
import json
import os
import shutil
import socket

import pytest

from alpaca import cli, workspace

LIVE = """tunnel: 0a1b2c3d-0000-4000-8000-000000000000
credentials-file: {home}/.cloudflared/0a1b2c3d-0000-4000-8000-000000000000.json
ingress:
  - hostname: example.com
    service: http://127.0.0.1:7328
{extra}  - service: http_status:404
"""
REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture(autouse=True)
def host(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".cloudflared").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("ALPACA_CLOUDFLARED_CONFIG", str(home / ".cloudflared" / "config.yml"))
    monkeypatch.setenv("ALPACA_WORKSPACES", str(tmp_path / "registry" / "workspaces.json"))
    live(home)
    return home


def live(home, rules=()):
    extra = "".join("  - hostname: %s\n    service: http://127.0.0.1:%d\n" % r for r in rules)
    (home / ".cloudflared" / "config.yml").write_text(LIVE.format(home=home, extra=extra))


def make_instance(tmp_path, name):
    root = tmp_path / name
    (root / ".alpaca").mkdir(parents=True)
    shutil.copy(os.path.join(REPO, "ALPACA-MANIFEST"), root / "ALPACA-MANIFEST")
    (root / ".alpaca" / "files-auth").write_text(":" + name + "-code")
    return str(root)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def registered_and_applied(tmp_path, host, name="old-root", hostname="moved.example.com"):
    """A registered instance whose hostname the owner has applied to the live config."""
    old = make_instance(tmp_path, name)
    port = free_port()
    item = workspace.add(old, name="Moved", port=port, hostname=hostname)["workspace"]
    live(host, [(hostname, port)])
    return old, item


def test_add_after_a_folder_move_is_refused_the_way_the_rename_found(tmp_path, host):
    old, item = registered_and_applied(tmp_path, host)
    new = str(tmp_path / "new-root")
    os.rename(old, new)
    # with the old entry still there, add names it, and says how to move it
    with pytest.raises(ValueError, match="registered to Moved") as refused:
        workspace.add(new, port=item["port"], hostname=item["hostname"])
    assert "alpaca workspace move --from %s" % item["root"] in str(refused.value)
    # remove then add: the old entry's live rule now looks foreign (the refusal the rename hit)
    workspace.remove(new, item["id"])
    with pytest.raises(ValueError, match="served by a live ingress rule"):
        workspace.add(new, port=item["port"], hostname=item["hostname"])


def test_move_keeps_the_port_and_hostname_and_adopts_its_own_live_rule(tmp_path, host):
    old, item = registered_and_applied(tmp_path, host)
    other = make_instance(tmp_path, "other")
    held = workspace.add(other, port=free_port())["workspace"]
    new = str(tmp_path / "new-root")
    os.rename(old, new)
    result = workspace.move(new, from_root=old)
    moved = result["workspace"]
    assert moved == {"id": workspace.instance_id(new), "name": "Moved", "root": os.path.realpath(new),
                     "port": item["port"], "hostname": "moved.example.com",
                     "href": "https://moved.example.com/hub/"}
    assert result["moved_from"] == {"id": item["id"], "root": item["root"]}
    assert result["adopted_rule"] is True
    entries = {e["id"]: e for e in workspace.load()}
    assert set(entries) == {moved["id"], held["id"]}          # the old id is gone, the other kept
    assert entries[held["id"]] == held
    assert workspace.entry(new)["port"] == item["port"]
    # the live rule is this workspace's own again: nothing new to route, no collision
    rendered = workspace.render("main")
    assert rendered["new_hosts"] == [] and "moved.example.com" in rendered["registered"]
    # and a plain `add` from the new folder now just re-validates it
    again = workspace.add(new, port=item["port"], hostname=item["hostname"])
    assert again["added"] is False and again["workspace"] == moved


def test_move_through_the_cli_by_id(tmp_path, host, monkeypatch, capsys):
    old, item = registered_and_applied(tmp_path, host)
    new = str(tmp_path / "new-root")
    os.rename(old, new)
    monkeypatch.chdir(new)
    monkeypatch.delenv("ALPACA_ROOT", raising=False)
    assert cli.main(["workspace", "move", "--id", item["id"]]) == cli.PASS
    out = json.loads(capsys.readouterr().out)
    assert out["workspace"]["root"] == os.path.realpath(new) and out["workspace"]["port"] == item["port"]
    assert out["adopted_rule"] is True


def test_move_without_a_live_rule_keeps_the_hostname_and_asks_for_a_render(tmp_path, host):
    old = make_instance(tmp_path, "old-root")
    item = workspace.add(old, port=free_port(), hostname="later.example.com")["workspace"]
    new = str(tmp_path / "new-root")
    os.rename(old, new)
    result = workspace.move(new, from_root=old)
    assert result["adopted_rule"] is False
    assert result["workspace"]["hostname"] == "later.example.com"
    assert result["workspace"]["port"] == item["port"]
    assert any("render-ingress" in step for step in result["next"])


def test_move_refusals(tmp_path, host):
    old, item = registered_and_applied(tmp_path, host)
    new = str(tmp_path / "new-root")
    shutil.copytree(old, new)
    # the old folder still exists: this is a copy, not a move
    with pytest.raises(ValueError, match="still exists"):
        workspace.move(new, from_root=old)
    shutil.rmtree(old)
    # neither --from nor --id
    with pytest.raises(ValueError, match="--from"):
        workspace.move(new)
    # no entry for that root or id
    with pytest.raises(ValueError, match="no registry entry"):
        workspace.move(new, from_root=str(tmp_path / "never-registered"))
    with pytest.raises(ValueError, match="no registry entry"):
        workspace.move(new, ident="0" * 12)
    # a live rule routes the hostname somewhere else: never adopted, never overridden
    live(host, [("moved.example.com", item["port"] + 1)])
    with pytest.raises(ValueError, match="already routed"):
        workspace.move(new, from_root=old)
    # a live rule for another hostname serves the port: refused
    live(host, [("moved.example.com", item["port"]), ("elsewhere.example.com", item["port"])])
    with pytest.raises(ValueError, match="served by a live ingress rule"):
        workspace.move(new, from_root=old)
    live(host, [("moved.example.com", item["port"])])
    # the new folder already has its own entry
    workspace.add(new, port=free_port())
    with pytest.raises(ValueError, match="already has a registry entry"):
        workspace.move(new, from_root=old)
    workspace.remove(new)
    # nothing above changed the registry: the old entry is still the only one
    assert [e["id"] for e in workspace.load()] == [item["id"]]
    assert workspace.move(new, from_root=old)["adopted_rule"] is True
