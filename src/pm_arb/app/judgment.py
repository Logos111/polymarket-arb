"""pm-judgment：人工判断信号事前记录 CLI（DEV_PLAN 5.4）。

在人工做出进/不进判断的**当时**（看到结果之前）记录依据信号：

    pm-judgment enter --symbol btc --window 1789305300 \
        --signal vol_low --signal trend_favor --note "趋势稳"
    pm-judgment skip  --symbol eth --window 1789305600 --signal depth_thin
    pm-judgment --list 10        # 看最近 10 条
    pm-judgment --stats          # enter/skip 计数 + 标签频次

数据落 runtime/judgments.jsonl（append-only）。样本到 ~384 笔后再做
标签-胜率分析，有效标签转 features.py 可计算特征。
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter

from pm_arb.data.judgment_log import (
    SIGNAL_TAGS,
    Judgment,
    load_judgments,
    record_judgment,
)

JUDGMENT_PATH = "runtime/judgments.jsonl"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="人工进/不进判断事前记录（防事后回忆偏差）")
    parser.add_argument("action", nargs="?", choices=["enter", "skip"],
                        help="enter=人工想进 / skip=人工主动放弃")
    parser.add_argument("--symbol", default="btc", choices=["btc", "eth"])
    parser.add_argument("--window", type=int, required=False,
                        help="窗口起点 unix ts（进行中窗口的起点）")
    parser.add_argument("--signal", action="append", default=None,
                        metavar="TAG", help="信号标签（可多次；词表见 --tags）")
    parser.add_argument("--note", default="", help="自由文本备注")
    parser.add_argument("--list", type=int, default=None, metavar="N",
                        help="显示最近 N 条记录")
    parser.add_argument("--stats", action="store_true", help="计数统计")
    parser.add_argument("--tags", action="store_true", help="显示信号标签词表")
    args = parser.parse_args()

    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    if args.tags:
        for tag, desc in SIGNAL_TAGS.items():
            print(f"  {tag:<12} {desc}")
        return 0

    if args.stats:
        js = load_judgments(JUDGMENT_PATH)
        n_enter = sum(j.action == "enter" for j in js)
        n_skip = len(js) - n_enter
        print(f"总计 {len(js)} 条（enter {n_enter} / skip {n_skip}），"
              f"距离分析样本量 ~384 还差 {max(0, 384 - len(js))} 条")
        for tag, n in Counter(
                s for j in js for s in j.signals).most_common():
            print(f"  {tag:<12} {n}")
        return 0

    if args.list is not None:
        js = load_judgments(JUDGMENT_PATH)[-args.list:]
        for j in js:
            print(f"{j.ts}  {j.symbol:<4} ws={j.window_start}  {j.action:<5}"
                  f"  [{' '.join(j.signals) or '-'}]  {j.notes}")
        return 0

    if args.action is None or args.window is None:
        parser.error("记录一条需要 action（enter/skip）与 --window；"
                     "查看用 --list/--stats/--tags")
    signals = args.signal or []
    if not signals:
        print("⚠️ 未给任何 --signal：无依据的记录无法转化为特征，建议至少一个标签。")
    record_judgment(JUDGMENT_PATH, Judgment(
        action=args.action, symbol=args.symbol,
        window_start=args.window, signals=signals, notes=args.note))
    print(f"已记录 {args.action} {args.symbol} ws={args.window}"
          f"  [{' '.join(signals) or '无标签'}] → {JUDGMENT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
