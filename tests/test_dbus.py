"""The control interface on D-Bus and agent-relay-ctl, on a private session bus
(dbus-daemon started for the test), never the real one."""

import asyncio
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

dbus_fast = pytest.importorskip("dbus_fast")
from dbus_fast import Message, MessageType  # noqa: E402
from dbus_fast.aio import MessageBus  # noqa: E402

from agent_relay_mcp import ctl  # noqa: E402
from agent_relay_mcp import relay as relay_module  # noqa: E402
from agent_relay_mcp.config import Config  # noqa: E402
from agent_relay_mcp.control import Control  # noqa: E402
from agent_relay_mcp.dbus_control import (INTERFACE, NAME, NOT_ALLOWED, PATH, Caller,  # noqa: E402
                                          DBusControl, may_grant, process)
from agent_relay_mcp.relay import Relay  # noqa: E402

FAKE = Path(__file__).with_name("fake_claude.py")

pytestmark = pytest.mark.skipif(not shutil.which("dbus-daemon"), reason="needs dbus-daemon")


@pytest.fixture
def private_bus(monkeypatch):
    daemon = subprocess.Popen(["dbus-daemon", "--session", "--nofork", "--print-address=1"],
                              stdout=subprocess.PIPE, text=True)
    address = daemon.stdout.readline().strip()
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", address)
    yield address
    daemon.terminate()
    daemon.wait()


@pytest.fixture
def config(tmp_path, monkeypatch, private_bus):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(relay_module, "SWEEP_SECONDS", 3600)
    projects = tmp_path / "projects"
    (projects / "alpha").mkdir(parents=True)
    (projects / "web/.devcontainer").mkdir(parents=True)
    (projects / "web/.devcontainer/devcontainer.json").write_text("{}")
    return Config(projects_dir=projects, allow_host_projects=True,
                  claude_command=f"{sys.executable} {FAKE}", state_dir=tmp_path / "state")


def run(coro):
    return asyncio.run(coro)


async def service(config, check=lambda caller: True):
    relay = Relay(config)
    bus = DBusControl(Control(relay), check=check)
    await bus.start()
    return relay, bus


async def call(member, signature="", body=None):
    client = await MessageBus().connect()
    try:
        return await client.call(Message(destination=NAME, path=PATH, interface=INTERFACE,
                                         member=member, signature=signature, body=body or []))
    finally:
        client.disconnect()


def test_state_and_grants_over_dbus(config):
    async def scenario():
        relay, bus = await service(config)
        reply = await call("GetState")
        state = json.loads(reply.body[0])
        assert {p["name"] for p in state["projects"]} == {"alpha", "web"}
        reply = await call("Grant", "s", [json.dumps({"target": "web", "minutes": 30})])
        assert reply.message_type == MessageType.METHOD_RETURN, reply.body
        grant = json.loads(reply.body[0])["grant"]
        assert grant["target"] == "web" and relay.access.grants
        reply = await call("Revoke", "sb", [grant["id"], False])
        assert reply.message_type == MessageType.METHOD_RETURN and not relay.access.grants
        bus.stop()
    run(scenario())


def test_errors_come_back_as_dbus_errors(config):
    async def scenario():
        _, bus = await service(config)
        reply = await call("Grant", "s", [json.dumps({"target": "alpha"})])
        assert reply.message_type == MessageType.ERROR
        assert "Needs confirmation" in reply.body[0]
        reply = await call("Grant", "s", [json.dumps({"target": "web", "stop": True})])
        assert "Unknown options: stop" in reply.body[0]
        reply = await call("Grant", "s", ["not json"])
        assert "not JSON" in reply.body[0]
        reply = await call("Revoke", "s", ["x"])
        assert "takes (sb)" in reply.body[0]
        bus.stop()
    run(scenario())


def test_untrusted_callers_cannot_grant_but_can_make_things_safer(config):
    async def scenario():
        relay, bus = await service(config, check=lambda caller: False)
        await Control(relay).grant("web")                 # given at the laptop
        await relay.send_message("alpha", "hi", False, 0)
        request = next(iter(relay.access.requests.values()))
        for member, options in (("Grant", {"target": "web"}),
                                ("Approve", {"request": request.id, "full_access_ok": True})):
            reply = await call(member, "s", [json.dumps(options)])
            assert reply.message_type == MessageType.ERROR
            assert reply.error_name == NOT_ALLOWED
        assert (await call("Deny", "s", [request.id])).message_type == \
            MessageType.METHOD_RETURN
        reply = await call("RevokeAll", "b", [False])
        assert reply.body == [1] and not relay.access.grants
        bus.stop()
    run(scenario())


def test_who_may_grant():
    me = os.getuid()
    assert may_grant(Caller(me, "/usr/bin/gnome-shell", False))
    assert may_grant(Caller(me, "/usr/bin/python3", True))           # a terminal
    assert not may_grant(Caller(me, "/usr/bin/python3", False))      # e.g. Claude Code
    assert not may_grant(Caller(me, "/home/x/gnome-shell", False))    # not the real one
    assert not may_grant(Caller(me + 1, "/usr/bin/gnome-shell", True))
    caller = process(os.getpid(), me)
    assert caller.program == os.path.realpath(sys.executable)


