"""BTC/ETH 5 分钟 Up/Down 多笔方向性交易——瘦 CLI（阶段 1 commit C）。

策略规则见 [strategies/crypto_5m/decisions.py]（唯一决策来源）与
[strategies/crypto_5m/params.py]（单一参数来源）；窗口生命周期编排见
[strategies/crypto_5m/orchestrator.py]；下单路径为 Broker 协议
（live=ClobBroker / dry-run=PaperRunner，见 execution/broker.py）。

用法::

    PYTHONPATH=src python -m pm_arb.app.trade5m --symbol btc --dry-run
    PYTHONPATH=src python -m pm_arb.app.trade5m --symbol btc           # 真实下单
    PYTHONPATH=src python -m pm_arb.app.trade5m --symbol btc --now     # 调试：不等待窗口起点
    pm-trade5m --param take_profit_price=0.80                          # 覆盖任意策略参数

风控（阶段 4）：下单前统一过闸（限额/熔断/重复入场/余额 fail-closed）；
runtime/KILL 文件存在即拒绝新仓并停止进程（拍腿 Kill Switch）。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from decimal import Decimal

from pm_arb.app.tui5m import SessionLog
from pm_arb.execution.chain import ChainClient
from pm_arb.execution.clob_broker import ClobBroker
from pm_arb.execution.clob_trader import ClobTrader
from pm_arb.execution.paper_runner import PaperRunner
from pm_arb.infra.config import get_settings
from pm_arb.infra.logging import setup_logging
from pm_arb.infra.store import Store
from pm_arb.risk.gates import RiskGate, RiskLimits
from pm_arb.strategies.crypto_5m.orchestrator import WindowOrchestrator
from pm_arb.strategies.crypto_5m.params import Crypto5mParams, parse_overrides

DB_PATH = "runtime/trade5m.sqlite3"


def main() -> int:
    parser = argparse.ArgumentParser(description="5min 加密单笔交易（冷门方 +100% 止盈）")
    parser.add_argument("--symbol", default="btc", choices=["btc", "eth"])
    parser.add_argument("--dry-run", action="store_true",
                        help="不真实下单，PaperBroker 按真实档深模拟成交")
    parser.add_argument("--windows", type=int, default=20, help="最多尝试的窗口数（默认 20）")
    parser.add_argument("--fills", type=int, default=3,
                        help="目标成交笔数：累计达到后停止（默认 3）")
    parser.add_argument("--now", action="store_true",
                        help="调试：不等待窗口起点，直接监测当前进行中窗口")
    parser.add_argument("--param", action="append", default=None,
                        metavar="K=V", help="覆盖策略参数（可多次，如 --param max_vol=25）")
    args = parser.parse_args()
    if args.windows < 1 or args.fills < 1:
        parser.error("--windows 和 --fills 必须为正整数")

    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    setup_logging(level="WARNING")

    s = get_settings()
    if not s.has_private_key:
        print("未配置 PM_PRIVATE_KEY，无法交易。")
        return 1

    p = Crypto5mParams().model_copy(update=parse_overrides(args.param))
    # dry-run 换 PaperBroker（commit B 唯一行为改进点）；live 构造时派生 L2 creds
    broker = PaperRunner() if args.dry_run else ClobBroker(ClobTrader(s))
    # 阶段 2：SQLite 结构层（orders/windows/positions）——dry/live 共用一库，
    # mode 列区分；live 库同时承担持仓持久化（kill 重启恢复）
    store = Store(DB_PATH)

    # 阶段 4：风控闸门（限额/熔断/重复入场/Kill Switch）。live 额外挂链上
    # 余额/gas 查询（fail-closed：查询失败也拒绝）；dry-run 无余额注入，
    # 其余规则同样生效（闸门是同一套纯函数，只是 I/O 依赖可缺省）。
    gate = RiskGate(
        limits=RiskLimits(), live=not args.dry_run, kill_path="runtime/KILL",
        day_stats_fn=store.day_stats,
        window_open_fn=lambda ws: store.has_open_position(args.symbol, ws),
    )
    if not args.dry_run:
        chain = ChainClient(s)

        async def _usdc_balance() -> Decimal | None:
            try:
                return await chain.usdc_balance()
            except Exception as e:
                print(f"[风控] USDC 余额查询失败（将 fail-closed 拒绝下单）："
                      f"{str(e)[:80]}")
                return None

        async def _gas_balance() -> Decimal | None:
            try:
                return await chain.pol_balance()
            except Exception:
                return None

        gate.balance_fn = _usdc_balance
        gate.gas_fn = _gas_balance

    orch = WindowOrchestrator(
        args.symbol, p, broker, SessionLog(sys.stdout.isatty()),
        dry=args.dry_run, max_windows=args.windows, max_fills=args.fills,
        store=store, gate=gate,
    )
    try:
        return asyncio.run(orch.run(now=args.now))
    except KeyboardInterrupt:
        return 130
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
