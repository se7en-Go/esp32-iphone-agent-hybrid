# macOS 从零开始

从 [README 的 Mac 快速开始](../README.md#第一次连接-mac-和-iphone) 可以依次完成接线、配对、镜像和校准。使用 Mac 镜像时，需要按 [本机运行说明](RUNTIME.md#配置位置) 准备和选择 UxPlay 接收环境。

## 准备

- Python 3.10 或更新。没有的话用 python.org 的 macOS 安装包。终端里 `python3 --version` 能打出来即可。
- 一根能传数据的 Type-C 线。只充电的线会让刷机失败。
- 板子有两个口。**COM** 接 Mac。使用有线控制时，**USB** 接 iPhone。走蓝牙时，板子的 USB 口可空着。COM 口上的芯片可能是 FTDI、沁恒 CH343 或 CP210x。端口一般是 `/dev/cu.usbserial-…` 或 `/dev/cu.wchusbserial…`。
- iPhone 打开「设置 → 辅助功能 → 触控 → 辅助触控」。
- 路由器要有 2.4GHz。不要用只有 5GHz 的网络，不要开 AP 隔离。

## 五步

1. 在仓库根目录运行 `./scripts/setup.sh`。
2. 从 GitHub Actions 里最近一次成功的 Firmware 作业下载 `esp32s3-n16r8-merged.bin`，放到 `hardware/dist/`。COM 口接 Mac，运行 `./hardware/flash.sh`。第一次会写完整的 16MB 镜像，请等到脚本自己说刷好了。
3. 运行 `./scripts/wifi.sh --ssid '你家的2.4GHz'`，按提示输入密码。密码只进板子的 NVS，不进仓库。
4. 走蓝牙时，运行 `./scripts/hid-mode.sh pair` 并在 iPhone 蓝牙设置里选择 **ESP32 Agent**。走有线时，把板子的 USB 口接 iPhone，Lightning 机型需要相机转换器。运行 `./scripts/run-local.sh start` 后，在 iPhone 控制中心打开「屏幕镜像」，选择网页提示的接收器。Mac 接收器为 **ESP32 Agent Mac**，板载接收器为 **ESP32 Agent**。浏览器打开 `http://127.0.0.1:8890`，等 device 和 screen 变绿，再点「校准」。需要手动校准时，按网页提示点指针圆点。
5. 运行 `./scripts/mcp-config.sh`，把片段放进 Agent。让它把指针移到屏幕中央，核对后停住。

## 只有网线的电脑才考虑热点

`./scripts/wifi.sh --ap` 会让板子自己开热点。Mac 若只有 Wi-Fi，连上热点就上不了网，云端模型会断。不要把它当默认。

## 镜像列表里没有 ESP32 Agent

先确认 iPhone 和接收器在同一局域网。使用 Mac 接收器时，检查 `./scripts/run-local.sh status`，再在 iPhone 重选 **ESP32 Agent Mac**。板载接收器也可在 Mac 上切到已配置的 UxPlay 接收环境。具体见 [本机运行说明](RUNTIME.md) 和 [故障排查](troubleshooting.md)。
