# macOS 从零开始

Apple Silicon 和 Intel 都用同一套步骤。不需要先装 Homebrew。不需要 Xcode，也不需要 Handsy 的 iOS App。

## 准备

- Python 3.10 或更新。没有的话用 python.org 的 macOS 安装包。终端里 `python3 --version` 能打出来即可。
- 一根能传数据的 Type-C 线。只充电的线会让刷机失败。
- 板子有两个口。**COM** 接 Mac，**USB** 接 iPhone。
- iPhone 打开「设置 → 辅助功能 → 触控 → 辅助触控」。
- 路由器要有 2.4GHz。不要用只有 5GHz 的网络，不要开 AP 隔离。

## 五步

1. 在仓库根目录运行 `./scripts/setup.sh`。
2. 从 GitHub Actions 里最近一次成功的 Firmware 作业下载 `esp32s3-n16r8-merged.bin`，放到 `hardware/dist/`。COM 口接 Mac，运行 `./hardware/flash.sh`。第一次会写完整的 16MB 镜像，请等到脚本自己说刷好了。
3. 运行 `./scripts/wifi.sh --ssid '你家的2.4GHz'`，按提示输入密码。密码只进板子的 NVS，不进仓库。
4. USB 口接 iPhone（Lightning 用相机转换器）。控制中心 → 屏幕镜像 → **ESP32 Agent**。运行 `./scripts/run-local.sh`，浏览器打开 `http://127.0.0.1:8890`。先等 device 和 screen 两盏灯变绿，再点「校准」，点中圆点后第三盏灯才绿。
5. 运行 `./scripts/mcp-config.sh`，把片段放进 Agent。让它把指针移到屏幕中央，核对后停住。

## 只有网线的电脑才考虑热点

`./scripts/wifi.sh --ap` 会让板子自己开热点。Mac 若只有 Wi-Fi，连上热点就上不了网，云端模型会断。不要把它当默认。

## 镜像列表里没有 ESP32 Agent

先看 `docs/troubleshooting.md`。iOS 16/17 镜像到这块板子的接收器，上游没有测过，这是第一道硬件关。退路是把系统升到 iOS 18，或仅在这台 Mac 上安装 UxPlay 和 GStreamer 后设置 `SCREEN_BACKEND=uxplay`。Windows 没有这条退路。
