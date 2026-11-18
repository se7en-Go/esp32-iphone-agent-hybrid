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
```

1. 先核对目标窗口/会话标题唯一准确（日常排查优先用本机 OCR 读取，减少大图拷贝）。
2. 从当前最新观察确定空输入框的归一化边界 `--target-box LEFT TOP RIGHT BOTTOM`（若光标在闪烁，记下左侧光标区 `--caret-box`）。
3. **已有草稿保护**：若输入框内已经存在文字，核对是否完全等于本轮预期清理的测试草稿；发现其他未发送草稿时**原样保留并报告**，不得清空或覆盖。

## 3. 一次 `paste-flow` 与回执判读

输入中文、多行文本或需要保留大小写与换行时，先把精确原文写入受限目录文本文件，只调用一次 `paste-flow`（内置 `prepare` → 触发复制 → `wait_copied` → 唯一一次 `Cmd+V` → 处理可选「允许粘贴」弹窗 → `confirm`），并停在发送前：

```sh
./server/.venv/bin/iphone-agent paste-flow @server/.local-data/runs/payload.txt \
  --target-box L T R B --expect-title "会话标题" \
  [--caret-box CL CT CR CB] \
  --image-output server/.local-data/runs/receipt.jpg
```

核对标准 JSON 回执：
- `cmd_v_sent == 1`、`phase == "completed"`、`effect_verified == true`。
- `copy_trigger` 明确标示本次复制来源（`assistive_touch_hold` 或 `trigger_key`），`trigger_configured` 记录服务端是否配置组合键；`permission_sheet_seen` 记录是否实际处理了 iOS「允许粘贴」弹窗；`retries` 与 `stages_ms` 记录各阶段重试与耗时。
- 完成后再 `observe` 一次确认输入框正文完整且只写入一次、会话标题未变、无新消息气泡。
- **中断恢复铁律**：若 `confirm` 超时或返回 409 但回执显示 `cmd_v_sent == 1`，先用新画面检查输入框现有内容；确认正文已入框且事务未超期时，只对同一 `transaction_id` 调 `paste-confirm`，**严禁清空重贴或重发第二次 `Cmd+V`**。

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
   # 方式 A：先 point 再 observe --for-click，确认指针落在发送按钮框内后原地点击
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
