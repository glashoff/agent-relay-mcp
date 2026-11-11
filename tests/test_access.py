"""Grants, requests and switches, end to end with tests/fake_claude.py.

"alpha" has no dev container (a host project). "web" has one; the tests make
its dev container run on the host too (stubbed), so both of its kinds can be
told apart by their conversations, not by where they really run.
"""

import asyncio
import inspect
import json
import sys
from pathlib import Path

import pytest

from agent_relay_mcp import relay as relay_module
from agent_relay_mcp import targets
from agent_relay_mcp.access import ALL, HOST, AccessError
from agent_relay_mcp.config import Config
from agent_relay_mcp.control import Control, ControlError
from agent_relay_mcp.relay import Relay

FAKE = Path(__file__).with_name("fake_claude.py")


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(relay_module, "QUEUE_POLL_SECONDS", 0.2)
    monkeypatch.setattr(relay_module, "SWEEP_SECONDS", 3600)    # sweeps by hand
    projects = tmp_path / "projects"
    (projects / "alpha").mkdir(parents=True)
    (projects / "web/.devcontainer").mkdir(parents=True)
    (projects / "web/.devcontainer/devcontainer.json").write_text("{}")
    # The dev container of "web", stubbed: runs on the host as well.
    real_target_for = targets.target_for
    monkeypatch.setattr(targets, "REAL_TARGET_FOR", real_target_for, raising=False)
    monkeypatch.setattr(targets, "running_container", lambda config, project: "stub")
    monkeypatch.setattr(targets, "target_for",
                        lambda config, project, *, start, on_host=False: real_target_for(
                            config, project, start=start, on_host=True))
    config = Config(projects_dir=projects, allow_host_projects=True,
                    claude_command=f"{sys.executable} {FAKE}",
                    state_dir=tmp_path / "state", max_wait_seconds=10)
    return config


def run(coro):
    return asyncio.run(coro)


class Clock:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now


def relay_at(config, clock):
    relay = Relay(config)
    relay.clock = clock
    return relay


async def until_done(relay, project, seconds=10):
    for _ in range(seconds * 10):
        report = await relay.get_updates(project, 0)
        if "still working" not in report and "being started" not in report \
                and "waiting for approval" not in report and "held until" not in report:
            return report
        await asyncio.sleep(0.1)
    raise AssertionError("never finished: " + report)


async def until_ended(relay, key, seconds=10):
    """Waits for the turn of conversation key to end; get_updates then."""
    for _ in range(seconds * 10):
        report = await relay.get_updates(key.split("/")[0], 0)
        if relay.conversations[key].turn.phase in ("finished", "failed"):
            return report
        await asyncio.sleep(0.1)
    raise AssertionError(f"{key} never ended")


async def until_running(relay, key, seconds=10):
    for _ in range(seconds * 20):
        conversation = relay.conversations.get(key)
        if conversation and conversation.turn and conversation.turn.phase == "running":
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"{key} never ran")


def only_request(relay):
    (request,) = relay.access.requests.values()
    return request


# --- locked by default ------------------------------------------------------------

def test_a_locked_project_makes_a_request_and_runs_nothing(setup):
    async def scenario():
        relay = Relay(setup)
        answer = await relay.send_message("web", "hello", False, 0)
        request = only_request(relay)
        assert "locked" in answer and f"number {request.number}" in answer
        assert "full access" not in answer
        assert not relay.conversations          # nothing started
        assert "waiting for approval" in await relay.get_updates("web", 0)
        listing = await relay.list_projects()
        assert f"waiting for approval (number {request.number})" in listing
        # A second message joins the same request.
        await relay.send_message("web", "and more", False, 0)
        assert only_request(relay).messages == ["hello", "and more"]
    run(scenario())


def test_a_host_project_request_warns(setup):
    answer = run(Relay(setup).send_message("alpha", "hi", False, 0))
    assert "full access to the computer" in answer


def test_approving_runs_the_waiting_messages(setup):
    async def scenario():
        relay = Relay(setup)
        await relay.send_message("web", "one", False, 0)
        await relay.send_message("web", "two", False, 0)
        result = await Control(relay).approve(only_request(relay).id, minutes=30)
        assert result["grant"]["target"] == "web"
        report = await until_done(relay, "web")
        assert "Done: one\n\ntwo (new)" in report
        # Approved now: the next message runs directly.
        assert "Passed the message" in await relay.send_message("web", "three", False, 0)
    run(scenario())


