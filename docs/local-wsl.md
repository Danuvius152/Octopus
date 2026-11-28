# 本机 WSL 配置

本文负责本机环境与操作步骤；验证规则由 [testing.md](testing.md) 维护，代码交付
与纯文档改动的动作边界见 [AGENTS.md](../AGENTS.md#repository-and-upstream-maintenance)。

| 操作者任务 | 入口 |
| --- | --- |
| 定位配置、密钥和持久资料 | [配置和数据](#配置和数据) |
| 重建依赖、代理和本地 skills | [环境重建](#环境重建) |
| 放置临时分析、脚本和工作副本 | [临时任务目录](#临时任务目录) |
| 选择本机验证环境与凭据 | [PATH/代理](#path-与代理)、[离线目录](#离线目录与浏览器)、[live 凭据/网络](#live-凭据与网络) |
| 管理服务、手机入口或发布 | [服务管理](#服务管理)、[手机公网入口](#手机公网入口)、[自动发布](#自动发布) |
| 合并上游变化 | [同步原作者更新](#同步原作者更新) |

本页描述 WSL2 / Ubuntu、用户级 systemd 服务与 Codex 的配置目标。先用
`cat /etc/os-release`、`python3 --version`、`systemctl --user status octopus`
确认现场；登录、依赖和运行状态不能从旧安装记录推断。项目支持 Python >=3.12，
依赖约束的解析环境另有固定版本。虚拟环境用 venv 和 pip 管理。

默认后端由 `.env` 的 `OCTOPUS_DEFAULT_BACKEND` 决定；新会话继承 Agent 配置，
已有 Agent 不随环境默认值自动重写。模型登录须核对当前选定的 CLI home。

## 配置和数据

- 开发目录：`~/Octopus`。
- 服务代码：`~/.octopus/releases/main-<commit>`，是已验证提交的独立
  Git 工作树；`~/.octopus/releases/current` 指向正在使用的版本。
  开发目录中尚未验收的改动不会随服务重启上线。
- 开发配置：项目根目录的 `.env`，权限为 `0600`，已由 Git 忽略；受管服务使用下文的中央配置。
- 数据库：`~/.octopus/octopus.db`。
- 监控数据库：`~/.octopus/octopus-metrics.db`。
- 初始工作目录：`~/.octopus/workspace`。创建账户后，项目按账户分配工作目录。
- 代理记忆、应用、附件、账户目录等持久数据：`~/.octopus/`。
- 用户服务：`~/.config/systemd/user/octopus.service`。
- 手机公网入口：独立的 `octopus-tunnel.service`，只在用户明确要求时手动启动，
  不自启、不自动重启；安装与启停见[手机公网入口](#手机公网入口)。
- 旧部署的代码覆盖配置：`~/.config/systemd/user/octopus.service.d/runtime-release.conf`。
- 旧部署的代理环境文件：`~/.octopus/proxy.env`，权限 `0600`；首次受管安装时捕获其有效值。
- 服务时区：`Asia/Shanghai`。

账户的工作区、代理记忆、应用、附件、research 和 Wiki 状态位于
`~/.octopus/users/<user_id>/`。根目录同名路径属于创建账户前的布局；账户模式
仅保留仍有数据或工作目录引用的旧路径。数据库、安装配置、共享 CLI 凭据及发布
工具仍由安装管理。`.octopus-installation` 和旁置的 `*.installation` 只记录资源
所属的服务配置位置，用于阻止另一安装重复认领；有用资源的归属标记应保留。

新附件和长输入分别写入`users/<user_id>/attachments/<session_id>/`与
`users/<user_id>/large-prompts/<session_id>/`，不再因正常写入创建全局同名目录。
启动仍沿用现有归属检查，不扫描或搬运文件，不补写缺失标记。删除会话会关闭其
HTTP入口并删除行；原生转录、导出或已获准下载可能仍引用原绝对路径，因此原文件
在没有完整失效证明时保留，不按会话结束、TTL或缺行自动回收。

Windows 浏览器访问 <http://localhost:8000>。
[WSL 官方文档](https://learn.microsoft.com/en-us/windows/wsl/networking)
说明了 Windows 通过 localhost 访问 WSL 中服务的方式。

用户的 `.wslconfig` 使用 `networkingMode=mirrored`、`dnsTunneling=true`、
`firewall=true`、`autoProxy=true`，这些设置保留。镜像网络允许 WSL 通过
`127.0.0.1` 访问 Windows 服务；`autoProxy` 让 WSL 继承 Windows 的 HTTP
代理。因而本机 API / MCP 回调需要下文的明确 `NO_PROXY` 规则。
DNS 隧道和防火墙分别控制域名解析与流量过滤，不会影响项目对安装令牌的校验。
参见 [WSL 配置说明](https://learn.microsoft.com/en-us/windows/wsl/wsl-config)。

空安装首次进入时，在开户页填写 `.env` 中的 `OCTOPUS_AUTH_TOKEN` 作为 setup
证明，并设置管理员账户名和密码；此后用账户名和密码登录。证明未配置或存在
留存业务资料时拒绝开户，证明不能用于业务 API 或 WebSocket。开户响应丢失后
使用原账户登录；不要反复开户或更换密钥。不要把 `.env` 提交到 Git。

启动诊断 `RetainedLegacyInstallation` 表示发现无账户业务资料、旧交接凭据或
异常的状态容器。服务会在密钥与业务存储准备前拒绝；保全原数据库及其 WAL/
journal、配置、密钥和目录，使用原协议版本诊断现场。`complete` journal 也可能
是旧成功路径留下的记录，不能承诺再执行一次 resume 就会消失。新版本不搬移、
认领或清空尚未核验的输入。原协议的归属、文件效果及恢复责任全部核验收束后，
定向清退已无当前职责的旧请求原始凭据，不留永久兼容副本；仅有`complete`标签
不能证明这些条件成立。已有账户正常重启保留其memory、DEK、凭据与显式访问根。

启动诊断 `UnsupportedMainStorage` 表示主库标记或完整结构不匹配候选声明的
[当前完整格式](implemented/current-storage-format.md)。真正空库初始化当前格式；
有保留资料的旧库在密钥与 writer 前拒绝，启动不再补列、回填或串联旧版升级。
跨格式发布须先确认旧 writer 退休，再完成有限转换与完整保全核验；提交未知或
部分完成时保持业务关闭并沿原发布计划前向恢复。保留原库、既有 WAL、配置和
密钥，使用匹配输入的代码诊断；不要重置版本号、删列、清空记录或回灌旧库绕过
拒绝。密文、DEK、历史、配置及恢复关系仍须保全。
普通恢复仍会结算 pending/running 输入并保留 unknown 事实，不自动重放模型。

## 环境重建

重建前安装 `python3-venv`，通过 nvm 选择项目兼容的 Node.js，并准备 Bun。
用 `. scripts/toolchain-env.sh` 解析已安装工具，以 `node --version`、
`bun --version` 和解释器版本确认实际环境。下例使用约束解析环境 Python 3.14；
不要在运行服务共享的环境中试装。

```bash
cd ~/Octopus
python3.14 -m venv .venv
PIP_CONSTRAINT="$PWD/constraints-python.txt" .venv/bin/pip install \
  -c constraints-python.txt -c constraints-wiki.txt -e '.[test,dev]'
npm install --global lefthook@2.2.2
./scripts/setup-hooks.sh

cd web
bun install --frozen-lockfile
bun run build
```

依赖边界由 [pyproject.toml](../pyproject.toml) 声明；
[constraints-wiki.txt](../constraints-wiki.txt) 固定 Wiki 解析输入，
[constraints-python.txt](../constraints-python.txt) 固定运行、test/dev 和构建工具的
完整声明依赖闭包，解析环境为 CPython 3.14.4 / Linux x86_64。其他组合须
独立验证；约束不承诺跨平台相同 wheel 或位级相同构建，SQLite 和 Codex binary
仍按各自合同核对。

上面的验证安装用于新环境；运行安装把 `.[test,dev]` 换为 `.`，仍使用同一约束。
`PIP_CONSTRAINT` 使用绝对路径，使 pip 的隔离构建也消费该快照。不要在运行服务
共享的虚拟环境中试装；需先在独立环境重建，当前共享环境有差异时拒绝候选准备。
`scripts/python_environment.py check` 是只读核验，检查项目声明及所选 extra 的递归
依赖、marker、Python 要求和精确版本；无关宿主包只记入 `unselected_packages` 诊断。

更新依赖时，在独立全新 venv 中先按 `pyproject.toml` 和 Wiki 输入解析安装；安装
`.[test,dev]`，并安装约束生成所需的构建后端 `setuptools` 和 `pip`。用该验证环境
解释器执行：

```bash
python scripts/python_environment.py lock --project "$PWD" --python /path/to/verify/bin/python \
  --extra test --extra dev --build
```

然后用新约束分别重建第二个验证环境和 runtime-only 环境，执行 `pip check`、
服务导入及适用门禁；从验证环境运行 `check --extra test --extra dev --build`
检查验证安装，运行 `check --python /path/to/runtime/bin/python` 检查运行安装。
目标解释器只做 stdlib metadata 查询；要求解析工具属于调用者验证环境。保留完整
解析比较和候选实际消费证据。改变 Wiki 解析依赖还须重新核验原文/换行/坐标、检索
等价和受影响基线；接口迁移同时核对 MCP 命名空间和账户隔离。

代理地址从当前 unit 环境与 `~/.bashrc` 查询；不要用历史笔记覆盖现有设置。
服务使用受管 unit 的原生 Environment；旧 `proxy.env` 不作为当前加载来源。
大小写代理变量需一致，本机 API/MCP 回调明确绕过 `localhost`、`127.0.0.1`
和 `::1`。Windows 风格的 `127.*` 不能替代 HTTPX 所需的明确地址。

### 本地 Codex skills

本机通过 Codex 原生的用户级发现目录共享自有的独立技能：
`~/.agents/skills` 软链接到 `~/.codex/skills`。
首次重建时，目标路径不存在才创建，不覆盖已有技能目录：

```bash
mkdir -p "$HOME/.agents"
ln -s "$HOME/.codex/skills" "$HOME/.agents/skills"
```

Octopus 服务以当前用户及其 `HOME` 运行，因此本地 CLI 和 Codex agent
都能发现这些独立技能。凭据仍使用 `~/.octopus/codex/<credential_id>/`
作为独立 `CODEX_HOME`。插件的 marketplace、启用配置和缓存归各自
`CODEX_HOME`，终端 CLI 在 `~/.codex` 安装插件不会自动给 Octopus 凭据安装。
不需要复制认证或会话文件，框架和 agent prompt 无需添加技能清单。

Matt Pocock skills 通过原生插件安装。对实际使用的每个凭据执行以下命令，
先将 `<credential_id>` 换成目标凭据；终端 CLI 则选 `~/.codex`：

```bash
octopus_skills_home="$HOME/.octopus/codex/<credential_id>"
CODEX_HOME="$octopus_skills_home" codex plugin marketplace add mattpocock/skills
CODEX_HOME="$octopus_skills_home" codex plugin add mattpocock-skills@mattpocock
CODEX_HOME="$octopus_skills_home" codex plugin list --marketplace mattpocock --json
```

插件更新由 Codex 原生机制管理，也可用
`CODEX_HOME="$octopus_skills_home" codex plugin marketplace upgrade mattpocock`
刷新 marketplace。安装或更新后核对插件实际版本与技能发现结果；不把用户技能
入口固定链接到 `plugins/cache` 下的版本目录，也不再复制同一套 Matt skills
到独立技能目录。[作者安装说明](https://github.com/mattpocock/skills#installation-30-second-setup)
区分了自动更新的插件与手动维护的技能副本。

安装或目录调整后，新启动的原生 Codex 运行按当前目录与配置发现技能，
无需重启 Octopus；已经运行的回合不必打断。技能按名称、描述及自身调用策略
按需加载。插件技能使用 `mattpocock-skills:` 命名空间；显式调用例如
`$mattpocock-skills:grill-me`。自动发现不意味着所有技能都应自动触发。见
[官方 skills 说明](https://learn.chatgpt.com/docs/build-skills#where-codex-loads-local-skills)。

私有技能的当前内容、局部修改和插件版本以实际文件及发现结果为准。更新或
重装可能覆盖本地修改；本页不保存技能数量或某次安装验收结果。

## 临时任务目录

临时任务材料使用当前账户的 `~/.octopus/users/<user_id>/workspace/<task>/`。
唯一的仓库 `.scratch/<task>/` 例外是 grill 讨论、设计等文档及其派生规格、
票据和任务地图，现有这些文档保持原路径。grill 实施、验证产生的日志、脚本、
实验输出、下载源码、审查报告、patch 和 manifest 同样写入账户 workspace。
同一 grill 任务在两处使用相同任务名，以链接关联文档和工作产物，不复制文件。
需要隔离开发时，Git worktree 可放 workspace 任务目录的 `worktree/` 子目录；
正式代码仍在选定仓库或工作树内修改。目录选择、生命周期与票据格式见
[本地任务约定](agents/issue-tracker.md)。

新建普通临时任务、审查报告和隔离工作树均使用账户 workspace。已有非文档
产物若仍被活动进程、审查或恢复引用，先保留原路径，待可安全更新消费者时迁移；
新增产物立即按上述分工写入。
这里的目录选择不改变框架的账户路径、应用输出或受管发布目录。

下面的测试目录约束针对运行数据，而非源码存放位置：即使源码工作树位于
workspace，测试数据库、账户、服务安装资源和 CLI 输出仍须与正式安装隔离。

## 本机测试

测试命令与范围见 [testing.md](testing.md#test-coverage)，浏览器资源和服务复用见
[执行规则](testing.md#browser-ownership-and-execution)。本节只说明本机环境。

### 离线目录与浏览器

WSL 的 `/tmp` 是容量有限的 tmpfs。后端 pytest、浏览器入口和 Codex 实机验收
默认使用 `~/.cache/octopus-tests/` 存放临时运行数据。pytest 沿用自身的会话
编号和保留机制。直接调用 pytest 时，可用 `TMPDIR`、`TEMP` 或 `TMP` 指定临时
目录，也可用 `--basetemp` 指定 pytest 的 fixture 目录。

其他需要磁盘空间的检查可在同一缓存下创建独立运行根。手动运行真实 Codex 时，
将 CLI home 和 `TMPDIR` 放在同根下的平行目录。运行根须在受管安装及账户工作区之外。
复用既有依赖和浏览器，可保留缓存残留，并保留可定位的运行记录与审计。

仓库锁定的 Playwright 1.58.2 尚未提供 Ubuntu 26.04 平台包，本机使用它的
Ubuntu 24.04 Chromium 构建，并已安装官方浏览器依赖。运行浏览器测试前须完成
[PATH、代理绕过与 Playwright 平台设置](#path-与代理)。

### PATH 与代理

交互终端已通过 `.bashrc` 加载 PATH、HTTP/HTTPS 代理及本机绕过规则。
下面 PATH 和 NO_PROXY 的 `export` 用于不会加载 `.bashrc` 的非交互 shell；
systemd 服务通过 unit 的原生 Environment 获取 PATH 和代理设置；独立手机
隧道使用自己的私有代理文件，见[手机公网入口](#手机公网入口)。

```bash
. scripts/toolchain-env.sh  # 从仓库根目录执行，解析已安装工具
export NO_PROXY="${NO_PROXY:+$NO_PROXY,}127.0.0.1,localhost,::1"
export no_proxy="$NO_PROXY"
export PLAYWRIGHT_HOST_PLATFORM_OVERRIDE=ubuntu24.04-x64
```

### Live 凭据与网络

research 实机测试需要目录式凭据：临时登录目录链接既有选定 credential 的
`auth.json`，用 `OCTOPUS_CODEX_HOME_DIR` 指定目录根，并在临时 DB 登记对应
凭据元数据。后台任务可能继承服务的另一份登录，必须显式传入本次获授权验证选定的
`CODEX_HOME` 并核对最终链接目标，不读取或复制认证内容。失败时核对实际目录、
原模型结果和网络审计，不能仅凭退出码或只读接口成功认定通过或要求重新登录。
CLI 默认模型额度不足时，使用当前对话的凭据、模型和显式服务档位隔离验证，
不切换宿主登录或修改 agent 默认；默认实机 effort 与断言规则见
[验证政策](testing.md#markers-providers-and-isolation)。

实机期间不向 `auth.openai.com` 请求认证或刷新，也不申请设备码、新登录。
只链接已有 auth 文件不足以证明这一点：运行时须阻断认证域的上游访问，并核对
实际网络目标。临时转发代理可只记录 method/host/port，在转发前拒绝认证域，
同时为子进程设置大小写 HTTP/HTTPS/ALL_PROXY 与明确的 loopback NO_PROXY；
不记录认证头、请求正文或 TLS 内容。认证尝试或无有效登录时保留失败，不换用
另一份宿主登录。下例展示凭据与运行目录隔离，网络约束和审计需同时落实。

临时 CLI home 的 `config.toml` 显式使用 `cli_auth_credentials_store = "file"`，
使该轮只使用链接的选定登录文件；保留所需原配置，但不修改宿主配置或 Agent
默认值。实机可用性以实际模型执行、网络审计和测试通过数为准，pytest 全部
skip 的零退出码不能计为验收通过。

发布入口按[发布矩阵](testing.md#release-and-documentation-checks)选择 Codex
实机范围。常规发布执行两轮浏览器主路径，按影响加入相关能力或完整矩阵。
直接调用 `scripts/codex_live.py` 默认串行运行完整 Codex pytest 与浏览器主路径。
入口创建并清理隔离 home、凭据目录和临时目录，链接
所选 auth，生成显式 file-store 配置，并在转发前阻断认证域。file-store 的语义
见 [OpenAI 配置参考](https://learn.chatgpt.com/docs/config-file/config-reference)。
验收临时配置设置 `features.plugins = false`，避免每个新 Codex home 重复下载插件。
常规验收覆盖 Octopus 与 Codex 的协议及模型交互，账户插件安装与市场同步在此范围之外。
网络审计仅记录 method/host/port/转发结果，不解密 TLS；它证明经过本轮代理的
请求，不是操作系统级网络隔离。

输出目录必须尚不存在，保留 `result.json`、网络审计及已执行套件的原始报告/日志。
任一套件无实际通过项、有 skip/失败、认证域尝试、代理错误、缺失网络证据，
未结算的代理处理器，或源 auth/config 改变，入口均非零退出。CONNECT 的
200 响应发送并 flush 成功后才计入 `tunnels`；此后仅从客户端读取时的连接
复位，或向客户端发送时的断管／复位，单独记录为 `client_closed`，不计为
成功隧道。上游读写、握手及应答前的错误仍失败；只有 `client_closed` 而无
成功 CONNECT 也不能通过。

审计一旦记录会令本轮失败的首个事件，就关闭其他活跃转发并拒绝后续上游拨号。
主动停流引起的断开从属于首错。接入后识别出的认证请求在审计结算前独立记录。
pytest 在首个用例失败后停止。pytest 失败或审计已记录首错时，组合入口跳过
浏览器套件，保留已执行范围的报告。

命令输出和套件日志会显示各套件执行窗口内的双向字节数及总 MiB，
命令输出另有整轮合计。结构化用量分别在 `result.json` 的
`execution.<suite>.network_bytes` 和 `network` 中。指定一个或多个
`--pytest-target` 时，可据此测量所选实机用例的用量。

`result.json` 的 `network` 记录 `bytes_from_client`、`bytes_from_upstream`、
`max_bytes` 和 `stop_reason`。字节数在本地代理读到 CONNECT 隧道内的数据时累计，
包含 TLS 握手及触发阈值后拒绝转发的那段数据。直接运行入口时可用
`--max-mib <正整数>` 设置阈值，或通过 `OCTOPUS_CODEX_LIVE_MAX_MIB` 传给
入口和发布门禁。命令行优先。超出双向合计阈值的字节块会触发停流和
`budget_exceeded` 失败回执。这个阈值约束本地隧道转发，上游已缓冲的数据和
代理服务商的计费范围仍以实际用量为准。

套件结束后先停止接入，再冻结已启动处理器的未结算数并关闭其持有的连接，
最后统一判定网络结果。未结算数非零就保留失败，后续退出不会将该轮改判成功，
也不通过重试或延长等待消除这个事实。指定 `--pytest-target` 时，报告记录各目标
及原生 pytest 实际选中的用例。按需使用 `--suite pytest` 或 `--suite browser`
复验受影响一侧。`--model`、`--service-tier` 只改本轮隔离配置。
隔离副本解析 TOML 后，只覆盖本节指定的配置项，保留其他值。
引用键、多行指令、嵌套表及数组按 TOML 值处理。宿主原文件保持原样。

```bash
cd ~/Octopus
: "${CODEX_HOME:?Set CODEX_HOME to the current conversation's selected login}"
# 先按上文设置 PATH/代理/浏览器平台；此目录必须尚不存在。
.venv/bin/python scripts/codex_live.py --codex-home "$CODEX_HOME" \
  --output "$HOME/.cache/octopus-tests/codex-release-evidence"
```

可选 Wiki 测量、维护意图和自然问答命令见
[Wiki evaluation method](wiki-evaluation.md#wiki-evaluation-method)，仍使用上述隔离与网络约束。

## Wiki 文件事件与索引进程

同一 Wiki 索引由一个后端进程观察和写入；`observer.lock` 在进程持有索引期间
排除另一个进程独立打开 Store。冲突返回 503，不在第二个进程重放文件事件。
同进程因注册绑定更新而重叠的 Store 租约共享观察器与尚未提交的事件，最后一个
租约释放后才关闭。需要离线打开 Store 时，应先停止拥有该索引的后端。

格式拒绝或发布转换先查[当前存储格式与交接](implemented/current-storage-format.md)。
保留原 DB、WAL、配置和正文；账本包含持久权威，不能按缓存删除、补建或只改 marker。
本节处理文件事件与旧 intent 的操作者动作；Registry 的准入与恢复规则归
[Wiki 恢复合同](implemented/wiki-development-summary.md#恢复与效果核验)。

外部修改 intent 只接受当前 v2、source-binding protocol 1 及完整的操作字段。
遇到旧版、不完整或未知的 `external-change.json`，新读取器在写历史、同步和
清理前返回503，暂留原件。应暂停相关 writer，在原 vault 锁下用确实支持该记录
的已验证原协议核对账户、vault、根目录、原请求身份和文件效果。部分完成的效果
由原 owner 继续收束；已全部完成的先核验正文、身份、来源 head/proof、历史与
保留依赖，并确认没有待处理的文件事件或来源计划，再直接清退原件。不能仅凭
历史行标为 applied 就认定完成，也不要对已完整核验的效果再执行一次收尾。
最终须重读同一 intent，unlink 后 fsync 所在目录，并验证当前版本冷开不重放
效果。核验或收束未完成时保持拒绝；完成后删除退役原始记录，不保留旧解释器、
兼容归档或影子副本。当前领域事实及其仍需的历史、blob 和来源证明继续保留。

迁移到更高 schema 后，旧代码会拒绝打开新账本。恢复只使用支持当前
schema 及持久协议的已验证代码，或在保留现有状态的基础上前向修复；不额外
制作旧版兼容构建，不维护第二份知识库。不能仅放宽版本检查或恢复旧数据库
覆盖当前写入。停机期间的
补偿扫描只能观察最终文件，不能恢复未被观察的瞬时身份历史。实现细节与
证据边界见[Wiki 恢复合同](implemented/wiki-development-summary.md#恢复与效果核验)。

## 服务管理

```bash
systemctl --user status octopus
journalctl --user -u octopus -n 100 --no-pager
```

服务通过用户级 systemd 启动。WSL 关闭时服务也停止；它不会自动启动 Windows
或阻止 WSL 关闭。

服务的工作目录是 `releases/current`。受管 unit 使用现有虚拟环境中的
`python -m server.cli serve --service-process`，从服务工作目录导入源码，避免
editable 安装的 `octopus` 入口导入开发目录中的未提交改动。普通 `serve`
在发现受管安装后转交 systemd，不能代替 unit 的服务进程入口。

安装身份与中央配置分别保存在数据库旁的 `<db_path>.service.json` 和
`<db_path>.service.env`；配置文件权限为 0600，unit 只加载这一份 Settings
环境文件，并用 `OCTOPUS_ENV_FILE` 指向它。proxy、TZ、PATH 与 provider homes
由同一 unit 的原生 Environment 保留，UMask 也沿用实际配置。不再额外叠加
旧项目 `.env` 或 proxy EnvironmentFile，以免令牌轮换出现多个写入来源。
服务工作树的 `.venv` 和 `web/node_modules` 仍复用本机依赖；账户数据和
Codex 登录位置保持原绑定。共享依赖的改变仍会影响正式服务。

旧部署仍可能使用不带 `--service-process` 的入口和 release drop-in。首次发布
受管版本时，由私有发布流程接管：冻结旧 unit 的实际环境，在消费原进程的
单次切换许可后屏蔽自动抢跑，旧进程继续自然退出；确认退出后安装 profile、
中央配置及新 unit，最后启动同一候选。安装中断只沿原计划向前恢复，不能
单独覆盖入口、恢复旧数据库或让未完成安装的候选先运行。具体完成状态以
该次发布回执和实际 unit/profile 为准，源码或本文更新不表示服务已切换。

`systemctl --user stop octopus` / `restart octopus` 是变更操作；先等执行中、
排队和待回报任务自然结束，不能为发布 kill CLI 或取消用户工作。只需查看状态
或整理文档时，不执行它们。发布切换按下节完成，避免重叠服务动作。

本地开发的 `bun run dev` 使用 5173 端口；8000 提供服务快照的 `web/dist`。
开发目录改动不会直接出现在 8000，不能靠反复重启把未验收源码带入正式服务。

## 手机公网入口

本机使用 Cloudflare Quick Tunnel，将公网 HTTPS 请求转到现有的
`http://127.0.0.1:8000`。入口由独立的用户服务 `octopus-tunnel.service`
管理，Octopus 的内置 `OCTOPUS_ENABLE_TUNNEL` 保持 `false`，受管 profile
和中央配置也保持原绑定。开关隧道不重启后端、不改变现有账户和会话。

**只有用户明确要求时才启动隧道。** 本机不执行 `enable`，不配置 Windows、
WSL、登录或定时自启，进程退出后也不由 systemd 自动重启。已手动启动的服务
脱离操作终端运行；cloudflared 进程仍在时会自行重连网络，进程退出或 WSL
关闭后则需要再次明确要求并手动启动。关闭公网入口只停止这个独立服务。

### 安装与配置

隧道程序入口为 `~/.local/bin/cloudflared`；用 `--version` 确认实际版本。
下载来源见 [Cloudflare 下载说明](https://developers.cloudflare.com/tunnel/downloads/)。
下方版本和校验值是一组固定重建输入，升级时须一起替换并重新核验。
此方式无需 sudo；绝对路径启动不依赖交互终端 PATH。

如需重建这份安装，先将下面的 `<账户工作区>` 替换为 Account 页面显示的
workspace 绝对路径。升级时先查看官方发布与校验值，替换版本和摘要；安装前
先按下文停止隧道，结束公网连接。主 Octopus 服务继续运行。

```bash
(
  set -euo pipefail
  octopus_tunnel_task='<账户工作区>/cloudflared-install'
  mkdir -p "$octopus_tunnel_task" "$HOME/.local/bin"
  curl --fail --location --connect-timeout 15 --max-time 180 \
    https://github.com/cloudflare/cloudflared/releases/download/2026.10.6/cloudflared-linux-amd64 \
    --output "$octopus_tunnel_task/cloudflared-linux-amd64"
  printf '1d356dac769d0dbf62ebe588c4034824bbff98d5790c2f844cc99e0e2f57462d  %s\n' \
    "$octopus_tunnel_task/cloudflared-linux-amd64" | sha256sum --check
  install -m 0755 "$octopus_tunnel_task/cloudflared-linux-amd64" \
    "$HOME/.local/bin/cloudflared"
  "$HOME/.local/bin/cloudflared" --version
)
```

`~/.config/octopus-tunnel/proxy.env` 为隧道独有的代理环境文件，目录权限
`0700`，文件权限 `0600`。安装时只从实际 Octopus 服务进程提取
`HTTP_PROXY`、`HTTPS_PROXY`、`ALL_PROXY`、`NO_PROXY` 及其小写形式；
不复制整个服务配置、setup 证明、账户 bearer 或模型凭据。两种 `NO_PROXY`
均保留已有绕过规则并包含 `127.0.0.1,localhost,::1`，确保连接本地后端。
主服务代理发生变化时，需要同步检查这份独立配置；它不自动叠加开发 `.env`
或旧的 `~/.octopus/proxy.env`。

重建时先创建 `~/.config/octopus-tunnel/` 和权限 `0600` 的 `proxy.env`；
把实际有效的代理键逐行写入，形式为 `HTTPS_PROXY="http://127.0.0.1:7890"`。
绕过规则按上一段保留；无需联网代理时文件可为空。代理值不要写进 unit 或 Git。

`~/.config/systemd/user/octopus-tunnel.service` 的实际定义为：

```ini
[Unit]
Description=Octopus public HTTPS via Cloudflare Quick Tunnel
After=octopus.service

[Service]
Type=simple
WorkingDirectory=%h
EnvironmentFile=%h/.config/octopus-tunnel/proxy.env
ExecStart=%h/.local/bin/cloudflared tunnel --no-autoupdate --url http://127.0.0.1:8000
Restart=no
TimeoutStopSec=30s
UMask=0077
NoNewPrivileges=yes
```

该 unit 没有 `[Install]` 段，`is-enabled` 应报告 `static`，没有自启目标；
`After` 只规定启动顺序，不会自动启动隧道或后端。`--no-autoupdate` 禁止
独立二进制自行更新、重启。创建或修改后只校验和重载定义，不自动启动：

```bash
systemd-analyze --user verify "$HOME/.config/systemd/user/octopus-tunnel.service"
systemctl --user daemon-reload
systemctl --user is-enabled octopus-tunnel
```

### 手动使用与故障处理

先确认 `octopus.service` 运行且 `http://127.0.0.1:8000/ready` 返回 200。
需要开放公网入口且用户已经明确要求时执行：

```bash
systemctl --user start octopus-tunnel
systemctl --user status octopus-tunnel --no-pager
```

`start` 在服务已运行时不创建第二份。日志出现网址后，还须看到
`Registered tunnel connection` 并实际验证公网 `/ready` 返回 200。
下面只读取当前仍在运行的这次启动，服务已停止时不会输出旧网址；启动初期
尚未生成网址时，稍后再读日志。

```bash
if systemctl --user is-active --quiet octopus-tunnel; then
  octopus_tunnel_invocation=$(systemctl --user show octopus-tunnel --property=InvocationID --value)
  journalctl --user -u octopus-tunnel \
    "_SYSTEMD_INVOCATION_ID=$octopus_tunnel_invocation" --no-pager -o cat \
    | rg --only-matching 'https://[a-z0-9-]+\.trycloudflare\.com'
fi
```

手机打开此 HTTPS 网址，用现有 Octopus 账户名和密码登录。公网健康检查和
登录页可以匿名打开，业务 API、会话与 WebSocket 仍须账户身份。域名不是
登录凭据；不要把 setup 证明当作手机登录令牌。

查看日志：

```bash
journalctl --user -u octopus-tunnel -n 80 --no-pager
```

关闭公网入口：

```bash
systemctl --user stop octopus-tunnel
```

查看日志不会启动服务。退出日志查看器也不会停止服务。隧道异常退出时查看
原因，只有用户再次明确要求后才执行 `start`；不要为隧道故障重启 Octopus。
如果只停止后端，隧道进程仍保留，公网会暂时返回后端不可用；后端恢复后沿用
原网址。重启隧道则会生成新网址，原浏览器入口需要更新。

Quick Tunnel 无需 Cloudflare 账户或域名，提供临时网址，没有可用性保证，
不支持 SSE。主聊天使用 WebSocket；Applications 的 SSE 流式对话需要正式
Cloudflare Tunnel，固定域名也需要正式隧道。见
[Quick Tunnel 官方限制](https://developers.cloudflare.com/tunnel/get-started/quick-tunnels/)。
电脑、WSL 和服务须持续运行；systemd 不能阻止 Windows 睡眠或 WSL 退出，
保持 WSL 的方法见[用户指南](user-guide.md#电脑需要保持什么状态)。

## 自动发布

用于已授权、通过[发布门禁](testing.md#release-and-documentation-checks)的 main
代码交付；用户本次限制优先，纯文档动作见[根规则](../AGENTS.md#repository-and-upstream-maintenance)。
同一交付者负责提交、推送、上线和退役，使用一份验收记录。有效检查及审查直接复用；
版本、PID、路径等绑定由现有守卫核对，机制、所有权或协议变化才补受影响验证。
[提交时复用 hook 结果](testing.md#full-checks-before-code-commits)，无需再跑一遍门禁。

私有工具入口是 `~/.octopus/ops/publication/tools/README.md` 的“当前安装的发布入口”；
执行前读取现场文件核对适用版本，仓库文档不证明私有脚本已验证：
原生 quiesce 使用 `local-deploy.py`；历史 callback/packet 流程不适用于当前服务。
准备时同时确定上线、撤权和旧树退役的执行路径及权限；已有发布进程运行时只观察。
正常只回报开始与最终结果，实际失败或阻塞才中断说明。

1. **固定交付。** 核对验收范围与工作树，提交并推送；将提交号关联到已有验证记录。
   绑定本轮实际候选、原 PID/启动身份、unit 环境/profile、配置和通知会话。
   沿用受审模板与配置捕获入口，只更新绑定；旧凭据、请求和 permit 均不可复用。
2. **准备候选。** 创建 `releases/main-<commit>`，连接共享配置/依赖，在候选中构建
   一次；同候选已有有效产物直接复用。用验证环境运行
   `scripts/validate-release.py <候选> --python <实际 ExecStart 解释器>`，保存资产与
   Python 依赖闭包回执。漂移时保留原服务，环境修复走[环境重建](#环境重建)。
   运行本轮 `local-deploy.py preflight`，只读预检存储/资源，并固定旧树收尾计划、
   提前确认进程读取权限；复用工具入口，不临时编写退役脚本。
   跨格式发布需另绑定本轮库身份和获准的有限转换材料，并以源版声明做在线预检。
3. **自然交接。** 独立 publisher 继承原 unit 解析后的环境，等待会话、队列、CLI
   及后台/委托回报自然排空。材料准备完再启动并结束模型回合；启动后继续唤醒
   模型查询或补写材料仍会阻塞排空。原生入口交出本轮单次许可后，publisher
   才 mask 原 unit、等待原进程与启动任务退出，取得安装离线锁。
   quiesce 请求回执未知时保留其原发布凭据，并用同一请求恢复。即使源服务仍显示就绪，
   请求也可能稍后被接纳。
   在同一离线锁下完成获准的转换和候选核验，再原子替换 `current` 并启动匹配的后端与资产。
   切换 `current` 前落盘与本轮交接、候选及主库身份绑定的转换验收回执。转换结果未知时
   保持启动关闭，在同一次系统启动内用原发布计划 `resume`，由已提交的库状态决定后续
   操作。切换后恢复沿用该回执并校验当前格式，保留候选启动后已提交的写入。unit 的合法
   mask 由冻结计划校验，不列入要求字节不变的环境/profile 输入。
4. **验收与撤权。** 核对实际 PID/cwd/cgroup、配置、`/health`、`/ready`、登录页和
   真实浏览器；以同一 verifier 加 `--base-url http://127.0.0.1:8000` 比较线上资产。
   核对当前存储、消息及保存设置保全。按原 hash 精确撤销本次发布凭据，事务后用
   新数据库连接确认 `auth_tokens.revoked_at`，再删除原凭据并同步目录。
   `candidate-result.json` 只证明候选验收；包含撤权事实和原文件清退的
   `release-result.json` 才证明上线完成。通知接纳与实际到达分别核对。
5. **自动收尾。** publisher 在上线及撤权回执落盘后，自动复核旧树身份、干净状态、
   当前服务和进程/共享依赖/保存路径引用，以普通 `git worktree remove` 退役旧树，
   最终通知同时报告上线及退役结果。脏树、引用或检查不可读时保留旧树，写
   `old-source-retirement.json` 的具体阻塞；解决后只运行本轮 `local-deploy.py finish`。
   已有最终上线回执时，`resume` 也只补收尾，不重跑构建、门禁、取凭据或重启。
   回执补入原验收记录；无需二次委托、文档提交或发布。

存储格式变化才进入[有限交接合同](implemented/current-storage-format.md)；格式不变仍
做当前完整格式的只读核验。未知格式、提交未知或跨库部分完成时保持业务关闭，沿
同一计划按提交事实前向恢复；只有旧构建仍兼容时才可恢复旧指向，保留数据库及诊断
材料。交接、验收与恢复责任结束后，退出本轮专属转换材料。

布局变化或发现待退出的旧空壳时，用捕获的受管 Settings/provider 环境运行
`scripts/check-storage-layout.py`，仅清退本安装无数据、链接或保存路径引用的空壳
（独立维护用 `--prune`）。附件/长输入的旧 writer 必须先退休；账户资料、旧附件、
Wiki、登录和共享依赖保留，范围不扩展到其他工作树或测试缓存。

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
