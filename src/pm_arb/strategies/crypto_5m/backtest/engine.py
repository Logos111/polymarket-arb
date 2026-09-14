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

from dataclasses import dataclass, field, fields
from decimal import Decimal
from enum import StrEnum

from pm_arb.data.book_history import BookHistory
from pm_arb.data.series_buffer import SeriesBuffer

from ..context import WindowFeatureBufs
from ..decisions import (
    EntryAction,
    decide_entry,
    decide_entry_v2,
    decide_exit,
    decide_stop,
    pick_underdog,
)
from ..features import (
    FeatureSnapshot,
    compute_features,
    load_score_weights,
    reversal_score,
)
from ..params import Crypto5mParams
from .hf_loader import HfDataset, HfMarket, d2, s2
from .spot_vol import SpotVol

FEE_RATE = Decimal("0.07")  # Crypto 类 taker feeRate（双源确认）


def taker_fee(shares: int, price: Decimal) -> Decimal:
    """taker 手续费 = shares × feeRate × p × (1−p)。"""
    return shares * FEE_RATE * price * (Decimal(1) - price)


class ExitKind(StrEnum):
    """窗口终态。"""

    NO_ENTRY = "no_entry"          # 未入场（含档深放弃）
    NO_ENTRY_DEPTH = "no_entry_depth"  # 信号成立但档深不足，保守放弃
    TAKE_PROFIT = "take_profit"
    STOP_LOSS = "stop_loss"        # bid 跌破止损价，市价卖出（stop_loss_price>0）
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
    ticks: list[dict], mkt: HfMarket, p: Crypto5mParams, *,
    min_size: int = 5, rng_seq: list[Decimal | None] | None = None,
    twap_seq: list[Decimal | None] | None = None,
    capture: FeatureCapture | None = None,
) -> WindowResult:
    """单窗口秒级回放核心（tick 列表可跨参数组共享，网格扫描用）。

    ``rng_seq``：各 tick 秒的现货波动（b07 SpotVol 重建，与 ticks 等长）；
    None 元素 = 现货数据不全（decide_entry 按 ABORT_DATA 放弃）；
    不传 = 无现货数据，rng 恒 0（max_vol 过滤关闭，旧行为）。

    ``twap_seq``/``capture``（b09）：特征采集旁路——capture=None 时
    本函数行为与旧版逐字节一致（零回归，test_hf_engine 锚定）；
    采集只读观察，绝不影响判定路径。

    评分门控（b09 二轮）：``p.min_reversal_score`` 非 None 时启用——
    自窗口首 tick 起（与实盘 orchestrator 同口径：每 poll 入缓冲，
    保 slope_60/delta_60 在入场判定时有完整历史）维护 WindowFeatureBufs，
    判定时算 reversal_score 交给 decide_entry_v2；None（默认）走
    decide_entry 原路径，零回归。门控需调用方传 twap_seq（无现货 →
    score=None → fail-closed 全部 OBSERVE）。
    """
    res = WindowResult(
        symbol="", slug=mkt.slug, outcome=mkt.outcome,
        exit_kind=ExitKind.NO_ENTRY,
    )
    position = None   # (cand, entry_ask, size, entry_t)
    gate = p.min_reversal_score is not None
    fbufs = WindowFeatureBufs() if gate else None
    score_w = load_score_weights(p.score_weights_path) if gate else None
    if capture is not None:
        capture.start_window()

    for i, tick in enumerate(ticks):
        elapsed = tick["t"] - mkt.start
        rng = rng_seq[i] if rng_seq is not None else Decimal(0)
        book = {
            "Up": {"best_ask": d2(tick["au"]), "best_bid": d2(tick["bu"]),
                   "ask_size": s2(tick["sau"]), "bid_size": s2(tick["su"])},
            "Down": {"best_ask": d2(tick["ad"]), "best_bid": d2(tick["bd"]),
                     "ask_size": s2(tick["sad"]), "bid_size": s2(tick["sd"])},
        }
        if capture is not None:
            capture.observe(tick, mkt, book, i, twap_seq, p,
                            entered=position is not None)
        if fbufs is not None:
            fbufs.update(float(elapsed), book,
                         twap_seq[i] if twap_seq is not None else None)

        if position is None:
            cand, ask = pick_underdog(book)
            if ask is None:
                continue
            ask_size = book[cand]["ask_size"]
            if fbufs is not None:
                # 与实盘 orchestrator 同一计算源（context.WindowFeatureBufs
                # + features.compute_features + reversal_score）
                fav = "Down" if cand == "Up" else "Up"
                ud_a, ud_b = fbufs.books.bufs(cand)
                fa, fb = fbufs.books.bufs(fav)
                fsnap = compute_features(
                    fbufs.spot, ud_a, ud_b, fa, fb, book, cand,
                    float(elapsed))
                score = reversal_score(fsnap, score_w)
                dec = decide_entry_v2(elapsed, cand, ask, ask_size, rng, p,
                                      min_size=min_size, score=score)
            else:
                dec = decide_entry(elapsed, cand, ask, ask_size, rng, p,
                                   min_size=min_size)
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
                if capture is not None:
                    capture.mark_entered(tick["t"])
        else:
            cand, entry_ask, size, entry_t = position
            bid = book[cand]["best_bid"]
            bid_size = book[cand]["bid_size"]
            if bid is not None and bid_size >= size:
                if decide_exit(bid, p):
                    cost = size * entry_ask + taker_fee(size, entry_ask)
                    proceeds = size * bid - taker_fee(size, bid)
                    res.exit_kind = ExitKind.TAKE_PROFIT
                    res.pnl = proceeds - cost
                    res.fee = taker_fee(size, entry_ask) + taker_fee(size, bid)
                    return res
                if decide_stop(bid, p):
                    # 止损与止盈同一保守档深门槛：深度不足宁可继续持有
                    cost = size * entry_ask + taker_fee(size, entry_ask)
                    proceeds = size * bid - taker_fee(size, bid)
                    res.exit_kind = ExitKind.STOP_LOSS
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
    ds: HfDataset, mkt: HfMarket, p: Crypto5mParams, *,
    min_size: int = 5, spot: SpotVol | None = None,
) -> WindowResult:
    """单窗口回放（HfDataset 门面；网格扫描直接用 replay_ticks）。"""
    ticks = ds.window_ticks(mkt.condition_id)
    rng_seq = (
        spot.window_rng_seq(mkt.start, [t["t"] for t in ticks])
        if spot is not None else None
    )
    twap_seq = (
        spot.window_twap_seq(mkt.start, [t["t"] for t in ticks])
        if spot is not None and p.min_reversal_score is not None else None
    )
    res = replay_ticks(ticks, mkt, p, min_size=min_size,
                       rng_seq=rng_seq, twap_seq=twap_seq)
    res.symbol = ds.symbol
    return res


