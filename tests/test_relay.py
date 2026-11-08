"""The relay end to end, with tests/fake_claude.py in place of Claude Code and
projects that run on the host (no dev container needed)."""

import asyncio
import json
import sys
from pathlib import Path

import pytest

from agent_relay_mcp import targets
from agent_relay_mcp.config import Config
from agent_relay_mcp.relay import Relay
from agent_relay_mcp.targets import RelayError

FAKE = Path(__file__).with_name("fake_claude.py")


@pytest.fixture
def setup(tmp_path, monkeypatch):
    # Runs keep their files under $HOME/.cache; keep them in the test's directory.
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    projects = tmp_path / "projects"
    (projects / "alpha").mkdir(parents=True)
    (projects / "Beta").mkdir()
    (projects / ".hidden").mkdir()
    config = Config(projects_dir=projects, allow_host_projects=True,
                    claude_command=f"{sys.executable} {FAKE}",
                    state_dir=tmp_path / "state", max_wait_seconds=10)
    return config, projects


def run(coro):
    return asyncio.run(coro)


async def until_done(relay, project, seconds=10):
    for _ in range(seconds * 4):
        report = await relay.get_updates(project, 0)
        if "still working" not in report and "being started" not in report:
            return report
        await asyncio.sleep(0.25)
    raise AssertionError("never finished: " + report)


async def until_finished(relay, project, seconds=10):
    """Waits for a run to end without picking up its reply."""
    for _ in range(seconds * 4):
        turn = relay.conversations[project].turn
        await relay._refresh(targets.find_project(relay.config, project),
                             relay.conversations[project])
        if turn.phase not in ("starting", "running"):
            return
        await asyncio.sleep(0.25)
    raise AssertionError(f"{project} never finished")


def test_list_projects_skips_hidden(setup):
    config, _ = setup
    listing = run(Relay(config).list_projects())
    assert "alpha" in listing and "Beta" in listing and ".hidden" not in listing


def test_host_projects_need_opt_in(setup):
    config, _ = setup
    config.allow_host_projects = False
    with pytest.raises(RelayError, match="no dev container"):
        run(Relay(config).send_message("alpha", "hi", False, 0))


def test_send_returns_at_once_and_reply_arrives_later(setup):
    config, _ = setup

    async def scenario():
        relay = Relay(config)
        first = await relay.send_message("alpha", "sleep 2", False, 0)
        assert "working on it" in first
        early = await relay.get_updates("alpha", 0)
        assert "still working" in early or "being started" in early
        report = await until_done(relay, "alpha")
        assert "finished" in report and "Done: sleep 2 (new)" in report
        assert "Bash: List files" in report
        # Reported once; asking again says so but still gives the reply.
        again = await relay.get_updates("alpha", 0)
        assert "already reported once" in again and "Done: sleep 2" in again
    run(scenario())


def test_wait_seconds_returns_quick_replies_directly(setup):
    config, _ = setup
    report = run(Relay(config).send_message("alpha", "quick", False, 8))
    assert "Final reply" in report and "Done: quick (new)" in report


def test_follow_up_resumes_and_new_conversation_does_not(setup):
    config, projects = setup

    async def scenario():
        relay = Relay(config)
        await relay.send_message("alpha", "one", False, 8)
        session = relay.conversations["alpha"].session_id
        second = await relay.send_message("alpha", "two", False, 8)
        assert "(resumed)" in second
        assert relay.conversations["alpha"].session_id == session
        args = json.loads((projects / "alpha/fake-args.txt").read_text())
        assert args[args.index("--resume") + 1] == session
        third = await relay.send_message("alpha", "three", True, 8)
        assert "(new)" in third
        assert relay.conversations["alpha"].session_id != session
    run(scenario())


def test_busy_project_refuses_a_second_message(setup):
    config, _ = setup

    async def scenario():
        relay = Relay(config)
        await relay.send_message("alpha", "sleep 3", False, 0)
        await asyncio.sleep(0.5)
        with pytest.raises(RelayError, match="still working"):
            await relay.send_message("alpha", "more", False, 0)
        await until_done(relay, "alpha")
    run(scenario())


def test_crash_is_reported_with_stderr(setup):
    config, _ = setup
    report = run(Relay(config).send_message("alpha", "crash", False, 8))
    assert "failed" in report and "boom" in report


