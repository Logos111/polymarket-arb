"""实盘交易客户端：封装 py-clob-client-v2 的认证与下单。

- py-clob-client-v2 是同步库（requests），所有调用通过 ``asyncio.to_thread``
  包装，避免阻塞事件循环；
- 认证两级：L1（钱包私钥 EIP-712）派生/注册 API key → L2（key/secret/
  passphrase 签名请求头）；
- 只读模式（无私钥）可访问公开行情接口。

⚠️ 任何真实下单前，策略层必须经过风控层审批。
"""

from __future__ import annotations

import asyncio
import os
from decimal import Decimal

from pm_arb.execution.orders import Order, OrderType, Side
from pm_arb.infra.config import Settings, get_settings
from pm_arb.infra.logging import get_logger

log = get_logger(__name__)


def _apply_proxy_env(settings: Settings) -> None:
    """py-clob-client-v2（requests）与 web3 均信任环境变量代理。"""
    if settings.proxy_url:
        os.environ.setdefault("HTTPS_PROXY", settings.proxy_url)
        os.environ.setdefault("HTTP_PROXY", settings.proxy_url)
        os.environ.setdefault("https_proxy", settings.proxy_url)
        os.environ.setdefault("http_proxy", settings.proxy_url)


class ClobTrader:
    """实盘 CLOB 交易客户端。优先用 ``ClobTrader.authenticated()`` 构造。"""

    def __init__(self, settings: Settings | None = None):
        from py_clob_client_v2.client import ClobClient

        self._settings = settings or get_settings()
        _apply_proxy_env(self._settings)
        s = self._settings

        kwargs: dict = {"chain_id": s.chain_id, "signature_type": s.signature_type}
        if s.signature_type != 0 and s.funder_address:
            kwargs["funder"] = s.funder_address
        if s.has_clob_creds:
            from py_clob_client_v2.clob_types import ApiCreds

            kwargs["creds"] = ApiCreds(
                api_key=s.clob_api_key,
                api_secret=s.clob_api_secret,
                api_passphrase=s.clob_api_passphrase,
            )
        if s.has_private_key:
            kwargs["key"] = s.private_key

        self._client = ClobClient(s.clob_api_url, **kwargs)
        self._address: str | None = None

        if s.has_private_key and not s.has_clob_creds:
            self._derive_l2_creds()

    # ---- 构造 ----

    @classmethod
    def authenticated(cls, settings: Settings | None = None) -> ClobTrader:
        """需要私钥；自动完成 L1→L2 凭证派生。"""
        s = settings or get_settings()
        if not s.trading_enabled:
            raise RuntimeError("交易未启用：请在 .env 配置 PM_PRIVATE_KEY")
        return cls(s)

    def _derive_l2_creds(self) -> None:
        """用 L1 私钥派生（或注册）L2 API 凭证并装载。"""
        from py_clob_client_v2.clob_types import ApiCreds

        log.info("clob_deriving_api_creds", signature_type=self._settings.signature_type)
        # 同步 SDK，直接调用（构造阶段尚未进入事件循环）
        derived = self._client.create_or_derive_api_key()
        creds = ApiCreds(
            api_key=derived.api_key,
            api_secret=derived.api_secret,
            api_passphrase=derived.api_passphrase,
        )
        self._client.set_api_creds(creds)
        log.info("clob_api_creds_ready", api_key=derived.api_key[:8] + "…")

    @property
    def address(self) -> str:
        if self._address is None:
            self._address = self._client.get_address()
        return self._address

    # ---- 下单 / 撤单 ----

    async def place_limit(
        self,
        token_id: str,
        side: Side,
        price: Decimal,
        size: Decimal,
        *,
        order_type: OrderType = OrderType.GTC,
        post_only: bool = False,
        neg_risk: bool = False,
    ) -> Order:
        """下限价单。返回带状态的 Order（失败时状态为 REJECTED/FAILED）。"""
        from py_clob_client_v2.clob_types import OrderArgs, PartialCreateOrderOptions
        from py_clob_client_v2.clob_types import OrderType as PyOrderType

        order = Order(
            token_id=token_id,
            side=side,
            order_type=order_type,
            price=Decimal(price),
            size=Decimal(size),
            post_only=post_only,
        )

        def _do() -> dict:
            args = OrderArgs(
                token_id=token_id,
                price=float(price),
                size=float(size),
                side=side.value,
            )
            options = PartialCreateOrderOptions(neg_risk=neg_risk)
            py_type = getattr(PyOrderType, order_type.value)
            return self._client.create_and_post_order(args, options, py_type, post_only)

        try:
            resp = await asyncio.to_thread(_do)
        except Exception as e:
            order.mark_failed(f"{type(e).__name__}: {e}")
            log.warning("place_order_failed", client_id=order.client_id, error=str(e)[:150])
            return order

        if resp.get("success"):
            order.mark_submitted(str(resp.get("orderID") or resp.get("orderHashes", [""])[0]))
            log.info(
                "order_submitted",
                client_id=order.client_id,
                exchange_id=order.exchange_id,
                side=side.value,
                price=str(price),
                size=str(size),
                type=order_type.value,
            )
        else:
            order.mark_rejected(str(resp.get("errorMsg") or resp.get("error") or resp))
            log.warning("order_rejected", client_id=order.client_id, resp=resp)
        return order

    async def place_market(
        self,
        token_id: str,
        side: Side,
        amount: Decimal,
        *,
        order_type: OrderType = OrderType.FAK,
        neg_risk: bool = False,
        ref_price: Decimal | None = None,
    ) -> Order:
        """下市价单（价格由 SDK 按当前盘口自动计算）。

        - BUY：amount 为美元金额；SELL：amount 为份额数；
        - 默认 FAK：能成交多少算多少，剩余取消——比 FOK 更贴近“市价成交”
          语义，不会被往返延迟内的价格上移整单杀掉；
        - ref_price：参考价（调用方从本地盘口取，如当前 ask/bid），用于
          估算目标份数赋给 order.size，让 FILLED/PARTIAL 状态区分有
          意义（问题 4a：size=0 时任何成交都会被判 FILLED）。SELL 的
          amount 本身就是份数，无需估算。缺失时 size=0，行为同旧版：
          成交即 FILLED（阶段 2 落库一律用 filled_size 与目标名义对比，
          不依赖 status，双保险）。
        """
        from py_clob_client_v2.clob_types import MarketOrderArgs, PartialCreateOrderOptions
        from py_clob_client_v2.clob_types import OrderType as PyOrderType

        # 目标份数估算：BUY 用参考价折算美元；SELL 直接取份数
        if side is Side.SELL:
            est_size = Decimal(amount)
        elif ref_price and ref_price > 0:
            est_size = Decimal(amount) / ref_price
        else:
            est_size = Decimal(0)

        order = Order(
            token_id=token_id,
            side=side,
            order_type=order_type,
            price=ref_price or Decimal(0),
            size=est_size,
        )

        def _do() -> dict:
            args = MarketOrderArgs(
                token_id=token_id,
                amount=float(amount),
                side=side.value,
            )
            options = PartialCreateOrderOptions(neg_risk=neg_risk)
            py_type = getattr(PyOrderType, order_type.value)
            return self._client.create_and_post_market_order(args, options, py_type)

        try:
            resp = await asyncio.to_thread(_do)
        except Exception as e:
            order.mark_failed(f"{type(e).__name__}: {e}")
            log.warning("place_order_failed", client_id=order.client_id, error=str(e)[:150])
            return order

        if resp.get("success"):
            order.mark_submitted(str(resp.get("orderID") or ""))
            log.info(
                "order_submitted",
                client_id=order.client_id,
                exchange_id=order.exchange_id,
                side=side.value,
                amount=str(amount),
                type=f"MARKET/{order_type.value}",
            )
            # 回执 status=matched 表示已（部分）成交：换算实际份数与均价。
            # 实测（2026-09-11 首笔市价成交）：making/taking 直接是十进制字符串
            # （如 BUY：making="2.000000" USDC、taking="7.142858" 份），
            # 不是 6 位定点整数——曾多除 1e6 导致仓位显示缩小百万倍。
            # 均价 = 花费/份数（比值天然消除单位）。
            if str(resp.get("status") or "").lower() == "matched":
                try:
                    making = Decimal(str(resp.get("makingAmount") or 0))
                    taking = Decimal(str(resp.get("takingAmount") or 0))
                    shares, notional = (
                        (taking, making) if side is Side.BUY else (making, taking)
                    )
                    if shares > 0 and notional > 0:
                        order.apply_fill(shares, notional / shares)
                except (ArithmeticError, ValueError) as e:
                    log.warning("market_fill_parse_failed",
                                client_id=order.client_id, error=str(e)[:100])
        else:
            order.mark_rejected(str(resp.get("errorMsg") or resp.get("error") or resp))
            log.warning("order_rejected", client_id=order.client_id, resp=resp)
        return order

    async def cancel(self, exchange_id: str) -> bool:
        from py_clob_client_v2.clob_types import OrderPayload

        try:
            resp = await asyncio.to_thread(
                self._client.cancel_order, OrderPayload(orderID=exchange_id)
            )
            log.info("order_cancel_sent", exchange_id=exchange_id, resp=resp)
            return True
        except Exception as e:
            log.warning("cancel_failed", exchange_id=exchange_id, error=str(e)[:150])
            return False

    async def cancel_all(self) -> int:
        """撤销所有挂单。返回（尝试）撤销数量。"""
        try:
            resp = await asyncio.to_thread(self._client.cancel_all)
            n = len(resp) if isinstance(resp, list) else 0
            log.info("cancel_all_done", canceled=n)
            return n
        except Exception as e:
            log.warning("cancel_all_failed", error=str(e)[:150])
            return 0

    # ---- 查询 ----

    async def get_order(self, exchange_id: str) -> dict:
        return await asyncio.to_thread(self._client.get_order, exchange_id)

    async def get_open_orders(self, market: str | None = None) -> list[dict]:
        from py_clob_client_v2.clob_types import OpenOrderParams

        params = OpenOrderParams(market=market) if market else None
        resp = await asyncio.to_thread(self._client.get_open_orders, params)
        return resp if isinstance(resp, list) else resp.get("data", [])

    async def get_trades(self, market: str | None = None) -> list[dict]:
        from py_clob_client_v2.clob_types import TradeParams

        params = TradeParams(market=market) if market else None
        resp = await asyncio.to_thread(self._client.get_trades, params)
        return resp if isinstance(resp, list) else resp.get("data", [])

    async def get_balance_allowance(self, asset_type: str = "COLLATERAL") -> dict:
        """查询 CTF 交易所视角的余额/授权（资产类型 COLLATERAL / CONDITIONAL）。"""
        from py_clob_client_v2.clob_types import AssetType, BalanceAllowanceParams

        at = AssetType.COLLATERAL if asset_type == "COLLATERAL" else AssetType.CONDITIONAL
        params = BalanceAllowanceParams(asset_type=at, signature_type=self._settings.signature_type)
        return await asyncio.to_thread(self._client.get_balance_allowance, params)

    async def update_balance_allowance(self, asset_type: str = "COLLATERAL") -> dict:
        """触发交易所刷新余额/授权缓存（下单前若提示需要 approve 时调用）。"""
        from py_clob_client_v2.clob_types import AssetType, BalanceAllowanceParams

        at = AssetType.COLLATERAL if asset_type == "COLLATERAL" else AssetType.CONDITIONAL
        params = BalanceAllowanceParams(asset_type=at, signature_type=self._settings.signature_type)
        return await asyncio.to_thread(self._client.update_balance_allowance, params)

    async def ping(self) -> bool:
        """只读连通性检查。"""
        try:
            await asyncio.to_thread(self._client.get_ok)
            return True
        except Exception:
            return False

    @property
    def status_summary(self) -> dict:
        s = self._settings
        return {
            "address": self.address if s.has_private_key else "(read-only)",
            "authenticated": s.has_private_key,
            "signature_type": s.signature_type,
            "funder": s.funder_address or None,
        }
