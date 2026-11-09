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
"""

from __future__ import annotations

import asyncio
import json
import logging
import shlex
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import targets
from .config import Config
from .events import TurnOutput, complete_lines
from .targets import Project, RelayError, Target

log = logging.getLogger("agent_relay_mcp")

QUEUE_POLL_SECONDS = 2

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


class Relay:
    def __init__(self, config: Config):
        self.config = config
        self.state_file = config.state_dir / "state.json"
        self.conversations: dict[str, Conversation] = {}
        self.locks: dict[str, asyncio.Lock] = {}
        self.background: set[asyncio.Task] = set()
        self.watchers: dict[str, asyncio.Task] = {}
        self._load()

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
                                        queue=entry.get("queue") or [])
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
                       "queue": c.queue}
                for name, c in self.conversations.items()}
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        tmp.replace(self.state_file)

    def _lock(self, name: str) -> asyncio.Lock:
        return self.locks.setdefault(name, asyncio.Lock())

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

    # --- refreshing ---------------------------------------------------------

    async def _refresh(self, project: Project, conversation: Conversation) -> None:
        turn = conversation.turn
        if not turn or turn.phase not in (RUNNING,):
            return
        target = await asyncio.to_thread(targets.target_for, self.config, project, start=False)
        if target is None:
            turn.phase = FAILED
            turn.error = "The project's dev container is not running any more."
        else:
            await asyncio.to_thread(self._poll, target, turn)
        if turn.output.session_id:
            conversation.session_id = turn.output.session_id
        if turn.phase != RUNNING:
            self._save()

    async def _load_output(self, project: Project, turn: Turn) -> bool:
        """Reads the reply of a finished turn again from its run, after a restart.
        False if the dev container is not running, so it cannot be read now."""
        target = await asyncio.to_thread(targets.target_for, self.config, project, start=False)
        if target is None:
            return False
        await asyncio.to_thread(self._poll, target, turn)
        if turn.phase != FINISHED:      # the run is gone, or left no reply
            self._save()
        return True

    async def _check(self, project: Project) -> None:
        """For list_projects: whether a turn that is running still is. Skipped
        while another call works on the project; that one keeps it current."""
        conversation = self.conversations.get(project.name)
        turn = conversation.turn if conversation else None
        lock = self._lock(project.name)
        if not turn or turn.phase != RUNNING or lock.locked():
            return
        async with lock:
            try:
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

    async def list_projects(self) -> str:
        self._watch_queues()
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
                where = "no dev container, runs on the host"
            else:
                continue
            listed.append((project, where))
        await asyncio.gather(*(self._check(project) for project, _ in listed))
        lines = []
        for project, where in listed:
            conversation = self.conversations.get(project.name)
            turn = conversation.turn if conversation else None
            if turn and turn.phase in (STARTING, RUNNING):
                where += "; Claude Code is working"
            elif turn and not turn.delivered:
                where += "; a reply is waiting"
            if conversation and conversation.queue:
                where += f"; {len(conversation.queue)} message(s) queued"
            lines.append(f"- {project.name} ({where})")
        return "Projects:\n" + "\n".join(lines) if lines else "No usable projects found."

    async def send_message(self, project_name: str, message: str,
                           new_conversation: bool, wait_seconds: int) -> str:
        project = targets.find_project(self.config, project_name)
        message = message.strip()
        if not message:
            raise RelayError("The message is empty.")
        if not project.has_devcontainer and not self.config.allow_host_projects:
            raise RelayError(f"{project.name} has no dev container, and running "
                             "Claude Code outside one is turned off.")
        self._watch_queues()
        async with self._lock(project.name):
            conversation = self.conversations.setdefault(project.name, Conversation())
            await self._refresh(project, conversation)
            if conversation.turn and conversation.turn.phase in (STARTING, RUNNING):
                if new_conversation:
                    raise RelayError(f"Claude Code is still working in {project.name}. A new "
                                     "conversation can start once it has finished, or after "
                                     "a stop.")
                return await self._add_to_running(project, conversation, message, wait_seconds)
            turn, resume = self._begin(project, conversation, message, new_conversation)
            container_running = (not project.has_devcontainer) or bool(
                await asyncio.to_thread(targets.running_container, self.config, project))
            log.info("send_message %s: %d characters, %s", project.name, len(message),
                     "resume" if resume else "new conversation")

            if wait_seconds > 0:
                deadline = time.monotonic() + min(wait_seconds, self.config.max_wait_seconds)
                while turn.phase == STARTING and time.monotonic() < deadline:
                    await asyncio.sleep(0.5)
                remaining = int(deadline - time.monotonic())
                if turn.phase == RUNNING and remaining > 0:
                    await self._wait(project, conversation, remaining)
            if turn.phase in (FINISHED, FAILED):
                return self._report(project, turn, mark_delivered=True)

        what = "Continuing the existing conversation" if resume else "Started a new conversation"
        start_note = "" if container_running else \
            " The dev container is being started first, which can take a minute or two."
        return (f"{what} with Claude Code in {project.name}. It is working on it."
                f"{start_note} Ask for the result later with get_updates.")

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
            target = await asyncio.to_thread(targets.target_for, self.config, project,
                                             start=False)
            if target and await asyncio.to_thread(self._pass_in, target, turn, message):
                turn.passed_in.append(message)
                self._save()
                log.info("send_message %s: %d characters, passed into the running request",
                         project.name, len(message))
                if wait_seconds > 0:
                    await self._wait(project, conversation, wait_seconds)
                    if turn.phase in (FINISHED, FAILED):
                        return ("Passed the message into the running request. "
                                + self._report(project, turn, mark_delivered=True))
                return (f"Passed the message directly into the request Claude Code in "
                        f"{project.name} is working on; it takes it into account at its "
                        "next step. Ask for the result later with get_updates.")
        conversation.queue.append(message)
        self._save()
        self._watch(project.name)
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

    def _watch(self, name: str) -> None:
        """Makes sure something takes care of name's queue."""
        if name not in self.watchers:
            task = asyncio.create_task(self._drain(name))
            self.watchers[name] = task
            task.add_done_callback(lambda _: self.watchers.pop(name, None))

    def _watch_queues(self) -> None:
        """Takes up queues left by a restart."""
        for name, conversation in self.conversations.items():
            if conversation.queue:
                self._watch(name)

    async def _drain(self, name: str) -> None:
        """Passes the queue into the run once it can take messages, or sends it
        as the next request once the run has ended."""
        while True:
            await asyncio.sleep(QUEUE_POLL_SECONDS)
            try:
                project = targets.find_project(self.config, name)
            except RelayError as error:     # the project is gone
                log.warning("dropping the queue of %s: %s", name, error)
                return
            try:
                async with self._lock(name):
                    conversation = self.conversations.get(name)
                    if not conversation or not conversation.queue:
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
                        target = await asyncio.to_thread(targets.target_for, self.config,
                                                         project, start=False)
                        if target and await asyncio.to_thread(self._pass_in, target, turn,
                                                              message):
                            turn.passed_in += conversation.queue
                            conversation.queue = []
                            self._save()
                            log.info("%s: passed the queue into the running request", name)
                            return
                        continue
                    conversation.queue = []
                    earlier = await self._unreported(project, turn) if turn else ""
                    self._begin(project, conversation, message, False, earlier)
                    log.info("%s: sent the queue as the next request", name)
                    return
            except RelayError as error:
                log.warning("handling the queue of %s failed: %s", name, error)
            except Exception:
                log.exception("handling the queue of %s failed", name)

    async def _unreported(self, project: Project, turn: Turn) -> str:
        """What the chat has not heard yet about an ended turn."""
        if turn.delivered:
            return ""
        turn.delivered = True
        text = turn.earlier_reply + "\n\n" if turn.earlier_reply else ""
        if turn.phase == FINISHED and not turn.output_loaded \
                and not await self._load_output(project, turn):
            return text + "(Its reply could not be read: the dev container was not running.)"
        if turn.phase == FAILED:
            return text + f"It failed: {turn.error}"
        return text + self._replies(turn.output)

    async def _start(self, project: Project, conversation: Conversation, turn: Turn,
                     session_id: str, resume: bool) -> None:
        try:
            target = await asyncio.to_thread(targets.target_for, self.config, project, start=True)
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
        self._watch_queues()
        async with self._lock(project.name):
            conversation = self.conversations.get(project.name)
            if not conversation or not conversation.turn:
                return f"Nothing has been sent to Claude Code in {project.name} yet."
            turn = conversation.turn
            if turn.phase == FINISHED and not turn.output_loaded:
                if turn.delivered:
                    return (f"Claude Code in {project.name} finished the last request, and "
                            "its reply was already reported. It is not kept here any more.")
                if not await self._load_output(project, turn):
                    return (f"Claude Code in {project.name} finished the last request, but its "
                            "reply is in the project's dev container, which is not running "
                            "now. Start the container to read the reply.")
            if wait_seconds > 0:
                deadline = time.monotonic() + min(wait_seconds, self.config.max_wait_seconds)
                while conversation.turn.phase == STARTING and time.monotonic() < deadline:
                    await asyncio.sleep(0.5)
                await self._wait(project, conversation, int(deadline - time.monotonic()))
            else:
                await self._refresh(project, conversation)
            return self._report(project, conversation.turn, mark_delivered=True)

    async def stop(self, project_name: str) -> str:
        project = targets.find_project(self.config, project_name)
        async with self._lock(project.name):
            conversation = self.conversations.get(project.name)
            turn = conversation.turn if conversation else None
            if not turn or turn.phase not in (STARTING, RUNNING):
                return f"Claude Code is not working in {project.name}."
            if turn.phase == STARTING:
                return ("The dev container is still starting; stop again in a moment, "
                        "once Claude Code runs.")
            target = await asyncio.to_thread(targets.target_for, self.config, project, start=False)
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
        log.info("stop %s", project.name)
        note = f" {dropped} queued message{'s were' if dropped > 1 else ' was'} dropped." \
            if dropped else ""
        return f"Stopped Claude Code in {project.name}. The conversation is kept.{note}"

    # --- reporting ------------------------------------------------------------

    @staticmethod
    def _replies(out: TurnOutput) -> str:
        if len(out.results) > 1:
            # A message passed in after a pass had ended got a pass of its own.
            return "Final replies, in order:\n" + "\n\n---\n\n".join(out.results)
        return f"Final reply:\n{out.result}"

    def _report(self, project: Project, turn: Turn, *, mark_delivered: bool) -> str:
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
        conversation = self.conversations.get(project.name)
        queued = len(conversation.queue) if conversation else 0
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
