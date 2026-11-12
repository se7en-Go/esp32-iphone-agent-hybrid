# 排错

每条都是「你看到什么 → 下一步」。不要把 Wi-Fi 密码或聊天内容贴出来。

## 没找到串口

线要插在标着 **COM** 的口，并且是数据线。标着 USB 的口在电脑上不会出现这个串口。芯片可能是 FTDI、沁恒 CH343 或 CP210x。Mac 上一般是 `/dev/cu.usbserial-…` 或 `/dev/cu.wchusbserial…`，Windows 是 `COMx`。FTDI 在 macOS 上通常不用装驱动。Windows 没有 COM 口时，FTDI 装 https://ftdichip.com/drivers/vcp-drivers/ ，沁恒装 https://www.wch.cn/downloads/CH343SER_EXE.html 。仍不行就设置 `HIDAGENT_PORT`（`COM5` 或 `/dev/cu.usbserial-A5069RR4`）。先关掉已经打开的串口监视器。

## device 灯是红的

COM 口没连上，或固件没刷进去。拔插 COM 线，看启动窗口里的中文说明。`0 ping` 没有 `ok` 时，先重刷一次。

## 刷机失败

确认不是只充电的线。换 `--baud 115200`。按住 BOOT 再插 COM 口，立刻重跑刷机脚本。不要对标着 USB 的口运行 esptool。

## screen 灯是红的

解锁手机，控制中心重新选 **ESP32 Agent**。板子、手机、电脑要在同一个 2.4GHz，路由器不能开 AP 隔离或访客隔离，要放行 mDNS。企业 802.1X 和需要网页登录的 Wi-Fi 不行。

列表里一直没有 ESP32 Agent：iOS 16/17 镜像到板载接收器还没有在这块板上验证过。退路是升级到 iOS 18，或只在 Mac 上使用 `SCREEN_BACKEND=uxplay`。Windows 不要设 uxplay。

## 手机提示配件耗电量太大，或板子自己重启

看串口状态里的 `rst`。若是 brownout 或 power-on，是供电不够。COM 口保持接在电脑上给板子供电。**先看板上 USB-OTG / IN-OUT 焊盘没有被短接**，短接会把电脑的 5V 倒灌进手机。Lightning 手机要用相机转换器，转换器能给的电流有限。

## calibrated 灯是红的

前两个灯先绿。然后在网页里点校准，再点画面上的圆点。圆点没出现就是辅助触控没开，或当前这条控制通道没通。网页连接行会写着 HID 走 USB 还是蓝牙。走 USB 时检查标着 USB 的口。走蓝牙时，在 iPhone 设置 → 蓝牙里选择 **ESP32 Agent**，电脑上可运行 `scripts/hid-mode.sh pair`（Windows 用 `hid-mode.ps1`）。换了 USB 和蓝牙之后，旧的校准会被清掉，需要再点一次校准。蓝牙 HID 还没有在真机上验证。

## 中文只有拼音、或候选不对

`iphone_compose_chinese` 故意不选词。看它返回的图，首选对就按空格，否则点正确的候选，再核对输入框里的汉字。标点不要塞进这一步。emoji 打不了。

## 指针点偏

重新校准。小按钮用带核对的点击（`iphone_tap`），不要在没看到圆点时连续盲点。Home 键机型的底部上滑不一定回主屏幕，用辅助触控。

## 锁屏之后没画面

镜像会停，USB 也可能休眠。解锁，重新选一次屏幕镜像。命令若返回 asleep，就是在等你解锁。输入密码和付款界面本来就不会出现在镜像里。

## 连上板子热点之后电脑上不了网

这是预期。热点不提供互联网。请改回 STA：`wifi.sh --ssid ...`。只有电脑另有网线时才用 `--ap`。

## 手机蓝牙列表里没有 ESP32 Agent

COM 口仍接电脑。运行 `scripts/hid-mode.sh pair`（Windows 用 `hid-mode.ps1`），60 秒内在 iPhone 设置 → 蓝牙里选择名字以 **ESP32 Agent** 开头的设备，并打开辅助触控。列表里已经有旧的同名设备时，先在系统蓝牙里忽略它，再运行 `hid-mode.sh unpair` 或 `hid-mode.ps1 unpair`，然后重新 pair。网页 `/api/status` 的 `hid.ble` 会写着是否在广播、是否连上、是否已绑定。这一步还没有在真机上验证。

## 命令说 phone not connected over usb/ble

当前模式是 auto，USB 没插上，蓝牙 HID 也没连上。插上标着 USB 的口，或按上面的步骤配对。想固定一种：`hid-mode.sh usb`、`hid-mode.sh ble` 或 `hid-mode.sh auto`。Windows 把 `hid-mode.sh` 换成 `hid-mode.ps1`。手机锁屏后 USB 可能休眠，解锁即可；回复里带 asleep 就是这件事。
