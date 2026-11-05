"""CLI `/compact` 与 `/dream` 命令测试（V2.2）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from mini_harness.agent import PersistentCompactionResult
from mini_harness.cli import Console, InteractiveCLI, ProfileStore, ProviderProfile
from mini_harness.cli.main import parse_args
from mini_harness.memory.dream import DreamResult
from mini_harness.session import SessionManager


class ScriptedConsole(Console):
    """按预置答案模拟终端输入并收集输出。"""

    def __init__(self, answers: list[str]) -> None:
        self.answers = iter(answers)
        self.output: list[str] = []

    def write(self, text: str = "") -> None:
        self.output.append(text)

    async def read(self, prompt: str = "") -> str:
        try:
            return next(self.answers)
        except StopIteration as exc:
            raise EOFError from exc

    @property
    def text(self) -> str:
        return "\n".join(self.output)


class FakeCompactor:
    """记录 compact 调用的 SessionCompactor 替身。"""

    def __init__(
        self,
        result: PersistentCompactionResult | None = None,
        *,
        provider: object | None = None,
    ) -> None:
        self.provider = provider
        self.result = result or PersistentCompactionResult(
            ok=True,
            status="compacted",
            reason="manual",
            summarized_messages=4,
            summary="摘要",
            boundary=4,
            checkpoint_id="cp-1",
            archive_cursor=1,
        )
        self.calls: list[str] = []

    async def compact(self, *, reason: str = "manual") -> PersistentCompactionResult:
        self.calls.append(reason)
        return self.result


class FakeDream:
    """记录 run 调用的 DreamManager 替身。"""

    def __init__(
        self,
        result: DreamResult | None = None,
        *,
        provider: object | None = None,
    ) -> None:
        self.provider = provider
        self.result = result or DreamResult(
            processed_entries=2,
            added=1,
            updated=1,
            removed=0,
            skipped=0,
            cursor=2,
            memory_updated=True,
            llm_called=True,
        )
        self.calls = 0

    async def run(self) -> DreamResult:
        self.calls += 1
        return self.result


def test_cli_memory_flags_parse() -> None:
    """新增的 Idle/Dream CLI 参数可配置，0 表示关闭空闲压缩。"""
    args = parse_args(
        [
            "--idle-compact-after-minutes",
            "0",
            "--dream-interval-hours",
            "3",
            "--no-dream",
        ]
    )
    assert args.idle_compact_after_minutes == 0
    assert args.dream_interval_hours == 3
    assert args.no_dream is True


def make_cli(
    tmp_path: Path,
    console: ScriptedConsole,
    *,
    compactor: FakeCompactor,
    dream: FakeDream,
) -> InteractiveCLI:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    store = ProfileStore(tmp_path / "config.json")
    store.upsert(
        ProviderProfile(name="本地 Mock", provider="mock", model="mock-add-demo"),
        set_default=True,
    )
    manager = SessionManager(workspace, data_root=tmp_path / "data")
    return InteractiveCLI(
        console=console,
        profile_store=store,
        session_manager=manager,
        session_compactor=compactor,
        dream_manager=dream,
        maintenance_enabled=False,
    )


@pytest.mark.asyncio
async def test_cli_compact_command_reports_success(tmp_path: Path) -> None:
    """`/compact` 调用统一压缩器并输出成功信息。"""
    console = ScriptedConsole(["/compact", "/exit"])
    compactor = FakeCompactor()
    dream = FakeDream()
    cli = make_cli(tmp_path, console, compactor=compactor, dream=dream)

    await cli.run()

    assert compactor.calls == ["manual"]
    assert "压缩成功：已归档 4 条历史" in console.text
    assert "checkpoint cp-1" in console.text
    assert "Memory Archive cursor=1" in console.text


@pytest.mark.asyncio
async def test_cli_compact_command_reports_nothing_and_failure(
    tmp_path: Path,
) -> None:
    """`/compact` 分别展示无需压缩与失败原因。"""
    console = ScriptedConsole(["/compact", "/exit"])
    compactor = FakeCompactor(
        PersistentCompactionResult(
            ok=True, status="nothing_to_compact", reason="manual"
        )
    )
    cli = make_cli(tmp_path, console, compactor=compactor, dream=FakeDream())
    await cli.run()
    assert "没有新的未归档历史，无需压缩" in console.text

    console = ScriptedConsole(["/compact", "/exit"])
    compactor = FakeCompactor(
        PersistentCompactionResult(
            ok=False, status="failed", reason="manual", error="摘要服务不可用"
        )
    )
    cli = make_cli(tmp_path, console, compactor=compactor, dream=FakeDream())
    await cli.run()
    assert "压缩失败：摘要服务不可用" in console.text


@pytest.mark.asyncio
async def test_cli_dream_command_reports_counts(tmp_path: Path) -> None:
    """`/dream` 输出处理条数、更新统计与 cursor。"""
    console = ScriptedConsole(["/dream", "/exit"])
    dream = FakeDream()
    cli = make_cli(tmp_path, console, compactor=FakeCompactor(), dream=dream)
    await cli.run()

    assert dream.calls == 1
    assert "Dream 完成：处理 2 条归档摘要" in console.text
    assert "新增 1 条、更新 1 条、删除 0 条、跳过 0 条" in console.text
    assert "cursor=2" in console.text


@pytest.mark.asyncio
async def test_cli_dream_command_reports_no_records_and_busy(
    tmp_path: Path,
) -> None:
    """`/dream` 无记录明确说明未调用模型；busy 时提示稍后再试。"""
    console = ScriptedConsole(["/dream", "/exit"])
    dream = FakeDream(DreamResult(processed_entries=0, llm_called=False))
    cli = make_cli(tmp_path, console, compactor=FakeCompactor(), dream=dream)
    await cli.run()
    assert "没有待处理的归档摘要" in console.text

    console = ScriptedConsole(["/dream", "/exit"])
    dream = FakeDream(DreamResult(busy=True, error="Dream 正在运行"))
    cli = make_cli(tmp_path, console, compactor=FakeCompactor(), dream=dream)
    await cli.run()
    assert "Dream 正在运行，请稍后再试" in console.text


@pytest.mark.asyncio
async def test_cli_maintenance_overrides_disable_start(tmp_path: Path) -> None:
    """--idle-compact-after-minutes 0 / --no-dream 必须在启动维护任务前生效。"""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    console = ScriptedConsole(["/exit"])
    cli = InteractiveCLI(
        console=console,
        session_manager=SessionManager(workspace, data_root=tmp_path / "data"),
        idle_compact_after_minutes=0,
        dream_enabled=False,
    )
    cli._ensure_memory_runtime(None)
    cli._start_maintenance()
    try:
        assert cli._idle_scheduler is None
        assert cli._dream_scheduler is None
    finally:
        await cli._stop_maintenance()
