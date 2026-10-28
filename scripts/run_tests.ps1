# Run the automated test suite.  Extra arguments go to pytest, e.g.  .\scripts\run_tests.ps1 -k upsert
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot
$VenvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $VenvPython)) { Write-Error "Run .\scripts\setup_windows.ps1 first."; exit 1 }
& $VenvPython -m pytest @args
exit $LASTEXITCODE
