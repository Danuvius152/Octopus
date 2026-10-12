# Octopus 本地共享 Wiki：SmartReader 阅读、讨论与按需入库 v2

> **Status:** proposed — 后续应用规划；本轮交付设计，尚未实现或部署。\
> **版本：** 2.1；修订 2026-10-13（Asia/Shanghai）；文件名保留 v2 兼容已有引用。\
> **审阅基线：** Octopus `453a7995bd6c54df33b0fc41dec09d370b4b40aa`；本轮核对远端 main 仍为该提交；用户提供 `.octopus.zip` 中 SmartReader 非敏感源码。\
> **依赖：** 共享身份、证据协议与任务能力；另需本文的多 app client 授权。V2-09 不阻塞 V2-01～04 核心、V2-06 少量试点或 V2-08 旧资料适配。

## 1. 产品职责

**SmartReader 是“围绕正在读的材料思考、对话、形成草稿，必要时进入知识库”的主入口。** Sapphire 负责主库浏览、标签过滤、文件编辑及版本维护；`/wiki` 负责显式查库与受控维护。三个入口调用同一套身份、检索、证据与持久任务机制，不维护三份知识库。

| 用户正在做什么 | 主入口 | 结果 |
| --- | --- | --- |
| 在本地库找文章、按标签筛选、直接编辑文件 | Sapphire | 当前文档或一次明确保存 |
| 点回答中的引用，核对论证上下文 | SmartReader | 打开正确文档、版本与段落 |
| 解释当前一段、质疑文章、延伸一个想法 | SmartReader 与 Weber 对话 | 有边界的解释和分析 |
| 写一段想法、修改个人草稿、组织研究讨论稿 | SmartReader 写作 | 对话文本或 reader 草稿 |
| 查“我是什么专业”、找 Vidu、概览数字人资料 | 当前 agent 的 `/wiki`，或 SmartReader 中明确查库 | 小量候选与当前证据 |
| 将材料或有独立价值的讨论产物整理入库 | `/wiki maintain` 的现有 durable worker | 保留来源的规范产物与真实任务回执 |

Sapphire 可以保留普通文件预览与编辑，结果页提供“在 SmartReader 阅读”即可；不复制完整 Weber 聊天、选区讨论与写作工作台。SmartReader 可以显示关联主库文件，但不复制 Sapphire 的 Git、回收站和整库管理。

## 2. 已有能力与本轮新增设计

附件源码已经有很好的阅读基础，优先保留其文档、会话与交互结构。[A1]

| 项目 | 已有事实 | 拟议补充 |
| --- | --- | --- |
| 阅读格式 | Markdown、HTML、文本、PDF、图片；网页抓取和导入 | 绑定主库文件，保留不同格式的证据与提取状态 |
| 材料存储 | localStorage 元数据、IndexedDB 正文、浏览器 UUID | 单独的 Wiki 文档引用；必要时 reader material capture |
| 对话 | 指定 agent、材料×agent 的 conversation_id、SSE、历史恢复 | 默认 Weber 作为阅读伙伴，沿用原会话能力，不新建 LLM runner |
| 选区 | DOM Range、摘录、页码、前后文、本地回跳 | 稳定引用、版本核验、跨渲染坐标映射与歧义处理 |
| 内容上下文 | 每次聊天附最多 150,000 个 JS 字符串单位正文，选区另最多 12,000 | 删除重复全文注入；改为引用、局部证据与可继续的完整阅读 |
| 保存 | 下载 HTML/原始文件、导出摘录 Markdown | 明确的 reader 草稿保存/修改与“入库”动作 |
| Wiki 接入 | SmartReader 没有 `wiki/*` 调用，也没有设置 `wiki_mode` | 先完成 client 权限，再启用能力匹配的 Wiki read/maintain |

SmartReader 的 `reader_server.py` 只提供公共网页抓取和 health；没有材料编辑、主库读写或持久化草稿 API。文件下载也不是原文件写回。以上新增能力不能仅靠改一段 system prompt 宣称完成。

## 3. 第一个依赖：保留 owner，增加受限客户端

### 3.1 为什么不能只换一个 app_id

当前 `WikiRegistry.configure` 只存一个 `app_id`；`app_wiki` 要求请求 app 与它匹配。`app_bridge.begin` 又用该字段判断哪些 app 文件写入需要进入共同历史与锁。因此把它改为 SmartReader，会改变既有 owner/provider 归属。[F1][F2]

