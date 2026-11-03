"""Anthropic Messages API Provider。

Anthropic 与 OpenAI-compatible 协议不同：工具定义使用 input_schema，工具调用
与工具结果使用 content block，系统提示单独放在 system 字段。
"""

from __future__ import annotations

from typing import Any

import httpx

from .base import (
    LLMProvider,
    LLMResponse,
    Message,
    ProviderError,
    ToolCallRequest,
    ToolDefinition,
)


class AnthropicProvider(LLMProvider):
    """通过 HTTP 调用 Anthropic Messages API。"""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        temperature: float = 0.0,
        max_tokens: int = 4096,
        timeout: float = 60.0,
        extra_headers: dict[str, str] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        provider_name: str = "Anthropic",
    ) -> None:
        """保存 Anthropic 连接参数。

        Args:
            api_key: Anthropic API Key。
            base_url: API Base URL，默认 ``https://api.anthropic.com/v1``。
            model: Claude 模型 ID。
            temperature: 采样温度。
            max_tokens: 最大输出 token 数（Anthropic 必填）。
            timeout: 单次请求超时秒数。
            extra_headers: 额外 HTTP 头。
            transport: 测试用 httpx transport。
            provider_name: 错误信息中展示的名称。

        上下游:
            ProviderFactory 创建本对象，AgentRunner 调用 ``chat``。
        """
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout = timeout
        self.extra_headers = dict(extra_headers or {})
        self.transport = transport
        self.provider_name = provider_name

    @property
    def messages_url(self) -> str:
        """返回完整的 Messages API URL。"""
        if self.base_url.endswith("/messages"):
            return self.base_url
        return f"{self.base_url}/messages"

    def _headers(self) -> dict[str, str]:
        """构造 Anthropic 请求头。"""
        return {
            "content-type": "application/json",
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            **self.extra_headers,
        }

    def _convert_messages(
        self,
        messages: list[Message],
    ) -> tuple[str, list[dict[str, Any]]]:
        """把内部消息转换为 Anthropic system + messages 结构。"""
        system_parts: list[str] = []
        converted: list[dict[str, Any]] = []

        def append_user_blocks(blocks: list[dict[str, Any]]) -> None:
            """追加用户内容块；连续的用户消息会合并，避免空消息。"""
            if not blocks:
                return
            if converted and converted[-1].get("role") == "user":
                converted[-1]["content"].extend(blocks)
            else:
                converted.append({"role": "user", "content": blocks})

        def append_assistant_blocks(blocks: list[dict[str, Any]]) -> None:
            """追加助手内容块；没有有效内容时不发送空的 assistant 消息。"""
            if not blocks:
                return
            if converted and converted[-1].get("role") == "assistant":
                converted[-1]["content"].extend(blocks)
            else:
                converted.append({"role": "assistant", "content": blocks})

        for message in messages:
            role = str(message.get("role", "user"))
            content = message.get("content")

            if role == "system":
                if content:
                    system_parts.append(str(content))
                continue

            if role == "user":
                # Anthropic 不接受空 text block；空用户消息直接跳过。
                text = "" if content is None else str(content)
                if text:
                    append_user_blocks([{"type": "text", "text": text}])
                continue

            if role == "assistant":
                blocks: list[dict[str, Any]] = []
                text = "" if content is None else str(content)
                if text:
                    blocks.append({"type": "text", "text": text})
                for tool_call in message.get("tool_calls") or []:
                    arguments = tool_call.get("arguments", {})
                    if not isinstance(arguments, dict):
                        arguments = {}
                    blocks.append(
                        {
                            "type": "tool_use",
                            "id": tool_call.get("id", ""),
                            "name": tool_call.get("name", ""),
                            "input": arguments,
                        }
                    )
                # 空 assistant 消息会被 Anthropic 拒绝；跳过而不是补空文本。
                append_assistant_blocks(blocks)
                continue

            if role == "tool":
                tool_result = {
                    "type": "tool_result",
                    "tool_use_id": message.get("tool_call_id", ""),
                    "content": str(message.get("content", "")),
                }
                # Anthropic 要求同一轮的多个 tool_result 合并到一个 user
                # 消息中；如果上一条已经是纯 tool_result，就追加 block。
                if (
                    converted
                    and converted[-1].get("role") == "user"
                    and all(
                        isinstance(block, dict) and block.get("type") == "tool_result"
                        for block in converted[-1].get("content", [])
                    )
                ):
                    converted[-1]["content"].append(tool_result)
                else:
                    append_user_blocks([tool_result])

        return "\n\n".join(system_parts), converted

    def _convert_tools(
        self,
        tools: list[ToolDefinition] | None,
    ) -> list[dict[str, Any]]:
        """把 OpenAI function 定义转换为 Anthropic tool 定义。"""
        converted: list[dict[str, Any]] = []
        for tool in tools or []:
            function = tool.get("function", tool)
            converted.append(
                {
                    "name": function.get("name", ""),
                    "description": function.get("description", ""),
                    "input_schema": function.get(
                        "parameters",
                        {"type": "object", "properties": {}},
                    ),
                }
            )
        return converted

    async def chat(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
    ) -> LLMResponse:
        """调用 Anthropic Messages API。

        Args:
            messages: 内部完整消息历史。
            tools: ToolRegistry 导出的工具定义。

        Returns:
            解析后的 LLMResponse，包含 text 和 tool_use content block。

        上下游:
            AgentRunner._run_core await 本方法；ProviderError 由 CLI 捕获。
        """
        system, anthropic_messages = self._convert_messages(messages)
        payload: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "messages": anthropic_messages,
        }
        if system:
            payload["system"] = system
        anthropic_tools = self._convert_tools(tools)
        if anthropic_tools:
            payload["tools"] = anthropic_tools

        try:
            async with httpx.AsyncClient(
                timeout=self.timeout,
                transport=self.transport,
            ) as client:
                response = await client.post(
                    self.messages_url,
                    headers=self._headers(),
                    json=payload,
                )
        except ImportError as exc:
            raise ProviderError(
                "无法初始化 HTTP 客户端，可能是 SOCKS 代理依赖缺失。"
                "请运行 `uv sync`，或安装 `httpx[socks]` 后重试。"
            ) from exc
        except httpx.RequestError as exc:
            raise ProviderError(
                f"{self.provider_name} 网络请求失败：{exc}"
            ) from exc

        if response.status_code >= 400:
            raise ProviderError(self._format_http_error(response))

        try:
            data = response.json()
        except ValueError as exc:
            raise ProviderError(
                f"{self.provider_name} 返回了非 JSON 响应：{response.text[:300]}"
            ) from exc

        blocks = data.get("content") if isinstance(data, dict) else None
        if not isinstance(blocks, list):
            raise ProviderError(
                f"{self.provider_name} 响应缺少 content：{str(data)[:300]}"
            )

        text_parts: list[str] = []
        tool_calls: list[ToolCallRequest] = []
        for block in blocks:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                text_parts.append(str(block.get("text", "")))
            elif block.get("type") == "tool_use":
                arguments = block.get("input")
                tool_calls.append(
                    ToolCallRequest(
                        id=str(block.get("id", "")),
                        name=str(block.get("name", "")),
                        arguments=arguments if isinstance(arguments, dict) else {},
                    )
                )

        return LLMResponse(
            content="".join(text_parts) or None,
            tool_calls=tool_calls,
            finish_reason=str(data.get("stop_reason") or "stop"),
        )

    def _format_http_error(self, response: httpx.Response) -> str:
        """提取 Anthropic 错误响应中的可读信息。"""
        message = response.text[:500]
        try:
            body = response.json()
            error = body.get("error") if isinstance(body, dict) else None
            if isinstance(error, dict):
                message = str(error.get("message") or error)
            elif error:
                message = str(error)
        except ValueError:
            pass
        return f"{self.provider_name} 请求失败 (HTTP {response.status_code}): {message}"
