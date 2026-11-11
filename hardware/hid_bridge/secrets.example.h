// 开发者可选：hardware/build_firmware.sh --sta 才会读取 secrets.h（从本文件复制）。
// 普通使用者不要填这里，也不要编译。家里 Wi-Fi 由电脑经 COM 口写入板子 NVS
// （scripts/wifi_config.py），换地方不用重烧，也不会把密码提交进仓库。
#define WIFI_SSID "your-wifi"
#define WIFI_PASS "your-password"
