"""OpenAI Chat Completions-compatible HTTP Provider。

DeepSeek、Qwen、GLM、Moonshot、OpenRouter、SiliconFlow、Ollama 等大量厂商
都提供这一协议，因此一个适配器即可覆盖多数国内外供应商与第三方聚合平台。
"""

from __future__ import annotations

import json
from urllib.parse import urlsplit
from typing import Any

import httpx

from .config import DEFAULT_MAX_OUTPUT_TOKENS, validate_max_output_tokens
from .base import (
    LLMProvider,
    LLMResponse,
    Message,
    ProviderError,
    TextDeltaCallback,
    ToolCallRequest,
    ToolDefinition,
)
from .retry import (
    RetryPolicy,
    RetryableStatusError,
    response_retry_after,
    with_retries,
)


def _normalize_content(content: Any) -> str | None:
    """把 Provider 返回的 content 归一化为文本。"""
    if content is None:
        return None
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") in (None, "text"):
                parts.append(str(block.get("text", "")))
        return "".join(parts) or None
    return str(content)


def _parse_tool_arguments(raw_arguments: Any) -> dict[str, Any]:
    """把 OpenAI 的 JSON 字符串参数解析为 dict。

    非法 JSON 不直接让调用崩溃，而是保留到 ``_raw`` 中；ToolRegistry 会
    返回参数校验错误，让模型有机会在下一轮修正。
    """
    if raw_arguments is None:
        return {}
    if isinstance(raw_arguments, dict):
        return raw_arguments
    if isinstance(raw_arguments, str):
        try:
            parsed = json.loads(raw_arguments)
        except json.JSONDecodeError:
            return {"_raw": raw_arguments}
        return parsed if isinstance(parsed, dict) else {"_raw": parsed}
    return {"_raw": raw_arguments}


