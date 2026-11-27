$statePath = Join-Path $PSScriptRoot 'hub-state.json'
if (Test-Path $statePath) {
    $state = Get-Content $statePath -Raw -Encoding UTF8 | ConvertFrom-Json
    $hpid = $state.pid
    if ($hpid) {
        Stop-Process -Id $hpid -Force -ErrorAction SilentlyContinue
        Write-Host "Stopped hub pid $hpid."
    } else {
        Write-Host "No pid recorded in hub-state.json."
    }
} else {
    Write-Host "Hub not running (no hub-state.json)."
}
