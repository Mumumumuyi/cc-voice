# cc-voice SessionStart 钩子
#
# 做两件事：
#   1. 把「本终端的进程链」登记到 sessions/，供守护进程判断前台窗口是不是
#      Claude Code 终端。不靠窗口标题猜 —— 标题会被改、会被 ssh/tmux 顶掉。
#   2. 确保守护进程在跑。三个入口（cc/cc2/cc3）都会触发本钩子，守护进程内部
#      用命名互斥量保证只起一个。
#
# 钩子必须快且绝不阻塞会话启动：任何异常都吞掉并返回空 JSON。

$ErrorActionPreference = 'Stop'
# 项目根目录。不能只靠 $PSScriptRoot 往上推：插件被安装后，这个脚本是从
# ~/.claude/plugins/cache/... 里执行的，往上四层落不到项目根。所以先按脚本
# 位置试（从源码目录直接跑时成立），不成立再回落到文档里的安装位置。
$Root = Split-Path -Parent (Split-Path -Parent (Split-Path -Parent $PSScriptRoot))
$Root = Split-Path -Parent $Root
if (-not (Test-Path -LiteralPath (Join-Path $Root 'daemon\ccvoice.py'))) {
    $Root = Join-Path $env:USERPROFILE '.claude-voice'
}

try {
    $stdin = [Console]::In.ReadToEnd()
    $cwd = ''
    if ($stdin) {
        try { $cwd = ($stdin | ConvertFrom-Json).cwd } catch { }
    }

    # 一次性拉全表再在内存里走链。逐级 Get-CimInstance 每次约 100ms，
    # 十几层就要 1 秒以上，会明显拖慢会话启动。
    $map = @{}
    Get-CimInstance Win32_Process -Property ProcessId, ParentProcessId, Name |
        ForEach-Object { $map[[int]$_.ProcessId] = $_ }

    $chain = @()
    $claudePid = 0
    $cur = $PID
    for ($i = 0; $i -lt 14 -and $cur -gt 4; $i++) {
        $chain += $cur
        $proc = $map[$cur]
        if (-not $proc) { break }
        if ($claudePid -eq 0 -and $proc.Name -match '^(claude|node)\.exe$') { $claudePid = $cur }
        $cur = [int]$proc.ParentProcessId
    }
    if ($claudePid -eq 0) { $claudePid = $PID }

    # CLAUDE_CONFIG_DIR 是三个入口唯一可靠的区分标志：cc 不设，cc2 指向
    # .claude-account-b，cc3 指向 .claude-alt
    $entry = switch -Wildcard ($env:CLAUDE_CONFIG_DIR) {
        '*.claude-account-b' { 'cc2'; break }
        '*.claude-alt'       { 'cc3'; break }
        default              { 'cc' }
    }

    $sessions = Join-Path $Root 'sessions'
    New-Item -ItemType Directory -Force -Path $sessions | Out-Null
    @{
        claude_pid = $claudePid
        pids       = $chain
        entry      = $entry
        cwd        = $cwd
        started    = (Get-Date).ToString('s')
    } | ConvertTo-Json -Compress | ForEach-Object {
        # 不能用 Set-Content -Encoding utf8：Windows PowerShell 5.1 下它一定
        # 写 BOM，Python 侧 json.loads 会直接抛异常。显式指定不带 BOM 的编码。
        [System.IO.File]::WriteAllText(
            (Join-Path $sessions "$claudePid.json"), $_,
            (New-Object System.Text.UTF8Encoding($false)))
    }

    # 守护进程是否已在跑：直接探它的命名互斥量 —— 和守护进程自己做单实例
    # 判断用的是同一个权威源，不会有歧义。扫命令行会误判：任何提到过这个
    # 路径的编辑器/终端/脚本都会被当成守护进程，导致永远不拉起（实测踩过）。
    $running = $false
    try {
        $m = [System.Threading.Mutex]::OpenExisting('Local\cc-voice-daemon')
        $m.Dispose(); $running = $true
    } catch { }
    if (-not $running) {
        $py = Join-Path $Root '.venv\Scripts\pythonw.exe'
        $script = Join-Path $Root 'daemon\ccvoice.py'
        if ((Test-Path -LiteralPath $py) -and (Test-Path -LiteralPath $script)) {
            Start-Process -FilePath $py -ArgumentList $script -WindowStyle Hidden `
                          -WorkingDirectory (Join-Path $Root 'daemon')
        }
    }
} catch {
    # 语音输入起不来不该让 Claude Code 起不来。
    # 日志用 -Value 显式传参：管道绑定在 Windows PowerShell 5.1 下会因为
    # 输入流已被 ReadToEnd 消费而报 "missing mandatory parameters: Value"，
    # 结果是 catch 自己抛异常、把真正的错误吞掉。
    try {
        $line = "{0}  session-start: {1}`n    {2}" -f (Get-Date -Format s),
                $_.Exception.Message, $_.ScriptStackTrace
        Add-Content -LiteralPath (Join-Path $Root 'logs\hook.log') -Value $line -Encoding utf8
    } catch { }
}

Write-Output '{}'
exit 0
