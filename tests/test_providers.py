"""Provider 预设、工厂和 HTTP 协议适配器测试。"""

from __future__ import annotations

import json

import httpx
import pytest

from mini_harness.agent import AgentRunSpec, AgentRunner
from mini_harness.providers import (
    AnthropicProvider,
    MockProvider,
    OpenAICompatibleProvider,
    ProviderConfig,
    ProviderConfigurationError,
    ProviderError,
    create_provider,
    grouped_presets,
)
from mini_harness.providers.presets import PRESETS
from mini_harness.tools import AddTool, ToolRegistry


def test_grouped_presets_cover_all_presets_and_huawei_models() -> None:
    """菜单分组必须覆盖全部预设，并包含截图中的华为云模型。"""
    grouped_keys = [
        preset.key
        for _, presets in grouped_presets()
        for preset in presets
    ]
    assert grouped_keys == list(PRESETS)
    assert len(grouped_keys) == len(set(grouped_keys))
    assert "huawei-maas" in PRESETS
    assert "huawei-maas-direct" in PRESETS
    for model in ("DeepSeek-v4.1-Flash", "GLM-5.3", "Qwen-300", "deepseek-v4.1-flash-1017"):
        assert model in PRESETS["huawei-maas"].models


def test_factory_builds_openai_compatible_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """DeepSeek 预设应创建 OpenAI-compatible Provider 并读取环境变量 Key。"""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-deepseek-test")
    provider = create_provider(
        ProviderConfig(provider="deepseek", model="deepseek-chat")
    )
    assert isinstance(provider, OpenAICompatibleProvider)
    assert provider.base_url == "https://api.deepseek.com/v1"
    assert provider.api_key == "sk-deepseek-test"
    assert provider.model == "deepseek-chat"


def test_factory_builds_anthropic_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """Anthropic 预设应创建 Messages API 适配器。"""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    provider = create_provider(
        ProviderConfig(provider="anthropic", model="claude-sonnet-4-5")
    )
    assert isinstance(provider, AnthropicProvider)
    assert provider.messages_url == "https://api.anthropic.com/v1/messages"


def test_factory_accepts_zhipu_env_alias(monkeypatch: pytest.MonkeyPatch) -> None:
    """智谱同时支持 ZHIPUAI_API_KEY 与 ZAI_API_KEY。"""
    monkeypatch.delenv("ZHIPUAI_API_KEY", raising=False)
    monkeypatch.setenv("ZAI_API_KEY", "sk-zai-test")
    provider = create_provider(
        ProviderConfig(provider="zhipu", model="glm-4-plus")
    )
    assert isinstance(provider, OpenAICompatibleProvider)
    assert provider.api_key == "sk-zai-test"


