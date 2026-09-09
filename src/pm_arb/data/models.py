"""数据层 Pydantic 模型：市场元数据、盘口、WebSocket 事件。

价格与数量统一使用 ``Decimal``——套利利润计算对浮点误差零容忍
（0.1+0.2 这类问题在 $1 定价边界上会直接导致错误信号）。
"""

from __future__ import annotations

import json
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Token(BaseModel):
    """一个可交易结果（YES 或 NO；NegRisk 市场下为某候选人）。"""

    token_id: str
    outcome: str  # "Yes" / "No" / 候选人名等

    def __str__(self) -> str:
        return f"{self.outcome}({self.token_id[:8]}…)"


class Market(BaseModel):
    """Gamma API 的市场元数据（字段做了宽松映射，多余字段忽略）。"""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: str
    question: str = ""
    slug: str = ""
    condition_id: str = Field(default="", alias="conditionId")
    neg_risk: bool = Field(default=False, alias="negRisk")
    active: bool = True
    closed: bool = False
    # Gamma 中这两个字段是 JSON 编码字符串：'["Yes","No"]' / '["id1","id2"]'
    outcomes: list[str] = Field(default_factory=list)
    clob_token_ids: list[str] = Field(default_factory=list, alias="clobTokenIds")
    volume: Decimal = Decimal(0)
    liquidity: Decimal = Field(default=Decimal(0), alias="liquidityNum")
    event_slug: str = ""

    @field_validator("outcomes", "clob_token_ids", mode="before")
    @classmethod
    def _parse_json_list(cls, v):
        """Gamma 返回的是 JSON 字符串，自动解析为列表。"""
        if isinstance(v, str):
            try:
                return json.loads(v)
            except json.JSONDecodeError:
                return []
        return v or []

    @property
    def tokens(self) -> list[Token]:
        """outcomes 与 clob_token_ids 按位置配对。"""
        return [
            Token(token_id=tid, outcome=out)
            for tid, out in zip(self.clob_token_ids, self.outcomes, strict=False)
        ]

    @property
    def is_tradeable(self) -> bool:
        return self.active and not self.closed and len(self.clob_token_ids) >= 2


class BookLevel(BaseModel):
    """订单簿一档。"""

    price: Decimal
    size: Decimal


class OrderBook(BaseModel):
    """某 token 的 L2 订单簿快照。

    bids/asks 用 ``price -> size`` 字典存储；访问最优价用属性方法，
    避免依赖字典顺序。
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    asset_id: str
    bids: dict[Decimal, Decimal] = Field(default_factory=dict)
    asks: dict[Decimal, Decimal] = Field(default_factory=dict)
    timestamp: str = ""
    hash: str = ""

    @property
    def best_bid(self) -> BookLevel | None:
        if not self.bids:
            return None
        p = max(self.bids)
        return BookLevel(price=p, size=self.bids[p])

    @property
    def best_ask(self) -> BookLevel | None:
        if not self.asks:
            return None
        p = min(self.asks)
        return BookLevel(price=p, size=self.asks[p])

    @property
    def spread(self) -> Decimal | None:
        ba, bb = self.best_ask, self.best_bid
        if ba is None or bb is None:
            return None
        return ba.price - bb.price

    @property
    def midpoint(self) -> Decimal | None:
        ba, bb = self.best_ask, self.best_bid
        if ba is None or bb is None:
            return None
        return (ba.price + bb.price) / 2

    def top_levels(self, depth: int = 5) -> tuple[list[BookLevel], list[BookLevel]]:
        """返回前 depth 档（bids 价格降序、asks 升序）。"""
        bid_lvls = [
            BookLevel(price=p, size=self.bids[p])
            for p in sorted(self.bids, reverse=True)[:depth]
        ]
        ask_lvls = [BookLevel(price=p, size=self.asks[p]) for p in sorted(self.asks)[:depth]]
        return bid_lvls, ask_lvls


# ---------------- WebSocket 事件 ----------------


class WsBookEvent(BaseModel):
    """book 快照消息（订阅后首条 / 重连后首条）。"""

    asset_id: str
    market: str = ""
    bids: list[BookLevel]
    asks: list[BookLevel]
    timestamp: str = ""
    hash: str = ""


class WsPriceChange(BaseModel):
    """price_change 增量消息。size=0 表示该档被撤销。"""

    asset_id: str
    market: str = ""
    side: str  # "BUY"(bid) / "SELL"(ask)
    price: Decimal
    size: Decimal
    timestamp: str = ""
    hash: str = ""

    @property
    def is_removal(self) -> bool:
        return self.size == 0


class WsLastTrade(BaseModel):
    """last_trade_price 成交通知。"""

    asset_id: str
    market: str = ""
    price: Decimal
    size: Decimal
    side: str = ""
    timestamp: str = ""
    fee_rate_bps: str = ""
