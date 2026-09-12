"""HF 回测引擎正确性测试（合成 tick 流，不依赖 parquet）。"""

from decimal import Decimal

from pm_arb.backtest.engine import ExitKind, replay_window, taker_fee
from pm_arb.backtest.hf_loader import HfMarket
from pm_arb.strategies.crypto_5m.params import Crypto5mParams

P = Crypto5mParams()
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
