# 本机 WSL 配置

本文负责本机环境与操作步骤；验证规则由 [testing.md](testing.md) 维护，代码交付
与纯文档改动的动作边界见 [AGENTS.md](../AGENTS.md#repository-and-upstream-maintenance)。

| 操作者任务 | 入口 |
| --- | --- |
| 定位配置、密钥和持久资料 | [配置和数据](#配置和数据) |
| 重建依赖、代理和本地 skills | [环境重建](#环境重建) |
| 放置临时分析、脚本和工作副本 | [临时任务目录](#临时任务目录) |
| 选择本机验证环境与凭据 | [本机测试](#本机测试)，门禁规则见 [testing.md](testing.md) |
| 管理服务、手机入口或发布 | [服务管理](#服务管理)、[手机公网入口](#手机公网入口)、[自动发布](#自动发布) |
| 合并上游变化 | [同步原作者更新](#同步原作者更新) |

这台机器运行 WSL2 / Ubuntu 26.04，已启用 systemd。项目使用 Ubuntu 自带的
Python 3.14.4，满足 `pyproject.toml` 的 Python >=3.12 要求。虚拟环境通过
`python3 -m venv .venv` 创建，依赖用 pip 安装；本机不使用 uv。

AI 后端使用已经登录 ChatGPT 的 Linux 版 Codex。`.env` 设置
`OCTOPUS_DEFAULT_BACKEND=codex`，现有默认代理同样设为 `codex`。新建代理的
界面和 API 都采用这个默认值，新建普通会话继承代理的后端；继续使用当前
用户的 CLI 登录，无需复制或重新设置凭据。

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

Ubuntu 的标准虚拟环境需要 `python3-venv` 软件包，本机已经安装。
前端使用 Linux 版 Node.js 24.21.0（nvm 管理）和 Bun 1.4.2。
`~/.bashrc` 已加载 nvm，默认 Node 版本为 24.21.0，同时包含 `~/.local/bin`
的 PATH 配置。新开 WSL Bash 终端即可直接使用 `node`、`bun` 和 `codex`；
当前交互终端可执行 `source ~/.bashrc` 生效。使用 nvm 加载默认版本，无需在
`.bashrc` 中写死带版本号的 bin 路径。

```bash
cd ~/Octopus
python3.14 -m venv .venv
PIP_CONSTRAINT="$PWD/constraints-python.txt" .venv/bin/pip install \
  -c constraints-python.txt -c constraints-wiki.txt -e '.[test,dev]'

cd web
bun install --frozen-lockfile
bun run build
```

依赖边界由 [pyproject.toml](../pyproject.toml) 声明；
[constraints-wiki.txt](../constraints-wiki.txt) 固定 Wiki 解析输入，
[constraints-python.txt](../constraints-python.txt) 固定运行、test/dev 和构建工具的
完整声明依赖闭包。该组合已在 CPython 3.14.4 / Linux x86_64 的全新验证及
runtime-only 安装核对 `pip check`、服务导入和实际闭包，并通过验证环境的 mypy
及账户/路由/WS 定向检查；不承诺跨平台相同 wheel 或位级相同构建。
其他组合须独立验证，SQLite 和 Codex binary 仍按各自合同核对。

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

本机使用 Windows 的 `http://127.0.0.1:7890` HTTP 代理。HTTP/HTTPS 的
大写和小写代理变量均已持久写入 `~/.bashrc`，新终端不依赖上一终端的环境。
服务使用同一代理地址，当前配置保存在受管 unit 的原生 Environment 中；旧
`proxy.env` 不是当前加载来源。`~/.bashrc` 另补充明确的
`localhost`、`127.0.0.1` 和 `::1` 绕过规则，Codex 的外网请求继续走代理。
Windows 风格的 `127.*`
不能代替 HTTPX 所需的明确地址，否则 MCP 回调和应用后端请求可能收到代理的 502。

### 本地 Codex skills

本机通过 Codex 原生的用户级发现目录共享已安装技能：
`~/.agents/skills` 软链接到 `~/.codex/skills`。
首次重建时，目标路径不存在才创建，不覆盖已有技能目录：

```bash
mkdir -p "$HOME/.agents"
ln -s "$HOME/.codex/skills" "$HOME/.agents/skills"
```

Octopus 服务以当前用户及其 `HOME` 运行，因此本地 CLI、已有和新建
Codex agent、普通新会话都使用同一份用户技能。凭据仍使用
`~/.octopus/codex/<credential_id>/` 作为独立 `CODEX_HOME`，不需要复制
认证、配置或会话文件。下一轮启动 Codex 即可发现，无需重启 Octopus 或新建
会话；已经运行的回合不必打断。框架和 agent prompt 无需添加技能清单。

技能由 Codex 按名称、描述及自身调用策略按需加载；`grilling` 可按任务匹配，
`grill-me` 的 `allow_implicit_invocation: false` 保留，需显式 `$grill-me`。
自动发现不意味着所有技能都应自动触发。目录、软链接与调用策略见
[官方 skills 说明](https://learn.chatgpt.com/docs/build-skills#where-codex-loads-local-skills)。

## 临时任务目录

新的一般临时任务使用当前账户的 `~/.octopus/users/<user_id>/workspace/<task>/`。
分析材料、规格票据、调试脚本、临时实验、交接和任务产物放在同一任务目录；需要
隔离开发时，Git worktree 可放其 `worktree/` 子目录。正式代码仍在选定仓库或
工作树内修改。具体生命周期与票据格式见[本地任务约定](agents/issue-tracker.md)。

`.scratch/` 和 `.cache/octopus-worktrees/` 不再作为新任务的默认目录；已有活动
任务先保留路径与引用，完成后再清理。`docs/review/` 仍可放当次审阅报告。
这里的目录选择不改变框架的账户路径、应用输出或受管发布目录。

下面的测试目录约束针对运行数据，而非源码存放位置：即使源码工作树位于
workspace，测试数据库、账户、服务安装资源和 CLI 输出仍须与正式安装隔离。

## 本机测试

测试命令、门禁和可选评测统一见 [testing.md](testing.md#test-coverage)：离线覆盖
Claude Code 与 Codex，日常实机仅 Codex；文档整理只做链接、索引和差异检查。

全量离线使用四个按文件分组的 pytest worker。WSL 的 `/tmp` 是容量有限的 tmpfs；
需要磁盘空间或运行真实 Codex 时，在 `~/.cache/octopus-tests/` 下创建本轮独立目录，
将 `TMPDIR` 指向它。不要放进 `~/.octopus` 或账户工作区：子目录仍继承受管安装归属。
浏览器公共入口自动使用这一缓存根，每轮隔离全部临时状态并在退出后清理运行目录，
审计回执单独保留；已安装依赖和浏览器仍复用。局部回归和 Codex 实机保持串行，
公共测试配置不自动开启并行。
CLI 登录目录与 `TMPDIR` 放在同一本轮运行根下的平行目录，避免辅助程序安装限制。

仓库锁定的 Playwright 1.58.2 尚未提供 Ubuntu 26.04 平台包，本机使用它的
Ubuntu 24.04 Chromium 构建，并已安装官方浏览器依赖。运行浏览器测试前指定
平台，同时明确绕过本机代理：

交互终端已通过 `.bashrc` 加载 PATH、HTTP/HTTPS 代理及本机绕过规则。
下面 PATH 和 NO_PROXY 的 `export` 用于不会加载 `.bashrc` 的非交互 shell；
systemd 服务通过 unit 的原生 Environment 获取 PATH 和代理设置；独立手机
隧道使用自己的私有代理文件，见[手机公网入口](#手机公网入口)。

```bash
export PATH="$HOME/.nvm/versions/node/v24.21.0/bin:$HOME/.local/bin:$PATH"
export NO_PROXY="${NO_PROXY:+$NO_PROXY,}127.0.0.1,localhost,::1"
export no_proxy="$NO_PROXY"
export PLAYWRIGHT_HOST_PLATFORM_OVERRIDE=ubuntu24.04-x64
```

research 实机测试需要目录式凭据：临时登录目录链接既有选定 credential 的
`auth.json`，用 `OCTOPUS_CODEX_HOME_DIR` 指定目录根，并在临时 DB 登记对应
凭据元数据。后台任务可能继承服务的另一份登录，必须显式传入本对话已核实的
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

重跑 Codex 后端实机测试时，可以这样创建并清理临时凭据入口：

```bash
(
  cd ~/Octopus || exit
  : "${CODEX_HOME:?Set CODEX_HOME to the current conversation's selected login}"
  octopus_test_base="$HOME/.cache/octopus-tests"
  mkdir -p "$octopus_test_base" || exit
  octopus_test_root=$(mktemp -d "$octopus_test_base/pytest.XXXXXX") || exit
  trap 'rm -rf -- "$octopus_test_root"' EXIT
  mkdir "$octopus_test_root/credentials" || exit
  ln -s "$CODEX_HOME" "$octopus_test_root/credentials/selected" || exit
  TMPDIR="$octopus_test_root" OCTOPUS_CODEX_HOME_DIR="$octopus_test_root/credentials" \
    .venv/bin/pytest tests/ --run-real -v \
    --ignore=tests/test_codex_login_real.py \
    -m 'real and (real_codex or codex_bin) and not real_claude and not knowledge_eval'
)
```

可选 Wiki 测量、维护意图和自然问答命令见
[Wiki evaluation method](testing.md#wiki-evaluation-method)，仍使用上述隔离与网络约束。

## Wiki 文件事件与索引进程

同一 Wiki 索引由一个后端进程观察和写入；`observer.lock` 在进程持有索引期间
排除另一个进程独立打开 Store。冲突返回 503，不在第二个进程重放文件事件。
同进程因注册绑定更新而重叠的 Store 租约共享观察器与尚未提交的事件，最后一个
租约释放后才关闭。需要离线打开 Store 时，应先停止拥有该索引的后端。

当前代码要求 Wiki 格式17、主库格式7；实际运行提交及格式以对应发布验收为准。每库只保留一个
`PRAGMA user_version` 当前标记，启动、注册及恢复打开时等值匹配，失配在写入前
拒绝。新空库直接建立完整当前结构；普通重开不解释或转换旧格式。完整存量结构、
来源、授权、任务、通知/ACK及文章证明由发布交接核验，格式变化须在旧 writer
退出后、新服务启动前完成有限转换，结构、数据和标记在同一原事务中提交。
保留原 DB、已有 WAL、配置和正文；索引库中的持久权威不能按缓存删除或补建。
失配时按原发布计划核验并前向继续，不能只改标记解除拒绝。
失败保持旧格式或已经完整提交的新格式；COMMIT 无法确认时按原存储合同退出70。
item 与内部写入 journal 的现有恢复职责、当前派生投影恢复仍保留。文件事件协议仍为1：
先持久接收，再原子应用身份与索引，最后精确确认。
删除重建结束旧身份，已匹配重命名/原子保存沿用身份。索引失败保留事件重试；
接收中断、无法配对的移动或通知溢出会保留不确定状态并返回 503。应核对阶段
和证据，不能删除队列或用补偿扫描强行伪造 ready。

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
证据边界见[Wiki 运行合同](implemented/wiki-development-summary.md#当前运行合同)。

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

### 手机公网入口

本机使用 Cloudflare Quick Tunnel，将公网 HTTPS 请求转到现有的
`http://127.0.0.1:8000`。入口由独立的用户服务 `octopus-tunnel.service`
管理，Octopus 的内置 `OCTOPUS_ENABLE_TUNNEL` 保持 `false`，受管 profile
和中央配置也保持原绑定。开关隧道不重启后端、不改变现有账户和会话。

**只有用户明确要求时才启动隧道。** 本机不执行 `enable`，不配置 Windows、
WSL、登录或定时自启，进程退出后也不由 systemd 自动重启。已手动启动的服务
脱离操作终端运行；cloudflared 进程仍在时会自行重连网络，进程退出或 WSL
关闭后则需要再次明确要求并手动启动。关闭公网入口只停止这个独立服务。

#### 安装与配置

2026-10-30 已安装官方 `cloudflared 2026.10.6` Linux amd64 二进制到
`~/.local/bin/cloudflared`，与官方发布的 SHA-256 校验一致。下载来源见
[Cloudflare 下载说明](https://developers.cloudflare.com/tunnel/downloads/)及
[该版本发布页](https://github.com/cloudflare/cloudflared/releases/tag/2026.10.6)。
此方式无需 sudo；使用绝对路径启动，不依赖交互终端的 PATH。

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

#### 手动使用与故障处理

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

### 自动发布

本节适用于已授权、完成本机门禁并推送 main 的代码交付；“发布”或“上线”即
授权执行，用户本次指令优先。提交范围及纯文档动作见
[根仓库规则](../AGENTS.md#repository-and-upstream-maintenance)。
在验证阶段完成[发布门禁](testing.md#release-and-documentation-checks)，复用有效
测试和审查，只补缺失或失效项，通过后直接进入以下流程。版本、PID、路径、配置
和摘要按本轮实际值捕获，由现有守卫核对；仅绑定变化不触发复审或全套重测，
排空机制、所有权或协议变化则须在验证阶段完成定向检查和必要审查。

准备连续执行：启动独立发布进程，从本地 packet 回执取得并立即执行本轮管理员
校验操作，随后结束模型回合，让发布进程自行排空、验收；不靠后台通知接续准备。
正常只回报开始与结果，缺项、失败或阻塞才中断说明，不重复请求已有授权。

1. 为已验证提交创建 `releases/main-<commit>` 的独立工作树，连接本机配置和
   共享依赖，先在候选快照执行 `cd web && bun run build`，再运行
   `/path/to/verify/bin/python scripts/validate-release.py <候选快照> --python /home/jack/Octopus/.venv/bin/python`
   核验 JS/CSS 引用及实际 ExecStart 解释器的依赖闭包；`--python` 以当轮 unit
   为准，不能猜用候选 `.venv`。prepare 由验证环境运行并记录验证解释器、Python
   回执与摘要；切换前、上线后及前向恢复复用该入口。比较资产与必要依赖事实，
   无关包诊断不参与门禁；漂移时保留服务，不在准备阶段向共享环境安装依赖。
2. 用不依赖当前模型回合继续存活的独立发布进程等待自然排空，包括主控会话、
   队列、CLI 和待交付的后台/委托回报。已有发布进程运行时只观察，不叠加切换
   或重启。切换前再确认目标和准入条件仍有效。
3. 原服务确认自然排空并交出单次许可后，原子替换 `current`，等待原进程自然
   退出，并确认 unit 没有剩余进程或启动任务。保持启动屏蔽，在此边界执行本次
   存量检查、必要的有限转换和当前格式完整核验；通过后才安装/解除屏蔽并幂等
   启动候选。任一库未准备、提交结果未知或跨库部分完成时保持业务关闭，按同一
   发布计划和实际提交事实前向继续。候选只接受[当前完整格式](implemented/current-storage-format.md)，
   不在启动时转换；不变格式也须只读完整核验。首次受管安装完成上文 unit/profile
   过渡，前端产物随候选一次切换。
4. 验证存活与就绪接口、登录页面、静态资源和真实浏览器，确认代码与构建产物属于
   本次提交及 MainPID、工作目录、cgroup、配置绑定，`/health` 为 ok、`/ready`
   为 ready。在服务快照运行
   `/path/to/verify/bin/python scripts/validate-release.py <服务快照> --python /home/jack/Octopus/.venv/bin/python --base-url http://127.0.0.1:8000`
   比对实际 HTML/JS/CSS 的 SHA-256。用本轮捕获的受管 Settings/provider 环境
   清理旧目录空壳，运行 `scripts/check-storage-layout.py` 确认；独立维护用
   `--prune`。只删除属于本安装、没有数据/链接/已保存工作目录引用的空壳，不递归
   强删或继承模型测试环境。全局 attachments/large-prompts 须在旧 writer 退休、
   账户新写入接管后，确认仅剩归属标记且无引用才清退整个空壳，不能只删 marker；
   后续上传、长输入、配置核验和重启不得重建它们。保留账户目录、旧附件及凭据。
   只读核对实际数据库、Wiki schema 与持久协议；验收失败仅在旧构建仍兼容时恢复
   旧指向并重启验证，否则保留现状前向修复。不回灌旧 DB、不清理诊断所需快照，
   保留历史与会话设置。通知接纳、实际交付和部署验收分别记录，不等待发布进程
   自身通知。
5. 验收全部通过且旧工作树干净、没有进程继续使用后，用 `git worktree remove`
   退役旧源码，不强制删除脏工作树。交接及发布验收、恢复责任全部完成后，
   同时退出本次有限转换和仅服务本次转换的测试材料；未完成时保留原计划及所需
   材料，不保留长期旧版支持链。账户、数据库、工作区、Wiki、登录及共享
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
