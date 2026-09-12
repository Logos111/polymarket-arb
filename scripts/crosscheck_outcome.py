"""HF 数据集 outcome 与 Gamma 真实结算交叉核对（结论可信度验证）。

数据集限定：outcome 是作者用窗口最后 tick bid 推断的 best-effort 值。
本脚本抽样查询 Gamma（closed=true）的 outcomePrices 真实结算，
统计推断准确率——准确率不达标则回测胜率全部存疑。

用法（仓库根目录）::

    python scripts/crosscheck_outcome.py --symbol btc --sample 30
"""

from __future__ import annotations

import argparse
import asyncio
import random

from pm_arb.backtest.hf_loader import HfDataset
from pm_arb.data.gamma import GammaClient


def gamma_winner(prices: list[str], outcomes: list[str]) -> str | None:
    """outcomePrices=['1','0'] 中为 1 的一方即结算赢家。"""
    pairs = list(zip(outcomes, prices, strict=True))
    for out, p in pairs:
        if p in ("1", "1.0", "0.999"):
            return out
    return None


async def run(symbol: str, sample: int, parquet_dir: str) -> int:
    ds = HfDataset(symbol, parquet_dir)
    valid = [m for m in ds.markets if m.outcome in ("Up", "Down")]
    picks = random.Random(42).sample(valid, min(sample, len(valid)))
    print(f"抽样 {len(picks)} / 有效 {len(valid)} / 全部 {len(ds.markets)}")

    agree = disagree = missing = 0
    async with GammaClient() as gamma:
        for m in picks:
            g = await gamma.get_market_by_slug(m.slug, closed=True)
            if g is None or not g.outcome_prices:
                missing += 1
                print(f"  {m.slug}: Gamma 无结果")
                continue
            gw = gamma_winner(g.outcome_prices, g.outcomes)
            if gw == m.outcome:
                agree += 1
            else:
                disagree += 1
                print(f"  {m.slug}: 数据集={m.outcome} Gamma={gw} 不一致")
    total = agree + disagree
    print(f"\n一致 {agree} / 不一致 {disagree} / 查无 {missing}")
    if total:
        print(f"推断准确率: {agree / total:.1%}")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="btc")
    ap.add_argument("--sample", type=int, default=30)
    ap.add_argument("--parquet-dir", default="runtime/hf")
    a = ap.parse_args()
    raise SystemExit(asyncio.run(run(a.symbol, a.sample, a.parquet_dir)))
