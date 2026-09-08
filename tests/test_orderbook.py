"""本地订单簿：快照 + 增量更新逻辑测试。"""

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
