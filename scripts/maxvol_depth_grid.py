"""b10 P3：max_vol 下探 + 深度 veto 网格（walk-forward 70/30）。

实验设计（b10 计划 P3，回应用户疑问 2）：
- max_vol 轴（分币种）：BTC 10/15/20 × ETH 0.4/0.65/1.0 —— b07 单调性
  未探底，本轮下探"再调低滤掉的是好单多还是坏单多"；
- min_depth_ratio 轴：None / 0.27 —— P0 全量重验后唯一 hard veto 候选
  （BTC 16.9% vs 22.8%，ETH 18.3% vs 25.2%，误杀 ~7.7%）；
- 口径沿四轮网格：固定参数与 grid.py 完全一致，窗口按时间序前 70% 训练、
  后 30% 验证，**报告只认 val 段**；
- 边际分解（用户疑问 2 的直接回答）：相邻档位间"被滤组"（高档可入、
  低档被拒）与"保留组"的胜率 / EV 对照 —— 被滤组 EV 更差 → 滤掉的是
  坏单（过滤有效）；被滤组 EV 更好 → 误杀好单。

输出：runtime/maxvol_depth_grid.csv（kind=grid/mvol/mdepth 三段）+ 控制台
边际分解表。

用法（仓库根目录，需 runtime/hf/{sym}_spot_1s.parquet）::

    python scripts/maxvol_depth_grid.py --workers 2
"""

from __future__ import annotations

import argparse
import csv
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

# 与 grid.py 四轮口径逐字一致（固定参数不进网格）
FIXED: dict[str, Decimal | int] = {
    "take_profit_price": Decimal("0.99"),   # multiple=None 时生效
    "min_entry": Decimal("0.20"),
    "max_entry": Decimal("0.30"),
    "entry_until": 100,
}
SYMBOL_VOLS: dict[str, list[Decimal]] = {
    "btc": [Decimal("10"), Decimal("15"), Decimal("20")],   # b07 最优 20 未探底
    "eth": [Decimal("0.4"), Decimal("0.65"), Decimal("1.0")],  # 等比 0.65 上下探
}
AXIS_DEPTH: list[Decimal | None] = [None, Decimal("0.27")]  # P0 重验阈值
SYMBOLS = ["btc", "eth"]
TRAIN_FRAC = 0.7
OUT_CSV = "runtime/maxvol_depth_grid.csv"


def _combos(symbol: str) -> list[tuple[Decimal, Decimal | None, Crypto5mParams]]:
    """(max_vol, min_depth_ratio, params) 笛卡尔积（vol × depth）。"""
    out = []
    for v in SYMBOL_VOLS[symbol]:
        for d in AXIS_DEPTH:
            p = Crypto5mParams().model_copy(update={**FIXED, "max_vol": v,
                                                    "min_depth_ratio": d})
            out.append((v, d, p))
    return out


def _run_symbol(symbol: str) -> tuple[str, list[tuple[Decimal, Decimal | None,
                                                       list[WindowResult]]]]:
    """worker：加载数据一次，跑该币全部 (vol, depth) 组（tick 共享）。"""
    ds = HfDataset(symbol)
    spot = SpotVol(symbol)   # max_vol 轴必需：无现货 → fail-closed 全 ABORT
    combos = _combos(symbol)
    grid = run_grid(ds, [c[2] for c in combos], spot=spot)
    return symbol, [(c[0], c[1], res) for c, res in zip(combos, grid, strict=True)]


def _stats(results: list[WindowResult]) -> dict:
    """入场窗口统计：n / 胜率（止盈+结算赢）/ 每笔 pnl / 均入场价。"""
    entered = [r for r in results if r.size > 0]
    n = len(entered)
    if not n:
        return {"n": 0, "win%": 0.0, "per": 0.0, "ask": 0.0}
    win = sum(1 for r in entered
              if r.exit_kind in (ExitKind.TAKE_PROFIT, ExitKind.SETTLE_WIN))
    pnl = sum(r.pnl for r in entered)
    ask = sum(r.entry_ask for r in entered) / n
    return {"n": n, "win%": round(win / n * 100, 1),
            "per": round(float(pnl / n), 4), "ask": round(float(ask), 3)}


def _marginal(res_hi: list[WindowResult],
              res_lo: list[WindowResult]) -> tuple[dict, dict]:
    """相邻档位边际分解（两结果列表同窗同序，run_grid 保证对齐）。

    被滤组 = 高档入场、低档被拒；保留组 = 低档也入场（高档必然入场）。
    """
    filtered = [a for a, b in zip(res_hi, res_lo, strict=True)
                if a.size > 0 and b.size == 0]
    kept = [b for b in res_lo if b.size > 0]
    return _stats(filtered), _stats(kept)


