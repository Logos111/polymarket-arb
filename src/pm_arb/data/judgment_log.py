"""人工判断信号记录（DEV_PLAN 5.4）：事前固定格式，事后不可篡改。

背景：人工判断胜率高于机械规则，说明 decisions.py 未编码全部可用信息。
做法是每次人工进/不进判断**之前**用统一 JSONL 格式落一条（结构化标签，
禁止事后回忆补记）；积累到统计口径一致的样本量（~384 笔）再分析，
有效标签逐条转为可计算特征（features.py）后写进 decisions.py。

纪律：
- **事前记录**：判断时写，不看结果后补写（防记忆偏差）；
- **固定词表**：signal 只能取 :data:`SIGNAL_TAGS` 中的标签，防止标签
  漂移导致统计口径碎掉；自由文本放 ``notes``；
- **append-only**：JSONL 只追加不修改（对齐 ticks 录制的崩溃安全设计）。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

# 固定信号词表（可扩展，但扩展须在此登记，旧样本不受影响）。
# 每个标签对应一个假设，未来逐条转成 features.py 的可计算特征。
SIGNAL_TAGS: dict[str, str] = {
    "vol_low": "开窗波动明显低于 max_vol（机械规则可能过严）",
    "vol_high": "波动超限但趋势方向有利（机械规则拒绝，人工看好）",
    "trend_favor": "TWAP 斜率与所选方向一致（动量延续假设，对应 F3/F4）",
    "feed_slow": "Chainlink 喂价更新变慢（对应 F1 更新频率特征）",
    "spread_wide": "冷门方 bid-ask 宽（流动性噪声，对应 F5）",
    "depth_thin": "ask 档位深度不足（成交冲击顾虑）",
    "cross_coin": "BTC/ETH 同向联动（宏观驱动先验，对应 F6）",
    "streak": "连续窗口同方向/同结果（自相关，对应 F8）",
    "session": "时段效应（欧美/亚洲流动性差异，对应 F7）",
    "intuition": "无具体信号的盘感（单独标签便于统计其可信度）",
    "news": "外部消息/事件驱动",
}


@dataclass
class Judgment:
    """一条人工进/不进判断（事前记录）。"""

    action: str  # "enter"（人工想进）| "skip"（人工主动放弃）
    symbol: str
    window_start: int
    signals: list[str] = field(default_factory=list)
    notes: str = ""
    ts: str = ""  # 留空由 record 填 UTC ISO（记录时刻即判断时刻）

    def __post_init__(self) -> None:
        if self.action not in ("enter", "skip"):
            raise ValueError(f"action 须为 enter/skip，得到 {self.action!r}")
        bad = [s for s in self.signals if s not in SIGNAL_TAGS]
        if bad:
            raise ValueError(
                f"未登记信号标签 {bad}（须在 SIGNAL_TAGS 中，防统计口径漂移）")


def record_judgment(path: str | Path, j: Judgment) -> None:
    """追加一条判断到 JSONL（父目录不存在则创建；append-only）。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    if not j.ts:
        j.ts = datetime.now(UTC).isoformat(timespec="seconds")
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(asdict(j), ensure_ascii=False) + "\n")


def load_judgments(path: str | Path) -> list[Judgment]:
    """读回全部判断（文件不存在返回空表）。"""
    p = Path(path)
    if not p.exists():
        return []
    out: list[Judgment] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        d = json.loads(line)
        out.append(Judgment(
            action=d["action"], symbol=d["symbol"],
            window_start=int(d["window_start"]),
            signals=list(d.get("signals", [])),
            notes=str(d.get("notes", "")), ts=str(d.get("ts", "")),
        ))
    return out
