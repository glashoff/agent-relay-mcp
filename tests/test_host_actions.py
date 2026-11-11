"""Host actions, step 1: asking for them and denying them. Nothing is run.

"alpha" has no dev container, "web" has one.
"""

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from agent_relay_mcp import host_actions
from agent_relay_mcp import relay as relay_module
from agent_relay_mcp.access import Request
from agent_relay_mcp.config import load
from agent_relay_mcp.control import Control, ControlError
from agent_relay_mcp.host_actions import Action
from agent_relay_mcp.relay import Relay
from agent_relay_mcp.server import build
from agent_relay_mcp.targets import RelayError

FAKE = Path(__file__).with_name("fake_claude.py")

CONFIG = """
projects_dir = "{projects}"

[actions.deploy]
description = "Deploy from the host"
command = ["~/.local/bin/devcontainer-deploy", "--pause", "{{project}}"]
projects = ["web"]
requires_protected = ["deploy.py"]

[actions.approve]
command = ["/usr/local/bin/devcontainer-approve", "{{project}}"]
projects = "*"

[actions.install]
description = "Install or update the relay"
command = ["{{project}}/scripts/install.sh"]
projects = ["alpha"]
writable_by_host_runs = true
"""


class Clock:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def projects(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(relay_module, "SWEEP_SECONDS", 3600)    # sweeps by hand
    projects = tmp_path / "projects"
    (projects / "alpha").mkdir(parents=True)
    (projects / "web/.devcontainer").mkdir(parents=True)
    (projects / "web/.devcontainer/devcontainer.json").write_text("{}")
    return projects


@pytest.fixture
def config(tmp_path, projects):
    path = tmp_path / "config.toml"
    path.write_text(CONFIG.format(projects=projects))
    config = load(path)
    config.claude_command = f"{sys.executable} {FAKE}"
    config.state_dir = tmp_path / "state"
    config.allow_host_projects = True
    return config


@pytest.fixture
def no_subprocess(monkeypatch):
    """Asking for and denying host actions never starts a process. Only the
    relay's docker calls (listing running containers) are seen, and fail as
    without docker."""
    started = []

    def refuse(*args, **kwargs):
        started.append(args[0] if args else kwargs.get("args"))
        raise OSError("no processes in this test")
    for name in ("run", "Popen", "call", "check_call", "check_output"):
        monkeypatch.setattr(subprocess, name, refuse)
    monkeypatch.setattr(os, "system", refuse)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", refuse)
    monkeypatch.setattr(asyncio, "create_subprocess_shell", refuse)
    yield
    assert all(isinstance(cmd, list) and cmd[0] == "docker" for cmd in started), started


def relay_at(config, clock):
    relay = Relay(config)
    relay.clock = clock
    return relay


def events_of(relay):
    queue = relay.subscribe()

    def drain():
        found = []
        while not queue.empty():
            found.append(queue.get_nowait())
        return found
    return drain


# --- configuration -------------------------------------------------------------------

def test_the_actions_are_read_from_the_config(config, projects, tmp_path):
    deploy = config.actions["deploy"]
    assert deploy.command == (f"{tmp_path}/home/.local/bin/devcontainer-deploy", "--pause",
                              "{project}")
    assert deploy.projects == ("web",) and deploy.requires_protected == ("deploy.py",)
    assert not deploy.writable_by_host_runs
    assert config.actions["approve"].projects == "*"
    assert config.actions["approve"].applies_to("anything")
    assert config.actions["approve"].description == ""
    assert config.actions["install"].writable_by_host_runs
    assert config.actions["install"].resolve(projects / "alpha") == [
        f"{projects}/alpha/scripts/install.sh"]
    assert deploy.resolve(Path("/p/web"))[-1] == "/p/web"


def test_without_actions_there_are_none(tmp_path):
    assert load(tmp_path / "missing.toml").actions == {}


@pytest.mark.parametrize("entry, error", [
    ('command = ["/bin/true"]', "projects must be"),
    ('projects = "*"', "command must be"),
    ('command = []\nprojects = "*"', "command must be"),
    ('command = "/bin/true"\nprojects = "*"', "command must be"),
    ('command = ["true"]\nprojects = "*"', "absolute path"),
    ('command = ["{project}/../x"]\nprojects = "*"', "must not contain .."),
    ('command = ["/bin/true", 1]\nprojects = "*"', "command must be"),
    ('command = ["/bin/true"]\nprojects = []', "projects must be"),
    ('command = ["/bin/true"]\nprojects = "web"', "projects must be"),
    ('command = ["/bin/true"]\nprojects = "*"\nrun_as = "root"', "unknown keys: run_as"),
    ('command = ["/bin/true"]\nprojects = "*"\nrequires_protected = ["/etc/x"]',
     "relative to the project"),
    ('command = ["/bin/true"]\nprojects = "*"\nrequires_protected = ["a/../../x"]',
     "relative to the project"),
    ('command = ["/bin/true"]\nprojects = "*"\nwritable_by_host_runs = "yes"',
     "true or false"),
    ('command = ["/bin/true"]\nprojects = "*"\ndescription = 3', "description must be"),
])
def test_bad_actions_stop_the_relay_from_starting(tmp_path, entry, error):
    path = tmp_path / "config.toml"
    path.write_text(f"[actions.x]\n{entry}\n")
    with pytest.raises(SystemExit, match=error):
        load(path)


def test_action_names_are_letters_digits_and_hyphens(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[actions."de ploy"]\ncommand = ["/bin/true"]\nprojects = "*"\n')
    with pytest.raises(SystemExit, match="letters, digits and hyphens"):
        load(path)
    path.write_text('[actions.deploy-dry]\ncommand = ["/bin/true"]\nprojects = "*"\n')
    assert "deploy-dry" in load(path).actions
    path.write_text('actions = 3\n')
    with pytest.raises(SystemExit, match="table"):
        load(path)


def test_writable_actions_need_projects_without_a_dev_container(tmp_path, projects):
    path = tmp_path / "config.toml"
    action = '[actions.install]\ncommand = ["{project}/install.sh"]\nwritable_by_host_runs = true\n'
    path.write_text(f'projects_dir = "{projects}"\n{action}projects = ["web"]\n')
    with pytest.raises(SystemExit, match="web, which has a dev container"):
        load(path)
    path.write_text(f'projects_dir = "{projects}"\n{action}projects = "*"\n')
    with pytest.raises(SystemExit, match="needs a list of projects"):
        load(path)
    # A project that does not exist (yet) is no error.
    path.write_text(f'projects_dir = "{projects}"\n{action}projects = ["alpha", "later"]\n')
    assert load(path).actions["install"].writable_by_host_runs


# --- asking ------------------------------------------------------------------------------

def test_asking_makes_a_waiting_request_and_runs_nothing(config, projects, no_subprocess):
    async def scenario():
        relay = relay_at(config, Clock())
        events = events_of(relay)
        answer = await relay.request_host_action("Web", "deploy", "  the fix is merged ")
        (request,) = relay.host_actions.waiting()
        assert f"number {request.number}" in answer and "Tell the user" in answer
        assert "Nothing runs before it is approved" in answer
        assert "expires in 30 min" in answer
        assert request.project == "web" and request.action == "deploy"
        assert request.note == "the fix is merged" and not request.writable
        assert request.command[1:] == ["--pause", str(projects / "web")]
        assert 10 <= request.number <= 99
        (event,) = [e for e in events() if e["type"] == "host_action_requested"]
        assert event["id"] == request.id and event["number"] == request.number
        assert event["project"] == "web" and event["action"] == "deploy"
        log = (projects / "web/.relay/log.md").read_text()
        assert f"Host action deploy requested (number {request.number})" in log
        # What the laptop shows.
        (shown,) = relay.state()["host_actions"]
        assert shown["command"] == request.command and shown["note"] == "the fix is merged"
        assert shown["description"] == "Deploy from the host" and not shown["writable"]
        assert "may have been changed by Claude" in \
            relay.state()["warnings"]["writable_action"]
        # And what Claude sees.
        assert f"host action deploy waiting (number {request.number})" in \
            await relay.list_projects()
        assert f"number {request.number}" in await relay.get_updates("web", 0)
        assert "is waiting for approval" in await relay.get_host_action(request.number)
        assert not relay.conversations and not relay.access.requests
    run(scenario())


def test_writable_actions_are_shown_with_a_warning(config):
    async def scenario():
        relay = relay_at(config, Clock())
        await relay.request_host_action("alpha", "install")
        (shown,) = relay.state()["host_actions"]
        assert shown["writable"]
    run(scenario())


def test_unknown_actions_and_projects_are_refused(config, projects):
    async def scenario():
        relay = relay_at(config, Clock())
        with pytest.raises(RelayError, match="Host actions for it: deploy, approve"):
            await relay.request_host_action("web", "install")
        with pytest.raises(RelayError, match="Host actions for it: approve, install"):
            await relay.request_host_action("alpha", "deploy")
        with pytest.raises(RelayError, match="no project named"):
            await relay.request_host_action("nowhere", "approve")
        with pytest.raises(RelayError, match="too long"):
            await relay.request_host_action("web", "deploy", "x" * 1001)
        config.actions = {}
        with pytest.raises(RelayError, match="No host actions are configured for web"):
            await relay.request_host_action("web", "deploy")
        assert not relay.host_actions.requests
    run(scenario())


def test_a_writable_action_is_refused_once_the_project_has_a_dev_container(config,
                                                                           projects):
    async def scenario():
        relay = relay_at(config, Clock())
        (projects / "alpha/.devcontainer.json").write_text("{}")
        with pytest.raises(RelayError, match="now has a dev container"):
            await relay.request_host_action("alpha", "install")
    run(scenario())


def test_one_waiting_action_per_project_and_a_few_in_total(config, projects):
    async def scenario():
        relay = relay_at(config, Clock())
        await relay.request_host_action("web", "deploy")
        number = relay.host_actions.waiting()[0].number
        with pytest.raises(RelayError, match=f"number {number}"):
            await relay.request_host_action("web", "approve")
        for name in ("p1", "p2", "p3", "p4", "p5"):
            (projects / name).mkdir()
        for name in ("p1", "p2", "p3", "p4"):
            await relay.request_host_action(name, "approve")
        with pytest.raises(RelayError, match="Too many host actions"):
            await relay.request_host_action("p5", "approve")
        assert len({r.number for r in relay.host_actions.waiting()}) == 5
    run(scenario())


def test_numbers_never_match_open_access_requests(config, monkeypatch):
    # Every number but two is taken by access requests (faked), so the
    # action has to take one of those, and the next access request the other.
    async def scenario():
        clock = Clock()
        relay = relay_at(config, clock)
        for n in range(10, 98):
            relay.access.requests[f"r{n}"] = Request(
                id=f"r{n}", project=f"x{n}", number=n, messages=["m"], created=0,
                expires=clock.now + 3600, full_access=False)
        await relay.request_host_action("web", "deploy")
        (action,) = relay.host_actions.waiting()
        assert action.number in (98, 99)
        relay.access.limits.max_open_requests = 1000
        await relay.send_message("alpha", "hi", False, 0)
        request = relay.access.open_request("alpha")
        assert request.number == ({98, 99} - {action.number}).pop()
    run(scenario())


# --- denying and expiry ---------------------------------------------------------------------

def test_denying_at_the_laptop(config, projects, no_subprocess):
    async def scenario():
        relay = relay_at(config, Clock())
        events = events_of(relay)
        await relay.request_host_action("web", "deploy")
        (request,) = relay.host_actions.waiting()
        await Control(relay).deny_action(request.id)
        assert request.status == "denied" and not relay.host_actions.waiting()
        assert {"type": "host_action_answered", "id": request.id, "project": "web",
                "number": request.number, "status": "denied"}.items() <= \
            [e for e in events() if e["type"] == "host_action_answered"][0].items()
        assert "denied at the laptop; nothing was run" in \
            await relay.get_host_action(request.number)
        assert "was denied" in (projects / "web/.relay/log.md").read_text()
        assert not relay.state()["host_actions"]
        with pytest.raises(ControlError, match="no waiting host action"):
            await Control(relay).deny_action(request.id)
        with pytest.raises(ControlError, match="no waiting host action"):
            await Control(relay).deny_action("nothing")
        with pytest.raises(RelayError, match="no host action with number"):
            await relay.get_host_action(100)
        # The project can ask again.
        await relay.request_host_action("web", "deploy")
    run(scenario())


def test_unanswered_actions_expire(config, projects, no_subprocess):
    async def scenario():
        clock = Clock()
        relay = relay_at(config, clock)
        events = events_of(relay)
        await relay.request_host_action("web", "deploy")
        (request,) = relay.host_actions.waiting()
        clock.now += 29 * 60
        await relay.sweep()
        assert request.status == "waiting"
        clock.now += 2 * 60
        with pytest.raises(ControlError):
            await Control(relay).deny_action(request.id)      # too late
        await relay.sweep()
        assert request.status == "expired"
        assert [e["id"] for e in events() if e["type"] == "host_action_expired"] == \
            [request.id]
        assert "expired without an answer; nothing was run" in \
            await relay.get_host_action(request.number)
        assert "expired without an answer" in (projects / "web/.relay/log.md").read_text()
    run(scenario())


def test_history_is_pruned():
    store = host_actions.HostActions(None)
    action = Action("a", "", ("/bin/true",), "*")
    now = 0.0
    for i in range(60):
        r = store.request(f"p{i}", action, ["/bin/true"], "", now)
        store.deny(r.id, now)
        now += 60
    assert len(store.requests) == host_actions.KEEP_FINISHED
    store.request("late", action, ["/bin/true"], "", now + 8 * 24 * 3600)
    assert [r.project for r in store.requests.values()] == ["late"]


# --- persistence ---------------------------------------------------------------------------

def test_requests_survive_a_restart(config):
    async def scenario():
        clock = Clock()
        relay = relay_at(config, clock)
        await relay.request_host_action("web", "deploy", "please")
        await relay.request_host_action("alpha", "approve")
        (denied,) = [r for r in relay.host_actions.waiting() if r.project == "alpha"]
        await relay.deny_action(denied.id)
        path = config.state_dir / "host_actions.json"
        assert path.stat().st_mode & 0o777 == 0o600
        again = relay_at(config, clock)
        (waiting,) = again.host_actions.waiting()
        assert waiting.note == "please" and waiting.project == "web"
        assert again.host_actions.get(denied.id).status == "denied"
    run(scenario())


def test_a_broken_file_drops_the_requests(config, caplog):
    path = config.state_dir / "host_actions.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"requests": [{"id": "x", "number": "47"}]}))
    relay = Relay(config)
    assert not relay.host_actions.requests
    assert "host action requests are dropped" in caplog.text
    assert path.with_suffix(".broken").exists() and not path.exists()


# --- the MCP tools --------------------------------------------------------------------------

def test_the_tools_ask_and_tell(config, no_subprocess):
    server, relay = build(config)

    async def scenario():
        tools = {t.name: t for t in await server.list_tools()}
        assert {"request_host_action", "get_host_action"} <= set(tools)
        assert "explicit request" in tools["request_host_action"].description
        result = await server.call_tool("request_host_action",
                                        {"project": "web", "action": "deploy"})
        (request,) = relay.host_actions.waiting()
        assert f"number {request.number}" in result.content[0].text
        result = await server.call_tool("get_host_action", {"number": request.number})
        assert "waiting for approval" in result.content[0].text
    asyncio.run(scenario())
