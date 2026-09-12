"""HF 公开数据集回测 CLI（python -m pm_arb.backtest.hf_backtest）。

用法（仓库根目录）::

    python -m pm_arb.backtest.hf_backtest --symbols btc,eth
    python -m pm_arb.backtest.hf_backtest --symbols btc --limit 50   # 冒烟
"""

from __future__ import annotations

import argparse
from decimal import Decimal, InvalidOperation

from pm_arb.backtest.engine import run_backtest
from pm_arb.backtest.hf_loader import HfDataset
from pm_arb.backtest.report import print_report
from pm_arb.strategies.crypto_5m.params import Crypto5mParams

NOTES = [
    "数据集固定窗口 2026-03-24 → 2026-05-18，非活数据，微结构可能与当前不同；",
    "outcome 为数据集作者按窗口最后 tick bid 推断（非链上结算），结论以"
    " Gamma 交叉核对为准；",
    "数据集无现货 TWAP：max_vol 波动过滤关闭（rng=0），与实盘行为有差异；",
    "撮合保守：入场/止盈均要求档深 ≥ 份数，否则放弃/继续持有。",
]


def parse_overrides(items: list[str] | None) -> Crypto5mParams:
    """--param take_profit_price=0.55 形式覆盖默认参数。"""
    update: dict = {}
    for item in items or []:
        if "=" not in item:
            raise SystemExit(f"--param 格式应为 k=v：{item}")
        k, v = item.split("=", 1)
        try:
            update[k.strip()] = Decimal(v.strip())
        except InvalidOperation:
            raise SystemExit(f"--param 值须为数字：{item}") from None
    return Crypto5mParams().model_copy(update=update)


def main() -> int:
    ap = argparse.ArgumentParser(description="HF 数据集 5m Up/Down 回测")
    ap.add_argument("--symbols", default="btc,eth", help="逗号分隔币种")
    ap.add_argument("--parquet-dir", default="runtime/hf")
    ap.add_argument("--limit", type=int, default=None, help="每币只回放前 N 窗口（冒烟）")
    ap.add_argument("--param", action="append", default=None,
                    metavar="K=V", help="覆盖策略参数（可多次）")
    args = ap.parse_args()

    p = parse_overrides(args.param)
    print(f"参数: {p.model_dump()}")
    for sym in [s.strip().lower() for s in args.symbols.split(",") if s.strip()]:
        ds = HfDataset(sym, args.parquet_dir)
        skipped = sum(1 for m in ds.markets if m.outcome not in ("Up", "Down"))
        results = run_backtest(ds, p, limit=args.limit)
        print_report(results, skipped_no_outcome=skipped, notes=NOTES)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