本轮附件没有 live Wiki config；开发时必须核对真实账户、Vault、owner app 和 SmartReader app ID。不能根据名称猜绑定，也不能假设附件当前线上一定绑定 Sapphire。

### 3.2 最小共享契约：client_apps

保留现有 `app_id` 作为 owner/provider，拟增加：

```json
{
  "app_id": "<现有 owner/provider app 的真实 ID>",
  "client_apps": [
    {
      "app_id": "<SmartReader 的真实 app ID>",
      "permissions": ["read", "submit_maintenance"]
    }
  ]
}
```

这是**拟议配置**；本轮不执行配置写入或 client grant。无 `client_apps` 的旧配置维持旧行为。新增列表必须校验 app 属于同一账户，并与配置代和服务缓存失效机制一起实现。

| client permission | 允许的事情 | 不由此取得的能力 |
| --- | --- | --- |
| `read` | 获准的 search/read/read_many/documents/tags/链接或引用解析，以及当前消息的 Wiki read 任务 | 普通 `PUT document`、publish、organize、restore、config 或快照管理 |
| `submit_maintenance` | 提交明确维护请求，读取本客户端获准 job 的状态和结果；需要取消/重试时按相同 job 归属检查 | 让阅读 agent 自己获得 curator 发布能力，或改写其他 app / 其他账户的任务 |

具体端点 allowlist 必须由一个共享策略函数负责。只读内容之外的历史详情、任务管理和配置返回是否开放，要按实际 UI 所需逐项定义；不能把所有 GET 都当作当然可读。维护实际写入继续由现有 curator worker、任务 scope、ownership 和 revision 控制。

### 3.3 Wiki gateway 与 AppAgent 入口同步

当前 `AppAgentManager` 会直接 `explicit_mode(message, wiki_mode) → start_message`，该路径尚无 client policy 检查。[F3] 新策略必须同时覆盖：

1. `/apps/{id}/wiki/*` 的直接请求；
2. `/apps/{id}/agent/chat` 和 `agent/ask` 中的 `wiki_mode`；
3. 用户在 app 对话中明确输入 `/wiki` 的同一任务创建路径；
4. 客户端权限撤销后续轮次与新的工具调用。

保留 legacy 配置下已有行为，不暗中改变旧应用；启用新 client policy 后，上述入口必须服从同一规则。文章正文或 `context` 里出现 `/wiki` 不构成授权。普通阅读对话继续作为 app 对话运行，不因为材料已入库就自动读取其他库内容。

还有一段必须贯通：现有 MCP wrapper 调用普通 `/api/wiki/*`，不会再次经过 app gateway；`WikiTask` 当前没有 app provenance 字段。实现时从可信 `session.app_id` 将 client 归属绑定到 task，或在 task 检查时从服务器会话记录解析。普通 REST 的 `access/tasks.require` 与 MCP `list_tools` 均使用本次任务和 client permission 的有效交集；不能只在开始聊天时检查一次后就丢失限制。无需信任浏览器 context 里的 app ID，也不要求因此另造一套 token。[F5]

client 撤销阻止该客户端新的请求和后续受限工具调用；已经接收的持久维护按真实 job 的授权与取消语义处理，不默删任务。Weber worker 的发布能力来自已验证 job assignment，不直接继承 reader 的普通写权限。

## 4. 三种材料身份

SmartReader 的本地 UUID、reader 保存的材料、Wiki 权威文档必须可区分，避免同名或同 URL 的内容被误当成同一个当前版本。

| 状态 | 身份与位置 | agent 能读取什么 |
| --- | --- | --- |
| 浏览器材料 | `reader_id`；localStorage / IndexedDB；可记录内容 fingerprint | 本次和之前实际提供的片段；不能假定能访问浏览器全文 |
| reader capture / 草稿 | 服务器生成的材料或 draft handle、revision；属于 reader app 数据 | 显式提供且获准的 handle 内容，不是任意文件路径 |
| 主库材料 | 真实 `vault_id`、Wiki 服务 `id`、revision；浏览器另保留 reader_id | 按本次 Wiki grant 与证据 API 读取当前版本 |

拟议本地绑定形态如下，属于 app context 格式草案，不是当前顶层 AppAgentRequest 已有字段：

