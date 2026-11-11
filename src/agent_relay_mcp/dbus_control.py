"""The control layer on the D-Bus session bus, for the GNOME extension and
agent-relay-ctl on the laptop. Needs dbus-fast (the "desktop" extra).

Name and object: io.github.glashoff.AgentRelay1 at /io/github/glashoff/AgentRelay1.
Methods take and return small JSON strings; events are the signal Event. The
relay also listens to org.gnome.ScreenSaver, so grants that end on screen lock
end even while the extension is switched off by the lock screen.

Anything that only makes things safer (state, deny, denying a host action,
revoke, cancelling a switch) is accepted from any caller of this user. Grant and Approve are only
accepted from GNOME Shell or from a process with a terminal: Claude Code
started by the relay has none. That is a hurdle, not a wall: a process of this
user can always find a way (see docs/access-control-plan.md, section 8).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass

from dbus_fast import BusType, Message, MessageType
from dbus_fast.aio import MessageBus
from dbus_fast.constants import NameFlag, RequestNameReply

from .control import Control, ControlError

log = logging.getLogger("agent_relay_mcp")

NAME = "io.github.glashoff.AgentRelay1"
PATH = "/io/github/glashoff/AgentRelay1"
INTERFACE = NAME
ERROR = NAME + ".Error"
NOT_ALLOWED = NAME + ".Error.NotAllowed"

TRUSTED_PROGRAMS = {"/usr/bin/gnome-shell"}

INTROSPECTION = f"""<!DOCTYPE node PUBLIC "-//freedesktop//DTD D-BUS Object Introspection 1.0//EN"
 "http://www.freedesktop.org/standards/dbus/1.0/introspect.dtd">
<node>
  <interface name="{INTERFACE}">
    <method name="GetState"><arg direction="out" type="s" name="state"/></method>
    <method name="Grant">
      <arg direction="in" type="s" name="options"/>
      <arg direction="out" type="s" name="result"/>
    </method>
    <method name="Approve">
      <arg direction="in" type="s" name="options"/>
      <arg direction="out" type="s" name="result"/>
    </method>
    <method name="Deny"><arg direction="in" type="s" name="request"/></method>
    <method name="DenyAction"><arg direction="in" type="s" name="action"/></method>
    <method name="Revoke">
      <arg direction="in" type="s" name="grant"/>
      <arg direction="in" type="b" name="stop"/>
      <arg direction="out" type="s" name="result"/>
    </method>
    <method name="RevokeAll">
      <arg direction="in" type="b" name="stop"/>
      <arg direction="out" type="u" name="revoked"/>
    </method>
    <method name="CancelSwitch"><arg direction="in" type="s" name="project"/></method>
    <method name="StopProject">
      <arg direction="in" type="s" name="project"/>
      <arg direction="in" type="b" name="lock"/>
      <arg direction="out" type="s" name="result"/>
    </method>
    <method name="SetKeepAwake">
      <arg direction="in" type="s" name="mode"/>
      <arg direction="in" type="b" name="lid"/>
      <arg direction="out" type="s" name="state"/>
    </method>
    <signal name="Event"><arg type="s" name="event"/></signal>
  </interface>
  <interface name="org.freedesktop.DBus.Introspectable">
    <method name="Introspect"><arg direction="out" type="s" name="xml"/></method>
  </interface>
