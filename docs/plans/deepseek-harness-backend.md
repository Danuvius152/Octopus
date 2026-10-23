# DeepSeek Harness 作为第三个 backend：实施计划

> **Status:** proposed — 以 API key + `dsh --profile headless` 接入 DeepSeek Harness（DSH），复用现有 harness 边界；先交付单轮/事件/MCP/resume，账号登录与 SDK 长驻模式留在后续。

本计划回答三件事：怎样把 DSH 接进 Octopus、登录只用 API key 时每个环节怎么走、以及 DSH 的 skills 在本地怎样被发现与同步。文中 DSH 事实来自本机安装的 `@deepseek-ai/dsh@0.2.1-rc.2`（随包 README 与类型定义）；Octopus 事实来自当前工作树（`before-takeover` 之后、含未提交改动）。

## 1. 目标与非目标

**目标**

- 新增持久化 backend 值 `deepseek`，与 `claude-code`、`codex` 并列，走同一个 `Harness` / `HarnessRun` 引擎。
- 认证只做 **API key**：复用现有 `POST /api/credentials`（`auth_type=api_key`）与加密存储，spawn 时注入 `DEEPSEEK_API_KEY`。
- 能力对齐现有两后端中可复用的部分：单轮执行、事件流（文本/思考/工具调用）、resume、MCP 工具、模型与 reasoning effort、一次性调用（`run_oneshot`）、按账户隔离的 `DSH_HOME`。
- 前端最小改动：为 `deepseek` 提供 API key 录入入口、后端标签与模型短名单。
- 文档、契约、hermetic + live 测试齐全，纳入 `./scripts/check.sh`。

**非目标（本计划不承诺）**

- DeepSeek 账号（`deepseek-account`）浏览器登录；本计划明确关闭该路由。
- `dsh --profile sdk` 的 JSON-RPC 长驻模式、token 级流式输出、mid-turn steering。
- 原生 subagent 卡片、原生 transcript 的 fork/复制、`handoff`/`pull` 导入导出。
- 为 DSH 单独实现一套权限/审批协议；沿用 Octopus 自己的 MCP 提问与工作区约束。

## 2. 已核实的 DSH 约定（0.2.1-rc.2）

| 主题 | 事实 |
| --- | --- |
| 入口 | `dsh --profile headless [launcher flags] <app flags> "task"`；stdout 只放结果/事件流，诊断走 stderr；退出码 0 完成、1 失败。 |
| 事件流 | `--json` 输出 NDJSON：`session`、`status`（`turn_start`/`step_start`/`step_end`/`turn_end`）、`text`、`thinking`、`tool_call`、`tool_result`、`final`、`error`。文本与思考在 **step 提交时**产生，不是逐 token；除 `final` 外每个字符串上限 8 KiB。 |
| resume | `--session-id <id>` 精确续接已持久化 Session；id 不存在、cwd 不一致、属于 subagent/fork、或 preset 不匹配都会在开跑前失败。每次调用的新会话 id 由开头的 `session` 事件给出。 |
| 凭据 | headless profile 默认禁用 `credentials`/`authorization`/`deepseek-account`；API key 路由读 launch env `DEEPSEEK_API_KEY`，再读 `<cwd>/.env`、`$DSH_HOME/.env`；GUI 保存的登录被忽略且不改动其文件。 |
| MCP | `dsh-mcp-client` 支持 `transport: stdio | streamable-http`，HTTP 支持 `url` + **自定义 `headers`**、`toolCallTimeoutMs`、`failOnStartupError`、重连参数；工具名为 `mcp__<serverName>__<tool>`。 |
| 系统提示 | `system-prompt` 的 `personaPrefix`/`personaSuffix`（支持 `{{variable}}` 模板）；`includeHarnessIdentity` 默认带固定开场。 |
| 项目指令 | `agent-instructions` 会把项目根的指令文件读进系统提示（`maxBytes: 65536`）。 |
| 模型 | `agent-default-model { provider: deepseek-official, model: deepseek-flash }`；内置目录含 `deepseek-flash`、`deepseek-v4-pro`；`llm-deepseek` 有 `reasoningEffort`。 |
| 沙箱 | `sandbox-policy.mode`：`read-only`（默认）/ `workspace-write` / `danger-full-access`；`fs-sandbox`、`bash-sandbox` 消费该策略。 |
| 覆盖层 | launcher 的 `--patch <file>` 是 YAML 覆盖层，叠在 bundle 与 profile patch 之后；`!!js` 可读 `process.env`。 |
| Home | `DSH_HOME`（默认 `~/.dsh`）存放 profiles、sessions 与 credentials；`DSH_AGENTS_HOME`（默认 `~/.agents`）是共享 agent 配置根。 |
| Skills | 扫描根：`<projectRoot>/.dsh/skills`（100）、`<projectRoot>/.agents/skills`（200）、`customSkillDirs`（300）、`<DSH_HOME>/skills`（400）、`<DSH_AGENTS_HOME>/skills`（500）；`projectRoot` 是最近的含 `.git` 祖先。 |
| 安装形态 | npm 包只发布 `lib/*.js` 与类型，**不含**仓库根 `.agents/`；随包 skills 只存在于 `dsh-agent-preset/skills`（rank 600）。 |

