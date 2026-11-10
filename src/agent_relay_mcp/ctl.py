"""agent-relay-ctl: approvals for the relay from a terminal, over D-Bus.

    agent-relay-ctl                       state: grants, requests, projects
    agent-relay-ctl grant web --for 2h    approve a project (or --all)
    agent-relay-ctl approve 47            approve the request with number 47
    agent-relay-ctl deny 47
    agent-relay-ctl revoke web [--stop]   end a grant (or --all)
    agent-relay-ctl stop web [--lock]     stop what runs there (and end its grants)
    agent-relay-ctl keep-awake running    off, running or granted; --lid too
    agent-relay-ctl watch                 print events as they come

Grants and approvals ask for a confirmation typed on the terminal, so they do
not work from a process without one.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time

from .dbus_control import INTERFACE, NAME, PATH

DURATION = re.compile(r"^(?:(\d+)h)?(?:(\d+)m(?:in)?)?$")


def minutes(text: str) -> int:
    """'90', '90m', '2h', '1h30m' as minutes."""
    text = text.strip().lower()
    if text.isdigit():
        value = int(text)
    else:
        match = DURATION.match(text)
        if not text or not match or not any(match.groups()):
            raise argparse.ArgumentTypeError(f"not a duration: {text!r} (e.g. 30m, 2h, 1h30m)")
        value = int(match.group(1) or 0) * 60 + int(match.group(2) or 0)
    if value < 1:
        raise argparse.ArgumentTypeError("the duration must be at least one minute")
    return value


def until(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds >= 3600:
        return f"{seconds // 3600} h {seconds % 3600 // 60} min"
    return f"{seconds // 60} min"


class Failure(Exception):
    pass


class Client:
    def __init__(self):
        self.bus = None

    async def connect(self):
        from dbus_fast import BusType
        from dbus_fast.aio import MessageBus
        try:
            self.bus = await MessageBus(bus_type=BusType.SESSION).connect()
        except Exception as error:
            raise Failure(f"Cannot reach the session bus: {error}") from error
        return self

    async def call(self, member: str, signature: str = "", body: list | None = None):
        from dbus_fast import Message, MessageType
        reply = await self.bus.call(Message(destination=NAME, path=PATH, interface=INTERFACE,
                                            member=member, signature=signature,
                                            body=body or []))
        if reply.message_type == MessageType.ERROR:
            text = reply.body[0] if reply.body else reply.error_name
            if reply.error_name == "org.freedesktop.DBus.Error.ServiceUnknown":
                text = "The relay is not running (or runs without its D-Bus interface)."
            raise Failure(text)
        return reply.body[0] if reply.body else None

    async def state(self) -> dict:
        return json.loads(await self.call("GetState"))


def confirm(question: str, answer: str) -> None:
    """Asks on the terminal itself, not on stdin, so a pipe cannot answer."""
    try:
        with open("/dev/tty", "r+") as tty:
            tty.write(f"{question}\nType '{answer}' to confirm: ")
            tty.flush()
            reply = tty.readline().strip()
    except OSError as error:
        raise Failure("Grants need a confirmation typed on a terminal, and there is none "
                      f"({error}).") from error
    if reply.lower() != answer:
        raise Failure("Not confirmed; nothing was changed.")


# --- printing ------------------------------------------------------------------------

def show_state(state: dict) -> None:
    now = time.time()
    where = f" ({state['instance']})" if state.get("instance") else ""
    print(f"Relay{where}")
    grants = state["grants"]
    print("\nGrants:" if grants else "\nGrants: none, every project is locked.")
    for g in grants:
        target = "all projects" + (" incl. host projects" if g["include_host_projects"]
                                   else "") if g["target"] == "*" else g["target"]
        container = any(p["name"] == g["target"] and p["dev_container"]
                        for p in state["projects"])
        kind = " on the host" if g["kind"] == "host" and container else ""
        flags = []
        if g["full_access"]:
            flags.append("FULL ACCESS")
        if g["end_on_lock"]:
            flags.append("ends on screen lock")
        extra = f" [{', '.join(flags)}]" if flags else ""
        print(f"  {g['id']}  {target}{kind}: {until(g['expires'] - now)} left, ends after "
              f"{until(g['idle_ends'] - now)} more without use{extra}")
    requests = state["requests"]
    print("\nWaiting for approval:" if requests else "\nWaiting for approval: nothing.")
    for r in requests:
        if r.get("kind") == "host":
            warning = "  [asks to run ON THE HOST: FULL ACCESS]"
        else:
            warning = "  [no dev container: FULL ACCESS]" if r["full_access"] else ""
        print(f"  number {r['number']}  {r['project']}, {until(now - r['created'])} ago, "
              f"expires in {until(r['expires'] - now)}{warning}")
        for message in r["messages"]:
            text = " ".join(message.split())
            print(f"      > {text[:300]}{'…' if len(text) > 300 else ''}")
    for s in state["switches"]:
        where_to = "on the host" if s["kind"] == "host" else "in the dev container"
        print(f"\nSwitch waiting: {s['project']} to run {where_to} once idle "
              f"({s['held']} message(s) held), gives up in {until(s['expires'] - now)}")
    k = state.get("keep_awake")
    if k:
        lid = ", also with the lid closed" if k["lid"] else ""
        held = "now holding" if k["active"] else "not holding now"
        reason = f" ({k['reason']})" if k["reason"] else ""
        print(f"\nKeep awake: {k['mode']}{lid}; {held}{reason}")
    print("\nProjects:")
    for p in state["projects"]:
        kind = {"host": "on the host", "container": "in the dev container"}.get(p["kind"])
        status = f"approved, {kind}" if p["grant"] else "locked"
        busy = ", working" if p["busy"] else ""
        container = "" if p["dev_container"] else "  [no dev container]"
        print(f"  {p['name']}: {status}{busy}{container}")


def describe_grant(result: dict) -> str:
    if "switch" in result:
        s = result["switch"]
        return f"{s['waiting_for']} The switch happens once it is idle."
    g = result["grant"]
    text = f"Granted {g['id']}: {until(g['seconds_left'])} left"
    if g.get("cut_to_limit"):
        text += " (cut to the upper limit)"
    return text + "."


# --- commands ------------------------------------------------------------------------

async def find_request(client: Client, which: str) -> dict:
    state = await client.state()
    for r in state["requests"]:
        if which in (r["id"], str(r["number"])):
            return r
    raise Failure(f"No open request {which!r}.")


async def find_grant(client: Client, which: str) -> str:
    state = await client.state()
    ids = [g["id"] for g in state["grants"] if which in (g["id"], g["target"])]
    if not ids:
        raise Failure(f"No grant {which!r}.")
    return ids[0]


async def command(args, client: Client) -> None:
    if args.command in (None, "status"):
        state = await client.state()
        if args.json:
            print(json.dumps(state, indent=2))
        else:
            show_state(state)
        return
    if args.command == "grant":
        state = await client.state()
        target = "*" if args.all else args.project
        if target is None:
            raise Failure("Name a project, or --all.")
        warnings = state["warnings"]
        kind = "host" if args.on_host else ""
        full = args.on_host or args.include_host_projects
        if target != "*":
            project = next((p for p in state["projects"] if p["name"] == target), None)
            if project is None:
                raise Failure(f"No project named {target!r}.")
            full = full or not project["dev_container"]
            warning = warnings["on_host"] if args.on_host else warnings["host_project"]
        else:
            warning = warnings["host_project"]
        what = "all projects" if target == "*" else target
        if args.on_host:
            what += " on the host"
        if args.include_host_projects:
            what += ", including host projects"
        question = f"Grant {what} for {until(args.minutes * 60)}?"
        if full:
            confirm(f"{question}\nWARNING: {warning}", "full access")
        else:
            confirm(question, "yes")
        options = {"target": target, "kind": kind, "minutes": args.minutes,
                   "include_host_projects": args.include_host_projects,
                   "full_access_ok": full, "when_idle": args.when_idle}
        if args.end_on_lock is not None:
            options["end_on_lock"] = args.end_on_lock
        print(describe_grant(json.loads(await client.call("Grant", "s",
                                                          [json.dumps(options)]))))
        return
    if args.command == "approve":
        request = await find_request(client, args.request)
        state = await client.state()
        container = any(p["name"] == request["project"] and p["dev_container"]
                        for p in state["projects"])
        # A request that asks for the host runs there unless --in-container.
        on_host = container and not args.in_container and (
            args.on_host or request.get("kind") == "host")
        kind = "host" if on_host else ("container" if container else "")
        full = on_host or not container
        warning = state["warnings"]["on_host" if on_host else "host_project"]
        messages = "\n".join("  > " + " ".join(m.split())[:300] for m in request["messages"])
        where = " on the host" if on_host else (" in its dev container" if container else "")
        question = (f"Approve number {request['number']} for {request['project']}{where}, "
                    f"granting it for {until(args.minutes * 60)}?\n{messages}")
        if full:
            confirm(f"{question}\nWARNING: {warning}", "full access")
        else:
            confirm(question, "yes")
        options = {"request": request["id"], "kind": kind,
                   "minutes": args.minutes, "full_access_ok": full,
                   "when_idle": args.when_idle, "messages": len(request["messages"])}
        if args.end_on_lock is not None:
            options["end_on_lock"] = args.end_on_lock
        print(describe_grant(json.loads(await client.call("Approve", "s",
                                                          [json.dumps(options)]))))
        return
    if args.command == "deny":
        request = await find_request(client, args.request)
        await client.call("Deny", "s", [request["id"]])
        print(f"Denied number {request['number']} for {request['project']}.")
        return
    if args.command == "revoke":
        if args.all:
            count = await client.call("RevokeAll", "b", [args.stop])
            print(f"Revoked {count} grant(s)." + (" Running requests were stopped."
                                                  if args.stop else ""))
            return
        if not args.grant:
            raise Failure("Name a grant or project, or --all.")
        grant = await find_grant(client, args.grant)
        result = json.loads(await client.call("Revoke", "sb", [grant, args.stop]))
        stopped = f" Stopped {result['stopped']} running request(s)." if args.stop else \
            " Running requests may finish."
        print(f"Revoked {grant}.{stopped}")
        return
    if args.command == "stop":
        result = json.loads(await client.call("StopProject", "sb", [args.project, args.lock]))
        text = f"Stopped {result['stopped']} running request(s) in {args.project}."
        if result["revoked"]:
            text += " Ended the grant(s) for: " + ", ".join(
                "all projects" if t == "*" else t for t in result["revoked"]) + "."
        print(text)
        return
    if args.command == "keep-awake":
        state = json.loads(await client.call("SetKeepAwake", "sb", [args.mode, args.lid]))
        print(f"Keep awake: {state['mode']}" + (", also with the lid closed" if state["lid"]
                                                else "") + ".")
        return
    if args.command == "cancel-switch":
        await client.call("CancelSwitch", "s", [args.project])
        print(f"Cancelled the switch of {args.project}.")
        return
    if args.command == "watch":
        await watch(client)


async def watch(client: Client) -> None:
    from dbus_fast import Message, MessageType
    rule = f"type='signal',interface='{INTERFACE}',member='Event',path='{PATH}'"
    await client.bus.call(Message(
        destination="org.freedesktop.DBus", path="/org/freedesktop/DBus",
        interface="org.freedesktop.DBus", member="AddMatch", signature="s", body=[rule]))
    events: asyncio.Queue = asyncio.Queue()

    def handler(msg):
        if msg.message_type == MessageType.SIGNAL and msg.member == "Event" \
                and msg.interface == INTERFACE:
            events.put_nowait(msg.body[0])
    client.bus.add_message_handler(handler)
    while True:
        event = json.loads(await events.get())
        stamp = time.strftime("%H:%M:%S", time.localtime(event.pop("time", time.time())))
        kind = event.pop("type")
        print(f"{stamp} {kind} " + " ".join(f"{k}={v}" for k, v in event.items()), flush=True)


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="agent-relay-ctl",
                                description="Approvals for agent-relay-mcp.")
    sub = p.add_subparsers(dest="command")

    s = sub.add_parser("status", help="grants, requests and projects (the default)")
    s.add_argument("--json", action="store_true")

    def lock_options(q):
        g = q.add_mutually_exclusive_group()
        g.add_argument("--end-on-lock", dest="end_on_lock", action="store_true", default=None,
                       help="end the grant when the screen locks")
        g.add_argument("--keep-on-lock", dest="end_on_lock", action="store_false",
                       help="keep it when the screen locks")

    g = sub.add_parser("grant", help="approve a project, or all projects")
    g.add_argument("project", nargs="?")
    g.add_argument("--all", action="store_true", help="all projects with a dev container")
    g.add_argument("--for", dest="minutes", type=minutes, default=60,
                   help="duration, e.g. 30m, 2h, 1h30m (default 1h)")
    g.add_argument("--on-host", action="store_true",
                   help="run a project with a dev container on the host instead")
    g.add_argument("--include-host-projects", action="store_true",
                   help="with --all: projects without a dev container too")
    g.add_argument("--when-idle", action="store_true",
                   help="if the other side of the project still works, switch once it is idle")
    lock_options(g)

    a = sub.add_parser("approve", help="approve a waiting request")
    a.add_argument("request", help="its number (or id)")
    a.add_argument("--for", dest="minutes", type=minutes, default=60)
    g2 = a.add_mutually_exclusive_group()
    g2.add_argument("--on-host", action="store_true",
                    help="run it on the host (the default for requests that ask for it)")
    g2.add_argument("--in-container", action="store_true",
                    help="run it in its dev container, although it asks for the host")
    a.add_argument("--when-idle", action="store_true")
    lock_options(a)

    d = sub.add_parser("deny", help="deny a waiting request")
    d.add_argument("request", help="its number (or id)")

    r = sub.add_parser("revoke", help="end a grant; running requests finish unless --stop")
    r.add_argument("grant", nargs="?", help="grant id or project")
    r.add_argument("--all", action="store_true")
    r.add_argument("--stop", action="store_true", help="also stop running requests")

    t = sub.add_parser("stop", help="stop what Claude Code does in a project")
    t.add_argument("project")
    t.add_argument("--lock", action="store_true",
                   help="also end the grants that cover it (all projects, if that is one)")

    k = sub.add_parser("keep-awake", help="when to keep the computer from sleeping")
    k.add_argument("mode", choices=["off", "running", "granted"])
    k.add_argument("--lid", action="store_true",
                   help="also keep it running with the lid closed (warm in a bag!)")

    c = sub.add_parser("cancel-switch", help="cancel a waiting switch")
    c.add_argument("project")

    sub.add_parser("watch", help="print events as they come")
    return p


def main(argv: list[str] | None = None) -> None:
    args = parser().parse_args(argv)
    if args.command is None:
        args.json = False
    if getattr(args, "include_host_projects", False) and not args.all:
        parser().error("--include-host-projects goes with --all")

    async def go():
        client = await Client().connect()
        try:
            await command(args, client)
        finally:
            client.bus.disconnect()

    try:
        asyncio.run(go())
    except Failure as error:
        print(f"agent-relay-ctl: {error}", file=sys.stderr)
        raise SystemExit(1)
    except KeyboardInterrupt:
        raise SystemExit(130)


if __name__ == "__main__":
    main()
