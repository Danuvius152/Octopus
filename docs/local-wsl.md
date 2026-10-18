# 本机 WSL 配置

本文负责本机环境与操作步骤，不追加逐次发布日志或测试项数。代码交付与纯文档
改动的动作边界统一遵循 [AGENTS.md](../AGENTS.md#repository-and-upstream-maintenance)；
Wiki 交付历史、证据及未完成需求分别见[开发总结](plans/wiki-development-summary.md)
和[未来计划](plans/wiki-future-plan.md)。

这台机器运行 WSL2 / Ubuntu 26.04，已启用 systemd。项目使用 Ubuntu 自带的
Python 3.14.4，满足 `pyproject.toml` 的 Python >=3.12 要求。虚拟环境通过
`python3 -m venv .venv` 创建，依赖用 pip 安装；本机不使用 uv。

AI 后端使用已经登录 ChatGPT 的 Linux 版 Codex。`.env` 设置
`OCTOPUS_DEFAULT_BACKEND=codex`，现有默认代理同样设为 `codex`。新建代理的
界面和 API 都采用这个默认值，新建普通会话继承代理的后端；继续使用当前
用户的 CLI 登录，无需复制或重新设置凭据。

本机当前没有可用的 Claude 环境；开发与发布的真实模型验证只使用 Codex。
这是本机配置与验证范围，项目仍支持两个后端，不修改其他使用者的后端选择。

## 配置和数据

- 开发目录：`/home/jack/Octopus`。
- 服务代码：`/home/jack/.octopus/releases/main-<commit>`，是已验证提交的独立
  Git 工作树；`/home/jack/.octopus/releases/current` 指向正在使用的版本。
  开发目录中尚未验收的改动不会随服务重启上线。
- 本地配置：项目根目录的 `.env`，权限为 `0600`，已由 Git 忽略。
- 数据库：`/home/jack/.octopus/octopus.db`。
- 监控数据库：`/home/jack/.octopus/octopus-metrics.db`。
- 初始工作目录：`/home/jack/.octopus/workspace`。创建账户后，项目按账户分配工作目录。
- 代理记忆、应用、附件、账户目录等持久数据：`/home/jack/.octopus/`。
- 用户服务：`/home/jack/.config/systemd/user/octopus.service`。
- 服务代码覆盖配置：`/home/jack/.config/systemd/user/octopus.service.d/runtime-release.conf`。
- 服务代理配置：`/home/jack/.octopus/proxy.env`，权限 `0600`。
- 服务时区：`Asia/Shanghai`。

Windows 浏览器访问 <http://localhost:8000>。
[WSL 官方文档](https://learn.microsoft.com/en-us/windows/wsl/networking)
说明了 Windows 通过 localhost 访问 WSL 中服务的方式。

用户的 `.wslconfig` 使用 `networkingMode=mirrored`、`dnsTunneling=true`、
`firewall=true`、`autoProxy=true`，这些设置保留。镜像网络允许 WSL 通过
`127.0.0.1` 访问 Windows 服务；`autoProxy` 让 WSL 继承 Windows 的 HTTP
代理。因而本机 API / MCP 回调需要下文的明确 `NO_PROXY` 规则。
DNS 隧道和防火墙分别控制域名解析与流量过滤，不会影响项目对安装令牌的校验。
参见 [WSL 配置说明](https://learn.microsoft.com/en-us/windows/wsl/wsl-config)。

首次进入时，使用 `.env` 中的 `OCTOPUS_AUTH_TOKEN` 登录，再在账户页面创建
自己的管理员账户；此后用账户名和密码登录。不要把 `.env` 提交到 Git。

## 环境重建

Ubuntu 的标准虚拟环境需要 `python3-venv` 软件包，本机已经安装。
前端使用 Linux 版 Node.js 24.21.0（nvm 管理）和 Bun 1.4.2。
`~/.bashrc` 已加载 nvm，默认 Node 版本为 24.21.0，同时包含 `~/.local/bin`
的 PATH 配置。新开 WSL Bash 终端即可直接使用 `node`、`bun` 和 `codex`；
当前交互终端可执行 `source ~/.bashrc` 生效。使用 nvm 加载默认版本，无需在
`.bashrc` 中写死带版本号的 bin 路径。

```bash
cd /home/jack/Octopus
python3 -m venv .venv
.venv/bin/pip install -e '.[test]'

cd web
bun install --frozen-lockfile
bun run build
```

Python 依赖及兼容边界统一在 `pyproject.toml` 声明，当前 FastMCP 接口使用
`mcp>=1.30,<2`，安装不需要额外的全量约束文件。接口迁移或更新依赖时，
在独立虚拟环境验证安装、`pip check`、服务导入、MCP 命名空间和账户隔离；
不要在运行服务共享的虚拟环境中试装。账户隔离结构检查同时支持直接路由和
新版 FastAPI 的 included-router context，检查嵌套前缀和继承的实际权限依赖。

本机使用 Windows 的 `http://127.0.0.1:7890` HTTP 代理。HTTP/HTTPS 的
大写和小写代理变量均已持久写入 `~/.bashrc`，新终端不依赖上一终端的环境。
服务使用同一代理地址，配置保存在 `proxy.env` 中。`~/.bashrc` 另补充明确的
`localhost`、`127.0.0.1` 和 `::1` 绕过规则，Codex 的外网请求继续走代理。
Windows 风格的 `127.*`
不能代替 HTTPX 所需的明确地址，否则 MCP 回调和应用后端请求可能收到代理的 502。

### 本地 Codex skills

本机通过 Codex 原生的用户级发现目录共享已安装技能：
`/home/jack/.agents/skills` 软链接到 `/home/jack/.codex/skills`。
首次重建时，目标路径不存在才创建，不覆盖已有技能目录：

```bash
mkdir -p "$HOME/.agents"
ln -s "$HOME/.codex/skills" "$HOME/.agents/skills"
```

Octopus 服务以 `jack` 运行，`HOME=/home/jack`，因此本地 CLI、已有和新建
Codex agent、普通新会话都使用同一份用户技能。凭据仍使用
`~/.octopus/codex/<credential_id>/` 作为独立 `CODEX_HOME`，不需要复制
认证、配置或会话文件。下一轮启动 Codex 即可发现，无需重启 Octopus 或新建
会话；已经运行的回合不必打断。框架和 agent prompt 无需添加技能清单。

技能由 Codex 按名称、描述及自身调用策略按需加载；`grilling` 可按任务匹配，
`grill-me` 的 `allow_implicit_invocation: false` 保留，需显式 `$grill-me`。
自动发现不意味着所有技能都应自动触发。目录、软链接与调用策略见
[官方 skills 说明](https://learn.chatgpt.com/docs/build-skills#where-codex-loads-local-skills)。

## 本机测试

发布所需检查与 Codex 选择条件以 [AGENTS.md](../AGENTS.md#after-every-code-change)
为准，不在这里复制一份测试清单或固定通过数。下面只补充 WSL 环境、凭据入口
和可选知识评测的使用方法；文档整理只做受影响链接、索引和差异检查，不触发
全套测试、构建或付费模型请求。

每次交付报告和审阅任务都应明确写出：Codex 的实际检查结果；Claude 当前
不可用、真实 CLI/浏览器测试未执行，不计为通过，也不作为本机待补发布门禁。
离线单元、协议与契约测试仍覆盖两个后端；它们不能代替 Claude 实机验证。

仓库锁定的 Playwright 1.58.2 尚未提供 Ubuntu 26.04 平台包，本机使用它的
Ubuntu 24.04 Chromium 构建，并已安装官方浏览器依赖。运行浏览器测试前指定
平台，同时明确绕过本机代理：

交互终端已通过 `.bashrc` 加载 PATH、HTTP/HTTPS 代理及本机绕过规则。
下面 PATH 和 NO_PROXY 的 `export` 用于不会加载 `.bashrc` 的非交互 shell；
systemd 服务通过自身的 PATH 和
`proxy.env` 获取这些设置。

```bash
export PATH="$HOME/.nvm/versions/node/v24.21.0/bin:$HOME/.local/bin:$PATH"
export NO_PROXY="${NO_PROXY:+$NO_PROXY,}127.0.0.1,localhost,::1"
export no_proxy="$NO_PROXY"
export PLAYWRIGHT_HOST_PLATFORM_OVERRIDE=ubuntu24.04-x64
```

Codex 实机测试按 AGENTS 的 marker 选择。其中 research 测试需要
目录式凭据，验证时将临时登录目录链接到既有选定 credential 的 `auth.json`，
并通过 `OCTOPUS_CODEX_HOME_DIR` 指定目录根；临时 DB 同时登记对应凭据元数据，
认证仍保留在原目录，不复制密钥或重新登录。当前不运行 Claude
或跨后端的真实模型测试；只有用户明确要求并具备环境后才扩展验证范围。

重跑 Codex 后端实机测试时，可以这样创建并清理临时凭据入口：

```bash
(
  cd /home/jack/Octopus || exit
  octopus_test_homes=$(mktemp -d) || exit
  trap 'rm -f -- "$octopus_test_homes/host"; rmdir -- "$octopus_test_homes"' EXIT
  ln -s "$HOME/.codex" "$octopus_test_homes/host" || exit
  OCTOPUS_CODEX_HOME_DIR="$octopus_test_homes" .venv/bin/pytest tests/ -v \
    -m 'real and (real_codex or codex_bin) and not real_claude and not knowledge_eval'
)
```

Wiki 自然问答使用独立的 `knowledge_eval` 标记，默认排除，不作发布必过项，
也不记为通过。只为具体质量问题显式运行，不能为补一条文档记录重跑。
下列命令会调用真实 Codex，使用隔离合成样本，不读取本人的笔记；输出使用
新目录，避免覆盖先前证据：

```bash
cd /home/jack/Octopus
octopus_knowledge_evidence=$(mktemp -d /tmp/octopus-wiki-evals.XXXXXX)
.venv/bin/pytest tests/test_wiki_query_real.py --run-knowledge-evals \
  --knowledge-eval-output="$octopus_knowledge_evidence" -q
```

保留源版本、实际读取、完整回答、终态及原始用量；Pytest 通过只说明执行
留证成功，`not_reviewed` 仍需语义评阅。资料变化后按当前版本判断，不能
用固定个人事实作 gold；累计 token 不相加，未知总量/费用不补造。

普通聊天入口评测使用真实 WebSocket `send_message` 和正常共享提示装配，不以
委托代替普通入口。预登记自然问题和源版本，评阅材料不随问题提交，不追加
逐题评阅提示，也不强制 Wiki 模式。准备 registration JSON，
包含 `source_root`、`source_manifest_sha256`、当前所选的 `credential_id` 和
一至四项 `questions`（每项为 `id`、`question`）。源清单摘要按
`tests.wiki_query_evidence.source_manifest` 的结果做 Unicode JSON 编码，
`sort_keys=True, separators=(",", ":")`，再计算 UTF-8 SHA-256。

```bash
.venv/bin/pytest tests/test_wiki_chat_real.py --run-knowledge-evals \
  --wiki-chat-registration=/path/to/registration.json \
  --knowledge-eval-output=/path/to/fresh-chat-evidence -q
```

测试会话固定 `gpt-6.1-sol/high`、正常速度和既有 credential 绑定，不改用户
代理默认，不回退其他登录，也不先发额外付费探针。报告保留隔离数据库、
认证宿主、文件访问及源绑定的实际限制；新 cwd/空 memory 不证明盲测。

## Wiki 文件事件与索引进程

同一 Wiki 索引由一个后端进程观察和写入；`observer.lock` 在进程持有索引期间
排除另一个进程独立打开 Store。冲突返回 503，不在第二个进程重放文件事件。
同进程因注册绑定更新而重叠的 Store 租约共享观察器与尚未提交的事件，最后一个
租约释放后才关闭。需要离线打开 Store 时，应先停止拥有该索引的后端。

当前代码使用 schema11，增加持久任务通知和所有者文件事件恢复；上个已验收
的简化基线使用 schema10。实际运行提交及 schema 以对应发布验收为准。
打开旧状态时在同一事务中升级相应账本，保留既有来源、
注册、选择和有限授权事实；事务失败则保持迁移前状态。文件事件协议仍为1：
先持久接收，再原子应用身份与索引，最后精确确认。
删除重建结束旧身份，已匹配重命名/原子保存沿用身份。索引失败保留事件重试；
接收中断、无法配对的移动或通知溢出会保留不确定状态并返回 503。应核对阶段
和证据，不能删除队列或用补偿扫描强行伪造 ready。

迁移到更高 schema 后，旧代码会拒绝打开新账本。恢复只使用支持当前
schema 及持久协议的已验证代码，或在保留现有状态的基础上前向修复；不额外
制作旧版兼容构建，不维护第二份知识库。不能仅放宽版本检查或恢复旧数据库
覆盖当前写入。停机期间的
补偿扫描只能观察最终文件，不能恢复未被观察的瞬时身份历史。实现细节与
证据边界见[开发总结 §4](plans/wiki-development-summary.md#4-当前运行合同)。

## 服务管理

```bash
systemctl --user status octopus
journalctl --user -u octopus -n 100 --no-pager
```

服务通过用户级 systemd 启动。WSL 关闭时服务也停止；它不会自动启动 Windows
或阻止 WSL 关闭。

服务的工作目录是 `releases/current`，入口是现有虚拟环境中的
`python -m server.cli serve`。这个入口从服务工作目录导入源码，避免 editable
安装的 `octopus` 入口重新导入开发目录中的未提交改动。服务工作树的 `.env`、
`.venv` 和 `web/node_modules` 复用开发目录中的本机配置与依赖；账户数据和
Codex 登录位置不变。修改这些共享配置或依赖仍会影响正式服务。

`systemctl --user stop octopus` / `restart octopus` 是变更操作；先等执行中、
排队和待回报任务自然结束，不能为发布 kill CLI 或取消用户工作。只需查看状态
或整理文档时，不执行它们。发布切换按下节完成，避免重叠服务动作。

本地开发的 `bun run dev` 使用 5173 端口；8000 提供服务快照的 `web/dist`。
开发目录改动不会直接出现在 8000，不能靠反复重启把未验收源码带入正式服务。

### 自动发布

本节只适用于已授权、完成必要本机检查并推送 main 的代码改变，按用户已授权
流程直接发布；用户本次另有指令时优先遵循。只提交本任务已验证的代码及必要
相关文档，排除并行工作，不能把整个脏工作树一起收进提交。

纯文档改动完成链接、索引和差异检查后留在工作区，不自动 stage、commit、
push、构建、重启或发布。用户明确要求文档提交时才提交，提交本身也不触发发布。

1. 为已验证提交创建 `releases/main-<commit>` 的独立工作树，连接本机配置和
   共享依赖，先在候选快照执行 `cd web && bun run build`，再运行
   `.venv/bin/python scripts/validate-release.py <候选快照>` 核验入口及其引用的
   JS / CSS。候选缺少资源时不能切换；保留当前快照用于回退。
2. 用不依赖当前模型回合继续存活的独立发布进程等待自然排空，包括主控会话、
   队列、CLI 和待交付的后台/委托回报。已有发布进程运行时只观察，不叠加切换
   或重启。切换前再确认目标和准入条件仍有效。
3. 原子替换 `current` 指向新快照，重启服务；确认实际进程工作目录与目标提交一致，
   `/health` 正常。候选中已构建并验证的前端产物随同一次切换生效。
4. 验证健康接口、登录页面、静态资源和真实浏览器，确认代码与构建产物属于
   本次提交；在服务快照运行
   `.venv/bin/python scripts/validate-release.py <服务快照> --base-url http://127.0.0.1:8000`
   比对实际 HTML / JS / CSS 的 SHA-256。验收失败时，只有旧构建仍兼容实际
   schema 和持久协议才可恢复旧指向并重启验证。更高 schema 迁移已提交时，
   不自动切回不兼容的旧构建，保留当前状态并采用兼容代码恢复或前向修复；
   不恢复旧 DB，不清理尚需诊断的代码快照。
5. 验收全部通过且旧工作树干净、没有进程继续使用后，用 `git worktree remove`
   退役旧源码，不强制删除脏工作树。账户、数据库、工作区、Wiki、登录及共享
   配置/依赖保留。记录实际部署提交与结果到相应任务产物；必要时更新现有文档，
   不为补验收记录再造一次文档提交或发布。

## 同步原作者更新

仅在用户要求同步原作者更新时，按 [AGENTS 的仓库规则](../AGENTS.md#repository-and-upstream-maintenance)
检查 `upstream`，保护未提交工作，读取上游差异后再合并。`origin` 是当前独立
维护仓库，GitHub fork 关系和 remote 的说明不在这里重复维护。

```bash
git fetch upstream
# 先检查差异并保护现有工作，再合并。
git merge upstream/main
```

保留 `.env`、持久数据和本地定制，按变更运行 AGENTS 要求的检查；有代码变化
的发布统一走上节流程。同步上游不顺带提交本地文档整理；没有运行代码变化时
不重启或发布。
