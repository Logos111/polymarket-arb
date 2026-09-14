"""b10 P1：从全量特征 parquet 派生 candidate_orders 表（GPT 文档 §18 形态）。

一行 = 一个"基础策略可入场决策时刻"：elapsed ∈ [entry_after, entry_until]
且冷门方 ask ∈ (min_entry, max_entry] 的逐秒行。后续 P2/P3/P4 全部实验
只读这张表，不再解析原始 tick。

派生列（不引入任何前视信息，均为入场时点已知或 Label 区）：
- win_label：final_outcome == cand（主标签，结算口径）
- ev_breakeven：该行盈亏平衡胜率 = ask + 0.07×ask×(1−ask)
- 原表全部特征列、score、baseline_enter 原样保留

用法（仓库根目录，先跑全量采集）::

    uv run pm-bt5m --symbols btc --capture-features --spot   # 全量采集
    uv run python scripts/make_candidate_orders.py --symbols btc,eth
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

FEATURES_TAG = "hf_20260324_20260518"
ENTRY_AFTER, ENTRY_UNTIL = 90, 150
MIN_ENTRY, MAX_ENTRY = 0.20, 0.30
FEE_RATE = 0.07


def derive(symbol: str, features_dir: str, out_dir: str) -> tuple[int, int]:
    src = Path(features_dir) / f"{symbol}_features_{FEATURES_TAG}.parquet"
    tbl = pq.read_table(src)
    d = tbl.to_pydict()
    n = tbl.num_rows

    keep_idx = []
    for i in range(n):
        el = d["elapsed"][i]
        ask = d["cand_ask"][i]
        if el is None or ask is None:
            continue
        if not (ENTRY_AFTER <= el <= ENTRY_UNTIL):
            continue
        if not (MIN_ENTRY < float(ask) <= MAX_ENTRY):
            continue
        keep_idx.append(i)

    out_cols: dict[str, list] = {}
    for col in tbl.column_names:
        vals = d[col]
        if col == "entered":
            continue
        out_cols[col] = [vals[i] for i in keep_idx]

    # 派生列
    n_k = len(keep_idx)
    win_label = []
    breakeven = []
    for i in keep_idx:
        outcome = d["final_outcome"][i]
        cand = d["cand"][i]
        ask = float(d["cand_ask"][i])
        win_label.append(outcome is not None and outcome == cand)
        breakeven.append(ask + FEE_RATE * ask * (1 - ask))
    out_cols["win_label"] = win_label
    out_cols["ev_breakeven"] = breakeven

    out = Path(out_dir) / f"{symbol}_candidate_orders_{FEATURES_TAG}.parquet"
    pq.write_table(pa.table(out_cols), out)
    return n, n_k


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default="btc,eth")
    ap.add_argument("--features-dir", default="runtime/features")
    ap.add_argument("--out-dir", default="runtime/features")
    args = ap.parse_args()

    for symbol in [s.strip().lower() for s in args.symbols.split(",") if s.strip()]:
        n_total, n_kept = derive(symbol, args.features_dir, args.out_dir)
        print(f"[{symbol}] 特征行 {n_total} → candidate_orders {n_kept} 行 "
              f"（{n_kept / max(1, n_total):.1%}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
