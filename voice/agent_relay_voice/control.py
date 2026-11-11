"""Control socket for the GNOME extension (docs/voice-plan.md, section 8).

JSON lines in both directions on a Unix socket:

- request {"id": 1, "cmd": "<command>", ...arguments}
  -> reply {"id": 1, "ok": true, "result": ...} or {"id": 1, "ok": false, "error": "..."}
- pushed to every client: {"event": "state", "state": {...}} and
  {"event": "exchange", "user": "...", "assistant": "..."}

The commands themselves are handled by the service (handler(cmd, args) ->
result, raising CommandError for a refusal).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Awaitable, Callable

log = logging.getLogger("voice.control")

MAX_LINE = 64 * 1024


class CommandError(Exception):
    pass


Handler = Callable[[str, dict], Awaitable[object]]


class ControlServer:
    def __init__(self, path: Path, handler: Handler) -> None:
        self.path = path
        self.handler = handler
        self.server: asyncio.base_events.Server | None = None
        self.clients: set[asyncio.StreamWriter] = set()

    async def start(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists() or self.path.is_symlink():
            self.path.unlink()          # stale socket from an earlier run
        old = os.umask(0o177)
        try:
            self.server = await asyncio.start_unix_server(self._client, path=str(self.path),
                                                          limit=MAX_LINE)
        finally:
            os.umask(old)
        os.chmod(self.path, 0o600)
        log.info("control socket at %s", self.path)

    async def close(self) -> None:
        if self.server:
            self.server.close()
        for writer in list(self.clients):
            writer.close()
        try:
            self.path.unlink()
        except OSError:
            pass

    def broadcast(self, message: dict) -> None:
        data = (json.dumps(message, ensure_ascii=False) + "\n").encode()
        for writer in list(self.clients):
            if writer.is_closing():
                self.clients.discard(writer)
                continue
            # A client that does not read is dropped instead of buffering forever.
            transport = writer.transport
            if transport.get_write_buffer_size() > 1024 * 1024:
                log.info("dropping a control client that does not keep up")
                self.clients.discard(writer)
                writer.close()
                continue
            writer.write(data)

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.clients.add(writer)
        try:
            while True:
                try:
                    line = await reader.readline()
                except (asyncio.LimitOverrunError, ValueError):
                    self._send(writer, {"id": None, "ok": False, "error": "line too long"})
                    break
                if not line:
                    break
                if not line.strip():
                    continue
                reply = await self.handle_line(line)
                self._send(writer, reply)
        except ConnectionError:
            pass
        finally:
            self.clients.discard(writer)
            writer.close()

    async def handle_line(self, line: bytes) -> dict:
        try:
            request = json.loads(line)
        except ValueError:
            return {"id": None, "ok": False, "error": "invalid JSON"}
        if not isinstance(request, dict):
            return {"id": None, "ok": False, "error": "a request is a JSON object"}
        rid = request.get("id")
        cmd = request.get("cmd")
        if not isinstance(cmd, str):
            return {"id": rid, "ok": False, "error": "missing cmd"}
        args = {k: v for k, v in request.items() if k not in ("id", "cmd")}
        try:
            result = await self.handler(cmd, args)
        except CommandError as error:
            return {"id": rid, "ok": False, "error": str(error)}
        except Exception as error:  # noqa: BLE001 - report, keep serving
            log.exception("command %s failed", cmd)
            return {"id": rid, "ok": False, "error": f"internal error: {error}"}
        return {"id": rid, "ok": True, "result": result}

    @staticmethod
    def _send(writer: asyncio.StreamWriter, message: dict) -> None:
        if not writer.is_closing():
            writer.write((json.dumps(message, ensure_ascii=False) + "\n").encode())
