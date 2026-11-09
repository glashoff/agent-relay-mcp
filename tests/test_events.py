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
