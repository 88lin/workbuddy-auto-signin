# install-qoder-windows.ps1 — Qoder CN 每日 100 Credits 一键安装（Windows）
#
# 作者：88lin
# 仓库：https://github.com/88lin/workbuddy-auto-signin
# 协议：MIT
#
# Qoder CN 个人用户每天可在桌面端领取 100 通用 Credits。本脚本只注册一个
# 独立的计划任务，不碰 WorkBuddy 的两个任务；登录态由 qoder_signin.py 自动读取。
#
# 用法：在本仓库目录下，用 PowerShell 运行
#     powershell -ExecutionPolicy Bypass -File .\install-qoder-windows.ps1
#
# 会创建定时任务：
#   QoderDailyCredits   每天 10:05（UTC+8）起，另设 4 个补跑点，共 5 次
#
# 零 Token、无窗口、错过开机自动补跑；已领过时只查询、不重复领取。

$ErrorActionPreference = "Stop"

# ====== 一般不用改；自动探测失败时手动填这两个 ======
$ManualPythonw = ""   # 例如 C:\Python313\pythonw.exe
$ManualSignin  = ""   # 例如 C:\Users\You\Desktop\checkin\qoder_signin.py
# ====================================================

function Find-Pythonw {
    # 1) PATH 里就有 pythonw
    $c = (Get-Command pythonw -ErrorAction SilentlyContinue).Source
    if ($c -and (Test-Path -LiteralPath $c)) { return $c }

    # 2) PATH 里有 python.exe，取同目录的 pythonw
    $py = (Get-Command python -ErrorAction SilentlyContinue).Source
    if ($py) {
        $c = Join-Path (Split-Path $py) "pythonw.exe"
        if (Test-Path -LiteralPath $c) { return $c }
    }

    # 3) 常见安装目录（很多 Windows 装机 Python 并不进 PATH，这一步最常命中）
    foreach ($pat in @(
        "C:\Python3*\pythonw.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python3*\pythonw.exe",
        "C:\Program Files\Python3*\pythonw.exe"
    )) {
        $hit = Get-ChildItem $pat -ErrorAction SilentlyContinue |
               Sort-Object FullName -Descending | Select-Object -First 1
        if ($hit) { return $hit.FullName }
    }

    # 4) py 启动器（装在 C:\Windows，几乎总在）
    $launcher = Join-Path $env:SystemRoot "py.exe"
    if (Test-Path -LiteralPath $launcher) {
        try {
            $exe = (& $launcher -3 -c "import sys;print(sys.executable)" 2>$null | Select-Object -Last 1)
            if ($exe) {
                $c = Join-Path (Split-Path $exe.Trim()) "pythonw.exe"
                if (Test-Path -LiteralPath $c) { return $c }
            }
        } catch { }
    }
    return $null
}

Write-Host ""
Write-Host "Qoder CN 每日 Credits · 一键安装" -ForegroundColor Cyan
Write-Host ("-" * 46) -ForegroundColor DarkGray

# --- 1. 定位 pythonw.exe ---
Write-Host "[1/3] 定位 pythonw.exe ..." -NoNewline
$pythonw = $ManualPythonw
# 必须先判空再 Test-Path：Test-Path "" 在 PowerShell 5.1 抛参数校验异常，
# 配合顶部的 $ErrorActionPreference = "Stop" 会让脚本直接死在这一行。
if (-not $pythonw -or -not (Test-Path -LiteralPath $pythonw)) { $pythonw = Find-Pythonw }
if (-not $pythonw -or -not (Test-Path -LiteralPath $pythonw)) {
    Write-Host " 失败" -ForegroundColor Red
    Write-Host ""
    Write-Host "没找到 pythonw.exe。请先安装 Python 3：https://www.python.org/downloads/" -ForegroundColor Yellow
    Write-Host "装好后重跑本脚本；或把脚本顶部的 `$ManualPythonw 填成完整路径。" -ForegroundColor Yellow
    exit 1
}
Write-Host " $pythonw" -ForegroundColor Green

# --- 2. 定位 qoder_signin.py ---
Write-Host "[2/3] 定位 qoder_signin.py ..." -NoNewline
$signin = $ManualSignin
# 同上：$ManualSignin 默认为空串，不判空会崩在 Test-Path 的参数校验上。
# $PSScriptRoot 在脚本被直接粘进控制台执行时也是空的，Join-Path 同样不接受空 Path。
if (-not $signin -or -not (Test-Path -LiteralPath $signin)) {
    if ($PSScriptRoot) { $signin = Join-Path $PSScriptRoot "qoder_signin.py" }
}
if (-not $signin -or -not (Test-Path -LiteralPath $signin)) { $signin = Join-Path (Get-Location) "qoder_signin.py" }
if (-not $signin -or -not (Test-Path -LiteralPath $signin)) {
    Write-Host " 失败" -ForegroundColor Red
    Write-Host ""
    Write-Host "没找到 qoder_signin.py。请把本脚本和 qoder_signin.py 放在同一目录后重跑，" -ForegroundColor Yellow
    Write-Host "或把脚本顶部的 `$ManualSignin 填成完整路径。" -ForegroundColor Yellow
    exit 1
}
$signin = (Resolve-Path -LiteralPath $signin).Path
Write-Host " $signin" -ForegroundColor Green

