"""tick 数据录制器：把行情事件追加写入按天分片的 JSONL 文件。

数据是量化团队的核心资产——回测、滑点建模、腿失败率校准都依赖它。
格式：每行一个 JSON 对象，含 UTC 时间戳、事件类型与原始内容。

文件布局::

    runtime/ticks/2026-09-08_market.jsonl   # 盘口流（typed WS 事件）
    runtime/ticks/2026-09-12_rtds_btc.jsonl # RTDS TWAP 流
    runtime/ticks/2026-09-12_raw_ws.jsonl   # 原始 WS 帧（抽样）
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from pm_arb.data.models import WsBookEvent, WsLastTrade, WsPriceChange
from pm_arb.infra.logging import get_logger

log = get_logger(__name__)


class JsonlWriter:
    """单一流按天分片 JSONL 写手（append-only，UTC 日界自动滚动）。

    pm-record 的三路流（market/rtds_*/raw_ws）各用一个实例；
    TickRecorder 也复用它，行为与旧版完全一致。
    """

    def __init__(self, out_dir: str | Path, stream: str):
        self._dir = Path(out_dir)
        self._stream = stream
        self._dir.mkdir(parents=True, exist_ok=True)
        self._fh = None
        self._current_day = ""
        self.lines_written = 0

    # 每写 N 行刷盘一次（24/7 挂机抗崩溃 + 外部进程可见性）
    FLUSH_EVERY = 50

    def _path_for_today(self) -> Path:
        day = datetime.now(UTC).strftime("%Y-%m-%d")
        return self._dir / f"{day}_{self._stream}.jsonl"

    def _ensure_file(self) -> None:
        day = datetime.now(UTC).strftime("%Y-%m-%d")
        if day != self._current_day or self._fh is None:
            if self._fh is not None:
                self._fh.close()
            self._current_day = day
            # 长持有句柄是录制器的核心语义（append-only 跨整日），不适用上下文管理器
            self._fh = open(self._path_for_today(), "a", encoding="utf-8")  # noqa: SIM115
            log.info("recorder_file_opened", path=str(self._path_for_today()))

    def write(self, payload: dict) -> None:
        """追加一行：recv_ts + 任意 payload 字段。

        每 FLUSH_EVERY 行强制 flush：录制器活着时未刷出的数据对其他
        进程不可见（实测 8KB 用户态缓冲可滞留数分钟），周期性 flush
        保证 tail/外部检查与崩溃时的数据完整性。
        """
        self._ensure_file()
        row = {"recv_ts": datetime.now(UTC).isoformat(timespec="microseconds"), **payload}
        assert self._fh is not None
        self._fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
        self.lines_written += 1
        if self.lines_written % self.FLUSH_EVERY == 0:
            self._fh.flush()

    def flush(self) -> None:
        if self._fh is not None:
            self._fh.flush()

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None
        log.info("recorder_closed", stream=self._stream, lines=self.lines_written)

    def __enter__(self) -> JsonlWriter:
        return self

    def __exit__(self, *exc) -> None:
        self.close()


class TickRecorder:
    def __init__(self, out_dir: str | Path = "runtime/ticks"):
        self._w = JsonlWriter(out_dir, "market")
        self.lines_written = 0

    def record(self, event: WsBookEvent | WsPriceChange | WsLastTrade) -> None:
        """追加录制一条 typed WS 事件。"""
        self.record_raw(type(event).__name__, {"event": event.model_dump(mode="json")})

    def record_raw(self, etype: str, payload: dict) -> None:
        """追加录制一条任意类型事件（RestBook / WsReconnect 等无 pydantic 模型的）。"""
        self._w.write({"type": etype, **payload})
        self.lines_written = self._w.lines_written

    def flush(self) -> None:
        self._w.flush()

    def close(self) -> None:
        self._w.close()
        self.lines_written = self._w.lines_written

    def __enter__(self) -> TickRecorder:
        return self

    def __exit__(self, *exc) -> None:
        self.close()
