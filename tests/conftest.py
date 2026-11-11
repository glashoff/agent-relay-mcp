import os
import signal

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


@pytest.fixture(autouse=True)
def end_open_runs(monkeypatch):
    """Runs stay open after a request until they are closed; ends those a
    test leaves behind (their runner leads its own process group)."""
    pids = []
    launch = relay.Relay._launch

    def recording(self, target, turn, session_id, resume):
        try:
            launch(self, target, turn, session_id, resume)
        finally:
            if turn.pid > 1:
                pids.append(turn.pid)
    monkeypatch.setattr(relay.Relay, "_launch", recording)
    yield
    for pid in pids:
        try:
            os.killpg(pid, signal.SIGTERM)
        except OSError:
            pass
