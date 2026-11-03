"""Workspace 解析与 workspace_id 计算。

Workspace 是 Agent 的工作根目录。V1 只负责确定并固定路径，不实现
Nanobot 的持久 workspace-id 标记、路径迁移或复制检测。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path


class WorkspaceError(ValueError):
    """Workspace 路径不可用或文件访问越界时抛出的异常。"""


class WorkspacePathError(WorkspaceError):
    """请求的文件路径不在 Workspace 根目录内。"""


def workspace_id(path: str | Path) -> str:
    """根据规范化绝对路径计算稳定的 16 位 workspace_id。

    Args:
        path: Workspace 路径；会先执行 ``expanduser().resolve()``。

    Returns:
        SHA-256 十六进制摘要的前 16 个字符。

    Notes:
        V1 的已知限制：Workspace 被重命名或移动后会得到新的 ID，原 Session
        不会自动迁移。Nanobot 的持久 workspace-id 标记不在本版本实现。
    """
    normalized = Path(path).expanduser().resolve()
    return hashlib.sha256(str(normalized).encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True, slots=True)
class Workspace:
    """一个已固定的 Workspace。

    Args:
        path: 规范化后的绝对目录路径。
        id: 稳定的 workspace_id。
    """

    path: Path
    id: str

    def resolve_path(self, value: str | Path) -> Path:
        """把相对路径解析到 Workspace 内，并拒绝越界路径。

        该方法同时处理 ``../``、绝对路径和符号链接：先拼接（绝对路径会
        直接替换根目录），再 ``resolve()`` 消除 ``..`` 和符号链接，最后
        检查结果是否仍在 Workspace 内。

        注意：这不是完整的文件系统安全沙箱。路径检查与后续打开文件之间
        仍可能存在并发替换符号链接的竞态，V1 明确将其列为后续安全强化项。
        """
        raw = Path(value).expanduser()
        candidate = (
            raw.resolve()
            if raw.is_absolute()
            else (self.path / raw).resolve()
        )
        try:
            candidate.relative_to(self.path)
        except ValueError as exc:
            raise WorkspacePathError(
                f"路径不在 Workspace 内：{value!r}（Workspace: {self.path}）"
            ) from exc
        return candidate


class WorkspaceResolver:
    """确定并固定当前进程使用的 Workspace。

    Args:
        cwd: 默认工作目录；测试可以注入临时目录，默认使用
            ``Path.cwd()``。

    Notes:
        Workspace 在 ``resolve()`` 调用时固定，不会因为后续工具执行中
        改变进程工作目录而悄悄切换。
    """

    def __init__(self, cwd: Path | None = None) -> None:
        self._cwd = Path(cwd or Path.cwd()).expanduser().resolve()

    @property
    def cwd(self) -> Path:
        """返回创建 Resolver 时固定的默认工作目录。"""
        return self._cwd

    def resolve(self, requested: str | Path | Workspace | None = None) -> Workspace:
        """解析显式路径或默认当前工作目录。

        Args:
            requested: 用户提供的 ``--workspace``；为 None 时使用 cwd。

        Returns:
            已规范化的 Workspace。

        Raises:
            WorkspaceError: 路径不存在、不是目录或无法解析。
        """
        if isinstance(requested, Workspace):
            return requested
        raw = Path(requested).expanduser() if requested is not None else self._cwd
        if not raw.is_absolute():
            raw = self._cwd / raw
        try:
            path = raw.resolve()
        except OSError as exc:
            raise WorkspaceError(f"无法解析 Workspace 路径：{raw}") from exc
        if not path.exists():
            raise WorkspaceError(f"Workspace 不存在：{path}")
        if not path.is_dir():
            raise WorkspaceError(f"Workspace 不是目录：{path}")
        return Workspace(path=path, id=workspace_id(path))


__all__ = [
    "Workspace",
    "WorkspaceError",
    "WorkspacePathError",
    "WorkspaceResolver",
    "workspace_id",
]
