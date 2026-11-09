"""The control layer: what the GNOME extension and agent-relay-ctl can do.

Independent of how it is reached: a transport (D-Bus on the laptop, later a
Unix socket on a server) calls these methods with plain values and passes the
results and events on as JSON. It is never reachable through an MCP tool, and
it checks nothing about its caller; transports do that.
"""

from __future__ import annotations

from typing import AsyncIterator

from .access import CONTAINER, HOST, AccessError
from .relay import Relay
from .targets import RelayError


class ControlError(Exception):
    """A control request that cannot be done; the text says why, for the user."""


class Control:
    def __init__(self, relay: Relay):
        self.relay = relay

    async def _call(self, coro):
        try:
            return await coro
        except (AccessError, RelayError) as error:
            raise ControlError(str(error)) from error

    def state(self) -> dict:
        try:
            return self.relay.state()
        except RelayError as error:
            raise ControlError(str(error)) from error

    async def grant(self, target: str, kind: str = "", minutes: int = 60, *,
                    end_on_lock: bool | None = None, include_host_projects: bool = False,
                    full_access_ok: bool = False, when_idle: bool = False) -> dict:
        """Grants target (a project, or "*" for all projects) for minutes. kind
        "host" runs a project with a dev container on the host instead; it,
        host projects and include_host_projects need full_access_ok, the
        confirmation of the warning."""
        if kind not in ("", CONTAINER, HOST):
            raise ControlError(f"Unknown kind {kind!r}: use {CONTAINER!r} or {HOST!r}.")
        return await self._call(self.relay.grant(
            target, kind or None, _seconds(minutes), end_on_lock=end_on_lock,
            include_host_projects=include_host_projects, full_access_ok=full_access_ok,
            when_idle=when_idle))

    async def approve(self, request_id: str, kind: str = "", minutes: int = 60, *,
                      end_on_lock: bool | None = None, full_access_ok: bool = False,
                      when_idle: bool = False) -> dict:
        if kind not in ("", CONTAINER, HOST):
            raise ControlError(f"Unknown kind {kind!r}: use {CONTAINER!r} or {HOST!r}.")
        return await self._call(self.relay.approve(
            request_id, kind or None, _seconds(minutes), end_on_lock=end_on_lock,
            full_access_ok=full_access_ok, when_idle=when_idle))

    async def deny(self, request_id: str) -> None:
        await self._call(self.relay.deny(request_id))

    async def revoke(self, grant_id: str, stop: bool = False) -> dict:
        return await self._call(self.relay.revoke(grant_id, stop))

    async def revoke_all(self, stop: bool = False) -> int:
        """Revokes every grant; the number revoked."""
        ids = list(self.relay.access.grants)
        for grant_id in ids:
            await self._call(self.relay.revoke(grant_id, stop))
        return len(ids)

    async def cancel_switch(self, project: str) -> None:
        await self._call(self.relay.cancel_switch(project))

    async def stop_project(self, project: str, lock: bool = False) -> dict:
        return await self._call(self.relay.stop_project(project, lock))

    def keep_awake(self, mode: str, lid: bool) -> dict:
        try:
            return self.relay.set_keep_awake(mode, lid)
        except AccessError as error:
            raise ControlError(str(error)) from error

    async def screen_locked(self) -> None:
        await self._call(self.relay.screen_locked())

    async def events(self) -> AsyncIterator[dict]:
        """Every event from now on, until the caller stops iterating."""
        queue = self.relay.subscribe()
        try:
            while True:
                yield await queue.get()
        finally:
            self.relay.unsubscribe(queue)


def _seconds(minutes: int) -> int:
    if not isinstance(minutes, int) or minutes < 1:
        raise ControlError("The duration must be at least one minute.")
    return minutes * 60

