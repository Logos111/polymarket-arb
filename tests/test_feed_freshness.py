"""books_from_feed 新鲜度门控回归测试。

复现并验证根因修复：``LocalOrderBook.ready`` 是单向锁（收到首个快照即永久
True），仅凭 ready 判定会让 trade5m 的 REST 回退分支成为死代码——WS 静默
（连接未断但服务端不再推 book/price_change）时，本地簿冻结在旧值，策略却
毫不知情地继续用它下单。叠加 ``is_fresh`` 阈值后，陈旧盘口会被识别，
``books_from_feed`` 返回 None，调用方据此回退 REST 快照。
"""

import time
from decimal import Decimal

from pm_arb.app.trade5m import WS_FRESH_SEC, books_from_feed
from pm_arb.data.feed import MarketDataFeed
from pm_arb.data.models import BookLevel, WsBookEvent

UP, DOWN = "up_tok", "down_tok"


def _book_event(asset_id: str, bid: str, ask: str) -> WsBookEvent:
    return WsBookEvent(
        asset_id=asset_id,
        bids=[BookLevel(price=Decimal(bid), size=Decimal("10"))],
        asks=[BookLevel(price=Decimal(ask), size=Decimal("10"))],
    )


def _feed_with_fresh_books() -> MarketDataFeed:
    """构造一个两边本地簿都已就绪且新鲜的 feed（无需网络）。"""
    feed = MarketDataFeed([UP, DOWN])
    feed.book(UP).apply_snapshot(_book_event(UP, "0.40", "0.42"))
    feed.book(DOWN).apply_snapshot(_book_event(DOWN, "0.58", "0.60"))
    return feed


def _make_stale(feed: MarketDataFeed, asset_id: str) -> None:
    """模拟 WS 静默：把该簿最后更新时间倒退到远超阈值（ready 仍为 True）。"""
    feed.book(asset_id)._last_update = time.monotonic() - (WS_FRESH_SEC + 1000)


def test_returns_books_when_both_fresh():
    feed = _feed_with_fresh_books()
    b = books_from_feed(feed, UP, DOWN, WS_FRESH_SEC)
    assert b is not None
    assert b["Up"]["best_bid"] == Decimal("0.40")
    assert b["Up"]["best_ask"] == Decimal("0.42")
    assert b["Down"]["best_bid"] == Decimal("0.58")
    assert b["Down"]["best_ask"] == Decimal("0.60")


def test_returns_none_when_stale_even_though_ready():
    """核心回归：WS 静默使簿陈旧，ready 仍为 True，但必须回退（返回 None）。"""
    feed = _feed_with_fresh_books()
    _make_stale(feed, UP)
    _make_stale(feed, DOWN)
    # 单向锁依然"就绪"，若不叠加新鲜度判定就会误用冻结盘口
    assert feed.book(UP).ready is True
    assert feed.book(DOWN).ready is True
    assert books_from_feed(feed, UP, DOWN, WS_FRESH_SEC) is None


def test_returns_none_when_only_one_side_stale():
    """任一边陈旧即回退，避免用半新半旧的盘口做方向判断。"""
    feed = _feed_with_fresh_books()
    _make_stale(feed, DOWN)
    assert books_from_feed(feed, UP, DOWN, WS_FRESH_SEC) is None


def test_returns_none_when_never_ready():
    """从未收到快照：两个簿都未就绪 → 回退 REST。"""
    feed = MarketDataFeed([UP, DOWN])
    assert books_from_feed(feed, UP, DOWN, WS_FRESH_SEC) is None


def test_recovers_to_fresh_after_new_snapshot():
    """陈旧后收到新快照（重连/REST 刷新）应立即恢复可用，无需重启。"""
    feed = _feed_with_fresh_books()
    _make_stale(feed, UP)
    assert books_from_feed(feed, UP, DOWN, WS_FRESH_SEC) is None
    # 模拟重连后重新 apply_snapshot
    feed.book(UP).apply_snapshot(_book_event(UP, "0.41", "0.43"))
    b = books_from_feed(feed, UP, DOWN, WS_FRESH_SEC)
    assert b is not None
    assert b["Up"]["best_ask"] == Decimal("0.43")
