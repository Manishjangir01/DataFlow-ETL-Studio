# DataFlow ETL Studio - one-time setup for Windows (PowerShell)
# Usage (from the project folder):  .\scripts\setup_windows.ps1
# Creates .venv, installs requirements, creates .env with a new encryption key and initialises the database.

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

function Invoke-Checked {
    param([string]$Exe, [string[]]$Arguments)
    & $Exe @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Command failed ($LASTEXITCODE): $Exe $($Arguments -join ' ')" }
}

# 1. Find Python 3.12 or 3.13 (prefer the Windows 'py' launcher)
$Exe = $null
$ExeArgs = @()
if (Get-Command py -ErrorAction SilentlyContinue) {
    # 'py' writes to stderr when a version is missing; do not treat that as fatal.
    $ErrorActionPreference = "Continue"
    foreach ($Version in @("3.12", "3.13")) {
        & py "-$Version" -c "import sys" 2>$null | Out-Null
        if ($LASTEXITCODE -eq 0) { $Exe = "py"; $ExeArgs = @("-$Version"); break }
    }
    $ErrorActionPreference = "Stop"
}
if (-not $Exe) {
    if (Get-Command python -ErrorAction SilentlyContinue) { $Exe = "python" }
    else { throw "Python 3.12 or 3.13 was not found. Install it from https://www.python.org/downloads/windows/ (tick 'Add python.exe to PATH')." }
}
Write-Host "Using Python: $Exe $ExeArgs" -ForegroundColor Cyan

# 2. Virtual environment
if (-not (Test-Path ".venv\Scripts\python.exe")) {
    Write-Host "Creating virtual environment .venv ..." -ForegroundColor Cyan
    Invoke-Checked $Exe ($ExeArgs + @("-m", "venv", ".venv"))
}
$VenvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"

# 3. Dependencies
Write-Host "Installing dependencies (this can take a few minutes) ..." -ForegroundColor Cyan
Invoke-Checked $VenvPython @("-m", "pip", "install", "--upgrade", "pip")
Invoke-Checked $VenvPython @("-m", "pip", "install", "-r", "requirements.txt")

# 4. Configuration file with a fresh encryption key
if (-not (Test-Path ".env")) {
    Write-Host "Creating .env with a new encryption key ..." -ForegroundColor Cyan
    $Key = (& $VenvPython -m src.cli generate-key).Trim()
    if ($LASTEXITCODE -ne 0 -or -not $Key) { throw "Could not generate an encryption key." }
    $Lines = Get-Content ".env.example" | ForEach-Object {
        if ($_ -match '^DATAFLOW_SECRET_KEY=') { "DATAFLOW_SECRET_KEY=$Key" } else { $_ }
    }
    $Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllLines((Join-Path $ProjectRoot ".env"), $Lines, $Utf8NoBom)
} else {
    Write-Host ".env already exists - left unchanged." -ForegroundColor Yellow
}

# 5. Database
Invoke-Checked $VenvPython @("-m", "src.cli", "init-db")

Write-Host ""
Write-Host "Setup complete. Start the app with:  .\scripts\run_app.ps1" -ForegroundColor Green
Write-Host "Run the tests with:                .\scripts\run_tests.ps1" -ForegroundColor Green
