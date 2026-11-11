#!/usr/bin/env python3
"""A stand-in for `claude -p --input-format stream-json --output-format
stream-json --include-partial-messages`, for the tests.

Logs its argv, its environment and every input line as JSON lines to
./fake-claude.log (the working directory the service gives it), and answers:

- control_request initialize -> control_response
- a user message -> system init (once), text deltas in small pieces,
  content_block_stop, the complete assistant message, result
- a user message containing "slow" -> first sentence only, then waits for an
  interrupt control_request and ends the turn with an error result
- a user message containing "tool" -> a tool_use block first, then text
- a user message containing "nopartial" -> only the complete assistant message
- a user message containing "crash" -> exits with code 3
- a user message containing "autherror" -> Claude Code's synthetic auth error
"""

import json
import os
import sys

LOG = open("fake-claude.log", "a", buffering=1)


def log(kind, data):
    LOG.write(json.dumps({"kind": kind, "data": data}) + "\n")


def out(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def delta(text):
    out({"type": "stream_event", "parent_tool_use_id": None,
         "event": {"type": "content_block_delta", "index": 0,
                   "delta": {"type": "text_delta", "text": text}}})


def stream_text(text, size=7):
    out({"type": "stream_event", "parent_tool_use_id": None,
         "event": {"type": "content_block_start", "index": 0,
                   "content_block": {"type": "text", "text": ""}}})
    for i in range(0, len(text), size):
        delta(text[i:i + size])
    out({"type": "stream_event", "parent_tool_use_id": None,
         "event": {"type": "content_block_stop", "index": 0}})


def assistant(text):
    out({"type": "assistant", "parent_tool_use_id": None,
         "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}})


def result(text, error=False):
    out({"type": "result", "subtype": "error_during_execution" if error else "success",
         "is_error": error, "result": text, "num_turns": 1, "duration_ms": 5})


log("argv", sys.argv)
log("env", dict(os.environ))
log("cwd", os.getcwd())
initialized = False
waiting_for_interrupt = False

for line in sys.stdin:
    log("stdin", line.rstrip("\n"))
    try:
        msg = json.loads(line)
    except ValueError:
        continue
    if msg.get("type") == "control_request":
        sub = (msg.get("request") or {}).get("subtype")
        out({"type": "control_response", "response": {
            "subtype": "success", "request_id": msg.get("request_id"), "response": {}}})
        if sub == "interrupt" and waiting_for_interrupt:
            waiting_for_interrupt = False
            result("", error=True)
        continue
    if msg.get("type") != "user":
        continue
    text = "".join(c.get("text", "") for c in msg["message"]["content"])
    if not initialized:
        initialized = True
        out({"type": "system", "subtype": "init", "session_id": "fake-session", "model": "fake",
             "mcp_servers": [{"name": "relay", "status": "failed"}],
             "tools": ["WebSearch", "WebFetch"]})
    if "crash" in text:
        sys.exit(3)
    if "autherror" in text:
        # what Claude Code 2.1.289 prints for an invalid token
        out({"type": "assistant", "parent_tool_use_id": None, "error": "authentication_failed",
             "message": {"model": "<synthetic>", "role": "assistant", "content": [
                 {"type": "text", "text": "Failed to authenticate. API Error: 401 OAuth access token is invalid."}]}})
        out({"type": "result", "subtype": "success", "is_error": True, "num_turns": 1,
             "result": "Failed to authenticate. API Error: 401 OAuth access token is invalid."})
        continue
    if "slow" in text:
        stream_text("Das ist der erste Satz. ")
        waiting_for_interrupt = True
        continue
    if "tool" in text:
        out({"type": "stream_event", "parent_tool_use_id": None,
             "event": {"type": "content_block_start", "index": 0,
                       "content_block": {"type": "tool_use", "name": "mcp__relay__list_projects"}}})
        out({"type": "stream_event", "parent_tool_use_id": None,
             "event": {"type": "content_block_stop", "index": 0}})
    reply = f"Du hast gesagt: {text}. Das ist der zweite Satz! Und ein dritter?"
    if "nopartial" in text:
        assistant(reply)
    else:
        stream_text(reply)
        # a subagent's text is not spoken
        out({"type": "stream_event", "parent_tool_use_id": "toolu_1",
             "event": {"type": "content_block_delta", "index": 0,
                       "delta": {"type": "text_delta", "text": "SUBAGENT. "}}})
        assistant(reply)
    result(reply)
