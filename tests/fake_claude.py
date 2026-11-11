#!/usr/bin/env python3
"""Stands in for `claude -p --input-format stream-json --output-format stream-json`
in the tests.

Reads messages from stdin, one per line, until the end of input; each one is a
pass with a result. "sleep N" in a message makes it work N seconds, and
messages that arrive meanwhile are taken into that pass, as Claude Code does.
"crash" makes it exit without a result. "background N" starts a background
task (as the Agent tool with run_in_background does) that ends after N
seconds; the pass ends at once with a result, and when the task ends, Claude
Code's events for that follow and a pass of its own takes it up, with a
second result. Like Claude Code with CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS=0,
it waits for open background tasks after the end of input before it exits.

It writes its arguments to fake-args.txt and the environment variables
starting with CLAUDE_CODE_ to fake-env.txt in its working directory, so tests
can check them.
"""
import json
import os
import queue
import re
import sys
import threading
import time
from pathlib import Path

args = sys.argv[1:]
session = args[args.index("--resume") + 1] if "--resume" in args else args[args.index("--session-id") + 1]
Path("fake-args.txt").write_text(json.dumps(args))
Path("fake-env.txt").write_text(json.dumps(
    {k: v for k, v in os.environ.items() if k.startswith("CLAUDE_CODE_")}))

inbox: queue.Queue = queue.Queue()
EOF = None


def read_stdin():
    for line in sys.stdin:
        if line.strip():
            inbox.put(json.loads(line)["message"]["content"])
    inbox.put(EOF)


threading.Thread(target=read_stdin, daemon=True).start()


def emit(event):
    print(json.dumps(event), flush=True)


open_tasks: dict[str, str] = {}
counter = 0


def tasks_changed():
    emit({"type": "system", "subtype": "background_tasks_changed", "session_id": session,
          "tasks": [{"task_id": t, "task_type": "local_agent", "description": d}
                    for t, d in open_tasks.items()]})


def start_background(seconds: int) -> None:
    global counter
    counter += 1
    task, tool = f"task{counter}", f"toolu_{counter}"
    emit({"type": "assistant", "parent_tool_use_id": None, "message": {"content": [
        {"type": "tool_use", "id": tool, "name": "Agent",
         "input": {"description": f"Background job {counter}", "run_in_background": True}}]}})
    open_tasks[task] = f"Background job {counter}"
    tasks_changed()
    emit({"type": "system", "subtype": "task_started", "task_id": task, "tool_use_id": tool,
          "description": open_tasks[task], "is_backgrounded": True,
          "task_type": "local_agent", "session_id": session})
    # A step of the subagent, and one of its own foreground shells.
    emit({"type": "assistant", "parent_tool_use_id": tool, "message": {"content": [
        {"type": "tool_use", "name": "Bash", "input": {"command": "make"}}]}})
    emit({"type": "system", "subtype": "task_started", "task_id": f"sub{counter}",
          "owned_by_subagent": True, "is_backgrounded": False, "task_type": "local_bash",
          "description": "make", "session_id": session})
    threading.Timer(seconds, inbox.put, args=(("done", task),)).start()


def finish_background(task: str) -> None:
    description = open_tasks.pop(task)
    tasks_changed()
    emit({"type": "system", "subtype": "task_updated", "task_id": task,
          "patch": {"status": "completed"}, "session_id": session})
    emit({"type": "system", "subtype": "task_notification", "task_id": task,
          "status": "completed", "summary": description, "session_id": session})
    # Claude Code takes the notification up in a pass of its own.
    emit({"type": "system", "subtype": "init", "session_id": session})
    emit({"type": "assistant", "parent_tool_use_id": None, "message": {"content": [
        {"type": "text", "text": f"{description} is done."}]}})
    emit({"type": "result", "subtype": "success", "is_error": False, "session_id": session,
          "result": f"Background done: {description}", "duration_ms": 10})


def work(message: str, held: list) -> None:
    """One pass for message; messages that arrive while it sleeps join it."""
    emit({"type": "system", "subtype": "init", "session_id": session})
    if "think" in message:
        emit({"type": "assistant", "parent_tool_use_id": None, "message": {"content": [
            {"type": "thinking", "thinking": "Pondering the request.", "signature": "x"}]}})
    emit({"type": "assistant", "parent_tool_use_id": None, "message": {"content": [
        {"type": "text", "text": "Looking into it."},
        {"type": "tool_use", "name": "Bash", "input": {"command": "ls", "description": "List files"}}]}})
    parts = [message]
    match = re.search(r"sleep (\d+)", message)
    if match:
        deadline = time.monotonic() + int(match.group(1))
        while (left := deadline - time.monotonic()) > 0:
            try:
                more = inbox.get(timeout=left)
            except queue.Empty:
                break
            if isinstance(more, str):
                parts.append(more)
            else:
                held.append(more)       # the end of input, or a task that ended
    if "crash" in message:
        print("boom", file=sys.stderr)
        sys.exit(3)
    match = re.search(r"background (\d+)", message)
    if match:
        start_background(int(match.group(1)))
    emit({"type": "result", "subtype": "success", "is_error": False, "session_id": session,
          "result": f"Done: {' + '.join(p.strip() for p in parts)} "
                    f"({'resumed' if '--resume' in args else 'new'})",
          "duration_ms": 1234})


ended = False
held: list = []
while not (ended and not open_tasks):
    item = held.pop(0) if held else inbox.get()
    if item is EOF:
        ended = True
    elif isinstance(item, tuple):
        finish_background(item[1])
    else:
        work(item, held)
