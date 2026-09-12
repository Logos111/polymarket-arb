@echo off
rem pm-record autostart entry (called by Startup-folder VBS, visible console).
rem Prereq: Clash proxy autostart (PM_PROXY_URL). The recorder retries on its
rem own, so starting before the proxy is fine - it recovers once network is up.
rem Visible indicator: window title = status; closing this window stops recording.
title pm-record REC [BTC/ETH] - CLOSE THIS WINDOW TO STOP
cd /d d:\kimi\polymarket
set "PM_PROXY_URL=http://127.0.0.1:7890"
.venv\Scripts\pm-record.exe --symbols btc,eth --raw-sample 100
