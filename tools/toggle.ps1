# cc-voice 开关：没在跑就启动，跑着就关闭。
#
# 判定用命名互斥量而不是扫进程命令行 —— 和守护进程自己做单实例判断用的是同一个
# 权威源。扫命令行会误判：任何提到过这个路径的编辑器/终端/脚本都会被当成守护进程。
#
# 全程静默，不弹窗：屏幕上那枚悬浮岛本身就是状态指示 —— 出现即已启动，消失即已关闭。

$ErrorActionPreference = 'SilentlyContinue'
$Root = Split-Path -Parent $PSScriptRoot

$running = $false
try {
    $m = [System.Threading.Mutex]::OpenExisting('Local\cc-voice-daemon')
    $m.Dispose(); $running = $true
} catch { }

if ($running) {
    Get-Process python, pythonw |
        Where-Object { $_.Path -like '*claude-voice*' } |
        Stop-Process -Force
    exit 0
}

$py = Join-Path $Root '.venv\Scripts\pythonw.exe'
$script = Join-Path $Root 'daemon\ccvoice.py'
if ((Test-Path -LiteralPath $py) -and (Test-Path -LiteralPath $script)) {
    Start-Process -FilePath $py -ArgumentList $script `
                  -WorkingDirectory (Join-Path $Root 'daemon') -WindowStyle Hidden
}
