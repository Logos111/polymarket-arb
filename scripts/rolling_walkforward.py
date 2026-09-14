"""b10 P4：候选参数配置的 rolling walk-forward 多 fold 验证（引擎级）。

背景（b10 计划 P4）：P3 网格（maxvol_depth_grid.py）用单一 70/30 切分给
出 train/val 对照；本脚本对入围配置做**多 fold 时间分段验证**——窗口按
时间序均分 K 段，逐段报告入场窗口的 N / 胜率 / 每笔 pnl，检验"单段
依赖"（EV 集中在一两个时段 = 时段巧合，非稳定信号，175 子集教训）。

上线判定口径（与"验证段清晰超线"纪律一致）：
- 全 fold 汇总 EV > 0 且 > 1 SE（SE = pnl 标准差 / √N，费后口径）；
- EV>0 的 fold 数过半；
- 两币一致（单币种信号不上线，relative_obi≥1 已证伪为前车之鉴）。

配置项来自 P3 网格入围者，格式 ``symbol:max_vol:min_depth_ratio``
（depth 用 none 或数值），逗号分隔。

用法（仓库根目录，需 spot 数据）::

    python scripts/rolling_walkforward.py --configs btc:20:none,btc:20:0.27 --folds 5
"""

from __future__ import annotations

import argparse
import csv
import math
import sys
from concurrent.futures import ProcessPoolExecutor
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pm_arb.strategies.crypto_5m.backtest.engine import (  # noqa: E402
    ExitKind,
    WindowResult,
    run_grid,
)
from pm_arb.strategies.crypto_5m.backtest.hf_loader import HfDataset  # noqa: E402
from pm_arb.strategies.crypto_5m.backtest.spot_vol import SpotVol  # noqa: E402
from pm_arb.strategies.crypto_5m.params import Crypto5mParams  # noqa: E402

# 与 grid.py / maxvol_depth_grid.py 四轮口径逐字一致
FIXED: dict[str, Decimal | int] = {
    "take_profit_price": Decimal("0.99"),
    "min_entry": Decimal("0.20"),
    "max_entry": Decimal("0.30"),
    "entry_until": 100,
}
OUT_CSV = "runtime/rolling_walkforward.csv"


def parse_config(spec: str) -> tuple[str, Decimal, Decimal | None]:
    """symbol:max_vol:depth → (symbol, max_vol, min_depth_ratio)。"""
    try:
        sym, v, d = spec.strip().split(":")
    except ValueError:
        raise SystemExit(f"--configs 条目格式应为 symbol:max_vol:depth：{spec}") from None
    depth = None if d.lower() == "none" else Decimal(d)
    return sym, Decimal(v), depth


def _run_config(spec: str) -> tuple[str, Decimal, Decimal | None,
                                    list[WindowResult]]:
    """worker：单配置全量回放（run_grid 单参数组，tick 构造一次）。"""
    sym, v, d = parse_config(spec)
    ds = HfDataset(sym)
    spot = SpotVol(sym)
    p = Crypto5mParams().model_copy(update={**FIXED, "max_vol": v,
                                            "min_depth_ratio": d})
    res = run_grid(ds, [p], spot=spot)[0]
    return sym, v, d, res


def _stats(entered: list[WindowResult]) -> dict:
    n = len(entered)
    if not n:
        return {"n": 0, "win%": 0.0, "per": 0.0, "ask": 0.0, "se": 0.0}
    win = sum(1 for r in entered
              if r.exit_kind in (ExitKind.TAKE_PROFIT, ExitKind.SETTLE_WIN))
    pnls = [float(r.pnl) for r in entered]
    mean = sum(pnls) / n
    var = sum((x - mean) ** 2 for x in pnls) / n
    ask = sum(float(r.entry_ask or 0) for r in entered) / n
    return {"n": n, "win%": round(win / n * 100, 1),
            "per": round(mean, 4), "ask": round(ask, 3),
            "se": round(math.sqrt(var / n), 4)}


def main() -> int:
    ap = argparse.ArgumentParser(description="b10 P4 rolling walk-forward")
    ap.add_argument("--configs", required=True,
                    help="逗号分隔 symbol:max_vol:depth，如 btc:20:none,btc:20:0.27")
    ap.add_argument("--folds", type=int, default=5)
    ap.add_argument("--workers", type=int, default=2)
    args = ap.parse_args()

    specs = [s for s in args.configs.split(",") if s.strip()]
    print(f"rolling walk-forward：{len(specs)} 配置 × {args.folds} fold")
    with ProcessPoolExecutor(max_workers=args.workers) as ex:
        got = [f.result() for f in
               [ex.submit(_run_config, s) for s in specs]]

    rows: list[dict] = []
    fields_all: list[str] | None = None
    for sym, v, d, res in got:
        k = max(1, len(res) // args.folds)
        all_entered = [r for r in res if r.size > 0]
        agg = _stats(all_entered)
        verdict = ("超线" if agg["n"] and agg["per"] > agg["se"]
                   else "未超线" if agg["n"] else "无样本")
        pos_folds = 0
        for i in range(args.folds):
            seg = [r for r in res[i * k:(i + 1) * k] if r.size > 0]
            st = _stats(seg)
            pos_folds += 1 if st["per"] > 0 else 0
            rows.append({"symbol": sym, "max_vol": str(v),
                         "min_depth_ratio": str(d), "fold": i + 1, **st})
        rows.append({"symbol": sym, "max_vol": str(v),
                     "min_depth_ratio": str(d), "fold": "汇总",
                     **agg, "ev>0_folds": f"{pos_folds}/{args.folds}",
                     "verdict": verdict})
        if fields_all is None:
            fields_all = list(rows[0]) + ["ev>0_folds", "verdict"]
        print(f"[{sym} vol={v} depth={d}] 汇总 N={agg['n']} "
              f"胜率={agg['win%']}% EV={agg['per']:+.4f} SE={agg['se']:.4f} "
              f"→ {verdict}（EV>0 fold {pos_folds}/{args.folds}）")

    with open(OUT_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields_all or list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    print(f"\n共 {len(rows)} 行 → {OUT_CSV}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
