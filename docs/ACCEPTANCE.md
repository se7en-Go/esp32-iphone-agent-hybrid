# seven 的真机验收清单（ESP32-S3-N16R8 版）

> 本次验收用 **ESP32-S3-N16R8**：两个 Type-C 口，16MB Flash，8MB PSRAM，排针已焊好。仓库就是按这块板子设计的。
>
> **这块板子平时还跑录音笔固件。验收做完后，要把录音笔固件刷回去**（录音笔固件不在本仓库，刷回方法按录音笔项目自己的说明）。
>
> XIAO ESP32S3 暂不支持，需要另外适配。本清单不涉及 XIAO。

核对版本：基于提交 `6520a47`，工作树 `e65acae`（= main `62326a4`）。本清单只依据代码和文档编写，没有烧录，也没有做真机测试。

仓库明确写着“还没有在真 iPhone 或真板子上跑过”。下面全部是**待执行的验收项目，不是已经通过的证明**。出处：`README.md:7`；`docs/hardware-test.md:3`。

执行规则：只有达到“通过标准”才勾选；遇到阻断就记录原因，不继续发送消息。命令都在仓库根目录执行。文中的 MCP 调用交给已连接本项目的 Agent 执行，不是粘贴进 PowerShell 的命令。

## 0．准备：烧录、插线、配网、配对

### 0.1 认口和插线

| 板子上的口 | 接到 | 作用 |
|---|---|---|
| **COM**（CH343 USB 转串口） | 电脑 | 刷机、发命令、读 vtok、把 Wi-Fi 写进 NVS，同时给板子供电 |
| **USB**（GPIO19/20，原生 USB） | iPhone | USB 鼠标键盘（HID）。Lightning 手机要用相机转换器 |

出处：`README.md:11–16`；`ARCHITECTURE.md:7、31`。

- [ ] **检查 USB-OTG / IN-OUT 焊盘是断开的。** 刷机前看一眼板子背面或正面的这对焊盘。
  **通过标准：** 焊盘没有被焊锡短接。短接后电脑的 5V 会倒灌进手机。
  **失败记录：** 焊盘的清晰照片。焊盘被短接时停下，不要接手机。
  **出处：** `README.md:18`；`scripts/flash.py:94`；`docs/troubleshooting.md:23–25`。

- [ ] **用能传数据的 Type-C 线，把 COM 口接电脑。** 只能充电的线刷不了机，也不会出现串口。
  **通过标准：** 电脑上能看到沁恒串口。
  - **Windows：** 设备管理器 →「端口 (COM 和 LPT)」里出现一个 COM 口（例如 `COM5`）。没有的话，安装沁恒 CH343 驱动：https://www.wch.cn/downloads/CH343SER_EXE.html （Win10/11 有时不用装驱动）。
  - **Mac：** 出现 `/dev/cu.wchusbserial…` 这样的设备，仓库里的示例是 `/dev/cu.wchusbserial110`。
  - 程序只自动认 VID `0x1A86`、PID `0x55D3 / 0x55D4 / 0x7523 / 0x5523` 的沁恒串口。只找到一个就直接用；找到多个，或者想手动指定，就设置 `HIDAGENT_PORT`（Windows：`$env:HIDAGENT_PORT = "COM5"`），或者给脚本加 `--port`。

  **失败记录：** Windows 设备管理器「端口」的截图；Mac 上 `ls /dev/cu.*` 的输出；线和口的照片。
  **出处：** `docs/setup-windows.md:8、26`；`server/.env.example:7–8`；`server/hidagent/device.py:25–26、115–126`；`scripts/flash.py:44–68`。

### 0.2 安装电脑环境

- [ ] **在每台要用的电脑上安装 Python 和依赖。** Windows 用自带的 PowerShell，不装 WSL2。

  | macOS | Windows 10/11 |
  |---|---|
  | 安装 Python 3.10 或更新，`python3 --version` 能打出来 | 从 python.org 安装 Python 3.10 或更新，勾选 Add python.exe to PATH；新开一个 PowerShell，`python --version` 能打出来 |
  | `./scripts/setup.sh` | `powershell -ExecutionPolicy Bypass -File scripts\setup.ps1` |

  **通过标准：** 输出“Python 环境好了。”，没有报错。脚本会建好 `server/.venv`，装上 PyAV、pyserial、esptool、pypinyin 和 MCP。
  **失败记录：** Python 版本，以及安装窗口的完整输出，尤其是 pip 的最后几行。仓库没有给安装过程配日志文件。
  **出处：** `docs/setup-macos.md:7、15`；`docs/setup-windows.md:3、7、16`；`scripts/setup.py:15–67`；`server/pyproject.toml:11–24`。

### 0.3 刷入 N16R8 固件

- [ ] **下载预编译固件。** 打开本仓库的 GitHub Actions，找最近一次成功的 Firmware 作业，下载 `esp32s3-n16r8-merged.bin`，放进 `hardware/dist/`。
  **通过标准：** 文件在 `hardware/dist/esp32s3-n16r8-merged.bin`。
  **出处：** `scripts/flash.py:17–39`；`docs/setup-windows.md:17`；`docs/setup-macos.md:16`。

- [ ] **COM 口接电脑，运行刷机脚本。** 先关掉所有串口监视器和已经启动的服务。

  | macOS | Windows |
  |---|---|
  | `./hardware/flash.sh` | `powershell -ExecutionPolicy Bypass -File hardware\flash.ps1` |

  可选参数：`--port <口>`、`--bin <文件>`、`--baud <波特率>`（默认 460800）。脚本用 esptool 从 `0x0` 写入完整的 16MB 镜像，第一次会比较久。
  **通过标准：** 脚本输出“刷好了。下一步：运行配网脚本……”。
  **失败记录：** esptool 的完整输出、端口名、波特率。依次试这几步：关掉占用这个口的程序；确认插的是 COM 口；加 `--baud 115200` 重跑；仍然失败就按住 BOOT 再插拔 COM 口，然后马上重跑。不要对 USB 口运行 esptool。
  **出处：** `scripts/flash.py:81–107`；`docs/troubleshooting.md:15`；`docs/setup-windows.md:27`。

