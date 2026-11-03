"""交互式 CLI 的本地配置存储。

保存 Provider、模型、Base URL 等配置；API Key 可在保存 profile 时一并写入
权限为 0600 的本地文件，用于下次启动直接复用。默认配置会随 profile 保存。
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from mini_harness.providers.config import ProviderConfig

DEFAULT_MAX_TOKENS = 4096
_LEGACY_MAX_TOKENS = 1024


@dataclass(frozen=True, slots=True)
class ProviderProfile:
    """可在 CLI 中复用的命名配置。

    ``api_key`` 默认不写入序列化结果；只有非空时才会保存到本地 0600 文件。
    """

    name: str
    provider: str
    model: str
    base_url: str = ""
    temperature: float = 0.0
    max_tokens: int = DEFAULT_MAX_TOKENS
    api_key: str = ""
    is_default: bool = False

    @classmethod
    def from_config(cls, name: str, config: ProviderConfig) -> "ProviderProfile":
        """从运行配置创建可保存的 profile，并保留本次输入的 API Key。"""
        return cls(
            name=name,
            provider=config.provider,
            model=config.model,
            base_url=config.base_url or "",
            temperature=config.temperature,
            max_tokens=config.max_tokens,
            api_key=config.api_key or "",
        )

    def to_config(self, api_key: str = "") -> ProviderConfig:
        """转换成运行配置；显式 Key 优先，否则使用 profile 中保存的 Key。"""
        return ProviderConfig(
            provider=self.provider,
            model=self.model,
            api_key=api_key or self.api_key,
            base_url=self.base_url,
            temperature=self.temperature,
            max_tokens=self.max_tokens,
        )


def default_profile_path() -> Path:
    """返回 profile 存储路径。

    可通过 ``MINI_HARNESS_CONFIG`` 指定完整文件路径，或通过
    ``MINI_HARNESS_HOME`` 指定目录；默认是 ``~/.mini-harness/config.json``。
    """
    explicit = os.getenv("MINI_HARNESS_CONFIG")
    if explicit:
        return Path(explicit).expanduser()
    home = os.getenv("MINI_HARNESS_HOME")
    base = Path(home).expanduser() if home else Path.home() / ".mini-harness"
    return base / "config.json"


class ProfileStore:
    """读写本地 Provider profile 的 JSON 存储。"""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_profile_path()

    def load(self) -> list[ProviderProfile]:
        """加载全部 profile；文件不存在或损坏时返回空列表。"""
        if not self.path.exists():
            return []
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return []

        items = payload.get("profiles") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            return []

        profiles: list[ProviderProfile] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            provider = item.get("provider")
            model = item.get("model")
            if not isinstance(name, str) or not name.strip():
                continue
            if not isinstance(provider, str) or not provider.strip():
                continue
            if not isinstance(model, str) or not model.strip():
                continue
            raw_base_url = item.get("base_url")
            base_url = "" if raw_base_url is None else str(raw_base_url)
            raw_api_key = item.get("api_key")
            api_key = (
                ""
                if raw_api_key is None
                else str(raw_api_key).replace("\r", "").replace("\n", "").strip()
            )
            try:
                raw_max_tokens = int(
                    item.get("max_tokens", DEFAULT_MAX_TOKENS)
                )
                # 旧版默认值是 1024；显式保存的旧配置也按旧默认值迁移，
                # 否则代码默认值改了但用户原来的 config.json 仍会被截断。
                max_tokens = (
                    DEFAULT_MAX_TOKENS
                    if raw_max_tokens == _LEGACY_MAX_TOKENS
                    else raw_max_tokens
                )
                profiles.append(
                    ProviderProfile(
                        name=name,
                        provider=provider,
                        model=model,
                        base_url=base_url,
                        temperature=float(item.get("temperature", 0.0)),
                        max_tokens=max_tokens,
                        api_key=api_key,
                        is_default=bool(item.get("is_default", False)),
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue
        return profiles

    def upsert(
        self,
        profile: ProviderProfile,
        *,
        set_default: bool = False,
    ) -> None:
        """按 name 新增或替换 profile，并原子写回磁盘。

        Args:
            profile: 要保存的 profile。
            set_default: 是否同时把它设为启动默认配置。
        """
        profiles = self.load()
        existing = next(
            (item for item in profiles if item.name == profile.name),
            None,
        )
        if existing is not None and not set_default:
            profile = replace(profile, is_default=existing.is_default)
        if set_default:
            profiles = [
                replace(item, is_default=False)
                for item in profiles
            ]
            profile = replace(profile, is_default=True)
        updated = [item for item in profiles if item.name != profile.name]
        updated.append(profile)
        # 保持稳定顺序：默认配置优先，其余按名称排序。
        updated.sort(key=lambda item: (not item.is_default, item.name))
        self._save_all(updated)

    def set_default(self, name: str) -> bool:
        """把指定 profile 设为默认；不存在时返回 False。"""
        profiles = self.load()
        if not any(item.name == name for item in profiles):
            return False
        updated = [
            replace(item, is_default=(item.name == name))
            for item in profiles
        ]
        updated.sort(key=lambda item: (not item.is_default, item.name))
        self._save_all(updated)
        return True

    def default_profile(self) -> ProviderProfile | None:
        """返回默认 profile；没有显式默认时兼容只有一个 profile 的旧配置。"""
        profiles = self.load()
        for profile in profiles:
            if profile.is_default:
                return profile
        if len(profiles) == 1:
            return profiles[0]
        return None

    def delete(self, name: str) -> bool:
        """删除指定 profile；返回是否删除了内容。"""
        profiles = self.load()
        remaining = [item for item in profiles if item.name != name]
        if len(remaining) == len(profiles):
            return False
        self._save_all(remaining)
        return True

    def _save_all(self, profiles: list[ProviderProfile]) -> None:
        """原子写入全部 profiles；空 API Key 不写入文件。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        serialized: list[dict[str, object]] = []
        for profile in profiles:
            item = asdict(profile)
            if not item["api_key"]:
                item.pop("api_key", None)
            if not item["is_default"]:
                item.pop("is_default", None)
            serialized.append(item)
        payload = {
            "version": 1,
            "profiles": serialized,
        }
        temp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        temp_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        try:
            temp_path.chmod(0o600)
        except OSError:
            pass
        temp_path.replace(self.path)
