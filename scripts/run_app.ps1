# Start the DataFlow ETL Studio web app.  Extra arguments are passed to Streamlit,
# e.g.  .\scripts\run_app.ps1 --server.port 8502
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot
$VenvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $VenvPython)) { Write-Error "Run .\scripts\setup_windows.ps1 first."; exit 1 }
& $VenvPython -m streamlit run app.py @args
