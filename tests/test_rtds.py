"""RtdsTwapFeed 单元测试：消息解析、E18 精度、窗口统计与协议细节。

重点保障（对应既往教训）：
- ``full_accuracy_value``（E18 定点）必须精确转为 Decimal，不得用浮点展示值；
- 非目标 symbol 的消息必须被过滤；
- base（结算参照的窗口起点价）取首个观测值；
- 订阅帧 filters 必须是紧凑 JSON（无空格），否则服务端拒订。
"""

import asyncio
import contextlib
import json
from decimal import Decimal

from pm_arb.data.rtds import RtdsTwapFeed


def _msg(value: float = 65000.5, fav: str = "65000500000000000000000",
         symbol: str = "btc/usd", topic: str = "crypto_prices_twap_sixty") -> str:
    import json

    return json.dumps({
        "topic": topic,
        "type": "update",
        "timestamp": 1789052700123,
        "payload": {
            "symbol": symbol,
            "value": value,
            "full_accuracy_value": fav,
            "timestamp": 1789052700000,
            "window_s": 60,
        },
    })


def test_parse_full_accuracy_value_e18():
    """E18 定点精确值优先，且转换为精确 Decimal（非浮点）。"""
    f = RtdsTwapFeed("btc")
    price = f._handle_message(_msg())
    assert price == Decimal("65000.5")
    assert f.last == Decimal("65000.5")


def test_parse_falls_back_to_display_value():
    """缺失 full_accuracy_value 时退回 value 展示值。"""
    raw = json.dumps({"payload": {"symbol": "btc/usd", "value": 65001.25}})
    f = RtdsTwapFeed("btc")
    assert f._handle_message(raw) == Decimal("65001.25")


def test_filters_other_symbols():
    f = RtdsTwapFeed("btc")
    assert f._handle_message(_msg(symbol="eth/usd")) is None
    assert f.samples == 0
    assert f.last is None


def test_base_is_first_observation_and_window_stats():
    """base 取首个观测（结算规则的窗口起点参照）；high/low 正确累积。"""
    f = RtdsTwapFeed("btc")
    f._handle_message(_msg(fav="65000000000000000000000"))   # 65000
    f._handle_message(_msg(fav="65050000000000000000000"))   # 65050
    f._handle_message(_msg(fav="64980000000000000000000"))   # 64980
    assert f.base == Decimal("65000")
    assert f.high == Decimal("65050")
    assert f.low == Decimal("64980")
    assert f.price_range == Decimal("70")
    assert f.samples == 3
    assert f.seq == 3


def test_reset_window_keeps_last_as_new_base():
    f = RtdsTwapFeed("btc")
    f._handle_message(_msg(fav="65000000000000000000000"))
    f._handle_message(_msg(fav="65100000000000000000000"))
    f.reset_window()
    # 新窗口 base 继承上一窗口最后值；统计清零
    assert f.base == Decimal("65100")
    assert f.high is None and f.low is None
    assert f.samples == 0


def test_freshness_semantics():
    """从未收到 → age=+inf / is_fresh=False；收到后立即新鲜。"""
    f = RtdsTwapFeed("btc")
    assert f.age() == float("inf")
    assert f.is_fresh(10.0) is False
    f._handle_message(_msg())
    assert f.age() < 1.0
    assert f.is_fresh(10.0) is True


def test_bad_payload_counts_error_and_is_ignored():
    f = RtdsTwapFeed("btc")
    # 非 JSON 文本帧（如服务端回的 PONG）：静默跳过，不计错误
    assert f._handle_message("PONG") is None
    assert f._handle_message("not json") is None
    assert f.errors == 0
    # 以 { 开头但内容坏：计入解析错误；合法 JSON 但无目标数据：静默忽略
    assert f._handle_message("{broken json") is None
    assert f._handle_message('{"payload": null}') is None
    assert f.errors == 1
    assert f.last is None


def test_invalid_window_seconds_rejected():
    try:
        RtdsTwapFeed("btc", window_seconds=45)
    except ValueError as e:
        assert "window_seconds" in str(e)
    else:
        raise AssertionError("应拒绝不支持的回看窗口")


async def test_subscription_frame_is_compact_json(monkeypatch):
    """订阅帧 filters 必须是紧凑 JSON（文档强制），PING 心跳必须启动。"""
    from pm_arb.data import rtds as rtds_mod

    sent: list[str] = []

    class _FakeWs:
        async def send(self, payload: str) -> None:
            sent.append(payload)

        async def recv(self) -> str:
            await asyncio.sleep(3600)  # 永不推数据 → 触发看门狗
            return ""  # pragma: no cover

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    f = RtdsTwapFeed("btc", idle_timeout=6.5)
    monkeypatch.setattr(rtds_mod.websockets, "connect", lambda *a, **k: _FakeWs())

    async def _drive() -> None:
        await f.run(asyncio.Event())  # stop 未置位 → 只能靠取消退出

    task = asyncio.create_task(_drive())
    try:
        # 等过首个 PING 时刻（5s）但未到看门狗（6.5s），验证心跳已发出
        await asyncio.sleep(6)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    sub_frames = [s for s in sent if s.startswith("{")]
    assert sub_frames, "应发送订阅帧"
    sub = json.loads(sub_frames[0])
    flt = sub["subscriptions"][0]["filters"]
    assert flt == '{"symbol":"btc/usd"}', "filters 必须紧凑无空格"
    assert "PING" in sent, "应用层心跳 PING 必须周期发送"
