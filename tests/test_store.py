"""阶段 2 SQLite 结构层测试：orders/windows/positions 三表 + settlement 纯函数。"""

import asyncio
from decimal import Decimal

import pytest

from pm_arb.data.models import Market
from pm_arb.data.settlement import (
    market_winner,
    settle_key,
    settle_result,
    taker_fee,
)
from pm_arb.execution.orders import Order, Side
from pm_arb.infra.store import Store


@pytest.fixture()
def store(tmp_path):
    s = Store(tmp_path / "t.sqlite3")
    yield s
    s.close()


# ---- orders ----

def test_order_upsert_and_fill_ratio(store):
    o = Order(token_id="tok", side=Side.BUY, price=Decimal("0.5"),
              size=Decimal("4"), filled_size=Decimal("4"),
              avg_fill_price=Decimal("0.5"))
    store.upsert_order(o, mode="live", symbol="btc", window_start=1000,
                       target_notional=Decimal("2"))
    cur = store.conn.execute("SELECT * FROM orders")
    r = dict(zip([c[0] for c in cur.description], cur.fetchone(), strict=True))
    assert r["mode"] == "live"
    assert Decimal(r["fill_ratio"]) == Decimal("1")  # 成交额 $2 / 目标 $2
    # 回调再次触发（状态更新）→ 覆盖写不新增行
    o.mark_rejected("x")
    store.upsert_order(o, mode="live", symbol="btc", window_start=1000)
    assert store.conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 1
    assert store.conn.execute("SELECT status FROM orders").fetchone()[0] == "REJECTED"


# ---- windows ----

def test_window_partial_upsert_merges(store):
    store.upsert_window("btc", 1000, slug="btc-updown-5m-1000")
    store.upsert_window("btc", 1000, entered=True, side="Up",
                        entry_price=Decimal("0.28"), filled_size=Decimal("7"),
                        entry_cost=Decimal("1.96"))
    cur = store.conn.execute("SELECT * FROM windows")
    row = dict(zip([c[0] for c in cur.description], cur.fetchone(), strict=True))
    assert row["slug"] == "btc-updown-5m-1000"  # 第一次写入保留
    assert row["entered"] == 1 and row["side"] == "Up"
    assert Decimal(row["entry_cost"]) == Decimal("1.96")
    assert "settlement" in row


# ---- positions 生命周期 ----

def test_position_lifecycle_and_pending_settlement(store):
    store.open_position(symbol="btc", window_start=1000, token_id="tok",
                        condition_id="0xabc", side="Up",
                        entry_price=Decimal("0.28"), filled_size=Decimal("7"),
                        fee=Decimal("0.098"))
    # 窗口未结束 → 不进待结算队列
    assert store.pending_settlement(now_ts=1000 + 300 + 359) == []
    # 结束 + 宽限期 → 出队
    pend = store.pending_settlement(now_ts=1000 + 300 + 360)
    assert len(pend) == 1 and pend[0]["side"] == "Up"
    assert Decimal(pend[0]["cost"]) == Decimal("1.96")
    store.settle_position(symbol="btc", window_start=1000, won=True,
                          realized_pnl=Decimal("4.942"))
    assert store.open_positions() == []
    assert store.pending_settlement(now_ts=10**9) == []


def test_close_position_and_redeem_mark(store):
    store.open_position(symbol="eth", window_start=2000, token_id="t",
                        condition_id=None, side="Down",
                        entry_price=Decimal("0.25"), filled_size=Decimal("8"))
    store.close_position(symbol="eth", window_start=2000,
                         exit_price=Decimal("0.99"), realized_pnl=Decimal("5.72"))
    store.mark_redeemed(symbol="eth", window_start=2000)
    row = store.conn.execute(
        "SELECT status, redeemed, realized_pnl FROM positions").fetchone()
    assert row[0] == "closed" and row[1] == 1
    assert Decimal(row[2]) == Decimal("5.72")


# ---- settlement 纯函数 ----

def _mkt(closed: bool, prices: list[str]) -> Market:
    return Market(id="1", slug="btc-updown-5m-1000", closed=closed,
                  outcomes=["Up", "Down"], outcome_prices=prices,
                  condition_id="0xabc")


def test_market_winner_parsing():
    assert market_winner(_mkt(True, ["1", "0"])) == "Up"
    assert market_winner(_mkt(True, ["0", "1"])) == "Down"
    assert market_winner(_mkt(False, ["0.5", "0.5"])) is None  # 未结算不可信
    assert market_winner(_mkt(True, ["0.5", "0.5"])) is None   # 异常保护


def test_taker_fee_formula():
    # 8 份 @ 0.28：8 × 0.07 × 0.28 × 0.72 = 0.112896
    assert taker_fee(Decimal("0.28"), Decimal("8")) == Decimal("0.112896")
    assert taker_fee(Decimal("0.28"), Decimal("0")) == 0


def test_settle_result_win_lose():
    win, pnl_w = settle_result("Up", Decimal("7"), Decimal("1.96"),
                               Decimal("0.098"), "Up")
    lose, pnl_l = settle_result("Up", Decimal("7"), Decimal("1.96"),
                                Decimal("0.098"), "Down")
    assert (win, pnl_w) == ("win", Decimal("7") - Decimal("1.96") - Decimal("0.098"))
    assert (lose, pnl_l) == ("lose", -(Decimal("1.96") + Decimal("0.098")))
    assert settle_result("Up", Decimal("7"), Decimal("1.96"), Decimal("0"), None) is None


# ---- settle_key 端到端（假 Gamma，不起网络）----

class _FakeGamma:
    def __init__(self, m: Market | None) -> None:
        self.m = m

    async def get_market_by_slug(self, slug: str, *, closed=None):
        return self.m


def test_settle_key_settles_open_position(store):
    from pm_arb.data.settlement import taker_fee as _fee

    price, shares = Decimal("0.28"), Decimal("7")
    store.open_position(symbol="btc", window_start=1000, token_id="tok",
                        condition_id="0xabc", side="Up", entry_price=price,
                        filled_size=shares, fee=_fee(price, shares))
    m = _mkt(True, ["1", "0"]).model_copy(update={"slug": "btc-updown-5m-1000"})
    r = asyncio.run(settle_key(store, _FakeGamma(m), "btc", 1000, dry=True))
    assert r["outcome"] == "win" and r["winner"] == "Up"
    assert r["redeemed"] is False  # proxy（signature_type=3）只记待赎
    cur = store.conn.execute("SELECT status, realized_pnl FROM positions")
    row = dict(zip([c[0] for c in cur.description], cur.fetchone(), strict=True))
    assert row["status"] == "settled"
    assert Decimal(row["realized_pnl"]) == shares - price * shares - _fee(price, shares)
    cur = store.conn.execute("SELECT settlement, settle_pnl FROM windows")
    row = dict(zip([c[0] for c in cur.description], cur.fetchone(), strict=True))
    assert row["settlement"] == "win" and row["settle_pnl"] is not None
