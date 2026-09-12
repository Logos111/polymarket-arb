"""虚拟时钟秒级回放引擎（阶段 3 前置：HF 数据集验证版）。

铁律复用（DEV_PLAN 阶段 3）：
1. 判定复用生产代码：decide_entry / decide_exit / pick_underdog 与实盘
   trade5m 同源，本引擎只负责把 tick 快照喂给同一套纯函数；
2. 严防前视偏差：每个 tick 只用截至当前秒的盘口做判定，入场窗口
   [entry_after, entry_until] 内逐秒扫描，绝不使用窗口尾部信息；
3. 撮合保守：入场要求 ask 档深 ≥ 目标份数，否则放弃该窗口；止盈要求
   bid 档深 ≥ 持仓份数，否则继续持有（不部分成交）；
4. 结算：数据集 outcome 为推断值（须交叉核对），赢方 $1/份、输方 $0。

费用口径（settlement-rule.md）：taker fee = shares × 0.07 × p × (1−p)，
买卖两侧各按成交价计。

数据集限定：无现货 TWAP，rng 恒传 0（max_vol 过滤关闭）——报表必须
注明该差异；数据集时间窗 2026-03-24 → 2026-05-18，微结构与当前可能
不同。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum

from ..decisions import (
    EntryAction,
    decide_entry,
    decide_exit,
    pick_underdog,
)
from ..params import Crypto5mParams
from .hf_loader import HfDataset, HfMarket, d2, s2

FEE_RATE = Decimal("0.07")  # Crypto 类 taker feeRate（双源确认）


def taker_fee(shares: int, price: Decimal) -> Decimal:
    """taker 手续费 = shares × feeRate × p × (1−p)。"""
    return shares * FEE_RATE * price * (Decimal(1) - price)


class ExitKind(StrEnum):
    """窗口终态。"""

    NO_ENTRY = "no_entry"          # 未入场（含档深放弃）
    NO_ENTRY_DEPTH = "no_entry_depth"  # 信号成立但档深不足，保守放弃
    TAKE_PROFIT = "take_profit"
    SETTLE_WIN = "settle_win"
    SETTLE_LOSE = "settle_lose"


@dataclass
class WindowResult:
    """单窗口回放结果。"""

    symbol: str
    slug: str
    outcome: str | None
    exit_kind: ExitKind
    cand: str | None = None
    entry_ask: Decimal | None = None
    entry_t: int | None = None
    size: int = 0
    pnl: Decimal = Decimal(0)
    fee: Decimal = Decimal(0)
    reasons: list[str] = field(default_factory=list)


def replay_ticks(
    ticks: list[dict], mkt: HfMarket, p: Crypto5mParams, *, min_size: int = 5
) -> WindowResult:
    """单窗口秒级回放核心（tick 列表可跨参数组共享，网格扫描用）。"""
    res = WindowResult(
        symbol="", slug=mkt.slug, outcome=mkt.outcome,
        exit_kind=ExitKind.NO_ENTRY,
    )
    rng = Decimal(0)  # 数据集无 TWAP，波动过滤关闭
    position = None   # (cand, entry_ask, size, entry_t)

    for tick in ticks:
        elapsed = tick["t"] - mkt.start
        book = {
            "Up": {"best_ask": d2(tick["au"]), "best_bid": d2(tick["bu"]),
                   "ask_size": s2(tick["sau"]), "bid_size": s2(tick["su"])},
            "Down": {"best_ask": d2(tick["ad"]), "best_bid": d2(tick["bd"]),
                     "ask_size": s2(tick["sad"]), "bid_size": s2(tick["sd"])},
        }

        if position is None:
            cand, ask = pick_underdog(book)
            if ask is None:
                continue
            ask_size = book[cand]["ask_size"]
            dec = decide_entry(elapsed, cand, ask, ask_size, rng, p, min_size=min_size)
            if dec.action is EntryAction.ENTER:
                if ask_size < dec.size:
                    # 保守铁律：档深不足宁可放弃，不部分成交
                    res.exit_kind = ExitKind.NO_ENTRY_DEPTH
                    res.cand, res.entry_ask = cand, ask
                    res.reasons.append(f"depth {ask_size}<{dec.size}")
                    return res
                position = (cand, ask, dec.size, tick["t"])
                res.cand, res.entry_ask = cand, ask
                res.size = dec.size
                res.entry_t = tick["t"]
        else:
            cand, entry_ask, size, entry_t = position
            bid = book[cand]["best_bid"]
            bid_size = book[cand]["bid_size"]
            if bid is not None and bid_size >= size and decide_exit(bid, p):
                cost = size * entry_ask + taker_fee(size, entry_ask)
                proceeds = size * bid - taker_fee(size, bid)
                res.exit_kind = ExitKind.TAKE_PROFIT
                res.pnl = proceeds - cost
                res.fee = taker_fee(size, entry_ask) + taker_fee(size, bid)
                return res

    if position is None:
        return res

    # 未止盈 → 按 outcome 结算（赢 $1/份、输 $0，兑付无费）
    cand, entry_ask, size, _ = position
    cost = size * entry_ask + taker_fee(size, entry_ask)
    res.fee = taker_fee(size, entry_ask)
    if mkt.outcome == cand:
        res.exit_kind = ExitKind.SETTLE_WIN
        res.pnl = size - cost
    else:
        res.exit_kind = ExitKind.SETTLE_LOSE
        res.pnl = -cost
    return res


def replay_window(
    ds: HfDataset, mkt: HfMarket, p: Crypto5mParams, *, min_size: int = 5
) -> WindowResult:
    """单窗口回放（HfDataset 门面；网格扫描直接用 replay_ticks）。"""
    res = replay_ticks(ds.window_ticks(mkt.condition_id), mkt, p, min_size=min_size)
    res.symbol = ds.symbol
    return res


def run_backtest(
    ds: HfDataset,
    p: Crypto5mParams | None = None,
    *,
    min_size: int = 5,
    limit: int | None = None,
) -> list[WindowResult]:
    """全量回放：跳过 outcome 缺失窗口（计数交给报表）。"""
    p = p or Crypto5mParams()
    results: list[WindowResult] = []
    markets = ds.markets if limit is None else ds.markets[:limit]
    for mkt in markets:
        if mkt.outcome not in ("Up", "Down"):
            continue
        results.append(replay_window(ds, mkt, p, min_size=min_size))
    return results


def run_grid(
    ds: HfDataset,
    params_list: list[Crypto5mParams],
    *,
    min_size: int = 5,
) -> list[list[WindowResult]]:
    """网格扫描：每窗口 tick 构造一次，逐参数组回放（单遍数据多组共享）。"""
    results: list[list[WindowResult]] = [[] for _ in params_list]
    for mkt in ds.markets:
        if mkt.outcome not in ("Up", "Down"):
            continue
        if not ds.has_ticks(mkt.condition_id):
            continue
        ticks = ds.window_ticks(mkt.condition_id)  # 每窗口只构造一次
        for i, p in enumerate(params_list):
            r = replay_ticks(ticks, mkt, p, min_size=min_size)
            r.symbol = ds.symbol
            results[i].append(r)
    return results
