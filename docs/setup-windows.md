# Windows 10 / 11 从零开始

用系统自带的 PowerShell 和 python.org 的 Python。不要安装 Linux 子系统，脚本也不会去调用它。

## 准备

- 到 https://www.python.org/downloads/windows/ 安装 Python 3.10 或更新，勾选 **Add python.exe to PATH**。新开一个 PowerShell，`python --version` 能打出来。
- 若设备管理器里没有 COM 口，安装沁恒 CH343 驱动：https://www.wch.cn/downloads/CH343SER_EXE.html 。Win10/11 有时免驱动，没有口再装。
- 线要能传数据。板子上标着 **COM** 的口接电脑，标着 **USB** 的口接手机。
- iPhone 打开辅助触控。路由器要有 2.4GHz，且没有 AP 隔离。

## 五步

在仓库根目录打开 PowerShell：

1. `powershell -ExecutionPolicy Bypass -File scripts\setup.ps1`
2. 从 GitHub Actions 的 Firmware 作业下载 `esp32s3-n16r8-merged.bin`，放到 `hardware\dist\`。COM 口接好后运行 `powershell -ExecutionPolicy Bypass -File hardware\flash.ps1`。第一次会写完整的 16MB 镜像，请等到脚本自己说刷好了。
3. `powershell -ExecutionPolicy Bypass -File scripts\wifi.ps1 --ssid "你家的2.4GHz"`，按提示输入密码。
4. USB 口接 iPhone。控制中心 → 屏幕镜像 → **ESP32 Agent**。`powershell -ExecutionPolicy Bypass -File scripts\run-server.ps1`，浏览器打开 `http://127.0.0.1:8890`。先等 device 和 screen 两盏灯变绿，再点校准。
5. `powershell -ExecutionPolicy Bypass -File scripts\mcp-config.ps1`，把片段放进 Agent。

服务只监听本机 8890，一般不用为它开入站防火墙。若 Windows 询问 Python 能否访问网络，允许专用网络的出站即可，那是电脑去连板子的 7201。

## 常见下一步

- 没有 COM 口：换线、换 COM 那个口、装上面的驱动，或设置 `$env:HIDAGENT_PORT = "COM5"`。
- 刷机失败：关掉串口监视器，再试 `--baud 115200`。仍失败就按住 BOOT 插拔 COM 口后立刻重跑。
- 不要把 `SCREEN_BACKEND` 设成 `uxplay`。那个退路在 Windows 上会直接拒绝。
