"""One Claude Code conversation per project, one detached claude -p run per request.

A run is started detached from everything this server holds (setsid, output to
a file in the run's own directory), so it survives a restart of this server, a
dropped tunnel or a phone call that never comes back. The server only keeps
what it needs to find the run again: its id, and the conversation's session id.

Messages sent while a run works are passed into it: the run reads stream-json
from a FIFO, and Claude Code takes a message that arrives there into account at
its next step. A small shell loop next to the run closes the FIFO once a result
has come after the last message passed in, so the run ends. Messages that
cannot be passed in (the run is still being started, or is already ending) are
queued here and sent as the next request once the run has ended.

Nothing runs without a grant (access.py). A message to a project no grant
covers becomes a request that waits for approval at the laptop; grants and
approvals only come through the control layer (control.py), never through an
MCP tool. A project with a dev container runs on the host only under a grant of
the kind "host"; it then has a conversation of its own there (key
"<name>/host"), since Claude Code keeps its sessions per machine and folder.
Only one kind of a project is ever busy: a switch waits until the other kind
has nothing left to do.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shlex
import time
import uuid
from dataclasses import dataclass, field

from . import targets
from .access import (ALL, CONTAINER, HOST, HOST_WARNING, ON_HOST_WARNING, Access,
                     AccessError, Grant, Limits, Switch)
from .config import Config
from .events import TurnOutput, complete_lines
from .keep_awake import KeepAwake
from .targets import Project, RelayError, Target

log = logging.getLogger("agent_relay_mcp")

QUEUE_POLL_SECONDS = 2
SWEEP_SECONDS = 10         # how often grants, requests and switches are checked

RUN_ROOT = '"$HOME/.cache/agent-relay-mcp"'
KEEP_RUNS_DAYS = 7

# Phases of a turn.
STARTING = "starting"      # container start and launch still in progress here
RUNNING = "running"
FINISHED = "finished"      # Claude Code ended and left a result
FAILED = "failed"          # it never started, crashed, or was stopped


@dataclass
class Turn:
    id: str
    message: str
    started: float
    phase: str = STARTING
    error: str = ""
    delivered: bool = False
    resumed: bool = False      # continued an existing session rather than starting one
    born: bool = False         # Claude Code reported the session, so it exists
    passed_in: list[str] = field(default_factory=list)  # messages added while it worked
    earlier_reply: str = ""    # the request before, ended unreported when this one was sent
    output: TurnOutput = field(default_factory=TurnOutput)
    offset: int = 0            # bytes of out.jsonl already fed to output
    # The output is not stored in state.json. After a restart it is read again
    # from the run, but only when needed: a reply not yet reported.
    output_loaded: bool = True
    closed: bool = False       # the run takes no more messages

    def to_state(self) -> dict:
        return {"id": self.id, "message": self.message, "started": self.started,
                "phase": self.phase, "error": self.error, "delivered": self.delivered,
                "resumed": self.resumed, "born": self.born, "passed_in": self.passed_in,
                "earlier_reply": self.earlier_reply}


@dataclass
class Conversation:
    session_id: str = ""
    turn: Turn | None = None
    queue: list[str] = field(default_factory=list)  # sent as the next request
    on_host: bool = False      # runs on this machine, not in a dev container

    @property
    def busy(self) -> bool:
        return bool(self.queue) or bool(self.turn and self.turn.phase in (STARTING, RUNNING))


# Runs next to Claude Code, as its parent: feeds it the first message through
# the FIFO "in", keeps the FIFO open for messages passed in later, and closes it
# once a result has come after the last of them ("mark" holds the number of
# results when that one was passed in). Messages still in the FIFO then are
# read before the end of input, so none is lost. "lock" makes passing a
# message in and closing exclusive.
_RUNNER = """
d=$0
"$@" < "$d/in" > "$d/out.jsonl" 2> "$d/err.log" &
c=$!
exec 3> "$d/in"
cat "$d/prompt" >&3
exec 9> "$d/lock"
while kill -0 "$c" 2>/dev/null; do
    sleep 1
    flock 9 2>/dev/null
    r=$(grep -c '"type": *"result"' "$d/out.jsonl" 2>/dev/null)
    if [ "${r:-0}" -gt "$(cat "$d/mark")" ]; then
        : > "$d/closed"
        exec 3>&-
        break
    fi
    flock -u 9 2>/dev/null
