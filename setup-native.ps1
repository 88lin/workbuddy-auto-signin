<#
.SYNOPSIS
  WorkBuddy Auto Signin - Windows 原生定时任务安装脚本

.DESCRIPTION
  注册 Windows 任务计划程序，每天 00:05 静默运行 signin.py auto。
  零 AI token 消耗、零聊天记录、输出写入 signin.log。
  错过触发时间时，下次开机/登录后自动补跑。

.PARAMETER Uninstall
  卸载定时任务并清理 wrapper 文件。

.EXAMPLE
  .\setup-native.ps1              # 安装
  .\setup-native.ps1 -Uninstall   # 卸载
#>
param(
  [switch]$Uninstall
)

$ErrorActionPreference = "Stop"
$TaskName = "WorkBuddyAutoSignin"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ScriptPath = Join-Path $ScriptDir "signin.py"
$LogPath = Join-Path $ScriptDir "signin.log"
$BatPath = Join-Path $ScriptDir "run-silent.bat"

# --- 卸载 ---
if ($Uninstall) {
  Write-Host "正在卸载定时任务..." -ForegroundColor Yellow
  try {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction Stop
    Write-Host "  已删除任务: $TaskName" -ForegroundColor Green
  } catch {
    Write-Host "  任务不存在，跳过" -ForegroundColor DarkGray
  }
  foreach ($f in @($BatPath, $LogPath)) {
    if (Test-Path $f) { Remove-Item $f -Force; Write-Host "  已删除: $f" -ForegroundColor Green }
  }
  Write-Host "卸载完成。" -ForegroundColor Cyan
  return
}

# --- 安装 ---

# 1. 探测 Python
Write-Host "[1/4] 探测 Python..." -ForegroundColor Cyan
$PythonExe = $null

# 优先: WorkBuddy 托管 Python
$managedBase = Join-Path $env:USERPROFILE ".workbuddy\binaries\python\versions"
if (Test-Path $managedBase) {
  $managedPy = Get-ChildItem $managedBase -Directory |
    Sort-Object Name -Descending |
    Select-Object -First 1 -ExpandProperty FullName
  if ($managedPy) {
    $candidate = Join-Path $managedPy "python.exe"
    if (Test-Path $candidate) { $PythonExe = $candidate }
  }
}

# 其次: 系统 python.exe
if (-not $PythonExe) {
  $sysPy = (Get-Command python.exe -ErrorAction SilentlyContinue).Source
  if ($sysPy) { $PythonExe = $sysPy }
}

if (-not $PythonExe) {
  Write-Host "  未找到 Python，请确保 Python 3 已安装" -ForegroundColor Red
  exit 1
}
Write-Host "  Python: $PythonExe" -ForegroundColor Green

# 2. 检查脚本存在
if (-not (Test-Path $ScriptPath)) {
  Write-Host "  未找到 signin.py，请在本目录运行" -ForegroundColor Red
  exit 1
}
Write-Host "[2/4] 脚本: $ScriptPath" -ForegroundColor Cyan

# 3. 生成静默运行 wrapper
Write-Host "[3/4] 生成 run-silent.bat..." -ForegroundColor Cyan
$batContent = @"
@echo off
chcp 65001 >nul 2>&1
cd /d "$ScriptDir"
echo. >> "$LogPath"
echo === %date% %time% === >> "$LogPath"
"$PythonExe" "$ScriptPath" auto >> "$LogPath" 2>&1
"@
$batContent | Out-File -FilePath $BatPath -Encoding ascii -Force
Write-Host "  已生成: $BatPath" -ForegroundColor Green

# 4. 注册定时任务
Write-Host "[4/4] 注册定时任务..." -ForegroundColor Cyan

# 若已存在则先删除
try { Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction Stop } catch {}

$Action = New-ScheduledTaskAction -Execute $BatPath -WorkingDirectory $ScriptDir
$Trigger = New-ScheduledTaskTrigger -Daily -At "00:05"
$Settings = New-ScheduledTaskSettingsSet `
  -StartWhenAvailable `
  -DontStopOnIdleEnd `
  -ExecutionTimeLimit (New-TimeSpan -Minutes 10) `
  -RestartCount 2 `
  -RestartInterval (New-TimeSpan -Minutes 5)
$Principal = New-ScheduledTaskPrincipal `
  -UserId $env:USERNAME `
  -LogonType Interactive `
  -RunLevel Limited

Register-ScheduledTask `
  -TaskName $TaskName `
  -Action $Action `
  -Trigger $Trigger `
  -Settings $Settings `
  -Principal $Principal `
  -Description "WorkBuddy 每日自动签到 + 成长中心（静默，零 AI token）" `
  -Force | Out-Null

Write-Host "  任务已注册: $TaskName" -ForegroundColor Green
Write-Host ""
Write-Host "安装完成!" -ForegroundColor Cyan
Write-Host "  任务名:    $TaskName" -ForegroundColor White
Write-Host "  触发时间:   每天 00:05" -ForegroundColor White
Write-Host "  错过补跑:   是（下次开机/登录后自动补跑）" -ForegroundColor White
Write-Host "  日志文件:   $LogPath" -ForegroundColor White
Write-Host "  AI token:   零消耗" -ForegroundColor Green
Write-Host ""
Write-Host "  立即测试:   右键任务 -> 运行" -ForegroundColor DarkGray
Write-Host "  查看日志:   type `"$LogPath`"" -ForegroundColor DarkGray
Write-Host "  卸载:       .\setup-native.ps1 -Uninstall" -ForegroundColor DarkGray
