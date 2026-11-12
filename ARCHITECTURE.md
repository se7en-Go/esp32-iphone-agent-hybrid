# 架构

本文先于代码写定。seven 已批准的偏差（见下）覆盖 `muse-spec` 里与之冲突的句子。没有真机，文中凡是“能在 iPhone 上怎样”都只是协议推断，不是实测结论。

## 一句话

电脑上的任意 MCP Agent，通过本仓库的 Python 服务（`127.0.0.1:8890`）控制一块 ESP32-S3 N16R8。板子的 **COM（USB 转串口，可能是 CH343 / FTDI / CP210x）** 接电脑，只传命令、vtok 和配网；板子的 **USB（GPIO19/20）** 接 iPhone，做 USB HID 鼠标键盘。画面不经过 UxPlay：iPhone 系统“屏幕镜像”到板载 AirPlay 接收器，板子把 H.264 从 **TCP 7201** 推给电脑，电脑用 **PyAV** 解成 JPEG。

```
Codex / Pi / Claude Code / Cursor / 任意 MCP 客户端
        │  stdio MCP（工具名与 Kz-Aven 保持一致，并多了 batch / layout / skill）
        ▼
Python 服务  127.0.0.1:8890   （macOS 与 Windows 10/11 原生，同一套代码）
        │  命令、vtok、配网：COM 串口 115200，协议 `<id> <cmd>`
        │  画面：Wi-Fi TCP 7201，hello <vtok> / ka，PyAV 解码
        ▼
ESP32-S3 N16R8 固件（Handsy hid_bridge，commit 5a4fddf，协议未改）
        │  原生 USB HID
        ▼
iPhone（Lightning 或 USB-C，目标 iOS 16.7+）
        │  辅助触控指针 + 控制中心里的「屏幕镜像 → ESP32 Agent」
        ▼
微信或任意 App（只模拟真人点按，不碰 App 协议）
```

默认网络是 **STA**：板子、iPhone、电脑连同一个 2.4GHz 路由器。电脑用自己原来的网上网，云端模型不会因为连板子热点而断开。

## 已批准、并覆盖原 spec 的偏差

1. **默认 STA，不是 AP。** 家里或办公室的 2.4GHz 路由器上，板子、手机、电脑在同一局域网。电脑经 COM 口发 `0 net {"mode":"sta","ssid","pass"}`，配置写入板子 NVS。换地方就再跑一次配网脚本，不重烧、不提交 `secrets.h`。AP 只留给“电脑另有网线”的人：板子热点不下发网关（`netcfg.cpp` 的 `noDefaultRoute`），只连热点的电脑会失去互联网和云端模型。
2. **拓扑固定成双 Type-C。** 标着 USB 的口接手机（USB HID）。标着 COM 的口接电脑（命令、`0 netinfo` 读 vtok、配网）。视频只走 Wi-Fi 7201。电脑这一侧不要求蓝牙。7202 + 配套 App 的 BLE 配对留作实验，本阶段不交付。手机这一侧可以改用蓝牙 HID，见后面的「蓝牙 HID」一节；那一步还没有真机验证。
3. **iOS 目标 16.7+。** 不装 Handsy iOS App，不用 TestFlight，不越狱，不开开发者模式。板载接收器代码里没有 iOS 版本判断，但 Handsy 作者的记录是 iOS 18/26/27；Kz-Aven 在 iOS 16.7.16 上测通的是电脑上的 UxPlay，不是这块板子。**iOS 16/17 镜像到板载接收器是第一道硬件关，本仓库没有测过。** 失败时的退路：把手机升到 iOS 18，或仅在 Mac 上把 `SCREEN_BACKEND=uxplay` 打开（要自己装 UxPlay 和 GStreamer，Windows 不走这条）。
4. **许可证拆开。** `hardware/` 里的固件链接了 GPL-3.0 的 AirPlay 代码，固件源码与固件二进制按 GPL-3.0 分发，保留 `src/airplay/LICENSE-GPL.md` 和各文件头。`server/`、`scripts/`、`skills/`、`docs/` 是独立程序，只通过串口和 TCP 与固件说话，按 MIT 分发，保留 Kz-Aven 与 Henry Yuan 的版权声明。根目录 `NOTICE` 说明边界。
5. **小白不装 arduino-cli。** GitHub Actions 用报告里已编译通过的 FQBN 打出 N16R8 的 `merged.bin`。日常刷机是 `esptool`（pip）一键脚本，按 VID 自动找 COM 口：沁恒 `0x1A86`、FTDI `0x0403`、CP210x `0x10C4`；不会去认乐鑫原生 USB（`0x303A`）。想改固件的人仍可从源码编译。
6. **微信群自动回复是第二阶段，不挡住核心。** 按 seven 的选择，发送是全自动的：代码里没有草稿、没有人工确认弹窗。回复内容只受 `skills/wechat-group-reply/SKILL.md` 约束。系统级熔断另算，而且不能被 skill 关掉：见到「操作频繁 / 风控 / 验证码」立刻停、不重试；有频率上限；只有调用方声明屏幕上出现了完整的新气泡，才把这一次计为发送成功。微信逻辑不进核心服务，skill 可插拔。

