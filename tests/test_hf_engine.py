"""HF 回测引擎正确性测试（合成 tick 流，不依赖 parquet）。"""

from dataclasses import fields as dataclasses_fields
from decimal import Decimal

from pm_arb.strategies.crypto_5m.backtest.engine import (
    ExitKind,
    FeatureCapture,
    replay_ticks,
    replay_window,
    taker_fee,
)
from pm_arb.strategies.crypto_5m.backtest.hf_loader import HfMarket
from pm_arb.strategies.crypto_5m.features import FeatureSnapshot
from pm_arb.strategies.crypto_5m.params import Crypto5mParams

# 显式锚定参数：不随 params.py 默认值漂移（实盘默认 take_profit 已改 0.99）
P = Crypto5mParams(entry_after=70, entry_until=135,
                   take_profit_price=Decimal("0.65"),
                   min_entry=Decimal("0.15"), max_entry=Decimal("0.30"))
CID = "0xabc"
START = 1_800_000_000


class FakeDS:
    """duck-typing HfDataset：engine 只用 symbol/markets/has_ticks/window_ticks。"""

    symbol = "btc"

    def __init__(self, ticks: list[dict]) -> None:
        self._ticks = ticks
        self.markets = []

    def has_ticks(self, cid: str) -> bool:
        return True

    def window_ticks(self, cid: str) -> list[dict]:
        return self._ticks


def tick(t_off: int, *, au=0.28, bu=0.27, ad=0.73, bd=0.72,
         sau=461, sad=461, su=500, sd=500) -> dict:
    return {"t": START + t_off, "bu": bu, "au": au, "bd": bd, "ad": ad,
            "su": su, "sd": sd, "sau": sau, "sad": sad}


def mkt(outcome: str) -> HfMarket:
    return HfMarket(condition_id=CID, slug=f"btc-updown-5m-{START}", start=START,
                    outcome=outcome, volume=100.0, liquidity=10_000.0)


def test_taker_fee_formula():
    # fee = shares × 0.07 × p × (1−p)
    assert taker_fee(100, Decimal("0.5")) == Decimal("1.75")
    assert taker_fee(10, Decimal("0.28")) == Decimal("0.14112")


def test_settle_lose_no_bounce():
    # 入场 0.28，之后 bid 不涨、结算 Down → 冷门方 Up 输
    ticks = [tick(t) for t in range(80, 300)]
    res = replay_window(FakeDS(ticks), mkt("Down"), P)
    assert res.exit_kind is ExitKind.SETTLE_LOSE
    assert res.size == 8  # max(ceil(2/0.28), 5)
    assert res.entry_ask == Decimal("0.28")
    # pnl = -(8×0.28 + fee)
    fee = taker_fee(8, Decimal("0.28"))
    assert res.pnl == -(Decimal(8) * Decimal("0.28") + fee)


def test_settle_win_but_take_profit_intercepts():
    # 冷门方最终赢：bid 中途必然穿过止盈价 0.65 → 被止盈截获
    ticks = [tick(80), tick(90, au=0.40, bu=0.39), tick(100, au=0.70, bu=0.68)]
    res = replay_window(FakeDS(ticks), mkt("Up"), P)
    assert res.exit_kind is ExitKind.TAKE_PROFIT
    assert res.entry_t == START + 80
    gain = Decimal(8) * (Decimal("0.68") - Decimal("0.28"))
    fees = taker_fee(8, Decimal("0.28")) + taker_fee(8, Decimal("0.68"))
    assert res.pnl == gain - fees
    assert res.fee == fees


def test_depth_short_aborts_window():
    # 档深 3 < 目标 8 份 → 保守放弃整窗
    ticks = [tick(80, sau=3)]
    res = replay_window(FakeDS(ticks), mkt("Up"), P)
    assert res.exit_kind is ExitKind.NO_ENTRY_DEPTH
    assert res.size == 0


def test_no_signal_when_ask_out_of_band():
    # ask=0.40 越过 max_entry → 观察不入场
    ticks = [tick(t, au=0.40, ad=0.61) for t in range(80, 300)]
    res = replay_window(FakeDS(ticks), mkt("Up"), P)
    assert res.exit_kind is ExitKind.NO_ENTRY
    assert res.size == 0


def test_no_entry_before_window():
    # 全部 tick 在入场窗之前（elapsed<70）
    ticks = [tick(t) for t in range(0, 60)]
    res = replay_window(FakeDS(ticks), mkt("Up"), P)
    assert res.exit_kind is ExitKind.NO_ENTRY


def test_bid_depth_short_holds_to_settle():
    # 止盈价曾到达但 bid 档深 < 持仓 → 不卖，持有到结算赢
    ticks = [tick(80), tick(100, au=0.70, bu=0.68, su=2), tick(200)]
    res = replay_window(FakeDS(ticks), mkt("Up"), P)
    assert res.exit_kind is ExitKind.SETTLE_WIN
    assert res.pnl == Decimal(8) - (Decimal(8) * Decimal("0.28") + taker_fee(8, Decimal("0.28")))


# ---- 止损（stop_loss_price>0 启用，与止盈同一保守档深门槛）----

PSL = Crypto5mParams(entry_after=70, entry_until=135,
                     take_profit_price=Decimal("0.65"),
                     stop_loss_price=Decimal("0.10"),
                     min_entry=Decimal("0.15"), max_entry=Decimal("0.30"))


def test_stop_loss_exit():
    # bid 跌破止损价 0.10 且档深足 → 市价止损卖出（双边手续费）
    ticks = [tick(80), tick(100, au=0.12, bu=0.10)]
    res = replay_window(FakeDS(ticks), mkt("Down"), PSL)
    assert res.exit_kind is ExitKind.STOP_LOSS
    fees = taker_fee(8, Decimal("0.28")) + taker_fee(8, Decimal("0.10"))
    assert res.pnl == Decimal(8) * Decimal("0.10") - Decimal(8) * Decimal("0.28") - fees
    assert res.fee == fees


