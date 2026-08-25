---
description: 打开 cc-voice 语音输入的管理面板，并汇报当前状态
allowed-tools: Bash, PowerShell
---

打开 cc-voice 管理面板并汇报状态：

1. 读取 `http://127.0.0.1:8731/api/state`（PowerShell: `Invoke-RestMethod`）。
   连不上说明守护进程没在跑，用下面的命令拉起它，再重试一次：
   ```powershell
   $r = Join-Path $env:USERPROFILE '.claude-voice'
   Start-Process (Join-Path $r '.venv\Scripts\pythonw.exe') `
                 -ArgumentList (Join-Path $r 'daemon\ccvoice.py') `
                 -WorkingDirectory (Join-Path $r 'daemon') -WindowStyle Hidden
   ```

2. 用一段话汇报：模型是哪个、状态是否「就绪」、登记了几个 Claude Code 会话、
   触发键设置、累计识别次数。若 `error` 非空，把它原样贴出来。

3. 在默认浏览器里打开 `http://127.0.0.1:8731/`。

$ARGUMENTS 为 `status` 时只汇报、不开浏览器。
