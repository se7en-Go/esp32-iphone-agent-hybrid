"""给使用者看的中文说明。不包含密码、聊天内容或堆栈。"""

NO_PORT = (
    "没找到串口。请把板子上标着 COM 的 Type-C（不是标着 USB 的那个口）"
    "用能传数据的线接到电脑。只充电的线不行。"
    "COM 口上的芯片可能是沁恒 CH343、FTDI 或 CP210x。"
    "Mac 上一般是 /dev/cu.usbserial-… 或 /dev/cu.wchusbserial…，Windows 是 COMx。"
    "Windows 如果仍然没有 COM 口，FTDI 板子装 FTDI VCP 驱动："
    "https://ftdichip.com/drivers/vcp-drivers/ ；沁恒板子装 CH343 驱动："
    "https://www.wch.cn/downloads/CH343SER_EXE.html 。macOS 上 FTDI 通常不用装驱动。"
    "也可以设置环境变量 HIDAGENT_PORT，例如 COM5 或 /dev/cu.usbserial-A5069RR4。"
)

PORT_SILENT = (
    "串口打开了，但板子没有回答 0 ping。确认线插在 COM 口、固件已经烧进这块 N16R8，"
    "并且没有别的串口监视器占着这个口。然后重新运行服务。"
)

BOARD_NOT_CONNECTED = (
    "板子没连上。确认 COM 口已插好。网页上的 device 灯灭着时，先拔插一次 COM 线，"
    "或设置 HIDAGENT_PORT 后再启动。"
)

NO_SCREEN = (
    "还没有画面。请解锁手机，在控制中心打开「屏幕镜像」，选择「ESP32 Agent」。"
    "板子、手机和电脑要在同一个 2.4GHz 网络，路由器不要开 AP 隔离。"
    "iOS 16/17 镜像到板载接收器还没有在这块板上验证过；列表里如果没有 ESP32 Agent，"
    "请看 docs/troubleshooting.md。"
)

NOT_CALIBRATED = (
    "指针还没校准。device 和 screen 两盏灯亮了之后，打开 http://127.0.0.1:8890 点「校准」，"
    "再在画面里点一下圆点。第三盏灯这时才会绿。"
)

UXPLAY_WINDOWS = (
    "SCREEN_BACKEND=uxplay 在 Windows 上不可用（UxPlay 依赖 GStreamer，而且 pass_fds 不存在）。"
    "请改回默认的 board，用板子自己的 TCP 7201。"
)


def port_open_failed(name: str) -> str:
    return (
        f"打不开串口 {name}。它可能正被串口监视器占用，请先关掉监视器，或拔插 COM 口。"
        "插到标着 USB 的口时，电脑上看不到这个串口。"
    )


def link_lost(command: str) -> str:
    shown = "net" if command.startswith("net ") else command.split(" ", 1)[0][:24]
    return (
        f"发送 {shown} 时串口断了。板子可能复位了，供电不足时会这样。"
        "请确认 USB-OTG / IN-OUT 焊盘没有短接，COM 口仍插在电脑上，然后重新运行服务。"
    )


def explain_reply(command: str, reply: str) -> str:
    if "asleep" in reply:
        return "手机 USB 休眠了：解锁 iPhone，保持屏幕亮着，辅助触控保持打开，然后再试。原始回复：" + reply
    head = "net" if command.startswith("net ") else command.split(" ", 1)[0][:24]
    if reply.startswith("err"):
        return f"板子拒绝了 {head}：{reply}。若刚插上，先解锁手机并打开辅助触控。"
    return f"板子对 {head} 的回复不是 ok：{reply or '空回复'}"


def for_user(text: str) -> str:
    """把会直接出现在网页上的英文运行时错误换成下一步。校验错误保持原文，测试和 Agent 依赖那些句子。"""
    table = (
        ("pointer not calibrated", NOT_CALIBRATED),
        ("live screen required", NO_SCREEN),
        ("pointer could not be verified", "没有在画面里找到指针，这次没有点击。请重新校准，或先观察一次画面。"),
        ("no phone screen", NO_SCREEN),
        ("no live screen", NO_SCREEN),
        ("lost the phone screen", NO_SCREEN),
    )
    for key, zh in table:
        if key in text:
            return zh
    return text


def hints(device: bool, screen: bool, calibrated: bool, last_error: str = "",
          screen_error: str = "") -> dict:
    """三个状态灯各自的中英提示。灯亮时提示为空。"""
    device_zh = last_error or BOARD_NOT_CONNECTED
    device_en = last_error or "Board serial is not connected. Plug the COM port into the computer."
    if screen:
        screen_zh, screen_en = "", ""
    elif not device:
        screen_zh, screen_en = "device 还没亮。先接好 COM 口，再开屏幕镜像。", "Connect the board before mirroring."
    else:
        screen_zh = screen_error or NO_SCREEN
        screen_en = screen_error or "No mirror yet. Control Center → Screen Mirroring → ESP32 Agent."
    if screen and screen_error:
        screen_zh, screen_en = screen_error, screen_error
    cal_zh = NOT_CALIBRATED if device and screen else (
        "先让 device 和 screen 两盏灯亮，再点校准。校准成功后第三盏灯才变绿。"
    )
    cal_en = "Turn device and screen on, then calibrate. The third light turns green after that."
    return {
        "device": {"zh": "" if device else device_zh, "en": "" if device else device_en},
        "screen": {
            "zh": "" if screen and not screen_error else screen_zh,
            "en": "" if screen and not screen_error else screen_en,
        },
        "calibrated": {"zh": "" if calibrated else cal_zh, "en": "" if calibrated else cal_en},
    }
