"""现货波动代理（b07）：Binance 1s K 线 → 滚动 60s TWAP 的窗口内 high-low。

语义对齐实盘 :class:`RtdsTwapFeed`：TWAP-60s 流进入窗口后维护 high/low
（单调不减），price_range = high - low 喂给 decide_entry 的 max_vol 过滤。
回测用 1s K 线收盘价重建每秒 TWAP-60s：

    twap60(t) = mean(close[t-59 .. t])     （近 60 秒简单均值）
    rng(t)    = max(twap60[ws..t]) - min(twap60[ws..t])   （自窗口起点累计）

与实盘的差异（报告必须注明）：
1. Binance 单所现货 ≠ Chainlink 多所聚合，极端行情可能有偏差；
2. 收盘价均值近似 TWAP（真实为成交加权）——1s 粒度下差异可忽略；
3. 个别缺秒用最邻近不晚于 t 的收盘前向填充；覆盖率 < 95% 的窗口
   返回 None（调用方按 ABORT_DATA 放弃入场，与实盘“数据不全”同口径）。
"""

from __future__ import annotations

from decimal import Decimal

import numpy as np
import pyarrow.parquet as pq

COVERAGE_MIN = 0.95


class SpotVol:
    """单币 1s K 线序列（ts 升序），按窗口秒查询 rng（twap60 high-low）。"""

    def __init__(self, symbol: str, parquet_dir: str = "runtime/hf") -> None:
        tbl = pq.read_table(f"{parquet_dir}/{symbol}_spot_1s.parquet",
                            columns=["ts", "close"])
        self._ts = tbl.column("ts").to_numpy()
        self._close = tbl.column("close").to_numpy()
        self._cums = np.concatenate(([0.0], np.cumsum(self._close)))

    def window_rng_seq(self, ws: int, tick_ts: list[int]) -> list[Decimal | None]:
        """各 tick 秒的 rng 序列（与 tick_ts 等长，自 ws 累计 high-low）。

        现货覆盖率不足或窗口前历史缺失 → 全 None（ABORT_DATA 口径）。
        """
        if not tick_ts:
            return []
        lo = int(np.searchsorted(self._ts, ws - 60))
        hi = int(np.searchsorted(self._ts, tick_ts[-1], side="right"))
        ts = self._ts[lo:hi]
        span = tick_ts[-1] - (ws - 60) + 1
        if len(ts) < span * COVERAGE_MIN or (len(ts) and ts[0] > ws - 59):
            return [None] * len(tick_ts)

        cums = self._cums
        out: list[Decimal | None] = []
        hi_twap = -np.inf
        lo_twap = np.inf
        for t in tick_ts:
            # 最近一条不晚于 t 的 K 线索引（缺秒前向填充）
            i = lo + int(np.searchsorted(ts, t, side="right")) - 1
            if i < lo:
                return [None] * len(tick_ts)
            j0 = max(0, i - 59)
            twap = (cums[i + 1] - cums[j0]) / (i - j0 + 1)
            hi_twap = max(hi_twap, twap)
            lo_twap = min(lo_twap, twap)
            out.append(Decimal(str(round(float(hi_twap - lo_twap), 2))))
        return out
