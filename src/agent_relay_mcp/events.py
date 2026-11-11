"""Reading Claude Code's stream-json output (claude -p --output-format stream-json)."""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field

MAX_STEPS = 200
MAX_RESULTS = 20
# After a background task has ended, Claude Code takes up its notification in
# a pass of its own, which ends with a result. If no such pass begins within
# this time, the request counts as done anyway.
NOTICE_GRACE_SECONDS = 120
_TASK_ENDED = ("completed", "failed", "killed", "stopped")


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
    # Background tasks of the main agent (subagents, shells run in the
    # background) that are still open: task id -> description. Claude Code
    # lists them in "background_tasks_changed"; task_started and
    # task_notification keep track where that is missing.
    tasks: dict[str, str] = field(default_factory=dict)
    known_tasks: dict[str, str] = field(default_factory=dict)
    # Where in the output the last result is, and the last sign of work: a
    # pass beginning, Claude Code writing, or a background task of the main
    # agent ending (Claude Code takes that up in a pass of its own). Byte
    # offsets of the lines, or line numbers when fed without offsets.
    result_at: int = -1
    work_at: int = -1
    # When a background task ended with no pass begun since (time.time()).
    noticed_at: float = 0.0
    _lines: int = 0

    def feed(self, line: str, at: int | None = None) -> None:
        """Takes one line of output; at: where the line starts in the output.
        The output is written where the run is, so in a dev container by
        whatever runs there: anything malformed is skipped."""
        self._lines += 1
        try:
            event = json.loads(line)
        except ValueError:
            return
        if not isinstance(event, dict):
            return
        try:
            self._feed(event, self._lines if at is None else at)
        except (AttributeError, TypeError, ValueError):
            pass

    def done(self, mark: int, now: float | None = None) -> bool:
        """Whether Claude Code has finished what it was given: a result came
        after mark (where the last message was passed in) and after all work
        since, and no background task is open."""
        if self.result_at < 0 or self.result_at < mark or self.tasks:
            return False
        if self.work_at > self.result_at:
            # Only a background task ended since the result: its pass should
            # begin at once; if it never does, do not wait for ever.
            now = time.time() if now is None else now
            return bool(self.noticed_at) and now - self.noticed_at >= NOTICE_GRACE_SECONDS
        return True

    def _work(self, at: int) -> None:
        self.work_at = max(self.work_at, at)
        self.noticed_at = 0.0

    def _system(self, event: dict, at: int) -> None:
        subtype = event.get("subtype")
        if subtype == "init":
            self.session_id = _uuid(event.get("session_id")) or self.session_id
            self._work(at)
        elif subtype == "background_tasks_changed":
            tasks = event.get("tasks")
            if isinstance(tasks, list):
                self.tasks = {str(t["task_id"]): _describe(t)
                              for t in tasks if isinstance(t, dict) and t.get("task_id")}
                self.known_tasks.update(self.tasks)
        elif subtype == "task_started":
            if event.get("is_backgrounded") and not event.get("owned_by_subagent") \
                    and event.get("task_id"):
                task = str(event["task_id"])
                self.tasks[task] = self.known_tasks[task] = _describe(event)
        elif subtype in ("task_notification", "task_updated"):
            task = str(event.get("task_id") or "")
            status = event.get("status") if subtype == "task_notification" \
                else (event.get("patch") or {}).get("status")
            if task not in self.known_tasks or (
                    subtype == "task_updated" and status not in _TASK_ENDED):
                return          # not one of the main agent's background tasks
            self.tasks.pop(task, None)
            if subtype == "task_notification":
                self.items.append(("step", f"Background task {status or 'ended'}: "
                                           f"{self.known_tasks[task]}"))
                self.work_at = max(self.work_at, at)
                self.noticed_at = time.time()

    def _feed(self, event: dict, at: int) -> None:
        kind = event.get("type")
        if kind == "system":
            self._system(event, at)
        elif kind in ("assistant", "user") and event.get("parent_tool_use_id") is None:
            self._work(at)
        if kind == "assistant" and event.get("parent_tool_use_id") is None:
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
            self.result_at = max(self.result_at, at)
            self.noticed_at = 0.0
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


def _describe(task: dict) -> str:
    return " ".join(str(task.get("description") or task.get("task_type") or "task")
                    .split())[:120]


def complete_lines(data: bytes) -> tuple[list[str], int]:
    """The complete lines in data and the number of bytes they take up.

    A line still being written has no newline yet; it is read next time.
    """
    end = data.rfind(b"\n")
    if end < 0:
        return [], 0
    chunk = data[:end + 1]
    return chunk.decode("utf-8", errors="replace").splitlines(), len(chunk)
