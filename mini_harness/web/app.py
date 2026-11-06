"""FastAPI 应用工厂与 Code Explorer HTTP API。

正式运行时由 ``mini-bot web`` 启动单个 Uvicorn 服务；FastAPI 同时提供：

- ``/api/*``：绑定当前 Workspace 的只读索引与 CodeGraph API；
- ``/``：本地构建的 React/Vite 静态前端。
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from mini_harness.explorer import (
    CodeGraph,
    ProjectIndex,
    build_code_graph,
    scan_project,
)
from mini_harness.workspace import Workspace, WorkspaceResolver

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_FRONTEND_DIR = _REPO_ROOT / "frontend" / "dist"
_PACKAGED_FRONTEND_DIR = Path(__file__).resolve().parent / "static"


class FrontendNotFoundError(RuntimeError):
    """前端构建产物不存在。"""


def _resolve_frontend_dir(frontend_dir: Path | None) -> Path | None:
    """定位本地静态前端目录。

    优先级：显式参数 > ``MINI_HARNESS_WEB_DIST`` > 仓库 ``frontend/dist``
    > 包内 ``mini_harness/web/static``。
    """
    if frontend_dir is not None:
        path = Path(frontend_dir).expanduser().resolve()
        if not (path / "index.html").is_file():
            raise FrontendNotFoundError(
                f"前端目录缺少 index.html：{path}"
            )
        return path

    env_path = os.environ.get("MINI_HARNESS_WEB_DIST")
    candidates = [
        Path(env_path).expanduser() if env_path else None,
        _DEFAULT_FRONTEND_DIR,
        _PACKAGED_FRONTEND_DIR,
    ]
    for candidate in candidates:
        if candidate is None:
            continue
        path = candidate.resolve()
        if (path / "index.html").is_file():
            return path
    return None


def _project_payload(
    workspace: Workspace,
    index: ProjectIndex,
    graph: CodeGraph,
) -> dict[str, object]:
    """构造前端首屏需要的项目元信息。"""
    return {
        "name": workspace.path.name,
        "root": str(workspace.path),
        "workspaceId": workspace.id,
        "description": f"{workspace.path.name} 的本地静态代码结构",
        "version": "V3.0",
        "generatedAt": index.generated_at,
        "stats": index.stats,
        "graphStats": graph.stats,
    }


def _node_payload(graph: CodeGraph, node_id: str) -> dict[str, object]:
    """返回单个节点及其直接关系和子节点。"""
    node = graph.get_node(node_id)
    if node is None:
        raise HTTPException(status_code=404, detail=f"节点不存在：{node_id}")
    return {
        "node": node.to_dict(),
        "children": [child.to_dict() for child in graph.get_children(node_id)],
        "callers": [item.to_dict() for item in graph.get_callers(node_id)],
        "callees": [item.to_dict() for item in graph.get_callees(node_id)],
        "incoming": [
            edge.to_dict() for edge in graph.get_incoming(node_id)
        ],
        "outgoing": [
            edge.to_dict() for edge in graph.get_outgoing(node_id)
        ],
    }


def create_app(
    workspace: Workspace | str | Path | None = None,
    *,
    frontend_dir: Path | None = None,
) -> FastAPI:
    """创建绑定当前 Workspace 的 FastAPI 应用。

    Args:
        workspace: 固定 Workspace；省略时使用当前工作目录。
        frontend_dir: 可选的前端构建目录，默认为仓库 ``frontend/dist``。

    Returns:
        FastAPI 应用实例。索引与 CodeGraph 在应用创建时构建。
    """
    resolved = (
        workspace
        if isinstance(workspace, Workspace)
        else WorkspaceResolver().resolve(workspace)
    )
    index = scan_project(resolved)
    graph = build_code_graph(index)
    frontend = _resolve_frontend_dir(frontend_dir)

    app = FastAPI(
        title="Mini Harness Code Explorer",
        description="只读的本地项目结构与符号关系浏览服务",
        version="3.0.0",
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
        redoc_url=None,
    )
    app.state.workspace = resolved
    app.state.index = index
    app.state.graph = graph
    app.state.frontend_dir = frontend

    @app.get("/api/health")
    def health() -> dict[str, object]:
        """服务与 Workspace 健康检查。"""
        return {
            "status": "ok",
            "workspace": str(resolved.path),
            "workspaceId": resolved.id,
        }

    @app.get("/api/project")
    def project() -> dict[str, object]:
        """返回当前 Workspace 与索引概览。"""
        return _project_payload(resolved, index, graph)

    @app.get("/api/graph")
    def project_graph(
        include_unresolved: bool = Query(
            default=False,
            description="是否返回无法静态解析的引用明细",
        ),
    ) -> dict[str, object]:
        """返回完整 CodeGraph，供 React Flow 构建可下钻视图。"""
        data = graph.to_dict()
        if not include_unresolved:
            data.pop("unresolved", None)
        return data

    @app.get("/api/codegraph")
    def codegraph(
        include_unresolved: bool = Query(
            default=False,
            description="是否返回无法静态解析的引用明细",
        ),
    ) -> dict[str, object]:
        """``/api/graph`` 的语义别名，方便按产品名称调用。"""
        return project_graph(include_unresolved)

    @app.get("/api/node")
    def node_by_query(node_id: str = Query(...)) -> dict[str, object]:
        """按节点 ID 查询节点及其直接关系。"""
        return _node_payload(graph, node_id)

    @app.get("/api/nodes/{node_id:path}")
    def node_by_path(node_id: str) -> dict[str, object]:
        """按路径中的节点 ID 查询节点及其直接关系。"""
        return _node_payload(graph, node_id)

    @app.get("/api/children")
    def children(parent_id: str | None = Query(default=None)) -> list[dict]:
        """返回某个父节点的直接子节点；省略父节点时返回根节点。"""
        if parent_id is None:
            return [
                node.to_dict()
                for node in graph.nodes.values()
                if node.parent_id is None
            ]
        if graph.get_node(parent_id) is None:
            raise HTTPException(
                status_code=404,
                detail=f"节点不存在：{parent_id}",
            )
        return [node.to_dict() for node in graph.get_children(parent_id)]

    if frontend is not None:
        app.mount(
            "/",
            StaticFiles(directory=str(frontend), html=True),
            name="frontend",
        )
    else:

        @app.get("/", response_class=HTMLResponse)
        def missing_frontend() -> HTMLResponse:
            """在当前目录没有前端构建产物时返回明确错误。"""
            return HTMLResponse(
                "<h1>Code Explorer 前端尚未构建</h1>"
                "<p>请先运行 <code>cd frontend && npm install && npm run build</code>，"
                "然后重新启动 <code>mini-bot web</code>。</p>",
                status_code=503,
            )

    return app


__all__ = [
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "FrontendNotFoundError",
    "create_app",
]
