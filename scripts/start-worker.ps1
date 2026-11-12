# Start the render worker detached from this terminal (it survives closing it).
# Log: outputs/worker.log (crashes: worker.log.err). Stop it with scripts/stop-worker.ps1.
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root "tools\Wan2GP\venv\Scripts\python.exe"
$log = Join-Path $root "outputs\worker.log"
New-Item -ItemType Directory -Force (Join-Path $root "outputs") | Out-Null
$env:PYTHONPATH = Join-Path $root "src"
$env:PYTHONUNBUFFERED = "1"
$p = Start-Process -FilePath $python -ArgumentList "-m", "zoetrope.worker" -WorkingDirectory $root `
    -WindowStyle Hidden -RedirectStandardOutput $log -RedirectStandardError "$log.err" -PassThru
"Worker started (launcher pid $($p.Id); the worker writes its own pid to outputs\worker.pid). Log: $log"
