"""b09 二维特征交叉表（Step 4；工程落地方案 §5）。

单变量分桶之后的双变量验证：两特征各等频 5 分位 → 5×5 交叉表，
输出每格 N + 名义胜率（cand==final_outcome）+ future_return_60 均值。
N<384 的格标注"·"（方向性参考）。默认组合（优化方案 §30 顺序）：
spot_slope_30 × ud_ask_delta_30、spot_slope_30 × relative_obi。

用法（仓库根目录）::

    uv run python scripts/feature_matrix_2d.py --symbols btc
    uv run python scripts/feature_matrix_2d.py --symbols btc,eth \
        --x spot_slope_30 --y relative_obi
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pyarrow.parquet as pq

FEATURES_TAG = "hf_20260324_20260518"
DEFAULT_PAIRS = [
    ("slope_30", "ud_ask_delta_30"),
    ("slope_30", "relative_obi"),
]


def _bucket_edges(vals: list[float], nb: int = 5) -> list[float]:
    """等频分桶边界（含两端，len=nb+1）；空列表返回零边界。"""
    xs = sorted(vals)
    k = len(xs)
    if k == 0:
        return [0.0] * (nb + 1)
    return [xs[min(i * k // nb, k - 1)] for i in range(nb + 1)]


def _bucket_of(v: float, edges: list[float]) -> int:
    """v → 桶号（0..4）：左闭右开，最后桶闭区间。"""
    for i in range(len(edges) - 2, -1, -1):
        if v >= edges[i]:
            return i
    return 0


def matrix(path: Path, x: str, y: str) -> list[str]:
    rows = [r for r in pq.read_table(path).to_pylist()
            if r.get(x) is not None and r.get(y) is not None]
    sym = rows[0]["symbol"] if rows else "?"
    if not rows:
        return [f"### {sym}：{y} × {x}，N=0（特征全 None，跳过）\n"]
    ex = _bucket_edges([r[x] for r in rows])
    ey = _bucket_edges([r[y] for r in rows])
    cells: dict[tuple[int, int], list[dict]] = {}
    for r in rows:
        cells.setdefault((_bucket_of(r[x], ex), _bucket_of(r[y], ey)),
                         []).append(r)
    out = [f"### {sym}：{y}(行) × {x}(列)，N={len(rows)}\n"]
    head = "| y\\x | " + " | ".join(
        f"Q{i + 1} [{ex[i]:.4f},{ex[i + 1]:.4f}]" for i in range(5)) + " |"
    out.append(head)
    out.append("|---|" + "---|" * 5)
    for j in range(5):
        cells_txt = []
        for i in range(5):
            seg = cells.get((i, j), [])
            if not seg:
                cells_txt.append("–")
                continue
            n = len(seg)
            win = sum(1 for r in seg if r.get("final_outcome") == r.get("cand"))
            r60s = [r["future_return_60"] for r in seg
                    if r.get("future_return_60") is not None]
            r60 = sum(r60s) / len(r60s) if r60s else None
            flag = "·" if n < 384 else ""
            r60s_txt = f"{r60:+.4f}" if r60 is not None else "n/a"
            cells_txt.append(f"{n}{flag}<br>胜{win / n:.0%} ret{r60s_txt}")
        out.append(f"| Q{j + 1} [{ey[j]:.4f},{ey[j + 1]:.4f}] | "
                   + " | ".join(cells_txt) + " |")
    out.append("")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="b09 二维特征交叉表（5×5）")
    ap.add_argument("--symbols", default="btc")
    ap.add_argument("--features-dir", default="runtime/features")
    ap.add_argument("--x", default=None, help="列特征（缺省跑默认两组合）")
    ap.add_argument("--y", default=None, help="行特征")
    ap.add_argument("--out", default=None, help="markdown 输出路径（缺省打印）")
    args = ap.parse_args()

    pairs = [(args.x, args.y)] if args.x and args.y else DEFAULT_PAIRS
    lines = [
        "# b09 二维特征交叉表（Step 4）\n",
        f"- 数据：{args.features_dir}/*_features_{FEATURES_TAG}.parquet",
        "- · = 格 N<384（方向性参考）；胜 = cand==final_outcome（推断值）\n",
    ]
    for sym in [s.strip().lower() for s in args.symbols.split(",") if s.strip()]:
        path = Path(args.features_dir) / f"{sym}_features_{FEATURES_TAG}.parquet"
        if not path.is_file():
            print(f"跳过 {sym}：{path} 不存在")
            continue
        for x, y in pairs:
            lines.extend(matrix(path, x, y))
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