done
flock -u 9 2>/dev/null
wait "$c"
echo $? > "$d/exit"
"""


def _user_message(text: str) -> bytes:
    """One message for claude -p --input-format stream-json."""
    return (json.dumps({"type": "user", "message": {"role": "user", "content": text},
                        "parent_tool_use_id": None, "session_id": ""}) + "\n").encode()


def _run_dir(turn_id: str) -> str:
    uuid.UUID(turn_id)          # never anything else in a shell command
    return f'{RUN_ROOT}/{turn_id}'


def natural(project: Project) -> str:
    """Where a project runs unless a grant says otherwise."""
    return CONTAINER if project.has_devcontainer else HOST


def _key(project: Project, kind: str) -> str:
    """The conversation of project when it runs as kind. A project name never
    contains "/"."""
    return project.name if kind == natural(project) else f"{project.name}/{kind}"


def _where(kind: str) -> str:
    return "on the host" if kind == HOST else "in the dev container"


def _duration(seconds: float) -> str:
    minutes = max(0, int(seconds // 60))
    if minutes >= 60:
        return f"{minutes // 60} h {minutes % 60} min"
    return f"{minutes} min"


class Relay:
    def __init__(self, config: Config):
        self.config = config
        self.state_file = config.state_dir / "state.json"
        self.conversations: dict[str, Conversation] = {}
        self.locks: dict[str, asyncio.Lock] = {}
        self.background: set[asyncio.Task] = set()
        self.watchers: dict[str, asyncio.Task] = {}
        self.sweeper: asyncio.Task | None = None
        self.subscribers: set[asyncio.Queue] = set()
        self.clock = time.time
        self.access = Access(config.state_dir / "access.json", Limits(
            idle=config.grant_idle_minutes * 60,
            request_expiry=config.request_expiry_minutes * 60,
            max_project=config.max_grant_hours * 3600,
            max_all=config.max_all_grant_hours * 3600,
            max_host=config.max_host_grant_hours * 3600))
        self.keep_awake = KeepAwake(config.state_dir / "keep_awake.json")
        self._load()

    def close(self) -> None:
        """Lets the computer sleep again; called when the server stops."""
        self.keep_awake.close()

    # --- persistence -------------------------------------------------------

    def _load(self) -> None:
        if not self.state_file.is_file():
            return
        try:
            raw = json.loads(self.state_file.read_text())
        except ValueError:
            log.warning("ignoring unreadable %s", self.state_file)
            return
        for name, entry in raw.items():
            turn = entry.get("turn")
            conversation = Conversation(entry.get("session_id", ""),
                                        queue=entry.get("queue") or [],
                                        on_host=entry.get("on_host", False))
            if turn:
                conversation.turn = Turn(**turn, output_loaded=False)
                if conversation.turn.phase == STARTING:
                    # The launch was cut off by the restart; the run may or may
                    # not exist. Polling finds it running, finished or missing.
                    conversation.turn.phase = RUNNING
            self.conversations[name] = conversation

    def _save(self) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        data = {name: {"session_id": c.session_id,
                       "turn": c.turn.to_state() if c.turn else None,
                       "queue": c.queue, "on_host": c.on_host}
                for name, c in self.conversations.items()}
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        tmp.replace(self.state_file)

    def _lock(self, name: str) -> asyncio.Lock:
        """One lock per project, for both of its conversations."""
        return self.locks.setdefault(name.split("/")[0], asyncio.Lock())

    # --- events, for the control layer ---------------------------------------

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self.subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self.subscribers.discard(queue)

    def _emit(self, event_type: str, **data) -> None:
        log.info("event %s %s", event_type,
                 {k: v for k, v in data.items() if k != "message"})
        event = {"type": event_type, "time": self.clock(), **data}
        for queue in list(self.subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                self.subscribers.discard(queue)

    # --- conversations and where they run -------------------------------------

    def _conversation(self, project: Project, kind: str) -> Conversation:
        key = _key(project, kind)
        conversation = self.conversations.get(key)
        if conversation is None:
            conversation = self.conversations[key] = Conversation(on_host=kind == HOST)
        return conversation

    def _project_conversations(self, project: Project) -> list[tuple[str, Conversation]]:
        """(kind, conversation) for the kinds project has conversations in."""
        found = []
        for kind in (CONTAINER, HOST):
            conversation = self.conversations.get(_key(project, kind))
            if conversation is not None:
                found.append((kind, conversation))
        return found

    def _busy_kind(self, project: Project) -> str | None:
        return next((kind for kind, c in self._project_conversations(project) if c.busy),
                    None)

    def _current(self, project: Project) -> tuple[str, Conversation] | None:
        """The conversation to report on: the busy one, else the latest."""
        found = self._project_conversations(project)
        busy = [(k, c) for k, c in found if c.busy]
        if busy:
            return busy[0]
        with_turn = [(k, c) for k, c in found if c.turn]
        if with_turn:
            return max(with_turn, key=lambda kc: kc[1].turn.started)
        return found[0] if found else None

    def _route(self, project: Project, now: float) -> tuple[str, Grant | None, str]:
        """Where a new message for project goes: ("go", grant, kind), ("hold",
        None, kind) while a switch waits, or ("locked", None, "")."""
        switch = self.access.switches.get(project.name)
        if switch:
            return "hold", None, switch.kind
        grant = self.access.covering(project.name, natural(project), now)
        if not grant:
            return "locked", None, ""
        kind = grant.kind if grant.target != ALL else natural(project)
        busy = self._busy_kind(project)
        if busy and busy != kind:
            # The grant now covers the other kind, but this one still works:
            # the change waits until it is idle, as any switch does.
            self.access.switches[project.name] = Switch(
                project.name, kind, now, now + self.access.limits.request_expiry, None)
            self.access.save()
            self._emit("switch_pending", project=project.name, kind=kind)
            return "hold", None, kind
        return "go", grant, kind

    # --- the run in the container -----------------------------------------

    def _claude_args(self, session_id: str, resume: bool) -> list[str]:
        c = self.config
        args = shlex.split(c.claude_command) + [
            "-p", "--input-format", "stream-json", "--output-format", "stream-json",
            "--verbose"]
        if c.permission_mode:
            args += ["--permission-mode", c.permission_mode]
        if c.model:
            args += ["--model", c.model]
        if c.append_system_prompt:
            args += ["--append-system-prompt", c.append_system_prompt]
        args += ["--resume", session_id] if resume else ["--session-id", session_id]
        return args + c.extra_args

    def _launch(self, target: Target, turn: Turn, session_id: str, resume: bool) -> None:
        d = _run_dir(turn.id)
        claude = " ".join(shlex.quote(a) for a in self._claude_args(session_id, resume))
        # The prompt goes in through stdin, so no length limit and no quoting.
        # setsid: its own session, so a stop can end the whole process group,
        # and nothing here waits for it.
        script = f"""
set -e
find {RUN_ROOT} -mindepth 1 -maxdepth 1 -mtime +{KEEP_RUNS_DAYS} -exec rm -rf {{}} + 2>/dev/null || true
d={d}
mkdir -p "$d"
cat > "$d/prompt"
mkfifo "$d/in"
echo 0 > "$d/mark"
setsid sh -c {shlex.quote(_RUNNER)} "$d" {claude} < /dev/null > /dev/null 2>&1 &
echo $! > "$d/pid"
"""
        result = targets.run_in(self.config, target, script, stdin=_user_message(turn.message))
        if result.returncode != 0:
            raise RelayError("Claude Code could not be started: "
                             + result.stderr.decode(errors="replace").strip()[-800:])

    def _pass_in(self, target: Target, turn: Turn, message: str) -> bool:
        """Passes message into the running run; False if it takes none any more
        (it is ending, or was started before runs could take messages)."""
        d = _run_dir(turn.id)
        script = f"""
d={d}
[ -p "$d/in" ] || {{ echo CLOSED; exit 0; }}
exec 9> "$d/lock"
flock -w 10 9 2>/dev/null
if [ -e "$d/closed" ] || [ -e "$d/exit" ] || ! kill -0 "$(cat "$d/pid")" 2>/dev/null; then
    echo CLOSED; exit 0
fi
r=$(grep -c '"type": *"result"' "$d/out.jsonl" 2>/dev/null)
if timeout 10 sh -c 'cat > "$0/in"' "$d"; then
    echo "${{r:-0}}" > "$d/mark"
    echo PASSED
fi
"""
        result = targets.run_in(self.config, target, script, stdin=_user_message(message))
        if result.stdout.strip() == b"PASSED":
            return True
        turn.closed = True
        return False

    def _poll(self, target: Target, turn: Turn) -> None:
        """Reads what the run has written since the last poll, and its state."""
        d = _run_dir(turn.id)
        script = f"""
