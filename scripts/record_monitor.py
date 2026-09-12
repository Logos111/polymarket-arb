"""pm-record 状态监视器：置顶小窗口显示录制状态，可启动/停止。

开机自启链：启动文件夹 VBS → pythonw 本文件（无控制台，只有这个窗口）。
监视器启动时若录制进程未运行则自动拉起（环境变量含 PM_PROXY_URL）。

状态判定（每 2s 刷新）：
- 进程存在（tasklist 查 pm-record.exe）
- 数据新鲜度：runtime/ticks 最新 jsonl 的 mtime 距今 <45s 视为数据流动
"""

from __future__ import annotations

import os
import subprocess
import time
import tkinter as tk
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TICKS = ROOT / "runtime" / "ticks"
EXE = ROOT / ".venv" / "Scripts" / "pm-record.exe"
LOG = ROOT / "runtime" / "record_console.log"
PROXY = "http://127.0.0.1:7890"
CREATE_NO_WINDOW = 0x08000000

REFRESH_MS = 2000
FRESH_SEC = 45.0  # RTDS 约 1Hz 写入，45s 无写入才算停滞


def recorder_running() -> bool:
    r = subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq pm-record.exe", "/FO", "CSV", "/NH"],
        capture_output=True, text=True, creationflags=CREATE_NO_WINDOW,
    )
    return "pm-record.exe" in r.stdout


def data_age_sec() -> float | None:
    files = list(TICKS.glob("*.jsonl"))
    if not files:
        return None
    return time.time() - max(f.stat().st_mtime for f in files)


def start_recorder() -> None:
    env = {**os.environ, "PM_PROXY_URL": PROXY}
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("ab") as f:
        subprocess.Popen(
            [str(EXE), "--symbols", "btc,eth", "--raw-sample", "100"],
            cwd=str(ROOT), env=env, stdout=f, stderr=subprocess.STDOUT,
            creationflags=CREATE_NO_WINDOW,
        )


def stop_recorder() -> None:
    subprocess.run(
        ["taskkill", "/F", "/T", "/IM", "pm-record.exe"],
        capture_output=True, creationflags=CREATE_NO_WINDOW,
    )


class Monitor:
    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.title("pm-record 监视器")
        self.root.geometry("300x86")
        self.root.attributes("-topmost", True)
        self.status = tk.Label(self.root, font=("Microsoft YaHei", 11), fg="#666")
        self.status.pack(pady=(10, 4))
        row = tk.Frame(self.root)
        row.pack()
        self.btn_start = tk.Button(row, text="启动", width=8, command=self._start)
        self.btn_stop = tk.Button(row, text="停止", width=8, command=self._stop)
        self.btn_start.pack(side="left", padx=6)
        self.btn_stop.pack(side="left", padx=6)
        self._bootstrapped = False
        self.root.after(500, self._tick)

    def _start(self) -> None:
        start_recorder()
        self._refresh()

    def _stop(self) -> None:
        stop_recorder()
        self._refresh()

    def _tick(self) -> None:
        # 开机路径：首次刷新时若未运行则自动拉起
        if not self._bootstrapped:
            self._bootstrapped = True
            if not recorder_running():
                start_recorder()
        self._refresh()
        self.root.after(REFRESH_MS, self._tick)

    def _refresh(self) -> None:
        running = recorder_running()
        self.btn_start.config(state="disabled" if running else "normal")
        self.btn_stop.config(state="normal" if running else "disabled")
        if not running:
            self.status.config(text="○ 未运行", fg="#999")
            return
        age = data_age_sec()
        if age is None or age > FRESH_SEC:
            self.status.config(
                text=f"● 进程在，数据停滞（{age if age is not None else '无文件'}）",
                fg="#d80",
            )
        else:
            self.status.config(
                text=f"● 录制中，数据正常（{age:.0f}s 前更新）", fg="#181"
            )

    def run(self) -> None:
        self.root.mainloop()


if __name__ == "__main__":
    Monitor().run()
