"""SeriesBuffer 边界单测（b09 A：空缓冲/单点/超龄/样本不足/maxlen 淘汰）。"""

from decimal import Decimal

from pm_arb.data.series_buffer import SeriesBuffer


def _push_seq(b: SeriesBuffer, pts: list[tuple[float, float]]) -> None:
    for t, v in pts:
        b.push(t, Decimal(str(v)))


def test_push_and_len_maxlen_eviction() -> None:
    b = SeriesBuffer(maxlen_sec=10.0)
    _push_seq(b, [(0, 1), (5, 2), (9, 3), (12, 4)])
    # 12-10=2 之前（t<2）的点被淘汰：仅移除 t=0，剩 (5,9,12)
    assert len(b) == 3
    assert b.value_asof(12.0) == Decimal("4")


def test_push_none_skipped() -> None:
    b = SeriesBuffer()
    b.push(1.0, None)
    b.push(2.0, Decimal("3"))
    assert len(b) == 1


def test_value_asof_empty_and_before_start() -> None:
    b = SeriesBuffer()
    assert b.value_asof(5.0) is None
    _push_seq(b, [(10, 1)])
    assert b.value_asof(9.0) is None  # 早于首点
    assert b.value_asof(10.0) == Decimal("1")


def test_value_asof_max_age() -> None:
    b = SeriesBuffer()
    _push_seq(b, [(10, 5), (40, 7)])
    # t=42：最近点 40 距今 2s，fresh
    assert b.value_asof(42.0, max_age=5) == Decimal("7")
    # t=50：最近点 40 距今 10s，超龄 → None（"不知道"≠旧值）
    assert b.value_asof(50.0, max_age=5) is None
    assert b.value_asof(50.0, max_age=None) == Decimal("7")


def test_sample_count_window() -> None:
    b = SeriesBuffer()
    _push_seq(b, [(0, 1), (5, 1), (10, 1), (11, 1), (20, 1)])
    assert b.sample_count(20.0, 10.0) == 2   # (10,11,20] 半开：11,20 → 2
    assert b.sample_count(20.0, 100.0) == 5
    assert b.sample_count(3.0, 10.0) == 1    # 仅 t=0


def test_ret_basic_and_missing() -> None:
    b = SeriesBuffer()
    _push_seq(b, [(10, 100), (20, 110)])
    assert b.ret(20.0, 10.0) == Decimal("0.1")
    # 端点超龄
    assert b.ret(20.0, 15.0) is None  # t-15=5 早于首点 10
    # 分母 0
    b2 = SeriesBuffer()
    _push_seq(b2, [(0, 0), (5, 10)])
    assert b2.ret(5.0, 5.0) is None


def test_slope_least_squares() -> None:
    b = SeriesBuffer()
    # 完美线性：v = 2t（t=0..9 每秒一点）
    _push_seq(b, [(float(i), float(2 * i)) for i in range(10)])
    s = b.slope(9.0, 10.0)
    assert s is not None and abs(float(s) - 2.0) < 1e-9
    # 样本不足
    b2 = SeriesBuffer()
    _push_seq(b2, [(0, 1), (1, 2)])
    assert b2.slope(1.0, 10.0) is None  # min_samples=3
    # 空缓冲
    assert SeriesBuffer().slope(5.0, 10.0) is None


def test_slope_window_filter() -> None:
    b = SeriesBuffer()
    _push_seq(b, [(0, 0), (1, 2), (2, 4), (10, 100), (11, 102)])
    # 窗口 5s：仅 (10,11) 两点 → 不足
    assert b.slope(11.0, 5.0) is None
    # 窗口 12s：全部 5 点，前段斜率 2、后段 2 —— 整体回归斜率接近 8.75?不精确，
    # 只断言非 None 且为正
    s = b.slope(11.0, 12.0)
    assert s is not None and float(s) > 0


def test_new_extreme_count() -> None:
    b = SeriesBuffer()
    _push_seq(b, [(0, 10), (1, 12), (2, 11), (3, 13), (4, 13), (5, 9)])
    assert b.new_extreme_count(5.0, 6.0, "high") == 2  # 12、13 两次新高
    assert b.new_extreme_count(5.0, 6.0, "low") == 1   # 9 新低
    # 窗口外样本不计
    assert b.new_extreme_count(5.0, 2.0, "high") == 0  # (3,5]：13,13,9 → 无新高
    # 空窗口
    assert b.new_extreme_count(100.0, 5.0, "high") is None
    # 单样本 → 0
    b2 = SeriesBuffer()
    _push_seq(b2, [(0, 5)])
    assert b2.new_extreme_count(0.0, 5.0, "high") == 0


def test_time_since_extreme_and_extremes() -> None:
    b = SeriesBuffer()
    _push_seq(b, [(0, 10), (5, 20), (8, 12)])
    assert b.time_since_extreme(8.0, "high") == 3.0   # 极值在 t=5
    assert b.time_since_extreme(8.0, "low") == 8.0    # 极值在 t=0
    hi, lo = b.extremes()
    assert hi == Decimal("20") and lo == Decimal("10")
    assert SeriesBuffer().extremes() is None
    assert SeriesBuffer().time_since_extreme(1.0, "high") is None


def test_buffer_persists_query_after_push() -> None:
    """push 后旧查询仍正确（淘汰只影响 maxlen 之外）。"""
    b = SeriesBuffer(maxlen_sec=100.0)
    _push_seq(b, [(float(i), float(i)) for i in range(50)])
    got = b.ret(49.0, 30.0)  # (49-19)/19 = 30/19
    assert got is not None and abs(float(got) - 30 / 19) < 1e-9
    assert b.sample_count(49.0, 50.0) == 50
