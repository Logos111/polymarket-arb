"""实时盘口演示 / 数据层联调入口。

流程：
1. Gamma 拉取活跃市场（默认按 24h 成交量排序，取流动性最好的前 N 个）；
2. 订阅这些市场 YES/NO token 的 WS 行情，本地维护 L2 订单簿；
3. 每秒打印一次各 token 的最优 bid/ask/价差；
4. ``--record`` 时同时把 tick 数据录制到 runtime/ticks/。

用法::

    uv run pm-watch                       # 看前 3 个市场，跑 30 秒
    uv run pm-watch --markets 5 --seconds 60 --record
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
from decimal import Decimal

from pm_arb.data.feed import MarketDataFeed
from pm_arb.data.gamma import GammaClient
from pm_arb.data.models import Market
from pm_arb.data.orderbook import LocalOrderBook
from pm_arb.data.recorder import TickRecorder
from pm_arb.infra.logging import get_logger, setup_logging

log = get_logger(__name__)


def _fmt(d: Decimal | None, width: int = 6) -> str:
    return f"{d:.3f}".rjust(width) if d is not None else "  n/a ".rjust(width)


async def _print_boards(
    labels: dict[str, str], books: dict[str, LocalOrderBook], stop: asyncio.Event
) -> None:
    """每秒清屏打印一次所有订阅 token 的盘口头部。"""
    while not stop.is_set():
        lines = ["\033[2J\033[H" + "=" * 78]
        lines.append(f"{'token':<34}{'bid':>7}{'ask':>7}{'spread':>8}{'mid':>8}  lvls(b/a)")
        lines.append("-" * 78)
        for asset_id, label in labels.items():
            ob = books.get(asset_id)
            if ob is None or not ob.ready:
                lines.append(f"{label:<34}{'... waiting for data':>30}")
                continue
            bb, ba = ob.best_bid, ob.best_ask
            lines.append(
                f"{label:<34}{_fmt(bb.price if bb else None)}{_fmt(ba.price if ba else None)}"
                f"{_fmt(ob.spread, 8)}{_fmt(ob.midpoint, 8)}  "
                f"{len(ob.snapshot.bids)}/{len(ob.snapshot.asks)}"
            )
        lines.append("=" * 78)
        lines.append("Ctrl+C 退出")
        print("\n".join(lines), flush=True)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=1.0)


async def _amain(markets_count: int, seconds: int, record: bool) -> int:
    async with GammaClient() as gamma:
        markets = await gamma.get_markets(limit=markets_count * 3)
        tradeable: list[Market] = [m for m in markets if m.is_tradeable][:markets_count]

    if not tradeable:
        log.error("watch_no_markets")
        return 1

    labels: dict[str, str] = {}
    token_ids: list[str] = []
    print(f"\n订阅 {len(tradeable)} 个市场、{len(tradeable) * 2} 个 token：")
    for m in tradeable:
        for tok in m.tokens[:2]:
            labels[tok.token_id] = f"{(m.question or m.slug)[:32]} | {tok.outcome}"
            token_ids.append(tok.token_id)
        print(f"  - {m.question[:72]}  neg_risk={m.neg_risk}")

    recorder = TickRecorder() if record else None
    feed = MarketDataFeed(token_ids, recorder=recorder)
    stop = asyncio.Event()
    printer = asyncio.create_task(_print_boards(labels, feed.books(), stop))

    async def _stopper() -> None:
        await asyncio.sleep(seconds)
        stop.set()

    stopper = asyncio.create_task(_stopper())
    feed_gen = feed.run()
    try:
        async for _ in feed_gen:
            if stop.is_set():
                break
    finally:
        stop.set()
        await feed_gen.aclose()  # 确定性关闭 WS/REST 连接
        await asyncio.gather(printer, stopper, return_exceptions=True)
        if recorder:
            recorder.flush()
            recorder.close()
            log.info("watch_recorded", lines=recorder.lines_written)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Polymarket 实时盘口监控（数据层联调）")
    parser.add_argument("--markets", type=int, default=3, help="订阅市场数量（默认 3）")
    parser.add_argument("--seconds", type=int, default=30, help="运行秒数（默认 30）")
    parser.add_argument("--record", action="store_true", help="同时录制 tick 到 runtime/ticks/")
    args = parser.parse_args()

    setup_logging(level="WARNING")  # 屏显模式下压制日志，避免打乱界面
    try:
        return asyncio.run(_amain(args.markets, args.seconds, args.record))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
