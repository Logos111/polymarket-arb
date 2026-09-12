"""pm-record：独立 24/7 纯录制脚本（DEV_PLAN 阶段 0.2）。

不下单、不占资金，把 5m Up/Down 市场行情与结算同源 TWAP 逐笔落盘
（JSONL 按天分片，append-only）::

    runtime/ticks/{YYYY-MM-DD}_market.jsonl     # typed WS 事件 + RestBook
                                                # + 连接事件 + 窗口元数据
    runtime/ticks/{YYYY-MM-DD}_rtds_{sym}.jsonl # RTDS 原始 payload
                                                # （含 full_accuracy_value/seq）
    runtime/ticks/{YYYY-MM-DD}_raw_ws.jsonl     # 原始 WS 帧（抽样/截断，
                                                # 验证 event_type 分类是服务端行为）

每窗口轮换订阅：显式传 window_start 取窗口市场（边界竞态教训，见
crypto_5m.py），窗口结束后追加一条 WindowMeta 再进入下一窗口。
RTDS 流与窗口无关，进程生命周期内常驻。Ctrl-C 优雅退出。

用法::

    pm-record --symbols btc,eth [--out runtime/ticks] [--raw-sample 1]
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import time

from pm_arb.data.crypto_5m import (
    WINDOW_SECONDS,
    current_window_start,
    get_window_market,
    up_down_tokens,
)
from pm_arb.data.feed import MarketDataFeed
from pm_arb.data.gamma import GammaClient
from pm_arb.data.models import Market
from pm_arb.data.recorder import JsonlWriter, TickRecorder
from pm_arb.data.rtds import RtdsTwapFeed
from pm_arb.infra.logging import get_logger

log = get_logger(__name__)

# 窗口起点前 N 秒开始引导（查 Gamma + 建订阅），降低窗口头部空窗
LEAD_SECONDS = 8.0
# 窗口结束后再多录 N 秒（吃尾部结算前的最后更新）
TAIL_SECONDS = 5.0
# 原始帧落盘截断长度（验证 event_type 分类足够，避免 book 快照撑爆磁盘）
RAW_FRAME_MAX = 8000


class _Pump:
    """把 async generator 喂进任务的壳：cancelled 时静默退出。"""

    def __init__(self, agen_factory):
        self._factory = agen_factory

    async def run(self) -> None:
        agen = self._factory()
        try:
            async for _ in agen:
                pass
        finally:
            with contextlib.suppress(Exception):
                await agen.aclose()


async def record_until(feeds: list[MarketDataFeed], deadline: float) -> None:
    """并发运行所有 feed 直到窗口截止，随后取消任务。"""
    tasks = [asyncio.create_task(_Pump(f.run).run()) for f in feeds]
    try:
        await asyncio.sleep(max(0.0, deadline - time.time()))
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def run(symbols: list[str], out_dir: str, raw_sample: int) -> int:
    market_rec = TickRecorder(out_dir)
    rtds_writers = {s: JsonlWriter(out_dir, f"rtds_{s}") for s in symbols}
    raw_ws = JsonlWriter(out_dir, "raw_ws")
    rtds_tasks: list[asyncio.Task] = []
    try:
        async with GammaClient() as gamma:
            # RTDS 常驻任务（结算同源 TWAP，与窗口无关）
            for s in symbols:
                w = rtds_writers[s]

                def rtds_sink(data: dict, _w: JsonlWriter = w) -> None:
                    _w.write({"type": "RtdsUpdate", "payload": data.get("payload")})

                feed = RtdsTwapFeed(s, on_raw=rtds_sink)
                rtds_tasks.append(asyncio.create_task(feed.run()))
            log.info("record_started", symbols=symbols, out=out_dir)

            raw_counter = 0

            def raw_frame_sink(raw: str) -> None:
                nonlocal raw_counter
                raw_counter += 1
                if raw_sample > 1 and raw_counter % raw_sample != 1:
                    return
                raw_ws.write({"type": "RawFrame", "frame": raw[:RAW_FRAME_MAX],
                              "truncated": len(raw) > RAW_FRAME_MAX})

            def conn_sink(etype: str, detail: dict) -> None:
                market_rec.record_raw(etype, {"detail": dict(detail)})

            while True:
                # ---- 对齐到窗口起点 ----
                ws_start = current_window_start()
                if time.time() - ws_start > LEAD_SECONDS:
                    # 当前窗口已进行超过引导期 → 等下一个完整窗口
                    ws_start += WINDOW_SECONDS
                    partial = False
                    await asyncio.sleep(ws_start - LEAD_SECONDS - time.time())
                else:
                    # 刚开窗不久，直接录剩余段（partial 标注）
                    partial = True
                deadline = ws_start + WINDOW_SECONDS + TAIL_SECONDS

                # ---- 窗口发现（起点后重试；显式传 ws，边界竞态教训）----
                markets: dict[str, Market] = {}
                for s in symbols:
                    for _ in range(6):
                        try:
                            m = await get_window_market(gamma, s, ws_start)
                        except Exception as e:
                            log.warning("record_window_lookup_failed",
                                        symbol=s, error=str(e)[:100])
                            m = None
                        if m is not None:
                            markets[s] = m
                            break
                        await asyncio.sleep(5)

                feeds: list[MarketDataFeed] = []
                meta_syms: dict[str, list[str]] = {}
                for s, m in markets.items():
                    up, down = up_down_tokens(m)
                    meta_syms[s] = [up, down]
                    feeds.append(MarketDataFeed(
                        [up, down], recorder=market_rec,
                        on_conn_event=conn_sink, on_raw_frame=raw_frame_sink,
                    ))

                market_rec.record_raw("WindowMeta", {
                    "window_start": ws_start, "partial": partial,
                    "symbols": meta_syms,
                    "missing": [s for s in symbols if s not in markets],
                })
                log.info("record_window", window_start=ws_start, partial=partial,
                         found=list(markets))

                # ---- 录满一窗口 ----
                await record_until(feeds, deadline)
                market_rec.flush()
                for w in rtds_writers.values():
                    w.flush()
                raw_ws.flush()
                log.info("record_window_done", window_start=ws_start,
                         market_lines=market_rec.lines_written)
    finally:
        for t in rtds_tasks:
            t.cancel()
        await asyncio.gather(*rtds_tasks, return_exceptions=True)
        market_rec.close()
        for w in rtds_writers.values():
            w.close()
        raw_ws.close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="pm-record：5m 市场行情 + RTDS TWAP 纯录制（不下单）"
    )
    parser.add_argument("--symbols", default="btc,eth",
                        help="逗号分隔币种（默认 btc,eth）")
    parser.add_argument("--out", default="runtime/ticks", help="落盘目录")
    parser.add_argument("--raw-sample", type=int, default=1,
                        help="原始 WS 帧抽样：每 N 帧录 1（默认 1=全录）")
    args = parser.parse_args()
    if args.raw_sample < 1:
        parser.error("--raw-sample 必须 >= 1")
    symbols = [s.strip().lower() for s in args.symbols.split(",") if s.strip()]
    if not symbols:
        parser.error("--symbols 不能为空")

    # Ctrl-C：KeyboardInterrupt 穿透 asyncio.run，run() 的 finally 负责取消
    # 常驻 RTDS 任务并 flush/close 全部落盘文件（丢最多一个缓冲区行）
    return asyncio.run(run(symbols, args.out, args.raw_sample))


if __name__ == "__main__":
    raise SystemExit(main())
