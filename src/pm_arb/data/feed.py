"""行情 Feed：编排 REST 快照 + WS 增量，维护一组 token 的实时订单簿。

职责：
1. 启动 / WS 重连时，先对所有订阅 token 拉 REST 快照（兜底，防止
   首条 book 消息前的增量丢失，也用于校验本地状态）；
2. 接收 WS 事件：book → 覆盖本地簿；price_change → 应用增量；
   last_trade → 透传回调；
3. 每次簿更新后回调通知策略层 / 录制器。

用法::

    feed = MarketDataFeed(token_ids)
    async for event in feed.run():
        ...  # 或传入 on_book 回调
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable

from pm_arb.data.clob_rest import ClobRestClient
from pm_arb.data.models import WsBookEvent, WsLastTrade, WsPriceChange
from pm_arb.data.orderbook import LocalOrderBook
from pm_arb.data.recorder import TickRecorder
from pm_arb.data.ws import MarketWsClient, WsEvent
from pm_arb.infra.logging import get_logger

log = get_logger(__name__)

BookCallback = Callable[[str, LocalOrderBook], Awaitable[None] | None]
TradeCallback = Callable[[WsLastTrade], Awaitable[None] | None]


class MarketDataFeed:
    def __init__(
        self,
        asset_ids: list[str],
        *,
        rest: ClobRestClient | None = None,
        ws: MarketWsClient | None = None,
        recorder: TickRecorder | None = None,
        on_book: BookCallback | None = None,
        on_trade: TradeCallback | None = None,
    ):
        self.asset_ids = list(asset_ids)
        self._rest = rest
        self._owns_rest = rest is None
        self._ws = ws or MarketWsClient()
        self._recorder = recorder
        self.on_book = on_book
        self.on_trade = on_trade

        self._books: dict[str, LocalOrderBook] = {a: LocalOrderBook(a) for a in self.asset_ids}
        self._snapshot_lock = asyncio.Lock()

    def book(self, asset_id: str) -> LocalOrderBook | None:
        return self._books.get(asset_id)

    def books(self) -> dict[str, LocalOrderBook]:
        return self._books

    async def _rest_snapshot_all(self) -> None:
        """对所有 token 拉 REST 快照并重建本地簿（重连后调用）。"""
        if self._rest is None:
            log.debug("feed_no_rest_client_skip_snapshot")
            return
        async with self._snapshot_lock:
            async def fetch_one(asset_id: str) -> None:
                try:
                    book = await self._rest.get_book(asset_id)  # type: ignore[union-attr]
                    ob = self._books[asset_id]
                    ob.apply_snapshot(book)
                    await self._notify_book(asset_id, ob)
                except Exception as e:
                    log.warning("feed_snapshot_failed", token=asset_id[:10], error=str(e)[:100])

            await asyncio.gather(*(fetch_one(a) for a in self.asset_ids))
            log.info("feed_rest_snapshots_done", assets=len(self.asset_ids))

    async def _notify_book(self, asset_id: str, ob: LocalOrderBook) -> None:
        if self.on_book is not None:
            res = self.on_book(asset_id, ob)
            if asyncio.iscoroutine(res):
                await res

    async def _notify_trade(self, trade: WsLastTrade) -> None:
        if self.on_trade is not None:
            res = self.on_trade(trade)
            if asyncio.iscoroutine(res):
                await res

    async def run(self) -> AsyncIterator[WsEvent]:
        """主循环：连接 WS、维护订单簿、产出事件。无限运行直到被取消。"""
        if self._owns_rest:
            self._rest = ClobRestClient()

        # 启动即先拉一次快照，不等 WS 的 book 消息（降低启动后空窗）
        await self._rest_snapshot_all()

        async def _on_reconnect() -> None:
            # 重连后重拉快照；随后到达的 WS book 消息会再次覆盖
            await self._rest_snapshot_all()

        def _on_disconnect() -> None:
            # 断连/看门狗超时：立即把本地簿置为未就绪，使上层马上回退 REST，
            # 而不是硬扛一份可能已冻结的旧盘口（补救 ready 单向锁）
            for ob in self._books.values():
                ob.invalidate()
            log.info("feed_books_invalidated", assets=len(self._books))

        stream = self._ws.stream(
            self.asset_ids, on_reconnected=_on_reconnect, on_disconnected=_on_disconnect
        )
        try:
            async for event in stream:
                if self._recorder is not None:
                    self._recorder.record(event)

                asset_id = event.asset_id
                ob = self._books.get(asset_id)
                if ob is None:
                    # 订阅集合外的 token（理论上不会出现）
                    continue

                if isinstance(event, WsBookEvent):
                    ob.apply_snapshot(event)
                    await self._notify_book(asset_id, ob)
                elif isinstance(event, WsPriceChange):
                    ob.apply_change(event)
                    await self._notify_book(asset_id, ob)
                elif isinstance(event, WsLastTrade):
                    await self._notify_trade(event)

                yield event
        finally:
            # 显式关闭 WS 流（触发 websockets 上下文清理）与 REST 客户端，
            # 避免事件循环退出时异步生成器被 GC 强杀产生告警
            await stream.aclose()
            if self._owns_rest and self._rest is not None:
                await self._rest.close()
