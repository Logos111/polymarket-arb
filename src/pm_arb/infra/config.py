"""集中式配置：从环境变量 / .env 文件加载，所有密钥以 SecretStr 持有。

用法::

    from pm_arb.infra.config import get_settings

    settings = get_settings()
    print(settings.clob_api_url)

约定：所有环境变量统一使用 ``PM_`` 前缀（如 ``PM_PRIVATE_KEY``）。
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """运行时配置。字段名大写即为对应环境变量（加 PM_ 前缀）。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="PM_",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---- 行情 / 交易 API 端点（公开默认值，一般无需修改）----
    gamma_api_url: str = "https://gamma-api.polymarket.com"
    clob_api_url: str = "https://clob.polymarket.com"
    clob_ws_market_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    clob_ws_user_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/user"

    # ---- 钱包 / 链上 ----
    # Polygon 交易钱包私钥（小额专用钱包）；日志/打印中永不明文输出
    private_key: str = Field(default="", repr=False)
    chain_id: int = 137
    # Chainlink 喂价读取 RPC（5min 市场结算价同源；polygon-rpc.com 已 401，默认公共节点）
    price_feed_rpc_url: str = "https://polygon-bor-rpc.publicnode.com"
    # 0=EOA 直连；1=Polymarket Proxy；2=Gnosis Safe
    signature_type: int = 0
    # Proxy/Safe 模式下的资金地址
    funder_address: str = ""
    polygon_rpc_url: str = Field(default="https://polygon-rpc.com", repr=False)
    # NegRiskAdapter（Polygon），多结果市场转换用；以官方文档为准
    neg_risk_adapter: str = "0xC5d563A36AE78145C45a50134d48A1215220f80a"

    # ---- CLOB L2 API 凭证（留空则后续用私钥派生）----
    clob_api_key: str = Field(default="", repr=False)
    clob_api_secret: str = Field(default="", repr=False)
    clob_api_passphrase: str = Field(default="", repr=False)

    # ---- 运行参数 ----
    log_level: str = "INFO"
    log_json: bool = False
    http_timeout: float = 10.0
    # 本地代理（网络受限时使用，如 http://127.0.0.1:7890）；留空则直连
    proxy_url: str = Field(default="", repr=False)

    # ---- 便捷判断 ----
    @property
    def has_private_key(self) -> bool:
        return bool(self.private_key and self.private_key.strip())

    @property
    def has_clob_creds(self) -> bool:
        return bool(
            self.clob_api_key.strip()
            and self.clob_api_secret.strip()
            and self.clob_api_passphrase.strip()
        )

    @property
    def trading_enabled(self) -> bool:
        """交易（下单/链上操作）所需的最小配置是否齐备。"""
        return self.has_private_key and bool(self.polygon_rpc_url.strip())


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """返回进程级单例配置（测试中可调用 ``get_settings.cache_clear()`` 重置）。"""
    return Settings()
