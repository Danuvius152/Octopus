# Octopus app-server 迁移规范：参考 okou，保留业务合同

**2026-10-22 · 修订7 · 设计候选，未实施，未运行真实 CLI 验收。**\
配套：[octopus-app-server-tickets-20261022.md](octopus-app-server-tickets-20261022.md)。本稿完整替代方案 B 的修订6；沿用 exec 的方案 A 仍是另一条路线，本次不切换部署。

> **2026-10-23 阅读说明：** 本文保留完整原生尾部方案的历史设计，未成为当前执行合同。
> 当前七票暂停及已交付状态以[最小机制执行计划](../plans/minimal-architecture-execution-20261020.md)为准。
> 基于本地 `3d793d3` 和 Codex 0.164.0 的新核对见
> [迁移可行性与实施设计](codex-app-server-feasibility-20261023.md)；其中范围 A 指
> 请求内 app-server 迁移，与本文旧“方案 A＝继续 exec”的编号不同。新设计区分协议
> 迁移和完整原生尾部，未授权实施或恢复暂停票；本文旧依赖与兼容范围不能直接照搬。
> 本文提到的未来 DSH SDK 也不构成选型决定；用户要求按适配度选择，当前
> [DSH 方案](../plans/deepseek-harness-backend.md)比较 ACP、SDK 与 headless。

## 1. 结论与本次修正

**可以将 Codex 交互路径迁至 app-server；最小可行结构是保留 Octopus 的 Harness/SessionOwner 公共接口，在内部增加 Codex RPC 实现和兼容事件映射。** okou 为这种接缝提供了具体参考，但其主 turn 完成合同不等于 Octopus 要求的完整原生尾部。

本次把三类工作分开：协议迁移；本项目已确定的请求身份/权限/资源改造；完整原生尾部的能力证明。三者分别验收，最终候选仍满足全部选定需求，不用先完成第一类来宣称整个目标完成。

| 修订6的选择 | 修订7的决定 |
| --- | --- |
| 每request最多主动补一次父报告 | 撤销硬上限。保留原需求：子结果可以引发后续原生处理与再委派，受原请求预算、Stop和真实完成边界约束。 |
| 固定关闭原生自动wake，再由框架报告 | 不预定此策略。先验证实际版本的原生续跑，再决定是否需要宿主补报告；只采用一条已证明的驱动路径。 |
| 先给所有后端建立通用driver模型 | 保留现有JSONL路径；Codex使用私有协议协作实现。仅抽取已有两种实现真正共享的义务。 |
| 每request独立宿主作为整体方案选择 | 保留，但依据是项目既有请求授权隔离决策，不是app-server协议强制要求。 |
| Claude result后idle、下一请求可截断旧尾部 | 明确这是旧行为的兼容基线及其局限；完整大改不能因新请求到来就截断仍归旧请求的已知原生工作。 |
| transport和强尾部一起描述为一项能力 | 分开报告验收；22只有原要求全部满足才完成，transport通过不自动允许默认切换。 |

**本稿不承诺任意版本、任意脱离管理的任务或无限工作流都能完成。** 所支持的原生功能、归属范围、结果交付与结束依据必须有实际版本证据；证据不足时保留缺口，不能关闭原有能力或改成单轮限制来伪装兼容。

## 2. 基线与 okou 的真实参考价值

本轮只读核验：Octopus main `85b5b3c99192eb9a85b6bd923cd1be856492ded0`，before-takeover `88d2284b2b2493001be2e945df375d7a228ccc1f`；okou main `328c79f29a81e44e454ff155cd2416e78ea7860d`。Codex协议研究沿用官方源码基线 `2f3fb111be71d277c80bd5b96d9019748b921f5a` 并核对官方文档；这些基线不代表用户安装的CLI版本。

### 2.1 采用其设计方法

