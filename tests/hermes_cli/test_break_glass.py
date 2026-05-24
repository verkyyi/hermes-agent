import json

from hermes_cli import break_glass


def test_classify_failure_request_id_kwarg():
    text = "TypeError: handle_function_call() got an unexpected keyword argument 'request_id'"
    assert break_glass.classify_failure(text) == "tool_dispatch_metadata_kwarg"


def test_classify_failure_unknown_skill():
    assert break_glass.classify_failure("Error: Unknown skill(s): tailnet-service-ops") == "unknown_forced_skill"


def test_direct_tool_smoke_passes():
    result = break_glass.run_direct_tool_smoke()
    assert result["ok"] is True, json.dumps(result, indent=2, ensure_ascii=False, default=str)
    assert result["final_status"] == "done"
    assert [s["tool"] for s in result["steps"]] == ["kanban_show", "terminal", "kanban_complete"]


def test_py_compile_command_passes():
    result = break_glass.run_py_compile()
    assert result["ok"] is True, result.get("output_tail")
