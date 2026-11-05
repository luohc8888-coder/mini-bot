"""输出能力配置、请求额度隔离与已保存配置的严格校验。"""

import asyncio
from dataclasses import asdict
import json
from pathlib import Path

import httpx
import pytest

from mini_harness.agent import AgentRunner, AgentRunSpec, ContextConfig
from mini_harness.cli import InteractiveCLI, ProfileStore, ProfileStoreError, ProviderProfile
from mini_harness.cli.main import parse_args
from mini_harness.providers import ProviderConfig, create_provider
from mini_harness.tools import ToolRegistry
from tests.test_cli import ScriptedConsole


@pytest.mark.parametrize("value", [0, -1, True, 4096.5, "4096", None])
def test_config_and_profile_require_positive_integer_output_limit(value) -> None:
    with pytest.raises(ValueError, match="max_output_tokens 必须是正整数"):
        ProviderConfig(provider="custom", max_output_tokens=value)
    with pytest.raises(ValueError, match="max_output_tokens 必须是正整数"):
        ProviderProfile(name="test", provider="custom", model="m", max_output_tokens=value)


def test_removed_output_keyword_is_not_accepted() -> None:
    with pytest.raises(TypeError, match="max_tokens"):
        ProviderConfig(provider="custom", max_tokens=4096)
    with pytest.raises(TypeError, match="max_tokens"):
        ProviderProfile(name="test", provider="custom", model="m", max_tokens=4096)


def test_profile_roundtrip_uses_one_field_and_preserves_small_limit(tmp_path: Path) -> None:
    config = ProviderConfig(provider="custom", model="m", max_output_tokens=1024)
    profile = ProviderProfile.from_config("small", config)
    store = ProfileStore(tmp_path / "config.json")
    store.upsert(profile)
    saved = json.loads(store.path.read_text())["profiles"][0]
    assert saved["max_output_tokens"] == 1024
    assert "max_tokens" not in saved and "max_tokens" not in asdict(config)
    assert store.load()[0].to_config() == config


@pytest.mark.parametrize("invalid", [0, -1, True, "4096", 4096.5])
def test_invalid_saved_limit_is_reported_without_discarding_profiles(tmp_path: Path, invalid) -> None:
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"profiles": [
        {"name": "kept", "provider": "custom", "model": "m", "api_key": "test-secret", "max_output_tokens": invalid},
    ]}))
    original = path.read_bytes()
    with pytest.raises(ProfileStoreError, match="max_output_tokens 必须是正整数"):
        ProfileStore(path).load()
    assert path.read_bytes() == original


@pytest.mark.parametrize("fields", [{"max_tokens": 8192}, {"max_output_tokens": 0}])
@pytest.mark.asyncio
async def test_cli_reports_invalid_saved_output_settings(tmp_path: Path, fields: dict) -> None:
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"profiles": [{
        "name": "invalid", "provider": "custom", "model": "m", "api_key": "test-secret", **fields,
    }]}))
    original = path.read_bytes()
    console = ScriptedConsole([])
    await InteractiveCLI(workspace=tmp_path, data_root=tmp_path / "data", console=console, profile_store=ProfileStore(path)).run()
    assert "max_output_tokens" in console.text
    assert "test-secret" not in console.text
    assert path.read_bytes() == original


@pytest.mark.parametrize("invalid", ["0", "-1", "1.5"])
def test_explicit_invalid_output_cli_option_is_rejected(invalid: str) -> None:
    with pytest.raises(SystemExit):
        parse_args(["--max-output-tokens", invalid])


def test_missing_output_cli_option_is_not_a_numeric_sentinel() -> None:
    assert parse_args([]).max_output_tokens is None
    assert parse_args(["--max-output-tokens", "8192"]).max_output_tokens == 8192