| okou的做法 | Octopus采用方式 |
| --- | --- |
| 稳定CLI facade；Codex RPC与Claude/Pi各有私有执行路径 | 保留Harness入口、Owner和业务消费者，把协议差异收在运行适配边界。 |
| app-server通知映射成原事件形状，消费者不解析原始RPC | 产出现有HarnessEvent、工具身份、错误和usage合同；必要原生执行事实单独保留，不能被展示映射丢掉。 |
| 每次执行创建CLI子进程，历史恢复与进程寿命分开 | 一个request attempt独占宿主，跨request按既有隔离计划创建新lease和进程；不引入宿主池。 |
| RPC client、兼容映射、执行接线分别验证 | 先用协议fixture验证客户端和映射，再接实际运行；用真实CLI验证fixture对应的能力。 |
| 只对实际跨版本消费者和持久数据承担兼容义务 | 列出旧浏览器、API/WS、旧历史和恢复候选等可达组合；同构建内部不增加版本协商。 |
| 兼容桥有具体保护对象和移除条件 | 保留有实际用途的路径，删除已被替代的writer；不增加永久exec自动兜底。 |

这些是结构与实施方法，不引用或移植外部框架代码。[K1][K2][K3]

### 2.2 不从其实现推导更强保证

核查的okou版本以匹配的主thread/turn终态结束本次执行，过滤其他thread通知，然后关闭宿主。它能展示主线程中的协作项，不等于发现、等待和汇总整个原生任务树；相关测试还明确验证了其他thread通知被过滤。它不能作为Ticket22强尾部已经解决的证据。[K1]

其他不能直接照搬的选择：统一拒绝原生server request；沙箱专用认证与权限默认值；抑制Claude后台任务；按最终item投影而忽略多类delta。Octopus的Question、凭据、Research只读、流式展示和原生能力均须按自身合同处理。

okou的CLI子进程每次新建，但sandbox可以复用；不能把它概括成每请求新VM。其兼容CI也不是所有边界都硬阻断，存在按消费者区分的检查强度。借鉴的是可执行的兼容规则，不是“开源项目已有测试，所以我们也有保证”。

## 3. 当前 Octopus 的改动面

已识别约 **14–18个现有生产文件的改动或兼容核查触点**；明确核心集中约10–11个，新增约2–4个私有协议协作模块是组织估计。它们不是最终diff、改动上限或工期。强尾部策略尚待能力门禁，测试、生成契约、普通委托保留及原隔离计划其他文件另计。

| 触点 | 责任 |
| --- | --- |
| `server/harness/profile.py`、`harness.py`、`run.py` | 选择私有协议实现，保持实际资源owner；拆开stdin、同请求追加输入和跨请求复用。 |
| `harness/codex.py`、`codex_control.py`及小型RPC/映射模块 | 交互app-server、原配置与controller接回；保留轻量exec。 |
| `harness/events.py`、`tool_identity.py` | 公共事件兼容，必要thread/turn/item及原生执行归属。 |
| `sessions/owner_runtime.py`、`turns.py`、`runtime_types.py` | 主输出候选与最终request完成分开；原结算链接收准确终态。 |
| `sessions/base.py`、`owner_questions.py`、`questions.py`、两侧`client_submissions.py` | 公共输入事实与问题路由；只在实际需要时演进既有字段。 |
| `routers/sessions.py`、`research/leaf.py` | 实际can_steer能力；Research入口与原隔离验收。 |

沿用现有SessionOwner、输入账本、RequestCompletion/SubmissionReceipt、MCP身份/租约、配置快照、每凭据CODEX_HOME、Wiki业务接口、fork/rewind及业务manager。已有接口不代表其全部后续架构票已经完成。[O1][O2][O3]

`run_oneshot`继续exec；代码中的轻量或“tool-free”注释不构成已强制禁用全部原生工具的保证。Research web leaf使用create_run，必须纳入迁移；无需改调用点也不代表无需验证。

