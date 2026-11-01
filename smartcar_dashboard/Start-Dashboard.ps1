param([switch]$NoBrowser)
$ErrorActionPreference = 'Stop'
$dashboardDirectory = $PSScriptRoot
$dashboardUrl = 'http://127.0.0.1:8765'
$alreadyRunning = $false
try {
    $state = Invoke-RestMethod "$dashboardUrl/api/state" -TimeoutSec 2
    $alreadyRunning = $null -ne $state.source
} catch {}
if (-not $alreadyRunning) {
    $dashboardPython = (Get-Command python -ErrorAction SilentlyContinue).Source
    if (-not $dashboardPython) { $dashboardPython = (Get-Command py -ErrorAction SilentlyContinue).Source }
    if (-not $dashboardPython) { throw 'Python 3.10+ was not found on PATH. See README.md.' }
    $serverPath = Join-Path $dashboardDirectory 'server.py'
    Start-Process -FilePath $dashboardPython -ArgumentList @('-B', ('"' + $serverPath + '"')) -WorkingDirectory $dashboardDirectory -WindowStyle Hidden -RedirectStandardOutput (Join-Path $dashboardDirectory 'server-output.log') -RedirectStandardError (Join-Path $dashboardDirectory 'server-error.log')
    for ($attempt = 0; $attempt -lt 20; $attempt++) {
        Start-Sleep -Milliseconds 500
        try {
            $state = Invoke-RestMethod "$dashboardUrl/api/state" -TimeoutSec 1
            if ($null -ne $state.source) { $alreadyRunning = $true; break }
        } catch {}
    }
}
if (-not $alreadyRunning) { throw 'Dashboard did not start. Check server-error.log in this folder.' }
if (-not $NoBrowser) { Start-Process $dashboardUrl }
Write-Output "Dashboard ready: $dashboardUrl"
