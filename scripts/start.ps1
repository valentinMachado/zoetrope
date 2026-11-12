# Start the whole server detached from this terminal: the API (http://127.0.0.1:8765, log
# outputs/api.log) and the render worker (log outputs/worker.log). What already runs is left alone,
# so the script can be run again at any time; it ends by printing the state.
# Stop everything with scripts/stop.ps1. For development, scripts/start-api.ps1 runs the API in the
# foreground instead.
param([int]$Port = 8765)
$root = Split-Path -Parent $PSScriptRoot
$outputs = Join-Path $root "outputs"
New-Item -ItemType Directory -Force $outputs | Out-Null

function Get-ApiPid {
    $c = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($c) { $c.OwningProcess } else { $null }
}
function Get-WorkerPid {
    $f = Join-Path $outputs "worker.pid"
    if (-not (Test-Path $f)) { return $null }
    $id = [int](Get-Content $f)
    $p = Get-Process -Id $id -ErrorAction SilentlyContinue
    if ($p -and $p.ProcessName -like "python*") { $id } else { $null }
}

if (Get-ApiPid) {
    "API already running (pid $(Get-ApiPid))."
} else {
    $log = Join-Path $outputs "api.log"
    Start-Process -FilePath (Join-Path $root ".venv\Scripts\python.exe") `
        -ArgumentList "-m", "uvicorn", "zoetrope.api:app", "--host", "127.0.0.1", "--port", $Port `
        -WorkingDirectory $root -WindowStyle Hidden -RedirectStandardOutput $log `
        -RedirectStandardError "$log.err" | Out-Null
    for ($i = 0; $i -lt 30 -and -not (Get-ApiPid); $i++) { Start-Sleep -Milliseconds 500 }
    if (Get-ApiPid) { "API started (pid $(Get-ApiPid)). Log: $log" }
    else { "API did not open port $Port within 15 s; see $log.err" }
}

if (Get-WorkerPid) {
    "Worker already running (pid $(Get-WorkerPid))."
} else {
    & (Join-Path $PSScriptRoot "start-worker.ps1")
    for ($i = 0; $i -lt 30 -and -not (Get-WorkerPid); $i++) { Start-Sleep -Milliseconds 500 }
    if (-not (Get-WorkerPid)) { "Worker did not report a pid within 15 s; see outputs\worker.log.err" }
}

""
"API    : $(if (Get-ApiPid) { "up, pid $(Get-ApiPid)" } else { 'DOWN' })"
"Worker : $(if (Get-WorkerPid) { "up, pid $(Get-WorkerPid)" } else { 'DOWN' })"
"Front  : http://127.0.0.1:$Port"