- [ ] **确认串口能回应命令。** 用串口工具打开 COM 口（115200），逐行发送 `0 ping` 和 `0 status`。用哪个串口工具**待确认**（仓库没有指定）；不想装工具的话，可以直接做 0.4 的配网，配网脚本能跑通也说明串口正常。
  **通过标准：** `0 ping` 回 `0 ok`；`0 status` 回一行以 `0 ` 开头的 JSON。
  **失败记录：** 发送和收到的原文、端口名。状态 JSON 里重点记 `fw`、`up`、`rst`、`usb`、`net`、`ip`。
  **出处：** `hardware/hid_bridge/hid_bridge.ino:111、378、470`；`docs/hardware-test.md:15`。

### 0.4 配网（2.4GHz，STA）

- [ ] **把板子配进和手机、电脑同一个 2.4GHz 路由器。** 路由器不要开 AP 隔离或访客隔离，要放行 mDNS。企业 802.1X 网络和需要网页登录的 Wi-Fi 不行。

  | macOS | Windows |
  |---|---|
  | `./scripts/wifi.sh --ssid '你家的2.4GHz'` | `powershell -ExecutionPolicy Bypass -File scripts\wifi.ps1 --ssid "你家的2.4GHz"` |

  密码按提示输入，不要写在命令行里。密码只存进板子的 NVS，不会进仓库。
  **通过标准：** 脚本显示 `net=sta`、一个不是 `0.0.0.0` 的 IP，以及“板子已经连上路由器”。
  **失败记录：** 去掉密码后的脚本输出、路由器频段和隔离设置的截图。不要公开 `0 netinfo` 的原文（里面有 vtok）。
  **出处：** `scripts/wifi_config.py:23–80`；`ARCHITECTURE.md:83`；`docs/troubleshooting.md:19`。

- [ ] **重启后不用重新配网。** 拔插一次 COM 线，再看 `0 status`。
  **通过标准：** 板子自动连回原来的网络。换了网络才需要再跑一次配网脚本，不用重新刷机。
  **失败记录：** 重启前后 `0 status` 里的 `net`、`ip`。
  **出处：** `hardware/hid_bridge/netcfg.cpp:174–194`；`docs/hardware-test.md` 第 6 步。

- [ ] **只有电脑插着网线时才考虑热点模式。** `wifi.sh --ap` / `wifi.ps1 --ap` 会让板子自己开热点。电脑如果只靠 Wi-Fi 上网，连上这个热点就断网了，云端模型也会断。不要把它当默认方式。
  **出处：** `docs/setup-macos.md:20–23`；`scripts/wifi_config.py:78`。

### 0.5 接 iPhone、供电和配对

- [ ] **USB 口接 iPhone。** 先解锁手机，保持亮屏。iPhone 15/16 这类 USB-C 手机用 C 对 C 数据线；Lightning 手机用相机转换器。第一次插上时如果手机问要不要允许有线配件，选「允许」，之后在「设置 → 隐私与安全性 → 有线配件」里确认它没被关掉。
- [ ] **打开辅助触控：** 「设置 → 辅助功能 → 触控 → 辅助触控」。
  **通过标准：** 屏幕上出现辅助触控的圆点和指针，后面的步骤能控制它移动。
  **失败记录：** 手机的配件提示截图、插线照片。没有出现鼠标时，先解锁，拔掉手机那头的线再插上。
  **出处：** `docs/hardware-test.md:9–17`。

- [ ] **供电：COM 口一直插在电脑上给板子供电。**
  **通过标准：** 板子不会反复重启；手机不弹「配件耗电量太大」；`0 status` 的 `rst` 不是 brownout。
  **失败记录：** `0 status` 原文，重点是 `rst` 和 `up`；手机提示截图。刚上电时显示一次 power-on 不算故障，要结合是否反复重启一起看。
  **出处：** `docs/troubleshooting.md:23–25`；`docs/hardware-test.md:16`。

- [ ] **蓝牙配对：本次验收不做。** BLE 和 7202 是实验功能，这一阶段不交付。只有做实验时才在串口发 `0 ble pair on`，配对窗口只开 60 秒；平时不要打开。
  **通过标准：** 不开蓝牙也能完成后面所有章节。
  **出处：** `ARCHITECTURE.md:31`；`hardware/hid_bridge/hid_bridge.ino:466–467`；`docs/hardware-test.md:15`。

## 1．第一关：iPhone 画面投到电脑

### 1.1 记录真实设备与系统版本

- [ ] **记录 seven 实际使用的 iPhone 型号、iOS 版本、接口和电脑系统版本。**
  
  **通过标准：**记录对应真实设备；不能把文档中的“iPhone 16、iOS 27.2.1”当成 seven 已有的配置或通过证明。  
  **失败记录：**手机系统版本截图、Windows/macOS 版本、线材或转换器型号。  
  **待确认：**所有实际 iOS 版本与板载 AirPlay 的兼容性。仓库目标为 iOS 16.7+，特别注明 iOS 16/17 未验证；文档列出的 iOS 27 也未在本仓库验证。  
  **出处：**`README.md:46`；`docs/hardware-test.md:3–15`；`ARCHITECTURE.md:32`。

### 1.2 启动服务并打开网页

