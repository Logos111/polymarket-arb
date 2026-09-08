"""tick 录制器测试。"""

import json
from decimal import Decimal

from pm_arb.data.models import WsPriceChange
from pm_arb.data.recorder import TickRecorder


def test_recorder_writes_jsonl(tmp_path):
    rec = TickRecorder(out_dir=tmp_path)
    rec.record(
        WsPriceChange(
            asset_id="tok1",
            side="BUY",
            price=Decimal("0.61"),
            size=Decimal("50"),
            timestamp="1700000000",
        )
    )
    rec.close()

    files = list(tmp_path.glob("*.jsonl"))
    assert len(files) == 1
    lines = files[0].read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    rec0 = json.loads(lines[0])
    assert rec0["type"] == "WsPriceChange"
    assert rec0["event"]["asset_id"] == "tok1"
    assert "recv_ts" in rec0
