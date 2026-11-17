# 中文与数字粘贴输入

USB/BLE HID 只能发送键盘、鼠标事件，**不能直接设置 iPhone 剪贴板**。现有固件支持 `key cmd+v`；正文仍须由 iPhone 快捷指令或前台 App 写入剪贴板。

## 宿主 AI 推荐路线：分阶段粘贴

现在提供 `prepare → 查看复制状态与画面 → paste → 查看完整输入结果 → confirm`，每阶段返回后释放控制锁，AI 可以继续观察。正常使用不再需要下面旧流程里的两次人工提醒。首次安装快捷指令、设置配对信息、允许网络访问及绑定触发键仍是独立配置环节；iOS 是否再次弹出权限提示取决于实际权限和网络地址。

**使用本仓库生成的新模板时，服务端须设置 `HIDAGENT_CLIPBOARD_PROTOCOL=staged-v1`。** 这样普通 `type` 的 `auto` 模式仍将可打印 ASCII/数字作为键盘事件输入；中文、Unicode、换行文本以及显式 `type --mode paste` 会立即提示使用下列分阶段接口，不发布旧协议任务、不等待 TTL，也不发送触发键。新模板不会处理旧同步协议，不能仅配置 URL/Key 后就让普通 `auto` 输入走旧同步粘贴。

`capabilities.features.clipboard` 区分 `configured`、`protocol`、`legacy_paste_available`、`staged_available` 与 `trigger_configured`。这些是服务端配置/接口能力；不证明 iPhone 已导入模板、绑定按键或实际粘贴成功。触发键未配置时，prepare 仍可发布待办，但需要由宿主按实际已安装的入口运行快捷指令。

| MCP 工具 | CLI | 行为 |
| --- | --- | --- |
| `iphone_paste_prepare` | `paste-prepare 文本 --screen-id ID --focused` | 保留原输入画面，发布一次性任务并可选触发快捷指令；尚未粘贴。 |
| `iphone_paste_status` | `paste-status 事务ID` | 返回复制阶段、当前图和新的观察凭证；不会确认输入成功。 |
| `iphone_paste` | `paste 事务ID --screen-id ID --focused` | 实际复制完成、原输入上下文复查通过后，只发送一次 Cmd+V，返回新图。 |
| `iphone_paste_confirm` | `paste-confirm 事务ID --screen-id ID --observed-text 完整文字 --confirmed` | 宿主已经看清文字完整进入原输入框后确认，结束事务。 |
| `iphone_paste_cancel` | `paste-cancel 事务ID` | 作废正文和事务凭证。已领取或粘贴结果不明时仍须核对旧任务后解除阻止。 |

这些工具使用 `/api/control/clipboard/{prepare,status,paste,confirm,cancel}`，只在本机控制端口提供。不要将它们开放到局域网代理。CLI 可加 `--image-output 文件` 保存阶段返回图片。

`focused=true` / `--focused` 是宿主根据当前截图作出的明确判断。服务器优先寻找有蓝色插入光标的有界输入框；若无法识别，prepare 必须同时提供宿主观察到的归一化 `target_box=[left,top,right,bottom]`。后者返回 `focus_verified_by=host_observation`，不会冒充自动识别。不能仅传 `focused:true` 而不给无边框编辑器的区域。

快捷指令只在真实执行 Copy 后回报 `copied:true`，不回报 `focused` 或 `pasted`。真正的 Cmd+V 要求所选观察和当前帧都晚于复制 ACK，并经过镜像传输延迟余量，不能复用 prepare 时的旧图。原画面、设备连接和镜像代次仍须一致，并在串口写入前再次校验。等待确认期间禁止其它键盘输入或第二次复制覆盖当前事务；仍允许观察和用指针处理界面。确认同样要求粘贴后的新图，只允许原输入区域内发生变化，并要求完整 `observed_text` 与预期文本一致。返回 `verified_by=host_observation, server_content_verified=false`，明确是宿主视觉核验，不是像素改变或键盘 ACK 自动证明。

紧急停止会立即作废待处理的分阶段正文与凭证。连接或镜像会话改变后，旧 payload、复制 ACK 和宿主事务在访问前均撤销；已领取的剪贴板仍保留结果不明标记，不能误认清理服务端正文也清除了 iPhone 剪贴板。prepare / paste 各自受动作超时约束，超时后不得继续发出迟到按键。