- [ ] **关闭串口监视器，启动服务。**

  | macOS | Windows |
  |---|---|
  | `./scripts/run-local.sh` | `powershell -ExecutionPolicy Bypass -File scripts\run-server.ps1` |

  打开 `http://127.0.0.1:8890`，保留启动终端。Mac 的实际入口叫 `run-local.sh`，不要误写成不存在的 `run-server.sh`。

  必须指定端口时，配置项是 `HIDAGENT_PORT`。仓库给出的示例是：

  ```text
  HIDAGENT_PORT=/dev/cu.wchusbserial110
  ```

  Windows 文档示例：

  ```powershell
  $env:HIDAGENT_PORT = "COM5"
  ```

  **这些只是仓库里的示例，以你电脑上实际看到的口为准。**正常启动可读取仓库根目录 `.env` 或 `server/.env`；已有环境变量优先，根目录文件先读取。

  **通过标准：**网页能打开，`device` 灯变绿，状态中的端口与实际转接器一致。  
  **失败记录：**启动窗口完整输出、网页错误提示，以及 `http://127.0.0.1:8890/api/status` 返回内容。  
  **出处：**`scripts/run-local.sh:3–4`；`scripts/run-server.ps1:5–7`；`scripts/run_server.py:13–23`；`server/.env.example:7–8`；`server/hidagent/__main__.py:9–17、28–29`；`docs/setup-windows.md:26`。

### 1.3 选择屏幕镜像

- [ ] **在 iPhone 控制中心打开“屏幕镜像”，选择 `ESP32 Agent`。**不要把板子 Wi-Fi 热点名或蓝牙名称当成镜像接收器名称。
  
  **通过标准：**电脑网页显示手机当前画面，`screen` 灯变绿。用手在手机上切换一个无敏感信息的页面，网页随之改变；不是只显示一张旧图。  
  **失败记录：**屏幕镜像列表截图、网页截图、`/api/status` 内容、启动终端输出。若需板子状态，先保存现场画面，再关闭服务、打开串口监视器读取 `0 status`，记录 `ap`、`fps`、`vage`、`fz` 等字段。  
  **出处：**`hardware/hid_bridge/hid_bridge.ino:53、276–288`；`server/hidagent/app.py:275–297`；`docs/hardware-test.md:15、21–22`。

- [ ] **确认真实视频链路是 AirPlay → 板子 → TCP 7201 → 电脑。**
  
  服务会从串口 `0 netinfo` 读取 IP 和 `vtok`，再连接板子的 7201；`vtok` 是访问令牌，每次板子重启会变化。正常情况下不必填写 `BOARD_HOST`、`BOARD_VTOK`。这两个变量只覆盖视频地址和令牌，不能取代 HID 串口。

  **通过标准：**动态页面更新正确、静止页面停在最新画面，无持续花屏；Windows 联网提示按仓库说明允许专用网络访问。  
  **失败记录：**服务窗口中 `screen:` 相关输出和 `/api/status`。若保存 `netinfo`，必须遮掉 `pass` 和 `vtok` 后再分享。  
  **出处：**`server/hidagent/device.py:294–306`；`server/hidagent/screen.py:60–93、158–168`；`server/.env.example:10–13`；`scripts/run_server.py:19–21`。

### 1.4 镜像失败的处理边界

- [ ] **若列表找不到接收器或不出图，先检查同网、隔离和 mDNS，再重新开启镜像。**
  
  **通过标准：**恢复后可持续更新；记录此次恢复是否必须手动操作手机。只刷新浏览器不算完成恢复。  
  **Windows：**不要设置 `SCREEN_BACKEND=uxplay`，代码会拒绝启动该后端。  
  **Mac：**仓库给出的退路是安装 UxPlay 和 GStreamer 后设置 `SCREEN_BACKEND=uxplay`，但这套安装命令、所需版本和现场结果均**待确认**。它只替换画面来源。  
  **失败记录：**镜像列表、网页提示、服务输出；使用 Mac 退路时另外记录后端和实际接收器名称。不要把“升级到 iOS 18”当成已保证成功的方案。  
  **出处：**`docs/setup-macos.md:25–27`；`docs/setup-windows.md:28`；`server/hidagent/app.py:34–42`；`server/hidagent/screen_uxplay.py:31–49`。

## 2．基础控制：校准、点击、滑动、回主屏、输入

### 2.1 校准与 MCP 连接

- [ ] **先校准指针。**确认 `device`、`screen` 两灯绿，再点击网页“校准”。自动校准成功可直接继续；若要求手动定位，就在网页手机画面上点中辅助触控圆点。
  
  **通过标准：**`calibrated` 变绿，三灯全绿；实际指针能被定位。  
  **失败记录：**校准前后网页截图、终端中的 `calibration:` 输出、`/api/status`，以及已生成的 `server/.local-data/calibration.json`。  
  **出处：**`server/hidagent/static/app.js:222–227`；`server/hidagent/phone.py:90–124、184–193`；`scripts/run_server.py:18`；`server/hidagent/app.py:48`。

- [ ] **生成并接入 MCP 配置。**

  | macOS | Windows |
  |---|---|
  | `./scripts/mcp-config.sh` | `powershell -ExecutionPolicy Bypass -File scripts\mcp-config.ps1` |

  将脚本打印的配置按所用 Agent 的要求加入。它会输出 Cursor／Claude Code 配置片段和 Codex 命令；具体客户端版本的配置入口与日志位置**待确认**。

  **通过标准：**Agent 能调用 `iphone_status()` 和 `iphone_observe()`，并实际看到返回的图片。只拿到文本状态不算看图成功。  
  **失败记录：**配置脚本输出、MCP 连接错误、工具返回内容及图片。默认使用宿主 Agent 的视觉能力，不要求额外配置服务端模型 key。  
  **出处：**`scripts/mcp_config.py:14–38`；`server/hidagent/mcp.py:17–36、50–65`。

### 2.2 控制验收

以下动作必须用当前观察返回的真实 `screen_id`；它只有 30 秒有效、只能用一次。动作返回的新图和新 `screen_id` 可用于下一步。不要填写自造的编号。出处：`server/hidagent/control.py:35–54`。

