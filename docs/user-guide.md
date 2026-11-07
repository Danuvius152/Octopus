# Octopus 使用说明：本机启动、智能体协作与手机访问

本文针对 `/home/jack/Octopus` 这套 WSL + Codex 安装，按当前源码和本机配置整理。
手机访问采用[项目 README 推荐的 Cloudflare Tunnel](../README.md#quick-start)。

## 1. 当前安装状态

2026-10-30 核对结果：

| 项目 | 本机情况 |
| --- | --- |
| 运行环境 | Windows 上的 WSL / Ubuntu，已启用 systemd |
| 项目目录 | `/home/jack/Octopus` |
| 服务代码 | `/home/jack/.octopus/releases/current` 指向已发布工作树 |
| Python 环境 | 项目内 `.venv`，使用 pip |
| 默认 AI 后端 | Codex，Linux CLI 已使用 ChatGPT 账户登录 |
| 后台服务 | 用户级 `octopus.service`，已启用且正在运行 |
| 后端监听 | `127.0.0.1:8000`，`/health` 返回 `{"status":"ok"}` |
| Windows 访问 | <http://localhost:8000> |
| 前端 | 已构建，后台服务直接提供 `web/dist` |
| Octopus 登录 | 已有账户，电脑和手机均使用用户名、密码 |
| 手机隧道 | 已安装 `cloudflared`，独立的 `octopus-tunnel.service` 仅在明确要求时手动启动 |

这些是核对时的状态。隧道不自启、退出后不自动重启；当前是否开放公网入口，
以第 6 节的状态检查为准。

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

重启或停止服务前，先等执行中、排队和待回报任务自然结束，具体流程见
[本机服务管理](local-wsl.md#服务管理)。手机隧道由独立服务管理；Octopus
重启不会重建隧道，但后端停止期间公网入口无法提供业务服务。

### 停止、恢复处理与控制提示

**Stop** 会中断当前请求并暂停队列。停止确认成功后，确认条自动消失；失败或确认不明时仍保留提示。

| 操作 | 实际作用 |
| --- | --- |
| **Resume processing** | 恢复暂停队列的自动处理；不会重跑历史 unknown 输入。 |
| 暂停期间发送新指令 | 优先处理这条新指令，旧积压仍保持暂停；要继续旧积压，再点 Resume processing。 |
| **Restore to input** | 把已保存内容填回输入框；检查并点击发送后才创建新请求。 |
| Saved inputs 中的 **Dismiss notice** | 持久化隐藏这条历史提示；不恢复队列、不重跑输入、不证明原执行已退出。 |
| 控制命令提示中的 **Dismiss** | 关闭该条确认或错误提示，不重新发送命令、不恢复处理。 |

因此，是否继续处理暂停的积压由你明确操作；两种 Dismiss 都没有放行作用。
Resume processing 只解除队列暂停，实际执行仍须满足权限、配额和原执行资源退役等条件。
它不是强制跳过这些条件的按钮；退役完成也不会自行解除你用 Stop 设置的暂停。

### 重启后找回尚未处理的输入

浏览器消息确认接纳前，服务保存原文、附件引用和处理状态。服务重启不会自动
重跑这些消息；打开原会话，可在 **Saved inputs** 中查看：

- **Not executed · input saved**：尚未执行便撤回。点击 **Restore to input**
  填回输入框，检查或编辑后再点发送，才会产生新请求。
- **Execution result unknown**：可能已经执行，缺少确定结果。先用
  **Review saved input** 对照聊天记录，不提供一键重跑。

恢复会校验所需附件；附件缺失或变化时，可以恢复文字并重新附加完整文件。
不再需要提示时可点 **Dismiss notice**；原输入和 unknown 结果仍保留，隐藏不会解除执行阻塞。
输入随原会话保存，删除会话时一并删除。若旧执行仍未确认退出，新执行可能被阻止，
Resume 和 Dismiss 都不能绕过它。

受管服务完成启动清场和恢复后，旧代输入仍保留 unknown，但缺失历史进程信息不再
阻止新工作。同代执行仍须由原 Owner 确认资源退役；隐藏提示不会跳过这项检查。
非受管的 standalone 启动没有同样的清场证明，仍保留保守的退出检查。
这项修复须随已验证版本发布后生效，源码更新不表示运行服务已经部署。
升级前没有保存的排队原文无法补回；已保存的聊天历史继续保留。

## 3. 登录、工作目录和智能体设置

### 登录 Octopus

当前安装已有账户，电脑和手机都使用同一个用户名、密码登录。
管理员可以在 Account 页面创建邀请，供其他人注册独立账户。

全新空安装首次进入时，开户页才会要求配置中的 `OCTOPUS_AUTH_TOKEN` 作为
setup 证明，同时设置首个管理员的用户名和密码。证明只用于开户，不能登录
业务页面或访问 API；开户响应丢失后使用原账户登录。存在未归属的旧业务资料
时会拒绝开户，不能用“认领安装”绕过。配置来源见第 8 节，令牌不要写入文档
或提交到 Git。

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
把 [agent-prompts.md](agent-prompts.md) 中对应角色的内容（Octo/Vera 为英文，Weber 为中文）分别复制到
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
每个请求在装配开始时一次捕获会话与智能体默认值；之后的设置更新不会混入当前请求及其自动重试。
凭据删除、需重连或账户撤权仍在启动进程和输入投递前检查，当前快照不会延长授权。
强度候选项随当前模型变化，较高强度通常需要更多时间；`ultra` 还包含自动委派。

| 命令 | 用途 |
| --- | --- |
| `/remember <内容>` | 让当前智能体将内容保存到自己的长期记忆 |
| `/research <问题>` | 启动深度研究，最后生成带来源的报告 |
| `/showme <路径>` | 在网页中查看服务器上的文件 |
| `/schedule [时间] [任务]` | 设置定时运行；仅输入 `/schedule` 打开任务总览 |
| `/model [模型名]` | 覆盖当前会话的模型；不填写名称时打开选择界面 |
| `/compact` | 压缩空闲 Codex 原生上下文，不带参数；本阶段尚未整体发布 |
| `/rewind` | 从之前某条用户消息新建分支；界面可检查是否回退相关文件变更 |
| `/fork [名称]` | 复制会话上下文和整个工作目录，创建独立副本 |
| `/archive` | 归档当前会话并开始新会话 |
| `/reset` | 强制重置卡住的会话 |

`/model` 的名称必须是当前 Codex 后端可用的模型。`/fork` 会复制整个工作目录，
操作前注意是否包含大型依赖或数据目录。智能体长期记忆与 Obsidian vault
是两个独立位置；需要写入知识库时，应明确指定 vault 中的笔记路径。

`/compact` 保留聊天、附件和上传，只在确认接纳且原草稿匹配时清命令文字。
不支持或没有原生历史时明确拒绝；停止不保证回滚压缩，断连后只查询原请求。
当前源码只接入空闲 Codex 路径，Claude 和忙时压缩排队尚未接入。

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

本机使用独立的 `octopus-tunnel.service`，把现有 Octopus 服务接到公网。
**只有用户明确要求时才启动隧道**：不设置 Windows／WSL 自启，也不设置
进程退出后的自动重启。关闭操作终端不会直接停止已启动的隧道。

```text
手机浏览器 → 公网 HTTPS / Cloudflare Tunnel → WSL 的 127.0.0.1:8000 → Octopus
```

### 第一步：确认安装与服务

本机已将 Cloudflare 官方 Linux 二进制安装到 `~/.local/bin/cloudflared`。
安装、校验和用户服务定义统一见[本机手机公网入口](local-wsl.md#手机公网入口)。
日常使用不需要重新安装，也不需要修改 Octopus 的配置或重启后端。

在 Ubuntu／WSL 中检查：

```bash
~/.local/bin/cloudflared --version
systemctl --user status octopus --no-pager
systemctl --user status octopus-tunnel --no-pager
```

隧道尚未启动时，最后一条显示 `inactive` 是正常状态。

### 第二步：按需启动并获取网址

确认 Octopus 正常运行后，在需要手机访问时执行：

```bash
systemctl --user start octopus-tunnel
```

等待日志出现公网网址与 `Registered tunnel connection`。使用
[本机手机公网入口中的命令](local-wsl.md#手机公网入口)读取**本次启动**的网址，
避免复制到旧日志中的失效地址。成功时类似以下内容；示例域名不能直接使用：

```text
https://example-random-name.trycloudflare.com
```

若暂时没有网址，使用 `journalctl --user -u octopus-tunnel -f` 查看启动过程；
从日志查看器按 `Ctrl+C` 退出不会停止隧道。
同时可运行第 2 节的 `/health` 检查，确认 Octopus 本地服务正常。

### 第三步：在手机浏览器登录

1. 在手机 Safari / Chrome 打开日志给出的完整 `https://…trycloudflare.com` 网址。
2. 使用与电脑相同的 Octopus 用户名和密码登录。
3. 打开侧栏抽屉，选择智能体和会话；可以继续电脑上的同一会话。
4. 手机输入框的回车用于换行，点击发送按钮发送消息。
5. 可通过浏览器“添加到主屏幕”保存入口；网址变化后需要更新入口。

手机不需要与电脑处在同一个 Wi-Fi，也不需要安装 Codex。文件读写和代码
执行仍发生在电脑的 WSL 中。启用隧道后，可以先用手机访问同一网址的
`/ready`，再登录并确认能看到已有会话。后端仍监听 `127.0.0.1:8000`，
无需开放路由器端口或改为 `0.0.0.0`。

本机这个入口使用 **Quick Tunnel**：不需要 Cloudflare 账户或自有域名，生成
临时公开 HTTPS 网址。网址在隧道退出后失效，重新启动会变化；新的网址
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

### 项目内置入口与本机配置

README 的 `octopus serve --tunnel` 用于启用项目内置隧道。当前受管安装由
数据库旁的中央配置和服务 profile 绑定，普通 `serve` 会转交 systemd；直接
添加 `--tunnel` 会与已绑定的 `enable_tunnel=false` 冲突。仅修改开发目录
`/home/jack/Octopus/.env` 再重启服务不会启用当前受管服务的隧道。

本机保留内置隧道关闭，由独立用户服务连接现有 8000 端口。配置边界见
[本机服务管理](local-wsl.md#服务管理)。

### 关闭手机入口和长期网址

关闭公网入口时执行：

```bash
systemctl --user stop octopus-tunnel
```

关闭隧道不影响 Octopus 的本地服务和现有任务，电脑继续通过
<http://localhost:8000> 访问。以后只有再次明确要求并手动启动才开放入口。

需要固定域名时，应配置普通 Cloudflare Tunnel，并将其指向本机 8000 服务；
项目的 `--tunnel` 参数只创建临时隧道。普通隧道的域名配置见
[Cloudflare Tunnel 入门](https://developers.cloudflare.com/tunnel/get-started/)。

Quick Tunnel 不支持 SSE。按项目当前实现，主聊天界面使用 WebSocket；
Applications 中调用 `/apps/{id}/agent/chat` 的应用则使用 SSE，因此该类
应用的流式对话不适用 Quick Tunnel。需要该功能时使用普通 Cloudflare Tunnel。
这个限制来自 [Cloudflare Quick Tunnels 文档](https://developers.cloudflare.com/tunnel/get-started/quick-tunnels/)。

## 7. 更新后的部署与本地开发

普通使用只需启动服务。8000 端口提供的是
`~/.octopus/releases/current` 中已发布的后端和 `web/dist`；开发目录中的编辑
和构建不会直接更新正式页面。已授权的源码发布须完成相应检查，再按
[自动发布流程](local-wsl.md#自动发布)切换配套后端和静态资源，等待现有任务
自然结束。单独重启服务不能发布开发目录中的改动。

前端实时开发在开发目录 `web` 中执行 `bun dev`，电脑访问
<http://localhost:5173>。开发目录的本地构建命令为 `cd web && bun run build`，
工具链见 [local-wsl.md](local-wsl.md#本机测试)。手机隧道仍指向 8000 的
已发布服务；仅重启后端不会改变独立隧道的网址。

重装依赖、代理配置、完整测试和同步上游的本机细节见
[local-wsl.md](local-wsl.md)；同步之前应先保存已有源码改动。

## 8. 配置和数据在哪里

| 内容 | 本机路径或入口 |
| --- | --- |
| 当前服务配置、setup 证明 | `/home/jack/.octopus/octopus.db.service.env`，权限 `0600` |
| 安装身份与路径绑定 | `/home/jack/.octopus/octopus.db.service.json` |
| 开发配置 | `/home/jack/Octopus/.env`，不叠加到当前受管服务 |
| 主数据库：智能体角色、prompt、会话、定时任务等 | `/home/jack/.octopus/octopus.db` |
| 监控数据库 | `/home/jack/.octopus/octopus-metrics.db` |
| 用户服务定义 | `/home/jack/.config/systemd/user/octopus.service` |
| 服务的网络代理环境 | `octopus.service` 的原生 Environment，见 [本机服务管理](local-wsl.md#服务管理) |
| 隧道程序、用户服务 | `~/.local/bin/cloudflared`、`~/.config/systemd/user/octopus-tunnel.service` |
| 隧道的网络代理环境 | `~/.config/octopus-tunnel/proxy.env`，权限 `0600` |
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
| 页面显示旧界面或静态文件 404 | 核对 `releases/current` 的已发布版本及配套资产，按正式流程发布后刷新 |
| 手机网址失效 | 检查电脑、WSL、两个服务的状态；按第 6 节读取本次隧道的网址 |
| 隧道启动后没有网址 | 检查 `~/.local/bin/cloudflared --version` 和隧道日志；代理配置见本机手机公网入口 |
| 隧道中途退出、本地服务仍正常 | 查看隧道日志；仅在明确要求时重新 `start octopus-tunnel`，获取新网址，不重启 Octopus |
| 关闭终端后手机无法连接 | 检查 Windows 是否睡眠、WSL 是否退出；用户服务不能阻止 WSL 关闭 |
| 安装令牌不能登录 | 当前安装使用账户名和密码；setup 证明只用于全新空安装开户 |
| 新会话工作目录被拒绝 | 到 Account 的 Where you can work 添加实际目录，再使用 WSL 路径 |
| 提示找不到 `codex` 或 `bun` | 新开 Ubuntu 终端加载 nvm，或使用上文的 PATH 设置 |
| Codex 外网请求失败 | 检查 Windows 的本机代理及受管服务的实际代理环境，参见 local-wsl.md |
| 本地 MCP 回调出现代理 502 | 检查 `NO_PROXY` / `no_proxy` 明确包含 `127.0.0.1,localhost,::1` |
| 想用手机上传的文件 | 使用聊天附件入口；`/showme` 的路径指向服务器文件系统 |
