"""Compatibility imports for the agent runtime.

Implementation owners are agent_configuration (pure configuration),
agent_commands (provider/MCP command assembly), and agent_process (lifecycle).
New code should import its owner directly. Keep this facade stateless: do not
copy mutable runtimes or forward monkeypatches into implementation modules.
"""

# Legacy command tests import these shared modules from this path. Retaining
# the aliases does not introduce a second configuration or process state.
import platform
import shutil

from applypilot.apply.agent_commands import (
    APPLICATION_TOOL_ENV_VARS,
    CONTROL_REPORT_ENV_VARS,
    CREDENTIAL_RELAY_ENV_VARS,
    DEFAULT_MAILBOX_BLOCKED_TOOLS,
    _toml_skill_config,
    _toml_value,
    apply_mcp_process_environment,
    bound_visual_bridge_dir,
    build_agent_command,
    make_mcp_config,
    resolve_claude_command,
    resolve_codex_command,
)
from applypilot.apply.agent_configuration import (
    AgentRuntimeConfiguration,
    ReasoningEffortResolution,
    resolve_agent_runtime_configuration,
    resolve_reasoning_effort,
    resolve_reasoning_effort_configuration,
)
from applypilot.apply.agent_process import (
    RuntimeContinuityError,
    SubprocessAgentRuntime,
    SubprocessLaunchSpec,
    SubprocessParentIdentity,
    SubprocessRuntimeAdapter,
    SubprocessRuntimeError,
    SubprocessRuntimeHealth,
    _current_working_set_bytes,
    process_rss_bytes,
    start_timeout_watchdog,
)

__all__ = [
    "APPLICATION_TOOL_ENV_VARS",
    "CONTROL_REPORT_ENV_VARS",
    "CREDENTIAL_RELAY_ENV_VARS",
    "DEFAULT_MAILBOX_BLOCKED_TOOLS",
    "AgentRuntimeConfiguration",
    "ReasoningEffortResolution",
    "RuntimeContinuityError",
    "SubprocessAgentRuntime",
    "SubprocessLaunchSpec",
    "SubprocessParentIdentity",
    "SubprocessRuntimeAdapter",
    "SubprocessRuntimeError",
    "SubprocessRuntimeHealth",
    "_current_working_set_bytes",
    "_toml_skill_config",
    "_toml_value",
    "apply_mcp_process_environment",
    "bound_visual_bridge_dir",
    "build_agent_command",
    "make_mcp_config",
    "platform",
    "process_rss_bytes",
    "resolve_agent_runtime_configuration",
    "resolve_claude_command",
    "resolve_codex_command",
    "resolve_reasoning_effort",
    "resolve_reasoning_effort_configuration",
    "shutil",
    "start_timeout_watchdog",
]
