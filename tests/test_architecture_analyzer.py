"""Phase 4.3：LLM 架构分析器、Prompt 预算与缓存测试。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mini_harness.architecture import (
    ArchitectureAnalysisError,
    ArchitectureAnalyzer,
    ArchitectureCache,
    ArchitectureService,
)
from mini_harness.explorer import build_code_graph, scan_project
from mini_harness.providers.base import LLMResponse
from mini_harness.providers.mock import MockProvider


def _project(tmp_path: Path) -> tuple[Path, object, object]:
    root = tmp_path / "project"
    root.mkdir()
    (root / "pkg").mkdir()
    (root / "pkg" / "__init__.py").write_text('"""Package."""\n', encoding="utf-8")
    (root / "pkg" / "service.py").write_text(
        'class Service:\n'
        '    """A service."""\n'
        "\n"
        "    def run(self) -> None:\n"
        "        pass\n"
        "\n"
        "\n"
        "def make_service() -> Service:\n"
        "    return Service()\n",
        encoding="utf-8",
    )
    (root / "main.py").write_text(
        "from pkg.service import make_service\n"
        "\n"
        "def main() -> None:\n"
        "    make_service()\n",
        encoding="utf-8",
    )
    index = scan_project(root)
    return root, index, build_code_graph(index)


def _payload(symbol_id: str, *, fake: bool = False) -> dict:
    return {
        "defaultViewId": "overview",
        "views": {
            "overview": {
                "id": "overview",
                "title": "项目运行架构",
                "subtitle": "测试",
                "nodes": [
                    {
                        "id": "entry",
                        "kind": "stage",
                        "label": "入口",
                        "description": "入口阶段",
                    },
                    {
                        "id": "service",
                        "kind": "component",
                        "label": "Service",
                        "description": "核心服务",
                        "sourceRefs": [
                            {"symbolId": "invented-symbol" if fake else symbol_id}
                        ],
                        "detailView": "service-detail",
                    },
                ],
                "edges": [
                    {
                        "id": "entry-service",
                        "source": "entry",
                        "target": "service",
                        "kind": "flow",
                        "confidence": "inferred",
                    }
                ],
            },
            "service-detail": {
                "id": "service-detail",
                "title": "Service 内部流程",
                "nodes": [
                    {
                        "id": "run",
                        "kind": "stage",
                        "label": "run",
                        "description": "运行服务",
                    }
                ],
                "edges": [],
            },
        },
    }


@pytest.mark.asyncio
async def test_analyzer_builds_validated_graph(tmp_path: Path) -> None:
    root, index, graph = _project(tmp_path)
    service = next(node for node in graph.nodes.values() if node.kind == "class")
    provider = MockProvider(
        [
            LLMResponse(
                content=json.dumps(_payload(service.id), ensure_ascii=False),
                usage={"prompt_tokens": 120, "completion_tokens": 45, "total_tokens": 165},
            )
        ]
    )
    analyzer = ArchitectureAnalyzer(provider)

    result = await analyzer.analyze(index, graph)

    assert result.graph.mock is False
    assert result.graph.project_name == root.name
    assert result.graph.workspace_root == str(root)
    assert list(result.graph.views_by_id) == ["overview", "service-detail"]
    ref = result.graph.views_by_id["overview"].nodes[1].source_refs[0]
    assert ref.verified is True
    assert ref.symbol_id == service.id
    assert result.prompt_tokens == 120
    assert result.completion_tokens == 45
    assert result.attempts == 1
    assert provider.call_count == 1
    assert provider.received_messages[0][0]["role"] == "system"
    assert "只返回一个合法 JSON 对象" in provider.received_messages[0][0]["content"]


@pytest.mark.asyncio
async def test_analyzer_repairs_invalid_source_reference(tmp_path: Path) -> None:
    _, index, graph = _project(tmp_path)
    service = next(node for node in graph.nodes.values() if node.kind == "class")
    provider = MockProvider(
        [
            LLMResponse(
                content=json.dumps(_payload(service.id, fake=True), ensure_ascii=False)
            ),
            LLMResponse(content=json.dumps(_payload(service.id), ensure_ascii=False)),
        ]
    )
    analyzer = ArchitectureAnalyzer(provider, max_repair_attempts=1)

    result = await analyzer.analyze(index, graph)

    assert result.attempts == 2
    assert provider.call_count == 2
    assert result.graph.views_by_id["overview"].nodes[1].source_refs[0].verified


@pytest.mark.asyncio
async def test_analyzer_fails_after_invalid_output(tmp_path: Path) -> None:
    _, index, graph = _project(tmp_path)
    provider = MockProvider([LLMResponse(content="not json")])
    analyzer = ArchitectureAnalyzer(provider, max_repair_attempts=0)

    with pytest.raises(ArchitectureAnalysisError):
        await analyzer.analyze(index, graph)


@pytest.mark.asyncio
async def test_service_reuses_cache(tmp_path: Path) -> None:
    _, index, graph = _project(tmp_path)
    service = next(node for node in graph.nodes.values() if node.kind == "class")
    provider = MockProvider(
        [LLMResponse(content=json.dumps(_payload(service.id), ensure_ascii=False))]
    )
    cache = ArchitectureCache(tmp_path / "architecture.json")
    service_layer = ArchitectureService(
        index,
        graph,
        analyzer=ArchitectureAnalyzer(provider),
        cache=cache,
    )

    first = await service_layer.generate()
    second = await service_layer.generate()

    assert first.cached is False
    assert second.cached is True
    assert provider.call_count == 1
    assert service_layer.status()["available"] is True


def test_prompt_budget_trims_large_source_excerpts(tmp_path: Path) -> None:
    from dataclasses import replace

    from mini_harness.architecture import (
        ArchitectureInputBuilder,
        ArchitectureSourceExcerpt,
        build_architecture_prompt,
    )

    _, index, graph = _project(tmp_path)
    data = ArchitectureInputBuilder(index, graph).build()
    symbol = next(node for node in graph.nodes.values() if node.kind == "class")
    excerpts = [
        ArchitectureSourceExcerpt(
            symbol_id=symbol.id,
            qualified_name=symbol.qualified_name,
            file_path=symbol.file_path,
            start_line=symbol.start_line,
            end_line=symbol.end_line,
            text="源码片段 " * 5000,
        )
        for _ in range(20)
    ]
    data = replace(data, source_excerpts=excerpts)

    prompt = build_architecture_prompt(data, max_input_tokens=6000)

    assert prompt.trimmed is True
    assert prompt.estimated_input_tokens <= 6000
    assert len(prompt.payload["sourceExcerpts"]) < 20


def test_analyzer_disables_thinking_for_openai_compatible_provider() -> None:
    from mini_harness.providers import OpenAICompatibleProvider

    provider = OpenAICompatibleProvider(
        api_key="test",
        base_url="https://maas.qianwenaiapi.com/compatible-mode/v1",
        model="test-model",
    )
    ArchitectureAnalyzer(provider)

    assert provider.request_thinking_disabled is True
    payload = provider._build_payload([{"role": "user", "content": "hi"}], None)
    assert payload["enable_thinking"] is False
