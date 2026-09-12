"""pm-redeem：已结算 5m 窗口待赎回仓位扫描与赎回（DEV_PLAN 阶段 0.4）。

策略持仓"未止盈则拿到结算"，赢方每份赎 $1、输方归 $0；不主动赎回的话
赢方价值一直挂在 CTF 合约里。本脚本：

1. 扫描窗口：``--window-start``（逗号分隔 unix ts）或 ``--windows N``
   （回溯最近 N 个已结算窗口），Gamma ``closed=True`` 查结算结果；
2. 链上核对：CTF ``balanceOf(owner, token_id)`` 逐 token 查持仓，
   owner 优先取 funder（proxy 模式下仓位在 funder 名下）；
3. 赎回：默认 dry-run 只出报告；``--execute`` 时对赢方仓位调
   ``ChainClient.redeem``（redeemPositions 对输方仓位是 no-op，可一并赎）。

限制：owner 为 funder（signature_type=3，EIP-1271 proxy）时链上赎回
须从 proxy 发起（Safe execTransaction，阶段 2「自动赎回」实现），
``--execute`` 会明确报错拒绝，避免从 signer 盲发无效交易。

用法::

    pm-redeem --symbol btc --windows 6
    pm-redeem --window-start 1789065300,1789066200
    pm-redeem --windows 6 --execute
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from decimal import Decimal

from pm_arb.data.crypto_5m import WINDOW_SECONDS, get_window_market, window_slug
from pm_arb.data.gamma import GammaClient
from pm_arb.execution.chain import ChainClient
from pm_arb.infra.logging import get_logger

log = get_logger(__name__)


@dataclass
class Position:
    """单窗口单 outcome 的链上持仓与结算状态。"""

    window_start: int
    condition_id: str
    outcome: str
    token_id: str
    balance: Decimal  # CTF 份数（1 份 = $1 面值）
    settle_price: Decimal  # 结算价：赢 "1" / 输 "0"（closed 市场可信）

    @property
    def payout(self) -> Decimal:
        """按结算价的应赎金额。"""
        return self.balance * self.settle_price

    @property
    def is_winning(self) -> bool:
        return self.settle_price == 1


async def scan_window(
    gamma: GammaClient, symbol: str, window_start: int, owner: str, ctf,
    decimals: int,
) -> list[Position]:
    """查单窗口结算结果 + owner 链上持仓（市场不存在返回空表）。"""
    m = await get_window_market(
        gamma, symbol, window_start, closed=True
    )
    if m is None:
        log.info("redeem_window_not_found", window_start=window_start)
        return []
    positions: list[Position] = []
    prices = dict(zip(m.outcomes, m.outcome_prices, strict=False))
    scale = Decimal(10 ** decimals)
    for out, tid in zip(m.outcomes, m.clob_token_ids, strict=False):
        if out not in prices:
            continue  # 未结算（closed 但 outcomePrices 缺失），跳过
        bal = await asyncio.to_thread(ctf.functions.balanceOf(owner, int(tid)).call)
        balance = Decimal(bal) / scale
        if balance <= 0:
            continue
        positions.append(
            Position(window_start, m.condition_id, out, tid, balance,
                     Decimal(prices[out]))
        )
    return positions


async def run(symbol: str, window_starts: list[int], execute: bool) -> int:
    from pm_arb.infra.config import get_settings

    s = get_settings()
    chain = ChainClient(s)
    # owner：proxy 模式下仓位在 funder 名下；EOA 直连即 signer 自身
    owner = (
        s.funder_address
        if s.funder_address
        else chain.address
    )
    print(f"owner={owner}  dry-run=off" if execute else f"owner={owner}  [DRY-RUN]")
    if s.funder_address and owner.lower() != chain.address.lower() and execute:
        print("!! owner 为 funder（proxy）：链上赎回须从 proxy 发起"
              "（Safe execTransaction，阶段 2 实现）。--execute 已拒绝。")
        return 2

    positions: list[Position] = []
    # CTF 份额精度与 collateral（USDC）一致，动态读取避免硬编码漂移
    decimals = int(await chain.usdc_decimals)
    async with GammaClient(s) as gamma:
        for ws in window_starts:
            positions.extend(
                await scan_window(gamma, symbol, ws, owner, chain.ctf, decimals))

    if not positions:
        print(f"{symbol} {window_starts}: 无待赎回持仓。")
        return 0

    total_payout = Decimal(0)
    print(f"\n{'窗口':<12}{'方向':<6}{'份数':>10}{'结算':>6}{'应赎$':>8}")
    print("-" * 44)
    for p in sorted(positions, key=lambda x: x.window_start):
        total_payout += p.payout
        print(f"{p.window_start:<12}{p.outcome:<6}{p.balance:>10.6f}"
              f"{int(p.settle_price):>6}{p.payout:>8.2f}")
    print("-" * 44)
    print(f"合计应赎 ${total_payout:.2f}（{sum(p.is_winning for p in positions)} 笔赢方）")

    if not execute:
        print("[DRY-RUN] 未发起链上交易。加 --execute 赎回。")
        return 0

    winners = [p for p in positions if p.is_winning]
    if not winners:
        print("无赢方仓位，无需赎回。")
        return 0
    # 同一 condition 一次 redeemPositions 覆盖两个 index set；逐窗口调用
    for p in winners:
        h = await chain.redeem(p.condition_id)
        log.info("redeem_sent", window_start=p.window_start, tx=h)
        print(f"已赎回 窗口{p.window_start} {p.outcome} {p.balance:.6f} 份  tx={h}")
    return 0


def _parse_window_starts(args: argparse.Namespace) -> list[int]:
    if args.window_start:
        return [int(x) for x in args.window_start.split(",") if x.strip()]
    import time

    # 最近已结算窗口（窗口结束后留缓冲再视为已结算）往前回溯
    latest = (int(time.time()) - WINDOW_SECONDS) // WINDOW_SECONDS * WINDOW_SECONDS
    return [latest - i * WINDOW_SECONDS for i in range(max(1, args.windows))]


def main() -> int:
    parser = argparse.ArgumentParser(description="5m 已结算窗口赎回扫描/执行")
    parser.add_argument("--symbol", default="btc", choices=["btc", "eth"])
    parser.add_argument("--window-start", default="",
                        help="逗号分隔的窗口起点 unix 时间戳（显式指定）")
    parser.add_argument("--windows", type=int, default=6,
                        help="回溯最近 N 个已结算窗口（默认 6，--window-start 优先）")
    parser.add_argument("--execute", action="store_true",
                        help="真实赎回（默认 dry-run 只出报告）")
    args = parser.parse_args()

    starts = _parse_window_starts(args)
    slug_preview = window_slug(args.symbol, starts[0])
    log.info("pm_redeem_start", symbol=args.symbol, windows=len(starts),
             first_slug=slug_preview, execute=args.execute)
    return asyncio.run(run(args.symbol, starts, args.execute))


if __name__ == "__main__":
    raise SystemExit(main())
