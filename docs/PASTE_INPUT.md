# 中文与数字粘贴输入

USB/BLE HID 只能发送键盘、鼠标事件，**不能直接设置 iPhone 剪贴板**。现有固件支持 `key cmd+v`；正文仍须由 iPhone 快捷指令或前台 App 写入剪贴板。

## 输入模式与路线

`Phone.type(text, mode="auto")`、`POST /api/type`、控制接口的 `type`、MCP `iphone_type` 和 CLI `type --mode` 支持：

| 模式 | 行为 |
| --- | --- |
| `auto`（默认） | 配置剪贴板通道后中文、ASCII、数字都粘贴；未配置时分块发送 ASCII（含数字）。 |
| `paste` | 必须配置通道；确认复制和焦点后发一次 `key cmd+v`，确认文字已出现后才完成请求。 |
| `keys` | 强制分块发送可打印 ASCII；非 ASCII 提示配置剪贴板通道。 |

限 **1–4000 字符、单行文本**，不接受换行或控制字符，不自动发送消息。粘贴失败不会自动改成逐键重试。**一个 batch 最多包含一个粘贴步骤**；多个粘贴步骤在执行前整批拒绝。逐键步骤不受此限制。

推荐顺序：

1. **已实现：服务端组合键触发 iOS 快捷指令。** Windows、Linux、Mac 均可作服务器。先把 iPhone「全键盘控制」的组合键绑定到下述快捷指令，再设置 `HIDAGENT_CLIPBOARD_TRIGGER_KEY`。服务端发布文本后发送该 HID 组合键；手机领取、复制，用户确认目标焦点，服务端才粘贴。用户看到文字出现后运行完成确认快捷指令。这是有人工确认的流程，不是无人值守焦点检测。
2. **已实现：手动触发同一快捷指令。** 不设置触发组合键时，在请求等待期间用轻点背面或辅助触控启动。确认协议相同。
3. **未交付 App：前台伴侣应用接入下述协议。** App 必须自行实现 `UIPasteboard` 写入、恢复目标焦点及结果确认；不能假设 iOS 允许后台常驻写剪贴板。
4. **未实现：Mac 通用剪贴板 / iPhone 镜像。** 要求同一 Apple 账户、Handoff、Wi-Fi 与蓝牙等条件；Windows 无等价通道。`pbcopy` 成功不能证明 iPhone 已收到，故没有自动回退。

## 服务器配置

iPhone 与服务器须在同一可互通局域网，快捷指令须允许获取服务器 URL。`127.0.0.1` 是手机自己；将下例 `computer.local` 替换成手机能访问的电脑主机名或局域网地址。

```sh
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
export HIDAGENT_CLIPBOARD_KEY='替换为上面生成的密钥'
export HIDAGENT_CLIPBOARD_BASE_URL='http://computer.local:8890'
export HIDAGENT_CLIPBOARD_TRIGGER_KEY='ctrl+alt+p'
export HIDAGENT_CLIPBOARD_TTL=60
export HOST=0.0.0.0
# 包括手机端确认时间，须比 TTL 至少多 5 秒。
export IPHONE_AGENT_TIMEOUT=70
./scripts/run-local.sh
```

PowerShell 用 `$env:HIDAGENT_CLIPBOARD_KEY='…'` 等设置同名变量。密钥为 32–256 位随机字母、数字、下划线或减号；TTL 默认 60 秒，可设 1–300 秒，覆盖领取、焦点确认和粘贴完成确认的全程。`IPHONE_AGENT_TIMEOUT` 在 CLI/MCP 进程中设置。省略 `HIDAGENT_CLIPBOARD_TRIGGER_KEY` 即手动触发模式；该组合键必须与手机设置一致，不会自动创建快捷指令或绑定。

直连须让服务监听局域网并放行端口。**完整控制 API 没有身份验证，优先保留本机监听，由专用反向代理仅开放 `/api/clipboard/`**，把 BASE_URL 改成代理地址。不要映射到公网；HTTP 仅用于可信局域网，需要保密时用手机信任证书的 HTTPS。

