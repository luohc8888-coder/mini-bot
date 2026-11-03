"""交互式 CLI 菜单和配置存储测试。"""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from mini_harness.cli import (
    Console,
    InteractiveCLI,
    ProfileStore,
    ProviderProfile,
    print_provider_catalog,
)
from mini_harness.providers import get_preset


class ScriptedConsole(Console):
    """按预置答案模拟终端输入并收集输出。"""

    def __init__(
        self,
        answers: list[str],
        secrets: list[str] | None = None,
    ) -> None:
        self.answers = iter(answers)
        self.secrets = iter(secrets or [])
        self.output: list[str] = []
        self.prompts: list[str] = []

    def write(self, text: str = "") -> None:
        self.output.append(text)

    async def read(self, prompt: str = "") -> str:
        self.prompts.append(prompt)
        try:
            return next(self.answers)
        except StopIteration as exc:
            raise EOFError from exc

    async def read_secret(self, prompt: str = "") -> str:
        self.prompts.append(prompt)
        try:
            return next(self.secrets)
        except StopIteration as exc:
            raise EOFError from exc

    @property
    def text(self) -> str:
        return "\n".join(self.output)


def test_profile_store_roundtrip_does_not_persist_api_key(tmp_path: Path) -> None:
    """profile 只保存非敏感配置，并采用 0600 权限。"""
    path = tmp_path / "nested" / "config.json"
    store = ProfileStore(path)
    profile = ProviderProfile(
        name="DeepSeek 默认",
        provider="deepseek",
        model="deepseek-chat",
        base_url="https://api.deepseek.com/v1",
    )
    store.upsert(profile)

    loaded = store.load()
    assert loaded == [profile]
    raw = path.read_text(encoding="utf-8")
    assert "deepseek-chat" in raw
    assert "api_key" not in raw
    assert stat.S_IMODE(path.stat().st_mode) == 0o600

    payload = json.loads(raw)
    assert payload["version"] == 1
    assert payload["profiles"][0]["name"] == "DeepSeek 默认"


def test_profile_store_delete(tmp_path: Path) -> None:
    """可以删除已保存配置。"""
    store = ProfileStore(tmp_path / "config.json")
    store.upsert(
        ProviderProfile(
            name="temp",
            provider="mock",
            model="mock-add-demo",
        )
    )
    assert store.delete("temp") is True
    assert store.load() == []
    assert store.delete("missing") is False


@pytest.mark.asyncio
async def test_interactive_cli_mock_chat(tmp_path: Path) -> None:
    """无保存配置时可新建 Mock 配置并完成一次工具调用对话。"""
    console = ScriptedConsole(
        answers=["1", "", "计算 2 + 3", "/exit"],
    )
    cli = InteractiveCLI(
        console=console,
        profile_store=ProfileStore(tmp_path / "config.json"),
    )
    await cli.run()

    assert "尚未保存配置" in console.text
    assert "Mock（离线演示）" in console.text
    assert "助手 > 结果是 5" in console.text
    assert "[工具] add" in console.text
    assert not (tmp_path / "config.json").exists()


@pytest.mark.asyncio
async def test_interactive_cli_mock_supports_multiple_turns(tmp_path: Path) -> None:
    """Mock 的有限响应序列不能导致第二个用户回合失败。"""
    console = ScriptedConsole(
        answers=["1", "", "第一轮", "第二轮", "/exit"],
    )
    cli = InteractiveCLI(
        console=console,
        profile_store=ProfileStore(tmp_path / "config.json"),
    )
    await cli.run()

    assert console.text.count("助手 > 结果是 5") == 2
    assert "请求失败" not in console.text
    assert "响应序列已耗尽" not in console.text


@pytest.mark.asyncio
async def test_interactive_cli_auto_uses_saved_profile(tmp_path: Path) -> None:
    """只有一个保存配置时应自动作为默认配置启动。"""
    store = ProfileStore(tmp_path / "config.json")
    store.upsert(
        ProviderProfile(
            name="本地 Mock",
            provider="mock",
            model="mock-add-demo",
        )
    )
    console = ScriptedConsole(
        answers=["你好", "/exit"],
    )
    cli = InteractiveCLI(console=console, profile_store=store)
    await cli.run()

    assert "使用默认配置" in console.text
    assert "选择配置" not in console.text
    assert "助手 > 结果是 5" in console.text


