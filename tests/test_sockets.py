"""The relay on Unix sockets, its only way in: the MCP endpoint and the event
stream."""

import asyncio
import json
import shutil
import socket
import stat
import sys
import tempfile
from pathlib import Path

import httpx2
import pytest
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

from agent_relay_mcp import relay as relay_module
from agent_relay_mcp import sockets
from agent_relay_mcp.config import Config, expand_socket_path, load
from agent_relay_mcp.control import Control
from agent_relay_mcp.relay import Relay
from agent_relay_mcp.server import build, http_app, serve

FAKE = Path(__file__).with_name("fake_claude.py")


@pytest.fixture
def run_dir():
    """A short directory standing in for $XDG_RUNTIME_DIR: socket paths are
    limited to about 100 bytes, too few for pytest's tmp_path."""
    path = Path(tempfile.mkdtemp(prefix="relay-", dir="/tmp" if Path("/tmp").is_dir() else None))
    yield path
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def config(tmp_path, run_dir, monkeypatch):
    monkeypatch.setattr(relay_module, "SWEEP_SECONDS", 3600)
    (tmp_path / "projects/web/.devcontainer").mkdir(parents=True)
    (tmp_path / "projects/web/.devcontainer/devcontainer.json").write_text("{}")
    # Never the real session bus: the relay's control interface stays off.
    return Config(projects_dir=tmp_path / "projects", allow_host_projects=True,
                  claude_command=f"{sys.executable} {FAKE}", state_dir=tmp_path / "state",
                  keep_awake_command="", control_dbus=False,
                  mcp_socket=str(run_dir / "agent-relay/mcp.sock"),
                  events_socket=str(run_dir / "agent-relay/events.sock"))


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


async def wait_for(path: Path, timeout: float = 10) -> None:
    for _ in range(int(timeout * 20)):
        if path.exists():
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"{path} did not appear")


async def read_event(reader: asyncio.StreamReader) -> dict:
    return json.loads(await asyncio.wait_for(reader.readline(), 5))


# --- configuration -----------------------------------------------------------------