## 3. 架构映射

| Octopus 概念 | DSH 对应物 | 复用方式 |
| --- | --- | --- |
| `RuntimeProfile.build_turn_argv` | `dsh --profile headless --patch … --json [--session-id …] "prompt"` | 新写约 60 行，结构照抄 [codex.py](../../server/harness/codex.py) 的 `build_turn_argv` |
| `EventParser` | 上述 NDJSON | 新写 `DeepSeekEventParser`，约 150 行 |
| resume id | `--session-id`；`session` 事件返回 id | 存现有 `sessions.claude_session_id`（本来就是通用 resume 列） |
| `McpServerEntry` | `dsh-mcp-client` 行 | patch 的 `insert` 列表；HTTP + `Authorization: Bearer` 比 Codex 更直接 |
| `TurnContext.system_prompt` | `system-prompt.personaPrefix` | 逐轮写进 patch |
| `credential_style="env_secret"` | `DEEPSEEK_API_KEY` | 沿用现有解密/解析路径，profile 内注入 |
| `WebCapability` | `web_search` + `web_fetch` | 深度研究的 research leaf 可开 |
| `run_oneshot` | `dsh --profile headless`（默认只打印最终答案） | `parse_oneshot_stdout` 直接读 stdout |
| 账户隔离 | `DSH_HOME` | 每账户 `<users_root>/<uid>/dsh` |

## 4. 代码改动清单

### 4.1 新文件 `server/harness/deepseek.py`（约 600 行）

按 [codex.py](../../server/harness/codex.py) 的骨架组织：profile 值、argv 渲染、事件解析、一次性调用、模型目录、错误分类。

**Profile 字段取值**

```python
DEEPSEEK = RuntimeProfile(
    backend="deepseek",
    binary="dsh",
    credential_style="env_secret",
    tools_prompt=_OCTOPUS_SYSTEM_PROMPT_DEEPSEEK,  # 本节 4.1.4
    stdin_mode=StdinMode.CLOSE_AFTER_SPAWN,
    build_turn_argv=build_turn_argv,
    new_event_parser=DeepSeekEventParser,
    build_oneshot_argv=build_oneshot_argv,
    parse_oneshot_stdout=parse_oneshot_stdout,
    model_settings=read_model_settings,
    web=WebCapability(tool_names=("web_search", "web_fetch"), combined=False),
    injects_memory_prompt=True,      # 与 Codex 一样用文件工具读写 memory 目录
    premature_exit_recovery=False,   # 无 hold 进程，无 Claude 的提前退出补偿
    can_fork=True,                   # 仅 HISTORY_REPLAY；无 native copy
    fork_prepare=_fork_prepare_replay,
    auth_error_patterns=_DSH_AUTH_PATTERNS,
    transient_error_patterns=_DSH_TRANSIENT_PATTERNS,
    stale_session_patterns=_DSH_STALE_PATTERNS,
    models=("deepseek-flash", "deepseek-v4-pro"),
    login=None,                      # 没有 CLI 登录；API key 走 credentials 路由
    transcript_codec=None,           # 无 handoff/pull
)
```

