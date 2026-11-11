# 固件来源

`hid_bridge/` 来自 **ESP32 iPhone Agent by Henry Yuan**

- https://github.com/onehumanbeing/esp32-iphone-agent
- commit `5a4fddf`（2026-11-10）

协议没有改：串口 `<id> <command>`、TCP 7201 的 `hello <vtok>` / `ka`、NVS 里的 `net` 配置，都与该 commit 一致。

本目录整体按 **GPL-3.0** 分发，因为 `src/airplay/` 是 UxPlay / playfair 的 GPL-3.0 移植，固件二进制链接了它。全文见 `src/airplay/LICENSE-GPL.md` 和 `src/airplay/playfair/LICENSE.md`。

相对上游的改动只有：

- `hid_bridge.ino` 顶部多了一段说明，编译行为不变。
- `secrets.example.h` 的注释改成“这不是普通使用者的配网方式”。
- 编译入口换成 `build_firmware.sh` / `build_firmware.ps1`，FQBN 针对 N16R8：

```
esp32:esp32:esp32s3:USBMode=default,CDCOnBoot=default,FlashSize=16M,PSRAM=opi,PartitionScheme=app3M_fat9M_16MB
```

`CDCOnBoot=default` 让串口留在 COM 口（CH343）。`default_16MB` 这个 PartitionScheme 在通用 esp32s3 板型上不存在，所以用带 OTA 和 coredump 的 `app3M_fat9M_16MB`。

核心版本：esp32 Arduino core **3.3.12**，库 **NimBLE-Arduino 2.5.1**。编译时必须 `-include hid_bridge/nimble_cfg.h`，和上游 `hardware/flash.sh` 一样。
