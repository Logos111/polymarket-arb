"""Crypto 5m Up/Down 策略参数（阶段 1 commit A：常量 → 可配置）。

单一决策来源的一部分：实盘 orchestrator 与回测引擎共用同一份参数对象，
杜绝"回测参数与实盘漂移"。后续接 CLI ``--param k=v`` 覆盖。
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, ConfigDict


class Crypto5mParams(BaseModel):
    """策略参数（默认值 = 09-11/12 实盘验证过的现行值）。"""

    model_config = ConfigDict(frozen=True)

    target_notional: Decimal = Decimal("2.00")  # 单笔名义金额（美元）
    entry_after: int = 70                       # 开窗后 70s 起可入场（剩余 3:50）
    entry_until: int = 135                      # 开窗后 135s 后不再入场（剩余 2:45）
    take_profit_price: Decimal = Decimal("0.99")  # 固定止盈价（与入场价无关）
    max_entry: Decimal = Decimal("0.30")        # 冷门方入场价上限（30 点）
    min_entry: Decimal = Decimal("0.15")        # 入场价下限：过冷说明市场已大致
                                                # 定局，买入近乎拾彩票，不入场
    max_vol: Decimal = Decimal("30")            # 本窗口 TWAP 波动上限（USD）
    poll: float = 2.0                           # 监测轮询间隔（秒）
    ws_fresh_sec: float = 10.0                  # WS 本地簿新鲜度阈值：超龄回退 REST
    feed_fresh_sec: float = 15.0                # RTDS TWAP 新鲜度阈值：超龄不可信
    end_margin: int = 60                        # 结算前 N 秒停止操作
