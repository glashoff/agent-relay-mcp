"""The Claude Code "voice" instance: one long-lived `claude -p` process in
stream-json mode, locked down to web search/fetch and the relay's MCP tools.

build_command() is the single place where the flags are decided; parse_line()
turns one stream-json output line into simple events. Both are pure and
tested; ClaudeProcess runs the process under asyncio.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

log = logging.getLogger("voice.claude")

RELAY_SERVER = "relay"
RELAY_URL = "http://localhost:8765/mcp"
TOOLS = "WebSearch,WebFetch"
ALLOWED_TOOLS = f"mcp__{RELAY_SERVER},WebSearch,WebFetch"

MODELS = [
    {"id": "", "label": "Default"},
    {"id": "fable", "label": "Fable"},
    {"id": "opus", "label": "Opus"},
    {"id": "sonnet", "label": "Sonnet"},
    {"id": "haiku", "label": "Haiku"},
]


def mcp_config(url: str = RELAY_URL) -> dict:
    return {"mcpServers": {RELAY_SERVER: {"type": "http", "url": url}}}


def build_command(claude: str, mcp_config_file: Path, system_prompt: str, model: str = "") -> list[str]:
    cmd = [
        claude, "-p",
        "--input-format", "stream-json",
        "--output-format", "stream-json",
        "--include-partial-messages", "--verbose",
        # built-in tools: only these two; no Bash, no file tools
        "--tools", TOOLS,
        # MCP: only the relay, nothing from settings or claude.ai
        "--strict-mcp-config", "--mcp-config", str(mcp_config_file),
        # no settings files (user, project, local): no hooks, plugins, permissions from files
        "--setting-sources", "",
        "--allowedTools", ALLOWED_TOOLS,
        # anything not allowed above is denied instead of asked
        "--permission-mode", "dontAsk",
        "--append-system-prompt", system_prompt,
    ]
    if model:
        cmd += ["--model", model]
    return cmd


def build_env(token: str | None, home: str) -> dict[str, str]:
    """A minimal environment: nothing of the service's own is passed on."""
    env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": home,
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "ENABLE_CLAUDEAI_MCP_SERVERS": "false",
        "DISABLE_AUTOUPDATER": "1",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    }
    if token:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = token
    return env


