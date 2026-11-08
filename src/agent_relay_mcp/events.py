"""Reading Claude Code's stream-json output (claude -p --output-format stream-json)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field

# Input field that best says what a tool call does, per tool.
_SUMMARY_FIELDS = {
    "Bash": ("description", "command"),
    "Read": ("file_path",),
    "Edit": ("file_path",),
    "MultiEdit": ("file_path",),
    "Write": ("file_path",),
    "NotebookEdit": ("notebook_path",),
    "Glob": ("pattern",),
    "Grep": ("pattern",),
    "WebFetch": ("url",),
    "WebSearch": ("query",),
    "Agent": ("description",),
    "Task": ("description",),
}


def summarize_tool(name: str, tool_input: dict) -> str:
    for key in _SUMMARY_FIELDS.get(name, ("description",)):
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            text = " ".join(value.split())
            return f"{name}: {text[:120]}"
    return name


@dataclass
class TurnOutput:
    """What one claude -p run has produced so far."""
    session_id: str = ""
    steps: list[str] = field(default_factory=list)
    last_text: str = ""
    result: str | None = None
    is_error: bool = False
    duration_ms: int = 0
    cost_usd: float | None = None

    def feed(self, line: str) -> None:
        try:
            event = json.loads(line)
        except ValueError:
            return
        if not isinstance(event, dict):
            return
        kind = event.get("type")
        if kind == "system" and event.get("subtype") == "init":
            self.session_id = event.get("session_id") or self.session_id
        elif kind == "assistant" and event.get("parent_tool_use_id") is None:
            for block in (event.get("message") or {}).get("content") or []:
                if block.get("type") == "text" and block.get("text", "").strip():
                    self.last_text = block["text"].strip()
                elif block.get("type") == "tool_use":
                    self.steps.append(summarize_tool(block.get("name", "?"),
                                                     block.get("input") or {}))
        elif kind == "result":
            self.session_id = event.get("session_id") or self.session_id
            self.result = event.get("result") or ""
            self.is_error = bool(event.get("is_error")) or event.get("subtype") != "success"
            if self.is_error and not self.result:
                errors = event.get("errors") or []
                self.result = "; ".join(map(str, errors)) or str(event.get("subtype"))
            self.duration_ms = int(event.get("duration_ms") or 0)
            self.cost_usd = event.get("total_cost_usd")


def complete_lines(data: bytes) -> tuple[list[str], int]:
    """The complete lines in data and the number of bytes they take up.

    A line still being written has no newline yet; it is read next time.
    """
    end = data.rfind(b"\n")
    if end < 0:
        return [], 0
    chunk = data[:end + 1]
    return chunk.decode("utf-8", errors="replace").splitlines(), len(chunk)
