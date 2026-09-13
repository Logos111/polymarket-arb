"""features.py 合成数据单测（b09 B；含 §2.1 feed_fresh 护栏验证）。"""

from decimal import Decimal

from pm_arb.data.series_buffer import SeriesBuffer
from pm_arb.strategies.crypto_5m.features import (
    DEFAULT_WEIGHTS,
    FeatureSnapshot,
    compute_features,
    load_score_weights,
    reversal_score,
)


def _lin(buf: SeriesBuffer, pts: list[tuple[float, float]]) -> None:
    for t, v in pts:
        buf.push(t, Decimal(str(v)))


def _book(ua=0.25, ub=0.20, us=100, bs=80, da=0.80, db=0.75, ds=50, dbs=60):
    return {
        "Up": {"best_ask": Decimal(str(ua)), "best_bid": Decimal(str(ub)),
               "ask_size": us, "bid_size": bs},
        "Down": {"best_ask": Decimal(str(da)), "best_bid": Decimal(str(db)),
                 "ask_size": ds, "bid_size": dbs},
    }


def _mkbuf(pts: list[tuple[float, float]] | None) -> SeriesBuffer:
    b = SeriesBuffer()
    _lin(b, pts or [])
    return b


def _bufs(spot_pts=None, ud_a=None, ud_b=None, fa=None, fb=None):
    return (_mkbuf(spot_pts), _mkbuf(ud_a), _mkbuf(ud_b),
            _mkbuf(fa), _mkbuf(fb))


def _compute(spot, uda, udb, faa, fbb, t=100.0, **kw):
    return compute_features(spot, uda, udb, faa, fbb, _book(), "Up", t, **kw)


def test_book_batch0_obi_spread_depth() -> None:
    f = _compute(*_bufs())
    # Up: bid 80, ask 100 → (80-100)/180 = -0.1111
    assert f.obi_up == (Decimal(80) - Decimal(100)) / Decimal(180)
    # Down: bid 60, ask 50 → (60-50)/110
    assert f.obi_down == (Decimal(60) - Decimal(50)) / Decimal(110)
    assert f.relative_obi == f.obi_up - f.obi_down
    assert f.spread_underdog == Decimal("0.05")   # 0.25-0.20
    assert f.spread_favorite == Decimal("0.05")   # 0.80-0.75
    # depth_ratio = 100*0.25 / 60*0.75 = 25/45
    assert f.depth_ratio_underdog == Decimal(25) / Decimal(45)
    # 批次 0 不依赖 feed_fresh：默认空现货 → feed_fresh=False 但 Book 字段有值
    assert f.feed_fresh is False
    assert f.ret_30 is None and f.slope_60 is None


def test_relative_obi_flips_with_cand() -> None:
    s, a, b, fa, fb = _bufs()
    f1 = compute_features(s, a, b, fa, fb, _book(), "Up", 100.0)
    f2 = compute_features(s, fa, fb, a, b, _book(), "Down", 100.0)
    assert f1.obi_up == f2.obi_up  # 同一边 OBI 与视角无关
    assert f1.relative_obi == -f2.relative_obi


def test_feed_fresh_gate_blocks_trend() -> None:
    """§2.1 护栏：现货数据存在但 60s 采样不足 → Trend 字段全部 None。"""
    # 仅 10 个点（< SPOT_MIN_SAMPLES_60=45）
    spot_pts = [(float(i), 100.0 + i) for i in range(10)]
    f = _compute(*_bufs(spot_pts), t=9.0)   # 查询时刻对齐数据末端
    assert f.spot_sample_count_60 == 10
    assert f.feed_fresh is False
    assert f.ret_15 is None and f.slope_15 is None
    assert f.acceleration is None and f.dist_low is None
    assert f.new_low_count_15 is None


def test_feed_fresh_false_explicit() -> None:
    spot_pts = [(float(i), 100.0) for i in range(60)]
    f = _compute(*_bufs(spot_pts), t=100.0, feed_fresh=False)
    assert f.feed_fresh is False
    assert f.slope_60 is None  # 明确不可用，即使数据充足


def test_trend_momentum_extreme_values() -> None:
    # 0..99s，v=2t：t=99 时 slope=2，ret_60=(198-78)/78
    spot_pts = [(float(i), float(2 * i)) for i in range(100)]
    f = _compute(*_bufs(spot_pts), t=99.0)
    assert f.feed_fresh is True
    assert abs(float(f.slope_60) - 2.0) < 1e-9
    assert f.slope_15 == f.slope_60  # 纯线性 → acceleration=0
    assert f.acceleration == 0
    assert abs(float(f.ret_60) - 120 / 78) < 1e-9
    # 线性序列 |ret| 随窗口拉长而变大 → decay = |ret60| - |ret15|
    assert f.momentum_decay == abs(f.ret_60) - abs(f.ret_15)
    assert f.dist_high == 0 and f.dist_low == Decimal(198)
    assert f.time_since_high == 0.0
    assert f.new_high_count_30 == 29  # 近 30s：首点为基准，之后每秒新高


