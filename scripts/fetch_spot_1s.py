"""b07 数据底座：Binance 1s K 线批量归档下载 → parquet。

数据源：data.binance.vision 月度归档（免费免鉴权）：
    https://data.binance.vision/data/spot/monthly/klines/{SYM}/1s/{SYM}-1s-{YYYY-MM}.zip

覆盖 HF 数据集窗口 2026-03-24 → 2026-05-18（前后各留 1 天余量，
供滚动 60s TWAP 的窗口前历史）。产出 runtime/hf/{sym}_spot_1s.parquet：
    ts(int64, epoch 秒), open/high/low/close(float64)

用法（仓库根目录）::

    PM_PROXY_URL=http://127.0.0.1:7890 .venv\\Scripts\\python.exe scripts/fetch_spot_1s.py
"""

from __future__ import annotations

import csv as csvmod
import io
import itertools
import os
import sys
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pyarrow as pa
import pyarrow.parquet as pq

BASE = "https://data.binance.vision/data/spot/monthly/klines"
SYMBOLS = {"btc": "BTCUSDT", "eth": "ETHUSDT"}
MONTHS = ["2026-03", "2026-04", "2026-05"]
OUT_DIR = Path("runtime/hf")
ZIP_DIR = Path("runtime/spot_zip")

# 2026-03-23T00:00Z → 2026-05-19T00:00Z（窗口范围 ± 1 天余量）
TS_LO = int(datetime(2026, 3, 23, tzinfo=UTC).timestamp())
TS_HI = int(datetime(2026, 5, 19, tzinfo=UTC).timestamp())


def download(http: httpx.Client, sym: str, month: str, dest: Path) -> bool:
    url = f"{BASE}/{sym}/1s/{sym}-1s-{month}.zip"
    if dest.exists():
        try:
            with zipfile.ZipFile(dest):
                pass  # 打开即读中央目录，截断的残文件会抛 BadZipFile
            print(f"  已存在 {dest.name}（{dest.stat().st_size / 1e6:.1f}MB）")
            return True
        except zipfile.BadZipFile:
            print(f"  残损文件（历史中断残留），删除重下：{dest.name}")
            dest.unlink()
    tmp = dest.with_suffix(".part")
    for attempt in range(1, 4):
        print(f"  下载 {url}（第 {attempt} 次）")
        try:
            with http.stream("GET", url) as r:
                if r.status_code != 200:
                    print(f"  !! HTTP {r.status_code}（该月 1s 月度归档可能不存在）")
                    return False
                with open(tmp, "wb") as f:
                    for chunk in r.iter_bytes(1 << 20):
                        f.write(chunk)
        except httpx.HTTPError as e:
            print(f"  !! 下载中断：{e}")
            tmp.unlink(missing_ok=True)
            continue
        tmp.rename(dest)
        print(f"  → {dest.name} {dest.stat().st_size / 1e6:.1f}MB")
        return True
    print("  !! 3 次重试均失败")
    return False


def parse_zip(zp: Path) -> tuple[list[int], list[float], list[float], list[float], list[float]]:
    """zip 内 CSV → 过滤时间范围的 ts/open/high/low/close 列。"""
    ts_l: list[int] = []
    o_l: list[float] = []
    h_l: list[float] = []
    lo_l: list[float] = []
    c_l: list[float] = []
    with zipfile.ZipFile(zp) as z:
        name = z.namelist()[0]
        with z.open(name) as f:
            reader = csvmod.reader(io.TextIOWrapper(f, encoding="utf-8"))
            first = next(reader)
            rows = reader if first[0].isalpha() else itertools.chain([first], reader)
            for r in rows:
                ot = int(r[0])
                if ot > 10**14:      # 新归档 open_time 为微秒
                    ot //= 1000
                ts = ot // 1000
                if not (TS_LO <= ts < TS_HI):
                    continue
                ts_l.append(ts)
                o_l.append(float(r[1]))
                h_l.append(float(r[2]))
                lo_l.append(float(r[3]))
                c_l.append(float(r[4]))
    return ts_l, o_l, h_l, lo_l, c_l


def main() -> int:
    proxy = os.environ.get("PM_PROXY_URL") or None
    ZIP_DIR.mkdir(parents=True, exist_ok=True)
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    with httpx.Client(timeout=60, proxy=proxy, follow_redirects=True,
                      headers={"User-Agent": "pm-arb-b07/0.1"}) as http:
        for short, sym in SYMBOLS.items():
            all_cols: tuple[list, ...] = ([], [], [], [], [])
            ok = True
            for month in MONTHS:
                zp = ZIP_DIR / f"{sym}-1s-{month}.zip"
                if not download(http, sym, month, zp):
                    ok = False
                    break
                cols = parse_zip(zp)
                print(f"  解析 {zp.name}：范围内 {len(cols[0])} 行")
                for a, b in zip(all_cols, cols, strict=True):
                    a.extend(b)
            if not ok:
                print(f"!! {sym} 数据不完整，跳过")
                continue

            n = len(all_cols[0])
            print(f"{short}: 合计 {n} 行，写 parquet ...")
            order = sorted(range(n), key=all_cols[0].__getitem__)
            tbl = pa.table({
                "ts": pa.array([all_cols[0][i] for i in order], type=pa.int64()),
                "open": pa.array([all_cols[1][i] for i in order], type=pa.float64()),
                "high": pa.array([all_cols[2][i] for i in order], type=pa.float64()),
                "low": pa.array([all_cols[3][i] for i in order], type=pa.float64()),
                "close": pa.array([all_cols[4][i] for i in order], type=pa.float64()),
            })
            out = OUT_DIR / f"{short}_spot_1s.parquet"
            pq.write_table(tbl, out, compression="zstd")
            span = (all_cols[0][order[-1]] - all_cols[0][order[0]]) / 86400
            print(f"  → {out}（{n} 行，{span:.1f} 天，{out.stat().st_size / 1e6:.1f}MB）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