d={d}
if [ -f "$d/exit" ]; then echo "EXIT $(cat "$d/exit")"
elif [ ! -f "$d/pid" ]; then echo MISSING
elif kill -0 "$(cat "$d/pid")" 2>/dev/null; then echo RUNNING
else echo GONE; fi
tail -c +{turn.offset + 1} "$d/out.jsonl" 2>/dev/null
"""
        result = targets.run_in(self.config, target, script)
        status, _, data = result.stdout.partition(b"\n")
        status = status.decode(errors="replace").strip()
        lines, used = complete_lines(data)
        for line in lines:
            turn.output.feed(line)
        turn.offset += used
        turn.output_loaded = True
        turn.born = turn.born or bool(turn.output.session_id)
        if status == "RUNNING":
            turn.phase = RUNNING
        elif status.startswith("EXIT") or status == "GONE":
            if turn.output.result is not None:
                turn.phase = FINISHED
            else:
                turn.phase = FAILED
                turn.error = "Claude Code ended without a reply. " + self._stderr(target, turn)
        elif status == "MISSING":
            turn.phase = FAILED
            turn.error = "The run is gone from the container (was it rebuilt?)."
        else:
            turn.phase = FAILED
            turn.error = "Cannot read the run: " + result.stderr.decode(errors="replace")[-500:]

    def _stderr(self, target: Target, turn: Turn) -> str:
        result = targets.run_in(self.config, target, f'tail -c 1500 {_run_dir(turn.id)}/err.log')
        return result.stdout.decode(errors="replace").strip()

    def _stop(self, target: Target, turn: Turn) -> None:
        d = _run_dir(turn.id)
        script = f"""