## 4. A：最小架构与兼容接缝

### A1. 唯一责任保持不变

| 层 | 拥有的事实/责任 |
| --- | --- |
| SessionOwner | 请求准入、固定业务身份、问题与取消、唯一最终结算。 |
| HarnessRun | 原进程/连接、spawn/reader/writer/stop任务、lease、资源捕获及退出证据。 |
| Codex私有协议实现 | 握手、RPC关联、协议控制、原生事实解析与映射；执行Owner已授权的操作。 |
| UI及业务manager | 读取统一事件和receipt；各自独立work保持原生命周期。 |

优先让HarnessRun提供受管pipe/process给RPC协作对象。若实现确实移交进程责任，必须一并移走旧退出路径，不能形成两个process owner。RPC对象不新增用户队列、持久任务账本或自己的业务完成裁决。

无需新增通用Transport、SessionHost、ControllerService、NativeSupervisor、能力服务或外部workflow平台。不同协议共享事件/结果合同，不要求它们具有相同内部循环。

### A2. 展示兼容与完成事实分开

归一后的正文、工具项、问题、诊断和usage继续进入原历史/UI/消费者。保持当前有效流式行为，不能照搬okou的delta取舍造成退化；明确增量与最终快照如何避免重复。

`item完成`、`provider turn完成`、`request完成`是不同事实。原生turn/completed可更新原生状态，但不能被旧exec式result/EOS直接解释为最终业务成功。兼容旧事件形状，不等于保留过早完成的旧语义。

已支持且影响身份/完成/控制的事件缺少必要字段时，局部失败并保留原因。未知可选通知可以按选定schema忽略或记诊断；影响所声明能力的未知事实不能静默丢弃。不能把未知状态默认成成功，也不因一个无关新字段停止整个服务。

### A3. 能力按操作与阶段提供

沿RuntimeProfile/registry的现有接缝表达所需实现；不建立人工维护的大型能力矩阵。公共`can_steer`由实现能力与当前阶段共同决定，不能再由`stdin_mode == STREAM_JSON`或`reusable`推导。

共同义务只包括：启动/恢复、提交输入或关联答复、观察事件、停止、资源退出。完成覆盖范围必须分别说明，不定义所有provider都无条件支持的全树join。

## 5. P：Codex协议合同

### P1. 固定可验收版本与必要协议面

记录实际部署CLI版本、生成schema、配置及原生multi-agent模式；只实现使用到的请求、响应、通知和server request。okou固定镜像版本的做法可参考，不能复制其版本号代替本地能力验证。

每连接完成initialize/initialized后才开始业务。生成schema用于核对与fixture，不必维护一整套未使用的协议类型。实验字段和内部事件明确标记；必要能力不满足时在发送业务输入前准确拒绝。新字段兼容和核心语义兼容分别验证。[C1]

### P2. 分派与背压

一个reader持续区分response、notification与server request；一个writer串行写完整帧，等待ACK不占住发送通道。展示/日志背压不能阻塞Stop与RPC答复，也不能使用无界通知缓存来隐藏压力。

复用已有队列和交付机制，对实际可达的大小/数量压力采用明确界限与失败路径；不为每个消息类型建独立重试器。必需执行事实无法保留或关键协议事件丢失时，不能断言执行完整成功。面向UI/App的发送与前缀交付沿原通知合同；断连或通知失败不改写业务outcome。EOF、协议终态、控制结果分别记录；保留原诊断脱敏，认证载荷与bearer不进入日志。

### P3. 输入与问题

区分三层事实：Octopus持久接纳输入；provider接受此次发送；请求最终完成。写入pipe、RPC成功和模型处理不互相替代。

初始输入使用turn/start；活动追加使用turn/steer及expectedTurnId。结束竞争中的明确拒绝，只有证明未提交才可回原队列；写出后响应未知保持unknown，不自动重发、resume重跑或切exec。RPC id和clientUserMessageId不是已证明的幂等执行key。

