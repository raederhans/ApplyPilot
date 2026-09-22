"""Compatibility imports for the agent runtime.

Implementation owners are agent_configuration (pure configuration),
agent_commands (provider/MCP command assembly), and agent_process (lifecycle).
New code should import its owner directly. Keep this facade stateless: do not
copy mutable runtimes or forward monkeypatches into implementation modules.
"""

# Legacy command tests import these shared modules from this path. Retaining
# the aliases does not introduce a second configuration or process state.
import platform as platform
import shutil as shutil

from applypilot.apply.agent_commands import (
    APPLICATION_TOOL_ENV_VARS as APPLICATION_TOOL_ENV_VARS,
    CONTROL_REPORT_ENV_VARS as CONTROL_REPORT_ENV_VARS,
    CREDENTIAL_RELAY_ENV_VARS as CREDENTIAL_RELAY_ENV_VARS,
    DEFAULT_MAILBOX_BLOCKED_TOOLS as DEFAULT_MAILBOX_BLOCKED_TOOLS,
    _toml_skill_config as _toml_skill_config,
    _toml_value as _toml_value,
    apply_mcp_process_environment as apply_mcp_process_environment,
    bound_visual_bridge_dir as bound_visual_bridge_dir,
    build_agent_command as build_agent_command,
    make_mcp_config as make_mcp_config,
    resolve_claude_command as resolve_claude_command,
    resolve_codex_command as resolve_codex_command,
)
from applypilot.apply.agent_configuration import (
    AgentRuntimeConfiguration as AgentRuntimeConfiguration,
    ReasoningEffortResolution as ReasoningEffortResolution,
    resolve_agent_runtime_configuration as resolve_agent_runtime_configuration,
    resolve_reasoning_effort as resolve_reasoning_effort,
    resolve_reasoning_effort_configuration as resolve_reasoning_effort_configuration,
)
from applypilot.apply.agent_process import (
    RuntimeContinuityError as RuntimeContinuityError,
    SubprocessAgentRuntime as SubprocessAgentRuntime,
    SubprocessLaunchSpec as SubprocessLaunchSpec,
    SubprocessParentIdentity as SubprocessParentIdentity,
    SubprocessRuntimeAdapter as SubprocessRuntimeAdapter,
    SubprocessRuntimeError as SubprocessRuntimeError,
    SubprocessRuntimeHealth as SubprocessRuntimeHealth,
    _current_working_set_bytes as _current_working_set_bytes,
    process_rss_bytes as process_rss_bytes,
    start_timeout_watchdog as start_timeout_watchdog,
)
