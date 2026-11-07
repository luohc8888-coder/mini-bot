"""源码工作台 AI 对话接口测试。"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from mini_harness.architecture import ArchitectureCache
from mini_harness.explorer import build_code_graph, scan_project
from mini_harness.providers.base import LLMResponse
from mini_harness.providers.mock import MockProvider
from mini_harness.web import create_app


def _project(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "project"
    root.mkdir()
    (root / "main.py").write_text(
        "def main() -> None:\n"
        "    \"\"\"程序入口。\"\"\"\n"
        "    print('hello')\n",
        encoding="utf-8",
    )
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    (frontend / "index.html").write_text("<h1>Test</h1>", encoding="utf-8")
    return root, frontend


def _main_symbol_id(root: Path) -> str:
    graph = build_code_graph(scan_project(root))
    symbol = next(node for node in graph.nodes.values() if node.kind == "function")
    return symbol.id


def test_chat_uses_bounded_source_context(tmp_path: Path) -> None:
    root, frontend = _project(tmp_path)
    provider = MockProvider([LLMResponse(content="main 是程序入口，负责打印 hello。")])
    app = create_app(
        root,
        frontend_dir=frontend,
        architecture_provider=provider,
        architecture_cache=ArchitectureCache(tmp_path / "cache.json"),
    )

    with TestClient(app) as client:
        response = client.post(
            "/api/chat",
            json={
                "message": "这个函数做什么？",
                "symbolId": _main_symbol_id(root),
                "nodeLabel": "Main",
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["message"] == "main 是程序入口，负责打印 hello。"
    assert payload["provider"]
    assert provider.call_count == 1
    system_prompt = provider.received_messages[0][0]["content"]
    user_message = provider.received_messages[0][1]["content"]
    assert "当前架构节点：Main" in system_prompt
    assert "def main() -> None:" in system_prompt
    assert "print('hello')" in system_prompt
    assert user_message == "这个函数做什么？"


def test_chat_without_provider_reports_not_configured(tmp_path: Path) -> None:
    root, frontend = _project(tmp_path)
    app = create_app(
        root,
        frontend_dir=frontend,
        architecture_provider_error="没有默认 Provider",
        architecture_cache=ArchitectureCache(tmp_path / "cache.json"),
    )

    with TestClient(app) as client:
        response = client.post("/api/chat", json={"message": "解释一下"})

    assert response.status_code == 503
    assert "没有默认 Provider" in response.json()["detail"]


def test_chat_rejects_empty_message(tmp_path: Path) -> None:
    root, frontend = _project(tmp_path)
    provider = MockProvider([LLMResponse(content="不应调用")])
    app = create_app(
        root,
        frontend_dir=frontend,
        architecture_provider=provider,
        architecture_cache=ArchitectureCache(tmp_path / "cache.json"),
    )

    with TestClient(app) as client:
        response = client.post("/api/chat", json={"message": ""})

    assert response.status_code == 422
    assert provider.call_count == 0
