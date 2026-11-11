"""The MCP endpoint: seven tools over Streamable HTTP, on a Unix socket only."""


import argparse
import asyncio
import contextlib
import logging
import signal
from pathlib import Path
from typing import Annotated

import uvicorn
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import Field

from . import config as config_module
from . import sockets
from .control import Control
from .relay import Relay
from .targets import RelayError

INSTRUCTIONS = """\
This server relays requests to Claude Code, a coding agent on this computer,
one conversation per project (a folder here, usually with its own dev
container). Claude Code can read and change the project's files and run
commands in it; it works on its own and takes from a few seconds to many
minutes per request. You reach this server only from this computer, on its
Unix socket; you are typically the voice assistant the user talks to.

How to use it:
- When the user names a project vaguely, call list_projects and pick the
  closest match; ask only if it is really unclear.
- Pass the user's request on with send_message, in the user's words but
  complete and unambiguous, including what you already know from the
  conversation. Do not invent requirements.
- send_message returns at once. Tell the user that Claude Code is working on it.
- Additions, corrections and follow-ups go through send_message too. Claude
  Code stays open in the project's conversation between messages, so they are
  passed straight in, also while it is still working, and it takes them into
  account at its next step. Only while it is being started is a message
  queued for a moment; the reply says what happened.
- Claude Code may answer and go on working in the background (a subagent, a
  long command). get_updates then shows its reply so far and that it is still
  working; the request is finished once the background work is done too.
- Do not call get_updates on your own in a loop; call it when the user asks
  how it is going, when they come back to the topic, or when you are told
  that a request has finished.
- Report results the way the user needs them. When speaking, give a short
  summary in plain words without file paths, code or markup, and offer
  details. Quote Claude Code's reply verbatim only when asked.
- Questions Claude Code asks in its reply go to the user; pass the user's
  answer back with send_message.
- Projects are locked unless the user has approved them at the laptop (the
  GNOME menu or agent-relay-ctl). A message to a locked project waits for that
  approval and runs by itself afterwards; the reply gives a number. Tell the
  user the number, so they can match it there. Approvals cannot be given
  through this server; never try to work around a locked project.
- To run a project with a dev container directly on the computer instead, pass
  run_on_host=true to send_message, but only when the user asks for exactly that.
  It only asks: the user confirms it at the laptop, with a full-access warning.
- When the user says they are done with a project (or with everything), end
  the approval with release_access. If Claude Code is still working there,
  tell the user and ask whether to wait or to stop it first.
- Host actions are fixed commands the user has set up to run on the computer
  itself, outside the dev container (such as a deploy). Ask for one with
  request_host_action only when the user explicitly wants it. It only asks:
  the user approves this one run at the laptop, and nothing runs before.
  Tell the user the number from the reply; get_host_action gives its state.
  A host action is not a message to Claude Code and does not touch its
  conversation. Never try to work around a denied or waiting host action.
"""

ProjectName = Annotated[str, Field(description="Project name as listed by list_projects.")]


