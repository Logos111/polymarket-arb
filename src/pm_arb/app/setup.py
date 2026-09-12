"""钱包/授权设置向导。

默认只检查、不发任何交易::

    uv run pm-setup                  # 检查地址、POL/USDC 余额、各项授权
    uv run pm-setup --approve        # 交互式确认后发送授权交易

前置：.env 已配置 PM_PRIVATE_KEY（Bitget/MetaMask 等 EOA 钱包导出的私钥，
signature_type=0）、PM_POLYGON_RPC_URL、需要时 PM_PROXY_URL。
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from pm_arb.infra.config import get_settings
from pm_arb.infra.logging import get_logger, setup_logging

log = get_logger(__name__)


def _ok(flag: bool) -> str:
    return "✅ 已授权" if flag else "❌ 未授权"


def _bal(d, unit: str) -> str:
    return f"{d} {unit}"


async def check() -> dict:
    """只读检查，返回状态字典。"""
    from pm_arb.execution.chain import ChainClient
    from pm_arb.execution.clob_trader import ClobTrader

    s = get_settings()
    if not s.trading_enabled:
        print("❌ 未配置 PM_PRIVATE_KEY。请先 cp .env.example .env 并填入私钥。")
        sys.exit(2)

    print("初始化链上客户端（RPC）…")
    chain = ChainClient(s)
    addr = chain.address
    print(f"\n钱包地址      : {addr}")
    print(f"签名类型      : {s.signature_type} (0=EOA 直连)")
    print(f"RPC           : {s.polygon_rpc_url[:50]}")
    print(f"合约          : Exchange {chain.addrs.exchange}")
    print(f"                CTF      {chain.addrs.conditional_tokens}")
    print(f"                USDC.e   {chain.addrs.collateral}")

    print("\n读取链上状态…")
    pol = await chain.pol_balance()
    usdc = await chain.usdc_balance()
    allow_ex = await chain.usdc_allowance(chain.addrs.exchange)
    allow_ctf = await chain.usdc_allowance(chain.addrs.conditional_tokens)
    ctf_op = await chain.ctf_is_approved_for_all(chain.addrs.exchange)

    status = {
        "pol": pol,
        "usdc": usdc,
        "allow_exchange": allow_ex,
        "allow_ctf": allow_ctf,
        "ctf_operator": ctf_op,
        "chain": chain,
    }

    print("\n--- 余额 ---")
    print(f"  POL (gas)    : {_bal(pol, 'POL')}  {'⚠️ 过少，授权需要 gas' if pol < 0.05 else ''}")
    print(f"  USDC.e       : {_bal(usdc, 'USDC')}")
    print("\n--- 授权状态 ---")
    print(f"  USDC→Exchange: {_ok(allow_ex > 0)}   (额度 {allow_ex} )")
    print(f"  USDC→CTF     : {_ok(allow_ctf > 0)}   (split 需要，额度 {allow_ctf})")
    print(f"  CTF→Exchange : {_ok(ctf_op)}   (卖出头寸需要)")

    print("\n校验 CLOB API 凭证（L1 签名派生 L2 key，无 gas）…")
    try:
        trader = ClobTrader.authenticated(s)
        ping = await trader.ping()
        print(f"  CLOB 连接    : {'✅ 正常' if ping else '❌ 失败'}")
        key_set = bool(s.clob_api_key.get_secret_value().strip())
        key_disp = "已配置（****）" if key_set else "（本次会话派生，未写入 .env）"
        print(f"  L2 API key   : {key_disp}")
        status["trader"] = trader
    except Exception as e:
        print(f"  CLOB 认证    : ❌ {type(e).__name__}: {str(e)[:120]}")

    return status


async def approve(status: dict) -> None:
    """发送授权交易。"""
    chain = status["chain"]
    steps = [
        ("USDC → Exchange（下单结算扣款）", chain.ensure_exchange_approval),
        ("USDC → CTF（split 铸造）", chain.ensure_ctf_approval),
        ("CTF → Exchange setApprovalForAll（卖出头寸）", chain.ensure_ctf_operator_approval),
    ]
    for name, coro_fn in steps:
        print(f"\n→ {name} …")
        tx = await coro_fn()
        print(f"  {'已授权，跳过' if tx is None else f'交易已提交: {tx}'}")

    trader = status.get("trader")
    if trader is not None:
        print("\n→ 刷新 CLOB 余额/授权缓存 …")
        try:
            await trader.update_balance_allowance("COLLATERAL")
            print("  ✅ 已刷新")
        except Exception as e:
            print(f"  ⚠️ {str(e)[:120]}（下次下单时 SDK 会自动重试）")

    print("\n🎉 授权完成。现在可以用 ClobTrader.place_limit 下单了。")
    print("   提示：L2 API key 本次只在内存派生；如需持久化，")
    print("   可把返回的 key/secret/passphrase 写入 .env（PM_CLOB_API_*）。")


def main() -> int:
    parser = argparse.ArgumentParser(description="Polymarket 钱包与授权设置向导")
    parser.add_argument("--approve", action="store_true", help="检查后交互式发送授权交易")
    args = parser.parse_args()

    setup_logging(level="WARNING")

    # 确认提示放在同步上下文，避免 async 中阻塞调用 input()
    do_approve = False
    if args.approve:
        print("\n即将发送链上授权交易（消耗少量 POL gas）。")
        if input("确认执行授权？输入 yes 继续：").strip().lower() != "yes":
            print("已取消。")
            return 1
        do_approve = True

    async def _run() -> int:
        status = await check()
        if not do_approve:
            print("\n（仅检查模式。确认无误后加 --approve 发送授权交易。）")
            return 0
        await approve(status)
        return 0

    try:
        return asyncio.run(_run())
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
