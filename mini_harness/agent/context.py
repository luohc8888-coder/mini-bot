"""ContextBuilder：把 Session 历史构建成一次模型请求的上下文。

职责边界（V2 的核心）：

- ``SessionManager`` / ``Session`` 只负责持久化完整历史；
- ``ContextBuilder`` 只负责读取约定的 Workspace 配置、估算 Token、选择
  历史并生成 ``request_messages``；
- ``AgentRunner`` 只负责执行 Provider → Tool → Provider 循环。

本模块不读写 Session 文件、不调用 Provider、不执行工具，也不修改传入的
history。它唯一的外部 I/O 是读取 Workspace 根目录下的 ``AGENTS.md``。
"""

from __future__ import annotations

import json
import logging
import math
from collections import Counter
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from mini_harness.prompts.defaults import build_default_system_prompt

logger = logging.getLogger(__name__)

AGENTS_MD_FILENAME = "AGENTS.md"
# 每条消息除了 content 之外，角色、分隔符、工具字段等也会占用真实 Token。
# 这里用固定开销做保守估算，避免只数 content 导致请求明显超预算。
MESSAGE_OVERHEAD_TOKENS = 4
TOOL_CALL_OVERHEAD_TOKENS = 8
TOOL_DEFINITION_OVERHEAD_TOKENS = 8


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
        current_tokens = breakdown.get("current_message", 0)
        tool_tokens = breakdown.get("tool_schema", 0)
        margin_tokens = breakdown.get("safety_margin", 0)
        super().__init__(
            "当前请求估算需要 "
            f"{estimated_tokens} tokens，超过输入预算 "
            f"{available_input_tokens} tokens"
            "（System Prompt "
            f"{system_tokens} + 当前消息 {current_tokens} + 工具 Schema "
            f"{tool_tokens} + 安全余量 {margin_tokens}）。"
            "请缩短当前输入，或提高 ContextConfig.max_context_tokens / "
            "降低 reserve_output_tokens；Session 历史不会被清空或修改。"
        )


def estimate_tokens(text: str) -> int:
    """对文本做保守的 Token 估算。

    V2 不绑定具体模型 Tokenizer，使用如下近似：

    - ASCII 字符约每 4 个字符 1 Token（向上取整）；
    - 非 ASCII 字符（中文、emoji 等）按 1 字符 1 Token 估算。

    Notes:
        估算 Token **不等于**模型实际 Token 数。不同模型、不同工具 Schema
        和不同消息格式都会有差异；``reserve_output_tokens`` 同时承担输出
        预留与估算误差的安全余量。
    """
    if not isinstance(text, str):
        text = str(text)
    if not text:
        return 0
    ascii_chars = 0
    non_ascii_chars = 0
    for char in text:
        if ord(char) < 128:
            ascii_chars += 1
        else:
            non_ascii_chars += 1
    return max(1, math.ceil(ascii_chars / 4) + non_ascii_chars)


def estimate_message_tokens(message: dict[str, Any]) -> int:
    """估算单条消息的结构化 Token 开销。

    除 content 外，还计入 role、tool_calls、tool_call_id、name 和固定消息
    结构开销。这样 Tool Calling 历史不会因为只计算 content 而被低估。
    """
    if not isinstance(message, dict):
        return estimate_tokens(str(message)) + MESSAGE_OVERHEAD_TOKENS

    total = MESSAGE_OVERHEAD_TOKENS
    role = message.get("role")
    if role is not None:
        total += estimate_tokens(str(role))

    content = message.get("content")
    if isinstance(content, str):
        total += estimate_tokens(content)
    elif content is not None:
        total += estimate_tokens(_json_text(content))

    tool_calls = message.get("tool_calls")
    if tool_calls:
        total += TOOL_CALL_OVERHEAD_TOKENS
        total += estimate_tokens(_json_text(tool_calls))

    for key in ("tool_call_id", "name", "reasoning_content"):
        value = message.get(key)
        if value is not None:
            total += estimate_tokens(str(value))
    return total


