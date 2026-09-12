"""HF 数据集参数扫描（第一轮单维敏感性）。

进程内加载 btc/eth 数据各一次，对多组参数全量回放，输出对比表。
用法::

    python scripts/hf_sweep.py
"""

from __future__ import annotations

from decimal import Decimal

from pm_arb.strategies.crypto_5m.backtest.engine import ExitKind, run_backtest
from pm_arb.strategies.crypto_5m.backtest.hf_loader import HfDataset
from pm_arb.strategies.crypto_5m.params import Crypto5mParams

BASE = Crypto5mParams()


def sweep(name: str, update: dict) -> dict:
    p = BASE.model_copy(update=update)
    row = {"name": name}
    for sym in ("btc", "eth"):
        results = run_backtest(DS[sym], p)
        entered = [r for r in results if r.size > 0]
        n = len(entered)
        tp = sum(1 for r in results if r.exit_kind is ExitKind.TAKE_PROFIT)
        pnl = sum((r.pnl for r in results), start=Decimal(0))
        fee = sum((r.fee for r in results), start=Decimal(0))
        row[sym] = (n, tp / n if n else 0, pnl, fee, pnl / n if n else Decimal(0))
    return row


DS: dict[str, HfDataset] = {}

if __name__ == "__main__":
    for sym in ("btc", "eth"):
        print(f"loading {sym} ...")
        DS[sym] = HfDataset(sym)

    grid = [
        ("baseline tp=0.65", {}),
        ("tp=0.45", {"take_profit_price": Decimal("0.45")}),
        ("tp=0.55", {"take_profit_price": Decimal("0.55")}),
        ("tp=0.75", {"take_profit_price": Decimal("0.75")}),
        ("entry 0.15-0.25", {"max_entry": Decimal("0.25")}),
        ("entry 0.20-0.30", {"min_entry": Decimal("0.20")}),
        ("window 60-180", {"entry_after": 60, "entry_until": 180}),
        ("window 70-100", {"entry_until": 100}),
    ]
    print(f"\n{'参数组':<20} {'币':<4} {'入场':>6} {'止盈率':>7} "
          f"{'总PnL':>12} {'手续费':>10} {'每笔':>9}")
    for name, update in grid:
        row = sweep(name, update)
        for sym in ("btc", "eth"):
            n, tpr, pnl, fee, avg = row[sym]
            print(f"{name:<20} {sym:<4} {n:>6} {tpr:>7.1%} "
                  f"{float(pnl):>+12.2f} {float(fee):>10.2f} {float(avg):>+9.4f}")
