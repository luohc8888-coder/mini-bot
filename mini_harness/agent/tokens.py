"""消息、工具 Schema 的 Token 估算与确定性文本截断。"""

from __future__ import annotations

import json
import math
from typing import Any, Iterable


MESSAGE_OVERHEAD_TOKENS = 4
TOOL_CALL_OVERHEAD_TOKENS = 8
TOOL_DEFINITION_OVERHEAD_TOKENS = 8


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


def _truncate_head_tail(text: str, budget_tokens: int, marker: str) -> str:
    """保留内容头部与尾部，中间插入截断标记。"""
    if estimate_tokens(text) <= budget_tokens:
        return text
    if budget_tokens <= 0:
        return ""
    marker_tokens = estimate_tokens(marker)
    if marker_tokens >= budget_tokens:
        # 保留带元数据的截断标记；此处宁可让标记本身占用几个 Token，
        # 也不能退化成通用提示而丢失 tool/path/pattern 信息。
        return marker

    low = 0
    high = len(text)
    while low < high:
        mid = (low + high + 1) // 2
        head_chars = int(mid * 0.6)
        tail_chars = mid - head_chars
        candidate = text[:head_chars] + marker
        if tail_chars > 0:
            candidate += text[-tail_chars:]
        if estimate_tokens(candidate) <= budget_tokens:
            low = mid
        else:
            high = mid - 1

    head_chars = int(low * 0.6)
    tail_chars = low - head_chars
    candidate = text[:head_chars] + marker
    if tail_chars > 0:
        candidate += text[-tail_chars:]
    return candidate