- [ ] **把指针移到中央并停住。**先 `iphone_observe()`，再用 `iphone_point`，参数 `x=0.5`、`y=0.5`，附当前 `screen_id`。
  
  **通过标准：**截图和手机上的指针都在中央附近，没有点击任何按钮。  
  **失败记录：**动作返回图片、`pointer`、`error`、`timing_ms`；重新观察或校准后再决定，不盲点。  
  **出处：**`server/hidagent/mcp.py:68–72`；`server/hidagent/phone.py:202–220`；`server/hidagent/control.py:149–156`。

- [ ] **点击一个无风险目标。**从截图中选择一个可见目标，用 `iphone_tap` 点击。
  
  **通过标准：**目标确实被打开或选中；不是只收到 `command_acknowledged=true`。指针无法验证时应报错，不应继续盲点。  
  **失败记录：**点击前后图片、所用坐标、工具完整返回。默认坐标是 `0..1`，不是 `0..1000`。  
  **出处：**`server/hidagent/mcp.py:74–79`；`server/hidagent/phone.py:202–225`；`server/hidagent/control.py:64–70、152–156`。

- [ ] **滑动列表并回主屏。**使用 `iphone_swipe` 在可见列表中滑动，再用 `iphone_home` 尝试回主屏。
  
  **通过标准：**列表方向正确，松开后没有持续拖拽；回主屏后确实看到主屏，而不是多任务界面。Home 键机型若手势失败，按仓库建议改用辅助触控，并单独记录。  
  **失败记录：**动作前后截图、坐标、工具返回和手机型号。注意固件原始命令 `home` 只是把鼠标归到左上角，不等于回 iPhone 主屏。  
  **出处：**`server/hidagent/mcp.py:82–86、120–123`；`server/hidagent/phone.py:295–312、329–330`；`hardware/hid_bridge/hid_bridge.ino:114–118`。

- [ ] **输入一行英文和数字。**在不会对外发送的文本框中，确认焦点和英文输入状态，用 `iphone_type` 输入 `test 123`。
  
  **通过标准：**输入内容、空格都正确，没有自动发送；需要时用 `iphone_key` 的 `backspace` 修改。  
  **失败记录：**输入前后截图、输入法状态、工具返回。网页普通输入框和 `iphone_type` 都不是中文粘贴入口。  
  **出处：**`server/hidagent/mcp.py:95–118`；`server/hidagent/control.py:84–87`；`server/hidagent/app.py:243–245、458–461`。

- [ ] **测试紧急停止。**在无风险控制过程中点击网页紧急停止，或调用 `stop_task()`。
  
  **通过标准：**后续动作停止，鼠标释放，不继续输入；再次操作前重新观察。紧急停止不等于永久熔断，也不能撤回已经发到手机的按键。  
  **失败记录：**停止时刻、动作返回、停止后截图及终端输出。  
  **出处：**`server/hidagent/mcp.py:232–236`；`server/hidagent/app.py:529–549`；`server/hidagent/device.py:214–224`。

## 3．中文快速输入：当前是拼音输入，不是剪贴板

**中文是按拼音打出来的**：先把汉字转成拼音，用 USB 键盘打进 iPhone 的拼音输入法，再由 Agent 看截图选候选词。不走剪贴板，也不能一次粘贴进去。README 里“短回复大约 30–60 秒”只是经验估计，不是本仓库的真机测量。

具体链路：

**汉字 → `pypinyin.lazy_pinyin` 转为连续拼音 → 串口 `type` → 固件 USB 键盘 `keyboard.print` → iPhone 拼音候选 → Agent 看图选词。**

`phone.py` 只负责把文本分块发给固件；转换发生在 `control.py`。`layouts.py` 只读取命名坐标，不负责拼音、选词或剪贴板。

出处：`server/pyproject.toml:16`；`server/hidagent/control.py:13、88–96、124–125`；`server/hidagent/phone.py:318–324`；`hardware/hid_bridge/hid_bridge.ino:125`；`server/hidagent/layouts.py:33–45`。

### 3.1 配置并确认手机输入法

- [ ] **在 iPhone 上启用并切换到中文拼音输入状态。**先点中可编辑输入框，再观察实际候选界面。仅给工具传 `input_mode="pinyin"` 不会自动替手机切换输入法。
  
  **通过标准：**输入拼音后出现可选汉字，而不是只留下英文字母。  
  **失败记录：**iOS 版本、输入法／硬件键盘设置截图、输入框和候选截图。  
  **待确认：**不同 iOS 下添加中文拼音、切换外接键盘语言的具体菜单路径，以及全拼／双拼／键盘布局的兼容性。仓库没有给出完整设置教程，不能把任意中文键盘都视为兼容。  
  **出处：**`server/hidagent/mcp.py:26–27、101–108`；`server/hidagent/control.py:91–95`；`skills/wechat-group-reply/SKILL.md:33–35`。

### 3.2 输入和选词

- [ ] **输入“谢谢分享”，先不发送。**调用 `iphone_compose_chinese`，参数为 `text="谢谢分享"`、`input_mode="pinyin"`，附当前 `screen_id`。
  
  **通过标准：**返回的 `expected_text` 为“谢谢分享”，`typed_ascii` 是实际输入的拼音，`requires_candidate_selection=true`、`submitted=false`；手机出现候选，尚未发送。  
  **失败记录：**完整工具返回、候选截图、输入框截图。若只有拼音，先检查输入法，不要把它算成中文输入成功。  
  **出处：**`server/hidagent/control.py:88–96`；`docs/hardware-test.md:23`。

