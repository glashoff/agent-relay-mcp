"""Who may make Claude Code work where: grants, requests waiting for one, and
switches between running a project in its dev container and on the host.

Projects are locked unless a grant covers them. Grants are only given through
the control layer (control.py), never by anything an MCP tool receives. This
module keeps the state and its rules; it does no I/O apart from its own file,
and takes the time as an argument, so the rules can be tested on their own.
"""

from __future__ import annotations

import json
import logging
import os
import random
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

log = logging.getLogger("agent_relay_mcp")

# Where a project runs.
CONTAINER = "container"    # in its dev container
HOST = "host"              # directly on this machine, as the user

ALL = "*"                  # grant target: every project of the kind

HOST_WARNING = ("No dev container: Claude Code runs directly on this computer, as you, "
                "with full access to all files, credentials and programs.")
ON_HOST_WARNING = ("Runs on this computer instead of its dev container: full access as "
                   "you, and the container's protections (read-only files, missing "
                   "credentials, network limits) do not apply. Files the container could "
                   "change (project settings, hooks, scripts) then run with your rights.")
MAX_MESSAGES = 20           # per waiting request


class AccessError(Exception):
    """A control request that cannot be done; the text says why."""


@dataclass
class Limits:
    idle: int = 30 * 60                 # a grant ends after this long unused
    request_expiry: int = 30 * 60       # an unanswered request is dropped
    max_project: int = 12 * 3600
    max_all: int = 4 * 3600
    max_host: int = 2 * 3600            # host projects, and the kind "host"
    max_open_requests: int = 10


@dataclass
class Grant:
    id: str
    target: str                 # a project name, or ALL
    kind: str                   # CONTAINER or HOST
    created: float
    expires: float
    idle: int
    last_used: float
    end_on_lock: bool
    include_host_projects: bool = False     # only for target ALL
    # Where the project ran when the grant was given (CONTAINER or HOST). A
    # grant stops covering a project that has gained or lost its dev container
    # since: a container could otherwise delete its devcontainer.json and run
    # on the host under a grant meant for the container.
    natural: str = ""

    @property
    def full_access(self) -> bool:
        return self.kind == HOST or self.include_host_projects


@dataclass
class Request:
    id: str
    project: str
    number: int                 # said to the user by Claude, shown at the laptop
    messages: list[str]
    created: float
    expires: float
    full_access: bool           # it would run on the host, as the user
    # Where the chat asked it to run: "" (where the project runs), or HOST
    # for a project with a dev container. Only a wish; the approval decides.
    kind: str = ""


@dataclass
class Switch:
    """A project waits to change where it runs, until the other kind is idle."""
    project: str
    kind: str                   # where it runs afterwards
    requested: float
    expires: float
    grant: dict | None          # the grant to give then (None: whatever covers it)
    held: list[str] = field(default_factory=list)   # messages for afterwards


@dataclass
class Outcome:
    """How the last request of a project ended, for get_updates."""
    status: str                 # "denied" or "expired"
    number: int
    time: float


