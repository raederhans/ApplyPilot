from __future__ import annotations

from applypilot.apply import agent_report_mcp, ats_tools_mcp, credential_relay_mcp


def _listed_tools(module: object) -> list[dict[str, object]]:
    response = module._handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert response is not None
    return response["result"]["tools"]


def test_mcp_tool_declarations_are_derived_from_canonical_specs() -> None:
    ats_specs = ats_tools_mcp._tool_specs()
    assert _listed_tools(ats_tools_mcp) == [
        ats_tools_mcp._mcp_tool(spec) for spec in ats_specs
    ]

    report_spec = agent_report_mcp._report_spec()
    assert _listed_tools(agent_report_mcp) == [
        agent_report_mcp._mcp_tool(report_spec)
    ]

    credential_specs = credential_relay_mcp._tool_specs()
    assert _listed_tools(credential_relay_mcp) == [
        credential_relay_mcp._mcp_tool(spec) for spec in credential_specs
    ]

    assert ats_tools_mcp._broker_surface()[1].surface_hash
    assert agent_report_mcp._broker_surface()[1].surface_hash
    assert credential_relay_mcp._broker_surface()[1].surface_hash


def test_active_mcp_surfaces_only_list_read_and_report_tools(monkeypatch) -> None:
    monkeypatch.setenv("APPLYPILOT_TOOL_BROKER_MODE", "active")

    ats_tools = _listed_tools(ats_tools_mcp)
    report_tools = _listed_tools(agent_report_mcp)
    credential_tools = _listed_tools(credential_relay_mcp)

    assert [tool["name"] for tool in ats_tools] == [
        spec.name for spec in ats_tools_mcp._tool_specs()
    ]
    assert [tool["name"] for tool in report_tools] == ["report_agent_turn"]
    assert credential_tools == []


def test_active_ats_call_passes_broker_and_reaches_existing_handler(monkeypatch) -> None:
    monkeypatch.setenv("APPLYPILOT_TOOL_BROKER_MODE", "active")

    response = ats_tools_mcp._handle(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "detect_ats",
                "arguments": {"url": "https://boards.greenhouse.io/example/jobs/1"},
            },
        }
    )

    assert response is not None
    assert response["result"]["isError"] is False


def test_dynamic_handler_map_is_the_active_credential_free_ats_surface() -> None:
    handlers = ats_tools_mcp.dynamic_tool_handlers()
    expected = ats_tools_mcp.ToolBroker(
        ats_tools_mcp.CapabilityRegistry(ats_tools_mcp.dynamic_tool_specs()),
        mode="active",
    ).compile_surface(
        phase="prepare",
        state=("ats_unknown", "ats_workday"),
    )

    assert list(handlers) == expected.names()
    assert list(handlers) == ["detect_ats"]
    assert all("credential" not in name for name in handlers)
    result = handlers["detect_ats"](
        {"url": "https://boards.greenhouse.io/example/jobs/1"}
    )
    assert result["adapter"] == "greenhouse"


def test_active_credential_call_is_denied_before_secret_or_browser_access(
    monkeypatch,
) -> None:
    monkeypatch.setenv("APPLYPILOT_TOOL_BROKER_MODE", "active")
    monkeypatch.setenv("APPLYPILOT_CREDENTIAL_RELAY_AUTHORIZED", "1")
    monkeypatch.setattr(
        credential_relay_mcp,
        "_credential_path",
        lambda: (_ for _ in ()).throw(AssertionError("secret store was accessed")),
    )

    response = credential_relay_mcp._handle(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "fill_ats_credentials",
                "arguments": {"field": "password"},
            },
        }
    )

    assert response is not None
    assert response["result"]["isError"] is True
    assert "tool broker denied fill_ats_credentials" in response["result"]["content"][0][
        "text"
    ]


def test_shadow_broker_does_not_replace_existing_credential_authorization(
    monkeypatch,
) -> None:
    monkeypatch.delenv("APPLYPILOT_TOOL_BROKER_MODE", raising=False)
    monkeypatch.delenv("APPLYPILOT_CREDENTIAL_RELAY_AUTHORIZED", raising=False)

    response = credential_relay_mcp._handle(
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {
                "name": "fill_ats_credentials",
                "arguments": {"field": "password"},
            },
        }
    )

    assert response is not None
    assert response["result"]["isError"] is True
    assert response["result"]["content"][0]["text"] == (
        "Credential relay is not authorized."
    )