def run_backtest(
    ds: HfDataset,
    p: Crypto5mParams | None = None,
    *,
    min_size: int = 5,
    limit: int | None = None,
    spot: SpotVol | None = None,
) -> list[WindowResult]:
    """全量回放：跳过 outcome 缺失窗口（计数交给报表）。"""
    p = p or Crypto5mParams()
    results: list[WindowResult] = []
    markets = ds.markets if limit is None else ds.markets[:limit]
    for mkt in markets:
        if mkt.outcome not in ("Up", "Down"):
            continue
        results.append(replay_window(ds, mkt, p, min_size=min_size, spot=spot))
    return results


def run_grid(
    ds: HfDataset,
    params_list: list[Crypto5mParams],
    *,
    min_size: int = 5,
    spot: SpotVol | None = None,
) -> list[list[WindowResult]]:
    """网格扫描：每窗口 tick 与 rng_seq 各构造一次，逐参数组共享回放。"""
    # 任一参数组开启评分门控 → 需 twap_seq（spot 缺失时 fail-closed 全 OBSERVE）
    need_twap = any(pp.min_reversal_score is not None for pp in params_list)
    results: list[list[WindowResult]] = [[] for _ in params_list]
    for mkt in ds.markets:
        if mkt.outcome not in ("Up", "Down"):
            continue
        if not ds.has_ticks(mkt.condition_id):
            continue
        ticks = ds.window_ticks(mkt.condition_id)  # 每窗口只构造一次
        ts = [t["t"] for t in ticks]
        rng_seq = spot.window_rng_seq(mkt.start, ts) if spot is not None else None
        twap_seq = (
            spot.window_twap_seq(mkt.start, ts)
            if spot is not None and need_twap else None
        )
        for i, p in enumerate(params_list):
            r = replay_ticks(ticks, mkt, p, min_size=min_size,
                             rng_seq=rng_seq, twap_seq=twap_seq)
            r.symbol = ds.symbol
            results[i].append(r)
    return results


