"""tick 数据录制器：把行情事件追加写入按天分片的 JSONL 文件。

数据是量化团队的核心资产——回测、滑点建模、腿失败率校准都依赖它。
格式：每行一个 JSON 对象，含 UTC 时间戳、事件类型与原始内容。

文件布局::

    runtime/ticks/2026-09-08_market.jsonl
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from pm_arb.data.models import WsBookEvent, WsLastTrade, WsPriceChange
from pm_arb.infra.logging import get_logger

log = get_logger(__name__)


class TickRecorder:
    def __init__(self, out_dir: str | Path = "runtime/ticks"):
        self._dir = Path(out_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._fh = None
        self._current_day = ""
        self.lines_written = 0

    def _path_for_today(self) -> Path:
        day = datetime.now(UTC).strftime("%Y-%m-%d")
        return self._dir / f"{day}_market.jsonl"

    def _ensure_file(self) -> None:
        day = datetime.now(UTC).strftime("%Y-%m-%d")
        if day != self._current_day or self._fh is None:
            if self._fh is not None:
                self._fh.close()
            self._current_day = day
            self._fh = open(self._path_for_today(), "a", encoding="utf-8")
            log.info("recorder_file_opened", path=str(self._path_for_today()))

    def record(self, event: WsBookEvent | WsPriceChange | WsLastTrade) -> None:
        """追加录制一条事件。"""
        self._ensure_file()
        etype = type(event).__name__
        record = {
            "recv_ts": datetime.now(UTC).isoformat(timespec="microseconds"),
            "type": etype,
            "event": event.model_dump(mode="json"),
        }
        assert self._fh is not None
        self._fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        self.lines_written += 1

    def flush(self) -> None:
        if self._fh is not None:
            self._fh.flush()

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None
        log.info("recorder_closed", lines=self.lines_written)

    def __enter__(self) -> TickRecorder:
        return self

    def __exit__(self, *exc) -> None:
        self.close()