d={d}
p="$(cat "$d/pid" 2>/dev/null)"
[ -n "$p" ] || exit 0
kill -TERM -- "-$p" 2>/dev/null || kill -TERM "$p" 2>/dev/null || true
for i in 1 2 3 4 5 6 7 8 9 10; do kill -0 "$p" 2>/dev/null || exit 0; sleep 0.5; done
kill -KILL -- "-$p" 2>/dev/null || true
"""
        targets.run_in(self.config, target, script)

    def _target(self, project: Project, conversation: Conversation, *,
                start: bool) -> Target | None:
        return targets.target_for(self.config, project, start=start,
                                  on_host=conversation.on_host)

    # --- refreshing ---------------------------------------------------------

    async def _refresh(self, project: Project, conversation: Conversation) -> None:
        turn = conversation.turn
        if not turn or turn.phase not in (RUNNING,):
            return
        target = await asyncio.to_thread(self._target, project, conversation, start=False)
        if target is None:
            turn.phase = FAILED
            turn.error = "The project's dev container is not running any more."
        else:
            await asyncio.to_thread(self._poll, target, turn)
        if turn.output.session_id:
            conversation.session_id = turn.output.session_id
        if turn.phase != RUNNING:
            self._save()
            self._emit("run_ended", project=project.name,
                       kind=self._kind_of(project, conversation), phase=turn.phase)

    async def _refresh_all(self, project: Project) -> None:
        for _, conversation in self._project_conversations(project):
            await self._refresh(project, conversation)

    @staticmethod
    def _kind_of(project: Project, conversation: Conversation) -> str:
        return HOST if conversation.on_host or not project.has_devcontainer else CONTAINER

    async def _load_output(self, project: Project, conversation: Conversation) -> bool:
        """Reads the reply of a finished turn again from its run, after a restart.
        False if the dev container is not running, so it cannot be read now."""
        turn = conversation.turn
        target = await asyncio.to_thread(self._target, project, conversation, start=False)
        if target is None:
            return False
        await asyncio.to_thread(self._poll, target, turn)
        if turn.phase != FINISHED:      # the run is gone, or left no reply
            self._save()
        return True

    async def _check(self, project: Project) -> None:
        """For list_projects: whether a turn that is running still is. Skipped
        while another call works on the project; that one keeps it current."""
        lock = self._lock(project.name)
        running = [c for _, c in self._project_conversations(project)
                   if c.turn and c.turn.phase == RUNNING]
        if not running or lock.locked():
            return
        async with lock:
            try:
                for conversation in running:
                    await self._refresh(project, conversation)
            except RelayError as error:
                log.warning("checking %s failed: %s", project.name, error)

    async def _wait(self, project: Project, conversation: Conversation, seconds: int) -> None:
        deadline = time.monotonic() + max(0, min(seconds, self.config.max_wait_seconds))
        while True:
            await self._refresh(project, conversation)
            turn = conversation.turn
            if not turn or turn.phase in (FINISHED, FAILED) or time.monotonic() >= deadline:
                return
            await asyncio.sleep(min(1.5, max(0.1, deadline - time.monotonic())))

    # --- the tools ------------------------------------------------------------

    def _access_note(self, project: Project, now: float) -> str:
        """The access state of project for list_projects."""
        request = self.access.open_request(project.name)
        if request:
            return f"locked, waiting for approval (number {request.number})"
        switch = self.access.switches.get(project.name)
        if switch:
            return f"switching to run {_where(switch.kind)} once idle"
        grant = self.access.covering(project.name, natural(project), now)
        if not grant:
            note = "locked"
        else:
            left = min(grant.expires - now, grant.idle - (now - grant.last_used))
            note = f"approved for {_duration(left)}"
            kind = grant.kind if grant.target != ALL else natural(project)
            if kind == HOST and project.has_devcontainer:
                note += ", on the host: full access"
        return note

    def _instance(self) -> str:
        return f" on the {self.config.instance_name}" if self.config.instance_name else ""

    async def list_projects(self) -> str:
        self._background()
        now = self.clock()
        projects = await asyncio.to_thread(targets.list_projects, self.config)
        try:
            running = await asyncio.to_thread(targets.running_folders, self.config)
        except RelayError:
            running = set()
        listed = []
        for project in projects:
            if project.has_devcontainer:
                where = "dev container" + (", running" if project.path in running else "")
            elif self.config.allow_host_projects:
                where = "no dev container: runs on the host, with full access to the computer"
            else:
                continue
            listed.append((project, where))
        await asyncio.gather(*(self._check(project) for project, _ in listed))
        lines = []
        for project, where in listed:
            current = self._current(project)
            conversation = current[1] if current else None
            turn = conversation.turn if conversation else None
            if turn and turn.phase in (STARTING, RUNNING):
                where += "; Claude Code is working"
            elif turn and not turn.delivered:
                where += "; a reply is waiting"
            if conversation and conversation.queue:
                where += f"; {len(conversation.queue)} message(s) queued"
            lines.append(f"- {project.name} ({where}; {self._access_note(project, now)})")
        if not lines:
            return "No usable projects found."
        return (f"Projects{self._instance()} (every project is locked unless approved at "
                "the laptop):\n" + "\n".join(lines))

    async def send_message(self, project_name: str, message: str,
                           new_conversation: bool, wait_seconds: int) -> str:
        project = targets.find_project(self.config, project_name)
        message = message.strip()
        if not message:
            raise RelayError("The message is empty.")
        if not project.has_devcontainer and not self.config.allow_host_projects:
            raise RelayError(f"{project.name} has no dev container, and running "
                             "Claude Code outside one is turned off.")
        self._background()
        async with self._lock(project.name):
            await self._refresh_all(project)
            now = self.clock()
            route, grant, kind = self._route(project, now)
            if route == "locked":
                return self._ask(project, [message], now)
            if route == "hold":
                switch = self.access.switches[project.name]
                switch.held.append(message)
                self.access.save()
                log.info("send_message %s: %d characters, held for a switch",
                         project.name, len(message))
                return (f"{project.name} is switching to run {_where(kind)}; that waits "
                        "until the request still working on the other side has finished. "
                        "The message is held and runs there once the switch is done. Ask "
                        "for the result later with get_updates.")
            self.access.touch(grant, now)
            self.access.save()
            conversation = self._conversation(project, kind)
            await self._refresh(project, conversation)
            if conversation.turn and conversation.turn.phase in (STARTING, RUNNING):
                if new_conversation:
                    raise RelayError(f"Claude Code is still working in {project.name}. A new "
                                     "conversation can start once it has finished, or after "
                                     "a stop.")
                return await self._add_to_running(project, conversation, message, wait_seconds)
            turn, resume = self._begin(project, conversation, message, new_conversation)
            container_running = conversation.on_host or bool(
                await asyncio.to_thread(targets.running_container, self.config, project))
            log.info("send_message %s: %d characters, %s, %s", project.name, len(message),
                     "resume" if resume else "new conversation", kind)

            if wait_seconds > 0:
                deadline = time.monotonic() + min(wait_seconds, self.config.max_wait_seconds)
                while turn.phase == STARTING and time.monotonic() < deadline:
                    await asyncio.sleep(0.5)
                remaining = int(deadline - time.monotonic())
                if turn.phase == RUNNING and remaining > 0:
                    await self._wait(project, conversation, remaining)
            if turn.phase in (FINISHED, FAILED):
                return self._report(project, conversation, mark_delivered=True)

        what = "Continuing the existing conversation" if resume else "Started a new conversation"
        if kind == HOST and project.has_devcontainer:
            what += " on the host (not in the dev container, as approved at the laptop)"
        start_note = "" if container_running else \
            " The dev container is being started first, which can take a minute or two."
        return (f"{what} with Claude Code in {project.name}. It is working on it."
                f"{start_note} Ask for the result later with get_updates.")

    def _ask(self, project: Project, messages: list[str], now: float) -> str:
        """Turns messages for a project no grant covers into a request."""
        try:
            request, new = self.access.request(project.name, messages, now,
                                               full_access=not project.has_devcontainer)
        except AccessError as error:
            raise RelayError(str(error)) from error
        self.access.save()
        self._emit("request_added" if new else "request_updated", project=project.name,
                   request=request.id, number=request.number,
                   full_access=request.full_access)
        log.info("%s: %d message(s) wait for approval (number %d)", project.name,
                 len(messages), request.number)
        warning = (f" {project.name} has no dev container, so the approval at the laptop "
                   "warns that it runs with full access to the computer."
                   if request.full_access else "")
        added = "" if new else " The message was added to the request already waiting."
        return (f"{project.name} is locked{self._instance()}. The message waits for approval "
                f"at the laptop: number {request.number}. Tell the user this number, so they "
                "can match it there. Once approved, it runs automatically; ask with "
                f"get_updates. The request expires in "
                f"{_duration(request.expires - now)} if nobody answers it.{added}{warning}")

    def _begin(self, project: Project, conversation: Conversation, message: str,
               new_conversation: bool, earlier_reply: str = "") -> tuple[Turn, bool]:
        """Starts a run for message in the background: (its turn, whether it
        continues the conversation)."""
        previous = conversation.turn
        # A new conversation whose first run failed may never have been
        # created; resuming it would fail too.
        unborn = (previous is not None and previous.phase == FAILED and not previous.resumed
                  and not previous.born)
        resume = bool(conversation.session_id) and not new_conversation and not unborn
        if not resume:
            conversation.session_id = str(uuid.uuid4())
        turn = Turn(str(uuid.uuid4()), message, time.time(), resumed=resume,
                    earlier_reply=earlier_reply)
        conversation.turn = turn
        self._save()
        self._update_keep_awake()
        self._emit("run_started", project=project.name,
                   kind=self._kind_of(project, conversation),
                   full_access=self._kind_of(project, conversation) == HOST,
                   message=message[:300])
        task = asyncio.create_task(
            self._start(project, conversation, turn, conversation.session_id, resume))
        self.background.add(task)
        task.add_done_callback(self.background.discard)
        return turn, resume

    async def _add_to_running(self, project: Project, conversation: Conversation,
                              message: str, wait_seconds: int) -> str:
        """A message for a project where Claude Code is working: passed into the
        run, or queued as the next request."""
        turn = conversation.turn
        # Queued messages go first, so the order stays as sent.
        if turn.phase == RUNNING and not turn.closed and not conversation.queue:
            target = await asyncio.to_thread(self._target, project, conversation, start=False)
            if target and await asyncio.to_thread(self._pass_in, target, turn, message):
                turn.passed_in.append(message)
                self._save()
                log.info("send_message %s: %d characters, passed into the running request",
                         project.name, len(message))
                if wait_seconds > 0:
                    await self._wait(project, conversation, wait_seconds)
                    if turn.phase in (FINISHED, FAILED):
                        return ("Passed the message into the running request. "
                                + self._report(project, conversation, mark_delivered=True))
                return (f"Passed the message directly into the request Claude Code in "
                        f"{project.name} is working on; it takes it into account at its "
                        "next step. Ask for the result later with get_updates.")
        conversation.queue.append(message)
        self._save()
        self._watch(_key(project, self._kind_of(project, conversation)))
        log.info("send_message %s: %d characters, queued", project.name, len(message))
        if turn.phase == STARTING:
            why = ("Claude Code is still being started, so the message cannot be passed "
                   "in yet. It is passed into the request as soon as Claude Code runs, or "
                   "sent as a follow-up request if that is no longer possible")
        else:
            why = ("the request Claude Code is working on is already ending and takes no "
                   "more messages. It is sent automatically as a follow-up request in the "
                   "same conversation as soon as the current one has finished")
        count = len(conversation.queue)
        waiting = f" {count} messages are queued now." if count > 1 else ""
        return f"Queued the message for {project.name}: {why}.{waiting}"

    # --- the queue ------------------------------------------------------------

    def _watch(self, key: str) -> None:
        """Makes sure something takes care of the queue of conversation key."""
        if key not in self.watchers:
            task = asyncio.create_task(self._drain(key))
            self.watchers[key] = task
            task.add_done_callback(lambda _: self.watchers.pop(key, None))

    def start_background(self) -> None:
        """Starts the sweeper and takes up queues; called once the server runs."""
        self._background()

    def _background(self) -> None:
        """Takes up queues left by a restart, and keeps the sweeper going."""
        for key, conversation in self.conversations.items():
            if conversation.queue:
                self._watch(key)
        if self.sweeper is None or self.sweeper.done():
            self.sweeper = asyncio.create_task(self._sweep_forever())

    def _may_run(self, project: Project, conversation: Conversation, now: float) -> bool:
        """Whether a grant still covers conversation's kind of project."""
        grant = self.access.covering(project.name, natural(project), now)
        if not grant:
            return False
        kind = grant.kind if grant.target != ALL else natural(project)
        return kind == self._kind_of(project, conversation)

    def _queue_to_request(self, project: Project, conversation: Conversation,
                          now: float) -> None:
        """Queued messages no grant covers any more wait for approval again."""
        if not conversation.queue:
            return
        messages, conversation.queue = conversation.queue, []
        self._save()
        try:
            self._ask(project, messages, now)
        except RelayError as error:
            log.warning("%s: dropped %d queued message(s): %s", project.name,
                        len(messages), error)

    async def _drain(self, key: str) -> None:
        """Passes the queue into the run once it can take messages, or sends it
        as the next request once the run has ended."""
        name = key.split("/")[0]
        while True:
            await asyncio.sleep(QUEUE_POLL_SECONDS)
            try:
                project = targets.find_project(self.config, name)
            except RelayError as error:     # the project is gone
                log.warning("dropping the queue of %s: %s", key, error)
                return
            try:
                async with self._lock(name):
                    conversation = self.conversations.get(key)
                    if not conversation or not conversation.queue:
                        return
                    if not self._may_run(project, conversation, self.clock()):
                        self._queue_to_request(project, conversation, self.clock())
                        return
                    turn = conversation.turn
                    if turn and turn.phase == STARTING:
                        continue
                    if turn and turn.phase == RUNNING:
                        await self._refresh(project, conversation)
                    message = "\n\n".join(conversation.queue)
                    if turn and turn.phase == RUNNING:
                        if turn.closed:
                            continue
                        target = await asyncio.to_thread(self._target, project, conversation,
                                                         start=False)
                        if target and await asyncio.to_thread(self._pass_in, target, turn,
                                                              message):
                            turn.passed_in += conversation.queue
                            conversation.queue = []
                            self._save()
                            log.info("%s: passed the queue into the running request", key)
                            return
                        continue
                    conversation.queue = []
                    earlier = await self._unreported(project, conversation) if turn else ""
                    self._begin(project, conversation, message, False, earlier)
                    log.info("%s: sent the queue as the next request", key)
                    return
            except RelayError as error:
                log.warning("handling the queue of %s failed: %s", key, error)
            except Exception:
                log.exception("handling the queue of %s failed", key)

    async def _unreported(self, project: Project, conversation: Conversation) -> str:
        """What the chat has not heard yet about an ended turn."""
        turn = conversation.turn
        if turn.delivered:
            return ""
        turn.delivered = True
        text = turn.earlier_reply + "\n\n" if turn.earlier_reply else ""
        if turn.phase == FINISHED and not turn.output_loaded \
                and not await self._load_output(project, conversation):
            return text + "(Its reply could not be read: the dev container was not running.)"
        if turn.phase == FAILED:
            return text + f"It failed: {turn.error}"
        return text + self._replies(turn.output)

    async def _start(self, project: Project, conversation: Conversation, turn: Turn,
                     session_id: str, resume: bool) -> None:
        try:
            target = await asyncio.to_thread(self._target, project, conversation, start=True)
            await asyncio.to_thread(self._launch, target, turn, session_id, resume)
            turn.phase = RUNNING
        except RelayError as error:
            turn.phase = FAILED
            turn.error = str(error)
        except Exception as error:     # never lose a turn silently
            log.exception("starting a turn in %s failed", project.name)
            turn.phase = FAILED
            turn.error = f"Unexpected error: {error}"
        self._save()

    async def get_updates(self, project_name: str, wait_seconds: int) -> str:
        project = targets.find_project(self.config, project_name)
        self._background()
        async with self._lock(project.name):
            now = self.clock()
            current = self._current(project)
            conversation = current[1] if current else None
            turn = conversation.turn if conversation else None
            waiting = self._waiting_note(project, turn, now)
            if not turn and conversation and conversation.queue:
                return (f"Claude Code in {project.name} is being started with "
                        f"{len(conversation.queue)} approved message(s).")
            if not turn:
                return waiting or f"Nothing has been sent to Claude Code in {project.name} yet."
            if waiting and turn.delivered and not conversation.busy:
                return waiting
            prefix = waiting + "\n\n" if waiting else ""
            if turn.phase == FINISHED and not turn.output_loaded:
                if turn.delivered:
                    return prefix + (f"Claude Code in {project.name} finished the last request, "
                                     "and its reply was already reported. It is not kept here "
                                     "any more.")
                if not await self._load_output(project, conversation):
                    return prefix + (f"Claude Code in {project.name} finished the last "
                                     "request, but its reply is in the project's dev "
                                     "container, which is not running now. Start the "
                                     "container to read the reply.")
            if wait_seconds > 0:
                deadline = time.monotonic() + min(wait_seconds, self.config.max_wait_seconds)
                while conversation.turn.phase == STARTING and time.monotonic() < deadline:
                    await asyncio.sleep(0.5)
                await self._wait(project, conversation, int(deadline - time.monotonic()))
            else:
                await self._refresh(project, conversation)
            return prefix + self._report(project, conversation, mark_delivered=True)

    def _waiting_note(self, project: Project, turn: Turn | None, now: float) -> str:
        """What get_updates says about a request or switch of project, if any."""
        request = self.access.open_request(project.name)
        if request:
            return (f"The request to {project.name} is waiting for approval at the laptop "
                    f"(number {request.number}, {_duration(now - request.created)} so far; "
                    f"it expires in {_duration(request.expires - now)}).")
        switch = self.access.switches.get(project.name)
        if switch and switch.held:
            return (f"{len(switch.held)} message(s) to {project.name} are held until it has "
                    f"switched to run {_where(switch.kind)}.")
        outcome = self.access.outcomes.get(project.name)
        if outcome and (not turn or outcome.time > turn.started):
            if outcome.status == "denied":
                return (f"The request to {project.name} (number {outcome.number}) was "
                        "denied at the laptop. Nothing was run.")
            return (f"The request to {project.name} (number {outcome.number}) expired "
                    "without an answer at the laptop. Nothing was run.")
        return ""

    async def stop(self, project_name: str) -> str:
        project = targets.find_project(self.config, project_name)
        async with self._lock(project.name):
            working = [c for _, c in self._project_conversations(project)
                       if c.turn and c.turn.phase in (STARTING, RUNNING)]
            if not working:
                return f"Claude Code is not working in {project.name}."
            conversation = working[0]
            turn = conversation.turn
            if turn.phase == STARTING:
                return ("The dev container is still starting; stop again in a moment, "
                        "once Claude Code runs.")
            dropped = await self._stop_conversation(project, conversation)
        log.info("stop %s", project.name)
        note = f" {dropped} queued message{'s were' if dropped > 1 else ' was'} dropped." \
            if dropped else ""
        return f"Stopped Claude Code in {project.name}. The conversation is kept.{note}"

    async def _stop_conversation(self, project: Project, conversation: Conversation) -> int:
        """Stops the running turn of conversation and drops its queue; the
        number of messages dropped. The caller holds the project's lock."""
        turn = conversation.turn
        if turn and turn.phase == RUNNING:
            target = await asyncio.to_thread(self._target, project, conversation, start=False)
            if target:
                await asyncio.to_thread(self._stop, target, turn)
                await asyncio.to_thread(self._poll, target, turn)
            if turn.phase == RUNNING or turn.output.result is None:
                turn.phase = FAILED
                turn.error = "Stopped on request."
            turn.delivered = True
        dropped = len(conversation.queue)
        conversation.queue = []
        self._save()
        return dropped

    # --- grants, requests and switches (for the control layer) ----------------

    def _project(self, name: str) -> Project:
        """A project by its exact name, as the control layer names them."""
        project = targets.find_project(self.config, name)
        if project.name != name:
            raise AccessError(f"There is no project named {name!r}.")
        return project

    def _check_kind(self, project: Project | None, kind: str, include_host_projects: bool,
                    full_access_ok: bool) -> None:
        if project and natural(project) == HOST and kind != HOST:
            raise AccessError(f"{project.name} has no dev container; it can only run on "
                              "the host.")
        full = kind == HOST or include_host_projects
        if full and not self.config.allow_host_projects:
            raise AccessError("Running Claude Code outside a dev container is turned off "
                              "(allow_host_projects).")
        if full and not full_access_ok:
            warning = ON_HOST_WARNING if project and project.has_devcontainer else HOST_WARNING
            raise AccessError(f"Needs confirmation: {warning}")

    async def grant(self, target: str, kind: str | None = None, seconds: int = 3600, *,
                    end_on_lock: bool | None = None, include_host_projects: bool = False,
                    full_access_ok: bool = False, when_idle: bool = False) -> dict:
        """Gives a grant. For a project whose other kind is still busy, it is
        refused unless when_idle, in which case it waits as a switch."""
        self._background()
        now = self.clock()
        if target == ALL:
            if kind not in (None, CONTAINER):
                raise AccessError("A grant for all projects runs them in their dev "
                                  "containers; a project runs on the host only by a grant "
                                  "of its own.")
            self._check_kind(None, CONTAINER, include_host_projects, full_access_ok)
            grant = self.access.add_grant(ALL, CONTAINER, seconds, now, natural=CONTAINER,
                                          end_on_lock=end_on_lock,
                                          include_host_projects=include_host_projects)
            self.access.save()
            self._emit("grant_added", grant=grant.id, target=ALL,
                       full_access=grant.full_access)
            return {"grant": self._grant_info(grant, now, seconds)}
        project = self._project(target)
        kind = kind or natural(project)
        self._check_kind(project, kind, False, full_access_ok)
        params = {"target": project.name, "kind": kind, "seconds": seconds,
                  "end_on_lock": end_on_lock}
        async with self._lock(project.name):
            await self._refresh_all(project)
            busy = self._busy_kind(project)
            if busy and busy != kind:
                return self._switch(project, kind, params, when_idle, [], now)
            grant = self._add_grant(project, params, now)
            return {"grant": self._grant_info(grant, now, seconds)}

    def _add_grant(self, project: Project, params: dict, now: float) -> Grant:
        grant = self.access.add_grant(params["target"], params["kind"], params["seconds"],
                                      now, natural=natural(project),
                                      end_on_lock=params["end_on_lock"])
        self.access.switches.pop(project.name, None)
        self.access.save()
        self._emit("grant_added", grant=grant.id, target=project.name, kind=grant.kind,
                    full_access=grant.full_access or natural(project) == HOST)
        return grant

    def _switch(self, project: Project, kind: str, params: dict | None, when_idle: bool,
                held: list[str], now: float) -> dict:
        other = self._busy_kind(project)
        conversation = self.conversations[_key(project, other)]
        turn = conversation.turn
        parts = []
        if turn and turn.phase in (STARTING, RUNNING):
            parts.append(f"is still working on a request ({_duration(now - turn.started)} "
                         "so far)")
        if conversation.queue:
            parts.append(f"has {len(conversation.queue)} queued message(s)")
        busy = (f"Switching {project.name} to run {_where(kind)}: {_where(other)} it "
                + " and ".join(parts) + ".")
        if not when_idle:
            raise AccessError(busy + " Switch when it is idle, or revoke and stop it first.")
        self.access.switches[project.name] = Switch(
            project.name, kind, now, now + self.access.limits.request_expiry, params,
            list(held))
        self.access.save()
        self._emit("switch_pending", project=project.name, kind=kind)
        return {"switch": {"project": project.name, "kind": kind, "waiting_for": busy}}

    async def approve(self, request_id: str, kind: str | None = None, seconds: int = 3600, *,
                      end_on_lock: bool | None = None, full_access_ok: bool = False,
                      when_idle: bool = False) -> dict:
        """Approves an open request with a grant for its project; its messages
        then run as if they had just been sent."""
        self._background()
        request = self.access.requests.get(request_id)
        if not request:
            raise AccessError(f"There is no open request {request_id}; it may have "
                              "expired or been answered already.")
        project = self._project(request.project)
        kind = kind or natural(project)
        self._check_kind(project, kind, False, full_access_ok)
        params = {"target": project.name, "kind": kind, "seconds": seconds,
                  "end_on_lock": end_on_lock}
        async with self._lock(project.name):
            now = self.clock()
            await self._refresh_all(project)
            busy = self._busy_kind(project)
            if busy and busy != kind:
                result = self._switch(project, kind, params, when_idle, request.messages, now)
                self.access.take_request(request_id)
                self.access.save()
                self._emit("request_answered", request=request_id, project=project.name,
                           approved=True)
                return result
            self.access.take_request(request_id)
            grant = self._add_grant(project, params, now)
            self._emit("request_answered", request=request_id, project=project.name,
                       approved=True)
            self._enqueue(project, kind, request.messages)
            return {"grant": self._grant_info(grant, now, seconds)}

    def _enqueue(self, project: Project, kind: str, messages: list[str]) -> None:
        """Hands approved messages to the queue of project's conversation as kind,
        which passes them in or sends them as the next request."""
        if not messages:
            return
        conversation = self._conversation(project, kind)
        conversation.queue += messages
        self._save()
        self._watch(_key(project, kind))

    async def deny(self, request_id: str) -> None:
        request = self.access.deny(request_id, self.clock())
        self.access.save()
        self._emit("request_answered", request=request_id, project=request.project,
                   approved=False)

    async def revoke(self, grant_id: str, stop: bool = False) -> dict:
        """Ends a grant. Running requests finish unless stop; queued messages
        wait for approval again (or are dropped, with stop)."""
        grant = self.access.end(grant_id)
        self.access.save()
        self._emit("grant_ended", grant=grant_id, reason="revoked", stopped=stop)
        stopped = await self._after_grant_end(grant, stop)
        return {"revoked": grant_id, "stopped": stopped}

    async def _after_grant_end(self, grant: Grant, stop: bool) -> int:
        """Applies the end of grant to the conversations it covered; the number
        of requests stopped."""
        now = self.clock()
        stopped = 0
        for project in await asyncio.to_thread(targets.list_projects, self.config):
            if grant.target not in (ALL, project.name):
                continue
            async with self._lock(project.name):
                for kind, conversation in self._project_conversations(project):
                    if self._may_run(project, conversation, now):
                        continue        # another grant still covers it
                    if stop:
                        working = conversation.turn and conversation.turn.phase == RUNNING
                        await self._stop_conversation(project, conversation)
                        stopped += bool(working)
                        switch = self.access.switches.pop(project.name, None)
                        if switch:
                            self.access.save()
                    else:
                        self._queue_to_request(project, conversation, now)
        return stopped

    async def release_access(self, project_name: str, all_projects: bool) -> str:
        """For the chat: ends grants when the user is done. It only takes access
        away, so it is a tool; it refuses while anything is still to be done
        there, so nothing the user asked for is cut off."""
        self._background()
        now = self.clock()
        if all_projects:
            grants = [g for g in self.access.grants.values() if self.access.live(g, now)]
            if not grants:
                return "Nothing is approved; every project is already locked."
            projects = [p for p in await asyncio.to_thread(targets.list_projects, self.config)
                        if any(g.target == p.name or (g.target == ALL and (
                            natural(p) == CONTAINER or g.include_host_projects))
                            for g in grants)]
        else:
            if not project_name.strip():
                raise RelayError("Name a project, or release all projects.")
            project = targets.find_project(self.config, project_name)
            projects = [project]
            grants = [g for g in self.access.grants.values()
                      if g.target == project.name and self.access.live(g, now)]
            if not grants:
                if self.access.covering(project.name, natural(project), now):
                    return (f"{project.name} is approved only through the approval for all "
                            "projects. Ending that locks every project; call release_access "
                            "with all_projects to do so.")
                return f"{project.name} is not approved; it is already locked."
        busy = []
        for project in projects:
            async with self._lock(project.name):
                await self._refresh_all(project)
                if self._busy_kind(project) or project.name in self.access.switches:
                    busy.append(project.name)
        if busy:
            raise RelayError(
                f"Not released: Claude Code is still working in {', '.join(busy)}, or has "
                "messages waiting there. Release it once it has finished, or stop it first.")
        for grant in grants:
            if grant.id in self.access.grants:
                self.access.end(grant.id)
                self._emit("grant_ended", grant=grant.id, reason="released by the chat",
                           stopped=False)
        self.access.save()
        log.info("released %d grant(s) on request of the chat", len(grants))
        if all_projects:
            return "Released all approvals. Every project is locked again."
        return f"Released the approval for {projects[0].name}. It is locked again."

    async def cancel_switch(self, project_name: str) -> None:
        project = self._project(project_name)
        async with self._lock(project.name):
            switch = self.access.switches.pop(project.name, None)
            if not switch:
                raise AccessError(f"No switch is waiting for {project.name}.")
            self.access.save()
            self._emit("switch_cancelled", project=project.name)
            if switch.held:
                self._ask(project, switch.held, self.clock())

    async def screen_locked(self) -> None:
        for grant in self.access.screen_locked():
            self.access.save()
            self._emit("grant_ended", grant=grant.id, reason="screen locked", stopped=False)
            await self._after_grant_end(grant, False)

    # --- the sweeper ------------------------------------------------------------

    async def _sweep_forever(self) -> None:
        while True:
            await asyncio.sleep(SWEEP_SECONDS)
            try:
                await self.sweep()
            except Exception:
                log.exception("checking grants failed")

    async def sweep(self) -> None:
        """Ends grants that ran out, drops expired requests, and carries out
        switches whose other side has become idle."""
        projects = {p.name: p for p in await asyncio.to_thread(targets.list_projects,
                                                                self.config)}
        for project in projects.values():
            if any(c.turn and c.turn.phase == RUNNING
                   for _, c in self._project_conversations(project)):
                await self._check(project)
        now = self.clock()
        # A running request is use: it keeps the grants that cover it alive.
        for project in projects.values():
            if self._busy_kind(project):
                grant = self.access.covering(project.name, natural(project), now,
                                             idle_ok=True)
                if grant:
                    self.access.touch(grant, now)
        for grant in self.access.ended(now):
            reason = "expired" if now >= grant.expires else "idle"
            self.access.save()
            self._emit("grant_ended", grant=grant.id, reason=reason, stopped=False)
            await self._after_grant_end(grant, False)
        for request in self.access.expired_requests(now):
            self.access.save()
            self._emit("request_expired", request=request.id, project=request.project)
        for name, switch in list(self.access.switches.items()):
            project = projects.get(name)
            if project:
                await self._advance_switch(project, switch)
        self.access.save()
        self._update_keep_awake()

    def _update_keep_awake(self) -> None:
        now = self.clock()
        running = any(c.turn and c.turn.phase in (STARTING, RUNNING)
                      for c in self.conversations.values())
        granted = any(self.access.live(g, now) for g in self.access.grants.values())
        was = self.keep_awake.active
        note = self.keep_awake.update(running=running, granted=granted, now=now)
        if note:
            self._emit("keep_awake_dropped", reason=note)
        if was != self.keep_awake.active:
            self._emit("keep_awake", active=self.keep_awake.active)

    def set_keep_awake(self, mode: str, lid: bool) -> dict:
        try:
            self.keep_awake.configure(mode, lid)
        except ValueError as error:
            raise AccessError(str(error)) from error
        self._update_keep_awake()
        self._emit("keep_awake_changed", mode=mode, lid=lid)
        return self.keep_awake.state()

    async def stop_project(self, project_name: str, lock: bool) -> dict:
        """For the laptop: stops what runs in a project; with lock, also ends
        the grants that cover it (for all projects too, if one does)."""
        project = self._project(project_name)
        async with self._lock(project.name):
            stopped = 0
            for _, conversation in self._project_conversations(project):
                working = conversation.turn and conversation.turn.phase == RUNNING
                if working or conversation.queue:
                    await self._stop_conversation(project, conversation)
                    stopped += bool(working)
            switch = self.access.switches.pop(project.name, None)
        ended = []
        if lock:
            now = self.clock()
            while True:
                grant = self.access.covering(project.name, natural(project), now,
                                             idle_ok=True)
                if not grant:
                    break
                self.access.end(grant.id)
                ended.append(grant.target)
                self._emit("grant_ended", grant=grant.id, reason="stopped and locked",
                           stopped=True)
                await self._after_grant_end(grant, True)
        if switch or ended:
            self.access.save()
        return {"stopped": stopped, "revoked": ended}

    async def _advance_switch(self, project: Project, switch: Switch) -> None:
        async with self._lock(project.name):
            now = self.clock()
            if self.access.switches.get(project.name) is not switch:
                return
            busy = self._busy_kind(project)
            if busy and busy != switch.kind:
                if now >= switch.expires:
                    del self.access.switches[project.name]
                    self.access.save()
                    self._emit("switch_expired", project=project.name, kind=switch.kind)
                    if switch.held:
                        self._ask(project, switch.held, now)
                return
            del self.access.switches[project.name]
            if switch.grant:
                self._add_grant(project, switch.grant, now)
            self.access.save()
            self._emit("switch_done", project=project.name, kind=switch.kind)
            if switch.held:
                route, _, kind = self._route(project, now)
                if route == "go" and kind == switch.kind:
                    self._enqueue(project, kind, switch.held)
                else:
                    self._ask(project, switch.held, now)

    # --- state, for the control layer -------------------------------------------

    def _grant_info(self, grant: Grant, now: float, asked: int | None = None) -> dict:
        """A grant for the control layer. asked: the duration asked for, to say
        whether it was cut to the upper limit."""
        return {"id": grant.id, "target": grant.target, "kind": grant.kind,
                "cut_to_limit": asked is not None and grant.expires - grant.created < asked,
                "expires": grant.expires,
                "idle_ends": grant.last_used + grant.idle,
                "seconds_left": max(0, int(min(grant.expires,
                                               grant.last_used + grant.idle) - now)),
                "end_on_lock": grant.end_on_lock,
                "include_host_projects": grant.include_host_projects,
                "full_access": grant.full_access or self._is_host_project(grant.target)}

    def _is_host_project(self, name: str) -> bool:
        if name == ALL:
            return False
        try:
            return natural(targets.find_project(self.config, name)) == HOST
        except RelayError:
            return False

    def state(self) -> dict:
        """Everything the GNOME menu or agent-relay-ctl shows."""
        now = self.clock()
        projects = []
        for project in targets.list_projects(self.config):
            if not project.has_devcontainer and not self.config.allow_host_projects:
                continue
            grant = self.access.covering(project.name, natural(project), now)
            busy = self._busy_kind(project)
            projects.append({
                "name": project.name, "dev_container": project.has_devcontainer,
                "grant": grant.id if grant else None,
                "kind": (grant.kind if grant.target != ALL else natural(project))
                if grant else None,
                "busy": busy})
        return {
            "instance": self.config.instance_name,
            "keep_awake": self.keep_awake.state(),
            "allow_host": self.config.allow_host_projects,
            "projects": projects,
            "grants": [self._grant_info(g, now) for g in self.access.grants.values()
                       if self.access.live(g, now)],
            "requests": [{"id": r.id, "project": r.project, "number": r.number,
                          "messages": [m[:2000] for m in r.messages],
                          "created": r.created, "expires": r.expires,
                          "full_access": r.full_access}
                         for r in self.access.requests.values()],
            "switches": [{"project": s.project, "kind": s.kind, "requested": s.requested,
                          "expires": s.expires, "held": len(s.held)}
                         for s in self.access.switches.values()],
            "warnings": {"host_project": HOST_WARNING, "on_host": ON_HOST_WARNING},
        }

    # --- reporting ------------------------------------------------------------

    @staticmethod
    def _replies(out: TurnOutput) -> str:
        if len(out.results) > 1:
            # A message passed in after a pass had ended got a pass of its own.
            return "Final replies, in order:\n" + "\n\n---\n\n".join(out.results)
        return f"Final reply:\n{out.result}"

    def _report(self, project: Project, conversation: Conversation, *,
                mark_delivered: bool) -> str:
        turn = conversation.turn
        elapsed = int(time.time() - turn.started)
        took = f"{elapsed // 60} min {elapsed % 60} s" if elapsed >= 60 else f"{elapsed} s"
        out = turn.output
        steps = out.steps
        recent = "\n".join(f"  - {s}" for s in steps[-8:])
        step_block = f"\nSteps ({len(steps)} in total, the last {min(8, len(steps))}):\n{recent}" \
            if steps else ""
        earlier = (f"Reply to the request before, which ended unreported:\n"
                   f"{turn.earlier_reply}\n\n") if turn.earlier_reply else ""
        passed = (f"\n{len(turn.passed_in)} message(s) were passed into this request "
                  "while it worked.") if turn.passed_in else ""
        queued = len(conversation.queue)
        queue_note = (f"\n{queued} message(s) are queued and follow as soon as possible."
                      if queued else "")
        if turn.phase == STARTING:
            return (f"{earlier}Claude Code in {project.name} is being started "
                    f"(the dev container may still be booting), {took} so far.{queue_note}")
        if turn.phase == RUNNING:
            text = f"\nLatest message from Claude Code:\n{out.last_text[-1200:]}" \
                if out.last_text else ""
            return (f"{earlier}Claude Code in {project.name} is still working ({took} so far)."
                    f"{passed}{queue_note}{step_block}{text}")
        again = "" if not turn.delivered else " (already reported once)"
        if mark_delivered and not turn.delivered:
            turn.delivered = True
            self._save()
        if turn.phase == FAILED:
            return f"{earlier}The request to {project.name} failed{again}: {turn.error}{queue_note}"
        status = "reported an error" if out.is_error else "finished"
        return (f"{earlier}Claude Code in {project.name} {status} after {took}{again}."
                f"{passed}{queue_note}{step_block}\n{self._replies(out)}")
