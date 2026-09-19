import pytest

from applypilot.apply.visual_bridge import VisualBridgeError, _validate_operation
from applypilot.apply.visual_bridge_mcp import _tool


@pytest.mark.parametrize("operation,args", [
    ("fill_control", {"field_key": "observed", "value": "2026-11-10"}),
    ("select_control", {"field_key": "observed", "value": "Singapore"}),
    ("set_checked", {"field_key": "observed", "checked": False}),
])
def test_form_operations_require_fresh_observation_and_typed_arguments(operation, args):
    _validate_operation(operation, "fresh", args)
    with pytest.raises(VisualBridgeError):
        _validate_operation(operation, None, args)
    with pytest.raises(VisualBridgeError):
        _validate_operation(operation, "fresh", {**args, "selector": "guessed"})
    value_key = "checked" if operation == "set_checked" else "value"
    with pytest.raises(VisualBridgeError):
        _validate_operation(operation, "fresh", {**args, value_key: 1})
    assert operation in _tool()["inputSchema"]["properties"]["operation"]["enum"]
