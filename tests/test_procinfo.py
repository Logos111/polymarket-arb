"""procinfo 进程内存自监控测试。"""

import sys

from pm_arb.infra.procinfo import rss_mb


def test_rss_mb_returns_positive_on_current_platform():
    cur, peak = rss_mb()
    if sys.platform in ("win32", "linux"):
        # 当前进程至少加载了 pytest + 解释器，工作集必然为正
        assert cur > 0
        assert peak >= cur
    else:
        assert cur == -1.0 and peak == -1.0
