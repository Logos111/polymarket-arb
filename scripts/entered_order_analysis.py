"""b09 五轮：真假订单精确分析（用户核心问题：过滤假订单 + 回收误杀订单）。

口径（对齐四轮网格 FIXED）：
- would-enter = elapsed ∈ [entry_after, entry_until] 且 cand_ask ∈ (min_entry, max_entry]
  （新默认窗 [90,150]，采集窗 [40,165] 完整覆盖，无需重采集）；
- 真订单 = final_outcome（cand 结算赢）；假订单 = 结算输；
- EV/份（持有到结算口径）= 胜率 × $1 − cost，cost = ask + 0.07×ask×(1−ask)
  → 盈亏平衡胜率 ≈ ask + 0.07×ask×(1−ask)。

三个问题：
A. 假订单 vs 真订单的特征分离度（Cohen's d + AUC，双指标防单一口径误导）；
B. 过滤 trade-off：候选过滤条件的 保留N/胜率/EV/误杀真订单数 —— 宁可不做；
C. 误杀回收：被现行条件挡掉的真实反转订单（价格带外/时间窗外）是否有正 EV。

用法（仓库根目录）::

    uv run python scripts/entered_order_analysis.py --symbols btc \
        --out src/pm_arb/strategies/crypto_5m/backtest/runtime/entered_order_btc.md
"""

from __future__ import annotations

import argparse
import math
from pathlib import Path

import pyarrow.parquet as pq

FEATURES_TAG = "hf_20260324_20260518"
ENTRY_AFTER, ENTRY_UNTIL = 90, 150        # 新默认窗（params.py 手改后）
MIN_ENTRY, MAX_ENTRY = 0.20, 0.30         # 四轮网格 FIXED 价格带
FEE_RATE = 0.07

# 候选特征（全部入场时点已知；排除 label/元数据/score 本身）
FEATURES = [
    # Book
    "obi_up", "obi_down", "relative_obi", "depth_ratio_underdog",
    "spread_underdog", "spread_favorite",
    # Trend / momentum
    "ret_15", "ret_30", "ret_60", "ret_90", "ret_120",
    "slope_15", "slope_30", "slope_60", "acceleration", "momentum_decay",
    # Extreme
    "dist_high", "dist_low", "new_low_count_15", "new_low_count_30",
    "new_high_count_15", "new_high_count_30",
    "time_since_low", "time_since_high",
    # Token / favorite / cross
    "ud_ask_delta_5", "ud_ask_delta_10", "ud_ask_delta_15",
    "ud_ask_delta_30", "ud_ask_delta_60",
    "ud_mid_delta_10", "ud_mid_delta_30", "ud_mid_delta_60",
    "fav_mid_delta_10", "fav_mid_delta_30", "fav_mid_delta_60",
    "spot_token_divergence_10", "spot_token_divergence_30",
    "spot_token_divergence_60",
    # 入场时点价格本身
    "cand_ask",
]


def _load(symbol: str) -> list[dict]:
    path = Path(f"runtime/features/{symbol}_features_{FEATURES_TAG}.parquet")
    tbl = pq.read_table(path)
    cols = tbl.column_names
    rows = []
    d = tbl.to_pydict()
    n = tbl.num_rows
    for i in range(n):
        r = {c: d[c][i] for c in cols}
        rows.append(r)
    return rows


def _cost(ask: float) -> float:
    return ask + FEE_RATE * ask * (1 - ask)


def _win(rows: list[dict]) -> float:
    vs = [r["final_outcome"] == r["cand"] for r in rows
          if r.get("final_outcome") is not None]
    return sum(1 for v in vs if v) / len(vs) if vs else float("nan")


def _ev(win_rate: float, ask_mean: float) -> float:
    return win_rate - _cost(ask_mean)


