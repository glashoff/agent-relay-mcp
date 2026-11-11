"""Host actions: fixed commands from config.toml that Claude can ask to have
run on this computer itself, outside any dev container (see
docs/host-actions-plan.md).

Claude can only ask. Each request waits for the owner's answer at the laptop,
once per run. So far requests can only be denied or expire: nothing in this
module, or anywhere else yet, runs an action. This module parses the actions
from the config and keeps the requests; like access.py, it does no I/O apart
from its own file, and takes the time as an argument.
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath

from .access import ALL, AccessError

log = logging.getLogger("agent_relay_mcp")

NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]*$")
PLACEHOLDER = "{project}"
FIELDS = {"description", "command", "projects", "requires_protected",
          "writable_by_host_runs"}

WRITABLE_WARNING = ("This command lives in the project and may have been changed by "
                    "Claude since your last review.")
MAX_NOTE = 1000             # characters of Claude's note
MAX_WAITING = 5             # waiting requests, over all projects
KEEP_FINISHED = 50          # denied and expired requests kept for get_host_action
KEEP_SECONDS = 7 * 24 * 3600

WAITING = "waiting"
DENIED = "denied"
EXPIRED = "expired"


# --- the actions, from config.toml ---------------------------------------------------

@dataclass(frozen=True)
class Action:
    name: str
    description: str
    command: tuple[str, ...]        # command[0] with "~" expanded
    projects: tuple[str, ...] | str     # project names, or ALL
    requires_protected: tuple[str, ...] = ()
    # The command may lie where host runs of the project can change it (only
    # for projects without a dev container; see check_writable).
    writable_by_host_runs: bool = False

    def applies_to(self, project: str) -> bool:
        return self.projects == ALL or project in self.projects

    def resolve(self, project_path: Path) -> list[str]:
        """The command line for a project: "{project}" is its absolute path."""
        return [arg.replace(PLACEHOLDER, str(project_path)) for arg in self.command]


def parse(raw, where: str) -> dict[str, Action]:
    """The [actions.<name>] tables of the config, checked strictly. where
    starts every error message (the config file)."""
    if not isinstance(raw, dict):
        raise SystemExit(f"{where}: actions must be a table of [actions.<name>] tables")
    actions = {}
    for name, entry in raw.items():
        what = f"{where}: actions.{name}"
        if not NAME.match(name):
            raise SystemExit(f"{what}: action names are letters, digits and hyphens only")
        if not isinstance(entry, dict):
            raise SystemExit(f"{what} must be a table")
        unknown = set(entry) - FIELDS
        if unknown:
            raise SystemExit(f"{what}: unknown keys: {', '.join(sorted(unknown))}")
        description = entry.get("description", "")
        if not isinstance(description, str):
            raise SystemExit(f"{what}: description must be a string")
        actions[name] = Action(
            name=name, description=description,
            command=_command(entry.get("command"), what),
            projects=_projects(entry.get("projects"), what),
            requires_protected=_relative_paths(entry.get("requires_protected", []), what),
            writable_by_host_runs=_flag(entry.get("writable_by_host_runs", False), what))
    return actions


def _command(command, what: str) -> tuple[str, ...]:
    if not (isinstance(command, list) and command
            and all(isinstance(arg, str) and arg for arg in command)):
        raise SystemExit(f"{what}: command must be a non-empty list of non-empty strings")
    program = os.path.expanduser(command[0])
    inside = program.startswith(PLACEHOLDER + "/")
    if not (os.path.isabs(program) or inside):
        raise SystemExit(f"{what}: command must start with an absolute path, a path "
                         f"starting with ~/ or {PLACEHOLDER}/, not {command[0]!r}")
    rest = program[len(PLACEHOLDER):] if inside else program
    if PLACEHOLDER in rest or ".." in PurePosixPath(rest).parts:
        raise SystemExit(f"{what}: the program path must not contain .. or a second "
                         f"{PLACEHOLDER}")
    return (program, *command[1:])


def _projects(projects, what: str) -> tuple[str, ...] | str:
    if projects == ALL:
        return ALL
    if not (isinstance(projects, list) and projects
            and all(isinstance(p, str) and p and "/" not in p and p != ALL
                    for p in projects)):
        raise SystemExit(f'{what}: projects must be a list of project names, or "*"')
    return tuple(projects)


def _relative_paths(paths, what: str) -> tuple[str, ...]:
    if not (isinstance(paths, list) and all(isinstance(p, str) and p for p in paths)):
        raise SystemExit(f"{what}: requires_protected must be a list of paths")
    for p in paths:
        if os.path.isabs(p) or p.startswith("~") or ".." in PurePosixPath(p).parts:
            raise SystemExit(f"{what}: requires_protected takes paths relative to the "
                             f"project, without .., not {p!r}")
    return tuple(paths)


def _flag(value, what: str) -> bool:
    if not isinstance(value, bool):
        raise SystemExit(f"{what}: writable_by_host_runs must be true or false")
    return value


def has_devcontainer(folder: Path) -> bool:
    return (folder / ".devcontainer/devcontainer.json").is_file() \
        or (folder / ".devcontainer.json").is_file()


def check_writable(actions: dict[str, Action], projects_dir: Path, where: str) -> None:
    """writable_by_host_runs is only for projects without a dev container: a
    container could otherwise change the command that then runs on the host.
    A project whose folder does not exist (yet) is not an error."""
    for action in actions.values():
        if not action.writable_by_host_runs:
            continue
        what = f"{where}: actions.{action.name}"
        if action.projects == ALL:
            raise SystemExit(f"{what}: writable_by_host_runs needs a list of projects, "
                             'not "*"')
        for name in action.projects:
            if has_devcontainer(projects_dir / name):
                raise SystemExit(f"{what}: writable_by_host_runs is not allowed for "
                                 f"{name}, which has a dev container")


# --- requests ------------------------------------------------------------------------

@dataclass
class ActionRequest:
    id: str
    number: int                 # said to the user by Claude, shown at the laptop
    project: str
    action: str
    command: list[str]          # the command line as it would run, shown at the laptop
    description: str
    note: str                   # Claude's note: foreign text
    writable: bool              # from writable_by_host_runs: shown with a warning
    created: float
    expires: float
    status: str = WAITING       # WAITING, DENIED or EXPIRED
    answered: float | None = None


class HostActions:
    def __init__(self, path: Path | None, expiry: int = 30 * 60):
        self.path = path
        self.expiry = expiry
        self.requests: dict[str, ActionRequest] = {}
        self._load()

    # --- persistence -------------------------------------------------------

    def _load(self) -> None:
        if not self.path or not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text())
            if not isinstance(raw, dict):
                raise ValueError("not a JSON object")
            requests = {r["id"]: ActionRequest(**r) for r in raw.get("requests", [])}
            for r in requests.values():
                if r.status not in (WAITING, DENIED, EXPIRED) \
                        or not isinstance(r.command, list) \
                        or not all(isinstance(arg, str) for arg in r.command) \
                        or not isinstance(r.number, int) \
                        or not isinstance(r.expires, (int, float)):
                    raise ValueError(f"host action request {r.id} is malformed")
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as error:
            # Fail closed: the requests are gone, nothing waits. Keep the file
            # for a look.
            log.error("cannot read %s (%s); host action requests are dropped",
                      self.path, error)
            try:
                self.path.replace(self.path.with_suffix(".broken"))
            except OSError:
                pass
            return
        self.requests = requests

    def save(self) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {"requests": [asdict(r) for r in self.requests.values()]}
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        tmp.replace(self.path)

    # --- requests ------------------------------------------------------------

    def waiting(self) -> list[ActionRequest]:
        return [r for r in self.requests.values() if r.status == WAITING]

    def waiting_for(self, project: str) -> ActionRequest | None:
        return next((r for r in self.waiting() if r.project == project), None)

    def numbers(self) -> set[int]:
        """The numbers of the waiting requests."""
        return {r.number for r in self.waiting()}

    def request(self, project: str, action: Action, command: list[str], note: str,
                now: float, *, taken: set[int] = frozenset()) -> ActionRequest:
        """A new waiting request. taken: numbers in use elsewhere (open access
        requests), never given to this one."""
        if len(note) > MAX_NOTE:
            raise AccessError(f"The note is too long ({len(note)} characters, at most "
                              f"{MAX_NOTE}).")
        other = self.waiting_for(project)
        if other:
            raise AccessError(f"The host action {other.action} for {project} (number "
                              f"{other.number}) already waits for an answer at the "
                              "laptop; it has to be answered first.")
        if len(self.waiting()) >= MAX_WAITING:
            raise AccessError("Too many host actions are already waiting for an answer at "
                              "the laptop; try again later.")
        self._prune(now)
        taken = set(taken) | self.numbers()
        free = [n for n in range(10, 100) if n not in taken]
        # Prefer a number not used lately, so an old number is not mistaken
        # for this one.
        recent = {r.number for r in self.requests.values()}
        number = random.choice([n for n in free if n not in recent] or free)
        r = ActionRequest(id=uuid.uuid4().hex[:12], number=number, project=project,
                          action=action.name, command=list(command),
                          description=action.description, note=note,
                          writable=action.writable_by_host_runs, created=now,
                          expires=now + self.expiry)
        self.requests[r.id] = r
        return r

    def get(self, request_id: str) -> ActionRequest | None:
        return self.requests.get(request_id)

    def by_number(self, number: int) -> ActionRequest | None:
        """The newest request with number."""
        found = [r for r in self.requests.values() if r.number == number]
        return max(found, key=lambda r: r.created) if found else None

    def deny(self, request_id: str, now: float) -> ActionRequest:
        r = self.requests.get(request_id)
        if not r or r.status != WAITING or now >= r.expires:
            raise AccessError(f"There is no waiting host action {request_id}; it may have "
                              "expired or been answered already.")
        r.status, r.answered = DENIED, now
        self._prune(now)
        return r

    def expired(self, now: float) -> list[ActionRequest]:
        """Marks the waiting requests that ran out as expired, and returns them."""
        gone = [r for r in self.waiting() if now >= r.expires]
        for r in gone:
            r.status, r.answered = EXPIRED, now
        if gone:
            self._prune(now)
        return gone

    def _prune(self, now: float) -> None:
        """Keeps the latest finished requests, as history for get_host_action."""
        finished = sorted((r for r in self.requests.values() if r.status != WAITING),
                          key=lambda r: r.answered or r.created, reverse=True)
        for i, r in enumerate(finished):
            if i >= KEEP_FINISHED or now - (r.answered or r.created) > KEEP_SECONDS:
                del self.requests[r.id]
