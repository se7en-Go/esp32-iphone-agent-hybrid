# 本机运行与镜像接收器

在仓库根目录启动后台服务，入口使用 `server/.venv`，配置与校准保存在 `server/.local-data`。

```sh
./scripts/run-local.sh start
./scripts/run-local.sh status
```

网页地址是 <http://127.0.0.1:8890/>。启动后可以关闭启动命令所在的终端。电脑关机后再执行 `start`。这套入口没有注册开机启动任务。

先预览启动设置可以使用下面的命令。它会显示后端、数据目录和接收器是否可找到，不启动进程，也不输出配置内容或 API Key。

```sh
./scripts/run-local.sh start --dry-run
```

## Mac 镜像与网页重启

Mac 的 UxPlay 接收器由独立进程持有。iPhone 连接「ESP32 Agent Mac」后，网页 worker 通过用户专属的 Unix socket 读取最新画面。更新 Python 代码后重启网页服务，接收器继续运行。

```sh
./scripts/run-local.sh restart-worker
```

这个命令保留 receiver 进程及当前 AirPlay 会话。重启期间停止当前网页任务，等待新服务就绪后重新观察手机，再提交任务。iPhone 是否仍连接可从 `status` 和网页的新画面确认。

配置剪贴板通道时，后台 worker 会同时管理仅开放剪贴板接口的代理。`restart-worker` 会一起替换网页 worker 与该代理，`stop` 会一起关闭；不会遗留需要另一个终端手动关闭的代理。任一子进程退出，管理进程会关闭另一个并清除进程记录，避免网页继续显示可用、手机却已无法领取文本。

停止全部本机进程使用下面的命令，它会断开屏幕镜像。

```sh
./scripts/run-local.sh stop
```

无参数运行 `./scripts/run-local.sh` 仍以前台方式启动网页 worker。其终端关闭会终止前台服务，独立 receiver 会继续运行。前台 worker 不属于后台管理命令的管理范围。

Windows 的 `scripts/run-server.ps1` 保留原来的前台板载镜像路径。Unix socket 后台管理用于 macOS 和 Linux。

## 配置位置

配置按进程环境、仓库 `.env`、`server/.env`、数据目录 `runtime.env` 的顺序读取，先出现的值优先。运行脚本默认提供回环监听地址及 `server/.local-data` 数据目录。可在启动命令的环境里覆盖 `PORT`、`HIDAGENT_DATA` 等设置。

管理入口的 `HOST` 只支持 `127.0.0.1` 或 `localhost`；IPv6 回环 `::1` 会在启动任何进程前明确报错。

Mac 本地已有 `server/.local-data/uxplay-runtime/bin/uxplay` 时，管理入口默认使用 UxPlay。显式的 `SCREEN_BACKEND=board` 或 `--backend board` 可以选择板载镜像。使用 UxPlay 时，入口自动补齐该 runtime 的 `bin`、GStreamer 插件和 scanner 路径。也可以用 `UXPLAY_RUNTIME` 指向另一套已安装 runtime，或让 `PATH` 中已有 `uxplay`。

可选的 UxPlay 分帧模式会在 iPhone 快速断开再连接时废弃旧画面和旧操作凭证。构建兼容的二进制、选择依赖路径和启用 `HIDAGENT_UXPLAY_FRAMED=1` 的步骤见 [原生接收器构建说明](../native/uxplay/README.md)。启用设置在接收器启动时生效。

推荐的非敏感运行设置如下。

```dotenv
SCREEN_BACKEND=uxplay
AIRPLAY_NAME=ESP32 Agent Mac
```

如果需要 AirPlay 配置，把已有接收器配置保存在数据目录 `uxplay.conf`。入口自动设置 `UXPLAYRC`。显式设置的 `UXPLAYRC` 优先，文件须在启动时存在。`AIRPLAY_SOCKET` 可指定更短的 Unix socket 路径，macOS 路径长度限制为 103 字节。

MCP 使用宿主 Agent 的模型时，网页服务与接收器都不需要 LLM API Key。已有 `scripts/run-mcp.sh` 连接本机网页服务。网页内置模型任务需要另外配置 `LLM_MODEL`、`LLM_BASE_URL` 和 `LLM_API_KEY`，DeepSeek 等自定义提供方按原有配置方式选择。

## 受管剪贴板代理