先限制公共追加输入进入已验证的主处理阶段；收集/原生续跑/内部报告阶段新human输入排队。需要扩大可steer阶段时单独证明其归属，不借turn/start在活动turn可能追加输入的行为混入内部报告。

原生requestUserInput使用原连接、原server request及thread/turn答复；Octopus AskUserQuestion保留原渠道。普通问题超时自动回答不能用于审批。未知/未实现审批或elicitation明确拒绝或失败，不挂起、不自动批准；若选定权限策略需要人工审批，先补齐typed决策和界面再支持该策略。[O6][C1]

### P4. 配置、认证、历史与controller

显式映射每凭据CODEX_HOME、登录方式、model/effort/tier、cwd、MCP bearer与工具名、connector、memory、system/controller及请求上下文。独立app-server与exec的认证启用路径存在差异，复制环境不构成等价证据。

controller更新并入同一宿主，沿既有检查点和有效内容规则，确认后再提交输入；删除被替代的临时writer。不能同时用临时app-server与新宿主写同一thread。旧history、compaction、fork/rewind后首轮均须验证。

RPC是交互执行的协议事实源；保留仍有用途的exec或诊断观察，不能让rollout observer再裁决一次完成。usage区分恢复历史与当前执行，防重复计费；父统计与子统计不盲目相加。

## 6. L：请求寿命、权限与退出

### L1. 一个request attempt独占宿主

采用本地stdio；同一逻辑请求可以有多个原生turn，但都属于原attempt、宿主与binding。历史thread可以恢复，宿主不跨已结束的请求复用。这履行项目既有授权隔离决定，而非app-server限制。[O1][O2]

捕获session/request/attempt/work、原backend handle、lease、凭据和配置引用；异步事件与Stop不读取session最新绑定。既有恢复若允许串行新attempt，先收束前一attempt，沿原预算与已证明重试条件执行。

### L2. 原生尾部保留原授权

首个result只是候选输出；仍有归本run的原生工作时，保留原WikiTask、Question上下文和有效MCP授权，继续响应原Stop/Answer。新输入排队，不把旧子任务换绑到新WikiTask。动态撤权仍然有效，内部报告不获得新授权。

决定收束时，在原捕获边界关闭本请求继续工作的资格、新工具准入并撤lease；发起原宿主及受管资源的停止，对匹配活动turn可尽力interrupt，根turn已结束时仍须停止原宿主。停止动作不等待尚未结清的RPC ACK。随后按既有有界退出政策收束并join已捕获工具、输入I/O、宿主及其他受管资源，避免先等待必须靠停止才能退出的I/O。形成内部outcome并确认退役后，再由Owner沿原事务提交唯一terminal/receipt并通知。无法确认退出时保留实际handle并沿既有隔离规则处理，不另开writer。此处细化原退出责任的执行顺序，不增加另一套supervisor。[O1]

MCP lease不控制所有原生shell/文件操作；不能仅看到PID退出或interrupt ACK就声称全部后代已停。实际部署只对已验证受管范围作保证，既成外部效果不因Stop撤销。

### L3. Claude兼容归原隔离票

协议迁移阶段保留Claude stream-json、同请求已有steer和原生能力，不照搬禁用后台功能。现行result后完成receipt、关闭本轮WikiTask，再由idle尽力追加，是兼容基线，尚不代表满足完整原生完成合同；WikiTask关闭也不意味着held进程的run lease已同时撤销。已结算旧receipt和已关闭授权不因迟到idle重新打开。

完整大改按原§5.7/§6.1处理：已知原生尾部仍活动时，新request排队并保留原binding；真正结束或明确停止后退役旧进程，再以新lease/进程resume。不能用新请求到来当成旧尾部自然完成，也不能把已失效授权在idle期间重新授予。

