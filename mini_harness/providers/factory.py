"""Provider 工厂：把 ProviderConfig 转成可用的 LLMProvider 实例。"""

from __future__ import annotations

import os

from .anthropic import AnthropicProvider
from .base import LLMProvider, LLMResponse, ToolCallRequest
from .config import ProviderConfig
from .mock import MockProvider
from .openai_compat import OpenAICompatibleProvider
from .presets import ProviderPreset, get_preset


class ProviderConfigurationError(ValueError):
    """Provider 配置缺失或不合法时抛出。"""


class ProviderFactory:
    """面向对象形式的 Provider 工厂，便于依赖注入和扩展。

    ``create_provider`` 是等价的函数式入口；二者都根据 ``ProviderConfig``
    的 provider key 查找预设并构建具体适配器。
    """

    def create(self, config: ProviderConfig) -> LLMProvider:
        """创建 Provider 实例。"""
        return create_provider(config)


def resolve_api_key(
    preset: ProviderPreset,
    explicit_api_key: str = "",
) -> str:
    """解析 API Key：显式值优先，否则读取预设环境变量。

    Args:
        preset: Provider 预设。
        explicit_api_key: CLI 输入或调用方提供的 Key。

    Returns:
        解析到的 Key；本地服务允许为空。

    Raises:
        ProviderConfigurationError: Provider 要求 Key 但两者都为空。
    """
    if explicit_api_key.strip():
        return explicit_api_key.strip()
    for env_name in preset.env_keys:
        env_value = os.getenv(env_name, "").strip()
        if env_value:
            return env_value
    if preset.api_key_required:
        hint = f"并设置环境变量 {preset.env_key}" if preset.env_key else ""
        raise ProviderConfigurationError(
            f"{preset.display_name} 缺少 API Key。请在 CLI 中输入 Key {hint}。"
        )
    return ""


def resolve_base_url(
    preset: ProviderPreset,
    explicit_base_url: str = "",
) -> str:
    """解析 Base URL：显式值优先，否则使用预设默认值。"""
    base_url = explicit_base_url.strip() or preset.default_base_url.strip()
    if not base_url and preset.base_url_env:
        base_url = os.getenv(preset.base_url_env, "").strip()
    if not base_url:
        env_hint = (
            f"，或设置环境变量 {preset.base_url_env}"
            if preset.base_url_env
            else ""
        )
        raise ProviderConfigurationError(
            f"{preset.display_name} 需要 Base URL，请在 CLI 中填写{env_hint}。"
        )
    return base_url


def create_mock_provider() -> MockProvider:
    """创建用于离线演示/测试的 MockProvider。

    第一次响应请求 add(2, 3)，第二次读取工具结果并回答 ``"结果是 5"``。
    CLI 的 Mock 选项和 `python main.py --demo` 都使用该工厂。
    """
    return MockProvider(
        [
            LLMResponse(
                content=None,
                tool_calls=[
                    ToolCallRequest(
                        id="call_add_1",
                        name="add",
                        arguments={"a": 2, "b": 3},
                    )
                ],
                finish_reason="tool_calls",
            ),
            MockProvider.final_from_tool_result(prefix="结果是"),
        ]
    )


def create_provider(config: ProviderConfig) -> LLMProvider:
    """根据 ProviderConfig 创建 Provider。

    Args:
        config: Provider key、模型、可选 API Key 和 Base URL。

    Returns:
        可直接注入 AgentRunner 的 LLMProvider。

    Raises:
        ProviderConfigurationError: 未知 Provider、缺少模型/Base URL/API Key。

    上下游:
        交互式 CLI 在用户选择配置后调用；创建出的 Provider 被放入
        AgentRunner，后者负责多轮工具调用。
    """
    try:
        preset = get_preset(config.provider)
    except ValueError as exc:
        raise ProviderConfigurationError(str(exc)) from exc

    if preset.protocol == "mock":
        return create_mock_provider()

    protocol = (config.protocol or preset.protocol).strip().lower()
    if protocol not in {"openai", "anthropic"}:
        raise ProviderConfigurationError(
            f"不支持的 Provider 协议：{protocol}；可选 openai 或 anthropic。"
        )

    model = (config.model or "").strip() or preset.default_model
    if not model:
        raise ProviderConfigurationError(
            f"{preset.display_name} 需要模型 ID，请在 CLI 中选择或输入。"
        )

    base_url = resolve_base_url(preset, config.base_url)
    api_key = resolve_api_key(preset, config.api_key)
    extra_headers = {**dict(preset.extra_headers), **config.extra_headers}

    if protocol == "anthropic":
        return AnthropicProvider(
            api_key=api_key,
            base_url=base_url,
            model=model,
            temperature=config.temperature,
            max_tokens=config.max_output_tokens or config.max_tokens,
            timeout=config.timeout,
            extra_headers=extra_headers,
            provider_name=preset.display_name,
            context_window_tokens=config.context_window_tokens,
            thinking=config.thinking,
            reasoning_effort=config.reasoning_effort,
        )

    return OpenAICompatibleProvider(
        api_key=api_key,
        base_url=base_url,
        model=model,
        temperature=config.temperature,
        max_tokens=config.max_output_tokens or config.max_tokens,
        timeout=config.timeout,
        extra_headers=extra_headers,
        provider_name=preset.display_name,
        context_window_tokens=config.context_window_tokens,
        thinking=config.thinking,
        reasoning_effort=config.reasoning_effort,
    )
