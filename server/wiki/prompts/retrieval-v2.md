Wiki v2 检索先定位：保留问题中的命名对象和原词，精确路径/id/uid 可直接 read。明确文章用 lookup，具体问题用 question，宽主题用 overview，多方比较用 compare；新增能力可用时显式选择 view="compact"。候选有歧义时用题名、目录和描述区分，必要时补可靠别名。

选材：索引卡中的 description、Key note 摘录和 snippet 用于导航，freshness=indexed 不是已读。实际 read 到的当前 Key note、Concept/Synthesis 可用于其覆盖的问题，注明摘要或综合的来源层级；有条件、精确数字或证据缺口再补正文。宽主题覆盖实际不同方向，明确展示副本和纯重复摘要限制配额；不同文章不因共同引用合并。lexical_partial、partial/unavailable 和没有知识分别判断，合理调整原词与必要浏览后才报告缺口。

当前证据：按 revision/sections 定点 read；read_many 的实际内容在 ranges 中，items 的 range_id/start/end 给出覆盖。失败项和预算未返回项没有读过；按需继续 next_start。坐标是 Unicode code-point 零基半开区间，行号一基；版本变化先取得新位置，cursor 代变化重启列表。

模型工具默认 compact，仅返回所选正文及定位、版本和必要警告；完整章节元数据按需显式 view="full"。budget_chars 只计去重正文字符；compact 另限制包含文本 JSON 及包装的序列化 MCP 结果字节数，可能返回较短正文，以实际范围和 next_start 续读。字节数不是 token 数，客户端仍可能截断；后续失败或截断的内容不能用服务端已读记录补证。全文理解和维护终稿冷读仍须逐段完成。

来源：知识页覆盖问题且解释仍适用时复用其论证；精确数字、条件、作者措辞、冲突和变更再追读相应来源。链接与外部出处不等于已读，不自动上网。整体论证和全篇评价从0到next_start=null完整读取；局部判断读足上下文，多方比较覆盖各方条件。图片影响判断才 inspect_image；PDF 本轮保留为附件。
