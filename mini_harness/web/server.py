"""单进程启动入口：Uvicorn + FastAPI + 本地静态前端。"""

from __future__ import annotations

import socket
from pathlib import Path

import uvicorn

from .app import DEFAULT_HOST, DEFAULT_PORT, create_app
from .provider import load_architecture_provider


def _check_port(host: str, port: int) -> None:
    """在启动 Uvicorn 前给出明确的端口占用错误。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except OSError as exc:
            raise OSError(
                f"端口 {host}:{port} 已被占用，请使用 --port 指定其他端口"
            ) from exc


async def run_web_server(
    workspace: str | Path | None = None,
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    frontend_dir: Path | None = None,
    log_level: str = "info",
    architecture_profile: str | None = None,
) -> None:
    """启动 Mini Harness Code Explorer 的单一 Web 服务。

    正式使用时前端由同一个 FastAPI 进程提供，不需要另外启动 Vite。
    LLM Provider 从 CLI 保存的 profile 解析；缺少配置时仍可浏览代码结构。
    """
    _check_port(host, port)
    selection = load_architecture_provider(architecture_profile)
    if selection.error:
        print(f"架构分析不可用：{selection.error}", flush=True)
    app = create_app(
        workspace,
        frontend_dir=frontend_dir,
        architecture_provider=selection.provider,
        architecture_profile_name=selection.profile_name,
        architecture_provider_error=selection.error,
    )
    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_level=log_level,
    )
    server = uvicorn.Server(config)
    print(
        f"Mini Harness Code Explorer: http://{host}:{port}",
        flush=True,
    )
    print(
        f"Workspace: {app.state.workspace.path}",
        flush=True,
    )
    if selection.provider is not None:
        print(
            f"Architecture Provider: {selection.profile_name} "
            f"({getattr(selection.provider, 'model', '') or 'default model'})",
            flush=True,
        )
    await server.serve()


__all__ = ["run_web_server"]
