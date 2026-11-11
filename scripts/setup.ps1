$ErrorActionPreference = "Stop"
$env:PYTHONUTF8 = "1"
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}
$OutputEncoding = [System.Text.Encoding]::UTF8
Set-Location (Split-Path -Parent $PSScriptRoot)
if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    Write-Error "没有找到 Python。下一步：到 https://www.python.org/downloads/windows/ 安装 3.10 或更新版本，勾选 Add python.exe to PATH，重新打开 PowerShell，再运行 scripts\setup.ps1。"
}
& python (Join-Path $PSScriptRoot "setup.py") @args
exit $LASTEXITCODE
