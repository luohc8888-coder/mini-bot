"""FastAPI 应用工厂与 Code Explorer HTTP API。

正式运行时由 ``mini-bot web`` 启动单个 Uvicorn 服务；FastAPI 同时提供：

- ``/api/*``：绑定当前 Workspace 的只读索引与 CodeGraph API；
- ``/``：本地构建的 React/Vite 静态前端。
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from mini_harness.architecture import (
    ArchitectureAnalysisError,
    ArchitectureAnalyzer,
    ArchitectureCache,
    ArchitectureNotConfiguredError,
    ArchitectureService,
)
from mini_harness.explorer import (
    CodeGraph,
    ProjectIndex,
    build_code_graph,
    scan_project,
)
from mini_harness.providers import LLMProvider, ProviderError
from mini_harness.workspace import (
    Workspace,
    WorkspacePathError,
    WorkspaceResolver,
)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765

_MAX_SOURCE_LINES = 400
_MAX_SOURCE_BYTES = 512 * 1024

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


def _source_payload(
    workspace: Workspace,
    graph: CodeGraph,
    symbol_id: str,
) -> dict[str, object]:
    """按符号 ID 读取 Workspace 内的只读源码片段。

    只接受 CodeGraph 中真实存在的 symbol_id，不接收任意前端文件路径；
    返回值限制为符号行范围，并限制文件大小与返回行数，避免把整个文件
    或 Workspace 外内容暴露给浏览器。
    """
    symbol = graph.get_node(symbol_id)
    if symbol is None:
        raise HTTPException(status_code=404, detail=f"符号不存在：{symbol_id}")
    try:
        path = workspace.resolve_path(symbol.file_path)
    except WorkspacePathError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not path.is_file():
        raise HTTPException(
            status_code=404,
            detail=f"源码文件不存在：{symbol.file_path}",
        )
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise HTTPException(
            status_code=404,
            detail=f"无法读取源码文件：{symbol.file_path}",
        ) from exc
    if size > _MAX_SOURCE_BYTES:
        raise HTTPException(
            status_code=413,
            detail=(
                f"源码文件过大，无法预览：{symbol.file_path} "
                f"({size} bytes > {_MAX_SOURCE_BYTES} bytes)"
            ),
        )
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(
            status_code=415,
            detail=f"源码文件不是 UTF-8 文本：{symbol.file_path}",
        ) from exc
    except OSError as exc:
        raise HTTPException(
            status_code=404,
            detail=f"无法读取源码文件：{symbol.file_path}",
        ) from exc

    file_lines = text.splitlines()
    total_lines = len(file_lines)
    symbol_start = max(1, min(symbol.start_line, total_lines or 1))
    symbol_end = max(symbol_start, min(symbol.end_line, total_lines or 1))
    if total_lines == 0:
        start_line = 0
        end_line = 0
        returned_lines: list[dict[str, object]] = []
        truncated = False
    else:
        start_line = symbol_start
        end_line = min(symbol_end, start_line + _MAX_SOURCE_LINES - 1)
        truncated = end_line < symbol_end
        returned_lines = [
            {
                "number": line_number,
                "text": file_lines[line_number - 1],
            }
            for line_number in range(start_line, end_line + 1)
        ]

    return {
        "symbolId": symbol.id,
        "symbolName": symbol.name,
        "kind": symbol.kind,
        "qualifiedName": symbol.qualified_name,
        "filePath": symbol.file_path,
        "language": symbol.language,
        "signature": symbol.signature,
        "docstring": symbol.docstring,
        "symbolStartLine": symbol.start_line,
        "symbolEndLine": symbol.end_line,
        "startLine": start_line,
        "endLine": end_line,
        "totalLines": total_lines,
        "truncated": truncated,
        "lines": returned_lines,
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
    architecture_provider: LLMProvider | None = None,
    architecture_analyzer: ArchitectureAnalyzer | None = None,
    architecture_cache: ArchitectureCache | None = None,
    architecture_profile_name: str = "",
    architecture_provider_error: str = "",
) -> FastAPI:
    """创建绑定当前 Workspace 的 FastAPI 应用。

    Args:
        workspace: 固定 Workspace；省略时使用当前工作目录。
        frontend_dir: 可选的前端构建目录，默认为仓库 ``frontend/dist``。
        architecture_provider: 可注入的 LLMProvider；生产环境由
            ``run_web_server`` 从 CLI profile 创建，测试可注入 MockProvider。
        architecture_analyzer: 可选的已构造分析器，优先于 provider。
        architecture_cache: 可选的架构缓存路径/实例。
        architecture_profile_name: 状态接口展示的 profile 名。
        architecture_provider_error: Provider 不可用时的可读原因。

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
    if architecture_analyzer is None and architecture_provider is not None:
        architecture_analyzer = ArchitectureAnalyzer(
            architecture_provider,
            provider_name=str(
                getattr(architecture_provider, "provider_name", "")
                or type(architecture_provider).__name__
            ),
            model_name=str(getattr(architecture_provider, "model", "") or ""),
        )
    architecture_service = ArchitectureService(
        index,
        graph,
        analyzer=architecture_analyzer,
        cache=architecture_cache,
        profile_name=architecture_profile_name,
        provider_error=architecture_provider_error,
    )

    app.state.workspace = resolved
    app.state.index = index
    app.state.graph = graph
    app.state.frontend_dir = frontend
    app.state.architecture_service = architecture_service

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

    @app.get("/api/architecture/status")
    def architecture_status() -> dict[str, object]:
        """返回 LLM Provider 与架构缓存状态（不暴露 API Key）。"""
        return architecture_service.status()

    @app.get("/api/architecture")
    def architecture_graph(
        allow_stale: bool = Query(
            default=False,
            description="缓存指纹已变化时，是否仍返回旧架构图（用户显式选择）",
        ),
    ) -> dict[str, object]:
        """返回当前源码指纹对应的已生成架构图。"""
        cached = architecture_service.cached_result(allow_stale=allow_stale)
        if cached is None:
            raise HTTPException(
                status_code=404,
                detail="运行架构尚未生成；请调用 POST /api/architecture/analyze",
            )
        return cached.graph.to_dict()

    @app.post("/api/architecture/analyze")
    async def analyze_architecture(
        force: bool = Query(
            default=False,
            description="忽略缓存并重新调用 LLM 分析",
        ),
    ) -> dict[str, object]:
        """调用 LLM 生成并校验 ArchitectureGraph。"""
        try:
            result = await architecture_service.generate(force=force)
        except ArchitectureNotConfiguredError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except ArchitectureAnalysisError as exc:
            return JSONResponse(
                status_code=502,
                content={
                    "detail": str(exc),
                    "issues": list(exc.details),
                },
            )
        except ProviderError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return result.to_dict()

    @app.get("/api/node")
    def node_by_query(node_id: str = Query(...)) -> dict[str, object]:
        """按节点 ID 查询节点及其直接关系。"""
        return _node_payload(graph, node_id)

    @app.get("/api/nodes/{node_id:path}")
    def node_by_path(node_id: str) -> dict[str, object]:
        """按路径中的节点 ID 查询节点及其直接关系。"""
        return _node_payload(graph, node_id)

    @app.get("/api/source")
    def source(symbol_id: str = Query(...)) -> dict[str, object]:
        """按符号 ID 返回只读源码片段，用于运行架构的源码追溯。"""
        return _source_payload(resolved, graph, symbol_id)

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
