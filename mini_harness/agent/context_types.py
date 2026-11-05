"""上下文配置、统计、请求快照与错误类型。"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any


class ContextError(RuntimeError):
    """上下文构建失败的基类。"""


class ContextBudgetExceeded(ContextError):
    """System Prompt + 当前消息 + 必要开销已经超过输入预算。

    Attributes:
        estimated_tokens: 超预算部分的最小估算总量。
        available_input_tokens: ``max_context_tokens - reserve_output_tokens``。
        breakdown: 各固定开销的明细，便于 CLI/测试定位是谁占用了预算。
    """

    def __init__(
        self,
        *,
        estimated_tokens: int,
        available_input_tokens: int,
        breakdown: dict[str, int],
    ) -> None:
        self.estimated_tokens = estimated_tokens
        self.available_input_tokens = available_input_tokens
        self.breakdown = dict(breakdown)
        system_tokens = breakdown.get("system_prompt", 0)
        memory_tokens = breakdown.get("long_term_memory", 0)
        summary_tokens = breakdown.get("summary", 0)
        current_tokens = breakdown.get("current_message", 0)
        tool_tokens = breakdown.get("tool_schema", 0)
        margin_tokens = breakdown.get("safety_margin", 0)
        super().__init__(
            "当前请求估算需要 "
            f"{estimated_tokens} tokens，超过输入预算 "
            f"{available_input_tokens} tokens"
            "（System Prompt "
            f"{system_tokens}（其中 Long-term Memory {memory_tokens}） + "
            f"Session Summary {summary_tokens} + 当前消息 {current_tokens} + "
            f"工具 Schema {tool_tokens} + 安全余量 {margin_tokens}）。"
            "请缩短当前输入，或提高 ContextConfig.max_context_tokens / "
            "降低 reserve_output_tokens；Session 历史不会被清空或修改。"
        )


@dataclass(slots=True)
class ContextConfig:
    """V2 上下文预算配置。

    Args:
        max_context_tokens: 模型上下文窗口的估算总量。教学默认 8192，不能
            假定适合所有模型，调用方应按实际模型配置。
        reserve_output_tokens: 为模型输出预留的 Token。同时作为估算误差的
            安全余量。
        include_agents_md: 是否读取 Workspace 根目录的 AGENTS.md。
        agents_md_max_bytes: AGENTS.md 最大字节数。超过限制时整份跳过，
            不做半份截断，避免模型拿到被截断的规则。
        safety_margin_tokens: 额外从历史预算中扣除的安全余量；默认 0，
            因为 ``reserve_output_tokens`` 已经承担主要缓冲。
        compact_soft_ratio: 工具循环中的软 Compact 阈值，按可用输入预算的
            比例计算。达到该比例时提前压缩已完成历史，避免下一次工具结果
            直接撞上硬阈值。默认 0.8（80%）。
        max_tool_result_tokens: 请求超预算时，单条 Tool Result 的首轮回退
            Token 上限。请求放得下时保留全文；原始结果始终保存在 Session。
        enable_auto_compact: 是否允许 ContextGovernor 自动调用 LLM 生成摘要；
            关闭后仍会执行确定性截断与预算保护。
        memory_max_tokens: 注入 System Prompt 的 MEMORY.md 最大估算 Token；
            超出时按预算截断并标注，避免长期记忆挤占全部输入空间。
        idle_compact_after_minutes: Session 空闲多少分钟后允许后台压缩；
            0 表示关闭 Idle Compaction。
        idle_compact_check_interval_seconds: 后台空闲检查间隔秒数；
            0 表示关闭后台检查。
        dream_enabled: 是否允许在 Agent 运行期间自动触发 Dream。
        dream_interval_hours: 自动 Dream 的检查间隔小时数。
        dream_max_entries: 单次 Dream 最多处理的归档摘要条数。
    """

    max_context_tokens: int = 8192
    reserve_output_tokens: int = 1024
    include_agents_md: bool = True
    agents_md_max_bytes: int = 16 * 1024
    safety_margin_tokens: int = 0
    compact_soft_ratio: float = 0.8
    max_tool_result_tokens: int = 2048
    enable_auto_compact: bool = True
    memory_max_tokens: int = 4096
    idle_compact_after_minutes: float = 15
    idle_compact_check_interval_seconds: float = 60
    dream_enabled: bool = True
    dream_interval_hours: float = 2
    dream_max_entries: int = 20

    def __post_init__(self) -> None:
        if self.max_context_tokens <= 0:
            raise ValueError("max_context_tokens 必须大于 0")
        if self.reserve_output_tokens < 0:
            raise ValueError("reserve_output_tokens 不能为负数")
        if self.reserve_output_tokens >= self.max_context_tokens:
            raise ValueError(
                "reserve_output_tokens 必须小于 max_context_tokens"
            )
        if self.agents_md_max_bytes <= 0:
            raise ValueError("agents_md_max_bytes 必须大于 0")
        if self.safety_margin_tokens < 0:
            raise ValueError("safety_margin_tokens 不能为负数")
        if not 0 < self.compact_soft_ratio <= 1:
            raise ValueError("compact_soft_ratio 必须在 (0, 1] 范围内")
        if self.max_tool_result_tokens <= 0:
            raise ValueError("max_tool_result_tokens 必须大于 0")
        if self.memory_max_tokens <= 0:
            raise ValueError("memory_max_tokens 必须大于 0")
        if self.idle_compact_after_minutes < 0:
            raise ValueError("idle_compact_after_minutes 不能为负数")
        if self.idle_compact_check_interval_seconds < 0:
            raise ValueError("idle_compact_check_interval_seconds 不能为负数")
        if self.dream_interval_hours < 0:
            raise ValueError("dream_interval_hours 不能为负数")
        if self.dream_max_entries <= 0:
            raise ValueError("dream_max_entries 必须大于 0")

    @classmethod
    def from_provider(
        cls,
        provider: object,
        *,
        fallback: "ContextConfig | None" = None,
    ) -> "ContextConfig":
        """按 Provider 声明的模型能力构造预算；未知能力保持兼容默认值。"""
        base = fallback or cls()
        context_window = int(getattr(provider, "context_window_tokens", 0) or 0)
        max_output = int(getattr(provider, "max_output_tokens", 0) or 0)
        if context_window <= 0:
            return base
        if max_output <= 0 or max_output >= context_window:
            max_output = min(4096, max(1, context_window // 4))
        return replace(
            base,
            max_context_tokens=context_window,
            reserve_output_tokens=min(max_output, 4096),
        )

    @property
    def available_input_tokens(self) -> int:
        """可用于输入消息的预算（不含输出预留）。"""
        return self.max_context_tokens - self.reserve_output_tokens


@dataclass(slots=True)
class ContextStats:
    """一次上下文构建的可观察统计。

    该对象只包含数量和估算值，不包含聊天正文、API Key 或完整历史，因此
    可以安全地交给 CLI ``/context`` 展示或写入 Debug 日志。
    """

    workspace: str = ""
    max_context_tokens: int = 0
    reserve_output_tokens: int = 0
    available_input_tokens: int = 0
    system_prompt_tokens: int = 0
    agents_md_status: str = "missing"
    agents_md_chars: int = 0
    agents_md_tokens: int = 0
    current_message_tokens: int = 0
    tool_schema_tokens: int = 0
    safety_margin_tokens: int = 0
    history_budget_tokens: int = 0
    session_message_count: int = 0
    selected_history_messages: int = 0
    dropped_history_messages: int = 0
    selected_history_tokens: int = 0
    history_digest_count: int = 0
    estimated_input_tokens: int = 0
    session_role_counts: dict[str, int] = field(default_factory=dict)
    selected_history_role_counts: dict[str, int] = field(default_factory=dict)
    # V2.2 统计字段放在末尾，避免改变旧字段的位置参数顺序。
    long_term_memory_tokens: int = 0
    long_term_memory_status: str = "disabled"
    summary_tokens: int = 0
    summary_boundary: int = 0
    unarchived_message_count: int = 0

    @property
    def agents_md_loaded(self) -> bool:
        """AGENTS.md 是否在本轮实际加载。"""
        return self.agents_md_status == "loaded"

    def log_line(self) -> str:
        """生成适合 Debug 日志的单行摘要。"""
        return (
            f"workspace={self.workspace} "
            f"system={self.system_prompt_tokens} "
            f"current={self.current_message_tokens} "
            f"tool_schema={self.tool_schema_tokens} "
            f"memory={self.long_term_memory_tokens} "
            f"summary={self.summary_tokens} "
            f"history_budget={self.history_budget_tokens} "
            f"selected={self.selected_history_messages} "
            f"dropped={self.dropped_history_messages} "
            f"digests={self.history_digest_count} "
            f"estimated_input={self.estimated_input_tokens}/"
            f"{self.available_input_tokens} "
            f"agents_md={self.agents_md_status}"
        )


@dataclass(slots=True)
class ProviderRequestSnapshot:
    """一次真实 Provider 请求的只读诊断快照。

    由 ``AgentRunner`` 在调用 ``provider.chat`` / ``provider.chat_stream``
    之前记录，因此 ``/last`` 看到的是最后一次真正发送的请求，而不是
    ContextBuilder 初次构造的版本。

    快照只保存 messages 与 Tool Schema，不包含 Provider 配置、API Key 或
    认证 Header，可以安全地留在内存中供 CLI 展示。
    """

    iteration: int = 0
    call_index: int = 1
    request_kind: str = "normal"
    messages: list[dict[str, Any]] = field(default_factory=list)
    estimated_tokens: int = 0
    available_input_tokens: int = 0
    max_output_tokens: int = 0
    accepted_message_count: int = 0
    compacted: bool = False
    compacted_message_count: int = 0
    accepted_compacted: bool = False
    accepted_summarized_messages: int = 0
    tool_message_count: int = 0
    tool_definitions: list[dict[str, Any]] = field(default_factory=list)
    compaction_error: str | None = None
    finish_reason: str = ""

    def log_line(self) -> str:
        """生成适合 Debug 日志的单行摘要。"""
        return (
            f"iteration={self.iteration} call={self.call_index} "
            f"kind={self.request_kind} messages={len(self.messages)} "
            f"tools={self.tool_message_count} "
            f"estimated_input={self.estimated_tokens}/"
            f"{self.available_input_tokens} "
            f"max_output={self.max_output_tokens} "
            f"accepted={self.accepted_message_count} "
            f"compacted={self.compacted} "
            f"accepted_compacted={self.accepted_compacted}"
        )


@dataclass(slots=True)
class BuiltContext:
    """``build_context`` 的完整返回值。"""

    messages: list[dict[str, Any]]
    stats: ContextStats
