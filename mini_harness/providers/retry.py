"""Provider 请求的重试策略、退避计算与统一重试循环。

只重试可能自行恢复的失败：网络传输错误、超时、限流（429）和服务端 5xx。
鉴权失败、参数错误等 4xx 立即失败，避免把同一个错误请求反复发送。

流式请求额外遵守一条规则：一旦向调用方产生过任何增量（文本、推理或工具
参数），就不再重试，否则用户会看到重复输出。
"""

from __future__ import annotations

import asyncio
import math
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TypeVar

import httpx

from .base import ProviderError

T = TypeVar("T")

# 默认重试状态码：408 超时、429 限流、500/502/503/504 服务端错误，
# 以及 Anthropic 在过载时返回的 529。
DEFAULT_RETRY_STATUS_CODES: frozenset[int] = frozenset(
    {408, 429, 500, 502, 503, 504, 529}
)


class RetryableStatusError(RuntimeError):
    """内部信号：本次响应的状态码可重试。

    携带已经格式化好的错误文本，由 ``with_retries`` 在尝试次数耗尽时
    转换成 ProviderError，避免协议适配器重复构造错误信息。

    Args:
        message: 供最终 ProviderError 使用的可读错误信息。
        retry_after: 服务端 Retry-After 秒数；None 表示使用指数退避。

    上下游:
        协议适配器在确认状态码属于 ``RetryPolicy.retry_status_codes`` 后
        抛出；``with_retries`` 捕获并决定继续等待还是最终失败。
    """

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.retry_after = retry_after


@dataclass(slots=True)
class RetryPolicy:
    """一次 Provider 请求的尝试次数与退避参数。

    Args:
        max_attempts: 总尝试次数，包含第一次请求；1 表示不重试。
        base_delay: 第一次重试的基础等待秒数，之后按 2 的幂增长。
        max_delay: 单次等待上限，同时约束服务端 Retry-After 的取值。
        retry_status_codes: 允许重试的 HTTP 状态码集合。

    Returns:
        不可变的策略配置，可直接传给 OpenAI-compatible 或 Anthropic Provider。

    上下游:
        两个协议适配器在 ``chat`` 与 ``chat_stream`` 中共用同一个策略实例；
        测试传入 ``max_attempts=1`` 或 ``base_delay=0`` 即可关闭/加速重试。
    """

    max_attempts: int = 3
    base_delay: float = 0.5
    max_delay: float = 8.0
    retry_status_codes: frozenset[int] = DEFAULT_RETRY_STATUS_CODES

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        if not math.isfinite(self.base_delay) or self.base_delay < 0:
            raise ValueError("base_delay must be >= 0 and finite")
        if not math.isfinite(self.max_delay) or self.max_delay < self.base_delay:
            raise ValueError("max_delay must be finite and >= base_delay")
        if not self.retry_status_codes:
            raise ValueError("retry_status_codes must not be empty")


def parse_retry_after(value: str | None) -> float | None:
    """解析 Retry-After 响应头。

    Args:
        value: 原始头值，可能是秒数或 HTTP 日期。

    Returns:
        非负秒数；空值、负数、非数字和 HTTP 日期形式返回 None，
        由调用方回退到指数退避。
    """
    if not value:
        return None
    try:
        seconds = float(value.strip())
    except (TypeError, ValueError):
        return None
    if not math.isfinite(seconds) or seconds < 0:
        return None
    return seconds


def response_retry_after(response: httpx.Response) -> float | None:
    """读取响应头中的 Retry-After 秒数。"""
    return parse_retry_after(response.headers.get("retry-after"))


def retry_delay_seconds(
    attempt: int,
    policy: RetryPolicy,
    retry_after: float | None = None,
    *,
    rand: Callable[[], float] | None = None,
) -> float:
    """计算第 ``attempt`` 次重试前的等待秒数。

    Args:
        attempt: 已失败的重试序号，0 表示第一次重试。
        policy: 退避参数。
        retry_after: 服务端 Retry-After 秒数；有值时优先采用并受 max_delay 约束。
        rand: 抖动随机源，返回 ``[0, 1)``；测试可注入固定值。

    Returns:
        非负等待秒数。没有 Retry-After 时使用 50%~100% 抖动，
        让多个客户端不会在同一时刻重新发起请求。
    """
    if retry_after is not None:
        return min(max(retry_after, 0.0), policy.max_delay)
    raw_delay = min(policy.base_delay * (2**attempt), policy.max_delay)
    jitter = rand() if rand is not None else random.random()
    return raw_delay * (0.5 + 0.5 * jitter)


async def with_retries(
    operation: Callable[[], Awaitable[T]],
    *,
    policy: RetryPolicy,
    provider_name: str,
    has_progressed: Callable[[], bool] | None = None,
) -> T:
    """执行一次可能重试的 Provider 请求。

    Args:
        operation: 单次尝试。可重试状态码应抛出 ``RetryableStatusError``，
            网络故障交由 ``httpx.RequestError`` 冒泡。
        policy: 重试策略。
        provider_name: 错误信息中展示的厂商名称。
        has_progressed: 是否已经向调用方产生输出；流式请求用它阻止重试
            造成重复输出，普通请求保持 None。

    Returns:
        ``operation`` 的返回值。

    Raises:
        ProviderError: 网络错误或可重试状态码在尝试次数耗尽后仍未成功。

    上下游:
        OpenAI-compatible / Anthropic Provider 的 ``chat`` 与 ``chat_stream``
        都经过这里；异常最终由 CLI 统一捕获并展示。
    """
    progressed = has_progressed if has_progressed is not None else lambda: False
    for attempt in range(policy.max_attempts):
        try:
            return await operation()
        except RetryableStatusError as exc:
            if attempt + 1 >= policy.max_attempts:
                raise ProviderError(
                    f"{exc.message}（已尝试 {policy.max_attempts} 次）"
                ) from exc
            await asyncio.sleep(
                retry_delay_seconds(attempt, policy, exc.retry_after)
            )
        except httpx.RequestError as exc:
            if progressed() or attempt + 1 >= policy.max_attempts:
                raise ProviderError(
                    f"{provider_name} 网络请求失败：{exc}"
                ) from exc
            await asyncio.sleep(retry_delay_seconds(attempt, policy))
    raise AssertionError("unreachable: retry loop must return or raise")