**4.1.1 `build_turn_argv(ctx)`**

1. 在 `settings` 的运行时目录（见 4.2）写一份 `turn-<run_id>.yml` 覆盖层，`0600`；内容见 §6。
2. 组装 argv：

```
dsh --profile headless --patch <patch.yml> --json [--session-id <ctx.resume_id>] <prompt>
```

   launcher flags（`--profile`、`--patch`）必须在 app flags（`--json`、`--session-id`）和位置参数之前。
3. 环境：`os.environ.copy()`，然后
   - `env["DSH_HOME"] = paths_for(ctx.user_id).dsh_home`
   - `env["DEEPSEEK_API_KEY"] = ctx.credential.secret`（`credential_style=env_secret`；无凭据时不设置，让 DSH 报 `MISSING_CREDENTIAL`）
   - `env["OCTOPUS_MCP_TOKEN"] = <该会话的 MCP bearer>`（patch 里用 `!!js` 引用，不落盘）
   - `diagnostic_environment(env)`（沿用现有诊断脱敏）
4. 返回 `{"cwd": ctx.working_dir, "env": env}`；每轮结束删除 patch 文件（`try/finally`）。

**4.1.2 `DeepSeekEventParser`**

| DSH 事件 | 产出 |
| --- | --- |
| `session` | `HarnessEvent(type="session_started", session_id=sessionId)`；记住 id 供 `result` 使用；首帧 `activity=True` |
| `text` | `text`（整块，不产生 `text_delta`） |
| `thinking` | `thinking` |
| `tool_call` | `tool_use`：`tool_name=tool`、`tool_input=input`、`tool_use_id=callId` |
| `tool_result` | `tool_result`：`tool_use_id=callId`、`content=result`、`is_error=(status=="error")` |
| `status/turn_start/step_start` | 仅 `activity=True`（不产生事件） |
| `status/step_end` | 记下 `usage`，供 `result.raw` 与 TPS 监控 |
| `status/turn_end` | 记下 `reason`；**不**在这里发 `result`，等 `final` |
| `final` | `result`：`content=text`、`session_id=<captured>`、`duration_ms`、`raw={"usage":…, "reason":…}`、`end_of_stream=True` |
| `error` | `result`：`is_error=True`、`error_code=<message>`、`end_of_stream=True`（`--json` 的用法错误也会先发 `error` 且不发 `final`） |

退出码 1 且没有 `final` 时，由 `run.py` 的失败路径合成错误结果（与 Codex 一致）。

**4.1.3 `build_oneshot_argv` / `parse_oneshot_stdout`**

- 命令：`dsh --profile headless --patch <oneshot.yml> <prompt>`；`oneshot.yml` 禁用变更类工具（`tool-bash`、`tool-fs`、`tool-fs-search`、`tool-pwsh`、`tool-web`），`sandbox-policy.mode=read-only`。
- `parse_oneshot_stdout`：stdout 就是最终答案；去掉尾部换行、空输出按 `empty` 处理（`HarnessOneshotError`）。

**4.1.4 `tools_prompt` 与系统提示**

- 新写 `_OCTOPUS_SYSTEM_PROMPT_DEEPSEEK`：以现有 Codex 文案为底，去掉 Codex 专有说明（`spawn_agent`、`codex exec`），保留 `mcp__bg__*`、`mcp__ask__*`、`mcp__ask_agent__*`、`mcp__schedule__*`、`mcp__research__*`、`mcp__wiki__*` 的用法。
- 必须写明：内置 `ask_user_question` 已关闭，改用 `mcp__ask__user`（理由见 §8.2）。