```json
{
  "type": "reader_context_v1",
  "reader_id": "<浏览器材料 UUID>",
  "wiki_ref": {
    "vault_id": "<已验证主库 ID>",
    "id": "<Wiki 服务现有文档 ID>",
    "revision": "<实际读取版本>"
  },
  "selection": null,
  "provided_ranges": []
}
```

调用 Wiki read 时，`doc` 参数取真实 `wiki_ref.id`。`uid` 只是可移植别名，不代替该身份或授予权限。路径用于显示和定位候选；改名后先由身份解析当前路径。浏览器文件名、大小、lastModified 的组合只能帮助检测本地缓存，不作为主库 revision。

绑定由服务端解析确认后写入 reader 状态。外部网页与主库文章标题相同，只能显示“可能已有这份材料”；不能自动关联后声称正在阅读当前主库。用户阅读已缓存的旧版本时，UI 显示快照状态，并让核查当前内容有明确入口。

## 5. 删除 150k 每轮注入，同时保留完整阅读

### 5.1 已绑定 Wiki 的材料

用户点“问这篇”或明确基于库中报告提问，本次设置真实的 `wiki_mode="read"`，附文档引用和问题；选区只附必要 quote 与定位描述。agent 使用同一 WikiService 的 read/read_many。

- “TTFF 和吞吐的关系？”：先读指标定义和相关上下文，回答后停止；不重发整篇报告。
- “完整评价论证是否成立”：从当前版本开始连续阅读到结尾，分批预算只限制一次输出，不能降低全文覆盖。
- “那这句话是否与你前面判断冲突？”：复用同一 conversation，必要时重读当前相关段，附本次增量上下文。
- 材料 revision 变了：重新定位并判断哪些已读证据失效，不能把旧 offset 套入新正文。

服务端负责 read 覆盖和 revision；应用的“已展示”不能代替模型实际“已读”。如果运行时压缩了历史，对后续需要使用的证据重新取读，不虚称模型永久记得完整原文。

### 5.2 仅在浏览器、尚未入库的材料

不能把全文删掉后只传一个 Wiki 不认识的 UUID。支持以下实际可完成的路径，按问题需要使用：

| 需求 | 可用方式 | 完成条件 |
| --- | --- | --- |
| 解释选区或附近小节 | 浏览器附选区、必要前后文和材料元数据 | 回答限定于已提供内容；不宣称读过全文 |
| 对浏览器长文作全文分析 | 前端按同一 material revision 分批提供正文与原始范围，记录已提供/实际已读覆盖 | 从开始到结尾都实际可达并读过；中断时保留真实覆盖，不能仅靠前半篇给全文结论 |
| 频繁多轮精读，需要持久按需访问 | 用户实际需要时先 capture 到 reader material store；再提供受限 read handle | capture/read 工具、权限和生命周期已实现且 capability 可见 |
| 明确整理为主库知识 | 提交 durable maintain，待成功后绑定真实 Wiki 文档 | job 发布与索引回执明确；上传或排队不等于入库完成 |

首个局部问题建议只发送问题所需的选区及约一个小节；字符预算根据实际材料调整。没有必要从 150k 机械改成另一个所有问题都相同的固定上限。

若暂未实现 reader capture/read 能力，使用前两条完整可行的路径，不调用不存在的全文工具。需要新增时，为 capture 生成受账户、app、conversation/task 约束的 handle；read 只接受已授予的 handle 与 revision/范围。不得以传来的 URL、本地绝对路径、文件 URI 或别的 app 目录任意读取文件。

capture 只保存材料，未进入 Wiki 的默认索引，不自动加 Key note、tags、Concept 或个人事实。其保留/删除与浏览器缓存是 reader 自己的生命周期；用户明确入库后才转入权威主库的内容规则。

### 5.3 上下文装配

现有 `AppAgentTurnRequest.context` 是字符串，材料说明可暂以版本化 JSON 序列化；模型输入不得从这个字符串直接获得权限。若新增结构化请求字段，必须同时更新 Pydantic、app transport、实际服务校验和前端。

每轮保留：用户原消息、目标材料/版本、此次选区和新提供范围、确有必要的任务状态。不要每轮重复全文、全部摘录、全部历史消息、目录或全库摘要。工具 schema 与 capability 声明真实可用读法；没有开启新 API 时按旧接口退回有界的片段阅读。

连续三轮对话的请求 payload 应成为验收证据：第二、第三轮不能携带与第一轮相同的 150k 正文块。模型 usage 单独记录，不能仅看网络 payload 变小就宣称全部输入 token 消失。

