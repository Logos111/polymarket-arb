"""前视泄漏护栏（b09；工程落地方案 §2.6：人为纪律 → CI 硬约束）。

静态检查实盘判定路径模块**不得** import labels（Label 含 future_return /
MFE / MAE 等未来信息，只在回测研究管线使用）。检查方式：AST 扫描
import 语句，含 from-import 与延迟 import（函数体内 import 也算）。
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "pm_arb"

# 实盘判定路径模块（含未来新增判定模块时的扩展点：加路径即可）
GUARDED = [
    SRC / "strategies" / "crypto_5m" / "orchestrator.py",
    SRC / "strategies" / "crypto_5m" / "decisions.py",
    SRC / "strategies" / "crypto_5m" / "context.py",
    SRC / "strategies" / "crypto_5m" / "features.py",
    SRC / "risk" / "gates.py",
]

FORBIDDEN = "pm_arb.strategies.crypto_5m.labels"


def _imports_of(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    mods: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            mods.add(node.module)
    return mods


def test_no_lookahead_leak() -> None:
    for p in GUARDED:
        assert p.is_file(), f"护栏目标缺失: {p}"
        mods = _imports_of(p)
        assert FORBIDDEN not in mods, (
            f"{p.name} 引入了 {FORBIDDEN}（前视泄漏）：Label 只允许"
            "在回测研究管线（backtest/run.py 等）使用"
        )
