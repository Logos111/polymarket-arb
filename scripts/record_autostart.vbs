' pm-record 开机自启：pythonw 启动状态监视器（置顶小窗口，无控制台）
' 监视器自动拉起录制进程；窗口即状态显示，也可手动启动/停止。
CreateObject("Wscript.Shell").Run """d:\kimi\polymarket\.venv\Scripts\pythonw.exe"" ""d:\kimi\polymarket\scripts\record_monitor.py""", 1, False
