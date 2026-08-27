"""Deterministic browser capability routing for application workers.

The browser runtime and the interaction driver are separate concerns:
Playwright is the in-process structured driver, Edge is the fast default
runtime, and CloakBrowser is a bounded pre-submit runtime fallback. Windows
Computer Use is represented as an external handoff because the isolated
Codex/Claude subprocess does not expose the desktop Computer Use API.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from applypilot.runtime_settings import load_runtime_settings

SUPPORTED_INTERACTION_MODES = {"auto", "playwright"}

EXPLICIT_BROWSER_BLOCK_PREFIXES: tuple[str, ...] = (
    "site_blocked",
    "cloudflare",
    "blocked_by_cloudflare",
    "automation_blocked",
    "bot_detected",
    "browser_challenge",
)

COMPUTER_USE_HANDOFF_REASONS: tuple[str, ...] = (
    "browser_mcp_unavailable",
    "computer_use_handoff_required",
    "native_dialog_required",
    "visual_only_control",
)


@dataclass(frozen=True)
class ControlRoute:
    """One immutable interaction/runtime assignment for an agent turn."""

    interaction_driver: str
    browser_runtime: str
    phase: str
    reason_code: str
    contract_version: int = 1
    single_writer: bool = True
    submit_owner: str = "playwright"
    requires_fresh_observation: bool = True

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def resolve_interaction_mode(value: str | None = None) -> str:
    """Resolve the supported interaction policy without inventing an adapter."""
    return load_runtime_settings().resolve_interaction_mode(value)


def initial_route(browser_backend: str, *, phase: str = "prepare") -> ControlRoute:
    """Select the fast structured primary route."""
    runtime = "edge" if browser_backend == "auto" else browser_backend
    reason = "primary_playwright" if runtime == "edge" else "explicit_cloak_runtime"
    return ControlRoute(
        interaction_driver="playwright",
        browser_runtime=runtime,
        phase=phase,
        reason_code=reason,
    )


def normalized_failure_reason(result: str) -> str:
    reason = result.split(":", 1)[-1] if ":" in result else result
    return reason.casefold().replace(" ", "_").replace("-", "_")


def cloak_fallback_route(
    result: str,
    *,
    requested_browser_backend: str,
    phase: str,
    current_runtime: str,
    fallback_already_used: bool,
) -> ControlRoute | None:
    """Return one eligible Edge -> Cloak transition, otherwise fail closed."""
    if (
        requested_browser_backend != "auto"
        or phase != "prepare"
        or current_runtime != "edge"
        or fallback_already_used
    ):
        return None
    reason = normalized_failure_reason(result)
    if not reason.startswith(EXPLICIT_BROWSER_BLOCK_PREFIXES):
        return None
    return ControlRoute(
        interaction_driver="playwright",
        browser_runtime="cloak",
        phase="prepare",
        reason_code="explicit_browser_block",
    )


def computer_use_handoff_allowed(
    result: str, *, interaction_mode: str, phase: str, submit_started: bool
) -> bool:
    """Whether an outer agent may take a bounded visual-control handoff."""
    if interaction_mode != "auto" or phase != "prepare" or submit_started:
        return False
    return normalized_failure_reason(result).startswith(COMPUTER_USE_HANDOFF_REASONS)


def prompt_control_contract(
    route: ControlRoute, *, interaction_mode: str, resume_existing_page: bool
) -> dict[str, object]:
    """Build the route contract shown to the isolated browser agent."""
    requestable_handoffs = []
    if interaction_mode == "auto" and route.phase == "prepare":
        requestable_handoffs.append("computer_use")
    return {
        **route.as_dict(),
        "requestable_handoffs": requestable_handoffs,
        "resume_existing_page": resume_existing_page,
        "handoff_requires_fresh_observation": True,
        "runtime_switch_after_submit_forbidden": True,
    }