## 6. 选区与引用直达

### 6.1 四类位置

| 位置类型 | 单位与内容 | 可直接做什么 |
| --- | --- | --- |
| Wiki 原文本范围 | 服务端原提取文本、Python codepoint、revision | 调用 Wiki read/read_many |
| Markdown block/heading | 原文 block 标识和渲染映射 | 找对应渲染块，再定位其中 quote |
| 阅读 DOM 选区 | 渲染后 UTF-16、exact quote、prefix/suffix | 在该渲染版本核查与高亮 |
| PDF 页面选区 | 页码、该页文字层 quote 与上下文 | 在相同 PDF/提取版本核查；不能冒充 Markdown offset |

当前 SmartReader 把 DOM UTF-16 offset 记录为 `start/end`，而 Wiki 原文 read 使用 codepoint。二者必须显式转换或重定位，不能只重命名字段。[A2]

Markdown 渲染保留 backend block/heading 到 DOM 的映射；选区转服务坐标时核对当前原文。数学公式、emoji、链接、空白规范化和去掉 frontmatter 均会改变显示长度。exact quote + prefix/suffix 是定位线索，不保证唯一；重复段落应限定章节/页面，仍歧义时显示候选或仅定位到章节。

### 6.2 从回答进入阅读器

回答引用携带或可解析到真实文档 identity、revision、heading/block、必要 range 与 quote。具体引用 envelope 遵循[检索协议](local-shared-wiki-retrieval-v2.md)，本 app 不定义另一套权威字段。

打开顺序：

1. 服务端验证 app/账户和文档身份，解析当前路径；
2. 若 revision 相同，使用已知范围与渲染映射；
3. 若 revision 不同，按稳定 block/heading、quote 与上下文重新定位；
4. 缺失、歧义或只有历史快照时明确显示实际状态，允许继续查看可用位置。

跨 app 导航地址由 Octopus 解析真实 SmartReader app ID 后构造，不假设固定 slug；地址只包含必要引用参数或 opaque reference，不包含凭据、文章全文或私密上下文。链接解析不授权读取任意文件。

SmartReader 现有 `navigateToQuote` 的首次 `indexOf` 回退可继续服务单一未变材料的本地便利操作，但不能作为新 Wiki 精确引用的最后正确性判定。UI 说“定位到引用”时，必须已确认实际命中。

## 7. 阅读写作与 Wiki mutation

### 7.1 默认讨论无需写作流程

“为什么作者这样说”“这个假设合理吗”“数字人方向能否用这个思路”属于阅读与思考。Weber直接讨论，区分文章观点、用户观点、自己的推断；只有确有查库需要且本次获得 Wiki read 权限时再取相关资料。普通解释不加载 writing module，也不自动创建摘要或新笔记。

用户明确要求“写”“改”“整理成一段”“把它加入我的草稿”时，按任务复用共享写作指南。该指南负责结构、表达、证据归属和保留条件，不决定工具权限。Weber 的智者与思考伙伴身份保留；不把整个读书对话变成工程验收表。

### 7.2 草稿能力

SmartReader 可以产生两种写作结果：一段可复制的对话文本，或有身份与版本的 reader draft。明确的文件草稿后续规划应提供：

- 草稿与原材料分别显示，标明来源引用；
- 用户可查看、继续编辑和下载；
- agent 修改带 draft revision 与明确范围，冲突时保留双方内容；
- UI 只在实际保存回执到达后标“已保存”；
- app rebuild 后草稿仍属于 app data，代码目录不成为资料目录。

框架已有 app 对话工作目录为 app `.data` 的机制，首轮提示也将普通写入指向这里。[F3] 但当前 SmartReader 没有草稿 API/UI；需补完整闭环，不假设模型写了一个文件就能出现在页面。

**生成/应用 reader 草稿不会扩大 Wiki 权限。** 不因点击“写得更清楚”就加 `wiki_mode=maintain`；不因输出符合 OKF 外观就宣布进入主库。

### 7.3 明确入库与范围修改

用户说“把这篇整理入库”，或明确说“把这段见解补进这份研究笔记”，使用既有 durable maintain。提交内容包括实际材料、可追溯来源、目标/范围和稳定 idempotency key；同一次点击、SSE 重连或客户端重试不能创建多个任务。[F4]

