# 用 MCP 或 CLI 控制 iPhone

MCP 与 CLI 共用本机 HTTP 服务和 ESP32 HID 执行层。宿主 AI 自己看截图、选择目标、调用动作并核对结果；这条路径不调用服务端配置的 DeepSeek，也不需要另配服务端模型 Key。宿主 AI 自身的计费仍由宿主决定。

## 连接与能力检查

启动项目服务后，可在仓库根目录使用下面的完整入口，无需先激活虚拟环境：

```sh
./server/.venv/bin/python -m hidagent.cli status
./server/.venv/bin/python -m hidagent.cli capabilities
./server/.venv/bin/python -m hidagent.cli observe /tmp/iphone-before.jpg
./server/.venv/bin/python -m hidagent.cli --url http://127.0.0.1:8890 mcp
```

后文的 `iphone-agent` 简写假定已经激活 `server/.venv`；未激活时，继续替换为 `./server/.venv/bin/python -m hidagent.cli`。`mcp` 启动的是 stdio 工具服务，通常由宿主 Agent 启动；它不会替你启动 8890 网页控制服务。

`status` 表示当前板子、镜像、校准和任务状态。`capabilities` 表示可用动作、参数限制、剪贴板等可选功能是否配置，以及不支持的能力。支持某个动作不等于此刻已连接或校准，也不等于真机任务已验收。

使用 MCP 的宿主可启动 stdio 服务：

```sh
iphone-agent --url http://127.0.0.1:8890 mcp
```

例如 Codex 的 MCP 配置可使用虚拟环境 Python，路径替换为实际仓库位置：

```toml
[mcp_servers.iphone]
command = "/absolute/path/to/repo/server/.venv/bin/python"
args = ["-m", "hidagent.cli", "--url", "http://127.0.0.1:8890", "mcp"]
```

同一个任务保持同一 MCP 会话。只允许一个控制者：不要同时运行网页自主任务和宿主 AI 的直接操作。`stop_task` / `iphone-agent stop` 可以停止网页任务；停止后重新观察再操作。

## 观察与执行

推荐顺序是 `iphone_observe` → 看图 → 单次动作 → 看动作返回的图 → 使用新 `screen_id` 继续。CLI 同样执行这个循环：

```sh
iphone-agent observe /tmp/iphone-before.jpg
iphone-agent tap 0.5 0.4 --screen-id "实际返回的ID" \
  --target-box 0.4 0.3 0.6 0.5 --image-output /tmp/iphone-after.jpg
```

以上坐标仅展示语法，实际位置必须从最新图像确定。

- `observe` 同时返回截图、图像尺寸和有效期 30 秒的一次性 `screen_id`。
- 对普通单步动作，这 30 秒是首次实际 HID 写入的准入期限，包含为刷新静止镜像而执行的微移；排队到首写时已过期仍会拒绝。动作一旦开始，后续继续检查原设备会话、画面与目标，并受从请求进入起计算的同一 45 秒操作期限约束，不续期、不重签原凭证。原地宿主单击、批量动作与剪贴板阶段保留各自的期限规则。
- 指定输出文件时，JSON 中的 `image_path` 和 `screen_id` 属于同一张图；未指定则保留 `image_base64`。
- 动作带 `--image-output` 时同样保存其返回的新图，并在 JSON 中保留新的 `screen_id`、差异和验证结果。
- 在图像仍新鲜时，直接用动作返回的图和新 ID；不必每次另调 `observe`。
- 凭证过期、动作失败、用户改变画面或没有新图时，重新观察。不要重放已经可能完成的发送操作。
- `command_acknowledged` 仅证明指令收到；`effect_verified: false` 不能被当作成功。即使像素有变化，宿主仍要检查是否达成用户目标。

动作返回的可用 `screen_id` 绑定当前同一镜像会话中、经过动作后镜像延迟并已稳定的画面，不直接复用点击后第一张变化图。`changed` / `screen_diff` 描述返回图与动作前图的差异；`screen_changed` / `effect_observed` 保留动作执行时观察到的效果事实，`effect_frame_received_at` 是那张效果图的接收时间，不能据此推断目标已完成。等待中断、画面持续变化或原操作预算不足时，`fresh` 为 `false` 且 `screen_id` 为空，需重新观察，不能自动重复可能已执行的动作。

