// UI strings and plain-language step descriptions, in English and Simplified Chinese.
const I18N = {
  en: {
    waiting: "Waiting for the phone screen…", liveCaption: "Live screen · click to tap, drag to swipe",
    estop: "■ Emergency stop (Esc)", motionOn: "Motion: on", motionOff: "Motion: off", estopHint: "Stops the AI and lets go of the screen",
    clear: "Clear history",
    manual: "Manual controls", home: "Home", back: "Back", calibrate: "Calibrate", type: "Type", press: "Press",
    typePh: "Type text", keyPh: "Key: enter, cmd+a…", log: "Live log",
    tagline: "Tell the AI what to do. It looks at the screen, decides, and taps for you.",
    s1: "COM port to the computer", s1d: "The port labelled COM goes to the computer. The port labelled USB goes to the iPhone. Turn on AssistiveTouch.",
    s2: "Mirror to the board", s2d: "Control Center → Screen Mirroring → ESP32 Agent. Phone, board and computer share one 2.4 GHz LAN.",
    s3: "Calibrate the pointer", s3d: "When all three lights are on, choose Calibrate and click the round pointer.",
    taskPh: "e.g. Open Whiteboard and draw a smiley face", start: "Start", stop: "Stop",
    status: "status", actions: "actions", total: "total time", perAction: "per action", plan: "Plan",
    idle: "Idle", working: "Working…", done: "Done", gaveUp: "Gave up", stopped: "Stopped", errorState: "Error",
    calNow: "Now click exactly on the round pointer in the live screen.", calFinding: "Finding the pointer…", calOk: "Calibrated. You're ready.",
    needTask: "Describe what the phone should do first.", thinking: "thinking", doing: "doing",
    step: "Step", planner: "Planner", decision: "Decision · planner", executor: "Action · actor", avg: "avg",
    examples: ["Set an alarm for 7:00 AM that repeats every day (check all days under Repeat), then save it", "Open Whiteboard and draw a smiley face",
      "Open Settings and go to Wi-Fi", "Open Notes and write \"hello\""],
  },
  zh: {
    waiting: "等待手机画面…", liveCaption: "实时画面 · 点击 = 点按，拖动 = 滑动",
    estop: "■ 紧急停止（Esc）", motionOn: "动效：开", motionOff: "动效：关", estopHint: "立即停止 AI，并松开屏幕",
    clear: "清空记录",
    manual: "手动操控", home: "主屏幕", back: "返回", calibrate: "校准", type: "输入", press: "按键",
    typePh: "输入文字", keyPh: "按键：enter、cmd+a…", log: "实时日志",
    tagline: "告诉 AI 要做什么，它看屏幕、做决定、替你点。",
    s1: "COM 口接电脑", s1d: "标着 COM 的口接电脑，标着 USB 的口接手机，并打开辅助触控。",
    s2: "屏幕镜像到板子", s2d: "控制中心 → 屏幕镜像 → ESP32 Agent。手机、板子、电脑在同一个 2.4GHz 网络。",
    s3: "校准指针", s3d: "三个灯都亮后点「校准」，再在画面里点一下圆点。",
    taskPh: "例如：打开白板画一个笑脸", start: "开始", stop: "停止",
    status: "状态", actions: "动作数", total: "总用时", perAction: "每步", plan: "计划",
    idle: "空闲", working: "进行中…", done: "完成", gaveUp: "放弃", stopped: "已停止", errorState: "出错",
    calNow: "现在在实时画面里精确点一下圆形指针。", calFinding: "正在找指针…", calOk: "校准完成，可以开始了。",
    needTask: "先写一下要让手机做什么。", thinking: "思考", doing: "执行",
    step: "第", planner: "规划", decision: "决策 · 规划模型", executor: "执行 · 动作模型", avg: "平均",
    examples: ["设置早上 7:00 的闹钟，在「重复」里把每一天都勾上，然后保存", "打开白板画一个笑脸", "打开设置，进入 Wi-Fi", "打开备忘录写 hello"],
  },
};

// One short phrase per action, in both languages: [english, chinese].
function describeStep(s) {
  const sideways = s.action === "swipe" && Math.abs(s.x2 - s.x1) > Math.abs(s.y2 - s.y1);
  const table = {
    plan: ["Made a plan", "制定了计划"],
    "replan (stuck)": ["Stuck — made a new plan", "卡住了，重新规划"],
    replan: ["Rethinking the plan", "重新考虑计划"],
    tap: ["Tap", "点按"],
    swipe: sideways ? ["Swipe sideways", "左右滑动"] : ["Swipe up / down", "上下滑动"],
    path: ["Draw a stroke", "画一笔"],
    circle: ["Draw a circle", "画一个圆"],
    type: [`Type “${s.text}”`, `输入“${s.text}”`],
    key: [`Press ${s.combo}`, `按 ${s.combo}`],
    home: ["Go to the Home Screen", "回到主屏幕"],
    back: ["Go back", "返回"],
    wait: ["Wait for the screen", "等待画面加载"],
    hiccup: ["Hiccup — recovering", "出了点小问题，正在恢复"],
    done: ["Done", "完成"],
    fail: ["Gave up", "放弃了"],
    stopped: ["Stopped by you", "已被你停止"],
    error: ["Something went wrong", "出错了"],
  };
  return table[s.action] || [s.action, s.action];
}
