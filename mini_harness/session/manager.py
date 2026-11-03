"""Session 数据与 JSON 持久化。

V1 固定使用单 Channel、单默认 Session。Session 只保存消息历史，不保存
Provider、API Key 等运行配置；Session 文件位于 Harness 数据目录，而不是
Workspace 内。
"""

from __future__ import annotations

import json
import os
import tempfile
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from mini_harness.workspace import (
    Workspace,
    WorkspaceResolver,
    workspace_id,
)


DEFAULT_SESSION_KEY = "cli:main"
SCHEMA_VERSION = 1


class SessionError(RuntimeError):
    """Session 操作失败的基类。"""


class SessionStorageError(SessionError):
    """Session 文件无法读取、写入或包含无效结构。"""


class SessionInUseError(SessionError):
    """同一 Workspace 已有另一个 CLI 实例持有 Session 锁。"""


class SessionCorruptError(SessionStorageError):
    """Session 文件不是合法 JSON 或结构不符合 V1 格式。"""


class SessionWorkspaceMismatch(SessionStorageError):
    """Session 文件记录的 Workspace 与当前 Workspace 不一致。"""


class SessionKeyMismatch(SessionStorageError):
    """Session 文件记录的 Session Key 与当前请求不一致。"""


class SessionSerializationError(SessionStorageError):
    """消息历史无法安全地序列化为 JSON。"""


@dataclass(slots=True)
class Session:
    """一段持久化的聊天历史。

    Args:
        key: Session 标识；V1 固定为 ``cli:main``。
        messages: 跨轮次保存的消息列表，字段与 V0 内部消息协议一致。

    Notes:
        ``messages`` 使用 ``default_factory=list``，避免多个 Session 实例
        共享同一个可变列表。
    """

    key: str
    messages: list[dict[str, Any]] = field(default_factory=list)


