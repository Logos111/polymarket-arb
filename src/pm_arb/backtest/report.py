"""回测汇总报表（终端输出）。"""

from __future__ import annotations

from collections import Counter
from decimal import Decimal

from pm_arb.backtest.engine import ExitKind, WindowResult


def print_report(results: list[WindowResult], *, skipped_no_outcome: int, notes: list[str]) -> None:
    n = len(results)
    kinds = Counter(r.exit_kind for r in results)
    total_pnl = sum((r.pnl for r in results), start=Decimal(0))
    total_fee = sum((r.fee for r in results), start=Decimal(0))

    entered = [r for r in results if r.size > 0]
    tp = kinds[ExitKind.TAKE_PROFIT]
    sw = kinds[ExitKind.SETTLE_WIN]
    sl = kinds[ExitKind.SETTLE_LOSE]
    settled = sw + sl

    print("=" * 64)
    print("回测汇总（HF 公开数据集，秒级快照回放）")
    print("=" * 64)
    for note in notes:
        print(f"[!] {note}")
    print(f"窗口: 回放 {n}（outcome 缺失跳过 {skipped_no_outcome}）")
    print(f"入场: {len(entered)}  "
          f"(信号成立档深放弃 {kinds[ExitKind.NO_ENTRY_DEPTH]})")
    if entered:
        print(f"止盈: {tp}  ({tp / len(entered):.1%} of 入场)")
        print(f"结算: 赢 {sw} / 输 {sl}"
              f"  (胜率 {sw / settled:.1%} of 结算)" if settled else "结算: 0")
        wins = sw + tp
        print(f"名义胜率(含止盈): {wins / len(entered):.1%}")
        print(f"总 PnL: ${total_pnl:+.2f}   其中手续费 ${total_fee:.2f}")
        print(f"每笔均值: ${total_pnl / len(entered):+.4f}")
        pnls = sorted(r.pnl for r in entered)
        qs = [pnls[int(len(pnls) * q)] for q in (0.05, 0.25, 0.5, 0.75, 0.95)]
        print("PnL 分位 5/25/50/75/95: "
              + " / ".join(f"${q:+.2f}" for q in qs))
        print("入场分布 by 方向: "
              + str(Counter(r.cand for r in entered)))
        print("入场分布 by 价格桶: ")
        buckets: Counter[str] = Counter()
        for r in entered:
            buckets[f"{float(r.entry_ask):.2f}"] += 1
        for k in sorted(buckets):
            print(f"  {k}: {buckets[k]}")
    print("=" * 64)