## 决策与理由

### 固件以 Handsy 为底

Kz-Aven 的 `hid_bridge.ino` 把 Wi-Fi 名、密码和电脑 IP 写进 `secrets.h`，换网就要重烧，而且它自己不是 AirPlay 接收器，画面依赖电脑上的 UxPlay。Handsy 的固件已经具备：

- 板载 AirPlay（`hardware/hid_bridge/src/airplay/`，自 UxPlay 移植）
- TCP 7201 推 H.264（`wifi_video_tcp.cpp`）
- USB HID 鼠标 / 键盘 / AC Home
- NVS 里的 AP/STA（`netcfg.cpp`），串口 `0 net` 可改，不必重烧
- 串口与 App 同一套 `<id> <command>`（`hid_bridge.ino` 的 `loop`）

本仓库把 Handsy `5a4fddf` 的 `hardware/hid_bridge` 原样引进，**不改协议**。产品默认的 STA 由电脑在第一次配网时写入 NVS；固件里 `mode` 的出厂缺省仍是 `ap`（`netcfg.cpp` 的 `prefs.getString("mode", "ap")`），没有 SSID 时也会退回 AP。这是为了让“还没配过网的板子”能启动。配过一次之后 NVS 记住 STA，重启不用再烧。

N16R8 的编译参数用可行性报告里已经编过的这一条，而不是 spec 草稿里的 `CDCOnBoot=cdc` + `PartitionScheme=default_16MB`：

```
esp32:esp32:esp32s3:USBMode=default,CDCOnBoot=default,FlashSize=16M,PSRAM=opi,PartitionScheme=app3M_fat9M_16MB
```

- `CDCOnBoot=default`：串口留在 UART0，也就是 COM 口。`cdc` 会把串口复合到手机那一侧，电脑就读不到 vtok。
- 通用 `esp32s3` 板型没有 `default_16MB` 这个 PartitionScheme，用了会直接报错。`app3M_fat9M_16MB` 带 ota_0、ota_1 和 coredump，固件里的 OTA 回滚和 `crash` 命令要用。
- 编译时仍强制 `#include` `nimble_cfg.h`，与 Handsy 的 `flash.sh` 一样，否则 NimBLE 缓冲不是作者调过的那套。
- 烧录走 COM 口的 esptool 自动复位，不再用 Handsy 那套“1200 波特触发原生 USB 进 bootloader”。那套是给 XIAO 的原生 USB 口用的。

### 电脑端以 Kz-Aven 为底

保留 `server/hidagent` 的工具面：`iphone_status`、`iphone_observe` / `iphone_screenshot`、`iphone_point` / `iphone_tap`、`iphone_swipe` / `iphone_scroll`、`iphone_type`、`iphone_compose_chinese`、`iphone_key`、`iphone_home` / `iphone_back`、`iphone_calibrate`，以及 `task_status`、`stop_task`、`iphone_model_help`。`delegate_task` 仍要 `IPHONE_MCP_ENABLE_DELEGATE=1` 才出现。

