"""Dream 长期记忆引擎与 DreamScheduler 测试。

覆盖 docs/memory_compaction_design.md §5 的关键契约：

- 无未处理归档时不调用 LLM；
- 成功写 MEMORY.md 后才推进 ``.dream_cursor``；
- Provider 失败、空响应、非法 JSON / 非法结构时 cursor 与记忆不变；
- 重启后从 cursor 恢复，不重复处理；
- ``max_entries`` 截断、规范化去重、update/remove、非法条目跳过、大小上限；
- asyncio.Lock 互斥（busy）与 DreamScheduler 的启动/禁用行为。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from mini_harness.memory.dream import (
    BUSY_ERROR,
    DREAM_SYSTEM_PROMPT,
    DreamConfig,
    DreamManager,
    DreamScheduler,
)
from mini_harness.memory.store import MemoryStore, MemoryStoreError
from mini_harness.providers.base import LLMProvider, LLMResponse


# ---------------------------------------------------------------------------
# 测试辅助
# ---------------------------------------------------------------------------


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """每个测试独立的 Workspace，避免记忆目录互相污染。"""
    path = tmp_path / "workspace"
    path.mkdir()
    return path


@pytest.fixture
def store(workspace: Path) -> MemoryStore:
    return MemoryStore(workspace)


class ScriptedProvider(LLMProvider):
    """按顺序回放预设响应的测试 Provider。

    响应可以是字符串（自动包装成 LLMResponse）、LLMResponse，或一个
    Exception 实例（模拟 Provider 异常）。没有剩余响应时抛出断言错误，
    用于证明某些路径“绝不调用 LLM”。
    """

    def __init__(self, *responses: str | LLMResponse | Exception) -> None:
        self._responses = list(responses)
        self.call_count = 0
        self.received_messages: list[list[dict[str, Any]]] = []
        self.called = asyncio.Event()

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMResponse:
        self.call_count += 1
        self.called.set()
        self.received_messages.append(messages)
        if not self._responses:
            raise AssertionError("不应调用 Provider")
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        if isinstance(item, str):
            return LLMResponse(content=item, finish_reason="stop")
        return item


class BlockingProvider(LLMProvider):
    """第一次 chat 阻塞到 release 被设置，用于验证 Dream 互斥锁。"""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.call_count = 0

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMResponse:
        self.call_count += 1
        self.started.set()
        await self.release.wait()
        return LLMResponse(content="(nothing)")


def seed_history(store: MemoryStore, *contents: str) -> list[int]:
    """向 MemoryStore 追加归档摘要，返回每条记录的 cursor。"""
    return [
        store.append_history(
            content,
            session_key="cli:main",
            timestamp="2026-01-01T00:00:00+08:00",
        )
        for content in contents
    ]


def dream_json(
    *,
    add: list[dict[str, Any]] | None = None,
    update: list[dict[str, Any]] | None = None,
    remove: list[str] | None = None,
) -> str:
    """构造合法的 Dream JSON 响应文本。"""
    return json.dumps(
        {
            "add": add or [],
            "update": update or [],
            "remove": remove or [],
        },
        ensure_ascii=False,
    )


# ---------------------------------------------------------------------------
# 基础流程
# ---------------------------------------------------------------------------


async def test_no_unprocessed_history_does_not_call_llm(
    store: MemoryStore,
) -> None:
    """无新归档时直接返回，绝不调用 LLM。"""
    provider = ScriptedProvider()
    manager = DreamManager(provider, store)

    result = await manager.run()

    assert result.processed_entries == 0
    assert result.llm_called is False
    assert result.cursor == 0
    assert result.error is None
    assert provider.call_count == 0
    assert store.read_memory() == ""


async def test_success_writes_memory_and_advances_cursor(
    store: MemoryStore,
) -> None:
    """成功调用 LLM 后按 section 写入 MEMORY.md 并推进 cursor。"""
    seed_history(store, "用户喜欢简洁回答", "项目使用 pytest")
    provider = ScriptedProvider(
        dream_json(
            add=[
                {"section": "用户偏好", "text": "用户喜欢简洁回答"},
                {"section": "项目决策", "text": "项目使用 pytest"},
            ]
        )
    )
    manager = DreamManager(provider, store)

    result = await manager.run()

    assert result.processed_entries == 2
    assert result.added == 2
    assert result.updated == 0
    assert result.removed == 0
    assert result.skipped == 0
    assert result.cursor == 2
    assert result.memory_updated is True
    assert result.llm_called is True
    assert result.error is None
    assert store.get_dream_cursor() == 2

    memory = store.read_memory()
    assert "## 用户偏好" in memory
    assert "- 用户喜欢简洁回答" in memory
    assert "## 项目决策" in memory
    assert "- 项目使用 pytest" in memory

    # Prompt 必须包含系统提示、当前空记忆占位和归档正文。
    messages = provider.received_messages[0]
    assert messages[0] == {"role": "system", "content": DREAM_SYSTEM_PROMPT}
    user_text = messages[1]["content"]
    assert "(空)" in user_text
    assert "用户喜欢简洁回答" in user_text
    assert "项目使用 pytest" in user_text


async def test_nothing_response_advances_cursor_without_memory_update(
    store: MemoryStore,
) -> None:
    """LLM 输出 ``(nothing)`` 时无需改文件，但仍推进 cursor。"""
    seed_history(store, "只是一次闲聊")
    provider = ScriptedProvider("(nothing)")
    manager = DreamManager(provider, store)

    result = await manager.run()

    assert result.processed_entries == 1
    assert result.added == 0
    assert result.memory_updated is False
    assert result.llm_called is True
    assert result.error is None
    assert store.get_dream_cursor() == 1
    assert store.read_memory() == ""


# ---------------------------------------------------------------------------
# 失败路径：cursor 与 MEMORY.md 必须保持不变
# ---------------------------------------------------------------------------


async def test_provider_failure_keeps_cursor_and_memory(
    store: MemoryStore,
) -> None:
    """Provider 抛异常时 cursor 不推进、MEMORY.md 不变。"""
    seed_history(store, "用户偏好 A")
    provider = ScriptedProvider(RuntimeError("boom"))
    manager = DreamManager(provider, store)

    result = await manager.run()

    assert result.llm_called is True
    assert result.processed_entries == 1
    assert result.error is not None
    assert "boom" in result.error
    assert result.cursor == 0
    assert store.get_dream_cursor() == 0
    assert store.read_memory() == ""


@pytest.mark.parametrize("response", [LLMResponse(content=None), LLMResponse(content="   ")])
async def test_empty_response_keeps_cursor(
    store: MemoryStore, response: LLMResponse
) -> None:
    """空响应属于失败，cursor 与记忆不变。"""
    seed_history(store, "摘要")
    provider = ScriptedProvider(response)
    result = await DreamManager(provider, store).run()

    assert result.error is not None
    assert result.llm_called is True
    assert store.get_dream_cursor() == 0
    assert store.read_memory() == ""


@pytest.mark.parametrize(
    "content",
    [
        "这不是 JSON",
        "[]",
        '{"add": "not-a-list"}',
        '{"update": {"old": "a", "new": "b"}}',
    ],
)
async def test_invalid_json_or_structure_keeps_cursor(
    store: MemoryStore, content: str
) -> None:
    """非法 JSON / 非法顶层结构时 cursor 不推进、MEMORY.md 不变。"""
    seed_history(store, "摘要")
    provider = ScriptedProvider(content)
    result = await DreamManager(provider, store).run()

    assert result.error is not None
    assert result.llm_called is True
    assert result.cursor == 0
    assert store.get_dream_cursor() == 0
    assert store.read_memory() == ""


async def test_write_failure_keeps_cursor(
    store: MemoryStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MEMORY.md 写入失败（MemoryStoreError）时 cursor 不变。"""
    seed_history(store, "摘要")

    def fail_write(content: str) -> None:
        raise MemoryStoreError("disk full")

    monkeypatch.setattr(store, "write_memory", fail_write)
    provider = ScriptedProvider(
        dream_json(add=[{"section": "用户偏好", "text": "新偏好"}])
    )
    result = await DreamManager(provider, store).run()

    assert result.error is not None
    assert "disk full" in result.error
    assert result.memory_updated is False
    assert store.get_dream_cursor() == 0


