# Octopus 第一阶段实施方案：当前正确性、Wiki 效率与工程约定

实施顺序：**第一阶段（本文）** → [第二阶段](Octopus_Phase_2_Architecture_Contracts.md) → [第三阶段](Octopus_Phase_3_Codex_App_Server.md) → [第四阶段](Octopus_Phase_4_DeepSeek_Integration.md)。每阶段先达到自己的出口，再开始下一阶段产品改造。

> 状态：**待实施规格**，不是已实施、已测试或已部署说明。日期：2026-10-30（Asia/Hong_Kong）。
>
> 依据：用户提供的 `Octopus_Engineering_and_Runtime_Roadmap.md`，重点为 §1、§2、§6；本次只读核查的仓库提交为 `0ca897e97189c5f213a658d986cbd67f4d69c5e3`。附件原始后端核查基线为 `1126d106d95af582ecf989b7c9206ec48c0a09c2`。以下源码事实来自固定提交阅读；本文没有运行产品测试、模型或性能基准。
>
> 本文的函数、字段及测试名称若标注“拟议”，表示实施建议，不表示仓库已经存在。实施前须在实际开发 HEAD 上复核；已经完成的行为直接转为保留回归，不按本文重复开发。

## 1. 本阶段在完整路线中的位置

第一阶段的任务是让**现有路径的行为正确、可解释、可测量**。第二阶段再把这些已验证的规则收回既有 owner；第三阶段用共同接口完成 Codex app-server 与原生尾部；第四阶段复用经过验证的接口接入 DeepSeek。

| 阶段 | 本阶段交付给它的内容 | 不提前承担的工作 |
| --- | --- | --- |
| 第一阶段 G1 | 返回错误分类、默认配置变更规则、Wiki 等价性与成本证据、文档 owner | 不切换运行时，不重写调度器 |
| 第二阶段 G2 | 可复用的行为测试、来源消费者清单、真实提交点和失败边界 | `admit_return`、配置捕获接口、退休查询、共同 runtime 接口由第二阶段收敛 |
| 第三阶段 G3 | 原输入身份与 unknown 规则、配置真实生效规则、未解决原生尾部的反例 | 不把当前 exec 的首次 result 固化成未来完成判据 |
| 第四阶段 G4 | 默认配置与 capability 校验语义、同样的来源返回政策 | 不为 DSH 单独发明输入账本或兼容性规则 |

全套方案采用 C1–C8 的共同语义索引：C1 原绑定；C2 接纳结果；C3 有效配置捕获与实时授权；C4 输入证据层次；C5 请求范围完成证据；C6 原执行退休；C7 问题原通道；C8 owner 排空事实。第一阶段主要落实 C1–C4 的现有行为，并保留 C5–C8 的反例；这些编号是文档索引，不是要求新增八个类或八张表。

### 1.1 不变的产品行为

1. BG、Research、命名委派的自动返回按**成功接纳顺序**进入原会话，每次派发一条。提交时间、来源完成时间、接纳顺序分别记录，不能混用。
2. 普通 Stop 暂停派发且保留积压；暂停后的人工纠偏可先执行，但不会自动恢复积压。显式恢复后继续原顺序。
3. 自动返回不转成 steer，不合批，不增加正常派发时的人工优先级，不把通知代替模型消费。
4. 独立 BG/Research/命名委派允许父请求先结束；provider 原生子任务的完整尾部属于第三阶段的请求完成问题。两者不能互相代替。
5. Agent 默认值允许改变；旧会话需要时由用户修正其有效设置再继续。不批量固化旧 override，不静默换 backend，不改写历史。
6. 来源业务成功、返回已接纳、父请求处理完成、通知送达、资源退休是不同事实。任意一项失败不得反向篡改已经确定的另一项。

这些约束以附件 §1.1–§1.3 为产品裁决；现有 Owner 派发与回归入口见 [S1]。本文所有优化均须保留这些约束。

### 1.2 已有实现不重开

附件已裁定的删除前退休、Wiki 普通/App 正文校验、排序事务、应用/计划列表协调、Research 分类、Connector 身份绑定及 RecordProjection 不重新立项。第一阶段只保留受本次改动影响的回归。

以下情况也不是本阶段目标：全仓 strict、统一 ORM、全局任务总线、通用重试引擎、语义检索、跨请求进程池、SmartReader/Sapphire 新产品、批量修改已保存 persona/memory。性能指标没有证实的问题，不通过新增缓存或持久计数器提前解决。

## 2. 当前事实、风险与实施入口

| 工作项 | 固定提交中读到的事实 | 实施入口与真实消费者 | 本阶段结果 |
| --- | --- | --- | --- |
| BG 返回 | `_publish_terminal` 记录命令结果后，对 `_deliver_cb` 的所有普通异常按 1 秒循环；回调返回布尔值 | `server/bg_tasks.py`、`server/sessions/turns.py:deliver_bg_result`；后台任务详情、原会话 | 永久拒绝停止提交；未知仅观察原 receipt；保留来源结果和诊断 |
| 委派终态/问题 | `_inject_terminal`、`_inject_question` 仍有泛异常重试；终态已有 `_terminal_disposition` | `server/delegations.py`；父会话、子会话、问题 owner、委派卡片 | 终态与问题分别分类；不重复派发、不关闭仍可手动回答的问题 |
| Research | `_inject_report` 已区分完整性拒绝、busy/capacity、unknown；业务报告与投递状态分别持久化 | `server/research/manager.py`、现有回归 | 作为语义参照，保留已修行为，不复制一套 Research 实现 |
| 默认配置 | Agent PATCH 遍历旧会话并因不兼容拒绝保存；执行准备只在 capability 列表非空时检查 effort/tier | `routers/agents.py`、`routers/sessions.py`、`sessions/owner_runtime.py`、`harness/model_settings.py` | 只以候选 Agent 自身是否合法决定保存；每次执行检查实际有效配置 |
| compact search | lexical 页命中后逐文档 `SELECT d.*`；exact 路径也读取完整行后再分页 | `wiki/retrieval.py`、`wiki/search_index.py` | 只搬运页内必需字段与有限命中范围，保留同一检索语义 |
| compact list | `documents(view="compact")` 已有窄列投影 | `wiki/store.py:documents` | 不把它重复实现或宣称为搜索优化成果 |
| 健康诊断 | `index_state.summary` 已计算精确 count；`_index_state` 又物化全部错误，`compact_index` 截为 3 条并以列表长度计数 | `wiki/index_state.py`、`wiki/store.py`、`wiki/read_response.py` | 同快照精确计数、有界样本；完整诊断仍可取得 |
| 文档版本 | Wiki summary 仍写格式 16；Wiki schema 为 17；主数据库是独立的格式 5 | Wiki summary、存储合同、各 schema | 删除重复版本陈述并引用权威，不能把两个数据库格式混为一谈 |

