"""ClobBroker：Broker 协议的 live 实现（阶段 1 commit B）。

薄包装 :class:`ClobTrader`（认证/回执解析保持原样），只加两件事：
- 实现 Broker 协议，让 orchestrator 与 paper 路径同构；
- on_order 钩子触发——阶段 2 SQLite ``orders`` 表的统一写入点，
  live 与 paper 共用同一落库通道，杜绝两套记录漂移。
"""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal

from pm_arb.data.orderbook import LocalOrderBook
from pm_arb.execution.broker import OrderHooks
from pm_arb.execution.clob_trader import ClobTrader
from pm_arb.execution.orders import Order, Side

GetBooks = Callable[[], dict[str, LocalOrderBook]]


class ClobBroker:
    """live 下单通道（真实资金）。"""

    def __init__(self, trader: ClobTrader) -> None:
        self._trader = trader
        self.hooks = OrderHooks()

    def bind_books(self, get_books: GetBooks) -> None:
        """live 成交由 CLOB 撮合，无需本地簿——空操作保协议一致。"""

    async def place_market(
        self,
        token_id: str,
        side: Side,
        amount: Decimal,
        *,
        ref_price: Decimal | None = None,
    ) -> Order:
        order = await self._trader.place_market(token_id, side, amount, ref_price=ref_price)
        await self.hooks.emit(order)
        return order

    async def cancel_all(self) -> int:
        return await self._trader.cancel_all()