`iphone_compose_chinese` 继续只负责把 1–60 个汉字打成拼音、不选词、不发送。选词和发送是后面的独立工具调用，方便对着截图核对。

改掉的只有传输层：

| 原 Kz-Aven | 本仓库 |
|---|---|
| 板子作为 TCP 客户端连电脑的 7300，回复裸的 `ok` | 电脑打开 COM 口，发送 `<id> <cmd>\n`，读 `<id> ok` 或 `<id> err …` |
| `screen.py` 拉起 UxPlay + GStreamer，`pass_fds` 只在 Unix 上存在 | `video7201.py` 连板子 7201，PyAV 解码，pip wheel，三个操作系统都能装 |

`Phone`、`Controls`、校准和 `screen_id` 单次有效这些语义不动，所以原来的测试大部分可以留着。

### 为什么不让电脑去连板子热点

Handsy 的 AP 是给“手机上的 App 当大脑”设计的。DHCP 故意不给网关和 DNS，手机靠蜂窝上网。电脑若只有一块 Wi-Fi，连上热点就断了家里的网，Claude / Cursor / 视觉模型一起断。所以默认 STA。

AP 仍可用，条件写在文档里：电脑另有网线；Wi-Fi 改去连板子热点（SSID 形如 `ESP32Agent-XXXX`，密码在 NVS 里，配网脚本从串口读出来显示一次）；手机也连这个热点；手机上网靠蜂窝。控制命令依然走 COM 线，不走热点。

STA 的限制也写清楚：路由器要有 2.4GHz；不能开 AP 隔离或访客隔离；要放行 mDNS，否则手机的屏幕镜像列表里看不到 “ESP32 Agent”；企业 802.1X 和门户登录的 Wi-Fi 不行。

### 控制通道为什么是串口

三条通道里串口最适合这块双口板子，也最适合没有蓝牙的台式机：

1. **串口明文（本阶段使用）。** `hid_bridge.ino` 在 `Serial` 上接受与 App 相同的命令。`0 netinfo` 回 JSON，里面有 `vtok`（每次开机随机，`wifi_video_tcp.cpp` 的 `wv_begin`）。`0 status` 回状态。`0 net {json}` 改网络并立刻回 `0 ok`，真正切换在 `net_poll()`。没有加密，安全边界是“线插在你自己的电脑上”。
2. **TCP 7202。** X25519 + AES-GCM，文档在 Handsy `docs/cli-protocol.md`。第一次配对必须走 BLE，而且要在上电 3 分钟内。没有蓝牙就配不了。本阶段不做。
3. **BLE GATT。** 给 Handsy App 用，`net` 特征还要求加密绑定。本阶段不依赖 App，也不走 BLE。

串口速率 115200，只传命令。视频不能走这根线。

### 蓝牙 HID

电脑仍然只走 COM 串口。板子收到 `home`、`move`、`click`、`type`、`key` 之后，再决定从 USB 还是 BLE HID 发给手机。两条路都先经过服务端同一把熔断锁，没有第二条写手机的入口。

默认 `auto`：USB 已挂载且没休眠就走 USB，否则走已经连上的蓝牙 HID。`0 hid usb`、`0 hid ble`、`0 hid auto` 写入 NVS，重启仍有效。`0 hid pair` 打开 60 秒配对窗口，iPhone 在设置 → 蓝牙里选择名字以 ESP32 Agent 开头的设备。`0 hid unpair` 删掉 HID 名单里的绑定。这些地址和配套 App 存在同一处 NimBLE 绑定里，所以配套 App 的绑定也会一起删掉。名单存在 NVS 的 `hid` 命名空间，重启后 iPhone 可以自己连回来。

鼠标、键盘、地球键（切换输入法，用量 0x029D）是同一个 HID 服务，和原来的 GATT 共用一个广播。Wi-Fi、AirPlay 和 BLE 共用一块射频。连接间隔保持在建链时的 15 ms，不再为手势改短或改长，避免把同一条链上的画面拖慢。蓝牙报告比 USB 少、步子更大（pace 4，USB 仍是 pace 3），并且要等上一包 HID 归还缓冲、同时给视频留出 16 块 mbuf。电脑发现 pace 数字变了会丢掉旧校准。

