"""Paper trading：用实时本地订单簿模拟成交，零资金风险验证策略。

成交模型（保守偏乐观之间，适合套利可行性验证）：
- **吃单（提交时即穿越盘口）**：沿对手方档位逐档成交，按真实档位价
  VWAP 结算；FOK 若深度不足整单拒绝，FAK 成交可得部分、剩余撤销，
  GTC 剩余转为挂单；
- **挂单（价格在盘口外）**：进入订单簿，之后盘口穿越时成交
  （买入：best_ask <= 挂单价；卖出：best_bid >= 挂单价），按挂单价
  结算、数量受该档深度限制。忽略排队位置（偏乐观，paper 阶段可接受）。

同时维护简单账本：每个 token 的净头寸与现金变动，便于 PnL 核对。
"""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal

from pydantic import BaseModel

from pm_arb.data.models import BookLevel
from pm_arb.data.orderbook import LocalOrderBook
from pm_arb.execution.orders import Order, OrderType, Side


class Fill(BaseModel):
    order_id: str
    client_id: str
    token_id: str
    side: Side
    price: Decimal
    size: Decimal


class PaperBroker:
    def __init__(self) -> None:
        self.orders: dict[str, Order] = {}
        self.fills: list[Fill] = []
        self.positions: dict[str, Decimal] = defaultdict(Decimal)  # token -> 净份数
        self.cash: Decimal = Decimal(0)  # 买入为负、卖出为正

    # ---- 下单 ----

    def submit_limit(
        self,
        token_id: str,
        side: Side,
        price: Decimal,
        size: Decimal,
        order_type: OrderType = OrderType.GTC,
    ) -> Order:
        order = Order(
            token_id=token_id,
            side=side,
            order_type=order_type,
            price=Decimal(price),
            size=Decimal(size),
        )
        self.orders[order.client_id] = order
        # 无盘口则挂起；等 on_book 到达后处理
        order.mark_submitted(f"paper-{order.client_id}")
        return order

    def cancel(self, client_id: str) -> bool:
        order = self.orders.get(client_id)
        if order and order.status.is_open:
            order.mark_canceled()
            return True
        return False

    # ---- 盘口驱动成交 ----

    def on_book(self, asset_id: str, book: LocalOrderBook) -> list[Fill]:
        """盘口更新时调用：处理新挂单的即时成交 + 存量挂单穿越成交。"""
        fills: list[Fill] = []
        for order in list(self.orders.values()):
            if order.token_id != asset_id or not order.status.is_open:
                continue
            fills.extend(self._try_fill(order, book))
        return fills

    def _try_fill(self, order: Order, book: LocalOrderBook) -> list[Fill]:
        snap = book.snapshot
        levels: list[BookLevel] = []
        if order.side is Side.BUY:
            # 对手方为 ask：取所有 ask <= 挂单价，价格升序
            levels = [
                BookLevel(price=p, size=snap.asks[p])
                for p in sorted(snap.asks)
                if p <= order.price
            ]
        else:
            # 对手方为 bid：取所有 bid >= 挂单价，价格降序
            levels = [
                BookLevel(price=p, size=snap.bids[p])
                for p in sorted(snap.bids, reverse=True)
                if p >= order.price
            ]

        if not levels:
            return []  # 盘口未穿越，继续挂单

        available = sum((lvl.size for lvl in levels), Decimal(0))
        need = order.remaining_size

        if order.order_type is OrderType.FOK and available < need:
            # FOK：深度不足，整单撤销（从未成交过）
            order.mark_canceled()
            return []

        # 逐档成交到 need 耗尽（GTC/FAK 成交可得部分）
        fills: list[Fill] = []
        for lvl in levels:
            if need <= 0:
                break
            take = min(need, lvl.size)
            # 吃单按档位价；挂单穿越按挂单价（限价保证）——取更优成交价
            fill_price = (
                min(lvl.price, order.price)
                if order.side is Side.BUY
                else max(lvl.price, order.price)
            )
            order.apply_fill(take, fill_price)
            fill = Fill(
                order_id=order.exchange_id or order.client_id,
                client_id=order.client_id,
                token_id=order.token_id,
                side=order.side,
                price=fill_price,
                size=take,
            )
            fills.append(fill)
            self._record_fill(fill)
            need -= take

        if order.status.is_open and order.order_type is OrderType.FAK:
            order.mark_canceled()  # IOC：剩余撤单
        return fills

    def _record_fill(self, fill: Fill) -> None:
        self.fills.append(fill)
        signed = fill.size if fill.side is Side.BUY else -fill.size
        self.positions[fill.token_id] += signed
        self.cash += fill.size * fill.price * (1 if fill.side is Side.SELL else -1)

    # ---- 市价吃单（阶段 1 commit B：dry-run 换 PaperBroker）----

    def submit_market_buy(
        self,
        token_id: str,
        notional: Decimal,
        book: LocalOrderBook | None = None,
    ) -> Order:
        """市价买 ``notional`` 美元：沿 ask 升序逐档吃（FAK）。

        目标份数按最优 ask 估算（与 live place_market 的 ref_price 口径
        一致，问题 4a）；实际成交沿真实档位 VWAP。无本地簿/无 ask 档
        则 FAILED（保守：不虚构成交）。不计 taker fee——与实盘
        realized_pnl 口径一致（价差）。
        """
        asks = sorted(book.snapshot.asks.items()) if book is not None else []
        best = asks[0][0] if asks else None
        est_size = notional / best if best else Decimal(0)
        order = Order(
            token_id=token_id, side=Side.BUY, order_type=OrderType.FAK,
            price=best or Decimal(0), size=est_size,
        )
        if not asks:
            order.mark_failed("paper: no ask liquidity")
            return order
        order.mark_submitted(f"paper-{order.client_id}")
        self._walk_and_fill(order, asks)
        return order

    def submit_market_sell(
        self,
        token_id: str,
        size: Decimal,
        book: LocalOrderBook | None = None,
    ) -> Order:
        """市价卖 ``size`` 份：沿 bid 降序逐档吃（FAK）。口径同上。"""
        bids = sorted(book.snapshot.bids.items(), reverse=True) if book is not None else []
        best = bids[0][0] if bids else None
        order = Order(
            token_id=token_id, side=Side.SELL, order_type=OrderType.FAK,
            price=best or Decimal(0), size=Decimal(size),
        )
        if not bids:
            order.mark_failed("paper: no bid liquidity")
            return order
        order.mark_submitted(f"paper-{order.client_id}")
        self._walk_and_fill(order, bids)
        return order

    def _walk_and_fill(self, order: Order, levels: list[tuple[Decimal, Decimal]]) -> None:
        """沿给定档位序列（已按吃单方向排序）逐档成交到 remaining 耗尽。

        终态与 live 市价单对齐：成交满 → FILLED，深度不足 → 停在
        PARTIAL（调用方按 filled_size 判断，同实盘口径），不标 CANCELED。
        """
        need = order.remaining_size
        for price, avail in levels:
            if need <= 0:
                break
            take = min(need, avail)
            order.apply_fill(take, price)
            self._record_fill(Fill(
                order_id=order.exchange_id or order.client_id,
                client_id=order.client_id,
                token_id=order.token_id,
                side=order.side,
                price=price,
                size=take,
            ))
            need -= take

    def settle(self, token_id: str, won: bool) -> Decimal:
        """窗口结算兑付：赢方 $1/份、输方 $0（持仓清零，现金入账）。"""
        shares = self.positions.pop(token_id, Decimal(0))
        payout = shares if won else Decimal(0)
        self.cash += payout
        return payout

    # ---- 汇总 ----

    def open_orders(self) -> list[Order]:
        return [o for o in self.orders.values() if o.status.is_open]

    def position(self, token_id: str) -> Decimal:
        return self.positions.get(token_id, Decimal(0))
