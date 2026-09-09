"""BTC/ETH 5 分钟 Up/Down 单笔方向性交易（小额真实资金）。

策略（用户指定规则，方向性投机，非套利）：
1. 对齐到下一个干净窗口起点，从 t=0 开始持续监测；
2. 监测两个源（每 2s 轮询）：
   - BTC 价格：Polygon 链上 Chainlink XX/USD 聚合器（与市场结算同源，
     市场描述明确写明按 Chainlink BTC/USD TWAP 结算）；
   - 双边盘口：Up/Down token 的 best bid/ask（CLOB REST /book）；
   同时记录本窗口 BTC 价格高低点；
3. 入场（窗口第 90–120 秒，即剩余 3:30–3:00，条件全部满足才买）：
   a. 本窗口 BTC 价格波动（high-low）< $20；
   b. 冷门方（更便宜一边）best_ask < 0.30（30 点）；
4. 买入冷门方吃单（FOK），名义金额 ≈ $2：份数 = ceil($2/ask)，不低于 orderMinSize；
5. 止盈：best_bid >= 买入价 ×1.5 时市价卖出（+50%）；
6. 未止盈则拿到结算：对每份赎 $1，错归 $0。

不满足入场条件则等下一个窗口重试（--windows 上限，默认 3）。

用法::

    PYTHONPATH=src python -m pm_arb.app.trade5m --symbol btc --dry-run
    PYTHONPATH=src python -m pm_arb.app.trade5m --symbol btc           # 真实下单
"""

from __future__ import annotations

import argparse
import asyncio
import math
import sys
import time
from decimal import Decimal

from pm_arb.data.clob_rest import ClobRestClient
from pm_arb.data.crypto_5m import discover_market, up_down_tokens
from pm_arb.data.gamma import GammaClient
from pm_arb.data.models import Market
from pm_arb.infra.config import Settings, get_settings
from pm_arb.infra.logging import get_logger, setup_logging

log = get_logger(__name__)

TARGET_NOTIONAL = Decimal("2.00")   # 名义金额 $2
ENTRY_AFTER = 90                     # 开窗后 90s（剩余 3:30）
ENTRY_UNTIL = 120                    # 开窗后 120s（剩余 3:00）
TAKE_PROFIT_RATIO = Decimal("1.5")   # +50% 止盈
MAX_ENTRY = Decimal("0.30")          # 冷门方入场价上限（30 点）
MAX_VOL = Decimal("20")              # 本窗口 BTC 波动上限（USD）
POLL = 2.0                           # 监测轮询间隔
TRACE_EVERY = 10.0                   # 行情轨迹打印间隔（秒）
END_MARGIN = 20                      # 结算前 N 秒停止操作

# Polygon 主网 Chainlink 聚合器（与 5min 市场结算同源；answer 8 位小数）
CHAINLINK_FEEDS: dict[str, str] = {
    "btc": "0xc907E116054Ad103354f2D350FD2514433D57F6f",
    "eth": "0xF9680D99D6C9589e2a93a78A04A279e509205945",
}
_LATEST_ROUND_DATA = "0xfeaf968c"  # latestRoundData() selector


