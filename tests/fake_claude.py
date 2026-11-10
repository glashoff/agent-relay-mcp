#!/usr/bin/env python3
"""Stands in for `claude -p --input-format stream-json --output-format stream-json`
in the tests.

Reads messages from stdin, one per line, until the end of input; each one is a
pass with a result. "sleep N" in a message makes it work N seconds, and
messages that arrive meanwhile are taken into that pass, as Claude Code does.
"crash" makes it exit without a result. It writes its arguments to
fake-args.txt in its working directory, so tests can check them.
"""
import json
import queue
import re
import sys
import threading
import time
from pathlib import Path

args = sys.argv[1:]
session = args[args.index("--resume") + 1] if "--resume" in args else args[args.index("--session-id") + 1]
Path("fake-args.txt").write_text(json.dumps(args))

inbox: queue.Queue = queue.Queue()


def read_stdin():
    for line in sys.stdin:
        if line.strip():
            inbox.put(json.loads(line)["message"]["content"])
    inbox.put(None)


threading.Thread(target=read_stdin, daemon=True).start()


def emit(event):
    print(json.dumps(event), flush=True)


message = inbox.get()
if message is not None:
    emit({"type": "system", "subtype": "init", "session_id": session})
while message is not None:
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
            if more is None:
                inbox.put(None)
                time.sleep(max(0, deadline - time.monotonic()))
                break
            parts.append(more)
    if "crash" in message:
        print("boom", file=sys.stderr)
        sys.exit(3)
    emit({"type": "result", "subtype": "success", "is_error": False, "session_id": session,
          "result": f"Done: {' + '.join(p.strip() for p in parts)} "
                    f"({'resumed' if '--resume' in args else 'new'})",
          "duration_ms": 1234})
    message = inbox.get()
