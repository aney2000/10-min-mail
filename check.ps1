# The commit gate, for Windows / PowerShell users who do not have `make`.
#
# Mirrors the `check` target in the Makefile exactly. Runs the cheap checks
# first so failures surface fast, tests last because they are slowest.
#
# Usage:   .\check.ps1

$ErrorActionPreference = "Stop"
$python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"

if (-Not (Test-Path $python)) {
    Write-Host "No virtualenv found at .venv -- falling back to 'python' on PATH." -ForegroundColor Yellow
    $python = "python"
}

function Invoke-Step {
    param([string]$Name, [string[]]$Arguments)

    Write-Host ""
    Write-Host "--- $Name " -NoNewline -ForegroundColor Cyan
    Write-Host ("-" * (60 - $Name.Length)) -ForegroundColor DarkGray

    & $python @Arguments
    if ($LASTEXITCODE -ne 0) {
        Write-Host ""
        Write-Host "FAILED: $Name" -ForegroundColor Red
        exit $LASTEXITCODE
    }
}

Invoke-Step "lint (ruff check)"     @("-m", "ruff", "check", ".")
Invoke-Step "format (ruff format)"  @("-m", "ruff", "format", "--check", ".")
Invoke-Step "typecheck (mypy)"      @("-m", "mypy")
Invoke-Step "test (pytest)"         @("-m", "pytest")

Write-Host ""
Write-Host "All checks passed." -ForegroundColor Green
