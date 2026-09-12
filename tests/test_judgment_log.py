"""人工判断记录（5.4）测试：词表校验 / append-only / 读回对称。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pm_arb.data.judgment_log import (
    SIGNAL_TAGS,
    Judgment,
    load_judgments,
    record_judgment,
)


def test_signal_tags_registry_nonempty_and_documented():
    assert len(SIGNAL_TAGS) >= 5
    for tag, desc in SIGNAL_TAGS.items():
        assert tag and desc  # 每个标签都有假设说明（转特征的依据）


def test_action_must_be_enter_or_skip():
    with pytest.raises(ValueError):
        Judgment(action="maybe", symbol="btc", window_start=1)


def test_untagged_signal_rejected():
    with pytest.raises(ValueError, match="未登记"):
        Judgment(action="enter", symbol="btc", window_start=1,
                 signals=["完全自造的标签"])


def test_record_and_load_roundtrip(tmp_path: Path):
    path = tmp_path / "judgments.jsonl"
    j1 = Judgment(action="enter", symbol="btc", window_start=100,
                  signals=["vol_low", "trend_favor"], notes="趋势稳")
    j2 = Judgment(action="skip", symbol="eth", window_start=200,
                  signals=["depth_thin"])
    record_judgment(path, j1)
    record_judgment(path, j2)
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    # 事前记录的 ts 由 record 填充（判断时刻）
    assert json.loads(lines[0])["ts"]
    loaded = load_judgments(path)
    assert loaded == [j1, j2]


def test_load_missing_file_returns_empty(tmp_path: Path):
    assert load_judgments(tmp_path / "nope.jsonl") == []


def test_record_creates_parent_dirs(tmp_path: Path):
    path = tmp_path / "a" / "b" / "judgments.jsonl"
    record_judgment(path, Judgment(action="skip", symbol="btc",
                                   window_start=1, signals=["intuition"]))
    assert path.exists()
