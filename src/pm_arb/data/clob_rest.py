"""CLOB REST 客户端：盘口快照、最新价、市场列表。

只读接口，无需 L1/L2 认证。认证后的交易接口在 execution 层实现。
"""

from __future__ import annotations

from decimal import Decimal

import httpx

from pm_arb.data.http import get_json, make_http_client
from pm_arb.data.models import BookLevel, OrderBook
from pm_arb.infra.config import Settings, get_settings
from pm_arb.infra.logging import get_logger

log = get_logger(__name__)


def _parse_levels(raw: list[dict]) -> dict[Decimal, Decimal]:
    """CLOB 返回 [{"price": "0.62", "size": "1500"}, ...] -> {price: size}。"""
    levels: dict[Decimal, Decimal] = {}
    for lvl in raw or []:
        price = Decimal(str(lvl["price"]))
        size = Decimal(str(lvl["size"]))
        if size > 0:
            levels[price] = size
    return levels


class ClobRestClient:
    def __init__(self, settings: Settings | None = None, client: httpx.AsyncClient | None = None):
        self._settings = settings or get_settings()
        self._client = client or make_http_client(self._settings.clob_api_url, self._settings)
        self._owns_client = client is None

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> ClobRestClient:
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    async def get_book(self, token_id: str) -> OrderBook:
        """拉取某 token 的全量 L2 订单簿（WS 重连后用它重建快照）。"""
        data = await get_json(self._client, "/book", params={"token_id": token_id})
        book = OrderBook(
            asset_id=str(data.get("asset_id") or token_id),
            bids=_parse_levels(data.get("bids")),
            asks=_parse_levels(data.get("asks")),
            timestamp=str(data.get("timestamp", "")),
            hash=str(data.get("hash", "")),
        )
        log.debug(
            "clob_book_fetched",
            token=token_id[:10],
            bids=len(book.bids),
            asks=len(book.asks),
        )
        return book

    async def get_price(self, token_id: str, side: str) -> Decimal:
        """最优价。side: "buy"（你能买到的最优 ask）/ "sell"（你能卖到的最优 bid）。"""
        data = await get_json(
            self._client, "/price", params={"token_id": token_id, "side": side.lower()}
        )
        return Decimal(str(data["price"]))

    async def get_midpoint(self, token_id: str) -> Decimal:
        data = await get_json(self._client, "/midpoint", params={"token_id": token_id})
        return Decimal(str(data["mid"]))

    async def get_sampling_markets(self) -> list[dict]:
        """参与流动性奖励的市场列表（做市策略用）。返回原始 JSON。"""
        data = await get_json(self._client, "/sampling-markets")
        return data if isinstance(data, list) else data.get("data", [])

    async def get_top_of_book(self, token_id: str) -> tuple[BookLevel | None, BookLevel | None]:
        """便捷方法：最优 bid / ask。"""
        book = await self.get_book(token_id)
        return book.best_bid, book.best_ask
