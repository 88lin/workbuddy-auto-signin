# uninstall-qoder-windows.ps1 — Qoder CN 每日 100 Credits 一键卸载（Windows）
#
# install-qoder-windows.ps1 只注册 QoderDailyCredits 一个任务，本脚本按同样的
# 粒度把它摘掉，不碰 WorkBuddyAutoSignin / WorkBuddyGrowthPoll。
#
# 沿用 uninstall-qoder-macos.sh 的原则：注销失败时绝不能报成功。Windows 上没有
# 「配置文件」这一步——Register-ScheduledTask 的全部状态就在任务定义里，
# Unregister 成功即彻底移除，所以这里只需要回读确认，不存在「plist 删了但
# 服务还在跑」那种半卸载状态。
#
# 用法：在本仓库目录下，用 PowerShell 运行（与两个 install 脚本同一条命令格式）
#     powershell -ExecutionPolicy Bypass -File .\uninstall-qoder-windows.ps1
#
# 从浏览器下 ZIP 的话，解出来的 .ps1 会带「网络来源」标记，RemoteSigned 策略下
# 不让直接跑，所以上面要带 -ExecutionPolicy Bypass；用 git clone 拿的没有这层标记。
#
# 只操作任务计划程序：不动脚本文件、不动登录态、不写注册表、不改环境变量，
# 也不删日志。日志位置会在结尾打印，要删自己删。
#
# 系统要求：Get-/Unregister-ScheduledTask 来自操作系统自带的 ScheduledTasks 模块，
# 它是 Windows 8 才有的组件（升级 PowerShell 补不上），Windows 7 没有——但那里的
# install-qoder-windows.ps1 同样跑不起来，本来也就没有任务可卸。实测环境是
# Windows 10 22H2 + Windows PowerShell 5.1；Windows 11 只在 CI 里以假模块跑过。

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$taskPath = "\"
$taskNames = @("QoderDailyCredits")
$logNames = @("qoder_signin.log")
$installer = "install-qoder-windows.ps1"

Write-Host ""
Write-Host "Qoder CN 每日 Credits · 一键卸载" -ForegroundColor Cyan
Write-Host ("-" * 46) -ForegroundColor DarkGray

