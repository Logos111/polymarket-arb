"""SpotVol 现货波动重建测试（合成 1s K 线，不依赖真实 parquet）。"""

from decimal import Decimal

import pyarrow as pa
import pyarrow.parquet as pq

from pm_arb.strategies.crypto_5m.backtest.spot_vol import SpotVol

WS = 1100  # 测试窗口起点


def _write_spot(tmp_path, ts: list[int], close: list[float]) -> str:
    d = tmp_path / "hf"
    d.mkdir(exist_ok=True)
    tbl = pa.table({"ts": pa.array(ts, type=pa.int64()),
                    "close": pa.array(close, type=pa.float64())})
    pq.write_table(tbl, d / "btc_spot_1s.parquet")
    return str(d)


def test_window_rng_seq_flat_price(tmp_path):
    # 恒定价格 → twap60 恒定 → rng 恒 0
    ts = list(range(1000, 1500))
    d = _write_spot(tmp_path, ts, [100.0] * len(ts))
    sv = SpotVol("btc", d)
    rng = sv.window_rng_seq(WS, list(range(WS, WS + 300)))
    assert len(rng) == 300
    assert all(r == Decimal(0) for r in rng)


def test_window_rng_seq_linear_trend(tmp_path):
    # 价格每秒 +1：twap60(t) = close(t) - 29.5，rng(t) = twap(t) - twap(ws) = t - ws
    ts = list(range(1000, 1500))
    d = _write_spot(tmp_path, ts, [float(t) for t in ts])
    sv = SpotVol("btc", d)
    rng = sv.window_rng_seq(WS, list(range(WS, WS + 300)))
    assert rng[0] == Decimal(0)
    assert rng[10] == Decimal(10)
    assert rng[299] == Decimal(299)


def test_window_rng_seq_low_coverage_returns_none(tmp_path):
    # 只覆盖一半秒（< 95%）→ 全 None（ABORT_DATA 口径）
    ts = list(range(1000, 1500, 2))
    d = _write_spot(tmp_path, ts, [100.0] * len(ts))
    sv = SpotVol("btc", d)
    rng = sv.window_rng_seq(WS, list(range(WS, WS + 300)))
    assert rng == [None] * 300


def test_window_rng_seq_missing_history_returns_none(tmp_path):
    # 数据从窗口起点才开始（缺窗口前 60s 历史）→ 全 None
    ts = list(range(WS, WS + 300))
    d = _write_spot(tmp_path, ts, [100.0] * len(ts))
    sv = SpotVol("btc", d)
    rng = sv.window_rng_seq(WS, list(range(WS, WS + 300)))
    assert rng == [None] * 300


def test_window_rng_seq_sparse_seconds_forward_fill(tmp_path):
    # 个别缺秒（覆盖率 ≥95%）：前向填充不失败
    ts = [t for t in range(1000, 1500) if t % 97 != 0]  # 抽掉约 1% 的秒
    d = _write_spot(tmp_path, ts, [100.0] * len(ts))
    sv = SpotVol("btc", d)
    rng = sv.window_rng_seq(WS, list(range(WS, WS + 300)))
    assert all(r == Decimal(0) for r in rng)
