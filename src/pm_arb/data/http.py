"""共享 HTTP 客户端工厂：统一超时、重试与日志。"""

from __future__ import annotations

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from pm_arb.infra.config import Settings, get_settings
from pm_arb.infra.logging import get_logger

log = get_logger(__name__)

_DEFAULT_HEADERS = {
    "Accept": "application/json",
    "User-Agent": "pm-arb/0.1 (+https://github.com/Logos111/polymarket-arb)",
}


def make_http_client(base_url: str, settings: Settings | None = None) -> httpx.AsyncClient:
    """创建带基础配置的异步 HTTP 客户端。调用方负责 ``async with`` 或 close。

    设置了 ``PM_PROXY_URL`` 时自动走代理（如 Clash 的 http://127.0.0.1:7890）。
    """
    s = settings or get_settings()
    return httpx.AsyncClient(
        base_url=base_url.rstrip("/"),
        timeout=httpx.Timeout(s.http_timeout),
        headers=_DEFAULT_HEADERS,
        limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        follow_redirects=True,
        proxy=s.proxy_url or None,
    )


# 仅对网络层瞬时错误重试；4xx（除 429）属于请求本身问题，不重试
_retry_http = retry(
    retry=retry_if_exception_type(
        (httpx.TransportError, httpx.RemoteProtocolError, httpx.HTTPStatusError)
    ),
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=0.5, min=0.5, max=8),
    reraise=True,
)


@_retry_http
async def get_json(client: httpx.AsyncClient, path: str, params: dict | None = None):
    """GET 请求并返回 JSON；对 429/5xx 与传输错误自动重试。"""
    resp = await client.get(path, params=params)
    if resp.status_code >= 400:
        log.warning("http_get_error", path=path, status=resp.status_code, body=resp.text[:200])
        resp.raise_for_status()
    return resp.json()
