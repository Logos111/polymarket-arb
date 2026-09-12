"""Broker Protocol：策略层与执行层之间的统一下单接口（阶段 1 commit B）。

实盘（:class:`~pm_arb.execution.clob_broker.ClobBroker` 包装 ClobTrader）与
dry-run/回测（:class:`~pm_arb.execution.paper_runner.PaperRunner` 包装
PaperBroker）实现同一协议——orchestrator 只面向协议编程，不感知真实资金，
判定之外的下单/成交路径完全可替换。

on_order 钩子：每笔订单提交/有新成交后回调，阶段 2 SQLite ``orders`` 表的
统一写入点（DEV_PLAN 阶段 2；两实现共用 :class:`OrderHooks`）。
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from decimal import Decimal
from typing import Protocol

from pm_arb.data.orderbook import LocalOrderBook
from pm_arb.execution.orders import Order, Side


class Broker(Protocol):
    """策略层可见的最小执行接口（全部 async；live 的同步 SDK 已 to_thread 包装）。"""

    async def place_market(
        self,
        token_id: str,
        side: Side,
        amount: Decimal,
        *,
        ref_price: Decimal | None = None,
    ) -> Order:
        """市价单（FAK 语义）：BUY amount=美元名义，SELL amount=份数。

        ref_price：调用方从本地盘口取的参考价（ask/bid），live 用它估算
        目标份数让 FILLED/PARTIAL 区分有意义（问题 4a），paper 用它兜底
        无本地簿时的成交价。
        """
        ...

    def bind_books(self, get_books: Callable[[], dict[str, LocalOrderBook]]) -> None:
        """给 paper 撮合提供当前本地簿（token_id → LocalOrderBook）；live 空操作。"""
        ...

    async def cancel_all(self) -> int:
        """撤销所有挂单，返回（尝试）撤销数量。"""
        ...


OrderHook = Callable[[Order], Awaitable[None] | None]


class OrderHooks:
    """on_order 钩子注册与触发（ClobBroker / PaperRunner 共用）。"""

    def __init__(self) -> None:
        self._hooks: list[OrderHook] = []

    def add(self, hook: OrderHook) -> None:
        self._hooks.append(hook)

    async def emit(self, order: Order) -> None:
        for hook in self._hooks:
            out = hook(order)
            if inspect.isawaitable(out):
                await out