def test_factory_builds_kimi_coding_with_extra_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Kimi Coding 使用 Anthropic 协议并携带专用 User-Agent。"""
    monkeypatch.setenv("KIMI_CODING_API_KEY", "sk-kimi-test")
    provider = create_provider(
        ProviderConfig(provider="kimi-coding", model="kimi-k2.5")
    )
    assert isinstance(provider, AnthropicProvider)
    assert provider.base_url == "https://api.kimi.com/coding/v1"
    assert provider.extra_headers["User-Agent"] == "claude-code/0.1.0"


def test_factory_requires_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """需要的 Key 缺失时应抛出可读配置错误。"""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(ProviderConfigurationError, match="API Key"):
        create_provider(
            ProviderConfig(provider="openai", model="gpt-4.1-mini")
        )


def test_factory_requires_custom_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """自定义 Provider 未提供 Base URL 时不能创建。"""
    monkeypatch.delenv("MINI_HARNESS_BASE_URL", raising=False)
    with pytest.raises(ProviderConfigurationError, match="Base URL"):
        create_provider(
            ProviderConfig(provider="custom", model="some-model")
        )


def test_factory_resolves_huawei_base_url_from_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """华为云 Base URL 可由环境变量提供，方便本地初始化。"""
    monkeypatch.setenv("HUAWEI_MAAS_API_KEY", "sk-huawei-test")
    monkeypatch.setenv("HUAWEI_MAAS_BASE_URL", "https://maas.example.com/v1")
    provider = create_provider(
        ProviderConfig(provider="huawei-maas", model="DeepSeek-v4.1-Flash")
    )
    assert isinstance(provider, OpenAICompatibleProvider)
    assert provider.base_url == "https://maas.example.com/v1"


def test_factory_builds_mock_provider() -> None:
    """Mock 预设不要求 Key 或 Base URL。"""
    provider = create_provider(
        ProviderConfig(provider="mock", model="mock-add-demo")
    )
    assert isinstance(provider, MockProvider)


def test_factory_builds_custom_openai_compatible_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """自定义 Provider 可用任意 Base URL 和模型，并允许无 Key。"""
    monkeypatch.delenv("MINI_HARNESS_API_KEY", raising=False)
    monkeypatch.delenv("MINI_HARNESS_BASE_URL", raising=False)
    provider = create_provider(
        ProviderConfig(
            provider="custom",
            model="my-model",
            base_url="http://127.0.0.1:8000/v1",
        )
    )
    assert isinstance(provider, OpenAICompatibleProvider)
    assert provider.base_url == "http://127.0.0.1:8000/v1"
    assert provider.model == "my-model"
    assert provider.api_key == ""


@pytest.mark.asyncio
async def test_openai_provider_converts_messages_and_tool_calls() -> None:
    """OpenAI 适配器应正确转换内部消息与工具响应。"""
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["headers"] = dict(request.headers)
        seen["payload"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_2",
                                    "type": "function",
                                    "function": {
                                        "name": "add",
                                        "arguments": '{"a": 10, "b": 20}',
                                    },
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ]
            },
        )

    provider = OpenAICompatibleProvider(
        api_key="sk-test",
        base_url="https://api.deepseek.com/v1",
        model="deepseek-chat",
        transport=httpx.MockTransport(handler),
    )
    response = await provider.chat(
        [
            {"role": "user", "content": "计算 10 + 20"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": "call_1", "name": "add", "arguments": {"a": 2, "b": 3}}
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call_1",
                "name": "add",
                "content": "5",
            },
        ],
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "add",
                    "description": "两数相加",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ],
    )

    assert seen["url"] == "https://api.deepseek.com/v1/chat/completions"
    payload = seen["payload"]
    assert isinstance(payload, dict)
    messages = payload["messages"]
    assert messages[1]["tool_calls"][0]["function"]["arguments"] == '{"a": 2, "b": 3}'
    assert messages[2]["role"] == "tool"
    assert response.content is None
    assert response.finish_reason == "tool_calls"
    assert response.tool_calls[0].id == "call_2"
    assert response.tool_calls[0].name == "add"
    assert response.tool_calls[0].arguments == {"a": 10, "b": 20}


@pytest.mark.asyncio
async def test_openai_provider_wraps_http_error() -> None:
    """非 2xx 响应应转为 ProviderError，而不是泄露 httpx 异常。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            json={"error": {"message": "invalid api key"}},
        )

    provider = OpenAICompatibleProvider(
        api_key="bad",
        base_url="https://example.com/v1",
        model="m",
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(ProviderError, match="HTTP 401"):
        await provider.chat([{"role": "user", "content": "hi"}])


@pytest.mark.asyncio
async def test_anthropic_provider_converts_tool_blocks() -> None:
    """Anthropic 适配器应处理 system、tool_use、tool_result 和返回块。"""
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["headers"] = dict(request.headers)
        seen["payload"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "content": [
                    {"type": "text", "text": "我来计算。"},
                    {
                        "type": "tool_use",
                        "id": "toolu_1",
                        "name": "add",
                        "input": {"a": 2, "b": 3},
                    },
                ],
                "stop_reason": "tool_use",
            },
        )

    provider = AnthropicProvider(
        api_key="sk-ant",
        base_url="https://api.anthropic.com/v1",
        model="claude-test",
        transport=httpx.MockTransport(handler),
    )
    response = await provider.chat(
        [
            {"role": "system", "content": "你是助手"},
            {"role": "user", "content": "计算 2 + 3"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": "toolu_0", "name": "add", "arguments": {"a": 2, "b": 3}}
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "toolu_0",
                "name": "add",
                "content": "5",
            },
        ],
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "add",
                    "description": "两数相加",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ],
    )

    headers = seen["headers"]
    assert headers["x-api-key"] == "sk-ant"
    assert headers["anthropic-version"] == "2023-06-01"
    payload = seen["payload"]
    assert isinstance(payload, dict)
    assert payload["system"] == "你是助手"
    assert payload["messages"][1]["content"][0]["type"] == "tool_use"
    assert payload["messages"][2]["content"][0]["type"] == "tool_result"
    assert payload["tools"][0]["input_schema"]["type"] == "object"
    assert response.content == "我来计算。"
    assert response.tool_calls[0].id == "toolu_1"
    assert response.tool_calls[0].arguments == {"a": 2, "b": 3}


