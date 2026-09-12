"""历史窗口结算批量回填（DEV_PLAN 阶段 2，pm-backfill）。

按 (symbol, window_start) 批量查 Gamma closed 市场回填 windows/positions 表：
- 无交易窗口也回填 market_winner（结算标签）——回测样本的关键放大器；
- 有 open 持仓 → 费后 PnL 结算 + 赢单自动赎回尝试（proxy 钱包记待赎）。

用法::

    pm-backfill --symbols btc,eth --windows 50          # 最近 50 个已结束窗口
    pm-backfill --symbols btc --since "2026-09-12 08:00"
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime
from pathlib import Path

from pm_arb.data.crypto_5m import WINDOW_SECONDS, current_window_start
from pm_arb.data.gamma import GammaClient
from pm_arb.data.settlement import settle_key
from pm_arb.infra.store import Store

DEFAULT_DB = Path("runtime/trade5m.sqlite3")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="历史窗口结算批量回填（Gamma）")
    ap.add_argument("--symbols", default="btc,eth", help="逗号分隔（默认 btc,eth）")
    ap.add_argument("--db", default=str(DEFAULT_DB), help="SQLite 路径")
    ap.add_argument("--windows", type=int, default=None,
                    help="回填最近 N 个已结束窗口（每币）")
    ap.add_argument("--since", default=None,
                    help='回填该时刻之后的窗口，如 "2026-09-12 08:00"（本地时区）')
    ap.add_argument("--dry-run", action="store_true", help="只打印不落库")
    return ap.parse_args(argv)


def _window_starts(args: argparse.Namespace, symbols: list[str]) -> list[tuple[str, int]]:
    cur = current_window_start()  # 当前窗口未结束，从上一个开始
    pairs: list[tuple[str, int]] = []
    if args.windows:
        n = args.windows
        starts = [cur - WINDOW_SECONDS * k for k in range(1, n + 1)]
        pairs = [(s, ws) for s in symbols for ws in starts]
    elif args.since:
        dt = datetime.fromisoformat(args.since).astimezone()
        ws0 = int(dt.timestamp()) // WINDOW_SECONDS * WINDOW_SECONDS
        n = (cur - ws0) // WINDOW_SECONDS
        starts = [ws0 + WINDOW_SECONDS * k for k in range(n)]
        pairs = [(s, ws) for s in symbols for ws in starts]
    else:
        raise SystemExit("必须指定 --windows N 或 --since ... 之一")
    return pairs


async def _run(args: argparse.Namespace) -> int:
    symbols = [s.strip().lower() for s in args.symbols.split(",") if s.strip()]
    pairs = _window_starts(args, symbols)
    store = Store(args.db)
    done = skip = miss = 0
    try:
        async with GammaClient() as gamma:
            for sym, ws in pairs:
                if args.dry_run:
                    print(f"[dry] {sym} {ws} 将回填 {sym}-updown-5m-{ws}")
                    continue
                try:
                    r = await settle_key(store, gamma, sym, ws, dry=args.dry_run)
                except Exception as e:
                    print(f"[err] {sym} {ws}: {str(e)[:100]}")
                    miss += 1
                    continue
                if r is None:
                    skip += 1  # 市场不存在或尚未结算
                elif r.get("outcome"):
                    done += 1
                    print(f"[结算] {sym} {ws}: {r['outcome']}（{r['winner']}）"
                          f" PnL ${r['pnl']:+.2f}"
                          + ("，已赎回" if r.get("redeemed") else
                             ("，待赎回（proxy）" if r["outcome"] == "win" else "")))
                else:
                    done += 1  # 无交易窗口：只回填了 market_winner
    finally:
        store.close()
    print(f"完成：结算/回填 {done}，未结算跳过 {skip}，失败 {miss}")
    return 0 if not miss else 1


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    args = _parse_args()
    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
