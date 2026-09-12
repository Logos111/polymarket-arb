"""Crypto 5m 纯决策函数（阶段 1 commit A：唯一决策来源）。

实盘 orchestrator 与回测引擎必须调同一套函数——防两套逻辑漂移。
本模块只做判定，不做 I/O、不读时钟、不下单：

- :func:`pick_underdog` — 冷门方（更便宜一边）选择；
- :func:`calc_size` — 目标名义 → 份数；
- :func:`decide_entry` — 入场判定（时间窗 + 波动过滤 + 价格区间）；
- :func:`decide_exit` — 止盈判定。

文案（status/log）与现行 trade5m 日志逐字对齐，保证行为不变。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from pm_arb.strategies.crypto_5m.params import Crypto5mParams


def fmt_price(d: Decimal | None, w: int = 6) -> str:
    """价格展示格式（与现行 TUI/日志逐字一致）。"""
    return f"{d:.3f}".rjust(w) if d is not None else "n/a".rjust(w)


def pick_underdog(b: dict[str, dict]) -> tuple[str, Decimal | None]:
    """返回 (side_name, best_ask) —— 更便宜的一方。"""
    up_ask = b["Up"].get("best_ask")
    down_ask = b["Down"].get("best_ask")
    if up_ask is None and down_ask is None:
        return "Up", None
    if down_ask is None or (up_ask is not None and up_ask <= down_ask):
        return "Up", up_ask
    return "Down", down_ask


def calc_size(ask: Decimal, min_size: int, target_notional: Decimal) -> int:
    """目标名义金额 → 份数（向上取整，且不低于市场最小下单份数）。"""
    return max(math.ceil(target_notional / ask), min_size)


class EntryAction(StrEnum):
    """入场判定结果。"""

    WAIT = "wait"            # 未进入入场时间窗
    MISSED = "missed"        # 已错过入场时间窗（本窗口不再判定）
    ABORT_DATA = "abort_data"  # 数据不全，不可恢复放弃
    ABORT_VOL = "abort_vol"    # 波动超限（high-low 单调不减），不可恢复放弃
    OBSERVE = "observe"        # ask 越界观察（ask 回落区间内可重新判定）
    ENTER = "enter"            # 信号成立，市价买入


@dataclass(frozen=True)
class EntryDecision:
    """decide_entry 的结果；status/log 与现行 TUI/日志文案逐字一致。"""

    action: EntryAction
    status: str
    log: str = ""     # event 日志文案（仅 ABORT/OBSERVE/ENTER 有意义）
    size: int = 0     # ENTER 时的目标份数


def decide_entry(
    elapsed: float,
    cand: str,
    ask: Decimal | None,
    ask_size: int | None,
    rng: Decimal | None,
    p: Crypto5mParams,
    *,
    min_size: int = 5,
) -> EntryDecision:
    """入场判定（纯函数）。

    时间窗语义（与现行实现一致）：
    - ``elapsed < entry_after``：WAIT（窗口未开）；
    - ``elapsed > entry_until``：MISSED（已错过，等价现行"已错过"）；
    - 窗口内：波动过滤一旦超限即永久拒绝（调用方据此置 entry_done），
      ask 越界仅观察——ask 回落到 (min_entry, max_entry) 可重新判定。

    ``rng`` 是窗口 TWAP high-low（单调不减）；``cand``/``ask_size`` 仅供
    文案（冷门方名/档深），不影响判定。
    """
    if elapsed < p.entry_after:
        return EntryDecision(EntryAction.WAIT, f"等待 t∈[{p.entry_after},{p.entry_until}]")
    if elapsed > p.entry_until:
        return EntryDecision(EntryAction.MISSED, "已错过")
    if ask is None or rng is None:
        return EntryDecision(
            EntryAction.ABORT_DATA, "放弃(数据不全)",
            log=f"[入场检查] 数据不全（ask={ask} range={rng}），放弃。")
    if rng >= p.max_vol:
        return EntryDecision(
            EntryAction.ABORT_VOL, f"放弃(波动${rng:.0f})",
            log=f"[入场检查] ❌ 波动 ${rng:.2f} ≥ ${p.max_vol}，放弃。")
    if ask >= p.max_entry:
        return EntryDecision(
            EntryAction.OBSERVE, f"观察(ask{ask}≥{p.max_entry})",
            log=f"[入场检查] … {cand} ask={fmt_price(ask)} ≥ {p.max_entry}，观察")
    if ask <= p.min_entry:
        # 过冷不入场：市场已大致定局，冷门方近乎彩票
        return EntryDecision(
            EntryAction.OBSERVE, f"观察(ask{ask}≤{p.min_entry}过冷)",
            log=f"[入场检查] … {cand} ask={fmt_price(ask)} ≤ {p.min_entry}，过冷观察")
    size = calc_size(ask, min_size, p.target_notional)
    depth = (f"（档深{ask_size:.0f}份）" if ask_size is not None else "（档深未知）")
    return EntryDecision(
        EntryAction.ENTER, "判定中",
        log=f"[入场检查] ✅ {cand} ask={fmt_price(ask)}{depth} 波动${rng:.2f}"
            f" → 市价买 ≈ ${p.target_notional:.2f}，止盈 {fmt_price(p.take_profit_price)}",
        size=size,
    )


def decide_exit(bid: Decimal | None, p: Crypto5mParams) -> bool:
    """止盈判定：best_bid 达到固定止盈价即卖出（与入场价无关）。"""
    return bid is not None and bid >= p.take_profit_price