async def test_memory_size_limit_fails_without_cursor_advance(
    store: MemoryStore,
) -> None:
    """最终文件超过 max_memory_bytes 时整次失败，不写文件、不推进 cursor。"""
    store.write_memory("# 长期记忆\n\n## 用户偏好\n- 已有条目\n")
    before = store.read_memory()
    seed_history(store, "大段内容")
    provider = ScriptedProvider(
        dream_json(add=[{"section": "用户偏好", "text": "x" * 200}])
    )
    config = DreamConfig(max_memory_bytes=50)
    result = await DreamManager(provider, store, config).run()

    assert result.error is not None
    assert "大小上限" in result.error
    assert result.cursor == 0
    assert store.get_dream_cursor() == 0
    assert store.read_memory() == before


# ---------------------------------------------------------------------------
# 重启恢复、截断与幂等
# ---------------------------------------------------------------------------


async def test_restart_resumes_from_cursor(store: MemoryStore) -> None:
    """新进程（新 MemoryStore/DreamManager）从 .dream_cursor 恢复，不重复处理。"""
    seed_history(store, "长期偏好 X", "决策 Y")
    first_provider = ScriptedProvider(
        dream_json(
            add=[
                {"section": "用户偏好", "text": "长期偏好 X"},
                {"section": "项目决策", "text": "决策 Y"},
            ]
        )
    )
    await DreamManager(first_provider, store).run()
    memory_after_first = store.read_memory()

    # 模拟重启：重新构造 store 与 manager，Provider 没有任何预设响应。
    restarted_store = MemoryStore(store.workspace)
    restarted_provider = ScriptedProvider()
    result = await DreamManager(restarted_provider, restarted_store).run()

    assert result.processed_entries == 0
    assert result.llm_called is False
    assert restarted_provider.call_count == 0
    assert restarted_store.get_dream_cursor() == 2
    assert restarted_store.read_memory() == memory_after_first


