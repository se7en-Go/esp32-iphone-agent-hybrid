# 为什么这里没有 esp32agent

Handsy 上游有一个 Go 写的 `esp32agent`。它是 MCP 服务，也能 `net join`，但第一次配对要走蓝牙，而且要在板子上电后的 3 分钟内完成。没有蓝牙的台式机配不了。它的视频解码还要本机另装 ffmpeg。

本仓库的电脑端不走那条路：

- 命令、vtok、配网：COM 口串口，`<id> <command>`
- 画面：Wi-Fi TCP 7201，PyAV 解码

TCP 7202 的加密 CLI 协议留作实验，本阶段不实现电脑端。需要那条无线通道时，直接使用上游 Handsy 的 CLI，不要把 GPL 的 airplay 代码拷进这里。

上游 CLI 走应用层会话：BLE 上先发 PAIR / HELLO，之后的命令都是带计数器的密封帧，不建立操作系统的蓝牙绑定。固件按这个协议收 CLI 特征上的帧；没有打开应用层会话的写入不会变成 HID 或配网命令。手机那条 HID 链路仍然要链路加密。实验 BLE 时在 COM 口发 `0 ble pair on`，那是给手机配对用的 60 秒窗口，不是 Handsy CLI 的登录方式。不要为了 CLI 把未加密的 HID 写入重新放行。
