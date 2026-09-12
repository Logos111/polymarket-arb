"""风控闸门测试：纯函数边界 + RiskGate 装配 + Store 聚合。"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from pathlib import Path

import pytest

from pm_arb.infra.store import Store
from pm_arb.risk.gates import (
    DAY_SECONDS,
    GateContext,
    RiskGate,
    RiskLimits,
    check_order,
    gas_warning,
    utc_day_start,
)

D = Decimal
LIMITS = RiskLimits()


def _ctx(**kw) -> GateContext:
    """最小合法上下文（默认全通过），测试按需覆写。"""
    kw.setdefault("now_ts", 1_000_000)
    return GateContext(**kw)


# ---- utc_day_start ----


def test_utc_day_start_aligns_to_midnight():
    ts = 1_700_000_123  # 非对齐时刻
    ds = utc_day_start(ts)
    assert 0 <= ts - ds < DAY_SECONDS
    assert ds % DAY_SECONDS == 0


# ---- check_order：拒绝优先级与边界 ----


def test_pass_when_all_clean():
    v = check_order(D("5.00"), _ctx(), LIMITS)
    assert v.ok and v.code == "PASS"


def test_kill_switch_first_priority():
    # 即使名义超限，Kill Switch 也应最先命中
    v = check_order(D("999"), _ctx(kill_switch=True), LIMITS)
    assert not v.ok and v.code == "KILL_SWITCH"


def test_max_notional_boundary():
    assert check_order(LIMITS.max_notional, _ctx(), LIMITS).ok  # 等于上限放行
    v = check_order(LIMITS.max_notional + D("0.01"), _ctx(), LIMITS)
    assert not v.ok and v.code == "MAX_NOTIONAL"


def test_dup_window():
    v = check_order(D("1"), _ctx(same_window_open=True), LIMITS)
    assert not v.ok and v.code == "DUP_WINDOW"


def test_daily_cap_counts_today_invested():
    ctx = _ctx(invested_today=D("18.00"))
    v = check_order(D("2.00"), ctx, LIMITS)  # 恰好等于上限 → 放行
    assert v.ok
    v = check_order(D("2.01"), ctx, LIMITS)
    assert not v.ok and v.code == "DAILY_CAP"


def test_circuit_break_inclusive_boundary():
    ctx = _ctx(realized_today=-LIMITS.max_daily_loss)
    v = check_order(D("1"), ctx, LIMITS)
    assert not v.ok and v.code == "CIRCUIT_BREAK"
    # 亏损略小于阈值 → 放行
    ctx = _ctx(realized_today=-LIMITS.max_daily_loss + D("0.01"))
    assert check_order(D("1"), ctx, LIMITS).ok


def test_balance_only_checked_when_live():
    # live + 余额查询失败（None）→ fail-closed
    v = check_order(D("1"), _ctx(live=True, usdc_balance=None), LIMITS)
    assert not v.ok and v.code == "BALANCE_UNKNOWN"
    # live + 余额不足（名义 + buffer）
    v = check_order(D("5"), _ctx(live=True, usdc_balance=D("5.50")), LIMITS)
    assert not v.ok and v.code == "BALANCE_LOW"
    # live + 余额恰好够 → 放行
    v = check_order(
        D("5"), _ctx(live=True, usdc_balance=D("6.00")), LIMITS)
    assert v.ok
    # dry-run：余额未知也放行（闸门只对真金 fail-closed）
    assert check_order(D("5"), _ctx(live=False, usdc_balance=None), LIMITS).ok


# ---- gas 告警（仅提醒，不阻断）----


def test_gas_warning_boundary():
    assert gas_warning(None, LIMITS) is None  # 未查询不告警
    assert gas_warning(D("1.00"), LIMITS) is None  # 恰好达线
    w = gas_warning(D("0.99"), LIMITS)
    assert w is not None and "POL" in w


# ---- RiskGate 装配层（async，依赖注入）----


def test_risk_gate_assembles_context(tmp_path: Path):
    gate = RiskGate(
        limits=LIMITS,
        kill_path=tmp_path / "KILL",
        live=False,
        day_stats_fn=lambda day_start: (D("18"), D("-3")),
        window_open_fn=lambda ws: ws == 300,
    )
    # 同窗口已有持仓 → 拒绝
    v = asyncio.run(gate.check(D("1"), now_ts=600, window_start=300))
    assert not v.ok and v.code == "DUP_WINDOW"
    # 当日投入 18 + 本次 3 > 20 → DAILY_CAP
    v = asyncio.run(gate.check(D("3"), now_ts=600, window_start=900))
    assert not v.ok and v.code == "DAILY_CAP"
    # Kill 文件落下即生效
    (tmp_path / "KILL").write_text("")
    v = asyncio.run(gate.check(D("1"), now_ts=600, window_start=900))
    assert not v.ok and v.code == "KILL_SWITCH"


def test_risk_gate_live_fail_closed_on_balance_error():
    async def broken() -> Decimal | None:
        raise RuntimeError("rpc down")

    gate = RiskGate(
        limits=LIMITS, live=True,
        balance_fn=broken,  # type: ignore[arg-type]
    )
    v = asyncio.run(gate.check(D("1"), now_ts=600))
    assert not v.ok and v.code == "BALANCE_UNKNOWN"


def test_risk_gate_day_stats_receives_day_start():
    seen: list[int] = []
    gate = RiskGate(
        limits=LIMITS,
        day_stats_fn=lambda ds: (seen.append(ds), (D(0), D(0)))[1],
    )
    asyncio.run(gate.check(D("1"), now_ts=DAY_SECONDS + 5))
    assert seen == [DAY_SECONDS]  # 注入的 day_start 已按 UTC 日界对齐


# ---- Store 聚合（day_stats / has_open_position）----


def _mk_store(tmp_path: Path) -> Store:
    return Store(str(tmp_path / "t.sqlite3"))


def test_store_day_stats_aggregates_today_only(tmp_path: Path):
    store = _mk_store(tmp_path)
    day0, day1 = DAY_SECONDS, 2 * DAY_SECONDS
    store.upsert_window("btc", day0 + 300, entered=True,
                        entry_cost=D("4"), realized_pnl=D("1"))
    store.upsert_window("btc", day0 + 900, entered=True,
                        entry_cost=D("2"), settle_pnl=D("-2"))
    store.upsert_window("eth", day1 + 300, entered=True,
                        entry_cost=D("50"), realized_pnl=D("10"))
    invested, realized = store.day_stats(day0)
    assert invested == D("6")
    assert realized == D("-1")
    invested, realized = store.day_stats(day1)
    assert invested == D("50") and realized == D("10")
    store.close()


def test_store_has_open_position(tmp_path: Path):
    store = _mk_store(tmp_path)
    store.open_position(symbol="btc", window_start=300, token_id="1",
                        condition_id="0xc", side="Up",
                        entry_price=D("0.4"), filled_size=D("5"), fee=D("0.1"))
    assert store.has_open_position("btc", 300)
    assert not store.has_open_position("btc", 600)
    assert not store.has_open_position("eth", 300)
    store.close_position(symbol="btc", window_start=300,
                         exit_price=D("0.8"), realized_pnl=D("1"))
    assert not store.has_open_position("btc", 300)  # 平仓后不再阻断
    store.close()


def test_store_day_stats_empty(tmp_path: Path):
    store = _mk_store(tmp_path)
    assert store.day_stats(DAY_SECONDS) == (D(0), D(0))
    store.close()


@pytest.mark.parametrize("code", ["KILL_SWITCH", "CIRCUIT_BREAK"])
def test_hard_stop_codes_are_the_global_ones(code: str):
    """orchestrator 依赖这两个 code 判定中止全部窗口——契约锁定。"""
    hard = {"KILL_SWITCH", "CIRCUIT_BREAK"}
    ctx = _ctx(kill_switch=(code == "KILL_SWITCH"),
               realized_today=(-LIMITS.max_daily_loss
                               if code == "CIRCUIT_BREAK" else D(0)))
    assert check_order(D("1"), ctx, LIMITS).code == code
    assert code in hard