**4.1.5 `read_model_settings` 与错误分类**

- `read_model_settings`：静态目录 + 已配置的短名单；不探测网络，不读取 DSH 登录态。
- `_DSH_AUTH_PATTERNS`：`missing_credential`、`invalid_credential`、`401`、`unauthorized`、`api key` 相关短语。
- `_DSH_TRANSIENT_PATTERNS`：`overloaded`、`5xx`、`econnreset`、`socket hang up`、`timeout`；DSH 自带的 `llm-retry` 已做 provider 级重试，这里只兜进程级故障。
- `_DSH_STALE_PATTERNS`：`no stored session`、`not found`（session 采用失败）、`working directory`、`agent preset`。

### 4.2 路径与设置

- [config.py](../../server/config.py)：新增 `dsh_home_dir: str = "~/.octopus/dsh"`。
- [workspace.py](../../server/workspace.py)：`UserPaths` 增加 `dsh_home`；`paths_for(None)` 返回 `_expand(settings.dsh_home_dir)`，账户态返回 `root / "dsh"`。
- [service_installation.py](../../server/service_installation.py)：`_DIRECTORIES` 增加 `"dsh_home_dir"`（安装标记、所有权与清理都覆盖它）；[tests/test_service_layout.py](../../tests/test_service_layout.py) 的 `legacy_directories` 同步增加。
- 目录首次使用时创建（`0700`），profile 首次 headless 运行会自动从随包模板初始化 `profiles/headless`。

### 4.3 注册与枚举

- [harness/__init__.py](../../server/harness/__init__.py)：`from .deepseek import DEEPSEEK` + `register(Harness(DEEPSEEK))`。
- [models.py](../../server/models.py)：`BackendKind` 增加 `deepseek = "deepseek"`。
- 重新生成前端契约（`cd web && bun run generate:contracts`），否则 `gate_contracts` 失败。

### 4.4 前端

- [HarnessPage.tsx](../../web/src/components/HarnessPage.tsx)：现有凭据对话框是 Claude（OAuth）与 Codex（device code）两态状态机；增加第三态：
  - `deepseek_key`：一个 label 输入 + 一个 `secret` 输入，提交 `POST /api/credentials`，body `{backend:"deepseek", auth_type:"api_key", label, secret}`；
  - 成功后 `upsertCredential`，与现有两条登录路径共用列表与绑定 UI；
  - 后端无 `login` 时不要显示“重新授权”按钮（现有 `needs_reconnect` 卡片逻辑按 backend 分支）。
- [modelOptions.ts](../../web/src/lib/modelOptions.ts)：`deepseek` 的短名单与默认模型。
- 后端标签/图标：`SidebarManage`、`AgentFormPage`、`HarnessPage` 的 backend 文案映射。

### 4.5 文档

- [architecture.md](../architecture.md)：harness 表的 `deepseek` 行（入口、凭据、resume、限制）。
- [AGENTS.md](../../AGENTS.md)：支持的 backend 与“Claude 未配置”同类说明；测试标记 `real_deepseek`。
- [local-wsl.md](../local-wsl.md)：`npm i -g @deepseek-ai/dsh`、`dsh --version`、`DSH_HOME` 目录、密钥放 `.env`/凭据页。
- 本计划自身进入 docs plans 索引（`gen-docs-index.py`）。

## 5. API key 认证（端到端）