对应源码见 [S2]–[S8]。上述结论是代码阅读结果，不能当作这些分支已经在本环境复现或通过测试的证明。

## 3. 工作包 P1-A：完整修复自动返回的错误分类

### 3.1 先确定谁拥有哪项事实

| 事实 | 唯一权威 | 其他模块可以保存什么 |
| --- | --- | --- |
| BG 命令 exit code、stdout/stderr、完成时间 | BG 原任务记录与其 manager | 显示投影，不倒写为投递失败 |
| 委派本轮业务结果与正文 | 原 `work_id` 对应子会话、已提交 terminal、原 round | 父返回的交付观察，不重新执行子任务 |
| Research 报告 | 原报告文件与 research job | 投递观察，不能把投递失败改成研究失败 |
| 返回是否被接纳、原 request 身份 | 原 source/delivery 对应的 `SubmissionReceipt` | 来源可保存自己观察到的状态，不能另造接纳事实 |
| 原请求是否处理完成 | receipt 对应的 completion 与已提交 outcome | UI 卡片不得自行决定 |
| 问题是否仍可回答、答案如何关闭 | 原 Question owner | 返回诊断，不把返回失败解释成问题已回答 |

原 identity 至少保留 account/session、source、delivery、来源 work 与原 return binding。`delivery_id` 沿现有构造保留：BG 为 `bg:{id}`；委派终态为 `agent-terminal:{work_id}`；委派问题为 `agent-question:{work_id}:{question_id}`；Research 为 `research:{job_id}`。这些已存在的身份不替换成重试次数、当前会话 request 或新的 UUID。[S2][S3]

### 3.2 接纳观察真值表

以下是语义分类，不要求第一阶段建立跨来源的新结果框架。

| 本次可证明的观察 | 再次提交 | 下一步 | 对来源业务结果的影响 |
| --- | --- | --- | --- |
| 原 receipt 已存在 | 禁止 | 使用同一 receipt；正文已删除也不重新读取或重新唤醒 | 无 |
| `SessionAdmissionBusy`、`SessionMailboxFull`、`QuotaExceeded` 明确发生在效果前 | 允许沿原 identity，按既有节流与关闭条件重试 | 重验原资格；问题重验是否仍 pending | 无 |
| `AdmissionIntegrityError` 明确保证本次效果前永久拒绝，且没有此前未知提交 | 禁止 | 结束自动投递；持久保留原因和材料位置 | 无 |
| 提交可能发生、ACK 丢失、Owner/通道失联，无法证明未提交 | 禁止 | 标记 unknown；只核对原 receipt | 无 |
| 原 receipt 被确认 | 禁止 | 从 unknown 归并到原接纳事实，再观察原 completion | 无 |
| 后续查询暂时没有查到 receipt | 禁止据此重放 | 继续 unknown；只有既有权威恢复路径提供可靠反证才可改变判断 | 无 |
| `SubmissionConflict` | 禁止 | 保留原异常与 identity 冲突诊断，交由明确纠正 | 无 |
| 未分类的程序错误 | 禁止当作普通重试 | 保留异常和原 owner；若发送可能发生，另保留 unknown 事实 | 无 |
| 存储权威失信 | 禁止同代猜测重试 | 沿现有 fail-stop 与恢复路径 | 不篡改已确定结果 |
| 投递 coroutine 被取消 | 不能由取消推断未接纳 | 保留原观察、收束原责任，传播取消 | 不重跑来源 |

关键区别是“**后续明确拒绝**”只能说明那一次没有接纳，不能证明此前一次未知提交没有成功。unknown 不能被下一次完整性拒绝、目标归档、问题过期或关闭会话覆盖成“肯定未接纳”。Research 已有针对这一点的回归，BG 与委派应取得等价证据。[S3][T1]

当前委派测试使用 `ConnectionError` 模拟提交后丢失 ACK。修复不能仅匹配 Research 当前的两个异常类而遗漏既有调用边界实际会抛出的错误。应从 `start_message`/Owner 边界列出可到达异常；有明确效果前保证的才归入可重试。不能用 `except Exception: sleep; continue` 代替这项判断。

### 3.3 实施顺序与提交点

1. **保留来源业务提交。** BG 继续先确认原进程退休再记录命令结果；委派从原已提交 request 读取正文；Research 不重写已完成报告。这些顺序不是本次修复对象。
2. **先查询原 receipt。** 此快路径无需正文，也不为已接纳返回重新唤醒归档会话。receipt 的身份核对不能借当前 request 或“同一 session 最近一条”完成。
3. **只在没有旧 receipt 且没有 unknown 时检查原资格并准备返回。** 保留现有 return binding 与归档唤醒规则。准备失败不新建 source/delivery。
4. **调用现有提交入口并分类。** 第一阶段完成每个来源的行为，不提前把所有步骤抽入第二阶段 `admit_return`。
5. **先确定不再提交，再记录交付诊断。** 若持久诊断失败，重试的是原诊断写入或既有恢复，不是重新执行提交、命令或子任务。
6. **接纳后只观察同一 receipt。** 父处理成功、失败或观察中断均记录为对应事实；不把“已接纳”显示成“已处理”。

归档唤醒与输入接纳继续是两个提交点。输入提交失败不证明可以把已经成功的唤醒撤回，也不能声称两步处于一个跨操作原子事务。第二阶段会把顺序收回会话模块，但不会改变这些提交事实。

### 3.4 BG 的具体存储选择

本次源码核查发现，`BgTaskRecord` 与 `bg_tasks` 行没有独立投递错误字段。`retain_result_notice` 的确会将 `delivery_notice` 写入原 transcript，并按 delivery identity 去重；但是原会话不存在、Owner 失效、通知容量不足或 transcript 完整性拒绝时，它不能成为可靠的唯一诊断载体。[S2][S9]

**本方案选择在已有 BG 行增加最小来源交付诊断，而不是新建投递表。** 拟议字段如下，名称实施时可按现有命名约定调整：

| 拟议字段 | 值与含义 | 不能承载的含义 |
| --- | --- | --- |
| `delivery_disposition` | 可空；`stopped` 表示该来源停止自动提交；`unknown` 表示原提交结果未能确认 | 不是业务 status；不是 receipt；NULL 不代表可重放或已接纳 |
| `delivery_error` | 可空；与上述观察对应的清晰原因 | 不放凭据、完整敏感输入或原命令失败原因 |

