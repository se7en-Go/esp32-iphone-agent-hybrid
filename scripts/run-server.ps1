$ErrorActionPreference = "Stop"
$env:PYTHONUTF8 = "1"
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}
$OutputEncoding = [System.Text.Encoding]::UTF8
Set-Location (Split-Path -Parent $PSScriptRoot)
& python (Join-Path $PSScriptRoot "run_server.py") @args
exit $LASTEXITCODE
