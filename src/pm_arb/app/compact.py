"""pm-compact：把已封口的 ticks market JSONL 压实为分区 Parquet。

用法（Python 3.14 的 duckdb cp314 wheel DLL 损坏，须用 3.12 跑）::

    uv run --no-project --python 3.12 --no-cache \\
        --with duckdb --with . pm-compact            # 全部已封口文件
    uv run --no-project --python 3.12 --no-cache \\
        --with duckdb --with . pm-compact --keep     # 只转换不删源
    ... pm-compact --file runtime/ticks/2026-09-12_market.jsonl --keep

默认行为：扫描 --src 下 ``*_market.jsonl``，跳过今日（UTC）文件；
行数校验通过且无坏行后删除源 JSONL（--keep 只转不删）。
"""

from __future__ import annotations

import argparse
from pathlib import Path

from pm_arb.data.ticks_compact import compact_dir, compact_file


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="ticks market JSONL → 分区 Parquet 压实（校验后删源）",
    )
    ap.add_argument("--src", default="runtime/ticks", help="JSONL 目录")
    ap.add_argument("--dst", default="runtime/ticks_parquet", help="Parquet 输出目录")
    ap.add_argument("--file", default=None, help="只处理指定文件（可配合 --keep）")
    ap.add_argument("--keep", action="store_true", help="只转换不删源")
    ap.add_argument("--force", action="store_true", help="目标已存在时重做")
    ap.add_argument("--include-today", action="store_true",
                    help="包含今日 UTC 文件（须先停录）")
    args = ap.parse_args(argv)

    if args.file:
        src = Path(args.file)
        if not src.exists():
            ap.error(f"文件不存在: {src}")
        results = [r for r in [
            compact_file(src, Path(args.dst), keep=args.keep, force=args.force),
        ] if r is not None]
        if not results:
            print("目标已存在，跳过（--force 重做）")
    else:
        results = compact_dir(
            args.src, args.dst, keep=args.keep, force=args.force,
            include_today=args.include_today,
        )
        if not results:
            print("没有待压实的已封口文件")

    for r in results:
        status = "OK" if r.verified else "MISMATCH"
        act = "deleted-src" if r.deleted else ("kept-src" if r.verified else "kept-src(BAD)")
        print(
            f"[{status}] {r.src.name}: lines={r.n_lines} bad={r.n_bad} "
            f"book={r.n_book_rows} meta={r.n_meta_rows} {act}"
        )
    return 0 if all(r.verified for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
