"""本地 L2 订单簿：应用 REST 快照与 WS 增量，维护实时盘口状态。

状态机::

    (空) --apply_snapshot--> 就绪
    就绪 --apply_change--> 就绪（size=0 撤档；否则更新/新增档位）

WS 断连重连后必须重新 apply_snapshot（由 MarketDataFeed 负责），
否则增量可能已过期。
"""

from __future__ import annotations

from decimal import Decimal

from pm_arb.data.models import BookLevel, OrderBook, WsBookEvent, WsPriceChange
from pm_arb.infra.logging import get_logger

log = get_logger(__name__)


class LocalOrderBook:
    """单 token 的实时订单簿。"""

    def __init__(self, asset_id: str):
        self.asset_id = asset_id
        self._book = OrderBook(asset_id=asset_id)
        self._ready = False

    @property
    def ready(self) -> bool:
        return self._ready

    @property
    def snapshot(self) -> OrderBook:
        return self._book

    def apply_snapshot(self, event: WsBookEvent | OrderBook) -> None:
        """全量覆盖（WS book 消息或 REST /book 响应）。"""
        if isinstance(event, OrderBook):
            book = event
        else:
            book = OrderBook(
                asset_id=event.asset_id,
                bids={lvl.price: lvl.size for lvl in event.bids},
                asks={lvl.price: lvl.size for lvl in event.asks},
                timestamp=event.timestamp,
                hash=event.hash,
            )
        self._book = book
        self._ready = True
        log.debug(
            "ob_snapshot",
            token=self.asset_id[:10],
            bids=len(book.bids),
            asks=len(book.asks),
        )

    def apply_change(self, change: WsPriceChange) -> None:
        """应用一档增量。side="BUY" 为 bid 侧，"SELL" 为 ask 侧。"""
        if not self._ready:
            # 尚未收到快照就来了增量——丢弃，等待快照（feed 层会主动拉取）
            log.warning("ob_change_before_snapshot", token=self.asset_id[:10])
            return

        side = self._book.bids if change.side.upper() == "BUY" else self._book.asks
        if change.is_removal:
            side.pop(change.price, None)
        else:
            side[change.price] = change.size

        self._book.timestamp = change.timestamp or self._book.timestamp
        self._book.hash = change.hash or self._book.hash

    # ---- 只读访问 ----

    @property
    def best_bid(self) -> BookLevel | None:
        return self._book.best_bid

    @property
    def best_ask(self) -> BookLevel | None:
        return self._book.best_ask

    @property
    def spread(self) -> Decimal | None:
        return self._book.spread

    @property
    def midpoint(self) -> Decimal | None:
        return self._book.midpoint

    def top_levels(self, depth: int = 5):
        return self._book.top_levels(depth)
