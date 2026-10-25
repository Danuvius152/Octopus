这是按需维护细则，权限仍以当前 job、身份、版本和可写范围的服务校验为准。模板化及原块布局见 article，新解释的表达见 writing，关联选材见 retrieval；已在上下文的模块直接复用。

局部修订用 revise(operation="patch")：已知块 ids 可直接 blocks(ids=[...])；文末追加先 blocks(tail=2)，取得末块及邻块的实际原文和定位。只有位置不明才取少量 locators 再读目标块，不枚举全篇坐标。使用当前 locator、expected_block_hash 和逐字 expected_text；start/end 是该原始块内 code-point 零基半开区间。correction 只替换明确错误，addition/annotation 在 start=end 处插入；重复句依靠定位区分，多补丁互不重叠。read/search 的提取 offset 不可直接当 raw offset。原始块分页按 worker 的续读规则，只有实际变化才重新定位；过去人工编辑、AI 作者或旧 ownership 均不决定修订资格。

保全思想：有风格的判断、推测和引文保留其主体、语境及强度。事实有新条件时补充证据或更正说明，不替原作者改立场；引文疑点在引文外注明。纠错不顺带润色整段，不用新摘要替换正文。修改涉及旧管理区时由服务更新或撤销过期定位，历史继续保留。

先加载当前 worker 指南。正文改动后的阅读覆盖、版本连续性及 no_change 例外，遵守 worker 的完整终稿冷读合同。

来源重读后原解释仍适用，用 confirm_sources 只更新实际检查的直接依赖；未检查部分保留。文章已经满足任务，用 no_change 记录当前实际复核，不为结束队列改字；全篇任务仍需同版全文，局部任务读足目标与上下文。不能以一次来源确认或“已核验”认证未读整篇，verified 不由模型自报。正文修订不顺带重做标签、摘要或来源。

异常恢复先用原 job_id 或固定 idempotency_key 查 jobs 默认摘要；只在确实需要单项回执或失败详情时取完整结果，大结果用 view="full", export=true，按返回路径、bytes 和 SHA-256 核文件，短程序只读所需字段，不把大 JSON 拼进 shell 参数。检查工具实际 error/status，外层调用成功不代表操作成功。已保存但索引失败只恢复索引，不再生成或重放正文；成功且未变项不换 key 重排。冲突时在原范围重读当前版本，失败的提议和历史保留，不使用旧坐标或旧阅读覆盖。

多输入任务中新建独立页面用 item_doc 指明所属项。publish 只用于获准、有独立用途的新页；不为文章入库强制创建来源副本或 Concept/Topic。普通文件保存、目录移动和自动索引不触发语义整理；人工小修后的明确维护按当前实际版本及必要上下文处理，不重复整理整库。

原始块的完整 hash/locator/start/end 不因截断变化，实际已读范围是 content_start..content_end；只读定位符不算已读正文，原块 end 不代表本次返回末尾。
