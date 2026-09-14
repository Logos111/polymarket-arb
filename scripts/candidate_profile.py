"""b10 P2：真假订单画像与误杀审计（读 candidate_orders 表）。

GPT 文档 Experiment 1/2/4/5 的落地：
- E1 画像：每特征 P(win) vs P(loss) 的 Cohen's d / AUC / 中位数（全量口径）
- E2 单规则：每条候选规则的 precision / recall / FP reduction / FN loss
- E4 Hard Negative：score 高却输的订单画像（找缺失特征）
- E5 误杀审计：按现行各过滤原因统计被拒真单（可回收项检查）

纪律：
- 全部数字标注"全量口径"；与 175 子集数字对照列于报告头；
- 单规则阈值只用 train 段（前 70% 窗口）选、val 段（后 30%）验证，
  报告只认 val 段（walk-forward 纪律，杜绝校准泄漏）。

用法（仓库根目录，先跑 make_candidate_orders.py）::

    uv run python scripts/candidate_profile.py --symbols btc \
        --out src/pm_arb/strategies/crypto_5m/backtest/runtime/candidate_profile_btc.md
"""

from __future__ import annotations

import argparse
import math
from pathlib import Path

import pyarrow.parquet as pq

FEATURES_TAG = "hf_20260324_20260518"
TRAIN_FRAC = 0.70            # 窗口级切分（b09 三轮同口径）

# 候选特征（入场时点已知；与 entered_order_analysis.py 保持一致）
FEATURES = [
    "obi_up", "obi_down", "relative_obi", "depth_ratio_underdog",
    "spread_underdog", "spread_favorite",
    "ret_15", "ret_30", "ret_60", "ret_90", "ret_120",
    "slope_15", "slope_30", "slope_60", "acceleration", "momentum_decay",
    "dist_high", "dist_low", "new_low_count_15", "new_low_count_30",
    "new_high_count_15", "new_high_count_30",
    "time_since_low", "time_since_high",
    "ud_ask_delta_5", "ud_ask_delta_10", "ud_ask_delta_15",
    "ud_ask_delta_30", "ud_ask_delta_60",
    "ud_mid_delta_10", "ud_mid_delta_30", "ud_mid_delta_60",
    "fav_mid_delta_10", "fav_mid_delta_30", "fav_mid_delta_60",
    "spot_token_divergence_10", "spot_token_divergence_30",
    "spot_token_divergence_60",
    "cand_ask", "score",
]


def _auc(pos: list[float], neg: list[float]) -> float:
    if not pos or not neg:
        return float("nan")
    allv = pos + neg
    order = sorted(range(len(allv)), key=lambda i: allv[i])
    ranks = [0.0] * len(allv)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and allv[order[j + 1]] == allv[order[i]]:
            j += 1
        avg = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    r_pos = sum(ranks[i] for i in range(len(pos)))
    u = r_pos - len(pos) * (len(pos) + 1) / 2
    return u / (len(pos) * len(neg))


def _cohens_d(pos: list[float], neg: list[float]) -> float:
    if len(pos) < 2 or len(neg) < 2:
        return float("nan")
    m1, m2 = sum(pos) / len(pos), sum(neg) / len(neg)
    v1 = sum((x - m1) ** 2 for x in pos) / (len(pos) - 1)
    v2 = sum((x - m2) ** 2 for x in neg) / (len(neg) - 1)
    sp = math.sqrt((v1 + v2) / 2)
    return (m1 - m2) / sp if sp > 0 else 0.0