async def test_replay_same_batch_is_idempotent(store: MemoryStore) -> None:
    """cursor 被回退后重放同一批记录，规范化去重保证 MEMORY.md 不重复。"""
    seed_history(store, "用户偏好 X")
    response = dream_json(add=[{"section": "用户偏好", "text": "用户偏好 X"}])
    await DreamManager(ScriptedProvider(response), store).run()
    memory_after_first = store.read_memory()

    store.set_dream_cursor(0)
    result = await DreamManager(ScriptedProvider(response), store).run()

    assert result.processed_entries == 1
    assert result.added == 0
    assert result.skipped == 1
    assert result.memory_updated is False
    assert store.read_memory() == memory_after_first
    assert store.get_dream_cursor() == 1


async def test_default_max_entries_truncates_to_20(store: MemoryStore) -> None:
    """默认每次最多处理 20 条历史，剩余记录留给下一次 Dream。"""
    for index in range(25):
        store.append_history(
            f"归档摘要 {index + 1}",
            session_key="cli:main",
            timestamp="2026-01-01T00:00:00+08:00",
        )
    provider = ScriptedProvider("(nothing)", "(nothing)")
    manager = DreamManager(provider, store)

    first = await manager.run()
    assert first.processed_entries == 20
    assert first.cursor == 20
    first_prompt = provider.received_messages[0][1]["content"]
    assert "归档摘要 20" in first_prompt
    assert "归档摘要 21" not in first_prompt

    second = await manager.run()
    assert second.processed_entries == 5
    assert second.cursor == 25
    assert store.get_dream_cursor() == 25


async def test_run_max_entries_override(store: MemoryStore) -> None:
    """run(max_entries=...) 可覆盖配置值。"""
    seed_history(store, "一", "二", "三")
    provider = ScriptedProvider("(nothing)", "(nothing)")
    manager = DreamManager(provider, store)

    first = await manager.run(max_entries=1)
    assert first.processed_entries == 1
    assert first.cursor == 1

    second = await manager.run()
    assert second.processed_entries == 2
    assert second.cursor == 3


