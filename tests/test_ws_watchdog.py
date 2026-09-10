"""ws.py 空闲看门狗回归测试。

复现场景：TCP 连接与 ping/pong 都正常，但订阅通道静默失效——服务端不再推
任何 book/price_change。纯 ``ping_timeout`` 检测不到这种情况（链路层心跳不
代表业务频道还在推），必须用 ``asyncio.wait_for(ws.recv(), timeout)`` 看门狗。

断言：静默超过 ``ws_idle_timeout`` 后，stream() 主动判定断连并触发
``on_disconnected`` 回调（feed 层借此 invalidate 本地簿 → 上层回退 REST）。
"""

import asyncio
import contextlib

from pm_arb.data import ws as ws_mod
from pm_arb.data.ws import MarketWsClient
from pm_arb.infra.config import Settings


class _SilentWs:
    """send 正常、recv 永久阻塞，模拟"连接在但服务端不再推数据"。"""

    def __init__(self) -> None:
        self.subscriptions: list[str] = []

    async def send(self, payload: str) -> None:
        self.subscriptions.append(payload)

    async def recv(self) -> str:
        # 永不返回：逼看门狗在 ws_idle_timeout 后超时
        await asyncio.sleep(3600)
        return ""  # pragma: no cover

    async def __aenter__(self) -> "_SilentWs":
        return self

    async def __aexit__(self, *exc) -> bool:
        return False


async def test_idle_watchdog_fires_on_silent_connection(monkeypatch):
    settings = Settings(ws_idle_timeout=0.2)  # 极短阈值加速测试
    client = MarketWsClient(settings)
    fake = _SilentWs()
    # 用假的静默连接替换 websockets.connect（返回异步上下文管理器）
    monkeypatch.setattr(ws_mod.websockets, "connect", lambda *a, **k: fake)

    reconnected = asyncio.Event()
    disconnected = asyncio.Event()

    gen = client.stream(
        ["tok1"],
        on_reconnected=lambda: reconnected.set(),
        on_disconnected=lambda: disconnected.set(),
    )

    # stream() 是惰性异步生成器，必须用任务驱动迭代，回调才会随连接流程触发
    async def _drive() -> None:
        async for _ in gen:
            pass

    task = asyncio.create_task(_drive())
    try:
        # 首次连接成功 → on_reconnected 触发
        await asyncio.wait_for(reconnected.wait(), timeout=2.0)
        # recv 静默 → 看门狗应在 ~ws_idle_timeout 后触发 on_disconnected
        await asyncio.wait_for(disconnected.wait(), timeout=2.0)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        await gen.aclose()

    assert fake.subscriptions, "应在连接后发送订阅消息"


async def test_watchdog_does_not_fire_when_data_flows(monkeypatch):
    """对照：正常推数据时看门狗不应误触发断连。"""
    settings = Settings(ws_idle_timeout=0.5)
    client = MarketWsClient(settings)

    class _ActiveWs(_SilentWs):
        async def recv(self) -> str:
            # 每 0.1s 推一条 book，间隔远小于 idle_timeout
            await asyncio.sleep(0.1)
            return (
                '{"event_type":"book","asset_id":"tok1","market":"m",'
                '"bids":[{"price":"0.4","size":"10"}],"asks":[{"price":"0.6","size":"10"}]}'
            )

    fake = _ActiveWs()
    monkeypatch.setattr(ws_mod.websockets, "connect", lambda *a, **k: fake)

    disconnected = asyncio.Event()
    gen = client.stream(["tok1"], on_disconnected=lambda: disconnected.set())
    got = 0
    try:
        async for _event in gen:
            got += 1
            if got >= 3:
                break
    finally:
        await gen.aclose()

    assert got >= 3, "数据正常流动时应持续收到事件"
    assert not disconnected.is_set(), "有数据时看门狗不应误判为静默断连"