class FeatureCapture:
    """b09 特征采集累加器（只读旁路，绝不影响 replay_ticks 判定路径）。

    逐 tick 把现货 TWAP / 双边盘口 push 进 SeriesBuffer / BookHistory，
    调用与实盘同一套 :func:`compute_features`，行字段 = 窗口/候选元数据
    + FeatureSnapshot 全字段（Decimal→float）+ 反转评分 + ``entered``。
    Label（future_return_*/mfe_60/mae_60/final_outcome）不在此处算——
    由 labels.compute_window_labels 在窗口回放完后就地附加（泄漏护栏：
    前视信息只进 labels.py）。

    采集窗口：``[entry_after-30, min(entry_until+30, 240)]``（工程方案 §3，
    覆盖入场判定前后各 30s；缓冲仍自窗口首 tick 开始 push，保证
    ret_120/slope_60 在采集窗口内有完整历史）。
    """

    SPOT_MAXLEN = 130.0   # 覆盖 ret_120 + 余量
    BOOK_MAXLEN = 65.0    # 覆盖 delta_60 + 余量

    def __init__(self, symbol: str = "") -> None:
        self.symbol = symbol
        self.rows: list[dict] = []
        self._spot: SeriesBuffer | None = None
        self._bh: BookHistory | None = None

    def start_window(self) -> None:
        """每窗口重建缓冲（replay_ticks 入口自动调用）。"""
        self._spot = SeriesBuffer(self.SPOT_MAXLEN)
        self._bh = BookHistory(self.BOOK_MAXLEN)

    def mark_entered(self, t: int) -> None:
        """入场 tick 事后补标（observe 先于决策执行，见 replay_ticks）。"""
        if self.rows and self.rows[-1]["t"] == t:
            self.rows[-1]["entered"] = True

    def observe(self, tick: dict, mkt: HfMarket, book: dict, i: int,
                twap_seq: list[Decimal | None] | None, p: Crypto5mParams,
                *, entered: bool) -> None:
        elapsed = float(tick["t"] - mkt.start)
        twap = twap_seq[i] if twap_seq is not None else None
        self._spot.push(elapsed, twap)
        self._bh.update(elapsed, book)
        lo = float(p.entry_after) - 30.0
        hi = min(float(p.entry_until) + 30.0, 240.0)
        if not lo <= elapsed <= hi:
            return
        cand, ask = pick_underdog(book)
        if cand is None or ask is None:
            return
        fav = "Down" if cand == "Up" else "Up"
        ud_a, ud_b = self._bh.bufs(cand)
        fa, fb = self._bh.bufs(fav)
        f = compute_features(self._spot, ud_a, ud_b, fa, fb, book, cand,
                              elapsed)
        row: dict = {
            "symbol": self.symbol, "condition_id": mkt.condition_id,
            "slug": mkt.slug, "window_start": mkt.start,
            "t": tick["t"], "elapsed": elapsed,
            "cand": cand, "cand_ask": float(ask),
            "entered": entered, "score": reversal_score(f),
        }
        for fd in fields(FeatureSnapshot):
            v = getattr(f, fd.name)
            if isinstance(v, Decimal):
                v = float(v)
            row[fd.name] = v
        self.rows.append(row)
