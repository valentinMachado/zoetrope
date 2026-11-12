# Start the HTTP API on http://127.0.0.1:8765 (foreground; Ctrl+C stops it, the worker keeps running).
param([int]$Port = 8765)
$root = Split-Path -Parent $PSScriptRoot
& (Join-Path $root ".venv\Scripts\python.exe") -m uvicorn zoetrope.api:app --host 127.0.0.1 --port $Port
