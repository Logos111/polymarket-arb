"""PaperBroker 模拟成交测试。"""

from decimal import Decimal

from pm_arb.data.models import BookLevel, WsBookEvent
from pm_arb.data.orderbook import LocalOrderBook
from pm_arb.execution.orders import OrderType, Side
from pm_arb.execution.paper import PaperBroker


def _book(asset: str, bids: list[tuple[str, str]], asks: list[tuple[str, str]]) -> LocalOrderBook:
    ob = LocalOrderBook(asset)
    ob.apply_snapshot(
        WsBookEvent(
            asset_id=asset,
            bids=[BookLevel(price=Decimal(p), size=Decimal(s)) for p, s in bids],
            asks=[BookLevel(price=Decimal(p), size=Decimal(s)) for p, s in asks],
        )
    )
    return ob


def test_taker_buy_fills_at_ask():
    broker = PaperBroker()
    book = _book("tok1", bids=[("0.59", "100")], asks=[("0.61", "50"), ("0.62", "100")])
    o = broker.submit_limit("tok1", Side.BUY, Decimal("0.62"), Decimal("80"), OrderType.GTC)
    fills = broker.on_book("tok1", book)
    # 50 @0.61 + 30 @0.62
    assert len(fills) == 2
    assert fills[0].price == Decimal("0.61") and fills[0].size == Decimal("50")
    assert fills[1].price == Decimal("0.62") and fills[1].size == Decimal("30")
    assert o.is_filled
    assert broker.position("tok1") == Decimal("80")


def test_fok_rejected_when_depth_insufficient():
    broker = PaperBroker()
    book = _book("tok1", bids=[("0.59", "100")], asks=[("0.61", "50")])
    o = broker.submit_limit("tok1", Side.BUY, Decimal("0.61"), Decimal("80"), OrderType.FOK)
    fills = broker.on_book("tok1", book)
    assert fills == []
    assert o.status.value == "CANCELED"


def test_fok_fills_when_depth_sufficient():
    broker = PaperBroker()
    book = _book("tok1", bids=[("0.59", "100")], asks=[("0.61", "80")])
    o = broker.submit_limit("tok1", Side.BUY, Decimal("0.61"), Decimal("80"), OrderType.FOK)
    fills = broker.on_book("tok1", book)
    assert len(fills) == 1
    assert o.is_filled


def test_maker_buy_rests_then_crosses():
    broker = PaperBroker()
    # 初始盘口 ask 0.65，买 0.60 不穿越 → 挂单
    book = _book("tok1", bids=[("0.59", "100")], asks=[("0.65", "40")])
    o = broker.submit_limit("tok1", Side.BUY, Decimal("0.60"), Decimal("40"), OrderType.GTC)
    assert broker.on_book("tok1", book) == []
    assert o.status.is_open

    # 盘口下移，ask 0.59 穿越挂单价 0.60 → 成交
    book2 = _book("tok1", bids=[("0.58", "100")], asks=[("0.59", "40")])
    fills = broker.on_book("tok1", book2)
    assert len(fills) == 1
    assert fills[0].price == Decimal("0.59")  # 按更优的 ask 成交
    assert o.is_filled


def test_taker_sell_fills_at_bid():
    broker = PaperBroker()
    book = _book("tok1", bids=[("0.40", "60")], asks=[("0.42", "100")])
    o = broker.submit_limit("tok1", Side.SELL, Decimal("0.40"), Decimal("60"), OrderType.FAK)
    fills = broker.on_book("tok1", book)
    assert len(fills) == 1
    assert fills[0].price == Decimal("0.40")
    assert o.is_filled
    assert broker.position("tok1") == Decimal("-60")
    assert broker.cash == Decimal("24.00")


def test_fak_remainder_canceled():
    broker = PaperBroker()
    book = _book("tok1", bids=[("0.40", "30")], asks=[("0.42", "100")])
    o = broker.submit_limit("tok1", Side.SELL, Decimal("0.40"), Decimal("60"), OrderType.FAK)
    fills = broker.on_book("tok1", book)
    assert len(fills) == 1
    assert fills[0].size == Decimal("30")
    assert o.status.value == "CANCELED"  # 剩余 30 撤单