def test_denying_runs_nothing(setup):
    async def scenario():
        relay = Relay(setup)
        await relay.send_message("web", "hello", False, 0)
        number = only_request(relay).number
        await Control(relay).deny(only_request(relay).id)
        report = await relay.get_updates("web", 0)
        assert f"(number {number}) was denied" in report
        assert not relay.conversations
    run(scenario())


def test_an_unanswered_request_expires(setup):
    clock = Clock()

    async def scenario():
        relay = relay_at(setup, clock)
        await relay.send_message("web", "hello", False, 0)
        clock.now += 31 * 60
        await relay.sweep()
        assert not relay.access.requests
        assert "expired without an answer" in await relay.get_updates("web", 0)
    run(scenario())


def test_too_many_requests_are_refused(setup):
    async def scenario():
        relay = Relay(setup)
        relay.access.limits.max_open_requests = 1
        await relay.send_message("web", "hello", False, 0)
        with pytest.raises(targets.RelayError, match="Too many requests"):
            await relay.send_message("alpha", "hello", False, 0)
    run(scenario())


# --- grants -------------------------------------------------------------------------

def test_grants_are_cut_to_their_upper_limits(setup):
    async def scenario():
        relay = Relay(setup)
        control = Control(relay)
        hour = 3600
        g = (await control.grant("web", minutes=24 * 60))["grant"]
        assert round(g["expires"] - g["idle_ends"] + 30 * 60) == 12 * hour
        assert g["cut_to_limit"]
        assert not (await control.grant("web", minutes=60))["grant"]["cut_to_limit"]
        g = (await control.grant(ALL, minutes=24 * 60))["grant"]
        assert round(g["expires"] - g["idle_ends"] + 30 * 60) == 4 * hour
        g = (await control.grant("alpha", minutes=24 * 60, full_access_ok=True))["grant"]
        assert round(g["expires"] - g["idle_ends"] + 30 * 60) == 2 * hour
        assert g["full_access"] and not g["end_on_lock"]
    run(scenario())


def test_full_access_needs_the_confirmation(setup):
    async def scenario():
        control = Control(Relay(setup))
        for call in (control.grant("alpha"),
                     control.grant("web", kind=HOST),
                     control.grant(ALL, include_host_projects=True)):
            with pytest.raises(ControlError, match="Needs confirmation"):
                await call
        with pytest.raises(ControlError, match="only run on the host"):
            await control.grant("alpha", kind="container", full_access_ok=True)
        with pytest.raises(ControlError, match="all projects"):
            await control.grant(ALL, kind=HOST, full_access_ok=True)
    run(scenario())


def test_full_access_can_be_turned_off(setup):
    setup.allow_host_projects = False
    with pytest.raises(ControlError, match="turned off"):
        run(Control(Relay(setup)).grant("web", kind=HOST, full_access_ok=True))


def test_all_projects_leaves_host_projects_out(setup):
    async def scenario():
        relay = Relay(setup)
        await Control(relay).grant(ALL)
        assert "locked" in await relay.send_message("alpha", "hi", False, 0)
        assert "working on it" in await relay.send_message("web", "hi", False, 0)
    run(scenario())


def test_grants_survive_a_restart_and_a_broken_file_locks_everything(setup):
    async def scenario():
        await Control(Relay(setup)).grant("web")
        assert "working on it" in await Relay(setup).send_message("web", "hi", False, 0)
        (setup.state_dir / "access.json").write_text("{not json")
        relay = Relay(setup)
        assert "locked" in await relay.send_message("web", "hi", False, 0)
        assert (setup.state_dir / "access.broken").exists()
    run(scenario())


def test_the_state_file_is_private(setup):
    run(Control(Relay(setup)).grant("web"))
    assert (setup.state_dir / "access.json").stat().st_mode & 0o777 == 0o600


