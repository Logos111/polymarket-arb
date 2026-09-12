"""market 流 JSONL 压实：整日文件 → 分区 Parquet（DuckDB ZSTD）。

背景与实测（b08 实验，2026-09-13）：全档快照 JSONL 约 3.3 GB/天，
列式 ZSTD 压缩 28.1x（132.6 MB → 4.7 MB）。录制器保持 JSONL 热缓冲
（append-only、周期 flush、崩溃安全），本模块把**已封口**（非今日 UTC）
的整日文件转为 Parquet，行数校验通过后删源。

输出布局（按 UTC 日期分区，文件名日期即 UTC 日）::

    runtime/ticks_parquet/date=YYYY-MM-DD/book.parquet
    runtime/ticks_parquet/date=YYYY-MM-DD/meta.parquet

book 行（行情，按 kind 区分来源）:
    ts TIMESTAMPTZ, kind, asset_id, market, side, price DOUBLE, size DOUBLE
    kind=book      WsBookEvent 全档快照逐档展开（side: bid/ask）
    kind=pchange   WsPriceChange 增量（BUY→bid / SELL→ask）
    kind=restbook  RestBook 字典快照逐档展开
    kind=trade     WsLastTrade（side 保留原始 BUY/SELL 语义）
meta 行（无时序意义的小事件，原样保 JSON）:
    ts TIMESTAMPTZ, type, payload VARCHAR

精度说明：price/size 存 DOUBLE——IEEE754 shortest-repr 往返对两位小数
价格与常规数量字符串无损（"0.01"→0.01→"0.01"），与 HF 数据集口径一致。

duckdb 为延迟导入：本模块可在未安装 duckdb 的解释器下导入（单测覆盖
扁平化纯函数）；实测 Python 3.14 的 cp314 wheel DLL 损坏，运行须用
3.12（见 DEV_PLAN 与 pyproject marker）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

BOOK_KINDS = ("book", "pchange", "restbook", "trade")


@dataclass(frozen=True)
class CompactResult:
    """单文件压实结果。"""

    src: Path
    book_path: Path | None = None
    meta_path: Path | None = None
    n_lines: int = 0
    n_bad: int = 0
    n_book_rows: int = 0
    n_meta_rows: int = 0
    verified: bool = False
    deleted: bool = False


def _side_of_pchange(raw: str) -> str:
    """WsPriceChange.side：BUY 挂在 bid 侧，SELL 挂在 ask 侧。"""
    return "bid" if raw.upper() == "BUY" else "ask"


def flatten_event(e: dict) -> tuple[list[dict], list[dict]]:
    """单事件 → (book_rows, meta_rows)。

    纯函数：不含 IO，也不依赖 duckdb；未知类型一律落 meta 表保底，
    保证压实不丢事件（资产原则）。
    """
    ts = e.get("recv_ts", "")
    etype = e.get("type", "?")
    ev = e.get("event")
    book_rows: list[dict] = []
    if isinstance(ev, dict) and etype in (
        "WsBookEvent", "WsPriceChange", "WsLastTrade", "RestBook",
    ):
        asset = ev.get("asset_id", "")
        market = ev.get("market", "")
        if etype == "WsBookEvent":
            for side in ("bids", "asks"):
                for lvl in ev.get(side) or []:
                    book_rows.append({
                        "ts": ts, "kind": "book", "asset_id": asset,
                        "market": market, "side": "bid" if side == "bids" else "ask",
                        "price": float(lvl["price"]), "size": float(lvl["size"]),
                    })
        elif etype == "WsPriceChange":
            book_rows.append({
                "ts": ts, "kind": "pchange", "asset_id": asset,
                "market": market, "side": _side_of_pchange(ev.get("side", "")),
                "price": float(ev["price"]), "size": float(ev["size"]),
            })
        elif etype == "RestBook":
            for side in ("bids", "asks"):
                for price, size in (ev.get(side) or {}).items():
                    book_rows.append({
                        "ts": ts, "kind": "restbook", "asset_id": asset,
                        "market": market, "side": "bid" if side == "bids" else "ask",
                        "price": float(price), "size": float(size),
                    })
        else:  # WsLastTrade
            book_rows.append({
                "ts": ts, "kind": "trade", "asset_id": asset,
                "market": market, "side": ev.get("side", ""),
                "price": float(ev["price"]), "size": float(ev["size"]),
            })
        return book_rows, []
    # WindowMeta / WsReconnect / WsDisconnected / 未知类型 → meta 保底
    payload = {k: v for k, v in e.items() if k not in ("recv_ts", "type")}
    meta_rows = [{"ts": ts, "type": etype,
                  "payload": json.dumps(payload, ensure_ascii=False, default=str)}]
    return [], meta_rows


def compact_file(
    src: Path, dst_dir: Path, *, keep: bool = False, force: bool = False,
) -> CompactResult | None:
    """压实单个 market JSONL。目标已存在且未 --force 时返回 None（幂等跳过）。

    流程：流式扁平化到临时 JSONL → DuckDB 排序写 Parquet(ZSTD) →
    行数校验 →（非 --keep 且无坏行）删源。任何坏行都会阻止删源，
    但 Parquet 仍然产出（坏行内容打印告警，由人工决定处置）。
    """
    date = src.name.split("_", 1)[0]  # 文件名前缀即 UTC 日期
    book_dst = dst_dir / f"date={date}" / "book.parquet"
    meta_dst = dst_dir / f"date={date}" / "meta.parquet"
    if book_dst.exists() and not force:
        return None

    try:
        import duckdb  # 延迟导入：见模块 docstring
    except ImportError as e:
        raise RuntimeError(
            "duckdb 不可用（cp314 wheel DLL 损坏）。用 3.12 运行："
            "uv run --no-project --python 3.12 --no-cache --with duckdb --with . pm-compact"
        ) from e

    dst_dir.mkdir(parents=True, exist_ok=True)
    tmp_book = dst_dir / f"_tmp_{date}_book.jsonl"
    tmp_meta = dst_dir / f"_tmp_{date}_meta.jsonl"

    n_lines = n_bad = 0
    n_book_emitted = n_meta_emitted = 0   # 扁平化实际写出的行数
    n_events_book = n_events_meta = 0     # 产生行的事件数（校验用）
    with src.open(encoding="utf-8") as f, \
         tmp_book.open("w", encoding="utf-8") as fb, \
         tmp_meta.open("w", encoding="utf-8") as fm:
        for line in f:
            n_lines += 1
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                n_bad += 1
                print(f"!! 坏行 #{n_lines}: {line[:120]!r}")
                continue
            brows, mrows = flatten_event(e)
            for r in brows:
                fb.write(json.dumps(r, ensure_ascii=False) + "\n")
            for r in mrows:
                fm.write(json.dumps(r, ensure_ascii=False) + "\n")
            n_book_emitted += len(brows)
            n_meta_emitted += len(mrows)
            if brows:
                n_events_book += 1
            if mrows:
                n_events_meta += 1

    con = duckdb.connect()
    cols = ("{'ts': 'TIMESTAMPTZ', 'kind': 'VARCHAR', 'asset_id': 'VARCHAR',"
            " 'market': 'VARCHAR', 'side': 'VARCHAR',"
            " 'price': 'DOUBLE', 'size': 'DOUBLE'}")
    mcols = "{'ts': 'TIMESTAMPTZ', 'type': 'VARCHAR', 'payload': 'VARCHAR'}"
    book_dst.parent.mkdir(parents=True, exist_ok=True)
    con.execute(
        f"COPY (SELECT * FROM read_json('{tmp_book.as_posix()}', columns={cols})"
        f" ORDER BY ts, asset_id, side, price)"
        f" TO '{book_dst.as_posix()}' (FORMAT PARQUET, COMPRESSION ZSTD)"
    )
    con.execute(
        f"COPY (SELECT * FROM read_json('{tmp_meta.as_posix()}', columns={mcols})"
        f" ORDER BY ts, type)"
        f" TO '{meta_dst.as_posix()}' (FORMAT PARQUET, COMPRESSION ZSTD)"
    )
    n_book = con.execute(f"SELECT count(*) FROM '{book_dst.as_posix()}'").fetchone()[0]
    n_meta = con.execute(f"SELECT count(*) FROM '{meta_dst.as_posix()}'").fetchone()[0]
    con.close()
    tmp_book.unlink(missing_ok=True)
    tmp_meta.unlink(missing_ok=True)

    # 双重校验：① 两张 parquet 行数与写出行数一致（write→store 无损）；
    # ② 事件数守恒：book 事件 + meta 事件 + 坏行 = 源文件总行数
    verified = (
        n_book == n_book_emitted
        and n_meta == n_meta_emitted
        and n_events_book + n_events_meta + n_bad == n_lines
    )
    deleted = False
    if verified and not keep and n_bad == 0:
        src.unlink()
        deleted = True
    return CompactResult(
        src=src, book_path=book_dst, meta_path=meta_dst,
        n_lines=n_lines, n_bad=n_bad, n_book_rows=n_book, n_meta_rows=n_meta,
        verified=verified, deleted=deleted,
    )


def compact_dir(
    src_dir: str | Path, dst_dir: str | Path, *, keep: bool = False,
    force: bool = False, include_today: bool = False,
) -> list[CompactResult]:
    """扫描目录压实所有已封口的 ``*_market.jsonl``。

    今天（UTC）的文件正在被录制器追加，默认跳过（--include-today 只在
    手工停录后使用）。
    """
    src_dir, dst_dir = Path(src_dir), Path(dst_dir)
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    out: list[CompactResult] = []
    for src in sorted(src_dir.glob("*_market.jsonl")):
        date = src.name.split("_", 1)[0]
        if date == today and not include_today:
            continue
        r = compact_file(src, dst_dir, keep=keep, force=force)
        if r is not None:
            out.append(r)
    return out
