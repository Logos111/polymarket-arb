"""数据模型与 WS 消息解析测试。"""

import json
from decimal import Decimal

from pm_arb.data.models import Market
from pm_arb.data.ws import parse_message


def test_market_parses_gamma_json_fields():
    """Gamma 的 clobTokenIds/outcomes 是 JSON 编码字符串，需自动解析。"""
    raw = {
        "id": "123",
        "question": "Will X happen?",
        "conditionId": "0xabc",
        "negRisk": False,
        "active": True,
        "closed": False,
        "outcomes": json.dumps(["Yes", "No"]),
        "clobTokenIds": json.dumps(["111", "222"]),
        "liquidityNum": "50000.5",
    }
    m = Market.model_validate(raw)
    assert m.outcomes == ["Yes", "No"]
    assert m.clob_token_ids == ["111", "222"]
    assert m.liquidity == Decimal("50000.5")
    tokens = m.tokens
    assert tokens[0].token_id == "111"
    assert tokens[0].outcome == "Yes"
    assert m.is_tradeable is True


def test_market_closed_not_tradeable():
    m = Market.model_validate(
        {
            "id": "1",
            "active": True,
            "closed": True,
            "clobTokenIds": json.dumps(["a", "b"]),
        }
    )
    assert m.is_tradeable is False


def test_parse_book_snapshot_message():
    raw = json.dumps(
        {
            "event_type": "book",
            "asset_id": "tok1",
            "market": "0xmkt",
            "bids": [{"price": "0.60", "size": "100"}],
            "asks": [{"price": "0.62", "size": "150"}],
            "timestamp": "1700000000",
            "hash": "h1",
        }
    )
    events = parse_message(raw)
    assert len(events) == 1
    ev = events[0]
    assert ev.__class__.__name__ == "WsBookEvent"
    assert ev.bids[0].price == Decimal("0.60")
    assert ev.asks[0].size == Decimal("150")


def test_parse_price_change_batch():
    """price_change 一条消息可含多个 changes，且服务端可能下发 list。"""
    raw = json.dumps(
        [
            {
                "event_type": "price_change",
                "asset_id": "tok1",
                "changes": [
                    {"price": "0.61", "size": "50", "side": "BUY"},
                    {"price": "0.62", "size": "0", "side": "SELL"},
                ],
                "timestamp": "1700000001",
                "hash": "h2",
            }
        ]
    )
    events = parse_message(raw)
    assert len(events) == 2
    buy, sell = events
    assert buy.side == "BUY"
    assert buy.size == Decimal("50")
    assert sell.is_removal is True


def test_parse_last_trade():
    raw = json.dumps(
        {
            "event_type": "last_trade_price",
            "asset_id": "tok1",
            "price": "0.63",
            "size": "200",
            "side": "SELL",
            "timestamp": "1700000002",
        }
    )
    events = parse_message(raw)
    assert len(events) == 1
    assert events[0].__class__.__name__ == "WsLastTrade"
    assert events[0].price == Decimal("0.63")


def test_parse_unknown_event_skipped():
    raw = json.dumps({"event_type": "tick_size_change", "asset_id": "tok1"})
    assert parse_message(raw) == []