def main() -> int:
    ap = argparse.ArgumentParser(description="b10 P3 max_vol 下探 + 深度 veto 网格")
    ap.add_argument("--workers", type=int, default=2,
                    help="进程数（每 worker 一币，加载数据一次）")
    args = ap.parse_args()

    with ProcessPoolExecutor(max_workers=args.workers) as ex:
        futs = {s: ex.submit(_run_symbol, s) for s in SYMBOLS}
        got = {s: f.result() for s, f in futs.items()}

    rows: list[dict] = []
    for sym in SYMBOLS:
        combos = got[sym][1]   # _run_symbol 返回 (symbol, [(vol, depth, results)...])
        nw = len(combos[0][2])
        cut = int(nw * TRAIN_FRAC)
        # ---- kind=grid：逐参数组 train/val 统计 ----
        for v, d, res in combos:
            for split, seg in (("tr", res[:cut]), ("va", res[cut:])):
                st = _stats(seg)
                rows.append({"kind": "grid", "symbol": sym,
                             "max_vol": str(v), "min_depth_ratio": str(d),
                             "split": split, "group": "", **st})
        by_combo = {(v, d): res for v, d, res in combos}
        # ---- kind=mvol：相邻 vol 档边际（depth=None 隔离 vol 效应）----
        vols = SYMBOL_VOLS[sym]
        for v_hi, v_lo in zip(vols[1:], vols[:-1], strict=False):
            hi, lo = by_combo[(v_hi, None)], by_combo[(v_lo, None)]
            for split, (a, b) in (("tr", (hi[:cut], lo[:cut])),
                                  ("va", (hi[cut:], lo[cut:]))):
                flt, kep = _marginal(a, b)
                rows.append({"kind": "mvol", "symbol": sym,
                             "max_vol": f"{v_hi}>{v_lo}", "min_depth_ratio": "None",
                             "split": split, "group": "被滤", **flt})
                rows.append({"kind": "mvol", "symbol": sym,
                             "max_vol": f"{v_hi}>{v_lo}", "min_depth_ratio": "None",
                             "split": split, "group": "保留", **kep})
        # ---- kind=mdepth：depth veto 边际（各 vol 档）----
        for v in vols:
            hi, lo = by_combo[(v, None)], by_combo[(v, Decimal("0.27"))]
            for split, (a, b) in (("tr", (hi[:cut], lo[:cut])),
                                  ("va", (hi[cut:], lo[cut:]))):
                flt, kep = _marginal(a, b)
                rows.append({"kind": "mdepth", "symbol": sym,
                             "max_vol": str(v), "min_depth_ratio": "0.27",
                             "split": split, "group": "被滤", **flt})
                rows.append({"kind": "mdepth", "symbol": sym,
                             "max_vol": str(v), "min_depth_ratio": "0.27",
                             "split": split, "group": "保留", **kep})

    with open(OUT_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)

    # ---- 控制台：验证段汇总（纪律：只认 val）----
    for sym in SYMBOLS:
        print(f"\n=== {sym.upper()} 验证段（val）===")
        print(f"{'max_vol':<8} {'depth':<6} {'N':>6} {'胜率%':>6} "
              f"{'每笔EV':>8} {'均价':>6}")
        for r in rows:
            if (r["kind"] == "grid" and r["symbol"] == sym
                    and r["split"] == "va"):
                print(f"{r['max_vol']:<8} {r['min_depth_ratio']:<6} "
                      f"{r['n']:>6} {r['win%']:>6.1f} {r['per']:>+8.4f} "
                      f"{r['ask']:>6.3f}")
        print("\n-- 边际分解（val，被滤 vs 保留）--")
        for kind, label in (("mvol", "vol 下探"), ("mdepth", "depth veto")):
            for r in rows:
                if (r["kind"] == kind and r["symbol"] == sym
                        and r["split"] == "va"):
                    print(f"[{label}] {r['max_vol']:<10} {r['group']:<4} "
                          f"N={r['n']:>6} 胜率={r['win%']:>5.1f}% "
                          f"EV={r['per']:>+8.4f} 均价={r['ask']:.3f}")

    print(f"\n共 {len(rows)} 行 → {OUT_CSV}（报告只认 val 段）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
