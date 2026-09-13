"""HF 公开数据集回测 CLI（一键：``pm-bt5m``）。

用法（仓库根目录）::

    pm-bt5m                                     # btc+eth 全量
    pm-bt5m --symbols btc --limit 50            # 冒烟（秒出）
    pm-bt5m --param take_profit_price=0.55      # 覆盖任意策略参数

等价模块调用：``python -m pm_arb.strategies.crypto_5m.backtest.run``
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from ..labels import compute_window_labels
from ..params import Crypto5mParams, parse_overrides
from .engine import FeatureCapture, replay_ticks, run_backtest
from .hf_loader import HfDataset
from .report import print_report
from .spot_vol import SpotVol

NOTES = [
    "数据集固定窗口 2026-03-24 → 2026-05-18，非活数据，微结构可能与当前不同；",
    "outcome 为数据集作者按窗口最后 tick bid 推断（非链上结算），结论以"
    " Gamma 交叉核对为准；",
    "--spot 未开：无现货 TWAP，max_vol 波动过滤关闭（rng=0），与实盘行为有差异；",
    "撮合保守：入场/止盈均要求档深 ≥ 份数，否则放弃/继续持有。",
]

NOTES_SPOT = [
    NOTES[0],
    NOTES[1],
    "现货波动为 Binance 1s K 线重建的滚动 60s TWAP high-low（收盘价均值近似；"
    "单所现货 ≠ Chainlink 多所聚合，极端行情可能有偏差）；",
    NOTES[3],
]


def parse_overrides_cli(items: list[str] | None) -> Crypto5mParams:
    """--param k=v 覆盖默认参数（解析逻辑在 params.parse_overrides）。"""
    return Crypto5mParams().model_copy(update=parse_overrides(items))


FEATURES_TAG = "hf_20260324_20260518"  # 数据集固定范围，写进文件名留痕


def capture_features(ds: HfDataset, p: Crypto5mParams, spot: SpotVol,
                      *, limit: int | None, out_dir: str,
                      ) -> tuple[Path, int, int, int]:
    """特征采集主流程：逐窗口回放 + Label 附加 + parquet 落盘。

    返回 (落盘路径, 行数, 窗口数, 入场行数)。Label 在窗口回放完后
    就地附加（前视信息只进 labels.py）；行间字段不齐（Label 缺失行）
    统一补 None 后落盘。无现货数据时 twap 全 None → Trend 特征为
    None（与实盘“喂价缺失”同一语义，绝不造 0）。
    """
    cap = FeatureCapture(symbol=ds.symbol)
    markets = ds.markets if limit is None else ds.markets[:limit]
    n_windows = 0
    for mkt in markets:
        if mkt.outcome not in ("Up", "Down"):
            continue
        if not ds.has_ticks(mkt.condition_id):
            continue
        ticks = ds.window_ticks(mkt.condition_id)
        tick_ts = [t["t"] for t in ticks]
        twap_seq = spot.window_twap_seq(mkt.start, tick_ts)
        rng_seq = spot.window_rng_seq(mkt.start, tick_ts)
        n0 = len(cap.rows)
        replay_ticks(ticks, mkt, p, rng_seq=rng_seq, twap_seq=twap_seq,
                     capture=cap)
        compute_window_labels(cap.rows[n0:], ticks, mkt)
        n_windows += 1
    # 行间字段对齐（Label 条件性附加 → 缺失补 None）
    keys: list[str] = []
    for r in cap.rows:
        for k in r:
            if k not in keys:
                keys.append(k)
    rows = [{**{k: None for k in keys}, **r} for r in cap.rows]
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{ds.symbol}_features_{FEATURES_TAG}.parquet"
    pq.write_table(pa.Table.from_pylist(rows), path)
    n_entered = sum(1 for r in cap.rows if r["entered"])
    return path, len(cap.rows), n_windows, n_entered


def main() -> int:
    ap = argparse.ArgumentParser(description="HF 数据集 5m Up/Down 回测")
    ap.add_argument("--symbols", default="btc,eth", help="逗号分隔币种")
    ap.add_argument("--parquet-dir", default="runtime/hf")
    ap.add_argument("--limit", type=int, default=None, help="每币只回放前 N 窗口（冒烟）")
    ap.add_argument("--param", action="append", default=None,
                    metavar="K=V", help="覆盖策略参数（可多次）")
    ap.add_argument("--spot", action="store_true",
                    help="启用 b07 现货波动（需 runtime/hf/{sym}_spot_1s.parquet）")
    ap.add_argument("--capture-features", action="store_true",
                    help="b09 特征采集：逐 tick 特征+评分+Label 落 parquet（研究用，"
                         "不影响回测判定；建议与 --spot 同开否则 Trend 特征为 None）")
    ap.add_argument("--features-dir", default="runtime/features",
                    help="特征 parquet 输出目录")
    args = ap.parse_args()

    p = parse_overrides_cli(args.param)
    print(f"参数: {p.model_dump()}")
    for sym in [s.strip().lower() for s in args.symbols.split(",") if s.strip()]:
        ds = HfDataset(sym, args.parquet_dir)
        spot = SpotVol(sym, args.parquet_dir) if args.spot else None
        skipped = sum(1 for m in ds.markets if m.outcome not in ("Up", "Down"))
        if args.capture_features:
            assert spot is not None, "capture 需 --spot（现货 TWAP 序列）"
            path, n_rows, n_win, n_ent = capture_features(
                ds, p, spot, limit=args.limit, out_dir=args.features_dir)
            print(f"[{sym}] 特征已落盘: {path}")
            print(f"  窗口 {n_win}，采集行 {n_rows}，入场行 {n_ent}")
            continue
        results = run_backtest(ds, p, limit=args.limit, spot=spot)
        print_report(results, skipped_no_outcome=skipped,
                     notes=NOTES_SPOT if args.spot else NOTES)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
