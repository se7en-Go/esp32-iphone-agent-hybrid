$ErrorActionPreference = "Stop"
$env:PYTHONUTF8 = "1"
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}
$OutputEncoding = [System.Text.Encoding]::UTF8
Set-Location (Split-Path -Parent $PSScriptRoot)
if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    Write-Error "Python was not found. Install Python 3.10+ from python.org and enable Add python.exe to PATH."
}
& python (Join-Path $PSScriptRoot "setup.py") @args
exit $LASTEXITCODE
