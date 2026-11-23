"""源码工作台 AI 对话接口测试。"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from mini_harness.architecture import ArchitectureCache
from mini_harness.explorer import build_code_graph, scan_project
from mini_harness.providers.base import LLMResponse, ToolCallRequest
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


def test_chat_forwards_multi_turn_history(tmp_path: Path) -> None:
    root, frontend = _project(tmp_path)
    provider = MockProvider([LLMResponse(content="结合上一轮，这里会直接调用 print。")])
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
                "message": "它具体调用了什么？",
                "symbolId": _main_symbol_id(root),
                "nodeLabel": "Main",
                "history": [
                    {"role": "user", "content": "这个函数做什么？"},
                    {"role": "assistant", "content": "它是程序入口。"},
                ],
            },
        )

    assert response.status_code == 200
    sent = provider.received_messages[0]
    assert [msg["role"] for msg in sent] == ["system", "user", "assistant", "user"]
    assert sent[1]["content"] == "这个函数做什么？"
    assert sent[2]["content"] == "它是程序入口。"
    assert sent[3]["content"] == "它具体调用了什么？"


def test_chat_stream_runs_readonly_agent_with_tool_and_sse_events(
    tmp_path: Path,
) -> None:
    root, frontend = _project(tmp_path)
    (root / "helper.py").write_text(
        "def greet(name: str) -> str:\n"
        '    """返回问候语。"""\n'
        "    return f'hi {name}'\n",
        encoding="utf-8",
    )
    (root / "main.py").write_text(
        "from helper import greet\n\n"
        "def main() -> str:\n"
        '    """程序入口。"""\n'
        "    return greet('world')\n",
        encoding="utf-8",
    )
    symbol_id = _main_symbol_id(root)

    provider = MockProvider(
        [
            LLMResponse(
                content=None,
                tool_calls=[
                    ToolCallRequest(
                        id="call_graph_1",
                        name="query_code_graph",
                        arguments={"symbol": symbol_id},
                    )
                ],
                finish_reason="tool_calls",
            ),
            LLMResponse(
                content="main 在 L5 调用了 `helper.py:1` 中的 `greet`。",
                finish_reason="stop",
            ),
        ]
    )
    app = create_app(
        root,
        frontend_dir=frontend,
        architecture_provider=provider,
        architecture_cache=ArchitectureCache(tmp_path / "cache.json"),
        index_db_path=tmp_path / "index.db",
    )

    # 安全不变量：Web Agent 工具集必须严格只读
    tool_names = set(app.state.readonly_tools.tool_names)
    assert "query_code_graph" in tool_names
    assert "search_code" in tool_names
    assert "read_file" in tool_names
    assert not (
        tool_names & {"write_file", "edit_file", "apply_patch", "run_command"}
    )

    with TestClient(app) as client:
        with client.stream(
            "POST",
            "/api/chat/stream",
            json={
                "message": "它下游调用了哪个函数？",
                "symbolId": symbol_id,
                "nodeLabel": "Main",
            },
        ) as resp:
            assert resp.status_code == 200
            assert "text/event-stream" in resp.headers["content-type"]
            body = "".join(resp.iter_text())

    assert "event: tool" in body
    assert '"name": "query_code_graph"' in body
    assert "event: delta" in body
    assert "event: done" in body
    assert "helper.py:1" in body
    assert '"toolsUsed": ["query_code_graph"]' in body


def test_symbol_by_path_resolves_enclosing_symbol_and_blocks_escape(
    tmp_path: Path,
) -> None:
    root, frontend = _project(tmp_path)
    app = create_app(
        root,
        frontend_dir=frontend,
        index_db_path=tmp_path / "index.db",
    )
    with TestClient(app) as client:
        ok = client.get(
            "/api/symbol-by-path",
            params={"file_path": "main.py", "line": 2},
        )
        assert ok.status_code == 200
        payload = ok.json()
        assert payload["symbolName"] == "main"
        assert payload["kind"] == "function"
        assert payload["filePath"] == "main.py"

        bad = client.get(
            "/api/symbol-by-path",
            params={"file_path": "../../etc/passwd", "line": 1},
        )
        assert bad.status_code == 400
