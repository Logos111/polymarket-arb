"""HF 公开数据集加载器（kachoio/polymarket-5-minute-crypto-up-down-markets）。

数据集结构（每币一对 parquet）：
- ``{sym}_markets.parquet``：每窗口一行，condition_id / slug / market_start /
  market_end / token_up / token_down / volume / liquidity / outcome / n_ticks；
- ``{sym}_ticks.parquet``：每秒一条盘口快照（1Hz，每窗口 300 条），
  列 bu/au（Up bid/ask）、bd/ad（Down bid/ask）、su/sd（Up/Down bid 档深）、
  sau/sad（Up/Down ask 档深）、du/dd（Up/Down 美元深度）。

限定（使用时必须牢记）：
1. 固定历史窗口 2026-03-24 → 2026-05-18，非活数据；
2. ``outcome`` 是数据集作者用窗口最后 tick 的 bid 推断的，非链上/Gamma
   结算结果，边缘窗口为 null——正式下结论前须抽样与 Gamma 交叉核对；
3. 无现货 TWAP，策略的 max_vol 波动过滤在本数据上不可计算（引擎以
   rng=0 跳过，报表必须注明）。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

import pyarrow.parquet as pq

TICK_COLS = ["condition_id", "t", "bu", "au", "bd", "ad", "su", "sd", "sau", "sad"]


@dataclass(frozen=True)
class HfMarket:
    """markets 表一行。"""

    condition_id: str
    slug: str
    start: int              # 窗口起点 epoch 秒
    outcome: str | None     # "Up"/"Down"/None（推断值）
    volume: float
    liquidity: float


class HfDataset:
    """单币 parquet 数据集：markets 全量内存 + ticks 按窗口切片访问。"""

    def __init__(self, symbol: str, parquet_dir: str = "runtime/hf") -> None:
        self.symbol = symbol
        mkt_tbl = pq.read_table(f"{parquet_dir}/{symbol}_markets.parquet")
        self.markets: list[HfMarket] = [
            HfMarket(
                condition_id=r["condition_id"],
                slug=r["slug"],
                start=int(r["market_start"].timestamp()),
                outcome=r["outcome"],
                volume=r["volume"],
                liquidity=r["liquidity"],
            )
            for r in mkt_tbl.to_pylist()
        ]

        # ticks：只读需要的列；condition_id 字典化省内存，预建每窗口行区间
        tk = pq.read_table(f"{parquet_dir}/{symbol}_ticks.parquet", columns=TICK_COLS)
        col = tk.column("condition_id").combine_chunks()
        d = col.dictionary_encode()
        idx = d.indices.to_numpy(zero_copy_only=False)
        cid_dict = [str(v) for v in d.dictionary.to_pylist()]
        # 快照若已按 condition_id 分组连续（数据集实测如此）则直接用；
        # 否则重排一次
        ordered = bool((idx[1:] >= idx[:-1]).all()) if len(idx) else True
        # 字典序与出现顺序等价性：字典按首次出现顺序编号，连续分组时
        # indices 非降 ⇔ condition_id 分组连续。若非降不成立，重排。
        if not ordered:
            tk = tk.sort_by("condition_id")
            col = tk.column("condition_id").combine_chunks()
            d = col.dictionary_encode()
            cid_dict = [str(v) for v in d.dictionary.to_pylist()]
            idx = d.indices.to_numpy(zero_copy_only=False)

        self._t = tk.column("t").to_numpy()
        self._cols = {
            name: tk.column(name).to_numpy()
            for name in TICK_COLS[2:]
        }
        # 每个唯一 condition_id 的连续行区间 [lo, hi)
        self._ranges: dict[str, tuple[int, int]] = {}
        n = len(idx)
        if n:
            cur = int(idx[0])
            lo = 0
            for i in range(1, n):
                c = int(idx[i])
                if c != cur:
                    self._ranges[cid_dict[cur]] = (lo, i)
                    cur = c
                    lo = i
            self._ranges[cid_dict[cur]] = (lo, n)

    def __len__(self) -> int:
        return len(self.markets)

    def has_ticks(self, condition_id: str) -> bool:
        return condition_id in self._ranges

    def window_ticks(self, condition_id: str) -> list[dict]:
        """单窗口 tick 快照列表（按时间升序）。"""
        lo, hi = self._ranges[condition_id]
        t = self._t[lo:hi]
        cols = {k: v[lo:hi] for k, v in self._cols.items()}
        return [
            {
                "t": int(t[j]),
                **{k: float(cols[k][j]) for k in cols},
            }
            for j in range(hi - lo)
        ]


def _bad(x: float | None) -> bool:
    """None/NaN/非正数均视为无值（数据集缺档时用 NaN 填充）。"""
    return x is None or x != x or x <= 0


def d2(x: float | None) -> Decimal | None:
    """float 价格 → Decimal（0.01 步长，缓存常用值避免 8M 次构造）。"""
    if _bad(x):
        return None
    key = round(x, 2)
    d = _PRICE_CACHE.get(key)
    if d is None:
        d = Decimal(f"{key:.2f}")
        _PRICE_CACHE[key] = d
    return d


_PRICE_CACHE: dict[float, Decimal] = {}


def s2(x: float | None) -> int:
    """float 档深 → int 份数；None/NaN/负值视同 0。"""
    if _bad(x):
        return 0
    return int(x)
