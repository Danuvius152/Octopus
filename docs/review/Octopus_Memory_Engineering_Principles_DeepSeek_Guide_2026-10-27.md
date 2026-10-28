# Octopus：Memory、Engineering principles 与 DeepSeek Harness 文档实践建议

**原审阅日期：2026-10-27（Asia/Shanghai）；六类问题复核：2026-10-28。**

**范围：**原审阅依据 `agents(1).zip`、`Engineer.txt`、当时的 Octopus 公开仓库与 deepseek-harness 资料。此次按本机工作区和实际 memory 文件更新第 1.3 节及相关结论、建议状态；原附件统计与外部参考仍保留原基线，未重新开展全篇审阅。复核基线和限制见附录 B.3。本文更新不代表已采用建议或修改保存的 persona、memory。

## 结论与阅读路线

**2026-10-28 复核：M1、M2 的入口缺失已在本机工作区解决；M3 已不只由 Vera 持有，但共同规则尚未进入根 `AGENTS.md`；M4 的需求与实现混放仍在，M5、M6 已有部分范围或来源说明，仍需定点补全。无需重新大规模压缩 memory。**

当前建议按以下状态理解：

1. **已解决：**`docs/AGENTS.md` 与 Wiki 总结的“评测口径与证据边界”均已存在，原 memory 指针可以保留。
2. **共同规则尚待统一：**Octo 新增了八条工程原则全文；若采用本报告的项目共同规则方案，应将正文归根 `AGENTS.md`，memory 留偏好、角色方法与入口。
3. **仍需整理：**定点处理应用实现细节重复、历史特例身份和来源依据，保留所有有效需求与限制。
4. **快照已调整：**当前 `docs/agent-prompts.md` 已修正测试证据复用、memory 归属及 Vera 简化建议标准；本次未核对保存的 agent row 和运行时采用情况。
5. **按需采用：**沿用现有文档职责与少数有实际需求的 skill；DeepSeek 参考分析仍按原审阅版本理解。

如果先看最实用的部分，读第 1、2、3、9、11 节。第 4–8 节解释参考项目里哪些做法值得采用，附录 A 保留原 28 项处置意见并补充本机新增的工程原则条目。

### 目录

