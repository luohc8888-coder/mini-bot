"""交互式推理设置：保存/恢复、立即发送、历史与其他配置保持。"""

import json
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

import mini_harness.cli.app as cli_app
from mini_harness.cli import InteractiveCLI, ProfileStore, ProviderProfile
from mini_harness.providers import ProviderConfig
from tests.test_cli import ScriptedConsole


def profile() -> ProviderProfile:
    return ProviderProfile(
        name="DeepSeek", provider="custom", model="deepseek-v4.1-flash-1017",
        base_url="https://maas.qianwenaiapi.com/compatible-mode/v1", api_key="test-secret",
        context_window_tokens=1048576, max_output_tokens=393216,
        thinking=True, reasoning_effort="max", is_default=True,
    )


@pytest.mark.asyncio
async def test_new_profile_saves_selected_reasoning_effort(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-env-key")
    console = ScriptedConsole(["2", "", "", "high", "y", "new"])
    store = ProfileStore(tmp_path / "config.json")
    cli = InteractiveCLI(console=console, profile_store=store)
    selection = await cli._create_config()
    assert selection.profile_name == "new"
    assert selection.config.reasoning_effort == "high" and selection.config.thinking
    saved = store.default_profile()
    assert saved.reasoning_effort == "high" and saved.thinking
    restored = await cli._config_from_profile(saved)
    assert restored.config.reasoning_effort == "high"


@pytest.mark.parametrize("workspace_enabled", [False, True])
@pytest.mark.parametrize("choice,expected", [("low", "low"), ("0", ""), ("q", "max")])
@pytest.mark.asyncio
async def test_settings_rebuilds_provider_and_preserves_chat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, workspace_enabled: bool,
    choice: str, expected: str,
) -> None:
    store = ProfileStore(tmp_path / "config.json")
    original = profile()
    store.upsert(original, set_default=True)
    unrelated = replace(original, name="other", reasoning_effort="high", is_default=False)
    store.upsert(unrelated)
    before = store.path.read_bytes()
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{
            "message": {"role": "assistant", "content": "OK"}, "finish_reason": "stop",
        }]})

    real_factory = cli_app.create_provider

    def create(config):
        provider = real_factory(config)
        provider.transport = httpx.MockTransport(handler)
        return provider

    monkeypatch.setattr(cli_app, "create_provider", create)
    console = ScriptedConsole(["第一句", "/settings", choice, "第二句", "/exit"])
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    cli = InteractiveCLI(console=console, profile_store=store,
                         workspace=workspace if workspace_enabled else None,
                         data_root=tmp_path / "data" if workspace_enabled else None)
    await cli.run()

    assert len(requests) == 2  # 设置命令不请求 LLM。
    assert requests[0]["reasoning_effort"] == "max"
    if expected:
        assert requests[1]["reasoning_effort"] == expected
        assert requests[1]["enable_thinking"] is True
    else:
        assert "reasoning_effort" not in requests[1]
        assert "enable_thinking" not in requests[1]
    users = [m["content"] for m in requests[1]["messages"] if m["role"] == "user"]
    assert users == ["第一句", "第二句"]
    saved = store.default_profile()
    assert saved == replace(original, reasoning_effort=expected, thinking=bool(expected))
    assert next(p for p in store.load() if p.name == "other") == unrelated
    assert "test-secret" not in console.text
    if choice == "q":
        assert store.path.read_bytes() == before
        assert "已取消推理设置" in console.text
    else:
        assert "已保存推理设置" in console.text
    if cli.session_manager:
        messages = cli.session_manager.load().messages
        assert [m["content"] for m in messages if m["role"] == "user"] == users


@pytest.mark.asyncio
async def test_save_failure_applies_settings_for_current_connection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = ProfileStore(tmp_path / "config.json")
    original = profile()
    store.upsert(original)
    cli = InteractiveCLI(console=ScriptedConsole(["high"]), profile_store=store)
    selection = await cli._config_from_profile(original)

    def fail(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(store, "upsert", fail)
    provider, updated = await cli._edit_reasoning_settings(selection)
    assert updated.config.reasoning_effort == "high"
    assert provider._build_payload([{"role": "user", "content": "OK"}], None)["reasoning_effort"] == "high"
    assert store.load() == [original]
    assert "保存失败，仅本次连接生效" in cli.console.text


@pytest.mark.asyncio
async def test_menu_handles_invalid_choices_and_custom_effort(tmp_path: Path) -> None:
    console = ScriptedConsole(["medium", "bad", "c", "invalid value", "c", "custom-effort"])
    cli = InteractiveCLI(console=console, profile_store=ProfileStore(tmp_path / "config.json"))
    effort = await cli._choose_reasoning_effort(profile().to_config())
    assert effort == "custom-effort"
    assert console.text.count("请选择有效序号或档位名称") == 3


@pytest.mark.asyncio
async def test_enter_keeps_current_effort(tmp_path: Path) -> None:
    cli = InteractiveCLI(console=ScriptedConsole([""]), profile_store=ProfileStore(tmp_path / "config.json"))
    assert await cli._choose_reasoning_effort(profile().to_config()) == "max"


@pytest.mark.asyncio
async def test_unsaved_config_settings_remain_in_memory(tmp_path: Path) -> None:
    cli = InteractiveCLI(console=ScriptedConsole(["max"]), profile_store=ProfileStore(tmp_path / "config.json"))
    selection = cli_app.Selection(ProviderConfig(provider="custom", model="deepseek-v4.1-flash-1017", base_url="https://example.com/v1"), cli_app.get_preset("custom"))
    _, updated = await cli._edit_reasoning_settings(selection)
    assert updated.config.reasoning_effort == "max"
    assert not cli.store.path.exists()
    assert "本次连接" in cli.console.text