这一节描述的是固件和电脑端的行为。蓝牙 HID 还没有在真 iPhone 上验证。

蓝牙配对默认关闭。固件不再在每次上电后的 3 分钟里接受没有密码的 Just Works 配对，否则旁边的设备可以读到 Wi-Fi 密码、vtok，并发 HID 或用 vtok 做 OTA。需要实验 BLE / 7202 时，在 COM 口发送 `0 ble pair on`，窗口 60 秒，`0 ble pair off` 可以提前关掉。已经绑定过的设备仍可重连。日常控制不需要这一步。

Kz 的 `Device.send()` 原来要求回复恰好是 `ok`。适配层剥掉 `<id> ` 前缀后再做同样的判断，所以 `phone.py` 不用改命令字符串。`netinfo` 和 `status` 的正文是 JSON，不是 `ok`，走单独的 `query()`，不会被当成 HID 失败。

`0 netinfo` / `0 status` / `0 net ` 在固件里是按整行匹配的快捷分支，所以这三条的 id 固定用 `0`。其它命令用递增 id，避免和启动时的杂散字节对上。

### 7201 画面协议（按固件实现，不猜）

来源：`wifi_video_tcp.cpp`、`wifi_video.h`、`src/video/gop.c` 的 `vchunk_next`。

- 客户端首次连接发一行 `hello <vtok>\n`，等板子发送 CONFIG 和 IDR；token 不对，板子直接断开。同一板子/token 断线重连时保留解码器，发 `hello <vtok> at <ts>`，从已解码的最后一个 AU 之后续传。连接结束前发布限帧暂存的最后一张图，续传时间戳仍按已解码 AU 推进。时间戳已不在缓存中但参考链完整时，板子回退 CONFIG 和 IDR；参考链缺失则等新 IDR。序号断了再发 `resync`。静屏没有新帧但保活仍在时不重置解码器。
- 之后每秒一行 `ka`。板子在 `RECV_TIMEOUT_MS`（5 秒）内收不到就拆会话。我们按 1 秒发。
- 板子下发的记录是 `[u16 LE 长度][chunk]`。长度为 0 的记录是 hello/ka 的应答，不是视频。
- chunk 是 `[seq u16 LE][flags u8][payload]`。flags：bit0 START、bit1 END、bit2 IDR、bit3 CONFIG、bit4 BENCH、bit5 SNAP、bit6 RESUME、bit7（`WV_AUDIO`）音频。
- 带 START 的 chunk，payload 前 4 字节是板子时间戳（u32 LE），后面才是 Annex-B。
- 同一时刻只服务一个客户端。断线就重连，并重新 `hello`。序号不连续时发 `resync\n`，丢掉半个访问单元，重新等 IDR。
- 音频记录（bit7）丢掉，本阶段只做画面。
- 出厂分辨率 480×1040、5 fps（`cfg_width/height/fps` 的 NVS 缺省）。够微信文字界面用。串口 `res` / `fps` 可以改，上限 1920×2560、60 fps，但会吃 PSRAM 和带宽；默认不动。

解码只用 `av`（PyAV）的 wheel。不要求用户安装 ffmpeg、GStreamer、Bonjour 或 UxPlay。PyAV 在 Windows、macOS（Intel 与 Apple Silicon）和 Linux 都有现成 wheel。可行性报告里的 `decode_7201_poc.py` 已在 Linux 上把这种分帧解成 480×1040 的 JPEG；本仓库的测试用 PyAV 自己生成一小段 H.264，按同一分帧喂给同一个解码器，不调用系统 ffmpeg。

`Screen` 对外仍是 `start/stop`、`live`、`frame`、`updated`、`aspect`、`settled()`、`fresh()`。上面的模块不用知道传输换了。

### 为什么不用 WSL

Windows 10/11 上的运行时是：官方 Python 3.10+、PowerShell、pip wheel（PyAV、pyserial、esptool、FastAPI）。脚本逻辑在 `scripts/*.py`，`.ps1` 和 `.sh` 只是入口。虚拟环境优先用 `uv`，没有就退回 `python -m venv` + `pip`。不要求 Homebrew，也不要求 WSL。