1. **创建**：`POST /api/credentials`，`auth_type=api_key`、`backend=deepseek`、`secret`。现有路由已经支持（[credentials.py](../../server/routers/credentials.py)）；只是 `BackendKind` 需要新值，前端需要入口。
2. **存储**：`encrypt(secret, secret_key)`，与 Claude/Codex 同样的 master key → 账户 DEK。
3. **解析**：`_resolve_credential` 走 `env_secret` 分支，返回 `HarnessCredential(backend="deepseek", auth_type="api_key", secret=…)`；不需要新的 DB 列或迁移。
4. **注入**：profile 的 `build_turn_argv` 设 `env["DEEPSEEK_API_KEY"]`。
5. **DSH 侧**：headless 只看 launch env 与 `.env`；不触碰 `$DSH_HOME/.credentials.yaml`，也不启用账号路由。
6. **失效**：`MISSING_CREDENTIAL` / `INVALID_CREDENTIAL` / 401 → `Harness.is_auth_error` 命中 → 现有 `needs_reconnect` 流程；用户在凭据页重填 key（`POST` 新 key，或后续补一个 `PUT`；本计划先用“新 key + 删旧”）。
7. **删除**：`cleanup_credential` 无磁盘状态（key 只在 Octopus DB），返回即可；`DSH_HOME` 里的会话历史按账户生命周期清理，不随凭据删除。

**多租户**：`DSH_HOME` 必须按账户隔离（会话日志、profile 在本机共享会串号）。skills 根 `~/.agents/skills` 默认共享（它是 `~/.codex/skills` 的软链，见 §8.4）；若要求账户级 skills 隔离，spawn 时设 `DSH_AGENTS_HOME=<users_root>/<uid>/agents`。

## 6. 每轮命令与覆盖层模板

```sh
dsh --profile headless \
    --patch <runtime>/turn-<run_id>.yml \
    --json [--session-id <resume_id>] \
    "<prompt>"
```

```yaml
# <runtime>/turn-<run_id>.yml — 0600，运行结束即删
- id: system-prompt
  config:
    personaPrefix: |
      <Octopus 组装的 system prompt（persona + 工具说明 + 连接器）>
- id: agent-instructions
  disabled: true            # 不让 DSH 再读项目 AGENTS.md，避免双重注入
- id: sandbox-policy
  config:
    mode: danger-full-access   # 完整轮次与 Claude/Codex 对齐；research leaf 用 read-only
- id: agent-default-model
  config:
    provider: deepseek-official
    model: deepseek-v4-pro
    reasoningEffort: high
- id: tool-ask-user
  disabled: true            # headless 没有 answerer；改用 mcp__ask__user
- insert:
    - id: mcp-ask
      name: '@deepseek-ai/dsh-mcp-client'
      config:
        serverName: ask
        transport: streamable-http
        url: http://127.0.0.1:<port>/mcp/ask
        headers:
          Authorization: !!js '`Bearer ${process.env.OCTOPUS_MCP_TOKEN}`'
        toolCallTimeoutMs: <ask_user_question_timeout_seconds + 60>000
        failOnStartupError: true
    # bg / ask_agent / schedule / research / connectors / wiki 同样逐条 insert
```

要点：

- **patch 文件不含密钥**：MCP bearer 通过 `!!js process.env.OCTOPUS_MCP_TOKEN` 引用；API key 只在进程环境里。
- `serverName` 必须与 Octopus 的命名空间键一致（`ask`、`bg`、`ask_agent`、`schedule`、`research`、`wiki`、连接器名），否则 `mcp__<key>__` 工具名会变，提示词与历史全部失配。
- `toolCallTimeoutMs` 至少与 `ask_user_question_timeout_seconds + 60` 对齐（[assembly.py](../../server/harness/assembly.py) 已有同样的算法），否则提问会在 Octopus 侧回答前超时。
- `web_research=True` 的 leaf：`sandbox-policy.mode=read-only`，只保留 `web_search`/`web_fetch`，禁用变更类工具行。
- `personaPrefix` 走 `{{variable}}` 模板；若组装后的提示文本含 `{{`，必须转义或改用 `personaSuffix`，否则 DSH 组装会抛错。

## 7. 失败与恢复矩阵