如果是阅读草稿入库，提交用户选定的草稿 revision；如果是网页入库，保留实际取得的原文与 source URL；如果只是讨论摘要，注明它是讨论产物及所依据的材料，不能伪装成外部来源原文。维护结果可为原位模板化、局部纠错/补充或有独立用途的新文件，遵循[内容合约](local-shared-wiki-content-v2.md)的去重与来源角色。

将研究问题补进已获准的旧报告，使用现有 `blocks/restructure`：先完整读取当前目标，满足 read ledger 并核对 revision；保持每个原块与原顺序，在指定位置追加新段。organize 处理它支持的生成区，publish 只创建新页，不用新建文件来冒充旧页更新。新增区及来源经过 V2-02 的共同 OKF 适配；任意替换受保护正文不属于这条纯追加能力。

若请求是“修正已有概念页的解释”，worker 按[定点维护](local-shared-wiki-maintenance-v2.md)读取最新内容，使用 revise 局部纠错或补充，保留思想、论证与风格；过去人工编辑不阻止维护，读后并发变化才需重读。SmartReader 只提交目标和修改意图，read/submit 不包含直接正文编辑；来源声明或打开过文件不产生写权限。

不先由 SmartReader 写一份主库文件，再让 maintain 把同一材料当新文写一次。确有“已经创建后整理”的既有路径，沿用真实 creation change_id 与当前 revision；当前普通 app 保存响应未必提供它，应从真实 history 或新的桥接回执取得，不能编造 `created_changes`。

用户已明确授权同一范围写改时继续执行；不要求“生成草稿 → 审批 → 再授权同一修改”的固定链。目标、冲突或新增无关文件确有歧义时才澄清相应问题。写作模块的启用不构成另一份授权。

任务回执至少让用户分辨提交、处理中、失败/冲突、已发布、索引待更新及可用文档；复用现有 job 状态，不自造第二套独立 job 系统。页面关闭或 SSE 断开不应撤销 durable job。当前聊天“停止接收”与后续“取消维护任务”是不同操作，要分别接现有真实能力。

## 8. 日常使用模拟

| 问法/动作 | 预期协同 |
| --- | --- |
| 阅读 Vidu 时选中一个指标定义：“这和 FPS 什么关系？” | SmartReader 给准确选区和 Wiki 身份；本次 read 取必要定义上下文；Weber解释，引用可回跳，无写入 |
| “我不同意作者把模型压缩说得这么简单，你怎么看？” | 对话围绕实际论证，标出推断；不自动生成持久笔记 |
| “把刚才的讨论写成一段研究日志” | 按共享写作指南形成对话稿或用户指定 reader draft；无 Wiki mutation |
| “把这段加到我当前草稿的实验问题部分” | 修改指定 draft revision 与区段；不重写整篇、不改 Vault |
| “这篇是否和库里的 MetaHuman 研究有关？” | 本次明确查库；先找相关候选、读代表证据，再说明关联程度，不只靠共同标签判同义 |
| “把这个网页整理入库，保留原文” | 提交实际抓到的材料一次；Weber沿现有维护 worker 适配/去重，完成后绑定返回的 Wiki id |
| “把刚才的研究假设补进现有报告的研究问题” | 已明确同范围修改则提交维护；检查当前 revision、用户区与目标，不追加无关 Concept 页 |
| 在 Sapphire 点“数字人”标签后打开 Vidu | Sapphire 服务端过滤后的候选导航到 SmartReader；阅读器不再自己做主库子串排名 |
| 离线打开浏览器 PDF：“总结整篇” | 先确认文字是否完整可达；只有本地页选区时不能声称全文已读；按实际分批或 capture 能力完成 |
| 点击旧回答的引用，目标后来改名或改过段落 | 用服务 id 找当前文件；核对 revision，重新定位；不能继续高亮旧 offset |

这些行为依赖当前真实内容和能力。查不到外部论文原文时不能把本地技术报告改称论文原文；收藏的作者观点不变成用户个人偏好。

## 9. 验收与实施边界

V2-09 执行时应完整覆盖它选择交付的用户行为，而不是增加几个按钮后留下无效路径。必要验收如下：

