"""进程内存自监控（24/7 挂机进程的 RSS 可观测性）。

Python 进程 RSS = 解释器 + 依赖库基线（本项目约 35-40MB）+ 运行期动态
分配。长期运行进程的内存泄漏只能靠趋势发现：调用方按固定周期记录
:func:`rss_mb`，配合阈值告警——泄漏在早期是每小时几 MB 的缓慢爬升，
等系统级 OOM 才发现就晚了。
"""

from __future__ import annotations

import ctypes
import sys

if sys.platform == "win32":
    # 64 位下 GetCurrentProcess 伪句柄是 -1，默认 c_int 签名会截断为
    # ERROR_INVALID_HANDLE(6)，必须显式声明 HANDLE/BOOL 签名。
    _K32 = ctypes.windll.kernel32
    _PSAPI = ctypes.windll.psapi
    _K32.GetCurrentProcess.restype = ctypes.c_void_p
    _PSAPI.GetProcessMemoryInfo.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_ulong,
    ]
    _PSAPI.GetProcessMemoryInfo.restype = ctypes.c_int


def rss_mb() -> tuple[float, float]:
    """返回 (当前工作集 MB, 峰值工作集 MB)；获取失败返回 (-1.0, -1.0)。

    Windows：psapi.GetProcessMemoryInfo；POSIX：/proc/self/status VmHWM/VmRSS。
    """
    if sys.platform == "win32":

        class _PMC(ctypes.Structure):
            _fields_ = [
                ("cb", ctypes.c_ulong),
                ("PageFaultCount", ctypes.c_ulong),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        try:
            pmc = _PMC()
            pmc.cb = ctypes.sizeof(_PMC)
            handle = _K32.GetCurrentProcess()
            if not _PSAPI.GetProcessMemoryInfo(handle, ctypes.byref(pmc), pmc.cb):
                return -1.0, -1.0
            mb = 1024.0 * 1024.0
            return pmc.WorkingSetSize / mb, pmc.PeakWorkingSetSize / mb
        except Exception:
            return -1.0, -1.0

    # POSIX 回退：读 /proc/self/status
    try:
        cur = peak = -1.0
        with open("/proc/self/status", encoding="ascii") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    cur = float(line.split()[1]) / 1024.0
                elif line.startswith("VmHWM:"):
                    peak = float(line.split()[1]) / 1024.0
        return cur, peak
    except Exception:
        return -1.0, -1.0
