# 为什么这里没有 esp32agent

Handsy 上游有一个 Go 写的 `esp32agent`。它是 MCP 服务，也能 `net join`，但第一次配对要走蓝牙，而且要在板子上电后的 3 分钟内完成。没有蓝牙的台式机配不了。它的视频解码还要本机另装 ffmpeg。

本仓库的电脑端不走那条路：

- 命令、vtok、配网：COM 口串口，`<id> <command>`
- 画面：Wi-Fi TCP 7201，PyAV 解码

TCP 7202 的加密 CLI 协议（配对仍要蓝牙）留作实验，本阶段不实现。需要那条无线通道时，直接使用上游 Handsy 的 CLI，不要把 GPL 的 airplay 代码拷进这里。
