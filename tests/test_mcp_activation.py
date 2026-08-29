from applypilot.apply.capabilities import DEFAULT_PLAYWRIGHT_MCP_PACKAGE, resolve_playwright_mcp_spec
from applypilot.apply.email_routing import MailboxMcpSpec, mailbox_mcp_for_phase


def test_playwright_default_is_pinned_but_environment_can_override() -> None:
    assert DEFAULT_PLAYWRIGHT_MCP_PACKAGE == "@playwright/mcp@0.0.79"
    assert resolve_playwright_mcp_spec(None, environ={}).package == "@playwright/mcp@0.0.79"
    assert resolve_playwright_mcp_spec(
        None, environ={"APPLYPILOT_PLAYWRIGHT_MCP_PACKAGE": "@playwright/mcp@0.0.80"}
    ).package == "@playwright/mcp@0.0.80"


def test_mailbox_is_phase_scoped() -> None:
    spec = MailboxMcpSpec()
    assert mailbox_mcp_for_phase(spec, submission_phase="prepare").enabled is True
    assert mailbox_mcp_for_phase(spec, submission_phase="submit").enabled is False
    assert mailbox_mcp_for_phase(
        spec, submission_phase="submit", direct_email_send_authorized=True
    ).enabled is True
    assert mailbox_mcp_for_phase(
        spec, submission_phase="submit", verification_resume=True
    ).enabled is True