def test_a_grant_ends_after_idle_time_but_not_while_a_request_runs(setup):
    clock = Clock()

    async def scenario():
        relay = relay_at(setup, clock)
        await Control(relay).grant("web", minutes=600)
        await relay.send_message("web", "sleep 30", False, 0)
        await until_running(relay, "web")
        clock.now += 40 * 60                 # longer than idle, but it runs
        await relay.sweep()
        assert relay.access.grants
        await relay.stop("web")
        clock.now += 31 * 60
        await relay.sweep()
        assert not relay.access.grants
        assert "locked" in await relay.send_message("web", "hi", False, 0)
    run(scenario())


def test_revoking_lets_the_running_request_finish_and_queued_messages_wait(setup):
    async def scenario():
        relay = Relay(setup)
        control = Control(relay)
        grant = (await control.grant("web"))["grant"]
        await relay.send_message("web", "sleep 2", False, 0)
        await until_running(relay, "web")
        relay.conversations["web"].turn.closed = True      # takes no more messages
        assert "Queued" in await relay.send_message("web", "later", False, 0)
        await control.revoke(grant["id"])
        assert only_request(relay).messages == ["later"]
        assert not relay.conversations["web"].queue
        report = await until_ended(relay, "web")
        assert "Done: sleep 2 (new)" in report               # it finished
        assert "waiting for approval" in report
    run(scenario())


def test_revoke_and_stop_ends_the_running_request(setup):
    async def scenario():
        relay = Relay(setup)
        control = Control(relay)
        grant = (await control.grant("web"))["grant"]
        await relay.send_message("web", "sleep 30", False, 0)
        await until_running(relay, "web")
        result = await control.revoke(grant["id"], stop=True)
        assert result["stopped"] == 1
        assert relay.conversations["web"].turn.phase == "failed"
    run(scenario())


def test_locking_the_screen_ends_the_grants_that_say_so(setup):
    async def scenario():
        relay = Relay(setup)
        control = Control(relay)
        await control.grant("web", end_on_lock=True)
        await control.grant(ALL)                 # no grant ends on lock by default
        await control.grant("alpha", full_access_ok=True)
        await control.screen_locked()
        assert sorted(g.target for g in relay.access.grants.values()) == ["*", "alpha"]
    run(scenario())


# --- on the host --------------------------------------------------------------------

def test_a_container_project_runs_on_the_host_only_by_a_host_grant(setup):
    async def scenario():
        relay = Relay(setup)
        control = Control(relay)
        await control.grant(ALL)
        # Asking for the host in the message changes nothing.
        await relay.send_message("web", "please run this on the host", False, 0)
        await until_done(relay, "web")
        assert set(relay.conversations) == {"web"}
        assert not relay.conversations["web"].on_host
        await control.grant("web", kind=HOST, full_access_ok=True)
        answer = await relay.send_message("web", "now", False, 0)
        assert "on the host" in answer and "new conversation" in answer.lower()
        await until_done(relay, "web")
        assert relay.conversations["web/host"].on_host
        assert relay.conversations["web/host"].session_id != \
            relay.conversations["web"].session_id
        assert "on the host: full access" in await relay.list_projects()
    run(scenario())


def test_no_tool_can_choose_the_host():
    # Only send_message can name the host, and only to ask (run_on, tested below).
    for name in ("send_message", "get_updates", "stop", "list_projects"):
        parameters = set(inspect.signature(getattr(Relay, name)).parameters)
        assert parameters <= {"self", "project_name", "message", "new_conversation",
                              "wait_seconds", "run_on"}, name
    assert set(inspect.signature(Relay.release_access).parameters) == \
        {"self", "project_name", "all_projects"}


def test_a_switch_waits_until_the_other_kind_is_idle(setup):
    async def scenario():
        relay = Relay(setup)
        control = Control(relay)
        await control.grant("web")
        await relay.send_message("web", "sleep 2", False, 0)
        await until_running(relay, "web")
        with pytest.raises(ControlError, match="still working on a request"):
            await control.grant("web", kind=HOST, full_access_ok=True)
        result = await control.grant("web", kind=HOST, full_access_ok=True, when_idle=True)
        assert result["switch"]["kind"] == HOST
        answer = await relay.send_message("web", "afterwards", False, 0)
        assert "switching to run on the host" in answer
        assert "switching to run on the host once idle" in await relay.list_projects()
        # The container finishes; the switch happens; the held message runs there.
        for _ in range(100):
            await relay.sweep()
            if not relay.access.switches:
                break
            await asyncio.sleep(0.1)
        assert relay.access.covering("web", "container", relay.clock()).kind == HOST
        await until_running(relay, "web/host")
        assert relay.conversations["web/host"].turn.message == "afterwards"
    run(scenario())


