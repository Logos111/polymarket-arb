"""SQLite 结构层（DEV_PLAN 阶段 2）：orders / windows / positions 三表。

设计纪律（来自计划与阶段 0/1 教训）：
- WAL 模式（多进程读写：交易进程写、pm-backfill/pm-redeem 读）；
- Decimal 一律存 TEXT（SQLite 无 decimal，FLOAT 会引入二进制误差）；
- **成交比例用 filled_size × 成交价 / 目标名义计算，不依赖 status 字段**
  （问题 4a：市价单 size=0 时 FILLED 判定不可信，双保险纪律）；
- 一窗口至多一笔持仓 → positions 以 (symbol, window_start) 为主键，
  与 windows 表同键对应，重启恢复/结算回填都按此键寻址；
- settlement_outcome 存持仓视角的 'win'/'lose'；market_winner 存市场视角
  （'Up'/'Down'）——无交易窗口也能由 pm-backfill 回填，作回测样本放大器。
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from pm_arb.execution.orders import Order
from pm_arb.infra.logging import get_logger

log = get_logger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    client_id      TEXT PRIMARY KEY,
    exchange_id    TEXT,
    mode           TEXT NOT NULL,            -- 'live' / 'paper'
    symbol         TEXT,
    window_start   INTEGER,
    token_id       TEXT NOT NULL,
    side           TEXT NOT NULL,
    order_type     TEXT NOT NULL,
    price          TEXT NOT NULL,
    size           TEXT NOT NULL,
    filled_size    TEXT NOT NULL,
    avg_fill_price TEXT,
    status         TEXT NOT NULL,
    error          TEXT,
    target_notional TEXT,
    fill_ratio     TEXT,                     -- 成交额/目标名义（不依赖 status）
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS windows (
    symbol        TEXT NOT NULL,
    window_start  INTEGER NOT NULL,
    slug          TEXT,
    question      TEXT,
    condition_id  TEXT,
    entered       INTEGER NOT NULL DEFAULT 0,
    side          TEXT,
    entry_price   TEXT,
    filled_size   TEXT,
    entry_cost    TEXT,                      -- price × filled_size（不含 fee）
    entry_fee     TEXT,                      -- taker fee = 0.07×p×(1−p)×份数
    tp_hit        INTEGER NOT NULL DEFAULT 0,
    stop_hit      INTEGER NOT NULL DEFAULT 0,
    exit_price    TEXT,
    realized_pnl  TEXT,                      -- 已平仓实现盈亏（费后）
    market_winner TEXT,                      -- 市场视角结算赢家 'Up'/'Down'
    settlement    TEXT,                      -- 持仓视角 'win'/'lose'
    settle_pnl    TEXT,                      -- 结算盈亏（费后，win/lose 时回填）
    redeemed      INTEGER NOT NULL DEFAULT 0,
    updated_at    TEXT NOT NULL,
    PRIMARY KEY (symbol, window_start)
);
CREATE TABLE IF NOT EXISTS positions (
    symbol        TEXT NOT NULL,
    window_start  INTEGER NOT NULL,
    token_id      TEXT NOT NULL,
    condition_id  TEXT,
    side          TEXT NOT NULL,
    entry_price   TEXT NOT NULL,
    filled_size   TEXT NOT NULL,
    cost          TEXT NOT NULL,             -- price × filled_size
    fee           TEXT NOT NULL DEFAULT '0',
    status        TEXT NOT NULL,             -- 'open' / 'closed' / 'settled'
    realized_pnl  TEXT,                      -- 平仓或结算后的最终盈亏（费后）
    redeemed      INTEGER NOT NULL DEFAULT 0,
    updated_at    TEXT NOT NULL,
    PRIMARY KEY (symbol, window_start)
);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _d(v: Decimal | None) -> str | None:
    return None if v is None else str(v)


def _rows(cur: sqlite3.Cursor) -> list[dict]:
    """cursor → list[dict]（列名取自 description）。"""
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]


class Store:
    """三表结构层。线程约束：SQLite 连接不跨线程，调用方在 owning 线程使用。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # ---- orders（Broker on_order 钩子统一写入点）----

    def upsert_order(
        self,
        order: Order,
        *,
        mode: str,
        symbol: str | None = None,
        window_start: int | None = None,
        target_notional: Decimal | None = None,
    ) -> None:
        """整单快照 upsert（on_order 每次回调覆盖写，终态自然覆盖中间态）。

        fill_ratio = 成交额 / 目标名义：BUY 目标为美元名义、SELL 目标为
        份数——两者都用成交金额口径（filled_size × avg_fill_price）。
        """
        filled_amt = order.filled_size * (order.avg_fill_price or Decimal(0))
        target_amt: Decimal | None = None
        if target_notional is not None and target_notional > 0:
            target_amt = target_notional
        ratio = (filled_amt / target_amt) if target_amt else None
        self.conn.execute(
            """INSERT INTO orders (client_id, exchange_id, mode, symbol, window_start,
                 token_id, side, order_type, price, size, filled_size, avg_fill_price,
                 status, error, target_notional, fill_ratio, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(client_id) DO UPDATE SET
                 exchange_id=excluded.exchange_id, status=excluded.status,
                 filled_size=excluded.filled_size, avg_fill_price=excluded.avg_fill_price,
                 error=excluded.error, fill_ratio=excluded.fill_ratio,
                 updated_at=excluded.updated_at""",
            (
                order.client_id, order.exchange_id, mode, symbol, window_start,
                order.token_id, order.side.value, order.order_type.value,
                _d(order.price), _d(order.size), _d(order.filled_size),
                _d(order.avg_fill_price), order.status.value, order.error,
                _d(target_notional), _d(ratio), _now(), _now(),
            ),
        )
        self.conn.commit()

    # ---- windows ----

    def upsert_window(self, symbol: str, window_start: int, **fields: Any) -> None:
        """部分更新：只写传入列（slug/question/entered/side/.../settle_pnl）。"""
        allowed = {
            "slug", "question", "condition_id", "entered", "side", "entry_price",
            "filled_size", "entry_cost", "entry_fee", "tp_hit", "stop_hit",
            "exit_price", "realized_pnl", "market_winner", "settlement",
            "settle_pnl", "redeemed",
        }
        cols, vals = [], []
        for k, v in fields.items():
            if k not in allowed:
                raise ValueError(f"upsert_window 不支持列 {k}")
            cols.append(k)
            vals.append(int(v) if isinstance(v, bool) else (_d(v) if isinstance(v, Decimal) else v))
        cols += ["updated_at"]
        vals += [_now()]
        all_cols = ["symbol", "window_start", *cols]
        placeholders = ", ".join(["?"] * len(all_cols))
        # upsert：PK 冲突时更新传入列
        assignments = ", ".join(f"{c}=excluded.{c}" for c in cols)
        self.conn.execute(
            f"INSERT INTO windows ({', '.join(all_cols)}) VALUES ({placeholders}) "
            f"ON CONFLICT(symbol, window_start) DO UPDATE SET {assignments}",
            (symbol, window_start, *vals),
        )
        self.conn.commit()

    # ---- positions（持仓持久化：修复重启丢持仓）----

    def open_position(
        self, *, symbol: str, window_start: int, token_id: str, condition_id: str | None,
        side: str, entry_price: Decimal, filled_size: Decimal,
        fee: Decimal = Decimal(0),
    ) -> None:
        cost = entry_price * filled_size
        self.conn.execute(
            """INSERT INTO positions (symbol, window_start, token_id, condition_id, side,
                 entry_price, filled_size, cost, fee, status, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,'open',?)
               ON CONFLICT(symbol, window_start) DO UPDATE SET
                 status='open', updated_at=excluded.updated_at""",
            (symbol, window_start, token_id, condition_id, side, _d(entry_price),
             _d(filled_size), _d(cost), _d(fee), _now()),
        )
        self.conn.commit()

    def close_position(
        self, *, symbol: str, window_start: int, exit_price: Decimal,
        realized_pnl: Decimal,
    ) -> None:
        """止盈/止损平仓：position → closed，窗口行同步 tp/exit/pnl。"""
        self.conn.execute(
            """UPDATE positions SET status='closed', realized_pnl=?, updated_at=?
               WHERE symbol=? AND window_start=?""",
            (_d(realized_pnl), _now(), symbol, window_start),
        )
        self.conn.commit()

    def settle_position(
        self, *, symbol: str, window_start: int, won: bool, realized_pnl: Decimal,
    ) -> None:
        self.conn.execute(
            """UPDATE positions SET status='settled', realized_pnl=?, updated_at=?
               WHERE symbol=? AND window_start=?""",
            (_d(realized_pnl), _now(), symbol, window_start),
        )
        self.conn.commit()

    def open_positions(self, symbol: str | None = None) -> list[dict]:
        sql = "SELECT * FROM positions WHERE status='open'"
        args: tuple = ()
        if symbol:
            sql += " AND symbol=?"
            args = (symbol,)
        sql += " ORDER BY window_start"
        return _rows(self.conn.execute(sql, args))

    def pending_settlement(self, *, now_ts: int, grace_sec: int = 360) -> list[dict]:
        """窗口结束超过 grace 且未回填结算的持仓（结算回填任务的输入）。

        grace 默认 360s：窗口结束 + 结算延迟（Gamma closed 通常 1~5 分钟）。
        """
        cur = self.conn.execute(
            """SELECT * FROM positions
               WHERE status='open' AND window_start + 300 + ? <= ?
               ORDER BY window_start""",
            (grace_sec, now_ts),
        )
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]

    def mark_redeemed(self, *, symbol: str, window_start: int) -> None:
        self.conn.execute(
            "UPDATE positions SET redeemed=1, updated_at=? WHERE symbol=? AND window_start=?",
            (_now(), symbol, window_start),
        )
        self.conn.execute(
            "UPDATE windows SET redeemed=1, updated_at=? WHERE symbol=? AND window_start=?",
            (_now(), symbol, window_start),
        )
        self.conn.commit()
