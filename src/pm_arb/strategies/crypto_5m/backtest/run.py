"""HF 公开数据集回测 CLI（一键：``pm-bt5m``）。

用法（仓库根目录）::

    pm-bt5m                                     # btc+eth 全量
    pm-bt5m --symbols btc --limit 50            # 冒烟（秒出）
    pm-bt5m --param take_profit_price=0.55      # 覆盖任意策略参数

等价模块调用：``python -m pm_arb.strategies.crypto_5m.backtest.run``
"""

from __future__ import annotations

import argparse

from ..params import Crypto5mParams, parse_overrides
from .engine import run_backtest
from .hf_loader import HfDataset
from .report import print_report
from .spot_vol import SpotVol

NOTES = [
    "数据集固定窗口 2026-03-24 → 2026-05-18，非活数据，微结构可能与当前不同；",
    "outcome 为数据集作者按窗口最后 tick bid 推断（非链上结算），结论以"
    " Gamma 交叉核对为准；",
    "--spot 未开：无现货 TWAP，max_vol 波动过滤关闭（rng=0），与实盘行为有差异；",
    "撮合保守：入场/止盈均要求档深 ≥ 份数，否则放弃/继续持有。",
]

NOTES_SPOT = [
    NOTES[0],
    NOTES[1],
    "现货波动为 Binance 1s K 线重建的滚动 60s TWAP high-low（收盘价均值近似；"
    "单所现货 ≠ Chainlink 多所聚合，极端行情可能有偏差）；",
    NOTES[3],
]


def parse_overrides_cli(items: list[str] | None) -> Crypto5mParams:
    """--param k=v 覆盖默认参数（解析逻辑在 params.parse_overrides）。"""
    return Crypto5mParams().model_copy(update=parse_overrides(items))


def main() -> int:
    ap = argparse.ArgumentParser(description="HF 数据集 5m Up/Down 回测")
    ap.add_argument("--symbols", default="btc,eth", help="逗号分隔币种")
    ap.add_argument("--parquet-dir", default="runtime/hf")
    ap.add_argument("--limit", type=int, default=None, help="每币只回放前 N 窗口（冒烟）")
    ap.add_argument("--param", action="append", default=None,
                    metavar="K=V", help="覆盖策略参数（可多次）")
    ap.add_argument("--spot", action="store_true",
                    help="启用 b07 现货波动（需 runtime/hf/{sym}_spot_1s.parquet）")
    args = ap.parse_args()

    p = parse_overrides_cli(args.param)
    print(f"参数: {p.model_dump()}")
    for sym in [s.strip().lower() for s in args.symbols.split(",") if s.strip()]:
        ds = HfDataset(sym, args.parquet_dir)
        spot = SpotVol(sym, args.parquet_dir) if args.spot else None
        skipped = sum(1 for m in ds.markets if m.outcome not in ("Up", "Down"))
        results = run_backtest(ds, p, limit=args.limit, spot=spot)
        print_report(results, skipped_no_outcome=skipped,
                     notes=NOTES_SPOT if args.spot else NOTES)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