class Access:
    def __init__(self, path: Path | None, limits: Limits | None = None):
        self.path = path
        self.limits = limits or Limits()
        self.grants: dict[str, Grant] = {}
        self.requests: dict[str, Request] = {}
        self.switches: dict[str, Switch] = {}
        self.outcomes: dict[str, Outcome] = {}
        # Set while no control interface runs: nothing can be revoked then,
        # so no grant counts (fail closed).
        self.suspended = ""
        self._load()

    # --- persistence -------------------------------------------------------

    def _load(self) -> None:
        if not self.path or not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text())
            if not isinstance(raw, dict):
                raise ValueError("not a JSON object")
            grants = {g["id"]: Grant(**g) for g in raw.get("grants", [])}
            requests = {r["id"]: Request(**r) for r in raw.get("requests", [])}
            switches = {s["project"]: Switch(**s) for s in raw.get("switches", [])}
            outcomes = {name: Outcome(**o) for name, o in raw.get("outcomes", {}).items()}
            for g in grants.values():
                if not all(isinstance(v, (int, float)) and not isinstance(v, bool)
                           for v in (g.created, g.expires, g.idle, g.last_used)) \
                        or g.kind not in (CONTAINER, HOST) or not isinstance(g.target, str):
                    raise ValueError(f"grant {g.id} is malformed")
            for r in requests.values():
                if not isinstance(r.messages, list) or not isinstance(r.expires, (int, float)):
                    raise ValueError(f"request {r.id} is malformed")
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as error:
            # Fail closed: everything is locked. Keep the file for a look.
            log.error("cannot read %s (%s); every project is locked", self.path, error)
            try:
                self.path.replace(self.path.with_suffix(".broken"))
            except OSError:
                pass
            return
        self.grants, self.requests, self.switches, self.outcomes = \
            grants, requests, switches, outcomes

    def save(self) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {"grants": [asdict(g) for g in self.grants.values()],
                "requests": [asdict(r) for r in self.requests.values()],
                "switches": [asdict(s) for s in self.switches.values()],
                "outcomes": {name: asdict(o) for name, o in self.outcomes.items()}}
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        tmp.replace(self.path)

    # --- grants --------------------------------------------------------------

    def covering(self, project: str, natural: str, now: float, *,
                 idle_ok: bool = False) -> Grant | None:
        """The live grant that covers project, whose own kind is natural (where
        it runs without being told otherwise). A grant for the project itself
        wins over one for all projects. idle_ok: count grants that are only
        idle as live (to keep them alive while a request runs)."""
        if self.suspended:
            return None
        own = [g for g in self.grants.values()
               if g.target == project and self.live(g, now, idle_ok=idle_ok)
               and (not g.natural or g.natural == natural)]
        if own:
            return own[0]
        for g in self.grants.values():
            if g.target == ALL and self.live(g, now, idle_ok=idle_ok) \
                    and (natural == CONTAINER or g.include_host_projects):
                return g
        return None

    def live(self, grant: Grant, now: float, *, idle_ok: bool = False) -> bool:
        return now < grant.expires and (idle_ok or now - grant.last_used < grant.idle)

    def cap(self, target: str, kind: str, natural: str, seconds: int,
            include_host_projects: bool = False) -> int:
        """seconds, cut to the upper limit for such a grant."""
        if kind == HOST or (target != ALL and natural == HOST) or include_host_projects:
            limit = self.limits.max_host
        elif target == ALL:
            limit = self.limits.max_all
        else:
            limit = self.limits.max_project
        return max(60, min(seconds, limit))

    def add_grant(self, target: str, kind: str, seconds: int, now: float, *,
                  natural: str, end_on_lock: bool | None = None,
                  include_host_projects: bool = False) -> Grant:
        """A new grant, replacing one the project already has. Callers check
        that the other kind is idle first (see Relay.grant)."""
        if end_on_lock is None:
            # No grant ends with the screen lock unless asked to.
            end_on_lock = False
        grant = Grant(id=uuid.uuid4().hex[:12], target=target, kind=kind, created=now,
                      expires=now + self.cap(target, kind, natural, seconds,
                                             include_host_projects),
                      idle=self.limits.idle, last_used=now, end_on_lock=end_on_lock,
                      include_host_projects=include_host_projects,
                      natural="" if target == ALL else natural)
        for old in [g for g in self.grants.values() if g.target == target]:
            del self.grants[old.id]
        self.grants[grant.id] = grant
        return grant

    def touch(self, grant: Grant, now: float) -> None:
        grant.last_used = now

    def end(self, grant_id: str) -> Grant:
        grant = self.grants.pop(grant_id, None)
        if not grant:
            raise AccessError(f"There is no grant {grant_id}.")
        return grant

    def ended(self, now: float) -> list[Grant]:
        """Removes and returns the grants that have run out (time or idle)."""
        gone = [g for g in self.grants.values() if not self.live(g, now)]
        for g in gone:
            del self.grants[g.id]
        return gone

    def screen_locked(self) -> list[Grant]:
        gone = [g for g in self.grants.values() if g.end_on_lock]
        for g in gone:
            del self.grants[g.id]
        return gone

    # --- requests ------------------------------------------------------------

    def request(self, project: str, messages: list[str], now: float, *,
                full_access: bool, kind: str = "") -> tuple[Request, bool]:
        """The open request of project with messages added: (request, whether it
        is new). kind: where the chat asks it to run (see Request.kind)."""
        for r in self.requests.values():
            if r.project == project:
                if r.kind != kind:
                    where = "on the host" if r.kind == HOST else "where it normally runs"
                    raise AccessError(
                        f"A request to run {project} {where} (number {r.number}) already "
                        "waits for approval at the laptop; it has to be answered first.")
                if len(r.messages) + len(messages) > MAX_MESSAGES:
                    raise AccessError(f"{MAX_MESSAGES} messages already wait for approval "
                                      f"for {project}; wait until they are answered.")
                r.messages += messages
                return r, False
        if len(self.requests) >= self.limits.max_open_requests:
            raise AccessError("Too many requests are already waiting for approval at "
                              "the laptop; try again later.")
        taken = {r.number for r in self.requests.values()}
        number = random.choice([n for n in range(10, 100) if n not in taken])
        r = Request(id=uuid.uuid4().hex[:12], project=project, number=number,
                    messages=list(messages), created=now,
                    expires=now + self.limits.request_expiry, full_access=full_access,
                    kind=kind)
        self.requests[r.id] = r
        self.outcomes.pop(project, None)
        return r, True

    def open_request(self, project: str) -> Request | None:
        return next((r for r in self.requests.values() if r.project == project), None)

    def take_request(self, request_id: str, now: float | None = None,
                     messages: int | None = None) -> Request:
        """Removes and returns an open request, checked as in check_request."""
        r = self.check_request(request_id, now, messages)
        del self.requests[request_id]
        self.outcomes.pop(r.project, None)
        return r

    def check_request(self, request_id: str, now: float | None = None,
                      messages: int | None = None) -> Request:
        """An open request. With now: refuses one that has expired. With
        messages: refuses one that has changed since it was shown (more
        messages were added)."""
        r = self.requests.get(request_id)
        if not r or (now is not None and now >= r.expires):
            raise AccessError(f"There is no open request {request_id}; it may have "
                              "expired or been answered already.")
        if messages is not None and messages != len(r.messages):
            raise AccessError(f"Request {r.number} has changed since it was shown "
                              f"({len(r.messages)} messages now); review it again.")
        return r

    def deny(self, request_id: str, now: float) -> Request:
        r = self.take_request(request_id)
        self.outcomes[r.project] = Outcome("denied", r.number, now)
        return r

    def expired_requests(self, now: float) -> list[Request]:
        gone = [r for r in self.requests.values() if now >= r.expires]
        for r in gone:
            del self.requests[r.id]
            self.outcomes[r.project] = Outcome("expired", r.number, now)
        return gone
