# 本地共享 Wiki：实施交接

> **Status:** in-progress — 框架与 Weber 已部署验收；首次旧库批量整理等待 Q35 样稿确认。

2026-10-12。用户 Q1–Q36、统一 `/wiki maintain` 及日常只整理新增文件的决定已实施。框架和实际 Weber 已交付；旧库批量整理仍遵守用户确认的 Q35：先看三篇样稿，再开始批量写入。

## 使用与实际行为

- `/wiki <问题>` 与 `/research`、`/model` 一样在 Octopus 聊天命令入口使用。本条消息开启查库；下一条普通消息关闭授权。
- `/wiki maintain <新材料>` 入库并交给实际 Weber 整理。需要整理旧文件时，首行明确列出相对路径，含空格的路径使用引号；无材料和范围不会启动全库整理。
- 日常新材料整理只写这次的新文件。已有相关笔记可读，关联写入新文，反向关联由索引呈现。明确选中的旧文件维护有独立范围。
- 普通聊天仅含简短能力说明，不带知识摘要和 Wiki 工具。授权经排队、重试、可信委托、应用与计划任务传递，结束后撤销。原生私有 agent memory 继续独立。
- 读取支持中文检索、分页、当前 revision、行号、目录、图片和关联图。Key note 是定位入口；涉及重要结论要读取正文，全文讨论要完成分页。
- 管理工具逐字保留已有正文、frontmatter、原路径和附件，增加 Weber 标注的 Key note、细标签和必要关联；已有开头要点不重复生成。TXT/HTML 原件不变，整理页单独生成。
- 内容文件是真相。索引可重建，派生页有过期检查；普通保存不自动整理。用户改动生成区时保留它并留下冲突候选。
- 共同写锁、乐观版本检查、二进制历史、快照和 ownership 状态共同恢复，崩溃恢复后仍能准确区分原文和生成区。
- 维护队列持久化，有幂等、逐项产物、取消、失败、重试与重启恢复。实际 Weber worker 每批最多三篇，发布前必须完整读取目标版本；模型宣称完成不代表任务完成。
- PDF 本轮完全排除内容处理。工具、tmp、临时提取和配套静态资产保留，默认不参加普通笔记检索和模板整理；明确关联时可按需读取文本或图片。

实际 prompt 见 `server/wiki/prompts/{entry,query,maintenance,writing,curator}.md`。查询方法参考 my-wiki；维护与作者风格校准参考 Pika obsidian-vault-notes，未迁入其 PDF 管线、自动查库或改写原文权限。

## 已部署的本地配置

主库继续使用 Sapphire 的 `.data`：
`/home/jack/.octopus/users/f527ad605dd4/applications/sapphire.data/vaults/obv/repo`。
账户 `f527ad605dd4`，库 `6bdb9fe5-3453-47a8-8510-9a892c510346`，Sapphire `3e7830a1336b`，实际 Weber `f37612d667ec`。

Weber 仅更新 system prompt，保留原有 Codex / gpt-6.1-sol / high、credentials、MCP 配置、私有记忆和其他字段。persona 保留智者、读书与思考伙伴身份，加上简短表达原则；详细知识规则按任务加载。

修改前备份位于：
`/home/jack/.octopus/users/f527ad605dd4/wiki-backups/20261011T201749Z`。
包含 `octopus.before.db`、`weber.before.json`、`weber.after.json` 和验收记录，均为本机私有文件，不入 Git。
二进制快照 `d02ee2b85ffc4cb89f3a1b07b149d15c` 保存全部 780 文件、570,451,724 字节。生产后端已用 `systemctl --user restart octopus` 重启并健康检查，再完成前端构建。

最终部署核对：Sapphire 知识网关与 curator 绑定正常，三篇样稿来源版本当前，全库 780 个原文件仍与二进制备份一致。

实际 Weber 验收：普通讨论零知识调用；显式查库读取当前笔记并引用版本/行号；新材料经 Weber 入库并完成。临时测试笔记随后通过历史接口删除，完整原库文件清单与字节哈希和验收前一致；本次临时验收令牌已撤销。

## 验证结果

- 完整后端隔离测试：**1,399 passed**，36 real deselected。最终日志保存于上述私有备份的 `validation/` 目录。
- 前端单元测试：**306 passed**；`tsc -b --force`、ESLint、前端构建通过。
- 浏览器快速 E2E：**69 passed**；Codex 实机浏览器 E2E：**1 passed**，无跳过。
- Codex 后端实机相关用例：**11 项通过**。最终 marker 运行 10 项通过，research 缺目录式入口而跳过；按本机文档用临时符号链接接入现有登录后，该项实机补验通过，未把跳过计为通过。首轮维护用例的异常处理器在启动后注册，导致预期 422 表现为 500；修复测试服务器构造后补验通过，未把原失败算通过。
- 知识 HTTP 用例 **37 passed**，涵盖真实作用域、完整阅读门槛、生成区保护、崩溃 ownership 恢复、伪造新文件清单拒绝、关联图片、版本冲突及恢复。Sapphire 兼容性另覆盖并发保存、异步 Git 暂时 503、取消与锁等待。
- Ruff、mypy、OpenAPI contracts 漂移与文档目录检查通过。
- 用户明确选择 **Codex 验收**；Claude 实机未验证，未将跳过当作通过。

本次提交从独立暂存树构造，并额外检查该树：后端 **1,352 passed**，36 real deselected；前端 **299 passed**，TypeScript 也独立检查。工作区更多测试属于用户原有改动，未混入本次提交。审查信息见[审查记录](local-shared-wiki-review.md)。

## 尚需用户确认的具体结果

首次旧库三篇预览由实际 Weber 只读生成，包含完整原文及新增 Key note/标签；原库尚未写入。样稿和逐篇版本、覆盖说明、含义自检保存在上述私有备份目录的 `samples/` 中，原始正文不复制入 Git。

用户确认风格后，才执行此前约定的普通 MD/TXT/HTML 及必要图片范围；PDF 与工具/tmp/静态资产继续排除。运行时记录每项当前来源、产物和错误，不因文件扩展名就把 143 篇 Markdown 全部当笔记整理。

Sapphire app 界面在应用内部 Iterate 开发；可直接复制[完整 Iterate prompt](../sapphire-wiki-iterate-prompt.md)。当前应用源码保持原有功能，框架网关已提供共同检索、历史、维护和明确问库接口。