| 条件 | 识别 | 行为 |
| --- | --- | --- |
| 没有 key | stderr `dsh: MISSING_CREDENTIAL: …` | `is_auth_error` → 标记凭据缺失，提示到凭据页填写；不重试 |
| key 无效 | `INVALID_CREDENTIAL` / 401 | `needs_reconnect` + 提示重新填写 |
| provider 过载/断连 | `overloaded`/5xx/timeout | `is_transient_error` → 现有有界重试（DSH 内部已重试一轮，Octopus 只在进程级再兜一次） |
| resume id 失效 | 采用失败：`no stored Session` / cwd / preset | `stale_session_patterns` → 丢弃 id，开新会话一次（与 Codex 同路径） |
| `--session-id` 被并发占用 | 采用失败：live id | Octopus 的 SessionOwner 已保证同会话串行；若仍出现，按致命错误上报，不重试 |
| `--json` 用法错误 | `error` 事件 + 无 `final` | 视为 harness 配置错误（我们的 argv 生成 bug），带 stderr 上报 |
| 进程超时/被杀 | 退出码 + 无 `final` | 现有 `run.py` 超时/取消路径 |

## 8. 与 DSH 特性相关的设计决定

### 8.1 流式输出

`--json` 的 `text`/`thinking` 在 step 提交时产生，**没有 token 级 delta**。因此：

- `text_delta` 不会出现，UI 文本按块到达；
- 不实现 `ActivityObserver`（无逐 token 活动信号），`activity` 只用 `turn_start`/`step_start`；
- 若要真正流式，走 `dsh --profile sdk`（JSON-RPC 长驻），那是后续独立计划。

### 8.2 提问

DSH 原生 `ask_user_question` 在 headless 没有 answerer（阻塞调用直接失败）。所以：

- patch 里 `tool-ask-user: disabled: true`；
- 系统提示明确“用 `mcp__ask__user`”；
- `mcp__ask__user` 是 Octopus 自己的长轮询 MCP 工具，与后端无关，回答路径完全复用。

### 8.3 沙箱

DSH 默认 `read-only`，与 Claude/Codex 的 full-turn 行为不同。映射：

| Octopus 场景 | `sandbox-policy.mode` |
| --- | --- |
| 普通 human turn / agent 自主轮次 | `danger-full-access`（与现有两后端对齐；账户边界由 Octopus 的 workspace/`extra_roots` 负责） |
| research leaf（`ctx.web_research`） | `read-only` |
| 一次性调用（标题/schedule 解析） | `read-only` + 禁用变更类工具 |

如果后续要把账户边界下沉到 DSH，可改用 `workspace-write`，但 DSH 的 workspace 是单一 cwd，无法表达 Octopus 的 `extra_roots`；本计划不这样做。

### 8.4 Skills：本地可用性与同步

**发现根（随包 README 原文顺序）**

| rank | 来源 | 路径 |
| --- | --- | --- |
| 100 | project | `<projectRoot>/.dsh/skills` |
| 200 | project | `<projectRoot>/.agents/skills` |
| 300 | custom | `Config.customSkillDirs` |
| 400 | user | `<DSH_HOME>/skills` |
| 500 | user | `<DSH_AGENTS_HOME>/skills`（默认 `~/.agents/skills`） |

`projectRoot` 是最近的含 `.git` 的祖先目录。

**GitHub 仓库 `.agents/skills` 会不会自动可用？不会。** 原因有三：

1. npm 包只发布 `lib/*.js` 与类型，不包含仓库根 `.agents/`；
2. 这些 skill 属于 `project` 根（rank 200），只有把 DSH 的 cwd 放进 `deepseek-harness` 仓库时才会被扫描；在 Octopus 里跑，`projectRoot` 是 `/home/jack/Octopus`；
3. 它们是 **DSH 仓库自己的开发 skill**（`dsh-code-review`、`dsh-pre-push-checks`、`cordis-*` 等 20 个），对 Octopus 没有意义，装进来只会污染目录。