服务默认只听 `127.0.0.1:8890`，本机防火墙通常不用为 8890 开入站。7201 是电脑主动连板子，属于出站。启动脚本会用中文提醒：如果系统弹出 Python 联网提示，选择允许专用网络出站。

### 速度：少调模型，而不是把帧率拉高

慢的主要原因是每一步都要等视觉模型看图，不是 USB 或 Wi-Fi。本仓库落地四件事：

1. **一次观察、多步动作。** 新增 `iphone_batch`：同一个 `screen_id` 上连续执行最多 8 个不依赖新画面的动作（点输入框、打拼音、按一个编辑键），最后只回一张图。Kz 的内置 actor 本来就可以在一条回复里给多行 `DO`，这条保留。选词、发送这种“必须先看见结果”的步骤不要放进同一次 batch。
2. **固定界面用坐标，不每次全屏找按钮。** `skills/*/layout.json` 提供命名坐标（0..1）。核心只提供通用的 `iphone_layouts` / `iphone_layout_tap`，微信坐标放在 skill 目录，不写进 `phone.py`。可选的区域模板用 Pillow 做差异比较；真要认字，默认仍是宿主 Agent 看截图。不把 PaddleOCR / Tesseract 装进必选依赖，否则 Windows 小白的安装会失败。
3. **截图复用。** 沿用 `screen_id`：30 秒、单次有效，画面变了就作废。动作返回的新图既核对上一步，也作为下一步的 `screen_id`。板子出厂默认 5 fps（NVS 里 `fps` 的缺省）。电脑端把交给模型的帧上限默认收在 8，对应 `VIDEO_FPS`（`screen.py` 的缺省）；要改再设这个环境变量。
4. **模型可换。** 默认路径是宿主 Agent 自己的视觉模型，MCP 不需要额外的 key。若启用服务端回退（`IPHONE_MCP_ENABLE_DELEGATE=1`），`LLM_BASE_URL` / `LLM_MODEL` / `LLM_API_KEY` 是任何 OpenAI 兼容接口。换一个更快的视觉模型，是提速最直接的办法。

诚实的耗时预期（不是本仓库的真机测量；Kz-Aven 在 Mac 上的局部记录是指针定位中位数大约 5 秒）：短回复大约 30–60 秒，长回复更久。群里几十条未读不要指望一次回完。不要写“秒回”或“实时”。

### 微信：skill 管内容，代码管熔断

核心服务不知道“微信发送键”在业务上是什么，只提供点、滑、打字、拼音、截图。群回复的语气、话题、禁词、时段写在 `skills/wechat-group-reply/SKILL.md`，这是内容约束的唯一地方。

系统熔断在 `hidagent/guard.py`：风险文字仍需 Agent 调用 `skill_check` 或 `skill_confirm_send` 报告；一旦熔断，底层 HID gate 会自动拦截点、滑、打字等操作。紧急 stop 所需的 `release` / `home` 命令保留豁免，用于松开按键和归位指针。微信 skill 不能把它关掉：

- 观察到的文字里出现「操作频繁」「风控」「验证码」：立刻跳闸，之后的发送检查直接拒绝，不自动重试。跳闸写在数据目录的 `guard.json`，重启服务不会清掉；人工确认手机上的提示已经处理之后，从本机网页显式重置，保留这一小时的发送次数（没有给 Agent 的解除工具）。
- 频率：默认两次发送至少间隔 8 秒、每小时最多 20 次、23:00–07:00 不发。skill 只能把间隔加大、次数减少，不能低于 3 秒、不能超过每小时 30 次。
- `prepare` 先占一次性名额；Enter 和命名 `send` 按钮在实际 HID 发送前统一消费并更新发送时间，气泡确认不重复计数。
- `skill_confirm_send` 只有在 `bubble_visible=true` 时才把这次计为发送成功。输入框清空、收到 `ok`，都不算。服务端看不到气泡的语义，这一位由看着截图的 Agent 填写；工具说明写明不许在没看见完整新气泡时填 true。这是技术校验，不是“停下来等用户批准”。