## 快捷指令一：「领取待粘贴文本」

先完成首次网络权限授权。JSON 的确认字段必须选「布尔值」类型，不能写成字符串 `"true"`。动作顺序如下：

1. 添加「URL」：`http://computer.local:8890/api/clipboard/pending`。
2. 「获取 URL 内容」：方法 **GET**，标头 `Authorization` = `Bearer 你的密钥`（有一个空格）。把结果转为词典，设为变量 **任务**。
3. 读取 **任务** 的 `text_url`，设为 **文本地址**；若无值，停止快捷指令。没有待办或已领取时响应为 `{"pending":false}`，此时不可复制或确认。
4. 读取 **任务** 的 `ack_url`，设为 **确认地址**。单独保存变量，避免被下一步正文覆盖。
5. 「获取 URL 内容」：URL 为 **文本地址**，方法 **GET**；领取失败立即停止，不重试。将返回结果作为文本交给「拷贝至剪贴板」，如有「仅本地」选项可打开。不要用数字动作，避免手机号前导零丢失；不要复制任务词典。
6. 「显示提醒」：“已复制；确认目标 App 的输入框仍有焦点，继续后立即粘贴”，开启取消按钮。**只有复制成功，底下仍是目标 App 且输入框保持焦点时才继续**。若跳到了快捷指令 App、有未处理权限弹窗或焦点不明，取消，恢复输入框后重新发起请求。此条件由人确认，服务端不会从 HTTP 下载成功推断焦点。
7. 「获取 URL 内容」：URL 为 **确认地址**，方法 **POST**，正文 **JSON**，添加布尔字段 `copied` = `true`、`focused` = `true`。随后结束，不添加展示结果、打开 App 或导航动作。服务器收到确认后留 0.5 秒给请求与运行浮层退出，再发送一次 `key cmd+v`；此缓冲不证明焦点或粘贴已完成。

复制和提醒须在当前 App 的输入框上方运行。快捷指令运行浮层能否正确退出、焦点是否保留，需要用本机短文本验收；若当前 iOS 的运行方式会抢走焦点，不要确认，也不要启用自动触发。

## 绑定服务端触发组合键

1. 在 iPhone「设置 → 辅助功能 → 键盘（或键盘与键入）→ 全键盘控制」开启此功能。
2. 进入「命令」，找到「快捷指令」下的「领取待粘贴文本」，录入 **Control + Option（Alt）+ P**，与 `ctrl+alt+p` 一致。可用外接键盘录入，或在录入界面从服务器手动发送一次现有 `key ctrl+alt+p` 命令。选择未占用的组合键，避免覆盖编辑键或 `cmd+v`。
3. 在普通可编辑文本框聚焦后试运行。服务端发起 `type` 会先发布待办，再发送组合键，由 iOS 执行上面的领取指令。若菜单中没有该快捷指令或组合键不能唤起它，先检查绑定；不要把键盘 ACK 当作快捷指令执行成功。本项目尚未在 iOS 26 真机验证这项设置。

也可在「设置 → 辅助功能 → 触控 → 轻点背面」或「辅助触控 → 自定操作/自定顶层菜单」绑定快捷指令；不配置触发组合键即可采用手动路线。

## 快捷指令二：「确认粘贴完成」

**只有看见本次文字确实出现在目标输入框后**才运行此指令。可绑定到另一个轻点背面/辅助触控操作。它不修改剪贴板，也不发送粘贴键。

1. 「获取 URL 内容」：GET `http://computer.local:8890/api/clipboard/current`，标头同上 `Authorization: Bearer 你的密钥`，将结果存为词典 **当前任务**。
2. 若 `active` 不是布尔 `true`，停止；若 `paste_sent` 不是布尔 `true`，也停止。没有进行中的任务时不能确认下一次输入。
3. 从 **当前任务** 保存 `complete_url` 为 **完成地址**。
4. 「显示提醒」：“已看到本次文字完整出现在目标输入框，确认完成？”，开启取消按钮。未出现、内容不对或只看到板子 ACK 时取消。
5. 「获取 URL 内容」：POST **完成地址**，JSON 布尔字段 `pasted` = `true`，结束。