class SessionManager:
    """加载、保存和清空单个 Workspace 的默认 Session。

    Args:
        workspace: Workspace 路径或已解析的 Workspace 对象。
        data_root: Harness 数据根目录；默认 ``~/.mini-harness``，也可由
            ``MINI_HARNESS_HOME`` 指定。
        session_key: 固定 Session Key，默认 ``cli:main``。

    上下游:
        ``WorkspaceResolver`` 解析 Workspace；``AgentLoop`` 和 CLI 读取
        Session；本类只负责状态持久化，不感知 Provider 或工具。
    """

    def __init__(
        self,
        workspace: Path | Workspace,
        data_root: Path | None = None,
        *,
        session_key: str = DEFAULT_SESSION_KEY,
    ) -> None:
        if isinstance(workspace, Workspace):
            self.workspace = workspace.path.expanduser().resolve()
            self.workspace_id = workspace.id
        else:
            self.workspace = Path(workspace).expanduser().resolve()
            self.workspace_id = workspace_id(self.workspace)
        if data_root is None:
            configured = os.getenv("MINI_HARNESS_HOME")
            data_root = (
                Path(configured).expanduser()
                if configured
                else Path.home() / ".mini-harness"
            )
        self.data_root = Path(data_root).expanduser().resolve()
        self.session_key = session_key
        self.path = (
            self.data_root
            / "sessions"
            / self.workspace_id
            / "session.json"
        )

    @property
    def workspace_path(self) -> Path:
        """当前 Session 绑定的规范化 Workspace 路径。"""
        return self.workspace

    @property
    def session_path(self) -> Path:
        """Session JSON 文件路径（``path`` 的别名）。"""
        return self.path

    @property
    def session_file(self) -> Path:
        """Session JSON 文件路径的兼容别名。"""
        return self.path

    def load(self, session_key: str | None = None) -> Session:
        """读取 Session；文件不存在时返回空 Session。

        Args:
            session_key: 可选的覆盖 Key；默认使用管理器固定 Key。

        Returns:
            Session 的独立副本，调用方修改不会影响已加载文件。

        Raises:
            SessionStorageError: 文件无法读取或不是合法 V1 格式。
            SessionCorruptError: JSON 损坏或字段结构错误。
            SessionWorkspaceMismatch: 文件记录的 Workspace 不匹配。
            SessionKeyMismatch: 文件记录的 Session Key 不匹配。
        """
        key = session_key or self.session_key
        if not self.path.exists():
            return Session(key=key, messages=[])

        try:
            raw = self.path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise SessionStorageError(
                f"无法读取 Session 文件：{self.path}"
            ) from exc

        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise SessionCorruptError(
                f"Session 文件不是合法 JSON，保留原文件不覆盖：{self.path}"
            ) from exc
        if not isinstance(payload, dict):
            raise SessionCorruptError(
                f"Session 文件格式错误（顶层必须是对象）：{self.path}"
            )

        if payload.get("schema_version") != SCHEMA_VERSION:
            raise SessionStorageError(
                f"不支持的 Session schema_version："
                f"{payload.get('schema_version')!r}（支持 {SCHEMA_VERSION}）"
            )

        stored_workspace = payload.get("workspace")
        if not isinstance(stored_workspace, str):
            raise SessionCorruptError(
                f"Session 文件缺少 workspace 字段：{self.path}"
            )
        try:
            stored_path = Path(stored_workspace).expanduser().resolve()
        except OSError as exc:
            raise SessionCorruptError(
                f"Session 文件中的 workspace 无法解析：{stored_workspace!r}"
            ) from exc
        if stored_path != self.workspace:
            raise SessionWorkspaceMismatch(
                f"Session 的 Workspace 不匹配：文件为 {stored_path}，"
                f"当前为 {self.workspace}"
            )

        stored_key = payload.get("session_key")
        if stored_key != key:
            raise SessionKeyMismatch(
                f"Session Key 不匹配：文件为 {stored_key!r}，当前为 {key!r}"
            )

        messages = payload.get("messages")
        if not isinstance(messages, list):
            raise SessionCorruptError(
                f"Session 的 messages 必须是列表：{self.path}"
            )
        self._validate_messages(messages)
        return Session(key=key, messages=deepcopy(messages))

    def save(self, session: Session) -> None:
        """原子保存 Session 到 JSON 文件。

        保存前先检查消息是否可以 JSON 序列化；写入同目录临时文件，执行
        ``flush + fsync`` 后用 ``os.replace`` 替换目标文件。异常时清理
        临时文件，尽量保持旧文件完整。

        Raises:
            SessionSerializationError: 消息包含不可 JSON 序列化内容。
            SessionStorageError: 目录创建或文件写入失败。
        """
        if session.key != self.session_key:
            raise SessionKeyMismatch(
                f"拒绝保存非默认 Session Key：{session.key!r}"
            )
        self._validate_messages(session.messages)

        payload = {
            "schema_version": SCHEMA_VERSION,
            "workspace": str(self.workspace),
            "session_key": session.key,
            "messages": session.messages,
        }
        try:
            serialized = json.dumps(
                payload,
                ensure_ascii=False,
                indent=2,
                allow_nan=False,
            )
        except (TypeError, ValueError) as exc:
            raise SessionSerializationError(
                f"Session 历史无法序列化为 JSON：{exc}"
            ) from exc

        parent = self.path.parent
        try:
            parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise SessionStorageError(
                f"无法创建 Session 目录：{parent}"
            ) from exc

        temp_path: Path | None = None
        try:
            fd, temp_name = tempfile.mkstemp(
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                dir=str(parent),
            )
            temp_path = Path(temp_name)
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(serialized)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temp_path, 0o600)
            os.replace(temp_path, self.path)
            temp_path = None
            self._fsync_directory(parent)
        except OSError as exc:
            raise SessionStorageError(
                f"无法保存 Session 文件：{self.path}"
            ) from exc
        finally:
            if temp_path is not None:
                try:
                    temp_path.unlink(missing_ok=True)
                except OSError:
                    pass

    def clear(self) -> None:
        """清空当前 Session 的历史并持久化空 Session。"""
        self.save(Session(key=self.session_key, messages=[]))

    @contextmanager
    def lock(self) -> Iterator[None]:
        """持有 CLI 生命周期的非阻塞建议锁。

        V1 明确是单进程单写者模型。CLI 启动时用 ``flock`` 对当前
        Workspace 的锁文件加独占锁；同机第二个 CLI 实例会收到明确的
        ``SessionInUseError``，而不是静默并发覆盖。直接调用 SessionManager
        的其他程序不在锁保护范围内，跨进程强一致写入仍不在 V1 范围。

        注释：``flock`` 是建议锁，不是数据库事务；Windows 无 ``fcntl``
        时退化为不锁，并在 README 中明确说明限制。
        """
        lock_path = self.path.parent / "session.lock"
        try:
            lock_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise SessionStorageError(
                f"无法创建 Session 锁目录：{lock_path.parent}"
            ) from exc

        try:
            import fcntl
        except ImportError:
            yield
            return

        handle = None
        try:
            handle = lock_path.open("a+", encoding="utf-8")
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise SessionInUseError(
                    "当前 Workspace 已有另一个 Mini Harness CLI 实例在运行，"
                    "请先退出该实例；V1 不支持同一 Session 的并发写入。"
                ) from exc
            yield
        except OSError as exc:
            if isinstance(exc, FileNotFoundError):
                raise
            raise SessionStorageError(f"无法使用 Session 锁：{exc}") from exc
        finally:
            if handle is not None:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
                handle.close()

    @staticmethod
    def _validate_messages(messages: list[dict[str, Any]]) -> None:
        """校验消息列表的基本结构，不负责解释工具内容。"""
        if not isinstance(messages, list):
            raise SessionCorruptError("Session messages 必须是列表")
        for index, message in enumerate(messages):
            if not isinstance(message, dict):
                raise SessionCorruptError(
                    f"Session 第 {index} 条消息必须是对象"
                )
            role = message.get("role")
            if not isinstance(role, str) or not role:
                raise SessionCorruptError(
                    f"Session 第 {index} 条消息缺少合法 role"
                )

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        """尽量 fsync 目录；部分平台不支持目录 fsync，忽略该错误。"""
        try:
            fd = os.open(path, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(fd)
        except OSError:
            pass
        finally:
            os.close(fd)


__all__ = [
    "DEFAULT_SESSION_KEY",
    "SCHEMA_VERSION",
    "Session",
    "SessionCorruptError",
    "SessionError",
    "SessionInUseError",
    "SessionKeyMismatch",
    "SessionManager",
    "SessionSerializationError",
    "SessionStorageError",
    "SessionWorkspaceMismatch",
    "Workspace",
    "WorkspaceResolver",
    "workspace_id",
]
