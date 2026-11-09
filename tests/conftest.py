import pytest

from agent_relay_mcp import keep_awake


@pytest.fixture(autouse=True)
def no_real_inhibitor(monkeypatch):
    """Tests never keep the real computer awake."""
    monkeypatch.setattr(keep_awake, "COMMAND", "")
