"""风控闸门（DEV_PLAN 阶段 4 最小集）：live 下单前统一过闸。

与 decisions.py 同一设计纪律：检查逻辑为纯函数（无 I/O、无时钟、
无数据库），实盘/paper/回测可复用同一套规则；I/O 聚合（当日统计、
余额查询、Kill 文件探测）集中在 :class:`RiskGate` 装配层。

检查项（按拒绝优先级排序，首个命中即返回）：

1. **Kill Switch**（4.4）：``runtime/KILL`` 文件存在 → 全部拒绝新仓。
   落文件即生效，无需重启（拍腿应急开关）。
2. **单笔名义上限**（4.1）：防参数误配放大仓位。
3. **同窗口重复入场**（4.2）：防回执异常导致的重复下单（崩溃恢复后
   同窗口已有 open 持仓时拒绝）。
4. **单日累计投入上限**（4.1）：当日已入场成本 + 本次名义超限拒绝。
5. **单日已实现亏损熔断**（4.1）：当日已实现盈亏（含结算回填）亏损
   达到阈值 → 当日剩余时间熔断（UTC 日界自动解除）。
6. **USDC 余额检查**（4.2）：余额不足拒绝；**余额查询失败也拒绝**
   （fail-closed：真金场景无法确认余额 ≈ 不该下单）。仅 live 生效。
7. **POL gas 告警**（4.3）：仅告警不阻断（gas 只影响 redeem，不影响
   CLOB 下单）；低于阈值时提醒补充，防"已实现盈利挂在链上赎不回"。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

DAY_SECONDS = 86_400


def utc_day_start(now_ts: int) -> int:
    """当日 UTC 零点（单日限额/熔断的滚动边界）。"""
    return now_ts // DAY_SECONDS * DAY_SECONDS


# ---- 限额配置 ----


@dataclass(frozen=True)
class RiskLimits:
    """闸门阈值（来自 Settings 风控字段，测试可显式构造）。"""

    max_notional: Decimal = Decimal("5.00")          # 单笔名义上限 $
    max_daily_notional: Decimal = Decimal("20.00")   # 单日累计投入上限 $
    max_daily_loss: Decimal = Decimal("10.00")       # 单日已实现亏损熔断线 $
    min_usdc_buffer: Decimal = Decimal("1.00")       # 余额须 ≥ 名义 + buffer
    min_gas_warn: Decimal = Decimal("1.00")          # POL 余额告警阈值


# ---- 检查输入/输出 ----


@dataclass(frozen=True)
class GateContext:
    """一次过闸所需的全部状态（装配层聚合，纯函数只读）。"""

    now_ts: int
    live: bool = False
    kill_switch: bool = False
    invested_today: Decimal = Decimal(0)     # 当日已入场成本合计（全币种）
    realized_today: Decimal = Decimal(0)     # 当日已实现盈亏（含结算，负=亏）
    usdc_balance: Decimal | None = None      # None=未查询/查询失败
    same_window_open: bool = False           # 本窗口已有 open 持仓


@dataclass(frozen=True)
class GateVerdict:
    """过闸结论。``ok=False`` 时 ``code`` 供日志/落库区分拒绝原因。"""

    ok: bool
    code: str = "PASS"
    message: str = ""


def _reject(code: str, message: str) -> GateVerdict:
    return GateVerdict(ok=False, code=code, message=message)


def check_order(
    notional: Decimal, ctx: GateContext, limits: RiskLimits,
) -> GateVerdict:
    """入场名义过闸：纯函数，按 docstring 优先级逐项检查。"""
    if ctx.kill_switch:
        return _reject("KILL_SWITCH", "Kill Switch 生效（runtime/KILL 存在），拒绝新仓")
    if notional > limits.max_notional:
        return _reject(
            "MAX_NOTIONAL",
            f"单笔名义 ${notional} 超上限 ${limits.max_notional}",
        )
    if ctx.same_window_open:
        return _reject("DUP_WINDOW", "本窗口已有未平仓持仓，拒绝重复入场")
    if ctx.invested_today + notional > limits.max_daily_notional:
        return _reject(
            "DAILY_CAP",
            f"单日投入 ${ctx.invested_today} + 本次 ${notional}"
            f" 超上限 ${limits.max_daily_notional}",
        )
    if ctx.realized_today <= -limits.max_daily_loss:
        day = datetime.fromtimestamp(ctx.now_ts, tz=UTC).strftime("%Y-%m-%d")
        return _reject(
            "CIRCUIT_BREAK",
            f"单日已实现亏损 ${-ctx.realized_today} 达熔断线 "
            f"${limits.max_daily_loss}，{day}（UTC）剩余时间停止开仓",
        )
    if ctx.live:
        if ctx.usdc_balance is None:
            return _reject("BALANCE_UNKNOWN", "USDC 余额查询失败（fail-closed），拒绝下单")
        need = notional + limits.min_usdc_buffer
        if ctx.usdc_balance < need:
            return _reject(
                "BALANCE_LOW",
                f"USDC 余额 ${ctx.usdc_balance} < 名义+buffer ${need}",
            )
    return GateVerdict(ok=True)


def gas_warning(gas_balance: Decimal | None, limits: RiskLimits) -> str | None:
    """POL gas 余额告警文案（None=未查询）。仅告警，不阻断下单。"""
    if gas_balance is None:
        return None
    if gas_balance < limits.min_gas_warn:
        return (f"⚠️ POL gas 余额 {gas_balance:.4f} 低于告警线 "
                f"{limits.min_gas_warn}——赢单赎回（redeem）会失败，请补充 gas。")
    return None


def kill_switch_active(kill_path: str | Path) -> bool:
    """Kill Switch 探测：文件存在即生效（内容忽略）。"""
    return Path(kill_path).exists()


# ---- 装配层：把 I/O 聚合成 GateContext ----


@dataclass
class RiskGate:
    """下单前闸门装配：聚合 store 统计 / 余额 / Kill 文件后过闸。

    所有依赖都可注入（回测传 None 即全跳过 I/O）；
    ``balance_fn``/``gas_fn`` 为 async（CLOB/链上查询走 to_thread）。
    """

    limits: RiskLimits
    kill_path: str | Path = "runtime/KILL"
    live: bool = False
    day_stats_fn: Callable[[int], tuple[Decimal, Decimal]] | None = None
    balance_fn: Callable[[], Awaitable[Decimal | None]] | None = None
    gas_fn: Callable[[], Awaitable[Decimal | None]] | None = None
    window_open_fn: Callable[[int], bool] | None = None
    _last_gas: Decimal | None = field(default=None, repr=False)

    async def check(self, notional: Decimal, now_ts: int,
                    window_start: int | None = None) -> GateVerdict:
        invested, realized = (
            self.day_stats_fn(utc_day_start(now_ts))
            if self.day_stats_fn is not None else (Decimal(0), Decimal(0))
        )
        usdc: Decimal | None = None
        if self.live and self.balance_fn is not None:
            try:
                usdc = await self.balance_fn()
            except Exception:
                # fail-closed：查询异常与返回 None 同样拒绝（真金场景无法
                # 确认余额 ≈ 不该下单）
                usdc = None
        ctx = GateContext(
            now_ts=now_ts,
            live=self.live,
            kill_switch=kill_switch_active(self.kill_path),
            invested_today=invested,
            realized_today=realized,
            usdc_balance=usdc,
            same_window_open=(
                self.window_open_fn(window_start)
                if (self.window_open_fn is not None and window_start is not None)
                else False
            ),
        )
        return check_order(notional, ctx, self.limits)

    async def gas_warning(self) -> str | None:
        """每窗口调用一次（缓存结果，轮询内不重复上链查询）。"""
        if self.gas_fn is None:
            return None
        if self._last_gas is None:
            self._last_gas = await self.gas_fn()
        return gas_warning(self._last_gas, self.limits)
