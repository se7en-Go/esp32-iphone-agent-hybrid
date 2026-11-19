# Pi 与本地 CLI 快速用法

面向 `Pi`、Claude Code 等本地 Bash Agent 的单页速查指南，直接复用 `./server/.venv/bin/iphone-agent`，无需先翻源码摸索接口或参数。

## 1. 固定入口与就绪检查

在仓库根目录直接调用虚拟环境入口（默认连接 `http://127.0.0.1:8890`，可用 `--url` 或 `IPHONE_AGENT_URL` 覆盖）：

```sh
./server/.venv/bin/iphone-agent status
./server/.venv/bin/iphone-agent capabilities
```

开始动作前核对：
- `status` 返回 `device: true`、`screen: true`、`calibrated: true`、`ready: true` 且 `guard.tripped: false`。
- 若启用中文/多行粘贴，核对 `capabilities.features.clipboard`：`staged_available: true`（`protocol: "staged-v1"`）；同时查看 `trigger_configured` 判断当前机器是仅靠辅助触控长按触发，还是配置了触发组合键。

## 2. 新帧核对与已有草稿保护

**所有目标位置必须从当前最新画面确认，禁止跨机型、跨窗口套用写死坐标。**

```sh
./server/.venv/bin/iphone-agent observe server/.local-data/runs/check.jpg
# 本机 OCR：read_text 只接受图片字节，不接受路径字符串
./server/.venv/bin/python -c 'from pathlib import Path; from hidagent.ocr import read_text
for b in read_text(Path("server/.local-data/runs/check.jpg").read_bytes()): print(b.text, b.box)'
```

1. 先核对目标窗口/会话标题唯一准确（日常排查优先用本机 OCR 读取，减少大图拷贝）。
2. 打开 App 避坑：**当前桌面已能看到图标时，直接点击文字标题上方的图标本体**（点文字中心不会启动 App），不要多调 `search-app`，否则可能翻到别的主屏页。当前页确实没有该 App 时，再用 `search-app`（查询词仅限英文 ASCII，例如 `WeChat`），并从结果中点图标本体。
3. 底部输入框避坑：点击底部收起态输入框时，避开最底端 `y > 0.95`（`x ≈ 0.32..0.68`）的 iOS 主屏幕指示条吸附区（优先点击白色输入框偏左文字起点区域），并以「原生键盘弹出 + 光标出现」确认聚焦成功。
4. `point` 与 `click-observed` 落点核对：`point` 返回的 `measured_pointer` 是 JSON 对象 `{"x": …, "y": …}`，未确认落点时为 `null`（**不是数组**，脚本里请用 JSON 解析判断）。`measured_pointer` 非空且在目标框内即可推进，不必再移动。`click-observed` 的坐标必须取 `measured_pointer` 或 `observe --for-click` 实拍图里确认的真实灰点，**严禁传请求坐标或 OCR 文字中心**。发生偏位（如曾触碰底部系统指示条）时加 `--rehome` 重新定位。
5. 内容会自动刷新的列表（如微信会话列表）里，`tap` / `tap-text` 可能因整屏变化被拒；改用 `point --target-box` → `observe --for-click` → `click-observed`。
6. 从当前最新观察确定空输入框的归一化边界 `--target-box LEFT TOP RIGHT BOTTOM`（若光标在闪烁，记下左侧光标区 `--caret-box`）。
7. **已有草稿保护**：若输入框内已经存在文字，核对是否完全等于本轮预期清理的测试草稿；发现其他未发送草稿时**原样保留并报告**，不得清空或覆盖。

## 3. 一次 `paste-flow` 与回执判读

输入中文、多行文本或需要保留大小写与换行时，先把精确原文写入受限目录文本文件，只调用一次 `paste-flow`（内置 `prepare` → 触发复制 → `wait_copied` → 唯一一次 `Cmd+V` → 处理可选「允许粘贴」弹窗 → `confirm`），并停在发送前：

```sh
./server/.venv/bin/iphone-agent paste-flow @server/.local-data/runs/payload.txt \
  --target-box L T R B --expect-title "会话标题" \
  [--caret-box CL CT CR CB] \
  --evidence-dir server/.local-data/runs/<task> \
  --image-output server/.local-data/runs/receipt.jpg
```

`--evidence-dir`（`0700` 目录）会保存 `1-prepare-original.jpg`、`2-paste-original*.jpg`、`3-after-cmd-v.jpg`、`4-confirm-*.jpg`，失败时可直接对照定位哪块画面变了。完整 JSON 回执在标准输出（成功）或标准错误（失败）中，请整段保存，不要只挑几个字段。

