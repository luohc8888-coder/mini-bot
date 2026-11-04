"""Provider 运行时配置。"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class ProviderConfig:
    """创建 Provider 所需的全部运行参数。

    Args:
        provider: Provider 预设 key，例如 ``"deepseek"``、``"openai"``。
        model: 发送给模型的 model ID；为空时使用预设默认模型。
        api_key: 已解析的 API Key；为空时 factory 会尝试读取预设环境变量。
        base_url: 覆盖预设的 Base URL；为空时使用预设默认值。
        temperature: 采样温度。
        max_tokens: 兼容旧配置的输出上限；未配置 ``max_output_tokens`` 时生效。
        max_output_tokens: 模型/端点支持的最大输出 token 数；0 表示沿用
            ``max_tokens``，可显式设为端点确认的上限（如 393216）。
        context_window_tokens: 端点与模型的有效上下文窗口；0 表示由
            ContextConfig 决定。用于按 ``C - I - M`` 计算动态输出额度。
        timeout: HTTP 请求超时（秒）。
        thinking: 是否向端点请求 thinking/reasoning 模式。
        reasoning_effort: 推理档位，例如 ``high``/``max``；空字符串不发送。
        extra_headers: 追加到预设头部的自定义 HTTP 头，用于特殊代理/网关。
        protocol: 可选协议覆盖，支持 ``"openai"`` / ``"anthropic"``；为空时
            使用预设自身协议，主要用于自定义 Anthropic-compatible 端点。

    Returns:
        传给 ``create_provider`` 的配置对象。

    上下游:
        交互式 CLI、环境变量或未来的配置文件产生该对象；ProviderFactory
        根据它构建 OpenAI-compatible 或 Anthropic Provider。
    """

    provider: str
    model: str = ""
    api_key: str = ""
    base_url: str = ""
    temperature: float = 0.0
    max_tokens: int = 4096
    max_output_tokens: int = 0
    context_window_tokens: int = 0
    timeout: float = 60.0
    thinking: bool = False
    reasoning_effort: str = ""
    extra_headers: dict[str, str] = field(default_factory=dict)
    protocol: str = ""
