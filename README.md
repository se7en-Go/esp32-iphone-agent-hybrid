# ESP32 iPhone Agent Hybrid

[English](README.en.md)

用电脑上的 Agent 操作你的 iPhone。ESP32-S3 模拟鼠标和键盘，iPhone 的实时画面通过屏幕镜像送回电脑。你可以在 [本机网页](http://127.0.0.1:8890/) 看画面和试操作，也可以通过 MCP 或 CLI 让 Agent 看图、点击、滑动和输入。

iPhone 使用系统自带的辅助触控和屏幕镜像，基础操作无需安装额外 App。中文原样粘贴可以加装一条快捷指令。

## 先准备这些

- ESP32-S3 N16R8 开发板、一根能传数据的线、一台 iPhone，以及装有 Python 3.10 或更新版本的电脑。
- Mac 建议先用独立的 Mac 镜像接收器。Windows 10/11 使用板载镜像，安装入口见下文。
- 给板子配网时选路由器的 **2.4 GHz Wi-Fi**。Mac 接收器模式下，iPhone 和 Mac 需要在能互通的同一局域网。板载镜像还要求电脑和 iPhone 都能连到板子。

| 板上的口 | 接到哪里 | 用途 |
| --- | --- | --- |
| **COM** | 电脑 | 刷固件、配网和下发控制命令。用蓝牙控制时，这根线也要接着。 |
| **USB** | iPhone，可选 | 有线鼠标和键盘。走蓝牙时可以空着。Lightning 机型需要支持数据的相机转换器。 |

连接 iPhone 的 USB 口前，确认板子上的 **USB-OTG / IN-OUT** 焊盘处于断开状态，避免电脑的 5 V 电源倒灌。

## 第一次连接 Mac 和 iPhone

### 1. 安装电脑端并准备板子

在仓库根目录运行安装命令。

```sh
./scripts/setup.sh
```

首次使用空板时，从 [GitHub Actions 的成功构建](https://github.com/se7en-Go/esp32-iphone-agent-hybrid/actions) 下载 Firmware 产物 `esp32s3-n16r8-merged.bin`，放到 `hardware/dist/`。把板子的 COM 口接到 Mac，再运行烧录命令。

```sh
./hardware/flash.sh
```

已经刷好兼容固件的板子可以跳过首次烧录。首次配网或换 Wi-Fi 时运行下面的命令，按提示输入密码。板子通电后会尝试重新连接已保存的网络。

```sh
./scripts/wifi.sh --ssid '你的 Wi-Fi 名称'
```

### 2. 打开辅助触控并配对蓝牙

1. 在 iPhone 打开「设置」→「辅助功能」→「触控」→「辅助触控」，打开总开关。屏幕边缘会出现辅助触控悬浮按钮。
2. 保持 COM 口接电脑。在 Mac 运行 `./scripts/hid-mode.sh pair`，然后在 iPhone「设置」→「蓝牙」里选择名称以 **ESP32 Agent** 开头的设备。配对窗口约 60 秒，超时可以重新运行配对命令。
3. 如果会同时使用有线连接，可以用 `./scripts/hid-mode.sh ble` 明确选蓝牙，或按原配置使用自动模式。

### 3. 启动网页并连上屏幕镜像

Mac 接收器需要先准备好 UxPlay 运行环境，并在本地 `server/.local-data/runtime.env` 中保存 `SCREEN_BACKEND=uxplay`。配置方法见 [Mac 接收器配置](docs/RUNTIME.md#配置位置)。准备好后，在仓库根目录启动服务。

```sh
./scripts/run-local.sh start
./scripts/run-local.sh status
```

在 iPhone 打开「控制中心」→「屏幕镜像」，选 **ESP32 Agent Mac**。若你配置的是板载镜像，接收器名称是 **ESP32 Agent**。网页的连接提示会显示当前应该选择的名称。

打开 [127.0.0.1:8890](http://127.0.0.1:8890/)，等 **device** 和 **screen** 变绿后点「校准」。如果网页要求手动确认，在镜像画面里点指针圆点的中心。**calibrated** 变绿后，先试把指针移动到画面中央，再在测试备忘录试一次点击或输入。

Mac 重启后，可以在仓库根目录再次运行 `./scripts/run-local.sh start`。更新电脑端代码时，用 `./scripts/run-local.sh restart-worker` 保留独立镜像接收器。更多启动和排查命令见 [本机运行说明](docs/RUNTIME.md)。

## 手机上那些设置分别做什么

| 设置 | 什么时候需要 |
| --- | --- |
| **辅助触控** | 使用 ESP32 鼠标指针时打开。悬浮按钮还可用于主屏幕导航和触发粘贴快捷指令。 |
| **屏幕镜像** | Agent 看画面时打开。中断后在控制中心重选接收器。 |
| **辅助触控里的「长按」** | 需要自动粘贴中文或多行文字时，将它绑定到 `ESP32 Agent Clipboard`。 |
| **外接鼠标「按钮 3」** | 想用中键快速回主屏幕时设置。需要新版固件和一次真机验证。 |
| **全键盘控制** | 仅在试用组合键触发快捷指令时配置。基础控制和长按粘贴可以沿用手机现有设置。 |
| **辅助功能快捷键** | 连按侧边按钮调出辅助功能的系统入口。本项目无需为它增加绑定。 |

如需调整指针大小、颜色或自动隐藏，在辅助功能里搜索「指针控制」，部分机型可在辅助触控下找到「指针样式」。改变指针跟踪速度后，重新校准一次。

### 中文和多行文字怎么粘贴

点按、滑动、逐键输入英文和数字可以先用起来。要粘贴中文、多行文字，或保留原样的换行与大小写，再做一次以下设置。

1. 按 [中文粘贴配置](docs/PASTE_INPUT.md#服务器配置) 启用分阶段剪贴板通道。电脑用本机 **8890** 提供控制页面，用局域网可达的 **8891** 提供剪贴板代理。
2. 在 Mac 生成并签名 [快捷指令模板](docs/PASTE_INPUT.md#安装不含秘密的快捷指令模板)，传到 iPhone 后导入为 `ESP32 Agent Clipboard`。导入时填电脑的**局域网地址**和专用剪贴板配对 Key。首次运行时按 iOS 提示确认访问自己的电脑。
3. 在 iPhone 打开「设置」→「辅助功能」→「触控」→「辅助触控」→「长按」，选 `ESP32 Agent Clipboard`。这样 Agent 可以经 ESP32 长按悬浮按钮来启动快捷指令。
4. 首次在测试备忘录练习。Agent 先让快捷指令写入剪贴板，再用 `Cmd+V` 粘贴。若 iOS 弹出「允许粘贴」，核对来源后处理弹窗。看到文字完整且只出现一次，等粘贴事务返回 `completed`。

`Control+Option+P` 是另一个可选入口，在此次真机验收配置上仍待验证。当前可用的路线是长按辅助触控。

### 可选的中键回主屏幕

使用支持中键的固件并通过 BLE 配对后，在 iPhone「设置」→「辅助功能」→「触控」→「辅助触控」→「设备」里，进入已连接的 ESP32，将鼠标 **按钮 3** 设为「主屏幕」。打开备忘录等测试页面，把辅助触控菜单收起。回到 Mac 的仓库根目录，先观察手机画面，再用刚返回的 `screen_id` 验证中键。

```sh
./server/.venv/bin/iphone-agent observe server/.local-data/home-before.jpg
./server/.venv/bin/iphone-agent middle-click-home enable --screen-id '替换为刚返回的 screen_id'
./server/.venv/bin/iphone-agent middle-click-home status
```

验证成功后，CLI 会用新画面确认回到主屏幕。需要取消时，运行 `iphone-agent middle-click-home revoke`。更多命令见 [MCP 和 CLI 指南](docs/MCP_CLI.md)。

## 在 Windows 上使用

Windows 10/11 使用系统自带的 PowerShell。把 COM 接电脑，按照 [Windows 安装说明](docs/setup-windows.md) 安装环境、烧录和配网，再运行 `scripts/run-server.ps1`。iPhone 的「屏幕镜像」选择 **ESP32 Agent**，即板载接收器，之后也在 [127.0.0.1:8890](http://127.0.0.1:8890/) 校准和操作。

Windows 自动化测试已经通过，物理 Windows 主机连接 ESP32 和 iPhone 的完整流程仍待真机验证。

## 让 Agent 开始操作

- **网页**可以看实时画面、手动点击和校准。使用网页内置的模型任务时，按 [模型配置](docs/RUNTIME.md#配置位置) 填写模型信息。
- **MCP** 在 Mac 运行 `./scripts/mcp-config.sh`，在 Windows 使用 `scripts/mcp-config.ps1`，把输出的配置加到你的 Agent。由 Agent 自己读图和判断时，无需再给网页后端配置一把模型 Key。
- **CLI** 安装完成后，在虚拟环境中运行 `iphone-agent --help` 查看命令。截图、操作和粘贴步骤见 [MCP 和 CLI 指南](docs/MCP_CLI.md)；`Pi` / 本地 CLI Agent 速查见 [Pi 与本地 CLI 快速用法](docs/PI_CLI.md)。

第一次可以让 Agent「截图，把指针移到屏幕中央并停住」。启用中文粘贴后，在测试备忘录验完内容和结果，再测试社交 App 的发送。发送前核对会话和消息，发送后用新画面确认结果。

**真机已验证** Mac 镜像重连、ESP32 蓝牙点击、中键回主屏幕、Spotlight 打开应用、备忘录双击选词和带「允许粘贴」弹窗的中文粘贴。微信发送还待单独验收。更多见 [真机验收记录](docs/control-completion-plan.md)。

## 更多说明

[常见连接问题](docs/troubleshooting.md) · [运行与重启](docs/RUNTIME.md) · [微信功能说明](docs/wechat.md) · [架构与开发](ARCHITECTURE.md) · [项目声明](NOTICE)
