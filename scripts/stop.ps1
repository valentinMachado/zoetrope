# Stop the whole server: the API at once (it never touches a render), the worker after its
# current task (stop file), or at once with -Force (the running take goes back to the queue when
# the worker restarts). Safe to run when nothing is running.
param([switch]$Force, [int]$Port = 8765)
$root = Split-Path -Parent $PSScriptRoot

$conns = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($conns) {
    foreach ($id in ($conns | Select-Object -ExpandProperty OwningProcess -Unique)) {
        # The venv's python.exe is a launcher: stop the listener and its launcher parent.
        $parent = (Get-CimInstance Win32_Process -Filter "ProcessId = $id").ParentProcessId
        $pp = Get-Process -Id $parent -ErrorAction SilentlyContinue
        Stop-Process -Id $id -Force -ErrorAction SilentlyContinue
        if ($pp -and $pp.ProcessName -like "python*") { Stop-Process -Id $parent -Force -ErrorAction SilentlyContinue }
        "API stopped (pid $id)."
    }
} else {
    "API not running."
}

$pidFile = Join-Path $root "outputs\worker.pid"
$alive = $false
if (Test-Path $pidFile) {
    $p = Get-Process -Id ([int](Get-Content $pidFile)) -ErrorAction SilentlyContinue
    $alive = [bool]($p -and $p.ProcessName -like "python*")
}
if ($alive) { & (Join-Path $PSScriptRoot "stop-worker.ps1") -Force:$Force } else { "Worker not running." }
