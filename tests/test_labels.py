"""labels.py 单测（b09 C）：future_return / MFE / MAE / final_outcome 正确性。

合成 tick 序列手工可算，验证 Label 计算（仅回测研究用；前视信息
只允许出现在 labels.py，泄漏护栏见 test_no_lookahead_leak.py）。
"""

from __future__ import annotations

from types import SimpleNamespace

from pm_arb.strategies.crypto_5m.labels import _asof, compute_window_labels


def _mk(cand: str, t: int, cand_ask: float) -> dict:
    return {"cand": cand, "t": t, "cand_ask": cand_ask}


def _tick(t: int, au: float, bu: float, ad: float, bd: float) -> dict:
    return {"t": t, "au": au, "bu": bu, "ad": ad, "bd": bd}


def test_asof_basic_and_stale() -> None:
    ts = [0, 2, 4]
    vals = [1.0, 2.0, 3.0]
    assert _asof(ts, vals, 3) == 2.0      # 最近不晚于 3 → t=2
    assert _asof(ts, vals, 4) == 3.0
    assert _asof(ts, vals, -1) is None    # 早于首点（无任何点不晚于 t）
    assert _asof(ts, vals, 4, max_age=1) == 3.0
    assert _asof(ts, vals, 9, max_age=2) is None  # 超龄


def test_future_return_labels() -> None:
    # Up 侧 mid：t=0 起 (au+bu)/2 = 0.20；t=60 变 0.25；t=120 变 0.30
    ticks = [
        _tick(0, 0.21, 0.19, 0.81, 0.79),
        _tick(30, 0.21, 0.19, 0.81, 0.79),
        _tick(60, 0.26, 0.24, 0.76, 0.74),
        _tick(120, 0.31, 0.29, 0.71, 0.69),
    ]
    mkt = SimpleNamespace(outcome="Up")
    rows = [_mk("Up", 0, 0.21)]
    compute_window_labels(rows, ticks, mkt)
    r = rows[0]
    assert abs(r["future_return_30"] - 0.0) < 1e-9   # t=30 mid 不变
    assert abs(r["future_return_60"] - 0.05 / 0.20) < 1e-9
    assert abs(r["future_return_120"] - 0.10 / 0.20) < 1e-9
    assert r["final_outcome"] == "Up"


def test_mfe_mae_against_cand_ask() -> None:
    # Up 侧 bid 序列（t, bu）：0.19 → 0.44（超 ask 0.21 → MFE>0）→ 0.14（MAE）
    ticks = [
        _tick(0, 0.21, 0.19, 0.81, 0.79),
        _tick(30, 0.21, 0.44, 0.81, 0.79),
        _tick(60, 0.21, 0.14, 0.81, 0.79),
        _tick(90, 0.21, 0.19, 0.81, 0.79),   # 60s 窗外，不计入
    ]
    mkt = SimpleNamespace(outcome="Down")
    rows = [_mk("Up", 0, 0.21)]
    compute_window_labels(rows, ticks, mkt)
    r = rows[0]
    # 未来 60s（t0<t<=60）内 bid 高点 0.44 / 低点 0.14（t=0 自身 bid 不算）
    assert abs(r["mfe_60"] - (0.44 - 0.21)) < 1e-9
    assert abs(r["mae_60"] - (0.21 - 0.14)) < 1e-9
    assert r["final_outcome"] == "Down"


def test_missing_ask_row_skipped() -> None:
    ticks = [_tick(0, 0.21, 0.19, 0.81, 0.79)]
    mkt = SimpleNamespace(outcome="Up")
    rows = [{"cand": "Up", "t": 0}]      # 无 cand_ask
    compute_window_labels(rows, ticks, mkt)
    assert "mfe_60" not in rows[0]
    assert rows[0]["final_outcome"] == "Up"   # outcome 不依赖 ask


def test_none_bid_gap_ignored() -> None:
    # t=30 双边盘口失效（0 价）→ 该点不计入 MFE/MAE
    ticks = [
        _tick(0, 0.21, 0.19, 0.81, 0.79),
        _tick(30, 0.0, 0.0, 0.0, 0.0),
        _tick(60, 0.21, 0.44, 0.81, 0.79),
    ]
    mkt = SimpleNamespace(outcome="Up")
    rows = [_mk("Up", 0, 0.21)]
    compute_window_labels(rows, ticks, mkt)
    # 未来 60s 内有效点仅 t=60（bid 0.44）：高点=低点=0.44
    assert abs(rows[0]["mfe_60"] - (0.44 - 0.21)) < 1e-9
    assert abs(rows[0]["mae_60"] - (0.21 - 0.44)) < 1e-9  # 未来只涨 → 负 MAE
