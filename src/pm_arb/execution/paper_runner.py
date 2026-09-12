"""PaperRunner：Broker 协议的 paper 实现（dry-run / 回测，阶段 1 commit B）。

把同步的 :class:`PaperBroker` 适配成 async Broker 协议 + on_order 钩子：
- 市价单沿真实本地簿逐档撮合（本地簿由 orchestrator 每窗口经
  ``bind_books`` 注入 MarketDataFeed.books）；
- 无本地簿/无档位时保守 FAILED，不虚构成交；
- ``settle`` 供窗口结算时兑付（$1/份、$0），cash 账本闭环可对账。
"""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal

from pm_arb.data.orderbook import LocalOrderBook
from pm_arb.execution.broker import OrderHooks
from pm_arb.execution.orders import Order, Side
from pm_arb.execution.paper import PaperBroker

GetBooks = Callable[[], dict[str, LocalOrderBook]]


class PaperRunner:
    """PaperBroker 的 async 适配器，实现 Broker 协议。"""

    def __init__(self, paper: PaperBroker | None = None) -> None:
        self.paper = paper or PaperBroker()
        self.hooks = OrderHooks()
        self._get_books: GetBooks = dict  # bind 前视为空簿集

    def bind_books(self, get_books: GetBooks) -> None:
        """注入当前窗口的本地簿访问器（每窗口开始时由 orchestrator 调用）。"""
        self._get_books = get_books

    async def place_market(
        self,
        token_id: str,
        side: Side,
        amount: Decimal,
        *,
        ref_price: Decimal | None = None,
    ) -> Order:
        book = self._get_books().get(token_id)
        if side is Side.BUY:
            order = self.paper.submit_market_buy(token_id, amount, book)
        else:
            order = self.paper.submit_market_sell(token_id, amount, book)
        await self.hooks.emit(order)
        return order

    def settle(self, token_id: str, won: bool) -> Decimal:
        """窗口结算兑付（透传 PaperBroker.settle）。"""
        return self.paper.settle(token_id, won)

    async def cancel_all(self) -> int:
        n = 0
        for o in list(self.paper.orders.values()):
            if self.paper.cancel(o.client_id):
                n += 1
        return n