def test_socket_paths_expand_the_runtime_dir_and_home(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1234")
    monkeypatch.setenv("HOME", str(tmp_path))
    path = tmp_path / "config.toml"
    path.write_text('mcp_socket = "$XDG_RUNTIME_DIR/agent-relay/mcp.sock"\n'
                    'events_socket = "~/run/events.sock"\n')
    config = load(path)
    assert config.mcp_socket == "/run/user/1234/agent-relay/mcp.sock"
    assert config.events_socket == f"{tmp_path}/run/events.sock"


def test_both_sockets_are_on_by_default(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1234")
    config = load(tmp_path / "missing.toml")
    assert config.mcp_socket == "/run/user/1234/agent-relay/mcp.sock"
    assert config.events_socket == "/run/user/1234/agent-relay/events.sock"


def test_the_mcp_socket_is_required_and_the_events_socket_optional(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1234")
    path = tmp_path / "config.toml"
    path.write_text('mcp_socket = ""\n')
    with pytest.raises(SystemExit, match="only way to reach"):
        load(path)
    path.write_text('events_socket = ""\n')
    assert load(path).events_socket == ""


def test_the_keys_of_the_tcp_listener_are_ignored_with_a_warning(monkeypatch, tmp_path,
                                                                 caplog):
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1234")
    path = tmp_path / "config.toml"
    path.write_text('listen_host = "127.0.0.1"\nlisten_port = 8765\n'
                    'public_hosts = ["example.org"]\n'
                    'mcp_socket = "$XDG_RUNTIME_DIR/agent-relay/mcp.sock"\n')
    config = load(path)
    assert config.mcp_socket == "/run/user/1234/agent-relay/mcp.sock"
    assert "ignoring listen_host, listen_port, public_hosts" in caplog.text
    assert not hasattr(config, "public_hosts")


def test_a_socket_path_needs_its_variables_and_must_be_absolute(monkeypatch):
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    with pytest.raises(SystemExit, match="not set"):
        expand_socket_path("$XDG_RUNTIME_DIR/agent-relay/mcp.sock")
    with pytest.raises(SystemExit, match="absolute"):
        expand_socket_path("agent-relay/mcp.sock")


# --- creating and removing sockets ----------------------------------------------------

def test_the_directory_and_the_socket_are_private(run_dir):
    path = run_dir / "agent-relay/mcp.sock"
    sock = sockets.bind(path)
    try:
        assert mode(path.parent) == 0o700
        assert stat.S_ISSOCK(path.lstat().st_mode) and mode(path) == 0o600
    finally:
        sock.close()


def test_a_stale_socket_is_replaced_and_a_live_one_or_a_file_is_not(run_dir):
    path = run_dir / "x.sock"
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(str(path))
    stale.close()                               # left behind by a crash
    sock = sockets.bind(path)
    sock.listen()
    try:
        with pytest.raises(sockets.SocketError, match="in use"):
            sockets.bind(path)
        assert path.exists()
    finally:
        sock.close()
    other = run_dir / "file"
    other.write_text("keep me")
    with pytest.raises(sockets.SocketError, match="not a socket"):
        sockets.bind(other)
    assert other.read_text() == "keep me"


def test_unlink_leaves_a_socket_that_replaced_ours(run_dir):
    path = run_dir / "x.sock"
    sock = sockets.bind(path)
    ino = sockets.inode(path)
    sock.close()
    path.unlink()
    newer = sockets.bind(path)
    try:
        sockets.unlink(path, ino)
        assert path.exists()
        sockets.unlink(path, sockets.inode(path))
        assert not path.exists()
    finally:
        newer.close()


# --- the event stream ---------------------------------------------------------------

async def events_socket(config):
    relay = Relay(config)
    stream = sockets.EventsSocket(Path(config.events_socket), Control(relay).events)
    await stream.start()
    return relay, stream


def test_every_client_gets_each_event_as_the_dbus_json(config):
    async def scenario():
        relay, stream = await events_socket(config)
        path = Path(config.events_socket)
        assert mode(path) == 0o600 and mode(path.parent) == 0o700
        queue = relay.subscribe()      # what the D-Bus forwarder gets
        first = await asyncio.open_unix_connection(str(path))
        second = await asyncio.open_unix_connection(str(path))
        await asyncio.sleep(0.05)
        relay._emit("run_ended", project="web", reply="done ✓")
        expected = queue.get_nowait()
        for reader, _ in (first, second):
            line = await asyncio.wait_for(reader.readline(), 5)
            assert line.endswith(b"\n") and line.count(b"\n") == 1
            # Exactly what the D-Bus Event signal carries (json.dumps of the event).
            assert line == (json.dumps(expected) + "\n").encode()
            assert json.loads(line)["type"] == "run_ended"
        await stream.stop()
        relay.close()
        assert not path.exists()
    asyncio.run(scenario())


def test_what_clients_send_is_ignored_and_grants_nothing(config):
    async def scenario():
        relay, stream = await events_socket(config)
        reader, writer = await asyncio.open_unix_connection(config.events_socket)
        for command in ({"cmd": "grant", "target": "*", "minutes": 60},
                        {"method": "Grant", "args": ["web"]}):
            writer.write(json.dumps(command).encode() + b"\n")
        writer.write(b"not json at all\n")
        await writer.drain()
        await asyncio.sleep(0.1)
        relay._emit("run_started", project="web")
        assert (await read_event(reader))["type"] == "run_started"
        assert not relay.access.grants and not relay.access.requests
        writer.close()
        await stream.stop()
        relay.close()
    asyncio.run(scenario())


def test_a_client_that_does_not_keep_up_is_dropped_without_blocking(config):
    async def scenario():
        relay, stream = await events_socket(config)
        slow_reader, slow_writer = await asyncio.open_unix_connection(config.events_socket)
        reader, writer = await asyncio.open_unix_connection(config.events_socket)
        await asyncio.sleep(0.05)
        assert len(stream.clients) == 2
        total = sockets.CLIENT_QUEUE * 4
        received = []

        async def read_all():       # the fast client keeps up
            while len(received) < total:
                received.append(json.loads(await reader.readline())["n"])
        fast = asyncio.ensure_future(read_all())
        big = "x" * 5000
        for i in range(total):
            relay._emit("test", n=i, message=big)    # never blocks the relay
            if i % 50 == 49:
                while len(received) < i - 100:
                    await asyncio.sleep(0.001)
        await asyncio.wait_for(fast, 10)
        assert received == list(range(total))
        assert len(stream.clients) == 1         # the slow one is gone
        # The slow client's connection is closed after what was sent already.
        async def until_closed():
            try:
                while await slow_reader.read(1 << 20):
                    pass
            except ConnectionError:
                pass
        await asyncio.wait_for(until_closed(), 5)
        for w in (slow_writer, writer):
            w.close()
        await stream.stop()
        relay.close()
    asyncio.run(scenario())


# --- MCP and events on sockets, the only listeners ----------------------------------------

def test_mcp_over_the_unix_socket_with_events_on_the_other(config):
    server, relay = build(config)
    app = http_app(server, config)
    mcp_path, events_path = Path(config.mcp_socket), Path(config.events_socket)

    async def scenario():
        stop = asyncio.Event()
        serving = asyncio.ensure_future(serve(app, config, stop=stop, signals=False,
                                              log_level="warning"))
        await wait_for(mcp_path)
        await wait_for(events_path)
        assert mode(mcp_path) == 0o600 and mode(mcp_path.parent) == 0o700
        events, _ = await asyncio.open_unix_connection(str(events_path))
        transport = httpx2.AsyncHTTPTransport(uds=str(mcp_path))
        # The forwarder in the voice container sends this Host.
        async with httpx2.AsyncClient(transport=transport) as http:
            url = "http://localhost:8765/mcp"
            async with streamable_http_client(url, http_client=http) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tools = {tool.name for tool in (await session.list_tools()).tools}
                    assert tools == {"list_projects", "send_message", "get_updates", "stop",
                                     "release_access"}
                    result = await session.call_tool("send_message", {
                        "project": "web", "message": "hello"})
                    assert "waits for approval" in result.content[0].text
            # Host and Origin are checked as on loopback.
            body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
            headers = {"Accept": "application/json, text/event-stream"}
            response = await http.post("http://evil.example/mcp", json=body, headers=headers)
            assert response.status_code in (400, 421)
            response = await http.post(url, json=body, headers={
                **headers, "Origin": "https://evil.example"})
            assert response.status_code in (400, 403)
            for host in ("localhost", "127.0.0.1"):           # without a port too
                response = await http.post(f"http://{host}/mcp", json=body, headers=headers)
                assert response.status_code == 200
        event = await read_event(events)
        while event["type"] == "control":
            event = await read_event(events)
        assert event["type"] == "request_added" and event["project"] == "web"
        assert not relay.access.grants          # nothing was approved through a socket
        stop.set()
        await asyncio.wait_for(serving, 10)
        assert not mcp_path.exists() and not events_path.exists()
    asyncio.run(scenario())


def test_a_live_socket_stops_the_start(config, run_dir):
    path = Path(config.mcp_socket)
    other = sockets.bind(path)
    other.listen()
    server, _ = build(config)

    async def scenario():
        with pytest.raises(sockets.SocketError, match="in use"):
            await serve(http_app(server, config), config, stop=asyncio.Event(),
                        signals=False)
    try:
        asyncio.run(scenario())
        assert path.exists()
    finally:
        other.close()