</node>
"""

# Method: (input signature, output signature, needs a trusted caller)
METHODS = {
    "GetState": ("", "s", False),
    "Grant": ("s", "s", True),
    "Approve": ("s", "s", True),
    "Deny": ("s", "", False),
    "DenyAction": ("s", "", False),
    "Revoke": ("sb", "s", False),
    "RevokeAll": ("b", "u", False),
    "CancelSwitch": ("s", "", False),
    "StopProject": ("sb", "s", False),
    "SetKeepAwake": ("sb", "s", True),
}

GRANT_OPTIONS = {"target", "kind", "minutes", "end_on_lock", "include_host_projects",
                 "full_access_ok", "when_idle"}
APPROVE_OPTIONS = {"request", "kind", "minutes", "end_on_lock", "full_access_ok",
                   "when_idle", "messages"}


class NotAllowed(Exception):
    pass


@dataclass
class Caller:
    uid: int
    program: str        # the path of its executable, "" if unknown
    has_terminal: bool


def may_grant(caller: Caller) -> bool:
    """Whether a caller may give grants: GNOME Shell, or a process with a
    terminal (a person typing), of this user."""
    if caller.uid != os.getuid():
        return False
    return caller.program in TRUSTED_PROGRAMS or caller.has_terminal


def process(pid: int, uid: int) -> Caller:
    try:
        program = os.readlink(f"/proc/{pid}/exe")
    except OSError:
        program = ""
    try:
        with open(f"/proc/{pid}/stat") as f:
            stat = f.read()
        # After "(comm)": state, ppid, pgrp, session, tty_nr, ...
        tty = int(stat[stat.rindex(")") + 2:].split()[4])
    except (OSError, ValueError, IndexError):
        tty = 0
    return Caller(uid, program, tty != 0)


RETRY_SECONDS = (1, 2, 5, 10, 30)     # waits between reconnection attempts


class DBusControl:
    """Keeps the control interface on the session bus, reconnecting when the
    bus goes away (for example when the desktop session restarts it at a new
    login). on_state(available, reason) is told whenever that changes: while
    the interface is away nothing can be revoked, so no grant may count."""

    def __init__(self, control: Control, *, check=may_grant,
                 on_state=lambda available, reason: None):
        self.control = control
        self.check = check
        self.on_state = on_state
        self.bus: MessageBus | None = None
        self.tasks: set[asyncio.Task] = set()
        self._forwarder: asyncio.Task | None = None

    async def start(self) -> None:
        """Connects, or keeps trying in the background; never raises."""
        try:
            await self._connect()
        except Exception as error:
            log.error("cannot offer the control interface on the session bus (%s); every "
                      "project stays locked until it can", error)
            self.on_state(False, f"no control interface: {error}")
        self._spawn(self._keep_connected())

    async def _connect(self) -> None:
        bus = await MessageBus(bus_type=BusType.SESSION).connect()
        try:
            bus.add_message_handler(self._handle)
            reply = await bus.request_name(NAME, NameFlag.DO_NOT_QUEUE)
            if reply != RequestNameReply.PRIMARY_OWNER:
                raise RuntimeError(f"{NAME} is already taken on the session bus: is "
                                   "another relay running?")
            self.bus = bus
            await self._bus_call("AddMatch", "s", [
                "type='signal',interface='org.gnome.ScreenSaver',member='ActiveChanged'"])
        except Exception:
            self.bus = None
            bus.disconnect()
            raise
        self._forwarder = asyncio.ensure_future(self._forward_events())
        self.tasks.add(self._forwarder)
        self._forwarder.add_done_callback(self.tasks.discard)
        log.info("control interface on the session bus as %s", NAME)
        self.on_state(True, "")
        # Locked while the relay was away from the bus: end what ends on lock.
        try:
            reply = await bus.call(Message(
                destination="org.gnome.ScreenSaver", path="/org/gnome/ScreenSaver",
                interface="org.gnome.ScreenSaver", member="GetActive"))
            if reply.message_type == MessageType.METHOD_RETURN and reply.body == [True]:
                await self._screen_locked()
        except Exception as error:      # no GNOME here: nothing to check
            log.debug("cannot ask whether the screen is locked: %s", error)

    async def _keep_connected(self) -> None:
        attempt = 0
        while True:
            if self.bus is not None:
                try:
                    await self.bus.wait_for_disconnect()
                except Exception:
                    pass
                log.error("lost the session bus; every project stays locked until the "
                          "control interface is back")
                self.on_state(False, "control interface lost")
                if self._forwarder:
                    self._forwarder.cancel()
                self.bus = None
                attempt = 0
            await asyncio.sleep(RETRY_SECONDS[min(attempt, len(RETRY_SECONDS) - 1)])
            attempt += 1
            try:
                await self._connect()
                attempt = 0
            except Exception as error:
                if attempt <= 3 or attempt % 20 == 0:
                    log.warning("reconnecting to the session bus failed: %s", error)

    def stop(self) -> None:
        for task in list(self.tasks):
            task.cancel()
        if self.bus:
            self.bus.disconnect()

    def _spawn(self, coro) -> None:
        task = asyncio.ensure_future(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def _bus_call(self, member: str, signature: str, body: list):
        reply = await self.bus.call(Message(
            destination="org.freedesktop.DBus", path="/org/freedesktop/DBus",
            interface="org.freedesktop.DBus", member=member, signature=signature, body=body))
        if reply.message_type == MessageType.ERROR:
            raise RuntimeError(f"{member}: {reply.body}")
        return reply.body

    async def _forward_events(self) -> None:
        async for event in self.control.events():
            bus = self.bus
            if bus is None or not bus.connected:
                return          # _keep_connected starts a new forwarder
            try:
                bus.send(Message.new_signal(PATH, INTERFACE, "Event", "s",
                                            [json.dumps(event)]))
            except Exception as error:
                log.warning("cannot send an event on the session bus: %s", error)
                return

    # --- incoming messages ------------------------------------------------------

    def _handle(self, msg: Message):
        if msg.message_type == MessageType.SIGNAL:
            if msg.interface == "org.gnome.ScreenSaver" and msg.member == "ActiveChanged" \
                    and msg.body == [True]:
                # Anyone may send this; it can only end grants.
                self._spawn(self._screen_locked())
            return None
        if msg.message_type != MessageType.METHOD_CALL or msg.path != PATH:
            return None
        if msg.interface == "org.freedesktop.DBus.Introspectable" \
                and msg.member == "Introspect":
            return Message.new_method_return(msg, "s", [INTROSPECTION])
        if msg.interface not in (INTERFACE, None) or msg.member not in METHODS:
            return None
        self._spawn(self._dispatch(msg))
        return True

    async def _screen_locked(self) -> None:
        try:
            await self.control.screen_locked()
        except ControlError as error:
            log.warning("ending grants on screen lock failed: %s", error)

    async def _dispatch(self, msg: Message) -> None:
        signature, out, trusted_only = METHODS[msg.member]
        try:
            if msg.signature != signature:
                raise ControlError(f"{msg.member} takes ({signature}), not ({msg.signature}).")
            if trusted_only:
                await self._check_caller(msg.sender)
            body = await self._call(msg.member, msg.body)
            reply = Message.new_method_return(msg, out, [body] if out else [])
        except NotAllowed as error:
            log.warning("refused %s from %s: %s", msg.member, msg.sender, error)
            reply = Message.new_error(msg, NOT_ALLOWED, str(error))
        except ControlError as error:
            reply = Message.new_error(msg, ERROR, str(error))
        except Exception as error:
            log.exception("D-Bus call %s failed", msg.member)
            reply = Message.new_error(msg, ERROR, f"Unexpected error: {error}")
        self.bus.send(reply)

    async def _check_caller(self, sender: str) -> None:
        (credentials,) = await self._bus_call("GetConnectionCredentials", "s", [sender])
        uid = credentials["UnixUserID"].value
        pid = credentials["ProcessID"].value
        caller = process(pid, uid)
        if not self.check(caller):
            raise NotAllowed("Grants and approvals are only accepted from GNOME Shell or "
                             f"from a terminal (caller: {caller.program or 'unknown'}).")

    async def _call(self, member: str, body: list):
        c = self.control
        if member == "GetState":
            return json.dumps(c.state())
        if member == "Grant":
            options = _options(body[0], GRANT_OPTIONS, "target")
            target = options.pop("target")
            return json.dumps(await c.grant(target, **options))
        if member == "Approve":
            options = _options(body[0], APPROVE_OPTIONS, "request")
            request = options.pop("request")
            return json.dumps(await c.approve(request, **options))
        if member == "Deny":
            await c.deny(body[0])
            return None
        if member == "DenyAction":
            await c.deny_action(body[0])
            return None
        if member == "Revoke":
            return json.dumps(await c.revoke(body[0], body[1]))
        if member == "RevokeAll":
            return await c.revoke_all(body[0])
        if member == "StopProject":
            return json.dumps(await c.stop_project(body[0], body[1]))
        if member == "SetKeepAwake":
            return json.dumps(c.keep_awake(body[0], body[1]))
        if member == "CancelSwitch":
            await c.cancel_switch(body[0])
            return None
        raise ControlError(f"Unknown method {member}.")


def _options(text: str, allowed: set[str], required: str) -> dict:
    try:
        options = json.loads(text)
    except ValueError as error:
        raise ControlError(f"The options are not JSON: {error}") from error
    if not isinstance(options, dict) or required not in options:
        raise ControlError(f"The options must be a JSON object with {required!r}.")
    unknown = set(options) - allowed
    if unknown:
        raise ControlError(f"Unknown options: {', '.join(sorted(unknown))}.")
    return options
