' pm-record 开机自启（可见控制台窗口：显性提示录制在跑，关窗即停）
' Run 参数：1 = 正常显示窗口（任务栏可见、日志实时滚动）
CreateObject("Wscript.Shell").Run """d:\kimi\polymarket\scripts\record_autostart.bat""", 1, False