# --- 3. 创建定时任务 ---
Write-Host "[3/3] 创建定时任务 ..." -NoNewline
$taskName = "QoderDailyCredits"

# 活动按 UTC+8 每天 10:00 刷新。先换算成本机时间，再加 5 分钟；
# 后面四个补跑点用来兜住「10:05 时活动还没下发 / 网络没就绪 / 机器刚开机」。
$resetUtc = [datetime]::SpecifyKind([datetime]::UtcNow.Date.AddHours(2), [DateTimeKind]::Utc)
$primary  = $resetUtc.ToLocalTime().AddMinutes(5)
$offsets  = @(0, 1, 5, 9, 13)
$times    = @()
foreach ($h in $offsets) { $times += $primary.AddHours($h).ToString("HH:mm") }

$operation = "创建"
try {
    # 模块命令可能使用自己的错误偏好；必须在调用处显式指定 Stop。
    # 即使安装时需要提权，定时运行仍使用当前账号的普通权限。
    $principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive `
        -RunLevel Limited -ErrorAction Stop

    $triggers = @()
    foreach ($t in $times) {
        $triggers += New-ScheduledTaskTrigger -Daily -At $t -ErrorAction Stop
    }

    $action = New-ScheduledTaskAction -Execute $pythonw -Argument "`"$signin`" silent" -ErrorAction Stop
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -Hidden `
        -DontStopIfGoingOnBatteries -AllowStartIfOnBatteries `
        -ExecutionTimeLimit (New-TimeSpan -Minutes 5) -ErrorAction Stop

    Register-ScheduledTask -TaskName $taskName -TaskPath "\" `
        -Action $action -Trigger $triggers -Settings $settings -Principal $principal `
        -Description "Qoder CN daily 100 Credits claim (silent, zero token)" `
        -Force -ErrorAction Stop | Out-Null

    # 回读任务和运行信息都成功后才宣布完成；同名旧任务不能掩盖注册失败。
    $operation = "校验"
    $tasks = @(Get-ScheduledTask -TaskName $taskName -TaskPath "\" -ErrorAction Stop)
    if ($tasks.Count -ne 1) { throw "未找到唯一的计划任务：$taskName" }
    $info = Get-ScheduledTaskInfo -TaskName $taskName -TaskPath "\" -ErrorAction Stop
    if ($null -eq $info -or $null -eq $info.NextRunTime) {
        throw "未读取到计划任务的下次运行时间：$taskName"
    }
    $state = $tasks[0].State
    $nextRun = $info.NextRunTime
} catch {
    Write-Host " 失败" -ForegroundColor Red
    Write-Host ""
    Write-Host "$operation $taskName 失败，安装未完成。" -ForegroundColor Red
    Write-Host "错误信息：$($_.Exception.Message)" -ForegroundColor Red
    if ($operation -eq "校验") {
        Write-Host "任务可能已注册，但回读校验失败；排除问题后重跑会更新同名任务。" -ForegroundColor Yellow
    }
    Write-Host "若提示拒绝访问（0x80070005），请使用同一 Windows 账号以管理员身份打开 PowerShell 后重试。" -ForegroundColor Yellow
    exit 1
}
Write-Host " 完成" -ForegroundColor Green

# --- 收尾汇报 ---
Write-Host ""
Write-Host "定时任务已就位：" -ForegroundColor Green
Write-Host ""
Write-Host "  $taskName  $state" -ForegroundColor Green
Write-Host "  每天 $($times -join '、')  领取 Qoder CN 100 Credits（静默）"
Write-Host "  下次运行 $nextRun" -ForegroundColor DarkGray
Write-Host ""
$logFile = Join-Path (Split-Path -LiteralPath $signin) "qoder_signin.log"
Write-Host "日志：$logFile" -ForegroundColor Cyan
Write-Host "任务只在真领到、活动异常或出错时写日志；平常「已领过」不落盘。" -ForegroundColor DarkGray
Write-Host ""
Write-Host "装完建议先手动自检（不领取）：" -ForegroundColor DarkGray
Write-Host "  python `"$signin`" doctor"
Write-Host ""
Write-Host "卸载：" -ForegroundColor DarkGray
Write-Host "  Unregister-ScheduledTask -TaskName `"$taskName`" -Confirm:`$false" -ForegroundColor DarkGray
Write-Host ""
