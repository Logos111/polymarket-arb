"""订单领域模型与状态机。

策略层与执行层之间统一使用本模块的 ``Order`` 对象，不直接暴露
py-clob-client 的数据结构，便于 paper trading 与回测复用同一套逻辑。
"""

from __future__ import annotations

import enum
import uuid
from datetime import UTC, datetime
from decimal import Decimal

from pydantic import BaseModel, Field


class Side(enum.StrEnum):
    BUY = "BUY"
    SELL = "SELL"

    @property
    def opposite(self) -> Side:
        return Side.SELL if self is Side.BUY else Side.BUY


class OrderType(enum.StrEnum):
    GTC = "GTC"  # 挂单，成交前一直有效（做市/挂单腿）
    FOK = "FOK"  # 全部成交或立即取消（套利吃单首选，杜绝腿风险）
    FAK = "FAK"  # 部分成交，剩余取消（IOC）


class OrderStatus(enum.StrEnum):
    PENDING = "PENDING"        # 本地已创建，尚未提交
    SUBMITTED = "SUBMITTED"    # 交易所已接受
    PARTIAL = "PARTIAL"        # 部分成交
    FILLED = "FILLED"          # 全部成交（终态）
    CANCELED = "CANCELED"      # 已撤销（终态）
    REJECTED = "REJECTED"      # 被交易所拒绝（终态）
    FAILED = "FAILED"          # 提交失败/异常（终态）

    @property
    def is_terminal(self) -> bool:
        return self in (OrderStatus.FILLED, OrderStatus.CANCELED,
                        OrderStatus.REJECTED, OrderStatus.FAILED)

    @property
    def is_open(self) -> bool:
        return self in (OrderStatus.SUBMITTED, OrderStatus.PARTIAL)


# 合法状态迁移
_TRANSITIONS: dict[OrderStatus, set[OrderStatus]] = {
    OrderStatus.PENDING: {
        OrderStatus.SUBMITTED, OrderStatus.REJECTED, OrderStatus.FAILED, OrderStatus.CANCELED
    },
    OrderStatus.SUBMITTED: {
        OrderStatus.PARTIAL, OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.FAILED
    },
    OrderStatus.PARTIAL: {
        OrderStatus.PARTIAL, OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.FAILED
    },
}


class Order(BaseModel):
    """一张订单的完整生命周期记录。"""

    client_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:16])
    exchange_id: str | None = None

    token_id: str
    side: Side
    order_type: OrderType = OrderType.GTC
    price: Decimal
    size: Decimal

    filled_size: Decimal = Decimal(0)
    avg_fill_price: Decimal | None = None
    post_only: bool = False

    status: OrderStatus = OrderStatus.PENDING
    error: str | None = None

    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    def _transition(self, new: OrderStatus) -> None:
        allowed = _TRANSITIONS.get(self.status, set())
        if new not in allowed and new != self.status:
            raise IllegalTransition(f"{self.status} -> {new} (client_id={self.client_id})")
        self.status = new
        self.updated_at = datetime.now(UTC)

    # ---- 状态迁移入口 ----

    def mark_submitted(self, exchange_id: str) -> None:
        self.exchange_id = exchange_id
        self._transition(OrderStatus.SUBMITTED)

    def apply_fill(self, fill_size: Decimal, fill_price: Decimal) -> None:
        """记录一笔成交。

        FILLED 判定用 filled_size >= size：调用方必须保证市价单在下单前
        已填入目标份数（如 place_market 的 ref_price 估算），否则 size=0
        的订单任何成交都会被判 FILLED（问题 4a 的历史事故）。size=0
        是“目标未知市价单”的兼容兑底：成交即视作 FILLED；阶段 2 落库
        一律用 filled_size 与目标名义对比计算成交比例，不依赖 status。
        """
        fill_size = Decimal(fill_size)
        if fill_size <= 0:
            return
        total_cost = (self.avg_fill_price or 0) * self.filled_size + fill_price * fill_size
        self.filled_size += fill_size
        self.avg_fill_price = total_cost / self.filled_size
        if self.filled_size >= self.size:
            self._transition(OrderStatus.FILLED)
        else:
            self._transition(OrderStatus.PARTIAL)

    def mark_canceled(self) -> None:
        self._transition(OrderStatus.CANCELED)

    def mark_rejected(self, error: str) -> None:
        self.error = error[:300]
        self._transition(OrderStatus.REJECTED)

    def mark_failed(self, error: str) -> None:
        self.error = error[:300]
        self._transition(OrderStatus.FAILED)

    # ---- 派生量 ----

    @property
    def remaining_size(self) -> Decimal:
        return max(self.size - self.filled_size, Decimal(0))

    @property
    def is_filled(self) -> bool:
        return self.status is OrderStatus.FILLED

    @property
    def notional(self) -> Decimal:
        """挂单名义金额（价格 × 数量）。"""
        return self.price * self.size


class IllegalTransition(RuntimeError):
    """非法订单状态迁移。"""
