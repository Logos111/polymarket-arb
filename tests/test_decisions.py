"""decisions.py 纯决策函数特征化测试（阶段 1 commit A）。

用例直接锚定 09-10/11 实盘验证过的现行行为（含逐字文案），
作为"行为不变"重构的特征化基线。
"""

from decimal import Decimal

from pm_arb.strategies.crypto_5m.decisions import (
    EntryAction,
    calc_size,
    decide_entry,
    decide_exit,
    decide_stop,
    fmt_price,
    pick_underdog,
)
from pm_arb.strategies.crypto_5m.params import Crypto5mParams

P = Crypto5mParams()


# ---- params 默认值锚定（防止无意改参） ----

def test_params_defaults():
    assert P.target_notional == Decimal("2.00")
    assert (P.entry_after, P.entry_until) == (70, 135)
    assert P.take_profit_price == Decimal("0.99")
    assert (P.min_entry, P.max_entry) == (Decimal("0.15"), Decimal("0.30"))
    assert P.max_vol == Decimal("30")
    assert (P.poll, P.ws_fresh_sec, P.feed_fresh_sec, P.end_margin) == (2.0, 10.0, 15.0, 60)


# ---- pick_underdog ----

def _book(up_ask, down_ask):
    return {
        "Up": {"best_ask": up_ask},
        "Down": {"best_ask": down_ask},
    }


def test_pick_underdog_prefers_cheaper():
    side, ask = pick_underdog(_book(Decimal("0.32"), Decimal("0.25")))
    assert (side, ask) == ("Down", Decimal("0.25"))


def test_pick_underdog_tie_goes_to_up():
    side, ask = pick_underdog(_book(Decimal("0.30"), Decimal("0.30")))
    assert (side, ask) == ("Up", Decimal("0.30"))


def test_pick_underdog_none_side():
    assert pick_underdog(_book(None, None)) == ("Up", None)
    assert pick_underdog(_book(None, Decimal("0.4"))) == ("Down", Decimal("0.4"))


# ---- calc_size ----

def test_calc_size_rounds_up_and_respects_min():
    assert calc_size(Decimal("0.28"), 5, Decimal("2.00")) == 8
    assert calc_size(Decimal("0.70"), 5, Decimal("2.00")) == 5  # min_size 兜底


# ---- decide_entry：时间窗 ----

def test_decide_entry_wait_and_missed():
    d = decide_entry(30.0, "Up", Decimal("0.28"), None, Decimal("5"), P)
    assert d.action is EntryAction.WAIT
    assert d.status == "等待 t∈[70,135]"
    d = decide_entry(200.0, "Up", Decimal("0.28"), None, Decimal("5"), P)
    assert d.action is EntryAction.MISSED
    assert d.status == "已错过"


# ---- decide_entry：不可恢复放弃 ----

def test_decide_entry_abort_data():
    d = decide_entry(80.0, "Up", None, None, None, P)
    assert d.action is EntryAction.ABORT_DATA
    assert d.status == "放弃(数据不全)"
    assert d.log == "[入场检查] 数据不全（ask=None range=None），放弃。"


def test_decide_entry_abort_vol():
    d = decide_entry(80.0, "Up", Decimal("0.28"), None, Decimal("30"), P)
    assert d.action is EntryAction.ABORT_VOL
    assert d.status == "放弃(波动$30)"
    assert d.log == "[入场检查] ❌ 波动 $30.00 ≥ $30，放弃。"


# ---- decide_entry：观察（可恢复） ----

def test_decide_entry_observe_high():
    d = decide_entry(80.0, "Up", Decimal("0.31"), None, Decimal("5"), P)
    assert d.action is EntryAction.OBSERVE
    assert d.status == "观察(ask0.31≥0.30)"
    assert "≥ 0.30，观察" in d.log


def test_decide_entry_observe_cold():
    d = decide_entry(80.0, "Up", Decimal("0.13"), None, Decimal("5"), P)
    assert d.action is EntryAction.OBSERVE
    assert d.status == "观察(ask0.13≤0.15过冷)"
    assert "过冷观察" in d.log


# ---- decide_entry：信号成立（实盘锚定 09-11 01:55 窗口场景） ----

def test_decide_entry_enter():
    d = decide_entry(80.0, "Up", Decimal("0.280"), 461, Decimal("24.61"), P)
    assert d.action is EntryAction.ENTER
    assert d.size == 8  # ceil(2.00/0.280)=8
    assert d.log == ("[入场检查] ✅ Up ask= 0.280（档深461份） 波动$24.61"
                     " → 市价买 ≈ $2.00，止盈  0.990")


# ---- decide_exit ----

def test_decide_exit_threshold():
    assert not decide_exit(Decimal("0.98"), P)
    assert decide_exit(Decimal("0.99"), P)
    assert not decide_exit(None, P)


# ---- decide_stop ----

def test_decide_stop_disabled_by_default():
    # 默认 stop_loss_price=0：任何价位都不止损
    assert not decide_stop(Decimal("0.01"), P)
    assert not decide_stop(None, P)


def test_decide_stop_threshold():
    Ps = Crypto5mParams(stop_loss_price=Decimal("0.10"))
    assert decide_stop(Decimal("0.10"), Ps)
    assert decide_stop(Decimal("0.05"), Ps)
    assert not decide_stop(Decimal("0.11"), Ps)
    assert not decide_stop(None, Ps)


# ---- fmt_price ----

def test_fmt_price():
    assert fmt_price(Decimal("0.28")) == " 0.280"
    assert fmt_price(None, 3) == "n/a"
