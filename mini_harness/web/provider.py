"""Phase 4.3：Web 服务加载 CLI 中保存的默认 Provider 配置。"""

from __future__ import annotations

from dataclasses import dataclass

from mini_harness.cli.profiles import ProfileStore, ProfileStoreError, ProviderProfile
from mini_harness.providers import (
    LLMProvider,
    ProviderConfigurationError,
    create_provider,
)


@dataclass(frozen=True, slots=True)
class ArchitectureProviderSelection:
    """Web 启动时选中的 Provider 及不可用原因。"""

    provider: LLMProvider | None
    profile_name: str = ""
    error: str = ""


def load_architecture_provider(
    profile_name: str | None = None,
    *,
    store: ProfileStore | None = None,
) -> ArchitectureProviderSelection:
    """从 ``~/.mini-harness/config.json`` 选择并创建 Provider。

    选择失败时不会阻止 ``mini-bot web`` 提供代码结构浏览；只有架构生成
    接口返回明确的配置错误。API Key 不进入返回值或日志。
    """
    store = store or ProfileStore()
    try:
        profiles = store.load()
    except ProfileStoreError as exc:
        return ArchitectureProviderSelection(
            provider=None,
            error=f"读取 Provider 配置失败：{exc}",
        )

    if profile_name:
        profile = next(
            (item for item in profiles if item.name == profile_name),
            None,
        )
        if profile is None:
            names = "、".join(item.name for item in profiles) or "无"
            return ArchitectureProviderSelection(
                provider=None,
                error=(
                    f"未找到 Provider profile：{profile_name}；"
                    f"当前可用：{names}"
                ),
            )
    else:
        profile = store.default_profile()
        if profile is None:
            return ArchitectureProviderSelection(
                provider=None,
                error=(
                    "尚未选择默认 Provider profile。请先运行 mini-bot 完成配置，"
                    "或使用 --architecture-profile 指定已有 profile。"
                ),
            )

    try:
        provider = create_provider(profile.to_config())
    except (ProviderConfigurationError, ValueError) as exc:
        return ArchitectureProviderSelection(
            provider=None,
            profile_name=profile.name,
            error=f"创建 Provider {profile.name!r} 失败：{exc}",
        )
    return ArchitectureProviderSelection(
        provider=provider,
        profile_name=profile.name,
    )


__all__ = [
    "ArchitectureProviderSelection",
    "load_architecture_provider",
]
