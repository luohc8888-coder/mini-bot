"""真实模型能力、已保存配置覆盖和小窗口回退的回归。"""

from copy import deepcopy
from pathlib import Path

import pytest

from mini_harness.agent import ContextConfig
from mini_harness.agent.compaction import ContextGovernor, truncate_tool_results
from mini_harness.agent.context import estimate_messages_tokens
from mini_harness.agent.runner import _is_readonly_command
from mini_harness.cli import InteractiveCLI, ProfileStore, ProviderProfile
from mini_harness.cli.app import Selection
from mini_harness.providers import MockProvider, ProviderConfig, get_preset
from mini_harness.providers.openai_compat import OpenAICompatibleProvider


@pytest.mark.asyncio
async def test_old_activity_summary_can_shrink_under_small_overflow() -> None:
    messages = [
        {"role": "system", "content": "s" * 2800},
        {"role": "user", "content": "分析当前工具"},
        {"role": "assistant", "accepted_summary": True, "content": "old activity " * 1300},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "read", "name": "run_command", "arguments": {"command": "cat README.md"}}
        ]},
        {"role": "tool", "name": "run_command", "tool_call_id": "read", "content": "result " * 1000},
    ]
    original = deepcopy(messages)
    aggressive, _ = truncate_tool_results(messages, 128)
    # 模拟只超出一百余 token、但摘要固定占约 4K 的真实故障。
    budget = estimate_messages_tokens(aggressive) - 114
    governor = ContextGovernor(MockProvider([]), ContextConfig(
        max_context_tokens=budget + 1024, reserve_output_tokens=1024,
        max_tool_result_tokens=128, enable_auto_compact=False,
    ))
    governed = await governor.prepare_request(messages, None, iteration=10, protected_start=1)
    assert governed.estimated_tokens <= budget
    summary = next(m for m in governed.messages if m.get("accepted_summary"))
    assert len(summary["content"]) < len(original[2]["content"])
    assert governed.messages[1] == original[1]
    assert governed.messages[-1]["tool_call_id"] == "read"
    assert messages == original


@pytest.mark.asyncio
async def test_capacity_flags_override_existing_profile_and_activate_immediately(tmp_path: Path) -> None:
    store = ProfileStore(tmp_path / "config.json")
    profile = ProviderProfile(name="saved", provider="custom", model="deepseek-v4.1-flash-1017", api_key="test", base_url="https://maas.qianwenaiapi.com/compatible-mode/v1")
    store.upsert(profile)
    cli = InteractiveCLI(
        profile_store=store, workspace=tmp_path, data_root=tmp_path / "data",
        context_window_tokens=1048576, max_output_tokens=393216,
        thinking=True, reasoning_effort="max",
    )
    selection = await cli._config_from_profile(profile)
    config = selection.config
    assert (config.context_window_tokens, config.max_output_tokens) == (1048576, 393216)
    assert config.thinking and config.reasoning_effort == "max"
    cli._activate_selection(selection)
    assert cli.context_builder.config.max_context_tokens == 1048576
    assert cli.context_builder.config.available_input_tokens == 1044480
    assert cli.context_builder.config.reserve_output_tokens == 4096
    assert store.load() == [profile]  # 本次覆盖不偷偷改写保存配置。


@pytest.mark.parametrize("window,reserve", [(32768, 2048), (0, 2048)])
def test_context_overrides_are_independent(tmp_path: Path, window: int, reserve: int) -> None:
    cli = InteractiveCLI(profile_store=ProfileStore(tmp_path / "config.json"), max_context_tokens=window, reserve_output_tokens=reserve)
    selection = Selection(ProviderConfig(provider="custom", context_window_tokens=1048576, max_output_tokens=393216), get_preset("custom"))
    config = cli._effective_context_config(selection)
    assert config.max_context_tokens == (window or 1048576)
    assert config.reserve_output_tokens == reserve


def test_unknown_model_does_not_inherit_previous_model_window(tmp_path: Path) -> None:
    cli = InteractiveCLI(profile_store=ProfileStore(tmp_path / "config.json"))
    first = Selection(ProviderConfig(provider="custom", context_window_tokens=1048576), get_preset("custom"))
    cli._activate_selection(first)
    unknown = Selection(ProviderConfig(provider="custom"), get_preset("custom"))
    assert cli._effective_context_config(unknown).max_context_tokens == 8192


@pytest.mark.asyncio
async def test_output_limit_tracks_remaining_window_and_recovers() -> None:
    provider = MockProvider([])
    provider.max_tokens = 12000
    provider.context_window_tokens = 1048576
    governor = ContextGovernor(provider, ContextConfig(max_context_tokens=16384, reserve_output_tokens=1024))
    large = await governor.prepare_request([{"role": "user", "content": "x" * 40000}], None)
    assert provider.max_tokens == 16384 - large.estimated_tokens
    assert provider.max_tokens < 12000
    await governor.prepare_request([{"role": "user", "content": "small"}], None)
    assert provider.max_tokens == 12000


def test_maximum_output_is_not_reserved_from_every_request() -> None:
    provider = MockProvider([])
    provider.max_tokens = 393216
    provider.context_window_tokens = 1048576
    config = ContextConfig.from_provider(provider)
    assert config.max_context_tokens == 1048576
    assert config.reserve_output_tokens == 4096


@pytest.mark.parametrize("host,key", [
    ("https://maas.qianwenaiapi.com/compatible-mode/v1", "enable_thinking"),
    ("https://dashscope.aliyuncs.com/compatible-mode/v1", "enable_thinking"),
    ("https://api.deepseek.com/v1", "thinking"),
])
def test_thinking_payload_uses_endpoint_format(host: str, key: str) -> None:
    provider = OpenAICompatibleProvider(api_key="test", base_url=host, model="deepseek-v4.1-flash-1017", thinking=True, reasoning_effort="max", max_tokens=393216)
    payload = provider._build_payload([{"role": "user", "content": "OK"}], None)
    assert payload[key] == (True if key == "enable_thinking" else {"type": "enabled"})
    assert payload["reasoning_effort"] == "max" and payload["max_tokens"] == 393216
    other = "thinking" if key == "enable_thinking" else "enable_thinking"
    assert other not in payload


@pytest.mark.parametrize("command,expected", [
    ("sed -n 1,20p docs/report.md", True),
    ("cat docs/report.md", True),
    ("grep -n 'class \\|name' mini_harness/tools/*.py", True),
    ("sed -i 's/a/b/' file", False),
    ("cat file > other", False),
    ("cat file; rm file", False),
    ("python -c 'print(1)'", False),
])
def test_readonly_shell_reads_contribute_to_exploration_progress(command: str, expected: bool) -> None:
    assert _is_readonly_command(command) is expected
