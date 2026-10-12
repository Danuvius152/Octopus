Wiki v2 检索先定位：保留问题中的命名对象和原词，精确路径/id/uid 可直接 read。明确文章用 lookup，具体问题用 question，宽主题用 overview，多方比较用 compare；新增能力可用时显式选择 view="compact"。候选有歧义时用题名、目录和描述区分，必要时补可靠别名。

选材：description、Key note 和索引卡用于导航，freshness=indexed 不是已读。宽主题覆盖实际不同方向，明确展示副本和纯重复摘要限制配额；不同文章不因共同引用合并。Concept/Synthesis 有独立解释价值，可以读取并复用。lexical_partial、partial/unavailable 和没有知识分别判断，合理调整原词与必要浏览后才报告缺口。

当前证据：按 revision/sections 定点 read；read_many 的实际内容在 ranges 中，items 的 range_id/start/end 给出覆盖。失败项和预算未返回项没有读过；按需继续 next_start。坐标是 Unicode code-point 零基半开区间，行号一基；版本变化先取得新位置，cursor 代变化重启列表。

来源：知识页覆盖问题且解释仍适用时复用其论证；精确数字、条件、作者措辞、冲突和变更再追读相应来源。链接与外部出处不等于已读，不自动上网。整体论证和全篇评价从0到next_start=null完整读取；局部判断读足上下文，多方比较覆盖各方条件。图片影响判断才 inspect_image；PDF 本轮保留为附件。
