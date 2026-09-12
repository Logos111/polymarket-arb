"""ticks 压实模块测试：扁平化纯函数全覆盖 + duckdb 集成（可用时）。"""

import importlib.util
import json

import pytest

from pm_arb.data.ticks_compact import compact_file, flatten_event

HAS_DUCKDB = importlib.util.find_spec("duckdb") is not None


def book_event() -> dict:
    return {
        "recv_ts": "2026-09-12T06:39:55.396775+00:00",
        "type": "WsBookEvent",
        "event": {
            "asset_id": "0xAAA", "market": "0xM1",
            "bids": [{"price": "0.01", "size": "93575.59"},
                     {"price": "0.02", "size": "19761.15"}],
            "asks": [{"price": "0.75", "size": "5"}],
        },
    }


def test_flatten_book_event() -> None:
    brows, mrows = flatten_event(book_event())
    assert not mrows
    assert len(brows) == 3
    kinds = {r["kind"] for r in brows}
    assert kinds == {"book"}
    bid = [r for r in brows if r["side"] == "bid"]
    ask = [r for r in brows if r["side"] == "ask"]
    assert {r["price"] for r in bid} == {0.01, 0.02}
    assert ask[0]["price"] == 0.75 and ask[0]["size"] == 5.0
    assert all(r["asset_id"] == "0xAAA" and r["market"] == "0xM1" for r in brows)


def test_flatten_price_change() -> None:
    e = {
        "recv_ts": "2026-09-12T06:40:00+00:00",
        "type": "WsPriceChange",
        "event": {"asset_id": "0xAAA", "market": "0xM1", "side": "BUY",
                  "price": "0.28", "size": "0"},
    }
    brows, mrows = flatten_event(e)
    assert not mrows and len(brows) == 1
    r = brows[0]
    assert r["kind"] == "pchange" and r["side"] == "bid"
    assert r["price"] == 0.28 and r["size"] == 0.0


def test_flatten_rest_book_dict_form() -> None:
    e = {
        "recv_ts": "2026-09-12T06:39:53.791578+00:00",
        "type": "RestBook",
        "event": {"asset_id": "0xBBB", "bids": {"0.01": "97582.02"},
                  "asks": {"0.99": "5"}},
    }
    brows, mrows = flatten_event(e)
    assert not mrows and len(brows) == 2
    assert {r["kind"] for r in brows} == {"restbook"}
    bid = next(r for r in brows if r["side"] == "bid")
    assert bid["price"] == 0.01 and bid["size"] == 97582.02
    assert bid["market"] == ""


def test_flatten_last_trade_keeps_side_semantics() -> None:
    e = {
        "recv_ts": "2026-09-12T06:39:55.594675+00:00",
        "type": "WsLastTrade",
        "event": {"asset_id": "0xAAA", "market": "0xM1", "price": "0.75",
                  "size": "5", "side": "BUY", "timestamp": "1789195196446",
                  "fee_rate_bps": "0"},
    }
    brows, mrows = flatten_event(e)
    assert not mrows and len(brows) == 1
    # trade 的 side 是 taker 方向，保留原始 BUY/SELL，不与 bid/ask 混用
    assert brows[0]["kind"] == "trade" and brows[0]["side"] == "BUY"


def test_flatten_meta_events_fallthrough() -> None:
    for etype, payload in (
        ("WindowMeta", {"window_start": 1789195200, "partial": False}),
        ("WsReconnect", {"detail": {}}),
        ("SomethingNew", {"x": 1}),
    ):
        e = {"recv_ts": "2026-09-12T06:39:53.230723+00:00", "type": etype, **payload}
        brows, mrows = flatten_event(e)
        assert not brows
        assert len(mrows) == 1
        assert mrows[0]["type"] == etype
        back = json.loads(mrows[0]["payload"])
        assert "recv_ts" not in back and "type" not in back


def test_flatten_sell_price_change_maps_ask() -> None:
    e = {
        "recv_ts": "2026-09-12T07:00:00+00:00",
        "type": "WsPriceChange",
        "event": {"asset_id": "0xAAA", "market": "0xM1", "side": "SELL",
                  "price": "0.72", "size": "100"},
    }
    brows, _ = flatten_event(e)
    assert brows[0]["side"] == "ask"


@pytest.mark.skipif(not HAS_DUCKDB, reason="duckdb 仅装在 <3.14 解释器")
def test_compact_file_end_to_end(tmp_path) -> None:
    src = tmp_path / "2026-09-12_market.jsonl"
    lines = [
        json.dumps(book_event()),
        json.dumps({"recv_ts": "2026-09-12T06:39:53.230723+00:00",
                    "type": "WindowMeta", "window_start": 1789195200}),
    ]
    src.write_text("\n".join(lines) + "\n", encoding="utf-8")

    dst = tmp_path / "pq"
    r = compact_file(src, dst)
    assert r is not None and r.verified and r.deleted
    assert not src.exists()
    assert r.n_lines == 2 and r.n_bad == 0
    assert r.n_book_rows == 3 and r.n_meta_rows == 1

    import duckdb
    con = duckdb.connect()
    got = con.execute(
        f"SELECT count(*) FROM '{r.book_path.as_posix()}'"
    ).fetchone()[0]
    assert got == 3
    meta = con.execute(
        f"SELECT type FROM '{r.meta_path.as_posix()}'"
    ).fetchall()
    assert meta == [("WindowMeta",)]
    con.close()

    # 幂等：目标存在时再次扫描返回 None
    assert compact_file(src, dst) is None


@pytest.mark.skipif(not HAS_DUCKDB, reason="duckdb 仅装在 <3.14 解释器")
def test_compact_file_bad_line_blocks_delete(tmp_path) -> None:
    src = tmp_path / "2026-09-11_market.jsonl"
    lines = [json.dumps(book_event()), "{broken"]
    src.write_text("\n".join(lines) + "\n", encoding="utf-8")

    r = compact_file(src, tmp_path / "pq")
    assert r is not None
    assert r.n_bad == 1 and not r.deleted  # 坏行阻止删源
    assert src.exists()


@pytest.mark.skipif(not HAS_DUCKDB, reason="duckdb 仅装在 <3.14 解释器")
def test_compact_file_keep_preserves_source(tmp_path) -> None:
    src = tmp_path / "2026-09-10_market.jsonl"
    src.write_text(json.dumps(book_event()) + "\n", encoding="utf-8")
    r = compact_file(src, tmp_path / "pq", keep=True)
    assert r is not None and r.verified and not r.deleted
    assert src.exists()
