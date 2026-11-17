# ESP32 iPhone Agent Hybrid

一块 ESP32-S3 N16R8、一台 iPhone、一台电脑，让 Codex、Pi、Claude Code、Cursor 或任何带 MCP 的 Agent 操作真机。macOS 和 Windows 10/11 共用 Python 控制核心；Mac 还可使用独立的 AirPlay 接收器，不经过 Linux 子系统。

基于 [ESP32 iPhone Agent by Henry Yuan](https://github.com/onehumanbeing/esp32-iphone-agent) 的板载 AirPlay 固件，以及 [Kz-Aven/esp32-to-iphone](https://github.com/Kz-Aven/esp32-to-iphone) 的 MCP 工具。固件按 GPL-3.0 分发，电脑端按 MIT 分发，边界写在 [NOTICE](NOTICE) 里。

**ESP32 蓝牙 HID + Mac AirPlay 已有真机实测，连续自主任务的完整验收仍待完成。** 已验证蓝牙输入、手机画面接收，以及重启网页 worker 时保留独立接收器和镜像连接。单次点击、连接成功或测试通过，不代表任意 App 都能连续自动操作。CI 的固件编译、模拟串口、视频与 MCP 测试和真机结果分别看待；验收清单见 [docs/hardware-test.md](docs/hardware-test.md)。

## 线怎么插

| 板子上的口 | 接到 | 作用 |
|---|---|---|
| COM（USB 转串口，可能是 CH343 / FTDI / CP210x） | 电脑 | 刷机、命令、读 vtok、把 Wi-Fi 写进 NVS |
| USB（GPIO19/20） | iPhone（可选） | USB 鼠标键盘。Lightning 要用相机转换器。也可在 iPhone 蓝牙里配对 **ESP32 Agent**，走已实测的蓝牙 HID，手机不接此线 |

默认是 STA：板子、手机、电脑连同一个 **2.4GHz** 路由器。换地方就再跑一次配网，不用重烧，也不会把密码放进仓库。板子自己的热点不提供互联网，只适合电脑另外插了网线的时候。

蓝牙传控制动作，屏幕镜像走 Wi-Fi。Mac 接收器模式下，iPhone 与 Mac 在同一局域网，画面直接传给 Mac；ESP32 仍通过 COM 接收命令，再经蓝牙控制手机。

刷机前看一下板子上的 **USB-OTG / IN-OUT** 焊盘。它必须是断开的。焊上之后，电脑的 5V 会倒灌进手机。

## macOS

1. 安装 Python 3.10+（python.org 的安装包即可），在仓库根目录运行 `./scripts/setup.sh`。
2. 从 Actions 的 Firmware 作业下载 `esp32s3-n16r8-merged.bin` 放到 `hardware/dist/`。COM 口接电脑，运行 `./hardware/flash.sh`。
3. `./scripts/wifi.sh --ssid '你家的2.4GHz'`，按提示输入密码。
4. 打开 iPhone 辅助触控。USB 口可接手机；使用蓝牙时，COM 口仍接电脑，运行 `./scripts/hid-mode.sh pair`，在 iPhone 设置 → 蓝牙里选择 **ESP32 Agent**（名字后面可能有四位字母）。运行 `./scripts/run-local.sh start`，打开 `http://127.0.0.1:8890`。屏幕镜像选择网页提示的接收器：板载模式是 **ESP32 Agent**，Mac 模式是 **ESP32 Agent Mac**。先等 device 和 screen 两盏灯变绿，再校准；第三盏灯变绿表示可以开始验证动作。
5. `./scripts/mcp-config.sh`，让 Agent 把指针移到屏幕中央并停住。

日常启动、状态、只重启网页及停止：[docs/RUNTIME.md](docs/RUNTIME.md)。已有本地 UxPlay runtime 时入口自动发现；无需服务端模型 Key 即可使用宿主 MCP。首次安装细节：[docs/setup-macos.md](docs/setup-macos.md)。

## Windows 10 / 11

1. 安装 Python 3.10+，勾选 Add python.exe to PATH。`powershell -ExecutionPolicy Bypass -File scripts\setup.ps1`
2. 下载同一个 `esp32s3-n16r8-merged.bin` 放到 `hardware\dist\`。COM 口接电脑。`powershell -ExecutionPolicy Bypass -File hardware\flash.ps1`
3. `powershell -ExecutionPolicy Bypass -File scripts\wifi.ps1 --ssid "你家的2.4GHz"`
4. USB 口接手机，打开辅助触控，屏幕镜像选 **ESP32 Agent**。使用蓝牙时，COM 口仍接电脑，运行 `powershell -ExecutionPolicy Bypass -File scripts\hid-mode.ps1 pair`，在 iPhone 设置 → 蓝牙里选择 **ESP32 Agent**。运行 `powershell -ExecutionPolicy Bypass -File scripts\run-server.ps1`，打开 `http://127.0.0.1:8890`，先等 device 和 screen 两盏灯变绿，再校准。Windows 使用前台板载镜像路径；Mac 的蓝牙实测不等于 Windows 全链路已经验收。
5. `powershell -ExecutionPolicy Bypass -File scripts\mcp-config.ps1`

Mac 上端口可能是 `/dev/cu.usbserial-…` 或 `/dev/cu.wchusbserial…`，Windows 是 `COMx`。FTDI 在 macOS 上通常不用装驱动。Windows 没有 COM 口时，按芯片装驱动：FTDI 用 VCP 驱动 https://ftdichip.com/drivers/vcp-drivers/ ，沁恒用 CH343 驱动 https://www.wch.cn/downloads/CH343SER_EXE.html 。

细节：[docs/setup-windows.md](docs/setup-windows.md)

## 三个灯

网页上 `device`、`screen`、`calibrated` 常亮。缺一个就写着缺什么、下一步是什么。三个都绿才算通。

iPhone 用系统自带的屏幕镜像和辅助触控。不装测试 App，不用 TestFlight，不越狱，不开开发者模式。目标是 iOS 16.7 及以上，但板载接收器仍需按机型、系统和网络验收。板载镜像不稳定时，Mac 可使用已安装的 UxPlay / GStreamer（`SCREEN_BACKEND=uxplay`）；Windows 保留板载后端。三个灯表示连接与校准状态，任务是否完成仍要核对动作后的新画面。

## 速度

每步耗时包括模型决策、蓝牙指针定位与纠偏、镜像传输和新画面核验。先看返回的 `timing_ms` 和画面新鲜度，再判断瓶颈；换模型不能解决指针漂移或旧画面。

`screen_id` 在 30 秒内单次有效，画面或连接变化会使它失效。动作返回的新图和**新** `screen_id` 可用于下一步，不可重复消费旧 ID。`iphone_batch` 最多执行 8 步，只适合不依赖新观察的动作；未验证的点击、跨页面变化、输入焦点变化会停止后续步骤。不要把「点输入框、输入、发送」作为一批盲续。默认由宿主 Agent 看图与决策，不必再配一把模型 Key；网页内置模型任务是可选入口。

中文和数字可配置[快捷指令粘贴输入](docs/PASTE_INPUT.md)：服务端通过已绑定的组合键触发手机快捷指令，领取文本并确认复制与焦点后发送 `cmd+v`，等待粘贴完成确认后才允许下一次复制。未配置时，`type` 保留 ASCII 逐键输入；HID 本身没有剪贴板通道。

## 微信

群回复是第二阶段，不挡住上面的五步。它是全自动的：没有草稿，也没有等人批准。说什么只由 [skills/wechat-group-reply/SKILL.md](skills/wechat-group-reply/SKILL.md) 决定。

熔断是协作的，但跳闸之后所有会发出 HID 的接口都会拒绝，只剩状态、截图和重置。见到「操作频繁 / 风控 / 验证码」就停并且不重试。频率和夜间静默要在按下发送之前检查，不是发完再算。`bubble_visible` 只是调用方声明自己看见了新气泡，服务端看不见屏幕，不能证明气泡是真的，也不能把输入框清空或收到 ok 当成发送成功。请用专用运营号，不要用主号。说明在 [docs/wechat.md](docs/wechat.md)。

## 开发者

普通使用者不用安装 arduino-cli。要改固件时，用 `hardware/build_firmware.sh` 或 `hardware/build_firmware.ps1`。FQBN 是：

```
esp32:esp32:esp32s3:USBMode=default,CDCOnBoot=default,FlashSize=16M,PSRAM=opi,PartitionScheme=app3M_fat9M_16MB
```

`CDCOnBoot=default` 让串口留在 COM 口。固件以 Handsy `5a4fddf` 为初始基线，保留 `<id> <cmd>` 串口命令帧。当前传输路径与限制写在 [ARCHITECTURE.md](ARCHITECTURE.md)。

## 许可证

电脑上的 Python、脚本和 skill 是 MIT，版权属于 Kz-Aven 与 Henry Yuan。`hardware/` 里的固件链接了 GPL-3.0 的 AirPlay 代码，所以固件源码和刷进板子的二进制按 GPL-3.0 提供。把烧好的板子给别人时，要同时给这套固件源码。
