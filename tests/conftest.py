import pytest

from agent_relay_mcp import relay


@pytest.fixture(autouse=True)
def no_systemd_scopes(monkeypatch):
    """Tests never start runs in real systemd scopes, even when pytest runs
    inside a systemd service (as under the relay itself)."""
    monkeypatch.delenv("INVOCATION_ID", raising=False)


@pytest.fixture(autouse=True)
def no_real_inhibitor(monkeypatch):
    """Tests never keep the real computer awake."""
    real = relay.KeepAwake
    monkeypatch.setattr(relay, "KeepAwake", lambda path, command=None: real(path, command=""))
