"""Agent 包公开接口。"""

from .compaction import (
    CompactionOutcome,
    ContextCompactor,
    ContextGovernor,
    GovernedRequest,
    SUMMARY_SYSTEM_PROMPT,
    truncate_tool_results,
)
from .context import (
    BuiltContext,
    ContextBudgetExceeded,
    ContextBuilder,
    ContextConfig,
    ContextError,
    ContextStats,
    ProviderRequestSnapshot,
    estimate_tokens,
)
from .loop import AgentLoop
from .prompt import build_system_prompt
from .runner import AgentRunResult, AgentRunSpec, AgentRunner

__all__ = [
    "AgentLoop",
    "AgentRunResult",
    "AgentRunSpec",
    "AgentRunner",
    "BuiltContext",
    "CompactionOutcome",
    "ContextBudgetExceeded",
    "ContextBuilder",
    "ContextCompactor",
    "ContextConfig",
    "ContextError",
    "ContextGovernor",
    "ContextStats",
    "GovernedRequest",
    "ProviderRequestSnapshot",
    "SUMMARY_SYSTEM_PROMPT",
    "build_system_prompt",
    "estimate_tokens",
    "truncate_tool_results",
]
