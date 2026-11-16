# 中文粘贴复制失败诊断：上一轮 copied=false 与本分支的改动

分支：`muse/paste-shortcut-regression`，基线 `9e2a8c7`，独立工作树
`/Users/seven/Documents/esp32-iphone-agent-hybrid-paste`。
主控 Muse 在主工作树继续真机验收时，合并或 cherry-pick 本分支即可；
本分支只动 `server/hidagent/clipboard.py`、两个测试文件和本文档，
与主分支未提交的指针检测改动（`assistive_vision.py` / `phone.py` /
`text_target.py`）没有文件重叠。

## 1. 上一轮故障判断：事务 8BPX4…，120 秒 copied=false

### 能确定的事

- 服务端配置核对无误（`server/.local-data/runtime.env`，只读核对，未改动）：
  `HIDAGENT_CLIPBOARD_PROTOCOL=staged-v1`、
  `HIDAGENT_CLIPBOARD_TRIGGER_KEY=ctrl+alt+p`、`HIDAGENT_CLIPBOARD_TTL=180`、
  `HIDAGENT_CLIPBOARD_BASE_URL=http://sevendeMacBook-Air.local:8891`。
- 配对 Key 没有在 11-16 当天轮换过：当前 `runtime.env` 与
  `runtime.env.bak-20261116` 的 Key 哈希一致（只比对了 SHA-256 前 12 位，
  未读取、未记录 Key 本身）。“快捷指令里存的 Key 过期”这个分支可以排除
  服务端轮换的原因，但手机里存的仍可能是更早的旧 Key，见下。
- `prepare` 确实只发了一次触发键：`prepare` 成功、phase `awaiting_copy`、
  24 次 `status` 轮询 120 秒 `copied` 始终 false。触发键发送本身有代码
  覆盖，不是“没发键”。
- 绑定必须指向的分阶段模板是 **`ESP32 Agent Clipboard`**：
  `scripts/build_clipboard_shortcut.py` 的 `WFWorkflowName` 就是这个名字，
  动作是领取 staged-v1 待办 → 取 `payload_url` → 校验 `copy_allowed` →
  原样复制（Text 动作，前导零保留）→ POST `ack_url {"copied": true}` →
  静默结束，没有提醒、没有焦点声明。旧同步协议的
  **“领取待粘贴文本”不是已验证过的绑定对象**，不要绑它。

### 不能确定的事（也是本分支要修的）

上一轮的三种情况，**从留存证据里区分不开**：

| 情况 | 服务端应有的痕迹 | 上一轮有没有这条痕迹 |
| --- | --- | --- |
| A. 快捷指令未启动（绑定缺失 / 全键盘控制未开 / 键位不对 / 触发键发给了别的 App） | `pending()` 一次也没被调 | 无从查：两个 uvicorn 都是 `access_log=False`，剪贴板代理转发不记日志，`server.log` 里没有任何 clipboard 行；事务是内存态，TTL 180 秒过期后已消失 |
| B. 已启动、未领取正文（URL/网络不通、Key 错 401、protocol 对不上、取 payload 前停下） | `pending()` 被调过，`fetched` 仍 false | 同上。`stage_status` 当时只暴露 `copied`（即 acknowledged），`fetched` 对宿主不可见 |
| C. 已领取、未回传复制成功（复制后 ACK 没发出来，或 ACK 在路上失败） | `fetched` true、`acknowledged` false | 同上。过期后 `_needs_resolution` 会变 true（fetched 且未完成），但这是事后标志，事务过期后也无人读过它的值，现在已无法补读 |

所以上一轮的诚实结论是：**只知道复制确认没到，不知道卡在哪一步**。
不要把“绑定没生效”写成已证实的根因，那只是三种情况里的一种。

### 最值得先查的一个假设：绑成了旧快捷指令

如果 Control+Option+P 绑的是旧同步协议的“领取待粘贴文本”，症状会和
上一轮**一模一样**：它照样 GET `/pending`、GET `text_url` 领走正文、
复制到剪贴板，然后弹出“已复制。请确认……”提醒，**等人在手机上点
“继续”才会 POST ack**。120 秒里没人点提醒，`copied` 就一直是 false，
而剪贴板里其实已经有正文了。分阶段模板没有这道提醒，绑对了就不会
出现这种停滞。真机复测前，先在手机上核对绑定名（见第 3 节），
并在轮询时看一眼镜像里有没有这个提醒。

其他可能，按排查成本从低到高：绑定根本没存上；全键盘控制开关被关；
快捷指令被改名/删除后绑定指向不存在的项；手机与电脑不在同一 Wi-Fi，
或 `sevendeMacBook-Air.local` 在手机上解析失败（此前有过 Wi-Fi 切换
后镜像断开的记录）；快捷指令里存的服务器地址/Key 是更早的旧值。

## 2. 本分支的改动：让下一次失败当场可定位

`stage_status`（即 `paste-status` 返回的字段）新增了只含计数与时间戳
的诊断字段，不记录正文、令牌、URL 或 Key：

- `claimed` / `claimed_at`：正文是否已被领取（`fetch`/`fetch_payload`
  成功即 true）、领取时间（单调时钟，与 TTL 同源，不是墙上时间）。
- `pending_polls`：本事务存续期间授权通过的 GET `/pending` 次数。
- `claim_attempts`：领取尝试次数（含重复领取的 404）。
- `ack_attempts` / `last_ack_error`：POST `/ack` 尝试次数与最后一次被拒
  的原因（`ack before claim` / `ack without copied=true` /
  `ack without focused=true` / `copy already acknowledged`）。
- `pending_auth_failures`：本事务开始后 GET `/pending` 因配对 Key 错误
  被拒的次数（401）。这是唯一能把“从未启动”与“启动了但 Key 错”
  分开的信号。

