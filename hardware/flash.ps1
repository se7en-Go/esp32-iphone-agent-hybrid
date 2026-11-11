$ErrorActionPreference = "Stop"
$env:PYTHONUTF8 = "1"
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}
$OutputEncoding = [System.Text.Encoding]::UTF8
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
$Python = Join-Path $Root "server\.venv\Scripts\python.exe"
if (-not (Test-Path $Python)) { $Python = "python" }
& $Python (Join-Path $Root "scripts\flash.py") @args
exit $LASTEXITCODE
