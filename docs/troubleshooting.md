# 排错

每条都是「你看到什么 → 下一步」。不要把 Wi-Fi 密码或聊天内容贴出来。

## 没找到串口

线要插在标着 **COM** 的口，并且是数据线。标着 USB 的口在电脑上不会出现这个串口。芯片可能是 FTDI、沁恒 CH343 或 CP210x。Mac 上一般是 `/dev/cu.usbserial-…` 或 `/dev/cu.wchusbserial…`，Windows 是 `COMx`。FTDI 在 macOS 上通常不用装驱动。Windows 没有 COM 口时，FTDI 装 https://ftdichip.com/drivers/vcp-drivers/ ，沁恒装 https://www.wch.cn/downloads/CH343SER_EXE.html 。仍不行就设置 `HIDAGENT_PORT`（`COM5` 或 `/dev/cu.usbserial-A5069RR4`）。先关掉已经打开的串口监视器。

## device 灯是红的

COM 口没连上，或固件没刷进去。拔插 COM 线，看启动窗口里的中文说明。`0 ping` 没有 `ok` 时，先重刷一次。

## 刷机失败

确认不是只充电的线。换 `--baud 115200`。按住 BOOT 再插 COM 口，立刻重跑刷机脚本。不要对标着 USB 的口运行 esptool。

## screen 灯是红的

解锁手机，在控制中心重选网页提示的接收器。Mac 接收器是 **ESP32 Agent Mac**，板载接收器是 **ESP32 Agent**。手机和接收器需要在能互通的同一局域网，使用板载接收器时电脑也要能连到它。检查路由器是否启用了访客隔离、AP 隔离，或拦截了 mDNS。

Mac 接收器一直找不到时，先运行 `./scripts/run-local.sh status` 确认接收器已经启动，再检查 Mac 与 iPhone 的网络。板载接收器的兼容性需要按机型和系统验证。Mac 可以按 [本机运行说明](RUNTIME.md) 切到独立接收器，Windows 使用板载接收器。

## 手机提示配件耗电量太大，或板子自己重启

看串口状态里的 `rst`。若是 brownout 或 power-on，是供电不够。COM 口保持接在电脑上给板子供电。**先看板上 USB-OTG / IN-OUT 焊盘没有被短接**，短接会把电脑的 5V 倒灌进手机。Lightning 手机要用相机转换器，转换器能给的电流有限。

## calibrated 灯是红的

等 device 和 screen 变绿，再在网页点「校准」。如果网页提示手动确认，点镜像里的指针圆点。没有指针时，先检查辅助触控与当前输入连接。USB 模式检查板上的 USB 口，蓝牙模式检查 **ESP32 Agent** 是否已连接。切换 USB 和蓝牙后需要重新校准。

## 中文只有拼音、或候选不对

`iphone_compose_chinese` 故意不选词。看它返回的图，首选对就按空格，否则点正确的候选，再核对输入框里的汉字。标点不要塞进这一步；这个拼音入口不支持 emoji。中文、数字和 emoji 的粘贴入口见 [PASTE_INPUT.md](PASTE_INPUT.md)，需要先配置快捷指令或伴侣应用。

## 指针点偏

重新校准。小按钮用带核对的点击（`iphone_tap`），不要在没看到圆点时连续盲点。Home 键机型的底部上滑不一定回主屏幕，用辅助触控。

## 锁屏之后没画面

镜像会停，USB 也可能休眠。解锁，重新选一次屏幕镜像。命令若返回 asleep，就是在等你解锁。输入密码和付款界面本来就不会出现在镜像里。

## 连上板子热点之后电脑上不了网

这是预期。热点不提供互联网。请改回 STA：`wifi.sh --ssid ...`。只有电脑另有网线时才用 `--ap`。

## 手机蓝牙列表里没有 ESP32 Agent

COM 口仍接电脑。运行 `scripts/hid-mode.sh pair`（Windows 用 `hid-mode.ps1`），60 秒内在 iPhone 设置 → 蓝牙里选择名字以 **ESP32 Agent** 开头的设备，并打开辅助触控。若旧的同名配对导致连接失败，可以先在 iPhone 忽略该设备，再对板子运行 `scripts/hid-mode.sh unpair` 后重新配对。`unpair` 会清除板子保存的蓝牙绑定，包括共用配对存储的绑定。网页 `/api/status` 的 `hid.ble` 会显示是否正在广播、是否连接和是否已绑定。

## 命令说 phone not connected over usb/ble

当前模式是 auto，USB 没插上，蓝牙 HID 也没连上。插上标着 USB 的口，或按上面的步骤配对。想固定一种：`hid-mode.sh usb`、`hid-mode.sh ble` 或 `hid-mode.sh auto`。Windows 把 `hid-mode.sh` 换成 `hid-mode.ps1`。手机锁屏后 USB 可能休眠，解锁即可；回复里带 asleep 就是这件事。
