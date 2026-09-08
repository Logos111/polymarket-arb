"""5 分钟加密市场发现逻辑测试（纯逻辑，不触网）。"""

import json

import pytest

from pm_arb.data import crypto_5m
from pm_arb.data.models import Market


def test_window_start_aligned_to_300():
    # 任意时刻向下取整到 5 分钟（1788835134 // 300 * 300）
    assert crypto_5m.current_window_start(1788835134) == 1788834900
    # 整点边界本身
    assert crypto_5m.current_window_start(1788835200) == 1788835200


def test_window_slug_format():
    assert crypto_5m.window_slug("BTC", 1788835200) == "btc-updown-5m-1788835200"
    assert crypto_5m.window_slug("eth", 123) == "eth-updown-5m-123"


def test_up_down_tokens():
    m = Market.model_validate(
        {
            "id": "1",
            "active": True,
            "closed": False,
            "outcomes": json.dumps(["Up", "Down"]),
            "clobTokenIds": json.dumps(["up-token-id", "down-token-id"]),
        }
    )
    up, down = crypto_5m.up_down_tokens(m)
    assert up == "up-token-id"
    assert down == "down-token-id"


def test_unsupported_symbol_raises():
    with pytest.raises(ValueError, match="不支持的币种"):
        crypto_5m.window_slug("dogebtc", 1)
