# ESP32 iPhone Agent Hybrid

一块 ESP32-S3 N16R8、一台 iPhone、一台电脑，让 Codex、Pi、Claude Code、Cursor 或任何带 MCP 的 Agent 操作真机。电脑端在 macOS 和 Windows 10/11 上是同一套 Python，不经过 Linux 子系统。

基于 [ESP32 iPhone Agent by Henry Yuan](https://github.com/onehumanbeing/esp32-iphone-agent) 的板载 AirPlay 固件，以及 [Kz-Aven/esp32-to-iphone](https://github.com/Kz-Aven/esp32-to-iphone) 的 MCP 工具。固件按 GPL-3.0 分发，电脑端按 MIT 分发，边界写在 [NOTICE](NOTICE) 里。

**还没有在真 iPhone 或真板子上跑过。** CI 能证明的是固件能编译、PyAV 能解 7201 的分帧、串口协议和 MCP 工具在假设备上能往返。真机清单在 [docs/hardware-test.md](docs/hardware-test.md)。

## 线怎么插

| 板子上的口 | 接到 | 作用 |
|---|---|---|
| COM（CH343） | 电脑 | 刷机、命令、读 vtok、把 Wi-Fi 写进 NVS |
| USB（GPIO19/20） | iPhone | USB 鼠标键盘。Lightning 要用相机转换器 |

默认是 STA：板子、手机、电脑连同一个 **2.4GHz** 路由器。换地方就再跑一次配网，不用重烧，也不会把密码放进仓库。板子自己的热点不提供互联网，只适合电脑另外插了网线的时候。

刷机前看一下板子上的 **USB-OTG / IN-OUT** 焊盘。它必须是断开的。焊上之后，电脑的 5V 会倒灌进手机。

## macOS

1. 安装 Python 3.10+（python.org 的安装包即可），在仓库根目录运行 `./scripts/setup.sh`。
2. 从 Actions 的 Firmware 作业下载 `esp32s3-n16r8-merged.bin` 放到 `hardware/dist/`。COM 口接电脑，运行 `./hardware/flash.sh`。
3. `./scripts/wifi.sh --ssid '你家的2.4GHz'`，按提示输入密码。
4. USB 口接手机，打开辅助触控，屏幕镜像选 **ESP32 Agent**。`./scripts/run-local.sh`，打开 `http://127.0.0.1:8890`。先等 device 和 screen 两盏灯变绿，再点校准，点中圆点后第三盏灯才绿。
5. `./scripts/mcp-config.sh`，让 Agent 把指针移到屏幕中央并停住。

细节：[docs/setup-macos.md](docs/setup-macos.md)

## Windows 10 / 11

1. 安装 Python 3.10+，勾选 Add python.exe to PATH。`powershell -ExecutionPolicy Bypass -File scripts\setup.ps1`
2. 下载同一个 `esp32s3-n16r8-merged.bin` 放到 `hardware\dist\`。COM 口接电脑。`powershell -ExecutionPolicy Bypass -File hardware\flash.ps1`
3. `powershell -ExecutionPolicy Bypass -File scripts\wifi.ps1 --ssid "你家的2.4GHz"`
4. USB 口接手机，辅助触控，屏幕镜像选 **ESP32 Agent**。`powershell -ExecutionPolicy Bypass -File scripts\run-server.ps1`，打开 `http://127.0.0.1:8890`，先等 device 和 screen 两盏灯变绿，再点校准，点中圆点后第三盏灯才绿。
5. `powershell -ExecutionPolicy Bypass -File scripts\mcp-config.ps1`

没有 COM 口时再装沁恒 CH343 驱动：https://www.wch.cn/downloads/CH343SER_EXE.html

细节：[docs/setup-windows.md](docs/setup-windows.md)

## 三个灯

网页上 `device`、`screen`、`calibrated` 常亮。缺一个就写着缺什么、下一步是什么。三个都绿才算通。

iPhone 用系统自带的屏幕镜像和辅助触控。不装测试 App，不用 TestFlight，不越狱，不开开发者模式。目标是 iOS 16.7 及以上。**iOS 16/17 能不能在镜像列表里看到这块板子的「ESP32 Agent」，上游没测过，这是第一道硬件关。** 看不到时：把系统升到 iOS 18，或只在 Mac 上改用 UxPlay（`SCREEN_BACKEND=uxplay`，要自己装 UxPlay 和 GStreamer）。Windows 不要设这个变量。

## 速度

慢主要是因为视觉模型在看图，不是因为线。已经做了这些事：一次观察可以 `iphone_batch` 连续点好几下；固定按钮放在 skill 的 `layout.json` 里；`screen_id` 可以复用；视觉模型走 OpenAI 兼容接口，不写死。默认用宿主 Agent 自己的视觉模型，不必再配一把 key。

不要指望秒回。短回复大约 30–60 秒，长回复更久。这是经验范围，不是本仓库的真机测量。Kz-Aven 在 Mac 上的局部记录里，指针定位中位数大约 5 秒。群里几十条未读不要一次回完。换一个更快的视觉模型，是提速最直接的办法。

## 微信

群回复是第二阶段，不挡住上面的五步。它是全自动的：没有草稿，也没有等人批准。说什么只由 [skills/wechat-group-reply/SKILL.md](skills/wechat-group-reply/SKILL.md) 决定。

熔断是协作的，但跳闸之后所有会发出 HID 的接口都会拒绝，只剩状态、截图和重置。见到「操作频繁 / 风控 / 验证码」就停并且不重试。频率和夜间静默要在按下发送之前检查，不是发完再算。`bubble_visible` 只是调用方声明自己看见了新气泡，服务端看不见屏幕，不能证明气泡是真的，也不能把输入框清空或收到 ok 当成发送成功。请用专用运营号，不要用主号。说明在 [docs/wechat.md](docs/wechat.md)。

## 开发者

普通使用者不用安装 arduino-cli。要改固件时，用 `hardware/build_firmware.sh` 或 `hardware/build_firmware.ps1`。FQBN 是：

```
esp32:esp32:esp32s3:USBMode=default,CDCOnBoot=default,FlashSize=16M,PSRAM=opi,PartitionScheme=app3M_fat9M_16MB
```

`CDCOnBoot=default` 让串口留在 COM 口。协议相对 Handsy `5a4fddf` 没有改。决策写在 [ARCHITECTURE.md](ARCHITECTURE.md)。

## 许可证

电脑上的 Python、脚本和 skill 是 MIT，版权属于 Kz-Aven 与 Henry Yuan。`hardware/` 里的固件链接了 GPL-3.0 的 AirPlay 代码，所以固件源码和刷进板子的二进制按 GPL-3.0 提供。把烧好的板子给别人时，要同时给这套固件源码。
