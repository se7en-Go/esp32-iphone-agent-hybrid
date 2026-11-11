$ErrorActionPreference = "Stop"
$env:PYTHONUTF8 = "1"
Set-Location (Split-Path -Parent $PSScriptRoot)
& python (Join-Path $PSScriptRoot "run_server.py") @args
exit $LASTEXITCODE
