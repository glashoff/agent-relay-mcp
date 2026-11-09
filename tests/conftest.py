import pytest

from agent_relay_mcp import relay


@pytest.fixture(autouse=True)
def no_real_inhibitor(monkeypatch):
    """Tests never keep the real computer awake."""
    real = relay.KeepAwake
    monkeypatch.setattr(relay, "KeepAwake", lambda path, command=None: real(path, command=""))