坐标默认是 `0..1` 的屏幕比例。CLI `--coordinate-space pixels` 和 MCP `coordinate_space="pixels"` 可以传观察图的像素坐标。禁止混用 `0..1000`；`target_box` 和 OCR `region` 始终是比例坐标 `[left, top, right, bottom]`。

## MCP 与 CLI 对照

表中的动作 CLI 都使用 `--screen-id`。完整参数用 `iphone-agent 命令 --help` 查看。

| MCP 工具 | CLI 命令 | 用途 |
|---|---|---|
| `iphone_status` | `status` | 当前连接和就绪状态 |
| `iphone_capabilities` | `capabilities` | 支持的能力和限制 |
| `iphone_observe` / `iphone_screenshot` | `observe [图片路径]` | 新图与一次性凭证 |
| `iphone_point` | `point x y` | 移动指针，不点击 |
| `iphone_tap` | `tap x y` | 校验落点后单击 |
| `iphone_click_observed` | `click-observed pointer_x pointer_y --target-box l t r b` | 宿主看清当前指针后原地单击，不移动指针 |
| `iphone_find_text` | `find-text 文字` | OCR 查找，不消费凭证 |
| `iphone_tap_text` | `tap-text 文字` | OCR 定位并校验点击 |
| `iphone_swipe` | `swipe x1 y1 x2 y2` | 单指拖动 |
| `iphone_scroll` | `scroll x y lines` | 滚轮式滚动 |
| `iphone_path` | `path '[[x,y],[x,y],…]'` | 连续路径，2..256 点 |
| `iphone_circle` | `circle x y radius_x radius_y` | 闭合椭圆，12..128 段 |
| `iphone_long_press` | `long-press x y --duration 0.8` | 按住 0.3..3 秒后松开 |
| `iphone_double_tap` | `double-tap x y` | 对已观察目标双击一次 |
| `iphone_search_app` | `search-app Freeform` | 打开 Spotlight 并输入 ASCII 应用搜索名 |
| `iphone_type` | `type 文本 --mode auto` | 兼容输入；粘贴模式可包含换行和制表符 |
| `iphone_paste_prepare` | `paste-prepare 文本 --screen-id ID --focused` | 发布复制任务，保留原输入框证据 |
| `iphone_paste_status` | `paste-status 事务ID` | 查看实际复制 ACK 与当前图 |
| `iphone_paste` | `paste 事务ID --screen-id ID --focused` | 校验原输入上下文后仅粘贴一次 |
| `iphone_paste_confirm` | `paste-confirm 事务ID --screen-id ID --observed-text 完整实见文本 --confirmed` | 宿主看图确认全文；不是服务端自动证明 |
| `iphone_paste_cancel` | `paste-cancel 事务ID` | 取消待完成事务并清除服务端文本 |
| `iphone_compose_chinese` | `compose-chinese 你好 --input-mode pinyin` | 输入拼音，返回候选图 |
| `iphone_key` | `key backspace --repeat 3` | 按键；仅退格/左右方向允许重复 |
| `iphone_home` / `iphone_back` | `home` / `back` | 返回主屏幕或上一页，须核对结果 |
| `iphone_calibrate` | `calibrate [--pointer x y]` | 启动或完成校准 |
| `iphone_batch` | `batch '[动作对象,…]'` | 有条件执行 1..8 个动作 |
| `task_status` / `stop_task` | `task-status` / `stop` | 网页任务状态和停止 |

`iphone_layouts`、`iphone_layout_tap` 和 `skill_*` 是已有的布局/工作流辅助 MCP 工具，不代表系统级 UI 树或无障碍访问权限。

`path`、`circle` 都会按住指针按钮拖动。`hold` 控制松开前是否短暂停留以减少惯性，关闭它仍会画线。只移动指针使用 `point`。服务端校验起点，后续根据该起点发送相对移动。iOS 对不同速度的 HID 移动响应可能不同，实际尺寸与落点以返回画面为准。归一化坐标中，圆的纵向半径为横向半径乘以 `图宽/图高`，像素坐标中两半径相等。绘图前确认画布和画笔，每笔结束后检查实际线条。