阶段调用之间不要盲目固定等待后直接粘贴。AI 应读取 `phase`：`awaiting_copy` 仍需等待 Shortcut，`awaiting_paste` 可以核对焦点，`awaiting_confirmation` 需要核对实际文字。若弹出系统权限、运行浮层或页面跳转，先处理并恢复原目标；状态不明时取消，不能重发 Cmd+V。正文默认 60 秒到期，首次安装先完成授权再发布真实输入事务。

## 安装不含秘密的快捷指令模板

构建器只生成模板，不读取环境密钥、私人快捷指令或剪贴板。macOS 可同时调用系统工具签名：

```sh
python3 scripts/build_clipboard_shortcut.py \
  --output build/shortcuts/ESP32-Agent-Clipboard.shortcut --sign
```

输出 `ESP32-Agent-Clipboard-signed.shortcut` 可交给 iPhone 导入。Apple 签名会接收文件副本，因此模板只包含 `http://computer.local:8891` 占位和空配对 Key；**不要把真实地址/Key 先写入模板再签名**。导入设置会分别询问服务器地址和本机生成的剪贴板配对 Key，此 Key 与 DeepSeek Key 无关。设置答案保存在设备上，不进入仓库。可由已授权的配置流程填入；它不是每次粘贴都要填写的输入。

安装步骤：

1. 按下文配置本机服务和仅转发剪贴板的 8891 监听器，并设置 `HIDAGENT_CLIPBOARD_PROTOCOL=staged-v1`；受管服务可写入私密 `runtime.env`。
2. 在 iPhone 导入签名文件，按设置问题填服务器地址和配对 Key，确认快捷指令名称；可命名为 `ESP32 Agent Clipboard`。
3. 首次允许该快捷指令访问自己的剪贴板服务。若 iOS 提供“始终允许”，可选择它；不能伪造或绕过系统权限结果。
4. 将此快捷指令绑定到 `HIDAGENT_CLIPBOARD_TRIGGER_KEY` 指定的组合键，例如 `ctrl+alt+p`。绑定方法见下文，此处选择新模板的名称。
5. 在空白测试输入框中运行分阶段流程，验证复制、浮层退出、原焦点恢复和准确文字。不得仅凭文件签名成功宣称手机粘贴已验收。

模板动作只有：领取 staged-v1 待办 → 获取结构化正文 → 检查 `copy_allowed` → 按文本原样本地复制 → 发送复制 ACK → 结束。没有自定义提醒、没有发送动作，也不自动声称输入完成。没有待办、旧协议待办、下载错误或过期响应缺少 `copy_allowed` 时，不复制错误正文。