def user_message(text: str) -> dict:
    return {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": text}]}}


def control_request(subtype: str, request_id: str) -> dict:
    return {"type": "control_request", "request_id": request_id, "request": {"subtype": subtype}}


@dataclass
class Event:
    kind: str  # init | text | block_end | message_text | api_error | tool | result | permission | error
    text: str = ""
    data: dict | None = None


def parse_line(line: str) -> list[Event]:
    """One stream-json line -> events. Subagent output (parent_tool_use_id
    set) is ignored: only the top-level conversation is spoken."""
    try:
        obj = json.loads(line)
    except ValueError:
        return []
    if not isinstance(obj, dict):
        return []
    kind = obj.get("type")
    if kind == "system" and obj.get("subtype") == "init":
        return [Event("init", data=obj)]
    if kind == "stream_event":
        if obj.get("parent_tool_use_id"):
            return []
        event = obj.get("event") or {}
        etype = event.get("type")
        if etype == "content_block_delta":
            delta = event.get("delta") or {}
            if delta.get("type") == "text_delta":
                return [Event("text", text=delta.get("text", ""))]
        elif etype == "content_block_start":
            block = event.get("content_block") or {}
            if block.get("type") in ("tool_use", "server_tool_use", "mcp_tool_use"):
                return [Event("tool", text=block.get("name", ""))]
        elif etype == "content_block_stop":
            return [Event("block_end")]
        return []
    if kind == "assistant":
        if obj.get("parent_tool_use_id"):
            return []
        message = obj.get("message") or {}
        content = message.get("content") or []
        text = "".join(c.get("text", "") for c in content
                       if isinstance(c, dict) and c.get("type") == "text")
        if message.get("model") == "<synthetic>" or obj.get("error"):
            # Claude Code's own error text ("Not logged in · Please run /login"),
            # not an answer: reported, not read aloud.
            return [Event("api_error", text=text, data=obj)]
        return [Event("message_text", text=text, data=obj)] if text else []
    if kind == "result":
        return [Event("result", text=str(obj.get("result") or ""), data=obj)]
    if kind == "control_request":
        request = obj.get("request") or {}
        if request.get("subtype") == "can_use_tool":
            return [Event("permission", data=obj)]
        return []
    if kind == "error":
        return [Event("error", text=str(obj.get("message", "")))]
    return []


def deny_response(request_id: str) -> dict:
    return {"type": "control_response", "response": {
        "subtype": "success", "request_id": request_id,
        "response": {"behavior": "deny", "message": "Not allowed in the voice instance."}}}


def relay_status(init: dict) -> str | None:
    """Status of the relay MCP server as reported in the init line."""
    for server in init.get("mcp_servers") or []:
        if isinstance(server, dict) and server.get("name") == RELAY_SERVER:
            return server.get("status")
    return None


class ClaudeProcess:
    """Runs one claude process; on_event(Event) for every event, on_exit(code,
    stderr tail) when it ends. send() writes one stream-json line."""

    def __init__(self, command: list[str], env: dict[str, str], cwd: Path,
                 on_event: Callable[[Event], None], on_exit: Callable[[int, str], None]) -> None:
        self.command, self.env, self.cwd = command, env, cwd
        self.on_event, self.on_exit = on_event, on_exit
        self.proc: asyncio.subprocess.Process | None = None
        self._stderr: list[str] = []
        self._counter = 0
        self.closing = False

    async def start(self) -> None:
        self.cwd.mkdir(parents=True, exist_ok=True)
        log.info("starting Claude Code: %s", " ".join(
            a if len(a) < 80 else a[:60] + "…" for a in self.command))
        self.proc = await asyncio.create_subprocess_exec(
            *self.command, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, cwd=str(self.cwd), env=self.env,
            limit=16 * 1024 * 1024)
        asyncio.create_task(self._read_stdout())
        asyncio.create_task(self._read_stderr())
        await self.send(control_request("initialize", "init"))

    @property
    def alive(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    async def send(self, obj: dict) -> bool:
        if not self.alive or self.proc.stdin is None:
            return False
        try:
            self.proc.stdin.write((json.dumps(obj, ensure_ascii=False) + "\n").encode())
            await self.proc.stdin.drain()
            return True
        except (BrokenPipeError, ConnectionResetError):
            return False

    async def ask(self, text: str) -> bool:
        return await self.send(user_message(text))

    async def interrupt(self) -> None:
        self._counter += 1
        await self.send(control_request("interrupt", f"interrupt-{self._counter}"))

    async def _read_stdout(self) -> None:
        assert self.proc and self.proc.stdout
        while True:
            line = await self.proc.stdout.readline()
            if not line:
                break
            for event in parse_line(line.decode(errors="replace")):
                if event.kind == "permission":
                    # Everything wanted is allowed by flag; anything asked is denied.
                    await self.send(deny_response(str(event.data.get("request_id", ""))))
                try:
                    self.on_event(event)
                except Exception:  # noqa: BLE001 - one bad event must not end the reader
                    log.exception("handling %s failed", event.kind)
        code = await self.proc.wait()
        if not self.closing:
            log.warning("Claude Code exited with %s: %s", code, " | ".join(self._stderr[-5:]))
        self.on_exit(code, "\n".join(self._stderr[-5:]))

    async def _read_stderr(self) -> None:
        assert self.proc and self.proc.stderr
        while True:
            line = await self.proc.stderr.readline()
            if not line:
                break
            text = line.decode(errors="replace").rstrip()
            if text:
                self._stderr = (self._stderr + [text])[-50:]
                log.info("claude: %s", text[:300])

    async def stop(self) -> None:
        self.closing = True
        if not self.alive:
            return
        try:
            self.proc.stdin.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            await asyncio.wait_for(self.proc.wait(), 3)
        except asyncio.TimeoutError:
            self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), 3)
            except asyncio.TimeoutError:
                self.proc.kill()
