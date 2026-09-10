"""加密货币 5 分钟 Up/Down 市场发现。

Polymarket 的 5 分钟加密预测市场（Chainlink 结算）命名规律稳定：

    slug = "{sym}-updown-5m-{window_start_unix}"

其中 ``window_start_unix`` 为 5 分钟整点对齐的窗口开始时刻（UTC 秒）；
实测（2026-09-10）：slug btc-updown-5m-1789055700 的市场标题为
"…11:55AM-12:00PM ET"，即标题区间与 slug 时间戳对齐。

**边界竞态警示**：取"即将开始"的窗口市场时必须显式传 ``window_start``，
不能用默认的 ``current_window_start()``——窗口边界前 1~2 秒它仍指向
上一个（即将结算的）窗口，恰好会拿到已死的市场（历史事故：trade5m
曾在边界前 1s 取市场，整窗监测了已结算盘口）。

因此无需翻页搜索，直接按窗口起点构造 slug → Gamma 查询即可拿到
token ID（Outcome 固定为 ["Up", "Down"]）。

已观察到的 sym：btc / eth / sol / xrp / doge / bnb / hype / zec 等。
"""

from __future__ import annotations

import time

from pm_arb.data.gamma import GammaClient
from pm_arb.data.models import Market
from pm_arb.infra.logging import get_logger

log = get_logger(__name__)

WINDOW_SECONDS = 300  # 5 分钟

# 支持的币种 → slug 前缀 / 展示名
SYMBOLS: dict[str, str] = {
    "btc": "Bitcoin",
    "eth": "Ethereum",
    "sol": "Solana",
    "xrp": "XRP",
    "doge": "Dogecoin",
    "bnb": "BNB",
    "hype": "Hyperliquid",
    "zec": "ZCash",
}


def current_window_start(now: float | None = None) -> int:
    """当前正在交易的 5 分钟窗口开始时刻（UTC unix 秒，向下取整到 300）。"""
    t = int(time.time() if now is None else now)
    return (t // WINDOW_SECONDS) * WINDOW_SECONDS


def window_slug(symbol: str, window_start: int) -> str:
    """返回**开始于** window_start 的窗口对应市场的 slug（slug 时间戳=窗口开始）。"""
    sym = symbol.lower()
    if sym not in SYMBOLS:
        raise ValueError(f"不支持的币种 {symbol}；可选：{sorted(SYMBOLS)}")
    return f"{sym}-updown-5m-{window_start}"


async def get_window_market(
    gamma: GammaClient, symbol: str, window_start: int | None = None
) -> Market | None:
    """获取某币种某 5 分钟窗口的市场（默认当前窗口）。

    取"即将开始"的窗口务必显式传 ``window_start``（边界竞态见模块 docstring）。
    窗口刚开始/结束瞬间市场可能尚未创建或已关闭，返回 None。
    """
    sym = symbol.lower()
    if sym not in SYMBOLS:
        raise ValueError(f"不支持的币种 {symbol}；可选：{sorted(SYMBOLS)}")
    ws = window_start if window_start is not None else current_window_start()
    market = await gamma.get_market_by_slug(window_slug(sym, ws))
    if market is None:
        log.info("crypto5m_window_not_found", symbol=sym, window_start=ws)
        return None
    log.debug(
        "crypto5m_window",
        symbol=sym,
        window_start=ws,
        question=market.question[:40],
        tokens=len(market.clob_token_ids),
    )
    return market


async def get_active_windows(
    gamma: GammaClient, symbols: tuple[str, ...] = ("btc", "eth")
) -> dict[str, Market]:
    """批量获取多个币种的当前窗口市场（跳过未找到的）。"""
    out: dict[str, Market] = {}
    for sym in symbols:
        m = await get_window_market(gamma, sym)
        if m is not None and m.is_tradeable:
            out[sym] = m
    return out


def up_down_tokens(market: Market) -> tuple[str, str]:
    """返回 (Up token_id, Down token_id)。5m 市场 outcomes 固定为 ["Up","Down"]。"""
    toks = market.tokens
    up = next((t.token_id for t in toks if t.outcome.lower() == "up"), toks[0].token_id)
    down = next((t.token_id for t in toks if t.outcome.lower() == "down"), toks[1].token_id)
    return up, down