- [ ] **选对汉字并核对最终文本。**首选候选正确时，用 `iphone_key`，参数 `combo="space"`；否则根据截图用 `iphone_tap` 点正确候选。使用新的 `screen_id`。
  
  **通过标准：**输入框最后显示完全正确的“谢谢分享”，没有错字、残留拼音或意外发送。  
  **失败记录：**选词前后截图、所选候选、工具返回。多音字、人名或词组识别错误时需要纠正，不能因为生成了拼音就声称汉字正确。  
  **出处：**`server/hidagent/mcp.py:103–108`；`docs/troubleshooting.md:31–33`。

- [ ] **分别验收标点、长度和不支持的内容。**中文组合工具一次只接收 1–60 个 `U+4E00..U+9FFF` 范围内的汉字；英文、数字和可打印 ASCII 标点分开用 `iphone_type` 输入。
  
  **通过标准：**合法短句可完成；把标点、emoji 或混合内容塞进中文组合工具时明确报错，没有假装成功。  
  **失败记录：**原始测试文本、错误返回、最终手机截图。  
  **明确缺口：**没有中文剪贴板粘贴、任意 Unicode 直输或 emoji 输入方案；不能直接用 `iphone_type` 输入中文标点。ASCII 标点在具体中文输入法下最终显示为何种符号，需要实测。超出该汉字范围的字也不在当前组合工具支持范围内。  
  **出处：**`server/hidagent/control.py:84–96`；`server/hidagent/mcp.py:95–108`；`skills/wechat-group-reply/SKILL.md:27、34`。

### 3.3 测量“快不快”

- [ ] **用同一个短句重复测量完整输入过程。**记录从开始观察，到拼音输入、选词、最终汉字核对完成的时间；再单独记录一次真实发送的总耗时。
  
  **通过标准：**文字准确，耗时经 seven 实际体验认可。验收所需的秒数上限**待确认**，不能替 seven 编一个承诺。  
  **失败记录：**每次总耗时、各工具返回的 `timing_ms`、选词次数、模型名称、截图；模型思考时间要另计，不能只看 HID 耗时。  
  **速度依据：**仓库只有“短回复约 30–60 秒”的经验估计，明确不是本仓库真机测量；没有“中文每秒多少字”或“秒发中文”的证据。`Device._timeout()` 的字符超时公式也不是输入速度。  
  **出处：**`README.md:48–52`；`server/hidagent/control.py:149–156`；`server/hidagent/device.py:170–181`。

## 4．微信群自动回复

> 本项目提供看图、操作手机、读取 skill 和发送保护工具。**启动网页并不会自动开始监听微信群。**需要一个能看图片的宿主 Agent 持续执行任务；仓库没有独立的微信消息监听、无人值守排班或完整未读消息队列。
>
> 群回复没有逐条人工审批；正常流程由 Agent 自动完成看图、判断、输入、核对和发送。出处：`server/hidagent/mcp.py:17–36`；`skills/wechat-group-reply/SKILL.md:29–41`；`docs/wechat.md:5–7`。

### 4.1 先核实规则与群页面

- [ ] **先在专用测试群和专用运营号上验收。**调用 `skill_show(name="wechat-group-reply")`，核对返回规则。默认只回复 @ 本号或明确属于本号职责的问题；看不懂、不确定的内容不回。
  
  **通过标准：**Agent 能复述并执行实际 skill；不发送密码、付款、隐私或没有画面依据的信息。  
  **失败记录：**skill 工具返回、测试任务、脱敏后的问题与回复。群聊截图会进入所使用的视觉模型；记录实际使用的宿主与模型。  
  **出处：**`server/hidagent/mcp.py:166–170`；`skills/wechat-group-reply/SKILL.md:12–27`；`docs/wechat.md:20`。

- [ ] **看图读取消息并决定是否回复。**先 `iphone_observe()`，确认群名、发言人、问题和上下文；再调用 `skill_check`，传 `skill="wechat-group-reply"`，`observed_text` 填实际看见的系统风险提示，无提示可为空。
  
  **通过标准：**回复对象与问题正确；不是凭猜测补上下文。`stop=true` 就停止；`stop=false` 只是没有触发熔断，**不是发送许可**。  
  **失败记录：**脱敏的观察图片、读到的消息、生成的回复、`skill_check` 完整返回。不要把整段群聊随意当成系统风险提示传入。  
  **出处：**`server/hidagent/mcp.py:173–180`；`server/hidagent/guard.py:180–202`；`skills/wechat-group-reply/SKILL.md:18–26、31–32`。

- [ ] **检查输入框与发送按钮的位置。**调用 `iphone_layouts()` 查看坐标，再核对当前截图。当前 `wechat-group-reply` 的 `input` 为 `(0.42, 0.93)`，`send` 为 `(0.91, 0.93)`，只是 480×1040 竖屏的近似位置。
  
  **通过标准：**命名位置确实对应当前界面；输入框获得焦点。位置不符时停止发送验收，先处理布局适配。  
  **失败记录：**微信版本、屏幕方向、截图尺寸、布局工具返回和实际按钮位置。不要为了绕过错误坐标，直接用普通点击发送而忽略限流保护。  
  **出处：**`skills/wechat-group-reply/layout.json:2–7`；`server/hidagent/layouts.py:33–45`；`server/hidagent/app.py:352–364`。

### 4.2 完成一次自动回复

- [ ] **按第 3 节输入、选词并核对最终中文。**候选选择和发送不要与中文组合放进同一个 `iphone_batch`。
  
  **通过标准：**群名正确、最终文字正确、内容符合 skill，尚未发送。  
  **失败记录：**输入框最终截图、工具返回；错误时编辑或停止，不直接发送。  
  **出处：**`server/hidagent/mcp.py:137–143`；`skills/wechat-group-reply/SKILL.md:33–35`。

