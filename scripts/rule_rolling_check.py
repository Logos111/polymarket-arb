"""b10 P4：候选规则 rolling walk-forward 稳定性检验（parquet 级）。

背景：P2 E2 在 ETH candidate_orders 上发现唯一 val EV 转正的单规则
``relative_obi ≥ 1``（val N=7165、胜率 28.5%、EV +0.0131），但它是
20 条候选中的最优（多重比较），且 BTC 上同类规则不进前列。本脚本做
两件事（P4 上线判定的前置检验）：

1. **rolling walk-forward（5 fold）**：窗口按时间序均分 5 段，逐段报告
   规则命中行的 N / 胜率 / EV / recall —— 检验"单段依赖"（若 EV 集中在
   一两个时段 = 时段巧合，非稳定信号）；
2. **跨币种对照**：同一固定阈值（≥1，可解释值，非分位数）在 BTC 上
   同口径复跑 —— 单币种信号不可上线。

EV 口径：taker 持有到结算 = 胜 (1−ask−fee) / 负 −(ask+fee)，
fee = 0.07·ask·(1−ask)；行级 EV 求均值。recall = 命中真单数 / 全体真单数。

用法（仓库根目录）::

    python scripts/rule_rolling_check.py --symbols btc,eth --folds 5
"""

from __future__ import annotations

import argparse
from collections import defaultdict

import pyarrow.parquet as pq

FEE_RATE = 0.07


def _ev(ask: float, win: bool) -> float:
    fee = FEE_RATE * ask * (1 - ask)
    return (1.0 - ask - fee) if win else -(ask + fee)


def _block(rows: list[dict], label: str) -> str:
    n = len(rows)
    if not n:
        return f"| {label} | 0 | — | — | — |"
    win = sum(1 for r in rows if r["win_label"])
    ev = sum(_ev(r["cand_ask"], r["win_label"]) for r in rows) / n
    ask = sum(r["cand_ask"] for r in rows) / n
    return (f"| {label} | {n} | {win / n:.3f} | {ev:+.4f} | {ask:.3f} |")


def run_symbol(symbol: str, features_dir: str, thr: float,
               folds: int) -> tuple[str, str]:
    path = (f"{features_dir}/{symbol}_candidate_orders_"
            f"hf_20260324_20260518.parquet")
    tbl = pq.read_table(path).to_pylist()
    wins_all = [r for r in tbl if r["win_label"]]

    lines = [f"\n## {symbol.upper()}（全量 {len(tbl)} 行，真单 "
             f"{len(wins_all)}，阈值 relative_obi ≥ {thr}）"]

    hit = [r for r in tbl if r["relative_obi"] is not None
           and r["relative_obi"] >= thr]
    hit_win = sum(1 for r in hit if r["win_label"])
    lines.append(f"\n全量命中 {len(hit)} 行（真单 {hit_win}，"
                 f"recall {hit_win / max(1, len(wins_all)):.3f}）\n")
    lines.append("| 区块 | N | 胜率 | EV | 均价 |")
    lines.append("|---|---|---|---|---|")
    lines.append(_block(hit, "全量"))
    lines.append(_block([r for r in hit if not r["win_label"]], "其中假单"))

    # rolling walk-forward：按窗口时间序均分 folds 段（窗口级切分，
    # 杜绝行级泄漏），逐段统计
    ws = sorted({r["window_start"] for r in tbl})
    k = max(1, len(ws) // folds)
    bounds = [ws[min((i + 1) * k, len(ws)) - 1] for i in range(folds - 1)]
    seg_of: dict[int, int] = {}
    for r in tbl:
        w = r["window_start"]
        seg_of[w] = sum(1 for b in bounds if w > b)
    by_seg: dict[int, list[dict]] = defaultdict(list)
    for r in tbl:
        by_seg[seg_of[r["window_start"]]].append(r)

    lines.append(f"\n### rolling walk-forward（{folds} fold，窗口级切分）\n")
    lines.append("| fold | N | 胜率 | EV | 均价 | recall |")
    lines.append("|---|---|---|---|---|---|")
    fold_evs: list[float] = []
    for i in range(folds):
        rows = by_seg.get(i, [])
        hs = [r for r in rows if r["relative_obi"] is not None
              and r["relative_obi"] >= thr]
        wtot = sum(1 for r in rows if r["win_label"])
        wh = sum(1 for r in hs if r["win_label"])
        rec = wh / wtot if wtot else 0.0
        n = len(hs)
        if n:
            ev = sum(_ev(r["cand_ask"], r["win_label"]) for r in hs) / n
            win = wh / n
            ask = sum(r["cand_ask"] for r in hs) / n
            fold_evs.append(ev)
        else:
            ev, win, ask = 0.0, 0.0, 0.0
        lines.append(f"| {i + 1} | {n} | {win:.3f} | {ev:+.4f} | {ask:.3f} "
                     f"| {rec:.3f} |")
    if fold_evs:
        pos = sum(1 for e in fold_evs if e > 0)
        lines.append(f"\nEV>0 的 fold：{pos}/{len(fold_evs)}；"
                     f"min {min(fold_evs):+.4f} / max {max(fold_evs):+.4f}")
    return symbol, "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="b10 P4 候选规则滚动检验")
    ap.add_argument("--symbols", default="btc,eth")
    ap.add_argument("--features-dir", default="runtime/features")
    ap.add_argument("--thr", type=float, default=1.0,
                    help="relative_obi 固定阈值（可解释值，非分位数）")
    ap.add_argument("--folds", type=int, default=5)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    parts = [f"# b10 P4：relative_obi ≥ {args.thr} 滚动检验\n",
             f"日期：2026-09-15 | folds={args.folds}，窗口级切分；"
             f"EV = taker 持有到结算口径"]
    for sym in args.symbols.split(","):
        _, text = run_symbol(sym.strip(), args.features_dir, args.thr,
                             args.folds)
        parts.append(text)
    report = "\n".join(parts) + "\n"
    print(report)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(report)
        print(f"written: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
