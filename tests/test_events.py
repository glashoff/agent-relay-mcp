import json

from agent_relay_mcp.events import TurnOutput, complete_lines, summarize_tool

SID = "6f1c1d3e-8a4b-4c55-9b7e-2a3f9d1e0c11"


def test_complete_lines_keeps_a_partial_line_for_later():
    lines, used = complete_lines(b'{"a":1}\n{"b":2}\n{"c":')
    assert lines == ['{"a":1}', '{"b":2}'] and used == 16
    assert complete_lines(b'{"c":') == ([], 0)


def test_turn_output_follows_the_top_level_conversation_only():
    out = TurnOutput()
    for event in [
        {"type": "system", "subtype": "init", "session_id": SID},
        {"type": "assistant", "parent_tool_use_id": None, "message": {"content": [
            {"type": "text", "text": "Checking."},
            {"type": "tool_use", "name": "Edit", "input": {"file_path": "/w/a.py"}}]}},
        {"type": "assistant", "parent_tool_use_id": "sub", "message": {"content": [
            {"type": "tool_use", "name": "Bash", "input": {"command": "rm -rf x"}}]}},
        {"type": "result", "subtype": "success", "is_error": False, "result": "All done",
         "session_id": SID, "duration_ms": 5},
    ]:
        out.feed(json.dumps(event))
    out.feed("not json")
    assert out.session_id == SID
    assert out.steps == ["Edit: /w/a.py"]
    assert out.last_text == "Checking."
    assert out.result == "All done" and not out.is_error
    assert out.results == ["All done"]


def test_turn_output_keeps_the_result_of_every_pass():
    out = TurnOutput()
    for text in ("First", "Second"):
        out.feed(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                             "result": text, "session_id": SID}))
    assert out.result == "Second" and out.results == ["First", "Second"]


def test_error_results_carry_their_reason():
    out = TurnOutput()
    out.feed(json.dumps({"type": "result", "subtype": "error_max_turns", "is_error": True}))
    assert out.is_error and out.result == "error_max_turns"


def test_bash_steps_prefer_the_description():
    assert summarize_tool("Bash", {"command": "ls -la", "description": "List files"}) \
        == "Bash: List files"
    assert summarize_tool("Bash", {"command": "ls   -la\n"}) == "Bash: ls -la"
    assert summarize_tool("TodoWrite", {}) == "TodoWrite"


def test_output_from_the_run_cannot_inject_a_session_id_or_crash_it():
    out = TurnOutput()
    out.feed(json.dumps({"type": "system", "subtype": "init",
                         "session_id": "--dangerously-skip-permissions"}))
    assert out.session_id == ""
    # Malformed events are skipped, not raised.
    out.feed(json.dumps({"type": "assistant", "parent_tool_use_id": None, "message": 5}))
    out.feed(json.dumps({"type": "assistant", "parent_tool_use_id": None,
                         "message": {"content": ["x", {"type": "text", "text": 7}]}}))
    out.feed(json.dumps({"type": "result", "subtype": "success", "result": ["a"],
                         "session_id": SID}))
    assert out.session_id == SID and out.result == "['a']"


def test_kept_steps_and_results_are_capped():
    out = TurnOutput()
    for i in range(500):
        out.feed(json.dumps({"type": "assistant", "parent_tool_use_id": None, "message": {
            "content": [{"type": "tool_use", "name": "Bash", "input": {"command": str(i)}}]}}))
        out.feed(json.dumps({"type": "result", "subtype": "success", "result": str(i)}))
    assert len(out.steps) == 200 and out.steps[-1] == "Bash: 499"
    assert len(out.results) == 20


def feed_all(out, events, start=0):
    """Feeds events as lines of out.jsonl from byte offset start; the offset after."""
    at = start
    for event in events:
        line = json.dumps(event)
        out.feed(line, at)
        at += len(line) + 1
    return at


def system(subtype, **fields):
    return {"type": "system", "subtype": subtype, "session_id": SID, **fields}


