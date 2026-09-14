"""decide_entry_v2 单测（b09 E + b10 P3）：零回归锚定 + 评分/深度双门控语义。

- ``min_reversal_score=None`` 且 ``min_depth_ratio=None``（默认）→ 与
  decide_entry 逐字段相等（含 log 文案），覆盖全部判定分支；
- 评分门控：评分不足/不可用 → OBSERVE；评分达标 → 原判定放行；
- 深度门控（b10）：深度比低于阈值/不可用 → OBSERVE（fail-closed）；
- 两门控独立启停、可叠加；非 ENTER 分支不受门控影响。
"""

from __future__ import annotations

from decimal import Decimal

from pm_arb.strategies.crypto_5m.decisions import (
    EntryAction,
    book_depth_ratio,
    decide_entry,
    decide_entry_v2,
)
from pm_arb.strategies.crypto_5m.params import Crypto5mParams


def _p(**kw) -> Crypto5mParams:
    return Crypto5mParams().model_copy(update=kw)


# 覆盖 decide_entry 全部分支的场景表：(elapsed, ask, rng)
_SCENARIOS = [
    (10.0, Decimal("0.25"), Decimal("5")),    # WAIT（未开窗）
    (200.0, Decimal("0.25"), Decimal("5")),   # MISSED
    (100.0, None, Decimal("5")),              # ABORT_DATA（ask 缺失）
    (100.0, Decimal("0.25"), None),           # ABORT_DATA（rng 缺失）
    (100.0, Decimal("0.25"), Decimal("30")),  # ABORT_VOL（rng ≥ max_vol）
    (100.0, Decimal("0.30"), Decimal("5")),   # OBSERVE（ask ≥ max_entry）
    (100.0, Decimal("0.15"), Decimal("5")),   # OBSERVE（ask ≤ min_entry）
    (100.0, Decimal("0.25"), Decimal("5")),   # ENTER
]


def test_v2_disabled_equals_decide_entry_all_branches() -> None:
    """min_reversal_score=None：全分支逐字段等价（零回归锚定）。"""
    p = _p()
    for elapsed, ask, rng in _SCENARIOS:
        base = decide_entry(elapsed, "Down", ask, 50, rng, p, min_size=5)
        v2 = decide_entry_v2(elapsed, "Down", ask, 50, rng, p, min_size=5,
                             score=1)  # 即使传了 score 也不生效
        assert v2 == base, f"{elapsed}/{ask}/{rng}: {v2} != {base}"


def test_v2_gate_blocks_enter_on_low_score() -> None:
    p = _p(min_reversal_score=Decimal("4"))
    d = decide_entry_v2(100.0, "Down", Decimal("0.25"), 50, Decimal("5"), p,
                        score=3)
    assert d.action is EntryAction.OBSERVE
    assert "3" in d.status and "4" in d.status
    assert "评分" in d.log


def test_v2_gate_blocks_enter_on_unavailable_score() -> None:
    """§2.1 护栏：评分不可用（喂价不新鲜）绝不放行。"""
    p = _p(min_reversal_score=Decimal("1"))
    d = decide_entry_v2(100.0, "Down", Decimal("0.25"), 50, Decimal("5"), p,
                        score=None)
    assert d.action is EntryAction.OBSERVE
    assert d.status == "观察(评分不可用)"


def test_v2_gate_passes_on_sufficient_score() -> None:
    p = _p(min_reversal_score=Decimal("4"))
    for s in (4, 5, 8):
        d = decide_entry_v2(100.0, "Down", Decimal("0.25"), 50, Decimal("5"),
                            p, score=s)
        assert d.action is EntryAction.ENTER, f"score={s} 应放行"
        assert d.size == 8  # calc_size(0.25, 5, 2.00) = ceil(8)


def test_v2_gate_ignores_non_enter_branches() -> None:
    """门控只在基础判定 ENTER 时生效：WAIT/MISSED/ABORT/OBSERVE 原样返回。"""
    p = _p(min_reversal_score=Decimal("9"))  # 永不可达的高阈值
    for elapsed, ask, rng in _SCENARIOS[:-1]:
        base = decide_entry(elapsed, "Down", ask, 50, rng, p, min_size=5)
        v2 = decide_entry_v2(elapsed, "Down", ask, 50, rng, p, min_size=5,
                             score=0)
        assert v2 == base


def test_v2_boundary_score_equal_passes() -> None:
    """等于阈值即放行（≥ 语义）。"""
    p = _p(min_reversal_score=Decimal("4"))
    d = decide_entry_v2(100.0, "Down", Decimal("0.25"), 50, Decimal("5"), p,
                        score=4)
    assert d.action is EntryAction.ENTER


