"""流式输出适配测试：Provider 分片、Runner 回调和 CLI 渲染。"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from mini_harness.agent import AgentRunSpec, AgentRunner
from mini_harness.cli.app import Console, InteractiveCLI
from mini_harness.cli.profiles import ProfileStore, ProviderProfile
from mini_harness.providers import (
    AnthropicProvider,
    MockProvider,
    OpenAICompatibleProvider,
)
from mini_harness.providers.base import LLMResponse
from mini_harness.tools import ToolRegistry


def _sse(*events: dict) -> bytes:
    """把事件列表编码为 SSE 响应体。"""
    body = "".join(
        f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        for event in events
    )
    return (body + "data: [DONE]\n\n").encode("utf-8")


@pytest.mark.asyncio
async def test_mock_provider_streams_text_chunks() -> None:
    """MockProvider 应按固定块大小回调文本。"""
    provider = MockProvider(
        [LLMResponse(content="你好，流式世界")],
        stream_chunk_size=3,
    )
    chunks: list[str] = []

    response = await provider.chat_stream(
        [{"role": "user", "content": "hi"}],
        on_text_delta=chunks.append,
    )

    assert "".join(chunks) == "你好，流式世界"
    assert len(chunks) > 1
    assert response.content == "你好，流式世界"


@pytest.mark.asyncio
async def test_runner_streams_final_without_partial_messages() -> None:
    """Runner 只把完整 assistant 消息写入历史，不能写入中间 chunk。"""
    provider = MockProvider(
        [LLMResponse(content="完整的最终回答")],
        stream_chunk_size=2,
    )
    chunks: list[str] = []
    result = await AgentRunner(
        provider=provider,
        tools=ToolRegistry(),
    ).run(
        AgentRunSpec(
            initial_messages=[{"role": "user", "content": "请回答"}],
            on_text_delta=chunks.append,
        )
    )

    assert "".join(chunks) == "完整的最终回答"
    assert result.final_content == "完整的最终回答"
    assert result.streamed_final is True
    assert result.messages == [
        {"role": "user", "content": "请回答"},
        {"role": "assistant", "content": "完整的最终回答"},
    ]


@pytest.mark.asyncio
async def test_openai_stream_aggregates_text_and_tool_calls() -> None:
    """OpenAI SSE 的文本和分片 tool_calls 都应正确聚合。"""
    seen_payloads: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_payloads.append(json.loads(request.content))
        return httpx.Response(
            200,
            content=_sse(
                {
                    "choices": [
                        {
                            "delta": {"content": "正在"},
                            "finish_reason": None,
                        }
                    ]
                },
                {
                    "choices": [
                        {
                            "delta": {"content": "处理"},
                            "finish_reason": None,
                        }
                    ]
                },
                {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "call_stream_1",
                                        "type": "function",
                                        "function": {
                                            "name": "write_file",
                                            "arguments": '{"path": "s.md", ',
                                        },
                                    }
                                ]
                            },
                            "finish_reason": None,
                        }
                    ]
                },
                {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "function": {
                                            "arguments": '"content": "hello"}'
                                        },
                                    }
                                ]
                            },
                            "finish_reason": "tool_calls",
                        }
                    ]
                },
            ),
            headers={"content-type": "text/event-stream"},
        )

    provider = OpenAICompatibleProvider(
        api_key="sk-test",
        base_url="https://example.com/v1",
        model="test-model",
        transport=httpx.MockTransport(handler),
    )
    chunks: list[str] = []
    response = await provider.chat_stream(
        [{"role": "user", "content": "生成文件"}],
        on_text_delta=chunks.append,
    )

    assert seen_payloads[0]["stream"] is True
    assert "".join(chunks) == "正在处理"
    assert response.content == "正在处理"
    assert len(response.tool_calls) == 1
    assert response.tool_calls[0].id == "call_stream_1"
    assert response.tool_calls[0].name == "write_file"
    assert response.tool_calls[0].arguments == {
        "path": "s.md",
        "content": "hello",
    }
    assert response.finish_reason == "tool_calls"


@pytest.mark.asyncio
async def test_anthropic_stream_aggregates_text_and_tool_use() -> None:
    """Anthropic SSE 的 text_delta 与 input_json_delta 应正确聚合。"""
    seen_payloads: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_payloads.append(json.loads(request.content))
        return httpx.Response(
            200,
            content=_sse(
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "text", "text": ""},
                },
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": "你好"},
                },
                {
                    "type": "content_block_start",
                    "index": 1,
                    "content_block": {
                        "type": "tool_use",
                        "id": "toolu_stream_1",
                        "name": "read_file",
                    },
                },
                {
                    "type": "content_block_delta",
                    "index": 1,
                    "delta": {
                        "type": "input_json_delta",
                        "partial_json": '{"path": "README.md"}',
                    },
                },
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "tool_use"},
                },
                {"type": "message_stop"},
            ),
            headers={"content-type": "text/event-stream"},
        )

    provider = AnthropicProvider(
        api_key="sk-ant",
        base_url="https://api.anthropic.com/v1",
        model="claude-test",
        transport=httpx.MockTransport(handler),
    )
    chunks: list[str] = []
    response = await provider.chat_stream(
        [{"role": "user", "content": "读取文件"}],
        on_text_delta=chunks.append,
    )

    assert seen_payloads[0]["stream"] is True
    assert "".join(chunks) == "你好"
    assert response.content == "你好"
    assert len(response.tool_calls) == 1
    assert response.tool_calls[0].id == "toolu_stream_1"
    assert response.tool_calls[0].name == "read_file"
    assert response.tool_calls[0].arguments == {"path": "README.md"}
    assert response.finish_reason == "tool_use"


class _StreamingConsole(Console):
    """收集流式输出的测试 Console。"""

    def __init__(self, answers: list[str]) -> None:
        self._answers = iter(answers)
        self.output: list[str] = []

    def write(self, text: str = "") -> None:
        self.output.append(text)

    def write_stream_start(self) -> None:
        self.output.append("助手 > ")

    def write_stream_delta(self, text: str) -> None:
        self.output[-1] += text

    def write_stream_end(self) -> None:
        self.output[-1] += "\n"

    async def read(self, prompt: str = "") -> str:
        try:
            return next(self._answers)
        except StopIteration as exc:
            raise EOFError from exc


@pytest.mark.asyncio
async def test_cli_streaming_does_not_duplicate_final_answer(
    tmp_path: Path,
) -> None:
    """CLI 流式打印后不应再调用 write_assistant 重复输出。"""
    store = ProfileStore(tmp_path / "config.json")
    store.upsert(
        ProviderProfile(
            name="Mock",
            provider="mock",
            model="mock-add-demo",
        )
    )
    console = _StreamingConsole(["计算 2 + 3", "/exit"])
    cli = InteractiveCLI(
        console=console,
        profile_store=store,
        workspace=tmp_path,
        data_root=tmp_path / "data",
        stream=True,
    )

    await cli.run()

    text = "\n".join(console.output)
    assert "助手 > 结果是 5" in text
    assert text.count("结果是 5") == 1
    assert "[工具] add" in text