- [ ] **发送前先占名额，再点命名发送按钮。**调用 `skill_prepare_send(skill="wechat-group-reply")`。只有 `allowed=true` 才继续；30 秒内使用 `iphone_layout_tap`，参数 `layout="wechat-group-reply"`、`point="send"`，附当前有效 `screen_id`。
  
  **通过标准：**允许时只点击一次；拒绝时不点击。实际发送动作消费预留名额，时间更新为真正发出 HID 时刻。预留与发送合计只占一个名额。  
  **失败记录：**prepare 返回、发送工具返回、前后截图与时间。若发送结果不确定，不自动重按。  
  **注意：**skill 文件自身的第 6 步直接写了“点 send”，没有明确列出 prepare；验收应补上 `docs/wechat.md` 和 MCP 工具说明要求的 prepare 步骤。  
  **出处：**`server/hidagent/mcp.py:183–191`；`server/hidagent/guard.py:245–286`；`docs/wechat.md:14`；`skills/wechat-group-reply/SKILL.md:36`。

- [ ] **看到完整新气泡后再确认。**再次观察，确实看见刚发送的完整消息气泡后，调用 `skill_confirm_send`，参数 `bubble_visible=true`、`skill="wechat-group-reply"`，并传入实际系统风险提示。
  
  **通过标准：**手机画面上出现正确的新气泡，确认返回 `counted=true`；没有重复发送。输入框清空或串口 `ok` 均不算发送成功。  
  **失败记录：**发送前后气泡截图、confirm 返回。没有看见新气泡就传 `bubble_visible=false`，记录 `counted=false`，不自动重发。  
  **限制：**服务器不能验证气泡真假，`bubble_visible` 只是 Agent 的声明；工具返回不能替代截图证据。  
  **出处：**`server/hidagent/mcp.py:194–204`；`server/hidagent/guard.py:288–304`；`docs/wechat.md:15`。

### 4.3 验收发送限制

- [ ] **验证默认至少间隔 8 秒。**完成一次已消费名额的发送后，在实际发送时刻起不足 8 秒时再请求 `skill_prepare_send(skill="wechat-group-reply")`；不要为了测试反复发群消息。
  
  **通过标准：**返回 `allowed=false`，原因说明不足 8 秒，手机没有第二条消息。若两次请求实际已相隔超过 8 秒，本次不能证明短间隔限制有效。  
  **失败记录：**两次时间、prepare 返回、`guard.json` 中对应的 `sends`。  
  **出处：**`skills/wechat-group-reply/SKILL.md:2`；`server/hidagent/guard.py:219–233、279–285`。

- [ ] **验证滚动一小时最多 20 个名额。**可在单独的测试阶段只调用 prepare、不按发送：每次等待超过 30 秒让预留过期，再申请下一个；既有名额也计入总数。这样可以测试上限而不用向群里发 20 条消息。
  
  **通过标准：**最近 3600 秒已有 20 个名额时，下一次 prepare 被拒绝；没有发出消息。记录本测试会占用额度，需要等窗口自然过去，重置熔断不会清掉额度。  
  **失败记录：**每次 prepare 返回及时间、`guard.json`。这项验证的是额度机制，不代表 20 条微信消息已发送成功。  
  **出处：**`server/hidagent/guard.py:219–242、245–261`；`docs/wechat.md:14`。

- [ ] **验证 23:00–07:00 静默。**在服务器电脑本地时间进入该时段时请求 prepare。
  
  **通过标准：**`allowed=false`，原因为静默时段，没有点击发送；23:00 起包含在静默内，07:00 起不再因该规则拒绝。  
  **失败记录：**电脑日期、时间、时区和返回结果。尚未到该时段就标为**待确认**，不能用别的时区结果代替。  
  **出处：**`skills/wechat-group-reply/SKILL.md:4`；`server/hidagent/guard.py:84–92、211–218`。

- [ ] **验证名额过期与重启行为。**prepare 成功后不发送，超过 30 秒再检查；另一次在 prepare 后重启服务，检查原先的名额记录。
  
  **通过标准：**未发送的预留不会无限期重复使用；过期或重启丢失的预留仍保守计入小时额度。不能把重新启动当成“额度清零”。  
  **失败记录：**prepare 返回、重启前后 `guard.json`、后续调用返回。  
  **出处：**`server/hidagent/guard.py:107、115–148、155–160、237–260`；`docs/wechat.md:14`。

### 4.4 验收停词与持久熔断

- [ ] **模拟风险提示，检查立即熔断。**在停止真实发送的测试阶段，向 `skill_check` 分别传入测试字符串“操作频繁”“风控”“验证码”；另测 skill 的“账号异常”“功能受限”，后两项必须携带 `skill="wechat-group-reply"`。每次测试后按下一项的人工检查流程重置，再测下一词。
  
  **通过标准：**返回 `stop=true`；后续点击、滑动、输入被拒绝，状态和截图仍可读取。不要去真实触发微信风控来完成测试。  
  **失败记录：**测试词、工具返回、`/api/status` 的 `guard`、`guard.json`，以及被拒绝动作的返回。此项验证上报后的保护，不证明视觉模型一定能识别风险提示。  
  **出处：**`server/hidagent/guard.py:21、176–199`；`skills/wechat-group-reply/SKILL.md:5`；`server/hidagent/app.py:82–132`。

- [ ] **熔断后重启服务，再由人从网页重置。**保留原数据目录，关闭并用第 1 节原命令重新启动。
  
  **通过标准：**重启后仍显示 `guard.tripped=true`，控制继续被拒绝；人工查看手机后，使用本机网页“重置熔断”并确认，才解除。解除后小时额度仍保留。Agent 没有解除熔断的 MCP 工具。  
  **失败记录：**重启前后状态、`guard.json`、网页重置结果、被拒绝动作返回。不要删除状态文件来冒充通过。  
  **注意：**底层 `release`／`home` 为紧急清理保留豁免；这里的 `home` 是指针归位，不是任意操作解禁。  
  **出处：**`server/hidagent/guard.py:115–174`；`server/hidagent/app.py:103–108、417–433`；`server/hidagent/static/app.js:241–243`；`docs/wechat.md:12、16`。