def test_cancelling_a_switch_turns_held_messages_into_a_request(setup):
    async def scenario():
        relay = Relay(setup)
        control = Control(relay)
        await control.grant("web")
        await relay.send_message("web", "sleep 30", False, 0)
        await until_running(relay, "web")
        await control.grant("web", kind=HOST, full_access_ok=True, when_idle=True)
        await relay.send_message("web", "held", False, 0)
        await control.cancel_switch("web")
        assert only_request(relay).messages == ["held"]
        await relay.stop("web")
    run(scenario())


def test_approving_on_the_host_while_the_container_works_waits(setup):
    async def scenario():
        relay = Relay(setup)
        control = Control(relay)
        grant = (await control.grant("web"))["grant"]
        await relay.send_message("web", "sleep 30", False, 0)
        await until_running(relay, "web")
        await control.revoke(grant["id"])                 # the run goes on
        await relay.send_message("web", "next", False, 0)
        request = only_request(relay)
        with pytest.raises(ControlError, match="still working"):
            await control.approve(request.id, kind=HOST, full_access_ok=True)
        assert relay.access.requests                       # still open
        result = await control.approve(request.id, kind=HOST, full_access_ok=True,
                                       when_idle=True)
        assert result["switch"]["kind"] == HOST and not relay.access.requests
        assert relay.access.switches["web"].held == ["next"]
        await relay.stop("web")
    run(scenario())


# --- released by the chat -----------------------------------------------------------

def test_the_chat_can_release_a_project_when_nothing_runs(setup):
    async def scenario():
        relay = Relay(setup)
        events = relay.subscribe()
        await Control(relay).grant("web")
        await relay.send_message("web", "quick", False, 8)
        answer = await relay.release_access("web", False)
        assert "locked again" in answer and not relay.access.grants
        assert "locked" in await relay.send_message("web", "more", False, 0)
        kinds = []
        while not events.empty():
            kinds.append(events.get_nowait())
        assert any(e["type"] == "grant_ended" and e["reason"] == "released by the chat"
                   for e in kinds)
        assert "already locked" in await relay.release_access("web", False)
    run(scenario())


def test_releasing_is_refused_while_claude_code_works_or_messages_wait(setup):
    async def scenario():
        relay = Relay(setup)
        await Control(relay).grant("web")
        await relay.send_message("web", "sleep 30", False, 0)
        await until_running(relay, "web")
        with pytest.raises(targets.RelayError, match="still working in web"):
            await relay.release_access("web", False)
        with pytest.raises(targets.RelayError, match="still working in web"):
            await relay.release_access("", True)
        assert relay.access.grants
        await relay.stop("web")
        relay.conversations["web"].queue.append("waiting")      # a message waits
        with pytest.raises(targets.RelayError, match="messages waiting"):
            await relay.release_access("web", False)
        relay.conversations["web"].queue.clear()
        assert "locked again" in await relay.release_access("web", False)
    run(scenario())


def test_releasing_all_and_the_grant_for_all_projects(setup):
    async def scenario():
        relay = Relay(setup)
        control = Control(relay)
        await control.grant(ALL)
        await control.grant("alpha", full_access_ok=True)
        answer = await relay.release_access("web", False)
        assert "only through the approval for all projects" in answer
        assert len(relay.access.grants) == 2
        assert "Every project is locked again" in await relay.release_access("", True)
        assert not relay.access.grants
        assert "already locked" in await relay.release_access("", True)
    run(scenario())


def test_releasing_never_grants(setup):
    async def scenario():
        relay = Relay(setup)
        await relay.release_access("web", False)
        await relay.release_access("", True)
        assert not relay.access.grants
        assert "locked" in await relay.send_message("web", "hi", False, 0)
    run(scenario())


def test_stop_and_lock_from_the_laptop(setup):
    async def scenario():
        relay = Relay(setup)
        control = Control(relay)
        await control.grant(ALL)
        await relay.send_message("web", "sleep 30", False, 0)
        await until_running(relay, "web")
        result = await control.stop_project("web", lock=True)
        assert result == {"stopped": 1, "revoked": ["*"]}
        assert relay.conversations["web"].turn.phase == "failed"
        assert "locked" in await relay.send_message("web", "hi", False, 0)
    run(scenario())