# ---------------------------------------------------------------------------
# 确定性操作：去重 / update / remove / 非法条目
# ---------------------------------------------------------------------------


async def test_add_deduplicates_normalized_entries(store: MemoryStore) -> None:
    """add 按规范化文本去重（忽略首尾空白、连续空白和 bullet 前缀）。"""
    store.write_memory("# 长期记忆\n\n## 用户偏好\n- 喜欢简洁回答\n")
    before = store.read_memory()
    seed_history(store, "聊到回答风格")
    provider = ScriptedProvider(
        dream_json(
            add=[
                {"section": "用户偏好", "text": "  喜欢简洁回答  "},
                {"section": "用户偏好", "text": "- 喜欢简洁回答"},
                {"section": "用户偏好", "text": "  -   喜欢简洁回答"},
            ]
        )
    )
    result = await DreamManager(provider, store).run()

    assert result.added == 0
    assert result.skipped == 3
    assert result.memory_updated is False
    assert store.read_memory() == before
    # 即使操作全部跳过，也推进 cursor 避免下一轮重复调用 LLM。
    assert store.get_dream_cursor() == 1


async def test_update_and_remove_operations(store: MemoryStore) -> None:
    """update 精确匹配已有条目后替换；remove 精确匹配后删除。"""
    store.write_memory(
        "# 长期记忆\n\n"
        "## 用户偏好\n"
        "- 喜欢简洁回答\n"
        "- 使用中文交流\n\n"
        "## 项目决策\n"
        "- 使用 unittest 作为测试框架\n"
    )
    seed_history(store, "讨论测试框架与交流语言")
    provider = ScriptedProvider(
        dream_json(
            update=[
                {
                    "old": "使用 unittest 作为测试框架",
                    "new": "使用 pytest 作为测试框架",
                },
                {"old": "不存在的条目", "new": "新条目"},
            ],
            remove=["使用中文交流", "也不存在的条目"],
        )
    )
    result = await DreamManager(provider, store).run()

    assert result.updated == 1
    assert result.removed == 1
    assert result.skipped == 2
    assert result.memory_updated is True
    assert result.cursor == 1

    memory = store.read_memory()
    assert "- 使用 pytest 作为测试框架" in memory
    assert "unittest" not in memory
    assert "使用中文交流" not in memory
    assert "- 喜欢简洁回答" in memory


async def test_invalid_entries_skipped_and_unknown_section_mapped(
    store: MemoryStore,
) -> None:
    """空/超长/控制字符条目跳过；未知 section 归类为“其他”。"""
    seed_history(store, "讨论若干规范")
    provider = ScriptedProvider(
        dream_json(
            add=[
                {"section": "用户偏好", "text": ""},
                {"section": "用户偏好", "text": "x" * 2001},
                {"section": "用户偏好", "text": "含\x00控制字符"},
                {"section": "用户偏好", "text": "有效偏好"},
                {"section": "不存在的分区", "text": "其他分区条目"},
            ]
        )
    )
    result = await DreamManager(provider, store).run()

    assert result.added == 2
    assert result.skipped == 3
    memory = store.read_memory()
    assert "- 有效偏好" in memory
    assert "- 其他分区条目" in memory
    # 未知 section 被归入“其他”，而不是生成自定义标题。
    assert "## 其他" in memory
    assert "不存在的分区" not in memory


async def test_fenced_json_is_accepted(store: MemoryStore) -> None:
    """允许 ```json 围栏包裹的 LLM 响应。"""
    seed_history(store, "摘要")
    fenced = "```json\n" + dream_json(
        add=[{"section": "经验知识", "text": "pytest-asyncio 的 auto 模式可用"}]
    ) + "\n```"
    provider = ScriptedProvider(fenced)
    result = await DreamManager(provider, store).run()

    assert result.added == 1
    assert "- pytest-asyncio 的 auto 模式可用" in store.read_memory()