# 用的人未必翻 README，所以把运行环境直接打出来。
$osv = [Environment]::OSVersion.Version
Write-Host ("  系统 Windows {0}.{1} (build {2}) · PowerShell {3}" -f `
            $osv.Major, $osv.Minor, $osv.Build, $PSVersionTable.PSVersion) -ForegroundColor DarkGray

# 这两个 cmdlet 来自操作系统自带的 ScheduledTasks 模块，Windows 8 起才有。缺它的
# 时候命令并不会「返回空」——实测 SilentlyContinue 也压不住 CommandNotFound，脚本
# 会死在第一条 Get-ScheduledTask 上，只留一屏看不懂的报错。所以先探一次再说清楚。
if (-not (Get-Command -Name "Get-ScheduledTask" -ErrorAction SilentlyContinue)) {
    Write-Host ""
    Write-Host "本机没有 Get-ScheduledTask / Unregister-ScheduledTask，卸载未执行。" -ForegroundColor Red
    Write-Host "它们来自操作系统自带的 ScheduledTasks 模块，Windows 8 起才提供。那种机器上" -ForegroundColor Yellow
    Write-Host "install-qoder-windows.ps1 本来也注册不出任务，也就没有东西需要卸。" -ForegroundColor Yellow
    Write-Host "若上面显示的系统确实在 Windows 8 及以上，那是任务计划程序服务被禁用或组件被裁剪，" -ForegroundColor Yellow
    Write-Host "需要先恢复它，再重跑本脚本。" -ForegroundColor Yellow
    exit 1
}

$failed = @()
$removed = @()
$absent = @()

try {
    $tasks = @(Get-ScheduledTask -ErrorAction Stop)
} catch {
    Write-Host "无法读取计划任务，卸载未执行。" -ForegroundColor Red
    Write-Host ("  {0}" -f $_.Exception.Message) -ForegroundColor Red
    exit 1
}

foreach ($name in $taskNames) {
    Write-Host ("  {0,-22}" -f $name) -NoNewline
    $manual = 'Unregister-ScheduledTask -TaskName "{0}" -TaskPath "{1}" -Confirm:$false' -f $name, $taskPath

    $found = @($tasks | Where-Object {
        $_.TaskName -eq $name -and $_.TaskPath -eq $taskPath
    })
    if ($found.Count -eq 0) {
        Write-Host " 未安装，跳过" -ForegroundColor DarkGray
        $absent += $name
        continue
    }

    try {
        # 必须显式 -ErrorAction Stop：ScheduledTasks 模块自带 Continue 偏好，
        # 脚本级的 $ErrorActionPreference 压不住它，失败会退化成非终止错误。
        Unregister-ScheduledTask -TaskName $name -TaskPath $taskPath -Confirm:$false -ErrorAction Stop
        $tasks = @(Get-ScheduledTask -ErrorAction Stop)
    } catch {
        Write-Host " 删除或确认失败" -ForegroundColor Red
        Write-Host ("      {0}" -f $_.Exception.Message) -ForegroundColor Red
        Write-Host "      可手动执行：$manual" -ForegroundColor Yellow
        $failed += $name
        continue
    }

    # 回读确认；命令谎报成功、任务其实还在，是最坏的结果。
    if (@($tasks | Where-Object {
        $_.TaskName -eq $name -and $_.TaskPath -eq $taskPath
    }).Count -gt 0) {
        Write-Host " 命令返回成功但任务仍在" -ForegroundColor Red
        Write-Host "      可手动执行：$manual" -ForegroundColor Yellow
        $failed += $name
        continue
    }

    Write-Host " 已删除" -ForegroundColor Green
    $removed += $name
}

# 同名任务可能注册在别的任务路径下，这里只提醒，不越权去删。
# MSFT_ScheduledTask 实例上只有 TaskName、没有 Name，而 StrictMode 下取不存在的
# 属性会直接抛 PropertyNotFound，所以这里必须用 TaskName。
$elsewhere = @($tasks |
               Where-Object { $taskNames -contains $_.TaskName -and $_.TaskPath -ne $taskPath })
if ($elsewhere.Count -gt 0) {
    Write-Host ""
    Write-Host "注意：以下位置还有同名任务，本脚本没有动它们：" -ForegroundColor Yellow
    foreach ($task in $elsewhere) {
        Write-Host ("  {0}{1}" -f $task.TaskPath, $task.TaskName) -ForegroundColor Yellow
    }
}

Write-Host ""
Write-Host "[日志]" -ForegroundColor Cyan
$thisDir = if ($PSScriptRoot) { $PSScriptRoot } else { (Get-Location).Path }
foreach ($log in $logNames) {
    $path = Join-Path $thisDir $log
    if (Test-Path -LiteralPath $path) {
        Write-Host "  保留 $path" -ForegroundColor DarkGray
        Write-Host ('  （如需一并删除：Remove-Item -LiteralPath "{0}" -Force）' -f $path) -ForegroundColor DarkGray
    } else {
        Write-Host "  （还没有日志文件）" -ForegroundColor DarkGray
    }
}

Write-Host ""
if ($failed.Count -gt 0) {
    Write-Host ("未完成：{0} 个任务删除或确认失败。" -f $failed.Count) -ForegroundColor Red
    Write-Host "若是「拒绝访问 / 0x80070005」，请用安装时的同一 Windows 账号，" -ForegroundColor Yellow
    Write-Host "以管理员身份打开 PowerShell 后在本目录重跑本脚本。" -ForegroundColor Yellow
    if ($removed.Count -gt 0) {
        Write-Host ("已删除的（不会恢复，无需重试这部分）：{0}" -f ($removed -join "、")) -ForegroundColor DarkGray
    }
    exit 1
}

Write-Host "完成：定时任务已全部清除。" -ForegroundColor Green
Write-Host ("  删除 {0} 个 · 本就未装 {1} 个" -f $removed.Count, $absent.Count) -ForegroundColor DarkGray
Write-Host ""
Write-Host "任务计划程序里没有残留；注册表、环境变量和脚本文件本来就没动过。" -ForegroundColor DarkGray
Write-Host ("以后想恢复：powershell -ExecutionPolicy Bypass -File .\{0}" -f $installer) -ForegroundColor DarkGray
