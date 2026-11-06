"""Phase 4.3：ArchitectureGraph 分析的 LLM Prompt 与 Token 预算。

Prompt 构建是确定性函数：它只读取 ``ArchitectureAnalysisInput``，把结构化
摘要压缩到模型上下文允许的范围。不会调用 Provider，也不会改写输入对象。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from mini_harness.agent.tokens import estimate_tokens

from .input import ArchitectureAnalysisInput

DEFAULT_ARCHITECTURE_CONTEXT_TOKENS = 32_768
DEFAULT_ARCHITECTURE_OUTPUT_TOKENS = 12_288
DEFAULT_ARCHITECTURE_SAFETY_MARGIN_TOKENS = 1_024

SYSTEM_PROMPT = """你是 Mini Harness 的本地代码架构分析器。你的任务是根据用户提供的结构化项目索引、入口候选、关键符号、静态关系和少量源码窗口，生成可交互的「运行架构」ArchitectureGraph。

## 输入
用户消息会给出一个 ArchitectureAnalysisInput JSON。它来自确定性的 AST / CodeGraph 索引，不是完整项目源码。请把它当作唯一事实来源。

## 输出格式
只返回一个合法 JSON 对象，不要返回 Markdown、代码围栏（```）、Mermaid、解释或前后缀文本。JSON 顶层结构必须是：

{
  "defaultViewId": "overview",
  "views": {
    "overview": {
      "id": "overview",
      "title": "项目运行架构",
      "subtitle": "一句话说明",
      "nodes": [
        {
          "id": "kebab-case-node-id",
          "kind": "component",
          "label": "组件名",
          "description": "职责说明",
          "meta": "可选标签，例如入口、基础设施",
          "detailView": "可选，指向另一个 view id",
          "responsibilities": ["职责 1", "职责 2"],
          "sourceRefs": [{"symbolId": "只能使用输入里真实存在的 symbol id"}],
          "nested": [{"id": "nested-id", "label": "嵌套组件名", "kind": "component", "detailView": "可选"}],
          "confidence": "static",
          "evidence": ["可选，引用输入里的符号或关系 id"]
        }
      ],
      "edges": [
        {
          "id": "edge-id",
          "source": "源节点 id",
          "target": "目标节点 id",
          "kind": "flow",
          "label": "可选",
          "confidence": "static",
          "evidence": ["可选"]
        }
      ]
    }
  }
}

## 硬性规则
1. 只能使用输入中出现过的 symbol id 或 qualifiedName。sourceRefs 优先只写 symbolId；绝对不要编造文件路径、行号或符号。
2. 如果某个组件或关系无法由输入直接证明，confidence 必须为 "inferred"；如果 input 中有直接 import/call/entry 证据，才可以使用 "static"。
3. 边端点必须存在；node id 在同一 view 内唯一；每个 detailView / nested.detailView 必须指向一个实际存在的 view。
4. 每个视图最多 12 个节点，建议 overview 使用 6-10 个逻辑组件或执行阶段；需要展开的复杂组件可以增加 detail view（3-6 个节点）。整个 ArchitectureGraph 总节点数不超过 32，视图数不超过 7。
5. 优先表达执行链和职责边界，不要把每个函数都做成节点。源码细节放在 sourceRefs 中，不要复制源码原文。
6. 节点 label / description / responsibilities 使用简体中文，面向项目维护者。
7. 不要调用工具。不要修改或声称修改任何文件。

## 分析重点
- 从入口候选与关键符号中识别真正的运行主链路；
- 区分入口、核心组件、执行阶段、条件分支和外部/基础设施依赖；
- 使用 import/call 关系推断组件之间的 flow、data、branch、dependency；
- 对 AgentLoop、AgentRunner、Provider、ToolRegistry、Session 等关键组件，可以生成下钻 detail view 和 nested 入口。
"""


class ArchitecturePromptError(ValueError):
    """Prompt 无法装入给定 Token 预算。"""


@dataclass(frozen=True, slots=True)
class ArchitecturePrompt:
    """一次架构分析请求的 system/user prompt 与预算统计。"""

    system_prompt: str
    user_prompt: str
    estimated_input_tokens: int
    payload: dict[str, Any]
    trimmed: bool
    budget: dict[str, int]


def build_architecture_prompt(
    analysis_input: ArchitectureAnalysisInput,
    *,
    max_input_tokens: int,
) -> ArchitecturePrompt:
    """生成受输入 Token 上限约束的架构分析 Prompt。

    Args:
        analysis_input: Phase 4.2 生成的结构化分析输入。
        max_input_tokens: system + user prompt 允许的最大估算 Token。

    Returns:
        ArchitecturePrompt；payload 是压缩后的输入字典，便于测试与调试。

    Raises:
        ArchitecturePromptError: 即使压缩到最小仍超过预算。
    """
    if max_input_tokens <= 0:
        raise ArchitecturePromptError("max_input_tokens 必须为正整数")

    base_payload = _compact_payload(analysis_input)
    payload = _fit_payload(base_payload, max_input_tokens=max_input_tokens)
    system_text = SYSTEM_PROMPT
    user_text = _render_user_prompt(payload)
    estimated = _estimate_prompt_tokens(system_text, user_text)
    if estimated > max_input_tokens:
        raise ArchitecturePromptError(
            f"架构分析 Prompt 约 {estimated} tokens，超过预算 "
            f"{max_input_tokens} tokens"
        )
    return ArchitecturePrompt(
        system_prompt=system_text,
        user_prompt=user_text,
        estimated_input_tokens=estimated,
        payload=payload,
        trimmed=payload != base_payload,
        budget={
            "maxInputTokens": max_input_tokens,
            "estimatedInputTokens": estimated,
        },
    )


def _compact_payload(analysis_input: ArchitectureAnalysisInput) -> dict[str, Any]:
    """把完整分析输入压缩成适合 Prompt 的稳定结构。"""
    return {
        "schemaVersion": analysis_input.schema_version,
        "project": {
            "name": analysis_input.project_name,
            "root": analysis_input.workspace_root,
        },
        "generatedAt": analysis_input.generated_at,
        "graphStats": dict(analysis_input.graph_stats),
        "entryPoints": [
            {
                "symbol": _compact_symbol(entry.symbol, include_doc=False),
                "reasons": list(entry.reasons),
                "score": entry.score,
            }
            for entry in analysis_input.entry_points
        ],
        "modules": [
            {
                "id": symbol.id,
                "qualifiedName": symbol.qualified_name,
                "filePath": symbol.file_path,
                "startLine": symbol.start_line,
                "endLine": symbol.end_line,
            }
            for symbol in analysis_input.modules
        ],
        "keySymbols": [
            _compact_symbol(symbol, include_doc=True)
            for symbol in analysis_input.key_symbols
        ],
        "imports": [
            _compact_relation(relation)
            for relation in analysis_input.imports
        ],
        "calls": [
            _compact_relation(relation)
            for relation in analysis_input.calls
        ],
        "sourceExcerpts": [
            {
                "symbolId": excerpt.symbol_id,
                "qualifiedName": excerpt.qualified_name,
                "filePath": excerpt.file_path,
                "startLine": excerpt.start_line,
                "endLine": excerpt.end_line,
                "text": excerpt.text,
                "truncated": excerpt.truncated,
            }
            for excerpt in analysis_input.source_excerpts
        ],
        "unresolvedSummary": dict(analysis_input.unresolved_summary),
        "limits": dict(analysis_input.limits),
        "warnings": list(analysis_input.warnings),
    }


def _compact_symbol(symbol: Any, *, include_doc: bool) -> dict[str, Any]:
    item: dict[str, Any] = {
        "id": symbol.id,
        "kind": symbol.kind,
        "name": symbol.name,
        "qualifiedName": symbol.qualified_name,
        "filePath": symbol.file_path,
        "startLine": symbol.start_line,
        "endLine": symbol.end_line,
        "signature": symbol.signature,
        "callerCount": symbol.caller_count,
        "calleeCount": symbol.callee_count,
    }
    if include_doc and symbol.docstring:
        item["docstring"] = symbol.docstring[:240]
    return item


def _compact_relation(relation: Any) -> dict[str, Any]:
    item: dict[str, Any] = {
        "id": relation.id,
        "source": relation.source,
        "target": relation.target,
        "kind": relation.kind,
        "confidence": relation.confidence,
        "line": relation.line,
        "occurrences": relation.occurrences,
    }
    if relation.detail:
        item["detail"] = relation.detail[:160]
    return item


def _render_user_prompt(payload: dict[str, Any]) -> str:
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return (
        "请分析下面的 ArchitectureAnalysisInput，并严格按 system 说明返回 JSON：\n"
        "## ArchitectureAnalysisInput\n"
        f"{serialized}"
    )


def _estimate_prompt_tokens(system_prompt: str, user_prompt: str) -> int:
    return estimate_tokens(system_prompt) + estimate_tokens(user_prompt)


def _fit_payload(payload: dict[str, Any], *, max_input_tokens: int) -> dict[str, Any]:
    """按阶段裁剪可选输入，直到 Prompt 能装入预算。"""
    system_text = SYSTEM_PROMPT
    candidates = _trim_stages(payload)
    for candidate in candidates:
        user_text = _render_user_prompt(candidate)
        if _estimate_prompt_tokens(system_text, user_text) <= max_input_tokens:
            return candidate

    # 最后的硬压缩：只保留入口/关键符号的 id 与少量关系，源码片段全删。
    hard = dict(payload)
    hard["sourceExcerpts"] = []
    hard["modules"] = []
    hard["imports"] = payload["imports"][:8]
    hard["calls"] = payload["calls"][:15]
    hard["entryPoints"] = payload["entryPoints"][:4]
    hard["keySymbols"] = [
        {
            "id": item["id"],
            "kind": item["kind"],
            "qualifiedName": item["qualifiedName"],
            "filePath": item["filePath"],
            "startLine": item["startLine"],
            "endLine": item["endLine"],
        }
        for item in payload["keySymbols"][:15]
    ]
    hard["keySymbols"] = hard["keySymbols"][:15]
    return hard


def _trim_stages(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """生成从较完整到最小化的候选输入序列。"""
    stage_limits = [
        (8, 160, 90, 60, 50, 10),
        (5, 100, 50, 40, 35, 8),
        (3, 60, 30, 30, 20, 6),
        (1, 30, 15, 20, 10, 4),
        (0, 15, 8, 10, 0, 3),
    ]
    stages: list[dict[str, Any]] = []
    for excerpts, calls, imports, keys, modules, entries in stage_limits:
        candidate = dict(payload)
        candidate["sourceExcerpts"] = payload["sourceExcerpts"][:excerpts]
        candidate["calls"] = payload["calls"][:calls]
        candidate["imports"] = payload["imports"][:imports]
        candidate["keySymbols"] = payload["keySymbols"][:keys]
        candidate["modules"] = payload["modules"][:modules]
        candidate["entryPoints"] = payload["entryPoints"][:entries]
        stages.append(candidate)
    return stages


__all__ = [
    "DEFAULT_ARCHITECTURE_CONTEXT_TOKENS",
    "DEFAULT_ARCHITECTURE_OUTPUT_TOKENS",
    "DEFAULT_ARCHITECTURE_SAFETY_MARGIN_TOKENS",
    "SYSTEM_PROMPT",
    "ArchitecturePrompt",
    "ArchitecturePromptError",
    "build_architecture_prompt",
]
