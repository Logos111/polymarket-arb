"""b10 P0：depth_ratio hard-veto 候选的全量窗口重验。

背景（b10 计划 P0）：b09 的"depth<0.27 胜率 13.6%"出自 175 窗口子集
（2026-03-24 起前 16.6h 连续时段），该子集已被证实存在选择偏差（方向
不对称 8 倍在 1.2 万全量窗口上消失）。本脚本在**全量**窗口上直查重验，
不经 FeatureCapture（口径与 features.py compute_features 同源）。

口径：
- would-enter = elapsed ∈ [entry_after, entry_until]（默认 90~150）且
  冷门方 ask ∈ (min_entry, max_entry]（默认 0.20~0.30），冷门方 = ask 更低侧；
- depth_ratio_underdog = (ud ask_size × ud_ask) / (fav bid_size × fav_bid)
  —— 与 features.py L152-158 完全同口径（top-of-book 名义，非美元深度列）；
- 胜率 = P(final_outcome == cand)，outcome 为数据集推断值（非链上结算）。

输出（每币种）：
1. depth_ratio 等频 5 分位桶：N / 胜率 / 均价 / EV（持有到结算口径）；
2. 0.27 阈值两侧对照：胜率 / EV / 被挡真单数（误杀检查）；
3. 0.27 附近 ±0.05 网格（阈值稳健性）。

用法（仓库根目录）::

    uv run python scripts/depth_revalidation.py --symbols btc,eth
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pyarrow.parquet as pq

ENTRY_AFTER, ENTRY_UNTIL = 90, 150
MIN_ENTRY, MAX_ENTRY = 0.20, 0.30
FEE_RATE = 0.07
VETO_THR = 0.27           # b09 子集结论的阈值（score_weights.thin_depth_ratio）


def _cost(ask: float) -> float:
    return ask + FEE_RATE * ask * (1 - ask)


def load_groups(symbol: str, parquet_dir: str):
    """yield (outcome, [(t, au, ad, su, sd, sau, sad), ...]) 按窗口分组。"""
    mk = pq.read_table(f"{parquet_dir}/{symbol}_markets.parquet").to_pylist()
    outcome_by_cid = {r["condition_id"]: r["outcome"] for r in mk}
    tk = pq.read_table(
        f"{parquet_dir}/{symbol}_ticks.parquet",
        columns=["condition_id", "t", "bu", "au", "bd", "ad",
                 "su", "sd", "sau", "sad"])
    td = tk.to_pydict()
    tn = tk.num_rows

    cur_cid = None
    buf: list[tuple] = []

    def flush(cid, rows):
        if cid is None:
            return None
        outcome = outcome_by_cid.get(cid)
        if outcome not in ("Up", "Down"):
            return None
        return outcome, rows

    for i in range(tn):
        cid = td["condition_id"][i]
        if cid != cur_cid:
            g = flush(cur_cid, buf)
            if g:
                yield g
            cur_cid = cid
            buf = []
        buf.append((td["t"][i], td["bu"][i], td["au"][i], td["bd"][i],
                    td["ad"][i], td["su"][i], td["sd"][i],
                    td["sau"][i], td["sad"][i]))
    g = flush(cur_cid, buf)
    if g:
        yield g


def window_candidates(rows):
    """yield (elapsed, cand, ask, depth_ratio) 的 would-enter 行。"""
    t0 = rows[0][0]
    for t, bu, au, bd, ad, su, sd, sau, sad in rows:
        el = t - t0
        if not (ENTRY_AFTER <= el <= ENTRY_UNTIL):
            continue
        if not au or not ad or au != au or ad != ad:
            continue
        if au < ad:
            cand, ask = "Up", au
            ud_size, fav_size, fav_bid = sau, sd, bd
        else:
            cand, ask = "Down", ad
            ud_size, fav_size, fav_bid = sad, su, bu
        if not (MIN_ENTRY < ask <= MAX_ENTRY):
            continue
        if not ud_size or not fav_size or not fav_bid:
            continue
        ud_notional = ud_size * ask
        fav_notional = fav_size * fav_bid
        if fav_notional <= 0:
            continue
        yield el, cand, ask, ud_notional / fav_notional


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default="btc")
    ap.add_argument("--parquet-dir", default="runtime/hf")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    out: list[str] = []
    for symbol in [s.strip().lower() for s in args.symbols.split(",") if s.strip()]:
        # 累计器：分位桶按全量 would-enter 的 depth 分布切
        depths: list[float] = []
        recs: list[tuple[str, float, float, float]] = []   # (cand, ask, depth, win)
        n_windows = 0
        for outcome, rows in load_groups(symbol, args.parquet_dir):
            n_windows += 1
            for _el, cand, ask, depth in window_candidates(rows):
                depths.append(depth)
                recs.append((cand, ask, depth, outcome == cand))
        depths.sort()
        n = len(recs)
        win_rate = sum(1 for r in recs if r[3]) / n if n else float("nan")
        avg_ask = sum(r[1] for r in recs) / n if n else float("nan")

        out.append(f"\n## {symbol.upper()} 全量重验（窗口 {n_windows}，"
                   f"would-enter N={n}）\n")
        out.append(f"- 基线：胜率 {win_rate:.3f}，均价 {avg_ask:.3f}，"
                   f"盈亏平衡 {_cost(avg_ask):.3f}，"
                   f"EV/份 {win_rate - _cost(avg_ask):+.4f}")
        out.append("- 【对照】b09 175 窗口子集基线：胜率 0.256，EV -0.0132\n")

        # ---- 1. 等频 5 分位桶 ----
        out.append("\n### depth_ratio 等频 5 分位桶（全量口径）\n")
        out.append("| 桶 | depth 区间 | N | 胜率 | 均价 | EV/份 |")
        out.append("|---|---|---|---|---|---|")
        qs = []
        for i in range(5):
            lo = depths[int(n * i / 5)]
            hi = depths[int(n * (i + 1) / 5) - 1] if i < 4 else float("inf")
            qs.append((lo, hi))
        for i, (lo, hi) in enumerate(qs):
            seg = [r for r in recs if lo <= r[2] <= hi]
            if not seg:
                continue
            w = sum(1 for r in seg if r[3]) / len(seg)
            a = sum(r[1] for r in seg) / len(seg)
            out.append(f"| Q{i + 1} | [{lo:.3f}, {hi:.3f}] | {len(seg)} | "
                       f"{w:.3f} | {a:.3f} | {w - _cost(a):+.4f} |")

        # ---- 2. 0.27 阈值两侧对照 + 误杀 ----
        out.append("\n### depth_ratio 0.27 阈值两侧对照（hard veto 候选）\n")
        out.append("| 区块 | N | 胜率 | 均价 | EV/份 | 备注 |")
        out.append("|---|---|---|---|---|---|")
        thin = [r for r in recs if r[2] < VETO_THR]
        keep = [r for r in recs if r[2] >= VETO_THR]
        for name, seg, note in (
                (f"depth < {VETO_THR}（veto 区）", thin,
                 "子集口径胜率 13.6%"),
                (f"depth ≥ {VETO_THR}（保留区）", keep, "—")):
            if not seg:
                out.append(f"| {name} | 0 | — | — | — | {note} |")
                continue
            w = sum(1 for r in seg if r[3]) / len(seg)
            a = sum(r[1] for r in seg) / len(seg)
            out.append(f"| {name} | {len(seg)} | {w:.3f} | {a:.3f} | "
                       f"{w - _cost(a):+.4f} | {note} |")
        killed_true = sum(1 for r in thin if r[3])
        true_total = sum(1 for r in recs if r[3])
        if thin:
            out.append(f"\n- 误杀检查：veto 区含真单 {killed_true} 个"
                       f"（占全部真单 {killed_true / true_total:.1%}）")

        # ---- 3. 阈值稳健性网格 ----
        out.append("\n### 阈值网格（0.20~0.40，验证 0.27 是否局部最优）\n")
        out.append("| 阈值 | veto 区 N | veto 区胜率 | 保留区 N | 保留区胜率 |")
        out.append("|---|---|---|---|---|")
        for thr in (0.20, 0.23, 0.27, 0.31, 0.35, 0.40):
            t_ = [r for r in recs if r[2] < thr]
            k_ = [r for r in recs if r[2] >= thr]
            if not t_ or not k_:
                continue
            tw = sum(1 for r in t_ if r[3]) / len(t_)
            kw = sum(1 for r in k_ if r[3]) / len(k_)
            out.append(f"| {thr:.2f} | {len(t_)} | {tw:.3f} | "
                       f"{len(k_)} | {kw:.3f} |")

    text = "\n".join(out) + "\n"
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"written: {args.out}")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