依据：[Apple 命令行签名说明](https://support.apple.com/guide/shortcuts-mac/apd455c82f02/mac)、[导入设置问题](https://support.apple.com/guide/shortcuts/apdf330fd3a0/ios)、[首次权限与始终允许](https://support.apple.com/guide/shortcuts/apd961a4fc65/ios)。生成器参考系统自带 Gallery `.wflow` 的 plist 结构；签名、导入兼容性和真机输入效果是不同的验收阶段。

## 输入模式与路线

默认 `HIDAGENT_CLIPBOARD_PROTOCOL=legacy-v1` 保留以下 `Phone.type` / `iphone_type` 的等待完成行为和两条旧快捷指令兼容性；现有用户无需改配置。新模板应显式选择 `staged-v1`，普通输入不会再错误等待旧版 `focused:true` / 完成确认。宿主 AI 自动控制请使用上面的分阶段工具。

`Phone.type(text, mode="auto")`、`POST /api/type`、控制接口的 `type`、MCP `iphone_type` 和 CLI `type --mode` 支持：

| 模式 | 行为 |
| --- | --- |
| `auto`（默认） | `legacy-v1` 通道配置后中文、ASCII、数字都同步粘贴；未配置或 `staged-v1` 时分块发送可打印 ASCII（含数字）。`staged-v1` 的其他文本立即提示分阶段接口。 |
| `paste` | 仅 `legacy-v1` 同步通道：确认复制和焦点后发一次 `key cmd+v`，确认文字已出现后才完成请求。`staged-v1` 立即拒绝此同步模式并指向分阶段工具。 |
| `keys` | 强制分块发送可打印 ASCII；非 ASCII 提示配置剪贴板通道。 |

限 **1–4000 字符**。选定粘贴模式时允许标准换行和制表符（`\n`、`\r`、`\t`），原样传递，不做空白或换行规范化；这些字符是剪贴板文本，不会转换成 Enter/Tab 按键。逐键输入仍只接受单行 ASCII；其他 C0/C1 控制字符、Unicode 行/段分隔符及非法代理字符在所有模式下均拒绝。输入不自动发送消息。粘贴失败不会自动改成逐键重试。**一个 batch 最多包含一个粘贴步骤**；多个粘贴步骤在执行前整批拒绝。此预检限制针对粘贴；批次执行后续打字或按键前也须重新观察并核对焦点，不能凭前一步 ACK 继续输入。

输入始终绑定开始时的画面：粘贴键发出前复查原画面、连接代次及新鲜度。逐键分块时，只允许原画面中带蓝色插入光标的有界输入框内部变化；无法识别输入框时，画面变化会停止后续分块。ACK 等待期间换页或断开连接也会停止，已写入的部分不会自动重放。

旧版同步输入支持以下触发方式：

1. **已实现：服务端组合键触发 iOS 快捷指令。** Windows、Linux、Mac 均可作服务器。先把 iPhone「全键盘控制」的组合键绑定到下述快捷指令，再设置 `HIDAGENT_CLIPBOARD_TRIGGER_KEY`。服务端发布文本后发送该 HID 组合键；手机领取、复制，用户确认目标焦点，服务端才粘贴。用户看到文字出现后运行完成确认快捷指令。这是有人工确认的流程，不是无人值守焦点检测。
2. **已实现：手动触发同一快捷指令。** 不设置触发组合键时，在请求等待期间用轻点背面或辅助触控启动。确认协议相同。
3. **未交付 App：前台伴侣应用接入下述协议。** App 必须自行实现 `UIPasteboard` 写入、恢复目标焦点及结果确认；不能假设 iOS 允许后台常驻写剪贴板。
4. **未实现：Mac 通用剪贴板 / iPhone 镜像。** 要求同一 Apple 账户、Handoff、Wi-Fi 与蓝牙等条件；Windows 无等价通道。`pbcopy` 成功不能证明 iPhone 已收到，故没有自动回退。

## 服务器配置

macOS / Linux 使用本仓库后台管理入口时，推荐把下列 `HIDAGENT_CLIPBOARD_*` 配置保存在数据目录的私密 `runtime.env`，然后运行 `./scripts/run-local.sh start`（已有 worker 用 `restart-worker`）。配对 URL 和 Key 同时存在时，后台管理进程自动启动专用剪贴板代理，并随 worker 一起重启/关闭；无需第二个终端。用 `status` 的 `clipboard_proxy` 核对监听状态，详细规则见 [RUNTIME.md](RUNTIME.md#受管剪贴板代理)。默认未配置不增加代理，使用外置代理时设置 `HIDAGENT_CLIPBOARD_MANAGED_PROXY=0`。

新生成模板另加 `HIDAGENT_CLIPBOARD_PROTOCOL=staged-v1`；下方旧版双快捷指令流程使用默认 `legacy-v1`。无效协议值在启动服务前拒绝。协议选择只决定是否提供旧同步粘贴；不会取消原会话、焦点、复制 ACK 或全文观察确认要求。

下面的两个终端方案保留给 Windows、无参数前台启动或外部管理代理的情况。不要在已启用受管代理的同一端口再手动启动第二个实例。

iPhone 与服务器须在同一可互通局域网，快捷指令须允许获取服务器 URL。`127.0.0.1` 是手机自己；将下例 `computer.local` 替换成手机能访问的电脑主机名或局域网地址。以下命令都在**仓库根目录**运行，使用安装步骤创建的 `server/.venv`，无需先激活环境。

1. 在终端一生成密钥，再配置并启动**本机控制服务**。复制生成的一整行密钥，替换下面的占位文字；两条 iPhone 快捷指令也使用同一密钥。

   **Mac / Linux：**

   ```sh
   ./server/.venv/bin/python -c 'import secrets; print(secrets.token_urlsafe(32))'
   export HIDAGENT_CLIPBOARD_KEY='替换为上面生成的密钥'
   export HIDAGENT_CLIPBOARD_BASE_URL='http://computer.local:8891'
   export HIDAGENT_CLIPBOARD_TRIGGER_KEY='ctrl+alt+p'
   export HIDAGENT_CLIPBOARD_TTL=60
   export HOST=127.0.0.1
   export PORT=8890
   ./scripts/run-local.sh
   ```

   **Windows 原生 PowerShell：**

   ```powershell
   .\server\.venv\Scripts\python.exe -c "import secrets; print(secrets.token_urlsafe(32))"
   $env:HIDAGENT_CLIPBOARD_KEY='替换为上面生成的密钥'
   $env:HIDAGENT_CLIPBOARD_BASE_URL='http://computer.local:8891'
   $env:HIDAGENT_CLIPBOARD_TRIGGER_KEY='ctrl+alt+p'
   $env:HIDAGENT_CLIPBOARD_TTL='60'
   $env:HOST='127.0.0.1'
   $env:PORT='8890'
   powershell -ExecutionPolicy Bypass -File .\scripts\run-server.ps1
   ```

2. 保持终端一运行。在终端二启动**只转发剪贴板请求的监听器**。它只向本机 `127.0.0.1:8890` 转发明确允许的剪贴板请求；截图、键鼠、输入、控制台和 API 文档等路径均不开放。它不另外启动手机控制服务，也不复制待办状态。

   **Mac / Linux：**

   ```sh
   ./server/.venv/bin/python -m hidagent.clipboard_proxy --host 0.0.0.0 --port 8891 --control-port 8890
   ```

   **Windows 原生 PowerShell：**

   ```powershell
   .\server\.venv\Scripts\python.exe -m hidagent.clipboard_proxy --host 0.0.0.0 --port 8891 --control-port 8890
   ```

3. 防火墙只需允许手机所在的**专用局域网**访问 TCP **8891**。本机控制台仍用 `http://127.0.0.1:8890`，不开放 8890 的入站规则。手机 URL 均使用 **8891**。不要将控制服务的 `HOST` 改为 `0.0.0.0`：完整控制 API 没有身份验证；配置剪贴板后，标准启动入口会拒绝这种绑定。若修改控制端口，须同步修改终端二的 `--control-port`；若修改剪贴板端口，须同步修改 BASE_URL 和手机 URL。

4. 密钥为 32–256 位随机字母、数字、下划线或减号。TTL 默认 60 秒，可设 1–300 秒，覆盖领取、焦点确认和粘贴完成确认的全程。省略 `HIDAGENT_CLIPBOARD_TRIGGER_KEY` 即手动触发模式。使用组合键时，在手机上完成下文的绑定。使用旧同步入口时，CLI/MCP 超时至少比 TTL 多 5 秒。

剪贴板监听器的配对与一次性令牌检查仍由本机服务执行，不会绕过确认步骤。不要映射到公网；HTTP 仅用于可信局域网，需要传输保密时再配置手机信任证书的 HTTPS。监听器关闭访问日志，避免一次性令牌 URL 留在日志中。

## 旧同步协议快捷指令一「领取待粘贴文本」

以下操作都在 **iPhone 的「快捷指令」App** 中完成，不是在电脑上，也不是在「设置」里写脚本。先记下两项：电脑地址（如 `http://computer.local:8891`）和上面生成的密钥。把示例里的地址替换成自己的；尖括号、引号不要复制到字段中。

1. 打开「快捷指令」App → 底部「快捷指令」→ 右上角「＋」。点顶部名称 →「重新命名」，输入 **领取待粘贴文本**。每次添加动作，在底部「搜索操作」中输入下文的动作名，再点搜索结果。
2. 添加 **「URL」** 动作，在地址框完整输入 `http://computer.local:8891/api/clipboard/pending`。
3. 添加 **「获取 URL 内容」**。URL 输入应是上一步的蓝色「URL」变量；若不是，点输入框 →「选择变量」→ 点上一步的输出。展开此动作的箭头/「显示更多」，把 **「方法」选为 GET**。在 **「标头」→「添加新字段」** 中，左边「键」输入 `Authorization`，右边「文本」输入 `Bearer 你的密钥`；`Bearer` 后必须有一个英文空格。
4. 添加 **「从输入中获取词典」**，输入选上一步「URL 内容」。再添加 **「设定变量」**，变量名输入 **任务**，输入选这个词典。后文的「任务」始终指这个变量，不能选最近一次网络请求的结果。
5. 添加 **「获取词典值」**：把模式选为「值」，键输入 `text_url`，词典输入选 **任务**。添加 **「设定变量」**，名称输入 **文本地址**，输入选刚取得的词典值。
6. 添加 **「如果」**，输入选 **文本地址**，条件选「没有任何值」。在「如果」和「否则」之间添加 **「停止此快捷指令」**（有些版本显示「停止并输出」，输出留空）；删掉空的「否则」分支。没有待办时服务器返回 `pending:false`，此时必须停止，不能复制旧值。
7. 在「结束如果」后添加 **「获取词典值」**：键输入 `ack_url`，词典明确选 **任务**。添加 **「设定变量」**，名称输入 **确认地址**，输入选刚取得的值。保存确认地址是为了防止它被下一次下载的正文覆盖。
8. 添加第二个 **「获取 URL 内容」**：URL 输入选 **文本地址**，「方法」选 **GET**；这个一次性地址不再添加 Authorization 标头。添加 **「文本」** 动作，内容只插入该请求的「URL 内容」变量，前后不要加字或换行。再添加 **「拷贝到剪贴板」**（部分版本叫「拷贝至剪贴板」），输入选「文本」；若显示「仅本地」，打开它。不要使用「数字」动作，否则 `00123` 的前导零可能丢失。下载失败应让快捷指令停止，不要重试领取。
9. 添加 **「显示提醒」**：正文输入 **已复制。请确认下面仍是目标 App，目标输入框仍有焦点；继续后将立即粘贴。** 展开选项并开启「显示取消按钮」。只有复制成功且焦点明确时才点继续/好。若已经进入「快捷指令」App、有未处理权限弹窗或焦点不明，点取消，恢复输入框后按文末的恢复步骤处理，再发起新请求。
10. 添加第三个 **「获取 URL 内容」**：URL 选 **确认地址**，「方法」选 **POST**，「请求正文」选 **JSON**。点「添加新字段」→ 选 **布尔值**，键输入 `copied`，值开关打开；再添加一个 **布尔值**字段，键输入 `focused`，值开关打开。两个字段都不能选「文本」，也不要输入带引号的 `"true"`。
11. 点「完成」保存。POST 后不要加「显示结果」「打开 App」或返回主屏幕动作。服务器收到确认后先留 0.5 秒给 HTTP 动作与运行浮层退出。通过观察凭据发起的手动模式会再等待最多 3.5 秒，让镜像显示新鲜的原输入画面，然后发一次 `key cmd+v`；等待仍受本次令牌到期时间限制。这段等待不代表焦点正确或文字已出现，仍需下面的完成确认。

首次运行若询问是否允许连接该电脑/获取 URL，核对地址后允许。**不要直接在「快捷指令」App 的编辑器里执行有待办的第一个测试**：那会把焦点留在编辑器。先完成下述键绑定，再在目标输入框上方触发。运行浮层能否退出并保持焦点仍须在自己的 iOS 上验收；不能保持时不要确认。

## 绑定服务端触发组合键

1. 打开 iPhone **「设置」→「辅助功能」→「键盘与键入」→「全键盘控制」**（部分 iOS 版本将「键盘与键入」显示为「键盘」），打开「全键盘控制」开关。此设置由机主确认，可由已授权的配置流程完成；不要为了测试改动借用手机的无关设置。
2. 进入同页 **「命令」**，向下找到 **「快捷指令」** 分组。选择已导入的 **ESP32 Agent Clipboard**，或旧同步协议的 **「领取待粘贴文本」**。在录入界面用连接到 iPhone 的键盘按 **Control + Option + P**（Windows 键盘上是 **Ctrl + Alt + P**），再保存。也可以从控制台发送一次 `key ctrl+alt+p`。手机的组合键与服务端 `HIDAGENT_CLIPBOARD_TRIGGER_KEY=ctrl+alt+p` 必须一致。
3. 若提示此键已有用途，换一个未占用的组合键，并同步修改服务器变量；不要覆盖 Return/Enter、普通编辑键或 `cmd+v`。若「快捷指令」分组未列出刚保存的指令，先退出并重新进入「命令」，确认指令已经保存在「快捷指令」App 中。iOS 26 的具体绑定和焦点行为尚待真机确认；找不到此入口时先使用下一段的手动方式，不要把键盘 ACK 当成运行成功。
4. 在目标 App 的普通可编辑文本框中点出光标，再发起对应协议的测试。分阶段模板以 `paste-status` 返回 `awaiting_paste` 确认复制成功，随后核对原输入框并执行 `paste`。旧同步快捷指令会显示「已复制」提醒，核对目标和焦点后按提醒继续。

手动方式可以省略 `HIDAGENT_CLIPBOARD_TRIGGER_KEY`，在「设置 → 辅助功能 → 触控 → 轻点背面 → 轻点两下/轻点三下」的「快捷指令」分组绑定所用的快捷指令，也可在「辅助触控 → 自定操作/自定顶层菜单」绑定。发布待办后再触发。

## 旧同步协议快捷指令二「确认粘贴完成」

这条指令不改剪贴板、不发粘贴键。**只有看见本次完整文字已经进入正确的输入框后才运行**；完成后服务器才能处理下一次复制。

1. 回到「快捷指令」App →「＋」→ 重新命名为 **确认粘贴完成**。
2. 添加「URL」，填 `http://computer.local:8891/api/clipboard/current`；添加「获取 URL 内容」，方法 **GET**，标头键 `Authorization`，值 `Bearer 你的密钥`。
3. 添加「从输入中获取词典」→「设定变量」，名称 **当前任务**。添加「获取词典值」，键 `active`、词典 **当前任务**；添加「如果」，条件为不是布尔真/未开启，在分支中「停止此快捷指令」。再对 **当前任务** 的 `paste_sent` 做同样检查。任一值不是真就停止，不得确认未来的一次输入。
4. 添加「获取词典值」，键 `complete_url`、词典 **当前任务** →「设定变量」，名称 **完成地址**。
5. 添加「显示提醒」，正文 **已看到本次完整文字出现在正确输入框，确认完成？**，开启「显示取消按钮」。文字没出现、内容不对或只有板子 ACK 时取消。
6. 添加「获取 URL 内容」，URL **完成地址**，方法 **POST**，请求正文 **JSON** →「添加新字段」→ **布尔值**，键 `pasted`，值开关打开。点「完成」保存。
7. 按上一节同样的方法，将它绑定到另一个未占用组合键（例如 `ctrl+alt+o`），或绑定另一个轻点背面/辅助触控操作。这个组合键由人按下，不要配置成服务器的领取触发键。

## 分阶段模板的第一次完整测试

1. 在 iPhone「备忘录」新建空白测试笔记并点出正文光标。用 `observe` 取得当前画面的凭证。保持镜像，记录实际可见的输入区域。
2. 用 `paste-prepare` 发布一段包含中文、换行和前导零的测试文本，例如两行「粘贴验收」「编号 00123」。传入 `--screen-id` 和 `--focused`。光标无法自动识别时，用 `--target-box` 提供你核对过的输入区域。没有绑定组合键时，按「绑定服务端触发组合键」中的手动方式触发。
3. 用 `paste-status` 等待 `awaiting_paste`。核对运行浮层已经退出、光标仍在原笔记，再用新的画面凭证执行一次 `paste`。若 iOS 首次询问是否允许粘贴，先在测试笔记中核对来源并处理提示，再观察结果。
4. 在新画面逐字核对两行，包括换行和 `00123`，再用 `paste-confirm` 确认。画面不明或内容有误时，先核对事务状态并按恢复流程处理。

关闭 iOS 的「允许粘贴」弹窗可能让镜像重新绘制。若原画面比对无法确认，启用本机 OCR 的服务会检查实际弹出的授权页，以及关闭后仍可识别的原输入框、周围文字和新出现的框内内容。此时 `paste-confirm` 仍要求新的画面凭证和宿主逐字确认的完整文本。通过后会返回 `confirmation_evidence=paste_permission_dismissed`。原事务只发送一次 Cmd+V。

## 旧同步协议的第一次完整测试

1. 在 iPhone「备忘录」新建一个仅用于测试的空白笔记，点正文使光标出现。先不要选真实群聊。确认电脑与 iPhone 在同一 Wi-Fi，服务器已启动。
2. 在电脑另开终端三，进入仓库根目录。两个服务终端保持运行。设置请求超时并通过虚拟环境运行 CLI，无需激活环境：

   **Mac / Linux：**

   ```sh
   export IPHONE_AGENT_TIMEOUT=70
   ./server/.venv/bin/iphone-agent type '你好，订单号0013267179555' --mode paste
   ```

   **Windows 原生 PowerShell：**

   ```powershell
   $env:IPHONE_AGENT_TIMEOUT='70'
   .\server\.venv\Scripts\iphone-agent.exe type '你好，订单号0013267179555' --mode paste
   ```

3. 手机应弹出「领取待粘贴文本」的提醒；核对底下仍是备忘录且输入光标在正文，继续。首次跨 App 粘贴若出现 **「允许粘贴」**，核对来源后允许。服务端此时仍在等待，属正常情况。
4. 看笔记是否逐字出现 **你好，订单号0013267179555**，特别检查中文和前导零。确定正确后运行「确认粘贴完成」。电脑请求应完成；无需按 Enter，也不要发送消息。
5. 再用不同文字测试一次，例如 `第二次测试ABC0099`，确认没有粘贴上一条旧文字。然后在自己选定的目标 App 的无害草稿中测试并保持未发送。**Phone 拨号键盘未必可粘贴**，不要用拨号作为此流程成功的唯一判断。

HTTP 用户也可 POST `/api/type`，JSON `{"text":"你好00123","mode":"paste"}`；控制动作 `type` 同样支持 `mode`。固定等待、键盘 ACK 或截图到达都不是粘贴完成证明；不要恢复旧剪贴板。其它 App 仍能修改系统剪贴板。

## 常见问题

| 现象 | 处理 |
| --- | --- |
| 「获取 URL 内容」无法连接 | 电脑和手机连同一 Wi-Fi；避开访客网络/客户端隔离。地址不能是 `127.0.0.1` 或 `localhost`。核对电脑局域网 IP、剪贴板端口 8891 和防火墙；终端二须保持运行。控制服务仍监听本机 8890，不要为解决连接问题将它开放到局域网。 |
| 返回 502/本机服务暂不可用 | 先检查终端一的控制服务是否仍在运行、是否监听 `127.0.0.1:8890`；检查终端二的 `--control-port` 是否一致。 |
| 返回 401/未授权 | 分阶段模板请核对导入时配置的配对 Key。旧同步协议请核对 pending/current 的 `Authorization` 标头，格式为 `Bearer`、一个英文空格、完整密钥。 |
| 分阶段一直停在 `awaiting_copy`，或旧同步协议没有提醒 | 核对待办仍有效、触发键与 iPhone 绑定一致、快捷指令已保存。也可按上文使用手动触发。 |
| 提示没有待办 | 请求未开始、已被领取或已过期。不要复制空词典或旧文字；查看原请求状态。 |
| 一次性 URL 已用过、令牌到期（404/409） | 默认 60 秒覆盖整个流程，可将 TTL 与客户端超时一起调长（TTL 最大 300 秒）。旧地址不可再用；如已领取但未完成，先按下节恢复，不能直接重试。 |
| 出现「允许粘贴」 | 先在测试笔记中处理并核对内容。该提示是否出现取决于当前 iOS/App，不保证每次或首次都会出现。不要在弹窗未处理时确认完成。 |
| 复制了但没出现文字 | 确认目标框可编辑、仍有焦点、浮层已退出。取消/停止并核对旧操作是否结束；不要连续发 `cmd+v`，以免重复插入。 |
| 提示「等待原输入画面恢复超时」 | 手动运行时，镜像中的提醒可能比手机晚消失，服务器会短暂等待。若仍超时，检查镜像连接和目标输入框；换了画面、重连或画面过旧都不会放行。先按下节核对并解除旧操作阻塞，再重新观察、发起输入。 |
| 请求一直等、下一次输入被阻塞 | 核对文字后使用当前协议的确认步骤。若前次已取消或超时，按下面的流程解除阻塞。 |

## 协议、取消与恢复

| 请求 | 用途 |
| --- | --- |
| GET `/api/clipboard/pending`，Bearer 密钥 | 返回一次待领取任务的 `text_url`、`ack_url`、`status_url`、`complete_url`、剩余秒数；已领取或空闲时 `pending:false`。 |
| GET `text_url` | UTF-8 纯文本，一次性领取；成功后清除服务端正文。 |
| GET `payload_url`（仅 staged-v1） | 一次性 JSON 正文，含 `copy_allowed:true,text`；失败响应不含 copy_allowed，快捷指令不得复制。 |
| POST `ack_url`，`{"copied":true,"focused":true}` | 复制及目标焦点确认；只下载内容不够。 |
| GET `status_url` | 查询 `paste_sent`；这只是快捷键发送状态，不是输入效果证明。 |
| GET `/api/clipboard/current`，Bearer 密钥 | 返回进行中任务的状态及完成地址；无任务时返回 `active:false` 和 `needs_resolution`。 |
| POST `complete_url`，`{"pasted":true}` | 确认本次文字已进入目标框，结束请求；发送粘贴键之前拒绝。 |
| POST `/api/clipboard/resolve`，Bearer 密钥，`{"paste_finished":true}` | 只在无活动任务时解除阻塞；须先确认旧快捷指令已结束，且前次粘贴（如有）已经结束。 |

staged-v1 的 `ack_url` 只要求 `{"copied":true}`；Shortcut 的 `complete_url` 在此模式明确拒绝调用。宿主专属事务 nonce 不会交给 Shortcut；完成必须通过本机分阶段确认接口。新旧协议共用同一待办槽，不能互相覆盖。

令牌随机、到期失效，正文和 ACK 只接受一次。`/api/type`、`/api/control/action`、`/api/control/batch` 调用方断连会取消并等待输入操作清理，旧令牌随后不可再领取或确认。按停止、超时或取消也会清除正文及令牌。尚未领取时只清理令牌；已领取后发生上述情况则还会阻止新复制，因为旧快捷指令可能仍握有正文并稍后写入剪贴板。

若正文已被领取但未完成整次交接，或 `cmd+v` 可能已发出却没有完成确认，系统会保持阻塞，**到期不会自动允许下一次复制**。先停止或等待旧快捷指令彻底结束，避免它稍后写入旧正文；再查看目标框、处理权限弹窗并确认前次粘贴（如有）已结束，最后调用 `/api/clipboard/resolve`。不能仅为消除报错而确认，也不要盲目重新发送。正常等待期间应使用完成确认，不能用 resolve 绕过。重启会清空内存状态，重启前后同样须核对是否存在未结束的粘贴。

剪贴板通道不记录正文。密钥与令牌 URL 都是秘密；标准启动入口配置密钥后关闭 Uvicorn 访问日志，直接运行时加 `--no-access-log`，代理也须关闭该路径日志或脱敏。保持 `HIDAGENT_DEBUG=0`，原 Agent 调试追踪可能保存提示、回复和截图。当前仅支持单进程内存，不能跨 worker 分流。

实现见 [clipboard.py](../server/hidagent/clipboard.py)、[clipboard_proxy.py](../server/hidagent/clipboard_proxy.py) 与 [phone.py](../server/hidagent/phone.py)。自动化测试仅使用假设备和 ASGI；组合键绑定、手机权限、浮层退出、焦点与实际粘贴速度均待真机验收。

## 复制失败诊断（paste-status 字段）

`paste-status` 除了 `phase` / `copied`，还返回只含计数与时间戳的诊断
字段（不记录正文、令牌、URL 或 Key），用于区分快捷指令没到服务端、
到了没领正文、领了没回确认这三种情况。完整判断表与上一轮故障分析见
[docs/paste-copy-diagnosis.md](paste-copy-diagnosis.md)。

| 字段 | 含义 |
| --- | --- |
| `claimed` / `claimed_at` | 正文是否已被领取及领取时间（单调时钟） |
| `pending_polls` | 本事务存续期间授权通过的 GET `/pending` 次数 |
| `claim_attempts` | 领取尝试次数（含重复领取） |
| `ack_attempts` / `last_ack_error` | POST `/ack` 尝试次数与最后一次被拒的原因 |
| `pending_auth_failures` | 本事务开始后 GET `/pending` 因配对 Key 错误被拒的次数 |
