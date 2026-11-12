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
{"action":"tap_text","screen_id":"...","text":"通用","exact":true,"nth":1}
```

匹配先统一全/半角、大小写并去除空白，不做模糊拼写纠正。完整匹配优先，
其次是较短的包含匹配、较高置信度、从上到下/从左到右的位置。
`nth` 从 1 开始，选择排序后的第几个匹配；重复标签应先查看 `matches`。
OCR 期间屏幕变化、画面断开、结果不存在时不会点击。
指针定位后、发送点击前再次检查目标和画面；只忽略识别出的灰色指针圆点及状态栏，
不会屏蔽整个文字框。目标或上下文变化时中止点击；指针高亮等无法区分的变化也会保守中止。
点击仍经过 `Phone.tap_verified`，需要先定位指针；文字识别不能修复指针高亮或消失问题。
`tap_text` 每次只做一步，不支持混入多步 batch。

MCP 提供 `iphone_find_text`、`iphone_tap_text`，参数相同。
内置 Agent 支持 `DO: find_text 通用`、`DO: tap_text 通用`，选取最佳匹配。
查找和文字点击后 Agent 都重新决策，丢弃同一回复中后续的坐标操作；每次点击后读取新画面和变化摘要。

所有 control 操作和 batch 返回 `screen_diff`，与返回的截图对应：

- `changed_regions`：最多 12 个归一化矩形；`changed_fraction`：变化像素的近似比例。
- `texts_appeared`、`texts_disappeared`：新增/消失的文字，最多各 30 项。
- `status`：`ok`、`stale`、`missing`、`invalid`；`size_changed` 表示截图尺寸变化。
- `text_status`：`available`、`unchanged`、`unavailable`、`disabled`、`timeout`、`busy`、`error` 或 `skipped`。

OCR 反馈最多等待 250ms；冷启动或较慢机器可能返回 `timeout`，后台任务可继续暖机。
没有可靠文字差异时列表为 `null`，不能当作“没有变化”。完全相同的截图无需 OCR，返回空列表。
action/batch 可设置 `ocr_diff:false` 只算像素变化，或 `include_diff:false` 关闭整个摘要。
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