# ---- b10 P3：深度 veto 门控 ----

def test_v2_depth_disabled_zero_regression() -> None:
    """min_depth_ratio=None：即使传了 depth_ratio 也不生效（零回归）。"""
    p = _p()
    for elapsed, ask, rng in _SCENARIOS:
        base = decide_entry(elapsed, "Down", ask, 50, rng, p, min_size=5)
        v2 = decide_entry_v2(elapsed, "Down", ask, 50, rng, p, min_size=5,
                             score=1, depth_ratio=Decimal("0.10"))
        assert v2 == base, f"{elapsed}/{ask}/{rng}: {v2} != {base}"


def test_v2_depth_veto_blocks_enter() -> None:
    p = _p(min_depth_ratio=Decimal("0.27"))
    d = decide_entry_v2(100.0, "Down", Decimal("0.25"), 50, Decimal("5"), p,
                        depth_ratio=Decimal("0.20"))
    assert d.action is EntryAction.OBSERVE
    assert "0.20" in d.status and "0.27" in d.status
    assert "深度" in d.log


def test_v2_depth_unavailable_blocks_enter() -> None:
    """b10 护栏：深度比不可用（盘口档深/价格缺失）绝不放行。"""
    p = _p(min_depth_ratio=Decimal("0.27"))
    d = decide_entry_v2(100.0, "Down", Decimal("0.25"), 50, Decimal("5"), p,
                        depth_ratio=None)
    assert d.action is EntryAction.OBSERVE
    assert d.status == "观察(深度比不可用)"


def test_v2_depth_boundary_equal_passes() -> None:
    """等于阈值即放行（≥ 语义，与评分门控一致）。"""
    p = _p(min_depth_ratio=Decimal("0.27"))
    d = decide_entry_v2(100.0, "Down", Decimal("0.25"), 50, Decimal("5"), p,
                        depth_ratio=Decimal("0.27"))
    assert d.action is EntryAction.ENTER
    assert d.size == 8  # calc_size(0.25, 5, 2.00) = ceil(8)


def test_v2_both_gates_independent() -> None:
    """双门控独立：评分达标但深度 veto → OBSERVE；两者均达标 → ENTER。"""
    p = _p(min_reversal_score=Decimal("1"), min_depth_ratio=Decimal("0.27"))
    d = decide_entry_v2(100.0, "Down", Decimal("0.25"), 50, Decimal("5"), p,
                        score=3, depth_ratio=Decimal("0.10"))
    assert d.action is EntryAction.OBSERVE
    assert "深度" in d.status
    ok = decide_entry_v2(100.0, "Down", Decimal("0.25"), 50, Decimal("5"), p,
                         score=3, depth_ratio=Decimal("0.30"))
    assert ok.action is EntryAction.ENTER


def test_book_depth_ratio_computation() -> None:
    """口径锚定：冷门方 ask 名义 / 热门方 bid 名义（P0 重验同源）。"""
    book = {
        "Up": {"best_ask": Decimal("0.28"), "ask_size": 461,
               "best_bid": Decimal("0.27"), "bid_size": 500},
        "Down": {"best_ask": Decimal("0.73"), "ask_size": 461,
                 "best_bid": Decimal("0.72"), "bid_size": 500},
    }
    expect = Decimal("0.28") * 461 / (Decimal("0.72") * 500)
    assert book_depth_ratio(book, "Up") == expect
    # fail-closed：档深为零 / 价格缺失 → None
    assert book_depth_ratio({"Up": {"best_ask": Decimal("0.28"), "ask_size": 0},
                             "Down": {"best_bid": Decimal("0.72"),
                                      "bid_size": 500}}, "Up") is None
    assert book_depth_ratio({"Up": {"best_ask": Decimal("0.28"), "ask_size": 10},
                             "Down": {"best_bid": Decimal("0.72"),
                                      "bid_size": 0}}, "Up") is None
    assert book_depth_ratio({"Up": {"best_ask": None, "ask_size": 10},
                             "Down": {"best_bid": Decimal("0.72"),
                                      "bid_size": 500}}, "Up") is None
    # best_bid = 0（Decimal 除零陷阱）：分母为零 → None 不抛异常
    assert book_depth_ratio({"Up": {"best_ask": Decimal("0.28"), "ask_size": 10},
                             "Down": {"best_bid": Decimal("0"),
                                      "bid_size": 500}}, "Up") is None
