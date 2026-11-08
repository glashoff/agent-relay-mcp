import json

from agent_relay_mcp.events import TurnOutput, complete_lines, summarize_tool


def test_complete_lines_keeps_a_partial_line_for_later():
    lines, used = complete_lines(b'{"a":1}\n{"b":2}\n{"c":')
    assert lines == ['{"a":1}', '{"b":2}'] and used == 16
    assert complete_lines(b'{"c":') == ([], 0)


def test_turn_output_follows_the_top_level_conversation_only():
    out = TurnOutput()
    for event in [
        {"type": "system", "subtype": "init", "session_id": "s1"},
        {"type": "assistant", "parent_tool_use_id": None, "message": {"content": [
            {"type": "text", "text": "Checking."},
            {"type": "tool_use", "name": "Edit", "input": {"file_path": "/w/a.py"}}]}},
        {"type": "assistant", "parent_tool_use_id": "sub", "message": {"content": [
            {"type": "tool_use", "name": "Bash", "input": {"command": "rm -rf x"}}]}},
        {"type": "result", "subtype": "success", "is_error": False, "result": "All done",
         "session_id": "s1", "duration_ms": 5},
    ]:
        out.feed(json.dumps(event))
    out.feed("not json")
    assert out.session_id == "s1"
    assert out.steps == ["Edit: /w/a.py"]
    assert out.last_text == "Checking."
    assert out.result == "All done" and not out.is_error


def test_error_results_carry_their_reason():
    out = TurnOutput()
    out.feed(json.dumps({"type": "result", "subtype": "error_max_turns", "is_error": True}))
    assert out.is_error and out.result == "error_max_turns"


def test_bash_steps_prefer_the_description():
    assert summarize_tool("Bash", {"command": "ls -la", "description": "List files"}) \
        == "Bash: List files"
    assert summarize_tool("Bash", {"command": "ls   -la\n"}) == "Bash: ls -la"
    assert summarize_tool("TodoWrite", {}) == "TodoWrite"