下一次轮询时按这张表读数：

| 读数 | 判断 | 下一步 |
| --- | --- | --- |
| 全部为 0，`pending_auth_failures` 也是 0 | A：快捷指令没到服务端 | 查绑定名、全键盘控制开关、键位，见第 3 节第 1 步 |
| `pending_auth_failures` > 0 | 快捷指令到了，但存的 Key 错 | 在 iPhone 上重新导入模板并重填服务器地址和配对 Key |
| `pending_polls` > 0，`claimed` false | B：到了 pending，但没领 payload | 看镜像：快捷指令报错弹窗？protocol 不是 staged-v1？网络断在两次请求之间？ |
| `claimed` true，`ack_attempts` == 0 | C：领走了，没回 ACK | 看镜像有没有“已复制”提醒（旧快捷指令的特征）；没有提醒则可能是 ACK 请求发不出 |
| `ack_attempts` > 0，`copied` false | ACK 发过但被拒，看 `last_ack_error` | 按错误原因处理：`ack before claim` 是时序问题，重走流程；`ack without copied=true` 是快捷指令没有真正复制 |

另有已有的事后标志：事务过期后 `current()` 的 `needs_resolution` 为
true 说明正文曾被领走（fetched），false 说明没领过。过期前优先看
`stage_status`，不要等过期。

### 测试

新增 `server/tests/test_clipboard_copy_diagnostics.py`（8 个测试）：
三种状态各一个、错 Key 计数、领前 ACK、复制为 false 的 ACK、重复领取、
旧 text_url 领取路径（绑错成旧快捷指令时领取同样可见），以及诊断
字段不含正文/令牌的断言。

`server/tests/test_confirm_sheet_retry.py` 新增一个端到端确认回归：
备忘录追加粘贴——已有正文后追加以换行开头的
`"\n秋天到了00731\n第二行 AbD eFg"`（中文、前导零 `00731`、换行、
大小写 `AbD eFg`），用**真实** `verify_after_paste_permission`（不是
mock）验证；权限弹窗只保留在**同一事务** `paste()` 时的帧里，
4 秒 recent 窗口已清空后 `confirm` 仍能完成，返回
`confirmation_evidence=paste_permission_dismissed`；弹窗文案是备忘录
的（“备忘录”想从“快捷指令”粘贴）。Safari 措辞的粘贴测试保留在
`server/tests/test_paste_confirmation.py`，未改动。

## 3. 主控 Muse 真机步骤（按顺序做，不要跳步）

前置：确认工作树已是含本分支代码的服务并已 `restart-worker`
（改了 `clipboard.py`，旧 worker 跑的是旧代码；`replay_lag_s` 为 null
就是旧 worker 的信号）。以下步骤用的都是新事务，旧事务
`8BPX4…` 已过期，不要对它重发触发键。

1. **先核对绑定，不要先发 prepare。** 在 iPhone 上：
   设置 → 辅助功能 → 键盘与键入 → 全键盘控制 → 打开 → 命令 →
   找到“快捷指令”分组，确认 Control+Option+P 绑的是
   **`ESP32 Agent Clipboard`**。如果是“领取待粘贴文本”或其他名字，
   改绑到 `ESP32 Agent Clipboard`；如果根本没有这一项，说明绑定没存
   上，重新录入一次（进录入界面后可从控制台发一次
   `key ctrl+alt+p`）。同时确认快捷指令 App 里 `ESP32 Agent Clipboard`
   还在，里面存的服务器地址是 `http://sevendeMacBook-Air.local:8891`
   这一台。
2. **新 prepare。** 在备忘录测试笔记、光标在正文末尾时发
   `paste-prepare`，正文用 `"\n秋天到了00731\n第二行 AbD eFg"`
   （上一轮备好的文本，从未验证成功过，继续用它；不要用已验证过的
   你好00123 / 00123 / 00042 / 00084 / AbC xYz）。prepare 会自动发一
   次 Control+Option+P，**不要再手动补发一次**。记下新事务 ID。
3. **轮询 `paste-status`，读第 2 节的表。** 每 5 秒看一次，最多 120 秒：
   - 10 秒内 `claimed` 仍 false 且 `pending_polls` 仍 0：先看镜像有
     没有快捷指令的报错/提醒；没有就停下来按 A 处理，不要干等 120 秒。
   - `pending_auth_failures` 上升：停下来重填 Key，不要继续轮询。
   - `claimed` true 但 `copied` 仍 false：看镜像有没有“已复制”提醒；
     有就是绑错了旧快捷指令，点掉提醒后按第 1 步改绑，再新开事务重测。
   - `copied` true（`awaiting_paste`）：继续第 4 步。
4. **paste。** 先用新画面凭证核对光标仍在原笔记、浮层已退出，再发
   一次 `paste`（只发一次 Cmd+V）。iOS 若弹“允许粘贴”，核对来源后允许。
5. **confirm。** 在新画面上逐字核对：原有正文原样在上，新增两行
   “秋天到了00731”“第二行 AbD eFg”在后（注意首行前有一个空行），
   再发 `paste-confirm`，`observed_text` 必须与 prepare 的正文
   **逐字节一致**（含开头的换行与 `AbD eFg` 的大小写）。成功应返回
   `completed`，`effect_verified` true。
6. **核对备忘录实际内容。** 把 `completed` 回执与备忘录里实际看到的
   全文逐字比对（不只是回执），确认无误后，把测试时误改的那一行
   从“核对：AbC xylem”恢复为“核对：AbC xYz”。
7. 如果第 3 步在 120 秒内仍是全零读数，把当时的 `stage_status`
   全量贴回本分支的诊断表里再定下一步，不要凭感觉重发触发键。