@pytest.mark.parametrize("protocol,model,field", [
    ("openai", "m", "max_tokens"),
    ("openai", "o4-mini", "max_completion_tokens"),
    ("anthropic", "m", "max_tokens"),
])
@pytest.mark.asyncio
async def test_wire_output_budget_changes_without_mutating_capability(protocol: str, model: str, field: str) -> None:
    config = ProviderConfig(
        provider="custom", protocol=protocol, model=model,
        api_key="test", base_url="https://example.com/v1",
        max_output_tokens=12000, context_window_tokens=16384,
    )
    original = asdict(config)
    provider = create_provider(config)
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        body = ({"content": [{"type": "text", "text": "OK"}], "stop_reason": "end_turn"}
                if protocol == "anthropic" else
                {"choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}]})
        return httpx.Response(200, json=body)

    provider.transport = httpx.MockTransport(handler)
    runner = AgentRunner(provider, ToolRegistry())
    snapshots = []
    for text in ("x" * 40000, "small"):
        result = await runner.run(AgentRunSpec(
            initial_messages=[{"role": "user", "content": text}],
            context_config=ContextConfig(max_context_tokens=16384, reserve_output_tokens=1024),
            request_recorder=snapshots.append,
        ))
        assert result.stop_reason == "completed"
    assert 0 < requests[0][field] < 12000
    assert requests[1][field] == 12000
    assert [s.max_output_tokens for s in snapshots] == [r[field] for r in requests]
    assert provider.max_output_tokens == 12000
    assert provider.output_token_limit == 12000
    assert asdict(config) == original


@pytest.mark.asyncio
async def test_request_output_budget_is_local_to_task_and_restored() -> None:
    provider = create_provider(ProviderConfig(provider="mock", max_output_tokens=4096))
    provider.set_output_budget(3000)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def maintenance() -> None:
        with provider.request_output_budget(256):
            entered.set()
            await release.wait()
            assert provider.output_token_limit == 256
        assert provider.output_token_limit == 2000

    task = asyncio.create_task(maintenance())
    await entered.wait()
    assert provider.output_token_limit == 3000
    provider.set_output_budget(2000)
    release.set()
    await task
    assert provider.output_token_limit == 2000
    with pytest.raises(RuntimeError):
        with provider.request_output_budget(128):
            assert provider.output_token_limit == 128
            raise RuntimeError("failed request")
    assert provider.output_token_limit == 2000


@pytest.mark.asyncio
async def test_request_output_budget_is_restored_after_cancellation() -> None:
    provider = create_provider(ProviderConfig(provider="mock"))
    entered = asyncio.Event()

    async def maintenance() -> None:
        try:
            with provider.request_output_budget(128):
                entered.set()
                await asyncio.Event().wait()
        finally:
            assert provider.output_token_limit == provider.max_output_tokens

    task = asyncio.create_task(maintenance())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.parametrize("protocol", ["openai", "anthropic"])
@pytest.mark.asyncio
async def test_dream_wire_request_respects_window_and_preserves_main_budget(
    tmp_path: Path, protocol: str,
) -> None:
    from mini_harness.agent.tokens import estimate_messages_tokens
    from mini_harness.memory import DreamManager, MemoryStore

    provider = create_provider(ProviderConfig(
        provider="custom", protocol=protocol, model="m", api_key="test",
        base_url="https://example.com/v1",
        max_output_tokens=12000, context_window_tokens=2048,
    ))
    provider.set_output_budget(1500)
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        if protocol == "anthropic":
            body = {"content": [{"type": "text", "text": "(nothing)"}], "stop_reason": "end_turn"}
        else:
            body = {"choices": [{"message": {"content": "(nothing)"}, "finish_reason": "stop"}]}
        return httpx.Response(200, json=body)

    provider.transport = httpx.MockTransport(handler)
    store = MemoryStore(tmp_path)
    store.append_history("项目使用 pytest", session_key="cli:main")
    manager = DreamManager(provider, store)
    result = await manager.run()
    assert result.error is None
    budget, _ = manager._request_limits()
    _, messages = manager._build_requests("", store.read_history(), budget)
    assert requests[0]["max_tokens"] == 2048 - estimate_messages_tokens(messages[0])
    assert provider.output_token_limit == 1500
    assert provider.max_output_tokens == 12000
