"""b09 批次 0/1/2 单变量特征分桶分析（Step 3；工程落地方案 §5）。

输入：``pm-bt5m --capture-features --spot`` 落盘的特征 parquet
（runtime/features/{sym}_features_hf_20260324_20260518.parquet）。

方法（对齐 take_profit 分桶方法论，不引 pandas/sklearn）：
- 等频 5 分位桶（特征值排序切 5 段；None 行排除并报告）；
- 每桶输出：N / 入场占比 / 名义胜率（cand==final_outcome）/
  future_return_{30,60,120} 均值 / mfe_60 / mae_60 均值；
- 单调性检验：桶均值 vs 桶序的 Spearman 秩相关（手写公式，
  |ρ|≥0.9 视为单调，方向性参考）；N<384 的桶标注"样本不足"。

用法（仓库根目录）::

    uv run python scripts/feature_bucket_analysis.py --symbols btc
    uv run python scripts/feature_bucket_analysis.py --symbols btc,eth \
        --features obi_up,relative_obi --label future_return_60
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pyarrow.parquet as pq

# 批次 0（Book，无序列依赖）默认清单；批次 1/2 特征可经 --features 传入
BATCH0_FEATURES = [
    "obi_up", "obi_down", "relative_obi",
    "spread_underdog", "spread_favorite", "depth_ratio_underdog",
]
FEATURES_TAG = "hf_20260324_20260518"


def _spearman(xs: list[float]) -> float:
    """桶均值序列 vs (0..n-1) 的 Spearman 秩相关（无并列，手写公式）。"""
    n = len(xs)
    if n < 2:
        return 0.0
    rx = _ranks(xs)
    d2 = sum((i - r) ** 2 for i, r in enumerate(rx))
    return 1.0 - 6.0 * d2 / (n * (n * n - 1))


def _ranks(xs: list[float]) -> list[int]:
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    r = [0] * len(xs)
    for pos, i in enumerate(order):
        r[i] = pos
    return r


def _fmt(v: float | None, w: int = 10) -> str:
    return f"{v:>{w}.4f}" if v is not None else "n/a".rjust(w)


def _mean(seg: list[dict], key: str) -> float | None:
    vs = [r[key] for r in seg if r.get(key) is not None]
    return sum(vs) / len(vs) if vs else None


def analyze(path: Path, features: list[str],
            label: str) -> tuple[str, list[str]]:
    """返回 (标题, markdown 行列表)；数字全部取自 parquet 数据。"""
    tbl = pq.read_table(path)
    rows = tbl.to_pylist()
    sym = rows[0]["symbol"] if rows else "?"
    out: list[str] = []
    out.append(f"### {sym}（{len(rows)} 行）\n")
    out.append("| 特征 | 桶 | N | 入场率 | 胜率 | ret30 | ret60 | ret120 "
               "| mfe60 | mae60 | 单调ρ |")
    out.append("|---|---|---|---|---|---|---|---|---|---|---|")
    for feat in features:
        vals = [r for r in rows if r.get(feat) is not None]
        n_none = len(rows) - len(vals)
        vals.sort(key=lambda r: r[feat])
        k = len(vals)
        tag = f"{feat}（排除None {n_none}）" if n_none else feat
        if k < 5:
            out.append(f"| {tag} | - | 0 | - | - | - | - | - | - | - | - |")
            continue
        means: list[float] = []
        for b in range(5):
            seg = vals[b * k // 5:(b + 1) * k // 5] or vals[b * k // 5:b * k // 5 + 1]
            n = len(seg)
            win = sum(1 for r in seg if r.get("final_outcome") == r.get("cand"))
            ent = sum(1 for r in seg if r.get("entered"))
            r30, r60, r120 = (_mean(seg, "future_return_30"),
                              _mean(seg, "future_return_60"),
                              _mean(seg, "future_return_120"))
            mfe, mae = _mean(seg, "mfe_60"), _mean(seg, "mae_60")
            low = seg[0][feat]
            high = seg[-1][feat]
            out.append(
                f"| {tag if b == 0 else ''} | Q{b + 1}"
                f" [{low:.4f},{high:.4f}] | {n}"
                f"{'*' if n < 384 else ''} | {ent / n:.1%} | {win / n:.1%} "
                f"| {_fmt(r30)} | {_fmt(r60)} | {_fmt(r120)} "
                f"| {_fmt(mfe)} | {_fmt(mae)} | |")
            if label in seg[0]:
                vs = [r[label] for r in seg if r.get(label) is not None]
                means.append(sum(vs) / len(vs) if vs else 0.0)
        if means:
            rho = _spearman(means)
            out[-1] = out[-1].rstrip()[:-1] + f" {rho:+.2f} |"
    return sym, out


def main() -> int:
    ap = argparse.ArgumentParser(description="b09 特征分桶分析（等频 5 桶）")
    ap.add_argument("--symbols", default="btc")
    ap.add_argument("--features-dir", default="runtime/features")
    ap.add_argument("--features", default=",".join(BATCH0_FEATURES),
                    help="逗号分隔特征名（默认批次 0 Book 组）")
    ap.add_argument("--label", default="future_return_60",
                    help="单调性检验的目标列")
    ap.add_argument("--out", default=None, help="markdown 输出路径（缺省打印）")
    args = ap.parse_args()

    feats = [f.strip() for f in args.features.split(",") if f.strip()]
    lines: list[str] = [
        "# b09 特征分桶报告（Step 3 单变量）\n",
        f"- 数据：{args.features_dir}/*_features_{FEATURES_TAG}.parquet"
        f"（HF 数据集 2026-03-24→05-18，outcome 为推断值）",
        f"- 特征：{', '.join(feats)}；目标列：{args.label}",
        "- * = 桶 N<384（方向性参考）；单调ρ = 桶均值 vs 桶序 Spearman\n",
    ]
    for sym in [s.strip().lower() for s in args.symbols.split(",") if s.strip()]:
        path = Path(args.features_dir) / f"{sym}_features_{FEATURES_TAG}.parquet"
        if not path.is_file():
            print(f"跳过 {sym}：{path} 不存在（先跑 pm-bt5m --capture-features）")
            continue
        _, out = analyze(path, feats, args.label)
        lines.extend(out)
        lines.append("")
    text = "\n".join(lines) + "\n"
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"已写入 {args.out}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