def build(config: config_module.Config) -> tuple[MCPServer, Relay]:
    relay = Relay(config)
    instructions = INSTRUCTIONS
    if config.instance_name:
        instructions += (f"\nThis server works on the user's {config.instance_name}; its "
                         "projects are the ones there.\n")

    @contextlib.asynccontextmanager
    async def lifespan(_server):
        # Until a control interface runs, nothing could revoke a grant, so
        # none counts (fail closed); the interface lifts this when it is up.
        relay.set_control(False, "the control interface is starting")
        events = None
        if config.events_socket:
            # Read-only: the events of the control layer, no commands.
            events = sockets.EventsSocket(
                Path(config_module.expand_socket_path(config.events_socket)),
                Control(relay).events)
            await events.start()
        bus = await start_dbus(config, relay)
        if bus is None:
            logging.getLogger("agent_relay_mcp").error(
                "without a control interface every project stays locked, also those "
                "approved before")
        relay.start_background()
        try:
            yield None
        finally:
            if bus:
                bus.stop()
            if events:
                await events.stop()
            relay.close()

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
        "Claude Code stays open in the conversation between messages: the message is "
        "passed straight in, also while Claude Code is still working (it takes it into "
        "account at its next step); only while Claude Code is being started is it queued "
        "for a moment. The reply says which happened. "
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
        run_on_host: Annotated[bool, Field(description=(
            "Only when the user explicitly asks for it: true asks to run a project that "
            "has a dev container directly on the computer instead. This is only a "
            "request: it waits for the user's confirmation at the laptop, with a "
            "full-access warning, and nothing runs on the host before that."))] = False,
    ) -> str:
        # A boolean, not a string with choices: claude.ai left that out of
        # the tool's schema.
        return await guarded(relay.send_message(project, message, new_conversation,
                                                wait_seconds, "host" if run_on_host else ""))

    @server.tool(description=(
        "Gets the state of the latest request to Claude Code in a project: still working "
        "(with its recent steps, and its reply so far if it answered and works on in the "
        "background), or finished with its final reply. Returns at once by default; "
        f"wait_seconds (at most {max_wait}) waits for the reply first."))
    async def get_updates(
        project: ProjectName,
        wait_seconds: Annotated[int, Field(ge=0, le=60, description=(
            f"Seconds to wait for the reply if it is not there yet, at most {max_wait}."))] = 0,
    ) -> str:
        return await guarded(relay.get_updates(project, wait_seconds))

    @server.tool(description="Stops the request Claude Code is working on in a project "
                             "(or ends Claude Code there if it only waits for the next "
                             "message). The conversation is kept, so it can be continued.")
    async def stop(project: ProjectName) -> str:
        return await guarded(relay.stop(project))

    @server.tool(description=(
        "Ends the user's approval for a project, or for all projects, when the user "
        "says they are done; the project is locked again. Only possible while Claude "
        "Code is not working there and no message is waiting. It cannot give "
        "approvals: those are only given at the user's laptop."))
    async def release_access(
        project: Annotated[str, Field(description=(
            "Project name as listed by list_projects; leave empty with all_projects."))] = "",
        all_projects: Annotated[bool, Field(description=(
            "End every approval, for all projects."))] = False,
    ) -> str:
        return await guarded(relay.release_access(project, all_projects))

    @server.tool(description=(
        "Only on the user's explicit request: asks to run a host action, a fixed command "
        "the user has set up for a project (such as a deploy), on the computer itself, "
        "outside the dev container. This only asks: the user approves this one run at "
        "the laptop, and nothing runs before. The reply gives a number; tell it to the "
        "user. Unknown actions are refused, naming the ones there are."))
    async def request_host_action(
        project: ProjectName,
        action: Annotated[str, Field(description="The name of the host action, e.g. deploy.")],
        note: Annotated[str, Field(description=(
            "A short note for the user, shown with the request at the laptop: why it "
            "is needed now."))] = "",
    ) -> str:
        return await guarded(relay.request_host_action(project, action, note))

    @server.tool(description=(
        "Gets the state of a host action asked for with request_host_action, by its "
        "number: waiting for approval, denied, or expired."))
    async def get_host_action(
        number: Annotated[int, Field(description="The number from request_host_action.")],
    ) -> str:
        return await guarded(relay.get_host_action(number))

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
    bus = DBusControl(Control(relay), on_state=relay.set_control)
    await bus.start()           # keeps trying, and reconnects when the bus goes away
    return bus


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="agent-relay-mcp",
        description="MCP server that relays requests to Claude Code in your projects.")
    parser.add_argument("--config", type=Path, help=f"config file "
                        f"(default: {config_module.DEFAULT_CONFIG_FILE})")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    config = config_module.load(args.config)

    server, _ = build(config)
    app = http_app(server, config)
    logging.getLogger("agent_relay_mcp").info("projects in %s", config.projects_dir)
    try:
        asyncio.run(serve(app, config, log_level=server.settings.log_level.lower()))
    except sockets.SocketError as error:
        raise SystemExit(f"agent-relay-mcp: {error}") from None


def http_app(server: MCPServer, config: config_module.Config):
    """The MCP endpoint as an ASGI app. It is served only on the Unix socket,
    but still checks Host and Origin as on loopback: clients send a local
    Host (the voice container's forwarder sends "localhost:8765"), and a web
    page cannot get a request through with its own."""
    hosts = ["127.0.0.1:*", "localhost:*", "[::1]:*", "127.0.0.1", "localhost", "[::1]"]
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True, allowed_hosts=hosts,
        allowed_origins=["http://127.0.0.1:*", "http://localhost:*"])
    return server.streamable_http_app(
        streamable_http_path=config.path, json_response=True, stateless_http=True,
        transport_security=security)


class _Server(uvicorn.Server):
    """A uvicorn server that leaves signals to serve(): uvicorn raises the
    signal again when it is done, which would end the process before the
    app's lifespan has shut down."""

    def capture_signals(self):
        return contextlib.nullcontext()


async def serve(app, config: config_module.Config, *, log_level: str = "info",
                stop: asyncio.Event | None = None, signals: bool = True) -> None:
    """Serves app on mcp_socket, the only way in: there is no TCP listener.
    The app's lifespan (the MCP session manager and with it the relay's) runs
    here, around the server, which runs without one. Ends on SIGINT/SIGTERM
    or when stop is set."""
    log = logging.getLogger("agent_relay_mcp")
    if not config.mcp_socket:
        raise sockets.SocketError("mcp_socket is not set; it is the only way to reach "
                                  "the relay")
    unix_path = Path(config_module.expand_socket_path(config.mcp_socket))
    unix_sock = sockets.bind(unix_path)     # before anything starts
    unix_inode = sockets.inode(unix_path)
    server = _Server(uvicorn.Config(app, lifespan="off", log_level=log_level))
    failed = False

    def shut_down(force: bool = False) -> None:
        if force and server.should_exit:
            server.force_exit = True
        server.should_exit = True

    async def wait_for_stop() -> None:
        await stop.wait()
        shut_down()

    loop = asyncio.get_running_loop()
    if signals:
        loop.add_signal_handler(signal.SIGTERM, shut_down)
        loop.add_signal_handler(signal.SIGINT, shut_down, True)
    waiter = asyncio.ensure_future(wait_for_stop()) if stop else None
    try:
        async with app.router.lifespan_context(app):
            log.info("MCP endpoint on %s (path %s); no TCP listener", unix_path, config.path)
            try:
                await server.serve(sockets=[unix_sock])
            except SystemExit:          # uvicorn could not start it
                failed = True
    finally:
        if waiter:
            waiter.cancel()
        if signals:
            loop.remove_signal_handler(signal.SIGTERM)
            loop.remove_signal_handler(signal.SIGINT)
        unix_sock.close()
        sockets.unlink(unix_path, unix_inode)
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
