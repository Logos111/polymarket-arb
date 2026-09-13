"""特征引擎（b09；纯函数，与 decisions.py 平级：无 I/O、无时钟）。

实盘 orchestrator 与回测 engine 共用 :func:`compute_features`——输入一律是
:data:`~pm_arb.data.series_buffer.SeriesBuffer`（现货/盘口序列）与当前双边
盘口视图，两侧行为同源（防"两套逻辑漂移"）。

None 语义纪律（工程落地方案 §2.1）：
    序列数据缺失/超龄/样本不足时特征一律为 ``None`` = "不可用"，**绝不**
    当 0 处理——0 意味着"确认走平"，None 意味着"不知道"。喂价（Chainlink
    TWAP 心跳）卡死时序列会呈现完全走平，与真实走平在斜率上不可区分，
    因此 Trend/Momentum 特征全部以 ``feed_fresh`` 新鲜度判定为前提。

批次划分（工程落地方案 §4，仅影响研究分析节奏，计算层一次建齐）：
- 批次 0：Book 组（无序列依赖）；批次 1：Trend/Momentum/Extreme（依赖
  现货 SeriesBuffer + feed_fresh）；批次 2：Token/Favorite/Cross（依赖
  盘口 SeriesBuffer）。

OBI/深度口径（§2.3）：一律只用最优一档（best bid/ask 的 price×size），
保证实盘 L2 与 HF 数据集（仅 top-of-book）是同一信息集合——多档深度
特征无法在 HF 数据集上历史复现，不进入判定路径。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, fields, replace
from decimal import Decimal
from pathlib import Path

from pm_arb.data.series_buffer import SeriesBuffer

# 现货序列 60s 内最少采样点（1Hz 满采样=60；低于此视为喂价心跳可疑）。
# 暂定值 45（容忍 25% 缺口），待实盘 RTDS 更新间隔分布统计后标定。
SPOT_MIN_SAMPLES_60 = 45

# 任意 asof 查询允许的最大数据年龄（秒）：超龄 = 数据太老，不可用。
FEATURE_MAX_AGE = 5.0


@dataclass(frozen=True)
class FeatureSnapshot:
    """t 时刻的特征快照（None = 数据不可用，绝不代表 0）。"""

    # --- Trend（批次 1，依赖现货序列）---
    ret_15: Decimal | None = None
    ret_30: Decimal | None = None
    ret_60: Decimal | None = None
    ret_90: Decimal | None = None
    ret_120: Decimal | None = None
    slope_15: Decimal | None = None
    slope_30: Decimal | None = None
    slope_60: Decimal | None = None
    # --- Momentum（批次 1）---
    acceleration: Decimal | None = None        # slope_15 - slope_60
    momentum_decay: Decimal | None = None      # |ret_60| - |ret_15|
    # --- Extreme（批次 1，现货窗口极值）---
    dist_high: Decimal | None = None           # 窗口高点 - 现价（>=0）
    dist_low: Decimal | None = None            # 现价 - 窗口低点（>=0）
    new_low_count_15: int | None = None
    new_low_count_30: int | None = None
    new_high_count_15: int | None = None
    new_high_count_30: int | None = None
    time_since_low: float | None = None
    time_since_high: float | None = None
    # --- Token 冷门方动量（批次 2，依赖盘口序列）---
    ud_ask_delta_5: Decimal | None = None
    ud_ask_delta_10: Decimal | None = None
    ud_ask_delta_15: Decimal | None = None
    ud_ask_delta_30: Decimal | None = None
    ud_ask_delta_60: Decimal | None = None
    ud_mid_delta_10: Decimal | None = None
    ud_mid_delta_30: Decimal | None = None
    ud_mid_delta_60: Decimal | None = None
    # --- Favorite 动量（批次 2）---
    fav_mid_delta_10: Decimal | None = None
    fav_mid_delta_30: Decimal | None = None
    fav_mid_delta_60: Decimal | None = None
    # --- Book（批次 0，无序列依赖）---
    obi_up: Decimal | None = None              # (bid-ask size)/(bid+ask size)
    obi_down: Decimal | None = None
    relative_obi: Decimal | None = None        # obi_cand - obi_fav
    spread_underdog: Decimal | None = None
    spread_favorite: Decimal | None = None
    depth_ratio_underdog: Decimal | None = None  # ud ask 名义 / fav bid 名义
    # --- Cross（批次 2）---
    spot_token_divergence_10: Decimal | None = None  # spot_ret_N - ud_mid_ret_N
    spot_token_divergence_30: Decimal | None = None
    spot_token_divergence_60: Decimal | None = None
    # --- 元数据：§2.1 可靠性判定的一等公民 ---
    spot_sample_count_60: int = 0              # 现货序列近 60s 有效采样点数
    feed_fresh: bool = False                   # False 时 Trend 字段全部为 None


def _sign(x: Decimal) -> int:
    return 1 if x > 0 else (-1 if x < 0 else 0)


def _obi(side: dict) -> Decimal | None:
    bs, as_ = side.get("bid_size"), side.get("ask_size")
    if bs is None or as_ is None or bs + as_ <= 0:
        return None
    return Decimal(bs - as_) / Decimal(bs + as_)


def _mid(ask_buf: SeriesBuffer, bid_buf: SeriesBuffer,
         t: float, max_age: float) -> Decimal | None:
    """(best_ask + best_bid) / 2 的 asof 值；任一侧缺失 → None。"""
    a = ask_buf.value_asof(t, max_age=max_age)
    b = bid_buf.value_asof(t, max_age=max_age)
    if a is None or b is None:
        return None
    return (a + b) / 2


def compute_features(
    spot: SeriesBuffer,
    ud_ask: SeriesBuffer,
    ud_bid: SeriesBuffer,
    fav_ask: SeriesBuffer,
    fav_bid: SeriesBuffer,
    book: dict[str, dict],
    cand: str,
    t: float,
    *,
    feed_fresh: bool | None = None,
    min_spot_samples_60: int = SPOT_MIN_SAMPLES_60,
    max_age: float = FEATURE_MAX_AGE,
) -> FeatureSnapshot:
    """在时刻 t 计算特征快照。纯函数：只读传入缓冲区，不做 I/O、不读时钟。

    ``cand``：当前冷门方名（"Up"/"Down"），favorite 即另一侧。
    ``feed_fresh=None`` 时自动判定：现货近 60s 采样数达
    ``min_spot_samples_60`` **且** 最新点距 t 不超 ``max_age``。
    缓冲区应在窗口起点开始 push（回测 engine / 实盘 orchestrator 每窗口
    新建），"窗口内极值" 即缓冲全量极值。
    """
    fav = "Down" if cand == "Up" else "Up"

    # ---- 批次 0：Book（无序列依赖）----
    obi_u, obi_d = _obi(book["Up"]), _obi(book["Down"])
    obi_c = obi_u if cand == "Up" else obi_d
    obi_f = obi_d if cand == "Up" else obi_u
    rel_obi = (
        obi_c - obi_f
        if obi_c is not None and obi_f is not None else None
    )
    ud_bk, fav_bk = book[cand], book[fav]
    ud_a, ud_b = ud_bk.get("best_ask"), ud_bk.get("best_bid")
    fa, fb = fav_bk.get("best_ask"), fav_bk.get("best_bid")
    spread_ud = ud_a - ud_b if ud_a is not None and ud_b is not None else None
    spread_fav = fa - fb if fa is not None and fb is not None else None
    depth_ratio = None
    if (ud_a is not None and fb is not None
            and ud_bk.get("ask_size") and fav_bk.get("bid_size")):
        ud_notional = Decimal(ud_bk["ask_size"]) * ud_a
        fav_notional = Decimal(fav_bk["bid_size"]) * fb
        if fav_notional > 0:
            depth_ratio = ud_notional / fav_notional

    base: dict = {
        "obi_up": obi_u, "obi_down": obi_d, "relative_obi": rel_obi,
        "spread_underdog": spread_ud, "spread_favorite": spread_fav,
        "depth_ratio_underdog": depth_ratio,
    }

    # ---- 新鲜度判定（§2.1 一等公民）----
    sc = spot.sample_count(t, 60.0)
    auto_fresh = (
        sc >= min_spot_samples_60
        and spot.value_asof(t, max_age=max_age) is not None
    )
    fresh = auto_fresh if feed_fresh is None else feed_fresh and auto_fresh
    base["spot_sample_count_60"] = sc
    base["feed_fresh"] = fresh

    if fresh:
        # ---- 批次 1：Trend / Momentum / Extreme ----
        for n in (15, 30, 60, 90, 120):
            base[f"ret_{n}"] = spot.ret(t, float(n), max_age=max_age)
        for n in (15, 30, 60):
            base[f"slope_{n}"] = spot.slope(t, float(n))
        s15, s60 = base["slope_15"], base["slope_60"]
        base["acceleration"] = (
            s15 - s60 if s15 is not None and s60 is not None else None)
        r15, r60 = base["ret_15"], base["ret_60"]
        base["momentum_decay"] = (
            abs(r60) - abs(r15)
            if r15 is not None and r60 is not None else None)
        ext = spot.extremes()
        last = spot.value_asof(t, max_age=max_age)
        if ext is not None and last is not None:
            hi, lo = ext
            base["dist_high"] = hi - last
            base["dist_low"] = last - lo
        for n in (15, 30):
            base[f"new_low_count_{n}"] = spot.new_extreme_count(
                t, float(n), "low")
            base[f"new_high_count_{n}"] = spot.new_extreme_count(
                t, float(n), "high")
        base["time_since_low"] = spot.time_since_extreme(t, "low")
        base["time_since_high"] = spot.time_since_extreme(t, "high")

    # ---- 批次 2：Token / Favorite / Cross（盘口序列，不依赖 feed_fresh）----
    for n in (5, 10, 15, 30, 60):
        v0 = ud_ask.value_asof(t, max_age=max_age)
        v1 = ud_ask.value_asof(t - n, max_age=max_age)
        base[f"ud_ask_delta_{n}"] = (
            v0 - v1 if v0 is not None and v1 is not None else None)
    for n in (10, 30, 60):
        m0 = _mid(ud_ask, ud_bid, t, max_age)
        m1 = _mid(ud_ask, ud_bid, t - n, max_age)
        base[f"ud_mid_delta_{n}"] = (
            m0 - m1 if m0 is not None and m1 is not None else None)
        f0 = _mid(fav_ask, fav_bid, t, max_age)
        f1 = _mid(fav_ask, fav_bid, t - n, max_age)
        base[f"fav_mid_delta_{n}"] = (
            f0 - f1 if f0 is not None and f1 is not None else None)
        sr = spot.ret(t, float(n), max_age=max_age) if fresh else None
        mr0 = _mid(ud_ask, ud_bid, t, max_age)
        mr1 = _mid(ud_ask, ud_bid, t - n, max_age)
        udr = None
        if mr0 is not None and mr1 is not None and mr1 != 0:
            udr = (mr0 - mr1) / mr1
        base[f"spot_token_divergence_{n}"] = (
            sr - udr if sr is not None and udr is not None else None)

    valid = {f.name for f in fields(FeatureSnapshot)}
    return FeatureSnapshot(**{k: v for k, v in base.items() if k in valid})


# ---- 反转评分（优化方案 §28 规则版；先规则、不上 ML）----


@dataclass(frozen=True)
class ScoreWeights:
    """离散评分阈值（score_weights.json 可调；全部暂定，待数据标定）。"""

    near_extreme_usd: float = 3.0       # 现价距窗口极值 ≤ 该 USD 值 → 接近极值
    obi_improve: float = 0.05           # relative_obi > 该值 → 冷门方买压占优
    new_extreme_burst: int = 3          # 30s 内创新极值 ≥ 该次数 → 趋势仍强
    trend_ret_15: float = 0.0002        # |ret_15| ≥ 该值 → 短线趋势仍强
    fav_delta_30: float = 0.02          # fav 30s 涨幅 ≥ 该绝对值 → 热门方增强


DEFAULT_WEIGHTS = ScoreWeights()
_WEIGHTS_FILE = Path(__file__).with_name("score_weights.json")


def load_score_weights(path: str | None = None) -> ScoreWeights:
    """加载评分权重；文件缺失/字段缺失时回落内置默认（None=内置默认）。"""
    p = Path(path) if path else _WEIGHTS_FILE
    if not p.is_file():
        return DEFAULT_WEIGHTS
    raw = json.loads(p.read_text(encoding="utf-8"))
    known = {f.name for f in fields(ScoreWeights)}
    return replace(
        DEFAULT_WEIGHTS,
        **{k: v for k, v in raw.items() if k in known},
    )


def reversal_score(f: FeatureSnapshot, w: ScoreWeights | None = None,
                   ) -> int | None:
    """反转评分（离散规则，方向对称；优化方案 §28）。

    返回 None 的情况（不给出误导性分数）：
    - ``feed_fresh=False``（喂价不新鲜，§2.1 护栏）；
    - ``slope_60`` 缺失或为 0（趋势方向未定义，反转无从谈起）。

    语义约定（方向对称，d = sign(slope_60) 为当前趋势方向）：
    +2  现货短线动量反转：slope_15 与 slope_60 反向；
    +2  冷门方被市场买入：ud_ask_delta_30 > 0（真金白银投票反转）；
    +1  现货短线掉头：ret_30 与 d 反向；
    +1  现价接近趋势方向极值（d<0 看低点，d>0 看高点）；
    +1  趋势方向不再创极值（d<0 看 15s 新低计数 == 0）；
    +1  冷门方相对买压占优：relative_obi ≥ w.obi_improve；
    -3  趋势仍猛烈：d 方向 30s 创新极值次数 ≥ w.new_extreme_burst；
    -2  短线趋势仍强：slope_15/slope_30 与 d 同向且 |ret_15| ≥ w.trend_ret_15；
    -2  热门方继续增强：fav_mid_delta_30 与 d 同向且 |·| ≥ w.fav_delta_30。
    """
    if not f.feed_fresh:
        return None
    s60 = f.slope_60
    if s60 is None or s60 == 0:
        return None
    w = w or DEFAULT_WEIGHTS
    d = _sign(s60)
    score = 0
    if f.slope_15 is not None and _sign(f.slope_15) == -d:
        score += 2
    if f.ud_ask_delta_30 is not None and f.ud_ask_delta_30 > 0:
        score += 2
    if f.ret_30 is not None and _sign(f.ret_30) == -d:
        score += 1
    if d < 0:
        near = (f.dist_low is not None
                and f.dist_low <= Decimal(str(w.near_extreme_usd)))
        no_new = f.new_low_count_15 == 0
        burst = (f.new_low_count_30 is not None
                 and f.new_low_count_30 >= w.new_extreme_burst)
    else:
        near = (f.dist_high is not None
                and f.dist_high <= Decimal(str(w.near_extreme_usd)))
        no_new = f.new_high_count_15 == 0
        burst = (f.new_high_count_30 is not None
                 and f.new_high_count_30 >= w.new_extreme_burst)
    if near:
        score += 1
    if no_new:
        score += 1
    if (f.relative_obi is not None
            and f.relative_obi >= Decimal(str(w.obi_improve))):
        score += 1
    if burst:
        score -= 3
    if (f.slope_15 is not None and _sign(f.slope_15) == d
            and f.slope_30 is not None and _sign(f.slope_30) == d
            and f.ret_15 is not None
            and abs(f.ret_15) >= Decimal(str(w.trend_ret_15))):
        score -= 2
    if (f.fav_mid_delta_30 is not None and _sign(f.fav_mid_delta_30) == d
            and abs(f.fav_mid_delta_30) >= Decimal(str(w.fav_delta_30))):
        score -= 2
    return score