def _median(xs: list[float]) -> float:
    s = sorted(xs)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def _auc(pos: list[float], neg: list[float]) -> float:
    """Mann-Whitney AUC：随机真订单特征值 > 随机假订单特征值的概率。"""
    if not pos or not neg:
        return float("nan")
    allv = pos + neg
    order = sorted(range(len(allv)), key=lambda i: allv[i])
    ranks = [0.0] * len(allv)
    i = 0
    while i < len(order):        # 并列取平均秩
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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default="btc")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    out_lines: list[str] = []
    for symbol in args.symbols.split(","):
        rows = _load(symbol)
        in_win = [r for r in rows
                  if ENTRY_AFTER <= r["elapsed"] <= ENTRY_UNTIL]
        we = [r for r in in_win
              if r["cand_ask"] is not None
              and MIN_ENTRY < r["cand_ask"] <= MAX_ENTRY]
        win = [r for r in we if r.get("final_outcome") == r["cand"]]
        lose = [r for r in we if r.get("final_outcome") is not None
                and r.get("final_outcome") != r["cand"]]
        n = len(win) + len(lose)
        wr = len(win) / n if n else float("nan")
        ask_mean = sum(r["cand_ask"] for r in we) / len(we)
        be = _cost(ask_mean)          # 盈亏平衡胜率
        ev = wr - be

        out_lines.append(f"\n## {symbol.upper()}：would-enter 基线\n")
        out_lines.append(f"- N={n}（真 {len(win)} / 假 {len(lose)}），"
                         f"胜率 {wr:.3f}，入场均价 {ask_mean:.3f}，"
                         f"盈亏平衡胜率 {be:.3f}，EV/份 {ev:+.4f}")

        # ---- A. 分离度表 ----
        out_lines.append(f"\n### A. 真/假订单特征分离度（按 |AUC-0.5| 降序，N={n}）\n")
        out_lines.append("| 特征 | 真订单中位 | 假订单中位 | Cohen's d | AUC | "
                         "方向 |")
        out_lines.append("|---|---|---|---|---|---|")
        stats = []
        for f in FEATURES:
            pos = [r[f] for r in win if r.get(f) is not None]
            neg = [r[f] for r in lose if r.get(f) is not None]
            if len(pos) < 30 or len(neg) < 30:
                continue
            auc = _auc(pos, neg)
            d = _cohens_d(pos, neg)
            stats.append((f, _median(pos), _median(neg), d, auc))
        stats.sort(key=lambda s: -abs(s[4] - 0.5))
        for f, mp, mn, d, auc in stats:
            direction = "真>假" if d > 0 else "真<假"
            out_lines.append(
                f"| {f} | {mp:.5g} | {mn:.5g} | {d:+.3f} | {auc:.3f} | "
                f"{direction} |")

        # ---- B. 过滤 trade-off（对 top 特征按分位数扫阈值）----
        out_lines.append("\n### B. 过滤 trade-off（保留胜率↑且误杀真订单少者优先；"
                         "阈值=假订单分布分位数）\n")
        out_lines.append("| 过滤条件 | 保留N | 保留胜率 | 保留EV | 滤掉N | "
                         "滤掉胜率 | 误杀真订单 | 误杀率 |")
        out_lines.append("|---|---|---|---|---|---|---|---|")
        trades = []
        for f, _mp, _mn, d, _aucv in stats[:12]:   # 只看 top 分离度特征
            vals = [r[f] for r in we if r.get(f) is not None]
            if len(vals) < 100:
                continue
            s = sorted(vals)
            for q in (0.25, 0.5, 0.75):
                thr = s[int(len(s) * q)]
                if d > 0:   # 真>假：保留 ≥ thr
                    keep = [r for r in we if r.get(f) is not None and r[f] >= thr]
                    cond = f"{f}≥{thr:.5g}"
                else:       # 真<假：保留 ≤ thr
                    keep = [r for r in we if r.get(f) is not None and r[f] <= thr]
                    cond = f"{f}≤{thr:.5g}"
                drop = [r for r in we if r not in keep]
                if len(keep) < 100 or not drop:
                    continue
                kw = _win(keep)
                dw = _win(drop)
                ka = sum(r["cand_ask"] for r in keep) / len(keep)
                killed_true = sum(1 for r in drop
                                   if r.get("final_outcome") == r["cand"])
                true_total = sum(1 for r in keep + drop
                                 if r.get("final_outcome") is not None)
                trades.append((cond, len(keep), kw, kw - _cost(ka),
                               len(drop), dw, killed_true,
                               killed_true / max(1, true_total)))
        trades.sort(key=lambda t: -t[2])
        for cond, kn, kw, kev, dn, dw, kt, kmr in trades[:25]:
            out_lines.append(
                f"| {cond} | {kn} | {kw:.3f} | {kev:+.4f} | {dn} | {dw:.3f} | "
                f"{kt} | {kmr:.1%} |")

        # ---- C. 误杀回收检查 ----
        out_lines.append("\n### C. 误杀回收：现行条件挡掉的订单是否藏正 EV\n")
        out_lines.append("| 区块 | N | 胜率 | 均价 | 盈亏平衡 | EV/份 |")
        out_lines.append("|---|---|---|---|---|---|")
        blocks = [
            ("窗内 ask∈(0.10,0.15]（过冷带）",
             [r for r in in_win if r["cand_ask"] is not None
              and 0.10 < r["cand_ask"] <= 0.15]),
            ("窗内 ask∈(0.15,0.20]（下带外）",
             [r for r in in_win if r["cand_ask"] is not None
              and 0.15 < r["cand_ask"] <= 0.20]),
            ("窗内 ask∈(0.30,0.40]（上带外）",
             [r for r in in_win if r["cand_ask"] is not None
              and 0.30 < r["cand_ask"] <= 0.40]),
            ("早窗 [40,90) 带内",
             [r for r in rows if 40 <= r["elapsed"] < ENTRY_AFTER
              and r["cand_ask"] is not None
              and MIN_ENTRY < r["cand_ask"] <= MAX_ENTRY]),
            ("晚窗 (150,165] 带内",
             [r for r in rows if r["elapsed"] > ENTRY_UNTIL
              and r["cand_ask"] is not None
              and MIN_ENTRY < r["cand_ask"] <= MAX_ENTRY]),
        ]
        for name, seg in blocks:
            nn = sum(1 for r in seg if r.get("final_outcome") is not None)
            if nn < 50:
                out_lines.append(f"| {name} | {nn} | 样本不足 | — | — | — |")
                continue
            sw = _win(seg)
            sa = sum(r["cand_ask"] for r in seg) / len(seg)
            out_lines.append(
                f"| {name} | {nn} | {sw:.3f} | {sa:.3f} | {_cost(sa):.3f} | "
                f"{sw - _cost(sa):+.4f} |")

    text = "\n".join(out_lines) + "\n"
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"written: {args.out}")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
