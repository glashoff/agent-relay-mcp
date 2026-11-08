#!/usr/bin/env python3
"""Stands in for `claude -p --output-format stream-json` in the tests.

Reads the prompt from stdin. "sleep N" in the prompt makes it work N seconds,
"crash" makes it exit without a result. It writes its arguments to args.txt
next to its output, so tests can check them.
"""
import json
import re
import sys
import time
from pathlib import Path

args = sys.argv[1:]
session = args[args.index("--resume") + 1] if "--resume" in args else args[args.index("--session-id") + 1]
prompt = sys.stdin.read()
Path("fake-args.txt").write_text(json.dumps(args))


def emit(event):
    print(json.dumps(event), flush=True)


emit({"type": "system", "subtype": "init", "session_id": session})
emit({"type": "assistant", "parent_tool_use_id": None, "message": {"content": [
    {"type": "text", "text": "Looking into it."},
    {"type": "tool_use", "name": "Bash", "input": {"command": "ls", "description": "List files"}}]}})
match = re.search(r"sleep (\d+)", prompt)
if match:
    time.sleep(int(match.group(1)))
if "crash" in prompt:
    print("boom", file=sys.stderr)
    sys.exit(3)
emit({"type": "result", "subtype": "success", "is_error": False, "session_id": session,
      "result": f"Done: {prompt.strip()} ({'resumed' if '--resume' in args else 'new'})",
      "duration_ms": 1234})
