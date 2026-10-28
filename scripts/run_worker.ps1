# Start the standalone scheduler worker (set DATAFLOW_EMBEDDED_SCHEDULER=false in .env first).
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot
$VenvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $VenvPython)) { Write-Error "Run .\scripts\setup_windows.ps1 first."; exit 1 }
& $VenvPython -m src.orchestration.worker