def _fmt(d: Decimal | None, w: int = 6) -> str:
    return f"{d:.3f}".rjust(w) if d is not None else "n/a".rjust(w)


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
            price = Decimal(int(h[64:128], 16)) / Decimal(10**8)
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
        ev = await rest.get_book(tid)
        if ev is None:
            out[name] = {}
            continue
        bids = sorted(ev.bids, key=lambda l: l.price, reverse=True)
        asks = sorted(ev.asks, key=lambda l: l.price)
        out[name] = {
            "best_bid": bids[0].price if bids else None,
            "best_ask": asks[0].price if asks else None,
            "bid_size": bids[0].size if bids else None,
            "ask_size": asks[0].size if asks else None,
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


async def try_window(symbol: str, dry: bool, max_windows: int) -> int:
    s = get_settings()
    if not s.has_private_key:
        print("未配置 PM_PRIVATE_KEY，无法交易。")
        return 1

    feed_rpc = s.price_feed_rpc_url
    print(f"=== 5min {symbol.upper()} 单笔交易  {'[DRY-RUN]' if dry else '[LIVE 真实资金]'} ===")
    print(f"    入场过滤: 窗口BTC波动<${MAX_VOL} 且 冷门方ask<{MAX_ENTRY}（开窗后{ENTRY_AFTER}-{ENTRY_UNTIL}s）")
    print(f"    BTC喂价: Chainlink on Polygon（{feed_rpc}）｜止盈 +50%｜未止盈拿到结算")

    trader = None
    if not dry:
        from pm_arb.execution.clob_trader import ClobTrader

        trader = ClobTrader(s)  # 构造时派生 L2 creds

    for attempt in range(1, max_windows + 1):
        ws = await wait_next_window_start()
        feed = ChainlinkFeed(symbol, feed_rpc, s.proxy_url or None)
        print(f"\n[窗口 {attempt}] 起点 {ws}（本地 {time.strftime('%H:%M:%S')}），开始持续监测 …")

        async with GammaClient() as gamma:
            m: Market | None = await discover_market(gamma, symbol)
        if m is None:
            print("  ❌ 未发现当前窗口市场，跳过。")
            continue
        raw = await fetch_raw_market(m.slug, s)
        min_size = int(raw.get("orderMinSize") or 5)
        if not raw.get("enableOrderBook"):
            print(f"  ❌ 市场不接受下单（enableOrderBook=False），跳过。")
            continue
        up, down = up_down_tokens(m)
        print(f"  市场: {m.question}  (minSize={min_size})")

        entered = False
        side_name: str | None = None
        entry_price: Decimal | None = None
        filled = Decimal(0)
        tp_hit = False
        deadline = ws + 300 - END_MARGIN
        last_trace = 0.0

        async with ClobRestClient() as rest:
            while int(time.time()) < deadline:
                elapsed = time.time() - ws
                await feed.poll_once()
                b = await books(rest, up, down)

                # ---- 行情轨迹 ----
                if elapsed - last_trace >= TRACE_EVERY or dry:
                    last_trace = elapsed
                    ua = b["Up"].get("best_ask")
                    ub = b["Up"].get("best_bid")
                    da = b["Down"].get("best_ask")
                    db = b["Down"].get("best_bid")
                    pos = ""
                    if entered:
                        pnl = (b[side_name].get("best_bid") or entry_price) - entry_price
                        pos = f"  [持仓 {side_name} @{entry_price} 浮动 {pnl:+.3f}]"
                    print(f"  t+{elapsed:>5.0f}s  BTC={_fmt(feed.last, 9)} "
                          f"range={_fmt(feed.price_range, 7)}  "
                          f"Up {_fmt(ub)}/{_fmt(ua)}  Down {_fmt(db)}/{_fmt(da)}{pos}")

                # ---- 入场判定（开窗后 90–120s，一次性）----
                if not entered and ENTRY_AFTER <= elapsed <= ENTRY_UNTIL:
                    cand, ask = pick_underdog(b)
                    rng = feed.price_range
                    if ask is None or rng is None:
                        print(f"  [入场检查] 数据不全（ask={ask} range={rng}），本窗口放弃。")
                    elif ask >= MAX_ENTRY:
                        print(f"  [入场检查] ❌ 冷门方 {cand} ask={_fmt(ask)} ≥ {MAX_ENTRY}，跳过。")
                    elif rng >= MAX_VOL:
                        print(f"  [入场检查] ❌ BTC 波动 ${rng:.2f} ≥ ${MAX_VOL}，跳过。")
                    else:
                        size = calc_size(ask, min_size)
                        token_id = up if cand == "Up" else down
                        print(f"  [入场检查] ✅ {cand} ask={_fmt(ask)} 波动${rng:.2f}"
                              f" → 买 {size} 份 ≈ ${ask * size:.2f}，止盈价 {_fmt(ask * TAKE_PROFIT_RATIO)}")
                        if trader is None:
                            entered, side_name, entry_price = True, cand, ask
                            filled = Decimal(size)
                        else:
                            from pm_arb.execution.orders import OrderType, Side

                            order = await trader.place_limit(
                                token_id, Side.BUY, ask, Decimal(size), order_type=OrderType.FOK
                            )
                            print(f"  买单: {order.status.value} {order.error or ''}")
                            if order.status.value in ("FILLED", "PARTIAL"):
                                entered = True
                                side_name = cand
                                entry_price = order.avg_fill_price or ask
                                filled = order.filled_size
                                print(f"  成交: {filled} 份 @ {entry_price}")
                            else:
                                print("  FOK 未成交，本窗口放弃。")
                                break

                # ---- 止盈判定 ----
                if entered and side_name is not None:
                    bb = b[side_name].get("best_bid")
                    if bb is not None and bb >= entry_price * TAKE_PROFIT_RATIO:
                        print(f"  ✅ 触发止盈: bid {bb} >= {entry_price * TAKE_PROFIT_RATIO}")
                        token_id = up if side_name == "Up" else down
                        if trader is None:
                            print(f"  [DRY] 模拟卖出 {filled} 份 @ {bb}，"
                                  f"盈利 ≈ ${(bb - entry_price) * filled:+.2f}")
                        else:
                            from pm_arb.execution.orders import OrderType, Side

                            o = await trader.place_limit(
                                token_id, Side.SELL, bb, filled, order_type=OrderType.FOK
                            )
                            print(f"  卖单: {o.status.value} {o.error or ''}")
                            if o.status.value in ("FILLED", "PARTIAL") and o.avg_fill_price:
                                pnl = (o.avg_fill_price - entry_price) * o.filled_size
                                print(f"  平仓 PnL ≈ ${pnl:+.2f}")
                                tp_hit = True
                                break
                            # 未成交则下轮继续尝试
                await asyncio.sleep(POLL)

        if entered:
            if tp_hit:
                return 0
            print(f"  窗口到点未止盈（最后 BTC={_fmt(feed.last, 9)} range={_fmt(feed.price_range, 7)}）。"
                  f"{'[DRY] ' if dry else ''}按规则持有到结算：对赎 $1/份，错归 $0。")
            return 0
        print(f"  本窗口未入场，进入下一个窗口（{attempt}/{max_windows}）。")

    print(f"\n{max_windows} 个窗口均未满足入场条件，未交易。")
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description="5min 加密单笔交易（冷门方 +50% 止盈）")
    parser.add_argument("--symbol", default="btc", choices=["btc", "eth"])
    parser.add_argument("--dry-run", action="store_true", help="不真实下单，只模拟观察")
    parser.add_argument("--windows", type=int, default=3, help="最多尝试的窗口数（默认 3）")
    args = parser.parse_args()

    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    setup_logging(level="WARNING")

    try:
        return asyncio.run(try_window(args.symbol, args.dry_run, args.windows))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