def estimate_messages_tokens(messages: Iterable[dict[str, Any]]) -> int:
    """估算一组消息的总 Token。"""
    return sum(estimate_message_tokens(message) for message in messages)


def estimate_tool_definitions_tokens(
    tool_definitions: Iterable[dict[str, Any]] | None,
) -> int:
    """估算 Provider 随请求携带的工具 Schema 开销。

    Tool Schema 不在 ``messages`` 里，但真实模型会把它计入上下文窗口，
    因此预算必须为它预留空间。
    """
    definitions = list(tool_definitions or [])
    if not definitions:
        return 0
    return TOOL_DEFINITION_OVERHEAD_TOKENS + estimate_tokens(
        _json_text(definitions)
    )


def _json_text(value: Any) -> str:
    """稳定地序列化为估算用文本；不用于持久化。"""
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError):
        return str(value)


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
    """

    max_context_tokens: int = 8192
    reserve_output_tokens: int = 1024
    include_agents_md: bool = True
    agents_md_max_bytes: int = 16 * 1024
    safety_margin_tokens: int = 0
    compact_soft_ratio: float = 0.8
    max_tool_result_tokens: int = 2048
    enable_auto_compact: bool = True

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

    @classmethod
    def from_provider(
        cls,
        provider: object,
        *,
        fallback: "ContextConfig | None" = None,
    ) -> "ContextConfig":
        """按 Provider 声明的模型能力构造预算；未知能力保持兼容默认值。"""
        context_window = int(getattr(provider, "context_window_tokens", 0) or 0)
        max_output = int(getattr(provider, "max_output_tokens", 0) or 0)
        if context_window <= 0:
            return fallback or cls()
        if max_output <= 0 or max_output >= context_window:
            max_output = min(4096, max(1, context_window // 4))
        return cls(
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


class ContextBuilder:
    """按 V2 规则构建模型请求上下文。

    Args:
        workspace: 当前 Workspace。构造时会 ``expanduser().resolve()``，
            之后 AGENTS.md 永远定位到该绝对路径的根目录，不受进程 cwd
            或用户传入的相对路径影响。
        config: 上下文预算配置；None 时使用 ``ContextConfig()``。
        tool_definitions: 可选工具 Schema，用于预算预留。AgentLoop 会把
            ``ToolRegistry.get_definitions()`` 传入；纯 ContextBuilder
            测试可以省略。

    Notes:
        本类不访问 SessionManager、不调用 Provider、不执行工具。``history``
        只在内存中被读取和复制，永远不会被修改。
    """

    def __init__(
        self,
        workspace: Path,
        config: ContextConfig | None = None,
        *,
        tool_definitions: list[dict[str, Any]] | None = None,
    ) -> None:
        self.workspace = Path(workspace).expanduser().resolve()
        self.config = config or ContextConfig()
        self._tool_definitions = deepcopy(list(tool_definitions or []))
        self._tool_schema_tokens = estimate_tool_definitions_tokens(
            self._tool_definitions
        )
        self.last_stats: ContextStats | None = None
        # 仅用于调试：保存最近一次“真实发给 Provider”的请求快照。
        # 由 AgentRunner 通过 record_provider_request 写入；ContextBuilder
        # 自身只负责构建初始上下文，不能把 initial build 误当成 /last。
        self.last_provider_request: ProviderRequestSnapshot | None = None
        # 兼容旧字段：与 last_provider_request 同步，便于旧调用方读取。
        self.last_request_messages: list[dict[str, Any]] | None = None
        # 与 last_request_messages 配套保存的 Tool Schema。Tool 定义不在
        # messages 中，而是 Provider 请求的独立 tools 参数。
        self.last_tool_definitions: list[dict[str, Any]] | None = None
        # 最近一次 _select_history 生成的“完成轮次摘要”数量，供 ContextStats
        # 展示；不是跨请求状态。
        self._last_selection_digest_count = 0

    @property
    def tool_schema_tokens(self) -> int:
        """当前工具 Schema 的估算 Token。"""
        return self._tool_schema_tokens

    def set_tool_definitions(
        self,
        tool_definitions: list[dict[str, Any]] | None,
    ) -> None:
        """更新用于预算估算的工具 Schema。

        V2 中工具集合在 CLI 启动后固定；保留该方法便于测试或后续版本替换
        工具注册表时重新计算预算。
        """
        self._tool_definitions = deepcopy(list(tool_definitions or []))
        self._tool_schema_tokens = estimate_tool_definitions_tokens(
            self._tool_definitions
        )

    def record_provider_request(
        self,
        snapshot: ProviderRequestSnapshot,
    ) -> None:
        """记录一次真实 Provider 请求，供 CLI ``/last`` 诊断使用。

        Args:
            snapshot: ``AgentRunner`` 在实际调用 Provider 前构造的快照。

        Notes:
            该方法只保存内存副本，不写日志文件、不写 Session，也不会包含
            API Key 等认证信息。若用户调用 ``/clear``，CLI 会同时清空它。
        """
        self.last_provider_request = deepcopy(snapshot)
        self.last_request_messages = deepcopy(snapshot.messages)
        self.last_tool_definitions = deepcopy(snapshot.tool_definitions)
        logger.debug("Provider request: %s", snapshot.log_line())

    def build_system_prompt(self) -> str:
        """构建当前轮次的 System Prompt。

        Returns:
            默认 System Prompt；若 Workspace 根目录存在非空且合法的
            ``AGENTS.md``，追加其内容。每次调用都会重新读取文件，因此文件
            修改后下一轮立即生效。
        """
        prompt, _status, _agents_chars, _agents_tokens = (
            self._build_system_prompt_with_status()
        )
        return prompt

    def load_agents_md(self) -> str | None:
        """读取 Workspace 根目录的 AGENTS.md。

        Returns:
            文件内容；不存在、为空、超限、不是 UTF-8 或读取失败时返回 None。

        Notes:
            失败不会抛出异常打断对话；状态记录在 ``ContextStats`` 和日志中。
            V2 只读取根目录的这一份 AGENTS.md，不递归查找、不合并多份文件。
        """
        content, _status = self._read_agents_md()
        return content

    def build_messages(
        self,
        history: list[dict[str, Any]],
        current_message: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """构建最终发送给 Provider 的 ``request_messages``。

        Args:
            history: Session 的完整历史；本方法不会修改它。
            current_message: 当前用户消息，必须是带 ``role``/``content`` 的
                dict。为方便测试也接受普通字符串并包装为 user message。

        Returns:
            ``[system, *selected_history, current_user_message]``。

        Raises:
            ContextBudgetExceeded: System Prompt + 当前消息 + 必要开销已经
                超过输入预算。
        """
        return self.build_context(history, current_message).messages

    def build_context(
        self,
        history: list[dict[str, Any]],
        current_message: dict[str, Any] | str | None = None,
        *,
        check_budget: bool = True,
    ) -> BuiltContext:
        """构建上下文并返回消息与统计。

        Args:
            history: Session 完整历史。
            current_message: 当前消息；``None`` 表示只做上下文预览（``/context``
                使用，不包含当前输入）。
            check_budget: 为 True 时固定开销超预算会抛
                ``ContextBudgetExceeded``；为 False 时使用 0 历史预算，
                便于调试命令展示超预算状态。

        Returns:
            ``BuiltContext(messages, stats)``。
        """
        if not isinstance(history, list):
            raise TypeError("history 必须是消息列表")

        normalized_current = (
            None
            if current_message is None
            else _normalize_message(current_message)
        )

        prompt, agents_status, agents_chars, agents_tokens = (
            self._build_system_prompt_with_status()
        )
        system_tokens = estimate_message_tokens(
            {"role": "system", "content": prompt}
        )
        current_tokens = (
            estimate_message_tokens(normalized_current)
            if normalized_current is not None
            else 0
        )

        available_input = self.config.available_input_tokens
        fixed_actual = (
            system_tokens
            + current_tokens
            + self._tool_schema_tokens
        )
        fixed_with_margin = fixed_actual + self.config.safety_margin_tokens
        breakdown = {
            "system_prompt": system_tokens,
            "current_message": current_tokens,
            "tool_schema": self._tool_schema_tokens,
            "safety_margin": self.config.safety_margin_tokens,
        }

        if fixed_with_margin > available_input:
            if check_budget:
                raise ContextBudgetExceeded(
                    estimated_tokens=fixed_with_margin,
                    available_input_tokens=available_input,
                    breakdown=breakdown,
                )
            history_budget = 0
        else:
            history_budget = available_input - fixed_with_margin

        selected_history, selected_tokens = self._select_history(
            history,
            history_budget,
        )

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": prompt},
            *deepcopy(selected_history),
        ]
        if normalized_current is not None:
            messages.append(deepcopy(normalized_current))

        estimated_input = fixed_actual + selected_tokens
        # 防御性检查：按当前估算器，正常路径不会超过预算。若未来 estimator
        # 或选择器改动导致偏差，宁可报错也不发送明显超预算的请求。
        if check_budget and estimated_input > available_input:
            raise ContextBudgetExceeded(
                estimated_tokens=estimated_input,
                available_input_tokens=available_input,
                breakdown=breakdown,
            )

        # 注意：这里不能记录 last_request_messages。/last 必须反映 AgentRunner
        # 工具循环中最后一次真正发送给 Provider 的请求；ContextBuilder 只负责
        # 初次构建，真实请求快照由 ProviderRequestSnapshot 单独维护。
        stats = ContextStats(
            workspace=str(self.workspace),
            max_context_tokens=self.config.max_context_tokens,
            reserve_output_tokens=self.config.reserve_output_tokens,
            available_input_tokens=available_input,
            system_prompt_tokens=system_tokens,
            agents_md_status=agents_status,
            agents_md_chars=agents_chars,
            agents_md_tokens=agents_tokens,
            current_message_tokens=current_tokens,
            tool_schema_tokens=self._tool_schema_tokens,
            safety_margin_tokens=self.config.safety_margin_tokens,
            history_budget_tokens=history_budget,
            session_message_count=len(history),
            selected_history_messages=len(selected_history),
            dropped_history_messages=len(history) - len(selected_history),
            selected_history_tokens=selected_tokens,
            history_digest_count=self._last_selection_digest_count,
            estimated_input_tokens=estimated_input,
            session_role_counts=_role_counts(history),
            selected_history_role_counts=_role_counts(selected_history),
        )
        self.last_stats = stats
        logger.debug("Context built: %s", stats.log_line())
        return BuiltContext(messages=messages, stats=stats)

    def inspect(self, history: list[dict[str, Any]]) -> ContextStats:
        """只检查历史选择结果，不包含任何当前用户输入。

        CLI ``/context`` 使用该方法：它不会触发 LLM 调用，也不会保存
        Session。统计中的 ``estimated_input_tokens`` 不含当前输入。
        """
        return self.build_context(
            history,
            current_message=None,
            check_budget=False,
        ).stats

    def _build_system_prompt_with_status(
        self,
    ) -> tuple[str, str, int, int]:
        """返回 ``(prompt, agents_status, agents_chars, agents_tokens)``。"""
        base_prompt = build_default_system_prompt(self.workspace)
        agents_content, agents_status = self._read_agents_md()
        if not agents_content:
            return base_prompt, agents_status, 0, 0

        section = (
            "# Workspace Instructions (AGENTS.md)\n\n"
            "以下内容来自当前 Workspace 根目录的 AGENTS.md，是本项目的额外"
            "约束；若与上面的通用规则冲突，以 AGENTS.md 中的项目特定要求"
            "为准（安全边界与“不得编造工具结果”的规则除外）。\n\n"
            f"{agents_content.strip()}"
        )
        prompt = f"{base_prompt}\n\n{section}"
        return (
            prompt,
            agents_status,
            len(agents_content),
            estimate_tokens(section),
        )

    def _read_agents_md(self) -> tuple[str | None, str]:
        """读取 AGENTS.md 并返回 ``(content, status)``。

        status 取值: ``loaded`` / ``missing`` / ``empty`` / ``disabled`` /
        ``too_large`` / ``decode_error`` / ``read_error`` / ``not_file``。
        """
        if not self.config.include_agents_md:
            return None, "disabled"

        path = self.workspace / AGENTS_MD_FILENAME
        try:
            stat = path.stat()
        except FileNotFoundError:
            return None, "missing"
        except OSError as exc:
            logger.warning("无法读取 AGENTS.md 元信息：%s (%s)", path, exc)
            return None, "read_error"

        if not path.is_file():
            return None, "not_file"
        if stat.st_size > self.config.agents_md_max_bytes:
            logger.warning(
                "AGENTS.md 超过大小限制，已跳过：%s (%s > %s bytes)",
                path,
                stat.st_size,
                self.config.agents_md_max_bytes,
            )
            return None, "too_large"

        try:
            raw = path.read_bytes()
        except OSError as exc:
            logger.warning("无法读取 AGENTS.md：%s (%s)", path, exc)
            return None, "read_error"

        if len(raw) > self.config.agents_md_max_bytes:
            logger.warning(
                "AGENTS.md 读取期间超过大小限制，已跳过：%s (%s > %s bytes)",
                path,
                len(raw),
                self.config.agents_md_max_bytes,
            )
            return None, "too_large"

        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            logger.warning("AGENTS.md 不是合法 UTF-8，已跳过：%s (%s)", path, exc)
            return None, "decode_error"

        if not content.strip():
            return None, "empty"
        return content, "loaded"

    def _select_history(
        self,
        history: list[dict[str, Any]],
        history_budget: int,
    ) -> tuple[list[dict[str, Any]], int]:
        """按“完整用户轮次 + 关键结论优先”选择历史。

        选择规则:

        1. 以 user 消息为轮次边界切分历史；
        2. 从最近的轮次向前选择，完整轮次放得下就原样保留；
        3. 如果**最近一个已完成轮次**整轮超过剩余预算，不再直接丢弃，
           而是生成确定性的“完成轮次摘要”：保留 user 请求、工具名和最终
           Assistant 结论。这样大量 read_file/grep 输出不会把 P0 结论一起
           挤掉；
        4. 只要出现工具调用结构不合法，就立即停止向前保留，保证输出始终
           合法，不会产生孤立的 tool 消息或缺失 tool result 的 tool_calls；
        5. 当前用户消息由 build_context 单独追加，永远不会被历史选择影响。

        Returns:
            ``(selected_messages, estimated_tokens)``。不修改 history。
        """
        self._last_selection_digest_count = 0
        if history_budget <= 0 or not history:
            return [], 0

        turns = _split_history_into_turns(history)
        selected_turns: list[list[dict[str, Any]]] = []
        used_tokens = 0
        seen_conclusions: set[str] = set()
        # 只有“带最终纯文本 Assistant 回答”的轮次才算已完成。尾部可能
        # 存在上一轮空回答留下的 user-only 轮次，它不能阻止更早的已完成
        # 轮次生成结论摘要。
        selected_completed = False

        for turn in reversed(turns):
            if not _is_valid_turn(turn):
                # 无法保证该轮工具协议完整，停止向前保留；绝不能为了多保留
                # 旧消息而输出非法请求或跳过中间轮次。
                break
            turn_tokens = _estimate_turn_tokens(turn)
            if used_tokens + turn_tokens <= history_budget:
                selected_turns.insert(0, turn)
                used_tokens += turn_tokens
                conclusion = _final_assistant_content(turn)
                if conclusion:
                    seen_conclusions.add(_conclusion_signature(conclusion))
                    selected_completed = True
                continue

            # 整轮放不下。只对最近一个“已完成轮次”做摘要回退；更老的轮次
            # 继续按原来的边界停止，避免把连续历史拆得支离破碎。若已选中
            # 的只是尾部未完成 user-only 轮次，则仍允许为更早的已完成轮次
            # 生成结论摘要。
            if selected_completed:
                break
            digest = build_completed_turn_digest(
                turn,
                history_budget - used_tokens,
                seen_conclusions=seen_conclusions,
            )
            if digest is None:
                break
            digest_tokens = _estimate_turn_tokens(digest)
            if used_tokens + digest_tokens > history_budget:
                break
            selected_turns.insert(0, digest)
            used_tokens += digest_tokens
            self._last_selection_digest_count += 1
            selected_completed = True
            conclusion = _final_assistant_content(digest)
            if conclusion:
                seen_conclusions.add(_conclusion_signature(conclusion))
            # 摘要通常很小；若仍有预算，可以继续保留更老的完整轮次。
            continue

        selected = [
            message
            for turn in selected_turns
            for message in turn
        ]
        return selected, used_tokens


def _normalize_message(
    message: dict[str, Any] | str,
) -> dict[str, Any]:
    """把当前消息规范成可发送的 dict，不修改调用方对象。"""
    if isinstance(message, str):
        return {"role": "user", "content": message}
    if not isinstance(message, dict):
        raise TypeError("current_message 必须是 dict 或 str")
    normalized = deepcopy(message)
    if not normalized.get("role"):
        normalized["role"] = "user"
    if "content" not in normalized:
        normalized["content"] = ""
    return normalized


DIGEST_HEADER = "[已完成历史轮次摘要]"


def _estimate_turn_tokens(turn: list[dict[str, Any]]) -> int:
    """估算一个完整用户轮次的 Token。"""
    return sum(estimate_message_tokens(message) for message in turn)


def _final_assistant_content(
    turn: list[dict[str, Any]],
) -> str | None:
    """返回一个已完成轮次最后一条纯文本 Assistant 结论。

    只有 ``role=assistant``、没有 tool_calls 且内容非空的消息才算“最终
    回答”。工具调用中间消息（即使带 content）不会被误当成结论。
    """
    for message in reversed(turn):
        if not isinstance(message, dict):
            continue
        if message.get("role") != "assistant":
            continue
        if message.get("tool_calls"):
            continue
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            return content
    return None


def _conclusion_signature(content: str) -> str:
    """把结论归一化为去重签名，不用于展示。"""
    return " ".join(content.split())


def _turn_tool_names(turn: list[dict[str, Any]]) -> list[str]:
    """按出现顺序收集轮次中使用过的工具名（去重）。"""
    names: list[str] = []
    seen: set[str] = set()
    for message in turn:
        if not isinstance(message, dict):
            continue
        for call in message.get("tool_calls") or []:
            name = _tool_call_name(call)
            if name and name not in seen:
                seen.add(name)
                names.append(name)
    return names


def truncate_text_to_token_budget(
    text: str,
    max_tokens: int,
    *,
    marker: str | None = None,
) -> str:
    """把文本确定性截断到 Token 预算内，并显式标记已截断。

    Args:
        text: 原始文本。
        max_tokens: 截断后允许的最大估算 Token（包含标记本身）。
        marker: 自定义截断标记；None 时使用通用中文/English 标记。

    Returns:
        若原文已满足预算返回原文；否则返回“前缀 + 截断标记”。不会抛异常，
        也不会尝试调用模型做摘要。
    """
    if max_tokens <= 0:
        return ""
    if not isinstance(text, str):
        text = str(text)
    original_tokens = estimate_tokens(text)
    if original_tokens <= max_tokens:
        return text

    marker_text = marker or (
        f"\n\n[... 内容已截断 truncated: 原始约 {original_tokens} tokens ...]"
    )
    if estimate_tokens(marker_text) >= max_tokens:
        marker_text = "...[truncated]..."
    if estimate_tokens(marker_text) >= max_tokens:
        return "…"

    # 二分查找可保留的最长前缀；estimate_tokens 对前缀长度单调不减。
    low = 0
    high = len(text)
    while low < high:
        mid = (low + high + 1) // 2
        candidate = text[:mid] + marker_text
        if estimate_tokens(candidate) <= max_tokens:
            low = mid
        else:
            high = mid - 1
    return text[:low] + marker_text


def build_completed_turn_digest(
    turn: list[dict[str, Any]],
    token_budget: int,
    *,
    seen_conclusions: set[str] | None = None,
) -> list[dict[str, Any]] | None:
    """为一个放不下的已完成轮次构造确定性摘要消息。

    优先保留最终 Assistant 结论（完整保留，空间不足时再按预算截断），
    同时保留用户请求和工具名。输出结构固定为 user + assistant 两条消息，
    不含任何 tool_calls，因此不会破坏 Tool Calling 协议。

    Returns:
        ``[user_digest, assistant_conclusion]``；该轮没有最终 Assistant
        结论、预算过小或结论已经出现过时返回 None。
    """
    if token_budget <= 0 or not turn:
        return None
    conclusion = _final_assistant_content(turn)
    if conclusion is None:
        return None
    signature = _conclusion_signature(conclusion)
    if seen_conclusions is not None and signature in seen_conclusions:
        return None

    user_content = ""
    if isinstance(turn[0], dict):
        raw_content = turn[0].get("content")
        if isinstance(raw_content, str):
            user_content = raw_content
    header = (
        f"{DIGEST_HEADER}\n用户请求："
        f"{truncate_text_to_token_budget(user_content, 80)}"
    )
    tool_names = _turn_tool_names(turn)
    if tool_names:
        header += "\n使用工具：" + ", ".join(tool_names[:6])

    header_message: dict[str, Any] = {"role": "user", "content": header}
    answer_base = estimate_message_tokens({"role": "assistant", "content": ""})
    remaining = (
        token_budget
        - estimate_message_tokens(header_message)
        - answer_base
    )
    if remaining <= 0:
        # 退化到最小 header，仍然优先保留结论。
        header_message = {"role": "user", "content": DIGEST_HEADER}
        remaining = (
            token_budget
            - estimate_message_tokens(header_message)
            - answer_base
        )
    if remaining <= 0:
        return None

    answer_text = truncate_text_to_token_budget(
        conclusion,
        remaining,
        marker="\n\n[... 历史结论已按预算截断 truncated ...]",
    )
    answer_message: dict[str, Any] = {
        "role": "assistant",
        "content": answer_text,
    }
    total = estimate_message_tokens(header_message) + estimate_message_tokens(
        answer_message
    )
    if total > token_budget:
        excess = total - token_budget
        reduced_budget = remaining - excess
        if reduced_budget <= 0:
            return None
        answer_message["content"] = truncate_text_to_token_budget(
            conclusion,
            reduced_budget,
            marker="[...truncated...]",
        )
        total = estimate_message_tokens(
            header_message
        ) + estimate_message_tokens(answer_message)
        if total > token_budget:
            return None
    return [header_message, answer_message]


def _role_counts(messages: Iterable[dict[str, Any]]) -> dict[str, int]:
    """统计消息角色分布，供 CLI 和测试观察。"""
    counter: Counter[str] = Counter()
    for message in messages:
        if isinstance(message, dict):
            counter[str(message.get("role", "unknown"))] += 1
        else:
            counter["unknown"] += 1
    return dict(counter)


def _split_history_into_turns(
    history: list[dict[str, Any]],
) -> list[list[dict[str, Any]]]:
    """按 user 消息切分历史；user 之前的前导片段单独成组（通常非法）。"""
    turns: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    for message in history:
        if not isinstance(message, dict):
            if current:
                current.append(message)  # type: ignore[arg-type]
            else:
                current = [message]  # type: ignore[list-item]
            continue
        if message.get("role") == "user":
            if current:
                turns.append(current)
            current = [message]
        elif current:
            current.append(message)
        else:
            # 历史开头不是 user：保留为独立片段，后续校验会判为非法并丢弃。
            current = [message]
    if current:
        turns.append(current)
    return turns


def _tool_call_id(call: Any) -> str | None:
    """兼容简化格式和 OpenAI function 格式读取 tool_call id。"""
    if not isinstance(call, dict):
        return None
    call_id = call.get("id")
    if isinstance(call_id, str) and call_id:
        return call_id
    return None


def _tool_call_name(call: Any) -> str | None:
    """兼容简化格式和 OpenAI function 格式读取 tool_call name。"""
    if not isinstance(call, dict):
        return None
    name = call.get("name")
    if isinstance(name, str) and name:
        return name
    function = call.get("function")
    if isinstance(function, dict):
        function_name = function.get("name")
        if isinstance(function_name, str) and function_name:
            return function_name
    return None


def _is_valid_turn(turn: list[dict[str, Any]]) -> bool:
    """校验一个完整用户轮次内的消息协议。

    合法性要求：

    - 轮次必须以 user 消息开始；
    - 每个 assistant tool_calls 的每个 id 都必须有唯一、紧随其后的 role=tool
      结果；
    - tool 结果必须能对应到本轮的某个 tool_call；
    - 不允许出现孤立的 tool 消息、缺失结果的 tool_calls 或重复结果。
    """
    if not turn or not isinstance(turn[0], dict):
        return False
    if turn[0].get("role") != "user":
        return False

    index = 1
    while index < len(turn):
        message = turn[index]
        if not isinstance(message, dict):
            return False
        role = message.get("role")

        if role == "user":
            # 按 user 边界切分后不应出现第二个 user。
            return False

        if role == "tool":
            # 不在 assistant tool_calls 之后出现的 tool 消息是孤立结果。
            return False

        if role != "assistant":
            return False

        calls = message.get("tool_calls") or []
        if not isinstance(calls, list):
            return False
        if not calls:
            index += 1
            continue

        declared_ids: list[str] = []
        for call in calls:
            call_id = _tool_call_id(call)
            if call_id is None or _tool_call_name(call) is None:
                return False
            if call_id in declared_ids:
                return False
            declared_ids.append(call_id)

        result_ids: list[str] = []
        result_index = index + 1
        while (
            result_index < len(turn)
            and isinstance(turn[result_index], dict)
            and turn[result_index].get("role") == "tool"
        ):
            result = turn[result_index]
            result_id = result.get("tool_call_id")
            if not isinstance(result_id, str) or not result_id:
                return False
            if result_id not in declared_ids or result_id in result_ids:
                return False
            result_ids.append(result_id)
            result_index += 1

        if set(result_ids) != set(declared_ids):
            return False
        index = result_index

    return True


__all__ = [
    "AGENTS_MD_FILENAME",
    "BuiltContext",
    "ContextBudgetExceeded",
    "ContextBuilder",
    "ContextConfig",
    "ContextError",
    "ContextStats",
    "DIGEST_HEADER",
    "ProviderRequestSnapshot",
    "build_completed_turn_digest",
    "estimate_message_tokens",
    "estimate_messages_tokens",
    "estimate_tokens",
    "estimate_tool_definitions_tokens",
    "truncate_text_to_token_budget",
]