- [1. 新版 memory 的评价](#s1)
- [2. Engineering principles 应该放在哪里](#s2)
- [3. 对 Engineer.txt 的逐条建议与可采用版本](#s3)
- [4. DeepSeek Harness 最值得借鉴的资料](#s4)
- [5. Defensive patterns：如何防错而不增加无谓复杂度](#s5)
- [6. Conventions：哪些适合 Octopus，哪些已有](#s6)
- [7. 文档管理与经验总结](#s7)
- [8. Prompt、skill 与上下文效率](#s8)
- [9. Octo、Vera、Weber 的具体提示词调整](#s9)
- [10. 可选的少数技能，以及如何判断优化有效](#s10)
- [11. 建议采用顺序与完成条件](#s11)
- [附录 A：memory 文件的处置意见](#appendix-a)
- [附录 B：证据、版本与核验范围](#appendix-b)

<a id="s1"></a>
## 1. 新版 memory 的评价

### 1.1 这轮整理已经解决了上一版的主要结构问题

原审阅重新比较了 `agents.zip` 和 `agents(1).zip`，没有直接沿用旧评审的问题清单。下表统计针对当时解压后的 Markdown 正文，不是 ZIP 压缩大小，也不是 2026-10-28 本机 memory 的重新统计。[M0] [M1]

| 指标 | 上一版 | 本轮新版 | 说明 |
| --- | ---: | ---: | --- |
| Markdown 文件 | 28 | 28 | 都是 5 个索引、23 个正文；质量改善没有依靠减少文件数 |
| UTF-8 字节 | 51,826 | 34,985 | 减少约 32.5% |
| Unicode 字符 | 30,158 | 21,290 | 减少约 29.4% |
| 行数 | 766 | 560 | 减少约 26.9% |
| Markdown 链接 | 93 | 69 | 不含以代码格式书写的仓库路径 |
| 包内相对链接 | 72 | 67 | 新版 67 个均能解析到存在的文件 |
| 失效相对链接 | 6 | 0 | 上一版报告的 6 个问题都已修复 |
| 指向其他角色私人目录的链接 | 17 | 0 | 共享约定不再依赖某个角色私人目录 |
| Markdown 绝对链接 | 21 | 2 | 现在集中在 `shared-memory/locations.md` |
| Octo 索引大小 | 2,527 字节 | 1,842 字节 | 三组任务入口更容易选择 |

最后两项尤其有价值。当前源码在硬删除 agent 时会删除其私人目录；把共同规则从那里移出，确实消除了一类引用生命周期问题。位置集中在一个入口，也降低了搬机后的维护成本。[O4]

**这些数字不能证明每轮 token 减少了 32.5%。** Codex 路径注入的是 memory 目录和读取协议，要求任务开始时读取 `MEMORY.md`；并没有自动把所有 28 个文件装进每轮 prompt。实际成本取决于模型读了哪些正文、是否重复读取，以及历史上下文如何保留。[O3]

### 1.2 值得保留的做法

新版已有以下良好特征，建议继续保持：[M1]

- **索引说明何时读取。** Octo 将入口分为开发验证、数据操作、Wiki 与应用，避免一个平铺列表要求所有任务逐项通读。
- **项目合同回到仓库。** `architecture-decisions.md`、`behavior-boundaries.md`、`runtime-audit-invariants.md` 基本成为任务导航，不再单独维护整套协议。
- **角色经验与共享约定分开。** 共享区只保留共同 Wiki 用法、验证协作和位置，Vera 的审查方法、Weber 的研究偏好仍留在各自目录。
- **保留了必要条件。** 验证证据只有在相关代码、配置、依赖和环境仍适用时才能复用；历史否决不是永久技术黑名单；设计选择不等于实施授权。
- **没有把“精简”理解成删掉用户真实要求。** `context-maintenance.md` 保留完整 AGENTS 开头的偏好，同时明确完整交付的范围；内容保全、原始资料与用户配置保护也仍然存在。

因此，我不建议再给 memory 设一个“必须减到 10 个文件”或“每条只能 200 字”的目标。现在的主要问题是归属是否准确、入口是否可用、内容是否仍适用。

### 1.3 优先修正的六类问题

**复核日期：2026-10-28；范围：本机工作区与当前 memory。** 仓库 HEAD 为 `0df8b78ec470c0186c0ef6bd758a3695f1e686d1`，判断包含未提交文件；此次没有重新 fetch 或验证公开仓库、已发布服务和长会话加载状态。以下替换原六项问题的当前状态，不能把本机已解决等同于已提交或已部署。

| 问题 | 当前状态 | 复核结论 |
| --- | --- | --- |
| M1：文档规则入口缺失 | 本机已解决 | 文件存在，根规则与文档索引均已入链 |
| M2：Wiki 评测章节缺失 | 本机已解决 | 同名章节存在，承接成本、样本与自然入口口径 |
| M3：共同工程方法归属 | 部分改善，尚未统一 | Octo 已有八条全文，但仍在私人 memory，根规则未收录或链接 |
| M4：需求与实现混放 | 仍存在 | 应用与私有 ops 已有说明文档，memory 仍重复部分实现细节 |
| M5：历史特例身份 | 部分改善，仍有缺口 | 仓库解释了搬迁专项范围，memory 的历史任务身份仍不完整 |
| M6：来源区分 | 部分改善，仍需核对 | 已有明确日期的用户反馈，也有泛称既有记录或未注明来源的条目 |

M4：按“需求是否保全”交接，不能只按文字是否具体来删

我会这样处理：

| 内容                                         | 拟议落点与处理                                       |
| -------------------------------------------- | ---------------------------------------------------- |
| 保留已有界面、原文阅读、Agent 负责发现和讨论 | 继续留在 SmartReader memory，作为稳定需求            |
| 中文覆盖实际正文和回复，避免同句缺字换字体   | 继续保留需求与验收条件                               |
| 缓存、压缩、公开地址切换、接口和脚本位置     | 由应用 README、源码及验证说明维护，memory 留入口     |
| Noto 字体选型、一次自动重试                  | 先确认是指定要求还是当前实现；不能因看起来具体就删除 |
| 固定回归网址                                 | 交给可定位的回归入口，并保留其适用条件               |
| 私有发布机制、版本绑定和操作步骤             | 归私有工具 README，memory 留位置和保护要求           |

这里有两个具体发现：

一是 SmartReader 的固定网址已经出现在 .checks/web-import.mjs，但应用 README 尚未介绍这个入口。应先补上入口及联网条件，再移除 memory 中重复的网址和脚本说明。

二是私有发布 README 尚未完整说明 memory 中“目录 0700、文件 0600”的适用对象。应先在私有说明中明确哪些目录、输入和敏感文件受此要求约束，再去重。

workspace-lifecycle.md 继续保留用户数据、并行工作、未完成发布的唯一恢复材料，以及私有工具不原样入库等保护条件。操作流程交给对应 README 和 local-wsl.md。

M5：给历史特例补可定位身份，保留原有限制

搬迁专项已经能够定位。我建议把“原搬迁专项 19–22”补成：

> 原《账户简化与搬迁计划》的账户搬迁专项 19–22，实施提交为 57565c0（2026-10-25）。现行范围与退役条件见架构合同“账户搬迁的开发约定”。该编号与暂停的架构执行票属于不同计划。

直接链接现行搬迁合同 (docs/plans/architecture-contract.md:411)，让读者无需猜测编号身份。“上一轮授权不覆盖新一轮”本身清楚有效，可以保留。

“那批知识/写作质量用例”目前还不能准确识别。08b2143 能证明发生过相关测试调整，但不足以证明它就是该句话的完整指代。拟稿应如实写成：

> 既有记录要求当次指定的知识/写作质量用例退出，不能仅换标记恢复；原用例清单尚未定位。涉及疑似对象时先核对原任务，不将本条扩展为禁止所有质量评测或自动删除测试的指令。

这样既保存历史限制，也限制其被误用的范围。

M6：补来源身份，避免把推导升级成用户命令

不新增强制元数据体系，只在影响授权、范围或历史例外的段落补一句来源说明：

| 已确认的依据  | 写法                             |
| ------------- | -------------------------------- |
| 原始用户反馈  | 写可定位的日期、任务或对话依据   |
| 当前项目合同  | 链接负责该事实的文档章节         |
| Agent 的推导  | 明写“建议”或“根据……推导”         |
| 只有旧 memory | 明写“既有记录，原始来源尚未核实” |



`Vera/architecture-simplicity.md:10–53` 仍保存必要性、事实 owner、派生状态和验证方法。根规则与架构合同已有部分相关约束，但不等于八条已经完成共同归属。

**此项部分改善，尚未达到本报告建议的共同规则目标。** 把 Octo 的对应条目改为用户偏好与入口；Vera 保留独立审查方法和角色反馈。不要因已有重叠就直接删除尚未交接的有效要求。

#### M4：少量应用条目仍混有“用户要求”和“当前实现”

**此项仍存在。** 当前 `Octo/smartreader.md:13–24` 仍将原文阅读、中文字体体验等稳定需求，与缓存方式、具体字体、字体清单和检查脚本放在一起。`Octo/workspace-lifecycle.md:9–13,20–23` 也保留私有发布目录结构、权限和操作细节；其中位置入口、用户数据保护及不将本机工具原样入库的偏好仍有保留价值。

此次已只读核对 SmartReader 自己的 `README.md` 以及私有发布目录、工具目录的两份 `README.md`：应用文档已说明缓存、字体和验证脚本，私有文档已有发布机制与回执职责。因此不能再沿用“未读取、未知是否有 owner”的旧结论。

后续整理应将已被应用或私有文档完整承接的实现细节换成准确入口，memory 保留稳定需求、位置和保护条件。并未证明每个权限细节都已在目标文档承接，不能为去重先删唯一记录；私有运维正文也不应复制进公开仓库。

#### M5：历史特例需要可识别的适用范围

**此项部分改善，历史身份仍有缺口。** 当前 `Octo/migration-scope.md:14–16` 保留“原搬迁专项 19–22”；`Octo/wiki-test-tiers.md:13,15–17` 仍写“19–22”“上一轮迁移授权”和“已要求退出的那批知识/写作质量用例”。

[架构合同的“账户搬迁的开发约定”](../plans/architecture-contract.md#账户搬迁的开发约定)已说明 19–22 的验收范围、专项用例退役及当前保护条件；“暂停范围与恢复条件”也区分了另一组执行票。这部分范围已有稳定说明，但 memory 尚未直接指向这一小节，“那批质量用例”仍没有可定位的任务、提交或清单。

“上一轮授权不覆盖新一轮”本身是可独立理解的有效限制，应保留；真正需要补的是历史专项和例外的身份。可直接补已有合同入口；其余依据找不到时标明“历史限定，执行前核对对应任务”，不得据此自动删除同名或同编号测试。

#### M6：来源说明要区分用户明确要求与 agent 推导

**已有部分清楚的来源说明，尚不能视为全部完成。** 例如，当前 `shared-memory/verification.md:19–22` 明确注明“用户于 2026-10-28 明确要求”，并给出既有登录验证的适用条件；`Octo/context-maintenance.md:8` 也标明明确用户偏好及适用任务。

仍有条目只称“Octo 既有换机约定”“Octo 既有测试取舍反馈”等，无法单凭文字追溯哪些具体句子是用户原要求、哪些是整理者推导；新增 `Octo/engineering-principles.md` 也未注明来源。此处是可追溯性的核验缺口，不能据此认定内容错误或擅自给每句补成“用户明确要求”。

针对容易影响授权、范围或历史例外的条目，补可确认的任务、日期、文档依据，或注明“既有记录，原始来源待核对”；agent 推导应明确标成建议。不需要给所有 memory 新增一套强制元数据，也不能用编辑日期替代事实验证日期。

本次仅复核并更新报告，没有改动上述 memory、根规则或保存的配置。

### 1.4 保持现在的目录，采用更明确的写入规则

当前布局可以继续使用。`shared-memory` 是同账户文件树中的普通约定，通过各角色索引中的相对链接访问；现有装配代码没有单独自动注入这个共享目录。不要为了共享再增加一套 memory 数据库、全量加载器或同步服务。[M1] [O3] [O4]

建议将下面这组规则用于持续整理，而不是每次再做一次全库改写：

1. 只保存会改变未来决策的信息；任务进度、一次命令结果和当前服务状态留在任务记录或现场证据中。
2. 用户偏好、角色反馈、位置和触发式入口进入 memory；工程合同和设计决定进入仓库 owner。
3. 更新同主题条目，不按对话日期新增近似副本。
4. 保留条件、例外与适用范围，不把一次事故变成禁止某机制的永久口号。
5. 同账户多个角色确实共同使用的内容才进入共享区；共享正文不依赖某个角色的私人路径。
6. 搬走正文前，先确认目标文件和章节已经存在，并承接所有有效要求；然后更新入链和索引。
7. 保存了文件，不等于运行中的会话已经读取；部署、权限和执行效果仍需相应证据。

现有 YAML `name / description / metadata.type` 和 `MEMORY.md` 索引协议应保留。如将来调整 Codex 的 memory 读写指令，直接修订现有 `render_memory_blurb` 即可，不需要新增另一个策略注入层。[O3]

<a id="s2"></a>
### 2.2 内容归属表

这张表是职责分配，不要求你新建表中每一种目录。

| 内容 | 主位置 | memory 应保留什么 |
| --- | --- | --- |
| 本项目所有工程工作遵守的八条原则 | 根 `AGENTS.md` | 用户强调这些原则的短偏好与入口 |
| Python/TS、工具链、路径与提交约定 | 现有 Conventions、`testing.md`、`local-wsl.md` | 需要时去哪里查 |
| SessionOwner、事务、完成、退休、Wiki 恢复等具体合同 | 已有 `docs/plans/architecture-contract.md` 等 owner | 哪类任务必须查哪个章节 |
| 用户要保留原始资料、不要擅自改保存配置等稳定偏好 | 用户/反馈 memory；形成项目约束的部分进入仓库 | 原意、必要条件和个人适用范围 |
| Octo/Vera/Weber 的职责与协作交付 | 保存的 persona；文档保留可复制快照 | 个性化反馈，而非整份 persona 副本 |
| 如何进行一类重复的复杂工作 | 确有需要时使用按需 skill | 用户对方法的偏好或入口 |
| 某次事故的证据、被推翻的假设、取舍 | 版本明确的复盘或原设计记录 | 会改变未来选择的短提醒 |
| 当前 task、PID、发布版本、今日测试结果 | 当次任务材料与现场工具 | 不当作长期当前事实保存 |
| 私有应用与运维位置 | 对应角色 memory 或私有文档 | 位置和核验条件；不复制到公开工程仓库 |

Octopus 的 `docs/README.md` 和 `architecture-contract.md` 已经承担其中大部分职责。应沿用这些现有 owner，而不是再造一套名字不同但内容相同的目录。[O2] [O7]

### 2.3 “通用”不意味着“必须放 memory”

这里有两种不同需求：

- **我希望所有人在 Octopus 中按这些原则开发。** 放项目 AGENTS。
- **这是我在多个项目中都偏好的工程方式。** 可以在个人偏好中保留简短描述；若需要自动成为每个项目的指令，再选择已确认会被实际 harness 读取的个人指令入口。

第二种需求不应直接变成“把全文再复制到所有 memory”。Octopus 的 Codex 路径会使用凭据对应的 `CODEX_HOME`；不能未经核对就保证修改宿主默认 `~/.codex/AGENTS.md` 会影响所有运行实例。项目 AGENTS 的具体发现和刷新仍由后端机制负责，本次没有运行真实 CLI 来证明已有长会话何时重读。[O5] [O6] [O16]

<a id="s3"></a>
## 3. 对 Engineer.txt 的逐条建议与可采用版本

### 3.3 不建议继续加进通用原则的内容

具体数据库同步级别、SessionOwner 的字段关系、Wiki 准入细节、特定 CLI 的尾部行为、某次迁移退出规则，应继续由现有设计合同负责。[O7]

同样，不要为了体现“严格”再增加这些绝对条款：任何错误都停全服务；任何失败都自动重试；所有恢复必须自动化；所有对象必须有状态机；内部一概不校验；所有新增行为都先插件化；所有改动都跑全量真实模型测试。

它们会覆盖原稿中已经写好的适用条件。遇到具体问题，应回答“保护哪个事实、谁负责、何时失效、如何验证”，而不是再加一个更强烈的口号。

<a id="s4"></a>
## 4. DeepSeek Harness 最值得借鉴的资料

### 4.1 优先借用方法，按需选择资料

DeepSeek Harness 的优势在于：把原则、现行合同、设计理由、事故记录和可重复工作流分开，并给它们清楚的读取入口。它的 `docs/AGENTS.md` 划分这些职责；skills 则把具体工作组织成有范围的流程。[D17]

下面是本轮重点阅读的资料。**“建议采用”是针对 Octopus 的判断，不表示原项目的所有要求都适用，也不要求每次任务读完这张表。** 当前没有必要复制整套 skills。

| 资料 | 值得学习的做法 | 在 Octopus 中怎样用 |
| --- | --- | --- |
| [根 AGENTS 的 Conventions][D01] | 将可执行约定与详细依据连接；区分静态内部接口与外部输入边界 | 工程原则在根文件；Python/TS 的具体约定保留自己的工具链和合同 |
| [Defensive patterns 入口][D02] | 按改动对象选择规则；规则带故障场景与适用边界 | 给审查者按主题入口，避免每次加载全部历史防御条目 |
| [general][D03] | 明确时序、状态含义、共享状态 owner 和真实退出 | 用于异步、生命周期与 subprocess 变更的定向审阅 |
| [remote-and-api][D04] | 业务责任留在对应服务；调用身份和资源访问可追踪 | 沿用 router/service/授权 owner，不引入 Cordis 的远程接口体系 |
| [client-components][D05] | 组件身份、订阅范围和实例 scope 清楚 | 用于 React/zustand 的流式更新和多会话界面；不照搬其 Slot 专属禁令 |
| [conversation-and-session][D06] | 一个权威读取路径；保留与释放明确；避免隐藏的全量历史读取 | 检查 Octopus 的会话投影、Wiki compact 查询和保留成本 |
| [dsh-error-handling][D07] | 错误分类、效果状态、安全继续条件、报告责任分别明确 | 为现有八条原则补充具体案例；不扩展为所有异常全局停机 |
| [dsh-find-simplifications][D08] | 追踪真实生产者、消费者和效果，衡量能移除哪些持续维护责任 | Vera 把已证实缺陷与可选简化分别报告，说明保留理由与能力损失 |
| [simplification 历史案例][D09] | 用已接受和被拒绝的案例校准判断 | 提炼少量 Octopus 自己的正反例，不把历史结果当当前删除清单 |
| [依赖选择的决策记录][D10] | 判断采用依赖后是否净减少自有实现、测试和适配维护 | 比较完整维护成本，避免“依赖越少越精简”或“用了库就算简化” |
| [dsh-code-review][D11] | 用实际调用链和行为判断问题，检查模型真正看到的接口 | 为 Vera 建立范围明确、证据充分的审查输出协议 |
| [dsh-ci-test-reliability 与诊断参考][D12] [D13] | 资源隔离、确定性交错、真实清理；先定位失败类别 | 使用 pytest 的现有隔离设施与事件同步，不靠 sleep 和反复重跑掩盖 race |
| [testing 与 pre-push][D14] [D15] | 证据匹配要证明的行为；未失效的检查可复用 | 沿用 Octopus 现有提交/发布门禁，不用别人的门禁覆盖本项目约定 |
| [dsh-speed-up-perf][D16] | 明确用户可见完成点、比较条件与端到端证据 | 用于 Wiki 完整任务成本，区别组件优化和真实用户收益 |
| [docs/AGENTS 与 dsh-doc][D17] [D18] | 文档各有职责；教程和参考服务不同阅读任务；入口可检索 | 优化已有 `docs/README.md`，必要时建立一个简短文档规则文件 |
| [dsh-prose-standard][D19] | 缩写时保留主体、条件、时序、义务强度、例外和后果 | 用于 AGENTS、memory、文档与注释；压缩不得损失实际要求 |
| [dsh-trim-cot-leakage][D20] | 清理只有作者当时会话才懂的引用和叙述 | 将“上一轮第七条”“当时 reviewer 说”改成可独立理解的事实与来源 |
| [Agent Notes、归档与 postmortem][D21] [D22] [D23] | 决策理由、真实替代方案、事故证据与当前状态分别保存 | 继续用 `docs/plans/`、`post-mortems/`、`review/`；不新造同义 ADR 目录 |
| [agent-experience][D24] | 信息可发现、关键限制在行动前可见、结果有界，衡量总工作量 | 用于 memory 索引、Wiki 操作卡、工具描述和按需指南 |
| [dsh-session-projection][D25] | 可恢复事实与运行资源分开，缓存可重建，恢复不重放外部效果 | 用其语义检查现有设计；不因此引入 DeepSeek 的 Projection 注册系统 |

其中，如果只选四项深入看，我会选 **prose-standard、find-simplifications、error-handling、agent-experience**。它们分别改善“写得准”“删得对”“失败时仍可信”“模型能以合理成本用对”，与你当前最关心的问题直接对应。

### 4.2 不宜直接照搬的做法

| 做法或可能的误读 | 不宜直接搬到 Octopus 的原因 |
| --- | --- |
| Everything is a Plugin、Cordis 注册/生命周期规则 | 与其架构一致，不能证明 Octopus 所有新增能力都需要插件化 |
| Slot 架构下禁止某些 React composition 方式 | 这是其组件扩展体系的约定，不是普通 React 的普遍规则 |
| 每文件 100% coverage 及其 CI/真实 API 安排 | 依赖团队预算、发布承诺与测试设施；Octopus 已有自己的验证矩阵 |
| 双语成对文档、sidecar、冻结归档 manifest | 对其语料治理有价值，个人项目直接引入会增加长期义务 |
| 特定 TypeScript 约束和 branded ID 工具链 | 可借类型表达思想，但 Python 注解本身不构成运行时验证 |
| 将小清理放 TODO/FIXME 或外部 issue 的流程 | Octopus 明确有自己的完整交付要求和本地任务约定，应保持这些约定 |
| 对参考文档统一硬裁字数 | DeepSeek 的文档 skill 本身就反对这样做；入口预算不能覆盖完整性 |
| skills 互相引用的整张网络 | 可以参考组织方式，但不应让一次普通任务递归读取所有背景材料 |

这些差异均可从对应约定看出；采用某一条前先检查它依赖的架构和工作方式。[D01] [D05] [D14] [D17] [D18] [D20] [D21]

<a id="s5"></a>
## 5. Defensive patterns：如何防错而不增加无谓复杂度

### 5.1 把防御规则写成有条件的工程约束

DeepSeek 的 defensive-patterns 采用“规则保护什么、避免什么故障、适用何处”的写法。这比不断累加“永远不要 X”更容易审阅。[D02]

对 Octopus，我建议首先沿现有 `architecture-contract.md`、`testing.md` 和 Wiki 合同增加缺失的例子。如果同类复盘反复出现、审查者又难以找到规则，再增加一个短的 `docs/defensive-patterns.md` 作为审查入口。**现在不用新建五个分类目录，也不要把八条原则换个名字再复制一遍。**

一个有用条目只需要回答：

> 何时适用 → 必须成立的事实 → 一个具体错误做法 → 现有责任方应如何保证 → 怎样验证 → 详细合同在哪里。

### 5.2 适合用于 Octopus 的八组审查问题

以下是结合你的原则和项目职责提出的检查例子，**不是本轮新复现的八个 bug，也不是八个待添加的子系统**。先查现有实现与合同是否已经满足。[E1] [O7] [O8]

| 触发改动 | 应检查的事实 | 可用来挑战设计的反例 | 优先落点 |
| --- | --- | --- | --- |
| 外部写入、重试、超时或取消 | 超时结束的是等待，还是效果已被证明未发生？重复操作依据是什么？ | 等待超时后无幂等身份直接重新执行写入 | 原操作 owner 与效果/恢复合同 |
| 凭据刷新、配置更新、job claim | 旧异步结果能否覆盖较新的事实？逻辑 owner 是否覆盖真实并发范围？ | 旧 refresh 失败把后来保存的新凭据标成不可用 | 凭据或任务 owner；必要的版本条件更新 |
| Session 完成、通知、持久化、退出 | 哪些事实独立成立，消费者分别需要什么？ | 把业务成功、通知失败、资源未退出压成一个 success bool | 原完成类型和消费者合同 |
| subprocess、后台任务、listener 退出 | 发出 stop 与确认资源退出是否分开？最终写入依赖是否仍存活？ | 返回“停止完成”后旧进程还在写文件 | 已有资源 owner 和退休过程 |
| Wiki 材料复用与请求去重 | 同材料是否真等于同一工作意图？scope、sources、目标怎样参与？ | 材料相同就静默吞掉新的摘要或来源要求 | 接纳/幂等语义，返回明确冲突或复用结果 |
| compact 查询、日志、工具返回 | limit 是否约束实际读取、保留和输出，而不只是最后展示？ | 最后只显示 3 条，却先载入所有大正文或全部错误详情 | 查询投影与输出 owner；保留可检索详情 |
| API、MCP、队列、存储与配置 | 外部数据在哪里验证？操作时的授权和并发前提是否仍成立？ | 把“prompt 要求只读”当作执行器已禁止写入 | 实际执行点的权限/类型/条件检查 |
| 并发、恢复、退出测试 | 测试是否确定触发目标交错并观察真实效果？ | sleep 一小段时间，就假定两次刷新已经重叠 | 现有测试层；事件、barrier 与真实资源观察 |

这些问题受到 DeepSeek 的错误处理、状态、生命周期、审查和测试方法启发；上表中的项目落点与反例是针对 Octopus 的适配。[D03] [D07] [D11] [D12]

### 5.3 需要同时防止的几种误用

**单一 owner 不能替代动态条件。** 一个 SessionOwner 串行处理一个会话，不代表两个会话共用的凭据已经串行。相反，建立 owner 之后也不该让所有下游再各加一把锁。判断依据是实际共享对象、更新范围和跨边界变化，而不是某个类名是否含 Owner。

**边界验证不能缩成“validate once”。** 外部输入解析完后，可以避免内部重复校验同一静态格式；但队列等待后权限撤销、资源版本变化、文件被外部修改是新的前提。Python 类型注解也不能替代入口验证。[E1] [D01]

**fail-stop 需要有明确范围。** Octopus 当前合同接受权威存储提交失信后停止本代业务，同时明确业务冲突、正常取消、认证失败和通知断线不一概升级为 fatal。通用原则应保留这个区分，不另开一套万能恢复器。[O7]

**减少状态不能丢失事实。** 可派生的当前视图和不可重建的历史效果证据不同。清理重复状态，不等于删除 receipt、journal 或中断证据；缓存恢复也不应重新调用模型或重复外部写入。[E1] [D25]

**判断“简化”要比较完整替代成本。** 把一段逻辑移进 helper、缩短一个文件、删掉没有静态调用者的接口，都不能单独证明维护责任减少。应同时说明实际消费者、仍需保留的行为、替代代码和最强的保留理由。[D08]

<a id="s6"></a>
## 6. Conventions：哪些适合 Octopus，哪些已有

Engineering principles 负责选择方法；Conventions 负责项目已经确定的具体做法。两者不能互相替代。DeepSeek 的主要 Conventions 在根 AGENTS，并没有一个需要照抄的 `docs/conventions/` 目录。[D01]

| 主题 | Octopus 当前已有落点 | 建议 |
| --- | --- | --- |
| Python、React、TypeScript、zustand | 根 AGENTS 的 Conventions | 保留现有栈与明确的 store 使用要求 |
| venv/pip、JS PATH、本地运行方式 | 根 AGENTS、`local-wsl.md` | 一处维护环境细节，memory 不记录不断变化的工具版本 |
| 测试选择、有效证据、提交/发布门禁 | `testing.md`、根 Verification | 保留已有门禁；修正 persona 对重跑的绝对表述 |
| owner、事务、独立完成事实、恢复 | `architecture-contract.md` | 原处补必要例子，不新增同义“并发原则全文” |
| 账户、工具权限与身份 | 所属服务和执行合同 | 身份取可信上下文；当前权限在依赖它的操作处检查 |
| 当前持久格式、发布交接、搬迁 | `current-storage-format.md`、`backup-and-migrate.md` | 新持久类型改变时，同步真实消费者与搬迁范围 |
| API 和前端类型 | 源码及已有生成合同流程 | 可生成的定义不再手写第二份；新增语义仍须文字说明 |
| 默认值、配置错误 | 配置 owner 与具体调用入口 | 明确缺省、空、未知、拒绝的区别；按真实需求决定何处失败 |
| 错误身份与诊断 | 对应 API/错误类型与测试 | 机器判断使用稳定分类；用户报告说明影响和安全下一步 |
| 迁移/搬文件与语义变更 | 当前任务 scope 与设计 owner | 让差异可审查；语义变化明确说明，不能藏在目录调整里 |
| 文档维护 | 根 Conventions、`docs/README.md` | 如果扩展规则，再集中到文档专用文件，根只保留入口 |

已有落点可由根规则、架构合同和文档索引核对；上表最后一列是维护建议，不表示每个具体错误类型或边界已经过本轮行为验证。[O1] [O2] [O7]

对新约定，可以用一个简单门槛：**它是否会改变实现或审查行为，是否有清楚的适用条件，是否有真实的执行或验证位置？** 如果只有“代码要优雅”“所有接口应可扩展”而没有判定方法，不如保留在解释中，不升级为强制条款。

<a id="s7"></a>
## 7. 文档管理与经验总结

### 7.1 保留 Octopus 现有结构，减少重复事实

Octopus 已区分当前指南、`docs/plans/`、`post-mortems/`、`review/`，并有自动生成的 plans 索引。这与 DeepSeek 想解决的知识归属问题相当接近；差距主要在个别文档的准确性和边界，不需要推倒重建。[O2] [D17]

建议继续采用以下分工：

| 读者要解决的问题 | 去哪里 |
| --- | --- |
| 每次工程工作必须遵守什么 | 根 AGENTS |
| 系统当前有哪些组件，怎样联系 | `architecture.md` |
| 一个特定机制的语义、限制和取舍是什么 | 已有主题 owner，如 `architecture-contract.md`、Wiki 合同 |
| 怎样安装、测试、发布或搬迁 | 对应操作指南 |
| 为什么作过一个重要选择、何时重考虑 | 原 owning plan 的决策/取舍部分 |
| 某次事故怎样发生、为什么未被挡住 | 版本明确的 post-mortem |
| 某次审阅查了什么、发现了什么 | 带日期和基线的 review 报告 |
| 怎样重复完成一种复杂任务 | 有实际需求时使用 skill |

一个主题的持续决定仍可以留在现有 `docs/plans/`。无需仅因名字包含 plans，就把全部已实现合同重新迁进一个新 ADR 体系；重点是当前、拟议和已放弃的范围清晰可辨。

### 7.2 最值得采纳的六项维护规则

1. **先确定哪个文件负责，再改文字。** 一项事实在 owner 中改正，其他位置保留必要入口；不要在旧段落下追加一段日期更晚的相反说法。
2. **保留完整命题。** 缩写前检查主体、动作、条件、时间顺序、必须/允许/禁止、例外与后果。文字变短而义务变了，不是纯文档优化。[D19]
3. **必要的局部说明可以保留。** 调用点需要知道取消不撤销效果，就应在那里说清；详细原因可以链接。一个事实有一个 owner，不等于禁止一切有用复述。[D19]
4. **入口围绕任务组织。** 修改 runtime、做 Wiki 维护、发布，应各自能找到少数相关页面；不要求所有任务先读全库。DeepSeek 的“三到五页”是新读者能建立必要理解的目标，不是每次任务最低阅读配额。[D18]
5. **移动正文要完成交接。** 确认目标存在且内容完整，再更新入链、索引和引用章节。原审阅发现的两个指针漂移已在此次本机复核中关闭，后续搬迁仍适用同样的检查。
6. **自动化检查容易客观漂移的部分。** 先利用已有索引与生成合同检查，再补有价值的链接/章节检查。语义是否被削弱仍需审阅，不必把每个写作偏好都做成 lint。

### 7.3 是否需要新增 `docs/AGENTS.md`

**2026-10-28 本机复核：已建立，无需再次新增。** 现有 [docs/AGENTS.md](../AGENTS.md) 已负责文档归属、语义保留和验证；根 Conventions 与文档索引都保留了入口，见 M1。

应继续由这一份正文维护详细规则，根 AGENTS 保留“写文档时读取”的入口，`docs/README.md` 专注导航。下面保留原审阅的适配草案作为方法示例；当前有效规则以现有文件为准，不再作为待新建任务：

```markdown
# Documentation rules

Use the existing document that owns the subject. Current guides describe
current behavior; design records preserve durable decisions and trade-offs;
post-mortems preserve incident evidence; dated reviews preserve their scope
and revision. Do not create a second owner for the same decision.

Before shortening or moving prose, preserve its actors, conditions, timing,
obligations, exceptions, ownership, failure behavior, and consequences.
Keep the local facts needed for correct use; link to the owner for detailed
rationale, history, and examples.

Write for the reader's task. A procedure gives prerequisites, actions, and
observable verification. A reference defines its lookup scope and complete
current behavior. Split them only when their mixture obstructs either task.

Update the owning section when behavior changes. State proposals and historical
observations as such. Do not append corrections that leave contradictory
current claims, or copy generated contracts and inventories into manual prose.

Move content together with its incoming links and index entries. Verify that
each replacement pointer reaches an existing section containing the required
information. Do not remove the only surviving decision rationale or evidence.

Use the checks appropriate to the changed material. Documentation-only edits
follow the repository's existing authorization and verification rules.
Changes to model-visible prompts or user-visible strings also need evidence
for the behavior they change; prose edits do not prove runtime adoption.
```

这份草案只表达适合本项目的文档方法。它不包含 DeepSeek 的双语配对、归档冻结、每个仓库相同字数上限或发布命令。

### 7.4 将“经验总结”转成可用知识

一次故障后，可以区分三个产物，而不是把整段聊天抄进 memory：

| 产物 | 应保存的内容 | 不应混入 |
| --- | --- | --- |
| 当前合同或回归测试 | 以后必须成立的行为，以及可验证边界 | 当时调查者的逐步猜测 |
| 决策/复盘记录 | 当时的事实、被推翻的假设、原因、真实替代方案和验证缺口 | 为凑格式编造的备选方案 |
| memory 提醒 | 用户偏好、角色方法，以及何时查这个 owner | 完整技术正文、当日绿色结果、未核验的上线状态 |

DeepSeek 的 Agent Notes 只要求在有长期决策理由时新增记录，允许更新已有 owner；其复盘和裁剪方法强调保留可解释未来选择的证据。这种“有价值才记录”的标准适合采用。[D21] [D23]

例如，“上次 wrapper 太多，所以以后不要 wrapper”会误删承担授权、生命周期或兼容职责的适配层。更有用的总结是：

> 对没有独立责任的转发层，先确认删除后哪些维护义务一起消失。承担权限、生命周期或真实兼容职责的边界可以保留；判断应依据当前生产者、消费者和合同。

这是可应用的经验，不是历史机制黑名单。

### 7.5 一个当前值得整理的例子：CLI 提示词说明

`docs/cli-system-prompt-notes.md` 开头将 Claude 描述为每轮新进程、未显式交付就看不到 memory；同页的 memory 表格、当前 memory override 设计与可复用运行模式之间存在不一致。[O11] [O12] [O17]

但不能把这份文档全部判为过时。其中“测试、构建、安装一律使用 bg”仍在当前 Claude 的工具提示中；Codex 的提示才采用 long-running/unbounded 的条件式措辞。两个后端需要分别说明，不能用一个结论覆盖。[O13] [O14]

建议把它整理成“各来源负责什么、各后端如何加载、哪些生命周期已验证”的当前参考；历史原因保留明确版本。委托返回前必须收齐核心证据与跨回合 bg 结果的关系也要讲清。本轮只确认了源码和文字之间的关系，没有运行真实 Claude 证明具体会话行为。

<a id="s8"></a>
## 8. Prompt、skill 与上下文效率

### 8.1 从 DeepSeek 借用“容易发现，按需展开”的设计

`agent-experience` 的重点是：入口先给用途、动作和关键限制，详细资源有清楚的获取路径，输出有边界；省上下文必须同时观察是否增加搜索、重读或错误。[D24]

对你当前的 memory，这意味着：索引不是文章摘要大全，而是任务路由；正文不必都变成一个链接，也不该没有读取条件。对 tool/prompt，则意味着参数规则优先放参数 schema，业务含义放工具合同，角色 prompt 专注职责，避免四处复述同一套工具说明。

### 8.2 Octopus 的 Wiki 已经有值得沿用的对应机制

当前 `wiki/workflow.py` 已有简短 bootstrap、按用途加载指南、内容 digest 复用、当前操作卡，以及每次 mutation 继续检查 live 权限的设计。**优化这些具体入口，比把全部 Wiki 流程重新塞进 persona 或 memory 更合适。**[O10]

应保持的区分是：

- 指南是否已经读过，属于内容复用。
- 当前 assignment、文档身份和权限是否仍成立，属于实时操作条件。
- 模型说做完了，不能替代持久回执、输出版本和真实终态。

这三件事不能用一个 hash、一段 memory 或一次旧工具回执代替。[O8] [O10]

### 8.3 借鉴加载语义，不盲目移植加载器

DeepSeek 的 instruction 文档明确说明初次加载、目录范围、后续刷新、内容去重和预算限制；skill 工具区分目录摘要与完整正文，已直接交付的 skill 不再重复加载。[D26] [D27]

值得学的是把“模型到底看见了什么、什么时候变化、什么被截断”写清楚。Octopus 不必因此另建一套原生后端已经负责的 AGENTS loader。

当前 Octopus 中需要准确说明的事实包括：[O3] [O4] [O5] [O6] [O16]

| 内容 | 本轮可核对的交付方式 | 不应由此推断 |
| --- | --- | --- |
| Codex memory | 注入路径与读写协议，任务开始读索引 | 全部 28 文件自动进入每轮 prompt |
| 共享 memory | 各角色索引的普通文件链接 | 框架自动发现并全量注入共享目录 |
| persona | `compose_system_prompt` 使用传入的 persona | 改 `docs/agent-prompts.md` 就改了保存的配置 |
| Codex 稳定控制指令 | 相同 checkpoint 复用，变化时再注入 | 只要 memory 文件改了，长会话立即重新读取 |
| 项目规则 | 由所选原生后端的发现机制负责；具体文件名、范围和刷新时机按后端核对 | 所有后端都原生识别同一个 AGENTS 文件名，或任意宿主默认目录都会被读取 |

如果以后要优化 prompt，先画清上述来源和交付责任，再删重复内容。没有必要为“看起来更短”增加更多间接文件、隐式导入或缓存层。

### 8.4 给模型看的文字也需要行为证据

对普通维护说明，链接和语义复核通常足够。对影响工具选择、权限理解、异步交付的 prompt，改变措辞可能改变行为，应选择能证明该变化的例子，而不只看字数。

例如，缩短 Wiki 入口之后，可以观察模型是否仍能主动检索、是否正确区分查询和维护、是否在结果足够时停止、是否仍保留来源义务。若删掉一句让工具误用增加，即使首轮 tokens 下降也不能称为优化。这是对 DeepSeek “prompt 也是行为、总工作量比入口大小更重要”的适配。[D19] [D24]

<a id="s9"></a>
## 9. Octo、Vera、Weber 的具体提示词调整

### 9.1 先区分快照与实际配置

[docs/agent-prompts.md](../agent-prompts.md) 明确说明：它是便于复制和审阅的快照，运行时以设置页保存的 agent row 为准。**2026-10-28 复核时，本机快照已包含有条件的测试证据复用、工程合同与 memory 分工，以及 Vera 简化建议的证据要求。** 因此第 9.2–9.4 节保留为原审阅的修改依据与示例，不再将其中旧句子描述为当前快照缺陷。[O9] 仍指向原审阅版本。

此次未读取运行数据库，不能确认保存的 persona 是否已采用。若需核对或采用，应以当前保存配置为准，保留用户的模型、工具与其他设置，并检查后续会话实际采用的指令；仅修改 Markdown 不算运行配置更新。

### 9.2 Octo：修正证据复用的绝对表述

原审阅快照第 54 行曾要求审查后不重跑同一套 suite；当时的共享 memory 和根 AGENTS 已采用“证据仍有效才复用”的更准确规则。[M1] [O1] [O9]

建议替换为：

```text
Give each check one executor. Reuse passing evidence while its code,
configuration, dependencies, and environment remain valid. After fixes or a
concrete new failure hypothesis, rerun the checks whose evidence was invalidated.
```

这保留了你反对重复消耗的目的，也允许修复后做必要验证。无需把所有 test tier 和命令复制进 persona。

### 9.3 Octo 和 Vera：把工程合同与私人 memory 分开

原审阅快照中，Octo 第 77 行曾将长期项目决策和用户偏好一并放 memory；Vera 第 132–134 行也曾鼓励把项目不变量记入私人 memory。这是当时建议调整归属的依据。[O9]

可以用以下短段替换相关句子：

```text
Keep shared project contracts and durable design decisions in their owning
repository documents. Keep stable user preferences, role-specific feedback,
and pointers to those owners in the provided memory. Do not treat memory as
evidence of the current code, configuration, deployment, or authorization.
```

若将完整工程原则放入根 AGENTS，Octo 的角色 prompt 不必再复述八条。保留它特有的交付责任、委托格式、版本固定、协作时机和结果收集方式即可；此次复核时原则正文仍在 Octo memory，见 M3。

### 9.4 Vera：增加“简化建议”的证据要求，保留独立判断

原审阅时 Vera 已要求触发条件、影响、位置和证据，也明确不制造发现。当时建议保留这些要求，并补充以下段落，使“审 bug”与“建议精简”都可评价；当前本机快照已采用相应标准：[O9]

```text
Separate confirmed defects from simplification proposals and unverified
hypotheses. For a simplification, trace the effective producers, consumers,
and effects; state which maintained responsibilities disappear, which behavior
is retained or intentionally lost, and the strongest reason to keep the current
design. A lower line count alone is not sufficient evidence.
```

这段是针对角色的工作要求，不要求每个简单改动都写架构报告，也不允许审查者未经当前任务授权就实施所有建议。

### 9.5 Weber：当前重点是保持边界，不必加入工程原则全文

Weber 当前 prompt 已区分作者观点、用户观点和自己的解释，保留讨论与知识写入的边界，并让 Wiki 工具和指南负责实际维护过程。其角色不需要常驻全部工程原则、发布流程和测试方法。[O9]

只有当 Weber 在软件仓库内实际执行工程任务时，才需要读取适用的项目规则；普通文章讨论不应被工程工作流包围。其私人研究偏好、PDF 材料准备要求和来源组合意图仍应保留。[M1]

### 9.6 如需调整框架 memory 指令，采用增量修订

现有 `render_memory_blurb` 已有索引、frontmatter、更新同主题文件和不存秘密/临时状态的要求。可以把“值得保存的事实”定义补得更准确，保留其他协议：[O3]

```text
Use the index to choose entries relevant to the current task; do not read every
entry by default. Keep stable user preferences, role-specific feedback, and
durable pointers here. Shared engineering contracts and design decisions belong
in their owning repository documents. When replacing a memory body with a
pointer, first verify that the destination exists and preserves the needed
information. Memory does not establish current runtime state or grant authority.
```

这是候选修订文本，不需要另加一个 memory-policy loader。Claude 依靠原生 memory 的部分，应通过它实际读取的入口表达适用约定；不能假定修改 Codex 专用 blurb 会自动改变所有后端。

<a id="s10"></a>
## 10. 可选的少数技能，以及如何判断优化有效

### 10.1 如果要引入 skill，先从两个明确工作流开始

目前只是建议职责和内容，不是安装请求，也没有生成或安装新的技能包。官方 OpenAI skills 保持原样，符合你的现有偏好。[M1]

| 候选工作流 | 何时使用 | 输入与输出 | 明确不承担 |
| --- | --- | --- | --- |
| 工程审阅与简化 | 用户要求 review/简化；或改动跨 owner、持久化、生命周期、接口语义 | 输入实际范围、基线、允许的动作、已有证据；输出已证实缺陷、简化建议、保留理由和验证缺口 | 重新定义八条原则、默认全仓审计、自动实施所有发现 |
| 文档与 memory 维护 | 用户要求维护规则；或行为变化需要同步说明 | 输入变更事实与受影响材料；输出 owner 映射、必要改动、保留项、链接/索引验证和未核验事实 | 重建目录、默认全文压缩、为每次小改创建决策记录、自动发布 |

其收益应来自减少经常遗漏的步骤。若现有 AGENTS 路由已经足够，就暂不建立 skill。性能调查、CI flake 诊断可以先作为参考页，反复使用且流程复杂时再独立拆分。

#### 工程审阅与简化：建议步骤

1. 从用户任务确定范围、基线和动作边界；可从当前上下文确定的内容不再反复询问。
2. 读取适用 AGENTS 和相关 owner，区分原始要求与待审方案自己增加的规则。
3. 追踪真实生产者、转换、消费者和效果，识别故障及最有力的保留理由。
4. 将已证实缺陷、可选简化、待验证假设分开；说明删除了哪些持续维护责任。
5. 用最小但充分的证据检验关键主张；报告实际范围和限制后结束。

#### 文档与 memory 维护：建议步骤

1. 判断信息属于当前合同、决定理由、历史证据、用户偏好、角色方法还是临时状态。
2. 找到已有 owner；同一主题优先修改原处。
3. 保留完整含义，删除重复解释和无法独立理解的过程叙述。
4. 需要搬迁时先验证目标已承接，再更新入链和索引。
5. 检查受影响的路径、章节、生成内容与实际语义；区分文件已修改和运行时已采用。

这两个流程借用 DeepSeek 的判断方法，但不复制它的命令、强制目录与审批结构。[D08] [D11] [D18] [D19]

### 10.2 不要只测字数，要测任务是否更容易完成

按你现有 Wiki 评测偏好，约 100 篇单库是优先评估场景，而不是本轮观察到的生产库规模。长文、大任务和多库可以用于增长诊断；这与“现在实际就是 100 篇”不同。[M1]

如果将来实施本报告的 prompt/memory 调整，建议选少量有明确预期的真实任务：

| 场景 | 应观察的行为 |
| --- | --- |
| 普通开发修复 | 读对工程原则和相关合同，不通读全部 memory；完成真实消费者和必要验证 |
| 架构 review | 区分 bug 与简化，不因“单 owner”删除必须保留的并发控制 |
| 文档-only 修改 | 保留语义，更新正确 owner 和入链，遵守既有操作范围 |
| Wiki 查询 | 选择合适指南、读足证据，不把来源偏好变成每问必全库/全网搜索 |
| Wiki 维护 | 使用当前 assignment 和输出证据，避免重复导出、验收和无效提交 |
| 修复后的复审 | 复用有效证据，同时补跑因修复而失效的检查 |
| 搬机或换 backend | 核验路径、加载和行为条件，不把旧 memory 当实时保证 |

记录整个任务的输入/输出 tokens、工具往返、耗时、错误与返工；未知 token 不按零处理。保持问题与验收条件可比，并说明是否包含网络、模型和等待时间。组件微基准可以解释原因，但不能独立证明完整任务更快。[D16]

**本轮没有执行这组行为评测，因此没有宣称具体 token、时延或准确率收益。** 现阶段可以确认的是结构改善、引用状态和规则文本的差异。

<a id="s11"></a>
## 11. 建议采用顺序与完成条件

以下按 2026-10-28 本机复核同步状态；未注明完成的采用建议不属于此次报告更新的实施范围。

| 项目 | 当前状态 | 后续动作与完成条件 |
| --- | --- | --- |
| 指针准确性 | M1、M2 在本机已解决 | 保留现有指针，继续由目标 owner 维护；不重复修复已存在的入口 |
| 建立共同规则 | M3 部分改善，八条正文在 Octo memory | 若采用共同规则方案，正文归根 AGENTS，memory 留偏好与入口；保留根开头、Scope、项目地图和操作边界 |
| 对齐角色提示 | 本机快照已修订；保存配置与运行时未核对 | 实际采用时核对当前保存配置与后续会话，不覆盖用户其他设置 |
| 文档 owner | `docs/AGENTS.md` 已建立并入链 | 沿用根入口、索引、详细规则的现有分工，不再另建同义正文 |
| 小范围整理 memory | M4 仍在，M5、M6 尚有缺口 | 只移除已完整移交的实现细节，补合同入口、历史任务身份与可确认来源；保留用户偏好、角色经验和保护条件 |
| 按实际频率决定 skill | 此次未评估采用结果 | 有重复需求时再选择第 10 节的流程；触发明确、引用可达、减少重复步骤 |
| prompt 效果 | 此次未运行行为或成本实验 | 实际修改后用代表任务验证质量与规则遵循，再讨论 tokens/耗时是否改善 |

此次完成的是现状核对与报告同步。后续采用剩余建议时，按该项的真实范围完整交付，满足相应完成条件后停止，不必同时重写整个仓库或搬迁所有 memory。

<a id="appendix-a"></a>
<a id="附录-a28-个-memory-文件的处置意见"></a>
## 附录 A：memory 文件的处置意见

本表原 28 项依据附件逐文件阅读；此次只同步与六类问题有关的处置意见，并加入本机新增的 `engineering-principles.md`。复核时相同角色目录范围内共有 29 个 Markdown 文件；第 1.1 节的字节、字符和链接统计仍是原附件数据。缩写：Octo 为 `98905a679ddf/memory/`，Vera 为 `f97a9e992aee/memory/`，Weber 为 `f37612d667ec/memory/`。`702bdc5c646b` 的角色名称未在附件中明确，不推断它是 Default。[M1]

| 文件 | 建议 | 具体处理 |
| --- | --- | --- |
| `702bdc5c646b/memory/MEMORY.md` | 保留 | 当前 9 行入口足够；有实际角色需要时再扩充 |
| `Octo/MEMORY.md` | 保留结构 | 保持任务分组；跟随正文更新入口，不恢复跨角色私人链接 |
| `Octo/agent-configuration.md` | 保留 | 用户保存配置的保护偏好与仓库指针准确；不用继续压缩 |
| `Octo/architecture-decisions.md` | 保留；可选合并 | 若经常与 behavior 同读，可合并成合同导航；保留设计不授权实施等限定 |
| `Octo/behavior-boundaries.md` | 保留；可选合并 | 已是触发式入口，不扩写第二份完成协议 |
| `Octo/context-maintenance.md` | 保留；M1 本机已解决 | `docs/AGENTS.md` 已存在并入链，无需退回旧入口；保留完整开头、按需读与官方 skill 原样的偏好 |
| `Octo/data-evolution.md` | 保留 | 内容/效果保全和旧表示退出有价值；具体程序仍归仓库 |
| `Octo/engineering-principles.md`（本机新增） | 统一共同归属 | 已有八条全文及 Octo 索引入口；若采用项目共同规则方案，先移交根 AGENTS，再保留偏好与入口，来源一并注明 |
| `Octo/migration-scope.md` | 补直接合同入口与历史身份 | 仓库已解释“19–22”范围；memory 应直接指向对应小节，不升级成永久测试删除指令 |
| `Octo/octopus.md` | 保留主入口 | 保留后端方向、日志和交付偏好；不追加通用原则全文 |
| `Octo/sapphire.md` | 保留 | 保留原始导入、已有界面和恢复保护；旧备份观察继续保留时间条件 |
| `Octo/smartreader.md` | 定点去重 | 应用 README 已说明缓存、字体和检查脚本；只移除已完整承接的重复细节，保留阅读、字体和搬迁保护需求 |
| `Octo/sudo-scope.md` | 保留 | 很短且范围明确；不解释成所有动作的永久授权 |
| `Octo/wiki-evaluation.md` | 保留；M2 本机已解决 | 同名章节已承接评测口径，无需改指针；保留约 100 篇、完整成本与先修接口的偏好 |
| `Octo/wiki-test-tiers.md` | 补任务身份 | 保留隔离候选、授权不跨轮自动延续；明确“那批用例”所指 |
| `Octo/workspace-lifecycle.md` | 保留私有保护，定点去重 | 私有发布与工具 README 已存在；逐项核对细节承接后再移除重复正文，不删除唯一记录或将私有工具原样入库 |
| `Weber/MEMORY.md` | 保留 | 维持研究与 Wiki 入口，避免普通讨论加载开发运维流程 |
| `Weber/research-workflow-preferences.md` | 保留主体 | 研究领域与 PDF 材料需求是真实个人偏好，不因 Wiki Markdown 范围而删掉 |
| `Vera/MEMORY.md` | 保留 | 共同准则提升后，区分项目入口与私有审查方法 |
| `Vera/architecture-simplicity.md` | 保留角色方法，统一共同部分 | Octo 已另有八条全文，但根规则未统一；完成共同正文交接后，保留必要性审查、证伪方法与协作判断 |
| `Vera/codex-runtime-preference.md` | 保留偏好 | 传输迁移范围、实机覆盖和原文证据的区别有价值；版本现场核验 |
| `Vera/log-review-read-only.md` | 保留主体 | 对日志调查中“继续”的解释有明确语境，不泛化为所有请求都再确认 |
| `Vera/runtime-audit-invariants.md` | 保留导航 | 当前已指向仓库合同；检查章节仍准确，不重新复制细则 |
| `shared-memory/MEMORY.md` | 保留 | 共同职责加 5 个入口清楚；仅为真实共同需求增项 |
| `shared-memory/locations.md` | 保留唯一位置入口 | 搬机核验公共 checkout；不集中全部私人应用路径 |
| `shared-memory/verification.md` | 保留 | 有效证据、失效条件和协作分工已合理，不再列完整测试命令/数量 |
| `shared-memory/wiki-maintenance-batching.md` | 保留 | 两份重复偏好已合并；用户偏好与当前工具实际约束分别核对 |
| `shared-memory/wiki-operations.md` | 保留，少量去重即可 | 保留内容保护；实时权限提醒有价值，不追求零复述 |
| `shared-memory/wiki-source-combination.md` | 保留 | 保持 /wiki 与 auto 的来源义务区别，不缩成每问必联网 |

**迁移提醒：**若共享目录位于同账户 `agents/` 树内，现有账户导出会递归收集该账户树中的文件；不能因为没有专门的 shared-memory API 就断言它会漏掉。Markdown 正文中的绝对路径不因此自动改写，搬机后仍要核对位置。[O15]

<a id="appendix-b"></a>
## 附录 B：证据、版本与核验范围

<a id="b1-本轮材料"></a>
### B.1 原审阅材料（2026-10-27）

| 对象 | 基线 |
| --- | --- |
| 本轮 memory | `agents(1).zip`；SHA-256：`08658d5c5b798bedfa89474c3ae182aa3a41bdc01e0619fee46516bdb3278fdf` |
| 前轮比较材料 | `agents.zip`；仅用于比较结构变化，旧问题已重新核对 |
| 通用原则 | `Engineer.txt`；8 条原则，约 512 个英文词 |
| Octopus | `c2bdeb1364049d9affd82cd5e70f786a22726aee`；本轮 fetch 后仍为此提交 |
| DeepSeek Harness | `22ddee3792402c3e70ec5f86a63c3e3e895a2f60`；本轮 fetch 后仍为此提交 |

本轮做了附件逐文件阅读、前后统计、相对链接与仓库指针核对、相关源码及文档交叉检查。DeepSeek 的资料按与用户问题的关联选择深读，不宣称穷尽了整个仓库的所有文档和历史。

本轮没有重新做完整 code review，没有运行真实 Codex/Claude、生产操作、模型成本实验或全量测试。上一轮的代码复现和测试结果不在本轮冒充新的行为验证。公开仓库、附件和你的本机未推送内容可能存在差异；报告已对相关位置作出限定。

本文中的路径建议、英文改写、工作流和优先级属于本次分析；引用链接支持被审阅来源的事实，不表示所有建议都是 DeepSeek 官方规则。

### B.2 引用说明

正文的 M/E 引用对应你提供的文件，O 引用对应 Octopus，D 引用对应 DeepSeek Harness。GitHub 链接固定到原审阅提交，不是此次本机状态的证据；本机复核使用仓库相对链接与下节的文件范围。若将规则正式写入日常维护文档，应使用相应仓库相对链接；有日期的 review 报告仍保留原版本证据。

原始附件：[前轮 memory][M0]、[本轮 memory][M1]、[Engineering principles 原稿][E1]。项目入口：[Octopus 根规则][O1]、[Octopus 文档索引][O2]、[DeepSeek 文档标准][D17]。

### B.3 六类问题的本机复核（2026-10-28）

| 对象 | 此次核对范围 |
| --- | --- |
| 仓库基线 | `/home/jack/Octopus`；HEAD `0df8b78ec470c0186c0ef6bd758a3695f1e686d1` 加复核时未提交改动；`docs/AGENTS.md` 为未跟踪文件 |
| 文档入口与共同规则 | 根 `AGENTS.md`、`docs/AGENTS.md`、`docs/README.md`；根文件未收录或链接八条工程原则 |
| 评测与历史范围 | `docs/plans/wiki-development-summary.md` 的“评测口径与证据边界”、`architecture-contract.md` 的“账户搬迁的开发约定”“暂停范围与恢复条件”，以及 `docs/testing.md` |
| memory | 本机实际 agent 目录中，第 1.3 节点名的 Octo/Vera/共享条目及其索引；角色路径缩写见附录 A，未以旧 ZIP 代替当前文件 |
| 应用与私有 owner | 只读查看 SmartReader README、私有发布目录及工具目录 README，确认有承接位置；没有运行应用验证或发布工具，也未将私有正文复制入仓库 |
| persona | 仅查看当前 `docs/agent-prompts.md` 快照；未读取保存的 agent row，未验证会话采用情况 |

本次只修改本报告，不改 memory、根规则、persona 或业务代码。复核不包含重新 fetch、公开仓库或部署核验、原始对话来源逐句追溯、真实模型调用及成本实验。未将日期或文件存在本身作为运行时效果证明；后续文件变化时应重新核对相应结论。

[M0]: https://chatgpt.com/library?file_id=libfile_836bb7dc58bc8191bd3022ff32baee8c&amp;amp;amp;amp;account_id=personal
[M1]: https://chatgpt.com/library?file_id=libfile_ad7f77eba17c8191baac3a3dd0794f06&amp;amp;amp;amp;account_id=personal
[E1]: https://chatgpt.com/library?file_id=libfile_e93094929bd48191ba50f1ae215f50e5&amp;amp;amp;amp;account_id=personal
[O1]: https://github.com/Danuvius152/Octopus/blob/c2bdeb1364049d9affd82cd5e70f786a22726aee/AGENTS.md#L1-L188
[O2]: https://github.com/Danuvius152/Octopus/blob/c2bdeb1364049d9affd82cd5e70f786a22726aee/docs/README.md#L1-L75
[O3]: https://github.com/Danuvius152/Octopus/blob/c2bdeb1364049d9affd82cd5e70f786a22726aee/server/harness/assembly.py#L267-L328
[O4]: https://github.com/Danuvius152/Octopus/blob/c2bdeb1364049d9affd82cd5e70f786a22726aee/server/agent_memory.py#L28-L61
[O5]: https://github.com/Danuvius152/Octopus/blob/c2bdeb1364049d9affd82cd5e70f786a22726aee/server/harness/codex.py#L148-L160
[O6]: https://github.com/Danuvius152/Octopus/blob/c2bdeb1364049d9affd82cd5e70f786a22726aee/server/harness/codex_control.py#L155-L175
[O7]: https://github.com/Danuvius152/Octopus/blob/c2bdeb1364049d9affd82cd5e70f786a22726aee/docs/plans/architecture-contract.md
[O8]: https://github.com/Danuvius152/Octopus/blob/c2bdeb1364049d9affd82cd5e70f786a22726aee/docs/plans/wiki-development-summary.md
[O9]: https://github.com/Danuvius152/Octopus/blob/c2bdeb1364049d9affd82cd5e70f786a22726aee/docs/agent-prompts.md#L1-L169
[O10]: https://github.com/Danuvius152/Octopus/blob/c2bdeb1364049d9affd82cd5e70f786a22726aee/server/wiki/workflow.py#L1-L158
[O11]: https://github.com/Danuvius152/Octopus/blob/c2bdeb1364049d9affd82cd5e70f786a22726aee/docs/cli-system-prompt-notes.md#L1-L67
[O12]: https://github.com/Danuvius152/Octopus/blob/c2bdeb1364049d9affd82cd5e70f786a22726aee/docs/plans/memory.md#L84-L129
[O13]: https://github.com/Danuvius152/Octopus/blob/c2bdeb1364049d9affd82cd5e70f786a22726aee/server/harness/claude_code.py#L50-L104
[O14]: https://github.com/Danuvius152/Octopus/blob/c2bdeb1364049d9affd82cd5e70f786a22726aee/server/harness/codex.py#L51-L127
[O15]: https://github.com/Danuvius152/Octopus/blob/c2bdeb1364049d9affd82cd5e70f786a22726aee/server/portability.py#L171-L213
[O16]: https://github.com/Danuvius152/Octopus/blob/c2bdeb1364049d9affd82cd5e70f786a22726aee/docs/plans/deepseek-harness-backend.md#L38-L66
[O17]: https://github.com/Danuvius152/Octopus/blob/c2bdeb1364049d9affd82cd5e70f786a22726aee/server/harness/claude_code.py#L890-L917
[D01]: https://github.com/deepseek-ai/deepseek-harness/blob/22ddee3792402c3e70ec5f86a63c3e3e895a2f60/AGENTS.md#L129-L167
[D02]: https://github.com/deepseek-ai/deepseek-harness/blob/22ddee3792402c3e70ec5f86a63c3e3e895a2f60/docs/defensive-patterns/README.md#L5-L25
[D03]: https://github.com/deepseek-ai/deepseek-harness/blob/22ddee3792402c3e70ec5f86a63c3e3e895a2f60/docs/defensive-patterns/general.md#L7-L224
[D04]: https://github.com/deepseek-ai/deepseek-harness/blob/22ddee3792402c3e70ec5f86a63c3e3e895a2f60/docs/defensive-patterns/remote-and-api.md#L7-L117
[D05]: https://github.com/deepseek-ai/deepseek-harness/blob/22ddee3792402c3e70ec5f86a63c3e3e895a2f60/docs/defensive-patterns/client-components.md#L7-L174
[D06]: https://github.com/deepseek-ai/deepseek-harness/blob/22ddee3792402c3e70ec5f86a63c3e3e895a2f60/docs/defensive-patterns/conversation-and-session.md#L7-L135
[D07]: https://github.com/deepseek-ai/deepseek-harness/blob/22ddee3792402c3e70ec5f86a63c3e3e895a2f60/.agents/skills/dsh-error-handling/SKILL.md#L10-L26
[D08]: https://github.com/deepseek-ai/deepseek-harness/blob/22ddee3792402c3e70ec5f86a63c3e3e895a2f60/.agents/skills/dsh-find-simplifications/SKILL.md#L18-L65
[D09]: https://github.com/deepseek-ai/deepseek-harness/blob/22ddee3792402c3e70ec5f86a63c3e3e895a2f60/.agents/skills/dsh-find-simplifications/references/historical-patterns.md#L5-L45
[D10]: https://github.com/deepseek-ai/deepseek-harness/blob/22ddee3792402c3e70ec5f86a63c3e3e895a2f60/.agents/notes/implemented/process/2026-07-26-dependencies-over-hand-rolling.md#L11-L30
[D11]: https://github.com/deepseek-ai/deepseek-harness/blob/22ddee3792402c3e70ec5f86a63c3e3e895a2f60/.agents/skills/dsh-code-review/SKILL.md#L8-L62
[D12]: https://github.com/deepseek-ai/deepseek-harness/blob/22ddee3792402c3e70ec5f86a63c3e3e895a2f60/.agents/skills/dsh-ci-test-reliability/SKILL.md#L18-L131
[D13]: https://github.com/deepseek-ai/deepseek-harness/blob/22ddee3792402c3e70ec5f86a63c3e3e895a2f60/.agents/skills/dsh-ci-test-reliability/references/ci-flake-diagnosis.md#L5-L60
[D14]: https://github.com/deepseek-ai/deepseek-harness/blob/22ddee3792402c3e70ec5f86a63c3e3e895a2f60/docs/testing.md#L9-L55
[D15]: https://github.com/deepseek-ai/deepseek-harness/blob/22ddee3792402c3e70ec5f86a63c3e3e895a2f60/.agents/skills/dsh-pre-push-checks/SKILL.md#L25-L43
[D16]: https://github.com/deepseek-ai/deepseek-harness/blob/22ddee3792402c3e70ec5f86a63c3e3e895a2f60/.agents/skills/dsh-speed-up-perf/SKILL.md#L14-L92
[D17]: https://github.com/deepseek-ai/deepseek-harness/blob/22ddee3792402c3e70ec5f86a63c3e3e895a2f60/docs/AGENTS.md#L5-L77
[D18]: https://github.com/deepseek-ai/deepseek-harness/blob/22ddee3792402c3e70ec5f86a63c3e3e895a2f60/.agents/skills/dsh-doc/SKILL.md#L26-L115
[D19]: https://github.com/deepseek-ai/deepseek-harness/blob/22ddee3792402c3e70ec5f86a63c3e3e895a2f60/.agents/skills/dsh-prose-standard/SKILL.md#L28-L58
[D20]: https://github.com/deepseek-ai/deepseek-harness/blob/22ddee3792402c3e70ec5f86a63c3e3e895a2f60/.agents/skills/dsh-trim-cot-leakage/SKILL.md#L8-L45
[D21]: https://github.com/deepseek-ai/deepseek-harness/blob/22ddee3792402c3e70ec5f86a63c3e3e895a2f60/.agents/notes/README.md#L44-L125
[D22]: https://github.com/deepseek-ai/deepseek-harness/blob/22ddee3792402c3e70ec5f86a63c3e3e895a2f60/.agents/skills/dsh-archive-agent-notes/SKILL.md#L14-L29
[D23]: https://github.com/deepseek-ai/deepseek-harness/blob/22ddee3792402c3e70ec5f86a63c3e3e895a2f60/docs/postmortem/README.md#L5-L11
[D24]: https://github.com/deepseek-ai/deepseek-harness/blob/22ddee3792402c3e70ec5f86a63c3e3e895a2f60/packages/preset/agent-preset/skills/agent-experience/SKILL.md#L6-L19
[D25]: https://github.com/deepseek-ai/deepseek-harness/blob/22ddee3792402c3e70ec5f86a63c3e3e895a2f60/.agents/skills/dsh-session-projection/SKILL.md#L12-L30
[D26]: https://github.com/deepseek-ai/deepseek-harness/blob/22ddee3792402c3e70ec5f86a63c3e3e895a2f60/packages/context/agent-instructions/README.md#L12-L42
[D27]: https://github.com/deepseek-ai/deepseek-harness/blob/22ddee3792402c3e70ec5f86a63c3e3e895a2f60/packages/skill/tool-skill/README.md#L110-L207
