"""本地订单簿：快照 + 增量更新逻辑测试。"""

import time
from decimal import Decimal

from pm_arb.data.models import BookLevel, WsBookEvent, WsPriceChange
from pm_arb.data.orderbook import LocalOrderBook


def _snapshot() -> WsBookEvent:
    return WsBookEvent(
        asset_id="tok1",
        bids=[
            BookLevel(price=Decimal("0.60"), size=Decimal("100")),
            BookLevel(price=Decimal("0.59"), size=Decimal("200")),
        ],
        asks=[
            BookLevel(price=Decimal("0.62"), size=Decimal("150")),
            BookLevel(price=Decimal("0.63"), size=Decimal("300")),
        ],
    )


def test_not_ready_before_snapshot():
    ob = LocalOrderBook("tok1")
    assert ob.ready is False
    assert ob.best_bid is None
    # 快照前的增量应被丢弃，不抛异常
    ob.apply_change(
        WsPriceChange(asset_id="tok1", side="BUY", price=Decimal("0.61"), size=Decimal("10"))
    )
    assert ob.best_bid is None


def test_snapshot_best_prices_and_spread():
    ob = LocalOrderBook("tok1")
    ob.apply_snapshot(_snapshot())
    assert ob.ready is True
    assert ob.best_bid.price == Decimal("0.60")
    assert ob.best_ask.price == Decimal("0.62")
    assert ob.spread == Decimal("0.02")
    assert ob.midpoint == Decimal("0.61")


def test_change_updates_level():
    ob = LocalOrderBook("tok1")
    ob.apply_snapshot(_snapshot())

    # bid 侧加一档更优价格 → best_bid 更新
    ob.apply_change(
        WsPriceChange(asset_id="tok1", side="BUY", price=Decimal("0.61"), size=Decimal("50"))
    )
    assert ob.best_bid.price == Decimal("0.61")
    assert ob.best_bid.size == Decimal("50")


def test_change_removes_level_when_size_zero():
    ob = LocalOrderBook("tok1")
    ob.apply_snapshot(_snapshot())

    ob.apply_change(
        WsPriceChange(asset_id="tok1", side="SELL", price=Decimal("0.62"), size=Decimal("0"))
    )
    # 0.62 被撤销后，最优 ask 变为 0.63
    assert ob.best_ask.price == Decimal("0.63")


def test_change_modifies_existing_level():
    ob = LocalOrderBook("tok1")
    ob.apply_snapshot(_snapshot())

    ob.apply_change(
        WsPriceChange(asset_id="tok1", side="BUY", price=Decimal("0.60"), size=Decimal("999"))
    )
    assert ob.best_bid.size == Decimal("999")


def test_top_levels_ordering():
    ob = LocalOrderBook("tok1")
    ob.apply_snapshot(_snapshot())
    bids, asks = ob.top_levels(2)
    assert [lvl.price for lvl in bids] == [Decimal("0.60"), Decimal("0.59")]  # 降序
    assert [lvl.price for lvl in asks] == [Decimal("0.62"), Decimal("0.63")]  # 升序


def test_resnapshot_overwrites():
    ob = LocalOrderBook("tok1")
    ob.apply_snapshot(_snapshot())
    ob.apply_change(
        WsPriceChange(asset_id="tok1", side="BUY", price=Decimal("0.99"), size=Decimal("1"))
    )
    # 重新快照应完全覆盖旧状态
    ob.apply_snapshot(_snapshot())
    assert ob.best_bid.price == Decimal("0.60")
    assert Decimal("0.99") not in ob.snapshot.bids


# ---------------- 新鲜度（陈旧度）语义 ----------------


def test_age_infinite_before_any_update():
    """从未收到过快照：age=+inf、is_fresh=False（即使阈值很大）。"""
    ob = LocalOrderBook("tok1")
    assert ob.age() == float("inf")
    assert ob.is_fresh(1e9) is False


def test_fresh_right_after_snapshot():
    ob = LocalOrderBook("tok1")
    ob.apply_snapshot(_snapshot())
    assert ob.age() < 1.0
    assert ob.is_fresh(10.0) is True


def test_stale_after_max_age_while_still_ready():
    """核心回归：ready 是单向锁，WS 静默后仍为 True，但 is_fresh 必须转 False。

    这正是“长时间失真”的根因：仅凭 ready 判定会让上层永远抱着冻结盘口。
    """
    ob = LocalOrderBook("tok1")
    ob.apply_snapshot(_snapshot())
    # 模拟 WS 连接未断但长时间不推数据：把最后更新时间倒退到远超阈值
    ob._last_update = time.monotonic() - 999
    assert ob.ready is True             # 单向锁：依然“就绪”
    assert ob.is_fresh(10.0) is False   # 但已陈旧 → 上层应回退 REST


def test_change_refreshes_freshness():
    """一个增量就应刷新新鲜度（证明 WS 确实在推实时数据）。"""
    ob = LocalOrderBook("tok1")
    ob.apply_snapshot(_snapshot())
    ob._last_update = time.monotonic() - 999
    assert ob.is_fresh(10.0) is False
    ob.apply_change(
        WsPriceChange(asset_id="tok1", side="BUY", price=Decimal("0.61"), size=Decimal("5"))
    )
    assert ob.is_fresh(10.0) is True


def test_invalidate_marks_not_ready_and_stale():
    """断连时 invalidate：立即未就绪 + age 归 inf，上层马上回退 REST。"""
    ob = LocalOrderBook("tok1")
    ob.apply_snapshot(_snapshot())
    assert ob.is_fresh(10.0) is True
    ob.invalidate()
    assert ob.ready is False
    assert ob.age() == float("inf")
    assert ob.is_fresh(10.0) is False
