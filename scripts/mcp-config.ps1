$ErrorActionPreference = "Stop"
$env:PYTHONUTF8 = "1"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
$Python = Join-Path $Root "server\.venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    Write-Error "Run scripts\setup.ps1 first."
}
& $Python (Join-Path $Root "scripts\mcp_config.py") @args
exit $LASTEXITCODE