1. **权限双入口：** owner app 保持原归属；SmartReader read/submit 各自有效；缺权限时直接 gateway 与 agent/chat/ask、手写 `/wiki` 一致；不允许 consumer PUT/publish 或跨 app 任务操作。
2. **材料可达：** 浏览器-only、capture、Wiki-bound 各跑真实阅读；不存在的材料工具不进入 prompt。全文任务实际完整覆盖，局部任务不被迫全读。
3. **成本：** 同一文档连续三轮不重复发送 150k；比较 payload、工具输出、模型 usage 和回答质量。当前无真实改善数值，不提前声称减少某个百分比。
4. **引用：** 中文、emoji、公式、重复句、同名标题、旧 revision、改名和 PDF 页内引用均可核查；精确定位失败时 UI 如实降级。
5. **草稿：** 对话文本、已保存 reader draft、主库文章是不同状态；版本冲突不丢内容，普通解释不触发写作/入库。
6. **维护：** 一次明确入库产生一个 durable job；同范围编辑不重复请求确认；发布和索引可追溯，失败/重连/关闭页面不制造重复文档。
7. **原交互：** 保留已有材料×agent 会话关联、恢复/删除、换材料时的 openEpoch 保护、PDF 等待、摘录和下载；用户选择的 agent 可继续使用，默认 Weber 不锁死该能力。

这些是未来 V2-09 的完成条件，不是本轮已测结果。核心检索、共享 prompt 和旧资料适配先按[开发票](local-shared-wiki-tickets-v2.md)推进；SmartReader 后续 UI 不成为试点的等待条件。

## 10. What this defers

- 本轮按用户要求只将 SmartReader 功能列入计划，不启动 app 开发、provider 调用、client 授权或上线。
- Reader material capture/read、草稿编辑 API 和新的结构化引用入口按实际使用选择完整实现；不存在的能力不能预先写入启用 prompt。
- 不默认把所有网页缓存入 Wiki，不自动把每次讨论做成 Synthesis/Concept，不新建另一份全文/向量知识库。
- 当前 Wiki 的 PDF 不提取约定保持；SmartReader 浏览器能展示或抽取 PDF 文字，不能自动改变 Wiki 的抽取边界。
- 智能跨设备阅读同步、复杂协同编辑、操作系统级 app sandbox 不属于本轮新增需求。

## 来源与代码证据

- [A1] 用户附件 SmartReader：`app.js::addDocument/openDocument/importFiles/importURL/contextKey/sessionForCurrent/sendMessage/downloadDocument/exportNotes`；`lib/storage.js`、`lib/agents.js`、`reader_server.py`、`README.md`。源码快照只读审阅，未执行应用。
- [A2] 用户附件：`lib/content.js::readingContext`（99–122），`app.js::captureSelection`（886 起）、`navigateToQuote`（962 起）。150k/12k 及 DOM offset 结论来自真实实现，不是一般性猜测。
- [F1] [WikiRegistry](https://github.com/Danuvius152/Octopus/blob/453a7995bd6c54df33b0fc41dec09d370b4b40aa/server/wiki/registry.py)、[app_wiki](https://github.com/Danuvius152/Octopus/blob/453a7995bd6c54df33b0fc41dec09d370b4b40aa/server/routers/wiki.py)。
- [F2] [AppMutation / begin](https://github.com/Danuvius152/Octopus/blob/453a7995bd6c54df33b0fc41dec09d370b4b40aa/server/wiki/app_bridge.py)：owner app 和实际 root 校验、写入历史桥。
- [F3] [AppAgentManager](https://github.com/Danuvius152/Octopus/blob/453a7995bd6c54df33b0fc41dec09d370b4b40aa/server/app_agent.py)、[AppAgentTurnRequest](https://github.com/Danuvius152/Octopus/blob/453a7995bd6c54df33b0fc41dec09d370b4b40aa/server/models.py)：现有 message/context/wiki_mode 与 app data 工作目录。
- [F4] [Wiki 维护 API](https://github.com/Danuvius152/Octopus/blob/453a7995bd6c54df33b0fc41dec09d370b4b40aa/server/routers/wiki.py)、[WikiJobs / WikiWorker](https://github.com/Danuvius152/Octopus/blob/453a7995bd6c54df33b0fc41dec09d370b4b40aa/server/wiki/jobs.py)：维护幂等、任务范围及真实发布。
- [F5] [WikiTask / tasks.require](https://github.com/Danuvius152/Octopus/blob/453a7995bd6c54df33b0fc41dec09d370b4b40aa/server/wiki/tasks.py)、[TaskWiki / MCP call](https://github.com/Danuvius152/Octopus/blob/453a7995bd6c54df33b0fc41dec09d370b4b40aa/server/mcp_servers/wiki.py)：当前任务记录与普通 Wiki REST 调用路径。
