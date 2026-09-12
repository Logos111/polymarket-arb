"""窗口生命周期编排（阶段 1 commit C：从 trade5m.try_window 迁入）。

职责（DEV_PLAN 阶段 1 目标布局）：对齐 → 引导 → 守卫 → 监测循环 → 清理。

装配约定：
- 决策只调 decisions.py（唯一决策来源），编排层不做判定；
- 下单只走 Broker 协议（live=ClobBroker / dry-run=PaperRunner），编排层
  不感知真实资金；
- 时间注入：``clock`` 缺省 wall clock，编排层禁止直接调 time.time()——
  回测虚拟时钟复用同一编排路径的前提（设计决策 2）；
- SessionLog 在 run() 结束/异常时均关闭（修复句柄泄漏：原先部分路径
  不经 close）。
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
import time
from collections.abc import Callable
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
from pm_arb.execution.broker import Broker
from pm_arb.execution.orders import Side
from pm_arb.infra.config import get_settings
from pm_arb.strategies.crypto_5m.context import WindowDataHub, fetch_raw_market
from pm_arb.strategies.crypto_5m.decisions import (
    EntryAction,
    decide_entry,
    decide_exit,
    decide_stop,
    pick_underdog,
)
from pm_arb.strategies.crypto_5m.decisions import (
    fmt_price as _fmt,
)
from pm_arb.strategies.crypto_5m.params import Crypto5mParams


class WindowOrchestrator:
    """单币 5m Up/Down 多窗口交易编排（对齐/引导/守卫/循环/清理）。"""

    def __init__(
        self,
        symbol: str,
        p: Crypto5mParams,
        broker: Broker,
        session: SessionLog,
        *,
        dry: bool,
        max_windows: int = 20,
        max_fills: int = 3,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.symbol = symbol
        self.p = p
        self.broker = broker
        self.sl = session
        self.dry = dry
        self.max_windows = max_windows
        self.max_fills = max_fills
        self.clock = clock

    # ---- 窗口对齐 ----

    async def _wait_next_window_start(self) -> int:
        """睡到下一个 5 分钟窗口起点（保证从 t=0 开始监测，高低点完整）。"""
        while True:
            now = int(self.clock())
            ws = (now // 300 + 1) * 300
            wait = ws - now
            if wait <= 2:
                return ws
            print(f"  等待下一个窗口起点（{wait}s 后）…")
            await asyncio.sleep(min(wait - 1, 10.0))

    # ---- 引导 + 守卫 ----

    async def _bootstrap(self, ws: int) -> tuple[Market, dict, int] | None:
        """取窗口市场 + 原始字段 + minSize；失败/守卫不过返回 None。

        窗口起点代理偶发抖动时（本地 Clash 实测有过 ~1 分钟断流），有限
        重试引导，避免一次网络故障浪费整个窗口。
        """
        s = get_settings()
        sl = self.sl
        m: Market | None = None
        raw: dict | None = None
        for boot_attempt in range(6):
            try:
                async with GammaClient() as gamma:
                    # 显式传监测窗口起点 ws：slug 按窗口"结束"时刻构造（见
                    # crypto_5m.window_slug），不传则会按取市场那一刻的当前
                    # 窗口取 slug，边界竞态会拿到已结算的上一个市场
                    m = await get_window_market(gamma, self.symbol, ws)
                if m is not None:
                    raw = await fetch_raw_market(m.slug, s)
                if raw is not None:
                    break
            except Exception as e:
                sl.line(f"[引导] 第 {boot_attempt + 1} 次取市场失败：{str(e)[:80]}")
            await asyncio.sleep(5)
        if m is None or raw is None:
            sl.line("❌ 引导重试后仍未取得当前窗口市场，跳过。", event=True)
            return None
        min_size = int(raw.get("orderMinSize") or 5)
        if not raw.get("enableOrderBook"):
            sl.line("❌ 市场不接受下单（enableOrderBook=False），跳过。", event=True)
            return None
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
            return None
        return m, raw, min_size

    # ---- 主循环 ----

    async def run(self, *, now: bool = False) -> int:
        p, sl, clock = self.p, self.sl, self.clock
        mode = "[DRY-RUN]" if self.dry else "[LIVE 真实资金]"
        sl.line(f"=== 5min {self.symbol.upper()} 多笔交易  {mode} ===", event=True)
        sl.line(f"计划: 最多 {self.max_windows} 个窗口，累计成交 {self.max_fills} 笔后停止",
                event=True)
        sl.line(f"入场过滤: 窗口TWAP波动<${p.max_vol} 且 冷门方ask∈({p.min_entry},{p.max_entry})"
                f"（开窗后{p.entry_after}-{p.entry_until}s）｜止盈价 {p.take_profit_price}"
                f"｜未止盈拿到结算")
        sl.line(f"喂价: Polymarket RTDS Chainlink TWAP-60s（结算同源）｜日志: {sl.path}")

        fills_done = 0  # 累计成交笔数（达到 max_fills 停止）
        realized_pnl = Decimal(0)  # 已止盈平仓的实现盈亏
        pending_cost = Decimal(0)  # 持有到结算仓位的成本（赎回前未计入 PnL）
        pending_count = 0  # 待结算仓位笔数
        s = get_settings()

        try:
            for attempt in range(1, self.max_windows + 1):
                ws = current_window_start() if now else await self._wait_next_window_start()
                # 结算同源喂价：RTDS 中继的 Chainlink TWAP-60s 流（每窗口新建，
                # 保证 base/high/low 统计与窗口起点对齐）
                rtds = RtdsTwapFeed(self.symbol, s)
                sl.line(f"[窗口 {attempt}] 起点 {ws}"
                        f"（本地 {time.strftime('%H:%M:%S', time.localtime(clock()))}），"
                        f"开始持续监测 …", event=True)

                boot = await self._bootstrap(ws)
                if boot is None:
                    continue
                m, _raw, min_size = boot
                up, down = up_down_tokens(m)
                sl.line(f"市场: {m.question}  (minSize={min_size})")

                entered = False
                entry_done = False  # 已做出最终判定（成交 / 波动超限等不可恢复拒绝）
                last_ask_reject: Decimal | None = None  # ask 拒绝只在价格变化时打印
                side_name: str | None = None
                entry_price: Decimal | None = None
                filled = Decimal(0)
                tp_hit = False
                deadline = ws + 300 - p.end_margin

                st = {
                    "symbol": self.symbol, "mode": mode, "attempt": attempt,
                    "max_windows": self.max_windows,
                    "fills_done": fills_done, "max_fills": self.max_fills,  # 跨窗口累计战况
                    "realized_pnl": str(realized_pnl), "pending_cost": str(pending_cost),
                    "pending_count": pending_count,
                    "elapsed": 0.0, "remain": 300.0,
                    "btc_last": None, "btc_high": None, "btc_low": None, "btc_range": None,
                    "book": {"Up": {}, "Down": {}}, "underdog": "-",
                    "ud_ask": None, "ud_ask_sz": None,
                    "entry_status": "等待中", "pos_side": None, "pos_entry": None,
                    "pos_filled": Decimal(0), "pos_bid": None, "pos_pnl": Decimal(0),
                    "tp_target": None,
                    # 数据源探针：盘口来自 WS 本地簿还是 REST 回退；喂价活跃度与新鲜度
                    "src": "-", "ws_events": 0, "ws_changes": 0, "ws_books": 0,
                    "ws_ago": -1.0, "feed_age": -1.0, "feed_seq": 0,
                    "book_age": float("inf"),
                }

                mfeed = MarketDataFeed([up, down])
                self.broker.bind_books(mfeed.books)  # paper 撮合吃真实本地簿；live 空操作
                stop_feed = asyncio.Event()
                # WS 活跃度统计：每收到一个真实 WS 事件就自增，证明盘口是实时增量流
                # 而非 REST 快照回退（events 持续增长 => WS 在推；停滞 => 断流/回退）
                ws_stats = {"events": 0, "books": 0, "changes": 0, "last_wall": 0.0}

                async def _run_feed(mf: MarketDataFeed, stop: asyncio.Event,
                                    stats: dict) -> None:
                    gen = mf.run()
                    try:
                        async for ev in gen:
                            stats["events"] += 1
                            stats["last_wall"] = time.time()  # 墙钟：仅用于活跃度展示
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
                    hub = WindowDataHub(mfeed, rtds, rest, up, down,
                                        ws_fresh_sec=p.ws_fresh_sec)
                    while clock() < deadline:
                        elapsed = clock() - ws
                        # 优先 WS 实时簿；未就绪/陈旧/断流时回退 REST 快照（见 hub docstring）
                        got = await hub.get_books()
                        if got is None:
                            sl.line(f"[盘口] 拉取失败，下轮重试："
                                    f"{str(hub.last_error or '')[:80]}")
                            await asyncio.sleep(p.poll)
                            continue
                        b, src = got

                        cand, ask = pick_underdog(b)
                        # ---- 刷新状态 ----
                        ws_ago = (time.time() - ws_stats["last_wall"]) \
                            if ws_stats["last_wall"] else -1.0
                        feed_age = rtds.age() if rtds.last is not None else -1.0
                        # 本地簿龄：WS 静默时持续增大，超阈值即触发上面的 REST 回退
                        book_age = hub.book_age()
                        st.update(
                            elapsed=elapsed, remain=max(0.0, deadline - clock()),
                            btc_last=rtds.last, btc_high=rtds.high, btc_low=rtds.low,
                            btc_range=rtds.price_range, book=b, underdog=cand, ud_ask=ask,
                            ud_ask_sz=(b.get(cand) or {}).get("ask_size"),
                            src=src, ws_events=ws_stats["events"],
                            ws_changes=ws_stats["changes"],
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
                        # best_ask 档位深度（份数）：回放验证成交可行性
                        us_s = (f"×{b['Up']['ask_size']:.0f}"
                                if b["Up"].get("ask_size") is not None else "")
                        ds_s = (f"×{b['Down']['ask_size']:.0f}"
                                if b["Down"].get("ask_size") is not None else "")
                        # 数据源探针：盘口源 + 本地簿龄/阈值 + WS 事件计数 + TWAP 喂价 age
                        ws_ago_s = f"{ws_ago:.1f}s" if ws_ago >= 0 else "从未"
                        feed_age_s = f"{feed_age:.0f}s" if feed_age >= 0 else "无"
                        book_age_s = f"{book_age:.1f}s" if book_age != float("inf") else "无"
                        probe = (f"[盘口={src} 簿龄={book_age_s}/{p.ws_fresh_sec:.0f}s "
                                 f"WS事件={ws_stats['events']}"
                                 f"(簿{ws_stats['books']}/增量{ws_stats['changes']}) "
                                 f"距上次WS={ws_ago_s}] [TWAP#{rtds.seq} age={feed_age_s}]")
                        pos = ""
                        if entered:
                            pnl = (b[side_name].get("best_bid") or entry_price) - entry_price
                            pos = f"  [持仓 {side_name} @{entry_price} 浮动 {pnl:+.3f}]"
                        sl.line(f"t+{elapsed:>5.0f}s  TWAP={_fmt(rtds.last, 9)} "
                                f"range={_fmt(rtds.price_range, 7)}  "
                                f"Up {_fmt(ub)}/{_fmt(ua)}{us_s}  "
                                f"Down {_fmt(db)}/{_fmt(da)}{ds_s}  "
                                f"{probe}{pos}")

                        # ---- 入场判定（唯一决策源：decisions.decide_entry）。波动
                        #      high-low 单调不减，一旦超限即永久拒绝；ask 可能回落，
                        #      仅在价格变化时打印 ----
                        if (not entered and not entry_done
                                and p.entry_after <= elapsed <= p.entry_until):
                            st["entry_status"] = "判定中"
                            rng = rtds.price_range
                            d = decide_entry(elapsed, cand, ask,
                                             (b[cand] or {}).get("ask_size"),
                                             rng, p, min_size=min_size)
                            if (d.action is EntryAction.ABORT_DATA
                                    or d.action is EntryAction.ABORT_VOL):
                                sl.line(d.log, event=True)
                                st["entry_status"] = d.status
                                entry_done = True
                            elif d.action is EntryAction.OBSERVE:
                                st["entry_status"] = d.status
                                if ask != last_ask_reject:
                                    sl.line(d.log)
                                    last_ask_reject = ask
                            else:  # ENTER：信号成立即市价成交（FAK），BUY 按美元下单；
                                # ref_price 用本地盘口 ask 估算目标份数（问题 4a），
                                # live/paper 同一 Broker 协议路径
                                token_id = up if cand == "Up" else down
                                sl.line(d.log, event=True)
                                order = await self.broker.place_market(
                                    token_id, Side.BUY, p.target_notional, ref_price=ask
                                )
                                sl.line(f"买单: {order.status.value} {order.error or ''}",
                                        event=True)
                                if order.filled_size > 0 and order.status.value in (
                                    "FILLED", "PARTIAL"
                                ):
                                    entered = True
                                    side_name = cand
                                    entry_price = order.avg_fill_price or ask
                                    filled = order.filled_size
                                    st.update(pos_side=cand, pos_entry=entry_price,
                                              pos_filled=filled,
                                              tp_target=p.take_profit_price)
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
                                elapsed, cand, ask, None, rtds.price_range, p).status

                        # ---- 止盈/止损判定（唯一决策源：decisions）----
                        if entered and side_name is not None:
                            bb = b[side_name].get("best_bid")
                            exit_msg = ""
                            if decide_exit(bb, p):
                                exit_msg = f"✅ 止盈: bid {bb} >= {p.take_profit_price}"
                            elif decide_stop(bb, p):
                                exit_msg = f"🛑 止损: bid {bb} <= {p.stop_loss_price}"
                            if exit_msg:
                                sl.line(exit_msg, event=True)
                                token_id = up if side_name == "Up" else down
                                # 止盈/止损均用市价卖出（按持仓份数），ref_price=bb
                                # 供目标份数记录（SELL amount 本身即份数）
                                o = await self.broker.place_market(
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

                        if sys.stdout.isatty():
                            _render_tui(st, sl, p)
                        await asyncio.sleep(p.poll)

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
                        sl.line(f"[成交 {fills_done}/{self.max_fills}] ✅ 已平仓（止盈/止损）。",
                                event=True)
                    else:
                        pending_cost += (entry_price or Decimal(0)) * filled
                        pending_count += 1
                        st["pending_cost"] = str(pending_cost)
                        st["pending_count"] = pending_count
                        tail = "[DRY] " if self.dry else ""
                        sl.line(f"到点未止盈（TWAP={_fmt(rtds.last, 9)} "
                                f"range={_fmt(rtds.price_range, 7)}）。"
                                f"{tail}持有到结算：对赎 $1/份，错归 $0。", event=True)
                    if fills_done >= self.max_fills:
                        sl.line(f"=== 目标达成：{self.max_fills} 笔成交"
                                f"（尝试 {attempt}/{self.max_windows} 窗口）。"
                                f"已止盈 PnL ${realized_pnl:+.2f}｜待结算 {pending_count} 笔 "
                                f"${pending_cost:.2f}（对赎 $1/份，错归 $0） ===", event=True)
                        return 0
                    sl.line(f"已成交 {fills_done}/{self.max_fills} 笔"
                            f"（已止盈 PnL ${realized_pnl:+.2f}｜待结算 {pending_count} 笔 "
                            f"${pending_cost:.2f}），进入下一个窗口"
                            f"（{attempt}/{self.max_windows}）。", event=True)
                else:
                    sl.line(f"本窗口未入场，进入下一个窗口"
                            f"（{attempt}/{self.max_windows}）。", event=True)

            sl.line(f"=== {self.max_windows} 个窗口尝试完毕：成交 {fills_done}/"
                    f"{self.max_fills} 笔，已止盈 PnL ${realized_pnl:+.2f}｜"
                    f"待结算 {pending_count} 笔 ${pending_cost:.2f} ===", event=True)
            return 0 if fills_done else 1
        finally:
            sl.close()
