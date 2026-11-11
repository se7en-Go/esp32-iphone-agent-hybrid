$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)
if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    Write-Error "Python was not found. Install Python 3.10+ from python.org and enable Add python.exe to PATH."
}
& python (Join-Path $PSScriptRoot "setup.py") @args
exit $LASTEXITCODE
