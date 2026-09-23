"""Reserved ports are a host setting ($ALPACA_RESERVED_PORTS) with an empty default."""
from alpaca import serve, workspace


def test_the_default_reserves_no_port(monkeypatch):
    monkeypatch.delenv("ALPACA_RESERVED_PORTS", raising=False)
    assert list(workspace.RESERVED_PORTS) == [] and len(workspace.RESERVED_PORTS) == 0
    # negative: with nothing reserved, no port in the derived range is skipped as reserved
    assert all(p not in workspace.RESERVED_PORTS for p in range(7300, 7390))


def test_the_setting_is_parsed_and_read_live(monkeypatch):
    monkeypatch.setenv("ALPACA_RESERVED_PORTS", " 7401, 7402;x,,0,70000 ")
    assert list(workspace.RESERVED_PORTS) == [7401, 7402]
    assert 7401 in workspace.RESERVED_PORTS and "7402" in workspace.RESERVED_PORTS
    assert 7403 not in workspace.RESERVED_PORTS and "x" not in workspace.RESERVED_PORTS
    monkeypatch.setenv("ALPACA_RESERVED_PORTS", "7403")
    assert list(workspace.RESERVED_PORTS) == [7403]


def test_a_reserved_port_is_never_the_derived_default(project, monkeypatch):
    monkeypatch.delenv("ALPACA_RESERVED_PORTS", raising=False)
    port = serve._default_port(project)
    monkeypatch.setenv("ALPACA_RESERVED_PORTS", str(port))
    assert serve._default_port(project) != port