@pytest.mark.asyncio
async def test_interactive_cli_saves_profile_without_api_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """从环境变量读取 Key 后保存配置，磁盘上不能出现 Key。"""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-secret-should-not-be-saved")
    store = ProfileStore(tmp_path / "config.json")
    console = ScriptedConsole(
        answers=[
            "2",   # Provider 菜单第 2 项：DeepSeek
            "",    # 模型默认 deepseek-chat
            "",    # Base URL 使用默认值
            "y",   # 保存配置
            "",    # 使用默认配置名
            "/exit",
        ],
    )
    cli = InteractiveCLI(console=console, profile_store=store)
    await cli.run()

    profiles = store.load()
    assert len(profiles) == 1
    assert profiles[0].provider == "deepseek"
    assert profiles[0].model == "deepseek-chat"
    raw = store.path.read_text(encoding="utf-8")
    assert "sk-secret-should-not-be-saved" not in raw
    assert "检测到环境变量 DEEPSEEK_API_KEY" in console.text


def test_print_provider_catalog_includes_huawei_and_custom() -> None:
    """--list-providers 输出应覆盖国内、第三方和自定义配置。"""
    console = ScriptedConsole([])
    print_provider_catalog(console)
    assert "DeepSeek" in console.text
    assert "Huawei Cloud MaaS" in console.text
    assert "Huawei MaaS Direct" in console.text
    assert "OpenRouter" in console.text
    assert "自定义 OpenAI-Compatible" in console.text


@pytest.mark.asyncio
async def test_choose_model_uses_preset_default_index(tmp_path: Path) -> None:
    """Huawei 的默认模型在候选列表第 4 项，Enter 应选中它。"""
    console = ScriptedConsole(answers=[""])
    cli = InteractiveCLI(
        console=console,
        profile_store=ProfileStore(tmp_path / "config.json"),
    )
    selected = await cli._choose_model(get_preset("huawei-maas"))
    assert selected == "deepseek-v4.1-flash-1017"
    assert any("请选择模型 [4]" in prompt for prompt in console.prompts)


