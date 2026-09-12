"""窗口数据上下文（阶段 1 commit A：数据访问搬运聚合）。

:class:`WindowDataHub` 聚合窗口监测所需的三个数据面：

- 双边盘口：WS 本地簿优先（新鲜度判定），未就绪/陈旧/断流回退 REST 快照；
- 结算同源喂价：RTDS Chainlink TWAP-60s（含窗口高低点统计）；
- 时钟：``clock`` 可注入（回测虚拟时钟），默认 wall clock。

纯数据访问，不做策略判定（判定在 decisions.py）。
"""

from __future__ import annotations

import time
from collections.abc import Callable

from pm_arb.data.clob_rest import ClobRestClient
from pm_arb.data.feed import MarketDataFeed
from pm_arb.data.rtds import RtdsTwapFeed


async def fetch_raw_market(slug: str, s) -> dict | None:
    """Gamma 原始市场 JSON（endDate/orderMinSize 等未入模型字段）。"""
    import httpx

    async with httpx.AsyncClient(timeout=s.http_timeout, proxy=s.proxy_url or None) as c:
        r = await c.get(f"{s.gamma_api_url}/markets", params={"slug": slug})
        r.raise_for_status()
        j = r.json()
        return j[0] if j else None


def _book_view(ob) -> dict:
    """OrderBook → 最优价/量视图（空盘口时 best_* 为 None）。"""
    bb, ba = ob.best_bid, ob.best_ask
    return {
        "best_bid": bb.price if bb else None,
        "best_ask": ba.price if ba else None,
        "bid_size": bb.size if bb else None,
        "ask_size": ba.size if ba else None,
    }


class WindowDataHub:
    """单窗口的数据访问聚合（REST 兜底 + WS 优先 + 喂价）。"""

    def __init__(
        self,
        feed: MarketDataFeed,
        rtds: RtdsTwapFeed,
        rest: ClobRestClient,
        up: str,
        down: str,
        *,
        ws_fresh_sec: float = 10.0,
        clock: Callable[[], float] = time.time,
    ):
        self._feed = feed
        self._rtds = rtds
        self._rest = rest
        self.up, self.down = up, down
        self._ws_fresh_sec = ws_fresh_sec
        self.clock = clock
        self.last_error: str | None = None  # 最近一次 REST 兑底失败的异常摘要

    async def get_books(self) -> tuple[dict[str, dict], str] | None:
        """双边最优价视图 + 数据源标记（"WS"/"REST"）；REST 也失败返回 None。"""
        b = self.books_from_ws()
        if b is not None:
            self.last_error = None
            return b, "WS"
        try:
            self.last_error = None
            return await self.books_from_rest(), "REST"
        except Exception as e:
            self.last_error = str(e)
            return None

    def books_from_ws(self) -> dict[str, dict] | None:
        """WS 本地簿实时最优价；任一簿未就绪/陈旧返回 None（触发 REST 兜底）。

        仅凭 ready 单向锁无法识别"WS 静默但连接未断"的冻结盘口，
        必须叠加新鲜度判定（僵尸盘口教训，见 pm-arb 架构记忆）。
        """
        out: dict[str, dict] = {}
        for name, tid in (("Up", self.up), ("Down", self.down)):
            ob = self._feed.books().get(tid)
            if ob is None or not ob.is_fresh(self._ws_fresh_sec):
                return None
            out[name] = _book_view(ob)
        return out

    async def books_from_rest(self) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for name, tid in (("Up", self.up), ("Down", self.down)):
            out[name] = _book_view(await self._rest.get_book(tid))
        return out

    def book_age(self) -> float:
        """双边本地簿龄的最大值（inf 表示从未更新）。"""
        up_ob = self._feed.books().get(self.up)
        dn_ob = self._feed.books().get(self.down)
        return max(up_ob.age() if up_ob else float("inf"),
                   dn_ob.age() if dn_ob else float("inf"))

    # ---- 喂价透传（保持行为不变的最薄封装） ----

    @property
    def rtds(self) -> RtdsTwapFeed:
        return self._rtds
