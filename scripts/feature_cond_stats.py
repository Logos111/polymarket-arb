"""b09 复合条件统计（Step 4b；批次 1/2 三因子交叉验证）。

单变量/二维之后的复合条件验证：对特征 parquet 逐行过滤，
输出 N / 入场率 / 名义胜率 / ret60 / mfe_60 / mae_60 均值。
条件为纯比较表达式（特征名 >/< 阈值），在行情语义上可复述。

用法（仓库根目录）::

    uv run python scripts/feature_cond_stats.py --symbols btc
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pyarrow.parquet as pq

FEATURES_TAG = "hf_20260324_20260518"

# 三因子：relative_obi（冷门买压）× depth_ratio（盘口深度）× ud_ask_delta_30（冷门已跌幅度）
# 阈值取分桶报告的分位边界（Q1/Q3 边界附近，取整便于复述）
CONDITIONS: list[tuple[str, str]] = [
    ("基准（全行）", "True"),
    ("反转: ud30<-0.08", "ud_ask_delta_30 is not None and ud_ask_delta_30 < -0.08"),
    ("深反转: ud30<-0.15", "ud_ask_delta_30 is not None and ud_ask_delta_30 < -0.15"),
    ("买压: rel_obi>0.53", "relative_obi is not None and relative_obi > 0.53"),
    ("深度: dep>0.45", "depth_ratio_underdog is not None and depth_ratio_underdog > 0.45"),
    ("深度薄: dep<0.27", "depth_ratio_underdog is not None and depth_ratio_underdog < 0.27"),
    ("陡涨(负对照): slope30>0.48", "slope_30 is not None and slope_30 > 0.48"),
    ("动能增强: mom_dec<0.0001",
     "momentum_decay is not None and momentum_decay < 0.0001"),
    ("三因子: ud30<-0.08 & rel_obi>0.53",
     "ud_ask_delta_30 < -0.08 and relative_obi > 0.53"),
    ("三因子+深度: ud30<-0.08 & rel_obi>0.53 & dep>0.45",
     "ud_ask_delta_30 < -0.08 and relative_obi > 0.53"
     " and depth_ratio_underdog > 0.45"),
    ("深反转+动能: ud30<-0.15 & mom_dec<0.0001",
     "ud_ask_delta_30 < -0.15 and momentum_decay < 0.0001"),
    ("深反转(现货跌): ud30<-0.15 & slope60<0",
     "ud_ask_delta_30 < -0.15 and slope_60 < 0"),
    ("深反转(现货涨): ud30<-0.15 & slope60>0",
     "ud_ask_delta_30 < -0.15 and slope_60 > 0"),
    ("深反转+动能(现货跌)",
     "ud_ask_delta_30 < -0.15 and momentum_decay < 0.0001 and slope_60 < 0"),
    ("深反转+动能(现货涨)",
     "ud_ask_delta_30 < -0.15 and momentum_decay < 0.0001 and slope_60 > 0"),
]


def _mean(rs: list[dict], key: str) -> float | None:
    vs = [r[key] for r in rs if r.get(key) is not None]
    return sum(vs) / len(vs) if vs else None


def _fmt(v: float | None) -> str:
    return f"{v:+.4f}" if v is not None else "n/a"


def stats(path: Path) -> list[str]:
    rows = pq.read_table(path).to_pylist()
    sym = rows[0]["symbol"] if rows else "?"
    out = [f"### {sym}（{len(rows)} 行）\n",
           "| 条件 | N | 入场率 | 胜率 | ret60 | mfe60 | mae60 |",
           "|---|---|---|---|---|---|---|"]
    for name, expr in CONDITIONS:
        seg = [r for r in rows if _safe_eval(expr, r)]
        if not seg:
            out.append(f"| {name} | 0 | - | - | - | - | - |")
            continue
        n = len(seg)
        win = sum(1 for r in seg if r.get("final_outcome") == r.get("cand"))
        ent = sum(1 for r in seg if r.get("entered"))
        out.append(
            f"| {name} | {n}{'*' if n < 384 else ''} | {ent / n:.1%}"
            f" | {win / n:.1%} | {_fmt(_mean(seg, 'future_return_60'))}"
            f" | {_fmt(_mean(seg, 'mfe_60'))} | {_fmt(_mean(seg, 'mae_60'))} |")
    out.append("")
    return out


def _safe_eval(expr: str, r: dict) -> bool:
    """纯比较表达式求值（仅允许行字段与数值比较）。"""
    names = {k: r.get(k) for k in
             ("ud_ask_delta_30", "relative_obi", "depth_ratio_underdog",
              "slope_30", "slope_60", "momentum_decay",
              "spot_token_divergence_30")}
    if any(ch in expr for ch in ("import", "__", "lambda", "[", "]")):
        return False
    try:
        return bool(eval(expr, {"__builtins__": {}}, names))  # noqa: S307
    except TypeError:  # None 参与比较 → 条件不成立（None=不知道）
        return False


def main() -> int:
    ap = argparse.ArgumentParser(description="b09 复合条件统计（三因子交叉）")
    ap.add_argument("--symbols", default="btc")
    ap.add_argument("--features-dir", default="runtime/features")
    ap.add_argument("--out", default=None, help="markdown 输出路径（缺省打印）")
    args = ap.parse_args()
    lines = [
        "# b09 复合条件统计（Step 4b）\n",
        f"- 数据：{args.features_dir}/*_features_{FEATURES_TAG}.parquet",
        "- 阈值取自单变量分桶分位边界（Q1/Q3）；* = N<384（方向性参考）",
        "- None 参与比较一律不成立（None=不知道，绝不按 0 处理）\n",
    ]
    for sym in [s.strip().lower() for s in args.symbols.split(",") if s.strip()]:
        path = Path(args.features_dir) / f"{sym}_features_{FEATURES_TAG}.parquet"
        if not path.is_file():
            print(f"跳过 {sym}：{path} 不存在")
            continue
        lines.extend(stats(path))
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
