$ErrorActionPreference = "Stop"
$env:PYTHONUTF8 = "1"
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}
$OutputEncoding = [System.Text.Encoding]::UTF8
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
$Python = Join-Path $Root "server\.venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    Write-Error "还没有 Python 环境。下一步：在仓库根目录运行 powershell -File scripts\setup.ps1 ，完成后再运行 scripts\mcp-config.ps1。"
}
& $Python (Join-Path $Root "scripts\mcp_config.py") @args
exit $LASTEXITCODE
