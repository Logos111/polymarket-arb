"""链上合约地址加载测试（不触网）。"""

from pm_arb.execution.chain import _load_addresses


def test_polygon_addresses_loaded():
    addrs = _load_addresses(137, "0xC5d563A36AE78145C45a50134d48A1215220f80a")
    # 与 py-clob-client-v2 内置 Polygon 配置一致
    assert addrs.exchange.lower() == "0x4bfb41d5b3570defd03c39a9a4d8de6bd8b8982e"
    assert addrs.collateral.lower() == "0xc011a7e12a19f7b1f670d46f03b03f3342e82dfb"
    assert addrs.conditional_tokens.lower() == "0x4d97dcd97ec945f40cf65f87097ace5ea0476045"
    assert addrs.neg_risk_adapter.startswith("0x")
