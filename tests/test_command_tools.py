"""本机命令边界、输出背压、超时/取消和宿主确认回归。"""

import asyncio
import os
import shlex
import sys
from pathlib import Path

import pytest

from mini_harness.tools import RunCommandTool, Tool, ToolRegistry, ToolResult
from mini_harness.workspace import WorkspaceResolver


def python_command(source: str) -> str:
    return shlex.join([sys.executable, "-c", source])


@pytest.mark.asyncio
async def test_command_combines_streams_and_reports_failure(tmp_path: Path) -> None:
    tool = RunCommandTool(WorkspaceResolver().resolve(tmp_path))
    result = await tool.execute(python_command(
        "import sys; print('out', flush=True); print('err', file=sys.stderr); sys.exit(7)"
    ))
    assert result.is_error
    assert "Exit code: 7" in result.content
    assert "out" in result.content and "err" in result.content


@pytest.mark.asyncio
async def test_command_cwd_and_escape(tmp_path: Path) -> None:
    (tmp_path / "sub").mkdir()
    tool = RunCommandTool(WorkspaceResolver().resolve(tmp_path))
    result = await tool.execute(python_command("import os; print(os.getcwd())"), cwd="sub")
    assert result.success and str(tmp_path / "sub") in result.content
    outside = await tool.execute("echo no", cwd="..")
    assert outside.is_error and "不在 Workspace 内" in outside.content
    (tmp_path / "link").symlink_to(tmp_path.parent, target_is_directory=True)
    assert (await tool.execute("echo no", cwd="link")).is_error
    assert (await tool.execute("echo no", cwd="missing")).is_error


@pytest.mark.asyncio
async def test_output_is_drained_after_limit_without_hanging(tmp_path: Path) -> None:
    tool = RunCommandTool(WorkspaceResolver().resolve(tmp_path), max_output_bytes=100)
    result = await tool.execute(python_command("print('x' * 1000000)"), timeout=5)
    assert result.success
    assert "output truncated" in result.content
    assert len(result.content) < 500


@pytest.mark.asyncio
async def test_dangerous_commands_need_host_approval(tmp_path: Path) -> None:
    target = tmp_path / "keep.txt"
    target.write_text("keep")
    workspace = WorkspaceResolver().resolve(tmp_path)
    denied = await RunCommandTool(workspace).execute("rm keep.txt")
    assert denied.is_error and target.exists()
    calls = []

    async def confirm(command: str, cwd: str) -> bool:
        calls.append((command, cwd))
        return False

    denied = await RunCommandTool(workspace, confirm=confirm).execute("rm keep.txt")
    assert denied.is_error and target.exists() and len(calls) == 1
    allowed = await RunCommandTool(workspace, confirm=lambda *_: True).execute("rm keep.txt")
    assert allowed.success and not target.exists()


@pytest.mark.parametrize("timeout", [0, -1, 121, float("inf"), float("nan")])
@pytest.mark.asyncio
async def test_invalid_command_timeout(tmp_path: Path, timeout: float) -> None:
    tool = RunCommandTool(WorkspaceResolver().resolve(tmp_path))
    assert (await tool.execute("echo no", timeout=timeout)).is_error


@pytest.mark.skipif(os.name != "posix", reason="POSIX process group cleanup")
@pytest.mark.parametrize("cancel", [False, True])
@pytest.mark.asyncio
async def test_timeout_and_cancellation_kill_descendants(tmp_path: Path, cancel: bool) -> None:
    # 后代持有 stdout，且在 1 秒后写文件。超时/取消必须杀掉它，
    # shell 父进程已经退出也不能放过该后代。
    marker = tmp_path / "leaked"
    ready = tmp_path / "ready"
    child = (
        "import pathlib,time; pathlib.Path('ready').write_text('yes'); "
        "time.sleep(1); pathlib.Path('leaked').write_text('bad')"
    )
    parent = (
        "import subprocess,sys; "
        f"subprocess.Popen([sys.executable, '-c', {child!r}])"
    )
    tool = RunCommandTool(WorkspaceResolver().resolve(tmp_path))
    task = asyncio.create_task(tool.execute(python_command(parent), timeout=0.4 if not cancel else 10))
    if cancel:
        for _ in range(100):
            if ready.exists():
                break
            await asyncio.sleep(0.01)
        assert ready.exists(), "child must start before testing cancellation"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        result = await task
        assert result.is_error and "Timed out" in result.content
    await asyncio.sleep(1.1)
    assert not marker.exists(), "descendant survived command cleanup"


class SlowTool(Tool):
    name = "slow"

    async def execute(self) -> ToolResult:
        await asyncio.sleep(10)
        return ToolResult("never")


class VerboseTool(Tool):
    name = "verbose"

    async def execute(self) -> ToolResult:
        return ToolResult.error("HEAD" + "x" * 10000 + "TAIL")


@pytest.mark.asyncio
async def test_registry_timeout_and_output_limit() -> None:
    registry = ToolRegistry(timeout_seconds=0.02, max_output_chars=256)
    registry.register(SlowTool())
    registry.register(VerboseTool())
    timed_out = await registry.execute("slow", {})
    assert timed_out.is_error and "超时" in timed_out.content
    output = await registry.execute("verbose", {})
    assert output.is_error
    assert len(output.content) == 256
    assert output.content.startswith("HEAD") and output.content.endswith("TAIL")


@pytest.mark.asyncio
async def test_registry_validates_command_timeout_bounds(tmp_path: Path) -> None:
    registry = ToolRegistry()
    registry.register(RunCommandTool(WorkspaceResolver().resolve(tmp_path)))
    for value in (0, 121, True, "30"):
        result = await registry.execute("run_command", {"command": "echo no", "timeout": value})
        assert result.is_error and "参数校验失败" in result.content
