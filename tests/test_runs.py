"""What a run's directory may contain when something else than the relay
wrote it (the container can write its own run directories), and when the
relay starts a dev container."""

import json
import sys
import uuid

import pytest

from agent_relay_mcp import relay as relay_module
from agent_relay_mcp import targets
from agent_relay_mcp.config import Config
from agent_relay_mcp.relay import Relay, Turn


@pytest.fixture
def setup(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    (tmp_path / "projects/alpha").mkdir(parents=True)
    config = Config(projects_dir=tmp_path / "projects", allow_host_projects=True,
                    claude_command=sys.executable, state_dir=tmp_path / "state")
    project = targets.Project("alpha", tmp_path / "projects/alpha")
    target = targets.Target(project, None, None, str(project.path))
    return config, target, home


def run_dir(home, turn):
    d = home / ".cache/agent-relay-mcp" / turn.id
    d.mkdir(parents=True)
    return d


def test_a_planted_pid_of_1_is_not_trusted(setup):
    config, target, home = setup
    relay = Relay(config)
    turn = Turn(str(uuid.uuid4()), "x", 0)
    d = run_dir(home, turn)
    (d / "pid").write_text("1\n")
    (d / "out.jsonl").write_text("")
    relay._poll(target, turn)
    assert turn.phase == "failed" and "gone" in turn.error
    relay._stop(target, turn)            # must not run kill -- -1
    relay._stop(target, Turn(turn.id, "x", 0, pid=0))


def test_the_pid_seen_at_launch_wins_over_the_file(setup):
    config, target, home = setup
    relay = Relay(config)
    import subprocess
    sleeper = subprocess.Popen(["sleep", "30"])
    try:
        turn = Turn(str(uuid.uuid4()), "x", 0, pid=sleeper.pid)
        d = run_dir(home, turn)
        (d / "pid").write_text("999999\n")          # changed afterwards
        (d / "out.jsonl").write_text("")
        relay._poll(target, turn)
        assert turn.phase == "running"
    finally:
        sleeper.kill()


def test_output_is_read_in_bounded_pieces(setup, monkeypatch):
    config, target, home = setup
    monkeypatch.setattr(relay_module, "READ_LIMIT", 1000)
    relay = Relay(config)
    turn = Turn(str(uuid.uuid4()), "x", 0)
    d = run_dir(home, turn)
    (d / "exit").write_text("0\n")
    result = json.dumps({"type": "result", "subtype": "success", "result": "ok"})
    (d / "out.jsonl").write_text("x" * 5000 + "\n" + result + "\n")
    for _ in range(10):
        relay._poll(target, turn)
        if turn.output.result:
            break
    assert turn.output.result == "ok"           # the huge line was skipped


def inspect_output(mounts):
    return json.dumps([{"Mounts": mounts}])


@pytest.mark.parametrize("previous,mounts,allowed", [
    ("", [], True),                                         # never had a container
    ("c1", [{"Source": "{p}/.devcontainer", "RW": False}], True),
    ("c1", [{"Source": "{p}/.devcontainer", "RW": True}], False),
    ("c1", [{"Source": "{p}", "RW": True}], False),          # writable through the folder
])
def test_dev_containers_that_could_change_their_config_are_not_started(
        setup, monkeypatch, previous, mounts, allowed):
    config, target, _ = setup
    project = target.project
    (project.path / ".devcontainer").mkdir()
    (project.path / ".devcontainer/devcontainer.json").write_text("{}")
    mounts = [{**m, "Source": m["Source"].format(p=project.path)} for m in mounts]

    def docker(config, *args, timeout=30):
        if args[0] == "ps":
            return previous
        return inspect_output(mounts)
    monkeypatch.setattr(targets, "_docker", docker)
    if allowed:
        targets.may_start(config, project)
    else:
        with pytest.raises(targets.RelayError, match="does not start it"):
            targets.may_start(config, project)
        config.start_writable_devcontainers = True
        targets.may_start(config, project)


def test_old_runs_files_are_not_trusted_for_kill(setup):
    # The shell that reads a pid file rejects anything but a plain number > 1.
    shell = relay_module._pid(Turn(str(uuid.uuid4()), "x", 0))
    assert '0|1) p=""' in shell
    assert relay_module._pid(Turn(str(uuid.uuid4()), "x", 0, pid=4242)) == "p=4242"


def test_host_runs_get_a_scope_of_their_own_under_systemd(setup, monkeypatch):
    config, target, _ = setup
    relay = Relay(config)
    turn = Turn(str(uuid.uuid4()), "x", 0)
    monkeypatch.setattr(relay_module.shutil, "which", lambda name: "/usr/bin/" + name)
    assert relay._scope(target, turn) == ""                 # not a systemd service
    monkeypatch.setenv("INVOCATION_ID", "abc")
    prefix = relay._scope(target, turn)
    assert prefix.startswith("systemd-run --user --scope") and turn.id in prefix
    container = targets.Target(target.project, "cid", "vscode", "/workspaces/alpha")
    assert relay._scope(container, turn) == ""              # lives in its container
    config.host_run_scope = False
    assert relay._scope(target, turn) == ""


def test_the_launch_reports_the_pid_the_runner_wrote(setup):
    config, target, home = setup
    config.claude_command = "sh -c 'sleep 30'"
    relay = Relay(config)
    turn = Turn(str(uuid.uuid4()), "x", 0)
    relay._launch(target, turn, str(uuid.uuid4()), False)
    try:
        d = home / ".cache/agent-relay-mcp" / turn.id
        assert turn.pid > 1 and int((d / "pid").read_text()) == turn.pid
        with open(f"/proc/{turn.pid}/stat") as f:      # the runner leads its group
            assert int(f.read().split(") ")[1].split()[2]) == turn.pid
    finally:
        relay._stop(target, turn)
