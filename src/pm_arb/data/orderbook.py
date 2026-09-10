"""本地 L2 订单簿：应用 REST 快照与 WS 增量，维护实时盘口状态。

状态机::

    (空) --apply_snapshot--> 就绪
    就绪 --apply_change--> 就绪（size=0 撤档；否则更新/新增档位）

WS 断连重连后必须重新 apply_snapshot（由 MarketDataFeed 负责），
否则增量可能已过期。
"""

from __future__ import annotations

import time
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
        # 本地收到该簿任何更新（快照/增量）的单调时钟时间，用于陈旧度判定：
        # 用 monotonic 而非 time.time()，免受系统时钟回拨/校时影响。
        # WS 静默时 age() 持续增大，上层据此回退 REST，避免抱着僵尸盘口。
        self._last_update: float = 0.0

    @property
    def ready(self) -> bool:
        return self._ready

    def age(self) -> float:
        """距上次更新的秒数（单调时钟）；从未更新返回 +inf。"""
        if not self._last_update:
            return float("inf")
        return time.monotonic() - self._last_update

    def is_fresh(self, max_age: float) -> bool:
        """就绪且距上次更新不超过 ``max_age`` 秒才算新鲜。

        ``ready`` 是单向锁（收到首个快照即永久 True），仅凭它无法识别
        “WS 静默/订阅失效但连接未断”导致的冻结盘口；必须叠加新鲜度判断。
        """
        return self._ready and self.age() < max_age

    def invalidate(self) -> None:
        """主动置为未就绪（断连时调用）：让上层立即回退 REST 而非硬扛旧数据。"""
        self._ready = False
        self._last_update = 0.0

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
        self._last_update = time.monotonic()
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
        self._last_update = time.monotonic()

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
