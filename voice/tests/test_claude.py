"""The locked-down flags, stream-json parsing, and the process wrapper with a
fake `claude`."""

import asyncio
import json
from pathlib import Path

from agent_relay_voice import claude

FAKE = str(Path(__file__).with_name("fake_claude.py"))


def test_exact_flags():
    cmd = claude.build_command("claude", Path("/tmp/x/mcp.json"), "PROMPT")
    assert cmd == [
        "claude", "-p",
        "--input-format", "stream-json",
        "--output-format", "stream-json",
        "--include-partial-messages", "--verbose",
        "--tools", "WebSearch,WebFetch",
        "--strict-mcp-config", "--mcp-config", "/tmp/x/mcp.json",
        "--setting-sources", "",
        "--allowedTools", "mcp__relay,WebSearch,WebFetch",
        "--permission-mode", "dontAsk",
        "--append-system-prompt", "PROMPT",
    ]
    assert claude.build_command("claude", Path("m.json"), "P", "haiku")[-2:] == ["--model", "haiku"]
    assert "--model" not in claude.build_command("claude", Path("m.json"), "P", "")


def test_mcp_config_names_only_the_relay():
    assert claude.mcp_config() == {
        "mcpServers": {"relay": {"type": "http", "url": "http://localhost:8765/mcp"}}}


def test_env_is_minimal():
    env = claude.build_env("tok", "/home/voice")
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "tok"
    assert env["ENABLE_CLAUDEAI_MCP_SERVERS"] == "false"
    assert env["HOME"] == "/home/voice"
    assert set(env) == {"PATH", "HOME", "LANG", "ENABLE_CLAUDEAI_MCP_SERVERS", "DISABLE_AUTOUPDATER",
                        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "CLAUDE_CODE_OAUTH_TOKEN"}
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in claude.build_env(None, "/h")


def line(obj):
    return json.dumps(obj)


def test_parse_lines():
    assert claude.parse_line("not json") == []
    assert claude.parse_line("[1]") == []
    ev = claude.parse_line(line({"type": "system", "subtype": "init", "session_id": "s"}))
    assert ev[0].kind == "init"
    ev = claude.parse_line(line({"type": "stream_event", "parent_tool_use_id": None, "event": {
        "type": "content_block_delta", "delta": {"type": "text_delta", "text": "Hal"}}}))
    assert [(e.kind, e.text) for e in ev] == [("text", "Hal")]
    # subagent output is ignored
    assert claude.parse_line(line({"type": "stream_event", "parent_tool_use_id": "t", "event": {
        "type": "content_block_delta", "delta": {"type": "text_delta", "text": "x"}}})) == []
    # thinking deltas are not spoken
    assert claude.parse_line(line({"type": "stream_event", "event": {
        "type": "content_block_delta", "delta": {"type": "thinking_delta", "thinking": "x"}}})) == []
    ev = claude.parse_line(line({"type": "stream_event", "event": {
        "type": "content_block_start", "content_block": {"type": "tool_use", "name": "WebSearch"}}}))
    assert [(e.kind, e.text) for e in ev] == [("tool", "WebSearch")]
    ev = claude.parse_line(line({"type": "stream_event", "event": {"type": "content_block_stop"}}))
    assert ev[0].kind == "block_end"
    ev = claude.parse_line(line({"type": "assistant", "parent_tool_use_id": None, "message": {
        "content": [{"type": "text", "text": "A"}, {"type": "tool_use"}, {"type": "text", "text": "B"}]}}))
    assert [(e.kind, e.text) for e in ev] == [("message_text", "AB")]
    ev = claude.parse_line(line({"type": "assistant", "error": "authentication_failed", "message": {
        "model": "<synthetic>", "content": [{"type": "text", "text": "Not logged in"}]}}))
    assert [(e.kind, e.text) for e in ev] == [("api_error", "Not logged in")]
    ev = claude.parse_line(line({"type": "result", "result": "fertig", "is_error": False}))
    assert [(e.kind, e.text) for e in ev] == [("result", "fertig")]
    ev = claude.parse_line(line({"type": "control_request", "request_id": "r1",
                                 "request": {"subtype": "can_use_tool", "tool_name": "Bash"}}))
    assert ev[0].kind == "permission"


def test_relay_status():
    assert claude.relay_status({"mcp_servers": [{"name": "relay", "status": "connected"}]}) == "connected"
    assert claude.relay_status({}) is None


def test_process_with_fake_claude(tmp_path):
    async def run():
        events, exits = [], []
        cmd = claude.build_command(FAKE, tmp_path / "mcp.json", "PROMPT", "sonnet")
        proc = claude.ClaudeProcess(cmd, claude.build_env("secret-token", str(tmp_path)),
                                    tmp_path / "work", events.append,
                                    lambda code, err: exits.append(code))
        await proc.start()
        assert await proc.ask("Hallo Welt")
        for _ in range(100):
            if any(e.kind == "result" for e in events):
                break
            await asyncio.sleep(0.05)
        text = "".join(e.text for e in events if e.kind == "text")
        assert text == "Du hast gesagt: Hallo Welt. Das ist der zweite Satz! Und ein dritter?"
        assert [e.kind for e in events][0] == "init"
        await proc.stop()
        await asyncio.sleep(0.1)
        return exits

    exits = asyncio.run(run())
    assert exits == [0]
    log = [json.loads(x) for x in (tmp_path / "work" / "fake-claude.log").read_text().splitlines()]
    argv = next(x["data"] for x in log if x["kind"] == "argv")
    # the flags arrive exactly, including the empty --setting-sources value
    assert argv[1:] == claude.build_command("claude", tmp_path / "mcp.json", "PROMPT", "sonnet")[1:]
    i = argv.index("--setting-sources")
    assert argv[i + 1] == ""
    env = next(x["data"] for x in log if x["kind"] == "env")
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "secret-token"
    assert env["ENABLE_CLAUDEAI_MCP_SERVERS"] == "false"
    assert "PULSE_SERVER" not in env and "HF_HOME" not in env
    stdin = [json.loads(x["data"]) for x in log if x["kind"] == "stdin"]
    assert stdin[0] == {"type": "control_request", "request_id": "init",
                        "request": {"subtype": "initialize"}}
    assert stdin[1] == {"type": "user", "message": {"role": "user", "content": [
        {"type": "text", "text": "Hallo Welt"}]}}
