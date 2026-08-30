<#
.SYNOPSIS
  WorkBuddy 自动签到 - Windows 任务计划程序一键安装/卸载

.DESCRIPTION
  创建一个每天定时运行 signin.py silent 的 Windows 计划任务，
  使用 pythonw.exe 无窗口静默执行，完全不经过 AI 模型，零 token 消耗。
  错过触发时间（如关机）时，下次开机自动补跑。

.PARAMETER Time
  每天运行时间，默认 00:05

.PARAMETER Remove
  卸载已创建的计划任务

.EXAMPLE
  .\setup-task.ps1                    # 安装，每天 00:05 运行
  .\setup-task.ps1 -Time "08:30"      # 安装，每天 08:30 运行
  .\setup-task.ps1 -Remove            # 卸载
#>
param(
  [string]$Time = "00:05",
  [switch]$Remove
)

$TaskName = "WorkBuddyAutoSignin"

if ($Remove) {
  try {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction Stop
    Write-Host "已卸载计划任务: $TaskName" -ForegroundColor Green
  } catch {
    Write-Host "任务不存在或已卸载" -ForegroundColor Yellow
  }
  return
}

# --- 定位 pythonw.exe ---
$PythonCandidates = @(
  # WorkBuddy 托管 Python
  "$env:USERPROFILE\.workbuddy\binaries\python\versions\3.13.12\pythonw.exe",
  "$env:USERPROFILE\.workbuddy\binaries\python\versions\3.13.7\pythonw.exe",
  # 系统 Python
  "C:\Python313\pythonw.exe",
  "C:\Python312\pythonw.exe",
  "C:\Python311\pythonw.exe"
)

$Pythonw = $null
foreach ($p in $PythonCandidates) {
  if (Test-Path $p) { $Pythonw = $p; break }
}

# 兜底：从 PATH 找
if (-not $Pythonw) {
  $found = Get-Command pythonw.exe -ErrorAction SilentlyContinue
  if ($found) { $Pythonw = $found.Source }
}

if (-not $Pythonw) {
  Write-Host "错误: 未找到 pythonw.exe，请先安装 Python 3" -ForegroundColor Red
  exit 1
}

# --- 定位 signin.py ---
$ScriptPath = Join-Path $PSScriptRoot "signin.py"
if (-not (Test-Path $ScriptPath)) {
  Write-Host "错误: 未找到 signin.py，请确保 setup-task.ps1 与 signin.py 在同一目录" -ForegroundColor Red
  exit 1
}

# --- 日志路径 ---
$LogPath = Join-Path $PSScriptRoot "signin.log"

Write-Host "配置信息:" -ForegroundColor Cyan
Write-Host "  Python:  $Pythonw"
Write-Host "  脚本:    $ScriptPath"
Write-Host "  日志:    $LogPath"
Write-Host "  时间:    每天 $Time"
Write-Host "  模式:    silent (无窗口, 输出写日志)"
Write-Host ""

# --- 创建计划任务 ---
$Action = New-ScheduledTaskAction `
  -Execute $Pythonw `
  -Argument "`"$ScriptPath`" silent" `
  -WorkingDirectory $PSScriptRoot

$Trigger = New-ScheduledTaskTrigger -Daily -At $Time

$Settings = New-ScheduledTaskSettingsSet `
  -StartWhenAvailable `
  -AllowStartIfOnBatteries `
  -DontStopIfGoingOnBatteries `
  -ExecutionTimeLimit (New-TimeSpan -Minutes 10)

$Principal = New-ScheduledTaskPrincipal `
  -UserId $env:USERNAME `
  -LogonType Interactive `
  -RunLevel Limited

try {
  # 先尝试删除已有的
  Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
  Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -Principal $Principal `
    -Force -ErrorAction Stop | Out-Null
  Write-Host "计划任务创建成功: $TaskName" -ForegroundColor Green
  Write-Host "  - 每天 $Time 静默运行签到 + 成长中心"
  Write-Host "  - 错过时间（关机等）下次开机自动补跑"
  Write-Host "  - 日志文件: $LogPath"
  Write-Host ""
  Write-Host "卸载命令: .\setup-task.ps1 -Remove" -ForegroundColor DarkGray
} catch {
  Write-Host "创建失败: $_" -ForegroundColor Red
  exit 1
}
