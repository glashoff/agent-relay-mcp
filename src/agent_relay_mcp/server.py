"""The MCP endpoint: four tools over Streamable HTTP."""


import argparse
import contextlib
import logging
from pathlib import Path
from typing import Annotated

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import Field

from . import config as config_module
from .control import Control
from .relay import Relay
from .targets import RelayError

INSTRUCTIONS = """\
This connector relays requests to Claude Code, a coding agent running on the
user's own computer, one conversation per project (a folder there, usually with
its own dev container). Claude Code can read and change the project's files and
run commands in it; it works on its own and takes from a few seconds to many
minutes per request.

How to use it:
- When the user names a project vaguely, call list_projects and pick the
  closest match; ask only if it is really unclear.
- Pass the user's request on with send_message, in the user's words but
  complete and unambiguous, including what you already know from the
  conversation. Do not invent requirements.
- send_message returns at once. Tell the user that Claude Code is working on it.
- Additions and corrections for a request still in progress go through
  send_message too. They are passed into the running request, or queued and
  sent right after it; the reply says which. Tell the user.
  Do not call get_updates on your own in a loop; call it when the user asks
  how it is going, or when they come back to the topic.
- Report results the way the user needs them. In a voice conversation, give a
  short spoken summary in plain words without file paths, code or markup, and
  offer details. Quote Claude Code's reply verbatim only when asked.
- Questions Claude Code asks in its reply go to the user; pass the user's
  answer back with send_message.
- Projects are locked unless the user has approved them at their laptop. A
  message to a locked project waits for that approval and runs by itself
  afterwards; the reply gives a number. Tell the user the number, so they can
  match it at the laptop. Approvals cannot be given through this connector;
  never try to work around a locked project.
"""

ProjectName = Annotated[str, Field(description="Project name as listed by list_projects.")]


def build(config: config_module.Config) -> tuple[MCPServer, Relay]:
    relay = Relay(config)
    instructions = INSTRUCTIONS
    if config.instance_name:
        instructions += (f"\nThis connector works on the user's {config.instance_name}; its "
                         "projects are the ones there.\n")

    @contextlib.asynccontextmanager
    async def lifespan(_server):
        relay.start_background()
        bus = await start_dbus(config, relay)
        try:
            yield None
        finally:
            if bus:
                bus.stop()

    server = MCPServer("agent-relay-mcp", instructions=instructions, version="0.1.0",
                       lifespan=lifespan)
    max_wait = config.max_wait_seconds

    async def guarded(call):
        try:
            return await call
        except RelayError as error:
            raise ToolError(str(error)) from error

    @server.tool(description="Lists the projects on the user's computer that Claude Code "
                             "can work in, with whether something is running or a reply "
                             "is waiting.")
    async def list_projects() -> str:
        return await guarded(relay.list_projects())

    @server.tool(description=(
        "Sends a message to Claude Code in a project: a new task, a follow-up, or the "
        "answer to a question Claude Code asked. Continues that project's conversation "
        "unless new_conversation is true. Starts the project's dev container if needed. "
        "While Claude Code is working, the message is passed into the running request, "
        "which takes it into account at its next step; if that is not possible, it is "
        "queued and sent as the next request once the current one has finished. The "
        "reply says which happened. "
        "Returns immediately by default; most requests take minutes, so tell the user it "
        "is in progress and fetch the result later with get_updates. Only for requests "
        f"that are obviously quick, set wait_seconds (at most {max_wait}) to wait for "
        "the reply right away."))
    async def send_message(
        project: ProjectName,
        message: Annotated[str, Field(description="The request for Claude Code, self-contained.")],
        new_conversation: Annotated[bool, Field(description=(
            "Start a fresh conversation instead of continuing the previous one, e.g. "
            "for an unrelated task."))] = False,
        wait_seconds: Annotated[int, Field(ge=0, le=60, description=(
            f"Seconds to wait for the reply before returning, at most {max_wait}. "
            "Default 0: return at once."))] = 0,
    ) -> str:
        return await guarded(relay.send_message(project, message, new_conversation, wait_seconds))

    @server.tool(description=(
        "Gets the state of the latest request to Claude Code in a project: still working "
        "(with its recent steps), or finished with its final reply. Returns at once by "
        f"default; wait_seconds (at most {max_wait}) waits for the reply first."))
    async def get_updates(
        project: ProjectName,
        wait_seconds: Annotated[int, Field(ge=0, le=60, description=(
            f"Seconds to wait for the reply if it is not there yet, at most {max_wait}."))] = 0,
    ) -> str:
        return await guarded(relay.get_updates(project, wait_seconds))

    @server.tool(description="Stops the request Claude Code is working on in a project. "
                             "The conversation is kept, so it can be continued.")
    async def stop(project: ProjectName) -> str:
        return await guarded(relay.stop(project))

    return server, relay


async def start_dbus(config: config_module.Config, relay: Relay):
    """The control interface on the session bus, if configured and possible.
    Without it nothing can be approved, so everything stays locked."""
    log = logging.getLogger("agent_relay_mcp")
    if not config.control_dbus:
        log.warning("control_dbus is off: projects can only be approved through another "
                    "control interface")
        return None
    try:
        from .dbus_control import DBusControl
    except ImportError:
        log.error("dbus-fast is not installed (pip extra 'desktop'): nothing can be "
                  "approved, every project stays locked")
        return None
    bus = DBusControl(Control(relay))
    try:
        await bus.start()
    except Exception as error:
        log.error("cannot offer the control interface on the session bus (%s): nothing can "
                  "be approved, every project stays locked", error)
        return None
    return bus


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="agent-relay-mcp",
        description="MCP server that relays requests to Claude Code in your projects.")
    parser.add_argument("--config", type=Path, help=f"config file "
                        f"(default: {config_module.DEFAULT_CONFIG_FILE})")
    args = parser.parse_args()
    config = config_module.load(args.config)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    server, _ = build(config)
    hosts = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
    for host in config.public_hosts:
        hosts += [host, f"{host}:*"]
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True, allowed_hosts=hosts,
        allowed_origins=[f"https://{h}" for h in config.public_hosts]
        + ["http://127.0.0.1:*", "http://localhost:*"])
    logging.getLogger("agent_relay_mcp").info(
        "listening on %s:%d%s, projects in %s", config.listen_host, config.listen_port,
        config.path, config.projects_dir)
    server.run("streamable-http", host=config.listen_host, port=config.listen_port,
               streamable_http_path=config.path, json_response=True, stateless_http=True,
               transport_security=security)


if __name__ == "__main__":
    main()
