"""Provider 选择、推理设置与模型预算配置。

ProviderSettingsMixin 由 InteractiveCLI 使用，依赖其组装的 console/store 与运行配置；
不维护第二份连接、会话或配置状态。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, replace
from mini_harness.agent.context_types import ContextConfig
from mini_harness.providers import (
    LLMProvider,
    ProviderConfig,
    ProviderConfigurationError,
    ProviderPreset,
    create_provider,
    get_preset,
    grouped_presets,
)
from mini_harness.providers.config import DEFAULT_MAX_OUTPUT_TOKENS
from .profiles import ProviderProfile


@dataclass(slots=True)
class Selection:
    """CLI 当前选择的 Provider 配置。"""

    config: ProviderConfig
    preset: ProviderPreset
    profile_name: str | None = None


class _AbortSelection(Exception):
    """用户在配置菜单中取消选择。"""


class ProviderSettingsMixin:
    """共享 CLI 实例上的配置选择和更新行为。"""

    async def _select_config(self, *, force_menu: bool = False) -> Selection | None:
        """展示已有配置或创建新配置。

        Args:
            force_menu: 为 True 时即使存在默认配置也显示选择菜单，供
                ``/model`` 主动切换使用。
        """
        profiles = self.store.load()
        if not profiles:
            self.console.write("\n尚未保存配置，先创建一个运行配置。")
            return await self._create_config()

        if not force_menu:
            default = self.store.default_profile()
            if default is not None:
                try:
                    selection = await self._config_from_profile(default)
                except ProviderConfigurationError as exc:
                    self.console.write(f"默认配置不可用：{exc}")
                else:
                    self.console.write(
                        f"使用默认配置：{default.name} "
                        f"({default.provider} / {default.model})"
                    )
                    return selection

        default = self.store.default_profile()
        default_name = default.name if default is not None else ""
        while True:
            self.console.write("\n选择配置：")
            for index, profile in enumerate(profiles, start=1):
                marker = " [默认]" if profile.name == default_name else ""
                self.console.write(
                    f"  {index}. {profile.name} "
                    f"({profile.provider} / {profile.model}){marker}"
                )
            new_index = len(profiles) + 1
            self.console.write(f"  {new_index}. 新建配置")
            self.console.write("  q. 退出")

            answer = await self.console.read("请选择 [1]: ")
            answer = answer or "1"
            if answer.lower() in {"q", "quit", "exit"}:
                return None

            try:
                choice = int(answer)
            except ValueError:
                self.console.write("请输入有效序号。")
                continue

            if 1 <= choice <= len(profiles):
                try:
                    selection = await self._config_from_profile(
                        profiles[choice - 1]
                    )
                except ProviderConfigurationError as exc:
                    self.console.write(f"配置不可用：{exc}")
                    continue
                try:
                    self.store.set_default(profiles[choice - 1].name)
                except OSError as exc:
                    self.console.write(f"默认配置保存失败：{exc}")
                return selection
            if choice == new_index:
                return await self._create_config()
            self.console.write("序号超出范围，请重新选择。")

    async def _create_config(self) -> Selection:
        """交互式创建新配置，并默认保存为下次启动使用的配置。"""
        preset = await self._choose_preset()
        model = await self._choose_model(preset)
        base_url = await self._choose_base_url(preset)
        api_key = await self._choose_api_key(preset)

        config = ProviderConfig(
            provider=preset.key,
            model=model,
            api_key=api_key,
            base_url=base_url,
            max_output_tokens=(
                self.max_output_tokens
                if self.max_output_tokens is not None
                else DEFAULT_MAX_OUTPUT_TOKENS
            ),
            context_window_tokens=self.context_window_tokens,
            thinking=self.thinking,
            reasoning_effort=self.reasoning_effort,
        )
        if preset.protocol == "openai":
            effort = await self._choose_reasoning_effort(config)
            config = replace(
                config, reasoning_effort=effort,
                thinking=bool(effort) or (effort == config.reasoning_effort and config.thinking),
            )

        profile_name = None
        if preset.protocol != "mock":
            save = await self._confirm("保存为默认配置? [Y/n]: ", default=True)
            if save:
                default_name = f"{preset.display_name} / {model}"
                name = await self.console.read(f"配置名称 [{default_name}]: ")
                profile = ProviderProfile.from_config(
                    name or default_name,
                    config,
                )
                try:
                    self.store.upsert(profile, set_default=True)
                except OSError as exc:
                    self.console.write(f"配置保存失败，将继续使用当前配置：{exc}")
                else:
                    profile_name = profile.name
                    saved_hint = (
                        "（含 API Key）" if profile.api_key else ""
                    )
                    self.console.write(
                        f"已保存默认配置：{profile.name}{saved_hint}"
                    )

        return Selection(config=config, preset=preset, profile_name=profile_name)

    async def _config_from_profile(self, profile: ProviderProfile) -> Selection:
        """将保存的 profile 恢复为运行配置。"""
        try:
            preset = get_preset(profile.provider)
        except ValueError as exc:
            raise ProviderConfigurationError(str(exc)) from exc

        base_url = profile.base_url
        if not base_url and preset.requires_base_url:
            base_url = await self._choose_base_url(preset)
        elif not base_url:
            base_url = preset.default_base_url

        api_key = profile.api_key
        if not api_key:
            api_key = await self._choose_api_key(preset)
            if api_key:
                # 首次为旧 profile 输入 Key 后补写，下次启动即可直接复用。
                try:
                    self.store.upsert(
                        replace(profile, api_key=api_key),
                        set_default=True,
                    )
                except OSError as exc:
                    self.console.write(f"API Key 保存失败：{exc}")
        config = ProviderConfig(
            provider=profile.provider,
            model=profile.model,
            api_key=api_key,
            base_url=base_url,
            temperature=profile.temperature,
            max_output_tokens=profile.max_output_tokens,
            context_window_tokens=profile.context_window_tokens,
            thinking=profile.thinking,
            reasoning_effort=profile.reasoning_effort,
        )
        return Selection(
            config=self._apply_runtime_overrides(config),
            preset=preset,
            profile_name=profile.name,
        )

    async def _choose_reasoning_effort(self, config: ProviderConfig) -> str:
        """选择推理档位；回车保持当前值，0 恢复端点默认。"""
        model = config.model.lower().rsplit("/", 1)[-1]
        levels = (
            ("low", "high", "max")
            if model.startswith("deepseek-v4.1") or model == "deepseek-flash"
            else ("low", "medium", "high", "xhigh", "max")
        )
        self.console.write(f"\n推理强度设置（{config.model}）")
        self.console.write(f"当前：{config.reasoning_effort or '端点默认'}")
        self.console.write("  0. 端点默认")
        for index, level in enumerate(levels, start=1):
            self.console.write(f"  {index}. {level}")
        self.console.write("  c. 自定义档位（按模型端点支持的值填写）")
        self.console.write("  q. 取消")
        while True:
            answer = (await self.console.read("选择推理强度 [回车保留]: ")).strip().lower()
            if not answer:
                return config.reasoning_effort
            if answer in {"q", "quit"}:
                raise _AbortSelection
            if answer in {"0", "default"}:
                return ""
            if answer in levels:
                return answer
            if answer.isdigit() and 1 <= int(answer) <= len(levels):
                return levels[int(answer) - 1]
            if answer == "c":
                custom = (await self.console.read("自定义推理档位: ")).strip()
                if custom.lower() in {"q", "quit"}:
                    raise _AbortSelection
                if re.fullmatch(r"[A-Za-z0-9_-]+", custom):
                    return custom
            self.console.write("请选择有效序号或档位名称。")

    async def _edit_reasoning_settings(
        self, selection: Selection
    ) -> tuple[LLMProvider, Selection] | None:
        """保存当前 profile 的推理设置，重建 Provider 后立即生效。"""
        if (selection.config.protocol or selection.preset.protocol) != "openai":
            self.console.write("当前 Provider 适配器不支持推理强度设置。")
            return None
        try:
            effort = await self._choose_reasoning_effort(selection.config)
        except (_AbortSelection, EOFError):
            self.console.write("已取消推理设置。")
            return None
        config = replace(
            selection.config, reasoning_effort=effort,
            thinking=bool(effort) or (
                effort == selection.config.reasoning_effort and selection.config.thinking
            ),
        )
        try:
            provider = create_provider(config)
        except ProviderConfigurationError as exc:
            self.console.write_error(f"配置错误：{exc}")
            return None
        profile = next(
            (p for p in self.store.load() if p.name == selection.profile_name), None
        )
        if profile is not None:
            try:
                self.store.upsert(replace(profile, reasoning_effort=effort, thinking=config.thinking))
            except OSError as exc:
                self.console.write_error(f"推理设置保存失败，仅本次连接生效：{exc}")
            else:
                self.console.write(f"已保存推理设置：{profile.name}")
        else:
            self.console.write("推理设置已应用到本次连接（当前配置未保存）。")
        updated = replace(selection, config=config)
        self._activate_selection(updated)
        return provider, updated

    def _apply_runtime_overrides(self, config: ProviderConfig) -> ProviderConfig:
        """命令行能力选项也覆盖已保存配置，不能只在创建 profile 时生效。"""
        return replace(
            config,
            context_window_tokens=self.context_window_tokens or config.context_window_tokens,
            max_output_tokens=(
                self.max_output_tokens
                if self.max_output_tokens is not None
                else config.max_output_tokens
            ),
            thinking=self.thinking or config.thinking,
            reasoning_effort=self.reasoning_effort or config.reasoning_effort,
        )

    def _activate_selection(self, selection: Selection) -> None:
        """连接/切换模型后立即同步预算，首次 /context 也显示真实配置。"""
        self.context_config = self._effective_context_config(selection)
        self.context_builder.config = self.context_config
        if self._session_compactor is not None:
            self._session_compactor.context_config = self.context_config
        self.console.write(
            f"模型预算：上下文 {self.context_config.max_context_tokens} tokens，"
            f"输入预算 {self.context_config.available_input_tokens}，"
            f"最大输出 {selection.config.max_output_tokens}；"
            f"思考={'请求开启' if selection.config.thinking else '端点默认'}，"
            f"推理档位={selection.config.reasoning_effort or '端点默认'}"
        )
        if not selection.config.context_window_tokens and not self._max_context_tokens_override:
            self.console.write(
                "模型上下文能力未配置，当前使用 8K 回退值；"
                "可用 --context-window-tokens 指定端点支持的上限。"
            )

    async def _choose_preset(self) -> ProviderPreset:
        """展示分组 Provider 菜单并返回选择。"""
        self.console.write("\n选择 Provider：")
        numbered: list[ProviderPreset] = []
        for group_name, presets in grouped_presets():
            self.console.write(f"\n[{group_name}]")
            for preset in presets:
                numbered.append(preset)
                self.console.write(
                    f"  {len(numbered):2}. {preset.display_name} "
                    f"({preset.key})"
                )

        while True:
            answer = await self.console.read(f"请选择 Provider [1-{len(numbered)}]: ")
            if answer.lower() in {"q", "quit"}:
                raise _AbortSelection
            answer = answer or "1"
            try:
                choice = int(answer)
            except ValueError:
                self.console.write("请输入有效序号。")
                continue
            if 1 <= choice <= len(numbered):
                return numbered[choice - 1]
            self.console.write("序号超出范围，请重新选择。")

    async def _choose_model(self, preset: ProviderPreset) -> str:
        """展示模型建议并允许选择或输入自定义模型 ID。"""
        if not preset.models:
            while True:
                model = await self.console.read("模型 ID（必填）: ")
                if model:
                    return model
                self.console.write("模型 ID 不能为空。")

        self.console.write("\n选择模型：")
        default_index = 1
        if preset.default_model in preset.models:
            default_index = preset.models.index(preset.default_model) + 1
        for index, model in enumerate(preset.models, start=1):
            self.console.write(f"  {index}. {model}")
        self.console.write("  0. 自定义模型 ID")

        while True:
            answer = await self.console.read(f"请选择模型 [{default_index}]: ")
            answer = answer or str(default_index)
            try:
                choice = int(answer)
            except ValueError:
                self.console.write("请输入有效序号。")
                continue
            if choice == 0:
                model = await self.console.read("模型 ID: ")
                if model:
                    return model
                self.console.write("模型 ID 不能为空。")
                continue
            if 1 <= choice <= len(preset.models):
                return preset.models[choice - 1]
            self.console.write("序号超出范围，请重新选择。")

    async def _choose_base_url(self, preset: ProviderPreset) -> str:
        """选择或输入 Base URL。"""
        if preset.default_base_url:
            value = await self.console.read(
                f"Base URL [{preset.default_base_url}]: "
            )
            return value or preset.default_base_url

        if preset.base_url_env:
            env_base_url = os.getenv(preset.base_url_env, "").strip()
            if env_base_url:
                value = await self.console.read(
                    f"Base URL [{env_base_url}]: "
                )
                return value or env_base_url

        if not preset.requires_base_url:
            return ""

        while True:
            value = await self.console.read("Base URL（必填）: ")
            if value:
                return value
            self.console.write("Base URL 不能为空。")

    async def _choose_api_key(self, preset: ProviderPreset) -> str:
        """解析 API Key：环境变量优先，否则在当前会话中输入。"""
        if preset.protocol == "mock":
            return ""
        if not preset.api_key_required and not preset.env_keys:
            return ""

        if preset.env_keys:
            for env_name in preset.env_keys:
                env_value = os.getenv(env_name, "").strip()
                if env_value:
                    self.console.write(
                        f"检测到环境变量 {env_name}，将使用其中的 API Key。"
                    )
                    return ""

        if preset.api_key_required:
            env_label = preset.env_keys[0] if preset.env_keys else "必填"
            while True:
                key = await self.console.read_secret(f"API Key（{env_label}）: ")
                if key:
                    return key
                self.console.write("API Key 不能为空。")

        if preset.key == "custom":
            self.console.write(
                "提示：自定义端点如需鉴权，请填写 API Key；"
                "本地无鉴权服务可直接回车。"
            )
        return await self.console.read_secret("API Key（可留空）: ")

    async def _confirm(self, prompt: str, *, default: bool) -> bool:
        """读取 y/N 确认。"""
        answer = (await self.console.read(prompt)).lower()
        if not answer:
            return default
        return answer in {"y", "yes", "是"}

    def _effective_context_config(self, selection: Selection) -> ContextConfig:
        """按当前 profile 的模型能力解析本轮上下文预算。"""
        config = selection.config
        context_window = (
            self._max_context_tokens_override or config.context_window_tokens
        )
        max_output = config.max_output_tokens
        base = getattr(self, "_base_context_config", self.context_config)
        if context_window <= 0:
            return replace(
                base,
                reserve_output_tokens=self._reserve_output_tokens_override or base.reserve_output_tokens,
            )
        if max_output >= context_window:
            max_output = min(4096, max(1, context_window // 4))
        return ContextConfig(
            max_context_tokens=context_window,
            # 能力上限不等于每轮固定预留：实际输出由 Governor 按剩余
            # 窗口动态分配，否则 384K 最大输出会白白吃掉 384K 输入空间。
            reserve_output_tokens=self._reserve_output_tokens_override or min(max_output, 4096),
            include_agents_md=base.include_agents_md,
            agents_md_max_bytes=base.agents_md_max_bytes,
            safety_margin_tokens=base.safety_margin_tokens,
            compact_soft_ratio=base.compact_soft_ratio,
            max_tool_result_tokens=base.max_tool_result_tokens,
            enable_auto_compact=base.enable_auto_compact,
            memory_max_tokens=base.memory_max_tokens,
            idle_compact_after_minutes=(
                base.idle_compact_after_minutes
                if self._idle_compact_after_minutes is None
                else self._idle_compact_after_minutes
            ),
            idle_compact_check_interval_seconds=base.idle_compact_check_interval_seconds,
            dream_enabled=(
                base.dream_enabled
                if self._dream_enabled is None
                else self._dream_enabled
            ),
            dream_interval_hours=(
                base.dream_interval_hours
                if self._dream_interval_hours is None
                else self._dream_interval_hours
            ),
            dream_max_entries=base.dream_max_entries,
        )
