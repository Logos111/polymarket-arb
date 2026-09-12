"""订单状态机测试。"""

from decimal import Decimal

import pytest

from pm_arb.execution.orders import IllegalTransition, Order, OrderStatus, OrderType, Side


def _order() -> Order:
    return Order(
        token_id="tok1",
        side=Side.BUY,
        order_type=OrderType.GTC,
        price=Decimal("0.60"),
        size=Decimal("100"),
    )


def test_new_order_pending():
    o = _order()
    assert o.status is OrderStatus.PENDING
    assert o.remaining_size == Decimal("100")
    assert o.client_id


def test_full_fill_lifecycle():
    o = _order()
    o.mark_submitted("ex-1")
    assert o.status is OrderStatus.SUBMITTED
    o.apply_fill(Decimal("100"), Decimal("0.599"))
    assert o.status is OrderStatus.FILLED
    assert o.filled_size == Decimal("100")
    assert o.avg_fill_price == Decimal("0.599")
    assert o.remaining_size == 0
    assert o.is_filled


def test_partial_then_full_vwap():
    o = _order()
    o.mark_submitted("ex-1")
    o.apply_fill(Decimal("40"), Decimal("0.60"))
    assert o.status is OrderStatus.PARTIAL
    o.apply_fill(Decimal("60"), Decimal("0.58"))
    assert o.status is OrderStatus.FILLED
    # VWAP = (40*0.60 + 60*0.58)/100 = 0.588
    assert o.avg_fill_price == Decimal("0.588")


def test_cancel_and_reject():
    o = _order()
    o.mark_submitted("ex-1")
    o.mark_canceled()
    assert o.status is OrderStatus.CANCELED
    assert o.status.is_terminal

    o2 = _order()
    o2.mark_rejected("insufficient allowance")
    assert o2.status is OrderStatus.REJECTED
    assert "allowance" in o2.error


def test_illegal_transition_rejected():
    o = _order()  # PENDING
    o.mark_submitted("ex-1")
    o.apply_fill(Decimal("100"), Decimal("0.6"))  # FILLED（终态）
    with pytest.raises(IllegalTransition):
        o.mark_canceled()


def test_side_opposite():
    assert Side.BUY.opposite is Side.SELL
    assert Side.SELL.opposite is Side.BUY


def test_notional():
    o = _order()
    assert o.notional == Decimal("60.00")


def test_market_order_zero_size_fill_fallback():
    """问题 4a 兼容兑底：size=0（目标未知市价单）成交即 FILLED。"""
    o = Order(token_id="tok1", side=Side.BUY, order_type=OrderType.FAK,
              price=Decimal(0), size=Decimal(0))
    o.mark_submitted("ex-1")
    o.apply_fill(Decimal("3.0"), Decimal("0.28"))
    assert o.status is OrderStatus.FILLED
    assert o.filled_size == Decimal("3.0")


# ---- place_market 目标份数估算（问题 4a 修复）----


class _StubMarketClient:
    """模拟 create_and_post_market_order：返回固定回执。"""

    def __init__(self, resp: dict):
        self._resp = resp
        self.last_args = None

    def create_and_post_market_order(self, args, options, order_type):
        self.last_args = args
        return self._resp


def _trader_with(stub: _StubMarketClient):
    """构造 ClobTrader 并替换底层 client（不触发网络/认证）。"""
    from pm_arb.execution.clob_trader import ClobTrader

    trader = ClobTrader.__new__(ClobTrader)  # 跳过 __init__（不建真实 client）
    trader._client = stub
    return trader


_MATCHED_FULL = {
    "success": True, "orderID": "0xabc", "status": "matched",
    "makingAmount": "2.000000", "takingAmount": "7.142858",
}


async def test_place_market_buy_estimates_size_from_ref_price():
    """BUY + ref_price：目标份数 = amount/ref_price，全量成交 → FILLED。"""
    from pm_arb.execution.clob_trader import ClobTrader

    stub = _StubMarketClient(_MATCHED_FULL)
    trader = _trader_with(stub)
    o = await ClobTrader.place_market(
        trader, "tok1", Side.BUY, Decimal("2.00"),
        ref_price=Decimal("0.28"),
    )
    # 目标份数 = 2.00/0.28 ≈ 7.142857；回执成交 7.142858 ≥ 目标 → FILLED
    assert o.size > Decimal("7.14")
    assert o.status.value == "FILLED"
    assert o.filled_size == Decimal("7.142858")
    assert o.avg_fill_price == Decimal("2.000000") / Decimal("7.142858")


async def test_place_market_buy_partial_fill_detected():
    """BUY 部分成交：filled < 目标份数 → PARTIAL（修复前会误判 FILLED）。"""
    from pm_arb.execution.clob_trader import ClobTrader

    stub = _StubMarketClient({"success": True, "orderID": "0xabc",
                              "status": "matched",
                              "makingAmount": "1.000000",
                              "takingAmount": "3.000000"})
    trader = _trader_with(stub)
    o = await ClobTrader.place_market(
        trader, "tok1", Side.BUY, Decimal("2.00"),
        ref_price=Decimal("0.28"),
    )
    assert o.filled_size == Decimal("3.000000")
    assert o.status.value == "PARTIAL"


async def test_place_market_sell_size_is_amount():
    """SELL：amount 本身即份数，size 精确等于 amount。"""
    from pm_arb.execution.clob_trader import ClobTrader

    stub = _StubMarketClient({"success": True, "orderID": "0xdef",
                              "status": "matched",
                              "makingAmount": "7.142858",
                              "takingAmount": "4.642857"})
    trader = _trader_with(stub)
    o = await ClobTrader.place_market(
        trader, "tok1", Side.SELL, Decimal("7.142858"),
        ref_price=Decimal("0.65"),
    )
    assert o.size == Decimal("7.142858")
    # SELL：making=份数 taking=美元；7.142858 份全部成交 → FILLED
    assert o.status.value == "FILLED"
    assert o.avg_fill_price == Decimal("4.642857") / Decimal("7.142858")


async def test_place_market_no_ref_price_backward_compat():
    """无 ref_price：size=0，成交即 FILLED（旧版行为兑底）。"""
    from pm_arb.execution.clob_trader import ClobTrader

    stub = _StubMarketClient(_MATCHED_FULL)
    trader = _trader_with(stub)
    o = await ClobTrader.place_market(trader, "tok1", Side.BUY, Decimal("2.00"))
    assert o.size == Decimal(0)
    assert o.status.value == "FILLED"
