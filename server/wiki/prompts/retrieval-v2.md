Wiki v2 检索按已知程度选入口：保留问题中的命名对象和原词，单个已知路径/id/uid 及范围直接 read，已确定多个目标或范围用 read_many；未知位置用 search(view="compact") 或相关目录定位。明确文章用 lookup，具体问题用 question，宽主题用 overview，多方比较用 compare。候选有歧义时用题名、目录和描述区分，必要时补可靠别名；取得身份后直接读取。

选材：索引卡中的 description、Key note 摘录和 snippet 用于导航，freshness=indexed 不是已读。实际 read 到的当前 Key note、Concept/Synthesis 可用于其覆盖的问题，注明摘要或综合的来源层级；有条件、精确数字或证据缺口再补正文。宽主题覆盖实际不同方向，明确展示副本和纯重复摘要限制配额；不同文章不因共同引用合并。lexical_partial、partial/unavailable 和没有知识分别判断，合理调整原词与必要浏览后才报告缺口。

当前证据：按 revision/sections 定点 read；read_many 的实际内容在 ranges 中，items 的 range_id/start/end 给出覆盖。失败项和预算未返回项没有读过；按需继续 next_start。坐标是 Unicode code-point 零基半开区间，行号一基；版本变化先取得新位置，cursor 失效后重定位。next_cursor 原样传回，字节上限截断时直接续页，增大 limit 不会取得缺失部分。kind 使用工具枚举的小写索引类型（如 topic），与文章 frontmatter 的 type（如 Topic）不同。

复用实际正文：本任务已经返回且仍在本调用方上下文的同 revision 范围可直接用于其覆盖的判断，只补缺口；多个已知目标/范围合并 read_many，并带 expected_revision。仅在先前正文仍保留时显式 reuse_delivered=true，already_delivered 只引用先前范围；上下文缺失便省略该选项重新取回正文。其他任务、账户或助手的阅读不代替当前取证。更新、移动、删除、同路径替换、重绑或撤权后按现有 live read/授权合同重新核验，不复用旧票据或权限。

模型工具默认 compact，仅返回所选正文及定位、版本和必要警告；完整章节元数据按需显式 view="full"，同样受 32,768 UTF-8 字节的完整 MCP 结果上限约束。budget_chars 只计去重正文字符；字节预算可能缩短正文或本页条目，以实际 next_start、next_cursor/next_offset 续读，不跳过未返回项。response_too_large 时改用 compact、缩小选择或 jobs 的完整文件导出；不能用 full 绕过预算。字节数不是 token 数，客户端仍可能截断；后续失败或截断的内容不能用服务端已读记录补证。全文理解和维护终稿冷读仍须逐段完成。

来源：知识页覆盖问题且解释仍适用时复用其论证；精确数字、条件、作者措辞、冲突和变更再追读相应来源。链接与外部出处不等于已读，不自动上网。整体论证和全篇评价要求实际同版范围从0无缺口覆盖全文末尾；复用上下文已有范围，按真实 next_start 补未返回部分。局部判断读足上下文，多方比较覆盖各方条件。图片影响判断才 inspect_image；PDF 本轮保留为附件。