def test_keep_awake_settings_through_the_control_layer(setup):
    control = Control(Relay(setup))
    state = control.keep_awake("granted", True)
    assert state["mode"] == "granted" and state["lid"]
    assert control.state()["keep_awake"]["mode"] == "granted"
    with pytest.raises(ControlError, match="Unknown mode"):
        control.keep_awake("always", False)


# --- the control layer --------------------------------------------------------------

def test_state_and_events(setup):
    async def scenario():
        relay = Relay(setup)
        control = Control(relay)
        events = control.events()
        waiting = asyncio.ensure_future(events.__anext__())
        await asyncio.sleep(0)
        await relay.send_message("alpha", "hi", False, 0)
        event = await asyncio.wait_for(waiting, 2)
        assert event["type"] == "request_added" and event["full_access"]
        state = control.state()
        assert {p["name"]: p["dev_container"] for p in state["projects"]} == \
            {"alpha": False, "web": True}
        (request,) = state["requests"]
        assert request["project"] == "alpha" and request["messages"] == ["hi"]
        assert "full access" in state["warnings"]["host_project"]
        json.dumps(state)
        await events.aclose()
    run(scenario())


def test_unknown_things_are_reported(setup):
    async def scenario():
        control = Control(Relay(setup))
        with pytest.raises(ControlError, match="no open request"):
            await control.approve("nope")
        with pytest.raises(ControlError, match="no grant"):
            await control.revoke("nope")
        with pytest.raises(ControlError, match="Unknown kind"):
            await control.grant("web", kind="cloud")
        with pytest.raises(ControlError, match="at least one minute"):
            await control.grant("web", minutes=0)
    run(scenario())


def test_access_error_is_not_a_relay_error():
    # Control errors never reach the MCP tools as something they could act on.
    assert not issubclass(AccessError, targets.RelayError)


# --- from the security review -------------------------------------------------------

def test_a_container_grant_never_runs_on_the_host_when_the_dev_container_is_gone(setup):
    async def scenario():
        relay = Relay(setup)
        await Control(relay).grant("web")              # for the dev container
        # Claude Code in the container deletes the project's devcontainer.json.
        (setup.projects_dir / "web/.devcontainer/devcontainer.json").unlink()
        answer = await relay.send_message("web", "now on the host?", False, 0)
        assert "locked" in answer and "full access" in answer
        assert "web" not in relay.conversations
    run(scenario())


def test_the_place_follows_the_grant_not_the_stored_conversation(setup):
    async def scenario():
        relay = Relay(setup)
        control = Control(relay)
        await control.grant("alpha", full_access_ok=True)
        await relay.send_message("alpha", "quick", False, 8)
        assert relay.conversations["alpha"].on_host
        old_session = relay.conversations["alpha"].session_id
        # alpha gets a dev container; the old grant does not cover it any more.
        (setup.projects_dir / "alpha/.devcontainer").mkdir()
        (setup.projects_dir / "alpha/.devcontainer/devcontainer.json").write_text("{}")
        assert "locked" in await relay.send_message("alpha", "again", False, 0)
        await control.deny(next(iter(relay.access.requests)))
        await control.grant("alpha")                     # now for its dev container
        await relay.send_message("alpha", "in the container", False, 8)
        conversation = relay.conversations["alpha"]
        assert not conversation.on_host and conversation.session_id != old_session
    run(scenario())


def test_without_a_dev_container_nothing_falls_back_to_the_host(setup):
    project = targets.Project("alpha", setup.projects_dir / "alpha")
    with pytest.raises(targets.RelayError, match="no dev container"):
        targets.REAL_TARGET_FOR(setup, project, start=False, on_host=False)


def test_a_folder_named_star_is_not_a_project(setup):
    (setup.projects_dir / "*").mkdir()
    names = [p.name for p in targets.list_projects(setup)]
    assert "*" not in names
    with pytest.raises(targets.RelayError, match="Invalid project name"):
        targets.find_project(setup, "*")


