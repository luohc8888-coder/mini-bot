"""Provider 厂商预设目录。

每个预设描述一个供应商的协议、默认 Base URL、API Key 环境变量和模型建议。
模型列表只是选择菜单中的建议值，用户在 CLI 中始终可以输入自定义模型 ID。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

ProviderProtocol = Literal["openai", "anthropic", "mock"]


@dataclass(frozen=True, slots=True)
class ProviderPreset:
    """一个 Provider 厂商/接入方式的静态配置。"""

    key: str
    display_name: str
    protocol: ProviderProtocol
    env_key: str
    default_model: str
    models: tuple[str, ...] = ()
    default_base_url: str = ""
    requires_base_url: bool = False
    api_key_required: bool = True
    description: str = ""
    extra_headers: tuple[tuple[str, str], ...] = ()
    env_aliases: tuple[str, ...] = ()
    base_url_env: str = ""

    @property
    def env_keys(self) -> tuple[str, ...]:
        """返回用于查找 API Key 的环境变量名列表。"""
        if not self.env_key:
            return self.env_aliases
        return (self.env_key, *self.env_aliases)


PRESETS: dict[str, ProviderPreset] = {
    "mock": ProviderPreset(
        key="mock",
        display_name="Mock（离线演示）",
        protocol="mock",
        env_key="",
        default_model="mock-add-demo",
        models=("mock-add-demo",),
        api_key_required=False,
        description="无需网络和 API Key，固定演示 add(2, 3) 工具闭环",
    ),
    "deepseek": ProviderPreset(
        key="deepseek",
        display_name="DeepSeek",
        protocol="openai",
        env_key="DEEPSEEK_API_KEY",
        default_model="deepseek-chat",
        models=("deepseek-chat", "deepseek-reasoner"),
        default_base_url="https://api.deepseek.com/v1",
        description="DeepSeek 官方 OpenAI-compatible API",
    ),
    "qwen": ProviderPreset(
        key="qwen",
        display_name="阿里云百炼 / Qwen",
        protocol="openai",
        env_key="DASHSCOPE_API_KEY",
        default_model="qwen-plus",
        models=("qwen-plus", "qwen-max", "qwen-turbo"),
        default_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        description="DashScope OpenAI-compatible API",
    ),
    "zhipu": ProviderPreset(
        key="zhipu",
        display_name="智谱 GLM",
        protocol="openai",
        env_key="ZHIPUAI_API_KEY",
        default_model="glm-4-plus",
        models=("glm-4-plus", "glm-4-flash", "glm-4.5"),
        default_base_url="https://open.bigmodel.cn/api/paas/v4",
        description="智谱开放平台 OpenAI-compatible API",
        env_aliases=("ZAI_API_KEY",),
    ),
    "moonshot": ProviderPreset(
        key="moonshot",
        display_name="Moonshot / Kimi",
        protocol="openai",
        env_key="MOONSHOT_API_KEY",
        default_model="moonshot-v1-8k",
        models=("moonshot-v1-8k", "moonshot-v1-32k", "kimi-k2.5"),
        default_base_url="https://api.moonshot.ai/v1",
        description="Moonshot OpenAI-compatible API",
    ),
    "kimi-coding": ProviderPreset(
        key="kimi-coding",
        display_name="Kimi Coding",
        protocol="anthropic",
        env_key="KIMI_CODING_API_KEY",
        default_model="kimi-k2.5",
        models=("kimi-k2.5", "kimi-k2.7", "kimi-k2.7-code"),
        default_base_url="https://api.kimi.com/coding/v1",
        description="Kimi Coding 的 Anthropic Messages 兼容入口",
        extra_headers=(("User-Agent", "claude-code/0.1.0"),),
    ),
    "modelscope": ProviderPreset(
        key="modelscope",
        display_name="魔搭 ModelScope",
        protocol="openai",
        env_key="MODELSCOPE_API_KEY",
        default_model="Qwen/Qwen3-235B-A22B",
        models=("Qwen/Qwen3-235B-A22B", "deepseek-ai/DeepSeek-V3"),
        default_base_url="https://api-inference.modelscope.cn/v1",
        description="ModelScope OpenAI-compatible 推理 API",
    ),
    "minimax": ProviderPreset(
        key="minimax",
        display_name="MiniMax",
        protocol="openai",
        env_key="MINIMAX_API_KEY",
        default_model="MiniMax-M3.1-Flash",
        models=("MiniMax-M3.1-Flash", "MiniMax-M2"),
        default_base_url="https://api.minimax.io/v1",
        description="MiniMax OpenAI-compatible API",
    ),
    "huawei-maas": ProviderPreset(
        key="huawei-maas",
        display_name="Huawei Cloud MaaS",
        protocol="openai",
        env_key="HUAWEI_MAAS_API_KEY",
        default_model="deepseek-v4.1-flash-1017",
        models=(
            "DeepSeek-v4.1-Flash",
            "GLM-5.3",
            "Qwen-300",
            "deepseek-v4.1-flash-1017",
        ),
        requires_base_url=True,
        description="华为云 MaaS；Base URL 以控制台提供的 OpenAI-compatible 地址为准",
        base_url_env="HUAWEI_MAAS_BASE_URL",
    ),
    "huawei-maas-direct": ProviderPreset(
        key="huawei-maas-direct",
        display_name="Huawei MaaS Direct",
        protocol="openai",
        env_key="HUAWEI_MAAS_DIRECT_API_KEY",
        default_model="deepseek-v4.1-flash-1017",
        models=(
            "DeepSeek-v4.1-Flash",
            "GLM-5.3",
            "Qwen-300",
            "deepseek-v4.1-flash-1017",
        ),
        requires_base_url=True,
        description="华为云 MaaS 直连；Base URL 以控制台或网关提供地址为准",
        base_url_env="HUAWEI_MAAS_DIRECT_BASE_URL",
    ),
    "openai": ProviderPreset(
        key="openai",
        display_name="OpenAI",
        protocol="openai",
        env_key="OPENAI_API_KEY",
        default_model="gpt-4.1-mini",
        models=("gpt-4.1-mini", "gpt-4.1", "gpt-4o-mini", "o4-mini"),
        default_base_url="https://api.openai.com/v1",
        description="OpenAI 官方 API",
    ),
    "anthropic": ProviderPreset(
        key="anthropic",
        display_name="Anthropic Claude",
        protocol="anthropic",
        env_key="ANTHROPIC_API_KEY",
        default_model="claude-sonnet-4-5",
        models=("claude-sonnet-4-5", "claude-opus-4-1", "claude-haiku-4-5"),
        default_base_url="https://api.anthropic.com/v1",
        description="Anthropic Messages API",
    ),
    "gemini": ProviderPreset(
        key="gemini",
        display_name="Google Gemini",
        protocol="openai",
        env_key="GEMINI_API_KEY",
        default_model="gemini-2.5-flash",
        models=("gemini-2.5-flash", "gemini-2.5-pro"),
        default_base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        description="Google Gemini OpenAI-compatible API",
    ),
    "xai": ProviderPreset(
        key="xai",
        display_name="xAI Grok",
        protocol="openai",
        env_key="XAI_API_KEY",
        default_model="grok-3-mini",
        models=("grok-3-mini", "grok-3"),
        default_base_url="https://api.x.ai/v1",
        description="xAI OpenAI-compatible API",
    ),
    "mistral": ProviderPreset(
        key="mistral",
        display_name="Mistral AI",
        protocol="openai",
        env_key="MISTRAL_API_KEY",
        default_model="mistral-large-latest",
        models=("mistral-large-latest", "mistral-small-latest"),
        default_base_url="https://api.mistral.ai/v1",
        description="Mistral OpenAI-compatible API",
    ),
    "groq": ProviderPreset(
        key="groq",
        display_name="Groq",
        protocol="openai",
        env_key="GROQ_API_KEY",
        default_model="llama-3.3-70b-versatile",
        models=("llama-3.3-70b-versatile", "llama-3.1-8b-instant"),
        default_base_url="https://api.groq.com/openai/v1",
        description="Groq OpenAI-compatible API",
    ),
    "openrouter": ProviderPreset(
        key="openrouter",
        display_name="OpenRouter",
        protocol="openai",
        env_key="OPENROUTER_API_KEY",
        default_model="openai/gpt-4.1-mini",
        models=(
            "openai/gpt-4.1-mini",
            "anthropic/claude-sonnet-4.5",
            "deepseek/deepseek-chat",
        ),
        default_base_url="https://openrouter.ai/api/v1",
        description="OpenRouter 多模型聚合网关",
    ),
    "siliconflow": ProviderPreset(
        key="siliconflow",
        display_name="硅基流动 SiliconFlow",
        protocol="openai",
        env_key="SILICONFLOW_API_KEY",
        default_model="deepseek-ai/DeepSeek-V3",
        models=(
            "deepseek-ai/DeepSeek-V3",
            "Qwen/Qwen3-235B-A22B",
            "moonshotai/Kimi-K2-Instruct",
        ),
        default_base_url="https://api.siliconflow.cn/v1",
        description="SiliconFlow OpenAI-compatible API",
    ),
    "aihubmix": ProviderPreset(
        key="aihubmix",
        display_name="AIHubMix",
        protocol="openai",
        env_key="AIHUBMIX_API_KEY",
        default_model="gpt-4.1-mini",
        models=("gpt-4.1-mini", "claude-sonnet-4-5", "deepseek-chat"),
        default_base_url="https://aihubmix.com/v1",
        description="AIHubMix 多模型聚合网关",
    ),
    "novita": ProviderPreset(
        key="novita",
        display_name="Novita AI",
        protocol="openai",
        env_key="NOVITA_API_KEY",
        default_model="deepseek/deepseek-chat",
        models=("deepseek/deepseek-chat", "meta-llama/llama-3.3-70b-instruct"),
        default_base_url="https://api.novita.ai/openai",
        description="Novita AI OpenAI-compatible API",
    ),
    "ollama": ProviderPreset(
        key="ollama",
        display_name="Ollama（本地）",
        protocol="openai",
        env_key="",
        default_model="llama3.2",
        models=("llama3.2", "qwen2.5:7b", "deepseek-r1:7b"),
        default_base_url="http://localhost:11434/v1",
        api_key_required=False,
        description="本地 Ollama OpenAI-compatible 服务，无需 API Key",
    ),
    "lm-studio": ProviderPreset(
        key="lm-studio",
        display_name="LM Studio（本地）",
        protocol="openai",
        env_key="",
        default_model="local-model",
        models=("local-model",),
        default_base_url="http://localhost:1234/v1",
        api_key_required=False,
        description="本地 LM Studio OpenAI-compatible 服务，无需 API Key",
    ),
    "custom": ProviderPreset(
        key="custom",
        display_name="自定义 OpenAI-Compatible",
        protocol="openai",
        env_key="MINI_HARNESS_API_KEY",
        default_model="",
        models=(),
        requires_base_url=True,
        api_key_required=False,
        description="任意 OpenAI-compatible API；Base URL 和模型 ID 手动输入",
        base_url_env="MINI_HARNESS_BASE_URL",
    ),
}


PRESET_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("快速体验", ("mock",)),
    (
        "国内厂商",
        (
            "deepseek",
            "qwen",
            "zhipu",
            "moonshot",
            "kimi-coding",
            "modelscope",
            "minimax",
            "huawei-maas",
            "huawei-maas-direct",
        ),
    ),
    ("国际厂商", ("openai", "anthropic", "gemini", "xai", "mistral", "groq")),
    ("第三方平台", ("openrouter", "siliconflow", "aihubmix", "novita")),
    ("本地 / 自定义", ("ollama", "lm-studio", "custom")),
)


def get_preset(key: str) -> ProviderPreset:
    """按 key 获取 Provider 预设；不存在时抛出 ValueError。"""
    try:
        return PRESETS[key]
    except KeyError as exc:
        available = ", ".join(PRESETS)
        raise ValueError(f"未知 Provider '{key}'。可用值：{available}") from exc


def grouped_presets() -> list[tuple[str, list[ProviderPreset]]]:
    """返回按展示分组排列的 Provider 预设。

    这个函数供交互式 CLI 菜单使用；缺失分组中的 key 会被明确报错，便于
    新增预设时及时补齐菜单。
    """
    groups: list[tuple[str, list[ProviderPreset]]] = []
    for group_name, keys in PRESET_GROUPS:
        groups.append((group_name, [get_preset(key) for key in keys]))
    return groups
