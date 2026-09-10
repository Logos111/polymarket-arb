"""BTC/ETH 等 5 分钟 Up/Down 市场盘口查看（只读，无需凭证）。

用法::

    uv run pm-crypto5m                       # 快照：btc/eth 当前窗口 Up/Down 盘口
    uv run pm-crypto5m --symbols btc eth --levels 5
    uv run pm-crypto5m --watch 60            # 订阅 WS，实时刷新 60 秒
    uv run pm-crypto5m --record              # 同时录制 tick 到 runtime/ticks/

输出每个币种当前 5 分钟窗口的 Up（上涨）/ Down（下跌）token 最优买卖价、
价差、中间价与前 N 档深度，并打印 Up+Down 中间价之和（理论上 ≈ 1）。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import sys
from datetime import UTC, datetime
from decimal import Decimal

from pm_arb.data.clob_rest import ClobRestClient
from pm_arb.data.crypto_5m import current_window_start, get_window_market, up_down_tokens
from pm_arb.data.feed import MarketDataFeed
from pm_arb.data.gamma import GammaClient
from pm_arb.data.orderbook import LocalOrderBook
from pm_arb.data.recorder import TickRecorder
from pm_arb.infra.logging import get_logger, setup_logging

log = get_logger(__name__)


def _fmt(d: Decimal | None, width: int = 7) -> str:
    return f"{d:.3f}".rjust(width) if d is not None else "  n/a ".rjust(width)


async def _collect(symbols: list[str]) -> list[dict]:
    """发现各币种当前窗口市场，返回 [{sym, market, up_id, down_id}]。"""
    out: list[dict] = []
    async with GammaClient() as gamma:
        for sym in symbols:
            m = await get_window_market(gamma, sym)
            if m is None:
                log.warning("crypto5m_no_market", symbol=sym)
                continue
            up, down = up_down_tokens(m)
            out.append({"sym": sym, "market": m, "up_id": up, "down_id": down})
    return out


def _print_book(label: str, ob: LocalOrderBook, levels: int) -> Decimal | None:
    bb, ba = ob.best_bid, ob.best_ask
    print(
        f"  {label:<14}{'bid':>7}{'ask':>7}{'spread':>8}{'mid':>7}   "
        f"{'深度(bid×ask档)':>16}"
    )
    depth = f"{len(ob.snapshot.bids)}×{len(ob.snapshot.asks)}".rjust(16)
    print(
        f"  {'':<14}{_fmt(bb.price if bb else None)}{_fmt(ba.price if ba else None)}"
        f"{_fmt(ob.spread, 8)}{_fmt(ob.midpoint, 7)}   {depth}"
    )
    bids, asks = ob.top_levels(levels)
    for i in range(levels):
        b = bids[i] if i < len(bids) else None
        a = asks[i] if i < len(asks) else None
        bp = f"{b.price:.3f}×{b.size:.0f}" if b else ""
        ap = f"{a.price:.3f}×{a.size:.0f}" if a else ""
        print(f"      {'':<12}{bp:>16}   {ap:<16}")
    return ob.midpoint


async def _snapshot(symbols: list[str], levels: int) -> int:
    items = await _collect(symbols)
    if not items:
        print("未发现任何 5min 加密市场（可能处于窗口切换间隙，稍后重试）。")
        return 1

    async with ClobRestClient() as rest:
        for it in items:
            m = it["market"]
            win = datetime.fromtimestamp(current_window_start(), UTC).strftime("%H:%M")
            print("\n" + "=" * 72)
            print(f"{it['sym'].upper()} 5min Up/Down   窗口起点 {win} UTC   slug={m.slug}")
            print("-" * 72)
            mids: list[Decimal] = []
            for label, tid in (("Up 上涨", it["up_id"]), ("Down 下跌", it["down_id"])):
                ob = LocalOrderBook(tid)
                ev = await rest.get_book(tid)
                if ev is not None:
                    ob.apply_snapshot(ev)
                mid = _print_book(label, ob, levels)
                if mid is not None:
                    mids.append(mid)
            if len(mids) == 2:
                total = mids[0] + mids[1]
                arb = total - Decimal(1)
                flag = "  [!] 偏离 $1，存在套利空间" if abs(arb) >= Decimal("0.01") else ""
                print(f"  Up mid + Down mid = {total:.4f}（理论 ≈ 1.000，偏离 {arb:+.4f}）{flag}")
    print("\n" + "=" * 72)
    return 0


async def _watch(symbols: list[str], seconds: int, levels: int, record: bool) -> int:
    items = await _collect(symbols)
    if not items:
        print("未发现任何 5min 加密市场。")
        return 1

    token_ids: list[str] = []
    labels: dict[str, str] = {}
    for it in items:
        token_ids += [it["up_id"], it["down_id"]]
        labels[it["up_id"]] = f"{it['sym'].upper()} Up"
        labels[it["down_id"]] = f"{it['sym'].upper()} Down"

    recorder = TickRecorder() if record else None
    feed = MarketDataFeed(token_ids, recorder=recorder)
    stop = asyncio.Event()

    async def _render() -> None:
        while not stop.is_set():
            lines = ["\033[2J\033[H" + "=" * 70]
            lines.append(f"{'token':<16}{'bid':>7}{'ask':>7}{'spread':>8}{'mid':>7}")
            lines.append("-" * 70)
            for tid, label in labels.items():
                ob = feed.books().get(tid)
                if ob is None or not ob.ready:
                    lines.append(f"{label:<16}{'... 等待数据':>30}")
                    continue
                bb, ba = ob.best_bid, ob.best_ask
                lines.append(
                    f"{label:<16}{_fmt(bb.price if bb else None)}{_fmt(ba.price if ba else None)}"
                    f"{_fmt(ob.spread, 8)}{_fmt(ob.midpoint, 7)}"
                )
            lines.append("=" * 70)
            lines.append(f"Ctrl+C 退出  |  录制 {recorder.lines_written if recorder else 0} ticks")
            print("\n".join(lines), flush=True)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=1.0)

    renderer = asyncio.create_task(_render())

    async def _stopper() -> None:
        await asyncio.sleep(seconds)
        stop.set()

    stopper = asyncio.create_task(_stopper())
    gen = feed.run()
    try:
        async for _ in gen:
            if stop.is_set():
                break
    finally:
        stop.set()
        await gen.aclose()
        await asyncio.gather(renderer, stopper, return_exceptions=True)
        if recorder:
            recorder.flush()
            recorder.close()
            log.info("crypto5m_recorded", lines=recorder.lines_written)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Polymarket 5min 加密 Up/Down 盘口")
    parser.add_argument(
        "--symbols", nargs="+", default=["btc", "eth"],
        help="币种，默认 btc eth（可选 btc eth sol xrp doge bnb hype zec）",
    )
    parser.add_argument("--levels", type=int, default=3, help="显示盘口档位（默认 3）")
    parser.add_argument("--watch", type=int, default=0, metavar="SECONDS", help="实时订阅秒数")
    parser.add_argument("--record", action="store_true", help="同时录制 tick 到 runtime/ticks/")
    args = parser.parse_args()

    # Windows 控制台默认 GBK，统一 UTF-8 输出，避免中文/emoji 编码报错
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    setup_logging(level="WARNING")  # 压制 httpx 请求日志，保持盘口界面干净
    try:
        if args.watch > 0:
            return asyncio.run(_watch(args.symbols, args.watch, args.levels, args.record))
        return asyncio.run(_snapshot(args.symbols, args.levels))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
