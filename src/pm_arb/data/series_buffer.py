"""通用有界时间序列缓冲（b09 特征工程基建；纯数据结构，无 I/O、无时钟）。

承载"现货 TWAP 序列"或"token best_ask/best_bid 序列"，为
features.py 的趋势/动量类特征提供统一输入源。调用方负责注入时间戳：

- 实盘：窗口内 elapsed 秒（orchestrator 每次 poll 后 push）；
- 回测：``tick["t"] - market_start``（engine replay 循环逐 tick push）。

实盘/回测**同一个类两处调用**，杜绝"实盘一套、回测重新算一套"的逻辑分叉。

None 语义纪律（工程落地方案 §2.1，最高优先级）：
    任何一个查询，若所需的历史点不存在或距今太久（喂价心跳卡死），
    一律返回 ``None`` = "不知道"。调用方**不得**把 None 当 0 处理——
    0 意味着"确认走平"，None 意味着"数据不可用"，混淆会直接导致
    喂价卡死时产生虚假的反转信号（卡死的喂价与真正走平的行情在
    斜率计算上不可区分，这是 Chainlink 心跳问题的本质）。

内部以 float 存储（省内存、回归计算快），对外输出 Decimal（与项目
价格纪律一致）。
"""

from __future__ import annotations

from collections import deque
from decimal import Decimal
from itertools import islice
from typing import Literal


def _d(x: float) -> Decimal:
    return Decimal(str(round(x, 10)))


class SeriesBuffer:
    """单序列滚动缓冲：按时间戳 push，按时间窗查询。"""

    def __init__(self, maxlen_sec: float = 180.0) -> None:
        self._maxlen = maxlen_sec
        # (t, v) 升序；t 由调用方保证单调不减（窗口 elapsed / tick t）
        self._d: deque[tuple[float, float]] = deque()

    def __len__(self) -> int:
        return len(self._d)

    def push(self, t: float, v: Decimal | float | None) -> None:
        """追加一个采样点；v=None 跳过（缺值不占坑）；淘汰超 maxlen 旧数据。"""
        if v is None:
            return
        self._d.append((float(t), float(v)))
        cutoff = float(t) - self._maxlen
        d = self._d
        while d and d[0][0] < cutoff:
            d.popleft()

    # ---- 点查询 ----

    def value_asof(self, t: float, max_age: float | None = None) -> Decimal | None:
        """不晚于 t 的最近一个值；不存在或超龄（数据太老）返回 None。"""
        d = self._d
        if not d or d[0][0] > t:
            return None
        # 二分找最后一个 t_i <= t
        lo, hi = 0, len(d)
        while lo < hi:
            mid = (lo + hi) // 2
            if d[mid][0] <= t:
                lo = mid + 1
            else:
                hi = mid
        ti, v = d[lo - 1]
        if max_age is not None and t - ti > max_age:
            return None
        return _d(v)

    def sample_count(self, t: float, window_sec: float) -> int:
        """(t-window_sec, t] 内有效采样点数——喂价新鲜度判定的一等公民输入。"""
        lo_t = t - window_sec
        d = self._d
        lo, hi = 0, len(d)
        while lo < hi:  # 第一个 > lo_t 的索引
            mid = (lo + hi) // 2
            if d[mid][0] <= lo_t:
                lo = mid + 1
            else:
                hi = mid
        first = lo
        lo, hi = first, len(d)
        while lo < hi:  # 第一个 > t 的索引
            mid = (lo + hi) // 2
            if d[mid][0] <= t:
                lo = mid + 1
            else:
                hi = mid
        return lo - first

    # ---- 派生特征（全部容忍缺数据：不足 → None）----

    def ret(self, t: float, seconds_ago: float, *,
            max_age: float = 5.0) -> Decimal | None:
        """(v(t) - v(t-seconds_ago)) / v(t-seconds_ago)；任一端缺失/超龄 → None。"""
        v_now = self.value_asof(t, max_age=max_age)
        if v_now is None:
            return None
        v_ago = self.value_asof(t - seconds_ago, max_age=max_age)
        if v_ago is None or v_ago == 0:
            return None
        return _d((float(v_now) - float(v_ago)) / float(v_ago))

    def slope(self, t: float, window_sec: float, *,
              min_samples: int = 3) -> Decimal | None:
        """窗口 (t-window_sec, t] 内最小二乘 价格~时间 斜率（每秒）。

        样本不足 ``min_samples`` 返回 None（数据不可用 ≠ 斜率为 0）。
        缺秒不插值：用窗口内实际采样点做回归（1Hz 下与理想等价，
        喂价稀疏时斜率可信度由调用方结合 sample_count 判定）。
        """
        d = self._d
        n = len(d)
        if n == 0:
            return None
        lo_t = t - window_sec
        # 窗口起点索引（第一个 > lo_t）
        lo, hi = 0, n
        while lo < hi:
            mid = (lo + hi) // 2
            if d[mid][0] <= lo_t:
                lo = mid + 1
            else:
                hi = mid
        pts = list(islice(d, lo, None))
        k = len(pts)
        if k < min_samples:
            return None
        # 最小二乘（相对 t 平移防大数精度损失）
        st = sum(p[0] - t for p in pts)
        sv = sum(p[1] for p in pts)
        stt = sum((p[0] - t) ** 2 for p in pts)
        stv = sum((p[0] - t) * p[1] for p in pts)
        denom = k * stt - st * st
        if denom == 0:
            return None
        return _d((k * stv - st * sv) / denom)

    def new_extreme_count(self, t: float, window_sec: float,
                          kind: Literal["high", "low"]) -> int | None:
        """窗口内创新高/新低次数：首点为基准，之后每突破一次计 1 并更新。

        窗口内无样本 → None；仅 1 个样本 → 0（确定不构成创新）。
        """
        d = self._d
        if not d:
            return None
        lo_t = t - window_sec
        pts = [(ti, v) for ti, v in d if ti > lo_t and ti <= t]
        if not pts:
            return None
        if kind == "high":
            best = pts[0][1]
            cnt = 0
            for _, v in pts[1:]:
                if v > best:
                    best = v
                    cnt += 1
        else:
            best = pts[0][1]
            cnt = 0
            for _, v in pts[1:]:
                if v < best:
                    best = v
                    cnt += 1
        return cnt

    def extremes(self) -> tuple[Decimal, Decimal] | None:
        """缓冲内（≈自窗口起点累计的）(high, low)；缓冲空 → None。"""
        if not self._d:
            return None
        vs = [v for _, v in self._d]
        return _d(max(vs)), _d(min(vs))

    def time_since_extreme(self, t: float,
                           kind: Literal["high", "low"]) -> float | None:
        """距缓冲（≈窗口起点至今）内极值点已过去的秒数；缓冲空 → None。"""
        d = self._d
        if not d:
            return None
        if kind == "high":
            ti, _ = max(d, key=lambda p: p[1])
        else:
            ti, _ = min(d, key=lambda p: p[1])
        return t - ti