## 5．稳定性：长时间运行、断线、锁屏与换电脑

下面的“连续运行至少 1 小时、做几轮断线”是本清单建议的测试安排，**不是仓库已有的稳定性承诺**。

- [ ] **连续运行至少 1 小时，交替测试静止画面和页面变化。**保持同一个控制服务，定期检查最新画面、指针和输入；不要靠不断发送微信群消息制造负载。
  
  **通过标准：**没有持续花屏、错误旧图、反复重启或失控；发生冻结时能识别并记录，恢复后先观察再操作。不能只看三灯是否绿色。  
  **失败记录：**开始和结束时间、每次异常时间、网页截图、`/api/status`、终端输出；需要时收集下文的板子诊断输出。真机持续稳定时间**待确认**。  
  **出处：**`server/hidagent/screen.py:99–116`；`hardware/hid_bridge/video_link.cpp:11–20、398–405`。

- [ ] **断开并恢复电脑串口通道。**在无发送任务时操作；若该线还承担供电，必须先确认实际供电方案，不能把掉电重启与单纯串口断线混为一谈。
  
  **通过标准：**服务检测断开，重新连接后恢复 `device`，能再次观察、定位；不重放此前发送命令。必要时重新校准。  
  **失败记录：**恢复耗时、服务窗口输出、前后状态、端口是否变化、板子是否重启。  
  **出处：**`server/hidagent/device.py:345–364`；`server/hidagent/phone.py:270–276`；`server/hidagent/mcp.py:25`。

- [ ] **断开并恢复 Wi-Fi／7201 视频连接。**恢复同一网络后观察是否自动回图；必要时在手机重新开启镜像。
  
  **通过标准：**恢复后显示当前画面；记录“自动恢复”还是“手动重开镜像后恢复”。若只能手动恢复，不得勾成无人值守自动恢复。  
  **失败记录：**断线时长、终端 `screen:` 输出、`/api/status`、冻结截图、恢复办法；板子重启时检查是否重新取得令牌。  
  **限制：**GOP 冻结且尾部视频已被截断时，重连或 `resync` 不保证恢复，见风险表。  
  **出处：**`server/hidagent/screen.py:72–93、166–167`；`server/hidagent/video7201.py:198–200、263–269`；`hardware/hid_bridge/netcfg.cpp:161–171`。

- [ ] **锁屏后解锁。**先停止自动回复，锁屏，再手动解锁并重新选择“屏幕镜像 → ESP32 Agent”。
  
  **通过标准：**锁屏时没有继续误发；解锁、重新镜像后画面和 HID 恢复。出现 `asleep` 应提示解锁，不能当成已经执行。密码、付款等受保护页面不作为普通镜像通过要求。  
  **失败记录：**锁屏前后截图、`asleep` 返回、恢复耗时和所需操作。不能宣称支持锁屏状态下全天自动回复。  
  **出处：**`docs/hardware-test.md:26`；`docs/troubleshooting.md:39–41`；`hardware/hid_bridge/hid_bridge.ino:94–113`。

- [ ] **在另一种电脑系统上重复核心流程。**分别在原生 Windows 和 Mac 上完成安装、串口、投屏、校准、中文输入及一条测试回复；两台电脑轮流测试，不同时抢同一块板子。
  
  **通过标准：**两边均有独立的实测记录；同一已适配板子换电脑不要求重烧，同网不要求重新配网。只在 Mac 成功不能代表 Windows 通过。  
  **失败记录：**每台电脑的系统、Python、端口、转接器驱动、工具返回和截图。  
  **出处：**`docs/hardware-test.md:24`；`scripts/runtime.py:26–29`；`.github/workflows/ci.yml:39–64、68–109`；`hardware/hid_bridge/wifi_video_tcp.cpp:126–129`。

## 失败时保存什么：真实路径与取证入口

以下是本清单前面各项引用的证据位置。保存到哪里由 seven 自行选择；仓库没有统一的“验收截图目录”。

| 证据 | 真实位置或入口 | 注意事项与出处 |
|---|---|---|
| 服务日志 | 启动 `run-local.sh`／`run-server.ps1` 的终端窗口 | 默认输出到终端，没有统一日志文件。`server/hidagent/__main__.py:31–42` |
| 状态 | `http://127.0.0.1:8890/api/status` | 保存 JSON，含连接、三灯状态、提示及熔断。`server/hidagent/app.py:275–290` |
| 当前截图 | `http://127.0.0.1:8890/api/frame.jpg` | 无有效画面时返回 404；也可保存 MCP 返回图片。`server/hidagent/app.py:293–297` |
| 校准记录 | 使用官方启动脚本、未另设数据目录时：`server/.local-data/calibration.json` | `scripts/run_server.py:18`；`server/hidagent/app.py:48` |
| 熔断与额度 | 同上：`server/.local-data/guard.json` | 保存 `tripped`、`reason`、`sends`；它是状态文件，不是完整操作日志。`server/hidagent/app.py:52`；`guard.py:155–160` |
| 其他启动方式的数据目录 | `HIDAGENT_DATA` 指定位置；没有设置时，服务默认用用户主目录下 `.hidagent` | 不要混淆不同目录，误以为熔断丢失。`server/hidagent/app.py:29–30` |
| 服务端模型调试 trace | `HIDAGENT_DEBUG=1` 且实际运行服务端 Agent 任务时，数据目录下 `traces/日期时间/steps.jsonl` 和编号 `.jpg` | **默认宿主 MCP 看图控制不保证生成这些文件。**当前 trace 路径可看 `/api/status` 的 `trace`。`app.py:50–51、282`；`debug.py:16–32` |
| 宿主 Agent／MCP 日志 | 所使用客户端的日志与工具调用记录 | 各客户端具体保存路径**待确认**；不要声称都保存在服务端 traces 中 |
| 板子状态及诊断 | 串口 `0 status`、`0 vdbg`、`0 crash` | 先保存网页现场，再关闭占用串口的服务进行读取。`hid_bridge.ino:239–241、469–473` |
| 板子 RAM 日志 | 串口 `0 logs 0 400`，按回复中的 next 偏移继续读取，直到 next=end | 日志在 RAM 环形缓冲，可能覆盖，重启可能丢失；不是 SD 卡文件。`hardware/hid_bridge/logbuf.h:4–11`；`logbuf.cpp:5–8、33–42` |