# ---------------------------------------------------------------------------
# 互斥与调度器
# ---------------------------------------------------------------------------


async def test_busy_lock_returns_immediately(store: MemoryStore) -> None:
    """已有 Dream 持锁时，第二次调用立即 busy，不重复调用 LLM。"""
    seed_history(store, "摘要")
    provider = BlockingProvider()
    manager = DreamManager(provider, store)

    first_task = asyncio.create_task(manager.run())
    await asyncio.wait_for(provider.started.wait(), timeout=1)

    second = await manager.run()
    assert second.busy is True
    assert second.error == BUSY_ERROR
    assert provider.call_count == 1

    provider.release.set()
    first = await asyncio.wait_for(first_task, timeout=1)
    assert first.busy is False
    assert first.cursor == 1
    assert store.get_dream_cursor() == 1


async def test_scheduler_disabled_returns_none(store: MemoryStore) -> None:
    """DreamConfig.enabled=False 时 run_once/start 均不执行。"""
    provider = ScriptedProvider()
    manager = DreamManager(provider, store, DreamConfig(enabled=False))
    scheduler = DreamScheduler(manager)

    assert await scheduler.run_once() is None
    assert scheduler.start() is None
    assert provider.call_count == 0


async def test_scheduler_run_on_start_executes_immediately(
    store: MemoryStore,
) -> None:
    """run_on_start=True 时 start 后立即执行一次 Dream。"""
    seed_history(store, "摘要")
    provider = ScriptedProvider("(nothing)")
    manager = DreamManager(provider, store)
    scheduler = DreamScheduler(manager, interval_hours=1.0, run_on_start=True)

    task = scheduler.start()
    assert task is not None
    await asyncio.wait_for(provider.called.wait(), timeout=1)
    await scheduler.stop()

    assert provider.call_count == 1
    assert store.get_dream_cursor() == 1


async def test_scheduler_periodic_run(store: MemoryStore) -> None:
    """run_on_start=False 时首次不执行，间隔到达后才执行。"""
    seed_history(store, "摘要")
    provider = ScriptedProvider("(nothing)")
    manager = DreamManager(provider, store)
    # 0.00001 小时 = 0.036 秒，便于测试而不必真的等 2 小时。
    scheduler = DreamScheduler(manager, interval_hours=0.00001, run_on_start=False)

    task = scheduler.start()
    assert task is not None
    await asyncio.sleep(0)
    assert provider.call_count == 0

    await asyncio.wait_for(provider.called.wait(), timeout=2)
    await scheduler.stop()

    assert provider.call_count == 1
    assert store.get_dream_cursor() == 1


def test_stale_dream_cursor_is_clamped_to_latest_history(store: MemoryStore) -> None:
    """history 被重建后旧 cursor 不能永久屏蔽新记录。"""
    store.set_dream_cursor(99)
    store.append_history("摘要一", session_key="cli:main", checkpoint_id="cp-1")
    provider = ScriptedProvider(
        json.dumps(
            {"add": [{"section": "项目决策", "text": "使用 Python 3.14"}],
             "update": [], "remove": []},
            ensure_ascii=False,
        )
    )
    result = asyncio.run(DreamManager(provider, store, DreamConfig()).run())
    assert result.error is None
    assert result.processed_entries == 1
    assert result.cursor == 1
    assert store.get_dream_cursor() == 1
    assert "Python 3.14" in store.read_memory()


def test_nothing_variants_are_accepted(store: MemoryStore) -> None:
    """模型输出的常见 (nothing) 变体应视为无操作而不是非法 JSON。"""
    store.append_history("摘要一", session_key="cli:main", checkpoint_id="cp-1")
    provider = ScriptedProvider("（nothing）。")
    result = asyncio.run(DreamManager(provider, store, DreamConfig()).run())
    assert result.error is None
    assert result.processed_entries == 1
    assert result.cursor == 1
    assert store.get_dream_cursor() == 1
    assert "摘要一" not in store.read_memory()