@pytest.mark.asyncio
async def test_openai_provider_full_tool_loop_with_agent_runner() -> None:
    """OpenAI 适配器应能与 AgentRunner 完成真实的工具调用闭环。"""
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        calls.append(payload)
        if len(calls) == 1:
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "call_runner_1",
                                        "type": "function",
                                        "function": {
                                            "name": "add",
                                            "arguments": '{"a": 2, "b": 3}',
                                        },
                                    }
                                ],
                            },
                            "finish_reason": "tool_calls",
                        }
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"content": "结果是 5"},
                        "finish_reason": "stop",
                    }
                ]
            },
        )

    provider = OpenAICompatibleProvider(
        api_key="sk-test",
        base_url="https://example.com/v1",
        model="test-model",
        transport=httpx.MockTransport(handler),
    )
    registry = ToolRegistry()
    registry.register(AddTool())
    result = await AgentRunner(provider=provider, tools=registry).run(
        AgentRunSpec(initial_messages=[{"role": "user", "content": "计算 2 + 3"}])
    )

    assert result.final_content == "结果是 5"
    assert result.tools_used == ["add"]
    assert result.stop_reason == "completed"
    assert calls[1]["messages"][-1]["role"] == "tool"
    assert calls[1]["messages"][-1]["tool_call_id"] == "call_runner_1"
    assert calls[1]["messages"][-1]["content"] == "5"


@pytest.mark.asyncio
async def test_openai_provider_wraps_socks_import_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """缺少 socksio 时应返回清晰的 ProviderError 提示。"""

    class BrokenAsyncClient:
        def __init__(self, **kwargs: object) -> None:
            raise ImportError("Using SOCKS proxy, but the 'socksio' package is not installed")

    monkeypatch.setattr(httpx, "AsyncClient", BrokenAsyncClient)
    provider = OpenAICompatibleProvider(
        api_key="sk-test",
        base_url="https://example.com/v1",
        model="test-model",
    )
    with pytest.raises(ProviderError, match="SOCKS"):
        await provider.chat([{"role": "user", "content": "hi"}])


def test_factory_unknown_provider_raises_configuration_error() -> None:
    """未知 Provider 必须使用文档承诺的 ProviderConfigurationError。"""
    with pytest.raises(ProviderConfigurationError, match="未知 Provider"):
        create_provider(ProviderConfig(provider="bogus", model="some-model"))


def test_factory_uses_preset_default_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """ProviderConfig 未指定模型时应回退到预设默认模型。"""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-deepseek-test")
    provider = create_provider(ProviderConfig(provider="deepseek"))
    assert isinstance(provider, OpenAICompatibleProvider)
    assert provider.model == "deepseek-chat"


@pytest.mark.asyncio
async def test_openai_reasoning_model_uses_max_completion_tokens() -> None:
    """o 系列 reasoning 模型不能接收 max_tokens 或自定义 temperature。"""
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["payload"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": "ok"}, "finish_reason": "stop"}
                ]
            },
        )

    provider = OpenAICompatibleProvider(
        api_key="sk-test",
        base_url="https://example.com/v1",
        model="o4-mini",
        transport=httpx.MockTransport(handler),
    )
    await provider.chat([{"role": "user", "content": "hi"}])

    payload = seen["payload"]
    assert isinstance(payload, dict)
    assert payload["max_completion_tokens"] == 1024
    assert "max_tokens" not in payload
    assert "temperature" not in payload


@pytest.mark.asyncio
async def test_anthropic_conversion_skips_empty_text_blocks() -> None:
    """Anthropic 请求中不能出现空 text block。"""
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["payload"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={"content": [{"type": "text", "text": "ok"}]},
        )

    provider = AnthropicProvider(
        api_key="sk-ant-test",
        base_url="https://api.anthropic.com/v1",
        model="claude-sonnet-4-5",
        transport=httpx.MockTransport(handler),
    )
    await provider.chat(
        [
            {"role": "user", "content": ""},
            {"role": "assistant", "content": ""},
            {"role": "user", "content": "hello"},
        ]
    )

    payload = seen["payload"]
    assert isinstance(payload, dict)
    assert payload["messages"] == [
        {"role": "user", "content": [{"type": "text", "text": "hello"}]}
    ]


def test_factory_supports_custom_anthropic_protocol_and_headers() -> None:
    """自定义配置可以覆盖协议并附加 HTTP 头。"""
    provider = create_provider(
        ProviderConfig(
            provider="custom",
            model="claude-compatible",
            base_url="https://gateway.example.com/v1",
            api_key="sk-test",
            protocol="anthropic",
            extra_headers={"X-Gateway": "mini-harness"},
        )
    )
    assert isinstance(provider, AnthropicProvider)
    assert provider.extra_headers["X-Gateway"] == "mini-harness"


@pytest.mark.asyncio
async def test_openai_provider_explains_missing_authorization_header() -> None:
    """上游返回缺少 Authorization 时应提示检查 API Key。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={"error": "Failed to get the authorization header."},
        )

    provider = OpenAICompatibleProvider(
        api_key="",
        base_url="https://gateway.example.com/v1",
        model="test-model",
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(ProviderError, match="Authorization") as exc_info:
        await provider.chat([{"role": "user", "content": "hello"}])
    assert "API Key" in str(exc_info.value)