def test_profile_store_migrates_legacy_max_tokens(tmp_path: Path) -> None:
    """旧配置中的 1024 默认值应迁移为 4096，避免长报告被截断。"""
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "profiles": [
                    {
                        "name": "legacy",
                        "provider": "custom",
                        "model": "m",
                        "max_tokens": 1024,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    loaded = ProfileStore(path).load()

    assert len(loaded) == 1
    assert loaded[0].max_tokens == 4096


def test_profile_store_handles_null_base_url_and_non_utf8(tmp_path: Path) -> None:
    """损坏或不完整的 profile 不应让 CLI 启动崩溃。"""
    path = tmp_path / "config.json"
    path.write_text(
        '{"profiles": [{"name": "x", "provider": "custom", "model": "m", "base_url": null}]}',
        encoding="utf-8",
    )
    loaded = ProfileStore(path).load()
    assert len(loaded) == 1
    assert loaded[0].base_url == ""

    path.write_bytes(b"\xff\xfe\x00")
    assert ProfileStore(path).load() == []


def test_profile_store_persists_api_key_and_default(tmp_path: Path) -> None:
    """用户明确保存 profile 时，API Key 和默认标记应持久化。"""
    path = tmp_path / "config.json"
    store = ProfileStore(path)
    profile = ProviderProfile(
        name="带 Key 的配置",
        provider="custom",
        model="my-model",
        base_url="https://example.com/v1",
        api_key="sk-saved-secret",
        is_default=True,
    )
    store.upsert(profile, set_default=True)

    loaded = store.load()
    assert loaded[0].api_key == "sk-saved-secret"
    assert loaded[0].is_default is True
    assert store.default_profile() == loaded[0]
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["profiles"][0]["api_key"] == "sk-saved-secret"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.asyncio
async def test_interactive_cli_default_profile_skips_api_key_prompt(
    tmp_path: Path,
) -> None:
    """默认 profile 已保存 Key 时，启动不再提示输入 Key。"""
    store = ProfileStore(tmp_path / "config.json")
    store.upsert(
        ProviderProfile(
            name="默认配置",
            provider="custom",
            model="my-model",
            base_url="https://example.com/v1",
            api_key="sk-saved-secret",
            is_default=True,
        ),
        set_default=True,
    )
    console = ScriptedConsole(answers=["/exit"])
    cli = InteractiveCLI(console=console, profile_store=store)
    await cli.run()

    assert "使用默认配置" in console.text
    assert "API Key（可留空）" not in "\n".join(console.prompts)
    assert "已退出。" in console.text


@pytest.mark.asyncio
async def test_interactive_cli_model_command_switches_profile(tmp_path: Path) -> None:
    """/model 应能切换到另一个已保存的 Provider/模型配置。"""
    store = ProfileStore(tmp_path / "config.json")
    store.upsert(
        ProviderProfile(
            name="第一个配置",
            provider="mock",
            model="mock-add-demo",
            is_default=True,
        ),
        set_default=True,
    )
    store.upsert(
        ProviderProfile(
            name="第二个配置",
            provider="mock",
            model="mock-add-demo",
        )
    )
    console = ScriptedConsole(
        answers=["/model", "2", "/exit"],
    )
    cli = InteractiveCLI(console=console, profile_store=store)
    await cli.run()

    assert "已切换：Mock（离线演示） / mock-add-demo" in console.text
    assert store.default_profile().name == "第二个配置"


@pytest.mark.asyncio
async def test_legacy_single_profile_is_promoted_to_default_with_saved_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """旧版单 profile 首次输入 Key 后应自动补写为默认配置。"""
    monkeypatch.delenv("MINI_HARNESS_API_KEY", raising=False)
    store = ProfileStore(tmp_path / "config.json")
    store.upsert(
        ProviderProfile(
            name="旧配置",
            provider="custom",
            model="my-model",
            base_url="https://example.com/v1",
        )
    )
    console = ScriptedConsole(answers=["/exit"], secrets=["sk-new-key"])
    cli = InteractiveCLI(console=console, profile_store=store)
    await cli.run()

    loaded = store.load()[0]
    assert loaded.api_key == "sk-new-key"
    assert loaded.is_default is True
    assert store.default_profile().name == "旧配置"


@pytest.mark.asyncio
async def test_console_secret_strips_terminal_newlines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """API Key 输入不应把回车/换行符保存为 Key 的一部分。"""
    import mini_harness.cli.app as cli_app

    monkeypatch.setattr(
        cli_app.getpass,
        "getpass",
        lambda prompt="": "sk-test-key\r\n",
    )
    console = Console(color=False)
    assert await console.read_secret("API Key: ") == "sk-test-key"


def test_console_role_colors(capsys: pytest.CaptureFixture[str]) -> None:
    """用户/助手标签在彩色终端下应有不同 ANSI 颜色。"""
    console = Console(color=True)
    assert "\033[1;36m你 > " in console.user_prompt()
    console.write_assistant("你好")
    output = capsys.readouterr().out
    assert "\033[1;32m助手 >" in output
    assert "你好" in output

    plain = Console(color=False)
    assert plain.user_prompt() == "你 > "


@pytest.mark.asyncio
async def test_console_read_separates_ansi_prompt_from_input(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """彩色提示符不能直接交给 input()，否则退格/删除会受 ANSI 宽度影响。"""
    import builtins

    seen_prompts: list[str] = []

    def fake_input(prompt: str = "") -> str:
        seen_prompts.append(prompt)
        return "hello"

    monkeypatch.setattr(builtins, "input", fake_input)
    console = Console(color=True)
    assert await console.read(console.user_prompt()) == "hello"
    assert seen_prompts == [""]
    assert "\033[1;36m你 > " in capsys.readouterr().out