def test_requests_that_expired_or_changed_cannot_be_approved(setup):
    clock = Clock()

    async def scenario():
        relay = relay_at(setup, clock)
        control = Control(relay)
        await relay.send_message("web", "one", False, 0)
        request = only_request(relay)
        await relay.send_message("web", "two", False, 0)       # added after it was shown
        with pytest.raises(ControlError, match="has changed"):
            await control.approve(request.id, messages=1)
        assert relay.access.requests
        clock.now += 31 * 60                                   # before any sweep
        with pytest.raises(ControlError, match="no open request"):
            await control.approve(request.id, messages=2)
        assert not relay.access.grants
    run(scenario())


def test_a_refused_switch_keeps_the_request(setup):
    async def scenario():
        relay = Relay(setup)
        control = Control(relay)
        grant = (await control.grant("web"))["grant"]
        await relay.send_message("web", "sleep 30", False, 0)
        await until_running(relay, "web")
        await control.revoke(grant["id"])
        await relay.send_message("web", "next", False, 0)
        request = only_request(relay)
        with pytest.raises(ControlError, match="still working"):
            await control.approve(request.id, kind=HOST, full_access_ok=True, messages=1)
        assert only_request(relay).id == request.id
        await relay.stop("web")
    run(scenario())


def test_requests_take_a_limited_number_of_messages(setup):
    async def scenario():
        relay = Relay(setup)
        for i in range(20):
            await relay.send_message("web", f"m{i}", False, 0)
        with pytest.raises(targets.RelayError, match="already wait"):
            await relay.send_message("web", "one more", False, 0)
    run(scenario())


def test_without_a_control_interface_no_grant_counts(setup):
    async def scenario():
        relay = Relay(setup)
        await Control(relay).grant("web")
        relay.access.suspended = "no control interface"
        assert "locked" in await relay.send_message("web", "hi", False, 0)
    run(scenario())


@pytest.mark.parametrize("content", ["[]", '{"grants": [{"id": "x"}]}',
                                      '{"grants": [{"id": "x", "target": "web", "kind": "container", '
                                      '"created": 0, "expires": null, "idle": 1800, "last_used": 0, '
                                      '"end_on_lock": false}]}'])
def test_a_malformed_access_file_locks_everything(setup, content):
    setup.state_dir.mkdir(parents=True, exist_ok=True)
    (setup.state_dir / "access.json").write_text(content)
    relay = Relay(setup)
    assert not relay.access.grants
    assert (setup.state_dir / "access.broken").exists()
    assert "locked" in run(relay.send_message("web", "hi", False, 0))


def test_state_files_are_private(setup):
    run(Relay(setup).send_message("web", "hi", False, 0))
    run(Control(Relay(setup)).grant("web"))
    run(Relay(setup).send_message("web", "quick", False, 8))
    for name in ("access.json", "state.json"):
        assert (setup.state_dir / name).stat().st_mode & 0o777 == 0o600, name


def test_old_state_without_places_keeps_host_projects_on_the_host(setup):
    setup.state_dir.mkdir(parents=True, exist_ok=True)
    (setup.state_dir / "state.json").write_text(json.dumps(
        {"alpha": {"session_id": "s", "turn": None, "queue": []},
         "web": {"session_id": "t", "turn": None, "queue": []}}))
    relay = Relay(setup)
    assert relay.conversations["alpha"].on_host
    assert not relay.conversations["web"].on_host


# --- asking to run on the host (send_message run_on="host") -------------------------

def test_asking_for_the_host_makes_a_host_request_and_runs_nothing(setup):
    async def scenario():
        relay = Relay(setup)
        answer = await relay.send_message("web", "do it on the host", False, 0, run_on=HOST)
        request = only_request(relay)
        assert request.kind == HOST and request.full_access
        assert "on the host" in answer and "nothing runs on the host" in answer
        assert not relay.conversations
        state = Control(relay).state()
        assert state["requests"][0]["kind"] == HOST
    run(scenario())


def test_a_container_grant_never_becomes_a_host_grant_by_asking(setup):
    async def scenario():
        relay = Relay(setup)
        await Control(relay).grant("web")
        answer = await relay.send_message("web", "on the host please", False, 0, run_on=HOST)
        assert "waits for approval" in answer
        (grant,) = relay.access.grants.values()
        assert grant.kind == "container"
        assert not relay.conversations          # not run in the container either
        # Plain messages still run in the dev container meanwhile.
        await relay.send_message("web", "quick", False, 8)
        assert set(relay.conversations) == {"web"}
    run(scenario())


