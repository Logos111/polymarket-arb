"""Polymarket RTDS：Chainlink TWAP 实时数据流客户端（5m 市场结算同源）。

5 分钟 Up/Down 市场的结算源是 Chainlink Data Streams 的 XX/USD TWAP-60s
数据流（市场 description 明示，见 docs/chainlink-twap.md）；Polymarket
RTDS 无凭证中继了这条流，也是网页端实时价格的数据源：

    wss://ws-live-data.polymarket.com

协议要点（官方文档 chainlink-twap.md）：
- 订阅帧：``{"action": "subscribe", "subscriptions": [{"topic":
  "crypto_prices_twap_sixty", "type": "update", "filters":
  '{"symbol":"btc/usd"}'}]}``（filters 必须是紧凑 JSON，无空格）；
- **应用层心跳：每 5 秒发送文本帧 ``PING``**（链路层 ping/pong 不保活）；
- ``payload.value`` 是浮点展示值；``full_accuracy_value`` 是 E18 定点
  精确值（结算口径），应优先使用；
- 订阅从下一条更新开始，**无快照/回放**，断连需重连重订阅。

教训（沿 ws.py 同样的坑）：链路层活着不代表业务频道在推数据，recv 必须
套空闲看门狗；同时提供单调时钟的新鲜度判定，供上层决策"数据是否可信"。
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from decimal import Decimal

import websockets
from websockets.exceptions import ConnectionClosed

from pm_arb.data.ws import _resolve_proxy
from pm_arb.infra.config import Settings, get_settings
from pm_arb.infra.logging import get_logger

log = get_logger(__name__)

# 重连退避（秒），与 ws.py 一致
_RECONNECT_DELAYS = [1, 2, 4, 8, 15, 30]

# TWAP 回看窗口 → RTDS topic
_TOPICS: dict[int, str] = {
    30: "crypto_prices_twap_thirty",
    60: "crypto_prices_twap_sixty",
}

_E18 = Decimal(10) ** 18


class RtdsTwapFeed:
    """订阅某交易对的 Chainlink TWAP 流，维护最新值与窗口高低点。

    用法::

        feed = RtdsTwapFeed("btc")
        task = asyncio.create_task(feed.run(stop))
        ...  # 读 feed.last / feed.high / feed.low / feed.age()
    """

    def __init__(
        self,
        symbol: str,
        settings: Settings | None = None,
        *,
        window_seconds: int = 60,
        idle_timeout: float = 15.0,
        on_raw: Callable[[dict], None] | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self.pair = f"{symbol.lower()}/usd"
        if window_seconds not in _TOPICS:
            raise ValueError(f"window_seconds 仅支持 {sorted(_TOPICS)}")
        self._topic = _TOPICS[window_seconds]
        self._url = "wss://ws-live-data.polymarket.com"
        # TWAP 流约每秒推一条；空闲看门狗阈值远大于正常间隔，
        # 触发即说明订阅静默失效，主动断开重连
        self._idle_timeout = idle_timeout
        # 每条目标交易对的原始 payload 回调（含 full_accuracy_value/seq），
        # 供 pm-record 落盘 RTDS 流；同步回调，异常不得阻断数据流
        self._on_raw = on_raw

        # ---- 状态（上层读取）----
        self.last: Decimal | None = None
        self.high: Decimal | None = None
        self.low: Decimal | None = None
        # 结算规则参照的"窗口起点价格"：本 feed 生命期内首个观测值
        self.base: Decimal | None = None
        self.samples = 0
        self.errors = 0
        self.seq = 0  # 收到的更新条数（单调递增，供探针展示活跃度）
        self._last_update = 0.0  # 单调时钟；0 表示从未收到
        self._conn_at = 0.0  # 本次连接建立时刻（业务看门狗宽限期基线）

    # ---- 只读访问 ----

    @property
    def price_range(self) -> Decimal | None:
        if self.high is None or self.low is None:
            return None
        return self.high - self.low

    def age(self) -> float:
        """距上次收到 TWAP 更新的秒数（单调时钟）；从未收到返回 +inf。"""
        if not self._last_update:
            return float("inf")
        return time.monotonic() - self._last_update

    def is_fresh(self, max_age: float) -> bool:
        return self.last is not None and self.age() < max_age

    def reset_window(self) -> None:
        """开始新窗口时重置统计（保留 last 作为新窗口的起点参照）。"""
        self.base = self.last
        self.high = None
        self.low = None
        self.samples = 0

    # ---- 事件处理 ----

    def _handle_message(self, raw: str | bytes) -> Decimal | None:
        """解析一条 RTDS 消息；非目标交易对/坏消息返回 None。

        实测服务端会对应用层心跳回文本帧 ``PONG``（非 JSON），应静默
        跳过而非计入解析错误。
        """
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        text = raw.strip()
        if not text.startswith("{"):
            log.debug("rtds_non_json_frame", frame=text[:16])
            return None
        try:
            data = json.loads(raw)
            payload = data.get("payload") or {}
            if payload.get("symbol") != self.pair:
                return None
            if self._on_raw is not None:
                try:
                    self._on_raw(data)
                except Exception as e:  # 录制失败不阻断行情流
                    log.warning("rtds_raw_sink_error", error=str(e)[:80])
            # 优先 E18 定点精确值（结算口径），缺失时退回展示值
            fav = payload.get("full_accuracy_value")
            if fav is not None:
                price = Decimal(str(fav)) / _E18
            elif payload.get("value") is not None:
                price = Decimal(str(payload["value"]))
            else:
                return None
        except (KeyError, TypeError, ValueError, ArithmeticError) as e:
            self.errors += 1
            log.warning("rtds_parse_error", error=str(e)[:100])
            return None

        self.last = price
        self.samples += 1
        self.seq += 1
        self._last_update = time.monotonic()
        if self.base is None:
            self.base = price
        self.high = price if self.high is None else max(self.high, price)
        self.low = price if self.low is None else min(self.low, price)
        return price

    # ---- 主循环 ----

    async def _ping_loop(self, ws, interval: float = 5.0) -> None:
        """RTDS 应用层心跳：每 5s 发文本帧 PING（文档强制要求）。"""
        while True:
            await asyncio.sleep(interval)
            await ws.send("PING")

    async def run(
        self,
        stop: asyncio.Event | None = None,
        *,
        on_update: Callable[[], Awaitable[None] | None] | None = None,
    ) -> None:
        """连接 RTDS 并持续接收 TWAP 更新，直到 stop 置位或被取消。"""
        attempt = 0
        filters = json.dumps({"symbol": self.pair}, separators=(",", ":"))
        while stop is None or not stop.is_set():
            try:
                connect_kwargs: dict = {
                    "ping_interval": 10,
                    "ping_timeout": 20,
                    "close_timeout": 5,
                    "max_queue": 64,
                    "proxy": _resolve_proxy(self._settings),
                }
                async with websockets.connect(self._url, **connect_kwargs) as ws:
                    sub = {
                        "action": "subscribe",
                        "subscriptions": [
                            {"topic": self._topic, "type": "update", "filters": filters}
                        ],
                    }
                    await ws.send(json.dumps(sub))
                    log.info("rtds_connected", url=self._url, pair=self.pair,
                             window=self._topic)
                    self._conn_at = time.monotonic()
                    attempt = 0

                    ping = asyncio.create_task(self._ping_loop(ws))
                    try:
                        while True:
                            try:
                                raw = await asyncio.wait_for(ws.recv(), timeout=self._idle_timeout)
                            except TimeoutError:
                                # 看门狗：链路活着但频道静默 → 主动断开重连
                                log.warning("rtds_idle_timeout", idle=self._idle_timeout,
                                            action="reconnect")
                                break
                            price = self._handle_message(raw)
                            # 业务级看门狗：链路有帧（如服务端周期性空帧心跳）
                            # 但长期无有效 update → 订阅静默失效，同样重连。
                            # 教训一：空帧会喂饱链路层看门狗，必须另看业务时钟；
                            # 教训二：新连接 age()=+inf，宽限期基线必须含连接
                            # 时刻，否则首条空帧就触发重连风暴，永远收不到数据。
                            baseline = max(self._last_update, self._conn_at)
                            if price is None and time.monotonic() - baseline > self._idle_timeout:
                                log.warning("rtds_stale_but_alive",
                                            idle=self._idle_timeout, action="reconnect",
                                            note="frames arriving but no valid update")
                                break
                            if price is not None and on_update is not None:
                                res = on_update()
                                if asyncio.iscoroutine(res):
                                    await res
                            if stop is not None and stop.is_set():
                                return
                    finally:
                        ping.cancel()

            except asyncio.CancelledError:
                log.info("rtds_stream_cancelled")
                raise
            except (ConnectionClosed, OSError, TimeoutError) as e:
                self.errors += 1
                delay = _RECONNECT_DELAYS[min(attempt, len(_RECONNECT_DELAYS) - 1)]
                attempt += 1
                log.warning("rtds_disconnected", error=str(e)[:120], reconnect_in=delay)
                await asyncio.sleep(delay)
            except Exception as e:  # 兜底：意外错误不得静默杀死流
                self.errors += 1
                delay = _RECONNECT_DELAYS[min(attempt, len(_RECONNECT_DELAYS) - 1)]
                attempt += 1
                log.warning("rtds_unexpected_error",
                            error=f"{type(e).__name__}: {e}"[:160], reconnect_in=delay)
                await asyncio.sleep(delay)
