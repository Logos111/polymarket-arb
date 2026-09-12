"""HF 数据集批量调参网格扫描（一键：``pm-grid5m``）。

单遍数据多参数组共享回放（run_grid：每窗口 tick 只构造一次）+ 多进程
分片（每 worker 加载一次数据跑一组参数片）。

walk-forward 纪律：窗口按时间升序，前 TRAIN_FRAC 训练、后 (1-TRAIN_FRAC)
验证。**排名只看验证段**——训练段选优在样本内必然好看，拟合噪声的小
网格搜索极易在验证段现形。

用法（仓库根目录）::

    pm-grid5m                       # 默认网格（改下方 AXES 即可调参）
    pm-grid5m --workers 4

结果: runtime/grid_runs.csv + 控制台验证段排名。
等价模块调用：``python -m pm_arb.strategies.crypto_5m.backtest.grid``
"""

from __future__ import annotations

import argparse
import csv
import itertools
from concurrent.futures import ProcessPoolExecutor
from decimal import Decimal

from ..params import Crypto5mParams
from .engine import ExitKind, WindowResult, run_grid
from .hf_loader import HfDataset
from .spot_vol import SpotVol

TRAIN_FRAC = 0.7

# ── 网格定义（笛卡尔积；改这里即可调参）──────────────────────
# b07 轮：max_vol 轴需 --spot（Binance 1s K 线重建滚动 60s TWAP）；
# 止损已证伪固定关闭（stop_loss_price 默认 0），止盈/入场价带固定上轮最优。
AXES: dict[str, list] = {
    "max_vol": [Decimal("20"), Decimal("25"), Decimal("30")],
    "entry_until": [100, 135, 180],
}
# 固定参数（不进网格）：入场价带与止盈取上轮 60 组扫描最优
FIXED: dict[str, Decimal | int] = {
    "take_profit_price": Decimal("0.99"),
    "min_entry": Decimal("0.20"),
    "max_entry": Decimal("0.30"),
}
SYMBOLS = ["btc", "eth"]


def grid_params() -> list[Crypto5mParams]:
    keys = list(AXES)
    combos = list(itertools.product(*(AXES[k] for k in keys)))
    base = Crypto5mParams().model_copy(update=FIXED)
    return [base.model_copy(update=dict(zip(keys, combo, strict=True)))
            for combo in combos]


def _shards(items: list, n: int) -> list[list]:
    """把参数组均分成 n 片（末片可短）。"""
    if n <= 1:
        return [items]
    k, r = divmod(len(items), n)
    out, lo = [], 0
    for i in range(n):
        hi = lo + k + (1 if i < r else 0)
        if hi > lo:
            out.append(items[lo:hi])
        lo = hi
    return out


def _run_shard(symbol: str, params_shard: list[Crypto5mParams], use_spot: bool):
    """worker：加载数据一次，跑一片参数组。"""
    ds = HfDataset(symbol)
    spot = SpotVol(symbol) if use_spot else None
    return symbol, run_grid(ds, params_shard, spot=spot)


def _stats(results: list[WindowResult]) -> dict:
    entered = [r for r in results if r.size > 0]
    n = len(entered)
    if not n:
        return {"n": 0, "tp_rate": 0.0, "sl_rate": 0.0, "win_rate": 0.0,
                "pnl_per": 0.0, "pnl": 0.0}
    tp = sum(1 for r in entered if r.exit_kind is ExitKind.TAKE_PROFIT)
    sx = sum(1 for r in entered if r.exit_kind is ExitKind.STOP_LOSS)
    win = sum(1 for r in entered if r.exit_kind in (ExitKind.TAKE_PROFIT, ExitKind.SETTLE_WIN))
    pnl = sum(r.pnl for r in entered)
    return {"n": n, "tp_rate": tp / n, "sl_rate": sx / n, "win_rate": win / n,
            "pnl_per": float(pnl / n), "pnl": float(pnl)}


def main() -> int:
    ap = argparse.ArgumentParser(description="HF 数据集网格扫描")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--spot", action="store_true",
                    help="启用 b07 现货波动（max_vol 轴生效；需 spot_1s.parquet）")
    args = ap.parse_args()

    params_list = grid_params()
    print(f"网格 {len(params_list)} 组 × {len(SYMBOLS)} 币，workers={args.workers}"
          f"{'，spot 波动开' if args.spot else ''}")

    # 多进程分片：每 worker 加载一次数据跑一组参数片；任务 (币, 片) 对应回填
    per_symbol: dict[str, list[list[WindowResult]]] = {}
    shard_lists = {s: _shards(params_list, args.workers) for s in SYMBOLS}
    with ProcessPoolExecutor(max_workers=args.workers) as ex:
        futs = {(s, i): ex.submit(_run_shard, s, sh, args.spot)
                for s in SYMBOLS for i, sh in enumerate(shard_lists[s])}
        for s in SYMBOLS:
            per_symbol[s] = [None] * len(params_list)
        for (s, i), fut in futs.items():
            got_sym, got_res = fut.result()
            base = sum(len(x) for x in shard_lists[s][:i])
            for j, r in enumerate(got_res):
                per_symbol[s][base + j] = r

    # walk-forward：按窗口时间序切 train/val（markets 已按时间升序，run_grid 保序）
    rows = []
    for gi, p in enumerate(params_list):
        row = {k: str(getattr(p, k)) for k in AXES}
        for sym in SYMBOLS:
            res = per_symbol[sym][gi]
            cut = int(len(res) * TRAIN_FRAC)
            tr = _stats(res[:cut])
            va = _stats(res[cut:])
            row[f"{sym}_tr_n"] = tr["n"]
            row[f"{sym}_tr_win%"] = round(tr["win_rate"] * 100, 1)
            row[f"{sym}_tr_sl%"] = round(tr["sl_rate"] * 100, 1)
            row[f"{sym}_tr_per"] = round(tr["pnl_per"], 4)
            row[f"{sym}_va_n"] = va["n"]
            row[f"{sym}_va_win%"] = round(va["win_rate"] * 100, 1)
            row[f"{sym}_va_sl%"] = round(va["sl_rate"] * 100, 1)
            row[f"{sym}_va_per"] = round(va["pnl_per"], 4)
        rows.append(row)

    with open("runtime/grid_runs.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)

    rank = sorted(rows, key=lambda r: r["btc_va_per"] + r["eth_va_per"], reverse=True)
    print(f"\n{'排名(验证段)':<4}", "  ".join(f"{k:<10}" for k in AXES), end="  ")
    print("  ".join(f"{s}_tr_per   {s}_va_per" for s in SYMBOLS))
    for i, r in enumerate(rank[:15]):
        print(f"{i + 1:<4}", "  ".join(f"{r[k]:<10}" for k in AXES), end="  ")
        print("  ".join(f"{r[s + '_tr_per']:>+9.4f} {r[s + '_va_per']:>+9.4f}"
                        for s in SYMBOLS))
    print(f"\n共 {len(rows)} 组 → runtime/grid_runs.csv（排名只看验证段 _va_per）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
