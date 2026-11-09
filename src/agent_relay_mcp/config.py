"""Configuration: one TOML file, every key optional."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_CONFIG_FILE = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") \
    / "agent-relay-mcp/config.toml"
DEFAULT_STATE_DIR = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") \
    / "agent-relay-mcp"

DEFAULT_SYSTEM_PROMPT = (
    "You are not talking to the user directly. Another assistant relays the user's "
    "requests to you and reads your final reply, often to summarize it for the user "
    "by voice while they are away from the computer. Nobody watches your intermediate "
    "steps and nobody can answer questions while you work, so work autonomously. "
    "End every task with a short, plain final message: what you did, what you found, "
    "and anything the user has to decide. Avoid long code listings in that message."
)


@dataclass
class Config:
    # Where the MCP endpoint listens. Keep it on loopback and put a TLS reverse
    # proxy (or a tunnel to one) in front of it.
    listen_host: str = "127.0.0.1"
    listen_port: int = 8765
    path: str = "/mcp"
    # Host header values the endpoint accepts besides localhost: the public
    # host name the reverse proxy forwards, e.g. "example.org".
    public_hosts: list[str] = field(default_factory=list)

    # Every direct subdirectory is a project.
    projects_dir: Path = Path("~/Projects").expanduser()
    # Projects without .devcontainer/devcontainer.json run Claude Code directly
    # on this machine only when this is on.
    allow_host_projects: bool = False
    # Starts a project's dev container; "{project}" is the project's absolute path.
    start_command: list[str] = field(default_factory=lambda: [
        "devcontainer", "up", "--workspace-folder", "{project}"])
    start_timeout: int = 600
    docker: str = "docker"

    # How Claude Code is run in the container (or on the host).
    claude_command: str = "claude"
    permission_mode: str = "auto"
    model: str = ""
    append_system_prompt: str = DEFAULT_SYSTEM_PROMPT
    extra_args: list[str] = field(default_factory=list)

    # Longest wait a tool call may ask for. Claude's apps give up on tool calls
    # somewhere between 20 and 60 seconds.
    max_wait_seconds: int = 15
    state_dir: Path = DEFAULT_STATE_DIR

    # What this instance is called in the tool texts, e.g. "Laptop"; empty:
    # not mentioned.
    instance_name: str = ""
    # Grants and requests (see access.py). A grant ends after this long
    # without a message or a running request; a request nobody answers is
    # dropped after request_expiry_minutes. Upper limits per grant: for one
    # project, for all projects, and for anything with full access (host
    # projects, a project run on the host).
    grant_idle_minutes: int = 30
    request_expiry_minutes: int = 30
    max_grant_hours: int = 12
    max_all_grant_hours: int = 4
    max_host_grant_hours: int = 2
    # Offer the control interface (grants, approvals) on the D-Bus session bus,
    # for the GNOME extension and agent-relay-ctl. Needs the "desktop" extra.
    control_dbus: bool = True
    # Holds the inhibitor that keeps the computer awake (see keep_awake.py);
    # empty: never keep it awake.
    keep_awake_command: str = "systemd-inhibit"


def load(path: Path | None = None) -> Config:
    path = path or DEFAULT_CONFIG_FILE
    raw = tomllib.loads(path.read_text()) if path.is_file() else {}
    config = Config()
    known = set(Config.__dataclass_fields__)
    unknown = set(raw) - known
    if unknown:
        raise SystemExit(f"{path}: unknown keys: {', '.join(sorted(unknown))}")
    for key, value in raw.items():
        default = getattr(config, key)
        if isinstance(default, Path):
            value = Path(value).expanduser()
        elif isinstance(default, bool):
            if not isinstance(value, bool):
                raise SystemExit(f"{path}: {key} must be true or false")
        elif isinstance(default, int) and not isinstance(value, int):
            raise SystemExit(f"{path}: {key} must be a number")
        elif isinstance(default, list) and not (
                isinstance(value, list) and all(isinstance(v, str) for v in value)):
            raise SystemExit(f"{path}: {key} must be a list of strings")
        elif isinstance(default, str) and not isinstance(value, str):
            raise SystemExit(f"{path}: {key} must be a string")
        setattr(config, key, value)
    if not config.path.startswith("/"):
        raise SystemExit(f"{path}: path must start with /")
    if not 0 <= config.max_wait_seconds <= 60:
        raise SystemExit(f"{path}: max_wait_seconds must be between 0 and 60")
    for key in ("grant_idle_minutes", "request_expiry_minutes", "max_grant_hours",
                "max_all_grant_hours", "max_host_grant_hours"):
        if getattr(config, key) < 1:
            raise SystemExit(f"{path}: {key} must be at least 1")
    return config