正常已接纳事实继续从原 source receipt 取得，不另存 `delivery_request_id`、新重试账本或活动计数。unknown 后取得原 receipt 时可以清除来源的未确认诊断；其接纳依据必须是该 receipt，不能是根 session 后来 idle。原日志可保留观察过程。

这两个字段必须在同一工作包覆盖 `server/db/schema.py`、`server/db/base.py` 的 `_BG_TASK_COLS`、读写解码、`BgTaskRecord`、BG REST/MCP 返回、现有任务详情/卡片及必要生成契约。至少让用户能同时看到“命令完成”和“自动返回停止/未确认”的原因；现有业务状态颜色不能被改成命令失败。

`retain_result_notice` 保留为会话内的辅助可见说明，使用原 delivery identity；它写失败不能覆盖 BG 行的原因，也不能再次打开投递循环。对已损坏 transcript 不另造一个旁路写入器。

这是实际新增的持久语义，须同 PR 更新主数据库完整格式、验证器和有限发布转换。**不修改 Wiki 格式 17，不在应用启动时增加自动升级链。** 实施时以实际主库格式选定下一版本；本文不预占版本号。旧行字段置 NULL，旧命令结果/receipt 原样保留，不从历史猜测交付状态。[S8]

实例完整备份继续保留这些字段；账户导出/搬迁目前排除 session、BG 和委派执行历史，继续保持该排除，不因为新增诊断字段扩大历史导入范围。[S8]

### 3.5 委派终态与问题的具体选择

**终态返回。** 保留现有 `_terminal_disposition`，明确 `unknown` 与永久停止的枚举含义；在同一原 round 的持久 JSON 中增加拟议 `terminal_delivery_error`，为来源交付观察提供原因，不能挪用业务 `rec.error`。`_save_round` 仍是这一事实的 writer。读取、公开委派详情与前端类型必须同步；旧持久 round 的有限转换/默认语义与完整存储合同同次交付，不靠任意 JSON 字段容错猜旧状态。

恢复逻辑已经承诺 `never resume work or replay replies`，并将无 disposition 的旧 round 处理为 `not_replayed`。保留这一规则；不能借修复恢复自动重放。正常 follow-up 创建新 round 是独立授权行为，不是旧交付重试。[S3]

**问题返回。** 问题有独立 delivery identity；终态返回失败不能关闭问题，问题返回失败也不能把整个 round 的 terminal 标为已投递。永久拒绝后保留原 child 的 pending question，用户仍可沿现有界面回答。将带原 question identity 的交付说明写到**原 child 会话**的既有 `delivery_notice`，让原问题及原因可见；不能依赖已经拒绝返回的父 transcript。

问题 relay 的运行中观察归原 question/work 的控制对象。第一次取得 `stopped` 或 `unknown` 观察后，在任何后续 await、诊断写入或再次进入 relay 前，原控制对象都必须记住该处置；重复 question event、同代 relay 重入和 `_resume_settlement` 不得重新提交。`unknown` 只观察原 receipt；停止后的说明写入失败只处理原说明的失败，不再次接纳。状态与原问题同时结束，不新增跨问题全局账本。

持久通知不等于恢复一个可回答的新问题。进程重启仍按当前问题生命周期关闭旧回调，不新建持久问题队列。如果原 child Owner 也不可写，保留原异常及未完成责任并沿既有关闭/故障机制处理，不能声称通知已经持久化或为了展示重建问题。

### 3.6 重启、手动重试与资源释放

当前 BG `start()` 只把旧运行中任务标为 interrupted，没有恢复终态自动投递；委派 `restore()` 也不重放结果。修复保留这个边界。[S2][S3]

同一服务代中的 `_resume_settlement`、Stop/shutdown、归档恢复更值得注意：它们可能重新进入原结算路径。`stopped`/`unknown` 的来源交付观察必须在这些入口被尊重，不能因为 `_publish_terminal` 再次被调用就重新提交。对 unknown 只能查原 receipt；明确永久停止可直接结束该交付责任。

未确认退休、失败的持久写或尚未收束的 callback 仍由原 owner 持有。只有来源命令结果、必要交付处置记录及真实资源责任都符合现有条件，才能从 `_running`/原委派控制集合移除。不能把“停止自动提交”实现成丢失失败任务引用。

本阶段不新增“自动修复后重放所有停止返回”的产品按钮。用户需要再次分析已保留材料时，通过新的明确输入发起新请求，不能伪装成旧返回从未发生。

### 3.7 最小但完整的验证矩阵

| 场景 | 驱动方式 | 必须断言 |
| --- | --- | --- |
| 永久完整性拒绝 | 真实 SQLite/Owner 制造明确坏 transcript，来源先已完成 | 提交只尝试一次；源结果保留；原因可从来源详情取得；恢复 transcript 后也不自动重放 |
| 提交后 ACK 丢失 | 在真实 `start_message` 返回前丢 ACK | 一个 source receipt、一个队列项；来源只执行一次 |
| unknown 且原 receipt 暂不可读 | 第一提交结果未知，随后查询 unavailable | 不进行第二次提交；诊断保留 unknown |
| unknown 后“若再提交则永久拒绝” | 让第二次提交成为陷阱 | 陷阱永不触发；后拒绝不能覆盖原 unknown |
| 明确 busy/capacity | 效果前拒绝一次，再恢复 | 同 identity 成功接纳，不另建 work |
| 正文冲突 | 复用 delivery identity 提交不同确定正文 | `SubmissionConflict` 保留，不能称 duplicate 成功 |
| receipt 已有、正文删除 | 先接纳，删除来源正文，再触发观察 | 返回原 receipt；不读正文、不重新唤醒、不新增输入 |
| 问题过期与 ACK 竞态 | 拒绝前/提交后分别关闭原问题 | 未接纳时停止；已经接纳事实保留；不会回答下一问题 |
| 同问题重复事件与 relay 重入 | 原 question 取得 stopped/unknown 后再次进入，并令诊断写入延迟 | 同一 delivery 的提交次数不增加；原 pending question 可回答；unknown 只观察原 receipt |
| 停止/关闭/归档 | 各发生于准备、提交、观察、诊断持久化边界 | 队列政策不变，不取消无关独立工作，不丢原 owner |
| 存储失信 | writer/commit 确认失败 | 进入已有 fail-stop；同代不循环查写猜结果 |
| 诊断自身写失败 | BG 诊断或 round 保存失败 | 业务 success 不倒写；不重新提交；责任与异常仍可追踪 |

复用 `tests/test_research_session_return.py` 的真实反例结构，扩展 `test_bg_session_return.py`、`test_delegation_return_retry.py`、`test_delegation_session_return.py`；重启相关验证放回已有 BG/委派生命周期测试，而不另建模拟全部系统的测试框架。[T1][T2]

