"""Workspace 接口的兼容导出。

实现位于 :mod:`mini_harness.workspace`，这里保留 session 子包下的稳定
导入路径。
"""

from mini_harness.workspace import (
    Workspace,
    WorkspaceError,
    WorkspacePathError,
    WorkspaceResolver,
    workspace_id,
)

__all__ = [
    "Workspace",
    "WorkspaceError",
    "WorkspacePathError",
    "WorkspaceResolver",
    "workspace_id",
]
