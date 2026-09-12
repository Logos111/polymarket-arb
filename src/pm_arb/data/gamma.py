"""Gamma API 客户端：市场 / 事件元数据发现。

Gamma 是 Polymarket 的元数据服务（非交易接口），无需认证：
- GET /markets  市场列表（含 conditionId、clobTokenIds、negRisk 等）
- GET /events   事件列表（一个事件可包含多个市场，NegRisk 组在此关联）
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx

from pm_arb.data.http import get_json, make_http_client
from pm_arb.data.models import Market
from pm_arb.infra.config import Settings, get_settings
from pm_arb.infra.logging import get_logger

log = get_logger(__name__)


class GammaClient:
    def __init__(self, settings: Settings | None = None, client: httpx.AsyncClient | None = None):
        self._settings = settings or get_settings()
        self._client = client or make_http_client(self._settings.gamma_api_url, self._settings)
        self._owns_client = client is None

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> GammaClient:
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    async def get_markets(
        self,
        *,
        active: bool = True,
        closed: bool = False,
        neg_risk: bool | None = None,
        limit: int = 100,
        order: str = "volume24hr",
        ascending: bool = False,
    ) -> list[Market]:
        """按条件拉取市场列表，默认按 24h 成交量降序。"""
        params: dict = {
            "active": str(active).lower(),
            "closed": str(closed).lower(),
            "limit": limit,
            "order": order,
            "ascending": str(ascending).lower(),
        }
        if neg_risk is not None:
            params["negRisk"] = str(neg_risk).lower()

        data = await get_json(self._client, "/markets", params=params)
        markets = [Market.model_validate(m) for m in data]
        log.info("gamma_markets_fetched", count=len(markets), order=order)
        return markets

    async def iter_all_markets(
        self, *, page_size: int = 500, max_pages: int = 100
    ) -> AsyncIterator[Market]:
        """分页迭代全部活跃未关闭市场（用于全量同步 / 数据录制）。"""
        offset = 0
        for _ in range(max_pages):
            params = {
                "active": "true",
                "closed": "false",
                "limit": page_size,
                "offset": offset,
            }
            data = await get_json(self._client, "/markets", params=params)
            if not data:
                break
            for raw in data:
                yield Market.model_validate(raw)
            if len(data) < page_size:
                break
            offset += page_size

    async def get_market_by_slug(self, slug: str) -> Market | None:
        data = await get_json(self._client, "/markets", params={"slug": slug})
        if not data:
            return None
        return Market.model_validate(data[0])

    async def get_liquid_markets(
        self, min_liquidity: float = 10_000, limit: int = 50
    ) -> list[Market]:
        """流动性过滤：套利只参与盘口厚度足够的市场。"""
        markets = await self.get_markets(limit=limit)
        from decimal import Decimal

        filtered = [m for m in markets if m.liquidity >= Decimal(str(min_liquidity))]
        log.info("gamma_liquid_filtered", total=len(markets), kept=len(filtered))
        return filtered