## 4. 工作包 P1-B：修正默认配置与实际生效规则

### 4.1 必须分开的四个时点

| 时点 | 回答的问题 | 本阶段要求 |
| --- | --- | --- |
| 保存候选 Agent | 这份默认配置自身是否合法、credential 是否属于账户并匹配 backend | 校验候选值；旧会话暂时不兼容不阻止保存 |
| 会话准备执行 | 该会话的 backend、逐字段 override、当前 Agent 默认组合能否运行 | 每次检查真实有效配置；给出可操作错误 |
| 本请求捕获 | 这次 attempt 使用哪一组配置 | 同一次捕获，已执行请求不随默认值变化 |
| 真实操作前授权 | 现在是否仍允许使用 credential、Wiki 范围、工具与资源 | 保留即时撤权，不把旧快照视作永久授权 |

第二阶段 S3 会集中这四种操作的接口。第一阶段先修正现有入口的行为，并确保测试覆盖它们；不在第一阶段新增全局 ConfigurationManager。

### 4.2 后端绑定与继承不要混淆

当前 `effective_settings()` 逐字段处理 model、effort、tier、credential；空 override 继承默认。会话 backend 有自己的实际绑定，执行准备读取 `configured.backend`。改变 Agent 默认 backend **不意味着旧会话历史自动切换 backend**。[S4]

因此跨后端改默认后，旧会话可能仍是 Codex 历史，却继承了 Claude credential/模型/控制项；应显示实际 backend 与字段来源，要求在现有可编辑范围内调整。若某字段当前只有 Agent 配置而没有 session override，应明确指出修正位置，不能在本阶段顺带给所有字段增加 override，也不能悄悄清除用户保存的默认值。

### 4.3 具体改动

1. 在 `routers/agents.py:_update_agent_settings` 删除“遍历所有旧会话并因此拒绝候选默认保存”的门禁。候选自身的字段合法性、账户归属、credential/backend 匹配和工具策略可执行性继续验证。
2. 把“候选默认允许保存，但旧会话实际使用可能失败”写入已有设置帮助和会话实际配置展示。优先在选中会话显示实际值、继承来源和修正原因，避免保存时扫描全部历史会话生成持久兼容性缓存。
3. 修正 `owner_runtime.py:_prepare_run` 的条件校验：非空 effort/tier 必须按实际 backend 的能力检查，不能因 capability 列表为空而跳过。空列表代表“不提供已声明的该控制项”时应明确拒绝，而不是执行后仍显示已生效。
4. 共享**现有领域校验语义**，避免在 runtime 直接依赖 FastAPI `HTTPException`。如需要抽取纯校验函数，放在现有 `harness/model_settings.py`，由路由映射为 HTTP 错误；这里只抽取校验，不提前承担 S3 的配置更新/捕获编排。
5. 工具 allow/deny 沿 `ToolPolicySupport.validate()` 与真实 assembly 入口验证。Agent 保存检查候选 backend；旧会话执行检查实际 backend。不能把 approval/auto-approve 当作工具限制能力。[S5]
6. 校验通过仍需验证 renderer/调用参数实际携带该配置。模型列表是建议和已知约束依据，不因自定义模型名称不在 picker 内就一概拒绝。
7. 当前请求已捕获的模型/凭据/工具配置保持原值；下一请求使用新有效值。撤权作用于实际操作，不能等待下一请求才执行。慢 memory、prompt、connector 准备继续在配置锁外。

保存后的 UI 只能说“默认设置已保存”；会话未实际启动前不得宣称该会话“已应用新 backend”。错误中给出实际 backend、字段来源、失败约束与修改入口，避免暴露密钥或路径中的秘密。

### 4.4 行为矩阵

| 操作 | 预期结果 |
| --- | --- |
| 改成候选自身合法的新默认，某旧会话 override 不兼容 | 保存成功；旧会话下一次执行在 spawn 前明确拒绝；历史不变 |
| 改成候选自身非法 effort/tier 或错误 backend credential | 保存失败且原默认不变 |
| 同 backend 改模型，旧会话有 model override | override 继续优先；其余字段独立继承，必要时明确报不兼容 |
| 旧会话清空 override | 恢复继承，不是“强制关闭控制项” |
| 旧会话把 model/effort/credential 一起改成合法组合 | 保存与下一次执行成功；原 thread/history 保留 |
| Agent 改 backend | 新会话按新默认创建；旧会话不自动改 backend 或转换历史 |
| 保存时合法，准备前 credential 被撤销 | 准备/授权拒绝；不调用模型 |
| 运行中默认变化 | 当前已捕获配置不变；后续请求捕获新配置 |
| capability 缺失但存有非空控制项 | 明确不支持/不可用；不静默忽略 |
| 自定义模型名，其他约束合法 | 保留现有可输入能力；provider 返回的实际错误如实展示 |

覆盖现有 `tests/test_model_settings.py`、`test_model_entry_authority.py` 与模型设置前端测试。原“默认变更应保护所有旧会话因此拒绝”的测试须按新产品裁决替换，其他 credential、历史与权限断言保留；不能通过删除整个测试文件消除冲突。[T3]

## 5. 工作包 P1-C：先降低 Wiki 调用与重复输入成本

也可以看看.scratch/wiki-test-audit-20261030/report.md中处理优先级最后这一段。

本工作包调整现有工具使用与说明，不把检索引擎从头重做。已知路径/稳定文档身份直接 read；不知道位置时用 compact 搜索发现；对多个已确定范围用 `read_many`。当前调用方上下文已经持有同 revision 的正文时，避免无理由重复传输。[S6]

复用正文不等于复用权限或阅读证明。换任务、换账户、授权撤销、文件更新、删除、重绑后按现有 live read/授权合同重新核验。搜索 snippet 是导航证据，不自动获得维护操作所需的全文或范围阅读证明。

实施时先核对真正进入模型的 instruction/skill 源文件。修改 `docs/agent-prompts.md` 这样的可复制快照不等于更新已保存 Agent；本阶段不批量改 persona/memory。若修改实际 runtime prompt，使用已有代表性问题验证调用序列及答案证据，不能将“只改几句提示词”当作无需行为验证。[S10]

固定问题建议至少覆盖：已知 `Library/Me` 路径、已知研究报告、宽泛“数字人研究”、比较多个方法、中文未知人名、同名不同目录、alias、正文更新后重读。每个问题记录预期文档与段落、要求 revision、允许的工具序列和必须保留的引用；不把模型回答措辞完全相同作为通过条件。

## 6. 工作包 P1-D：compact 搜索窄投影与页内批量读取

### 6.1 优化的具体对象

