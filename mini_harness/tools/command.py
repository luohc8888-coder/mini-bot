"""本地命令执行：限制 cwd、时间和输出，并提供宿主确认钩子。

cwd 边界不是 shell 沙箱；命令仍具有当前用户的文件和网络权限。
危险命令检测只覆盖常见写法，不是完整的 shell 静态分析。
"""

from __future__ import annotations

import asyncio
import inspect
import math
import os
import re
import signal
from collections.abc import Awaitable, Callable

from mini_harness.workspace import Workspace, WorkspaceError

from .base import Tool, ToolResult

CommandConfirmation = Callable[[str, str], bool | Awaitable[bool]]
_DANGEROUS = re.compile(
    r"\b(?:rm|rmdir|unlink|sudo|mkfs(?:\.\w+)?|dd|shutdown|reboot)\b"
    r"|\bgit\s+(?:[^;&|\n]*\s)?(?:reset\s+--hard|clean\b|push\b|restore\b)"
    r"|\bgit\s+checkout\s+[^;&|\n]*--\s"
    r"|(?:^|[;|&\n])\s*(?:ba|z|da)?sh\s+-c\b",
    re.IGNORECASE,
)


class RunCommandTool(Tool):
    """运行前台 shell 命令，超时/取消后清理整个 POSIX 进程组。"""

    name = "run_command"
    description = (
        "在 Workspace 内的 cwd 执行本地 shell 命令（测试、git status 等）。"
        "合并 stdout/stderr，返回退出码；默认超时 30 秒，最长 120 秒。"
        "cwd 检查不是 shell 沙箱，常见危险命令需要宿主确认。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "要执行的 shell 命令"},
            "cwd": {"type": "string", "description": "Workspace 内目录，默认 ."},
            "timeout": {"type": "number", "minimum": 0.01, "maximum": 120},
        },
        "required": ["command"],
        "additionalProperties": False,
    }

    def __init__(
        self,
        workspace: Workspace,
        *,
        confirm: CommandConfirmation | None = None,
        max_output_bytes: int = 16_384,
    ) -> None:
        if max_output_bytes < 1:
            raise ValueError("max_output_bytes must be positive")
        self.workspace = workspace
        self.confirm = confirm
        self.max_output_bytes = max_output_bytes

    async def execute(
        self, command: str, cwd: str = ".", timeout: float = 30.0
    ) -> ToolResult:
        if not command.strip():
            return ToolResult.error("Error: command must not be empty")
        if not math.isfinite(timeout) or not 0.01 <= timeout <= 120:
            return ToolResult.error("Error: timeout must be between 0.01 and 120 seconds")
        try:
            directory = self.workspace.resolve_path(cwd)
            if not directory.is_dir():
                return ToolResult.error(f"Error: cwd is not a directory: {cwd}")
        except (WorkspaceError, OSError) as exc:
            return ToolResult.error(f"Error: {exc}")

        if _DANGEROUS.search(command):
            if self.confirm is None:
                return ToolResult.error("Error: command requires host confirmation; no confirmation hook configured")
            approved = self.confirm(command, str(directory))
            if inspect.isawaitable(approved):
                approved = await approved
            if not approved:
                return ToolResult.error("Error: command denied by host")

        process = await asyncio.create_subprocess_shell(
            command,
            cwd=directory,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=(os.name == "posix"),
        )
        output = bytearray()
        total_bytes = 0

        async def drain() -> None:
            nonlocal total_bytes
            assert process.stdout is not None
            while chunk := await process.stdout.read(65_536):
                total_bytes += len(chunk)
                remaining = self.max_output_bytes - len(output)
                if remaining > 0:
                    output.extend(chunk[:remaining])
            await process.wait()

        async def cleanup() -> None:
            try:
                if os.name == "posix":
                    # 即使 shell 已退出，继承 stdout 的后代也必须被清理。
                    os.killpg(process.pid, signal.SIGKILL)
                elif process.returncode is None:
                    process.kill()
            except ProcessLookupError:
                pass
            await process.wait()

        timed_out = False
        try:
            await asyncio.wait_for(drain(), timeout=timeout)
        except TimeoutError:
            timed_out = True
        finally:
            # 外层 Registry 超时或用户取消也会经过这里，不能留下子进程。
            await asyncio.shield(cleanup())

        status = f"Timed out after {timeout:g}s" if timed_out else f"Exit code: {process.returncode}"
        text = output.decode("utf-8", errors="replace")
        if total_bytes > len(output):
            text += f"\n[output truncated: kept {len(output)} of {total_bytes} bytes]"
        return ToolResult(
            content=f"{status}\ncwd: {directory}\n{text}",
            is_error=timed_out or process.returncode != 0,
        )