def test_token_and_favorite_deltas() -> None:
    uda = [(float(i), 0.20 + 0.001 * i) for i in range(101)]
    udb = [(float(i), 0.18) for i in range(101)]
    fa = [(float(i), 0.80) for i in range(101)]
    fb = [(float(i), 0.75 + 0.0005 * i) for i in range(101)]
    f = _compute(*_bufs(ud_a=uda, ud_b=udb, fa=fa, fb=fb), t=100.0)
    assert f.ud_ask_delta_5 == Decimal("0.005")     # 0.001*5
    assert f.ud_ask_delta_30 == Decimal("0.030")
    # mid_10 = ((0.3+0.18)/2) - ((0.29+0.18)/2) = 0.005
    assert f.ud_mid_delta_10 == Decimal("0.005")
    assert f.fav_mid_delta_30 == Decimal("0.0075")  # bid 涨 0.015 → mid 涨一半


def test_spot_token_divergence() -> None:
    # 现货 +10% 但冷门方 mid 不动 → divergence ≈ spot ret
    spot_pts = [(float(i), float(100 + i)) for i in range(101)]
    uda = [(float(i), 0.20) for i in range(101)]
    udb = [(float(i), 0.18) for i in range(101)]
    f = _compute(*_bufs(spot_pts=spot_pts, ud_a=uda, ud_b=udb), t=100.0)
    d30 = f.spot_token_divergence_30
    # spot ret_30 = (200-170)/170（mid 不动 → divergence = spot ret）
    assert d30 is not None and abs(float(d30) - 30 / 170) < 1e-9


def _sym_snapshot(trend: str, *, fresh=True) -> FeatureSnapshot:
    """构造对称情景：trend='down' 现货下行+冷门方(Up)走强；'up' 为镜像。"""
    if trend == "down":
        return FeatureSnapshot(
            feed_fresh=fresh, slope_60=Decimal("-1"), slope_15=Decimal("1"),
            slope_30=Decimal("-0.5"), ret_15=Decimal("0.0001"),
            ret_30=Decimal("0.0003"), dist_low=Decimal("1"),
            new_low_count_15=0, new_low_count_30=0,
            ud_ask_delta_30=Decimal("0.01"), relative_obi=Decimal("0.1"),
            fav_mid_delta_30=Decimal("-0.03"),
        )
    return FeatureSnapshot(
        feed_fresh=fresh, slope_60=Decimal("1"), slope_15=Decimal("-1"),
        slope_30=Decimal("0.5"), ret_15=Decimal("-0.0001"),
        ret_30=Decimal("-0.0003"), dist_high=Decimal("1"),
        new_high_count_15=0, new_high_count_30=0,
        ud_ask_delta_30=Decimal("0.01"), relative_obi=Decimal("0.1"),
        fav_mid_delta_30=Decimal("0.03"),
    )


def test_reversal_score_symmetric() -> None:
    """方向对称性：同一结构的镜像情景评分一致。"""
    down = reversal_score(_sym_snapshot("down"))
    up = reversal_score(_sym_snapshot("up"))
    # +2 slope反转 +2 ud买入 +1 掉头 +1 近极值 +1 不创极值 +1 OBI −2 fav 增强
    assert down == up == 6


def test_reversal_score_guards() -> None:
    assert reversal_score(_sym_snapshot("down", fresh=False)) is None
    f = _sym_snapshot("down")
    assert reversal_score(replace_f(f, slope_60=None)) is None
    assert reversal_score(replace_f(f, slope_60=Decimal(0))) is None


def replace_f(f: FeatureSnapshot, **kw) -> FeatureSnapshot:
    from dataclasses import replace
    return replace(f, **kw)


def test_reversal_score_penalties() -> None:
    # 趋势仍强：slope15/30 同向 + |ret15| 大 + burst 新低 ×3 + fav 增强 → −3−2−2
    f = FeatureSnapshot(
        feed_fresh=True, slope_60=Decimal("-1"), slope_15=Decimal("-1"),
        slope_30=Decimal("-1"), ret_15=Decimal("-0.001"),
        new_low_count_30=3, fav_mid_delta_30=Decimal("-0.05"),
    )
    assert reversal_score(f) == -7
    # 阈值内不算罚分：|ret15| 低于 trend_ret_15、fav 涨幅低于 fav_delta_30
    f2 = FeatureSnapshot(
        feed_fresh=True, slope_60=Decimal("-1"), slope_15=Decimal("-1"),
        slope_30=Decimal("-1"), ret_15=Decimal("-0.00001"),
        new_low_count_30=1, fav_mid_delta_30=Decimal("-0.001"),
    )
    assert reversal_score(f2) == 0


def test_load_score_weights() -> None:
    w = load_score_weights()  # 项目内置 score_weights.json
    assert w == DEFAULT_WEIGHTS
    assert load_score_weights("nonexistent.json") == DEFAULT_WEIGHTS


def test_snapshot_defaults_are_none() -> None:
    f = FeatureSnapshot()
    assert f.ret_30 is None and f.obi_up is None and f.feed_fresh is False