这会取消现行跨request热复用，启动成本必须测量。共享HarnessRun/Owner的EOS与完成路径需要调整，必要时局部归一Claude事件，但不重写其协议或agent loop。Claude的实际尾部边界、问题与退出需单独验证；不因Codex通过就宣称Claude有同等级能力。若当前Claude协议不足以证明要求，记录具体兼容缺口，不靠禁用功能或隐藏截断通过验收。跨request接线由23/52负责，不能藏进Codex mapper。

## 7. N：原生尾部目标与可行性门禁

### N1. 保留原产品目标

父主turn先结束，子任务继续；子结果交给父代理，父代理可以继续处理和再委派；最终在本请求受支持工作完成后交付。主输出立即显示，整体保持可停止。原生子工作与具备独立身份的delegation/Research/bg分别管理。

不预设“一次报告即结束”，不默认关闭原生wake。总请求预算、资源上限、动态撤权和取消沿既有机制；预算耗尽可以准确失败，不能将其伪装成正常完成。常驻原生工作不因一段静默就被视为已结束。

### N2. 22-P0先建立版本运行合同

必须写明并用实际协议/source依据与针对性录制共同支持：

1. 哪些thread/turn/execution属于本request，后代与同一child再次执行如何区分。
2. 如何发现受支持范围，快速结束、孙任务、通知缺口及冷恢复时是否仍成立。
3. 子结果何时成为父处理的实际输入，父后续turn如何关联和观察。
4. 原生自动续跑与宿主显式报告由谁驱动，如何避免双触发。
5. 最终判定时，还有谁能产生未覆盖工作；依据不是已知卡片为空、安静几秒或多次list为空。
6. 根turn已结束时，Stop如何停止原attempt的实际资源。

当前公开协议没有被本次研究证明提供原子的“封闭未来全部后代并确认所有结果已交付”操作。okou也未实现这一合同。实际版本若无法证明用户要求的范围，就明确标为方案未解项；transport仍可独立开发和评审，22完整验收与默认切换保持未通过。

### N3. 由证据选择一条续跑策略

优先验证原生续跑机制，让runtime继续自己的agent loop；Octopus保留宿主、观察事实和执行原请求预算。若确需宿主显式提交父报告，先在22-P0证明其必要性、输入身份和终止条件，再作为同一请求内的操作实现。

显式报告必须转交真正收集到的结果，保留工具数据身份、来源和因果关系；可用的官方toolOutput能力须按实际版本验收，不伪造新human授权。确认父处理阶段后再提交，不能误把start变成活动turn追加；原生wake与宿主驱动不能同时争抢同一报告。

报告RPC接受、输入实际生效、父turn终态和结果覆盖分别确认。hook拒绝或改变输入时重新核对覆盖；ACK或最终正文不能补齐缺失证据。发送未知不再启动第二次。允许符合合同的后续处理，不增加脱离原预算的无限人工汇总循环。

本期不预造持久任务树或通用监督平台。必要执行引用只属于当前attempt，并复用既有历史和结果记录；若实际方案需要更大机制，先明确范围与成本，不藏进adapter。

### N4. 成功、失败和停止

执行成功必须同时具备：声明范围已收束；所需结果进入相应父处理；最后所需父阶段完成；无未确认关键发送/问题；原受管资源已退役。结果与必要输出按原持久化合同记录，由Owner提交唯一terminal/receipt后再通知。

面向UI/App的发送与前缀交付单独反映在既有notification_state中；客户端断连或通知失败不改变业务outcome、不撤销提交，也不触发模型重跑。App是否可以发done仍遵守原输出前缀交付合同，不能先发done再补正文；这不是等待客户端在线才允许业务结算。

子任务失败可以由父代理解释，但不能被根success抹掉。EOF、身份失配、事件覆盖失证、输入未知、取消或预算耗尽保留partial与准确原因。provider故障先局部收束；只有共享权威状态失信或原执行无法受控退出时才沿既有fatal边界关闭准入。