def test_screen_lock_ends_grants_that_say_so(config):
    async def scenario():
        relay, bus = await service(config)
        control = Control(relay)
        await control.grant("web", end_on_lock=False)
        await control.grant("*")
        sender = await MessageBus().connect()
        sender.send(Message.new_signal("/org/gnome/ScreenSaver", "org.gnome.ScreenSaver",
                                       "ActiveChanged", "b", [True]))
        for _ in range(50):
            if len(relay.access.grants) == 1:
                break
            await asyncio.sleep(0.05)
        assert [g.target for g in relay.access.grants.values()] == ["web"]
        sender.disconnect()
        bus.stop()
    run(scenario())


def test_events_are_signals(config):
    async def scenario():
        relay, bus = await service(config)
        listener = await MessageBus().connect()
        await listener.call(Message(
            destination="org.freedesktop.DBus", path="/org/freedesktop/DBus",
            interface="org.freedesktop.DBus", member="AddMatch", signature="s",
            body=[f"type='signal',interface='{INTERFACE}',member='Event'"]))
        received = asyncio.Queue()
        listener.add_message_handler(
            lambda m: received.put_nowait(json.loads(m.body[0]))
            if m.message_type == MessageType.SIGNAL and m.member == "Event" else None)
        await relay.send_message("web", "hello", False, 0)
        event = await asyncio.wait_for(received.get(), 3)
        assert event["type"] == "request_added" and event["project"] == "web"
        listener.disconnect()
        bus.stop()
    run(scenario())


def test_only_one_relay_owns_the_name(config):
    async def scenario():
        _, bus = await service(config)
        with pytest.raises(RuntimeError, match="already taken"):
            await service(config)
        bus.stop()
    run(scenario())


# --- agent-relay-ctl --------------------------------------------------------------

async def ctl_run(argv, monkeypatch=None, confirmed=True):
    args = ctl.parser().parse_args(argv)
    if args.command is None:
        args.json = False
    client = await ctl.Client().connect()
    try:
        await ctl.command(args, client)
    finally:
        client.bus.disconnect()


def test_ctl_grant_approve_and_revoke(config, monkeypatch, capsys):
    asked = []
    monkeypatch.setattr(ctl, "confirm", lambda question, answer: asked.append(answer))

    async def scenario():
        relay, bus = await service(config)
        await ctl_run(["grant", "web", "--for", "2h"])
        assert "Granted" in capsys.readouterr().out and asked == ["yes"]
        await ctl_run(["grant", "web", "--on-host", "--for", "1h"])
        assert asked[-1] == "full access"
        assert next(iter(relay.access.grants.values())).kind == "host"
        await relay.send_message("alpha", "hi", False, 0)
        number = next(iter(relay.access.requests.values())).number
        await ctl_run(["approve", str(number), "--for", "30m"])
        assert asked[-1] == "full access"           # alpha has no dev container
        assert not relay.access.requests
        await ctl_run([])
        out = capsys.readouterr().out
        assert "FULL ACCESS" in out and "alpha" in out
        await ctl_run(["revoke", "--all"])
        assert "Revoked 2 grant(s)" in capsys.readouterr().out
        bus.stop()
    run(scenario())


def test_ctl_does_nothing_without_confirmation(config, monkeypatch):
    def refuse(question, answer):
        raise ctl.Failure("Not confirmed; nothing was changed.")
    monkeypatch.setattr(ctl, "confirm", refuse)

    async def scenario():
        relay, bus = await service(config)
        with pytest.raises(ctl.Failure, match="Not confirmed"):
            await ctl_run(["grant", "web"])
        assert not relay.access.grants
        bus.stop()
    run(scenario())


def test_ctl_says_when_the_relay_is_not_running(config):
    with pytest.raises(ctl.Failure, match="not running"):
        run(ctl_run([]))


def test_durations():
    assert ctl.minutes("90") == 90
    assert ctl.minutes("2h") == 120
    assert ctl.minutes("1h30m") == 90
    assert ctl.minutes("45m") == 45
    for bad in ("", "0", "abc", "1d"):
        with pytest.raises(Exception):
            ctl.minutes(bad)


def test_ctl_approves_a_host_request_on_the_host_with_full_access(config, monkeypatch, capsys):
    asked = []
    monkeypatch.setattr(ctl, "confirm", lambda question, answer: asked.append((question, answer)))

    async def scenario():
        relay, bus = await service(config)
        await relay.send_message("web", "on the host", False, 0, run_on="host")
        await ctl_run([])
        assert "asks to run ON THE HOST" in capsys.readouterr().out
        number = next(iter(relay.access.requests.values())).number
        await ctl_run(["approve", str(number)])
        question, answer = asked[-1]
        assert answer == "full access" and "on the host" in question
        assert next(iter(relay.access.grants.values())).kind == "host"
        bus.stop()
    run(scenario())