核对标准 JSON 回执：
- `cmd_v_sent == 1`、`phase == "completed"`、`effect_verified == true`、`recovery_state == "completed"`。
- `copy_trigger` 明确标示本次复制来源（`assistive_touch_hold` 或 `trigger_key`），`trigger_configured` 记录服务端是否配置组合键；`permission_sheet_seen` 记录是否实际处理了 iOS「允许粘贴」弹窗；`retries` 与 `stages_ms` 记录各阶段重试与耗时（`retries.paste` 表示发键前的画面校验被拒后重新观察，此时 `Cmd+V` 尚未发出）。
- 完成后再 `observe` 一次确认输入框正文完整且只写入一次、会话标题未变、无新消息气泡。

**失败时按 `recovery_state` 停下，执行 `next_action`，不要现场编写重试循环、猜事务 ID 或等 TTL：**

| `recovery_state` | 含义 | 下一步 |
| --- | --- | --- |
| `not_copied` | 快捷指令没领取正文，什么都没粘贴 | 检查画面后可重新运行一次 `paste-flow` |
| `claimed_not_confirmed` | 快捷指令领取了正文但没回复制确认，`Cmd+V` 未发出，剪贴板阻塞（`needs_resolution`） | 停止。确认旧快捷指令已结束、输入框未变后，按 [PASTE_INPUT.md](PASTE_INPUT.md#协议取消与恢复) 调用 resolve 或重启 worker |
| `copied_not_pasted` | 已复制但 `Cmd+V` 未发出，剪贴板阻塞（`needs_resolution`） | 同上：停止并核对后 resolve 或重启 worker，**重跑 `paste-flow` 只会一直报「剪贴板仍有未完成或结果不明的事务」** |
| `paste_sent_unconfirmed` | `Cmd+V` 可能已发出，`paste-flow` 未能确认 | 用新画面检查输入框并停止汇报，**严禁清空重贴或重发第二次 `Cmd+V`**；由人核对正文后再 resolve 或重启 worker |

## 4. 受守卫发送流程（仅在获明确授权后执行）

普通无标签坐标 `tap x y` **不会被服务端自动识别为发送按钮**，也不会校验或消费发送额度；实际发送必须显式走发送守卫与带标签点击：

1. **风险词自检**：将屏幕上看见的系统提示原文传给熔断器，`stop: true` 时立刻停止且不重试：
   ```sh
   ./server/.venv/bin/iphone-agent skill-check --observed-text "提示原文" --skill <skill>
   ```
2. **预留发送名额**（核对静默时段与频率上限；`allowed: false` 时停止）：
   ```sh
   ./server/.venv/bin/iphone-agent skill-prepare-send --skill <skill>
   ```
3. **观察并显式带标签发送一次**：用新凭证确认会话标题与输入框全文无误后，使用带可见发送标签（如 `发送`、`Send`）的已验证点击发送一次；绝不手动重试：
   ```sh
   # 方式 A：先 point（可带 --target-box，确认 measured_pointer 非空）再 observe --for-click，从实拍图确认指针在框内后点击
   ./server/.venv/bin/iphone-agent click-observed <px> <py> --screen-id <id> \
     --target-box <l> <t> <r> <b> --target-label 发送 --image-output server/.local-data/runs/sent.jpg
   # 方式 B：紧区域 OCR 精确匹配发送标签后点击
   ./server/.venv/bin/iphone-agent tap-text 发送 --screen-id <id> --exact --region 0.7 0.6 1 1
   ```
4. **见新气泡再确认**：只有在新画面里明确看到完整的新消息气泡时，才结算发送记录；输入框变空或仅硬件 `ok` 不算发出：
   ```sh
   ./server/.venv/bin/iphone-agent skill-confirm-send --bubble-visible --skill <skill>
   ```
   若结果不明确，**先重新观察并核对聊天记录**，不要直接补点第二次发送。

## 5. 外部业务仓库挂载 Skill

服务端通过 `HIDAGENT_SKILLS` 环境变量（见 [paths.py](../server/hidagent/paths.py)）优先读取外部目录中的 `<skill>/SKILL.md` 与 `layout.json`。微信群监控、跨重启去重和业务话术可以放在独立业务仓库中，启动本底座服务前指向外部 `skills` 目录即可，无需修改本仓库代码：

```sh
export HIDAGENT_SKILLS=/path/to/external-repo/skills
```

也可在本地 CLI 随时核对已挂载列表与内容：

```sh
./server/.venv/bin/iphone-agent skill-list
./server/.venv/bin/iphone-agent skill-show <skill>
./server/.venv/bin/iphone-agent layouts
```

## 6. 本地 OCR 与截图留存规范

- 日常标题检查、空输入框判定与草稿核对**优先走本机 OCR / 内存字节读取**，减少截图文件落地与视觉上下文冗余。
- 必须保存截图或回执证据时，写入权限为 `0700` 且已被 `.gitignore` 忽略的本机私有目录（例如 `server/.local-data/runs/<task>/`），**不要留存在权限较宽的公共 `/tmp`**。
- 任务结束后及时清理临时截图；如需外发截图或日志，务必先对聊天头像、联系人信息、`transaction_id` 及环境密钥做脱敏。