现有章节索引已经完成排序、去重、overview/compare 与分页。第一轮优化保持 `search_index.select()` 的候选与排序语义，主要修改**页命中之后的数据库→Python 数据搬运**，并修正 exact 路径不必要的全文装载。[S6]

两类成本分别测量：

| 成本 | 当前表现 | 优化目标 |
| --- | --- | --- |
| 查询次数 | 每个 hit 再执行一次完整行查询 | 页内批取；保留命中顺序 |
| Python 接收字节 | 每个 hit 取整篇 `text` | 只取 metadata 与所选章节所需范围 |
| SQLite 内部文本处理 | FTS、排序或 substr 仍可能访问文本页 | 单独用查询计划与耗时验证，不能拿返回字节下降冒充 IO 全消失 |
| 模型响应字节 | compact 已截短输出 | 保持输出合同；不把现有截短重复计为新收益 |

### 6.2 推荐算法

1. 在同一个 `BEGIN` 读快照内取得 epoch/cursor、完整过滤与排序后的页 hits；不限制搜索候选以换取速度。
2. 对 lexical 页构造临时 SQL 参数关系，携 `doc_id/start/end/ordinal`，通过一次参数化查询取得 `id/path/title/revision/native metadata/managed metadata` 与选中章节的文本切片。临时参数不是新表或持久缓存。
3. 保留现有 metadata 合并规则：managed 覆盖普通字段，tags/aliases 去重合并。第一轮可以继续读取 metadata JSON；若其本身成为测量瓶颈，再投影必要 JSON 字段，不能不经证明改变覆盖顺序。
4. 正文只取**当前最佳命中章节的既有范围**，以该范围计算原 `matched_fields.body` 和 snippet；不要只取最终 240 字符 snippet 再判断是否命中 body。当前搜索章节分窗有上界，读取选定章节仍显著小于整篇长文；该上界来自当前索引算法，版本变化须重新核对。[S7]
5. 在返回行中保留 slice 的绝对 origin。拟议的 `preview_from_slice(text, origin, hit, terms)` 只替代现有 preview 对整篇文本的依赖，必须产生原算法相同的 snippet、`snippet_start`，不能把 NFKC/casefold 后的坐标直接当作原文坐标。
6. 输出最多两个 section 的展示规则、`matched_section_count` 的完整命中数、doc role 与每个 section role 保持现有语义。第一轮可以从已取得 metadata 的 body sections 计算 role；不能因少读 metadata 而一律填 `body`。
7. exact 先做原身份解析，保留“身份已存在但被 scope 过滤时，不再退化为同名标题搜索”的规则；count 与页内选择使用同快照。只投影 compact 所需列，不先读全部 exact_rows 的 `text` 再 Python 切页。
8. full 路径保持原输出与 navigation 语义。后续 full 导航依赖批读只有在数据证明 N+1 显著时才进入 P1 的附加工作包，不能借此更改文档去重或 stale 规则。

这里的临时参数关系可用 `WITH page(...) AS (VALUES ...)` 或项目现有批查模式；具体 SQL 由实际 SQLite 参数上限与页数限制决定。使用绑定参数，保留 page ordinal，不依赖 `IN (...)` 自然返回顺序。空页直接返回，不能生成不合法空 VALUES。实现应选择一个简洁方案，不增加通用查询构建器。

### 6.3 必须原样保留的输出

对冻结的索引快照，新旧结果逐字段比较：文档排序、`total`、`next_offset`、`next_cursor`、`match_mode`、query terms、intent/view、revision、doc role、sections、section role、matched fields、snippet 起点及正文。性能观测字段若另存，不能混入业务输出使调用方承担新兼容规则。

matched fields 尤其要区分：title/path/alias/description 命中，以及最佳章节正文命中。正文命中在 snippet 窗口外仍应被报告；额外 contributing sections 的数量不能因只展示前两个而变小。[S6][S7]

### 6.4 Unicode 与分页反例

| 反例 | 验收要求 |
| --- | --- |
| 中文相邻词、未知专名、bigram | 连续词匹配与当前 FTS 规则等价，不能将不相邻单字拼成命中 |
| emoji、扩展字符、组合音标、全角字符 | Python code point、UTF-8 字节数、前端 UTF-16 位置分开；不把三者相加或直接混用 |
| CRLF、LF、前置 BOM、Markdown 标题/代码块 | 索引文本与 live read 的已有坐标解释不变 |
| 命中在长章节尾、snippet 前后界 | snippet 与 start 的范围有效；body 命中证据不丢 |
| 同一路径 `%`、`_`、大小写差异 | 保留 literal 目录语义和身份大小写约定，不换成未经转义的 LIKE |
| 同名/alias 多文档、被过滤的稳定 ID | 不越过身份优先规则返回另一篇 |
| overview/compare、原文与 summary | 去重及多样化在分页前生效；不让 summary 借原文坐标 |
| 翻页期间 index epoch 变化 | 沿当前 409 重新起页，不拼接不同快照结果 |
| 搜索后正文外部变更或撤权 | read 继续校验 live revision 与授权；窄投影不能授予阅读证明 |

## 7. 工作包 P1-E：健康诊断同快照精确计数与有界样本

当前 `index_state.summary()` 已在传入 connection 上得到精确文档错误数和少量系统诊断。应复用这一权威，避免先把所有错误行转换成 Python 列表再截为 3 条。[S7]

### 7.1 建议接口变化

在现有 Store 内增加有明确调用语义的有界诊断参数，例如拟议 `index_state(..., error_limit=3)`；没有限制的完整诊断入口保留。参数只改变物化的样本量，不能改变准确 count、index status、freshness 或维护准入谓词。

compact 路径应返回：准确 `index_error_count`、至多 3 条 `index_errors`、必要的 `index_errors_truncated`。完整路径仍返回完整错误列表。`compact_index()` 优先使用显式准确 count；不能再对样本执行 `len(errors)` 当总数。

### 7.2 同快照算法

1. search/list 已持有 connection 时，诊断复用同一个读事务；独立健康入口自己开启只读事务。
2. `summary(connection, observed_pending)` 计算文档 count、durable recovery/observer/catalog 事实。
3. 用相同 `DOCUMENT_ERROR` 谓词读取有界文档错误样本；复用已知系统诊断，按确定规则补足样本上限。若为稳定样本新增 `ORDER BY path,id`，文档说明样本不是错误严重度排序。
4. count 中包含文档错误与系统诊断，样本中每项按同一语义出现一次。不能遗漏系统诊断，也不能把同一错误同时作为文档和系统计数重复加入。
5. `truncated` 由总数超过样本数或单条说明被裁剪共同决定。错误为零、只有系统诊断、恰好 3 条、大量错误均覆盖。

