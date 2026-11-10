"""The MCP tools as a client sees them."""

import asyncio
import sys
from pathlib import Path

from agent_relay_mcp.config import Config
from agent_relay_mcp.server import build

FAKE = Path(__file__).with_name("fake_claude.py")


def make(tmp_path):
    (tmp_path / "projects/web/.devcontainer").mkdir(parents=True)
    (tmp_path / "projects/web/.devcontainer/devcontainer.json").write_text("{}")
    config = Config(projects_dir=tmp_path / "projects", allow_host_projects=True,
                    claude_command=f"{sys.executable} {FAKE}", state_dir=tmp_path / "state",
                    keep_awake_command="")
    return build(config)


def schemas(server):
    tools = asyncio.run(server.list_tools())
    return {t.name: t.input_schema for t in tools}


def test_the_tool_schemas_use_only_plain_types(tmp_path):
    # claude.ai left out a parameter whose schema was a string with choices
    # (enum with ""): every parameter is a plain string, boolean or integer.
    server, _ = make(tmp_path)
    for name, schema in schemas(server).items():
        for prop, spec in schema.get("properties", {}).items():
            assert spec.get("type") in ("string", "boolean", "integer"), (name, prop, spec)
            assert "enum" not in spec and "anyOf" not in spec, (name, prop, spec)


def test_send_message_offers_asking_for_the_host(tmp_path):
    server, _ = make(tmp_path)
    spec = schemas(server)["send_message"]["properties"]["run_on_host"]
    assert spec["type"] == "boolean" and spec["default"] is False
    assert "only a request" in spec["description"]


def test_run_on_host_through_the_tool_makes_a_host_request(tmp_path):
    server, relay = make(tmp_path)

    async def scenario():
        result = await server.call_tool("send_message", {
            "project": "web", "message": "deploy from the host", "run_on_host": True})
        text = result.content[0].text
        assert "on the host" in text and "waits for approval" in text
        (request,) = relay.access.requests.values()
        assert request.kind == "host" and request.full_access
        assert not relay.access.grants
    asyncio.run(scenario())