没有草稿接口，没有确认弹窗。风险写在 README 和 skill 里：用专用运营号，不要用主号；腾讯的条款不允许自动化操作，行为特征（秒回、全天、高频、模板化）仍可能被限制。

截图会在宿主 Agent 或你配置的视觉模型里离开本机。不要把聊天记录、Wi-Fi 密码、API key 写进仓库或日志。`netinfo` 里的 `pass` 会在进入状态接口和日志之前丢掉。

### 小白路径

1. 安装 Python 3.10+（Windows 用 python.org 的安装包，勾选 PATH），运行 `setup`。
2. COM 口插电脑，运行 `flash`（esptool 写入 CI 打好的 merged.bin）。
3. 运行 `wifi`，把家里的 2.4GHz 名称和密码经串口写进 NVS。
4. USB 口经数据线（Lightning 手机要用相机转换器）接 iPhone，打开辅助触控，屏幕镜像选 “ESP32 Agent”。运行 `run-server`，打开 `http://127.0.0.1:8890`，先等 device 和 screen 变绿，再校准，第三盏灯随后才绿。
5. 运行 `mcp-config`，把片段贴进 Agent，让它把指针移到屏幕中央并停住，不要只看 ACK。

每一步失败都走中文错误，带下一步，不把 traceback 甩给用户。网页上 `device` / `screen` / `calibrated` 三个灯常亮，缺一个就显示缺什么、怎么修。

### 供电（必须让使用者自己看一眼板子）

iPhone 11 Pro 是 Lightning，要“Lightning 转 USB 相机转换器”，手机能给的电流有限，可能弹出“配件耗电量太大”。COM 口同时接电脑可以帮板子供电，**前提是两个 USB 的 VBUS 没有被短接**。不少 N16R8 克隆板有一个标着 USB-OTG 或 IN-OUT 的焊盘，焊上之后电脑的 5V 会倒灌进手机。刷机前确认这个焊盘是断开的。STA 模式没有把发射功率降到 5 dBm（固件只在 AP 分支里降了），电流峰值会更高，所以更要让电脑从 COM 口供电，并盯着有没有 brownout 复位。

## 目录

```
hardware/hid_bridge/     Handsy 固件（GPL-3.0 整体，因为链接 airplay）
hardware/tests/          固件里纯 C 模块的主机测试
hardware/flash.sh|.ps1   开发者编译，以及转给 scripts/flash.py 的刷机入口
server/hidagent/         MIT。device=串口，screen=7201，另有 video7201、guard、layouts
server/tests/
scripts/                 跨平台 Python 入口；.sh / .ps1 只负责调用它们
skills/                  可插拔 skill。微信群回复在这里，不在核心里
prompts/                 Agent 系统提示
docs/                    中文。macOS 与 Windows 分开写
```

## 已知做不到或还没测的

- 没有在真 iPhone、真 N16R8、真 Windows 桌面上跑通过。蓝牙 HID 同样没有真机记录：配对、重启重连、指针加速、中文输入、和 AirPlay 共用射频，都要在真 iPhone 上再做一遍。CI 能证明的是：固件能编译、PyAV 能解我们自己生成的 7201 分帧、串口协议能在假串口上往返、传输选择和错误字符串能在主机测试里跑通、MCP stdio 能列出并调用工具、PowerShell 脚本能在 `windows-latest` 上解析并跑到 `--help`。
- 锁屏、密码、Apple Pay、DRM 画面不会出现在镜像里。镜像停止后要解锁并重新选一次屏幕镜像。
- `type` 只接受可打印 ASCII。中文走拼音和候选词，emoji 打不了。
- 横屏不支持。指针是相对坐标，靠校准和偶尔 `home` 消除漂移；Home 键机型的底部上滑不一定回主屏幕，需要辅助触控。
- 预编译固件是某个 commit 的产物。改了 `hardware/hid_bridge` 却没有新的 CI 产物时，刷机脚本会拒绝使用过期的 bin，并告诉你怎么编译。
