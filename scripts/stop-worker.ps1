# Stop the worker after its current task (stop file), or at once with -Force (the running take
# goes back to the queue when the worker restarts).
param([switch]$Force)
$root = Split-Path -Parent $PSScriptRoot
$pidFile = Join-Path $root "outputs\worker.pid"
if ($Force) {
    if (Test-Path $pidFile) {
        $id = [int](Get-Content $pidFile)
        try { Stop-Process -Id $id -Force -ErrorAction Stop; "Worker $id killed." } catch { "No process $id." }
    } else { "No pid file." }
} else {
    New-Item -ItemType File -Force (Join-Path $root "outputs\worker.stop") | Out-Null
    "Stop requested: the worker exits after its current task."
}
