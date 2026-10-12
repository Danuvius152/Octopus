# Pika 2.1.5 Skills：设计分析与计算机 / AI 学习用法

分析对象：`/mnt/c/Users/User/Desktop/pika_math_learning_toolkit-2.1.5`。
已阅读六份 `SKILL.md`、安装说明，以及检索、编辑、计算、研究记录等关键参考
和部分脚本实现。以下按计算机 / AI 学习、论文阅读和知识库管理来选择用途。

## 1. 对你最有用的部分

优先使用 **`obsidian-vault-notes` 和 `manage-personal-knowledge`**：前者管理
Markdown 笔记，后者协调笔记、PDF 资料和外部资源。数学题库、数学训练、
符号计算和数学研究归档可以先跳过。

| Skill | 实际职责 | 对你的用法 |
| --- | --- | --- |
| `obsidian-vault-notes` | 查找、阅读、修改 Obsidian 笔记，维护链接，检查改动和版本 | 经常使用：AI 概念笔记、课程笔记、论文阅读记录、项目总结 |
| `manage-personal-knowledge` | 知识库总入口；协调 Vault、PDF 库、结构化题库、稳定资源 ID | 经常使用：跨笔记与论文检索、查出处、管理长期资料 |
| `pdf-problem-search` | 定位数学定理或题目所在的 PDF 页，并核验具体陈述 | 低优先级：有明确数学陈述需要找原文时才用；普通 AI 论文查询走上一项的通用 PDF 检索 |
| `math-science-computation` | 选择 Mathematica、SageMath、Python 等后端，执行计算并记录验证依据 | 特定数值 / 数学计算时可用；其可复现与证据分级设计值得借鉴 |
| `math-coach` | 检查学习者提出的数学步骤，默认 GUIDED，也支持 STRICT；附带独立本地 Harness | 可以跳过日常使用；学习模式与状态机设计可作为工程案例 |
| `math-research-solve` | 保存数学研究记录、失败路线、证据、审查、修订与交接包 | 可以跳过日常使用；研究历史与证据管理设计可迁移到 AI 实验记录 |

最后两项明确围绕数学设计。`math-coach` 还会拒绝非数学任务，因此不能直接
当作编程老师；`math-research-solve` 也不能仅换一个名字就视为通用 AI 实验平台。

## 2. Skill、Agent 和工具怎样分工

| 层 | 在你的项目里对应什么 | 负责什么 |
| --- | --- | --- |
| Agent | Octo、Vera、Weber | 持续职责、会话、记忆和任务决策 |
| Skill | 本工具包的六个工作流目录 | 某一类任务的做法、边界、失败处理和交付标准 |
| 工具 | Codex 文件 / 命令工具、Octopus MCP、Skill 附带的 Python CLI | 实际检索、计算、文件修改与状态保存 |

六个 Skill 可以被同一个运行中的 Agent 按需使用。它们之间的“加载另一个
Skill”是工作流切换；Octopus 的 `ask_agent` 才是向另一个持久智能体委托。
安装该工具包不会自动创建六个 Agent，也不会自动安排四人流水线。

适合 Weber 的一条流程：

```mermaid
flowchart LR
    U[学习或讨论任务] --> W[Weber / Codex]
    W --> M[manage-personal-knowledge 路由]
    M --> N[obsidian-vault-notes 查笔记]
    M --> P[通用 PDF 检索]
    N --> E[当前文件和来源证据]
    P --> E
    E --> A[回答并说明依据]
    A --> S[用户要求保存时：指定笔记、受保护编辑、校验]
```

## 3. 它们怎样设计

### 3.1 用元数据决定何时触发

每个 Skill 的开头有 `name`、`description` 和自身版本。`description` 写了
任务范围与触发场景。例如 Obsidian Skill 描述的是查找、阅读、修订、版本化
笔记，PDF Skill 描述的是定位数学原文页。

这让模型先选择适合的工作流，再读取详细规则。你自己设计 Skill 时，首先
应写清“什么时候用、输入是什么、交付什么”，避免仅写一个宽泛角色名。

### 3.2 按需加载说明、参考资料和脚本

典型目录结构：

```text
skill-name/
  SKILL.md          触发条件、流程、不变量与交付规则
  references/       按具体操作加载的协议、边界和细节
  scripts/          可重复执行的检索、写入、校验和状态管理
  agents/openai.yaml  显示名称、简短介绍和默认调用提示
  assets/           部分 Skill 的页面、模板等资源
```

例如只有修改笔记时才加载编辑协议，只有 OCR 时才检查 OCR 工具，普通检索
不需要启动全部计算后端。这样的加载方式减少无关上下文和无关工具调用。