**怎样让它们在本地/Octopus 里可用（如果确实要）**：clone 一次，`git pull` 就是同步。

```bash
git clone --depth 1 https://github.com/deepseek-ai/deepseek-harness.git ~/src/deepseek-harness
# 方式 A：配置 customSkillDirs（推荐，目录被 watch，git pull 后自动生效）
#   在 profile 的 cordis.patch.yml：
#   - id: skill-filesystem
#     config:
#       customSkillDirs: ['/home/<user>/src/deepseek-harness/.agents/skills']
#   --patch 覆盖层也可以，适合只给某轮/某后端启用。
# 方式 B：逐个软链到 ~/.agents/skills（该目录当前是 ~/.codex/skills 的软链）
ln -s ~/src/deepseek-harness/.agents/skills/dsh-code-review ~/.agents/skills/dsh-code-review
```

不要试图把整个 `.agents/skills` 目录软链进 `~/.agents/skills`：`~/.agents/skills` 本身已经是指向 `~/.codex/skills` 的软链，无法再嵌套并保留现有 skill。

**Octopus 接入后的自然状态**：DSH 的 `user-agents` 根就是 `~/.agents/skills`，与 Codex 当前使用的是同一份（本机 `~/.agents/skills -> ~/.codex/skills`）。因此**为 Codex 装好的 skill 会自动出现在 DSH 会话目录里**，不需要复制；新的/改名的/删除的 skill 由 provider 的 watch 直接生效，编辑正文无需重启。账户级隔离才需要设 `DSH_AGENTS_HOME`。

**升级 DSH 后的同步**：

```bash
npm i -g @deepseek-ai/dsh@latest     # 或锁定版本：@deepseek-ai/dsh@0.2.1-rc.2
dsh --version                         # Octopus 启动时也做一次兼容性探测
```

- CLI 与随包 skills（`dsh-agent-preset/skills`）随 npm 包一起更新；
- 仓库 `.agents/skills` 不随 npm 更新，需要 `git -C ~/src/deepseek-harness pull`；
- 本机目前 `dsh` 只解析到 npx 缓存（`~/.npm/_npx/.../node_modules/.bin`），不稳定；必须全局安装或用绝对路径。

## 9. 测试与门禁

**hermetic（默认门禁）**

- `tests/test_deepseek_harness.py`
  - argv 顺序：`--profile headless --patch … --json [--session-id …] "prompt"`；
  - patch 内容：persona、MCP 行、`tool-ask-user` 关闭、sandbox 模式、`toolCallTimeoutMs`；
  - **密钥不落盘**：patch 文本不含 key/bearer；
  - 环境：`DSH_HOME`、`DEEPSEEK_API_KEY`、`OCTOPUS_MCP_TOKEN`；
  - 事件解析：8 类事件、`final`→`result`、`error`→错误结果、usage 归属、session id 捕获；
  - 无 key / 错 key / 过期 id 的分类；
  - oneshot argv 与 stdout 解析。
- 假 `dsh` 脚本：注册到 `tests/hermetic_guard.py` 的 `allow_fake_executable`，按 argv 输出固定 NDJSON。
- `tests/test_deepseek_failures.py`：auth/transient/stale 三张表逐条。

**live（`real_deepseek` 标记）**

- `tests/conftest.py` 增加 `real_deepseek` 的 setup 探测：`dsh` 可解析 + `DEEPSEEK_API_KEY` 存在；否则 skip。
- 选择式：`pytest -m "real and real_deepseek"`。
- 用例：单轮文本、一次工具调用、一次 resume、一次 MCP 提问（用测试桩回答）、一次 oneshot。
- 不加入 `real_claude`/`real_codex` 的混合选择；本机 Claude 仍然不探测。

**门禁**

