"""Reading Claude Code's stream-json output (claude -p --output-format stream-json)."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field

MAX_STEPS = 200
MAX_RESULTS = 20


def _uuid(value) -> str:
    """value if it is a UUID, else "": session ids come from the run's output,
    which the container writes, and end up as an argument of claude."""
    try:
        return str(uuid.UUID(value)) if isinstance(value, str) else ""
    except ValueError:
        return ""

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
    # One per pass: a message passed in after a pass has ended gets a pass of its own.
    results: list[str] = field(default_factory=list)
    is_error: bool = False
    duration_ms: int = 0
    cost_usd: float | None = None
    # What came in, in order, for the project's log: ("step", summary),
    # ("text", text), ("thinking", text), ("result", text, is_error). The
    # reader takes them out (see Relay._poll).
    items: list[tuple] = field(default_factory=list)

    def feed(self, line: str) -> None:
        """Takes one line of output. The output is written where the run is, so
        in a dev container by whatever runs there: anything malformed is skipped."""
        try:
            event = json.loads(line)
        except ValueError:
            return
        if not isinstance(event, dict):
            return
        try:
            self._feed(event)
        except (AttributeError, TypeError, ValueError):
            pass

    def _feed(self, event: dict) -> None:
        kind = event.get("type")
        if kind == "system" and event.get("subtype") == "init":
            self.session_id = _uuid(event.get("session_id")) or self.session_id
        elif kind == "assistant" and event.get("parent_tool_use_id") is None:
            for block in (event.get("message") or {}).get("content") or []:
                if block.get("type") == "text" and block.get("text", "").strip():
                    self.last_text = block["text"].strip()
                    self.items.append(("text", self.last_text))
                elif block.get("type") == "thinking" and str(block.get("thinking") or "").strip():
                    # Only with --thinking-display summarized; otherwise empty.
                    self.items.append(("thinking", str(block["thinking"]).strip()))
                elif block.get("type") == "tool_use":
                    self.steps.append(summarize_tool(block.get("name", "?"),
                                                     block.get("input") or {}))
                    del self.steps[:-MAX_STEPS]
                    self.items.append(("step", self.steps[-1]))
        elif kind == "result":
            self.session_id = _uuid(event.get("session_id")) or self.session_id
            self.result = str(event.get("result") or "")
            self.is_error = bool(event.get("is_error")) or event.get("subtype") != "success"
            if self.is_error and not self.result:
                errors = event.get("errors") or []
                self.result = "; ".join(map(str, errors)) or str(event.get("subtype"))
            self.results.append(self.result)
            del self.results[:-MAX_RESULTS]
            self.items.append(("result", self.result, self.is_error))
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