def test_stop_ends_the_run(setup):
    config, _ = setup

    async def scenario():
        relay = Relay(config)
        await relay.send_message("alpha", "sleep 30", False, 0)
        await asyncio.sleep(1)
        assert "Stopped" in await relay.stop("alpha")
        assert "Stopped on request" in await relay.get_updates("alpha", 0)
        # The conversation can go on.
        assert "(resumed)" in await relay.send_message("alpha", "next", False, 8)
    run(scenario())


def test_a_restarted_server_finds_the_run_and_its_reply(setup):
    config, _ = setup

    async def scenario():
        relay = Relay(config)
        await relay.send_message("alpha", "sleep 1", False, 0)
        await asyncio.sleep(0.5)
        restarted = Relay(config)          # reads state.json, knows only the ids
        report = await until_done(restarted, "alpha")
        assert "Done: sleep 1 (new)" in report
    run(scenario())


def test_project_names_match_case_insensitively(setup):
    config, _ = setup
    report = run(Relay(config).send_message("beta", "hi", False, 8))
    assert "Beta" in report


def test_unknown_project_lists_the_real_ones(setup):
    config, _ = setup
    with pytest.raises(RelayError, match="alpha"):
        run(Relay(config).send_message("gamma", "hi", False, 0))


def listed(listing, name):
    return next(line for line in listing.splitlines() if line.startswith(f"- {name} "))


def test_a_restart_keeps_finished_turns_finished(setup):
    config, projects = setup
    (projects / "delta").mkdir()

    async def scenario():
        relay = Relay(config)
        await relay.send_message("alpha", "quick", False, 8)       # reply reported
        await relay.send_message("Beta", "quick", False, 0)        # reply not picked up
        await until_finished(relay, "Beta")
        await relay.send_message("delta", "sleep 3", False, 0)     # still running
        await asyncio.sleep(0.5)

        restarted = Relay(config)
        listing = await restarted.list_projects()
        assert listed(listing, "alpha").endswith("host)")
        assert "a reply is waiting" in listed(listing, "Beta")
        assert "Claude Code is working" in listed(listing, "delta")

        assert "already reported" in await restarted.get_updates("alpha", 0)
        report = await restarted.get_updates("Beta", 0)
        assert "finished" in report and "Done: quick (new)" in report
        assert "a reply is waiting" not in listed(await restarted.list_projects(), "Beta")

        # list_projects notices on its own that the running one has ended.
        for _ in range(40):
            if "a reply is waiting" in listed(await restarted.list_projects(), "delta"):
                break
            await asyncio.sleep(0.25)
        else:
            raise AssertionError("delta never finished")
        assert "Done: sleep 3 (new)" in await restarted.get_updates("delta", 0)
    run(scenario())


def test_a_restart_with_the_container_stopped(setup, monkeypatch):
    config, projects = setup
    (projects / "delta").mkdir()

    async def scenario():
        relay = Relay(config)
        await relay.send_message("alpha", "quick", False, 0)       # reply not picked up
        await until_finished(relay, "alpha")
        await relay.send_message("delta", "sleep 30", False, 0)    # running
        await asyncio.sleep(0.5)

        # The containers stop with the server down.
        real_target_for = targets.target_for
        monkeypatch.setattr(targets, "target_for",
                            lambda config, project, *, start: None if not start
                            else real_target_for(config, project, start=start))
        restarted = Relay(config)
        listing = await restarted.list_projects()
        assert "a reply is waiting" in listed(listing, "alpha")
        assert "working" not in listed(listing, "delta")
        assert "not running any more" in await restarted.get_updates("delta", 0)

        report = await restarted.get_updates("alpha", 0)
        assert "not running now" in report and "failed" not in report
        assert "a reply is waiting" in listed(await restarted.list_projects(), "alpha")

        # Once the container runs again, the reply is there.
        monkeypatch.setattr(targets, "target_for", real_target_for)
        assert "Done: quick (new)" in await restarted.get_updates("alpha", 0)
        await relay.stop("delta")
    run(scenario())


def test_a_failed_turn_after_a_restart_still_resumes(setup):
    config, _ = setup

    async def scenario():
        relay = Relay(config)
        await relay.send_message("alpha", "crash", False, 8)   # the session was created
        session = relay.conversations["alpha"].session_id
        restarted = Relay(config)
        assert "(resumed)" in await restarted.send_message("alpha", "next", False, 8)
        assert restarted.conversations["alpha"].session_id == session
    run(scenario())
