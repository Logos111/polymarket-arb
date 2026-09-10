"""BTC/ETH 5 分钟 Up/Down 单笔方向性交易（小额真实资金）。

策略（用户指定规则，方向性投机，非套利）：
1. 对齐到下一个干净窗口起点，从 t=0 开始持续监测；
2. 监测两个源（每 2s 轮询）：
   - BTC 价格：Polygon 链上 Chainlink XX/USD 聚合器（与市场结算同源，
     市场描述明确写明按 Chainlink BTC/USD TWAP 结算）；
   - 双边盘口：Up/Down token 的 best bid/ask（CLOB REST /book）；
   同时记录本窗口 BTC 价格高低点；
3. 入场（窗口第 105–135 秒，即剩余 3:15–2:45，条件全部满足才买）：
   a. 本窗口 BTC 价格波动（high-low）< $25；
   b. 冷门方（更便宜一边）best_ask < 0.30（30 点）；
4. 买入冷门方吃单（FOK），名义金额 ≈ $2：份数 = ceil($2/ask)，不低于 orderMinSize；
5. 止盈：best_bid >= 买入价 ×2.0 时市价卖出（+100%）；
6. 未止盈则拿到结算：对每份赎 $1，错归 $0。

不满足入场条件则等下一个窗口重试（--windows 上限，默认 3）。

可视化：
- 交互式终端（TTY）：清屏实时仪表盘，每轮询（2s）刷新，含盘口、喂价、
  过滤状态、入场/止盈判定与持仓浮盈，底部滚动最近决策事件；
- 非 TTY（后台/管道）：滚动打印同样信息；
- 始终追加写入 ``data/logs/trade5m_<ts>.log``，可 ``tail -f`` 实时查看。

用法::

    PYTHONPATH=src python -m pm_arb.app.trade5m --symbol btc --dry-run
    PYTHONPATH=src python -m pm_arb.app.trade5m --symbol btc           # 真实下单
    PYTHONPATH=src python -m pm_arb.app.trade5m --symbol btc --now     # 调试：不等待窗口起点
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import math
import os
import sys
import time
from decimal import Decimal

from pm_arb.data.clob_rest import ClobRestClient
from pm_arb.data.crypto_5m import current_window_start, get_window_market, up_down_tokens
from pm_arb.data.feed import MarketDataFeed
from pm_arb.data.gamma import GammaClient
from pm_arb.data.models import Market, WsBookEvent, WsPriceChange
from pm_arb.infra.config import Settings, get_settings
from pm_arb.infra.logging import get_logger, setup_logging

log = get_logger(__name__)

TARGET_NOTIONAL = Decimal("2.00")   # 名义金额 $2
ENTRY_AFTER = 105                    # 开窗后 105s（剩余 3:15）
ENTRY_UNTIL = 135                    # 开窗后 135s（剩余 2:45）
TAKE_PROFIT_RATIO = Decimal("2.0")   # +100% 止盈
MAX_ENTRY = Decimal("0.30")          # 冷门方入场价上限（30 点）
MAX_VOL = Decimal("25")              # 本窗口 BTC 波动上限（USD）
POLL = 2.0                           # 监测轮询间隔
WS_FRESH_SEC = 10.0                  # WS 本地簿新鲜度阈值：超龄则判定陈旧回退 REST
END_MARGIN = 20                      # 结算前 N 秒停止操作
LOG_DIR = os.path.join("data", "logs")

# Polygon 主网 Chainlink 聚合器（与 5min 市场结算同源；answer 8 位小数）
CHAINLINK_FEEDS: dict[str, str] = {
    "btc": "0xc907E116054Ad103354f2D350FD2514433D57F6f",
    "eth": "0xF9680D99D6C9589e2a93a78A04A279e509205945",
}
_LATEST_ROUND_DATA = "0xfeaf968c"  # latestRoundData() selector


def _fmt(d: Decimal | None, w: int = 6) -> str:
    return f"{d:.3f}".rjust(w) if d is not None else "n/a".rjust(w)


class SessionLog:
    """决策/行情日志：写文件 + （非 TTY 时）滚动打印 + （TTY 时）事件尾缓冲。"""

    def __init__(self, tui: bool) -> None:
        self.tui = tui
        os.makedirs(LOG_DIR, exist_ok=True)
        self.path = os.path.join(LOG_DIR, f"trade5m_{time.strftime('%Y%m%d_%H%M%S')}.log")
        self._f = open(self.path, "a", encoding="utf-8")  # noqa: SIM115
        self.events: list[str] = []

    def line(self, msg: str, event: bool = False) -> None:
        rec = f"[{time.strftime('%H:%M:%S')}] {msg}"
        self._f.write(rec + "\n")
        self._f.flush()
        if event:
            self.events.append(msg)
            self.events = self.events[-10:]
        if not self.tui:
            print(msg, flush=True)

    def close(self) -> None:
        self._f.close()


def _render_tui(st: dict, sl: SessionLog) -> None:
    """清屏刷新实时仪表盘。"""
    L: list[str] = ["\033[2J\033[H" + "=" * 74]
    hdr = (f" 5min {st['symbol'].upper()}  {st['mode']:<16} "
           f"窗口 {st['attempt']}/{st['max_windows']}")
    L.append(hdr + f"   t+{st['elapsed']:>5.0f}s  剩余 {st['remain']:>3.0f}s")
    L.append("-" * 74)
    rng = st["btc_range"]
    vol_ok = rng is not None and rng < MAX_VOL
    feed_age = st.get("feed_age", -1.0)
    feed_age_s = f"{feed_age:.0f}s前更新" if feed_age >= 0 else "无"
    L.append(f" Chainlink BTC  last={_fmt(st['btc_last'], 11)}  high={_fmt(st['btc_high'], 11)}"
             f"  low={_fmt(st['btc_low'], 11)}")
    L.append(f" 窗口波动 range={_fmt(rng, 8)}  过滤(<${MAX_VOL}): "
             f"{'✅ 通过' if vol_ok else '❌ 超限' if rng is not None else '… 采样中'}"
             f"   链上round={st.get('feed_round')} {feed_age_s}")
    L.append("-" * 74)
    L.append(f" {'side':<6}{'best_bid':>10}{'best_ask':>10}")
    for name in ("Up", "Down"):
        bk = st["book"][name]
        L.append(f" {name:<6}{_fmt(bk['best_bid'], 10)}{_fmt(bk['best_ask'], 10)}")
    ud, ua = st["underdog"], st["ud_ask"]
    ask_ok = ua is not None and ua < MAX_ENTRY
    L.append(f" 冷门方={ud:<5} ask={_fmt(ua)}  过滤(<{MAX_ENTRY}): "
             f"{'✅' if ask_ok else '❌' if ua is not None else '…'}")
    L.append(f" 入场窗口[{ENTRY_AFTER},{ENTRY_UNTIL}s]: {st['entry_status']}")
    ws_ago = st.get("ws_ago", -1.0)
    ws_ago_s = f"{ws_ago:.1f}s前" if ws_ago >= 0 else "从未"
    src = st.get("src", "-")
    src_tag = "✅实时WS" if src == "WS" else "⚠️REST回退"
    book_age = st.get("book_age", float("inf"))
    book_age_s = f"{book_age:.1f}s" if book_age != float("inf") else "无"
    L.append(f" 数据源: 盘口={src} {src_tag}  簿龄={book_age_s}/{WS_FRESH_SEC:.0f}s  "
             f"WS事件={st.get('ws_events', 0)}"
             f"(簿{st.get('ws_books', 0)}/增量{st.get('ws_changes', 0)})  距上次WS={ws_ago_s}")
    L.append("-" * 74)
    if st["pos_side"]:
        L.append(f" 持仓 {st['pos_side']} {st['pos_filled']} 份 @ {st['pos_entry']}"
                 f"  现bid={_fmt(st['pos_bid'])}  浮盈 {st['pos_pnl']:+.3f}"
                 f"  止盈线 {st['tp_target']}")
    else:
        L.append(" 持仓: 无")
    L.append("=" * 74)
    L.append(" 最近事件:")
    for ev in sl.events[-6:]:
        L.append(f"   · {ev[:70]}")
    L.append("=" * 74)
    L.append(f" Ctrl+C 退出  |  日志 {sl.path}")
    print("\n".join(L), flush=True)


class ChainlinkFeed:
    """持续轮询 Polygon 链上 Chainlink XX/USD 喂价，记录窗口内高低点。"""

    def __init__(self, symbol: str, rpc_url: str, proxy: str | None) -> None:
        self.addr = CHAINLINK_FEEDS[symbol]
        self.rpc_url = rpc_url
        self.proxy = proxy
        self.last: Decimal | None = None
        self.high: Decimal | None = None
        self.low: Decimal | None = None
        self.samples = 0
        self.errors = 0
        # 链上喂价的 round 与 updatedAt：用于证明价格静止是 Chainlink 心跳
        # 尚未翻新（真实结算源节奏），而非本地轮询卡住/读到陈旧值
        self.round_id: int | None = None
        self.updated_at: int | None = None

    @property
    def price_range(self) -> Decimal | None:
        if self.high is None or self.low is None:
            return None
        return self.high - self.low

    async def poll_once(self) -> Decimal | None:
        import httpx

        payload = {
            "jsonrpc": "2.0", "id": 1, "method": "eth_call",
            "params": [{"to": self.addr, "data": _LATEST_ROUND_DATA}, "latest"],
        }
        try:
            async with httpx.AsyncClient(timeout=10, proxy=self.proxy) as c:
                r = await c.post(self.rpc_url, json=payload)
                r.raise_for_status()
                res = r.json().get("result", "")
            if not res or len(res) < 258:
                raise ValueError(f"bad rpc payload: {str(r.json())[:100]}")
            h = res[2:]
            # latestRoundData 返回: roundId|answer|startedAt|updatedAt|answeredInRound
            self.round_id = int(h[0:64], 16)
            price = Decimal(int(h[64:128], 16)) / Decimal(10**8)
            self.updated_at = int(h[192:256], 16)
        except Exception as e:
            self.errors += 1
            if self.errors <= 3 or self.errors % 20 == 0:
                log.warning("feed_poll_failed", n=self.errors, error=str(e)[:100])
            return None
        self.last = price
        self.samples += 1
        self.high = price if self.high is None else max(self.high, price)
        self.low = price if self.low is None else min(self.low, price)
        return price


async def fetch_raw_market(slug: str, s: Settings) -> dict | None:
    import httpx

    async with httpx.AsyncClient(timeout=s.http_timeout, proxy=s.proxy_url or None) as c:
        r = await c.get(f"{s.gamma_api_url}/markets", params={"slug": slug})
        r.raise_for_status()
        j = r.json()
        return j[0] if j else None


async def books(rest: ClobRestClient, up: str, down: str) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for name, tid in (("Up", up), ("Down", down)):
        ob = await rest.get_book(tid)  # 上游返回 OrderBook（空盘口时 best_* 为 None）
        bb, ba = ob.best_bid, ob.best_ask
        out[name] = {
            "best_bid": bb.price if bb else None,
            "best_ask": ba.price if ba else None,
            "bid_size": bb.size if bb else None,
            "ask_size": ba.size if ba else None,
        }
    return out


def books_from_feed(
    feed: MarketDataFeed, up: str, down: str, max_age: float = WS_FRESH_SEC
) -> dict[str, dict] | None:
    """从 WS 维护的本地簿读实时最优价。

    仅当两个簿都“就绪且新鲜”（距上次更新 < max_age 秒）才返回；否则返回
    None，调用方回退 REST 快照。这是修复“ready 单向锁导致 REST 回退成死
    代码”的关键：WS 静默/订阅失效但连接未断时，本地簿会冻结在旧值，必须
    靠新鲜度而非 ready 判定来触发回退。
    """
    out: dict[str, dict] = {}
    for name, tid in (("Up", up), ("Down", down)):
        ob = feed.books().get(tid)
        if ob is None or not ob.is_fresh(max_age):
            return None
        bb, ba = ob.best_bid, ob.best_ask
        out[name] = {
            "best_bid": bb.price if bb else None,
            "best_ask": ba.price if ba else None,
            "bid_size": bb.size if bb else None,
            "ask_size": ba.size if ba else None,
        }
    return out


def pick_underdog(b: dict[str, dict]) -> tuple[str, Decimal | None]:
    """返回 (side_name, best_ask) —— 更便宜的一方。"""
    up_ask = b["Up"].get("best_ask")
    down_ask = b["Down"].get("best_ask")
    if up_ask is None and down_ask is None:
        return "Up", None
    if down_ask is None or (up_ask is not None and up_ask <= down_ask):
        return "Up", up_ask
    return "Down", down_ask


def calc_size(ask: Decimal, min_size: int) -> int:
    return max(math.ceil(TARGET_NOTIONAL / ask), min_size)


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


async def try_window(symbol: str, dry: bool, max_windows: int, now: bool = False) -> int:
    s = get_settings()
    if not s.has_private_key:
        print("未配置 PM_PRIVATE_KEY，无法交易。")
        return 1

    tui = sys.stdout.isatty()
    sl = SessionLog(tui)
    feed_rpc = s.price_feed_rpc_url
    mode = "[DRY-RUN]" if dry else "[LIVE 真实资金]"
    sl.line(f"=== 5min {symbol.upper()} 单笔交易  {mode} ===", event=True)
    sl.line(f"入场过滤: 窗口BTC波动<${MAX_VOL} 且 冷门方ask<{MAX_ENTRY}"
            f"（开窗后{ENTRY_AFTER}-{ENTRY_UNTIL}s）｜止盈 +100%｜未止盈拿到结算")
    sl.line(f"BTC喂价: Chainlink on Polygon（{feed_rpc}）｜日志: {sl.path}")

    trader = None
    if not dry:
        from pm_arb.execution.clob_trader import ClobTrader

        trader = ClobTrader(s)  # 构造时派生 L2 creds

    for attempt in range(1, max_windows + 1):
        ws = current_window_start() if now else await wait_next_window_start()
        # 实测 publicnode 直连可达，而本地代理对该 RPC 返回空体，喂价轮询走直连
        feed = ChainlinkFeed(symbol, feed_rpc, None)
        sl.line(f"[窗口 {attempt}] 起点 {ws}（本地 {time.strftime('%H:%M:%S')}），开始持续监测 …",
                event=True)

        # 窗口起点代理偶发抖动时（本地 Clash 实测有过 ~1 分钟断流），有限重试引导，
        # 避免一次网络故障浪费整个窗口
        m: Market | None = None
        raw: dict | None = None
        for boot_attempt in range(6):
            try:
                async with GammaClient() as gamma:
                    m = await get_window_market(gamma, symbol)
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
        up, down = up_down_tokens(m)
        sl.line(f"市场: {m.question}  (minSize={min_size})")

        entered = False
        entry_done = False  # 已做出最终判定（成交 / 波动超限等不可恢复拒绝）
        last_ask_reject: Decimal | None = None  # ask 拒绝只在价格变化时打印
        side_name: str | None = None
        entry_price: Decimal | None = None
        filled = Decimal(0)
        tp_hit = False
        deadline = ws + 300 - END_MARGIN

        st = {
            "symbol": symbol, "mode": mode, "attempt": attempt,
            "max_windows": max_windows, "elapsed": 0.0, "remain": 300.0,
            "btc_last": None, "btc_high": None, "btc_low": None, "btc_range": None,
            "book": {"Up": {}, "Down": {}}, "underdog": "-", "ud_ask": None,
            "entry_status": "等待中", "pos_side": None, "pos_entry": None,
            "pos_filled": Decimal(0), "pos_bid": None, "pos_pnl": Decimal(0),
            "tp_target": None,
            # 数据源探针：盘口来自 WS 本地簿还是 REST 回退；WS 活跃度与新鲜度
            "src": "-", "ws_events": 0, "ws_changes": 0, "ws_books": 0,
            "ws_ago": -1.0, "feed_age": -1.0, "feed_round": None,
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

        async with ClobRestClient() as rest:
            while int(time.time()) < deadline:
                elapsed = time.time() - ws
                await feed.poll_once()
                # 优先 WS 实时簿；未就绪/陈旧/断流时回退 REST 快照
                b = books_from_feed(mfeed, up, down, WS_FRESH_SEC)
                src = "WS" if b is not None else "REST"
                if b is None:
                    try:
                        b = await books(rest, up, down)
                    except Exception as e:
                        sl.line(f"[盘口] 拉取失败，下轮重试：{str(e)[:80]}")
                        await asyncio.sleep(POLL)
                        continue

                cand, ask = pick_underdog(b)
                # ---- 刷新状态 ----
                ws_ago = (time.time() - ws_stats["last_wall"]) if ws_stats["last_wall"] else -1.0
                feed_age = (time.time() - feed.updated_at) if feed.updated_at else -1.0
                # 本地簿龄：WS 静默时持续增大，超过 WS_FRESH_SEC 即触发上面的 REST 回退
                up_ob, dn_ob = mfeed.books().get(up), mfeed.books().get(down)
                book_age = max(up_ob.age() if up_ob else float("inf"),
                               dn_ob.age() if dn_ob else float("inf"))
                st.update(
                    elapsed=elapsed, remain=max(0.0, deadline - time.time()),
                    btc_last=feed.last, btc_high=feed.high, btc_low=feed.low,
                    btc_range=feed.price_range, book=b, underdog=cand, ud_ask=ask,
                    src=src, ws_events=ws_stats["events"], ws_changes=ws_stats["changes"],
                    ws_books=ws_stats["books"], ws_ago=ws_ago, feed_age=feed_age,
                    feed_round=feed.round_id, book_age=book_age,
                )
                if entered and side_name:
                    st["pos_bid"] = b[side_name].get("best_bid")
                    cur = st["pos_bid"] or entry_price or Decimal(0)
                    st["pos_pnl"] = cur - (entry_price or Decimal(0))

                # ---- 行情轨迹（写入日志，实时可 tail）----
                ua, ub = b["Up"].get("best_ask"), b["Up"].get("best_bid")
                da, db = b["Down"].get("best_ask"), b["Down"].get("best_bid")
                # 数据源探针：盘口源 + 本地簿龄/阈值 + WS 事件计数 + 链上喂价 age
                ws_ago_s = f"{ws_ago:.1f}s" if ws_ago >= 0 else "从未"
                feed_age_s = f"{feed_age:.0f}s" if feed_age >= 0 else "无"
                book_age_s = f"{book_age:.1f}s" if book_age != float("inf") else "无"
                probe = (f"[盘口={src} 簿龄={book_age_s}/{WS_FRESH_SEC:.0f}s "
                         f"WS事件={ws_stats['events']}"
                         f"(簿{ws_stats['books']}/增量{ws_stats['changes']}) "
                         f"距上次WS={ws_ago_s}] [链上round={feed.round_id} age={feed_age_s}]")
                pos = ""
                if entered:
                    pnl = (b[side_name].get("best_bid") or entry_price) - entry_price
                    pos = f"  [持仓 {side_name} @{entry_price} 浮动 {pnl:+.3f}]"
                sl.line(f"t+{elapsed:>5.0f}s  BTC={_fmt(feed.last, 9)} "
                        f"range={_fmt(feed.price_range, 7)}  "
                        f"Up {_fmt(ub)}/{_fmt(ua)}  Down {_fmt(db)}/{_fmt(da)}  {probe}{pos}")

                # ---- 入场判定（开窗后 105–135s）。波动 high-low 单调不减，
                #      一旦超限即永久拒绝；ask 可能回落，仅在价格变化时打印 ----
                if not entered and not entry_done and ENTRY_AFTER <= elapsed <= ENTRY_UNTIL:
                    st["entry_status"] = "判定中"
                    rng = feed.price_range
                    if ask is None or rng is None:
                        sl.line(f"[入场检查] 数据不全（ask={ask} range={rng}），放弃。",
                                event=True)
                        st["entry_status"] = "放弃(数据不全)"
                        entry_done = True
                    elif rng >= MAX_VOL:
                        sl.line(f"[入场检查] ❌ 波动 ${rng:.2f} ≥ ${MAX_VOL}，放弃。",
                                event=True)
                        st["entry_status"] = f"放弃(波动${rng:.0f})"
                        entry_done = True
                    elif ask >= MAX_ENTRY:
                        st["entry_status"] = f"观察(ask{ask}≥{MAX_ENTRY})"
                        if ask != last_ask_reject:
                            sl.line(f"[入场检查] … {cand} ask={_fmt(ask)} ≥ {MAX_ENTRY}，观察")
                            last_ask_reject = ask
                    else:
                        size = calc_size(ask, min_size)
                        token_id = up if cand == "Up" else down
                        tp = _fmt(ask * TAKE_PROFIT_RATIO)
                        sl.line(f"[入场检查] ✅ {cand} ask={_fmt(ask)} 波动${rng:.2f}"
                                f" → 买 {size} 份 ≈ ${ask * size:.2f}，止盈 {tp}",
                                event=True)
                        if trader is None:
                            entered, side_name, entry_price = True, cand, ask
                            filled = Decimal(size)
                            st.update(pos_side=cand, pos_entry=ask, pos_filled=filled,
                                      tp_target=ask * TAKE_PROFIT_RATIO)
                            st["entry_status"] = f"已入场 {cand}"
                        else:
                            from pm_arb.execution.orders import OrderType, Side

                            order = await trader.place_limit(
                                token_id, Side.BUY, ask, Decimal(size), order_type=OrderType.FOK
                            )
                            sl.line(f"买单: {order.status.value} {order.error or ''}", event=True)
                            if order.status.value in ("FILLED", "PARTIAL"):
                                entered = True
                                side_name = cand
                                entry_price = order.avg_fill_price or ask
                                filled = order.filled_size
                                st.update(pos_side=cand, pos_entry=entry_price, pos_filled=filled,
                                          tp_target=entry_price * TAKE_PROFIT_RATIO)
                                st["entry_status"] = f"已成交 {cand}"
                                sl.line(f"成交: {filled} 份 @ {entry_price}", event=True)
                            else:
                                st["entry_status"] = "FOK未成交"
                                sl.line("FOK 未成交，本窗口放弃。", event=True)
                                break
                elif not entered and not entry_done:
                    st["entry_status"] = (f"等待 t∈[{ENTRY_AFTER},{ENTRY_UNTIL}]"
                                          if elapsed < ENTRY_AFTER else "已错过")

                # ---- 止盈判定 ----
                if entered and side_name is not None:
                    bb = b[side_name].get("best_bid")
                    if bb is not None and bb >= entry_price * TAKE_PROFIT_RATIO:
                        sl.line(f"✅ 止盈: bid {bb} >= {entry_price * TAKE_PROFIT_RATIO}",
                                event=True)
                        token_id = up if side_name == "Up" else down
                        if trader is None:
                            sl.line(f"[DRY] 模拟卖出 {filled} 份 @ {bb}，"
                                    f"盈利 ≈ ${(bb - entry_price) * filled:+.2f}", event=True)
                        else:
                            from pm_arb.execution.orders import OrderType, Side

                            o = await trader.place_limit(
                                token_id, Side.SELL, bb, filled, order_type=OrderType.FOK
                            )
                            sl.line(f"卖单: {o.status.value} {o.error or ''}", event=True)
                            if o.status.value in ("FILLED", "PARTIAL") and o.avg_fill_price:
                                pnl = (o.avg_fill_price - entry_price) * o.filled_size
                                sl.line(f"平仓 PnL ≈ ${pnl:+.2f}", event=True)
                                tp_hit = True
                                break
                            # 未成交则下轮继续尝试

                if tui:
                    _render_tui(st, sl)
                await asyncio.sleep(POLL)

        stop_feed.set()
        feed_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await feed_task

        if entered:
            if tp_hit:
                sl.close()
                return 0
            tail = "[DRY] " if dry else ""
            sl.line(f"到点未止盈（BTC={_fmt(feed.last, 9)} range={_fmt(feed.price_range, 7)}）。"
                    f"{tail}持有到结算：对赎 $1/份，错归 $0。", event=True)
            sl.close()
            return 0
        sl.line(f"本窗口未入场，进入下一个窗口（{attempt}/{max_windows}）。", event=True)

    sl.line(f"{max_windows} 个窗口均未满足入场条件，未交易。", event=True)
    sl.close()
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description="5min 加密单笔交易（冷门方 +100% 止盈）")
    parser.add_argument("--symbol", default="btc", choices=["btc", "eth"])
    parser.add_argument("--dry-run", action="store_true", help="不真实下单，只模拟观察")
    parser.add_argument("--windows", type=int, default=3, help="最多尝试的窗口数（默认 3）")
    parser.add_argument("--now", action="store_true",
                        help="调试：不等待窗口起点，直接监测当前进行中窗口")
    args = parser.parse_args()

    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    setup_logging(level="WARNING")

    try:
        return asyncio.run(try_window(args.symbol, args.dry_run, args.windows, args.now))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