def result(text):
    return {"type": "result", "subtype": "success", "is_error": False, "result": text,
            "session_id": SID}


def say(text):
    return {"type": "assistant", "parent_tool_use_id": None,
            "message": {"content": [{"type": "text", "text": text}]}}


# As Claude Code 2.1 writes them (shapes taken from real runs).
BG_STARTED = [
    {"type": "assistant", "parent_tool_use_id": None, "message": {"content": [
        {"type": "tool_use", "id": "toolu_1", "name": "Bash",
         "input": {"command": "sleep 20; echo bgdone", "run_in_background": True}}]}},
    system("background_tasks_changed", tasks=[
        {"task_id": "bontik2o2", "task_type": "local_bash",
         "description": "sleep 20; echo bgdone"}]),
    system("task_started", task_id="bontik2o2", tool_use_id="toolu_1",
           description="sleep 20; echo bgdone", is_backgrounded=True, task_type="local_bash"),
    {"type": "user", "parent_tool_use_id": None, "message": {"content": [
        {"type": "tool_result", "tool_use_id": "toolu_1", "content": "Command running"}]}},
    say("started"),
    result("started"),
]
BG_ENDED = [
    system("background_tasks_changed", tasks=[]),
    system("task_updated", task_id="bontik2o2", patch={"status": "completed"}),
    system("task_notification", task_id="bontik2o2", tool_use_id="toolu_1",
           status="completed", summary='Background command "sleep 20" completed'),
]
FOLLOW_UP = [system("init"), say("The background task completed."),
             result("The background task completed.")]


def test_a_request_is_done_only_once_its_background_tasks_have_ended():
    out = TurnOutput()
    at = feed_all(out, [system("init")] + BG_STARTED)
    assert out.tasks == {"bontik2o2": "sleep 20; echo bgdone"}
    assert out.result == "started" and not out.done(0)
    at = feed_all(out, BG_ENDED, at)
    assert not out.tasks
    assert not out.done(0)          # Claude Code takes the notification up next
    assert ("step", "Background task completed: sleep 20; echo bgdone") in out.items
    feed_all(out, FOLLOW_UP, at)
    assert out.done(0) and out.results == ["started", "The background task completed."]


def test_a_request_is_done_only_with_a_result_after_the_last_message():
    out = TurnOutput()
    at = feed_all(out, [system("init"), say("ok"), result("first")])
    assert out.done(0) and not out.done(at)     # a message was passed in at "at"
    feed_all(out, [system("init"), say("more"), result("second")], at)
    assert out.done(at)


def test_tasks_of_subagents_and_foreground_tasks_do_not_count():
    out = TurnOutput()
    feed_all(out, [system("init"),
                   system("task_started", task_id="b1", owned_by_subagent=True,
                          is_backgrounded=True, task_type="local_bash", description="x"),
                   system("task_started", task_id="b2", is_backgrounded=False,
                          task_type="local_bash", description="y"),
                   system("task_notification", task_id="b1", status="completed"),
                   result("done")])
    assert not out.tasks and out.done(0)


def test_without_the_task_list_task_events_keep_track():
    out = TurnOutput()
    at = feed_all(out, [system("init"),
                        system("task_started", task_id="a1", is_backgrounded=True,
                               task_type="local_agent", description="Build it"),
                        result("started")])
    assert out.tasks == {"a1": "Build it"} and not out.done(0)
    at = feed_all(out, [system("task_updated", task_id="a1",
                               patch={"status": "killed"})], at)
    assert not out.tasks
    feed_all(out, [system("task_notification", task_id="a1", status="stopped")], at)
    assert not out.done(0)          # a pass for it may still come


def test_a_notification_nobody_takes_up_ends_the_wait():
    from agent_relay_mcp import events
    out = TurnOutput()
    at = feed_all(out, [system("init")] + BG_STARTED)
    feed_all(out, BG_ENDED, at)
    assert not out.done(0, now=out.noticed_at + 1)
    assert out.done(0, now=out.noticed_at + events.NOTICE_GRACE_SECONDS)
