"""5m 策略终端可视化（阶段 1 commit A：从 app/trade5m.py 纯搬运）。

- :class:`SessionLog`：决策/行情日志（写文件 + 非 TTY 滚动打印 + 事件尾缓冲）；
- :func:`render_tui`：清屏实时仪表盘（每轮询刷新）。

渲染引用 :class:`Crypto5mParams` 而非模块常量，保证与决策层同一份参数。
"""

from __future__ import annotations

import os
import time
from decimal import Decimal

from pm_arb.strategies.crypto_5m.params import Crypto5mParams

LOG_DIR = os.path.join("runtime", "logs")


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


def render_tui(st: dict, sl: SessionLog, p: Crypto5mParams) -> None:
    """清屏刷新实时仪表盘。"""
    L: list[str] = ["\033[2J\033[H" + "=" * 74]
    fd, mf = st["fills_done"], st["max_fills"]
    hdr = (f" 5min {st['symbol'].upper()}  {st['mode']:<16} "
           f"窗口 {st['attempt']}/{st['max_windows']}  累计成交 {fd}/{mf}"
           f"  已止盈 ${Decimal(st['realized_pnl']):+.2f}"
           f"｜待结算 ${Decimal(st['pending_cost']):.2f}({st['pending_count']}笔)")
    L.append(hdr + f"   t+{st['elapsed']:>5.0f}s  剩余 {st['remain']:>3.0f}s")
    L.append("-" * 74)
    rng = st["btc_range"]
    vol_ok = rng is not None and rng < p.max_vol
    feed_age = st.get("feed_age", -1.0)
    feed_age_s = f"{feed_age:.0f}s前更新" if feed_age >= 0 else "无"
    L.append(f" Chainlink TWAP  last={_fmt(st['btc_last'], 11)}  high={_fmt(st['btc_high'], 11)}"
             f"  low={_fmt(st['btc_low'], 11)}")
    L.append(f" 窗口波动 range={_fmt(rng, 8)}  过滤(<${p.max_vol}): "
             f"{'✅ 通过' if vol_ok else '❌ 超限' if rng is not None else '… 采样中'}"
             f"   TWAP#{st.get('feed_seq')} {feed_age_s}")
    L.append("-" * 74)
    L.append(f" {'side':<6}{'best_bid':>10}{'best_ask':>10}")
    for name in ("Up", "Down"):
        bk = st["book"][name]
        L.append(f" {name:<6}{_fmt(bk['best_bid'], 10)}{_fmt(bk['best_ask'], 10)}")
    ud, ua, usz = st["underdog"], st["ud_ask"], st.get("ud_ask_sz")
    ask_ok = ua is not None and p.min_entry < ua < p.max_entry
    depth = f"×{usz:.0f}" if usz is not None else ""
    L.append(f" 冷门方={ud:<5} ask={_fmt(ua)}{depth}  过滤({p.min_entry}-{p.max_entry}): "
             f"{'✅' if ask_ok else '❌' if ua is not None else '…'}")
    L.append(f" 入场窗口[{p.entry_after},{p.entry_until}s]: {st['entry_status']}")
    ws_ago = st.get("ws_ago", -1.0)
    ws_ago_s = f"{ws_ago:.1f}s前" if ws_ago >= 0 else "从未"
    src = st.get("src", "-")
    src_tag = "✅实时WS" if src == "WS" else "⚠️REST回退"
    book_age = st.get("book_age", float("inf"))
    book_age_s = f"{book_age:.1f}s" if book_age != float("inf") else "无"
    L.append(f" 数据源: 盘口={src} {src_tag}  簿龄={book_age_s}/{p.ws_fresh_sec:.0f}s  "
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
