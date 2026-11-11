"""Talking to the relay through its Unix sockets (mounted from
$XDG_RUNTIME_DIR/agent-relay/ on the host):

- events.sock: JSON lines, the same objects as the relay's D-Bus Event signal
  ({"type": "run_ended", "project": ..., ...}); read only. A missing socket or
  a dropped connection is retried with backoff.
- mcp.sock: the relay's MCP app. The service itself only calls list_projects
  (project names become Whisper hotwords); Claude Code reaches it through the
  socat forwarder on localhost:8765.
"""

from __future__ import annotations

import asyncio
import http.client
import json
import logging
import re
import socket
from pathlib import Path
from typing import Callable

log = logging.getLogger("voice.relay")


def notice_for(event: dict, wanted: list[str]) -> str | None:
    """Short German notice for the instance, or None if the event is not one
    to announce."""
    kind = event.get("type")
    if kind not in wanted:
        return None
    project = str(event.get("project") or "").strip() or "unbekannt"
    if kind == "run_ended":
        if event.get("phase") == "failed":
            return f"[Ereignis: Der Auftrag in Projekt {project} ist abgebrochen oder fehlgeschlagen.]"
        return f"[Ereignis: Der Auftrag in Projekt {project} ist fertig.]"
    if kind == "request_added":
        number = event.get("number")
        number_text = f", Nummer {number}" if number is not None else ""
        host = " Er würde mit vollem Zugriff auf den Computer laufen." if event.get("full_access") else ""
        return (f"[Ereignis: Eine Nachricht an Projekt {project} wartet am Laptop auf "
                f"Freigabe{number_text}.{host}]")
    if kind == "request_expired":
        return f"[Ereignis: Eine Freigabe-Anfrage für Projekt {project} ist abgelaufen.]"
    if kind == "run_started":
        return f"[Ereignis: Claude Code hat in Projekt {project} angefangen.]"
    return None


def spoken_notice(notice: str) -> str:
    """What to say when no Claude instance is there to turn a notice into words."""
    return notice.removeprefix("[Ereignis: ").removesuffix("]")


async def read_events(path: Path, on_event: Callable[[dict], None],
                      on_connected: Callable[[bool], None] = lambda c: None,
                      max_delay: float = 30.0) -> None:
    """Runs forever: connect, read JSON lines, reconnect when the socket goes away."""
    delay = 1.0
    logged_missing = False
    while True:
        try:
            reader, writer = await asyncio.open_unix_connection(str(path), limit=1024 * 1024)
        except (FileNotFoundError, ConnectionRefusedError, PermissionError, OSError) as error:
            if not logged_missing:
                log.info("relay events not available at %s (%s); retrying", path, error)
                logged_missing = True
            on_connected(False)
            await asyncio.sleep(delay)
            delay = min(delay * 2, max_delay)
            continue
        log.info("connected to relay events at %s", path)
        logged_missing = False
        delay = 1.0
        on_connected(True)
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if isinstance(event, dict):
                    on_event(event)
        except (ConnectionError, ValueError, asyncio.LimitOverrunError) as error:
            log.info("relay events connection lost: %s", error)
        finally:
            writer.close()
        log.info("relay events disconnected; reconnecting")
        on_connected(False)
        await asyncio.sleep(delay)


class _UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path: str, host: str, timeout: float) -> None:
        super().__init__(host, timeout=timeout)
        self.unix_path = path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self.unix_path)
        self.sock = sock


def _parse_mcp_body(body: bytes, content_type: str) -> dict | None:
    """The response is JSON or an SSE stream of JSON-RPC messages."""
    text = body.decode("utf-8", "replace")
    if "text/event-stream" in content_type:
        result = None
        for line in text.splitlines():
            if line.startswith("data:"):
                try:
                    msg = json.loads(line[5:].strip())
                except ValueError:
                    continue
                if isinstance(msg, dict) and ("result" in msg or "error" in msg):
                    result = msg
        return result
    try:
        return json.loads(text) if text.strip() else None
    except ValueError:
        return None


def call_tool(socket_path: Path, tool: str, arguments: dict | None = None,
              host: str = "localhost:8765", path: str = "/mcp", timeout: float = 20.0) -> str:
    """Minimal MCP streamable-HTTP client over the Unix socket: initialize,
    initialized, tools/call. Returns the text content of the result."""
    headers = {"Host": host, "Content-Type": "application/json",
               "Accept": "application/json, text/event-stream"}

    def post(payload: dict, session: str | None) -> tuple[dict | None, str | None]:
        conn = _UnixHTTPConnection(str(socket_path), host, timeout)
        try:
            h = dict(headers)
            if session:
                h["Mcp-Session-Id"] = session
            conn.request("POST", path, body=json.dumps(payload), headers=h)
            resp = conn.getresponse()
            body = resp.read()
            if resp.status >= 400:
                raise RuntimeError(f"HTTP {resp.status}: {body[:200]!r}")
            return (_parse_mcp_body(body, resp.getheader("Content-Type", "")),
                    resp.getheader("Mcp-Session-Id") or session)
        finally:
            conn.close()

    init, session = post({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {},
        "clientInfo": {"name": "agent-relay-voice", "version": "1"}}}, None)
    if not init or "result" not in init:
        raise RuntimeError(f"initialize failed: {init}")
    post({"jsonrpc": "2.0", "method": "notifications/initialized"}, session)
    reply, _ = post({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                     "params": {"name": tool, "arguments": arguments or {}}}, session)
    if not reply or "result" not in reply:
        raise RuntimeError(f"{tool} failed: {reply}")
    content = reply["result"].get("content") or []
    return "\n".join(c.get("text", "") for c in content if isinstance(c, dict))


def project_names(list_projects_text: str) -> list[str]:
    """Names from list_projects' lines "- name (where; ...)"."""
    return re.findall(r"(?m)^- (.+?) \(", list_projects_text)
