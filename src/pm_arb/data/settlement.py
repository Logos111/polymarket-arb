"""结算回填（DEV_PLAN 阶段 2）：Gamma closed 市场查询 → 赢家判定 → 费后 PnL。

被两处共用：
- :mod:`pm_arb.app.backfill`（pm-backfill：任意历史窗口批量回填）；
- orchestrator 的后台结算守护任务（窗口结束 + 宽限期后自动回填）。

口径（docs/settlement-rule.md）：结算标签以 Gamma outcomePrices 为权威；
PnL 含双边 taker fee（fee = 份数 × 0.07 × p × (1−p)，买卖两侧各计）。
"""

from __future__ import annotations

from decimal import Decimal

from pm_arb.data.crypto_5m import window_slug
from pm_arb.data.gamma import GammaClient
from pm_arb.data.models import Market
from pm_arb.infra.logging import get_logger
from pm_arb.infra.store import Store

log = get_logger(__name__)

FEE_RATE = Decimal("0.07")


def taker_fee(price: Decimal, shares: Decimal) -> Decimal:
    """taker fee = 份数 × feeRate × p × (1−p)（Crypto 类 feeRate=0.07）。"""
    if shares <= 0:
        return Decimal(0)
    return shares * FEE_RATE * price * (Decimal(1) - price)


def market_winner(m: Market) -> str | None:
    """从 Gamma outcomePrices 取结算赢家（仅 closed 市场可信，'1' 的那个）。

    返回 outcomes 原文名（二元市场即 'Up'/'Down'）；未结算/解析失败返回 None。
    """
    if not m.closed or len(m.outcome_prices) != 2 or len(m.outcomes) != 2:
        return None
    try:
        prices = [Decimal(p) for p in m.outcome_prices]
    except Exception:
        return None
    if prices[0] == 1 and prices[1] == 0:
        return m.outcomes[0]
    if prices[1] == 1 and prices[0] == 0:
        return m.outcomes[1]
    return None  # 平局/异常（理论不应出现）


def settle_result(
    side: str, filled_size: Decimal, cost: Decimal, entry_fee: Decimal,
    winner: str | None,
) -> tuple[str, Decimal] | None:
    """持仓结算 → (结果, 费后 PnL)。winner None（未结算/异常）返回 None。

    win：份数 × $1 − 成本 − 入场 fee（出场 fee 只在止盈/止损平仓时发生）；
    lose：−(成本 + 入场 fee)。
    """
    if winner is None:
        return None
    if winner == side:
        pnl = filled_size * Decimal(1) - cost - entry_fee
        return "win", pnl
    return "lose", -(cost + entry_fee)


async def settle_key(
    store: Store, gamma: GammaClient, symbol: str, window_start: int, *,
    dry: bool,
) -> dict | None:
    """回填单个 (symbol, window_start)：返回回填摘要 dict，未结算/无市场返回 None。

    副作用：
    - windows 行回填 market_winner（无交易窗口也写——回测样本放大器）；
    - 有 open 持仓 → positions 置 settled + windows 回填 settlement/settle_pnl；
    - 赢单自动赎回：仅 EOA 钱包（signature_type≠3）尝试；proxy（Safe
      execTransaction + EIP-1271，阶段 0.4 遗留）只记待赎日志。
    """
    slug = window_slug(symbol, window_start)
    m = await gamma.get_market_by_slug(slug, closed=True)
    if m is None:
        return None
    winner = market_winner(m)
    summary: dict = {"symbol": symbol, "window_start": window_start,
                     "slug": slug, "winner": winner}
    store.upsert_window(symbol, window_start, slug=slug, question=m.question,
                        condition_id=m.condition_id, market_winner=winner)
    cur = store.conn.execute(
        "SELECT * FROM positions WHERE symbol=? AND window_start=?",
        (symbol, window_start),
    )
    pos = cur.fetchone()
    if pos is None:
        return summary
    row = dict(zip([c[0] for c in cur.description], pos, strict=True))
    if row["status"] == "open" and winner is not None:
        res = settle_result(
            row["side"], Decimal(row["filled_size"]), Decimal(row["cost"]),
            Decimal(row["fee"] or "0"), winner,
        )
        if res is not None:
            outcome, pnl = res
            store.settle_position(symbol=symbol, window_start=window_start,
                                  won=(outcome == "win"), realized_pnl=pnl)
            store.upsert_window(symbol, window_start, settlement=outcome,
                                settle_pnl=pnl)
            summary.update(outcome=outcome, pnl=pnl, redeemed=False)
            if outcome == "win":
                summary["redeemed"] = await _try_redeem(
                    m.condition_id, symbol, window_start, dry=dry)
    return summary


async def _try_redeem(condition_id: str, symbol: str, window_start: int, *,
                      dry: bool) -> bool:
    """赢单自动赎回。proxy 钱包（signature_type=3，实盘现状）只记待赎。"""
    from pm_arb.infra.config import get_settings

    s = get_settings()
    if s.signature_type == 3:
        log.info("redeem_pending_proxy", symbol=symbol,
                 window=window_start, condition=condition_id[:10])
        return False
    if dry or not condition_id:
        return False
    try:
        from pm_arb.execution.chain import ChainClient

        async with ChainClient() as chain:
            await chain.redeem(condition_id)
        return True
    except Exception as e:  # 赎回失败不阻塞结算回填，留待 pm-redeem 手动
        log.warning("redeem_failed", symbol=symbol, window=window_start,
                    error=str(e)[:120])
        return False
