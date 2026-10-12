# Octopus 使用说明：本机启动、智能体协作与手机访问

本文针对 `/home/jack/Octopus` 这套 WSL + Codex 安装，按当前源码和本机配置整理。
手机访问采用[项目 README 推荐的 Cloudflare Tunnel](../README.md#quick-start)。

## 1. 当前安装状态

2026-10-10 核对结果：

| 项目 | 本机情况 |
| --- | --- |
| 运行环境 | Windows 上的 WSL / Ubuntu，已启用 systemd |
| 项目目录 | `/home/jack/Octopus` |
| Python 环境 | 项目内 `.venv`，使用 pip |
| 默认 AI 后端 | Codex，Linux CLI 已使用 ChatGPT 账户登录 |
| 后台服务 | 用户级 `octopus.service`，已启用且正在运行 |
| 后端监听 | `127.0.0.1:8000`，`/health` 返回 `{"status":"ok"}` |
| Windows 访问 | <http://localhost:8000> |
| 前端 | 已构建，后台服务直接提供 `web/dist` |
| Octopus 登录 | 尚未创建账户，目前使用安装令牌 |
| 手机隧道 | 尚未启用，WSL 中尚未安装 `cloudflared` |

这些是核对时的状态；本文的安装和隧道配置命令供手动执行。

## 2. 日常启动和关闭

### 启动

先在 Windows 打开 Ubuntu 终端，或在 PowerShell 执行：

```powershell
wsl -d Ubuntu
```

然后在 Ubuntu 中执行：

```bash
cd /home/jack/Octopus
systemctl --user start octopus
systemctl --user status octopus --no-pager
curl --noproxy '*' --fail --silent --show-error \
  --retry 30 --retry-connrefused --retry-delay 2 --max-time 5 \
  http://127.0.0.1:8000/health
```

看到 `active (running)`，且健康检查返回 `{"status":"ok"}` 后，在 Windows
浏览器打开 <http://localhost:8000>。服务已经运行时，`start` 不会再启动第二份。
日常使用不需要重新安装依赖或构建前端。

### 管理服务

本机安装的是**用户级服务**，在 Ubuntu 中使用下面的命令：

| 操作 | 命令 |
| --- | --- |
| 启动 | `systemctl --user start octopus` |
| 查看状态 | `systemctl --user status octopus --no-pager` |
| 重启 | `systemctl --user restart octopus` |
| 停止 | `systemctl --user stop octopus` |
| 查看近期日志 | `journalctl --user -u octopus -n 100 --no-pager` |
| 实时查看日志 | `journalctl --user -u octopus -f` |

重启或停止服务会中断正在执行的智能体任务，宜在任务完成后操作。
启用手机隧道后，服务停止也会关闭隧道，重启会生成新的访问网址。

## 3. 登录、工作目录和智能体设置

### 登录 Octopus

当前首次登录使用项目 `.env` 中的 `OCTOPUS_AUTH_TOKEN`。用本机编辑器查看并
复制其值到登录框；不要把令牌写入文档或提交到 Git。

登录后，可以通过左下角账户菜单进入 **Account → Claim this install**，设置
用户名和密码并点击 **Create account**。第一个账户成为管理员，并接管已有
智能体、会话等数据。此后电脑和手机都使用用户名、密码登录，原安装令牌
不再作为登录凭据。管理员可以在 Account 页面创建邀请，供其他人注册。

Octopus 网页登录与 Codex 模型登录是两回事。本机已登录 Codex，新会话选择
**Codex** 并沿用已有 CLI 登录即可。

### 给任务选择正确的工作目录

在侧栏的智能体下新建会话时，展开 **+ working dir · engine**，填写 working dir。
开发本项目使用：

```text
/home/jack/Octopus
```

未填写时使用服务的默认工作目录；创建账户后，默认目录属于该账户。
创建账户后，外部目录需要列入允许范围：到 **Account → Where you can work**
检查是否已有 `/home/jack/Octopus`，没有就通过 **Add a directory** 添加。
管理员同样可以添加实际 Obsidian vault 所在目录。

所有路径都以运行 Octopus 的 WSL 文件系统为准。例如 Windows 的
`D:\Notes\Vault` 对应 WSL 的 `/mnt/d/Notes/Vault`。Weber 的实际 vault 路径
尚未确定，首次交给它知识库任务时应提供完整路径。

### 保存 One-line role 和 System prompt

打开智能体的编辑页面，或在选中智能体后通过左下角菜单进入 **Agent settings**。
把 [agent-prompts.md](agent-prompts.md) 中对应的英文内容分别复制到
**One-line role** 和 **System prompt**，保存即可；后续轮次会读取更新后的配置。

| 字段 | 用途 |
| --- | --- |
| One-line role | 在界面和协作对象列表中简短介绍职责 |
| System prompt | 给模型的持续行为指令，包括职责、工具使用和协作规则 |
| Engine | 选择执行后端；本机使用 Codex |
| Tools / MCP | 决定可调用的工具；prompt 只能指导模型使用已启用的工具 |

角色和 prompt 保存在本机 SQLite 数据库的 `agents.description`、
`agents.system_prompt` 中。[agent-prompts.md](agent-prompts.md) 是便于复制的
参考文件，编辑该文件不会自动更新界面的智能体配置。

## 4. 四个智能体怎样配合

| 智能体 | 适合交给它的任务 |
| --- | --- |
| Octo | 主力开发：理解项目、制定实现方案、修改代码、验证、根据 review 修复 |
| Vera | `Review the artifacts from other agents`：审查架构、文档、代码与测试，指出有证据的问题 |
| Weber | 讨论 idea、澄清取舍、管理 Obsidian / Markdown 知识库 |
| Charlie | 按你配置的职责处理数据分析与投资研究任务 |

日常从最合适的智能体发起任务。例如给 Octo：

```text
请在 /home/jack/Octopus 实现这个需求：……
遵守项目 AGENTS.md。完成后请 Vera review 架构、代码和测试，
根据其具体发现修复并完成验证，再给我最终结果。
```

Octo 可以通过框架的 `mcp__ask_agent__ask` 把明确的审查任务交给 Vera。
框架为 Vera 创建独立会话，带上它自己的 prompt、工具和记忆，并沿用父会话
的工作目录。Vera 的结果会自动作为后续消息送回 Octo；继续讨论同一项审查
时，Octo 使用原 `delegation_id` 续接。界面会显示协作卡片。

Vera 不会自动得到 Octo 的完整聊天记录，因此委托应说明目标、文件路径、
改动范围、验证结果，以及需要判断的问题。框架限制循环委托和调用深度，
常规流程应由 Octo 发起、Vera 返回发现、Octo修复，再让 Vera 复查。

写 prompt 能指导协作，但不等于让四个智能体自动组成固定流水线：执行者
需要实际调用协作工具。`ask_agent` 用于调用你创建的持久智能体；Codex 自身的
原生子智能体用于当前任务的临时分工，两者分别承担不同用途。

给 Weber 的首次任务可以这样写：

```text
我的 Obsidian vault 位于 /mnt/d/Notes/Vault（请替换成实际路径）。
先阅读现有目录和笔记组织方式，再帮我讨论这个 idea：……
把最终达成共识的内容整理成 Markdown 笔记，并链接相关已有笔记。
```

框架提供的主要工具：

| 工具 | 用途 |
| --- | --- |
| `ask` | 在界面中向你提问，需要选择或补充信息时使用 |
| `bg` | 启动跨轮次后台命令，完成后将结果送回会话 |
| `ask_agent` | 委托、续接、查看和取消其他持久智能体的任务 |
| `research` | 发起深度研究，界面显示进度，完成后回送报告 |
| `schedule` | 管理当前智能体自己的定时任务 |

被委托的智能体应在返回前完成委托的核心工作；不能先用后台任务或研究工具
启动核心工作，再提前给父智能体一个“已完成”的回复。

## 5. 常用界面和命令

输入框输入 `/` 可以查看命令菜单：

聊天页右上角的 **Model & reasoning** 可以分别设置当前会话的模型和思考强度。
修改从下一轮生效，保留会话历史；只改思考强度时模型不会改变。选择
**Agent default** 恢复继承智能体默认值。智能体编辑页的 **Model** 和
**Reasoning effort** 保存该智能体的默认设置，未设置时继承 Codex 配置。
强度候选项随当前模型变化，较高强度通常需要更多时间；`ultra` 还包含自动委派。

| 命令 | 用途 |
| --- | --- |
| `/remember <内容>` | 让当前智能体将内容保存到自己的长期记忆 |
| `/research <问题>` | 启动深度研究，最后生成带来源的报告 |
| `/showme <路径>` | 在网页中查看服务器上的文件 |
| `/schedule [时间] [任务]` | 设置定时运行；仅输入 `/schedule` 打开任务总览 |
| `/model [模型名]` | 覆盖当前会话的模型；不填写名称时打开选择界面 |
| `/rewind` | 从之前某条用户消息新建分支；界面可检查是否回退相关文件变更 |
| `/fork [名称]` | 复制会话上下文和整个工作目录，创建独立副本 |
| `/archive` | 归档当前会话并开始新会话 |
| `/reset` | 强制重置卡住的会话 |

`/model` 的名称必须是当前 Codex 后端可用的模型。`/fork` 会复制整个工作目录，
操作前注意是否包含大型依赖或数据目录。智能体长期记忆与 Obsidian vault
是两个独立位置；需要写入知识库时，应明确指定 vault 中的笔记路径。

侧栏 **Schedules** 管理定时任务，**Connectors** 管理外部服务连接，
**Harness** 管理后端及凭据。连接 GitHub、Gmail 或自定义 OAuth 服务可参照
[connectors-setup.md](connectors-setup.md)。**Applications** 用来创建和使用
智能体生成的网页应用。

智能体和应用可以固定到侧栏。未固定的项目仍然可以运行；到对应的 **All**
页面可以找到它们。归档表示停用，可在归档列表恢复。

关闭浏览器不会主动停止服务上的任务。只要服务和 WSL 仍在运行，任务会继续；
重新打开页面可接着查看。定时任务也依赖这台机器上的服务持续运行。
当前 Codex 后端不支持 README 中面向 Claude 会话的 `handoff` / `pull` 工作流。

## 6. 手机远程访问：Cloudflare Tunnel

项目内置入口是 `octopus serve --tunnel`。在这台已配置后台服务的机器上，
使用 `.env` 启用同一功能，继续由现有服务管理进程即可。

```text
手机浏览器 → HTTPS / Cloudflare Tunnel → WSL 的 localhost:8000 → Octopus
```

### 第一步：在 Ubuntu / WSL 安装 cloudflared

使用 [Cloudflare 官方 apt 仓库](https://pkg.cloudflare.com/index.html)
推荐的通用 Debian 系发行版配置，在 Ubuntu 终端执行：

```bash
sudo mkdir -p --mode=0755 /usr/share/keyrings
curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg \
  -o /tmp/octopus-cloudflare-main.gpg
sudo install -m 0644 /tmp/octopus-cloudflare-main.gpg \
  /usr/share/keyrings/cloudflare-main.gpg
printf '%s\n' \
  'deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared any main' \
  | sudo tee /etc/apt/sources.list.d/cloudflared.list
sudo apt-get update
sudo apt-get install cloudflared
cloudflared --version
```

逐条确认成功再执行下一条。安装位置是运行 Octopus 的 WSL，后台服务已有
`/usr/bin` 路径，可以找到通过 apt 安装的 `cloudflared`。

### 第二步：启用后台服务的隧道

用编辑器打开 `/home/jack/Octopus/.env`，将已有的隧道设置改为以下值；没有则
新增该行，避免保留重复设置：

```dotenv
OCTOPUS_ENABLE_TUNNEL=true
```

保留现有 `OCTOPUS_HOST=127.0.0.1` 和 `OCTOPUS_PORT=8000`。隧道连接本地
服务，无需为手机访问开放路由器端口或改变后端监听地址。

在 Ubuntu 中重启：

```bash
systemctl --user restart octopus
systemctl --user status octopus --no-pager
```

隧道初始化可能需要等待；项目最多等待约 60 秒获取网址。查看当前服务进程的
启动日志，避免复制到以前运行留下的旧网址：

```bash
octopus_server_pid=$(systemctl --user show octopus --property=MainPID --value)
journalctl --user -u octopus "_PID=$octopus_server_pid" -b --no-pager -o cat \
  | rg 'Tunnel URL:|Cloudflare Tunnel active:'
```

成功时日志中会出现类似以下内容；示例域名不能直接使用：

```text
Tunnel URL: https://example-random-name.trycloudflare.com
```

若暂时没有网址，使用 `journalctl --user -u octopus -f` 查看启动过程。
同时可运行第 2 节的 `/health` 检查，确认 Octopus 本地服务正常。

### 第三步：在手机浏览器登录

1. 在手机 Safari / Chrome 打开日志给出的完整 `https://…trycloudflare.com` 网址。
2. 使用与电脑相同的 Octopus 登录方式：尚未创建账户时使用安装令牌，创建后
   使用用户名和密码。
3. 打开侧栏抽屉，选择智能体和会话；可以继续电脑上的同一会话。
4. 手机输入框的回车用于换行，点击发送按钮发送消息。
5. 可通过浏览器“添加到主屏幕”保存入口；网址变化后需要更新入口。

手机不需要与电脑处在同一个 Wi-Fi，也不需要安装 Codex。文件读写和代码
执行仍发生在电脑的 WSL 中。启用隧道后，可以先用手机访问同一网址的
`/health`，再登录并确认能看到已有会话。

本项目启动的是 **Quick Tunnel**：不需要 Cloudflare 账户或自有域名，生成
临时公开 HTTPS 网址。网址在隧道退出后失效，每次重新启动会变化；新的网址
属于新的浏览器站点，可能需要重新登录。该服务不提供可用性保证。
参见 [Cloudflare Quick Tunnels 文档](https://developers.cloudflare.com/tunnel/get-started/quick-tunnels/)。

### 电脑需要保持什么状态

手机访问期间，Windows 需要保持开机、联网且不进入睡眠，WSL 和 Octopus
服务需要运行。`octopus.service` 已启用，意味着 WSL 的用户服务环境启动后
可以自动运行；它不会让已关机的 Windows 自动开机。

WSL 的 systemd 服务本身不会阻止 WSL 实例退出，见
[Microsoft 的 WSL systemd 说明](https://learn.microsoft.com/en-us/windows/wsl/systemd)。
可保持 Ubuntu 终端打开；也可以在一个单独的 PowerShell 窗口运行下面的
前台命令来保持 WSL 实例运行，并保持这个窗口打开：

```powershell
wsl -d Ubuntu --exec /usr/bin/sleep infinity
```

这不阻止 Windows 睡眠或关机，也不是 Windows 开机自启配置。

### 直接使用 README 的前台命令

需要在终端直接看到网址和日志时，可以停掉后台服务后运行：

```bash
systemctl --user stop octopus
cd /home/jack/Octopus
export PATH="$HOME/.nvm/versions/node/v24.21.0/bin:$HOME/.local/bin:$PATH"
.venv/bin/octopus serve --tunnel
```

保持该终端运行。结束时按 `Ctrl+C`，再用 `systemctl --user start octopus`
恢复后台服务；后台是否启用隧道仍取决于 `.env`。前台和后台只能运行一份，
否则会争用 8000 端口。

### 关闭手机入口和长期网址

关闭隧道时，将 `.env` 中 `OCTOPUS_ENABLE_TUNNEL` 改为 `false`，再执行
`systemctl --user restart octopus`。电脑继续通过 <http://localhost:8000> 访问。

需要固定域名时，应配置普通 Cloudflare Tunnel，并将其指向本机 8000 服务；
项目的 `--tunnel` 参数只创建临时隧道。普通隧道的域名配置见
[Cloudflare Tunnel 入门](https://developers.cloudflare.com/tunnel/get-started/)。

Quick Tunnel 不支持 SSE。按项目当前实现，主聊天界面使用 WebSocket；
Applications 中调用 `/apps/{id}/agent/chat` 的应用则使用 SSE，因此该类
应用的流式对话不适用 Quick Tunnel。需要该功能时使用普通 Cloudflare Tunnel。
这个限制来自 [Cloudflare Quick Tunnels 文档](https://developers.cloudflare.com/tunnel/get-started/quick-tunnels/)。

## 7. 更新后的部署与本地开发

普通使用只需启动服务。源码更新后，若有前端变化，按项目要求先重启后端，
运行第 2 节的健康检查并确认正常，然后构建前端：

```bash
cd /home/jack/Octopus/web
export PATH="$HOME/.nvm/versions/node/v24.21.0/bin:$HOME/.local/bin:$PATH"
bun run build
```

刷新浏览器；启用了 Quick Tunnel 时，先查看重启后生成的新网址。
8000 端口提供的是构建后的 `web/dist`，编辑 `web/src` 后必须构建才能看到变化。
需要前端实时开发时，在 `web` 中执行 `bun dev`，在电脑访问
<http://localhost:5173>。手机隧道仍指向 8000 的正常服务。

重装依赖、代理配置、完整测试和同步上游的本机细节见
[local-wsl.md](local-wsl.md)；同步之前应先保存已有源码改动。

## 8. 配置和数据在哪里

| 内容 | 本机路径或入口 |
| --- | --- |
| 服务配置、安装令牌 | `/home/jack/Octopus/.env` |
| 主数据库：智能体角色、prompt、会话、定时任务等 | `/home/jack/.octopus/octopus.db` |
| 监控数据库 | `/home/jack/.octopus/octopus-metrics.db` |
| 用户服务定义 | `/home/jack/.config/systemd/user/octopus.service` |
| 服务的网络代理环境 | `/home/jack/.octopus/proxy.env` |
| 智能体记忆、应用、附件和账户数据 | `/home/jack/.octopus/` 下的相应目录 |
| Codex 自身登录和会话数据 | 本机 `~/.codex/` |
| 智能体 prompt 参考文件 | [agent-prompts.md](agent-prompts.md) |
| Obsidian 知识库 | 你另行指定的实际 vault 目录 |

创建账户会使用账户对应的数据目录。当前允许访问的工作目录，以 Account
页面显示的 workspace 和额外目录为准。备份时保留数据库、配置、Codex 数据、
应用及实际项目 / vault；通用备份说明见 [backup-and-migrate.md](backup-and-migrate.md)，
本机数据库路径以本节为准。

## 9. 常见问题

| 现象 | 处理 |
| --- | --- |
| 电脑打不开 `localhost:8000` | 确认 WSL 正在运行，查看用户服务状态、日志和 `/health` |
| 提示 8000 端口已占用 | 检查是否同时启动了后台服务和前台 `octopus serve`，保留一份 |
| 页面显示旧界面或静态文件 404 | 更新后构建 `web/dist`，然后刷新当前页面 |
| 手机网址失效 | 检查电脑、WSL、服务状态，从当前服务进程日志重新获取网址 |
| 隧道启动后没有网址 | 检查 `cloudflared --version`、`.env` 设置和服务启动日志；隧道失败时本地服务仍可运行 |
| 隧道中途退出、本地服务仍正常 | 查看日志，重启 Octopus 重新建立隧道并获取新网址；当前实现不会单独自动重启隧道 |
| 创建账户后原令牌不能登录 | 改用刚创建的用户名和密码 |
| 新会话工作目录被拒绝 | 到 Account 的 Where you can work 添加实际目录，再使用 WSL 路径 |
| 提示找不到 `codex` 或 `bun` | 新开 Ubuntu 终端加载 nvm，或使用上文的 PATH 设置 |
| Codex 外网请求失败 | 检查 Windows 的本机代理及 `/home/jack/.octopus/proxy.env`，参见 local-wsl.md |
| 本地 MCP 回调出现代理 502 | 检查 `NO_PROXY` / `no_proxy` 明确包含 `127.0.0.1,localhost,::1` |
| 想用手机上传的文件 | 使用聊天附件入口；`/showme` 的路径指向服务器文件系统 |
