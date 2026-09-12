"""tick 录制器测试。"""

import json
from decimal import Decimal

from pm_arb.data.models import WsPriceChange
from pm_arb.data.recorder import JsonlWriter, TickRecorder


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


def test_recorder_record_raw(tmp_path):
    """record_raw：无 pydantic 模型的事件（RestBook/WsReconnect 等）落盘。"""
    rec = TickRecorder(out_dir=tmp_path)
    rec.record_raw("RestBook", {"event": {"asset_id": "tok1", "bids": []}})
    rec.record_raw("WindowMeta", {"window_start": 1789192200, "partial": False})
    rec.close()

    f = next(tmp_path.glob("*_market.jsonl"))
    lines = [json.loads(x) for x in f.read_text(encoding="utf-8").strip().splitlines()]
    assert [r["type"] for r in lines] == ["RestBook", "WindowMeta"]
    assert lines[1]["window_start"] == 1789192200


def test_jsonl_writer_streams_are_independent(tmp_path):
    """多流写手各写各的文件，互不干扰（pm-record 三路流）。"""
    w1 = JsonlWriter(tmp_path, "rtds_btc")
    w2 = JsonlWriter(tmp_path, "raw_ws")
    w1.write({"type": "RtdsUpdate", "payload": {"value": 1.0}})
    w2.write({"type": "RawFrame", "frame": "{}"})
    w1.close()
    w2.close()

    names = sorted(p.name.split("_", 1)[1] for p in tmp_path.glob("*.jsonl"))
    assert names == ["raw_ws.jsonl", "rtds_btc.jsonl"]
    rtds = next(tmp_path.glob("*_rtds_btc.jsonl"))
    row = json.loads(rtds.read_text(encoding="utf-8").strip())
    assert row["payload"]["value"] == 1.0


def test_jsonl_writer_appends(tmp_path):
    w = JsonlWriter(tmp_path, "market")
    w.write({"n": 1})
    w.flush()
    w.write({"n": 2})
    w.close()

    f = next(tmp_path.glob("*_market.jsonl"))
    lines = f.read_text(encoding="utf-8").strip().splitlines()
    assert [json.loads(x)["n"] for x in lines] == [1, 2]