`search-app` 返回的是搜索结果，**不会自行选择结果，也不会证明应用已打开**。宿主应从结果图识别应用，用新凭证单独点击，再检查应用画面。双击不能用于发送控件；不要自动重试可能产生重复副作用的动作。

## 宿主观察后的原地单击

当宿主已经从最新截图中明确看到当前圆形指针位于目标内部，可显式调用 `iphone_click_observed`，或使用独立 CLI：

```sh
iphone-agent observe /tmp/iphone-pointer.jpg --for-click
iphone-agent click-observed 0.5 0.4 --screen-id "实际返回的ID" \
  --target-box 0.4 0.3 0.6 0.5 --target-label 主屏幕 \
  --image-output /tmp/iphone-clicked.jpg
```

坐标仅展示语法。`pointer_x/pointer_y` 必须来自这张图中实际看到的指针，严格位于可见目标的 `target_box` 内，全部采用 `0..1` 比例。不要把上次要求移动到的坐标当作指针实测位置，也不要在自动指针识别失败后无条件调用它。

点击前使用 `iphone_observe(for_click=true)`、CLI `observe --for-click` 或 HTTP `GET /api/control/observe?for_click=true`。这会最多被动等待 3.5 秒，直到真实新帧晚于设备连接和最后一次 HID 输入的完整镜像延迟，并通过新鲜度检查，再签发一次性凭证；等待过程不会校准或微移指针。后台防锁屏的微移会从观察开始最多暂停 30 秒，等待也计入这段时间；设备心跳和状态读取继续。凭证使用完、取消或被新观察覆盖会释放这次暂停，不会更改已有输入时间或帧时间。默认观察行为不变。

这条独立接口 `POST /api/control/host-click` 只在当前位置点击一次，不校准、不微移、不拖动，也不要求已校准。专用凭证允许宿主在 30 秒有效期内查看原图，即使查看已超过 5 秒；实际点击时，当前来源仍必须有 5 秒内的新帧、原设备会话及输入序号未变。

图像不再要求 JPEG 字节逐字节相同：尺寸必须一致，全图仍须通过严格的页面一致性校验；宿主所见指针周围另用**原生分辨率 RGB** 检查极小压缩差异，不缩放、不估计新的指针位置。检查区域是以观测坐标为中心的方形，半径为图宽的 6.4% 向上取整且至少 32 像素；区域内任一 RGB 通道差不能超过 5，全部通道平均差不能超过 0.15（通道范围 0–255）。这些容差只处理轻微编码噪声，不是服务端确认指针位置或证明指针完全没移动；落点仍依赖宿主对原图的明确观察，点击效果仍须看返回图。

若当前帧暂旧，最多被动等待 3.5 秒；等待中一旦看到超过上述范围的变化立即拒绝，不等它变回原样。每次都与原图比较，实际写入前再次检查；不提供两次取样之间未观察到的画面变化历史。凭证超时、其他输入、重连或已观察到的不允许变化都会使本次点击失败。压缩变化超过容差也需要重新观察。普通观察凭证仍采用更保守的原图新鲜度校验。

请求可带最多 120 字符的 `target_label`。点击发送控件时，宿主必须已获得用户发送授权，并提供图中实际可见的发送标签，例如 `发送` 或 `Send`，以沿用现有发送间隔和额度限制。服务端不能仅凭任意坐标自动识别发送按钮。剪贴板互斥、熔断、取消和操作期限仍然生效。

返回 `evidence_source: "host_observed"`、`pointer_seen: false` 和 `effect_verified: false`，明确区分宿主观察与服务端自动指针识别。宿主必须查看返回画面确认效果；没有动作后的新画面就不会获得下一张可用凭证。该能力不加入模型自动动作或 `batch`，不支持重复、长按或双击。ACK 未知或点击可能已执行时，不要自动重试。

## 通用 JSON 与批次

CLI `action` 对应 `/api/control/action`，可以传完整动作对象，适合脚本与新增参数。支持命令行 JSON、`@文件路径` 或 `-` 从标准输入读取：

```sh
iphone-agent action @next-action.json --image-output /tmp/iphone-next.jpg
```

