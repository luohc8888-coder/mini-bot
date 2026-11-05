"""请求内摘要与持久化摘要共享的 transcript 预算和逐条渲染。"""

from __future__ import annotations

from typing import Any
from .messages import _content_text, _tool_call_name
from .tokens import _json_text, _truncate_head_tail, estimate_tokens, truncate_text_to_token_budget


def transcript_token_limit(
    summary_system_prompt: str,
    budget: int,
) -> int:
    """摘要 transcript 的共享 Token 上限。"""
    prompt_overhead = estimate_tokens(summary_system_prompt) + 128
    effective_budget = max(int(budget), 0)
    return max(
        64,
        min(
            max(128, int(effective_budget * 0.55)),
            max(64, effective_budget - prompt_overhead),
        ),
    )


def render_transcript_entry(
    message: Any,
    *,
    assistant_cap: int,
    call_index: dict[str, Any],
) -> str:
    """摘要器与持久化前缀选择共享的逐条渲染，确保预算与覆盖范围一致。"""
    if not isinstance(message, dict):
        return (
            "[unknown]\n"
            + truncate_text_to_token_budget(str(message), 200)
        )
    role = str(message.get("role", "unknown"))
    if role == "tool":
        call_id = str(message.get("tool_call_id") or "")
        name = str(message.get("name") or "")
        if not name and call_id in call_index:
            name = call_index[call_id].name
        content = truncate_text_to_token_budget(_content_text(message), 300)
        return f"[tool:{name or 'unknown'}]\n{content}"
    if role == "assistant":
        content = _truncate_head_tail(
            _content_text(message),
            assistant_cap,
            "\n\n[... assistant 中间内容已截断 truncated ...]\n\n",
        )
        lines = ["[assistant]", content]
        tool_calls = message.get("tool_calls") or []
        if tool_calls:
            rendered_calls = []
            for call in tool_calls:
                name = _tool_call_name(call) or "unknown"
                arguments = call.get("arguments")
                arguments_text = _json_text(arguments)
                if len(arguments_text) > 300:
                    arguments_text = arguments_text[:300] + "…"
                rendered_calls.append(f"{name}({arguments_text})")
            lines.append(
                "[assistant tool_calls] " + "; ".join(rendered_calls)
            )
        return "\n".join(lines)
    content = truncate_text_to_token_budget(
        _content_text(message),
        500 if role == "user" else 300,
    )
    return f"[{role}]\n{content}"


def render_prior_summary(summary: str) -> str:
    return "[Earlier compacted summary — preserve these facts]\n" + summary