完成确认后，原来的 HTTP/CLI 输入请求才结束，下一次复制才获准进行。**固定等待、键盘 ACK 或截图到达都不等于粘贴完成**；不需要也不要恢复旧剪贴板。若你另外使用其它 App 修改系统剪贴板，本服务无法替你锁住它。

使用示例：

```sh
iphone-agent type '你好，订单号0013267179555' --mode paste
```

也可 POST `/api/type`，JSON `{"text":"你好00123","mode":"paste"}`；`/api/control/action` 的 `type` 动作同样支持 `mode`。首次跨 App 粘贴可能弹出「允许粘贴」，请先允许并验证短文本。**Phone 拨号键盘未必是可粘贴文本框**，不能承诺 `cmd+v` 能填入电话号码。

## 协议、取消与恢复

| 请求 | 用途 |
| --- | --- |
| GET `/api/clipboard/pending`，Bearer 密钥 | 返回一次待领取任务的 `text_url`、`ack_url`、`status_url`、`complete_url`、剩余秒数；已领取或空闲时 `pending:false`。 |
| GET `text_url` | UTF-8 纯文本，一次性领取；成功后清除服务端正文。 |
| POST `ack_url`，`{"copied":true,"focused":true}` | 复制及目标焦点确认；只下载内容不够。 |
| GET `status_url` | 查询 `paste_sent`；这只是快捷键发送状态，不是输入效果证明。 |
| GET `/api/clipboard/current`，Bearer 密钥 | 返回进行中任务的状态及完成地址；无任务时返回 `active:false` 和 `needs_resolution`。 |
| POST `complete_url`，`{"pasted":true}` | 确认本次文字已进入目标框，结束请求；发送粘贴键之前拒绝。 |
| POST `/api/clipboard/resolve`，Bearer 密钥，`{"paste_finished":true}` | 只在无活动任务时解除阻塞；须先确认旧快捷指令已结束，且前次粘贴（如有）已经结束。 |

令牌随机、到期失效，正文和 ACK 只接受一次。`/api/type`、`/api/control/action`、`/api/control/batch` 调用方断连会取消并等待输入操作清理，旧令牌随后不可再领取或确认。按停止、超时或取消也会清除正文及令牌。尚未领取时只清理令牌；已领取后发生上述情况则还会阻止新复制，因为旧快捷指令可能仍握有正文并稍后写入剪贴板。

若正文已被领取但未完成整次交接，或 `cmd+v` 可能已发出却没有完成确认，系统会保持阻塞，**到期不会自动允许下一次复制**。先停止或等待旧快捷指令彻底结束，避免它稍后写入旧正文；再查看目标框、处理权限弹窗并确认前次粘贴（如有）已结束，最后调用 `/api/clipboard/resolve`。不能仅为消除报错而确认，也不要盲目重新发送。正常等待期间应使用完成确认，不能用 resolve 绕过。重启会清空内存状态，重启前后同样须核对是否存在未结束的粘贴。

剪贴板通道不记录正文。密钥与令牌 URL 都是秘密；标准启动入口配置密钥后关闭 Uvicorn 访问日志，直接运行时加 `--no-access-log`，代理也须关闭该路径日志或脱敏。保持 `HIDAGENT_DEBUG=0`，原 Agent 调试追踪可能保存提示、回复和截图。当前仅支持单进程内存，不能跨 worker 分流。

实现见 [clipboard.py](../server/hidagent/clipboard.py) 与 [phone.py](../server/hidagent/phone.py)。自动化测试仅使用假设备和 ASGI；组合键绑定、手机权限、浮层退出、焦点与实际粘贴速度均待真机验收。
