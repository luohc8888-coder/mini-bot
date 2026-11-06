"""Phase 4.3：ArchitectureAnalyzer 的缓存与状态门面。"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from mini_harness.explorer import CodeGraph, ProjectIndex

from .analyzer import (
    ArchitectureAnalysisError,
    ArchitectureAnalysisResult,
    ArchitectureAnalyzer,
    ArchitectureNotConfiguredError,
)
from .cache import (
    ArchitectureCache,
    ArchitectureCacheError,
    codegraph_fingerprint,
)


class ArchitectureService:
    """为 Web 层提供「读取缓存 / 触发分析」的统一入口。"""

    def __init__(
        self,
        index: ProjectIndex | CodeGraph,
        graph: CodeGraph,
        *,
        analyzer: ArchitectureAnalyzer | None = None,
        cache: ArchitectureCache | None = None,
        profile_name: str = "",
        provider_error: str = "",
    ) -> None:
        self.index = index
        self.graph = graph
        self.analyzer = analyzer
        self.cache = cache or ArchitectureCache.for_workspace(graph.workspace_id)
        self.profile_name = profile_name
        self.provider_error = provider_error
        self.fingerprint = codegraph_fingerprint(graph)

    @property
    def configured(self) -> bool:
        return self.analyzer is not None

    def cached_result(self) -> ArchitectureAnalysisResult | None:
        """读取与当前 CodeGraph 指纹匹配的缓存结果。"""
        graph = self.cache.load(self.fingerprint)
        if graph is None:
            return None
        provider = self.analyzer.provider_name if self.analyzer else ""
        model = self.analyzer.model_name if self.analyzer else ""
        return ArchitectureAnalysisResult(
            graph=graph,
            cached=True,
            provider=provider,
            model=model,
            prompt_stats={"cachePath": str(self.cache.path)},
        )

    async def generate(self, *, force: bool = False) -> ArchitectureAnalysisResult:
        """返回缓存或调用 LLM 生成架构图。"""
        if not force:
            cached = self.cached_result()
            if cached is not None:
                return cached
        if self.analyzer is None:
            raise ArchitectureNotConfiguredError(
                self.provider_error
                or "尚未配置 LLM Provider；请先运行 mini-bot 保存默认配置，"
                "或使用 --architecture-profile 指定 profile。"
            )

        result = await self.analyzer.analyze(self.index, self.graph)
        try:
            self.cache.save(result.graph, self.fingerprint)
        except ArchitectureCacheError as exc:
            result = replace(
                result,
                warnings=(*result.warnings, f"架构缓存写入失败：{exc}"),
            )
        return result

    def status(self) -> dict[str, Any]:
        """返回前端使用的 Provider / 缓存状态，不包含 API Key。"""
        cached = self.cached_result()
        provider = ""
        model = ""
        if self.analyzer is not None:
            provider = self.analyzer.provider_name
            model = self.analyzer.model_name
        return {
            "configured": self.configured,
            "profile": self.profile_name or None,
            "provider": provider,
            "model": model,
            "available": cached is not None,
            "generatedAt": cached.graph.generated_at if cached else None,
            "cachePath": str(self.cache.path),
            "error": self.provider_error or None,
        }


__all__ = ["ArchitectureService"]
