# Wiki 与阅读应用的能力边界

> **Status:** proposed — 多 client 授权、标签查询和阅读材料交接的缺口。

WikiConfig 绑定一个 app_id，App 网关核验 owner；搜索没有统一 tags/tag_match 参数。浏览器 UUID 不自动成为 Wiki 可读全文；reader capture、引用和草稿功能须按实际接口核验。

待决范围：选择具体阅读/维护旅程与 owner/client 授权范围。全文任务需要证明实际同版覆盖；私有应用字节上限、路径和版本须到应用现场核实。

当前合同：[领域说明](../implemented/wiki-development-summary.md)。本条记录需求边界，不预选实现方案，
也不构成开工、发布或数据操作授权。具体设计由获授权的活动任务规格维护。