def test_a_host_request_runs_on_the_host_only_with_the_confirmation(setup):
    async def scenario():
        relay = Relay(setup)
        control = Control(relay)
        await relay.send_message("web", "on the host", False, 0, run_on=HOST)
        request = only_request(relay)
        with pytest.raises(ControlError, match="Needs confirmation"):
            await control.approve(request.id, messages=1)
        assert relay.access.requests and not relay.access.grants
        result = await control.approve(request.id, full_access_ok=True, messages=1)
        assert result["grant"]["kind"] == HOST
        await until_done(relay, "web")
        assert relay.conversations["web/host"].on_host
        # Approved for the host: asking again simply runs there.
        assert "on the host" in await relay.send_message("web", "more", False, 0,
                                                         run_on=HOST)
    run(scenario())


def test_a_host_request_can_be_approved_for_the_dev_container_instead(setup):
    async def scenario():
        relay = Relay(setup)
        await relay.send_message("web", "on the host", False, 0, run_on=HOST)
        result = await Control(relay).approve(only_request(relay).id, kind="container",
                                              messages=1)
        assert result["grant"]["kind"] == "container"
        await until_done(relay, "web")
        assert set(relay.conversations) == {"web"}
    run(scenario())


def test_a_host_request_follows_the_switch_rules(setup):
    async def scenario():
        relay = Relay(setup)
        control = Control(relay)
        await control.grant("web")
        await relay.send_message("web", "sleep 30", False, 0)
        await until_running(relay, "web")
        await relay.send_message("web", "then on the host", False, 0, run_on=HOST)
        request = only_request(relay)
        with pytest.raises(ControlError, match="still working"):
            await control.approve(request.id, full_access_ok=True, messages=1)
        result = await control.approve(request.id, full_access_ok=True, messages=1,
                                       when_idle=True)
        assert result["switch"]["kind"] == HOST
        assert relay.access.switches["web"].held == ["then on the host"]
        # While the switch waits, asking for the host is held, not a new request.
        assert "switching" in await relay.send_message("web", "x", False, 0, run_on=HOST)
        await relay.stop("web")
    run(scenario())


def test_host_and_normal_requests_do_not_mix(setup):
    async def scenario():
        relay = Relay(setup)
        await relay.send_message("web", "normal", False, 0)
        with pytest.raises(targets.RelayError, match="has to be answered first"):
            await relay.send_message("web", "host", False, 0, run_on=HOST)
        await Control(relay).deny(only_request(relay).id)
        await relay.send_message("web", "host", False, 0, run_on=HOST)
        with pytest.raises(targets.RelayError, match="has to be answered first"):
            await relay.send_message("web", "normal", False, 0)
    run(scenario())


def test_run_on_is_checked(setup):
    async def scenario():
        relay = Relay(setup)
        with pytest.raises(targets.RelayError, match="run_on is"):
            await relay.send_message("web", "x", False, 0, run_on="cloud")
        with pytest.raises(targets.RelayError, match="no dev container"):
            await relay.send_message("alpha", "x", False, 0, run_on="container")
        # A host project already runs on the host: an ordinary request.
        await relay.send_message("alpha", "x", False, 0, run_on=HOST)
        assert only_request(relay).kind == ""
        setup.allow_host_projects = False
        with pytest.raises(targets.RelayError, match="turned off"):
            await Relay(setup).send_message("web", "x", False, 0, run_on=HOST)
    run(scenario())


def test_full_access_grants_keep_running_when_the_screen_locks_unless_ticked(setup):
    async def scenario():
        relay = Relay(setup)
        control = Control(relay)
        await control.grant("web", kind=HOST, full_access_ok=True, minutes=24 * 60)
        await control.grant("alpha", full_access_ok=True, end_on_lock=True)
        await control.screen_locked()
        assert [(g.target, g.kind) for g in relay.access.grants.values()] == [("web", HOST)]
        # The other limits stay: 2 h at most, 30 min idle.
        (grant,) = relay.access.grants.values()
        assert grant.expires - grant.created == 2 * 3600 and grant.idle == 30 * 60
    run(scenario())