不得新增持久错误计数、后台维护样本缓存或对全部错误做“缓存失效扫描”。这一优化减少的是返回样本物化量，不承诺 COUNT 本身永远 O(1)。如果 COUNT 成为瓶颈，先用实际计划定位，再另作设计。

### 7.3 验证

在同事务内交错另一个 writer 修改错误文档，证明 count 与样本仍来自同一快照；下一次请求看到新事实。检查 compact 的投影内存与返回条数不随错误总量线性增加；完整诊断仍能得到所有错误。维护准入、恢复阻塞和 status 判断不得读取截短样本推断全局健康。

## 8. 性能与效果证据：明确测什么，不预填提速数字

### 8.1 语料和执行矩阵

| 组别 | 内容 | 目的 |
| --- | --- | --- |
| 小型语义集 | 中文/英文、身份/alias、summary/原文、过滤、Unicode、坏 frontmatter | 精确输出与引用等价 |
| 长文集 | 若干长文，匹配分布在前/中/后及不同章节；固定 revision | 度量 compact 全文搬运与命中窗口 |
| 规模集 | 根据实际个人库规模逐级增长的文档/章节数量、错误比例 | 测 SQL 数、内存、宽查询及分页 |
| 变更集 | 单文件改动、批量变更、移动/删除/同路径替换 | 验证增量同步和 live-read 成本与正确性 |

复用 `scripts/evaluate-wiki-search.py` 与现有可选测试结构；固定随机种子、语料摘要、查询集、代码 SHA、Python/SQLite/依赖版本及机器信息。不要把用户私库复制进仓库 fixture。规模与性能基准保持 `wiki_perf` 独立 opt-in；模型回答质量评估保持 `knowledge_eval` 独立 opt-in。[T4]

### 8.2 指标与判定

记录冷索引创建、热 search、live read、批量同步、维护队列等待各自的 p50/p95；同时记录 SQL 次数、数据库返回 Python 字节数、实际文件读取次数/字节、峰值内存、工具轮数、模型输入重复字节、Recall@k 与目标章节命中。

正确性为硬门：冻结问题集不能漏掉原本应命中的文档/章节、不能变 revision/权限语义。性能收益以配对重复测量判断，记录样本与波动；实施前根据基线制定可接受回归容差，不把一个随意百分比写成产品 SLO。

建议最低性能出口：long-document compact search 的 Python 全文字节搬运显著下降；页 hits 的完整行 N+1 不再存在；bounded diagnostics 不物化所有错误。若 SQL 次数下降但 p95/内存恶化，必须解释原因并修正，不能只选择对自己有利的一个指标。

附件的“151,266 字节候选字符串 / 2,057 字节最终 JSON”属于历史探针；compact list 的“1,148 / 1,077”是另一操作。它们可指导选反例，不能作本次优化前后对照或生产提速倍数。

## 9. 工作包 P1-F：文档、类型与可重建环境

### 9.1 文档 owner 与具体动作

| owner | 本阶段修改 | 不放入这里的内容 |
| --- | --- | --- |
| 根 `AGENTS.md` | 高频工程原则、真实失败模型、owner/提交点、验证止点 | 每个协议的详细事件矩阵 |
| `docs/implemented/architecture-contract.md` | 已实施的返回分类与输入事实；明确原生尾部仍未完成 | 把第三阶段提案标为 current |
| `docs/implemented/harness-layer.md` 等主题当前合同 | 修正“只差序列化”“静默忽略工具策略”等误导陈述；区分当前与候选协议 | 复制所有 argv/schema 字段 |
| `wiki-development-summary.md` | 链接当前存储合同；记录实际 compact/query/read 事实 | 重复写死 Wiki 格式号 |
| `deepseek-harness-backend.md` | 标明旧 rc.2/headless 条件规格被新路线取代、接入尚未实现 | 把附件 rc.5 静态阅读当成本次实机通过 |
| `native-subagents.md` | 区分已提供卡片/观察与未完成的原生完整尾部 | “shipped” 同时覆盖两种范围 |
| `testing.md`、`local-wsl.md` | 唯一验证政策与环境/发布操作 | 在四份实施文档各维护一份测试数量表 |
| `review/` 与阶段交接记录 | 固定版本证据、取舍、未解决依赖 | 长期作为第二份当前合同 |
| persona、memory | 保留角色与短入口/偏好 | 复制架构正文、把历史偏好当运行时权限 |

每个工作包同 PR 更新它的 owner；P1-F 只处理跨主题漂移和入链收尾，不意味着此前 PR 可留下失实文档。四份阶段实施方案用于实施与交接，完成后其“当前事实”仍由主题文档维护，避免形成五套互相覆盖的架构说明。[S10]

### 9.2 类型与依赖

边界优先覆盖本阶段触及的投递结果、持久诊断、有效配置及 API/WS 投影。避免新增裸字符串隐藏状态；状态、原因和业务结果分别建模。旧 `Any`/mypy 豁免只在实际改动边界收窄，不趁机全仓重写类型。

继续使用当前 Python venv/pip 与前端锁文件。为验收记录可重建的 Python 解析依赖、Python/SQLite 版本、安装命令与 CLI/schema 基线；若项目缺少可复现安装产物，在既有打包流程补齐，不以此迁移包管理器。CLI/schema 仅用于后续协议证据，不在本阶段启动模型或替换生产工具。

## 10. PR 顺序、删除项与变更边界

阶段内部按以下默认顺序交付；独立的只读基线测量可以并行，但每个 PR 都要包含该行为的消费者、失败路径、文档和必要旧路径删除。

| PR | 内容 | 合并前必须成立 | 必须删除或替换 |
| --- | --- | --- | --- |
| P1-01 | 返回分类与来源诊断；BG/委派全路径、格式/消费者闭合 | 永久拒绝、unknown、重启/结算重入、问题路径反例通过 | 泛异常无限提交循环；把投递失败写成业务失败的代码 |
| P1-02 | 默认配置与执行前校验 | 保存/继承/实际能力/历史/撤权矩阵通过 | 全部旧会话兼容才准保存的门禁；空能力绕过非空控制项 |
| P1-03 | Wiki 问题集、调用说明与成本基线 | 源 revision/预期段落明确；opt-in 规则保持 | 将搜索片段当全文阅读证明的指引；无必要重复读取 |
| P1-04 | compact 窄投影与页内批取 | 新旧快照输出等价、Unicode/过滤/分页反例通过、成本证据改善 | compact 全文 `SELECT d.*` 路径与每 hit 完整行查询 |
| P1-05 | bounded diagnostics | 同快照准确 count、样本有界、完整入口保留 | compact 先物化全部错误；以样本 len 覆盖真实 count |
| P1-06 | 文档漂移/依赖记录与 G1 收束 | 链接/锚点/索引一致，所有工作包证据可追溯 | 重复版本号、失效当前说明、已替代的本阶段旁路 |

