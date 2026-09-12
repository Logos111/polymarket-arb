"""Broker 协议 + paper 市价撮合/结算 测试（阶段 1 commit B）。"""

from decimal import Decimal

from pm_arb.data.models import OrderBook
from pm_arb.data.orderbook import LocalOrderBook
from pm_arb.execution.clob_broker import ClobBroker
from pm_arb.execution.orders import Order, OrderStatus, OrderType, Side
from pm_arb.execution.paper import PaperBroker
from pm_arb.execution.paper_runner import PaperRunner

TOKEN = "0xtok"


def make_book(bids: dict, asks: dict) -> LocalOrderBook:
    ob = LocalOrderBook(TOKEN)
    ob.apply_snapshot(OrderBook(
        asset_id=TOKEN,
        bids={Decimal(k): Decimal(v) for k, v in bids.items()},
        asks={Decimal(k): Decimal(v) for k, v in asks.items()},
        timestamp="", hash="",
    ))
    return ob


# ---- PaperBroker 市价撮合 ----

def test_market_buy_walks_levels_vwap():
    pb = PaperBroker()
    o = pb.submit_market_buy(TOKEN, Decimal("5.60"), make_book({}, {"0.28": 10, "0.29": 100}))
    # est = 5.60/0.28 = 20 份：吃 10@0.28 + 10@0.29
    assert o.status is OrderStatus.FILLED
    assert o.filled_size == Decimal(20)
    assert o.avg_fill_price == Decimal("0.285")
    assert pb.positions[TOKEN] == Decimal(20)
    assert pb.cash == Decimal("-5.70")


def test_market_buy_partial_when_depth_short():
    pb = PaperBroker()
    o = pb.submit_market_buy(TOKEN, Decimal("5.60"), make_book({}, {"0.28": 3}))
    # est 20 份但档深只有 3 → FAK 部分成交，停在 PARTIAL（与 live 口径一致）
    assert o.status is OrderStatus.PARTIAL
    assert o.filled_size == Decimal(3)
    assert o.avg_fill_price == Decimal("0.28")


def test_market_buy_no_book_fails_conservatively():
    pb = PaperBroker()
    o = pb.submit_market_buy(TOKEN, Decimal("2.00"), None)
    assert o.status is OrderStatus.FAILED
    assert o.filled_size == 0
    assert not pb.fills


def test_market_sell_walks_bids():
    pb = PaperBroker()
    pb.submit_market_buy(TOKEN, Decimal("5.60"), make_book({}, {"0.28": 100}))
    o = pb.submit_market_sell(TOKEN, Decimal(20), make_book({"0.30": 5, "0.29": 100}, {}))
    assert o.status is OrderStatus.FILLED
    assert o.filled_size == Decimal(20)
    assert o.avg_fill_price == Decimal("0.2925")  # 5@0.30 + 15@0.29
    assert pb.positions[TOKEN] == 0
    # cash：买 -5.60 + 卖 5.85
    assert pb.cash == Decimal("-5.60") + Decimal("5.85")


def test_settle_win_and_lose():
    pb = PaperBroker()
    pb.submit_market_buy(TOKEN, Decimal("5.60"), make_book({}, {"0.28": 100}))
    assert pb.settle(TOKEN, won=False) == 0
    assert pb.positions[TOKEN] == 0  # 输方归零
    pb.submit_market_buy(TOKEN, Decimal("5.60"), make_book({}, {"0.28": 100}))
    assert pb.settle(TOKEN, won=True) == Decimal(20)  # 赢方 $1/份
    assert pb.cash == Decimal("-11.20") + Decimal(20)
    assert pb.settle(TOKEN, won=True) == 0  # 重复结算无效


# ---- PaperRunner（async 适配 + 钩子）----

async def test_paper_runner_place_market_and_hook():
    runner = PaperRunner()
    seen: list[Order] = []
    runner.hooks.add(seen.append)
    runner.bind_books(lambda: {TOKEN: make_book({}, {"0.28": 100})})
    o = await runner.place_market(TOKEN, Side.BUY, Decimal("2.00"))
    assert o.status is OrderStatus.FILLED
    assert o.filled_size == Decimal("7.142857142857142857142857143")
    assert len(seen) == 1 and seen[0] is o
    # 未 bind 的 token → 保守 FAILED
    o2 = await runner.place_market("0xother", Side.BUY, Decimal("2.00"))
    assert o2.status is OrderStatus.FAILED
    assert len(seen) == 2


async def test_paper_runner_settle_passthrough():
    runner = PaperRunner()
    runner.bind_books(lambda: {TOKEN: make_book({}, {"0.28": 100})})
    await runner.place_market(TOKEN, Side.BUY, Decimal("2.00"))
    assert runner.settle(TOKEN, True) > 0
    assert runner.paper.cash + runner.settle("x", True) == runner.paper.cash


# ---- ClobBroker（钩子装配，trader 用桩替换）----

class _StubTrader:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def place_market(self, token_id, side, amount, *, ref_price=None, order_type=None):
        self.calls.append((token_id, side, amount, ref_price))
        o = Order(token_id=token_id, side=side, order_type=OrderType.FAK,
                  price=ref_price or Decimal("0.28"), size=Decimal(8))
        o.mark_submitted("stub-1")
        o.apply_fill(Decimal(8), ref_price or Decimal("0.28"))
        return o

    async def cancel_all(self) -> int:
        return 0


async def test_clob_broker_delegates_and_emits_hook():
    stub = _StubTrader()
    broker = ClobBroker(stub)  # type: ignore[arg-type]
    seen: list[Order] = []
    broker.hooks.add(seen.append)
    broker.bind_books(lambda: {})  # live 空操作，不应抛错
    o = await broker.place_market(TOKEN, Side.BUY, Decimal("2.00"), ref_price=Decimal("0.28"))
    assert o.status is OrderStatus.FILLED
    assert stub.calls == [(TOKEN, Side.BUY, Decimal("2.00"), Decimal("0.28"))]
    assert len(seen) == 1


async def test_clob_broker_hook_even_on_failure():
    class _FailTrader(_StubTrader):
        async def place_market(self, token_id, side, amount, *, ref_price=None, order_type=None):
            o = Order(token_id=token_id, side=side, price=Decimal(0), size=Decimal(0))
            o.mark_failed("boom")
            return o

    broker = ClobBroker(_FailTrader())  # type: ignore[arg-type]
    seen: list[Order] = []
    broker.hooks.add(seen.append)
    o = await broker.place_market(TOKEN, Side.BUY, Decimal("2.00"))
    assert o.status is OrderStatus.FAILED
    assert len(seen) == 1  # 失败单也进钩子（阶段 2 落库含失败记录）