分享证据前遮掉 Wi-Fi 密码、`vtok`、模型 key 和真实聊天隐私。`0 netinfo` 包含密码；不要直接截图公开。出处：`hardware/hid_bridge/netcfg.cpp:197–200`；`docs/troubleshooting.md:3`。

## 代码已知风险与尚未真机验证的项目

| 风险或未验证项 | 具体含义 | 验收结论／处理 | 出处 |
|---|---|---|---|
| **AirPlay 无法主动要求新的 IDR** | 当前接收器没有主动请求关键帧的恢复接口。GOP 满后冻结；尾部仍完整时可继续或重放，但 tail 被截断后，新解码器缺失依赖帧 | 只能等 iPhone 自然发新 IDR，或重新开启镜像。`resync` 是重发缓存的请求，不是强制 iPhone 生成新 IDR；不能承诺自动恢复 | `hardware/hid_bridge/src/airplay/airplay.h:43–48`；`video_link.cpp:11–20、172–184、398–405`；`wifi_video_tcp.cpp:106–107` |
| **P2：发送预留不携带 skill** | `_reservation` 只保存 `(token, time)`；真正发 Enter／命名 send 时，`app.py:127` 调用 `consume_reservation("")`，实际重新检查的是默认规则，不是预留时的 skill | 当前微信 skill 恰好与默认 8 秒／20 次／23–07 相同，通常掩盖问题。自定义更严格时段时，可能在预留后跨入其静默期却仍按默认规则放行；跳过 prepare 时，自定义间隔和小时上限也不会在发送 gate 中应用。相反，自定义放宽默认限制也可能被默认值拦住。修复前不能保证所有自定义规则在真正发送时生效 | `server/hidagent/app.py:119–131、364`；`guard.py:107、245–285` |
| **普通坐标点击无法识别“发送”** | 自动计费 gate 识别 Enter 和命名 `send` 的 click，普通 `iphone_tap` 点到发送键没有这个语义 | 日常使用规定的命名发送流程；不能宣称服务端能拦住所有方式的限流绕过。熔断后的 HID 拦截是另一项机制 | `server/hidagent/app.py:119–127、352–364` |
| **风险词和气泡依赖 Agent 上报** | 服务端不自动理解截图；漏报风险词或虚报 `bubble_visible` 都可能产生错误判断 | 必须保存图像证据，不能只验返回布尔值 | `server/hidagent/mcp.py:173–204`；`guard.py:180–202、288–304` |
| **“skill 只能加严”与代码有差异** | 代码下限是 3 秒、上限 30 次；允许 `quiet_hours: off`，并非始终固定默认 8 秒／20 次且必须夜间静默 | 以实际 skill 和 `limits_for()` 返回为准；不要把文档表述当成绝对限制 | `server/hidagent/guard.py:57–80`；`server/tests/test_guard.py:34–45` |
| **中文输入准确率与速度** | 拼音逐字符经键盘输入，选词依赖截图；无中文剪贴板、Unicode 直输和 emoji 方案 | 输入法配置、候选准确率、多音字、标点、长句与速度全部待真机验证 | `control.py:84–96`；`phone.py:318–324`；`README.md:52` |
| **iOS 版本兼容性** | 目标版本不等于本仓库已实测；16/17 与文档列出的 iOS 27 均有未验证说明 | seven 的具体型号和版本必须单独过投屏第一关 | `README.md:46`；`docs/hardware-test.md:5、15` |
| **USB-C 直连、Lightning 与供电** | 手机识别 HID、线材、配件权限、转换器耗电、焊盘倒灌，本仓库都没有真机证明 | 每种连接方式单独记录；USB-OTG / IN-OUT 焊盘必须断开 | `docs/hardware-test.md:9–20`；`README.md:18` |
| **原生 Windows／Mac 真机运行** | 有原生脚本和跨平台 CI 配置，但配置文件本身不证明 seven 的电脑已通过，更不证明真实板子可用 | 两种系统分别完成完整流程；Windows 无 UxPlay 退路 | `.github/workflows/ci.yml:39–109`；`screen_uxplay.py:31–33`；`README.md:7` |
| **静止旧画面可能暂时仍为绿色** | `screen.live` 允许最近 90 秒内的帧；视频心跳继续时，即使没有新视频帧，也不按完全断流提示 | 人工改变页面核对新鲜度；绿灯不是最新画面的充分证据 | `screen.py:109–116`；`video7201.py:104–106、236–257` |
| **长时间运行、重连、锁屏** | 有维护、保活、重连与 asleep 处理，但锁屏后文档要求重新镜像 | 长测、断线恢复、令牌更新、急停、重启保留熔断均待真机验证；不能承诺全天无人值守 | `device.py:345–385`；`docs/hardware-test.md:26` |
| **微信群业务闭环** | 没有独立微信消息监听服务；布局是近似坐标，内容判断由宿主模型执行 | 实际读消息、生成合适回复、正确选词、只发一次及不该回时不回，均待实测 | `skills/wechat-group-reply/SKILL.md:18–41`；`server/hidagent/mcp.py:17–36` |