若 P1-01 的 storage/前端变更过大，可以按同一完整工作单元组织多个开发提交，但不能先发布只会捕获异常却不保留诊断的半套行为。第一阶段不交付 `admit_return` facade、统一 runtime transport 或配置生命周期重构；这些归第二阶段，避免边修行为边改变其 owner。

## 11. 验证与发布边界

开发时先运行受影响的 hermetic 回归；代码提交门禁依仓库 `scripts/check.sh`，发布门禁依 `docs/testing.md` 与实际安装的 `local-wsl.md`。本方案不另定义常驻测试数量，也不把本次文档阅读称为绿色测试结果。[S10][T4]

源码已有测试命令可作为实施入口，具体文件集按实际 diff 选择：

```bash
.venv/bin/pytest tests/test_bg_session_return.py tests/test_delegation_return_retry.py tests/test_research_session_return.py -m "not real and not knowledge_eval and not wiki_perf" -q
.venv/bin/pytest tests/test_model_settings.py tests/test_model_entry_authority.py -m "not real and not knowledge_eval and not wiki_perf" -q
.venv/bin/pytest tests/test_wiki_search.py tests/test_wiki_read_response.py tests/test_wiki_live_read_cost.py -m "not real and not knowledge_eval and not wiki_perf" -q
```

这些命令不是穷尽门禁，也未在本次文档工作中运行。新增来源诊断字段后，格式验证、BG detail/卡片、委派 JSON 与生成契约测试同样必需。对本机真实模型验证沿已有 Codex 登录和隔离资料；不为此安装/登录 Claude；Wiki 性能与知识评估继续显式 opt-in。

一个检查只有一个执行者。源码、配置、依赖、环境未改变时复用有效证据；变更影响某个假设时重跑相应检查，不能靠重复全套测试替代解释失败。

## 12. 回退与失败处置

| 变更 | 可回退部分 | 不能声称能回退的事实 |
| --- | --- | --- |
| 返回分类 | 在未放入不兼容持久数据前回退代码，或准备兼容当前格式的修正版 | 已接纳输入、已执行命令/子任务不能通过代码回退撤销 |
| BG/委派交付诊断持久语义 | 按当前存储合同准备经过验证的发布候选；数据保留 | 旧二进制不能直接打开新完整格式；不自动降低 marker 或删除诊断 |
| 配置规则 | 修正 UI/校验逻辑，保留用户已保存的值 | 不批量恢复旧 Agent 默认或改写用户 overrides |
| Wiki 窄投影 | 回退查询实现；派生索引无变化时沿用现有索引 | 不回退用户正文、revision、权限或来源关系 |
| bounded diagnostics | 回退到完整物化实现 | 不能以回退丢弃健康错误或解除恢复阻塞 |

发布前自然排空；失败留在原候选与原数据恢复责任中。不得在未知接纳或未退出资源时切新 writer；也不以“这是阶段一的小改动”为由跳过实际新增格式的有限转换与保全验证。

## 13. G1：进入第二阶段的硬门与交接物

### 13.1 必须全部满足

- [ ] BG、委派终态、委派问题与 Research 的已完成来源不会因永久拒绝无效重试；unknown 不授权重放；来源结果与材料保留。
- [ ] source/delivery/work/receipt 身份不变；已有 receipt 不要求正文仍存在；冲突保留原异常。
- [ ] BG 与委派交付诊断有明确 writer、可见入口和恢复规则；诊断写失败不会再次提交；新增持久语义完成相应格式/消费者交付。
- [ ] 默认配置可以合法改变；旧会话使用前检查真实有效值；执行参数确实兑现支持的控制项，历史与 backend 不被静默转换。
- [ ] compact 搜索不再为每个命中无条件搬运整篇全文，页内读取消除相应 N+1；排序、分页、匹配证据、Unicode 坐标和 live-read 权限合同等价。
- [ ] 健康 count 与样本同快照；compact 物化有界、完整诊断仍可用；维护准入不使用截断样本判断健康。
- [ ] 实际成本证据存在，未把历史数字写成当前收益；opt-in 与暂停压力测试范围不变。
- [ ] 文档 owner、链接、当前/提案状态与依赖记录一致；代码、验证、部署三个状态分别记录。
- [ ] Codex 原生尾部仍标为未解决；本阶段反例未被“临时 wait/提示词/延长超时”伪修复。

### 13.2 交给第二阶段的材料

交接包放在现有主题合同与固定版本验收记录，不建新的长期真相表：

| 交接物 | 内容 | 第二阶段用途 |
| --- | --- | --- |
| 行为真值表 | 本文 §3.2 的实际实现版本、异常到达边界、unknown 单调性 | S1 抽取共同接纳接口的不可变语义 |
| 来源差异表 | BG/Research/委派业务材料、交付诊断、恢复与关闭各归谁 | 防止 S1 变成通用来源重试/结算引擎 |
| 配置矩阵 | 默认保存、逐字段继承、实际 backend、捕获与实时授权 | S3 集中规则时不恢复旧默认门禁 |
| Wiki 契约与测量 | 查询等价 fixture、live-read 反例、成本数据与方法 | S5/S6 重构资源/入口时保留可测行为 |
| 现有 consumer 清单 | 原 receipt 消费、问题、Wiki/App、UI 与失败展示 | S2/S2b/S4 迁移不能漏真实入口 |
| 未完成范围 | 原生完整尾部、退休私有字段、owner 排空、配置锁编排等 | 明确 G2/G3 的职责，不能把 G1 当整体完成 |

G1 完成后开始第二阶段。协议静态调查和最小反证可以提前准备，但不得提前切换 Codex/DSH 产品路径；否则会同时改变错误分类、owner 和协议，无法判断一个失败究竟违反了哪层合同。

## 14. 可直接给开发代理的实施任务

> 在当前 HEAD 复核本阶段事实与根/主题 AGENTS，按 P1-01 至 P1-06 顺序实施。先给出本工作包涉及的事实、writer、提交点、真实消费者及失败范围，再改代码。对已存在且符合目标的实现复用证据，不重复开发。自动返回继续按成功接纳顺序逐条派发，Stop 保留积压；unknown 不授权重放。候选默认配置允许改变，执行前检查实际有效值。每个工作包同时完成真实消费者、失败路径、旧路径删除和主题文档；不新建通用重试引擎、配置中心或任务账本。性能只按当前可复现实验报告。未达到 G1 的项明确保留未完成，不让第二阶段代为收尾。完成后提交 G1 证据与交接矩阵；本任务不授权第三/第四阶段切换。

