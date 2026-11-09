"""Where Claude Code runs for a project: in its dev container, or on this machine."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .config import Config


class RelayError(Exception):
    """A failure to report to the calling assistant as it is."""


@dataclass(frozen=True)
class Project:
    name: str
    path: Path

    @property
    def has_devcontainer(self) -> bool:
        return (self.path / ".devcontainer/devcontainer.json").is_file() \
            or (self.path / ".devcontainer.json").is_file()


@dataclass(frozen=True)
class Target:
    """A running place to execute commands in, as the user Claude Code runs as."""
    project: Project
    container_id: str | None    # None: directly on this machine
    user: str | None
    workdir: str


def list_projects(config: Config) -> list[Project]:
    root = config.projects_dir
    if not root.is_dir():
        raise RelayError(f"The projects directory {root} does not exist.")
    return [Project(p.name, p.resolve()) for p in sorted(root.iterdir(), key=lambda p: p.name.lower())
            if p.is_dir() and not p.name.startswith(".")]


def find_project(config: Config, name: str) -> Project:
    name = name.strip()
    if not name or "/" in name or name.startswith("."):
        raise RelayError(f"Invalid project name: {name!r}")
    projects = list_projects(config)
    for project in projects:
        if project.name == name:
            return project
    # Spoken names rarely match exactly; accept an unambiguous case-insensitive match.
    folded = [p for p in projects if p.name.casefold() == name.casefold()]
    if len(folded) == 1:
        return folded[0]
    raise RelayError(f"There is no project named {name!r}. "
                     f"Projects: {', '.join(p.name for p in projects)}")


def _docker(config: Config, *args: str, timeout: int = 30) -> str:
    try:
        result = subprocess.run([config.docker, *args], capture_output=True,
                                text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RelayError(f"docker {args[0]} failed: {error}") from error
    if result.returncode != 0:
        raise RelayError(f"docker {args[0]} failed: {result.stderr.strip()[-500:]}")
    return result.stdout


def running_container(config: Config, project: Project) -> str | None:
    """The id of the project's running dev container, by the label the dev
    container CLI puts on every container it creates."""
    out = _docker(config, "ps", "-q", "--filter",
                  f"label=devcontainer.local_folder={project.path}")
    ids = out.split()
    return ids[0] if ids else None


def running_folders(config: Config) -> set[Path]:
    """Project folders whose dev container is running, in one docker call."""
    out = _docker(config, "ps", "--filter", "label=devcontainer.local_folder",
                  "--format", '{{.Label "devcontainer.local_folder"}}')
    return {Path(line) for line in out.splitlines() if line.strip()}


def start_container(config: Config, project: Project) -> str:
    command = [part.replace("{project}", str(project.path)) for part in config.start_command]
    try:
        result = subprocess.run(command, capture_output=True, text=True,
                                stdin=subprocess.DEVNULL, timeout=config.start_timeout)
    except subprocess.TimeoutExpired as error:
        raise RelayError(f"Starting the dev container took longer than "
                         f"{config.start_timeout} seconds.") from error
    except OSError as error:
        raise RelayError(f"Cannot run {command[0]}: {error}") from error
    container_id = running_container(config, project)
    if result.returncode != 0 or not container_id:
        output = (result.stderr.strip() or result.stdout.strip())[-1500:]
        raise RelayError(f"The dev container of {project.name} did not start "
                         f"(exit code {result.returncode}): {output}")
    return container_id


def _container_details(config: Config, container_id: str, project: Project) -> tuple[str, str]:
    """(user, workspace folder) of a dev container."""
    info = json.loads(_docker(config, "inspect", container_id))[0]
    user = ""
    metadata = (info.get("Config", {}).get("Labels") or {}).get("devcontainer.metadata")
    if metadata:
        try:
            # A list of fragments (image, features, devcontainer.json); later ones win.
            for fragment in json.loads(metadata):
                user = fragment.get("remoteUser") or fragment.get("containerUser") or user
        except (ValueError, AttributeError):
            pass
    user = user or info.get("Config", {}).get("User") or "root"
    workdir = ""
    for mount in info.get("Mounts") or []:
        if Path(mount.get("Source", "")) == project.path:
            workdir = mount.get("Destination", "")
            break
    if not workdir:
        raise RelayError(f"Cannot tell where {project.name} is mounted in its container.")
    return user, workdir


def target_for(config: Config, project: Project, *, start: bool,
               on_host: bool = False) -> Target | None:
    """Where Claude Code runs for project; None if its container is not running
    and start is off. on_host: on this machine even if it has a dev container
    (only ever set from a grant, see access.py)."""
    if on_host or not project.has_devcontainer:
        if not config.allow_host_projects:
            raise RelayError(f"{project.name} cannot run on this machine: running "
                             "Claude Code outside a dev container is turned off.")
        return Target(project, None, None, str(project.path))
    container_id = running_container(config, project)
    if not container_id:
        if not start:
            return None
        container_id = start_container(config, project)
    user, workdir = _container_details(config, container_id, project)
    return Target(project, container_id, user, workdir)


def run_in(config: Config, target: Target, script: str, *, stdin: bytes = b"",
           timeout: int = 30) -> subprocess.CompletedProcess:
    """Runs a shell script where Claude Code lives for target, in its folder.

    bash -l, because login profiles are what put user installs such as
    ~/.local/bin/claude on the PATH.
    """
    if target.container_id:
        command = [config.docker, "exec", "-i", "-u", target.user or "root",
                   "-w", target.workdir, target.container_id, "bash", "-lc", script]
        cwd = None
    else:
        command = ["bash", "-lc", script]
        cwd = target.workdir
    try:
        return subprocess.run(command, input=stdin, capture_output=True,
                              timeout=timeout, cwd=cwd)
    except subprocess.TimeoutExpired as error:
        raise RelayError(f"A command in {target.project.name} timed out.") from error
    except OSError as error:
        raise RelayError(f"Cannot run a command in {target.project.name}: {error}") from error