## 8. C：兼容范围与独立业务

| 边界 | 必须保留或明确验证 |
| --- | --- |
| 旧Codex历史 | exec历史resume、历史字段及ID、controller/compaction、fork/rewind；历史可读不代表恢复运行中外部任务。 |
| 原权限 | 普通运行与Research各自策略、MCP身份与工具名、Wiki scope、凭据home和认证；不复制okou沙箱占位凭据。 |
| 展示与消费者 | 原正文/工具/问题/错误/usage；UI、App、委托、构建、调度、Research、Wiki job继续使用原receipt。 |
| Claude | 独立JSONL路径、原生功能与同轮输入；跨请求隔离按L3验证，明确相对热复用的代价。 |
| 旧客户端/数据 | 只检查实际可达的旧浏览器与新服务、新客户端与可恢复旧服务、现存历史/输入记录；不先造版本协商平台。 |
| 其他运行时 | 公共语义保持中立；未来DeepSeek Harness使用自己的SDK与完成证据，不映射成假Codex RPC。 |

DeepSeek模型provider与DeepSeek Harness是两种接入：前者保留所选runtime语义，后者引入独立agent loop及SDK。其区分入队、root idle和受管工作等待的设计值得借鉴；取消与版本合同须单验。本期不新增DeepSeek backend。[D1][D2]

独立bg/Research/named delegation继续原work与25的持久返回；结果作为后续输入，不延长已结束的父request。宿主退出不调用独立work的close/cancel，避免父请求等待只能由自己下一轮处理的返回。

Ticket30完整保留：普通委托本轮成功/失败/取消与一次回传结束，conversation仍可访问、可追问；同时修正常终态自动归档和启动扫档。人工追问是新human request，parent显式follow-up是新round/work；显式Archive与原schedule临时会话策略保持。会话可见不依赖永久进程或永久running。

## 9. G：验收矩阵

| 编号 | 场景 | 必须成立的证据 |
| --- | --- | --- |
| G1 | 新thread与旧exec cold resume，父先结束、子后返回 | 同等归属与结果覆盖；父后续处理完成后才最终receipt。 |
| G2 | 快速child、孙任务、同child再次执行、通知缺口/晚到 | 不过滤本request后代，不以旧completed结算新执行；失证不成功。 |
| G3 | 父处理迟到结果后再委派、再汇总 | 无一次报告硬上限；续跑驱动唯一，受原预算控制；输入hook拒绝不误判。 |
| G4 | start/steer写出后ACK丢失与结束竞争 | 保留输入送达unknown，不重放、不切exec；旧回执不影响新请求。 |
| G5 | 展示背压、server question、Stop同时发生 | 控制/response可处理，答复只回原宿主；无无界缓冲或自动审批。 |
| G6 | 根turn idle、child仍运行时Stop | 原attempt受管资源实际退出；不是仅interrupt ACK或父PID退出。 |
| G7 | 原尾部Wiki调用、新request和撤权交错 | 原授权不早关、不换绑，动态撤权有效；新grant在旧资源退出后安装。 |
| G8 | 配置、认证、controller、fork、Research、oneshot | 原功能/权限有逐项结果，必要事件与usage兼容，轻量exec未误迁。 |
| G9 | Claude普通轮、同轮输入、native尾部与下一轮 | 保留能力；已知尾部不被新请求截断；原grant和退出符合L3，缺口明确。 |
| G10 | UI/App/委托/构建观察同一请求，断连重连 | 主输出可先显示，唯一最终结果不早发；历史/工具/诊断无重复或遗漏。 |
| G11 | 委托完成、重启、人工追问、parent follow-up、Archive | 普通会话保留和合法归档都正确，独立work不被误关。 |
| G12 | 切换、恢复旧候选与保留数据 | 活动attempt不换协议；未知工作不重放；仅真实可达版本组合承担兼容。 |

