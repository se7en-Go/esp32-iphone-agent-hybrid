# 开发者在 Windows 上从源码编译 N16R8 固件。普通使用者不要运行本脚本。
# 用法：powershell -File hardware\build_firmware.ps1 [-DebugBuild] [-Sta] [-ApTune]
param(
    [switch]$DebugBuild,
    [switch]$Sta,
    [switch]$ApTune
)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root
$Fqbn = "esp32:esp32:esp32s3:USBMode=default,CDCOnBoot=default,FlashSize=16M,PSRAM=opi,PartitionScheme=app3M_fat9M_16MB"
$Cli = if ($env:ARDUINO_CLI) { $env:ARDUINO_CLI } else { "arduino-cli" }
if (-not (Get-Command $Cli -ErrorAction SilentlyContinue)) {
    Write-Error "没有找到 arduino-cli。普通刷机请用仓库根目录的 flash.ps1，只需要 Python 和 esptool。"
}
if ($Root -match " ") { Write-Error "固件目录不能包含空格：$Root" }
$Defs = " -include $Root/hid_bridge/nimble_cfg.h"
if ($DebugBuild) { $Defs = " -DDEBUG_SERIAL=1" + $Defs }
if ($ApTune) { $Defs = " -DAP_TUNE=1" + $Defs }
if ($Sta) {
    if (-not (Test-Path "hid_bridge\secrets.h")) {
        Write-Error "缺少 hid_bridge\secrets.h。那是开发者测试开关。普通配网请用 scripts\wifi_config.py。"
    }
    $Defs = " -DSTA_FROM_SECRETS=1" + $Defs
}
New-Item -ItemType Directory -Force -Path build, dist | Out-Null
& $Cli compile --config-file "$Root\arduino-cli.yaml" --fqbn $Fqbn `
    --build-property "compiler.cpp.extra_flags=$Defs" `
    --build-property "compiler.c.extra_flags=$Defs" `
    --output-dir "$Root\build" "$Root\hid_bridge"
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
$Merged = Join-Path $Root "build\hid_bridge.ino.merged.bin"
if (-not (Test-Path $Merged)) { Write-Error "编译结束了，但没有找到 $Merged" }
Copy-Item $Merged (Join-Path $Root "dist\esp32s3-n16r8-merged.bin") -Force
Write-Output "已生成 $Root\dist\esp32s3-n16r8-merged.bin"