def _median(xs: list[float]) -> float:
    s = sorted(xs)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def _se(p: float, n: int) -> float:
    return math.sqrt(p * (1 - p) / n) if n else float("nan")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default="btc")
    ap.add_argument("--features-dir", default="runtime/features")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    out: list[str] = []
    for symbol in [s.strip().lower() for s in args.symbols.split(",") if s.strip()]:
        src = (Path(args.features_dir)
               / f"{symbol}_candidate_orders_{FEATURES_TAG}.parquet")
        tbl = pq.read_table(src)
        d = tbl.to_pydict()
        n = tbl.num_rows
        # 窗口级 train/val 切分
        ws = sorted(set(d["window_start"]))
        cut = ws[int(len(ws) * TRAIN_FRAC)]
        idx_val = [i for i in range(n) if d["window_start"][i] >= cut]
        idx_train = [i for i in range(n) if d["window_start"][i] < cut]

        win_label = d["win_label"]
        wr_all = sum(win_label) / n
        wr_val = sum(win_label[i] for i in idx_val) / len(idx_val)
        wr_tr = sum(win_label[i] for i in idx_train) / len(idx_train)
        ask_val = sum(float(d["cand_ask"][i]) for i in idx_val) / len(idx_val)

        out.append(f"\n## {symbol.upper()} candidate_orders（全量口径，"
                   f"N={n}）\n")
        out.append(f"- 窗口 {len(ws)}（train 行 {len(idx_train)} / "
                   f"val 行 {len(idx_val)}，切分点 {cut}）")
        out.append(f"- 胜率：全量 {wr_all:.3f} / train {wr_tr:.3f} / "
                   f"**val {wr_val:.3f}**；val 均价 {ask_val:.3f}，"
                   f"盈亏平衡 {ask_val + 0.07 * ask_val * (1 - ask_val):.3f}")

        # ---- E1 画像（val 段，防校准泄漏后仍稳定的分离度）----
        pos = [i for i in idx_val if win_label[i]]
        neg = [i for i in idx_val if not win_label[i]]
        out.append(f"\n### E1 真/假订单分离度（val 段，真 {len(pos)} / "
                   f"假 {len(neg)}，按 |AUC-0.5| 降序）\n")
        out.append("| 特征 | 真中位 | 假中位 | Cohen's d | AUC |")
        out.append("|---|---|---|---|---|")
        stats = []
        for f in FEATURES:
            if f not in d:
                continue
            pv = [float(d[f][i]) for i in pos if d[f][i] is not None]
            nv = [float(d[f][i]) for i in neg if d[f][i] is not None]
            if len(pv) < 100 or len(nv) < 100:
                continue
            stats.append((f, _median(pv), _median(nv),
                          _cohens_d(pv, nv), _auc(pv, nv)))
        stats.sort(key=lambda s: -abs(s[4] - 0.5))
        for f, mp, mn, dd, auc in stats[:20]:
            out.append(f"| {f} | {mp:.5g} | {mn:.5g} | {dd:+.3f} | "
                       f"{auc:.3f} |")

        # ---- E2 单规则（阈值 train 选、val 验证）----
        out.append("\n### E2 单规则 precision/recall（阈值=train 分位数；"
                   "报告只认 val 段）\n")
        out.append("| 规则 | val N | val 胜率 | val EV | precision | "
                   "recall | FP降幅 |")
        out.append("|---|---|---|---|---|---|---|")
        rows = []
        true_total = len(pos)
        for f, _mp, _mn, dd, _aucv in stats[:12]:
            tv = [float(d[f][i]) for i in idx_train if d[f][i] is not None]
            if len(tv) < 500:
                continue
            st = sorted(tv)
            for q in (0.25, 0.5, 0.75):
                thr = st[int(len(st) * q)]
                if dd > 0:
                    keep = [i for i in idx_val
                            if d[f][i] is not None and float(d[f][i]) >= thr]
                    cond = f"{f}≥{thr:.5g}"
                else:
                    keep = [i for i in idx_val
                            if d[f][i] is not None and float(d[f][i]) <= thr]
                    cond = f"{f}≤{thr:.5g}"
                if len(keep) < 200:
                    continue
                kw = sum(win_label[i] for i in keep) / len(keep)
                ka = sum(float(d["cand_ask"][i]) for i in keep) / len(keep)
                ev = kw - ka - 0.07 * ka * (1 - ka)
                tp = sum(1 for i in keep if win_label[i])
                fp = len(keep) - tp
                fp_base = len(neg)
                rows.append((cond, len(keep), kw, ev, kw,
                             tp / true_total,
                             1 - fp / fp_base if fp_base else float("nan")))
        rows.sort(key=lambda r: -r[3])
        for cond, kn, kw, ev, prec, recall, fpr in rows[:20]:
            out.append(f"| {cond} | {kn} | {kw:.3f} | {ev:+.4f} | "
                       f"{prec:.3f} | {recall:.1%} | {fpr:.1%} |")

        # ---- E4 Hard Negative：score 高却输 ----
        if "score" in d and stats:
            out.append("\n### E4 Hard Negative 画像（val 段 score≥2 且输 "
                       "vs 全体赢家）\n")
            hn = [i for i in neg if d["score"][i] is not None
                  and d["score"][i] >= 2]
            out.append(f"- score≥2 且输：{len(hn)} 笔（假单中 "
                       f"{len(hn) / max(1, len(neg)):.1%}）")
            for f, _mp, _mn, _dd, _aucv in stats[:6]:
                hv = [float(d[f][i]) for i in hn if d[f][i] is not None]
                wv = [float(d[f][i]) for i in pos if d[f][i] is not None]
                if len(hv) < 50:
                    continue
                out.append(f"  - {f}: hard-neg 中位 {_median(hv):.5g} vs "
                           f"赢家中位 {_median(wv):.5g}")

        # ---- E5 误杀审计（现行条件挡掉的真单，从特征表全量行算）----
        feat_src = (Path(args.features_dir)
                    / f"{symbol}_features_{FEATURES_TAG}.parquet")
        if feat_src.exists():
            ft = pq.read_table(feat_src, columns=[
                "elapsed", "cand_ask", "final_outcome", "cand"])
            fd = ft.to_pydict()
            fn = ft.num_rows
            blocks = {
                "价格带外 ask≤0.20": ("cand_ask", "le", 0.20),
                "价格带外 ask>0.30": ("cand_ask", "gt", 0.30),
                "早窗 elapsed<90": ("elapsed", "lt", 90),
                "晚窗 elapsed>150": ("elapsed", "gt", 150),
            }
            out.append("\n### E5 误杀审计（特征表全量行，含非入场行；"
                       "标记被挡行的真单率）\n")
            out.append("| 区块 | N | 真单率 | 备注 |")
            out.append("|---|---|---|---|")
            for name, (key, op, thr) in blocks.items():
                idxs = []
                for i in range(fn):
                    v = fd[key][i]
                    if v is None:
                        continue
                    ok = ((op == "le" and float(v) <= thr)
                          or (op == "gt" and float(v) > thr)
                          or (op == "lt" and float(v) < thr))
                    if ok:
                        idxs.append(i)
                if not idxs:
                    continue
                t = sum(1 for i in idxs
                        if fd["final_outcome"][i] is not None
                        and fd["final_outcome"][i] == fd["cand"][i])
                out.append(f"| {name} | {len(idxs)} | "
                           f"{t / len(idxs):.3f} | 对照入场行 "
                           f"{wr_all:.3f} |")

    text = "\n".join(out) + "\n"
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"written: {args.out}")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
