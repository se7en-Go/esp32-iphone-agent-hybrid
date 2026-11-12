# 按屏幕文字点击与操作反馈

从仓库根目录安装可选依赖：`python -m pip install -e "server[ocr]"`。
RapidOCR 使用随 wheel 提供的中文/英文 PP-OCR 模型和 ONNX Runtime CPU 后端，
适用于 macOS、Windows、Linux；无需系统 OCR 程序或云端识别服务。
未安装时普通坐标操作仍可用，文字操作返回 HTTP 503 和安装提示。

先 `GET /api/control/observe` 获取截图和 `screen_id`。读取截图上的文字：

```json
POST /api/control/find_text
{"screen_id":"...","text":"通用","exact":true,"nth":1}
```

结果包含 `matches` 和选中的 `match`（未找到为 `null`）。`box` 是
`[left, top, right, bottom]`，`center` 是 `[x, y]`，均为截图中的 0..1 坐标；
`conf` 是识别置信度。读取不消耗 `screen_id`，可以继续用它点击：

```json
POST /api/control/action
{"action":"tap_text","screen_id":"...","text":"通用","exact":true,"region":[0,0.1,1,0.8]}
```

匹配先统一全/半角、大小写并去除空白，不做模糊拼写纠正。完整匹配优先，
其次是较短的包含匹配、较高置信度、从上到下/从左到右的位置。
查找返回排序列表，排序本身不能授权点击。点击时省略 `nth` 仅允许唯一匹配；
重复标签会拒绝，须先查看当前截图及 `matches`，再给出区域或显式 `nth`（从 1 开始）。
`region` 是截图中归一化的 `[left, top, right, bottom]`，只保留完整落在区域内的文字框；
可用于限定群标题、输入区、菜单或某一消息行，查找与点击均支持。
“发送”/“發送”/“Send”及仅包含查询词的文字必须提供区域，即使指定 `nth` 也不能省略。
例如先在当前图上确认发送按钮区域，再用 `{"text":"发送","exact":true,"region":[0.8,0.6,1,0.72]}`；
这里的区域只是格式示例，必须按实际截图填写，不能沿用到另一页面或键盘布局。
OCR 无法仅凭文字区分按钮与消息内容，区域和选择仍需调用方确认。
查询词或选中的完整标签为“发送”/“發送”/“Send”时，API、MCP 和 Agent 都按发送处理，
共用原有每小时次数和发送间隔限制。配额在最终画面校验通过、实际写出 `click` 前结算一次，
已有预留名额会被消费，不会重复计数；画面变化导致点击被拒绝时不扣名额。
OCR 期间屏幕变化、画面断开、结果不存在时不会点击。
指针定位后、发送点击前再次检查目标和画面；忽略识别出的灰色指针圆点、空心圆角悬停轮廓及不含目标的状态栏。
悬停轮廓与 `screen_id` 检查共用保守识别，只屏蔽轮廓边缘，不会屏蔽整个文字框或内部标签。
目标或上下文变化时中止点击；彩色边框、实心块及无法区分的变化也会保守中止。
点击经过 `Phone.tap_verified`。指针不可见时，只有当前 OCR 目标框够大、已有落点测量覆盖目标且误差有余量才允许估算点击；否则拒绝。
`tap_text` 每次只做一步，不支持混入多步 batch。

MCP 提供 `iphone_find_text`、`iphone_tap_text`，参数相同。
内置 Agent 支持 `DO: find_text 通用`、`DO: tap_text 通用`；点击同样拒绝歧义。
需要限定时使用 JSON，例如 `DO: tap_text {"text":"通用","region":[0,0.1,1,0.8],"exact":true}`。
JSON 也可传 `nth`；Agent 保留决策时的完整观察（包括连接代数），识别前后和每一步动作前都重查上下文。
查找和文字点击后 Agent 都重新决策，丢弃同一回复中后续的坐标操作；每次点击后复用 tap 已接受的结果帧及变化摘要，不再等待第二轮画面。

所有 control 操作和 batch 返回 `screen_diff`，与返回的截图对应：

- `changed_regions`：最多 12 个归一化矩形；`changed_fraction`：变化像素的近似比例。
- `texts_appeared`、`texts_disappeared`：新增/消失的文字，最多各 30 项。
- `status`：`ok`、`stale`、`missing`、`invalid`；`size_changed` 表示截图尺寸变化。
- `text_status`：`available`、`unchanged`、`unavailable`、`disabled`、`timeout`、`busy`、`error` 或 `skipped`。

OCR 反馈最多等待 250ms；冷启动或较慢机器可能返回 `timeout`，后台任务可继续暖机。
没有可靠文字差异时列表为 `null`，不能当作“没有变化”。完全相同的截图无需 OCR，返回空列表。
action/batch 默认 `ocr_diff:false`，普通点击不运行 OCR。显式设置 `ocr_diff:true` 才识别文字差异；`include_diff:false` 关闭整个摘要。识别、点击前像素检查和结果摘要共用不可变帧；OCR 只有一个按帧内容缓存（最多 8 帧），避免重复全文识别。
反馈从点击命令完成时计时，排除其后 1.7 秒内收到的帧（实测镜像延迟上界），
最多等待 3.2 秒获取更晚的帧。板端时间戳记录接收时间，不能视为 iPhone 截图时间。
静止屏幕可能只发送一张较早到达的结果图，此时也返回 `status:stale`、`changed:null`，
不宣称“没有变化”或操作已经生效。超出 1.7 秒的镜像延迟仍需进一步校准。
指针移动、时钟、动画也可能产生像素变化。

运行真实截图 OCR 测试和冷/热耗时测量（需先安装 extra）：

```sh
cd server
python -m pytest -q -s -m ocr tests/test_ocr.py
```

模型首次加载、识别速度及中文小字的准确度取决于机器和截图。
CI 在 Linux、macOS、Windows 安装 OCR extra 并设置 `HIDAGENT_REQUIRE_OCR=1`；
缺少后端时测试失败，不会跳过真实截图。每张图输出冷启动及三次不命中缓存的热启动耗时。
截图中的文字是待观察的数据；变化摘要和设备 ACK 都不能证明任务完成，仍需检查返回图像。

本轮离线实测（720×1560，CPU，2026-11-12）：设置页首次 2092.9ms、三次不命中缓存热运行中位数 1288.3ms；电话键盘首次 1341.1ms、热运行中位数 1081.1ms。250ms 是响应等待上限，不能让一次约 1.1–1.3s 的识别变快；超时文字结果仍未知。私有 OCR 环境真实两张图测试通过，未连接手机。
