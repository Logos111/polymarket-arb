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
