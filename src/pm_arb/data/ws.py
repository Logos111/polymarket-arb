"""CLOB 行情 WebSocket 客户端（market 通道）。

协议要点：
- 连接后发送订阅：``{"assets_ids": [...], "type": "market"}``
- 首条消息为 ``book`` 全量快照，之后为 ``price_change`` 增量；
  另有 ``last_trade_price`` 成交通知、``tick_size_change``。
- 服务端可能以单条 dict 或 list[dict] 批量下发。
- 断连需重新订阅（首条快照会重建盘口）。

本类只负责连接、订阅、解析与重连；盘口状态维护见 ``orderbook.py``，
两者的编排见 ``feed.py``。
"""

from __future__ import annotations

import asyncio
import inspect
import json
import urllib.request
from collections.abc import AsyncIterator, Awaitable, Callable

import websockets
from websockets.exceptions import ConnectionClosed

from pm_arb.data.models import WsBookEvent, WsLastTrade, WsPriceChange
from pm_arb.infra.config import Settings, get_settings
from pm_arb.infra.logging import get_logger

log = get_logger(__name__)

# 重连退避（秒）
_RECONNECT_DELAYS = [1, 2, 4, 8, 15, 30]

WsEvent = WsBookEvent | WsPriceChange | WsLastTrade


def _resolve_proxy(settings: Settings) -> str | None:
    """决定 WS 连接使用的代理。

    websockets 默认 ``proxy=True`` 会自动读取系统代理，在 macOS 上可能选中
    SOCKS 条目（需额外安装 python-socks，否则抛 ImportError 静默断流）。
    这里显式解析：优先 ``PM_PROXY_URL``，其次系统 http/https 代理（CONNECT 方式，
    无需 socks 依赖），忽略 socks 条目；都没有则返回 None（直连）。
    """
    if settings.proxy_url:
        return settings.proxy_url
    gp = urllib.request.getproxies()
    for key in ("https", "http"):
        v = gp.get(key)
        if v and v.startswith("http://"):
            return v
    return None


def parse_message(raw: str | bytes) -> list[WsEvent]:
    """把一条原始 WS 消息解析为 typed events；无法识别的类型跳过。"""
    data = json.loads(raw)
    messages = [data] if isinstance(data, dict) else data

    events: list[WsEvent] = []
    for m in messages:
        etype = m.get("event_type")
        try:
            if etype == "book":
                events.append(
                    WsBookEvent(
                        asset_id=str(m["asset_id"]),
                        market=str(m.get("market", "")),
                        bids=m.get("bids", []),
                        asks=m.get("asks", []),
                        timestamp=str(m.get("timestamp", "")),
                        hash=str(m.get("hash", "")),
                    )
                )
            elif etype == "price_change":
                for ch in m.get("changes", []):
                    events.append(
                        WsPriceChange(
                            asset_id=str(m["asset_id"]),
                            market=str(m.get("market", "")),
                            side=str(ch["side"]),
                            price=ch["price"],
                            size=ch["size"],
                            timestamp=str(m.get("timestamp", "")),
                            hash=str(m.get("hash", "")),
                        )
                    )
            elif etype == "last_trade_price":
                events.append(
                    WsLastTrade(
                        asset_id=str(m["asset_id"]),
                        market=str(m.get("market", "")),
                        price=m["price"],
                        size=m.get("size", 0),
                        side=str(m.get("side", "")),
                        timestamp=str(m.get("timestamp", "")),
                        fee_rate_bps=str(m.get("fee_rate_bps", "")),
                    )
                )
            else:
                log.debug("ws_unknown_event", event_type=etype)
        except (KeyError, TypeError, ValueError) as e:
            log.warning("ws_parse_error", error=str(e), event_type=etype)
    return events


class MarketWsClient:
    """market 通道连接。用法::

        async for event in client.stream(asset_ids):
            ...   # 断连自动重连，重连后会重新收到 book 快照
    """

    def __init__(self, settings: Settings | None = None):
        self._settings = settings or get_settings()
        self._url = self._settings.clob_ws_market_url

    async def stream(
        self,
        asset_ids: list[str],
        *,
        on_reconnected: Callable[[], Awaitable[None] | None] | None = None,
    ) -> AsyncIterator[WsEvent]:
        """订阅 asset_ids 并无限产出事件；连接断开自动重连。

        ``on_reconnected``：每次（重）连接成功后回调，feed 层借此重新拉
        REST 快照，防止重连间隙丢失增量。
        """
        attempt = 0
        while True:
            try:
                connect_kwargs: dict = {
                    "ping_interval": 10,
                    "ping_timeout": 20,
                    "close_timeout": 5,
                    "max_queue": 256,
                    # 显式指定代理，避免自动选用系统 SOCKS 导致 ImportError
                    "proxy": _resolve_proxy(self._settings),
                }
                async with websockets.connect(self._url, **connect_kwargs) as ws:
                    sub = {"assets_ids": asset_ids, "type": "market"}
                    await ws.send(json.dumps(sub))
                    log.info("ws_connected", url=self._url, assets=len(asset_ids))
                    if on_reconnected is not None:
                        res = on_reconnected()
                        if inspect.isawaitable(res):
                            await res
                    attempt = 0

                    async for raw in ws:
                        for event in parse_message(raw):
                            yield event

            except (ConnectionClosed, OSError, TimeoutError) as e:
                delay = _RECONNECT_DELAYS[min(attempt, len(_RECONNECT_DELAYS) - 1)]
                attempt += 1
                log.warning("ws_disconnected", error=str(e)[:120], reconnect_in=delay)
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                log.info("ws_stream_cancelled")
                raise
            except Exception as e:  # 兜底：ImportError/SSL 等意外错误不应静默杀死流
                delay = _RECONNECT_DELAYS[min(attempt, len(_RECONNECT_DELAYS) - 1)]
                attempt += 1
                log.warning("ws_unexpected_error", error=f"{type(e).__name__}: {e}"[:160],
                            reconnect_in=delay)
                await asyncio.sleep(delay)