def test_stop_loss_default_off():
    # 默认 stop_loss_price=0：同样走势不止损，持有到结算输
    ticks = [tick(80), tick(100, au=0.12, bu=0.10), tick(299)]
    res = replay_window(FakeDS(ticks), mkt("Down"), P)
    assert res.exit_kind is ExitKind.SETTLE_LOSE


def test_stop_loss_depth_short_holds():
    # 止损价到达但 bid 档深不足 → 保守持有到结算
    ticks = [tick(80), tick(100, au=0.12, bu=0.10, su=2)]
    res = replay_window(FakeDS(ticks), mkt("Down"), PSL)
    assert res.exit_kind is ExitKind.SETTLE_LOSE


# ---- 波动过滤（rng_seq 现货序列；b07）----

def test_vol_filter_aborts_entry():
    # rng_seq 全程超 max_vol=30 → ABORT_VOL 不入场（rng 单调不减，重复拒绝）
    ticks = [tick(t) for t in range(80, 120)]
    rng_seq = [Decimal("50")] * len(ticks)
    res = replay_ticks(ticks, mkt("Up"), P, rng_seq=rng_seq)
    assert res.exit_kind is ExitKind.NO_ENTRY
    assert res.size == 0


def test_vol_data_missing_aborts_entry():
    # rng None（现货数据不全）→ ABORT_DATA 不入场（与实盘“数据不全”同口径）
    ticks = [tick(t) for t in range(80, 120)]
    rng_seq = [None] * len(ticks)
    res = replay_ticks(ticks, mkt("Up"), P, rng_seq=rng_seq)
    assert res.exit_kind is ExitKind.NO_ENTRY
    assert res.size == 0


def test_vol_below_threshold_enters():
    # rng 低于 max_vol → 正常入场（与 rng=0 旧行为一致）
    ticks = [tick(t) for t in range(80, 120)]
    rng_seq = [Decimal("12.5")] * len(ticks)
    res = replay_ticks(ticks, mkt("Up"), P, rng_seq=rng_seq)
    assert res.exit_kind is ExitKind.SETTLE_WIN
    assert res.size == 8


# ---- b09 capture（特征采集旁路；capture=None 零回归 + 行语义）----


def test_capture_none_zero_regression():
    """显式传 capture=None/twap_seq=None 与不传完全一致（零回归锚定）。"""
    ticks = [tick(t) for t in range(60, 300)]
    base = replay_ticks(ticks, mkt("Up"), P)
    same = replay_ticks(ticks, mkt("Up"), P, rng_seq=None, twap_seq=None,
                         capture=None)
    assert same.exit_kind == base.exit_kind
    assert same.pnl == base.pnl
    assert same.entry_t == base.entry_t
    assert same.size == base.size


def test_capture_rows_schema_and_window():
    """采集窗 [entry_after-30, entry_until+30] 逐秒一行；schema 完整。"""
    ticks = [tick(t) for t in range(0, 300)]
    cap = FeatureCapture(symbol="btc")
    replay_ticks(ticks, mkt("Up"), P, capture=cap)
    # P.entry_after=70，entry_until=135 → 采集 [40, 165] 共 126 行
    assert len(cap.rows) == 126
    r = cap.rows[0]
    assert r["elapsed"] == 40.0 and r["cand"] == "Up"
    assert r["symbol"] == "btc" and r["window_start"] == START
    assert r["cand_ask"] == 0.28
    snap_fields = {f.name for f in dataclasses_fields(FeatureSnapshot)}
    assert snap_fields <= set(r)
    assert cap.rows[-1]["elapsed"] == 165.0


def test_capture_entered_marking():
    """入场 tick（首个 elapsed≥entry_after，t=70）行 entered=True。"""
    ticks = [tick(t) for t in range(0, 200)]
    cap = FeatureCapture()
    replay_ticks(ticks, mkt("Up"), P, capture=cap)
    by_t = {r["t"]: r for r in cap.rows}
    assert by_t[START + 69]["entered"] is False
    assert by_t[START + 70]["entered"] is True   # 入场 tick 事后补标
    assert by_t[START + 71]["entered"] is True


def test_capture_no_spot_trend_all_none():
    """无现货 TWAP：feed_fresh=False，Trend 字段全 None（§2.1 护栏）。"""
    ticks = [tick(t) for t in range(0, 200)]
    cap = FeatureCapture()
    replay_ticks(ticks, mkt("Up"), P, twap_seq=None, capture=cap)
    assert cap.rows
    for r in cap.rows:
        assert r["feed_fresh"] is False
        assert r["ret_60"] is None
        assert r["slope_30"] is None
        assert r["score"] is None   # 评分同样不可用


def test_capture_with_spot_feed_fresh():
    """有现货 TWAP 序列：feed_fresh 转真，Trend 特征可算。"""
    ticks = [tick(t) for t in range(0, 200)]
    twap = [Decimal("77000") + Decimal(i) for i in range(200)]
    cap = FeatureCapture()
    replay_ticks(ticks, mkt("Up"), P, twap_seq=twap, capture=cap)
    # 1Hz 满采样 60s → 采样数 60 ≥ 阈值，末尾行 fresh
    late = [r for r in cap.rows if r["elapsed"] >= 100]
    assert any(r["feed_fresh"] for r in late)
    fresh_rows = [r for r in late if r["feed_fresh"]]
    assert all(r["ret_60"] is not None for r in fresh_rows)
