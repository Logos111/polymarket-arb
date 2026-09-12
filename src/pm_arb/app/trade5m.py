"""BTC/ETH 5 分钟 Up/Down 多笔方向性交易（小额真实资金）。

策略（用户指定规则，方向性投机，非套利）：
1. 对齐到下一个干净窗口起点，从 t=0 开始持续监测；
2. 监测两个源：
   - BTC 价格：Polymarket RTDS 中继的 Chainlink BTC/USD TWAP-60s 流
     （市场 description 明示的结算源，与网页端实时价格同源；勿改用
     Polygon 链上聚合器 latestRoundData——那是另一条数据流，与结算无关）；
   - 双边盘口：Up/Down token 的 best bid/ask（CLOB WS 本地簿，REST 兑底）；
   同时记录本窗口 TWAP 高低点；
3. 入场（窗口第 70–135 秒，条件全部满足才买）：
   a. 本窗口 TWAP 波动（high-low）< $30；
   b. 冷门方（更便宜一边）0.15 < best_ask < 0.30；
4. 信号成立即市价买入冷门方（FAK，价格由 CLOB 按当前盘口计算），名义金额 ≈ $2；
   入场时记录 best_ask 档位深度（ask_size），用于回放验证成交可行性；
5. 止盈：best_bid >= 0.65 时卖出（固定止盈价，与入场价无关）；
6. 未止盈则拿到结算：对每份赎 $1，错归 $0。

不满足入场条件则等下一个窗口重试（--windows 上限，默认 20）；累计成交笔数达到
--fills（默认 3）后停止，成交后不退出而是继续监测下一个窗口。

可视化：
- 交互式终端（TTY）：清屏实时仪表盘，每轮询（2s）刷新，含盘口、喂价、
  过滤状态、入场/止盈判定与持仓浮盈，底部滚动最近决策事件；
- 非 TTY（后台/管道）：滚动打印同样信息；
- 始终追加写入 ``runtime/logs/trade5m_<ts>.log``，可 ``tail -f`` 实时查看。

用法::

    PYTHONPATH=src python -m pm_arb.app.trade5m --symbol btc --dry-run
    PYTHONPATH=src python -m pm_arb.app.trade5m --symbol btc           # 真实下单
    PYTHONPATH=src python -m pm_arb.app.trade5m --symbol btc --now     # 调试：不等待窗口起点
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import sys
import time
from datetime import datetime
from decimal import Decimal

from pm_arb.app.tui5m import SessionLog
from pm_arb.app.tui5m import render_tui as _render_tui
from pm_arb.data.clob_rest import ClobRestClient
from pm_arb.data.crypto_5m import current_window_start, get_window_market, up_down_tokens
from pm_arb.data.feed import MarketDataFeed
from pm_arb.data.gamma import GammaClient
from pm_arb.data.models import Market, WsBookEvent, WsPriceChange
from pm_arb.data.rtds import RtdsTwapFeed
from pm_arb.infra.config import get_settings
from pm_arb.infra.logging import get_logger, setup_logging
from pm_arb.strategies.crypto_5m.context import WindowDataHub, fetch_raw_market
from pm_arb.strategies.crypto_5m.decisions import (
    EntryAction,
    decide_entry,
    decide_exit,
    pick_underdog,
)
from pm_arb.strategies.crypto_5m.decisions import (
    fmt_price as _fmt,
)
from pm_arb.strategies.crypto_5m.params import Crypto5mParams

log = get_logger(__name__)

P = Crypto5mParams()  # 单一决策来源：实盘与回测共用同一份参数（commit C 接 CLI 覆盖）


async def wait_next_window_start() -> int:
    """睡到下一个 5 分钟窗口起点（保证从 t=0 开始监测，高低点完整）。"""
    while True:
        now = int(time.time())
        ws = (now // 300 + 1) * 300
        wait = ws - now
        if wait <= 2:
            return ws
        print(f"  等待下一个窗口起点（{wait}s 后）…")
        await asyncio.sleep(min(wait - 1, 10.0))


async def try_window(
    symbol: str, dry: bool, max_windows: int, now: bool = False, max_fills: int = 3
) -> int:
    s = get_settings()
    if not s.has_private_key:
        print("未配置 PM_PRIVATE_KEY，无法交易。")
        return 1

    tui = sys.stdout.isatty()
    sl = SessionLog(tui)
    mode = "[DRY-RUN]" if dry else "[LIVE 真实资金]"
    sl.line(f"=== 5min {symbol.upper()} 多笔交易  {mode} ===", event=True)
    sl.line(f"计划: 最多 {max_windows} 个窗口，累计成交 {max_fills} 笔后停止", event=True)
    sl.line(f"入场过滤: 窗口TWAP波动<${P.max_vol} 且 冷门方ask∈({P.min_entry},{P.max_entry})"
            f"（开窗后{P.entry_after}-{P.entry_until}s）｜止盈价 {P.take_profit_price}"
            f"｜未止盈拿到结算")
    sl.line(f"喂价: Polymarket RTDS Chainlink TWAP-60s（结算同源）｜日志: {sl.path}")

    trader = None
    if not dry:
        from pm_arb.execution.clob_trader import ClobTrader

        trader = ClobTrader(s)  # 构造时派生 L2 creds

    fills_done = 0  # 累计成交笔数（达到 max_fills 停止）
    realized_pnl = Decimal(0)  # 已止盈平仓的实现盈亏
    pending_cost = Decimal(0)  # 持有到结算仓位的成本（赎回前未计入 PnL）
    pending_count = 0  # 待结算仓位笔数

    for attempt in range(1, max_windows + 1):
        ws = current_window_start() if now else await wait_next_window_start()
        # 结算同源喂价：RTDS 中继的 Chainlink TWAP-60s 流（每窗口新建，
        # 保证 base/high/low 统计与窗口起点对齐）
        rtds = RtdsTwapFeed(symbol, s)
        sl.line(f"[窗口 {attempt}] 起点 {ws}（本地 {time.strftime('%H:%M:%S')}），开始持续监测 …",
                event=True)

        # 窗口起点代理偶发抖动时（本地 Clash 实测有过 ~1 分钟断流），有限重试引导，
        # 避免一次网络故障浪费整个窗口
        m: Market | None = None
        raw: dict | None = None
        for boot_attempt in range(6):
            try:
                async with GammaClient() as gamma:
                    # 显式传监测窗口起点 ws：slug 按窗口“结束”时刻构造（见
                    # crypto_5m.window_slug），不传则会按取市场那一刻的当前
                    # 窗口取 slug，边界路态会拿到已结算的上一个市场
                    m = await get_window_market(gamma, symbol, ws)
                if m is not None:
                    raw = await fetch_raw_market(m.slug, s)
                if raw is not None:
                    break
            except Exception as e:
                sl.line(f"[引导] 第 {boot_attempt + 1} 次取市场失败：{str(e)[:80]}")
            await asyncio.sleep(5)
        if m is None or raw is None:
            sl.line("❌ 引导重试后仍未取得当前窗口市场，跳过。", event=True)
            continue
        min_size = int(raw.get("orderMinSize") or 5)
        if not raw.get("enableOrderBook"):
            sl.line("❌ 市场不接受下单（enableOrderBook=False），跳过。", event=True)
            continue
        # 窗口匹配守卫：市场 endDate 实测给为窗口结束时刻（ws+300，ISO8601 UTC）。
        # 防边界竞态/选错市场——历史事故：曾在边界前 1s 默认取 slug，拿到刚结算的
        # 上一个市场，整窗监测死盘口且方向与喂价完全相反。假设必须运行时验证。
        end_raw = str(raw.get("endDate") or "")
        try:
            mkt_end = int(datetime.fromisoformat(end_raw.replace("Z", "+00:00")).timestamp())
        except ValueError:
            mkt_end = 0
        if abs(mkt_end - (ws + 300)) > 60:
            sl.line(f"❌ 市场窗口不匹配（endDate={end_raw}，预期≈{ws + 300}），疑取错市场，跳过。",
                    event=True)
            continue
        up, down = up_down_tokens(m)
        sl.line(f"市场: {m.question}  (minSize={min_size})")

        entered = False
        entry_done = False  # 已做出最终判定（成交 / 波动超限等不可恢复拒绝）
        last_ask_reject: Decimal | None = None  # ask 拒绝只在价格变化时打印
        side_name: str | None = None
        entry_price: Decimal | None = None
        filled = Decimal(0)
        tp_hit = False
        deadline = ws + 300 - P.end_margin

        st = {
            "symbol": symbol, "mode": mode, "attempt": attempt,
            "max_windows": max_windows,
            "fills_done": fills_done, "max_fills": max_fills,  # 跨窗口累计战况
            "realized_pnl": str(realized_pnl), "pending_cost": str(pending_cost),
            "pending_count": pending_count,
            "elapsed": 0.0, "remain": 300.0,
            "btc_last": None, "btc_high": None, "btc_low": None, "btc_range": None,
            "book": {"Up": {}, "Down": {}}, "underdog": "-", "ud_ask": None, "ud_ask_sz": None,
            "entry_status": "等待中", "pos_side": None, "pos_entry": None,
            "pos_filled": Decimal(0), "pos_bid": None, "pos_pnl": Decimal(0),
            "tp_target": None,
            # 数据源探针：盘口来自 WS 本地簿还是 REST 回退；喂价活跃度与新鲜度
            "src": "-", "ws_events": 0, "ws_changes": 0, "ws_books": 0,
            "ws_ago": -1.0, "feed_age": -1.0, "feed_seq": 0,
            "book_age": float("inf"),
        }

        mfeed = MarketDataFeed([up, down])
        stop_feed = asyncio.Event()
        # WS 活跃度统计：每收到一个真实 WS 事件就自增，证明盘口是实时增量流
        # 而非 REST 快照回退（events 持续增长 => WS 在推；停滞 => 断流/回退）
        ws_stats = {"events": 0, "books": 0, "changes": 0, "last_wall": 0.0}

        async def _run_feed(mf: MarketDataFeed, stop: asyncio.Event, stats: dict) -> None:
            gen = mf.run()
            try:
                async for ev in gen:
                    stats["events"] += 1
                    stats["last_wall"] = time.time()
                    if isinstance(ev, WsBookEvent):
                        stats["books"] += 1
                    elif isinstance(ev, WsPriceChange):
                        stats["changes"] += 1
                    if stop.is_set():
                        break
            finally:
                with contextlib.suppress(Exception):
                    await gen.aclose()

        feed_task = asyncio.create_task(_run_feed(mfeed, stop_feed, ws_stats))
        rtds_task = asyncio.create_task(rtds.run(stop_feed))

        async with ClobRestClient() as rest:
            hub = WindowDataHub(mfeed, rtds, rest, up, down, ws_fresh_sec=P.ws_fresh_sec)
            while int(time.time()) < deadline:
                elapsed = time.time() - ws
                # 优先 WS 实时簿；未就绪/陈旧/断流时回退 REST 快照（见 hub docstring）
                got = await hub.get_books()
                if got is None:
                    sl.line(f"[盘口] 拉取失败，下轮重试：{str(hub.last_error or '')[:80]}")
                    await asyncio.sleep(P.poll)
                    continue
                b, src = got

                cand, ask = pick_underdog(b)
                # ---- 刷新状态 ----
                ws_ago = (time.time() - ws_stats["last_wall"]) if ws_stats["last_wall"] else -1.0
                feed_age = rtds.age() if rtds.last is not None else -1.0
                # 本地簿龄：WS 静默时持续增大，超过 ws_fresh_sec 即触发上面的 REST 回退
                book_age = hub.book_age()
                st.update(
                    elapsed=elapsed, remain=max(0.0, deadline - time.time()),
                    btc_last=rtds.last, btc_high=rtds.high, btc_low=rtds.low,
                    btc_range=rtds.price_range, book=b, underdog=cand, ud_ask=ask,
                    ud_ask_sz=(b.get(cand) or {}).get("ask_size"),
                    src=src, ws_events=ws_stats["events"], ws_changes=ws_stats["changes"],
                    ws_books=ws_stats["books"], ws_ago=ws_ago, feed_age=feed_age,
                    feed_seq=rtds.seq, book_age=book_age,
                )
                if entered and side_name:
                    st["pos_bid"] = b[side_name].get("best_bid")
                    cur = st["pos_bid"] or entry_price or Decimal(0)
                    st["pos_pnl"] = cur - (entry_price or Decimal(0))

                # ---- 行情轨迹（写入日志，实时可 tail）----
                ua, ub = b["Up"].get("best_ask"), b["Up"].get("best_bid")
                da, db = b["Down"].get("best_ask"), b["Down"].get("best_bid")
                # best_ask 档位深度（份数）：回放验证 FOK 成交可行性
                us_s = f"×{b['Up']['ask_size']:.0f}" if b["Up"].get("ask_size") is not None else ""
                ds_s = (f"×{b['Down']['ask_size']:.0f}"
                        if b["Down"].get("ask_size") is not None else "")
                # 数据源探针：盘口源 + 本地簿龄/阈值 + WS 事件计数 + TWAP 喂价 age
                ws_ago_s = f"{ws_ago:.1f}s" if ws_ago >= 0 else "从未"
                feed_age_s = f"{feed_age:.0f}s" if feed_age >= 0 else "无"
                book_age_s = f"{book_age:.1f}s" if book_age != float("inf") else "无"
                probe = (f"[盘口={src} 簿龄={book_age_s}/{P.ws_fresh_sec:.0f}s "
                         f"WS事件={ws_stats['events']}"
                         f"(簿{ws_stats['books']}/增量{ws_stats['changes']}) "
                         f"距上次WS={ws_ago_s}] [TWAP#{rtds.seq} age={feed_age_s}]")
                pos = ""
                if entered:
                    pnl = (b[side_name].get("best_bid") or entry_price) - entry_price
                    pos = f"  [持仓 {side_name} @{entry_price} 浮动 {pnl:+.3f}]"
                sl.line(f"t+{elapsed:>5.0f}s  TWAP={_fmt(rtds.last, 9)} "
                        f"range={_fmt(rtds.price_range, 7)}  "
                        f"Up {_fmt(ub)}/{_fmt(ua)}{us_s}  Down {_fmt(db)}/{_fmt(da)}{ds_s}  "
                        f"{probe}{pos}")

                # ---- 入场判定（唯一决策源：decisions.decide_entry）。波动
                #      high-low 单调不减，一旦超限即永久拒绝；ask 可能回落，
                #      仅在价格变化时打印 ----
                if not entered and not entry_done and P.entry_after <= elapsed <= P.entry_until:
                    st["entry_status"] = "判定中"
                    rng = rtds.price_range
                    d = decide_entry(elapsed, cand, ask, (b[cand] or {}).get("ask_size"),
                                     rng, P, min_size=min_size)
                    if d.action is EntryAction.ABORT_DATA or d.action is EntryAction.ABORT_VOL:
                        sl.line(d.log, event=True)
                        st["entry_status"] = d.status
                        entry_done = True
                    elif d.action is EntryAction.OBSERVE:
                        st["entry_status"] = d.status
                        if ask != last_ask_reject:
                            sl.line(d.log)
                            last_ask_reject = ask
                    else:  # ENTER
                        size = d.size
                        token_id = up if cand == "Up" else down
                        sl.line(d.log, event=True)
                        if trader is None:
                            entered, side_name, entry_price = True, cand, ask
                            filled = Decimal(size)
                            st.update(pos_side=cand, pos_entry=ask, pos_filled=filled,
                                      tp_target=P.take_profit_price)
                            st["entry_status"] = f"已入场 {cand}"
                        else:
                            from pm_arb.execution.orders import Side

                            # 信号成立即市价成交（FAK）：价格由 CLOB 按当前盘口
                            # 计算，彻底消除 FOK 限价被往返延迟内价格上移整单
                            # 杀掉的问题（连续两窗实测）；BUY 按美元金额下单；
                            # ref_price 用本地盘口 ask 估算目标份数，让
                            # FILLED/PARTIAL 状态区分有意义（问题 4a）
                            order = await trader.place_market(
                                token_id, Side.BUY, P.target_notional, ref_price=ask
                            )
                            sl.line(f"买单: {order.status.value} {order.error or ''}", event=True)
                            if order.filled_size > 0 and order.status.value in (
                                "FILLED", "PARTIAL"
                            ):
                                entered = True
                                side_name = cand
                                entry_price = order.avg_fill_price or ask
                                filled = order.filled_size
                                st.update(pos_side=cand, pos_entry=entry_price, pos_filled=filled,
                                          tp_target=P.take_profit_price)
                                st["entry_status"] = f"已成交 {cand}"
                                sl.line(f"成交: {filled:.4f} 份 @ {entry_price:.4f}"
                                        f"（≈${entry_price * filled:.2f}）",
                                        event=True)
                            else:
                                st["entry_status"] = "市价单未成交"
                                sl.line("市价单未成交，本窗口放弃。", event=True)
                                entry_done = True
                elif not entered and not entry_done:
                    st["entry_status"] = decide_entry(
                        elapsed, cand, ask, None, rtds.price_range, P).status

                # ---- 止盈判定（唯一决策源：decisions.decide_exit） ----
                if entered and side_name is not None:
                    bb = b[side_name].get("best_bid")
                    if decide_exit(bb, P):
                        sl.line(f"✅ 止盈: bid {bb} >= {P.take_profit_price}",
                                event=True)
                        token_id = up if side_name == "Up" else down
                        if trader is None:
                            sl.line(f"[DRY] 模拟卖出 {filled} 份 @ {bb}，"
                                    f"盈利 ≈ ${(bb - entry_price) * filled:+.2f}", event=True)
                        else:
                            from pm_arb.execution.orders import Side

                            # 止盈也用市价卖出（按持仓份数），避免 FOK 限价
                            # 在快市中被杀导致止盈落空；ref_price=bb 供目标
                            # 份数记录（SELL amount 本身即份数）
                            o = await trader.place_market(
                                token_id, Side.SELL, filled, ref_price=bb
                            )
                            sl.line(f"卖单: {o.status.value} {o.error or ''}", event=True)
                            if o.status.value in ("FILLED", "PARTIAL") and o.avg_fill_price:
                                pnl = (o.avg_fill_price - entry_price) * o.filled_size
                                sl.line(f"平仓 PnL ≈ ${pnl:+.2f}", event=True)
                                realized_pnl += pnl
                                st["realized_pnl"] = str(realized_pnl)
                                tp_hit = True
                                break
                            # 未成交则下轮继续尝试

                if tui:
                    _render_tui(st, sl, P)
                await asyncio.sleep(P.poll)

        stop_feed.set()
        rtds_task.cancel()
        feed_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await rtds_task
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await feed_task

        if entered:
            fills_done += 1
            if tp_hit:
                sl.line(f"[成交 {fills_done}/{max_fills}] ✅ 已止盈平仓。", event=True)
            else:
                pending_cost += entry_price * filled
                pending_count += 1
                st["pending_cost"] = str(pending_cost)
                st["pending_count"] = pending_count
                tail = "[DRY] " if dry else ""
                sl.line(f"到点未止盈（TWAP={_fmt(rtds.last, 9)} "
                        f"range={_fmt(rtds.price_range, 7)}）。"
                        f"{tail}持有到结算：对赎 $1/份，错归 $0。", event=True)
            if fills_done >= max_fills:
                sl.line(f"=== 目标达成：{max_fills} 笔成交（尝试 {attempt}/{max_windows} 窗口）。"
                        f"已止盈 PnL ${realized_pnl:+.2f}｜待结算 {pending_count} 笔 "
                        f"${pending_cost:.2f}（对赎 $1/份，错归 $0） ===", event=True)
                sl.close()
                return 0
            sl.line(f"已成交 {fills_done}/{max_fills} 笔"
                    f"（已止盈 PnL ${realized_pnl:+.2f}｜待结算 {pending_count} 笔 "
                    f"${pending_cost:.2f}），进入下一个窗口"
                    f"（{attempt}/{max_windows}）。", event=True)
        else:
            sl.line(f"本窗口未入场，进入下一个窗口（{attempt}/{max_windows}）。", event=True)

    sl.line(f"=== {max_windows} 个窗口尝试完毕：成交 {fills_done}/{max_fills} 笔，"
            f"已止盈 PnL ${realized_pnl:+.2f}｜待结算 {pending_count} 笔 "
            f"${pending_cost:.2f} ===", event=True)
    sl.close()
    return 0 if fills_done else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="5min 加密单笔交易（冷门方 +100% 止盈）")
    parser.add_argument("--symbol", default="btc", choices=["btc", "eth"])
    parser.add_argument("--dry-run", action="store_true", help="不真实下单，只模拟观察")
    parser.add_argument("--windows", type=int, default=20, help="最多尝试的窗口数（默认 20）")
    parser.add_argument("--fills", type=int, default=3,
                        help="目标成交笔数：累计达到后停止（默认 3）")
    parser.add_argument("--now", action="store_true",
                        help="调试：不等待窗口起点，直接监测当前进行中窗口")
    args = parser.parse_args()
    if args.windows < 1 or args.fills < 1:
        parser.error("--windows 和 --fills 必须为正整数")

    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    setup_logging(level="WARNING")

    try:
        return asyncio.run(
            try_window(args.symbol, args.dry_run, args.windows, args.now, args.fills)
        )
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