- `./scripts/check.sh`：ruff/mypy/hermetic/vitest/tsc/contracts/docs index 全绿；
- `bun run generate:contracts` 后提交 `web/src/api/contracts.ts`；
- 前端单测覆盖新的 API key 表单；
- 文档链接检查（本计划与 `architecture.md`/`local-wsl.md` 的相互链接）。

## 10. 交付顺序

| 票 | 内容 | 完成标准 |
| --- | --- | --- |
| T1 | `deepseek.py` 骨架：profile、argv、parser、oneshot、错误分类 | hermetic 测试绿；假 `dsh` 跑通一轮 |
| T2 | 注册/枚举/契约/前端标签 | `check.sh` 绿；后端出现在选择器 |
| T3 | API key 凭据：设置、`DSH_HOME`、前端录入 | 从 UI 建 key → 跑一轮真实模型 |
| T4 | MCP 覆盖层：ask/bg/schedule/research/wiki/连接器 + `mcp__ask__user` | 真实轮次能调用工具与提问 |
| T5 | resume、模型/effort、research leaf、失败矩阵 | 两次调用续接同一会话；leaf 只读 |
| T6 | 文档、live 标记、验收记录 | live 用例绿；文档索引同步 |

## 11. 风险与未决问题

1. **DSH 仍是 rc**（0.2.1-rc.2）：事件字段、patch 行 id、headless 采用规则都可能变。对策：文档锁版本；启动探测 `dsh --version`；parser 对未知事件容错（忽略但计数）。
2. **`--patch` 的解析位置**：launcher flags 必须在前；若未来 argv 语法改变，T1 的 argv 测试是第一道警报。
3. **`personaPrefix` 模板**：`{{` 会被当作变量插值；需要在生成 patch 前校验/转义。
4. **`agent-instructions` 双重注入**：默认读项目 `AGENTS.md`；本计划关闭。若用户希望 DSH 也读项目指令，改为开启并去掉 Octopus 自己的同内容段落。
5. **沙箱语义**：`danger-full-access` 是产品选择，需要在文档里写明与 Claude/Codex 对齐的理由；账户边界仍由 Octopus 负责。
6. **无 token 级流式**：这是 headless 的硬限制，不是 bug；UI 需要接受按块显示。
7. **`dsh` 安装路径**：必须全局安装或配置绝对路径，否则 `is_available()` 依赖调用者的 PATH（当前只在 npx 缓存里）。
8. **headless 一次一任务**：每个 turn 一个新进程，启动成本高于 Claude 的 held process；长任务靠 `--session-id` 续接，不能中途 steer。
9. **MCP 工具名稳定性**：`serverName` 必须与现有键一致；改动等于重命名工具，会破坏历史与提示词。
10. **账户级 skills 隔离**：默认共享 `~/.agents/skills`；需要隔离时用 `DSH_AGENTS_HOME`。

## 12. 明确不在本计划内

- `deepseek-account` 浏览器登录、`dsh-authorization` flow、把 `~/.dsh/.credentials.yaml` 复用到 Octopus；
- `dsh --profile sdk`（JSON-RPC 长驻）与真正的逐 token 流式；
- 原生 subagent 卡片与 `subagent.*` 通知；
- 原生 transcript 的 fork/复制、`handoff`/`pull`；
- 从 DSH 仓库自动同步 `.agents/skills`（是否接入由使用方决定，见 §8.4）。

---

**参考**

- [harness-layer.md](harness-layer.md) — 一个 `Harness` + 每后端一个 `RuntimeProfile` 的边界。
- [codex-backend.md](codex-backend.md) — 第二个后端落地时验证过的形状，本计划照此复制。
- [docs/architecture.md](../architecture.md) — harness 表与组件职责。
- DSH 本机包：`~/.npm/_npx/1e7f6d9597241db0/node_modules/@deepseek-ai/dsh/`（`README.md`、`headless`、`mcp-client`、`system-prompt`、`sandbox-policy`、`skill-filesystem`）。
