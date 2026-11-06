"""Phase 4.3：FastAPI 架构生成接口测试。"""

from __future__ import annotations

import json
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
        "def main() -> None:\n    pass\n",
        encoding="utf-8",
    )
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    (frontend / "index.html").write_text("<h1>Test</h1>", encoding="utf-8")
    return root, frontend


def _architecture_payload(root: Path) -> dict:
    index = scan_project(root)
    graph = build_code_graph(index)
    symbol = next(node for node in graph.nodes.values() if node.kind == "function")
    return {
        "defaultViewId": "overview",
        "views": {
            "overview": {
                "id": "overview",
                "title": "运行架构",
                "nodes": [
                    {
                        "id": "main",
                        "kind": "component",
                        "label": "Main",
                        "description": "入口",
                        "sourceRefs": [{"symbolId": symbol.id}],
                    }
                ],
                "edges": [],
            }
        },
    }


def test_architecture_api_generates_and_serves_cache(tmp_path: Path) -> None:
    root, frontend = _project(tmp_path)
    payload = _architecture_payload(root)
    provider = MockProvider(
        [LLMResponse(content=json.dumps(payload, ensure_ascii=False))]
    )
    app = create_app(
        root,
        frontend_dir=frontend,
        architecture_provider=provider,
        architecture_cache=ArchitectureCache(tmp_path / "cache.json"),
    )

    with TestClient(app) as client:
        status = client.get("/api/architecture/status")
        assert status.status_code == 200
        assert status.json()["configured"] is True
        assert status.json()["available"] is False

        missing = client.get("/api/architecture")
        assert missing.status_code == 404

        generated = client.post("/api/architecture/analyze")
        assert generated.status_code == 200
        body = generated.json()
        assert body["graph"]["mock"] is False
        assert body["graph"]["views"]["overview"]["nodes"][0]["sourceRefs"][0]["verified"] is True
        assert body["cached"] is False

        cached = client.get("/api/architecture")
        assert cached.status_code == 200
        assert cached.json()["mock"] is False

        again = client.post("/api/architecture/analyze")
        assert again.status_code == 200
        assert again.json()["cached"] is True


def test_architecture_api_without_provider_reports_not_configured(tmp_path: Path) -> None:
    root, frontend = _project(tmp_path)
    app = create_app(
        root,
        frontend_dir=frontend,
        architecture_provider_error="没有默认 Provider",
        architecture_cache=ArchitectureCache(tmp_path / "cache.json"),
    )

    with TestClient(app) as client:
        status = client.get("/api/architecture/status")
        assert status.status_code == 200
        assert status.json()["configured"] is False
        response = client.post("/api/architecture/analyze")
        assert response.status_code == 503
        assert "没有默认 Provider" in response.json()["detail"]