例如 `next-action.json` 的结构：

```json
{
  "action": "path",
  "screen_id": "实际返回的ID",
  "coordinate_space": "normalized",
  "points": [[0.3, 0.5], [0.5, 0.6], [0.7, 0.5]],
  "hold": true
}
```

`batch` 的 JSON 是动作数组，外层凭证由 `--screen-id` 提供。数组和路径坐标也支持 `@文件路径` / 标准输入。服务端会在未验证的点击、缺少新鲜画面、页面变化等边界停止批次；文字输入、候选选择、发送需要观察后的独立决策。批次停止后应读取返回状态与图像，不要把整个数组自动重试。

旧 CLI 的 `tap/swipe/scroll/type/key/home/back` 不带凭证仍保留兼容旧路由。新自动化应使用带凭证形式；`--target-box`、像素坐标、重复按键、输入意图和动作图片输出都要求凭证，不能静默降级。旧 `screenshot 文件` 仅保存预览图，不生成动作凭证。

## 中文输入与模型委托

普通蓝牙 HID 没有直接设置 iPhone 剪贴板的通道。快速粘贴依赖已配置的 iOS Shortcut/companion，检查 `capabilities` 的 clipboard 项并按 [PASTE_INPUT.md](PASTE_INPUT.md) 配置与验收。

宿主 AI 优先使用分阶段 `paste-prepare` → `paste-status` → 看图确认原输入框 → `paste` → 看图核对全文 → `paste-confirm`。准备与复制期间不会长期占用观察锁。Shortcut 只确认实际复制，宿主确认焦点及插入文本，两者不互相代替。首次导入、设置快捷键及系统权限是一次配置；新模板没有每次粘贴的人为确认弹窗。`iphone_type` 的旧同步粘贴方式仍为兼容保留。

新模板对应 `HIDAGENT_CLIPBOARD_PROTOCOL=staged-v1`：普通 `type --mode auto` / `iphone_type(mode="auto")` 的可打印 ASCII 仍走键盘；Unicode/多行文本与显式同步 `mode="paste"` 立即返回分阶段指引，不能用新模板等待旧同步完成。默认 `legacy-v1` 不改变旧用户行为；查看 capabilities 中的协议、同步/分阶段可用性及触发键配置，不能把 `configured:true` 当成手机流程已验收。

没有伴随剪贴板时，可以在图中确认拼音键盘后用 `compose-chinese`。工具只输入拼音，不选候选、不提交、不发送；宿主需要核对候选图，用新凭证选词，再验证最终中文。不能把“输入了拼音”计作中文已正确输入。

不能假定空格或数字键一定选中候选；实际行为受当前输入法和「全键盘控制」影响。以新画面中的可见候选为依据点击，再检查输入框中的中文。

逐键输入已收到固件 ACK、但随后的原输入框验证失败时，响应保留 `command_acknowledged: true`，同时返回 `error`、`requires_verification: true` 和 `input_verified: false`。`typing_progress` 列出已确认的分块数、总块数及 ASCII 字符数；`all_chunks_acknowledged` 只说明传输是否完整，不证明文字已经正确落入目标。剩余分块和 batch 后续动作立即停止。未知或否定 ACK 不会被提升为成功；应观察当前内容，不要重新输入整段造成重复。

`iphone-agent task "任务"` 是可选的**服务端模型委托**，仍使用网页配置的模型。MCP 默认不暴露 `delegate_task`；只有显式设置 `IPHONE_MCP_ENABLE_DELEGATE=1` 才会出现。正常 MCP 看图控制不依赖这条委托路径。

## 已知边界与验收

当前接口是单指针 HID，不支持双指捏合或真正多点触控。不能从“提供了 MCP 工具”推断任意 iOS 页面、权限弹窗或受保护内容都能自动完成。镜像建立、首次系统授权和实际支持范围仍受设备与 iOS 状态影响。

接口测试覆盖 CLI 参数、真实 stdio MCP 协议、凭证传递、返回截图和错误传播；这些测试使用模拟 HTTP/图片，不证明真机点击成功。实际可用性还需真机连续验证：启动应用、多步导航、中文候选或粘贴、返回主屏幕、绘图，以及画面变化/断连后的恢复。