只有同时设置 `HIDAGENT_CLIPBOARD_BASE_URL` 和 `HIDAGENT_CLIPBOARD_KEY` 才会启用后台代理。它们可以与可选的 `HIDAGENT_CLIPBOARD_TRIGGER_KEY` 一起保存在数据目录的 `runtime.env`，该私密文件不要提交。Key 的生成和 iPhone Shortcut 配置见 [PASTE_INPUT.md](PASTE_INPUT.md)。未配置时维持原来的单 worker 行为，不增加局域网监听。

使用仓库生成的分阶段 Shortcut 模板时，同时设置 `HIDAGENT_CLIPBOARD_PROTOCOL=staged-v1`，避免旧同步 `type` 等待模板不会提供的人工焦点/完成 ACK。此模式下普通 ASCII `type --mode auto` 继续走 HID 键盘，其他粘贴使用分阶段工具；默认 `legacy-v1` 保留旧快捷指令兼容。配置校验不会证明手机已经安装或绑定触发键。

`HIDAGENT_CLIPBOARD_BASE_URL` 是 iPhone 能访问的电脑局域网地址，例如 `http://computer.local:8891`。受管代理只支持无路径的 HTTP 地址，拒绝用户名、密码、查询参数和 fragment；**请显式指定与控制服务不同的专用端口**。省略端口按 HTTP 标准使用 80，可能因权限或端口占用而启动失败，不会偷偷改成 8891。监听地址可用 `HIDAGENT_CLIPBOARD_PROXY_HOST` 指定，IPv4 默认 `0.0.0.0`；这只开放代理已有的剪贴板白名单，控制 API 仍在回环地址。

使用下面入口，不再另外运行 `hidagent.clipboard_proxy`：

```sh
./scripts/run-local.sh start --dry-run
./scripts/run-local.sh start
./scripts/run-local.sh status
```

`dry-run` 只输出配置存在与否、受管开关和监听设置，绝不输出 Key。`status` 的 `clipboard_proxy.running` 同时检查受管 worker 和代理的受保护端点；服务启动要等两个子进程都就绪。缺少配对字段、非法 URL 或控制端口冲突会在启动任何服务之前报错；操作系统绑定失败则关闭已启动的子进程，并在 `worker.log` 留下原因。

已有 HTTPS 反向代理或独立代理时，设置 `HIDAGENT_CLIPBOARD_MANAGED_PROXY=0`，由原有外部进程管理其生命周期。无参数的前台 `run` 以及 Windows 前台入口也仍按 [PASTE_INPUT.md](PASTE_INPUT.md) 的双终端方式运行；本节的自动代理生命周期仅用于 Unix 的后台 `start` / `restart-worker`。

## 从旧启动方式迁移

旧版 worker 同时持有 UxPlay 子进程，迁移到独立 receiver 时需要一次停止旧 worker 并重新连接镜像。管理入口发现端口被未受其管理的 worker 占用时会停止启动，避免重复占用串口或新建第二个接收器。

先将旧启动器需要的配置保存在上述持久文件中，停止旧 worker，再执行 `start`。本仓库入口不读取临时目录中的启动脚本。迁移成功后，日常更新只需 `restart-worker`。

## 画面与故障检查

Unix socket 限制为当前用户可访问的 `0600`。每次响应只有最新一帧，保留接收器发布画面的原始时间。重复读取不会刷新旧帧时间。worker 与 receiver 断连会清空 worker 画面，receiver 重启会更换实例与画面代际，原来尚未执行的输入会重新核验。

运行日志在数据目录的 `worker.log` 和 `receiver.log`，进程记录位于 `worker.json`。这些文件由 `.gitignore` 排除。运行管理只会停止带匹配实例标识的 worker，避免 PID 被其他进程复用时误操作。

`status` 可确认网页响应、ESP32、校准、镜像接收器进程和当前画面状态。镜像处于静止画面时可以没有新帧，网页会显示等待或闲置状态，动作仍须满足现有的新鲜画面校验。

`HIDAGENT_POINTER_DIAGNOSTICS` 是可选的本地截图诊断目录，只在支持 POSIX 私有权限及禁止跟随符号链接的文件操作时启用，目录权限为 `0700`、文件为 `0600`。Windows 自动停用此诊断功能，不创建或修改诊断文件，手机控制和原有错误校验继续运行。
