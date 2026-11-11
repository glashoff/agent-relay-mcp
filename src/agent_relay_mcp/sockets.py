"""The relay on Unix sockets, for clients on this machine that have no TCP path
to it (the voice service in its container).

Two sockets, both optional:

- the MCP endpoint (mcp_socket): the same app as the TCP listener, served by a
  second uvicorn server next to it (see server.serve);
- the event stream (events_socket): every control event as one JSON line,
  exactly the JSON of the D-Bus signal Event. Read-only: the relay reads
  nothing from it, so nothing that grants or approves is reachable there. This
  is the events part of the Unix socket adapter of the access-control plan.

Sockets are created with mode 600, in a directory created with mode 700; a
stale socket file is replaced, one a process still listens on is not.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import socket
import stat
from pathlib import Path
from typing import AsyncIterator, Callable

log = logging.getLogger("agent_relay_mcp")

# Lines a client may be behind before it is dropped, and clients at once.
CLIENT_QUEUE = 256
MAX_CLIENTS = 32


class SocketError(Exception):
    """A socket that cannot be set up; the text says why."""


def bind(path: Path) -> socket.socket:
    """A Unix stream socket bound to path, mode 600, not yet listening."""
    if not path.parent.is_dir():
        try:
            path.parent.mkdir(mode=0o700, parents=True)
        except OSError as error:
            raise SocketError(f"cannot create {path.parent}: {error}") from error
    _remove_stale(path)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    old_umask = os.umask(0o177)     # no moment with wider permissions
    try:
        sock.bind(str(path))
    except OSError as error:
        sock.close()
        raise SocketError(f"cannot bind {path}: {error}") from error
    finally:
        os.umask(old_umask)
    os.chmod(path, 0o600)
    return sock


def _remove_stale(path: Path) -> None:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return
    if not stat.S_ISSOCK(mode):
        raise SocketError(f"{path} exists and is not a socket; not replacing it")
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.settimeout(2)
    try:
        probe.connect(str(path))
    except OSError:
        path.unlink(missing_ok=True)        # nobody listens: left over
        return
    finally:
        probe.close()
    raise SocketError(f"{path} is in use: is another relay running?")


def inode(path: Path) -> int:
    """The inode of the socket file, right after bind(), for unlink()."""
    return path.lstat().st_ino


def unlink(path: Path, ino: int) -> None:
    """Removes the socket file at shutdown, unless it has been replaced since
    (ino: inode() of the file right after binding)."""
    with contextlib.suppress(OSError):
        if path.lstat().st_ino == ino:
            path.unlink()


class EventsSocket:
    """Every control event to every connected client, one JSON line each."""

    def __init__(self, path: Path, events: Callable[[], AsyncIterator[dict]]):
        self.path = path
        self.events = events            # the same source as the D-Bus Event signal
        self.clients: dict[asyncio.Queue, asyncio.StreamWriter] = {}
        self.tasks: set[asyncio.Task] = set()
        self.server: asyncio.AbstractServer | None = None
        self._inode = 0

    async def start(self) -> None:
        sock = bind(self.path)
        self._inode = inode(self.path)
        self.server = await asyncio.start_unix_server(self._client, sock=sock)
        self._spawn(self._fan_out())
        log.info("event stream on %s", self.path)

    async def stop(self) -> None:
        if self.server:
            self.server.close()
        for task in list(self.tasks):
            task.cancel()
        for writer in list(self.clients.values()):
            writer.transport.abort()
        self.clients.clear()
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)
        unlink(self.path, self._inode)

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.ensure_future(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    async def _fan_out(self) -> None:
        async for event in self.events():
            line = (json.dumps(event) + "\n").encode()
            for queue, writer in list(self.clients.items()):
                try:
                    queue.put_nowait(line)
                except asyncio.QueueFull:
                    log.warning("event stream: dropping a client that does not keep up")
                    self._drop(queue, writer)

    def _drop(self, queue: asyncio.Queue, writer: asyncio.StreamWriter) -> None:
        if self.clients.pop(queue, None) is not None:
            writer.transport.abort()

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if len(self.clients) >= MAX_CLIENTS:
            log.warning("event stream: too many clients, refusing one")
            writer.transport.abort()
            return
        queue: asyncio.Queue = asyncio.Queue(maxsize=CLIENT_QUEUE)
        self.clients[queue] = writer
        sender = self._spawn(self._send(queue, writer))
        try:
            while await reader.read(4096):      # whatever the client sends: ignored
                pass
        except (ConnectionError, OSError):
            pass
        finally:
            sender.cancel()
            self._drop(queue, writer)

    async def _send(self, queue: asyncio.Queue, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                writer.write(await queue.get())
                await writer.drain()
        except (ConnectionError, OSError):
            self._drop(queue, writer)