按需加载元数据和完整指令也是 OpenAI 文档描述的 Skill 使用方式，见
[Build skills](https://learn.chatgpt.com/docs/build-skills)。

### 3.3 模型负责判断，程序负责可重复的操作

模型判断问题意图、相关资料、修改内容和证据是否足够；Python 程序负责
检索、路径检查、索引更新、文件替换和结构校验。

通用 PDF 检索使用本地 SQLite FTS5，结合关键词、别名和路径 / 标题权重；
笔记召回也包含标题、标签、章节和链接邻域等可解释信号。它没有采用向量
embedding 检索，工具包还明确禁止默认将资料库发送到远端。

这对 AI 学习的启发是：可以先用可解释的本地检索解决资料管理需求。若要做
语义检索实验，需要另外设计和评估，不能把当前包宣传成已经具有该能力。

### 3.4 索引用于找资料，当前源文件用于确认事实

Obsidian 索引先定位候选，实际阅读指定笔记时打开当前 Markdown 文件。
PDF 返回文件、页码、摘录与提取状态，再检查具体页面中的陈述。

“排名高”“命中关键词”“索引没找到”分别不能证明“来源准确”“支持结论”
和“整个资料库不存在”。工具返回覆盖范围、截断信息和提取错误，供模型
判断结论能说到什么程度。

用在论文阅读上，应分开记录论文原文的结论、自己的理解和实际实验结果。

### 3.5 限制每次读取的范围和输出

Obsidian 默认查询只返回候选元数据；小范围内容读取通常最多三篇笔记、
每篇三个选定章节、合计 12,000 个正文字符。明确指定笔记时有另一套读取模式。
各个 CLI 还有完整 JSON 响应大小限制，并返回读取审计和截断状态。

这些限制帮助减少上下文噪声，也让失败和覆盖不足可见。但它们主要约束
单次脚本返回：跨调用累计范围仍需 Agent 维护，任意 shell 读取也不在同一
约束内。设计和评估时应区分脚本保证与模型应遵守的规则。

### 3.6 写入使用预览、校验值和事务

笔记编辑先读取当前内容，预览改动，再使用预览时的 SHA-256 绑定正式写入。
文件在此期间变化时应停止，避免覆盖你在 Obsidian 中刚做的修改。编辑器
保留无关内容、换行、frontmatter、链接和已有未提交修改；批量编辑有预检查
与回滚机制。

写入后检查链接、公式 Markdown、实际 diff，最后返回 `EditAudit`。对外部
长期文件，它还设计了稳定的 `KB-...` ID，将文件身份与可能变化的路径分开。

这些设计可用于代码变更、实验配置修改和研究数据管理。但一次格式校验
通过不能证明笔记中的技术结论正确，语义审查仍有自己的职责。

### 3.7 用状态和证据区别“执行成功”与“结论成立”

计算 Skill 分 `chat`、`reproducible`、`high-assurance` 等执行档次，要求
说明参数、环境、实际结果与验证强度。有限测试或数值一致不能升级成一般证明。

研究 Skill 保存失败尝试、修订、纠正关系、证据与审查状态，区分目前可以
依赖的记录和应保留的历史记录。实验失败也可以有长期价值，关键是留下
触发条件、原因和最小复现材料。

对你的 AI 实验，可以借鉴这些字段：问题 / 假设、代码提交、数据版本、
模型与参数、随机种子、指标、实际输出、失败原因、适用范围和审查结论。
这是迁移设计的建议，不是原包已经实现的通用 AI 实验功能。

### 3.8 保持安装包与个人运行数据分离

Skill 源码、个人路径配置、索引和运行记录分开存放。安装器校验包内容，
以整套技能为单位安装、替换和回滚；安装不自动给真实知识库建索引。
多个 Skill 从同级安装目录定位依赖，而不是固定在原作者的绝对路径。

套件还区分安装诊断、功能测试、可选工具可用性和检索评估，避免一个层面的
成功被当成其他层面的保证。Observer 只记录有限运行阶段，失败时不阻断任务。

## 4. 借鉴时需要看清的取舍

- **任务范围带有数学偏好。** 通用 PDF 检索适合 AI 论文；定理定位器的页面
  分类和结构锚点围绕数学题目设计，普通算法或系统论文无需强行走这条路线。
- **写作风格档案属于原作者。** `author-voice-profile.md` 来自其授权分析的
  85 篇笔记。使用时应明确以你的当前笔记和表达偏好为准，不能称为“你的风格”。
- **入口说明很长。** 六份 `SKILL.md` 合计约 14.3 万字符。虽然参考资料按需
  加载，入口本身仍有明显上下文成本；你设计自己的 Skill 时，可以保留清晰
  的入口，把具体协议交给参考文件，把机械检查交给脚本。
- **一些接受规则是作者的选择。** 大型笔记编辑允许三轮 review 全部 FAIL 后
  以 `accepted_after_three_reviews` 进入写入流程，并保留未解决问题。这个状态
  不能称为 review 通过，也不应直接成为你的代码发布标准。
- **运行环境需要按本机判断。** 包有 Linux / WSL 适配，Windows + DSH 的
  特定流程只在对应可信运行环境下启用。原作者的 Mathematica、Sage、OCR、
  浏览器工具和其他专用 Skill 并没有全部随包提供。
- **prompt 约束与程序保证要分开。** `math-coach` 的普通聊天模式依赖模型
  遵守指令；其独立 Harness 才通过闭合字段、状态机和确定性渲染器限制输出。
  这部分适合研究 Agent Harness 设计，日常数学训练用途可以跳过。

## 5. 在你当前 WSL / Octopus 中怎样使用

### 5.1 安装前先确定知识库位置

在本机常见用户级和项目级 Skill 目录中，尚未发现这六个 Skill 的安装。
桌面上的解压目录是安装包；它需要进入 Codex 的发现范围才能作为原生技能
被列出。直接让 Agent 读取指定的 `SKILL.md` 可以查看流程，但不代表依赖、
个人配置和索引已经就绪。

使用整套知识库配置时，需要一个根目录，下面有相互独立、互不嵌套的
Obsidian Vault 和 PDF 资料目录。例如：

```text
/mnt/d/AI-Knowledge/          示例根目录，请换成你的真实目录
  Vault/                    已有 Obsidian vault，包含 .obsidian
  Papers/                   AI 论文、教材等 PDF
```

这套安装器要求同时配置 Vault 和 PDF 资料目录。若目前只有一个 vault，
需要先明确同一根目录下的独立 PDF 目录，再采用整套安装流程；日常仍可
只调用笔记管理功能。不要猜测或自动搬动已有资料。

### 5.2 在 WSL 安装到明确的发现目录

当前 OpenAI 文档列出的用户级目录是 `$HOME/.agents/skills`，项目级目录是
`.agents/skills`。安装器默认使用 `CODEX_HOME/skills`（通常为 `~/.codex/skills`），
可以用 `--target-root` 明确选择前者。参见
[OpenAI 官方 Skill 加载位置](https://learn.chatgpt.com/docs/build-skills#where-codex-loads-local-skills)。

下面命令供手动执行，本次阅读没有执行安装或建立索引。
使用你已有的 Python 虚拟环境，在 Ubuntu / WSL 运行：

```bash
cd /mnt/c/Users/User/Desktop/pika_math_learning_toolkit-2.1.5
/home/jack/Octopus/.venv/bin/python -B install.py \
  --dry-run --agent codex --target-root "$HOME/.agents/skills"
```

确认安装目标后，交互式安装会询问真实知识库根目录以及 Vault / PDF 子目录：

```bash
/home/jack/Octopus/.venv/bin/python -B install.py \
  --agent codex --target-root "$HOME/.agents/skills"
```

检查安装：

```bash
/home/jack/Octopus/.venv/bin/python -B install.py \
  --doctor --agent codex --target-root "$HOME/.agents/skills"
```

安装器目前安装整套六个 Skill；你可以按任务只调用其中两个。已有同名安装
时会保护原目录，不要直接加 `--replace` 覆盖。个人路径配置和索引写入 WSL
的用户配置 / 状态目录；需要索引时再明确指定资料范围和建立哪些索引。

同一个 WSL 用户下的其他 Codex Agent 也可能发现用户级 Skill，这不是仅对
Weber 可见的权限隔离。不要为了安装 Skill 更改 Codex 的登录目录。

### 5.3 在 Octopus 新建 Weber 会话

安装后新建 Weber 会话，并确认其能发现对应 Skill。需要处理 vault 时，使用
实际 WSL 路径；创建 Octopus 账户后，外部工作目录还需要列入 Account 的
允许目录，参见 [项目使用说明](user-guide.md)。

在消息中写 `$obsidian-vault-notes` 或 `$manage-personal-knowledge` 加上任务。
`$skill-name` 是技能调用提示，不是需要在 shell 中执行的命令。
原生 Codex CLI / IDE 提供 `/skills` 选择入口；Octopus 当前的聊天斜杠菜单
没有该入口，直接在消息中指定技能名称即可。

这个包在遇到图表、排版等问题时会引用另一个 `$pdf` Skill，检索评估也会
引用其他专业 Skill。它们不在本包的六项之中；仅安装本包不代表这些外部
依赖全部可用。普通文字层 PDF 检索可以先走附带的通用 CLI。

## 6. 适合你的可复制任务

### 查找已有知识，避免重复整理

```text
$obsidian-vault-notes
在我的 Obsidian vault 中查找与 agent memory、RAG 和会话上下文有关的笔记。
优先在 AI 与系统设计相关目录查找，最多阅读三篇最相关笔记的必要章节。
结合实际笔记，说明这三个概念分别解决什么问题，以及我之前有哪些误解。
给出来源笔记和章节。本次只阅读，不修改。
```

### 在本地 AI 论文中找原文

```text
$manage-personal-knowledge
只搜索我的 PDF 资料库，查找 KV cache 如何影响推理显存占用的原文。
使用少量准确的中英文别名，返回论文 / 文件、PDF 页码和支持结论的摘录。
如果索引覆盖不完整或提取失败，明确说明。
区分论文的直接结论与根据原文作出的推断。本次不写笔记。
```

### 把讨论保存为自己的笔记

```text
$obsidian-vault-notes
把我们刚才达成共识的 KV cache 内容写入 AI/LLM/KV Cache.md。
若已有这篇笔记，先阅读当前文件并保留其 frontmatter、已有链接和无关内容。
使用本轮已核查的资料，分清原文结论、我的理解和待验证问题。
写作风格以我的当前笔记与表达偏好为准，不套用包中的原作者风格档案。
完成受保护写入和链接检查，给出简短改动说明，不提交 Git。
```

### 讨论 idea，再决定是否记录

```text
Weber，先和我讨论这个想法：为个人知识库做一个混合检索系统。
使用 $manage-personal-knowledge 查找我的笔记和 PDF 中已有的相关材料。
围绕我的使用场景分析 BM25、向量召回和 reranker 的收益、成本与验证方法。
先形成具体实验方案，区分已有证据和待测假设；这轮先不写入知识库。
```

这是一项结合资料的设计讨论，不意味着原包已经带有向量检索或 reranker。

## 7. Weber 的 System prompt 可附加什么

不必把六份 `SKILL.md` 全部复制进 System prompt。保留简短的职责与路由规则，
让具体工作流按任务加载。可在你已有的 Weber prompt 后附加：

```text
For Obsidian or Markdown note retrieval and editing, load obsidian-vault-notes.
For coordinated retrieval across notes, local PDFs, and managed resources, load manage-personal-knowledge and honor the user's selected source scope.
Use only the configured knowledge root and explicitly supplied paths. Preserve live Markdown as the source of truth.
Keep source statements, interpretations, and experimental observations distinct. State coverage gaps and verification limits.
When asked to save a result, write to the identified note through the skill's guarded workflow and validate the final artifact.
Base prose style on this user's preferences and the current note. Do not treat the toolkit's bundled author profile as this user's personal voice.
```

Octo 继续负责实现和实验代码，Vera 负责有依据的审查，Weber 负责讨论和知识库。
需要跨智能体配合时，沿用 Octopus 的协作工具，把目标、路径、证据和具体问题
交给另一位 Agent。Skill 和智能体的职责不需要一一对应。

## 8. 原始资料入口

- [安装说明](/mnt/c/Users/User/Desktop/pika_math_learning_toolkit-2.1.5/AGENT_INSTALL.md)
- [套件清单及依赖图](/mnt/c/Users/User/Desktop/pika_math_learning_toolkit-2.1.5/package-manifest.json)
- [知识库总入口](/mnt/c/Users/User/Desktop/pika_math_learning_toolkit-2.1.5/payload/manage-personal-knowledge/SKILL.md)
- [Obsidian 笔记流程](/mnt/c/Users/User/Desktop/pika_math_learning_toolkit-2.1.5/payload/obsidian-vault-notes/SKILL.md)
- [数学 PDF 定位](/mnt/c/Users/User/Desktop/pika_math_learning_toolkit-2.1.5/payload/pdf-problem-search/SKILL.md)
- [计算与验证](/mnt/c/Users/User/Desktop/pika_math_learning_toolkit-2.1.5/payload/math-science-computation/SKILL.md)
- [数学辅导状态机](/mnt/c/Users/User/Desktop/pika_math_learning_toolkit-2.1.5/payload/math-coach/SKILL.md)
- [研究记录与交接](/mnt/c/Users/User/Desktop/pika_math_learning_toolkit-2.1.5/payload/math-research-solve/SKILL.md)
- [笔记读取协议](/mnt/c/Users/User/Desktop/pika_math_learning_toolkit-2.1.5/payload/obsidian-vault-notes/references/retrieval-protocol.md)
- [笔记编辑协议](/mnt/c/Users/User/Desktop/pika_math_learning_toolkit-2.1.5/payload/obsidian-vault-notes/references/editing-protocol.md)
- [原作者写作风格档案](/mnt/c/Users/User/Desktop/pika_math_learning_toolkit-2.1.5/payload/obsidian-vault-notes/references/author-voice-profile.md)
- [大型编辑 review 接受规则](/mnt/c/Users/User/Desktop/pika_math_learning_toolkit-2.1.5/payload/obsidian-vault-notes/references/large-edit-verification.md)
