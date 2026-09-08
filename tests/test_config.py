"""配置加载测试。"""

from pm_arb.infra.config import Settings


def test_default_endpoints():
    s = Settings(_env_file=None)
    assert "polymarket.com" in s.gamma_api_url
    assert "polymarket.com" in s.clob_api_url
    assert s.clob_ws_market_url.startswith("wss://")
    assert s.chain_id == 137
    assert s.signature_type == 0


def test_env_override(monkeypatch):
    monkeypatch.setenv("PM_CHAIN_ID", "80001")
    monkeypatch.setenv("PM_SIGNATURE_TYPE", "2")
    monkeypatch.setenv("PM_PRIVATE_KEY", "0xabc123")
    s = Settings(_env_file=None)
    assert s.chain_id == 80001
    assert s.signature_type == 2
    assert s.has_private_key is True
    assert s.trading_enabled is True


def test_trading_requires_private_key():
    s = Settings(_env_file=None, private_key="", polygon_rpc_url="https://x")
    assert s.has_private_key is False
    assert s.trading_enabled is False


def test_clob_creds_check(monkeypatch):
    monkeypatch.setenv("PM_CLOB_API_KEY", "k")
    monkeypatch.setenv("PM_CLOB_API_SECRET", "s")
    # passphrase 缺失时不算齐备
    s = Settings(_env_file=None)
    assert s.has_clob_creds is False

    monkeypatch.setenv("PM_CLOB_API_PASSPHRASE", "p")
    s = Settings(_env_file=None)
    assert s.has_clob_creds is True
