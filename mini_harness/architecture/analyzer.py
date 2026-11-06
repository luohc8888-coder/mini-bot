"""Phase 4.3：LLM 驱动的语义架构分析器。

调用链保持三层职责：

``ArchitectureInputBuilder``
    确定性地把 CodeGraph 压缩成有界输入；
``ArchitectureAnalyzer``
    构造受预算约束的 Prompt，调用 Provider，解析并校验 LLM 输出；
``ArchitectureService``（见 ``service.py``）
    复用本地缓存，避免同一份源码反复付费分析。

LLM 不直接接触完整项目，也不负责渲染。它返回结构化 ArchitectureGraph，
所有 sourceRefs 都经 SymbolIndex 校验；无法静态确认的关系由 Prompt 要求
标记为 inferred。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mini_harness.agent.tokens import estimate_tokens
from mini_harness.explorer import CodeGraph, ProjectIndex
from mini_harness.providers import LLMProvider, LLMResponse, ProviderError

from .input import ArchitectureAnalysisInput, ArchitectureInputBuilder
from .models import (
    ARCHITECTURE_SCHEMA_VERSION,
    ArchitectureGraph,
)
from .prompt import (
    DEFAULT_ARCHITECTURE_CONTEXT_TOKENS,
    DEFAULT_ARCHITECTURE_OUTPUT_TOKENS,
    DEFAULT_ARCHITECTURE_SAFETY_MARGIN_TOKENS,
    ArchitecturePrompt,
    ArchitecturePromptError,
    build_architecture_prompt,
)
from .source_refs import ArchitectureGraphValidator, ArchitectureValidationReport


class ArchitectureAnalysisError(RuntimeError):
    """LLM 输出无法解析或未通过 ArchitectureGraph/SymbolIndex 校验。"""

    def __init__(
        self,
        message: str,
        *,
        details: list[str] | None = None,
        raw_content: str | None = None,
    ) -> None:
        super().__init__(message)
        self.details = list(details or [])
        self.raw_content = raw_content


class ArchitectureNotConfiguredError(RuntimeError):
    """当前 Web 进程没有可用的 LLM Provider。"""


@dataclass(frozen=True, slots=True)
class ArchitectureAnalysisResult:
    """一次架构分析的成功结果及可观测统计。"""

    graph: ArchitectureGraph
    cached: bool = False
    provider: str = ""
    model: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    attempts: int = 1
    prompt_trimmed: bool = False
    warnings: tuple[str, ...] = ()
    duration_ms: int = 0
    prompt_stats: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "graph": self.graph.to_dict(),
            "cached": self.cached,
            "provider": self.provider,
            "model": self.model,
            "promptTokens": self.prompt_tokens,
            "completionTokens": self.completion_tokens,
            "totalTokens": self.total_tokens,
            "attempts": self.attempts,
            "promptTrimmed": self.prompt_trimmed,
            "warnings": list(self.warnings),
            "durationMs": self.duration_ms,
            "promptStats": dict(self.prompt_stats),
        }


class ArchitectureAnalyzer:
    """通过 LLMProvider 生成经过 SymbolIndex 校验的 ArchitectureGraph。

    Args:
        provider: 已创建的 LLMProvider；真正请求发生在 ``analyze`` 中。
        max_input_tokens: Prompt 输入预算；省略时根据 Provider 的上下文
            窗口和最大输出额度计算。
        max_output_tokens: 单次架构生成输出额度；省略时使用架构分析推荐的
            12288 token，并在 Provider 上限不足时提升 provider 上限。
        provider_name / model_name: 返回给 API 的展示信息，可省略。
        max_repair_attempts: JSON/校验失败后最多追加的修复请求次数，默认 1。
    """

    def __init__(
        self,
        provider: LLMProvider,
        *,
        max_input_tokens: int | None = None,
        max_output_tokens: int | None = None,
        provider_name: str = "",
        model_name: str = "",
        max_repair_attempts: int = 1,
    ) -> None:
        self.provider = provider
        # 架构分析需要稳定 JSON：对支持该开关的 OpenAI-compatible Provider
        # 显式关闭 thinking，避免推理 token 把输出额度全部耗尽。
        if hasattr(provider, "request_thinking_disabled"):
            provider.request_thinking_disabled = True
        self.provider_name = provider_name or str(
            getattr(provider, "provider_name", type(provider).__name__)
        )
        self.model_name = model_name or str(getattr(provider, "model", ""))
        requested_output_tokens = (
            max_output_tokens
            if max_output_tokens is not None
            else DEFAULT_ARCHITECTURE_OUTPUT_TOKENS
        )
        self.max_output_tokens = requested_output_tokens
        if self.max_output_tokens <= 0:
            raise ValueError("max_output_tokens 必须是正整数")
        # 结构化 ArchitectureGraph 需要足够的输出空间；当调用方没有显式
        # 指定额度时，允许 Provider 使用架构分析推荐的 12288 token。
        if max_output_tokens is None:
            provider_max = int(
                getattr(provider, "max_output_tokens", requested_output_tokens)
            )
            if provider_max < requested_output_tokens:
                provider.max_output_tokens = requested_output_tokens
        self.max_repair_attempts = max(0, int(max_repair_attempts))
        self.max_input_tokens = max_input_tokens or _resolve_input_budget(
            provider,
            output_tokens=self.max_output_tokens,
        )
        if self.max_input_tokens <= 0:
            raise ValueError("max_input_tokens 必须是正整数")
        self.last_prompt: ArchitecturePrompt | None = None
        self.last_messages: list[dict[str, Any]] = []

    async def analyze(
        self,
        index: ProjectIndex | CodeGraph,
        graph: CodeGraph,
    ) -> ArchitectureAnalysisResult:
        """生成一次架构图；失败抛出 ArchitectureAnalysisError。

        该方法不读写磁盘缓存，负责纯分析。缓存与并发去重由
        ``ArchitectureService`` 负责，便于单独测试 Prompt/Parser。
        """
        started = time.perf_counter()
        analysis_input = ArchitectureInputBuilder(index, graph).build()
        try:
            prompt = build_architecture_prompt(
                analysis_input,
                max_input_tokens=self.max_input_tokens,
            )
        except ArchitecturePromptError as exc:
            raise ArchitectureAnalysisError(str(exc)) from exc
        self.last_prompt = prompt
        base_messages: list[dict[str, Any]] = [
            {"role": "system", "content": prompt.system_prompt},
            {"role": "user", "content": prompt.user_prompt},
        ]
        messages = list(base_messages)
        usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        last_error: Exception | None = None
        last_content = ""
        for attempt in range(self.max_repair_attempts + 1):
            response = await self._call_provider(messages)
            self.last_messages = list(messages)
            content = response.content or ""
            if not content.strip() and response.reasoning_content:
                # 个别 OpenAI-compatible 端点只在 reasoning_content 中回传
                # 文本；尝试从其中提取 JSON，避免无意义地再请求一次。
                content = response.reasoning_content
            last_content = content
            _accumulate_usage(usage, response, prompt)
            try:
                payload = _extract_json_object(content)
                candidate = _normalise_generated_graph(
                    payload,
                    graph,
                    generated_at=datetime.now(timezone.utc).isoformat(),
                )
                report = ArchitectureGraphValidator(graph).check(candidate)
                if not report.ok:
                    raise ArchitectureAnalysisError(
                        "ArchitectureGraph 未通过结构或源码引用校验",
                        details=[issue.message for issue in report.errors],
                        raw_content=content,
                    )
                verified_graph = report.graph
                break
            except ArchitectureAnalysisError as exc:
                last_error = exc
                if attempt >= self.max_repair_attempts:
                    raise _final_analysis_error(exc, attempts=attempt + 1) from exc
                messages = _build_repair_messages(base_messages, content, exc)
            except (ValueError, TypeError, KeyError) as exc:
                wrapped = ArchitectureAnalysisError(
                    f"LLM 返回的架构 JSON 无法解析：{exc}",
                    raw_content=content,
                )
                last_error = wrapped
                if attempt >= self.max_repair_attempts:
                    raise _final_analysis_error(wrapped, attempts=attempt + 1) from exc
                messages = _build_repair_messages(base_messages, content, wrapped)
        else:  # pragma: no cover - 循环必然 break 或 raise
            raise ArchitectureAnalysisError("架构分析未产生结果") from last_error

        elapsed_ms = int((time.perf_counter() - started) * 1000)
        return ArchitectureAnalysisResult(
            graph=verified_graph,
            cached=False,
            provider=self.provider_name,
            model=self.model_name,
            prompt_tokens=usage["prompt_tokens"],
            completion_tokens=usage["completion_tokens"],
            total_tokens=usage["total_tokens"],
            attempts=attempt + 1,
            prompt_trimmed=prompt.trimmed,
            warnings=tuple(
                issue.message
                for issue in report.issues
                if issue.severity == "warning"
            ),
            duration_ms=elapsed_ms,
            prompt_stats={
                **prompt.budget,
                "payloadBytes": len(
                    json.dumps(prompt.payload, ensure_ascii=False)
                ),
            },
        )

    async def _call_provider(self, messages: list[dict[str, Any]]) -> LLMResponse:
        try:
            with self.provider.request_output_budget(self.max_output_tokens):
                return await self.provider.chat(messages, tools=None)
        except ProviderError:
            raise
        except Exception as exc:
            # 将 httpx / URL / 连接层异常统一为 ProviderError，API 映射为 502。
            raise ProviderError(
                f"{self.provider_name} 架构分析请求失败：{exc}"
            ) from exc


def _resolve_input_budget(
    provider: LLMProvider,
    *,
    output_tokens: int,
) -> int:
    context_tokens = int(
        getattr(provider, "context_window_tokens", 0)
        or DEFAULT_ARCHITECTURE_CONTEXT_TOKENS
    )
    return max(
        1024,
        context_tokens
        - max(0, output_tokens)
        - DEFAULT_ARCHITECTURE_SAFETY_MARGIN_TOKENS,
    )


def _accumulate_usage(
    usage: dict[str, int],
    response: LLMResponse,
    prompt: ArchitecturePrompt,
) -> None:
    raw = response.usage if isinstance(response.usage, dict) else {}
    prompt_tokens = _as_int(raw.get("prompt_tokens"), prompt.estimated_input_tokens)
    completion_tokens = _as_int(
        raw.get("completion_tokens"),
        estimate_tokens(response.content or ""),
    )
    usage["prompt_tokens"] += prompt_tokens
    usage["completion_tokens"] += completion_tokens
    usage["total_tokens"] += _as_int(
        raw.get("total_tokens"),
        prompt_tokens + completion_tokens,
    )


def _as_int(value: Any, default: int) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return max(0, int(default))


def _extract_json_object(content: str) -> dict[str, Any]:
    """从可能带 Markdown/说明文字的模型响应中提取第一个 JSON 对象。"""
    text = (content or "").strip()
    if not text:
        raise ArchitectureAnalysisError("LLM 返回空内容")
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            value, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise ArchitectureAnalysisError("LLM 响应中没有可解析的 JSON 对象")


def _normalise_generated_graph(
    payload: dict[str, Any],
    code_graph: CodeGraph,
    *,
    generated_at: str,
) -> ArchitectureGraph:
    """把 LLM 返回的局部图包装成由服务端控制元数据的完整图。"""
    raw = dict(payload)
    if "views" not in raw:
        # 容忍模型偶尔把结果包在 graph / architecture 字段里。
        nested = raw.get("graph") or raw.get("architecture")
        if isinstance(nested, dict):
            raw = dict(nested)
    views = raw.get("views")
    if isinstance(views, dict):
        normalised_views: dict[str, Any] = {}
        for key, item in views.items():
            if not isinstance(item, dict):
                continue
            view = dict(item)
            view.setdefault("id", str(key))
            normalised_views[str(key)] = view
        raw["views"] = normalised_views
    project_name = Path(code_graph.workspace_root).name or "project"
    project = raw.get("project") if isinstance(raw.get("project"), dict) else {}
    raw["project"] = {
        "name": project_name,
        "root": code_graph.workspace_root,
        "description": str(
            project.get("description")
            or f"{project_name} 的 AI 语义运行架构"
        ),
    }
    raw["schemaVersion"] = ARCHITECTURE_SCHEMA_VERSION
    raw["version"] = str(ARCHITECTURE_SCHEMA_VERSION)
    raw["mock"] = False
    raw["workspaceRoot"] = code_graph.workspace_root
    raw["workspaceId"] = code_graph.workspace_id
    raw["generatedAt"] = generated_at
    if not raw.get("defaultViewId"):
        raw["defaultViewId"] = "overview"
    graph = ArchitectureGraph.from_dict(raw)
    _prune_broken_links(graph)
    return graph


def _prune_broken_links(graph: ArchitectureGraph) -> None:
    """移除指向不存在的 detail view 的链接，保留节点本身。"""
    view_ids = set(graph.views_by_id)
    views = []
    for view in graph.views:
        nodes = []
        for node in view.nodes:
            nested = [
                replace(item, detail_view=None)
                if item.detail_view and item.detail_view not in view_ids
                else item
                for item in node.nested
            ]
            detail_view = (
                node.detail_view if node.detail_view in view_ids else None
            )
            nodes.append(
                replace(node, detail_view=detail_view, nested=nested)
            )
        views.append(replace(view, nodes=nodes))
    object.__setattr__(graph, "views", views)


def _build_repair_messages(
    base_messages: list[dict[str, Any]],
    previous_content: str,
    error: Exception,
) -> list[dict[str, Any]]:
    error_text = str(error)
    if len(error_text) > 2400:
        error_text = error_text[:2400] + "…"
    assistant_content = previous_content[:30000]
    return [
        *base_messages,
        {"role": "assistant", "content": assistant_content},
        {
            "role": "user",
            "content": (
                "上一次响应未通过 ArchitectureGraph 结构或 SymbolIndex 源码校验。\n"
                f"错误：{error_text}\n"
                "请修复后只返回完整的 JSON 对象；不要返回 Markdown、解释或代码围栏。"
                "所有 sourceRefs.symbolId 必须是 ArchitectureAnalysisInput 中真实存在的符号。"
            ),
        },
    ]


def _final_analysis_error(
    error: ArchitectureAnalysisError,
    *,
    attempts: int,
) -> ArchitectureAnalysisError:
    message = str(error)
    if attempts > 1:
        message = f"{message}（已尝试 {attempts} 次）"
    return ArchitectureAnalysisError(
        message,
        details=list(error.details),
        raw_content=error.raw_content,
    )


__all__ = [
    "ArchitectureAnalysisError",
    "ArchitectureAnalysisResult",
    "ArchitectureAnalyzer",
    "ArchitectureNotConfiguredError",
]