class OpenAICompatibleProvider(LLMProvider):
    """通过 HTTP 调用 OpenAI-compatible ``/chat/completions`` 接口。"""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        temperature: float = 0.0,
        max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
        timeout: float = 60.0,
        extra_headers: dict[str, str] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        provider_name: str = "OpenAI-compatible",
        context_window_tokens: int = 0,
        thinking: bool = False,
        reasoning_effort: str = "",
        retry_policy: RetryPolicy | None = None,
    ) -> None:
        """保存连接参数。

        Args:
            api_key: API Key；Ollama 等本地服务可为空。
            base_url: API Base URL，例如 ``https://api.deepseek.com/v1``。
            model: 模型 ID。
            temperature: 采样温度。
            max_output_tokens: 配置的最大输出额度；请求额度可由预算治理调低。
            timeout: 单次请求超时秒数。
            extra_headers: 额外 HTTP 头。
            transport: 测试用 httpx transport；生产环境保持 None。
            provider_name: 错误信息中展示的厂商名称。
            retry_policy: 瞬时错误的重试策略；None 使用默认 3 次尝试。

        上下游:
            create_provider 创建本对象，AgentRunner 每轮调用 ``chat``。
        """
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.temperature = temperature
        validate_max_output_tokens(max_output_tokens)
        self.max_output_tokens = max_output_tokens
        self.timeout = timeout
        self.extra_headers = dict(extra_headers or {})
        self.transport = transport
        self.provider_name = provider_name
        self.context_window_tokens = context_window_tokens
        self.thinking = thinking
        self.reasoning_effort = reasoning_effort
        # 架构分析等结构化输出场景可显式关闭 Qwen 系端点的 thinking，
        # 避免 reasoning_tokens 耗尽 max_tokens 后 content 为空。
        self.request_thinking_disabled = False
        self.retry_policy = retry_policy if retry_policy is not None else RetryPolicy()

    @property
    def chat_url(self) -> str:
        """返回完整的 chat completions URL。"""
        if self.base_url.endswith("/chat/completions"):
            return self.base_url
        return f"{self.base_url}/chat/completions"

    def _headers(self) -> dict[str, str]:
        """构造请求头；无 API Key 的本地服务不发送 Authorization。"""
        headers = {
            "Content-Type": "application/json",
            **self.extra_headers,
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _uses_reasoning_parameters(self) -> bool:
        """判断是否属于需要 reasoning 参数的 OpenAI 模型。

        OpenAI 的 o1/o3/o4 与 gpt-5 系列通常使用 ``max_completion_tokens``，
        且不接受自定义 ``temperature``。第三方模型即使名称相似也不一定
        遵循该规则，因此仅对明确的 OpenAI reasoning 前缀做适配。
        """
        return self.model.lower().startswith(("o1", "o3", "o4", "gpt-5"))

    def _to_openai_message(self, message: Message) -> dict[str, Any]:
        """把内部简化消息转换为 OpenAI Chat Completions 消息。"""
        role = str(message.get("role", "user"))
        if role in {"system", "user"}:
            return {
                "role": role,
                "content": message.get("content", ""),
            }

        if role == "assistant":
            content = message.get("content")
            converted: dict[str, Any] = {
                "role": "assistant",
                "content": content,
            }
            tool_calls = message.get("tool_calls") or []
            if tool_calls:
                converted["tool_calls"] = [
                    self._to_openai_tool_call(tool_call)
                    for tool_call in tool_calls
                ]
            if converted["content"] is None and not tool_calls:
                converted["content"] = ""
            if message.get("reasoning_content") is not None:
                converted["reasoning_content"] = message["reasoning_content"]
            return converted

        if role == "tool":
            return {
                "role": "tool",
                "tool_call_id": message.get("tool_call_id", ""),
                "content": str(message.get("content", "")),
            }

        return {
            "role": role,
            "content": message.get("content", ""),
        }

    @staticmethod
    def _to_openai_tool_call(tool_call: dict[str, Any]) -> dict[str, Any]:
        """把内部 tool_call 转为 OpenAI function call 结构。"""
        if "function" in tool_call:
            function = dict(tool_call["function"])
            return {
                "id": tool_call.get("id", ""),
                "type": "function",
                "function": function,
            }

        arguments = tool_call.get("arguments", {})
        if not isinstance(arguments, str):
            arguments = json.dumps(arguments, ensure_ascii=False)
        return {
            "id": tool_call.get("id", ""),
            "type": "function",
            "function": {
                "name": tool_call.get("name", ""),
                "arguments": arguments,
            },
        }

    def _build_payload(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None,
        *,
        stream: bool = False,
    ) -> dict[str, Any]:
        """构造普通/流式请求共用的 payload。"""
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [self._to_openai_message(message) for message in messages],
        }
        if self._uses_reasoning_parameters():
            payload["max_completion_tokens"] = self.output_token_limit
        else:
            payload["temperature"] = self.temperature
            payload["max_tokens"] = self.output_token_limit
        if tools:
            payload["tools"] = tools
        host = urlsplit(self.base_url).hostname or ""
        is_qwen_host = (
            host == "maas.qianwenaiapi.com"
            or host == "dashscope.aliyuncs.com"
            or (host.endswith(".aliyuncs.com") and host.startswith("dashscope"))
        )
        if self.request_thinking_disabled and is_qwen_host:
            payload["enable_thinking"] = False
        elif self.thinking:
            if is_qwen_host:
                payload["enable_thinking"] = True
            else:
                payload["thinking"] = {"type": "enabled"}
        if self.reasoning_effort:
            payload["reasoning_effort"] = self.reasoning_effort
        if stream:
            payload["stream"] = True
        return payload

    def _raise_for_status(self, response: httpx.Response) -> None:
        """把常见 HTTP 错误转换为带上下文的 ProviderError。"""
        if response.status_code < 400:
            return
        body = response.text
        body_lower = body.lower()
        if response.status_code in {400, 401, 403} and (
            "authorization header" in body_lower
            or "authorization" in body_lower
            or "api key" in body_lower
            or "unauthorized" in body_lower
        ):
            raise ProviderError(
                f"{self.provider_name} 鉴权失败（HTTP {response.status_code}）："
                "服务端没有收到有效的 Authorization 头。"
                "请检查 API Key 是否已填写，或设置对应环境变量"
                "（自定义 Provider 使用 MINI_HARNESS_API_KEY）；"
                "如果端点无需鉴权，可留空。\n"
                f"服务端响应：{body[:300]}"
            )
        raise ProviderError(self._format_http_error(response))

    @staticmethod
    def _build_tool_calls(
        buffers: dict[int, dict[str, str]],
    ) -> list[ToolCallRequest]:
        """把流式分片的 tool_calls 缓冲转换为 ToolCallRequest。"""
        tool_calls: list[ToolCallRequest] = []
        for index in sorted(buffers):
            buffer = buffers[index]
            tool_calls.append(
                ToolCallRequest(
                    id=buffer.get("id") or f"call_{index}",
                    name=buffer.get("name", ""),
                    arguments=_parse_tool_arguments(buffer.get("arguments", "")),
                )
            )
        return tool_calls

    async def chat(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
    ) -> LLMResponse:
        """调用 OpenAI-compatible Chat Completions 接口。

        Args:
            messages: 完整内部消息历史（user/assistant/tool）。
            tools: ToolRegistry 导出的 OpenAI function 定义。

        Returns:
            解析后的 LLMResponse；工具调用参数已经转换为 dict。

        上下游:
            AgentRunner._run_core await 本方法；出错时抛出 ProviderError，
            由交互式 CLI 或上层调用方捕获。
        """
        payload = self._build_payload(messages, tools)

        try:
            async with httpx.AsyncClient(
                timeout=self.timeout,
                transport=self.transport,
            ) as client:

                async def send() -> httpx.Response:
                    """发送一次请求；可重试状态码交给 with_retries 处理。"""
                    response = await client.post(
                        self.chat_url,
                        headers=self._headers(),
                        json=payload,
                    )
                    if response.status_code in self.retry_policy.retry_status_codes:
                        raise RetryableStatusError(
                            self._format_http_error(response),
                            response_retry_after(response),
                        )
                    return response

                response = await with_retries(
                    send,
                    policy=self.retry_policy,
                    provider_name=self.provider_name,
                )
        except ImportError as exc:
            raise ProviderError(
                "无法初始化 HTTP 客户端，可能是 SOCKS 代理依赖缺失。"
                "请运行 `uv sync`，或安装 `httpx[socks]` 后重试。"
            ) from exc

        self._raise_for_status(response)

        try:
            data = response.json()
        except ValueError as exc:
            raise ProviderError(
                f"{self.provider_name} 返回了非 JSON 响应：{response.text[:300]}"
            ) from exc

        choices = data.get("choices") if isinstance(data, dict) else None
        if not isinstance(choices, list) or not choices:
            raise ProviderError(
                f"{self.provider_name} 响应缺少 choices：{str(data)[:300]}"
            )

        choice = choices[0] if isinstance(choices[0], dict) else {}
        message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
        tool_calls: list[ToolCallRequest] = []
        for index, raw_call in enumerate(message.get("tool_calls") or []):
            if not isinstance(raw_call, dict):
                continue
            function = raw_call.get("function")
            if not isinstance(function, dict):
                function = {}
            tool_calls.append(
                ToolCallRequest(
                    id=str(raw_call.get("id") or f"call_{index}"),
                    name=str(function.get("name", "")),
                    arguments=_parse_tool_arguments(function.get("arguments")),
                )
            )

        return LLMResponse(
            content=_normalize_content(message.get("content")),
            tool_calls=tool_calls,
            finish_reason=str(choice.get("finish_reason") or "stop"),
            reasoning_content=(
                str(message["reasoning_content"])
                if message.get("reasoning_content") is not None
                else None
            ),
            usage=(
                data.get("usage")
                if isinstance(data.get("usage"), dict)
                else {}
            ),
        )

    async def chat_stream(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        on_text_delta: TextDeltaCallback | None = None,
    ) -> LLMResponse:
        """通过 SSE 流式读取 OpenAI-compatible 响应。

        文本 delta 会实时回调；工具调用的 JSON 参数按 ``index`` 聚合，
        流结束后转换为完整的 ``LLMResponse``，保持 AgentRunner 的消息
        与工具执行逻辑不变。
        """
        if on_text_delta is None:
            return await self.chat(messages=messages, tools=tools)

        payload = self._build_payload(messages, tools, stream=True)
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_buffers: dict[int, dict[str, str]] = {}
        finish_reason = "stop"
        progressed = False

        async def stream_once() -> None:
            """读取一次完整 SSE；未产生输出前的失败允许整体重试。"""
            nonlocal finish_reason, progressed
            async with httpx.AsyncClient(
                timeout=self.timeout,
                transport=self.transport,
            ) as client:
                async with client.stream(
                    "POST",
                    self.chat_url,
                    headers=self._headers(),
                    json=payload,
                ) as response:
                    if response.status_code >= 400:
                        await response.aread()
                        if response.status_code in self.retry_policy.retry_status_codes:
                            raise RetryableStatusError(
                                self._format_http_error(response),
                                response_retry_after(response),
                            )
                        self._raise_for_status(response)

                    async for line in response.aiter_lines():
                        if not line or line.startswith(":"):
                            continue
                        if not line.startswith("data:"):
                            continue
                        data = line[len("data:") :].strip()
                        if data == "[DONE]":
                            break
                        try:
                            chunk = json.loads(data)
                        except json.JSONDecodeError:
                            # SSE 可能夹杂注释或厂商扩展事件，跳过即可。
                            continue
                        choices = (
                            chunk.get("choices")
                            if isinstance(chunk, dict)
                            else None
                        )
                        if not isinstance(choices, list) or not choices:
                            continue
                        choice = choices[0] if isinstance(choices[0], dict) else {}
                        finish_reason = str(
                            choice.get("finish_reason") or finish_reason
                        )
                        delta = choice.get("delta")
                        if not isinstance(delta, dict):
                            continue

                        text = _normalize_content(delta.get("content"))
                        if text:
                            progressed = True
                            content_parts.append(text)
                            on_text_delta(text)
                        reasoning = delta.get("reasoning_content")
                        if isinstance(reasoning, str) and reasoning:
                            progressed = True
                            reasoning_parts.append(reasoning)

                        raw_calls = delta.get("tool_calls") or []
                        if raw_calls:
                            progressed = True
                        for raw_call in raw_calls:
                            if not isinstance(raw_call, dict):
                                continue
                            try:
                                index = int(raw_call.get("index", 0))
                            except (TypeError, ValueError):
                                index = 0
                            buffer = tool_buffers.setdefault(
                                index,
                                {"id": "", "name": "", "arguments": ""},
                            )
                            call_id = raw_call.get("id")
                            if call_id:
                                buffer["id"] += str(call_id)
                            function = raw_call.get("function")
                            if not isinstance(function, dict):
                                continue
                            name = function.get("name")
                            if name:
                                buffer["name"] += str(name)
                            arguments = function.get("arguments")
                            if isinstance(arguments, str):
                                buffer["arguments"] += arguments
                            elif arguments is not None:
                                buffer["arguments"] += json.dumps(
                                    arguments, ensure_ascii=False
                                )

        try:
            await with_retries(
                stream_once,
                policy=self.retry_policy,
                provider_name=self.provider_name,
                has_progressed=lambda: progressed,
            )
        except ImportError as exc:
            raise ProviderError(
                "无法初始化 HTTP 客户端，可能是 SOCKS 代理依赖缺失。"
                "请运行 `uv sync`，或安装 `httpx[socks]` 后重试。"
            ) from exc

        return LLMResponse(
            content="".join(content_parts) or None,
            tool_calls=self._build_tool_calls(tool_buffers),
            finish_reason=finish_reason,
            reasoning_content="".join(reasoning_parts) or None,
        )

    def _format_http_error(self, response: httpx.Response) -> str:
        """提取可读的 HTTP 错误信息。"""
        message = response.text[:500]
        try:
            body = response.json()
            if isinstance(body, dict):
                error = body.get("error")
                if isinstance(error, dict):
                    message = str(error.get("message") or error)
                elif error:
                    message = str(error)
        except ValueError:
            pass
        return f"{self.provider_name} 请求失败 (HTTP {response.status_code}): {message}"