## 来源与核查入口

附件来源：[R] 用户提供的 `Octopus_Engineering_and_Runtime_Roadmap.md`，§1–§3、§6–§7。下列固定提交链接用于说明本次读到的代码，实施时须与新的 HEAD 对照。

- [S1] [Owner 调度](https://github.com/Danuvius152/Octopus/blob/0ca897e97189c5f213a658d986cbd67f4d69c5e3/server/sessions/owner_runtime.py)、[派发回归](https://github.com/Danuvius152/Octopus/blob/0ca897e97189c5f213a658d986cbd67f4d69c5e3/tests/test_session_dispatch.py)。
- [S2] [BG owner 与交付循环](https://github.com/Danuvius152/Octopus/blob/0ca897e97189c5f213a658d986cbd67f4d69c5e3/server/bg_tasks.py)、[BG 数据访问](https://github.com/Danuvius152/Octopus/blob/0ca897e97189c5f213a658d986cbd67f4d69c5e3/server/db/bg_tasks.py)、[会话提交与 BG 回调](https://github.com/Danuvius152/Octopus/blob/0ca897e97189c5f213a658d986cbd67f4d69c5e3/server/sessions/turns.py)。
- [S3] [委派本轮、终态/问题与恢复](https://github.com/Danuvius152/Octopus/blob/0ca897e97189c5f213a658d986cbd67f4d69c5e3/server/delegations.py)、[Research 分类](https://github.com/Danuvius152/Octopus/blob/0ca897e97189c5f213a658d986cbd67f4d69c5e3/server/research/manager.py)。
- [S4] [Agent 保存](https://github.com/Danuvius152/Octopus/blob/0ca897e97189c5f213a658d986cbd67f4d69c5e3/server/routers/agents.py)、[会话配置](https://github.com/Danuvius152/Octopus/blob/0ca897e97189c5f213a658d986cbd67f4d69c5e3/server/routers/sessions.py)、[逐字段继承](https://github.com/Danuvius152/Octopus/blob/0ca897e97189c5f213a658d986cbd67f4d69c5e3/server/harness/model_settings.py)、[执行准备](https://github.com/Danuvius152/Octopus/blob/0ca897e97189c5f213a658d986cbd67f4d69c5e3/server/sessions/owner_runtime.py)。
- [S5] [模型入口校验](https://github.com/Danuvius152/Octopus/blob/0ca897e97189c5f213a658d986cbd67f4d69c5e3/server/routers/backends.py)、[ToolPolicySupport](https://github.com/Danuvius152/Octopus/blob/0ca897e97189c5f213a658d986cbd67f4d69c5e3/server/harness/profile.py)、[工具装配](https://github.com/Danuvius152/Octopus/blob/0ca897e97189c5f213a658d986cbd67f4d69c5e3/server/harness/assembly.py)。
- [S6] [检索与 compact](https://github.com/Danuvius152/Octopus/blob/0ca897e97189c5f213a658d986cbd67f4d69c5e3/server/wiki/retrieval.py)、[Store、metadata、list/read](https://github.com/Danuvius152/Octopus/blob/0ca897e97189c5f213a658d986cbd67f4d69c5e3/server/wiki/store.py)。
- [S7] [章节索引、分窗与 preview](https://github.com/Danuvius152/Octopus/blob/0ca897e97189c5f213a658d986cbd67f4d69c5e3/server/wiki/search_index.py)、[准确健康谓词](https://github.com/Danuvius152/Octopus/blob/0ca897e97189c5f213a658d986cbd67f4d69c5e3/server/wiki/index_state.py)、[compact 响应裁剪](https://github.com/Danuvius152/Octopus/blob/0ca897e97189c5f213a658d986cbd67f4d69c5e3/server/wiki/read_response.py)。
- [S8] [存储与发布交接合同](https://github.com/Danuvius152/Octopus/blob/0ca897e97189c5f213a658d986cbd67f4d69c5e3/docs/plans/current-storage-format.md)、[主数据库 schema](https://github.com/Danuvius152/Octopus/blob/0ca897e97189c5f213a658d986cbd67f4d69c5e3/server/db/schema.py)、[Wiki 格式](https://github.com/Danuvius152/Octopus/blob/0ca897e97189c5f213a658d986cbd67f4d69c5e3/server/wiki/schema.py)、[账户资料搬迁](https://github.com/Danuvius152/Octopus/blob/0ca897e97189c5f213a658d986cbd67f4d69c5e3/docs/backup-and-migrate.md)。
- [S9] [delivery_notice 持久化与原 Owner](https://github.com/Danuvius152/Octopus/blob/0ca897e97189c5f213a658d986cbd67f4d69c5e3/server/sessions/owner.py)。
- [S10] [根工程规则](https://github.com/Danuvius152/Octopus/blob/0ca897e97189c5f213a658d986cbd67f4d69c5e3/AGENTS.md)、[文档 owner 规则](https://github.com/Danuvius152/Octopus/blob/0ca897e97189c5f213a658d986cbd67f4d69c5e3/docs/AGENTS.md)。
- [T1] [Research 返回行为回归](https://github.com/Danuvius152/Octopus/blob/0ca897e97189c5f213a658d986cbd67f4d69c5e3/tests/test_research_session_return.py)。
- [T2] [BG 返回回归](https://github.com/Danuvius152/Octopus/blob/0ca897e97189c5f213a658d986cbd67f4d69c5e3/tests/test_bg_session_return.py)、[委派返回与 ACK 回归](https://github.com/Danuvius152/Octopus/blob/0ca897e97189c5f213a658d986cbd67f4d69c5e3/tests/test_delegation_return_retry.py)。
- [T3] [模型设置回归](https://github.com/Danuvius152/Octopus/blob/0ca897e97189c5f213a658d986cbd67f4d69c5e3/tests/test_model_settings.py)、[模型入口权限回归](https://github.com/Danuvius152/Octopus/blob/0ca897e97189c5f213a658d986cbd67f4d69c5e3/tests/test_model_entry_authority.py)。
- [T4] [验证政策](https://github.com/Danuvius152/Octopus/blob/0ca897e97189c5f213a658d986cbd67f4d69c5e3/docs/testing.md)、[搜索评估](https://github.com/Danuvius152/Octopus/blob/0ca897e97189c5f213a658d986cbd67f4d69c5e3/tests/test_wiki_search_evaluation.py)、[live read 成本回归](https://github.com/Danuvius152/Octopus/blob/0ca897e97189c5f213a658d986cbd67f4d69c5e3/tests/test_wiki_live_read_cost.py)。
