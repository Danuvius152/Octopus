# 本机 WSL 配置

这台机器运行 WSL2 / Ubuntu 26.04，已启用 systemd。项目使用 Ubuntu 自带的
Python 3.14.4，满足 `pyproject.toml` 的 Python >=3.12 要求。虚拟环境通过
`python3 -m venv .venv` 创建，依赖用 pip 安装；本机不使用 uv。

AI 后端使用已经登录 ChatGPT 的 Linux 版 Codex。`.env` 设置
`OCTOPUS_DEFAULT_BACKEND=codex`，现有默认代理同样设为 `codex`。新建代理的
界面和 API 都采用这个默认值，新建普通会话继承代理的后端；继续使用当前
用户的 CLI 登录，无需复制或重新设置凭据。

## 配置和数据

- 项目目录：`/home/jack/Octopus`。
- 本地配置：项目根目录的 `.env`，权限为 `0600`，已由 Git 忽略。
- 数据库：`/home/jack/.octopus/octopus.db`。
- 监控数据库：`/home/jack/.octopus/octopus-metrics.db`。
- 初始工作目录：`/home/jack/.octopus/workspace`。创建账户后，项目按账户分配工作目录。
- 代理记忆、应用、附件、账户目录等持久数据：`/home/jack/.octopus/`。
- 用户服务：`/home/jack/.config/systemd/user/octopus.service`。
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
.venv/bin/pip install -e '.[test]' -c /home/jack/.octopus/python-constraints.txt

cd web
bun install --frozen-lockfile
bun run build
```

`python-constraints.txt` 保存本机验证过的 Python 依赖版本，不包含本地路径、
凭据或项目的 editable 安装记录。当前源码依赖 MCP 1.x 的 FastMCP 接口，
依赖声明中的 `mcp>=1.0` 会允许安装不兼容的 MCP 2.x；重装时保留这个约束文件。
上游迁移接口后，需要重新验证对应的依赖版本。

本机验证使用 FastAPI 0.136.3。FastAPI 0.137 起改变了路由列表的结构，
项目现有的账户隔离结构测试依赖此前的结构，因此同样通过本地约束固定。
参见 [FastAPI 0.137 的变更说明](https://fastapi.tiangolo.com/release-notes/#01370)。

本机使用 Windows 的 `http://127.0.0.1:7890` HTTP 代理。HTTP/HTTPS 的
大写和小写代理变量均已持久写入 `~/.bashrc`，新终端不依赖上一终端的环境。
服务使用同一代理地址，配置保存在 `proxy.env` 中。`~/.bashrc` 另补充明确的
`localhost`、`127.0.0.1` 和 `::1` 绕过规则，Codex 的外网请求继续走代理。
Windows 风格的 `127.*`
不能代替 HTTPX 所需的明确地址，否则 MCP 回调和应用后端请求可能收到代理的 502。

## 本机测试

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

cd /home/jack/Octopus
.venv/bin/pytest tests/ -v -m 'not real'
cd web
bun run test
bun run typecheck
bun run test:e2e:fast
bunx playwright test e2e/codex.spec.ts --workers=1
```

仅 Codex 的后端实机测试按 marker 选择
`real and (real_codex or codex_bin) and not real_claude`。其中 research 测试需要
目录式凭据，验证时使用临时目录中的符号链接指向现有 `~/.codex`，并通过
`OCTOPUS_CODEX_HOME_DIR` 指定；无需复制认证文件或重新登录。Claude 实机测试
需要另外安装并登录 Claude，本机按用户选择使用 Codex。

重跑 Codex 后端实机测试时，可以这样创建并清理临时凭据入口：

```bash
cd /home/jack/Octopus
octopus_test_homes=$(mktemp -d)
ln -s "$HOME/.codex" "$octopus_test_homes/host"
OCTOPUS_CODEX_HOME_DIR="$octopus_test_homes" .venv/bin/pytest tests/ -v \
  -m 'real and (real_codex or codex_bin) and not real_claude'
rm "$octopus_test_homes/host"
rmdir "$octopus_test_homes"
unset octopus_test_homes
```

### 已完成的验证

截至 2026-10-10，本机验证结果：

| 检查 | 结果 |
| --- | --- |
| 后端普通测试 | 1,320 项通过 |
| Codex 后端实机测试 | 9 项通过，无跳过 |
| 前端单元测试 | 302 项通过 |
| TypeScript 检查、前端构建、ESLint | 通过 |
| 纯界面 E2E 测试桶 | 67 项通过 |
| Codex 浏览器实机对话 | 1 项通过，无跳过 |
| OpenAPI 生成类型、文档索引 | 与源码一致 |
| Windows 访问服务 | `localhost:8000/health` 返回正常 |
| 实际服务界面 | 安装令牌登录、刷新后保持登录、新代理默认 Codex 均通过 |
| 清空继承环境的新 Bash 终端 | 自动加载代理与工具链，真实 Codex 请求返回 `PONG-CODEX` |

Claude 后端实机测试及依赖 Claude 的浏览器实机用例未运行。

验证过程中修复了安装令牌登录被身份接口拒绝的问题，以及 Codex 原生
fork 的 transcript 文件名与当前 CLI 不兼容的问题，并补充了回归测试。
研究取消界面测试使用受控的 REST / WebSocket 数据，不再依赖模型运行时间。

## 服务管理

```bash
systemctl --user status octopus
systemctl --user restart octopus
systemctl --user stop octopus
journalctl --user -u octopus -n 100 --no-pager
```

服务通过用户级 systemd 启动。WSL 关闭时服务也停止；它不会自动启动 Windows
或阻止 WSL 关闭。

## 同步原作者更新

本仓库 `Danuvius152/Octopus` 已解除 GitHub 的 fork 关系，独立维护；
提交和推送使用 `origin`。`upstream` 仍指向原作者的 `archeryue/Octopus`，
仅用于按需检查和同步原作者更新，保留这个 Git remote 不会恢复 fork 关系。
需要同步时，先保存自己的源码改动并检查上游差异，再合并到当前维护分支：

```bash
git fetch upstream
git merge upstream/main
```

合并时保留本机 `.env`、持久数据和定制行为，处理冲突后执行 `AGENTS.md`
要求的测试。部署有前端变化的更新时，按项目规定先重启后端、等待 `/health`
正常，再执行 `cd web && bun run build`，随后刷新浏览器。

本地开发使用 `bun run dev` 的 5173 端口；后台服务的 8000 端口提供构建后的
`web/dist`，源码修改需要重新构建才能在 8000 端口看到。