验证分三类：纯映射fixture；带实际子进程的协议替身及竞争场景；同一候选上的真实CLI/公共入口。替身只证明本地处理逻辑，不能证明官方原生能力；真实一次答对也不能替代归属和完成依据。针对具体剩余风险扩展测试，不全仓反复加压。

## 10. 实施与切换纪律

22-P0核查能力 → 22-P1/P2协议与兼容接线 → 22-P3当前attempt原生尾部 → 23跨请求隔离 → 24/28公共输入与结算 → 31组合 → 50/51最终候选。

22局部验收不依赖23/24整票；后者验收跨请求与公共入口，31证明组合。若实现确实产生反向依赖，必须先修改真实依赖图，不能一边让23等整票22、一边让22等23。

沿Octopus既有计划：这些是审查/验收单元，生产接收完整候选。okou的多部署准备/迁移/清理方法不自动变成本项目的多轮生产发布要求。开发中可独立评审兼容mapper，但不发布缺少所选交付语义的半套完成链。[O2][K2]

切换只作用尚未启动的request；旧attempt自然完成或明确停止并确认退役。协议响应未知时禁止自动exec兜底。保留exec用于原oneshot有独立用途，不属于待删的兼容债。

恢复遵守既有兼容读取/前向修复选择，不回灌旧DB覆盖新文件。若启用旧执行候选，先证明其能读取已写历史和协议；不能仅因旧二进制还能启动就认定可回退。临时兼容桥须记录真实消费者、移除条件和责任票；不重开已完成票或虚构历史状态。

交付报告写明candidate commit/diff、真实CLI版本/功能模式、源事件与完成/资源证据、实际检查、未验证项及旧路径退出。遵守仓库适用门禁；未安装/配置的Claude不探测或安装，明确live未验证。本次文件不触发代码实施、模型调用、提交、推送、重启或部署。

## 11. 来源

外部参考仅列设计/协议入口；实施正文不引入其代码或模块层级。

- [K1：okou核查基线](https://github.com/okou-ai/okou/tree/328c79f29a81e44e454ff155cd2416e78ea7860d)。
- [K2：okou部署兼容设计](https://github.com/okou-ai/okou/blob/328c79f29a81e44e454ff155cd2416e78ea7860d/docs/deployment-compatibility.md)。
- [K3：okou事件兼容迁移议题](https://github.com/okou-ai/okou/issues/18371)；历史议题用于理解迁移方法，当前行为以K1为准。
- [O1：Octopus原大改规范，§5.7/§6.1](https://github.com/Danuvius152/Octopus/blob/85b5b3c99192eb9a85b6bd923cd1be856492ded0/docs/review/octopus_minimal_architecture_spec_20261020.md)。
- [O2：Octopus执行计划与既有决策](https://github.com/Danuvius152/Octopus/blob/85b5b3c99192eb9a85b6bd923cd1be856492ded0/docs/plans/minimal-architecture-execution-20261020.md)。
- [O3：现行HarnessRun](https://github.com/Danuvius152/Octopus/blob/85b5b3c99192eb9a85b6bd923cd1be856492ded0/server/harness/run.py)；[Owner](https://github.com/Danuvius152/Octopus/blob/85b5b3c99192eb9a85b6bd923cd1be856492ded0/server/sessions/owner_runtime.py)。
- [O6：当前问题/审批入口](https://github.com/Danuvius152/Octopus/blob/85b5b3c99192eb9a85b6bd923cd1be856492ded0/server/sessions/questions.py)。
- [C1：Codex app-server官方协议说明](https://learn.chatgpt.com/docs/app-server)。
- [D1：DeepSeek Harness官方项目与SDK说明](https://github.com/deepseek-ai/deepseek-harness)；[D2：DeepSeek官方Codex模型接入](https://api-docs.deepseek.com/quick_start/agent_integrations/codex/)。
