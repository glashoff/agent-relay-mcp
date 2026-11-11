"""A run stays open between messages: background tasks, the idle timeout,
what closes a run, and a restart while one is open. End to end with
tests/fake_claude.py.

"alpha" has no dev container (a host project). "web" has one, stubbed to run
on the host too, so its two kinds can be told apart by their conversations.
"""

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

from agent_relay_mcp import relay as relay_module
from agent_relay_mcp import targets
from agent_relay_mcp.access import HOST
from agent_relay_mcp.config import Config
from agent_relay_mcp.control import Control
from agent_relay_mcp.relay import Relay

FAKE = Path(__file__).with_name("fake_claude.py")


@pytest.fixture
def config(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(relay_module, "QUEUE_POLL_SECONDS", 0.2)
    monkeypatch.setattr(relay_module, "SWEEP_SECONDS", 3600)    # sweeps by hand
    projects = tmp_path / "projects"
    (projects / "alpha").mkdir(parents=True)
    (projects / "web/.devcontainer").mkdir(parents=True)
    (projects / "web/.devcontainer/devcontainer.json").write_text("{}")
    real_target_for = targets.target_for
    monkeypatch.setattr(targets, "running_container", lambda config, project: "stub")
    monkeypatch.setattr(targets, "target_for",
                        lambda config, project, *, start, on_host=False: real_target_for(
                            config, project, start=start, on_host=True))
    return Config(projects_dir=projects, allow_host_projects=True,
                  claude_command=f"{sys.executable} {FAKE}",
                  state_dir=tmp_path / "state", max_wait_seconds=10)


def run(coro):
    return asyncio.run(coro)


async def granted(config, *projects):
    relay = Relay(config)
    control = Control(relay)
    for project in projects:
        await control.grant(project, full_access_ok=True)
    return relay, control


async def until(condition, what, seconds=15):
    for _ in range(seconds * 10):
        if await condition():
            return
        await asyncio.sleep(0.1)
    raise AssertionError(f"never: {what}")


async def until_phase(relay, key, *phases, seconds=15):
    project = targets.find_project(relay.config, key.split("/")[0])

    async def reached():
        async with relay._lock(project.name):      # as the relay itself does
            await relay._refresh(project, relay.conversations[key])
        return relay.conversations[key].turn.phase in phases
    await until(reached, f"{key} in {phases}", seconds)


async def until_ended(relay, key, seconds=15):
    project = targets.find_project(relay.config, key.split("/")[0])

    async def ended():
        async with relay._lock(project.name):      # as the relay itself does
            await relay._refresh(project, relay.conversations[key])
        return relay.conversations[key].turn.ended
    await until(ended, f"the run of {key} ended", seconds)


def alive(turn) -> bool:
    try:
        os.kill(turn.pid, 0)
        return True
    except OSError:
        return False


def events_of(relay):
    queue = relay.subscribe()

    def taken():
        found = []
        while not queue.empty():
            found.append(queue.get_nowait())
        return found
    return taken


def test_a_run_stays_open_and_takes_the_next_message(config):
    async def scenario():
        relay, _ = await granted(config, "alpha")
        report = await relay.send_message("alpha", "one", False, 8)
        assert "Done: one (new)" in report and "stays open" in report
        turn = relay.conversations["alpha"].turn
        assert turn.phase == "finished" and turn.open and alive(turn)
        assert "open for follow-ups" in await relay.list_projects()
        answer = await relay.send_message("alpha", "two", False, 8)
        assert "Done: two (new)" in answer and "Done: one" not in answer
        second = relay.conversations["alpha"].turn
        assert second is not turn and second.run_id == turn.run_id and second.pid == turn.pid
        # The log shows each exchange as a request of its own.
        text = (config.projects_dir / "alpha/.relay/log.md").read_text()
        assert text.count("· Request**") == 2 and text.count("· Reply**") == 2
    run(scenario())


def test_claude_code_may_wait_for_background_tasks_without_a_limit(config):
    async def scenario():
        relay, _ = await granted(config, "alpha")
        await relay.send_message("alpha", "quick", False, 8)
        env = json.loads((config.projects_dir / "alpha/fake-env.txt").read_text())
        assert env["CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS"] == "0"
    run(scenario())


def test_messages_pass_in_while_a_background_task_runs(config):
    async def scenario():
        relay, _ = await granted(config, "alpha")
        taken = events_of(relay)
        first = await relay.send_message("alpha", "background 4", False, 8)
        # The first result came, but a background task is open: still working.
        assert "has replied, but is still working" in first
        assert "1 background task(s) still running: Background job 1" in first
        assert "Done: background 4 (new)" in first
        turn = relay.conversations["alpha"].turn
        assert turn.phase == "running" and relay.keep_awake.state()
        # Not queued: passed in, and answered while the task still runs.
        answer = await relay.send_message("alpha", "meanwhile", False, 8)
        assert "Passed the message into the running request" in answer
        assert "Done: meanwhile (new)" in answer and "still working" in answer
        assert not relay.conversations["alpha"].queue
        await until_phase(relay, "alpha", "finished")
        report = await relay.get_updates("alpha", 0)
        assert "Final replies, in order" in report
        assert "Done: meanwhile (new)" in report and "Background done: Background job 1" \
            in report
        ended = [e for e in taken() if e["type"] == "run_ended"]
        assert len(ended) == 1 and ended[0]["phase"] == "finished" and ended[0]["open"]
        assert alive(turn)
    run(scenario())


def test_keep_awake_and_grants_count_work_but_not_an_open_idle_run(config):
    async def scenario():
        relay, _ = await granted(config, "alpha")
        seen = []
        update = relay.keep_awake.update

        def recording(*, running, granted, now):
            seen.append(running)
            return update(running=running, granted=granted, now=now)
        relay.keep_awake.update = recording
        grant = next(iter(relay.access.grants.values()))

        await relay.send_message("alpha", "background 2", False, 8)
        assert relay.conversations["alpha"].turn.phase == "running"
        used = grant.last_used = grant.last_used - 100
        await relay.sweep()
        assert seen[-1] is True and grant.last_used > used     # a background task is use

        await until_phase(relay, "alpha", "finished")
        assert relay.conversations["alpha"].turn.open
        used = grant.last_used = grant.last_used - 100
        await relay.sweep()
        assert seen[-1] is False and grant.last_used == used   # open but idle is not
        assert relay.state()["projects"][0]["busy"] is None
        assert relay.state()["projects"][0]["open"] == "host"
    run(scenario())


def test_an_open_run_ends_after_the_idle_timeout(config):
    async def scenario():
        relay, _ = await granted(config, "alpha")
        taken = events_of(relay)
        relay.conversation_idle = 1
        await relay.send_message("alpha", "one", False, 8)
        turn = relay.conversations["alpha"].turn
        await relay.sweep()
        assert turn.open                        # not idle for long enough yet
        await asyncio.sleep(1.2)
        await relay.sweep()
        assert turn.closed and "idle" in turn.close_reason
        await until_ended(relay, "alpha")
        assert not alive(turn) and turn.phase == "finished"
        closed = [e for e in taken() if e["type"] == "process_ended"]
        assert closed and "idle" in closed[0]["reason"]
        assert "was closed (idle" in (config.projects_dir / "alpha/.relay/log.md").read_text()
        # The conversation goes on in a new run.
        answer = await relay.send_message("alpha", "two", False, 8)
        assert "Done: two (resumed)" in answer
        assert relay.conversations["alpha"].turn.run_id != turn.run_id
    run(scenario())


def test_a_background_task_keeps_the_run_open_past_the_idle_timeout(config):
    async def scenario():
        relay, _ = await granted(config, "alpha")
        relay.conversation_idle = 0
        await relay.send_message("alpha", "background 3", False, 8)
        turn = relay.conversations["alpha"].turn
        await asyncio.sleep(1)
        await relay.sweep()
        assert not turn.closed and turn.phase == "running"
        await until_phase(relay, "alpha", "finished")
        await relay.sweep()
        assert turn.closed
        await until_ended(relay, "alpha")
        assert "Background done" in await relay.get_updates("alpha", 0)
    run(scenario())


def test_revoking_closes_an_open_run_and_lets_a_working_one_finish(config):
    async def scenario():
        relay, control = await granted(config, "alpha")
        await relay.send_message("alpha", "one", False, 8)
        turn = relay.conversations["alpha"].turn
        grant = next(iter(relay.access.grants))
        await control.revoke(grant)
        assert turn.closed and turn.close_reason == "approval ended"
        await until_ended(relay, "alpha")

        await control.grant("alpha", full_access_ok=True)
        await relay.send_message("alpha", "background 2", False, 0)
        await until_phase(relay, "alpha", "running")
        working = relay.conversations["alpha"].turn
        await control.revoke(next(iter(relay.access.grants)))
        assert not working.closed and alive(working)        # finishes its work
        await until_phase(relay, "alpha", "finished")
        await relay.sweep()
        assert working.closed
        await until_ended(relay, "alpha")
        assert "Background done" in await relay.get_updates("alpha", 0)
    run(scenario())


def test_revoke_and_stop_ends_an_open_run_too(config):
    async def scenario():
        relay, control = await granted(config, "alpha")
        await relay.send_message("alpha", "one", False, 8)
        turn = relay.conversations["alpha"].turn
        await control.revoke(next(iter(relay.access.grants)), stop=True)
        assert turn.closed
        await until_ended(relay, "alpha")
    run(scenario())


def test_stop_closes_an_open_run(config):
    async def scenario():
        relay, _ = await granted(config, "alpha")
        await relay.send_message("alpha", "one", False, 8)
        turn = relay.conversations["alpha"].turn
        answer = await relay.stop("alpha")
        assert "has been closed" in answer and turn.closed
        await until_ended(relay, "alpha")
        assert "Claude Code is not working" in await relay.stop("alpha")
        assert "Done: two (resumed)" in await relay.send_message("alpha", "two", False, 8)
    run(scenario())


def test_stop_project_from_the_laptop_closes_an_open_run(config):
    async def scenario():
        relay, control = await granted(config, "alpha")
        await relay.send_message("alpha", "one", False, 8)
        turn = relay.conversations["alpha"].turn
        await relay.stop_project("alpha", False)
        assert turn.closed
        await until_ended(relay, "alpha")
    run(scenario())


def test_releasing_closes_the_open_run(config):
    async def scenario():
        relay, _ = await granted(config, "alpha")
        await relay.send_message("alpha", "one", False, 8)
        turn = relay.conversations["alpha"].turn
        assert "Released" in await relay.release_access("alpha", False)
        assert turn.closed
        await until_ended(relay, "alpha")
    run(scenario())


def test_a_new_conversation_closes_the_open_run(config):
    async def scenario():
        relay, _ = await granted(config, "alpha")
        await relay.send_message("alpha", "one", False, 8)
        old = relay.conversations["alpha"].turn
        assert "Done: two (new)" in await relay.send_message("alpha", "two", True, 8)
        assert old.closed and old.close_reason == "a new conversation"
        assert relay.conversations["alpha"].turn.run_id != old.run_id
        for _ in range(100):
            if not alive(old):
                break
            await asyncio.sleep(0.1)
        assert not alive(old)
    run(scenario())


def test_a_switch_to_the_host_closes_the_open_run_in_the_container(config):
    async def scenario():
        relay, control = await granted(config, "web")
        await relay.send_message("web", "in the container", False, 8)
        container = relay.conversations["web"].turn
        assert container.open
        # The other side is idle (open, but done), so the grant switches at once.
        await control.grant("web", kind=HOST, full_access_ok=True)
        assert container.closed and container.close_reason == "switched to run on the host"
        await until_ended(relay, "web")
        assert "Done: on the host (new)" in await relay.send_message(
            "web", "on the host", False, 8, run_on=HOST)
        assert relay.conversations["web/host"].turn.open
        # And back: a message for the container closes the host's open run.
        await control.grant("web", full_access_ok=True)
        host = relay.conversations["web/host"].turn
        assert host.closed
    run(scenario())


def test_a_restarted_relay_reattaches_to_the_open_run(config):
    async def scenario():
        relay, _ = await granted(config, "alpha")
        await relay.send_message("alpha", "one", False, 8)
        turn = relay.conversations["alpha"].turn

        restarted = Relay(config)
        restarted.set_control(True, "")
        again = restarted.conversations["alpha"].turn
        assert again.run_id == turn.run_id and again.phase == "finished"
        assert "already reported" in await restarted.get_updates("alpha", 0)
        assert again.open and alive(again)
        answer = await restarted.send_message("alpha", "two", False, 8)
        assert "Done: two (new)" in answer and "Done: one" not in answer
        assert restarted.conversations["alpha"].turn.run_id == turn.run_id

        # The idle timeout still applies after a restart.
        third = Relay(config)
        third.set_control(True, "")
        third.conversation_idle = 0
        await third.sweep()
        assert third.conversations["alpha"].turn.closed
        await until_ended(third, "alpha")
    run(scenario())


def test_a_restart_while_a_background_task_runs(config):
    async def scenario():
        relay, _ = await granted(config, "alpha")
        await relay.send_message("alpha", "background 3", False, 8)
        assert relay.conversations["alpha"].turn.phase == "running"
        for task in list(relay.followers.values()):
            task.cancel()                   # the server goes down

        restarted = Relay(config)
        restarted.set_control(True, "")
        report = await restarted.get_updates("alpha", 0)
        assert "still working" in report and "Background job 1" in report
        answer = await restarted.send_message("alpha", "meanwhile", False, 0)
        assert "Passed the message directly" in answer
        await until_phase(restarted, "alpha", "finished")
        report = await restarted.get_updates("alpha", 0)
        assert "Background done" in report and "Done: meanwhile" in report
        # The log has each entry once.
        text = (config.projects_dir / "alpha/.relay/log.md").read_text()
        assert text.count("Background done: Background job 1") == 1
    run(scenario())


OLD_RUNNER = """
d=$0
echo $$ > "$d/pid"
"$@" < "$d/in" > "$d/out.jsonl" 2> "$d/err.log" &
c=$!
exec 3> "$d/in"
cat "$d/prompt" >&3
exec 9> "$d/lock"
while kill -0 "$c" 2>/dev/null; do
    sleep 1
    flock 9 2>/dev/null
    r=$(grep -c '"type": *"result"' "$d/out.jsonl" 2>/dev/null)
    if [ "${r:-0}" -gt "$(cat "$d/mark")" ]; then
        : > "$d/closed"
        exec 3>&-
        break
    fi
    flock -u 9 2>/dev/null
done
flock -u 9 2>/dev/null
wait "$c"
echo $? > "$d/exit"
"""


def test_runs_of_the_relay_before_runs_stayed_open(config, monkeypatch):
    """A run the old relay started closes itself after its result; its
    state.json has none of the new fields."""
    async def scenario():
        relay, _ = await granted(config, "alpha")
        runner = relay_module._RUNNER
        monkeypatch.setattr(relay_module, "_RUNNER", OLD_RUNNER)
        launch = relay_module.Relay._launch

        def old_launch(self, target, turn, session_id, resume):
            targets.run_in(self.config, target,
                           f'mkdir -p {relay_module._run_dir(turn.run_id)} && '
                           f'echo 0 > {relay_module._run_dir(turn.run_id)}/mark')
            launch(self, target, turn, session_id, resume)
        monkeypatch.setattr(relay_module.Relay, "_launch", old_launch)
        await relay.send_message("alpha", "sleep 2", False, 0)
        await until_phase(relay, "alpha", "running")
        for task in list(relay.followers.values()) + list(relay.background):
            task.cancel()
        state = json.loads((config.state_dir / "state.json").read_text())
        state["alpha"]["turn"] = {k: v for k, v in state["alpha"]["turn"].items()
                                  if k in ("id", "message", "started", "phase", "error",
                                           "delivered", "resumed", "born", "passed_in",
                                           "earlier_reply", "pid", "logged")}
        (config.state_dir / "state.json").write_text(json.dumps(state))
        monkeypatch.setattr(relay_module.Relay, "_launch", launch)
        monkeypatch.setattr(relay_module, "_RUNNER", runner)

        restarted = Relay(config)
        restarted.set_control(True, "")
        await until_ended(restarted, "alpha")       # its runner closed it after the result
        assert "Done: sleep 2 (new)" in await restarted.get_updates("alpha", 0)
        assert "Done: next (resumed)" in await restarted.send_message("alpha", "next",
                                                                      False, 8)
    run(scenario())
